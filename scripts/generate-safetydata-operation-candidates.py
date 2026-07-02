#!/usr/bin/env python3
"""Discover Safety Data operation candidates from data.go.kr materialization batches."""

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
from typing import Any


SCHEMA_VERSION = "datapan.safetydata-operation-candidates.v1"
USER_AGENT = "Mozilla/5.0 (compatible; datapan-registry/operation-discovery)"


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


def fetch_text(url: str, timeout: int, referer: str = "") -> str:
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
        headers["X-Requested-With"] = "XMLHttpRequest"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-provided public URLs
        return response.read().decode("utf-8", "replace")


def strip_tags(value: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def find_safetydata_link(data_go_kr_html: str) -> str:
    match = re.search(r"https://www\.safetydata\.go\.kr/disaster-data/view\?dataSn=(\d+)", data_go_kr_html)
    if match:
        return match.group(0)
    return ""


def data_sn_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    values = query.get("dataSn") or []
    return values[0] if values else ""


def parse_url_path(api_html: str) -> str:
    match = re.search(r"<th>\s*URL 주소\s*</th>\s*<td[^>]*>([\s\S]*?)</td>", api_html, flags=re.I)
    if not match:
        return ""
    text = strip_tags(match.group(1)).replace("이용가이드", "").strip()
    for token in text.split():
        if token.startswith("http://") or token.startswith("https://") or token.startswith("/"):
            return token
    return text


def parse_param_rows(section_html: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row_html in re.findall(r"<tr[\s\S]*?</tr>", section_html, flags=re.I):
        cells = [strip_tags(cell) for cell in re.findall(r"<td[^>]*>([\s\S]*?)</td>", row_html, flags=re.I)]
        if len(cells) < 6:
            continue
        rows.append(
            {
                "name_ko": cells[0],
                "name": cells[1],
                "type": cells[2],
                "size": cells[3],
                "required": cells[4] == "Y",
                "description": cells[5],
            }
        )
    return rows


def section_between(body: str, start: str, end: str) -> str:
    start_index = body.find(start)
    if start_index < 0:
        return ""
    end_index = body.find(end, start_index + len(start))
    if end_index < 0:
        end_index = len(body)
    return body[start_index:end_index]


def discover_api(api: dict[str, Any], timeout: int) -> dict[str, Any]:
    dataset_id = str(api.get("dataset_id") or "")
    result: dict[str, Any] = {
        "dataset_id": dataset_id,
        "title": api.get("title"),
        "organization": api.get("organization"),
        "meta_url": api.get("meta_url"),
        "status": "unknown",
    }
    try:
        meta_url = str(api.get("meta_url") or "")
        if not meta_url:
            result.update({"status": "skipped", "reason": "missing_meta_url"})
            return result
        meta = as_dict(json.loads(fetch_text(meta_url, timeout)), f"{dataset_id}.meta")
        data_go_kr_url = str(meta.get("url") or "")
        if not data_go_kr_url:
            result.update({"status": "skipped", "reason": "missing_data_go_kr_url"})
            return result
        data_go_kr_html = fetch_text(data_go_kr_url, timeout)
        safetydata_url = find_safetydata_link(data_go_kr_html)
        if not safetydata_url:
            result.update({"status": "skipped", "reason": "missing_safetydata_link", "data_go_kr_url": data_go_kr_url})
            return result
        data_sn = data_sn_from_url(safetydata_url)
        if not data_sn:
            result.update({"status": "skipped", "reason": "missing_safetydata_data_sn", "data_go_kr_url": data_go_kr_url, "safetydata_url": safetydata_url})
            return result

        api_view_url = f"https://www.safetydata.go.kr/disaster-data/getApiView?dataSn={urllib.parse.quote(data_sn)}"
        api_table_url = f"https://www.safetydata.go.kr/disaster-data/apiDataTable?dataSn={urllib.parse.quote(data_sn)}"
        api_view = as_dict(json.loads(fetch_text(api_view_url, timeout, safetydata_url)), f"{dataset_id}.api_view")
        api_table = fetch_text(api_table_url, timeout, safetydata_url)
        url_path = parse_url_path(api_table)
        endpoint = url_path
        if endpoint.startswith("/"):
            endpoint = "https://www.safetydata.go.kr" + endpoint
        request_params = parse_param_rows(section_between(api_table, "요청변수", "출력결과"))
        response_fields = parse_param_rows(section_between(api_table, "출력결과", "샘플코드"))
        if not endpoint:
            result.update({"status": "failed", "reason": "missing_endpoint", "data_go_kr_url": data_go_kr_url, "safetydata_url": safetydata_url, "data_sn": data_sn})
            return result

        result.update(
            {
                "status": "candidate",
                "reason": "safetydata_open_api_metadata_extracted",
                "data_go_kr_url": data_go_kr_url,
                "safetydata_url": safetydata_url,
                "data_sn": data_sn,
                "api_view_url": api_view_url,
                "api_table_url": api_table_url,
                "source_api_name": api_view.get("dataNm"),
                "source_api_type": api_view.get("apiTypeCd"),
                "source_data_format": api_view.get("dataFrmtCd"),
                "source_interface_id": api_view.get("intrfId"),
                "method": "GET",
                "endpoint": endpoint,
                "request_params": request_params,
                "response_fields": response_fields,
                "required_params": [row["name"] for row in request_params if row.get("required")],
                "optional_params": [row["name"] for row in request_params if not row.get("required")],
                "operation_mapping_candidate": {
                    "name": api_view.get("dataNm") or api.get("title"),
                    "endpoint": endpoint,
                    "method": "GET",
                    "request_params": request_params,
                    "response_fields": response_fields,
                },
            }
        )
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        result.update({"status": "failed", "reason": type(exc).__name__, "message": str(exc)})
    return result


def build_report(batch_path: pathlib.Path, limit: int, timeout: int, delay: float) -> dict[str, Any]:
    batch = as_dict(load_json(batch_path), batch_path)
    apis = [row for row in as_list(batch.get("apis"), "batch.apis") if isinstance(row, dict)]
    if limit > 0:
        apis = apis[:limit]
    results = []
    for index, api in enumerate(apis):
        if index and delay > 0:
            time.sleep(delay)
        results.append(discover_api(api, timeout))

    counts: dict[str, int] = {}
    for row in results:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": "data.go.kr",
        "source_id": "data_go_kr",
        "discovery_source": "safetydata.go.kr",
        "generation_inputs": {
            "batch": portable_path(batch_path),
        },
        "policy": {
            "limit": limit,
            "timeout_seconds": timeout,
            "delay_seconds": delay,
        },
        "summary": {
            "batch_label": batch.get("label"),
            "organization": batch.get("organization"),
            "input_apis": len(apis),
            "candidates": counts.get("candidate", 0),
            "skipped": counts.get("skipped", 0),
            "failed": counts.get("failed", 0),
        },
        "results": results,
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
    for row in as_list(report.get("results"), "results"):
        if not isinstance(row, dict):
            continue
        rows.append(
            [
                row.get("dataset_id"),
                row.get("status"),
                row.get("reason"),
                row.get("endpoint", ""),
                ", ".join(row.get("required_params") or []),
            ]
        )
    return (
        "# data.go.kr Safety Data Operation Candidates\n\n"
        "This live-discovery snapshot is generated from an operation materialization "
        "batch and public `data.go.kr` / `safetydata.go.kr` metadata. It identifies "
        "APIs that can be converted from uncovered API rows into operation mappings.\n\n"
        f"- Generated at: `{report.get('generated_at')}`\n"
        f"- Batch: `{summary.get('batch_label')}`\n"
        f"- Organization: `{summary.get('organization')}`\n"
        f"- Input APIs: `{summary.get('input_apis')}`\n"
        f"- Candidates: `{summary.get('candidates')}`\n"
        f"- Skipped: `{summary.get('skipped')}`\n"
        f"- Failed: `{summary.get('failed')}`\n\n"
        "## Results\n\n"
        f"{table(['API ID', 'Status', 'Reason', 'Endpoint', 'Required Params'], rows)}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", default="reports/data-go-kr/operation-materialization-batches/institution-01.json", type=pathlib.Path)
    parser.add_argument("--limit", default=5, type=int)
    parser.add_argument("--timeout", default=20, type=int)
    parser.add_argument("--delay", default=0.2, type=float)
    parser.add_argument("--output", default="reports/data-go-kr/safetydata-operation-candidates.json", type=pathlib.Path)
    parser.add_argument("--markdown-output", default="docs/data-go-kr-safetydata-operation-candidates.md", type=pathlib.Path)
    args = parser.parse_args()

    if args.limit < 0:
        raise ValueError("--limit must be non-negative")
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.delay < 0:
        raise ValueError("--delay must be non-negative")

    report = build_report(args.batch, args.limit, args.timeout, args.delay)
    write_json(args.output, report)
    write_text(args.markdown_output, build_markdown(report))
    print(
        f"wrote {args.output} and {args.markdown_output} "
        f"(candidates={report['summary']['candidates']}, skipped={report['summary']['skipped']}, failed={report['summary']['failed']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
