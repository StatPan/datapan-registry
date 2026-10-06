"""Validate the retained publication/anonymous-readback/ACK historical facet.

This module is deliberately offline.  It consumes objects and bytes already
admitted by the completeness input index, rechecks their cross-identities, and
returns historical evidence only.  It does not publish, acknowledge, write the
promotion journal, or make a currentness or release-readiness claim.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import re
import subprocess
import zipfile
from collections.abc import Mapping
from io import BytesIO
from typing import Any


REPOSITORY = "StatPan/datapan-registry"
REGISTRY_PATH = "data/data-go-kr.registry.json"
MANIFEST_PATH = "manifest.json"
PUBLISHER_WORKFLOW_PATH = ".github/workflows/huggingface-distribution.yml"
ACK_WORKFLOW_PATH = ".github/workflows/canonical-update-publication-ack.yml"
PUBLISHER_RUN_ID = 37199628001
PUBLISHER_ATTEMPT = 1
PUBLISHER_WORKFLOW_ID = 311133646
PUBLISHER_ARTIFACT_ID = 11301727720
PUBLISHER_ARTIFACT_NAME = "huggingface-registry-publication-receipts"
ACK_RUN_ID = 37199709258
ACK_ATTEMPT = 2
ACK_WORKFLOW_ID = 373708873
TARGET_PR = 686
EXPECTED_GENERATION = "66a2ae130fce7463dfbdd47caa48d3b7013e47a365a9a2745fe22b5378cd77d4"
MAX_LOG_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_LOG_MEMBER_BYTES = 512 * 1024
MAX_LOG_MEMBERS = 64
MAX_JSON_BYTES = 2 * 1024 * 1024
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LFS_VERSION = "version https://git-lfs.github.com/spec/v1"


REQUIRED_INPUTS = frozenset({
    "publisher_run",
    "publisher_jobs",
    "publisher_artifact_metadata",
    "publisher_archive",
    "publication_receipt",
    "source_binding",
    "source_commit",
    "source_manifest",
    "publisher_workflow",
    "pointer_before",
    "pointer_after",
    "pointer_immutable",
    "anonymous_manifest",
    "anonymous_payload",
    "source_catalog_snapshot",
    "ack_run",
    "ack_jobs",
    "ack_log",
    "journal_before",
    "journal_after",
})


class PublicationEvidenceError(ValueError):
    """A malformed, conflicting, or unbound publication evidence input."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PublicationEvidenceError(f"{label}_must_be_object")
    return value


def _json_bytes(value: Any, label: str) -> bytes:
    if not isinstance(value, bytes) or not value or len(value) > MAX_JSON_BYTES:
        raise PublicationEvidenceError(f"{label}_bytes_invalid")
    return value


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PublicationEvidenceError("duplicate_json_key")
        result[key] = value
    return result


def _parse_json(value: Any, label: str) -> Any:
    raw = _json_bytes(value, label)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationEvidenceError(f"{label}_invalid_json") from exc


def _sha(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value) or value == "0" * len(value):
        raise PublicationEvidenceError(f"{label}_invalid")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PublicationEvidenceError(f"{label}_invalid")
    return value


def _parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise PublicationEvidenceError(f"{label}_missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationEvidenceError(f"{label}_invalid") from exc
    if parsed.tzinfo is None:
        raise PublicationEvidenceError(f"{label}_timezone_missing")
    return parsed.astimezone(dt.timezone.utc)


def _time_text(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _load_ack_validator(repo_root: pathlib.Path):
    path = repo_root / "scripts" / "recover-canonical-publication-ack.py"
    if not path.is_file():
        raise PublicationEvidenceError("ack_validator_unavailable")
    name = "_completeness_publication_ack_validator"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PublicationEvidenceError("ack_validator_unavailable")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the class module during import.
    import sys
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise PublicationEvidenceError("ack_validator_load_failed") from exc
    return module


def _artifact_from_metadata(value: Any) -> Mapping[str, Any]:
    metadata = _mapping(value, "publisher_artifact_metadata")
    if isinstance(metadata.get("artifacts"), list):
        rows = metadata["artifacts"]
        total = metadata.get("total_count")
        if isinstance(total, bool) or not isinstance(total, int) or total != len(rows):
            raise PublicationEvidenceError("publisher_artifact_list_incomplete")
        selected = [row for row in rows if isinstance(row, Mapping) and row.get("id") == PUBLISHER_ARTIFACT_ID]
        if len(selected) != 1:
            raise PublicationEvidenceError("publisher_receipt_artifact_ambiguous")
        return selected[0]
    if metadata.get("id") != PUBLISHER_ARTIFACT_ID:
        raise PublicationEvidenceError("publisher_receipt_artifact_id_mismatch")
    return metadata


def _parse_lfs_pointer_patch(source_commit: Mapping[str, Any]) -> tuple[str, int]:
    files = source_commit.get("files")
    if not isinstance(files, list):
        raise PublicationEvidenceError("source_commit_files_missing")
    matches = [item for item in files if isinstance(item, Mapping) and item.get("filename") == REGISTRY_PATH]
    if len(matches) != 1:
        raise PublicationEvidenceError("source_registry_pointer_change_missing")
    file_row = matches[0]
    patch = file_row.get("patch")
    if not isinstance(patch, str):
        raise PublicationEvidenceError("source_registry_pointer_patch_missing")
    added_oids = re.findall(r"^\+oid sha256:([0-9a-f]{64})$", patch, flags=re.MULTILINE)
    added_sizes = re.findall(r"^\+size ([0-9]+)$", patch, flags=re.MULTILINE)
    if len(added_oids) != 1 or len(added_sizes) != 1:
        raise PublicationEvidenceError("source_registry_pointer_patch_ambiguous")
    try:
        size = int(added_sizes[0])
    except ValueError as exc:
        raise PublicationEvidenceError("source_registry_pointer_size_invalid") from exc
    if size < 1:
        raise PublicationEvidenceError("source_registry_pointer_size_invalid")
    oid = added_oids[0]
    pointer = f"{LFS_VERSION}\noid sha256:{oid}\nsize {size}\n".encode("ascii")
    blob = b"blob " + str(len(pointer)).encode("ascii") + b"\0" + pointer
    expected_git_blob_sha = hashlib.sha1(blob).hexdigest()
    if file_row.get("sha") != expected_git_blob_sha:
        raise PublicationEvidenceError("source_registry_pointer_blob_mismatch")
    return oid, size


def _artifact_row(manifest: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    rows = manifest.get("artifacts")
    if not isinstance(rows, list):
        raise PublicationEvidenceError(f"{label}_artifacts_missing")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("path") == REGISTRY_PATH]
    if len(matches) != 1:
        raise PublicationEvidenceError(f"{label}_registry_artifact_ambiguous")
    return matches[0]


def _pointer_bundle(value: Any, label: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    bundle = _mapping(value, label)
    metadata = bundle.get("repo_metadata", bundle.get("metadata"))
    index = bundle.get("distribution_index", bundle.get("index"))
    if not isinstance(metadata, Mapping) or not isinstance(index, Mapping):
        raise PublicationEvidenceError(f"{label}_shape_invalid")
    return metadata, index


def _check_distribution_index(
    index: Mapping[str, Any],
    *,
    payload_revision: str,
    pointer_revision: str,
    manifest_sha256: str,
    manifest_bytes: int,
    registry_sha256: str,
    registry_bytes: int,
    artifact_count: int,
    label: str,
) -> None:
    if index.get("schema_version") != "datapan.huggingface-distribution.v1":
        raise PublicationEvidenceError(f"{label}_schema_invalid")
    dataset = index.get("dataset")
    if not isinstance(dataset, Mapping) or dataset.get("id") != REPOSITORY or dataset.get("revision") != payload_revision:
        raise PublicationEvidenceError(f"{label}_dataset_revision_mismatch")
    release_manifest = index.get("release_manifest")
    if (
        not isinstance(release_manifest, Mapping)
        or release_manifest.get("path") != MANIFEST_PATH
        or release_manifest.get("bytes") != manifest_bytes
        or release_manifest.get("sha256") != manifest_sha256
    ):
        raise PublicationEvidenceError(f"{label}_manifest_mismatch")
    if index.get("artifact_count") != artifact_count:
        raise PublicationEvidenceError(f"{label}_artifact_count_mismatch")
    rows = index.get("artifacts")
    if not isinstance(rows, list) or len(rows) != artifact_count:
        raise PublicationEvidenceError(f"{label}_artifact_listing_incomplete")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("path") == REGISTRY_PATH]
    if len(matches) != 1:
        raise PublicationEvidenceError(f"{label}_registry_artifact_ambiguous")
    row = matches[0]
    if row.get("kind") != "registry" or row.get("bytes") != registry_bytes or row.get("sha256") != registry_sha256:
        raise PublicationEvidenceError(f"{label}_registry_artifact_mismatch")
    if pointer_revision and not SHA1_RE.fullmatch(pointer_revision):
        raise PublicationEvidenceError(f"{label}_pointer_revision_invalid")


def _extract_ack_log(raw: bytes) -> tuple[str, list[tuple[dt.datetime, Mapping[str, Any]]]]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_LOG_ARCHIVE_BYTES:
        raise PublicationEvidenceError("acknowledgement_log_archive_size_invalid")
    try:
        archive = zipfile.ZipFile(BytesIO(raw))
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or len(infos) > MAX_LOG_MEMBERS:
            raise PublicationEvidenceError("acknowledgement_log_archive_members_invalid")
        if "0_reconcile.txt" not in names or not any(
            name.startswith("reconcile/") and name.endswith("Reconcile publication and anonymous read-back evidence.txt")
            for name in names
        ):
            raise PublicationEvidenceError("acknowledgement_reconcile_logs_missing")
        if sum(item.file_size for item in infos) > MAX_LOG_MEMBER_BYTES:
            raise PublicationEvidenceError("acknowledgement_log_archive_expanded_size_exceeded")
        for item in infos:
            mode = item.external_attr >> 16
            file_type = mode & 0o170000
            relative = pathlib.PurePosixPath(item.filename)
            if (
                item.is_dir() or relative.is_absolute() or ".." in relative.parts
                or "\\" in item.filename or (file_type and file_type != 0o100000)
                or item.file_size > MAX_LOG_MEMBER_BYTES
            ):
                raise PublicationEvidenceError("acknowledgement_log_member_unsafe")
        if archive.testzip() is not None:
            raise PublicationEvidenceError("acknowledgement_log_crc_invalid")
        summary = archive.read("0_reconcile.txt").decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError, KeyError) as exc:
        raise PublicationEvidenceError("acknowledgement_log_archive_invalid") from exc
    publisher_ids = re.findall(r"PUBLICATION_RUN_ID:\s*([0-9]+)", summary)
    if not publisher_ids or set(publisher_ids) != {str(PUBLISHER_RUN_ID)}:
        raise PublicationEvidenceError("acknowledgement_log_publisher_binding_mismatch")
    results: list[tuple[dt.datetime, Mapping[str, Any]]] = []
    for line in summary.splitlines():
        if "\"status\": \"read-back-confirmed\"" not in line:
            continue
        match = re.match(r"^(\S+Z)\s+(\{.*\})$", line)
        if not match:
            raise PublicationEvidenceError("acknowledgement_log_result_line_invalid")
        timestamp = _parse_time(match.group(1), "acknowledgement_log_result_time")
        try:
            result = json.loads(match.group(2), object_pairs_hook=_strict_pairs)
        except (json.JSONDecodeError, PublicationEvidenceError) as exc:
            raise PublicationEvidenceError("acknowledgement_log_result_invalid") from exc
        if not isinstance(result, Mapping):
            raise PublicationEvidenceError("acknowledgement_log_result_not_object")
        results.append((timestamp, result))
    if len(results) != 1:
        raise PublicationEvidenceError("acknowledgement_log_result_ambiguous")
    return summary, results


def _target_journal_row(journal: Mapping[str, Any], *, source_sha: str, manifest_sha256: str,
                        registry_sha256: str, registry_bytes: int) -> Mapping[str, Any]:
    rows = journal.get("records")
    if not isinstance(rows, list) or len(rows) != 4:
        raise PublicationEvidenceError("acknowledgement_journal_record_count_invalid")
    matches: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise PublicationEvidenceError("acknowledgement_journal_record_invalid")
        candidate = row.get("candidate")
        pr = row.get("pr")
        source = row.get("source_refresh_evidence")
        if not isinstance(candidate, Mapping) or not isinstance(pr, Mapping) or not isinstance(source, Mapping):
            continue
        candidate_repository = candidate.get("repository")
        if (
            isinstance(candidate_repository, str)
            and candidate_repository.casefold() == REPOSITORY.casefold()
            and candidate.get("source_id") == "data_go_kr"
            and candidate.get("scope") == "aggregate_supported_catalog"
            and candidate.get("manifest_sha256") == manifest_sha256
            and candidate.get("registry_path") == REGISTRY_PATH
            and candidate.get("registry_sha256") == registry_sha256
            and candidate.get("registry_bytes") == registry_bytes
            and source.get("registry_path") == REGISTRY_PATH
            and source.get("registry_sha256") == registry_sha256
            and source.get("registry_bytes") == registry_bytes
            and pr.get("merge_commit_sha") == source_sha
        ):
            matches.append(row)
    if len(matches) != 1:
        raise PublicationEvidenceError("acknowledgement_journal_target_ambiguous")
    return matches[0]


def _validate_ack_journal(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    source_sha: str,
    manifest_sha256: str,
    registry_sha256: str,
    registry_bytes: int,
    receipt_sha256: str,
    payload_revision: str,
    pointer_revision: str,
    ack_started_at: dt.datetime,
    ack_completed_at: dt.datetime,
) -> dt.datetime:
    for label, journal in (("before", before), ("after", after)):
        if (
            journal.get("schema_version") != "datapan.canonical-update-promotion-journal.v1"
            or journal.get("repository", "").casefold() != REPOSITORY.casefold()
        ):
            raise PublicationEvidenceError(f"acknowledgement_journal_{label}_identity_invalid")
    before_row = _target_journal_row(before, source_sha=source_sha, manifest_sha256=manifest_sha256,
                                    registry_sha256=registry_sha256, registry_bytes=registry_bytes)
    after_row = _target_journal_row(after, source_sha=source_sha, manifest_sha256=manifest_sha256,
                                   registry_sha256=registry_sha256, registry_bytes=registry_bytes)
    if before_row.get("status") != "merged" or after_row.get("status") != "read-back-confirmed":
        raise PublicationEvidenceError("acknowledgement_journal_transition_invalid")
    candidate = after_row.get("candidate")
    ownership = after_row.get("ownership")
    pr = after_row.get("pr")
    if (
        not isinstance(candidate, Mapping) or not isinstance(ownership, Mapping) or not isinstance(pr, Mapping)
        or candidate.get("generation_id") != EXPECTED_GENERATION
        or candidate.get("head_sha") != "8b086826fb1e30ea949cfb8222a252e7aa09fa40"
        or candidate.get("repository", "").casefold() != REPOSITORY.casefold()
        or candidate.get("registry_path") != REGISTRY_PATH
        or pr.get("number") != TARGET_PR
        or pr.get("state") != "merged"
        or pr.get("merge_commit_sha") != source_sha
        or ownership.get("expected_head_sha") != candidate.get("head_sha")
        or ownership.get("body_sha256") != hashlib.sha256(str(ownership.get("body", "")).encode("utf-8")).hexdigest()
        or after_row.get("superseded_by") is not None
    ):
        raise PublicationEvidenceError("acknowledgement_journal_owner_binding_mismatch")
    immutable_keys = set(before_row) | set(after_row)
    if not {"status", "acknowledgements"}.issubset(immutable_keys):
        raise PublicationEvidenceError("acknowledgement_journal_target_shape_invalid")
    for key in immutable_keys - {"status", "acknowledgements"}:
        if before_row.get(key) != after_row.get(key):
            raise PublicationEvidenceError("acknowledgement_journal_target_mutated")
    before_records = before.get("records")
    after_records = after.get("records")
    if not isinstance(before_records, list) or not isinstance(after_records, list):
        raise PublicationEvidenceError("acknowledgement_journal_records_missing")
    before_others = [row for row in before_records if row is not before_row]
    after_others = [row for row in after_records if row is not after_row]
    if before_others != after_others:
        raise PublicationEvidenceError("acknowledgement_journal_unrelated_records_changed")
    before_acks = before_row.get("acknowledgements")
    after_acks = after_row.get("acknowledgements")
    if not isinstance(before_acks, list) or len(before_acks) != 2 or not isinstance(after_acks, list):
        raise PublicationEvidenceError("acknowledgement_journal_history_invalid")
    if after_acks[:len(before_acks)] != before_acks or len(after_acks) != len(before_acks) + 3:
        raise PublicationEvidenceError("acknowledgement_journal_history_not_preserved")
    added = after_acks[len(before_acks):]
    if [item.get("status") if isinstance(item, Mapping) else None for item in added] != [
        "publication-pending", "published", "read-back-confirmed",
    ]:
        raise PublicationEvidenceError("acknowledgement_journal_transition_sequence_invalid")
    observed_times: list[dt.datetime] = []
    for item in added:
        if not isinstance(item, Mapping):
            raise PublicationEvidenceError("acknowledgement_journal_ack_invalid")
        identity = item.get("artifact_identity")
        if (
            item.get("run_id") != ACK_RUN_ID
            or item.get("run_attempt") != ACK_ATTEMPT
            or item.get("source_sha") != source_sha
            or item.get("manifest_sha256") != manifest_sha256
            or item.get("run_url") != f"https://github.com/{REPOSITORY}/actions/runs/{ACK_RUN_ID}/attempts/{ACK_ATTEMPT}"
            or not isinstance(identity, Mapping)
            or identity.get("path") != REGISTRY_PATH
            or identity.get("bytes") != registry_bytes
            or identity.get("sha256") != registry_sha256
        ):
            raise PublicationEvidenceError("acknowledgement_journal_ack_identity_mismatch")
        reference = item.get("evidence_reference")
        if item.get("status") == "publication-pending":
            if reference != "existing_manual_publication_workflow_completed":
                raise PublicationEvidenceError("acknowledgement_journal_pending_reference_invalid")
        elif not isinstance(reference, str) or not reference.endswith(f"sha256={receipt_sha256}"):
            raise PublicationEvidenceError("acknowledgement_journal_receipt_reference_invalid")
        observed_times.append(_parse_time(item.get("observed_at"), "acknowledgement_journal_observed_at"))
    if len(set(observed_times)) != 1:
        raise PublicationEvidenceError("acknowledgement_journal_transition_times_differ")
    final_ack = added[-1]
    if (
        final_ack.get("read_back_verified") is not True
        or final_ack.get("read_back_sha256") != registry_sha256
        or final_ack.get("read_back_bytes") != registry_bytes
        or final_ack.get("publication_revision") != payload_revision
        or final_ack.get("publication_pointer_revision") != pointer_revision
    ):
        raise PublicationEvidenceError("acknowledgement_journal_readback_identity_mismatch")
    observed = observed_times[-1]
    if observed < ack_started_at or observed > ack_completed_at:
        raise PublicationEvidenceError("acknowledgement_journal_observed_outside_attempt")
    return observed


def validate_historical_publication_facet(
    *,
    inputs: Mapping[str, object],
    expected_source_sha: str,
    expected_manifest_sha256: str,
    expected_registry_sha256: str,
    expected_registry_bytes: int,
    repo_root: pathlib.Path,
    evaluation_epoch: str,
) -> dict[str, object]:
    """Admit the exact retained native publication and same-subject read-back.

    All input file digests and source-tree provenance are checked by the caller's
    completeness input-index validator before this function is called.  This
    function independently validates the captured semantic identities and
    preserves event times.  Missing roles yield an explicit ``missing`` facet;
    present but malformed or conflicting evidence raises
    :class:`PublicationEvidenceError`.
    """
    if not isinstance(inputs, Mapping):
        raise PublicationEvidenceError("publication_inputs_must_be_object")
    source_sha = _sha(expected_source_sha, SHA1_RE, "expected_source_sha")
    manifest_sha = _sha(expected_manifest_sha256, SHA256_RE, "expected_manifest_sha256")
    registry_sha = _sha(expected_registry_sha256, SHA256_RE, "expected_registry_sha256")
    registry_bytes = _positive_int(expected_registry_bytes, "expected_registry_bytes")
    epoch = _parse_time(evaluation_epoch, "evaluation_epoch")
    subject = {
        "repository": REPOSITORY,
        "source_sha": source_sha,
        "manifest_sha256": manifest_sha,
        "registry_path": REGISTRY_PATH,
        "registry_sha256": registry_sha,
        "registry_bytes": registry_bytes,
        "publisher_run_id": PUBLISHER_RUN_ID,
        "publisher_attempt": PUBLISHER_ATTEMPT,
        "acknowledgement_run_id": ACK_RUN_ID,
        "acknowledgement_attempt": ACK_ATTEMPT,
    }
    missing = sorted(name for name in REQUIRED_INPUTS if name not in inputs or inputs[name] is None)
    if missing:
        return {
            "status": "missing",
            "subject": subject,
            "missing": missing,
            "evidence": [],
            "details": {
                "classification": "historical_delivery_only",
                "currentness_established": False,
                "updated_claim_established": False,
                "release_authority": False,
            },
        }
    if source_sha != "6a5138c792f4b7402da0c5ab439646bd752a307f":
        raise PublicationEvidenceError("unexpected_historical_source_identity")
    root = pathlib.Path(repo_root).resolve()
    validator = _load_ack_validator(root)

    receipt_raw = _json_bytes(inputs["publication_receipt"], "publication_receipt")
    binding_raw = _json_bytes(inputs["source_binding"], "source_binding")
    manifest_raw = _json_bytes(inputs["source_manifest"], "source_manifest")
    anonymous_manifest_raw = _json_bytes(inputs["anonymous_manifest"], "anonymous_manifest")
    receipt = _mapping(validator.parse_json(receipt_raw, label="publication_receipt"), "publication_receipt")
    binding = _mapping(validator.parse_json(binding_raw, label="source_binding"), "source_binding")
    if receipt != _mapping(validator.parse_json(_receipt_member(inputs, validator, 0), label="archive_receipt"), "archive_receipt"):
        raise PublicationEvidenceError("publication_archive_receipt_mismatch")
    if binding != _mapping(validator.parse_json(_receipt_member(inputs, validator, 1), label="archive_source_binding"), "archive_source_binding"):
        raise PublicationEvidenceError("publication_archive_source_binding_mismatch")
    workflow = _mapping(inputs["publisher_workflow"], "publisher_workflow")
    if (
        workflow.get("id") != PUBLISHER_WORKFLOW_ID
        or workflow.get("path") != PUBLISHER_WORKFLOW_PATH
        or workflow.get("state") != "active"
    ):
        raise PublicationEvidenceError("publisher_workflow_identity_mismatch")
    run = _mapping(inputs["publisher_run"], "publisher_run")
    jobs = _mapping(inputs["publisher_jobs"], "publisher_jobs")
    run_id, attempt, publisher_head = validator.validate_workflow_run(
        run,
        repository=REPOSITORY,
        repository_id=1278568329,
        workflow_path=PUBLISHER_WORKFLOW_PATH,
        workflow_id=PUBLISHER_WORKFLOW_ID,
        default_branch="main",
        required_event="workflow_dispatch",
    )
    if run_id != PUBLISHER_RUN_ID or attempt != PUBLISHER_ATTEMPT:
        raise PublicationEvidenceError("publisher_attempt_identity_mismatch")
    publisher_mode, publisher_started, publisher_completed = validator.validate_job_and_steps(
        jobs, run_id=run_id, attempt=attempt, head_sha=publisher_head,
    )
    if publisher_mode != "publishing":
        raise PublicationEvidenceError("publisher_run_did_not_publish")
    job_rows = jobs.get("jobs")
    publisher_job = next(row for row in job_rows if isinstance(row, Mapping) and row.get("name") == validator.PUBLISHER_JOB)
    publisher_steps = publisher_job.get("steps", [])
    publish_step = next(row for row in publisher_steps if isinstance(row, Mapping) and row.get("name") == validator.PUBLISH_STEP)
    verify_step = next(row for row in publisher_steps if isinstance(row, Mapping) and row.get("name") == validator.VERIFY_STEP)
    publish_started = _parse_time(publish_step.get("started_at"), "publication_step_started_at")
    publish_completed = _parse_time(publish_step.get("completed_at"), "publication_step_completed_at")
    verify_started = _parse_time(verify_step.get("started_at"), "anonymous_verify_step_started_at")
    verify_completed = _parse_time(verify_step.get("completed_at"), "anonymous_verify_step_completed_at")
    if not publisher_started <= publish_started <= publish_completed <= verify_started <= verify_completed <= publisher_completed:
        raise PublicationEvidenceError("publisher_step_time_order_invalid")

    artifact = _artifact_from_metadata(inputs["publisher_artifact_metadata"])
    archive_raw = inputs["publisher_archive"]
    if not isinstance(archive_raw, bytes) or not archive_raw:
        raise PublicationEvidenceError("publisher_archive_bytes_invalid")
    artifact_digest = artifact.get("digest")
    if isinstance(artifact_digest, str) and artifact_digest.startswith("sha256:"):
        artifact_digest = artifact_digest.removeprefix("sha256:")
    _sha(artifact_digest, SHA256_RE, "publisher_artifact_digest")
    artifact_run = artifact.get("workflow_run")
    if (
        artifact.get("id") != PUBLISHER_ARTIFACT_ID
        or artifact.get("name") != PUBLISHER_ARTIFACT_NAME
        or artifact.get("expired") is not False
        or artifact.get("size_in_bytes") != len(archive_raw)
        or artifact_digest != hashlib.sha256(archive_raw).hexdigest()
        or not isinstance(artifact_run, Mapping)
        or artifact_run.get("id") != PUBLISHER_RUN_ID
        or artifact_run.get("repository_id") != 1278568329
        or artifact_run.get("head_repository_id") != 1278568329
        or artifact_run.get("head_sha") != publisher_head
    ):
        raise PublicationEvidenceError("publisher_artifact_identity_mismatch")
    artifact_created = _parse_time(artifact.get("created_at"), "publisher_artifact_created_at")
    expires_at = _parse_time(artifact.get("expires_at"), "publisher_artifact_expires_at")
    if not publisher_started <= artifact_created <= publisher_completed or expires_at <= artifact_created:
        raise PublicationEvidenceError("publisher_artifact_availability_interval_invalid")
    archive_receipt_raw, archive_binding_raw = validator.extract_receipt_archive(archive_raw)
    if archive_receipt_raw != receipt_raw or archive_binding_raw != binding_raw:
        raise PublicationEvidenceError("publication_archive_member_bytes_mismatch")
    publication_identity = validator.verify_receipt(
        receipt_raw, binding_raw, repository=REPOSITORY, workflow_head_sha=publisher_head, root=root,
    )
    if (
        publication_identity["source_sha"] != source_sha
        or publication_identity["manifest_sha256"] != manifest_sha
        or binding.get("source_sha") != source_sha
        or binding.get("manifest_sha256") != manifest_sha
        or binding.get("workflow_sha") != publisher_head
    ):
        raise PublicationEvidenceError("publication_source_binding_mismatch")

    if hashlib.sha256(manifest_raw).hexdigest() != manifest_sha or anonymous_manifest_raw != manifest_raw:
        raise PublicationEvidenceError("source_or_anonymous_manifest_bytes_mismatch")
    source_manifest = _mapping(_parse_json(manifest_raw, "source_manifest"), "source_manifest")
    manifest_artifact = _artifact_row(source_manifest, "source_manifest")
    if (
        manifest_artifact.get("kind") != "registry"
        or manifest_artifact.get("bytes") != registry_bytes
        or manifest_artifact.get("sha256") != registry_sha
    ):
        raise PublicationEvidenceError("source_manifest_registry_identity_mismatch")
    source_commit = _mapping(inputs["source_commit"], "source_commit")
    if source_commit.get("sha") != source_sha:
        raise PublicationEvidenceError("source_commit_sha_mismatch")
    source_commit_body = _mapping(source_commit.get("commit"), "source_commit_body")
    source_tree = _mapping(source_commit_body.get("tree"), "source_commit_tree").get("sha")
    if source_tree != binding.get("source_tree_sha"):
        raise PublicationEvidenceError("source_commit_tree_mismatch")
    pointer_oid, pointer_size = _parse_lfs_pointer_patch(source_commit)
    if (pointer_oid, pointer_size) != (registry_sha, registry_bytes):
        raise PublicationEvidenceError("source_commit_lfs_identity_mismatch")
    try:
        committed_pointer = subprocess.run(
            ("git", "show", f"{source_sha}:{REGISTRY_PATH}"),
            cwd=root, capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicationEvidenceError("source_registry_pointer_unavailable") from exc
    expected_pointer_bytes = (
        f"{LFS_VERSION}\noid sha256:{registry_sha}\nsize {registry_bytes}\n".encode("ascii")
    )
    if committed_pointer.returncode != 0 or committed_pointer.stdout != expected_pointer_bytes:
        raise PublicationEvidenceError("source_tree_lfs_pointer_mismatch")
    snapshot = _mapping(inputs["source_catalog_snapshot"], "source_catalog_snapshot")
    if (
        snapshot.get("path") != REGISTRY_PATH
        or snapshot.get("bytes") != registry_bytes
        or snapshot.get("sha256") != registry_sha
        or snapshot.get("role") not in (None, "source_catalog_snapshot")
    ):
        raise PublicationEvidenceError("source_catalog_snapshot_identity_mismatch")

    publication = _mapping(receipt.get("publication"), "publication")
    anonymous = _mapping(receipt.get("anonymous_verification"), "anonymous_verification")
    payload_revision = _sha(publication.get("payload_revision"), SHA1_RE, "payload_revision")
    pointer_revision = _sha(publication.get("pointer_revision"), SHA1_RE, "pointer_revision")
    if (
        publication.get("status") != "published"
        or publication.get("dataset") != REPOSITORY
        or publication.get("artifacts") != 196
        or anonymous.get("status") != "verified"
        or anonymous.get("dataset") != REPOSITORY
        or anonymous.get("revision") != payload_revision
        or anonymous.get("release_manifest") != "verified"
        or anonymous.get("artifacts") != publication.get("artifacts")
    ):
        raise PublicationEvidenceError("publication_receipt_subject_invalid")
    _check_distribution_index(
        _mapping(inputs["pointer_immutable"], "pointer_immutable"),
        payload_revision=payload_revision, pointer_revision=pointer_revision,
        manifest_sha256=manifest_sha, manifest_bytes=len(manifest_raw),
        registry_sha256=registry_sha, registry_bytes=registry_bytes,
        artifact_count=publication["artifacts"], label="immutable_pointer",
    )
    before_meta, before_index = _pointer_bundle(inputs["pointer_before"], "pointer_before")
    after_meta, after_index = _pointer_bundle(inputs["pointer_after"], "pointer_after")
    for label, metadata in (("pointer_before", before_meta), ("pointer_after", after_meta)):
        siblings = metadata.get("siblings")
        if (
            metadata.get("id") != REPOSITORY
            or metadata.get("sha") != pointer_revision
            or not isinstance(siblings, list)
            or not {MANIFEST_PATH, REGISTRY_PATH}.issubset({
                item.get("rfilename") for item in siblings if isinstance(item, Mapping)
            })
        ):
            raise PublicationEvidenceError(f"{label}_repo_metadata_mismatch")
    for label, index in (("pointer_before", before_index), ("pointer_after", after_index)):
        _check_distribution_index(
            index, payload_revision=payload_revision, pointer_revision=pointer_revision,
            manifest_sha256=manifest_sha, manifest_bytes=len(manifest_raw),
            registry_sha256=registry_sha, registry_bytes=registry_bytes,
            artifact_count=publication["artifacts"], label=label,
        )
    immutable_index = _mapping(inputs["pointer_immutable"], "pointer_immutable")
    if (
        before_meta.get("sha") != after_meta.get("sha")
        or before_index != after_index
        or after_index != immutable_index
    ):
        raise PublicationEvidenceError("public_pointer_changed_during_readback")

    readback = _mapping(inputs["anonymous_payload"], "anonymous_payload")
    if readback.get("publisher_run_id") != PUBLISHER_RUN_ID or readback.get("publisher_attempt") != PUBLISHER_ATTEMPT:
        raise PublicationEvidenceError("anonymous_readback_publisher_mismatch")
    checks = readback.get("checks")
    if not isinstance(checks, list):
        raise PublicationEvidenceError("anonymous_readback_checks_missing")
    stream_rows = [item for item in checks if isinstance(item, Mapping) and item.get("check") == "immutable_registry_stream_matches_expected_sha_and_size"]
    if len(stream_rows) != 1:
        raise PublicationEvidenceError("anonymous_registry_stream_check_ambiguous")
    stream = _mapping(stream_rows[0].get("detail"), "anonymous_registry_stream_detail")
    readback_at = _parse_time(readback.get("generated_at"), "anonymous_readback_observed_at")
    if (
        stream.get("path") != REGISTRY_PATH
        or stream.get("bytes_streamed") != registry_bytes
        or stream.get("sha256") != registry_sha
        or stream.get("revision") != payload_revision
    ):
        raise PublicationEvidenceError("anonymous_readback_subject_mismatch")

    ack_run = _mapping(inputs["ack_run"], "ack_run")
    ack_jobs = _mapping(inputs["ack_jobs"], "ack_jobs")
    ack_run_id, ack_attempt, ack_head = validator.validate_workflow_run(
        ack_run,
        repository=REPOSITORY,
        repository_id=1278568329,
        workflow_path=ACK_WORKFLOW_PATH,
        workflow_id=ACK_WORKFLOW_ID,
        default_branch="main",
        required_event="workflow_run",
    )
    if ack_run_id != ACK_RUN_ID or ack_attempt != ACK_ATTEMPT or ack_head != publisher_head:
        raise PublicationEvidenceError("acknowledgement_attempt_identity_mismatch")
    ack_jobs_rows = ack_jobs.get("jobs")
    ack_total = ack_jobs.get("total_count")
    if (
        not isinstance(ack_jobs_rows, list) or isinstance(ack_total, bool)
        or not isinstance(ack_total, int) or ack_total != len(ack_jobs_rows) or ack_total != 1
    ):
        raise PublicationEvidenceError("acknowledgement_jobs_incomplete")
    ack_job = ack_jobs_rows[0]
    if (
        not isinstance(ack_job, Mapping)
        or ack_job.get("run_id") != ACK_RUN_ID
        or ack_job.get("run_attempt") != ACK_ATTEMPT
        or ack_job.get("head_sha") != ack_head
        or ack_job.get("name") != "reconcile"
        or ack_job.get("status") != "completed"
        or ack_job.get("conclusion") != "success"
    ):
        raise PublicationEvidenceError("acknowledgement_job_untrusted")
    ack_started = _parse_time(ack_job.get("started_at"), "acknowledgement_job_started_at")
    ack_completed = _parse_time(ack_job.get("completed_at"), "acknowledgement_job_completed_at")
    if ack_completed < ack_started:
        raise PublicationEvidenceError("acknowledgement_job_interval_invalid")
    ack_steps = ack_job.get("steps")
    if not isinstance(ack_steps, list):
        raise PublicationEvidenceError("acknowledgement_steps_missing")
    reconcile_steps = [step for step in ack_steps if isinstance(step, Mapping) and step.get("name") == "Reconcile publication and anonymous read-back evidence"]
    if len(reconcile_steps) != 1 or reconcile_steps[0].get("status") != "completed" or reconcile_steps[0].get("conclusion") != "success":
        raise PublicationEvidenceError("acknowledgement_reconcile_step_untrusted")
    reconcile_step = reconcile_steps[0]
    reconcile_started = _parse_time(reconcile_step.get("started_at"), "acknowledgement_reconcile_started_at")
    reconcile_completed = _parse_time(reconcile_step.get("completed_at"), "acknowledgement_reconcile_completed_at")
    if not ack_started <= reconcile_started <= reconcile_completed <= ack_completed:
        raise PublicationEvidenceError("acknowledgement_step_time_order_invalid")
    summary, log_results = _extract_ack_log(inputs["ack_log"])
    output_at, log_result = log_results[0]
    if (
        log_result.get("status") != "read-back-confirmed"
        or log_result.get("source_sha") != source_sha
        or log_result.get("manifest_sha256") != manifest_sha
        or log_result.get("run_url") != f"https://github.com/{REPOSITORY}/actions/runs/{ACK_RUN_ID}/attempts/{ACK_ATTEMPT}"
        # GitHub's completed_at API timestamp is second precision; the log
        # retains sub-second precision and can be later within that same second.
        or not ack_started <= reconcile_started <= output_at <= ack_completed
    ):
        raise PublicationEvidenceError("acknowledgement_log_result_binding_mismatch")
    journal_observed = _validate_ack_journal(
        _mapping(inputs["journal_before"], "journal_before"),
        _mapping(inputs["journal_after"], "journal_after"),
        source_sha=source_sha, manifest_sha256=manifest_sha,
        registry_sha256=registry_sha, registry_bytes=registry_bytes,
        receipt_sha256=hashlib.sha256(receipt_raw).hexdigest(),
        payload_revision=payload_revision, pointer_revision=pointer_revision,
        ack_started_at=ack_started, ack_completed_at=ack_completed,
    )
    if output_at < journal_observed:
        raise PublicationEvidenceError("acknowledgement_log_precedes_journal_transition")
    if not (
        verify_completed <= publisher_completed <= readback_at <= ack_started
        and ack_completed <= epoch
        and output_at <= epoch
        and readback_at <= epoch
        and publisher_completed <= epoch
        and journal_observed <= epoch
    ):
        raise PublicationEvidenceError("publication_evidence_after_evaluation_epoch")

    return {
        "status": "verified",
        "subject": {
            **subject,
            "publisher_head_sha": publisher_head,
            "source_tree_sha": binding.get("source_tree_sha"),
            "payload_revision": payload_revision,
            "pointer_revision": pointer_revision,
            "candidate_generation_id": EXPECTED_GENERATION,
            "candidate_head_sha": "8b086826fb1e30ea949cfb8222a252e7aa09fa40",
            "pull_request": TARGET_PR,
        },
        "missing": [],
        "evidence": sorted(REQUIRED_INPUTS),
        "details": {
            "classification": "historical_delivery_only",
            "currentness_established": False,
            "updated_claim_established": False,
            "release_authority": False,
            "cutover_established": False,
            "evaluation_epoch": _time_text(epoch),
            "publisher_job_started_at": _time_text(publisher_started),
            "publisher_job_completed_at": _time_text(publisher_completed),
            "publisher_publish_step_completed_at": _time_text(publish_completed),
            "publisher_anonymous_verify_step_completed_at": _time_text(verify_completed),
            "anonymous_readback_observed_at": _time_text(readback_at),
            "acknowledgement_job_started_at": _time_text(ack_started),
            "acknowledgement_job_completed_at": _time_text(ack_completed),
            "acknowledgement_log_result_at": _time_text(output_at),
            "acknowledgement_journal_observed_at": _time_text(journal_observed),
            "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
            "publisher_artifact_sha256": artifact_digest,
            "publisher_artifact_expires_at": _time_text(expires_at),
            "publisher_artifact_available_at_evaluation": expires_at > epoch,
            "acknowledgement_attempt": ACK_ATTEMPT,
            "acknowledgement_log_sha256": hashlib.sha256(inputs["ack_log"]).hexdigest(),
        },
    }


def _receipt_member(inputs: Mapping[str, object], validator: Any, member_index: int) -> bytes:
    archive = inputs.get("publisher_archive")
    if not isinstance(archive, bytes):
        raise PublicationEvidenceError("publisher_archive_bytes_invalid")
    receipt_raw, binding_raw = validator.extract_receipt_archive(archive)
    return (receipt_raw, binding_raw)[member_index]
