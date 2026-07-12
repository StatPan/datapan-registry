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
DEFAULT_CREDENTIAL_EXECUTION_PLAN = pathlib.Path("reports/credential-runtime-collection-execution-plan.json")
DEFAULT_OPERATIONAL_PRESSURE = pathlib.Path("reports/release-operational-pressure.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.release-goal-continuation-queue.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-goal-continuation-queue.json")
SCHEMA_VERSION = "datapan.release-goal-continuation-queue.v1"
RELEASE_EVIDENCE_REFRESH_COMMAND = "python3 scripts/refresh-release-ledger-evidence.py --write --max-iterations 5"
RELEASE_EVIDENCE_CHECK_COMMAND = "python3 scripts/refresh-release-ledger-evidence.py --check"
POST_CHILD_REFRESH_COMMANDS = [
    RELEASE_EVIDENCE_REFRESH_COMMAND,
    RELEASE_EVIDENCE_CHECK_COMMAND,
]
GIRA_TICKET_BODY_STDIN = "--body-file -"
TICKET_PACKET_RUNNER = "python3 scripts/run-release-goal-continuation-ticket-packet.py"


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


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def render_ticket_body(candidate: dict[str, Any]) -> str:
    acceptance = "\n".join(
        f"- {item}"
        for item in as_list(
            candidate.get("proposed_acceptance_criteria"),
            "candidate.proposed_acceptance_criteria",
        )
    )
    evidence_inputs = "\n".join(
        f"- {item}"
        for item in as_list(candidate.get("evidence_inputs"), "candidate.evidence_inputs")
    )
    safe_start = "\n".join(
        f"- {item}"
        for item in as_list(candidate.get("safe_start_conditions"), "candidate.safe_start_conditions")
    )
    blocked_finish = "\n".join(
        f"- {item}"
        for item in as_list(
            candidate.get("blocked_finish_conditions"),
            "candidate.blocked_finish_conditions",
        )
    )
    post_completion = "\n".join(
        f"- {item}"
        for item in as_list(candidate.get("post_completion_commands"), "candidate.post_completion_commands")
    )
    return (
        "## Goal\n"
        f"Advance #344 by executing continuation candidate `{candidate['id']}`: {candidate['title']}.\n\n"
        "## Scope\n"
        f"{candidate['rationale']} Keep the work bounded to the evidence inputs below and preserve "
        "canonical registry compatibility. This child ticket is progress evidence only and must not mark #344 complete.\n\n"
        "## Evidence Inputs\n"
        f"{evidence_inputs}\n\n"
        "## Safe Start Conditions\n"
        f"{safe_start}\n\n"
        "## Blocked Finish Conditions\n"
        f"{blocked_finish}\n\n"
        "## Acceptance Criteria\n"
        f"{acceptance}\n\n"
        "## Post Completion Commands\n"
        f"{post_completion}\n\n"
        "## Doctor Impact\n"
        "No user-facing doctor behavior change unless this child explicitly updates release verification evidence. "
        "Any workflow, status, readiness, or compatibility changes must remain reflected in checked-in release evidence.\n\n"
        "## Goal Boundary\n"
        "goal_closure_allowed=false. Leave #344 open unless repo-owned finish preflight and completion audit allow closure.\n"
        "\n## Notes\n"
        "This ticket is generated from reports/release-goal-continuation-queue.json. It is progress evidence only.\n"
    )


def attach_ticket_packet(candidate: dict[str, Any]) -> dict[str, Any]:
    title = str(candidate["title"])
    candidate_id = str(candidate["id"])
    body = render_ticket_body(candidate)
    command_prefix = f"gira ticket new {shell_quote(title)} {GIRA_TICKET_BODY_STDIN}"
    runner_prefix = f"{TICKET_PACKET_RUNNER} --candidate {candidate_id}"
    duplicate_check_command = (
        "gh issue list --repo StatPan/datapan-registry --state open "
        f"--search {shell_quote(f'{title} in:title')} --json number,title,state,url"
    )
    return {
        **candidate,
        "ticket_packet": {
            "parent_goal_issue": 344,
            "title": title,
            "goal": f"Advance #344 by executing continuation candidate {candidate_id}.",
            "body": body,
            "body_input": "stdin",
            "dry_run_command": f"{command_prefix} --dry-run",
            "apply_command": f"{command_prefix} --apply",
            "duplicate_check_command": duplicate_check_command,
            "runner_json_command": f"{runner_prefix} --json",
            "runner_body_command": f"{runner_prefix} --body",
            "runner_command_command": f"{runner_prefix} --command",
            "runner_dry_run_command": f"{runner_prefix} --dry-run",
            "runner_apply_command": f"{runner_prefix} --apply",
            "start_after_create": True,
            "goal_closure_allowed": False,
        },
    }


def candidate_receipt_collection(
    credential_preflight_summary: dict[str, Any],
    execution_plan_summary: dict[str, Any],
    handoff_summary: dict[str, Any],
) -> dict[str, Any]:
    reviewed_missing = credential_preflight_summary.get("reviewed_receipts_missing")
    operator_required = credential_preflight_summary.get("operator_environment_required_sources")
    candidate_batches = credential_preflight_summary.get("candidate_batches_present")
    return attach_ticket_packet({
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
            DEFAULT_CREDENTIAL_EXECUTION_PLAN.as_posix(),
        ],
        "safe_start_conditions": [
            f"candidate batches present: {candidate_batches}",
            f"operator environment required sources: {operator_required}",
            f"batch ready for operator credentials: {execution_plan_summary.get('batch_ready_for_operator_credentials')}",
            f"execution plan status: {execution_plan_summary.get('session_plan_status')}",
            f"execution plan next action: {execution_plan_summary.get('next_action')}",
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
    })


def candidate_manual_review_acceptance() -> dict[str, Any]:
    return attach_ticket_packet({
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
            "source runtime blockers still require manual-review release adoption",
        ],
        "proposed_acceptance_criteria": [
            "Manual-review decision intake records accepted=true with required evidence.",
            "Manual-review acceptance report validates and remains manifest-bound.",
            "Release consumer decision explains manual-review-only adoption for affected consumers.",
            "Goal audit remains not_complete unless it explicitly proves the completion contract.",
        ],
        "post_completion_commands": POST_CHILD_REFRESH_COMMANDS,
        "goal_closure_allowed": False,
        "rationale": "Explicit manual-review acceptance is the accountable release boundary when source runtime evidence still requires manual-review adoption.",
    })


def candidate_shard_preferred_compatibility(
    pressure_summary: dict[str, Any],
    pressure_distribution: dict[str, Any],
    pressure_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    required_action = next(
        (
            action
            for action in pressure_actions
            if action.get("id") == "prove_shard_preferred_consumer_compatibility"
            and action.get("required") is True
        ),
        {},
    )
    return attach_ticket_packet({
        "order": 1,
        "id": "prove_shard_preferred_consumer_compatibility",
        "title": "Prove shard-preferred consumer compatibility",
        "capability_planes": [
            "shard/release distribution",
            "consumer compatibility",
            "downstream impact",
        ],
        "evidence_inputs": [
            DEFAULT_OPERATIONAL_PRESSURE.as_posix(),
            "reports/release-distribution-footprint.json",
            "reports/release-consumer-compatibility.json",
            "reports/release-consumer-decision.json",
        ],
        "safe_start_conditions": [
            f"operational pressure decision: {pressure_summary.get('operational_pressure_decision')}",
            f"canonical registry bytes: {pressure_distribution.get('canonical_registry_bytes')}",
            f"large monolith threshold bytes: {pressure_distribution.get('large_monolith_threshold_bytes')}",
            "keep canonical registry fallback required while proving shard-preferred consumption",
            "treat shard archives as additive assets until downstream compatibility proves migration safety",
        ],
        "blocked_finish_conditions": [
            f"shard publication status: {pressure_distribution.get('shard_publication_status')}",
            f"consumer effect: {pressure_distribution.get('consumer_effect')}",
            "release consumer compatibility still records shard_assets_required=false",
            "goal completion remains blocked by credential/runtime or manual-review evidence even if shard compatibility improves",
        ],
        "proposed_acceptance_criteria": [
            "Consumer compatibility evidence proves shard-preferred install/readiness with canonical monolith fallback.",
            "Release operational pressure no longer reports an unresolved distribution-pressure next action.",
            "Release manifest, shard package evidence, and downstream impact evidence remain mutually consistent.",
            "Goal continuation queue still preserves credential/manual-review blockers until those gates are resolved.",
        ],
        "post_completion_commands": POST_CHILD_REFRESH_COMMANDS,
        "goal_closure_allowed": False,
        "rationale": required_action.get(
            "reason",
            "Large canonical registry pressure needs shard-preferred compatibility evidence without breaking canonical consumers.",
        ),
    })


def build_candidates(
    finish_summary: dict[str, Any],
    decision_summary: dict[str, Any],
    credential_preflight_summary: dict[str, Any],
    execution_plan_summary: dict[str, Any],
    handoff_summary: dict[str, Any],
    pressure_summary: dict[str, Any],
    pressure_distribution: dict[str, Any],
    pressure_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if finish_summary.get("finish_allowed") is True:
        return []

    candidates: list[dict[str, Any]] = []
    if credential_preflight_summary.get("reviewed_receipts_missing", 0) > 0:
        candidates.append(candidate_receipt_collection(credential_preflight_summary, execution_plan_summary, handoff_summary))
    if decision_summary.get("manual_review_required") is True and decision_summary.get("manual_review_accepted") is not True:
        next_order = len(candidates) + 1
        acceptance = candidate_manual_review_acceptance()
        acceptance["order"] = next_order
        candidates.append(acceptance)
    if pressure_summary.get("distribution_pressure_present") is True:
        if any(
            action.get("id") == "prove_shard_preferred_consumer_compatibility"
            and action.get("required") is True
            for action in pressure_actions
        ):
            next_order = len(candidates) + 1
            shard_candidate = candidate_shard_preferred_compatibility(
                pressure_summary,
                pressure_distribution,
                pressure_actions,
            )
            shard_candidate["order"] = next_order
            candidates.append(shard_candidate)
    return candidates


def build_report(
    goal_audit: dict[str, Any],
    finish_preflight: dict[str, Any],
    consumer_decision: dict[str, Any],
    credential_preflight: dict[str, Any],
    credential_runner_readiness: dict[str, Any],
    credential_handoff: dict[str, Any],
    credential_execution_plan: dict[str, Any],
    operational_pressure: dict[str, Any],
) -> dict[str, Any]:
    generated_at = consumer_decision.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("consumer_decision.generated_at must be a non-empty string")

    audit_summary = as_dict(goal_audit.get("summary"), "goal_audit.summary")
    finish_summary = as_dict(finish_preflight.get("summary"), "finish_preflight.summary")
    decision_summary = as_dict(consumer_decision.get("summary"), "consumer_decision.summary")
    credential_preflight_summary = as_dict(credential_preflight.get("summary"), "credential_preflight.summary")
    as_dict(credential_runner_readiness.get("summary"), "credential_runner_readiness.summary")
    execution_plan_summary = as_dict(credential_execution_plan.get("summary"), "credential_execution_plan.summary")
    handoff_summary = as_dict(credential_handoff.get("summary"), "credential_handoff.summary")
    pressure_summary = as_dict(operational_pressure.get("summary"), "operational_pressure.summary")
    pressure_distribution = as_dict(
        operational_pressure.get("distribution_pressure"),
        "operational_pressure.distribution_pressure",
    )
    pressure_actions = [
        as_dict(item, "operational_pressure.next_actions[]")
        for item in as_list(operational_pressure.get("next_actions"), "operational_pressure.next_actions")
    ]
    blocking = [
        as_dict(item, "finish_preflight.blocking_evidence[]")
        for item in as_list(finish_preflight.get("blocking_evidence"), "finish_preflight.blocking_evidence")
    ]
    candidates = build_candidates(
        finish_summary,
        decision_summary,
        credential_preflight_summary,
        execution_plan_summary,
        handoff_summary,
        pressure_summary,
        pressure_distribution,
        pressure_actions,
    )
    finish_allowed = finish_summary.get("finish_allowed") is True
    goal_closure_allowed = finish_allowed and audit_summary.get("decision") == "prepare_goal_finish"
    primary_candidate = candidates[0] if candidates else None
    active_non_completion_reason = (
        str(pressure_summary.get("operational_pressure_decision") or "finish_preflight_blocked")
        if not finish_allowed
        else "goal_finish_allowed"
    )
    next_safe_action = (
        "finish_goal"
        if finish_allowed
        else "use_existing_or_create_primary_child_ticket_after_duplicate_check"
    )
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
            "credential_collection_execution_plan": DEFAULT_CREDENTIAL_EXECUTION_PLAN.as_posix(),
            "release_operational_pressure": DEFAULT_OPERATIONAL_PRESSURE.as_posix(),
        },
        "goal_routing": {
            "parent_goal_issue": 344,
            "goal_status": goal_audit.get("goal_status"),
            "finish_allowed": finish_allowed,
            "goal_completion_allowed": decision_summary.get("goal_completion_allowed"),
            "goal_closure_allowed": goal_closure_allowed,
            "active_non_completion_reason": active_non_completion_reason,
            "primary_candidate": primary_candidate.get("id") if primary_candidate else "goal_finish_allowed",
            "primary_candidate_title": primary_candidate.get("title") if primary_candidate else "Goal finish allowed",
            "primary_candidate_duplicate_check_command": (
                as_dict(primary_candidate.get("ticket_packet"), "primary_candidate.ticket_packet").get(
                    "duplicate_check_command"
                )
                if primary_candidate
                else ""
            ),
            "next_safe_action": next_safe_action,
            "blocked_by_external_evidence": not finish_allowed
            and (
                credential_preflight_summary.get("reviewed_receipts_missing", 0) > 0
                or decision_summary.get("manual_review_required") is True
            ),
            "routing_note": (
                "Use the primary candidate packet only after duplicate detection; keep #344 open until "
                "finish preflight and completion audit allow closure."
            ),
        },
        "summary": {
            "finish_allowed": finish_allowed,
            "goal_status": goal_audit.get("goal_status"),
            "release_decision": decision_summary.get("release_decision"),
            "operational_pressure_decision": pressure_summary.get("operational_pressure_decision"),
            "distribution_pressure_present": pressure_summary.get("distribution_pressure_present"),
            "credential_pressure_present": pressure_summary.get("credential_pressure_present"),
            "goal_completion_allowed": decision_summary.get("goal_completion_allowed"),
            "reviewed_credential_receipts": decision_summary.get("reviewed_credential_receipts"),
            "reviewed_receipts_missing": credential_preflight_summary.get("reviewed_receipts_missing"),
            "manual_review_required": decision_summary.get("manual_review_required"),
            "manual_review_accepted": decision_summary.get("manual_review_accepted"),
            "candidate_count": len(candidates),
            "primary_candidate": candidates[0]["id"] if candidates else "goal_finish_allowed",
            "next_action": "goal_finish_allowed" if finish_allowed else "create_child_ticket",
            "goal_closure_allowed": goal_closure_allowed,
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
                "shard_preferred_consumer_compatibility",
            ],
            "goal_closure_effect": "refresh_may_update_evidence_but_does_not_close_goal_without_goal_finish_preflight",
        },
        "blocking_evidence": blocking,
        "candidates": candidates,
    }


def validate_invariants(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    goal_routing = as_dict(report.get("goal_routing"), "goal_routing")
    candidates = as_list(report.get("candidates"), "candidates")
    blocking = as_list(report.get("blocking_evidence"), "blocking_evidence")
    if goal_routing.get("parent_goal_issue") != 344:
        raise ValueError("goal_routing.parent_goal_issue must preserve #344")
    if goal_routing.get("goal_status") != summary.get("goal_status"):
        raise ValueError("goal_routing.goal_status must mirror summary.goal_status")
    if goal_routing.get("finish_allowed") != summary.get("finish_allowed"):
        raise ValueError("goal_routing.finish_allowed must mirror summary.finish_allowed")
    if goal_routing.get("goal_completion_allowed") != summary.get("goal_completion_allowed"):
        raise ValueError("goal_routing.goal_completion_allowed must mirror summary.goal_completion_allowed")
    if goal_routing.get("goal_closure_allowed") != summary.get("goal_closure_allowed"):
        raise ValueError("goal_routing.goal_closure_allowed must mirror summary.goal_closure_allowed")
    if goal_routing.get("primary_candidate") != summary.get("primary_candidate"):
        raise ValueError("goal_routing.primary_candidate must mirror summary.primary_candidate")
    if summary.get("goal_closure_allowed") is False and goal_routing.get("next_safe_action") == "finish_goal":
        raise ValueError("goal routing must not route to finish while closure is disallowed")
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
        if goal_routing.get("blocked_by_external_evidence") is not True:
            raise ValueError("blocked reports must expose goal_routing.blocked_by_external_evidence=true")
        duplicate_command = goal_routing.get("primary_candidate_duplicate_check_command")
        if not isinstance(duplicate_command, str) or "gh issue list" not in duplicate_command:
            raise ValueError("blocked reports must expose the primary candidate duplicate check command")
    orders = [as_dict(item, "candidate").get("order") for item in candidates]
    if orders != list(range(1, len(candidates) + 1)):
        raise ValueError("candidate.order values must be consecutive from 1")
    candidate_ids = {as_dict(item, "candidate").get("id") for item in candidates}
    if summary.get("distribution_pressure_present") is True:
        if "prove_shard_preferred_consumer_compatibility" not in candidate_ids:
            raise ValueError("distribution pressure must emit a shard-preferred compatibility candidate")
    if summary.get("credential_pressure_present") is True:
        if "collect_reviewed_credential_runtime_receipts" not in candidate_ids:
            raise ValueError("credential pressure must preserve the reviewed receipt collection candidate")
    refresh = as_dict(report.get("release_evidence_refresh"), "release_evidence_refresh")
    expected_commands = [RELEASE_EVIDENCE_REFRESH_COMMAND, RELEASE_EVIDENCE_CHECK_COMMAND]
    if refresh.get("refresh_command") != RELEASE_EVIDENCE_REFRESH_COMMAND:
        raise ValueError("release evidence refresh command must use the fixed-point refresh command")
    if refresh.get("check_command") != RELEASE_EVIDENCE_CHECK_COMMAND:
        raise ValueError("release evidence check command must use the fixed-point check command")
    for candidate in candidates:
        candidate_obj = as_dict(candidate, "candidate")
        commands = as_list(candidate_obj.get("post_completion_commands"), "candidate.post_completion_commands")
        if commands != expected_commands:
            raise ValueError("candidate post-completion commands must refresh and check release evidence")
        ticket_packet = as_dict(candidate_obj.get("ticket_packet"), "candidate.ticket_packet")
        if ticket_packet.get("parent_goal_issue") != 344:
            raise ValueError("candidate ticket packets must target parent goal #344")
        if ticket_packet.get("title") != candidate_obj.get("title"):
            raise ValueError("candidate ticket packet title must match candidate title")
        if ticket_packet.get("goal_closure_allowed") is not False:
            raise ValueError("candidate ticket packets must not allow goal closure")
        body = ticket_packet.get("body")
        if not isinstance(body, str) or "#344" not in body or "goal_closure_allowed=false" not in body:
            raise ValueError("candidate ticket packet body must preserve #344 and the non-closure boundary")
        expected_dry_run = (
            f"gira ticket new {shell_quote(str(candidate_obj['title']))} "
            f"{GIRA_TICKET_BODY_STDIN} --dry-run"
        )
        if ticket_packet.get("dry_run_command") != expected_dry_run:
            raise ValueError("candidate ticket packet dry-run command must match the candidate title")
        expected_apply = (
            f"gira ticket new {shell_quote(str(candidate_obj['title']))} "
            f"{GIRA_TICKET_BODY_STDIN} --apply"
        )
        if ticket_packet.get("apply_command") != expected_apply:
            raise ValueError("candidate ticket packet apply command must match the candidate title")
        candidate_title = str(candidate_obj["title"])
        expected_duplicate_check = (
            "gh issue list --repo StatPan/datapan-registry --state open "
            f"--search {shell_quote(f'{candidate_title} in:title')} --json number,title,state,url"
        )
        if ticket_packet.get("duplicate_check_command") != expected_duplicate_check:
            raise ValueError("candidate ticket packet duplicate check command must match the candidate title")
        expected_runner_prefix = f"{TICKET_PACKET_RUNNER} --candidate {candidate_obj['id']}"
        for key, suffix in {
            "runner_json_command": "--json",
            "runner_body_command": "--body",
            "runner_command_command": "--command",
            "runner_dry_run_command": "--dry-run",
            "runner_apply_command": "--apply",
        }.items():
            if ticket_packet.get(key) != f"{expected_runner_prefix} {suffix}":
                raise ValueError(f"candidate ticket packet {key} must target the candidate runner")


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
    parser.add_argument("--credential-execution-plan", default=DEFAULT_CREDENTIAL_EXECUTION_PLAN, type=pathlib.Path)
    parser.add_argument("--operational-pressure", default=DEFAULT_OPERATIONAL_PRESSURE, type=pathlib.Path)
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
            load_json(args.credential_execution_plan),
            load_json(args.operational_pressure),
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
