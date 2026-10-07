#!/usr/bin/env python3
"""Generate test-only REST and SOAP consumers for the operation-plan schema."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "fixtures/operation-observation-plan/synthetic-source.json"
SCHEMA = ROOT / "schemas/datapan.operation-observation-plan.v1.schema.json"
OUTPUT = ROOT / "fixtures/operation-observation-plan"
SCOPE_PREFIX = b"datapan.quota-scope.v1\0"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def pointer_ref(source_hash: str, pointer: str) -> dict[str, str]:
    return {
        "artifact_path": SOURCE.relative_to(ROOT).as_posix(),
        "sha256": source_hash,
        "json_pointer": pointer,
        "evidence_kind": "synthetic_fixture",
    }


def quota_digest(scope_kind: str, scope_key: str) -> str:
    data = SCOPE_PREFIX + scope_kind.encode("utf-8") + b"\0" + scope_key.encode("utf-8")
    return sha256(data)


def build_records() -> list[dict[str, object]]:
    source_bytes = SOURCE.read_bytes()
    source_hash = sha256(source_bytes)
    source = json.loads(source_bytes)
    source_ref = {
        "path": SOURCE.relative_to(ROOT).as_posix(),
        "sha256": source_hash,
        "bytes": len(source_bytes),
    }
    records = []

    for key in ("rest", "soap"):
        spec = source["operations"][key]
        base = f"#/operations/{key}"
        evidence = lambda suffix: pointer_ref(source_hash, base + suffix)
        parameters = []
        for index, parameter in enumerate(spec["parameters"]):
            strategy = parameter["value_strategy"]
            strategy_contract = {"kind": strategy["kind"]}
            if strategy["kind"] == "credential_reference":
                strategy_contract.update({"authority": "runtime_binding", "binding_field": "credential_reference"})
            else:
                strategy_contract["authority"] = "synthetic_fixture"
            strategy_contract.update(
                {
                    name: strategy[name]
                    for name in ("minimum", "maximum", "offset_years", "selection", "selected_value", "minimum_year", "maximum_year", "anchor")
                    if name in strategy
                }
            )
            parameters.append(
                {
                    "name": parameter["name"],
                    **({"qualified_name": parameter["qualified_name"]} if "qualified_name" in parameter else {}),
                    "location": parameter["location"],
                    "cardinality": parameter["cardinality"],
                    "value_strategy": strategy_contract,
                    "evidence_refs": [evidence(f"/parameters/{index}")],
                }
            )

        transport = spec["transport"]
        authentication = spec["authentication"]
        limits = spec["limits"]
        assertion = spec["response_assertion"]
        contract = {
            "transport": {
                **transport,
                "protocol": spec["protocol"],
                "authority": "synthetic_fixture",
                "evidence_refs": [evidence("/transport")],
            },
            "operation_effect": {
                **spec["effect"],
                "authority": "synthetic_fixture",
                "evidence_refs": [evidence("/effect")],
            },
            "parameter_inventory_evidence_refs": [evidence("/parameters")],
            "parameters": parameters,
            "authentication": {
                **authentication,
                "evidence_refs": [evidence("/authentication")],
            },
            "limits": {**limits, "evidence_refs": [evidence("/limits")]},
            "response_assertion": {
                **assertion,
                "evidence_refs": [evidence("/response_assertion")],
            },
        }

        quota_policies = []
        for policy, policy_pointer in (
            (source["shared_quota_policy"], "#/shared_quota_policy"),
            (source["api_quota_policies"][key], f"#/api_quota_policies/{key}"),
        ):
            quota_policies.append(
                {
                    **policy,
                    "scope_sha256": quota_digest(policy["scope_kind"], policy["scope_key"]),
                    "evidence_refs": [pointer_ref(source_hash, policy_pointer)],
                }
            )

        record = {
            "schema_version": "datapan.operation-observation-plan.v1",
            "artifact_kind": "operation_plan",
            "source_binding": {
                "source_id": "synthetic_test",
                "provider": "synthetic-test-provider",
                "adapter_id": "synthetic-test",
                "inventory_status": "source_complete",
                "inventory_unknown": False,
                "test_only": True,
                "source_artifacts": [source_ref],
            },
            "operation_identity": {
                "operation_id": spec["operation_id"],
                "protocol": spec["protocol"],
                "registered_endpoint": {"host": transport["host"], "path": transport["path"]},
            },
            "request_plan": {
                "status": "complete",
                "evidence_refs": [evidence("")],
                "request_contract": contract,
            },
            "runtime_binding": {
                "status": "bound",
                "credential_reference": spec["runtime_binding"]["credential_reference"],
                "credential_scope_key": source["shared_quota_policy"]["scope_key"],
                "quota_policies": quota_policies,
                "observation_period_seconds": spec["runtime_binding"]["observation_period_seconds"],
                "evidence_refs": [pointer_ref(source_hash, f"#/operations/{key}/runtime_binding")],
            },
            "admission": {
                "status": "admitted",
                "reasons": [],
                "evidence_refs": [pointer_ref(source_hash, "#/test_only")],
            },
        }
        records.append(record)
    return records


def main() -> int:
    try:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
        records = build_records()
        for record in records:
            validator.validate(record)
            operation = record["operation_identity"]["operation_id"]
            OUTPUT.joinpath(f"{operation}.json").write_text(dump(record), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL synthetic operation-plan fixtures: {exc}", file=sys.stderr)
        return 1
    print("ok synthetic operation-plan fixtures (REST=1, SOAP=1, shared credential quota=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
