#!/usr/bin/env python3
"""Generate adapter-safe link-detail patches for one materialization batch."""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "datapan.link-detail-registry-patches.v1"
ANCHOR_RE = re.compile(r"(?is)<a\b[^>]*>")
HREF_RE = re.compile(r"(?is)\bhref\s*=\s*[\"']([^\"']+)[\"']")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def registered_hosts(provider_index: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for adapter in provider_index.get("adapters") or []:
        for host in adapter.get("hosts") or []:
            host = str(host).strip().lower()
            if host:
                out.add(host)
    return out


def data_go_kr_application_url(dataset_id: str) -> str:
    return f"https://www.data.go.kr/data/{dataset_id}/openapi.do"


def fetch_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "datapan-registry-link-detail-batch/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")


def extract_link_detail_operation_urls(page_html: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in ANCHOR_RE.findall(page_html):
        if "fn_LinkApiRequest" not in tag:
            continue
        match = HREF_RE.search(tag)
        if not match:
            continue
        raw = html.unescape(match.group(1)).strip()
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if parsed.hostname.lower() in {"data.go.kr", "www.data.go.kr"}:
            continue
        if raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


def operation_name(row: dict[str, Any], index: int, total: int) -> str:
    raw = ((row.get("source") or {}).get("raw") or {}) if isinstance(row.get("source"), dict) else {}
    name = str(raw.get("title") or row.get("title") or raw.get("list_title") or "").strip()
    if total <= 1:
        return name
    return f"{name} 외부 링크 {index + 1}"


def operation(row: dict[str, Any], endpoint: str, index: int, total: int) -> dict[str, Any]:
    raw = dict(((row.get("source") or {}).get("raw") or {}) if isinstance(row.get("source"), dict) else {})
    name = operation_name(row, index, total)
    raw["operation_nm"] = name
    raw["operation_url"] = endpoint
    return {
        "name": name,
        "endpoint": endpoint,
        "source": {
            "system": "data.go.kr",
            "url": str(raw.get("meta_url") or row.get("source", {}).get("url") or data_go_kr_application_url(str(row.get("id") or ""))),
            "raw": raw,
        },
    }


def operation_host(op: dict[str, Any]) -> str:
    return (urllib.parse.urlparse(str(op.get("endpoint") or "")).hostname or "").lower()


def batch_apis(batch: dict[str, Any]) -> list[dict[str, Any]]:
    apis = batch.get("apis")
    if not isinstance(apis, list):
        raise ValueError("batch.apis must be an array")
    return [api for api in apis if isinstance(api, dict)]


def build_report(
    registry: list[Any],
    batch: dict[str, Any],
    provider_index: dict[str, Any],
    *,
    limit: int,
    delay: float,
    timeout: float,
) -> dict[str, Any]:
    hosts = registered_hosts(provider_index)
    rows_by_id = {str(row.get("id") or ""): row for row in registry if isinstance(row, dict)}
    patches: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    fetched = 0
    for api in batch_apis(batch):
        if limit > 0 and fetched >= limit:
            skipped.append({"dataset_id": str(api.get("dataset_id") or ""), "reason": "limit_reached"})
            continue
        dataset_id = str(api.get("dataset_id") or "")
        row = rows_by_id.get(dataset_id)
        if not row:
            skipped.append({"dataset_id": dataset_id, "reason": "missing_registry_row"})
            continue
        if row.get("operations"):
            skipped.append({"dataset_id": dataset_id, "reason": "already_has_operations"})
            continue
        url = data_go_kr_application_url(dataset_id)
        try:
            body = fetch_text(url, timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            failed.append({"dataset_id": dataset_id, "reason": "fetch_failed", "message": str(exc)})
            continue
        fetched += 1
        links = extract_link_detail_operation_urls(body)
        if delay > 0:
            time.sleep(delay)
        if not links:
            skipped.append({"dataset_id": dataset_id, "reason": "missing_link_detail_operations"})
            continue
        operations = [operation(row, link, index, len(links)) for index, link in enumerate(links)]
        operation_hosts = [operation_host(op) for op in operations]
        missing_hosts = sorted({host for host in operation_hosts if host not in hosts})
        if missing_hosts:
            skipped.append(
                {
                    "dataset_id": dataset_id,
                    "title": row.get("title"),
                    "organization": row.get("organization"),
                    "operation_count": len(operations),
                    "reason": "unregistered_adapter_host",
                    "hosts": missing_hosts,
                }
            )
            continue
        patches.append(
            {
                "dataset_id": dataset_id,
                "title": row.get("title"),
                "organization": row.get("organization"),
                "action": "replace_empty_operations",
                "operation_count": len(operations),
                "hosts": sorted(set(operation_hosts)),
                "operations": operations,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "summary": {
            "batch_label": batch.get("label"),
            "organization": batch.get("organization"),
            "input_apis": len(batch_apis(batch)),
            "details_fetched": fetched,
            "patches": len(patches),
            "operations_to_add": sum(int(patch["operation_count"]) for patch in patches),
            "skipped": len(skipped),
            "failed": len(failed),
            "registered_hosts": len(hosts),
        },
        "patches": patches,
        "skipped": skipped,
        "failed": failed,
    }


def write_markdown(path: pathlib.Path, report: dict[str, Any]) -> None:
    summary = report["summary"]
    lines = [
        "# data.go.kr Batch Link Detail Registry Patches",
        "",
        "This report fetches one operation-materialization batch from public data.go.kr detail pages and keeps only operations whose hosts already have registered adapters.",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Batch: `{summary.get('batch_label')}`",
        f"- Organization: `{summary.get('organization')}`",
        f"- Input APIs: `{summary['input_apis']}`",
        f"- Details fetched: `{summary['details_fetched']}`",
        f"- Patches: `{summary['patches']}`",
        f"- Operations to add: `{summary['operations_to_add']}`",
        f"- Skipped: `{summary['skipped']}`",
        f"- Failed: `{summary['failed']}`",
        "",
        "## Patches",
        "",
        "| Dataset ID | Organization | Title | Operations | Hosts |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for patch in report["patches"]:
        lines.append(
            f"| {patch['dataset_id']} | {patch.get('organization') or ''} | {patch.get('title') or ''} | {patch['operation_count']} | {', '.join(patch.get('hosts') or [])} |"
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
    parser.add_argument("--batch", required=True, type=pathlib.Path)
    parser.add_argument("--provider-index", default="data/provider-index.json", type=pathlib.Path)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--output", default="reports/data-go-kr/link-detail-registry-patches.json", type=pathlib.Path)
    parser.add_argument("--markdown-output", default="docs/data-go-kr-link-detail-registry-patches.md", type=pathlib.Path)
    args = parser.parse_args()
    report = build_report(
        load_json(args.registry),
        load_json(args.batch),
        load_json(args.provider_index),
        limit=args.limit,
        delay=args.delay,
        timeout=args.timeout,
    )
    write_json(args.output, report)
    write_markdown(args.markdown_output, report)
    summary = report["summary"]
    print(
        f"wrote {args.output} and {args.markdown_output} "
        f"(patches={summary['patches']}, operations={summary['operations_to_add']}, skipped={summary['skipped']}, failed={summary['failed']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
