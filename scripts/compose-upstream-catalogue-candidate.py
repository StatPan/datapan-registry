#!/usr/bin/env python3
"""Compose a source-backed catalogue candidate without live provider calls.

The composer starts from the checked-in baseline and applies only rows whose
identity and provenance can be validated.  Missing upstream rows and unsafe
enrichment changes remain in the baseline and are listed for follow-up.  The
result is a review artifact; this command never changes the canonical registry
or authorizes publication.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import unicodedata
from typing import Any
from urllib.parse import urlsplit

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before composing catalogue candidates") from exc


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_REFRESH_POLICY = ROOT / "policy/source-refresh.json"
REGISTRY_SCHEMA = ROOT / "schemas/datapan.specs.v1.schema.json"
PROVIDER_INDEX_SCHEMA = ROOT / "schemas/datapan.provider-index.v1.schema.json"
DIFF_SCHEMA = ROOT / "schemas/datapan.catalog-diff.v1.schema.json"
REFRESH_EVIDENCE_SCHEMA = ROOT / "schemas/datapan.upstream-refresh-evidence.v1.schema.json"
RECEIPT_SCHEMA = ROOT / "schemas/datapan.catalogue-composition-receipt.v1.schema.json"
ENRICHMENT_EVIDENCE_SCHEMA = ROOT / "schemas/datapan.catalogue-enrichment-evidence.v1.schema.json"
SCHEMA_VERSION = "datapan.catalogue-composition-receipt.v1"
PROVIDER = "data.go.kr"
UNORDERED_API_ARRAYS = {"source_keywords", "search_terms"}


class CompositionError(ValueError):
    """Input or policy could not be safely composed."""


def stable_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def load_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompositionError(f"cannot read JSON {path.name}: {exc}") from exc


def file_digest(path: pathlib.Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": sha256_bytes(data)}


def normalized_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise CompositionError(f"{label} must be a string")
    result = unicodedata.normalize("NFC", value.strip())
    if not result:
        raise CompositionError(f"{label} must be non-empty")
    return result


def api_key(row: dict[str, Any]) -> tuple[str, str]:
    provider = normalized_text(row.get("provider"), "API provider").casefold()
    identifier = normalized_text(row.get("id"), "API id")
    return provider, identifier


def display_key(key: tuple[str, str]) -> dict[str, str]:
    return {"provider": key[0], "id": key[1]}


def raw_source(row: dict[str, Any]) -> dict[str, Any]:
    source = row.get("source")
    if not isinstance(source, dict):
        return {}
    raw = source.get("raw")
    return raw if isinstance(raw, dict) else {}


def operation_source(operation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    source = operation.get("source")
    if not isinstance(source, dict):
        return {}, {}
    raw = source.get("raw")
    return source, raw if isinstance(raw, dict) else {}


def valid_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in {"http", "https"} and bool(parts.hostname) and parts.port != 0
    except ValueError:
        return False


def normalized_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.port:
            host = f"{host}:{parts.port}"
        return f"{parts.scheme.lower()}://{host}{parts.path}?{parts.query}" if parts.query else f"{parts.scheme.lower()}://{host}{parts.path}"
    except ValueError as exc:
        raise CompositionError("URL has an invalid port or authority") from exc


def registered_hosts(provider_index: dict[str, Any]) -> set[str]:
    adapters = provider_index.get("adapters")
    if not isinstance(adapters, list):
        raise CompositionError("provider-index.adapters must be an array")
    hosts: set[str] = set()
    for adapter in adapters:
        if not isinstance(adapter, dict):
            raise CompositionError("provider-index adapters must contain objects")
        values = adapter.get("hosts", [])
        if not isinstance(values, list):
            raise CompositionError("provider-index adapter hosts must be arrays")
        for value in values:
            host = normalized_text(value, "adapter host").casefold()
            hosts.add(host)
    return hosts


def operation_identity(row: dict[str, Any], operation: dict[str, Any]) -> str:
    source, raw = operation_source(operation)
    system = source.get("system")
    dataset_raw = raw_source(row)
    api_type = raw.get("api_type") or dataset_raw.get("api_type")
    if system == "data.go.kr" and api_type == "LINK":
        endpoint = operation.get("endpoint")
        raw_url = raw.get("operation_url")
        if not isinstance(endpoint, str) or not valid_http_url(endpoint):
            raise CompositionError("LINK operation endpoint is missing or invalid")
        if not isinstance(raw_url, str) or not valid_http_url(raw_url):
            raise CompositionError("LINK operation source.raw.operation_url is missing or invalid")
        if normalized_url(endpoint) != normalized_url(raw_url):
            raise CompositionError("LINK endpoint does not match source.raw.operation_url")
        parsed = urlsplit(normalized_url(raw_url))
        # Scheme is part of the operation contract, but not its identity. This
        # lets HTTP-to-HTTPS migration compare as a contract change on one
        # endpoint-backed operation rather than an unexplained delete/add pair.
        route = f"{parsed.netloc}{parsed.path}?{parsed.query}" if parsed.query else f"{parsed.netloc}{parsed.path}"
        return f"data.go.kr:LINK:{route}"
    if system == "safetydata.go.kr":
        upstream_key = raw.get("source_interface_id") or raw.get("data_sn")
        if upstream_key is None or not str(upstream_key).strip():
            raise CompositionError("safetydata operation lacks source_interface_id/data_sn")
        return f"safetydata.go.kr:{str(upstream_key).strip()}"
    if system == "data.go.kr":
        upstream_key = raw.get("operation_seq")
        if upstream_key is None or not str(upstream_key).strip():
            raise CompositionError("data.go.kr operation lacks immutable operation_seq")
        if api_type not in {"REST", "SOAP"}:
            raise CompositionError("data.go.kr operation has unsupported api_type")
        return f"data.go.kr:{api_type}:{str(upstream_key).strip()}"
    raise CompositionError("operation has no supported source-backed identity")


def index_registry(registry: Any, label: str) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    if not isinstance(registry, list):
        raise CompositionError(f"{label} registry must be an array")
    rows: list[dict[str, Any]] = []
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for position, row in enumerate(registry):
        if not isinstance(row, dict):
            raise CompositionError(f"{label}[{position}] must be an object")
        try:
            key = api_key(row)
        except CompositionError as exc:
            raise CompositionError(f"{label}[{position}] has no usable API identity: {exc}") from exc
        if key in index:
            raise CompositionError(f"duplicate API identity {key[0]}:{key[1]} in {label}")
        index[key] = row
        rows.append(row)
    return rows, index


def validate_registry_schema(registry: Any, schema: dict[str, Any], label: str) -> None:
    errors = sorted(
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).iter_errors(registry),
        key=lambda error: (list(error.path), error.message),
    )
    if errors:
        first = errors[0]
        where = "/".join(str(part) for part in first.path) or "<root>"
        raise CompositionError(f"{label} fails specs schema at {where}: {first.message}")


def source_contract_view(row: dict[str, Any]) -> dict[str, Any]:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    raw = source.get("raw") if isinstance(source.get("raw"), dict) else {}
    source_raw = copy.deepcopy(raw)
    source_raw.pop("request_cnt", None)
    return {
        "provider": row.get("provider"),
        "id": api_key(row)[1],
        "source_system": source.get("system"),
        "source_url": source.get("url"),
        "source_raw": source_raw,
    }


def source_fingerprint(row: dict[str, Any]) -> str:
    return digest_json(source_contract_view(row))


def guide_fingerprint(row: dict[str, Any]) -> str | None:
    guide = raw_source(row).get("guide_url")
    if not isinstance(guide, str) or not guide.strip():
        return None
    return sha256_bytes(guide.encode("utf-8"))


def canonical_detail_page_url(row: dict[str, Any]) -> str:
    identifier = normalized_text(row.get("id"), "API id")
    if not re.fullmatch(r"[0-9]+", identifier):
        raise CompositionError("fresh LINK detail evidence requires a numeric data.go.kr API id")
    return f"https://www.data.go.kr/data/{int(identifier)}/openapi.do"


def apply_enrichment_record(row: dict[str, Any], evidence_record: dict[str, Any]) -> dict[str, Any]:
    enriched = copy.deepcopy(row)
    enriched["operations"] = copy.deepcopy(evidence_record["operations"])
    observed_guide = evidence_record.get("observed_guide_url")
    source = enriched.get("source")
    if isinstance(source, dict) and isinstance(source.get("raw"), dict):
        if observed_guide is None:
            source["raw"].pop("guide_url", None)
        else:
            source["raw"]["guide_url"] = observed_guide
    return enriched


def enrichment_operation_validation_kwargs(row: dict[str, Any], evidence_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_page_url": canonical_detail_page_url(row),
        "observed_guide_url_sha256": evidence_record.get("observed_guide_url_sha256"),
        "observed_guide_url": evidence_record.get("observed_guide_url"),
    }


def operation_semantic_view(row: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    source, raw = operation_source(operation)
    req = operation.get("request_params") or []
    resp = operation.get("response_params") or []
    defaults = operation.get("default_params") or {}
    if not isinstance(req, list) or not isinstance(resp, list):
        raise CompositionError("operation request_params/response_params must be arrays")
    if not isinstance(defaults, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in defaults.items()):
        raise CompositionError("operation default_params must map strings to strings")
    def params(values: list[Any], label: str) -> list[Any]:
        names: set[str] = set()
        out = []
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
                raise CompositionError(f"operation {label} contains a parameter without a name")
            name = unicodedata.normalize("NFC", item["name"].strip())
            if name in names:
                raise CompositionError(f"duplicate operation parameter name {name}")
            names.add(name)
            out.append(item)
        return sorted(out, key=lambda item: (item["name"], str(item.get("label") or "")))
    return {
        "identity": operation_identity(row, operation),
        "name": operation.get("name"),
        "endpoint": operation.get("endpoint"),
        "request_params": params(req, "request_params"),
        "response_params": params(resp, "response_params"),
        "source": {
            "system": source.get("system"),
            "url": source.get("url"),
            "raw": {key: value for key, value in raw.items() if key != "request_cnt"},
        },
        "default_params": defaults,
    }


def operations_digest(operations: Any) -> str:
    if not isinstance(operations, list):
        raise CompositionError("enrichment operations must be an array")
    return digest_json(operations)


def validate_enrichment_evidence(
    evidence: Any,
    *,
    candidate_by_key: dict[tuple[str, str], dict[str, Any]],
    baseline_by_key: dict[tuple[str, str], dict[str, Any]],
    candidate_sha256: str,
    provider_index_sha256: str,
    hosts: set[str],
    registry_schema: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Validate the envelope globally and bind each row to original inputs.

    A row-level mismatch stays local so other safe candidate rows can proceed;
    malformed or duplicate evidence identities invalidate the whole envelope.
    """
    if evidence is None:
        return {}
    if not isinstance(evidence, dict) or evidence.get("schema_version") != "datapan.catalogue-enrichment-evidence.v1":
        raise CompositionError("enrichment evidence must use datapan.catalogue-enrichment-evidence.v1")
    if evidence.get("original_candidate_sha256") != candidate_sha256:
        raise CompositionError("enrichment evidence original_candidate_sha256 does not match producer candidate bytes")
    if evidence.get("provider_index_sha256") != provider_index_sha256:
        raise CompositionError("enrichment evidence provider_index_sha256 does not match provider-index bytes")
    if evidence.get("adapter_revision") != provider_index_sha256:
        raise CompositionError("enrichment evidence adapter_revision must match the provider-index digest")
    for name in ("adapter_revision", "extractor_revision"):
        value = evidence.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise CompositionError(f"enrichment evidence {name} must be a sha256 revision digest")
    records = evidence.get("records")
    if not isinstance(records, list):
        raise CompositionError("enrichment evidence records must be an array")
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get("api_key"), dict):
            raise CompositionError(f"enrichment evidence records[{index}] must include api_key")
        key = api_key(record["api_key"])
        if key in by_key:
            raise CompositionError(f"duplicate API identity {key[0]}:{key[1]} in enrichment evidence")
        if key not in candidate_by_key:
            raise CompositionError(f"enrichment evidence identity {key[0]}:{key[1]} is absent from original candidate")
        ops = record.get("operations")
        if not isinstance(ops, list) or record.get("operations_sha256") != operations_digest(ops):
            raise CompositionError(f"enrichment evidence operations digest mismatch for {key[0]}:{key[1]}")
        if record.get("status") != "enriched":
            raise CompositionError(f"enrichment evidence status is unsupported for {key[0]}:{key[1]}")
        candidate_row = candidate_by_key[key]
        if raw_source(candidate_row).get("api_type") != "LINK":
            raise CompositionError(f"enrichment evidence identity {key[0]}:{key[1]} is not a LINK API")
        binding_errors: list[str] = []
        if record.get("source_sha256") != source_fingerprint(candidate_row):
            binding_errors.append("enrichment_source_binding_mismatch")
        if record.get("guide_sha256") != guide_fingerprint(candidate_row):
            binding_errors.append("enrichment_guide_binding_mismatch")
        source_provenance = record.get("source_provenance")
        try:
            expected_detail = canonical_detail_page_url(candidate_row)
        except CompositionError:
            expected_detail = None
        observed_guide = record.get("observed_guide_url")
        observed_guide_digest = record.get("observed_guide_url_sha256")
        if observed_guide is None:
            if observed_guide_digest is not None:
                binding_errors.append("observed_guide_digest_without_url")
        elif (
            not valid_http_url(observed_guide)
            or not isinstance(observed_guide_digest, str)
            or observed_guide_digest != sha256_bytes(observed_guide.encode("utf-8"))
        ):
            binding_errors.append("observed_guide_url_digest_mismatch")
        if guide_fingerprint(candidate_row) is not None and observed_guide is None:
            binding_errors.append("candidate_guide_removal_unproven")
        if (
            not isinstance(source_provenance, dict)
            or source_provenance.get("system") != "data.go.kr"
            or expected_detail is None
            or source_provenance.get("page_url") != expected_detail
            or source_provenance.get("effective_url") != expected_detail
            or not isinstance(source_provenance.get("page_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", source_provenance.get("page_sha256", ""))
            or not isinstance(source_provenance.get("observed_at"), str)
        ):
            binding_errors.append("enrichment_detail_source_binding_mismatch")
        else:
            try:
                jsonschema.FormatChecker().check(source_provenance.get("observed_at"), "date-time")
            except (jsonschema.FormatError, TypeError):
                binding_errors.append("enrichment_observed_at_invalid")
        if not ops:
            binding_errors.append("enrichment_operations_empty")
        evidence_row = apply_enrichment_record(candidate_row, record)
        errors = [*api_provenance_errors(evidence_row, allow_missing_link_guide=True)]
        op_errors, _ = _validate_operation_set(
            evidence_row,
            hosts,
            source_page_url=expected_detail,
            observed_guide_url_sha256=observed_guide_digest,
            observed_guide_url=observed_guide,
        )
        errors.extend(op_errors)
        try:
            validate_registry_schema([evidence_row], registry_schema, "enrichment evidence row")
        except CompositionError:
            errors.append("enrichment_specs_schema_invalid")
        binding_errors.extend(errors)
        if binding_errors:
            record = {**record, "_binding_error": sorted(set(binding_errors))[0]}
        by_key[key] = record
    return by_key


def operation_list_view(row: dict[str, Any]) -> list[dict[str, Any]]:
    operations = row.get("operations") or []
    if not isinstance(operations, list):
        raise CompositionError("API operations must be an array")
    values = []
    identities: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            raise CompositionError("API operations must contain objects")
        item = operation_semantic_view(row, operation)
        identity = item["identity"]
        if identity in identities:
            raise CompositionError(f"duplicate/ambiguous operation identity {identity}")
        identities.add(identity)
        values.append(item)
    return sorted(values, key=lambda item: item["identity"])


def semantic_record_view(row: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(row)
    source = result.get("source")
    if isinstance(source, dict) and isinstance(source.get("raw"), dict):
        source["raw"].pop("request_cnt", None)
    for name in UNORDERED_API_ARRAYS:
        values = result.get(name)
        if isinstance(values, list) and all(isinstance(value, str) for value in values):
            result[name] = sorted(set(values))
    if isinstance(result.get("operations"), list):
        result["operations"] = operation_list_view(row)
    return result


def operation_provenance_errors(
    row: dict[str, Any],
    operation: dict[str, Any],
    hosts: set[str],
    *,
    source_page_url: str | None = None,
    observed_guide_url_sha256: str | None = None,
    observed_guide_url: str | None = None,
) -> list[str]:
    errors: list[str] = []
    source, raw = operation_source(operation)
    system = source.get("system")
    endpoint = operation.get("endpoint")
    if not isinstance(operation.get("name"), str) or not operation["name"].strip():
        errors.append("operation_name_missing")
    if not valid_http_url(endpoint):
        errors.append("operation_endpoint_invalid")
    if system not in {"data.go.kr", "safetydata.go.kr"}:
        errors.append("operation_source_system_unsupported")
    if not valid_http_url(source.get("url")):
        errors.append("operation_source_url_missing")
    if not isinstance(source.get("raw"), dict):
        errors.append("operation_source_raw_missing")
    try:
        operation_identity(row, operation)
    except CompositionError as exc:
        code = str(exc).split(":", 1)[0].strip().lower().replace(" ", "_").replace("/", "_")
        errors.append(code[:80] or "operation_identity_invalid")
    api_source = row.get("source") if isinstance(row.get("source"), dict) else {}
    api_raw = raw_source(row)
    if system == "data.go.kr":
        expected_source_url = source_page_url or api_source.get("url")
        recognized_stored_detail = False
        if source_page_url is None and source.get("url") != expected_source_url:
            try:
                recognized_stored_detail = source.get("url") == canonical_detail_page_url(row)
            except CompositionError:
                pass
        if source.get("url") != expected_source_url and not recognized_stored_detail:
            errors.append("operation_source_url_differs_from_api")
        if source_page_url is None:
            for field in ("meta_url", "guide_url"):
                if field in api_raw and raw.get(field) != api_raw.get(field):
                    errors.append(f"operation_{field}_provenance_mismatch")
        else:
            if "guide_url" in raw and raw.get("guide_url") != observed_guide_url:
                errors.append("operation_observed_guide_provenance_mismatch")
            if observed_guide_url is not None:
                if observed_guide_url_sha256 != sha256_bytes(observed_guide_url.encode("utf-8")):
                    errors.append("operation_observed_guide_digest_mismatch")
    api_type = raw_source(row).get("api_type") or raw.get("api_type")
    if valid_http_url(endpoint) and (system == "safetydata.go.kr" or (system == "data.go.kr" and api_type == "LINK")):
        host = (urlsplit(endpoint).hostname or "").casefold()
        if host not in hosts:
            errors.append("operation_host_not_registered")
    return sorted(set(errors))


def api_provenance_errors(row: dict[str, Any], *, allow_missing_link_guide: bool = False) -> list[str]:
    errors: list[str] = []
    try:
        provider, _ = api_key(row)
    except CompositionError:
        return ["api_identity_missing"]
    if provider != PROVIDER:
        errors.append("api_provider_not_data_go_kr")
    source = row.get("source")
    if not isinstance(source, dict):
        return [*errors, "api_source_missing"]
    if source.get("system") != "data.go.kr":
        errors.append("api_source_system_not_data_go_kr")
    if not valid_http_url(source.get("url")):
        errors.append("api_source_url_missing")
    raw = source.get("raw")
    if not isinstance(raw, dict):
        errors.append("api_source_raw_missing")
    elif raw.get("api_type") == "LINK":
        if not valid_http_url(raw.get("meta_url")):
            errors.append("link_meta_url_missing")
        if not allow_missing_link_guide and not valid_http_url(raw.get("guide_url")):
            errors.append("link_guide_url_missing")
    return sorted(set(errors))


def operation_counts(registry: list[dict[str, Any]]) -> dict[str, int]:
    result = {"total": 0, "data_go_kr_link": 0, "safetydata": 0, "gateway": 0, "operationless_apis": 0}
    for row in registry:
        operations = row.get("operations") or []
        if not operations:
            result["operationless_apis"] += 1
            continue
        for operation in operations:
            result["total"] += 1
            source, raw = operation_source(operation)
            if source.get("system") == "safetydata.go.kr":
                result["safetydata"] += 1
            elif raw.get("api_type") == "LINK" or raw_source(row).get("api_type") == "LINK":
                result["data_go_kr_link"] += 1
            else:
                result["gateway"] += 1
    return result


def endpoint_change_tags(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    before = {item["identity"]: item for item in operation_list_view(old)}
    after = {item["identity"]: item for item in operation_list_view(new)}
    tags: set[str] = set()
    for identity in before.keys() & after.keys():
        a, b = before[identity].get("endpoint"), after[identity].get("endpoint")
        if a == b or not isinstance(a, str) or not isinstance(b, str):
            continue
        pa, pb = urlsplit(a), urlsplit(b)
        if pa.hostname and pb.hostname and pa.hostname.casefold() == pb.hostname.casefold() and pa.path == pb.path and pa.query == pb.query and pa.scheme == "http" and pb.scheme == "https":
            tags.add("http_to_https_contract_change")
            tags.add("prior_runtime_evidence_stale")
        else:
            tags.add("endpoint_contract_change")
            tags.add("prior_runtime_evidence_stale")
    unmatched_before = [item for identity, item in before.items() if identity not in after]
    unmatched_after = [item for identity, item in after.items() if identity not in before]
    def route_key(value: Any) -> tuple[str, int | None, str, str] | None:
        if not isinstance(value, str) or not valid_http_url(value):
            return None
        parsed = urlsplit(value)
        return ((parsed.hostname or "").casefold(), parsed.port, parsed.path, parsed.query)
    before_routes: dict[tuple[str, int | None, str, str], list[dict[str, Any]]] = {}
    after_routes: dict[tuple[str, int | None, str, str], list[dict[str, Any]]] = {}
    for item in unmatched_before:
        route = route_key(item.get("endpoint"))
        if route:
            before_routes.setdefault(route, []).append(item)
    for item in unmatched_after:
        route = route_key(item.get("endpoint"))
        if route:
            after_routes.setdefault(route, []).append(item)
    for route in before_routes.keys() & after_routes.keys():
        if len(before_routes[route]) == len(after_routes[route]) == 1:
            old_url = urlsplit(before_routes[route][0]["endpoint"])
            new_url = urlsplit(after_routes[route][0]["endpoint"])
            if old_url.scheme == "http" and new_url.scheme == "https":
                tags.add("http_to_https_contract_change")
                tags.add("prior_runtime_evidence_stale")
            elif old_url.scheme != new_url.scheme:
                tags.add("endpoint_contract_change")
                tags.add("prior_runtime_evidence_stale")
    return sorted(tags)


def _record_hash(row: dict[str, Any] | None) -> str | None:
    return digest_json(row) if row is not None else None


def _enriched_candidate_hash(original: dict[str, Any] | None, evidence_record: dict[str, Any] | None) -> str | None:
    if original is None or evidence_record is None or evidence_record.get("_binding_error"):
        return None
    enriched = apply_enrichment_record(original, evidence_record)
    return _record_hash(enriched)


def _enrichment_decision_fields(evidence_record: dict[str, Any] | None) -> dict[str, Any]:
    if evidence_record is None or evidence_record.get("_binding_error"):
        return {}
    provenance = evidence_record.get("source_provenance")
    if not isinstance(provenance, dict):
        return {}
    return {
        "enrichment_page_url": provenance.get("page_url"),
        "enrichment_page_sha256": provenance.get("page_sha256"),
        "observed_guide_url_sha256": evidence_record.get("observed_guide_url_sha256"),
    }


def _queue_item(
    key: tuple[str, str],
    reason_codes: list[str],
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    baseline_sha256: str,
    candidate_sha256: str,
    required_evidence: list[str],
) -> dict[str, Any]:
    return {
        "api_key": display_key(key),
        "reason_codes": sorted(set(reason_codes)),
        "before": {"source_sha256": source_fingerprint(before) if before else None, "guide_sha256": guide_fingerprint(before) if before else None},
        "after": {"source_sha256": source_fingerprint(after) if after else None, "guide_sha256": guide_fingerprint(after) if after else None},
        "required_evidence": sorted(set(required_evidence)),
        "baseline_sha256": baseline_sha256,
        "candidate_sha256": candidate_sha256,
    }


def _candidate_diff_tags(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    tags: set[str] = set()
    braw, araw = raw_source(before), raw_source(after)
    if braw.get("request_cnt") != araw.get("request_cnt"):
        tags.add("volatile_request_cnt")
    try:
        if source_contract_view(before) != source_contract_view(after):
            tags.add("source_contract_change")
        if operation_list_view(before) != operation_list_view(after):
            tags.add("operation_contract_change")
        semantically_equal = semantic_record_view(before) == semantic_record_view(after)
    except CompositionError:
        semantically_equal = False
    if semantically_equal and canonical_json_bytes(before) != canonical_json_bytes(after):
        tags.add("ordering_only")
    elif not tags or (tags == {"volatile_request_cnt"}):
        try:
            if not semantically_equal and source_contract_view(before) == source_contract_view(after) and operation_list_view(before) == operation_list_view(after):
                tags.add("metadata_change")
        except CompositionError:
            pass
    return sorted(tags)


def _validate_operation_set(
    row: dict[str, Any],
    hosts: set[str],
    *,
    source_page_url: str | None = None,
    observed_guide_url_sha256: str | None = None,
    observed_guide_url: str | None = None,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    operations = row.get("operations") or []
    if not isinstance(operations, list):
        return ["operations_not_array"], {}
    errors: list[str] = []
    indexed: dict[str, dict[str, Any]] = {}
    for operation in operations:
        if not isinstance(operation, dict):
            errors.append("operation_not_object")
            continue
        try:
            identity = operation_identity(row, operation)
        except CompositionError as exc:
            errors.append("ambiguous_operation_identity:" + str(exc))
            continue
        if identity in indexed:
            errors.append("duplicate_operation_identity:" + identity)
            continue
        indexed[identity] = operation
        try:
            operation_semantic_view(row, operation)
        except CompositionError as exc:
            errors.append("operation_contract_invalid:" + str(exc))
        errors.extend(operation_provenance_errors(
            row,
            operation,
            hosts,
            source_page_url=source_page_url,
            observed_guide_url_sha256=observed_guide_url_sha256,
            observed_guide_url=observed_guide_url,
        ))
    return sorted(set(errors)), indexed


def compose_registries(
    baseline: Any,
    candidate: Any,
    provider_index: dict[str, Any],
    *,
    baseline_sha256: str,
    candidate_sha256: str,
    provider_index_sha256: str = "",
    enrichment_evidence: Any = None,
    registry_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, non-publishing composition result."""
    baseline_rows, baseline_by_key = index_registry(baseline, "baseline")
    candidate_rows, candidate_by_key = index_registry(candidate, "candidate")
    hosts = registered_hosts(provider_index)
    registry_schema = registry_schema or load_json(REGISTRY_SCHEMA)
    enrichment_by_key = validate_enrichment_evidence(
        enrichment_evidence,
        candidate_by_key=candidate_by_key,
        baseline_by_key=baseline_by_key,
        candidate_sha256=candidate_sha256,
        provider_index_sha256=provider_index_sha256,
        hosts=hosts,
        registry_schema=registry_schema,
    )
    composed_by_key = dict(baseline_by_key)
    decisions: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    ready_delta: dict[tuple[str, str], dict[str, Any]] = {}

    for key in sorted(baseline_by_key.keys() | candidate_by_key.keys()):
        before = baseline_by_key.get(key)
        after = candidate_by_key.get(key)
        candidate_original = after
        tags: set[str] = set()
        reason_codes: list[str] = []
        disposition = "unchanged"
        retained_ops: list[str] = []

        if before is not None and after is None:
            disposition = "retain_deletion_pending"
            tags.add("candidate_missing")
            reason_codes = ["missing_from_candidate_without_authoritative_deletion"]
            required = ["authoritative_deletion_evidence"]
            queue.append(_queue_item(key, reason_codes, before, None, baseline_sha256, candidate_sha256, required))
            decisions.append({
                "api_key": display_key(key), "disposition": disposition, "tags": sorted(tags),
                "baseline_record_sha256": _record_hash(before), "candidate_record_sha256": None,
                "retained_operation_identities": retained_ops, "findings": reason_codes,
            })
            continue

        if before is None and after is not None:
            evidence_record = enrichment_by_key.get(key)
            evidence_valid = bool(evidence_record and not evidence_record.get("_binding_error"))
            if evidence_record and raw_source(after).get("api_type") == "LINK":
                if evidence_record.get("_binding_error"):
                    reason_codes = [str(evidence_record["_binding_error"])]
                else:
                    after = apply_enrichment_record(after, evidence_record)
            validation_kwargs = enrichment_operation_validation_kwargs(after, evidence_record) if evidence_valid else {}
            errors = api_provenance_errors(after, allow_missing_link_guide=evidence_valid)
            op_errors, _ = _validate_operation_set(after, hosts, **validation_kwargs)
            if reason_codes:
                pass
            elif errors:
                reason_codes = errors
            elif raw_source(after).get("api_type") == "LINK" and not (after.get("operations") or []):
                reason_codes = ["new_link_api_missing_enrichment"]
            elif op_errors:
                reason_codes = op_errors
            if reason_codes:
                disposition = "quarantine"
                tags.add("pending_addition")
                queue.append(_queue_item(key, reason_codes, None, after, baseline_sha256, candidate_sha256, ["source_backed_operation_identity_and_provenance"]))
                quarantine.append({"api_key": display_key(key), "reason_codes": reason_codes, "record_state": "candidate_excluded"})
            else:
                disposition = "accept_new"
                tags.add("candidate_addition")
                composed_by_key[key] = after
                ready_delta[key] = after
            decisions.append({
                "api_key": display_key(key), "disposition": disposition, "tags": sorted(tags),
                "baseline_record_sha256": None, "candidate_record_sha256": _record_hash(candidate_original),
                "enriched_candidate_record_sha256": _enriched_candidate_hash(candidate_original, evidence_record),
                "enrichment_operations_sha256": evidence_record.get("operations_sha256") if evidence_record and not evidence_record.get("_binding_error") else None,
                **_enrichment_decision_fields(evidence_record),
                "composed_record_sha256": _record_hash(after) if disposition == "accept_new" else None,
                "retained_operation_identities": retained_ops, "findings": reason_codes,
            })
            continue

        assert before is not None and after is not None
        evidence_record = enrichment_by_key.get(key)
        evidence_valid = bool(evidence_record and not evidence_record.get("_binding_error"))
        if evidence_record and raw_source(after).get("api_type") == "LINK":
            if evidence_record.get("_binding_error"):
                reason_codes = [str(evidence_record["_binding_error"])]
            else:
                after = apply_enrichment_record(after, evidence_record)
                if before is not None and evidence_record.get("status") == "enriched":
                    enriched_ids = {
                        operation_identity(after, operation)
                        for operation in after["operations"]
                        if isinstance(operation, dict)
                    }
                    for old_operation in before.get("operations") or []:
                        if not isinstance(old_operation, dict):
                            continue
                        old_source, _ = operation_source(old_operation)
                        if old_source.get("system") != "data.go.kr":
                            identity = operation_identity(before, old_operation)
                            if identity not in enriched_ids:
                                after["operations"].append(copy.deepcopy(old_operation))
                                enriched_ids.add(identity)
        validation_kwargs = enrichment_operation_validation_kwargs(after, evidence_record) if evidence_valid else {}
        source_errors = api_provenance_errors(after, allow_missing_link_guide=evidence_valid)
        candidate_op_errors, candidate_ops_by_id = _validate_operation_set(after, hosts, **validation_kwargs)
        try:
            base_op_errors, base_ops_by_id = _validate_operation_set(before, hosts)
            before_contract = source_fingerprint(before)
            after_contract = source_fingerprint(after)
        except CompositionError as exc:
            base_op_errors = ["identity_or_contract_invalid:" + str(exc)]
            before_contract = ""
            after_contract = ""
            base_ops_by_id = {}
            candidate_op_errors = [*candidate_op_errors, "identity_or_contract_invalid:" + str(exc)]
            candidate_ops_by_id = {}
        try:
            tags.update(_candidate_diff_tags(before, after))
            tags.update(endpoint_change_tags(before, after))
        except CompositionError as exc:
            reason_codes = [*reason_codes, "identity_or_contract_invalid:" + str(exc)]
        candidate_link = raw_source(after).get("api_type") == "LINK"
        if {"source_contract_change", "operation_contract_change"} & tags:
            tags.add("prior_runtime_evidence_stale")
        before_has_ops = bool(before.get("operations") or [])
        after_has_ops = bool(after.get("operations") or [])
        record = after

        if reason_codes:
            pass
        elif source_errors:
            reason_codes = source_errors
        elif candidate_op_errors:
            reason_codes = candidate_op_errors
        elif base_op_errors and canonical_json_bytes(before) != canonical_json_bytes(after):
            reason_codes = ["baseline_operation_provenance_invalid"]
        elif before_has_ops and not after_has_ops:
            if before_contract != after_contract:
                reason_codes = ["source_contract_changed_while_enrichment_missing"]
            elif base_op_errors:
                reason_codes = base_op_errors
            else:
                record = copy.deepcopy(after)
                record["operations"] = copy.deepcopy(before.get("operations") or [])
                retained_ops = sorted(base_ops_by_id)
                disposition = "retain_enrichment"
                tags.add("retained_enrichment_not_refreshed")
        elif before_has_ops and after_has_ops and (base_ops_by_id.keys() - candidate_ops_by_id.keys()):
            # Producer completeness is not proof that a removed source operation
            # is authoritatively deleted. Keep the old row whole until there is
            # operation-level deletion evidence.
            reason_codes = ["operation_removed_without_authoritative_evidence"]
        elif before_has_ops and after_has_ops and candidate_link:
            if before_contract != after_contract:
                if evidence_record and not evidence_record.get("_binding_error") and evidence_record.get("status") == "enriched":
                    disposition = "accept_changed"
                else:
                    reason_codes = ["link_source_contract_changed"]
            elif base_op_errors:
                reason_codes = base_op_errors
            else:
                conflicts = [identity for identity in candidate_ops_by_id.keys() & base_ops_by_id.keys() if canonical_json_bytes(candidate_ops_by_id[identity]) != canonical_json_bytes(base_ops_by_id[identity])]
                unmatched_candidate = candidate_ops_by_id.keys() - base_ops_by_id.keys()
                if evidence_record and not evidence_record.get("_binding_error") and evidence_record.get("status") == "enriched":
                    merged = dict(candidate_ops_by_id)
                    record = copy.deepcopy(after)
                    record["operations"] = [merged[identity] for identity in sorted(merged)]
                    disposition = "accept_changed"
                elif conflicts or unmatched_candidate:
                    reason_codes = ["link_operation_changed_without_enrichment_evidence"]
                else:
                    merged = dict(base_ops_by_id)
                    merged.update(candidate_ops_by_id)
                    record = copy.deepcopy(after)
                    record["operations"] = [merged[identity] for identity in sorted(merged)]
                    retained_ops = sorted(base_ops_by_id.keys() - candidate_ops_by_id.keys())
                    disposition = "retain_enrichment" if retained_ops else ("accept_changed" if tags else "unchanged")
                    if retained_ops:
                        tags.add("retained_enrichment_not_refreshed")
        elif before_contract != after_contract:
            # A provider-backed gateway contract can be admitted as changed,
            # while runtime evidence remains stale and outside this artifact.
            if candidate_op_errors:
                reason_codes = candidate_op_errors
            else:
                disposition = "accept_changed"
        else:
            try:
                semantically_equal = semantic_record_view(before) == semantic_record_view(after)
            except CompositionError as exc:
                semantically_equal = False
                reason_codes = ["identity_or_contract_invalid:" + str(exc)]
            if reason_codes:
                pass
            elif not semantically_equal:
                disposition = "accept_changed"
            elif canonical_json_bytes(before) != canonical_json_bytes(after):
                # Volatile request counts and set-order changes are observed but
                # do not replace a canonical row or generate a review delta.
                disposition = "unchanged"
                record = before
            else:
                disposition = "unchanged"

        if reason_codes:
            disposition = "quarantine"
            tags.add("pending_regeneration_or_review")
            required = ["source_backed_current_operation_provenance"]
            if any("guide" in code for code in reason_codes) or "link_source_contract_changed" in reason_codes or "source_contract_changed_while_enrichment_missing" in reason_codes:
                required = ["current_link_detail_page", "operation_source_provenance", "registered_adapter_host"]
            if "operation_removed_without_authoritative_evidence" in reason_codes:
                required = ["authoritative_operation_deletion_evidence", "current_source_operation_inventory"]
            queue.append(_queue_item(key, reason_codes, before, after, baseline_sha256, candidate_sha256, required))
            quarantine.append({"api_key": display_key(key), "reason_codes": sorted(set(reason_codes)), "record_state": "baseline_retained"})
            record = before
            retained_ops = sorted(base_ops_by_id)
        else:
            composed_by_key[key] = record
            if canonical_json_bytes(semantic_record_view(before)) == canonical_json_bytes(semantic_record_view(record)):
                # A volatile statistic or provider array ordering must not
                # create weekly churn, even when LINK operations were restored.
                record = before
                composed_by_key[key] = before
                tags.difference_update({
                    "source_contract_change", "operation_contract_change", "endpoint_contract_change",
                    "http_to_https_contract_change", "prior_runtime_evidence_stale",
                })
                if disposition in {"accept_changed", "retain_enrichment"}:
                    disposition = "unchanged"
            if disposition in {"accept_new", "accept_changed", "retain_enrichment"} and canonical_json_bytes(record) != canonical_json_bytes(before):
                ready_delta[key] = record
            if disposition == "unchanged" and tags:
                # Volatile statistics and set ordering are useful evidence but
                # do not become API contract changes.
                disposition = "unchanged"

        decisions.append({
            "api_key": display_key(key), "disposition": disposition, "tags": sorted(tags),
            "baseline_record_sha256": _record_hash(before), "candidate_record_sha256": _record_hash(candidate_original),
            "enriched_candidate_record_sha256": _enriched_candidate_hash(candidate_original, evidence_record),
            "enrichment_operations_sha256": evidence_record.get("operations_sha256") if evidence_record and not evidence_record.get("_binding_error") else None,
            **_enrichment_decision_fields(evidence_record),
            "composed_record_sha256": _record_hash(record),
            "retained_operation_identities": retained_ops,
            "findings": sorted(set(reason_codes)),
        })

    composed_rows = [composed_by_key[api_key(row)] for row in baseline_rows if api_key(row) in composed_by_key]
    baseline_order = {api_key(row) for row in baseline_rows}
    composed_rows.extend(composed_by_key[key] for key in sorted(composed_by_key.keys() - baseline_order))
    ready_rows = [ready_delta[key] for key in sorted(ready_delta)]
    counts = {name: sum(1 for item in decisions if item["disposition"] == name) for name in sorted({item["disposition"] for item in decisions})}
    pending_ids = [item["api_key"] for item in decisions if item["disposition"] == "retain_deletion_pending"]
    quarantined_ids = [item["api_key"] for item in decisions if item["disposition"] == "quarantine"]
    applied_ids = [item["api_key"] for item in decisions if item["disposition"] not in {"retain_deletion_pending", "quarantine"}]
    partition_ids = applied_ids + pending_ids + quarantined_ids
    if len({(item["provider"], item["id"]) for item in partition_ids}) != len(partition_ids) or len(partition_ids) != len(decisions):
        raise CompositionError("API disposition identities do not form an exact disjoint partition")
    summary = {
        "api_records": {
            "baseline": len(baseline_rows), "candidate": len(candidate_rows), "composed": len(composed_rows),
            "ready_scope_delta": len(ready_rows), "baseline_candidate_union": len(decisions),
        },
        "dispositions": counts,
        "operations": {
            "baseline": operation_counts(baseline_rows), "candidate": operation_counts(candidate_rows),
            "composed": operation_counts(composed_rows), "ready_scope_delta": operation_counts(ready_rows),
        },
        "evidence": {"pending_removal": len(pending_ids), "quarantined": len(quarantined_ids), "full_scope_fresh": False},
    }
    status = "ready_scoped" if ready_rows else ("no_safe_change" if queue or quarantine else "no_change")
    return {
        "composed_registry": composed_rows,
        "ready_scope_registry": ready_rows,
        "semantic_diff": {
            "schema_version": "datapan.catalogue-semantic-diff.v1",
            "summary": summary,
            "api_decisions": decisions,
            "applied_api_keys": applied_ids,
            "retained_pending_api_keys": pending_ids,
            "quarantined_api_keys": quarantined_ids,
        },
        "regeneration_queue": {
            "schema_version": "datapan.catalogue-regeneration-queue.v1",
            "items": sorted(queue, key=lambda item: (item["api_key"]["provider"], item["api_key"]["id"])),
        },
        "quarantine": {
            "schema_version": "datapan.catalogue-quarantine.v1",
            "items": sorted(quarantine, key=lambda item: (item["api_key"]["provider"], item["api_key"]["id"])),
        },
        "status": status,
    }


def verify_refresh_inputs(
    baseline: list[Any],
    candidate: list[Any],
    diff: dict[str, Any],
    evidence: dict[str, Any],
    baseline_digest: dict[str, Any],
    candidate_digest: dict[str, Any],
    diff_digest: dict[str, Any],
    run_id: str,
    run_url: str,
) -> None:
    if not re.fullmatch(r"[0-9]{6,20}", run_id):
        raise CompositionError("producer run id must be a numeric GitHub Actions run id")
    expected_url = f"https://github.com/StatPan/datapan-registry/actions/runs/{run_id}"
    if run_url != expected_url:
        raise CompositionError("producer run URL must match the repository and run id")
    if evidence.get("schema_version") != "datapan.upstream-refresh-evidence.v1" or evidence.get("source_id") != "data_go_kr":
        raise CompositionError("refresh evidence must identify the data_go_kr producer")
    if evidence.get("status") not in {"material_change", "no_change"}:
        raise CompositionError("refresh evidence must have material_change or no_change status")
    if evidence.get("collection", {}).get("succeeded") is not True:
        raise CompositionError("refresh collection evidence is not successful")
    if evidence.get("publication", {}).get("automatic") is not False:
        raise CompositionError("refresh evidence must remain non-publishing")
    for role, actual, expected in (
        ("baseline", baseline_digest, evidence.get("baseline")),
        ("candidate", candidate_digest, evidence.get("snapshot")),
    ):
        if not isinstance(expected, dict) or (actual["bytes"], actual["sha256"]) != (expected.get("bytes"), expected.get("sha256")):
            raise CompositionError(f"{role} input bytes/hash do not match producer evidence")
    expected_diff = evidence.get("diff")
    if not isinstance(expected_diff, dict) or diff_digest["sha256"] != expected_diff.get("sha256"):
        raise CompositionError("diff input sha256 does not match producer evidence")
    if diff.get("provider") != PROVIDER:
        raise CompositionError("full catalog diff provider does not match data.go.kr")
    if diff.get("old") != evidence["baseline"].get("path") or diff.get("new") != evidence["snapshot"].get("path"):
        raise CompositionError("full diff paths do not match producer evidence")
    if diff.get("truncated") is not False or diff.get("limit") != 0:
        raise CompositionError("full catalog diff must be untruncated with limit=0")
    _, base_by_key = index_registry(baseline, "baseline")
    _, candidate_by_key = index_registry(candidate, "candidate")
    added = candidate_by_key.keys() - base_by_key.keys()
    removed = base_by_key.keys() - candidate_by_key.keys()
    common = base_by_key.keys() & candidate_by_key.keys()
    summary = diff.get("summary", {})
    expected_status = "material_change" if any(summary.get(name, 0) for name in ("added", "removed", "changed")) else "no_change"
    if evidence.get("status") != expected_status:
        raise CompositionError("refresh evidence status does not match full diff material-change summary")
    if any(len(diff.get(name, [])) != summary.get(name) for name in ("added", "removed", "changed")):
        raise CompositionError("full diff array counts do not match its summary")
    if diff.get("counts", {}).get("old") != len(base_by_key) or diff.get("counts", {}).get("new") != len(candidate_by_key):
        raise CompositionError("catalog diff record counts do not match input registries")
    if summary.get("added") != len(added) or summary.get("removed") != len(removed):
        raise CompositionError("catalog diff added/removed counts do not match input identities")
    if summary.get("changed", -1) + summary.get("stable", -1) != len(common):
        raise CompositionError("catalog diff changed/stable counts do not cover common identities")
    diff_added = {(str(item.get("provider", PROVIDER)).casefold(), str(item.get("id"))) for item in diff.get("added", []) if isinstance(item, dict)}
    diff_removed = {(str(item.get("provider", PROVIDER)).casefold(), str(item.get("id"))) for item in diff.get("removed", []) if isinstance(item, dict)}
    diff_changed = {(str(item.get("provider", PROVIDER)).casefold(), str(item.get("id"))) for item in diff.get("changed", []) if isinstance(item, dict)}
    if diff_added != added or diff_removed != removed:
        raise CompositionError("full catalog diff identity sets do not match registry inputs")
    if len(diff_changed) != summary.get("changed") or not diff_changed.issubset(common):
        raise CompositionError("full catalog changed identities do not match common input identities")
    if evidence.get("diff", {}).get("summary") != summary:
        raise CompositionError("producer evidence diff summary does not match full diff")


def build_bundle(
    result: dict[str, Any],
    *,
    input_digests: dict[str, Any],
    run_id: str,
    run_url: str,
) -> dict[str, bytes]:
    names = {
        "composed-candidate.registry.json": result["composed_registry"],
        "ready-scope.registry.json": result["ready_scope_registry"],
        "semantic-diff.json": result["semantic_diff"],
        "regeneration-queue.json": result["regeneration_queue"],
        "quarantine.json": result["quarantine"],
    }
    payloads = {name: stable_json_bytes(value) for name, value in names.items()}
    output_digests = {name: {"bytes": len(data), "sha256": sha256_bytes(data)} for name, data in sorted(payloads.items())}
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "producer": {"repository": "StatPan/datapan-registry", "run_id": run_id, "run_url": run_url},
        "input_digests": input_digests,
        "status": result["status"],
        "scope": {
            "full_scope_fresh": False,
            "publication_allowed": False,
            "global_counts": result["semantic_diff"]["summary"],
            "applied_api_keys": result["semantic_diff"]["applied_api_keys"],
            "retained_pending_api_keys": result["semantic_diff"]["retained_pending_api_keys"],
            "quarantined_api_keys": result["semantic_diff"]["quarantined_api_keys"],
        },
        "outputs": output_digests,
    }
    jsonschema.Draft202012Validator(load_json(RECEIPT_SCHEMA), format_checker=jsonschema.FormatChecker()).validate(receipt)
    payloads["composition-receipt.json"] = stable_json_bytes(receipt)
    return payloads


def publish_bundle(output_dir: pathlib.Path, payloads: dict[str, bytes]) -> str:
    """Publish a whole output directory atomically; identical replay is a no-op."""
    if output_dir.exists():
        if not output_dir.is_dir():
            raise CompositionError("output path exists and is not a directory")
        children = list(output_dir.iterdir())
        actual_names = {path.name for path in children}
        if actual_names == set(payloads) and all(path.is_file() and not path.is_symlink() for path in children) and all((output_dir / name).read_bytes() == data for name, data in payloads.items()):
            return "replayed"
        raise CompositionError("output directory already contains different composition bytes")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        for name, data in payloads.items():
            (temporary / name).write_bytes(data)
        if output_dir.exists():
            raise CompositionError("output directory appeared during composition")
        os.replace(temporary, output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return "created"


def validate_policy(policy: dict[str, Any]) -> None:
    matches = [row for row in policy.get("sources", []) if isinstance(row, dict) and row.get("source_id") == "data_go_kr"]
    if len(matches) != 1 or matches[0].get("canonical_registry") != "data/data-go-kr.registry.json":
        raise CompositionError("source policy must bind exactly one canonical data_go_kr registry")
    importer = matches[0].get("importer", {})
    if importer.get("capability") != "catalogue_import":
        raise CompositionError("source policy importer capability is unsupported")
    publication = matches[0].get("publication", {})
    if not isinstance(publication, dict) or publication.get("automatic") is not False:
        raise CompositionError("source policy must keep publication automatic=false")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=pathlib.Path)
    parser.add_argument("--candidate", required=True, type=pathlib.Path)
    parser.add_argument("--diff", required=True, type=pathlib.Path)
    parser.add_argument("--refresh-evidence", required=True, type=pathlib.Path)
    parser.add_argument("--provider-index", type=pathlib.Path, default=ROOT / "data/provider-index.json")
    parser.add_argument("--source-policy", type=pathlib.Path, default=DEFAULT_REFRESH_POLICY)
    parser.add_argument("--enrichment-evidence", type=pathlib.Path)
    parser.add_argument("--producer-run-id", required=True)
    parser.add_argument("--producer-run-url", required=True)
    parser.add_argument("--output-dir", required=True, type=pathlib.Path)
    parser.add_argument("--expected-baseline-sha256")
    parser.add_argument("--expected-candidate-sha256")
    parser.add_argument("--expected-diff-sha256")
    args = parser.parse_args()

    try:
        baseline_digest = file_digest(args.baseline)
        candidate_digest = file_digest(args.candidate)
        diff_digest = file_digest(args.diff)
        evidence_digest = file_digest(args.refresh_evidence)
        provider_index_digest = file_digest(args.provider_index)
        source_policy_digest = file_digest(args.source_policy)
        enrichment_digest = file_digest(args.enrichment_evidence) if args.enrichment_evidence else None
        script_digest = file_digest(pathlib.Path(__file__))
        receipt_schema_digest = file_digest(RECEIPT_SCHEMA)
        registry_schema_digest = file_digest(REGISTRY_SCHEMA)
        provider_schema_digest = file_digest(PROVIDER_INDEX_SCHEMA)
        diff_schema_digest = file_digest(DIFF_SCHEMA)
        refresh_schema_digest = file_digest(REFRESH_EVIDENCE_SCHEMA)
        enrichment_schema_digest = file_digest(ENRICHMENT_EVIDENCE_SCHEMA)
        for label, expected, actual in (
            ("baseline", args.expected_baseline_sha256, baseline_digest["sha256"]),
            ("candidate", args.expected_candidate_sha256, candidate_digest["sha256"]),
            ("diff", args.expected_diff_sha256, diff_digest["sha256"]),
        ):
            if expected and expected != actual:
                raise CompositionError(f"{label} sha256 does not match the pinned expected digest")
        baseline = load_json(args.baseline)
        candidate = load_json(args.candidate)
        diff = load_json(args.diff)
        evidence = load_json(args.refresh_evidence)
        provider_index = load_json(args.provider_index)
        policy = load_json(args.source_policy)
        enrichment_evidence = load_json(args.enrichment_evidence) if args.enrichment_evidence else None
        validate_policy(policy)
        schema = load_json(REGISTRY_SCHEMA)
        provider_schema = load_json(PROVIDER_INDEX_SCHEMA)
        diff_schema = load_json(DIFF_SCHEMA)
        refresh_schema = load_json(REFRESH_EVIDENCE_SCHEMA)
        validate_registry_schema(baseline, schema, "baseline")
        validate_registry_schema(candidate, schema, "candidate")
        jsonschema.Draft202012Validator(provider_schema, format_checker=jsonschema.FormatChecker()).validate(provider_index)
        jsonschema.Draft202012Validator(diff_schema, format_checker=jsonschema.FormatChecker()).validate(diff)
        jsonschema.Draft202012Validator(refresh_schema, format_checker=jsonschema.FormatChecker()).validate(evidence)
        if enrichment_evidence is not None:
            enrichment_schema = load_json(ENRICHMENT_EVIDENCE_SCHEMA)
            jsonschema.Draft202012Validator(enrichment_schema, format_checker=jsonschema.FormatChecker()).validate(enrichment_evidence)
        verify_refresh_inputs(
            baseline, candidate, diff, evidence, baseline_digest, candidate_digest, diff_digest,
            args.producer_run_id, args.producer_run_url,
        )
        if not isinstance(provider_index, dict):
            raise CompositionError("provider index must be an object")
        result = compose_registries(
            baseline, candidate, provider_index,
            baseline_sha256=baseline_digest["sha256"], candidate_sha256=candidate_digest["sha256"],
            provider_index_sha256=provider_index_digest["sha256"], enrichment_evidence=enrichment_evidence,
            registry_schema=schema,
        )
        validate_registry_schema(result["composed_registry"], schema, "composed candidate")
        validate_registry_schema(result["ready_scope_registry"], schema, "ready scope")
        input_digests = {
            "baseline": baseline_digest,
            "candidate": candidate_digest,
            "full_diff": diff_digest,
            "refresh_evidence": evidence_digest,
            "provider_index": provider_index_digest,
            "source_policy": source_policy_digest,
            "registry_schema": registry_schema_digest,
            "provider_index_schema": provider_schema_digest,
            "diff_schema": diff_schema_digest,
            "refresh_evidence_schema": refresh_schema_digest,
            "enrichment_evidence_schema": enrichment_schema_digest,
            "composer": script_digest,
            "receipt_schema": receipt_schema_digest,
        }
        if enrichment_digest:
            input_digests["enrichment_evidence"] = enrichment_digest
        payloads = build_bundle(result, input_digests=input_digests, run_id=args.producer_run_id, run_url=args.producer_run_url)
        publish_status = publish_bundle(args.output_dir, payloads)
        print(json.dumps({"status": result["status"], "output": args.output_dir.as_posix(), "publish_status": publish_status, "counts": result["semantic_diff"]["summary"]}, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - command reports a bounded failure
        print(f"FAIL compose upstream catalogue candidate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
