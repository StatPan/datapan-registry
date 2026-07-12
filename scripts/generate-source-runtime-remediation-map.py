#!/usr/bin/env python3
"""Generate or check the source runtime remediation map."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any

import credential_runtime_receipts as receipts

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating source runtime remediation maps") from exc


DEFAULT_RUNTIME_ROLLUP = pathlib.Path("reports/source-runtime-evidence-rollup.json")
DEFAULT_ROUTING = pathlib.Path("reports/error-action-routing-rollup.json")
DEFAULT_IMPACT = pathlib.Path("reports/registry-impact-plan.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.source-runtime-remediation-map.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/source-runtime-remediation-map.json")
DEFAULT_RECEIPT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-receipt.v1.schema.json")
SCHEMA_VERSION = "datapan.source-runtime-remediation-map.v1"
FOLLOW_UP_ISSUE = 362
REVIEWED_RECEIPT_GLOB = "reports/credential-runtime-receipts/*-credentialed-receipt.json"
RECEIPT_LINKED_FINDING_IDS = {
    "credential_required",
    "metadata_only_verification",
    "non_data_runtime_evidence_not_collected",
}


REMEDIATION_RULES: dict[str, dict[str, Any]] = {
    "adapter_not_registered": {
        "status": "follow_up_required",
        "action": "register_runtime_adapter",
        "owner": "provider-adapter",
        "follow_up_issue": FOLLOW_UP_ISSUE,
        "release_boundary": "manual_review_required",
    },
    "source_runtime_adapter_not_registered": {
        "status": "follow_up_required",
        "action": "register_runtime_adapter",
        "owner": "provider-adapter",
        "follow_up_issue": FOLLOW_UP_ISSUE,
        "release_boundary": "manual_review_required",
    },
    "credential_required": {
        "status": "manual_review_boundary",
        "action": "use_credential_runtime_evidence_policy_or_accept_manual_review_boundary",
        "owner": "release-operator",
        "release_boundary": "credentialed_runtime_evidence_not_required_for_canonical_registry_release",
    },
    "metadata_only_verification": {
        "status": "manual_review_boundary",
        "action": "collect_live_runtime_evidence_through_credential_safe_policy",
        "owner": "source-evidence",
        "release_boundary": "metadata_only_source_remains_manual_review",
    },
    "non_data_runtime_evidence_not_collected": {
        "status": "manual_review_boundary",
        "action": "collect_non_data_runtime_evidence_through_credential_safe_policy",
        "owner": "source-evidence",
        "release_boundary": "non_data_runtime_evidence_gap_documented",
    },
    "source_specific_error_taxonomy_pending": {
        "status": "follow_up_required",
        "action": "update_source_specific_error_taxonomy",
        "owner": "error-action-routing",
        "follow_up_issue": FOLLOW_UP_ISSUE,
        "release_boundary": "manual_review_required",
    },
    "source_runtime_error_taxonomy_pending": {
        "status": "follow_up_required",
        "action": "update_source_specific_error_taxonomy",
        "owner": "error-action-routing",
        "follow_up_issue": FOLLOW_UP_ISSUE,
        "release_boundary": "manual_review_required",
    },
}


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def file_digest(path: pathlib.Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def string_list(value: object, label: str) -> list[str]:
    result: list[str] = []
    for index, item in enumerate(as_list(value, label)):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{label}[{index}] must be a non-empty string")
        result.append(item)
    return sorted(result)


def evidence_input(path: pathlib.Path, schema: str) -> dict[str, Any]:
    bytes_value, sha256 = file_digest(path)
    return {
        "path": path.as_posix(),
        "schema": schema,
        "bytes": bytes_value,
        "sha256": sha256,
    }


def source_dir(source_id: str) -> str:
    return source_id.replace("_", "-")


def reviewed_receipt_path(source_id: str) -> str:
    return f"reports/credential-runtime-receipts/{source_dir(source_id)}-credentialed-receipt.json"


def receipt_state(record: dict[str, Any] | None) -> str:
    if record is None:
        return "absent"
    if record.get("review_state") == "reviewed_rejected":
        return "reviewed_rejected"
    if record.get("relief_eligible") is True:
        return "relief_eligible"
    return "reviewed_accepted"


def receipt_linkage(source_id: str, receipt_records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    record = receipt_records.get(source_id)
    return {
        "expected_reviewed_receipt_path": reviewed_receipt_path(source_id),
        "checked_in_receipt_present": record is not None,
        "checked_in_receipt_path": record.get("path") if record else reviewed_receipt_path(source_id),
        "current_receipt_state": receipt_state(record),
        "review_state": record.get("review_state") if record else "none",
        "receipt_outcome": record.get("outcome") if record else "none",
        "source_relief_eligible": bool(record and record.get("relief_eligible") is True),
    }


def receipt_resolves_finding(finding: dict[str, Any]) -> bool:
    linkage = finding.get("reviewed_receipt_linkage")
    return (
        finding.get("status") == "manual_review_boundary"
        and isinstance(linkage, dict)
        and linkage.get("source_relief_eligible") is True
    )


def finding_entry(
    source_id: str,
    severity: str,
    finding_id: str,
    receipt_records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rule = REMEDIATION_RULES.get(finding_id)
    if rule is None:
        raise ValueError(f"missing remediation rule for {severity} {finding_id} in {source_id}")
    entry = {
        "id": finding_id,
        "severity": severity,
        "status": rule["status"],
        "action": rule["action"],
        "owner": rule["owner"],
        "release_boundary": rule["release_boundary"],
    }
    if "follow_up_issue" in rule:
        entry["follow_up_issue"] = rule["follow_up_issue"]
    if finding_id in RECEIPT_LINKED_FINDING_IDS:
        entry["reviewed_receipt_linkage"] = receipt_linkage(source_id, receipt_records)
    return entry


def source_entries(
    runtime_rollup: dict[str, Any],
    receipt_records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, raw_source in enumerate(as_list(runtime_rollup.get("sources"), "runtime_rollup.sources")):
        source = as_dict(raw_source, f"runtime_rollup.sources[{index}]")
        source_id = source.get("source_id")
        provider = source.get("provider")
        plan = source.get("runtime_evidence_plan")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError(f"runtime_rollup.sources[{index}].source_id must be a non-empty string")
        if not isinstance(provider, str) or not provider:
            raise ValueError(f"{source_id}.provider must be a non-empty string")
        if not isinstance(plan, str) or not plan:
            raise ValueError(f"{source_id}.runtime_evidence_plan must be a non-empty string")
        blocker_ids = string_list(source.get("blocker_ids"), f"{source_id}.blocker_ids")
        warning_ids = string_list(source.get("warning_ids"), f"{source_id}.warning_ids")
        entries.append(
            {
                "source_id": source_id,
                "provider": provider,
                "runtime_evidence_plan": plan,
                "evidence_total": source.get("evidence_total"),
                "blocking_count": source.get("blocking_count"),
                "warning_count": source.get("warning_count"),
                "findings": [
                    *(finding_entry(source_id, "blocker", finding_id, receipt_records) for finding_id in blocker_ids),
                    *(finding_entry(source_id, "warning", finding_id, receipt_records) for finding_id in warning_ids),
                ],
            }
        )
    return entries


def receipt_source_entries(runtime_rollup: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, raw_source in enumerate(as_list(runtime_rollup.get("sources"), "runtime_rollup.sources")):
        source = as_dict(raw_source, f"runtime_rollup.sources[{index}]")
        source_id = source.get("source_id")
        provider = source.get("provider")
        plan_path_value = source.get("runtime_evidence_plan")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError(f"runtime_rollup.sources[{index}].source_id must be a non-empty string")
        if not isinstance(provider, str) or not provider:
            raise ValueError(f"{source_id}.provider must be a non-empty string")
        if not isinstance(plan_path_value, str) or not plan_path_value:
            raise ValueError(f"{source_id}.runtime_evidence_plan must be a non-empty string")
        if source_id not in receipts.CREDENTIAL_ENVS:
            raise ValueError(f"missing credential env contract for {source_id}")
        plan = load_json(pathlib.Path(plan_path_value))
        candidate_batch = plan.get("candidate_batch")
        if not isinstance(candidate_batch, str) or not candidate_batch:
            raise ValueError(f"{source_id} runtime plan missing candidate_batch")
        entries.append(
            {
                "source_id": source_id,
                "provider": provider,
                "runtime_evidence_plan": plan_path_value,
                "candidate_batch": candidate_batch,
                "credential_envs": receipts.CREDENTIAL_ENVS[source_id],
            }
        )
    return entries


def validate_runtime_alignment(runtime_rollup: dict[str, Any], sources: list[dict[str, Any]]) -> None:
    for source in sources:
        blockers = [item for item in source["findings"] if item["severity"] == "blocker"]
        warnings = [item for item in source["findings"] if item["severity"] == "warning"]
        if not blockers and not warnings:
            raise ValueError(f"{source['source_id']} must have blocker or warning remediation findings")

    summary = as_dict(runtime_rollup.get("summary"), "runtime_rollup.summary")
    for key in ("blocking_count", "warning_count"):
        if not isinstance(summary.get(key), int) or summary[key] < 0:
            raise ValueError(f"runtime_rollup.summary.{key} must be a non-negative integer")


def build_report(
    runtime_rollup: dict[str, Any],
    routing: dict[str, Any],
    impact: dict[str, Any],
) -> dict[str, Any]:
    runtime_summary = as_dict(runtime_rollup.get("summary"), "runtime_rollup.summary")
    routing_summary = as_dict(routing.get("summary"), "routing.summary")
    impact_summary = as_dict(impact.get("summary"), "impact.summary")
    receipt_state = receipts.discover_reviewed_receipts(
        receipt_glob=REVIEWED_RECEIPT_GLOB,
        schema_path=DEFAULT_RECEIPT_SCHEMA,
        sources=receipt_source_entries(runtime_rollup),
    )
    receipt_records = {
        str(record["source_id"]): record
        for record in as_list(receipt_state.get("receipt_records"), "receipt_state.receipt_records")
    }
    sources = source_entries(runtime_rollup, receipt_records)
    validate_runtime_alignment(runtime_rollup, sources)

    findings = [finding for source in sources for finding in source["findings"]]
    unresolved = sum(1 for finding in findings if finding["status"] == "follow_up_required")
    manual = sum(1 for finding in findings if finding["status"] == "manual_review_boundary")
    receipt_resolved = sum(1 for finding in findings if receipt_resolves_finding(finding))
    effective_findings = [finding for finding in findings if not receipt_resolves_finding(finding)]
    effective_blockers = sum(1 for finding in effective_findings if finding["severity"] == "blocker")
    effective_warnings = sum(1 for finding in effective_findings if finding["severity"] == "warning")
    receipt_linked = sum(1 for finding in findings if "reviewed_receipt_linkage" in finding)
    receipt_linked_absent = sum(
        1
        for finding in findings
        if "reviewed_receipt_linkage" in finding
        and as_dict(finding.get("reviewed_receipt_linkage"), "finding.reviewed_receipt_linkage").get(
            "current_receipt_state"
        )
        == "absent"
    )
    receipt_linked_relief_eligible = sum(
        1
        for finding in findings
        if "reviewed_receipt_linkage" in finding
        and as_dict(finding.get("reviewed_receipt_linkage"), "finding.reviewed_receipt_linkage").get(
            "source_relief_eligible"
        )
        is True
    )
    mapped_blockers = sum(1 for finding in findings if finding["severity"] == "blocker")
    mapped_warnings = sum(1 for finding in findings if finding["severity"] == "warning")
    generated_at = runtime_rollup.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("runtime rollup must provide generated_at")
    manual_review_required = bool(effective_blockers or effective_warnings)
    compatibility_effect = (
        "manual_review_required_until_runtime_blockers_resolved"
        if manual_review_required
        else "runtime_evidence_clear"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "mapping_ticket": 360,
        "follow_up_issue": FOLLOW_UP_ISSUE,
        "provider": "datapan-registry",
        "summary": {
            "sources": runtime_summary.get("sources"),
            "blocking_count": runtime_summary.get("blocking_count"),
            "warning_count": runtime_summary.get("warning_count"),
            "effective_blocking_count": effective_blockers,
            "effective_warning_count": effective_warnings,
            "mapped_blocker_findings": mapped_blockers,
            "mapped_warning_findings": mapped_warnings,
            "sources_without_evidence": runtime_summary.get("sources_without_evidence"),
            "manual_review_boundaries": manual,
            "receipt_resolved_findings": receipt_resolved,
            "follow_up_required": unresolved,
            "compatibility_effect": compatibility_effect,
            "manual_review_required": manual_review_required,
            "credential_policy_available": True,
            "receipt_contract_available": True,
            "reviewed_receipt_intake_available": True,
            "receipt_reviewed": receipt_state["receipt_reviewed"],
            "receipt_relief_eligible": receipt_state["receipt_relief_eligible"],
            "receipt_backed_relief_allowed": receipt_state["manual_review_reduction_allowed"],
            "receipt_backed_relief_status": receipt_state["relief_gate_status"],
            "receipt_linked_findings": receipt_linked,
            "receipt_linked_absent": receipt_linked_absent,
            "receipt_linked_relief_eligible": receipt_linked_relief_eligible,
        },
        "release_evidence_inputs": [
            evidence_input(
                DEFAULT_RUNTIME_ROLLUP,
                "https://schemas.datapan.dev/datapan.source-runtime-evidence-rollup.v1.schema.json",
            ),
            evidence_input(
                DEFAULT_ROUTING,
                "https://schemas.datapan.dev/datapan.error-action-routing-rollup.v1.schema.json",
            ),
            evidence_input(
                DEFAULT_IMPACT,
                "https://schemas.datapan.dev/datapan.registry-impact-plan.v1.schema.json",
            ),
        ],
        "routing_evidence": {
            "blocking_rules": routing_summary.get("blocking_rules"),
            "manual_review_rules": routing_summary.get("manual_review_rules"),
        },
        "downstream_impact_evidence": {
            "requires_manual_review": impact_summary.get("requires_manual_review"),
            "requires_db_migration_review": impact_summary.get("requires_db_migration_review"),
            "requires_served_contract_regeneration": impact_summary.get("requires_served_contract_regeneration"),
        },
        "sources": sources,
    }


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
    parser.add_argument("--runtime-rollup", default=DEFAULT_RUNTIME_ROLLUP, type=pathlib.Path)
    parser.add_argument("--routing", default=DEFAULT_ROUTING, type=pathlib.Path)
    parser.add_argument("--impact", default=DEFAULT_IMPACT, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when the checked-in remediation map is stale")
    args = parser.parse_args()

    try:
        report = build_report(
            load_json(args.runtime_rollup),
            load_json(args.routing),
            load_json(args.impact),
        )
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate source runtime remediation map: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing source runtime remediation map", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale source runtime remediation map; "
                "run `python3 scripts/generate-source-runtime-remediation-map.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (sources={report['summary']['sources']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (sources={report['summary']['sources']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
