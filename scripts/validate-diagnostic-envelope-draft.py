#!/usr/bin/env python3
"""Validate the unreleased diagnostic envelope contract and deterministic examples."""

from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
DRAFT = ROOT / "drafts/diagnostic-envelope"
SCHEMA = DRAFT / "datapan.diagnostic-envelope.v1.schema.json"
CONSUMER_CONTRACT = DRAFT / "consumer-contract.v1.json"
FIXTURES = DRAFT / "fixtures"
SCHEMA_INDEX = ROOT / "schemas/index.json"
MANIFEST = ROOT / "manifest.json"

EXPECTED = {
    "approval-propagating.json": "approval_propagating",
    "approval-required.json": "approval_required",
    "contract-drift.json": "contract_drift",
    "credential-invalid.json": "credential_invalid",
    "invalid-input.json": "invalid_input",
    "provider-outage.json": "provider_outage",
    "rate-limited.json": "rate_limited",
    "ready.json": "ready",
    "semantic-quality.json": "semantic_quality",
    "stale-data.json": "stale_data",
    "unknown.json": "unknown",
}
FORBIDDEN_KEYS = {
    "authorization",
    "credential",
    "credential_hash",
    "credential_value",
    "credentials",
    "raw_provider_text",
    "raw_provider_url",
    "request_body",
    "request_headers",
    "request_url",
    "response_body",
    "response_rows",
    "secret",
    "secret_hash",
    "secret_value",
    "user_id",
    "user_identity",
}
FORBIDDEN_TEXT = (
    re.compile(r"(?i)\b(?:authorization|service[_-]?key|api[_-]?key)\s*[:=]"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~-]+"),
    re.compile(r"https?://"),
)
VALIDATION_LEVEL_RANK = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}


def load(path: pathlib.Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def canonical_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def reject_sensitive(value: Any, path: str = "envelope") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_KEYS:
                raise ValueError(f"{path}: forbidden sensitive field {key!r}")
            reject_sensitive(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            reject_sensitive(child, f"{path}[{index}]")
        return
    if isinstance(value, str):
        for pattern in FORBIDDEN_TEXT:
            if pattern.search(value):
                raise ValueError(f"{path}: forbidden raw or credential-like text")


def validate_draft_boundary() -> None:
    draft_prefix = "drafts/diagnostic-envelope/"
    index_paths = {item["path"] for item in load(SCHEMA_INDEX).get("schemas", [])}
    manifest_paths = {item["path"] for item in load(MANIFEST).get("artifacts", [])}
    if any(path.startswith(draft_prefix) for path in index_paths | manifest_paths):
        raise ValueError("draft diagnostic contract must not be schema-indexed or manifest-bound before release review")
    if "schemas/datapan.diagnostic-envelope.v1.schema.json" in index_paths | manifest_paths:
        raise ValueError("public diagnostic schema is present before consumer compatibility review")


def validate_contract(contract: dict[str, Any], schema: dict[str, Any]) -> None:
    if contract.get("status") != "draft":
        raise ValueError("consumer contract status must remain draft")
    if contract.get("envelope_schema") != "drafts/diagnostic-envelope/datapan.diagnostic-envelope.v1.schema.json":
        raise ValueError("consumer contract schema path drift")
    if contract.get("required_consumers") != ["datapan-cli", "datapan-health", "datapan-web"]:
        raise ValueError("consumer review set drift")
    compatibility = contract.get("compatibility", {})
    if compatibility.get("certainty_axis") != ["observed", "inferred", "unknown"]:
        raise ValueError("certainty axis must be observed/inferred/unknown")
    if compatibility.get("numeric_or_probability_confidence_allowed") is not False:
        raise ValueError("numeric or probability-like confidence is not allowed")
    catalog_codes = [item["code"] for item in contract.get("cause_catalog", [])]
    schema_codes = schema["$defs"]["cause"]["properties"]["code"]["enum"]
    if catalog_codes != schema_codes:
        raise ValueError("consumer cause catalog must match schema enum order exactly")
    reject_sensitive(contract, "consumer_contract")


def validate_semantics(value: dict[str, Any]) -> None:
    if value.get("cause", {}).get("code") != "ready":
        return
    validations = [
        item["validation"]
        for item in value.get("evidence_refs", [])
        if item.get("kind") == "validation_result"
        and item.get("validation", {}).get("result") == "passed"
    ]
    if not any(
        VALIDATION_LEVEL_RANK[item["achieved_level"]]
        >= VALIDATION_LEVEL_RANK[item["required_level"]]
        for item in validations
    ):
        raise ValueError("ready requires achieved validation level to meet or exceed required level")


def validate_fixture(path: pathlib.Path, value: dict[str, Any], validator: jsonschema.Draft202012Validator) -> None:
    validator.validate(value)
    validate_semantics(value)
    if value.get("fixture", {}).get("status") != "deterministic_example":
        raise ValueError(f"{path.name}: fixture status must be deterministic_example")
    if value["fixture"]["scenario_id"] != path.stem:
        raise ValueError(f"{path.name}: scenario_id must match filename")
    if value["fixture"]["snapshot_at"] != value["assessed_at"]:
        raise ValueError(f"{path.name}: snapshot_at must equal assessed_at")
    if value["cause"]["code"] != EXPECTED[path.name]:
        raise ValueError(f"{path.name}: cause code drift")
    if path.read_text(encoding="utf-8") != canonical_text(value):
        raise ValueError(f"{path.name}: fixture JSON is not deterministic canonical text")
    reject_sensitive(value, path.name)


def validate_all() -> dict[str, int]:
    schema = load(SCHEMA)
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    contract = load(CONSUMER_CONTRACT)
    validate_contract(contract, schema)
    validate_draft_boundary()

    paths = sorted(FIXTURES.glob("*.json"))
    if {path.name for path in paths} != set(EXPECTED):
        raise ValueError("fixture set must cover every declared deterministic scenario exactly once")
    for path in paths:
        validate_fixture(path, load(path), validator)

    symptom = "provider-response:http-401"
    same_symptom = {
        value["cause"]["code"]
        for path in paths
        for value in [load(path)]
        if any(item["ref_id"] == symptom for item in value["evidence_refs"])
    }
    if same_symptom != {"approval_propagating", "credential_invalid", "provider_outage"}:
        raise ValueError("same HTTP symptom must demonstrate three evidence-dependent causes and actions")
    return {"fixtures": len(paths), "causes": len(EXPECTED)}


def main() -> int:
    try:
        summary = validate_all()
    except Exception as exc:  # noqa: BLE001 - one release-style failure
        print(f"FAIL diagnostic envelope draft: {exc}", file=sys.stderr)
        return 1
    print(f"ok diagnostic envelope draft (fixtures={summary['fixtures']}, causes={summary['causes']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
