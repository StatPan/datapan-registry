#!/usr/bin/env python3
"""Resume bounded detail enrichment and compose one upstream catalogue candidate.

This worker never replaces the producer's candidate bytes. Enrichment is emitted
as separate digest-bound evidence for the reviewed composer interface.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import html
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Callable

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from upstream_catalogue_handoff import (  # noqa: E402
    HandoffError,
    admission_id,
    admission_order_key,
    add_admission,
    derive_legacy_floor,
    empty_ledger,
    find_admitted_observation,
    validate_admission_row,
    validate_collector_admission_envelope,
    validate_ledger,
)
import upstream_catalogue_derivation as DERIVATION  # noqa: E402

CHECKPOINT_SCHEMA = "datapan.upstream-catalogue-checkpoint.v1"
ENRICHMENT_SCHEMA = "datapan.catalogue-enrichment-evidence.v1"
REPLAY_PERSISTENCE_SCHEMA = "datapan.upstream-catalogue-replay-persistence.v1"
REPLAY_PERSISTENCE_RECEIPT = "upstream-catalogue-replay-persistence-receipt.json"
STATE_FILE_LIMIT = 256 * 1024
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_DETAIL_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
DETAIL_FAILURE_CODES = frozenset({
    "timeout", "transport_error", "provider_http_error", "response_bytes_cap",
    "unsafe_redirect", "contract_or_parse_error", "missing_link_detail_operations",
    "unsafe_or_unregistered_operation_host", "observation_mismatch", "unexpected_error",
    "insufficient_budget_for_link_resolver", "resolved_link_operation_contract_unproven",
})
DETAIL_FAILURE_PHASES = frozenset({"page", "resolver"})
CONTRACT_FAILURES = {
    "no_reviewed_declaration": {
        "unresolved_requirements": ["reviewed_operation_declaration"],
        "next_action": "review_authoritative_declaration",
    },
    "subject_binding_unproven": {
        "unresolved_requirements": ["subject_binding"],
        "next_action": "verify_subject_binding",
    },
    "declaration_evidence_rejected": {
        "unresolved_requirements": ["declaration_source_binding", "operation_contract_validation"],
        "next_action": "review_declaration_evidence",
    },
    "validation_detail_unknown": {
        "unresolved_requirements": [],
        "next_action": "inspect_bound_validation_evidence",
    },
}
DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_RETRIES_PER_DETAIL = 2
DEFAULT_MAX_ATTEMPTS = 24
DEFAULT_MAX_QUEUE = 48
DEFAULT_MAX_ACTIVE_GENERATIONS = 8
DEFAULT_MAX_GENERATIONS = 64
DEFAULT_MAX_RETRY_STATES = 4096
STATE_INDEX_FILE_LIMIT = 2 * 1024 * 1024
LEASE_SECONDS = 45 * 60
ARTIFACT_RETENTION_DAYS = 30
DETAIL_OBSERVATION_TTL_DAYS = 21
SECRET_QUERY_KEYS = {"key", "apikey", "api_key", "servicekey", "service_key", "token", "access_token", "authorization", "signature"}
ANCHOR_RE = re.compile(r"(?is)<a\b[^>]*>.*?</a>")
HREF_RE = re.compile(r"(?is)\bhref\s*=\s*[\"']([^\"']+)[\"']")
GUIDE_MARKER_RE = re.compile(r"(?i)(guide|manual|documentation|document|이용안내|가이드|매뉴얼|설명서|지침)")


class DetailPageObservation:
    __slots__ = ("body", "page_bytes", "page_url", "effective_url", "page_sha256", "observed_at")

    def __init__(self, *, body: str, page_url: str, effective_url: str, page_sha256: str, observed_at: str, page_bytes: bytes | None = None) -> None:
        self.body = body
        self.page_bytes = page_bytes if page_bytes is not None else body.encode("utf-8")
        self.page_url = page_url
        self.effective_url = effective_url
        self.page_sha256 = page_sha256
        self.observed_at = observed_at


class KnownDeclarationFailure(Exception):
    """A fixed classification emitted only at an explicit failed boundary."""

    def __init__(self, reason: str) -> None:
        if reason not in CONTRACT_FAILURES:
            raise ValueError("invalid_contract_failure_reason")
        self.reason = reason


def contract_failure_value(reason: str) -> dict[str, Any]:
    detail = CONTRACT_FAILURES.get(reason)
    if detail is None:
        raise ValueError("invalid_contract_failure_reason")
    return {
        "version": 1,
        "reason": reason,
        "unresolved_requirements": list(detail["unresolved_requirements"]),
        "next_action": detail["next_action"],
    }


def validate_contract_failure(value: Any, *, identity: str | None = None) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "reason", "unresolved_requirements", "next_action"}
        or not isinstance(value.get("version"), int)
        or isinstance(value.get("version"), bool)
        or not isinstance(value.get("reason"), str)
    ):
        raise ValueError("invalid_contract_failure")
    expected = contract_failure_value(value["reason"])
    if value != expected:
        raise ValueError("invalid_contract_failure")
    if identity is not None:
        subject_id = str(SEOUL_DECLARATION.DECLARATION["subject"]["portal_dataset_id"])
        if (
            value["reason"] == "no_reviewed_declaration" and identity == subject_id
            or value["reason"] in {"subject_binding_unproven", "declaration_evidence_rejected"}
            and identity != subject_id
        ):
            raise ValueError("invalid_contract_failure_subject_binding")
    return expected


def seoul_subject_binding_matches(row: Any, identity: str) -> bool:
    """Check only the fixed subject identity fields, not guide or operation evidence."""
    subject = SEOUL_DECLARATION.DECLARATION["subject"]
    if not isinstance(row, Mapping):
        return False
    source = row.get("source")
    source = source if isinstance(source, Mapping) else {}
    raw = source.get("raw")
    raw = raw if isinstance(raw, Mapping) else {}
    return (
        row.get("provider") == subject["provider"]
        and str(row.get("id") or "") == identity == str(subject["portal_dataset_id"])
        and source.get("system") == subject["source_system"]
        and source.get("url") == subject["source_url"]
        and raw.get("api_type") == subject["source_api_type"]
        and raw.get("id") == subject["source_uddi"]
        and raw.get("list_id") == subject["portal_dataset_id"]
        and raw.get("meta_url") == subject["source_meta_url"]
    )


class LinkResolverObservation:
    __slots__ = ("body", "request_url", "effective_url", "observed_at")

    def __init__(self, *, body: bytes, request_url: str, effective_url: str, observed_at: str) -> None:
        self.body = body
        self.request_url = request_url
        self.effective_url = effective_url
        self.observed_at = observed_at


def extract_current_template_dataset_id(page_html: str, expected_id: str) -> dict[str, str | None] | None:
    return DETAIL_HELPERS.extract_current_template_dataset_id(page_html, expected_id)


def validate_link_resolver_response(
    raw_bytes: bytes,
    expected_id: str,
    observed_at: str,
    *,
    expected_public_data_detail_pk: str | None = None,
) -> dict[str, Any]:
    return DETAIL_HELPERS.validate_link_resolver_response(
        raw_bytes, expected_id, observed_at,
        expected_public_data_detail_pk=expected_public_data_detail_pk,
    )


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def timestamp(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generator_revision() -> str:
    source = pathlib.Path(__file__)
    handoff = source.with_name("upstream_catalogue_handoff.py")
    declaration_helper = source.with_name("seoul_oa109_operation_declaration.py")
    snapshot_generator = source.with_name("generate-seoul-oa109-subject-snapshot.py")
    declaration = source.parent.parent / "contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json"
    historical_snapshot = source.parent.parent / "contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json"
    return sha256_bytes(canonical_json({
        "processor_script_sha256": file_sha256(source),
        "collector_handoff_helper_sha256": file_sha256(handoff),
        "seoul_operation_declaration_helper_sha256": file_sha256(declaration_helper),
        "seoul_historical_subject_snapshot_generator_sha256": file_sha256(snapshot_generator),
        "seoul_operation_declaration_sha256": file_sha256(declaration),
        "seoul_historical_subject_snapshot_sha256": file_sha256(historical_snapshot),
    }))


def derivation_processor_revision() -> str:
    source = pathlib.Path(__file__)
    handoff = source.with_name("upstream_catalogue_handoff.py")
    derivation = source.with_name("upstream_catalogue_derivation.py")
    return sha256_bytes(canonical_json({
        "processor_script_sha256": file_sha256(source),
        "collector_handoff_helper_sha256": file_sha256(handoff),
        "same_observation_derivation_helper_sha256": file_sha256(derivation),
    }))


def extractor_revision() -> str:
    return file_sha256(pathlib.Path(__file__).with_name("generate-batch-link-detail-registry-patches.py"))


def load_json(path: pathlib.Path, *, maximum_bytes: int | None = None) -> Any:
    if maximum_bytes is not None and path.stat().st_size > maximum_bytes:
        raise ValueError(f"input exceeds byte limit: {path.name}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def load_detail_helpers() -> Any:
    helper_path = pathlib.Path(__file__).with_name("generate-batch-link-detail-registry-patches.py")
    spec = importlib.util.spec_from_file_location("link_detail_batch_helpers", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load existing link-detail pure helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_seoul_declaration_helper() -> Any:
    helper_path = pathlib.Path(__file__).with_name("seoul_oa109_operation_declaration.py")
    spec = importlib.util.spec_from_file_location("seoul_oa109_operation_declaration", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load pinned Seoul operation declaration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DETAIL_HELPERS = load_detail_helpers()
SEOUL_DECLARATION = load_seoul_declaration_helper()


def load_registry(path: pathlib.Path) -> list[dict[str, Any]]:
    value = load_json(path, maximum_bytes=MAX_INPUT_BYTES)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"registry must be an array of objects: {path.name}")
    return value


def record_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("dataset_id") or "").strip()


def unique_rows(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = record_id(row)
        if not identity:
            raise ValueError(f"{label} contains a record without id")
        if identity in result:
            raise ValueError(f"{label} contains duplicate id {identity}")
        result[identity] = row
    return result


def row_fingerprint(row: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(row))


def source_fingerprint(row: dict[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    raw = source.get("raw") if isinstance(source.get("raw"), dict) else {}
    source_raw = dict(raw)
    source_raw.pop("request_cnt", None)
    view = {
        "provider": row.get("provider"),
        "id": record_id(row),
        "source_system": source.get("system"),
        "source_url": source.get("url"),
        "source_raw": source_raw,
    }
    return sha256_bytes(canonical_json(view))


def guide_fingerprint(row: dict[str, Any]) -> str | None:
    value = pick_raw(row).get("guide_url")
    if not isinstance(value, str) or not value.strip():
        return None
    return sha256_bytes(value.encode("utf-8"))


def pick_raw(row: dict[str, Any]) -> dict[str, Any]:
    source = row.get("source")
    raw = source.get("raw") if isinstance(source, dict) else None
    return raw if isinstance(raw, dict) else {}


def row_is_link(row: dict[str, Any]) -> bool:
    raw = pick_raw(row)
    names = (
        "type", "kind", "catalog_type", "catalogue_type", "data_type", "dataType",
        "api_type", "apiType", "api_kind", "apiKind", "dataset_type", "datasetType",
        "service_type", "serviceType", "provid_type", "providType", "openapi_type",
        "openApiType", "link_type", "linkType", "interface_type", "interfaceType",
    )
    values = [row.get(name) for name in names] + [raw.get(name) for name in names]
    normalized = {re.sub(r"[^a-z]", "", str(value).lower()) for value in values if value is not None}
    return bool(normalized.intersection({"link", "linkapi", "linkedapi", "linkdata"}))


def detail_is_trustworthy(row: dict[str, Any]) -> bool:
    operations = row.get("operations")
    if not isinstance(operations, list) or not operations:
        return False
    for operation in operations:
        if not isinstance(operation, dict) or not operation.get("name") or not operation.get("endpoint"):
            return False
        source = operation.get("source")
        if not isinstance(source, dict) or not source.get("system") or not source.get("url") or not isinstance(source.get("raw"), dict):
            return False
    return True


def safe_public_page_url(dataset_id: str) -> str:
    if not dataset_id.isdigit():
        raise ValueError("data.go.kr detail identity must be numeric")
    return f"https://www.data.go.kr/data/{int(dataset_id)}/openapi.do"


def candidate_detail_url(row: dict[str, Any], dataset_id: str) -> str:
    del row  # Metadata URLs remain candidate-bound; the portal fetch URL is derived from the API id.
    return safe_public_page_url(dataset_id)


def observed_guide_url(page_html: str, page_url: str) -> str | None:
    for anchor in ANCHOR_RE.findall(page_html):
        href = HREF_RE.search(anchor)
        if not href or not GUIDE_MARKER_RE.search(anchor):
            continue
        value = urllib.parse.urljoin(page_url, html.unescape(href.group(1)).strip())
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment
        ):
            continue
        query_keys = {urllib.parse.unquote(item.split("=", 1)[0]).strip().lower() for item in parsed.query.split("&") if item}
        if query_keys.intersection(SECRET_QUERY_KEYS):
            continue
        return value
    return None


def redact_operation_raw(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if re.search(r"(?i)(service.?key|api.?key|access.?token|authorization|secret)", str(key)) else redact_operation_raw(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_operation_raw(item) for item in value]
    if isinstance(value, str) and re.search(r"(?i)(servicekey|api[_-]?key|access[_-]?token|authorization)=", value):
        return "[REDACTED]"
    return value


class DetailResponseBytesCapError(ValueError):
    """The public detail body exceeded the fixed response byte cap."""

    def __init__(self, *_args: Any) -> None:
        super().__init__("detail_response_too_large")


class UnsafeDetailRedirectError(urllib.error.URLError):
    """A public detail request attempted a redirect outside its fixed host."""

    def __init__(self, *_args: Any) -> None:
        super().__init__("redirect_outside_allowed_public_detail_host")


class LinkResolverContractError(ValueError):
    """The fixed read-only link resolver violated its bounded response contract."""


class SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_host: str = "www.data.go.kr") -> None:
        super().__init__()
        self.allowed_host = allowed_host
        self.redirects = 0

    def redirect_request(self, request: urllib.request.Request, response: Any, code: int, message: str, headers: Any, new_url: str) -> urllib.request.Request | None:
        # A redirect is another physical request, even when it repeats the
        # same URL. Do not let urllib perform an unaccounted hidden GET.
        self.redirects += 1
        raise UnsafeDetailRedirectError()


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: urllib.request.Request, response: Any, code: int, message: str, headers: Any, new_url: str) -> urllib.request.Request | None:
        raise UnsafeDetailRedirectError()


def fetch_public_detail(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> DetailPageObservation:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "www.data.go.kr" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("unsafe_detail_url")
    request = urllib.request.Request(url, headers={"User-Agent": "datapan-registry-continuous-catalogue/1.0"})
    # Ignore ambient proxy configuration as it may attach Proxy-Authorization.
    # The worker's persisted physical-request accounting covers this single
    # GET only; urllib must not follow any redirect internally.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        SameHostRedirectHandler(),
    )
    with opener.open(request, timeout=timeout) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != "www.data.go.kr" or final.username or final.password or response.geturl() != url:
            raise UnsafeDetailRedirectError()
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_DETAIL_BYTES:
            raise DetailResponseBytesCapError()
        body_bytes = response.read(MAX_DETAIL_BYTES + 1)
        if len(body_bytes) > MAX_DETAIL_BYTES:
            raise DetailResponseBytesCapError()
    return DetailPageObservation(
        body=body_bytes.decode("utf-8", errors="replace"), page_url=url, effective_url=response.geturl(),
        page_sha256=sha256_bytes(body_bytes), observed_at=timestamp(), page_bytes=body_bytes,
    )


def fetch_link_resolver(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> LinkResolverObservation:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.hostname != "www.data.go.kr"
        or parsed.path != "/tcs/dss/selectApiLinkUrl.do"
        or parsed.username or parsed.password or parsed.fragment
        or parsed.query.count("=") != 1 or not re.fullmatch(r"publicDataPk=[0-9]+", parsed.query)
        or parsed.netloc != "www.data.go.kr"
    ):
        raise LinkResolverContractError("unsafe_link_resolver_request_url")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "datapan-registry-continuous-catalogue/1.0",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    # A process-wide proxy can attach Proxy-Authorization even when the
    # resolver request itself has no credentials. This fixed portal read must
    # not inherit ambient proxy credentials or follow a redirect.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        RejectRedirectHandler(),
    )
    with opener.open(request, timeout=timeout) as response:
        if response.getcode() != 200 or response.geturl() != url:
            raise UnsafeDetailRedirectError()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise LinkResolverContractError("link_resolver_content_type_invalid")
        content_encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        if content_encoding not in {"", "identity"}:
            raise LinkResolverContractError("link_resolver_content_encoding_invalid")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > DETAIL_HELPERS.MAX_LINK_RESOLVER_RESPONSE_BYTES:
                    raise DetailResponseBytesCapError()
            except ValueError as exc:
                if isinstance(exc, DetailResponseBytesCapError):
                    raise
                raise LinkResolverContractError("link_resolver_content_length_invalid") from exc
        body = response.read(DETAIL_HELPERS.MAX_LINK_RESOLVER_RESPONSE_BYTES + 1)
        if len(body) > DETAIL_HELPERS.MAX_LINK_RESOLVER_RESPONSE_BYTES:
            raise DetailResponseBytesCapError()
    return LinkResolverObservation(
        body=body, request_url=url, effective_url=url, observed_at=timestamp(),
    )


def safe_operation(operation: dict[str, Any]) -> bool:
    endpoint = str(operation.get("endpoint") or "")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    keys = {urllib.parse.unquote(part.split("=", 1)[0]).strip().lower() for part in parsed.query.split("&") if part}
    if keys.intersection(SECRET_QUERY_KEYS) or re.search(r"(?i)bearer\s+[a-z0-9._~+/-]+=*", endpoint):
        return False
    return True


def detail_queue(
    baseline_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]],
    cached_records: dict[str, dict[str, Any]] | None = None, now: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    baseline = unique_rows(baseline_rows, "baseline")
    candidate = unique_rows(candidate_rows, "candidate")
    cached_records = cached_records or {}
    now = now or utc_now()
    queued: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for identity, row in sorted(candidate.items()):
        if not row_is_link(row):
            continue
        source_hash = source_fingerprint(row)
        guide_hash = guide_fingerprint(row)
        prior = baseline.get(identity)
        cached = cached_records.get(identity)
        cached_provenance = cached.get("source_provenance") if isinstance(cached, dict) else None
        fresh_cache = False
        if (
            isinstance(cached, dict) and cached.get("source_sha256") == source_hash
            and cached.get("guide_sha256") == guide_hash and isinstance(cached_provenance, dict)
        ):
            try:
                fresh_cache = now - parse_timestamp(str(cached_provenance["observed_at"])) < dt.timedelta(days=DETAIL_OBSERVATION_TTL_DAYS)
            except (KeyError, ValueError):
                fresh_cache = False
        if fresh_cache:
            retained.append({"id": identity, "source_sha256": source_hash, "guide_sha256": guide_hash, "status": "retained_unchanged"})
            continue
        if prior is None:
            reason = "new_link_detail"
        elif source_fingerprint(prior) != source_hash:
            reason = "changed_link_source"
        elif guide_fingerprint(prior) != guide_hash:
            reason = "changed_link_guide"
        else:
            # The composer preserves unchanged baseline operation bytes. A
            # worker only has to observe detail for new/changed contracts or
            # when an earlier worker observation has aged out of its TTL.
            if isinstance(cached, dict):
                reason = "detail_observation_missing_or_stale"
            else:
                continue
        if not fresh_cache:
            queued.append({
                "id": identity,
                "reason": reason,
                "source_sha256": source_hash,
                "guide_sha256": guide_hash,
            })
    return queued, retained, pending


def source_queue_cursor(index_path: pathlib.Path, fallback: int = 0) -> int:
    if not index_path.is_file():
        return fallback
    index = load_json(index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
    if not isinstance(index, dict) or index.get("schema_version") != CHECKPOINT_SCHEMA:
        raise ValueError("corrupt_generation_index")
    value = index.get("detail_queue_cursor", fallback)
    if not isinstance(value, int) or value < 0:
        raise ValueError("corrupt_detail_queue_cursor")
    return value


def source_retry_state(index_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    if not index_path.is_file():
        return {}
    index = load_json(index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
    if not isinstance(index, dict) or index.get("schema_version") != CHECKPOINT_SCHEMA:
        raise ValueError("corrupt_generation_index")
    retry_state = index.get("detail_retry_state", {})
    if not isinstance(retry_state, dict) or len(retry_state) > DEFAULT_MAX_RETRY_STATES:
        raise ValueError("corrupt_detail_retry_state")
    for identity, row in retry_state.items():
        if (
            not isinstance(identity, str) or not isinstance(row, dict)
            or not {"source_sha256", "guide_sha256", "attempts", "last_attempt_at"}.issubset(row)
            or set(row) - {"source_sha256", "guide_sha256", "attempts", "last_attempt_at", "failure_diagnostic"}
            or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("source_sha256") or ""))
            or row.get("guide_sha256") is not None and not re.fullmatch(r"[a-f0-9]{64}", str(row.get("guide_sha256")))
            or not isinstance(row.get("attempts"), int) or row["attempts"] < 1
        ):
            raise ValueError("corrupt_detail_retry_state")
        parse_timestamp(str(row.get("last_attempt_at")))
        if "failure_diagnostic" in row:
            try:
                validate_failure_diagnostic(row["failure_diagnostic"], identity=identity)
            except ValueError as exc:
                raise ValueError("corrupt_detail_retry_state") from exc
    return retry_state


def reserve_requests(
    checkpoint: dict[str, Any], queue: list[dict[str, Any]], *, cursor: int,
    processor_run_id: str, max_attempts: int, max_queue: int,
    retries_per_detail: int, now: dt.datetime, no_advance_when_empty: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Durably account a fair bounded request slice before any network call."""
    if not queue:
        rotated: list[dict[str, Any]] = []
    else:
        ordered = sorted(queue, key=lambda row: row["id"])
        start = cursor % len(ordered)
        rotated = (ordered[start:] + ordered[:start])

    attempts = checkpoint.setdefault("attempts_by_id", {})
    max_for_row = retries_per_detail + 1
    inspected: list[dict[str, Any]] = []
    budget_left = max_attempts
    reserved: dict[str, int] = {}
    # `max_queue` caps identities selected for this request slice, not local
    # inspection of already exhausted rows. Scan past exhausted identities
    # so an exhausted prefix cannot starve a later eligible identity forever.
    # This loop performs no network I/O; physical calls remain bounded by the
    # pre-reserved `max_attempts` and each row's lifetime allowance.
    eligible_inspected = 0
    for row in rotated:
        inspected.append(row)
        identity = row["id"]
        remaining = max_for_row - int(attempts.get(identity, 0))
        if remaining <= 0:
            continue
        eligible_inspected += 1
        allocation = min(remaining, budget_left)
        if allocation:
            reserved[identity] = allocation
            budget_left -= allocation
        if budget_left == 0 or eligible_inspected >= max_queue:
            break
    reservation_records = []
    for row in inspected:
        count = reserved.get(row["id"], 0)
        if count:
            reservation_records.append({
                "id": row["id"], "attempts_reserved": count,
                "source_sha256": row["source_sha256"], "guide_sha256": row["guide_sha256"],
            })
            attempts[row["id"]] = int(attempts.get(row["id"], 0)) + count
    reserved_ids = {row["id"] for row in reservation_records}
    reserved_attempts = sum(row["attempts_reserved"] for row in reservation_records)
    # The source-wide cursor lives in the small checkpoint/index, so a new
    # weekly candidate digest does not restart from the first catalogue page.
    # Advance past every inspected row, including those already at their
    # per-identity retry cap, or an exhausted prefix can pin the cursor forever.
    cursor_advance = 0 if no_advance_when_empty and reserved_attempts == 0 else len(inspected)
    if no_advance_when_empty and reserved_attempts == 0:
        # Pure same-observation recomposition does not own the durable retry
        # cursor. Preserve its exact value, even when the queue is empty or an
        # exhausted queue has a cursor outside the current queue length.
        checkpoint["detail_queue_cursor"] = cursor
    elif queue:
        checkpoint["detail_queue_cursor"] = (cursor + cursor_advance) % len(queue)
    else:
        # An empty queue has no position to advance over. Preserve the source
        # cursor so a cached/local-only pass cannot reset fairness state.
        checkpoint["detail_queue_cursor"] = cursor
    reservation = {
        "owner_run_id": processor_run_id,
        "generation_id": checkpoint["generation_id"],
        "fencing_token": int(checkpoint["fencing_token"]),
        "reserved_at": timestamp(now),
        "expires_at": checkpoint["lease"]["expires_at"],
        "attempt_budget": max_attempts,
        "reserved_attempts": reserved_attempts,
        "attempts_made": 0,
        "records": reservation_records,
    }
    checkpoint["request_reservation"] = reservation
    checkpoint["attempts_consumed"] = int(checkpoint.get("attempts_consumed", 0)) + reserved_attempts
    reserved_queue = [row for row in inspected if row["id"] in reserved_ids]
    unqueued = [row for row in queue if row["id"] not in reserved_ids]
    return reservation, reserved_queue, unqueued


def checkpoint_digest(checkpoint: dict[str, Any]) -> str:
    unsigned = dict(checkpoint)
    unsigned.pop("checkpoint_sha256", None)
    return sha256_bytes(canonical_json(unsigned))


def seal_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    checkpoint["checkpoint_sha256"] = checkpoint_digest(checkpoint)
    return checkpoint


def verify_checkpoint(value: Any, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != CHECKPOINT_SCHEMA:
        raise ValueError("corrupt_checkpoint_schema")
    claimed = value.get("checkpoint_sha256")
    if not isinstance(claimed, str) or claimed != checkpoint_digest(value):
        raise ValueError("corrupt_checkpoint_digest")
    if schema is not None:
        import jsonschema
        jsonschema.Draft202012Validator(schema).validate(value)
    return value


def _object_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def _replay_admission(
    checkpoint: Mapping[str, Any], index: Mapping[str, Any],
) -> dict[str, Any]:
    observation = checkpoint.get("last_observation")
    references = checkpoint.get("input_artifacts")
    if not isinstance(observation, Mapping) or not isinstance(references, list):
        raise ValueError("replay_original_observation_invalid")
    matching_references = [
        item for item in references
        if isinstance(item, Mapping)
        and str(item.get("run_id") or "") == str(observation.get("producer_run_id") or "")
        and item.get("evidence_sha256") == observation.get("refresh_evidence_sha256")
        and str(item.get("artifact_id") or "").isdigit()
    ]
    if len(matching_references) != 1:
        raise ValueError("replay_original_input_artifact_invalid")
    reference = matching_references[0]
    ledger_value = index.get("collector_handoff")
    if ledger_value is None:
        raise ValueError("replay_original_admission_missing")
    try:
        ledger = validate_ledger(ledger_value)
    except HandoffError as exc:
        raise ValueError(str(exc)) from exc
    matches = [
        item for item in ledger["admitted_observations"]
        if item["producer_run_id"] == str(observation.get("producer_run_id") or "")
        and item["refresh_evidence_sha256"] == observation.get("refresh_evidence_sha256")
        and item["artifact_id"] == str(reference.get("artifact_id") or "")
        and item["generation_id"] == checkpoint.get("generation_id")
    ]
    if len(matches) != 1:
        raise ValueError("replay_original_admission_not_unique")
    admission = validate_admission_row(matches[0])
    generation_inputs = checkpoint.get("generation_inputs")
    if (
        not isinstance(generation_inputs, Mapping)
        or admission["candidate_sha256"] != generation_inputs.get("candidate_sha256")
        or admission["observed_at"] != observation.get("observed_at")
        or reference.get("candidate_sha256") != admission["candidate_sha256"]
    ):
        raise ValueError("replay_original_admission_binding_mismatch")
    return admission


def _replay_index_transition(
    prior: Mapping[str, Any], current: Mapping[str, Any], *,
    generation_id: str, prior_checkpoint: Mapping[str, Any], current_checkpoint: Mapping[str, Any],
) -> list[str]:
    if (
        prior.get("schema_version") != CHECKPOINT_SCHEMA
        or current.get("schema_version") != CHECKPOINT_SCHEMA
        or not isinstance(prior.get("generations"), list)
        or not isinstance(current.get("generations"), list)
    ):
        raise ValueError("replay_generation_index_invalid")
    prior_other = {key: value for key, value in prior.items() if key != "generations"}
    current_other = {key: value for key, value in current.items() if key != "generations"}
    if prior_other != current_other:
        raise ValueError("replay_generation_index_nonadmission_change")

    def split_rows(value: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        target: list[dict[str, Any]] = []
        others: list[dict[str, Any]] = []
        target_position = -1
        seen: set[str] = set()
        for position, raw in enumerate(value["generations"]):
            if not isinstance(raw, Mapping):
                raise ValueError("replay_generation_index_invalid")
            row = dict(raw)
            identity = row.get("generation_id")
            if not isinstance(identity, str) or identity in seen:
                raise ValueError("replay_generation_index_invalid")
            seen.add(identity)
            if identity == generation_id:
                target.append(row)
                target_position = position
            else:
                others.append(row)
        if len(target) != 1:
            raise ValueError("replay_generation_index_target_invalid")
        return target[0], others, target_position

    prior_target, prior_others, prior_position = split_rows(prior)
    current_target, current_others, current_position = split_rows(current)
    if prior_others != current_others:
        raise ValueError("replay_generation_index_unrelated_generation_changed")
    prior_expected = {
        "generation_id": generation_id,
        "status": prior_checkpoint.get("status"),
        "checkpoint": f"{generation_id}.json",
        "updated_at": prior_checkpoint.get("last_heartbeat_at"),
        "candidate_sha256": prior_checkpoint.get("generation_inputs", {}).get("candidate_sha256"),
    }
    current_expected = {
        "generation_id": generation_id,
        "status": current_checkpoint.get("status"),
        "checkpoint": f"{generation_id}.json",
        "updated_at": current_checkpoint.get("last_heartbeat_at"),
        "candidate_sha256": current_checkpoint.get("generation_inputs", {}).get("candidate_sha256"),
    }
    if prior_target != prior_expected or current_target != current_expected:
        raise ValueError("replay_generation_index_checkpoint_mismatch")
    changes: list[str] = []
    base = f"sources/{prior_checkpoint.get('source_id')}/index.json#/generations/{generation_id}"
    if prior_target["updated_at"] != current_target["updated_at"]:
        changes.append(f"{base}/updated_at")
    if prior_position != current_position:
        if current_position != len(current["generations"]) - 1:
            raise ValueError("replay_generation_index_order_invalid")
        changes.append(f"{base}/order")
    return changes


def _replay_transition_facts(
    *, prior_checkpoint: Any, current_checkpoint: Any,
    prior_index: Any, current_index: Any, result: Any,
    state_head_sha: str, checkpoint_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(state_head_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", state_head_sha):
        raise ValueError("replay_state_head_sha_invalid")
    prior = verify_checkpoint(prior_checkpoint, checkpoint_schema)
    current = verify_checkpoint(current_checkpoint, checkpoint_schema)
    if (
        prior.get("status") not in {"ready", "no-change"}
        or current.get("status") != prior.get("status")
        or current.get("source_id") != prior.get("source_id")
        or current.get("generation_id") != prior.get("generation_id")
    ):
        raise ValueError("replay_terminal_checkpoint_identity_mismatch")
    prior_immutable = copy.deepcopy(prior)
    current_immutable = copy.deepcopy(current)
    for value in (prior_immutable, current_immutable):
        value.pop("checkpoint_sha256", None)
        value.pop("last_heartbeat_at", None)
    if prior_immutable != current_immutable:
        raise ValueError("replay_checkpoint_immutable_field_changed")
    prior_heartbeat = parse_timestamp(str(prior.get("last_heartbeat_at") or ""))
    current_heartbeat = parse_timestamp(str(current.get("last_heartbeat_at") or ""))
    if current_heartbeat < prior_heartbeat:
        raise ValueError("replay_checkpoint_heartbeat_regressed")
    if not isinstance(prior_index, Mapping) or not isinstance(current_index, Mapping):
        raise ValueError("replay_generation_index_invalid")
    generation_id = str(prior["generation_id"])
    changes: list[str] = []
    checkpoint_path = f"sources/{prior['source_id']}/generations/{generation_id}.json#"
    if prior.get("last_heartbeat_at") != current.get("last_heartbeat_at"):
        changes.append(f"{checkpoint_path}/last_heartbeat_at")
    if prior.get("checkpoint_sha256") != current.get("checkpoint_sha256"):
        changes.append(f"{checkpoint_path}/checkpoint_sha256")
    changes.extend(_replay_index_transition(
        prior_index, current_index, generation_id=generation_id,
        prior_checkpoint=prior, current_checkpoint=current,
    ))
    admission = _replay_admission(prior, prior_index)
    if not isinstance(result, Mapping) or set(result) != {
        "status", "reason", "processing_replay", "candidate_available", "source_id",
        "producer_run_id", "processor_run_id", "processor_artifact_run_id",
    }:
        raise ValueError("replay_processing_result_shape_invalid")
    if (
        result.get("status") != "idle"
        or result.get("reason") != "exact_producer_delivery_replay"
        or result.get("processing_replay") is not True
        or result.get("candidate_available") is not False
        or result.get("source_id") != prior.get("source_id")
        or str(result.get("producer_run_id") or "") != admission["producer_run_id"]
        or not str(result.get("processor_run_id") or "")
        or not str(result.get("processor_artifact_run_id") or "")
    ):
        raise ValueError("replay_processing_result_identity_invalid")
    return {
        "schema_version": REPLAY_PERSISTENCE_SCHEMA,
        "state_head_sha": state_head_sha,
        "source_id": prior["source_id"],
        "generation_id": generation_id,
        "terminal_status": prior["status"],
        "prior_checkpoint_sha256": prior["checkpoint_sha256"],
        "checkpoint_sha256": current["checkpoint_sha256"],
        "generation_index_before_sha256": _object_sha256(prior_index),
        "generation_index_sha256": _object_sha256(current_index),
        "collector_admission_sha256": _object_sha256(admission),
        "original_observation_sha256": _object_sha256(prior["last_observation"]),
        "input_artifacts_sha256": _object_sha256(prior["input_artifacts"]),
        "output_artifact_sha256": _object_sha256(prior["output_artifact"]),
        "output_digests_sha256": _object_sha256(prior["output_digests"]),
        "previous_last_heartbeat_at": prior["last_heartbeat_at"],
        "last_heartbeat_at": current["last_heartbeat_at"],
        "producer_run_id": admission["producer_run_id"],
        "processor_run_id": str(result["processor_run_id"]),
        "processor_artifact_run_id": str(result["processor_artifact_run_id"]),
        "processing_result_sha256": _object_sha256(result),
        "state_changes": sorted(changes),
    }


def build_replay_persistence_receipt(**kwargs: Any) -> dict[str, Any]:
    receipt = _replay_transition_facts(**kwargs)
    receipt["receipt_sha256"] = _object_sha256(receipt)
    return receipt


def verify_replay_persistence_receipt(receipt: Any, **kwargs: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version", "state_head_sha", "source_id", "generation_id", "terminal_status",
        "prior_checkpoint_sha256", "checkpoint_sha256", "generation_index_before_sha256",
        "generation_index_sha256", "collector_admission_sha256", "original_observation_sha256",
        "input_artifacts_sha256", "output_artifact_sha256", "output_digests_sha256",
        "previous_last_heartbeat_at", "last_heartbeat_at", "producer_run_id", "processor_run_id",
        "processor_artifact_run_id", "processing_result_sha256", "state_changes", "receipt_sha256",
    }:
        raise ValueError("replay_persistence_receipt_shape_invalid")
    unsigned = dict(receipt)
    claimed = unsigned.pop("receipt_sha256")
    if not isinstance(claimed, str) or claimed != _object_sha256(unsigned):
        raise ValueError("replay_persistence_receipt_digest_invalid")
    expected = build_replay_persistence_receipt(**kwargs)
    if receipt != expected:
        raise ValueError("replay_persistence_receipt_binding_mismatch")
    return receipt


def generation_identity(
    source_id: str,
    source_scope: str,
    baseline_sha256: str | None,
    candidate_sha256: str | None,
    observation_failure_sha256: str | None,
    policy_sha256: str,
    adapter_sha256: str,
    same_observation_derivation: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    inputs = {
        "source_id": source_id,
        "source_scope": source_scope,
        "baseline_sha256": baseline_sha256,
        "candidate_sha256": candidate_sha256,
        # Successful observation timestamps do not create a second candidate
        # generation when the immutable candidate bytes did not change. A
        # failure has no candidate digest, so bind that failed observation to
        # its exact evidence bytes instead.
        "observation_failure_sha256": observation_failure_sha256,
        "policy_sha256": policy_sha256,
        "adapter_revision": adapter_sha256,
        "generator_revision": generator_revision(),
        "extractor_revision": extractor_revision(),
    }
    if same_observation_derivation is not None:
        inputs["same_observation_derivation"] = DERIVATION.validate_derivation_envelope(same_observation_derivation)
    return sha256_bytes(canonical_json(inputs)), inputs


def receipt_base(
    *, source_id: str, source_scope: str, generation_id: str, generation_inputs: dict[str, Any],
    observed_at: str, producer_run_id: str, processor_run_id: str, processor_artifact_run_id: str,
    repository: str, execution_mode: str,
    artifact_name: str, artifact_id: str | None, input_artifact_expires_at: str,
    output_artifact_expires_at: str,
    evidence_sha256: str | None, diff_sha256: str | None, status: str,
) -> dict[str, Any]:
    return {
        "schema_version": CHECKPOINT_SCHEMA,
        "source_id": source_id,
        "source_scope": source_scope,
        "generation_id": generation_id,
        "generation_inputs": generation_inputs,
        "observed_at": observed_at,
        "last_observation": {
            "observed_at": observed_at,
            "producer_run_id": producer_run_id,
            "refresh_evidence_sha256": evidence_sha256,
            "collection_status": "success" if evidence_sha256 else "missing",
            "execution_mode": execution_mode,
        },
        "observation_count": 1,
        "last_heartbeat_at": timestamp(),
        "last_progress_at": timestamp(),
        "status": status,
        "attempts_consumed": 0,
        "attempts_by_id": {},
        "detail_retry_reset_ids": [],
        "request_reservation": None,
        "detail_records": [],
        "output_digests": [],
        "detail_queue_cursor": 0,
        "output_artifact": {
            "repository": repository,
            "run_id": processor_artifact_run_id,
            "name": f"upstream-catalogue-processing-{processor_run_id}",
            "artifact_id": None,
            "expires_at": output_artifact_expires_at,
            "bundle_manifest_sha256": None,
        },
        "input_artifacts": [{
            "run_id": producer_run_id, "name": artifact_name, "artifact_id": artifact_id, "expires_at": input_artifact_expires_at,
            "candidate_sha256": generation_inputs.get("candidate_sha256"),
            "evidence_sha256": evidence_sha256,
            "diff_sha256": diff_sha256,
        }],
        "lease": None,
        "fencing_token": 0,
        "outcome": {"reason": "queued"},
    }


def active_lease(checkpoint: dict[str, Any], now: dt.datetime) -> bool:
    lease = checkpoint.get("lease")
    if not isinstance(lease, dict):
        return False
    try:
        return parse_timestamp(str(lease["expires_at"])) > now
    except (KeyError, ValueError):
        return False


def acquire_lease(checkpoint: dict[str, Any], run_id: str, now: dt.datetime) -> tuple[bool, str]:
    lease = checkpoint.get("lease")
    if active_lease(checkpoint, now):
        if isinstance(lease, dict) and lease.get("owner_run_id") == run_id:
            return True, "resumed_same_owner"
        return False, "lease_conflict"
    checkpoint["fencing_token"] = int(checkpoint.get("fencing_token", 0)) + 1
    checkpoint["lease"] = {
        "owner_run_id": run_id,
        "expires_at": timestamp(now + dt.timedelta(seconds=LEASE_SECONDS)),
        "fencing_token": checkpoint["fencing_token"],
    }
    return True, "lease_acquired"


def ensure_fence(checkpoint: dict[str, Any], run_id: str, token: int, now: dt.datetime) -> None:
    lease = checkpoint.get("lease")
    if not isinstance(lease, dict) or lease.get("owner_run_id") != run_id or lease.get("fencing_token") != token:
        raise ValueError("stale_lease_fencing_token")
    if parse_timestamp(str(lease.get("expires_at"))) <= now:
        raise ValueError("expired_lease_fencing_token")


def safe_error_class(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    text_value = str(exc).lower()
    if "too_large" in text_value:
        return "response_bytes_cap"
    if "redirect" in text_value:
        return "unsafe_redirect"
    if isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return "network_timeout_or_error"
    return name[:64]


def validate_failure_diagnostic(value: Any, *, identity: str | None = None) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or "code" not in value
        or set(value) - {"code", "http_status", "phase", "contract_failure"}
    ):
        raise ValueError("invalid_detail_failure_diagnostic")
    code = value.get("code")
    if not isinstance(code, str) or code not in DETAIL_FAILURE_CODES:
        raise ValueError("invalid_detail_failure_diagnostic")
    result: dict[str, Any] = {"code": code}
    if "phase" in value:
        phase = value["phase"]
        if not isinstance(phase, str) or phase not in DETAIL_FAILURE_PHASES:
            raise ValueError("invalid_detail_failure_diagnostic")
        result["phase"] = phase
    if "http_status" in value:
        status = value["http_status"]
        if (
            code != "provider_http_error" or not isinstance(status, int) or isinstance(status, bool)
            or not 400 <= status <= 599
        ):
            raise ValueError("invalid_detail_failure_diagnostic")
        result["http_status"] = status
    if "contract_failure" in value:
        if code != "resolved_link_operation_contract_unproven" or result.get("phase") != "resolver":
            raise ValueError("invalid_detail_failure_diagnostic")
        try:
            result["contract_failure"] = validate_contract_failure(
                value["contract_failure"], identity=identity,
            )
        except ValueError as exc:
            raise ValueError("invalid_detail_failure_diagnostic") from exc
    return result


def classify_detail_exception(exc: BaseException) -> dict[str, Any]:
    """Classify a fetch exception using fixed types and never inspect its text."""
    if isinstance(exc, urllib.error.HTTPError):
        diagnostic: dict[str, Any] = {"code": "provider_http_error"}
        status = exc.code
        if isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599:
            diagnostic["http_status"] = status
        return diagnostic
    if isinstance(exc, DetailResponseBytesCapError):
        return {"code": "response_bytes_cap"}
    if isinstance(exc, UnsafeDetailRedirectError):
        return {"code": "unsafe_redirect"}
    if isinstance(exc, (LinkResolverContractError, DETAIL_HELPERS.LinkDetailContractError)):
        return {"code": "contract_or_parse_error"}
    if isinstance(exc, TimeoutError):
        return {"code": "timeout"}
    if isinstance(exc, urllib.error.URLError):
        return {"code": "timeout" if isinstance(exc.reason, TimeoutError) else "transport_error"}
    if isinstance(exc, (ConnectionError, OSError)):
        return {"code": "transport_error"}
    return {"code": "unexpected_error"}


def extract_operations(row: dict[str, Any], body: str) -> list[dict[str, Any]]:
    operations = []
    for index, endpoint in enumerate(DETAIL_HELPERS.extract_link_detail_operation_urls(body)):
        operation = DETAIL_HELPERS.operation(row, endpoint, index, len(DETAIL_HELPERS.extract_link_detail_operation_urls(body)))
        if safe_operation(operation):
            operations.append(operation)
    return operations


def atomic_output(output_dir: pathlib.Path, name: str, value: Any) -> pathlib.Path:
    path = output_dir / name
    atomic_write_json(path, value)
    return path


def processing_result(
    checkpoint: dict[str, Any],
    *,
    exact_delivery_replay: bool = False,
    producer_run_id: str | None = None,
    processor_run_id: str | None = None,
    processor_artifact_run_id: str | None = None,
) -> dict[str, Any]:
    if exact_delivery_replay:
        return {
            "status": "idle",
            "reason": "exact_producer_delivery_replay",
            "processing_replay": True,
            "candidate_available": False,
            "source_id": checkpoint.get("source_id"),
            "producer_run_id": str(producer_run_id or ""),
            "processor_run_id": str(processor_run_id or producer_run_id or ""),
            "processor_artifact_run_id": str(processor_artifact_run_id or processor_run_id or producer_run_id or ""),
        }
    observation = checkpoint.get("last_observation")
    observation = observation if isinstance(observation, dict) else {}
    outcome = checkpoint.get("outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    return {
        "status": checkpoint.get("status"),
        "generation_id": checkpoint.get("generation_id"),
        "reason": outcome.get("reason"),
        "processing_replay": False,
        "candidate_available": checkpoint.get("status") == "ready",
        "source_id": checkpoint.get("source_id"),
        "producer_run_id": str(producer_run_id or observation.get("producer_run_id") or ""),
        "processor_run_id": str(processor_run_id or ""),
        "processor_artifact_run_id": str(processor_artifact_run_id or ""),
        "attempts_consumed": checkpoint.get("attempts_consumed", 0),
        "detail_failure_counts": outcome.get("detail_failure_counts", {}),
        "detail_reason_unavailable_count": outcome.get("detail_reason_unavailable_count", 0),
        "detail_unattempted_count": outcome.get("detail_unattempted_count", 0),
        "observed_at": observation.get("observed_at"),
        "last_heartbeat_at": checkpoint.get("last_heartbeat_at"),
        "last_progress_at": checkpoint.get("last_progress_at"),
    }


def write_state_outputs(output_dir: pathlib.Path, checkpoint: dict[str, Any], enrichment: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    enrichment_path = atomic_output(output_dir, "upstream-catalogue-enrichment-evidence.json", enrichment)
    heartbeat = atomic_output(output_dir, "upstream-catalogue-checkpoint-receipt.json", checkpoint)
    checkpoint["output_digests"] = [
        {"path": enrichment_path.name, "sha256": file_sha256(enrichment_path), "bytes": enrichment_path.stat().st_size},
        {"path": heartbeat.name, "sha256": file_sha256(heartbeat), "bytes": heartbeat.stat().st_size},
    ]


def composer_outputs_valid(
    output_dir: pathlib.Path,
    original_candidate_sha256: str,
    baseline_sha256: str,
) -> tuple[bool, str, dict[str, Any] | None]:
    required = (
        "composed-candidate.registry.json", "ready-scope.registry.json", "semantic-diff.json",
        "regeneration-queue.json", "quarantine.json", "composition-receipt.json",
    )
    for name in required:
        path = output_dir / name
        if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
            return False, f"composer_output_missing_or_oversize:{name}", None
    try:
        composed = load_json(output_dir / required[0], maximum_bytes=MAX_INPUT_BYTES)
        if not isinstance(composed, list):
            return False, "composer_candidate_not_registry_array", None
        for name in required[1:]:
            load_json(output_dir / name, maximum_bytes=MAX_INPUT_BYTES)
        receipt = load_json(output_dir / "composition-receipt.json", maximum_bytes=2 * 1024 * 1024)
        if not isinstance(receipt, dict):
            return False, "composer_receipt_not_object", None
        receipt_status = str(receipt.get("status") or receipt.get("composition_status") or "")
        # Both source digests must be present in the receipt. The composer owns
        # their exact field names; recursively verify the immutable candidate digest.
        flattened = json.dumps(receipt, sort_keys=True)
        if original_candidate_sha256 not in flattened or baseline_sha256 not in flattened:
            return False, "composer_receipt_missing_input_digest", receipt
        for name in required[:-1]:
            digest = file_sha256(output_dir / name)
            if digest not in flattened:
                return False, f"composer_receipt_missing_output_digest:{name}", receipt
        if receipt_status not in {"ready", "ready_scoped", "no_safe_change", "no_change", "quarantined", "global_invalid", "invalid", "retry"}:
            return False, "composer_receipt_unknown_status", receipt
        if receipt_status == "ready_scoped":
            scope = receipt.get("scope")
            if not isinstance(scope, dict) or scope.get("full_scope_fresh") is not False or scope.get("publication_allowed") is not False:
                return False, "scoped_ready_missing_partial_scope_guards", receipt
            if not isinstance(scope.get("global_counts"), dict):
                return False, "scoped_ready_missing_global_counts", receipt
            for key in ("applied_api_keys", "retained_pending_api_keys", "quarantined_api_keys"):
                if not isinstance(scope.get(key), list):
                    return False, f"scoped_ready_missing_{key}", receipt
        return True, receipt_status, receipt
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return False, safe_error_class(exc), None


def api_key_token(value: Any) -> str:
    if isinstance(value, dict):
        provider = str(value.get("provider") or "").strip().lower()
        identity = str(value.get("id") or "").strip()
        return f"{provider}/{identity}" if provider and identity else ""
    if isinstance(value, str):
        return value.strip().lower().replace(":", "/")
    return ""


def verify_worker_scope(
    receipt_status: str,
    receipt: dict[str, Any],
    worker_records: list[dict[str, Any]],
    unqueued: list[dict[str, Any]],
) -> tuple[bool, str, int]:
    """Require each unresolved worker row to remain explicit in composer scope."""
    scope = receipt.get("scope") if isinstance(receipt.get("scope"), dict) else {}
    pending = {api_key_token(value) for value in scope.get("retained_pending_api_keys", [])}
    quarantined = {api_key_token(value) for value in scope.get("quarantined_api_keys", [])}
    pending.discard("")
    quarantined.discard("")
    unresolved_keys = {
        f"data.go.kr/{row['id']}" for row in worker_records if row.get("status") == "retry"
    } | {f"data.go.kr/{row['id']}" for row in unqueued}
    quarantine_keys = {
        f"data.go.kr/{row['id']}" for row in worker_records if row.get("status") == "quarantined"
    }
    if receipt_status in {"ready_scoped", "no_safe_change", "no_change"}:
        missing_retry = unresolved_keys - (pending | quarantined)
        missing_quarantine = quarantine_keys - (pending | quarantined)
        if missing_retry or missing_quarantine:
            return False, "composer_scope_omits_worker_outcomes", len(pending | quarantined)
    return True, "", len(pending | quarantined)


def materialize_cached_seoul_declaration(
    row: dict[str, Any], outcome: dict[str, Any], *, now: dt.datetime, registered_hosts: set[str],
) -> dict[str, Any] | None:
    """Resolve a pinned declaration locally from a verified cached resolver chain.

    This path spends no detail-request budget. It only upgrades the exact
    previously quarantined OA-109 metadata outcome, and only while the
    digest-bound page/resolver observations remain within the ordinary detail
    freshness window. It does not turn an absent guide into a positive claim.
    """
    identity = str(outcome.get("api_key", {}).get("id") or "")
    if identity != SEOUL_DECLARATION.DECLARATION["subject"]["portal_dataset_id"]:
        return None
    if guide_fingerprint(row) is not None:
        return None
    metadata = outcome.get("link_metadata")
    if not isinstance(metadata, dict):
        return None
    try:
        SEOUL_DECLARATION.validate_subject_row(row)
        DETAIL_HELPERS.validate_link_metadata(metadata, identity, registered_hosts)
        page = metadata["page"]
        observed_at = parse_timestamp(str(page["observed_at"]))
        if observed_at > now + dt.timedelta(minutes=5) or now - observed_at >= dt.timedelta(days=DETAIL_OBSERVATION_TTL_DAYS):
            return None
        operation = SEOUL_DECLARATION.build_operation(row, None)
        provenance = SEOUL_DECLARATION.build_provenance(
            row,
            source_sha256=str(outcome["source_sha256"]),
            guide_sha256=outcome.get("guide_sha256"),
            observed_guide_url=None,
            observed_guide_url_sha256=None,
            link_metadata=metadata,
            operation=operation,
        )
        operations = [copy.deepcopy(item) for item in row.get("operations", [])]
        operations.append(operation)
        source_provenance = {
            "system": "data.go.kr",
            "page_url": page["url"],
            "effective_url": page["effective_url"],
            "page_sha256": page["sha256"],
            "observed_at": page["observed_at"],
        }
        record = {
            "api_key": {"provider": "data.go.kr", "id": identity},
            "status": "enriched",
            "source_sha256": outcome["source_sha256"],
            "guide_sha256": outcome.get("guide_sha256"),
            "observed_guide_url": None,
            "observed_guide_url_sha256": None,
            "operations": operations,
            "operations_sha256": sha256_bytes(canonical_json(operations)),
            "source_provenance": source_provenance,
            "declaration_provenance": provenance,
        }
        SEOUL_DECLARATION.validate_enriched_record(row, record)
        return record
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("resume_seoul_cached_declaration_binding_invalid") from exc


def validated_resume_records(
    path: pathlib.Path | None,
    *,
    checkpoint: dict[str, Any],
    state_dir: pathlib.Path,
    source_id: str,
    checkpoint_schema: dict[str, Any],
    provider_index_sha256: str,
    candidate_by_id: dict[str, dict[str, Any]],
    registered_hosts: set[str],
    now: dt.datetime,
    allow_parent_extractor_revision: bool = False,
    expected_owner_generation_id: str | None = None,
    reject_stale_source_or_guide_contract: bool = False,
) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    owner_checkpoints: list[dict[str, Any]] = []
    current_path = state_dir / "sources" / source_id / "generations" / f"{checkpoint['generation_id']}.json"
    if current_path.is_file():
        owner_checkpoints.append(verify_checkpoint(load_json(current_path, maximum_bytes=STATE_FILE_LIMIT), checkpoint_schema))
    index_path = state_dir / "sources" / source_id / "index.json"
    if index_path.is_file():
        index = load_json(index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
        if not isinstance(index, dict) or index.get("schema_version") != CHECKPOINT_SCHEMA or not isinstance(index.get("generations"), list):
            raise ValueError("corrupt_generation_index")
        for entry in reversed(index["generations"]):
            if not isinstance(entry, dict) or entry.get("generation_id") == checkpoint["generation_id"]:
                continue
            old_path = state_dir / "sources" / source_id / "generations" / f"{entry.get('generation_id')}.json"
            if old_path.is_file():
                try:
                    owner_checkpoints.append(verify_checkpoint(load_json(old_path, maximum_bytes=STATE_FILE_LIMIT), checkpoint_schema))
                except (ValueError, OSError, json.JSONDecodeError):
                    continue
    owner: dict[str, Any] | None = None
    evidence_digest: str | None = None
    for old in owner_checkpoints:
        if expected_owner_generation_id is not None and old.get("generation_id") != expected_owner_generation_id:
            continue
        locator = old.get("output_artifact")
        outputs = old.get("output_digests")
        if not isinstance(locator, dict) or not isinstance(outputs, list):
            continue
        try:
            if parse_timestamp(str(locator.get("expires_at"))) <= now:
                continue
        except ValueError:
            continue
        manifest = locator.get("bundle_manifest_sha256")
        if not isinstance(manifest, str) or sha256_bytes(canonical_json(outputs)) != manifest:
            continue
        digest = next((row.get("sha256") for row in outputs if isinstance(row, dict) and row.get("path") == path.name), None)
        if isinstance(digest, str) and digest == file_sha256(path):
            owner, evidence_digest = old, digest
            break
    if owner is None or evidence_digest is None:
        raise ValueError("resume_artifact_unbound")
    evidence = load_json(path, maximum_bytes=MAX_INPUT_BYTES)
    expected_top = {
        "schema_version", "original_candidate_sha256", "provider_index_sha256",
        "adapter_revision", "extractor_revision", "records",
    }
    if (
        not isinstance(evidence, dict)
        or frozenset(evidence) not in {frozenset(expected_top), frozenset(expected_top | {"worker_outcomes"})}
        or evidence.get("schema_version") != ENRICHMENT_SCHEMA
        or evidence.get("original_candidate_sha256") != owner.get("generation_inputs", {}).get("candidate_sha256")
        or evidence.get("provider_index_sha256") != owner.get("generation_inputs", {}).get("adapter_revision")
        or not isinstance(evidence.get("records"), list)
    ):
        raise ValueError("resume_enrichment_binding_mismatch")
    owner_detail_by_id: dict[str, dict[str, Any]] = {}
    for owner_detail in owner.get("detail_records", []):
        if not isinstance(owner_detail, dict):
            continue
        owner_identity = str(owner_detail.get("id") or "")
        if owner_identity:
            if owner_identity in owner_detail_by_id:
                raise ValueError("resume_checkpoint_detail_identity_invalid")
            owner_detail_by_id[owner_identity] = owner_detail
    owner_contract_failure_ids = {
        identity for identity, detail in owner_detail_by_id.items()
        if isinstance(detail.get("failure_diagnostic"), dict)
        and "contract_failure" in detail["failure_diagnostic"]
    }
    locally_materialized_declarations: dict[str, dict[str, Any]] = {}
    outcome_ids: set[str] = set()
    if "worker_outcomes" in evidence:
        outcomes = evidence["worker_outcomes"]
        if not isinstance(outcomes, list):
            raise ValueError("resume_worker_outcomes_invalid")
        successful_ids = {
            str(record.get("api_key", {}).get("id") or "")
            for record in evidence["records"] if isinstance(record, dict) and isinstance(record.get("api_key"), dict)
        }
        for outcome in outcomes:
            if (
                not isinstance(outcome, dict)
                or set(outcome) not in (
                    {"api_key", "status", "source_sha256", "guide_sha256"},
                    {"api_key", "status", "source_sha256", "guide_sha256", "failure_diagnostic"},
                    {"api_key", "status", "source_sha256", "guide_sha256", "link_metadata"},
                    {"api_key", "status", "source_sha256", "guide_sha256", "failure_diagnostic", "link_metadata"},
                )
                or not isinstance(outcome.get("api_key"), dict)
                or outcome["api_key"].get("provider") != "data.go.kr"
                or outcome.get("status") not in {"retry", "quarantined"}
                or not re.fullmatch(r"[a-f0-9]{64}", str(outcome.get("source_sha256") or ""))
                or (outcome.get("guide_sha256") is not None and not re.fullmatch(r"[a-f0-9]{64}", str(outcome.get("guide_sha256"))))
            ):
                raise ValueError("resume_worker_outcome_invalid")
            identity = str(outcome["api_key"].get("id") or "")
            if "failure_diagnostic" in outcome:
                try:
                    diagnostic = validate_failure_diagnostic(outcome["failure_diagnostic"], identity=identity)
                except ValueError as exc:
                    raise ValueError("resume_worker_outcome_invalid") from exc
            else:
                diagnostic = None
            if "link_metadata" in outcome:
                try:
                    DETAIL_HELPERS.validate_link_metadata(
                        outcome["link_metadata"], str(outcome["api_key"].get("id") or ""), registered_hosts,
                    )
                except (ValueError, TypeError) as exc:
                    raise ValueError("resume_worker_link_metadata_invalid") from exc
                if (
                    outcome.get("status") != "quarantined"
                    or not isinstance(diagnostic, dict)
                    or diagnostic.get("code") != "resolved_link_operation_contract_unproven"
                    or diagnostic.get("phase") != "resolver"
                ):
                    raise ValueError("resume_worker_link_metadata_binding_invalid")
            elif (
                isinstance(diagnostic, dict)
                and diagnostic.get("code") == "resolved_link_operation_contract_unproven"
            ):
                raise ValueError("resume_worker_link_metadata_missing")
            if not identity or identity in outcome_ids or identity in successful_ids:
                raise ValueError("resume_worker_outcome_identity_invalid")
            row = candidate_by_id.get(identity)
            if (
                row is None or not row_is_link(row)
                or outcome["source_sha256"] != source_fingerprint(row)
                or outcome["guide_sha256"] != guide_fingerprint(row)
            ):
                raise ValueError("resume_worker_outcome_binding_mismatch")
            outcome_ids.add(identity)
            owner_detail = owner_detail_by_id.get(identity)
            owner_diagnostic = owner_detail.get("failure_diagnostic") if owner_detail else None
            if (
                isinstance(owner_diagnostic, dict) and "contract_failure" in owner_diagnostic
                or isinstance(diagnostic, dict) and "contract_failure" in diagnostic
            ):
                owner_has_diagnostic = isinstance(owner_detail, dict) and "failure_diagnostic" in owner_detail
                outcome_has_diagnostic = isinstance(outcome, dict) and "failure_diagnostic" in outcome
                owner_has_metadata = isinstance(owner_detail, dict) and "link_metadata" in owner_detail
                outcome_has_metadata = isinstance(outcome, dict) and "link_metadata" in outcome
                if (
                    not isinstance(owner_detail, dict)
                    or owner_detail.get("status") != outcome.get("status")
                    or owner_detail.get("source_sha256") != outcome.get("source_sha256")
                    or owner_detail.get("guide_sha256") != outcome.get("guide_sha256")
                    or owner_has_diagnostic != outcome_has_diagnostic
                    or owner_has_diagnostic and owner_detail.get("failure_diagnostic") != outcome.get("failure_diagnostic")
                    or owner_has_metadata != outcome_has_metadata
                    or owner_has_metadata and owner_detail.get("link_metadata") != outcome.get("link_metadata")
                ):
                    raise ValueError("resume_worker_contract_failure_binding_mismatch")
            if (
                isinstance(diagnostic, dict)
                and diagnostic.get("code") == "resolved_link_operation_contract_unproven"
                and isinstance(outcome.get("link_metadata"), dict)
            ):
                materialized = materialize_cached_seoul_declaration(
                    row, outcome, now=now, registered_hosts=registered_hosts,
                )
                if materialized is not None:
                    locally_materialized_declarations[identity] = materialized
    if owner_contract_failure_ids - outcome_ids:
        raise ValueError("resume_worker_contract_failure_outcome_missing")
    owner_inputs = owner.get("generation_inputs", {}) if isinstance(owner, dict) else {}
    expected_extractor_revision = (
        owner_inputs.get("extractor_revision") if allow_parent_extractor_revision
        else extractor_revision()
    )
    if (
        evidence.get("provider_index_sha256") != provider_index_sha256
        or evidence.get("adapter_revision") != provider_index_sha256
        or evidence.get("extractor_revision") != expected_extractor_revision
        or owner_inputs.get("candidate_sha256") is None
    ):
        return {}
    expected_record_fields = {
        "api_key", "source_sha256", "guide_sha256", "observed_guide_url",
        "observed_guide_url_sha256", "operations", "operations_sha256", "status", "source_provenance",
    }
    resumed: dict[str, dict[str, Any]] = {}
    for record in evidence["records"]:
        if (
            not isinstance(record, dict)
            or set(record) not in (expected_record_fields, expected_record_fields | {"declaration_provenance"})
            or record.get("status") != "enriched"
        ):
            raise ValueError("resume_enrichment_record_invalid")
        api_key = record.get("api_key")
        if not isinstance(api_key, dict) or api_key.get("provider") != "data.go.kr":
            raise ValueError("resume_enrichment_identity_invalid")
        identity = str(api_key.get("id") or "")
        if identity in resumed or identity not in candidate_by_id or not row_is_link(candidate_by_id[identity]):
            raise ValueError("resume_enrichment_identity_mismatch")
        row = candidate_by_id[identity]
        expected_url = safe_public_page_url(identity)
        provenance = record.get("source_provenance")
        operations = record.get("operations")
        observed_guide = record.get("observed_guide_url")
        observed_guide_digest = record.get("observed_guide_url_sha256")
        if record.get("source_sha256") != source_fingerprint(row) or record.get("guide_sha256") != guide_fingerprint(row):
            if reject_stale_source_or_guide_contract:
                raise ValueError("resume_enrichment_source_or_guide_identity_mismatch")
            continue
        if (
            not isinstance(provenance, dict)
            or set(provenance) != {"system", "page_url", "effective_url", "page_sha256", "observed_at"}
            or provenance.get("system") != "data.go.kr"
            or provenance.get("page_url") != expected_url
            or provenance.get("effective_url") != expected_url
            or not re.fullmatch(r"[a-f0-9]{64}", str(provenance.get("page_sha256") or ""))
            or not isinstance(operations, list) or not operations
            or record.get("operations_sha256") != sha256_bytes(canonical_json(operations))
        ):
            raise ValueError("resume_enrichment_source_binding_mismatch")
        observed_at = parse_timestamp(str(provenance.get("observed_at")))
        if observed_at > now + dt.timedelta(minutes=5):
            raise ValueError("resume_enrichment_observation_in_future")
        if now - observed_at >= dt.timedelta(days=DETAIL_OBSERVATION_TTL_DAYS):
            continue
        if observed_guide is None:
            if observed_guide_digest is not None or guide_fingerprint(row) is not None:
                raise ValueError("resume_enrichment_guide_binding_mismatch")
        elif (
            not isinstance(observed_guide, str)
            or observed_guide_digest != sha256_bytes(observed_guide.encode("utf-8"))
        ):
            raise ValueError("resume_enrichment_guide_binding_mismatch")
        declaration_provenance = record.get("declaration_provenance")
        has_declared_operation = any(
            isinstance(operation, dict)
            and isinstance(operation.get("source"), dict)
            and isinstance(operation["source"].get("raw"), dict)
            and operation["source"]["raw"].get("operation_declaration_id") == SEOUL_DECLARATION.DECLARATION_ID
            for operation in operations
        )
        if declaration_provenance is not None or has_declared_operation:
            if declaration_provenance is None:
                raise ValueError("resume_seoul_declaration_provenance_missing")
            try:
                SEOUL_DECLARATION.validate_enriched_record(row, record)
                link_metadata = declaration_provenance.get("page_resolver")
                DETAIL_HELPERS.validate_link_metadata(link_metadata, identity, registered_hosts)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError("resume_seoul_declaration_binding_invalid") from exc
        historical_operations = row.get("operations", [])
        operations_to_validate = operations
        if declaration_provenance is not None:
            # A pinned declaration is additive: its three historical metadata
            # links retain their original catalogue provenance.  The exact
            # prefix was already verified by validate_enriched_record, so only
            # the new operation is bound to this observed page/resolver pair.
            operations_to_validate = operations[len(historical_operations):]
        for operation in operations_to_validate:
            source = operation.get("source") if isinstance(operation, dict) else None
            raw = source.get("raw") if isinstance(source, dict) else None
            endpoint = str(operation.get("endpoint") or "") if isinstance(operation, dict) else ""
            if (
                not isinstance(source, dict) or source.get("system") != "data.go.kr"
                or source.get("url") != expected_url or not isinstance(raw, dict)
                or not safe_operation(operation)
                or urllib.parse.urlsplit(endpoint).hostname.lower() not in registered_hosts
                or raw.get("operation_url") != endpoint
                or (observed_guide is None and "guide_url" in raw)
                or (observed_guide is not None and raw.get("guide_url") != observed_guide)
            ):
                raise ValueError("resume_enrichment_operation_binding_mismatch")
            if raw.get("operation_declaration_id") is not None:
                try:
                    SEOUL_DECLARATION.validate_declared_operation(row, operation)
                except (TypeError, ValueError) as exc:
                    raise ValueError("resume_seoul_declared_operation_invalid") from exc
        resumed[identity] = record
    for identity, record in locally_materialized_declarations.items():
        if identity not in resumed:
            resumed[identity] = record
    return resumed


def merge_derivation_enrichment_records(
    resume_records: dict[str, dict[str, Any]],
    canonical_records: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Union verified A-bound contributions without choosing between conflicts."""
    merged = dict(resume_records)
    for identity, candidate in canonical_records.items():
        prior = merged.get(identity)
        if prior is None:
            merged[identity] = candidate
            continue
        if (
            prior.get("source_sha256") != candidate.get("source_sha256")
            or prior.get("guide_sha256") != candidate.get("guide_sha256")
            or prior.get("operations_sha256") != candidate.get("operations_sha256")
            or prior.get("operations") != candidate.get("operations")
        ):
            raise ValueError("derivation_parent_enrichment_contribution_conflict")
        prior_time = parse_timestamp(str((prior.get("source_provenance") or {}).get("observed_at") or ""))
        candidate_time = parse_timestamp(str((candidate.get("source_provenance") or {}).get("observed_at") or ""))
        if candidate_time > prior_time:
            merged[identity] = candidate
    return merged


def set_output_artifact_locator(checkpoint: dict[str, Any], args: argparse.Namespace, expires_at: str, bundle: list[dict[str, Any]]) -> None:
    processor_run_id = args.processor_run_id or args.producer_run_id
    checkpoint["output_artifact"].update({
        "repository": args.repository,
        "run_id": args.processor_artifact_run_id or processor_run_id,
        "name": f"upstream-catalogue-processing-{processor_run_id}",
        "artifact_id": None,
        "expires_at": expires_at,
        "bundle_manifest_sha256": sha256_bytes(canonical_json(bundle)) if bundle else None,
    })
    checkpoint["output_digests"] = bundle


def persist_result_only_checkpoint(
    checkpoint: dict[str, Any],
    args: argparse.Namespace,
    *,
    checkpoint_path: pathlib.Path,
    index_path: pathlib.Path,
    output_expires_at: str,
) -> dict[str, Any]:
    """Publish a sealed checkpoint and its small, digest-bound result artifact."""
    retention_index = (
        load_json(index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
        if index_path.is_file() else {
            "schema_version": CHECKPOINT_SCHEMA, "generations": [],
            "detail_queue_cursor": 0, "detail_retry_state": {},
        }
    )
    plan_generation_retention(retention_index, checkpoint_path.parent, checkpoint)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result_path = atomic_output(
        args.output_dir,
        "upstream-catalogue-processing-result.json",
        processing_result(
            checkpoint,
            producer_run_id=args.producer_run_id,
            processor_run_id=args.processor_run_id or args.producer_run_id,
            processor_artifact_run_id=args.processor_artifact_run_id or args.processor_run_id or args.producer_run_id,
        ),
    )
    bundle = [{
        "path": result_path.name,
        "sha256": file_sha256(result_path),
        "bytes": result_path.stat().st_size,
    }]
    set_output_artifact_locator(checkpoint, args, output_expires_at, bundle)
    sealed = seal_checkpoint(checkpoint)
    atomic_write_json(checkpoint_path, sealed)
    atomic_output(args.output_dir, "upstream-catalogue-checkpoint-receipt.json", sealed)
    append_generation_index(index_path, checkpoint_path, sealed)
    return sealed


def recover_failed_processor_generation(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Seal a verified old processor scope failure without reprocessing inputs."""
    required_paths = (args.failed_run_metadata, args.failed_artifact_metadata, args.failed_artifact_dir)
    if any(path is None for path in required_paths):
        raise ValueError("failed_processor_recovery_evidence_required")
    if (
        not args.expected_checkpoint_sha256
        or not args.expected_state_head_sha
        or not args.recover_failed_processor_run_id
        or not args.processor_run_id
        or not args.processor_artifact_run_id
        or not args.output_artifact_expires_at
        or not args.current_head_sha
    ):
        raise ValueError("failed_processor_recovery_identity_required")
    if not re.fullmatch(r"[a-f0-9]{64}", args.expected_checkpoint_sha256):
        raise ValueError("failed_processor_expected_checkpoint_digest_invalid")
    if not re.fullmatch(r"[a-f0-9]{40}", args.expected_state_head_sha) or not re.fullmatch(r"[a-f0-9]{40}", args.current_head_sha):
        raise ValueError("failed_processor_recovery_git_identity_invalid")
    processor_parts = str(args.processor_run_id).rsplit("-", 1)
    if len(processor_parts) != 2 or processor_parts[0] != str(args.processor_artifact_run_id) or not processor_parts[1].isdigit():
        raise ValueError("failed_processor_recovery_current_run_invalid")
    run_attempt = int(processor_parts[1])
    artifact_dir = args.failed_artifact_dir
    if not artifact_dir.is_dir() or artifact_dir.is_symlink():
        raise ValueError("failed_processor_artifact_directory_invalid")
    required_members = (
        "upstream-catalogue-checkpoint-receipt.json",
        "upstream-catalogue-processing-result.json",
    )
    members: dict[str, bytes] = {}
    for name in required_members:
        path = artifact_dir / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("failed_processor_artifact_member_invalid")
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("failed_processor_artifact_too_large")
        members[name] = path.read_bytes()
    state_index_path = args.state_dir / "sources" / args.source / "index.json"
    checkpoint_path = args.state_dir / "sources" / args.source / "generations" / f"{args.recover_failed_generation_id}.json"
    index = load_json(state_index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
    schema = load_json(args.checkpoint_schema, maximum_bytes=1024 * 1024)
    checkpoint = verify_checkpoint(load_json(checkpoint_path, maximum_bytes=STATE_FILE_LIMIT), schema)
    failed_run_metadata = load_json(args.failed_run_metadata, maximum_bytes=1024 * 1024)
    failed_artifact_metadata = load_json(args.failed_artifact_metadata, maximum_bytes=1024 * 1024)
    if (
        not isinstance(failed_run_metadata, dict)
        or str(failed_run_metadata.get("id") or "") != str(args.recover_failed_processor_run_id)
        or checkpoint.get("generation_id") != args.recover_failed_generation_id
    ):
        raise ValueError("failed_processor_recovery_target_mismatch")
    now = utc_now() if not args.now else parse_timestamp(args.now)
    current_run = {
        "repository": args.repository,
        "processor_run_id": args.processor_run_id,
        "processor_artifact_run_id": args.processor_artifact_run_id,
        "run_attempt": run_attempt,
        "head_sha": args.current_head_sha,
        "artifact_name": f"upstream-catalogue-processing-{args.processor_run_id}",
        "expires_at": args.output_artifact_expires_at,
    }
    from recover_upstream_catalogue_failed_generation import recover_failed_generation
    plan = recover_failed_generation(
        durable_state={"state_branch_sha": args.expected_state_head_sha, "index": index},
        checkpoint=checkpoint,
        failed_run_metadata=failed_run_metadata,
        failed_artifact_metadata=failed_artifact_metadata,
        failed_artifact_dir_or_payload=members,
        expected_checkpoint_sha256=args.expected_checkpoint_sha256,
        current_run=current_run,
        now=now,
    )
    if plan.expected_state_head_sha != args.expected_state_head_sha:
        raise ValueError("failed_processor_recovery_state_head_mismatch")
    output_expiry = args.output_artifact_expires_at
    if parse_timestamp(output_expiry) <= now:
        raise ValueError("output_artifact_expired")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "composed-candidate.registry.json", "ready-scope.registry.json", "semantic-diff.json",
        "regeneration-queue.json", "quarantine.json", "composition-receipt.json",
        "upstream-catalogue-enrichment-evidence.json", "upstream-catalogue-processing-result.json",
        "upstream-catalogue-checkpoint-receipt.json",
    ):
        path = args.output_dir / name
        if path.is_symlink():
            raise ValueError("failed_processor_output_path_invalid")
        path.unlink(missing_ok=True)
    sealed = persist_result_only_checkpoint(
        plan.checkpoint,
        args,
        checkpoint_path=checkpoint_path,
        index_path=state_index_path,
        output_expires_at=output_expiry,
    )
    return 3, sealed


def call_composer(
    command: pathlib.Path,
    *, baseline: pathlib.Path, candidate: pathlib.Path, diff: pathlib.Path,
    refresh_evidence: pathlib.Path, provider_index: pathlib.Path, source_policy: pathlib.Path,
    producer_run_id: str, producer_run_url: str, output_dir: pathlib.Path, enrichment: pathlib.Path,
    composition_baseline: pathlib.Path | None = None,
    same_observation_derivation: pathlib.Path | None = None,
) -> tuple[int, str]:
    if not command.is_file():
        return 127, "composer_cli_missing"
    import subprocess
    args = [
        sys.executable, str(command), "--baseline", str(baseline), "--candidate", str(candidate),
        "--diff", str(diff), "--refresh-evidence", str(refresh_evidence),
        "--provider-index", str(provider_index), "--source-policy", str(source_policy),
        "--producer-run-id", producer_run_id, "--producer-run-url", producer_run_url,
        "--output-dir", str(output_dir), "--enrichment-evidence", str(enrichment),
    ]
    if composition_baseline is not None:
        args.extend(["--composition-baseline", str(composition_baseline)])
    if same_observation_derivation is not None:
        args.extend(["--same-observation-derivation", str(same_observation_derivation)])
    result = subprocess.run(args, text=True, capture_output=True, check=False, timeout=300)  # noqa: S603
    if result.returncode != 0:
        return result.returncode, "composer_failed"
    return 0, "composer_succeeded"


def _git_bytes(root: pathlib.Path, revision_path: str) -> bytes:
    import subprocess

    result = subprocess.run(
        ("git", "show", revision_path), cwd=root, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        raise ValueError("derivation_main_revision_unavailable")
    return result.stdout


def _assert_git_ancestor(root: pathlib.Path, ancestor: str, descendant: str) -> None:
    import subprocess

    result = subprocess.run(
        ("git", "merge-base", "--is-ancestor", ancestor, descendant), cwd=root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode != 0:
        raise ValueError("derivation_canonical_merge_not_ancestor")


def validate_derivation_before_claim(
    args: argparse.Namespace,
    *,
    generation_id: str,
    derivation: Mapping[str, Any],
    composition_baseline_path: pathlib.Path,
    collector_admission: Mapping[str, Any] | None,
    state_index_path: pathlib.Path,
    schema: Mapping[str, Any],
    original_baseline_sha256: str,
    candidate_sha256: str,
    evidence_sha256: str,
    diff_sha256: str,
    policy_sha256: str,
    adapter_sha256: str,
    current_main_root: pathlib.Path,
    resume_parent_bundle_dir: pathlib.Path,
    canonical_parent_bundle_dir: pathlib.Path,
    allow_source_only_main_advance: bool = False,
    allow_monotonic_journal_successor: bool = False,
) -> dict[str, Any]:
    """Authenticate both B parents and the exact merged-C snapshot before claim."""
    import subprocess

    envelope = DERIVATION.validate_derivation_envelope(derivation)
    if args.execution_mode != "live" or collector_admission is None:
        raise ValueError("same_observation_derivation_requires_live_admission")
    if (
        args.resume_parent_bundle_dir is None or args.canonical_parent_bundle_dir is None
        or args.derivation_journal is None or args.derivation_journal_ref_sha is None
    ):
        raise ValueError("same_observation_derivation_parent_archive_or_journal_missing")
    if not state_index_path.is_file():
        raise ValueError("derivation_state_index_missing")
    state_index = load_json(state_index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
    if not isinstance(state_index, dict) or state_index.get("schema_version") != CHECKPOINT_SCHEMA:
        raise ValueError("derivation_state_index_invalid")
    ledger = state_index.get("collector_handoff")
    if not isinstance(ledger, Mapping):
        raise ValueError("derivation_admission_ledger_missing")
    try:
        validated_ledger = validate_ledger(ledger)
    except HandoffError as exc:
        raise ValueError("derivation_admission_ledger_invalid") from exc
    admitted_rows = validated_ledger["admitted_observations"]
    original = envelope["original_observation"]
    def admission_for(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        observed = checkpoint.get("last_observation") if isinstance(checkpoint.get("last_observation"), Mapping) else {}
        refs = checkpoint.get("input_artifacts") if isinstance(checkpoint.get("input_artifacts"), list) else []
        matches = [
            row for row in admitted_rows
            if row.get("producer_run_id") == str(observed.get("producer_run_id") or "")
            and row.get("refresh_evidence_sha256") == observed.get("refresh_evidence_sha256")
            and any(
                isinstance(ref, Mapping)
                and str(ref.get("run_id")) == row["producer_run_id"]
                and str(ref.get("artifact_id")) == row["artifact_id"]
                and ref.get("evidence_sha256") == row["refresh_evidence_sha256"]
                for ref in refs
            )
        ]
        if len(matches) != 1:
            raise ValueError("derivation_parent_admission_ambiguous_or_missing")
        return matches[0]

    if (
        str(collector_admission.get("producer_run_id")) != original["producer_run_id"]
        or collector_admission.get("run_attempt") != original["producer_run_attempt"]
        or collector_admission.get("head_sha") != original["producer_head_sha"]
        or str(collector_admission.get("artifact_id")) != original["producer_artifact_id"]
        or collector_admission.get("artifact_digest_sha256") != original["producer_artifact_sha256"]
        or collector_admission.get("observed_at") != original["observed_at"]
        or collector_admission.get("candidate_sha256") != candidate_sha256
        or collector_admission.get("refresh_evidence_sha256") != evidence_sha256
        or candidate_sha256 != original["candidate_sha256"]
        or evidence_sha256 != original["evidence_sha256"]
        or diff_sha256 != original["diff_sha256"]
        or original_baseline_sha256 != original["original_baseline_sha256"]
        or args.source != original["source_id"]
        or args.source_scope != original["source_scope"]
        or derivation_processor_revision() != envelope["derivation_processor_revision_sha256"]
    ):
        raise ValueError("derivation_incoming_observation_or_contract_mismatch")

    parents: dict[str, dict[str, Any]] = {}
    for field in ("resume_parent_processor", "canonical_parent_processor"):
        ref = envelope[field]
        cp_path = args.state_dir / "sources" / args.source / "generations" / f"{ref['generation_id']}.json"
        if not cp_path.is_file():
            raise ValueError("derivation_parent_checkpoint_unavailable")
        try:
            cp = verify_checkpoint(load_json(cp_path, maximum_bytes=STATE_FILE_LIMIT), dict(schema))
            DERIVATION.validate_processor_parent_checkpoint(ref, cp, original_observation=original)
            parent_admission = admission_for(cp)
            parent_subject = DERIVATION.original_observation_from_checkpoint(cp, parent_admission)
        except Exception as exc:
            raise ValueError("derivation_parent_checkpoint_invalid") from exc
        if parent_subject != original:
            raise ValueError("derivation_parent_original_observation_mismatch")
        if parse_timestamp(str(ref["artifact_expires_at"])) <= utc_now():
            raise ValueError("derivation_parent_artifact_expired")
        parents[field] = cp
    try:
        DERIVATION.validate_processor_parent_bundle(
            envelope["resume_parent_processor"], parents["resume_parent_processor"],
            resume_parent_bundle_dir,
        )
        DERIVATION.validate_processor_parent_bundle(
            envelope["canonical_parent_processor"], parents["canonical_parent_processor"],
            canonical_parent_bundle_dir,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("derivation_parent_archived_bundle_invalid") from exc
    if envelope["resume_parent_processor"]["generation_id"] == envelope["canonical_parent_processor"]["generation_id"]:
        if parents["resume_parent_processor"] != parents["canonical_parent_processor"]:
            raise ValueError("derivation_parent_checkpoint_conflict")

    indexed = {
        row.get("generation_id") for row in state_index.get("generations", [])
        if isinstance(row, Mapping)
    }
    def load_lineage_checkpoint(generation: str) -> dict[str, Any]:
        if generation not in indexed:
            raise ValueError("derivation_parent_graph_checkpoint_unavailable")
        path = args.state_dir / "sources" / args.source / "generations" / f"{generation}.json"
        if not path.is_file() or path.is_symlink():
            raise ValueError("derivation_parent_graph_checkpoint_unavailable")
        return verify_checkpoint(load_json(path, maximum_bytes=STATE_FILE_LIMIT), dict(schema))

    try:
        expected_ancestors = DERIVATION.validate_processor_parent_graph(
            [
                envelope["resume_parent_processor"]["generation_id"],
                envelope["canonical_parent_processor"]["generation_id"],
            ],
            load_checkpoint=load_lineage_checkpoint,
            admission_for=admission_for,
            original_observation=original,
            expected_ancestor_generation_ids=envelope["ancestor_generation_ids"],
            forbidden_generation_id=generation_id,
        )
    except (DERIVATION.DerivationError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("derivation_parent_graph_checkpoint_invalid") from exc

    journal_path = args.derivation_journal
    journal_ref_sha = args.derivation_journal_ref_sha
    if journal_path is None or not journal_path.is_file() or not journal_ref_sha:
        raise ValueError("derivation_promotion_journal_snapshot_missing")
    journal = load_json(journal_path, maximum_bytes=16 * 1024 * 1024)
    if not isinstance(journal, Mapping):
        raise ValueError("derivation_promotion_journal_invalid")
    if not re.fullmatch(r"[a-f0-9]{40}", str(journal_ref_sha)):
        raise ValueError("derivation_promotion_journal_ref_invalid")
    live_journal_ref = subprocess.run(
        ("git", "ls-remote", "--heads", "origin", "refs/heads/automation/canonical-update-state"),
        cwd=current_main_root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    live_journal_shas = [
        line.split("\t", 1)[0] for line in live_journal_ref.stdout.splitlines()
        if line.endswith("\trefs/heads/automation/canonical-update-state")
    ]
    if live_journal_ref.returncode != 0 or live_journal_shas != [journal_ref_sha]:
        raise ValueError("derivation_promotion_journal_ref_changed")
    spec = importlib.util.spec_from_file_location(
        "upstream_catalogue_derivation_canonical_update_pr", args.canonical_update_pr_helper,
    )
    if spec is None or spec.loader is None:
        raise ValueError("derivation_promotion_validator_unavailable")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.validate_journal(journal, load_json(args.canonical_update_journal_schema, maximum_bytes=1024 * 1024))
    try:
        row = DERIVATION.validate_readback_against_journal(
            envelope, journal, journal_ref_sha=journal_ref_sha,
            allow_monotonic_successor=allow_monotonic_journal_successor,
        )
    except DERIVATION.DerivationError as exc:
        raise ValueError("derivation_canonical_readback_invalid") from exc
    try:
        health_policy = load_json(current_main_root / "policy/upstream-catalogue-health.json", maximum_bytes=4 * 1024 * 1024)
        DERIVATION.authenticate_canonical_merge_ack(
            current_main_root, args.repository, row, health_policy, now=utc_now(),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("derivation_canonical_merge_ack_untrusted") from exc
    active_same_a = []
    for index, record in enumerate(journal.get("records", [])):
        if not isinstance(record, Mapping):
            continue
        candidate = record.get("candidate") if isinstance(record.get("candidate"), Mapping) else {}
        if (
            record.get("superseded_by") is not None
            or record.get("status") not in {"merged", "publication-pending", "published", "read-back-confirmed"}
            or candidate.get("source_id") != original["source_id"]
            or candidate.get("scope") != original["source_scope"]
            or candidate.get("registry_sha256") != envelope["composition_baseline"]["registry_sha256"]
        ):
            continue
        generation = str(candidate.get("generation_id") or "")
        cp_path = args.state_dir / "sources" / args.source / "generations" / f"{generation}.json"
        if not cp_path.is_file():
            continue
        try:
            cp = verify_checkpoint(load_json(cp_path, maximum_bytes=STATE_FILE_LIMIT), dict(schema))
            admission = admission_for(cp)
            if DERIVATION.original_observation_from_checkpoint(cp, admission) == original:
                active_same_a.append(index)
        except Exception:
            continue
    if active_same_a != [int(envelope["canonical_parent_readback"]["journal_record_index"])]:
        raise ValueError("derivation_canonical_lineage_is_ambiguous_or_not_current")
    if row.get("candidate", {}).get("generation_id") != envelope["canonical_parent_processor"]["generation_id"]:
        raise ValueError("derivation_canonical_producer_generation_mismatch")

    baseline_identity = envelope["composition_baseline"]
    current_head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=current_main_root, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if current_head.returncode != 0 or not re.fullmatch(r"[a-f0-9]{40}", current_head.stdout.strip()):
        raise ValueError("derivation_current_main_head_mismatch")
    current_main_sha = current_head.stdout.strip()
    if not allow_source_only_main_advance and current_main_sha != baseline_identity["main_sha"]:
        raise ValueError("derivation_current_main_head_mismatch")
    live_main = subprocess.run(
        ("git", "ls-remote", "--heads", "origin", "refs/heads/main"), cwd=current_main_root,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    live_main_shas = [line.split("\t", 1)[0] for line in live_main.stdout.splitlines() if line.endswith("\trefs/heads/main")]
    if live_main.returncode != 0 or live_main_shas != [current_main_sha]:
        raise ValueError("derivation_current_main_moved")

    def manifest_registry_identity(revision: str, *, expected_manifest_sha: str | None = None) -> tuple[str, int, str]:
        manifest_bytes = _git_bytes(current_main_root, f"{revision}:manifest.json")
        if expected_manifest_sha is not None and sha256_bytes(manifest_bytes) != expected_manifest_sha:
            raise ValueError("derivation_composition_baseline_manifest_mismatch")
        try:
            manifest = json.loads(manifest_bytes)
        except json.JSONDecodeError as exc:
            raise ValueError("derivation_current_manifest_invalid") from exc
        if not isinstance(manifest, Mapping) or manifest.get("source_registry") != baseline_identity["registry_path"]:
            raise ValueError("derivation_current_manifest_path_mismatch")
        entries = [
            item for item in manifest.get("artifacts", []) if isinstance(item, Mapping)
            and item.get("path") == baseline_identity["registry_path"] and item.get("kind") == "registry"
        ]
        if len(entries) != 1:
            raise ValueError("derivation_current_manifest_registry_mismatch")
        entry = entries[0]
        if not isinstance(entry.get("bytes"), int) or isinstance(entry.get("bytes"), bool) or not isinstance(entry.get("sha256"), str):
            raise ValueError("derivation_current_manifest_registry_mismatch")
        pointer = _git_bytes(current_main_root, f"{revision}:{baseline_identity['registry_path']}")
        try:
            pointer_text = pointer.decode("ascii", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError("derivation_current_lfs_pointer_mismatch") from exc
        if (
            f"oid sha256:{entry['sha256']}" not in pointer_text
            or f"size {entry['bytes']}" not in pointer_text
        ):
            raise ValueError("derivation_current_lfs_pointer_mismatch")
        return str(entry["sha256"]), int(entry["bytes"]), sha256_bytes(manifest_bytes)

    baseline_sha, baseline_bytes, _baseline_manifest_sha = manifest_registry_identity(
        baseline_identity["main_sha"], expected_manifest_sha=baseline_identity["manifest_sha256"],
    )
    if (baseline_sha, baseline_bytes) != (
        baseline_identity["registry_sha256"], baseline_identity["registry_bytes"],
    ):
        raise ValueError("derivation_composition_baseline_manifest_registry_mismatch")
    current_sha, current_bytes, _current_manifest_sha = manifest_registry_identity(current_main_sha)
    if (current_sha, current_bytes) != (
        baseline_identity["registry_sha256"], baseline_identity["registry_bytes"],
    ):
        raise ValueError("derivation_current_manifest_registry_mismatch")
    _assert_git_ancestor(
        current_main_root, envelope["canonical_parent_readback"]["merge_sha"], baseline_identity["main_sha"],
    )
    _assert_git_ancestor(current_main_root, baseline_identity["main_sha"], current_main_sha)
    _assert_git_ancestor(current_main_root, envelope["canonical_parent_readback"]["merge_sha"], current_main_sha)
    if composition_baseline_path.is_symlink() or not composition_baseline_path.is_file():
        raise ValueError("derivation_composition_baseline_missing")
    composition_bytes = composition_baseline_path.read_bytes()
    if (len(composition_bytes), sha256_bytes(composition_bytes)) != (
        baseline_identity["registry_bytes"], baseline_identity["registry_sha256"],
    ):
        raise ValueError("derivation_composition_baseline_digest_mismatch")
    return envelope


def append_generation_index(
    index_path: pathlib.Path, checkpoint_path: pathlib.Path, checkpoint: dict[str, Any], *,
    collector_admission: dict[str, Any] | None = None,
    legacy_floor: dict[str, Any] | None = None,
    admitted_at: str | None = None,
) -> set[str]:
    index = {"schema_version": CHECKPOINT_SCHEMA, "generations": [], "detail_queue_cursor": 0, "detail_retry_state": {}}
    if index_path.exists():
        loaded = load_json(index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
        if not isinstance(loaded, dict) or loaded.get("schema_version") != CHECKPOINT_SCHEMA or not isinstance(loaded.get("generations"), list):
            raise ValueError("corrupt_generation_index")
        index = loaded
    rows = [row for row in index["generations"] if isinstance(row, dict) and row.get("generation_id") != checkpoint["generation_id"]]
    rows.append({
        "generation_id": checkpoint["generation_id"], "status": checkpoint["status"],
        "checkpoint": checkpoint_path.name, "updated_at": checkpoint["last_heartbeat_at"],
        "candidate_sha256": checkpoint["generation_inputs"].get("candidate_sha256"),
    })
    index["generations"] = rows
    retained_ids = plan_generation_retention(index, checkpoint_path.parent, checkpoint)
    index["generations"] = [row for row in rows if row.get("generation_id") in retained_ids]
    index["detail_queue_cursor"] = int(checkpoint.get("detail_queue_cursor", index.get("detail_queue_cursor", 0)))
    retry_state = index.setdefault("detail_retry_state", {})
    if not isinstance(retry_state, dict):
        raise ValueError("corrupt_detail_retry_state")
    for identity in checkpoint.get("detail_retry_reset_ids", []):
        retry_state.pop(str(identity), None)
    reservation = checkpoint.get("request_reservation")
    if isinstance(reservation, dict):
        for record in reservation.get("records", []):
            if not isinstance(record, dict):
                continue
            identity = str(record.get("id") or "")
            attempts = int(checkpoint.get("attempts_by_id", {}).get(identity, 0))
            if attempts:
                retry_entry = {
                    "source_sha256": record["source_sha256"],
                    "guide_sha256": record["guide_sha256"],
                    "attempts": attempts,
                    "last_attempt_at": reservation["reserved_at"],
                }
                prior = retry_state.get(identity)
                if (
                    isinstance(prior, dict)
                    and prior.get("source_sha256") == retry_entry["source_sha256"]
                    and prior.get("guide_sha256") == retry_entry["guide_sha256"]
                    and "failure_diagnostic" in prior
                ):
                    retry_entry["failure_diagnostic"] = validate_failure_diagnostic(
                        prior["failure_diagnostic"], identity=identity,
                    )
                retry_state[identity] = retry_entry
    for record in checkpoint.get("detail_records", []):
        if not isinstance(record, dict):
            continue
        identity = str(record.get("id") or "")
        if record.get("status") == "enriched":
            retry_state.pop(identity, None)
            continue
        if "failure_diagnostic" in record:
            current = retry_state.get(identity)
            if (
                isinstance(current, dict)
                and current.get("source_sha256") == record.get("source_sha256")
                and current.get("guide_sha256") == record.get("guide_sha256")
            ):
                current["failure_diagnostic"] = validate_failure_diagnostic(
                    record["failure_diagnostic"], identity=identity,
                )
    if len(retry_state) > DEFAULT_MAX_RETRY_STATES:
        raise ValueError("detail_retry_state_capacity_exceeded")
    if collector_admission is not None:
        try:
            add_admission(index, collector_admission, now=admitted_at or checkpoint["last_heartbeat_at"], legacy_floor=legacy_floor)
        except HandoffError as exc:
            raise ValueError(str(exc)) from exc
    atomic_write_json(index_path, index)
    return retained_ids


def plan_generation_retention(
    index: Mapping[str, Any], generation_dir: pathlib.Path,
    prospective_checkpoint: Mapping[str, Any], *, max_generations: int = DEFAULT_MAX_GENERATIONS,
) -> set[str]:
    """Plan one validated index/file keep-set without mutating either store."""
    rows_value = index.get("generations")
    if not isinstance(rows_value, list):
        raise ValueError("corrupt_generation_index")
    current_id = prospective_checkpoint.get("generation_id")
    if not isinstance(current_id, str) or not re.fullmatch(r"[a-f0-9]{64}", current_id):
        raise ValueError("generation_retention_current_identity_invalid")
    rows: list[dict[str, Any]] = []
    seen_rows: set[str] = set()
    for raw in rows_value:
        if not isinstance(raw, Mapping):
            raise ValueError("corrupt_generation_index")
        generation_id = raw.get("generation_id")
        if not isinstance(generation_id, str) or not re.fullmatch(r"[a-f0-9]{64}", generation_id):
            raise ValueError("corrupt_generation_index")
        if generation_id in seen_rows:
            raise ValueError("duplicate_generation_index_identity")
        seen_rows.add(generation_id)
        rows.append(dict(raw))
    rows = [row for row in rows if row["generation_id"] != current_id]
    rows.append({
        "generation_id": current_id,
        "status": prospective_checkpoint.get("status"),
        "checkpoint": f"{current_id}.json",
        "updated_at": prospective_checkpoint.get("last_heartbeat_at"),
        "candidate_sha256": (
            prospective_checkpoint.get("generation_inputs", {}).get("candidate_sha256")
            if isinstance(prospective_checkpoint.get("generation_inputs"), Mapping) else None
        ),
    })
    index_ids = {row["generation_id"] for row in rows}

    schema_path = pathlib.Path(__file__).resolve().parent.parent / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
    schema = load_json(schema_path, maximum_bytes=1024 * 1024)
    checkpoints: dict[str, dict[str, Any]] = {}
    for path in generation_dir.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            raise ValueError("derivation_lineage_checkpoint_file_invalid")
        generation_id = path.stem
        if not re.fullmatch(r"[a-f0-9]{64}", generation_id):
            raise ValueError("derivation_lineage_checkpoint_filename_invalid")
        try:
            checkpoint = verify_checkpoint(load_json(path, maximum_bytes=STATE_FILE_LIMIT), schema)
        except Exception as exc:
            raise ValueError("derivation_lineage_checkpoint_corrupt") from exc
        if checkpoint.get("generation_id") != generation_id:
            raise ValueError("derivation_lineage_checkpoint_identity_mismatch")
        checkpoints[generation_id] = checkpoint

    prospective = dict(prospective_checkpoint)
    prospective.pop("checkpoint_sha256", None)
    prospective = seal_checkpoint(prospective)
    try:
        verify_checkpoint(prospective, schema)
    except Exception as exc:
        raise ValueError("generation_retention_prospective_checkpoint_invalid") from exc
    checkpoints[current_id] = prospective

    protected: set[str] = set()
    derived_ids: list[str] = []
    ancestors_by_generation: dict[str, set[str]] = {}
    parents_by_generation: dict[str, tuple[str, str]] = {}
    for generation_id, checkpoint in checkpoints.items():
        inputs = checkpoint.get("generation_inputs")
        envelope_value = inputs.get("same_observation_derivation") if isinstance(inputs, Mapping) else None
        if envelope_value is None:
            continue
        try:
            envelope = DERIVATION.validate_derivation_envelope(envelope_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("derivation_lineage_envelope_corrupt") from exc
        if generation_id in envelope["ancestor_generation_ids"]:
            raise ValueError("derivation_lineage_checkpoint_cycle")
        ancestors = set(envelope["ancestor_generation_ids"])
        protected.update(ancestors)
        ancestors_by_generation[generation_id] = ancestors
        parents_by_generation[generation_id] = (
            envelope["resume_parent_processor"]["generation_id"],
            envelope["canonical_parent_processor"]["generation_id"],
        )
        derived_ids.append(generation_id)

    handoff_ledger = index.get("collector_handoff")
    admitted_rows: list[dict[str, Any]] = []
    if handoff_ledger is not None:
        try:
            admitted_rows = validate_ledger(handoff_ledger)["admitted_observations"]
        except HandoffError as exc:
            raise ValueError("derivation_lineage_admission_ledger_invalid") from exc

    def admission_for(checkpoint: Mapping[str, Any]) -> Mapping[str, Any]:
        observation = checkpoint.get("last_observation")
        refs = checkpoint.get("input_artifacts")
        if not isinstance(observation, Mapping) or not isinstance(refs, list):
            raise ValueError("derivation_lineage_admission_identity_missing")
        matches = [
            row for row in admitted_rows
            if str(row.get("producer_run_id")) == str(observation.get("producer_run_id") or "")
            and row.get("refresh_evidence_sha256") == observation.get("refresh_evidence_sha256")
            and any(
                isinstance(ref, Mapping)
                and str(ref.get("run_id")) == str(row.get("producer_run_id"))
                and str(ref.get("artifact_id")) == str(row.get("artifact_id"))
                and ref.get("evidence_sha256") == row.get("refresh_evidence_sha256")
                for ref in refs
            )
        ]
        if len(matches) != 1:
            raise ValueError("derivation_lineage_admission_ambiguous_or_missing")
        return matches[0]

    if derived_ids:
        if not admitted_rows:
            raise ValueError("derivation_lineage_admission_ledger_missing")
        for generation_id in derived_ids:
            checkpoint = checkpoints[generation_id]
            envelope = DERIVATION.validate_derivation_envelope(
                checkpoint["generation_inputs"]["same_observation_derivation"],
            )
            try:
                DERIVATION.validate_processor_parent_graph(
                    [
                        envelope["resume_parent_processor"]["generation_id"],
                        envelope["canonical_parent_processor"]["generation_id"],
                    ],
                    load_checkpoint=lambda parent_id: checkpoints[parent_id],
                    admission_for=admission_for,
                    original_observation=envelope["original_observation"],
                    expected_ancestor_generation_ids=envelope["ancestor_generation_ids"],
                    forbidden_generation_id=generation_id,
                )
            except (KeyError, DERIVATION.DerivationError, OSError, ValueError) as exc:
                raise ValueError("derivation_lineage_parent_graph_invalid") from exc

    missing_files = protected - checkpoints.keys()
    if missing_files:
        raise ValueError("derivation_lineage_parent_checkpoint_unavailable")
    if not protected.issubset(index_ids):
        raise ValueError("derivation_parent_index_entry_missing")

    active_statuses = {"queued", "validating", "enriching", "composing", "retry"}
    active_ids: set[str] = set()
    shadowed_ready_ancestors: set[str] = set()
    for derived_id, ancestors in ancestors_by_generation.items():
        parents = parents_by_generation[derived_id]
        # Each authenticated C-lineage edge shadows the ready/retry ancestors
        # that the selector cannot independently promote. Preserve this
        # envelope's distinct resume parent when it differs from the already
        # merged canonical producer. Compute the exception per envelope before
        # unioning: a later descendant may shadow an older envelope's resume
        # parent, and must not have that shadow undone by a global subtraction.
        shadowed_by_derived_checkpoint = set(ancestors)
        if parents[0] != parents[1]:
            shadowed_by_derived_checkpoint.discard(parents[0])
        shadowed_ready_ancestors.update(shadowed_by_derived_checkpoint)
    for generation_id, checkpoint in checkpoints.items():
        outcome = checkpoint.get("outcome") if isinstance(checkpoint.get("outcome"), Mapping) else {}
        lease = checkpoint.get("lease")
        ready_retry = (
            checkpoint.get("status") == "ready"
            and int(outcome.get("detail_retry_count", 0) or 0) > 0
        )
        # A ready retry ancestor named by the current authenticated derivation
        # is retained as lineage evidence, but the C selector screens the
        # derived continuation against the exact current canonical row. Such
        # ancestors are not independent work slots once the continuation
        # shadows them. A distinct resume parent is left selectable above.
        # Keep any leased checkpoint active: a live lease must never be
        # discounted as shadowed work.
        shadowed_ready_ancestor = (
            ready_retry and generation_id in shadowed_ready_ancestors and lease is None
        )
        if not shadowed_ready_ancestor and (
            checkpoint.get("status") in active_statuses
            or ready_retry
            or lease is not None
        ):
            active_ids.add(generation_id)
    if len(active_ids) > DEFAULT_MAX_ACTIVE_GENERATIONS:
        raise ValueError("active_generation_queue_full")
    if not active_ids.issubset(index_ids):
        raise ValueError("active_generation_index_entry_missing")

    required_ids = protected | active_ids | {current_id}
    if len(required_ids) > max_generations:
        raise ValueError("derivation_lineage_generation_capacity_exceeded")
    if not required_ids.issubset(index_ids):
        raise ValueError("generation_retention_required_index_entry_missing")

    retained_ids = set(required_ids)
    # The index row order is the durable recency order used by both the index
    # writer and the checkpoint-file pruner.
    for row in reversed(rows):
        generation_id = row["generation_id"]
        if generation_id in retained_ids or generation_id not in checkpoints:
            continue
        if len(retained_ids) >= max_generations:
            break
        retained_ids.add(generation_id)
    return retained_ids


def preflight_generation_retention(
    index_path: pathlib.Path, generation_dir: pathlib.Path,
    prospective_checkpoint: Mapping[str, Any],
) -> set[str]:
    """Read the durable snapshot and reject unsafe capacity before mutation."""
    if index_path.is_symlink():
        raise ValueError("generation_retention_index_path_invalid")
    if index_path.is_file():
        index = load_json(index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
    else:
        index = {
            "schema_version": CHECKPOINT_SCHEMA, "generations": [],
            "detail_queue_cursor": 0, "detail_retry_state": {},
        }
    if not isinstance(index, Mapping) or index.get("schema_version") != CHECKPOINT_SCHEMA:
        raise ValueError("corrupt_generation_index")
    return plan_generation_retention(index, generation_dir, prospective_checkpoint)


def validate_collector_admission(
    args: argparse.Namespace, *, evidence: Any, candidate_sha256: str | None,
    evidence_sha256: str | None, generation_id: str, artifact_name: str,
    artifact_expires_at: str, now: dt.datetime,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Bind the processor's validated input bytes to the exact A run attempt."""
    if not args.collector_admission_file:
        if args.execution_mode == "live" and not args.input_error and candidate_sha256:
            raise ValueError("collector_handoff_admission_metadata_missing")
        return None, None
    path = args.collector_admission_file
    try:
        envelope = load_json(path, maximum_bytes=64 * 1024)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("collector_handoff_admission_metadata_invalid") from exc
    expected: dict[str, Any] = {
        "repository": args.repository,
        "producer_run_id": str(args.producer_run_id),
        "artifact_id": str(args.input_artifact_id or ""),
        "artifact_name": artifact_name,
        "artifact_expires_at": artifact_expires_at,
        "head_sha": args.producer_head_sha,
    }
    expected_arg_names = {
        "run_attempt": "producer_run_attempt",
        "run_started_at": "producer_run_started_at",
        "run_completed_at": "producer_run_completed_at",
        "observe_job_started_at": "producer_observe_job_started_at",
        "observe_job_completed_at": "producer_observe_job_completed_at",
        "artifact_created_at": "producer_artifact_created_at",
        "artifact_digest_sha256": "producer_artifact_digest_sha256",
        "artifact_size_bytes": "producer_artifact_size_bytes",
        "event": "producer_event",
    }
    for field, attr in expected_arg_names.items():
        value = getattr(args, attr, None)
        if value is not None:
            expected[field] = value
    diff_sha256 = file_sha256(args.diff) if getattr(args, "diff", None) and args.diff.is_file() else None
    try:
        envelope = validate_collector_admission_envelope(
            envelope,
            expected=expected,
            evidence=evidence,
            candidate_sha256=candidate_sha256,
            evidence_sha256=evidence_sha256,
            diff_sha256=diff_sha256,
            source_id=args.source,
            archive_path=args.collector_archive,
            producer_run_url=args.producer_run_url,
            now=now,
        )
    except HandoffError as exc:
        if str(exc).startswith("handoff_timestamp_"):
            raise ValueError("collector_handoff_admission_timestamp_invalid") from exc
        raise ValueError(str(exc)) from exc
    run_attempt = envelope["run_attempt"]
    archive_size = envelope["archive_size_bytes"]
    row = {
        "admission_id": admission_id(str(args.producer_run_id), run_attempt, str(evidence_sha256)),
        "producer_run_id": str(args.producer_run_id),
        "run_attempt": run_attempt,
        "head_sha": str(envelope["head_sha"]),
        "run_started_at": str(envelope["run_started_at"]),
        "artifact_id": str(args.input_artifact_id),
        "artifact_name": artifact_name,
        "artifact_expires_at": artifact_expires_at,
        "artifact_digest_sha256": str(envelope["artifact_digest_sha256"]),
        "artifact_size_bytes": archive_size,
        "refresh_evidence_sha256": str(evidence_sha256),
        "observed_at": str(envelope["observed_at"]),
        "generation_id": generation_id,
        "candidate_sha256": str(candidate_sha256),
        "admitted_at": timestamp(now),
    }
    try:
        validate_admission_row(row)
    except HandoffError as exc:
        raise ValueError(str(exc)) from exc
    index_path = args.state_dir / "sources" / args.source / "index.json"
    if index_path.exists():
        index = load_json(index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
    else:
        index = {"schema_version": CHECKPOINT_SCHEMA, "generations": [], "detail_queue_cursor": 0, "detail_retry_state": {}}
    if not isinstance(index, dict) or index.get("schema_version") != CHECKPOINT_SCHEMA or not isinstance(index.get("generations"), list):
        raise ValueError("corrupt_generation_index")
    existing_ledger = index.get("collector_handoff")
    if existing_ledger is not None:
        try:
            ledger = validate_ledger(existing_ledger)
        except HandoffError as exc:
            raise ValueError(str(exc)) from exc
        legacy_floor = ledger["legacy_discovery_floor"]
    else:
        try:
            legacy_floor = derive_legacy_floor(args.state_dir, index, now=now)
        except HandoffError as exc:
            raise ValueError(str(exc)) from exc
        if index["generations"] and legacy_floor is None:
            raise ValueError("legacy_discovery_floor_unverifiable")
    try:
        add_admission(index, row, now=timestamp(now), legacy_floor=legacy_floor)
    except HandoffError as exc:
        raise ValueError(str(exc)) from exc
    return row, legacy_floor


def bind_output_artifact_id(
    state_dir: pathlib.Path, source_id: str, generation_id: str, artifact_id: str,
    schema_path: pathlib.Path, *, processor_run_id: str, processor_artifact_run_id: str,
    artifact_expires_at: str,
) -> None:
    checkpoint_path = state_dir / "sources" / source_id / "generations" / f"{generation_id}.json"
    schema = load_json(schema_path, maximum_bytes=1024 * 1024)
    checkpoint = verify_checkpoint(load_json(checkpoint_path, maximum_bytes=STATE_FILE_LIMIT), schema)
    locator = checkpoint["output_artifact"]
    if (
        locator.get("name") != f"upstream-catalogue-processing-{processor_run_id}"
        or locator.get("run_id") != processor_artifact_run_id
        or (locator.get("artifact_id") is not None and locator.get("artifact_id") != artifact_id)
    ):
        raise ValueError("output_artifact_owner_mismatch")
    expiry = parse_timestamp(artifact_expires_at)
    if expiry <= utc_now():
        raise ValueError("output_artifact_expired")
    preflight_generation_retention(
        state_dir / "sources" / source_id / "index.json",
        checkpoint_path.parent,
        checkpoint,
    )
    locator["artifact_id"] = artifact_id
    locator["expires_at"] = timestamp(expiry)
    atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
    append_generation_index(
        state_dir / "sources" / source_id / "index.json", checkpoint_path, checkpoint,
    )


def protected_lineage_generations(generation_dir: pathlib.Path) -> set[str]:
    """Return all parent IDs referenced by any durable derived checkpoint."""
    protected: set[str] = set()
    for path in generation_dir.glob("*.json"):
        try:
            value = load_json(path, maximum_bytes=STATE_FILE_LIMIT)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("schema_version") != CHECKPOINT_SCHEMA:
            continue
        claimed = value.get("checkpoint_sha256")
        unsigned = dict(value)
        unsigned.pop("checkpoint_sha256", None)
        if claimed != checkpoint_digest(unsigned):
            raise ValueError("derivation_lineage_checkpoint_corrupt")
        inputs = value.get("generation_inputs")
        envelope = inputs.get("same_observation_derivation") if isinstance(inputs, dict) else None
        if envelope is None:
            continue
        try:
            validated = DERIVATION.validate_derivation_envelope(envelope)
        except ValueError as exc:
            raise ValueError("derivation_lineage_envelope_corrupt") from exc
        protected.update(validated["ancestor_generation_ids"])
    return protected


def prune_generation_files(
    generation_dir: pathlib.Path,
    current_path: pathlib.Path,
    max_generations: int = DEFAULT_MAX_GENERATIONS,
    *, retained_ids: set[str] | None = None,
) -> None:
    if retained_ids is None:
        index_path = generation_dir.parent / "index.json"
        index = load_json(index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
        current_checkpoint = verify_checkpoint(load_json(current_path, maximum_bytes=STATE_FILE_LIMIT))
        retained_ids = plan_generation_retention(
            index, generation_dir, current_checkpoint, max_generations=max_generations,
        )
    if current_path.stem not in retained_ids:
        raise ValueError("generation_retention_current_checkpoint_not_retained")
    for path in generation_dir.glob("*.json"):
        if path.stem not in retained_ids:
            path.unlink(missing_ok=True)


def mark_existing_generation_input_unavailable(
    args: argparse.Namespace,
    *,
    generation_id: str,
    reason: str,
    now: dt.datetime,
) -> tuple[int, dict[str, Any]]:
    """Close an active generation whose exact producer artifact cannot be restored."""
    if not re.fullmatch(r"[a-f0-9]{64}", generation_id):
        raise ValueError("target_generation_id_invalid")
    if reason not in {"artifact_missing", "input_expired"}:
        raise ValueError("target_generation_requires_missing_or_expired_input_error")
    if args.candidate or args.diff or args.refresh_evidence or args.resume_enrichment_evidence:
        raise ValueError("target_generation_input_unavailable_mode_forbids_candidate_bytes")
    checkpoint_path = (
        args.state_dir / "sources" / args.source / "generations" / f"{generation_id}.json"
    )
    schema = load_json(args.checkpoint_schema, maximum_bytes=1024 * 1024)
    checkpoint = verify_checkpoint(load_json(checkpoint_path, maximum_bytes=STATE_FILE_LIMIT), schema)
    if checkpoint.get("generation_id") != generation_id or checkpoint.get("source_id") != args.source:
        raise ValueError("target_generation_checkpoint_identity_mismatch")
    artifact_name = args.artifact_name or f"upstream-catalog-refresh-{args.producer_run_id}"
    refs = checkpoint.get("input_artifacts")
    if not isinstance(refs, list):
        raise ValueError("target_generation_input_artifacts_invalid")
    matches = [
        item for item in refs
        if isinstance(item, dict)
        and item.get("run_id") == str(args.producer_run_id)
        and item.get("name") == artifact_name
    ]
    if len(matches) != 1:
        raise ValueError("target_generation_input_artifact_locator_mismatch")
    input_artifact = matches[0]
    if args.input_artifact_id and input_artifact.get("artifact_id") != args.input_artifact_id:
        raise ValueError("target_generation_input_artifact_id_mismatch")
    if reason == "input_expired":
        try:
            expires_at = parse_timestamp(str(input_artifact.get("expires_at")))
        except (TypeError, ValueError) as exc:
            raise ValueError("target_generation_input_artifact_expiry_invalid") from exc
        if expires_at > now:
            raise ValueError("target_generation_input_artifact_not_expired")

    status = checkpoint.get("status")
    outcome = checkpoint.get("outcome") if isinstance(checkpoint.get("outcome"), dict) else {}
    if status in {"ready", "no-change"} and int(outcome.get("detail_retry_count", 0) or 0) == 0:
        raise ValueError("target_generation_already_terminal")
    if status not in {"queued", "validating", "enriching", "composing", "ready", "retry", "quarantined"}:
        raise ValueError("target_generation_not_active")
    if active_lease(checkpoint, now):
        return 2, checkpoint

    preflight_generation_retention(
        args.state_dir / "sources" / args.source / "index.json",
        args.state_dir / "sources" / args.source / "generations",
        checkpoint,
    )

    if status != "quarantined":
        checkpoint["status"] = "quarantined"
        checkpoint["outcome"] = {
            "reason": "input_expired" if reason == "input_expired" else "input_artifact_missing",
            "producer_run_id": str(args.producer_run_id),
            "artifact_name": artifact_name,
        }
    checkpoint["lease"] = None
    checkpoint["request_reservation"] = None
    if status != "quarantined":
        checkpoint["fencing_token"] = int(checkpoint.get("fencing_token", 0)) + 1
        checkpoint["last_heartbeat_at"] = timestamp(now)
        checkpoint["last_progress_at"] = timestamp(now)

    output_expiry = args.output_artifact_expires_at or timestamp(now + dt.timedelta(days=ARTIFACT_RETENTION_DAYS))
    if parse_timestamp(output_expiry) <= now:
        raise ValueError("output_artifact_expired")
    for name in (
        "composed-candidate.registry.json", "ready-scope.registry.json", "semantic-diff.json",
        "regeneration-queue.json", "quarantine.json", "composition-receipt.json",
        "upstream-catalogue-enrichment-evidence.json",
    ):
        (args.output_dir / name).unlink(missing_ok=True)
    sealed = persist_result_only_checkpoint(
        checkpoint,
        args,
        checkpoint_path=checkpoint_path,
        index_path=args.state_dir / "sources" / args.source / "index.json",
        output_expires_at=output_expiry,
    )
    return 3, sealed


def process(
    args: argparse.Namespace, *, fetcher: Callable[[str, float], str] = fetch_public_detail,
    resolver_fetcher: Callable[[str, float], LinkResolverObservation] = fetch_link_resolver,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], dt.datetime] | None = None,
) -> tuple[int, dict[str, Any]]:
    if (
        not 1 <= args.max_attempts <= DEFAULT_MAX_ATTEMPTS
        or not 1 <= args.max_queue <= DEFAULT_MAX_QUEUE
        or not 0 <= args.retries_per_detail <= DEFAULT_RETRIES_PER_DETAIL
        or not 1 <= args.timeout <= 30
    ):
        raise ValueError("invalid_request_bounds")
    now_fn = clock or (lambda: parse_timestamp(args.now) if args.now else utc_now())
    now = now_fn()
    processor_run_id = args.processor_run_id or args.producer_run_id
    if args.fixture_composer and not args.allow_fixture_composer:
        raise ValueError("fixture_composer_requires_test_flag")
    if args.fixture_composer and args.execution_mode != "fixture":
        raise ValueError("fixture_composer_forbidden_in_live_mode")
    if args.execution_mode == "live" and args.now:
        raise ValueError("injected_test_clock_forbidden_in_live_mode")
    if args.execution_mode == "live":
        if not str(args.producer_run_id).isdigit():
            raise ValueError("producer_run_id_invalid")
        if not re.fullmatch(r"[0-9]+-[0-9]+", str(args.processor_run_id or "")):
            raise ValueError("processor_run_id_invalid")
        if not str(args.processor_artifact_run_id or "").isdigit():
            raise ValueError("processor_artifact_run_id_invalid")
        if args.artifact_name and args.artifact_name != f"upstream-catalog-refresh-{args.producer_run_id}":
            raise ValueError("producer_artifact_name_mismatch")
    if args.target_generation_id:
        return mark_existing_generation_input_unavailable(
            args,
            generation_id=args.target_generation_id,
            reason=args.input_error or "",
            now=now_fn(),
        )
    source_scope = args.source_scope if args.execution_mode == "live" else f"fixture:{args.source_scope}"
    policy = load_json(args.source_policy, maximum_bytes=4 * 1024 * 1024)
    if not isinstance(policy, dict):
        raise ValueError("source_policy must be an object")
    sources = [row for row in policy.get("sources", []) if isinstance(row, dict) and row.get("source_id") == args.source]
    if len(sources) != 1:
        raise ValueError(f"source policy must contain exactly one {args.source}")
    source = sources[0]
    baseline_path = args.baseline or pathlib.Path(source["canonical_registry"])
    baseline_rows = load_registry(baseline_path)
    composition_baseline_path = args.composition_baseline or baseline_path
    derivation: dict[str, Any] | None = None
    if args.same_observation_derivation:
        if not args.composition_baseline:
            raise ValueError("same_observation_derivation_requires_composition_baseline")
        derivation = DERIVATION.validate_derivation_envelope(
            load_json(args.same_observation_derivation, maximum_bytes=256 * 1024),
        )
    elif args.composition_baseline or args.derivation_journal or args.derivation_journal_ref_sha:
        raise ValueError("composition_baseline_requires_same_observation_derivation")
    candidate_rows: list[dict[str, Any]] | None = None
    candidate_path = args.candidate
    evidence = load_json(args.refresh_evidence, maximum_bytes=8 * 1024 * 1024) if args.refresh_evidence and args.refresh_evidence.exists() else None
    diff_path = args.diff
    candidate_sha = file_sha256(candidate_path) if candidate_path and candidate_path.is_file() else None
    evidence_sha = file_sha256(args.refresh_evidence) if args.refresh_evidence and args.refresh_evidence.is_file() else None
    baseline_sha = file_sha256(baseline_path)
    policy_sha = file_sha256(args.source_policy)
    adapter_sha = file_sha256(args.provider_index)
    collection = evidence.get("collection") if isinstance(evidence, dict) else None
    collection_succeeded = isinstance(collection, dict) and collection.get("succeeded") is True
    supplied_observed_at = evidence.get("observed_at") if isinstance(evidence, dict) else None
    if collection_succeeded and (not isinstance(supplied_observed_at, str) or not supplied_observed_at.strip()):
        raise ValueError("successful_producer_observation_timestamp_missing")
    if isinstance(supplied_observed_at, str) and supplied_observed_at.strip():
        try:
            producer_observed_at = parse_timestamp(supplied_observed_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("producer_observation_timestamp_invalid") from exc
        if producer_observed_at > now + dt.timedelta(minutes=5):
            raise ValueError("producer_observation_in_future")
        observed_at = supplied_observed_at
    elif collection_succeeded:
        # Keep the explicit guard above close to this fallback so a successful
        # collector delivery can never inherit processor time as source time.
        raise ValueError("successful_producer_observation_timestamp_missing")
    else:
        observed_at = timestamp(now)
    collection_status = "success" if collection_succeeded else "failure" if isinstance(evidence, dict) and evidence.get("status") == "collection_failure" else "missing"
    artifact_name = args.artifact_name or f"upstream-catalog-refresh-{args.producer_run_id}"
    artifact_expires_at = args.artifact_expires_at or timestamp(now + dt.timedelta(days=ARTIFACT_RETENTION_DAYS))
    output_artifact_expires_at = args.output_artifact_expires_at or timestamp(now + dt.timedelta(days=ARTIFACT_RETENTION_DAYS))
    try:
        if parse_timestamp(output_artifact_expires_at) <= now:
            raise ValueError("output_artifact_expired")
    except (TypeError, ValueError) as exc:
        if str(exc) == "output_artifact_expired":
            raise
        raise ValueError("output_artifact_expiry_invalid") from exc
    if args.producer_run_url and re.search(r"(?i)(servicekey|api[_-]?key|token|authorization)=", args.producer_run_url):
        raise ValueError("producer run URL must not contain credentials")

    generation_id, generation_inputs = generation_identity(
        args.source, source_scope, baseline_sha, candidate_sha,
        evidence_sha if candidate_sha is None else None, policy_sha, adapter_sha,
        same_observation_derivation=derivation,
    )
    if args.expected_generation_id and generation_id != args.expected_generation_id:
        raise ValueError("prepared_generation_identity_mismatch")
    generation_dir = args.state_dir / "sources" / args.source / "generations"
    index_path = args.state_dir / "sources" / args.source / "index.json"
    schema = load_json(args.checkpoint_schema, maximum_bytes=1024 * 1024)
    derivation_parent_checkpoint: dict[str, Any] | None = None
    if derivation is not None:
        resume_id = derivation["resume_parent_processor"]["generation_id"]
        parent_path = generation_dir / f"{resume_id}.json"
        if not parent_path.is_file():
            raise ValueError("derivation_resume_parent_checkpoint_unavailable")
        derivation_parent_checkpoint = verify_checkpoint(load_json(parent_path, maximum_bytes=STATE_FILE_LIMIT), schema)
        parent_inputs = derivation_parent_checkpoint.get("generation_inputs")
        if not isinstance(parent_inputs, dict):
            raise ValueError("derivation_resume_parent_inputs_missing")
        if (
            parent_inputs.get("source_id") != args.source
            or parent_inputs.get("source_scope") != source_scope
            or parent_inputs.get("baseline_sha256") != baseline_sha
            or parent_inputs.get("candidate_sha256") != candidate_sha
            or parent_inputs.get("observation_failure_sha256") is not None
        ):
            raise ValueError("derivation_resume_parent_source_contract_changed")
        if (
            derivation["resume_parent_processor"]["checkpoint_sha256"] != derivation_parent_checkpoint.get("checkpoint_sha256")
            or derivation["resume_parent_processor"]["generation_id"] != derivation_parent_checkpoint.get("generation_id")
        ):
            raise ValueError("derivation_resume_parent_checkpoint_reference_mismatch")
    checkpoint_path = generation_dir / f"{generation_id}.json"
    collector_admission: dict[str, Any] | None = None
    legacy_floor: dict[str, Any] | None = None
    if not args.input_error and candidate_path is not None and candidate_path.is_file() and evidence_sha:
        collector_admission, legacy_floor = validate_collector_admission(
            args, evidence=evidence, candidate_sha256=candidate_sha,
            evidence_sha256=evidence_sha, generation_id=generation_id,
            artifact_name=artifact_name, artifact_expires_at=artifact_expires_at, now=now,
        )
    if derivation is not None:
        if not args.current_main_root or not args.current_main_root.is_dir():
            raise ValueError("derivation_current_main_checkout_missing")
        assert derivation_parent_checkpoint is not None
        existing_target: dict[str, Any] | None = None
        if checkpoint_path.is_file() and not checkpoint_path.is_symlink():
            existing_target = verify_checkpoint(
                load_json(checkpoint_path, maximum_bytes=STATE_FILE_LIMIT), schema,
            )
        existing_outcome = existing_target.get("outcome") if isinstance(existing_target, Mapping) and isinstance(existing_target.get("outcome"), Mapping) else {}
        can_resume_advanced_main = bool(
            isinstance(existing_target, Mapping)
            and existing_target.get("generation_inputs", {}).get("same_observation_derivation") == derivation
            and (
                existing_target.get("status") in {"queued", "validating", "enriching", "composing", "retry"}
                or existing_target.get("status") == "ready" and int(existing_outcome.get("detail_retry_count", 0) or 0) > 0
            )
        )
        validate_derivation_before_claim(
            args,
            generation_id=generation_id,
            derivation=derivation,
            composition_baseline_path=composition_baseline_path,
            collector_admission=collector_admission,
            state_index_path=index_path,
            schema=schema,
            original_baseline_sha256=baseline_sha,
            candidate_sha256=str(candidate_sha or ""),
            evidence_sha256=str(evidence_sha or ""),
            diff_sha256=file_sha256(diff_path) if diff_path and diff_path.is_file() else "",
            policy_sha256=policy_sha,
            adapter_sha256=adapter_sha,
            current_main_root=args.current_main_root.resolve(),
            resume_parent_bundle_dir=args.resume_parent_bundle_dir,
            canonical_parent_bundle_dir=args.canonical_parent_bundle_dir,
            allow_source_only_main_advance=can_resume_advanced_main,
            allow_monotonic_journal_successor=can_resume_advanced_main,
        )
    if checkpoint_path.exists():
        try:
            checkpoint = verify_checkpoint(load_json(checkpoint_path, maximum_bytes=STATE_FILE_LIMIT), schema)
        except Exception as exc:  # corrupted state is preserved and separately quarantined
            quarantine_path = args.state_dir / "quarantine" / f"{generation_id}.json"
            quarantined = {
                "schema_version": CHECKPOINT_SCHEMA, "generation_id": generation_id,
                "status": "quarantined", "reason": safe_error_class(exc),
                "observed_at": observed_at, "last_heartbeat_at": timestamp(now),
            }
            atomic_write_json(quarantine_path, quarantined)
            return 3, quarantined
    else:
        checkpoint = receipt_base(
            source_id=args.source, source_scope=source_scope, generation_id=generation_id,
            generation_inputs=generation_inputs, observed_at=observed_at,
            producer_run_id=args.producer_run_id, processor_run_id=processor_run_id,
            processor_artifact_run_id=args.processor_artifact_run_id or processor_run_id,
            repository=args.repository, execution_mode=args.execution_mode,
            artifact_name=artifact_name, artifact_id=args.input_artifact_id,
            input_artifact_expires_at=artifact_expires_at,
            output_artifact_expires_at=output_artifact_expires_at,
            evidence_sha256=evidence_sha,
            diff_sha256=file_sha256(diff_path) if diff_path and diff_path.is_file() else None,
            status="queued",
        )
        if derivation is not None:
            assert derivation_parent_checkpoint is not None
            # A derivative is still the same immutable A observation. Carry
            # its observation clock/count verbatim; retry counters and cursor
            # continue to come from the current state index below.
            checkpoint["observed_at"] = derivation_parent_checkpoint["observed_at"]
            checkpoint["last_observation"] = dict(derivation_parent_checkpoint["last_observation"])
            checkpoint["observation_count"] = int(derivation_parent_checkpoint["observation_count"])
        checkpoint["detail_queue_cursor"] = source_queue_cursor(index_path, 0)

    if checkpoint.get("generation_inputs") != generation_inputs:
        raise ValueError("generation identity input mismatch")
    replay_prior_checkpoint = copy.deepcopy(checkpoint)
    replay_prior_index = (
        load_json(index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
        if index_path.is_file() else None
    )
    refs = checkpoint.setdefault("input_artifacts", [])
    if not any(
        isinstance(ref, dict)
        and ref.get("run_id") == args.producer_run_id
        and ref.get("artifact_id") == args.input_artifact_id
        and ref.get("evidence_sha256") == evidence_sha
        for ref in refs
    ):
        refs.append({
            "run_id": args.producer_run_id, "name": artifact_name, "artifact_id": args.input_artifact_id,
            "expires_at": artifact_expires_at,
            "candidate_sha256": candidate_sha, "evidence_sha256": evidence_sha,
            "diff_sha256": file_sha256(diff_path) if diff_path and diff_path.is_file() else None,
        })
        del refs[:-8]
    # A repeated delivery of a terminal generation is idempotent. Update only
    # heartbeat metadata; a no-change result remains distinct from observation.
    new_observation = {
        "observed_at": observed_at,
        "producer_run_id": args.producer_run_id,
        "refresh_evidence_sha256": evidence_sha,
        "collection_status": collection_status,
        "execution_mode": args.execution_mode,
    }
    prior_observation = checkpoint.get("last_observation")
    observation_is_older_replay = False
    if isinstance(prior_observation, dict) and collector_admission is not None:
        prior_artifact_id = None
        matching_prior_refs = [
            ref for ref in refs if isinstance(ref, dict)
            and str(ref.get("run_id")) == str(prior_observation.get("producer_run_id"))
            and ref.get("evidence_sha256") == prior_observation.get("refresh_evidence_sha256")
        ]
        if matching_prior_refs:
            prior_artifact_id = str(matching_prior_refs[-1].get("artifact_id") or "") or None
        prior_admission = None
        if index_path.is_file():
            current_index = load_json(index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT)
            current_ledger = current_index.get("collector_handoff") if isinstance(current_index, dict) else None
            if current_ledger is not None:
                prior_admission = find_admitted_observation(
                    current_ledger,
                    producer_run_id=str(prior_observation.get("producer_run_id") or ""),
                    evidence_sha256=str(prior_observation.get("refresh_evidence_sha256") or ""),
                    generation_id=str(checkpoint.get("generation_id") or ""),
                    artifact_id=prior_artifact_id,
                )
        if prior_admission is not None:
            observation_is_older_replay = admission_order_key(collector_admission) < admission_order_key(prior_admission)
        else:
            observation_is_older_replay = parse_timestamp(observed_at) < parse_timestamp(
                str(prior_observation.get("observed_at") or "")
            )
    new_observation_received = not observation_is_older_replay and (not isinstance(prior_observation, dict) or any(
        prior_observation.get(key) != value for key, value in new_observation.items()
    ))
    if new_observation_received:
        checkpoint["observation_count"] = int(checkpoint.get("observation_count", 0)) + 1
    if not observation_is_older_replay:
        checkpoint["last_observation"] = new_observation
    prior_outcome = checkpoint.get("outcome") if isinstance(checkpoint.get("outcome"), dict) else {}
    ready_has_detail_work = (
        checkpoint.get("status") == "ready"
        and int(prior_outcome.get("detail_retry_count", 0) or 0) > 0
    )
    retention_candidate = dict(checkpoint)
    if checkpoint.get("status") in {"ready", "no-change"} and (new_observation_received or ready_has_detail_work):
        retention_candidate["status"] = "retry"
    # Plan the exact required/retained generation set before terminal replay,
    # quarantine, lease acquisition, or request reservation can write state.
    preflight_generation_retention(index_path, generation_dir, retention_candidate)
    if checkpoint.get("status") in {"ready", "no-change", "quarantined"} and not new_observation_received and not ready_has_detail_work:
        checkpoint["last_heartbeat_at"] = timestamp(now)
        atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
        append_generation_index(
            index_path, checkpoint_path, checkpoint,
            collector_admission=collector_admission, legacy_floor=legacy_floor, admitted_at=timestamp(now),
        )
        args.exact_delivery_replay = True
        if checkpoint.get("status") in {"ready", "no-change"}:
            if (
                collector_admission is not None
                and replay_prior_index is not None
                and getattr(args, "replay_state_head_sha", None)
            ):
                durable_admission = _replay_admission(replay_prior_checkpoint, replay_prior_index)
                incoming_admission = validate_admission_row(collector_admission)
                if any(
                    durable_admission.get(field) != incoming_admission.get(field)
                    for field in set(durable_admission) - {"admitted_at"}
                ):
                    raise ValueError("replay_authenticated_admission_mismatch")
                args.exact_delivery_replay_context = {
                    "prior_checkpoint": replay_prior_checkpoint,
                    "current_checkpoint": copy.deepcopy(checkpoint),
                    "prior_index": replay_prior_index,
                    "current_index": load_json(index_path, maximum_bytes=STATE_INDEX_FILE_LIMIT),
                    "state_head_sha": args.replay_state_head_sha,
                    "checkpoint_schema": schema,
                }
        return (0 if checkpoint.get("status") in {"ready", "no-change"} else 3), checkpoint
    if checkpoint.get("status") in {"ready", "no-change"} and (new_observation_received or ready_has_detail_work):
        checkpoint["status"] = "retry"

    # Expiry is explicit and digest-bound. A newer artifact never substitutes
    # for the bytes named by this generation's immutable input hashes.
    if parse_timestamp(artifact_expires_at) <= now:
        checkpoint.update({
            "status": "quarantined", "outcome": {"reason": "input_expired"},
            "lease": None, "request_reservation": None,
            "last_heartbeat_at": timestamp(now), "last_progress_at": timestamp(now),
        })
        sealed = persist_result_only_checkpoint(
            checkpoint,
            args,
            checkpoint_path=checkpoint_path,
            index_path=index_path,
            output_expires_at=output_artifact_expires_at,
        )
        return 3, sealed

    acquired, lease_reason = acquire_lease(checkpoint, processor_run_id, now)
    if not acquired:
        conflict = dict(checkpoint)
        conflict.update({"status": "retry", "outcome": {"reason": lease_reason}, "last_heartbeat_at": timestamp(now)})
        return 2, conflict
    token = int(checkpoint["fencing_token"])
    checkpoint["status"] = "validating"
    checkpoint["last_heartbeat_at"] = timestamp(now)
    checkpoint["last_progress_at"] = timestamp(now)
    input_error = args.input_error
    if isinstance(evidence, dict) and evidence.get("status") == "collection_failure":
        input_error = input_error or "collection_failure"
    if candidate_path is None or not candidate_path.is_file():
        input_error = input_error or "candidate_artifact_missing"
    if input_error:
        terminal_missing_input = input_error in {"artifact_missing", "candidate_artifact_missing"}
        checkpoint.update({
            "status": "quarantined" if terminal_missing_input else "retry",
            "outcome": {"reason": input_error},
            "lease": None, "request_reservation": None,
        })
        checkpoint["last_heartbeat_at"] = timestamp(now)
        checkpoint["last_progress_at"] = timestamp(now)
        sealed = persist_result_only_checkpoint(
            checkpoint,
            args,
            checkpoint_path=checkpoint_path,
            index_path=index_path,
            output_expires_at=output_artifact_expires_at,
        )
        return (3 if terminal_missing_input else 2), sealed

    assert candidate_path is not None
    candidate_rows = load_registry(candidate_path)
    if not isinstance(evidence, dict) or evidence.get("collection", {}).get("succeeded") is not True:
        checkpoint.update({"status": "retry", "outcome": {"reason": "observation_not_successful"}, "lease": None})
        checkpoint["last_heartbeat_at"] = timestamp(now)
        atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
        append_generation_index(index_path, checkpoint_path, checkpoint)
        return 2, checkpoint
    if not diff_path or not diff_path.is_file():
        checkpoint.update({"status": "retry", "outcome": {"reason": "catalog_diff_missing"}, "lease": None})
        checkpoint["last_heartbeat_at"] = timestamp(now)
        atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
        append_generation_index(index_path, checkpoint_path, checkpoint)
        return 2, checkpoint

    baseline_asset = unique_rows(baseline_rows, "baseline")
    candidate_asset = unique_rows(candidate_rows, "candidate")
    registered_hosts = DETAIL_HELPERS.registered_hosts(load_json(args.provider_index))
    try:
        all_cached_records = validated_resume_records(
            args.resume_enrichment_evidence,
            checkpoint=checkpoint,
            state_dir=args.state_dir,
            source_id=args.source,
            checkpoint_schema=schema,
            provider_index_sha256=adapter_sha,
            candidate_by_id=candidate_asset,
            registered_hosts=registered_hosts,
            now=now,
            allow_parent_extractor_revision=derivation is not None,
            expected_owner_generation_id=(
                derivation["resume_parent_processor"]["generation_id"] if derivation is not None else None
            ),
            reject_stale_source_or_guide_contract=derivation is not None,
        )
        if derivation is not None:
            canonical_evidence_path = args.canonical_parent_bundle_dir / "upstream-catalogue-enrichment-evidence.json"
            canonical_records = validated_resume_records(
                canonical_evidence_path,
                checkpoint=checkpoint,
                state_dir=args.state_dir,
                source_id=args.source,
                checkpoint_schema=schema,
                provider_index_sha256=adapter_sha,
                candidate_by_id=candidate_asset,
                registered_hosts=registered_hosts,
                now=now,
                allow_parent_extractor_revision=True,
                expected_owner_generation_id=derivation["canonical_parent_processor"]["generation_id"],
                reject_stale_source_or_guide_contract=True,
            )
            all_cached_records = merge_derivation_enrichment_records(all_cached_records, canonical_records)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        checkpoint.update({"status": "quarantined", "outcome": {"reason": safe_error_class(exc)}, "lease": None})
        checkpoint["last_heartbeat_at"] = timestamp(now)
        atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
        append_generation_index(index_path, checkpoint_path, checkpoint)
        return 3, checkpoint
    queued, retained, _ = detail_queue(baseline_rows, candidate_rows, all_cached_records, now)
    durable_retry_state = source_retry_state(index_path)
    retained_failure_diagnostics: dict[str, dict[str, Any]] = {}
    attempt_counts = checkpoint.setdefault("attempts_by_id", {})
    reset_ids = set(str(value) for value in checkpoint.get("detail_retry_reset_ids", []))
    for queue_row in queued:
        identity = queue_row["id"]
        prior_state = durable_retry_state.get(identity)
        if not isinstance(prior_state, dict):
            continue
        same_contract = (
            prior_state.get("source_sha256") == queue_row["source_sha256"]
            and prior_state.get("guide_sha256") == queue_row["guide_sha256"]
        )
        if not same_contract:
            attempt_counts.pop(identity, None)
            reset_ids.add(identity)
            continue
        last_attempt_at = parse_timestamp(str(prior_state["last_attempt_at"]))
        stale_retry_epoch = (
            new_observation_received
            and now_fn() - last_attempt_at >= dt.timedelta(days=DETAIL_OBSERVATION_TTL_DAYS)
        )
        if stale_retry_epoch:
            attempt_counts.pop(identity, None)
            reset_ids.add(identity)
        else:
            attempt_counts[identity] = max(int(attempt_counts.get(identity, 0)), int(prior_state["attempts"]))
            if "failure_diagnostic" in prior_state:
                retained_failure_diagnostics[identity] = validate_failure_diagnostic(
                    prior_state["failure_diagnostic"], identity=identity,
                )
    checkpoint["detail_retry_reset_ids"] = sorted(reset_ids)[-DEFAULT_MAX_RETRY_STATES:]
    reset_identity_set = set(checkpoint["detail_retry_reset_ids"])
    if reset_identity_set:
        checkpoint["detail_records"] = [
            row for row in checkpoint.get("detail_records", [])
            if not isinstance(row, dict) or str(row.get("id") or "") not in reset_identity_set
        ]
    queue_cursor = source_queue_cursor(index_path, int(checkpoint.get("detail_queue_cursor", 0)))
    reservation = checkpoint.get("request_reservation")
    if args.claim_only:
        reservation, reserved_queue, unqueued = reserve_requests(
            checkpoint, queued, cursor=queue_cursor, processor_run_id=processor_run_id,
            max_attempts=args.max_attempts, max_queue=args.max_queue,
            retries_per_detail=args.retries_per_detail, now=now_fn(),
            no_advance_when_empty=derivation is not None,
        )
        checkpoint.update({
            "status": "enriching",
            "outcome": {
                "reason": "request_budget_reserved",
                "reserved_attempts": reservation["reserved_attempts"],
                "reserved_records": len(reserved_queue), "pending_count": len(unqueued),
            },
            "last_heartbeat_at": timestamp(now_fn()), "last_progress_at": timestamp(now_fn()),
        })
        atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
        append_generation_index(
            index_path, checkpoint_path, checkpoint,
            collector_admission=collector_admission, legacy_floor=legacy_floor, admitted_at=timestamp(now_fn()),
        )
        return 0, checkpoint

    if args.require_durable_reservation:
        if (
            not isinstance(reservation, dict)
            or reservation.get("owner_run_id") != processor_run_id
            or reservation.get("generation_id") != checkpoint["generation_id"]
            or reservation.get("fencing_token") != token
        ):
            raise ValueError("durable_request_reservation_missing")
        if parse_timestamp(str(reservation.get("expires_at"))) <= now_fn():
            raise ValueError("durable_request_reservation_expired")
        reserved_ids = {str(row.get("id")) for row in reservation.get("records", []) if isinstance(row, dict)}
        reserved_queue = [row for row in queued if row["id"] in reserved_ids]
        unqueued = [row for row in queued if row["id"] not in reserved_ids]
    else:
        reservation, reserved_queue, unqueued = reserve_requests(
            checkpoint, queued, cursor=queue_cursor, processor_run_id=processor_run_id,
            max_attempts=args.max_attempts, max_queue=args.max_queue,
            retries_per_detail=args.retries_per_detail, now=now_fn(),
            no_advance_when_empty=derivation is not None,
        )
    reserved_by_id = {
        str(row["id"]): int(row["attempts_reserved"])
        for row in reservation.get("records", []) if isinstance(row, dict)
    }
    exhausted_rows = [
        row for row in unqueued
        if int(checkpoint.get("attempts_by_id", {}).get(row["id"], 0)) >= args.retries_per_detail + 1
    ]
    exhausted_ids = {row["id"] for row in exhausted_rows}
    unqueued = [row for row in unqueued if row["id"] not in exhausted_ids]
    retained_ids = {row["id"] for row in retained}
    resumed_records = {identity: record for identity, record in all_cached_records.items() if identity in retained_ids}
    checkpoint["status"] = "enriching"
    checkpoint["outcome"] = {"reason": "detail_processing"}
    checkpoint["detail_records"] = []
    checkpoint["last_heartbeat_at"] = timestamp(now_fn())
    # Persist the lease and the queue before network requests. A committed copy
    # is still protected by the workflow's old-ref SHA compare-and-swap.
    atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))

    candidate_by_id = candidate_asset
    enriched_records: list[dict[str, Any]] = list(resumed_records.values())
    worker_records: list[dict[str, Any]] = []
    attempt_budget = int(args.max_attempts)
    attempts_this_invocation = 0
    attempt_counts = checkpoint.setdefault("attempts_by_id", {})
    attempted_by_id: dict[str, int] = {}
    refunded_reservations = 0
    for queue_row in reserved_queue + exhausted_rows:
        ensure_fence(checkpoint, processor_run_id, token, now_fn())
        identity = queue_row["id"]
        row = candidate_by_id[identity]
        if identity in resumed_records:
            worker_records.append({
                "id": identity, "status": "enriched",
                "source_sha256": queue_row["source_sha256"], "guide_sha256": queue_row["guide_sha256"],
            })
            continue
        reserved_count = reserved_by_id.get(identity, 0)
        attempts_available = reserved_count
        row_status = "retry"
        row_operations: list[dict[str, Any]] = []
        failure_diagnostic = retained_failure_diagnostics.get(identity)
        source_provenance: dict[str, Any] | None = None
        declaration_provenance: dict[str, Any] | None = None
        link_metadata: dict[str, Any] | None = None
        observed_guide: str | None = None
        observation: DetailPageObservation | None = None
        current_template: dict[str, str | None] | None = None
        resolver_attempts = 0
        expected_page_url = ""
        if not identity.isdigit():
            row_status = "quarantined"
            failure_diagnostic = {"code": "contract_or_parse_error"}
        try:
            page_url = candidate_detail_url(row, identity)
        except ValueError:
            page_url = ""
            row_status = "quarantined"
            failure_diagnostic = {"code": "contract_or_parse_error"}
        if row_status != "quarantined" and reserved_count == 0:
            row_status = "quarantined"

        def consume_physical_slot() -> bool:
            nonlocal attempts_available, attempts_this_invocation
            if attempts_available <= 0 or attempts_this_invocation >= attempt_budget:
                return False
            ensure_fence(checkpoint, processor_run_id, token, now_fn())
            attempts_available -= 1
            attempts_this_invocation += 1
            attempted_by_id[identity] = attempted_by_id.get(identity, 0) + 1
            reservation["attempts_made"] = attempts_this_invocation
            checkpoint["last_heartbeat_at"] = timestamp(now_fn())
            checkpoint["last_progress_at"] = timestamp(now_fn())
            # The reservation and lifetime budget were durably charged before
            # this physical request. An interruption cannot refund it.
            atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
            return True

        def is_terminal_contract_error(exc: BaseException) -> bool:
            return isinstance(exc, (LinkResolverContractError, DETAIL_HELPERS.LinkDetailContractError))

        while attempts_available > 0 and attempts_this_invocation < attempt_budget:
            if row_status == "quarantined":
                break
            if observation is None:
                if not consume_physical_slot():
                    break
                failure_context = "page_fetch"
                try:
                    expected_page_url = safe_public_page_url(identity)
                    fetched = fetcher(expected_page_url, float(args.timeout))
                    if isinstance(fetched, DetailPageObservation):
                        observation = fetched
                    else:
                        if args.execution_mode == "live":
                            raise ValueError("live_fetch_missing_page_observation")
                        body = str(fetched)
                        body_bytes = body.encode("utf-8")
                        observation = DetailPageObservation(
                            body=body, page_url=expected_page_url, effective_url=expected_page_url,
                            page_sha256=sha256_bytes(body_bytes), observed_at=timestamp(now), page_bytes=body_bytes,
                        )
                    failure_context = "page_observation"
                    body = observation.body
                    observation_valid = (
                        observation.page_url == expected_page_url
                        and observation.effective_url == expected_page_url
                        and observation.page_sha256 == sha256_bytes(observation.page_bytes)
                        and len(observation.page_bytes) <= MAX_DETAIL_BYTES
                        and parse_timestamp(observation.observed_at) <= now_fn() + dt.timedelta(minutes=5)
                    )
                    if not observation_valid:
                        row_status = "quarantined"
                        failure_diagnostic = {"code": "observation_mismatch"}
                        break
                    failure_context = "page_parser"
                    current_template = extract_current_template_dataset_id(body, identity)
                    if current_template is not None:
                        observed_guide = observed_guide_url(body, expected_page_url)
                        continue

                    # The old anchor extractor remains the only path for the
                    # legacy template. A malformed current button raises above
                    # and cannot fall through to an unrelated anchor.
                    urls = DETAIL_HELPERS.extract_link_detail_operation_urls(body)
                    missing_hosts = {
                        (urllib.parse.urlsplit(endpoint).hostname or "").lower()
                        for endpoint in urls
                        if not safe_operation(DETAIL_HELPERS.operation(row, endpoint, 0, len(urls)))
                        or (urllib.parse.urlsplit(endpoint).hostname or "").lower() not in registered_hosts
                    }
                    if missing_hosts:
                        row_status = "quarantined"
                        failure_diagnostic = {"code": "unsafe_or_unregistered_operation_host"}
                        break
                    observed_guide = observed_guide_url(body, page_url)
                    for index, endpoint in enumerate(urls):
                        item = DETAIL_HELPERS.operation(row, endpoint, index, len(urls))
                        item["source"]["url"] = page_url
                        operation_raw = item["source"].get("raw") or {}
                        if observed_guide is None:
                            operation_raw.pop("guide_url", None)
                        else:
                            operation_raw["guide_url"] = observed_guide
                        item["source"]["raw"] = redact_operation_raw(operation_raw)
                        row_operations.append(item)
                    if row_operations:
                        row_status = "enriched"
                        failure_diagnostic = None
                        source_provenance = {
                            "system": "data.go.kr",
                            "page_url": observation.page_url,
                            "effective_url": observation.effective_url,
                            "page_sha256": observation.page_sha256,
                            "observed_at": observation.observed_at,
                        }
                    else:
                        row_status = "quarantined"
                        failure_diagnostic = {"code": "missing_link_detail_operations"}
                    break
                except Exception as exc:
                    if failure_context == "page_parser":
                        row_status = "quarantined"
                        failure_diagnostic = {"code": "contract_or_parse_error"}
                        break
                    if failure_context == "page_observation":
                        failure_diagnostic = {"code": "observation_mismatch"}
                        row_status = "retry"
                        observation = None
                        current_template = None
                    else:
                        failure_diagnostic = classify_detail_exception(exc)
                        if (
                            failure_context == "page_fetch"
                            and failure_diagnostic.get("code") in {"timeout", "transport_error"}
                        ):
                            failure_diagnostic["phase"] = "page"
                        row_status = "retry"
                    if failure_context == "page_fetch" and is_terminal_contract_error(exc):
                        row_status = "quarantined"
                    if attempts_available > 0 and attempts_this_invocation < attempt_budget:
                        sleeper(min(5.0, 0.25 * (2 ** max(0, attempted_by_id.get(identity, 1) - 1))))
                    continue

            if current_template is None:
                # A successful page parse can only be absent here after a
                # legacy result; those rows were finalized above.
                row_status = "quarantined"
                failure_diagnostic = {"code": "contract_or_parse_error", "phase": "page"}
                break
            if attempts_available <= 0 or attempts_this_invocation >= attempt_budget:
                row_status = "retry"
                failure_diagnostic = {"code": "insufficient_budget_for_link_resolver", "phase": "resolver"}
                break
            if not consume_physical_slot():
                row_status = "retry"
                failure_diagnostic = {"code": "insufficient_budget_for_link_resolver", "phase": "resolver"}
                break
            resolver_attempts += 1

            resolver_url = DETAIL_HELPERS.CURRENT_LINK_RESOLVER_URL.format(dataset_id=identity)
            failure_context = "resolver_fetch"
            try:
                resolver_observation = resolver_fetcher(resolver_url, float(args.timeout))
                if not isinstance(resolver_observation, LinkResolverObservation):
                    raise LinkResolverContractError("live_resolver_missing_observation")
                if (
                    resolver_observation.request_url != resolver_url
                    or resolver_observation.effective_url != resolver_url
                    or not isinstance(resolver_observation.body, bytes)
                    or len(resolver_observation.body) > DETAIL_HELPERS.MAX_LINK_RESOLVER_RESPONSE_BYTES
                    or parse_timestamp(resolver_observation.observed_at) > now_fn() + dt.timedelta(minutes=5)
                ):
                    row_status = "quarantined"
                    failure_diagnostic = {"code": "observation_mismatch", "phase": "resolver"}
                    break
                failure_context = "resolver_validation"
                resolved = validate_link_resolver_response(
                    resolver_observation.body, identity, resolver_observation.observed_at,
                    expected_public_data_detail_pk=current_template.get("public_data_detail_pk"),
                )
                candidate_link_metadata = {
                    "method": DETAIL_HELPERS.CURRENT_LINK_RESOLVER_METHOD,
                    "dataset_id": current_template["dataset_id"],
                    "public_data_pk": current_template["public_data_pk"],
                    "public_data_detail_pk": current_template.get("public_data_detail_pk"),
                    "page": {
                        "url": observation.page_url,
                        "effective_url": observation.effective_url,
                        "sha256": observation.page_sha256,
                        "bytes": len(observation.page_bytes),
                        "observed_at": observation.observed_at,
                    },
                    "resolver": {
                        "request_url": resolver_url,
                        "effective_url": resolver_observation.effective_url,
                        "sha256": resolved["response_sha256"],
                        "bytes": resolved["response_bytes"],
                        "observed_at": resolver_observation.observed_at,
                        "public_data_detail_pk": resolved["public_data_detail_pk"],
                        "resolved_url": resolved["link_url"],
                        "resolved_url_sha256": resolved["link_url_sha256"],
                    },
                }
                link_metadata = DETAIL_HELPERS.validate_link_metadata(
                    candidate_link_metadata, identity, registered_hosts,
                )
                try:
                    if identity != SEOUL_DECLARATION.DECLARATION["subject"]["portal_dataset_id"]:
                        raise KnownDeclarationFailure("no_reviewed_declaration")
                    if not seoul_subject_binding_matches(row, identity):
                        raise KnownDeclarationFailure("subject_binding_unproven")
                    declared_operation = SEOUL_DECLARATION.build_operation(row, observed_guide)
                    declaration_provenance = SEOUL_DECLARATION.build_provenance(
                        row,
                        source_sha256=queue_row["source_sha256"],
                        guide_sha256=queue_row["guide_sha256"],
                        observed_guide_url=observed_guide,
                        observed_guide_url_sha256=(
                            sha256_bytes(observed_guide.encode("utf-8")) if observed_guide else None
                        ),
                        link_metadata=link_metadata,
                        operation=declared_operation,
                    )
                    row_operations = [copy.deepcopy(item) for item in row.get("operations", [])]
                    row_operations.append(declared_operation)
                    source_provenance = {
                        "system": "data.go.kr",
                        "page_url": observation.page_url,
                        "effective_url": observation.effective_url,
                        "page_sha256": observation.page_sha256,
                        "observed_at": observation.observed_at,
                    }
                    declaration_record = {
                        "api_key": {"provider": "data.go.kr", "id": identity},
                        "source_sha256": queue_row["source_sha256"],
                        "guide_sha256": queue_row["guide_sha256"],
                        "observed_guide_url": observed_guide,
                        "observed_guide_url_sha256": (
                            sha256_bytes(observed_guide.encode("utf-8")) if observed_guide else None
                        ),
                        "operations": row_operations,
                        "operations_sha256": sha256_bytes(canonical_json(row_operations)),
                        "status": "enriched",
                        "source_provenance": source_provenance,
                        "declaration_provenance": declaration_provenance,
                    }
                    try:
                        SEOUL_DECLARATION.validate_enriched_record(row, declaration_record)
                    except SEOUL_DECLARATION.DeclarationError:
                        raise KnownDeclarationFailure("declaration_evidence_rejected") from None
                    failure_diagnostic = None
                    row_status = "enriched"
                except KnownDeclarationFailure as exc:
                    row_operations = []
                    declaration_provenance = None
                    row_status = "quarantined"
                    failure_diagnostic = {
                        "code": "resolved_link_operation_contract_unproven",
                        "phase": "resolver",
                        "contract_failure": contract_failure_value(exc.reason),
                    }
                except (SEOUL_DECLARATION.DeclarationError, KeyError, TypeError, ValueError):
                    row_operations = []
                    declaration_provenance = None
                    row_status = "quarantined"
                    failure_diagnostic = {
                        "code": "resolved_link_operation_contract_unproven",
                        "phase": "resolver",
                        "contract_failure": contract_failure_value("validation_detail_unknown"),
                    }
                break
            except Exception as exc:
                diagnostic = classify_detail_exception(exc)
                if failure_context == "resolver_validation" and is_terminal_contract_error(exc):
                    reason = str(exc)
                    code = (
                        "unsafe_or_unregistered_operation_host"
                        if "unsafe" in reason or "unregistered" in reason
                        else "contract_or_parse_error"
                    )
                    row_status = "quarantined"
                    failure_diagnostic = {"code": code, "phase": "resolver"}
                    break
                if failure_context == "resolver_fetch" and isinstance(exc, LinkResolverContractError):
                    row_status = "quarantined"
                    failure_diagnostic = {"code": "contract_or_parse_error", "phase": "resolver"}
                    break
                failure_diagnostic = {**diagnostic, "phase": "resolver"}
                row_status = "retry"
                if attempts_available > 0 and attempts_this_invocation < attempt_budget:
                    sleeper(min(5.0, 0.25 * (2 ** max(0, attempted_by_id.get(identity, 1) - 1))))
        if current_template is not None and resolver_attempts == 0 and row_status == "retry":
            failure_diagnostic = {"code": "insufficient_budget_for_link_resolver", "phase": "resolver"}
        # Request-budget exhaustion is accounted in the reservation counters;
        # it must not replace the last observed failure diagnostic.
        if row_status == "enriched":
            attempt_counts.pop(identity, None)
            refunded_reservations += max(0, reserved_count - attempted_by_id.get(identity, 0))
        else:
            remaining_reserved = max(0, reserved_count - attempted_by_id.get(identity, 0))
            refunded_reservations += remaining_reserved
            remaining_lifetime = max(0, int(attempt_counts.get(identity, 0)) - remaining_reserved)
            if remaining_lifetime:
                attempt_counts[identity] = remaining_lifetime
            else:
                attempt_counts.pop(identity, None)
        fingerprint = queue_row["source_sha256"]
        guide_fingerprint = queue_row["guide_sha256"]
        worker_record = {
            "id": identity, "status": row_status,
            "source_sha256": fingerprint, "guide_sha256": guide_fingerprint,
        }
        if row_status != "enriched" and failure_diagnostic is not None:
            worker_record["failure_diagnostic"] = validate_failure_diagnostic(
                failure_diagnostic, identity=identity,
            )
        if row_status != "enriched" and link_metadata is not None:
            worker_record["link_metadata"] = link_metadata
        worker_records.append(worker_record)
        if row_status == "enriched":
            enriched_record = {
                "api_key": {"provider": "data.go.kr", "id": identity},
                "status": "enriched",
                "source_sha256": fingerprint,
                "guide_sha256": guide_fingerprint,
                "observed_guide_url": observed_guide,
                "observed_guide_url_sha256": sha256_bytes(observed_guide.encode("utf-8")) if observed_guide else None,
                "operations": row_operations,
                "operations_sha256": sha256_bytes(canonical_json(row_operations)),
                "source_provenance": source_provenance,
            }
            if declaration_provenance is not None:
                enriched_record["declaration_provenance"] = declaration_provenance
            enriched_records.append(enriched_record)
        checkpoint["detail_records"] = worker_records[-128:]
        checkpoint["last_heartbeat_at"] = timestamp(now_fn())
        checkpoint["last_progress_at"] = timestamp(now_fn())
        atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))

    checkpoint["detail_records"] = worker_records[-128:]
    checkpoint["attempts_consumed"] = max(0, int(checkpoint.get("attempts_consumed", 0)) - refunded_reservations)
    reservation["attempts_made"] = attempts_this_invocation
    reservation["reserved_attempts"] = attempts_this_invocation
    reservation["records"] = [
        {
            "id": record["id"], "attempts_reserved": attempted_by_id[record["id"]],
            "source_sha256": record["source_sha256"], "guide_sha256": record["guide_sha256"],
        }
        for record in reservation.get("records", []) if attempted_by_id.get(record["id"], 0)
    ]

    unresolved_worker_outcomes: list[dict[str, Any]] = []
    for row in worker_records:
        if row.get("status") not in {"retry", "quarantined"}:
            continue
        outcome = {
            "api_key": {"provider": "data.go.kr", "id": row["id"]},
            "status": row["status"],
            "source_sha256": row["source_sha256"],
            "guide_sha256": row["guide_sha256"],
        }
        if "failure_diagnostic" in row:
            outcome["failure_diagnostic"] = validate_failure_diagnostic(
                row["failure_diagnostic"], identity=str(outcome["api_key"]["id"]),
            )
        if "link_metadata" in row:
            outcome["link_metadata"] = DETAIL_HELPERS.validate_link_metadata(
                row["link_metadata"], row["id"], registered_hosts,
            )
        unresolved_worker_outcomes.append(outcome)
    for row in unqueued:
        outcome = {
            "api_key": {"provider": "data.go.kr", "id": row["id"]},
            "status": "retry",
            "source_sha256": row["source_sha256"],
            "guide_sha256": row["guide_sha256"],
        }
        diagnostic = retained_failure_diagnostics.get(row["id"])
        if diagnostic is not None:
            outcome["failure_diagnostic"] = validate_failure_diagnostic(
                diagnostic, identity=str(outcome["api_key"]["id"]),
            )
        unresolved_worker_outcomes.append(outcome)
    detail_failure_counts: dict[str, int] = {}
    for row in unresolved_worker_outcomes:
        diagnostic = row.get("failure_diagnostic")
        if isinstance(diagnostic, dict):
            code = validate_failure_diagnostic(diagnostic, identity=str(row["api_key"]["id"]))["code"]
            detail_failure_counts[code] = detail_failure_counts.get(code, 0) + 1
    detail_failure_counts = dict(sorted(detail_failure_counts.items()))
    detail_reason_unavailable_count = sum("failure_diagnostic" not in row for row in unresolved_worker_outcomes)
    detail_unattempted_count = sum(
        row["api_key"]["id"] not in attempted_by_id for row in unresolved_worker_outcomes
    )

    enrichment = {
        "schema_version": ENRICHMENT_SCHEMA,
        "original_candidate_sha256": candidate_sha,
        "provider_index_sha256": adapter_sha,
        "adapter_revision": adapter_sha,
        "extractor_revision": extractor_revision(),
        "records": enriched_records,
        "worker_outcomes": unresolved_worker_outcomes,
    }
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_name in (
        "composed-candidate.registry.json", "ready-scope.registry.json", "semantic-diff.json",
        "regeneration-queue.json", "quarantine.json", "composition-receipt.json",
    ):
        (output_dir / stale_name).unlink(missing_ok=True)
    enrichment_path = atomic_output(output_dir, "upstream-catalogue-enrichment-evidence.json", enrichment)
    checkpoint["status"] = "composing"
    ensure_fence(checkpoint, processor_run_id, token, now_fn())
    checkpoint["last_heartbeat_at"] = timestamp(now_fn())
    checkpoint["last_progress_at"] = timestamp(now)
    atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
    checkpoint["last_heartbeat_at"] = timestamp(now_fn())
    atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
    # No fixture composer path is accepted by the operational CLI. Tests can
    # supply a temporary executable; production always uses the reviewed CLI.
    if args.fixture_composer:
        if not args.allow_fixture_composer:
            raise ValueError("fixture composer requires explicit test-only flag")
        composer_command = args.fixture_composer
    else:
        composer_command = args.composer
    checkpoint["status"] = "composing"
    composer_output_dir = output_dir / "composer-output"
    if composer_output_dir.exists():
        import shutil
        shutil.rmtree(composer_output_dir)
    code, reason = call_composer(
        composer_command, baseline=baseline_path, candidate=candidate_path, diff=diff_path,
        refresh_evidence=args.refresh_evidence, provider_index=args.provider_index,
        source_policy=args.source_policy, producer_run_id=args.producer_run_id,
        producer_run_url=args.producer_run_url, output_dir=composer_output_dir, enrichment=enrichment_path,
        composition_baseline=composition_baseline_path if derivation is not None else None,
        same_observation_derivation=args.same_observation_derivation if derivation is not None else None,
    )
    ensure_fence(checkpoint, processor_run_id, token, now_fn())
    if code == 0:
        for name in (
            "composed-candidate.registry.json", "ready-scope.registry.json", "semantic-diff.json",
            "regeneration-queue.json", "quarantine.json", "composition-receipt.json",
        ):
            source_path = composer_output_dir / name
            if source_path.is_file():
                source_path.replace(output_dir / name)
        import shutil
        shutil.rmtree(composer_output_dir, ignore_errors=True)
    if code != 0:
        checkpoint.update({"status": "retry", "outcome": {"reason": reason}, "lease": None})
        checkpoint["last_heartbeat_at"] = timestamp(now_fn())
        partial_bundle = [
            {"path": name, "sha256": file_sha256(output_dir / name), "bytes": (output_dir / name).stat().st_size}
            for name in ("upstream-catalogue-enrichment-evidence.json",)
            if (output_dir / name).is_file()
        ]
        set_output_artifact_locator(checkpoint, args, output_artifact_expires_at, partial_bundle)
        atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
        atomic_write_json(output_dir / "upstream-catalogue-checkpoint-receipt.json", checkpoint)
        append_generation_index(index_path, checkpoint_path, checkpoint)
        return 2, checkpoint

    valid, receipt_status, receipt = composer_outputs_valid(output_dir, str(candidate_sha), baseline_sha)
    if not valid:
        checkpoint.update({"status": "quarantined", "outcome": {"reason": receipt_status}, "lease": None})
        checkpoint["last_heartbeat_at"] = timestamp(now_fn())
        partial_bundle = [
            {"path": path.name, "sha256": file_sha256(path), "bytes": path.stat().st_size}
            for path in sorted(output_dir.iterdir()) if path.is_file() and path.suffix == ".json"
        ][:16]
        set_output_artifact_locator(checkpoint, args, output_artifact_expires_at, partial_bundle)
        atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
        atomic_write_json(output_dir / "upstream-catalogue-checkpoint-receipt.json", checkpoint)
        append_generation_index(index_path, checkpoint_path, checkpoint)
        return 3, checkpoint

    scope = (receipt or {}).get("scope") if isinstance((receipt or {}).get("scope"), dict) else {}
    worker_scope_ok, scope_error, scope_pending_count = verify_worker_scope(
        receipt_status, receipt or {}, worker_records, unqueued,
    )
    if not worker_scope_ok:
        checkpoint.update({"status": "quarantined", "outcome": {"reason": scope_error}, "lease": None})
        checkpoint["last_heartbeat_at"] = timestamp(now_fn())
        for name in (
            "composed-candidate.registry.json", "ready-scope.registry.json", "semantic-diff.json",
            "regeneration-queue.json", "quarantine.json", "composition-receipt.json",
            "upstream-catalogue-enrichment-evidence.json",
        ):
            (output_dir / name).unlink(missing_ok=True)
        sealed = persist_result_only_checkpoint(
            checkpoint,
            args,
            checkpoint_path=checkpoint_path,
            index_path=index_path,
            output_expires_at=output_artifact_expires_at,
        )
        return 3, sealed
    pending_count = int(
        (receipt or {}).get("pending_count")
        or (receipt or {}).get("summary", {}).get("pending", 0)
        or scope_pending_count
    )
    worker_retry_count = sum(row.get("status") == "retry" for row in worker_records) + len(unqueued)
    if receipt_status in {"ready", "ready_scoped"}:
        terminal_status = "ready"
        outcome_reason = "composed_candidate_ready" if receipt_status == "ready" else "scoped_candidate_ready_pending_outcomes_retained"
    elif receipt_status == "no_change" and worker_retry_count == 0 and pending_count == 0:
        terminal_status = "no-change"
        outcome_reason = "genuine_no_change"
    elif receipt_status == "no_safe_change" or (receipt_status == "no_change" and (worker_retry_count or pending_count)):
        terminal_status = "retry"
        outcome_reason = "pending_detail_or_no_safe_change"
    elif receipt_status in {"quarantined", "global_invalid", "invalid"}:
        terminal_status = "quarantined"
        outcome_reason = "composer_quarantined"
    else:
        terminal_status = "retry"
        outcome_reason = "composer_pending"
    checkpoint.update({
        "status": terminal_status,
        "outcome": {
            "reason": outcome_reason, "composer_status": receipt_status,
            "pending_count": pending_count, "detail_retry_count": worker_retry_count,
            "attempts_this_invocation": attempts_this_invocation,
            "detail_failure_counts": detail_failure_counts,
            "detail_reason_unavailable_count": detail_reason_unavailable_count,
            "detail_unattempted_count": detail_unattempted_count,
        },
        "lease": None,
        "last_heartbeat_at": timestamp(now_fn()),
    })
    atomic_output(
        output_dir,
        "upstream-catalogue-processing-result.json",
        processing_result(
            checkpoint,
            producer_run_id=args.producer_run_id,
            processor_run_id=processor_run_id,
            processor_artifact_run_id=args.processor_artifact_run_id or processor_run_id,
        ),
    )
    bundle = []
    for name in (
        "composed-candidate.registry.json", "ready-scope.registry.json", "semantic-diff.json",
        "regeneration-queue.json", "quarantine.json", "composition-receipt.json",
        "upstream-catalogue-enrichment-evidence.json", "upstream-catalogue-processing-result.json",
    ):
        path = output_dir / name
        bundle.append({"path": name, "sha256": file_sha256(path), "bytes": path.stat().st_size})
    set_output_artifact_locator(checkpoint, args, output_artifact_expires_at, bundle)
    atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
    atomic_write_json(output_dir / "upstream-catalogue-checkpoint-receipt.json", checkpoint)
    retained_generation_ids = append_generation_index(index_path, checkpoint_path, checkpoint)
    prune_generation_files(
        generation_dir, checkpoint_path, retained_ids=retained_generation_ids,
    )
    return (0 if terminal_status in {"ready", "no-change"} else 2 if terminal_status == "retry" else 3), checkpoint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="data_go_kr")
    parser.add_argument("--source-scope", default="aggregate_supported_catalog")
    parser.add_argument("--baseline", type=pathlib.Path)
    parser.add_argument("--composition-baseline", type=pathlib.Path)
    parser.add_argument("--same-observation-derivation", type=pathlib.Path)
    parser.add_argument("--expected-generation-id")
    parser.add_argument("--current-main-root", type=pathlib.Path)
    parser.add_argument("--derivation-journal", type=pathlib.Path)
    parser.add_argument("--derivation-journal-ref-sha")
    parser.add_argument("--resume-parent-bundle-dir", type=pathlib.Path)
    parser.add_argument("--canonical-parent-bundle-dir", type=pathlib.Path)
    parser.add_argument("--canonical-update-pr-helper", type=pathlib.Path, default=pathlib.Path("scripts/canonical_update_pr.py"))
    parser.add_argument("--canonical-update-journal-schema", type=pathlib.Path, default=pathlib.Path("schemas/datapan.canonical-update-promotion-journal.v1.schema.json"))
    parser.add_argument("--candidate", type=pathlib.Path)
    parser.add_argument("--diff", type=pathlib.Path)
    parser.add_argument("--refresh-evidence", type=pathlib.Path)
    parser.add_argument("--source-policy", type=pathlib.Path, default=pathlib.Path("policy/source-refresh.json"))
    parser.add_argument("--provider-index", type=pathlib.Path, default=pathlib.Path("data/provider-index.json"))
    parser.add_argument("--state-dir", type=pathlib.Path, default=pathlib.Path(".datapan/upstream-catalogue-state"))
    parser.add_argument("--output-dir", type=pathlib.Path, default=pathlib.Path(".datapan/ci/upstream-catalogue-processing"))
    parser.add_argument("--checkpoint-schema", type=pathlib.Path, default=pathlib.Path("schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"))
    parser.add_argument("--composer", type=pathlib.Path, default=pathlib.Path("scripts/compose-upstream-catalogue-candidate.py"))
    parser.add_argument("--fixture-composer", type=pathlib.Path, help=argparse.SUPPRESS)
    parser.add_argument("--allow-fixture-composer", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--producer-run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    parser.add_argument("--processor-run-id")
    parser.add_argument("--processor-artifact-run-id")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "local/local"))
    parser.add_argument("--execution-mode", choices=["live", "fixture"], default="fixture")
    parser.add_argument("--producer-run-url", default=os.environ.get("GITHUB_SERVER_URL", "") + "/" + os.environ.get("GITHUB_REPOSITORY", "") + "/actions/runs/" + os.environ.get("GITHUB_RUN_ID", "local"))
    parser.add_argument("--artifact-name")
    parser.add_argument("--producer-head-sha")
    parser.add_argument("--producer-run-attempt", type=int)
    parser.add_argument("--producer-run-started-at")
    parser.add_argument("--producer-run-completed-at")
    parser.add_argument("--producer-observe-job-started-at")
    parser.add_argument("--producer-observe-job-completed-at")
    parser.add_argument("--producer-artifact-created-at")
    parser.add_argument("--producer-artifact-digest-sha256")
    parser.add_argument("--producer-artifact-size-bytes", type=int)
    parser.add_argument("--producer-event", choices=["schedule", "workflow_dispatch"])
    parser.add_argument("--collector-admission-file", type=pathlib.Path, help=argparse.SUPPRESS)
    parser.add_argument("--collector-archive", type=pathlib.Path, help=argparse.SUPPRESS)
    parser.add_argument("--resume-enrichment-evidence", type=pathlib.Path)
    parser.add_argument("--input-artifact-id")
    parser.add_argument("--artifact-expires-at")
    parser.add_argument("--output-artifact-expires-at")
    parser.add_argument("--bind-output-artifact-id")
    parser.add_argument("--bind-output-artifact-expires-at")
    parser.add_argument("--bind-generation-id")
    parser.add_argument("--target-generation-id", help="mark an existing generation quarantined when its exact input artifact is unavailable")
    parser.add_argument("--recover-failed-processor-run-id")
    parser.add_argument("--recover-failed-generation-id")
    parser.add_argument("--failed-run-metadata", type=pathlib.Path)
    parser.add_argument("--failed-artifact-metadata", type=pathlib.Path)
    parser.add_argument("--failed-artifact-dir", type=pathlib.Path)
    parser.add_argument("--expected-checkpoint-sha256")
    parser.add_argument("--expected-state-head-sha")
    parser.add_argument("--current-head-sha")
    parser.add_argument("--replay-state-head-sha", help=argparse.SUPPRESS)
    parser.add_argument("--claim-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--require-durable-reservation", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--input-error", choices=["artifact_missing", "candidate_artifact_missing", "input_expired", "observation_failure", "collection_failure"])
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--max-queue", type=int, default=DEFAULT_MAX_QUEUE)
    parser.add_argument("--retries-per-detail", type=int, default=DEFAULT_RETRIES_PER_DETAIL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--now", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        not 1 <= args.max_attempts <= DEFAULT_MAX_ATTEMPTS
        or not 1 <= args.max_queue <= DEFAULT_MAX_QUEUE
        or not 0 <= args.retries_per_detail <= DEFAULT_RETRIES_PER_DETAIL
        or not 1 <= args.timeout <= 30
    ):
        print("FAIL upstream catalogue processor: invalid request bounds", file=sys.stderr)
        return 1
    try:
        if args.bind_output_artifact_id:
            if (
                not args.bind_generation_id or not args.processor_run_id
                or not args.processor_artifact_run_id or not args.bind_output_artifact_expires_at
            ):
                raise ValueError("artifact_binding_identity_required")
            bind_output_artifact_id(
                args.state_dir, args.source, args.bind_generation_id,
                args.bind_output_artifact_id, args.checkpoint_schema,
                processor_run_id=args.processor_run_id,
                processor_artifact_run_id=args.processor_artifact_run_id,
                artifact_expires_at=args.bind_output_artifact_expires_at,
            )
            return 0
        if args.recover_failed_processor_run_id:
            if (
                not args.recover_failed_generation_id
                or args.target_generation_id or args.candidate or args.diff
                or args.refresh_evidence or args.claim_only or args.require_durable_reservation
            ):
                raise ValueError("failed_processor_recovery_arguments_conflict")
            status, checkpoint = recover_failed_processor_generation(args)
        else:
            if any((
                args.recover_failed_generation_id, args.failed_run_metadata, args.failed_artifact_metadata,
                args.failed_artifact_dir, args.expected_checkpoint_sha256, args.expected_state_head_sha,
                args.current_head_sha,
            )):
                raise ValueError("failed_processor_recovery_arguments_incomplete")
            status, checkpoint = process(args)
        exact_delivery_replay = bool(getattr(args, "exact_delivery_replay", False))
        result = processing_result(
            checkpoint,
            exact_delivery_replay=exact_delivery_replay,
            producer_run_id=args.producer_run_id,
            processor_run_id=args.processor_run_id or args.producer_run_id,
            processor_artifact_run_id=args.processor_artifact_run_id or args.processor_run_id or args.producer_run_id,
        )
        atomic_output(args.output_dir, "upstream-catalogue-processing-result.json", result)
        if checkpoint.get("schema_version") == CHECKPOINT_SCHEMA and checkpoint.get("checkpoint_sha256") == checkpoint_digest(checkpoint):
            if exact_delivery_replay:
                replay_context = getattr(args, "exact_delivery_replay_context", None)
                if replay_context is not None:
                    receipt = build_replay_persistence_receipt(result=result, **replay_context)
                    atomic_output(args.output_dir, "upstream-catalogue-checkpoint-receipt.json", checkpoint)
                    atomic_output(args.output_dir, REPLAY_PERSISTENCE_RECEIPT, receipt)
            else:
                atomic_output(args.output_dir, "upstream-catalogue-checkpoint-receipt.json", checkpoint)
        print(json.dumps(result, sort_keys=True))
        return status
    except Exception as exc:  # noqa: BLE001
        result = {"status": "retry", "reason": safe_error_class(exc), "run_id": args.producer_run_id}
        try:
            atomic_output(args.output_dir, "upstream-catalogue-processing-result.json", result)
        except OSError:
            pass
        print(f"FAIL upstream catalogue processor: {safe_error_class(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
