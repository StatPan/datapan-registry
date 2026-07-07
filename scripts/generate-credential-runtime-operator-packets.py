#!/usr/bin/env python3
"""Generate or check credential runtime operator packet evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating credential operator packets") from exc


DEFAULT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_HANDOFF = pathlib.Path("reports/credential-runtime-review-handoff.json")
DEFAULT_DECISION = pathlib.Path("reports/release-consumer-decision.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-operator-packets.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/credential-runtime-operator-packets.json")
SCHEMA_VERSION = "datapan.credential-runtime-operator-packets.v1"
PACKET_TICKET = 405
DEFAULT_SESSION_OUTPUT = ".datapan/runtime-evidence/credential-runtime-collection-session.json"
DEFAULT_SESSION_REVIEW_PLAN_OUTPUT = ".datapan/runtime-evidence/credential-runtime-session-review-plan.json"
SESSION_VALIDATION_COMMAND = (
    "python3 scripts/validate-credential-runtime-collection-session.py "
    f"{DEFAULT_SESSION_OUTPUT} --require-complete-source-set"
)
SESSION_REVIEW_PLAN_COMMAND = (
    "python3 scripts/generate-credential-runtime-session-review-plan.py "
    f"{DEFAULT_SESSION_OUTPUT} --output {DEFAULT_SESSION_REVIEW_PLAN_OUTPUT}"
)
SESSION_REVIEW_PLAN_VALIDATION_COMMAND = (
    "python3 scripts/validate-credential-runtime-session-review-plan.py "
    f"{DEFAULT_SESSION_REVIEW_PLAN_OUTPUT} --queue {DEFAULT_QUEUE.as_posix()}"
)
RELEASE_EVIDENCE_REFRESH_COMMAND = "python3 scripts/refresh-release-ledger-evidence.py --write --max-iterations 5"
RELEASE_EVIDENCE_CHECK_COMMAND = "python3 scripts/refresh-release-ledger-evidence.py --check"


POST_PROMOTION_CHECKS = [
    RELEASE_EVIDENCE_REFRESH_COMMAND,
    RELEASE_EVIDENCE_CHECK_COMMAND,
]

DOWNSTREAM_EVIDENCE = [
    "reports/credential-runtime-evidence-policy.json",
    "reports/credential-runtime-collection-preflight.json",
    "reports/credential-runtime-receipt-collection-queue.json",
    "reports/credential-runtime-review-handoff.json",
    "reports/source-runtime-remediation-map.json",
    "reports/release-consumer-compatibility.json",
    "reports/release-consumer-decision.json",
    "reports/release-goal-finish-preflight.json",
    "reports/release-goal-continuation-queue.json",
    "reports/release-goal-operating-contract.json",
    "reports/release-assembly-receipt.json",
    "manifest.json",
]

BATCH_COLLECTION_COMMANDS = {
    "secret_free_batch_preflight": "python3 scripts/run-credential-runtime-collection.py --all --json",
    "require_env_batch_preflight": "python3 scripts/run-credential-runtime-collection.py --all --require-env",
    "batch_collection_run": (
        "python3 scripts/run-credential-runtime-collection.py "
        "--all --run --skip-not-ready --continue-on-error --json"
    ),
    "batch_collection_run_with_session_output": (
        "python3 scripts/run-credential-runtime-collection.py "
        "--all --run --skip-not-ready --continue-on-error "
        f"--session-output {DEFAULT_SESSION_OUTPUT} --json"
    ),
    "session_output_validation": SESSION_VALIDATION_COMMAND,
    "session_review_plan_generation": SESSION_REVIEW_PLAN_COMMAND,
    "session_review_plan_validation": SESSION_REVIEW_PLAN_VALIDATION_COMMAND,
    "batch_runner_self_test": "python3 scripts/run-credential-runtime-collection.py --self-test",
}


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


def credential_envs(operator_command: str) -> list[str]:
    names = sorted(
        part.split("=", 1)[0]
        for part in shlex.split(operator_command)
        if part.endswith("=<secret>")
    )
    if not names:
        raise ValueError("operator command must declare credential env placeholders")
    return names


def handoff_by_source(handoff: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = as_list(handoff.get("sources"), "handoff.sources")
    result: dict[str, dict[str, Any]] = {}
    for raw_entry in entries:
        entry = as_dict(raw_entry, "handoff.sources[]")
        source_id = string_value(entry.get("source_id"), "handoff.source_id")
        result[source_id] = entry
    return result


def source_packet(source: dict[str, Any], handoff_entry: dict[str, Any]) -> dict[str, Any]:
    source_id = string_value(source.get("source_id"), "source.source_id")
    envs = credential_envs(string_value(source.get("operator_command"), f"{source_id}.operator_command"))
    commands = as_dict(handoff_entry.get("operator_commands"), f"{source_id}.operator_commands")
    expected_evidence = as_dict(handoff_entry.get("expected_evidence"), f"{source_id}.expected_evidence")
    relief_blockers = [
        string_value(blocker, f"{source_id}.relief_blockers[]")
        for blocker in as_list(handoff_entry.get("relief_blockers"), f"{source_id}.relief_blockers")
    ]
    current_state = string_value(source.get("current_receipt_state"), f"{source_id}.current_receipt_state")
    reviewed_present = bool_value(source.get("checked_in_receipt_present"), f"{source_id}.checked_in_receipt_present")
    packet_status = "reviewed_receipt_present" if reviewed_present else "operator_credentials_and_review_required"
    return {
        "source_id": source_id,
        "provider": string_value(source.get("provider"), f"{source_id}.provider"),
        "current_receipt_state": current_state,
        "packet_status": packet_status,
        "credential_envs": envs,
        "required_operator_inputs": [
            "credential_env_values",
            "reviewer_identity",
            "review_reason",
            "review_decision",
        ],
        "evidence_paths": {
            "candidate_batch": string_value(expected_evidence.get("candidate_batch"), f"{source_id}.candidate_batch"),
            "runtime_evidence_plan": string_value(
                expected_evidence.get("runtime_evidence_plan"),
                f"{source_id}.runtime_evidence_plan",
            ),
            "staged_receipt_path": string_value(
                expected_evidence.get("staged_receipt_path"),
                f"{source_id}.staged_receipt_path",
            ),
            "reviewed_receipt_path": string_value(
                expected_evidence.get("reviewed_receipt_path"),
                f"{source_id}.reviewed_receipt_path",
            ),
        },
        "commands": {
            "secret_free_preflight": string_value(
                commands.get("collection_preflight"),
                f"{source_id}.collection_preflight",
            ),
            "require_env_preflight": f"python3 scripts/run-credential-runtime-collection.py --source {source_id} --require-env",
            "bounded_collection_run": string_value(commands.get("collection_run"), f"{source_id}.collection_run"),
            "staged_receipt_validation": string_value(
                commands.get("staged_receipt_validation"),
                f"{source_id}.staged_receipt_validation",
            ),
            "reviewed_receipt_promotion": string_value(
                commands.get("reviewed_receipt_promotion"),
                f"{source_id}.reviewed_receipt_promotion",
            ),
            "reviewed_receipt_validation": string_value(
                commands.get("reviewed_receipt_validation"),
                f"{source_id}.reviewed_receipt_validation",
            ),
            "post_promotion_checks": POST_PROMOTION_CHECKS,
        },
        "downstream_evidence_affected": DOWNSTREAM_EVIDENCE,
        "relief_blockers": relief_blockers,
        "next_action": string_value(source.get("next_action"), f"{source_id}.next_action"),
    }


def build_report(queue: dict[str, Any], handoff: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    queue_summary = as_dict(queue.get("summary"), "queue.summary")
    handoff_summary = as_dict(handoff.get("summary"), "handoff.summary")
    decision_summary = as_dict(decision.get("summary"), "decision.summary")
    source_handoff = handoff_by_source(handoff)
    sources = [as_dict(source, "queue.sources[]") for source in as_list(queue.get("sources"), "queue.sources")]
    packets = []
    for source in sorted(sources, key=lambda item: str(item.get("source_id"))):
        source_id = string_value(source.get("source_id"), "source.source_id")
        if source_id not in source_handoff:
            raise ValueError(f"handoff missing source packet: {source_id}")
        packets.append(source_packet(source, source_handoff[source_id]))

    reviewed_receipts = int(queue_summary.get("reviewed_receipts_checked_in", 0))
    reviewed_missing = len(packets) - reviewed_receipts
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": string_value(queue.get("generated_at"), "queue.generated_at"),
        "goal_issue": 344,
        "operator_packet_ticket": PACKET_TICKET,
        "provider": "datapan-registry",
        "inputs": {
            "collection_queue": DEFAULT_QUEUE.as_posix(),
            "review_handoff": DEFAULT_HANDOFF.as_posix(),
            "release_consumer_decision": DEFAULT_DECISION.as_posix(),
        },
        "summary": {
            "sources": len(packets),
            "credential_gated_sources": queue_summary.get("credential_gated_sources"),
            "operator_packets": len(packets),
            "packets_ready_for_operator_credentials": sum(
                1 for packet in packets if packet["packet_status"] == "operator_credentials_and_review_required"
            ),
            "packets_waiting_for_review": handoff_summary.get("pending_review_sources"),
            "reviewed_receipts_checked_in": reviewed_receipts,
            "reviewed_receipts_missing": reviewed_missing,
            "manual_review_required": decision_summary.get("manual_review_required"),
            "manual_review_accepted": decision_summary.get("manual_review_accepted"),
            "default_ci_requires_credentials": False,
            "checked_in_secrets_allowed": False,
            "packet_status": "reviewed_receipts_complete" if reviewed_missing == 0 else "operator_collection_required",
        },
        "post_promotion_contract": {
            "commands": POST_PROMOTION_CHECKS,
            "release_evidence_refresh_command": RELEASE_EVIDENCE_REFRESH_COMMAND,
            "release_evidence_check_command": RELEASE_EVIDENCE_CHECK_COMMAND,
            "downstream_evidence_affected": DOWNSTREAM_EVIDENCE,
            "goal_closure_effect": "no_goal_closure_without_reviewed_receipts_or_explicit_manual_review_acceptance",
        },
        "batch_collection_contract": {
            "commands": BATCH_COLLECTION_COMMANDS,
            "skip_not_ready_allowed": True,
            "continue_on_error_allowed": True,
            "session_output_schema": "datapan.credential-runtime-collection-session.v1",
            "session_output_schema_path": "schemas/datapan.credential-runtime-collection-session.v1.schema.json",
            "session_output_path": DEFAULT_SESSION_OUTPUT,
            "session_output_validation_command": SESSION_VALIDATION_COMMAND,
            "session_review_plan_schema": "datapan.credential-runtime-session-review-plan.v1",
            "session_review_plan_schema_path": "schemas/datapan.credential-runtime-session-review-plan.v1.schema.json",
            "session_review_plan_output_path": DEFAULT_SESSION_REVIEW_PLAN_OUTPUT,
            "session_review_plan_command": SESSION_REVIEW_PLAN_COMMAND,
            "session_review_plan_validation_command": SESSION_REVIEW_PLAN_VALIDATION_COMMAND,
            "reviewer_handoff_command": SESSION_VALIDATION_COMMAND,
            "checked_in_session_output_allowed": False,
            "checked_in_review_plan_allowed": False,
            "checked_in_secrets_allowed": False,
            "default_ci_requires_credentials": False,
        },
        "packets": packets,
    }


def validate_invariants(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    packets = [as_dict(packet, "packets[]") for packet in as_list(report.get("packets"), "packets")]
    if summary.get("sources") != len(packets):
        raise ValueError("summary.sources must match packets length")
    if summary.get("operator_packets") != len(packets):
        raise ValueError("summary.operator_packets must match packets length")
    if summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("operator packets must preserve secret-free default CI")
    if summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("operator packets must not allow checked-in secrets")
    if summary.get("manual_review_accepted") is not False:
        raise ValueError("operator packets cannot assert manual-review acceptance")
    post_promotion_contract = as_dict(report.get("post_promotion_contract"), "post_promotion_contract")
    if post_promotion_contract.get("release_evidence_refresh_command") != RELEASE_EVIDENCE_REFRESH_COMMAND:
        raise ValueError("post-promotion contract must expose the fixed-point refresh command")
    if post_promotion_contract.get("release_evidence_check_command") != RELEASE_EVIDENCE_CHECK_COMMAND:
        raise ValueError("post-promotion contract must expose the fixed-point check command")
    if as_list(post_promotion_contract.get("commands"), "post_promotion_contract.commands") != POST_PROMOTION_CHECKS:
        raise ValueError("post-promotion contract commands must refresh and check release evidence")
    batch_contract = as_dict(report.get("batch_collection_contract"), "batch_collection_contract")
    batch_commands = as_dict(batch_contract.get("commands"), "batch_collection_contract.commands")
    for key, command in batch_commands.items():
        value = string_value(command, f"batch_collection_contract.commands.{key}")
        if "<secret>" in value:
            raise ValueError(f"batch command {key} includes a secret placeholder")
    if batch_contract.get("checked_in_secrets_allowed") is not False:
        raise ValueError("batch collection contract must not allow checked-in secrets")
    if batch_contract.get("checked_in_session_output_allowed") is not False:
        raise ValueError("batch collection contract must not allow checked-in live session output")
    if batch_contract.get("checked_in_review_plan_allowed") is not False:
        raise ValueError("batch collection contract must not allow checked-in live review plans")
    if batch_contract.get("default_ci_requires_credentials") is not False:
        raise ValueError("batch collection contract must preserve secret-free default CI")
    seen: set[str] = set()
    for packet in packets:
        source_id = string_value(packet.get("source_id"), "packet.source_id")
        if source_id in seen:
            raise ValueError(f"duplicate source packet: {source_id}")
        seen.add(source_id)
        commands = as_dict(packet.get("commands"), f"{source_id}.commands")
        for key, value in commands.items():
            if key == "post_promotion_checks":
                post_promotion_checks = as_list(value, f"{source_id}.commands.post_promotion_checks")
                if post_promotion_checks != POST_PROMOTION_CHECKS:
                    raise ValueError(f"{source_id}: post-promotion checks must refresh and check release evidence")
                for command in post_promotion_checks:
                    if "<secret>" in string_value(command, f"{source_id}.post_promotion_checks[]"):
                        raise ValueError(f"{source_id}: post-promotion command includes a secret placeholder")
                continue
            command = string_value(value, f"{source_id}.commands.{key}")
            if "<secret>" in command:
                raise ValueError(f"{source_id}: {key} includes a secret placeholder")


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
    parser.add_argument("--handoff", default=DEFAULT_HANDOFF, type=pathlib.Path)
    parser.add_argument("--decision", default=DEFAULT_DECISION, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in operator packet evidence is stale")
    args = parser.parse_args()

    try:
        report = build_report(load_json(args.queue), load_json(args.handoff), load_json(args.decision))
        validate_invariants(report)
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate credential operator packets: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing credential operator packets", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale credential operator packets; "
                "run `python3 scripts/generate-credential-runtime-operator-packets.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (packets={report['summary']['operator_packets']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (packets={report['summary']['operator_packets']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
