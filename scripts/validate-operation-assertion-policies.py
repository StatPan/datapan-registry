#!/usr/bin/env python3
"""Validate immutable operation assertion policies and Health projection proof."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "scripts/generate-operation-assertion-policies.py"
SPEC = importlib.util.spec_from_file_location("operation_assertion_generator", GENERATOR_PATH)
GENERATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(GENERATOR)

SECRET_KEYS = {"servicekey", "service_key", "api_key", "apikey", "authorization", "credential", "token", "password", "query_value"}
PRIVATE_KEYS = {"response", "response_rows", "sample_rows", "request_url", "query", "headers", "cookies", "user_id", "email"}
SECRET_VALUE = re.compile(r"(?i)(bearer\s+[a-z0-9._~+/-]{8,}|(?:servicekey|api[_-]?key|token|password)\s*[=:]\s*\S+)")


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reject_leaks(value: Any, path: str = "artifact") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = key.lower()
            fail(lowered not in SECRET_KEYS, f"{path}: forbidden secret field {key!r}")
            fail(lowered not in PRIVATE_KEYS, f"{path}: forbidden private/runtime field {key!r}")
            reject_leaks(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_leaks(child, f"{path}[{index}]")
    elif isinstance(value, str):
        fail(SECRET_VALUE.search(value) is None, f"{path}: secret-shaped value")
        parsed = urlsplit(value)
        fail(not (parsed.scheme and parsed.query), f"{path}: URL query values are forbidden")


def freshness_result(assertion: dict[str, Any], observed: str | None, now: datetime) -> str:
    if assertion["state"] == "not_asserted":
        return "not_observed"
    if (
        assertion.get("actual_time_source") != "health_observed_at"
        or assertion.get("reference_time_source") != "response_field"
        or assertion.get("maximum_age_boundary") != "inclusive"
    ):
        return "unknown"
    if observed is None:
        return assertion["empty_result_policy"]
    if assertion["calendar"] != "gregorian":
        return "unknown"
    try:
        value = datetime.strptime(observed, assertion["timestamp_format"])
    except ValueError:
        return "fail"
    if assertion["timezone"] == "UTC":
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    else:
        return "unknown"
    age = (now.astimezone(timezone.utc) - value).total_seconds()
    if age < -assertion["future_tolerance_seconds"]:
        return "fail"
    return "pass" if age <= assertion["maximum_age_seconds"] else "fail"


def project_case(artifact: dict[str, Any], proof: dict[str, Any], case: dict[str, Any]) -> str:
    binding = dict(proof["policy_binding"])
    binding.update(case.get("policy_binding_override", {}))
    if (
        binding.get("artifact_sha256") != artifact["artifact_sha256"]
        or binding.get("policy_set_version") != artifact["policy_set"]["version"]
        or binding.get("diagnostic_vocabulary_sha256") != artifact["diagnostic_vocabulary"]["sha256"]
    ):
        return "unknown"
    entries = [item for item in artifact["operations"] if item["operation_id"] == case["operation_id"]]
    if len(entries) != 1 or case["dimension"] not in entries[0]["dimensions"]:
        return "unknown"
    assertion = entries[0]["dimensions"][case["dimension"]]
    if assertion["state"] == "not_asserted":
        return "not_observed"
    if case["dimension"] == "contract":
        observed = case["observation"].get("response_fields", [])
        if not observed:
            return assertion["empty_payload_policy"]
        return "pass" if set(observed).issubset(assertion["declared_response_fields"]) else "fail"
    return "unknown"


def validate_all(
    artifact: dict[str, Any],
    proof: dict[str, Any],
    bundle_manifest: dict[str, Any],
    candidate: dict[str, Any],
    schema: dict[str, Any],
    catalog: dict[str, Any],
    registry: list[dict[str, Any]],
) -> None:
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(artifact)
    reject_leaks(artifact)

    version = artifact["policy_set"]["version"]
    supersedes = artifact["policy_set"]["supersedes_sha256"]
    fail(version == 1, f"unsupported policy set version: {version}")
    fail(supersedes is None, "first policy set version must not supersede another artifact")
    digest_input = dict(artifact)
    claimed_digest = digest_input.pop("artifact_sha256")
    fail(GENERATOR.value_sha256(digest_input) == claimed_digest, "artifact canonical digest mismatch")
    fail(artifact == GENERATOR.build_artifact(), "artifact is stale or not deterministically generated")

    for name, binding in artifact["bindings"].items():
        path = ROOT / binding["path"]
        fail(path.is_file() and file_sha256(path) == binding["sha256"], f"{name} binding mismatch")
    vocabulary = artifact["diagnostic_vocabulary"]
    fail(file_sha256(ROOT / vocabulary["path"]) == vocabulary["sha256"], "diagnostic vocabulary binding mismatch")

    expected_ids = [item["operation_id"] for item in catalog["entries"]]
    actual_ids = [item["operation_id"] for item in artifact["operations"]]
    fail(actual_ids == expected_ids and len(set(actual_ids)) == 10, "policies must exactly cover the ten ordered canaries")
    registry_sha = artifact["bindings"]["registry"]["sha256"]
    for entry, catalog_entry in zip(artifact["operations"], catalog["entries"], strict=True):
        operation = GENERATOR.operation_by_alias(registry, catalog_entry["aliases"])
        fail(GENERATOR.value_sha256(operation) == entry["operation_revision_sha256"], f"{entry['operation_id']}: operation revision mismatch")
        dimensions = entry["dimensions"]
        fail(dimensions["contract"]["state"] == "asserted", f"{entry['operation_id']}: contract evidence unexpectedly absent")
        declared = sorted({item["name"] for item in operation["response_params"] if item.get("name")})
        fail(dimensions["contract"]["declared_response_fields"] == declared, f"{entry['operation_id']}: declared response fields drift")
        fail(dimensions["contract"]["evidence"]["sha256"] == registry_sha, f"{entry['operation_id']}: evidence revision mismatch")
        reasons = {
            "transport": "provider_transport_expectation_not_reviewed",
            "presence": "record_presence_expectation_not_reviewed",
            "semantic": "domain_semantics_not_reviewed",
            "freshness": "upstream_timestamp_contract_not_reviewed",
        }
        for dimension, reason in reasons.items():
            fail(dimensions[dimension]["state"] == "not_asserted", f"{entry['operation_id']}: {dimension} lacks reviewed Registry evidence")
            fail(dimensions[dimension]["reason_code"] == reason, f"{entry['operation_id']}: {dimension} not_asserted reason mismatch")

    reject_leaks(proof, "proof")
    fail(proof == GENERATOR.build_proof(artifact), "Health consumer proof is stale")
    for case in proof["cases"]:
        fail(project_case(artifact, proof, case) == case["expected"], f"Health projection mismatch: {case['name']}")

    fail(bundle_manifest == GENERATOR.build_bundle_manifest(artifact, proof), "candidate manifest is stale")
    fail(bundle_manifest["publication_status"] == "unreleased_candidate", "candidate manifest must remain unreleased")
    fail(bundle_manifest["artifact_count"] == len(bundle_manifest["artifacts"]) == 3, "candidate manifest artifact count mismatch")
    for item in bundle_manifest["artifacts"]:
        fail(file_sha256(ROOT / item["path"]) == item["sha256"], f"candidate manifest digest mismatch: {item['path']}")

    fail(candidate == GENERATOR.build_candidate(artifact, proof, bundle_manifest), "release candidate binding is stale")
    fail(candidate["status"] == "ready_for_consumer_review", "release candidate is not ready for review")
    fail(not any(candidate["authority"].values()), "release candidate must not grant release, runtime, or publishing authority")
    candidate_bindings = {item["path"]: item["sha256"] for item in candidate["bindings"]}
    for path in (GENERATOR.SCHEMA, GENERATOR.ARTIFACT, GENERATOR.PROOF, GENERATOR.BUNDLE_MANIFEST):
        fail(candidate_bindings.get(path.as_posix()) == file_sha256(ROOT / path), f"release candidate does not bind {path}")


def main() -> int:
    try:
        validate_all(
            load(ROOT / GENERATOR.ARTIFACT),
            load(ROOT / GENERATOR.PROOF),
            load(ROOT / GENERATOR.BUNDLE_MANIFEST),
            load(ROOT / GENERATOR.CANDIDATE),
            load(ROOT / GENERATOR.SCHEMA),
            load(ROOT / GENERATOR.CATALOG),
            load(ROOT / GENERATOR.REGISTRY),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL operation assertion policies: {exc}", file=sys.stderr)
        return 1
    print("ok operation assertion policies (operations=10, asserted=10, intentional_not_asserted=40, missing=0, health_projection=6, publishing=false)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
