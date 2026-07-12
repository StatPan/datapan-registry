#!/usr/bin/env python3
"""Generate or check the release consumer decision report."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating release consumer decisions") from exc


DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_COMPATIBILITY = pathlib.Path("reports/release-consumer-compatibility.json")
DEFAULT_FOOTPRINT = pathlib.Path("reports/release-distribution-footprint.json")
DEFAULT_MANUAL_REVIEW_ACCEPTANCE = pathlib.Path("reports/credential-runtime-manual-review-acceptance.json")
DEFAULT_GOAL_AUDIT = pathlib.Path("docs/release-ledger-goal-completion-audit.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.release-consumer-decision.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-consumer-decision.json")
SCHEMA_VERSION = "datapan.release-consumer-decision.v1"


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


def build_report(
    manifest: dict[str, Any],
    compatibility: dict[str, Any],
    footprint: dict[str, Any],
    manual_review_acceptance: dict[str, Any],
    goal_audit: dict[str, Any],
) -> dict[str, Any]:
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("manifest.generated_at must be a non-empty string")

    compatibility_summary = as_dict(compatibility.get("summary"), "compatibility.summary")
    runtime_risk = as_dict(compatibility.get("runtime_risk_evidence"), "compatibility.runtime_risk_evidence")
    footprint_summary = as_dict(footprint.get("summary"), "footprint.summary")
    acceptance_summary = as_dict(manual_review_acceptance.get("summary"), "manual_review_acceptance.summary")
    acceptance_boundary = as_dict(
        manual_review_acceptance.get("release_boundary"),
        "manual_review_acceptance.release_boundary",
    )
    goal_summary = as_dict(goal_audit.get("summary"), "goal_audit.summary")

    manual_review_required = runtime_risk.get("manual_review_required") is True
    manual_review_accepted = acceptance_summary.get("accepted") is True
    reviewed_receipts = runtime_risk.get("credential_handoff_reviewed_receipts")
    if not isinstance(reviewed_receipts, int) or reviewed_receipts < 0:
        raise ValueError("compatibility.runtime_risk_evidence.credential_handoff_reviewed_receipts must be a count")
    manual_review_reduction_allowed = runtime_risk.get("manual_review_reduction_allowed") is True
    release_decision = "safe_to_consume"
    if manual_review_required:
        release_decision = "manual_review_required"
    if compatibility_summary.get("blocked_consumers", 0) > 0 and compatibility_summary.get("proven_consumers", 0) == 0:
        release_decision = "blocked"

    completion_evidence_available = manual_review_reduction_allowed or manual_review_accepted
    goal_completion_allowed = goal_audit.get("goal_status") == "complete" and completion_evidence_available

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "decision_ticket": 395,
        "provider": "datapan-registry",
        "inputs": {
            "manifest": DEFAULT_MANIFEST.as_posix(),
            "release_consumer_compatibility": DEFAULT_COMPATIBILITY.as_posix(),
            "release_distribution_footprint": DEFAULT_FOOTPRINT.as_posix(),
            "manual_review_acceptance": DEFAULT_MANUAL_REVIEW_ACCEPTANCE.as_posix(),
            "goal_completion_audit": DEFAULT_GOAL_AUDIT.as_posix(),
            "self_artifact_excluded": True,
        },
        "summary": {
            "release_decision": release_decision,
            "canonical_registry_consumption": "compatible",
            "shard_assets_required": False,
            "manual_review_required": manual_review_required,
            "manual_review_accepted": manual_review_accepted,
            "reviewed_credential_receipts": reviewed_receipts,
            "goal_completion_allowed": goal_completion_allowed,
            "goal_status": goal_audit.get("goal_status"),
        },
        "decision_factors": {
            "canonical_registry_path": footprint_summary.get("canonical_registry_path"),
            "canonical_registry_bytes": footprint_summary.get("canonical_registry_bytes"),
            "registry_footprint_status": footprint_summary.get("registry_footprint_status"),
            "canonical_registry_required": footprint_summary.get("canonical_registry_required"),
            "monolith_fallback_required": footprint_summary.get("monolith_fallback_required"),
            "compatibility_effect": runtime_risk.get("compatibility_effect"),
            "manual_review_acceptance_status": acceptance_summary.get("acceptance_status"),
            "manual_review_boundary_accepted": acceptance_boundary.get("manual_review_release_boundary_accepted"),
            "manual_review_goal_completion_effect": acceptance_boundary.get("goal_completion_effect"),
            "credential_handoff_status": runtime_risk.get("credential_handoff_status"),
            "credential_collection_preflight_status": runtime_risk.get("credential_collection_preflight_status"),
            "credential_collection_preflight_reviewed_receipts_missing": runtime_risk.get(
                "credential_collection_preflight_reviewed_receipts_missing"
            ),
            "credential_collection_preflight_operator_environment_required_sources": runtime_risk.get(
                "credential_collection_preflight_operator_environment_required_sources"
            ),
            "credential_queue_status": runtime_risk.get("credential_queue_status"),
            "credential_handoff_relief_eligible_sources": runtime_risk.get(
                "credential_handoff_relief_eligible_sources"
            ),
            "credential_handoff_global_manual_review_relief_allowed": runtime_risk.get(
                "credential_handoff_global_manual_review_relief_allowed"
            ),
            "manual_review_reduction_allowed": manual_review_reduction_allowed,
            "goal_audit_decision": goal_summary.get("decision"),
        },
        "consumer_actions": [
            {
                "consumer": "datapan-cli",
                "action": "consume_canonical_registry",
                "reason": "Canonical registry compatibility remains required and manifest-bound.",
            },
            {
                "consumer": "release-operator",
                "action": "manual_review_before_release_adoption",
                "reason": "Runtime risk evidence still requires manual review for source runtime blockers or warnings.",
            },
            {
                "consumer": "release-operator",
                "action": "keep_shards_optional",
                "reason": "Shard assets are additive while canonical registry fallback remains required.",
            },
            {
                "consumer": "goal-operator",
                "action": "do_not_finish_goal",
                "reason": "Goal completion remains disallowed until source runtime blockers are resolved and the completion audit is complete.",
            },
        ],
    }


def validate_invariants(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    factors = as_dict(report.get("decision_factors"), "decision_factors")
    if summary.get("manual_review_required") is True and summary.get("release_decision") != "manual_review_required":
        raise ValueError("manual-review-required evidence must produce a manual_review_required release decision")
    if summary.get("manual_review_required") is True and factors.get("manual_review_boundary_accepted") is not True:
        if summary.get("goal_completion_allowed") is not False:
            raise ValueError("goal completion must stay disallowed without accepted manual-review boundary")
    if factors.get("canonical_registry_required") is not True:
        raise ValueError("release decision must preserve canonical registry requirement")
    if factors.get("monolith_fallback_required") is not True:
        raise ValueError("release decision must preserve monolith fallback")
    if summary.get("manual_review_required") is True:
        missing = factors.get("credential_collection_preflight_reviewed_receipts_missing")
        if missing != 0 and factors.get("credential_collection_preflight_operator_environment_required_sources") == 0:
            raise ValueError("manual-review-required missing-receipt decisions must expose credential operator environment needs")


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
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    parser.add_argument("--compatibility", default=DEFAULT_COMPATIBILITY, type=pathlib.Path)
    parser.add_argument("--footprint", default=DEFAULT_FOOTPRINT, type=pathlib.Path)
    parser.add_argument("--manual-review-acceptance", default=DEFAULT_MANUAL_REVIEW_ACCEPTANCE, type=pathlib.Path)
    parser.add_argument("--goal-audit", default=DEFAULT_GOAL_AUDIT, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in release consumer decision is stale")
    args = parser.parse_args()

    try:
        report = build_report(
            load_json(args.manifest),
            load_json(args.compatibility),
            load_json(args.footprint),
            load_json(args.manual_review_acceptance),
            load_json(args.goal_audit),
        )
        validate_invariants(report)
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate release consumer decision: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing release consumer decision", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale release consumer decision; "
                "run `python3 scripts/generate-release-consumer-decision.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (decision={report['summary']['release_decision']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (decision={report['summary']['release_decision']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
