#!/usr/bin/env python3
"""Generate a source-scoped external endpoint coverage summary."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
from typing import Any


SCHEMA_VERSION = "datapan.external-coverage.v1"
DEFAULT_SOURCE_PROFILE = pathlib.Path("sources/data_go_kr.json")
DEFAULT_COVERAGE = pathlib.Path("reports/coverage.json")
DEFAULT_ADAPTER_TARGETS = pathlib.Path("reports/adapter-targets.json")
DEFAULT_ROUTE_DISPOSITION = pathlib.Path("reports/route-disposition.json")
DEFAULT_PROVIDER_INDEX = pathlib.Path("data/provider-index.json")
DEFAULT_OUTPUT = pathlib.Path("reports/data-go-kr/external-coverage-summary.json")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def portable_path(path: pathlib.Path) -> str:
    return path.as_posix()


def count(summary: dict[str, Any], key: str) -> int:
    value = summary.get(key, 0)
    if not isinstance(value, int):
        raise ValueError(f"summary.{key} must be an integer")
    return value


def percent_value(summary: dict[str, Any], key: str) -> float:
    value = summary.get(key, 0)
    if not isinstance(value, (int, float)):
        raise ValueError(f"summary.{key} must be a number")
    return float(value)


def host_disposition(host_routes: list[dict[str, Any]]) -> tuple[str, str]:
    dispositions = collections.Counter(
        str(route.get("disposition") or "unknown") for route in host_routes
    )
    reasons = collections.Counter(str(route.get("probe_reason") or "") for route in host_routes)
    if len(dispositions) == 1:
        disposition = next(iter(dispositions))
    else:
        disposition = "mixed"

    reason_parts = [
        f"{key}={dispositions[key]}" for key in sorted(dispositions) if key != "unknown"
    ]
    if not reason_parts:
        reason_parts = ["route-disposition evidence exists"]

    top_reasons = [key for key, _ in reasons.most_common(3) if key]
    if top_reasons:
        return disposition, f"{'; '.join(reason_parts)}; reasons: {', '.join(top_reasons)}"
    return disposition, "; ".join(reason_parts)


def build_report(
    source_profile_path: pathlib.Path,
    coverage_path: pathlib.Path,
    adapter_targets_path: pathlib.Path,
    route_disposition_path: pathlib.Path,
    provider_index_path: pathlib.Path,
) -> dict[str, Any]:
    source_profile = as_dict(load_json(source_profile_path), str(source_profile_path))
    coverage = as_dict(load_json(coverage_path), str(coverage_path))
    route_disposition = as_dict(load_json(route_disposition_path), str(route_disposition_path))

    coverage_summary = as_dict(coverage.get("summary"), f"{coverage_path}.summary")
    route_summary = as_dict(route_disposition.get("summary"), f"{route_disposition_path}.summary")
    routes = [
        as_dict(route, f"{route_disposition_path}.routes[]")
        for route in as_list(route_disposition.get("routes"), f"{route_disposition_path}.routes")
    ]

    routes_by_host: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for route in routes:
        host = str(route.get("endpoint_host") or "")
        if host:
            routes_by_host[host].append(route)

    missing_hosts: list[dict[str, Any]] = []
    for host_row in route_summary.get("by_host", []):
        host_data = as_dict(host_row, f"{route_disposition_path}.summary.by_host[]")
        host = str(host_data.get("host") or host_data.get("key") or "")
        if not host:
            raise ValueError("route_disposition.summary.by_host[] must include host")
        operations = host_data.get("count")
        if not isinstance(operations, int):
            raise ValueError(f"route_disposition host {host} count must be an integer")
        disposition, reason = host_disposition(routes_by_host.get(host, []))
        missing_hosts.append(
            {
                "host": host,
                "operations": operations,
                "disposition": disposition,
                "reason": reason,
            }
        )

    without_probe_evidence = count(route_summary, "without_probe_evidence")
    adapter_candidates = count(route_summary, "adapter_candidates")
    raw_external_adapter_coverage_percent = percent_value(
        coverage_summary,
        "external_adapter_coverage_percent",
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": str(route_disposition.get("generated_at") or coverage.get("generated_at")),
        "provider": source_profile.get("provider"),
        "source_id": source_profile.get("source_id"),
        "source_profile": portable_path(source_profile_path),
        "coverage_report": portable_path(coverage_path),
        "adapter_targets_report": portable_path(adapter_targets_path),
        "route_disposition_report": portable_path(route_disposition_path),
        "provider_index": portable_path(provider_index_path),
        "summary": {
            "external_endpoint_operations": count(coverage_summary, "external_endpoint_operations"),
            "registered_adapter_operations": count(coverage_summary, "registered_adapter_operations"),
            "missing_adapter_operations": count(coverage_summary, "missing_adapter_operations"),
            "raw_external_adapter_coverage_percent": raw_external_adapter_coverage_percent,
            "registered_adapter_hosts": count(coverage_summary, "registered_adapter_hosts"),
            "missing_adapter_hosts": count(coverage_summary, "missing_adapter_hosts"),
            "route_evidence_covered_operations": count(route_summary, "with_probe_evidence"),
            "evidence_adjusted_adapter_candidate_operations": adapter_candidates,
        },
        "route_disposition": {
            "routes_total": count(route_summary, "routes_total"),
            "with_probe_evidence": count(route_summary, "with_probe_evidence"),
            "without_probe_evidence": without_probe_evidence,
            "dead_route_candidates": count(route_summary, "dead_route_candidates"),
            "transient_failures": count(route_summary, "transient_failures"),
            "adapter_candidates": adapter_candidates,
        },
        "operational_gate": {
            "status": "passing" if without_probe_evidence == 0 else "action_required",
            "unclassified_missing_route_operations": without_probe_evidence,
            "adapter_backlog_candidate_operations": adapter_candidates,
            "ci_action": "fail_on_unclassified_missing_routes",
            "adapter_backlog_policy": (
                "Create adapter backlog only from route-disposition adapter_candidate evidence, "
                "not from raw external endpoint status."
            ),
        },
        "missing_hosts": missing_hosts,
        "next": [
            (
                f"Preserve raw external adapter coverage at "
                f"{raw_external_adapter_coverage_percent:.1f}% unless new adapters or catalog "
                "changes alter the denominator."
            ),
            (
                f"Treat {adapter_candidates} evidence-adjusted adapter candidate operations as "
                "the current external adapter backlog."
            ),
            (
                f"Keep unclassified missing external routes at {without_probe_evidence}; "
                "refresh unadapted external probes before creating adapter work from raw misses."
            ),
            (
                "Promote host-level disposition from mixed to specific dead/transient/adapter "
                "classifications as route evidence becomes stable."
            ),
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-profile", default=DEFAULT_SOURCE_PROFILE, type=pathlib.Path)
    parser.add_argument("--coverage", default=DEFAULT_COVERAGE, type=pathlib.Path)
    parser.add_argument("--adapter-targets", default=DEFAULT_ADAPTER_TARGETS, type=pathlib.Path)
    parser.add_argument("--route-disposition", default=DEFAULT_ROUTE_DISPOSITION, type=pathlib.Path)
    parser.add_argument("--provider-index", default=DEFAULT_PROVIDER_INDEX, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    args = parser.parse_args()

    report = build_report(
        args.source_profile,
        args.coverage,
        args.adapter_targets,
        args.route_disposition,
        args.provider_index,
    )
    write_json(args.output, report)
    summary = report["summary"]
    print(
        f"wrote {args.output} "
        f"(external={summary['external_endpoint_operations']}, "
        f"missing={summary['missing_adapter_operations']}, "
        f"adapter_candidates={summary['evidence_adjusted_adapter_candidate_operations']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
