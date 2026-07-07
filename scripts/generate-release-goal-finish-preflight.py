#!/usr/bin/env python3
"""Generate or check release goal finish preflight evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating goal finish preflight evidence") from exc


DEFAULT_GOAL_AUDIT = pathlib.Path("docs/release-ledger-goal-completion-audit.json")
DEFAULT_CONSUMER_DECISION = pathlib.Path("reports/release-consumer-decision.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.release-goal-finish-preflight.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-goal-finish-preflight.json")
SCHEMA_VERSION = "datapan.release-goal-finish-preflight.v1"


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


def build_report(goal_audit: dict[str, Any], consumer_decision: dict[str, Any]) -> dict[str, Any]:
    decision_summary = as_dict(consumer_decision.get("summary"), "consumer_decision.summary")
    audit_summary = as_dict(goal_audit.get("summary"), "goal_audit.summary")
    generated_at = consumer_decision.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("consumer_decision.generated_at must be a non-empty string")

    blocking: list[dict[str, Any]] = []
    if goal_audit.get("goal_status") != "complete":
        blocking.append(
            {
                "id": "goal_audit_not_complete",
                "path": DEFAULT_GOAL_AUDIT.as_posix(),
                "finding": "Goal completion audit status is not_complete.",
            }
        )
    if audit_summary.get("decision") != "prepare_goal_finish":
        blocking.append(
            {
                "id": "goal_audit_decision_leave_open",
                "path": DEFAULT_GOAL_AUDIT.as_posix(),
                "finding": "Goal completion audit decision does not prepare goal finish.",
            }
        )
    if decision_summary.get("goal_completion_allowed") is not True:
        blocking.append(
            {
                "id": "consumer_decision_disallows_goal_completion",
                "path": DEFAULT_CONSUMER_DECISION.as_posix(),
                "finding": "Release consumer decision records goal_completion_allowed=false.",
            }
        )
    if decision_summary.get("release_decision") != "safe_to_consume":
        blocking.append(
            {
                "id": "release_decision_not_safe_to_consume",
                "path": DEFAULT_CONSUMER_DECISION.as_posix(),
                "finding": "Release consumer decision is not safe_to_consume.",
            }
        )
    if decision_summary.get("manual_review_required") is True and decision_summary.get("manual_review_accepted") is not True:
        blocking.append(
            {
                "id": "manual_review_required_not_accepted",
                "path": DEFAULT_CONSUMER_DECISION.as_posix(),
                "finding": "Manual review is required and not accepted by checked-in release decision evidence.",
            }
        )

    reviewed_receipts = decision_summary.get("reviewed_credential_receipts")
    if not isinstance(reviewed_receipts, int) or reviewed_receipts < 0:
        raise ValueError("consumer_decision.summary.reviewed_credential_receipts must be a count")
    finish_allowed = not blocking
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "preflight_ticket": 397,
        "provider": "datapan-registry",
        "inputs": {
            "goal_completion_audit": DEFAULT_GOAL_AUDIT.as_posix(),
            "release_consumer_decision": DEFAULT_CONSUMER_DECISION.as_posix(),
        },
        "summary": {
            "finish_allowed": finish_allowed,
            "goal_status": goal_audit.get("goal_status"),
            "goal_audit_decision": audit_summary.get("decision"),
            "release_decision": decision_summary.get("release_decision"),
            "goal_completion_allowed": decision_summary.get("goal_completion_allowed"),
            "manual_review_required": decision_summary.get("manual_review_required"),
            "manual_review_accepted": decision_summary.get("manual_review_accepted"),
            "reviewed_credential_receipts": reviewed_receipts,
            "blocking_evidence_count": len(blocking),
            "next_action": "goal_finish_allowed" if finish_allowed else "do_not_finish_goal",
        },
        "blocking_evidence": blocking,
    }


def validate_invariants(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    blocking = as_list(report.get("blocking_evidence"), "blocking_evidence")
    if summary.get("finish_allowed") is True and blocking:
        raise ValueError("finish_allowed cannot be true with blocking evidence")
    if summary.get("finish_allowed") is False and not blocking:
        raise ValueError("finish_allowed=false requires blocking evidence")
    if summary.get("finish_allowed") is False and summary.get("next_action") != "do_not_finish_goal":
        raise ValueError("blocked preflight must instruct do_not_finish_goal")
    if summary.get("finish_allowed") is True and summary.get("next_action") != "goal_finish_allowed":
        raise ValueError("allowed preflight must instruct goal_finish_allowed")


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
    parser.add_argument("--consumer-decision", default=DEFAULT_CONSUMER_DECISION, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in preflight evidence is stale")
    args = parser.parse_args()

    try:
        report = build_report(load_json(args.goal_audit), load_json(args.consumer_decision))
        validate_invariants(report)
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate release goal finish preflight: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing release goal finish preflight", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale release goal finish preflight; "
                "run `python3 scripts/generate-release-goal-finish-preflight.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (finish_allowed={report['summary']['finish_allowed']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (finish_allowed={report['summary']['finish_allowed']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
