#!/usr/bin/env python3
"""Validate the immutable, manifest-bound Datapan health probe catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import jsonschema


CATALOG = pathlib.Path("reports/health-probe-catalog.json")
SCHEMA = pathlib.Path("schemas/datapan.health-probe-catalog.v1.schema.json")
REGISTRY = pathlib.Path("data/data-go-kr.registry.json")
MANIFEST = pathlib.Path("manifest.json")
FIXTURE = pathlib.Path("fixtures/health-probe-catalog/cli-health-probe-v1.json")
AUTH_NAMES = {"servicekey", "service_key", "apikey", "api_key", "authorization"}
FORBIDDEN_KEYS = {"credential_value", "query_value", "response_rows", "live_status", "receipt"}


def load(path: pathlib.Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def digest(path: pathlib.Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def operation_key(fields: list[str]) -> str:
    value = bytearray()
    for field in fields:
        encoded = field.encode("utf-8")
        value.extend(f"{len(encoded)}:".encode("ascii"))
        value.extend(encoded)
    return hashlib.sha256(value).hexdigest()


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def reject_mutable_keys(value: Any, path: str = "catalog") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            fail(key.lower() not in FORBIDDEN_KEYS, f"{path}: forbidden mutable field {key!r}")
            reject_mutable_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_mutable_keys(child, f"{path}[{index}]")


def validate_catalog(catalog: dict[str, Any], schema: dict[str, Any], registry: list[dict[str, Any]], manifest: dict[str, Any], fixture: dict[str, Any], *, catalog_path: pathlib.Path = CATALOG, schema_path: pathlib.Path = SCHEMA, registry_path: pathlib.Path = REGISTRY) -> None:
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(catalog)
    reject_mutable_keys(catalog)

    artifacts = {item["path"]: item for item in manifest["artifacts"]}
    for path in (catalog_path, schema_path, registry_path):
        fail(path.as_posix() in artifacts, f"manifest does not bind {path}")
        byte_count, sha = digest(path)
        artifact = artifacts[path.as_posix()]
        fail((artifact["bytes"], artifact["sha256"]) == (byte_count, sha), f"manifest digest drift: {path}")
    fail(artifacts[catalog_path.as_posix()].get("kind") == "health_probe_catalog", "catalog manifest kind is not health_probe_catalog")
    fail(artifacts[catalog_path.as_posix()].get("schema") == schema["$id"], "catalog manifest schema mismatch")
    fail(catalog["source_registry"]["sha256"] == artifacts[registry_path.as_posix()]["sha256"], "catalog source registry digest mismatch")

    entries = catalog["entries"]
    fail(len({e["operation_id"] for e in entries}) == len(entries), "operation_id must be unique")
    fail(len({e["aliases"]["cli_operation_key"] for e in entries}) == len(entries), "cli_operation_key alias must be unique")
    generated_year = datetime.fromisoformat(catalog["generated_at"].replace("Z", "+00:00")).year

    gateway = 0
    external = 0
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        operation_id = entry["operation_id"]
        by_id[operation_id] = entry
        fail(entry["policy"]["key"] == operation_id, f"{operation_id}: policy key must be stable operation_id")
        aliases = entry["aliases"]
        matches = [(d, o) for d in registry if d["id"] == aliases["dataset_id"] for o in d["operations"] if o["name"] == aliases["operation_name"]]
        fail(len(matches) == 1, f"{operation_id}: aliases do not resolve exactly once")
        dataset, operation = matches[0]
        raw = operation["source"]["raw"]
        fail(str(raw["operation_seq"]) == aliases["upstream_operation_seq"], f"{operation_id}: upstream operation seq drift")
        parsed = urlsplit(operation["endpoint"])
        endpoint = entry["endpoint"]
        fail((parsed.hostname, parsed.path) == (endpoint["host"], endpoint["path"]), f"{operation_id}: endpoint drift")
        dependency = "data_go_kr_gateway" if parsed.hostname == "apis.data.go.kr" else "external_endpoint"
        fail(endpoint["dependency_class"] == dependency, f"{operation_id}: dependency class mismatch")
        fields = [dataset["provider"], dataset["id"], operation["name"], dependency, parsed.hostname.lower(), parsed.path]
        fail(operation_key(fields) == aliases["cli_operation_key"], f"{operation_id}: CLI operation key drift")
        fail(entry["provider"] == dataset["provider"], f"{operation_id}: provider drift")

        credential = entry["credential_requirement"]
        status = entry["eligibility"]["status"]
        if status == "eligible":
            fail(credential == {"required": False, "type": "none", "scope": "none"}, f"{operation_id}: eligible operation must not require credentials")
        if status == "credential_required":
            fail(credential["required"] and credential["type"] != "none" and credential["scope"] != "none", f"{operation_id}: credential requirement is underspecified")
        if status in {"eligible", "credential_required"}:
            gateway += dependency == "data_go_kr_gateway"
            external += dependency == "external_endpoint"
            execution = entry["execution"]
            request_names = {p["name"] for p in operation["request_params"]}
            for parameter in execution["safe_parameters"]:
                name = parameter["name"]
                fail(name in request_names and name.lower() not in AUTH_NAMES, f"{operation_id}: unsafe or unknown parameter {name}")
                if parameter["strategy"] == "bounded_integer":
                    fail(parameter["minimum"] <= parameter["maximum"], f"{operation_id}: invalid integer bounds")
                else:
                    value = generated_year + parameter["offset_years"]
                    fail(parameter["minimum_year"] <= value <= parameter["maximum_year"], f"{operation_id}: generated year outside bounds")

    expected_summary = {"entries": len(entries), "gateway_canaries": gateway, "external_adapter_canaries": external}
    fail(catalog["summary"] == expected_summary, "catalog summary does not match entries")

    fail(fixture.get("catalog") == catalog_path.as_posix(), "fixture catalog path mismatch")
    probeable_ids = {e["operation_id"] for e in entries if e["eligibility"]["status"] in {"eligible", "credential_required"}}
    fail({case["operation_id"] for case in fixture.get("cases", [])} == probeable_ids, "fixture must cover every probeable catalog entry")
    for case in fixture["cases"]:
        entry = by_id.get(case["operation_id"])
        fail(entry is not None, f"fixture references unknown operation_id {case['operation_id']}")
        aliases = entry["aliases"]
        fail(case["selector"] == {"ref": aliases["dataset_id"], "operation": aliases["operation_name"]}, f"{case['operation_id']}: fixture selector drift")
        expected = case["expected"]
        fail(expected["operation_key"] == aliases["cli_operation_key"], f"{case['operation_id']}: fixture operation key drift")
        fail(expected["dependency_class"] == entry["endpoint"]["dependency_class"], f"{case['operation_id']}: fixture dependency drift")
        safe_names = sorted(p["name"] for p in entry["execution"]["safe_parameters"])
        fail(expected["safe_parameter_names"] == safe_names, f"{case['operation_id']}: fixture safe parameters drift")
        fail(expected["request_budget"] == entry["execution"]["request_budget"], f"{case['operation_id']}: fixture request budget drift")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        validate_catalog(load(CATALOG), load(SCHEMA), load(REGISTRY), load(MANIFEST), load(FIXTURE))
    except Exception as exc:  # noqa: BLE001 - emit one release-gate failure
        print(f"FAIL health probe catalog: {exc}", file=sys.stderr)
        return 1
    print("ok health probe catalog (entries=2, gateway=1, external_adapter=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
