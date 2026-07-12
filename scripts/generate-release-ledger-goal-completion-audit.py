#!/usr/bin/env python3
"""Refresh the mutable evidence portion of the #344 completion audit."""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys
from typing import Any


DEFAULT_AUDIT = pathlib.Path("docs/release-ledger-goal-completion-audit.json")
DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_SCHEMA_INDEX = pathlib.Path("schemas/index.json")
DEFAULT_READINESS = pathlib.Path("reports/latest-release-readiness.json")
DEFAULT_INVENTORY = pathlib.Path("reports/source-report-inventory.json")
DEFAULT_RUNTIME = pathlib.Path("reports/source-runtime-evidence-rollup.json")
DEFAULT_REMEDIATION = pathlib.Path("reports/source-runtime-remediation-map.json")
DEFAULT_IMPACT = pathlib.Path("reports/registry-impact-plan.json")
DEFAULT_COMPATIBILITY = pathlib.Path("reports/release-consumer-compatibility.json")
DEFAULT_FOOTPRINT = pathlib.Path("reports/release-distribution-footprint.json")
DEFAULT_POLICY = pathlib.Path("reports/credential-runtime-evidence-policy.json")
DEFAULT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_HANDOFF = pathlib.Path("reports/credential-runtime-review-handoff.json")
DEFAULT_MANUAL_REVIEW = pathlib.Path("reports/credential-runtime-manual-review-acceptance.json")
DEFAULT_CONSUMER_DECISION = pathlib.Path("reports/release-consumer-decision.json")


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


def summary_block(path: pathlib.Path, report: dict[str, Any]) -> dict[str, Any]:
    return {"path": path.as_posix(), **as_dict(report.get("summary"), f"{path}.summary")}


def build_current_state_evidence() -> dict[str, Any]:
    manifest = load_json(DEFAULT_MANIFEST)
    schema_index = load_json(DEFAULT_SCHEMA_INDEX)
    readiness = load_json(DEFAULT_READINESS)
    inventory = load_json(DEFAULT_INVENTORY)
    runtime = load_json(DEFAULT_RUNTIME)
    remediation = load_json(DEFAULT_REMEDIATION)
    impact = load_json(DEFAULT_IMPACT)
    compatibility = load_json(DEFAULT_COMPATIBILITY)
    footprint = load_json(DEFAULT_FOOTPRINT)
    policy = load_json(DEFAULT_POLICY)
    queue = load_json(DEFAULT_QUEUE)
    handoff = load_json(DEFAULT_HANDOFF)
    manual_review = load_json(DEFAULT_MANUAL_REVIEW)
    consumer_decision = load_json(DEFAULT_CONSUMER_DECISION)

    artifacts = manifest.get("artifacts")
    schemas = schema_index.get("schemas")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be an array")
    if not isinstance(schemas, list):
        raise ValueError("schemas/index.json schemas must be an array")

    runtime_risk = as_dict(
        compatibility.get("runtime_risk_evidence"),
        "release-consumer-compatibility.runtime_risk_evidence",
    )
    readiness_block = summary_block(DEFAULT_READINESS, readiness)
    readiness_block["ready"] = readiness.get("ready")
    compatibility_block = summary_block(DEFAULT_COMPATIBILITY, compatibility)
    compatibility_block["manual_review_required"] = runtime_risk.get("manual_review_required")
    compatibility_block["runtime_blocking_count"] = runtime_risk.get("blocking_count")
    compatibility_block["runtime_warning_count"] = runtime_risk.get("warning_count")

    return {
        "manifest": {
            "path": DEFAULT_MANIFEST.as_posix(),
            "artifact_count": len(artifacts),
        },
        "schema_index": {
            "path": DEFAULT_SCHEMA_INDEX.as_posix(),
            "schema_count": len(schemas),
        },
        "release_readiness": readiness_block,
        "source_report_inventory": summary_block(DEFAULT_INVENTORY, inventory),
        "source_runtime": summary_block(DEFAULT_RUNTIME, runtime),
        "source_runtime_remediation": summary_block(DEFAULT_REMEDIATION, remediation),
        "downstream_impact": summary_block(DEFAULT_IMPACT, impact),
        "consumer_compatibility": compatibility_block,
        "release_distribution_footprint": summary_block(DEFAULT_FOOTPRINT, footprint),
        "credential_runtime_policy": summary_block(DEFAULT_POLICY, policy),
        "credential_runtime_receipt_collection_queue": summary_block(DEFAULT_QUEUE, queue),
        "credential_runtime_review_handoff": summary_block(DEFAULT_HANDOFF, handoff),
        "manual_review_acceptance": summary_block(DEFAULT_MANUAL_REVIEW, manual_review),
        "release_consumer_decision": summary_block(DEFAULT_CONSUMER_DECISION, consumer_decision),
    }


def build_audit(template: dict[str, Any]) -> dict[str, Any]:
    audit = copy.deepcopy(template)
    evidence = build_current_state_evidence()
    decision = as_dict(evidence["release_consumer_decision"], "release_consumer_decision")
    runtime = as_dict(evidence["source_runtime"], "source_runtime")
    remediation = as_dict(evidence["source_runtime_remediation"], "source_runtime_remediation")
    compatibility = as_dict(evidence["consumer_compatibility"], "consumer_compatibility")
    policy = as_dict(evidence["credential_runtime_policy"], "credential_runtime_policy")
    queue = as_dict(
        evidence["credential_runtime_receipt_collection_queue"],
        "credential_runtime_receipt_collection_queue",
    )
    handoff = as_dict(evidence["credential_runtime_review_handoff"], "credential_runtime_review_handoff")

    generated_at = load_json(DEFAULT_CONSUMER_DECISION).get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("release consumer decision generated_at must be a non-empty string")

    audit["audited_at"] = generated_at
    audit["current_state_evidence"] = evidence
    sources_without_evidence = runtime.get("sources_without_evidence")
    runtime_boundary = f"{runtime.get('blocking_count')} blockers"
    if sources_without_evidence:
        runtime_boundary += f" across {sources_without_evidence} sources without runtime evidence"

    audit["summary"]["reason"] = (
        "The deterministic release path and its checks are proven for the current state, "
        "but #344 remains open because source runtime evidence has "
        f"{runtime_boundary}, "
        "consumer adoption remains manual-review-required, and no accountable "
        "manual-review release acceptance is asserted. Credential receipt collection is "
        f"relief-ready ({queue.get('reviewed_receipts_checked_in')} reviewed receipts; "
        f"handoff={handoff.get('handoff_status')}); it is no longer the active boundary."
    )
    remaining_goal_risks = [
        {
            "id": "source_runtime_manual_review_required",
            "evidence": DEFAULT_REMEDIATION.as_posix(),
            "finding": (
                f"The remediation map records {remediation.get('blocking_count')} runtime blockers "
                "and keeps consumer adoption manual-review-required until they are resolved."
            ),
        },
        {
            "id": "manual_review_acceptance_not_asserted",
            "evidence": DEFAULT_CONSUMER_DECISION.as_posix(),
            "finding": (
                "The release decision remains manual_review_required and manual-review acceptance "
                "is not asserted, so goal completion is not allowed."
            ),
        },
    ]
    if sources_without_evidence:
        remaining_goal_risks.insert(
            1,
            {
                "id": "runtime_evidence_coverage_gap",
                "evidence": DEFAULT_RUNTIME.as_posix(),
                "finding": (
                    f"{sources_without_evidence} source(s) still lack runtime evidence; "
                    "metadata-only and non-data runtime boundaries remain explicit release risk."
                ),
            },
        )
    audit["remaining_goal_risks"] = remaining_goal_risks
    commands = audit.get("local_validation_commands")
    if isinstance(commands, list) and not any(
        "generate-release-ledger-goal-completion-audit.py --check" in str(command)
        for command in commands
    ):
        commands.insert(
            1,
            "python3 scripts/generate-release-ledger-goal-completion-audit.py --check",
        )

    for criterion in audit.get("criteria", []):
        if criterion.get("id") == "completion_audit_exists":
            criterion["next_action"] = (
                "Keep this audit generated from current release evidence; leave #344 open until "
                "source-runtime risk is resolved or an accountable manual-review decision is accepted."
            )
        for item in criterion.get("evidence", []):
            if item.get("path") == DEFAULT_REMEDIATION.as_posix():
                item["finding"] = (
                    f"Maps {remediation.get('blocking_count')} runtime blockers and "
                    f"{remediation.get('receipt_linked_findings')} receipt-linked findings; "
                    "all linked receipts are relief-eligible, but runtime risk still requires "
                    "manual-review adoption."
                )
            elif item.get("path") == DEFAULT_COMPATIBILITY.as_posix():
                item["finding"] = (
                    f"Reports {compatibility.get('consumer_count')} consumers, "
                    f"{compatibility.get('proven_consumers')} proven consumers, and "
                    f"{compatibility.get('blocked_consumers')} blocked consumers with canonical "
                    "registry compatibility, optional shard assets, and runtime risk evidence "
                    "that still requires manual review."
                )
            elif item.get("path") == DEFAULT_POLICY.as_posix():
                item["finding"] = (
                    f"Derives {policy.get('reviewed_receipts_checked_in')} reviewed, validated, "
                    "relief-eligible credential receipts from the checked-in intake path while "
                    "keeping unresolved runtime risk explicit."
                )
            elif item.get("path") == DEFAULT_QUEUE.as_posix():
                item["finding"] = (
                    f"Records a {queue.get('queue_status')} receipt queue with "
                    f"{queue.get('absent')} absent and {queue.get('reviewed_rejected')} rejected "
                    "reviewed receipts."
                )
            elif item.get("path") == DEFAULT_HANDOFF.as_posix():
                item["finding"] = (
                    f"Records {handoff.get('handoff_status')} review handoff status with "
                    f"{handoff.get('pending_review_sources')} pending sources and "
                    f"{handoff.get('relief_eligible_sources')} relief-eligible reviewed receipts."
                )
            elif item.get("path") == "scripts/validate-release-ledger-goal-audit.py":
                item["finding"] = (
                    "Validates audit structure, completion boundaries, and freshness against "
                    "current release evidence."
                )
    return audit


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", default=DEFAULT_AUDIT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when the audit is stale")
    args = parser.parse_args()

    try:
        rendered = render_json(build_audit(load_json(args.audit)))
        if args.check:
            current = args.audit.read_text(encoding="utf-8")
            if current != rendered:
                raise ValueError(
                    f"{args.audit} is stale; run `python3 scripts/generate-release-ledger-goal-completion-audit.py`"
                )
        else:
            args.audit.write_text(rendered, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL release-ledger goal completion audit: {exc}", file=sys.stderr)
        return 1

    print(f"ok release-ledger goal completion audit ({'checked' if args.check else 'refreshed'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
