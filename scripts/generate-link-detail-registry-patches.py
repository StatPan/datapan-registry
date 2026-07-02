#!/usr/bin/env python3
"""Generate adapter-safe link-detail operation patches from an enriched registry."""

from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = "datapan.link-detail-registry-patches.v1"


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def operation_host(operation: dict[str, Any]) -> str:
    endpoint = str(operation.get("endpoint") or "")
    return (urlparse(endpoint).hostname or "").lower()


def registered_hosts(provider_index: dict[str, Any]) -> set[str]:
    hosts: set[str] = set()
    for adapter in provider_index.get("adapters") or []:
        if not isinstance(adapter, dict):
            continue
        for host in adapter.get("hosts") or []:
            host = str(host).strip().lower()
            if host:
                hosts.add(host)
    return hosts


def build_report(registry: list[Any], enriched: list[Any], provider_index: dict[str, Any]) -> dict[str, Any]:
    hosts = registered_hosts(provider_index)
    registry_by_id = {
        str(row.get("id") or ""): row
        for row in registry
        if isinstance(row, dict) and row.get("id")
    }
    patches: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for raw_row in enriched:
        if not isinstance(raw_row, dict):
            continue
        dataset_id = str(raw_row.get("id") or "")
        if not dataset_id:
            continue
        base_row = registry_by_id.get(dataset_id)
        if not isinstance(base_row, dict):
            skipped.append({"dataset_id": dataset_id, "reason": "missing_registry_row"})
            continue
        base_ops = base_row.get("operations") or []
        enriched_ops = raw_row.get("operations") or []
        if base_ops or not enriched_ops:
            continue
        operation_hosts = [operation_host(op) for op in enriched_ops if isinstance(op, dict)]
        if len(operation_hosts) != len(enriched_ops) or not operation_hosts:
            skipped.append({"dataset_id": dataset_id, "reason": "missing_operation_host"})
            continue
        missing_hosts = sorted({host for host in operation_hosts if host not in hosts})
        if missing_hosts:
            skipped.append(
                {
                    "dataset_id": dataset_id,
                    "title": raw_row.get("title"),
                    "organization": raw_row.get("organization"),
                    "operation_count": len(enriched_ops),
                    "reason": "unregistered_adapter_host",
                    "hosts": missing_hosts,
                }
            )
            continue
        patches.append(
            {
                "dataset_id": dataset_id,
                "title": raw_row.get("title"),
                "organization": raw_row.get("organization"),
                "action": "replace_empty_operations",
                "operation_count": len(enriched_ops),
                "hosts": sorted(set(operation_hosts)),
                "operations": enriched_ops,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "summary": {
            "enriched_specs": len(enriched),
            "patches": len(patches),
            "operations_to_add": sum(int(patch["operation_count"]) for patch in patches),
            "skipped": len(skipped),
            "registered_hosts": len(hosts),
        },
        "patches": patches,
        "skipped": skipped,
    }


def write_markdown(path: pathlib.Path, report: dict[str, Any]) -> None:
    summary = report["summary"]
    lines = [
        "# data.go.kr Link Detail Registry Patches",
        "",
        "This report is generated from a bounded `datapan catalog enrich link-details` output and keeps only operations whose hosts already have registered adapters.",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Patches: `{summary['patches']}`",
        f"- Operations to add: `{summary['operations_to_add']}`",
        f"- Skipped enriched rows: `{summary['skipped']}`",
        "",
        "## Patches",
        "",
        "| Dataset ID | Organization | Title | Operations | Hosts |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for patch in report["patches"]:
        hosts = ", ".join(patch.get("hosts") or [])
        lines.append(
            f"| {patch['dataset_id']} | {patch.get('organization') or ''} | {patch.get('title') or ''} | {patch['operation_count']} | {hosts} |"
        )
    lines.extend(["", "## Skipped Reasons", ""])
    counts: dict[str, int] = {}
    for row in report["skipped"]:
        reason = str(row.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    for reason, count in sorted(counts.items()):
        lines.append(f"- `{reason}`: `{count}`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="data/data-go-kr.registry.json", type=pathlib.Path)
    parser.add_argument("--enriched", required=True, type=pathlib.Path)
    parser.add_argument("--provider-index", default="data/provider-index.json", type=pathlib.Path)
    parser.add_argument("--output", default="reports/data-go-kr/link-detail-registry-patches.json", type=pathlib.Path)
    parser.add_argument("--markdown-output", default="docs/data-go-kr-link-detail-registry-patches.md", type=pathlib.Path)
    args = parser.parse_args()

    report = build_report(
        load_json(args.registry),
        load_json(args.enriched),
        load_json(args.provider_index),
    )
    write_json(args.output, report)
    write_markdown(args.markdown_output, report)
    summary = report["summary"]
    print(
        f"wrote {args.output} and {args.markdown_output} "
        f"(patches={summary['patches']}, operations={summary['operations_to_add']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
