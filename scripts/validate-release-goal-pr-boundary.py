#!/usr/bin/env python3
"""Guard persistent release goal issues against accidental PR closing keywords."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any


DEFAULT_FINISH_PREFLIGHT = pathlib.Path("reports/release-goal-finish-preflight.json")
GOAL_ISSUE = 344
SCHEMA_VERSION = "datapan.release-goal-pr-boundary.v1"

CLOSING_KEYWORD_PATTERN = re.compile(
    r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\s+"
    r"("
    r"#344\b"
    r"|https://github\.com/StatPan/datapan-registry/issues/344\b"
    r"|StatPan/datapan-registry#344\b"
    r")",
    re.IGNORECASE,
)


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def finish_boundary(finish_preflight: dict[str, Any]) -> dict[str, bool]:
    summary = as_dict(finish_preflight.get("summary"), "finish_preflight.summary")
    return {
        "finish_allowed": summary.get("finish_allowed") is True,
        "goal_completion_allowed": summary.get("goal_completion_allowed") is True,
    }


def body_from_event(event_path: pathlib.Path) -> tuple[str, str]:
    event = as_dict(load_json(event_path), "github_event")
    if "pull_request" not in event:
        return "", "non_pull_request_event"
    pull_request = as_dict(event.get("pull_request"), "github_event.pull_request")
    body = pull_request.get("body") or ""
    if not isinstance(body, str):
        raise ValueError("github_event.pull_request.body must be a string or null")
    return body, "pull_request_event"


def find_goal_closing_references(body: str) -> list[str]:
    return [match.group(0) for match in CLOSING_KEYWORD_PATTERN.finditer(body)]


def validate_body(*, body: str, finish_preflight: dict[str, Any]) -> dict[str, Any]:
    boundary = finish_boundary(finish_preflight)
    matches = find_goal_closing_references(body)
    allowed = boundary["finish_allowed"] and boundary["goal_completion_allowed"]
    status = "allowed"
    if matches and not allowed:
        status = "blocked"
    return {
        "schema_version": SCHEMA_VERSION,
        "goal_issue": GOAL_ISSUE,
        "finish_allowed": boundary["finish_allowed"],
        "goal_completion_allowed": boundary["goal_completion_allowed"],
        "goal_closing_references": matches,
        "goal_closing_reference_count": len(matches),
        "status": status,
    }


def run_self_test() -> None:
    blocked_preflight = {
        "summary": {
            "finish_allowed": False,
            "goal_completion_allowed": False,
        }
    }
    allowed_preflight = {
        "summary": {
            "finish_allowed": True,
            "goal_completion_allowed": True,
        }
    }
    unrelated = validate_body(body="Closes #468", finish_preflight=blocked_preflight)
    if unrelated["status"] != "allowed" or unrelated["goal_closing_reference_count"] != 0:
        raise ValueError("self-test expected unrelated child closing reference to pass")
    negative_sentence = validate_body(body="This does not close #344.", finish_preflight=blocked_preflight)
    if negative_sentence["status"] != "blocked":
        raise ValueError("self-test expected #344 closing keyword to be blocked even in prose")
    fixed_url = validate_body(
        body="Fixes https://github.com/StatPan/datapan-registry/issues/344",
        finish_preflight=blocked_preflight,
    )
    if fixed_url["status"] != "blocked":
        raise ValueError("self-test expected GitHub issue URL closing keyword to be blocked")
    allowed = validate_body(body="Resolves #344", finish_preflight=allowed_preflight)
    if allowed["status"] != "allowed":
        raise ValueError("self-test expected finish-allowed boundary to pass")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finish-preflight", default=DEFAULT_FINISH_PREFLIGHT, type=pathlib.Path)
    parser.add_argument("--body-file", type=pathlib.Path)
    parser.add_argument("--event-path", type=pathlib.Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--check", action="store_true", help="validate finish preflight shape without a PR body")
    args = parser.parse_args()

    try:
        if args.self_test:
            run_self_test()
            print("ok release goal PR boundary self-test")
            return 0
        finish_preflight = as_dict(load_json(args.finish_preflight), "finish_preflight")
        if args.check:
            finish_boundary(finish_preflight)
            print("ok release goal PR boundary config")
            return 0
        if args.body_file and args.event_path:
            raise ValueError("pass only one of --body-file or --event-path")
        if args.body_file:
            body = args.body_file.read_text(encoding="utf-8")
            source = args.body_file.as_posix()
        elif args.event_path:
            body, source = body_from_event(args.event_path)
        else:
            body = ""
            source = "no_pr_body"
        if source == "non_pull_request_event":
            print("ok release goal PR boundary skipped (non-pull-request event)")
            return 0
        report = validate_body(body=body, finish_preflight=finish_preflight)
    except Exception as exc:  # noqa: BLE001 - CI needs the failed invariant
        print(f"FAIL release goal PR boundary: {exc}", file=sys.stderr)
        return 1

    if report["status"] == "blocked":
        refs = ", ".join(report["goal_closing_references"])
        print(
            "FAIL release goal PR boundary: PR body contains GitHub closing keyword(s) "
            f"for persistent goal #{GOAL_ISSUE} while finish_allowed=false: {refs}",
            file=sys.stderr,
        )
        return 1
    print("ok release goal PR boundary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
