#!/usr/bin/env python3
"""Generate or check release goal continuation queue evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating goal continuation queue") from exc


DEFAULT_GOAL_AUDIT = pathlib.Path("docs/release-ledger-goal-completion-audit.json")
DEFAULT_FINISH_PREFLIGHT = pathlib.Path("reports/release-goal-finish-preflight.json")
DEFAULT_CONSUMER_DECISION = pathlib.Path("reports/release-consumer-decision.json")
DEFAULT_CREDENTIAL_COLLECTION_PREFLIGHT = pathlib.Path("reports/credential-runtime-collection-preflight.json")
DEFAULT_CREDENTIAL_RUNNER_READINESS = pathlib.Path("reports/credential-runtime-runner-readiness.json")
DEFAULT_CREDENTIAL_REVIEW_HANDOFF = pathlib.Path("reports/credential-runtime-review-handoff.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.release-goal-continuation-queue.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-goal-continuation-queue.json")
SCHEMA_VERSION = "datapan.release-goal-continuation-queue.v1"
RELEASE_EVIDENCE_REFRESH_COMMAND = "python3 scripts/refresh-release-ledger-evidence.py --write --max-iterations 5"
RELEASE_EVIDENCE_CHECK_COMMAND = "python3 scripts/refresh-release-ledger-evidence.py --check"
POST_CHILD_REFRESH_COMMANDS = [
    RELEASE_EVIDENCE_REFRESH_COMMAND,
    RELEASE_EVIDENCE_CHECK_COMMAND,
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


def candidate_receipt_collection(
    credential_preflight_summary: dict[str, Any],
    handoff_summary: dict[str, Any],
) -> dict[str, Any]:
    reviewed_missing = credential_preflight_summary.get("reviewed_receipts_missing")
    operator_required = credential_preflight_summary.get("operator_environment_required_sources")
    candidate_batches = credential_preflight_summary.get("candidate_batches_present")
    return {
        "order": 1,
        "id": "collect_reviewed_credential_runtime_receipts",
        "title": "Collect reviewed credential runtime receipts",
        "capability_planes": [
            "credential-safe evidence",
            "verification evidence",
            "consumer compatibility",
        ],
        "evidence_inputs": [
            DEFAULT_CREDENTIAL_COLLECTION_PREFLIGHT.as_posix(),
            DEFAULT_CREDENTIAL_RUNNER_READINESS.as_posix(),
            "reports/credential-runtime-receipt-collection-queue.json",
            DEFAULT_CREDENTIAL_REVIEW_HANDOFF.as_posix(),
        ],
        "safe_start_conditions": [
            f"candidate batches present: {candidate_batches}",
            f"operator environment required sources: {operator_required}",
            "run credentialed collection only from an operator environment with provider credentials",
            "do not check in credentials, hashes, headers, or secret-derived values",
        ],
        "blocked_finish_conditions": [
            f"reviewed receipts missing: {reviewed_missing}",
            f"pending review sources: {handoff_summary.get('pending_review_sources')}",
            "manual-review reduction remains disallowed until reviewed validated receipts are checked in",
        ],
        "proposed_acceptance_criteria": [
            "Staged credential runtime receipts are generated without secret material.",
            "Reviewed receipts validate with python3 scripts/validate-credential-runtime-receipts.py.",
            "Promotion uses scripts/promote-credential-runtime-receipt.py and keeps reviewed states explicit.",
            "Consumer compatibility continues to report manual-review status unless relief gates are fully satisfied.",
        ],
        "post_completion_commands": POST_CHILD_REFRESH_COMMANDS,
        "goal_closure_allowed": False,
        "rationale": "Reviewed credential runtime receipts are the strongest evidence path for reducing current manual-review compatibility gates.",
    }


def candidate_manual_review_acceptance() -> dict[str, Any]:
    return {
        "order": 2,
        "id": "assert_explicit_manual_review_release_acceptance",
        "title": "Assert explicit manual-review release acceptance",
        "capability_planes": [
            "error/action routing",
            "downstream impact",
            "consumer compatibility",
        ],
        "evidence_inputs": [
            "reports/credential-runtime-manual-review-decision.json",
            "reports/credential-runtime-manual-review-acceptance.json",
            DEFAULT_CONSUMER_DECISION.as_posix(),
            DEFAULT_GOAL_AUDIT.as_posix(),
        ],
        "safe_start_conditions": [
            "manual-review acceptance must be asserted by an accountable reviewer or release owner",
            "acceptance must state affected consumers remain manual-review only",
            "acceptance must define expiry or revalidation trigger",
            "acceptance must not include credentials, hashes, headers, or secret-derived values",
        ],
        "blocked_finish_conditions": [
            "manual_review_accepted is false in current release consumer decision",
            "goal_completion_allowed is false in current release consumer decision",
            "reviewed credential receipts are still absent",
        ],
        "proposed_acceptance_criteria": [
            "Manual-review decision intake records accepted=true with required evidence.",
            "Manual-review acceptance report validates and remains manifest-bound.",
            "Release consumer decision explains manual-review-only adoption for affected consumers.",
            "Goal audit remains not_complete unless it explicitly proves the completion contract.",
        ],
        "post_completion_commands": POST_CHILD_REFRESH_COMMANDS,
        "goal_closure_allowed": False,
        "rationale": "Explicit manual-review acceptance is the alternative release boundary when reviewed live receipt evidence is not yet available.",
    }


def build_candidates(
    finish_summary: dict[str, Any],
    decision_summary: dict[str, Any],
    credential_preflight_summary: dict[str, Any],
    handoff_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    if finish_summary.get("finish_allowed") is True:
        return []

    candidates: list[dict[str, Any]] = []
    if credential_preflight_summary.get("reviewed_receipts_missing", 0) > 0:
        candidates.append(candidate_receipt_collection(credential_preflight_summary, handoff_summary))
    if decision_summary.get("manual_review_required") is True and decision_summary.get("manual_review_accepted") is not True:
        next_order = len(candidates) + 1
        acceptance = candidate_manual_review_acceptance()
        acceptance["order"] = next_order
        candidates.append(acceptance)
    return candidates


def build_report(
    goal_audit: dict[str, Any],
    finish_preflight: dict[str, Any],
    consumer_decision: dict[str, Any],
    credential_preflight: dict[str, Any],
    credential_runner_readiness: dict[str, Any],
    credential_handoff: dict[str, Any],
) -> dict[str, Any]:
    generated_at = consumer_decision.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("consumer_decision.generated_at must be a non-empty string")

    audit_summary = as_dict(goal_audit.get("summary"), "goal_audit.summary")
    finish_summary = as_dict(finish_preflight.get("summary"), "finish_preflight.summary")
    decision_summary = as_dict(consumer_decision.get("summary"), "consumer_decision.summary")
    credential_preflight_summary = as_dict(credential_preflight.get("summary"), "credential_preflight.summary")
    as_dict(credential_runner_readiness.get("summary"), "credential_runner_readiness.summary")
    handoff_summary = as_dict(credential_handoff.get("summary"), "credential_handoff.summary")
    blocking = [
        as_dict(item, "finish_preflight.blocking_evidence[]")
        for item in as_list(finish_preflight.get("blocking_evidence"), "finish_preflight.blocking_evidence")
    ]
    candidates = build_candidates(finish_summary, decision_summary, credential_preflight_summary, handoff_summary)
    finish_allowed = finish_summary.get("finish_allowed") is True
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "continuation_ticket": 403,
        "provider": "datapan-registry",
        "inputs": {
            "goal_completion_audit": DEFAULT_GOAL_AUDIT.as_posix(),
            "release_goal_finish_preflight": DEFAULT_FINISH_PREFLIGHT.as_posix(),
            "release_consumer_decision": DEFAULT_CONSUMER_DECISION.as_posix(),
            "credential_collection_preflight": DEFAULT_CREDENTIAL_COLLECTION_PREFLIGHT.as_posix(),
            "credential_runner_readiness": DEFAULT_CREDENTIAL_RUNNER_READINESS.as_posix(),
            "credential_review_handoff": DEFAULT_CREDENTIAL_REVIEW_HANDOFF.as_posix(),
        },
        "summary": {
            "finish_allowed": finish_allowed,
            "goal_status": goal_audit.get("goal_status"),
            "release_decision": decision_summary.get("release_decision"),
            "goal_completion_allowed": decision_summary.get("goal_completion_allowed"),
            "reviewed_credential_receipts": decision_summary.get("reviewed_credential_receipts"),
            "reviewed_receipts_missing": credential_preflight_summary.get("reviewed_receipts_missing"),
            "manual_review_required": decision_summary.get("manual_review_required"),
            "manual_review_accepted": decision_summary.get("manual_review_accepted"),
            "candidate_count": len(candidates),
            "primary_candidate": candidates[0]["id"] if candidates else "goal_finish_allowed",
            "next_action": "goal_finish_allowed" if finish_allowed else "create_child_ticket",
            "goal_closure_allowed": finish_allowed and audit_summary.get("decision") == "prepare_goal_finish",
        },
        "finish_boundary": {
            "finish_guard_command": "python3 scripts/guard-release-goal-finish.py",
            "finish_preflight": DEFAULT_FINISH_PREFLIGHT.as_posix(),
            "finish_allowed": finish_allowed,
            "next_action": finish_summary.get("next_action"),
            "blocking_evidence_count": finish_summary.get("blocking_evidence_count"),
        },
        "release_evidence_refresh": {
            "refresh_command": RELEASE_EVIDENCE_REFRESH_COMMAND,
            "check_command": RELEASE_EVIDENCE_CHECK_COMMAND,
            "max_iterations": 5,
            "applies_after": [
                "reviewed_credential_receipt_collection",
                "reviewed_credential_receipt_promotion",
                "manual_review_acceptance_decision",
            ],
            "goal_closure_effect": "refresh_may_update_evidence_but_does_not_close_goal_without_goal_finish_preflight",
        },
        "blocking_evidence": blocking,
        "candidates": candidates,
    }


def validate_invariants(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    candidates = as_list(report.get("candidates"), "candidates")
    blocking = as_list(report.get("blocking_evidence"), "blocking_evidence")
    if summary.get("candidate_count") != len(candidates):
        raise ValueError("summary.candidate_count must match candidates length")
    if summary.get("finish_allowed") is True:
        if candidates:
            raise ValueError("finish-allowed goal continuation reports must not emit continuation candidates")
        if blocking:
            raise ValueError("finish-allowed goal continuation reports must not carry blocking evidence")
        if summary.get("next_action") != "goal_finish_allowed":
            raise ValueError("finish-allowed reports must set next_action=goal_finish_allowed")
    else:
        if not candidates:
            raise ValueError("blocked goal continuation reports must emit at least one next child candidate")
        if not blocking:
            raise ValueError("blocked goal continuation reports must preserve blocking evidence")
        if summary.get("next_action") != "create_child_ticket":
            raise ValueError("blocked goal continuation reports must set next_action=create_child_ticket")
        if summary.get("goal_closure_allowed") is not False:
            raise ValueError("blocked goal continuation reports must not allow goal closure")
    orders = [as_dict(item, "candidate").get("order") for item in candidates]
    if orders != list(range(1, len(candidates) + 1)):
        raise ValueError("candidate.order values must be consecutive from 1")
    refresh = as_dict(report.get("release_evidence_refresh"), "release_evidence_refresh")
    expected_commands = [RELEASE_EVIDENCE_REFRESH_COMMAND, RELEASE_EVIDENCE_CHECK_COMMAND]
    if refresh.get("refresh_command") != RELEASE_EVIDENCE_REFRESH_COMMAND:
        raise ValueError("release evidence refresh command must use the fixed-point refresh command")
    if refresh.get("check_command") != RELEASE_EVIDENCE_CHECK_COMMAND:
        raise ValueError("release evidence check command must use the fixed-point check command")
    for candidate in candidates:
        commands = as_list(as_dict(candidate, "candidate").get("post_completion_commands"), "candidate.post_completion_commands")
        if commands != expected_commands:
            raise ValueError("candidate post-completion commands must refresh and check release evidence")


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
    parser.add_argument("--goal-audit", default=DEFAULT_GOAL_AUDIT, type=pathlib.Path)
    parser.add_argument("--finish-preflight", default=DEFAULT_FINISH_PREFLIGHT, type=pathlib.Path)
    parser.add_argument("--consumer-decision", default=DEFAULT_CONSUMER_DECISION, type=pathlib.Path)
    parser.add_argument("--credential-preflight", default=DEFAULT_CREDENTIAL_COLLECTION_PREFLIGHT, type=pathlib.Path)
    parser.add_argument("--credential-runner-readiness", default=DEFAULT_CREDENTIAL_RUNNER_READINESS, type=pathlib.Path)
    parser.add_argument("--credential-handoff", default=DEFAULT_CREDENTIAL_REVIEW_HANDOFF, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in continuation queue is stale")
    args = parser.parse_args()

    try:
        report = build_report(
            load_json(args.goal_audit),
            load_json(args.finish_preflight),
            load_json(args.consumer_decision),
            load_json(args.credential_preflight),
            load_json(args.credential_runner_readiness),
            load_json(args.credential_handoff),
        )
        validate_invariants(report)
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate goal continuation queue: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing goal continuation queue", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale goal continuation queue; "
                "run `python3 scripts/generate-release-goal-continuation-queue.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (candidates={report['summary']['candidate_count']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (candidates={report['summary']['candidate_count']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
