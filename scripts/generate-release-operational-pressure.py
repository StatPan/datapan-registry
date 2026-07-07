#!/usr/bin/env python3
"""Generate or check release operational pressure evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating release pressure evidence") from exc


DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_FOOTPRINT = pathlib.Path("reports/release-distribution-footprint.json")
DEFAULT_COMPATIBILITY = pathlib.Path("reports/release-consumer-compatibility.json")
DEFAULT_SHARD_CONSUMER_PROOF = pathlib.Path("reports/release-shard-consumer-proof.json")
DEFAULT_RUNNER_READINESS = pathlib.Path("reports/credential-runtime-runner-readiness.json")
DEFAULT_GOAL_PREFLIGHT = pathlib.Path("reports/release-goal-finish-preflight.json")
DEFAULT_OPERATING_CONTRACT = pathlib.Path("reports/release-goal-operating-contract.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.release-operational-pressure.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-operational-pressure.json")
SCHEMA_VERSION = "datapan.release-operational-pressure.v1"


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


def count(value: object, label: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def pressure_decision(distribution_pressure: bool, credential_pressure: bool, finish_allowed: bool) -> str:
    if finish_allowed and not distribution_pressure and not credential_pressure:
        return "operational_pressure_resolved"
    if distribution_pressure and credential_pressure:
        return "distribution_and_credential_pressure"
    if distribution_pressure:
        return "distribution_latency_pressure"
    if credential_pressure:
        return "credential_runtime_pressure"
    return "goal_boundary_pressure"


def build_report(
    manifest: dict[str, Any],
    footprint: dict[str, Any],
    compatibility: dict[str, Any],
    shard_proof: dict[str, Any],
    runner_readiness: dict[str, Any],
    goal_preflight: dict[str, Any],
    operating_contract: dict[str, Any],
) -> dict[str, Any]:
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("manifest.generated_at must be a non-empty string")

    footprint_summary = as_dict(footprint.get("summary"), "footprint.summary")
    shard_policy = as_dict(compatibility.get("shard_policy"), "compatibility.shard_policy")
    shard_consumer_proof = as_dict(compatibility.get("shard_consumer_proof"), "compatibility.shard_consumer_proof")
    shard_evidence = as_dict(compatibility.get("shard_release_evidence"), "compatibility.shard_release_evidence")
    proof_summary = as_dict(shard_proof.get("summary"), "shard_proof.summary")
    proof_policy = as_dict(shard_proof.get("release_policy"), "shard_proof.release_policy")
    runtime_risk = as_dict(compatibility.get("runtime_risk_evidence"), "compatibility.runtime_risk_evidence")
    runner_summary = as_dict(runner_readiness.get("summary"), "runner_readiness.summary")
    preflight_summary = as_dict(goal_preflight.get("summary"), "goal_preflight.summary")
    operating_summary = as_dict(operating_contract.get("operating_summary"), "operating_contract.operating_summary")

    registry_bytes = count(footprint_summary.get("canonical_registry_bytes"), "footprint.summary.canonical_registry_bytes")
    threshold_bytes = count(
        footprint_summary.get("large_monolith_threshold_bytes"),
        "footprint.summary.large_monolith_threshold_bytes",
    )
    reviewed_missing = count(runner_summary.get("reviewed_receipts_missing"), "runner.summary.reviewed_receipts_missing")
    blocked_on_operator_env = count(
        runner_summary.get("blocked_on_operator_env"),
        "runner.summary.blocked_on_operator_env",
    )

    distribution_action_resolved = (
        proof_summary.get("distribution_action_resolved") is True
        and shard_consumer_proof.get("distribution_action_resolved") is True
        and proof_policy.get("consumer_effect") == "shard_preferred_supported_with_canonical_fallback"
    )
    distribution_pressure = registry_bytes > threshold_bytes and not distribution_action_resolved
    credential_pressure = reviewed_missing > 0 or blocked_on_operator_env > 0
    finish_allowed = preflight_summary.get("finish_allowed") is True
    goal_completion_allowed = operating_summary.get("goal_completion_allowed") is True
    decision = pressure_decision(distribution_pressure, credential_pressure, finish_allowed)

    next_actions = [
        {
            "id": "prove_shard_preferred_consumer_compatibility",
            "capability_plane": "shard_release_distribution",
            "required": distribution_pressure and shard_policy.get("required_for_release") is not True,
            "reason": "Large canonical registry remains above threshold while shards are still optional additive assets.",
        },
        {
            "id": "collect_reviewed_credential_runtime_receipts",
            "capability_plane": "credential_safe_evidence",
            "required": credential_pressure,
            "reason": "Credential-gated runtime checks still need reviewed redacted receipts before compatibility relief.",
        },
        {
            "id": "do_not_finish_goal",
            "capability_plane": "verification_evidence",
            "required": not finish_allowed or not goal_completion_allowed,
            "reason": "Repo-owned preflight and operating contract still disallow #344 closure.",
        },
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "pressure_ticket": 437,
        "provider": "datapan-registry",
        "inputs": {
            "manifest": DEFAULT_MANIFEST.as_posix(),
            "release_distribution_footprint": DEFAULT_FOOTPRINT.as_posix(),
            "release_consumer_compatibility": DEFAULT_COMPATIBILITY.as_posix(),
            "release_shard_consumer_proof": DEFAULT_SHARD_CONSUMER_PROOF.as_posix(),
            "credential_runtime_runner_readiness": DEFAULT_RUNNER_READINESS.as_posix(),
            "release_goal_finish_preflight": DEFAULT_GOAL_PREFLIGHT.as_posix(),
            "release_goal_operating_contract": DEFAULT_OPERATING_CONTRACT.as_posix(),
        },
        "summary": {
            "operational_pressure_decision": decision,
            "distribution_pressure_present": distribution_pressure,
            "credential_pressure_present": credential_pressure,
            "finish_allowed": finish_allowed,
            "goal_completion_allowed": goal_completion_allowed,
            "goal_closure_allowed": operating_summary.get("goal_closure_allowed") is True,
            "next_action_count": sum(1 for action in next_actions if action["required"]),
        },
        "distribution_pressure": {
            "canonical_registry_path": footprint_summary.get("canonical_registry_path"),
            "canonical_registry_bytes": registry_bytes,
            "large_monolith_threshold_bytes": threshold_bytes,
            "registry_footprint_status": footprint_summary.get("registry_footprint_status"),
            "canonical_registry_required": footprint_summary.get("canonical_registry_required"),
            "monolith_fallback_required": footprint_summary.get("monolith_fallback_required"),
            "shard_distribution_required": footprint_summary.get("shard_distribution_required"),
            "shard_publication_status": shard_policy.get("publication_status"),
            "shard_policy_phase": shard_policy.get("phase"),
            "shard_archive_status": shard_evidence.get("status"),
            "consumer_effect": shard_evidence.get("footprint_consumer_effect"),
            "shard_consumer_proof": DEFAULT_SHARD_CONSUMER_PROOF.as_posix(),
            "shard_preferred_ready": proof_summary.get("shard_preferred_ready"),
            "distribution_action_resolved": distribution_action_resolved,
            "proof_consumer_effect": proof_policy.get("consumer_effect"),
        },
        "credential_pressure": {
            "runner_status": runner_summary.get("runner_status"),
            "ready_to_run_without_credentials": runner_summary.get("ready_to_run_without_credentials"),
            "blocked_on_operator_env": blocked_on_operator_env,
            "blocked_on_candidate_batch": runner_summary.get("blocked_on_candidate_batch"),
            "reviewed_receipts_present": runner_summary.get("reviewed_receipts_present"),
            "reviewed_receipts_missing": reviewed_missing,
            "default_ci_requires_credentials": runner_summary.get("default_ci_requires_credentials"),
            "manual_review_reduction_allowed": runner_summary.get("manual_review_reduction_allowed"),
            "checked_in_secrets_allowed": runner_summary.get("checked_in_secrets_allowed"),
            "runtime_compatibility_effect": runtime_risk.get("compatibility_effect"),
            "credential_queue_status": runtime_risk.get("credential_queue_status"),
        },
        "goal_boundary": {
            "goal_status": operating_summary.get("goal_status"),
            "release_decision": operating_summary.get("release_decision"),
            "manual_review_required": operating_summary.get("manual_review_required"),
            "manual_review_accepted": operating_summary.get("manual_review_accepted"),
            "reviewed_credential_receipts": operating_summary.get("reviewed_credential_receipts"),
            "preflight_next_action": preflight_summary.get("next_action"),
            "primary_continuation_candidate": operating_summary.get("primary_continuation_candidate"),
        },
        "next_actions": next_actions,
    }


def validate_invariants(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    distribution = as_dict(report.get("distribution_pressure"), "distribution_pressure")
    credential = as_dict(report.get("credential_pressure"), "credential_pressure")
    boundary = as_dict(report.get("goal_boundary"), "goal_boundary")

    if summary.get("finish_allowed") is False and summary.get("goal_closure_allowed") is not False:
        raise ValueError("finish_allowed=false must keep goal_closure_allowed=false")
    if summary.get("goal_completion_allowed") is False:
        if summary.get("operational_pressure_decision") == "operational_pressure_resolved":
            raise ValueError("goal_completion_allowed=false cannot report resolved operational pressure")
    if summary.get("distribution_pressure_present") is True:
        if distribution.get("canonical_registry_required") is not True:
            raise ValueError("distribution pressure must preserve canonical registry requirement")
        if distribution.get("monolith_fallback_required") is not True:
            raise ValueError("distribution pressure must preserve monolith fallback")
        if distribution.get("shard_distribution_required") is not False:
            raise ValueError("distribution pressure must keep shard distribution additive until migration is proven")
    if distribution.get("distribution_action_resolved") is True:
        if distribution.get("proof_consumer_effect") != "shard_preferred_supported_with_canonical_fallback":
            raise ValueError("resolved distribution action must cite shard-preferred fallback proof")
        if distribution.get("shard_distribution_required") is not False:
            raise ValueError("resolved distribution action must still keep shard distribution optional")
    if summary.get("credential_pressure_present") is True:
        if credential.get("reviewed_receipts_missing", 0) <= 0:
            raise ValueError("credential pressure must expose missing reviewed receipts")
        if credential.get("checked_in_secrets_allowed") is not False:
            raise ValueError("credential pressure must not allow checked-in secrets")
        if credential.get("manual_review_reduction_allowed") is not False:
            raise ValueError("credential pressure must not allow manual review reduction without receipts")
    if boundary.get("manual_review_required") is True and boundary.get("manual_review_accepted") is not True:
        if summary.get("goal_completion_allowed") is not False:
            raise ValueError("unaccepted manual review must keep goal completion disallowed")
    required_actions = [action for action in report.get("next_actions", []) if isinstance(action, dict) and action.get("required")]
    if summary.get("next_action_count") != len(required_actions):
        raise ValueError("summary.next_action_count must match required next_actions")
    if not required_actions and summary.get("operational_pressure_decision") != "operational_pressure_resolved":
        raise ValueError("unresolved operational pressure must have at least one required next action")


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
    parser.add_argument("--footprint", default=DEFAULT_FOOTPRINT, type=pathlib.Path)
    parser.add_argument("--compatibility", default=DEFAULT_COMPATIBILITY, type=pathlib.Path)
    parser.add_argument("--shard-consumer-proof", default=DEFAULT_SHARD_CONSUMER_PROOF, type=pathlib.Path)
    parser.add_argument("--runner-readiness", default=DEFAULT_RUNNER_READINESS, type=pathlib.Path)
    parser.add_argument("--goal-preflight", default=DEFAULT_GOAL_PREFLIGHT, type=pathlib.Path)
    parser.add_argument("--operating-contract", default=DEFAULT_OPERATING_CONTRACT, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in pressure evidence is stale")
    args = parser.parse_args()

    try:
        report = build_report(
            load_json(args.manifest),
            load_json(args.footprint),
            load_json(args.compatibility),
            load_json(args.shard_consumer_proof),
            load_json(args.runner_readiness),
            load_json(args.goal_preflight),
            load_json(args.operating_contract),
        )
        validate_invariants(report)
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate release operational pressure: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing release operational pressure", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale release operational pressure; "
                "run `python3 scripts/generate-release-operational-pressure.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (decision={report['summary']['operational_pressure_decision']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (decision={report['summary']['operational_pressure_decision']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
