#!/usr/bin/env python3
"""Generate or check the credential runtime review handoff."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating credential review handoff") from exc


DEFAULT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-review-handoff.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/credential-runtime-review-handoff.json")
SCHEMA_VERSION = "datapan.credential-runtime-review-handoff.v1"
HANDOFF_TICKET = 385


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


def bool_value(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def relief_blockers(source: dict[str, Any], *, global_relief_allowed: bool) -> list[str]:
    state = string_value(source.get("current_receipt_state"), "source.current_receipt_state")
    if global_relief_allowed:
        return []
    if state == "absent":
        return [
            "reviewed_receipt_absent",
            "credentialed_runtime_check_not_reviewed",
            "global_manual_review_reduction_blocked",
        ]
    if state == "staged_only":
        return [
            "staged_receipt_not_checked_in",
            "review_metadata_absent",
            "global_manual_review_reduction_blocked",
        ]
    if state == "reviewed_rejected":
        return [
            "review_decision_keeps_manual_review_boundary",
            "global_manual_review_reduction_blocked",
        ]
    if state == "reviewed_accepted":
        return [
            "source_receipt_not_relief_eligible",
            "global_manual_review_reduction_blocked",
        ]
    if state == "relief_eligible":
        return ["waiting_for_remaining_credential_gated_sources"]
    raise ValueError(f"unsupported receipt state: {state}")


def reviewer_action(source: dict[str, Any], blockers: list[str]) -> str:
    state = string_value(source.get("current_receipt_state"), "source.current_receipt_state")
    if not blockers:
        return "maintain_reviewed_receipt_evidence"
    if state in {"absent", "staged_only"}:
        return "collect_validate_review_and_promote_redacted_receipt"
    if state == "reviewed_rejected":
        return "keep_manual_review_boundary_or_request_new_receipt"
    if state == "reviewed_accepted":
        return "confirm_verified_no_error_outcome_or_keep_global_relief_blocked"
    if state == "relief_eligible":
        return "wait_for_remaining_sources_before_global_relief"
    raise ValueError(f"unsupported receipt state: {state}")


def source_handoff(source: dict[str, Any], *, global_relief_allowed: bool) -> dict[str, Any]:
    source_id = string_value(source.get("source_id"), "source.source_id")
    blockers = relief_blockers(source, global_relief_allowed=global_relief_allowed)
    return {
        "source_id": source_id,
        "provider": string_value(source.get("provider"), f"{source_id}.provider"),
        "current_receipt_state": string_value(
            source.get("current_receipt_state"),
            f"{source_id}.current_receipt_state",
        ),
        "checked_in_review_state": string_value(
            source.get("checked_in_review_state"),
            f"{source_id}.checked_in_review_state",
        ),
        "checked_in_receipt_present": bool_value(
            source.get("checked_in_receipt_present"),
            f"{source_id}.checked_in_receipt_present",
        ),
        "receipt_relief_eligible": bool_value(
            source.get("receipt_relief_eligible"),
            f"{source_id}.receipt_relief_eligible",
        ),
        "expected_evidence": {
            "candidate_batch": string_value(source.get("candidate_batch"), f"{source_id}.candidate_batch"),
            "runtime_evidence_plan": string_value(
                source.get("runtime_evidence_plan"),
                f"{source_id}.runtime_evidence_plan",
            ),
            "staged_receipt_path": string_value(
                source.get("staged_receipt_path"),
                f"{source_id}.staged_receipt_path",
            ),
            "reviewed_receipt_path": string_value(
                source.get("reviewed_receipt_path"),
                f"{source_id}.reviewed_receipt_path",
            ),
        },
        "operator_commands": {
            "collection_preflight": string_value(
                source.get("collection_preflight_command"),
                f"{source_id}.collection_preflight_command",
            ),
            "collection_run": string_value(
                source.get("collection_run_command"),
                f"{source_id}.collection_run_command",
            ),
            "staged_receipt_validation": string_value(
                source.get("staged_receipt_validation_command"),
                f"{source_id}.staged_receipt_validation_command",
            ),
            "reviewed_receipt_promotion": string_value(
                source.get("reviewed_receipt_promotion_command"),
                f"{source_id}.reviewed_receipt_promotion_command",
            ),
            "reviewed_receipt_validation": string_value(
                source.get("reviewed_receipt_validation_command"),
                f"{source_id}.reviewed_receipt_validation_command",
            ),
        },
        "review_packet_requirements": [
            "staged_receipt_schema_valid",
            "staged_receipt_redaction_safe",
            "review_metadata_present_before_check_in",
            "review_decision_matches_receipt_outcome",
            "reviewed_receipt_validates_in_default_ci",
        ],
        "relief_blockers": blockers,
        "next_reviewer_action": reviewer_action(source, blockers),
    }


def validate_queue_invariants(queue: dict[str, Any]) -> None:
    summary = as_dict(queue.get("summary"), "queue.summary")
    operator_contract = as_dict(queue.get("operator_contract"), "queue.operator_contract")
    release_boundary = as_dict(queue.get("release_boundary"), "queue.release_boundary")
    if summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("review handoff requires default_ci_requires_credentials=false")
    if summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("review handoff requires checked_in_secrets_allowed=false")
    if operator_contract.get("review_required_for_checked_in_receipts") is not True:
        raise ValueError("review handoff requires review metadata for checked-in receipts")
    if release_boundary.get("canonical_registry_compatible") is not True:
        raise ValueError("review handoff must preserve canonical registry compatibility")


def build_report(queue: dict[str, Any]) -> dict[str, Any]:
    validate_queue_invariants(queue)
    summary = as_dict(queue.get("summary"), "queue.summary")
    operator_contract = as_dict(queue.get("operator_contract"), "queue.operator_contract")
    release_boundary = as_dict(queue.get("release_boundary"), "queue.release_boundary")
    sources = [as_dict(source, "queue.sources[]") for source in as_list(queue.get("sources"), "queue.sources")]
    global_relief_allowed = bool_value(
        summary.get("manual_review_reduction_allowed"),
        "queue.summary.manual_review_reduction_allowed",
    )
    entries = [
        source_handoff(source, global_relief_allowed=global_relief_allowed)
        for source in sorted(sources, key=lambda item: str(item.get("source_id")))
    ]
    pending_review = sum(1 for entry in entries if entry["relief_blockers"])

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": string_value(queue.get("generated_at"), "queue.generated_at"),
        "goal_issue": 344,
        "handoff_ticket": HANDOFF_TICKET,
        "queue_ticket": queue.get("queue_ticket"),
        "provider": "datapan-registry",
        "summary": {
            "sources": len(entries),
            "credential_gated_sources": summary.get("credential_gated_sources"),
            "pending_review_sources": pending_review,
            "reviewed_receipts_checked_in": summary.get("reviewed_receipts_checked_in"),
            "relief_eligible_sources": summary.get("relief_eligible"),
            "global_manual_review_relief_allowed": global_relief_allowed,
            "manual_review_required": release_boundary.get("manual_review_required"),
            "default_ci_requires_credentials": False,
            "checked_in_secrets_allowed": False,
            "handoff_status": "relief_ready" if global_relief_allowed else "review_required",
        },
        "operator_contract": {
            "queue": DEFAULT_QUEUE.as_posix(),
            "handoff_check_command": "python3 scripts/generate-credential-runtime-review-handoff.py --check",
            "queue_check_command": string_value(
                operator_contract.get("queue_check_command"),
                "operator_contract.queue_check_command",
            ),
            "collection_runner_script": string_value(
                operator_contract.get("collection_runner_script"),
                "operator_contract.collection_runner_script",
            ),
            "receipt_promotion_script": string_value(
                operator_contract.get("receipt_promotion_script"),
                "operator_contract.receipt_promotion_script",
            ),
            "receipt_validation_command": string_value(
                operator_contract.get("receipt_validation_command"),
                "operator_contract.receipt_validation_command",
            ),
            "reviewed_receipt_glob": string_value(
                operator_contract.get("reviewed_receipt_glob"),
                "operator_contract.reviewed_receipt_glob",
            ),
            "default_ci_mode": "secret_free_review_handoff_validation",
        },
        "release_boundary": {
            "canonical_registry_compatible": True,
            "manual_review_required": release_boundary.get("manual_review_required"),
            "compatibility_effect": "review_handoff_only",
            "live_evidence_claim": release_boundary.get("live_evidence_claim"),
            "relief_decision": (
                "allowed_by_all_reviewed_validated_receipts"
                if global_relief_allowed
                else "blocked_until_all_sources_relief_eligible"
            ),
        },
        "global_review_requirements": [
            "one_reviewed_redacted_receipt_per_credential_gated_source",
            "all_reviewed_receipts_validate_in_default_ci",
            "all_reviewed_receipts_have_reviewed_accepted_state",
            "all_reviewed_receipts_have_verified_outcome_and_no_error_class",
            "no_secret_values_hashes_headers_or_service_keys_checked_in",
            "source_runtime_remediation_and_consumer_compatibility_counts_recompute_after_promotion",
        ],
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
    parser.add_argument("--queue", default=DEFAULT_QUEUE, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when the checked-in handoff is stale")
    args = parser.parse_args()

    try:
        report = build_report(load_json(args.queue))
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate credential runtime review handoff: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing credential runtime review handoff", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale credential runtime review handoff; "
                "run `python3 scripts/generate-credential-runtime-review-handoff.py`",
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
