#!/usr/bin/env python3
"""Generate institution-scoped data.go.kr operation materialization batches."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
from typing import Any


SCHEMA_VERSION = "datapan.operation-materialization-plan.v1"


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


def as_dict(value: Any, label: str | pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def portable_path(path: pathlib.Path) -> str:
    return str(path).replace("\\", "/")


def percent(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round((part / whole) * 100, 1)


def api_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("updated_at") or ""), str(row.get("dataset_id") or ""))


def build_report(
    coverage_backlog_path: pathlib.Path,
    institution_limit: int,
    batch_size: int,
    sample_limit: int,
) -> dict[str, Any]:
    backlog = as_dict(load_json(coverage_backlog_path), coverage_backlog_path)
    summary = as_dict(backlog.get("summary"), "coverage_backlog.summary")
    institutions = [
        row
        for row in as_list(backlog.get("institutions"), "coverage_backlog.institutions")
        if isinstance(row, dict) and int(row.get("uncovered_api_count") or 0) > 0
    ]
    uncovered_apis = [
        row
        for row in as_list(backlog.get("uncovered_apis"), "coverage_backlog.uncovered_apis")
        if isinstance(row, dict)
    ]

    apis_by_institution: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in uncovered_apis:
        organization = str(row.get("organization") or "Unknown")
        apis_by_institution[organization].append(row)
    for rows in apis_by_institution.values():
        rows.sort(key=api_sort_key, reverse=True)

    institutions.sort(
        key=lambda row: (
            -int(row.get("api_count") or 0),
            -int(row.get("uncovered_api_count") or 0),
            str(row.get("organization") or ""),
        )
    )

    batches = []
    planned_api_count = 0
    for index, row in enumerate(institutions[:institution_limit], start=1):
        organization = str(row.get("organization") or "Unknown")
        apis = apis_by_institution.get(organization, [])
        planned = min(len(apis), batch_size)
        planned_api_count += planned
        batches.append(
            {
                "label": f"institution-{index:02d}",
                "rank": index,
                "organization": organization,
                "api_count": int(row.get("api_count") or 0),
                "covered_api_count": int(row.get("covered_api_count") or 0),
                "uncovered_api_count": int(row.get("uncovered_api_count") or 0),
                "api_operation_coverage_percent": float(row.get("api_operation_coverage_percent") or 0),
                "planned_api_count": planned,
                "remaining_after_batch": max(0, len(apis) - planned),
                "action": "materialize_operation_mapping",
                "reason": "no_dependency_rows",
                "output": portable_path(pathlib.Path("reports/data-go-kr/operation-materialization-batches") / f"institution-{index:02d}.json"),
                "apis": apis[:planned],
                "sample_apis": apis[:sample_limit],
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": backlog.get("generated_at"),
        "provider": backlog.get("provider"),
        "source_id": backlog.get("source_id"),
        "generation_inputs": {
            "coverage_backlog": portable_path(coverage_backlog_path),
        },
        "policy": {
            "selection": "largest_institutions_with_uncovered_operation_mappings",
            "institution_limit": institution_limit,
            "batch_size": batch_size,
            "sample_limit": sample_limit,
        },
        "summary": {
            "institutions": int(summary.get("institutions") or 0),
            "api_total": int(summary.get("api_total") or 0),
            "covered_api_count": int(summary.get("covered_api_count") or 0),
            "uncovered_api_count": int(summary.get("uncovered_api_count") or 0),
            "api_operation_coverage_percent": float(summary.get("api_operation_coverage_percent") or 0),
            "planned_institutions": len(batches),
            "planned_api_count": planned_api_count,
            "first_queue": batches[0]["organization"] if batches else "",
        },
        "batches": batches,
        "next": [
            {
                "label": "materialize-operation-mapping",
                "action": "For each planned API, inspect the data.go.kr metadata URL, extract callable operation endpoints and required parameters, then regenerate dependencies and coverage backlog.",
            },
            {
                "label": "regenerate-derived-reports",
                "commands": [
                    "python scripts/generate-coverage-backlog.py",
                    "python scripts/generate-operation-materialization-plan.py",
                    "python scripts/generate-institution-api-overview.py",
                    "python scripts/generate-institution-runtime-plan.py",
                ],
            },
        ],
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


def build_markdown(report: dict[str, Any], markdown_limit: int) -> str:
    summary = as_dict(report.get("summary"), "summary")
    policy = as_dict(report.get("policy"), "policy")
    batches = as_list(report.get("batches"), "batches")
    batch_rows = [
        [
            row["rank"],
            row["organization"],
            row["api_count"],
            row["covered_api_count"],
            row["uncovered_api_count"],
            f"{row['api_operation_coverage_percent']}%",
            row["planned_api_count"],
            row["remaining_after_batch"],
        ]
        for row in batches[:markdown_limit]
        if isinstance(row, dict)
    ]
    sample_rows: list[list[object]] = []
    for batch in batches[: min(len(batches), 3)]:
        if not isinstance(batch, dict):
            continue
        for api in as_list(batch.get("sample_apis"), "sample_apis")[:5]:
            if not isinstance(api, dict):
                continue
            sample_rows.append(
                [
                    batch.get("organization"),
                    api.get("dataset_id"),
                    api.get("title"),
                    api.get("source_category"),
                    api.get("data_format"),
                    api.get("dev_approval"),
                    api.get("prod_approval"),
                    api.get("updated_at"),
                ]
            )

    return (
        "# data.go.kr Operation Materialization Plan\n\n"
        "This plan is generated from `reports/data-go-kr/coverage-backlog.json` "
        "and turns APIs without operation mappings into bounded institution work queues. "
        "It is separate from runtime evidence reactivation: these APIs need operation "
        "metadata materialized before they can enter verification batches.\n\n"
        f"- Generated at: `{report.get('generated_at')}`\n"
        f"- Institutions: `{summary.get('institutions')}`\n"
        f"- APIs: `{summary.get('api_total')}`\n"
        f"- APIs with operation mapping: `{summary.get('covered_api_count')}` "
        f"(`{summary.get('api_operation_coverage_percent')}%`)\n"
        f"- APIs without operation mapping: `{summary.get('uncovered_api_count')}`\n"
        f"- Planned institutions: `{summary.get('planned_institutions')}`\n"
        f"- Planned APIs: `{summary.get('planned_api_count')}`\n"
        f"- First queue: `{summary.get('first_queue')}`\n"
        f"- Batch size: `{policy.get('batch_size')}`\n\n"
        "## Planned Institution Batches\n\n"
        f"{table(['Rank', 'Institution', 'APIs', 'Covered APIs', 'Uncovered APIs', 'API Coverage', 'Planned APIs', 'Remaining After Batch'], batch_rows)}\n\n"
        "## Sample APIs To Materialize\n\n"
        f"{table(['Institution', 'API ID', 'Title', 'Category', 'Format', 'Dev Approval', 'Prod Approval', 'Updated'], sample_rows)}\n\n"
        "## Regeneration Loop\n\n"
        "After materializing operation mappings for a batch, regenerate the coverage "
        "backlog, this plan, the institution API overview, and the institution runtime "
        "plan. APIs that gain operations should leave this plan and enter runtime "
        "reactivation if they still lack verification evidence.\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-backlog", default="reports/data-go-kr/coverage-backlog.json", type=pathlib.Path)
    parser.add_argument("--institution-limit", default=10, type=int)
    parser.add_argument("--batch-size", default=100, type=int)
    parser.add_argument("--sample-limit", default=10, type=int)
    parser.add_argument("--output", default="reports/data-go-kr/operation-materialization-plan.json", type=pathlib.Path)
    parser.add_argument("--markdown-output", default="docs/data-go-kr-operation-materialization-plan.md", type=pathlib.Path)
    parser.add_argument("--markdown-limit", default=30, type=int)
    args = parser.parse_args()

    if args.institution_limit <= 0:
        raise ValueError("--institution-limit must be positive")
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be positive")
    if args.sample_limit <= 0:
        raise ValueError("--sample-limit must be positive")

    report = build_report(args.coverage_backlog, args.institution_limit, args.batch_size, args.sample_limit)
    write_json(args.output, report)
    write_text(args.markdown_output, build_markdown(report, args.markdown_limit))
    print(
        f"wrote {args.output} and {args.markdown_output} "
        f"(first_queue={report['summary']['first_queue']}, planned_apis={report['summary']['planned_api_count']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
