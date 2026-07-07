#!/usr/bin/env python3
"""Generate or check the credential runtime collection execution plan."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before generating credential collection execution plans"
    ) from exc


DEFAULT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_RUNNER_READINESS = pathlib.Path("reports/credential-runtime-runner-readiness.json")
DEFAULT_OPERATOR_PACKETS = pathlib.Path("reports/credential-runtime-operator-packets.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-collection-execution-plan.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/credential-runtime-collection-execution-plan.json")
SCHEMA_VERSION = "datapan.credential-runtime-collection-execution-plan.v1"
OPERATOR_WORKFLOW_SCRIPT = pathlib.Path("scripts/run-credential-runtime-operator-workflow.py")


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


def count_value(value: object, label: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def source_map(items: list[Any], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        entry = as_dict(item, f"{label}[]")
        source_id = string_value(entry.get("source_id"), f"{label}.source_id")
        if source_id in result:
            raise ValueError(f"{label} contains duplicate source_id {source_id}")
        result[source_id] = entry
    return result


def execution_status(summary: dict[str, Any]) -> str:
    reviewed_missing = count_value(summary.get("reviewed_receipts_missing"), "summary.reviewed_receipts_missing")
    candidate_missing = count_value(summary.get("candidate_batches_missing"), "summary.candidate_batches_missing")
    blocked_on_env = count_value(summary.get("blocked_on_operator_env"), "summary.blocked_on_operator_env")
    if reviewed_missing == 0:
        return "reviewed_receipts_complete"
    if candidate_missing > 0:
        return "candidate_batches_missing"
    if blocked_on_env > 0:
        return "operator_credentials_required"
    return "ready_for_operator_collection"


def build_source_plan(
    source_id: str,
    readiness: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, Any]:
    evidence_paths = as_dict(packet.get("evidence_paths"), f"{source_id}.evidence_paths")
    commands = as_dict(packet.get("commands"), f"{source_id}.commands")
    readiness_blockers = [string_value(item, f"{source_id}.readiness_blockers[]") for item in as_list(
        readiness.get("readiness_blockers"),
        f"{source_id}.readiness_blockers",
    )]
    return {
        "source_id": source_id,
        "provider": string_value(packet.get("provider"), f"{source_id}.provider"),
        "credential_envs": [
            string_value(item, f"{source_id}.credential_envs[]")
            for item in as_list(packet.get("credential_envs"), f"{source_id}.credential_envs")
        ],
        "candidate_batch": string_value(evidence_paths.get("candidate_batch"), f"{source_id}.candidate_batch"),
        "candidate_batch_present": bool_value(
            readiness.get("candidate_batch_present"),
            f"{source_id}.candidate_batch_present",
        ),
        "staged_receipt_path": string_value(
            evidence_paths.get("staged_receipt_path"),
            f"{source_id}.staged_receipt_path",
        ),
        "reviewed_receipt_path": string_value(
            evidence_paths.get("reviewed_receipt_path"),
            f"{source_id}.reviewed_receipt_path",
        ),
        "reviewed_receipt_present": bool_value(
            readiness.get("reviewed_receipt_present"),
            f"{source_id}.reviewed_receipt_present",
        ),
        "readiness_blockers": readiness_blockers,
        "require_env_preflight_command": string_value(
            commands.get("require_env_preflight"),
            f"{source_id}.require_env_preflight",
        ),
        "bounded_collection_run_command": string_value(
            commands.get("bounded_collection_run"),
            f"{source_id}.bounded_collection_run",
        ),
        "staged_receipt_validation_command": string_value(
            commands.get("staged_receipt_validation"),
            f"{source_id}.staged_receipt_validation",
        ),
        "reviewed_receipt_promotion_command": string_value(
            commands.get("reviewed_receipt_promotion"),
            f"{source_id}.reviewed_receipt_promotion",
        ),
        "next_action": string_value(packet.get("next_action"), f"{source_id}.next_action"),
    }


def build_report(
    queue: dict[str, Any],
    runner_readiness: dict[str, Any],
    operator_packets: dict[str, Any],
) -> dict[str, Any]:
    generated_at = queue.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("queue.generated_at must be a non-empty string")

    queue_summary = as_dict(queue.get("summary"), "queue.summary")
    readiness_summary = as_dict(runner_readiness.get("summary"), "runner_readiness.summary")
    packet_summary = as_dict(operator_packets.get("summary"), "operator_packets.summary")
    batch_contract = as_dict(operator_packets.get("batch_collection_contract"), "operator_packets.batch_collection_contract")
    batch_commands = as_dict(batch_contract.get("commands"), "operator_packets.batch_collection_contract.commands")
    post_promotion = as_dict(operator_packets.get("post_promotion_contract"), "operator_packets.post_promotion_contract")

    readiness_sources = source_map(as_list(runner_readiness.get("sources"), "runner_readiness.sources"), "readiness.sources")
    packet_sources = source_map(as_list(operator_packets.get("packets"), "operator_packets.packets"), "operator_packets.packets")
    if list(readiness_sources) != list(packet_sources):
        raise ValueError("runner readiness and operator packets must contain the same source order")

    sources = [
        build_source_plan(source_id, readiness_sources[source_id], packet_sources[source_id])
        for source_id in readiness_sources
    ]
    credential_envs = ordered_unique(
        env for source in sources for env in as_list(source.get("credential_envs"), "source.credential_envs")
    )
    reviewed_missing = count_value(readiness_summary.get("reviewed_receipts_missing"), "runner.reviewed_receipts_missing")
    candidate_missing = count_value(readiness_summary.get("candidate_batches_missing"), "runner.candidate_batches_missing")
    blocked_on_env = count_value(readiness_summary.get("blocked_on_operator_env"), "runner.blocked_on_operator_env")
    operator_sources_ready = sum(
        1
        for source in sources
        if source["candidate_batch_present"] is True and source["reviewed_receipt_present"] is not True
    )
    summary = {
        "sources": len(sources),
        "credential_gated_sources": count_value(
            readiness_summary.get("credential_gated_sources"),
            "runner.credential_gated_sources",
        ),
        "operator_sources_ready": operator_sources_ready,
        "candidate_batches_present": count_value(
            readiness_summary.get("candidate_batches_present"),
            "runner.candidate_batches_present",
        ),
        "candidate_batches_missing": candidate_missing,
        "blocked_on_operator_env": blocked_on_env,
        "reviewed_receipts_present": count_value(
            readiness_summary.get("reviewed_receipts_present"),
            "runner.reviewed_receipts_present",
        ),
        "reviewed_receipts_missing": reviewed_missing,
        "batch_ready_for_operator_credentials": candidate_missing == 0 and reviewed_missing > 0,
        "manual_review_required": bool_value(packet_summary.get("manual_review_required"), "packet.manual_review_required"),
        "manual_review_accepted": bool_value(packet_summary.get("manual_review_accepted"), "packet.manual_review_accepted"),
        "manual_review_reduction_allowed": bool_value(
            readiness_summary.get("manual_review_reduction_allowed"),
            "runner.manual_review_reduction_allowed",
        ),
        "default_ci_requires_credentials": bool_value(
            readiness_summary.get("default_ci_requires_credentials"),
            "runner.default_ci_requires_credentials",
        ),
        "checked_in_secrets_allowed": bool_value(
            readiness_summary.get("checked_in_secrets_allowed"),
            "runner.checked_in_secrets_allowed",
        ),
        "checked_in_session_output_allowed": bool_value(
            batch_contract.get("checked_in_session_output_allowed"),
            "batch.checked_in_session_output_allowed",
        ),
        "goal_closure_allowed": False,
        "session_plan_status": "pending",
        "next_action": "run_batch_collection_session_in_operator_env",
    }
    summary["session_plan_status"] = execution_status(summary)
    if reviewed_missing == 0:
        summary["next_action"] = "refresh_release_evidence_after_reviewed_receipts"
    elif candidate_missing > 0:
        summary["next_action"] = "restore_candidate_batches_before_operator_collection"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "execution_plan_ticket": 443,
        "provider": "datapan-registry",
        "inputs": {
            "collection_queue": DEFAULT_QUEUE.as_posix(),
            "runner_readiness": DEFAULT_RUNNER_READINESS.as_posix(),
            "operator_packets": DEFAULT_OPERATOR_PACKETS.as_posix(),
        },
        "summary": summary,
        "batch_execution": {
            "secret_free_batch_preflight_command": string_value(
                batch_commands.get("secret_free_batch_preflight"),
                "batch.secret_free_batch_preflight",
            ),
            "require_env_batch_preflight_command": string_value(
                batch_commands.get("require_env_batch_preflight"),
                "batch.require_env_batch_preflight",
            ),
            "batch_collection_run_command": string_value(
                batch_commands.get("batch_collection_run"),
                "batch.batch_collection_run",
            ),
            "batch_collection_run_with_session_output_command": string_value(
                batch_commands.get("batch_collection_run_with_session_output"),
                "batch.batch_collection_run_with_session_output",
            ),
            "session_output_schema": string_value(
                batch_contract.get("session_output_schema"),
                "batch.session_output_schema",
            ),
            "session_output_schema_path": string_value(
                batch_contract.get("session_output_schema_path"),
                "batch.session_output_schema_path",
            ),
            "session_output_path": string_value(
                batch_contract.get("session_output_path"),
                "batch.session_output_path",
            ),
            "session_output_validation_command": string_value(
                batch_contract.get("session_output_validation_command"),
                "batch.session_output_validation_command",
            ),
            "session_review_plan_output_path": string_value(
                batch_contract.get("session_review_plan_output_path"),
                "batch.session_review_plan_output_path",
            ),
            "session_review_plan_command": string_value(
                batch_contract.get("session_review_plan_command"),
                "batch.session_review_plan_command",
            ),
            "session_review_plan_validation_command": string_value(
                batch_contract.get("session_review_plan_validation_command"),
                "batch.session_review_plan_validation_command",
            ),
            "skip_not_ready_allowed": bool_value(batch_contract.get("skip_not_ready_allowed"), "batch.skip_not_ready_allowed"),
            "continue_on_error_allowed": bool_value(batch_contract.get("continue_on_error_allowed"), "batch.continue_on_error_allowed"),
            "checked_in_session_output_allowed": bool_value(
                batch_contract.get("checked_in_session_output_allowed"),
                "batch.checked_in_session_output_allowed",
            ),
            "checked_in_review_plan_allowed": bool_value(
                batch_contract.get("checked_in_review_plan_allowed"),
                "batch.checked_in_review_plan_allowed",
            ),
            "checked_in_secrets_allowed": bool_value(batch_contract.get("checked_in_secrets_allowed"), "batch.checked_in_secrets_allowed"),
            "default_ci_requires_credentials": bool_value(
                batch_contract.get("default_ci_requires_credentials"),
                "batch.default_ci_requires_credentials",
            ),
        },
        "operator_workflow": {
            "script": OPERATOR_WORKFLOW_SCRIPT.as_posix(),
            "plan_command": f"python3 {OPERATOR_WORKFLOW_SCRIPT.as_posix()} --json",
            "run_command": f"python3 {OPERATOR_WORKFLOW_SCRIPT.as_posix()} --run --json",
            "check_command": f"python3 {OPERATOR_WORKFLOW_SCRIPT.as_posix()} --check",
            "self_test_command": f"python3 {OPERATOR_WORKFLOW_SCRIPT.as_posix()} --self-test",
            "workflow_status": summary["session_plan_status"],
            "next_action": summary["next_action"],
            "session_output_path": string_value(
                batch_contract.get("session_output_path"),
                "batch.session_output_path",
            ),
            "session_review_plan_output_path": string_value(
                batch_contract.get("session_review_plan_output_path"),
                "batch.session_review_plan_output_path",
            ),
            "requires_explicit_run": True,
            "requires_operator_credentials": True,
            "default_ci_requires_credentials": False,
            "checked_in_session_output_allowed": False,
            "checked_in_review_plan_allowed": False,
            "checked_in_secrets_allowed": False,
            "goal_closure_allowed": False,
        },
        "operator_environment": {
            "required_credential_envs": credential_envs,
            "required_credential_env_count": len(credential_envs),
            "required_operator_inputs": [
                "credential_env_values",
                "reviewer_identity",
                "review_reason",
                "review_decision",
            ],
            "environment_mode": "operator_credentials_required",
            "checked_in_credentials_allowed": False,
        },
        "post_collection_review": {
            "session_validation_command": string_value(
                batch_contract.get("session_output_validation_command"),
                "batch.session_output_validation_command",
            ),
            "review_plan_generation_command": string_value(
                batch_contract.get("session_review_plan_command"),
                "batch.session_review_plan_command",
            ),
            "review_plan_validation_command": string_value(
                batch_contract.get("session_review_plan_validation_command"),
                "batch.session_review_plan_validation_command",
            ),
            "post_promotion_commands": [
                string_value(item, "post_promotion.commands[]")
                for item in as_list(post_promotion.get("commands"), "post_promotion.commands")
            ],
            "goal_closure_effect": string_value(
                post_promotion.get("goal_closure_effect"),
                "post_promotion.goal_closure_effect",
            ),
        },
        "sources": sources,
        "source_queue_summary": {
            "queue_status": string_value(queue_summary.get("queue_status"), "queue.summary.queue_status"),
            "reviewed_receipts_checked_in": count_value(
                queue_summary.get("reviewed_receipts_checked_in"),
                "queue.summary.reviewed_receipts_checked_in",
            ),
            "reviewed_receipts_missing": reviewed_missing,
        },
    }


def validate_invariants(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    batch = as_dict(report.get("batch_execution"), "batch_execution")
    workflow = as_dict(report.get("operator_workflow"), "operator_workflow")
    environment = as_dict(report.get("operator_environment"), "operator_environment")
    review = as_dict(report.get("post_collection_review"), "post_collection_review")
    sources = [as_dict(item, "sources[]") for item in as_list(report.get("sources"), "sources")]
    if summary.get("sources") != len(sources):
        raise ValueError("summary.sources must match sources length")
    if summary.get("credential_gated_sources") != len(sources):
        raise ValueError("execution plan must cover every credential-gated source")
    if summary.get("candidate_batches_missing") == 0 and summary.get("reviewed_receipts_missing", 0) > 0:
        if summary.get("batch_ready_for_operator_credentials") is not True:
            raise ValueError("candidate-complete missing receipts must be batch-ready for operator credentials")
    if summary.get("reviewed_receipts_missing", 0) > 0:
        if summary.get("goal_closure_allowed") is not False:
            raise ValueError("missing reviewed receipts must keep goal_closure_allowed=false")
        if summary.get("manual_review_reduction_allowed") is not False:
            raise ValueError("missing reviewed receipts must not allow manual review reduction")
    for key in ("default_ci_requires_credentials", "checked_in_secrets_allowed", "checked_in_session_output_allowed"):
        if summary.get(key) is not False:
            raise ValueError(f"summary.{key} must remain false")
    for key in ("default_ci_requires_credentials", "checked_in_secrets_allowed", "checked_in_session_output_allowed"):
        if batch.get(key) is not False:
            raise ValueError(f"batch_execution.{key} must remain false")
    if batch.get("checked_in_review_plan_allowed") is not False:
        raise ValueError("batch_execution.checked_in_review_plan_allowed must remain false")
    for key in (
        "default_ci_requires_credentials",
        "checked_in_secrets_allowed",
        "checked_in_session_output_allowed",
        "checked_in_review_plan_allowed",
        "goal_closure_allowed",
    ):
        if workflow.get(key) is not False:
            raise ValueError(f"operator_workflow.{key} must remain false")
    for key in ("requires_explicit_run", "requires_operator_credentials"):
        if workflow.get(key) is not True:
            raise ValueError(f"operator_workflow.{key} must remain true")
    if workflow.get("workflow_status") != summary.get("session_plan_status"):
        raise ValueError("operator_workflow.workflow_status must match summary.session_plan_status")
    if workflow.get("next_action") != summary.get("next_action"):
        raise ValueError("operator_workflow.next_action must match summary.next_action")
    if workflow.get("session_output_path") != batch.get("session_output_path"):
        raise ValueError("operator_workflow.session_output_path must match batch_execution")
    if workflow.get("session_review_plan_output_path") != batch.get("session_review_plan_output_path"):
        raise ValueError("operator_workflow.session_review_plan_output_path must match batch_execution")
    if environment.get("checked_in_credentials_allowed") is not False:
        raise ValueError("operator_environment.checked_in_credentials_allowed must remain false")
    if environment.get("required_credential_env_count") != len(environment.get("required_credential_envs", [])):
        raise ValueError("required_credential_env_count must match required_credential_envs length")
    if review.get("goal_closure_effect") != "no_goal_closure_without_reviewed_receipts_or_explicit_manual_review_acceptance":
        raise ValueError("post-collection review must preserve goal closure boundary")
    rendered = render_json(report).lower()
    forbidden = ["authorization:", "bearer ", "service_key=", "api_key=", "secret=", "token="]
    for marker in forbidden:
        if marker in rendered:
            raise ValueError(f"execution plan must not contain secret-like marker {marker!r}")


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
    parser.add_argument("--runner-readiness", default=DEFAULT_RUNNER_READINESS, type=pathlib.Path)
    parser.add_argument("--operator-packets", default=DEFAULT_OPERATOR_PACKETS, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in execution plan is stale")
    args = parser.parse_args()

    try:
        report = build_report(
            load_json(args.queue),
            load_json(args.runner_readiness),
            load_json(args.operator_packets),
        )
        validate_invariants(report)
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate credential runtime collection execution plan: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing credential collection execution plan", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale credential collection execution plan; "
                "run `python3 scripts/generate-credential-runtime-collection-execution-plan.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (status={report['summary']['session_plan_status']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (status={report['summary']['session_plan_status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
