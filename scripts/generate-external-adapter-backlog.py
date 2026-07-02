#!/usr/bin/env python3
"""Generate a source-scoped external adapter implementation backlog."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
from typing import Any


SCHEMA_VERSION = "datapan.external-adapter-backlog.v1"
DEFAULT_SOURCE_PROFILE = pathlib.Path("sources/data_go_kr.json")
DEFAULT_ROUTE_DISPOSITION = pathlib.Path("reports/route-disposition.json")
DEFAULT_ADAPTER_TARGETS = pathlib.Path("reports/adapter-targets.json")
DEFAULT_COVERAGE = pathlib.Path("reports/coverage.json")
DEFAULT_OUTPUT = pathlib.Path("reports/data-go-kr/external-adapter-backlog.json")
DEFAULT_MARKDOWN_OUTPUT = pathlib.Path("docs/data-go-kr-external-adapter-backlog.md")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_text(path: pathlib.Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


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


def percent(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round((part / whole) * 100, 1)


def count_by_key(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    counts = collections.Counter(str(row.get(key) or "") for row in rows)
    items = [
        {"key": item_key, "count": counts[item_key]}
        for item_key in sorted(counts)
        if item_key
    ]
    items.sort(key=lambda item: (-int(item["count"]), str(item["key"])))
    return items


def normalize_route(raw_route: Any, label: str) -> dict[str, Any]:
    route = as_dict(raw_route, label)
    return {
        "dataset_id": str(route.get("dataset_id") or ""),
        "title": route.get("title"),
        "organization": route.get("organization"),
        "operation": route.get("operation"),
        "endpoint": route.get("endpoint"),
        "endpoint_host": route.get("endpoint_host"),
        "dependency_class": route.get("dependency_class"),
        "disposition": route.get("disposition"),
        "probe_status": route.get("probe_status"),
        "probe_reason": route.get("probe_reason"),
        "http_status": route.get("http_status"),
        "body_shape": route.get("body_shape"),
        "missing_params": route.get("missing_params") or [],
        "recommended_action": "implement_provider_adapter",
    }


def build_host_rows(candidate_routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for route in candidate_routes:
        host = str(route.get("endpoint_host") or "")
        if host:
            grouped[host].append(route)

    hosts: list[dict[str, Any]] = []
    for host, routes in grouped.items():
        dataset_ids = sorted({str(route.get("dataset_id") or "") for route in routes if route.get("dataset_id")})
        institutions = sorted({str(route.get("organization") or "") for route in routes if route.get("organization")})
        body_shapes = count_by_key(routes, "body_shape")
        probe_reasons = count_by_key(routes, "probe_reason")
        sample_routes = sorted(
            routes,
            key=lambda route: (
                str(route.get("dataset_id") or ""),
                str(route.get("operation") or ""),
                str(route.get("endpoint") or ""),
            ),
        )[:5]
        hosts.append(
            {
                "host": host,
                "priority": len(routes),
                "candidate_operations": len(routes),
                "candidate_apis": len(dataset_ids),
                "institutions": institutions,
                "probe_reasons": probe_reasons,
                "body_shapes": body_shapes,
                "sample_dataset_ids": dataset_ids[:5],
                "sample_routes": sample_routes,
                "implementation_status": "adapter_not_registered",
                "recommended_next_step": (
                    f"Add a provider adapter for {host} or mark the host with stronger route "
                    "disposition evidence if the HTML surface is not machine-callable."
                ),
            }
        )
    hosts.sort(key=lambda item: (-int(item["candidate_operations"]), str(item["host"])))
    return hosts


def build_report(
    source_profile_path: pathlib.Path,
    route_disposition_path: pathlib.Path,
    adapter_targets_path: pathlib.Path,
    coverage_path: pathlib.Path,
) -> dict[str, Any]:
    source_profile = as_dict(load_json(source_profile_path), str(source_profile_path))
    route_disposition = as_dict(load_json(route_disposition_path), str(route_disposition_path))
    adapter_targets = as_dict(load_json(adapter_targets_path), str(adapter_targets_path))
    coverage = as_dict(load_json(coverage_path), str(coverage_path))

    route_summary = as_dict(route_disposition.get("summary"), f"{route_disposition_path}.summary")
    adapter_summary = as_dict(adapter_targets.get("summary"), f"{adapter_targets_path}.summary")
    coverage_summary = as_dict(coverage.get("summary"), f"{coverage_path}.summary")
    routes = [
        normalize_route(route, f"{route_disposition_path}.routes[]")
        for route in as_list(route_disposition.get("routes"), f"{route_disposition_path}.routes")
    ]
    candidate_routes = [route for route in routes if route.get("disposition") == "adapter_candidate"]
    excluded_routes = [route for route in routes if route.get("disposition") != "adapter_candidate"]
    hosts = build_host_rows(candidate_routes)

    candidate_apis = sorted({route["dataset_id"] for route in candidate_routes if route["dataset_id"]})
    candidate_institutions = sorted(
        {str(route.get("organization") or "") for route in candidate_routes if route.get("organization")}
    )
    next_steps = [
        "Create host-scoped adapter implementation tickets from the hosts array in priority order.",
        "Refresh route probes before implementing any host whose evidence changes away from adapter_candidate.",
    ]
    if hosts:
        first_host = hosts[0]
        next_steps.insert(
            1,
            (
                f"Start with {first_host.get('host')} because it currently owns the largest "
                f"candidate-operation count ({first_host.get('candidate_operations')})."
            ),
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": route_disposition.get("generated_at"),
        "provider": source_profile.get("provider"),
        "source_id": source_profile.get("source_id"),
        "source_profile": portable_path(source_profile_path),
        "generation_inputs": {
            "route_disposition": portable_path(route_disposition_path),
            "adapter_targets": portable_path(adapter_targets_path),
            "coverage": portable_path(coverage_path),
        },
        "summary": {
            "candidate_hosts": len(hosts),
            "candidate_operations": len(candidate_routes),
            "candidate_apis": len(candidate_apis),
            "candidate_institutions": len(candidate_institutions),
            "raw_missing_adapter_operations": coverage_summary.get("missing_adapter_operations"),
            "raw_missing_adapter_hosts": coverage_summary.get("missing_adapter_hosts"),
            "route_disposition_operations": route_summary.get("operations"),
            "route_disposition_adapter_candidates": route_summary.get("adapter_candidates"),
            "excluded_dead_route_candidates": route_summary.get("dead_route_candidates"),
            "excluded_transient_failures": route_summary.get("transient_failures"),
            "unclassified_missing_routes": route_summary.get("without_probe_evidence"),
            "adapter_target_operations": adapter_summary.get("target_operations"),
            "candidate_share_of_missing_adapter_operations_percent": percent(
                len(candidate_routes),
                int(coverage_summary.get("missing_adapter_operations") or 0),
            ),
        },
        "policy": {
            "candidate_filter": "route_disposition.disposition == adapter_candidate",
            "excluded_policy": (
                "Dead-route and transient-failure evidence remains in route-disposition "
                "and must not be converted into adapter implementation work."
            ),
            "gate": "unclassified_missing_routes must remain 0 before adapter backlog is actionable",
        },
        "hosts": hosts,
        "candidate_routes": sorted(
            candidate_routes,
            key=lambda route: (
                str(route.get("endpoint_host") or ""),
                str(route.get("dataset_id") or ""),
                str(route.get("operation") or ""),
                str(route.get("endpoint") or ""),
            ),
        ),
        "excluded_dispositions": {
            "dead_route_candidate": [
                route for route in routes if route.get("disposition") == "dead_route_candidate"
            ],
            "transient_failure": [
                route for route in routes if route.get("disposition") == "transient_failure"
            ],
        },
        "next": next_steps,
    }


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(out)


def build_markdown(report: dict[str, Any]) -> str:
    summary = as_dict(report.get("summary"), "report.summary")
    hosts = as_list(report.get("hosts"), "report.hosts")
    host_rows = [
        [
            f"`{host.get('host')}`",
            host.get("candidate_operations"),
            host.get("candidate_apis"),
            ", ".join(str(item) for item in host.get("institutions", [])),
            host.get("implementation_status"),
        ]
        for host in hosts
    ]
    sample_rows: list[list[Any]] = []
    for route in as_list(report.get("candidate_routes"), "report.candidate_routes")[:30]:
        sample_rows.append(
            [
                f"`{route.get('endpoint_host')}`",
                route.get("dataset_id"),
                route.get("organization"),
                route.get("title"),
                route.get("operation"),
            ]
        )

    return (
        "# data.go.kr External Adapter Backlog\n\n"
        "This backlog is generated from route-disposition evidence and includes only "
        "`adapter_candidate` routes. Dead-route and transient-failure routes remain "
        "evidence, not adapter implementation work.\n\n"
        f"- Generated at: `{report.get('generated_at')}`\n"
        f"- Candidate hosts: `{summary.get('candidate_hosts')}`\n"
        f"- Candidate operations: `{summary.get('candidate_operations')}`\n"
        f"- Candidate APIs: `{summary.get('candidate_apis')}`\n"
        f"- Candidate institutions: `{summary.get('candidate_institutions')}`\n"
        f"- Raw missing adapter operations: `{summary.get('raw_missing_adapter_operations')}`\n"
        f"- Dead-route candidates excluded: `{summary.get('excluded_dead_route_candidates')}`\n"
        f"- Transient failures excluded: `{summary.get('excluded_transient_failures')}`\n"
        f"- Unclassified missing routes: `{summary.get('unclassified_missing_routes')}`\n\n"
        "## Candidate Hosts\n\n"
        f"{markdown_table(['Host', 'Ops', 'APIs', 'Institutions', 'Status'], host_rows)}\n\n"
        "## Sample Candidate Routes\n\n"
        f"{markdown_table(['Host', 'API ID', 'Institution', 'Title', 'Operation'], sample_rows)}\n\n"
        "The full machine-readable backlog is "
        "`reports/data-go-kr/external-adapter-backlog.json`.\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-profile", default=DEFAULT_SOURCE_PROFILE, type=pathlib.Path)
    parser.add_argument("--route-disposition", default=DEFAULT_ROUTE_DISPOSITION, type=pathlib.Path)
    parser.add_argument("--adapter-targets", default=DEFAULT_ADAPTER_TARGETS, type=pathlib.Path)
    parser.add_argument("--coverage", default=DEFAULT_COVERAGE, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--markdown-output", default=DEFAULT_MARKDOWN_OUTPUT, type=pathlib.Path)
    args = parser.parse_args()

    report = build_report(args.source_profile, args.route_disposition, args.adapter_targets, args.coverage)
    write_json(args.output, report)
    write_text(args.markdown_output, build_markdown(report))
    summary = report["summary"]
    print(
        f"wrote {args.output} and {args.markdown_output} "
        f"(hosts={summary['candidate_hosts']}, operations={summary['candidate_operations']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
