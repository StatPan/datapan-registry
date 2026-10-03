#!/usr/bin/env python3
"""Resume bounded detail enrichment and compose one upstream catalogue candidate.

This worker never replaces the producer's candidate bytes. Enrichment is emitted
as separate digest-bound evidence for the reviewed composer interface.
"""

from __future__ import annotations

import argparse
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
from typing import Any, Callable

CHECKPOINT_SCHEMA = "datapan.upstream-catalogue-checkpoint.v1"
ENRICHMENT_SCHEMA = "datapan.catalogue-enrichment-evidence.v1"
STATE_FILE_LIMIT = 256 * 1024
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_DETAIL_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
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
    return file_sha256(pathlib.Path(__file__))


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


DETAIL_HELPERS = load_detail_helpers()


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


class SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_host: str = "www.data.go.kr") -> None:
        super().__init__()
        self.allowed_host = allowed_host
        self.redirects = 0

    def redirect_request(self, request: urllib.request.Request, response: Any, code: int, message: str, headers: Any, new_url: str) -> urllib.request.Request | None:
        self.redirects += 1
        parsed = urllib.parse.urlsplit(new_url)
        if (
            self.redirects > MAX_REDIRECTS or parsed.scheme != "https" or parsed.hostname != self.allowed_host
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or new_url != request.full_url
        ):
            raise urllib.error.URLError("redirect_outside_allowed_public_detail_host")
        return super().redirect_request(request, response, code, message, headers, new_url)


def fetch_public_detail(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> DetailPageObservation:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "www.data.go.kr" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("unsafe_detail_url")
    request = urllib.request.Request(url, headers={"User-Agent": "datapan-registry-continuous-catalogue/1.0"})
    opener = urllib.request.build_opener(SameHostRedirectHandler())
    with opener.open(request, timeout=timeout) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != "www.data.go.kr" or final.username or final.password or response.geturl() != url:
            raise ValueError("redirect_outside_allowed_public_detail_host")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_DETAIL_BYTES:
            raise ValueError("detail_response_too_large")
        body_bytes = response.read(MAX_DETAIL_BYTES + 1)
        if len(body_bytes) > MAX_DETAIL_BYTES:
            raise ValueError("detail_response_too_large")
    return DetailPageObservation(
        body=body_bytes.decode("utf-8", errors="replace"), page_url=url, effective_url=response.geturl(),
        page_sha256=sha256_bytes(body_bytes), observed_at=timestamp(), page_bytes=body_bytes,
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
            or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("source_sha256") or ""))
            or row.get("guide_sha256") is not None and not re.fullmatch(r"[a-f0-9]{64}", str(row.get("guide_sha256")))
            or not isinstance(row.get("attempts"), int) or row["attempts"] < 1
        ):
            raise ValueError("corrupt_detail_retry_state")
        parse_timestamp(str(row.get("last_attempt_at")))
    return retry_state


def reserve_requests(
    checkpoint: dict[str, Any], queue: list[dict[str, Any]], *, cursor: int,
    processor_run_id: str, max_attempts: int, max_queue: int,
    retries_per_detail: int, now: dt.datetime,
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
    selected: list[dict[str, Any]] = []
    for row in rotated[:max_queue]:
        inspected.append(row)
        if int(attempts.get(row["id"], 0)) < max_for_row:
            selected.append(row)
            if len(selected) >= max_attempts:
                break
    reserved: dict[str, int] = {row["id"]: 0 for row in selected}
    budget_left = max_attempts
    # Give each selected identity one request before assigning retries. This
    # keeps later records moving when several earlier pages are unavailable.
    for _round in range(max_for_row):
        for row in selected:
            identity = row["id"]
            already = int(attempts.get(identity, 0)) + reserved[identity]
            if budget_left and already < max_for_row:
                reserved[identity] += 1
                budget_left -= 1
    reservation_records = []
    for row in selected:
        count = reserved[row["id"]]
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
    cursor_advance = len(inspected)
    if queue:
        checkpoint["detail_queue_cursor"] = (cursor + cursor_advance) % len(queue)
    else:
        checkpoint["detail_queue_cursor"] = 0
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


def generation_identity(
    source_id: str,
    source_scope: str,
    baseline_sha256: str | None,
    candidate_sha256: str | None,
    observation_failure_sha256: str | None,
    policy_sha256: str,
    adapter_sha256: str,
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
        not isinstance(evidence, dict) or set(evidence) != expected_top
        or evidence.get("schema_version") != ENRICHMENT_SCHEMA
        or evidence.get("original_candidate_sha256") != owner.get("generation_inputs", {}).get("candidate_sha256")
        or evidence.get("provider_index_sha256") != owner.get("generation_inputs", {}).get("adapter_revision")
        or not isinstance(evidence.get("records"), list)
    ):
        raise ValueError("resume_enrichment_binding_mismatch")
    if (
        evidence.get("provider_index_sha256") != provider_index_sha256
        or evidence.get("adapter_revision") != provider_index_sha256
        or evidence.get("extractor_revision") != extractor_revision()
        or owner.get("generation_inputs", {}).get("candidate_sha256") is None
    ):
        return {}
    expected_record_fields = {
        "api_key", "source_sha256", "guide_sha256", "observed_guide_url",
        "observed_guide_url_sha256", "operations", "operations_sha256", "status", "source_provenance",
    }
    resumed: dict[str, dict[str, Any]] = {}
    for record in evidence["records"]:
        if not isinstance(record, dict) or set(record) != expected_record_fields or record.get("status") != "enriched":
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
        for operation in operations:
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
        resumed[identity] = record
    return resumed


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


def call_composer(
    command: pathlib.Path,
    *, baseline: pathlib.Path, candidate: pathlib.Path, diff: pathlib.Path,
    refresh_evidence: pathlib.Path, provider_index: pathlib.Path, source_policy: pathlib.Path,
    producer_run_id: str, producer_run_url: str, output_dir: pathlib.Path, enrichment: pathlib.Path,
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
    result = subprocess.run(args, text=True, capture_output=True, check=False, timeout=300)  # noqa: S603
    if result.returncode != 0:
        return result.returncode, "composer_failed"
    return 0, "composer_succeeded"


def append_generation_index(index_path: pathlib.Path, checkpoint_path: pathlib.Path, checkpoint: dict[str, Any]) -> None:
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
    index["generations"] = rows[-DEFAULT_MAX_GENERATIONS:]
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
                retry_state[identity] = {
                    "source_sha256": record["source_sha256"],
                    "guide_sha256": record["guide_sha256"],
                    "attempts": attempts,
                    "last_attempt_at": reservation["reserved_at"],
                }
    for record in checkpoint.get("detail_records", []):
        if isinstance(record, dict) and record.get("status") == "enriched":
            retry_state.pop(str(record.get("id") or ""), None)
    if len(retry_state) > DEFAULT_MAX_RETRY_STATES:
        raise ValueError("detail_retry_state_capacity_exceeded")
    atomic_write_json(index_path, index)


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
    locator["artifact_id"] = artifact_id
    locator["expires_at"] = timestamp(expiry)
    atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
    append_generation_index(
        state_dir / "sources" / source_id / "index.json", checkpoint_path, checkpoint,
    )


def prune_generation_files(generation_dir: pathlib.Path, current_path: pathlib.Path, max_generations: int = DEFAULT_MAX_GENERATIONS) -> None:
    checkpoints = sorted(generation_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if len(checkpoints) <= max_generations:
        return
    active = []
    for path in checkpoints:
        try:
            value = load_json(path, maximum_bytes=STATE_FILE_LIMIT)
            if isinstance(value, dict) and value.get("status") in {"queued", "validating", "enriching", "composing", "retry"}:
                active.append(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    if len(active) > DEFAULT_MAX_ACTIVE_GENERATIONS:
        raise ValueError("active_generation_queue_full")
    retained = {path for path in checkpoints if path in active or path == current_path}
    for path in checkpoints:
        if len(retained) >= max_generations:
            break
        retained.add(path)
    for path in checkpoints:
        if path not in retained:
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
    )
    generation_dir = args.state_dir / "sources" / args.source / "generations"
    checkpoint_path = generation_dir / f"{generation_id}.json"
    index_path = args.state_dir / "sources" / args.source / "index.json"
    schema = load_json(args.checkpoint_schema, maximum_bytes=1024 * 1024)
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
        checkpoint["detail_queue_cursor"] = source_queue_cursor(index_path, 0)

    if checkpoint.get("generation_inputs") != generation_inputs:
        raise ValueError("generation identity input mismatch")
    if checkpoint.get("status") not in {"ready", "no-change", "quarantined"}:
        refs = checkpoint.setdefault("input_artifacts", [])
        if not any(isinstance(ref, dict) and ref.get("run_id") == args.producer_run_id for ref in refs):
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
    new_observation_received = not isinstance(prior_observation, dict) or any(
        prior_observation.get(key) != value for key, value in new_observation.items()
    )
    if new_observation_received:
        checkpoint["observation_count"] = int(checkpoint.get("observation_count", 0)) + 1
    checkpoint["last_observation"] = new_observation
    prior_outcome = checkpoint.get("outcome") if isinstance(checkpoint.get("outcome"), dict) else {}
    ready_has_detail_work = (
        checkpoint.get("status") == "ready"
        and int(prior_outcome.get("detail_retry_count", 0) or 0) > 0
    )
    if checkpoint.get("status") in {"ready", "no-change", "quarantined"} and not new_observation_received and not ready_has_detail_work:
        checkpoint["last_heartbeat_at"] = timestamp(now)
        atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
        append_generation_index(index_path, checkpoint_path, checkpoint)
        args.exact_delivery_replay = True
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
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        checkpoint.update({"status": "quarantined", "outcome": {"reason": safe_error_class(exc)}, "lease": None})
        checkpoint["last_heartbeat_at"] = timestamp(now)
        atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
        append_generation_index(index_path, checkpoint_path, checkpoint)
        return 3, checkpoint
    queued, retained, _ = detail_queue(baseline_rows, candidate_rows, all_cached_records, now)
    durable_retry_state = source_retry_state(index_path)
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
    checkpoint["detail_retry_reset_ids"] = sorted(reset_ids)[-DEFAULT_MAX_RETRY_STATES:]
    queue_cursor = source_queue_cursor(index_path, int(checkpoint.get("detail_queue_cursor", 0)))
    reservation = checkpoint.get("request_reservation")
    if args.claim_only:
        reservation, reserved_queue, unqueued = reserve_requests(
            checkpoint, queued, cursor=queue_cursor, processor_run_id=processor_run_id,
            max_attempts=args.max_attempts, max_queue=args.max_queue,
            retries_per_detail=args.retries_per_detail, now=now_fn(),
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
        append_generation_index(index_path, checkpoint_path, checkpoint)
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
        max_for_row = args.retries_per_detail + 1
        reserved_count = reserved_by_id.get(identity, 0)
        used = max(0, int(attempt_counts.get(identity, 0)) - reserved_count)
        attempts_available = reserved_count
        row_status = "retry"
        row_operations: list[dict[str, Any]] = []
        last_error = ""
        source_provenance: dict[str, Any] | None = None
        if not identity.isdigit():
            row_status = "quarantined"
            last_error = "invalid_detail_identity"
        try:
            page_url = candidate_detail_url(row, identity)
        except ValueError:
            page_url = ""
            row_status = "quarantined"
            last_error = "unsafe_candidate_detail_provenance"
        if row_status != "quarantined" and reserved_count == 0:
            row_status = "quarantined"
            last_error = "detail_retry_limit_exhausted"
        while attempts_available > 0 and attempts_this_invocation < attempt_budget:
            if row_status == "quarantined":
                break
            # The request was reserved durably before this invocation began
            # network work, so cancellation cannot refund its retry budget.
            used += 1
            attempts_available -= 1
            attempts_this_invocation += 1
            attempted_by_id[identity] = attempted_by_id.get(identity, 0) + 1
            checkpoint["last_heartbeat_at"] = timestamp(now_fn())
            checkpoint["last_progress_at"] = timestamp(now_fn())
            atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
            try:
                expected_page_url = safe_public_page_url(identity)
                fetched = fetcher(expected_page_url, float(args.timeout))
                if isinstance(fetched, DetailPageObservation):
                    observation = fetched
                else:
                    if args.execution_mode == "live":
                        raise ValueError("live_fetch_missing_page_observation")
                    body = str(fetched)
                    observation = DetailPageObservation(
                        body=body, page_url=expected_page_url, effective_url=expected_page_url,
                        page_sha256=sha256_bytes(body.encode("utf-8")), observed_at=timestamp(now),
                    )
                body = observation.body
                if (
                    observation.page_url != expected_page_url or observation.effective_url != expected_page_url
                    or observation.page_sha256 != sha256_bytes(observation.page_bytes)
                    or parse_timestamp(observation.observed_at) > now_fn() + dt.timedelta(minutes=5)
                ):
                    row_status = "quarantined"
                    last_error = "detail_page_observation_mismatch"
                    break
                urls = DETAIL_HELPERS.extract_link_detail_operation_urls(body)
                missing_hosts = {
                    (urllib.parse.urlsplit(endpoint).hostname or "").lower()
                    for endpoint in urls
                    if not safe_operation(DETAIL_HELPERS.operation(row, endpoint, 0, len(urls)))
                    or (urllib.parse.urlsplit(endpoint).hostname or "").lower() not in registered_hosts
                }
                if missing_hosts:
                    row_status = "quarantined"
                    last_error = "unsafe_or_unregistered_operation_host"
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
                    last_error = ""
                    source_provenance = {
                        "system": "data.go.kr",
                        "page_url": observation.page_url,
                        "effective_url": observation.effective_url,
                        "page_sha256": observation.page_sha256,
                        "observed_at": observation.observed_at,
                    }
                else:
                    row_status = "quarantined"
                    last_error = "missing_link_detail_operations"
                break
            except Exception as exc:  # network failures remain per-identity
                last_error = safe_error_class(exc)
                if attempts_available > 0 and attempts_this_invocation < attempt_budget:
                    sleeper(min(5.0, 0.25 * (2 ** (used - 1))))
        else:
            if attempts_available == 0 and row_status == "retry":
                last_error = "request_budget_exhausted"
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
        worker_records.append({
            "id": identity, "status": row_status,
            "source_sha256": fingerprint, "guide_sha256": guide_fingerprint,
        })
        if row_status == "enriched":
            enriched_records.append({
                "api_key": {"provider": "data.go.kr", "id": identity},
                "status": "enriched",
                "source_sha256": fingerprint,
                "guide_sha256": guide_fingerprint,
                "observed_guide_url": observed_guide,
                "observed_guide_url_sha256": sha256_bytes(observed_guide.encode("utf-8")) if observed_guide else None,
                "operations": row_operations,
                "operations_sha256": sha256_bytes(canonical_json(row_operations)),
                "source_provenance": source_provenance,
            })
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

    enrichment = {
        "schema_version": ENRICHMENT_SCHEMA,
        "original_candidate_sha256": candidate_sha,
        "provider_index_sha256": adapter_sha,
        "adapter_revision": adapter_sha,
        "extractor_revision": extractor_revision(),
        "records": enriched_records,
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
        atomic_write_json(checkpoint_path, seal_checkpoint(checkpoint))
        atomic_write_json(output_dir / "upstream-catalogue-checkpoint-receipt.json", checkpoint)
        append_generation_index(index_path, checkpoint_path, checkpoint)
        return 3, checkpoint
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
    append_generation_index(index_path, checkpoint_path, checkpoint)
    prune_generation_files(generation_dir, checkpoint_path)
    return (0 if terminal_status in {"ready", "no-change"} else 2 if terminal_status == "retry" else 3), checkpoint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="data_go_kr")
    parser.add_argument("--source-scope", default="aggregate_supported_catalog")
    parser.add_argument("--baseline", type=pathlib.Path)
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
    parser.add_argument("--resume-enrichment-evidence", type=pathlib.Path)
    parser.add_argument("--input-artifact-id")
    parser.add_argument("--artifact-expires-at")
    parser.add_argument("--output-artifact-expires-at")
    parser.add_argument("--bind-output-artifact-id")
    parser.add_argument("--bind-output-artifact-expires-at")
    parser.add_argument("--bind-generation-id")
    parser.add_argument("--target-generation-id", help="mark an existing generation quarantined when its exact input artifact is unavailable")
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
        if (
            not exact_delivery_replay
            and checkpoint.get("schema_version") == CHECKPOINT_SCHEMA
            and checkpoint.get("checkpoint_sha256") == checkpoint_digest(checkpoint)
        ):
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
