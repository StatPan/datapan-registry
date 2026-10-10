#!/usr/bin/env python3
"""Build a read-only, explicit-time health receipt for the upstream catalogue pipeline."""

from __future__ import annotations

import argparse
import ast
import copy
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import selectors
import subprocess
import sys
import time
import urllib.parse
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKPOINT_SCHEMA = ROOT / "schemas" / "datapan.upstream-catalogue-checkpoint.v1.schema.json"
HEALTH_POLICY_SCHEMA = ROOT / "schemas" / "datapan.upstream-catalogue-health-policy.v1.schema.json"
HEALTH_RECEIPT_SCHEMA = ROOT / "schemas" / "datapan.upstream-catalogue-health.v1.schema.json"
HEALTH_STATE_SCHEMA = ROOT / "schemas" / "datapan.upstream-catalogue-health-state.v1.schema.json"
PROMOTION_SCHEMA = ROOT / "schemas" / "datapan.canonical-update-promotion-receipt.v1.schema.json"
PROMOTION_JOURNAL_SCHEMA = ROOT / "schemas" / "datapan.canonical-update-promotion-journal.v1.schema.json"
PROMOTION_SPEC = importlib.util.spec_from_file_location("canonical_update_pr_health", ROOT / "scripts" / "canonical_update_pr.py")
assert PROMOTION_SPEC and PROMOTION_SPEC.loader
PROMOTION = importlib.util.module_from_spec(PROMOTION_SPEC)
sys.modules[PROMOTION_SPEC.name] = PROMOTION
PROMOTION_SPEC.loader.exec_module(PROMOTION)
TERMINAL_EVIDENCE_PATH = ROOT / "scripts" / "canonical_update_terminal_evidence.py"
TERMINAL_EVIDENCE_SPEC = importlib.util.spec_from_file_location(
    "canonical_update_terminal_evidence_health", TERMINAL_EVIDENCE_PATH,
)
assert TERMINAL_EVIDENCE_SPEC and TERMINAL_EVIDENCE_SPEC.loader
TERMINAL_EVIDENCE = importlib.util.module_from_spec(TERMINAL_EVIDENCE_SPEC)
sys.modules[TERMINAL_EVIDENCE_SPEC.name] = TERMINAL_EVIDENCE
TERMINAL_EVIDENCE_SPEC.loader.exec_module(TERMINAL_EVIDENCE)
PROMOTION_RUNNER_PATH = ROOT / "scripts" / "run-canonical-update-promotion.py"
COMPOSITION_SCHEMA_PATH = ROOT / "schemas" / "datapan.catalogue-composition-receipt.v1.schema.json"
SOURCE_POLICY_DEFAULT = pathlib.Path("policy/source-refresh.json")
HEALTH_POLICY_DEFAULT = pathlib.Path("policy/upstream-catalogue-health.json")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
REVISION = re.compile(r"^[a-f0-9]{40,64}$")
MAX_PROMOTION_JOB_PAGES = 5
MAX_PROMOTION_JOBS = 500
MAX_PROCESSOR_PRIOR_ATTEMPT_LOOKUPS = 5
MAX_COLLECTOR_EXECUTION_RUNS = 20
MAX_COLLECTOR_EXECUTION_LOOKUPS = 25
MAX_PROMOTION_EXECUTION_ATTEMPTS = 2
MAX_TERMINAL_OUTCOME_ATTEMPTS = 2
MAX_TERMINAL_ARTIFACT_PAGES = 5
MAX_TERMINAL_ARTIFACTS = 500
MAX_TERMINAL_ARCHIVE_BYTES = 1024 * 1024
MAX_PUBLICATION_RECOVERY_REFERENCES = 20
MAX_PUBLICATION_RECOVERY_API_REQUESTS = 256
PUBLICATION_RECOVERY_REFERENCE_PREFIX = "publication-recovery/v1 "
PUBLICATION_WORKFLOW_PATH = ".github/workflows/huggingface-distribution.yml"
PROMOTION_WORKFLOW_EVENTS = {"workflow_run", "schedule", "workflow_dispatch"}
PROCESSOR_OUTPUT_PATHS = (
    "composed-candidate.registry.json",
    "ready-scope.registry.json",
    "semantic-diff.json",
    "regeneration-queue.json",
    "quarantine.json",
    "composition-receipt.json",
    "upstream-catalogue-enrichment-evidence.json",
    "upstream-catalogue-processing-result.json",
)
C_TERMINAL_EVALUATOR_SOURCE_PATHS = (
    ".github/workflows/canonical-update-promotion.yml",
    "scripts/run-canonical-update-promotion.py",
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
    "schemas/datapan.canonical-update-promotion-terminal-outcome.v1.schema.json",
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
)
HEALTH_EVALUATOR_SOURCE_PATHS = (
    ".github/workflows/upstream-catalogue-health.yml",
    "schemas/datapan.upstream-catalogue-health.v1.schema.json",
    "schemas/datapan.upstream-catalogue-health-state.v1.schema.json",
    "schemas/datapan.upstream-catalogue-health-policy.v1.schema.json",
    *C_TERMINAL_EVALUATOR_SOURCE_PATHS,
)
LINK_CONTRACT_FAILURES = {
    "no_reviewed_declaration": {
        "unresolved_requirements": ["reviewed_operation_declaration"],
        "next_action": "review_authoritative_declaration",
    },
    "subject_binding_unproven": {
        "unresolved_requirements": ["subject_binding"],
        "next_action": "verify_subject_binding",
    },
    "declaration_evidence_rejected": {
        "unresolved_requirements": ["declaration_source_binding", "operation_contract_validation"],
        "next_action": "review_declaration_evidence",
    },
    "validation_detail_unknown": {
        "unresolved_requirements": [],
        "next_action": "inspect_bound_validation_evidence",
    },
}
C_TERMINAL_MODE_STEPS = {
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
MAX_TERMINAL_SOURCE_BYTES = 8 * 1024 * 1024
MAX_TERMINAL_API_TIMEOUT_SECONDS = 30


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_registry_identity(manifest_path: pathlib.Path, registry_path: pathlib.Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    try:
        registry_name = registry_path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("main_registry_path_outside_repository") from exc
    matches = [
        row for row in artifacts or []
        if isinstance(row, dict) and row.get("path") == registry_name and row.get("kind") == "registry"
    ]
    if len(matches) != 1:
        raise ValueError("main_registry_manifest_identity_missing")
    entry = matches[0]
    expected_bytes = entry.get("bytes")
    expected_sha256 = entry.get("sha256")
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 1 or not DIGEST.fullmatch(str(expected_sha256 or "")):
        raise ValueError("main_registry_manifest_identity_invalid")
    try:
        raw = registry_path.read_bytes()
    except OSError as exc:
        raise ValueError("main_registry_pointer_unavailable") from exc
    if raw.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        try:
            lines = raw.decode("ascii").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError("main_registry_lfs_pointer_invalid") from exc
        if len(lines) != 3 or not re.fullmatch(r"oid sha256:[a-f0-9]{64}", lines[1]) or not re.fullmatch(r"size [0-9]+", lines[2]):
            raise ValueError("main_registry_lfs_pointer_invalid")
        actual_sha256 = lines[1].removeprefix("oid sha256:")
        actual_bytes = int(lines[2].removeprefix("size "))
    else:
        actual_sha256 = sha256_bytes(raw)
        actual_bytes = len(raw)
    if actual_sha256 != expected_sha256 or actual_bytes != expected_bytes:
        raise ValueError("main_registry_manifest_mismatch")
    return {"registry_path": registry_name, "registry_bytes": actual_bytes, "registry_sha256": actual_sha256}


def git_bytes(root: pathlib.Path, revision: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{revision}:{path}"], cwd=root, capture_output=True, check=False, timeout=20,
    )
    if result.returncode != 0:
        raise ValueError("main_revision_input_unavailable")
    return result.stdout


def checked_out_main_revision(root: pathlib.Path, requested_revision: str = "") -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False, timeout=10,
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or not REVISION.fullmatch(revision):
        raise ValueError("main_revision_unavailable")
    if requested_revision and requested_revision != revision:
        raise ValueError("main_revision_does_not_match_checked_out_head")
    return revision


def verified_health_evaluator_source(root: pathlib.Path, expected_revision: str) -> str | None:
    """Return the pinned Health evaluator revision only when every loaded source file matches it."""
    if not REVISION.fullmatch(expected_revision):
        return None
    try:
        checked_out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            capture_output=True, check=False, timeout=10,
        )
        if checked_out.returncode != 0 or checked_out.stdout.strip() != expected_revision:
            return None
        for relative in HEALTH_EVALUATOR_SOURCE_PATHS:
            path = root.joinpath(*pathlib.PurePosixPath(relative).parts)
            local_bytes = path.read_bytes()
            trusted = subprocess.run(
                ["git", "show", f"{expected_revision}:{relative}"], cwd=root,
                capture_output=True, check=False, timeout=10,
            )
            if trusted.returncode != 0 or trusted.stdout != local_bytes:
                return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    return expected_revision


def verify_main_manifest_binding(
    root: pathlib.Path, revision: str, manifest_path: pathlib.Path, registry_path: pathlib.Path,
) -> dict[str, Any]:
    """Bind the strict manifest/LFS identity to the immutable checked-out main tree."""
    if checked_out_main_revision(root, revision) != revision:
        raise ValueError("main_revision_does_not_match_checked_out_head")
    try:
        manifest_name = manifest_path.resolve().relative_to(root.resolve()).as_posix()
        registry_name = registry_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("main_manifest_path_outside_repository") from exc
    try:
        working_manifest = manifest_path.read_bytes()
        working_registry = registry_path.read_bytes()
    except OSError as exc:
        raise ValueError("main_manifest_input_unavailable") from exc
    if working_manifest != git_bytes(root, revision, manifest_name):
        raise ValueError("main_manifest_not_bound_to_checked_out_head")
    head_registry = git_bytes(root, revision, registry_name)
    if head_registry.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        try:
            lines = head_registry.decode("ascii").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError("main_registry_lfs_pointer_invalid") from exc
        if len(lines) != 3 or not re.fullmatch(r"oid sha256:[a-f0-9]{64}", lines[1]) or not re.fullmatch(r"size [0-9]+", lines[2]):
            raise ValueError("main_registry_lfs_pointer_invalid")
        head_sha256 = lines[1].removeprefix("oid sha256:")
        head_bytes = int(lines[2].removeprefix("size "))
    else:
        head_sha256 = sha256_bytes(head_registry)
        head_bytes = len(head_registry)
        if working_registry != head_registry:
            raise ValueError("main_registry_not_bound_to_checked_out_head")
    identity = manifest_registry_identity(manifest_path, registry_path)
    if (identity["registry_sha256"], identity["registry_bytes"]) != (head_sha256, head_bytes):
        raise ValueError("main_registry_manifest_not_bound_to_checked_out_head")
    return identity


def load_promotion_runner() -> Any:
    spec = importlib.util.spec_from_file_location("canonical_update_promotion_health", PROMOTION_RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise ValueError("processor_candidate_verifier_unavailable")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
        raise
    return module


def _candidate_screen_result(
    *, status: str, stage: str, reason_code: str | None,
    screened: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use a closed, value-free envelope around the promotion worker's screen."""
    return {
        "status": status,
        "stage": stage,
        "reason_code": reason_code,
        "screened": screened,
    }


def _candidate_screen_status(reason_code: str | None) -> str:
    if reason_code in {"processor_run_unavailable", "processor_artifact_listing_unavailable", "processor_artifact_unavailable"}:
        return "unavailable"
    return "rejected"


def build_processor_candidate_screen(root: pathlib.Path, repository: str, main_revision: str) -> Any:
    """Use the promotion worker's full, read-only B bundle screen and preserve its reason."""
    setup_failure = _candidate_screen_result(
        status="unavailable", stage="screen_setup", reason_code="processor_screen_unavailable",
    )
    try:
        runner = load_promotion_runner()
        screen = getattr(runner, "screen_processor_recovery_candidate", None)
        if not callable(screen):
            return lambda _checkpoint: setup_failure
        compatibility_paths = getattr(runner, "PROCESSOR_COMPATIBILITY_FILES", None)
        if not isinstance(compatibility_paths, (tuple, list)) or not compatibility_paths:
            return lambda _checkpoint: setup_failure
        paths = ["manifest.json", "data/data-go-kr.registry.json", *compatibility_paths]
        diff = subprocess.run(
            ["git", "diff", "--quiet", main_revision, "--", *paths],
            cwd=root, capture_output=True, check=False, timeout=20,
        )
        if diff.returncode != 0:
            reason = "processor_screen_source_mismatch" if diff.returncode == 1 else "processor_screen_unavailable"
            failure = _candidate_screen_result(
                status="rejected" if diff.returncode == 1 else "unavailable",
                stage="trusted_source", reason_code=reason,
            )
            return lambda _checkpoint: failure
        composition_schema = load_json(COMPOSITION_SCHEMA_PATH)
    except Exception:
        return lambda _checkpoint: setup_failure

    def screen_candidate(checkpoint: dict[str, Any]) -> dict[str, Any]:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                screened, reason = screen(
                    root, repository, checkpoint,
                    default_branch="main",
                    current_head_sha=main_revision,
                    composition_schema=composition_schema,
                    composition_helper=PROMOTION,
                )
        except Exception:
            return _candidate_screen_result(
                status="unavailable", stage="processor_bundle_screen",
                reason_code="processor_screen_unavailable",
            )
        if isinstance(screened, dict):
            return _candidate_screen_result(
                status="verified", stage="complete", reason_code=None, screened=screened,
            )
        stable_reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,96}", reason) else "processor_screen_rejected"
        return _candidate_screen_result(
            status=_candidate_screen_status(stable_reason),
            stage="processor_bundle_screen", reason_code=stable_reason,
        )

    return screen_candidate


def already_canonical_candidate_relation(
    checkpoint: dict[str, Any] | None, main_identity: dict[str, Any], main_revision: str,
    mode: str, candidate_screen: Any | None, *, diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a relation only when the complete B screen proves exact current-main bytes."""
    def reject(reason_code: str, stage: str) -> None:
        if diagnostic is not None:
            diagnostic.update({"status": "rejected", "stage": stage, "reason_code": reason_code})

    if (
        mode != "live"
        or checkpoint is None
        or checkpoint.get("status") not in {"ready", "no-change"}
        or not callable(candidate_screen)
    ):
        if diagnostic is not None:
            diagnostic.update({"status": "not_applicable", "stage": "eligibility", "reason_code": "candidate_not_screenable"})
        return None
    try:
        screen_result = candidate_screen(checkpoint)
        if isinstance(screen_result, dict) and "status" in screen_result and "screened" in screen_result:
            if screen_result.get("status") != "verified":
                status = screen_result.get("status")
                if status not in {"rejected", "unavailable", "not_applicable"}:
                    status = "unavailable"
                if diagnostic is not None:
                    diagnostic.update({
                        "status": status,
                        "stage": screen_result.get("stage", "processor_bundle_screen"),
                        "reason_code": screen_result.get("reason_code") or "processor_screen_unavailable",
                    })
                return None
            screened = screen_result.get("screened")
        else:
            # Compatibility for existing read-only callers/tests that provide
            # the previously documented successful screened bundle mapping.
            screened = screen_result
        if not isinstance(screened, dict):
            reject("processor_screen_result_invalid", "processor_bundle_screen")
            return None
        bundle = screened.get("bundle")
        run = screened.get("run")
        locator = checkpoint.get("output_artifact")
        if not isinstance(bundle, dict) or not isinstance(run, dict) or not isinstance(locator, dict):
            reject("candidate_relation_identity_invalid", "producer_identity")
            return None
        generation_id = checkpoint.get("generation_id")
        checkpoint_sha = checkpoint.get("checkpoint_sha256")
        generation_inputs = checkpoint.get("generation_inputs")
        artifact_id = str(locator.get("artifact_id", ""))
        bundle_sha256 = locator.get("bundle_manifest_sha256")
        run_id = str(locator.get("run_id", ""))
        name_match = re.fullmatch(r"upstream-catalogue-processing-([0-9]{6,20})-([1-9][0-9]*)", str(locator.get("name", "")))
        if (
            not isinstance(generation_id, str) or not DIGEST.fullmatch(generation_id)
            or not isinstance(checkpoint_sha, str) or not DIGEST.fullmatch(checkpoint_sha)
            or not isinstance(generation_inputs, dict)
            or generation_inputs.get("source_id") != "data_go_kr"
            or generation_inputs.get("source_scope") != "aggregate_supported_catalog"
            or sha256_bytes(canonical_json(generation_inputs)) != generation_id
            or checkpoint.get("source_id") != "data_go_kr"
            or checkpoint.get("source_scope") != "aggregate_supported_catalog"
            or not isinstance(bundle_sha256, str) or not DIGEST.fullmatch(bundle_sha256)
            or screened.get("generation_id") != generation_id
            or screened.get("run_id") != run_id
            or not name_match or name_match.group(1) != run_id
            or str(screened.get("attempt", "")) != name_match.group(2)
            or str(run.get("id", "")) != run_id
            or str(run.get("run_attempt", "")) != name_match.group(2)
            or str(screened.get("artifact_id", "")) != artifact_id
            or not artifact_id.isdigit()
            or bundle.get("status") != checkpoint.get("status")
            or bundle.get("registry_path") != main_identity.get("registry_path")
        ):
            reject("candidate_relation_identity_mismatch", "producer_identity")
            return None
        bundle_dir = pathlib.Path(str(bundle.get("composition_outputs_dir", "")))
        candidate_path = bundle_dir / "composed-candidate.registry.json"
        if not candidate_path.is_file() or candidate_path.is_symlink():
            reject("candidate_composition_unavailable", "candidate_payload")
            return None
        composed_bytes = candidate_path.stat().st_size
        composed_sha256 = file_sha256(candidate_path)
        if (
            isinstance(bundle.get("registry_bytes"), bool)
            or bundle.get("registry_bytes") != composed_bytes
            or bundle.get("registry_sha256") != composed_sha256
            or composed_bytes != main_identity.get("registry_bytes")
            or composed_sha256 != main_identity.get("registry_sha256")
            or not REVISION.fullmatch(main_revision)
            or main_identity.get("revision") != main_revision
            or not DIGEST.fullmatch(str(main_identity.get("manifest_sha256", "")))
        ):
            if bundle.get("baseline_sha256") == main_identity.get("registry_sha256"):
                reject("candidate_payload_requires_promotion", "canonical_payload")
            else:
                reject("candidate_baseline_stale_for_current_main", "canonical_payload")
            return None
        if diagnostic is not None:
            diagnostic.update({"status": "verified", "stage": "complete", "reason_code": None})
        return {
            "verified": True,
            "source_id": checkpoint.get("source_id"),
            "generation_id": generation_id,
            "checkpoint_sha256": checkpoint_sha,
            "processor_run_id": run_id,
            "processor_run_attempt": int(name_match.group(2)),
            "artifact_id": artifact_id,
            "output_bundle_sha256": bundle_sha256,
            "composed_registry_bytes": composed_bytes,
            "composed_registry_sha256": composed_sha256,
            "main_revision": main_revision,
            "main_manifest_sha256": main_identity["manifest_sha256"],
        }
    except Exception:
        reject("candidate_relation_unavailable", "candidate_relation")
        return None


def current_candidate_evaluation_record(
    checkpoint: dict[str, Any] | None,
    main_identity: dict[str, Any],
    main_revision: str,
    mode: str,
    diagnostic: dict[str, Any],
    screen_result: Any,
    evaluator_source_sha: str | None,
) -> dict[str, Any]:
    """Serialize the current screen outcome without embedding bundle paths or exception text."""
    allowed_statuses = {"verified", "rejected", "unavailable", "not_applicable"}
    status = diagnostic.get("status")
    if status not in allowed_statuses:
        status = "unavailable"
    reason_code = diagnostic.get("reason_code")
    if reason_code is not None and (not isinstance(reason_code, str) or not re.fullmatch(r"[a-z0-9_]{1,96}", reason_code)):
        reason_code = "processor_screen_unavailable"
    stage = diagnostic.get("stage")
    if not isinstance(stage, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", stage):
        stage = "screen_setup"

    checkpoint_value = checkpoint if isinstance(checkpoint, dict) else {}
    locator = checkpoint_value.get("output_artifact") if isinstance(checkpoint_value.get("output_artifact"), dict) else {}
    artifact_name = locator.get("name")
    name_match = re.fullmatch(r"upstream-catalogue-processing-([0-9]{1,20})-([1-9][0-9]*)", str(artifact_name or ""))
    producer_run_id = str(locator.get("run_id", ""))
    run_attempt: int | None = int(name_match.group(2)) if name_match and name_match.group(1) == producer_run_id else None
    screened = screen_result.get("screened") if isinstance(screen_result, dict) and "screened" in screen_result else None
    screened_run = screened.get("run") if isinstance(screened, dict) and isinstance(screened.get("run"), dict) else {}
    producer = {
        "generation_id": checkpoint_value.get("generation_id"),
        "checkpoint_sha256": checkpoint_value.get("checkpoint_sha256"),
        "run_id": producer_run_id if re.fullmatch(r"[0-9]{1,20}", producer_run_id) else None,
        "run_attempt": run_attempt,
        "head_sha": screened_run.get("head_sha") if isinstance(screened_run.get("head_sha"), str) and REVISION.fullmatch(screened_run.get("head_sha", "")) else None,
        "artifact_id": str(locator.get("artifact_id")) if str(locator.get("artifact_id", "")).isdigit() else None,
        "bundle_manifest_sha256": locator.get("bundle_manifest_sha256"),
    }
    if not isinstance(producer["generation_id"], str) or not DIGEST.fullmatch(producer["generation_id"]):
        producer["generation_id"] = None
    if not isinstance(producer["checkpoint_sha256"], str) or not DIGEST.fullmatch(producer["checkpoint_sha256"]):
        producer["checkpoint_sha256"] = None
    if not isinstance(producer["bundle_manifest_sha256"], str) or not DIGEST.fullmatch(producer["bundle_manifest_sha256"]):
        producer["bundle_manifest_sha256"] = None

    evaluated_main = None
    if (
        isinstance(main_identity, dict)
        and main_identity.get("revision") == main_revision
        and REVISION.fullmatch(main_revision)
        and DIGEST.fullmatch(str(main_identity.get("manifest_sha256", "")))
        and isinstance(main_identity.get("registry_path"), str)
        and isinstance(main_identity.get("registry_bytes"), int)
        and not isinstance(main_identity.get("registry_bytes"), bool)
        and main_identity.get("registry_bytes", 0) > 0
        and DIGEST.fullmatch(str(main_identity.get("registry_sha256", "")))
    ):
        evaluated_main = {
            "revision": main_revision,
            "manifest_sha256": main_identity["manifest_sha256"],
            "registry_path": main_identity["registry_path"],
            "registry_bytes": main_identity["registry_bytes"],
            "registry_sha256": main_identity["registry_sha256"],
        }
    revalidation_required = mode == "live" and (
        status == "unavailable"
        or (status == "rejected" and reason_code != "candidate_payload_requires_promotion")
    )
    return {
        "status": status,
        "stage": stage,
        "reason_code": reason_code,
        "revalidation_required": revalidation_required,
        "evaluator_source_sha": (
            evaluator_source_sha
            if mode == "live" and isinstance(evaluator_source_sha, str) and REVISION.fullmatch(evaluator_source_sha)
            else None
        ),
        "evaluated_main": evaluated_main,
        "producer": producer if producer["generation_id"] is not None else None,
        "evaluation_mode": mode,
    }


def link_contract_diagnostic_projection(
    checkpoint: dict[str, Any] | None,
    screen_result: Any,
    current_evaluation: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    """Project only C-admitted, privacy-bounded LINK diagnostics into Health."""
    schema_version = "datapan.upstream-catalogue-link-contract-diagnostics.v1"

    def unavailable(status: str, applicability: str, reason_code: str) -> dict[str, Any]:
        return {
            "schema_version": schema_version,
            "status": status,
            "applicability": applicability,
            "reason_code": reason_code,
            "generation_id": None,
            "checkpoint_sha256": None,
            "producer": None,
            "records": [],
        }

    if (
        mode != "live"
        or not isinstance(checkpoint, dict)
        or checkpoint.get("status") not in {"ready", "no-change"}
    ):
        return unavailable("not_applicable", "not_applicable", "candidate_not_screenable")

    envelope = screen_result if isinstance(screen_result, dict) else {}
    if "status" in envelope and "screened" in envelope:
        screen_status = envelope.get("status")
        screened = envelope.get("screened")
        reason_code = envelope.get("reason_code")
    else:
        # Preserve compatibility with the original read-only test seam while
        # requiring the same complete screened mapping below.
        screen_status = "verified" if isinstance(screen_result, dict) else "unavailable"
        screened = screen_result
        reason_code = None
    if screen_status != "verified":
        safe_status = screen_status if screen_status in {"rejected", "unavailable"} else "unavailable"
        safe_reason = (
            reason_code
            if isinstance(reason_code, str) and re.fullmatch(r"[a-z0-9_]{1,96}", reason_code)
            else "processor_screen_unavailable"
        )
        return unavailable(safe_status, "unavailable", safe_reason)

    if not isinstance(screened, dict):
        return unavailable("rejected", "unavailable", "contract_diagnostic_projection_invalid")
    bundle = screened.get("bundle")
    records = bundle.get("contract_diagnostics") if isinstance(bundle, dict) else None
    producer = current_evaluation.get("producer") if isinstance(current_evaluation, dict) else None
    locator = checkpoint.get("output_artifact")
    screened_run = screened.get("run")
    generation_id = checkpoint.get("generation_id")
    checkpoint_sha256 = checkpoint.get("checkpoint_sha256")
    name_match = re.fullmatch(
        r"upstream-catalogue-processing-([0-9]{1,20})-([1-9][0-9]*)",
        str(locator.get("name", "")) if isinstance(locator, dict) else "",
    )
    if (
        not isinstance(bundle, dict)
        or not isinstance(records, list)
        or len(records) > 128
        or not isinstance(producer, dict)
        or not isinstance(locator, dict)
        or not isinstance(screened_run, dict)
        or not name_match
        or not isinstance(generation_id, str) or not DIGEST.fullmatch(generation_id)
        or not isinstance(checkpoint_sha256, str) or not DIGEST.fullmatch(checkpoint_sha256)
        or producer.get("generation_id") != generation_id
        or producer.get("checkpoint_sha256") != checkpoint_sha256
        or producer.get("bundle_manifest_sha256") != locator.get("bundle_manifest_sha256")
        or producer.get("artifact_id") != str(locator.get("artifact_id"))
        or producer.get("run_id") != str(locator.get("run_id"))
        or producer.get("run_id") != name_match.group(1)
        or producer.get("run_attempt") != int(name_match.group(2))
        or not isinstance(producer.get("head_sha"), str)
        or not REVISION.fullmatch(producer["head_sha"])
        or str(screened_run.get("id", "")) != producer.get("run_id")
        or screened_run.get("run_attempt") != producer.get("run_attempt")
        or screened_run.get("head_sha") != producer.get("head_sha")
        or screened.get("generation_id") != generation_id
        or screened.get("artifact_id") != str(locator.get("artifact_id"))
    ):
        return unavailable("rejected", "unavailable", "contract_diagnostic_projection_invalid")

    evaluation_status = current_evaluation.get("status")
    evaluation_reason = current_evaluation.get("reason_code")
    if evaluation_status == "verified":
        applicability = "current"
        projection_reason = None
    elif evaluation_status == "rejected" and evaluation_reason == "candidate_payload_requires_promotion":
        applicability = "current"
        projection_reason = evaluation_reason
    elif evaluation_status == "rejected" and evaluation_reason == "candidate_baseline_stale_for_current_main":
        applicability = "historical"
        projection_reason = evaluation_reason
    else:
        return unavailable("rejected", "unavailable", "contract_diagnostic_projection_context_invalid")

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    try:
        for row in records:
            allowed = {
                "api_key", "worker_status", "source_sha256", "guide_sha256",
                "worker_outcome_sha256", "detail_status", "next_action",
            }
            if not isinstance(row, dict):
                raise ValueError("record_not_object")
            if "contract_failure" in row:
                allowed.add("contract_failure")
            if set(row) != allowed:
                raise ValueError("record_shape")
            api_key = row.get("api_key")
            provider = api_key.get("provider") if isinstance(api_key, dict) else None
            identity = api_key.get("id") if isinstance(api_key, dict) else None
            key = (str(provider), str(identity))
            if (
                provider != "data.go.kr"
                or not isinstance(identity, str) or not identity or len(identity) > 128
                or any(ord(character) < 32 or ord(character) == 127 for character in identity)
                or key in seen
                or row.get("worker_status") not in {"retry", "quarantined"}
                or any(
                    not isinstance(row.get(field), str) or not DIGEST.fullmatch(row[field])
                    for field in ("source_sha256", "worker_outcome_sha256")
                )
                or (
                    row.get("guide_sha256") is not None
                    and (
                        not isinstance(row.get("guide_sha256"), str)
                        or not DIGEST.fullmatch(row["guide_sha256"])
                    )
                )
            ):
                raise ValueError("record_identity_or_digest")
            detail_status = row.get("detail_status")
            if detail_status == "verified":
                contract_failure = row.get("contract_failure")
                reason = contract_failure.get("reason") if isinstance(contract_failure, dict) else None
                mapping = LINK_CONTRACT_FAILURES.get(reason)
                expected = {
                    "version": 1,
                    "reason": reason,
                    "unresolved_requirements": list(mapping["unresolved_requirements"]) if mapping else None,
                    "next_action": mapping["next_action"] if mapping else None,
                }
                if not mapping or contract_failure != expected or row.get("next_action") != mapping["next_action"]:
                    raise ValueError("record_contract_failure")
            elif detail_status == "legacy_detail_unknown":
                if "contract_failure" in row or row.get("next_action") != "inspect_bound_validation_evidence":
                    raise ValueError("record_legacy_detail")
            else:
                raise ValueError("record_detail_status")
            seen.add(key)
            normalized.append(copy.deepcopy(row))
    except (AttributeError, TypeError, ValueError):
        return unavailable("rejected", "unavailable", "contract_diagnostic_projection_invalid")

    ordered = sorted(normalized, key=lambda row: (row["api_key"]["provider"], row["api_key"]["id"]))
    if normalized != ordered:
        return unavailable("rejected", "unavailable", "contract_diagnostic_projection_unsorted")
    return {
        "schema_version": schema_version,
        "status": "verified",
        "applicability": applicability,
        "reason_code": projection_reason,
        "generation_id": generation_id,
        "checkpoint_sha256": checkpoint_sha256,
        "producer": copy.deepcopy(producer),
        "records": ordered,
    }


def load_json(path: pathlib.Path, *, maximum_bytes: int = 4 * 1024 * 1024) -> Any:
    if path.stat().st_size > maximum_bytes:
        raise ValueError(f"input_too_large:{path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid_timestamp:{label}")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid_timestamp:{label}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timezone_required:{label}")
    return parsed.astimezone(dt.timezone.utc)


def utc_timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def seconds_since(now: dt.datetime, value: Any, label: str, maximum_future_skew: int) -> int:
    parsed = parse_time(value, label)
    delta = (now - parsed).total_seconds()
    if delta < -maximum_future_skew:
        raise ValueError(f"future_timestamp:{label}")
    return max(0, int(delta))


def cadence_interval_seconds(cron: str) -> int:
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError("source_cadence_unsupported")
    minute, hour, day_of_month, month, day_of_week = fields
    if not minute.isdigit() or not hour.isdigit() or day_of_month != "*" or month != "*":
        raise ValueError("source_cadence_unsupported")
    if not 0 <= int(minute) <= 59 or not 0 <= int(hour) <= 23:
        raise ValueError("source_cadence_unsupported")
    if day_of_week == "*":
        return 24 * 60 * 60
    if day_of_week.isdigit() and 0 <= int(day_of_week) <= 6:
        return 7 * 24 * 60 * 60
    raise ValueError("source_cadence_unsupported")


def fault(
    source_id: str, stage: str, reason: str, severity: str, owner_ticket: int, action: str,
    failure_identity: str = "",
) -> dict[str, Any]:
    key_material = {
        "source_id": source_id, "stage": stage, "reason": reason,
        "owner_ticket": owner_ticket, "failure_identity": failure_identity,
    }
    return {
        "source_id": source_id,
        "stage": stage,
        "reason": reason,
        "severity": severity,
        "owner_ticket": owner_ticket,
        "fault_key": sha256_bytes(canonical_json(key_material)),
        "recommended_action": action,
    }


def seal_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    receipt["receipt_sha256"] = sha256_bytes(canonical_json(unsigned))
    return receipt


def verify_sealed(value: Any, digest_field: str) -> bool:
    if not isinstance(value, dict):
        return False
    claimed = value.get(digest_field)
    if not isinstance(claimed, str) or not DIGEST.fullmatch(claimed):
        return False
    unsigned = dict(value)
    unsigned.pop(digest_field, None)
    return claimed == sha256_bytes(canonical_json(unsigned))


def gh_json(endpoint: str) -> Any:
    try:
        result = subprocess.run(
            ["gh", "api", endpoint], text=True, capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("github_api_unavailable") from exc
    if result.returncode != 0:
        raise RuntimeError("github_api_unavailable")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("github_api_invalid_json") from exc


def collect_workflow_runs(repository: str, workflow_path: str, limit: int) -> list[dict[str, Any]]:
    workflow_filename = pathlib.PurePosixPath(workflow_path).name
    if not workflow_filename.endswith((".yml", ".yaml")):
        raise RuntimeError("collector_workflow_filename_invalid")
    payload = gh_json(f"repos/{repository}/actions/workflows/{workflow_filename}/runs?per_page={limit}")
    rows = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("github_workflow_runs_missing")
    return [row for row in rows if isinstance(row, dict)]


def collect_workflow_identity(repository: str, workflow_path: str) -> int:
    """Resolve the configured workflow path to GitHub's authoritative workflow ID."""
    workflow_filename = pathlib.PurePosixPath(workflow_path).name
    if not workflow_filename.endswith((".yml", ".yaml")):
        raise RuntimeError("collector_workflow_filename_invalid")
    payload = gh_json(f"repos/{repository}/actions/workflows/{workflow_filename}")
    workflow_id = payload.get("id") if isinstance(payload, dict) else None
    if (
        isinstance(workflow_id, bool)
        or not isinstance(workflow_id, int)
        or workflow_id < 1
        or not isinstance(payload.get("path"), str)
        or not workflow_path_matches(payload["path"], workflow_path)
    ):
        raise RuntimeError("github_workflow_identity_invalid")
    return workflow_id


def collect_run_artifacts(repository: str, run_id: str) -> list[dict[str, Any]]:
    if not run_id.isdigit():
        return []
    payload = gh_json(f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100")
    rows = payload.get("artifacts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("github_run_artifacts_missing")
    return [row for row in rows if isinstance(row, dict)]


def collect_run(repository: str, run_id: str) -> dict[str, Any]:
    if not run_id.isdigit():
        raise RuntimeError("github_workflow_run_id_invalid")
    payload = gh_json(f"repos/{repository}/actions/runs/{run_id}")
    if not isinstance(payload, dict):
        raise RuntimeError("github_workflow_run_missing")
    return payload


def collect_run_attempt(repository: str, run_id: str, run_attempt: int) -> dict[str, Any]:
    if not run_id.isdigit() or isinstance(run_attempt, bool) or run_attempt < 1:
        raise RuntimeError("github_workflow_run_attempt_invalid")
    payload = gh_json(f"repos/{repository}/actions/runs/{run_id}/attempts/{run_attempt}")
    if not isinstance(payload, dict):
        raise RuntimeError("github_workflow_run_attempt_missing")
    return payload


def collect_run_attempt_jobs(repository: str, run_id: str, run_attempt: int) -> dict[str, Any]:
    """Fetch a bounded, exact-attempt job list for stable completion ordering."""
    if not run_id.isdigit() or isinstance(run_attempt, bool) or run_attempt < 1:
        raise RuntimeError("github_workflow_run_attempt_invalid")
    jobs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    expected_count: int | None = None
    endpoint = f"repos/{repository}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
    for page in range(1, MAX_PROMOTION_JOB_PAGES + 1):
        payload = gh_json(f"{endpoint}?per_page=100&page={page}")
        rows = payload.get("jobs") if isinstance(payload, dict) else None
        total_count = payload.get("total_count") if isinstance(payload, dict) else None
        if (
            not isinstance(rows, list)
            or isinstance(total_count, bool)
            or not isinstance(total_count, int)
            or total_count < 1
            or total_count > MAX_PROMOTION_JOBS
        ):
            raise RuntimeError("github_workflow_attempt_jobs_invalid")
        if expected_count is None:
            expected_count = total_count
        elif total_count != expected_count:
            raise RuntimeError("github_workflow_attempt_jobs_count_changed")
        if not rows and len(jobs) < expected_count:
            raise RuntimeError("github_workflow_attempt_jobs_incomplete")
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("github_workflow_attempt_job_invalid")
            job_id = row.get("id")
            if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1 or str(job_id) in seen_ids:
                raise RuntimeError("github_workflow_attempt_job_identity_invalid")
            seen_ids.add(str(job_id))
            jobs.append(row)
        if len(jobs) > expected_count:
            raise RuntimeError("github_workflow_attempt_jobs_excess")
        if len(jobs) == expected_count:
            return {
                "attempt_number": run_attempt,
                "jobs_api_endpoint": endpoint,
                "job_count": expected_count,
                "jobs": jobs,
            }
    raise RuntimeError("github_workflow_attempt_jobs_page_limit_exceeded")


def collect_run_attempt_evidence(repository: str, run_id: str, run_attempt: int) -> dict[str, Any]:
    run = collect_run_attempt(repository, run_id, run_attempt)
    jobs = collect_run_attempt_jobs(repository, run_id, run_attempt)
    return {"run": run, **jobs}


def _terminal_result_stub(
    status: str, reason_code: str, *, invocation: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(invocation, dict):
        run_id = invocation.get("run_id")
        attempt = invocation.get("run_attempt")
        repository = invocation.get("repository")
        event = invocation.get("event")
        head_sha = invocation.get("head_sha")
        if not (
            isinstance(repository, str) and re.fullmatch(r"[^\s/]+/[^\s/]+", repository)
            and isinstance(run_id, str) and run_id.isdigit() and 0 < len(run_id) <= 20
            and isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0
            and event in {"schedule", "workflow_run", "workflow_dispatch"}
            and isinstance(head_sha, str) and REVISION.fullmatch(head_sha)
            and invocation.get("mode") in TERMINAL_EVIDENCE.MODE_STEPS
        ):
            invocation = None
    return {
        "status": status,
        "reason_code": reason_code,
        "invocation": invocation,
        "artifact": None,
        "execution_status": None,
        "started_at": None,
        "completed_at": None,
        "failure_code": None,
        "outcome": None,
        "run_status": run.get("status") if isinstance(run, dict) and isinstance(run.get("status"), str) and len(run["status"]) <= 32 else None,
        "run_conclusion": run.get("conclusion") if isinstance(run, dict) and isinstance(run.get("conclusion"), str) and len(run["conclusion"]) <= 32 else None,
        "mode_step_conclusion": None,
        "current_applicability": {
            "status": "not_checked", "reason_code": reason_code, "matching_generations": [],
        },
    }


def _bounded_gh_archive(endpoint: str, maximum_bytes: int) -> bytes:
    """Read one GH artifact ZIP without allowing an unbounded subprocess output."""
    if (
        not isinstance(endpoint, str) or not endpoint.startswith("repos/")
        or "\n" in endpoint or maximum_bytes < 1
    ):
        raise RuntimeError("terminal_archive_request_invalid")
    argv = [
        "gh", "api", "--header", "X-GitHub-Api-Version: 2022-11-28",
        "--header", "Accept: application/vnd.github+json", endpoint,
    ]
    try:
        process = subprocess.Popen(
            argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
    except OSError as exc:
        raise RuntimeError("terminal_archive_unavailable") from exc
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    chunks: list[bytes] = []
    total = 0
    eof = False
    deadline = time.monotonic() + MAX_TERMINAL_API_TIMEOUT_SECONDS
    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("terminal_archive_timeout")
            if not eof:
                events = selector.select(min(remaining, 0.25))
                if events:
                    chunk = os.read(process.stdout.fileno(), min(65536, maximum_bytes + 1 - total))
                    if not chunk:
                        eof = True
                        selector.unregister(process.stdout)
                    else:
                        total += len(chunk)
                        if total > maximum_bytes:
                            raise RuntimeError("terminal_archive_size_limit")
                        chunks.append(chunk)
            if process.poll() is not None and eof:
                break
        if process.returncode != 0 or total == 0:
            raise RuntimeError("terminal_archive_unavailable")
        return b"".join(chunks)
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        raise
    finally:
        selector.close()
        process.stdout.close()


def collect_terminal_artifact_inventory(repository: str, run_id: str) -> dict[str, Any]:
    """Fetch a complete, bounded exact-run artifact listing and detail snapshots."""
    if not run_id.isdigit():
        raise RuntimeError("terminal_run_id_invalid")
    endpoint = f"repos/{repository}/actions/runs/{run_id}/artifacts"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_count: int | None = None
    for page in range(1, MAX_TERMINAL_ARTIFACT_PAGES + 1):
        payload = gh_json(f"{endpoint}?per_page=100&page={page}")
        page_rows = payload.get("artifacts") if isinstance(payload, dict) else None
        total_count = payload.get("total_count") if isinstance(payload, dict) else None
        if (
            not isinstance(page_rows, list)
            or isinstance(total_count, bool) or not isinstance(total_count, int)
            or total_count < 0 or total_count > MAX_TERMINAL_ARTIFACTS
        ):
            raise RuntimeError("terminal_artifact_listing_invalid")
        if expected_count is None:
            expected_count = total_count
        elif expected_count != total_count:
            raise RuntimeError("terminal_artifact_listing_changed")
        if not page_rows and len(rows) < total_count:
            raise RuntimeError("terminal_artifact_listing_incomplete")
        for row in page_rows:
            if not isinstance(row, dict):
                raise RuntimeError("terminal_artifact_listing_invalid")
            artifact_id = row.get("id")
            if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id < 1 or str(artifact_id) in seen:
                raise RuntimeError("terminal_artifact_listing_identity_invalid")
            seen.add(str(artifact_id))
            rows.append(row)
        if len(rows) > total_count:
            raise RuntimeError("terminal_artifact_listing_excess")
        if len(rows) == total_count:
            break
    else:
        raise RuntimeError("terminal_artifact_listing_page_limit")
    if expected_count is None or len(rows) != expected_count:
        raise RuntimeError("terminal_artifact_listing_incomplete")
    return {"run_id": run_id, "total_count": expected_count, "artifacts": rows, "details_by_id": {}}


def _terminal_git_blob(root: pathlib.Path, source_sha: str, relative: str) -> bytes:
    path = pathlib.PurePosixPath(relative)
    if (
        not REVISION.fullmatch(source_sha)
        or path.is_absolute()
        or path.as_posix() != relative
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in relative
    ):
        raise RuntimeError("terminal_source_path_invalid")
    try:
        entry = subprocess.run(
            ["git", "ls-tree", "-z", source_sha, "--", relative], cwd=root,
            capture_output=True, check=False, timeout=15,
        )
        if entry.returncode != 0:
            raise RuntimeError("terminal_source_tree_entry_unavailable")
        entries = [row for row in entry.stdout.split(b"\0") if row]
        if len(entries) != 1 or b"\t" not in entries[0]:
            raise RuntimeError("terminal_source_tree_entry_invalid")
        metadata, raw_path = entries[0].split(b"\t", 1)
        fields = metadata.split()
        if (
            len(fields) != 3
            or fields[0] not in {b"100644", b"100755"}
            or fields[1] != b"blob"
            or raw_path.decode("utf-8") != relative
        ):
            raise RuntimeError("terminal_source_tree_entry_not_regular_blob")
        result = subprocess.run(
            ["git", "show", f"{source_sha}:{relative}"], cwd=root,
            capture_output=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        raise RuntimeError("terminal_source_unavailable") from exc
    if result.returncode != 0 or len(result.stdout) > MAX_TERMINAL_SOURCE_BYTES:
        raise RuntimeError("terminal_source_unavailable")
    return result.stdout


def _terminal_runner_declared_paths(runner_bytes: bytes) -> tuple[str, ...]:
    """Read one top-level literal-only C dependency tuple without executing source."""
    try:
        tree = ast.parse(runner_bytes.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise RuntimeError("terminal_source_runner_invalid") from exc

    def binds(node: ast.AST) -> bool:
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        return any(
            isinstance(target, ast.Name) and target.id == "TERMINAL_EVALUATOR_SOURCE_PATHS"
            for target in targets
        )

    declarations = [node for node in ast.walk(tree) if binds(node)]
    top_level = [node for node in tree.body if binds(node)]
    if len(declarations) != 1 or len(top_level) != 1 or declarations[0] is not top_level[0]:
        raise RuntimeError("terminal_source_runner_dependency_declaration_invalid")
    node = top_level[0]
    value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
    if not isinstance(value, ast.Tuple) or not value.elts:
        raise RuntimeError("terminal_source_runner_dependency_tuple_invalid")
    paths: list[str] = []
    for element in value.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            raise RuntimeError("terminal_source_runner_dependency_not_literal")
        path_text = element.value
        path = pathlib.PurePosixPath(path_text)
        if (
            path.is_absolute()
            or path.as_posix() != path_text
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in path_text
        ):
            raise RuntimeError("terminal_source_runner_dependency_path_invalid")
        paths.append(path_text)
    if len(paths) != len(set(paths)):
        raise RuntimeError("terminal_source_runner_dependency_duplicate")
    return tuple(paths)


def _terminal_workflow_contract(workflow_bytes: bytes) -> dict[str, dict[str, str]]:
    """Check the pinned C workflow's exact mode, output and always-upload steps."""
    try:
        import yaml

        workflow = yaml.load(workflow_bytes.decode("utf-8"), Loader=yaml.BaseLoader)
    except Exception as exc:
        raise RuntimeError("terminal_workflow_source_invalid") from exc
    if not isinstance(workflow, dict):
        raise RuntimeError("terminal_workflow_source_invalid")
    jobs = workflow.get("jobs")
    reconcile = jobs.get("reconcile") if isinstance(jobs, dict) else None
    steps = reconcile.get("steps") if isinstance(reconcile, dict) else None
    if not isinstance(steps, list):
        raise RuntimeError("terminal_workflow_step_contract_missing")
    checkout = [row for row in steps if isinstance(row, dict) and row.get("uses", "").startswith("actions/checkout@")]
    if not checkout or checkout[0].get("with", {}).get("ref") != "${{ github.sha }}":
        raise RuntimeError("terminal_workflow_checkout_contract_mismatch")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        if isinstance(step, dict) and isinstance(step.get("name"), str):
            by_name.setdefault(step["name"], []).append(step)
    result: dict[str, dict[str, str]] = {}
    for mode, contract in C_TERMINAL_MODE_STEPS.items():
        invocations = by_name.get(contract["invocation_step"], [])
        uploads = by_name.get(contract["upload_step"], [])
        if len(invocations) != 1 or len(uploads) != 1:
            raise RuntimeError("terminal_workflow_step_contract_mismatch")
        invocation = invocations[0]
        upload = uploads[0]
        run_text = invocation.get("run")
        upload_with = upload.get("with")
        if (
            not isinstance(run_text, str)
            or f"--mode {mode}" not in run_text
            or "--terminal-outcome-output" not in run_text
            or upload.get("if") != "always()"
            or not isinstance(upload.get("uses"), str)
            or not upload["uses"].startswith("actions/upload-artifact@")
            or not isinstance(upload_with, dict)
            or upload_with.get("name") != f"canonical-update-promotion-terminal-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}-{mode}"
            or upload_with.get("if-no-files-found") != "ignore"
            or upload_with.get("include-hidden-files") != "true"
        ):
            raise RuntimeError("terminal_workflow_step_contract_mismatch")
        upload_path = upload_with.get("path")
        if not isinstance(upload_path, str) or "terminal-outcome.json" not in upload_path or "terminal-outcome.sha256" not in upload_path:
            raise RuntimeError("terminal_workflow_upload_contract_mismatch")
        result[mode] = dict(contract)
    helper_modes = TERMINAL_EVIDENCE.MODE_STEPS
    if any(helper_modes.get(mode) != contract for mode, contract in result.items()):
        raise RuntimeError("terminal_health_helper_mode_contract_mismatch")
    return {mode: dict(contract) for mode, contract in helper_modes.items()}


def terminal_source_contract(
    root: pathlib.Path, repository: str, workflow_id: int, source_sha: str,
    current_main_sha: str,
) -> dict[str, Any]:
    """Build a C source contract only from exact, pinned Git blobs and workflow structure."""
    if not REVISION.fullmatch(source_sha) or not REVISION.fullmatch(current_main_sha):
        raise RuntimeError("terminal_source_revision_invalid")
    try:
        commit = subprocess.run(
            ["git", "cat-file", "-e", f"{source_sha}^{{commit}}"], cwd=root,
            capture_output=True, check=False, timeout=10,
        )
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_sha, current_main_sha], cwd=root,
            capture_output=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("terminal_source_history_unavailable") from exc
    if commit.returncode != 0 or ancestor.returncode != 0:
        raise RuntimeError("terminal_source_not_in_trusted_main_history")
    helper_paths = set(TERMINAL_EVIDENCE.EVALUATOR_SOURCE_PATHS)
    if helper_paths != set(C_TERMINAL_EVALUATOR_SOURCE_PATHS):
        raise RuntimeError("terminal_health_helper_source_set_mismatch")
    source_files: dict[str, bytes] = {}
    total_bytes = 0
    for relative in sorted(helper_paths):
        raw = _terminal_git_blob(root, source_sha, relative)
        total_bytes += len(raw)
        if total_bytes > 32 * 1024 * 1024:
            raise RuntimeError("terminal_source_closure_size_limit")
        source_files[relative] = raw
    declared_paths = _terminal_runner_declared_paths(source_files["scripts/run-canonical-update-promotion.py"])
    if declared_paths != C_TERMINAL_EVALUATOR_SOURCE_PATHS:
        raise RuntimeError("terminal_source_runner_dependency_set_mismatch")
    mode_steps = _terminal_workflow_contract(source_files[".github/workflows/canonical-update-promotion.yml"])
    if any(mode_steps.get(mode) != contract for mode, contract in C_TERMINAL_MODE_STEPS.items()):
        raise RuntimeError("terminal_source_mode_contract_mismatch")
    schema_path = TERMINAL_EVIDENCE.SCHEMA_PATH
    schema_bytes = source_files[schema_path]
    return {
        "repository": repository,
        "workflow_id": workflow_id,
        "workflow_path": TERMINAL_EVIDENCE.WORKFLOW_PATH,
        "source_sha": source_sha,
        "schema_path": schema_path,
        "schema_bytes": schema_bytes,
        "schema_sha256": sha256_bytes(schema_bytes),
        "source_files": {path: sha256_bytes(raw) for path, raw in source_files.items()},
        "mode_steps": mode_steps,
    }


def _terminal_run_identity(run: dict[str, Any]) -> dict[str, Any]:
    identity = {
        key: run.get(key) for key in (
            "id", "run_attempt", "workflow_id", "path", "event", "head_sha",
            "head_branch", "status", "conclusion", "created_at", "run_started_at",
        )
    }
    for key in ("repository", "head_repository"):
        value = run.get(key)
        identity[key] = {
            nested: value.get(nested) for nested in ("id", "full_name")
        } if isinstance(value, dict) else None
    return identity


def _terminal_jobs_identity(jobs: dict[str, Any]) -> dict[str, Any]:
    rows = jobs.get("jobs")
    return {
        "attempt_number": jobs.get("attempt_number"),
        "jobs_api_endpoint": jobs.get("jobs_api_endpoint"),
        "job_count": jobs.get("job_count"),
        "jobs": rows,
    }


def _terminal_attempt_modes(jobs: dict[str, Any]) -> list[str]:
    rows = jobs.get("jobs")
    if not isinstance(rows, list):
        return []
    steps = [
        step for job in rows if isinstance(job, dict) and job.get("name") == "reconcile"
        for step in (job.get("steps") if isinstance(job.get("steps"), list) else [])
        if isinstance(step, dict)
    ]
    selected: list[str] = []
    for mode, contract in C_TERMINAL_MODE_STEPS.items():
        matches = [step for step in steps if step.get("name") == contract["invocation_step"]]
        if len(matches) > 1:
            raise RuntimeError("terminal_mode_step_not_unique")
        if matches and matches[0].get("status") == "completed" and matches[0].get("conclusion") in {"success", "failure"}:
            selected.append(mode)
    return selected


def _terminal_unavailable_result(
    reason_code: str, run: dict[str, Any], mode: str,
) -> dict[str, Any]:
    return _terminal_result_stub(
        "unavailable", reason_code,
        invocation={
            "repository": str((run.get("repository") or {}).get("full_name", "")),
            "workflow_path": TERMINAL_EVIDENCE.WORKFLOW_PATH,
            "run_id": str(run.get("id", "")),
            "run_attempt": run.get("run_attempt"),
            "event": run.get("event"),
            "head_sha": run.get("head_sha"),
            "mode": mode,
        },
        run=run,
    )


def _terminal_current_subject(
    checkpoint: dict[str, Any] | None, screen_result: Any,
    main_identity: dict[str, Any], relation: dict[str, Any] | None, mode: str,
) -> dict[str, Any] | None:
    """Expose a current B row only after the normal full screen proved exact canonical bytes."""
    if mode != "live" or relation is None or not isinstance(checkpoint, dict):
        return None
    if not isinstance(screen_result, dict) or screen_result.get("status") != "verified":
        return None
    screened = screen_result.get("screened")
    if not isinstance(screened, dict):
        return None
    try:
        runner = load_promotion_runner()
        project = getattr(runner, "terminal_generation_record", None)
        if not callable(project):
            return None
        row = project(
            checkpoint, status="already_canonical",
            reason_code="already_canonical_payload", screened=screened,
        )
    except Exception:
        return None
    if not isinstance(row, dict) or row.get("generation_id") != relation.get("generation_id"):
        return None
    return {"main_identity": main_identity, "generations": [row]}


def _terminal_artifact_projection(value: Any) -> dict[str, Any] | None:
    """Expose an artifact identity only after the archive digest and size were verified."""
    if not isinstance(value, dict):
        return None
    artifact_id = value.get("artifact_id")
    name = value.get("name")
    expires_at = value.get("expires_at")
    sha256 = value.get("sha256")
    size = value.get("bytes")
    if (
        not isinstance(artifact_id, str) or re.fullmatch(r"[0-9]{1,20}", artifact_id) is None or artifact_id == "0"
        or not isinstance(name, str) or not name or len(name) > 200
        or not isinstance(expires_at, str)
        or not isinstance(sha256, str) or not DIGEST.fullmatch(sha256)
        or not isinstance(size, int) or isinstance(size, bool) or size < 1
        or size > TERMINAL_EVIDENCE.MAX_ARCHIVE_BYTES
    ):
        return None
    try:
        parse_time(expires_at, "promotion_terminal_evidence.artifact.expires_at")
    except ValueError:
        return None
    return {
        "artifact_id": artifact_id,
        "name": name,
        "expires_at": expires_at,
        "sha256": sha256,
        "bytes": size,
    }


def finalize_terminal_outcome_records(
    collection: dict[str, Any] | None, *, current_subject: dict[str, Any] | None,
    as_of: dt.datetime, mode: str,
) -> dict[str, Any]:
    """Validate collected native inputs and return only the closed, bounded result projection."""
    if mode != "live" or not isinstance(collection, dict):
        return {"status": "not_applicable", "reason_code": "fixture_or_collection_not_applicable", "attempts_considered": 0, "records": []}
    if collection.get("status") in {"unavailable", "rejected"}:
        return {
            "status": collection["status"],
            "reason_code": collection.get("reason_code"),
            "attempts_considered": collection.get("attempts_considered", 0),
            "records": [],
        }
    normalized: list[dict[str, Any]] = []
    for row in collection.get("records", []):
        if not isinstance(row, dict):
            continue
        result = row.get("result")
        terminal_input = row.get("_terminal_input")
        if isinstance(terminal_input, dict):
            result = TERMINAL_EVIDENCE.validate_terminal_evidence(
                archive_bytes=terminal_input["archive_bytes"],
                run=terminal_input["run"],
                exact_attempt=terminal_input["exact_attempt"],
                jobs=terminal_input["jobs"],
                artifact_inventory=terminal_input["artifact_inventory"],
                expected_context=terminal_input["expected_context"],
                source_contract=terminal_input["source_contract"],
                as_of=as_of,
                current_subject=current_subject,
            )
        if not isinstance(result, dict):
            continue
        result = dict(result)
        artifact = _terminal_artifact_projection(result.get("artifact"))
        result["artifact"] = artifact
        if result.get("status") == "verified" and artifact is None:
            result.update({
                "status": "rejected",
                "reason_code": "terminal_artifact_identity_incomplete",
                "outcome": None,
                "current_applicability": {
                    "status": "unavailable",
                    "reason_code": "terminal_artifact_identity_incomplete",
                    "matching_generations": [],
                },
            })
        for field in ("run_status", "run_conclusion"):
            value = result.get(field)
            result[field] = value if isinstance(value, str) and len(value) <= 32 else None
        normalized.append({
            "run_id": row.get("run_id") if isinstance(row.get("run_id"), str) and re.fullmatch(r"[0-9]{1,20}", row["run_id"]) else None,
            "run_attempt": row.get("run_attempt") if isinstance(row.get("run_attempt"), int) and not isinstance(row.get("run_attempt"), bool) and row.get("run_attempt", 0) > 0 else None,
            "mode": row.get("mode") if row.get("mode") in TERMINAL_EVIDENCE.MODE_STEPS else None,
            "result": result,
        })
    if not normalized:
        return {"status": "not_applicable", "reason_code": None, "attempts_considered": collection.get("attempts_considered", 0), "records": []}
    verified = sum(row["result"].get("status") == "verified" for row in normalized)
    if all(row["result"].get("status") == "not_applicable" for row in normalized):
        status = "not_applicable"
        reason = None
    elif verified == len(normalized):
        status = "verified"
        reason = None
    elif verified:
        status = "partially_verified"
        reason = "some_terminal_evidence_unavailable_or_rejected"
    else:
        status = "unavailable"
        reason = "terminal_evidence_unavailable_or_rejected"
    return {
        "status": status,
        "reason_code": reason,
        "attempts_considered": collection.get("attempts_considered", 0),
        "records": normalized,
    }


def collect_terminal_outcome_records(
    *, root: pathlib.Path, repository: str, workflow_id: int | None,
    runs: list[dict[str, Any]], previous_attempts: dict[str, Any],
    previous_attempt_errors: set[str], attempt_evidence: dict[str, Any],
    current_main_sha: str, as_of: dt.datetime, maximum_future_skew: int,
) -> dict[str, Any]:
    """Read bounded terminal artifacts for the selected exact C failure/success attempts."""
    base = {"status": "not_applicable", "reason_code": None, "attempts_considered": 0, "records": []}
    latest, failure, success, errors = promotion_execution_references(
        runs, previous_attempts, previous_attempt_errors, repository,
        TERMINAL_EVIDENCE.WORKFLOW_PATH, workflow_id, PROMOTION_WORKFLOW_EVENTS,
        as_of, maximum_future_skew,
    )
    selected: dict[str, dict[str, Any]] = {}
    for summary in (failure, success):
        if isinstance(summary, dict):
            selected[f"{summary.get('run_id')}/{summary.get('run_attempt')}"] = summary
    if not selected:
        if errors:
            return {**base, "status": "unavailable", "reason_code": "terminal_attempt_listing_incomplete"}
        return base
    base["attempts_considered"] = len(selected)
    records: list[dict[str, Any]] = []
    try:
        native_repository = gh_json(f"repos/{repository}")
        repo_id = native_repository.get("id") if isinstance(native_repository, dict) else None
        default_branch = native_repository.get("default_branch") if isinstance(native_repository, dict) else None
        full_name = native_repository.get("full_name") if isinstance(native_repository, dict) else None
        if (
            isinstance(repo_id, bool) or not isinstance(repo_id, int) or repo_id < 1
            or not isinstance(default_branch, str) or not default_branch.strip()
            or not isinstance(full_name, str) or full_name.casefold() != repository.casefold()
        ):
            raise RuntimeError("terminal_repository_identity_unavailable")
    except RuntimeError:
        return {**base, "status": "unavailable", "reason_code": "terminal_repository_identity_unavailable"}

    source_contracts: dict[str, dict[str, Any] | str] = {}
    for identity, summary in sorted(selected.items()):
        run_id = str(summary.get("run_id", ""))
        try:
            attempt = int(summary.get("run_attempt"))
        except (TypeError, ValueError):
            attempt = 0
        evidence = attempt_evidence.get(identity)
        run = evidence.get("run") if isinstance(evidence, dict) else None
        jobs = {
            key: evidence.get(key) for key in ("attempt_number", "jobs_api_endpoint", "job_count", "jobs")
        } if isinstance(evidence, dict) else {}
        if not isinstance(run, dict) or not isinstance(jobs.get("jobs"), list):
            records.append({"run_id": run_id, "run_attempt": attempt or None, "modes": [], "result": _terminal_result_stub("unavailable", "exact_attempt_unavailable", run=run)})
            continue
        try:
            modes = _terminal_attempt_modes(jobs)
        except RuntimeError:
            records.append({"run_id": run_id, "run_attempt": attempt or None, "modes": [], "result": _terminal_result_stub("rejected", "terminal_mode_step_not_unique", run=run)})
            continue
        if not modes:
            records.append({"run_id": run_id, "run_attempt": attempt, "modes": [], "result": _terminal_result_stub("not_applicable", "no_terminal_mode_invoked", run=run)})
            continue
        source_sha = run.get("head_sha")
        contract: dict[str, Any] | None = None
        contract_error: str | None = None
        if isinstance(source_sha, str) and source_sha not in source_contracts:
            try:
                source_contracts[source_sha] = terminal_source_contract(
                    root, repository, int(workflow_id or 0), source_sha, current_main_sha,
                )
            except RuntimeError as exc:
                reason = str(exc)
                source_contracts[source_sha] = (
                    reason if re.fullmatch(r"terminal_[a-z0-9_]{1,80}", reason)
                    else "terminal_source_contract_unavailable"
                )
        cached_contract = source_contracts.get(source_sha) if isinstance(source_sha, str) else None
        if isinstance(cached_contract, dict):
            contract = cached_contract
        else:
            contract_error = cached_contract if isinstance(cached_contract, str) else "terminal_source_contract_unavailable"
        if contract_error is not None:
            for mode in modes:
                records.append({"run_id": run_id, "run_attempt": attempt, "mode": mode, "result": _terminal_unavailable_result(contract_error, run, mode)})
            continue

        context = {
            "repository": repository,
            "repository_id": repo_id,
            "workflow_id": workflow_id,
            "workflow_path": TERMINAL_EVIDENCE.WORKFLOW_PATH,
            "default_branch": default_branch,
            "run_id": run_id,
            "run_attempt": attempt,
            "event": run.get("event"),
            "head_sha": source_sha,
        }
        try:
            latest_before = collect_run(repository, run_id)
            exact_before = collect_run_attempt(repository, run_id, attempt)
            jobs_before = collect_run_attempt_jobs(repository, run_id, attempt)
            if (
                latest_before.get("run_attempt") != attempt
                or _terminal_run_identity(exact_before) != _terminal_run_identity(run)
                or _terminal_run_identity(latest_before) != _terminal_run_identity(run)
                or _terminal_jobs_identity(jobs_before) != _terminal_jobs_identity(jobs)
            ):
                raise RuntimeError("terminal_attempt_changed_before_intake")
            inventory = collect_terminal_artifact_inventory(repository, run_id)
            expected_names = {
                mode: f"canonical-update-promotion-terminal-{run_id}-{attempt}-{mode}"
                for mode in modes
            }
            candidate_rows = [row for row in inventory["artifacts"] if row.get("name") in set(expected_names.values())]
            if len(candidate_rows) > len(modes):
                raise RuntimeError("terminal_artifact_identity_ambiguous")
            for row in candidate_rows:
                detail = collect_artifact(repository, str(row["id"]))
                if not isinstance(detail, dict):
                    raise RuntimeError("terminal_artifact_detail_unavailable")
                inventory["details_by_id"][str(row["id"])] = detail
            archives: dict[str, bytes] = {}
            for mode, expected_name in expected_names.items():
                mode_rows = [row for row in candidate_rows if row.get("name") == expected_name]
                if len(mode_rows) == 1:
                    artifact_id = str(mode_rows[0]["id"])
                    detail = inventory["details_by_id"].get(artifact_id)
                    size = detail.get("size_in_bytes") if isinstance(detail, dict) else None
                    if isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_TERMINAL_ARCHIVE_BYTES:
                        archives[mode] = _bounded_gh_archive(
                            f"repos/{repository}/actions/artifacts/{artifact_id}/zip", size,
                        )
            latest_after = collect_run(repository, run_id)
            exact_after = collect_run_attempt(repository, run_id, attempt)
            jobs_after = collect_run_attempt_jobs(repository, run_id, attempt)
            inventory_after = collect_terminal_artifact_inventory(repository, run_id)
            details_after: dict[str, dict[str, Any]] = {}
            for row in candidate_rows:
                artifact_id = str(row["id"])
                detail_after = collect_artifact(repository, artifact_id)
                if not isinstance(detail_after, dict):
                    raise RuntimeError("terminal_artifact_detail_unavailable")
                details_after[artifact_id] = detail_after
            if (
                latest_after.get("run_attempt") != attempt
                or _terminal_run_identity(exact_after) != _terminal_run_identity(run)
                or _terminal_run_identity(latest_after) != _terminal_run_identity(run)
                or _terminal_jobs_identity(jobs_after) != _terminal_jobs_identity(jobs)
                or canonical_json(inventory_after["artifacts"]) != canonical_json(inventory["artifacts"])
                or inventory_after["total_count"] != inventory["total_count"]
                or any(
                    canonical_json(inventory["details_by_id"].get(artifact_id))
                    != canonical_json(detail_after)
                    for artifact_id, detail_after in details_after.items()
                )
            ):
                raise RuntimeError("terminal_attempt_changed_during_intake")
        except RuntimeError as exc:
            reason = str(exc)
            allowed_reason = reason if re.fullmatch(r"terminal_[a-z0-9_]{1,80}", reason) else "terminal_intake_unavailable"
            for mode in modes:
                records.append({"run_id": run_id, "run_attempt": attempt, "mode": mode, "result": _terminal_unavailable_result(allowed_reason, run, mode)})
            continue

        for mode in modes:
            records.append({
                "run_id": run_id,
                "run_attempt": attempt,
                "mode": mode,
                "_terminal_input": {
                    "archive_bytes": archives.get(mode, b""),
                    "run": run,
                    "exact_attempt": attempt,
                    "jobs": jobs,
                    "artifact_inventory": inventory,
                    "expected_context": {**context, "mode": mode},
                    "source_contract": contract,
                },
            })
    return {
        "status": "pending_validation",
        "reason_code": None,
        "attempts_considered": len(selected),
        "records": records,
    }


def collect_artifact(repository: str, artifact_id: str) -> dict[str, Any] | None:
    if not artifact_id.isdigit():
        return None
    payload = gh_json(f"repos/{repository}/actions/artifacts/{artifact_id}")
    return payload if isinstance(payload, dict) else None


def load_processor_checkpoints(
    state_dir: pathlib.Path, source_id: str, max_bytes: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load only generation files named by that source's processor index."""
    index_path = state_dir / "sources" / source_id / "index.json"
    if not index_path.exists():
        return [], ["processor_index_missing"]
    issues: list[str] = []
    try:
        index = load_json(index_path, maximum_bytes=max_bytes)
    except (OSError, ValueError, json.JSONDecodeError):
        return [], ["processor_index_corrupt"]
    if not isinstance(index, dict) or index.get("schema_version") != "datapan.upstream-catalogue-checkpoint.v1" or not isinstance(index.get("generations"), list):
        return [], ["processor_index_corrupt"]
    checkpoints: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in index["generations"]:
        if not isinstance(row, dict):
            issues.append("processor_index_corrupt")
            continue
        generation_id = row.get("generation_id")
        name = row.get("checkpoint")
        if not isinstance(generation_id, str) or not DIGEST.fullmatch(generation_id) or name != f"{generation_id}.json" or generation_id in seen:
            issues.append("processor_index_corrupt")
            continue
        seen.add(generation_id)
        path = state_dir / "sources" / source_id / "generations" / name
        if not path.exists():
            issues.append("checkpoint_missing")
            continue
        try:
            value = load_json(path, maximum_bytes=max_bytes)
        except (OSError, ValueError, json.JSONDecodeError):
            issues.append("checkpoint_corrupt")
            continue
        if not isinstance(value, dict) or value.get("generation_id") != generation_id or not verify_sealed(value, "checkpoint_sha256"):
            issues.append("checkpoint_corrupt")
            continue
        generation_inputs = value.get("generation_inputs")
        if (
            value.get("source_id") != source_id
            or not isinstance(generation_inputs, dict)
            or generation_inputs.get("source_id") != source_id
        ):
            issues.append("checkpoint_source_binding_mismatch")
            continue
        if CHECKPOINT_SCHEMA.exists():
            try:
                import jsonschema

                jsonschema.Draft202012Validator(load_json(CHECKPOINT_SCHEMA)).validate(value)
            except ImportError:
                issues.append("checkpoint_schema_dependency_missing")
                continue
            except Exception:
                issues.append("checkpoint_corrupt")
                continue
        checkpoints.append(value)
    return checkpoints, list(dict.fromkeys(issues))


def latest_observation(checkpoints: list[dict[str, Any]], now: dt.datetime, future_skew: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    observations: list[tuple[dt.datetime, dt.datetime, str, dict[str, Any], dict[str, Any]]] = []
    errors: list[str] = []
    for checkpoint in checkpoints:
        observation = checkpoint.get("last_observation")
        if not isinstance(observation, dict):
            continue
        try:
            observed_at = parse_time(observation.get("observed_at"), "last_observation.observed_at")
            seconds_since(now, observation.get("observed_at"), "last_observation.observed_at", future_skew)
            last_progress_at = parse_time(checkpoint.get("last_progress_at"), "checkpoint.last_progress_at")
            seconds_since(now, checkpoint.get("last_progress_at"), "checkpoint.last_progress_at", future_skew)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        generation_id = checkpoint.get("generation_id")
        if not isinstance(generation_id, str) or not DIGEST.fullmatch(generation_id):
            errors.append("invalid_generation_id:checkpoint.generation_id")
            continue
        observations.append((observed_at, last_progress_at, generation_id, observation, checkpoint))
    if not observations:
        return None, None, sorted(set(errors))[0] if errors else None
    _, _, _, observation, checkpoint = max(observations, key=lambda item: item[:3])
    return observation, checkpoint, sorted(set(errors))[0] if errors else None


def workflow_run_order(run: dict[str, Any]) -> tuple[dt.datetime, int]:
    try:
        created = parse_time(run.get("created_at"), "workflow_run.created_at")
    except ValueError:
        created = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    try:
        identifier = int(run.get("id", 0))
    except (TypeError, ValueError):
        identifier = 0
    return created, identifier


def processor_workflow_run_order(run: dict[str, Any]) -> tuple[dt.datetime, int]:
    """Order processor executions by actual start time, falling back to creation time."""
    try:
        started = parse_time(run.get("run_started_at"), "processor_run.run_started_at")
    except ValueError:
        try:
            started = parse_time(run.get("created_at"), "processor_run.created_at")
        except ValueError:
            started = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    try:
        identifier = int(run.get("id", 0))
    except (TypeError, ValueError):
        identifier = 0
    return started, identifier


def workflow_path_matches(actual: Any, expected: str) -> bool:
    if not isinstance(actual, str):
        return False
    return actual in {expected, f"{expected}@main", f"{expected}@refs/heads/main"}


def run_matches_workflow(run: dict[str, Any], expected: str) -> bool:
    return workflow_path_matches(run.get("path"), expected)


def trusted_main_workflow_run(
    run: dict[str, Any], repository: str, workflow_path: str, workflow_id: int | None,
    allowed_events: set[str] | None,
) -> bool:
    """Require authoritative run metadata to bind workflow, repository, ref, and commit."""
    actual_workflow_id = run.get("workflow_id")
    head_repository = run.get("head_repository")
    run_repository = run.get("repository")
    run_id = run.get("id")
    return bool(
        isinstance(workflow_id, int)
        and not isinstance(workflow_id, bool)
        and workflow_id > 0
        and isinstance(run_id, int)
        and not isinstance(run_id, bool)
        and run_id > 0
        and isinstance(actual_workflow_id, int)
        and not isinstance(actual_workflow_id, bool)
        and actual_workflow_id == workflow_id
        and run_matches_workflow(run, workflow_path)
        and isinstance(run.get("event"), str)
        and bool(run.get("event"))
        and (allowed_events is None or run.get("event") in allowed_events)
        and run.get("head_branch") == "main"
        and isinstance(run_repository, dict)
        and str(run_repository.get("full_name", "")).casefold() == repository.casefold()
        and isinstance(head_repository, dict)
        and str(head_repository.get("full_name", "")).casefold() == repository.casefold()
        and isinstance(run.get("head_sha"), str)
        and REVISION.fullmatch(run["head_sha"])
    )


def trusted_processor_workflow_run(
    run: dict[str, Any], repository: str, workflow_path: str, workflow_id: int | None,
    allowed_events: set[str],
) -> bool:
    attempt = run.get("run_attempt")
    return bool(
        run.get("path") == workflow_path
        and trusted_main_workflow_run(run, repository, workflow_path, workflow_id, allowed_events)
        and isinstance(attempt, int)
        and not isinstance(attempt, bool)
        and attempt > 0
    )


def report_workflow_run(run: dict[str, Any], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(artifacts, list):
        artifacts = []
    return {
        "run_id": str(run.get("id", "")),
        "run_attempt": run.get("run_attempt") if isinstance(run.get("run_attempt"), int) and not isinstance(run.get("run_attempt"), bool) else None,
        "event": str(run.get("event", "unknown")),
        "status": str(run.get("status", "unknown")),
        "conclusion": str(run.get("conclusion") or "pending"),
        "created_at": str(run.get("created_at", "")),
        "updated_at": str(run.get("updated_at", "")),
        "head_branch": str(run.get("head_branch", "")),
        "head_sha": str(run.get("head_sha", "")),
        "artifacts": [
            {
                "artifact_id": str(row.get("id", "")),
                "name": str(row.get("name", "")),
                "expired": bool(row.get("expired", False)),
                "expires_at": str(row.get("expires_at", "")),
            }
            for row in artifacts
        ],
    }


def processor_run_disposition(run: dict[str, Any]) -> str:
    if run.get("status") != "completed":
        return "pending"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "success"
    if conclusion in {None, "skipped", "neutral"}:
        return "pending"
    return "failure"


def collector_execution_disposition(run: dict[str, Any]) -> str:
    """Classify known Actions conclusions without treating unknown values as failures."""
    if run.get("status") != "completed":
        return "pending"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "success"
    if conclusion in {"skipped", "neutral"}:
        return "pending"
    if conclusion in {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}:
        return "failure"
    return "unavailable"


def processor_run_hides_prior_attempt(run: dict[str, Any]) -> bool:
    return run.get("status") != "completed" or run.get("conclusion") in {None, "skipped", "neutral"}


def report_processor_run(run: dict[str, Any], workflow_path: str) -> dict[str, Any]:
    repository = run.get("repository")
    head_repository = run.get("head_repository")
    started_at = run.get("run_started_at") or run.get("created_at")
    return {
        "run_id": str(run["id"]),
        "run_attempt": int(run["run_attempt"]),
        "workflow_id": int(run["workflow_id"]),
        "path": workflow_path,
        "event": str(run.get("event", "")),
        "status": str(run.get("status", "unknown")),
        "conclusion": str(run.get("conclusion") or "pending"),
        "created_at": str(run.get("created_at", "")),
        "run_started_at": str(started_at or ""),
        "updated_at": str(run.get("updated_at", "")),
        "head_branch": str(run.get("head_branch", "")),
        "head_sha": str(run.get("head_sha", "")),
        "repository": str(repository.get("full_name", "")) if isinstance(repository, dict) else "",
        "head_repository": str(head_repository.get("full_name", "")) if isinstance(head_repository, dict) else "",
    }


def processor_execution_order(run: dict[str, Any]) -> tuple[dt.datetime, int, int] | None:
    if not isinstance(run, dict):
        return None
    try:
        started = parse_time(
            run.get("run_started_at") or run.get("created_at"),
            "processor_run.run_started_at",
        )
        run_id = int(run.get("run_id", run.get("id")))
        attempt = run.get("run_attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            return None
    except (TypeError, ValueError):
        return None
    if run_id < 1:
        return None
    return started, run_id, attempt


def processor_attempt_evidence_run(
    evidence: Any, repository: str, workflow_path: str, workflow_id: int | None, attempt: int,
    allowed_events: set[str],
) -> dict[str, Any] | None:
    if not isinstance(evidence, dict) or evidence.get("availability_error") is True:
        return None
    run = evidence.get("run")
    run_id = run.get("id") if isinstance(run, dict) else None
    jobs = evidence.get("jobs")
    job_count = evidence.get("job_count")
    expected_endpoint = f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs"
    if (
        not isinstance(run, dict)
        or isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id < 1
        or isinstance(evidence.get("attempt_number"), bool)
        or not isinstance(evidence.get("attempt_number"), int)
        or evidence.get("attempt_number") != attempt
        or isinstance(run.get("run_attempt"), bool)
        or not isinstance(run.get("run_attempt"), int)
        or run.get("run_attempt") != attempt
        or str(run.get("id")) != str(run_id)
        or evidence.get("jobs_api_endpoint") != expected_endpoint
        or isinstance(job_count, bool)
        or not isinstance(job_count, int)
        or job_count < 1
        or job_count > MAX_PROMOTION_JOBS
        or not isinstance(jobs, list)
        or len(jobs) != job_count
        or not trusted_processor_workflow_run(run, repository, workflow_path, workflow_id, allowed_events)
    ):
        return None
    seen_job_ids: set[int] = set()
    for job in jobs:
        if (
            not isinstance(job, dict)
            or isinstance(job.get("id"), bool)
            or not isinstance(job.get("id"), int)
            or job["id"] < 1
            or job["id"] in seen_job_ids
            or str(job.get("run_id", "")) != str(run_id)
            or job.get("head_sha") != run.get("head_sha")
            or job.get("status") != "completed"
        ):
            return None
        seen_job_ids.add(job["id"])
    return run if run.get("status") == "completed" else None


def trusted_collector_execution_run(
    run: Any, repository: str, workflow_path: str, workflow_id: int | None,
    allowed_events: set[str],
) -> bool:
    if not isinstance(run, dict):
        return False
    attempt = run.get("run_attempt")
    return bool(
        trusted_main_workflow_run(run, repository, workflow_path, workflow_id, allowed_events)
        and isinstance(attempt, int)
        and not isinstance(attempt, bool)
        and attempt > 0
    )


def collector_execution_order(run: Any) -> tuple[dt.datetime, int, int] | None:
    """Use the exact run-attempt start; collector execution never falls back to created_at."""
    if not isinstance(run, dict):
        return None
    try:
        started = parse_time(run.get("run_started_at"), "collector_run.run_started_at")
        run_id = int(run.get("id", run.get("run_id")))
        attempt = run.get("run_attempt")
    except (TypeError, ValueError):
        return None
    if run_id < 1 or isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return None
    return started, run_id, attempt


def collector_execution_identity_matches(
    candidate: dict[str, Any], expected: dict[str, Any], repository: str, workflow_path: str,
    workflow_id: int | None, allowed_events: set[str],
) -> bool:
    candidate_repository = candidate.get("repository")
    candidate_head_repository = candidate.get("head_repository")
    expected_repository = expected.get("repository")
    expected_head_repository = expected.get("head_repository")
    return bool(
        trusted_collector_execution_run(candidate, repository, workflow_path, workflow_id, allowed_events)
        and str(candidate.get("id")) == str(expected.get("id"))
        and candidate.get("workflow_id") == expected.get("workflow_id")
        and workflow_path_matches(candidate.get("path"), workflow_path)
        and candidate.get("event") == expected.get("event")
        and candidate.get("head_branch") == expected.get("head_branch")
        and candidate.get("head_sha") == expected.get("head_sha")
        and isinstance(candidate_repository, dict)
        and isinstance(expected_repository, dict)
        and str(candidate_repository.get("full_name", "")).casefold() == str(expected_repository.get("full_name", "")).casefold()
        and isinstance(candidate_head_repository, dict)
        and isinstance(expected_head_repository, dict)
        and str(candidate_head_repository.get("full_name", "")).casefold() == str(expected_head_repository.get("full_name", "")).casefold()
    )


def collector_attempt_evidence_run(
    evidence: Any, repository: str, workflow_path: str, workflow_id: int | None,
    run_id: str, attempt: int, allowed_events: set[str], expected: dict[str, Any],
    *, current_summary: bool, as_of: dt.datetime, maximum_future_skew: int,
) -> dict[str, Any] | None:
    """Validate one exact collector attempt and its complete, run-bound jobs response."""
    if not isinstance(evidence, dict) or evidence.get("availability_error") is True:
        return None
    run = evidence.get("run")
    jobs = evidence.get("jobs")
    job_count = evidence.get("job_count")
    if (
        not isinstance(run, dict)
        or str(run.get("id", "")) != run_id
        or run.get("run_attempt") != attempt
        or evidence.get("attempt_number") != attempt
        or evidence.get("jobs_api_endpoint") != f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs"
        or isinstance(job_count, bool)
        or not isinstance(job_count, int)
        or job_count < 1
        or job_count > MAX_PROMOTION_JOBS
        or not isinstance(jobs, list)
        or len(jobs) != job_count
        or run.get("status") != "completed"
        or not collector_execution_identity_matches(run, expected, repository, workflow_path, workflow_id, allowed_events)
    ):
        return None
    try:
        started = parse_time(run.get("run_started_at"), "collector_attempt.run_started_at")
        seconds_since(as_of, run.get("run_started_at"), "collector_attempt.run_started_at", maximum_future_skew)
    except ValueError:
        return None
    if current_summary:
        if (
            run.get("status") != expected.get("status")
            or run.get("conclusion") != expected.get("conclusion")
            or run.get("run_attempt") != expected.get("run_attempt")
        ):
            return None
        try:
            if parse_time(expected.get("run_started_at"), "collector_summary.run_started_at") != started:
                return None
            seconds_since(as_of, expected.get("run_started_at"), "collector_summary.run_started_at", maximum_future_skew)
        except ValueError:
            return None
    seen_job_ids: set[int] = set()
    for job in jobs:
        if (
            not isinstance(job, dict)
            or isinstance(job.get("id"), bool)
            or not isinstance(job.get("id"), int)
            or job["id"] < 1
            or job["id"] in seen_job_ids
            or str(job.get("run_id", "")) != run_id
            or job.get("head_sha") != run.get("head_sha")
            or job.get("status") != "completed"
        ):
            return None
        seen_job_ids.add(job["id"])
    return run


def collector_summary_groups(
    runs: list[dict[str, Any]], repository: str, workflow_path: str, workflow_id: int | None,
    allowed_events: set[str], as_of: dt.datetime, maximum_future_skew: int,
) -> tuple[list[dict[str, Any]], set[str]]:
    groups: dict[str, dict[int, dict[str, Any]]] = {}
    errors: set[str] = set()
    conflicting_run_ids: set[str] = set()
    for row in runs:
        if not isinstance(row, dict) or not trusted_main_workflow_run(row, repository, workflow_path, workflow_id, allowed_events):
            continue
        run_id = str(row.get("id", ""))
        attempt = row.get("run_attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            errors.add(f"{run_id}/invalid-attempt")
            continue
        try:
            seconds_since(as_of, row.get("run_started_at"), "collector_summary.run_started_at", maximum_future_skew)
        except ValueError:
            errors.add(f"{run_id}/{attempt}")
            continue
        attempts = groups.setdefault(run_id, {})
        previous = attempts.get(attempt)
        if previous is not None:
            comparable_fields = (
                "id", "run_attempt", "workflow_id", "path", "event", "status", "conclusion",
                "run_started_at", "head_branch", "head_sha", "repository", "head_repository",
            )
            if any(previous.get(field) != row.get(field) for field in comparable_fields):
                errors.add(f"{run_id}/{attempt}")
                conflicting_run_ids.add(run_id)
                continue
            continue
        attempts[attempt] = row
    latest: list[dict[str, Any]] = []
    for run_id, attempts in groups.items():
        rows = list(attempts.values())
        immutable = {
            (
                row.get("workflow_id"), row.get("path"), row.get("event"), row.get("head_branch"), row.get("head_sha"),
                str((row.get("repository") or {}).get("full_name", "")).casefold() if isinstance(row.get("repository"), dict) else "",
                str((row.get("head_repository") or {}).get("full_name", "")).casefold() if isinstance(row.get("head_repository"), dict) else "",
            )
            for row in rows
        }
        if len(immutable) != 1:
            errors.add(f"{run_id}/identity-conflict")
            continue
        latest.append(max(rows, key=lambda row: int(row["run_attempt"])))
    latest = [row for row in latest if str(row.get("id", "")) not in conflicting_run_ids]
    latest.sort(key=lambda row: collector_execution_order(row) or (dt.datetime.min.replace(tzinfo=dt.timezone.utc), 0, 0), reverse=True)
    return latest, errors


def collector_error_execution_identity(
    errors: set[str], runs: list[dict[str, Any]], repository: str, workflow_path: str,
    workflow_id: int | None, allowed_events: set[str], as_of: dt.datetime, maximum_future_skew: int,
) -> dict[str, Any] | None:
    """Bind an unavailable-attempt fault to one unambiguous trusted run identity when possible."""
    for error in sorted(errors):
        parts = error.split("/", 1)
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        run_id, attempt_text = parts
        attempt = int(attempt_text)
        candidates = [
            row for row in runs
            if isinstance(row, dict)
            and str(row.get("id", "")) == run_id
            and row.get("run_attempt") == attempt
            and trusted_main_workflow_run(row, repository, workflow_path, workflow_id, allowed_events)
        ]
        identities: set[tuple[str, int, str, str]] = set()
        for row in candidates:
            started_at = row.get("run_started_at")
            head_sha = row.get("head_sha")
            try:
                seconds_since(as_of, started_at, "collector_error.run_started_at", maximum_future_skew)
            except ValueError:
                continue
            if not isinstance(started_at, str) or not isinstance(head_sha, str) or not re.fullmatch(r"[a-f0-9]{40,64}", head_sha):
                continue
            identities.add((run_id, attempt, started_at, head_sha))
        if len(identities) == 1:
            identity = next(iter(identities))
            return {
                "run_id": identity[0],
                "run_attempt": identity[1],
                "run_started_at": identity[2],
                "head_sha": identity[3],
            }
    return None


def collect_collector_execution_attempts(
    repository: str, runs: list[dict[str, Any]], workflow_path: str, workflow_id: int | None,
    allowed_events: set[str], as_of: dt.datetime, maximum_future_skew: int,
) -> tuple[dict[str, Any], set[str]]:
    """Collect bounded exact attempts for the recent trusted A-run summaries."""
    groups, errors = collector_summary_groups(runs, repository, workflow_path, workflow_id, allowed_events, as_of, maximum_future_skew)
    attempts: dict[str, Any] = {}
    selected = groups[:MAX_COLLECTOR_EXECUTION_RUNS]
    lookup_count = 0
    for summary in selected:
        run_id = str(summary["id"])
        current_attempt = int(summary["run_attempt"])
        disposition = collector_execution_disposition(summary)
        attempt_numbers = [current_attempt] if disposition in {"success", "failure", "unavailable"} else list(
            range(current_attempt - 1, max(0, current_attempt - MAX_PROCESSOR_PRIOR_ATTEMPT_LOOKUPS - 1), -1)
        )
        for attempt in attempt_numbers:
            if attempt < 1:
                break
            identity = f"{run_id}/{attempt}"
            if lookup_count >= MAX_COLLECTOR_EXECUTION_LOOKUPS:
                errors.add(identity)
                break
            lookup_count += 1
            try:
                attempts[identity] = collect_run_attempt_evidence(repository, run_id, attempt)
            except RuntimeError:
                attempts[identity] = {"availability_error": True}
                errors.add(identity)
            evidence = attempts[identity]
            exact = collector_attempt_evidence_run(
                evidence, repository, workflow_path, workflow_id, run_id, attempt, allowed_events, summary,
                current_summary=attempt == current_attempt, as_of=as_of, maximum_future_skew=maximum_future_skew,
            )
            if exact is None:
                errors.add(identity)
                if attempt == current_attempt or identity in errors:
                    break
            else:
                exact_disposition = collector_execution_disposition(exact)
                if exact_disposition == "unavailable":
                    errors.add(identity)
                    break
                if exact_disposition in {"success", "failure"}:
                    break
        if lookup_count >= MAX_COLLECTOR_EXECUTION_LOOKUPS:
            for remaining in selected[selected.index(summary) + 1:]:
                errors.add(f"{remaining.get('id')}/{remaining.get('run_attempt')}")
            break
    return attempts, errors


def collector_execution_state(
    runs: list[dict[str, Any]], attempts: dict[str, Any], attempt_errors: set[str],
    repository: str, workflow_path: str, workflow_id: int | None, allowed_events: set[str],
    as_of: dt.datetime, maximum_future_skew: int,
) -> dict[str, Any]:
    groups, summary_errors = collector_summary_groups(runs, repository, workflow_path, workflow_id, allowed_events, as_of, maximum_future_skew)
    selected = groups[:MAX_COLLECTOR_EXECUTION_RUNS]
    truncated = len(groups) > len(selected)
    boundary = collector_execution_order(groups[len(selected)]) if truncated else None
    errors = set(summary_errors) | set(attempt_errors)
    events: dict[str, dict[str, Any]] = {}
    for summary in selected:
        run_id = str(summary["id"])
        current_attempt = int(summary["run_attempt"])
        disposition = collector_execution_disposition(summary)
        candidate_attempts = [current_attempt] if disposition in {"success", "failure", "unavailable"} else list(
            range(current_attempt - 1, max(0, current_attempt - MAX_PROCESSOR_PRIOR_ATTEMPT_LOOKUPS - 1), -1)
        )
        for attempt in candidate_attempts:
            if attempt < 1:
                break
            identity = f"{run_id}/{attempt}"
            evidence = attempts.get(identity)
            exact = collector_attempt_evidence_run(
                evidence, repository, workflow_path, workflow_id, run_id, attempt, allowed_events, summary,
                current_summary=attempt == current_attempt, as_of=as_of, maximum_future_skew=maximum_future_skew,
            )
            if exact is None:
                if identity not in attempt_errors:
                    errors.add(identity)
                break
            disposition = collector_execution_disposition(exact)
            if disposition in {"success", "failure"}:
                order = collector_execution_order(exact)
                if order is None:
                    errors.add(identity)
                else:
                    evidence_row = attempts.get(identity) if isinstance(attempts.get(identity), dict) else {}
                    jobs = evidence_row.get("jobs") if isinstance(evidence_row.get("jobs"), list) else []
                    repository_row = exact.get("repository") if isinstance(exact.get("repository"), dict) else {}
                    head_repository_row = exact.get("head_repository") if isinstance(exact.get("head_repository"), dict) else {}
                    events[run_id] = {
                        "run_id": run_id,
                        "run_attempt": attempt,
                        "run_started_at": utc_timestamp(order[0]),
                        "workflow_id": exact["workflow_id"],
                        "path": exact["path"],
                        "head_sha": exact["head_sha"],
                        "head_branch": exact["head_branch"],
                        "repository": str(repository_row.get("full_name", "")),
                        "head_repository": str(head_repository_row.get("full_name", "")),
                        "status": exact["status"],
                        "conclusion": exact.get("conclusion"),
                        "disposition": disposition,
                        "event": exact["event"],
                        "attempt_evidence": {
                            "jobs_api_endpoint": evidence_row.get("jobs_api_endpoint"),
                            "job_count": evidence_row.get("job_count"),
                            "jobs_sha256": sha256_bytes(canonical_json(jobs)),
                        },
                    }
                break
            if disposition == "unavailable":
                errors.add(identity)
                break
    ordered = sorted(events.values(), key=lambda row: collector_execution_order({**row, "id": int(row["run_id"])}) or (dt.datetime.min.replace(tzinfo=dt.timezone.utc), 0, 0))
    if boundary is not None:
        ordered = [row for row in ordered if (collector_execution_order({**row, "id": int(row["run_id"])}) or boundary) > boundary]
    latest_success = next((row for row in reversed(ordered) if row["disposition"] == "success"), None)
    unavailable_run_ids = {
        error.split("/", 1)[0]
        for error in errors
        if "/" in error and error.split("/", 1)[0].isdigit()
    }
    if latest_success is not None and latest_success["run_id"] in unavailable_run_ids:
        latest_success = None
    failures_after_success: list[dict[str, Any]] = []
    for row in ordered:
        if row["disposition"] == "success":
            failures_after_success = []
        else:
            failures_after_success.append(row)
    return {
        "events": ordered,
        "failure_streak": failures_after_success,
        "latest_success": latest_success,
        "errors": sorted(errors),
        "unavailable_run_ids": sorted(unavailable_run_ids),
        "truncated": truncated,
        "boundary": boundary,
    }


def processor_execution_state(
    processor_runs: list[dict[str, Any]], previous_attempts: dict[str, Any], previous_attempt_errors: set[str],
    repository: str, workflow_path: str, workflow_id: int | None, allowed_events: set[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, str | None]:
    trusted_runs = [
        run for run in processor_runs
        if isinstance(run, dict) and trusted_processor_workflow_run(run, repository, workflow_path, workflow_id, allowed_events)
    ]
    trusted_runs.sort(key=lambda run: (*processor_workflow_run_order(run), int(run["run_attempt"])))
    if not trusted_runs:
        return None, None, None, sorted(previous_attempt_errors)[0] if previous_attempt_errors else None

    latest = trusted_runs[-1]
    previous_runs: list[dict[str, Any]] = []
    lookup_error: str | None = None
    pending_reruns, success_boundary = processor_pending_reruns_after_success(
        trusted_runs, repository, workflow_path, workflow_id, allowed_events,
    )
    checked_identities: set[str] = set()
    for rerun in pending_reruns:
        run_id = str(rerun["id"])
        current_attempt = int(rerun["run_attempt"])
        rerun_order = processor_execution_order(rerun)
        if success_boundary is not None and rerun_order is not None and rerun_order <= success_boundary:
            continue
        oldest_to_check = max(1, current_attempt - MAX_PROCESSOR_PRIOR_ATTEMPT_LOOKUPS)
        for prior_attempt in range(current_attempt - 1, oldest_to_check - 1, -1):
            identity = f"{run_id}/{prior_attempt}"
            if identity in checked_identities:
                matching = next((row for row in previous_runs if str(row.get("id")) == run_id and row.get("run_attempt") == prior_attempt), None)
                if matching and processor_run_disposition(matching) in {"success", "failure"}:
                    break
                continue
            if len(checked_identities) >= MAX_PROCESSOR_PRIOR_ATTEMPT_LOOKUPS:
                lookup_error = identity
                break
            checked_identities.add(identity)
            if identity in previous_attempt_errors or identity not in previous_attempts:
                lookup_error = identity
                break
            prior_run = processor_attempt_evidence_run(
                previous_attempts[identity], repository, workflow_path, workflow_id, prior_attempt, allowed_events,
            )
            if prior_run is None or str(prior_run.get("id")) != run_id:
                lookup_error = identity
                break
            if not any(str(row.get("id")) == run_id and row.get("run_attempt") == prior_attempt for row in previous_runs):
                previous_runs.append(prior_run)
            if processor_run_disposition(prior_run) == "success":
                prior_summary = report_processor_run(prior_run, workflow_path)
                prior_order = processor_execution_order(prior_summary)
                if prior_order is not None and (success_boundary is None or prior_order > success_boundary):
                    success_boundary = prior_order
                break
            if processor_run_disposition(prior_run) == "failure":
                break
        else:
            if oldest_to_check > 1:
                lookup_error = f"{run_id}/{oldest_to_check - 1}"
                break
        if lookup_error:
            break

    execution_events = [*trusted_runs, *previous_runs]
    execution_events.sort(key=lambda run: (*processor_workflow_run_order(run), int(run["run_attempt"])))
    unresolved_failure: dict[str, Any] | None = None
    latest_success: dict[str, Any] | None = None
    for run in execution_events:
        disposition = processor_run_disposition(run)
        if disposition == "success":
            unresolved_failure = None
            latest_success = run
        elif disposition == "failure":
            unresolved_failure = run
    return (
        report_processor_run(latest, workflow_path),
        report_processor_run(unresolved_failure, workflow_path) if unresolved_failure else None,
        report_processor_run(latest_success, workflow_path) if latest_success else None,
        lookup_error,
    )


def promotion_execution_run_order(run: dict[str, Any]) -> tuple[dt.datetime, int, int] | None:
    """Order C runs only by their actual attempt start, never mutable timestamps."""
    if not isinstance(run, dict):
        return None
    try:
        started_at = parse_time(run.get("run_started_at"), "promotion_run.run_started_at")
        run_id = int(run.get("run_id", run.get("id")))
        attempt = run.get("run_attempt")
    except (TypeError, ValueError):
        return None
    if run_id < 1 or isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return None
    return started_at, run_id, attempt


def trusted_promotion_execution_run(
    run: Any, repository: str, workflow_path: str, workflow_id: int | None,
    allowed_events: set[str] = PROMOTION_WORKFLOW_EVENTS,
) -> bool:
    return bool(
        isinstance(run, dict)
        and trusted_processor_workflow_run(run, repository, workflow_path, workflow_id, allowed_events)
        and promotion_execution_run_order(run) is not None
    )


def promotion_attempt_evidence_run(
    evidence: Any, expected_run: dict[str, Any], repository: str, workflow_path: str,
    workflow_id: int | None, allowed_events: set[str], as_of: dt.datetime,
    maximum_future_skew: int,
) -> dict[str, Any] | None:
    """Validate exact C attempt evidence and bind it to the ordered workflow-run summary."""
    attempt = expected_run.get("run_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return None
    run = processor_attempt_evidence_run(evidence, repository, workflow_path, workflow_id, attempt, allowed_events)
    if run is None or not trusted_promotion_execution_run(run, repository, workflow_path, workflow_id, allowed_events):
        return None
    expected_run_id = expected_run.get("run_id", expected_run.get("id"))
    if str(run.get("id")) != str(expected_run_id):
        return None
    for field in ("run_attempt", "workflow_id", "path", "event", "status", "conclusion", "head_branch", "head_sha"):
        if run.get(field) != expected_run.get(field):
            return None
    for field in ("repository", "head_repository"):
        actual_repo = run.get(field)
        expected_repo = expected_run.get(field)
        if not isinstance(actual_repo, dict):
            return None
        actual_name = actual_repo.get("full_name")
        expected_name = expected_repo if isinstance(expected_repo, str) else (
            expected_repo.get("full_name") if isinstance(expected_repo, dict) else None
        )
        if not isinstance(actual_name, str) or not isinstance(expected_name, str) or actual_name.casefold() != expected_name.casefold():
            return None
    exact_order = promotion_execution_run_order(run)
    expected_order = promotion_execution_run_order(expected_run)
    if exact_order is None or expected_order is None or exact_order != expected_order:
        return None
    try:
        seconds_since(as_of, run.get("run_started_at"), "promotion_run.run_started_at", maximum_future_skew)
    except ValueError:
        return None
    return run


def trusted_promotion_execution_inputs(
    runs: list[dict[str, Any]], previous_attempts: dict[str, Any], previous_attempt_errors: set[str],
    repository: str, workflow_path: str, workflow_id: int | None,
    allowed_events: set[str], as_of: dt.datetime, maximum_future_skew: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], set[str]]:
    """Keep C execution ordering strict while reusing the B processor's bounded history collector."""
    trusted_runs: list[dict[str, Any]] = []
    errors = set(previous_attempt_errors)
    for run in runs:
        if not isinstance(run, dict) or not trusted_processor_workflow_run(run, repository, workflow_path, workflow_id, allowed_events):
            continue
        identity = f"{run.get('id')}/{run.get('run_attempt')}"
        try:
            seconds_since(as_of, run.get("run_started_at"), "promotion_run.run_started_at", maximum_future_skew)
        except ValueError:
            errors.add(identity)
            continue
        trusted_runs.append(run)

    validated_attempts: dict[str, Any] = {}
    for identity, evidence in previous_attempts.items():
        try:
            run_id, attempt_text = identity.split("/", 1)
            attempt = int(attempt_text)
        except (AttributeError, ValueError):
            errors.add(str(identity))
            continue
        run = processor_attempt_evidence_run(evidence, repository, workflow_path, workflow_id, attempt, allowed_events)
        if run is None or str(run.get("id")) != run_id or not trusted_promotion_execution_run(run, repository, workflow_path, workflow_id, allowed_events):
            errors.add(identity)
            continue
        try:
            seconds_since(as_of, run.get("run_started_at"), "promotion_run.run_started_at", maximum_future_skew)
        except ValueError:
            errors.add(identity)
            continue
        validated_attempts[identity] = evidence
    return trusted_runs, validated_attempts, errors


def promotion_execution_references(
    runs: list[dict[str, Any]], previous_attempts: dict[str, Any], previous_attempt_errors: set[str],
    repository: str, workflow_path: str, workflow_id: int | None, allowed_events: set[str],
    as_of: dt.datetime, maximum_future_skew: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, set[str]]:
    trusted_runs, validated_attempts, errors = trusted_promotion_execution_inputs(
        runs, previous_attempts, previous_attempt_errors, repository, workflow_path,
        workflow_id, allowed_events, as_of, maximum_future_skew,
    )
    latest, failure, success, lookup_error = processor_execution_state(
        trusted_runs, validated_attempts, errors, repository, workflow_path, workflow_id, allowed_events,
    )
    if lookup_error:
        errors.add(lookup_error)
    return latest, failure, success, errors


def collect_promotion_execution_attempts(
    repository: str, workflow_path: str, workflow_id: int | None,
    runs: list[dict[str, Any]], previous_attempts: dict[str, Any], previous_attempt_errors: set[str],
    as_of: dt.datetime, maximum_future_skew: int,
) -> tuple[dict[str, Any], set[str]]:
    """Fetch at most the selected unresolved failure and success exact C attempts."""
    _, failure, success, errors = promotion_execution_references(
        runs, previous_attempts, previous_attempt_errors, repository, workflow_path,
        workflow_id, PROMOTION_WORKFLOW_EVENTS, as_of, maximum_future_skew,
    )
    evidence_by_id = dict(previous_attempts)
    selected = {f"{run['run_id']}/{run['run_attempt']}": run for run in (failure, success) if isinstance(run, dict)}
    for identity, summary in sorted(selected.items()):
        if identity in evidence_by_id:
            continue
        if len(evidence_by_id) >= MAX_PROCESSOR_PRIOR_ATTEMPT_LOOKUPS + MAX_PROMOTION_EXECUTION_ATTEMPTS:
            errors.add(identity)
            continue
        try:
            evidence_by_id[identity] = collect_run_attempt_evidence(repository, identity.split("/", 1)[0], int(summary["run_attempt"]))
        except RuntimeError:
            evidence_by_id[identity] = {"availability_error": True}
            errors.add(identity)
    return evidence_by_id, errors


def promotion_execution_state(
    runs: list[dict[str, Any]], previous_attempts: dict[str, Any], previous_attempt_errors: set[str],
    attempt_evidence: dict[str, Any], repository: str, workflow_path: str, workflow_id: int | None,
    allowed_events: set[str], as_of: dt.datetime, maximum_future_skew: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, set[str]]:
    latest, failure, success, errors = promotion_execution_references(
        runs, previous_attempts, previous_attempt_errors, repository, workflow_path,
        workflow_id, allowed_events, as_of, maximum_future_skew,
    )
    trusted_failure = None
    trusted_success = None
    for summary in (failure, success):
        if not isinstance(summary, dict):
            continue
        identity = f"{summary.get('run_id')}/{summary.get('run_attempt')}"
        exact_run = promotion_attempt_evidence_run(
            attempt_evidence.get(identity), summary, repository, workflow_path,
            workflow_id, allowed_events, as_of, maximum_future_skew,
        )
        if exact_run is None:
            errors.add(identity)
            continue
        report = report_processor_run(exact_run, workflow_path)
        if summary is failure:
            trusted_failure = report
        if summary is success:
            trusted_success = report
    return latest, trusted_failure, trusted_success, errors


def processor_pending_reruns_after_success(
    runs: list[dict[str, Any]], repository: str, workflow_path: str,
    workflow_id: int | None, allowed_events: set[str],
) -> tuple[list[dict[str, Any]], tuple[dt.datetime, int, int] | None]:
    trusted_runs = [
        run for run in runs
        if isinstance(run, dict) and trusted_processor_workflow_run(run, repository, workflow_path, workflow_id, allowed_events)
    ]
    trusted_runs.sort(key=lambda run: (*processor_workflow_run_order(run), int(run["run_attempt"])))
    successes = [run for run in trusted_runs if processor_run_disposition(run) == "success"]
    success_boundary = max(
        (processor_execution_order(run) for run in successes if processor_execution_order(run) is not None),
        default=None,
    )
    pending = [
        run for run in trusted_runs
        if processor_run_hides_prior_attempt(run)
        and int(run["run_attempt"]) > 1
        and (success_boundary is None or (
            processor_execution_order(run) is not None and processor_execution_order(run) > success_boundary
        ))
    ]
    pending.sort(key=lambda run: (*processor_workflow_run_order(run), int(run["run_attempt"])), reverse=True)
    return pending, success_boundary


def collect_processor_prior_attempt_history(
    repository: str, runs: list[dict[str, Any]], workflow_path: str, workflow_id: int | None,
    allowed_events: set[str],
) -> tuple[dict[str, Any], set[str]]:
    attempts: dict[str, Any] = {}
    errors: set[str] = set()
    reruns, success_boundary = processor_pending_reruns_after_success(
        runs, repository, workflow_path, workflow_id, allowed_events,
    )
    lookup_count = 0
    for run in reruns:
        run_id = str(run["id"])
        current_attempt = int(run["run_attempt"])
        rerun_order = processor_execution_order(run)
        if success_boundary is not None and rerun_order is not None and rerun_order <= success_boundary:
            continue
        oldest_to_check = max(1, current_attempt - MAX_PROCESSOR_PRIOR_ATTEMPT_LOOKUPS)
        for prior_attempt in range(current_attempt - 1, oldest_to_check - 1, -1):
            identity = f"{run_id}/{prior_attempt}"
            if identity in attempts:
                previous = processor_attempt_evidence_run(attempts[identity], repository, workflow_path, workflow_id, prior_attempt, allowed_events)
                if previous and processor_run_disposition(previous) in {"success", "failure"}:
                    if processor_run_disposition(previous) == "success":
                        success_order = processor_execution_order(report_processor_run(previous, workflow_path))
                        if success_order is not None and (success_boundary is None or success_order > success_boundary):
                            success_boundary = success_order
                    break
                continue
            if lookup_count >= MAX_PROCESSOR_PRIOR_ATTEMPT_LOOKUPS:
                errors.add(identity)
                return attempts, errors
            lookup_count += 1
            try:
                evidence = collect_run_attempt_evidence(repository, run_id, prior_attempt)
            except RuntimeError:
                errors.add(identity)
                return attempts, errors
            attempts[identity] = evidence
            previous = processor_attempt_evidence_run(evidence, repository, workflow_path, workflow_id, prior_attempt, allowed_events)
            if previous is None or str(previous.get("id")) != run_id:
                errors.add(identity)
                return attempts, errors
            disposition = processor_run_disposition(previous)
            if disposition == "success":
                success_order = processor_execution_order(report_processor_run(previous, workflow_path))
                if success_order is not None and (success_boundary is None or success_order > success_boundary):
                    success_boundary = success_order
                break
            if disposition == "failure":
                break
        else:
            if oldest_to_check > 1:
                errors.add(f"{run_id}/{oldest_to_check - 1}")
                return attempts, errors
        if lookup_count >= MAX_PROCESSOR_PRIOR_ATTEMPT_LOOKUPS:
            next_unchecked = current_attempt - 1
            if next_unchecked > 0 and f"{run_id}/{next_unchecked}" not in attempts:
                errors.add(f"{run_id}/{next_unchecked}")
                return attempts, errors
    return attempts, errors


def collect_previous_processor_attempts(
    repository: str, run: dict[str, Any], workflow_path: str, workflow_id: int | None,
    allowed_events: set[str],
) -> tuple[dict[str, Any], set[str]]:
    """Compatibility wrapper for callers inspecting one pending rerun."""
    return collect_processor_prior_attempt_history(
        repository, [run], workflow_path, workflow_id, allowed_events,
    )


def _fault_action(source: dict[str, Any], key: str) -> str:
    actions = source.get("recovery_commands", {})
    return str(actions.get(key, "Inspect the health receipt and preserve the last-good canonical identity."))


def _promotion_recovery_action(source: dict[str, Any], workflow_path: str) -> str:
    return (
        f"Inspect the existing owned promotion reconciliation workflow `{workflow_path}` for the recorded candidate, "
        "then confirm its durable PR acknowledgement; preserve last-good canonical evidence and do not repeat provider requests. "
        + _fault_action(source, "promotion_wait")
    )


def validate_health_promotion_record(record: dict[str, Any]) -> None:
    """Read historical prepared records without inventing a missing payload-readback clock."""
    if record.get("status") != "prepared" or record.get("acknowledgements") != []:
        validate_schema(record, PROMOTION_SCHEMA, "promotion_record")
        return
    schema = load_json(PROMOTION_SCHEMA)
    candidate_required = schema.get("$defs", {}).get("candidate", {}).get("required")
    readback_required = schema.get("$defs", {}).get("payload_readback", {}).get("required")
    if isinstance(candidate_required, list) and "payload_readback" in candidate_required:
        candidate_required.remove("payload_readback")
    if isinstance(readback_required, list) and "observed_at" in readback_required:
        readback_required.remove("observed_at")
    validate_schema_value(record, schema, "promotion_record")


def evaluate_source(
    *, source: dict[str, Any], repository: str, refresh_source: dict[str, Any] | None, policy_sha256: str,
    as_of: dt.datetime, workflow_runs: list[dict[str, Any]], artifacts_by_run: dict[str, list[dict[str, Any]]],
    state_dir: pathlib.Path, artifact_by_id: dict[str, dict[str, Any]],
    promotion_ack: dict[str, Any] | None, main_identity: dict[str, Any], last_good: dict[str, Any] | None,
    mode: str, maximum_future_skew: int, max_checkpoint_bytes: int,
    producer_runs_by_id: dict[str, dict[str, Any]] | None = None,
    promotion_runs_by_id: dict[str, dict[str, Any]] | None = None,
    recovery_publisher_runs_by_id: dict[str, dict[str, Any]] | None = None,
    promotion_workflow_paths: dict[str, str] | None = None,
    workflow_ids_by_path: dict[str, int] | None = None,
    promotion_ack_error: str | None = None,
    workflow_api_error: str | None = None,
    processor_workflow_runs: list[dict[str, Any]] | None = None,
    processor_previous_attempts: dict[str, Any] | None = None,
    processor_previous_attempt_errors: set[str] | None = None,
    processor_workflow_api_error: str | None = None,
    processor_workflow_path: str = "",
    processor_workflow_id: int | None = None,
    processor_workflow_events: set[str] | None = None,
    prior_processor_execution_faults: list[dict[str, Any]] | None = None,
    collector_execution_attempts: dict[str, Any] | None = None,
    collector_execution_attempt_errors: set[str] | None = None,
    prior_collector_execution_faults: list[dict[str, Any]] | None = None,
    promotion_workflow_runs: list[dict[str, Any]] | None = None,
    promotion_workflow_previous_attempts: dict[str, Any] | None = None,
    promotion_workflow_previous_attempt_errors: set[str] | None = None,
    promotion_workflow_attempt_evidence: dict[str, Any] | None = None,
    promotion_workflow_attempt_errors: set[str] | None = None,
    promotion_workflow_api_error: str | None = None,
    prior_promotion_execution_faults: list[dict[str, Any]] | None = None,
    health_state_error: str | None = None,
    processor_candidate_screen: Any | None = None,
    evaluator_source_sha: str | None = None,
    promotion_terminal_collection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_id = str(source["source_id"])
    owner_ticket = int(source["owner_ticket"])
    faults: list[dict[str, Any]] = []

    def add(
        stage: str, reason: str, severity: str, action: str, failure_identity: str = "",
        *, generation_id: str | None = None, execution_identity: dict[str, Any] | None = None,
    ) -> None:
        item = fault(source_id, stage, reason, severity, owner_ticket, action, failure_identity)
        if generation_id is not None:
            if not DIGEST.fullmatch(generation_id):
                raise ValueError("historical_fault_generation_invalid")
            item["generation_id"] = generation_id
        if execution_identity is not None:
            item["execution_identity"] = execution_identity
        faults.append(item)

    latest_processor_run, execution_failure, latest_processor_success, previous_attempt_error = processor_execution_state(
        processor_workflow_runs or [],
        processor_previous_attempts or {},
        processor_previous_attempt_errors or set(),
        repository,
        processor_workflow_path,
        processor_workflow_id,
        processor_workflow_events or {"workflow_run", "schedule", "workflow_dispatch"},
    )
    if processor_workflow_api_error:
        add(
            "processor-execution", "processor_workflow_observations_unavailable", "error",
            _fault_action(source, "processor_stalled"), processor_workflow_path,
        )
    if previous_attempt_error:
        add(
            "processor-execution", "processor_run_attempt_unavailable", "error",
            _fault_action(source, "processor_stalled"), previous_attempt_error,
        )
    if execution_failure:
        execution_fault = fault(
            source_id, "processor-execution", "processor_run_failed", "error", owner_ticket,
            _fault_action(source, "processor_stalled"),
            f"{execution_failure['run_id']}/{execution_failure['run_attempt']}",
        )
        execution_fault["execution_identity"] = {
            "run_id": execution_failure["run_id"],
            "run_attempt": execution_failure["run_attempt"],
            "run_started_at": execution_failure["run_started_at"],
            "head_sha": execution_failure["head_sha"],
        }
        faults.append(execution_fault)
    success_order = processor_execution_order(latest_processor_success)
    failure_order = processor_execution_order(execution_failure)
    for prior in prior_processor_execution_faults or []:
        if (
            not isinstance(prior, dict)
            or prior.get("source_id") != source_id
            or prior.get("stage") != "processor-execution"
            or prior.get("reason") != "processor_run_failed"
            or prior.get("severity") != "error"
            or not isinstance(prior.get("fault_key"), str)
        ):
            continue
        prior_identity = prior.get("execution_identity")
        prior_order = processor_execution_order({
            "run_id": prior_identity.get("run_id"),
            "run_attempt": prior_identity.get("run_attempt"),
            "run_started_at": prior_identity.get("run_started_at"),
        }) if isinstance(prior_identity, dict) else None
        recovered_after_failure = (
            success_order is not None
            and execution_failure is None
            and prior_order is not None
            and success_order > prior_order
        )
        if recovered_after_failure:
            continue
        if not any(row.get("fault_key") == prior["fault_key"] for row in faults):
            faults.append({key: prior[key] for key in (
                "source_id", "stage", "reason", "severity", "owner_ticket", "fault_key", "recommended_action", "execution_identity",
            ) if key in prior})

    promotion_workflow_path = str((promotion_workflow_paths or {}).get("promotion_workflow_path") or "")
    promotion_workflow_id = (workflow_ids_by_path or {}).get(promotion_workflow_path)
    c_latest, c_failure, c_success, c_execution_errors = promotion_execution_state(
        promotion_workflow_runs or [],
        promotion_workflow_previous_attempts or {},
        (promotion_workflow_previous_attempt_errors or set()) | (promotion_workflow_attempt_errors or set()),
        promotion_workflow_attempt_evidence or {},
        repository,
        promotion_workflow_path,
        promotion_workflow_id,
        PROMOTION_WORKFLOW_EVENTS,
        as_of,
        maximum_future_skew,
    )
    c_success_order = promotion_execution_run_order(c_success) if c_success else None
    c_failure_order = promotion_execution_run_order(c_failure) if c_failure else None

    if health_state_error:
        add("health-state", "durable_health_state_unavailable", "error", _fault_action(source, "processor_stalled"), health_state_error)
    if promotion_ack_error:
        add("promotion", "promotion_journal_unavailable", "error", _fault_action(source, "promotion_wait"), promotion_ack_error)

    refresh = refresh_source or {}
    collector_path = str(refresh.get("workflow_path") or "")
    collector_workflow_id = (workflow_ids_by_path or {}).get(collector_path)
    matching_runs = [
        row for row in workflow_runs
        if isinstance(row, dict)
        and collector_path
        and trusted_main_workflow_run(
            row, repository, collector_path, collector_workflow_id, {"schedule", "workflow_dispatch"},
        )
    ]
    scheduled_runs = [row for row in matching_runs if row.get("event") == "schedule"]
    scheduled_runs.sort(key=workflow_run_order)
    latest_scheduled_run = scheduled_runs[-1] if scheduled_runs else None
    collector_runs = [row for row in matching_runs if row.get("event") in {"schedule", "workflow_dispatch"}]
    collector_runs.sort(key=workflow_run_order)
    latest_run = collector_runs[-1] if collector_runs else None
    collector_execution = collector_execution_state(
        workflow_runs,
        collector_execution_attempts or {},
        collector_execution_attempt_errors or set(),
        repository,
        collector_path,
        collector_workflow_id,
        {"schedule", "workflow_dispatch"},
        as_of,
        maximum_future_skew,
    )
    if collector_execution["errors"]:
        attempt_identity = collector_execution["errors"][0]
        add(
            "collector-execution", "collector_run_attempt_unavailable", "error",
            _fault_action(source, "schedule_missing"), attempt_identity,
            execution_identity=collector_error_execution_identity(
                {attempt_identity}, workflow_runs, repository, collector_path, collector_workflow_id,
                {"schedule", "workflow_dispatch"}, as_of, maximum_future_skew,
            ),
        )
    valid_prior_collector_faults = [
        prior for prior in prior_collector_execution_faults or []
        if isinstance(prior, dict)
        and prior.get("source_id") == source_id
        and prior.get("stage") == "collector-execution"
        and prior.get("reason") == "repeated_collector_execution_failures"
        and prior.get("severity") == "error"
        and isinstance(prior.get("fault_key"), str)
    ]
    unresolved_prior_collector_faults: list[dict[str, Any]] = []
    for prior in valid_prior_collector_faults:
        prior_identity = prior.get("execution_identity")
        prior_order = collector_execution_order(prior_identity) if isinstance(prior_identity, dict) else None
        recovered_after_failure = (
            not collector_execution["errors"]
            and prior_order is not None
            and any(
                event["disposition"] == "success"
                and (collector_execution_order({**event, "id": int(event["run_id"])}) or (dt.datetime.min.replace(tzinfo=dt.timezone.utc), 0, 0)) > prior_order
                for event in collector_execution["events"]
            )
        )
        if not recovered_after_failure:
            unresolved_prior_collector_faults.append(prior)
    execution_failure_streak = collector_execution["failure_streak"]
    threshold = int(source["provider_failure_threshold"])
    if (
        not collector_execution["errors"]
        and not unresolved_prior_collector_faults
        and len(execution_failure_streak) >= threshold
    ):
        anchor = execution_failure_streak[0]
        repeated = fault(
            source_id, "collector-execution", "repeated_collector_execution_failures", "error", owner_ticket,
            _fault_action(source, "schedule_missing"), f"{anchor['run_id']}/{anchor['run_attempt']}",
        )
        repeated["execution_identity"] = {
            "run_id": anchor["run_id"],
            "run_attempt": anchor["run_attempt"],
            "run_started_at": anchor["run_started_at"],
            "head_sha": anchor["head_sha"],
        }
        faults.append(repeated)
    for prior in unresolved_prior_collector_faults:
        if not any(row.get("fault_key") == prior["fault_key"] for row in faults):
            faults.append({key: prior[key] for key in (
                "source_id", "stage", "reason", "severity", "owner_ticket", "fault_key", "recommended_action", "execution_identity",
            ) if key in prior})
    baseline_value = (refresh_source or {}).get("last_successful_observation")
    schedule_anchor = None
    if latest_scheduled_run is not None:
        schedule_anchor = latest_scheduled_run.get("created_at")
    elif baseline_value:
        schedule_anchor = baseline_value
    if workflow_api_error:
        add("collector", "workflow_observations_unavailable", "error", _fault_action(source, "schedule_missing"), workflow_api_error)
        schedule_age = None
    elif schedule_anchor:
        try:
            schedule_age = seconds_since(as_of, schedule_anchor, "collector_schedule_anchor", maximum_future_skew)
        except ValueError as exc:
            add("schedule", str(exc), "error", _fault_action(source, "schedule_missing"))
            schedule_age = 0
        if schedule_age > int(source["expected_interval_seconds"]) + int(source["observation_grace_seconds"]):
            add("collector", "scheduled_execution_missing", "error", _fault_action(source, "schedule_missing"))
    else:
        schedule_age = None
        add("collector", "scheduled_execution_missing", "error", _fault_action(source, "schedule_missing"))

    latest_run_artifacts: list[dict[str, Any]] = []
    latest_scheduled_artifacts = artifacts_by_run.get(str(latest_scheduled_run.get("id", "")), []) if latest_scheduled_run else []
    if latest_run is not None:
        latest_run_artifacts = artifacts_by_run.get(str(latest_run.get("id", "")), [])
        if latest_run.get("status") == "in_progress":
            try:
                run_age = seconds_since(as_of, latest_run.get("run_started_at") or latest_run.get("created_at"), "collector_run.started_at", maximum_future_skew)
                if run_age > int(source["stage_deadlines_seconds"]["collector"]):
                    add("collector", "collector_run_stalled", "error", _fault_action(source, "schedule_missing"))
            except ValueError as exc:
                add("collector", str(exc), "error", _fault_action(source, "schedule_missing"))
        elif latest_run.get("conclusion") not in {"success", None}:
            add("collector", "collector_run_failed", "error", _fault_action(source, "schedule_missing"))
        elif latest_run.get("conclusion") == "success":
            expected_artifact = f"upstream-catalog-refresh-{latest_run.get('id')}"
            if isinstance(latest_run_artifacts, dict) and latest_run_artifacts.get("availability_error"):
                add("collector", "collector_artifact_api_unavailable", "error", _fault_action(source, "provider_failure"))
            elif not any(row.get("name") == expected_artifact and not row.get("expired") for row in latest_run_artifacts):
                add("collector", "collector_artifact_missing_or_expired", "error", _fault_action(source, "provider_failure"))

    checkpoints, checkpoint_issues = load_processor_checkpoints(state_dir, source_id, max_checkpoint_bytes)
    for issue in checkpoint_issues:
        stage = "checkpoint" if issue.startswith("checkpoint") or issue == "processor_index_corrupt" else "processor"
        add(stage, issue, "error", _fault_action(source, "processor_stalled"))
    observation, checkpoint, observation_error = latest_observation(checkpoints, as_of, maximum_future_skew)
    observation_age: int | None = None
    if observation_error:
        add("observation", observation_error, "error", _fault_action(source, "provider_failure"))
    if observation is None:
        observation_state = "missing"
        if not any(item["reason"] == "observation_missing" for item in faults):
            add("observation", "observation_missing", "error", _fault_action(source, "provider_failure"))
    elif mode == "fixture" or observation.get("execution_mode") != "live":
        observation_state = "fixture_only"
        add("observation", "fixture_observation_excluded", "error", _fault_action(source, "provider_failure"))
    elif observation.get("collection_status") != "success":
        observation_state = "collection_missing"
        reason = "source_collection_failed" if observation.get("collection_status") == "failure" else "collection_missing"
        add("observation", reason, "error", _fault_action(source, "schedule_missing"))
    else:
        producer_id = str(observation.get("producer_run_id") or "")
        producer_run = (producer_runs_by_id or {}).get(producer_id)
        producer_artifacts = artifacts_by_run.get(producer_id, [])
        expected_artifact = f"upstream-catalog-refresh-{producer_id}"
        run_is_trusted = bool(
            producer_run
            and str(producer_run.get("id", "")) == producer_id
            and trusted_main_workflow_run(
                producer_run, repository, collector_path, collector_workflow_id, {"schedule", "workflow_dispatch"},
            )
            and producer_run.get("status") == "completed"
            and producer_run.get("conclusion") == "success"
        )
        artifact_is_trusted = any(
            row.get("name") == expected_artifact and not row.get("expired")
            for row in producer_artifacts if isinstance(row, dict)
        )
        if mode == "live" and not run_is_trusted:
            observation_state = "producer_unverified"
            add("observation", "source_observation_run_unverified", "error", _fault_action(source, "provider_failure"), producer_id)
        elif mode == "live" and not artifact_is_trusted:
            observation_state = "producer_artifact_missing"
            add("observation", "source_observation_artifact_unavailable", "error", _fault_action(source, "provider_failure"), producer_id)
        else:
            try:
                observation_age = seconds_since(as_of, observation.get("observed_at"), "last_observation.observed_at", maximum_future_skew)
                if not DIGEST.fullmatch(str(observation.get("refresh_evidence_sha256") or "")):
                    observation_state = "evidence_digest_missing"
                    add("observation", "observation_evidence_digest_missing", "error", _fault_action(source, "provider_failure"))
                elif observation_age > int(source["max_observation_age_seconds"]):
                    observation_state = "stale"
                    add("observation", "source_observation_stale", "error", _fault_action(source, "schedule_missing"))
                else:
                    observation_state = "fresh"
            except ValueError as exc:
                observation_age = None
                observation_state = "invalid_time"
                if observation_error is None:
                    add("observation", str(exc), "error", _fault_action(source, "provider_failure"))

    if latest_run is not None and latest_run.get("conclusion") == "success" and observation is not None:
        try:
            observation_time = parse_time(observation.get("observed_at"), "last_observation.observed_at")
            completed_at = parse_time(latest_run.get("updated_at") or latest_run.get("created_at"), "collector_run.updated_at")
            collection_age = seconds_since(as_of, completed_at.isoformat(), "collector_run.updated_at", maximum_future_skew)
            processed_run_ids = {
                str(row.get("last_observation", {}).get("producer_run_id"))
                for row in checkpoints if isinstance(row.get("last_observation"), dict)
            }
            if (
                str(latest_run.get("id", "")) not in processed_run_ids
                and observation_time < completed_at
                and collection_age > int(source["stage_deadlines_seconds"]["queued"])
            ):
                add("processor", "successful_collector_not_observed", "error", _fault_action(source, "processor_stalled"))
        except ValueError as exc:
            add("processor", str(exc), "error", _fault_action(source, "processor_stalled"))

    processor_state = "missing" if checkpoint is None else str(checkpoint.get("status", "unknown"))
    generation_id = str(checkpoint.get("generation_id", "")) if checkpoint else ""
    if checkpoint is not None:
        generation_inputs = checkpoint.get("generation_inputs") if isinstance(checkpoint.get("generation_inputs"), dict) else {}
        if policy_sha256 and generation_inputs.get("policy_sha256") != policy_sha256:
            add("processor", "checkpoint_policy_changed", "warning", _fault_action(source, "processor_stalled"))
        status = str(checkpoint.get("status", "unknown"))
        outcome = checkpoint.get("outcome") if isinstance(checkpoint.get("outcome"), dict) else {}
        if status in {"queued", "validating", "enriching", "composing", "retry"}:
            try:
                progress_age = seconds_since(as_of, checkpoint.get("last_progress_at"), "checkpoint.last_progress_at", maximum_future_skew)
                if progress_age > int(source["stage_deadlines_seconds"].get(status, source["stage_deadlines_seconds"]["retry"])):
                    add("processor", "stage_progress_stalled", "error", _fault_action(source, "processor_stalled"))
            except ValueError as exc:
                add("processor", str(exc), "error", _fault_action(source, "processor_stalled"))
            if status in {"validating", "enriching", "composing"}:
                try:
                    heartbeat_age = seconds_since(as_of, checkpoint.get("last_heartbeat_at"), "checkpoint.last_heartbeat_at", maximum_future_skew)
                    if heartbeat_age > int(source["heartbeat_timeout_seconds"]):
                        add("processor", "heartbeat_stale", "error", _fault_action(source, "processor_stalled"))
                except ValueError as exc:
                    add("processor", str(exc), "error", _fault_action(source, "processor_stalled"))
            lease = checkpoint.get("lease")
            if isinstance(lease, dict):
                try:
                    if parse_time(lease.get("expires_at"), "checkpoint.lease.expires_at") <= as_of:
                        add("processor", "lease_expired", "error", _fault_action(source, "processor_stalled"))
                except ValueError as exc:
                    add("processor", str(exc), "error", _fault_action(source, "processor_stalled"))
            elif status in {"validating", "enriching", "composing"}:
                add("processor", "active_stage_lease_missing", "error", _fault_action(source, "processor_stalled"))
        if status == "retry":
            attempts_by_id = checkpoint.get("attempts_by_id") if isinstance(checkpoint.get("attempts_by_id"), dict) else {}
            detail_budget = int(source.get("retries_per_detail", 0)) + 1
            for detail in checkpoint.get("detail_records", []):
                if not isinstance(detail, dict) or detail.get("status") != "quarantined":
                    continue
                identity = str(detail.get("id", ""))
                if identity and int(attempts_by_id.get(identity, 0)) >= detail_budget:
                    failure_identity = sha256_bytes(canonical_json({"id": identity, "source_sha256": detail.get("source_sha256"), "guide_sha256": detail.get("guide_sha256")}))
                    add("processor", "retry_exhausted", "error", _fault_action(source, "processor_stalled"), failure_identity)
                    break
        if status == "quarantined":
            add("processor", "generation_quarantined", "error", _fault_action(source, "processor_stalled"))
        if status in {"queued", "validating", "enriching", "composing", "retry"}:
            for reference in checkpoint.get("input_artifacts", []):
                try:
                    expiry = parse_time(reference.get("expires_at"), "checkpoint.input_artifact.expires_at")
                    artifact_id = str(reference.get("artifact_id") or "")
                    artifact_meta = artifact_by_id.get(artifact_id)
                    if artifact_meta and artifact_meta.get("availability_error") is True:
                        add("processor", "artifact_availability_unavailable", "error", _fault_action(source, "processor_stalled"), artifact_id)
                    elif expiry <= as_of or (artifact_meta and artifact_meta.get("expired") is True):
                        add("processor", "input_artifact_expired", "error", _fault_action(source, "processor_stalled"))
                        break
                except ValueError as exc:
                    add("processor", str(exc), "error", _fault_action(source, "processor_stalled"))
                    break
        if status in {"ready", "no-change"}:
            if outcome.get("composer_status") == "ready_scoped" or int(outcome.get("pending_count", 0) or 0) > 0:
                add("candidate", "candidate_ready_with_pending_scope", "warning", _fault_action(source, "processor_stalled"))
            output_artifact = checkpoint.get("output_artifact") if isinstance(checkpoint.get("output_artifact"), dict) else {}
            artifact_id = str(output_artifact.get("artifact_id") or "")
            artifact_meta = artifact_by_id.get(artifact_id)
            try:
                output_expiry = parse_time(output_artifact.get("expires_at"), "checkpoint.output_artifact.expires_at")
                if artifact_meta and artifact_meta.get("availability_error") is True:
                    add("candidate", "artifact_availability_unavailable", "error", _fault_action(source, "processor_stalled"), artifact_id)
                elif not artifact_id or output_expiry <= as_of or (artifact_meta and artifact_meta.get("expired") is True):
                    add("candidate", "candidate_output_unavailable", "error", _fault_action(source, "processor_stalled"))
            except ValueError as exc:
                add("candidate", str(exc), "error", _fault_action(source, "processor_stalled"))
            bundle_valid, bundle_issue = processor_output_bundle_valid(checkpoint)
            if not bundle_valid:
                add("candidate", bundle_issue, "error", _fault_action(source, "processor_stalled"))

    main_revision = str(main_identity.get("revision", ""))
    screen_result: Any = None
    screen_once: Any | None = processor_candidate_screen
    if (
        mode == "live"
        and isinstance(checkpoint, dict)
        and checkpoint.get("status") in {"ready", "no-change"}
        and callable(processor_candidate_screen)
    ):
        try:
            screen_result = processor_candidate_screen(checkpoint)
        except Exception:
            screen_result = _candidate_screen_result(
                status="unavailable", stage="processor_bundle_screen",
                reason_code="processor_screen_unavailable",
            )
        screen_once = lambda _checkpoint: screen_result
    candidate_diagnostic: dict[str, Any] = {}
    already_canonical_candidate = already_canonical_candidate_relation(
        checkpoint, main_identity, main_revision, mode, screen_once,
        diagnostic=candidate_diagnostic,
    )
    current_candidate_evaluation = current_candidate_evaluation_record(
        checkpoint, main_identity, main_revision, mode, candidate_diagnostic, screen_result,
        evaluator_source_sha,
    )
    link_contract_diagnostics = link_contract_diagnostic_projection(
        checkpoint, screen_result, current_candidate_evaluation, mode,
    )
    terminal_current_subject = _terminal_current_subject(
        checkpoint, screen_result, main_identity, already_canonical_candidate, mode,
    )
    promotion_terminal_evidence = finalize_terminal_outcome_records(
        promotion_terminal_collection, current_subject=terminal_current_subject,
        as_of=as_of, mode=mode,
    )

    promotion_state = "unavailable"
    publication = None
    promotion_stage: dict[str, Any] | None = None
    last_good_for_source = last_good.get(source_id) if isinstance(last_good, dict) else None
    promotion_runs = promotion_runs_by_id or {}
    source_records = promotion_records_for_source(promotion_ack, source_id)
    current_records: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    trusted_readbacks: list[tuple[tuple[dt.datetime, int, int], dict[str, Any]]] = []
    for record in source_records:
        candidate = record.get("candidate") if isinstance(record, dict) else None
        if not isinstance(candidate, dict) or str(candidate.get("repository", repository)).casefold() != repository.casefold():
            add("promotion", "promotion_record_identity_invalid", "error", _fault_action(source, "promotion_wait"))
            continue
        record_generation = str(candidate.get("generation_id", ""))
        accepted = promotion_items_for_source(record, source_id, record_generation)
        if not accepted:
            add("promotion", "promotion_record_transition_invalid", "error", _fault_action(source, "promotion_wait"), record_generation)
            continue
        final_item = accepted[-1]
        if record_generation == generation_id and record.get("superseded_by") is None:
            current_records.append((record, accepted))
        if final_item.get("status") != "read-back-confirmed":
            continue
        claimed_publication = promotion_publication(final_item)
        trusted_run = trusted_promotion_run(
            final_item, promotion_runs, repository, promotion_workflow_paths or {}, workflow_ids_by_path or {},
            as_of, maximum_future_skew, recovery_publisher_runs_by_id,
        )
        if claimed_publication is None or claimed_publication.get("verified") is not True:
            if record_generation == generation_id:
                add("publication", "readback_receipt_invalid", "error", _fault_action(source, "publication_failure"), record_generation)
            continue
        if trusted_run is None:
            if record_generation == generation_id:
                add("publication", "promotion_readback_run_unverified", "error", _fault_action(source, "publication_failure"), str(final_item.get("run_id", "")))
            continue
        claimed_publication.update({
            "source_id": source_id,
            "generation_id": record_generation,
            "publication_run_jobs_completed_at": trusted_run["jobs_completed_at"],
            "publication_run_completion_basis": trusted_run["completion_basis"],
            "publication_run_id": trusted_run["run_id"],
            "publication_run_attempt": trusted_run["run_attempt"],
            "publisher_run_verified": True,
        })
        trusted_readbacks.append((publication_order(claimed_publication), claimed_publication))

    if len(current_records) > 1:
        # A durable refresh intent intentionally overlaps its predecessor
        # until the exact new PR head/body has been read back. Keep reporting
        # the predecessor as active while the target is only prepared.
        prepared_refreshes = [
            record for record, _accepted in current_records
            if record.get("status") == "prepared" and isinstance(record.get("refresh_from"), dict)
        ]
        if len(prepared_refreshes) == 1:
            predecessor_ref = prepared_refreshes[0]["refresh_from"]
            predecessors = [
                item for item in current_records
                if (
                    item[0].get("candidate", {}).get("registry_sha256") == predecessor_ref.get("registry_sha256")
                    and item[0].get("candidate", {}).get("head_sha") == predecessor_ref.get("head_sha")
                    and item[0].get("ownership", {}).get("body_sha256") == predecessor_ref.get("body_sha256")
                    and item[0].get("ownership", {}).get("branch") == predecessor_ref.get("branch")
                )
            ]
            if len(predecessors) == 1:
                current_records = predecessors
            else:
                add("promotion", "promotion_generation_record_ambiguous", "error", _fault_action(source, "promotion_wait"), generation_id)
        else:
            output_digests = checkpoint.get("output_digests") if isinstance(checkpoint, dict) else None
            composed_candidate_digests = [
                row.get("sha256") for row in output_digests
                if isinstance(row, dict) and row.get("path") == "composed-candidate.registry.json"
            ] if isinstance(output_digests, list) else []
            composed_candidate_sha = composed_candidate_digests[0] if len(composed_candidate_digests) == 1 else None
            exact_payloads = [
                item for item in current_records
                if item[0].get("candidate", {}).get("registry_sha256") == composed_candidate_sha
            ]
            competing_active = [
                item for item in current_records
                if item not in exact_payloads and item[0].get("status") != "read-back-confirmed"
            ]
            if len(exact_payloads) == 1 and not competing_active:
                # A later B output may legitimately create a new owned PR after
                # the prior payload was already published and read back. Keep
                # that confirmed row as last-good while tracking the exact
                # current composed payload as the active candidate.
                current_records = exact_payloads
            else:
                add("promotion", "promotion_generation_record_ambiguous", "error", _fault_action(source, "promotion_wait"), generation_id)
    current_items = current_records[0][1] if len(current_records) == 1 else []
    if already_canonical_candidate is not None and not current_records:
        # The current B generation has no C record because its exact payload is
        # already present on main. Preserve any independent delivery state for
        # an older record that proves the same canonical bytes; the no-op
        # relation is not evidence that its publication acknowledgement ran.
        matching_history: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for record in source_records:
            candidate = record.get("candidate") if isinstance(record, dict) else None
            if not isinstance(candidate, dict) or record.get("superseded_by") is not None:
                continue
            record_generation = str(candidate.get("generation_id", ""))
            if (
                record_generation == generation_id
                or str(candidate.get("repository", "")).casefold() != repository.casefold()
                or candidate.get("registry_path") != main_identity.get("registry_path")
                or isinstance(candidate.get("registry_bytes"), bool)
                or candidate.get("registry_bytes") != main_identity.get("registry_bytes")
                or candidate.get("registry_sha256") != main_identity.get("registry_sha256")
            ):
                continue
            accepted = promotion_items_for_source(record, source_id, record_generation)
            if accepted:
                matching_history.append((record, accepted))

        if len(matching_history) > 1:
            add(
                "promotion", "promotion_canonical_history_ambiguous", "error",
                _fault_action(source, "promotion_wait"),
            )
        elif len(matching_history) == 1:
            historical_record, historical_items = matching_history[0]
            historical_candidate = historical_record["candidate"]
            historical_generation = str(historical_candidate["generation_id"])
            historical_item = historical_items[-1]
            historical_status = str(historical_item.get("status", "unknown"))
            historical_run = None if historical_status == "prepared" else trusted_promotion_run(
                historical_item, promotion_runs, repository, promotion_workflow_paths or {}, workflow_ids_by_path or {},
                as_of, maximum_future_skew, recovery_publisher_runs_by_id,
            )
            if historical_status in {"merged", "publication-pending", "published", "read-back-confirmed"}:
                publication = promotion_publication(historical_item)
                if publication is not None:
                    publication.update({
                        "source_id": source_id,
                        "generation_id": historical_generation,
                        "publisher_run_verified": historical_run is not None if historical_status == "read-back-confirmed" else None,
                    })

            if historical_status == "prepared":
                add(
                    "promotion", "promotion_ack_missing", "warning",
                    _promotion_recovery_action(source, promotion_workflow_path), historical_generation,
                    generation_id=historical_generation,
                )
                historical_deadline_key = "pending-review"
            elif historical_status == "pending-review":
                add(
                    "promotion", "promotion_pending_review", "info", _fault_action(source, "promotion_wait"),
                    historical_generation, generation_id=historical_generation,
                )
                historical_deadline_key = "pending-review"
            elif historical_status in {"merged", "publication-pending", "published"}:
                add(
                    "publication", "publication_or_readback_pending", "warning",
                    _fault_action(source, "promotion_wait"), historical_generation,
                    generation_id=historical_generation,
                )
                historical_deadline_key = "publication-pending"
            else:
                historical_deadline_key = None

            if historical_status != "prepared" and historical_run is None:
                if historical_status == "read-back-confirmed":
                    historical_stage = "publication"
                    reason = "promotion_readback_run_unverified"
                    action = _fault_action(source, "publication_failure")
                else:
                    historical_stage = "promotion"
                    reason = "promotion_ack_run_unverified"
                    action = _fault_action(source, "promotion_wait")
                add(
                    historical_stage, reason, "error", action,
                    str(historical_item.get("run_id", "")), generation_id=historical_generation,
                )

            if historical_status == "failed":
                add(
                    "publication", "publication_or_promotion_failed", "error",
                    _fault_action(source, "publication_failure"), historical_generation,
                    generation_id=historical_generation,
                )
            elif historical_status == "closed":
                add(
                    "promotion", "candidate_closed_without_acknowledged_publication", "warning",
                    _fault_action(source, "promotion_wait"), historical_generation,
                    generation_id=historical_generation,
                )
            elif historical_status == "read-back-confirmed":
                claimed_publication = promotion_publication(historical_item)
                if claimed_publication is None or claimed_publication.get("verified") is not True:
                    add(
                        "publication", "readback_receipt_invalid", "error",
                        _fault_action(source, "publication_failure"), historical_generation,
                        generation_id=historical_generation,
                    )

            if historical_deadline_key is not None:
                entered_at = historical_item.get("observed_at")
                deadline = int(source["stage_deadlines_seconds"][historical_deadline_key])
                try:
                    stage_age = seconds_since(as_of, entered_at, "historical_promotion_stage.observed_at", maximum_future_skew)
                    if stage_age > deadline:
                        if historical_status == "prepared":
                            add(
                                "promotion", "promotion_prepared_delivery_overdue", "warning",
                                _promotion_recovery_action(source, promotion_workflow_path), historical_generation,
                                generation_id=historical_generation,
                            )
                        elif historical_deadline_key == "pending-review":
                            add(
                                "promotion", "promotion_review_wait_overdue", "warning",
                                _fault_action(source, "promotion_wait"), historical_generation,
                                generation_id=historical_generation,
                            )
                        else:
                            add(
                                "publication", "publication_readback_lag_overdue", "error",
                                _fault_action(source, "promotion_wait"), historical_generation,
                                generation_id=historical_generation,
                            )
                except ValueError:
                    if historical_status == "prepared":
                        add(
                            "promotion", "promotion_prepared_stage_clock_invalid", "warning",
                            _promotion_recovery_action(source, promotion_workflow_path), historical_generation,
                            generation_id=historical_generation,
                        )
                    else:
                        add(
                            "promotion", "promotion_stage_clock_invalid", "error",
                            _fault_action(source, "promotion_wait"), historical_generation,
                            generation_id=historical_generation,
                        )
    if current_items:
        current_item = current_items[-1]
        promotion_state = str(current_item.get("status", "unknown"))
        current_run = None if promotion_state == "prepared" else trusted_promotion_run(
            current_item, promotion_runs, repository, promotion_workflow_paths or {}, workflow_ids_by_path or {},
            as_of, maximum_future_skew, recovery_publisher_runs_by_id,
        )
        publication = None if promotion_state == "prepared" else promotion_publication(current_item)
        if publication is not None:
            publication["source_id"] = source_id
            publication["generation_id"] = generation_id
            publication["publisher_run_verified"] = current_run is not None if promotion_state == "read-back-confirmed" else None

        if promotion_state == "prepared":
            add(
                "promotion", "promotion_ack_missing", "warning",
                _promotion_recovery_action(source, promotion_workflow_path), generation_id,
            )
            stage_deadline_key = "pending-review"
        elif promotion_state == "pending-review":
            add("promotion", "promotion_pending_review", "info", _fault_action(source, "promotion_wait"))
            stage_deadline_key = "pending-review"
        elif promotion_state in {"merged", "publication-pending", "published"}:
            add("publication", "publication_or_readback_pending", "warning", _fault_action(source, "promotion_wait"))
            stage_deadline_key = "publication-pending"
        else:
            stage_deadline_key = None

        if stage_deadline_key is not None:
            stage_entered_at = current_item.get("observed_at")
            stage_deadline = int(source["stage_deadlines_seconds"][stage_deadline_key])
            if promotion_state == "prepared" and stage_entered_at is None:
                promotion_stage = {
                    "status": "prepared",
                    "entered_at": None,
                    "age_seconds": None,
                    "deadline_seconds": stage_deadline,
                }
                add(
                    "promotion", "promotion_prepared_stage_clock_unavailable", "warning",
                    _promotion_recovery_action(source, promotion_workflow_path), generation_id,
                )
            else:
                try:
                    stage_age = seconds_since(as_of, stage_entered_at, "promotion_stage.observed_at", maximum_future_skew)
                    promotion_stage = {
                        "status": promotion_state,
                        "entered_at": stage_entered_at,
                        "age_seconds": stage_age,
                        "deadline_seconds": stage_deadline,
                    }
                    if stage_age > stage_deadline:
                        if promotion_state == "prepared":
                            add(
                                "promotion", "promotion_prepared_delivery_overdue", "warning",
                                _promotion_recovery_action(source, promotion_workflow_path), generation_id,
                            )
                        elif stage_deadline_key == "pending-review":
                            add("promotion", "promotion_review_wait_overdue", "warning", _fault_action(source, "promotion_wait"))
                        else:
                            add("publication", "publication_readback_lag_overdue", "error", _fault_action(source, "promotion_wait"))
                except ValueError:
                    if promotion_state == "prepared":
                        promotion_stage = {
                            "status": "prepared",
                            "entered_at": None,
                            "age_seconds": None,
                            "deadline_seconds": stage_deadline,
                        }
                        add(
                            "promotion", "promotion_prepared_stage_clock_invalid", "warning",
                            _promotion_recovery_action(source, promotion_workflow_path), generation_id,
                        )
                    else:
                        add("promotion", "promotion_stage_clock_invalid", "error", _fault_action(source, "promotion_wait"), generation_id)

        if promotion_state not in {"prepared"}:
            if current_run is None:
                reason = "promotion_readback_run_unverified" if promotion_state == "read-back-confirmed" else "promotion_ack_run_unverified"
                stage = "publication" if promotion_state == "read-back-confirmed" else "promotion"
                add(stage, reason, "error", _fault_action(source, "publication_failure" if stage == "publication" else "promotion_wait"), str(current_item.get("run_id", "")))

        if promotion_state == "failed":
            add("publication", "publication_or_promotion_failed", "error", _fault_action(source, "publication_failure"))
        elif promotion_state == "closed":
            add("promotion", "candidate_closed_without_acknowledged_publication", "warning", _fault_action(source, "promotion_wait"))
        elif promotion_state == "read-back-confirmed":
            if publication is None or publication.get("verified") is not True:
                add("publication", "readback_receipt_invalid", "error", _fault_action(source, "publication_failure"))
            elif publication.get("publisher_run_verified") is not True:
                add("publication", "promotion_readback_run_unverified", "error", _fault_action(source, "publication_failure"), str(current_item.get("run_id", "")))
    elif checkpoint is not None and checkpoint.get("status") == "ready":
        if already_canonical_candidate is None:
            add(
                "promotion", "promotion_ack_missing", "warning",
                _promotion_recovery_action(source, promotion_workflow_path), generation_id,
            )
            if source_records:
                add("promotion", "promotion_record_for_different_generation", "warning", _fault_action(source, "promotion_wait"), generation_id)

    if promotion_workflow_api_error:
        add(
            "promotion-execution", "promotion_workflow_observations_unavailable", "error",
            _promotion_recovery_action(source, promotion_workflow_path), promotion_workflow_path,
        )
    for identity in sorted(c_execution_errors):
        add(
            "promotion-execution", "promotion_workflow_attempt_unavailable", "error",
            _promotion_recovery_action(source, promotion_workflow_path), identity,
        )
    if c_failure:
        execution_fault = fault(
            source_id, "promotion-execution", "promotion_workflow_run_failed", "error", owner_ticket,
            _promotion_recovery_action(source, promotion_workflow_path),
            f"{c_failure['run_id']}/{c_failure['run_attempt']}",
        )
        execution_fault["execution_identity"] = {
            "run_id": c_failure["run_id"],
            "run_attempt": c_failure["run_attempt"],
            "run_started_at": c_failure["run_started_at"],
            "head_sha": c_failure["head_sha"],
        }
        faults.append(execution_fault)
    for prior in prior_promotion_execution_faults or []:
        if (
            not isinstance(prior, dict)
            or prior.get("source_id") != source_id
            or prior.get("stage") != "promotion-execution"
            or prior.get("reason") != "promotion_workflow_run_failed"
            or prior.get("severity") != "error"
            or not isinstance(prior.get("fault_key"), str)
        ):
            continue
        prior_identity = prior.get("execution_identity")
        prior_order = promotion_execution_run_order({
            "run_id": prior_identity.get("run_id"),
            "run_attempt": prior_identity.get("run_attempt"),
            "run_started_at": prior_identity.get("run_started_at"),
        }) if isinstance(prior_identity, dict) else None
        recovered_after_failure = (
            c_success_order is not None
            and c_failure is None
            and not c_execution_errors
            and not promotion_workflow_api_error
            and prior_order is not None
            and c_success_order > prior_order
        )
        if not recovered_after_failure and not any(row.get("fault_key") == prior["fault_key"] for row in faults):
            faults.append({key: prior[key] for key in (
                "source_id", "stage", "reason", "severity", "owner_ticket", "fault_key", "recommended_action", "execution_identity",
            ) if key in prior})

    if trusted_readbacks:
        latest_order, latest_publication = max(trusted_readbacks, key=lambda entry: entry[0])
        existing_order = publication_order(last_good_for_source)
        if last_good_for_source is None:
            last_good_for_source = latest_publication
        elif existing_order is not None:
            if latest_order > existing_order:
                last_good_for_source = latest_publication
            elif latest_order == existing_order and latest_publication != last_good_for_source:
                add("publication", "last_good_publication_order_ambiguous", "warning", _fault_action(source, "publication_failure"))
        elif (
            isinstance(last_good_for_source, dict)
            and last_good_for_source.get("source_id") == latest_publication.get("source_id")
            and last_good_for_source.get("generation_id") == latest_publication.get("generation_id")
            and last_good_for_source.get("publication_revision") == latest_publication.get("publication_revision")
            and last_good_for_source.get("publication_pointer_revision") == latest_publication.get("publication_pointer_revision")
            and last_good_for_source.get("artifact_identity") == latest_publication.get("artifact_identity")
        ):
            last_good_for_source = latest_publication
        else:
            add("publication", "last_good_publication_order_ambiguous", "warning", _fault_action(source, "publication_failure"))

    canonical_report = {
        "main": main_identity,
        "last_good": last_good_for_source,
        "promotion_status": promotion_state,
        "publication": publication,
        "promotion_stage": promotion_stage,
        "promotion_execution": {
            "latest_execution_run": c_latest,
            "latest_successful_execution_run": c_success,
            "execution_failure": c_failure,
        },
        "promotion_terminal_evidence": promotion_terminal_evidence,
        "current_candidate_evaluation": current_candidate_evaluation,
        "already_canonical_candidate": already_canonical_candidate,
    }
    unique_faults = {row["fault_key"]: row for row in faults}
    faults = [unique_faults[key] for key in sorted(unique_faults)]
    severity_rank = {"info": 0, "warning": 1, "error": 2}
    worst = max((severity_rank.get(row["severity"], 2) for row in faults), default=0)
    overall = "healthy" if worst == 0 else "degraded" if worst == 1 else "blocked"
    if mode == "fixture":
        overall = "fixture"
    latest_execution_report = report_workflow_run(latest_run, latest_run_artifacts) if latest_run else None
    if (
        latest_execution_report is not None
        and collector_execution.get("latest_success") is not None
        and collector_execution["latest_success"]["run_id"] not in collector_execution.get("unavailable_run_ids", [])
    ):
        latest_execution_report["latest_successful_execution_run"] = collector_execution["latest_success"]
    return {
        "source_id": source_id,
        "overall": overall,
        "observation": {
            "state": observation_state,
            "observed_at": observation.get("observed_at") if observation else None,
            "producer_run_id": observation.get("producer_run_id") if observation else None,
            "refresh_evidence_sha256": observation.get("refresh_evidence_sha256") if observation else None,
            "execution_mode": "fixture" if mode == "fixture" and observation else (observation.get("execution_mode") if observation else None),
            "collection_status": observation.get("collection_status") if observation else None,
            "age_seconds": observation_age,
            "max_age_seconds": int(source["max_observation_age_seconds"]),
        },
        "processor": {
            "state": processor_state,
            "generation_id": generation_id or None,
            "checkpoint_sha256": checkpoint.get("checkpoint_sha256") if checkpoint else None,
            "candidate_sha256": (checkpoint.get("generation_inputs", {}).get("candidate_sha256") if checkpoint and isinstance(checkpoint.get("generation_inputs"), dict) else None),
            "checkpoint_observed_at": checkpoint.get("observed_at") if checkpoint else None,
            "last_observation": checkpoint.get("last_observation") if checkpoint else None,
            "last_heartbeat_at": checkpoint.get("last_heartbeat_at") if checkpoint else None,
            "last_progress_at": checkpoint.get("last_progress_at") if checkpoint else None,
            "attempts_consumed": checkpoint.get("attempts_consumed") if checkpoint else None,
            "attempts_by_id": checkpoint.get("attempts_by_id") if checkpoint else None,
            "request_reservation": checkpoint.get("request_reservation") if checkpoint else None,
            "detail_records": checkpoint.get("detail_records") if checkpoint else None,
            "link_contract_diagnostics": link_contract_diagnostics,
            "detail_queue_cursor": checkpoint.get("detail_queue_cursor") if checkpoint else None,
            "lease": checkpoint.get("lease") if checkpoint else None,
            "output_artifact": checkpoint.get("output_artifact") if checkpoint else None,
            "output_digests": checkpoint.get("output_digests") if checkpoint else None,
            "outcome": checkpoint.get("outcome") if checkpoint else None,
            "latest_execution_run": latest_processor_run,
            "latest_successful_execution_run": latest_processor_success,
            "execution_failure": execution_failure,
        },
        "collector": {
            "latest_scheduled_run": report_workflow_run(latest_scheduled_run, latest_scheduled_artifacts) if latest_scheduled_run else None,
            "latest_execution_run": latest_execution_report,
            "age_seconds": schedule_age,
            "expected_interval_seconds": int(source["expected_interval_seconds"]),
            "grace_seconds": int(source["observation_grace_seconds"]),
        },
        "canonical": canonical_report,
        "faults": faults,
        "recommended_recovery": sorted({row["recommended_action"] for row in faults if row["severity"] != "info"}),
    }


def promotion_items_for_source(ack: dict[str, Any], source_id: str, generation_id: str) -> list[dict[str, Any]]:
    """Bind the append-only acknowledgement history to its one canonical candidate."""
    candidate = ack.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("source_id") != source_id or candidate.get("generation_id") != generation_id:
        return []
    rows = ack.get("acknowledgements")
    if not isinstance(rows, list):
        return []
    previous_status = "prepared"
    merge_sha = None
    accepted: list[dict[str, Any]] = []
    transitions = {
        "prepared": {"pending-review", "failed", "closed"},
        "pending-review": {"merged", "failed", "closed"},
        "merged": {"publication-pending", "failed"},
        "publication-pending": {"published", "failed"},
        "published": {"read-back-confirmed", "failed"},
        "read-back-confirmed": set(), "failed": {"publication-pending", "closed"}, "closed": set(),
    }
    expected_artifact = {
        "path": candidate.get("registry_path"),
        "bytes": candidate.get("registry_bytes"),
        "sha256": candidate.get("registry_sha256"),
    }
    previous_observed_at: dt.datetime | None = None
    published_revision: str | None = None
    published_pointer_revision: str | None = None
    for row in rows:
        if not isinstance(row, dict):
            return []
        status = row.get("status")
        if status not in transitions.get(previous_status, set()):
            return []
        if row.get("manifest_sha256") != candidate.get("manifest_sha256") or row.get("artifact_identity") != expected_artifact:
            return []
        try:
            observed_at = parse_time(row.get("observed_at"), "promotion_ack.observed_at")
        except ValueError:
            return []
        run_id = row.get("run_id")
        run_attempt = row.get("run_attempt")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1 or isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or run_attempt < 1:
            return []
        run_url = row.get("run_url")
        try:
            parsed_url = urllib.parse.urlsplit(str(run_url))
        except ValueError:
            return []
        expected_url_path = f"/{candidate.get('repository')}/actions/runs/{run_id}/attempts/{run_attempt}"
        if parsed_url.scheme != "https" or parsed_url.netloc != "github.com" or parsed_url.path.casefold() != expected_url_path.casefold() or parsed_url.query or parsed_url.fragment:
            return []
        if previous_observed_at is not None and observed_at < previous_observed_at:
            return []
        previous_observed_at = observed_at
        if status == "pending-review":
            if row.get("source_sha") != candidate.get("head_sha"):
                return []
        elif status == "merged":
            merge_sha = row.get("source_sha")
        elif merge_sha is not None and status in {"publication-pending", "published", "read-back-confirmed", "failed"} and row.get("source_sha") != merge_sha:
            return []
        if status == "published":
            published_revision = row.get("publication_revision")
            published_pointer_revision = row.get("publication_pointer_revision")
            if not re.fullmatch(r"[a-f0-9]{40}", str(published_revision or "")) or not re.fullmatch(r"[a-f0-9]{40}", str(published_pointer_revision or "")):
                return []
        elif status == "read-back-confirmed":
            if (
                row.get("publication_revision") != published_revision
                or row.get("publication_pointer_revision") != published_pointer_revision
            ):
                return []
        if status == "read-back-confirmed":
            if (
                row.get("read_back_verified") is not True
                or row.get("read_back_sha256") != candidate.get("registry_sha256")
                or row.get("read_back_bytes") != candidate.get("registry_bytes")
                or row.get("publication_revision") is None
            ):
                return []
        previous_status = str(status)
        accepted.append(row)
    if ack.get("status") != previous_status:
        return []
    if not accepted and ack.get("status") == "prepared":
        readback = candidate.get("payload_readback")
        observed_at = readback.get("observed_at") if isinstance(readback, dict) else None
        accepted.append({"status": "prepared", "observed_at": observed_at})
    return accepted


def promotion_records_for_source(journal: dict[str, Any] | None, source_id: str) -> list[dict[str, Any]]:
    if not isinstance(journal, dict):
        return []
    if journal.get("schema_version") == "datapan.canonical-update-promotion-journal.v1":
        records = journal.get("records")
        return [row for row in records if isinstance(row, dict) and isinstance(row.get("candidate"), dict) and row["candidate"].get("source_id") == source_id] if isinstance(records, list) else []
    candidate = journal.get("candidate")
    return [journal] if isinstance(candidate, dict) and candidate.get("source_id") == source_id else []


def promotion_run_references(journal: dict[str, Any] | None, source_ids: set[str]) -> set[tuple[str, int]]:
    references: set[tuple[str, int]] = set()
    if not isinstance(journal, dict):
        return references
    for source_id in source_ids:
        for record in promotion_records_for_source(journal, source_id):
            acknowledgements = record.get("acknowledgements")
            if not isinstance(acknowledgements, list) or not acknowledgements:
                continue
            final_item = acknowledgements[-1]
            if not isinstance(final_item, dict):
                continue
            run_id = final_item.get("run_id")
            run_attempt = final_item.get("run_attempt")
            if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0 and isinstance(run_attempt, int) and not isinstance(run_attempt, bool) and run_attempt > 0:
                references.add((str(run_id), run_attempt))
    return references


def parse_publication_recovery_reference(reference: Any) -> dict[str, Any] | None:
    if not isinstance(reference, str) or not reference.startswith(PUBLICATION_RECOVERY_REFERENCE_PREFIX):
        return None
    if len(reference.encode("utf-8")) > 4096:
        raise ValueError("publication_recovery_reference_too_large")
    body = reference[len(PUBLICATION_RECOVERY_REFERENCE_PREFIX):]

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("publication_recovery_reference_duplicate_key")
            value[key] = item
        return value

    try:
        value = json.loads(body, object_pairs_hook=unique_pairs)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("publication_recovery_reference_invalid_json") from exc
    expected = {
        "artifact_id", "manifest_sha256", "payload_revision", "pointer_revision",
        "publisher_head_sha", "publisher_run_attempt", "publisher_run_id",
        "publisher_workflow_id", "publisher_workflow_path", "receipt_sha256",
        "repository", "source_sha",
    }
    if not isinstance(value, dict) or set(value) != expected or canonical_json(value).decode("utf-8") != body:
        raise ValueError("publication_recovery_reference_fields_invalid")
    for key in ("artifact_id", "publisher_run_id", "publisher_run_attempt", "publisher_workflow_id"):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("publication_recovery_reference_integer_invalid")
    for key in ("publisher_head_sha", "source_sha", "payload_revision", "pointer_revision"):
        if not isinstance(value.get(key), str) or not re.fullmatch(r"[a-f0-9]{40}", value[key]) or value[key] == "0" * 40:
            raise ValueError("publication_recovery_reference_revision_invalid")
    for key in ("manifest_sha256", "receipt_sha256"):
        if not isinstance(value.get(key), str) or not DIGEST.fullmatch(value[key]) or value[key] == "0" * 64:
            raise ValueError("publication_recovery_reference_digest_invalid")
    if value.get("publisher_workflow_path") != PUBLICATION_WORKFLOW_PATH or not isinstance(value.get("repository"), str):
        raise ValueError("publication_recovery_reference_identity_invalid")
    return value


def publication_recovery_references(
    journal: dict[str, Any] | None, source_ids: set[str], repository: str,
) -> dict[str, dict[str, Any]]:
    references: dict[str, dict[str, Any]] = {}
    for source_id in source_ids:
        for record in promotion_records_for_source(journal, source_id):
            acknowledgements = record.get("acknowledgements")
            if not isinstance(acknowledgements, list) or not acknowledgements:
                continue
            final_item = acknowledgements[-1]
            if not isinstance(final_item, dict):
                continue
            parsed = parse_publication_recovery_reference(final_item.get("evidence_reference"))
            if parsed is None:
                continue
            if parsed["repository"].casefold() != repository.casefold():
                raise ValueError("publication_recovery_reference_repository_mismatch")
            identity = f"{parsed['publisher_run_id']}/{parsed['publisher_run_attempt']}"
            prior = references.get(identity)
            if prior is not None and prior != parsed:
                raise ValueError("publication_recovery_reference_identity_conflict")
            references[identity] = parsed
    if len(references) > MAX_PUBLICATION_RECOVERY_REFERENCES:
        raise ValueError("publication_recovery_reference_limit_exceeded")
    return references


def trusted_recovery_publisher(
    item: dict[str, Any], recovery_reference: dict[str, Any],
    recovery_runs_by_id: dict[str, dict[str, Any]], repository: str,
    workflow_id: int | None, as_of: dt.datetime, maximum_future_skew: int,
    acknowledgement_completed_at: dt.datetime,
) -> dict[str, Any] | None:
    publisher_id = recovery_reference.get("publisher_run_id")
    attempt = recovery_reference.get("publisher_run_attempt")
    identity = f"{publisher_id}/{attempt}"
    evidence = recovery_runs_by_id.get(identity)
    if not isinstance(evidence, dict) or evidence.get("availability_error") is True:
        return None
    run = evidence.get("run")
    jobs = evidence.get("jobs")
    if (
        not isinstance(run, dict)
        or evidence.get("attempt_number") != attempt
        or evidence.get("jobs_api_endpoint") != f"repos/{repository}/actions/runs/{publisher_id}/attempts/{attempt}/jobs"
        or str(run.get("id", "")) != str(publisher_id)
        or run.get("run_attempt") != attempt
        or run.get("workflow_id") != workflow_id
        or run.get("path") != PUBLICATION_WORKFLOW_PATH
        or run.get("head_sha") != recovery_reference.get("publisher_head_sha")
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or not trusted_main_workflow_run(run, repository, PUBLICATION_WORKFLOW_PATH, workflow_id, {"workflow_dispatch"})
        or not isinstance(jobs, list)
        or len(jobs) != evidence.get("job_count")
        or evidence.get("job_count") != 1
    ):
        return None
    job = jobs[0]
    if (
        not isinstance(job, dict)
        or job.get("name") != "validate"
        or job.get("run_id") != publisher_id
        or job.get("run_attempt") != attempt
        or job.get("head_sha") != recovery_reference.get("publisher_head_sha")
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
    ):
        return None
    steps = job.get("steps")
    if not isinstance(steps, list):
        return None
    required = {
        "Publish two-phase immutable distribution",
        "Verify published pointer anonymously",
    }
    selected = [step for step in steps if isinstance(step, dict) and step.get("name") in required]
    if len(selected) != len(required) or {
        step.get("name") for step in selected
        if step.get("status") == "completed" and step.get("conclusion") == "success"
    } != required:
        return None
    try:
        publisher_started = parse_time(run.get("run_started_at") or run.get("created_at"), "recovery_publisher.run_started_at")
        job_started = parse_time(job.get("started_at"), "recovery_publisher.job_started_at")
        job_completed = parse_time(job.get("completed_at"), "recovery_publisher.job_completed_at")
        acknowledgement_observed = parse_time(item.get("observed_at"), "recovery_acknowledgement.observed_at")
        seconds_since(as_of, utc_timestamp(publisher_started), "recovery_publisher.run_started_at", maximum_future_skew)
        seconds_since(as_of, utc_timestamp(job_started), "recovery_publisher.job_started_at", maximum_future_skew)
        seconds_since(as_of, utc_timestamp(job_completed), "recovery_publisher.job_completed_at", maximum_future_skew)
        seconds_since(as_of, item.get("observed_at"), "recovery_acknowledgement.observed_at", maximum_future_skew)
    except ValueError:
        return None
    if (
        job_started < publisher_started
        or job_completed < job_started
        or job_completed > acknowledgement_observed
        or acknowledgement_observed > acknowledgement_completed_at
    ):
        return None
    publisher_repository = run.get("repository")
    publisher_head_repository = run.get("head_repository")
    if not isinstance(publisher_repository, dict) or not isinstance(publisher_head_repository, dict):
        return None
    if not (
        item.get("source_sha") == recovery_reference.get("source_sha")
        and item.get("manifest_sha256") == recovery_reference.get("manifest_sha256")
        and item.get("publication_revision") == recovery_reference.get("payload_revision")
        and item.get("publication_pointer_revision") == recovery_reference.get("pointer_revision")
        and publisher_repository.get("full_name", "").casefold() == recovery_reference.get("repository", "").casefold()
        and publisher_head_repository.get("full_name", "").casefold() == recovery_reference.get("repository", "").casefold()
    ):
        return None
    return {
        "run_id": int(publisher_id),
        "run_attempt": int(attempt),
        "jobs_completed_at": utc_timestamp(job_completed),
        "completion_basis": "max_completed_at_all_jobs_exact_run_attempt",
    }


def trusted_promotion_run(
    item: dict[str, Any], promotion_runs_by_id: dict[str, dict[str, Any]], repository: str,
    workflow_paths: dict[str, str], workflow_ids_by_path: dict[str, int],
    as_of: dt.datetime, maximum_future_skew: int,
    recovery_publisher_runs_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    run_id = item.get("run_id")
    run_attempt = item.get("run_attempt")
    evidence = promotion_runs_by_id.get(f"{run_id}/{run_attempt}")
    if not isinstance(evidence, dict) or evidence.get("availability_error") is True:
        return None
    run = evidence.get("run")
    jobs = evidence.get("jobs")
    if (
        evidence.get("attempt_number") != run_attempt
        or evidence.get("jobs_api_endpoint") != f"repos/{repository}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
        or isinstance(evidence.get("job_count"), bool)
        or not isinstance(evidence.get("job_count"), int)
        or not isinstance(jobs, list)
        or len(jobs) != evidence.get("job_count")
        or not jobs
        or not isinstance(run, dict)
    ):
        return None
    status = item.get("status")
    if status in {"publication-pending", "published", "read-back-confirmed"}:
        expected_path = workflow_paths.get("publication_ack_workflow_path")
    elif status == "failed":
        allowed_paths = {workflow_paths.get("promotion_workflow_path"), workflow_paths.get("publication_ack_workflow_path")}
        if not any(path and workflow_path_matches(run.get("path"), path) for path in allowed_paths):
            return None
        expected_path = next(path for path in allowed_paths if path and workflow_path_matches(run.get("path"), path))
    else:
        expected_path = workflow_paths.get("promotion_workflow_path")
    event = run.get("event")
    is_acknowledgement = bool(
        workflow_paths.get("publication_ack_workflow_path")
        and expected_path == workflow_paths.get("publication_ack_workflow_path")
    )
    recovery_reference: dict[str, Any] | None = None
    if not is_acknowledgement:
        trusted_event = event in PROMOTION_WORKFLOW_EVENTS
    else:
        trusted_event = event in {"workflow_run", "schedule"}
        raw_reference = item.get("evidence_reference")
        has_reference = raw_reference is not None
        if has_reference and not isinstance(raw_reference, str):
            trusted_event = False
        elif isinstance(raw_reference, str) and raw_reference.startswith("publication-recovery/"):
            try:
                recovery_reference = parse_publication_recovery_reference(raw_reference)
            except ValueError:
                trusted_event = False
            if recovery_reference is None:
                trusted_event = False
        elif event == "schedule":
            # Scheduled recovery has no parent workflow_run event to bind the
            # publisher, so a versioned authenticated reference is mandatory.
            trusted_event = False
        # Ordinary non-versioned workflow_run references remain compatible
        # with the legacy event-triggered acknowledgement contract.
        if recovery_reference is not None:
            publisher_workflow_id = workflow_ids_by_path.get(PUBLICATION_WORKFLOW_PATH)
            trusted_event = bool(
                trusted_event
                and recovery_reference.get("repository", "").casefold() == repository.casefold()
                and recovery_reference.get("publisher_workflow_id") == publisher_workflow_id
            )
        elif event == "schedule":
            trusted_event = False
    if (
        str(run.get("id", "")) != str(run_id)
        or run.get("run_attempt") != run_attempt
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or not trusted_event
        or not expected_path
        or not trusted_main_workflow_run(
            run, repository, expected_path, workflow_ids_by_path.get(expected_path),
            {event} if isinstance(event, str) else set(),
        )
    ):
        return None
    completed_jobs: list[dt.datetime] = []
    seen_job_ids: set[str] = set()
    allowed_job_conclusions = {"success", "skipped", "neutral"}
    expected_head_sha = run["head_sha"]
    for job in jobs:
        if not isinstance(job, dict):
            return None
        job_id = job.get("id")
        if (
            isinstance(job_id, bool)
            or not isinstance(job_id, int)
            or job_id < 1
            or str(job_id) in seen_job_ids
            or str(job.get("run_id", "")) != str(run_id)
            or job.get("status") != "completed"
            or job.get("conclusion") not in allowed_job_conclusions
            or (expected_head_sha and job.get("head_sha") != expected_head_sha)
        ):
            return None
        seen_job_ids.add(str(job_id))
        try:
            job_completed_at = parse_time(job.get("completed_at"), "promotion_job.completed_at")
            seconds_since(as_of, job.get("completed_at"), "promotion_job.completed_at", maximum_future_skew)
        except ValueError:
            return None
        completed_jobs.append(job_completed_at)
    if len(completed_jobs) != evidence.get("job_count"):
        return None
    jobs_completed_at = max(completed_jobs)
    if run.get("completed_at") is not None:
        try:
            seconds_since(as_of, run.get("completed_at"), "promotion_run.completed_at", maximum_future_skew)
        except ValueError:
            return None
    publication_run_id = int(run_id)
    publication_run_attempt = int(run_attempt)
    publication_jobs_completed_at = jobs_completed_at
    if is_acknowledgement and event in {"workflow_run", "schedule"} and recovery_reference is not None:
        publisher_workflow_id = workflow_ids_by_path.get(PUBLICATION_WORKFLOW_PATH)
        trusted_publisher = trusted_recovery_publisher(
            item,
            recovery_reference or {},
            recovery_publisher_runs_by_id or {},
            repository,
            publisher_workflow_id,
            as_of,
            maximum_future_skew,
            jobs_completed_at,
        )
        if trusted_publisher is None:
            return None
        publication_run_id = trusted_publisher["run_id"]
        publication_run_attempt = trusted_publisher["run_attempt"]
        publication_jobs_completed_at = parse_time(
            trusted_publisher["jobs_completed_at"], "recovery_publisher.jobs_completed_at",
        )
    return {
        "jobs_completed_at": utc_timestamp(publication_jobs_completed_at),
        "completion_basis": "max_completed_at_all_jobs_exact_run_attempt",
        "run_id": publication_run_id,
        "run_attempt": publication_run_attempt,
    }


def publication_order(value: dict[str, Any] | None) -> tuple[dt.datetime, int, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        if value.get("publication_run_completion_basis") != "max_completed_at_all_jobs_exact_run_attempt":
            return None
        completed_at = parse_time(value.get("publication_run_jobs_completed_at"), "last_good.publication_run_jobs_completed_at")
        run_id = int(value.get("publication_run_id"))
        run_attempt = int(value.get("publication_run_attempt"))
    except (ValueError, TypeError):
        return None
    if run_id < 1 or run_attempt < 1:
        return None
    return completed_at, run_id, run_attempt


def promotion_publication(item: dict[str, Any]) -> dict[str, Any] | None:
    artifact = item.get("artifact_identity")
    if not isinstance(artifact, dict):
        return None
    status = item.get("status")
    verified = (
        status == "read-back-confirmed"
        and item.get("read_back_verified") is True
        and item.get("read_back_sha256") == artifact.get("sha256")
        and item.get("read_back_bytes") == artifact.get("bytes")
        and isinstance(item.get("publication_revision"), str)
        and bool(re.fullmatch(r"[a-f0-9]{40}", item["publication_revision"]))
        and isinstance(item.get("publication_pointer_revision"), str)
        and bool(re.fullmatch(r"[a-f0-9]{40}", item["publication_pointer_revision"]))
    )
    return {
        "status": status,
        "observed_at": item.get("observed_at"),
        "source_sha": item.get("source_sha"),
        "manifest_sha256": item.get("manifest_sha256"),
        "publication_revision": item.get("publication_revision"),
        "publication_pointer_revision": item.get("publication_pointer_revision"),
        "artifact_identity": artifact,
        "verified": verified,
    }


def processor_output_bundle_valid(checkpoint: dict[str, Any]) -> tuple[bool, str]:
    """Cross-bind the declared artifact bundle without downloading its payload."""
    rows = checkpoint.get("output_digests")
    locator = checkpoint.get("output_artifact")
    if not isinstance(rows, list) or not rows or len(rows) > 16 or not isinstance(locator, dict):
        return False, "candidate_output_bundle_shape_invalid"
    paths: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            return False, "candidate_output_bundle_entry_invalid"
        path = row.get("path")
        digest = row.get("sha256")
        byte_count = row.get("bytes")
        if (
            not isinstance(path, str)
            or path not in PROCESSOR_OUTPUT_PATHS
            or not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
            or byte_count > 536870912
        ):
            return False, "candidate_output_bundle_entry_invalid"
        paths.append(path)
    if len(paths) != len(set(paths)):
        return False, "candidate_output_bundle_duplicate_path"
    if checkpoint.get("status") in {"ready", "no-change"} and tuple(paths) != PROCESSOR_OUTPUT_PATHS:
        return False, "candidate_output_bundle_paths_incomplete"
    bundle_digest = locator.get("bundle_manifest_sha256")
    if not isinstance(bundle_digest, str) or not DIGEST.fullmatch(bundle_digest):
        return False, "candidate_output_digest_missing"
    if bundle_digest != sha256_bytes(canonical_json(rows)):
        return False, "candidate_output_bundle_digest_mismatch"
    return True, ""


def read_health_state(history_path: pathlib.Path | None) -> dict[str, Any] | None:
    if history_path is None or not history_path.exists():
        return None
    value = load_json(history_path)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "datapan.upstream-catalogue-health-state.v1"
        or not verify_sealed(value, "state_sha256")
    ):
        raise ValueError("health_state_corrupt")
    try:
        validate_schema(value, HEALTH_STATE_SCHEMA, "health_state")
    except Exception as exc:  # Invalid durable state must become a receipt fault, not abort health evaluation.
        raise ValueError("health_state_corrupt") from exc
    return value


def evaluate(
    *, as_of: dt.datetime, repository: str, health_policy: dict[str, Any], source_policy: dict[str, Any],
    workflow_runs: list[dict[str, Any]], artifacts_by_run: dict[str, list[dict[str, Any]]],
    artifact_by_id: dict[str, dict[str, Any]], processor_state_dir: pathlib.Path,
    promotion_ack: dict[str, Any] | None, main_revision: str, manifest_sha256: str, registry_path: pathlib.Path,
    last_good: dict[str, Any] | None, mode: str, workflow_run_id: str, workflow_run_attempt: int,
    source_policy_sha256: str, health_policy_sha256: str, workflow_api_error: str | None = None,
    workflow_ids_by_path: dict[str, int] | None = None,
    processor_workflow_runs: list[dict[str, Any]] | None = None,
    processor_previous_attempts: dict[str, Any] | None = None,
    processor_previous_attempt_errors: set[str] | None = None,
    processor_workflow_api_error: str | None = None,
    prior_processor_execution_faults: list[dict[str, Any]] | None = None,
    collector_execution_attempts: dict[str, Any] | None = None,
    collector_execution_attempt_errors: set[str] | None = None,
    prior_collector_execution_faults: list[dict[str, Any]] | None = None,
    promotion_workflow_runs: list[dict[str, Any]] | None = None,
    promotion_workflow_previous_attempts: dict[str, Any] | None = None,
    promotion_workflow_previous_attempt_errors: set[str] | None = None,
    promotion_workflow_attempt_evidence: dict[str, Any] | None = None,
    promotion_workflow_attempt_errors: set[str] | None = None,
    promotion_workflow_api_error: str | None = None,
    prior_promotion_execution_faults: list[dict[str, Any]] | None = None,
    producer_runs_by_id: dict[str, dict[str, Any]] | None = None,
    promotion_runs_by_id: dict[str, dict[str, Any]] | None = None,
    recovery_publisher_runs_by_id: dict[str, dict[str, Any]] | None = None,
    promotion_workflow_paths: dict[str, str] | None = None,
    promotion_ack_error: str | None = None,
    health_state_error: str | None = None,
    processor_candidate_screen: Any | None = None,
    evaluator_source_sha: str | None = None,
    promotion_terminal_collection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mode not in {"live", "fixture"}:
        raise ValueError("invalid_execution_mode")
    if (
        isinstance(promotion_ack, dict)
        and promotion_ack.get("schema_version") == "datapan.canonical-update-promotion-journal.v1"
        and promotion_ack_error is None
    ):
        try:
            PROMOTION.validate_revision_links(promotion_ack)
        except Exception as exc:  # Invalid lineage must never hide an active receipt from Health.
            promotion_ack_error = f"promotion_journal_revision_links_invalid:{exc}"
            promotion_ack = None
    future_skew = int(health_policy["clock"]["maximum_future_skew_seconds"])
    if not REVISION.fullmatch(main_revision):
        raise ValueError("invalid_main_revision")
    if not DIGEST.fullmatch(manifest_sha256):
        raise ValueError("invalid_manifest_digest")
    source_rows = {row["source_id"]: row for row in source_policy.get("sources", []) if isinstance(row, dict) and isinstance(row.get("source_id"), str)}
    monitor_rows = health_policy.get("sources", [])
    if not isinstance(monitor_rows, list) or not monitor_rows:
        raise ValueError("health_policy_sources_missing")
    if len({row.get("source_id") for row in monitor_rows if isinstance(row, dict)}) != len(monitor_rows):
        raise ValueError("health_policy_source_id_duplicate")
    declared = set(source_rows)
    monitored = {row["source_id"] for row in monitor_rows}
    if declared != monitored:
        raise ValueError(f"health_policy_source_binding_mismatch:refresh={sorted(declared)}:health={sorted(monitored)}")
    for source in monitor_rows:
        refresh_source = source_rows[source["source_id"]]
        cadence = refresh_source.get("cadence", {})
        cron = cadence.get("cron") if isinstance(cadence, dict) else None
        if cadence.get("timezone") != "UTC" or not isinstance(cron, str):
            raise ValueError(f"source_cadence_invalid:{source['source_id']}")
        try:
            computed_interval = cadence_interval_seconds(cron)
        except ValueError as exc:
            raise ValueError(f"source_cadence_unsupported:{source['source_id']}") from exc
        if int(source["expected_interval_seconds"]) != computed_interval:
            raise ValueError(f"source_cadence_interval_mismatch:{source['source_id']}")
        if int(source["max_observation_age_seconds"]) != int(source["expected_interval_seconds"]) + int(source["observation_grace_seconds"]):
            raise ValueError(f"source_observation_ttl_mismatch:{source['source_id']}")
    main_registry_identity = manifest_registry_identity(ROOT / "manifest.json", registry_path)
    main_identity = {
        "revision": main_revision,
        "manifest_sha256": manifest_sha256,
        **main_registry_identity,
    }
    policy_sha = source_policy_sha256
    sources = [
        evaluate_source(
            source=source,
            repository=repository,
            refresh_source={**source_rows[source["source_id"]], "workflow_path": health_policy["health_workflow"]["collector_workflow_path"]},
            policy_sha256=policy_sha,
            as_of=as_of,
            workflow_runs=workflow_runs,
            artifacts_by_run=artifacts_by_run,
            state_dir=processor_state_dir,
            artifact_by_id=artifact_by_id,
            promotion_ack=promotion_ack,
            main_identity=main_identity,
            last_good=last_good,
            mode=mode,
            maximum_future_skew=future_skew,
            max_checkpoint_bytes=int(health_policy["processor_state"]["max_bytes_per_file"]),
            workflow_api_error=workflow_api_error,
            processor_workflow_runs=processor_workflow_runs or [],
            processor_previous_attempts=processor_previous_attempts or {},
            processor_previous_attempt_errors=processor_previous_attempt_errors or set(),
            processor_workflow_api_error=processor_workflow_api_error,
            processor_workflow_path=health_policy["processor_state"]["workflow_path"],
            processor_workflow_id=(workflow_ids_by_path or {}).get(health_policy["processor_state"]["workflow_path"]),
            processor_workflow_events=set(health_policy["processor_state"]["allowed_events"]),
            prior_processor_execution_faults=prior_processor_execution_faults or [],
            collector_execution_attempts=collector_execution_attempts or {},
            collector_execution_attempt_errors=collector_execution_attempt_errors or set(),
            prior_collector_execution_faults=prior_collector_execution_faults or [],
            promotion_workflow_runs=promotion_workflow_runs or [],
            promotion_workflow_previous_attempts=promotion_workflow_previous_attempts or {},
            promotion_workflow_previous_attempt_errors=promotion_workflow_previous_attempt_errors or set(),
            promotion_workflow_attempt_evidence=promotion_workflow_attempt_evidence or {},
            promotion_workflow_attempt_errors=promotion_workflow_attempt_errors or set(),
            promotion_workflow_api_error=promotion_workflow_api_error,
            prior_promotion_execution_faults=prior_promotion_execution_faults or [],
            producer_runs_by_id=producer_runs_by_id,
            promotion_runs_by_id=promotion_runs_by_id,
            recovery_publisher_runs_by_id=recovery_publisher_runs_by_id,
            promotion_workflow_paths=promotion_workflow_paths,
            workflow_ids_by_path=workflow_ids_by_path,
            promotion_ack_error=promotion_ack_error,
            health_state_error=health_state_error,
            processor_candidate_screen=processor_candidate_screen,
            evaluator_source_sha=evaluator_source_sha if mode == "live" else None,
            promotion_terminal_collection=promotion_terminal_collection if mode == "live" else None,
        )
        for source in monitor_rows
    ]
    all_faults = [entry for row in sources for entry in row["faults"]]
    receipt = {
        "schema_version": "datapan.upstream-catalogue-health.v1",
        "repository": repository,
        "evaluated_at": utc_timestamp(as_of),
        "execution_mode": mode,
        "health_workflow": {
            "run_id": workflow_run_id,
            "run_attempt": workflow_run_attempt,
            "revision": main_revision,
        },
        "policy_sha256": health_policy_sha256,
        "source_policy_sha256": policy_sha,
        "sources": sources,
        "faults": all_faults,
        "summary": {
            "source_count": len(sources),
            "healthy": sum(row["overall"] == "healthy" for row in sources),
            "degraded": sum(row["overall"] == "degraded" for row in sources),
            "blocked": sum(row["overall"] == "blocked" for row in sources),
            "fixture": sum(row["overall"] == "fixture" for row in sources),
            "fault_count": len(all_faults),
            "live_fresh_observation_count": 0 if mode == "fixture" else sum(row["observation"]["state"] == "fresh" and row["observation"]["execution_mode"] == "live" for row in sources),
            "last_good_count": sum(row["canonical"]["last_good"] is not None for row in sources),
        },
    }
    return seal_receipt(receipt)


def validate_schema(value: Any, schema_path: pathlib.Path, label: str) -> None:
    if not schema_path.exists():
        raise ValueError(f"schema_missing:{label}")
    validate_schema_value(value, load_json(schema_path), label)


def validate_schema_value(value: Any, schema: dict[str, Any], label: str) -> None:
    try:
        import jsonschema
    except ImportError as exc:
        raise ValueError("missing_dependency:jsonschema") from exc
    jsonschema.Draft202012Validator(schema).validate(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, help="Explicit UTC evaluation time (RFC 3339).")
    parser.add_argument("--repository", default="StatPan/datapan-registry")
    parser.add_argument("--health-policy", type=pathlib.Path, default=HEALTH_POLICY_DEFAULT)
    parser.add_argument("--source-policy", type=pathlib.Path, default=SOURCE_POLICY_DEFAULT)
    parser.add_argument("--processor-state-dir", type=pathlib.Path, default=pathlib.Path(".datapan/upstream-catalogue-state"))
    parser.add_argument("--promotion-ack", type=pathlib.Path, default=pathlib.Path(".datapan/canonical-update-state/reports/canonical-update-promotion-receipt.json"))
    parser.add_argument("--health-state", type=pathlib.Path)
    parser.add_argument("--registry", type=pathlib.Path, default=pathlib.Path("data/data-go-kr.registry.json"))
    parser.add_argument("--main-revision", default="")
    parser.add_argument("--evaluator-source-sha", default="", help="Pinned workflow source SHA that loaded this Health evaluator")
    parser.add_argument("--workflow-run-id", default="local")
    parser.add_argument("--workflow-run-attempt", type=int, default=1)
    parser.add_argument("--fixture-input", type=pathlib.Path, help="Explicit local-only inputs; never persisted as live health state.")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        as_of = parse_time(args.as_of, "as_of")
        health_policy = load_json(args.health_policy)
        source_policy = load_json(args.source_policy)
        validate_schema(health_policy, ROOT / "schemas/datapan.upstream-catalogue-health-policy.v1.schema.json", "health_policy")
        validate_schema(source_policy, ROOT / "schemas/datapan.source-refresh-policy.v1.schema.json", "source_policy")
        workflow_api_error = None
        processor_workflow_api_error = None
        promotion_workflow_api_error = None
        health_state_error = None
        promotion_ack_error = None
        workflow_ids_by_path: dict[str, int] = {}
        recovery_publisher_runs_by_id: dict[str, dict[str, Any]] = {}
        processor_candidate_screen = None
        evaluator_source_sha: str | None = None
        promotion_terminal_collection: dict[str, Any] | None = None
        collector_execution_attempts: dict[str, Any] = {}
        collector_execution_attempt_errors: set[str] = set()
        prior_collector_execution_faults: list[dict[str, Any]] = []
        if args.fixture_input:
            mode = "fixture"
            fixture = load_json(args.fixture_input)
            workflow_runs = fixture.get("workflow_runs", []) if isinstance(fixture, dict) else []
            collector_execution_attempts = fixture.get("collector_execution_attempts", {}) if isinstance(fixture, dict) else {}
            raw_collector_errors = fixture.get("collector_execution_attempt_errors", []) if isinstance(fixture, dict) else []
            collector_execution_attempt_errors = {
                row for row in raw_collector_errors if isinstance(row, str)
            } if isinstance(raw_collector_errors, list) else set()
            prior_collector_execution_faults = fixture.get("prior_collector_execution_faults", []) if isinstance(fixture, dict) else []
            processor_workflow_runs = fixture.get("processor_workflow_runs", []) if isinstance(fixture, dict) else []
            processor_previous_attempts = fixture.get("processor_previous_attempts", {}) if isinstance(fixture, dict) else {}
            processor_previous_attempt_errors = fixture.get("processor_previous_attempt_errors", []) if isinstance(fixture, dict) else []
            processor_previous_attempt_errors = {
                row for row in processor_previous_attempt_errors if isinstance(row, str)
            } if isinstance(processor_previous_attempt_errors, list) else set()
            processor_workflow_api_error = fixture.get("processor_workflow_api_error") if isinstance(fixture, dict) else None
            prior_processor_execution_faults = fixture.get("prior_processor_execution_faults", []) if isinstance(fixture, dict) else []
            promotion_workflow_runs = fixture.get("promotion_workflow_runs", []) if isinstance(fixture, dict) else []
            promotion_workflow_previous_attempts = fixture.get("promotion_workflow_previous_attempts", {}) if isinstance(fixture, dict) else {}
            promotion_workflow_previous_attempt_errors = fixture.get("promotion_workflow_previous_attempt_errors", []) if isinstance(fixture, dict) else []
            promotion_workflow_previous_attempt_errors = {
                row for row in promotion_workflow_previous_attempt_errors if isinstance(row, str)
            } if isinstance(promotion_workflow_previous_attempt_errors, list) else set()
            promotion_workflow_attempt_evidence = fixture.get("promotion_workflow_attempt_evidence", {}) if isinstance(fixture, dict) else {}
            promotion_workflow_attempt_errors = fixture.get("promotion_workflow_attempt_errors", []) if isinstance(fixture, dict) else []
            promotion_workflow_attempt_errors = {
                row for row in promotion_workflow_attempt_errors if isinstance(row, str)
            } if isinstance(promotion_workflow_attempt_errors, list) else set()
            promotion_workflow_api_error = fixture.get("promotion_workflow_api_error") if isinstance(fixture, dict) else None
            prior_promotion_execution_faults = fixture.get("prior_promotion_execution_faults", []) if isinstance(fixture, dict) else []
            artifacts_by_run = fixture.get("artifacts_by_run", {}) if isinstance(fixture, dict) else {}
            artifact_by_id = fixture.get("artifacts_by_id", {}) if isinstance(fixture, dict) else {}
            producer_runs_by_id = fixture.get("producer_runs_by_id", {}) if isinstance(fixture, dict) else {}
            promotion_ack = fixture.get("promotion_ack") if isinstance(fixture, dict) else None
            promotion_runs_by_id = fixture.get("promotion_runs_by_id", {}) if isinstance(fixture, dict) else {}
            recovery_publisher_runs_by_id = fixture.get("recovery_publisher_runs_by_id", {}) if isinstance(fixture, dict) else {}
            workflow_ids_by_path = fixture.get("workflow_ids_by_path", {}) if isinstance(fixture, dict) else {}
            main_revision = args.main_revision or (fixture.get("main_revision", "") if isinstance(fixture, dict) else "")
            last_good = fixture.get("last_good") if isinstance(fixture, dict) else None
        else:
            mode = "live"
            workflow = health_policy["health_workflow"]
            try:
                workflow_ids_by_path[workflow["collector_workflow_path"]] = collect_workflow_identity(
                    args.repository, workflow["collector_workflow_path"],
                )
                workflow_runs = collect_workflow_runs(args.repository, workflow["collector_workflow_path"], int(health_policy["processor_state"]["workflow_run_limit"]))
            except RuntimeError as exc:
                workflow_runs = []
                workflow_api_error = str(exc)
            if workflow_api_error is None:
                collector_execution_attempts, collector_execution_attempt_errors = collect_collector_execution_attempts(
                    args.repository,
                    workflow_runs,
                    workflow["collector_workflow_path"],
                    workflow_ids_by_path.get(workflow["collector_workflow_path"]),
                    {"schedule", "workflow_dispatch"},
                    as_of,
                    int(health_policy["clock"]["maximum_future_skew_seconds"]),
                )
            processor_policy = health_policy["processor_state"]
            processor_workflow_path = processor_policy["workflow_path"]
            processor_workflow_events = set(processor_policy["allowed_events"])
            try:
                workflow_ids_by_path[processor_workflow_path] = collect_workflow_identity(
                    args.repository, processor_workflow_path,
                )
                processor_workflow_runs = collect_workflow_runs(
                    args.repository, processor_workflow_path, int(processor_policy["workflow_run_limit"]),
                )
            except RuntimeError as exc:
                processor_workflow_runs = []
                processor_workflow_api_error = str(exc)
            processor_previous_attempts = {}
            processor_previous_attempt_errors: set[str] = set()
            if processor_workflow_api_error is None:
                processor_previous_attempts, processor_previous_attempt_errors = collect_processor_prior_attempt_history(
                    args.repository, processor_workflow_runs, processor_workflow_path,
                    workflow_ids_by_path.get(processor_workflow_path), processor_workflow_events,
                )
            promotion_workflow_path = health_policy["promotion_state"]["promotion_workflow_path"]
            promotion_workflow_runs = []
            promotion_workflow_previous_attempts = {}
            promotion_workflow_previous_attempt_errors: set[str] = set()
            promotion_workflow_attempt_evidence = {}
            promotion_workflow_attempt_errors: set[str] = set()
            try:
                workflow_ids_by_path[promotion_workflow_path] = collect_workflow_identity(
                    args.repository, promotion_workflow_path,
                )
                promotion_workflow_runs = collect_workflow_runs(
                    args.repository, promotion_workflow_path, int(processor_policy["workflow_run_limit"]),
                )
            except RuntimeError as exc:
                promotion_workflow_api_error = str(exc)
            if promotion_workflow_api_error is None:
                promotion_workflow_runs, _, promotion_workflow_previous_attempt_errors = trusted_promotion_execution_inputs(
                    promotion_workflow_runs, {}, set(), args.repository, promotion_workflow_path,
                    workflow_ids_by_path.get(promotion_workflow_path), PROMOTION_WORKFLOW_EVENTS,
                    as_of, int(health_policy["clock"]["maximum_future_skew_seconds"]),
                )
                promotion_workflow_previous_attempts, looked_up_errors = collect_processor_prior_attempt_history(
                    args.repository, promotion_workflow_runs, promotion_workflow_path,
                    workflow_ids_by_path.get(promotion_workflow_path), PROMOTION_WORKFLOW_EVENTS,
                )
                promotion_workflow_previous_attempt_errors |= looked_up_errors
                promotion_workflow_runs, promotion_workflow_previous_attempts, promotion_workflow_previous_attempt_errors = trusted_promotion_execution_inputs(
                    promotion_workflow_runs, promotion_workflow_previous_attempts, promotion_workflow_previous_attempt_errors,
                    args.repository, promotion_workflow_path, workflow_ids_by_path.get(promotion_workflow_path),
                    PROMOTION_WORKFLOW_EVENTS, as_of, int(health_policy["clock"]["maximum_future_skew_seconds"]),
                )
                promotion_workflow_attempt_evidence, promotion_workflow_attempt_errors = collect_promotion_execution_attempts(
                    args.repository, promotion_workflow_path, workflow_ids_by_path.get(promotion_workflow_path),
                    promotion_workflow_runs, promotion_workflow_previous_attempts,
                    promotion_workflow_previous_attempt_errors, as_of,
                    int(health_policy["clock"]["maximum_future_skew_seconds"]),
                )
            scheduled = sorted((
                row for row in workflow_runs
                if trusted_main_workflow_run(
                    row, args.repository, workflow["collector_workflow_path"],
                    workflow_ids_by_path.get(workflow["collector_workflow_path"]), {"schedule"},
                )
            ), key=workflow_run_order)
            artifacts_by_run = {}
            artifact_by_id = {}
            producer_runs_by_id = {}
            promotion_runs_by_id = {}
            promotion_ack = load_json(args.promotion_ack) if args.promotion_ack and args.promotion_ack.exists() else None
            if promotion_ack is not None:
                try:
                    if promotion_ack.get("schema_version") == "datapan.canonical-update-promotion-journal.v1":
                        validate_schema(promotion_ack, PROMOTION_JOURNAL_SCHEMA, "promotion_journal")
                        if str(promotion_ack.get("repository", "")).casefold() != args.repository.casefold():
                            raise ValueError("promotion_journal_repository_mismatch")
                        records = promotion_ack.get("records")
                        if not isinstance(records, list):
                            raise ValueError("promotion_journal_records_invalid")
                        record_keys: set[tuple[str, str, str, str, str, str]] = set()
                        for record in records:
                            validate_health_promotion_record(record)
                            candidate = record.get("candidate") if isinstance(record, dict) else None
                            if not isinstance(candidate, dict) or str(candidate.get("repository", "")).casefold() != args.repository.casefold():
                                raise ValueError("promotion_record_repository_mismatch")
                            key = PROMOTION.candidate_key(record)
                            if key in record_keys:
                                raise ValueError("promotion_journal_duplicate_candidate")
                            record_keys.add(key)
                    else:
                        validate_health_promotion_record(promotion_ack)
                        candidate = promotion_ack.get("candidate") if isinstance(promotion_ack, dict) else None
                        if not isinstance(candidate, dict) or str(candidate.get("repository", "")).casefold() != args.repository.casefold():
                            raise ValueError("promotion_receipt_repository_mismatch")
                except Exception as exc:  # Schema validation errors must become a health fault.
                    promotion_ack_error = str(exc)
                    promotion_ack = None
            main_revision = checked_out_main_revision(ROOT, args.main_revision)
            registry_path = args.registry if args.registry.is_absolute() else ROOT / args.registry
            verify_main_manifest_binding(ROOT, main_revision, ROOT / "manifest.json", registry_path)
            evaluator_source_sha = verified_health_evaluator_source(ROOT, args.evaluator_source_sha)
            if evaluator_source_sha is not None:
                processor_candidate_screen = build_processor_candidate_screen(ROOT, args.repository, main_revision)
                try:
                    promotion_terminal_collection = collect_terminal_outcome_records(
                        root=ROOT,
                        repository=args.repository,
                        workflow_id=workflow_ids_by_path.get(promotion_workflow_path),
                        runs=promotion_workflow_runs,
                        previous_attempts=promotion_workflow_previous_attempts,
                        previous_attempt_errors=promotion_workflow_previous_attempt_errors,
                        attempt_evidence=promotion_workflow_attempt_evidence,
                        current_main_sha=main_revision,
                        as_of=as_of,
                        maximum_future_skew=int(health_policy["clock"]["maximum_future_skew_seconds"]),
                    )
                except Exception:
                    promotion_terminal_collection = {
                        "status": "unavailable", "reason_code": "terminal_intake_unavailable",
                        "attempts_considered": 0, "records": [],
                    }
            else:
                processor_candidate_screen = lambda _checkpoint: _candidate_screen_result(
                    status="unavailable", stage="trusted_source",
                    reason_code="health_evaluator_source_unavailable",
                )
            referenced_artifact_ids: set[str] = set()
            observed_run_ids: set[str] = set()
            for source in health_policy["sources"]:
                checkpoints, _ = load_processor_checkpoints(args.processor_state_dir, source["source_id"], int(health_policy["processor_state"]["max_bytes_per_file"]))
                for checkpoint in checkpoints:
                    observation = checkpoint.get("last_observation")
                    if isinstance(observation, dict) and str(observation.get("producer_run_id") or "").isdigit():
                        observed_run_ids.add(str(observation["producer_run_id"]))
                    for collection in (checkpoint.get("input_artifacts", []), [checkpoint.get("output_artifact", {})]):
                        for ref in collection:
                            if not isinstance(ref, dict) or not ref.get("artifact_id"):
                                continue
                            referenced_artifact_ids.add(str(ref["artifact_id"]))
            run_ids_to_inspect = {str(scheduled[-1].get("id", ""))} if scheduled else set()
            latest_collector = sorted(
                (
                    row for row in workflow_runs
                    if trusted_main_workflow_run(
                        row, args.repository, workflow["collector_workflow_path"],
                        workflow_ids_by_path.get(workflow["collector_workflow_path"]),
                        {"schedule", "workflow_dispatch"},
                    )
                ),
                key=workflow_run_order,
            )
            if latest_collector:
                run_ids_to_inspect.add(str(latest_collector[-1].get("id", "")))
            run_ids_to_inspect |= observed_run_ids
            for run_id in sorted(run_ids_to_inspect):
                if not run_id.isdigit():
                    continue
                try:
                    run_info = collect_run(args.repository, run_id)
                    if run_id in observed_run_ids:
                        producer_runs_by_id[run_id] = run_info
                except RuntimeError:
                    run_info = None
                try:
                    artifacts_by_run[run_id] = collect_run_artifacts(args.repository, run_id)
                except RuntimeError:
                    artifacts_by_run[run_id] = {"availability_error": True}
                if run_info is not None:
                    for row in artifacts_by_run.get(run_id, []) if isinstance(artifacts_by_run.get(run_id), list) else []:
                        if isinstance(row, dict) and str(row.get("id", "")).isdigit():
                            referenced_artifact_ids.add(str(row["id"]))
            source_ids = {str(row.get("source_id")) for row in health_policy.get("sources", []) if isinstance(row, dict)}
            promotion_references = promotion_run_references(promotion_ack, source_ids)
            if len(promotion_references) > 500:
                promotion_ack_error = "promotion_run_reference_limit_exceeded"
                promotion_references = set(sorted(promotion_references)[-500:])
            if promotion_references:
                for workflow_key in ("promotion_workflow_path", "publication_ack_workflow_path"):
                    workflow_path = health_policy.get("promotion_state", {}).get(workflow_key)
                    if not isinstance(workflow_path, str) or not workflow_path:
                        promotion_ack_error = promotion_ack_error or "promotion_workflow_path_missing"
                        continue
                    try:
                        workflow_ids_by_path[workflow_path] = collect_workflow_identity(args.repository, workflow_path)
                    except RuntimeError as exc:
                        promotion_ack_error = promotion_ack_error or str(exc)
            for promotion_run_id, promotion_attempt in sorted(promotion_references):
                key = f"{promotion_run_id}/{promotion_attempt}"
                try:
                    promotion_runs_by_id[key] = collect_run_attempt_evidence(args.repository, promotion_run_id, promotion_attempt)
                except RuntimeError:
                    promotion_runs_by_id[key] = {"availability_error": True}
            try:
                recovery_references = publication_recovery_references(promotion_ack, source_ids, args.repository)
            except ValueError:
                recovery_references = {}
            if recovery_references:
                try:
                    publisher_workflow_id = collect_workflow_identity(args.repository, PUBLICATION_WORKFLOW_PATH)
                    workflow_ids_by_path[PUBLICATION_WORKFLOW_PATH] = publisher_workflow_id
                except RuntimeError:
                    publisher_workflow_id = None
                worst_case_requests = 1 + len(recovery_references) * (1 + MAX_PROMOTION_JOB_PAGES)
                if worst_case_requests <= MAX_PUBLICATION_RECOVERY_API_REQUESTS:
                    for identity, reference in sorted(recovery_references.items()):
                        if publisher_workflow_id is None or reference.get("publisher_workflow_id") != publisher_workflow_id:
                            recovery_publisher_runs_by_id[identity] = {"availability_error": True}
                            continue
                        try:
                            recovery_publisher_runs_by_id[identity] = collect_run_attempt_evidence(
                                args.repository,
                                str(reference["publisher_run_id"]),
                                int(reference["publisher_run_attempt"]),
                            )
                        except RuntimeError:
                            recovery_publisher_runs_by_id[identity] = {"availability_error": True}
                else:
                    recovery_publisher_runs_by_id = {
                        identity: {"availability_error": True} for identity in recovery_references
                    }
            for artifact_id in sorted(referenced_artifact_ids):
                try:
                    metadata = collect_artifact(args.repository, artifact_id)
                except RuntimeError:
                    metadata = {"availability_error": True}
                if metadata is not None:
                    artifact_by_id[artifact_id] = metadata
            prior_processor_execution_faults = []
            prior_collector_execution_faults = []
            prior_promotion_execution_faults = []
            try:
                health_state = read_health_state(args.health_state) if args.health_state else None
                last_good = health_state.get("last_good_by_source") if isinstance(health_state, dict) else None
                state_faults = health_state.get("faults", []) if isinstance(health_state, dict) else []
                if isinstance(state_faults, list):
                    prior_processor_execution_faults = [
                        row for row in state_faults
                        if isinstance(row, dict)
                        and row.get("stage") == "processor-execution"
                        and row.get("reason") == "processor_run_failed"
                        and row.get("status") in {"open", "recovery_pending_verification"}
                    ]
                    prior_collector_execution_faults = [
                        row for row in state_faults
                        if isinstance(row, dict)
                        and row.get("stage") == "collector-execution"
                        and row.get("reason") == "repeated_collector_execution_failures"
                        and row.get("status") in {"open", "recovery_pending_verification"}
                    ]
                    prior_promotion_execution_faults = [
                        row for row in state_faults
                        if isinstance(row, dict)
                        and row.get("stage") == "promotion-execution"
                        and row.get("reason") == "promotion_workflow_run_failed"
                        and row.get("status") in {"open", "recovery_pending_verification"}
                    ]
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                last_good = None
                prior_processor_execution_faults = []
                prior_collector_execution_faults = []
                prior_promotion_execution_faults = []
                health_state_error = str(exc)
        if mode == "fixture":
            workflow_runs = workflow_runs if isinstance(workflow_runs, list) else []
            collector_execution_attempts = collector_execution_attempts if isinstance(collector_execution_attempts, dict) else {}
            collector_execution_attempt_errors = collector_execution_attempt_errors if isinstance(collector_execution_attempt_errors, set) else set()
            prior_collector_execution_faults = prior_collector_execution_faults if isinstance(prior_collector_execution_faults, list) else []
            processor_workflow_runs = processor_workflow_runs if isinstance(processor_workflow_runs, list) else []
            processor_previous_attempts = processor_previous_attempts if isinstance(processor_previous_attempts, dict) else {}
            prior_processor_execution_faults = prior_processor_execution_faults if isinstance(prior_processor_execution_faults, list) else []
            promotion_workflow_runs = promotion_workflow_runs if isinstance(promotion_workflow_runs, list) else []
            promotion_workflow_previous_attempts = promotion_workflow_previous_attempts if isinstance(promotion_workflow_previous_attempts, dict) else {}
            promotion_workflow_previous_attempt_errors = promotion_workflow_previous_attempt_errors if isinstance(promotion_workflow_previous_attempt_errors, set) else set()
            promotion_workflow_attempt_evidence = promotion_workflow_attempt_evidence if isinstance(promotion_workflow_attempt_evidence, dict) else {}
            promotion_workflow_attempt_errors = promotion_workflow_attempt_errors if isinstance(promotion_workflow_attempt_errors, set) else set()
            prior_promotion_execution_faults = prior_promotion_execution_faults if isinstance(prior_promotion_execution_faults, list) else []
            artifacts_by_run = artifacts_by_run if isinstance(artifacts_by_run, dict) else {}
            artifact_by_id = artifact_by_id if isinstance(artifact_by_id, dict) else {}
            producer_runs_by_id = producer_runs_by_id if isinstance(producer_runs_by_id, dict) else {}
            promotion_runs_by_id = promotion_runs_by_id if isinstance(promotion_runs_by_id, dict) else {}
            workflow_ids_by_path = workflow_ids_by_path if isinstance(workflow_ids_by_path, dict) else {}
        receipt = evaluate(
            as_of=as_of,
            repository=args.repository,
            health_policy=health_policy,
            source_policy=source_policy,
            workflow_runs=workflow_runs,
            artifacts_by_run=artifacts_by_run,
            artifact_by_id=artifact_by_id,
            processor_state_dir=args.processor_state_dir,
            promotion_ack=promotion_ack if isinstance(promotion_ack, dict) else None,
            main_revision=main_revision,
            manifest_sha256=file_sha256(ROOT / "manifest.json"),
            registry_path=(registry_path if mode == "live" else args.registry),
            last_good=last_good if isinstance(last_good, dict) else None,
            mode=mode,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            source_policy_sha256=file_sha256(args.source_policy),
            health_policy_sha256=file_sha256(args.health_policy),
            workflow_api_error=workflow_api_error,
            producer_runs_by_id=producer_runs_by_id,
            processor_workflow_runs=processor_workflow_runs,
            processor_previous_attempts=processor_previous_attempts,
            processor_previous_attempt_errors=processor_previous_attempt_errors,
            processor_workflow_api_error=processor_workflow_api_error,
            prior_processor_execution_faults=prior_processor_execution_faults,
            collector_execution_attempts=collector_execution_attempts,
            collector_execution_attempt_errors=collector_execution_attempt_errors,
            prior_collector_execution_faults=prior_collector_execution_faults,
            promotion_workflow_runs=promotion_workflow_runs,
            promotion_workflow_previous_attempts=promotion_workflow_previous_attempts,
            promotion_workflow_previous_attempt_errors=promotion_workflow_previous_attempt_errors,
            promotion_workflow_attempt_evidence=promotion_workflow_attempt_evidence,
            promotion_workflow_attempt_errors=promotion_workflow_attempt_errors,
            promotion_workflow_api_error=promotion_workflow_api_error,
            prior_promotion_execution_faults=prior_promotion_execution_faults,
            promotion_runs_by_id=promotion_runs_by_id,
            promotion_workflow_paths=health_policy.get("promotion_state", {}),
            workflow_ids_by_path=workflow_ids_by_path,
            promotion_ack_error=promotion_ack_error,
            health_state_error=health_state_error,
            processor_candidate_screen=processor_candidate_screen,
            recovery_publisher_runs_by_id=recovery_publisher_runs_by_id,
            evaluator_source_sha=evaluator_source_sha,
            promotion_terminal_collection=promotion_terminal_collection,
        )
        validate_schema(receipt, HEALTH_RECEIPT_SCHEMA, "health_receipt")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": "ok", "execution_mode": mode, "fault_count": receipt["summary"]["fault_count"], "receipt_sha256": receipt["receipt_sha256"]}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL upstream catalogue health: {type(exc).__name__}:{str(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
