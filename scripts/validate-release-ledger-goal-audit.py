#!/usr/bin/env python3
"""Validate the release-ledger goal completion audit."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from typing import Any


DEFAULT_AUDIT = pathlib.Path("docs/release-ledger-goal-completion-audit.json")
EXPECTED_SCHEMA_VERSION = "datapan.release-ledger-goal-completion-audit.v1"
EXPECTED_GOAL_ISSUE = 344
EXPECTED_AUDIT_TICKET = 357
EXPECTED_CRITERIA = [
    "deterministic_release_assembly_path",
    "ci_matches_local_invariants",
    "mutual_artifact_consistency",
    "source_level_readiness_auditable",
    "downstream_compatibility_connected",
    "completion_audit_exists",
]
COMPLETE_VERDICTS = {"proven", "proven_for_current_state"}
INCOMPLETE_VERDICTS = {"partial", "gap", "not_proven"}
ALL_VERDICTS = COMPLETE_VERDICTS | INCOMPLETE_VERDICTS


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def validate_existing_path(value: object, label: str) -> None:
    path = pathlib.Path(non_empty_string(value, label))
    if path.is_absolute():
        raise ValueError(f"{label} must be repo-relative: {path}")
    if not path.exists():
        raise ValueError(f"{label} references missing path: {path}")


def validate_summary(audit: dict[str, Any], criteria: list[dict[str, Any]]) -> None:
    summary = as_dict(audit.get("summary"), "summary")
    verdict_counts = {verdict: 0 for verdict in ALL_VERDICTS}
    for criterion in criteria:
        verdict = str(criterion["verdict"])
        verdict_counts[verdict] += 1

    expected_values = {
        "criteria_total": len(criteria),
        "criteria_proven": verdict_counts["proven"] + verdict_counts["proven_for_current_state"],
        "criteria_partial": verdict_counts["partial"],
        "criteria_gap": verdict_counts["gap"] + verdict_counts["not_proven"],
    }
    for key, value in expected_values.items():
        if summary.get(key) != value:
            raise ValueError(f"summary.{key} expected {value}, got {summary.get(key)}")

    decision = non_empty_string(summary.get("decision"), "summary.decision")
    if audit.get("goal_status") == "complete" and decision != "prepare_goal_finish":
        raise ValueError("complete audits must prepare the goal finish path")
    if audit.get("goal_status") != "complete" and decision != "leave_goal_open":
        raise ValueError("non-complete audits must leave the goal open")
    non_empty_string(summary.get("reason"), "summary.reason")


def validate_current_state_evidence(audit: dict[str, Any]) -> None:
    evidence = as_dict(audit.get("current_state_evidence"), "current_state_evidence")
    required_blocks = {
        "manifest",
        "schema_index",
        "release_readiness",
        "source_report_inventory",
        "source_runtime",
        "downstream_impact",
        "consumer_compatibility",
    }
    missing = sorted(required_blocks.difference(evidence))
    if missing:
        raise ValueError(f"current_state_evidence missing blocks: {', '.join(missing)}")
    for block_name in sorted(required_blocks):
        block = as_dict(evidence.get(block_name), f"current_state_evidence.{block_name}")
        validate_existing_path(block.get("path"), f"current_state_evidence.{block_name}.path")

    readiness = as_dict(evidence.get("release_readiness"), "current_state_evidence.release_readiness")
    if readiness.get("ready") is not True:
        raise ValueError("release readiness evidence must currently be ready")
    if readiness.get("failed") != 0:
        raise ValueError("release readiness evidence must have zero failed gates")

    source_runtime = as_dict(evidence.get("source_runtime"), "current_state_evidence.source_runtime")
    if not isinstance(source_runtime.get("blocking_count"), int):
        raise ValueError("source runtime blocking_count must be recorded")

    compatibility = as_dict(evidence.get("consumer_compatibility"), "current_state_evidence.consumer_compatibility")
    if source_runtime["blocking_count"] > 0 and compatibility.get("manual_review_required") is not True:
        raise ValueError("runtime blockers require consumer compatibility manual_review_required evidence")


def validate_criteria(audit: dict[str, Any]) -> list[dict[str, Any]]:
    raw_criteria = as_list(audit.get("criteria"), "criteria")
    criteria: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_criterion in enumerate(raw_criteria):
        criterion = as_dict(raw_criterion, f"criteria[{index}]")
        criterion_id = non_empty_string(criterion.get("id"), f"criteria[{index}].id")
        if criterion_id in seen_ids:
            raise ValueError(f"duplicate criterion id: {criterion_id}")
        seen_ids.add(criterion_id)

        non_empty_string(criterion.get("goal_acceptance"), f"{criterion_id}.goal_acceptance")
        verdict = non_empty_string(criterion.get("verdict"), f"{criterion_id}.verdict")
        if verdict not in ALL_VERDICTS:
            raise ValueError(f"{criterion_id}.verdict has unexpected value: {verdict}")
        if verdict in INCOMPLETE_VERDICTS:
            non_empty_string(criterion.get("gap"), f"{criterion_id}.gap")
        non_empty_string(criterion.get("next_action"), f"{criterion_id}.next_action")

        evidence = as_list(criterion.get("evidence"), f"{criterion_id}.evidence")
        if not evidence:
            raise ValueError(f"{criterion_id}.evidence must not be empty")
        for evidence_index, raw_evidence in enumerate(evidence):
            item = as_dict(raw_evidence, f"{criterion_id}.evidence[{evidence_index}]")
            validate_existing_path(item.get("path"), f"{criterion_id}.evidence[{evidence_index}].path")
            non_empty_string(item.get("finding"), f"{criterion_id}.evidence[{evidence_index}].finding")
        criteria.append(criterion)

    if list(seen_ids) and sorted(seen_ids) != sorted(EXPECTED_CRITERIA):
        missing = sorted(set(EXPECTED_CRITERIA).difference(seen_ids))
        extra = sorted(seen_ids.difference(EXPECTED_CRITERIA))
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("extra: " + ", ".join(extra))
        raise ValueError("criteria set mismatch" + (": " + "; ".join(details) if details else ""))

    ordered_ids = [str(item["id"]) for item in criteria]
    if ordered_ids != EXPECTED_CRITERIA:
        raise ValueError("criteria must follow the #344 acceptance order")
    return criteria


def validate_follow_up(audit: dict[str, Any], criteria: list[dict[str, Any]]) -> None:
    incomplete = [criterion for criterion in criteria if criterion["verdict"] in INCOMPLETE_VERDICTS]
    follow_up = as_list(audit.get("follow_up_children"), "follow_up_children")
    remaining_risks = as_list(audit.get("remaining_goal_risks", []), "remaining_goal_risks")

    if audit.get("goal_status") == "complete":
        if incomplete:
            ids = ", ".join(str(item["id"]) for item in incomplete)
            raise ValueError(f"complete audit cannot have incomplete criteria: {ids}")
        if remaining_risks:
            raise ValueError("complete audit cannot have remaining_goal_risks")
        return

    if not incomplete and not remaining_risks:
        raise ValueError("non-complete audit must identify incomplete criteria or remaining goal risks")
    if not follow_up:
        raise ValueError("non-complete audit must record follow-up child work")

    for index, raw_risk in enumerate(remaining_risks):
        risk = as_dict(raw_risk, f"remaining_goal_risks[{index}]")
        non_empty_string(risk.get("id"), f"remaining_goal_risks[{index}].id")
        validate_existing_path(risk.get("evidence"), f"remaining_goal_risks[{index}].evidence")
        non_empty_string(risk.get("finding"), f"remaining_goal_risks[{index}].finding")

    for index, raw_child in enumerate(follow_up):
        child = as_dict(raw_child, f"follow_up_children[{index}]")
        issue = child.get("issue")
        if not isinstance(issue, int) or issue <= 0:
            raise ValueError(f"follow_up_children[{index}].issue must be a positive integer")
        non_empty_string(child.get("title"), f"follow_up_children[{index}].title")
        non_empty_string(child.get("reason"), f"follow_up_children[{index}].reason")


def validate_commands(audit: dict[str, Any]) -> None:
    commands = as_list(audit.get("local_validation_commands"), "local_validation_commands")
    if not commands:
        raise ValueError("local_validation_commands must not be empty")
    required_fragments = {
        "validate-release-ledger-goal-audit.py",
        "sync-release-schema-artifacts.py --check",
        "sync-release-manifest-artifacts.py --check",
        "validate-release-ledger-ownership.py",
        "validate-release-consumer-compatibility.py",
        "package-registry-release.py --check",
    }
    normalized_commands: list[str] = []
    for index, raw_command in enumerate(commands):
        normalized_commands.append(non_empty_string(raw_command, f"local_validation_commands[{index}]"))
    missing = sorted(
        fragment
        for fragment in required_fragments
        if not any(fragment in command for command in normalized_commands)
    )
    if missing:
        raise ValueError(f"local_validation_commands missing required fragments: {', '.join(missing)}")


def validate_audit(audit: dict[str, Any]) -> tuple[int, int, str]:
    if audit.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {EXPECTED_SCHEMA_VERSION}")
    if audit.get("goal_issue") != EXPECTED_GOAL_ISSUE:
        raise ValueError(f"goal_issue must be {EXPECTED_GOAL_ISSUE}")
    if audit.get("audit_ticket") != EXPECTED_AUDIT_TICKET:
        raise ValueError(f"audit_ticket must be {EXPECTED_AUDIT_TICKET}")
    goal_status = non_empty_string(audit.get("goal_status"), "goal_status")
    if goal_status not in {"complete", "not_complete"}:
        raise ValueError("goal_status must be complete or not_complete")
    guardrail = non_empty_string(audit.get("completion_guardrail"), "completion_guardrail")
    if "not goal completion" not in guardrail:
        raise ValueError("completion_guardrail must preserve the child-ticket progress boundary")

    validate_current_state_evidence(audit)
    criteria = validate_criteria(audit)
    validate_summary(audit, criteria)
    validate_follow_up(audit, criteria)
    validate_commands(audit)

    proven = sum(1 for item in criteria if item["verdict"] in COMPLETE_VERDICTS)
    return len(criteria), proven, goal_status


def validate_freshness(audit_path: pathlib.Path) -> None:
    result = subprocess.run(
        [
            "python3",
            "scripts/generate-release-ledger-goal-completion-audit.py",
            "--audit",
            str(audit_path),
            "--check",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("goal completion audit is stale against current release evidence")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", nargs="?", default=DEFAULT_AUDIT, type=pathlib.Path)
    args = parser.parse_args()

    try:
        criteria_count, proven_count, goal_status = validate_audit(load_json(args.audit))
        validate_freshness(args.audit)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL release ledger goal audit: {exc}", file=sys.stderr)
        return 1

    print(
        f"ok release ledger goal audit "
        f"(criteria={criteria_count}, proven={proven_count}, goal_status={goal_status})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
