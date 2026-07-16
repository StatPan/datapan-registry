#!/usr/bin/env python3
"""Validate and execute the static draft diagnostic mapping proof."""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import sys
from typing import Any

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
DRAFT = ROOT / "drafts/diagnostic-envelope"
MAPPING = DRAFT / "data-go-kr-evidence-mapping.v1.json"
CONTRACT = DRAFT / "consumer-contract.v1.json"
ENVELOPE_SCHEMA = DRAFT / "datapan.diagnostic-envelope.v1.schema.json"
FIXTURES = DRAFT / "fixtures"
PACKETS = DRAFT / "consumer-compatibility"
EXPECTED_CONSUMERS = ["datapan-cli", "datapan-health", "datapan-web"]
OBLIGATIONS = {"action", "scope", "timing", "redaction", "unknown_fallback"}
SOURCE_BASIS_TYPES = {"registry_rule", "registry_fact", "consumer_evidence", "resolution_policy"}
EXPECTED_OBLIGATIONS = {"action": "exact_mapping_result", "scope": "exact_bound_subject", "timing": "current_positive_validity", "redaction": "envelope_redaction_contract", "unknown_fallback": "unknown_gather_more_evidence"}
EXPECTED_DEPENDENCIES = {
    "datapan-cli": ["StatPan/datapan-cli#160"],
    "datapan-health": ["StatPan/datapan-health#19", "StatPan/datapan-health#21", "StatPan/datapan-health#22"],
    "datapan-web": ["StatPan/datapan#7", "StatPan/datapan#8", "StatPan/datapan#9", "StatPan/datapan#10", "StatPan/datapan#11"],
}
EXPECTED_PRODUCERS = {
    "datapan-cli": ["request_validation", "provider_response", "validation_result", "data_quality_assertion"],
    "datapan-health": ["health_observation", "validation_result", "data_quality_assertion"],
    "datapan-web": ["response_contract", "data_quality_assertion"],
}


def load(path: pathlib.Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def pointer_get(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError(f"invalid JSON pointer: {pointer}")
    current = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def pointer_set(value: Any, pointer: str, replacement: Any) -> None:
    tokens = pointer[1:].split("/")
    current = value
    for raw in tokens[:-1]:
        token = raw.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    token = tokens[-1].replace("~1", "/").replace("~0", "~")
    if isinstance(current, list):
        current[int(token)] = replacement
    else:
        current[token] = replacement


def evidence_validator() -> jsonschema.Draft202012Validator:
    schema = load(ENVELOPE_SCHEMA)
    wrapper = {"$schema": schema["$schema"], "$ref": "#/$defs/evidence_ref", "$defs": schema["$defs"]}
    return jsonschema.Draft202012Validator(wrapper, format_checker=jsonschema.FormatChecker())


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


def schema_contract() -> dict[str, dict[str, Any]]:
    defs = load(ENVELOPE_SCHEMA)["$defs"]
    variants = load(CONTRACT)["evidence_variants"]
    payloads = {
        "approval_record": ("approval", "approval_evidence"),
        "request_validation": ("request_validation", "request_validation_evidence"),
        "provider_response": ("response", "response_evidence"),
        "health_observation": ("health_correlation", "health_correlation_evidence"),
        "provider_notice": ("notice", "notice_evidence"),
        "response_contract": ("contract_assertion", "contract_assertion_evidence"),
        "data_quality_assertion": ("quality_assertion", "quality_assertion_evidence"),
        "validation_result": ("validation", "validation_evidence"),
    }
    result = {}
    for kind, variant in variants.items():
        allowed = {"/ref_id"}
        if kind in payloads:
            field, definition = payloads[kind]
            allowed |= {f"/{field}/{name}" for name in defs[definition]["properties"]}
            if kind == "data_quality_assertion":
                allowed |= {f"/freshness/{name}" for name in defs["freshness_evidence"]["properties"]}
            if kind == "approval_record":
                allowed |= {"/approval/effective_scope/level", "/approval/effective_scope/subject_ref", "/approval/effective_scope/detail_id"}
        result[kind] = {
            "authorities": set(variant["allowed_authorities"]),
            "scope": variant["allowed_scope_level"],
            "allowed_match_paths": allowed,
            "enums": {},
        }
    enum_bindings = {
        ("approval_record", "/approval/state"): ("approval_evidence", "state"),
        ("request_validation", "/request_validation/result"): ("request_validation_evidence", "result"),
        ("request_validation", "/request_validation/failure_class"): ("request_validation_evidence", "failure_class"),
        ("provider_response", "/response/provider_class"): ("response_evidence", "provider_class"),
        ("health_observation", "/health_correlation/state"): ("health_correlation_evidence", "state"),
        ("provider_notice", "/notice/state"): ("notice_evidence", "state"),
        ("response_contract", "/contract_assertion/result"): ("contract_assertion_evidence", "result"),
        ("data_quality_assertion", "/quality_assertion/kind"): ("quality_assertion_evidence", "kind"),
        ("data_quality_assertion", "/quality_assertion/result"): ("quality_assertion_evidence", "result"),
        ("data_quality_assertion", "/freshness/state"): ("freshness_evidence", "state"),
        ("validation_result", "/validation/result"): ("validation_evidence", "result"),
    }
    for (kind, path), (definition, field) in enum_bindings.items():
        result[kind]["enums"][path] = set(defs[definition]["properties"][field]["enum"])
    return result


def validate_predicates(mapping: dict[str, Any]) -> None:
    contract = schema_contract()
    for predicate_id, predicate in mapping["evidence_predicates"].items():
        if set(predicate) != {"kind", "authorities", "scope_level", "max_age_seconds", "supports", "matches"}:
            raise ValueError(f"{predicate_id}: predicate shape drift")
        kind = predicate.get("kind")
        if kind not in contract:
            raise ValueError(f"{predicate_id}: unknown evidence kind")
        if not predicate.get("authorities") or not set(predicate["authorities"]).issubset(contract[kind]["authorities"]):
            raise ValueError(f"{predicate_id}: authority vocabulary drift")
        if predicate.get("scope_level") != contract[kind]["scope"]:
            raise ValueError(f"{predicate_id}: scope vocabulary drift")
        if not isinstance(predicate.get("max_age_seconds"), int) or predicate["max_age_seconds"] < 0:
            raise ValueError(f"{predicate_id}: bounded timing required")
        if not isinstance(predicate.get("supports"), list) or not isinstance(predicate.get("matches"), dict) or not predicate["matches"]:
            raise ValueError(f"{predicate_id}: typed supports and matches required")
        unknown = set(predicate["matches"]) - contract[kind]["allowed_match_paths"]
        if unknown:
            raise ValueError(f"{predicate_id}: unknown payload field {sorted(unknown)}")
        for path, expected in predicate["matches"].items():
            allowed_values = contract[kind]["enums"].get(path)
            if allowed_values is not None and expected not in allowed_values:
                raise ValueError(f"{predicate_id}: payload enum drift at {path}")
            if path == "/response/http_status" and (not isinstance(expected, int) or not 100 <= expected <= 599):
                raise ValueError(f"{predicate_id}: HTTP status vocabulary drift")


def validate_source_basis(mapping: dict[str, Any]) -> None:
    evidence_kinds = set(load(CONTRACT)["evidence_variants"])
    allowed_artifacts = {item["path"] for item in mapping["authoritative_inputs"]}
    for item in mapping["cause_mappings"]:
        if not item.get("source_basis"):
            raise ValueError(f"{item['cause']}: empty source basis")
        for basis in item["source_basis"]:
            if basis.get("type") not in SOURCE_BASIS_TYPES:
                raise ValueError(f"{item['cause']}: invalid source basis type")
            if basis["type"] in {"registry_rule", "registry_fact"}:
                if basis.get("artifact") not in allowed_artifacts:
                    raise ValueError(f"{item['cause']}: unpinned Registry source basis")
                artifact = ROOT / basis.get("artifact", "")
                try:
                    actual = pointer_get(load(artifact), basis["json_pointer"])
                except (OSError, KeyError, IndexError, ValueError, TypeError) as exc:
                    raise ValueError(f"{item['cause']}: unresolved Registry source basis") from exc
                if "expected" in basis and any(actual.get(key) != expected for key, expected in basis["expected"].items()):
                    raise ValueError(f"{item['cause']}: Registry rule basis mismatch")
                if "equals" in basis and actual != basis["equals"]:
                    raise ValueError(f"{item['cause']}: Registry fact basis mismatch")
            elif basis["type"] == "consumer_evidence" and basis.get("kind") not in evidence_kinds:
                raise ValueError(f"{item['cause']}: invalid consumer evidence basis")
            elif basis["type"] == "resolution_policy" and basis.get("ref") != "no_or_conflicting_specific_cause":
                raise ValueError(f"{item['cause']}: invalid resolution policy basis")


def predicate_matches(predicate: dict[str, Any], instance: dict[str, Any], subject: dict[str, Any], validator: jsonschema.Draft202012Validator) -> bool:
    if instance.get("subject") != subject:
        return False
    evidence = instance.get("evidence")
    if not isinstance(evidence, dict) or not validator.is_valid(evidence):
        return False
    timing, scope = evidence["timing"], evidence["scope"]
    if timing.get("validity") != "current_at_assessment" or timing.get("remaining_validity_seconds", 0) <= 0:
        return False
    if timing["observed_age_seconds"] > predicate["max_age_seconds"]:
        return False
    if evidence["kind"] != predicate["kind"] or evidence["authority"] not in predicate["authorities"]:
        return False
    if scope.get("level") != predicate["scope_level"] or scope.get("subject_ref") != "envelope_subject":
        return False
    if not set(predicate["supports"]).issubset(evidence["supports"]):
        return False
    try:
        matched = all(pointer_get(evidence, path) == expected for path, expected in predicate["matches"].items())
    except (KeyError, IndexError, ValueError, TypeError):
        return False
    if not matched:
        return False
    if predicate["kind"] == "validation_result" and predicate["matches"].get("/validation/result") == "passed":
        levels = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}
        validation = evidence["validation"]
        return levels[validation["achieved_level"]] >= levels[validation["required_level"]]
    return True


def matching_predicates(mapping: dict[str, Any], instances: list[dict[str, Any]], subject: dict[str, Any]) -> set[str]:
    validator = evidence_validator()
    return {
        predicate_id
        for predicate_id, predicate in mapping["evidence_predicates"].items()
        if any(predicate_matches(predicate, instance, subject, validator) for instance in instances)
    }


def resolve(mapping: dict[str, Any], subject: dict[str, Any], instances: list[dict[str, Any]]) -> dict[str, Any]:
    matched = matching_predicates(mapping, instances, subject)
    candidate_only = set(mapping["candidate_only_predicates"])
    eligible = []
    for item in mapping["cause_mappings"]:
        if item["cause"] == "unknown":
            continue
        selectors_ok = all(any(pid in matched and pid not in candidate_only for pid in group) for group in item["selector_groups"])
        corroborators_ok = all(any(pid in matched for pid in group) for group in item["corroborator_groups"])
        if selectors_ok and corroborators_ok:
            eligible.append(item)
    if len(eligible) != 1:
        return next(item["result"] | {"cause": "unknown"} for item in mapping["cause_mappings"] if item["cause"] == "unknown")
    item = eligible[0]
    result = copy.deepcopy(item["result"] | {"cause": item["cause"]})
    for variant in item.get("result_variants", []):
        if any(predicate_id in matched for predicate_id in variant["when_any"]):
            result.update({key: value for key, value in variant.items() if key != "when_any"})
            break
    return result


def proof_instances(case: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    fixture = load(FIXTURES / case["fixture"])
    subject = fixture["subject"]
    binding_subject = copy.deepcopy(subject)
    if "binding_operation_override" in case:
        binding_subject["operation_id"] = case["binding_operation_override"]
    evidence = [copy.deepcopy(item) for item in fixture["evidence_refs"] if item["kind"] in case["include_kinds"]]
    evidence.extend(copy.deepcopy(case.get("synthetic_evidence", [])))
    for mutation in case.get("mutations", []):
        target = next(item for item in evidence if item["kind"] == mutation["kind"])
        pointer_set(target, mutation["json_pointer"], mutation["value"])
    return subject, [{"subject": copy.deepcopy(binding_subject), "evidence": item} for item in evidence]


def validate_mapping(mapping: dict[str, Any], contract: dict[str, Any]) -> None:
    boundary = mapping.get("authority_boundary", {})
    if mapping.get("status") != "draft" or boundary != {
        "registry_role": "static_contract_and_deterministic_proof_only",
        "runtime_inference_owner": "consumer",
        "publishing_allowed": False,
        "runtime_evidence_storage_allowed": False,
    }:
        raise ValueError("static unpublished Registry boundary drift")
    if mapping.get("resolution_policy") != {
        "subject_binding": "exact_source_provider_dataset_operation_match",
        "invalid_evidence": "exclude",
        "candidate_only_selector": "forbidden",
        "multiple_eligible_specific_causes": "unknown",
        "no_eligible_specific_cause": "unknown",
        "unknown_determination": "unknown",
        "unknown_action": "gather_more_evidence",
    }:
        raise ValueError("resolution policy drift")
    validate_predicates(mapping)
    validate_source_basis(mapping)
    predicate_ids = set(mapping["evidence_predicates"])
    candidates = set(mapping["candidate_only_predicates"])
    if not candidates or not candidates.issubset(predicate_ids):
        raise ValueError("candidate-only predicate set drift")
    catalog = {item["code"]: item for item in contract["cause_catalog"]}
    mappings = {item["cause"]: item for item in mapping["cause_mappings"]}
    if list(mappings) != list(catalog):
        raise ValueError("cause coverage/order drift")
    fixture_results = {
        value["cause"]["code"]: {
            "determination": value["cause"]["determination"],
            "accountable_party": value["ownership"]["accountable_party"],
            "recommended_action": value["actions"]["recommended"][0]["action_id"],
            "avoid_actions": [item["action_id"] for item in value["actions"]["avoid"]],
        }
        for path in FIXTURES.glob("*.json")
        for value in [load(path)]
    }
    for cause, item in mappings.items():
        if cause != "unknown" and not item["selector_groups"]:
            raise ValueError(f"{cause}: at least one typed selector group is required")
        referenced = {pid for group in item["selector_groups"] + item["corroborator_groups"] for pid in group}
        if not referenced.issubset(predicate_ids):
            raise ValueError(f"{cause}: unknown predicate reference")
        if any(candidates & set(group) for group in item["selector_groups"]):
            raise ValueError(f"{cause}: candidate-only predicate used as selector")
        if item["result"] != fixture_results[cause]:
            if cause != "provider_outage":
                raise ValueError(f"{cause}: result contract drift")
            expected = fixture_results[cause] | {"avoid_actions": []}
            if item["result"] != expected:
                raise ValueError(f"{cause}: result contract drift")
        if item["result"]["recommended_action"] != catalog[cause]["required_action"]:
            raise ValueError(f"{cause}: action drift")
    expected_outage_variants = [
        {"when_any": ["notice_suspended", "notice_degraded"], "determination": "observed", "avoid_actions": ["reissue_credential"]},
        {"when_any": ["health_unavailable", "health_degraded"], "determination": "inferred", "avoid_actions": ["reissue_credential"]},
        {"when_any": ["service_unavailable"], "determination": "inferred", "avoid_actions": []},
    ]
    if mappings["provider_outage"].get("result_variants") != expected_outage_variants:
        raise ValueError("provider_outage result variant drift")
    for case in mapping["proof_cases"]:
        subject, instances = proof_instances(case)
        actual = resolve(mapping, subject, instances)
        if any(actual.get(key) != value for key, value in case["expected"].items()):
            raise ValueError(f"{case['case_id']}: executable proof drift")


def validate_packets(mapping_digest: str) -> None:
    schema_digest = hashlib.sha256(ENVELOPE_SCHEMA.read_bytes()).hexdigest()
    mapping = load(MAPPING)
    causes = [item["cause"] for item in mapping["cause_mappings"]]
    fixtures = [f"{cause.replace('_', '-')}.json" for cause in causes]
    action_variants = ["approval_propagating:wait_for_approval_sync:avoid_reissue_credential", "provider_outage:health_or_notice:avoid_reissue_credential", "provider_outage:response_only:no_avoid_reissue_credential", "unknown:gather_more_evidence"]
    for consumer, path in zip(EXPECTED_CONSUMERS, sorted(PACKETS.glob("*.v1.json")), strict=True):
        packet = load(path)
        if packet.get("consumer") != consumer or packet.get("status") != "draft":
            raise ValueError("consumer packet identity drift")
        if packet.get("schema_contract") != {"path": "drafts/diagnostic-envelope/datapan.diagnostic-envelope.v1.schema.json", "sha256": schema_digest}:
            raise ValueError(f"{consumer}: schema contract pin missing")
        if packet.get("mapping_contract") != {"path": "drafts/diagnostic-envelope/data-go-kr-evidence-mapping.v1.json", "sha256": mapping_digest}:
            raise ValueError(f"{consumer}: mapping digest pin drift")
        if set(packet.get("obligations", {})) != OBLIGATIONS or packet["obligations"] != EXPECTED_OBLIGATIONS:
            raise ValueError(f"{consumer}: exact obligation keys required")
        production = packet.get("production_status", {})
        if production != {"currently_proven": [], "required_after_dependencies": EXPECTED_DEPENDENCIES[consumer]}:
            raise ValueError(f"{consumer}: production proof honesty drift")
        if packet.get("producer_evidence") != EXPECTED_PRODUCERS[consumer] or packet.get("accepted_causes") != causes or packet.get("accepted_action_variants") != action_variants or packet.get("fixture_contracts") != fixtures:
            raise ValueError(f"{consumer}: exact compatibility enumerations required")
        application = packet.get("operation_application_path", {})
        if application != {"source_profile": "sources/data_go_kr.json", "json_pointer": "/references/key_request_url", "required_for_cause": "approval_required"}:
            raise ValueError(f"{consumer}: operation application path contract drift")
        if not pointer_get(load(ROOT / application["source_profile"]), application["json_pointer"]):
            raise ValueError(f"{consumer}: operation application path unresolved")


def validate_draft_boundary() -> None:
    paths = {item["path"] for item in load(ROOT / "schemas/index.json").get("schemas", [])}
    paths |= {item["path"] for item in load(ROOT / "manifest.json").get("artifacts", [])}
    if any(path.startswith("drafts/diagnostic-envelope/") for path in paths):
        raise ValueError("draft mapping must not be released")


def validate_all() -> dict[str, int]:
    mapping, contract = load(MAPPING), load(CONTRACT)
    validate_inputs(mapping)
    validate_mapping(mapping, contract)
    validate_packets(hashlib.sha256(MAPPING.read_bytes()).hexdigest())
    validate_draft_boundary()
    return {"predicates": len(mapping["evidence_predicates"]), "causes": len(mapping["cause_mappings"]), "proof_cases": len(mapping["proof_cases"]), "consumers": len(EXPECTED_CONSUMERS)}


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
