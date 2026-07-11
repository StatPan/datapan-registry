#!/usr/bin/env python3
"""Generate or check the reviewed credential receipt collection queue."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import credential_runtime_receipts as receipts

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before generating credential receipt collection queue"
    ) from exc


DEFAULT_POLICY = pathlib.Path("reports/credential-runtime-evidence-policy.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-receipt-collection-queue.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_RECEIPT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-receipt.v1.schema.json")
SCHEMA_VERSION = "datapan.credential-runtime-receipt-collection-queue.v1"
QUEUE_TICKET = 375
PROMOTION_SCRIPT = "scripts/promote-credential-runtime-receipt.py"
COLLECTION_RUNNER = "scripts/run-credential-runtime-collection.py"


STATE_DEFINITIONS = [
    {
        "state": "absent",
        "meaning": "No reviewed checked-in credential runtime receipt exists for this source.",
        "relief_eligible": False,
    },
    {
        "state": "staged_only",
        "meaning": (
            "A receipt exists only at the local staging path; it is not default-CI evidence "
            "until reviewed and promoted to reports/credential-runtime-receipts/."
        ),
        "relief_eligible": False,
    },
    {
        "state": "reviewed_rejected",
        "meaning": "A checked-in receipt was reviewed and keeps the manual-review boundary.",
        "relief_eligible": False,
    },
    {
        "state": "reviewed_accepted",
        "meaning": "A checked-in receipt was reviewed and accepted, but global relief still needs every credential-gated source to qualify.",
        "relief_eligible": False,
    },
    {
        "state": "relief_eligible",
        "meaning": "A checked-in reviewed receipt is accepted with a verified, redaction-safe, no-error outcome.",
        "relief_eligible": True,
    },
]


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def string_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def receipt_state(record: dict[str, Any] | None) -> str:
    if record is None:
        return "absent"
    if record.get("review_state") == "reviewed_rejected":
        return "reviewed_rejected"
    if record.get("relief_eligible") is True:
        return "relief_eligible"
    return "reviewed_accepted"


def next_action(state: str, manual_review_reduction_allowed: bool) -> str:
    if manual_review_reduction_allowed:
        return "maintain_receipt_backed_compatibility_relief_evidence"
    if state == "absent":
        return "run_bounded_credentialed_runtime_check_then_review_and_promote_receipt"
    if state == "reviewed_rejected":
        return "keep_manual_review_boundary_or_collect_new_redacted_receipt"
    if state == "reviewed_accepted":
        return "collect_remaining_reviewed_receipts_before_requesting_global_relief"
    if state == "relief_eligible":
        return "wait_for_all_credential_gated_sources_to_be_relief_eligible"
    raise ValueError(f"unsupported receipt state: {state}")


def source_queue_entry(
    source: dict[str, Any],
    receipt_records: dict[str, dict[str, Any]],
    *,
    manual_review_reduction_allowed: bool,
) -> dict[str, Any]:
    source_id = string_value(source.get("source_id"), "policy.sources[].source_id")
    bounded_path = as_dict(source.get("bounded_live_evidence_path"), f"{source_id}.bounded_live_evidence_path")
    record = receipt_records.get(source_id)
    state = receipt_state(record)
    reviewed_receipt_path = string_value(
        bounded_path.get("reviewed_receipt_artifact"),
        f"{source_id}.bounded_live_evidence_path.reviewed_receipt_artifact",
    )
    staged_receipt_path = string_value(
        bounded_path.get("receipt_artifact"),
        f"{source_id}.bounded_live_evidence_path.receipt_artifact",
    )
    receipt_present = record is not None
    return {
        "source_id": source_id,
        "provider": string_value(source.get("provider"), f"{source_id}.provider"),
        "candidate_batch": string_value(source.get("candidate_batch"), f"{source_id}.candidate_batch"),
        "runtime_evidence_plan": string_value(
            source.get("runtime_evidence_plan"),
            f"{source_id}.runtime_evidence_plan",
        ),
        "staged_receipt_path": staged_receipt_path,
        "reviewed_receipt_path": reviewed_receipt_path,
        "operator_command": string_value(
            bounded_path.get("operator_command"),
            f"{source_id}.bounded_live_evidence_path.operator_command",
        ),
        "generic_verification_artifact": string_value(
            bounded_path.get("generic_verification_artifact"),
            f"{source_id}.bounded_live_evidence_path.generic_verification_artifact",
        ),
        "staged_receipt_validation_command": (
            "python3 scripts/validate-credential-runtime-receipts.py --allow-unreviewed "
            f"{staged_receipt_path}"
        ),
        "collection_preflight_command": (
            f"python3 scripts/run-credential-runtime-collection.py --source {source_id} --json"
        ),
        "collection_run_command": (
            f"python3 scripts/run-credential-runtime-collection.py --source {source_id} --run"
        ),
        "reviewed_receipt_validation_command": "python3 scripts/validate-credential-runtime-receipts.py",
        "reviewed_receipt_promotion_command": (
            "python3 scripts/promote-credential-runtime-receipt.py "
            f"{staged_receipt_path} --state <reviewed_accepted|reviewed_rejected> "
            "--decision <allows_manual_review_reduction|keeps_manual_review_boundary> "
            "--reviewer <reviewer> --reason <reason>"
        ),
        "review_required": True,
        "promotion_gate": string_value(
            bounded_path.get("promotion_gate"),
            f"{source_id}.bounded_live_evidence_path.promotion_gate",
        ),
        "current_receipt_state": state,
        "checked_in_review_state": record.get("review_state") if record else "none",
        "checked_in_receipt_present": receipt_present,
        "checked_in_receipt_path": record.get("path") if record else reviewed_receipt_path,
        "receipt_outcome": record.get("outcome") if record else "none",
        "receipt_relief_eligible": bool(record and record.get("relief_eligible") is True),
        "default_ci_requires_credentials": False,
        "next_action": next_action(state, manual_review_reduction_allowed),
    }


def validate_policy_invariants(policy: dict[str, Any]) -> None:
    summary = as_dict(policy.get("summary"), "policy.summary")
    operator_contract = as_dict(policy.get("operator_contract"), "policy.operator_contract")
    release_boundary = as_dict(policy.get("release_boundary"), "policy.release_boundary")
    intake = as_dict(release_boundary.get("reviewed_receipt_intake"), "policy.release_boundary.reviewed_receipt_intake")
    relief_gate = as_dict(release_boundary.get("receipt_backed_relief_gate"), "policy.release_boundary.receipt_backed_relief_gate")
    if summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("credential policy must keep default_ci_requires_credentials=false")
    if summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("credential policy must keep checked_in_secrets_allowed=false")
    if operator_contract.get("reviewed_receipt_glob") != receipts.DEFAULT_RECEIPT_GLOB:
        raise ValueError("credential policy reviewed receipt glob drifted from shared receipt helper")
    if intake.get("checked_in_receipt_glob") != receipts.DEFAULT_RECEIPT_GLOB:
        raise ValueError("credential policy intake glob drifted from shared receipt helper")
    if relief_gate.get("manual_review_reduction_allowed") != summary.get("manual_review_reduction_allowed"):
        raise ValueError("credential policy summary and relief gate disagree on manual_review_reduction_allowed")


def build_report(policy: dict[str, Any]) -> dict[str, Any]:
    validate_policy_invariants(policy)
    generated_at = string_value(policy.get("generated_at"), "policy.generated_at")
    summary = as_dict(policy.get("summary"), "policy.summary")
    operator_contract = as_dict(policy.get("operator_contract"), "policy.operator_contract")
    release_boundary = as_dict(policy.get("release_boundary"), "policy.release_boundary")
    relief_gate = as_dict(release_boundary.get("receipt_backed_relief_gate"), "policy.release_boundary.receipt_backed_relief_gate")
    sources = [as_dict(item, "policy.sources[]") for item in as_list(policy.get("sources"), "policy.sources")]

    receipt_state_data = receipts.discover_reviewed_receipts(
        receipt_glob=receipts.DEFAULT_RECEIPT_GLOB,
        schema_path=DEFAULT_RECEIPT_SCHEMA,
        sources=sources,
    )
    records_by_source = {
        string_value(record.get("source_id"), "receipt_records[].source_id"): record
        for record in as_list(receipt_state_data.get("receipt_records"), "receipt_state.receipt_records")
    }
    manual_review_reduction_allowed = receipt_state_data["manual_review_reduction_allowed"]
    entries = [
        source_queue_entry(
            source,
            records_by_source,
            manual_review_reduction_allowed=manual_review_reduction_allowed,
        )
        for source in sorted(sources, key=lambda item: str(item.get("source_id")))
    ]

    counts = {
        "absent": sum(1 for entry in entries if entry["current_receipt_state"] == "absent"),
        "staged_only": 0,
        "reviewed_rejected": sum(1 for entry in entries if entry["current_receipt_state"] == "reviewed_rejected"),
        "reviewed_accepted": sum(1 for entry in entries if entry["current_receipt_state"] == "reviewed_accepted"),
        "relief_eligible": sum(1 for entry in entries if entry["current_receipt_state"] == "relief_eligible"),
    }
    if summary.get("reviewed_receipts_checked_in") != receipt_state_data["receipt_count"]:
        raise ValueError("credential policy reviewed receipt count is stale")
    if summary.get("manual_review_reduction_allowed") != manual_review_reduction_allowed:
        raise ValueError("credential policy manual-review reduction state is stale")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "queue_ticket": QUEUE_TICKET,
        "provider": "datapan-registry",
        "summary": {
            "sources": len(entries),
            "credential_gated_sources": summary.get("credential_gated_sources"),
            "absent": counts["absent"],
            "staged_only": counts["staged_only"],
            "reviewed_rejected": counts["reviewed_rejected"],
            "reviewed_accepted": counts["reviewed_accepted"],
            "relief_eligible": counts["relief_eligible"],
            "reviewed_receipts_checked_in": receipt_state_data["receipt_count"],
            "manual_review_reduction_allowed": manual_review_reduction_allowed,
            "default_ci_requires_credentials": False,
            "checked_in_secrets_allowed": False,
            "queue_status": (
                "complete_relief_eligible"
                if manual_review_reduction_allowed
                else "collection_required"
            ),
        },
        "operator_contract": {
            "policy": DEFAULT_POLICY.as_posix(),
            "queue_check_command": "python3 scripts/generate-credential-runtime-receipt-collection-queue.py --check",
            "policy_check_command": operator_contract.get("policy_check_command"),
            "receipt_validation_command": operator_contract.get("receipt_validation_command"),
            "receipt_promotion_command_template": (
                "python3 scripts/promote-credential-runtime-receipt.py "
                ".datapan/runtime-evidence/<source>-credentialed-receipt.json "
                "--state <reviewed_accepted|reviewed_rejected> "
                "--decision <allows_manual_review_reduction|keeps_manual_review_boundary> "
                "--reviewer <reviewer> --reason <reason>"
            ),
            "receipt_promotion_script": PROMOTION_SCRIPT,
            "collection_runner_script": COLLECTION_RUNNER,
            "collection_preflight_command_template": (
                "python3 scripts/run-credential-runtime-collection.py --source <source_id> --json"
            ),
            "collection_run_command_template": (
                "python3 scripts/run-credential-runtime-collection.py --source <source_id> --run"
            ),
            "staged_receipt_validation_command": operator_contract.get("staged_receipt_validation_command"),
            "staged_receipt_glob": operator_contract.get("staged_receipt_glob"),
            "reviewed_receipt_glob": operator_contract.get("reviewed_receipt_glob"),
            "review_required_for_checked_in_receipts": True,
            "default_ci_mode": "secret_free_queue_validation",
        },
        "release_boundary": {
            "canonical_registry_compatible": True,
            "manual_review_required": not manual_review_reduction_allowed,
            "receipt_backed_relief_status": relief_gate.get("status"),
            "compatibility_effect": "operator_collection_queue_only",
            "live_evidence_claim": "not_claimed_until_reviewed_receipts_exist",
        },
        "state_model": STATE_DEFINITIONS,
        "sources": entries,
    }


def validate_schema(report: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("; ".join(rendered))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default=DEFAULT_POLICY, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when the checked-in queue is stale")
    args = parser.parse_args()

    try:
        report = build_report(load_json(args.policy))
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate credential runtime receipt collection queue: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing credential receipt collection queue", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale credential receipt collection queue; "
                "run `python3 scripts/generate-credential-runtime-receipt-collection-queue.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (sources={report['summary']['sources']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (sources={report['summary']['sources']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
