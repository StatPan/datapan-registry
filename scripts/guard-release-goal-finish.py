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
DEFAULT_OPERATING_CONTRACT = pathlib.Path("reports/release-goal-operating-contract.json")
DEFAULT_OPERATING_CONTRACT_SCHEMA = pathlib.Path("schemas/datapan.release-goal-operating-contract.v1.schema.json")


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


def validate_operating_contract_alignment(preflight: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    preflight_summary = as_dict(preflight.get("summary"), "preflight.summary")
    contract_summary = as_dict(contract.get("operating_summary"), "operating_contract.operating_summary")
    lifecycle = as_dict(
        contract.get("external_lifecycle_signal_policy"),
        "operating_contract.external_lifecycle_signal_policy",
    )
    finish_boundary = as_dict(
        lifecycle.get("external_finish_signal_boundary"),
        "operating_contract.external_lifecycle_signal_policy.external_finish_signal_boundary",
    )
    preflight_finish_allowed = preflight_summary.get("finish_allowed") is True
    contract_finish_allowed = contract_summary.get("finish_allowed") is True
    lines = [
        f"operating_contract_finish_allowed={contract_summary.get('finish_allowed')}",
        f"operating_contract_goal_closure_allowed={contract_summary.get('goal_closure_allowed')}",
        f"external_finish_signal_boundary={finish_boundary.get('divergence_status')}",
        f"external_finish_required_operator_action={finish_boundary.get('required_operator_action')}",
    ]
    if lifecycle.get("gira_child_graph_finish_signal") != "lifecycle_signal_only":
        raise ValueError("operating contract must classify Gira child graph finish as lifecycle_signal_only")
    if lifecycle.get("completion_proof_source") != "checked_in_release_evidence_and_completion_audit":
        raise ValueError("operating contract must require checked-in release evidence and completion audit")
    if lifecycle.get("finish_command_precondition") != "python3 scripts/guard-release-goal-finish.py":
        raise ValueError("operating contract must name guard-release-goal-finish.py as finish precondition")
    if finish_boundary.get("guard_command") != lifecycle.get("finish_command_precondition"):
        raise ValueError("external finish boundary guard command must match lifecycle precondition")
    if contract_finish_allowed != preflight_finish_allowed:
        raise ValueError("operating contract finish_allowed does not match finish preflight")
    if finish_boundary.get("repo_finish_allowed") is not preflight_finish_allowed:
        raise ValueError("external finish boundary repo_finish_allowed does not match finish preflight")
    if finish_boundary.get("repo_goal_closure_allowed") != contract_summary.get("goal_closure_allowed"):
        raise ValueError("external finish boundary closure allowance does not match operating summary")
    if preflight_finish_allowed:
        if finish_boundary.get("divergence_status") != "repo_and_lifecycle_finish_aligned":
            raise ValueError("finish-allowed evidence must align repo and lifecycle finish signals")
        if finish_boundary.get("required_operator_action") != "gira_goal_finish_allowed_after_guard":
            raise ValueError("finish-allowed evidence must route to guarded Gira goal finish")
    else:
        if finish_boundary.get("divergence_status") != "repo_blocks_lifecycle_finish_signal":
            raise ValueError("blocked evidence must record repo_blocks_lifecycle_finish_signal")
        if finish_boundary.get("required_operator_action") != "create_child_ticket_or_collect_required_evidence":
            raise ValueError("blocked evidence must route to continuation or missing evidence collection")
        if finish_boundary.get("continuation_next_action") != "create_child_ticket":
            raise ValueError("blocked evidence must preserve continuation_next_action=create_child_ticket")
        if finish_boundary.get("blocking_evidence_count") != preflight_summary.get("blocking_evidence_count"):
            raise ValueError("external finish boundary blocking evidence count must match finish preflight")
    return lines


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
    blocked_contract = {
        "operating_summary": {
            "finish_allowed": False,
            "goal_closure_allowed": False,
        },
        "external_lifecycle_signal_policy": {
            "gira_child_graph_finish_signal": "lifecycle_signal_only",
            "completion_proof_source": "checked_in_release_evidence_and_completion_audit",
            "finish_command_precondition": "python3 scripts/guard-release-goal-finish.py",
            "external_finish_signal_boundary": {
                "repo_finish_allowed": False,
                "repo_goal_closure_allowed": False,
                "divergence_status": "repo_blocks_lifecycle_finish_signal",
                "required_operator_action": "create_child_ticket_or_collect_required_evidence",
                "guard_command": "python3 scripts/guard-release-goal-finish.py",
                "continuation_next_action": "create_child_ticket",
                "blocking_evidence_count": 1,
            },
        },
    }
    allowed_contract = {
        "operating_summary": {
            "finish_allowed": True,
            "goal_closure_allowed": True,
        },
        "external_lifecycle_signal_policy": {
            "gira_child_graph_finish_signal": "lifecycle_signal_only",
            "completion_proof_source": "checked_in_release_evidence_and_completion_audit",
            "finish_command_precondition": "python3 scripts/guard-release-goal-finish.py",
            "external_finish_signal_boundary": {
                "repo_finish_allowed": True,
                "repo_goal_closure_allowed": True,
                "divergence_status": "repo_and_lifecycle_finish_aligned",
                "required_operator_action": "gira_goal_finish_allowed_after_guard",
                "guard_command": "python3 scripts/guard-release-goal-finish.py",
                "continuation_next_action": "goal_finish_allowed",
                "blocking_evidence_count": 0,
            },
        },
    }
    if guard_result(blocked)[0] is not False:
        raise ValueError("self-test expected blocked preflight to fail the guard")
    if guard_result(allowed)[0] is not True:
        raise ValueError("self-test expected allowed preflight to pass the guard")
    validate_operating_contract_alignment(blocked, blocked_contract)
    validate_operating_contract_alignment(allowed, allowed_contract)
    mismatched_contract = dict(blocked_contract)
    mismatched_contract["operating_summary"] = {"finish_allowed": True, "goal_closure_allowed": True}
    try:
        validate_operating_contract_alignment(blocked, mismatched_contract)
    except ValueError:
        return
    raise ValueError("self-test expected preflight/operating-contract mismatch to fail")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", default=DEFAULT_PREFLIGHT, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--operating-contract", default=DEFAULT_OPERATING_CONTRACT, type=pathlib.Path)
    parser.add_argument("--operating-contract-schema", default=DEFAULT_OPERATING_CONTRACT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--self-test", action="store_true", help="validate guard pass/fail semantics without reading repo state")
    args = parser.parse_args()

    try:
        if args.self_test:
            run_self_test()
            print("ok release goal finish guard self-test")
            return 0

        report = load_json(args.preflight)
        operating_contract = load_json(args.operating_contract)
        validate_schema(report, load_json(args.schema))
        validate_schema(operating_contract, load_json(args.operating_contract_schema))
        allowed, lines = guard_result(report)
        lines.extend(validate_operating_contract_alignment(report, operating_contract))
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
