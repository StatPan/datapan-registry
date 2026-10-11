#!/usr/bin/env python3
"""Require a valid scheduled stale-queue receipt after ordinary C work."""

from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import sys
from typing import Any, Mapping


REPOSITORY = "StatPan/datapan-registry"
WORKFLOW_PATH = ".github/workflows/canonical-update-promotion.yml"
RECEIPT_SCHEMA_PATH = "schemas/datapan.canonical-stale-c-queue-recovery-receipt.v1.schema.json"
RECEIPT_ROOT = pathlib.Path(".datapan/ci/canonical-stale-c-queue-recovery")
MAX_RECEIPT_BYTES = 256 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
SHA1_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
POSITIVE_DECIMAL_RE = re.compile(r"^[1-9][0-9]{0,19}$")


class GateError(RuntimeError):
    """The scheduled C recovery receipt or step results are not trustworthy."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateError("receipt_json_duplicate_key")
        result[key] = value
    return result


def _positive_env_integer(env: Mapping[str, str], name: str) -> int:
    value = env.get(name, "")
    if not isinstance(value, str) or not POSITIVE_DECIMAL_RE.fullmatch(value):
        raise GateError(f"environment_{name.lower()}_invalid")
    return int(value)


def _receipt_path(root: pathlib.Path, env: Mapping[str, str]) -> pathlib.Path:
    run_id = _positive_env_integer(env, "GITHUB_RUN_ID")
    attempt = _positive_env_integer(env, "GITHUB_RUN_ATTEMPT")
    return root / RECEIPT_ROOT / str(run_id) / str(attempt) / "receipt.json"


def _read_regular_file(path: pathlib.Path, *, maximum: int) -> bytes:
    current = path.parent
    while True:
        if current.is_symlink():
            raise GateError("receipt_path_component_not_directory")
        if not current.exists():
            raise GateError("receipt_file_unavailable")
        if not current.is_dir():
            raise GateError("receipt_path_component_not_directory")
        if current == current.parent:
            break
        if current == path.anchor:
            break
        current = current.parent
    if path.is_symlink():
        raise GateError("receipt_file_is_symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GateError("receipt_file_unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise GateError("receipt_file_type_or_size_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(maximum + 1)
    finally:
        os.close(descriptor)
    if len(raw) > maximum:
        raise GateError("receipt_file_type_or_size_invalid")
    return raw


def _load_receipt(root: pathlib.Path, env: Mapping[str, str]) -> dict[str, Any]:
    schema_path = root / RECEIPT_SCHEMA_PATH
    try:
        schema_raw = _read_regular_file(schema_path, maximum=MAX_SCHEMA_BYTES)
        schema = json.loads(schema_raw.decode("utf-8"), object_pairs_hook=_strict_object)
        receipt_raw = _read_regular_file(_receipt_path(root, env), maximum=MAX_RECEIPT_BYTES)
        receipt = json.loads(receipt_raw.decode("utf-8"), object_pairs_hook=_strict_object)
        if not isinstance(receipt, dict):
            raise GateError("receipt_root_not_object")
        import jsonschema

        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker(),
        )
        validator.check_schema(schema)
        validator.validate(receipt)
    except GateError:
        raise
    except Exception as exc:
        raise GateError("receipt_schema_or_json_invalid") from exc
    return receipt


def _validate_controller(receipt: Mapping[str, Any], env: Mapping[str, str]) -> None:
    controller = receipt.get("controller")
    if not isinstance(controller, Mapping):
        raise GateError("receipt_controller_missing")
    expected_run_id = _positive_env_integer(env, "GITHUB_RUN_ID")
    expected_attempt = _positive_env_integer(env, "GITHUB_RUN_ATTEMPT")
    expected_sha = env.get("GITHUB_SHA", "")
    if not SHA1_RE.fullmatch(expected_sha):
        raise GateError("environment_github_sha_invalid")
    expected_repo_id = env.get("GITHUB_REPOSITORY_ID")
    if expected_repo_id:
        expected_repo_id = str(_positive_env_integer(env, "GITHUB_REPOSITORY_ID"))
    actual_repo_id = controller.get("repository_id")
    if (
        receipt.get("repository") != REPOSITORY
        or receipt.get("workflow_path") != WORKFLOW_PATH
        or controller.get("run_id") != expected_run_id
        or controller.get("run_attempt") != expected_attempt
        or controller.get("event") != "schedule"
        or controller.get("branch") != "main"
        or controller.get("head_sha") != expected_sha
        or controller.get("job_name") != "reconcile"
        or isinstance(actual_repo_id, bool)
        or not isinstance(actual_repo_id, int)
        or actual_repo_id < 1
        or (expected_repo_id is not None and str(actual_repo_id) != expected_repo_id)
        or not isinstance(controller.get("workflow_id"), int)
        or isinstance(controller.get("workflow_id"), bool)
        or controller.get("workflow_id", 0) < 1
        or not isinstance(receipt.get("policy_sha256"), str)
        or not SHA256_RE.fullmatch(receipt["policy_sha256"])
        or not isinstance(receipt.get("controller_workflow_sha256"), str)
        or not SHA256_RE.fullmatch(receipt["controller_workflow_sha256"])
    ):
        raise GateError("receipt_controller_identity_or_proof_mismatch")


def _validate_success_outcome(receipt: Mapping[str, Any]) -> str:
    outcome = receipt.get("outcome")
    if outcome not in {"no_eligible_target", "native_cancel_confirmed"}:
        raise GateError("receipt_outcome_not_successful")
    if receipt.get("blocked_reason") is not None:
        raise GateError("receipt_blocked_reason_present")
    if outcome == "no_eligible_target":
        if not (
            receipt.get("target") is None
            and receipt.get("reservation_state") == "not_created"
            and receipt.get("request_disposition") == "not_attempted"
            and receipt.get("observations") == []
            and receipt.get("target_workflow_sha256") is None
        ):
            raise GateError("receipt_no_action_semantics_invalid")
        return outcome
    if outcome == "native_cancel_confirmed":
        target = receipt.get("target")
        observations = receipt.get("observations")
        if not isinstance(target, Mapping) or not isinstance(observations, list) or not observations:
            raise GateError("receipt_terminal_confirmation_missing")
        final = observations[-1]
        if not (
            receipt.get("reservation_state") == "confirmed"
            and target.get("run_attempt") == 1
            and target.get("event") == "schedule"
            and target.get("branch") == "main"
            and isinstance(receipt.get("target_workflow_sha256"), str)
            and SHA256_RE.fullmatch(receipt["target_workflow_sha256"])
            and final.get("run_id") == target.get("run_id")
            and final.get("run_attempt") == target.get("run_attempt")
            and final.get("head_sha") == target.get("head_sha")
            and final.get("status") == "completed"
            and final.get("conclusion") == "cancelled"
            and final.get("job_count") == 0
        ):
            raise GateError("receipt_terminal_confirmation_invalid")
        return outcome
    raise GateError("receipt_outcome_not_successful")


def check_final_gate(
    *,
    root: pathlib.Path,
    env: Mapping[str, str],
    recovery_step_outcome: str,
    artifact_step_outcome: str,
) -> str | None:
    """Return the successful scheduled outcome or fail closed; other events skip."""
    if env.get("GITHUB_EVENT_NAME") != "schedule":
        return None
    if recovery_step_outcome != "success":
        raise GateError("scheduled_recovery_step_failed")
    if artifact_step_outcome != "success":
        raise GateError("scheduled_receipt_artifact_step_failed")
    receipt = _load_receipt(root, env)
    _validate_controller(receipt, env)
    return _validate_success_outcome(receipt)


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    try:
        outcome = check_final_gate(
            root=root,
            env=env,
            recovery_step_outcome=env.get("QUEUE_RECOVERY_STEP_OUTCOME", ""),
            artifact_step_outcome=env.get("QUEUE_RECEIPT_ARTIFACT_STEP_OUTCOME", ""),
        )
    except GateError as exc:
        print(f"scheduled queue recovery gate failed: {exc}", file=sys.stderr)
        return 1
    if outcome is not None:
        print(f"scheduled queue recovery gate passed: {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
