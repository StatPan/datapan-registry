#!/usr/bin/env python3
"""Prepare owned, reviewable canonical update PRs from frozen processor runs.

The workflow caller validates the immutable #657 state/artifact handoff,
refreshes source-bound release evidence with the pinned native CLI, uploads
only the candidate LFS object, proves it from an empty storage root, and then
uses Git/API compare-and-swap checks before advancing an owned PR. It never
merges or publishes to Hugging Face.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import shlex
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

PROCESSOR_SCHEMA = "datapan.upstream-catalogue-checkpoint.v1"
JOURNAL_SCHEMA = "datapan.canonical-update-promotion-journal.v1"
JOURNAL_PATH = pathlib.Path("reports/canonical-update-promotion-receipt.json")
STATE_BRANCH = "automation/canonical-update-state"
PROCESSOR_STATE_BRANCH = "automation/upstream-catalogue-state"
PROCESSOR_WORKFLOW_NAME = "Process upstream catalogue"
PROCESSOR_WORKFLOW_PATH = ".github/workflows/upstream-catalogue-process.yml"
PROCESSOR_ARTIFACT_PREFIX = "upstream-catalogue-processing-"
VERIFY_WORKFLOW_PATH = ".github/workflows/verify-release.yml"
GITHUB_API_VERSION = "2026-03-10"
CI_EXPECTATION_UNSET = object()
PROCESSING_PREFIX = "upstream-catalogue-processing-"
PROMOTION_ISSUE_MARKER = "datapan-canonical-update-issue:v1:"
REVIEW_DIR = pathlib.Path("reports/data-go-kr/upstream-catalogue-review")
MAX_PROCESSOR_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_PROCESSOR_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PROCESSOR_ARTIFACT_PAGES = 10
PROCESSOR_INPUT_PROVENANCE = {
    "policy_sha256": "policy/source-refresh.json",
    "adapter_revision": "data/provider-index.json",
    "generator_revision": "scripts/process-upstream-catalogue-candidate.py",
    "extractor_revision": "scripts/generate-batch-link-detail-registry-patches.py",
}
PROCESSOR_COMPOSITION_INPUTS = {
    "registry_schema": "schemas/datapan.specs.v1.schema.json",
    "provider_index_schema": "schemas/datapan.provider-index.v1.schema.json",
    "diff_schema": "schemas/datapan.catalog-diff.v1.schema.json",
    "refresh_evidence_schema": "schemas/datapan.upstream-refresh-evidence.v1.schema.json",
    "enrichment_evidence_schema": "schemas/datapan.catalogue-enrichment-evidence.v1.schema.json",
    "composer": "scripts/compose-upstream-catalogue-candidate.py",
    "receipt_schema": "schemas/datapan.catalogue-composition-receipt.v1.schema.json",
}
PROCESSOR_COMPATIBILITY_FILES = (
    *PROCESSOR_INPUT_PROVENANCE.values(),
    ".github/workflows/upstream-catalogue-process.yml",
    ".github/workflows/upstream-catalog-refresh.yml",
    "scripts/upstream-catalogue-state-branch.py",
    "scripts/compose-upstream-catalogue-candidate.py",
    "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
    "schemas/datapan.catalogue-composition-receipt.v1.schema.json",
    *PROCESSOR_COMPOSITION_INPUTS.values(),
)
GENERATED_FILE_ALLOWLIST = frozenset({
    "data/data-go-kr.registry.json", "manifest.json", "schemas/index.json",
    "policy/data-go-kr-operation-denominator-expectation.json",
    "reports/catalog-diff.json", "reports/catalog-audit.json", "reports/error-catalog.json",
    "reports/dependencies.json", "reports/adapter-targets.json", "reports/provider-backlog.json",
    "reports/route-disposition.json", "reports/coverage.json", "reports/verification-plan.json",
    "reports/data-go-kr/operation-denominator.json", "reports/data-go-kr/operation-manifest.json",
    "reports/operation-denominator-rollup.json", "reports/current-runtime-evidence-projection.json",
    "reports/runtime-freshness-queue.json", "reports/health-probe-catalog.json",
    "reports/diagnostic-current-source-applicability.json",
    "fixtures/health-probe-catalog/cli-health-probe-v1.json", "reports/data-go-kr/runtime-evidence-growth.json",
    "drafts/operation-assertion-policies/operation-assertion-policies.v1.json",
    "fixtures/operation-assertion-policies/datapan-health-consumer-proof.v1.json",
    "drafts/operation-assertion-policies/release-manifest.v1.json",
    "drafts/operation-assertion-policies/release-candidate.v1.json",
    "drafts/diagnostic-envelope/release-candidate/diagnostic-release-candidate.v1.json",
    "schemas/datapan.diagnostic-envelope.v1.schema.json", "policy/diagnostic-envelope-consumer-contract.v1.json",
    "policy/data-go-kr-diagnostic-evidence-mapping.v1.json", "policy/diagnostic-cause-action-vocabulary.v1.json",
    "reports/diagnostic-publication-readiness.json",
    "reports/diagnostic-consumer-compatibility/datapan-cli.v1.json",
    "reports/diagnostic-consumer-compatibility/datapan-health.v1.json",
    "reports/diagnostic-consumer-compatibility/datapan-web.v1.json",
    "reports/data-go-kr/coverage-backlog.json", "docs/data-go-kr-coverage-backlog.md",
    "reports/data-go-kr/external-adapter-backlog.json", "docs/data-go-kr-external-adapter-backlog.md",
    "reports/data-go-kr/operation-materialization-plan.json", "docs/data-go-kr-operation-materialization-plan.md",
    "reports/data-go-kr/institution-api-overview.json", "docs/data-go-kr-institution-api-overview.md",
    "reports/data-go-kr/institution-runtime-plan.json", "docs/data-go-kr-institution-runtime-plan.md",
    "reports/sustainable-coverage.json", "README.md",
    "reports/source-contract-rollup.json", "reports/error-action-routing-rollup.json",
    "reports/failure-recovery-rollup.json", "reports/source-report-inventory.json",
    "reports/source-runtime-evidence-rollup.json", "docs/source-runtime-readiness.md",
    "reports/source-runtime-remediation-map.json", "reports/credential-runtime-evidence-policy.json",
    "reports/credential-runtime-collection-preflight.json", "reports/credential-runtime-runner-readiness.json",
    "reports/credential-runtime-receipt-collection-queue.json", "reports/credential-runtime-review-handoff.json",
    "reports/credential-runtime-collection-execution-plan.json", "reports/release-consumer-compatibility.json",
    "reports/credential-runtime-manual-review-technical-rebinding.json",
    "reports/credential-runtime-manual-review-acceptance.json",
    "reports/credential-runtime-manual-review-acceptance-packet.json",
    "reports/registry-impact-plan.json", "reports/release-distribution-footprint.json",
    "reports/release-shard-consumer-proof.json", "reports/release-operational-pressure.json",
    "reports/release-consumer-decision.json", "docs/release-ledger-goal-completion-audit.json",
    "reports/release-goal-finish-preflight.json", "reports/release-goal-continuation-queue.json",
    "reports/release-goal-operating-contract.json", "reports/release-assembly-receipt.json",
    "reports/health-runtime-observation-plan.v1.json", "reports/release-version-decision.json",
    "reports/regional-baseline-source-provenance.json", "fixtures/source-provenance/regional-baseline-v0-pin.json",
})
REQUIRED_PROCESSOR_FILES = (
    "composed-candidate.registry.json",
    "ready-scope.registry.json",
    "semantic-diff.json",
    "regeneration-queue.json",
    "quarantine.json",
    "composition-receipt.json",
    "upstream-catalogue-enrichment-evidence.json",
    "upstream-catalogue-processing-result.json",
)


class PromotionError(RuntimeError):
    pass


class ProcessorCandidateError(PromotionError):
    """One retained B generation is unusable; later durable generations may proceed."""


class ProcessorArtifactUnavailable(ProcessorCandidateError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_object(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"invalid required JSON input: {path}") from exc
    if not isinstance(value, dict):
        raise PromotionError(f"required JSON input must be an object: {path}")
    return value


def load_module(path: pathlib.Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PromotionError(f"cannot load required helper: {path}")
    module = importlib.util.module_from_spec(spec)
    missing = object()
    previous = sys.modules.get(name, missing)
    # Some helpers define dataclasses (and other decorators) that resolve
    # their module through sys.modules while the class body is executing.
    # Match normal import semantics, and avoid leaving a half-initialized
    # module behind when execution fails.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module


def load_canonical_update_pr(root: pathlib.Path) -> Any:
    """Load the shared canonical composition and promotion validator."""
    return load_module(root / "scripts/canonical_update_pr.py", "canonical_update_pr")


def command(
    argv: Sequence[str],
    cwd: pathlib.Path,
    *,
    allowed_returncodes: frozenset[int] = frozenset({0}),
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"+ [{cwd}] {shlex.join(tuple(argv))}", flush=True)
    result = subprocess.run(tuple(argv), cwd=cwd, text=True, capture_output=True, check=False, env=dict(env) if env else None)
    if result.returncode not in allowed_returncodes:
        # Authenticated Git LFS failures can contain signed URLs; report only
        # the command identity and exit code, never provider response text.
        raise PromotionError(f"command failed ({result.returncode}): {shlex.join(tuple(argv))}")
    return result


def verify_processor_checkpoint(value: dict[str, Any], schema_path: pathlib.Path) -> dict[str, Any]:
    import jsonschema

    if value.get("schema_version") != PROCESSOR_SCHEMA:
        raise PromotionError("processor state uses an unsupported checkpoint schema")
    schema = load_object(schema_path)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)
    unsigned = dict(value)
    claimed = unsigned.pop("checkpoint_sha256", None)
    if claimed != hashlib.sha256(canonical_json(unsigned)).hexdigest():
        raise PromotionError("processor checkpoint digest is invalid")
    generation_inputs = value.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("processor checkpoint lacks generation input provenance")
    if (
        generation_inputs.get("source_id") != value.get("source_id")
        or generation_inputs.get("source_scope") != value.get("source_scope")
    ):
        raise PromotionError("processor checkpoint generation source identity is inconsistent")
    expected_generation_id = hashlib.sha256(canonical_json(generation_inputs)).hexdigest()
    if value.get("generation_id") != expected_generation_id:
        raise PromotionError("processor checkpoint generation id does not bind its immutable inputs")
    return value


def locate_processor_checkpoint(
    state_root: pathlib.Path,
    processor_run_id: str,
    *,
    repository: str,
    workflow_run_id: str,
    artifact_id: str,
    schema_path: pathlib.Path,
) -> tuple[pathlib.Path, dict[str, Any]]:
    source_root = state_root / "sources/data_go_kr/generations"
    matches = []
    for path in sorted(source_root.glob("*.json")):
        checkpoint = verify_processor_checkpoint(load_object(path), schema_path)
        locator = checkpoint.get("output_artifact", {})
        if locator.get("run_id") == workflow_run_id and locator.get("name") == f"{PROCESSING_PREFIX}{processor_run_id}":
            matches.append((path, checkpoint))
    if len(matches) != 1:
        raise PromotionError("processor state does not contain exactly one artifact locator for this workflow attempt")
    path, checkpoint = matches[0]
    locator = checkpoint["output_artifact"]
    if locator.get("repository", "").lower() != repository.lower():
        raise PromotionError("processor artifact locator repository does not match this workflow")
    if str(locator.get("artifact_id")) != str(artifact_id):
        raise PromotionError("processor artifact id differs from the final CAS-published checkpoint")
    if checkpoint.get("status") not in {"ready", "no-change", "retry", "quarantined"}:
        raise PromotionError(f"processor state is not reviewable: {checkpoint.get('status')}")
    if not locator.get("bundle_manifest_sha256"):
        raise PromotionError("processor checkpoint has no output bundle manifest digest")
    return path, checkpoint


def parse_utc_timestamp(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise PromotionError(f"{label} timestamp is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PromotionError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise PromotionError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def processor_attempt_from_locator(checkpoint: Mapping[str, Any]) -> tuple[str, str, str]:
    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise PromotionError("ready processor checkpoint has no artifact locator")
    run_id = str(locator.get("run_id", ""))
    name = str(locator.get("name", ""))
    match = re.fullmatch(r"upstream-catalogue-processing-([0-9]{6,20})-([1-9][0-9]*)", name)
    if not re.fullmatch(r"[0-9]{6,20}", run_id) or not match or match.group(1) != run_id:
        raise PromotionError("ready processor artifact name does not bind one exact run attempt")
    return run_id, match.group(2), name


def validate_trusted_processor_run(
    value: Mapping[str, Any],
    *,
    repository: str,
    run_id: str,
    attempt: str,
    default_branch: str,
    expected_head_sha: str | None = None,
) -> dict[str, Any]:
    """Validate one exact completed B attempt from GitHub's run API."""
    path = str(value.get("path", "")).split("@", 1)[0]
    run_repository = value.get("repository")
    head_repository = value.get("head_repository")
    run_repository_name = run_repository.get("full_name") if isinstance(run_repository, Mapping) else None
    repository_name = head_repository.get("full_name") if isinstance(head_repository, Mapping) else None
    source_sha = str(value.get("head_sha", ""))
    if (
        str(value.get("id", "")) != run_id
        or str(value.get("run_attempt", "")) != attempt
        or value.get("name") != PROCESSOR_WORKFLOW_NAME
        or path != PROCESSOR_WORKFLOW_PATH
        or str(run_repository_name or "").casefold() != repository.casefold()
        or str(repository_name or "").lower() != repository.lower()
        or value.get("head_branch") != default_branch
        or value.get("event") not in {"schedule", "workflow_run", "workflow_dispatch"}
        or value.get("status") != "completed"
        or value.get("conclusion") != "success"
        or not re.fullmatch(r"[a-f0-9]{40}", source_sha)
        or (expected_head_sha is not None and source_sha != expected_head_sha)
    ):
        raise PromotionError("processor run is not the exact trusted successful default-branch attempt")
    return dict(value)


def validate_processor_artifact_metadata(
    artifact: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    repository: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise PromotionError("processor checkpoint has no output artifact locator")
    run_id, _attempt, name = processor_attempt_from_locator(checkpoint)
    actual_run = artifact.get("workflow_run")
    actual_run_id = actual_run.get("id") if isinstance(actual_run, Mapping) else None
    run_head = str(run.get("head_sha", ""))
    if (
        str(locator.get("repository", "")).lower() != repository.lower()
        or str(locator.get("run_id", "")) != run_id
        or str(locator.get("artifact_id", "")) != str(artifact.get("id", ""))
        or str(locator.get("artifact_id", "")) in {"", "None"}
        or artifact.get("name") != name
        or artifact.get("expired") is not False
        or str(actual_run_id) != run_id
        or not isinstance(actual_run, Mapping)
        or actual_run.get("head_sha") != run_head
        or actual_run.get("head_branch") != run.get("head_branch")
    ):
        raise PromotionError("processor artifact metadata differs from the durable exact run locator")
    size = artifact.get("size_in_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1 or size > MAX_PROCESSOR_ARCHIVE_BYTES:
        raise PromotionError("processor artifact archive size is invalid or outside the bounded download limit")
    locator_expiry = parse_utc_timestamp(locator.get("expires_at"), "durable processor artifact expiry")
    actual_expiry = parse_utc_timestamp(artifact.get("expires_at"), "processor artifact expiry")
    current = now or dt.datetime.now(dt.timezone.utc)
    if actual_expiry != locator_expiry or actual_expiry <= current:
        raise PromotionError("processor artifact is expired or its expiry differs from the durable locator")
    digest = artifact.get("digest")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)):
        raise PromotionError("processor artifact archive digest metadata is malformed")
    return dict(artifact)


def list_recoverable_processor_checkpoints(
    state_root: pathlib.Path,
    schema_path: pathlib.Path,
    journal: Mapping[str, Any] | None,
    *,
    now: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """List ordered ready checkpoints and report unusable generations without starving later work."""
    source_root = state_root / "sources/data_go_kr"
    index_path = source_root / "index.json"
    generation_root = source_root / "generations"
    if not index_path.is_file():
        return [], []
    index = load_object(index_path)
    rows = index.get("generations")
    if index.get("schema_version") != PROCESSOR_SCHEMA or not isinstance(rows, list):
        raise PromotionError("durable processor generation index is malformed or unsupported")
    current = now or dt.datetime.now(dt.timezone.utc)
    candidates: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise PromotionError("durable processor generation index contains a malformed row")
        generation_id = row.get("generation_id")
        if not isinstance(generation_id, str) or not re.fullmatch(r"[a-f0-9]{64}", generation_id):
            raise PromotionError("durable processor generation index contains an invalid generation id")
        if generation_id in seen:
            raise PromotionError("durable processor generation index contains a duplicate generation")
        seen.add(generation_id)
        if row.get("status") != "ready":
            continue
        checkpoint_name = row.get("checkpoint")
        if checkpoint_name != f"{generation_id}.json":
            blocked.append({"generation_id": generation_id, "reason": "ready_checkpoint_path_mismatch"})
            continue
        path = generation_root / checkpoint_name
        if not path.is_file() or path.is_symlink():
            blocked.append({"generation_id": generation_id, "reason": "ready_checkpoint_missing"})
            continue
        try:
            checkpoint = verify_processor_checkpoint(load_object(path), schema_path)
        except Exception:
            blocked.append({"generation_id": generation_id, "reason": "ready_checkpoint_invalid"})
            continue
        if checkpoint.get("generation_id") != generation_id or path.stem != generation_id:
            blocked.append({"generation_id": generation_id, "reason": "checkpoint_generation_mismatch"})
            continue
        if checkpoint.get("status") != "ready":
            blocked.append({"generation_id": generation_id, "reason": "checkpoint_status_mismatch"})
            continue
        if checkpoint.get("source_id") != "data_go_kr" or checkpoint.get("source_scope") != "aggregate_supported_catalog":
            continue
        locator = checkpoint.get("output_artifact")
        if not isinstance(locator, Mapping) or not locator.get("artifact_id") or not locator.get("bundle_manifest_sha256"):
            blocked.append({"generation_id": generation_id, "reason": "ready_artifact_locator_incomplete"})
            continue
        try:
            expiry = parse_utc_timestamp(locator.get("expires_at"), "durable processor artifact expiry")
        except PromotionError:
            blocked.append({"generation_id": generation_id, "reason": "ready_artifact_expiry_invalid"})
            continue
        if expiry <= current:
            blocked.append({"generation_id": generation_id, "reason": "ready_artifact_expired"})
            continue
        try:
            processor_attempt_from_locator(checkpoint)
        except PromotionError:
            blocked.append({"generation_id": generation_id, "reason": "ready_artifact_attempt_invalid"})
            continue
        try:
            parse_utc_timestamp(checkpoint.get("observed_at"), "processor observation")
        except PromotionError:
            blocked.append({"generation_id": generation_id, "reason": "processor_observation_time_invalid"})
            continue
        candidates.append(checkpoint)
    if not candidates:
        return [], blocked
    candidates.sort(key=lambda row: (
        parse_utc_timestamp(row.get("observed_at"), "processor observation"),
        str(row.get("generation_id", "")),
    ))
    return candidates, blocked


def select_recoverable_processor_checkpoint(
    state_root: pathlib.Path,
    schema_path: pathlib.Path,
    journal: Mapping[str, Any] | None,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any] | None:
    """Return the oldest locally viable checkpoint (API checks happen before selection)."""
    candidates, _blocked = list_recoverable_processor_checkpoints(state_root, schema_path, journal, now=now)
    return candidates[0] if candidates else None


def verify_processor_input_compatibility(
    root: pathlib.Path,
    checkpoint: Mapping[str, Any],
    processor_head_sha: str,
    current_head_sha: str,
    composition_receipt: Mapping[str, Any] | None = None,
) -> None:
    """Permit an older B run only when its relevant source contract is byte-identical."""
    if not re.fullmatch(r"[a-f0-9]{40}", processor_head_sha) or not re.fullmatch(r"[a-f0-9]{40}", current_head_sha):
        raise PromotionError("processor/current source commits must be full immutable Git SHAs")
    ancestry = command(("git", "merge-base", "--is-ancestor", processor_head_sha, current_head_sha), root, allowed_returncodes=frozenset({0, 1}))
    if ancestry.returncode != 0:
        raise PromotionError("processor source commit is not an ancestor of the current trusted default branch")
    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        raise PromotionError("processor checkpoint lacks generation input provenance")
    historical_bytes: dict[str, bytes] = {}
    for raw_path in dict.fromkeys(PROCESSOR_COMPATIBILITY_FILES):
        path = pathlib.PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts or "\\" in raw_path:
            raise PromotionError("processor compatibility contract contains an unsafe source path")
        try:
            source_bytes = command(("git", "show", f"{processor_head_sha}:{raw_path}"), root).stdout.encode("utf-8")
            current_bytes = (root / raw_path).read_bytes()
        except (OSError, PromotionError) as exc:
            raise PromotionError(f"current main or processor source is missing a compatibility input: {raw_path}") from exc
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        historical_bytes[raw_path] = source_bytes
        current_digest = hashlib.sha256(current_bytes).hexdigest()
        expected_field = next((field for field, input_path in PROCESSOR_INPUT_PROVENANCE.items() if input_path == raw_path), None)
        if expected_field is not None and generation_inputs.get(expected_field) != source_digest:
            raise PromotionError(f"processor checkpoint provenance does not match its source commit: {raw_path}")
        if source_digest != current_digest:
            raise PromotionError(f"processor input contract changed since observation: {raw_path}")
    if composition_receipt is not None:
        input_digests = composition_receipt.get("input_digests")
        if not isinstance(input_digests, Mapping):
            raise PromotionError("composition receipt lacks exact producer input digests")
        for input_name, raw_path in PROCESSOR_COMPOSITION_INPUTS.items():
            observed = input_digests.get(input_name)
            expected_bytes = historical_bytes.get(raw_path)
            if not isinstance(observed, Mapping) or expected_bytes is None:
                raise PromotionError(f"composition receipt is missing its exact {input_name} input digest")
            if (
                observed.get("bytes") != len(expected_bytes)
                or observed.get("sha256") != hashlib.sha256(expected_bytes).hexdigest()
            ):
                raise PromotionError(f"composition receipt {input_name} digest differs from the trusted producer commit")


def gh_rest_json(root: pathlib.Path, endpoint: str, *arguments: str) -> Any:
    argv = (
        "gh", "api", "--header", f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
        "--header", "Accept: application/vnd.github+json", *arguments, endpoint,
    )
    print(f"+ [{root}] {shlex.join(argv)}", flush=True)
    result = subprocess.run(argv, cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        status = re.search(r"\bHTTP\s+(404|410)\b", result.stderr, flags=re.IGNORECASE)
        if status and ("/actions/runs/" in endpoint or "/actions/artifacts" in endpoint):
            raise ProcessorCandidateError("the exact processor Actions run or artifact is no longer available")
        raise PromotionError(f"GitHub REST read failed ({result.returncode})")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PromotionError("GitHub REST endpoint returned invalid JSON") from exc


def gh_rest_bytes(root: pathlib.Path, endpoint: str) -> bytes:
    argv = ("gh", "api", "--header", f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            "--header", "Accept: application/vnd.github+json", endpoint)
    print(f"+ [{root}] {shlex.join(argv)}", flush=True)
    result = subprocess.run(argv, cwd=root, capture_output=True, check=False)
    if result.returncode != 0:
        status = re.search(rb"\bHTTP\s+(404|410)\b", result.stderr, flags=re.IGNORECASE)
        if status:
            raise ProcessorArtifactUnavailable("the exact processor artifact is no longer available")
        raise PromotionError(f"GitHub artifact API download failed ({result.returncode})")
    return result.stdout


def github_api_request(
    method: str,
    endpoint: str,
    body: Mapping[str, Any] | None = None,
) -> tuple[int, Any]:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise PromotionError("GitHub token is unavailable for the exact-head CI operation")
    base_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    url = f"{base_url}/{endpoint.lstrip('/')}"
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            **({"Content-Type": "application/json"} if payload is not None else {}),
        },
    )
    print(f"+ GitHub API {method} {endpoint}", flush=True)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = int(response.status)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PromotionError(f"GitHub API {method} transport failed for {endpoint}") from exc
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None


def github_api_get(endpoint: str) -> dict[str, Any]:
    status, value = github_api_request("GET", endpoint)
    if status != 200 or not isinstance(value, dict):
        raise PromotionError(f"GitHub API read failed with HTTP {status}")
    return value


def verify_release_branch_sha(repository: str, branch: str) -> str | None:
    ref = urllib.parse.quote(branch, safe="/")
    status, value = github_api_request("GET", f"repos/{repository}/git/ref/heads/{ref}")
    if status == 404:
        return None
    if status != 200 or not isinstance(value, Mapping):
        raise PromotionError(f"candidate branch API read failed with HTTP {status}")
    obj = value.get("object")
    sha = obj.get("sha") if isinstance(obj, Mapping) else None
    return str(sha) if isinstance(sha, str) else None


def list_verify_release_runs(
    repository: str,
    workflow_path: str,
    branch: str,
    event: str,
    head_sha: str,
) -> list[dict[str, Any]]:
    workflow = urllib.parse.quote(workflow_path, safe="")
    query = urllib.parse.urlencode({
        "event": event, "head_sha": head_sha, "branch": branch, "per_page": 100,
    })
    value = github_api_get(f"repos/{repository}/actions/workflows/{workflow}/runs?{query}")
    rows = value.get("workflow_runs")
    total = value.get("total_count")
    if (
        not isinstance(rows, list)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or total > 100
        or len(rows) != total
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise PromotionError("matching verify-release run list is malformed or exceeds its bounded result limit")
    return rows


def read_verify_release_run(repository: str, run_id: int) -> dict[str, Any]:
    for _attempt_read in range(2):
        run = github_api_get(f"repos/{repository}/actions/runs/{run_id}")
        current_attempt = run.get("run_attempt")
        if isinstance(current_attempt, bool) or not isinstance(current_attempt, int) or current_attempt < 1:
            raise PromotionError("verify-release run API omitted its exact current attempt")
        jobs_query = urllib.parse.urlencode({"per_page": 100})
        jobs = github_api_get(
            f"repos/{repository}/actions/runs/{run_id}/attempts/{current_attempt}/jobs?{jobs_query}"
        )
        job_rows = jobs.get("jobs")
        job_total = jobs.get("total_count")
        if (
            not isinstance(job_rows, list)
            or isinstance(job_total, bool)
            or not isinstance(job_total, int)
            or job_total < 0
            or job_total > 100
            or len(job_rows) != job_total
            or any(not isinstance(row, dict) for row in job_rows)
        ):
            raise PromotionError("verify-release attempt jobs are malformed or exceed the bounded result limit")
        after_jobs = github_api_get(f"repos/{repository}/actions/runs/{run_id}")
        if after_jobs.get("run_attempt") != current_attempt:
            continue
        after_jobs["jobs"] = job_rows
        return after_jobs
    raise PromotionError("verify-release run attempt changed while its required jobs were being read")


def processor_run_api(root: pathlib.Path, repository: str, run_id: str, attempt: str) -> dict[str, Any]:
    value = gh_rest_json(root, f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}")
    if not isinstance(value, dict):
        raise PromotionError("processor run attempt API returned a malformed value")
    return value


def processor_artifact_api(root: pathlib.Path, repository: str, run_id: str, artifact_id: str) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    total_count: int | None = None
    for page in range(1, MAX_PROCESSOR_ARTIFACT_PAGES + 1):
        payload = gh_rest_json(
            root, f"repos/{repository}/actions/runs/{run_id}/artifacts",
            "--method", "GET", "-F", "per_page=100", "-F", f"page={page}",
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("artifacts"), list)
            or isinstance(payload.get("total_count"), bool)
            or not isinstance(payload.get("total_count"), int)
            or payload["total_count"] < 0
        ):
            raise PromotionError("processor artifact API returned a malformed listing")
        if total_count is None:
            total_count = payload["total_count"]
        elif total_count != payload["total_count"]:
            raise PromotionError("processor artifact API listing changed while reading bounded pages")
        matches.extend(
            row for row in payload["artifacts"]
            if isinstance(row, dict) and str(row.get("id", "")) == artifact_id
        )
        if len(matches) > 1:
            raise PromotionError("processor state resolves to duplicate GitHub artifact ids")
        if page * 100 >= total_count:
            return matches[0] if matches else None
    if total_count is not None and total_count > MAX_PROCESSOR_ARTIFACT_PAGES * 100:
        raise PromotionError("processor artifact API listing exceeded its bounded pagination limit")
    return matches[0] if matches else None


def download_processor_artifact(
    root: pathlib.Path,
    repository: str,
    metadata: Mapping[str, Any],
    output_dir: pathlib.Path,
) -> pathlib.Path:
    artifact_id = str(metadata.get("id", ""))
    raw = gh_rest_bytes(root, f"repos/{repository}/actions/artifacts/{artifact_id}/zip")
    size = metadata.get("size_in_bytes")
    if len(raw) != size:
        raise ProcessorCandidateError("downloaded processor artifact byte count differs from GitHub metadata")
    digest = metadata.get("digest")
    if isinstance(digest, str) and hashlib.sha256(raw).hexdigest() != digest.removeprefix("sha256:"):
        raise ProcessorCandidateError("downloaded processor archive digest differs from GitHub metadata")
    output_dir = output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    archive = output_dir.parent / f"{output_dir.name}.zip"
    archive.write_bytes(raw)
    expected = set(REQUIRED_PROCESSOR_FILES) | {"upstream-catalogue-checkpoint-receipt.json"}
    total = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)) or set(names) != expected:
                raise ProcessorCandidateError("processor artifact does not contain the exact frozen bundle file set")
            for member in members:
                name = member.filename
                path = pathlib.PurePosixPath(name)
                mode = member.external_attr >> 16
                if (
                    path.is_absolute() or ".." in path.parts or "\\" in name
                    or len(path.parts) != 1 or member.is_dir() or stat.S_ISLNK(mode)
                ):
                    raise ProcessorCandidateError("processor artifact contains an unsafe path, directory, or link")
                maximum = 64 * 1024 * 1024 if name == "upstream-catalogue-checkpoint-receipt.json" else 512 * 1024 * 1024
                if member.file_size < 0 or member.file_size > maximum:
                    raise ProcessorCandidateError("processor artifact member exceeds its bounded byte limit")
                total += member.file_size
                if total > MAX_PROCESSOR_BUNDLE_BYTES:
                    raise ProcessorCandidateError("processor artifact expands beyond its bounded bundle limit")
                target = output_dir / name
                target.write_bytes(bundle.read(member))
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ProcessorCandidateError("processor artifact is not a readable immutable ZIP bundle") from exc
    return output_dir


def validate_processor_bundle(
    checkpoint: Mapping[str, Any],
    bundle_dir: pathlib.Path,
    composition_schema: Mapping[str, Any],
    composition_helper: Any,
) -> dict[str, Any]:
    digests = checkpoint.get("output_digests")
    locator = checkpoint.get("output_artifact")
    if not isinstance(digests, list) or not isinstance(locator, dict):
        raise PromotionError("processor checkpoint is missing its output bundle inventory")
    names = [row.get("path") for row in digests if isinstance(row, dict)]
    processor_status = checkpoint.get("status")
    if processor_status in {"ready", "no-change"}:
        if names != list(REQUIRED_PROCESSOR_FILES):
            raise PromotionError("reviewable processor bundle inventory does not contain the exact ordered eight-file contract")
    elif processor_status in {"retry", "quarantined"}:
        if not names or len(names) != len(set(names)) or not set(names).issubset(set(REQUIRED_PROCESSOR_FILES)):
            raise PromotionError("non-candidate bundle inventory is empty, duplicated, or contains an unrecognized output")
    else:
        raise PromotionError(f"processor state is not a terminal no-candidate or reviewable status: {processor_status}")
    if hashlib.sha256(canonical_json(digests)).hexdigest() != locator.get("bundle_manifest_sha256"):
        raise PromotionError("processor output inventory digest differs from its final checkpoint")
    if locator.get("artifact_id") is None:
        raise PromotionError("processor artifact upload has not been bound to the durable checkpoint")
    for record in digests:
        path_name = record["path"]
        relative = pathlib.PurePosixPath(path_name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in path_name:
            raise PromotionError("processor bundle contains an unsafe output path")
        path = bundle_dir / path_name
        if not path.is_file() or path.is_symlink():
            raise PromotionError(f"processor bundle is missing a digest-bound output: {path_name}")
        if (path.stat().st_size, file_sha256(path)) != (record["bytes"], record["sha256"]):
            raise PromotionError(f"processor output differs from its immutable bundle manifest: {path_name}")
    checkpoint_copy = load_object(bundle_dir / "upstream-catalogue-checkpoint-receipt.json")
    if checkpoint_copy.get("generation_id") != checkpoint.get("generation_id"):
        raise PromotionError("uploaded processor checkpoint belongs to another generation")
    unsigned_copy = dict(checkpoint_copy)
    claimed_copy_sha = unsigned_copy.pop("checkpoint_sha256", None)
    if claimed_copy_sha != hashlib.sha256(canonical_json(unsigned_copy)).hexdigest():
        raise PromotionError("uploaded processor checkpoint receipt digest is invalid")
    checkpoint_copy_locator = checkpoint_copy.get("output_artifact")
    if not isinstance(checkpoint_copy_locator, Mapping):
        raise PromotionError("uploaded processor checkpoint receipt has no output artifact locator")
    if checkpoint_copy_locator.get("artifact_id") is not None:
        raise PromotionError("uploaded processor checkpoint already has a bound artifact id")
    copied_expiry = parse_utc_timestamp(checkpoint_copy_locator.get("expires_at"), "uploaded processor artifact expiry")
    final_expiry = parse_utc_timestamp(locator.get("expires_at"), "durable processor artifact expiry")
    copied_heartbeat = parse_utc_timestamp(
        checkpoint_copy.get("last_heartbeat_at"), "uploaded processor checkpoint heartbeat",
    )
    final_heartbeat = parse_utc_timestamp(
        checkpoint.get("last_heartbeat_at"), "durable processor checkpoint heartbeat",
    )
    observation = checkpoint.get("last_observation")
    observed_at = parse_utc_timestamp(
        observation.get("observed_at") if isinstance(observation, Mapping) else checkpoint.get("observed_at"),
        "processor generation observation",
    )
    generation_start_value = checkpoint_copy.get("observed_at") or checkpoint.get("observed_at") or observed_at.isoformat()
    generation_started_at = parse_utc_timestamp(generation_start_value, "processor generation start")
    if copied_heartbeat < generation_started_at or copied_heartbeat > final_heartbeat:
        raise PromotionError("uploaded processor checkpoint heartbeat is outside the durable generation timeline")
    if copied_expiry <= observed_at or copied_expiry > final_expiry:
        raise PromotionError("uploaded checkpoint expiry is not a valid pre-upload timestamp for the final artifact")
    uploaded_normalized = copy.deepcopy(checkpoint_copy)
    durable_normalized = copy.deepcopy(dict(checkpoint))
    uploaded_normalized.pop("checkpoint_sha256", None)
    durable_normalized.pop("checkpoint_sha256", None)
    # The producer changes only these delivery-owned fields after archiving:
    # Actions binds the artifact id/expiry, and an exact idle replay may bump
    # last_heartbeat_at. Every other checkpoint value remains immutable.
    for normalized in (uploaded_normalized, durable_normalized):
        normalized["last_heartbeat_at"] = None
        output_artifact = normalized.get("output_artifact")
        if isinstance(output_artifact, dict):
            output_artifact["artifact_id"] = None
            output_artifact["expires_at"] = None
    if uploaded_normalized != durable_normalized:
        raise PromotionError("uploaded processor checkpoint receipt differs from immutable durable generation state")
    result = load_object(bundle_dir / "upstream-catalogue-processing-result.json")
    if result.get("generation_id") != checkpoint.get("generation_id") or result.get("status") != processor_status:
        raise PromotionError("uploaded processor result does not confirm the exact terminal generation state")
    observation = checkpoint.get("last_observation")
    locator_name = str(locator.get("name", ""))
    locator_processor_id = locator_name.removeprefix(PROCESSING_PREFIX)
    if (
        result.get("source_id") != checkpoint.get("source_id")
        or result.get("producer_run_id") != (observation.get("producer_run_id") if isinstance(observation, dict) else None)
        or result.get("processor_run_id") != locator_processor_id
        or result.get("processor_artifact_run_id") != locator.get("run_id")
        or result.get("processing_replay") is not False
        or result.get("candidate_available") is not (processor_status == "ready")
    ):
        raise PromotionError("uploaded processor result does not bind the checkpoint's exact source and run identities")
    outcome = checkpoint.get("outcome")
    if not isinstance(outcome, dict) or result.get("reason") != outcome.get("reason"):
        raise PromotionError("uploaded processor result reason differs from its durable checkpoint")
    if processor_status in {"retry", "quarantined"}:
        return {
            "status": processor_status,
            "generation_id": checkpoint["generation_id"],
            "reason": result.get("reason"),
        }
    candidate_path = bundle_dir / "composed-candidate.registry.json"
    candidate_sha = file_sha256(candidate_path)
    receipt_path = bundle_dir / "composition-receipt.json"
    composition = load_object(receipt_path)
    generation_inputs = checkpoint.get("generation_inputs", {})
    baseline_sha = generation_inputs.get("baseline_sha256")
    producer_candidate_sha = generation_inputs.get("candidate_sha256")
    input_digests = composition.get("input_digests")
    if not isinstance(input_digests, Mapping):
        raise PromotionError("composition receipt lacks exact baseline and candidate input digests")
    for receipt_key, label, digest in (
        ("baseline", "baseline", baseline_sha),
        ("candidate", "upstream candidate", producer_candidate_sha),
    ):
        observed = input_digests.get(receipt_key)
        if (
            not isinstance(digest, str)
            or not isinstance(observed, Mapping)
            or observed.get("sha256") != digest
        ):
            raise PromotionError(f"composition receipt is not bound to the exact {label} digest in processor state")
    expected_composition_status = "ready_scoped" if processor_status == "ready" else "no_change"
    try:
        composition_helper.validate_composition(
            composition, bundle_dir, composition_schema, candidate_sha,
            expected_status=expected_composition_status,
        )
    except Exception as exc:
        raise PromotionError(f"composer did not admit a valid {expected_composition_status} candidate receipt") from exc
    outcome = checkpoint.get("outcome", {})
    if processor_status == "no-change":
        if candidate_sha != baseline_sha or outcome.get("composer_status") != "no_change" or int(outcome.get("pending_count", -1)) != 0 or int(outcome.get("detail_retry_count", -1)) != 0:
            raise PromotionError("no-change processor proof does not bind an unchanged candidate with zero pending work")
    return {
        "status": processor_status,
        "registry_path": "data/data-go-kr.registry.json",
        "registry_bytes": candidate_path.stat().st_size,
        "registry_sha256": candidate_sha,
        "baseline_sha256": baseline_sha,
        "producer_candidate_sha256": producer_candidate_sha,
        "composition_receipt": composition,
        "composition_receipt_path": str(receipt_path.resolve()),
        "composition_receipt_sha256": file_sha256(receipt_path),
        "composition_outputs_dir": str(bundle_dir.resolve()),
    }


def screen_processor_recovery_candidate(
    root: pathlib.Path,
    repository: str,
    checkpoint: Mapping[str, Any],
    *,
    default_branch: str,
    current_head_sha: str,
    composition_schema: Mapping[str, Any],
    composition_helper: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one durable generation completely before it can occupy this C run."""
    generation_id = str(checkpoint.get("generation_id", ""))
    run_id, attempt, _name = processor_attempt_from_locator(checkpoint)
    try:
        run = processor_run_api(root, repository, run_id, attempt)
    except ProcessorCandidateError:
        return None, "processor_run_unavailable"
    try:
        run = validate_trusted_processor_run(
            run, repository=repository, run_id=run_id, attempt=attempt,
            default_branch=default_branch,
        )
    except PromotionError:
        return None, "processor_run_provenance_invalid"

    artifact_id = str(checkpoint["output_artifact"].get("artifact_id", ""))
    try:
        artifact = processor_artifact_api(root, repository, run_id, artifact_id)
    except ProcessorCandidateError:
        return None, "processor_artifact_listing_unavailable"
    if artifact is None:
        return None, "processor_artifact_unavailable"
    try:
        artifact = validate_processor_artifact_metadata(
            artifact, checkpoint, run, repository=repository,
        )
    except PromotionError:
        return None, "processor_artifact_metadata_invalid_or_expired"

    try:
        bundle_dir = download_processor_artifact(
            root, repository, artifact, root / ".datapan/recovered-processor-output",
        )
    except ProcessorCandidateError:
        return None, "processor_artifact_bundle_invalid"
    try:
        bundle = validate_processor_bundle(checkpoint, bundle_dir, composition_schema, composition_helper)
        verify_processor_input_compatibility(
            root, checkpoint, str(run["head_sha"]), current_head_sha,
            composition_receipt=bundle.get("composition_receipt"),
        )
    except PromotionError:
        return None, "processor_bundle_or_input_contract_incompatible"
    return {
        "run_id": run_id,
        "attempt": attempt,
        "run": run,
        "artifact_id": artifact_id,
        "bundle_dir": bundle_dir,
        "bundle": bundle,
        "generation_id": generation_id,
    }, None


def select_first_eligible_processor_bundle(
    root: pathlib.Path,
    repository: str,
    candidates: Sequence[Mapping[str, Any]],
    blocked: list[dict[str, str]],
    journal: Mapping[str, Any] | None = None,
    *,
    default_branch: str,
    current_head_sha: str,
    composition_schema: Mapping[str, Any],
    composition_helper: Any,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Screen generations in observation order and stop at the first valid bundle."""
    for checkpoint in candidates:
        generation_id = str(checkpoint.get("generation_id", ""))
        screened, reason = screen_processor_recovery_candidate(
            root, repository, checkpoint,
            default_branch=default_branch,
            current_head_sha=current_head_sha,
            composition_schema=composition_schema,
            composition_helper=composition_helper,
        )
        if screened is None:
            blocked.append({"generation_id": generation_id, "reason": str(reason)})
            continue
        bundle = screened.get("bundle", {})
        revision = journal_record_for(
            journal,
            str(checkpoint.get("source_id", "")),
            str(checkpoint.get("source_scope", "")),
            generation_id,
            str(bundle.get("registry_sha256", "")),
        )
        already_active_payload = any(
            isinstance(row, Mapping)
            and row.get("superseded_by") is None
            and row.get("status") == "pending-review"
            and str(row.get("candidate", {}).get("repository", "")).casefold() == repository.casefold()
            and row.get("candidate", {}).get("source_id") == checkpoint.get("source_id")
            and row.get("candidate", {}).get("scope") == checkpoint.get("source_scope")
            and row.get("candidate", {}).get("registry_sha256") == bundle.get("registry_sha256")
            for row in (journal.get("records", []) if isinstance(journal, Mapping) else [])
        )
        if already_active_payload:
            # Another B observation may have a distinct generation while
            # composing the same bytes. It is already represented by this
            # active PR, so don't let that no-op starve a later candidate.
            continue
        if revision is not None and revision.get("superseded_by") is not None:
            continue
        if revision is not None and revision.get("status") != "prepared":
            # Exact payload redelivery is idempotent, even if the producer's
            # heartbeat or Actions artifact locator has advanced.
            continue
        screened["prior_revision"] = revision
        return screened, blocked
    return None, blocked


def registry_sha_from_worktree(root: pathlib.Path) -> tuple[int, str]:
    path = root / "data/data-go-kr.registry.json"
    return registry_sha_from_path(path)


def registry_sha_from_path(path: pathlib.Path) -> tuple[int, str]:
    data = path.read_bytes()
    if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise PromotionError("baseline registry did not materialize from its declared immutable source")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError("baseline registry is not valid JSON") from exc
    if not isinstance(value, list) or not value:
        raise PromotionError("baseline registry is not a nonempty canonical array")
    return len(data), hashlib.sha256(data).hexdigest()


def candidate_generated_file_allowlist(root: pathlib.Path, generation_id: str) -> set[str]:
    allowed = set(GENERATED_FILE_ALLOWLIST)
    allowed.update(
        (REVIEW_DIR / generation_id / name).as_posix()
        for name in ("semantic-diff.json", "regeneration-queue.json", "quarantine.json", "composition-receipt.json")
    )
    allowed.update(
        (pathlib.Path("examples/diagnostic-envelope") / path.name).as_posix()
        for path in (root / "drafts/diagnostic-envelope/fixtures").glob("*.json")
    )
    for plan_name, expected_parent in (
        ("reports/data-go-kr/operation-materialization-plan.json", "reports/data-go-kr/operation-materialization-batches"),
        ("reports/data-go-kr/institution-runtime-plan.json", "reports/data-go-kr/institution-batches"),
    ):
        plan_path = root / plan_name
        if not plan_path.is_file():
            continue
        plan = load_object(plan_path)
        batches = plan.get("batches", [])
        if not isinstance(batches, list):
            raise PromotionError(f"generated batch plan is malformed: {plan_name}")
        for batch in batches:
            if not isinstance(batch, dict) or not isinstance(batch.get("output"), str):
                raise PromotionError(f"generated batch path is malformed: {plan_name}")
            output = pathlib.PurePosixPath(batch["output"])
            if (
                output.parent.as_posix() != expected_parent
                or not re.fullmatch(r"institution-[0-9]{2}\.json", output.name)
            ):
                raise PromotionError(f"generated batch path is outside its declared output inventory: {plan_name}")
            allowed.add(output.as_posix())
    return allowed


def changed_repository_paths(root: pathlib.Path) -> list[str]:
    tracked = command(("git", "diff", "--name-only", "-z", "HEAD"), root).stdout
    untracked = command(("git", "ls-files", "--others", "--exclude-standard", "-z"), root).stdout
    return sorted({path for path in (*tracked.split("\0"), *untracked.split("\0")) if path})


def stage_candidate_outputs(root: pathlib.Path, generation_id: str) -> list[str]:
    changed = changed_repository_paths(root)
    allowed = candidate_generated_file_allowlist(root, generation_id)
    unexpected = sorted(set(changed) - allowed)
    if unexpected:
        raise PromotionError("candidate generation modified files outside its explicit output allowlist: " + ", ".join(unexpected))
    if "data/data-go-kr.registry.json" not in changed or "manifest.json" not in changed:
        raise PromotionError("candidate generation did not update the canonical payload and release manifest")
    if not changed:
        raise PromotionError("candidate generation produced no source changes to stage")
    command(("git", "add", "--", *changed), root)
    staged_raw = command(("git", "diff", "--cached", "--name-only", "-z", "HEAD"), root).stdout
    unstaged_raw = command(("git", "diff", "--name-only", "-z"), root).stdout
    untracked_raw = command(("git", "ls-files", "--others", "--exclude-standard", "-z"), root).stdout
    staged = {path for path in staged_raw.split("\0") if path}
    if staged != set(changed) or unstaged_raw.strip("\0") or untracked_raw.strip("\0"):
        raise PromotionError("explicit candidate staging did not capture exactly the reviewed generated file set")
    return changed


def update_registry_review_artifacts(root: pathlib.Path, generation_id: str, bundle_dir: pathlib.Path) -> pathlib.Path:
    target = root / REVIEW_DIR / generation_id
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or not target.is_dir():
        raise PromotionError("generation review artifact directory is not a regular owned directory")
    allowed = {"semantic-diff.json", "regeneration-queue.json", "quarantine.json", "composition-receipt.json"}
    for child in target.iterdir():
        if child.name not in allowed or child.is_symlink() or not child.is_file():
            raise PromotionError("generation review artifact directory contains an unexpected entry")
    for name in ("semantic-diff.json", "regeneration-queue.json", "quarantine.json", "composition-receipt.json"):
        (target / name).write_bytes((bundle_dir / name).read_bytes())
    return target


def validate_generation_identity(checkpoint: Mapping[str, Any]) -> None:
    generation_id = checkpoint.get("generation_id")
    if not isinstance(generation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", generation_id):
        raise PromotionError("processor generation id is not a safe bounded path/marker identity")


def validate_no_candidate_processor_result(
    bundle_dir: pathlib.Path,
    *,
    repository: str,
    workflow_run_id: str,
    workflow_run_attempt: str,
) -> dict[str, Any] | None:
    """Recognize only a current-run idle/replay result with no candidate bundle."""
    if repository.lower() != "statpan/datapan-registry":
        raise PromotionError("idle processor result repository is outside the admitted source")
    result_path = bundle_dir / "upstream-catalogue-processing-result.json"
    if not result_path.is_file() or result_path.is_symlink():
        return None
    result = load_object(result_path)
    if result.get("status") != "idle":
        return None
    children = list(bundle_dir.iterdir())
    if len(children) != 1 or children[0] != result_path:
        raise PromotionError("idle processor result must be the only file in its immutable run artifact")
    if "generation_id" in result:
        raise PromotionError("idle processor result unexpectedly identifies a candidate generation")
    processor_run_id = f"{workflow_run_id}-{workflow_run_attempt}"
    if result.get("reason") == "exact_producer_delivery_replay":
        expected_keys = {
            "status", "reason", "processing_replay", "candidate_available", "source_id",
            "producer_run_id", "processor_run_id", "processor_artifact_run_id",
        }
        if set(result) != expected_keys:
            raise PromotionError("producer replay result fields differ from the frozen #657 contract")
        if (
            result.get("processing_replay") is not True
            or result.get("candidate_available") is not False
            or result.get("source_id") != "data_go_kr"
            or result.get("processor_run_id") != processor_run_id
            or result.get("processor_artifact_run_id") != workflow_run_id
            or not re.fullmatch(r"[0-9]{6,20}", str(result.get("producer_run_id", "")))
        ):
            raise PromotionError("producer replay result does not bind the exact processor/source identity")
    elif result.get("reason") == "no_active_generation":
        if set(result) != {"status", "reason"}:
            raise PromotionError("empty-queue idle result contains unexpected candidate identity")
    else:
        raise PromotionError("idle processor result reason is not an admitted no-candidate state")
    return {
        "status": "no-candidate",
        "reason": result["reason"],
        "source_id": result.get("source_id", "data_go_kr"),
        "processor_run_id": processor_run_id,
        "candidate_available": False,
    }


def manual_review_status(root: pathlib.Path) -> str:
    report = load_object(root / "reports/credential-runtime-manual-review-acceptance.json")
    summary = report.get("summary")
    if not isinstance(summary, dict):
        raise PromotionError("manual-review acceptance report has no summary object")
    status = summary.get("acceptance_status")
    if status not in {"accepted", "revalidation_required", "unproven"}:
        raise PromotionError("manual-review acceptance report is malformed or has an unsupported status")
    if summary.get("accepted") is not (status == "accepted"):
        raise PromotionError("manual-review acceptance summary does not agree with its current effective status")
    return str(status)


def finish_review_policy_status(root: pathlib.Path) -> str:
    """Return whether this repository explicitly configures a supported Gira policy.

    Gira accepts only ``required`` and ``none``. ``none`` still means a policy
    was configured: it explicitly waives GitHub APPROVED evidence while leaving
    independent Astra review and CI requirements in force. Require one unique
    top-level key so nested ``profiles.*.review_policy`` metadata, duplicate
    YAML keys, malformed documents, and unsupported values fail closed.
    """
    try:
        text = (root / ".gira/config.yaml").read_text(encoding="utf-8")
        document = yaml.compose(text, Loader=yaml.SafeLoader)
        if not isinstance(document, yaml.MappingNode):
            return "unconfigured"
        keys: set[str] = set()
        for key_node, _value_node in document.value:
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag != "tag:yaml.org,2002:str":
                return "unconfigured"
            key = key_node.value
            if key in keys:
                return "unconfigured"
            keys.add(key)
        if "finish_review_policy" not in keys:
            return "unconfigured"
        config = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError):
        return "unconfigured"
    if not isinstance(config, dict):
        return "unconfigured"
    policy = config.get("finish_review_policy")
    if not isinstance(policy, str) or policy.strip().casefold() not in {"none", "required"}:
        return "unconfigured"
    return "configured"


def gh_json(root: pathlib.Path, *arguments: str) -> Any:
    result = command(("gh", *arguments), root)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PromotionError("GitHub CLI returned invalid JSON") from exc


def repository_owner_id(helper: Any, repository: str, source_id: str, scope: str) -> str:
    return helper.owner_id(repository, source_id, scope)


def issue_marker(owner_id: str, generation_id: str) -> str:
    return f"<!-- {PROMOTION_ISSUE_MARKER}{owner_id.removeprefix('datapan-canonical-update:v1:')} generation={generation_id} -->"


def ensure_candidate_issue(
    root: pathlib.Path,
    repository: str,
    candidate: Mapping[str, Any],
    owner: str,
    existing_issue_number: int = 0,
) -> tuple[int, str]:
    """Reuse an issue only when its durable ownership marker matches exactly."""
    marker = issue_marker(owner, str(candidate["generation_id"]))
    issues = gh_json(root, "issue", "list", "--repo", repository, "--state", "all", "--limit", "1000", "--json", "number,title,body,url,state,labels")
    matches = [row for row in issues if isinstance(row.get("body"), str) and marker in row["body"]]
    open_matches = [row for row in matches if str(row.get("state", "")).upper() == "OPEN"]
    if len(open_matches) > 1:
        raise PromotionError("duplicate_candidate_issues: preserve all issues and resolve ownership before retry")
    if existing_issue_number:
        existing = gh_json(root, "issue", "view", str(existing_issue_number), "--repo", repository, "--json", "number,title,body,url,state")
        stable_prefix = f"<!-- {PROMOTION_ISSUE_MARKER}{owner.removeprefix('datapan-canonical-update:v1:')} "
        if existing.get("state") != "OPEN" or not isinstance(existing.get("body"), str) or not existing["body"].startswith(stable_prefix):
            raise PromotionError("durable candidate issue is closed or no longer owned; preserve its history")
        if open_matches and int(open_matches[0].get("number", 0)) != existing_issue_number:
            raise PromotionError("candidate issue differs from the durable open-PR ownership receipt")
        if marker not in existing["body"]:
            body_path = root / ".datapan/candidate-issue.md"
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text("\n".join((
                marker, "", "Parent work: #655", "",
                "This bounded automation issue tracks one owned canonical registry update PR. Review the exact candidate source, scoped composition, generated release evidence, and repository validation before merge. This issue does not authorize merging or Hugging Face publication.", "",
                f"Source: `{candidate['source_id']}`", f"Scope: `{candidate['scope']}`", f"Generation: `{candidate['generation_id']}`", "",
            )), encoding="utf-8")
            command(("gh", "issue", "edit", str(existing_issue_number), "--repo", repository,
                     "--title", f"Review canonical registry update: {candidate['source_id']} ({candidate['generation_id']})",
                     "--body-file", str(body_path)), root)
        return existing_issue_number, str(existing.get("url", ""))
    if open_matches:
        issue = open_matches[0]
        return int(issue["number"]), str(issue.get("url", ""))

    body = "\n".join((
        marker,
        "",
        "Parent work: #655",
        "",
        "This bounded automation issue tracks one owned canonical registry update PR. Review the exact candidate source, scoped composition, generated release evidence, and repository validation before merge. This issue does not authorize merging or Hugging Face publication.",
        "",
        f"Source: `{candidate['source_id']}`",
        f"Scope: `{candidate['scope']}`",
        f"Generation: `{candidate['generation_id']}`",
    ))
    body_path = root / ".datapan/candidate-issue.md"
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(body + "\n", encoding="utf-8")
    title = f"Review canonical registry update: {candidate['source_id']} ({candidate['generation_id']})"
    output = command((
        "gh", "issue", "create", "--repo", repository, "--title", title,
        "--body-file", str(body_path), "--label", "type:task", "--label", "status:ready", "--label", "priority:p1",
    ), root)
    url = output.stdout.strip().splitlines()[-1] if output.stdout.strip() else ""
    match = re.search(r"/issues/(\d+)$", url)
    if not match:
        # A create response can be lost after the server committed. Resolve by
        # the stable marker before any retry can create another issue.
        reread = gh_json(root, "issue", "list", "--repo", repository, "--state", "all", "--limit", "1000", "--json", "number,title,body,url,state,labels")
        recovered = [
            row for row in reread
            if isinstance(row.get("body"), str)
            and marker in row["body"]
            and str(row.get("state", "")).upper() == "OPEN"
        ]
        if len(recovered) != 1 or recovered[0].get("state") != "OPEN":
            raise PromotionError("candidate issue creation lacks an authoritative marker read-back")
        return int(recovered[0]["number"]), str(recovered[0].get("url", ""))
    return int(match.group(1)), url


def gh_open_prs(root: pathlib.Path, repository: str) -> list[dict[str, Any]]:
    values = gh_json(root, "pr", "list", "--repo", repository, "--state", "all", "--limit", "1000", "--json", "number,url,state,body,headRefName,headRefOid,baseRefName,mergeCommit")
    rows: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        body = value.get("body") if isinstance(value.get("body"), str) else ""
        owner_match = re.search(r"<!-- (datapan-canonical-update:v1:[a-f0-9]{64}) generation=([^ ]+) -->", body)
        rows.append({
            "number": int(value.get("number", 0)),
            "url": str(value.get("url", "")),
            "state": str(value.get("state", "")).lower(),
            "body": body,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "head_sha": value.get("headRefOid"),
            "head_ref": value.get("headRefName"),
            "base_ref": value.get("baseRefName"),
            "merge_commit_sha": (value.get("mergeCommit") or {}).get("oid") if isinstance(value.get("mergeCommit"), dict) else None,
            "owner_id": owner_match.group(1) if owner_match else "",
            "generation_id": owner_match.group(2) if owner_match else "",
        })
    return rows


def gh_pr_readback(root: pathlib.Path, repository: str, number: int) -> dict[str, Any]:
    value = gh_json(root, "pr", "view", str(number), "--repo", repository, "--json", "number,url,state,body,headRefName,headRefOid,baseRefName,mergeCommit")
    pull = gh_rest_json(root, f"repos/{repository}/pulls/{number}", "--method", "GET")
    base = pull.get("base") if isinstance(pull, Mapping) else None
    head = pull.get("head") if isinstance(pull, Mapping) else None
    base_repository = base.get("repo") if isinstance(base, Mapping) else None
    head_repository = head.get("repo") if isinstance(head, Mapping) else None
    authoritative_base = base_repository.get("full_name") if isinstance(base_repository, Mapping) else None
    authoritative_head = head_repository.get("full_name") if isinstance(head_repository, Mapping) else None
    return {
        "number": value.get("number"), "url": value.get("url"), "state": value.get("state"),
        "body": value.get("body"), "headRefName": value.get("headRefName"),
        "headRefOid": value.get("headRefOid"), "baseRefName": value.get("baseRefName"),
        "mergeCommit": value.get("mergeCommit"),
        "repository": authoritative_base,
        "headRepository": authoritative_head,
    }


def promotion_journal_worktree(root: pathlib.Path, path: pathlib.Path, source_base_sha: str) -> tuple[str | None, bool]:
    """Create an isolated worktree at the exact currently observed journal ref."""
    module = load_module(root / "scripts/materialize-canonical-registry.py", "journal_git_helper")
    ref = f"refs/heads/{STATE_BRANCH}"
    current = pr_helper_remote_sha(module, root, ref)
    if current:
        try:
            module.git_output(["fetch", "--no-tags", "origin", f"+{ref}:refs/remotes/origin/canonical-update-state"], root, availability=True)
        except module.AvailabilityError as exc:
            raise PromotionError("promotion state branch could not be fetched for compare-and-swap") from exc
        base = "refs/remotes/origin/canonical-update-state"
    else:
        base = source_base_sha
    result = subprocess.run(("git", "worktree", "add", "--detach", str(path), base), cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise PromotionError("promotion state worktree could not be created from the observed state branch")
    return current, current is not None


def pr_helper_remote_sha(module: Any, root: pathlib.Path, ref: str) -> str | None:
    try:
        result = module.git_output(["ls-remote", "--heads", "origin", ref], root, availability=True)
    except module.AvailabilityError as exc:
        raise PromotionError("promotion state branch read failed") from exc
    matches = [line.split("\t", 1)[0] for line in result.decode("ascii", errors="replace").splitlines() if line.endswith("\t" + ref)]
    if len(matches) > 1:
        raise PromotionError("promotion state branch returned duplicate refs")
    return matches[0] if matches else None


def persist_journal_record(
    root: pathlib.Path,
    source_base_sha: str,
    receipt: Mapping[str, Any],
    *,
    observed_at: str,
    expected_ci: Any = CI_EXPECTATION_UNSET,
    supersede_from: Mapping[str, Any] | None = None,
) -> None:
    helper = load_module(root / "scripts/canonical_update_pr.py", "canonical_update_journal_helper")
    schema = load_object(root / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json")
    path = root / ".datapan/promotion-state-worktree"
    if path.exists():
        subprocess.run(("git", "worktree", "remove", "--force", str(path)), cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        shutil.rmtree(path, ignore_errors=True)
    old_sha, _ = promotion_journal_worktree(root, path, source_base_sha)
    try:
        journal_path = path / JOURNAL_PATH
        journal = None
        if journal_path.is_file():
            journal = load_object(journal_path)
            helper.validate_journal(journal, schema)
        if expected_ci is not CI_EXPECTATION_UNSET:
            try:
                helper.assert_ci_compare_and_swap(journal, receipt, expected_ci)
            except helper.AdmissionError as exc:
                raise PromotionError(str(exc)) from exc
        updated = helper.append_journal_record(
            journal, receipt, repository=str(receipt["candidate"]["repository"]),
            observed_at=observed_at, supersede_from=supersede_from,
        )
        helper.validate_journal(updated, schema)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_bytes(json.dumps(updated, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        command(("git", "-C", str(path), "add", "--", JOURNAL_PATH.as_posix()), root)
        staged = command(("git", "-C", str(path), "diff", "--cached", "--quiet"), root, allowed_returncodes=frozenset({0, 1}))
        if staged.returncode == 0:
            return
        command(("git", "-C", str(path), "-c", "user.name=datapan-canonical-update[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-m", "Record canonical update promotion acknowledgement"), root)
        new_sha = command(("git", "-C", str(path), "rev-parse", "HEAD"), root).stdout.strip()
        ref = f"refs/heads/{STATE_BRANCH}"
        lease = f"--force-with-lease={ref}:{old_sha or ''}"
        command(("git", "-C", str(path), "push", "--no-verify", lease, "origin", f"{new_sha}:{ref}"), root)
        observed = pr_helper_remote_sha(helper.load_materializer(root), root, ref)
        if observed != new_sha:
            raise PromotionError("promotion state branch read-back differs from the exact journal commit")
    finally:
        subprocess.run(("git", "worktree", "remove", "--force", str(path)), cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def load_promotion_journal(root: pathlib.Path) -> dict[str, Any] | None:
    helper = load_module(root / "scripts/canonical_update_pr.py", "canonical_update_journal_reader")
    module = helper.load_materializer(root)
    ref = f"refs/heads/{STATE_BRANCH}"
    sha = pr_helper_remote_sha(module, root, ref)
    if not sha:
        return None
    try:
        module.git_output(["fetch", "--no-tags", "origin", f"+{ref}:refs/remotes/origin/canonical-update-state"], root, availability=True)
    except module.AvailabilityError as exc:
        raise PromotionError("promotion state branch could not be fetched") from exc
    try:
        raw = module.git_output(["show", f"{sha}:{JOURNAL_PATH.as_posix()}"], root)
    except module.IntegrityError:
        return None
    try:
        journal = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PromotionError("promotion state journal is malformed JSON") from exc
    helper.validate_journal(journal, load_object(root / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json"))
    return journal


def ensure_verify_release_ci(root: pathlib.Path, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Reconcile or dispatch the exact owned PR head and persist every CI step to its journal row."""
    module = load_module(root / "scripts/canonical_update_ci.py", "canonical_update_ci")
    repository = str(receipt.get("candidate", {}).get("repository", ""))
    candidate = receipt.get("candidate", {})
    source_id = str(candidate.get("source_id", ""))
    scope = str(candidate.get("scope", ""))
    generation_id = str(candidate.get("generation_id", ""))
    registry_sha256 = str(candidate.get("registry_sha256", ""))
    current_journal = load_promotion_journal(root)
    current = journal_record_for(current_journal, source_id, scope, generation_id, registry_sha256)
    if current is None:
        raise PromotionError("owned PR has no durable promotion receipt before verify-release CI")
    if current.get("superseded_by") is not None:
        raise PromotionError("verify-release CI cannot gate a superseded candidate revision")
    if current.get("status") not in {"prepared", "pending-review"} or current.get("pr", {}).get("number", 0) < 1:
        raise PromotionError("verify-release CI is limited to a durably owned active candidate PR")
    expected_ci = json.loads(json.dumps(current.get("ci"))) if current.get("ci") is not None else None
    source_base_sha = command(("git", "rev-parse", "HEAD"), root).stdout.strip()

    def persist_ci(entry: dict[str, Any]) -> None:
        nonlocal expected_ci
        latest_journal = load_promotion_journal(root)
        latest = journal_record_for(latest_journal, source_id, scope, generation_id, registry_sha256)
        if latest is None:
            raise PromotionError("verify-release CI candidate disappeared from the durable state branch")
        updated = json.loads(json.dumps(latest))
        updated["ci"] = json.loads(json.dumps(entry))
        persist_journal_record(
            root, source_base_sha, updated,
            observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            expected_ci=expected_ci,
        )
        expected_ci = json.loads(json.dumps(entry))

    def dispatch(branch: str, inputs: Mapping[str, str]) -> tuple[int | None, Mapping[str, Any] | None]:
        workflow = urllib.parse.quote(VERIFY_WORKFLOW_PATH, safe="")
        status, body = github_api_request("POST", f"repos/{repository}/actions/workflows/{workflow}/dispatches", {
            "ref": branch,
            "inputs": dict(inputs),
        })
        return status, body if isinstance(body, Mapping) else None

    result = module.ensure_verify_release_run(
        repository,
        current,
        lambda: gh_pr_readback(root, repository, int(current["pr"]["number"])),
        expected_ci,
        read_branch_sha=lambda branch: verify_release_branch_sha(repository, branch),
        list_matching_runs=list_verify_release_runs,
        dispatch=dispatch,
        read_run=lambda run_id: read_verify_release_run(repository, run_id),
        persist=persist_ci,
    )
    return result


def active_owned_pr(journal: Mapping[str, Any] | None, github_prs: Sequence[Mapping[str, Any]], helper: Any, candidate: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(journal, Mapping):
        related_github = [row for row in github_prs if row.get("owner_id") == helper.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"]) and row.get("state") == "open"]
        if related_github:
            raise PromotionError("open_candidate_pr_without_durable_owner_receipt: preserve it and investigate state branch")
        return [], 0
    owner = helper.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"])
    records = [
        row for row in journal.get("records", [])
        if row.get("ownership", {}).get("owner_id") == owner
        and row.get("superseded_by") is None
        and row.get("status") in {"prepared", "pending-review"}
        and row.get("pr", {}).get("number", 0) > 0
    ]
    active = []
    for record in records:
        number = int(record["pr"]["number"])
        matches = [row for row in github_prs if int(row.get("number", 0)) == number]
        if len(matches) != 1:
            raise PromotionError("durable candidate PR is not present in authoritative GitHub read-back")
        observed = matches[0]
        if observed.get("state") != "open":
            continue
        expected_head = record["candidate"]["head_sha"]
        expected_body = record["ownership"]["body_sha256"]
        if observed.get("head_sha") != expected_head:
            raise PromotionError("human_head_change: preserve the owned PR branch and stop")
        if observed.get("body_sha256") != expected_body:
            raise PromotionError("human_body_change: preserve the owned PR body and stop")
        if observed.get("owner_id") != owner:
            raise PromotionError("PR owner marker differs from durable promotion journal")
        active.append({
            "repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"],
            "state": "open", "owner_id": owner, "number": number,
            "head_sha": observed["head_sha"], "automation_head_sha": expected_head,
            "body_sha256": observed["body_sha256"], "automation_body_sha256": expected_body,
            "candidate_head_sha": record["candidate"]["head_sha"],
            "manifest_sha256": record["candidate"]["manifest_sha256"],
            "registry_sha256": record["candidate"]["registry_sha256"],
        })
    if len(active) > 1:
        raise PromotionError("duplicate_open_prs: preserve all candidate branches and resolve ownership")
    issue = int(active[0].get("issue_number", 0)) if active else 0
    if active:
        source_record = next(row for row in records if int(row["pr"]["number"]) == active[0]["number"])
        issue = int(source_record.get("ownership", {}).get("issue_number", 0))
    return active, issue


def run_bound_validation(root: pathlib.Path, datapan_cli: pathlib.Path, candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    commands_by_check = {
        "registry_manifest": [
            ("python3", "scripts/sync-release-schema-artifacts.py", "--check"),
            ("python3", "scripts/sync-release-manifest-artifacts.py", "--check"),
        ],
        "denominator": [
            ("python3", "scripts/validate-data-go-kr-operation-manifest.py"),
            ("python3", "scripts/generate-operation-denominator-rollup.py", "--check"),
        ],
        "adapter": [
            ("python3", "scripts/validate-external-adapter-backlog.py"),
            ("python3", "scripts/validate-source-profiles.py"),
        ],
        "ledger": [("python3", "scripts/refresh-release-ledger-evidence.py", "--check")],
        "diagnostic_current_source_applicability": [
            ("python3", "scripts/generate-diagnostic-current-source-applicability.py", "--check"),
            ("python3", "scripts/validate-diagnostic-current-source-applicability.py"),
        ],
        "release": [
            ("python3", "scripts/validate-release-report-artifacts.py"),
            ("python3", "scripts/validate-release-receipt-boundary.py"),
            ("python3", "scripts/generate-diagnostic-publication.py", "--check"),
        ],
        "consumer": [("python3", "scripts/validate-release-consumer-compatibility.py")],
    }
    evidence: list[dict[str, Any]] = [{
        "name": "composition", "source_sha": candidate["head_sha"], "manifest_sha256": candidate["manifest_sha256"],
        "command": "#656 receipt schema + exact bundle digest/arithmetic validation", "exit_code": 0,
    }]
    for name, commands in commands_by_check.items():
        for argv in commands:
            completed = command(argv, root)
            evidence.append({
                "name": name, "source_sha": candidate["head_sha"], "manifest_sha256": candidate["manifest_sha256"],
                "command": shlex.join(argv), "exit_code": completed.returncode,
            })
    for argv in (
        ("python3", "scripts/validate-credential-runtime-manual-review-decision.py"),
        ("python3", "scripts/generate-credential-runtime-manual-review-acceptance.py", "--check"),
    ):
        completed = command(argv, root)
        evidence.append({
            "name": "manual_review_acceptance", "source_sha": candidate["head_sha"],
            "manifest_sha256": candidate["manifest_sha256"], "command": shlex.join(argv),
            "exit_code": completed.returncode,
        })
    return evidence


def refresh_pr_phase(
    target: Mapping[str, Any],
    predecessor: Mapping[str, Any],
    observed: Mapping[str, Any],
    helper: Any,
    *,
    require_repository: bool = True,
) -> str:
    """Classify only the three durable states of an owned PR refresh."""
    target_candidate = target.get("candidate", {})
    target_owner = target.get("ownership", {})
    target_pr = target.get("pr", {})
    old_candidate = predecessor.get("candidate", {})
    old_owner = predecessor.get("ownership", {})
    old_pr = predecessor.get("pr", {})
    if target.get("refresh_from") != helper.revision_reference(predecessor):
        raise PromotionError("prepared refresh intent does not bind its exact predecessor head/body")
    if (
        target_owner.get("owner_id") != old_owner.get("owner_id")
        or target_owner.get("branch") != old_owner.get("branch")
        or target_owner.get("issue_number") != old_owner.get("issue_number")
        or target_pr.get("number") != old_pr.get("number")
        or target_pr.get("number", 0) < 1
    ):
        raise PromotionError("prepared refresh intent changed the owned issue, branch, or PR")
    if require_repository and (
        str(observed.get("repository", "")).casefold() != str(target_candidate.get("repository", "")).casefold()
        or str(observed.get("headRepository", "")).casefold() != str(target_candidate.get("repository", "")).casefold()
    ):
        raise PromotionError("owned PR base/head repository read-back differs from the candidate repository")
    if (
        str(observed.get("state", "")).upper() != "OPEN"
        or int(observed.get("number", 0)) != int(target_pr.get("number", 0))
        or observed.get("headRefName") != target_owner.get("branch")
        or observed.get("baseRefName") != "main"
    ):
        raise PromotionError("owned PR branch/base/number changed during candidate refresh")
    body = observed.get("body")
    if not isinstance(body, str):
        raise PromotionError("owned PR refresh read-back has no body")
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    claimed_body_sha = observed.get("body_sha256")
    if claimed_body_sha is not None and claimed_body_sha != body_sha:
        raise PromotionError("owned PR body digest differs from its API read-back")
    target_body = target_owner.get("body")
    if not isinstance(target_body, str) or hashlib.sha256(target_body.encode("utf-8")).hexdigest() != target_owner.get("body_sha256"):
        raise PromotionError("prepared refresh intent lacks its exact durable target body")
    owner = str(target_owner.get("owner_id", ""))
    old_marker = helper.body_marker(owner, str(old_candidate.get("generation_id", "")))
    new_marker = helper.body_marker(owner, str(target_candidate.get("generation_id", "")))
    if old_marker not in body and new_marker not in body:
        raise PromotionError("owned PR body no longer has either exact refresh owner marker")
    observed_head = str(observed.get("headRefOid", ""))
    old_head = str(old_candidate.get("head_sha", ""))
    target_head = str(target_candidate.get("head_sha", ""))
    old_body_sha = str(old_owner.get("body_sha256", ""))
    new_body_sha = str(target_owner.get("body_sha256", ""))
    if observed_head == old_head and body_sha == old_body_sha and old_marker in body:
        return "before-push"
    if observed_head == target_head and body_sha == old_body_sha and old_marker in body:
        return "after-push-before-body"
    if observed_head == target_head and body_sha == new_body_sha and new_marker in body:
        return "after-body-edit"
    raise PromotionError("human_head_change_or_body_change: preserve the owned PR and stop refresh recovery")


def validate_exact_open_pr(receipt: Mapping[str, Any], observed: Mapping[str, Any], helper: Any) -> None:
    candidate = receipt.get("candidate", {})
    ownership = receipt.get("ownership", {})
    pr = receipt.get("pr", {})
    body = observed.get("body")
    if (
        str(observed.get("repository", "")).casefold() != str(candidate.get("repository", "")).casefold()
        or str(observed.get("headRepository", "")).casefold() != str(candidate.get("repository", "")).casefold()
        or str(observed.get("state", "")).upper() != "OPEN"
        or int(observed.get("number", 0)) != int(pr.get("number", 0))
        or observed.get("headRefName") != ownership.get("branch")
        or observed.get("baseRefName") != "main"
        or observed.get("headRefOid") != candidate.get("head_sha")
        or not isinstance(body, str)
        or hashlib.sha256(body.encode("utf-8")).hexdigest() != ownership.get("body_sha256")
        or (isinstance(ownership.get("body"), str) and body != ownership.get("body"))
        or helper.body_marker(str(ownership.get("owner_id", "")), str(candidate.get("generation_id", ""))) not in body
    ):
        raise PromotionError("human_head_change_or_body_change: preserve the owned PR and stop")


def validate_existing_pr_api_readback(
    observed: Mapping[str, Any],
    journal: Mapping[str, Any] | None,
    existing: Sequence[Mapping[str, Any]],
    helper: Any,
) -> None:
    """Bind an existing PR to its exact durable revision before any candidate upload."""
    if len(existing) != 1 or not isinstance(journal, Mapping):
        raise PromotionError("owned PR read-back has no unique durable revision receipt")
    reference = existing[0].get("revision_ref")
    if not isinstance(reference, Mapping):
        raise PromotionError("owned PR list result has no exact durable revision identity")
    receipt = journal_record_for(
        journal,
        str(reference.get("source_id", "")),
        str(reference.get("scope", "")),
        str(reference.get("generation_id", "")),
        str(reference.get("registry_sha256", "")),
    )
    if receipt is None or helper.revision_reference(receipt) != dict(reference):
        raise PromotionError("owned PR read-back revision differs from its durable journal identity")
    if int(receipt.get("pr", {}).get("number", 0)) == 0:
        candidate = receipt.get("candidate", {})
        ownership = receipt.get("ownership", {})
        body = observed.get("body")
        if (
            str(observed.get("repository", "")).casefold() != str(candidate.get("repository", "")).casefold()
            or str(observed.get("headRepository", "")).casefold() != str(candidate.get("repository", "")).casefold()
            or str(observed.get("state", "")).upper() != "OPEN"
            or int(observed.get("number", 0)) < 1
            or observed.get("headRefName") != ownership.get("branch")
            or observed.get("baseRefName") != "main"
            or observed.get("headRefOid") != candidate.get("head_sha")
            or not isinstance(body, str)
            or hashlib.sha256(body.encode("utf-8")).hexdigest() != ownership.get("body_sha256")
            or (isinstance(ownership.get("body"), str) and body != ownership.get("body"))
            or helper.body_marker(str(ownership.get("owner_id", "")), str(candidate.get("generation_id", ""))) not in body
        ):
            raise PromotionError("interrupted PR creation read-back differs from its exact prepared owner/head/body")
        return
    if isinstance(receipt.get("refresh_from"), Mapping):
        validate_exact_open_pr(receipt, observed, helper)
        return
    intents = [
        row for row in journal.get("records", [])
        if isinstance(row, Mapping)
        and row.get("status") == "prepared"
        and row.get("superseded_by") is None
        and row.get("refresh_from") == dict(reference)
        and int(row.get("pr", {}).get("number", 0)) == int(receipt.get("pr", {}).get("number", 0))
    ]
    if len(intents) > 1:
        raise PromotionError("multiple prepared refresh intents claim one owned PR head")
    if not intents:
        validate_exact_open_pr(receipt, observed, helper)
        return
    refresh_pr_phase(intents[0], receipt, observed, helper)


def validate_prepared_create_pr_readback(
    receipt: Mapping[str, Any],
    pull_requests: Sequence[Mapping[str, Any]],
    observed: Mapping[str, Any],
    remote_branch_sha: str | None,
    helper: Any,
    *,
    repository: str,
    source_id: str,
    scope: str,
    generation_id: str,
    registry_path: str,
    registry_bytes: int,
    registry_sha256: str,
    composition_receipt_sha256: str,
) -> int:
    """Require one exact open PR, remote branch, and preserved prepared receipt."""
    candidate = receipt.get("candidate")
    ownership = receipt.get("ownership")
    pr_record = receipt.get("pr")
    if (
        receipt.get("status") != "prepared"
        or receipt.get("action") != "create"
        or receipt.get("refresh_from") is not None
        or not isinstance(candidate, Mapping)
        or not isinstance(ownership, Mapping)
        or not isinstance(pr_record, Mapping)
        or isinstance(pr_record.get("number"), bool)
        or pr_record.get("number") != 0
        or pr_record.get("state") != "missing"
    ):
        raise PromotionError("prepared PR recovery requires the original create intent with PR number zero")

    expected_candidate = {
        "repository": repository,
        "source_id": source_id,
        "scope": scope,
        "generation_id": generation_id,
        "registry_path": registry_path,
        "registry_bytes": registry_bytes,
        "registry_sha256": registry_sha256,
        "composition_receipt_sha256": composition_receipt_sha256,
    }
    if any(candidate.get(key) != value for key, value in expected_candidate.items()):
        raise PromotionError("prepared PR recovery candidate differs from the trusted processor bundle")
    for name in ("base_sha", "head_sha"):
        if (
            not isinstance(candidate.get(name), str)
            or not re.fullmatch(r"[a-f0-9]{40}", str(candidate.get(name)))
            or candidate.get(name) == "0" * 40
        ):
            raise PromotionError(f"prepared PR recovery candidate has an invalid {name}")
    for name in ("manifest_sha256", "registry_sha256", "composition_receipt_sha256"):
        if not isinstance(candidate.get(name), str) or not re.fullmatch(r"[a-f0-9]{64}", str(candidate.get(name))):
            raise PromotionError(f"prepared PR recovery candidate has an invalid {name}")
    if isinstance(candidate.get("registry_bytes"), bool) or not isinstance(candidate.get("registry_bytes"), int) or candidate["registry_bytes"] < 1:
        raise PromotionError("prepared PR recovery candidate has an invalid registry byte count")

    try:
        expected_owner = helper.owner_id(repository, source_id, scope)
        expected_branch = helper.automation_branch(candidate, "create")
    except Exception as exc:  # noqa: BLE001 - malformed durable ownership must fail closed
        raise PromotionError("prepared PR recovery ownership identity is invalid") from exc
    owner = ownership.get("owner_id")
    branch = ownership.get("branch")
    issue_number = ownership.get("issue_number")
    expected_head = ownership.get("expected_head_sha")
    if (
        owner != expected_owner
        or branch != expected_branch
        or expected_head not in {"0" * 40, candidate.get("head_sha")}
        or isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number < 1
        or ownership.get("issue_url") != f"https://github.com/{repository}/issues/{issue_number}"
    ):
        raise PromotionError("prepared PR recovery owner, branch, expected head, or issue identity differs")

    body = ownership.get("body")
    body_sha = ownership.get("body_sha256")
    if (
        not isinstance(body, str)
        or not isinstance(body_sha, str)
        or not re.fullmatch(r"[a-f0-9]{64}", body_sha)
        or hashlib.sha256(body.encode("utf-8")).hexdigest() != body_sha
    ):
        raise PromotionError("prepared PR recovery body bytes or digest are invalid")
    required_body_lines = (
        helper.body_marker(expected_owner, generation_id),
        f"- Source: `{source_id}`; scope: `{scope}`",
        f"- Candidate base commit: `{candidate['base_sha']}`",
        f"- Registry artifact: `{registry_path}` ({registry_bytes} bytes, sha256 `{registry_sha256}`)",
        f"- Manifest sha256: `{candidate['manifest_sha256']}`",
        f"- Composition receipt sha256: `{composition_receipt_sha256}`",
        "- Full-scope freshness: `false`; publication allowed: `false`",
        f"Closes #{issue_number}",
    )
    body_lines = set(body.splitlines())
    close_refs = re.findall(r"(?im)^[ \t]*(?:Closes|Fixes|Resolves)[ \t]+#([1-9][0-9]*)[ \t]*$", body)
    if any(line not in body_lines for line in required_body_lines) or close_refs != [str(issue_number)]:
        raise PromotionError("prepared PR recovery body does not bind the exact candidate and issue")
    if helper.body_marker(expected_owner, generation_id) not in body:
        raise PromotionError("prepared PR recovery body is missing the exact owner and generation marker")

    payload = candidate.get("payload_readback")
    if not isinstance(payload, Mapping) or any(payload.get(key) != value for key, value in {
        "status": "verified",
        "provider": "github-git-lfs",
        "repository": repository,
        "remote": "origin",
        "source_sha": candidate["head_sha"],
        "manifest_sha256": candidate["manifest_sha256"],
        "path": registry_path,
        "bytes": registry_bytes,
        "sha256": registry_sha256,
        "lfs_oid": registry_sha256,
        "readback": "isolated_lfs_storage_verified",
        "policy_path": "policy/registry-distribution.json",
    }.items()):
        raise PromotionError("prepared PR recovery lost the exact verified Git LFS payload proof")
    if (
        isinstance(payload.get("policy_bytes"), bool)
        or not isinstance(payload.get("policy_bytes"), int)
        or payload["policy_bytes"] < 1
        or not isinstance(payload.get("policy_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", payload["policy_sha256"])
        or not isinstance(payload.get("observed_at"), str)
        or not payload["observed_at"]
    ):
        raise PromotionError("prepared PR recovery has an invalid Git LFS policy proof")

    checks = receipt.get("checks")
    if not isinstance(checks, Mapping) or any(checks.get(name) != "passed" for name in helper.REQUIRED_CHECKS):
        raise PromotionError("prepared PR recovery has incomplete source validation checks")
    manual_review = candidate.get("manual_review_acceptance_status")
    if manual_review not in helper.MANUAL_REVIEW_STATES or checks.get("manual_review_acceptance") != manual_review:
        raise PromotionError("prepared PR recovery changed the preserved manual-review status")
    applicability = checks.get("diagnostic_current_source_applicability")
    if applicability not in {"historical_scope_unchanged", "revalidation_required"}:
        raise PromotionError("prepared PR recovery has an invalid current-source applicability status")
    source_refresh = receipt.get("source_refresh_evidence")
    if not isinstance(source_refresh, Mapping) or any(source_refresh.get(key) != value for key, value in {
        "schema_version": "datapan.canonical-source-refresh-evidence.v1",
        "registry_path": registry_path,
        "registry_bytes": registry_bytes,
        "registry_sha256": registry_sha256,
        "manifest_sha256": candidate["manifest_sha256"],
    }.items()):
        raise PromotionError("prepared PR recovery lost the exact source-refresh validation receipt")
    source_applicability = source_refresh.get("diagnostic_current_source_applicability")
    if not isinstance(source_applicability, Mapping) or source_applicability.get("status") != applicability:
        raise PromotionError("prepared PR recovery changed source-applicability evidence")
    source_commands = source_refresh.get("commands")
    if not isinstance(source_commands, list) or len(source_commands) != 9 or any(
        not isinstance(item, Mapping)
        or item.get("exit_code") != 0
        or item.get("input_registry_sha256") != registry_sha256
        for item in source_commands
    ):
        raise PromotionError("prepared PR recovery has incomplete source-refresh command evidence")
    evidence = receipt.get("validation_evidence")
    required_evidence = set((*helper.REQUIRED_CHECKS, "manual_review_acceptance", "diagnostic_current_source_applicability"))
    if not isinstance(evidence, list) or any(
        not isinstance(item, Mapping)
        or item.get("exit_code") != 0
        or item.get("source_sha") != candidate["head_sha"]
        or item.get("manifest_sha256") != candidate["manifest_sha256"]
        for item in evidence
    ) or not required_evidence.issubset({
        item.get("name") for item in evidence
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }):
        raise PromotionError("prepared PR recovery lost the exact-head source-validation command evidence")

    related_open = [
        row for row in pull_requests
        if str(row.get("state", "")).upper() == "OPEN"
        and (
            row.get("owner_id") == expected_owner
            or row.get("generation_id") == generation_id
            or row.get("head_ref") == expected_branch
        )
    ]
    if len(related_open) != 1:
        raise PromotionError("prepared PR recovery requires exactly one open PR read-back for this owner and generation")
    row = related_open[0]
    number = row.get("number")
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or number < 1
        or row.get("owner_id") != expected_owner
        or row.get("generation_id") != generation_id
        or row.get("head_ref") != expected_branch
        or row.get("base_ref") != "main"
        or row.get("head_sha") != candidate["head_sha"]
        or row.get("body") != body
        or row.get("body_sha256") != body_sha
    ):
        raise PromotionError("prepared PR recovery list read-back differs from the exact owned candidate")

    observed_body = observed.get("body")
    observed_number = observed.get("number")
    if (
        isinstance(observed_number, bool)
        or observed_number != number
        or observed.get("repository") != repository
        or observed.get("headRepository") != repository
        or str(observed.get("state", "")).upper() != "OPEN"
        or observed.get("headRefName") != expected_branch
        or observed.get("baseRefName") != "main"
        or observed.get("headRefOid") != candidate["head_sha"]
        or not isinstance(observed_body, str)
        or observed_body != body
        or hashlib.sha256(observed_body.encode("utf-8")).hexdigest() != body_sha
    ):
        raise PromotionError("prepared PR recovery API read-back differs from exact repository, branch, head, body, owner, generation, or issue")
    if not isinstance(remote_branch_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", remote_branch_sha) or remote_branch_sha != candidate["head_sha"]:
        raise PromotionError("prepared PR recovery remote branch SHA differs from the exact candidate head")
    return number


def resolve_prepared_create_pr_readback(
    root: pathlib.Path,
    repository: str,
    receipt: Mapping[str, Any],
    helper: Any,
    *,
    source_id: str,
    scope: str,
    generation_id: str,
    registry_path: str,
    registry_bytes: int,
    registry_sha256: str,
    composition_receipt_sha256: str,
) -> tuple[int, dict[str, Any]]:
    """Read back one accepted create without regenerating or trusting CLI output."""
    candidate = receipt.get("candidate")
    ownership = receipt.get("ownership")
    if not isinstance(candidate, Mapping) or not isinstance(ownership, Mapping):
        raise PromotionError("prepared PR recovery receipt has no candidate ownership")
    try:
        owner = helper.owner_id(repository, source_id, scope)
        branch = helper.automation_branch(candidate, "create")
    except Exception as exc:  # noqa: BLE001 - malformed durable identity must fail closed
        raise PromotionError("prepared PR recovery candidate ownership is invalid") from exc
    rows = gh_open_prs(root, repository)
    related_open = [
        row for row in rows
        if str(row.get("state", "")).upper() == "OPEN"
        and (
            row.get("owner_id") == owner
            or row.get("generation_id") == generation_id
            or row.get("head_ref") == branch
        )
    ]
    if len(related_open) != 1:
        raise PromotionError("prepared PR recovery found zero or multiple open owned PRs; no retry or mutation is safe")
    number = related_open[0].get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise PromotionError("prepared PR recovery list read-back has no valid PR number")
    observed = gh_pr_readback(root, repository, number)
    materializer = helper.load_materializer(root)
    remote_sha = helper.remote_ref_sha(materializer, root, "origin", f"refs/heads/{branch}")
    validate_prepared_create_pr_readback(
        receipt, rows, observed, remote_sha, helper,
        repository=repository, source_id=source_id, scope=scope,
        generation_id=generation_id, registry_path=registry_path,
        registry_bytes=registry_bytes, registry_sha256=registry_sha256,
        composition_receipt_sha256=composition_receipt_sha256,
    )
    return number, observed


def reconcile_prepared_create_pr(
    root: pathlib.Path,
    repository: str,
    receipt: Mapping[str, Any],
    helper: Any,
    *,
    source_id: str,
    scope: str,
    generation_id: str,
    registry_path: str,
    registry_bytes: int,
    registry_sha256: str,
    composition_receipt_sha256: str,
    journal_source_base_sha: str,
    observed_at: str,
    run_url: str,
) -> tuple[dict[str, Any], int]:
    """Promote only the durable acknowledgement after strict accepted-PR read-back."""
    number, observed = resolve_prepared_create_pr_readback(
        root, repository, receipt, helper,
        source_id=source_id, scope=scope, generation_id=generation_id,
        registry_path=registry_path, registry_bytes=registry_bytes,
        registry_sha256=registry_sha256,
        composition_receipt_sha256=composition_receipt_sha256,
    )
    try:
        updated = helper.record_pr_readback(
            json.loads(json.dumps(receipt)), observed,
            observed_at=observed_at, run_url=run_url,
        )
    except helper.AdmissionError as exc:
        raise PromotionError("prepared PR recovery could not bind the exact pending-review acknowledgement") from exc
    for field in ("candidate", "source_refresh_evidence", "checks", "validation_evidence", "ownership", "blockers"):
        if updated.get(field) != receipt.get(field):
            raise PromotionError(f"prepared PR recovery unexpectedly changed immutable {field} evidence")
    persist_journal_record(root, journal_source_base_sha, updated, observed_at=observed_at)
    return updated, number


def create_pr_then_reconcile_prepared_create(
    root: pathlib.Path,
    repository: str,
    receipt: Mapping[str, Any],
    helper: Any,
    *,
    source_id: str,
    scope: str,
    generation_id: str,
    registry_path: str,
    registry_bytes: int,
    registry_sha256: str,
    composition_receipt_sha256: str,
    journal_source_base_sha: str,
    observed_at: str,
    run_url: str,
    body_path: pathlib.Path,
) -> tuple[dict[str, Any], int]:
    """Attempt one PR create, then require authoritative read-back regardless of CLI status."""
    ownership = receipt.get("ownership")
    candidate = receipt.get("candidate")
    if not isinstance(ownership, Mapping) or not isinstance(candidate, Mapping):
        raise PromotionError("prepared PR creation has no durable owner or candidate")
    try:
        command((
            "gh", "pr", "create", "--repo", repository, "--draft", "--base", "main",
            "--head", str(ownership.get("branch", "")), "--title", pr_title(candidate),
            "--body-file", str(body_path),
        ), root, allowed_returncodes=frozenset(range(-255, 256)))
    except (OSError, subprocess.SubprocessError, PromotionError):
        # A missing CLI result has the same safe recovery path as a nonzero
        # status: authoritative read-back either proves exact acceptance or
        # fails closed without creating another PR.
        pass
    return reconcile_prepared_create_pr(
        root, repository, receipt, helper,
        source_id=source_id, scope=scope, generation_id=generation_id,
        registry_path=registry_path, registry_bytes=registry_bytes,
        registry_sha256=registry_sha256,
        composition_receipt_sha256=composition_receipt_sha256,
        journal_source_base_sha=journal_source_base_sha,
        observed_at=observed_at, run_url=run_url,
    )


def verify_release_ci_observation(root: pathlib.Path, receipt: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Keep CI observation independent from the exact accepted PR read-back."""
    ci_state = None
    ci_blocker = None
    try:
        ci_receipt = ensure_verify_release_ci(root, receipt)
        ci_entry = ci_receipt.get("ci", {})
        if isinstance(ci_entry, Mapping):
            ci_state = ci_entry.get("state")
            ci_blocker = ci_entry.get("blocker")
    except Exception as exc:  # noqa: BLE001 - CI observation is independent of accepted PR ownership
        ci_blocker = str(exc)
    return ci_state, ci_blocker


def existing_pr_rows(
    journal: Mapping[str, Any] | None,
    github_prs: Sequence[Mapping[str, Any]],
    helper: Any,
    candidate: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(journal, Mapping):
        owned = [row for row in github_prs if row.get("owner_id") == helper.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"]) and row.get("state") == "open"]
        if owned:
            raise PromotionError("open_candidate_pr_without_durable_owner_receipt: preserve it and investigate state branch")
        return [], 0
    owner = helper.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"])
    records = [
        row for row in journal.get("records", [])
        if row.get("ownership", {}).get("owner_id") == owner
    ]
    by_number = {int(row.get("number", 0)): row for row in github_prs}
    by_key = {helper.candidate_key(row): row for row in journal.get("records", []) if isinstance(row, Mapping)}
    result: list[dict[str, Any]] = []
    issue_number = 0
    recovered_revision_keys: set[tuple[str, str, str, str, str]] = set()
    open_numbers = {
        int(row.get("number", 0)) for row in github_prs
        if row.get("state") == "open" and row.get("owner_id") == owner
    }
    for number in sorted(open_numbers):
        observed = by_number.get(number)
        if observed is None:
            raise PromotionError("owned PR is absent from authoritative GitHub read-back")
        active_records = [
            row for row in records
            if row.get("superseded_by") is None
            and row.get("status") in {"prepared", "pending-review"}
            and int(row.get("pr", {}).get("number", 0)) == number
        ]
        if not active_records:
            recoveries = [
                row for row in records
                if row.get("superseded_by") is None
                and row.get("status") == "prepared"
                and int(row.get("pr", {}).get("number", 0)) == 0
                and row.get("candidate", {}).get("generation_id") == observed.get("generation_id")
                and row.get("candidate", {}).get("registry_sha256") == candidate.get("registry_sha256")
                and row.get("candidate", {}).get("head_sha") == observed.get("head_sha")
                and row.get("ownership", {}).get("body_sha256") == observed.get("body_sha256")
                and row.get("ownership", {}).get("branch") == observed.get("head_ref")
                and observed.get("base_ref") == "main"
                and observed.get("owner_id") == owner
                and helper.body_marker(owner, str(row.get("candidate", {}).get("generation_id", ""))) in str(observed.get("body", ""))
            ]
            if len(recoveries) != 1:
                raise PromotionError("interrupted candidate PR read-back is not one exact prepared head/body")
            selected = recoveries[0]
            recovered_revision_keys.add(helper.candidate_key(selected))
            issue_number = int(selected.get("ownership", {}).get("issue_number", 0))
            result.append({
                "repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"],
                "state": "open", "owner_id": owner, "number": number,
                "head_sha": observed.get("head_sha"), "automation_head_sha": observed.get("head_sha"),
                "body_sha256": observed.get("body_sha256"), "automation_body_sha256": observed.get("body_sha256"),
                "head_ref": observed.get("head_ref"), "base_ref": observed.get("base_ref"),
                "candidate_head_sha": selected["candidate"]["head_sha"],
                "manifest_sha256": selected["candidate"]["manifest_sha256"],
                "registry_sha256": selected["candidate"]["registry_sha256"],
                "revision_ref": helper.revision_reference(selected),
            })
            continue
        direct = [
            row for row in active_records
            if (
                observed.get("head_sha") == row.get("candidate", {}).get("head_sha")
                and observed.get("body_sha256") == row.get("ownership", {}).get("body_sha256")
                and observed.get("owner_id") == owner
                and observed.get("generation_id") == row.get("candidate", {}).get("generation_id")
                and observed.get("head_ref") == row.get("ownership", {}).get("branch")
                and observed.get("base_ref") == "main"
            )
        ]
        selected: Mapping[str, Any] | None = None
        if len(direct) == 1:
            selected = direct[0]
        elif len(direct) > 1:
            raise PromotionError("duplicate durable revisions claim the exact current owned PR head/body")
        else:
            intents = [
                row for row in active_records
                if row.get("status") == "prepared" and isinstance(row.get("refresh_from"), Mapping)
            ]
            phases: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
            for intent in intents:
                predecessor = by_key.get(helper._reference_key(intent["refresh_from"]))
                if predecessor is None:
                    raise PromotionError("prepared refresh intent has no exact durable predecessor")
                if int(intent.get("pr", {}).get("number", 0)) != number:
                    continue
                normalized = dict(observed)
                normalized.update({
                    "headRefName": observed.get("head_ref"),
                    "headRefOid": observed.get("head_sha"),
                    "baseRefName": observed.get("base_ref"),
                    "state": str(observed.get("state", "")).upper(),
                })
                phase = refresh_pr_phase(intent, predecessor, normalized, helper, require_repository=False)
                phases.append((phase, intent, predecessor))
            if len(phases) != 1:
                raise PromotionError("human_head_change_or_body_change: preserve the owned PR and stop")
            phase, intent, predecessor = phases[0]
            selected = predecessor if phase in {"before-push", "after-push-before-body"} else intent
        assert selected is not None
        row_state = "open"
        issue_number = int(selected.get("ownership", {}).get("issue_number", 0))
        result.append({
            "repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"],
            "state": row_state, "owner_id": owner, "number": number,
            "head_sha": observed.get("head_sha"), "automation_head_sha": observed.get("head_sha"),
            "body_sha256": observed.get("body_sha256"), "automation_body_sha256": observed.get("body_sha256"),
            "head_ref": observed.get("head_ref"), "base_ref": observed.get("base_ref"),
            "candidate_head_sha": selected["candidate"]["head_sha"],
            "manifest_sha256": selected["candidate"]["manifest_sha256"],
            "registry_sha256": selected["candidate"]["registry_sha256"],
            "revision_ref": helper.revision_reference(selected),
        })
    closed_numbers = {
        int(row.get("number", 0)) for row in github_prs
        if row.get("state") == "closed" and row.get("owner_id") == owner
    }
    for number in sorted(closed_numbers):
        observed = by_number.get(number)
        matches = [
            row for row in records
            if row.get("superseded_by") is None
            and row.get("status") == "closed"
            and int(row.get("pr", {}).get("number", 0)) == number
            and row.get("candidate", {}).get("head_sha") == observed.get("head_sha")
            and row.get("ownership", {}).get("body_sha256") == observed.get("body_sha256")
            and observed.get("owner_id") == owner
            and observed.get("generation_id") == row.get("candidate", {}).get("generation_id")
        ]
        if len(matches) != 1:
            raise PromotionError("closed owned PR read-back does not bind one exact durable candidate revision")
        record = matches[0]
        result.append({
            "repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"],
            "state": "closed", "owner_id": owner, "number": number,
            "head_sha": observed.get("head_sha"), "automation_head_sha": record["candidate"]["head_sha"],
            "body_sha256": observed.get("body_sha256"), "automation_body_sha256": record["ownership"]["body_sha256"],
            "candidate_head_sha": record["candidate"]["head_sha"],
            "manifest_sha256": record["candidate"]["manifest_sha256"],
            "registry_sha256": record["candidate"]["registry_sha256"],
            "revision_ref": helper.revision_reference(record),
        })
    for record in records:
        if record.get("superseded_by") is not None or record.get("status") not in {"prepared", "pending-review"}:
            continue
        if helper.candidate_key(record) in recovered_revision_keys:
            continue
        number = int(record.get("pr", {}).get("number", 0))
        if number:
            continue
        recoveries = [
            row for row in github_prs
            if row.get("state") == "open" and row.get("owner_id") == owner
            and row.get("generation_id") == record.get("candidate", {}).get("generation_id")
            and row.get("head_sha") == record.get("candidate", {}).get("head_sha")
            and row.get("body_sha256") == record.get("ownership", {}).get("body_sha256")
        ]
        if len(recoveries) > 1:
            raise PromotionError("duplicate_creation_readback: do not retry PR creation")
        if recoveries:
            observed = recoveries[0]
            result.append({
                "repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"],
                "state": "open", "owner_id": owner, "number": int(observed["number"]),
                "head_sha": observed.get("head_sha"), "automation_head_sha": observed.get("head_sha"),
                "body_sha256": observed.get("body_sha256"), "automation_body_sha256": observed.get("body_sha256"),
                "head_ref": observed.get("head_ref"), "base_ref": observed.get("base_ref"),
                "candidate_head_sha": record["candidate"]["head_sha"],
                "manifest_sha256": record["candidate"]["manifest_sha256"],
                "registry_sha256": record["candidate"]["registry_sha256"],
                "revision_ref": helper.revision_reference(record),
            })
    opens = [row for row in result if row["state"] == "open"]
    if len(opens) > 1:
        raise PromotionError("duplicate_open_prs: preserve all candidate branches and resolve ownership")
    return result, issue_number


def active_open_pr_rows(existing: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Separate the unique writable PR from closed archival ownership rows."""
    opened = [row for row in existing if row.get("state") == "open"]
    if len(opened) > 1:
        raise PromotionError("duplicate_open_prs: preserve all candidate branches and resolve ownership")
    return opened


def inspect_existing_pr_route(
    root: pathlib.Path,
    repository: str,
    journal: Mapping[str, Any] | None,
    existing: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    helper: Any,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], Mapping[str, Any] | None, Mapping[str, Any]]:
    """Resolve branch action from full history but read back only its unique open PR."""
    history = list(existing)
    decision = helper.decide_existing_prs(candidate, history)
    opened = active_open_pr_rows(existing)
    active_action = decision.get("action") in {"reuse_owned", "refresh_owned"}
    if bool(opened) != active_action:
        raise PromotionError("PR ownership route disagrees with the exact open-row inventory")
    if opened and int(decision.get("pr_number", 0)) != int(opened[0]["number"]):
        raise PromotionError("PR branch decision does not bind the unique open PR")
    observed = None
    if opened:
        observed = gh_pr_readback(root, repository, int(opened[0]["number"]))
        validate_existing_pr_api_readback(observed, journal, opened, helper)
    return history, opened, observed, decision


def pr_title(candidate: Mapping[str, Any]) -> str:
    return f"Review canonical registry update: {candidate['source_id']} ({candidate['generation_id']})"


def execute_candidate_preparation(args: argparse.Namespace, root: pathlib.Path) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id, attempt = str(args.workflow_run_id), str(args.workflow_run_attempt)
    if not re.fullmatch(r"[0-9]{6,20}", run_id) or not re.fullmatch(r"[1-9][0-9]*", attempt):
        raise PromotionError("processor workflow run/attempt identity is invalid")
    processor_run_id = f"{run_id}-{attempt}"
    bundle_dir = args.bundle_dir.resolve()
    idle = validate_no_candidate_processor_result(
        bundle_dir,
        repository=repo,
        workflow_run_id=run_id,
        workflow_run_attempt=attempt,
    )
    if idle is not None:
        print(json.dumps(idle, sort_keys=True))
        return
    helper = load_canonical_update_pr(root)
    composition_schema = load_object(root / "schemas/datapan.catalogue-composition-receipt.v1.schema.json")
    _, checkpoint = locate_processor_checkpoint(
        args.state_root, processor_run_id, repository=repo, workflow_run_id=run_id,
        artifact_id=args.processor_artifact_id,
        schema_path=root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
    )
    if checkpoint.get("source_id") != "data_go_kr" or checkpoint.get("source_scope") != "aggregate_supported_catalog":
        raise PromotionError("processor state is outside the admitted source/scope")
    validate_generation_identity(checkpoint)
    bundle = validate_processor_bundle(checkpoint, bundle_dir, composition_schema, helper)
    if not re.fullmatch(r"[a-f0-9]{40}", args.workflow_run_head_sha):
        raise PromotionError("processor workflow head must be a full immutable Git commit SHA")
    head_sha = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    verify_processor_input_compatibility(
        root, checkpoint, args.workflow_run_head_sha, head_sha,
        composition_receipt=bundle.get("composition_receipt"),
    )
    if bundle.get("status") in {"retry", "quarantined"}:
        print(json.dumps({
            "status": "no-candidate", "reason": bundle["reason"],
            "processor_status": bundle["status"],
            "source_id": checkpoint["source_id"], "generation_id": checkpoint["generation_id"],
            "candidate_available": False,
        }, sort_keys=True))
        return
    remote_main = command(("git", "ls-remote", "--heads", "origin", "refs/heads/main"), root)
    main_rows = [line.split("\t", 1)[0] for line in remote_main.stdout.splitlines() if line.endswith("\trefs/heads/main")]
    if main_rows != [head_sha]:
        raise PromotionError("stale_base: main changed after the upstream observation; request a fresh catalogue observation")

    # The old immutable Hugging Face revision is materialized only to verify
    # the processor's precise observation baseline. Candidate preparation
    # later uses the declared Git LFS backend for the new OID.
    canonical_path = root / bundle["registry_path"]
    baseline = root / ".datapan/previous/data-go-kr.registry.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    command((sys.executable, str(root / "scripts/materialize-canonical-registry.py"), "--output", str(baseline)), root)
    manifest = load_object(root / "manifest.json")
    registry_entries = [row for row in manifest.get("artifacts", []) if isinstance(row, dict) and row.get("path") == bundle["registry_path"] and row.get("kind") == "registry"]
    if len(registry_entries) != 1:
        raise PromotionError("release manifest does not have one canonical registry artifact")
    baseline_bytes, baseline_sha = registry_sha_from_path(baseline)
    if (baseline_bytes, baseline_sha) != (registry_entries[0].get("bytes"), bundle["baseline_sha256"]):
        raise PromotionError("stale_base: materialized source differs from the processor's immutable baseline; request a fresh observation")
    if bundle["registry_sha256"] == baseline_sha:
        print(json.dumps({"status": "no-change", "source_id": checkpoint["source_id"], "scope": checkpoint["source_scope"], "generation_id": checkpoint["generation_id"]}, sort_keys=True))
        return
    if command(("git", "status", "--porcelain", "--untracked-files=all"), root).stdout.strip():
        raise PromotionError("candidate checkout was not clean before staging; refusing to mix unrelated changes")

    prior_journal = load_promotion_journal(root)
    prior = journal_record_for(
        prior_journal, checkpoint["source_id"], checkpoint["source_scope"],
        checkpoint["generation_id"], bundle["registry_sha256"],
    )
    if prior is not None and prior.get("superseded_by") is not None:
        print(json.dumps({
            "status": "already-superseded", "generation_id": checkpoint["generation_id"],
            "registry_sha256": bundle["registry_sha256"],
        }, sort_keys=True))
        return
    if prior is not None and prior.get("status") not in {"prepared"}:
        number = int(prior.get("pr", {}).get("number", 0))
        if prior.get("status") == "pending-review" and number > 0:
            observed = gh_pr_readback(root, repo, number)
            validate_exact_open_pr(prior, observed, helper)
        print(json.dumps({
            "status": "already-delivered", "pr_number": number,
            "generation_id": checkpoint["generation_id"],
            "registry_sha256": bundle["registry_sha256"],
        }, sort_keys=True))
        return
    prior_pr = prior.get("pr") if isinstance(prior, Mapping) else None
    if (
        prior is not None
        and prior.get("status") == "prepared"
        and isinstance(prior_pr, Mapping)
        and prior_pr.get("number") == 0
    ):
        # A durable PR-zero intent means candidate generation, native source
        # reports, LFS upload, and branch advancement already completed. First
        # reconcile the original immutable intent; never regenerate it before
        # checking whether GitHub accepted its PR create request.
        helper = load_canonical_update_pr(root)
        observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}"
        final_receipt, number = reconcile_prepared_create_pr(
            root, repo, prior, helper,
            source_id=checkpoint["source_id"], scope=checkpoint["source_scope"],
            generation_id=checkpoint["generation_id"], registry_path=bundle["registry_path"],
            registry_bytes=bundle["registry_bytes"], registry_sha256=bundle["registry_sha256"],
            composition_receipt_sha256=bundle["composition_receipt_sha256"],
            journal_source_base_sha=head_sha, observed_at=observed_at, run_url=run_url,
        )
        ci_state, ci_blocker = verify_release_ci_observation(root, final_receipt)
        print(json.dumps({
            "status": final_receipt["status"], "pr_number": number,
            "recovered_prepared_create": True, "candidate_base_sha": final_receipt["candidate"]["base_sha"],
            "candidate_sha": final_receipt["candidate"]["head_sha"],
            "registry_sha256": final_receipt["candidate"]["registry_sha256"],
            "generation_id": final_receipt["candidate"]["generation_id"],
            "manual_review_acceptance": final_receipt["candidate"]["manual_review_acceptance_status"],
            "blockers": final_receipt.get("blockers", []),
            "source_refresh_commands": len(final_receipt.get("source_refresh_evidence", {}).get("commands", [])),
            "native_source_regeneration": False,
            "verify_release_ci_state": ci_state, "verify_release_ci_blocker": ci_blocker,
        }, sort_keys=True))
        return
    if prior is None and isinstance(prior_journal, Mapping):
        same_payload = [
            row for row in prior_journal.get("records", [])
            if row.get("superseded_by") is None
            and row.get("status") == "pending-review"
            and row.get("candidate", {}).get("repository", "").casefold() == repo.casefold()
            and row.get("candidate", {}).get("source_id") == checkpoint["source_id"]
            and row.get("candidate", {}).get("scope") == checkpoint["source_scope"]
            and row.get("candidate", {}).get("registry_sha256") == bundle["registry_sha256"]
        ]
        if len(same_payload) > 1:
            raise PromotionError("multiple active PR revisions claim the same canonical payload digest")
        if same_payload:
            active = same_payload[0]
            number = int(active.get("pr", {}).get("number", 0))
            if number < 1:
                raise PromotionError("active same-payload candidate has no durable PR number")
            validate_exact_open_pr(active, gh_pr_readback(root, repo, number), helper)
            print(json.dumps({
                "status": "already-delivered", "pr_number": number,
                "generation_id": active["candidate"]["generation_id"],
                "registry_sha256": bundle["registry_sha256"],
            }, sort_keys=True))
            return

    canonical_path.write_bytes((bundle_dir / "composed-candidate.registry.json").read_bytes())
    helper.update_registry_manifest_artifact(root, pathlib.PurePosixPath(bundle["registry_path"]), bundle["registry_bytes"], bundle["registry_sha256"])
    old_published_pin = json.dumps(load_object(root / "policy/registry-distribution.json").get("canonical_registry"), sort_keys=True)
    review_artifacts = update_registry_review_artifacts(root, checkpoint["generation_id"], bundle_dir)
    refresh = load_module(root / "scripts/refresh-canonical-snapshot-evidence.py", "refresh_canonical_snapshot_evidence")
    observation = checkpoint.get("last_observation")
    stable_generated_at = observation.get("observed_at") if isinstance(observation, dict) else None
    if not isinstance(stable_generated_at, str) or not stable_generated_at:
        raise PromotionError("processor checkpoint has no source observation time for deterministic report regeneration")
    registry_sha, source_commands, source_refresh_evidence = refresh.run_source_refresh(
        repository_root=root, datapan_cli=args.datapan_cli.resolve(), registry=canonical_path,
        verification=root / "reports/latest-verification.json", previous_registry=baseline,
        stable_generated_at=stable_generated_at,
    )
    refresh.run_ledger_refresh(root)
    command((sys.executable, "scripts/refresh-release-ledger-evidence.py", "--check"), root)
    if registry_sha != bundle["registry_sha256"]:
        raise PromotionError("source refresh reports do not bind to the exact composed registry bytes")
    if json.dumps(load_object(root / "policy/registry-distribution.json").get("canonical_registry"), sort_keys=True) != old_published_pin:
        raise PromotionError("candidate preparation changed the immutable published Hugging Face identity pin")
    command((sys.executable, "scripts/generate-credential-runtime-manual-review-acceptance.py", "--check"), root)
    acceptance = manual_review_status(root)
    command((sys.executable, "scripts/sync-release-schema-artifacts.py", "--write"), root)
    command((sys.executable, "scripts/sync-release-manifest-artifacts.py", "--write"), root)
    source_refresh_evidence["manifest_sha256"] = file_sha256(root / "manifest.json")
    for native_output in source_refresh_evidence.get("commands", []):
        output = root / str(native_output["output_path"])
        if not output.is_file() or (output.stat().st_size, file_sha256(output)) != (native_output["output_bytes"], native_output["output_sha256"]):
            raise PromotionError("pinned native report changed after its source-bound output digest was recorded")

    # Recover an exact payload revision before making a commit. New registry
    # bytes under the same B generation form a distinct C revision and may
    # refresh the existing owned PR after exact read-back.
    staged_paths = stage_candidate_outputs(root, checkpoint["generation_id"])
    current_tree = command(("git", "write-tree"), root).stdout.strip()
    if prior is not None:
        prior_sha = prior["candidate"]["head_sha"]
        branch = prior.get("ownership", {}).get("branch")
        if isinstance(branch, str) and branch:
            materializer = helper.load_materializer(root)
            remote_branch = helper.remote_ref_sha(materializer, root, "origin", f"refs/heads/{branch}")
            if remote_branch is not None:
                command(("git", "fetch", "--no-tags", "origin", f"refs/heads/{branch}:refs/remotes/origin/canonical-update-candidate"), root)
        prior_tree_result = command(("git", "rev-parse", f"{prior_sha}^{{tree}}"), root, allowed_returncodes=frozenset({0, 128}))
        if prior_tree_result.returncode == 0:
            if prior_tree_result.stdout.strip() != current_tree:
                raise PromotionError("candidate_payload_revision_reused_with_different_tree: preserve the durable candidate receipt")
            command(("git", "checkout", "--detach", prior_sha), root)
        else:
            commit_env = dict(os.environ)
            commit_env["GIT_AUTHOR_DATE"] = stable_generated_at
            commit_env["GIT_COMMITTER_DATE"] = stable_generated_at
            command((
                "git", "-c", "user.name=datapan-canonical-update[bot]",
                "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "commit", "-m", f"Prepare canonical catalogue update {checkpoint['generation_id']} {bundle['registry_sha256']}",
            ), root, env=commit_env)
            rebuilt_sha = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
            if rebuilt_sha != prior_sha:
                raise PromotionError("prepared candidate commit cannot be deterministically recovered from its durable refresh intent")
    else:
        commit_env = dict(os.environ)
        commit_env["GIT_AUTHOR_DATE"] = stable_generated_at
        commit_env["GIT_COMMITTER_DATE"] = stable_generated_at
        command((
            "git", "-c", "user.name=datapan-canonical-update[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", f"Prepare canonical catalogue update {checkpoint['generation_id']} {bundle['registry_sha256']}",
        ), root, env=commit_env)
    candidate_head = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    manifest_sha = file_sha256(root / "manifest.json")
    diagnostic_applicability_status = source_refresh_evidence["diagnostic_current_source_applicability"]["status"]
    candidate = {
        "repository": repo,
        "source_id": checkpoint["source_id"],
        "scope": checkpoint["source_scope"],
        "base_sha": head_sha,
        "head_sha": candidate_head,
        "manifest_sha256": manifest_sha,
        "registry_path": bundle["registry_path"],
        "registry_bytes": bundle["registry_bytes"],
        "registry_sha256": bundle["registry_sha256"],
        "composition_receipt_sha256": bundle["composition_receipt_sha256"],
        "composition_receipt": bundle["composition_receipt"],
        "composition_receipt_path": bundle["composition_receipt_path"],
        "composition_outputs_dir": bundle["composition_outputs_dir"],
        "generation_id": checkpoint["generation_id"],
        "checks": {
            "composition": "passed", "registry_manifest": "passed", "denominator": "passed",
            "adapter": "passed", "ledger": "passed", "release": "passed", "consumer": "passed",
            "finish_review_policy": finish_review_policy_status(root),
            "manual_review_acceptance": acceptance,
            "diagnostic_current_source_applicability": diagnostic_applicability_status,
        },
        "manual_review_acceptance_status": acceptance,
        "source_refresh_evidence": source_refresh_evidence,
    }
    evidence = run_bound_validation(root, args.datapan_cli.resolve(), candidate)
    candidate["validation_evidence"] = evidence

    if args.prepare_only:
        # Keep the offline rehearsal aligned with the pure local admission
        # gate that prepare_lfs_upload applies before any LFS operation.
        helper.validate_candidate(
            candidate, head_sha, composition_schema, require_payload_readback=False,
        )
        print(json.dumps({
            "status": "candidate-prepared-locally",
            "external_mutations": False,
            "repository": repo,
            "source_id": checkpoint["source_id"],
            "scope": checkpoint["source_scope"],
            "generation_id": checkpoint["generation_id"],
            "base_sha": head_sha,
            "candidate_sha": candidate_head,
            "manifest_sha256": manifest_sha,
            "registry_path": bundle["registry_path"],
            "registry_bytes": bundle["registry_bytes"],
            "registry_sha256": bundle["registry_sha256"],
            "manual_review_acceptance": acceptance,
            "validation_evidence_count": len(evidence),
            "source_refresh_evidence": source_refresh_evidence,
            "staged_paths": staged_paths,
        }, sort_keys=True))
        return

    github_prs = gh_open_prs(root, repo)
    existing, existing_issue_number = existing_pr_rows(prior_journal, github_prs, helper, candidate)
    # Keep closed rows intact for create_replacement and branch CAS decisions;
    # only the unique open row receives a writable PR API read-back.
    existing, open_existing, _, existing_decision = inspect_existing_pr_route(
        root, repo, prior_journal, existing, candidate, helper,
    )
    owner = helper.owner_id(repo, checkpoint["source_id"], checkpoint["source_scope"])
    # A refreshed generation reuses its still-open owned issue. Otherwise a
    # stable owner+generation marker makes interrupted issue creation safely
    # discoverable without duplicating it.
    policy_path = root / "policy/registry-distribution.json"
    previous_payload_readback = prior.get("candidate", {}).get("payload_readback") if prior is not None else None
    upload_lfs_oid = not (
        isinstance(previous_payload_readback, Mapping)
        and previous_payload_readback.get("status") == "verified"
        and previous_payload_readback.get("sha256") == bundle["registry_sha256"]
        and previous_payload_readback.get("source_sha") == candidate_head
    )
    prepared, _ = helper.prepare_lfs_upload(
        candidate, existing, head_sha, composition_schema, repository_root=root, policy_path=policy_path,
        remote="origin", upload=upload_lfs_oid,
    )
    if not upload_lfs_oid and isinstance(previous_payload_readback, Mapping):
        prepared["candidate"]["payload_readback"] = json.loads(json.dumps(previous_payload_readback))
    if open_existing:
        # Upload/read-back can take minutes. Re-read the actual PR API fields
        # after the remote LFS proof and compare them with the durable prior
        # receipt before advancing the branch.
        latest_prs = gh_open_prs(root, repo)
        latest_existing, _ = existing_pr_rows(prior_journal, latest_prs, helper, candidate)
        latest_existing, latest_open, _, latest_decision = inspect_existing_pr_route(
            root, repo, prior_journal, latest_existing, candidate, helper,
        )
        if len(latest_open) != 1 or latest_open[0]["number"] != open_existing[0]["number"]:
            raise PromotionError("owned PR changed during Git LFS validation; preserve branch and re-read")
        if latest_decision.get("branch") != existing_decision.get("branch"):
            raise PromotionError("owned PR branch route changed during Git LFS validation; preserve branch and re-read")
        existing = latest_existing
        open_existing = latest_open
    issue_number, issue_url = ensure_candidate_issue(root, repo, candidate, owner, existing_issue_number)
    body = helper.render_pr_body(candidate, owner, issue_number)
    prepared["ownership"]["issue_number"] = issue_number
    prepared["ownership"]["issue_url"] = issue_url
    prepared["ownership"]["body"] = body
    prepared["ownership"]["body_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if prior is not None and isinstance(prior.get("refresh_from"), Mapping):
        prepared["refresh_from"] = json.loads(json.dumps(prior["refresh_from"]))
    elif prepared.get("action") == "refresh_owned":
        if not open_existing or not isinstance(open_existing[0].get("revision_ref"), Mapping):
            raise PromotionError("owned PR refresh has no exact durable predecessor revision")
        prepared["refresh_from"] = json.loads(json.dumps(open_existing[0]["revision_ref"]))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    persist_journal_record(root, head_sha, prepared, observed_at=now)
    if open_existing:
        # This is the last GitHub PR read before the helper's remote-main and
        # candidate-branch compare-and-swap checks and branch push.
        latest_pr = gh_pr_readback(root, repo, int(open_existing[0]["number"]))
        if isinstance(prepared.get("refresh_from"), Mapping):
            predecessor = journal_record_for(
                prior_journal,
                str(prepared["refresh_from"].get("source_id", "")),
                str(prepared["refresh_from"].get("scope", "")),
                str(prepared["refresh_from"].get("generation_id", "")),
                str(prepared["refresh_from"].get("registry_sha256", "")),
            )
            if predecessor is None:
                raise PromotionError("prepared refresh intent lost its exact predecessor before branch CAS")
            refresh_pr_phase(prepared, predecessor, latest_pr, helper)
        else:
            validate_exact_open_pr(open_existing[0].get("record", prepared), latest_pr, helper)
    pushed = helper.push_owned_branch(
        candidate, prepared, existing, head_sha, repository_root=root, remote="origin",
    )
    body_path = root / ".datapan/candidate-pr.md"
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(body, encoding="utf-8")
    title = pr_title(candidate)
    if open_existing:
        number = int(open_existing[0]["number"])
        immediate = gh_pr_readback(root, repo, number)
        if isinstance(prepared.get("refresh_from"), Mapping):
            predecessor = journal_record_for(
                prior_journal,
                str(prepared["refresh_from"].get("source_id", "")),
                str(prepared["refresh_from"].get("scope", "")),
                str(prepared["refresh_from"].get("generation_id", "")),
                str(prepared["refresh_from"].get("registry_sha256", "")),
            )
            if predecessor is None:
                raise PromotionError("prepared refresh intent lost its exact predecessor after branch CAS")
            phase = refresh_pr_phase(prepared, predecessor, immediate, helper)
            if phase == "after-push-before-body":
                command(("gh", "pr", "edit", str(number), "--repo", repo, "--title", title, "--body-file", str(body_path)), root)
            elif phase != "after-body-edit":
                raise PromotionError("owned PR has not read back the exact pushed refresh head")
        else:
            validate_exact_open_pr(prepared, immediate, helper)
        final_receipt = helper.record_pr_readback(
            pushed, immediate, observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            run_url=f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}",
        )
        persist_journal_record(
            root, head_sha, final_receipt,
            observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            supersede_from=final_receipt.get("refresh_from"),
        )
    else:
        # The CLI may return nonzero after GitHub accepted the create request,
        # or lose its response. Its output is never the ownership authority.
        # Read back the unique exact candidate before recording pending-review.
        final_receipt, number = create_pr_then_reconcile_prepared_create(
            root, repo, pushed, helper,
            source_id=checkpoint["source_id"], scope=checkpoint["source_scope"],
            generation_id=checkpoint["generation_id"], registry_path=bundle["registry_path"],
            registry_bytes=bundle["registry_bytes"], registry_sha256=bundle["registry_sha256"],
            composition_receipt_sha256=bundle["composition_receipt_sha256"],
            journal_source_base_sha=head_sha,
            observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            run_url=f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}",
            body_path=body_path,
        )
    ci_state, ci_blocker = verify_release_ci_observation(root, final_receipt)
    print(json.dumps({
        "status": final_receipt["status"], "pr_number": number,
        "candidate_sha": candidate_head, "registry_sha256": bundle["registry_sha256"],
        "generation_id": checkpoint["generation_id"], "manual_review_acceptance": acceptance,
        "blockers": final_receipt.get("blockers", []), "source_refresh_commands": len(source_commands),
        "verify_release_ci_state": ci_state, "verify_release_ci_blocker": ci_blocker,
        "staged_path_count": len(staged_paths),
        "review_artifacts": str(review_artifacts.relative_to(root)),
    }, sort_keys=True))


def recover_ready_processor_candidate(args: argparse.Namespace, root: pathlib.Path) -> None:
    """Recover at most one durable B ready artifact using current trusted C code."""
    repository = os.environ["GITHUB_REPOSITORY"]
    default_branch = (
        os.environ.get("GITHUB_DEFAULT_BRANCH")
        or os.environ.get("DEFAULT_BRANCH")
        or os.environ.get("GITHUB_REF_NAME")
        or "main"
    )
    schema_path = root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
    if not args.state_root or not args.datapan_cli:
        raise PromotionError("ready recovery requires the durable B state branch and pinned Datapan CLI")

    # A workflow_run event is an additional trusted notification, not the
    # authority for selecting a candidate. The durable checkpoint's own run
    # and artifact locator are independently verified below.
    event_run_id = args.event_run_id
    if event_run_id:
        if not args.event_run_attempt or not args.event_head_sha:
            raise PromotionError("B workflow_run notification is missing its exact attempt or head SHA")
        if not re.fullmatch(r"[0-9]{6,20}", event_run_id):
            raise PromotionError("B workflow_run notification has an invalid run id")
        event_run = processor_run_api(root, repository, event_run_id, args.event_run_attempt)
        validate_trusted_processor_run(
            event_run, repository=repository, run_id=event_run_id,
            attempt=args.event_run_attempt, default_branch=default_branch,
            expected_head_sha=args.event_head_sha,
        )

    journal = load_promotion_journal(root)
    candidates, blocked = list_recoverable_processor_checkpoints(
        args.state_root.resolve(), schema_path, journal,
    )
    if not candidates:
        print(json.dumps({
            "status": "no-eligible-ready-processor-bundle" if blocked else "no-undelivered-ready-processor-bundle",
            "candidate_available": False,
            "blocked_generations": blocked,
        }, sort_keys=True))
        return

    composition_schema = load_object(root / "schemas/datapan.catalogue-composition-receipt.v1.schema.json")
    composition_helper = load_canonical_update_pr(root)
    current_head_sha = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    screened, blocked = select_first_eligible_processor_bundle(
        root, repository, candidates, blocked,
        journal=journal,
        default_branch=default_branch,
        current_head_sha=current_head_sha,
        composition_schema=composition_schema,
        composition_helper=composition_helper,
    )
    if screened is None:
        print(json.dumps({
            "status": "no-eligible-ready-processor-bundle",
            "candidate_available": False,
            "blocked_generations": blocked,
        }, sort_keys=True))
        return
    if blocked:
        print(json.dumps({"status": "skipped-unusable-older-generations", "blocked_generations": blocked}, sort_keys=True))
    args.bundle_dir = screened["bundle_dir"]
    args.workflow_run_id = screened["run_id"]
    args.workflow_run_attempt = screened["attempt"]
    args.workflow_run_head_sha = str(screened["run"]["head_sha"])
    args.processor_artifact_id = screened["artifact_id"]
    execute_candidate_preparation(args, root)


def reconcile_open_promotions(root: pathlib.Path) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    helper = load_module(root / "scripts/canonical_update_pr.py", "canonical_update_pr_reconcile")
    journal = load_promotion_journal(root)
    if not isinstance(journal, Mapping):
        print(json.dumps({"status": "no-promotion-journal"}))
        return
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}"
    base = command(("git", "rev-parse", "HEAD"), root).stdout.strip()
    # Complete only refreshes whose persisted intent, predecessor identity,
    # target head, and target body prove one of the two post-push crash points.
    # A pre-push intent remains pending while its predecessor stays active.
    for intent in list(journal.get("records", [])):
        if (
            intent.get("status") != "prepared"
            or intent.get("superseded_by") is not None
            or not isinstance(intent.get("refresh_from"), Mapping)
            or int(intent.get("pr", {}).get("number", 0)) < 1
        ):
            continue
        predecessor_ref = intent["refresh_from"]
        predecessor = journal_record_for(
            journal,
            str(predecessor_ref.get("source_id", "")),
            str(predecessor_ref.get("scope", "")),
            str(predecessor_ref.get("generation_id", "")),
            str(predecessor_ref.get("registry_sha256", "")),
        )
        if predecessor is None:
            raise PromotionError("prepared refresh intent has no exact predecessor revision")
        number = int(intent["pr"]["number"])
        observed = gh_pr_readback(root, repo, number)
        phase = refresh_pr_phase(intent, predecessor, observed, helper)
        if phase == "before-push":
            continue
        if phase == "after-push-before-body":
            body = intent.get("ownership", {}).get("body")
            if not isinstance(body, str):
                raise PromotionError("prepared refresh intent has no exact body for crash recovery")
            body_path = root / ".datapan/candidate-pr.md"
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text(body, encoding="utf-8")
            command((
                "gh", "pr", "edit", str(number), "--repo", repo,
                "--title", pr_title(intent["candidate"]), "--body-file", str(body_path),
            ), root)
            observed = gh_pr_readback(root, repo, number)
            phase = refresh_pr_phase(intent, predecessor, observed, helper)
            if phase != "after-body-edit":
                raise PromotionError("refresh body edit did not read back the exact target head and body")
        elif phase != "after-body-edit":
            raise PromotionError("prepared refresh intent is not at a recoverable PR state")
        refreshed = helper.record_pr_readback(
            intent, observed, observed_at=dt.datetime.now(dt.timezone.utc).isoformat(), run_url=run_url,
        )
        persist_journal_record(
            root, base, refreshed,
            observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            supersede_from=predecessor_ref,
        )
        journal = load_promotion_journal(root) or journal

    prs = gh_open_prs(root, repo)
    by_number = {int(row["number"]): row for row in prs}
    for old in list(journal.get("records", [])):
        if (
            old.get("superseded_by") is not None
            or (old.get("status") == "prepared" and isinstance(old.get("refresh_from"), Mapping))
            or old.get("status") not in {"prepared", "pending-review"}
            or not old.get("pr", {}).get("number")
        ):
            continue
        actual = by_number.get(int(old["pr"]["number"]))
        if actual is None:
            raise PromotionError("journal candidate PR is absent from GitHub read-back")
        observed = gh_pr_readback(root, repo, int(old["pr"]["number"]))
        if observed.get("state") == "OPEN":
            refreshed = helper.record_pr_readback(
                old, observed, observed_at=dt.datetime.now(dt.timezone.utc).isoformat(), run_url=run_url,
            )
            persist_journal_record(root, base, refreshed, observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
            try:
                ci_receipt = ensure_verify_release_ci(root, refreshed)
                ci = ci_receipt.get("ci", {})
                print(json.dumps({
                    "status": "verify-release-ci-reconciled",
                    "pr_number": int(old["pr"]["number"]),
                    "ci_state": ci.get("state") if isinstance(ci, Mapping) else None,
                    "ci_blocker": ci.get("blocker") if isinstance(ci, Mapping) else None,
                }, sort_keys=True))
            except Exception as exc:  # noqa: BLE001 - CI observation must not gate independent candidate recovery
                print(json.dumps({
                    "status": "verify-release-ci-observation-failed",
                    "pr_number": int(old["pr"]["number"]),
                    "reason": str(exc),
                }, sort_keys=True))
            continue
        new = helper.record_pr_readback(
            old, observed, observed_at=dt.datetime.now(dt.timezone.utc).isoformat(), run_url=run_url,
        )
        persist_journal_record(root, base, new, observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
    print(json.dumps({"status": "promotion-prs-reconciled", "run_url": run_url}, sort_keys=True))


def reconcile_publication(root: pathlib.Path, receipt_path: pathlib.Path) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    helper = load_module(root / "scripts/canonical_update_pr.py", "canonical_update_publication_reconcile")
    publication = load_object(receipt_path)
    source = publication.get("source_binding", {})
    if not isinstance(source, Mapping):
        raise PromotionError("#592 publication receipt has no source-binding object")
    source_sha = source.get("source_sha")
    manifest_sha = source.get("manifest_sha256")
    if publication.get("schema_version") != "datapan.registry-publication-receipt.v1":
        raise PromotionError("unsupported #592 publication receipt schema")
    if (
        not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
        or not isinstance(manifest_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha)
        or source.get("repository") != repo
    ):
        raise PromotionError("#592 receipt has no exact current-repository source and manifest binding")
    journal = load_promotion_journal(root)
    if not isinstance(journal, Mapping):
        raise PromotionError("publication receipt has no matching durable promotion journal")
    manifest_candidates = [
        row for row in journal.get("records", [])
        if row.get("candidate", {}).get("manifest_sha256") == manifest_sha
    ]
    tracked, pr = select_publication_candidate(
        manifest_candidates,
        source_sha=source_sha,
        readback=lambda number: gh_pr_readback(root, repo, number),
    )
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}"
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    observed = helper.record_pr_readback(tracked, pr, observed_at=observed_at, run_url=run_url)
    persist_journal_record(root, command(("git", "rev-parse", "HEAD"), root).stdout.strip(), observed, observed_at=observed_at)
    if observed.get("pr", {}).get("state") != "merged" or observed.get("pr", {}).get("merge_commit_sha") != source_sha:
        raise PromotionError("#592 publication source is not the exact merge commit independently read from the canonical candidate PR")
    updated = helper.reconcile_huggingface_publication(observed, receipt_path, observed_at=observed_at, run_url=run_url)
    persist_journal_record(root, source_sha, updated, observed_at=observed_at)
    print(json.dumps({"status": updated["status"], "source_sha": source_sha, "manifest_sha256": manifest_sha, "run_url": run_url}, sort_keys=True))


def select_publication_candidate(
    candidates: list[Mapping[str, Any]],
    *,
    source_sha: str,
    readback: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve #592 against live PR API state, including before hourly PR reconciliation."""
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise PromotionError("promotion journal contains a malformed candidate row")
        pr_record = candidate.get("pr")
        number = pr_record.get("number") if isinstance(pr_record, Mapping) else None
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            continue
        pr = readback(number)
        merge = pr.get("mergeCommit")
        merge_sha = merge.get("oid") if isinstance(merge, Mapping) else None
        if pr.get("state") == "MERGED" and merge_sha == source_sha:
            matches.append((dict(candidate), dict(pr)))
    if len(matches) != 1:
        raise PromotionError("#592 receipt does not identify exactly one merged canonical candidate by live PR read-back")
    return matches[0]


def journal_record_for(
    journal: Mapping[str, Any] | None,
    source_id: str,
    scope: str,
    generation_id: str | None = None,
    registry_sha256: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(journal, Mapping):
        return None
    rows = [
        row for row in journal.get("records", [])
        if isinstance(row, dict)
        and row.get("candidate", {}).get("source_id") == source_id
        and row.get("candidate", {}).get("scope") == scope
        and (generation_id is None or row.get("candidate", {}).get("generation_id") == generation_id)
        and (registry_sha256 is None or row.get("candidate", {}).get("registry_sha256") == registry_sha256)
    ]
    if registry_sha256 is not None and len(rows) > 1:
        raise PromotionError("promotion journal has duplicate source/scope/generation/payload revision entries")
    if generation_id is not None and registry_sha256 is None and len(rows) > 1:
        raise PromotionError("promotion journal revision lookup requires the exact registry payload digest")
    return rows[-1] if rows else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("recover-ready", "reconcile-prs", "reconcile-publication"), default="recover-ready")
    parser.add_argument("--repository-root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--state-root", type=pathlib.Path)
    parser.add_argument("--bundle-dir", type=pathlib.Path)
    parser.add_argument("--datapan-cli", type=pathlib.Path)
    parser.add_argument("--workflow-run-id")
    parser.add_argument("--workflow-run-attempt")
    parser.add_argument("--workflow-run-head-sha")
    parser.add_argument("--processor-artifact-id")
    parser.add_argument("--event-run-id")
    parser.add_argument("--event-run-attempt")
    parser.add_argument("--event-head-sha")
    parser.add_argument("--publication-receipt", type=pathlib.Path)
    parser.add_argument("--prepare-only", action="store_true", help="generate and validate a local candidate commit without uploading LFS, creating issues/PRs, or writing promotion state")
    args = parser.parse_args()
    root = args.repository_root.resolve()
    try:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if not repo or "/" not in repo:
            raise PromotionError("GITHUB_REPOSITORY is required")
        if args.mode == "recover-ready":
            required = (args.state_root, args.datapan_cli)
            if any(value is None for value in required):
                raise PromotionError("ready recovery requires durable processor state and the pinned Datapan CLI")
            recover_ready_processor_candidate(args, root)
        elif args.mode == "reconcile-prs":
            reconcile_open_promotions(root)
        else:
            if args.publication_receipt is None:
                raise PromotionError("reconcile-publication mode requires the downloaded immutable #592 receipt")
            reconcile_publication(root, args.publication_receipt.resolve())
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL canonical update promotion: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
