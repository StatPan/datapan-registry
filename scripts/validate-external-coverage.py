#!/usr/bin/env python3
"""Validate checked-in Datapan external coverage reports."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running external coverage validation"
    ) from exc


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, path: pathlib.Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def default_reports() -> list[pathlib.Path]:
    return sorted(pathlib.Path("reports").glob("*/external-coverage-summary.json"))


def referenced_path(report: dict[str, object], key: str) -> pathlib.Path:
    raw_path = report.get(key)
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError(f"{key} must be a non-empty path")
    path = pathlib.Path(raw_path)
    if not path.exists():
        raise ValueError(f"{key} does not exist: {path}")
    return path


def expect_equal(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise ValueError(f"{label}: expected {expected!r}, got {actual!r}")


def validate_generation_inputs(report_path: pathlib.Path, report: dict[str, object]) -> None:
    source_profile = as_dict(load_json(referenced_path(report, "source_profile")), report_path)
    coverage = as_dict(load_json(referenced_path(report, "coverage_report")), report_path)
    adapter_targets = as_dict(load_json(referenced_path(report, "adapter_targets_report")), report_path)
    route_disposition = as_dict(load_json(referenced_path(report, "route_disposition_report")), report_path)
    provider_index = as_dict(load_json(referenced_path(report, "provider_index")), report_path)

    source_id = report.get("source_id")
    provider = report.get("provider")
    expect_equal("source profile source_id", source_profile.get("source_id"), source_id)
    expect_equal("source profile provider", source_profile.get("provider"), provider)

    summary = as_dict(report.get("summary"), report_path)
    coverage_summary = as_dict(coverage.get("summary"), pathlib.Path(str(report.get("coverage_report"))))
    route_summary = as_dict(
        route_disposition.get("summary"),
        pathlib.Path(str(report.get("route_disposition_report"))),
    )
    adapter_summary = as_dict(
        adapter_targets.get("summary"),
        pathlib.Path(str(report.get("adapter_targets_report"))),
    )

    coverage_fields = {
        "external_endpoint_operations": "external_endpoint_operations",
        "registered_adapter_operations": "registered_adapter_operations",
        "missing_adapter_operations": "missing_adapter_operations",
        "raw_external_adapter_coverage_percent": "external_adapter_coverage_percent",
        "registered_adapter_hosts": "registered_adapter_hosts",
        "missing_adapter_hosts": "missing_adapter_hosts",
    }
    for report_key, coverage_key in coverage_fields.items():
        expect_equal(f"summary.{report_key}", summary.get(report_key), coverage_summary.get(coverage_key))

    route_fields = {
        "routes_total": "routes_total",
        "with_probe_evidence": "with_probe_evidence",
        "without_probe_evidence": "without_probe_evidence",
        "dead_route_candidates": "dead_route_candidates",
        "transient_failures": "transient_failures",
        "adapter_candidates": "adapter_candidates",
    }
    report_route = as_dict(report.get("route_disposition"), report_path)
    for report_key, route_key in route_fields.items():
        expect_equal(f"route_disposition.{report_key}", report_route.get(report_key), route_summary.get(route_key))

    operational_gate = as_dict(report.get("operational_gate"), report_path)
    expect_equal(
        "operational_gate.unclassified_missing_route_operations",
        operational_gate.get("unclassified_missing_route_operations"),
        route_summary.get("without_probe_evidence"),
    )
    expect_equal(
        "operational_gate.adapter_backlog_candidate_operations",
        operational_gate.get("adapter_backlog_candidate_operations"),
        route_summary.get("adapter_candidates"),
    )
    if operational_gate.get("unclassified_missing_route_operations") != 0:
        raise ValueError(
            "operational_gate.unclassified_missing_route_operations must be 0; "
            "refresh route-disposition evidence before treating missing external routes as adapter work"
        )
    expected_gate_status = (
        "passing"
        if operational_gate.get("unclassified_missing_route_operations") == 0
        else "action_required"
    )
    expect_equal("operational_gate.status", operational_gate.get("status"), expected_gate_status)

    expect_equal(
        "summary.route_evidence_covered_operations",
        summary.get("route_evidence_covered_operations"),
        route_summary.get("with_probe_evidence"),
    )
    expect_equal(
        "summary.evidence_adjusted_adapter_candidate_operations",
        summary.get("evidence_adjusted_adapter_candidate_operations"),
        route_summary.get("adapter_candidates"),
    )
    expect_equal(
        "adapter target operations",
        adapter_summary.get("target_operations"),
        coverage_summary.get("missing_adapter_operations"),
    )
    expect_equal(
        "adapter target hosts",
        adapter_summary.get("target_hosts"),
        coverage_summary.get("missing_adapter_hosts"),
    )
    expect_equal(
        "provider index host_count",
        provider_index.get("host_count"),
        coverage_summary.get("registered_adapter_hosts"),
    )

    expected_hosts = {
        item.get("host"): item.get("count")
        for item in route_summary.get("by_host", [])
        if isinstance(item, dict)
    }
    actual_hosts = {
        item.get("host"): item.get("operations")
        for item in report.get("missing_hosts", [])
        if isinstance(item, dict)
    }
    expect_equal("missing_hosts", actual_hosts, expected_hosts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.external-coverage.v1.schema.json",
        type=pathlib.Path,
        help="external coverage JSON Schema path",
    )
    parser.add_argument(
        "reports",
        nargs="*",
        type=pathlib.Path,
        help="coverage reports to validate; defaults to reports/*/external-coverage-summary.json",
    )
    args = parser.parse_args()

    reports = args.reports or default_reports()
    if not reports:
        print("no external coverage reports found", file=sys.stderr)
        return 1

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for report_path in reports:
        try:
            instance = as_dict(load_json(report_path), report_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
            if not errors:
                validate_generation_inputs(report_path, instance)
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
        candidates = instance.get("summary", {}).get("evidence_adjusted_adapter_candidate_operations", "<unknown>")
        print(f"ok {report_path} ({source_id}, evidence_adjusted_candidates={candidates})")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
