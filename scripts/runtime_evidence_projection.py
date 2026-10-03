#!/usr/bin/env python3
"""Bind historical runtime rows to the current complete operation contract.

The projection is deliberately fail-closed. A dataset/name/host match is useful
for accounting legacy rows, but it is never sufficient to make one current
evidence. Producers may attach ``contract_binding`` after a verified run; the
binding is reusable only when its stable upstream identity and complete
contract digest still match the current registry operation.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit


SCHEMA_VERSION = "datapan.current-runtime-evidence-projection.v1"
BINDING_SCHEMA_VERSION = "datapan.runtime-evidence-contract-binding.v1"
PLAN_BINDING_SCHEMA_VERSION = "datapan.runtime-freshness-plan-contract-binding.v1"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DISPOSITIONS = (
    "eligible",
    "stale",
    "expired",
    "unknown_timestamp",
    "recent_non_verified",
    "contract_changed",
    "ambiguous",
    "unbound",
    "historical",
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_sha256(path: pathlib.Path) -> str:
    """Return the payload hash for a materialized file or Git LFS pointer."""
    data = path.read_bytes()
    if data.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        for line in data.decode("ascii", errors="strict").splitlines():
            if line.startswith("oid sha256:"):
                value = line.partition(":")[2]
                if HASH_RE.fullmatch(value):
                    return value
        raise ValueError(f"{path} is a malformed Git LFS pointer")
    return hashlib.sha256(data).hexdigest()


def immutable_import_binding_index(root: pathlib.Path) -> tuple[dict[tuple[str, str], tuple[str, str]], dict[str, int]]:
    """Return row-binding digests proven by immutable, post-merge receipts.

    An admission alone is insufficient: the matching main attestation must
    bind that admission and import receipt. Legacy receipts remain readable;
    rows without the optional binding digest simply contribute no proof.
    """
    admissions_dir = root / "reports/runtime-freshness-import-admissions"
    imports_dir = root / "reports/runtime-freshness-imports"
    attestations_dir = root / "reports/runtime-freshness-import-attestations"
    bindings: dict[tuple[str, str], tuple[str, str]] = {}
    conflicted: set[tuple[str, str]] = set()
    counts = {"admitted_runs": 0, "attested_runs": 0, "pending_attestation_runs": 0, "invalid_runs": 0, "proven_row_bindings": 0}
    if not admissions_dir.is_dir():
        return bindings, counts
    for admission_path in sorted(admissions_dir.glob("*.json")):
        try:
            admission_bytes = admission_path.read_bytes()
            admission = json.loads(admission_bytes)
            run_id = admission.get("run_id")
            inputs = admission.get("inputs")
            producer = admission.get("producer")
            if not isinstance(run_id, str) or admission_path.stem != run_id or not isinstance(inputs, dict) or not isinstance(producer, dict):
                raise ValueError("admission identity is incomplete")
            import_ref = inputs.get("import_receipt")
            if not isinstance(import_ref, dict) or not isinstance(import_ref.get("path"), str):
                raise ValueError("admission import receipt reference is missing")
            relative = pathlib.PurePosixPath(import_ref["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("admission import receipt path is unsafe")
            receipt_path = root.joinpath(*relative.parts)
            receipt_bytes = receipt_path.read_bytes()
            if hashlib.sha256(receipt_bytes).hexdigest() != import_ref.get("sha256"):
                raise ValueError("admission import receipt digest mismatch")
            receipt = json.loads(receipt_bytes)
            receipt_inputs = receipt.get("inputs")
            if (
                receipt.get("run_id") != run_id
                or not isinstance(receipt_inputs, dict)
                or receipt_inputs.get("sanitized_report_sha256") != inputs.get("sanitized_report_sha256")
                or receipt_inputs.get("run_receipt_sha256") != inputs.get("run_receipt_sha256")
            ):
                raise ValueError("import receipt does not match admission lineage")
            counts["admitted_runs"] += 1
            attestation_path = attestations_dir / f"{run_id}.json"
            if not attestation_path.is_file():
                counts["pending_attestation_runs"] += 1
                continue
            attestation_bytes = attestation_path.read_bytes()
            attestation = json.loads(attestation_bytes)
            lineage = attestation.get("lineage")
            attested_admission = lineage.get("admission") if isinstance(lineage, dict) else None
            attested_receipt = lineage.get("import_receipt") if isinstance(lineage, dict) else None
            if (
                attestation.get("run_id") != run_id
                or not isinstance(attested_admission, dict)
                or attested_admission.get("sha256") != hashlib.sha256(admission_bytes).hexdigest()
                or not isinstance(attested_receipt, dict)
                or attested_receipt.get("sha256") != import_ref.get("sha256")
                or attestation.get("lineage", {}).get("sanitized_report_sha256") != inputs.get("sanitized_report_sha256")
                or attestation.get("lineage", {}).get("run_receipt_sha256") != inputs.get("run_receipt_sha256")
            ):
                raise ValueError("post-merge attestation does not match admission lineage")
            counts["attested_runs"] += 1
            result_rows = receipt.get("results")
            if not isinstance(result_rows, list):
                raise ValueError("import receipt results are missing")
            for result in result_rows:
                if not isinstance(result, dict):
                    continue
                identity_key = result.get("identity_key")
                binding_sha = result.get("contract_binding_sha256")
                result_sha = result.get("result_sha256")
                if (
                    not isinstance(identity_key, str)
                    or not isinstance(binding_sha, str)
                    or not HASH_RE.fullmatch(binding_sha)
                    or not isinstance(result_sha, str)
                    or not HASH_RE.fullmatch(result_sha)
                ):
                    continue
                key = (run_id, identity_key)
                proof = (binding_sha, result_sha)
                previous = bindings.get(key)
                if previous is not None and previous != proof:
                    bindings.pop(key, None)
                    conflicted.add(key)
                    continue
                if key not in conflicted:
                    bindings[key] = proof
            counts["proven_row_bindings"] = len(bindings)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            counts["invalid_runs"] += 1
    return bindings, counts


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _raw(operation: dict[str, Any]) -> dict[str, Any]:
    source = operation.get("source")
    raw = source.get("raw") if isinstance(source, dict) else None
    return raw if isinstance(raw, dict) else {}


def _dataset_raw(dataset: dict[str, Any]) -> dict[str, Any]:
    source = dataset.get("source")
    raw = source.get("raw") if isinstance(source, dict) else None
    return raw if isinstance(raw, dict) else {}


def upstream_operation_key(operation: dict[str, Any]) -> str | None:
    source = operation.get("source")
    system = source.get("system") if isinstance(source, dict) else None
    raw = _raw(operation)
    if system == "safetydata.go.kr":
        value = raw.get("source_interface_id") or raw.get("data_sn")
    else:
        value = raw.get("operation_seq")
    return str(value) if isinstance(value, (str, int)) and str(value) else None


def _endpoint(dataset: dict[str, Any], operation: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any] | None:
    dataset_raw = _dataset_raw(dataset)
    base = raw.get("end_point_url") or dataset_raw.get("end_point_url")
    action_url = raw.get("operation_url") or ""
    if not isinstance(base, str) or not base.strip():
        base = dataset.get("endpoint") if isinstance(dataset.get("endpoint"), str) else None
    if not isinstance(base, str) or not base.strip():
        return None
    candidate = urljoin(base.strip(), action_url.strip()) if isinstance(action_url, str) else base.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
    except (UnicodeError, ValueError):
        return None
    return {
        "scheme": parsed.scheme.lower(),
        "host": host,
        "path": parsed.path or "/",
        # Query text is not copied into the projection. Its digest still makes
        # a query-contract change invalidate old evidence.
        "query_sha256": hashlib.sha256(parsed.query.encode("utf-8")).hexdigest(),
    }


def _parameter_contract(operation: dict[str, Any], raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    request = operation.get("request_params")
    response = operation.get("response_params")
    request_names = raw.get("request_param_nm_en")
    response_names = raw.get("response_param_nm_en")
    has_request = isinstance(request, list) or isinstance(request_names, str) or isinstance(raw.get("request_param_nm"), str)
    has_response = isinstance(response, list) or isinstance(response_names, str) or isinstance(raw.get("response_param_nm"), str)

    def canonical_params(value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return sorted(value, key=lambda item: (str(item.get("name", "")) if isinstance(item, dict) else "", canonical_json(item)))

    contract = {
        "request": canonical_params(request),
        "request_names_en": request_names,
        "request_names": raw.get("request_param_nm"),
        "response": canonical_params(response),
        "response_names_en": response_names,
        "response_names": raw.get("response_param_nm"),
        # Registry-level defaults are separate from parameter descriptions and
        # change the actual request made by a verifier.
        "default_params": operation.get("default_params"),
    }
    return contract, has_request and has_response


def _operation_manifest_transport(entry: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
    """Normalize the exact endpoint and method/action contract from the manifest."""
    transport = entry.get("transport")
    if not isinstance(transport, dict):
        return None, None, False
    endpoint_value = transport.get("endpoint")
    endpoint = None
    if isinstance(endpoint_value, str) and endpoint_value.strip():
        parsed = urlsplit(endpoint_value.strip())
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            try:
                host = parsed.hostname.encode("idna").decode("ascii").lower()
                if parsed.port is not None:
                    host = f"{host}:{parsed.port}"
                endpoint = {
                    "scheme": parsed.scheme.lower(),
                    "host": host,
                    "path": parsed.path or "/",
                    "query_sha256": hashlib.sha256(parsed.query.encode("utf-8")).hexdigest(),
                }
            except (UnicodeError, ValueError):
                endpoint = None
    method = transport.get("method")
    protocol = entry.get("protocol")
    method_action = {
        "protocol": protocol,
        "method": method.upper() if isinstance(method, str) and method else None,
        "action": transport.get("action"),
        "method_evidence": transport.get("method_evidence"),
    }
    method_or_action_complete = (
        isinstance(method, str) and bool(method)
        if protocol == "REST"
        else isinstance(transport.get("action"), str) and bool(transport.get("action"))
    )
    return endpoint, method_action, endpoint is not None and isinstance(protocol, str) and bool(protocol) and method_or_action_complete


def operation_contract(
    dataset: dict[str, Any],
    operation: dict[str, Any],
    index: int,
    operation_manifest_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dataset_id = str(dataset.get("id") or "")
    name = operation.get("name")
    name = name if isinstance(name, str) else ""
    raw = _raw(operation)
    dataset_raw = _dataset_raw(dataset)
    api_id = raw.get("id") or dataset_raw.get("id")
    api_id = str(api_id) if isinstance(api_id, (str, int)) and str(api_id) else None
    operation_key = upstream_operation_key(operation)
    source = operation.get("source")
    source_system = str(source.get("system") or "data.go.kr") if isinstance(source, dict) else "data.go.kr"
    source_id = "data_go_kr"
    if operation_manifest_entry is not None:
        # The canonical data.go.kr API id is the catalogue dataset/list id;
        # raw UDDI IDs, when present, remain an additional source identity.
        api_id = api_id or dataset_id
        endpoint = None
    else:
        endpoint = _endpoint(dataset, operation, raw)
    method = operation.get("method") or operation.get("http_method") or raw.get("method") or raw.get("http_method")
    action = operation.get("action") or operation.get("operation_action") or raw.get("action") or raw.get("operation_action") or raw.get("operation_url")
    api_type = raw.get("api_type") or dataset_raw.get("api_type")
    operation_manifest_id = None
    operation_manifest_identity_matches = True
    manifest_contract_complete = True
    if operation_manifest_entry is not None:
        provenance = operation_manifest_entry.get("provenance")
        transport = operation_manifest_entry.get("transport")
        requirements = operation_manifest_entry.get("requirements")
        if isinstance(provenance, dict) and isinstance(transport, dict) and isinstance(requirements, dict):
            endpoint, method_action, transport_complete = _operation_manifest_transport(operation_manifest_entry)
            method_action = {
                **(method_action or {}),
                "registry_method": str(method).upper() if isinstance(method, str) and method else None,
                "registry_action": action if isinstance(action, str) and action else None,
                "registry_api_type": api_type,
            }
            registry_parameters, registry_parameters_complete = _parameter_contract(operation, raw)
            parameter_contract = {
                # The manifest records the caller-facing parameter requirement
                # projection. Preserve the exact registry parameter objects and
                # raw names too: defaults, schema, and labels are contract data.
                "operation_manifest_requirements": requirements,
                "registry_parameters": registry_parameters,
            }
            params_complete = (
                isinstance(requirements.get("all_request_parameters"), list)
                and registry_parameters_complete
            )
            manifest_contract_complete = transport_complete
            operation_manifest_id = operation_manifest_entry.get("operation_id")
            operation_manifest_identity_matches = (
                str(provenance.get("dataset_id") or "") == dataset_id
                and str(provenance.get("upstream_operation_key") or "") == (operation_key or "")
                and str(provenance.get("operation_name") or "") == name
                and provenance.get("provider") == "data.go.kr"
                and provenance.get("source_system") == source_system
            )
            api_type = operation_manifest_entry.get("protocol") or api_type
        else:
            method_action = {
                "api_type": api_type,
                "method": str(method).upper() if isinstance(method, str) and method else None,
                "method_source_state": "provided" if isinstance(method, str) and method else "unspecified_by_registry",
                "action": action if isinstance(action, str) and action else None,
                "operation_key": operation_key,
            }
            parameter_contract, params_complete = _parameter_contract(operation, raw)
    else:
        method_action = {
            "api_type": api_type,
            "method": str(method).upper() if isinstance(method, str) and method else None,
            "method_source_state": "provided" if isinstance(method, str) and method else "unspecified_by_registry",
            "action": action if isinstance(action, str) and action else None,
            "operation_key": operation_key,
        }
        parameter_contract, params_complete = _parameter_contract(operation, raw)
    source_identity = {
        "source_id": source_id,
        "source_system": source_system,
        "dataset_id": dataset_id,
        "upstream_api_id": api_id,
        "upstream_list_id": str(raw.get("list_id") or dataset_raw.get("list_id") or dataset_id),
        "upstream_operation_key": operation_key,
        "operation_manifest_id": str(operation_manifest_id) if isinstance(operation_manifest_id, str) else None,
    }
    completeness_reasons = []
    if not dataset_id:
        completeness_reasons.append("missing_dataset_id")
    if not api_id:
        completeness_reasons.append("missing_upstream_api_id")
    if not operation_key:
        completeness_reasons.append("missing_upstream_operation_key")
    if endpoint is None:
        completeness_reasons.append("missing_exact_endpoint")
    if not isinstance(api_type, str) or not api_type:
        completeness_reasons.append("missing_method_action_contract")
    if operation_manifest_entry is not None and not manifest_contract_complete:
        completeness_reasons.append("incomplete_operation_manifest_transport")
    if operation_manifest_entry is not None and not isinstance(operation_manifest_id, str):
        completeness_reasons.append("missing_operation_manifest_identity")
    if operation_manifest_entry is not None and not operation_manifest_identity_matches:
        completeness_reasons.append("operation_manifest_identity_mismatch")
    if not params_complete:
        completeness_reasons.append("missing_parameter_contract")
    source_identity_sha256 = digest(source_identity)
    parameter_contract_sha256 = digest(parameter_contract)
    contract = {
        "schema_version": "datapan.operation-evidence-contract.v1",
        "source_identity": source_identity,
        "endpoint": endpoint,
        "method_action": method_action,
        "parameter_contract_sha256": parameter_contract_sha256,
        # Include the operation label as descriptive contract content. It is
        # never used as an identity key.
        "operation_name": name,
        "operation_manifest_id": operation_manifest_id,
    }
    contract_sha256 = digest(contract)
    identity_key = f"{source_id}:{dataset_id}:{operation_key or name or index}"
    stable_identity = (source_id, dataset_id, api_id or "", operation_key or "")
    return {
        "source_id": source_id,
        "identity_key": identity_key,
        "dataset_id": dataset_id,
        "operation": name,
        "operation_seq": operation_key,
        "upstream_api_id": api_id,
        "operation_manifest_id": operation_manifest_id,
        "upstream_operation_key": operation_key,
        "source_identity_sha256": source_identity_sha256,
        "endpoint": endpoint,
        "method_action_sha256": digest(method_action),
        "parameter_contract_sha256": parameter_contract_sha256,
        "contract_sha256": contract_sha256,
        "contract_complete": not completeness_reasons,
        "incomplete_reasons": completeness_reasons,
        "stable_identity": stable_identity,
        "weak_identity": (dataset_id, name),
    }


def operation_manifest_index(operation_manifest: dict[str, Any]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    rows = operation_manifest.get("operations")
    if not isinstance(rows, list):
        raise ValueError("operation manifest operations must be an array")
    indexed: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            continue
        provenance = row.get("provenance")
        if not isinstance(provenance, dict):
            continue
        dataset_id = str(provenance.get("dataset_id") or "")
        operation_key = str(provenance.get("upstream_operation_key") or "")
        source_system = str(provenance.get("source_system") or "")
        if dataset_id and operation_key and source_system:
            indexed[(dataset_id, source_system, operation_key)].append(row)
    return indexed


def make_contract_binding(
    contract: dict[str, Any],
    *,
    source_snapshot_sha256: str,
    operation_manifest_sha256: str,
    identity_key: str | None = None,
) -> dict[str, Any]:
    """Build the operation binding carried in a hashed runtime batch plan."""
    if not contract.get("contract_complete"):
        raise ValueError("cannot bind runtime evidence to an incomplete operation contract")
    for label, value in (
        ("source_snapshot_sha256", source_snapshot_sha256),
        ("operation_manifest_sha256", operation_manifest_sha256),
    ):
        if not isinstance(value, str) or not HASH_RE.fullmatch(value):
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return {
        "schema_version": PLAN_BINDING_SCHEMA_VERSION,
        "identity_key": identity_key or contract["identity_key"],
        "source_id": contract["source_id"],
        "dataset_id": contract["dataset_id"],
        "upstream_api_id": contract["upstream_api_id"],
        "operation_key": contract["upstream_operation_key"],
        "source_identity_sha256": contract["source_identity_sha256"],
        "method_action_sha256": contract["method_action_sha256"],
        "parameter_contract_sha256": contract["parameter_contract_sha256"],
        "contract_sha256": contract["contract_sha256"],
        "source_snapshot_sha256": source_snapshot_sha256,
        "operation_manifest_sha256": operation_manifest_sha256,
    }


def bind_result_to_plan(plan_binding: dict[str, Any], *, run_id: str, plan_sha256: str) -> dict[str, Any]:
    """Add the producer run and immutable batch-plan digest to a result binding."""
    if plan_binding.get("schema_version") != PLAN_BINDING_SCHEMA_VERSION:
        raise ValueError("unsupported runtime freshness plan contract binding")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("runtime freshness run_id is missing")
    if not HASH_RE.fullmatch(plan_sha256):
        raise ValueError("batch plan digest must be a lowercase SHA-256 digest")
    return {
        **plan_binding,
        "schema_version": BINDING_SCHEMA_VERSION,
        "run_id": run_id,
        "plan_sha256": plan_sha256,
    }


def _binding(row: dict[str, Any]) -> dict[str, Any] | None:
    value = row.get("contract_binding")
    if not isinstance(value, dict) or value.get("schema_version") != BINDING_SCHEMA_VERSION:
        return None
    required = (
        "identity_key", "source_id", "dataset_id", "upstream_api_id", "operation_key", "run_id", "plan_sha256",
        "method_action_sha256", "parameter_contract_sha256",
        "source_identity_sha256", "contract_sha256", "source_snapshot_sha256",
        "operation_manifest_sha256",
    )
    if any(not isinstance(value.get(key), str) or not value.get(key) for key in required):
        return None
    for key in (
        "source_identity_sha256", "contract_sha256", "source_snapshot_sha256", "operation_manifest_sha256",
        "method_action_sha256", "parameter_contract_sha256", "plan_sha256",
    ):
        if not HASH_RE.fullmatch(value[key]):
            return None
    return value


def _record_hash(row: dict[str, Any]) -> str:
    return digest(row)


def _disposition(row: dict[str, Any], contract: dict[str, Any] | None, as_of: datetime, fresh_days: int, expire_days: int) -> tuple[str, str, datetime | None]:
    observed = parse_time(row.get("verified_at"))
    if observed and observed > as_of:
        raise ValueError("runtime evidence timestamp is after evaluation time")
    if contract is None:
        return "historical", "no_current_operation_identity", observed
    binding = _binding(row)
    if binding is None:
        return "unbound", "immutable_exact_contract_binding_missing", observed
    if (
        binding["source_id"] != contract["source_id"]
        or binding["dataset_id"] != contract["dataset_id"]
        or binding["upstream_api_id"] != contract["upstream_api_id"]
        or binding["operation_key"] != contract["upstream_operation_key"]
    ):
        return "contract_changed", "stable_upstream_identity_changed", observed
    if binding["source_identity_sha256"] != contract["source_identity_sha256"]:
        return "contract_changed", "source_identity_changed", observed
    if binding["contract_sha256"] != contract["contract_sha256"]:
        return "contract_changed", "operation_contract_changed", observed
    if not contract["contract_complete"]:
        return "unbound", "current_contract_incomplete", observed
    if observed is None:
        return "unknown_timestamp", "bound_result_timestamp_missing_or_invalid", None
    if observed < as_of - timedelta(days=expire_days):
        return "expired", "bound_result_outside_expiry_window", observed
    if observed < as_of - timedelta(days=fresh_days):
        return "stale", "bound_result_outside_fresh_window", observed
    if row.get("status") != "verified":
        return "recent_non_verified", "bound_result_not_verified", observed
    return "eligible", "exact_contract_bound_fresh_success", observed


def build_projection(
    registry_rows: list[dict[str, Any]],
    latest: dict[str, Any],
    operation_manifest: dict[str, Any] | None = None,
    *,
    registry_sha256: str,
    operation_manifest_sha256: str,
    registry_path: str = "data/data-go-kr.registry.json",
    operation_manifest_path: str = "reports/data-go-kr/operation-manifest.json",
    latest_path: str = "reports/latest-verification.json",
    latest_sha256: str | None = None,
    provenance_bindings: dict[tuple[str, str], tuple[str, str]] | None = None,
    provenance_summary: dict[str, int] | None = None,
    fresh_days: int = 30,
    expire_days: int = 90,
) -> dict[str, Any]:
    generated_at = latest.get("generated_at")
    as_of = parse_time(generated_at)
    if as_of is None:
        raise ValueError("latest verification generated_at must be a timezone-aware timestamp")
    if fresh_days <= 0 or expire_days <= fresh_days:
        raise ValueError("expire_days must be greater than positive fresh_days")
    if not HASH_RE.fullmatch(registry_sha256) or not HASH_RE.fullmatch(operation_manifest_sha256):
        raise ValueError("registry and operation manifest digests must be lowercase SHA-256 values")

    manifest_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    if operation_manifest is not None:
        source_snapshot = operation_manifest.get("source_snapshot")
        if not isinstance(source_snapshot, dict) or source_snapshot.get("sha256") != registry_sha256:
            raise ValueError("operation manifest is not bound to the exact current registry snapshot")
        manifest_by_key = operation_manifest_index(operation_manifest)
    operations: list[dict[str, Any]] = []
    stable_index: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    weak_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    key_index: dict[str, dict[str, Any]] = {}
    for dataset in registry_rows:
        if not isinstance(dataset, dict):
            continue
        dataset_operations = dataset.get("operations")
        if not isinstance(dataset_operations, list):
            continue
        for op_index, op in enumerate(dataset_operations):
            if not isinstance(op, dict):
                continue
            dataset_id = str(dataset.get("id") or "")
            raw = _raw(op)
            operation_key = upstream_operation_key(op)
            source = op.get("source")
            source_system = str(source.get("system") or "data.go.kr") if isinstance(source, dict) else "data.go.kr"
            manifest_matches = manifest_by_key.get((dataset_id, source_system, operation_key or ""), [])
            manifest_row = manifest_matches[0] if len(manifest_matches) == 1 else None
            contract = operation_contract(dataset, op, op_index, manifest_row if operation_manifest is not None else None)
            if operation_manifest is None:
                contract["contract_complete"] = False
                contract["incomplete_reasons"].append("missing_operation_manifest")
            else:
                if len(manifest_matches) > 1:
                    contract["contract_complete"] = False
                    contract["incomplete_reasons"].append("ambiguous_operation_manifest_identity")
                elif not manifest_matches:
                    contract["contract_complete"] = False
                    contract["incomplete_reasons"].append("missing_operation_manifest_identity")
            operations.append({key: value for key, value in contract.items() if key not in {"stable_identity", "weak_identity"}})
            stable_index[contract["stable_identity"]].append(contract)
            weak_index[contract["weak_identity"]].append(contract)
            if contract["identity_key"] in key_index:
                # A display-name fallback is intentionally not accepted as a
                # stable identity. Keep the collision visible to consumers.
                key_index.pop(contract["identity_key"], None)
            elif contract["upstream_operation_key"] is not None:
                key_index[contract["identity_key"]] = contract

    raw_results = latest.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("latest verification results must be an array")
    records: list[dict[str, Any]] = []
    provenance_bindings = provenance_bindings or {}
    provenance_summary = provenance_summary or {"admitted_runs": 0, "attested_runs": 0, "pending_attestation_runs": 0, "invalid_runs": 0, "proven_row_bindings": len(provenance_bindings)}
    matched_contracts: dict[str, list[tuple[datetime | None, int, dict[str, Any], str]]] = defaultdict(list)
    record_counts: Counter[str] = Counter({key: 0 for key in DISPOSITIONS})
    for index, raw_row in enumerate(raw_results):
        if not isinstance(raw_row, dict):
            raise ValueError(f"latest verification result {index} must be an object")
        binding = _binding(raw_row)
        contract: dict[str, Any] | None = None
        preset: tuple[str, str, datetime | None] | None = None
        if binding:
            stable_identity = (
                binding["source_id"], binding["dataset_id"], binding["upstream_api_id"], binding["operation_key"]
            )
            matches = stable_index.get(stable_identity, [])
            if len(matches) == 1:
                contract = matches[0]
            elif len(matches) > 1:
                preset = ("ambiguous", "stable_upstream_identity_matches_multiple_current_operations", parse_time(raw_row.get("verified_at")))
            else:
                dataset_id = str(raw_row.get("dataset_id") or "")
                name = str(raw_row.get("operation") or "")
                weak_matches = weak_index.get((dataset_id, name), [])
                if len(weak_matches) == 1:
                    contract = weak_matches[0]
                elif len(weak_matches) > 1:
                    preset = ("ambiguous", "dataset_and_name_match_multiple_current_operations", parse_time(raw_row.get("verified_at")))
                else:
                    preset = ("historical", "bound_upstream_operation_not_in_current_registry", parse_time(raw_row.get("verified_at")))
        else:
            dataset_id = str(raw_row.get("dataset_id") or "")
            name = str(raw_row.get("operation") or "")
            weak_matches = weak_index.get((dataset_id, name), [])
            if len(weak_matches) > 1:
                preset = ("ambiguous", "dataset_and_name_match_multiple_current_operations", parse_time(raw_row.get("verified_at")))
            elif len(weak_matches) == 1:
                contract = weak_matches[0]
            else:
                preset = ("historical", "no_current_operation_matches_legacy_identity", parse_time(raw_row.get("verified_at")))

        if preset is not None:
            disposition, reason, observed = preset
        elif contract is not None:
            disposition, reason, observed = _disposition(raw_row, contract, as_of, fresh_days, expire_days)
        else:
            disposition, reason, observed = "historical", "no_current_operation_identity", parse_time(raw_row.get("verified_at"))
        if observed and observed > as_of:
            raise ValueError(f"latest verification result {index} is after evaluation time")
        if disposition in {"eligible", "stale", "expired", "unknown_timestamp", "recent_non_verified"} and binding:
            expected_binding_sha = digest(binding)
            trusted_binding_sha = provenance_bindings.get((binding["run_id"], binding["identity_key"]))
            expected_result_sha = digest(raw_row)
            if trusted_binding_sha != (expected_binding_sha, expected_result_sha):
                disposition, reason = "unbound", "immutable_import_receipt_binding_missing_or_mismatched"
        record = {
            "evidence_record_sha256": _record_hash(raw_row),
            "disposition": disposition,
            "reason": reason,
            "source_id": binding.get("source_id") if binding else raw_row.get("provider"),
            "dataset_id": str(raw_row.get("dataset_id")) if raw_row.get("dataset_id") is not None else None,
            "operation": str(raw_row.get("operation")) if raw_row.get("operation") is not None else None,
            "identity_key": contract["identity_key"] if contract is not None else None,
            "status": str(raw_row.get("status")) if raw_row.get("status") is not None else None,
            "verified_at": raw_row.get("verified_at") if isinstance(raw_row.get("verified_at"), str) else None,
            "run_id": binding.get("run_id") if binding else None,
            "plan_sha256": binding.get("plan_sha256") if binding else None,
            "contract_binding_sha256": digest(binding) if binding else None,
            "source_snapshot_sha256": binding.get("source_snapshot_sha256") if binding else None,
            "operation_manifest_sha256": binding.get("operation_manifest_sha256") if binding else None,
            "contract_sha256": binding.get("contract_sha256") if binding else None,
        }
        records.append(record)
        record_counts[disposition] += 1
        if contract is not None and binding and binding.get("contract_sha256") == contract["contract_sha256"] and binding.get("source_identity_sha256") == contract["source_identity_sha256"] and contract["contract_complete"] and disposition in {"eligible", "stale", "expired", "unknown_timestamp", "recent_non_verified"}:
            matched_contracts[contract["identity_key"]].append((observed, index, raw_row, disposition))

    current_evidence: list[dict[str, Any]] = []
    operation_by_key = {op["identity_key"]: op for op in operations}
    for identity_key, rows in sorted(matched_contracts.items()):
        timestamp, index, row, disposition = max(
            rows,
            key=lambda item: (item[0] is not None, item[0] or datetime.min.replace(tzinfo=timezone.utc), item[1]),
        )
        current_evidence.append({
            "identity_key": identity_key,
            "dataset_id": operation_by_key[identity_key]["dataset_id"],
            "operation": operation_by_key[identity_key]["operation"],
            "contract_sha256": operation_by_key[identity_key]["contract_sha256"],
            "evidence_record_sha256": _record_hash(row),
            "contract_binding_sha256": digest(_binding(row)),
            "run_id": _binding(row)["run_id"],
            "status": str(row.get("status") or "unknown"),
            "verified_at": row.get("verified_at") if isinstance(row.get("verified_at"), str) else None,
            "disposition": disposition,
        })

    disposition_counts = {key: int(record_counts[key]) for key in DISPOSITIONS}
    current_counts: Counter[str] = Counter(item["disposition"] for item in current_evidence)
    contract_set_sha256 = digest(sorted((item["identity_key"], item["contract_sha256"], item["contract_complete"]) for item in operations))
    latest_bytes_digest = latest_sha256 if isinstance(latest_sha256, str) and HASH_RE.fullmatch(latest_sha256) else digest(latest)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "inputs": {
            "registry": registry_path,
            "registry_source_sha256": registry_sha256,
            "operation_manifest": operation_manifest_path,
            "operation_manifest_sha256": operation_manifest_sha256,
            "latest_verification": latest_path,
            "latest_verification_sha256": latest_bytes_digest,
        },
        "freshness": {"as_of": generated_at, "fresh_days": fresh_days, "expire_days": expire_days},
        "current_contracts": {
            "operation_count": len(operations),
            "complete_operation_count": sum(bool(item["contract_complete"]) for item in operations),
            "contract_set_sha256": contract_set_sha256,
        },
        "provenance": {key: int(provenance_summary.get(key, 0)) for key in ("admitted_runs", "attested_runs", "pending_attestation_runs", "invalid_runs", "proven_row_bindings")},
        "summary": {
            "historical_evidence_records": len(records),
            "current_operations_with_bound_evidence": len(current_evidence),
            "current_operations_with_fresh_verified_evidence": current_counts["eligible"],
            "current_operations_within_expiry_window": sum(current_counts[key] for key in ("eligible", "stale", "recent_non_verified")),
            "eligible": disposition_counts["eligible"],
            "stale": disposition_counts["stale"],
            "expired": disposition_counts["expired"],
            "unknown_timestamp": disposition_counts["unknown_timestamp"],
            "recent_non_verified": disposition_counts["recent_non_verified"],
            "contract_changed": disposition_counts["contract_changed"],
            "ambiguous": disposition_counts["ambiguous"],
            "unbound": disposition_counts["unbound"],
            "historical": disposition_counts["historical"],
            "bound_current_eligible_operations": current_counts["eligible"],
            "bound_current_stale_operations": current_counts["stale"],
            "bound_current_expired_operations": current_counts["expired"],
            "bound_current_non_verified_operations": current_counts["recent_non_verified"],
            "bound_current_unknown_timestamp_operations": current_counts["unknown_timestamp"],
        },
        "operations": operations,
        "current_evidence": current_evidence,
        "records": records,
    }


def current_evidence_index(projection: dict[str, Any], *, include_expired: bool = False) -> dict[str, dict[str, Any]]:
    accepted = {"eligible", "stale", "recent_non_verified"}
    if include_expired:
        accepted.add("expired")
    return {row["identity_key"]: row for row in projection.get("current_evidence", []) if row.get("disposition") in accepted}


def current_evidence_by_unique_operation_name(
    projection: dict[str, Any], *, include_expired: bool = False
) -> dict[tuple[str, str], dict[str, Any]]:
    """Map dependency rows only when dataset plus operation name is unique.

    Host and display label never prove evidence identity. This projection is
    only a bridge for dependency artifacts that do not yet store operation_seq.
    """
    operations_by_name: dict[tuple[str, str], list[str]] = defaultdict(list)
    for operation in projection.get("operations", []):
        if not isinstance(operation, dict):
            continue
        dataset_id = operation.get("dataset_id")
        name = operation.get("operation")
        identity_key = operation.get("identity_key")
        if isinstance(dataset_id, str) and isinstance(name, str) and isinstance(identity_key, str):
            operations_by_name[(dataset_id, name)].append(identity_key)
    evidence = current_evidence_index(projection, include_expired=include_expired)
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for key, identities in operations_by_name.items():
        if len(identities) == 1 and identities[0] in evidence:
            result[key] = evidence[identities[0]]
    return result
