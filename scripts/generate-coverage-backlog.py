#!/usr/bin/env python3
"""Generate data.go.kr API coverage and runtime reactivation backlog."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
from typing import Any


SCHEMA_VERSION = "datapan.coverage-backlog.v1"


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


def count_items(counter: collections.Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": counter[key]} for key in sorted(counter)]


def raw_field(row: dict[str, Any], key: str) -> Any:
    source = row.get("source")
    if not isinstance(source, dict):
        return None
    raw = source.get("raw")
    if not isinstance(raw, dict):
        return None
    return raw.get(key)


def api_row(row: dict[str, Any], action: str, reason: str) -> dict[str, Any]:
    return {
        "dataset_id": str(row.get("id") or ""),
        "title": row.get("title"),
        "organization": row.get("organization"),
        "source_category": row.get("source_category"),
        "api_type": raw_field(row, "api_type"),
        "data_format": raw_field(row, "data_format"),
        "register_status": raw_field(row, "register_status"),
        "dev_approval": raw_field(row, "is_confirmed_for_dev_nm"),
        "prod_approval": raw_field(row, "is_confirmed_for_prod_nm"),
        "created_at": raw_field(row, "created_at"),
        "updated_at": raw_field(row, "updated_at"),
        "meta_url": raw_field(row, "meta_url"),
        "action": action,
        "reason": reason,
    }


def dependency_summary(rows: list[dict[str, Any]], evidence_by_key: dict[tuple[str, str, str, str], dict[str, Any]]) -> dict[str, Any]:
    dependency_class = collections.Counter(str(row.get("dependency_class") or "unknown") for row in rows)
    adapter_status = collections.Counter(str(row.get("adapter_status") or "unknown") for row in rows)
    endpoint_host = collections.Counter(str(row.get("endpoint_host") or "") for row in rows if row.get("endpoint_host"))
    skip_reasons = sorted(
        {
            str(row.get("skip_reason"))
            for row in rows
            if row.get("skip_reason")
        }
    )[:5]
    evidence = collections.Counter()
    missing_evidence = 0
    approval_required = 0
    missing_adapter = 0
    no_endpoint = 0

    for row in rows:
        if row.get("approval_required") is True:
            approval_required += 1
        if row.get("adapter_status") == "missing":
            missing_adapter += 1
        if row.get("dependency_class") == "no_endpoint":
            no_endpoint += 1
        verification = evidence_by_key.get(verification_key(row))
        if verification:
            evidence[str(verification.get("status") or "unknown")] += 1
        else:
            missing_evidence += 1

    return {
        "operation_count": len(rows),
        "runtime_evidence_count": sum(evidence.values()),
        "runtime_missing_evidence_count": missing_evidence,
        "verified": evidence.get("verified", 0),
        "failed": evidence.get("failed", 0),
        "skipped": evidence.get("skipped", 0),
        "unknown": evidence.get("unknown", 0),
        "approval_required_operations": approval_required,
        "missing_adapter_operations": missing_adapter,
        "no_endpoint_operations": no_endpoint,
        "primary_dependency_class": dependency_class.most_common(1)[0][0] if dependency_class else "",
        "primary_adapter_status": adapter_status.most_common(1)[0][0] if adapter_status else "",
        "primary_endpoint_host": endpoint_host.most_common(1)[0][0] if endpoint_host else "",
        "skip_reasons": skip_reasons,
    }


def build_report(
    registry_path: pathlib.Path,
    dependencies_path: pathlib.Path,
    latest_verification_path: pathlib.Path,
) -> dict[str, Any]:
    registry_rows = [row for row in as_list(load_json(registry_path), str(registry_path)) if isinstance(row, dict)]
    dependencies = as_dict(load_json(dependencies_path), str(dependencies_path))
    latest = as_dict(load_json(latest_verification_path), str(latest_verification_path))
    dependency_rows = [
        row for row in as_list(dependencies.get("dependencies"), "dependencies") if isinstance(row, dict)
    ]
    verification_rows = [
        row for row in as_list(latest.get("results"), "latest verification results") if isinstance(row, dict)
    ]

    registry_by_id = {str(row.get("id")): row for row in registry_rows if row.get("id")}
    dependencies_by_id: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in dependency_rows:
        dataset_id = str(row.get("dataset_id") or "")
        if dataset_id:
            dependencies_by_id[dataset_id].append(row)

    evidence_by_key = {verification_key(row): row for row in verification_rows}
    evidence_ids = {str(row.get("dataset_id")) for row in verification_rows if row.get("dataset_id")}
    failed_ids = {
        str(row.get("dataset_id"))
        for row in verification_rows
        if row.get("dataset_id") and row.get("status") == "failed"
    }

    institution_state: dict[str, dict[str, Any]] = {}
    uncovered_apis: list[dict[str, Any]] = []
    runtime_reactivation_apis: list[dict[str, Any]] = []
    runtime_repair_apis: list[dict[str, Any]] = []

    for dataset_id, row in sorted(registry_by_id.items()):
        organization = str(row.get("organization") or "Unknown")
        state = institution_state.setdefault(
            organization,
            {
                "organization": organization,
                "api_count": 0,
                "covered_api_count": 0,
                "uncovered_api_count": 0,
                "operation_count": 0,
                "runtime_evidence_api_count": 0,
                "runtime_reactivation_api_count": 0,
                "runtime_repair_api_count": 0,
                "runtime_evidence_count": 0,
                "runtime_missing_evidence_count": 0,
                "verified": 0,
                "failed": 0,
                "skipped": 0,
                "unknown": 0,
                "approval_required_operations": 0,
                "missing_adapter_operations": 0,
                "no_endpoint_operations": 0,
            },
        )
        state["api_count"] += 1
        rows = dependencies_by_id.get(dataset_id, [])
        if not rows:
            state["uncovered_api_count"] += 1
            uncovered_apis.append(api_row(row, "materialize_operation_mapping", "no_dependency_rows"))
            continue

        summary = dependency_summary(rows, evidence_by_key)
        state["covered_api_count"] += 1
        state["operation_count"] += summary["operation_count"]
        state["runtime_evidence_count"] += summary["runtime_evidence_count"]
        state["runtime_missing_evidence_count"] += summary["runtime_missing_evidence_count"]
        for key in ["verified", "failed", "skipped", "unknown"]:
            state[key] += summary[key]
        state["approval_required_operations"] += summary["approval_required_operations"]
        state["missing_adapter_operations"] += summary["missing_adapter_operations"]
        state["no_endpoint_operations"] += summary["no_endpoint_operations"]

        if dataset_id in evidence_ids:
            state["runtime_evidence_api_count"] += 1
        else:
            state["runtime_reactivation_api_count"] += 1
            item = api_row(row, "collect_runtime_evidence", "operation_mapped_without_runtime_evidence")
            item.update(summary)
            runtime_reactivation_apis.append(item)

        if dataset_id in failed_ids:
            state["runtime_repair_api_count"] += 1
            item = api_row(row, "repair_failed_runtime_evidence", "latest_runtime_evidence_failed")
            item.update(summary)
            runtime_repair_apis.append(item)

    institutions = []
    for state in institution_state.values():
        api_count = int(state["api_count"])
        covered_api_count = int(state["covered_api_count"])
        operation_count = int(state["operation_count"])
        runtime_evidence_count = int(state["runtime_evidence_count"])
        state["api_operation_coverage_percent"] = percent(covered_api_count, api_count)
        state["runtime_evidence_percent"] = percent(runtime_evidence_count, operation_count)
        state["priority_score"] = int(state["uncovered_api_count"]) + int(state["runtime_reactivation_api_count"])
        institutions.append(state)

    institutions.sort(
        key=lambda row: (
            -int(row["priority_score"]),
            -int(row["uncovered_api_count"]),
            -int(row["runtime_reactivation_api_count"]),
            str(row["organization"]),
        )
    )
    uncovered_apis.sort(key=lambda row: (str(row["organization"]), str(row["dataset_id"])))
    runtime_reactivation_apis.sort(
        key=lambda row: (
            str(row["organization"]),
            -int(row["operation_count"]),
            str(row["dataset_id"]),
        )
    )
    runtime_repair_apis.sort(
        key=lambda row: (
            str(row["organization"]),
            -int(row["failed"]),
            str(row["dataset_id"]),
        )
    )

    api_total = len(registry_by_id)
    covered_api_total = sum(1 for dataset_id in registry_by_id if dataset_id in dependencies_by_id)
    operation_total = len(dependency_rows)
    runtime_evidence_api_total = len(evidence_ids.intersection(registry_by_id))
    runtime_reactivation_api_total = covered_api_total - runtime_evidence_api_total
    latest_summary = as_dict(latest.get("summary"), "latest.summary")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dependencies.get("generated_at"),
        "provider": dependencies.get("provider"),
        "source_id": "data_go_kr",
        "generation_inputs": {
            "registry": str(registry_path).replace("\\", "/"),
            "dependencies": str(dependencies_path).replace("\\", "/"),
            "latest_verification": str(latest_verification_path).replace("\\", "/"),
        },
        "summary": {
            "institutions": len(institutions),
            "api_total": api_total,
            "covered_api_count": covered_api_total,
            "uncovered_api_count": api_total - covered_api_total,
            "api_operation_coverage_percent": percent(covered_api_total, api_total),
            "operation_total": operation_total,
            "runtime_evidence_operation_count": int(latest_summary.get("total", 0)),
            "runtime_evidence_operation_percent": percent(int(latest_summary.get("total", 0)), operation_total),
            "runtime_verified_operation_count": int(latest_summary.get("verified", 0)),
            "runtime_failed_operation_count": int(latest_summary.get("failed", 0)),
            "runtime_skipped_operation_count": int(latest_summary.get("skipped", 0)),
            "runtime_evidence_api_count": runtime_evidence_api_total,
            "runtime_reactivation_api_count": runtime_reactivation_api_total,
            "runtime_repair_api_count": len(runtime_repair_apis),
        },
        "institutions": institutions,
        "uncovered_apis": uncovered_apis,
        "runtime_reactivation_apis": runtime_reactivation_apis,
        "runtime_repair_apis": runtime_repair_apis,
    }


def md(value: object) -> str:
    return str(value).replace("|", "\\|")


def table(headers: list[str], rows: list[list[object]]) -> str:
    align = ["---"] + ["---:"] * (len(headers) - 1)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(align) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md(item) for item in row) + " |")
    return "\n".join(lines)


def build_markdown(report: dict[str, Any], limit: int) -> str:
    summary = as_dict(report.get("summary"), "summary")
    institutions = as_list(report.get("institutions"), "institutions")
    priority_rows = [
        [
            row["organization"],
            row["api_count"],
            row["covered_api_count"],
            row["uncovered_api_count"],
            f"{row['api_operation_coverage_percent']}%",
            row["operation_count"],
            row["runtime_evidence_api_count"],
            row["runtime_reactivation_api_count"],
            row["runtime_repair_api_count"],
            row["priority_score"],
        ]
        for row in institutions[:limit]
        if isinstance(row, dict)
    ]
    uncovered_rows = [
        [row["organization"], row["dataset_id"], row["title"], row["source_category"], row["updated_at"]]
        for row in as_list(report.get("uncovered_apis"), "uncovered_apis")[:limit]
        if isinstance(row, dict)
    ]
    runtime_rows = [
        [
            row["organization"],
            row["dataset_id"],
            row["title"],
            row["operation_count"],
            row["runtime_missing_evidence_count"],
            row["missing_adapter_operations"],
            row["approval_required_operations"],
        ]
        for row in as_list(report.get("runtime_reactivation_apis"), "runtime_reactivation_apis")[:limit]
        if isinstance(row, dict)
    ]

    return (
        "# data.go.kr Coverage Backlog\n\n"
        "This backlog is generated from the checked-in registry, dependency report, "
        "and latest runtime verification evidence. It separates APIs with no "
        "operation mapping from APIs that have operations but still need runtime "
        "evidence collection.\n\n"
        f"- Generated at: `{report.get('generated_at')}`\n"
        f"- Institutions: `{summary.get('institutions')}`\n"
        f"- APIs: `{summary.get('api_total')}`\n"
        f"- APIs with operation mapping: `{summary.get('covered_api_count')}` "
        f"(`{summary.get('api_operation_coverage_percent')}%`)\n"
        f"- APIs without operation mapping: `{summary.get('uncovered_api_count')}`\n"
        f"- Operations: `{summary.get('operation_total')}`\n"
        f"- Runtime evidence operations: `{summary.get('runtime_evidence_operation_count')}` "
        f"(`{summary.get('runtime_evidence_operation_percent')}%`)\n"
        f"- Runtime verified operations: `{summary.get('runtime_verified_operation_count')}`\n"
        f"- Runtime failed operations: `{summary.get('runtime_failed_operation_count')}`\n"
        f"- Runtime skipped operations: `{summary.get('runtime_skipped_operation_count')}`\n"
        f"- APIs with runtime evidence: `{summary.get('runtime_evidence_api_count')}`\n"
        f"- Runtime reactivation APIs: `{summary.get('runtime_reactivation_api_count')}`\n"
        f"- Runtime repair APIs: `{summary.get('runtime_repair_api_count')}`\n\n"
        "## Highest Priority Institutions\n\n"
        f"{table(['Institution', 'APIs', 'Covered APIs', 'Uncovered APIs', 'API Coverage', 'Ops', 'Runtime APIs', 'Runtime Reactivation APIs', 'Runtime Repair APIs', 'Priority'], priority_rows)}\n\n"
        "## Sample Uncovered APIs\n\n"
        f"{table(['Institution', 'API ID', 'Title', 'Category', 'Updated'], uncovered_rows)}\n\n"
        "## Sample Runtime Reactivation APIs\n\n"
        f"{table(['Institution', 'API ID', 'Title', 'Ops', 'Missing Evidence Ops', 'Missing Adapter Ops', 'Approval Ops'], runtime_rows)}\n\n"
        "The full machine-readable backlog is "
        "`reports/data-go-kr/coverage-backlog.json`.\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="data/data-go-kr.registry.json", type=pathlib.Path)
    parser.add_argument("--dependencies", default="reports/dependencies.json", type=pathlib.Path)
    parser.add_argument("--latest-verification", default="reports/latest-verification.json", type=pathlib.Path)
    parser.add_argument("--output", default="reports/data-go-kr/coverage-backlog.json", type=pathlib.Path)
    parser.add_argument("--markdown-output", default="docs/data-go-kr-coverage-backlog.md", type=pathlib.Path)
    parser.add_argument("--markdown-limit", default=30, type=int)
    args = parser.parse_args()

    report = build_report(args.registry, args.dependencies, args.latest_verification)
    write_json(args.output, report)
    write_text(args.markdown_output, build_markdown(report, args.markdown_limit))
    print(
        f"wrote {args.output} and {args.markdown_output} "
        f"(uncovered={report['summary']['uncovered_api_count']}, "
        f"runtime_reactivation={report['summary']['runtime_reactivation_api_count']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
