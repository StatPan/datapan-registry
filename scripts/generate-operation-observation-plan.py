#!/usr/bin/env python3
"""Compile the registered API operation inventory into bounded observation-plan shards."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas/datapan.operation-observation-plan.v1.schema.json"
OUTPUT_DIR = ROOT / "reports/operation-observation-plan"
INDEX_PATH = OUTPUT_DIR / "index.json"
MANIFEST_PATH = ROOT / "reports/data-go-kr/operation-manifest.json"
LEGACY_POLICY_PATH = ROOT / "policy/health-probe-canaries.json"
PROVIDER_INDEX_PATH = ROOT / "data/provider-index.json"
DOCUMENT_EVIDENCE_DIR = ROOT / "reports/operation-document-evidence"
DOCUMENT_EVIDENCE_SCHEMA_PATH = ROOT / "schemas/datapan.operation-document-evidence.v1.schema.json"
DOCUMENT_EVIDENCE_V2_DIR = ROOT / "reports/operation-document-evidence/v2"
DOCUMENT_EVIDENCE_V2_SCOPE_DIR = ROOT / "reports/operation-document-evidence/source-scopes"
DOCUMENT_EVIDENCE_V2_SCHEMA_PATH = ROOT / "schemas/datapan.operation-document-evidence.v2.schema.json"
DOCUMENT_CAPTURE_RECEIPT_V2_SCHEMA_PATH = ROOT / "schemas/datapan.operation-document-capture-receipt.v2.schema.json"
DOCUMENT_WORK_ITEM_V2_SCHEMA_PATH = ROOT / "schemas/datapan.operation-document-work-item.v2.schema.json"
DOCUMENT_RECONCILIATION_V2_SCHEMA_PATH = ROOT / "schemas/datapan.operation-document-reconciliation.v2.schema.json"
DOCUMENT_EVIDENCE_V2_QUEUE_PATH = ROOT / "reports/operation-document-evidence/queue.v2.jsonl"
DOCUMENT_EVIDENCE_V2_RECONCILIATION_PATH = ROOT / "reports/operation-document-evidence/reconciliation.v2.json"
REVIEWED_POLICY_PATH = ROOT / "policy/operation-observation-policies.v1.json"
REVIEWED_POLICY_SCHEMA_PATH = ROOT / "schemas/datapan.operation-observation-policy.v1.schema.json"
RESPONSE_ASSERTION_DIR = ROOT / "reports/operation-response-assertions"
RESPONSE_ASSERTION_SCHEMA_PATH = ROOT / "schemas/datapan.operation-response-assertion.v2.schema.json"
DENOMINATOR_PATHS = {
    "ecos": ROOT / "reports/ecos/operation-denominator.json",
    "kosis": ROOT / "reports/kosis/operation-denominator.json",
    "open_assembly": ROOT / "reports/open-assembly/operation-denominator.json",
    "seoul_open_data": ROOT / "reports/seoul-open-data/operation-denominator.json",
}
SHARD_SIZE = 256
QUOTA_SCOPE_PREFIX = b"datapan.quota-scope.v1\0"
_EVIDENCE_CACHE: dict[Path, tuple[bytes, Any, str]] = {}
_READ_PURPOSE_MARKERS = {
    "조회": ("조회",),
    "목록": ("목록",),
    "검색": ("검색",),
    "현황": ("현황",),
    "retrieve": ("retrieve",),
    "list": ("list",),
    "search": ("search",),
    "read": ("read",),
    "lookup": ("lookup",),
}
_MUTATION_MARKERS = (
    "등록", "수정", "삭제", "변경", "추가", "신청", "취소", "발급", "전송", "처리", "실행", "갱신", "업데이트", "작성", "제출",
    "create", "update", "delete", "write", "submit", "insert", "modify", "cancel", "issue", "send", "execute", "apply", "withdraw", "remove",
)
_ACTION_PARAMETER_NAMES = {"action", "actiontype", "command", "method", "op", "operation", "operationid"}


class PlanError(ValueError):
    pass


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise PlanError(message)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def compact_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def pretty_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            byte_count += len(chunk)
            digest.update(chunk)
    return byte_count, digest.hexdigest()


def artifact_ref(path: Path, root: Path = ROOT) -> dict[str, Any]:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    byte_count, checksum = file_digest(path)
    return {"path": relative, "sha256": checksum, "bytes": byte_count}


def current_revision(root: Path = ROOT) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
    )
    revision = result.stdout.strip()
    fail(len(revision) == 40 and all(char in "0123456789abcdef" for char in revision), "git HEAD is not a full commit SHA")
    return revision


def evidence_ref(ref: dict[str, Any], pointer: str, kind: str) -> dict[str, str]:
    return {
        "artifact_path": ref["path"],
        "sha256": ref["sha256"],
        "json_pointer": pointer,
        "evidence_kind": kind,
    }


def identity_set_digest(operation_ids: list[str]) -> str:
    return sha256(compact_json(sorted(operation_ids)))


def quota_scope_digest(scope_kind: str, scope_key: str) -> str:
    return sha256(QUOTA_SCOPE_PREFIX + scope_kind.encode("utf-8") + b"\0" + scope_key.encode("utf-8"))


def json_pointer_value(document: Any, pointer: str) -> Any:
    fail(pointer.startswith("#"), "evidence pointer must be a URI fragment")
    fragment = pointer[1:]
    fail(not fragment or fragment.startswith("/"), "evidence pointer must be a JSON Pointer")
    value = document
    if not fragment:
        return value
    for raw in fragment[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            fail(token.isdigit(), f"invalid array evidence pointer: {pointer}")
            index = int(token)
            fail(index < len(value), f"evidence pointer does not resolve: {pointer}")
            value = value[index]
        elif isinstance(value, dict):
            fail(token in value, f"evidence pointer does not resolve: {pointer}")
            value = value[token]
        else:
            raise PlanError(f"evidence pointer does not resolve: {pointer}")
    return value


def cached_evidence(path: Path) -> tuple[bytes, Any, str]:
    resolved = path.resolve()
    if resolved not in _EVIDENCE_CACHE:
        data = resolved.read_bytes()
        _EVIDENCE_CACHE[resolved] = (data, json.loads(data), sha256(data))
    return _EVIDENCE_CACHE[resolved]


def check_data_go_manifest(manifest: dict[str, Any]) -> None:
    operations = manifest.get("operations")
    fail(isinstance(operations, list) and len(operations) > 0, "data.go.kr operation manifest must be nonempty")
    ids = [row.get("operation_id") for row in operations]
    fail(all(isinstance(value, str) and value for value in ids), "data.go.kr operation IDs must be nonempty strings")
    fail(len(ids) == len(set(ids)), "duplicate data.go.kr operation IDs")
    for row in operations:
        provenance = row.get("provenance")
        fail(isinstance(provenance, dict), "data.go.kr operation provenance must be an object")
        for field in ("dataset_id", "operation_name", "upstream_operation_key"):
            fail(isinstance(provenance.get(field), str) and provenance[field], f"data.go.kr operation provenance lacks {field}")
        fail(provenance.get("provider") == "data.go.kr", "data.go.kr operation provider identity mismatch")
        fail(provenance.get("source_system") in {"data.go.kr", "safetydata.go.kr"}, "unsupported data.go.kr source system")
    summary = manifest.get("summary", {})
    protocol_counts = Counter(row.get("protocol") for row in operations)
    fail(set(protocol_counts) == {"REST", "SOAP"}, "data.go.kr protocols must be exactly REST and SOAP")
    declared_protocols = summary.get("protocols")
    fail(isinstance(declared_protocols, dict) and set(declared_protocols) == {"REST", "SOAP"}, "manifest protocol summary must have exactly REST and SOAP")
    for protocol, count in protocol_counts.items():
        fail(type(declared_protocols[protocol]) is int and declared_protocols[protocol] >= 0, f"negative or invalid {protocol} count")
        fail(declared_protocols[protocol] == count, f"{protocol} summary does not equal derived operation count")
    fail(type(summary.get("api_operations")) is int and summary["api_operations"] == len(operations) == sum(protocol_counts.values()), "API operation total does not equal derived protocol counts")

    eligibility_counts = Counter(row.get("eligibility", {}).get("status") for row in operations)
    fail(set(eligibility_counts) == {"approval_required", "excluded"}, "manifest eligibility statuses are unsupported or missing")
    declared_eligibility = summary.get("eligibility")
    fail(isinstance(declared_eligibility, dict) and set(declared_eligibility) == {"approval_required", "excluded"}, "manifest eligibility summary must have exactly approval_required and excluded")
    for status, count in eligibility_counts.items():
        fail(type(declared_eligibility[status]) is int and declared_eligibility[status] >= 0, f"negative or invalid {status} count")
        fail(declared_eligibility[status] == count, f"{status} summary does not equal derived operation count")

    exclusions = summary.get("exclusions", {})
    expected_exclusions = {"link_operations", "operationless_catalog_entries", "filedata_catalog_entries"}
    fail(isinstance(exclusions, dict) and set(exclusions) == expected_exclusions, "manifest exclusions must have exactly the supported exclusion counters")
    for key, value in exclusions.items():
        fail(type(value) is int and value >= 0, f"negative or invalid exclusion count: {key}")
    fail(type(summary.get("identity_collisions")) is int and summary["identity_collisions"] == 0, "manifest reports identity collisions")
    fail(type(summary.get("identity_omissions")) is int and summary["identity_omissions"] == 0, "manifest reports identity omissions")


def checked_adapter_id(source_id: str, provider: str, profile: dict[str, Any]) -> str:
    fail(profile.get("source_id") == source_id, f"source profile ID mismatch: {source_id}")
    fail(profile.get("provider") == provider, f"source profile provider mismatch: {source_id}")
    adapter = profile.get("adapter")
    fail(isinstance(adapter, dict), f"source profile adapter missing: {source_id}")
    adapter_id = adapter.get("name")
    fail(isinstance(adapter_id, str) and adapter_id, f"source profile adapter name missing: {source_id}")
    fail(adapter.get("status") == "registered", f"source profile adapter is not registered: {source_id}")
    return adapter_id


def check_partial_catalog_identity_set(
    source_id: str,
    provider: str,
    profile_path: str,
    denominator: dict[str, Any],
    catalog: dict[str, Any],
) -> None:
    fail(catalog.get("source_id") == source_id, f"runtime candidate source ID mismatch: {source_id}")
    fail(catalog.get("provider") == provider, f"runtime candidate provider mismatch: {source_id}")
    fail(catalog.get("source_profile") == profile_path, f"runtime candidate source profile mismatch: {source_id}")
    candidates = catalog.get("candidates")
    fail(isinstance(candidates, list) and candidates, f"runtime candidate set missing: {source_id}")
    candidate_ids = [row.get("candidate_id") for row in candidates if isinstance(row, dict)]
    operation_rows = denominator.get("operations")
    fail(isinstance(operation_rows, list), f"operation denominator rows missing: {source_id}")
    operation_ids = [row.get("operation_id") for row in operation_rows if isinstance(row, dict)]
    fail(len(candidate_ids) == len(candidates) and all(isinstance(item, str) and item for item in candidate_ids), f"runtime candidate identity missing: {source_id}")
    fail(len(operation_ids) == len(operation_rows) and all(isinstance(item, str) and item for item in operation_ids), f"operation denominator identity missing: {source_id}")
    fail(len(candidate_ids) == len(set(candidate_ids)), f"duplicate runtime candidate identity: {source_id}")
    fail(len(operation_ids) == len(set(operation_ids)), f"duplicate operation denominator identity: {source_id}")
    fail(set(candidate_ids) == set(operation_ids), f"registered denominator does not match source candidate identity set: {source_id}")
    fail(catalog.get("summary", {}).get("candidates") == len(candidates), f"runtime candidate summary mismatch: {source_id}")
    candidate_by_id = {row["candidate_id"]: row for row in candidates}
    operation_by_id = {row["operation_id"]: row for row in operation_rows}
    for operation_id in sorted(set(operation_ids)):
        denominator_row = operation_by_id[operation_id]
        candidate_row = candidate_by_id[operation_id]
        fail(denominator_row.get("method") == candidate_row.get("method"), f"registered operation method differs from its candidate binding: {source_id}/{operation_id}")
        fail(denominator_row.get("endpoint_template") == candidate_row.get("endpoint_template"), f"registered operation endpoint differs from its candidate binding: {source_id}/{operation_id}")


def canary_policy_map(policy: dict[str, Any]) -> dict[tuple[str, str], tuple[int, dict[str, Any]]]:
    canaries = policy.get("canaries")
    fail(isinstance(canaries, list), "legacy Health canaries must be an array")
    result: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    legacy_ids: set[str] = set()
    for index, canary in enumerate(canaries):
        selector = (str(canary.get("dataset_id", "")), str(canary.get("operation_name", "")))
        legacy_id = canary.get("operation_id")
        fail(all(selector), "legacy canary selector must have dataset and operation name")
        fail(isinstance(legacy_id, str) and legacy_id, "legacy canary selector ID must be nonempty")
        fail(selector not in result and legacy_id not in legacy_ids, "duplicate legacy canary identity")
        result[selector] = (index, canary)
        legacy_ids.add(legacy_id)
    return result


def _load_json_rejecting_duplicate_keys(path: Path) -> Any:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            fail(key not in result, f"duplicate JSON key in {path}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"invalid operation document evidence JSON: {path}") from exc


def document_evidence_pointers(document: dict[str, Any]) -> list[str]:
    """Return fact-object pointers that carry at least one source locator."""
    pointers: list[str] = []

    def visit(value: Any, pointer: str) -> None:
        if isinstance(value, dict):
            refs = value.get("source_refs")
            if isinstance(refs, list) and refs:
                pointers.append(pointer or "#")
            for key, child in value.items():
                if key == "source_refs":
                    continue
                token = str(key).replace("~", "~0").replace("/", "~1")
                visit(child, f"{pointer}/{token}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{pointer}/{index}")

    visit(document, "#")
    return sorted(set(pointers))


def _load_jsonl_rejecting_duplicate_keys(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(
                    line,
                    object_pairs_hook=lambda pairs: _reject_duplicate_pairs(pairs, path, line_number),
                )
                fail(isinstance(value, dict), f"invalid JSONL row in {path}:{line_number}")
                rows.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"invalid operation-document queue JSONL: {path}") from exc
    return rows


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]], path: Path, line_number: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        fail(key not in result, f"duplicate JSON key in {path}:{line_number}")
        result[key] = value
    return result


def _document_evidence_v2_catalog(
    root: Path,
    operations: list[dict[str, Any]],
    denominator_documents: dict[str, dict[str, Any]],
    operation_manifest_ref: dict[str, Any],
    source_snapshot_ref: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load the committed v2 document evidence without collapsing any source status."""
    schema_path = root / DOCUMENT_EVIDENCE_V2_SCHEMA_PATH.relative_to(ROOT)
    receipt_schema_path = root / DOCUMENT_CAPTURE_RECEIPT_V2_SCHEMA_PATH.relative_to(ROOT)
    work_item_schema_path = root / DOCUMENT_WORK_ITEM_V2_SCHEMA_PATH.relative_to(ROOT)
    reconciliation_schema_path = root / DOCUMENT_RECONCILIATION_V2_SCHEMA_PATH.relative_to(ROOT)
    for path in (schema_path, receipt_schema_path, work_item_schema_path, reconciliation_schema_path):
        fail(path.is_file(), f"operation-document-evidence v2 contract input is missing: {path.relative_to(root)}")
    evidence_validator = jsonschema.Draft202012Validator(
        load_json(schema_path), format_checker=jsonschema.FormatChecker()
    )
    receipt_validator = jsonschema.Draft202012Validator(
        load_json(receipt_schema_path), format_checker=jsonschema.FormatChecker()
    )
    work_item_validator = jsonschema.Draft202012Validator(
        load_json(work_item_schema_path), format_checker=jsonschema.FormatChecker()
    )
    reconciliation_validator = jsonschema.Draft202012Validator(
        load_json(reconciliation_schema_path), format_checker=jsonschema.FormatChecker()
    )

    known: dict[tuple[str, str], dict[str, Any]] = {}
    for operation in operations:
        identity = operation_policy_identity("data_go_kr", "data.go.kr", operation)
        known[("data_go_kr", identity["operation_id"])] = {
            "provider": "data.go.kr",
            "operation": operation,
        }
    for source_id, denominator in denominator_documents.items():
        for operation in denominator.get("operations", []):
            key = (source_id, operation.get("operation_id"))
            fail(key not in known, f"duplicate registered operation identity across source scopes: {key}")
            known[key] = {
                "provider": denominator.get("provider"),
                "operation": operation,
            }

    # Reconcile the full v2 acquisition queue against the exact Data.go.kr manifest.
    # This queue reports acquisition state only; it never changes operation eligibility.
    reconciliation_path = root / DOCUMENT_EVIDENCE_V2_RECONCILIATION_PATH.relative_to(ROOT)
    queue_path = root / DOCUMENT_EVIDENCE_V2_QUEUE_PATH.relative_to(ROOT)
    fail(reconciliation_path.is_file() and queue_path.is_file(), "v2 operation-document queue/reconciliation inputs are missing")
    reconciliation = _load_json_rejecting_duplicate_keys(reconciliation_path)
    errors = list(reconciliation_validator.iter_errors(reconciliation))
    fail(not errors, f"invalid operation-document v2 reconciliation: {errors[0].message if errors else ''}")
    fail(reconciliation.get("manifest_binding") == {
        "path": operation_manifest_ref["path"],
        "sha256": operation_manifest_ref["sha256"],
        "source_snapshot_sha256": source_snapshot_ref["sha256"],
    }, "operation-document v2 reconciliation is bound to another manifest or source snapshot")
    queue = _load_jsonl_rejecting_duplicate_keys(queue_path)
    for row in queue:
        work_item_validator.validate(row)
    gov_rows = [row for row in queue if row.get("operation_identity", {}).get("source_id") == "data_go_kr"]
    expected_gov_ids = sorted(
        operation["operation_id"] for operation in operations
    )
    actual_gov_ids = [row["operation_identity"]["operation_id"] for row in gov_rows]
    fail(actual_gov_ids == sorted(actual_gov_ids), "operation-document v2 queue is not sorted by operation identity")
    fail(actual_gov_ids == expected_gov_ids, "operation-document v2 queue does not match the exact registered Data.go.kr identity set")
    statuses = Counter(row["status"] for row in gov_rows)
    fail(statuses == Counter(reconciliation["summary"]["statuses"]), "operation-document v2 queue statuses differ from the reconciliation report")
    fail(reconciliation["summary"]["registered_api_operations"] == len(operations), "operation-document v2 reconciliation denominator mismatch")
    fail(reconciliation["summary"]["coverage_complete"] is False, "partial source-document acquisition cannot claim complete coverage")

    documents: dict[tuple[str, str], dict[str, Any]] = {}
    sidecar_paths = sorted((root / DOCUMENT_EVIDENCE_V2_DIR.relative_to(ROOT)).glob("*.json"))
    sidecar_paths.extend(sorted((root / DOCUMENT_EVIDENCE_V2_SCOPE_DIR.relative_to(ROOT)).glob("*.json")))
    for path in sidecar_paths:
        document = _load_json_rejecting_duplicate_keys(path)
        errors = list(evidence_validator.iter_errors(document))
        fail(not errors, f"invalid operation-document evidence v2 {path.relative_to(root)}: {errors[0].message if errors else ''}")
        identity = document["identity"]
        source_id = identity["source_id"]
        operation_id = identity["operation_id"]
        key = (source_id, operation_id)
        fail(key in known, f"operation-document v2 evidence is outside the registered denominator: {path.relative_to(root)}")
        fail(key not in documents, f"duplicate operation-document v2 evidence identity: {key}")
        expected = known[key]
        fail(identity["provider"] == expected["provider"], f"operation-document v2 provider identity mismatch: {key}")
        registered = expected["operation"]
        if source_id == "data_go_kr":
            provenance = registered.get("provenance", {})
            expected_identity = {
                "operation_id": operation_id,
                "source_id": "data_go_kr",
                "provider": "data.go.kr",
                "protocol": registered.get("protocol"),
                "dataset_id": provenance.get("dataset_id"),
                "operation_name": provenance.get("operation_name"),
                "upstream_operation_key": provenance.get("upstream_operation_key"),
            }
            for field, value in expected_identity.items():
                fail(identity.get(field) == value, f"operation-document v2 identity mismatch at {field}: {key}")
            expected_path = f"{identity['dataset_id']}-{identity['upstream_operation_key']}.json"
            fail(path.name == expected_path and path.parent.name == "v2", f"operation-document v2 filename does not match its registered identity: {path.relative_to(root)}")
            endpoint = registered_endpoint(registered.get("transport", {}).get("endpoint"))
            if endpoint is None:
                fail(
                    registered.get("call_readiness", {}).get("status") == "endpoint_missing",
                    f"registered operation lacks a canonical endpoint: {key}",
                )
        else:
            fail(path.parent.name == "source-scopes" and path.name == f"{operation_id}.json", f"non-Data.go.kr evidence path does not match its registered identity: {path.relative_to(root)}")
            endpoint = registered_endpoint(registered.get("endpoint_template"))
            fail(endpoint is not None, f"registered operation lacks a canonical endpoint: {key}")
            fail(identity.get("protocol") in {"REST", "SOAP", "HTTP"}, f"operation-document v2 protocol is unsupported: {key}")

        transport = document["transport"]
        host_fact, path_fact = transport.get("host", {}), transport.get("path", {})
        # Partial acquisition is allowed to leave host/path unresolved. If the
        # parser did establish either fact, it must agree exactly with the
        # registered operation identity; unknown evidence must remain unknown.
        endpoint_facts = [] if endpoint is None else [
            ("host", host_fact, endpoint["host"]),
            ("path", path_fact, endpoint["path"]),
        ]
        for field, fact, expected_value in endpoint_facts:
            if fact.get("status") == "documented":
                value = fact.get("value")
                if field == "host":
                    fail(isinstance(value, str) and value.casefold() == expected_value, f"operation-document v2 host differs from its registered endpoint: {key}")
                else:
                    fail(registered_paths_match(value, expected_value), f"operation-document v2 path differs from its registered endpoint: {key}")
            else:
                fail(fact.get("status") in {"unknown", "not_established"}, f"operation-document v2 {field} has an unsupported unresolved status: {key}")
        documented_port = transport.get("port")
        port_refs = transport.get("port_source_refs", [])
        if documented_port is not None:
            fail(type(documented_port) is int and 1 <= documented_port <= 65535, f"operation-document v2 port is invalid: {key}")
            fail(bool(port_refs), f"operation-document v2 port lacks source evidence: {key}")
            if endpoint is not None:
                fail(endpoint.get("port") == documented_port, f"operation-document v2 port differs from its registered endpoint: {key}")
        else:
            fail(not port_refs, f"operation-document v2 has port evidence without a port value: {key}")
        if source_id != "data_go_kr":
            parsed = urlsplit(registered["endpoint_template"] if "://" in registered["endpoint_template"] else "https://" + registered["endpoint_template"].lstrip("/"))
            registered_selectors = dict(parse_qsl(parsed.query, keep_blank_values=True))
            for selector in transport.get("fixed_query_selectors", []):
                if selector.get("status") == "documented":
                    fail(registered_selectors.get(selector["name"]) == selector.get("value"), f"operation-document fixed selector differs from its registered endpoint: {key}")

        source_ids = [binding["source_id"] for binding in document["source_bindings"]]
        fail(len(source_ids) == len(set(source_ids)), f"duplicate v2 source binding IDs: {key}")
        source_id_set = set(source_ids)

        def check_v2_source_refs(value: Any) -> None:
            if isinstance(value, dict):
                refs = value.get("source_refs")
                if refs is not None:
                    fail(isinstance(refs, list), f"invalid v2 source refs: {key}")
                    for ref in refs:
                        locator = ref.get("locator", {})
                        fail(locator.get("source_id") in source_id_set, f"unbound v2 source locator: {key}")
                for child in value.values():
                    check_v2_source_refs(child)
            elif isinstance(value, list):
                for child in value:
                    check_v2_source_refs(child)

        check_v2_source_refs(document)
        ref = artifact_ref(path, root)
        if source_id == "data_go_kr":
            receipt_path = root / "reports/operation-document-evidence/v2/receipts" / path.name
            fail(receipt_path.is_file(), f"operation-document v2 capture receipt is missing: {path.name}")
            receipt = _load_json_rejecting_duplicate_keys(receipt_path)
            receipt_validator.validate(receipt)
            fail(receipt["operation_id"] == operation_id and receipt["evidence_sha256"] == ref["sha256"], f"operation-document v2 receipt binding mismatch: {key}")
        documents[key] = {"document": document, "artifact_ref": ref}
    return documents


def normalize_operation_document_evidence_v2(document: dict[str, Any]) -> dict[str, Any]:
    """Provide a lossless consumer view of v2 source facts and branch classes.

    Raw evidence remains the provenance authority. This view only names the
    fields consumers need and keeps success and numeric HTTP-error branches in
    separate arrays. Every branch retains its full source object so statuses
    such as ``incomplete`` and ``unknown`` cannot be flattened into absence.
    """
    fail(document.get("schema_version") == "datapan.operation-document-evidence.v2", "operation evidence normalizer requires v2")
    response = document.get("response_contract")
    fail(isinstance(response, dict), "operation evidence v2 has no response contract")
    success = response.get("success_branches")
    http_errors = response.get("documented_http_error_branches")
    fail(isinstance(success, list) and isinstance(http_errors, list), "operation evidence v2 branch arrays are missing")

    def branch_view(classification: str, index: int, branch: dict[str, Any]) -> dict[str, Any]:
        return {
            "classification": classification,
            "source_index": index,
            "http_status_code": branch.get("http_status_code"),
            "payload_status": branch.get("payload", {}).get("status"),
            "schema_shape_status": branch.get("schema_shape", {}).get("status"),
            "coded_result_inventory_status": branch.get("coded_result_field_inventory", {}).get("status"),
            "result_collection_status": branch.get("result_collection", {}).get("status"),
            "source": branch,
        }

    parse_status = document.get("parse_status", "unknown")
    if isinstance(parse_status, dict):
        parse_status = parse_status.get("status", "unknown")
    return {
        "schema_version": document["schema_version"],
        "identity": document["identity"],
        "source_parse_status": parse_status,
        "transport": document.get("transport", {}),
        "effect": document.get("effect", {}),
        "parameters": document.get("parameters", []),
        "authentication": document.get("authentication", {}),
        "response": {
            "payload": response.get("payload", {"status": "unknown"}),
            "schema_shape": response.get("schema_shape", {"status": "unknown"}),
            "coded_result_field_inventory": response.get("coded_result_field_inventory", {"status": "unknown"}),
            "result_collection": response.get("result_collection", {"status": "unknown"}),
            "empty_result_semantics": document.get("response_assertion", {}).get("empty_result_semantics", {"status": "unknown"}),
            "success_branches": [branch_view("success", index, branch) for index, branch in enumerate(success)],
            "http_error_branches": [branch_view("provider_error", index, branch) for index, branch in enumerate(http_errors)],
        },
        "raw_source": document,
    }


def apply_document_evidence_v2(record: dict[str, Any], captured: dict[str, Any]) -> None:
    """Attach v2 facts without collapsing unknown or incomplete source states."""
    document = captured["document"]
    normalized = normalize_operation_document_evidence_v2(document)
    ref = captured["artifact_ref"]
    request_plan = record["request_plan"]
    pointers = document_evidence_pointers(document)
    pointers.extend(f"#/source_bindings/{index}" for index in range(len(document["source_bindings"])))
    for pointer in sorted(set(pointers)):
        request_plan["evidence_refs"].append(evidence_ref(ref, pointer, "operation_document"))

    missing = request_plan["missing_fields"]

    def clear(name: str) -> None:
        if name in missing:
            missing.remove(name)

    protocol = normalized["identity"]["protocol"]
    method = normalized["transport"].get("http_method", {})
    if (
        method.get("status") == "documented"
        and method.get("authority_scope") == "operation_specific"
        and method.get("value") in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
        and any(row.get("evidence_kind") == "operation_http_method" for row in method.get("source_refs", []))
    ):
        clear("soap_request_method_authority" if protocol == "SOAP" else "operation_method_authority")
        clear("transport_and_method_authority")

    effect = normalized.get("effect", {})
    if (
        effect.get("status") == "documented"
        and effect.get("classification") == "read_only"
        and effect.get("authority") == "operation_document"
        and effect.get("source_refs")
    ):
        clear("operation_effect_read_only_authority")

    parameters = normalized.get("parameters")
    if isinstance(parameters, list) and parameters:
        if all(
            isinstance(parameter.get("location"), dict)
            and parameter["location"].get("status") == "documented"
            and parameter["location"].get("value") in {"query", "path", "header", "body", "soap_header"}
            and parameter["location"].get("source_refs")
            for parameter in parameters
        ):
            clear("parameter_location")
            clear("parameter_inventory_and_location")
        if all(
            parameter.get("requiredness", {}).get("status") == "documented"
            and parameter.get("requiredness", {}).get("value") in {"required", "optional"}
            and parameter.get("cardinality", {}).get("status") == "documented"
            and type(parameter.get("cardinality", {}).get("minimum")) is int
            and (parameter.get("cardinality", {}).get("maximum") is None or type(parameter.get("cardinality", {}).get("maximum")) is int)
            and parameter.get("requiredness", {}).get("source_refs")
            and parameter.get("cardinality", {}).get("source_refs")
            for parameter in parameters
        ):
            clear("parameter_cardinality")

    authentication = normalized.get("authentication", {})
    auth_is_grounded = (
        authentication.get("status") == "documented"
        and authentication.get("requirement") in {"required", "none"}
        and authentication.get("source_refs")
    )
    if auth_is_grounded and authentication.get("requirement") == "required":
        auth_is_grounded = (
            authentication.get("mechanism") in {"service_key", "api_key", "basic", "oauth2", "mutual_tls", "other"}
            and authentication.get("placement") in {"query", "header", "soap_header"}
            and isinstance(authentication.get("parameter_names"), list)
            and len(authentication["parameter_names"]) == 1
            and isinstance(authentication["parameter_names"][0], str)
            and bool(authentication["parameter_names"][0])
        )
    if auth_is_grounded:
        clear("authentication_placement")

    scheme = normalized["transport"].get("scheme", {})
    if scheme.get("status") == "documented" and scheme.get("value") == "https":
        clear("secure_transport_required")

    # Keep v2 response states explicit in the plan's reason set. In particular,
    # an unknown collection is never translated into an absent collection.
    response = normalized["response"]
    payload = response["payload"]
    if payload.get("status") not in {"documented", "not_applicable"}:
        missing.append("response_payload_kind")
    success_branches = response["success_branches"]
    error_branches = response["http_error_branches"]
    if not success_branches or any(branch["schema_shape_status"] != "complete" for branch in success_branches):
        missing.append("response_success_schema_shape")
    if error_branches and any(branch["schema_shape_status"] != "complete" for branch in error_branches):
        missing.append("response_http_error_branch_shape_incomplete")
    branches = [*success_branches, *error_branches]
    if any(branch["coded_result_inventory_status"] not in {"complete", "not_applicable"} for branch in branches):
        missing.append("response_code_inventory_incomplete_or_ambiguous")
    collection_statuses = [branch["result_collection_status"] for branch in success_branches]
    if not collection_statuses:
        collection_statuses.append(response["result_collection"].get("status"))
    if any(status not in {"documented", "not_applicable"} for status in collection_statuses):
        missing.append("response_collection_semantics_unknown")
    empty = response["empty_result_semantics"]
    if empty.get("status") != "documented" or empty.get("value") not in {"valid", "invalid"}:
        missing.append("response_empty_result_policy_unknown")
    request_plan["missing_fields"] = sorted(set(missing))


def load_document_evidence(
    root: Path,
    operations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Load only digestable, schema-valid document facts bound to manifest identities."""
    evidence_dir = root / DOCUMENT_EVIDENCE_DIR.relative_to(ROOT)
    if not evidence_dir.exists():
        return {}
    schema_path = root / DOCUMENT_EVIDENCE_SCHEMA_PATH.relative_to(ROOT)
    fail(schema_path.is_file(), "operation document evidence schema is missing")
    evidence_schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(
        evidence_schema, format_checker=jsonschema.FormatChecker()
    )
    operations_by_id = {row["operation_id"]: row for row in operations}
    fail(len(operations_by_id) == len(operations), "duplicate manifest operation identity")
    documents: dict[str, dict[str, Any]] = {}
    recognized_name = re.compile(r"^[0-9]+-[A-Za-z0-9][A-Za-z0-9:._-]*\.json$")
    for path in sorted(evidence_dir.glob("*.json")):
        if path.name in {"queue.v1.json", "reconciliation.v1.json", "reconciliation.v2.json"}:
            continue
        fail(recognized_name.fullmatch(path.name) is not None, f"unrecognized operation document evidence artifact: {path.name}")
        document = _load_json_rejecting_duplicate_keys(path)
        errors = list(validator.iter_errors(document))
        fail(not errors, f"invalid operation document evidence {path.name}: {errors[0].message if errors else ''}")
        identity = document["identity"]
        operation_id = identity["operation_id"]
        fail(operation_id in operations_by_id, f"operation document evidence is outside the registered denominator: {path.name}")
        operation = operations_by_id[operation_id]
        provenance = operation.get("provenance", {})
        expected = {
            "operation_id": operation_id,
            "provider": provenance.get("provider"),
            "dataset_id": provenance.get("dataset_id"),
            "protocol": operation.get("protocol"),
            "source_system": provenance.get("source_system"),
            "upstream_operation_key": provenance.get("upstream_operation_key"),
            "operation_name": provenance.get("operation_name"),
        }
        fail(identity == (expected | {"source_refs": identity.get("source_refs")}), f"operation document evidence identity mismatch: {path.name}")
        expected_name = f"{expected['dataset_id']}-{expected['upstream_operation_key']}.json"
        fail(path.name == expected_name, f"operation document evidence filename does not match its identity: {path.name}")
        fail(operation_id not in documents, f"duplicate operation document evidence identity: {operation_id}")

        source_ids = [binding["source_id"] for binding in document["source_bindings"]]
        fail(len(source_ids) == len(set(source_ids)), f"duplicate document source binding ID: {path.name}")
        source_id_set = set(source_ids)

        def check_source_refs(value: Any) -> None:
            if isinstance(value, dict):
                refs = value.get("source_refs")
                if refs is not None:
                    fail(isinstance(refs, list), f"invalid source refs in {path.name}")
                    for ref in refs:
                        locator = ref.get("locator", {})
                        fail(locator.get("source_id") in source_id_set, f"unbound document source locator in {path.name}")
                for child in value.values():
                    check_source_refs(child)
            elif isinstance(value, list):
                for child in value:
                    check_source_refs(child)

        check_source_refs(document)
        effect = document["effect"]
        if effect["classification"] == "read_only":
            fail(effect["status"] == "documented", f"read-only effect lacks documented status: {path.name}")
            fail(effect["authority"] == "operation_document", f"read-only effect lacks operation-document authority: {path.name}")
            fail(any(ref["evidence_kind"] == "operation_effect" for ref in effect["source_refs"]), f"read-only effect lacks an operation-type source locator: {path.name}")
        method = document["transport"]["http_method"]
        if method["value"] is not None:
            fail(method["status"] == "documented", f"operation method lacks documented status: {path.name}")
            fail(method["authority_scope"] == "operation_specific", f"service-level method cannot establish operation method: {path.name}")
            fail(any(ref["evidence_kind"] == "operation_http_method" for ref in method["source_refs"]), f"operation method lacks an operation-specific source locator: {path.name}")
        else:
            fail(method["status"] != "documented", f"documented operation method has no value: {path.name}")

        documents[operation_id] = {
            "document": document,
            "artifact_ref": artifact_ref(path, root),
        }
    return documents


def operation_policy_identity(source_id: str, provider: str, operation: dict[str, Any]) -> dict[str, str]:
    provenance = operation.get("provenance", {})
    identity = {
        "source_id": source_id,
        "operation_id": operation.get("operation_id"),
        "provider": provider,
        "protocol": operation.get("protocol"),
        "dataset_id": provenance.get("dataset_id"),
        "operation_name": provenance.get("operation_name"),
        "upstream_operation_key": provenance.get("upstream_operation_key"),
    }
    fail(all(isinstance(value, str) and value for value in identity.values()), "reviewed policy operation identity is incomplete")
    return identity


def _operation_policy_source_refs(value: Any, document_ref: dict[str, Any]) -> list[dict[str, Any]]:
    refs = value.get("source_refs") if isinstance(value, dict) else None
    fail(isinstance(refs, list) and refs, "response assertion fact has no source evidence references")
    result = []
    for ref in refs:
        fail(isinstance(ref, dict), "response assertion source evidence ref must be an object")
        fail(ref.get("artifact_path") == document_ref["path"], "response assertion fact references another operation document")
        fail(ref.get("sha256") == document_ref["sha256"], "response assertion fact digest differs from its operation document")
        fail(ref.get("evidence_kind") == "operation_document", "response assertion fact must use operation-document evidence")
        fail(isinstance(ref.get("json_pointer"), str) and ref["json_pointer"].startswith("#"), "response assertion source ref lacks a JSON pointer")
        result.append(ref)
    return result


def _validate_assertion_fact_binding(
    assertion_artifact: dict[str, Any],
    document: dict[str, Any],
    document_ref: dict[str, Any],
) -> None:
    assertion = assertion_artifact["assertion"]
    source_groups = [assertion["http_status_source_refs"], assertion["provider_result_code_evidence_refs"]]
    source_groups.extend(item["source_refs"] for item in assertion["required_fields"])
    source_groups.append(assertion["result_collection"]["source_refs"])
    if "provider_result_codes" in assertion:
        source_groups.append(assertion["provider_result_codes"]["source_refs"])
    for group in source_groups:
        _operation_policy_source_refs({"source_refs": group}, document_ref)

    def require_pointer_family(refs: list[dict[str, Any]], pattern: re.Pattern[str], message: str) -> None:
        fail(bool(refs) and all(pattern.fullmatch(ref["json_pointer"]) for ref in refs), message)

    require_pointer_family(
        assertion["http_status_source_refs"],
        re.compile(r"#/response_contract/accepted_http_status_codes"),
        "HTTP status assertion must bind the normalized documented status fact",
    )
    require_pointer_family(
        assertion["provider_result_code_evidence_refs"],
        re.compile(r"#/response_contract/(?:provider_result_codes|schema_shape|coded_result_field_inventory)"),
        "provider result-code status must bind its exact normalized source or schema facts",
    )
    require_pointer_family(
        assertion["result_collection"]["source_refs"],
        re.compile(r"#/response_contract/result_collection"),
        "result collection assertion must bind its normalized source fact",
    )
    for policy_field in assertion["required_fields"]:
        require_pointer_family(
            policy_field["source_refs"],
            re.compile(r"#/response_contract/(?:documented_fields|required_fields)/[0-9]+"),
            "response predicate must bind an exact normalized response-field fact",
        )
    if "provider_result_codes" in assertion:
        require_pointer_family(
            assertion["provider_result_codes"]["source_refs"],
            re.compile(r"#/response_contract/provider_result_codes"),
            "provider code values must bind the normalized result-code fact",
        )

    response_contract = document.get("response_contract")
    fail(isinstance(response_contract, dict), "operation document lacks normalized response-contract facts")

    def resolved_facts(refs: list[dict[str, Any]]) -> list[Any]:
        return [json_pointer_value(document, ref["json_pointer"]) for ref in refs]

    statuses: set[int] = set()
    for target in resolved_facts(assertion["http_status_source_refs"]):
        fail(isinstance(target, dict) and target.get("status") == "documented", "HTTP status evidence is not documented")
        values = target.get("values")
        fail(isinstance(values, list) and values, "HTTP status evidence has no values")
        fail(all(type(code) is int and 200 <= code <= 299 for code in values), "invalid documented HTTP success status")
        statuses.update(values)
    fail(set(assertion["accepted_http_status_codes"]) == statuses, "accepted HTTP statuses differ from documented source facts")

    for policy_field in assertion["required_fields"]:
        minimum = policy_field["cardinality"]["minimum"]
        maximum = policy_field["cardinality"]["maximum"]
        fail(maximum is None or minimum <= maximum, "response assertion field cardinality is inverted")
        if policy_field["path"]["kind"] == "json_pointer":
            fail(minimum == 1 and maximum == 1, "JSON Pointer field predicates must require exactly one resolved node")
        targets = resolved_facts(policy_field["source_refs"])
        matched = False
        for target in targets:
            if not isinstance(target, dict) or target.get("status") != "documented":
                continue
            expected = {key: policy_field[key] for key in ("path", "value_type")}
            actual = {key: target.get(key) for key in ("path", "value_type")}
            if actual == expected:
                matched = True
                break
        fail(matched, "reviewed response predicate path/type differs from documented source facts")

    code_targets = resolved_facts(assertion["provider_result_code_evidence_refs"])
    code_status = assertion["provider_result_code_status"]
    if code_status in {"documented", "expected_success_example"}:
        code_policy = assertion["provider_result_codes"]
        def code_values(value: Any, expected_status: str) -> list[Any]:
            if isinstance(value, dict):
                fail(value.get("status") == expected_status, f"provider code values are not {expected_status}")
                values = value.get("values")
                fail(isinstance(values, list) and values, "documented provider code values are empty")
                return values
            fail(expected_status == "documented" and isinstance(value, list) and value, "documented provider code values are empty")
            return value

        expected_source_status = "documented" if code_status == "documented" else "example_only"
        expected_basis = "documented_code_map" if code_status == "documented" else "official_success_example"
        fail(code_policy["basis"] == expected_basis, "provider result-code predicate basis differs from its evidence status")
        if code_status == "documented":
            code_match = any(
                isinstance(target, dict)
                and target.get("status") == "documented"
                and target.get("path") == code_policy["path"]
                and target.get("value_type") == code_policy["value_type"]
                and code_values(target.get("success_values"), "documented") == code_policy["success_values"]
                and code_values(target.get("error_values"), "documented") == code_policy["error_values"]
                for target in resolved_facts(code_policy["source_refs"])
            )
        else:
            code_match = any(
                isinstance(target, dict)
                and target.get("status") == expected_source_status
                and target.get("path") == code_policy["path"]
                and target.get("value_type") == code_policy["value_type"]
                and code_values(target.get("success_values"), "example_only") == code_policy["success_values"]
                for target in resolved_facts(code_policy["source_refs"])
            )
        fail(code_match, "provider expected-success code differs from its exact documented or official-example source fact")

        code_field_match = any(
            isinstance(target, dict)
            and target.get("status") == "documented"
            and target.get("path") == code_policy["path"]
            and target.get("value_type") == code_policy["value_type"]
            and isinstance(target.get("cardinality"), dict)
            and target["cardinality"].get("status") == "documented"
            for target in response_contract.get("documented_fields", [])
        )
        fail(code_field_match, "provider result-code path/type lacks a documented response-field fact")
        expected_python_type = {"string": str, "integer": int, "number": (int, float), "boolean": bool}[code_policy["value_type"]]
        code_values_to_check = code_policy["success_values"] + code_policy.get("error_values", [])
        for value in code_values_to_check:
            fail(type(value) in (expected_python_type if isinstance(expected_python_type, tuple) else (expected_python_type,)), "provider result code has the wrong type")
        if code_status == "documented":
            fail(
                not any(type(a) is type(b) and a == b for a in code_policy["success_values"] for b in code_policy["error_values"]),
                "provider success and error code sets overlap",
            )
    elif code_status == "none_by_policy":
        schema_facts = [
            target for target in code_targets
            if isinstance(target, dict) and target.get("status") == "complete"
        ]
        inventory_facts = [
            target for target in code_targets
            if isinstance(target, dict)
            and target.get("status") == "documented"
            and target.get("candidates") == []
        ]
        fail(schema_facts and inventory_facts, "code_mode=none needs a complete response shape and a reviewed source inventory with no code candidates")
        payload = response_contract.get("payload")
        fail(
            isinstance(payload, dict)
            and payload.get("status") == "documented"
            and payload.get("kind") == assertion["payload_kind"],
            "response payload kind differs from the documented response format",
        )
    else:
        fail(
            any(isinstance(target, dict) and target.get("status") == "not_applicable" for target in code_targets),
            "coded-response semantics are missing or unknown, not documented as not applicable",
        )

    collection = assertion["result_collection"]
    collection_match = any(
        isinstance(target, dict)
        and target.get("status") == "documented"
        and target.get("path") == collection["path"]
        and target.get("container_path") == collection["container_path"]
        and target.get("item_path") == collection["item_path"]
        and target.get("value_type") == collection["value_type"] == "array"
        and (
            (
                collection.get("container_cardinality") is None
                and isinstance(target.get("container_cardinality"), dict)
                and target["container_cardinality"].get("status") == "not_applicable"
            )
            or (
                isinstance(collection.get("container_cardinality"), dict)
                and isinstance(target.get("container_cardinality"), dict)
                and target["container_cardinality"].get("status") == "documented"
                and all(
                    target["container_cardinality"].get(key) == collection["container_cardinality"].get(key)
                    for key in ("minimum", "maximum")
                )
            )
        )
        for target in resolved_facts(collection["source_refs"])
    )
    fail(collection_match, "result collection differs from documented source facts")

    path_kinds: set[str] = set()
    if "provider_result_codes" in assertion:
        path_kinds.add(assertion["provider_result_codes"]["path"]["kind"])
    path_kinds.update(field["path"]["kind"] for field in assertion["required_fields"])
    for field in ("path", "container_path", "item_path"):
        if collection.get(field) is not None:
            path_kinds.add(collection[field]["kind"])
    expected_kind = "json_pointer" if assertion["payload_kind"] == "json" else "xml_qname_path"
    fail(path_kinds == {expected_kind}, "response assertion path grammar differs from payload kind")


def _validate_assertion_v2_fact_binding(
    assertion_artifact: dict[str, Any],
    document: dict[str, Any],
    document_ref: dict[str, Any],
) -> None:
    """Validate every response-union branch against one exact source-document branch."""
    fail(assertion_artifact.get("schema_version") == "datapan.operation-response-assertion.v2", "response assertion schema version is not v2")
    assertion = assertion_artifact["assertion"]
    response_contract = document.get("response_contract")
    fail(isinstance(response_contract, dict), "operation document lacks normalized response-contract facts")
    if assertion == {"mode": "observation_only"}:
        # The reviewed arm deliberately makes no claim about response health.
        # The artifact's document_evidence field binds these unresolved facts;
        # no parser omission is promoted into a body/status predicate.
        return
    source_branches = response_contract.get("success_branches")
    fail(isinstance(source_branches, list) and source_branches, "operation document lacks exact response branch facts")
    branches = assertion.get("branches")
    fail(isinstance(branches, list) and 1 <= len(branches) <= 16, "response assertion branch count is outside the supported bound")
    branch_ids = [branch.get("branch_id") for branch in branches]
    fail(len(branch_ids) == len(set(branch_ids)), "response assertion repeats a branch ID")

    def source_targets(refs: list[dict[str, Any]], pointer_pattern: re.Pattern[str], message: str) -> list[Any]:
        _operation_policy_source_refs({"source_refs": refs}, document_ref)
        require = [ref["json_pointer"] for ref in refs]
        fail(bool(require) and all(pointer_pattern.fullmatch(pointer) for pointer in require), message)
        return [json_pointer_value(document, pointer) for pointer in require]

    def exact_branch_ref(branch_index: int) -> str:
        return f"#/response_contract/success_branches/{branch_index}"

    def resolve_branch(branch: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        targets = source_targets(
            branch["source_refs"],
            re.compile(r"#/response_contract/success_branches/[0-9]+(?:/(?:http_status_code|payload|schema_shape|schema_source|root_shape|documented_fields/[0-9]+|required_fields/[0-9]+|coded_result_field_inventory|provider_result_codes|result_collection|member_shape_selectors(?:/[0-9]+)?))?"),
            "assertion branch must bind one exact success-branch source record",
        )
        matches: list[tuple[int, dict[str, Any]]] = []
        for target in targets:
            if not isinstance(target, dict):
                continue
            for index, source in enumerate(source_branches):
                if target == source:
                    matches.append((index, source))
        unique = {index: source for index, source in matches}
        fail(len(unique) == 1, "assertion branch does not resolve to exactly one source response branch")
        return next(iter(unique.items()))

    def facts_at(
        refs: list[dict[str, Any]],
        branch_index: int,
        allowed_suffix: tuple[str, ...],
    ) -> list[Any]:
        targets: list[Any] = []
        for ref in _operation_policy_source_refs({"source_refs": refs}, document_ref):
            pointer = ref["json_pointer"]
            allowed = any(
                re.fullmatch(rf"#/response_contract/success_branches/{branch_index}/{suffix}", pointer)
                for suffix in allowed_suffix
            )
            fail(allowed, "response assertion fact ref points outside its selected source branch")
            targets.append(json_pointer_value(document, pointer))
        return targets

    referenced_source_branches: set[int] = set()
    branch_status_maps: list[tuple[set[int], str, dict[str, Any]]] = []
    for branch in branches:
        source_index, source = resolve_branch(branch)
        fail(source_index not in referenced_source_branches, "multiple assertion branches bind the same undivided source response branch")
        referenced_source_branches.add(source_index)
        source_pointer = exact_branch_ref(source_index)

        status_values = branch["selector"]["accepted_http_status_codes"]
        fail(all(type(code) is int and 100 <= code <= 599 for code in status_values), "response branch has an invalid HTTP status selector")
        status_facts = facts_at(branch["http_status_source_refs"], source_index, ("http_status_code",))
        documented_statuses = {target.get("value", target.get("http_status_code")) for target in status_facts if isinstance(target, dict)}
        if not documented_statuses:
            documented_statuses = {target for target in status_facts if type(target) is int}
        source_status = source.get("http_status_code")
        fail(type(source_status) is int, "selected source response branch has no exact HTTP status")
        fail(documented_statuses == {source_status} and set(status_values) == {source_status}, "branch status selector differs from its exact source status")

        payload = source.get("payload", {})
        fail(
            isinstance(payload, dict)
            and payload.get("status") == "documented"
            and payload.get("kind") == assertion["payload_kind"],
            "branch payload kind differs from its exact source response branch",
        )
        shape = source.get("schema_shape", {})
        fail(isinstance(shape, dict) and shape.get("status") == "complete" and shape.get("source_refs"), "selected source response branch shape is incomplete")
        root = source.get("root_shape", {})
        selector = branch["selector"]
        fail(isinstance(root, dict) and root.get("status") == "documented", "selected source response branch root shape is unknown")
        fail(selector["root_kind"] == root.get("kind"), "response branch root selector differs from source facts")
        if selector["root_kind"] == "xml_element":
            fail(selector.get("root_qname") == _clark_qname(root.get("qname"), "response.root_shape.qname"), "XML response branch root QName differs from source facts")
        else:
            fail("root_qname" not in selector, "non-XML response branch must not carry a QName")

        payload_pointer = source_pointer + "/payload"
        schema_pointer = source_pointer + "/schema_shape"
        root_pointer = source_pointer + "/root_shape"
        source_pointer_prefix = re.compile(re.escape(source_pointer) + r"(?:/(?:http_status_code|payload|schema_shape|schema_source|root_shape|documented_fields/[0-9]+|required_fields/[0-9]+|coded_result_field_inventory|provider_result_codes|result_collection|member_shape_selectors(?:/[0-9]+)?))?")
        source_targets(branch["source_refs"], source_pointer_prefix, "branch evidence omits or misbinds its exact source record")
        fail(any(ref["json_pointer"] == payload_pointer for ref in branch["source_refs"]), "response branch omits payload-kind source reference")
        fail(any(ref["json_pointer"] == schema_pointer for ref in branch["source_refs"]), "response branch omits schema-shape source reference")
        fail(any(ref["json_pointer"] == root_pointer for ref in branch["source_refs"]), "response branch omits root-shape source reference")

        expected_path_kind = "json_pointer" if assertion["payload_kind"] == "json" else "xml_qname_path"
        for discriminator in selector["discriminators"]:
            fail(discriminator["path"]["kind"] == expected_path_kind, "branch discriminator grammar differs from payload kind")
            discrim_targets = facts_at(discriminator["source_refs"], source_index, ("documented_fields/[0-9]+", "required_fields/[0-9]+", "member_shape_selectors/[0-9]+"))
            matched = False
            for target in discrim_targets:
                if not isinstance(target, dict) or target.get("status") not in {"documented", "required", "not_present"}:
                    continue
                target_path = target.get("path")
                target_type = target.get("value_type")
                if target_path != discriminator["path"]:
                    continue
                predicate = discriminator["predicate"]
                if predicate == "present":
                    matched = target.get("status") in {"documented", "required"}
                elif predicate == "absent":
                    matched = target.get("status") == "not_present"
                elif predicate == "node_type":
                    matched = target.get("status") in {"documented", "required"} and target_type == discriminator["value_type"]
                elif predicate == "equals_any":
                    supported_values = target.get("enum_values", target.get("values"))
                    matched = (
                        target.get("status") in {"documented", "required"}
                        and target_type == discriminator["value_type"]
                        and isinstance(supported_values, list)
                        and all(any(type(a) is type(b) and a == b for b in supported_values) for a in discriminator["values"])
                    )
                if matched:
                    break
            fail(matched, "branch discriminator is not an exact source-documented member/type selector")

        for field in branch["required_fields"]:
            cardinality = field["cardinality"]
            fail(cardinality["maximum"] is None or cardinality["minimum"] <= cardinality["maximum"], "response assertion field cardinality is inverted")
            if field["path"]["kind"] == "json_pointer":
                fail(cardinality["minimum"] == 1 and cardinality["maximum"] == 1, "JSON Pointer field predicates must require exactly one node")
            field_targets = facts_at(field["source_refs"], source_index, ("documented_fields/[0-9]+", "required_fields/[0-9]+"))
            fail(any(isinstance(target, dict) and target.get("status") in {"documented", "required"} and target.get("path") == field["path"] and target.get("value_type") == field["value_type"] for target in field_targets), "branch response field differs from its source facts")

        code_status = branch["provider_result_code_status"]
        code_targets = facts_at(
            branch["provider_result_code_evidence_refs"], source_index,
            ("schema_shape", "coded_result_field_inventory", "provider_result_codes"),
        )
        code_policy = branch.get("provider_result_codes")
        if code_status == "none_by_policy":
            fail(any(isinstance(target, dict) and target.get("status") == "complete" for target in code_targets), "code_mode=none requires a complete source response schema")
            fail(any(isinstance(target, dict) and target.get("status") == "documented" and target.get("candidates") == [] for target in code_targets), "code_mode=none requires a documented empty result-code candidate inventory")
            fail(code_policy is None, "code_mode=none must not include a provider code map")
        elif code_status in {"documented", "expected_success_example"}:
            fail(isinstance(code_policy, dict), "coded branch lacks a typed provider result-code predicate")
            source_codes = [target for target in code_targets if isinstance(target, dict) and target.get("path") == code_policy["path"] and target.get("value_type") == code_policy["value_type"]]
            fail(len(source_codes) == 1, "provider result-code path/type is not bound to one exact source code fact")
            source_code = source_codes[0]
            if code_status == "documented":
                fail(source_code.get("status") == "documented" and code_policy.get("basis") == "documented_code_map", "documented code predicate has wrong evidence basis")
                if branch["classification"] == "success":
                    fail(code_policy.get("success_values") == source_code.get("success_values", {}).get("values"), "provider success-code set differs from source facts")
                    fail("error_values" not in code_policy, "success branch cannot carry provider error-code values")
                    errors = []
                else:
                    fail(code_policy.get("error_values") == source_code.get("error_values", {}).get("values"), "provider error-code set differs from source facts")
                    fail("success_values" not in code_policy, "provider-error branch cannot carry success-code values")
                    errors = code_policy.get("error_values", [])
                error_classes = code_policy.get("error_classes", []) if branch["classification"] == "provider_error" else []
                fail(all(any(type(row["value"]) is type(value) and row["value"] == value for value in errors) for row in error_classes), "provider error class maps a value outside the documented error set")
                fail(len({(type(row["value"]).__name__, json.dumps(row["value"], sort_keys=True)) for row in error_classes}) == len(error_classes), "provider error class repeats an error value")
                source_classes = source_code.get("error_classes", [])
                for error_class in error_classes:
                    refs = error_class["source_refs"]
                    class_targets = facts_at(refs, source_index, ("provider_result_codes/error_classes/[0-9]+",))
                    fail(any(isinstance(target, dict) and target.get("value") == error_class["value"] and target.get("category") == error_class["category"] for target in class_targets), "provider error category lacks an exact documented meaning fact")
                if source_classes:
                    fail(all(any(item.get("value") == row["value"] and item.get("category") == row["category"] for item in source_classes) for row in error_classes), "provider error category differs from the exact source fact")
            else:
                fail(branch["classification"] == "success", "an official success example cannot classify a provider-error branch")
                fail(source_code.get("status") == "example_only" and code_policy.get("basis") == "official_success_example", "expected-success code example has wrong evidence basis")
                fail(code_policy.get("success_values") == source_code.get("success_values", {}).get("values"), "expected-success code differs from exact source example")
                fail(not code_policy.get("error_values") and not code_policy.get("error_classes"), "success example cannot classify undocumented error codes")
        elif code_status == "not_applicable":
            fail(any(isinstance(target, dict) and target.get("status") == "not_applicable" for target in code_targets), "code-free status is not documented")
            fail(code_policy is None, "not-applicable code status must not include a code map")
        else:
            fail(False, "response branch code semantics are unresolved")

        collection = branch.get("result_collection")
        if collection is not None:
            collection_targets = facts_at(collection["source_refs"], source_index, ("result_collection",))
            expected_collection = collection
            matched_collection = any(
                isinstance(target, dict)
                and target.get("status") == "documented"
                and all(target.get(key) == expected_collection.get(key) for key in ("path", "container_path", "item_path", "value_type"))
                for target in collection_targets
            )
            fail(matched_collection, "branch result collection differs from exact source facts")
            if assertion["payload_kind"] == "json":
                fail(collection.get("path", {}).get("kind") == "json_pointer" and collection.get("item_path") is None, "JSON collection selector has invalid path shape")
            else:
                fail(collection.get("path") is None and collection.get("container_path", {}).get("kind") == "xml_qname_path" and collection.get("item_path", {}).get("kind") == "xml_qname_path", "XML collection selector lacks exact container and item paths")
            fail(branch["empty_result_semantics"] == collection["semantics"], "branch empty semantics differ from collection semantics")
        elif branch["classification"] == "success":
            fail(branch["empty_result_semantics"] == "not_applicable", "successful branch without a collection must use not_applicable empty semantics")
            fail(bool(branch["required_fields"] or selector["discriminators"]), "success branch without a result collection needs an exact documented field predicate")
        else:
            fail(branch["empty_result_semantics"] == "not_applicable", "branch without a collection must use not_applicable empty semantics")

        branch_status_maps.append((set(status_values), selector["root_kind"], selector))

    # Reject same-path selector overlaps unless another shared-path predicate proves
    # the full branch conjunctions disjoint. Distinct member paths remain runtime
    # exact-one decisions because both members can occur in the same object.
    for index, (left_statuses, left_root, left) in enumerate(branch_status_maps):
        for right_statuses, right_root, right in branch_status_maps[index + 1:]:
            if not left_statuses.intersection(right_statuses) or left_root != right_root:
                continue
            if left_root == "xml_element" and left.get("root_qname") != right.get("root_qname"):
                continue
            left_discriminators = left["discriminators"]
            right_discriminators = right["discriminators"]
            if not left_discriminators and not right_discriminators:
                fail(False, "response branches have identical status/root selectors")
            if not left_discriminators or not right_discriminators:
                fail(False, "unqualified response branch overlaps a branch with the same status/root selector")
            shared_path_pairs = [
                (ldisc, rdisc)
                for ldisc in left_discriminators
                for rdisc in right_discriminators
                if ldisc["path"] == rdisc["path"]
            ]
            if not shared_path_pairs:
                continue
            if any(_same_path_discriminators_are_disjoint(ldisc, rdisc) for ldisc, rdisc in shared_path_pairs):
                continue
            fail(False, "response branch selectors have overlapping predicates at one path")


def _selector_value_matches_type(value: Any, value_type: str) -> bool:
    if value_type == "string":
        return type(value) is str
    if value_type == "integer":
        return type(value) is int
    if value_type == "number":
        return type(value) in {int, float}
    if value_type == "boolean":
        return type(value) is bool
    return False


def _same_path_discriminators_are_disjoint(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return true only when two exact predicates on one path cannot both match."""
    left_predicate, right_predicate = left["predicate"], right["predicate"]
    if left_predicate == "absent" or right_predicate == "absent":
        return left_predicate != right_predicate
    if left_predicate == "present" or right_predicate == "present":
        return False

    left_type = left.get("value_type")
    right_type = right.get("value_type")
    numeric_types = {"integer", "number"}
    if left_predicate == right_predicate == "node_type":
        if left_type == right_type:
            return False
        return not (left_type in numeric_types and right_type in numeric_types)

    if left_predicate == right_predicate == "equals_any":
        for left_value in left["values"]:
            for right_value in right["values"]:
                both_numeric = type(left_value) in {int, float} and type(left_value) is not bool and type(right_value) in {int, float} and type(right_value) is not bool
                if (both_numeric and left_value == right_value) or (type(left_value) is type(right_value) and left_value == right_value):
                    return False
        return True

    node_type, exact = (left, right) if left_predicate == "node_type" else (right, left)
    if node_type["predicate"] == "node_type" and exact["predicate"] == "equals_any":
        return not any(_selector_value_matches_type(value, node_type["value_type"]) for value in exact["values"])
    return False
def load_reviewed_operation_policies(
    root: Path,
    operations: list[dict[str, Any]],
    document_evidence: dict[str, dict[str, Any]],
    document_evidence_v2: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    policy_path = root / REVIEWED_POLICY_PATH.relative_to(ROOT)
    policy_schema_path = root / REVIEWED_POLICY_SCHEMA_PATH.relative_to(ROOT)
    assertion_schema_path = root / RESPONSE_ASSERTION_SCHEMA_PATH.relative_to(ROOT)
    fail(policy_path.is_file() and policy_schema_path.is_file() and assertion_schema_path.is_file(), "reviewed operation policy inputs or schemas are missing")
    policy_document = _load_json_rejecting_duplicate_keys(policy_path)
    policy_schema = load_json(policy_schema_path)
    assertion_schema = load_json(assertion_schema_path)
    policy_validator = jsonschema.Draft202012Validator(policy_schema, format_checker=jsonschema.FormatChecker())
    assertion_validator = jsonschema.Draft202012Validator(assertion_schema, format_checker=jsonschema.FormatChecker())
    policy_errors = list(policy_validator.iter_errors(policy_document))
    fail(not policy_errors, f"invalid reviewed operation policy set: {policy_errors[0].message if policy_errors else ''}")

    operations_by_id = {row["operation_id"]: row for row in operations}
    document_evidence_v2 = document_evidence_v2 or {}
    fail(len(operations_by_id) == len(operations), "duplicate operation ID while loading reviewed policies")
    policy_ref = artifact_ref(policy_path, root)
    result: dict[str, dict[str, Any]] = {}
    assertion_refs: dict[str, dict[str, Any]] = {}
    for index, policy in enumerate(policy_document["policies"]):
        identity = policy["identity"]
        operation_id = identity["operation_id"]
        fail(operation_id in operations_by_id, "reviewed policy references an operation outside the registered denominator")
        operation = operations_by_id[operation_id]
        expected_identity = operation_policy_identity("data_go_kr", "data.go.kr", operation)
        fail(identity == expected_identity, f"reviewed policy identity differs from the registered operation: {operation_id}")
        candidates = []
        if operation_id in document_evidence:
            candidates.append(document_evidence[operation_id])
        if ("data_go_kr", operation_id) in document_evidence_v2:
            candidates.append(document_evidence_v2[("data_go_kr", operation_id)])
        fail(candidates, f"reviewed policy operation has no pinned official document evidence: {operation_id}")
        captured_matches = [candidate for candidate in candidates if policy["document_evidence"] == candidate["artifact_ref"]]
        fail(len(captured_matches) == 1, f"reviewed policy must bind exactly one current official document evidence artifact: {operation_id}")
        captured = captured_matches[0]
        if ("data_go_kr", operation_id) in document_evidence_v2:
            fail(captured["artifact_ref"] == document_evidence_v2[("data_go_kr", operation_id)]["artifact_ref"], f"reviewed policy cannot use stale v1 evidence when v2 evidence is available: {operation_id}")
        fail(policy["document_evidence"] == captured["artifact_ref"], f"reviewed policy document digest differs from the pinned operation evidence: {operation_id}")
        fail(operation_id not in result, f"duplicate reviewed policy for operation: {operation_id}")

        assertion_ref = policy["request"]["response_assertion_artifact"]
        expected_assertion_path = f"reports/operation-response-assertions/{operation_id}.json"
        fail(assertion_ref["path"] == expected_assertion_path, f"response assertion path is not bound to its operation identity: {operation_id}")
        assertion_path = root / assertion_ref["path"]
        fail(assertion_path.is_file(), f"reviewed response assertion artifact is missing: {operation_id}")
        actual_assertion_ref = artifact_ref(assertion_path, root)
        fail(assertion_ref == actual_assertion_ref, f"reviewed response assertion artifact digest mismatch: {operation_id}")
        assertion_artifact = _load_json_rejecting_duplicate_keys(assertion_path)
        assertion_errors = list(assertion_validator.iter_errors(assertion_artifact))
        fail(not assertion_errors, f"invalid reviewed response assertion {operation_id}: {assertion_errors[0].message if assertion_errors else ''}")

        expected_source_binding = {key: identity[key] for key in ("source_id", "provider", "protocol")}
        expected_operation_identity = {key: identity[key] for key in ("operation_id", "dataset_id", "operation_name", "upstream_operation_key")}
        fail(assertion_artifact["source_binding"] == expected_source_binding, f"response assertion source binding mismatch: {operation_id}")
        fail(assertion_artifact["operation_identity"] == expected_operation_identity, f"response assertion operation identity mismatch: {operation_id}")
        fail(assertion_artifact["document_evidence"] == captured["artifact_ref"], f"response assertion document digest mismatch: {operation_id}")
        _validate_assertion_v2_fact_binding(assertion_artifact, captured["document"], captured["artifact_ref"])

        result[operation_id] = {
            "index": index,
            "policy": policy,
            "artifact_ref": policy_ref,
            "captured": captured,
            "assertion": assertion_artifact,
            "assertion_ref": actual_assertion_ref,
        }
        assertion_refs[operation_id] = actual_assertion_ref

    profile_ids = [profile["profile_id"] for profile in policy_document["profiles"]]
    fail(len(profile_ids) == len(set(profile_ids)), "duplicate reviewed operation policy profile ID")
    effect_profile_ids = [profile["profile_id"] for profile in policy_document.get("effect_profiles", [])]
    fail(len(effect_profile_ids) == len(set(effect_profile_ids)), "duplicate reviewed operation effect profile ID")

    return result, policy_document, policy_ref, {
        "policy_schema": artifact_ref(policy_schema_path, root),
        "assertion_schema": artifact_ref(assertion_schema_path, root),
        "assertions": assertion_refs,
        "profiles": policy_document["profiles"],
        "effect_profiles": policy_document.get("effect_profiles", []),
    }


def profile_matches_operation(
    profile: dict[str, Any],
    source_id: str,
    provider: str,
    operation: dict[str, Any],
    document: dict[str, Any],
) -> bool:
    """Match a reusable profile only against exact, documented operation facts."""
    selector = profile["selector"]
    identity = operation_policy_identity(source_id, provider, operation)
    if selector["source_id"] != source_id or selector["provider"] != provider or selector["protocol"] != identity["protocol"]:
        return False

    effect = document.get("effect", {})
    documented_effect_match = (
        effect.get("status") == "documented"
        and effect.get("classification") == selector["effect"]
        and effect.get("authority") == "operation_document"
        and any(ref.get("evidence_kind") == "operation_effect" for ref in effect.get("source_refs", []))
    )
    reviewed_effect = profile["request"].get("effect_review")
    reviewed_effect_match = reviewed_effect is not None and reviewed_read_only_effect_matches(
        selector,
        reviewed_effect,
        source_id,
        provider,
        operation,
        document,
    )
    if not documented_effect_match and not reviewed_effect_match:
        return False
    method = document.get("transport", {}).get("http_method", {})
    if not (
        method.get("status") == "documented"
        and method.get("authority_scope") == "operation_specific"
        and str(method.get("value", "")).upper() == selector["method"]
        and any(ref.get("evidence_kind") == "operation_http_method" for ref in method.get("source_refs", []))
    ):
        return False

    auth = document.get("authentication", {})
    auth_selector = selector["authentication"]
    if not (
        auth.get("status") == "documented"
        and auth.get("requirement") == auth_selector["requirement"]
        and auth.get("mechanism") == auth_selector["mechanism"]
        and auth.get("placement") == auth_selector["placement"]
    ):
        return False
    parameter_names = auth.get("parameter_names", [])
    if auth_selector["requirement"] == "none":
        if auth_selector["parameter_name"] is not None or parameter_names:
            return False
    elif not (
        isinstance(auth_selector["parameter_name"], str)
        and len(parameter_names) == 1
        and parameter_names[0] == auth_selector["parameter_name"]
    ):
        return False

    source_parameters = document.get("parameters", [])
    if not isinstance(source_parameters, list):
        return False
    strategy_rows = profile["request"]["parameter_strategies"]
    strategies = {row["name"]: row["strategy"] for row in strategy_rows}
    if len(strategies) != len(strategy_rows) or len({name.casefold() for name in strategies}) != len(strategies):
        return False
    auth_name = auth_selector["parameter_name"]
    source_names = [parameter.get("name") for parameter in source_parameters]
    if any(not isinstance(name, str) or not name for name in source_names) or len({name.casefold() for name in source_names}) != len(source_names):
        return False
    if not set(strategies).issubset(set(source_names)):
        return False
    if any(name.casefold() in {item.casefold() for item in parameter_names} for name in strategies):
        return False
    for index, parameter in enumerate(source_parameters):
        try:
            location = _documented_fact(document, f"#/parameters/{index}/location").get("value")
            requiredness = _documented_fact(document, f"#/parameters/{index}/requiredness").get("value")
            _parameter_cardinality(parameter)
            data_type = _documented_fact(document, f"#/parameters/{index}/data_type").get("value")
        except (PlanError, KeyError, TypeError):
            return False
        if location not in {"query", "path", "header", "body", "soap_header"}:
            return False
        if auth_name is not None and parameter["name"].casefold() == auth_name.casefold():
            if location != auth_selector["placement"]:
                return False
            continue
        strategy = strategies.get(parameter["name"])
        if strategy is None:
            if not (
                profile["request"]["omit_unmapped_optional_parameters"]
                and requiredness == "optional"
                and parameter["cardinality"].get("minimum") == 0
            ):
                return False
        else:
            try:
                _require_policy_strategy_matches_type(strategy, data_type, parameter["name"])
            except PlanError:
                return False

    return profile_matches_response(profile["request"]["response"], document.get("response_contract", {}))


def reviewed_read_only_effect_matches(
    selector: dict[str, Any],
    effect_review: dict[str, Any],
    source_id: str,
    provider: str,
    operation: dict[str, Any],
    document: dict[str, Any],
) -> bool:
    """Require safe-method and operation-specific retrieval facts for a reviewed effect classification."""
    identity = document.get("identity", {})
    if (
        selector.get("source_id") != source_id
        or selector.get("provider") != provider
        or selector.get("protocol") != operation.get("protocol")
        or identity.get("source_id") != source_id
        or identity.get("provider") != provider
        or identity.get("operation_id") != operation.get("operation_id")
        or identity.get("protocol") != operation.get("protocol")
        or effect_review.get("classification") != "read_only"
        or effect_review.get("basis") != "rfc9110_safe_method_and_retrieval_purpose"
        or effect_review.get("rfc_reference") != "https://www.rfc-editor.org/rfc/rfc9110#section-9.2.1"
    ):
        return False

    method = document.get("transport", {}).get("http_method", {})
    method_value = str(method.get("value", "")).upper()
    expected_method = str(selector.get("method", "")).upper()
    if not (
        operation.get("protocol") == "REST"
        and expected_method in {"GET", "HEAD"}
        and method.get("status") == "documented"
        and method.get("authority_scope") == "operation_specific"
        and method_value == expected_method
        and any(ref.get("evidence_kind") == "operation_http_method" for ref in method.get("source_refs", []))
    ):
        return False

    # An explicit provider classification of mutation or an unknown effect
    # stays source fact; only unknown can use this operator-policy path.
    source_effect = document.get("effect", {})
    if source_effect.get("status") == "documented":
        if source_effect.get("classification") != "read_only":
            return False
    elif source_effect.get("status") != "unknown":
        return False

    operation_document = document.get("operation_document", {})
    title = operation_document.get("title", {})
    purpose = operation_document.get("purpose", {})
    if not (
        title.get("status") == "documented"
        and purpose.get("status") == "documented"
        and any(ref.get("evidence_kind") == "official_operation_title" for ref in title.get("source_refs", []))
        and any(ref.get("evidence_kind") == "official_operation_purpose" for ref in purpose.get("source_refs", []))
        and isinstance(title.get("value"), str)
        and isinstance(purpose.get("value"), str)
    ):
        return False
    title_text = title["value"].casefold()
    purpose_text = purpose["value"].casefold()
    combined = f"{title_text} {purpose_text}"
    if any(marker.casefold() in combined for marker in _MUTATION_MARKERS):
        return False
    terms = effect_review.get("purpose_terms", [])
    if not isinstance(terms, list) or not terms:
        return False
    if not all(term in _READ_PURPOSE_MARKERS for term in terms):
        return False
    if not any(_READ_PURPOSE_MARKERS[term][0].casefold() in purpose_text for term in terms):
        return False

    # Do not let an operator policy turn a caller-controlled action selector
    # into a safe read. Fixed operation selectors are also conservatively
    # excluded here; source-specific selector semantics need their own review.
    transport = document.get("transport", {})
    if any(
        item.get("status") == "documented"
        and item.get("role") in {"operation_selector", "action_selector", "command_selector"}
        for item in transport.get("fixed_query_selectors", [])
    ):
        return False
    if any(
        isinstance(parameter.get("name"), str)
        and parameter["name"].casefold() in _ACTION_PARAMETER_NAMES
        for parameter in document.get("parameters", [])
    ):
        return False
    return True


def effect_profile_matches_operation(
    profile: dict[str, Any],
    source_id: str,
    provider: str,
    operation: dict[str, Any],
    document: dict[str, Any],
) -> bool:
    selector = profile["selector"]
    if (
        selector.get("source_id") != source_id
        or selector.get("provider") != provider
        or selector.get("protocol") != operation.get("protocol")
    ):
        return False
    return reviewed_read_only_effect_matches(
        selector,
        profile["effect_review"],
        source_id,
        provider,
        operation,
        document,
    )


def apply_effect_profile_review(
    plan: dict[str, Any],
    profile: dict[str, Any],
    profile_index: int,
    document: dict[str, Any],
    document_ref: dict[str, Any],
    policy_ref: dict[str, Any],
) -> None:
    """Record exact source and review refs for an independently resolved effect gate."""
    refs = [
        _contract_fact_ref(document_ref, "#/transport/http_method"),
        _contract_fact_ref(document_ref, "#/operation_document/title"),
        _contract_fact_ref(document_ref, "#/operation_document/purpose"),
        evidence_ref(policy_ref, f"#/effect_profiles/{profile_index}", "reviewed_policy"),
        evidence_ref(policy_ref, f"#/effect_profiles/{profile_index}/effect_review", "reviewed_policy"),
        evidence_ref(policy_ref, f"#/effect_profiles/{profile_index}/review", "reviewed_policy"),
    ]
    existing = plan["request_plan"]["evidence_refs"]
    by_identity = {(ref["artifact_path"], ref["sha256"], ref["json_pointer"], ref["evidence_kind"]) for ref in existing}
    for ref in refs:
        key = (ref["artifact_path"], ref["sha256"], ref["json_pointer"], ref["evidence_kind"])
        if key not in by_identity:
            existing.append(ref)
            by_identity.add(key)
    missing = plan["request_plan"].get("missing_fields", [])
    for field in ("operation_method_authority", "operation_effect_read_only_authority"):
        if field in missing:
            missing.remove(field)


def profile_matches_response(response: dict[str, Any], response_contract: dict[str, Any]) -> bool:
    """Match a reusable branch policy only when every source branch is covered once."""
    branches = response_contract.get("success_branches")
    if not isinstance(branches, list) or not branches or len(branches) != len(response["branches"]):
        return False
    payload = response_contract.get("payload", {})
    if not (payload.get("status") == "documented" and payload.get("kind") == response["payload_kind"]):
        return False
    matched_source_indexes: set[int] = set()
    success_empty_semantics: set[str] = set()
    for policy_branch in response["branches"]:
        selector = policy_branch["selector"]
        matches = []
        for source_index, source_branch in enumerate(branches):
            if source_index in matched_source_indexes:
                continue
            if source_branch.get("http_status_code") not in selector["accepted_http_status_codes"]:
                continue
            payload_fact = source_branch.get("payload", {})
            shape = source_branch.get("schema_shape", {})
            root_shape = source_branch.get("root_shape", {})
            if (
                payload_fact.get("status") != "documented"
                or payload_fact.get("kind") != response["payload_kind"]
                or shape.get("status") != "complete"
                or not shape.get("source_refs")
                or root_shape.get("status") != "documented"
                or root_shape.get("kind") != selector["root_kind"]
            ):
                continue
            if selector["root_kind"] == "xml_element" and _clark_qname(root_shape.get("qname"), "response.root_shape.qname") != selector.get("root_qname"):
                continue
            documented_fields = source_branch.get("documented_fields", [])
            source_required = source_branch.get("required_fields", [])
            candidates = documented_fields + source_required + source_branch.get("member_shape_selectors", [])
            discriminator_match = True
            for discriminator in selector["discriminators"]:
                path_facts = [field for field in candidates if field.get("path") == discriminator["path"]]
                if discriminator["predicate"] == "absent":
                    positive_facts = [field for field in path_facts if field.get("status") in {"documented", "required"}]
                    absent_facts = [
                        field for field in source_branch.get("member_shape_selectors", [])
                        if field.get("path") == discriminator["path"]
                        and field.get("status") == "not_present"
                        and field.get("source_refs")
                    ]
                    if positive_facts or len(absent_facts) != 1:
                        discriminator_match = False
                        break
                    continue
                facts = [field for field in path_facts if field.get("status") in {"documented", "required"}]
                if not facts:
                    discriminator_match = False
                    break
                if discriminator["predicate"] in {"present", "node_type"}:
                    if discriminator["predicate"] == "node_type" and not any(fact.get("value_type") == discriminator["value_type"] for fact in facts):
                        discriminator_match = False
                        break
                else:
                    source_values = [fact.get("enum_values") for fact in facts if isinstance(fact.get("enum_values"), list)]
                    if not source_values or not any(
                        all(any(type(value) is type(source_value) and value == source_value for source_value in values) for value in discriminator["values"])
                        for values in source_values
                    ):
                        discriminator_match = False
                        break
            if discriminator_match:
                matches.append((source_index, source_branch))
        if len(matches) != 1:
            return False
        source_index, source_branch = matches[0]
        matched_source_indexes.add(source_index)

        # Operator policy selects classification and empty behavior; source policy must still
        # bind the branch's schema, status, code inventory, fields, and collection exactly.
        if policy_branch["classification"] not in {"success", "provider_error"}:
            return False
        if policy_branch["classification"] == "success":
            success_empty_semantics.add(policy_branch["empty_result_semantics"])
        collection = policy_branch.get("result_collection")
        source_collection = source_branch.get("result_collection")
        if (collection is None) != (source_collection is None):
            return False
        if collection is not None and any(source_collection.get(key) != collection.get(key) for key in ("path", "container_path", "item_path")):
            return False
        if collection is None and policy_branch["empty_result_semantics"] != "not_applicable":
            return False
        if collection is not None and policy_branch["empty_result_semantics"] not in {"valid", "invalid"}:
            return False
        if policy_branch["classification"] == "success" and collection is None and not (policy_branch["required_fields"] or selector["discriminators"]):
            return False
        expected_path_kind = "json_pointer" if response["payload_kind"] == "json" else "xml_qname_path"
        for selected in policy_branch["required_fields"]:
            if selected["path"]["kind"] == "json_pointer" and (selected["minimum"] != 1 or selected["maximum"] != 1):
                return False
            if not any(
                field.get("status") in {"documented", "required"}
                and field.get("path") == selected["path"]
                and field.get("value_type") == selected["value_type"]
                and field.get("source_refs")
                for field in source_branch.get("documented_fields", []) + source_branch.get("required_fields", [])
            ):
                return False
            if selected["path"]["kind"] != expected_path_kind:
                return False

        code_facts = source_branch.get("provider_result_codes", {})
        inventory = source_branch.get("coded_result_field_inventory", {})
        if policy_branch["code_mode"] == "none":
            if not (
                inventory.get("status") == "documented"
                and inventory.get("candidates") == []
                and inventory.get("source_refs")
            ):
                return False
        elif code_facts.get("status") not in {"documented", "example_only", "not_applicable"} or not code_facts.get("source_refs"):
            return False
        elif code_facts.get("status") == "documented":
            selected_values = (
                code_facts.get("success_values", {})
                if policy_branch["classification"] == "success"
                else code_facts.get("error_values", {})
            )
            if selected_values.get("status") != "documented" or not selected_values.get("values"):
                return False
        elif code_facts.get("status") == "example_only":
            if not (
                code_facts.get("evidence_strength") == "official_success_example"
                and code_facts.get("success_values", {}).get("status") == "example_only"
                and code_facts.get("success_values", {}).get("values")
            ):
                return False
        if policy_branch["code_mode"] == "none" and not policy_branch["code_mode_rationale"]:
            return False

    return len(matched_source_indexes) == len(branches) and len(success_empty_semantics) == 1


def compile_profile_assertion(
    profile: dict[str, Any],
    operation_identity: dict[str, Any],
    document: dict[str, Any],
    document_ref: dict[str, Any],
    policy_ref: dict[str, Any],
    profile_index: int,
) -> dict[str, Any]:
    response = profile["request"]["response"]
    response_contract = document["response_contract"]
    source_branches = response_contract["success_branches"]

    def source_ref(pointer: str) -> dict[str, str]:
        return evidence_ref(document_ref, pointer, "operation_document")

    def source_matches_discriminator(source_branch: dict[str, Any], discriminator: dict[str, Any]) -> tuple[bool, list[str]]:
        candidates: list[tuple[str, dict[str, Any]]] = []
        branch_fields = source_branch.get("documented_fields", []) + source_branch.get("required_fields", [])
        for index, fact in enumerate(branch_fields):
            if fact.get("path") == discriminator["path"] and fact.get("status") in {"documented", "required"}:
                base = "documented_fields" if index < len(source_branch.get("documented_fields", [])) else "required_fields"
                local_index = index if base == "documented_fields" else index - len(source_branch.get("documented_fields", []))
                candidates.append((f"{base}/{local_index}", fact))
        for index, fact in enumerate(source_branch.get("member_shape_selectors", [])):
            if fact.get("path") == discriminator["path"]:
                candidates.append((f"member_shape_selectors/{index}", fact))
        predicate = discriminator["predicate"]
        for suffix, fact in candidates:
            if predicate == "present" and fact.get("status") in {"documented", "required"}:
                return True, [suffix]
            if predicate == "absent" and fact.get("status") == "not_present":
                return True, [suffix]
            if predicate == "node_type" and fact.get("status") in {"documented", "required"} and fact.get("value_type") == discriminator["value_type"]:
                return True, [suffix]
            if predicate == "equals_any" and fact.get("status") in {"documented", "required"} and fact.get("value_type") == discriminator["value_type"]:
                source_values = fact.get("enum_values", fact.get("values"))
                if isinstance(source_values, list) and all(any(type(value) is type(source_value) and value == source_value for source_value in source_values) for value in discriminator["values"]):
                    return True, [suffix]
        return False, []

    selected_sources: list[tuple[dict[str, Any], int, dict[str, Any]]] = []
    used: set[int] = set()
    for policy_branch in response["branches"]:
        selector = policy_branch["selector"]
        matches = []
        for source_index, source_branch in enumerate(source_branches):
            if source_index in used or source_branch.get("http_status_code") not in selector["accepted_http_status_codes"]:
                continue
            root_shape = source_branch.get("root_shape", {})
            payload = source_branch.get("payload", {})
            if payload.get("kind") != response["payload_kind"] or payload.get("status") != "documented":
                continue
            if source_branch.get("schema_shape", {}).get("status") != "complete" or root_shape.get("status") != "documented" or root_shape.get("kind") != selector["root_kind"]:
                continue
            if selector["root_kind"] == "xml_element" and _clark_qname(root_shape.get("qname"), "response.root_shape.qname") != selector.get("root_qname"):
                continue
            if all(source_matches_discriminator(source_branch, item)[0] for item in selector["discriminators"]):
                matches.append((source_index, source_branch))
        fail(len(matches) == 1, "reviewed response branch selector does not resolve to exactly one source branch")
        source_index, source_branch = matches[0]
        used.add(source_index)
        selected_sources.append((policy_branch, source_index, source_branch))
    fail(len(used) == len(source_branches), "reviewed response policy does not cover every source branch")

    compiled_branches = []
    for policy_branch_index, (policy_branch, source_index, source_branch) in enumerate(selected_sources):
        base = f"#/response_contract/success_branches/{source_index}"
        branch_source_refs = [source_ref(base), source_ref(base + "/payload"), source_ref(base + "/schema_shape"), source_ref(base + "/root_shape")]
        status_refs = [source_ref(base + "/http_status_code")]
        discriminator_rows = []
        for discriminator in policy_branch["selector"]["discriminators"]:
            matched, suffixes = source_matches_discriminator(source_branch, discriminator)
            fail(matched and len(suffixes) == 1, "reviewed response discriminator lacks one exact source selector")
            discriminator_refs = [source_ref(f"{base}/{suffixes[0]}")]
            discriminator_rows.append({**discriminator, "source_refs": discriminator_refs})
            branch_source_refs.extend(discriminator_refs)

        required_fields = []
        source_field_rows = source_branch.get("documented_fields", [])
        for selected in policy_branch["required_fields"]:
            matches = [
                (index, field)
                for index, field in enumerate(source_field_rows)
                if field.get("status") == "documented" and field.get("path") == selected["path"] and field.get("value_type") == selected["value_type"] and field.get("source_refs")
            ]
            if not matches:
                matches = [
                    (index, field)
                    for index, field in enumerate(source_branch.get("required_fields", []))
                    if field.get("status") in {"documented", "required"} and field.get("path") == selected["path"] and field.get("value_type") == selected["value_type"] and field.get("source_refs")
                ]
                base_field = "required_fields"
            else:
                base_field = "documented_fields"
            fail(len(matches) == 1, "profile response field does not resolve to exactly one branch source fact")
            field_index, _field = matches[0]
            field_ref = source_ref(f"{base}/{base_field}/{field_index}")
            required_fields.append({
                "path": selected["path"],
                "value_type": selected["value_type"],
                "cardinality": {"minimum": selected["minimum"], "maximum": selected["maximum"]},
                "source_refs": [field_ref],
            })
            branch_source_refs.append(field_ref)

        codes = source_branch.get("provider_result_codes", {})
        inventory = source_branch.get("coded_result_field_inventory", {})
        if policy_branch["code_mode"] == "none":
            fail(source_branch.get("schema_shape", {}).get("status") == "complete" and inventory.get("status") == "documented" and inventory.get("candidates") == [], "code_mode=none requires complete source shape and an empty documented code inventory")
            code_status = "none_by_policy"
            code_refs = [source_ref(base + "/schema_shape"), source_ref(base + "/coded_result_field_inventory")]
            provider_codes = None
        elif codes.get("status") == "documented":
            success, errors = codes.get("success_values", {}), codes.get("error_values", {})
            code_status = "documented"
            code_refs = [source_ref(base + "/provider_result_codes")]
            fail(codes.get("basis") == "documented_code_map", "documented code fact has the wrong evidence basis")
            if policy_branch["classification"] == "success":
                fail(success.get("status") == "documented" and success.get("values"), "success branch lacks exact documented success values")
                provider_codes = {
                    "path": codes["path"], "value_type": codes["value_type"], "basis": "documented_code_map",
                    "success_values": success["values"], "source_refs": code_refs,
                }
            else:
                fail(errors.get("status") == "documented" and errors.get("values"), "provider-error branch lacks exact documented error values")
                provider_codes = {
                    "path": codes["path"], "value_type": codes["value_type"], "basis": "documented_code_map",
                    "error_values": errors["values"], "source_refs": code_refs,
                }
            if codes.get("error_classes"):
                error_classes = []
                for index, entry in enumerate(codes["error_classes"]):
                    error_classes.append({
                        "value": entry["value"], "category": entry["category"],
                        "source_refs": [source_ref(base + f"/provider_result_codes/error_classes/{index}")],
                    })
                provider_codes["error_classes"] = error_classes
        elif codes.get("status") == "example_only":
            success = codes.get("success_values", {})
            fail(policy_branch["classification"] == "success", "an official success example cannot classify an error branch")
            fail(codes.get("evidence_strength") == "official_success_example" and success.get("status") == "example_only" and success.get("values"), "example-only code fact lacks exact official success evidence")
            code_status = "expected_success_example"
            code_refs = [source_ref(base + "/provider_result_codes")]
            provider_codes = {"path": codes["path"], "value_type": codes["value_type"], "basis": "official_success_example", "success_values": success["values"], "source_refs": code_refs}
        elif codes.get("status") == "not_applicable":
            code_status = "not_applicable"
            code_refs = [source_ref(base + "/provider_result_codes")]
            provider_codes = None
        else:
            raise PlanError("source code semantics are unresolved for a reviewed response branch")

        collection = source_branch.get("result_collection")
        policy_collection = policy_branch.get("result_collection")
        compiled_collection = None
        if collection is not None:
            fail(collection.get("status") == "documented" and policy_collection is not None, "source result collection is unresolved or omitted from reviewed branch policy")
            fail(all(collection.get(key) == policy_collection.get(key) for key in ("path", "container_path", "item_path")), "reviewed result collection differs from source branch facts")
            compiled_collection = {
                "path": collection["path"], "container_path": collection["container_path"], "item_path": collection["item_path"],
                "value_type": "array", "semantics": policy_branch["empty_result_semantics"],
                "source_refs": [source_ref(base + "/result_collection")],
            }
            if response["payload_kind"] != "json":
                cardinality = collection.get("container_cardinality", {})
                fail(cardinality.get("status") == "documented", "XML result collection container cardinality is not documented")
                compiled_collection["container_cardinality"] = {"minimum": cardinality.get("minimum"), "maximum": cardinality.get("maximum")}
            branch_source_refs.append(source_ref(base + "/result_collection"))
        if policy_collection is None:
            fail(policy_branch["empty_result_semantics"] == "not_applicable", "branch without a reviewed collection must use not_applicable empty semantics")
        else:
            fail(policy_branch["empty_result_semantics"] in {"valid", "invalid"}, "collection branch needs valid or invalid empty semantics")

        review_ref = evidence_ref(
            policy_ref,
            f"#/profiles/{profile_index}/request/response/branches/{policy_branch_index}",
            "reviewed_policy",
        )
        compiled_branch = {
            "branch_id": policy_branch["branch_id"],
            "classification": policy_branch["classification"],
            "empty_result_semantics": policy_branch["empty_result_semantics"],
            "selector": {
                "accepted_http_status_codes": [source_branch["http_status_code"]],
                "root_kind": source_branch["root_shape"]["kind"],
                **({"root_qname": _clark_qname(source_branch["root_shape"]["qname"], "response.root_shape.qname")} if source_branch["root_shape"]["kind"] == "xml_element" else {}),
                "discriminators": discriminator_rows,
            },
            "http_status_source_refs": status_refs,
            "required_fields": required_fields,
            "provider_result_code_status": code_status,
            "provider_result_code_evidence_refs": code_refs,
            "source_refs": branch_source_refs,
            "review_refs": [review_ref],
        }
        if provider_codes is not None:
            compiled_branch["provider_result_codes"] = provider_codes
        if compiled_collection is not None:
            compiled_branch["result_collection"] = compiled_collection
        compiled_branches.append(compiled_branch)

    return {
        "schema_version": "datapan.operation-response-assertion.v2",
        "artifact_kind": "operation_response_assertion",
        "source_binding": {key: operation_identity[key] for key in ("source_id", "provider", "protocol")},
        "operation_identity": {key: operation_identity[key] for key in ("operation_id", "dataset_id", "operation_name", "upstream_operation_key")},
        "document_evidence": document_ref,
        "review": profile["review"],
        "assertion": {"payload_kind": response["payload_kind"], "branches": compiled_branches},
    }


def _resolve_documented_missing_fields(plan: dict[str, Any], document: dict[str, Any]) -> None:
    missing = plan["request_plan"]["missing_fields"]
    effect = document["effect"]
    if (
        effect["classification"] == "read_only"
        and effect["status"] == "documented"
        and effect["authority"] == "operation_document"
        and effect["source_refs"]
    ):
        missing.remove("operation_effect_read_only_authority")

    method = document["transport"]["http_method"]
    if method["value"] is not None and method["status"] == "documented" and method["authority_scope"] == "operation_specific":
        method_field = "soap_request_method_authority" if document["identity"]["protocol"] == "SOAP" else "operation_method_authority"
        missing.remove(method_field)

    parameters = document["parameters"]
    if parameters and all(
        parameter["location"]["value"] is not None
        and parameter["location"]["status"] == "documented"
        and parameter["location"]["source_refs"]
        for parameter in parameters
    ):
        if "parameter_location" in missing:
            missing.remove("parameter_location")

    assertion = document["response_assertion"]
    empty = assertion["empty_result_semantics"]
    if (
        assertion["kind"] == "documented_response_fields"
        and assertion["fields"]
        and empty["value"] in {"valid", "invalid"}
        and empty["status"] == "documented"
        and empty["source_refs"]
    ):
        missing.remove("response_assertion_and_empty_result_semantics")


def _documented_fact(document: dict[str, Any], pointer: str) -> dict[str, Any]:
    fact = json_pointer_value(document, pointer)
    fail(isinstance(fact, dict), f"operation document fact is not an object: {pointer}")
    fail(fact.get("status") == "documented", f"operation document fact is not documented: {pointer}")
    fail(isinstance(fact.get("source_refs"), list) and fact["source_refs"], f"operation document fact has no source locator: {pointer}")
    return fact


def _policy_evidence(
    ref: dict[str, Any],
    policy_index: int,
    suffix: str = "",
    kind: str = "reviewed_policy",
    pointer_base: str | None = None,
) -> dict[str, str]:
    base = pointer_base if pointer_base is not None else f"#/policies/{policy_index}"
    return evidence_ref(ref, f"{base}{suffix}", kind)


def _parameter_cardinality(parameter: dict[str, Any]) -> str:
    requiredness = parameter["requiredness"]
    cardinality = parameter["cardinality"]
    fail(requiredness.get("status") == "documented" and requiredness.get("value") in {"required", "optional"}, f"parameter requiredness is unresolved: {parameter['name']}")
    fail(cardinality.get("status") == "documented", f"parameter cardinality is unresolved: {parameter['name']}")
    minimum = cardinality.get("minimum")
    maximum = cardinality.get("maximum")
    fail(type(minimum) is int and minimum in {0, 1}, f"parameter minimum cardinality is unsupported: {parameter['name']}")
    fail(maximum is None or (type(maximum) is int and maximum >= 1), f"parameter maximum cardinality is unsupported: {parameter['name']}")
    expected_minimum = 1 if requiredness["value"] == "required" else 0
    fail(minimum == expected_minimum, f"parameter requiredness and cardinality disagree: {parameter['name']}")
    repeated = maximum is None or maximum > 1
    return f"{requiredness['value']}_{'repeated' if repeated else 'single'}"


def _require_policy_strategy_matches_type(strategy: dict[str, Any], data_type: Any, name: str) -> None:
    normalized = str(data_type or "").strip().casefold()
    integral = {"integer", "int", "long", "short", "whole_number"}
    numeric = integral | {"number", "numeric", "decimal", "float", "double"}
    if strategy["kind"] in {"bounded_integer", "relative_year"}:
        fail(normalized in integral, f"reviewed numeric strategy has no documented integer type: {name}")
        if strategy["kind"] == "bounded_integer":
            fail(strategy["minimum"] <= strategy["maximum"], f"reviewed integer strategy has inverted bounds: {name}")
            if strategy["selection"] == "fixed":
                fail(strategy["minimum"] <= strategy["selected_value"] <= strategy["maximum"], f"reviewed integer selection is outside its bounds: {name}")
        else:
            fail(strategy["minimum_year"] <= strategy["maximum_year"], f"reviewed relative-year bounds are inverted: {name}")
        return
    fail(strategy["kind"] in {"reviewed_enum", "reviewed_literal"}, f"unsupported reviewed value strategy: {name}")
    value = strategy["selected_value"]
    if normalized in {"string", "text"}:
        fail(isinstance(value, str) and value, f"reviewed string value has incompatible source type: {name}")
    elif normalized in integral:
        fail(type(value) is int, f"reviewed integer value has incompatible source type: {name}")
    elif normalized in numeric:
        fail(type(value) in {int, float}, f"reviewed numeric value has incompatible source type: {name}")
    elif normalized in {"boolean", "bool"}:
        fail(type(value) is bool, f"reviewed boolean value has incompatible source type: {name}")
    else:
        raise PlanError(f"reviewed literal/enum has unknown or unsupported documented type: {name}")


def _clark_qname(value: Any, field: str) -> dict[str, str]:
    if isinstance(value, dict):
        namespace = value.get("namespace")
        local_name = value.get("local_name")
    elif isinstance(value, str) and value.startswith("{") and "}" in value:
        namespace, local_name = value[1:].split("}", 1)
    else:
        raise PlanError(f"documented SOAP QName has unsupported representation: {field}")
    fail(isinstance(namespace, str), f"documented SOAP QName namespace is missing: {field}")
    fail(isinstance(local_name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9._-]*", local_name) is not None, f"documented SOAP QName local name is invalid: {field}")
    return {"namespace": namespace, "local_name": local_name}


def _contract_fact_ref(document_ref: dict[str, Any], pointer: str) -> dict[str, str]:
    return evidence_ref(document_ref, pointer, "operation_document")


def compile_reviewed_operation_plan(
    plan: dict[str, Any],
    operation: dict[str, Any],
    document: dict[str, Any],
    document_ref: dict[str, Any],
    policy_entry: dict[str, Any],
    policy_ref: dict[str, Any],
    policy_index: int,
    assertion_artifact: dict[str, Any],
    assertion_ref: dict[str, Any],
    root: Path = ROOT,
    policy_pointer_base: str | None = None,
) -> dict[str, Any]:
    """Compile one exact source-evidenced operation plus its reviewed operator choices."""
    identity = operation_policy_identity("data_go_kr", "data.go.kr", operation)
    fail(policy_entry["identity"] == identity, "reviewed policy identity changed after validation")
    fail(policy_entry["document_evidence"] == document_ref, "reviewed policy is not bound to the exact document-evidence bytes")
    expected_assertion_identity = {
        "operation_id": identity["operation_id"],
        "dataset_id": identity["dataset_id"],
        "operation_name": identity["operation_name"],
        "upstream_operation_key": identity["upstream_operation_key"],
    }
    fail(assertion_artifact["operation_identity"] == expected_assertion_identity, "response assertion identity differs from its reviewed policy")

    captured_transport = document["transport"]
    transport: dict[str, Any] = {"protocol": identity["protocol"]}
    transport_refs: list[dict[str, str]] = []
    for field in ("scheme", "host", "path", "http_method"):
        fact = _documented_fact(document, f"#/transport/{field}")
        value = fact.get("value")
        fail(isinstance(value, str) and value, f"documented transport fact lacks {field}")
        if field == "scheme":
            value = value.casefold()
            fail(value in {"http", "https"}, "documented transport scheme is unsupported")
            fail(value == "https", "reviewed production request plans require documented HTTPS transport")
        elif field == "host":
            value = value.casefold()
            fail("/" not in value and "?" not in value and "#" not in value, "documented transport host is not a host name")
        elif field == "path":
            fail(value.startswith("/") and "?" not in value and "#" not in value, "documented transport path contains query or fragment data")
        elif field == "http_method":
            value = value.upper()
            fail(fact.get("authority_scope") == "operation_specific", "HTTP method is not operation-specific evidence")
            fail(any(ref.get("evidence_kind") == "operation_http_method" for ref in fact["source_refs"]), "HTTP method lacks an operation-method source locator")
            fail(value in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}, "documented HTTP method is unsupported")
        transport[field] = value
        transport_refs.append(_contract_fact_ref(document_ref, f"#/transport/{field}"))

    endpoint = registered_endpoint(operation.get("transport", {}).get("endpoint"))
    fail(endpoint is not None, "registered operation has no canonical endpoint host and path")
    fail(
        transport["host"] == endpoint["host"]
        and registered_paths_match(transport["path"], endpoint["path"]),
        "documented operation endpoint differs from the registered endpoint",
    )
    documented_port = captured_transport.get("port")
    if documented_port is not None:
        fail(type(documented_port) is int and 1 <= documented_port <= 65535, "documented transport port is invalid")
        fail(bool(captured_transport.get("port_source_refs")), "documented transport port has no exact source reference")
        fail(endpoint.get("port") == documented_port, "documented transport port differs from the registered endpoint")
        transport["port"] = documented_port
        transport_refs.append(_contract_fact_ref(document_ref, "#/transport/port"))
    else:
        fail(endpoint.get("port") is None, "registered endpoint port is not documented by the operation source")
    fail(not (identity["protocol"] == "REST" and transport["http_method"] == "HEAD"), "HEAD does not establish a usable response-body observation contract")

    if identity["protocol"] == "SOAP":
        soap_values: dict[str, Any] = {}
        for field in ("soap_action", "soap_version", "envelope_namespace", "operation_qname", "body_encoding"):
            fact = _documented_fact(document, f"#/transport/{field}")
            value = fact.get("value")
            fail(value is not None, f"SOAP transport fact is missing: {field}")
            if field == "operation_qname":
                value = _clark_qname(value, field)
            soap_values[field] = value
            transport_refs.append(_contract_fact_ref(document_ref, f"#/transport/{field}"))
        fail(soap_values["soap_version"] in {"1.1", "1.2"}, "unsupported SOAP version")
        fail(soap_values["body_encoding"] in {"document_literal", "rpc_literal", "encoded_xml"}, "unsupported SOAP body encoding")
        transport.update(soap_values)
    elif identity["protocol"] == "REST":
        fail(captured_transport["soap_action"].get("status") == "not_applicable", "REST sidecar has contradictory SOAP action")
    else:
        raise PlanError("reviewed complete policy is currently supported only for REST and SOAP")
    transport["authority"] = "operation_document"
    transport["evidence_refs"] = transport_refs

    effect = document["effect"]
    if effect.get("classification") == "read_only" and effect.get("status") == "documented" and effect.get("authority") == "operation_document":
        fail(any(ref.get("evidence_kind") == "operation_effect" for ref in effect.get("source_refs", [])), "read-only effect lacks an operation-type source locator")
        operation_effect = {
            "classification": "read_only",
            "authority": "operation_document",
            "evidence_refs": [_contract_fact_ref(document_ref, "#/effect")],
        }
    else:
        effect_review = policy_entry["request"].get("effect_review")
        fail(isinstance(effect_review, dict), "operation effect is neither documented read-only nor explicitly reviewed")
        effect_selector = policy_entry.get("_effect_selector") or {
            "source_id": identity["source_id"],
            "provider": identity["provider"],
            "protocol": identity["protocol"],
            "method": transport["http_method"],
        }
        fail(
            reviewed_read_only_effect_matches(
                effect_selector,
                effect_review,
                "data_go_kr",
                "data.go.kr",
                {"operation_id": identity["operation_id"], "protocol": identity["protocol"]},
                document,
            ),
            "reviewed read-only effect lacks an exact safe-method and retrieval-purpose match",
        )
        base = policy_pointer_base or f"#/policies/{policy_index}"
        operation_effect = {
            "classification": "read_only",
            "authority": "reviewed_policy",
            "evidence_refs": [
                _contract_fact_ref(document_ref, "#/transport/http_method"),
                _contract_fact_ref(document_ref, "#/operation_document/title"),
                _contract_fact_ref(document_ref, "#/operation_document/purpose"),
                evidence_ref(policy_ref, base + "/request/effect_review", "reviewed_policy"),
                evidence_ref(policy_ref, base + "/review", "reviewed_policy"),
            ],
        }
        if policy_pointer_base is not None:
            operation_effect["evidence_refs"].append(evidence_ref(policy_ref, base + "/selector", "reviewed_policy"))

    auth = document["authentication"]
    fail(auth.get("status") == "documented" and auth.get("source_refs"), "operation authentication placement is not documented")
    fail(not all(ref.get("evidence_kind") == "example_request_query_key" for ref in auth["source_refs"]), "an example key alone cannot establish authentication")
    auth_requirement = auth.get("requirement")
    auth_mechanism = auth.get("mechanism")
    auth_placement = auth.get("placement")
    auth_refs = [_contract_fact_ref(document_ref, "#/authentication")]
    authentication: dict[str, Any] = {
        "requirement": auth_requirement,
        "mechanism": auth_mechanism,
        "placement": auth_placement,
        "credential_reference_required": auth_requirement == "required",
        "evidence_refs": auth_refs,
    }
    auth_parameter_name = None
    if auth_requirement == "required":
        fail(auth_mechanism in {"service_key", "api_key", "basic", "oauth2", "mutual_tls", "other"}, "documented authentication mechanism is unsupported")
        fail(auth_placement in {"query", "header", "soap_header"}, "documented authentication placement is unsupported")
        names = auth.get("parameter_names", [])
        fail(isinstance(names, list) and len(names) == 1 and isinstance(names[0], str) and names[0], "authentication must identify exactly one request parameter")
        auth_parameter_name = names[0]
        if auth_placement == "query":
            authentication["parameter_name"] = auth_parameter_name
        elif auth_placement == "header":
            authentication["header_name"] = auth_parameter_name
        else:
            fail(identity["protocol"] == "SOAP", "SOAP-header authentication requires SOAP")
            authentication["header_qname"] = _clark_qname(auth.get("header_qname"), "authentication.header_qname")
        if identity["protocol"] == "SOAP" and auth_placement == "soap_header":
            auth_parameter = next(
                (item for item in document["parameters"] if item["name"].casefold() == auth_parameter_name.casefold()),
                None,
            )
            fail(auth_parameter is not None, "SOAP authentication parameter is absent from the operation parameter inventory")
            fail(
                _clark_qname(auth_parameter.get("qualified_name"), "authentication.parameter_qname") == authentication["header_qname"],
                "SOAP authentication QName differs between its request parameter and authentication contract",
            )
        else:
            fail(identity["protocol"] != "SOAP" or auth_placement != "body", "SOAP body credential placement is unsupported")
    elif auth_requirement == "none":
        fail(auth_mechanism == "none" and auth_placement == "none", "no-auth declaration must explicitly use none")
        fail(not auth.get("parameter_names"), "no-auth declaration unexpectedly names credential parameters")
    else:
        raise PlanError("authentication requirement is unknown")

    source_parameters = document["parameters"]
    names = [parameter["name"] for parameter in source_parameters]
    fail(all(isinstance(name, str) and name for name in names), "documented request parameter name is missing")
    fail(len({name.casefold() for name in names}) == len(names), "documented request parameter names collide case-insensitively")
    request_policy = policy_entry["request"]
    strategy_rows = policy_entry["request"]["parameter_strategies"]
    strategy_by_name = {row["name"]: row["strategy"] for row in strategy_rows}
    fail(len(strategy_by_name) == len(strategy_rows), "reviewed policy repeats a request parameter strategy")
    fail(len({name.casefold() for name in strategy_by_name}) == len(strategy_by_name), "reviewed policy parameter strategies collide case-insensitively")
    auth_names = {auth_parameter_name.casefold()} if auth_parameter_name else set()
    noncredential_by_fold = {name.casefold(): name for name in names if name.casefold() not in auth_names}
    source_by_fold = {name.casefold(): name for name in names}
    strategy_by_fold = {name.casefold(): name for name in strategy_by_name}
    fail(set(strategy_by_fold).issubset(noncredential_by_fold), "reviewed policy names an absent or credential parameter strategy")
    fail(all(source_by_fold[name.casefold()] == name for name in strategy_by_name), "reviewed policy parameter spelling differs from the source operation")
    parameter_by_name = {parameter["name"].casefold(): parameter for parameter in source_parameters}
    missing_strategies = set(noncredential_by_fold) - set(strategy_by_fold)
    if missing_strategies:
        fail(
            request_policy.get("omit_unmapped_optional_parameters") is True,
            "reviewed strategies do not cover every non-credential operation parameter",
        )
        for name in missing_strategies:
            omitted = parameter_by_name[name]
            fail(
                omitted.get("requiredness", {}).get("status") == "documented"
                and omitted["requiredness"].get("value") == "optional"
                and omitted.get("cardinality", {}).get("status") == "documented"
                and omitted["cardinality"].get("minimum") == 0,
                f"unmapped request parameter is not documented optional: {omitted['name']}",
            )

    output_parameters = []
    document_parameter_refs = []
    for index, source_parameter in enumerate(source_parameters):
        name = source_parameter["name"]
        source_parameter_refs = source_parameter.get("source_refs", [])
        fail(source_parameter_refs and any(ref.get("evidence_kind") == "parameter_name" for ref in source_parameter_refs), f"parameter name lacks source evidence: {name}")
        location_fact = _documented_fact(document, f"#/parameters/{index}/location")
        location = location_fact.get("value")
        fail(location in {"query", "path", "header", "body", "soap_header"}, f"documented parameter location is unsupported: {name}")
        cardinality = _parameter_cardinality(source_parameter)
        data_type_fact = _documented_fact(document, f"#/parameters/{index}/data_type")
        if name.casefold() not in auth_names and name.casefold() not in strategy_by_fold:
            fail(source_parameter["requiredness"]["value"] == "optional", f"required input has no reviewed safe value strategy: {name}")
            continue
        if auth_parameter_name and name.casefold() == auth_parameter_name.casefold():
            expected_location = {"query": "query", "header": "header", "soap_header": "soap_header"}[auth_placement]
            fail(location == expected_location, "authentication parameter location differs from the operation document")
            value_strategy = {"kind": "credential_reference", "authority": "runtime_binding", "binding_field": "credential_reference"}
        else:
            strategy = strategy_by_name[name]
            _require_policy_strategy_matches_type(strategy, data_type_fact.get("value"), name)
            value_strategy = {"kind": strategy["kind"], "authority": "reviewed_policy"}
            value_strategy.update({key: value for key, value in strategy.items() if key != "kind"})
            policy_strategy_index = next(i for i, row in enumerate(strategy_rows) if row["name"] == name)
            strategy_ref = _policy_evidence(policy_ref, policy_index, f"/request/parameter_strategies/{policy_strategy_index}/strategy", pointer_base=policy_pointer_base)
            strategy_fact = {"kind": strategy["kind"], **{key: value for key, value in strategy.items() if key != "kind"}}
            fail(json_pointer_value(load_json(root / policy_ref["path"]), strategy_ref["json_pointer"]) == strategy_fact, "reviewed strategy pointer differs from the compiled value")
        if location == "query" and auth_placement == "query" and auth_parameter_name and name.casefold() == auth_parameter_name.casefold():
            fail(cardinality == "required_single", "credential query parameter must be exactly one required value")
        if location == "header" and auth_placement == "header" and auth_parameter_name and name.casefold() == auth_parameter_name.casefold():
            fail(cardinality == "required_single", "credential header parameter must be exactly one required value")
        source_cardinality = source_parameter["cardinality"]
        parameter_evidence_refs = [
            _contract_fact_ref(document_ref, f"#/parameters/{index}/name"),
            _contract_fact_ref(document_ref, f"#/parameters/{index}/location"),
            _contract_fact_ref(document_ref, f"#/parameters/{index}/requiredness"),
            _contract_fact_ref(document_ref, f"#/parameters/{index}/cardinality"),
            _contract_fact_ref(document_ref, f"#/parameters/{index}/data_type"),
        ]
        if name.casefold() in auth_names:
            parameter_evidence_refs.append(auth_refs[0])
        else:
            strategy_index = next(i for i, row in enumerate(strategy_rows) if row["name"] == name)
            parameter_evidence_refs.append(_policy_evidence(policy_ref, policy_index, f"/request/parameter_strategies/{strategy_index}", pointer_base=policy_pointer_base))
        output_parameters.append({
            "name": name,
            **({"qualified_name": _clark_qname(source_parameter["qualified_name"], f"parameter.{name}.qualified_name")} if "qualified_name" in source_parameter else {}),
            "location": location,
            "cardinality": cardinality,
            "value_strategy": value_strategy,
            "evidence_refs": parameter_evidence_refs,
        })
        document_parameter_refs.extend(parameter_evidence_refs[:4])

    if auth_requirement == "required":
        auth_parameter = next(parameter for parameter in source_parameters if parameter["name"].casefold() == auth_parameter_name.casefold())
        authentication["cardinality"] = _parameter_cardinality(auth_parameter)
    else:
        authentication["credential_reference_required"] = False

    limits = {**request_policy["limits"], "evidence_refs": [_policy_evidence(policy_ref, policy_index, "/request/limits", pointer_base=policy_pointer_base)]}
    assertion = assertion_artifact["assertion"]
    assertion_path = assertion_ref["path"]
    assertion_evidence = evidence_ref(assertion_ref, "#/assertion", "reviewed_policy")
    if assertion == {"mode": "observation_only"}:
        observation_evidence = [
            assertion_evidence,
            evidence_ref(assertion_ref, "#/review", "reviewed_policy"),
            _contract_fact_ref(document_ref, "#/response_contract"),
            _contract_fact_ref(document_ref, "#/parameters"),
            *transport_refs,
            *operation_effect["evidence_refs"],
            *auth_refs,
            *[ref for parameter in output_parameters for ref in parameter["evidence_refs"]],
            *limits["evidence_refs"],
            _policy_evidence(policy_ref, policy_index, "/request/response_assertion_artifact", pointer_base=policy_pointer_base),
            _policy_evidence(policy_ref, policy_index, "/review", pointer_base=policy_pointer_base),
        ]
        observation_evidence = list({
            (ref["artifact_path"], ref["sha256"], ref["json_pointer"], ref["evidence_kind"]): ref
            for ref in observation_evidence
        }.values())
        response_assertion = {
            "kind": "observation_only",
            "empty_result_semantics": "not_applicable",
            "assertion_ref": f"{assertion_path}#/assertion",
            "evidence_refs": observation_evidence,
        }
    else:
        if assertion["payload_kind"] == "json":
            assertion_kind = "json_contract"
        elif assertion["payload_kind"] == "xml":
            assertion_kind = "xml_contract"
        else:
            fail(identity["protocol"] == "SOAP", "SOAP response assertion cannot bind a non-SOAP operation")
            assertion_kind = "soap_fault_free"
        assertion_refs = [assertion_evidence]
        expected_status_codes: set[int] = set()
        empty_semantics = set()
        for branch in assertion["branches"]:
            expected_status_codes.update(branch["selector"]["accepted_http_status_codes"])
            if branch["classification"] == "success":
                empty_semantics.add(branch["empty_result_semantics"])
                if branch.get("result_collection") is not None:
                    fail(branch["empty_result_semantics"] == branch["result_collection"]["semantics"], "branch empty semantics differ from its result collection")
            assertion_refs.extend(branch["source_refs"])
            assertion_refs.extend(branch["http_status_source_refs"])
            assertion_refs.extend(branch["provider_result_code_evidence_refs"])
            assertion_refs.extend(ref for field in branch["required_fields"] for ref in field["source_refs"])
            if "provider_result_codes" in branch:
                assertion_refs.extend(branch["provider_result_codes"]["source_refs"])
                for error_class in branch["provider_result_codes"].get("error_classes", []):
                    assertion_refs.extend(error_class["source_refs"])
            if branch.get("result_collection") is not None:
                assertion_refs.extend(branch["result_collection"]["source_refs"])
            assertion_refs.extend(branch["review_refs"])
            for discriminator in branch["selector"]["discriminators"]:
                assertion_refs.extend(discriminator["source_refs"])
        fail(len(empty_semantics) == 1, "plan v1 requires one reviewed empty-result rule shared by all successful response branches")
        response_assertion = {
            "kind": assertion_kind,
            "expected_status_codes": sorted(expected_status_codes),
            "empty_result_semantics": next(iter(empty_semantics)),
            "assertion_ref": f"{assertion_path}#/assertion",
            "evidence_refs": assertion_refs,
        }

    contract = {
        "transport": transport,
        "operation_effect": operation_effect,
        "parameter_inventory_evidence_refs": [_contract_fact_ref(document_ref, "#/parameters")],
        "parameters": output_parameters,
        "authentication": authentication,
        "limits": limits,
        "response_assertion": response_assertion,
    }

    policy_root_ref = _policy_evidence(policy_ref, policy_index, pointer_base=policy_pointer_base)
    plan["request_plan"] = {
        "status": "complete",
        "evidence_refs": [
            policy_root_ref,
            _contract_fact_ref(document_ref, "#/effect"),
            _contract_fact_ref(document_ref, "#/transport"),
            _contract_fact_ref(document_ref, "#/parameters"),
            _contract_fact_ref(document_ref, "#/authentication"),
            assertion_evidence,
        ],
        "request_contract": contract,
    }

    runtime_policy = policy_entry.get("runtime_binding")
    if runtime_policy is None:
        plan["runtime_binding"] = {
            "status": "unbound",
            "missing_fields": ["credential_entitlement_reference", "shared_quota_scopes_and_limits", "observation_period"],
            "evidence_refs": [policy_root_ref],
        }
    else:
        required_auth = auth_requirement == "required"
        fail(required_auth == ("credential_reference" in runtime_policy), "runtime credential reference does not match documented authentication requirement")
        if required_auth:
            credential_scope_key = runtime_policy.get("credential_scope_key")
            fail(isinstance(credential_scope_key, str) and credential_scope_key, "authenticated runtime policy has no shared credential scope key")
        else:
            fail("credential_scope_key" not in runtime_policy, "no-auth runtime policy must not declare a credential quota scope key")
            credential_scope_key = None
        quotas = []
        quota_rows = runtime_policy["quota_policies"]
        quota_scope_keys: set[tuple[str, str]] = set()
        for quota_index, quota in enumerate(quota_rows):
            key = (quota["scope_kind"], quota["scope_key"])
            fail(key not in quota_scope_keys, "runtime policy repeats a quota scope")
            quota_scope_keys.add(key)
            quotas.append({
                **quota,
                "scope_sha256": quota_scope_digest(*key),
                "evidence_refs": [_policy_evidence(policy_ref, policy_index, f"/runtime_binding/quota_policies/{quota_index}", pointer_base=policy_pointer_base)],
            })
        if required_auth:
            credential_quotas = [quota for quota in quotas if quota["scope_kind"] == "credential"]
            fail(len(credential_quotas) == 1 and credential_quotas[0]["scope_key"] == credential_scope_key, "authenticated runtime policy must bind one matching shared credential quota")
        else:
            fail(not any(quota["scope_kind"] == "credential" for quota in quotas), "no-auth runtime policy cannot carry a credential quota scope")
        runtime_pointer_ref = _policy_evidence(policy_ref, policy_index, "/runtime_binding", pointer_base=policy_pointer_base)
        plan["runtime_binding"] = {
            "status": "bound",
            **({"credential_reference": runtime_policy["credential_reference"], "credential_scope_key": credential_scope_key} if required_auth else {}),
            "quota_policies": quotas,
            "observation_period_seconds": runtime_policy["observation_period_seconds"],
            "evidence_refs": [runtime_pointer_ref],
        }

    reasons = []
    if plan["request_plan"]["status"] != "complete":
        reasons.append("request_plan_incomplete")
    if plan["runtime_binding"]["status"] != "bound":
        reasons.append("runtime_binding_unbound")
    reasons.append("admission_not_authorized_by_registry_plan")
    plan["admission"] = {"status": "not_admitted", "reasons": reasons, "evidence_refs": []}
    return plan


def registered_endpoint(endpoint: str | None) -> dict[str, Any] | None:
    if not endpoint:
        return None
    parsed = urlsplit(endpoint if "://" in endpoint else "https://" + endpoint.lstrip("/"))
    if not parsed.hostname or not parsed.path.startswith("/") or parsed.username or parsed.password or parsed.fragment:
        return None
    result: dict[str, Any] = {"host": parsed.hostname.lower(), "path": parsed.path}
    if parsed.port is not None:
        result["port"] = parsed.port
    return result


def registered_paths_match(documented_path: str, registered_path: str) -> bool:
    """Match route templates while preserving literals and placeholder positions."""
    token_pattern = re.compile(r"(\{[^{}]+\})")

    def tokens(path: str) -> tuple[tuple[str, str], ...] | None:
        parts = token_pattern.split(path)
        if "{" in token_pattern.sub("", path) or "}" in token_pattern.sub("", path):
            return None
        result = []
        for part in parts:
            if not part:
                continue
            if part.startswith("{") and part.endswith("}"):
                result.append(("parameter", ""))
            else:
                result.append(("literal", part))
        return tuple(result)

    documented_tokens = tokens(documented_path)
    registered_tokens = tokens(registered_path)
    return documented_tokens is not None and documented_tokens == registered_tokens


def legacy_policy_record(
    canary: dict[str, Any], index: int, policy_ref: dict[str, Any]
) -> dict[str, Any]:
    pointer = f"#/canaries/{index}"
    result: dict[str, Any] = {
        "policy_ref": evidence_ref(policy_ref, pointer, "reviewed_policy"),
        "safe_parameters": [dict(item) for item in canary.get("safe_parameters", [])],
    }
    if isinstance(canary.get("policy_version"), int):
        result["policy_version"] = canary["policy_version"]
    correction = canary.get("endpoint_correction")
    if correction:
        result["endpoint_correction"] = {
            key: correction[key]
            for key in ("source_path", "request_path", "upstream_operation_seq")
        }
    return result


def make_incomplete_plan(
    *,
    scope: dict[str, Any],
    operation_id: str,
    protocol: str,
    identity: dict[str, Any],
    evidence: dict[str, str],
    legacy_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if protocol == "REST":
        missing = [
            "operation_method_authority",
            "secure_transport_required",
            "operation_effect_read_only_authority",
            "parameter_location",
            "parameter_cardinality",
            "safe_value_strategy_for_required_inputs",
            "authentication_placement",
            "request_resource_limits",
            "response_assertion_and_empty_result_semantics",
        ]
    elif protocol == "SOAP":
        missing = [
            "soap_request_method_authority",
            "secure_transport_required",
            "operation_effect_read_only_authority",
            "parameter_location",
            "parameter_cardinality",
            "safe_value_strategy_for_required_inputs",
            "authentication_placement",
            "request_resource_limits",
            "response_assertion_and_empty_result_semantics",
        ]
    else:
        missing = [
            "transport_and_method_authority",
            "secure_transport_required",
            "operation_effect_read_only_authority",
            "parameter_inventory_and_location",
            "parameter_cardinality",
            "safe_value_strategy_for_required_inputs",
            "authentication_placement",
            "request_resource_limits",
            "response_assertion_and_empty_result_semantics",
        ]
    plan: dict[str, Any] = {
        "schema_version": "datapan.operation-observation-plan.v1",
        "artifact_kind": "operation_plan",
        "source_binding": scope,
        "operation_identity": {"operation_id": operation_id, "protocol": protocol, **identity},
        "request_plan": {
            "status": "incomplete",
            "evidence_refs": [evidence],
            "missing_fields": missing,
        },
        "runtime_binding": {
            "status": "unbound",
            "missing_fields": ["credential_entitlement_reference", "shared_quota_scopes_and_limits", "observation_period"],
            "evidence_refs": [],
        },
        "admission": {
            "status": "not_admitted",
            "reasons": ["request_plan_incomplete", "runtime_binding_unbound"],
            "evidence_refs": [],
        },
    }
    if legacy_policy:
        plan["legacy_policy"] = legacy_policy
    return plan


def build(root: Path = ROOT, revision: str | None = None) -> tuple[dict[str, Any], dict[str, bytes]]:
    revision = revision or current_revision(root)
    manifest_path = root / "reports/data-go-kr/operation-manifest.json"
    source_snapshot_path = root / "data/data-go-kr.registry.json"
    source_profile_path = root / "sources/data_go_kr.json"
    provider_index_path = root / "data/provider-index.json"
    policy_path = root / "policy/health-probe-canaries.json"

    manifest = load_json(manifest_path)
    check_data_go_manifest(manifest)
    operations = manifest["operations"]
    source_profile = load_json(source_profile_path)
    data_go_adapter_id = checked_adapter_id("data_go_kr", "data.go.kr", source_profile)
    policy = load_json(policy_path)
    legacy_map = canary_policy_map(policy)
    provider_index = load_json(provider_index_path)
    adapters = provider_index.get("adapters")
    fail(isinstance(adapters, list), "provider index adapters must be an array")
    fail(provider_index.get("adapter_count") == len(adapters), "provider index adapter count mismatch")

    manifest_ref = artifact_ref(manifest_path, root)
    source_snapshot_ref = artifact_ref(source_snapshot_path, root)
    fail(manifest.get("source_snapshot") == source_snapshot_ref, "operation manifest source snapshot binding mismatch")
    policy_ref = artifact_ref(policy_path, root)
    provider_index_ref = artifact_ref(provider_index_path, root)
    denominator_documents = {
        source_id: load_json(root / path.relative_to(ROOT))
        for source_id, path in DENOMINATOR_PATHS.items()
    }
    document_evidence = load_document_evidence(root, operations)
    document_evidence_v2 = _document_evidence_v2_catalog(
        root,
        operations,
        denominator_documents,
        manifest_ref,
        source_snapshot_ref,
    )
    reviewed_policies, _reviewed_policy_document, reviewed_policy_ref, reviewed_policy_inputs = load_reviewed_operation_policies(
        root, operations, document_evidence, document_evidence_v2
    )
    source_artifact_paths = [
        source_snapshot_path,
        manifest_path,
        source_profile_path,
        policy_path,
        provider_index_path,
        root / "scripts/generate-operation-observation-plan.py",
        root / "scripts/operation_document_evidence.py",
        root / "schemas/datapan.operation-observation-plan.v1.schema.json",
        root / "schemas/datapan.operation-document-evidence.v1.schema.json",
        root / "schemas/datapan.operation-observation-policy.v1.schema.json",
        root / "schemas/datapan.operation-response-assertion.v2.schema.json",
        root / "schemas/datapan.operation-document-evidence.v2.schema.json",
        root / "schemas/datapan.operation-document-capture-receipt.v2.schema.json",
        root / "schemas/datapan.operation-document-work-item.v2.schema.json",
        root / "schemas/datapan.operation-document-reconciliation.v2.schema.json",
        root / "reports/operation-document-evidence/queue.v2.jsonl",
        root / "reports/operation-document-evidence/reconciliation.v2.json",
    ]
    for denominator_path in DENOMINATOR_PATHS.values():
        denominator_file = root / denominator_path.relative_to(ROOT)
        denominator_document = load_json(denominator_file)
        source_artifact_paths.extend([
            denominator_file,
            root / denominator_document["provenance"]["source_profile"],
            root / denominator_document["provenance"]["catalog_artifact"],
        ])
    source_artifact_paths.extend(root / captured["artifact_ref"]["path"] for captured in document_evidence.values())
    source_artifact_paths.extend(
        root / captured["artifact_ref"]["path"]
        for (source_id, _operation_id), captured in document_evidence_v2.items()
        if source_id == "data_go_kr"
    )
    source_artifact_paths.extend(
        root / "reports/operation-document-evidence/v2/receipts" / Path(captured["artifact_ref"]["path"]).name
        for (source_id, _operation_id), captured in document_evidence_v2.items()
        if source_id == "data_go_kr"
    )
    source_artifact_paths.extend(root / ref["path"] for ref in reviewed_policy_inputs["assertions"].values())
    source_artifact_paths.extend([
        root / reviewed_policy_ref["path"],
        root / reviewed_policy_inputs["policy_schema"]["path"],
        root / reviewed_policy_inputs["assertion_schema"]["path"],
    ])
    source_artifacts_by_path = {ref["path"]: ref for ref in (artifact_ref(path, root) for path in source_artifact_paths)}
    data_go_scope = {
        "source_id": "data_go_kr",
        "provider": "data.go.kr",
        "adapter_id": data_go_adapter_id,
        "inventory_status": "source_complete",
        "inventory_unknown": False,
        "test_only": False,
        "source_artifacts": [source_artifacts_by_path[path] for path in sorted(source_artifacts_by_path)],
    }

    scope_records: dict[str, dict[str, Any]] = {}
    scope_specs: dict[str, dict[str, Any]] = {"data_go_kr": data_go_scope}
    rows_by_source: dict[str, list[dict[str, Any]]] = {"data_go_kr": []}
    generated_assertion_outputs: dict[str, bytes] = {}
    policy_document = load_json(root / reviewed_policy_ref["path"])
    for index, operation in enumerate(operations):
        provenance = operation.get("provenance", {})
        transport = operation.get("transport", {})
        operation_id = operation["operation_id"]
        protocol = operation.get("protocol")
        identity: dict[str, Any] = {}
        for source_name, target_name in (
            ("dataset_id", "dataset_id"),
            ("operation_name", "operation_name"),
            ("upstream_operation_key", "upstream_operation_key"),
        ):
            value = provenance.get(source_name)
            if isinstance(value, str) and value:
                identity[target_name] = value
        endpoint = registered_endpoint(transport.get("endpoint"))
        if endpoint:
            identity["registered_endpoint"] = endpoint
        legacy = None
        selector = (str(provenance.get("dataset_id", "")), str(provenance.get("operation_name", "")))
        if selector in legacy_map:
            canary_index, canary = legacy_map[selector]
            fail(str(canary.get("upstream_operation_seq", canary.get("endpoint_correction", {}).get("upstream_operation_seq", ""))) in {
                "", str(provenance.get("upstream_operation_key", ""))
            }, f"legacy canary selector does not match source operation key: {canary['operation_id']}")
            identity["legacy_selectors"] = [canary["operation_id"]]
            legacy = legacy_policy_record(canary, canary_index, policy_ref)
            legacy_map.pop(selector)
        plan = make_incomplete_plan(
            scope=data_go_scope,
            operation_id=operation_id,
            protocol=protocol,
            identity=identity,
            evidence=evidence_ref(manifest_ref, f"#/operations/{index}", "operation_manifest"),
            legacy_policy=legacy,
        )
        if endpoint is None:
            plan["request_plan"]["missing_fields"].append("registered_endpoint_missing")
        reviewed = reviewed_policies.get(operation_id)
        captured_v2 = document_evidence_v2.get(("data_go_kr", operation_id))
        captured = reviewed["captured"] if reviewed else (document_evidence.get(operation_id) or captured_v2)
        if captured:
            document = captured["document"]
            if document.get("schema_version") == "datapan.operation-document-evidence.v2":
                apply_document_evidence_v2(plan, captured)
            else:
                plan["request_plan"]["evidence_refs"].extend(
                    evidence_ref(captured["artifact_ref"], pointer, "operation_document")
                    for pointer in document_evidence_pointers(document)
                )
                _resolve_documented_missing_fields(plan, document)
            if reviewed:
                plan = compile_reviewed_operation_plan(
                    plan,
                    operation,
                    document,
                    captured["artifact_ref"],
                    reviewed["policy"],
                    reviewed["artifact_ref"],
                    reviewed["index"],
                    reviewed["assertion"],
                    reviewed["assertion_ref"],
                    root,
                )
            else:
                profile_matches = [
                    (profile_index, profile)
                    for profile_index, profile in enumerate(reviewed_policy_inputs["profiles"])
                    if profile_matches_operation(profile, "data_go_kr", "data.go.kr", operation, document)
                ]
                fail(len(profile_matches) <= 1, f"multiple reviewed operation profiles match the same documented operation: {operation_id}")
                if profile_matches:
                    profile_index, profile = profile_matches[0]
                    operation_identity = operation_policy_identity("data_go_kr", "data.go.kr", operation)
                    assertion_artifact = compile_profile_assertion(
                        profile,
                        operation_identity,
                        document,
                        captured["artifact_ref"],
                        reviewed_policy_ref,
                        profile_index,
                    )
                    assertion_schema = load_json(root / reviewed_policy_inputs["assertion_schema"]["path"])
                    assertion_validator = jsonschema.Draft202012Validator(assertion_schema, format_checker=jsonschema.FormatChecker())
                    assertion_validator.validate(assertion_artifact)
                    _validate_assertion_v2_fact_binding(assertion_artifact, document, captured["artifact_ref"])
                    assertion_path = f"reports/operation-response-assertions/{operation_id}.json"
                    assertion_bytes = pretty_json(assertion_artifact)
                    assertion_ref = {"path": assertion_path, "sha256": sha256(assertion_bytes), "bytes": len(assertion_bytes)}
                    fail(assertion_path not in generated_assertion_outputs, f"duplicate generated assertion path for operation: {operation_id}")
                    generated_assertion_outputs[assertion_path] = assertion_bytes
                    profile_policy = {
                        "identity": operation_identity,
                        "document_evidence": captured["artifact_ref"],
            "review": profile["review"],
            "request": {
                **profile["request"],
                "response_assertion_artifact": assertion_ref,
            },
            "_effect_selector": profile["selector"],
        }
                    if "runtime_binding" in profile:
                        profile_policy["runtime_binding"] = profile["runtime_binding"]
                    plan = compile_reviewed_operation_plan(
                        plan,
                        operation,
                        document,
                        captured["artifact_ref"],
                        profile_policy,
                        reviewed_policy_ref,
                        profile_index,
                        assertion_artifact,
                        assertion_ref,
                    root,
                    policy_pointer_base=f"#/profiles/{profile_index}",
                )
        if captured_v2 and plan["request_plan"]["status"] != "complete":
            if captured is not captured_v2:
                apply_document_evidence_v2(plan, captured_v2)
            effect_matches = [
                (effect_index, effect_profile)
                for effect_index, effect_profile in enumerate(reviewed_policy_inputs["effect_profiles"])
                if effect_profile_matches_operation(
                    effect_profile,
                    "data_go_kr",
                    "data.go.kr",
                    operation,
                    captured_v2["document"],
                )
            ]
            fail(len(effect_matches) <= 1, f"multiple reviewed effect profiles match the same operation: {operation_id}")
            if effect_matches:
                effect_index, effect_profile = effect_matches[0]
                apply_effect_profile_review(
                    plan,
                    effect_profile,
                    effect_index,
                    captured_v2["document"],
                    captured_v2["artifact_ref"],
                    reviewed_policy_ref,
                )
        rows_by_source["data_go_kr"].append(plan)
    fail(not legacy_map, "one or more legacy canaries do not map to the pinned operation manifest")

    denominator_paths: list[Path] = []
    for source_id, path in DENOMINATOR_PATHS.items():
        denominator = denominator_documents[source_id]
        fail(denominator.get("source_id") == source_id, f"operation denominator source ID mismatch: {source_id}")
        fail(denominator.get("scope", {}).get("kind") == "enumerated_supported_operations", f"non-enumerated scope is not an operation denominator: {source_id}")
        fail(denominator.get("scope", {}).get("unknown_upstream_operations_covered") is False, f"unknown upstream inventory mislabeled as covered: {source_id}")
        operation_rows = denominator.get("operations")
        fail(isinstance(operation_rows, list) and len(operation_rows) > 0, f"registered operation denominator is empty: {source_id}")
        operation_ids = [row.get("operation_id") for row in operation_rows]
        fail(all(isinstance(value, str) and value for value in operation_ids), f"empty operation ID in {source_id}")
        fail(len(operation_ids) == len(set(operation_ids)), f"duplicate operation ID in {source_id}")
        summary = denominator.get("summary", {})
        fail(summary.get("operations") == len(operation_rows), f"operation denominator count mismatch: {source_id}")
        fail(isinstance(summary.get("callable_operations"), int) and 0 <= summary["callable_operations"] <= len(operation_rows), f"invalid callable count: {source_id}")
        profile = root / denominator["provenance"]["source_profile"]
        profile_document = load_json(profile)
        catalog = root / denominator["provenance"]["catalog_artifact"]
        adapter_id = checked_adapter_id(source_id, denominator["provider"], profile_document)
        catalog_document = load_json(catalog)
        check_partial_catalog_identity_set(
            source_id,
            denominator["provider"],
            denominator["provenance"]["source_profile"],
            denominator,
            catalog_document,
        )
        denominator_ref = artifact_ref(root / path.relative_to(ROOT), root)
        source_scope = {
            "source_id": source_id,
            "provider": denominator["provider"],
            "adapter_id": adapter_id,
        "inventory_status": "partial",
        "inventory_unknown": True,
        "test_only": False,
            "source_artifacts": [
                denominator_ref,
                artifact_ref(profile, root),
                artifact_ref(catalog, root),
                *(
                    [document_evidence_v2[(source_id, denominator["operations"][0]["operation_id"])]["artifact_ref"]]
                    if (source_id, denominator["operations"][0]["operation_id"]) in document_evidence_v2
                    else []
                ),
                *(
                    [
                        artifact_ref(root / "schemas/datapan.operation-document-evidence.v2.schema.json", root),
                        artifact_ref(root / "scripts/operation_document_evidence.py", root),
                    ]
                    if (source_id, denominator["operations"][0]["operation_id"]) in document_evidence_v2
                    else []
                ),
            ],
        }
        scope_specs[source_id] = source_scope
        rows_by_source[source_id] = []
        for index, operation in enumerate(operation_rows):
            identity = {}
            endpoint = registered_endpoint(operation.get("endpoint_template"))
            if endpoint:
                identity["registered_endpoint"] = endpoint
            captured_v2 = document_evidence_v2.get((source_id, operation["operation_id"]))
            operation_protocol = "HTTP"
            if captured_v2:
                evidence_identity = captured_v2["document"]["identity"]
                operation_protocol = evidence_identity["protocol"]
                if evidence_identity.get("operation_name"):
                    identity["operation_name"] = evidence_identity["operation_name"]
            record = make_incomplete_plan(
                scope=source_scope,
                operation_id=operation["operation_id"],
                protocol=operation_protocol,
                identity=identity,
                evidence=evidence_ref(denominator_ref, f"#/operations/{index}", "operation_denominator"),
            )
            if endpoint is None:
                record["request_plan"]["missing_fields"].append("registered_endpoint_missing")
            if captured_v2:
                apply_document_evidence_v2(record, captured_v2)
            rows_by_source[source_id].append(record)
        denominator_paths.append(root / path.relative_to(ROOT))

    all_pairs: set[tuple[str, str]] = set()
    source_scopes = []
    records_by_source: dict[str, list[dict[str, Any]]] = {}
    for source_id in sorted(rows_by_source):
        records = sorted(rows_by_source[source_id], key=lambda row: row["operation_identity"]["operation_id"])
        ids = [row["operation_identity"]["operation_id"] for row in records]
        for operation_id in ids:
            pair = (source_id, operation_id)
            fail(pair not in all_pairs, f"duplicate source-scoped operation identity: {source_id}")
            all_pairs.add(pair)
        scope = scope_specs[source_id]
        source_scopes.append({
            **scope,
            "registered_operations": len(ids),
            "identity_set_sha256": identity_set_digest(ids),
        })
        records_by_source[source_id] = records

    shards_by_path: dict[str, bytes] = {}
    shard_index: list[dict[str, Any]] = []
    for source_id in sorted(records_by_source):
        records = records_by_source[source_id]
        for shard_number, start in enumerate(range(0, len(records), SHARD_SIZE)):
            chunk = records[start : start + SHARD_SIZE]
            relative_path = f"reports/operation-observation-plan/shards/{source_id}-{shard_number:04d}.json"
            shard = {
                "schema_version": "datapan.operation-observation-plan.v1",
                "artifact_kind": "shard",
                "source_id": source_id,
                "shard_index": shard_number,
                "records": chunk,
            }
            contents = pretty_json(shard)
            shards_by_path[relative_path] = contents
            shard_index.append({
                "source_id": source_id,
                "shard_index": shard_number,
                "path": relative_path,
                "sha256": sha256(contents),
                "bytes": len(contents),
                "record_count": len(chunk),
                "first_operation_id": chunk[0]["operation_identity"]["operation_id"],
                "last_operation_id": chunk[-1]["operation_identity"]["operation_id"],
            })

    all_records = [record for records in records_by_source.values() for record in records]
    request_counts = Counter(record["request_plan"]["status"] for record in all_records)
    runtime_counts = Counter(record["runtime_binding"]["status"] for record in all_records)
    admission_counts = Counter(record["admission"]["status"] for record in all_records)
    operation_denominator_refs = [artifact_ref(path, root) for path in sorted(denominator_paths)]
    index = {
        "schema_version": "datapan.operation-observation-plan.v1",
        "artifact_kind": "index",
        "registry_revision": revision,
        "generation_inputs": {
            "generator_path": "scripts/generate-operation-observation-plan.py",
            "generator_sha256": sha256((root / "scripts/generate-operation-observation-plan.py").read_bytes()),
            "operation_manifest": manifest_ref,
            "operation_denominators": operation_denominator_refs,
            "legacy_policy": policy_ref,
            "provider_index": provider_index_ref,
            "document_evidence": sorted(
                [captured["artifact_ref"] for _operation_id, captured in document_evidence.items()]
                + [captured["artifact_ref"] for _identity, captured in document_evidence_v2.items()],
                key=lambda ref: ref["path"],
            ),
        },
        "inventory_context": {
            "separate_link_operations": manifest["summary"]["exclusions"]["link_operations"],
            "provider_index_adapter_entries": len(adapters),
            "provider_index_entries_counted_as_operations": False,
        },
        "summary": {
            "known_operations": len(all_records),
            "request_plans_complete": request_counts["complete"],
            "request_plans_incomplete": request_counts["incomplete"],
            "runtime_bindings_bound": runtime_counts["bound"],
            "runtime_bindings_unbound": runtime_counts["unbound"],
            "admitted": admission_counts["admitted"],
            "not_admitted": admission_counts["not_admitted"],
            "inventory_unknown_scopes": sum(scope["inventory_unknown"] for scope in source_scopes),
        },
        "source_scopes": source_scopes,
        "shards": shard_index,
    }
    return index, {
        **generated_assertion_outputs,
        **shards_by_path,
        "reports/operation-observation-plan/index.json": pretty_json(index),
    }


def _validate_complete_contract_evidence(
    record: dict[str, Any],
    contract: dict[str, Any],
    ref_targets: list[tuple[dict[str, Any], Any]],
    root: Path = ROOT,
) -> None:
    transport = contract["transport"]
    source_id = record["source_binding"]["source_id"]

    def matching_target(refs: list[dict[str, Any]], suffix: str) -> Any:
        matches = [
            target
            for ref, target in ref_targets
            if ref in refs and ref["json_pointer"].endswith(suffix)
        ]
        fail(len(matches) == 1, f"complete request contract needs one evidence pointer ending in {suffix}")
        return matches[0]

    if source_id == "synthetic_test":
        transport_fact = matching_target(transport["evidence_refs"], "/transport")
        for field in ("scheme", "host", "path", "http_method", "soap_action", "soap_version", "envelope_namespace", "body_encoding"):
            if field in transport:
                fail(transport_fact.get(field) == transport[field], f"transport evidence mismatch: {field}")
        if "operation_qname" in transport:
            fail(transport_fact.get("operation_qname") == transport["operation_qname"], "SOAP operation QName evidence mismatch")

        effect_fact = matching_target(contract["operation_effect"]["evidence_refs"], "/effect")
        fail(effect_fact.get("classification") == contract["operation_effect"]["classification"], "read-only effect evidence mismatch")

        inventory = matching_target(contract["parameter_inventory_evidence_refs"], "/parameters")
        fail(isinstance(inventory, list) and len(inventory) == len(contract["parameters"]), "parameter inventory evidence does not match the request contract")
        for index, parameter in enumerate(contract["parameters"]):
            param_fact = matching_target(parameter["evidence_refs"], f"/parameters/{index}")
            for field in ("name", "location", "cardinality"):
                fail(param_fact.get(field) == parameter[field], f"parameter evidence mismatch: {field}")
            strategy_fact = param_fact.get("value_strategy", {})
            strategy = parameter["value_strategy"]
            for field in ("kind", "minimum", "maximum", "selection", "selected_value", "offset_years", "minimum_year", "maximum_year", "anchor"):
                if field in strategy:
                    fail(strategy_fact.get(field) == strategy[field], f"parameter value-strategy evidence mismatch: {field}")

        for field, suffix in (("authentication", "/authentication"), ("limits", "/limits"), ("response_assertion", "/response_assertion")):
            fact = matching_target(contract[field]["evidence_refs"], suffix)
            for name, value in contract[field].items():
                if name not in {"evidence_refs"}:
                    fail(fact.get(name) == value, f"{field} evidence mismatch: {name}")
    else:
        def exact_target(refs: list[dict[str, Any]], pointer: str, kind: str) -> Any:
            matches = [target for ref, target in ref_targets if ref in refs and ref["json_pointer"] == pointer and ref["evidence_kind"] == kind]
            fail(len(matches) == 1, f"complete production contract needs one {kind} evidence pointer at {pointer}")
            return matches[0]

        def fact_value(value: Any) -> Any:
            return value.get("value") if isinstance(value, dict) and "value" in value else value

        for field in ("scheme", "host", "path", "http_method", "soap_action", "soap_version", "envelope_namespace", "body_encoding"):
            if field in transport:
                source_fact = exact_target(transport["evidence_refs"], f"#/transport/{field}", "operation_document")
                fail(isinstance(source_fact, dict) and source_fact.get("status") == "documented", f"transport evidence is not documented: {field}")
                evidence_value = fact_value(source_fact)
                expected_value = transport[field]
                if field == "http_method":
                    evidence_value = str(evidence_value).upper()
                fail(evidence_value == expected_value, f"transport evidence mismatch: {field}")
        if "port" in transport:
            port_fact = exact_target(transport["evidence_refs"], "#/transport/port", "operation_document")
            fail(type(port_fact) is int and port_fact == transport["port"], "transport evidence mismatch: port")
        if "operation_qname" in transport:
            source_fact = exact_target(transport["evidence_refs"], "#/transport/operation_qname", "operation_document")
            fail(isinstance(source_fact, dict) and source_fact.get("status") == "documented", "SOAP QName evidence is not documented")
            fail(_clark_qname(source_fact.get("value"), "operation_qname") == transport["operation_qname"], "SOAP operation QName evidence mismatch")

        policy_row_refs = [
            (ref, target)
            for ref, target in ref_targets
            if ref["evidence_kind"] == "reviewed_policy"
            and ref["artifact_path"].endswith("operation-observation-policies.v1.json")
            and re.fullmatch(r"#/(?:policies|profiles)/[0-9]+", ref["json_pointer"])
        ]
        policy_rows = [(ref, target) for ref, target in policy_row_refs if isinstance(target, dict) and "request" in target]
        fail(len(policy_rows) == 1, "complete request plan must bind exactly one reviewed operation policy or profile row")
        policy_root_ref, policy_row = policy_rows[0]
        is_profile_policy = policy_root_ref["json_pointer"].startswith("#/profiles/")
        request_policy = policy_row["request"]
        policy_limit_ref = next(
            (ref for ref, target in ref_targets if target == request_policy["limits"] and ref["json_pointer"].endswith("/request/limits")),
            None,
        )
        fail(policy_limit_ref is not None, "request limits lack a reviewed-policy evidence pointer")
        fail({key: value for key, value in contract["limits"].items() if key != "evidence_refs"} == request_policy["limits"], "request limits differ from reviewed policy")

        inventory = exact_target(contract["parameter_inventory_evidence_refs"], "#/parameters", "operation_document")
        fail(isinstance(inventory, list), "parameter inventory evidence is not an operation parameter list")
        strategies = {row["name"]: row["strategy"] for row in request_policy["parameter_strategies"]}
        source_by_name = {row["name"].casefold(): (index, row) for index, row in enumerate(inventory)}
        strategies_by_name = {name.casefold(): strategy for name, strategy in strategies.items()}
        fail(len(source_by_name) == len(inventory), "documented parameter inventory has case-insensitive duplicate names")
        contract_by_name = {row["name"].casefold(): row for row in contract["parameters"]}
        fail(len(contract_by_name) == len(contract["parameters"]), "request contract has case-insensitive duplicate parameter names")
        fail(set(contract_by_name).issubset(source_by_name), "request contract includes an undocumented parameter")
        auth_fact_for_inventory = exact_target(contract["authentication"]["evidence_refs"], "#/authentication", "operation_document")
        auth_names_for_inventory = {
            name.casefold() for name in auth_fact_for_inventory.get("parameter_names", [])
        } if auth_fact_for_inventory.get("requirement") == "required" else set()
        fail(set(strategies_by_name).issubset(set(source_by_name) - auth_names_for_inventory), "reviewed policy strategy names an absent or credential parameter")
        fail(set(contract_by_name) == (set(strategies_by_name) | auth_names_for_inventory), "request contract does not reflect the reviewed values and documented credential")
        for name, (source_index, source_parameter) in source_by_name.items():
            if name in auth_names_for_inventory or name in strategies_by_name:
                fail(name in contract_by_name, f"request contract omits a required or selected parameter: {source_parameter['name']}")
                continue
            fail(request_policy.get("omit_unmapped_optional_parameters") is True, "request policy does not authorize omission of unmapped optional parameters")
            fail(
                source_parameter.get("requiredness", {}).get("status") == "documented"
                and source_parameter.get("requiredness", {}).get("value") == "optional"
                and source_parameter.get("cardinality", {}).get("status") == "documented"
                and source_parameter.get("cardinality", {}).get("minimum") == 0,
                f"request contract omits a parameter not documented optional: {source_parameter['name']}",
            )
        for index, parameter in enumerate(contract["parameters"]):
            source_index, source_parameter = source_by_name[parameter["name"].casefold()]
            name_fact = exact_target(parameter["evidence_refs"], f"#/parameters/{source_index}/name", "operation_document")
            location_fact = exact_target(parameter["evidence_refs"], f"#/parameters/{source_index}/location", "operation_document")
            required_fact = exact_target(parameter["evidence_refs"], f"#/parameters/{source_index}/requiredness", "operation_document")
            cardinality_fact = exact_target(parameter["evidence_refs"], f"#/parameters/{source_index}/cardinality", "operation_document")
            data_type_fact = exact_target(parameter["evidence_refs"], f"#/parameters/{source_index}/data_type", "operation_document")
            fail(name_fact == parameter["name"], "request parameter name differs from source facts")
            fail(isinstance(location_fact, dict) and location_fact.get("status") == "documented" and location_fact.get("value") == parameter["location"], "request parameter location differs from source facts")
            fail(isinstance(required_fact, dict) and required_fact.get("status") == "documented", "request parameter requiredness is not documented")
            fail(isinstance(cardinality_fact, dict) and cardinality_fact.get("status") == "documented", "request parameter cardinality is not documented")
            fail(isinstance(data_type_fact, dict) and data_type_fact.get("status") == "documented", "request parameter type is not documented")
            fail(_parameter_cardinality(source_parameter) == parameter["cardinality"], "request parameter cardinality differs from source facts")
            if parameter["value_strategy"]["kind"] == "credential_reference":
                fail(parameter["value_strategy"] == {"kind": "credential_reference", "authority": "runtime_binding", "binding_field": "credential_reference"}, "credential strategy is not an opaque runtime reference")
            else:
                strategy = strategies.get(parameter["name"])
                fail(strategy is not None, "request parameter has no exact reviewed strategy")
                _require_policy_strategy_matches_type(strategy, data_type_fact.get("value"), parameter["name"])
                fail(
                    {"kind": strategy["kind"], **{key: value for key, value in strategy.items() if key != "kind"}}
                    == {key: value for key, value in parameter["value_strategy"].items() if key != "authority"},
                    "request parameter value differs from the reviewed strategy",
                )

        authentication_fact = exact_target(contract["authentication"]["evidence_refs"], "#/authentication", "operation_document")
        authentication = contract["authentication"]
        fail(authentication_fact.get("status") == "documented", "authentication placement is not documented")
        for name in ("requirement", "mechanism", "placement"):
            fail(authentication_fact.get(name) == authentication[name], f"authentication fact mismatch: {name}")
        if authentication["requirement"] == "required":
            source_names = authentication_fact.get("parameter_names", [])
            plan_name = authentication.get("parameter_name", authentication.get("header_name"))
            if plan_name is not None:
                fail(source_names == [plan_name], "authentication parameter name differs from source facts")

        assertion_artifact_target = None
        assertion_reference = contract["response_assertion"].get("assertion_ref", "")
        assertion_path, marker, assertion_pointer = assertion_reference.partition("#")
        fail(marker == "#" and assertion_pointer in {"", "/assertion"}, "response assertion reference is not a typed artifact pointer")
        assertion_artifact_target = exact_target(
            contract["response_assertion"]["evidence_refs"],
            "#" + assertion_pointer,
            "reviewed_policy",
        )
        assertion_rows = [
            target for ref, target in ref_targets
            if ref["evidence_kind"] == "reviewed_policy"
            and ref["artifact_path"] == assertion_path
            and ref["json_pointer"] == "#" + assertion_pointer
        ]
        fail(len(assertion_rows) == 1 and assertion_artifact_target == assertion_rows[0], "response assertion does not resolve to its manifest-bound policy artifact")
        assertion = assertion_artifact_target
        assertion_source_refs = []
        observation_only = assertion == {"mode": "observation_only"}
        if observation_only:
            response_plan = contract["response_assertion"]
            fail(response_plan["kind"] == "observation_only", "observation-only artifact must map to the observation-only plan kind")
            fail(response_plan["empty_result_semantics"] == "not_applicable", "observation-only mode must not claim empty-result semantics")
            fail("expected_status_codes" not in response_plan, "observation-only mode must not claim accepted HTTP statuses")
            exact_target(response_plan["evidence_refs"], "#/response_contract", "operation_document")
            exact_target(response_plan["evidence_refs"], policy_root_ref["json_pointer"] + "/request/response_assertion_artifact", "reviewed_policy")
            for pointer in ("#/transport/http_method", "#/operation_document/title", "#/operation_document/purpose", "#/authentication", "#/parameters"):
                exact_target(response_plan["evidence_refs"], pointer, "operation_document")
        else:
            # The target is the assertion subobject; the surrounding artifact identity is checked while loading it.
            expected_kind = {"json": "json_contract", "xml": "xml_contract", "soap_xml": "soap_fault_free"}[assertion["payload_kind"]]
            fail(contract["response_assertion"]["kind"] == expected_kind, "response assertion kind differs from its typed artifact")
            branch_statuses = sorted({code for branch in assertion["branches"] for code in branch["selector"]["accepted_http_status_codes"]})
            branch_empty_semantics = {branch["empty_result_semantics"] for branch in assertion["branches"] if branch["classification"] == "success"}
            fail(contract["response_assertion"]["expected_status_codes"] == branch_statuses, "accepted response status codes differ from the reviewed assertion branches")
            fail(len(branch_empty_semantics) == 1 and contract["response_assertion"]["empty_result_semantics"] == next(iter(branch_empty_semantics)), "empty-result semantics differ from the reviewed assertion branches")
            fail(contract["response_assertion"]["kind"] in {"json_contract", "xml_contract", "soap_fault_free"}, "HTTP-status-only assertion cannot establish response semantics")
            for branch in assertion["branches"]:
                assertion_source_refs.extend(branch["source_refs"])
                assertion_source_refs.extend(branch["http_status_source_refs"])
                assertion_source_refs.extend(branch["provider_result_code_evidence_refs"])
                assertion_source_refs.extend(ref for field in branch["required_fields"] for ref in field["source_refs"])
                if "provider_result_codes" in branch:
                    assertion_source_refs.extend(branch["provider_result_codes"]["source_refs"])
                    for error_class in branch["provider_result_codes"].get("error_classes", []):
                        assertion_source_refs.extend(error_class["source_refs"])
                if branch.get("result_collection") is not None:
                    assertion_source_refs.extend(branch["result_collection"]["source_refs"])
                assertion_source_refs.extend(branch["review_refs"])
                for discriminator in branch["selector"]["discriminators"]:
                    assertion_source_refs.extend(discriminator["source_refs"])
            for source_ref in assertion_source_refs:
                fail(source_ref in contract["response_assertion"]["evidence_refs"], "response assertion omits an exact operation-document fact reference")
        assertion_artifact_refs = [
            ref for ref, _target in ref_targets
            if ref["artifact_path"] == assertion_path and ref["json_pointer"] == "#/assertion"
        ]
        fail(len(assertion_artifact_refs) == 1, "assertion artifact has no unique reviewed binding")
        assertion_document = _load_json_rejecting_duplicate_keys(root / assertion_path)
        actual_assertion_ref = artifact_ref(root / assertion_path, root)
        fail(assertion_artifact_refs[0]["sha256"] == actual_assertion_ref["sha256"], "assertion ref digest differs from the stored reviewed assertion")
        assertion_schema = load_json(root / RESPONSE_ASSERTION_SCHEMA_PATH.relative_to(ROOT))
        jsonschema.Draft202012Validator(assertion_schema, format_checker=jsonschema.FormatChecker()).validate(assertion_document)
        expected_identity = record["operation_identity"]
        fail(
            assertion_document.get("operation_identity") == {
                key: expected_identity[key]
                for key in ("operation_id", "dataset_id", "operation_name", "upstream_operation_key")
            },
            "response assertion operation identity differs from its selected operation",
        )
        source_document_refs = {
            (ref["artifact_path"], ref["sha256"])
            for ref, _target in ref_targets
            if ref["evidence_kind"] == "operation_document"
        }
        fail(len(source_document_refs) == 1, "complete request plan must bind one exact operation-document artifact")
        source_document_path, source_document_sha = next(iter(source_document_refs))
        source_document = load_json(root / source_document_path)
        source_document_ref = artifact_ref(root / source_document_path, root)
        fail(source_document_ref["sha256"] == source_document_sha, "operation-document artifact digest changed after plan binding")
        fail(source_document.get("identity", {}).get("operation_id") == expected_identity["operation_id"], "operation-document identity differs from its selected operation")
        fail(assertion_document.get("document_evidence") == source_document_ref, "response assertion document binding differs from the selected operation")
        _validate_assertion_v2_fact_binding(assertion_document, source_document, source_document_ref)
        if observation_only:
            fail(assertion_document.get("review") == policy_row["review"], "observation-only assertion review differs from its selected policy")
        if contract["operation_effect"]["authority"] == "operation_document":
            effect_fact = exact_target(contract["operation_effect"]["evidence_refs"], "#/effect", "operation_document")
            fail(effect_fact.get("classification") == "read_only" and effect_fact.get("status") == "documented", "read-only effect evidence mismatch")
        elif contract["operation_effect"]["authority"] == "reviewed_policy":
            effect_review = request_policy.get("effect_review")
            fail(isinstance(effect_review, dict), "reviewed effect authority has no selected effect policy")
            effect_review_target = exact_target(
                contract["operation_effect"]["evidence_refs"],
                policy_root_ref["json_pointer"] + "/request/effect_review",
                "reviewed_policy",
            )
            fail(effect_review_target == effect_review, "reviewed effect evidence differs from its policy row")
            review_target = exact_target(
                contract["operation_effect"]["evidence_refs"],
                policy_root_ref["json_pointer"] + "/review",
                "reviewed_policy",
            )
            fail(review_target == policy_row["review"], "reviewed effect lacks its policy review record")
            if is_profile_policy:
                selector = policy_row["selector"]
                exact_target(contract["operation_effect"]["evidence_refs"], policy_root_ref["json_pointer"] + "/selector", "reviewed_policy")
            else:
                selector = {
                    "source_id": record["source_binding"]["source_id"],
                    "provider": record["source_binding"]["provider"],
                    "protocol": transport["protocol"],
                    "method": transport["http_method"],
                }
            exact_target(contract["operation_effect"]["evidence_refs"], "#/transport/http_method", "operation_document")
            exact_target(contract["operation_effect"]["evidence_refs"], "#/operation_document/title", "operation_document")
            exact_target(contract["operation_effect"]["evidence_refs"], "#/operation_document/purpose", "operation_document")
            fail(
                reviewed_read_only_effect_matches(
                    selector,
                    effect_review,
                    record["source_binding"]["source_id"],
                    record["source_binding"]["provider"],
                    {"operation_id": expected_identity["operation_id"], "protocol": transport["protocol"]},
                    source_document,
                ),
                "reviewed read-only effect no longer matches the source facts",
            )
        else:
            raise PlanError("unsupported production operation-effect authority")
        if is_profile_policy:
            profile_response = request_policy["response"]
            fail(profile_response["payload_kind"] == assertion["payload_kind"], "profile response payload kind differs from its assertion")
            fail(len(profile_response["branches"]) == len(assertion["branches"]), "compiled response branch count differs from the reusable profile")
            for profile_branch, assertion_branch in zip(profile_response["branches"], assertion["branches"]):
                fail(profile_branch["branch_id"] == assertion_branch["branch_id"], "compiled branch ID differs from the reusable profile")
                fail(profile_branch["classification"] == assertion_branch["classification"], "compiled branch classification differs from the reusable profile")
                selector = assertion_branch["selector"]
                profile_selector = profile_branch["selector"]
                fail(selector["root_kind"] == profile_selector["root_kind"], "compiled branch root kind differs from the reusable profile")
                fail(selector["accepted_http_status_codes"] == profile_selector["accepted_http_status_codes"], "compiled branch statuses differ from the reusable profile")
                if "root_qname" in profile_selector:
                    fail(selector.get("root_qname") == profile_selector["root_qname"], "compiled branch root QName differs from the reusable profile")
                expected_fields = [
                    {"path": field["path"], "value_type": field["value_type"], "cardinality": {"minimum": field["minimum"], "maximum": field["maximum"]}}
                    for field in profile_branch["required_fields"]
                ]
                actual_fields = [{key: field[key] for key in ("path", "value_type", "cardinality")} for field in assertion_branch["required_fields"]]
                fail(actual_fields == expected_fields, "compiled response fields differ from the reviewed reusable profile")
                if profile_branch["code_mode"] == "none":
                    fail(assertion_branch["provider_result_code_status"] == "none_by_policy", "profile code_mode=none is not represented by the typed assertion")
                    fail(bool(profile_branch["code_mode_rationale"]), "profile code_mode=none lacks reviewed rationale")
                else:
                    fail(assertion_branch["provider_result_code_status"] in {"documented", "expected_success_example", "not_applicable"}, "profile source code mode lacks an exact typed source predicate")
            fail(assertion_document["review"] == policy_row["review"], "generated response assertion review differs from the reusable profile")
            fail(assertion_path == f"reports/operation-response-assertions/{expected_identity['operation_id']}.json", "generated profile assertion path is not operation-scoped")
            fail(len(assertion_artifact_refs) == 1, "generated profile assertion is not uniquely digest-bound")
        else:
            policy_assertion = request_policy["response_assertion_artifact"]
            fail(assertion_artifact_refs[0]["sha256"] == policy_assertion["sha256"], "assertion ref digest differs from the reviewed policy binding")

        runtime_policy_ref = next(
            ((ref, target) for ref, target in ref_targets if ref in record["runtime_binding"].get("evidence_refs", []) and ref["evidence_kind"] == "reviewed_policy"),
            None,
        )
        if record["runtime_binding"]["status"] == "bound":
            fail(runtime_policy_ref is not None and isinstance(runtime_policy_ref[1], dict), "bound runtime policy lacks reviewed source evidence")
            runtime_policy = runtime_policy_ref[1]
            fail(runtime_policy.get("observation_period_seconds") == record["runtime_binding"]["observation_period_seconds"], "runtime cadence differs from reviewed policy")
            fail(runtime_policy.get("credential_reference") == record["runtime_binding"].get("credential_reference"), "runtime credential reference differs from reviewed policy")
            fail(runtime_policy.get("credential_scope_key") == record["runtime_binding"].get("credential_scope_key"), "runtime credential quota scope differs from reviewed policy")
            for quota in record["runtime_binding"]["quota_policies"]:
                matching = [target for ref, target in ref_targets if ref in quota["evidence_refs"]]
                fail(len(matching) == 1 and all(quota.get(key) == matching[0].get(key) for key in ("scope_kind", "scope_key", "max_concurrent", "requests_per_window", "window_seconds", "minimum_interval_seconds")), "runtime quota constraint differs from reviewed policy")

    for parameter in contract["parameters"]:
        strategy = parameter["value_strategy"]
        if "value_ref" in strategy:
            ref = strategy["value_ref"]
            matches = [target for candidate, target in ref_targets if candidate == ref]
            fail(len(matches) == 1, "reviewed value digest reference must resolve exactly once")
            fail(sha256(compact_json(matches[0])) == strategy["value_sha256"], "reviewed value digest does not match its referenced value")


def validate_record(
    record: dict[str, Any],
    schema: dict[str, Any],
    root: Path = ROOT,
    validator: jsonschema.Draft202012Validator | None = None,
    schema_validated: bool = False,
) -> None:
    if not schema_validated:
        if validator is None:
            validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
        validator.validate(record)
    request = record["request_plan"]
    runtime = record["runtime_binding"]
    admission = record["admission"]
    fail(admission["status"] != "admitted" or (request["status"] == "complete" and runtime["status"] == "bound"), "admission requires a complete request plan and bound runtime policy")
    fail(admission["status"] != "not_admitted" or bool(admission["reasons"]), "not-admitted plans must state at least one reason")

    refs = list(request.get("evidence_refs", [])) + list(runtime.get("evidence_refs", [])) + list(admission.get("evidence_refs", []))
    contract = request.get("request_contract")
    ref_targets: list[tuple[dict[str, Any], Any]] = []
    if contract:
        refs += contract["transport"]["evidence_refs"]
        refs += contract["operation_effect"]["evidence_refs"]
        refs += contract["parameter_inventory_evidence_refs"]
        refs += contract["authentication"]["evidence_refs"]
        refs += contract["limits"]["evidence_refs"]
        refs += contract["response_assertion"]["evidence_refs"]
        for parameter in contract["parameters"]:
            refs += parameter["evidence_refs"]
            strategy = parameter["value_strategy"]
            minimum = strategy.get("minimum")
            maximum = strategy.get("maximum")
            if strategy["kind"] == "bounded_integer":
                fail(minimum <= maximum, "bounded_integer minimum exceeds maximum")
                if strategy["selection"] == "fixed":
                    fail(minimum <= strategy["selected_value"] <= maximum, "fixed value is outside its reviewed integer bounds")
            if strategy["kind"] == "relative_year":
                fail(strategy["minimum_year"] <= strategy["maximum_year"], "relative_year minimum exceeds maximum")
            if "value_ref" in strategy:
                refs.append(strategy["value_ref"])
        transport = contract["transport"]
        effect = contract["operation_effect"]
        authentication = contract["authentication"]
        fail("?" not in transport["path"] and "#" not in transport["path"], "request transport path must not contain query or fragment data")
        fail("/" not in transport["host"] and "?" not in transport["host"], "request transport host must be a host name only")
        fail(transport["protocol"] == record["operation_identity"]["protocol"] or record["operation_identity"]["protocol"] == "HTTP", "request transport protocol differs from registered identity")
        names = [parameter["name"].casefold() for parameter in contract["parameters"]]
        fail(len(names) == len(set(names)), "request parameter names must be unique case-insensitively")
        if authentication["requirement"] == "required":
            fail(authentication["credential_reference_required"], "required authentication must use a runtime credential reference")
            if runtime["status"] == "bound":
                fail(bool(runtime.get("credential_reference")), "bound authenticated operation is missing its credential reference")
                fail(bool(runtime.get("credential_scope_key")), "bound authenticated operation is missing its credential quota-scope key")
                credential_scopes = [
                    quota
                    for quota in runtime["quota_policies"]
                    if quota["scope_kind"] == "credential"
                ]
                fail(len(credential_scopes) == 1, "bound authenticated operation must have exactly one shared credential quota scope")
                fail(credential_scopes[0]["scope_key"] == runtime["credential_scope_key"], "credential binding scope differs from its quota policy")
        else:
            fail(authentication["mechanism"] == "none" and authentication["placement"] == "none", "no-auth contract must explicitly use mechanism and placement none")
            fail(not runtime.get("credential_reference"), "unauthenticated operation must not carry a credential reference")
        if authentication["placement"] == "soap_header":
            fail(transport["protocol"] == "SOAP", "SOAP header authentication requires SOAP transport")
        credential_parameters = [
            parameter
            for parameter in contract["parameters"]
            if parameter["value_strategy"]["kind"] == "credential_reference"
        ]
        if authentication["requirement"] == "required":
            fail(len(credential_parameters) == 1, "required authentication must map to exactly one credential-reference parameter")
            credential_parameter = credential_parameters[0]
            placement = authentication["placement"]
            expected_location = {"query": "query", "header": "header", "soap_header": "soap_header"}[placement]
            fail(credential_parameter["location"] == expected_location, "credential-reference parameter location differs from authentication placement")
            fail(credential_parameter["cardinality"] == authentication["cardinality"], "credential-reference parameter cardinality differs from authentication contract")
            if placement in {"query", "header"}:
                expected_name = authentication.get("parameter_name" if placement == "query" else "header_name")
                fail(credential_parameter["name"].casefold() == expected_name.casefold(), "credential-reference parameter name differs from authentication contract")
            else:
                fail(credential_parameter.get("qualified_name") == authentication["header_qname"], "credential-reference parameter QName differs from authentication contract")
        else:
            fail(not credential_parameters, "unauthenticated operation must not declare a credential-reference parameter")
        if effect["authority"] == "synthetic_fixture":
            fail(record["source_binding"]["source_id"] == "synthetic_test", "synthetic read-only declaration cannot be used by a production source scope")
            fail(all(ref["evidence_kind"] == "synthetic_fixture" for ref in effect["evidence_refs"]), "synthetic read-only declaration requires synthetic-only evidence")
        if transport["authority"] == "synthetic_fixture":
            fail(record["source_binding"]["source_id"] == "synthetic_test", "synthetic transport authority cannot be used by a production source scope")
            fail(all(ref["evidence_kind"] == "synthetic_fixture" for ref in transport["evidence_refs"]), "synthetic transport requires synthetic-only evidence")
        else:
            fail(record["source_binding"]["source_id"] != "synthetic_test", "production method authority cannot be attached to a synthetic source scope")
            required_kind = transport["authority"]
            fail(any(ref["evidence_kind"] == required_kind for ref in transport["evidence_refs"]), "transport authority lacks its required operation-specific evidence")

    if "legacy_policy" in record:
        refs.append(record["legacy_policy"]["policy_ref"])

    for quota in runtime.get("quota_policies", []):
        expected = quota_scope_digest(quota["scope_kind"], quota["scope_key"])
        fail(quota["scope_sha256"] == expected, "quota scope digest does not match its nonsecret scope key")
        refs.extend(quota["evidence_refs"])
    quota_scopes = [(quota["scope_kind"], quota["scope_key"]) for quota in runtime.get("quota_policies", [])]
    fail(len(quota_scopes) == len(set(quota_scopes)), "duplicate quota scope in one operation plan")

    seen_evidence_refs: set[tuple[str, str, str, str]] = set()
    for ref in refs:
        ref_key = (ref["artifact_path"], ref["sha256"], ref["json_pointer"], ref["evidence_kind"])
        if ref_key in seen_evidence_refs:
            continue
        seen_evidence_refs.add(ref_key)
        artifact_path = root / ref["artifact_path"]
        fail(artifact_path.is_file(), f"evidence artifact is missing: {ref['artifact_path']}")
        _data, document, digest = cached_evidence(artifact_path)
        fail(digest == ref["sha256"], f"evidence artifact digest mismatch: {ref['artifact_path']}")
        target = json_pointer_value(document, ref["json_pointer"])
        ref_targets.append((ref, target))

    if contract:
        if contract["transport"]["authority"] != "synthetic_fixture":
            for ref, target in ref_targets:
                if ref["evidence_kind"] != "operation_manifest":
                    continue
                facts = target.get("transport", target) if isinstance(target, dict) else {}
                fail(facts.get("method_evidence") != "registry_default_get", "registry_default_get is an identity inference, not request-method authority")
        _validate_complete_contract_evidence(record, contract, ref_targets, root)


def validate_quota_consistency(records: list[dict[str, Any]]) -> None:
    limits_by_scope: dict[tuple[str, str], tuple[str, tuple[int, int, int, int]]] = {}
    key_by_digest: dict[str, tuple[str, str]] = {}
    for record in records:
        for quota in record.get("runtime_binding", {}).get("quota_policies", []):
            key = (quota["scope_kind"], quota["scope_key"])
            limits = (
                quota["max_concurrent"],
                quota["requests_per_window"],
                quota["window_seconds"],
                quota["minimum_interval_seconds"],
            )
            previous = limits_by_scope.get(key)
            if previous is not None:
                fail(previous == (quota["scope_sha256"], limits), "shared quota scope has conflicting digest or limits across operations")
            limits_by_scope[key] = (quota["scope_sha256"], limits)
            digest_owner = key_by_digest.setdefault(quota["scope_sha256"], key)
            fail(digest_owner == key, "quota scope digest collision across different scope keys")


def verify_source_revision(index: dict[str, Any], root: Path) -> None:
    revision = index["registry_revision"]
    fail(re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "invalid pinned Registry source revision")
    exists = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{revision}^{{commit}}"],
        capture_output=True,
    )
    fail(exists.returncode == 0, "pinned Registry source revision is not available as a commit")
    ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", revision, "HEAD"],
        capture_output=True,
    )
    fail(ancestor.returncode == 0, "pinned Registry source revision is not an ancestor of the release tree")

    generation_inputs = index["generation_inputs"]
    pinned_paths = {
        generation_inputs["generator_path"],
        "schemas/datapan.operation-observation-plan.v1.schema.json",
    }
    for field in ("operation_manifest", "legacy_policy", "provider_index"):
        pinned_paths.add(generation_inputs[field]["path"])
    for ref in generation_inputs["operation_denominators"]:
        pinned_paths.add(ref["path"])
    pinned_paths.update(ref["path"] for ref in generation_inputs["document_evidence"])
    if generation_inputs["document_evidence"]:
        pinned_paths.add("schemas/datapan.operation-document-evidence.v1.schema.json")
        pinned_paths.add("scripts/operation_document_evidence.py")
    for scope in index["source_scopes"]:
        pinned_paths.update(ref["path"] for ref in scope["source_artifacts"])
    for relative in sorted(pinned_paths):
        relative_path = Path(relative)
        fail(not relative_path.is_absolute() and ".." not in relative_path.parts, f"unsafe pinned source path: {relative}")
        current_path = root / relative
        fail(current_path.is_file(), f"pinned source input is missing: {relative}")
        verify_committed_input(root, revision, relative)


def verify_committed_input(root: Path, revision: str, relative: str) -> None:
    """Require an input byte-for-byte identical to the pinned source commit."""
    current_path = root / relative
    committed_content = subprocess.run(
        ["git", "-C", str(root), "show", f"{revision}:{relative}"],
        capture_output=True,
    )
    fail(committed_content.returncode == 0, f"pinned source commit does not contain {relative}")
    if committed_content.stdout.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        match = re.search(rb"(?m)^oid sha256:([0-9a-f]{64})$\n^size ([0-9]+)$", committed_content.stdout)
        fail(match is not None, f"invalid committed Git LFS pointer: {relative}")
        byte_count, checksum = file_digest(current_path)
        fail(checksum == match.group(1).decode("ascii") and byte_count == int(match.group(2)), f"release tree differs from pinned Git LFS source: {relative}")
        return
    committed_oid = subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{revision}:{relative}"],
        capture_output=True,
        text=True,
    )
    working_oid = subprocess.run(
        ["git", "-C", str(root), "hash-object", "--", str(current_path)],
        capture_output=True,
        text=True,
    )
    fail(committed_oid.returncode == 0 and working_oid.returncode == 0, f"cannot hash pinned source input: {relative}")
    fail(committed_oid.stdout.strip() == working_oid.stdout.strip(), f"release tree differs from pinned source commit input: {relative}")


def validate_artifacts(index_path: Path = INDEX_PATH, root: Path = ROOT) -> dict[str, Any]:
    schema_path = root / "schemas/datapan.operation-observation-plan.v1.schema.json"
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    index = load_json(index_path)
    validator.validate(index)
    verify_source_revision(index, root)
    generator_ref = index["generation_inputs"]["generator_path"]
    generator_path = root / generator_ref
    fail(sha256(generator_path.read_bytes()) == index["generation_inputs"]["generator_sha256"], "generator digest mismatch")

    all_records: list[dict[str, Any]] = []
    ids_by_scope: dict[str, list[str]] = {}
    expected_shards: dict[str, tuple[str, int, int]] = {}
    for shard_binding in index["shards"]:
        relative = shard_binding["path"]
        shard_path = root / relative
        data = shard_path.read_bytes()
        fail(len(data) == shard_binding["bytes"] and sha256(data) == shard_binding["sha256"], f"shard digest mismatch: {relative}")
        shard = load_json(shard_path)
        validator.validate(shard)
        fail(shard["source_id"] == shard_binding["source_id"], "shard source identity mismatch")
        fail(shard["shard_index"] == shard_binding["shard_index"], "shard index mismatch")
        records = shard["records"]
        ids = [record["operation_identity"]["operation_id"] for record in records]
        fail(len(records) == shard_binding["record_count"], "shard record count mismatch")
        fail(ids[0] == shard_binding["first_operation_id"] and ids[-1] == shard_binding["last_operation_id"], "shard ID range mismatch")
        fail(ids == sorted(ids), "shard operation records are not canonically ordered")
        fail(len(ids) == len(set(ids)), "duplicate operation identity inside shard")
        for record in records:
            fail(record["source_binding"]["source_id"] == shard["source_id"], "operation record scope differs from containing shard")
            validate_record(record, schema, root, validator, schema_validated=True)
            all_records.append(record)
            ids_by_scope.setdefault(shard["source_id"], []).append(record["operation_identity"]["operation_id"])
        key = (shard["source_id"], shard["shard_index"])
        fail(key not in expected_shards, "duplicate source/shard index")
        expected_shards[key] = (relative, len(data), len(records))

    scopes = {scope["source_id"]: scope for scope in index["source_scopes"]}
    fail(len(scopes) == len(index["source_scopes"]), "duplicate source scope")
    fail(set(scopes) == set(ids_by_scope), "source scopes and shard scopes do not match")
    total_ids: set[tuple[str, str]] = set()
    for source_id, scope in scopes.items():
        ids = ids_by_scope[source_id]
        fail(len(ids) == scope["registered_operations"], f"registered operation count mismatch for {source_id}")
        fail(len(ids) == len(set(ids)), f"duplicate operation identity across shards for {source_id}")
        fail(identity_set_digest(ids) == scope["identity_set_sha256"], f"operation identity-set digest mismatch for {source_id}")
        for operation_id in ids:
            pair = (source_id, operation_id)
            fail(pair not in total_ids, "duplicate source-scoped operation identity across index")
            total_ids.add(pair)
        expected_binding = {
            field: scope[field]
            for field in ("source_id", "provider", "adapter_id", "inventory_status", "inventory_unknown", "test_only", "source_artifacts")
        }
        for record in all_records:
            if record["source_binding"]["source_id"] == source_id:
                fail(record["source_binding"] == expected_binding, f"operation source binding differs from indexed source scope: {source_id}")
    validate_quota_consistency(all_records)

    summary = index["summary"]
    counts = {
        "known_operations": len(all_records),
        "request_plans_complete": sum(record["request_plan"]["status"] == "complete" for record in all_records),
        "request_plans_incomplete": sum(record["request_plan"]["status"] == "incomplete" for record in all_records),
        "runtime_bindings_bound": sum(record["runtime_binding"]["status"] == "bound" for record in all_records),
        "runtime_bindings_unbound": sum(record["runtime_binding"]["status"] == "unbound" for record in all_records),
        "admitted": sum(record["admission"]["status"] == "admitted" for record in all_records),
        "not_admitted": sum(record["admission"]["status"] == "not_admitted" for record in all_records),
        "inventory_unknown_scopes": sum(scope["inventory_unknown"] for scope in scopes.values()),
    }
    fail(summary == counts, "index summary does not match derived operation-plan counts")

    expected_index, expected_files = build(root, index["registry_revision"])
    fail(index == expected_index, "operation observation plan index does not match pinned source inventories")
    output_root = root / "reports/operation-observation-plan"
    for relative, expected_bytes in expected_files.items():
        path = root / relative
        fail(path.is_file() and path.read_bytes() == expected_bytes, f"generated observation-plan artifact drift: {relative}")
    actual_shards = {path.relative_to(root).as_posix() for path in (output_root / "shards").glob("*.json")}
    expected_shards_paths = {path for path in expected_files if path.endswith(".json") and "/shards/" in path}
    fail(actual_shards == expected_shards_paths, "stale or unindexed observation-plan shard exists")
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate checked-in deterministic index and shards")
    parser.add_argument("--source-revision", help="commit that contains the compiler, schema, and evidence inputs")
    args = parser.parse_args()
    try:
        if args.check:
            existing_index = load_json(INDEX_PATH)
            revision = existing_index.get("registry_revision")
            fail(isinstance(revision, str), "checked-in index has no source revision")
        else:
            revision = args.source_revision or current_revision()
        index, outputs = build(revision=revision)
        if args.check:
            validate_artifacts()
        else:
            for relative, data in outputs.items():
                destination = ROOT / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        summary = index["summary"]
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL operation observation plan: {exc}", file=sys.stderr)
        return 1
    print(
        "ok operation observation plan "
        f"(known={summary['known_operations']}, complete={summary['request_plans_complete']}, "
        f"incomplete={summary['request_plans_incomplete']}, admitted={summary['admitted']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
