#!/usr/bin/env python3
"""Validate checked-in Datapan runtime evidence growth reports."""

from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import sys

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running runtime evidence growth validation"
    ) from exc


TARGET_PERCENT = 10.0


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, path: pathlib.Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def default_reports() -> list[pathlib.Path]:
    return sorted(pathlib.Path("reports").glob("*/runtime-evidence-growth.json"))


def key_counts(counter: collections.Counter[str]) -> list[dict[str, object]]:
    return [{"key": key, "count": counter[key]} for key in sorted(counter)]


def validate_consistency(path: pathlib.Path, report: dict[str, object]) -> None:
    source_profile = pathlib.Path(str(report.get("source_profile")))
    if not source_profile.exists():
        raise ValueError(f"source_profile does not exist: {source_profile}")

    profile = as_dict(load_json(source_profile), source_profile)
    if report.get("source_id") != profile.get("source_id"):
        raise ValueError("source_id does not match source_profile")
    if report.get("provider") != profile.get("provider"):
        raise ValueError("provider does not match source_profile")

    generation_inputs = as_dict(report.get("generation_inputs"), path)
    coverage_path = pathlib.Path(str(generation_inputs.get("coverage")))
    latest_path = pathlib.Path(str(generation_inputs.get("latest_verification")))
    latest_summary_path = pathlib.Path(str(generation_inputs.get("latest_verification_summary")))
    verification_plan_path = pathlib.Path(str(generation_inputs.get("verification_plan")))
    provider_index_path = pathlib.Path(str(generation_inputs.get("provider_index")))

    coverage = as_dict(load_json(coverage_path), coverage_path)
    coverage_summary = as_dict(coverage.get("summary"), coverage_path)
    latest = as_dict(load_json(latest_path), latest_path)
    latest_summary = as_dict(load_json(latest_summary_path), latest_summary_path)
    verification_plan = as_dict(load_json(verification_plan_path), verification_plan_path)
    provider_index = as_dict(load_json(provider_index_path), provider_index_path)

    report_coverage = as_dict(report.get("coverage"), path)
    expected_coverage = {
        "operations": coverage_summary.get("operations"),
        "callable_operations": coverage_summary.get("callable_operations"),
        "data_go_kr_gateway_operations": coverage_summary.get("data_go_kr_gateway_operations"),
        "external_endpoint_operations": coverage_summary.get("external_endpoint_operations"),
        "registered_adapter_operations": coverage_summary.get("registered_adapter_operations"),
        "call_capable_adapters": coverage_summary.get("call_capable_adapters"),
    }
    for key, value in expected_coverage.items():
        if report_coverage.get(key) != value:
            raise ValueError(f"coverage.{key} expected {value}, got {report_coverage.get(key)}")

    latest_summary_counts = as_dict(latest_summary.get("summary"), latest_summary_path)
    report_evidence = as_dict(report.get("evidence"), path)
    for key in ["total", "verified", "failed", "skipped", "unknown"]:
        if report_evidence.get(key) != latest_summary_counts.get(key):
            raise ValueError(f"evidence.{key} expected {latest_summary_counts.get(key)}, got {report_evidence.get(key)}")

    results = latest.get("results")
    if not isinstance(results, list):
        raise ValueError("latest verification results must be an array")
    if len(results) != report_evidence.get("total"):
        raise ValueError(f"evidence.total expected {len(results)} latest verification results")

    by_kind: collections.Counter[str] = collections.Counter()
    for result in results:
        if isinstance(result, dict):
            kind = result.get("dependency_class")
            if isinstance(kind, str):
                by_kind[kind] += 1
    expected_by_kind = key_counts(by_kind)
    if report_evidence.get("by_kind") != expected_by_kind:
        raise ValueError("evidence.by_kind does not match latest verification results")

    operations = int(report_coverage["operations"])
    evidence_total = int(report_evidence["total"])
    expected_percent = round((evidence_total / operations) * 100, 1)
    if report_evidence.get("coverage_percent") != expected_percent:
        raise ValueError(
            f"evidence.coverage_percent expected {expected_percent}, got {report_evidence.get('coverage_percent')}"
        )

    growth_target = as_dict(report.get("growth_target"), path)
    target_total = math.ceil(operations * (TARGET_PERCENT / 100))
    remaining = max(0, target_total - evidence_total)
    status = "below_target" if remaining else "at_target"
    if evidence_total > target_total:
        status = "above_target"
    expected_growth = {
        "target_percent": TARGET_PERCENT,
        "target_evidence_total": target_total,
        "remaining_to_target": remaining,
        "status": status,
    }
    for key, value in expected_growth.items():
        if growth_target.get(key) != value:
            raise ValueError(f"growth_target.{key} expected {value}, got {growth_target.get(key)}")

    report_plan = as_dict(report.get("verification_plan"), path)
    plan_summary = as_dict(verification_plan.get("summary"), verification_plan_path)
    for key in [
        "planned_batches",
        "planned_operations",
        "uncovered_gateway_candidates",
        "uncovered_adapter_candidates",
        "missing_adapter_hosts",
    ]:
        if report_plan.get(key) != plan_summary.get(key):
            raise ValueError(f"verification_plan.{key} expected {plan_summary.get(key)}, got {report_plan.get(key)}")

    batches = verification_plan.get("batches")
    if not isinstance(batches, list):
        raise ValueError("verification_plan.batches must be an array")
    planned_by_kind: collections.Counter[str] = collections.Counter()
    expected_batches: list[dict[str, object]] = []
    for batch in batches:
        if not isinstance(batch, dict):
            raise ValueError("verification_plan.batches must contain objects")
        kind = batch.get("kind")
        if isinstance(kind, str):
            planned_by_kind[kind] += int(batch.get("planned_operations", 0))
        expected_batch = {
            "label": batch.get("label"),
            "kind": batch.get("kind"),
            "candidates": batch.get("candidates"),
            "uncovered_candidates": batch.get("uncovered_candidates"),
            "planned_operations": batch.get("planned_operations"),
            "output": batch.get("output"),
        }
        if "provider" in batch:
            expected_batch["provider"] = batch.get("provider")
        expected_batches.append(expected_batch)
    if report_plan.get("planned_by_kind") != key_counts(planned_by_kind):
        raise ValueError("verification_plan.planned_by_kind does not match verification plan batches")
    if report_plan.get("batches") != expected_batches:
        raise ValueError("verification_plan.batches does not match root verification plan")

    split_readiness = as_dict(provider_index.get("split_readiness"), provider_index_path)
    report_readiness = as_dict(report.get("provider_split_readiness"), path)
    expected_readiness = {
        "status": split_readiness.get("status"),
        "adapter_count": split_readiness.get("adapter_count"),
        "verification_capable_adapters": split_readiness.get("verification_capable_adapters"),
        "call_capable_adapters": split_readiness.get("call_capable_adapters"),
    }
    for key, value in expected_readiness.items():
        if report_readiness.get(key) != value:
            raise ValueError(f"provider_split_readiness.{key} expected {value}, got {report_readiness.get(key)}")

    warnings = report.get("warnings")
    if remaining > 0:
        if not isinstance(warnings, list) or not warnings:
            raise ValueError("warnings must record below-target runtime evidence coverage")
        if not any(isinstance(warning, dict) and warning.get("kind") == "runtime_evidence_below_target" for warning in warnings):
            raise ValueError("warnings must include runtime_evidence_below_target")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.runtime-evidence-growth.v1.schema.json",
        type=pathlib.Path,
        help="runtime evidence growth JSON Schema path",
    )
    parser.add_argument(
        "reports",
        nargs="*",
        type=pathlib.Path,
        help="reports to validate; defaults to reports/*/runtime-evidence-growth.json",
    )
    args = parser.parse_args()

    reports = args.reports or default_reports()
    if not reports:
        print("no runtime evidence growth reports found", file=sys.stderr)
        return 1

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for report_path in reports:
        try:
            instance = as_dict(load_json(report_path), report_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
            if not errors:
                validate_consistency(report_path, instance)
        except Exception as exc:  # noqa: BLE001 - report all validation blockers
            print(f"FAIL {report_path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if errors:
            failures += 1
            print(f"FAIL {report_path}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            continue

        source_id = instance.get("source_id", "<unknown>")
        evidence = instance.get("evidence", {}).get("total", "<unknown>")
        remaining = instance.get("growth_target", {}).get("remaining_to_target", "<unknown>")
        print(f"ok {report_path} ({source_id}, evidence={evidence}, remaining_to_target={remaining})")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
