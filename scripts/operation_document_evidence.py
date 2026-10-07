#!/usr/bin/env python3
"""Acquire and parse bounded, official data.go.kr operation documentation.

This tool never calls a registered provider operation. It reads only the
official catalogue/detail/guide routes, stores raw captures in a private
directory, and emits redacted digest-bound evidence for offline review.
"""

from __future__ import annotations

import argparse
import copy
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
import subprocess
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "reports/data-go-kr/operation-manifest.json"
QUEUE = ROOT / "reports/operation-document-evidence/queue.v2.jsonl"
RECONCILIATION = ROOT / "reports/operation-document-evidence/reconciliation.v2.json"
EVIDENCE_DIR = ROOT / "reports/operation-document-evidence"
EVIDENCE_V2_DIR = EVIDENCE_DIR / "v2"
SCHEMA_VERSION = "datapan.operation-document-evidence.v2"
PARSER_ID = "registered-operation-document-parser"
PARSER_VERSION = "2.1.1"
HOST = "www.data.go.kr"
DETAIL_ROUTE = "/tcs/dss/selectApiDetailFunction.do"
DOWNLOAD_ROUTE = "/cmm/cmm/fileDownload.do"
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_DETAIL_BYTES = 1024 * 1024
MAX_GUIDE_BYTES = 8 * 1024 * 1024
MAX_OPENAPI_BYTES = 8 * 1024 * 1024
MAX_DOCX_ENTRIES = 256
MAX_DOCX_EXPANDED_BYTES = 16 * 1024 * 1024
MAX_DOCX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 35
MAX_BATCH_OPERATIONS = 50
MIN_DOCUMENT_REQUEST_INTERVAL_SECONDS = 1.0
MAX_PRIVATE_CAPTURE_ROOT_BYTES = 1024 * 1024 * 1024
MAX_SCHEMA_VARIANTS_PER_RESPONSE = 16
MAX_KOSIS_GUIDE_BYTES = 3 * 1024 * 1024
MAX_KOSIS_MANUAL_BYTES = 16 * 1024 * 1024
KOSIS_GUIDE_SHA256 = "ffa38d0e4afaf09d54b8e3542fe815b5a4cc1237a4376b4812ce49fd15b07c52"
KOSIS_MANUAL_SHA256 = "0d2de8e58bebdeb1546b9accc805407c6c8fc0ece0eaba9af2ef6fcc578fb821"
KOSIS_REGISTRY_REVISION = "dcb4ae423fcc1612ce3678cbb79eeab0b4e7517f"
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
        "source_id": "data_go_kr",
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
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", identity["upstream_operation_key"])
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
            "schema_version": "datapan.operation-document-work-item.v2",
            "operation_identity": identity,
            "status": "pending",
            "source_profile_id": source_profile_id,
            "next_action": "implement_safetydata_document_profile" if source_profile_id == "safetydata_v1" else "acquire_official_catalogue_detail_and_guide",
        })
    queue.sort(key=lambda item: item["operation_identity"]["operation_id"])
    return queue


def select_queue_batch(queue: list[dict[str, Any]], *, offset: int, limit: int) -> list[dict[str, Any]]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset >= max(1, len(queue)):
        raise EvidenceError("queue_offset_out_of_range")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_BATCH_OPERATIONS:
        raise EvidenceError("queue_batch_limit_invalid")
    return queue[offset : offset + limit]


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
                "data_type": {"자료형", "데이터형", "자료타입", "데이터유형", "datatype", "data_type", "type"},
                "enum": {"허용값", "가능값", "유효값", "열거값", "enum", "allowedvalues"},
                "default": {"기본값", "초기값", "default"},
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


def _safe_metadata_cell(value: str, *, field_name: str, parameter_name: str) -> str | None:
    """Keep explicit contract literals only when they cannot be examples/secrets."""
    text = " ".join(value.split()).strip()
    if field_name == "default" and text in {'""', "''"}:
        return ""
    if not text or text in {"-", "없음", "N/A", "해당없음"}:
        return None
    if field_name == "default" and parameter_name.casefold().replace("_", "") in {"servicekey", "apikey", "authorization", "authkey", "token", "accesskey"}:
        return None
    if (
        len(text) > 128
        or re.search(r"(?i)(bearer\s|token|secret|api[-_ ]?key|password|https?://|[?&][^\s=]+=)", text)
        or re.search(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text)
        or re.search(r"(?<!\d)\d{8,}(?!\d)", text)
        or re.search(r"\b[A-Za-z0-9_-]{32,}\b", text)
    ):
        return None
    if any(ord(char) < 0x20 for char in text):
        return None
    return text


def _safe_operation_document_text(value: Any) -> str | None:
    """Keep only short plain operation prose, never examples or wire values."""
    if not isinstance(value, str) or any(char in value for char in "\r\n\t"):
        return None
    text = " ".join(value.split()).strip()
    if not text or text != value or len(text) > 256:
        return None
    if _safe_metadata_cell(text, field_name="operation_document", parameter_name="") is None:
        return None
    if re.search(r"(?i)(?:example|sample|request|response|servicekey|apikey|authorization|credential|token|password|https?://|\burl\b)", text):
        return None
    if re.search(r"(예시|샘플|요청|응답|인증키|토큰|비밀번호|쿼리|URL)", text):
        return None
    if any(char in text for char in "{}[]<>`=?|\\"):
        return None
    return text


def _split_explicit_enum(value: str, *, parameter_name: str) -> list[str] | None:
    text = " ".join(value.split()).strip()
    if not text or text in {"-", "없음", "N/A", "해당없음"}:
        return None
    pieces = [piece.strip().strip("\"'") for piece in re.split(r"[,;|\n]", text)]
    if not 1 <= len(pieces) <= 64:
        return None
    values = [_safe_metadata_cell(piece, field_name="enum", parameter_name=parameter_name) for piece in pieces]
    if any(item is None for item in values):
        return None
    return [str(item) for item in values]


def _provider_quota_facts(
    tables: list[list[list[str]]],
    *,
    source_id: str,
    kind: str,
    part: str | None = None,
) -> dict[str, Any]:
    observations: list[tuple[int | None, str | None, dict[str, Any]]] = []
    label_pattern = re.compile(r"(?i)(호출\s*(?:횟수|제한|한도)|일일\s*트래픽|트래픽\s*(?:제한|한도)|rate\s*limit|quota)")
    count_pattern = re.compile(r"(?i)(?<![A-Za-z0-9])([0-9][0-9,]*)(?:\s*)(회|건|requests?|calls?)?")
    for table_index, rows in enumerate(tables):
        for row_index, row in enumerate(rows):
            row_text = " ".join(row)
            if not label_pattern.search(row_text):
                continue
            amounts = [int(match.group(1).replace(",", "")) for match in count_pattern.finditer(row_text)]
            if not amounts:
                locator = _cell_locator(source_id, kind, table_index, row_index, 0, part=part)
                observations.append((None, None, locator))
                continue
            period = None
            if re.search(r"(?i)(초당|매초|/\s*초|per\s+second|/sec)", row_text):
                period = "second"
            elif re.search(r"(?i)(일일|하루|/\s*일|per\s+day|daily)", row_text):
                period = "day"
            elif re.search(r"(?i)(분당|매분|/\s*분|per\s+minute)", row_text):
                period = "minute"
            elif re.search(r"(?i)(월간|매월|/\s*월|per\s+month|monthly)", row_text):
                period = "month"
            unit_name = "requests" if re.search(r"(?i)(회|건|requests?|calls?)", row_text) else None
            unit = f"{unit_name}/{period}" if unit_name and period else (unit_name or (f"/{period}" if period else None))
            if len(set(amounts)) != 1:
                observations.append((None, "conflict", _cell_locator(source_id, kind, table_index, row_index, 0, part=part)))
            else:
                amount_column = next((idx for idx, cell in enumerate(row) if re.search(r"[0-9]", cell)), 0)
                observations.append((amounts[0], unit, _cell_locator(source_id, kind, table_index, row_index, amount_column, part=part)))
    unknown_scope = {"value": None, "status": "unknown", "source_refs": []}
    if not observations:
        return {"value": None, "unit": None, "status": "not_parsed", "source_refs": [], "scope": dict(unknown_scope), "account_tier": dict(unknown_scope)}
    refs = [_source_ref(locator, "provider_quota") for _, _, locator in observations]
    if any(unit == "conflict" for _, unit, _ in observations):
        return {"value": None, "unit": None, "status": "conflict", "source_refs": refs, "scope": dict(unknown_scope), "account_tier": dict(unknown_scope)}
    if any(amount is None for amount, _, _ in observations):
        return {"value": None, "unit": None, "status": "unknown", "source_refs": refs, "scope": dict(unknown_scope), "account_tier": dict(unknown_scope)}
    distinct = {(amount, unit) for amount, unit, _ in observations}
    if len(distinct) != 1:
        return {"value": None, "unit": None, "status": "conflict", "source_refs": refs, "scope": dict(unknown_scope), "account_tier": dict(unknown_scope)}
    amount, unit = next(iter(distinct))
    return {"value": amount, "unit": unit, "status": "documented", "source_refs": refs, "scope": dict(unknown_scope), "account_tier": dict(unknown_scope)}


def _combine_quota_facts(facts: list[dict[str, Any]]) -> dict[str, Any]:
    unknown_scope = {"value": None, "status": "unknown", "source_refs": []}
    parsed = [fact for fact in facts if fact["status"] != "not_parsed"]
    refs = [ref for fact in parsed for ref in fact["source_refs"]]
    if not parsed:
        return {"value": None, "unit": None, "status": "not_parsed", "source_refs": [], "scope": dict(unknown_scope), "account_tier": dict(unknown_scope)}
    if any(fact["status"] == "conflict" for fact in parsed):
        return {"value": None, "unit": None, "status": "conflict", "source_refs": refs, "scope": dict(unknown_scope), "account_tier": dict(unknown_scope)}
    if any(fact["status"] == "unknown" for fact in parsed):
        return {"value": None, "unit": None, "status": "unknown", "source_refs": refs, "scope": dict(unknown_scope), "account_tier": dict(unknown_scope)}
    values = {(fact["value"], fact["unit"]) for fact in parsed}
    if len(values) != 1:
        return {"value": None, "unit": None, "status": "conflict", "source_refs": refs, "scope": dict(unknown_scope), "account_tier": dict(unknown_scope)}
    value, unit = next(iter(values))
    return {"value": value, "unit": unit, "status": "documented", "source_refs": refs, "scope": dict(unknown_scope), "account_tier": dict(unknown_scope)}


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
    if raw.startswith(b"%PDF-"):
        raise EvidenceError("guide_format_unsupported_pdf")
    if raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise EvidenceError("guide_format_unsupported_legacy_ole_document")
    if not raw.startswith(b"PK\x03\x04"):
        raise EvidenceError("guide_format_unsupported")
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


def _make_source_binding(source_id: str, raw: bytes, media_type: str, path: str, method: str, retrieved_at: str, capture_role: str, host: str = HOST) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "origin": {"scheme": "https", "host": host, "path": path, "method": method, "query_values_stored": False, "body_values_stored": False},
        "media_type": media_type,
        "bytes": len(raw),
        "sha256": _sha256(raw),
        "retrieved_at": retrieved_at,
        "capture_role": capture_role,
        "parser": {"id": PARSER_ID, "version": PARSER_VERSION},
    }


def _source_ref(locator: dict[str, Any], evidence_kind: str = "document_cell") -> dict[str, Any]:
    return {"locator": locator, "evidence_kind": evidence_kind}


def _unknown_response_contract() -> dict[str, Any]:
    unknown_codes = {"status": "unknown", "values": [], "source_refs": []}
    return {
        "accepted_http_status_codes": {"status": "unknown", "values": [], "source_refs": []},
        "payload": {"status": "unknown", "kind": "unknown", "media_types": [], "source_refs": []},
        "schema_shape": {"status": "unknown", "source_refs": []},
        "coded_result_field_inventory": {"status": "unknown", "candidates": [], "source_refs": []},
        "success_branches": [],
        "documented_http_error_branches": [],
        "documented_fields": [],
        "required_fields": [],
        "provider_result_codes": {"status": "unknown", "evidence_strength": "unknown", "path": None, "value_type": None, "success_values": dict(unknown_codes), "error_values": dict(unknown_codes), "source_refs": []},
        "result_collection": {"status": "unknown", "path": None, "container_path": None, "item_path": None, "container_cardinality": {"status": "unknown", "minimum": None, "maximum": None, "source_refs": []}, "value_type": None, "source_refs": []},
        "declared_output_fields": [],
        "documented_error_contract": {"status": "unknown", "format": "unknown", "code_path": None, "message_path": None, "codes": [], "source_refs": []},
    }


def _json_pointer_child(pointer: str, token: str | int) -> str:
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    return f"{pointer}/{escaped}"


def _json_pointer_locator(source_id: str, pointer: str, byte_range: tuple[int, int]) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "kind": "html_inline_json",
        "variable_name": "swaggerJson",
        "byte_start": byte_range[0],
        "byte_end": byte_range[1],
        "json_pointer": pointer,
    }


def _json_load_no_duplicates(raw: str) -> Any:
    def pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError("openapi_json_duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=pairs_no_duplicates)
    except (json.JSONDecodeError, TypeError) as exc:
        raise EvidenceError("openapi_json_invalid") from exc


def _successful_response_label(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 512:
        return False
    normalized = value.casefold()
    if any(marker in normalized for marker in ("실패", "오류", "error", "failure", "unsuccessful", "invalid", "not successful", "not success")):
        return False
    compact = re.sub(r"\s+", "", normalized)
    return any(marker in compact for marker in ("성공", "정상응답", "정상처리")) or compact == "정상" or re.search(r"\bsuccess(?:ful)?(?:response)?\b", compact) is not None


def _json_pointer_tokens(pointer: str) -> list[str] | None:
    if not isinstance(pointer, str) or not pointer.startswith("#/"):
        return None
    try:
        return [token.replace("~1", "/").replace("~0", "~") for token in pointer[2:].split("/")]
    except (TypeError, ValueError):
        return None


def _example_scalar_type_matches(value: Any, type_name: str | None) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    return False


def _example_code_is_safe(value: Any) -> bool:
    if isinstance(value, str):
        return _safe_metadata_cell(value, field_name="provider_result_code", parameter_name="") is not None
    if isinstance(value, int) and not isinstance(value, bool):
        return 0 <= value <= 9999999
    if isinstance(value, float):
        return value == value and abs(value) <= 9999999
    return isinstance(value, bool)


def _inline_swagger(page_raw: bytes) -> tuple[dict[str, Any], tuple[int, int]]:
    try:
        text = page_raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise EvidenceError("catalogue_utf8_invalid") from exc
    matches = list(re.finditer(r"\bconst\s+swaggerJson\s*=\s*\x60([^\x60]*)\x60\s*;", text, re.S))
    if len(matches) != 1 or not matches[0].group(1):
        raise EvidenceError("inline_swagger_document_missing_or_ambiguous")
    match = matches[0]
    spec_text = html.unescape(match.group(1))
    if len(spec_text.encode("utf-8")) > MAX_OPENAPI_BYTES:
        raise EvidenceError("openapi_document_size_invalid")
    document = _json_load_no_duplicates(spec_text)
    if not isinstance(document, dict) or document.get("swagger") != "2.0":
        raise EvidenceError("openapi_version_unsupported")
    prefix = len(text[: match.start(1)].encode("utf-8"))
    end = len(text[: match.end(1)].encode("utf-8"))
    return document, (prefix, end)


def _openapi_resolve(document: dict[str, Any], schema: Any, pointer: str) -> tuple[Any, str, list[str]]:
    seen: set[str] = set()
    refs: list[str] = []
    while isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        if not ref.startswith("#/") or ref in seen:
            raise EvidenceError("openapi_ref_external_or_cyclic")
        seen.add(ref)
        refs.append(ref)
        current: Any = document
        for token in ref[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, dict) or token not in current:
                raise EvidenceError("openapi_ref_target_missing")
            current = current[token]
        schema = current
        pointer = ref
    return schema, pointer, refs


def _openapi_source_binding(page_raw: bytes, retrieved_at: str, media_type: str | None, dataset_id: str) -> dict[str, Any]:
    return _make_source_binding(
        "catalogue_detail_page",
        page_raw,
        _normalize_content_type(media_type, "text/html; charset=utf-8") or "text/html; charset=utf-8",
        f"/data/{dataset_id}/openapi.do",
        "GET",
        retrieved_at,
        "catalogue_identity_and_inline_openapi_operation",
    )


def parse_openapi_evidence(
    operation: dict[str, Any],
    *,
    page_raw: bytes,
    page_retrieved_at: str,
    page_media_type: str | None = None,
) -> dict[str, Any]:
    """Parse one exact inline Swagger 2 operation without following references."""
    identity = _safe_operation_identity(operation)
    page = _parse_html(page_raw)
    if page.hidden.get("publicDataPk") != [identity["dataset_id"]]:
        raise EvidenceError("catalogue_dataset_identity_mismatch")
    document, byte_range = _inline_swagger(page_raw)
    try:
        registered = urllib.parse.urlsplit(str(operation.get("transport", {}).get("endpoint") or ""))
        registered_port = registered.port
    except ValueError as exc:
        raise EvidenceError("registered_endpoint_invalid") from exc
    if registered.scheme != "https" or not registered.hostname or registered.query or registered.fragment or registered.username or registered.password:
        raise EvidenceError("registered_endpoint_invalid")
    host_value = document.get("host")
    if not isinstance(host_value, str) or not host_value or "@" in host_value or "?" in host_value or "#" in host_value:
        raise EvidenceError("openapi_host_invalid")
    host_parts = urllib.parse.urlsplit("//" + host_value)
    if not host_parts.hostname or host_parts.username or host_parts.password:
        raise EvidenceError("openapi_host_invalid")
    try:
        spec_port = host_parts.port
    except ValueError as exc:
        raise EvidenceError("openapi_host_invalid") from exc
    if host_parts.hostname.lower() != registered.hostname.lower() or spec_port != registered_port:
        raise EvidenceError("openapi_registered_host_mismatch")
    host_prefix = host_parts.path.rstrip("/")
    base_path = document.get("basePath", "")
    if not isinstance(base_path, str) or (base_path and (not base_path.startswith("/") or "?" in base_path or "#" in base_path)):
        raise EvidenceError("openapi_base_path_invalid")
    schemes = document.get("schemes")
    if not isinstance(schemes, list) or not schemes or any(value not in {"http", "https"} for value in schemes):
        raise EvidenceError("openapi_schemes_invalid")
    if registered.scheme not in schemes:
        raise EvidenceError("openapi_registered_scheme_mismatch")
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise EvidenceError("openapi_paths_invalid")
    method_names = {"get", "head", "post", "put", "patch", "delete", "options"}
    matched: list[tuple[str, str, dict[str, Any]]] = []
    for spec_path, path_item in paths.items():
        if not isinstance(spec_path, str) or not spec_path.startswith("/") or "?" in spec_path or "#" in spec_path or not isinstance(path_item, dict):
            raise EvidenceError("openapi_path_invalid")
        full_path = host_prefix + base_path.rstrip("/") + spec_path
        if not full_path:
            full_path = "/"
        for method_name, operation_spec in path_item.items():
            if method_name.lower() not in method_names or not isinstance(operation_spec, dict):
                continue
            summary = operation_spec.get("summary")
            operation_id = operation_spec.get("operationId")
            if (
                registered.path == full_path
                and isinstance(summary, str)
                and summary == identity["operation_name"]
                and isinstance(operation_id, str)
                and operation_id
            ):
                matched.append((spec_path, method_name.lower(), operation_spec))
    if len(matched) != 1:
        raise EvidenceError("openapi_operation_identity_ambiguous" if matched else "openapi_operation_identity_mismatch")
    spec_path, method_name, operation_spec = matched[0]
    operation_pointer = _json_pointer_child(_json_pointer_child("#/paths", spec_path), method_name)
    byte_locator = lambda pointer: _json_pointer_locator("catalogue_detail_page", pointer, byte_range)
    ref = lambda pointer, kind: _source_ref(byte_locator(pointer), kind)
    summary_pointer = _json_pointer_child(operation_pointer, "summary")
    description_pointer = _json_pointer_child(operation_pointer, "description")
    path_pointer = _json_pointer_child("#/paths", spec_path)
    identity["source_refs"] = [
        _source_ref({"source_id": "catalogue_detail_page", "kind": "html_hidden_input", "input_id": "publicDataPk", "value": identity["dataset_id"]}, "catalogue_dataset_identity"),
        ref(summary_pointer, "operation_name"),
        ref(path_pointer, "operation_endpoint_path"),
        ref(_json_pointer_child(operation_pointer, "operationId"), "operation_id"),
    ]
    source_binding = _openapi_source_binding(page_raw, page_retrieved_at, page_media_type, identity["dataset_id"])

    # Swagger 2 path-level parameters are inherited unless replaced by the
    # operation-level parameter with the same (name, in) pair.
    path_item = paths[spec_path]
    parameter_sets: list[tuple[list[Any], str, bool]] = []
    if isinstance(path_item.get("parameters"), list):
        parameter_sets.append((path_item["parameters"], _json_pointer_child(path_pointer, "parameters"), False))
    raw_operation_parameters = operation_spec.get("parameters", [])
    if not isinstance(raw_operation_parameters, list):
        raise EvidenceError("openapi_parameters_invalid")
    parameter_sets.append((raw_operation_parameters, _json_pointer_child(operation_pointer, "parameters"), True))
    parameter_map: dict[tuple[str, str], tuple[dict[str, Any], str, list[str]]] = {}
    for parameter_list, parameter_base, operation_level in parameter_sets:
        for index, raw_parameter in enumerate(parameter_list):
            pointer = _json_pointer_child(parameter_base, index)
            resolved, resolved_pointer, deref_refs = _openapi_resolve(document, raw_parameter, pointer)
            if not isinstance(resolved, dict):
                raise EvidenceError("openapi_parameter_invalid")
            name = resolved.get("name")
            location = resolved.get("in")
            if not isinstance(name, str) or not name or len(name) > 256 or any(ord(ch) < 0x20 for ch in name):
                raise EvidenceError("openapi_parameter_name_invalid")
            if location not in {"query", "path", "header", "body", "formData"}:
                raise EvidenceError("openapi_parameter_location_unsupported")
            key = (name.casefold(), location)
            if key in parameter_map and operation_level:
                parameter_map[key] = (resolved, resolved_pointer, deref_refs + ([pointer] if deref_refs else []))
            elif key in parameter_map:
                raise EvidenceError("openapi_parameter_duplicate")
            else:
                parameter_map[key] = (resolved, resolved_pointer, deref_refs + ([pointer] if deref_refs else []))
    parameters: list[dict[str, Any]] = []
    for (name_folded, location), (parameter, pointer, ref_pointers) in sorted(parameter_map.items()):
        name = parameter["name"]
        required_raw = parameter.get("required")
        required_value = "required" if required_raw is True or location == "path" else "optional" if required_raw is False else None
        required_status = "documented" if required_value else "unknown"
        location_value = "body" if location == "formData" else location
        type_value = parameter.get("type")
        schema_pointer = pointer
        schema = parameter.get("schema")
        if type_value is None and isinstance(schema, dict):
            schema_pointer = _json_pointer_child(pointer, "schema")
            type_value = schema.get("type")
        if not isinstance(type_value, str) or type_value not in {"string", "integer", "number", "boolean", "array", "file"}:
            type_value = None
        type_pointer = _json_pointer_child(schema_pointer, "type")
        required_refs = [ref(_json_pointer_child(pointer, "required"), "parameter_requiredness")] if required_value and required_raw is not None else []
        if location == "path" and required_raw is not True:
            required_refs = [ref(_json_pointer_child(pointer, "in"), "path_parameter_required_by_openapi")]
        location_refs = [ref(_json_pointer_child(pointer, "in"), "parameter_location")]
        name_refs = [ref(_json_pointer_child(pointer, "name"), "parameter_name")]
        type_refs = [ref(type_pointer, "parameter_data_type")] if type_value else []
        schema_for_values = schema if isinstance(schema, dict) else parameter
        enum_value = schema_for_values.get("enum")
        default_present = "default" in schema_for_values
        default_value = schema_for_values.get("default")
        enum_pointer = _json_pointer_child(schema_pointer, "enum")
        default_pointer = _json_pointer_child(schema_pointer, "default")
        enum_refs = [ref(enum_pointer, "parameter_enum")] if isinstance(enum_value, list) else []
        default_refs = [ref(default_pointer, "parameter_default")] if default_present else []
        enum_values: list[str] = []
        enum_status = "not_established"
        if isinstance(enum_value, list):
            safe_values = [
                _safe_metadata_cell(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":")), field_name="enum", parameter_name=name)
                if isinstance(value, (str, int, float, bool)) or value is None else None
                for value in enum_value
            ]
            if 1 <= len(safe_values) <= 64 and all(value is not None for value in safe_values):
                enum_values = [str(value) for value in safe_values]
                enum_status = "documented"
            else:
                enum_status = "unknown"
        default_text: str | None = None
        default_status = "not_established"
        if default_present:
            default_text = _safe_metadata_cell(default_value if isinstance(default_value, str) else json.dumps(default_value, ensure_ascii=False, separators=(",", ":")), field_name="default", parameter_name=name)
            default_status = "documented" if default_text is not None else "unknown"
        max_length = schema_for_values.get("maxLength")
        size_value = str(max_length) if isinstance(max_length, int) and not isinstance(max_length, bool) and max_length >= 0 else None
        size_refs = [ref(_json_pointer_child(schema_pointer, "maxLength"), "parameter_maximum_length")] if size_value is not None else []
        array_type = type_value == "array"
        min_items = schema_for_values.get("minItems")
        max_items = schema_for_values.get("maxItems")
        if array_type:
            if min_items is None and required_value == "required":
                min_items = 1
            elif min_items is None:
                min_items = 0
        elif type_value:
            min_items, max_items = (1 if required_value == "required" else 0), 1
        else:
            min_items = 1 if required_value == "required" else 0 if required_value == "optional" else None
        cardinality_refs = required_refs + (
            [ref(_json_pointer_child(schema_pointer, "minItems"), "parameter_minimum_cardinality")] if isinstance(schema_for_values.get("minItems"), int) else []
        ) + (
            [ref(_json_pointer_child(schema_pointer, "maxItems"), "parameter_maximum_cardinality")] if isinstance(schema_for_values.get("maxItems"), int) else []
        )
        if not array_type and type_value:
            cardinality_status = "documented"
        elif array_type and isinstance(min_items, int) and not isinstance(min_items, bool) and (max_items is None or isinstance(max_items, int) and not isinstance(max_items, bool)):
            cardinality_status = "documented"
        else:
            cardinality_status = "unknown"
        example_present = any(key in parameter or isinstance(schema, dict) and key in schema for key in ("example", "examples"))
        ref_source_refs = [ref(ref_pointer, "parameter_reference") for ref_pointer in ref_pointers]
        parameters.append({
            "name": name,
            "location": {"value": location_value, "status": "documented", "source_refs": location_refs},
            "cardinality": {
                "minimum": min_items if isinstance(min_items, int) and not isinstance(min_items, bool) and min_items >= 0 else None,
                "maximum": max_items if isinstance(max_items, int) and not isinstance(max_items, bool) and max_items >= 0 else (None if array_type else 1 if type_value else None),
                "status": cardinality_status if required_status == "documented" else "unknown",
                "source_refs": cardinality_refs,
            },
            "requiredness": {"value": required_value, "status": required_status, "source_refs": required_refs},
            "size": {"value": size_value, "observed_values": [size_value] if size_value is not None else [], "status": "documented" if size_value is not None else "unknown", "source_refs": size_refs},
            "data_type": {"value": type_value, "status": "documented" if type_value else "not_established", "source_refs": type_refs},
            "enum": {"values": enum_values, "status": enum_status, "source_refs": enum_refs},
            "default": {"value": default_text, "status": default_status, "source_refs": default_refs},
            "sample": {"present": example_present, "value_stored": False, "source_refs": [ref(_json_pointer_child(pointer, "example"), "parameter_example_presence")] if example_present else []},
            "source_refs": [*name_refs, *location_refs, *required_refs, *type_refs, *enum_refs, *default_refs, *size_refs, *cardinality_refs, *ref_source_refs],
        })

    authentication: dict[str, Any] = {"requirement": None, "status": "unknown", "mechanism": None, "parameter_names": [], "placement": None, "source_refs": []}
    security_definitions = document.get("securityDefinitions")
    if "security" in operation_spec:
        security_value, security_pointer = operation_spec.get("security"), _json_pointer_child(operation_pointer, "security")
    elif "security" in document:
        security_value, security_pointer = document.get("security"), "#/security"
    else:
        security_value, security_pointer = None, None
    if security_value == [] and security_pointer:
        authentication.update(requirement="none", status="documented", source_refs=[ref(security_pointer, "operation_authentication_absent")])
    elif isinstance(security_value, list) and isinstance(security_definitions, dict):
        observed_schemes: list[tuple[str, str, str, str]] = []
        for requirement in security_value:
            if not isinstance(requirement, dict):
                continue
            for scheme_name in requirement:
                definition = security_definitions.get(scheme_name)
                if not isinstance(definition, dict) or definition.get("type") != "apiKey":
                    continue
                placement = definition.get("in")
                parameter_name = definition.get("name")
                if placement in {"query", "header"} and isinstance(parameter_name, str):
                    mechanism = "service_key" if parameter_name.casefold().replace("_", "") == "servicekey" else "api_key"
                    observed_schemes.append((mechanism, placement, parameter_name, _json_pointer_child(_json_pointer_child("#/securityDefinitions", scheme_name), "name")))
        if len(observed_schemes) == 1:
            mechanism, placement, parameter_name, auth_pointer = observed_schemes[0]
            authentication.update(requirement="required", status="documented", mechanism=mechanism, parameter_names=[parameter_name], placement=placement, source_refs=[ref(security_pointer, "operation_security_requirement"), ref(auth_pointer, "authentication_security_scheme"), ref(_json_pointer_child(_json_pointer_child("#/securityDefinitions", scheme_name), "in"), "authentication_placement")])

    # Some official Swagger pages describe an issued credential directly on a
    # parameter instead of declaring securityDefinitions. A parameter name by
    # itself is never enough to classify authentication.
    explicit_auth: list[tuple[str, str, str | None, list[dict[str, Any]]]] = []
    credential_description = re.compile(r"(?i)(인증\s*키|공공데이터포털.{0,40}(?:받|발급)|\bapi\s*key\b|\baccess\s*token\b)")
    for (name_folded, location), (parameter, pointer, ref_pointers) in parameter_map.items():
        description = parameter.get("description")
        if not isinstance(description, str) or not credential_description.search(description):
            continue
        if location not in {"query", "header"}:
            continue
        name = parameter.get("name")
        if not isinstance(name, str):
            continue
        normalized_name = name.casefold().replace("_", "")
        mechanism = "service_key" if "공공데이터포털" in description or normalized_name == "servicekey" else "api_key"
        required_value = parameter.get("required") is True
        auth_refs = [
            ref(_json_pointer_child(pointer, "description"), "authentication_credential_description"),
            ref(_json_pointer_child(pointer, "name"), "authentication_parameter_name"),
            ref(_json_pointer_child(pointer, "in"), "authentication_parameter_placement"),
        ]
        if isinstance(parameter.get("required"), bool):
            auth_refs.append(ref(_json_pointer_child(pointer, "required"), "authentication_parameter_requiredness"))
        auth_refs.extend(ref(ref_pointer, "authentication_parameter_reference") for ref_pointer in ref_pointers)
        explicit_auth.append((name, location, mechanism if required_value else None, auth_refs))
    if explicit_auth:
        names = sorted({name for name, _, _, _ in explicit_auth})
        placements = {placement for _, placement, _, _ in explicit_auth}
        mechanisms = {mechanism for _, _, mechanism, _ in explicit_auth if mechanism is not None}
        required = any(mechanism is not None for _, _, mechanism, _ in explicit_auth)
        refs = [source_ref for _, _, _, auth_refs in explicit_auth for source_ref in auth_refs]
        if authentication["status"] == "documented" and authentication["requirement"] == "none":
            authentication.update(requirement=None, status="conflict", mechanism=None, parameter_names=names, placement=None, source_refs=[*authentication["source_refs"], *refs])
        elif len(placements) == 1 and len(mechanisms) <= 1:
            authentication.update(
                requirement="required" if required else None,
                status="documented",
                mechanism=next(iter(mechanisms)) if mechanisms else None,
                parameter_names=names,
                placement=next(iter(placements)),
                source_refs=[*authentication["source_refs"], *refs],
            )
        else:
            authentication.update(requirement=None, status="conflict", mechanism=None, parameter_names=names, placement=None, source_refs=[*authentication["source_refs"], *refs])

    response_fields: list[str] = []
    response_refs: list[dict[str, Any]] = []
    responses = operation_spec.get("responses")
    if not isinstance(responses, dict):
        raise EvidenceError("openapi_responses_invalid")
    numeric_responses = [
        (code, response)
        for code, response in responses.items()
        if re.fullmatch(r"[1-5][0-9][0-9]", str(code)) and isinstance(response, dict)
    ]
    success_responses = [(code, response) for code, response in numeric_responses if 200 <= int(code) <= 299]
    if isinstance(operation_spec.get("produces"), list):
        produces = operation_spec["produces"]
        produces_pointer = _json_pointer_child(operation_pointer, "produces")
    else:
        produces = document.get("produces", [])
        produces_pointer = "#/produces"
    if not isinstance(produces, list):
        produces = []
    payload_refs = [
        ref(_json_pointer_child(produces_pointer, index), "response_media_type")
        for index, value in enumerate(produces)
        if isinstance(value, str)
    ]
    payload_media_types = sorted({str(value).split(";", 1)[0].strip().casefold() for value in produces if isinstance(value, str) and value.strip()})
    normalized_produces = {str(value).split(";", 1)[0].strip().casefold() for value in produces if isinstance(value, str)}
    response_format = "json" if normalized_produces and any(value == "application/json" or value.endswith("+json") for value in normalized_produces) and not any("xml" in value for value in normalized_produces) else "xml" if normalized_produces and any("xml" in value for value in normalized_produces) and not any(value == "application/json" or value.endswith("+json") for value in normalized_produces) else None
    accepted_status_refs = [
        ref(_json_pointer_child(_json_pointer_child(operation_pointer, "responses"), code), "accepted_http_success_status")
        for code, _ in success_responses
    ]
    accepted_status_codes = sorted({int(code) for code, _ in success_responses})
    response_contract = {
        "accepted_http_status_codes": {"status": "documented" if accepted_status_codes else "unknown", "values": accepted_status_codes, "source_refs": accepted_status_refs},
        "payload": {"status": "documented" if response_format else "conflict" if normalized_produces else "unknown", "kind": response_format or "unknown", "media_types": payload_media_types, "source_refs": payload_refs},
        "schema_shape": {"status": "unknown", "source_refs": []},
        "coded_result_field_inventory": {"status": "unknown", "candidates": [], "source_refs": []},
        "success_branches": [],
        "documented_http_error_branches": [],
        "documented_fields": [],
        "required_fields": [],
        "provider_result_codes": {"status": "unknown", "evidence_strength": "unknown", "path": None, "value_type": None, "success_values": {"status": "unknown", "values": [], "source_refs": []}, "error_values": {"status": "unknown", "values": [], "source_refs": []}, "source_refs": []},
        "result_collection": {"status": "unknown", "path": None, "container_path": None, "item_path": None, "container_cardinality": {"status": "unknown", "minimum": None, "maximum": None, "source_refs": []}, "value_type": None, "source_refs": []},
        "declared_output_fields": [],
        "documented_error_contract": {"status": "unknown", "format": "unknown", "code_path": None, "message_path": None, "codes": [], "source_refs": []},
    }
    array_facts: list[tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any], list[dict[str, Any]]]] = []
    branch_array_facts: dict[tuple[int, str, int | None], list[tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any], list[dict[str, Any]]]]] = {}
    result_code_facts: list[tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]] = []
    branch_result_code_facts: dict[tuple[int, str, int | None], list[tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]]] = {}
    code_candidate_facts: list[dict[str, Any]] = []
    schema_shape_results: list[tuple[bool, list[dict[str, Any]]]] = []
    active_response_status_code = 0
    active_schema_variant: dict[str, Any] = {"kind": "single", "index": None}
    active_branch_key: tuple[int, str, int | None] = (0, "single", None)

    def response_path(tokens: list[str], xml_segments: list[dict[str, Any] | None], path_known: bool) -> dict[str, Any] | None:
        if response_format == "json" and path_known:
            pointer = "#" + "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in tokens)
            return {"kind": "json_pointer", "value": pointer}
        if response_format == "xml" and xml_segments and all(segment is not None and segment["namespace"] is not None for segment in xml_segments):
            return {"kind": "xml_qname_path", "segments": xml_segments}
        return None

    def collect_schema(schema_value: Any, schema_pointer: str, data_tokens: list[str], xml_segments: list[dict[str, Any] | None], inherited_namespace: str | None, path_known: bool, visited: set[tuple[str, tuple[str, ...]]]) -> None:
        resolved, resolved_pointer, ref_pointers = _openapi_resolve(document, schema_value, schema_pointer)
        if not isinstance(resolved, dict):
            return
        visit_key = (resolved_pointer, tuple(data_tokens))
        if visit_key in visited:
            return
        visited.add(visit_key)
        for ref_pointer in ref_pointers:
            response_refs.append(ref(ref_pointer, "response_schema_reference"))
        properties = resolved.get("properties")
        if isinstance(properties, dict):
            declared_required = resolved.get("required")
            required_set = set(declared_required) if isinstance(declared_required, list) and all(isinstance(value, str) for value in declared_required) else set() if "required" not in resolved else None
            if isinstance(declared_required, list) and (not all(isinstance(value, str) for value in declared_required) or len(set(declared_required)) != len(declared_required)):
                raise EvidenceError("openapi_response_required_invalid")
            for field_name, child_schema in properties.items():
                if not isinstance(field_name, str) or not field_name or not isinstance(child_schema, (dict, bool)):
                    continue
                property_pointer = _json_pointer_child(_json_pointer_child(resolved_pointer, "properties"), field_name)
                child_resolved, child_resolved_pointer, child_ref_pointers = _openapi_resolve(document, child_schema, property_pointer)
                if not isinstance(child_resolved, dict):
                    continue
                field_type = child_resolved.get("type")
                if not isinstance(field_type, str) or field_type not in {"object", "array", "string", "integer", "number", "boolean"}:
                    field_type = "object" if isinstance(child_resolved.get("properties"), dict) else None
                xml_value = child_resolved.get("xml") if isinstance(child_resolved.get("xml"), dict) else {}
                namespace = xml_value.get("namespace", inherited_namespace)
                local_name = xml_value.get("name", field_name)
                child_xml = [*xml_segments, {"namespace": namespace if isinstance(namespace, str) else None, "local_name": local_name}]
                child_tokens = [*data_tokens, field_name]
                path_value = response_path(child_tokens, child_xml, path_known)
                required = required_set is not None and field_name in required_set
                cardinality_refs: list[dict[str, Any]] = []
                if required_set is not None:
                    if "required" in resolved:
                        cardinality_refs.append(ref(_json_pointer_child(resolved_pointer, "required"), "response_field_requiredness"))
                    else:
                        # OpenAPI/JSON Schema object properties are optional
                        # unless named in the parent's required array.
                        cardinality_refs.append(ref(resolved_pointer, "response_field_optional_by_schema_default"))
                    minimum, cardinality_status = (1 if required else 0), "documented"
                else:
                    minimum, cardinality_status = None, "unknown"
                field_refs = [ref(property_pointer, "response_field_schema"), *[ref(ref_pointer, "response_field_schema_reference") for ref_pointer in child_ref_pointers]]
                type_pointer = _json_pointer_child(child_resolved_pointer, "type")
                if field_type and "type" in child_resolved:
                    field_refs.append(ref(type_pointer, "response_field_type"))
                if path_value is None:
                    field_status = "unknown"
                else:
                    field_status = "documented" if field_type else "unknown"
                cardinality = {"status": cardinality_status, "minimum": minimum, "maximum": 1 if cardinality_status == "documented" else None, "source_refs": cardinality_refs}
                field_fact = {"http_status_code": active_response_status_code, "schema_variant": copy.deepcopy(active_schema_variant), "name": field_name, "status": field_status, "path": path_value, "value_type": field_type, "cardinality": cardinality, "source_refs": field_refs}
                response_contract["documented_fields"].append(field_fact)
                response_fields.append(field_name)
                response_refs.extend(field_refs)
                if required:
                    response_contract["required_fields"].append({**field_fact, "source_refs": [*field_refs, *cardinality_refs]})
                normalized_field_name = re.sub(r"[^a-z0-9]", "", field_name.casefold())
                if normalized_field_name in {"resultcode", "responsecode", "returncode"}:
                    code_fact = (path_value, field_type, [*field_refs, *cardinality_refs])
                    result_code_facts.append(code_fact)
                    branch_result_code_facts.setdefault(active_branch_key, []).append(code_fact)
                    code_candidate_facts.append({"http_status_code": active_response_status_code, "schema_variant": copy.deepcopy(active_schema_variant), "name": field_name, "classification": "recognized_result_code_name", "path": path_value, "value_type": field_type, "source_refs": [*field_refs, *cardinality_refs]})
                elif normalized_field_name in {"code", "status", "success", "error", "result", "response"}:
                    code_candidate_facts.append({"http_status_code": active_response_status_code, "schema_variant": copy.deepcopy(active_schema_variant), "name": field_name, "classification": "ambiguous_code_semantics", "path": path_value, "value_type": field_type, "source_refs": [*field_refs, *cardinality_refs]})
                if field_type == "array":
                    container_path = None
                    item_path = None
                    if response_format == "json" and path_value is not None:
                        pointer = path_value["value"]
                        container_pointer = pointer.rsplit("/", 1)[0] or "#"
                        container_path = {"kind": "json_pointer", "value": container_pointer}
                    elif response_format == "xml" and path_value is not None:
                        xml_wrapper = child_resolved.get("xml") if isinstance(child_resolved.get("xml"), dict) else {}
                        items_schema = child_resolved.get("items")
                        item_xml = items_schema.get("xml") if isinstance(items_schema, dict) and isinstance(items_schema.get("xml"), dict) else {}
                        item_name = item_xml.get("name")
                        item_namespace = item_xml.get("namespace", namespace)
                        if xml_wrapper.get("wrapped") is True and isinstance(item_name, str) and item_name and isinstance(item_namespace, str):
                            container_path = path_value
                            # The container selector is absolute from the
                            # response root; the repeated QName is relative to
                            # that selected container so consumers can tell an
                            # absent container from an empty present one.
                            item_path = {"kind": "xml_qname_path", "segments": [{"namespace": item_namespace, "local_name": item_name}]}
                    array_fact = (path_value, container_path, item_path, cardinality, [*field_refs, *cardinality_refs])
                    array_facts.append(array_fact)
                    branch_array_facts.setdefault(active_branch_key, []).append(array_fact)
                    items = child_resolved.get("items")
                    if isinstance(items, dict):
                        # JSON Pointer has no wildcard token, so nested array
                        # item properties are not assigned invented paths.
                        if response_format == "xml":
                            item_xml_value = items.get("xml") if isinstance(items.get("xml"), dict) else {}
                            item_namespace = item_xml_value.get("namespace", namespace)
                            item_name = item_xml_value.get("name", field_name)
                            item_segments = [*child_xml, {"namespace": item_namespace if isinstance(item_namespace, str) else None, "local_name": item_name}]
                            collect_schema(items, _json_pointer_child(child_resolved_pointer, "items"), child_tokens, item_segments, item_namespace if isinstance(item_namespace, str) else None, path_known, visited)
                        else:
                            collect_schema(items, _json_pointer_child(child_resolved_pointer, "items"), child_tokens, child_xml, namespace if isinstance(namespace, str) else inherited_namespace, False, visited)
                    continue
                if field_type == "object" or isinstance(child_resolved.get("properties"), dict):
                    collect_schema(child_resolved, child_resolved_pointer, child_tokens, child_xml, namespace if isinstance(namespace, str) else inherited_namespace, path_known, visited)
        items = resolved.get("items")
        if isinstance(items, dict) and resolved.get("type") == "array":
            collect_schema(items, _json_pointer_child(resolved_pointer, "items"), data_tokens, xml_segments, inherited_namespace, path_known, visited)

    def inspect_response_shape(schema_value: Any, schema_pointer: str, visited: set[str]) -> tuple[bool, list[dict[str, Any]]]:
        resolved, resolved_pointer, ref_pointers = _openapi_resolve(document, schema_value, schema_pointer)
        refs = [ref(schema_pointer, "response_schema_shape_root"), *[ref(item, "response_schema_shape_reference") for item in ref_pointers]]
        if not isinstance(resolved, dict) or resolved_pointer in visited:
            return False, refs
        visited.add(resolved_pointer)
        schema_type = resolved.get("type")
        if schema_type in {"string", "integer", "number", "boolean"}:
            if "type" in resolved:
                refs.append(ref(_json_pointer_child(resolved_pointer, "type"), "response_schema_scalar_type"))
            return True, refs
        if schema_type == "array":
            items = resolved.get("items")
            if not isinstance(items, (dict, bool)):
                return False, refs
            refs.append(ref(_json_pointer_child(resolved_pointer, "type"), "response_schema_array_type"))
            complete, item_refs = inspect_response_shape(items, _json_pointer_child(resolved_pointer, "items"), visited.copy())
            return complete, [*refs, *item_refs]
        properties = resolved.get("properties")
        if schema_type == "object" or isinstance(properties, dict):
            if not isinstance(properties, dict) or resolved.get("additionalProperties") is not False:
                return False, refs
            refs.append(ref(_json_pointer_child(resolved_pointer, "additionalProperties"), "response_schema_closed_object"))
            if "type" in resolved:
                refs.append(ref(_json_pointer_child(resolved_pointer, "type"), "response_schema_object_type"))
            complete = True
            for property_name, child_schema in properties.items():
                child_pointer = _json_pointer_child(_json_pointer_child(resolved_pointer, "properties"), property_name)
                child_complete, child_refs = inspect_response_shape(child_schema, child_pointer, visited.copy())
                complete = complete and child_complete
                refs.extend(child_refs)
            return complete, refs
        return False, refs

    max_schema_variants = MAX_SCHEMA_VARIANTS_PER_RESPONSE
    for status_code, response in numeric_responses:
        active_response_status_code = int(status_code)
        is_http_success = 200 <= active_response_status_code <= 299
        status_evidence_kind = "accepted_http_success_status" if is_http_success else "documented_http_error_status"
        schema_value = response.get("schema")
        response_pointer = _json_pointer_child(_json_pointer_child(operation_pointer, "responses"), status_code)
        schema_pointer = _json_pointer_child(response_pointer, "schema")
        variants: list[tuple[str, int | None, Any, str]]
        union_keys = [key for key in ("oneOf", "anyOf") if isinstance(schema_value, dict) and key in schema_value]
        if not union_keys:
            variants = [("single", None, schema_value, schema_pointer)]
        elif len(union_keys) == 1 and isinstance(schema_value.get(union_keys[0]), list) and 0 < len(schema_value[union_keys[0]]) <= max_schema_variants and all(isinstance(item, (dict, bool)) for item in schema_value[union_keys[0]]):
            union_kind = union_keys[0]
            variants = [(union_kind, index, member, _json_pointer_child(_json_pointer_child(schema_pointer, union_kind), index)) for index, member in enumerate(schema_value[union_kind])]
        else:
            # Do not flatten ambiguous, malformed, or oversized unions.
            # Preserve the exact root pointer and mark its shape incomplete.
            variants = [("unsupported_union", None, None, schema_pointer)]
        for variant_kind, variant_index, variant_schema, variant_pointer in variants:
            active_schema_variant = {"kind": variant_kind, "index": variant_index}
            active_branch_key = (active_response_status_code, variant_kind, variant_index)
            if schema_value is None or variant_kind == "unsupported_union":
                shape_complete = False
                missing_schema_kind = "success_response_schema_not_declared" if is_http_success else "http_error_response_schema_not_declared"
                shape_refs = [ref(response_pointer if schema_value is None else schema_pointer, missing_schema_kind if schema_value is None else "response_schema_union_unresolved")]
            else:
                shape_complete, shape_refs = inspect_response_shape(variant_schema, variant_pointer, set())
            schema_shape_results.append((shape_complete, shape_refs))
            status_ref = ref(response_pointer, status_evidence_kind)
            schema_source = {
                "status": "documented" if schema_value is not None else "not_established",
                "json_pointer": variant_pointer if schema_value is not None else None,
                "source_refs": [ref(variant_pointer, "response_schema_variant_root")] if schema_value is not None else [ref(response_pointer, "success_response_schema_not_declared" if is_http_success else "http_error_response_schema_not_declared")],
            }
            root_shape: dict[str, Any] = {"status": "unknown", "kind": "unknown", "qname": None, "source_refs": list(schema_source["source_refs"])}
            if schema_value is not None and variant_kind != "unsupported_union":
                root_schema, root_pointer, root_ref_pointers = _openapi_resolve(document, variant_schema, variant_pointer)
                root_type = root_schema.get("type") if isinstance(root_schema, dict) else None
                root_xml = root_schema.get("xml") if isinstance(root_schema, dict) and isinstance(root_schema.get("xml"), dict) else {}
                root_name = root_xml.get("name")
                root_namespace = root_xml.get("namespace")
                root_kind = None
                root_qname = None
                root_status = "unknown"
                if response_format == "xml":
                    if isinstance(root_name, str) and root_name:
                        root_kind = "xml_element"
                        root_qname = {"namespace": root_namespace if isinstance(root_namespace, str) else None, "local_name": root_name}
                        if isinstance(root_namespace, str):
                            root_status = "documented"
                            root_shape["source_refs"].append(ref(_json_pointer_child(root_pointer, "xml"), "response_root_xml_qname"))
                        else:
                            root_shape["source_refs"].append(ref(_json_pointer_child(root_pointer, "xml"), "response_root_xml_namespace_not_established"))
                elif root_type == "array":
                    root_kind = "array"
                    root_status = "documented"
                    root_shape["source_refs"].append(ref(_json_pointer_child(root_pointer, "type"), "response_root_array_type"))
                elif root_type == "object" or isinstance(root_schema, dict) and isinstance(root_schema.get("properties"), dict):
                    root_kind = "object"
                    root_status = "documented"
                    root_shape["source_refs"].append(ref(_json_pointer_child(root_pointer, "type"), "response_root_object_type") if isinstance(root_type, str) else ref(_json_pointer_child(root_pointer, "properties"), "response_root_object_properties"))
                elif root_type in {"string", "integer", "number", "boolean"}:
                    root_kind = "scalar"
                    root_status = "documented"
                    root_shape["source_refs"].append(ref(_json_pointer_child(root_pointer, "type"), "response_root_scalar_type"))
                root_shape.update(status=root_status, kind=root_kind or "unknown", qname=root_qname)
                root_shape["source_refs"].extend(ref(pointer, "response_root_schema_reference") for pointer in root_ref_pointers)
            branch_collection = response_contract["success_branches"] if is_http_success else response_contract["documented_http_error_branches"]
            branch_collection.append({
                "http_status_code": active_response_status_code,
                "schema_variant": copy.deepcopy(active_schema_variant),
                "payload": {"status": response_contract["payload"]["status"], "kind": response_format or "unknown", "media_types": payload_media_types, "source_refs": payload_refs},
                "schema_shape": {"status": "complete" if shape_complete else "incomplete", "source_refs": shape_refs},
                "schema_source": schema_source,
                "root_shape": root_shape,
                "documented_fields": [],
                "required_fields": [],
                "coded_result_field_inventory": {"status": "complete" if shape_complete else "incomplete", "candidates": [], "source_refs": shape_refs},
                "provider_result_codes": {"status": "unknown", "evidence_strength": "unknown", "path": None, "value_type": None, "success_values": {"status": "unknown", "values": [], "source_refs": []}, "error_values": {"status": "unknown", "values": [], "source_refs": []}, "source_refs": []},
                "result_collection": {"status": "unknown", "path": None, "container_path": None, "item_path": None, "container_cardinality": {"status": "unknown", "minimum": None, "maximum": None, "source_refs": []}, "value_type": None, "source_refs": []},
                "source_refs": [status_ref, ref(response_pointer, "success_response_branch" if is_http_success else "http_error_response_branch"), *schema_source["source_refs"]],
            })
            if schema_value is not None and variant_kind != "unsupported_union":
                root_schema, root_pointer, _ = _openapi_resolve(document, variant_schema, variant_pointer)
                root_xml = root_schema.get("xml") if isinstance(root_schema, dict) and isinstance(root_schema.get("xml"), dict) else {}
                root_name = root_xml.get("name")
                root_namespace = root_xml.get("namespace")
                root_xml_segments = [{"namespace": root_namespace if isinstance(root_namespace, str) else None, "local_name": root_name}] if isinstance(root_name, str) and root_name else [None]
                collect_schema(variant_schema, variant_pointer, [], root_xml_segments, root_namespace if isinstance(root_namespace, str) else None, True, set())
    if schema_shape_results:
        shape_status = "complete" if response_contract["success_branches"] and all(complete for complete, _ in schema_shape_results) else "incomplete"
        shape_refs = [source_ref for _, refs in schema_shape_results for source_ref in refs]
        response_contract["schema_shape"].update(status=shape_status, source_refs=shape_refs)
        response_contract["coded_result_field_inventory"].update(
            status="complete" if shape_status == "complete" else "incomplete",
            candidates=code_candidate_facts,
            source_refs=[*shape_refs, *[source_ref for candidate in code_candidate_facts for source_ref in candidate["source_refs"]]],
        )
    # Preserve order while de-duplicating field labels for the compatibility
    # assertion; the structured facts above retain their distinct paths.
    response_fields = list(dict.fromkeys(response_fields))
    if len(result_code_facts) == 1:
        result_path, result_type, result_refs = result_code_facts[0]
        response_contract["provider_result_codes"].update(path=result_path, value_type=result_type, source_refs=result_refs)
        for target, label in (("success_values", "provider_result_code_success_values_not_declared"), ("error_values", "provider_result_code_error_values_not_declared")):
            response_contract["provider_result_codes"][target]["source_refs"] = [
                ref(result_refs[0]["locator"]["json_pointer"], label)
            ] if result_refs and result_refs[0]["locator"].get("kind") == "html_inline_json" else result_refs
        # A labeled official successful response example can establish only
        # the shown success literal. It never establishes an exhaustive error
        # map, nor does it store the example body or any response rows.
        if response_format == "json" and result_path and result_path.get("kind") == "json_pointer":
            field_tokens = _json_pointer_tokens(result_path["value"])
            example_values: dict[tuple[type, Any], list[dict[str, Any]]] = {}
            if field_tokens:
                for status_code, response in success_responses:
                    description = response.get("description")
                    examples = response.get("examples")
                    if not _successful_response_label(description) or not isinstance(examples, dict):
                        continue
                    for media_type, raw_example in examples.items():
                        if not isinstance(media_type, str) or not (media_type.casefold() == "application/json" or media_type.casefold().endswith("+json")):
                            continue
                        # JSON-encoded strings are nested documents: a child
                        # pointer would not resolve in the original Swagger
                        # source. Do not promote them without a nested binding.
                        example = raw_example
                        if not isinstance(example, dict):
                            continue
                        selected: Any = example
                        for token in field_tokens:
                            if not isinstance(selected, dict) or token not in selected:
                                selected = None
                                break
                            selected = selected[token]
                        if selected is None or not _example_scalar_type_matches(selected, result_type) or not _example_code_is_safe(selected):
                            continue
                        responses_pointer = _json_pointer_child(operation_pointer, "responses")
                        response_pointer = _json_pointer_child(responses_pointer, status_code)
                        description_pointer = _json_pointer_child(response_pointer, "description")
                        example_pointer = _json_pointer_child(_json_pointer_child(response_pointer, "examples"), media_type)
                        for token in field_tokens:
                            example_pointer = _json_pointer_child(example_pointer, token)
                        example_refs = [
                            ref(description_pointer, "official_success_response_example_label"),
                            ref(example_pointer, "provider_result_code_success_official_example_value"),
                        ]
                        example_values.setdefault((type(selected), selected), []).extend(example_refs)
            if example_values:
                value_refs = [source_ref for refs in example_values.values() for source_ref in refs]
                success_values = [value for _, value in example_values]
                response_contract["provider_result_codes"].update(
                    status="example_only",
                    evidence_strength="official_success_example",
                    source_refs=[*result_refs, *value_refs],
                )
                response_contract["provider_result_codes"]["success_values"].update(
                    status="example_only", values=success_values, source_refs=value_refs,
                )
    elif len(result_code_facts) > 1:
        response_contract["provider_result_codes"]["status"] = "unknown"
        response_contract["provider_result_codes"]["evidence_strength"] = "unknown"
        response_contract["provider_result_codes"]["source_refs"] = [source_ref for _, _, refs in result_code_facts for source_ref in refs]
    if len(array_facts) == 1:
        array_path, container_path, item_path, container_cardinality, array_refs = array_facts[0]
        collection_status = "documented" if array_path is not None and (response_format == "json" or container_path is not None and item_path is not None and container_cardinality["status"] == "documented") else "unknown"
        if response_format == "json":
            container_cardinality = {"status": "not_applicable", "minimum": None, "maximum": None, "source_refs": []}
        response_contract["result_collection"].update(status=collection_status, path=array_path if response_format == "json" else None, container_path=container_path, item_path=item_path, container_cardinality=container_cardinality, value_type="array", source_refs=array_refs)
    elif array_facts:
        response_contract["result_collection"]["source_refs"] = [source_ref for _, _, _, _, refs in array_facts for source_ref in refs]

    for branch in [*response_contract["success_branches"], *response_contract["documented_http_error_branches"]]:
        branch_status = branch["http_status_code"]
        branch_variant = branch["schema_variant"]
        branch_key = (branch_status, branch_variant["kind"], branch_variant["index"])
        branch["documented_fields"] = [field for field in response_contract["documented_fields"] if field["http_status_code"] == branch_status and field["schema_variant"] == branch_variant]
        branch["required_fields"] = [field for field in response_contract["required_fields"] if field["http_status_code"] == branch_status and field["schema_variant"] == branch_variant]
        branch_candidates = [candidate for candidate in code_candidate_facts if candidate["http_status_code"] == branch_status and candidate["schema_variant"] == branch_variant]
        branch["coded_result_field_inventory"].update(
            candidates=branch_candidates,
            source_refs=[*branch["schema_shape"]["source_refs"], *[source_ref for candidate in branch_candidates for source_ref in candidate["source_refs"]]],
        )
        branch_codes = branch_result_code_facts.get(branch_key, [])
        if len(branch_codes) == 1:
            branch_path, branch_type, branch_refs = branch_codes[0]
            branch["provider_result_codes"].update(path=branch_path, value_type=branch_type, source_refs=branch_refs)
            if len(result_code_facts) == 1 and response_contract["provider_result_codes"]["path"] == branch_path:
                branch["provider_result_codes"] = copy.deepcopy(response_contract["provider_result_codes"])
        elif branch_codes:
            branch["provider_result_codes"]["source_refs"] = [source_ref for _, _, refs in branch_codes for source_ref in refs]
        branch_arrays = branch_array_facts.get(branch_key, [])
        if len(branch_arrays) == 1:
            branch_path, container_path, item_path, container_cardinality, branch_refs = branch_arrays[0]
            collection_status = "documented" if branch_path is not None and (response_format == "json" or container_path is not None and item_path is not None and container_cardinality["status"] == "documented") else "unknown"
            if response_format == "json":
                container_cardinality = {"status": "not_applicable", "minimum": None, "maximum": None, "source_refs": []}
            branch["result_collection"].update(status=collection_status, path=branch_path if response_format == "json" else None, container_path=container_path, item_path=item_path, container_cardinality=container_cardinality, value_type="array", source_refs=branch_refs)
        elif branch_arrays:
            branch["result_collection"]["source_refs"] = [source_ref for _, _, _, _, refs in branch_arrays for source_ref in refs]

    scheme_refs = [
        ref(_json_pointer_child("#/schemes", index), "transport_scheme")
        for index, scheme in enumerate(schemes)
        if scheme == registered.scheme
    ]
    explicit_unknowns = [
        "operation_effect_not_established_by_openapi",
        "operation_purpose_not_established" if not isinstance(operation_spec.get("description"), str) or _safe_operation_document_text(operation_spec.get("description")) is None else "",
        "authentication_security_requirement_not_established" if authentication["status"] == "unknown" else "",
        "parameter_requiredness_not_established" if any(parameter["requiredness"]["status"] != "documented" for parameter in parameters) else "",
        "request_data_types_not_established" if any(parameter["data_type"]["status"] != "documented" for parameter in parameters) else "",
        "enum_values_not_established" if any(parameter["enum"]["status"] != "documented" for parameter in parameters) else "",
        "defaults_not_established" if any(parameter["default"]["status"] != "documented" for parameter in parameters) else "",
        "provider_quota_not_declared_or_parseable",
        "provider_quota_scope_not_established",
        "provider_quota_account_tier_not_established",
        "response_empty_result_semantics_not_declared",
        "response_error_contract_not_established",
    ]
    title_value = _safe_operation_document_text(operation_spec.get("summary"))
    title_fact = {
        "value": title_value,
        "status": "documented" if title_value is not None else "unknown",
        "source_refs": [ref(summary_pointer, "official_operation_title")] if isinstance(operation_spec.get("summary"), str) else [],
    }
    description_value = operation_spec.get("description")
    purpose_value = _safe_operation_document_text(description_value)
    purpose_fact = {
        "value": purpose_value,
        "status": "documented" if purpose_value is not None else "unknown" if isinstance(description_value, str) else "not_found_in_parsed_operation_sources",
        "source_refs": [ref(description_pointer, "official_operation_purpose" if purpose_value is not None else "operation_purpose_suppressed_untrusted_content")] if isinstance(description_value, str) else [],
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "parser": {"id": PARSER_ID, "version": PARSER_VERSION},
        "identity": identity,
        "operation_document": {"title": title_fact, "purpose": purpose_fact},
        "source_bindings": [source_binding],
        "parse_status": "parsed_with_unknowns",
        "transport": {
            "protocol": {"value": identity["protocol"], "status": "registered_manifest", "source_refs": []},
            "scheme": {"value": registered.scheme, "status": "documented", "source_refs": scheme_refs},
            "host": {"value": registered.hostname.lower(), "status": "documented", "source_refs": [ref("#/host", "transport_host")]},
            "path": {"value": registered.path, "status": "documented", "source_refs": [ref(path_pointer, "transport_path")]},
            "http_method": {"value": method_name.upper(), "status": "documented", "authority_scope": "operation_specific", "source_refs": [ref(operation_pointer, "operation_http_method")]},
            "soap_action": {"value": None, "status": "not_applicable", "source_refs": []},
            "soap_version": {"value": None, "status": "not_applicable", "source_refs": []},
            "envelope_namespace": {"value": None, "status": "not_applicable", "source_refs": []},
            "operation_qname": {"value": None, "status": "not_applicable", "source_refs": []},
            "body_encoding": {"value": None, "status": "not_applicable", "source_refs": []},
            "fixed_query_selectors": [],
        },
        "effect": {"classification": None, "status": "unknown", "authority": "operation_document", "source_refs": []},
        "parameters": parameters,
        "authentication": authentication,
        "limits": {"provider_quota": {"value": None, "unit": None, "status": "not_parsed", "source_refs": [], "scope": {"value": None, "status": "unknown", "source_refs": []}, "account_tier": {"value": None, "status": "unknown", "source_refs": []}}, "request_budget": {"value": None, "status": "not_a_provider_fact", "source_refs": []}},
        "response_assertion": {"kind": "documented_response_fields" if response_fields else "unknown", "fields": response_fields, "empty_result_semantics": {"value": None, "status": "unknown", "source_refs": []}, "source_refs": response_refs},
        "response_contract": response_contract,
        "explicit_unknowns": [value for value in explicit_unknowns if value],
    }
    _validate_evidence(result)
    return result


def parse_evidence(
    operation: dict[str, Any],
    *,
    page_raw: bytes,
    detail_raw: bytes,
    guide_raw: bytes,
    page_retrieved_at: str,
    detail_retrieved_at: str,
    guide_retrieved_at: str,
    source_media_types: dict[str, str] | None = None,
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

    source_media_types = source_media_types or {}
    page_type = _normalize_content_type(source_media_types.get("catalogue"), "text/html; charset=utf-8")
    detail_type = _normalize_content_type(source_media_types.get("operation_detail"), "text/html; charset=utf-8")
    guide_type = _normalize_content_type(source_media_types.get("reference_guide"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    detail_binding = _make_source_binding("operation_detail_html", detail_raw, detail_type or "text/html", DETAIL_ROUTE, "POST", detail_retrieved_at, "operation_detail")
    page_binding = _make_source_binding("catalogue_detail_page", page_raw, page_type or "text/html", f"/data/{identity['dataset_id']}/openapi.do", "GET", page_retrieved_at, "operation_selector_and_guide_index")
    guide_binding = _make_source_binding("reference_guide_docx", guide_raw, guide_type or "application/octet-stream", DOWNLOAD_ROUTE, "GET", guide_retrieved_at, "service_and_operation_guide")

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
            if name.casefold() in {item.casefold() for item in found}:
                raise EvidenceError("parameter_name_duplicate_in_source")
            required, required_status = _requiredness(get("requiredness"))
            sample_cell = get("sample")
            size_text = get("size")
            raw_type = get("data_type")
            raw_enum = get("enum")
            raw_default = get("default")
            type_text = _safe_metadata_cell(raw_type, field_name="data_type", parameter_name=name)
            enum_values = _split_explicit_enum(get("enum"), parameter_name=name)
            default_text = _safe_metadata_cell(raw_default, field_name="default", parameter_name=name)
            type_status = "documented" if type_text is not None else ("unknown" if raw_type else "not_established")
            enum_status = "documented" if enum_values is not None else ("unknown" if raw_enum else "not_established")
            default_status = "documented" if default_text is not None else ("unknown" if raw_default else "not_established")
            entry: dict[str, Any] = {
                "name": name,
                "requiredness": {"value": required, "status": required_status, "source_refs": [_source_ref(_cell_locator(source_id, kind, table_idx, row_offset, columns["requiredness"], "항목구분", part))]},
                "size": {"value": size_text or None, "status": "documented" if size_text else "unknown", "source_refs": [_source_ref(_cell_locator(source_id, kind, table_idx, row_offset, columns.get("size", 0), "항목크기", part))]},
                "sample": {"present": bool(sample_cell and sample_cell not in {"-", "없음", "N/A"}), "value_stored": False, "source_refs": [_source_ref(_cell_locator(source_id, kind, table_idx, row_offset, columns["sample"], "샘플데이터", part))]},
                "type": {"value": type_text, "status": type_status, "source_refs": [_source_ref(_cell_locator(source_id, kind, table_idx, row_offset, columns["data_type"], part=part), "parameter_data_type")] if "data_type" in columns else []},
                "enum": {"values": enum_values or [], "status": enum_status, "source_refs": [_source_ref(_cell_locator(source_id, kind, table_idx, row_offset, columns["enum"], part=part), "parameter_enum")] if "enum" in columns else []},
                "default": {"value": default_text, "status": default_status, "source_refs": [_source_ref(_cell_locator(source_id, kind, table_idx, row_offset, columns["default"], part=part), "parameter_default")] if "default" in columns else []},
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
    service_tables = guide_tables[:first_operation_index]
    # The shared service interface declaration is only accepted from the
    # pre-operation service section; a method mentioned under another
    # operation cannot authorize the selected operation.
    for table_index, rows in enumerate(service_tables):
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
        type_facts = [x["type"] for x in (d, g) if x]
        type_values = {x["value"] for x in type_facts if x["status"] == "documented"}
        type_unknown = any(x["status"] == "unknown" for x in type_facts)
        data_type = {
            "value": next(iter(type_values)) if len(type_values) == 1 and not type_unknown else None,
            "status": "conflict" if len(type_values) > 1 else ("unknown" if type_unknown else ("documented" if len(type_values) == 1 else "not_established")),
            "source_refs": [ref for x in type_facts for ref in x["source_refs"]],
        }
        enum_facts = [x["enum"] for x in (d, g) if x]
        explicit_enums = [x["values"] for x in enum_facts if x["status"] == "documented"]
        unique_enums = {tuple(values) for values in explicit_enums}
        enum_unknown = any(x["status"] == "unknown" for x in enum_facts)
        enum_fact = {
            "values": list(next(iter(unique_enums))) if len(unique_enums) == 1 else sorted({value for values in explicit_enums for value in values}),
            "status": "conflict" if len(unique_enums) > 1 else ("unknown" if enum_unknown else ("documented" if explicit_enums else "not_established")),
            "source_refs": [ref for x in enum_facts for ref in x["source_refs"]],
        }
        default_facts = [x["default"] for x in (d, g) if x]
        default_values = {x["value"] for x in default_facts if x["status"] == "documented"}
        default_unknown = any(x["status"] == "unknown" for x in default_facts)
        default_fact = {
            "value": next(iter(default_values)) if len(default_values) == 1 and not default_unknown else None,
            "status": "conflict" if len(default_values) > 1 else ("unknown" if default_unknown else ("documented" if len(default_values) == 1 else "not_established")),
            "source_refs": [ref for x in default_facts for ref in x["source_refs"]],
        }
        parameters.append({
            "name": name,
            "location": {"value": "query" if location_in_sample else None, "status": "demonstrated_by_example" if location_in_sample else "unknown", "source_refs": location_refs},
            "cardinality": {"minimum": 1 if requiredness["value"] == "required" else (0 if requiredness["value"] == "optional" else None), "maximum": None, "status": "unknown", "source_refs": requiredness["source_refs"]},
            "requiredness": requiredness,
            "size": size,
            "data_type": data_type,
            "enum": enum_fact,
            "default": default_fact,
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
    quota_facts = _combine_quota_facts([
        _provider_quota_facts(page.tables, source_id="catalogue_detail_page", kind="html_table_cell"),
        _provider_quota_facts(detail.tables, source_id="operation_detail_html", kind="html_table_cell"),
        _provider_quota_facts(service_tables, source_id="reference_guide_docx", kind="docx_table_cell", part="word/document.xml"),
    ])
    explicit_unknowns = [
        "operation_level_http_method_not_declared" if method["value"] is None else "",
        "request_data_types_not_established" if any(p["data_type"]["status"] != "documented" for p in parameters) else "",
        "enum_values_not_established" if any(p["enum"]["status"] != "documented" for p in parameters) else "",
        "defaults_not_established" if any(p["default"]["status"] != "documented" for p in parameters) else "",
        "provider_quota_not_declared_or_parseable" if quota_facts["status"] == "not_parsed" else "",
        "provider_quota_scope_not_established",
        "provider_quota_account_tier_not_established",
        "unsafe_declared_metadata_value_withheld" if any(p["default"]["status"] == "unknown" or p["enum"]["status"] == "unknown" or p["data_type"]["status"] == "unknown" for p in parameters) else "",
        "response_empty_result_semantics_not_declared",
        "response_error_contract_not_established",
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "parser": {"id": PARSER_ID, "version": PARSER_VERSION},
        "identity": identity,
        "operation_document": {"title": {"value": identity["operation_name"], "status": "registered_manifest", "source_refs": identity.get("source_refs", [])}, "purpose": {"value": None, "status": "unknown", "source_refs": []}},
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
            "fixed_query_selectors": [],
        },
        "effect": effect,
        "parameters": parameters,
        "authentication": authentication,
        "limits": {"provider_quota": quota_facts, "request_budget": {"value": None, "status": "not_a_provider_fact", "source_refs": []}},
        "response_assertion": {"kind": "documented_response_fields" if response_fields else "unknown", "fields": response_fields, "empty_result_semantics": {"value": None, "status": "unknown", "source_refs": []}, "source_refs": response_refs},
        "response_contract": _unknown_response_contract(),
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


def _seoul_html_text_blocks(raw: bytes) -> list[tuple[str, str, int, int]]:
    """Return short h1/p text blocks without retaining script or table data."""

    class TextBlocks(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.active: str | None = None
            self.parts: list[str] = []
            self.blocks: list[tuple[str, str, int, int]] = []
            self.source_text = ""
            self.line_starts: list[int] = [0]
            self.content_start: int | None = None

        def source_offset(self) -> int:
            line, column = self.getpos()
            return self.line_starts[line - 1] + column

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag in {"h1", "p"} and self.active is None:
                self.active = tag
                self.parts = []
                start = self.source_offset()
                opening_tag = self.get_starttag_text() or ""
                if not opening_tag.lower().startswith(f"<{tag}") or not opening_tag.endswith(">"):
                    self.active = None
                    raise ValueError("seoul_text_block_open_tag_invalid")
                self.content_start = start + len(opening_tag)

        def handle_data(self, data: str) -> None:
            if self.active is not None:
                self.parts.append(data)

        def handle_endtag(self, tag: str) -> None:
            if self.active == tag:
                text = " ".join("".join(self.parts).split())
                if text and len(text) <= 512 and not re.search(r"https?://|[?&][A-Za-z0-9_]+=", text, re.I):
                    content_end = self.source_offset()
                    content_start = self.content_start if self.content_start is not None else content_end
                    start_byte = len(self.source_text[:content_start].encode("utf-8"))
                    end_byte = len(self.source_text[:content_end].encode("utf-8"))
                    self.blocks.append((tag, text, start_byte, end_byte))
                self.active = None
                self.parts = []
                self.content_start = None

    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_PAGE_BYTES:
        raise EvidenceError("seoul_html_source_size_invalid")
    parser = TextBlocks()
    try:
        parser.source_text = raw.decode("utf-8", errors="strict")
        parser.line_starts.extend(match.end() for match in re.finditer("\n", parser.source_text))
        parser.feed(parser.source_text)
        parser.close()
    except (UnicodeError, ValueError) as exc:
        raise EvidenceError("seoul_html_source_invalid") from exc
    return parser.blocks


def parse_seoul_openapi_evidence(
    operation: dict[str, Any],
    *,
    openapi_raw: bytes,
    dataset_raw: bytes,
    openapi_retrieved_at: str,
    dataset_retrieved_at: str,
) -> dict[str, Any]:
    """Parse source-bound facts for the registered Seoul subway station API.

    The official page provides operation-specific parameter, output-field,
    and provider-code tables. Its sample URL demonstrates path placement but
    does not name the HTTP method, so the registered-candidate GET is not
    promoted to method authority. Sample URL values and response rows are
    never emitted.
    """
    if (
        operation.get("candidate_id") != "seoul-open-data-subway-station-list"
        or operation.get("endpoint_template") != "http://openapi.seoul.go.kr:8088/{KEY}/{format}/{service}/{start_index}/{end_index}"
    ):
        raise EvidenceError("seoul_operation_identity_mismatch")
    if len(openapi_raw) > MAX_PAGE_BYTES or len(dataset_raw) > MAX_PAGE_BYTES:
        raise EvidenceError("seoul_document_size_limit")
    api_page = _parse_html(openapi_raw)
    dataset_page = _parse_html(dataset_raw)
    api_page_source = "seoul_official_openapi_view"
    dataset_source = "seoul_official_dataset_view"

    def table_index_for_header(tables: list[list[list[str]]], header: list[str], code: str) -> int:
        matches = [index for index, rows in enumerate(tables) if rows and rows[0][: len(header)] == header]
        if len(matches) != 1:
            raise EvidenceError(code)
        return matches[0]

    request_table_index = table_index_for_header(api_page.tables, ["변수명", "타입", "변수설명", "값설명"], "seoul_request_table_missing_or_ambiguous")
    output_table_index = table_index_for_header(api_page.tables, ["No", "출력명", "출력설명"], "seoul_output_table_missing_or_ambiguous")
    request_rows = api_page.tables[request_table_index]
    output_rows = api_page.tables[output_table_index]
    sample_tables = [
        (index, rows)
        for index, rows in enumerate(api_page.tables)
        if rows
        and rows[0]
        and rows[0][0].strip() == "샘플 URL"
        and any(re.search(r"https?://", cell, re.I) for row in rows for cell in row)
    ]
    if len(sample_tables) != 1:
        raise EvidenceError("seoul_operation_path_example_missing_or_ambiguous")
    sample_table_index, sample_rows = sample_tables[0]
    sample_cells = [
        (row_index, cell_index, cell)
        for row_index, row in enumerate(sample_rows)
        for cell_index, cell in enumerate(row)
        if re.search(r"https?://", cell, re.I)
    ]
    if not sample_cells:
        raise EvidenceError("seoul_operation_path_example_url_ambiguous")
    sample_urls: list[tuple[int, int, str]] = []
    for row_index, cell_index, cell in sample_cells:
        urls_in_cell = re.findall(r"https?://[^\s<>\"']+", cell)
        if len(urls_in_cell) != 1:
            raise EvidenceError("seoul_operation_path_example_url_ambiguous")
        sample_urls.append((row_index, cell_index, urls_in_cell[0].rstrip("/")))
    sample_row_index, sample_cell_index, sample_url_text = sample_urls[0]
    sample_locator = _cell_locator(
        api_page_source,
        "html_table_cell",
        sample_table_index,
        sample_row_index,
        sample_cell_index,
        "샘플 URL",
    )
    try:
        parsed_sample_urls = [
            (row_index, cell_index, urllib.parse.urlsplit(url))
            for row_index, cell_index, url in sample_urls
        ]
        sample_authorities = [
            (parsed.scheme, parsed.hostname, parsed.port)
            for _row_index, _cell_index, parsed in parsed_sample_urls
        ]
    except ValueError as exc:
        raise EvidenceError("seoul_operation_path_example_invalid") from exc
    if len(set(sample_authorities)) != 1 or sample_authorities[0] != ("http", "openapi.seoul.go.kr", 8088):
        raise EvidenceError("seoul_operation_path_example_endpoint_mismatch")
    if any(
        parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
        for _row_index, _cell_index, parsed in parsed_sample_urls
    ):
        raise EvidenceError("seoul_operation_path_example_endpoint_mismatch")
    sample_url = parsed_sample_urls[0][2]
    sample_port = sample_authorities[0][2]

    blocks = _seoul_html_text_blocks(dataset_raw)
    titles = [block for block in blocks if block[0] == "h1" and block[1] == "서울교통공사_노선별 지하철역 정보"]
    purposes = [block for block in blocks if block[0] == "p" and block[1].startswith("서울교통공사에서 제공하는 ") and block[1].endswith("서비스 입니다.")]
    if len(titles) != 1 or len(purposes) != 1:
        raise EvidenceError("seoul_official_title_or_purpose_ambiguous")
    title_value, purpose_value = titles[0][1], purposes[0][1]
    if _safe_operation_document_text(title_value) is None or _safe_operation_document_text(purpose_value) is None:
        raise EvidenceError("seoul_official_title_or_purpose_unsafe")

    def block_ref(block: tuple[str, str, int, int], source_id: str, evidence_kind: str) -> dict[str, Any]:
        return _source_ref(
            {"source_id": source_id, "kind": "html_byte_range", "byte_start": block[2], "byte_end": block[3]},
            evidence_kind,
        )

    title_ref = block_ref(titles[0], dataset_source, "official_operation_title")
    purpose_ref = block_ref(purposes[0], dataset_source, "official_operation_purpose")
    termination_notice = "해당 데이터는 종료된 서비스입니다."
    termination_blocks = [block for block in blocks if block[0] == "p" and termination_notice in block[1]]
    if len(termination_blocks) != 1:
        raise EvidenceError("seoul_service_termination_status_ambiguous")
    service_status_ref = block_ref(termination_blocks[0], dataset_source, "official_service_termination_notice")
    openapi_binding = _make_source_binding(
        api_page_source,
        openapi_raw,
        "text/html",
        "/dataList/openApiView.do",
        "GET",
        openapi_retrieved_at,
        "operation_parameter_output_and_result_code_tables",
        host="data.seoul.go.kr",
    )
    dataset_binding = _make_source_binding(
        dataset_source,
        dataset_raw,
        "text/html",
        "/dataList/OA-15442/S/1/datasetView.do",
        "GET",
        dataset_retrieved_at,
        "official_dataset_title_and_purpose",
        host="data.seoul.go.kr",
    )

    def cell_ref(table: int, row: int, cell: int, kind: str) -> dict[str, Any]:
        return _source_ref(_cell_locator(api_page_source, "html_table_cell", table, row, cell), kind)

    input_rows: dict[str, tuple[int, list[str]]] = {}
    for row_index, row in enumerate(request_rows[1:], 1):
        if len(row) < 4 or not row[0].strip():
            raise EvidenceError("seoul_parameter_row_invalid")
        name = row[0].strip()
        if name in input_rows:
            raise EvidenceError("seoul_parameter_name_duplicate")
        input_rows[name] = (row_index, row)
    required_names = {"KEY", "TYPE", "SERVICE", "START_INDEX", "END_INDEX"}
    if not required_names.issubset(input_rows):
        raise EvidenceError("seoul_required_parameter_inventory_incomplete")
    service_row_index, service_row = input_rows["SERVICE"]
    operation_name = service_row[3].strip()
    if operation_name != "SearchSTNBySubwayLineInfo":
        raise EvidenceError("seoul_operation_service_identity_mismatch")

    sample_paths = [parsed.path.split("/") for _row_index, _cell_index, parsed in parsed_sample_urls]
    if any(len(path) != 6 or path[0] != "" or path[-1] != "5" for path in sample_paths):
        raise EvidenceError("seoul_operation_path_shape_unsupported")
    # The operation name appears in every source sample path and in the
    # explicit SERVICE request-parameter row. Sample values are never retained.
    if any(path[3] != operation_name for path in sample_paths):
        raise EvidenceError("seoul_operation_path_service_mismatch")
    path_parameter_names = {"KEY", "TYPE", "SERVICE", "START_INDEX", "END_INDEX"}
    param_location_ref = _source_ref(sample_locator, "operation_path_parameters_demonstrated_by_example")
    parameters: list[dict[str, Any]] = []
    auth_refs: list[dict[str, Any]] = []
    for name, (row_index, row) in input_rows.items():
        type_cell = row[1].strip()
        match = re.fullmatch(r"\s*(STRING|String|INTEGER|Integer)\s*(?:\((필수|선택)\))?\s*", type_cell)
        if not match:
            raise EvidenceError("seoul_parameter_type_or_requiredness_ambiguous")
        type_text = "string" if match.group(1).casefold() == "string" else "integer"
        required_text = match.group(2)
        if required_text not in {"필수", "선택"}:
            raise EvidenceError("seoul_parameter_requiredness_unknown")
        requiredness = "required" if required_text == "필수" else "optional"
        name_ref = cell_ref(request_table_index, row_index, 0, "parameter_name")
        type_ref = cell_ref(request_table_index, row_index, 1, "parameter_data_type_and_requiredness")
        description_ref = cell_ref(request_table_index, row_index, 2, "parameter_description")
        value_ref = cell_ref(request_table_index, row_index, 3, "parameter_values")
        location_status = "demonstrated_by_example" if name in path_parameter_names else "unknown"
        location = {"value": "path" if name in path_parameter_names else None, "status": location_status, "source_refs": [param_location_ref] if name in path_parameter_names else [name_ref]}
        enum_values: list[str] = []
        enum_status = "not_established"
        if name == "TYPE":
            enum_values = re.findall(r"\b(?:xmlf|xml|xls|json)\b", row[3], re.I)
            enum_values = list(dict.fromkeys(value.lower() for value in enum_values))
            if set(enum_values) != {"xml", "xmlf", "xls", "json"}:
                raise EvidenceError("seoul_format_enum_ambiguous")
            enum_status = "documented"
        elif name == "SERVICE":
            enum_values = [operation_name]
            enum_status = "documented"
        req_refs = [type_ref]
        parameter = {
            "name": name,
            "location": location,
            "cardinality": {"minimum": 1 if requiredness == "required" else 0, "maximum": None, "status": "documented", "source_refs": [type_ref]},
            "requiredness": {"value": requiredness, "status": "documented", "source_refs": req_refs},
            "size": {"value": None, "observed_values": [], "status": "unknown", "source_refs": [description_ref]},
            "data_type": {"value": type_text, "status": "documented", "source_refs": [type_ref]},
            "enum": {"values": enum_values, "status": enum_status, "source_refs": [value_ref] if enum_status == "documented" else [name_ref]},
            "default": {"value": None, "status": "unknown", "source_refs": [name_ref]},
            "sample": {"present": name in path_parameter_names, "value_stored": False, "source_refs": [param_location_ref] if name in path_parameter_names else []},
            "source_refs": [name_ref, type_ref, description_ref, value_ref],
        }
        parameters.append(parameter)
        if name == "KEY":
            if "인증" not in row[2] or requiredness != "required":
                raise EvidenceError("seoul_credential_requirement_not_established")
            auth_refs.extend([name_ref, type_ref, description_ref, param_location_ref])

    output_refs: list[dict[str, Any]] = []
    declared_output_fields: list[dict[str, Any]] = []
    response_names: list[str] = []
    for row_index, row in enumerate(output_rows[1:], 1):
        if len(row) < 3 or not row[1].strip():
            raise EvidenceError("seoul_output_field_row_invalid")
        name = row[1].strip()
        name_ref = cell_ref(output_table_index, row_index, 1, "declared_output_field_name")
        description_ref = cell_ref(output_table_index, row_index, 2, "declared_output_field_description")
        output_refs.extend([name_ref, description_ref])
        response_names.append(name)
        declared_output_fields.append({"name": name, "data_type": "not_declared_in_output_table", "size": None, "source_refs": [name_ref, description_ref]})
    if not response_names or "RESULT.CODE" not in response_names:
        raise EvidenceError("seoul_result_code_field_not_declared")

    code_tables: list[tuple[int, list[list[str]]]] = []
    for table_index, rows in enumerate(api_page.tables):
        if rows and len(rows[0]) >= 2 and rows[0][0].strip() == "INFO-000" and "정상 처리되었습니다" in rows[0][1]:
            code_tables.append((table_index, rows))
    if len(code_tables) != 1:
        raise EvidenceError("seoul_result_code_table_missing_or_ambiguous")
    code_table_index, code_rows = code_tables[0]
    codes: list[dict[str, Any]] = []
    success_values: list[str] = []
    error_values: list[str] = []
    success_code_refs: list[dict[str, Any]] = []
    error_code_refs: list[dict[str, Any]] = []
    info_empty_ref: dict[str, Any] | None = None
    code_refs: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for row_index, row in enumerate(code_rows):
        if len(row) < 2:
            raise EvidenceError("seoul_result_code_row_invalid")
        code = row[0].strip()
        if not re.fullmatch(r"(?:INFO|ERROR)-[0-9]{3}", code) or code in seen_codes:
            raise EvidenceError("seoul_result_code_name_or_duplicate_invalid")
        seen_codes.add(code)
        code_ref = cell_ref(code_table_index, row_index, 0, "provider_result_code")
        message_ref = cell_ref(code_table_index, row_index, 1, "provider_result_code_semantics")
        code_refs.extend([code_ref, message_ref])
        if code == "INFO-000":
            success_values.append(code)
            success_code_refs.extend([code_ref, message_ref])
            continue
        if code == "INFO-200":
            classification = "empty_result"
            info_empty_ref = _source_ref(
                _cell_locator(api_page_source, "html_table_cell", code_table_index, row_index, 1),
                "provider_no_matching_data_code_semantics",
            )
        elif code == "ERROR-336":
            classification = "size_limit"
        elif code.startswith("ERROR-5") or code.startswith("ERROR-6"):
            classification = "server"
        elif code.startswith("ERROR-"):
            classification = "bad_request"
            error_values.append(code)
            error_code_refs.extend([code_ref, message_ref])
        else:
            classification = "unknown"
        codes.append({"value": code, "classification": classification, "source_refs": [code_ref, message_ref]})
    if not success_values or not info_empty_ref:
        raise EvidenceError("seoul_result_success_or_empty_semantics_missing")

    identity_refs = [
        _source_ref(_cell_locator(api_page_source, "html_table_cell", request_table_index, service_row_index, 0), "operation_service_parameter_name"),
        _source_ref(_cell_locator(api_page_source, "html_table_cell", request_table_index, service_row_index, 3), "operation_service_name"),
        title_ref,
    ]
    identity = {
        "source_id": "seoul_open_data",
        "operation_id": "seoul-open-data-subway-station-list",
        "provider": "data.seoul.go.kr",
        "protocol": "REST",
        "operation_name": operation_name,
        "source_refs": identity_refs,
    }
    response_contract = _unknown_response_contract()
    response_contract["coded_result_field_inventory"] = {"status": "unknown", "candidates": [], "source_refs": [*output_refs]}
    response_contract["provider_result_codes"] = {
        "status": "documented",
        "evidence_strength": "explicit_documentation",
        "path": None,
        "value_type": "string",
        "success_values": {"status": "documented", "values": success_values, "source_refs": success_code_refs},
        "error_values": {"status": "documented", "values": error_values, "source_refs": error_code_refs},
        "source_refs": [*code_refs, *output_refs],
    }
    response_contract["declared_output_fields"] = declared_output_fields
    response_contract["documented_error_contract"] = {
        "status": "documented",
        "format": "unknown",
        "code_path": None,
        "message_path": None,
        "codes": codes,
        "source_refs": code_refs,
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "parser": {"id": PARSER_ID, "version": PARSER_VERSION},
        "identity": identity,
        "operation_document": {
            "title": {"value": title_value, "status": "documented", "source_refs": [title_ref]},
            "purpose": {"value": purpose_value, "status": "documented", "source_refs": [purpose_ref]},
            "service_status": {
                "classification": "terminated",
                "status": "documented",
                "source_refs": [service_status_ref],
            },
        },
        "source_bindings": [openapi_binding, dataset_binding],
        "parse_status": "parsed_with_unknowns",
        "transport": {
            "protocol": {"value": "REST", "status": "registered_manifest", "source_refs": []},
            "scheme": {"value": "http", "status": "documented", "source_refs": [param_location_ref]},
            "host": {"value": "openapi.seoul.go.kr", "status": "documented", "source_refs": [param_location_ref]},
            "port": sample_port,
            "port_source_refs": [param_location_ref],
            "path": {"value": "/{KEY}/{TYPE}/{SERVICE}/{START_INDEX}/{END_INDEX}", "status": "documented", "source_refs": [param_location_ref]},
            "http_method": {"value": None, "status": "unknown", "authority_scope": "not_found_in_parsed_operation_sources", "source_refs": []},
            "soap_action": {"value": None, "status": "not_applicable", "source_refs": []},
            "soap_version": {"value": None, "status": "not_applicable", "source_refs": []},
            "envelope_namespace": {"value": None, "status": "not_applicable", "source_refs": []},
            "operation_qname": {"value": None, "status": "not_applicable", "source_refs": []},
            "body_encoding": {"value": None, "status": "not_applicable", "source_refs": []},
            "fixed_query_selectors": [],
        },
        "effect": {"classification": None, "status": "unknown", "authority": "operation_document", "source_refs": []},
        "parameters": parameters,
        "authentication": {
            "requirement": "required",
            "status": "documented",
            "mechanism": "service_key",
            "parameter_names": ["KEY"],
            "placement": "path",
            "source_refs": auth_refs,
        },
        "limits": {
            "provider_quota": {"value": None, "unit": None, "status": "not_parsed", "source_refs": [], "scope": {"value": None, "status": "unknown", "source_refs": []}, "account_tier": {"value": None, "status": "unknown", "source_refs": []}},
            "request_budget": {"value": None, "status": "not_a_provider_fact", "source_refs": []},
        },
        "response_assertion": {
            "kind": "documented_response_fields",
            "fields": response_names,
            "empty_result_semantics": {"value": None, "status": "unknown", "source_refs": [info_empty_ref]},
            "source_refs": [*output_refs, *code_refs],
        },
        "response_contract": response_contract,
        "explicit_unknowns": [
            "operation_http_method_not_established",
            "provider_quota_scope_not_established",
            "provider_quota_account_tier_not_established",
            "provider_max_rows_per_request_is_described_by_error_code_336_but_not_normalized_as_a_limit_fact",
            "response_http_status_not_established",
            "response_format_is_selected_by_TYPE_but_exact_success_payload_shape_is_not_declared",
            "response_result_code_path_depends_on_selected_format_and_is_not_normalized",
            "success_result_collection_and_empty_collection_shape_not_established",
        ],
    }
    _validate_evidence(result)
    return result


def parse_kosis_evidence(
    operation: dict[str, Any],
    *,
    guide_raw: bytes,
    manual_raw: bytes,
    guide_retrieved_at: str,
    manual_retrieved_at: str,
) -> dict[str, Any]:
    """Parse the exact registered KOSIS table-selection operation from official docs.

    This parser never interprets the fixed `method=getList` selector as an HTTP
    method and never promotes examples/defaults into request values.
    """
    candidate_id = operation.get("candidate_id")
    if candidate_id != "kosis-statistics-data-dt-1b41" or operation.get("endpoint_template") != "https://kosis.kr/openapi/Param/statisticsParameterData.do?method=getList":
        raise EvidenceError("kosis_operation_identity_mismatch")
    if not guide_raw.startswith(b"<!") and b"statisticsParameterData.do" not in guide_raw:
        raise EvidenceError("kosis_guide_operation_not_found")
    if not manual_raw.startswith(b"%PDF-"):
        raise EvidenceError("kosis_manual_format_invalid")
    if len(guide_raw) > MAX_KOSIS_GUIDE_BYTES or len(manual_raw) > MAX_KOSIS_MANUAL_BYTES:
        raise EvidenceError("kosis_document_size_limit")
    if _sha256(guide_raw) != KOSIS_GUIDE_SHA256 or _sha256(manual_raw) != KOSIS_MANUAL_SHA256:
        raise EvidenceError("kosis_document_revision_unreviewed")
    try:
        manual_page = subprocess.run(
            ["pdftotext", "-layout", "-f", "16", "-l", "16", "-", "-"],
            input=manual_raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvidenceError("kosis_manual_page_extraction_failed") from exc
    manual_page_text = manual_page.stdout.decode("utf-8", errors="replace")
    if manual_page.returncode != 0 or len(manual_page.stdout) > 65536 or "1.4.2" not in manual_page_text or "분당 200건" not in manual_page_text:
        raise EvidenceError("kosis_manual_error_quota_facts_unverified")
    guide = _parse_html(guide_raw)
    if len(guide.tables) <= 3 or len(guide.tables[2]) < 2 or len(guide.tables[3]) < 2:
        raise EvidenceError("kosis_operation_tables_missing")
    request_rows = guide.tables[2]
    output_rows = guide.tables[3]
    request_header = request_rows[0]
    output_header = output_rows[0]
    if request_header[:2] != ["요청변수", "변수타입"] or output_header[:3] != ["출력변수", "설명", "형식"]:
        raise EvidenceError("kosis_operation_table_shape_ambiguous")

    guide_source_id = "kosis_official_devguide"
    manual_source_id = "kosis_official_api_manual"
    endpoint_bytes = b"/openapi/Param/statisticsParameterData.do?method=getList"
    endpoint_start = guide_raw.find(endpoint_bytes)
    if endpoint_start < 0 or guide_raw.find(endpoint_bytes, endpoint_start + 1) >= 0:
        raise EvidenceError("kosis_operation_endpoint_ambiguous")
    endpoint_locator = {"source_id": guide_source_id, "kind": "html_byte_range", "byte_start": endpoint_start, "byte_end": endpoint_start + len(endpoint_bytes)}
    title_bytes = "통계표선택 방법".encode("utf-8")
    title_start = guide_raw.find(title_bytes)
    if title_start < 0:
        raise EvidenceError("kosis_operation_title_missing")
    title_locator = {"source_id": guide_source_id, "kind": "html_byte_range", "byte_start": title_start, "byte_end": title_start + len(title_bytes)}

    guide_binding = _make_source_binding(guide_source_id, guide_raw, "text/html", "/openapi/devGuide/devGuide_0201List.do", "GET", guide_retrieved_at, "operation_request_and_declared_output_tables", host="kosis.kr")
    manual_binding = _make_source_binding(manual_source_id, manual_raw, "application/pdf", "/openapi/file/openApi_manual_v1.0.pdf", "GET", manual_retrieved_at, "source_error_and_quota_semantics", host="kosis.kr")

    def cell_ref(source_id: str, table: int, row: int, cell: int, kind: str) -> dict[str, Any]:
        return _source_ref(_cell_locator(source_id, "html_table_cell", table, row, cell), kind)

    def doc_unknown(source_refs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"value": None, "status": "unknown", "source_refs": source_refs or []}

    parameters: list[dict[str, Any]] = []
    auth_refs: list[dict[str, Any]] = []
    conditional_fields = {"startPrdDe", "endPrdDe", "newEstPrdCnt", "prdInterval"}
    for row_index, row in enumerate(request_rows[1:], 1):
        if len(row) < 2:
            raise EvidenceError("kosis_request_parameter_row_invalid")
        type_index = next((index for index, cell in enumerate(row) if cell.strip() in {"String", "Integer", "Number", "Boolean"}), None)
        if type_index is None or type_index == 0:
            raise EvidenceError("kosis_request_parameter_type_missing")
        name_index = next((index for index in range(type_index - 1, -1, -1) if re.search(r"[A-Za-z][A-Za-z0-9]*", row[index])), None)
        raw_name = row[name_index].strip() if name_index is not None else ""
        data_type = row[type_index].strip()
        name_match = re.search(r"([A-Za-z][A-Za-z0-9]*)\s*$", raw_name)
        range_match = re.fullmatch(r"\s*(objL)([0-9]+)\s*~\s*(objL)([0-9]+)\s*", raw_name, re.IGNORECASE)
        names = [f"objL{number}" for number in range(int(range_match.group(2)), int(range_match.group(4)) + 1)] if range_match else ([name_match.group(1)] if name_match else [])
        if not names or data_type != "String":
            raise EvidenceError("kosis_request_parameter_name_or_type_ambiguous")
        note_text = " ".join(row[type_index + 1:])
        if "필수" in note_text:
            requiredness_value = "required"
            requiredness_status = "documented"
        elif "선택" in note_text and not any(name in conditional_fields for name in names):
            requiredness_value = "optional"
            requiredness_status = "documented"
        elif any(name in conditional_fields for name in names):
            requiredness_value = "conditional"
            requiredness_status = "documented"
        else:
            requiredness_value = None
            requiredness_status = "unknown"
        name_ref = cell_ref(guide_source_id, 2, row_index, name_index, "parameter_name")
        type_ref = cell_ref(guide_source_id, 2, row_index, type_index, "parameter_data_type")
        required_index = next((index for index, cell in enumerate(row) if "필수" in cell or "선택" in cell), len(row) - 1)
        required_ref = cell_ref(guide_source_id, 2, row_index, required_index, "parameter_requiredness")
        for name in names:
            row_refs = [name_ref, type_ref, required_ref]
            if name == "apiKey":
                auth_refs.extend([name_ref, cell_ref(guide_source_id, 2, row_index, min(type_index + 1, len(row) - 1), "authentication_credential_description"), required_ref])
            parameters.append({
                "name": name,
                "location": {"value": "query", "status": "documented", "source_refs": [name_ref, _source_ref(endpoint_locator, "operation_request_url_query_parameters")]},
                "cardinality": {"minimum": None, "maximum": None, "status": "unknown", "source_refs": [name_ref]},
                "requiredness": {"value": requiredness_value, "status": requiredness_status, "condition": "source_describes_alternative_period_selection" if requiredness_value == "conditional" else None, "source_refs": [required_ref]},
                "size": {"value": None, "observed_values": [], "status": "unknown", "source_refs": []},
                "data_type": {"value": data_type, "status": "documented", "source_refs": [type_ref]},
                "enum": {"values": [], "status": "unknown", "source_refs": [name_ref]},
                "default": {"value": None, "status": "unknown", "source_refs": [name_ref]},
                "sample": {"present": False, "value_stored": False, "source_refs": []},
                "source_refs": row_refs,
            })

    outputs: list[dict[str, Any]] = []
    for row_index, row in enumerate(output_rows[1:], 1):
        if len(row) < 3 or not row[0].strip() or not row[2].strip():
            raise EvidenceError("kosis_declared_output_field_invalid")
        output_name = row[0].strip()
        output_type = row[2].strip()
        refs = [cell_ref(guide_source_id, 3, row_index, 0, "declared_output_field_name"), cell_ref(guide_source_id, 3, row_index, 2, "declared_output_field_type")]
        outputs.append({"name": output_name, "data_type": output_type, "size": output_type.partition("(")[2].rstrip(")") or None, "source_refs": refs})

    manual_ref = lambda entry, kind="kosis_manual_error_contract": _source_ref({"source_id": manual_source_id, "kind": "pdf_page_section", "page_number": 16, "section": "1.4", "entry": entry}, kind)
    error_contract_ref = manual_ref("XML error envelope fields err and errMsg")
    code_rows = [("10", "credential"), ("11", "credential"), ("20", "bad_request"), ("21", "bad_request"), ("30", "empty_result"), ("31", "size_limit"), ("40", "quota"), ("41", "size_limit"), ("42", "quota"), ("50", "server")]
    documented_error_codes = [{"value": code, "classification": classification, "source_refs": [manual_ref(f"error code {code}")]} for code, classification in code_rows]
    unknown_response = _unknown_response_contract()
    unknown_response["declared_output_fields"] = outputs
    unknown_response["documented_error_contract"] = {
        "status": "documented",
        "format": "xml",
        "code_path": {"kind": "xml_qname_path", "segments": [{"namespace": None, "local_name": "error"}, {"namespace": None, "local_name": "err"}]},
        "message_path": {"kind": "xml_qname_path", "segments": [{"namespace": None, "local_name": "error"}, {"namespace": None, "local_name": "errMsg"}]},
        "codes": documented_error_codes,
        "source_refs": [error_contract_ref],
    }
    identity_refs = [_source_ref(endpoint_locator, "registered_operation_endpoint_binding"), _source_ref(title_locator, "official_operation_title")]
    identity = {"source_id": "kosis", "operation_id": candidate_id, "provider": "KOSIS", "protocol": "REST", "operation_name": operation.get("label"), "source_refs": identity_refs}
    candidate_path = ROOT / "reports/kosis/runtime-candidates.json"
    profile_path = ROOT / "sources/kosis.json"
    candidate_document = _strict_read_json(candidate_path)
    profile_document = _strict_read_json(profile_path)
    candidate_matches = [item for item in candidate_document.get("candidates", []) if item.get("candidate_id") == candidate_id]
    if len(candidate_matches) != 1 or candidate_matches[0].get("endpoint_template") != operation.get("endpoint_template") or profile_document.get("source_id") != "kosis":
        raise EvidenceError("kosis_registry_binding_mismatch")
    registry_binding = {
        "registry_revision": KOSIS_REGISTRY_REVISION,
        "source_id": "kosis",
        "operation_id": candidate_id,
        "source_profile_artifact": "sources/kosis.json",
        "source_profile_sha256": _sha256(profile_path.read_bytes()),
        "source_profile_json_pointer": "#/source_id",
        "candidate_artifact": "reports/kosis/runtime-candidates.json",
        "candidate_artifact_sha256": _sha256(candidate_path.read_bytes()),
        "candidate_json_pointer": "#/candidates/0",
    }
    quota_ref = manual_ref("error code 40 rate limit of 200 requests per minute", "provider_quota")
    result = {
        "schema_version": SCHEMA_VERSION,
        "parser": {"id": PARSER_ID, "version": PARSER_VERSION},
        "identity": identity,
        "registry_binding": registry_binding,
        "operation_document": {
            "title": {"value": "통계표선택 방법", "status": "documented", "source_refs": [_source_ref(title_locator, "official_operation_title")]},
            "purpose": {"value": None, "status": "not_found_in_parsed_operation_sources", "source_refs": []},
        },
        "source_bindings": [guide_binding, manual_binding],
        "parse_status": "parsed_with_unknowns",
        "transport": {
            "protocol": {"value": "REST", "status": "documented", "source_refs": [identity_refs[0]]},
            "scheme": {"value": "https", "status": "documented", "source_refs": [identity_refs[0]]},
            "host": {"value": "kosis.kr", "status": "documented", "source_refs": [identity_refs[0]]},
            "path": {"value": "/openapi/Param/statisticsParameterData.do", "status": "documented", "source_refs": [identity_refs[0]]},
            "http_method": {"value": None, "status": "unknown", "authority_scope": "not_found_in_parsed_operation_sources", "source_refs": []},
            "soap_action": {"value": None, "status": "not_applicable", "source_refs": []},
            "soap_version": {"value": None, "status": "not_applicable", "source_refs": []},
            "envelope_namespace": {"value": None, "status": "not_applicable", "source_refs": []},
            "operation_qname": {"value": None, "status": "not_applicable", "source_refs": []},
            "body_encoding": {"value": None, "status": "not_applicable", "source_refs": []},
            "fixed_query_selectors": [{"name": "method", "value": "getList", "role": "operation_selector", "status": "documented", "source_refs": [_source_ref(endpoint_locator, "operation_fixed_query_selector")] }],
        },
        "effect": {"classification": "read_only", "status": "documented", "authority": "operation_document", "source_refs": [_source_ref(title_locator, "statistics_table_selection_operation"), *[item["source_refs"][0] for item in outputs[:1]]]},
        "parameters": parameters,
        "authentication": {"requirement": "required", "status": "documented", "mechanism": "api_key", "parameter_names": ["apiKey"], "placement": "query", "source_refs": auth_refs},
        "limits": {"provider_quota": {"value": 200, "unit": "requests/minute", "status": "documented", "source_refs": [quota_ref], "scope": {"value": None, "status": "unknown", "source_refs": []}, "account_tier": {"value": None, "status": "unknown", "source_refs": []}}, "request_budget": {"value": None, "status": "not_a_provider_fact", "source_refs": []}},
        "response_assertion": {"kind": "unknown", "fields": [], "empty_result_semantics": {"value": None, "status": "unknown", "source_refs": []}, "source_refs": []},
        "response_contract": unknown_response,
        "explicit_unknowns": ["operation_http_method_not_established", "parameter_cardinality_not_established", "period_selection_condition_not_fully_normalized", "success_http_status_not_established", "success_response_shape_not_established", "success_result_collection_not_established", "successful_result_code_semantics_not_established", "operation_purpose_sentence_not_found_in_parsed_sources", "provider_quota_scope_not_established", "provider_quota_account_tier_not_established", "response_empty_result_semantics_not_declared"],
    }
    _validate_evidence(result)
    return result


def _validate_evidence(value: dict[str, Any]) -> None:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("evidence_schema_version_invalid")
    if value.get("parse_status") not in {"parsed_with_unknowns", "parsed_complete", "ambiguous", "unsupported"}:
        raise EvidenceError("evidence_parse_status_invalid")
    bindings_by_id: dict[str, dict[str, Any]] = {}
    for binding in value.get("source_bindings", []):
        if not re.fullmatch(r"[a-f0-9]{64}", binding.get("sha256", "")) or binding.get("bytes", 0) <= 0:
            raise EvidenceError("evidence_source_binding_invalid")
        if binding.get("origin", {}).get("host") not in {HOST, "www.safetydata.go.kr", "kosis.kr", "ecos.bok.or.kr", "open.assembly.go.kr", "data.seoul.go.kr", "openapi.seoul.go.kr"} or "?" in binding.get("origin", {}).get("path", ""):
            raise EvidenceError("evidence_source_origin_unsafe")
        source_id = binding.get("source_id")
        if not isinstance(source_id, str) or source_id in bindings_by_id:
            raise EvidenceError("evidence_source_id_duplicate_or_missing")
        bindings_by_id[source_id] = binding
    source_id = value.get("identity", {}).get("source_id")
    if source_id not in {"data_go_kr", "kosis", "ecos", "open_assembly", "seoul_open_data"}:
        raise EvidenceError("evidence_identity_source_id_invalid")
    for source_ref in _iter_source_refs(value):
        locator_source = source_ref.get("locator", {}).get("source_id")
        if locator_source not in bindings_by_id:
            raise EvidenceError("evidence_source_reference_unbound")
    transport = value.get("transport", {})
    if "port" in transport and transport["port"] is not None:
        port = transport["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise EvidenceError("evidence_transport_port_invalid")
        if not transport.get("port_source_refs"):
            raise EvidenceError("evidence_transport_port_source_missing")
    if source_id == "kosis":
        identity = value["identity"]
        selectors = value.get("transport", {}).get("fixed_query_selectors", [])
        if identity.get("operation_id") != "kosis-statistics-data-dt-1b41" or identity.get("provider") != "KOSIS" or identity.get("protocol") != "REST":
            raise EvidenceError("kosis_operation_identity_invalid")
        if value.get("transport", {}).get("http_method", {}).get("value") is not None:
            raise EvidenceError("kosis_http_method_must_remain_source_derived")
        if not isinstance(selectors, list) or len(selectors) != 1 or selectors[0].get("name") != "method" or selectors[0].get("value") != "getList" or selectors[0].get("role") != "operation_selector":
            raise EvidenceError("kosis_fixed_query_selector_invalid")
    for parameter in value.get("parameters", []):
        if parameter.get("sample", {}).get("value_stored") is not False:
            raise EvidenceError("sample_value_must_not_be_stored")
        if parameter.get("sample", {}).get("present") not in {True, False}:
            raise EvidenceError("sample_presence_invalid")


def _iter_source_refs(value: Any):
    if isinstance(value, dict):
        if isinstance(value.get("locator"), dict) and isinstance(value.get("evidence_kind"), str):
            yield value
        for child in value.values():
            yield from _iter_source_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_source_refs(child)


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


class _OfficialDocumentRequestPacer:
    """Serialize source-document requests with a conservative 1 s interval."""

    def __init__(self, interval_seconds: float = MIN_DOCUMENT_REQUEST_INTERVAL_SECONDS) -> None:
        self.interval_seconds = interval_seconds
        self.last_started: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self.last_started is not None:
            remaining = self.interval_seconds - (now - self.last_started)
            if remaining > 0:
                time.sleep(remaining)
        self.last_started = time.monotonic()


def _capture_root_size(path: pathlib.Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
    except OSError as exc:
        raise EvidenceError("capture_root_size_unavailable") from exc
    return total


def _write_captured_document(root: pathlib.Path, path: pathlib.Path, raw: bytes) -> None:
    previous_bytes = path.stat().st_size if path.is_file() else 0
    if _capture_root_size(root) - previous_bytes + len(raw) > MAX_PRIVATE_CAPTURE_ROOT_BYTES:
        raise EvidenceError("source_capture_disk_budget_exceeded")
    write_capture(path, raw)


def prepare_private_capture_root(path: pathlib.Path) -> pathlib.Path:
    if path.is_symlink():
        raise EvidenceError("capture_root_symlink_rejected")
    resolved = path.resolve()
    protected = {pathlib.Path("/"), pathlib.Path("/tmp"), pathlib.Path("/var/tmp"), pathlib.Path.home(), ROOT.resolve()}
    if resolved in {item.resolve() for item in protected} or ROOT.resolve() in resolved.parents:
        raise EvidenceError("capture_root_not_private_dedicated_path")
    if len(resolved.parts) < 3:
        raise EvidenceError("capture_root_not_private_dedicated_path")
    try:
        resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not resolved.is_dir():
            raise EvidenceError("capture_root_not_directory")
        resolved.chmod(0o700)
    except OSError as exc:
        raise EvidenceError("capture_root_not_writable") from exc
    if resolved.stat().st_mode & 0o077:
        raise EvidenceError("capture_root_permissions_invalid")
    return resolved


def _capture_document_status(reason_code: str | None) -> tuple[str, str]:
    if reason_code is None:
        return "acquired", "run_offline_document_parser"
    if reason_code == "guide_format_unsupported_legacy_ole_document":
        return "unsupported", "implement_legacy_ole_or_hwp_guide_adapter"
    if reason_code == "safetydata_parser_not_yet_available":
        return "unsupported", "implement_safetydata_document_profile"
    if reason_code in {"official_document_not_found", "operation_document_attachment_not_found"}:
        return "missing", "locate_official_document"
    if any(token in reason_code for token in ("ambiguous", "duplicate", "conflict")):
        return "ambiguous", "resolve_operation_document_match"
    if any(token in reason_code for token in ("not_yet_available", "unsupported", "format_unavailable")):
        return "unsupported", "implement_or_register_document_adapter"
    # Transport, deadline, changed-source, and unclassified failures stay
    # retryable. A transient documentation failure is never evidence that the
    # registered provider operation or its documentation is absent.
    return "retryable", "retry_official_document_capture"


def _write_capture_receipt(
    folder: pathlib.Path,
    identity: dict[str, Any],
    *,
    status: str,
    reason_code: str | None,
    content_types: dict[str, str] | None = None,
    retrieved_at: dict[str, str] | None = None,
) -> None:
    if content_types is None:
        content_types = _capture_content_types(folder)
    if retrieved_at is None:
        retrieved_at = _capture_retrieval_times(folder)
    documents = []
    for role, filename in (("catalogue", "catalogue.html"), ("operation_detail", "operation-detail.html"), ("reference_guide", "reference-guide.bin")):
        path = folder / filename
        present = path.is_file()
        raw = path.read_bytes() if present else b""
        detected_type, detected_encoding = _document_media_type(role, raw) if present else (None, None)
        media_type = _normalize_content_type(content_types.get(role), detected_type) if present else None
        encoding = _content_type_encoding(media_type) if present else None
        if present and encoding is None:
            encoding = detected_encoding
        documents.append({"role": role, "present": present, "bytes": len(raw), "sha256": _sha256(raw) if present else None, "media_type": media_type, "encoding": encoding, "retrieved_at": retrieved_at.get(role) if present else None})
    evidence_path = folder / "evidence.json"
    receipt = {
        "schema_version": "datapan.operation-document-capture-receipt.v2",
        "operation_id": identity["operation_id"],
        "status": status,
        "reason_code": reason_code,
        "attempted_at": _utc_now(),
        "documents": documents,
        "evidence_sha256": _sha256(evidence_path.read_bytes()) if evidence_path.is_file() else None,
    }
    write_capture(folder / "capture-receipt.json", _json_bytes(receipt))


def _document_media_type(role: str, raw: bytes) -> tuple[str, str]:
    if role in {"catalogue", "operation_detail"}:
        return "text/html", "utf-8"
    if raw.startswith(b"PK\x03\x04"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "word/document.xml UTF-8"
    if raw.startswith(b"%PDF-"):
        return "application/pdf", "binary"
    if raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/x-ole-storage", "binary"
    if raw.lstrip().startswith((b"<html", b"<!DOCTYPE html")):
        return "text/html", "utf-8"
    return "application/octet-stream", "binary"


def _normalize_content_type(value: str | None, fallback: str | None) -> str | None:
    if isinstance(value, str) and len(value) <= 256 and re.fullmatch(
        r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:\s*;\s*charset\s*=\s*[A-Za-z0-9._-]{1,64})?",
        value.strip(),
        flags=re.I,
    ):
        return re.sub(r"\s*;\s*", "; ", value.strip())
    return fallback


def _content_type_encoding(media_type: str | None) -> str | None:
    if not media_type:
        return None
    match = re.search(r"(?i);\s*charset\s*=\s*([A-Za-z0-9._-]+)", media_type)
    return match.group(1).lower() if match else None


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
        "openapi_document": re.fullmatch(r"/catalog/[0-9]+/openapi\.json", parsed.path) is not None,
        "operation_detail": parsed.path == DETAIL_ROUTE,
        "reference_guide": parsed.path == DOWNLOAD_ROUTE,
    }
    if not allowed_paths.get(purpose, False):
        raise EvidenceError("source_path_outside_allowlist")
    if purpose == "catalogue" and parsed.query:
        raise EvidenceError("catalogue_query_not_allowed")
    if purpose == "openapi_document" and parsed.query:
        raise EvidenceError("openapi_query_not_allowed")
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
        process_started = False
        try:
            process.start()
            process_started = True
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
            if process_started and process.is_alive():
                process.kill()
                process.join(1.0)
            raise
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            if process_started and process.is_alive():
                process.kill()
                process.join(1.0)
            code = "source_capture_failed" if process_started else "source_deadline_process_unavailable"
            raise EvidenceError(code) from exc


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
        if response.status == 404:
            raise EvidenceError("official_document_not_found")
        if response.status != 200:
            raise EvidenceError("source_http_status_rejected")
        content_encoding = response.getheader("Content-Encoding", "identity").lower()
        if content_encoding not in {"", "identity"}:
            raise EvidenceError("source_compression_rejected")
        content_length_headers = response.headers.get_all("Content-Length") or []
        transfer_encoding_headers = response.headers.get_all("Transfer-Encoding") or []
        if len(content_length_headers) > 1 or len(transfer_encoding_headers) > 1 or (content_length_headers and transfer_encoding_headers):
            raise EvidenceError("source_framing_invalid")
        transfer_encoding = transfer_encoding_headers[0].strip().lower() if transfer_encoding_headers else ""
        if transfer_encoding not in {"", "chunked"}:
            raise EvidenceError("source_framing_invalid")
        content_length = content_length_headers[0].strip() if content_length_headers else response.getheader("Content-Length")
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


def _capture_one(
    operation: dict[str, Any],
    capture_root: pathlib.Path,
    request_pacer: _OfficialDocumentRequestPacer | None = None,
) -> dict[str, Any]:
    identity = _safe_operation_identity(operation)
    dataset_id, key = identity["dataset_id"], identity["upstream_operation_key"]
    folder = capture_root / dataset_id / key
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    if identity["source_system"] == "safetydata.go.kr":
        raise EvidenceError("safetydata_parser_not_yet_available")
    if not re.fullmatch(r"[0-9]+", key):
        raise EvidenceError("upstream_operation_key_unsupported")
    page_path = folder / "catalogue.html"
    detail_path = folder / "operation-detail.html"
    guide_path = folder / "reference-guide.bin"
    cookies: dict[str, str] = {}
    source_media_types = _capture_content_types(folder)
    source_retrieved_at = _capture_retrieval_times(folder)
    if page_path.exists():
        page_raw = page_path.read_bytes()
        page_at = source_retrieved_at.get("catalogue") or _capture_file_time(page_path)
        if not detail_path.exists() or not guide_path.exists():
            if request_pacer:
                request_pacer.wait()
            refreshed_page, _headers, new_cookies = _fetch_official(f"https://{HOST}/data/{dataset_id}/openapi.do", purpose="catalogue", max_bytes=MAX_PAGE_BYTES, cookies=cookies)
            cookies.update(new_cookies)
            if _sha256(refreshed_page) != _sha256(page_raw):
                raise EvidenceError("catalogue_document_changed_during_resume")
            source_media_types["catalogue"] = _headers.get("content-type", "")
    else:
        if request_pacer:
            request_pacer.wait()
        page_raw, page_headers, new_cookies = _fetch_official(f"https://{HOST}/data/{dataset_id}/openapi.do", purpose="catalogue", max_bytes=MAX_PAGE_BYTES, cookies=cookies)
        cookies.update(new_cookies)
        page_at = page_headers.get("date") or _utc_now()
        source_retrieved_at["catalogue"] = page_at
        source_media_types["catalogue"] = page_headers.get("content-type", "")
        _write_captured_document(capture_root, page_path, page_raw)
    page = _parse_html(page_raw)
    page_matches = [item for item in page.options if item.get("value") == key and item.get("name") == identity["operation_name"]]
    if page.hidden.get("publicDataPk") != [dataset_id]:
        raise EvidenceError("operation_selector_identity_ambiguous")
    if not page_matches:
        try:
            evidence = parse_openapi_evidence(
                operation,
                page_raw=page_raw,
                page_retrieved_at=page_at,
                page_media_type=source_media_types.get("catalogue"),
            )
        except EvidenceError as exc:
            raise EvidenceError("operation_selector_identity_ambiguous") from exc
        evidence_raw = _json_bytes(evidence)
        write_capture(folder / "evidence.json", evidence_raw)
        _write_capture_receipt(folder, identity, status="acquired", reason_code=None, content_types=source_media_types, retrieved_at=source_retrieved_at)
        return {"operation_id": identity["operation_id"], "dataset_id": dataset_id, "upstream_operation_key": key, "status": evidence["parse_status"], "evidence_sha256": _sha256(evidence_raw), "evidence_bytes": len(evidence_raw)}
    if len(page_matches) != 1 or len(page.hidden.get("publicDataDetailPk", [])) != 1:
        raise EvidenceError("operation_selector_identity_ambiguous")
    detail_pk = page.hidden["publicDataDetailPk"][0]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,255}", detail_pk):
        raise EvidenceError("page_detail_key_invalid")
    if detail_path.exists():
        detail_raw = detail_path.read_bytes()
        detail_at = source_retrieved_at.get("operation_detail") or _capture_file_time(detail_path)
    else:
        body = urllib.parse.urlencode({"oprtinSeqNo": key, "publicDataDetailPk": detail_pk, "publicDataPk": dataset_id}).encode("ascii")
        if request_pacer:
            request_pacer.wait()
        detail_raw, detail_headers, new_cookies = _fetch_official(f"https://{HOST}{DETAIL_ROUTE}", purpose="operation_detail", method="POST", body=body, max_bytes=MAX_DETAIL_BYTES, cookies=cookies)
        cookies.update(new_cookies)
        detail_at = detail_headers.get("date") or _utc_now()
        source_retrieved_at["operation_detail"] = detail_at
        source_media_types["operation_detail"] = detail_headers.get("content-type", "")
        _write_captured_document(capture_root, detail_path, detail_raw)
    attachments = page.attachments
    if not attachments:
        raise EvidenceError("operation_document_attachment_not_found")
    if len(attachments) != 1:
        raise EvidenceError("guide_attachment_ambiguous")
    file_id, file_sn = attachments[0]
    if guide_path.exists():
        guide_raw = guide_path.read_bytes()
        guide_at = source_retrieved_at.get("reference_guide") or _capture_file_time(guide_path)
    else:
        query = urllib.parse.urlencode({"atchFileId": file_id, "fileDetailSn": file_sn})
        if request_pacer:
            request_pacer.wait()
        guide_raw, guide_headers, new_cookies = _fetch_official(f"https://{HOST}{DOWNLOAD_ROUTE}?{query}", purpose="reference_guide", max_bytes=MAX_GUIDE_BYTES, cookies=cookies)
        cookies.update(new_cookies)
        guide_at = guide_headers.get("date") or _utc_now()
        source_retrieved_at["reference_guide"] = guide_at
        source_media_types["reference_guide"] = guide_headers.get("content-type", "")
        _write_captured_document(capture_root, guide_path, guide_raw)
    try:
        evidence = parse_evidence(operation, page_raw=page_raw, detail_raw=detail_raw, guide_raw=guide_raw, page_retrieved_at=page_at, detail_retrieved_at=detail_at, guide_retrieved_at=guide_at, source_media_types=source_media_types)
    except EvidenceError as exc:
        status, _ = _capture_document_status(str(exc))
        _write_capture_receipt(folder, identity, status=status, reason_code=str(exc), content_types=source_media_types, retrieved_at=source_retrieved_at)
        raise
    evidence_raw = _json_bytes(evidence)
    write_capture(folder / "evidence.json", evidence_raw)
    _write_capture_receipt(folder, identity, status="acquired", reason_code=None, content_types=source_media_types, retrieved_at=source_retrieved_at)
    return {"operation_id": identity["operation_id"], "dataset_id": dataset_id, "upstream_operation_key": key, "status": evidence["parse_status"], "evidence_sha256": _sha256(evidence_raw), "evidence_bytes": len(evidence_raw)}


def reconcile(
    queue: list[dict[str, Any]],
    evidence_dir: pathlib.Path,
    capture_root: pathlib.Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    statuses = ("pending", "retryable", "acquired", "parsed_with_unknowns", "parsed_complete", "missing", "ambiguous", "unsupported", "invalid")
    counts = {status: 0 for status in statuses}
    by_operation = {item["operation_identity"]["operation_id"]: item for item in queue}
    by_source_identity = {
        (item["operation_identity"]["dataset_id"], item["operation_identity"]["upstream_operation_key"]): item["operation_identity"]["operation_id"]
        for item in queue
    }
    evidence_by_id: dict[str, tuple[dict[str, Any], pathlib.Path, bytes]] = {}
    invalid_ids: set[str] = set()
    unmatched_invalid = 0
    for path in sorted(evidence_dir.glob("*.json")):
        if path.name.startswith("reconciliation."):
            continue
        path_match = re.fullmatch(r"([0-9]+)-([A-Za-z0-9:._-]+)\.json", path.name)
        matched_id = by_source_identity.get((path_match.group(1), path_match.group(2))) if path_match else None
        try:
            value = _strict_read_json(path)
            _validate_evidence(value)
            operation_id = value["identity"]["operation_id"]
            if operation_id not in by_operation or operation_id in evidence_by_id or operation_id in invalid_ids:
                if operation_id in by_operation:
                    invalid_ids.add(operation_id)
                    evidence_by_id.pop(operation_id, None)
                else:
                    unmatched_invalid += 1
                continue
            evidence_by_id[operation_id] = (value, path, path.read_bytes())
        except (EvidenceError, KeyError, TypeError, OSError):
            if matched_id:
                invalid_ids.add(matched_id)
            else:
                unmatched_invalid += 1
    work_items: list[dict[str, Any]] = []
    for original in queue:
        item = dict(original)
        identity = item["operation_identity"]
        operation_id = identity["operation_id"]
        evidence = evidence_by_id.get(operation_id)
        if operation_id in invalid_ids:
            item.update(status="invalid", next_action="repair_invalid_evidence_binding", reason_code="evidence_invalid_or_duplicate")
        elif evidence:
            value, path, raw = evidence
            status = value["parse_status"]
            item.update(status=status, next_action=("compile_request_contract" if status == "parsed_complete" else "compile_request_contract_and_resolve_unknowns"))
            try:
                relative = path.resolve().relative_to(ROOT.resolve()).as_posix()
            except ValueError:
                relative = ""
            if relative.startswith("reports/operation-document-evidence/"):
                item["evidence_ref"] = {"path": relative, "sha256": _sha256(raw), "bytes": len(raw)}
        else:
            capture = _capture_work_status(identity, capture_root) if capture_root else None
            if capture:
                item.update(capture)
            elif item["source_profile_id"] == "safetydata_v1":
                item.update(status="pending", next_action="implement_safetydata_document_profile")
            else:
                item.update(status="pending", next_action="acquire_official_catalogue_detail_and_guide")
        status = item["status"]
        if status not in counts:
            item.update(status="invalid", next_action="repair_invalid_evidence_binding", reason_code="work_item_status_invalid")
            status = "invalid"
        counts[status] += 1
        work_items.append(item)
    report = {
        "schema_version": "datapan.operation-document-reconciliation.v2",
        "manifest_binding": {"path": "reports/data-go-kr/operation-manifest.json", "sha256": _sha256(MANIFEST.read_bytes()) if MANIFEST.exists() else None, "source_snapshot_sha256": "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0"},
        "summary": {"registered_api_operations": len(queue), "statuses": counts, "invalid_artifacts_not_bound_to_registered_identity": unmatched_invalid, "coverage_complete": counts["parsed_complete"] == len(queue) and unmatched_invalid == 0},
        "scope": {"provider": "data.go.kr", "inventory": "registered_rest_and_soap_operations_only", "registered_protocols": {"REST": 12627, "SOAP": 35}, "link_operations_excluded": 8871, "operationless_catalog_entries_excluded": 473, "worldwide_provider_apis_included": False},
        "source_policy": {
            "official_document_hosts": [HOST, "www.safetydata.go.kr"],
            "provider_operation_calls": 0,
            "external_openapi_refs_followed": False,
            "soap_wsdl_imports_followed": False,
            "raw_capture_directory_private": True,
            "transport_bounds": {
                "max_document_requests_per_operation": 3,
                "request_deadline_seconds": REQUEST_TIMEOUT_SECONDS,
                "max_concurrent_requests_per_host": 1,
                "max_batch_operations": MAX_BATCH_OPERATIONS,
                "minimum_interval_between_document_requests_ms": int(MIN_DOCUMENT_REQUEST_INTERVAL_SECONDS * 1000),
                "max_private_capture_root_bytes": MAX_PRIVATE_CAPTURE_ROOT_BYTES,
                "max_schema_variants_per_response": MAX_SCHEMA_VARIANTS_PER_RESPONSE,
                "max_response_bytes": {"catalogue": MAX_PAGE_BYTES, "operation_detail": MAX_DETAIL_BYTES, "reference_guide": MAX_GUIDE_BYTES},
                "redirects_followed": False,
                "compressed_responses_accepted": False,
            },
        },
    }
    return report, work_items


def _capture_work_status(identity: dict[str, Any], capture_root: pathlib.Path | None) -> dict[str, Any] | None:
    if capture_root is None:
        return None
    folder = capture_root / identity["dataset_id"] / identity["upstream_operation_key"]
    receipt_path = folder / "capture-receipt.json"
    if receipt_path.is_file():
        try:
            receipt = _strict_read_json(receipt_path)
        except EvidenceError:
            return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_invalid"}
        version = receipt.get("schema_version")
        if version not in {"datapan.operation-document-capture-receipt.v1", "datapan.operation-document-capture-receipt.v2"} or receipt.get("operation_id") != identity["operation_id"]:
            return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_identity_mismatch"}
        status = receipt.get("status")
        reason_code = receipt.get("reason_code")
        allowed = {"acquired", "missing", "ambiguous", "unsupported"} | ({"retryable"} if version.endswith(".v2") else set())
        if status not in allowed or (reason_code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason_code)):
            return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_status_invalid"}
        expected_files = {"catalogue": "catalogue.html", "operation_detail": "operation-detail.html", "reference_guide": "reference-guide.bin"}
        raw_documents = receipt.get("documents")
        if not isinstance(raw_documents, list) or len(raw_documents) != len(expected_files):
            return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_documents_invalid"}
        seen_roles: set[str] = set()
        all_present = True
        for document in raw_documents:
            if not isinstance(document, dict):
                return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_document_invalid"}
            role = document.get("role")
            filename = expected_files.get(role)
            if filename is None or role in seen_roles or not isinstance(document.get("present"), bool):
                return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_document_invalid"}
            if document.get("present") and (not isinstance(document.get("media_type"), str) or not isinstance(document.get("encoding"), str)):
                return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_document_media_invalid"}
            if not document.get("present") and (document.get("media_type") is not None or document.get("encoding") is not None):
                return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_document_absence_invalid"}
            seen_roles.add(role)
            all_present = all_present and document["present"]
            raw_path = folder / filename
            if document["present"]:
                if not raw_path.is_file() or isinstance(document.get("bytes"), bool) or document.get("bytes") != raw_path.stat().st_size or not re.fullmatch(r"[a-f0-9]{64}", str(document.get("sha256", ""))) or _sha256(raw_path.read_bytes()) != document["sha256"]:
                    return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_document_binding_invalid"}
            elif raw_path.exists() or document.get("bytes") != 0 or document.get("sha256") is not None:
                return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_document_absence_invalid"}
        any_present = any(document["present"] for document in raw_documents)
        if status == "acquired" and (not any_present or (not all_present and receipt.get("evidence_sha256") is None)):
            return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_acquired_incomplete"}
        evidence_sha256 = receipt.get("evidence_sha256")
        evidence_path = folder / "evidence.json"
        if evidence_sha256 is not None and (not re.fullmatch(r"[a-f0-9]{64}", str(evidence_sha256)) or not evidence_path.is_file() or _sha256(evidence_path.read_bytes()) != evidence_sha256):
            return {"status": "invalid", "next_action": "repair_invalid_evidence_binding", "reason_code": "capture_receipt_evidence_binding_invalid"}
        next_action = {
            "acquired": "run_offline_document_parser",
            "missing": "retry_or_locate_official_document",
            "retryable": "retry_official_document_capture",
            "ambiguous": "resolve_operation_document_match",
            "unsupported": "implement_legacy_ole_or_hwp_guide_adapter" if reason_code == "guide_format_unsupported_legacy_ole_document" else ("implement_safetydata_document_profile" if reason_code == "safetydata_parser_not_yet_available" else "implement_or_register_document_adapter"),
        }[status]
        # Old v1 receipts used `missing` for every acquisition error. Do not
        # treat those ambiguous historical failures as verified document loss.
        if version.endswith(".v1") and status == "missing":
            status, next_action = "retryable", "retry_official_document_capture"
        result: dict[str, Any] = {"status": status, "next_action": next_action}
        if reason_code:
            result["reason_code"] = reason_code
        return result
    filenames = ("catalogue.html", "operation-detail.html", "reference-guide.bin")
    present = [ (folder / name).is_file() for name in filenames ]
    if all(present):
        return {"status": "acquired", "next_action": "run_offline_document_parser"}
    if any(present):
        return {"status": "retryable", "next_action": "retry_official_document_capture", "reason_code": "capture_incomplete"}
    return None


def _capture_content_types(folder: pathlib.Path) -> dict[str, str]:
    receipt_path = folder / "capture-receipt.json"
    if not receipt_path.is_file():
        return {}
    try:
        receipt = _strict_read_json(receipt_path)
    except EvidenceError:
        return {}
    if receipt.get("schema_version") not in {"datapan.operation-document-capture-receipt.v1", "datapan.operation-document-capture-receipt.v2"}:
        return {}
    result: dict[str, str] = {}
    for document in receipt.get("documents", []):
        if not isinstance(document, dict) or not document.get("present"):
            continue
        role = document.get("role")
        value = document.get("media_type")
        if role in {"catalogue", "operation_detail", "reference_guide"} and isinstance(value, str):
            normalized = _normalize_content_type(value, None)
            if normalized:
                result[role] = normalized
    return result


def _capture_retrieval_times(folder: pathlib.Path) -> dict[str, str]:
    """Recover source HTTP Date values when resuming a private capture.

    Prefer the public receipt's timestamp only when it is bound to the current
    raw bytes. Older receipts do not contain timestamps, so fall back to the
    source bindings in evidence.json, which also bind each timestamp to a
    digest. File mtimes are a last-resort parser input and are never written as
    source acquisition timestamps into a receipt.
    """
    role_files = {
        "catalogue": ("catalogue.html", "catalogue_detail_page"),
        "operation_detail": ("operation-detail.html", "operation_detail_html"),
        "reference_guide": ("reference-guide.bin", "reference_guide_docx"),
    }
    result: dict[str, str] = {}

    def valid_timestamp(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False

    receipt_path = folder / "capture-receipt.json"
    try:
        receipt = _strict_read_json(receipt_path) if receipt_path.is_file() else {}
    except EvidenceError:
        receipt = {}
    if receipt.get("schema_version") in {"datapan.operation-document-capture-receipt.v1", "datapan.operation-document-capture-receipt.v2"}:
        for document in receipt.get("documents", []):
            if not isinstance(document, dict) or not document.get("present"):
                continue
            role = document.get("role")
            entry = role_files.get(role)
            if entry is None or not valid_timestamp(document.get("retrieved_at")):
                continue
            raw_path = folder / entry[0]
            if raw_path.is_file() and _sha256(raw_path.read_bytes()) == document.get("sha256"):
                result[role] = document["retrieved_at"]

    evidence_path = folder / "evidence.json"
    try:
        evidence = _strict_read_json(evidence_path) if evidence_path.is_file() else {}
    except EvidenceError:
        evidence = {}
    bindings = evidence.get("source_bindings", []) if isinstance(evidence, dict) else []
    source_to_role = {source_id: role for role, (_, source_id) in role_files.items()}
    for binding in bindings if isinstance(bindings, list) else []:
        if not isinstance(binding, dict) or not valid_timestamp(binding.get("retrieved_at")):
            continue
        role = source_to_role.get(binding.get("source_id"))
        entry = role_files.get(role)
        if role is None or entry is None or role in result:
            continue
        raw_path = folder / entry[0]
        if raw_path.is_file() and _sha256(raw_path.read_bytes()) == binding.get("sha256"):
            result[role] = binding["retrieved_at"]
    return result


def _capture_file_time(path: pathlib.Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_existing_captures(manifest: dict[str, Any], capture_root: pathlib.Path, output_dir: pathlib.Path, wanted_ids: set[str] | None = None) -> int:
    by_id = {item["operation_identity"]["operation_id"]: item for item in build_queue(manifest)}
    count = 0
    for operation in manifest["operations"]:
        identity = _safe_operation_identity(operation)
        if wanted_ids is not None and identity["operation_id"] not in wanted_ids:
            continue
        folder = capture_root / identity["dataset_id"] / identity["upstream_operation_key"]
        paths = [folder / "catalogue.html", folder / "operation-detail.html", folder / "reference-guide.bin"]
        if not paths[0].is_file():
            continue
        page_raw = paths[0].read_bytes()
        has_detail_and_guide = paths[1].is_file() and paths[2].is_file()
        detail_raw = paths[1].read_bytes() if has_detail_and_guide else b""
        guide_raw = paths[2].read_bytes() if has_detail_and_guide else b""
        source_retrieved_at = _capture_retrieval_times(folder)
        get_at = lambda role, p: source_retrieved_at.get(role) or _capture_file_time(p)
        content_types = _capture_content_types(folder)
        try:
            if has_detail_and_guide:
                value = parse_evidence(operation, page_raw=page_raw, detail_raw=detail_raw, guide_raw=guide_raw, page_retrieved_at=get_at("catalogue", paths[0]), detail_retrieved_at=get_at("operation_detail", paths[1]), guide_retrieved_at=get_at("reference_guide", paths[2]), source_media_types=content_types)
            else:
                value = parse_openapi_evidence(
                    operation,
                    page_raw=page_raw,
                    page_retrieved_at=get_at("catalogue", paths[0]),
                    page_media_type=content_types.get("catalogue"),
                )
        except EvidenceError as exc:
            status, _ = _capture_document_status(str(exc))
            _write_capture_receipt(folder, identity, status=status, reason_code=str(exc), content_types=content_types, retrieved_at=source_retrieved_at)
            continue
        destination = output_dir / f"{identity['dataset_id']}-{identity['upstream_operation_key']}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        evidence_raw = _json_bytes(value)
        destination.write_bytes(evidence_raw)
        write_capture(folder / "evidence.json", evidence_raw)
        _write_capture_receipt(folder, identity, status="acquired", reason_code=None, content_types=content_types, retrieved_at=source_retrieved_at)
        receipt_path = folder / "capture-receipt.json"
        public_receipt_dir = output_dir / "receipts"
        public_receipt_dir.mkdir(parents=True, exist_ok=True)
        write_capture(public_receipt_dir / f"{identity['dataset_id']}-{identity['upstream_operation_key']}.json", receipt_path.read_bytes())
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
    parser.add_argument("--offset", type=int, default=0, help="Stable zero-based work-queue offset for bounded resumable batches")
    parser.add_argument("--operation-id", action="append", default=[], help="Select an exact registered operation identity; repeat up to the bounded batch limit")
    parser.add_argument("--parse-captures", action="store_true")
    parser.add_argument("--evidence-dir", type=pathlib.Path, default=EVIDENCE_V2_DIR)
    parser.add_argument("--queue", type=pathlib.Path, default=QUEUE)
    parser.add_argument("--reconciliation", type=pathlib.Path, default=RECONCILIATION)
    args = parser.parse_args()
    try:
        manifest = _strict_read_json(args.manifest)
        queue = build_queue(manifest)
        if args.capture:
            if args.capture_root is None:
                raise EvidenceError("capture_bounds_or_private_root_missing")
            if args.operation_id:
                if not 1 <= args.limit <= MAX_BATCH_OPERATIONS or len(args.operation_id) > args.limit or len(set(args.operation_id)) != len(args.operation_id):
                    raise EvidenceError("operation_id_selection_invalid")
                by_id = {item["operation_identity"]["operation_id"]: item for item in queue}
                if any(operation_id not in by_id for operation_id in args.operation_id):
                    raise EvidenceError("operation_id_not_in_registered_inventory")
                selected = [by_id[operation_id] for operation_id in args.operation_id]
            else:
                selected = select_queue_batch(queue, offset=args.offset, limit=args.limit)
            args.capture_root = prepare_private_capture_root(args.capture_root)
            operations = {op["operation_id"]: op for op in manifest["operations"]}
            receipts = []
            request_pacer = _OfficialDocumentRequestPacer()
            for item in selected:
                try:
                    receipts.append(_capture_one(operations[item["operation_identity"]["operation_id"]], args.capture_root, request_pacer))
                except EvidenceError as exc:
                    operation = operations[item["operation_identity"]["operation_id"]]
                    identity = _safe_operation_identity(operation)
                    folder = args.capture_root / identity["dataset_id"] / identity["upstream_operation_key"]
                    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                    try:
                        folder.chmod(0o700)
                    except OSError:
                        pass
                    status, _ = _capture_document_status(str(exc))
                    _write_capture_receipt(folder, identity, status=status, reason_code=str(exc))
                    receipts.append({"operation_id": item["operation_identity"]["operation_id"], "status": status, "reason_code": str(exc)})
            print(json.dumps({"attempted": len(receipts), "statuses": {s: sum(1 for r in receipts if r["status"] == s) for s in sorted({r["status"] for r in receipts})}, "private_capture_root": True}, sort_keys=True))
        if args.parse_captures:
            if args.capture_root is None:
                raise EvidenceError("capture_root_required")
            count = _parse_existing_captures(manifest, args.capture_root, args.evidence_dir)
            print(f"parsed evidence artifacts: {count}")
        if args.build_queue or args.reconcile:
            value, work_items = reconcile(queue, args.evidence_dir, args.capture_root)
            args.queue.parent.mkdir(parents=True, exist_ok=True)
            args.queue.write_bytes(b"".join(_json_line(item) for item in work_items))
        if args.reconcile:
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
