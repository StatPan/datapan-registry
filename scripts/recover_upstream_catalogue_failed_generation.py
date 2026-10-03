"""Plan a fenced, result-only recovery for the frozen failed catalogue run.

This module is deliberately pure. The workflow authenticates the Actions run,
artifact metadata, and archive download, then supplies only the two bounded
artifact members needed here. Persistence, upload, validation, and the state
branch compare-and-swap remain the caller's responsibility.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


CHECKPOINT_SCHEMA = "datapan.upstream-catalogue-checkpoint.v1"
RECOVERY_REASON = "recovered_failed_processor_scope_mismatch"
ORIGINAL_SCOPE_ERROR = "composer_scope_omits_worker_outcomes"

FAILED_REPOSITORY = "StatPan/datapan-registry"
FAILED_RUN_ID = 37091592758
FAILED_RUN_ATTEMPT = 1
FAILED_RUN_NAME = "Process upstream catalogue"
FAILED_RUN_PATH = ".github/workflows/upstream-catalogue-process.yml"
FAILED_RUN_HEAD_BRANCH = "main"
FAILED_RUN_HEAD_SHA = "446016d15d6c16b599bdaf89a97bc450b71205f4"

FAILED_ARTIFACT_ID = 11262805213
FAILED_ARTIFACT_NAME = "upstream-catalogue-processing-37091592758-1"
FAILED_ARTIFACT_SHA256 = "b3ef63c8dcfd22e04d3d54cac3763733d24883400d36a0eacfbbe834ad5496cf"
FAILED_ARTIFACT_SIZE = 17093081
FAILED_ARTIFACT_CREATED_AT = "2026-10-03T03:00:12Z"
FAILED_ARTIFACT_EXPIRES_AT = "2026-11-02T03:00:08Z"
FAILED_REPOSITORY_ID = 1278568329

GENERATION_ID = "48bd59cf7d2da0fc75fb6e9eb6dcee205be066bbfefa5d7f88cebdee3ddd5b50"
GENERATION_CHECKPOINT_SHA256 = "fa1bd6ed711c88290e43acf53c0d00b4b4fe931dc0ccef7b5b29cf74a3fe3bfb"
STATE_BRANCH_SHA = "2bc0e2a53e66f97a5b13912a80b8200931a46a49"
FAILED_CHECKPOINT_RECEIPT_SHA256 = "c67784de42c19b4fdd15979f1cb5ed8a8d88e158bd7e4e19cbf2745f8d2156be"
FAILED_CHECKPOINT_FILE_SHA256 = "9dfe32e4b767852d3407926c1b1a2511b396426cc38e0d99bb20547338dea038"
FAILED_RESULT_FILE_SHA256 = "214eddc35c05b7b66f1173ff6f751efc4f74393cd3022ff64338c649046f70cb"

MAX_CHECKPOINT_RECEIPT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 16 * 1024
_HEX_40 = re.compile(r"^[a-f0-9]{40}$")
_RUN_ATTEMPT = re.compile(r"^([0-9]+)-([1-9][0-9]*)$")


class RecoveryRejected(ValueError):
    """A recovery input did not match the frozen failed-run evidence."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class RecoveryPlan:
    """Unsealed current-run checkpoint and CAS base for the workflow caller."""

    checkpoint: dict[str, Any]
    expected_state_head_sha: str


def _reject(reason: str) -> None:
    raise RecoveryRejected(reason)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RecoveryRejected("recovery_checkpoint_not_canonical_json") from exc


def _checkpoint_digest(value: Mapping[str, Any]) -> str:
    unsigned = dict(value)
    unsigned.pop("checkpoint_sha256", None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _parse_time(value: Any, reason: str) -> datetime:
    if not isinstance(value, str):
        _reject(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryRejected(reason) from exc
    if parsed.tzinfo is None:
        _reject(reason)
    return parsed.astimezone(timezone.utc)


def _require_exact_fields(actual: Mapping[str, Any], expected: Mapping[str, Any], reason: str) -> None:
    for key, value in expected.items():
        if actual.get(key) != value:
            _reject(reason)


def _validate_run_metadata(value: Mapping[str, Any]) -> None:
    if type(value.get("id")) is not int or type(value.get("run_attempt")) is not int:
        _reject("recovery_failed_run_identity_mismatch")
    expected = {
        "repository": FAILED_REPOSITORY,
        "id": FAILED_RUN_ID,
        "name": FAILED_RUN_NAME,
        "path": FAILED_RUN_PATH,
        "head_branch": FAILED_RUN_HEAD_BRANCH,
        "head_sha": FAILED_RUN_HEAD_SHA,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": FAILED_RUN_ATTEMPT,
        "default_branch": "main",
    }
    _require_exact_fields(value, expected, "recovery_failed_run_identity_mismatch")


def _validate_artifact_metadata(value: Mapping[str, Any]) -> None:
    if (
        type(value.get("run_attempt")) is not int
        or type(value.get("id")) is not int
        or type(value.get("size_in_bytes")) is not int
        or type(value.get("expired")) is not bool
    ):
        _reject("recovery_failed_artifact_identity_mismatch")
    expected = {
        "repository": FAILED_REPOSITORY,
        "run_id": str(FAILED_RUN_ID),
        "run_attempt": FAILED_RUN_ATTEMPT,
        "id": FAILED_ARTIFACT_ID,
        "name": FAILED_ARTIFACT_NAME,
        "expired": False,
        "expires_at": FAILED_ARTIFACT_EXPIRES_AT,
        "created_at": FAILED_ARTIFACT_CREATED_AT,
        "size_in_bytes": FAILED_ARTIFACT_SIZE,
        "digest": f"sha256:{FAILED_ARTIFACT_SHA256}",
    }
    _require_exact_fields(value, expected, "recovery_failed_artifact_identity_mismatch")
    workflow_run = value.get("workflow_run")
    if not isinstance(workflow_run, Mapping):
        _reject("recovery_failed_artifact_run_binding_mismatch")
    if any(type(workflow_run.get(key)) is not int for key in ("id", "repository_id", "head_repository_id")):
        _reject("recovery_failed_artifact_run_binding_mismatch")
    _require_exact_fields(
        workflow_run,
        {
            "id": FAILED_RUN_ID,
            "head_sha": FAILED_RUN_HEAD_SHA,
            "head_branch": FAILED_RUN_HEAD_BRANCH,
            "repository_id": FAILED_REPOSITORY_ID,
            "head_repository_id": FAILED_REPOSITORY_ID,
        },
        "recovery_failed_artifact_run_binding_mismatch",
    )


def _validate_current_run(value: Mapping[str, Any], now: datetime) -> tuple[str, str, str]:
    if value.get("repository") != FAILED_REPOSITORY:
        _reject("recovery_current_run_repository_mismatch")
    processor_run_id = value.get("processor_run_id")
    artifact_run_id = value.get("processor_artifact_run_id")
    run_attempt = value.get("run_attempt")
    if not isinstance(processor_run_id, str):
        _reject("recovery_current_run_identity_invalid")
    match = _RUN_ATTEMPT.fullmatch(processor_run_id)
    if match is None or type(run_attempt) is not int or int(match.group(2)) != run_attempt:
        _reject("recovery_current_run_identity_invalid")
    if not isinstance(artifact_run_id, str) or artifact_run_id != match.group(1):
        _reject("recovery_current_run_identity_invalid")
    artifact_name = value.get("artifact_name")
    if artifact_name != f"upstream-catalogue-processing-{processor_run_id}":
        _reject("recovery_current_run_artifact_name_mismatch")
    if artifact_run_id == str(FAILED_RUN_ID):
        _reject("recovery_current_run_reuses_failed_run")
    head_sha = value.get("head_sha")
    if not isinstance(head_sha, str) or _HEX_40.fullmatch(head_sha) is None:
        _reject("recovery_current_run_head_sha_invalid")
    expires_at = _parse_time(value.get("expires_at"), "recovery_current_run_expiry_invalid")
    if expires_at <= now:
        _reject("recovery_current_run_artifact_expired")
    return processor_run_id, artifact_run_id, artifact_name


def _validate_payload(value: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    receipt_name = "upstream-catalogue-checkpoint-receipt.json"
    result_name = "upstream-catalogue-processing-result.json"
    if set(value) != {receipt_name, result_name}:
        _reject("recovery_artifact_members_invalid")
    receipt_bytes = value[receipt_name]
    result_bytes = value[result_name]
    if not isinstance(receipt_bytes, bytes) or len(receipt_bytes) > MAX_CHECKPOINT_RECEIPT_BYTES:
        _reject("recovery_checkpoint_receipt_invalid")
    if not isinstance(result_bytes, bytes) or len(result_bytes) > MAX_RESULT_BYTES:
        _reject("recovery_processing_result_invalid")
    receipt_file_sha = hashlib.sha256(receipt_bytes).hexdigest()
    result_file_sha = hashlib.sha256(result_bytes).hexdigest()
    if receipt_file_sha != FAILED_CHECKPOINT_FILE_SHA256 or result_file_sha != FAILED_RESULT_FILE_SHA256:
        _reject("recovery_artifact_content_digest_mismatch")
    try:
        receipt = json.loads(receipt_bytes)
        result = json.loads(result_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryRejected("recovery_artifact_json_invalid") from exc
    if not isinstance(receipt, dict) or not isinstance(result, dict):
        _reject("recovery_artifact_json_invalid")
    if receipt.get("checkpoint_sha256") != FAILED_CHECKPOINT_RECEIPT_SHA256:
        _reject("recovery_artifact_checkpoint_digest_mismatch")
    if _checkpoint_digest(receipt) != FAILED_CHECKPOINT_RECEIPT_SHA256:
        _reject("recovery_artifact_checkpoint_digest_mismatch")
    _require_exact_fields(
        receipt,
        {
            "schema_version": CHECKPOINT_SCHEMA,
            "source_id": "data_go_kr",
            "generation_id": GENERATION_ID,
            "status": "quarantined",
            "attempts_consumed": 24,
            "detail_queue_cursor": 24,
            "fencing_token": 1,
            "lease": None,
            "output_digests": [],
        },
        "recovery_artifact_checkpoint_evidence_mismatch",
    )
    outcome = receipt.get("outcome")
    if not isinstance(outcome, Mapping) or outcome.get("reason") != ORIGINAL_SCOPE_ERROR:
        _reject("recovery_artifact_scope_evidence_mismatch")
    if result != {
        "generation_id": GENERATION_ID,
        "reason": "processor_bundle_not_verified",
        "status": "retry",
    }:
        _reject("recovery_artifact_result_evidence_mismatch")
    return receipt, result, receipt_file_sha, result_file_sha


def _validate_original_claim(
    checkpoint: Mapping[str, Any],
    artifact_checkpoint: Mapping[str, Any],
    index: Mapping[str, Any],
    now: datetime,
) -> None:
    if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA:
        _reject("recovery_checkpoint_schema_mismatch")
    if checkpoint.get("checkpoint_sha256") != GENERATION_CHECKPOINT_SHA256:
        _reject("recovery_checkpoint_sha_mismatch")
    if _checkpoint_digest(checkpoint) != GENERATION_CHECKPOINT_SHA256:
        _reject("recovery_checkpoint_digest_mismatch")
    if checkpoint.get("generation_id") != GENERATION_ID or checkpoint.get("source_id") != "data_go_kr":
        _reject("recovery_checkpoint_generation_mismatch")
    if checkpoint.get("status") != "enriching":
        _reject("recovery_checkpoint_status_mismatch")
    generation_inputs = checkpoint.get("generation_inputs")
    if not isinstance(generation_inputs, Mapping):
        _reject("recovery_generation_inputs_invalid")
    if hashlib.sha256(_canonical_json(dict(generation_inputs))).hexdigest() != GENERATION_ID:
        _reject("recovery_generation_inputs_digest_mismatch")
    reservation = checkpoint.get("request_reservation")
    if not isinstance(reservation, Mapping):
        _reject("recovery_request_reservation_invalid")
    lease = checkpoint.get("lease")
    if not isinstance(lease, Mapping):
        _reject("recovery_lease_missing")
    expected_owner = f"{FAILED_RUN_ID}-{FAILED_RUN_ATTEMPT}"
    expected_expiry = "2026-10-03T03:43:30Z"
    _require_exact_fields(
        lease,
        {"owner_run_id": expected_owner, "fencing_token": 1, "expires_at": expected_expiry},
        "recovery_lease_owner_or_fence_mismatch",
    )
    if _parse_time(expected_expiry, "recovery_lease_expiry_invalid") >= now:
        _reject("recovery_lease_still_active")
    _require_exact_fields(
        reservation,
        {
            "owner_run_id": expected_owner,
            "generation_id": GENERATION_ID,
            "fencing_token": 1,
            "reserved_at": "2026-10-03T02:58:33Z",
            "expires_at": expected_expiry,
            "attempt_budget": 24,
            "reserved_attempts": 24,
            "attempts_made": 0,
        },
        "recovery_reservation_identity_mismatch",
    )
    if checkpoint.get("attempts_consumed") != 24 or checkpoint.get("detail_queue_cursor") != 24:
        _reject("recovery_accounting_mismatch")
    attempts = checkpoint.get("attempts_by_id")
    records = reservation.get("records")
    if not isinstance(attempts, Mapping) or not isinstance(records, list) or len(records) != 24:
        _reject("recovery_accounting_mismatch")
    record_ids = {row.get("id") for row in records if isinstance(row, Mapping)}
    if len(record_ids) != 24 or set(attempts) != record_ids or any(value != 1 for value in attempts.values()):
        _reject("recovery_accounting_mismatch")
    if checkpoint.get("fencing_token") != 1:
        _reject("recovery_fence_mismatch")
    output = checkpoint.get("output_artifact")
    if not isinstance(output, Mapping):
        _reject("recovery_output_locator_mismatch")
    _require_exact_fields(
        output,
        {
            "repository": FAILED_REPOSITORY,
            "run_id": str(FAILED_RUN_ID),
            "name": FAILED_ARTIFACT_NAME,
            "artifact_id": None,
            "bundle_manifest_sha256": None,
        },
        "recovery_output_locator_mismatch",
    )
    if checkpoint.get("output_digests") != []:
        _reject("recovery_output_manifest_mismatch")
    if checkpoint.get("generation_inputs") != artifact_checkpoint.get("generation_inputs"):
        _reject("recovery_artifact_generation_inputs_mismatch")
    for field in (
        "input_artifacts", "attempts_by_id", "attempts_consumed",
        "detail_queue_cursor", "fencing_token", "source_id", "generation_id",
    ):
        if checkpoint.get(field) != artifact_checkpoint.get(field):
            _reject(f"recovery_artifact_{field}_mismatch")
    artifact_reservation = artifact_checkpoint.get("request_reservation")
    if not isinstance(artifact_reservation, Mapping) or artifact_reservation.get("attempts_made") != 24:
        _reject("recovery_artifact_reservation_accounting_mismatch")
    durable_reservation = checkpoint.get("request_reservation")
    if not isinstance(durable_reservation, Mapping):
        _reject("recovery_reservation_identity_mismatch")
    if {
        key: value for key, value in durable_reservation.items() if key != "attempts_made"
    } != {
        key: value for key, value in artifact_reservation.items() if key != "attempts_made"
    }:
        _reject("recovery_artifact_reservation_identity_mismatch")
    if artifact_checkpoint.get("outcome", {}).get("reason") != ORIGINAL_SCOPE_ERROR:
        _reject("recovery_artifact_scope_evidence_mismatch")
    if artifact_checkpoint.get("output_artifact", {}).get("artifact_id") is not None:
        _reject("recovery_artifact_must_remain_unbound")
    if not isinstance(index, Mapping) or index.get("schema_version") != CHECKPOINT_SCHEMA:
        _reject("recovery_state_index_schema_mismatch")
    if index.get("detail_queue_cursor") != checkpoint.get("detail_queue_cursor"):
        _reject("recovery_state_index_cursor_mismatch")
    generations = index.get("generations")
    if not isinstance(generations, list):
        _reject("recovery_state_index_generation_mismatch")
    matching = [row for row in generations if isinstance(row, Mapping) and row.get("generation_id") == GENERATION_ID]
    if len(matching) != 1:
        _reject("recovery_state_index_generation_mismatch")
    _require_exact_fields(
        matching[0],
        {
            "generation_id": GENERATION_ID,
            "status": "enriching",
            "checkpoint": f"{GENERATION_ID}.json",
            "updated_at": checkpoint.get("last_heartbeat_at"),
            "candidate_sha256": generation_inputs.get("candidate_sha256"),
        },
        "recovery_state_index_generation_mismatch",
    )
    retry_state = index.get("detail_retry_state")
    if not isinstance(retry_state, Mapping) or set(retry_state) != record_ids:
        _reject("recovery_state_index_retry_state_mismatch")
    for row in records:
        assert isinstance(row, Mapping)
        retry = retry_state.get(row["id"])
        if not isinstance(retry, Mapping):
            _reject("recovery_state_index_retry_state_mismatch")
        _require_exact_fields(
            retry,
            {
                "source_sha256": row.get("source_sha256"),
                "guide_sha256": row.get("guide_sha256"),
                "attempts": attempts[row["id"]],
                "last_attempt_at": reservation.get("reserved_at"),
            },
            "recovery_state_index_retry_state_mismatch",
        )


def recover_failed_generation(
    durable_state: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    failed_run_metadata: Mapping[str, Any],
    failed_artifact_metadata: Mapping[str, Any],
    failed_artifact_dir_or_payload: Mapping[str, bytes],
    expected_checkpoint_sha256: str,
    current_run: Mapping[str, Any],
    now: datetime,
) -> RecoveryPlan:
    """Build a new-run quarantine marker for the one verified failed run.

    ``failed_artifact_dir_or_payload`` is intentionally the two-member byte
    mapping returned after the workflow has verified the full archive digest.
    No filesystem, network, provider, upload, or state-branch operation occurs
    here.
    """
    if not isinstance(durable_state, Mapping) or not isinstance(checkpoint, Mapping):
        _reject("recovery_state_input_invalid")
    if not isinstance(failed_run_metadata, Mapping):
        _reject("recovery_failed_run_metadata_invalid")
    if not isinstance(failed_artifact_metadata, Mapping):
        _reject("recovery_failed_artifact_metadata_invalid")
    if not isinstance(failed_artifact_dir_or_payload, Mapping):
        _reject("recovery_artifact_payload_invalid")
    if not isinstance(current_run, Mapping):
        _reject("recovery_current_run_invalid")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        _reject("recovery_now_must_be_timezone_aware")
    now_utc = now.astimezone(timezone.utc)

    _validate_run_metadata(failed_run_metadata)
    _validate_artifact_metadata(failed_artifact_metadata)
    _processor_run_id, current_artifact_run_id, current_artifact_name = _validate_current_run(current_run, now_utc)

    expected_head = durable_state.get("state_branch_sha")
    if not isinstance(expected_head, str) or _HEX_40.fullmatch(expected_head) is None:
        _reject("recovery_state_head_sha_invalid")
    if expected_checkpoint_sha256 != GENERATION_CHECKPOINT_SHA256:
        _reject("recovery_expected_checkpoint_sha_mismatch")
    if checkpoint.get("checkpoint_sha256") != expected_checkpoint_sha256:
        _reject("recovery_expected_checkpoint_sha_mismatch")
    index = durable_state.get("index")
    if not isinstance(index, Mapping):
        _reject("recovery_state_index_missing")

    (
        artifact_checkpoint,
        _artifact_result,
        receipt_file_sha,
        result_file_sha,
    ) = _validate_payload(failed_artifact_dir_or_payload)
    _validate_original_claim(checkpoint, artifact_checkpoint, index, now_utc)

    updated = copy.deepcopy(dict(checkpoint))
    updated.pop("checkpoint_sha256", None)
    updated["status"] = "quarantined"
    updated["lease"] = None
    updated["fencing_token"] = int(checkpoint["fencing_token"]) + 1
    updated["last_heartbeat_at"] = now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    updated["output_digests"] = []
    updated["output_artifact"] = {
        "repository": current_run["repository"],
        "run_id": current_artifact_run_id,
        "name": current_artifact_name,
        "artifact_id": None,
        "expires_at": current_run["expires_at"],
        "bundle_manifest_sha256": None,
    }
    updated["outcome"] = {
        "reason": RECOVERY_REASON,
        "recovery_evidence": {
            "original_scope_error": ORIGINAL_SCOPE_ERROR,
            "original_checkpoint_sha256": expected_checkpoint_sha256,
            "failed_run": {
                "repository": FAILED_REPOSITORY,
                "id": FAILED_RUN_ID,
                "attempt": FAILED_RUN_ATTEMPT,
                "name": FAILED_RUN_NAME,
                "path": FAILED_RUN_PATH,
                "head_sha": FAILED_RUN_HEAD_SHA,
                "event": "workflow_dispatch",
                "conclusion": "failure",
            },
            "failed_artifact": {
                "id": FAILED_ARTIFACT_ID,
                "name": FAILED_ARTIFACT_NAME,
                "archive_sha256": FAILED_ARTIFACT_SHA256,
                "archive_bytes": FAILED_ARTIFACT_SIZE,
                "checkpoint_sha256": FAILED_CHECKPOINT_RECEIPT_SHA256,
                "checkpoint_file_sha256": receipt_file_sha,
                "processing_result_file_sha256": result_file_sha,
            },
            "recovery_run": {
                "processor_run_id": current_run["processor_run_id"],
                "processor_artifact_run_id": current_artifact_run_id,
                "run_attempt": current_run["run_attempt"],
                "head_sha": current_run["head_sha"],
                "artifact_name": current_artifact_name,
            },
        },
    }
    return RecoveryPlan(
        checkpoint=updated,
        expected_state_head_sha=expected_head,
    )


__all__ = ["RecoveryPlan", "RecoveryRejected", "recover_failed_generation"]
