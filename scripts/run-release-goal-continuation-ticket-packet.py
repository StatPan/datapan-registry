#!/usr/bin/env python3
"""Run a Gira ticket packet from release goal continuation evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import subprocess
import sys
from typing import Any


DEFAULT_QUEUE = pathlib.Path("reports/release-goal-continuation-queue.json")


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


def select_candidate(report: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    summary = as_dict(report.get("summary"), "summary")
    candidates = [as_dict(item, "candidates[]") for item in as_list(report.get("candidates"), "candidates")]
    selected_id = summary.get("primary_candidate") if candidate_id == "primary" else candidate_id
    if not isinstance(selected_id, str) or not selected_id:
        raise ValueError("selected candidate id must be a non-empty string")
    for candidate in candidates:
        if candidate.get("id") == selected_id:
            return candidate
    raise ValueError(f"candidate {selected_id!r} not found in continuation queue")


def validate_ticket_packet(candidate: dict[str, Any]) -> dict[str, Any]:
    packet = as_dict(candidate.get("ticket_packet"), "candidate.ticket_packet")
    title = packet.get("title")
    body = packet.get("body")
    if packet.get("parent_goal_issue") != 344:
        raise ValueError("ticket packet must target parent goal #344")
    if title != candidate.get("title"):
        raise ValueError("ticket packet title must match candidate title")
    if packet.get("body_input") != "stdin":
        raise ValueError("ticket packet must declare stdin body input")
    if packet.get("goal_closure_allowed") is not False:
        raise ValueError("ticket packet must not allow goal closure")
    if candidate.get("goal_closure_allowed") is not False:
        raise ValueError("candidate must not allow goal closure")
    if not isinstance(title, str) or not title:
        raise ValueError("ticket packet title must be a non-empty string")
    if not isinstance(body, str) or not body:
        raise ValueError("ticket packet body must be a non-empty string")
    if "#344" not in body or "goal_closure_allowed=false" not in body:
        raise ValueError("ticket packet body must preserve #344 and the non-closure boundary")
    return packet


def command_argv(packet: dict[str, Any], *, apply: bool) -> list[str]:
    mode = "--apply" if apply else "--dry-run"
    return ["gira", "ticket", "new", str(packet["title"]), "--body-file", "-", mode]


def plan_for_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    packet = validate_ticket_packet(candidate)
    dry_run_argv = command_argv(packet, apply=False)
    apply_argv = command_argv(packet, apply=True)
    return {
        "schema_version": "datapan.release-goal-continuation-ticket-runner.v1",
        "candidate_id": candidate.get("id"),
        "title": packet.get("title"),
        "parent_goal_issue": packet.get("parent_goal_issue"),
        "goal_closure_allowed": packet.get("goal_closure_allowed"),
        "dry_run_command": shlex.join(dry_run_argv),
        "apply_command": shlex.join(apply_argv),
        "body_input": packet.get("body_input"),
        "next_safe_action": "run_dry_run_before_apply",
    }


def run_packet(candidate: dict[str, Any], *, apply: bool) -> int:
    packet = validate_ticket_packet(candidate)
    argv = command_argv(packet, apply=apply)
    return subprocess.run(argv, input=str(packet["body"]), text=True, check=False).returncode


def fixture_report() -> dict[str, Any]:
    return {
        "summary": {"primary_candidate": "collect_reviewed_credential_runtime_receipts"},
        "candidates": [
            {
                "id": "collect_reviewed_credential_runtime_receipts",
                "title": "Collect reviewed credential runtime receipts",
                "goal_closure_allowed": False,
                "ticket_packet": {
                    "parent_goal_issue": 344,
                    "title": "Collect reviewed credential runtime receipts",
                    "body": (
                        "## Goal\nAdvance #344.\n\n"
                        "## Goal Boundary\n"
                        "goal_closure_allowed=false. Leave #344 open.\n"
                    ),
                    "body_input": "stdin",
                    "goal_closure_allowed": False,
                },
            }
        ],
    }


def run_self_test() -> None:
    candidate = select_candidate(fixture_report(), "primary")
    packet = validate_ticket_packet(candidate)
    plan = plan_for_candidate(candidate)
    if packet["title"] not in plan["dry_run_command"]:
        raise ValueError("self-test dry-run command did not include packet title")
    if "--apply" not in plan["apply_command"]:
        raise ValueError("self-test apply command missing --apply")
    if plan["goal_closure_allowed"] is not False:
        raise ValueError("self-test plan must keep goal closure disallowed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", default=DEFAULT_QUEUE, type=pathlib.Path)
    parser.add_argument("--candidate", default="primary", help="candidate id, or 'primary'")
    parser.add_argument("--body", action="store_true", help="print the selected packet body")
    parser.add_argument("--command", action="store_true", help="print the selected packet dry-run command")
    parser.add_argument("--dry-run", action="store_true", help="run gira ticket new --dry-run with packet body")
    parser.add_argument("--apply", action="store_true", help="run gira ticket new --apply with packet body")
    parser.add_argument("--json", action="store_true", help="print the selected runner plan as JSON")
    parser.add_argument("--self-test", action="store_true", help="run offline runner invariants")
    args = parser.parse_args()

    try:
        if args.self_test:
            run_self_test()
            print("ok release goal continuation ticket packet runner self-test")
            return 0
        if args.dry_run and args.apply:
            raise ValueError("choose only one of --dry-run or --apply")
        candidate = select_candidate(load_json(args.queue), args.candidate)
        packet = validate_ticket_packet(candidate)
        if args.body:
            print(str(packet["body"]), end="")
            return 0
        if args.command:
            print(shlex.join(command_argv(packet, apply=False)))
            return 0
        if args.json or not (args.dry_run or args.apply):
            print(render_json(plan_for_candidate(candidate)), end="")
            return 0
        return run_packet(candidate, apply=args.apply)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL release goal continuation ticket packet runner: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
