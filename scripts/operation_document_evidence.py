#!/usr/bin/env python3
"""Acquire and parse bounded, official data.go.kr operation documentation.

This tool never calls a registered provider operation. It reads only the
official catalogue/detail/guide routes, stores raw captures in a private
directory, and emits redacted digest-bound evidence for offline review.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import http.client
import ipaddress
import json
import multiprocessing
import os
import pathlib
import re
import socket
import ssl
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "reports/data-go-kr/operation-manifest.json"
QUEUE = ROOT / "reports/operation-document-evidence/queue.v1.jsonl"
RECONCILIATION = ROOT / "reports/operation-document-evidence/reconciliation.v1.json"
EVIDENCE_DIR = ROOT / "reports/operation-document-evidence"
SCHEMA_VERSION = "datapan.operation-document-evidence.v1"
PARSER_ID = "data-go-kr-operation-document-parser"
PARSER_VERSION = "1.0.0"
HOST = "www.data.go.kr"
DETAIL_ROUTE = "/tcs/dss/selectApiDetailFunction.do"
DOWNLOAD_ROUTE = "/cmm/cmm/fileDownload.do"
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_DETAIL_BYTES = 1024 * 1024
MAX_GUIDE_BYTES = 8 * 1024 * 1024
MAX_DOCX_ENTRIES = 256
MAX_DOCX_EXPANDED_BYTES = 16 * 1024 * 1024
MAX_DOCX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 35
MAX_BATCH_OPERATIONS = 50
SOURCE_PROFILES = {
    "data_go_kr_v1": {
        "host": HOST,
        "routes": ["/data/{dataset_id}/openapi.do", DETAIL_ROUTE, DOWNLOAD_ROUTE],
        "max_requests_per_operation": 3,
    },
    "safetydata_v1": {
        "host": "www.safetydata.go.kr",
        "routes": ["/disaster-data/getApiView", "/disaster-data/apiDataTable"],
        "max_requests_per_operation": 2,
    },
}
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


class EvidenceError(ValueError):
    """A fixed-message input, source, or parser rejection."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _json_line(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _strict_read_json(path: pathlib.Path) -> Any:
    def pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError("json_duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs_no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("json_input_invalid") from exc


def _safe_operation_identity(operation: dict[str, Any]) -> dict[str, Any]:
    provenance = operation.get("provenance")
    if not isinstance(provenance, dict):
        raise EvidenceError("operation_identity_missing")
    identity = {
        "operation_id": operation.get("operation_id"),
        "provider": provenance.get("provider"),
        "dataset_id": provenance.get("dataset_id"),
        "protocol": operation.get("protocol"),
        "source_system": provenance.get("source_system"),
        "upstream_operation_key": provenance.get("upstream_operation_key"),
        "operation_name": provenance.get("operation_name"),
    }
    if (
        not isinstance(identity["operation_id"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", identity["operation_id"])
        or identity["provider"] != "data.go.kr"
        or identity["source_system"] not in {"data.go.kr", "safetydata.go.kr"}
        or identity["protocol"] not in {"REST", "SOAP"}
        or not isinstance(identity["dataset_id"], str)
        or not re.fullmatch(r"[0-9]+", identity["dataset_id"])
        or not isinstance(identity["upstream_operation_key"], str)
        or not identity["upstream_operation_key"]
        or len(identity["upstream_operation_key"]) > 128
        or not isinstance(identity["operation_name"], str)
        or not identity["operation_name"]
    ):
        raise EvidenceError("operation_identity_invalid")
    return identity


def build_queue(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a stable queue over the exact registered REST+SOAP denominator."""
    source = manifest.get("source_snapshot", {})
    if source.get("path") != "data/data-go-kr.registry.json":
        raise EvidenceError("manifest_source_path_mismatch")
    if source.get("sha256") != "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0" or source.get("bytes") != 139155499:
        raise EvidenceError("manifest_snapshot_not_canonical")
    records = manifest.get("operations")
    summary = manifest.get("summary", {})
    if not isinstance(records, list) or len(records) != 12662 or summary.get("api_operations") != 12662:
        raise EvidenceError("manifest_denominator_mismatch")
    queue: list[dict[str, Any]] = []
    seen: set[str] = set()
    for operation in records:
        identity = _safe_operation_identity(operation)
        if identity["operation_id"] in seen:
            raise EvidenceError("manifest_operation_id_collision")
        seen.add(identity["operation_id"])
        source_profile_id = "safetydata_v1" if identity["source_system"] == "safetydata.go.kr" else "data_go_kr_v1"
        queue.append({
            "schema_version": "datapan.operation-document-work-item.v1",
            "operation_identity": identity,
            "status": "pending",
            "source_profile_id": source_profile_id,
        })
    queue.sort(key=lambda item: item["operation_identity"]["operation_id"])
    return queue


class _DocumentHtml(HTMLParser):
    """Small structural parser that retains no cell sample values in outputs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_depth = 0
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cell_tag: str | None = None
        self._select_target = False
        self._option: dict[str, str] | None = None
        self.options: list[dict[str, str]] = []
        self._option_text: list[str] = []
        self.hidden: dict[str, list[str]] = {}
        self.attachments: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values: dict[str, list[str | None]] = {}
        for key, value in attrs:
            values.setdefault(key.lower(), []).append(value)
        if tag.lower() == "table":
            if self._table_depth == 0:
                self._table = []
                self.tables.append(self._table)
            self._table_depth += 1
        elif self._table_depth and tag.lower() == "tr" and self._table_depth == 1:
            self._row = []
            assert self._table is not None
            self._table.append(self._row)
        elif self._table_depth and tag.lower() in {"td", "th"} and self._table_depth == 1 and self._row is not None:
            self._cell, self._cell_tag = [], tag.lower()
        elif tag.lower() == "select":
            ids = values.get("id", [])
            self._select_target = ids == ["open_api_detail_select"]
        elif tag.lower() == "option" and self._select_target:
            option_values = values.get("value", [])
            self._option = {"value": str(option_values[0] or "")} if len(option_values) == 1 else {"value": ""}
            self._option_text = []
        elif tag.lower() == "input":
            ids = values.get("id", [])
            field = ids[0] if len(ids) == 1 and ids[0] in {"publicDataPk", "publicDataDetailPk"} else None
            if field:
                raw_values = values.get("value", [])
                self.hidden.setdefault(field, []).append(str(raw_values[0] or "") if len(raw_values) == 1 else "")
        elif tag.lower() == "button":
            onclicks = values.get("onclick", [])
            if len(onclicks) == 1:
                match = re.fullmatch(r"\s*fn_fileDownload\s*\(\s*(['\"])(FILE_[0-9]+)\1\s*,\s*(['\"])([0-9]+)\3\s*\)\s*;?\s*", str(onclicks[0] or ""))
                if match:
                    self.attachments.append((match.group(2), match.group(4)))

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        if self._option is not None:
            self._option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._cell is not None and tag == self._cell_tag:
            assert self._row is not None
            self._row.append(" ".join(" ".join(self._cell).split()))
            self._cell = None
            self._cell_tag = None
        elif tag == "tr" and self._table_depth == 1:
            self._row = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0:
                self._table = None
        elif tag == "option" and self._option is not None:
            self._option["name"] = " ".join(" ".join(self._option_text).split())
            self.options.append(self._option)
            self._option = None
            self._option_text = []
        elif tag == "select":
            self._select_target = False


def _parse_html(raw: bytes) -> _DocumentHtml:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_PAGE_BYTES:
        raise EvidenceError("html_source_size_invalid")
    try:
        text = raw.decode("utf-8", errors="strict")
        parser = _DocumentHtml()
        parser.feed(text)
        parser.close()
        return parser
    except (UnicodeError, ValueError, AssertionError) as exc:
        raise EvidenceError("html_source_invalid") from exc


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s()（）:_-]+", "", value).casefold()


def _cell_locator(source_id: str, kind: str, table: int, row: int, cell: int, label: str | None = None, part: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"source_id": source_id, "kind": kind, "table_index": table, "row_index": row, "cell_index": cell}
    if label:
        result["column_label"] = label
    if part:
        result["part"] = part
    return result


def _find_param_tables(tables: list[list[list[str]]]) -> list[tuple[int, dict[str, int], list[list[str]]]]:
    result: list[tuple[int, dict[str, int], list[list[str]]]] = []
    for table_index, rows in enumerate(tables):
        for header_index, header in enumerate(rows[:2]):
            normalized = [_normalize_header(x) for x in header]
            indexes: dict[str, int] = {}
            for name, aliases in {
                "english_name": {"항목명영문", "영문항목명", "englishname", "parametername"},
                "korean_name": {"항목명국문", "국문항목명", "koreanname"},
                "size": {"항목크기", "size", "length"},
                "requiredness": {"항목구분", "필수여부", "required"},
                "sample": {"샘플데이터", "sampledata", "example"},
                "description": {"항목설명", "description"},
            }.items():
                hits = [i for i, value in enumerate(normalized) if value in aliases]
                if len(hits) == 1:
                    indexes[name] = hits[0]
            if {"english_name", "requiredness"} <= indexes.keys() and "sample" in indexes:
                result.append((table_index, indexes, rows[header_index + 1 :]))
                break
    return result


def _requiredness(value: str) -> tuple[str | None, str]:
    compact = re.sub(r"\s+", "", value).casefold()
    if compact in {"필", "필수", "required", "y", "yes"}:
        return "required", "documented"
    if compact in {"옵", "선택", "optional", "n", "no"}:
        return "optional", "documented"
    return None, "unknown"


def _safe_url_parts(value: str) -> dict[str, str] | None:
    value = html.unescape(value.strip())
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    if parsed.port not in {None, 80, 443}:
        return None
    return {"scheme": parsed.scheme.lower(), "host": parsed.hostname.lower(), "path": parsed.path or "/"}


def _docx_tables(raw: bytes) -> list[list[list[str]]]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_GUIDE_BYTES:
        raise EvidenceError("docx_source_size_invalid")
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(raw)) as archive:
            infos = archive.infolist()
            if not 1 <= len(infos) <= MAX_DOCX_ENTRIES:
                raise EvidenceError("docx_entry_count_invalid")
            expanded = 0
            seen: set[str] = set()
            for info in infos:
                if info.filename in seen or info.filename.startswith(("/", "\\")) or ".." in pathlib.PurePosixPath(info.filename).parts:
                    raise EvidenceError("docx_member_path_invalid")
                seen.add(info.filename)
                expanded += info.file_size
                if info.file_size > MAX_DOCX_MEMBER_BYTES or expanded > MAX_DOCX_EXPANDED_BYTES:
                    raise EvidenceError("docx_expansion_limit")
                if info.flag_bits & 1:
                    raise EvidenceError("docx_encrypted_member")
            if "word/document.xml" not in seen:
                raise EvidenceError("docx_document_missing")
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise EvidenceError("docx_archive_invalid") from exc
    if len(xml) > MAX_XML_BYTES or re.search(rb"<!\s*(DOCTYPE|ENTITY)", xml, flags=re.I):
        raise EvidenceError("docx_xml_unsafe")
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise EvidenceError("docx_xml_invalid") from exc
    tables: list[list[list[str]]] = []
    for table in root.findall(".//w:tbl", ns):
        rows: list[list[str]] = []
        for row in table.findall("./w:tr", ns):
            cells: list[str] = []
            for cell in row.findall("./w:tc", ns):
                cells.append(" ".join("".join(item.text or "" for item in cell.findall(".//w:t", ns)).split()))
            rows.append(cells)
        tables.append(rows)
    return tables


def _make_source_binding(source_id: str, raw: bytes, media_type: str, path: str, method: str, retrieved_at: str, capture_role: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "origin": {"scheme": "https", "host": HOST, "path": path, "method": method, "query_values_stored": False, "body_values_stored": False},
        "media_type": media_type,
        "bytes": len(raw),
        "sha256": _sha256(raw),
        "retrieved_at": retrieved_at,
        "capture_role": capture_role,
        "parser": {"id": PARSER_ID, "version": PARSER_VERSION},
    }


def _source_ref(locator: dict[str, Any], evidence_kind: str = "document_cell") -> dict[str, Any]:
    return {"locator": locator, "evidence_kind": evidence_kind}


def parse_evidence(
    operation: dict[str, Any],
    *,
    page_raw: bytes,
    detail_raw: bytes,
    guide_raw: bytes,
    page_retrieved_at: str,
    detail_retrieved_at: str,
    guide_retrieved_at: str,
) -> dict[str, Any]:
    """Parse a selected official operation and emit no samples or response rows."""
    identity = _safe_operation_identity(operation)
    page = _parse_html(page_raw)
    detail = _parse_html(detail_raw)
    page_matches = [o for o in page.options if o.get("value") == identity["upstream_operation_key"] and o.get("name") == identity["operation_name"]]
    if len(page_matches) != 1:
        raise EvidenceError("operation_selector_identity_ambiguous")
    public_ids = page.hidden.get("publicDataPk", [])
    detail_ids = page.hidden.get("publicDataDetailPk", [])
    if public_ids != [identity["dataset_id"]] or len(detail_ids) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,255}", detail_ids[0]):
        raise EvidenceError("page_identity_binding_invalid")

    detail_binding = _make_source_binding("operation_detail_html", detail_raw, "text/html", DETAIL_ROUTE, "POST", detail_retrieved_at, "operation_detail")
    page_binding = _make_source_binding("catalogue_detail_page", page_raw, "text/html", f"/data/{identity['dataset_id']}/openapi.do", "GET", page_retrieved_at, "operation_selector_and_guide_index")
    guide_binding = _make_source_binding("reference_guide_docx", guide_raw, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", DOWNLOAD_ROUTE, "GET", guide_retrieved_at, "service_and_operation_guide")

    guide_tables = _docx_tables(guide_raw)
    operation_table_matches: list[int] = []
    for index, rows in enumerate(guide_tables):
        flattened = " ".join(cell for row in rows for cell in row)
        if "오퍼레이션 정보" in flattened and identity["operation_name"] in flattened:
            operation_table_matches.append(index)
    if len(operation_table_matches) != 1:
        raise EvidenceError("guide_operation_identity_ambiguous")
    operation_table_index = operation_table_matches[0]
    operation_table = guide_tables[operation_table_index]
    operation_name_refs: list[dict[str, Any]] = []
    effect_observations: list[tuple[str, dict[str, Any]]] = []
    method_observations: list[tuple[str, dict[str, Any], str]] = []
    operation_type = None
    for row_index, row in enumerate(operation_table):
        for cell_index, value in enumerate(row):
            if value == identity["operation_name"]:
                operation_name_refs.append(_source_ref(_cell_locator("reference_guide_docx", "docx_table_cell", operation_table_index, row_index, cell_index, part="word/document.xml"), "operation_name"))
            if value in {"조회(목록)", "조회(단건)", "조회", "목록 조회"}:
                operation_type = value
                effect_observations.append((value, _cell_locator("reference_guide_docx", "docx_table_cell", operation_table_index, row_index, cell_index, part="word/document.xml")))
            label = _normalize_header(row[0]) if row else ""
            if label in {"method", "httpmethod", "요청방식", "전송방식", "http전송방식"} and cell_index == 1 and value.upper() in METHODS:
                method_observations.append((value.upper(), _cell_locator("reference_guide_docx", "docx_table_cell", operation_table_index, row_index, cell_index, part="word/document.xml"), "operation_specific"))
    if not operation_name_refs:
        raise EvidenceError("guide_operation_name_missing")
    option_index = page.options.index(page_matches[0])
    identity["source_refs"] = [
        {"evidence_kind": "operation_selector_identity", "locator": {"source_id": "catalogue_detail_page", "kind": "html_select_option", "select_id": "open_api_detail_select", "option_index": option_index, "value": identity["upstream_operation_key"], "text": identity["operation_name"]}},
        *operation_name_refs,
    ]

    param_candidates = _find_param_tables(detail.tables)
    if not param_candidates:
        raise EvidenceError("detail_request_parameter_table_missing")
    # The operation-detail fragment is already bound to one selected upstream key.
    detail_table_index, detail_columns, detail_rows = param_candidates[0]
    operation_indexes = [
        index for index, rows in enumerate(guide_tables)
        if "오퍼레이션 정보" in " ".join(cell for row in rows for cell in row)
    ]
    next_operation_indexes = [index for index in operation_indexes if index > operation_table_index]
    section_end = min(next_operation_indexes) if next_operation_indexes else len(guide_tables)
    selected_section = {
        index: guide_tables[index]
        for index in range(operation_table_index + 1, section_end)
    }
    guide_param_candidates = [
        candidate for candidate in _find_param_tables(list(selected_section.values()))
    ]
    # _find_param_tables returns indexes relative to the selected section. Map
    # them back to the source document so every locator remains exact.
    selected_section_indexes = list(selected_section)
    guide_param_candidates = [
        (selected_section_indexes[relative_index], columns, rows)
        for relative_index, columns, rows in guide_param_candidates
    ]
    following = sorted(guide_param_candidates, key=lambda item: item[0])
    if not following:
        raise EvidenceError("guide_request_parameter_table_missing")
    if len(following) > 2:
        raise EvidenceError("guide_operation_parameter_tables_ambiguous")
    guide_table_index, guide_columns, guide_rows = following[0]

    def parse_rows(rows: list[list[str]], columns: dict[str, int], source_id: str, kind: str, table_idx: int, part: str | None = None) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for row_offset, row in enumerate(rows, 1):
            def get(column: str) -> str:
                i = columns.get(column, -1)
                return row[i].strip() if 0 <= i < len(row) else ""
            name = get("english_name")
            if not name:
                continue
            if len(name) > 256 or any(ord(c) < 0x20 for c in name):
                raise EvidenceError("parameter_name_invalid")
            if name in found:
                raise EvidenceError("parameter_name_duplicate_in_source")
            required, required_status = _requiredness(get("requiredness"))
            sample_cell = get("sample")
            size_text = get("size")
            entry: dict[str, Any] = {
                "name": name,
                "requiredness": {"value": required, "status": required_status, "source_refs": [_source_ref(_cell_locator(source_id, kind, table_idx, row_offset, columns["requiredness"], "항목구분", part))]},
                "size": {"value": size_text or None, "status": "documented" if size_text else "unknown", "source_refs": [_source_ref(_cell_locator(source_id, kind, table_idx, row_offset, columns.get("size", 0), "항목크기", part))]},
                "sample": {"present": bool(sample_cell and sample_cell not in {"-", "없음", "N/A"}), "value_stored": False, "source_refs": [_source_ref(_cell_locator(source_id, kind, table_idx, row_offset, columns["sample"], "샘플데이터", part))]},
                "type": {"value": None, "status": "unknown", "source_refs": []},
                "enum": {"values": [], "status": "not_established", "source_refs": []},
                "default": {"value": None, "status": "not_established", "source_refs": []},
                "description": {"value": None, "status": "not_parsed", "source_refs": []},
            }
            for key, col_name in (("name", "english_name"), ("location", "english_name")):
                entry.setdefault("source_refs", [])
            entry["source_refs"].append(_source_ref(_cell_locator(source_id, kind, table_idx, row_offset, columns["english_name"], "항목명(영문)", part), "parameter_name"))
            # Do not keep Korean descriptions, samples, or sample URLs.
            found[name] = entry
        return found

    detail_params = parse_rows(detail_rows, detail_columns, "operation_detail_html", "html_table_cell", detail_table_index)
    guide_params = parse_rows(guide_rows, guide_columns, "reference_guide_docx", "docx_table_cell", guide_table_index, "word/document.xml")
    if not detail_params or not guide_params:
        raise EvidenceError("request_parameter_inventory_empty")

    # A documented example URL proves query-key placement only when it is in
    # this operation's section and exactly matches the registered endpoint.
    registered_endpoint = urllib.parse.urlsplit(str(operation.get("transport", {}).get("endpoint") or ""))
    registered_host = (registered_endpoint.hostname or "").lower()
    registered_path = registered_endpoint.path or "/"
    query_key_observations: dict[str, list[dict[str, Any]]] = {}
    endpoint_observations: list[tuple[dict[str, str], dict[str, Any], list[str]]] = []
    guide_service_method_refs: list[dict[str, Any]] = []
    first_operation_index = min(operation_indexes) if operation_indexes else operation_table_index
    # The shared service interface declaration is only accepted from the
    # pre-operation service section; a method mentioned under another
    # operation cannot authorize the selected operation.
    for table_index, rows in enumerate(guide_tables[:first_operation_index]):
        for row_index, row in enumerate(rows):
            for cell_index, value in enumerate(row):
                if re.search(r"\bREST\s*\(\s*GET\s*,\s*POST\s*,\s*PUT\s*,\s*DELETE\s*\)", value, re.I):
                    guide_service_method_refs.append(_source_ref(_cell_locator("reference_guide_docx", "docx_table_cell", table_index, row_index, cell_index, part="word/document.xml"), "service_level_interface_methods"))
    for table_index, rows in selected_section.items():
        for row_index, row in enumerate(rows):
            for cell_index, value in enumerate(row):
                if "http://" in value or "https://" in value:
                    for part_value in re.findall(r"https?://[^\s<>\"']+", value):
                        parts = _safe_url_parts(part_value.rstrip(",;.)}"))
                        if parts:
                            locator = _cell_locator("reference_guide_docx", "docx_table_cell", table_index, row_index, cell_index, part="word/document.xml")
                            parsed_url = urllib.parse.urlsplit(part_value.rstrip(",;.)}"))
                            keys = [key for key, _ in urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True, strict_parsing=False) if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", key)]
                            endpoint_observations.append((parts, locator, keys))
                            if parts["host"] == registered_host and parts["path"] == registered_path:
                                for key_name in keys:
                                    query_key_observations.setdefault(key_name, []).append(locator)
                        try:
                            urllib.parse.urlsplit(part_value.rstrip(",;.)}"))
                        except ValueError:
                            pass

    # Parse response parameter names from the table immediately following the
    # selected guide request table. No response sample cells are copied.
    response_fields: list[str] = []
    response_refs: list[dict[str, Any]] = []
    later_tables = [(idx, cols, rows) for idx, cols, rows in guide_param_candidates if idx > guide_table_index]
    if later_tables:
        response_table_index, response_columns, response_rows = min(later_tables, key=lambda item: item[0])
        for row_offset, row in enumerate(response_rows, 1):
            col = response_columns["english_name"]
            name = row[col].strip() if col < len(row) else ""
            if name and name not in response_fields:
                response_fields.append(name)
                response_refs.append(_source_ref(_cell_locator("reference_guide_docx", "docx_table_cell", response_table_index, row_offset, col, "항목명(영문)", "word/document.xml"), "response_field_name"))

    # Resolve endpoint from a URI cell by exact match against the registered
    # endpoint host/path; do not retain or emit query strings.
    candidates = [(item, loc) for item, loc, _keys in endpoint_observations if item.get("host") == registered_host and item.get("path") == registered_path]
    if not candidates:
        # A service-level base URI is not an operation endpoint. Keep the
        # registered host/path only as a binding and mark the document path unknown.
        doc_endpoint: dict[str, Any] = {"scheme": None, "host": None, "path": None, "status": "not_established", "source_refs": []}
    else:
        unique_scheme = sorted({parts["scheme"] for parts, _ in candidates})
        unique_host = sorted({parts["host"] for parts, _ in candidates})
        unique_path = sorted({parts["path"] for parts, _ in candidates})
        first_loc = candidates[0][1]
        doc_endpoint = {
            "scheme": unique_scheme[0] if len(unique_scheme) == 1 else None,
            "scheme_status": "documented" if len(unique_scheme) == 1 else "conflict",
            "host": unique_host[0] if len(unique_host) == 1 else None,
            "host_status": "documented" if len(unique_host) == 1 else "conflict",
            "path": unique_path[0] if len(unique_path) == 1 else None,
            "path_status": "documented" if len(unique_path) == 1 else "conflict",
            "source_refs": [_source_ref(first_loc, "operation_endpoint")],
        }

    detail_values = parse_rows(detail_rows, detail_columns, "operation_detail_html", "html_table_cell", detail_table_index)
    guide_values = parse_rows(guide_rows, guide_columns, "reference_guide_docx", "docx_table_cell", guide_table_index, "word/document.xml")
    names = sorted(set(detail_values) | set(guide_values), key=lambda x: (x.casefold(), x))
    parameters: list[dict[str, Any]] = []
    for name in names:
        d = detail_values.get(name)
        g = guide_values.get(name)
        required_values = {x["requiredness"]["value"] for x in (d, g) if x and x["requiredness"]["value"] is not None}
        required_conflict = len(required_values) > 1
        requiredness = {
            "value": None if required_conflict or not required_values else next(iter(required_values)),
            "status": "conflict" if required_conflict else ("documented" if required_values else "unknown"),
            "source_refs": [ref for x in (d, g) if x for ref in x["requiredness"]["source_refs"]],
        }
        size_values = {x["size"]["value"] for x in (d, g) if x and x["size"]["value"] is not None}
        size = {
            "value": next(iter(size_values)) if len(size_values) == 1 else None,
            "observed_values": sorted(size_values),
            "status": "unknown" if not size_values else ("documented" if len(size_values) == 1 else "conflict"),
            "source_refs": [ref for x in (d, g) if x for ref in x["size"]["source_refs"]],
        }
        sample_presence = [x["sample"]["present"] for x in (d, g) if x]
        location_in_sample = name in query_key_observations
        location_refs: list[dict[str, Any]] = []
        if location_in_sample:
            location_refs = [_source_ref(loc, "example_request_query_key") for loc in query_key_observations[name]]
        parameters.append({
            "name": name,
            "location": {"value": "query" if location_in_sample else None, "status": "demonstrated_by_example" if location_in_sample else "unknown", "source_refs": location_refs},
            "cardinality": {"minimum": 1 if requiredness["value"] == "required" else (0 if requiredness["value"] == "optional" else None), "maximum": None, "status": "unknown", "source_refs": requiredness["source_refs"]},
            "requiredness": requiredness,
            "size": size,
            "data_type": {"value": None, "status": "unknown", "source_refs": []},
            "enum": {"values": [], "status": "not_established", "source_refs": []},
            "default": {"value": None, "status": "not_established", "source_refs": []},
            "sample": {"present": any(sample_presence), "value_stored": False, "source_refs": [ref for x in (d, g) if x for ref in x["sample"]["source_refs"]]},
            "source_refs": [ref for x in (d, g) if x for ref in x["source_refs"]],
        })

    method_refs: list[dict[str, Any]] = []
    if method_observations:
        values = sorted({value for value, _, scope in method_observations if scope == "operation_specific"})
        method_refs = [_source_ref(locator, "operation_http_method") for value, locator, scope in method_observations if scope == "operation_specific"]
        method = {"value": values[0] if len(values) == 1 else None, "status": "documented" if len(values) == 1 else "conflict", "authority_scope": "operation_specific", "source_refs": method_refs}
    else:
        method = {"value": None, "status": "unknown", "authority_scope": "service_level_only" if guide_service_method_refs else "not_found_in_parsed_operation_sources", "source_refs": guide_service_method_refs}

    effect_refs = [_source_ref(locator, "operation_effect") for _, locator in effect_observations]
    effect = {"classification": "read_only" if operation_type in {"조회(목록)", "조회(단건)", "조회", "목록 조회"} else None, "status": "documented" if effect_refs else "unknown", "authority": "operation_document", "source_refs": effect_refs}
    auth_names = [p["name"] for p in parameters if p["name"].casefold().replace("_", "") in {"servicekey", "apikey", "authorization", "authkey"}]
    service_key_in_example = "ServiceKey" in query_key_observations
    authentication = {
        "requirement": "required" if any(p["name"] == "ServiceKey" and p["requiredness"]["value"] == "required" for p in parameters) else None,
        "status": "documented" if any(p["name"] == "ServiceKey" and p["requiredness"]["value"] == "required" for p in parameters) else ("indicated_by_example" if service_key_in_example else "unknown"),
        "mechanism": "service_key" if auth_names or service_key_in_example else None,
        "parameter_names": sorted(set(auth_names) | ({"ServiceKey"} if service_key_in_example else set())),
        "placement": "query" if service_key_in_example else None,
        "source_refs": [ref for p in parameters if p["name"] == "ServiceKey" for ref in p["requiredness"]["source_refs"]] + ([_source_ref(loc, "example_request_query_key") for loc in query_key_observations["ServiceKey"]] if service_key_in_example else []),
    }
    explicit_unknowns = [
        "operation_level_http_method_not_declared" if method["value"] is None else "",
        "request_data_types_not_established" if any(p["data_type"]["value"] is None for p in parameters) else "",
        "enum_values_not_established" if any(p["enum"]["status"] == "not_established" for p in parameters) else "",
        "defaults_not_established" if any(p["default"]["status"] == "not_established" for p in parameters) else "",
        "provider_quota_not_parsed" ,
        "response_empty_result_semantics_not_declared",
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "parser": {"id": PARSER_ID, "version": PARSER_VERSION},
        "identity": identity,
        "source_bindings": [page_binding, detail_binding, guide_binding],
        "parse_status": "parsed_with_unknowns",
        "transport": {
            "protocol": {"value": identity["protocol"], "status": "registered_manifest", "source_refs": []},
            "scheme": {"value": doc_endpoint["scheme"], "status": doc_endpoint.get("scheme_status", doc_endpoint.get("status", "not_established")), "source_refs": doc_endpoint["source_refs"]},
            "host": {"value": doc_endpoint["host"], "status": doc_endpoint.get("host_status", doc_endpoint.get("status", "not_established")), "source_refs": doc_endpoint["source_refs"]},
            "path": {"value": doc_endpoint["path"], "status": doc_endpoint.get("path_status", doc_endpoint.get("status", "not_established")), "source_refs": doc_endpoint["source_refs"]},
            "http_method": method,
            "soap_action": {"value": None, "status": "not_applicable" if identity["protocol"] != "SOAP" else "unknown", "source_refs": []},
            "soap_version": {"value": None, "status": "not_applicable" if identity["protocol"] != "SOAP" else "unknown", "source_refs": []},
            "envelope_namespace": {"value": None, "status": "not_applicable" if identity["protocol"] != "SOAP" else "unknown", "source_refs": []},
            "operation_qname": {"value": None, "status": "not_applicable" if identity["protocol"] != "SOAP" else "unknown", "source_refs": []},
            "body_encoding": {"value": None, "status": "not_applicable" if identity["protocol"] != "SOAP" else "unknown", "source_refs": []},
        },
        "effect": effect,
        "parameters": parameters,
        "authentication": authentication,
        "limits": {"provider_quota": {"value": None, "unit": None, "status": "not_parsed", "source_refs": []}, "request_budget": {"value": None, "status": "not_a_provider_fact", "source_refs": []}},
        "response_assertion": {"kind": "documented_response_fields" if response_fields else "unknown", "fields": response_fields, "empty_result_semantics": {"value": None, "status": "unknown", "source_refs": []}, "source_refs": response_refs},
        "explicit_unknowns": [item for item in explicit_unknowns if item],
    }
    _validate_evidence(result)
    return result


def _url_has_query_key_from_cell(tables: list[list[list[str]]], locator: dict[str, Any], wanted: str) -> bool:
    table_index, row_index, cell_index = locator.get("table_index"), locator.get("row_index"), locator.get("cell_index")
    try:
        value = tables[table_index][row_index][cell_index]
    except (TypeError, IndexError):
        return False
    for candidate in re.findall(r"https?://[^\s<>\"']+", value):
        try:
            keys = {name for name, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(candidate.rstrip(",;.)}")).query, keep_blank_values=True)}
        except ValueError:
            continue
        if wanted in keys:
            return True
    return False


def _validate_evidence(value: dict[str, Any]) -> None:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("evidence_schema_version_invalid")
    if value.get("parse_status") not in {"parsed_with_unknowns", "parsed_complete", "ambiguous", "unsupported"}:
        raise EvidenceError("evidence_parse_status_invalid")
    for binding in value.get("source_bindings", []):
        if not re.fullmatch(r"[a-f0-9]{64}", binding.get("sha256", "")) or binding.get("bytes", 0) <= 0:
            raise EvidenceError("evidence_source_binding_invalid")
        if binding.get("origin", {}).get("host") not in {HOST, "www.safetydata.go.kr"} or "?" in binding.get("origin", {}).get("path", ""):
            raise EvidenceError("evidence_source_origin_unsafe")
    for parameter in value.get("parameters", []):
        if parameter.get("sample", {}).get("value_stored") is not False:
            raise EvidenceError("sample_value_must_not_be_stored")
        if parameter.get("sample", {}).get("present") not in {True, False}:
            raise EvidenceError("sample_presence_invalid")


def write_capture(path: pathlib.Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(raw)
    temp.chmod(0o600)
    temp.replace(path)


def _validate_official_url(url: str, *, purpose: str) -> urllib.parse.SplitResult:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise EvidenceError("source_url_invalid") from exc
    if parsed.scheme != "https" or parsed.hostname != HOST or parsed.username or parsed.password or port not in {None, 443} or parsed.fragment:
        raise EvidenceError("source_url_outside_official_allowlist")
    allowed_paths = {
        "catalogue": re.fullmatch(r"/data/[0-9]+/openapi\.do", parsed.path) is not None,
        "operation_detail": parsed.path == DETAIL_ROUTE,
        "reference_guide": parsed.path == DOWNLOAD_ROUTE,
    }
    if not allowed_paths.get(purpose, False):
        raise EvidenceError("source_path_outside_allowlist")
    if purpose == "catalogue" and parsed.query:
        raise EvidenceError("catalogue_query_not_allowed")
    if purpose == "operation_detail" and parsed.query:
        raise EvidenceError("detail_query_not_allowed")
    if purpose == "reference_guide":
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        if set(query) != {"atchFileId", "fileDetailSn"} or len(query["atchFileId"]) != 1 or len(query["fileDetailSn"]) != 1:
            raise EvidenceError("guide_query_invalid")
        if not re.fullmatch(r"FILE_[0-9]+", query["atchFileId"][0]) or not re.fullmatch(r"[0-9]+", query["fileDetailSn"][0]):
            raise EvidenceError("guide_query_values_invalid")
    return parsed


def _public_ip_for(host: str, *, resolver: Any = socket.getaddrinfo) -> str:
    try:
        entries = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise EvidenceError("source_dns_failed") from exc
    ips: list[str] = []
    for entry in entries:
        try:
            addr = ipaddress.ip_address(entry[4][0])
        except (ValueError, IndexError, TypeError):
            raise EvidenceError("source_dns_address_invalid")
        if not addr.is_global:
            raise EvidenceError("source_dns_private_or_reserved")
        ips.append(str(addr))
    if not ips:
        raise EvidenceError("source_dns_empty")
    return sorted(set(ips))[0]


def _fetch_official_worker(
    url: str,
    purpose: str,
    method: str,
    body: bytes | None,
    cookies: dict[str, str] | None,
    max_bytes: int,
    resolver: Any,
    output_dir: str,
) -> None:
    """Run the network operation in a killable process and save private output."""
    root = pathlib.Path(output_dir)
    try:
        raw, headers, response_cookies = _fetch_official_unbounded(
            url,
            purpose=purpose,
            method=method,
            body=body,
            cookies=cookies,
            max_bytes=max_bytes,
            resolver=resolver,
        )
        body_path = root / "body.bin"
        body_path.write_bytes(raw)
        body_path.chmod(0o600)
        result = {"status": "ok", "headers": headers, "cookies": response_cookies}
    except EvidenceError as exc:
        result = {"status": "error", "error": str(exc)}
    except BaseException:
        result = {"status": "error", "error": "source_capture_failed"}
    metadata_path = root / "result.json"
    metadata_path.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    metadata_path.chmod(0o600)


def _fetch_official(
    url: str,
    *,
    purpose: str,
    method: str = "GET",
    body: bytes | None = None,
    cookies: dict[str, str] | None = None,
    max_bytes: int,
    deadline_seconds: float = REQUEST_TIMEOUT_SECONDS,
    resolver: Any = socket.getaddrinfo,
) -> tuple[bytes, dict[str, str], dict[str, str]]:
    """Fetch with a hard wall-clock budget covering DNS through body EOF.

    Socket timeouts do not bound DNS and can be defeated by a peer that keeps
    a body read active while trickling bytes. Run the complete request in a
    killable process so its DNS, TLS, response-header, and body phases share
    one hard deadline.
    """
    if not isinstance(deadline_seconds, (int, float)) or deadline_seconds <= 0:
        raise EvidenceError("source_deadline_invalid")
    _validate_official_url(url, purpose=purpose)
    if method not in {"GET", "POST"}:
        raise EvidenceError("source_method_invalid")
    if purpose == "operation_detail" and method != "POST":
        raise EvidenceError("detail_method_invalid")
    try:
        context = multiprocessing.get_context("fork")
    except ValueError as exc:
        raise EvidenceError("source_deadline_process_unavailable") from exc
    with tempfile.TemporaryDirectory(prefix="datapan-operation-doc-fetch-") as temp_dir:
        pathlib.Path(temp_dir).chmod(0o700)
        process = context.Process(
            target=_fetch_official_worker,
            args=(url, purpose, method, body, cookies, max_bytes, resolver, temp_dir),
            daemon=True,
        )
        started = time.monotonic()
        try:
            process.start()
            remaining = deadline_seconds - (time.monotonic() - started)
            process.join(max(0.0, remaining))
            if process.is_alive():
                process.kill()
                process.join(1.0)
                raise EvidenceError("source_deadline_exceeded")
            if process.exitcode != 0:
                raise EvidenceError("source_capture_failed")
            root = pathlib.Path(temp_dir)
            metadata_path = root / "result.json"
            if not metadata_path.is_file() or metadata_path.stat().st_size > 65536:
                raise EvidenceError("source_capture_failed")
            result = json.loads(metadata_path.read_text(encoding="utf-8"))
            if result.get("status") == "error":
                raise EvidenceError(result.get("error", "source_capture_failed"))
            body_path = root / "body.bin"
            if not body_path.is_file() or body_path.stat().st_size > max_bytes:
                raise EvidenceError("source_capture_failed")
            return body_path.read_bytes(), result["headers"], result["cookies"]
        except EvidenceError:
            if process.is_alive():
                process.kill()
                process.join(1.0)
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            if process.is_alive():
                process.kill()
                process.join(1.0)
            raise EvidenceError("source_capture_failed") from exc


def _fetch_official_unbounded(
    url: str,
    *,
    purpose: str,
    method: str = "GET",
    body: bytes | None = None,
    cookies: dict[str, str] | None = None,
    max_bytes: int,
    deadline_seconds: float = REQUEST_TIMEOUT_SECONDS,
    resolver: Any = socket.getaddrinfo,
) -> tuple[bytes, dict[str, str], dict[str, str]]:
    """Perform one validated official response fetch inside the bounded child."""
    parsed = _validate_official_url(url, purpose=purpose)
    if method not in {"GET", "POST"}:
        raise EvidenceError("source_method_invalid")
    if purpose == "operation_detail" and method != "POST":
        raise EvidenceError("detail_method_invalid")
    started = time.monotonic()
    ip = _public_ip_for(HOST, resolver=resolver)
    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(HOST, 443, timeout=deadline_seconds, context=context)
    original_create = connection._create_connection

    def pinned_connect(address: tuple[str, int], timeout: float | None = None, source_address: Any = None) -> socket.socket:
        remaining = deadline_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("documentation request deadline")
        return socket.create_connection((ip, 443), timeout=min(timeout or remaining, remaining), source_address=source_address)

    connection._create_connection = pinned_connect  # type: ignore[method-assign]
    headers = {"Accept-Encoding": "identity", "User-Agent": "datapan-registry-operation-docs/1.0", "Accept": "text/html,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/octet-stream,*/*;q=0.2"}
    if cookies:
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in sorted(cookies.items()))
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    try:
        request_path = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection.request(method, request_path, body=body, headers=headers)
        remaining = deadline_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("documentation request deadline")
        if connection.sock:
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise EvidenceError("source_redirect_rejected")
        if response.status != 200:
            raise EvidenceError("source_http_status_rejected")
        content_encoding = response.getheader("Content-Encoding", "identity").lower()
        if content_encoding not in {"", "identity"}:
            raise EvidenceError("source_compression_rejected")
        content_length = response.getheader("Content-Length")
        if content_length and (not content_length.isdigit() or int(content_length) > max_bytes):
            raise EvidenceError("source_content_length_limit")
        raw = bytearray()
        while True:
            remaining = deadline_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise EvidenceError("source_deadline_exceeded")
            if connection.sock:
                connection.sock.settimeout(remaining)
            chunk = response.read(min(65536, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > max_bytes:
                raise EvidenceError("source_body_limit")
        response_headers = {name.lower(): response.getheader(name, "") for name in ("Content-Type", "Content-Length", "Date")}
        set_cookie_headers = response.headers.get_all("Set-Cookie") or []
        private_cookies: dict[str, str] = {}
        for item in set_cookie_headers:
            first = item.split(";", 1)[0]
            name, sep, value = first.partition("=")
            if sep and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name.strip()):
                private_cookies[name.strip()] = value.strip()
        connection.close()
        return bytes(raw), response_headers, private_cookies
    except EvidenceError:
        connection.close()
        raise
    except (OSError, http.client.HTTPException, TimeoutError, ssl.SSLError) as exc:
        connection.close()
        raise EvidenceError("source_transport_failed") from exc
    finally:
        # The connection object owns the socket; the closure does not retain it.
        _ = original_create


def _capture_one(operation: dict[str, Any], capture_root: pathlib.Path) -> dict[str, Any]:
    identity = _safe_operation_identity(operation)
    dataset_id, key = identity["dataset_id"], identity["upstream_operation_key"]
    if identity["source_system"] == "safetydata.go.kr":
        raise EvidenceError("safetydata_parser_not_yet_available")
    if not re.fullmatch(r"[0-9]+", key):
        raise EvidenceError("upstream_operation_key_unsupported")
    folder = capture_root / dataset_id / key
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    page_path = folder / "catalogue.html"
    detail_path = folder / "operation-detail.html"
    guide_path = folder / "reference-guide.docx"
    cookies: dict[str, str] = {}
    if page_path.exists():
        page_raw = page_path.read_bytes()
        page_at = dt.datetime.fromtimestamp(page_path.stat().st_mtime, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    else:
        page_raw, page_headers, new_cookies = _fetch_official(f"https://{HOST}/data/{dataset_id}/openapi.do", purpose="catalogue", max_bytes=MAX_PAGE_BYTES, cookies=cookies)
        cookies.update(new_cookies)
        page_at = page_headers.get("date") or _utc_now()
        write_capture(page_path, page_raw)
    page = _parse_html(page_raw)
    page_matches = [item for item in page.options if item.get("value") == key and item.get("name") == identity["operation_name"]]
    if len(page_matches) != 1 or page.hidden.get("publicDataPk") != [dataset_id] or len(page.hidden.get("publicDataDetailPk", [])) != 1:
        raise EvidenceError("operation_selector_identity_ambiguous")
    detail_pk = page.hidden["publicDataDetailPk"][0]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,255}", detail_pk):
        raise EvidenceError("page_detail_key_invalid")
    if detail_path.exists():
        detail_raw = detail_path.read_bytes()
        detail_at = dt.datetime.fromtimestamp(detail_path.stat().st_mtime, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    else:
        body = urllib.parse.urlencode({"oprtinSeqNo": key, "publicDataDetailPk": detail_pk, "publicDataPk": dataset_id}).encode("ascii")
        detail_raw, detail_headers, new_cookies = _fetch_official(f"https://{HOST}{DETAIL_ROUTE}", purpose="operation_detail", method="POST", body=body, max_bytes=MAX_DETAIL_BYTES, cookies=cookies)
        cookies.update(new_cookies)
        detail_at = detail_headers.get("date") or _utc_now()
        write_capture(detail_path, detail_raw)
    attachments = page.attachments
    if len(attachments) != 1:
        raise EvidenceError("guide_attachment_ambiguous")
    file_id, file_sn = attachments[0]
    if guide_path.exists():
        guide_raw = guide_path.read_bytes()
        guide_at = dt.datetime.fromtimestamp(guide_path.stat().st_mtime, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    else:
        query = urllib.parse.urlencode({"atchFileId": file_id, "fileDetailSn": file_sn})
        guide_raw, guide_headers, new_cookies = _fetch_official(f"https://{HOST}{DOWNLOAD_ROUTE}?{query}", purpose="reference_guide", max_bytes=MAX_GUIDE_BYTES, cookies=cookies)
        cookies.update(new_cookies)
        guide_at = guide_headers.get("date") or _utc_now()
        write_capture(guide_path, guide_raw)
    evidence = parse_evidence(operation, page_raw=page_raw, detail_raw=detail_raw, guide_raw=guide_raw, page_retrieved_at=page_at, detail_retrieved_at=detail_at, guide_retrieved_at=guide_at)
    evidence_raw = _json_bytes(evidence)
    write_capture(folder / "evidence.json", evidence_raw)
    return {"operation_id": identity["operation_id"], "dataset_id": dataset_id, "upstream_operation_key": key, "status": evidence["parse_status"], "evidence_sha256": _sha256(evidence_raw), "evidence_bytes": len(evidence_raw)}


def reconcile(queue: list[dict[str, Any]], evidence_dir: pathlib.Path) -> dict[str, Any]:
    counts = {"pending": 0, "parsed_with_unknowns": 0, "parsed_complete": 0, "ambiguous": 0, "unsupported": 0, "invalid": 0}
    by_operation = {item["operation_identity"]["operation_id"]: item for item in queue}
    processed: set[str] = set()
    for path in sorted(evidence_dir.glob("[0-9]*-[0-9]*.json")):
        try:
            value = _strict_read_json(path)
            _validate_evidence(value)
            operation_id = value["identity"]["operation_id"]
            if operation_id not in by_operation or operation_id in processed:
                counts["invalid"] += 1
                continue
            processed.add(operation_id)
            status = value["parse_status"]
            counts[status] = counts.get(status, 0) + 1
        except (EvidenceError, KeyError, TypeError):
            counts["invalid"] += 1
    counts["pending"] = len(queue) - len(processed) - counts["invalid"]
    return {
        "schema_version": "datapan.operation-document-reconciliation.v1",
        "manifest_binding": {"path": "reports/data-go-kr/operation-manifest.json", "sha256": _sha256(MANIFEST.read_bytes()) if MANIFEST.exists() else None, "source_snapshot_sha256": "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0"},
        "summary": {"registered_api_operations": len(queue), "statuses": counts, "coverage_complete": counts["pending"] == 0 and counts["invalid"] == 0},
        "scope": {"provider": "data.go.kr", "inventory": "registered_rest_and_soap_operations_only", "registered_protocols": {"REST": 12627, "SOAP": 35}, "link_operations_excluded": 8871, "operationless_catalog_entries_excluded": 473, "worldwide_provider_apis_included": False},
        "source_policy": {"official_document_hosts": [HOST, "www.safetydata.go.kr"], "provider_operation_calls": 0, "external_openapi_refs_followed": False, "soap_wsdl_imports_followed": False, "raw_capture_directory_private": True},
    }


def _parse_existing_captures(manifest: dict[str, Any], capture_root: pathlib.Path, output_dir: pathlib.Path, wanted_ids: set[str] | None = None) -> int:
    by_id = {item["operation_identity"]["operation_id"]: item for item in build_queue(manifest)}
    count = 0
    for operation in manifest["operations"]:
        identity = _safe_operation_identity(operation)
        if wanted_ids is not None and identity["operation_id"] not in wanted_ids:
            continue
        folder = capture_root / identity["dataset_id"] / identity["upstream_operation_key"]
        paths = [folder / "catalogue.html", folder / "operation-detail.html", folder / "reference-guide.docx"]
        if not all(path.is_file() for path in paths):
            continue
        page_raw, detail_raw, guide_raw = (path.read_bytes() for path in paths)
        get_at = lambda p: dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        value = parse_evidence(operation, page_raw=page_raw, detail_raw=detail_raw, guide_raw=guide_raw, page_retrieved_at=get_at(paths[0]), detail_retrieved_at=get_at(paths[1]), guide_retrieved_at=get_at(paths[2]))
        destination = output_dir / f"{identity['dataset_id']}-{identity['upstream_operation_key']}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_json_bytes(value))
        count += 1
    return count


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, default=MANIFEST)
    parser.add_argument("--build-queue", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--capture", action="store_true", help="Fetch official documentation only; never calls provider operation endpoints")
    parser.add_argument("--capture-root", type=pathlib.Path, help="Private raw capture directory; created mode 0700")
    parser.add_argument("--limit", type=int, default=1, help=f"Bounded operation count (maximum {MAX_BATCH_OPERATIONS})")
    parser.add_argument("--parse-captures", action="store_true")
    parser.add_argument("--evidence-dir", type=pathlib.Path, default=EVIDENCE_DIR)
    parser.add_argument("--queue", type=pathlib.Path, default=QUEUE)
    parser.add_argument("--reconciliation", type=pathlib.Path, default=RECONCILIATION)
    args = parser.parse_args()
    try:
        manifest = _strict_read_json(args.manifest)
        queue = build_queue(manifest)
        if args.build_queue:
            args.queue.parent.mkdir(parents=True, exist_ok=True)
            args.queue.write_bytes(b"".join(_json_line(item) for item in queue))
        if args.capture:
            if args.capture_root is None or args.limit < 1 or args.limit > MAX_BATCH_OPERATIONS:
                raise EvidenceError("capture_bounds_or_private_root_missing")
            args.capture_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                args.capture_root.chmod(0o700)
            except OSError:
                pass
            selected = queue[: args.limit]
            operations = {op["operation_id"]: op for op in manifest["operations"]}
            receipts = []
            for item in selected:
                try:
                    receipts.append(_capture_one(operations[item["operation_identity"]["operation_id"]], args.capture_root))
                except EvidenceError as exc:
                    receipts.append({"operation_id": item["operation_identity"]["operation_id"], "status": "unsupported", "reason_code": str(exc)})
            print(json.dumps({"attempted": len(receipts), "statuses": {s: sum(1 for r in receipts if r["status"] == s) for s in sorted({r["status"] for r in receipts})}, "private_capture_root": True}, sort_keys=True))
        if args.parse_captures:
            if args.capture_root is None:
                raise EvidenceError("capture_root_required")
            count = _parse_existing_captures(manifest, args.capture_root, args.evidence_dir)
            print(f"parsed evidence artifacts: {count}")
        if args.reconcile:
            value = reconcile(queue, args.evidence_dir)
            args.reconciliation.parent.mkdir(parents=True, exist_ok=True)
            args.reconciliation.write_bytes(_json_bytes(value))
            print(json.dumps(value["summary"], sort_keys=True))
        if not any((args.build_queue, args.capture, args.parse_captures, args.reconcile)):
            parser.print_help()
        return 0
    except EvidenceError as exc:
        print(f"FAIL operation-document evidence: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
