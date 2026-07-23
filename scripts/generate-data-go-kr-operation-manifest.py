#!/usr/bin/env python3
"""Generate the immutable API-operation manifest used by Health consumers.

The generator only reads checked-in Registry data.  It never calls a provider,
uses credentials, or records request/response values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from collections import Counter
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "data/data-go-kr.registry.json"
OUTPUT = ROOT / "reports/data-go-kr/operation-manifest.json"
AUTH_NAMES = {"servicekey", "service_key", "apikey", "api_key", "authorization", "authkey", "auth_key"}
IDENTITY_FIELDS = ["provider", "dataset_id", "protocol", "source_system", "upstream_operation_key", "endpoint", "method_or_action", "operation_name"]


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def render(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def length_prefixed_sha256(fields: list[str]) -> str:
    payload = bytearray()
    for field in fields:
        encoded = field.encode("utf-8")
        payload.extend(f"{len(encoded)}:".encode("ascii"))
        payload.extend(encoded)
    return hashlib.sha256(payload).hexdigest()


def protocol_for(dataset: dict[str, Any], operation: dict[str, Any]) -> str | None:
    source = operation.get("source", {})
    raw = source.get("raw", {}) if isinstance(source, dict) else {}
    if source.get("system") == "safetydata.go.kr" and raw.get("source_api_type") == "REST":
        return "REST"
    dataset_raw = dataset.get("source", {}).get("raw", {})
    value = dataset_raw.get("api_type")
    return value if value in {"REST", "SOAP"} else None


def parameter_rows(operation: dict[str, Any]) -> tuple[list[dict[str, str]], list[str], list[str]]:
    all_parameters: list[dict[str, str]] = []
    auth: list[str] = []
    approval: list[str] = []
    for item in operation.get("request_params", []):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            continue
        name = item["name"]
        all_parameters.append({"name": name, "label": str(item.get("label") or "")})
        if name.lower().replace("-", "_") in AUTH_NAMES:
            auth.append(name)
        else:
            approval.append(name)
    return all_parameters, sorted(set(auth)), sorted(set(approval))


def build(registry: list[dict[str, Any]]) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    link_operations = 0
    operationless = 0
    filedata = 0
    for dataset in registry:
        raw_dataset = dataset.get("source", {}).get("raw", {})
        dataset_operations = dataset.get("operations", [])
        if not dataset_operations:
            operationless += 1
            if raw_dataset.get("list_type") == "PR0010":
                filedata += 1
            continue
        for operation in dataset_operations:
            protocol = protocol_for(dataset, operation)
            if protocol is None:
                link_operations += 1
                continue
            source = operation.get("source", {})
            source_raw = source.get("raw", {}) if isinstance(source, dict) else {}
            endpoint = operation.get("endpoint") if isinstance(operation.get("endpoint"), str) and operation.get("endpoint") else None
            if source.get("system") == "safetydata.go.kr":
                upstream_key = str(source_raw.get("source_interface_id") or source_raw.get("data_sn") or "")
            else:
                upstream_key = str(source_raw.get("operation_seq") or "")
            if not upstream_key:
                raise ValueError(f"{dataset.get('id')}: API operation lacks immutable upstream operation key")
            if protocol == "SOAP":
                action = str(source_raw.get("operation_url") or operation.get("name") or "")
                method, method_evidence, method_or_action = None, "soap_action", action
            else:
                action, method, method_evidence, method_or_action = None, "GET", "registry_default_get", "GET"
            params, auth, approval = parameter_rows(operation)
            if endpoint is None:
                readiness = {"status": "endpoint_missing", "reason": "registry_operation_has_no_endpoint"}
                eligibility = {"status": "excluded", "excluded_reason": "endpoint_missing"}
            elif approval:
                readiness = {"status": "approval_required", "reason": "non_auth_request_parameters_require_explicit_value_approval"}
                eligibility = {"status": "approval_required", "excluded_reason": "required_parameter_approval"}
            else:
                # The catalog preserves parameter names but not a required/optional
                # declaration.  Treating a blank list as proof of an optional call
                # would turn a static Registry snapshot into an unsafe execution
                # policy.  Health only receives an eligible identity after #597
                # supplies a reviewed value/approval policy.
                readiness = {"status": "approval_required", "reason": "required_parameter_cardinality_not_recorded"}
                eligibility = {"status": "approval_required", "excluded_reason": "required_parameter_approval"}
            fields = ["data.go.kr", str(dataset.get("id") or ""), protocol, str(source.get("system") or ""), upstream_key, endpoint or "", method_or_action, str(operation.get("name") or "")]
            operations.append({
                "operation_id": length_prefixed_sha256(fields),
                "protocol": protocol,
                "provenance": {"provider": "data.go.kr", "dataset_id": str(dataset.get("id") or ""), "operation_name": str(operation.get("name") or ""), "source_system": source.get("system"), "source_url": source.get("url"), "upstream_operation_key": upstream_key},
                "transport": {"endpoint": endpoint, "method": method, "action": action, "method_evidence": method_evidence},
                "call_readiness": readiness,
                "requirements": {"auth_parameter_names": auth, "approval_parameter_names": approval, "all_request_parameters": params},
                "eligibility": eligibility,
            })
    operations.sort(key=lambda item: item["operation_id"])
    protocols = Counter(item["protocol"] for item in operations)
    eligibility = Counter(item["eligibility"]["status"] for item in operations)
    source_bytes = REGISTRY.read_bytes()
    result = {
        "schema_version": "datapan.data-go-kr-operation-manifest.v1",
        "authority": "datapan-registry",
        "source_snapshot": {"path": "data/data-go-kr.registry.json", "bytes": len(source_bytes), "sha256": hashlib.sha256(source_bytes).hexdigest()},
        "identity_contract": {"algorithm": "sha256-length-prefixed-utf8-v1", "fields": IDENTITY_FIELDS},
        "summary": {"api_operations": len(operations), "protocols": {"REST": protocols["REST"], "SOAP": protocols["SOAP"]}, "eligibility": dict(sorted(eligibility.items())), "exclusions": {"link_operations": link_operations, "operationless_catalog_entries": operationless, "filedata_catalog_entries": filedata}, "identity_collisions": len(operations) - len({item["operation_id"] for item in operations}), "identity_omissions": sum(not item["operation_id"] for item in operations)},
        "operations": operations,
    }
    return result


def main() -> int:
    global REGISTRY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--registry", type=pathlib.Path, default=REGISTRY)
    parser.add_argument("--output", type=pathlib.Path, default=OUTPUT)
    args = parser.parse_args()
    REGISTRY = args.registry
    try:
        value = build(load(args.registry))
        expected = render(value)
        if args.check:
            if not args.output.is_file() or args.output.read_bytes() != expected:
                raise ValueError(f"generated artifact drift: {args.output}")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(expected)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL generate data.go.kr operation manifest: {exc}", file=sys.stderr)
        return 1
    summary = value["summary"]
    print(f"ok data.go.kr operation manifest (api={summary['api_operations']}, rest={summary['protocols']['REST']}, soap={summary['protocols']['SOAP']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
