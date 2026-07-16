#!/usr/bin/env python3
"""Validate the draft data.go.kr diagnostic mapping and consumer packets."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
DRAFT = ROOT / "drafts/diagnostic-envelope"
MAPPING = DRAFT / "data-go-kr-evidence-mapping.v1.json"
CONTRACT = DRAFT / "consumer-contract.v1.json"
ENVELOPE_SCHEMA = DRAFT / "datapan.diagnostic-envelope.v1.schema.json"
PACKETS = DRAFT / "consumer-compatibility"
EXPECTED_CONSUMERS = ["datapan-cli", "datapan-health", "datapan-web"]
OBLIGATIONS = {"action", "scope", "timing", "redaction", "unknown_fallback"}
FORBIDDEN_KEYS = {"credential", "credentials", "headers", "request_body", "response_body", "rows", "secret", "user_id"}
FORBIDDEN_TEXT = (
    re.compile(r"(?i)\b(?:authorization|service[_-]?key|api[_-]?key)\s*[:=]"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~-]+"),
)


def load(path: pathlib.Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def reject_sensitive(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_KEYS:
                raise ValueError(f"{path}: forbidden sensitive field {key!r}")
            reject_sensitive(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_sensitive(child, f"{path}[{index}]")
    elif isinstance(value, str) and any(pattern.search(value) for pattern in FORBIDDEN_TEXT):
        raise ValueError(f"{path}: credential-like material is forbidden")


def eligible_causes(mapping: dict[str, Any], signals: set[str]) -> list[str]:
    return [
        item["cause"]
        for item in mapping["cause_mappings"]
        if item["cause"] != "unknown"
        and all(any(choice in signals for choice in group) for group in item["required_evidence_groups"])
    ]


def validate_mapping_vocabulary(mapping: dict[str, Any]) -> None:
    defs = load(ENVELOPE_SCHEMA)["$defs"]
    vocabularies = {
        ("approval_record", "state"): set(defs["approval_evidence"]["properties"]["state"]["enum"]),
        ("request_validation", "failure_class"): set(defs["request_validation_evidence"]["properties"]["failure_class"]["enum"]),
        ("provider_response", "class"): set(defs["response_evidence"]["properties"]["provider_class"]["enum"]),
        ("health_observation", "state"): set(defs["health_correlation_evidence"]["properties"]["state"]["enum"]),
        ("provider_notice", "state"): set(defs["notice_evidence"]["properties"]["state"]["enum"]),
        ("response_contract", "result"): set(defs["contract_assertion_evidence"]["properties"]["result"]["enum"]),
        ("data_quality_assertion", "kind"): set(defs["quality_assertion_evidence"]["properties"]["kind"]["enum"]),
        ("data_quality_assertion", "result"): set(defs["quality_assertion_evidence"]["properties"]["result"]["enum"]),
        ("freshness", "state"): set(defs["freshness_evidence"]["properties"]["state"]["enum"]),
        ("validation_result", "result"): set(defs["validation_evidence"]["properties"]["result"]["enum"]),
    }
    signals = {
        signal
        for item in mapping["cause_mappings"]
        for group in item["required_evidence_groups"]
        for signal in group
    }
    signals |= {
        signal
        for item in mapping["cause_mappings"]
        for variant in item.get("action_variants", [])
        for field in ("when_any", "when_all", "when_none")
        for signal in variant.get(field, [])
    }
    for signal in signals:
        if ":" not in signal:
            continue
        kind, conditions = signal.split(":", 1)
        for condition in conditions.split(","):
            if "=" not in condition:
                continue
            field, value = condition.split("=", 1)
            allowed = vocabularies.get((kind, field))
            if allowed is not None and value not in allowed:
                raise ValueError(f"mapping vocabulary drift: {kind}.{field}={value}")


def validate_inputs(mapping: dict[str, Any]) -> None:
    for item in mapping["authoritative_inputs"]:
        path = ROOT / item["path"]
        if not path.is_file():
            raise ValueError(f"missing authoritative input: {item['path']}")
        value = load(path)
        if item["schema_version"] != "json-schema-draft-2020-12" and value.get("schema_version") != item["schema_version"]:
            raise ValueError(f"schema version drift: {item['path']}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"authoritative input digest drift: {item['path']}")


def validate_mapping(mapping: dict[str, Any], contract: dict[str, Any]) -> None:
    boundary = mapping.get("authority_boundary", {})
    if mapping.get("status") != "draft" or boundary.get("publishing_allowed") is not False:
        raise ValueError("mapping must remain an unpublished draft")
    if boundary.get("runtime_inference_owner") != "consumer":
        raise ValueError("Registry must not own live inference")
    required_policy = {
        "specific_cause_requires_all_groups": True,
        "candidate_only_signal_can_select_cause": False,
        "multiple_eligible_specific_causes": "unknown",
        "no_eligible_specific_cause": "unknown",
        "unknown_determination": "unknown",
        "unknown_action": "gather_more_evidence",
        "evidence_must_match_subject_operation": True,
        "evidence_must_be_current_at_assessment": True,
        "raw_or_secret_material_allowed": False,
    }
    if mapping.get("resolution_policy") != required_policy:
        raise ValueError("safe resolution policy drift")

    catalog = {item["code"]: item for item in contract["cause_catalog"]}
    mappings = {item["cause"]: item for item in mapping["cause_mappings"]}
    if list(mappings) != list(catalog):
        raise ValueError("mapping must cover envelope causes in contract order")
    fixture_owners = {
        fixture["cause"]["code"]: fixture["ownership"]["accountable_party"]
        for path in (DRAFT / "fixtures").glob("*.json")
        for fixture in [load(path)]
    }
    for cause, item in mappings.items():
        if item["action"] != catalog[cause]["required_action"]:
            raise ValueError(f"{cause}: action drift")
        if item.get("accountable_party") != fixture_owners[cause]:
            raise ValueError(f"{cause}: accountable party drift")
        required_avoid = catalog[cause].get("required_avoid_action")
        if required_avoid and required_avoid not in item["avoid"]:
            raise ValueError(f"{cause}: missing required avoid action")
        if not item["source_basis"] or not item["required_evidence_groups"]:
            raise ValueError(f"{cause}: source basis and corroboration are required")

    rule_ids = {item["rule_id"] for item in load(ROOT / "reports/data-go-kr/error-action-catalog.json")["rules"]}
    referenced_rules = {
        ref.removeprefix("registry_rule:")
        for item in mappings.values()
        for ref in item["source_basis"]
        if ref.startswith("registry_rule:")
    }
    if referenced_rules - rule_ids:
        raise ValueError(f"unknown Registry rule refs: {sorted(referenced_rules - rule_ids)}")
    dangerous = {
        "registry_rule:data-go-kr-service-key-not-registered",
        "provider_response:http-401",
        "provider_response:http-403",
    }
    if not dangerous.issubset(mapping["candidate_only_signals"]):
        raise ValueError("generic credential/approval symptoms must be candidate-only")
    outage = mappings["provider_outage"]
    expected_variants = [
        {
            "when_any": ["health_observation:state=unavailable,probe_policy_version=present", "health_observation:state=degraded,probe_policy_version=present", "provider_notice:state=service_suspended,notice_version=present", "provider_notice:state=degraded,notice_version=present"],
            "action": "check_provider_status",
            "avoid": ["reissue_credential"],
        },
        {
            "when_all": ["provider_response:class=service_unavailable,policy_version=present"],
            "when_none": ["health_observation:state=unavailable,probe_policy_version=present", "health_observation:state=degraded,probe_policy_version=present", "provider_notice:state=service_suspended,notice_version=present", "provider_notice:state=degraded,notice_version=present"],
            "action": "check_provider_status",
            "avoid": [],
        },
    ]
    if outage.get("action_variants") != expected_variants:
        raise ValueError("provider_outage evidence-dependent action variants drift")
    validate_mapping_vocabulary(mapping)
    for case in mapping["proof_cases"]:
        eligible = eligible_causes(mapping, set(case["signals"]))
        if eligible != case["expected_eligible_causes"]:
            raise ValueError(f"{case['case_id']}: eligible cause proof drift")
        selected = eligible[0] if len(eligible) == 1 else "unknown"
        if selected != case["expected_result"]:
            raise ValueError(f"{case['case_id']}: selected result proof drift")
    reject_sensitive(mapping, "mapping")


def validate_packets() -> None:
    paths = sorted(PACKETS.glob("*.v1.json"))
    packets = [load(path) for path in paths]
    if [item["consumer"] for item in packets] != EXPECTED_CONSUMERS:
        raise ValueError("consumer compatibility packet set drift")
    for path, packet in zip(paths, packets, strict=True):
        if packet.get("schema_version") != "datapan.diagnostic-consumer-compatibility.v1" or packet.get("status") != "draft":
            raise ValueError(f"{path.name}: packet version/status drift")
        if set(packet.get("obligations", {})) != OBLIGATIONS:
            raise ValueError(f"{path.name}: action/scope/timing/redaction/unknown obligations required")
        production = packet.get("production_status", {})
        if set(production) != {"currently_proven", "required_after_dependencies"}:
            raise ValueError(f"{path.name}: production proof status must separate current and dependency-gated evidence")
        if set(production["currently_proven"]) & set(production["required_after_dependencies"]):
            raise ValueError(f"{path.name}: production proof status overlaps")
        if not production["required_after_dependencies"] or not packet.get("consumes") or not packet.get("forbidden"):
            raise ValueError(f"{path.name}: incomplete producer/consumer boundary")
        reject_sensitive(packet, path.name)


def validate_draft_boundary() -> None:
    public_paths = {item["path"] for item in load(ROOT / "schemas/index.json").get("schemas", [])}
    public_paths |= {item["path"] for item in load(ROOT / "manifest.json").get("artifacts", [])}
    if any(path.startswith("drafts/diagnostic-envelope/") for path in public_paths):
        raise ValueError("draft mapping or packets must not be public release artifacts")


def validate_all() -> dict[str, int]:
    mapping, contract = load(MAPPING), load(CONTRACT)
    validate_inputs(mapping)
    validate_mapping(mapping, contract)
    validate_packets()
    validate_draft_boundary()
    return {"inputs": len(mapping["authoritative_inputs"]), "causes": len(mapping["cause_mappings"]), "proof_cases": len(mapping["proof_cases"]), "consumers": len(EXPECTED_CONSUMERS)}


def main() -> int:
    try:
        summary = validate_all()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL diagnostic evidence mapping draft: {exc}", file=sys.stderr)
        return 1
    print("ok diagnostic evidence mapping draft (" + ", ".join(f"{key}={value}" for key, value in summary.items()) + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
