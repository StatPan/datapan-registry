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


def build(template_path: pathlib.Path) -> dict[str, Any]:
    template = load(template_path)
    inputs = template["generation_inputs"]
    coverage = load(pathlib.Path(inputs["coverage"]))["summary"]
    latest = load(pathlib.Path(inputs["latest_verification"]))
    latest_summary = load(pathlib.Path(inputs["latest_verification_summary"]))
    plan = load(pathlib.Path(inputs["verification_plan"]))
    provider_index = load(pathlib.Path(inputs["provider_index"]))
    results = latest.get("results")
    if not isinstance(results, list):
        raise ValueError("latest verification results must be an array")
    evidence_counts = latest_summary["summary"]
    by_kind: collections.Counter[str] = collections.Counter(
        str(row["dependency_class"]) for row in results if isinstance(row, dict) and isinstance(row.get("dependency_class"), str)
    )
    operations, total = int(coverage["operations"]), len(results)
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
    remaining = max(0, target - total)
    warnings = []
    if remaining:
        warnings.append({"kind": "runtime_evidence_below_target", "remaining": remaining})
    return {
        **template,
        "generated_at": latest_summary["generated_at"],
        "coverage": {key: coverage[key] for key in ("operations", "callable_operations", "data_go_kr_gateway_operations", "external_endpoint_operations", "registered_adapter_operations", "call_capable_adapters")},
        "evidence": {"total": total, "verified": evidence_counts["verified"], "failed": evidence_counts["failed"], "skipped": evidence_counts["skipped"], "unknown": evidence_counts["unknown"], "coverage_percent": round(total / operations * 100, 1), "by_kind": keyed(by_kind)},
        "growth_target": {"target_percent": 10, "target_evidence_total": target, "remaining_to_target": remaining, "status": "below_target" if remaining else ("above_target" if total > target else "at_target")},
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
