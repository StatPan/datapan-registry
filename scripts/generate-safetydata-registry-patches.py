#!/usr/bin/env python3
"""Generate registry operation patches from Safety Data candidates."""

from __future__ import annotations

import argparse
import json
import pathlib
import time
from typing import Any


SCHEMA_VERSION = "datapan.safetydata-registry-patches.v1"


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


def param(row: dict[str, Any]) -> dict[str, Any]:
    value = {
        "name": row.get("name"),
        "label": row.get("name_ko") or row.get("description") or row.get("name"),
    }
    if row.get("required") is True:
        value["required"] = True
    return value


def build_operation(candidate: dict[str, Any]) -> dict[str, Any]:
    mapping = as_dict(candidate.get("operation_mapping_candidate"), "operation_mapping_candidate")
    return {
        "name": mapping.get("name") or candidate.get("title"),
        "endpoint": candidate.get("endpoint"),
        "method": "GET",
        "request_params": [
            param(row)
            for row in as_list(candidate.get("request_params"), "request_params")
            if isinstance(row, dict) and row.get("name")
        ],
        "response_params": [
            param(row)
            for row in as_list(candidate.get("response_fields"), "response_fields")
            if isinstance(row, dict) and row.get("name")
        ],
        "source": {
            "system": "safetydata.go.kr",
            "url": candidate.get("safetydata_url"),
            "raw": {
                "data_sn": candidate.get("data_sn"),
                "api_view_url": candidate.get("api_view_url"),
                "api_table_url": candidate.get("api_table_url"),
                "source_api_name": candidate.get("source_api_name"),
                "source_api_type": candidate.get("source_api_type"),
                "source_data_format": candidate.get("source_data_format"),
                "source_interface_id": candidate.get("source_interface_id"),
            },
        },
    }


def build_report(registry_path: pathlib.Path, candidates_path: pathlib.Path) -> dict[str, Any]:
    registry_rows = [row for row in as_list(load_json(registry_path), registry_path) if isinstance(row, dict)]
    registry_by_id = {str(row.get("id") or ""): row for row in registry_rows}
    candidates = as_dict(load_json(candidates_path), candidates_path)
    patches = []
    skipped = []

    for index, raw in enumerate(as_list(candidates.get("results"), "results")):
        candidate = as_dict(raw, f"results[{index}]")
        dataset_id = str(candidate.get("dataset_id") or "")
        registry_row = registry_by_id.get(dataset_id)
        if candidate.get("status") != "candidate":
            skipped.append({"dataset_id": dataset_id, "reason": "not_candidate"})
            continue
        if not registry_row:
            skipped.append({"dataset_id": dataset_id, "reason": "missing_registry_row"})
            continue
        if registry_row.get("operations"):
            skipped.append({"dataset_id": dataset_id, "reason": "already_has_operations"})
            continue
        operation = build_operation(candidate)
        patches.append(
            {
                "dataset_id": dataset_id,
                "title": registry_row.get("title"),
                "organization": registry_row.get("organization"),
                "action": "add_operation_mapping",
                "reason": "safetydata_operation_candidate",
                "operation_count": 1,
                "operations": [operation],
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": "data.go.kr",
        "source_id": "data_go_kr",
        "generation_inputs": {
            "registry": portable_path(registry_path),
            "safetydata_operation_candidates": portable_path(candidates_path),
        },
        "summary": {
            "candidate_results": len(as_list(candidates.get("results"), "results")),
            "patches": len(patches),
            "operations_to_add": sum(int(row["operation_count"]) for row in patches),
            "skipped": len(skipped),
        },
        "patches": patches,
        "skipped": skipped,
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


def build_markdown(report: dict[str, Any]) -> str:
    summary = as_dict(report.get("summary"), "summary")
    rows = []
    for patch in as_list(report.get("patches"), "patches"):
        if not isinstance(patch, dict):
            continue
        operation = as_list(patch.get("operations"), "operations")[0]
        rows.append(
            [
                patch.get("dataset_id"),
                patch.get("organization"),
                patch.get("title"),
                operation.get("endpoint"),
                len(operation.get("request_params") or []),
                len(operation.get("response_params") or []),
            ]
        )
    return (
        "# data.go.kr Safety Data Registry Patches\n\n"
        "This report converts checked Safety Data operation candidates into exact "
        "registry operation mappings. It is a reviewable patch plan, not a registry "
        "mutation by itself.\n\n"
        f"- Generated at: `{report.get('generated_at')}`\n"
        f"- Candidate results: `{summary.get('candidate_results')}`\n"
        f"- Patches: `{summary.get('patches')}`\n"
        f"- Operations to add: `{summary.get('operations_to_add')}`\n"
        f"- Skipped: `{summary.get('skipped')}`\n\n"
        "## Patches\n\n"
        f"{table(['API ID', 'Institution', 'Title', 'Endpoint', 'Request Params', 'Response Params'], rows)}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="data/data-go-kr.registry.json", type=pathlib.Path)
    parser.add_argument("--candidates", default="reports/data-go-kr/safetydata-operation-candidates.json", type=pathlib.Path)
    parser.add_argument("--output", default="reports/data-go-kr/safetydata-registry-patches.json", type=pathlib.Path)
    parser.add_argument("--markdown-output", default="docs/data-go-kr-safetydata-registry-patches.md", type=pathlib.Path)
    args = parser.parse_args()

    report = build_report(args.registry, args.candidates)
    write_json(args.output, report)
    write_text(args.markdown_output, build_markdown(report))
    print(
        f"wrote {args.output} and {args.markdown_output} "
        f"(patches={report['summary']['patches']}, operations={report['summary']['operations_to_add']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
