#!/usr/bin/env python3
"""Guard `gira goal finish` with checked-in release goal preflight evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before running release goal finish guard") from exc


DEFAULT_PREFLIGHT = pathlib.Path("reports/release-goal-finish-preflight.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.release-goal-finish-preflight.v1.schema.json")


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def validate_schema(report: dict[str, Any], schema: dict[str, Any]) -> None:
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("; ".join(rendered))


def guard_result(report: dict[str, Any]) -> tuple[bool, list[str]]:
    summary = as_dict(report.get("summary"), "summary")
    blocking = [as_dict(item, "blocking_evidence[]") for item in as_list(report.get("blocking_evidence"), "blocking_evidence")]
    finish_allowed = summary.get("finish_allowed") is True
    next_action = summary.get("next_action")
    lines = [
        f"finish_allowed={summary.get('finish_allowed')}",
        f"next_action={next_action}",
        f"goal_status={summary.get('goal_status')}",
        f"release_decision={summary.get('release_decision')}",
        f"goal_completion_allowed={summary.get('goal_completion_allowed')}",
        f"blocking_evidence_count={summary.get('blocking_evidence_count')}",
    ]
    if finish_allowed and next_action == "goal_finish_allowed" and not blocking:
        return True, lines

    for item in blocking:
        lines.append(
            "blocking_evidence "
            f"id={item.get('id')} path={item.get('path')} finding={item.get('finding')}"
        )
    return False, lines


def run_self_test() -> None:
    blocked = {
        "summary": {
            "finish_allowed": False,
            "next_action": "do_not_finish_goal",
            "goal_status": "not_complete",
            "release_decision": "manual_review_required",
            "goal_completion_allowed": False,
            "blocking_evidence_count": 1,
        },
        "blocking_evidence": [
            {
                "id": "goal_audit_not_complete",
                "path": "docs/release-ledger-goal-completion-audit.json",
                "finding": "Goal completion audit status is not_complete.",
            }
        ],
    }
    allowed = {
        "summary": {
            "finish_allowed": True,
            "next_action": "goal_finish_allowed",
            "goal_status": "complete",
            "release_decision": "safe_to_consume",
            "goal_completion_allowed": True,
            "blocking_evidence_count": 0,
        },
        "blocking_evidence": [],
    }
    if guard_result(blocked)[0] is not False:
        raise ValueError("self-test expected blocked preflight to fail the guard")
    if guard_result(allowed)[0] is not True:
        raise ValueError("self-test expected allowed preflight to pass the guard")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", default=DEFAULT_PREFLIGHT, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--self-test", action="store_true", help="validate guard pass/fail semantics without reading repo state")
    args = parser.parse_args()

    try:
        if args.self_test:
            run_self_test()
            print("ok release goal finish guard self-test")
            return 0

        report = load_json(args.preflight)
        validate_schema(report, load_json(args.schema))
        allowed, lines = guard_result(report)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL release goal finish guard: {exc}", file=sys.stderr)
        return 1

    if allowed:
        print(f"ok release goal finish guard: {args.preflight} allows goal finish")
        for line in lines:
            print(line)
        return 0

    print(f"FAIL release goal finish guard: {args.preflight} blocks goal finish", file=sys.stderr)
    for line in lines:
        print(line, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
