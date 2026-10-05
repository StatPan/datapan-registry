#!/usr/bin/env python3
"""Pinned local declaration for Seoul's OA-109 last-train operation.

This module validates only the reviewed portal/UDDI/service join and emits its
source-backed Registry operation.  It never contacts the provider or includes
sample values, credentials, or response-shape assumptions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit


ROOT = pathlib.Path(__file__).resolve().parents[1]
DECLARATION_PATH = ROOT / "contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json"
DECLARATION_SHA256 = "224c1f2cb7fd39b1f72040957042b4d114b4540dc17765abfcadce4cfd7b8f5c"
DECLARATION_ID = "data-go-kr:OA-109:SearchLastTrainTimeByIDService:v1"
PROVENANCE_METHOD = "data_go_kr_seoul_target_navigation_declaration_v1"
OPERATION_KEY = "OA-109:SearchLastTrainTimeByIDService"
HISTORY_ENDPOINTS = (
    "http://data.seoul.go.kr/dataList/datasetView.do?infId=OA-109&srvType=A&serviceKind=1",
    "http://data.seoul.go.kr/dataList/datasetView.do?infId=OA-101&srvType=A&serviceKind=1",
    "http://data.seoul.go.kr/dataList/datasetView.do?infId=OA-108&srvType=A&serviceKind=1",
)
PAGE_URL = "https://www.data.go.kr/data/15056854/openapi.do"
RESOLVER_URL = "https://www.data.go.kr/tcs/dss/selectApiLinkUrl.do?publicDataPk=15056854"


class DeclarationError(ValueError):
    """A fixed, safe rejection of the pinned declaration contract."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def source_fingerprint(row: Mapping[str, Any]) -> str:
    source, raw = _source(row)
    source_raw = dict(raw)
    source_raw.pop("request_cnt", None)
    view = {
        "provider": row.get("provider"),
        "id": str(row.get("id") or ""),
        "source_system": source.get("system"),
        "source_url": source.get("url"),
        "source_raw": source_raw,
    }
    return digest_json(view)


def guide_fingerprint(row: Mapping[str, Any]) -> str | None:
    _source_value, raw = _source(row)
    value = raw.get("guide_url")
    if not isinstance(value, str) or not value.strip():
        return None
    return sha256_bytes(value.encode("utf-8"))


def _read_declaration() -> dict[str, Any]:
    try:
        raw = DECLARATION_PATH.read_bytes()
    except OSError as exc:
        raise DeclarationError("pinned declaration is unavailable") from exc
    if sha256_bytes(raw) != DECLARATION_SHA256:
        raise DeclarationError("pinned declaration digest mismatch")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeclarationError("pinned declaration is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "datapan.provider-operation-declaration.v1"
        or value.get("declaration_id") != DECLARATION_ID
    ):
        raise DeclarationError("pinned declaration identity mismatch")
    return value


DECLARATION = _read_declaration()


def declaration_sha256() -> str:
    return DECLARATION_SHA256


def native_document_sha256s() -> dict[str, str]:
    return {
        name: str(value["sha256"])
        for name, value in DECLARATION["native_documents"].items()
    }


def _source(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    source = row.get("source")
    raw = source.get("raw") if isinstance(source, Mapping) else None
    return (source if isinstance(source, Mapping) else {}, raw if isinstance(raw, Mapping) else {})


def validate_subject_row(row: Mapping[str, Any]) -> None:
    """Require the exact current data.go.kr record and OA-109 metadata target."""
    subject = DECLARATION["subject"]
    source, raw = _source(row)
    if (
        row.get("provider") != subject["provider"]
        or str(row.get("id")) != subject["portal_dataset_id"]
        or source.get("system") != subject["source_system"]
        or source.get("url") != subject["source_url"]
        or raw.get("api_type") != subject["source_api_type"]
        or raw.get("id") != subject["source_uddi"]
        or raw.get("list_id") != subject["portal_dataset_id"]
        or raw.get("meta_url") != subject["source_meta_url"]
        or raw.get("guide_url") != subject["source_guide_url"]
    ):
        raise DeclarationError("portal_source_subject_mismatch")
    operations = row.get("operations")
    if not isinstance(operations, list) or len(operations) != len(HISTORY_ENDPOINTS):
        raise DeclarationError("portal_historical_operation_set_mismatch")
    endpoints: list[str] = []
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise DeclarationError("portal_historical_operation_invalid")
        operation_source, operation_raw = _source(operation)
        if operation_source.get("system") != "data.go.kr" or operation_raw.get("api_type") != "LINK":
            raise DeclarationError("portal_historical_operation_invalid")
        endpoint = operation.get("endpoint")
        if not isinstance(endpoint, str) or operation_raw.get("operation_url") != endpoint:
            raise DeclarationError("portal_historical_operation_endpoint_mismatch")
        endpoints.append(endpoint)
    if tuple(endpoints) != HISTORY_ENDPOINTS:
        raise DeclarationError("portal_historical_operation_order_or_target_mismatch")


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise DeclarationError("declaration_observation_time_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeclarationError("declaration_observation_time_invalid") from exc
    if parsed.tzinfo is None:
        raise DeclarationError("declaration_observation_time_unzoned")
    return parsed.astimezone(timezone.utc)


def _safe_exact_resolver_context(row: Mapping[str, Any], link_metadata: Mapping[str, Any]) -> None:
    validate_subject_row(row)
    subject = DECLARATION["subject"]
    page = link_metadata.get("page")
    resolver = link_metadata.get("resolver")
    if not isinstance(page, Mapping) or not isinstance(resolver, Mapping):
        raise DeclarationError("declaration_page_resolver_provenance_missing")
    if (
        link_metadata.get("method") != "data_go_kr_select_api_link_url_v1"
        or link_metadata.get("dataset_id") != subject["portal_dataset_id"]
        or link_metadata.get("public_data_pk") != subject["portal_dataset_id"]
        or link_metadata.get("public_data_detail_pk") != subject["source_uddi"]
        or page.get("url") != PAGE_URL
        or page.get("effective_url") != PAGE_URL
        or resolver.get("request_url") != RESOLVER_URL
        or resolver.get("effective_url") != RESOLVER_URL
        or resolver.get("public_data_detail_pk") != subject["source_uddi"]
        or resolver.get("resolved_url") != subject["source_metadata_target"]
        or resolver.get("resolved_url_sha256") != sha256_bytes(subject["source_metadata_target"].encode("utf-8"))
    ):
        raise DeclarationError("declaration_page_resolver_identity_mismatch")
    for row_value, digest_key in ((page, "sha256"), (resolver, "sha256")):
        if not re.fullmatch(r"[a-f0-9]{64}", str(row_value.get(digest_key) or "")):
            raise DeclarationError("declaration_observation_digest_invalid")
        _timestamp(row_value.get("observed_at"))
        if isinstance(row_value.get("bytes"), bool) or not isinstance(row_value.get("bytes"), int) or row_value["bytes"] <= 0:
            raise DeclarationError("declaration_observation_size_invalid")
    page_time = _timestamp(page.get("observed_at"))
    resolver_time = _timestamp(resolver.get("observed_at"))
    if resolver_time < page_time:
        raise DeclarationError("declaration_observation_order_invalid")


def request_parameters() -> list[dict[str, Any]]:
    """Project only the exact named request facts; no sample values/defaults."""
    out: list[dict[str, Any]] = []
    enums = DECLARATION["enums"]
    for item in DECLARATION["request_parameters"]:
        value: dict[str, Any] = {
            "name": item["name"],
            "label": item["label"],
            "type": item["type"],
            "required": item["required"],
            "location": item["location"],
        }
        if item["name"] == "TYPE":
            value["enum_values"] = list(DECLARATION["transport"]["format_selectors"])
        elif item["name"] in enums:
            value["enum_values"] = list(enums[item["name"]])
            value["enum_labels"] = copy.deepcopy(enums[item["name"]])
        if item["name"] == "KEY":
            value["auth"] = True
        out.append(value)
    return out


def response_parameters() -> list[dict[str, str]]:
    # The reviewed documents declare names and labels only.  Do not add guessed
    # response types, requiredness, nullability, envelope, or media types.
    return [
        {"name": item["name"], "label": item["label"]}
        for item in DECLARATION["response_parameters"]
    ]


def build_operation(row: Mapping[str, Any], observed_guide_url: str | None) -> dict[str, Any]:
    validate_subject_row(row)
    subject = DECLARATION["subject"]
    raw: dict[str, Any] = {
        "api_type": "REST",
        "operation_declaration_id": DECLARATION_ID,
        "operation_declaration_key": OPERATION_KEY,
        "operation_declaration_sha256": DECLARATION_SHA256,
        "provider_dataset_id": subject["provider_dataset_id"],
        "service_name": subject["service_name"],
        "operation_url": DECLARATION["transport"]["endpoint_template"],
        "method_evidence": DECLARATION["transport"]["method_evidence"],
        "method_evidence_classification": DECLARATION["transport"]["method_evidence_classification"],
        "native_document_sha256s": native_document_sha256s(),
    }
    if observed_guide_url:
        raw["guide_url"] = observed_guide_url
    return {
        "name": subject["service_name"],
        "endpoint": DECLARATION["transport"]["endpoint_template"],
        "http_method": DECLARATION["transport"]["method"],
        "method_evidence": DECLARATION["transport"]["method_evidence"],
        "request_params": request_parameters(),
        "response_params": response_parameters(),
        "source": {
            "system": "data.go.kr",
            "url": PAGE_URL,
            "raw": raw,
        },
    }


def build_provenance(
    row: Mapping[str, Any],
    *,
    source_sha256: str,
    guide_sha256: str | None,
    observed_guide_url: str | None,
    observed_guide_url_sha256: str | None,
    link_metadata: Mapping[str, Any],
    operation: Mapping[str, Any],
) -> dict[str, Any]:
    _safe_exact_resolver_context(row, link_metadata)
    if not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
        raise DeclarationError("declaration_source_fingerprint_invalid")
    if guide_sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}", guide_sha256):
        raise DeclarationError("declaration_guide_fingerprint_invalid")
    expected_observed_digest = sha256_bytes(observed_guide_url.encode("utf-8")) if observed_guide_url else None
    if observed_guide_url_sha256 != expected_observed_digest:
        raise DeclarationError("declaration_observed_guide_binding_invalid")
    return {
        "method": PROVENANCE_METHOD,
        "declaration_id": DECLARATION_ID,
        "declaration_sha256": DECLARATION_SHA256,
        "subject": copy.deepcopy(DECLARATION["subject"]),
        "native_document_sha256s": native_document_sha256s(),
        "method_derivation": {
            "id": DECLARATION["transport"]["method_evidence"],
            "classification": DECLARATION["transport"]["method_evidence_classification"],
            "execution_verified": False,
            "exclusive_method_claim": False,
        },
        "source_binding": {
            "source_sha256": source_sha256,
            "guide_sha256": guide_sha256,
            "observed_guide_url": observed_guide_url,
            "observed_guide_url_sha256": observed_guide_url_sha256,
        },
        "page_resolver": copy.deepcopy(dict(link_metadata)),
        "operation_sha256": digest_json(dict(operation)),
    }


def validate_enriched_record(row: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    """Validate exact declaration, live metadata chain, and additive operation."""
    provenance = record.get("declaration_provenance")
    operations = record.get("operations")
    if not isinstance(provenance, Mapping) or not isinstance(operations, list):
        raise DeclarationError("declaration_enrichment_provenance_missing")
    validate_subject_row(row)
    if record.get("source_sha256") != source_fingerprint(row) or record.get("guide_sha256") != guide_fingerprint(row):
        raise DeclarationError("declaration_source_binding_invalid")
    expected_count = len(row["operations"]) + 1
    if len(operations) != expected_count or operations[:len(row["operations"])] != row["operations"]:
        raise DeclarationError("declaration_historical_operations_not_preserved")
    expected_operation = build_operation(row, record.get("observed_guide_url"))
    if operations[-1] != expected_operation:
        raise DeclarationError("declaration_generated_operation_mismatch")
    context = provenance.get("page_resolver")
    if not isinstance(context, Mapping):
        raise DeclarationError("declaration_page_resolver_provenance_missing")
    _safe_exact_resolver_context(row, context)
    source_provenance = record.get("source_provenance")
    page = context.get("page")
    if (
        not isinstance(source_provenance, Mapping)
        or not isinstance(page, Mapping)
        or source_provenance.get("system") != "data.go.kr"
        or source_provenance.get("page_url") != page.get("url")
        or source_provenance.get("effective_url") != page.get("effective_url")
        or source_provenance.get("page_sha256") != page.get("sha256")
        or source_provenance.get("observed_at") != page.get("observed_at")
    ):
        raise DeclarationError("declaration_page_source_binding_mismatch")
    expected = build_provenance(
        row,
        source_sha256=str(record.get("source_sha256") or ""),
        guide_sha256=record.get("guide_sha256"),
        observed_guide_url=record.get("observed_guide_url"),
        observed_guide_url_sha256=record.get("observed_guide_url_sha256"),
        link_metadata=context,
        operation=expected_operation,
    )
    if dict(provenance) != expected:
        raise DeclarationError("declaration_enrichment_provenance_mismatch")


def validate_declared_operation(row: Mapping[str, Any], operation: Mapping[str, Any]) -> None:
    """Validate a registry operation against the pinned declaration bytes."""
    subject_row = dict(row)
    source = subject_row.get("source")
    source = dict(source) if isinstance(source, Mapping) else {}
    raw = source.get("raw")
    raw = dict(raw) if isinstance(raw, Mapping) else {}
    operation_source = operation.get("source")
    operation_raw = operation_source.get("raw") if isinstance(operation_source, Mapping) else None
    operation_guide = operation_raw.get("guide_url") if isinstance(operation_raw, Mapping) else None
    expected_source_guide = DECLARATION["subject"]["source_guide_url"]
    current_source_guide = raw.get("guide_url")
    # Applying a valid page observation can remove the source row's empty
    # guide_url or replace it with the observed one. The record-level validator
    # binds that observation to the untouched candidate digest and operation;
    # here retain only those documented forms while checking the pinned source
    # identity against its original value.
    if current_source_guide not in (expected_source_guide, operation_guide) and not (
        current_source_guide is None and expected_source_guide == "" and operation_guide is None
    ):
        raise DeclarationError("portal_source_guide_observation_mismatch")
    raw["guide_url"] = expected_source_guide
    source["raw"] = raw
    subject_row["source"] = source
    operations = row.get("operations")
    if isinstance(operations, list):
        # A composed Registry row includes the declared operation alongside
        # the three historical metadata links.  Validate the original subject
        # set separately, without allowing that declared operation to replace
        # or rewrite any historical entry.
        remaining = list(operations)
        try:
            remaining.remove(dict(operation))
        except ValueError:
            pass
        subject_row["operations"] = remaining
    source = operation.get("source")
    raw = source.get("raw") if isinstance(source, Mapping) else None
    observed_guide = raw.get("guide_url") if isinstance(raw, Mapping) else None
    expected = build_operation(subject_row, observed_guide)
    if dict(operation) != expected:
        raise DeclarationError("registry_declared_operation_mismatch")
