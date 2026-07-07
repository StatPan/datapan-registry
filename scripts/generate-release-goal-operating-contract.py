#!/usr/bin/env python3
"""Generate or check the release goal operating contract."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating goal operating contract") from exc


DEFAULT_GOAL_AUDIT = pathlib.Path("docs/release-ledger-goal-completion-audit.json")
DEFAULT_FINISH_PREFLIGHT = pathlib.Path("reports/release-goal-finish-preflight.json")
DEFAULT_CONTINUATION_QUEUE = pathlib.Path("reports/release-goal-continuation-queue.json")
DEFAULT_CONSUMER_DECISION = pathlib.Path("reports/release-consumer-decision.json")
DEFAULT_CREDENTIAL_POLICY = pathlib.Path("reports/credential-runtime-evidence-policy.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.release-goal-operating-contract.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-goal-operating-contract.json")
SCHEMA_VERSION = "datapan.release-goal-operating-contract.v1"


CAPABILITY_PLANES = [
    {
        "id": "source_contracts",
        "label": "Source contracts",
        "purpose": "Provider, auth, request, response, runtime, and report boundaries are explicit and verifiable.",
    },
    {
        "id": "registry_artifacts",
        "label": "Registry artifacts",
        "purpose": "Canonical registry data, schemas, reports, rollups, and manifest metadata are deterministic.",
    },
    {
        "id": "shard_release_distribution",
        "label": "Shard and release distribution",
        "purpose": "Release archives and shard packages are reproducible, manifest-bound, and compatible with canonical consumers.",
    },
    {
        "id": "verification_evidence",
        "label": "Verification evidence",
        "purpose": "Release verification, readiness, runtime evidence, health receipts, and assembly receipts explain current registry health.",
    },
    {
        "id": "error_action_routing",
        "label": "Error/action routing",
        "purpose": "Failures and required actions are classified as blocking, manual-review, follow-up, or safe no-action regions.",
    },
    {
        "id": "downstream_impact",
        "label": "Downstream impact",
        "purpose": "Release changes carry client/server action hints, manual-review boundaries, and consumer-facing impact evidence.",
    },
    {
        "id": "consumer_compatibility",
        "label": "Consumer compatibility",
        "purpose": "Documented consumers have compatibility evidence, blockers, and release gates that prevent silent breakage.",
    },
    {
        "id": "credential_safe_evidence",
        "label": "Credential-safe evidence",
        "purpose": "Credentialed runtime checks have redacted receipt contracts, reviewed intake paths, deterministic promotion, and explicit relief rules.",
    },
]


COMPLETION_CONTRACT = [
    "Release assembly has one documented, deterministic path that regenerates and checks registry, schema, report, shard, compatibility, impact, and evidence artifacts.",
    "CI gates cover the same invariants that a release operator depends on locally.",
    "No manifest-bound artifact requires hand-maintained bytes, sha256, schema URI, or derived report metadata.",
    "Manifest, schema index, source report inventory, release package, shard package, and consumer compatibility report are mutually consistent and independently checkable.",
    "Source runtime evidence and report inventories make source-level readiness auditable without listing every nested report in the top-level manifest.",
    "Downstream impact and consumer compatibility can answer whether a release is safe to consume, blocked, or needs manual review.",
    "Credentialed runtime evidence is backed by reviewed, validated, redacted live receipts or preserved behind an explicit accepted manual-review release boundary.",
    "A requirement-by-requirement completion audit proves the contract from current files, commands, CI state, and linked PR evidence.",
]


CHILD_PLANNING_RULES = [
    "Select child tickets by the weakest remaining registry-ledger capability boundary, not by ticket count.",
    "Each child ticket must be bounded, reviewable, and able to produce durable checked-in evidence.",
    "Prefer generated reports, schemas, validators, receipts, CI gates, and checked-in evidence over prose-only instructions.",
    "Preserve canonical registry compatibility unless migration, compatibility evidence, and downstream impact handling are explicit.",
    "Treat merged child PRs as progress evidence only; do not treat an exhausted child graph as goal completion.",
    "Keep the goal open while finish_allowed=false, goal_completion_allowed=false, or manual_review_required remains unaccepted.",
]


OPERATING_LOOP = [
    "Read the current goal state, completion audit, release evidence, and credential/manual-review boundary before planning new work.",
    "Identify the weakest remaining registry-ledger capability boundary in the Datapan public-data standardization ledger vision.",
    "Create or select a child ticket only when the work is bounded, reviewable, and able to produce durable checked-in evidence.",
    "Preserve canonical registry compatibility unless migration, compatibility evidence, and downstream impact handling are explicit.",
    "After each child merges, record progress evidence and re-evaluate the current non-completion boundary.",
    "Keep the goal open when checked-in evidence still reports finish_allowed=false, goal_completion_allowed=false, or manual_review_required without acceptance.",
]


ANTI_COMPLETION_RULES = [
    "Do not treat a prompt update as goal completion.",
    "Do not treat a generated report as goal completion by itself.",
    "Do not treat a green workflow as goal completion by itself.",
    "Do not treat a merged child PR as goal completion by itself.",
    "Do not treat an exhausted child graph or Gira finish_goal signal as goal completion by itself.",
    "Do not run gira goal finish unless repo-owned finish preflight and completion audit both allow it.",
]


NEXT_CHILD_SELECTION_QUESTIONS = [
    "Which registry-ledger capability boundary is weakest in the current evidence?",
    "Which checked-in artifact, schema, receipt, report, workflow, or validator will prove improvement?",
    "How does the work preserve canonical registry compatibility and downstream consumer safety?",
    "Which non-completion boundary will still prevent the full registry-ledger vision from being considered achieved?",
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


def build_report(
    goal_audit: dict[str, Any],
    finish_preflight: dict[str, Any],
    continuation_queue: dict[str, Any],
    consumer_decision: dict[str, Any],
    credential_policy: dict[str, Any],
) -> dict[str, Any]:
    generated_at = consumer_decision.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("consumer_decision.generated_at must be a non-empty string")

    audit_summary = as_dict(goal_audit.get("summary"), "goal_audit.summary")
    finish_summary = as_dict(finish_preflight.get("summary"), "finish_preflight.summary")
    continuation_summary = as_dict(continuation_queue.get("summary"), "continuation_queue.summary")
    decision_summary = as_dict(consumer_decision.get("summary"), "consumer_decision.summary")
    policy_summary = as_dict(credential_policy.get("summary"), "credential_policy.summary")
    candidates = [
        as_dict(item, "continuation_queue.candidates[]")
        for item in as_list(continuation_queue.get("candidates"), "continuation_queue.candidates")
    ]

    finish_allowed = finish_summary.get("finish_allowed") is True
    goal_completion_allowed = decision_summary.get("goal_completion_allowed") is True
    manual_review_required = decision_summary.get("manual_review_required") is True
    manual_review_accepted = decision_summary.get("manual_review_accepted") is True

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "contract_ticket": 409,
        "provider": "datapan-registry",
        "inputs": {
            "goal_completion_audit": DEFAULT_GOAL_AUDIT.as_posix(),
            "release_goal_finish_preflight": DEFAULT_FINISH_PREFLIGHT.as_posix(),
            "release_goal_continuation_queue": DEFAULT_CONTINUATION_QUEUE.as_posix(),
            "release_consumer_decision": DEFAULT_CONSUMER_DECISION.as_posix(),
            "credential_runtime_evidence_policy": DEFAULT_CREDENTIAL_POLICY.as_posix(),
        },
        "identity": {
            "goal_type": "persistent_registry_ledger_goal",
            "not_a_task_title": True,
            "not_a_child_ticket_checklist": True,
            "one_off_prompt": False,
            "vision": "Mature datapan-registry into Datapan's durable public-data standardization ledger while preserving canonical registry compatibility.",
            "north_star": (
                "Connect source contracts, registry artifacts, shard and release distribution, "
                "verification evidence, error/action routing, downstream impact, consumer compatibility, "
                "and credential-safe evidence into repeatable release operations."
            ),
        },
        "operating_summary": {
            "goal_status": goal_audit.get("goal_status"),
            "goal_audit_decision": audit_summary.get("decision"),
            "release_decision": decision_summary.get("release_decision"),
            "finish_allowed": finish_allowed,
            "goal_completion_allowed": goal_completion_allowed,
            "manual_review_required": manual_review_required,
            "manual_review_accepted": manual_review_accepted,
            "reviewed_credential_receipts": decision_summary.get("reviewed_credential_receipts"),
            "manual_review_reduction_allowed": policy_summary.get("manual_review_reduction_allowed"),
            "continuation_next_action": continuation_summary.get("next_action"),
            "primary_continuation_candidate": continuation_summary.get("primary_candidate"),
            "candidate_count": continuation_summary.get("candidate_count"),
            "goal_closure_allowed": finish_allowed and audit_summary.get("decision") == "prepare_goal_finish",
        },
        "external_lifecycle_signal_policy": {
            "gira_child_graph_finish_signal": "lifecycle_signal_only",
            "completion_proof_source": "checked_in_release_evidence_and_completion_audit",
            "finish_command_precondition": "python3 scripts/guard-release-goal-finish.py",
            "operator_rule": "Do not run gira goal finish unless repo-owned finish preflight allows completion.",
            "external_finish_signal_boundary": {
                "observed_child_graph_signal": "finish_goal_when_child_graph_exhausted",
                "signal_authority": "lifecycle_progress_only",
                "repo_finish_allowed": finish_allowed,
                "repo_goal_closure_allowed": finish_allowed and audit_summary.get("decision") == "prepare_goal_finish",
                "divergence_status": (
                    "repo_and_lifecycle_finish_aligned"
                    if finish_allowed
                    else "repo_blocks_lifecycle_finish_signal"
                ),
                "required_operator_action": (
                    "gira_goal_finish_allowed_after_guard"
                    if finish_allowed
                    else "create_child_ticket_or_collect_required_evidence"
                ),
                "guard_command": "python3 scripts/guard-release-goal-finish.py",
                "continuation_next_action": continuation_summary.get("next_action"),
                "primary_continuation_candidate": continuation_summary.get("primary_candidate"),
                "blocking_evidence_count": finish_summary.get("blocking_evidence_count"),
            },
        },
        "persistent_goal_prompt": {
            "prompt_ticket": 415,
            "prompt_mode": "persistent_goal_based_development",
            "vision_statement": (
                "Use #344 to mature datapan-registry into Datapan's durable public-data "
                "standardization ledger, not to complete a single task title."
            ),
            "north_star_question": (
                "Can a release operator and downstream consumer rebuild, verify, package, "
                "understand, and safely consume the registry from checked-in evidence without "
                "ad hoc repair, hidden credential state, or memory-only release decisions?"
            ),
            "framing": {
                "one_off_task": False,
                "task_title_only": False,
                "child_ticket_checklist_only": False,
                "prompt_update_is_completion_evidence": False,
                "child_graph_exhaustion_is_completion_evidence": False,
                "completion_requires_current_repo_evidence": True,
            },
            "operating_loop": OPERATING_LOOP,
            "next_child_selection_basis": "weakest_remaining_registry_ledger_capability_boundary",
            "next_child_selection_questions": NEXT_CHILD_SELECTION_QUESTIONS,
            "anti_completion_rules": ANTI_COMPLETION_RULES,
            "current_prompt_boundary": {
                "gira_next_action": continuation_summary.get("next_action"),
                "gira_child_graph_signal_interpretation": "lifecycle_signal_only",
                "repo_evidence_status": "not_complete" if not finish_allowed else "finish_preflight_allowed",
                "goal_closure_allowed": finish_allowed and audit_summary.get("decision") == "prepare_goal_finish",
                "required_before_goal_finish": [
                    "repo-owned finish preflight allows completion",
                    "completion audit proves the full completion contract",
                    "release consumer decision allows goal completion",
                ],
            },
        },
        "capability_planes": CAPABILITY_PLANES,
        "completion_contract": [
            {"order": index + 1, "requirement": requirement}
            for index, requirement in enumerate(COMPLETION_CONTRACT)
        ],
        "child_planning_model": {
            "selection_basis": "weakest_remaining_registry_ledger_capability_boundary",
            "rules": CHILD_PLANNING_RULES,
            "current_candidates": [
                {
                    "order": candidate.get("order"),
                    "id": candidate.get("id"),
                    "title": candidate.get("title"),
                    "capability_planes": candidate.get("capability_planes"),
                    "goal_closure_allowed": candidate.get("goal_closure_allowed"),
                    "rationale": candidate.get("rationale"),
                }
                for candidate in candidates
            ],
        },
        "current_non_completion_boundary": {
            "status": "not_complete" if not finish_allowed else "finish_preflight_allowed",
            "reasons": [
                "live credentialed runtime receipts are not checked in",
                "credentialed source runtime evidence remains manual-review only by default",
                "manual-review release boundary has not been explicitly accepted",
                "consumer compatibility still disallows goal completion",
            ],
            "allowed_paths_to_completion": [
                "reviewed_validated_redacted_live_credential_runtime_receipts",
                "explicit_accepted_manual_review_release_boundary",
            ],
        },
    }


def validate_invariants(report: dict[str, Any]) -> None:
    identity = as_dict(report.get("identity"), "identity")
    summary = as_dict(report.get("operating_summary"), "operating_summary")
    lifecycle = as_dict(report.get("external_lifecycle_signal_policy"), "external_lifecycle_signal_policy")
    non_completion = as_dict(report.get("current_non_completion_boundary"), "current_non_completion_boundary")
    prompt = as_dict(report.get("persistent_goal_prompt"), "persistent_goal_prompt")
    prompt_framing = as_dict(prompt.get("framing"), "persistent_goal_prompt.framing")
    prompt_boundary = as_dict(
        prompt.get("current_prompt_boundary"),
        "persistent_goal_prompt.current_prompt_boundary",
    )

    if identity.get("not_a_task_title") is not True:
        raise ValueError("goal contract must state that #344 is not a task title")
    if identity.get("not_a_child_ticket_checklist") is not True:
        raise ValueError("goal contract must state that #344 is not a child-ticket checklist")
    if lifecycle.get("gira_child_graph_finish_signal") != "lifecycle_signal_only":
        raise ValueError("Gira finish signal must be lifecycle_signal_only")
    finish_boundary = as_dict(
        lifecycle.get("external_finish_signal_boundary"),
        "external_lifecycle_signal_policy.external_finish_signal_boundary",
    )
    if lifecycle.get("finish_command_precondition") != "python3 scripts/guard-release-goal-finish.py":
        raise ValueError("Gira goal finish must be guarded by guard-release-goal-finish.py")
    if finish_boundary.get("guard_command") != lifecycle.get("finish_command_precondition"):
        raise ValueError("external finish boundary guard must match finish_command_precondition")
    if finish_boundary.get("repo_finish_allowed") != summary.get("finish_allowed"):
        raise ValueError("external finish boundary must mirror operating_summary.finish_allowed")
    if finish_boundary.get("repo_goal_closure_allowed") != summary.get("goal_closure_allowed"):
        raise ValueError("external finish boundary must mirror operating_summary.goal_closure_allowed")
    if prompt.get("prompt_mode") != "persistent_goal_based_development":
        raise ValueError("goal prompt must be persistent goal-based development")
    if prompt_framing.get("one_off_task") is not False:
        raise ValueError("goal prompt must reject one-off task framing")
    if prompt_framing.get("prompt_update_is_completion_evidence") is not False:
        raise ValueError("prompt updates must not be completion evidence")
    if prompt_framing.get("child_graph_exhaustion_is_completion_evidence") is not False:
        raise ValueError("child graph exhaustion must not be completion evidence")
    if prompt_framing.get("completion_requires_current_repo_evidence") is not True:
        raise ValueError("goal completion must require current repo evidence")
    if prompt.get("next_child_selection_basis") != "weakest_remaining_registry_ledger_capability_boundary":
        raise ValueError("goal prompt must select children by weakest capability boundary")
    if prompt_boundary.get("gira_child_graph_signal_interpretation") != "lifecycle_signal_only":
        raise ValueError("goal prompt must interpret Gira finish signals as lifecycle-only")
    if summary.get("finish_allowed") is False:
        if summary.get("goal_closure_allowed") is not False:
            raise ValueError("finish_allowed=false must keep goal_closure_allowed=false")
        if non_completion.get("status") != "not_complete":
            raise ValueError("blocked operating contract must preserve not_complete status")
        if prompt_boundary.get("repo_evidence_status") != "not_complete":
            raise ValueError("blocked goal prompt must preserve not_complete evidence status")
        if prompt_boundary.get("goal_closure_allowed") is not False:
            raise ValueError("blocked goal prompt must not allow goal closure")
        if finish_boundary.get("divergence_status") != "repo_blocks_lifecycle_finish_signal":
            raise ValueError("blocked operating contract must record repo_blocks_lifecycle_finish_signal")
        if finish_boundary.get("required_operator_action") != "create_child_ticket_or_collect_required_evidence":
            raise ValueError("blocked operating contract must route to continuation evidence")
        if finish_boundary.get("continuation_next_action") != "create_child_ticket":
            raise ValueError("blocked operating contract must preserve continuation_next_action=create_child_ticket")
        if finish_boundary.get("blocking_evidence_count", 0) <= 0:
            raise ValueError("blocked operating contract must preserve blocking evidence count")
    if summary.get("finish_allowed") is True:
        if finish_boundary.get("divergence_status") != "repo_and_lifecycle_finish_aligned":
            raise ValueError("finish-allowed operating contract must align lifecycle and repo finish signals")
        if finish_boundary.get("required_operator_action") != "gira_goal_finish_allowed_after_guard":
            raise ValueError("finish-allowed operating contract must allow guarded goal finish")
    if summary.get("manual_review_required") is True and summary.get("manual_review_accepted") is not True:
        if summary.get("goal_completion_allowed") is not False:
            raise ValueError("unaccepted manual-review boundary must not allow goal completion")
    if summary.get("candidate_count", 0) > 0 and summary.get("continuation_next_action") != "create_child_ticket":
        raise ValueError("continuation candidates require continuation_next_action=create_child_ticket")


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
    parser.add_argument("--continuation-queue", default=DEFAULT_CONTINUATION_QUEUE, type=pathlib.Path)
    parser.add_argument("--consumer-decision", default=DEFAULT_CONSUMER_DECISION, type=pathlib.Path)
    parser.add_argument("--credential-policy", default=DEFAULT_CREDENTIAL_POLICY, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in operating contract is stale")
    args = parser.parse_args()

    try:
        report = build_report(
            load_json(args.goal_audit),
            load_json(args.finish_preflight),
            load_json(args.continuation_queue),
            load_json(args.consumer_decision),
            load_json(args.credential_policy),
        )
        validate_invariants(report)
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate release goal operating contract: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing release goal operating contract", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale release goal operating contract; "
                "run `python3 scripts/generate-release-goal-operating-contract.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (goal_status={report['operating_summary']['goal_status']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (goal_status={report['operating_summary']['goal_status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
