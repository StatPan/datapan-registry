"""Pure validation for attempt-bound canonical-update terminal artifacts.

The caller authenticates the source contract and obtains complete exact-attempt
GitHub API snapshots. This module performs no I/O, clock reads, subprocess
calls, environment lookups, or state mutation. A verified outcome is historical
C-mode evidence, not a promotion, publication, read-back, or current-candidate
decision.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import pathlib
import re
import stat
import zipfile
from collections.abc import Mapping
from typing import Any


SCHEMA_VERSION = "datapan.canonical-update-promotion-terminal-outcome.v1"
WORKFLOW_PATH = ".github/workflows/canonical-update-promotion.yml"
SCHEMA_PATH = "schemas/datapan.canonical-update-promotion-terminal-outcome.v1.schema.json"
RUNNER_PATH = "scripts/run-canonical-update-promotion.py"
SCHEMA_ID = "https://schemas.datapan.dev/datapan.canonical-update-promotion-terminal-outcome.v1.schema.json"
REPOSITORY_RE = re.compile(r"^[^\s/]+/[^\s/]+$")
REVISION_RE = re.compile(r"^[a-f0-9]{40}$")
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
RUN_ID_RE = re.compile(r"^[0-9]{1,20}$")
SHA_LINE_RE = re.compile(rb"([a-f0-9]{64})  terminal-outcome\.json\n\Z")

MAX_ARCHIVE_BYTES = 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024
MAX_JSON_BYTES = 768 * 1024
MAX_SHA_BYTES = 128
MAX_ARTIFACT_ROWS = 2000
MAX_GENERATIONS = 64

MODE_STEPS: dict[str, dict[str, str]] = {
    "reconcile-prs": {
        "invocation_step": "Reconcile owned PRs and exact-head CI",
        "upload_step": "Upload C terminal outcome (reconcile-prs)",
    },
    "refresh-owned-source": {
        "invocation_step": "Refresh one explicitly bound owned source revision",
        "upload_step": "Upload C terminal outcome (refresh-owned-source)",
    },
    "recover-ready": {
        "invocation_step": "Recover at most one durable ready processor bundle",
        "upload_step": "Upload C terminal outcome (recover-ready)",
    },
}

EVALUATOR_SOURCE_PATHS = frozenset({
    WORKFLOW_PATH,
    RUNNER_PATH,
    "scripts/canonical_update_pr.py",
    "scripts/canonical_update_ci.py",
    "scripts/refresh-canonical-snapshot-evidence.py",
    "scripts/upstream-catalogue-state-branch.py",
    "scripts/upstream_catalogue_handoff.py",
    "scripts/compose-upstream-catalogue-candidate.py",
    "scripts/generate-batch-link-detail-registry-patches.py",
    "scripts/seoul_oa109_operation_declaration.py",
    "scripts/upstream_catalogue_derivation.py",
    "scripts/materialize-canonical-registry.py",
    "scripts/check-upstream-catalogue-health.py",
    "scripts/canonical_update_terminal_evidence.py",
    SCHEMA_PATH,
    "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
    "schemas/datapan.catalogue-composition-receipt.v1.schema.json",
    "schemas/datapan.catalogue-enrichment-evidence.v1.schema.json",
    "schemas/datapan.specs.v1.schema.json",
    "schemas/datapan.provider-index.v1.schema.json",
    "schemas/datapan.catalog-diff.v1.schema.json",
    "schemas/datapan.upstream-refresh-evidence.v1.schema.json",
    "schemas/datapan.canonical-update-promotion-journal.v1.schema.json",
    "schemas/datapan.canonical-update-promotion-receipt.v1.schema.json",
    "policy/upstream-catalogue-health.json",
})


class _EvidenceProblem(Exception):
    def __init__(self, status: str, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class _ExternalSchemaResolutionForbidden(Exception):
    pass


def _fail(status: str, code: str) -> None:
    raise _EvidenceProblem(status, code)


def _is_int(value: Any, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _id_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return value
    return None


def _parse_time(value: Any, code: str) -> dt.datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        _fail("rejected", code)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("rejected", code)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("rejected", code)
    return parsed.astimezone(dt.timezone.utc)


def _as_of_time(value: dt.datetime | str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            _fail("rejected", "as_of_timezone_missing")
        return value.astimezone(dt.timezone.utc)
    return _parse_time(value, "as_of_invalid")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("invalid_json_constant")


def _walk_schema_refs(value: Any, *, root: bool = False) -> None:
    if isinstance(value, Mapping):
        if "$id" in value and (not root or value.get("$id") != SCHEMA_ID):
            _fail("rejected", "schema_external_reference_forbidden")
        for key in ("$ref", "$dynamicRef", "$recursiveRef"):
            if key in value:
                ref = value[key]
                if not isinstance(ref, str) or not ref.startswith("#/"):
                    _fail("rejected", "schema_external_reference_forbidden")
        for child in value.values():
            _walk_schema_refs(child)
    elif isinstance(value, list):
        for child in value:
            _walk_schema_refs(child)


def _deny_external_schema_resource(uri: str) -> Any:
    raise _ExternalSchemaResolutionForbidden("external schema resource resolution is disabled")


def _offline_schema_validator(schema: Mapping[str, Any], jsonschema: Any) -> Any:
    """Build a validator whose resolver cannot fetch an external resource."""
    try:
        from referencing import Registry
    except ImportError:
        base_resolver = jsonschema.validators.RefResolver

        class NoRemoteResolver(base_resolver):
            def resolve_remote(self, uri: str) -> Any:
                return _deny_external_schema_resource(uri)

        resolver = NoRemoteResolver.from_schema(schema)
        return jsonschema.Draft202012Validator(
            schema, resolver=resolver, format_checker=jsonschema.FormatChecker(),
        )
    else:
        registry = Registry(retrieve=_deny_external_schema_resource)
        return jsonschema.Draft202012Validator(
            schema, registry=registry, format_checker=jsonschema.FormatChecker(),
        )


def _validate_source_contract(
    context: Mapping[str, Any], source_contract: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    repository = context.get("repository")
    workflow_id = context.get("workflow_id")
    source_sha = context.get("head_sha")
    mode = context.get("mode")
    if (
        not isinstance(repository, str) or REPOSITORY_RE.fullmatch(repository) is None
        or not _is_int(workflow_id, minimum=1)
        or context.get("workflow_path") != WORKFLOW_PATH
        or not isinstance(source_sha, str) or REVISION_RE.fullmatch(source_sha) is None
        or mode not in MODE_STEPS
    ):
        _fail("unavailable", "expected_context_incomplete")
    if (
        source_contract.get("repository") != repository
        or source_contract.get("workflow_id") != workflow_id
        or source_contract.get("workflow_path") != WORKFLOW_PATH
        or source_contract.get("source_sha") != source_sha
        or source_contract.get("schema_path") != SCHEMA_PATH
        or source_contract.get("mode_steps") != MODE_STEPS
    ):
        _fail("unavailable", "source_contract_identity_mismatch")
    source_files = source_contract.get("source_files")
    if not isinstance(source_files, Mapping) or not EVALUATOR_SOURCE_PATHS.issubset(source_files.keys()):
        _fail("unavailable", "source_contract_files_incomplete")
    for path in EVALUATOR_SOURCE_PATHS:
        digest = source_files.get(path)
        if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
            _fail("unavailable", "source_contract_file_digest_invalid")
    schema_bytes = source_contract.get("schema_bytes")
    schema_sha = source_contract.get("schema_sha256")
    if not isinstance(schema_bytes, bytes) or not isinstance(schema_sha, str) or DIGEST_RE.fullmatch(schema_sha) is None:
        _fail("unavailable", "source_contract_schema_unavailable")
    if hashlib.sha256(schema_bytes).hexdigest() != schema_sha or source_files.get(SCHEMA_PATH) != schema_sha:
        _fail("rejected", "source_contract_schema_digest_mismatch")
    if not schema_bytes or len(schema_bytes) > MAX_JSON_BYTES:
        _fail("unavailable", "source_contract_schema_size_invalid")
    try:
        schema = json.loads(
            schema_bytes.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _fail("rejected", "source_contract_schema_invalid")
    if (
        not isinstance(schema, dict)
        or schema.get("$id") != SCHEMA_ID
        or schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or schema.get("properties", {}).get("schema_version", {}).get("const") != SCHEMA_VERSION
    ):
        _fail("rejected", "source_contract_schema_identity_invalid")
    _walk_schema_refs(schema, root=True)
    try:
        import jsonschema

        jsonschema.Draft202012Validator.check_schema(schema)
    except ImportError:
        _fail("unavailable", "jsonschema_dependency_unavailable")
    except Exception:
        _fail("rejected", "source_contract_schema_invalid")
    return schema, str(mode)


def _normalize_invocation(context: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _id_text(context.get("run_id"))
    run_attempt = context.get("run_attempt")
    event = context.get("event")
    head_sha = context.get("head_sha")
    default_branch = context.get("default_branch")
    mode = context.get("mode")
    if (
        run_id is None or run_id == "0" or RUN_ID_RE.fullmatch(run_id) is None
        or not _is_int(run_attempt, minimum=1)
        or event not in {"schedule", "workflow_run", "workflow_dispatch"}
        or not isinstance(head_sha, str) or REVISION_RE.fullmatch(head_sha) is None
        or not isinstance(default_branch, str) or not default_branch.strip()
        or default_branch != default_branch.strip()
        or mode not in MODE_STEPS
    ):
        _fail("unavailable", "expected_invocation_incomplete")
    return {
        "repository": context["repository"],
        "workflow_path": WORKFLOW_PATH,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "event": event,
        "head_sha": head_sha,
        "mode": mode,
    }


def _validate_native_run(
    run: Mapping[str, Any], invocation: Mapping[str, Any],
    context: Mapping[str, Any], contract: Mapping[str, Any],
    as_of: dt.datetime,
) -> dt.datetime:
    if not isinstance(run, Mapping):
        _fail("unavailable", "exact_attempt_run_unavailable")
    if (
        _id_text(run.get("id")) != invocation["run_id"]
        or run.get("run_attempt") != invocation["run_attempt"]
        or not _is_int(run.get("run_attempt"), minimum=1)
        or run.get("workflow_id") != contract.get("workflow_id")
        or run.get("path") != WORKFLOW_PATH
        or run.get("event") != invocation["event"]
        or run.get("head_sha") != invocation["head_sha"]
        or run.get("head_branch") != context.get("default_branch")
        or run.get("status") != "completed"
        or run.get("conclusion") not in {
            "success", "failure", "cancelled", "timed_out",
            "action_required", "neutral", "skipped",
        }
    ):
        _fail("rejected", "exact_attempt_run_identity_mismatch")
    repository = run.get("repository")
    head_repository = run.get("head_repository")
    if (
        not isinstance(repository, Mapping)
        or not isinstance(repository.get("full_name"), str)
        or repository["full_name"].casefold() != invocation["repository"].casefold()
        or not isinstance(head_repository, Mapping)
        or not isinstance(head_repository.get("full_name"), str)
        or head_repository["full_name"].casefold() != invocation["repository"].casefold()
        or _id_text(repository.get("id")) in {None, "0"}
        or _id_text(head_repository.get("id")) != _id_text(repository.get("id"))
    ):
        _fail("rejected", "exact_attempt_repository_mismatch")
    if context.get("repository_id") is not None and repository.get("id") != context.get("repository_id"):
        _fail("rejected", "exact_attempt_repository_id_mismatch")
    created = _parse_time(run.get("created_at"), "exact_attempt_created_at_invalid")
    started = _parse_time(run.get("run_started_at"), "exact_attempt_started_at_invalid")
    if created > started or started > as_of:
        _fail("rejected", "exact_attempt_time_order_invalid")
    return started


def _validate_jobs(
    jobs_value: Mapping[str, Any], invocation: Mapping[str, Any],
    run: Mapping[str, Any], contract: Mapping[str, Any], as_of: dt.datetime,
) -> tuple[dt.datetime, dt.datetime, str, dt.datetime, dt.datetime]:
    if not isinstance(jobs_value, Mapping):
        _fail("unavailable", "exact_attempt_jobs_unavailable")
    run_attempt = invocation["run_attempt"]
    endpoint = f"repos/{invocation['repository']}/actions/runs/{invocation['run_id']}/attempts/{run_attempt}/jobs"
    rows = jobs_value.get("jobs")
    if (
        jobs_value.get("attempt_number") != run_attempt
        or jobs_value.get("jobs_api_endpoint") != endpoint
        or not _is_int(jobs_value.get("job_count"), minimum=1)
        or not isinstance(rows, list)
        or jobs_value.get("job_count") != len(rows)
        or len(rows) > 500
    ):
        _fail("unavailable", "exact_attempt_jobs_incomplete")
    ids: set[str] = set()
    target_jobs: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            _fail("rejected", "exact_attempt_job_invalid")
        job_id = _id_text(row.get("id"))
        if job_id is None or job_id == "0" or job_id in ids:
            _fail("rejected", "exact_attempt_job_id_invalid")
        ids.add(job_id)
        if (
            _id_text(row.get("run_id")) != invocation["run_id"]
            or (row.get("run_attempt") is not None and row.get("run_attempt") != run_attempt)
            or row.get("head_sha") != invocation["head_sha"]
            or row.get("head_branch") != run.get("head_branch")
        ):
            _fail("rejected", "exact_attempt_job_identity_mismatch")
        if row.get("name") == "reconcile":
            target_jobs.append(row)
    if len(target_jobs) != 1:
        _fail("rejected", "reconcile_job_not_unique")
    job = target_jobs[0]
    if job.get("status") != "completed" or job.get("conclusion") not in {
        "success", "failure", "cancelled", "timed_out",
        "action_required", "neutral", "skipped",
    }:
        _fail("unavailable", "reconcile_job_not_completed")
    job_started = _parse_time(job.get("started_at"), "reconcile_job_started_at_invalid")
    job_completed = _parse_time(job.get("completed_at"), "reconcile_job_completed_at_invalid")
    run_started = _parse_time(run.get("run_started_at"), "exact_attempt_started_at_invalid")
    if job_started < run_started or job_completed < job_started or job_completed > as_of:
        _fail("rejected", "reconcile_job_time_order_invalid")
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        _fail("unavailable", "reconcile_job_steps_missing")
    step_numbers: set[int] = set()
    step_names = contract.get("mode_steps", {}).get(invocation["mode"])
    if not isinstance(step_names, Mapping):
        _fail("unavailable", "mode_step_contract_missing")
    selected_names = {
        step_names["invocation_step"]: "invocation",
        step_names["upload_step"]: "upload",
    }
    selected: dict[str, Mapping[str, Any]] = {}
    for step in steps:
        if not isinstance(step, Mapping):
            _fail("rejected", "reconcile_step_invalid")
        number = step.get("number")
        if not _is_int(number, minimum=1) or number in step_numbers:
            _fail("rejected", "reconcile_step_number_invalid")
        step_numbers.add(number)
        role = selected_names.get(step.get("name"))
        if role is not None:
            if role in selected:
                _fail("rejected", "reconcile_mode_step_not_unique")
            selected[role] = step
    if set(selected) != {"invocation", "upload"}:
        _fail("unavailable", "reconcile_mode_steps_missing")
    invocation_step = selected["invocation"]
    upload_step = selected["upload"]
    if invocation_step.get("number") >= upload_step.get("number"):
        _fail("rejected", "terminal_upload_precedes_invocation")
    invocation_started = _parse_time(invocation_step.get("started_at"), "invocation_step_started_at_invalid")
    invocation_completed = _parse_time(invocation_step.get("completed_at"), "invocation_step_completed_at_invalid")
    upload_started = _parse_time(upload_step.get("started_at"), "upload_step_started_at_invalid")
    upload_completed = _parse_time(upload_step.get("completed_at"), "upload_step_completed_at_invalid")
    if (
        invocation_step.get("status") != "completed"
        or invocation_completed < invocation_started
        or invocation_started < job_started
        or invocation_completed > job_completed
        or upload_step.get("status") != "completed"
        or upload_step.get("conclusion") != "success"
        or upload_completed < upload_started
        or upload_started < invocation_completed
        or upload_completed > job_completed
        or max(invocation_completed, upload_completed) > as_of
    ):
        _fail("rejected", "terminal_mode_step_timing_invalid")
    conclusion = invocation_step.get("conclusion")
    if conclusion not in {"success", "failure"}:
        _fail("unavailable", "terminal_mode_step_incomplete")
    if conclusion == "failure" and job.get("conclusion") == "success":
        _fail("rejected", "terminal_job_conclusion_inconsistent")
    return invocation_started, invocation_completed, conclusion, upload_started, upload_completed


def _validate_inventory(
    inventory: Mapping[str, Any], invocation: Mapping[str, Any],
    as_of: dt.datetime,
) -> tuple[dict[str, Any], dt.datetime, dt.datetime]:
    if not isinstance(inventory, Mapping):
        _fail("unavailable", "artifact_inventory_unavailable")
    rows = inventory.get("artifacts")
    total = inventory.get("total_count")
    if (
        _id_text(inventory.get("run_id")) != invocation["run_id"]
        or not _is_int(total, minimum=0) or total > MAX_ARTIFACT_ROWS
        or not isinstance(rows, list) or total != len(rows)
    ):
        _fail("unavailable", "artifact_inventory_incomplete")
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            _fail("rejected", "artifact_inventory_row_invalid")
        ident = _id_text(row.get("id"))
        if ident is None or ident == "0" or ident in ids:
            _fail("rejected", "artifact_inventory_id_duplicate_or_invalid")
        ids.add(ident)
    expected_name = (
        f"canonical-update-promotion-terminal-{invocation['run_id']}-"
        f"{invocation['run_attempt']}-{invocation['mode']}"
    )
    matches = [row for row in rows if row.get("name") == expected_name]
    if not matches:
        _fail("unavailable", "terminal_artifact_absent")
    if len(matches) != 1:
        _fail("rejected", "terminal_artifact_name_not_unique")
    listed = matches[0]
    ident = _id_text(listed.get("id"))
    details = inventory.get("details_by_id")
    if not isinstance(details, Mapping) or ident not in details or not isinstance(details[ident], Mapping):
        _fail("unavailable", "terminal_artifact_readback_missing")
    detail = details[ident]
    for key in ("id", "name", "expired", "created_at", "expires_at", "size_in_bytes"):
        if listed.get(key) != detail.get(key):
            _fail("rejected", "terminal_artifact_list_detail_mismatch")
    if listed.get("digest") is not None and listed.get("digest") != detail.get("digest"):
        _fail("rejected", "terminal_artifact_list_detail_mismatch")
    listed_run = listed.get("workflow_run")
    detail_run = detail.get("workflow_run")
    if not isinstance(listed_run, Mapping) or not isinstance(detail_run, Mapping):
        _fail("unavailable", "terminal_artifact_run_identity_missing")
    for key in ("id", "head_sha", "head_branch", "repository_id", "head_repository_id"):
        if listed_run.get(key) != detail_run.get(key):
            _fail("rejected", "terminal_artifact_list_detail_mismatch")
    created = _parse_time(detail.get("created_at"), "terminal_artifact_created_at_invalid")
    expires = _parse_time(detail.get("expires_at"), "terminal_artifact_expiry_invalid")
    if detail.get("expired") is not False or created > as_of or expires <= as_of or expires <= created:
        _fail("unavailable", "terminal_artifact_expired_or_time_invalid")
    if _id_text(detail_run.get("id")) != invocation["run_id"] or detail_run.get("head_sha") != invocation["head_sha"]:
        _fail("rejected", "terminal_artifact_native_run_mismatch")
    if not isinstance(detail.get("digest"), str) or re.fullmatch(r"sha256:[a-f0-9]{64}", detail["digest"]) is None:
        _fail("unavailable", "terminal_artifact_digest_unavailable")
    if not _is_int(detail.get("size_in_bytes"), minimum=1) or detail["size_in_bytes"] > MAX_ARCHIVE_BYTES:
        _fail("unavailable", "terminal_artifact_size_invalid")
    return dict(detail), created, expires


def _read_archive(archive_bytes: Any) -> tuple[bytes, str, int]:
    if not isinstance(archive_bytes, bytes) or not archive_bytes or len(archive_bytes) > MAX_ARCHIVE_BYTES:
        _fail("unavailable", "terminal_archive_size_invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            expected = {"terminal-outcome.json", "terminal-outcome.sha256"}
            if len(infos) != 2 or len(names) != len(set(names)) or set(names) != expected:
                _fail("rejected", "terminal_archive_members_invalid")
            expanded = 0
            members: dict[str, bytes] = {}
            for info in infos:
                path = pathlib.PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (
                    path.is_absolute() or len(path.parts) != 1 or ".." in path.parts
                    or "\\" in info.filename or info.is_dir()
                    or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                    or info.flag_bits & 0x1
                    or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                ):
                    _fail("rejected", "terminal_archive_member_unsafe")
                limit = MAX_JSON_BYTES if info.filename == "terminal-outcome.json" else MAX_SHA_BYTES
                if info.file_size < 1 or info.file_size > limit or info.compress_size < 1:
                    _fail("rejected", "terminal_archive_member_size_invalid")
                expanded += info.file_size
                if expanded > MAX_EXPANDED_BYTES:
                    _fail("rejected", "terminal_archive_expansion_limit")
                with archive.open(info, "r") as stream:
                    content = stream.read(limit + 1)
                if len(content) != info.file_size or len(content) > limit:
                    _fail("rejected", "terminal_archive_member_truncated")
                members[info.filename] = content
            document_bytes = members["terminal-outcome.json"]
            seal_bytes = members["terminal-outcome.sha256"]
    except _EvidenceProblem:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, EOFError, ValueError):
        _fail("rejected", "terminal_archive_invalid")
    seal = SHA_LINE_RE.fullmatch(seal_bytes)
    if seal is None or hashlib.sha256(document_bytes).hexdigest().encode("ascii") != seal.group(1):
        _fail("rejected", "terminal_archive_seal_mismatch")
    return document_bytes, hashlib.sha256(archive_bytes).hexdigest(), len(archive_bytes)


def _validate_generation(row: Mapping[str, Any], evaluated_main: Mapping[str, Any] | None) -> None:
    status = row.get("status")
    reason = row.get("reason_code")
    if status not in {"blocked", "already_canonical", "selected", "already_represented"}:
        _fail("rejected", "terminal_generation_status_invalid")
    if status == "blocked":
        if not isinstance(reason, str):
            _fail("rejected", "terminal_blocked_reason_missing")
        return
    if status == "selected" and reason is not None:
        _fail("rejected", "terminal_selected_reason_invalid")
    if status in {"already_canonical", "already_represented"} and not isinstance(reason, str):
        _fail("rejected", "terminal_screened_reason_missing")
    producer = row.get("producer")
    composition = row.get("composition")
    if not isinstance(producer, Mapping) or not isinstance(composition, Mapping):
        _fail("rejected", "terminal_screened_identity_missing")
    generation_id = row.get("generation_id")
    checkpoint_sha = row.get("checkpoint_sha256")
    run_id = producer.get("run_id")
    attempt = producer.get("run_attempt")
    if (
        not isinstance(generation_id, str) or DIGEST_RE.fullmatch(generation_id) is None
        or not isinstance(checkpoint_sha, str) or DIGEST_RE.fullmatch(checkpoint_sha) is None
        or not isinstance(row.get("source_id"), str) or not row["source_id"]
        or not isinstance(row.get("source_scope"), str) or not row["source_scope"]
        or not isinstance(run_id, str) or re.fullmatch(r"[0-9]{6,20}", run_id) is None
        or not _is_int(attempt, minimum=1)
        or producer.get("artifact_name") != f"upstream-catalogue-processing-{run_id}-{attempt}"
        or not isinstance(producer.get("head_sha"), str) or REVISION_RE.fullmatch(producer["head_sha"]) is None
        or _id_text(producer.get("artifact_id")) in {None, "0"}
        or not isinstance(producer.get("bundle_manifest_sha256"), str)
        or DIGEST_RE.fullmatch(producer["bundle_manifest_sha256"]) is None
        or not isinstance(composition.get("registry_path"), str)
        or not _is_int(composition.get("registry_bytes"), minimum=1)
        or not isinstance(composition.get("registry_sha256"), str)
        or DIGEST_RE.fullmatch(composition["registry_sha256"]) is None
        or not isinstance(composition.get("composition_receipt_sha256"), str)
        or DIGEST_RE.fullmatch(composition["composition_receipt_sha256"]) is None
    ):
        _fail("rejected", "terminal_screened_identity_invalid")
    _parse_time(producer.get("artifact_expires_at"), "terminal_screened_artifact_expiry_invalid")
    observation = row.get("checkpoint_observation")
    if (
        not isinstance(observation, Mapping)
        or observation.get("claim") != "checkpoint_reported_last_observation"
        or not isinstance(observation.get("producer_run_id"), str)
        or not observation.get("producer_run_id")
        or observation.get("collection_status") not in {"success", "failure", "missing"}
        or observation.get("execution_mode") not in {"live", "fixture"}
        or not _is_int(row.get("checkpoint_observation_count"), minimum=1)
        or not isinstance(row.get("generation_baseline_sha256"), str)
        or DIGEST_RE.fullmatch(row["generation_baseline_sha256"]) is None
        or (
            observation.get("refresh_evidence_sha256") is not None
            and (
                not isinstance(observation.get("refresh_evidence_sha256"), str)
                or DIGEST_RE.fullmatch(observation["refresh_evidence_sha256"]) is None
            )
        )
    ):
        _fail("rejected", "terminal_checkpoint_observation_invalid")
    _parse_time(observation.get("observed_at"), "terminal_checkpoint_observation_time_invalid")
    if status == "already_canonical":
        if reason != "already_canonical_payload" or not isinstance(evaluated_main, Mapping):
            _fail("rejected", "terminal_already_canonical_claim_invalid")
        for key in ("registry_path", "registry_bytes", "registry_sha256"):
            if composition.get(key) != evaluated_main.get(key):
                _fail("rejected", "terminal_canonical_payload_mismatch")


def _validate_outcome(document: Mapping[str, Any], mode: str) -> None:
    if document.get("execution_status") != "completed":
        return
    outcome = document.get("outcome")
    if not isinstance(outcome, Mapping) or outcome.get("kind") != mode:
        _fail("rejected", "terminal_outcome_mode_mismatch")
    if mode != "recover-ready":
        if (
            outcome.get("status") != "mode_completed_without_recovery_outcome"
            or outcome.get("candidate_available") is not False
            or outcome.get("evaluated_main") is not None
            or outcome.get("selected_generation_id") is not None
            or outcome.get("preparation_returned") is not False
            or outcome.get("generation_count") != 0
            or outcome.get("generation_results_truncated") is not False
            or outcome.get("generations") != []
        ):
            _fail("rejected", "terminal_nonrecovery_outcome_invalid")
        return
    status = outcome.get("status")
    rows = outcome.get("generations")
    count = outcome.get("generation_count")
    truncated = outcome.get("generation_results_truncated")
    main = outcome.get("evaluated_main")
    selected = outcome.get("selected_generation_id")
    if (
        status not in {
            "no-eligible-ready-processor-bundle",
            "no-undelivered-ready-processor-bundle",
            "already-canonical-payload",
            "candidate_preparation_returned",
        }
        or not isinstance(outcome.get("candidate_available"), bool)
        or not isinstance(outcome.get("preparation_returned"), bool)
        or not _is_int(count, minimum=0)
        or not isinstance(truncated, bool)
        or not isinstance(rows, list)
        or len(rows) > MAX_GENERATIONS
        or (not truncated and count != len(rows))
        or (truncated and (count <= len(rows) or len(rows) != MAX_GENERATIONS))
    ):
        _fail("rejected", "terminal_recovery_outcome_invalid")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            _fail("rejected", "terminal_generation_row_invalid")
        generation_id = row.get("generation_id")
        if generation_id is not None:
            if not isinstance(generation_id, str) or DIGEST_RE.fullmatch(generation_id) is None or generation_id in seen:
                _fail("rejected", "terminal_generation_id_duplicate_or_invalid")
            seen.add(generation_id)
        _validate_generation(row, main)
    if status == "candidate_preparation_returned":
        selected_rows = [row for row in rows if row.get("status") == "selected"]
        selected_matches = [row for row in selected_rows if row.get("generation_id") == selected]
        if (
            outcome.get("candidate_available") is not True
            or outcome.get("preparation_returned") is not True
            or not isinstance(selected, str) or DIGEST_RE.fullmatch(selected) is None
            or len(selected_rows) > 1
            or any(row.get("generation_id") != selected for row in selected_rows)
            or (truncated and any(row.get("generation_id") == selected for row in rows))
            or (not truncated and len(selected_matches) != 1)
        ):
            _fail("rejected", "terminal_selected_generation_mismatch")
    elif status == "already-canonical-payload":
        if (
            outcome.get("candidate_available") is not False
            or outcome.get("preparation_returned") is not False
            or selected is not None
            or not isinstance(main, Mapping)
            or truncated or not rows
            or any(row.get("status") != "already_canonical" for row in rows)
        ):
            _fail("rejected", "terminal_all_canonical_claim_invalid")
    else:
        if (
            outcome.get("candidate_available") is not False
            or outcome.get("preparation_returned") is not False
            or selected is not None
        ):
            _fail("rejected", "terminal_no_candidate_claim_invalid")
        if status == "no-undelivered-ready-processor-bundle" and (count != 0 or rows or truncated):
            _fail("rejected", "terminal_empty_recovery_claim_invalid")
        if status == "no-undelivered-ready-processor-bundle" and main is not None:
            _fail("rejected", "terminal_empty_recovery_claim_invalid")
        if status == "no-eligible-ready-processor-bundle":
            if not rows:
                _fail("rejected", "terminal_no_eligible_rows_or_main_missing")
            if any(row.get("status") == "selected" for row in rows):
                _fail("rejected", "terminal_no_candidate_selected_row")
            if main is None:
                # An early no-eligible return can precede canonical-main
                # evaluation. Keep its blocked rows as historical evidence,
                # without asserting a current canonical comparison.
                if any(row.get("status") != "blocked" for row in rows):
                    _fail("rejected", "terminal_no_eligible_rows_without_main_invalid")
            elif not isinstance(main, Mapping):
                _fail("rejected", "terminal_no_eligible_rows_or_main_missing")


def _current_applicability(outcome: Any, current_subject: Any) -> dict[str, Any]:
    if not isinstance(outcome, Mapping) or outcome.get("kind") != "recover-ready":
        return {"status": "not_applicable", "reason_code": "no_recovery_outcome", "matching_generations": []}
    if outcome.get("generation_results_truncated") is True:
        return {
            "status": "historical_only",
            "reason_code": "terminal_generation_results_truncated",
            "matching_generations": [],
        }
    if current_subject is None:
        return {"status": "not_checked", "reason_code": "current_subject_not_supplied", "matching_generations": []}
    if not isinstance(current_subject, Mapping):
        return {"status": "unavailable", "reason_code": "current_subject_invalid", "matching_generations": []}
    current_main = current_subject.get("main_identity")
    current_generations = current_subject.get("generations")
    if not isinstance(current_main, Mapping) or not isinstance(current_generations, list):
        return {"status": "unavailable", "reason_code": "current_subject_incomplete", "matching_generations": []}
    evaluated = outcome.get("evaluated_main")
    if not isinstance(evaluated, Mapping):
        return {"status": "historical_only", "reason_code": "historical_record_has_no_evaluated_main", "matching_generations": []}
    main_fields = ("revision", "manifest_sha256", "registry_path", "registry_bytes", "registry_sha256")
    main_matches = all(evaluated.get(key) == current_main.get(key) for key in main_fields)
    matches: list[str] = []
    for row in outcome.get("generations", []):
        if not isinstance(row, Mapping) or row.get("status") != "already_canonical":
            continue
        generation_id = row.get("generation_id")
        candidates = [
            candidate for candidate in current_generations
            if isinstance(candidate, Mapping) and candidate.get("generation_id") == generation_id
        ]
        if len(candidates) != 1:
            continue
        candidate = candidates[0]
        exact_fields = ("generation_id", "checkpoint_sha256", "source_id", "source_scope", "producer", "composition")
        if all(candidate.get(key) == row.get(key) for key in exact_fields):
            comp = candidate.get("composition")
            if isinstance(comp, Mapping) and all(comp.get(key) == current_main.get(key) for key in ("registry_path", "registry_bytes", "registry_sha256")):
                matches.append(str(generation_id))
    if main_matches and matches:
        return {"status": "current", "reason_code": None, "matching_generations": sorted(matches)}
    return {"status": "historical_only", "reason_code": "current_candidate_identity_mismatch", "matching_generations": []}


def _empty_result(status: str, code: str, invocation: Any, artifact: Any, run: Any) -> dict[str, Any]:
    return {
        "status": status,
        "reason_code": code,
        "invocation": dict(invocation) if isinstance(invocation, Mapping) else None,
        "artifact": artifact,
        "execution_status": None,
        "started_at": None,
        "completed_at": None,
        "failure_code": None,
        "outcome": None,
        "run_status": run.get("status") if isinstance(run, Mapping) else None,
        "run_conclusion": run.get("conclusion") if isinstance(run, Mapping) else None,
        "mode_step_conclusion": None,
        "current_applicability": {"status": "not_checked", "reason_code": code, "matching_generations": []},
    }


def validate_terminal_evidence(
    *, archive_bytes: bytes, run: Mapping[str, Any], exact_attempt: int,
    jobs: Mapping[str, Any], artifact_inventory: Mapping[str, Any],
    expected_context: Mapping[str, Any], source_contract: Mapping[str, Any],
    as_of: dt.datetime | str, current_subject: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one receipt against immutable native API and source snapshots.

    source_contract must be assembled by a caller that authenticated the
    evaluator source tree at the native C source SHA. It includes hash-bound
    schema bytes and a path-to-digest map for at least the workflow, runner,
    and schema. GitHub run, jobs, and complete paginated artifact inventory
    snapshots must be fetched for the exact attempt; the caller must reread
    the attempt before and after intake to detect reruns. This function never
    trusts a receipt's own evaluator claim as the source of expected identity.
    """
    invocation: dict[str, Any] | None = None
    artifact_identity: dict[str, Any] | None = None
    try:
        if not isinstance(expected_context, Mapping) or not isinstance(source_contract, Mapping):
            _fail("unavailable", "source_contract_unavailable")
        as_of_dt = _as_of_time(as_of)
        invocation = _normalize_invocation(expected_context)
        if not _is_int(exact_attempt, minimum=1) or exact_attempt != invocation["run_attempt"]:
            _fail("rejected", "exact_attempt_argument_mismatch")
        schema, mode = _validate_source_contract(expected_context, source_contract)
        run_started = _validate_native_run(run, invocation, expected_context, source_contract, as_of_dt)
        invocation_started, invocation_completed, step_conclusion, upload_started, upload_completed = _validate_jobs(
            jobs, invocation, run, source_contract, as_of_dt,
        )
        artifact, artifact_created, _expires = _validate_inventory(
            artifact_inventory, invocation, as_of_dt,
        )
        artifact_identity = {
            "artifact_id": str(artifact["id"]),
            "name": artifact["name"],
            "expires_at": artifact["expires_at"],
        }
        workflow_run = artifact.get("workflow_run")
        repo = run.get("repository")
        head_repo = run.get("head_repository")
        if (
            not isinstance(workflow_run, Mapping)
            or not isinstance(repo, Mapping)
            or not isinstance(head_repo, Mapping)
            or _id_text(workflow_run.get("repository_id")) != _id_text(repo.get("id"))
            or _id_text(workflow_run.get("head_repository_id")) != _id_text(head_repo.get("id"))
            or workflow_run.get("head_branch") != run.get("head_branch")
            or artifact_created < upload_started
            or artifact_created > upload_completed
        ):
            _fail("rejected", "terminal_artifact_repository_or_time_binding_mismatch")
        if artifact_created < run_started or artifact_created > as_of_dt:
            _fail("rejected", "terminal_artifact_creation_time_invalid")
        document_bytes, archive_sha, archive_size = _read_archive(archive_bytes)
        if len(archive_bytes) != artifact.get("size_in_bytes"):
            _fail("rejected", "terminal_artifact_archive_size_mismatch")
        if hashlib.sha256(archive_bytes).hexdigest() != artifact["digest"].removeprefix("sha256:"):
            _fail("rejected", "terminal_artifact_archive_digest_mismatch")
        try:
            document = json.loads(
                document_bytes.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _fail("rejected", "terminal_document_json_invalid")
        if not isinstance(document, Mapping) or document_bytes != _canonical_json(document) + b"\n":
            _fail("rejected", "terminal_document_not_canonical_json")
        try:
            import jsonschema

            _offline_schema_validator(schema, jsonschema).validate(document)
        except ImportError:
            _fail("unavailable", "jsonschema_dependency_unavailable")
        except Exception:
            _fail("rejected", "terminal_document_schema_invalid")
        if not isinstance(document, Mapping):
            _fail("rejected", "terminal_document_invalid")
        doc_invocation = document.get("invocation")
        evaluator = document.get("evaluator")
        if (
            not isinstance(document.get("repository"), str)
            or document["repository"].casefold() != invocation["repository"].casefold()
            or document.get("workflow_path") != WORKFLOW_PATH
            or not isinstance(doc_invocation, Mapping)
            or doc_invocation.get("mode") != mode
            or doc_invocation.get("run_id") != invocation["run_id"]
            or doc_invocation.get("run_attempt") != invocation["run_attempt"]
            or doc_invocation.get("event") != invocation["event"]
            or not isinstance(evaluator, Mapping)
            or evaluator.get("source_sha") != source_contract.get("source_sha")
            or evaluator.get("workflow_checkout_sha") != source_contract.get("source_sha")
            or evaluator.get("checkout_matches_source") is not True
            or evaluator.get("loaded_files_match_source") is not True
        ):
            _fail("rejected", "terminal_document_source_or_invocation_mismatch")
        execution_status = document["execution_status"]
        if execution_status == "started":
            _fail("unavailable", "terminal_execution_not_complete")
        if execution_status == "completed" and step_conclusion != "success":
            _fail("rejected", "terminal_execution_step_conclusion_mismatch")
        if execution_status == "failed" and step_conclusion != "failure":
            _fail("rejected", "terminal_execution_step_conclusion_mismatch")
        started = _parse_time(document.get("started_at"), "terminal_started_at_invalid")
        if started < invocation_started or started > invocation_completed:
            _fail("rejected", "terminal_execution_time_outside_invocation")
        completed_raw = document.get("completed_at")
        if completed_raw is None:
            _fail("unavailable", "terminal_execution_not_complete")
        completed = _parse_time(completed_raw, "terminal_completed_at_invalid")
        if completed < started or completed > invocation_completed or completed > as_of_dt:
            _fail("rejected", "terminal_execution_time_order_invalid")
        outcome = document.get("outcome")
        _validate_outcome(document, mode)
        return {
            "status": "verified",
            "reason_code": None,
            "invocation": dict(invocation),
            "artifact": {**artifact_identity, "sha256": archive_sha, "bytes": archive_size},
            "execution_status": execution_status,
            "started_at": document.get("started_at"),
            "completed_at": completed_raw,
            "failure_code": document.get("failure_code"),
            "outcome": dict(outcome) if isinstance(outcome, Mapping) else None,
            "run_status": run.get("status"),
            "run_conclusion": run.get("conclusion"),
            "mode_step_conclusion": step_conclusion,
            "current_applicability": _current_applicability(outcome, current_subject),
        }
    except _EvidenceProblem as problem:
        return _empty_result(problem.status, problem.code, invocation, artifact_identity, run)
    except Exception:
        # Do not leak API text, paths, or untrusted parser exceptions.
        return _empty_result("rejected", "terminal_evidence_invalid", invocation, artifact_identity, run)
