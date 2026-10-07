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
from urllib.parse import urlsplit

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
DENOMINATOR_PATHS = {
    "ecos": ROOT / "reports/ecos/operation-denominator.json",
    "kosis": ROOT / "reports/kosis/operation-denominator.json",
    "open_assembly": ROOT / "reports/open-assembly/operation-denominator.json",
    "seoul_open_data": ROOT / "reports/seoul-open-data/operation-denominator.json",
}
SHARD_SIZE = 256
QUOTA_SCOPE_PREFIX = b"datapan.quota-scope.v1\0"
_EVIDENCE_CACHE: dict[Path, tuple[bytes, Any, str]] = {}


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
        if path.name in {"queue.v1.json", "reconciliation.v1.json"}:
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


def registered_endpoint(endpoint: str | None) -> dict[str, str] | None:
    if not endpoint:
        return None
    parsed = urlsplit(endpoint if "://" in endpoint else "https://" + endpoint.lstrip("/"))
    if not parsed.hostname or not parsed.path.startswith("/"):
        return None
    return {"host": parsed.hostname.lower(), "path": parsed.path}


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
    data_go_scope = {
        "source_id": "data_go_kr",
        "provider": "data.go.kr",
        "adapter_id": data_go_adapter_id,
        "inventory_status": "source_complete",
        "inventory_unknown": False,
        "test_only": False,
        "source_artifacts": [
            source_snapshot_ref,
            manifest_ref,
            artifact_ref(source_profile_path, root),
        ],
    }

    scope_records: dict[str, dict[str, Any]] = {}
    scope_specs: dict[str, dict[str, Any]] = {"data_go_kr": data_go_scope}
    rows_by_source: dict[str, list[dict[str, Any]]] = {"data_go_kr": []}
    operations = manifest["operations"]
    document_evidence = load_document_evidence(root, operations)
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
        captured = document_evidence.get(operation_id)
        if captured:
            document = captured["document"]
            plan["request_plan"]["evidence_refs"].extend(
                evidence_ref(captured["artifact_ref"], pointer, "operation_document")
                for pointer in document_evidence_pointers(document)
            )
            _resolve_documented_missing_fields(plan, document)
        rows_by_source["data_go_kr"].append(plan)
    fail(not legacy_map, "one or more legacy canaries do not map to the pinned operation manifest")

    denominator_paths: list[Path] = []
    for source_id, path in DENOMINATOR_PATHS.items():
        denominator = load_json(root / path.relative_to(ROOT))
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
            ],
        }
        scope_specs[source_id] = source_scope
        rows_by_source[source_id] = []
        for index, operation in enumerate(operation_rows):
            identity = {}
            endpoint = registered_endpoint(operation.get("endpoint_template"))
            if endpoint:
                identity["registered_endpoint"] = endpoint
            record = make_incomplete_plan(
                scope=source_scope,
                operation_id=operation["operation_id"],
                protocol="HTTP",
                identity=identity,
                evidence=evidence_ref(denominator_ref, f"#/operations/{index}", "operation_denominator"),
            )
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
    provider_index_ref = artifact_ref(root / PROVIDER_INDEX_PATH.relative_to(ROOT), root)
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
            "document_evidence": [
                captured["artifact_ref"]
                for _operation_id, captured in sorted(document_evidence.items())
            ],
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
    return index, {**shards_by_path, "reports/operation-observation-plan/index.json": pretty_json(index)}


def _validate_complete_contract_evidence(
    record: dict[str, Any],
    contract: dict[str, Any],
    ref_targets: list[tuple[dict[str, Any], Any]],
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

    for ref in refs:
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
        _validate_complete_contract_evidence(record, contract, ref_targets)


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

    pinned_paths = {
        index["generation_inputs"]["generator_path"],
        "schemas/datapan.operation-observation-plan.v1.schema.json",
    }
    if index["generation_inputs"]["document_evidence"]:
        pinned_paths.add("schemas/datapan.operation-document-evidence.v1.schema.json")
        pinned_paths.add("scripts/operation_document_evidence.py")
    pinned_paths.update(ref["path"] for ref in index["generation_inputs"]["document_evidence"])
    for scope in index["source_scopes"]:
        pinned_paths.update(ref["path"] for ref in scope["source_artifacts"])
    for relative in sorted(pinned_paths):
        relative_path = Path(relative)
        fail(not relative_path.is_absolute() and ".." not in relative_path.parts, f"unsafe pinned source path: {relative}")
        current_path = root / relative
        fail(current_path.is_file(), f"pinned source input is missing: {relative}")
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
        else:
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
