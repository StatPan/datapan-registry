#!/usr/bin/env python3
"""Generate the immutable health-probe policy catalog and CLI selector fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any
from urllib.parse import urlsplit


POLICY = pathlib.Path("policy/health-probe-canaries.json")
REGISTRY = pathlib.Path("data/data-go-kr.registry.json")
CATALOG = pathlib.Path("reports/health-probe-catalog.json")
FIXTURE = pathlib.Path("fixtures/health-probe-catalog/cli-health-probe-v1.json")


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def operation_key(fields: list[str]) -> str:
    value = bytearray()
    for field in fields:
        encoded = field.encode("utf-8")
        value.extend(f"{len(encoded)}:".encode("ascii"))
        value.extend(encoded)
    return hashlib.sha256(value).hexdigest()


def render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def build(policy: dict[str, Any], registry: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    for selection in policy["canaries"]:
        datasets = [dataset for dataset in registry if dataset["id"] == selection["dataset_id"]]
        if len(datasets) != 1:
            raise ValueError(f"{selection['operation_id']}: dataset selector must resolve exactly once")
        dataset = datasets[0]
        operations = [operation for operation in dataset["operations"] if operation["name"] == selection["operation_name"]]
        if len(operations) != 1:
            raise ValueError(f"{selection['operation_id']}: operation selector must resolve exactly once")
        operation = operations[0]
        parsed = urlsplit(operation["endpoint"])
        if not parsed.hostname or not parsed.path:
            raise ValueError(f"{selection['operation_id']}: endpoint must contain host and path")
        dependency = "data_go_kr_gateway" if parsed.hostname == "apis.data.go.kr" else "external_endpoint"
        reason = "data_go_kr_service_key_required" if dependency == "data_go_kr_gateway" else "registered_external_adapter_service_key_required"
        key = operation_key([dataset["provider"], dataset["id"], operation["name"], dependency, parsed.hostname.lower(), parsed.path])
        aliases = {
            "dataset_id": dataset["id"],
            "operation_name": operation["name"],
            "upstream_operation_seq": str(operation["source"]["raw"]["operation_seq"]),
            "cli_operation_key": key,
        }
        entry = {
            "operation_id": selection["operation_id"],
            "policy": {"key": selection["operation_id"], "version": 1, "authority": "datapan-registry", "max_level": "L4"},
            "aliases": aliases,
            "provider": dataset["provider"],
            "endpoint": {"host": parsed.hostname, "path": parsed.path, "dependency_class": dependency},
            "eligibility": {"status": "credential_required", "reason_code": reason},
            "credential_requirement": {"required": True, "type": "service_key", "scope": "operation"},
            "execution": {"timeout_ceiling_ms": 10000, "request_budget": 1, "safe_parameters": selection["safe_parameters"]},
            "response_freshness": {"mode": "not_asserted", "not_asserted_reason": policy["not_asserted_reason"]},
            "empty_data_policy": "observation_only",
        }
        entries.append(entry)
        cases.append({"operation_id": entry["operation_id"], "selector": {"ref": aliases["dataset_id"], "operation": aliases["operation_name"]}, "expected": {"operation_key": key, "dependency_class": dependency, "safe_parameter_names": sorted(parameter["name"] for parameter in selection["safe_parameters"]), "request_budget": 1}})
    gateway = sum(entry["endpoint"]["dependency_class"] == "data_go_kr_gateway" for entry in entries)
    catalog = {
        "schema_version": "datapan.health-probe-catalog.v1",
        "generated_at": policy["generated_at"],
        "authority": "datapan-registry",
        "receipt_contract": {"schema": "https://schemas.datapan.dev/datapan.health-probe.v1.schema.json", "operation_key_algorithm": "datapan-cli-health-operation-key-v1", "policy_authority": "datapan-registry"},
        "source_registry": {"path": REGISTRY.as_posix(), "sha256": hashlib.sha256(REGISTRY.read_bytes()).hexdigest()},
        "summary": {"entries": len(entries), "gateway_canaries": gateway, "external_adapter_canaries": len(entries) - gateway},
        "entries": entries,
    }
    fixture = {"schema_version": "datapan.health-probe-catalog-cli-fixture.v1", "catalog": CATALOG.as_posix(), "receipt_schema": "datapan.health-probe.v1", "cases": cases}
    return catalog, fixture


def write_or_check(path: pathlib.Path, value: Any, check: bool) -> None:
    expected = render(value)
    if check:
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise ValueError(f"generated artifact drift: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        catalog, fixture = build(load(POLICY), load(REGISTRY))
        write_or_check(CATALOG, catalog, args.check)
        write_or_check(FIXTURE, fixture, args.check)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL generate health probe catalog: {exc}", file=sys.stderr)
        return 1
    print(f"ok health probe catalog generation (entries={catalog['summary']['entries']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
