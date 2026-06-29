#!/usr/bin/env python3
"""Generate institution-level API overview reports for a registry source."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
from typing import Any


SCHEMA_VERSION = "datapan.institution-api-overview.v1"


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


def count_items(counter: collections.Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": counter[key]} for key in sorted(counter)]


def percent(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round((part / whole) * 100, 1)


def verification_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("dataset_id", "")),
        str(row.get("operation", "")),
        str(row.get("endpoint_host", "")),
        str(row.get("dependency_class", "")),
    )


def provider_summary(provider_index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    adapters = provider_index.get("adapters", [])
    if not isinstance(adapters, list):
        return {}
    by_host: dict[str, dict[str, Any]] = {}
    for adapter in adapters:
        if not isinstance(adapter, dict):
            continue
        for host in adapter.get("hosts", []):
            if isinstance(host, str):
                by_host[host] = {
                    "adapter": adapter.get("name"),
                    "adapter_status": adapter.get("status"),
                    "capabilities": adapter.get("capabilities", []),
                }
    return by_host


def build_report(
    registry_path: pathlib.Path,
    dependencies_path: pathlib.Path,
    latest_verification_path: pathlib.Path,
    coverage_path: pathlib.Path,
    provider_index_path: pathlib.Path,
) -> dict[str, Any]:
    registry_rows = as_list(load_json(registry_path), str(registry_path))
    dependencies = as_dict(load_json(dependencies_path), str(dependencies_path))
    latest = as_dict(load_json(latest_verification_path), str(latest_verification_path))
    coverage = as_dict(load_json(coverage_path), str(coverage_path))
    provider_index = as_dict(load_json(provider_index_path), str(provider_index_path))

    dependency_rows = as_list(dependencies.get("dependencies"), "dependencies")
    verification_rows = as_list(latest.get("results"), "latest verification results")
    coverage_summary = as_dict(coverage.get("summary"), "coverage.summary")
    host_adapters = provider_summary(provider_index)

    evidence_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for raw in verification_rows:
        if isinstance(raw, dict):
            evidence_by_key[verification_key(raw)] = raw

    institutions: dict[str, dict[str, Any]] = {}
    dependency_class_total: collections.Counter[str] = collections.Counter()
    adapter_status_total: collections.Counter[str] = collections.Counter()
    category_total: collections.Counter[str] = collections.Counter()
    host_total: collections.Counter[str] = collections.Counter()

    for raw in registry_rows:
        if not isinstance(raw, dict):
            continue
        organization = str(raw.get("organization") or "Unknown")
        institution = institutions.setdefault(
            organization,
            {
                "organization": organization,
                "api_ids": set(),
                "operation_api_ids": set(),
                "operation_count": 0,
                "dependency_class": collections.Counter(),
                "adapter_status": collections.Counter(),
                "source_category": collections.Counter(),
                "endpoint_hosts": collections.Counter(),
                "approval_required_operations": 0,
                "no_endpoint_operations": 0,
                "runtime_evidence": collections.Counter(),
            },
        )
        dataset_id = str(raw.get("id", ""))
        if dataset_id:
            institution["api_ids"].add(dataset_id)

    for raw in dependency_rows:
        if not isinstance(raw, dict):
            continue
        organization = str(raw.get("organization") or "Unknown")
        institution = institutions.setdefault(
            organization,
            {
                "organization": organization,
                "api_ids": set(),
                "operation_api_ids": set(),
                "operation_count": 0,
                "dependency_class": collections.Counter(),
                "adapter_status": collections.Counter(),
                "source_category": collections.Counter(),
                "endpoint_hosts": collections.Counter(),
                "approval_required_operations": 0,
                "no_endpoint_operations": 0,
                "runtime_evidence": collections.Counter(),
            },
        )

        dataset_id = str(raw.get("dataset_id", ""))
        if dataset_id:
            institution["operation_api_ids"].add(dataset_id)
        institution["operation_count"] += 1

        dependency_class = str(raw.get("dependency_class") or "unknown")
        adapter_status = str(raw.get("adapter_status") or "unknown")
        source_category = str(raw.get("source_category") or "unknown")
        endpoint_host = str(raw.get("endpoint_host") or raw.get("source_host") or "")

        institution["dependency_class"][dependency_class] += 1
        institution["adapter_status"][adapter_status] += 1
        institution["source_category"][source_category] += 1
        dependency_class_total[dependency_class] += 1
        adapter_status_total[adapter_status] += 1
        category_total[source_category] += 1

        if endpoint_host:
            institution["endpoint_hosts"][endpoint_host] += 1
            host_total[endpoint_host] += 1

        if raw.get("approval_required") is True:
            institution["approval_required_operations"] += 1
        if dependency_class == "no_endpoint":
            institution["no_endpoint_operations"] += 1

        evidence = evidence_by_key.get(verification_key(raw))
        if evidence:
            status = str(evidence.get("status") or "unknown")
            institution["runtime_evidence"][status] += 1

    institution_rows: list[dict[str, Any]] = []
    for value in institutions.values():
        api_count = len(value["api_ids"])
        operation_api_count = len(value["operation_api_ids"])
        operation_count = value["operation_count"]
        evidence_counter = value["runtime_evidence"]
        evidence_total = sum(evidence_counter.values())
        top_hosts = [
            {
                "host": host,
                "operation_count": count,
                **host_adapters.get(host, {}),
            }
            for host, count in value["endpoint_hosts"].most_common(5)
        ]
        top_categories = [
            {"category": key, "operation_count": count}
            for key, count in value["source_category"].most_common(5)
        ]
        institution_rows.append(
            {
                "organization": value["organization"],
                "api_count": api_count,
                "operation_api_count": operation_api_count,
                "operation_count": operation_count,
                "runtime_evidence_count": evidence_total,
                "runtime_evidence_percent": percent(evidence_total, operation_count),
                "verified": evidence_counter.get("verified", 0),
                "failed": evidence_counter.get("failed", 0),
                "skipped": evidence_counter.get("skipped", 0),
                "unknown": evidence_counter.get("unknown", 0),
                "approval_required_operations": value["approval_required_operations"],
                "no_endpoint_operations": value["no_endpoint_operations"],
                "by_dependency_class": count_items(value["dependency_class"]),
                "by_adapter_status": count_items(value["adapter_status"]),
                "top_categories": top_categories,
                "top_hosts": top_hosts,
            }
        )

    institution_rows.sort(
        key=lambda row: (
            -int(row["operation_count"]),
            -int(row["api_count"]),
            str(row["organization"]),
        )
    )

    total_apis = len(
        {
            str(row.get("id"))
            for row in registry_rows
            if isinstance(row, dict) and row.get("id")
        }
    )
    evidence_total = as_dict(latest.get("summary"), "latest.summary").get("total", 0)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dependencies.get("generated_at"),
        "provider": dependencies.get("provider"),
        "source_id": "data_go_kr",
        "generation_inputs": {
            "registry": str(registry_path).replace("\\", "/"),
            "dependencies": str(dependencies_path).replace("\\", "/"),
            "latest_verification": str(latest_verification_path).replace("\\", "/"),
            "coverage": str(coverage_path).replace("\\", "/"),
            "provider_index": str(provider_index_path).replace("\\", "/"),
        },
        "summary": {
            "institutions": len(institution_rows),
            "apis": total_apis,
            "operations": len(dependency_rows),
            "runtime_evidence": evidence_total,
            "runtime_evidence_percent": percent(int(evidence_total), len(dependency_rows)),
            "callable_operations": coverage_summary.get("callable_operations"),
            "external_endpoint_operations": coverage_summary.get("external_endpoint_operations"),
            "registered_adapter_operations": coverage_summary.get("registered_adapter_operations"),
            "missing_adapter_operations": coverage_summary.get("missing_adapter_operations"),
            "missing_adapter_hosts": coverage_summary.get("missing_adapter_hosts"),
        },
        "by_dependency_class": count_items(dependency_class_total),
        "by_adapter_status": count_items(adapter_status_total),
        "top_source_categories": [
            {"category": key, "operation_count": count}
            for key, count in category_total.most_common(20)
        ],
        "top_hosts": [
            {
                "host": key,
                "operation_count": count,
                **host_adapters.get(key, {}),
            }
            for key, count in host_total.most_common(20)
        ],
        "institutions": institution_rows,
    }


def markdown_table(rows: list[list[object]]) -> str:
    output = [
        "| Institution | APIs | APIs with ops | Ops | Evidence | Evidence % | Verified | Failed | Skipped | Approval | No endpoint |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        output.append(
            "| "
            + " | ".join(str(value).replace("|", "\\|") for value in row)
            + " |"
        )
    return "\n".join(output)


def build_markdown(report: dict[str, Any], limit: int) -> str:
    summary = as_dict(report.get("summary"), "summary")
    institutions = as_list(report.get("institutions"), "institutions")
    operation_rows = [
        [
            row["organization"],
            row["api_count"],
            row["operation_api_count"],
            row["operation_count"],
            row["runtime_evidence_count"],
            f"{row['runtime_evidence_percent']}%",
            row["verified"],
            row["failed"],
            row["skipped"],
            row["approval_required_operations"],
            row["no_endpoint_operations"],
        ]
        for row in institutions[:limit]
        if isinstance(row, dict)
    ]
    api_rows = [
        [
            row["organization"],
            row["api_count"],
            row["operation_api_count"],
            row["operation_count"],
            row["runtime_evidence_count"],
            f"{row['runtime_evidence_percent']}%",
            row["verified"],
            row["failed"],
            row["skipped"],
            row["approval_required_operations"],
            row["no_endpoint_operations"],
        ]
        for row in sorted(
            [row for row in institutions if isinstance(row, dict)],
            key=lambda row: (
                -int(row["api_count"]),
                -int(row["operation_count"]),
                str(row["organization"]),
            ),
        )[:limit]
    ]
    category_lines = [
        f"- {item['category']}: `{item['operation_count']}` operations"
        for item in report.get("top_source_categories", [])[:10]
        if isinstance(item, dict)
    ]
    host_lines = [
        f"- {item['host']}: `{item['operation_count']}` operations"
        for item in report.get("top_hosts", [])[:10]
        if isinstance(item, dict)
    ]
    return (
        "# data.go.kr Institution API Overview\n\n"
        f"- Generated at: `{report.get('generated_at')}`\n"
        f"- Institutions: `{summary.get('institutions')}`\n"
        f"- APIs: `{summary.get('apis')}`\n"
        f"- Operations: `{summary.get('operations')}`\n"
        f"- Runtime evidence: `{summary.get('runtime_evidence')}` "
        f"(`{summary.get('runtime_evidence_percent')}%`)\n"
        f"- External endpoint operations: `{summary.get('external_endpoint_operations')}`\n"
        f"- Registered adapter operations: `{summary.get('registered_adapter_operations')}`\n"
        f"- Missing adapter operations: `{summary.get('missing_adapter_operations')}`\n\n"
        "## Largest Institutions By API Count\n\n"
        f"{markdown_table(api_rows)}\n\n"
        "## Largest Institutions By Operation Count\n\n"
        f"{markdown_table(operation_rows)}\n\n"
        "The full machine-readable report is "
        "`reports/data-go-kr/institution-api-overview.json`.\n\n"
        "## Largest Categories\n\n"
        + "\n".join(category_lines)
        + "\n\n## Largest Hosts\n\n"
        + "\n".join(host_lines)
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="data/data-go-kr.registry.json", type=pathlib.Path)
    parser.add_argument("--dependencies", default="reports/dependencies.json", type=pathlib.Path)
    parser.add_argument("--latest-verification", default="reports/latest-verification.json", type=pathlib.Path)
    parser.add_argument("--coverage", default="reports/coverage.json", type=pathlib.Path)
    parser.add_argument("--provider-index", default="data/provider-index.json", type=pathlib.Path)
    parser.add_argument("--output", default="reports/data-go-kr/institution-api-overview.json", type=pathlib.Path)
    parser.add_argument("--markdown-output", default="docs/data-go-kr-institution-api-overview.md", type=pathlib.Path)
    parser.add_argument("--markdown-limit", default=30, type=int)
    args = parser.parse_args()

    report = build_report(
        args.registry,
        args.dependencies,
        args.latest_verification,
        args.coverage,
        args.provider_index,
    )
    write_json(args.output, report)
    write_text(args.markdown_output, build_markdown(report, args.markdown_limit))
    print(
        f"wrote {args.output} and {args.markdown_output} "
        f"({report['summary']['institutions']} institutions)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
