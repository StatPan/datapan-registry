#!/usr/bin/env python3
"""Generate runtime evidence growth from its declared source artifacts."""

from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import sys
from typing import Any


DEFAULT_OUTPUT = pathlib.Path("reports/data-go-kr/runtime-evidence-growth.json")


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def keyed(counter: collections.Counter[str]) -> list[dict[str, object]]:
    return [{"key": key, "count": counter[key]} for key in sorted(counter)]


def current_evidence_counts(projection: dict[str, Any]) -> tuple[list[dict[str, Any]], collections.Counter[str], int]:
    """Return current bound observations and fresh verified successes separately."""
    rows = [
        row for row in projection.get("current_evidence", [])
        if isinstance(row, dict) and row.get("disposition") in {"eligible", "stale", "recent_non_verified"}
    ]
    statuses = collections.Counter(str(row.get("status") or "unknown") for row in rows)
    fresh_verified = sum(row.get("disposition") == "eligible" and row.get("status") == "verified" for row in rows)
    return rows, statuses, fresh_verified


def build(template_path: pathlib.Path) -> dict[str, Any]:
    template = load(template_path)
    inputs = template["generation_inputs"]
    coverage = load(pathlib.Path(inputs["coverage"]))["summary"]
    latest = load(pathlib.Path(inputs["latest_verification"]))
    latest_summary = load(pathlib.Path(inputs["latest_verification_summary"]))
    projection = load(pathlib.Path(inputs["current_runtime_evidence_projection"]))
    dependencies = load(pathlib.Path(inputs["dependencies"]))
    plan = load(pathlib.Path(inputs["verification_plan"]))
    provider_index = load(pathlib.Path(inputs["provider_index"]))
    results = latest.get("results")
    if not isinstance(results, list):
        raise ValueError("latest verification results must be an array")
    historical_counts = latest_summary["summary"]
    historical_by_kind: collections.Counter[str] = collections.Counter(
        str(row["dependency_class"]) for row in results if isinstance(row, dict) and isinstance(row.get("dependency_class"), str)
    )
    current_rows, current_statuses, fresh_verified_total = current_evidence_counts(projection)
    operations_by_key: dict[str, dict[str, Any]] = {
        str(row["identity_key"]): row for row in projection.get("operations", [])
        if isinstance(row, dict) and isinstance(row.get("identity_key"), str)
    }
    deps_by_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in dependencies.get("dependencies", []):
        if isinstance(row, dict):
            deps_by_name.setdefault((str(row.get("dataset_id") or ""), str(row.get("operation") or "")), []).append(row)
    operations_for_name: dict[tuple[str, str], list[str]] = {}
    for row in projection.get("operations", []):
        if isinstance(row, dict):
            operations_for_name.setdefault((str(row.get("dataset_id") or ""), str(row.get("operation") or "")), []).append(str(row.get("identity_key") or ""))
    current_by_kind: collections.Counter[str] = collections.Counter()
    for row in current_rows:
        operation = operations_by_key.get(str(row.get("identity_key")), {})
        key = (str(operation.get("dataset_id") or ""), str(operation.get("operation") or ""))
        dependencies_for_name = deps_by_name.get(key, [])
        operation_identities = operations_for_name.get(key, [])
        if len(dependencies_for_name) == 1 and len(operation_identities) == 1:
            kind = dependencies_for_name[0].get("dependency_class")
        else:
            kind = "unclassified_dependency_identity"
        current_by_kind[str(kind or "unknown")] += 1

    operations, total = int(coverage["operations"]), len(current_rows)
    target = math.ceil(operations * 0.10)
    batches = plan["batches"]
    planned_by_kind: collections.Counter[str] = collections.Counter()
    normalized_batches = []
    for batch in batches:
        planned_by_kind[str(batch["kind"])] += int(batch["planned_operations"])
        normalized = {"label": batch.get("label")}
        if "provider" in batch:
            normalized["provider"] = batch["provider"]
        normalized.update({key: batch.get(key) for key in ("kind", "candidates", "uncovered_candidates", "planned_operations", "output")})
        normalized_batches.append(normalized)
    plan_summary = plan["summary"]
    split = provider_index["split_readiness"]
    remaining = max(0, target - fresh_verified_total)
    warnings = []
    if remaining:
        warnings.append({"kind": "runtime_evidence_below_target", "severity": "warning", "message": f"{remaining} fresh verified current-contract runtime results remain before the success growth target."})
    projection_summary = projection["summary"]
    return {
        **template,
        "generated_at": latest_summary["generated_at"],
        "coverage": {key: coverage[key] for key in ("operations", "callable_operations", "data_go_kr_gateway_operations", "external_endpoint_operations", "registered_adapter_operations", "call_capable_adapters")},
        "evidence": {"scope": "current_contract_bound_within_expiry", "total": total, "current_bound_observations": total, "fresh_verified": fresh_verified_total, "verified": current_statuses["verified"], "failed": current_statuses["failed"], "skipped": current_statuses["skipped"], "unknown": current_statuses["unknown"], "coverage_percent": round(total / operations * 100, 1), "success_coverage_percent": round(fresh_verified_total / operations * 100, 1), "by_kind": keyed(current_by_kind)},
        "historical_evidence": {"total": len(results), "verified": historical_counts["verified"], "failed": historical_counts["failed"], "skipped": historical_counts["skipped"], "unknown": historical_counts["unknown"], "by_kind": keyed(historical_by_kind)},
        "evidence_dispositions": {"unbound": int(projection_summary.get("unbound", 0)), "contract_changed": int(projection_summary.get("contract_changed", 0)), "ambiguous": int(projection_summary.get("ambiguous", 0)), "historical": int(projection_summary.get("historical", 0))},
        "growth_target": {"target_percent": 10, "target_basis": "fresh_verified_current_contract", "target_evidence_total": target, "fresh_verified_total": fresh_verified_total, "remaining_to_target": remaining, "status": "below_target" if remaining else ("above_target" if fresh_verified_total > target else "at_target")},
        "verification_plan": {**{key: plan_summary[key] for key in ("planned_batches", "planned_operations", "uncovered_gateway_candidates", "uncovered_adapter_candidates", "missing_adapter_hosts")}, "planned_by_kind": keyed(planned_by_kind), "batches": normalized_batches},
        "provider_split_readiness": {key: split[key] for key in ("status", "adapter_count", "verification_capable_adapters", "call_capable_adapters")},
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        report = build(args.output)
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
                raise ValueError(f"{args.output} is stale")
        else:
            args.output.write_text(rendered, encoding="utf-8")
        print(f"{'ok' if args.check else 'wrote'} {args.output} (evidence={report['evidence']['total']})")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL generate runtime evidence growth: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
