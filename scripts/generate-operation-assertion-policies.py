#!/usr/bin/env python3
"""Generate immutable operation assertions and a Registry reference model."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any


REGISTRY = pathlib.Path("data/data-go-kr.registry.json")
CATALOG = pathlib.Path("reports/health-probe-catalog.json")
VOCABULARY = pathlib.Path("policy/diagnostic-cause-action-vocabulary.v1.json")
SCHEMA = pathlib.Path("drafts/operation-assertion-policies/datapan.operation-assertion-policies.v1.schema.json")
ARTIFACT = pathlib.Path("drafts/operation-assertion-policies/operation-assertion-policies.v1.json")
PROOF = pathlib.Path("fixtures/operation-assertion-policies/datapan-health-consumer-proof.v1.json")
BUNDLE_MANIFEST = pathlib.Path("drafts/operation-assertion-policies/release-manifest.v1.json")
CANDIDATE = pathlib.Path("drafts/operation-assertion-policies/release-candidate.v1.json")


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def value_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def operation_by_alias(registry: list[dict[str, Any]], aliases: dict[str, str]) -> dict[str, Any]:
    matches = [
        operation
        for dataset in registry
        if dataset["id"] == aliases["dataset_id"]
        for operation in dataset["operations"]
        if operation["name"] == aliases["operation_name"]
        and str(operation["source"]["raw"]["operation_seq"]) == aliases["upstream_operation_seq"]
    ]
    if len(matches) != 1:
        raise ValueError(f"selector must resolve exactly once: {aliases}")
    return matches[0]


def not_asserted(reason_code: str) -> dict[str, Any]:
    return {"state": "not_asserted", "reason_code": reason_code, "evidence": None}


def build_artifact() -> dict[str, Any]:
    registry = load(REGISTRY)
    catalog = load(CATALOG)
    registry_sha = file_sha256(REGISTRY)
    operations: list[dict[str, Any]] = []
    for entry in catalog["entries"]:
        aliases = entry["aliases"]
        operation = operation_by_alias(registry, aliases)
        declared_fields = sorted({item["name"] for item in operation["response_params"] if item.get("name")})
        if not declared_fields:
            raise ValueError(f"{entry['operation_id']}: Registry has no declared response fields")
        selector = {
            "dataset_id": aliases["dataset_id"],
            "operation_name": aliases["operation_name"],
            "upstream_operation_seq": aliases["upstream_operation_seq"],
        }
        evidence = {
            "kind": "registry_operation_contract",
            "rationale_code": "registry_declares_normalized_response_field_vocabulary",
            "path": REGISTRY.as_posix(),
            "sha256": registry_sha,
            "selector": selector,
        }
        operations.append({
            "operation_id": entry["operation_id"],
            "operation_revision_sha256": value_sha256(operation),
            "dimensions": {
                "transport": not_asserted("provider_transport_expectation_not_reviewed"),
                "contract": {
                    "state": "asserted",
                    "assertion_type": "declared_response_field_vocabulary",
                    "projection_input": "normalized_leaf_field_names",
                    "declared_response_fields": declared_fields,
                    "unknown_field_policy": "fail",
                    "empty_payload_policy": "not_observed",
                    "evidence": evidence,
                },
                "presence": not_asserted("record_presence_expectation_not_reviewed"),
                "semantic": not_asserted("domain_semantics_not_reviewed"),
                "freshness": not_asserted("upstream_timestamp_contract_not_reviewed"),
            },
        })
    artifact: dict[str, Any] = {
        "schema_version": "datapan.operation-assertion-policies.v1",
        "policy_set": {
            "id": "datapan-health-canary-assertions",
            "version": 1,
            "supersedes": None,
        },
        "generated_at": catalog["generated_at"],
        "authority": "datapan-registry",
        "diagnostic_vocabulary": {
            "schema_version": "datapan.diagnostic-cause-action-vocabulary.v1",
            "path": VOCABULARY.as_posix(),
            "sha256": file_sha256(VOCABULARY),
        },
        "bindings": {
            "registry": {"path": REGISTRY.as_posix(), "sha256": registry_sha},
            "health_probe_catalog": {"path": CATALOG.as_posix(), "sha256": file_sha256(CATALOG)},
        },
        "artifact_sha256": "0" * 64,
        "operations": operations,
    }
    digest_input = dict(artifact)
    digest_input.pop("artifact_sha256")
    artifact["artifact_sha256"] = value_sha256(digest_input)
    return artifact


def build_proof(artifact: dict[str, Any]) -> dict[str, Any]:
    first = artifact["operations"][0]
    fields = first["dimensions"]["contract"]["declared_response_fields"]
    current_binding = {
        "path": ARTIFACT.as_posix(),
        "policy_set_id": artifact["policy_set"]["id"],
        "artifact_sha256": artifact["artifact_sha256"],
        "policy_set_version": artifact["policy_set"]["version"],
        "diagnostic_vocabulary_sha256": artifact["diagnostic_vocabulary"]["sha256"],
    }
    next_binding = {
        "path": "drafts/operation-assertion-policies/operation-assertion-policies.v2.json",
        "policy_set_id": artifact["policy_set"]["id"],
        "artifact_sha256": "b" * 64,
        "policy_set_version": 2,
        "diagnostic_vocabulary_sha256": artifact["diagnostic_vocabulary"]["sha256"],
    }
    return {
        "schema_version": "datapan.operation-assertion-policy-reference-model.v1",
        "proof_kind": "reference_model_only",
        "producer": "datapan-registry",
        "consumer_status": "not_executed_by_datapan_health",
        "policy_binding": current_binding,
        "supersession_transition_model": {
            "from": current_binding,
            "to": next_binding,
            "to_policy_set": {
                "version": 2,
                "supersedes": {
                    "policy_set_version": current_binding["policy_set_version"],
                    "artifact_sha256": current_binding["artifact_sha256"],
                },
            },
        },
        "projection_contract": {
            "asserted_match": "pass",
            "asserted_mismatch": "fail",
            "not_asserted": "not_observed",
            "invalid_or_stale_binding": "unknown",
        },
        "cases": [
            {
                "name": "evidence_backed_contract_match",
                "operation_id": first["operation_id"],
                "dimension": "contract",
                "observation": {"response_fields": [fields[0]]},
                "expected": "pass",
            },
            {
                "name": "evidence_backed_contract_mismatch",
                "operation_id": first["operation_id"],
                "dimension": "contract",
                "observation": {"response_fields": ["__undeclared_field__"]},
                "expected": "fail",
            },
            {
                "name": "semantic_has_no_registry_expectation",
                "operation_id": first["operation_id"],
                "dimension": "semantic",
                "observation": {},
                "expected": "not_observed",
            },
            {
                "name": "stale_policy_binding_is_not_a_health_failure",
                "operation_id": first["operation_id"],
                "dimension": "contract",
                "policy_binding_override": {"artifact_sha256": "f" * 64},
                "observation": {"response_fields": [fields[0]]},
                "expected": "unknown",
            },
            {
                "name": "unsupported_policy_version_is_not_a_health_failure",
                "operation_id": first["operation_id"],
                "dimension": "contract",
                "policy_binding_override": {"policy_set_version": 2},
                "observation": {"response_fields": [fields[0]]},
                "expected": "unknown",
            },
            {
                "name": "superseded_old_policy_pin_is_unknown",
                "operation_id": first["operation_id"],
                "dimension": "contract",
                "policy_binding": current_binding,
                "active_policy_binding": next_binding,
                "observation": {"response_fields": [fields[0]]},
                "expected": "unknown",
            },
            {
                "name": "diagnostic_vocabulary_mismatch_is_not_a_health_failure",
                "operation_id": first["operation_id"],
                "dimension": "contract",
                "policy_binding_override": {"diagnostic_vocabulary_sha256": "e" * 64},
                "observation": {"response_fields": [fields[0]]},
                "expected": "unknown",
            },
        ],
    }


def rendered_sha256(value: Any) -> str:
    return hashlib.sha256(render(value).encode("utf-8")).hexdigest()


def build_bundle_manifest(artifact: dict[str, Any], proof: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "datapan.operation-assertion-policy-candidate-manifest.v1",
        "publication_status": "unreleased_candidate",
        "artifact_count": 3,
        "artifacts": [
            {"path": SCHEMA.as_posix(), "kind": "schema", "sha256": file_sha256(SCHEMA)},
            {"path": ARTIFACT.as_posix(), "kind": "operation_assertion_policy", "sha256": rendered_sha256(artifact)},
            {"path": PROOF.as_posix(), "kind": "registry_reference_model", "sha256": rendered_sha256(proof)},
        ],
    }


def build_candidate(artifact: dict[str, Any], proof: dict[str, Any], bundle_manifest: dict[str, Any]) -> dict[str, Any]:
    bindings = []
    for path in (SCHEMA, ARTIFACT, PROOF, BUNDLE_MANIFEST):
        if path == ARTIFACT:
            sha = rendered_sha256(artifact)
        elif path == PROOF:
            sha = rendered_sha256(proof)
        elif path == BUNDLE_MANIFEST:
            sha = rendered_sha256(bundle_manifest)
        else:
            sha = file_sha256(path)
        bindings.append({"path": path.as_posix(), "sha256": sha})
    return {
        "schema_version": "datapan.operation-assertion-policy-release-candidate.v1",
        "status": "ready_for_health_implementation_review",
        "authority": {"release": False, "runtime": False, "publishing": False},
        "policy_set": artifact["policy_set"],
        "artifact_sha256": artifact["artifact_sha256"],
        "bindings": bindings,
        "reference_model": {
            "kind": proof["proof_kind"],
            "producer": proof["producer"],
            "schema_version": proof["schema_version"],
            "cases": len(proof["cases"]),
        },
        "next_gate": "datapan_health_exact_revision_compatibility_proof",
    }


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
        artifact = build_artifact()
        proof = build_proof(artifact)
        bundle_manifest = build_bundle_manifest(artifact, proof)
        candidate = build_candidate(artifact, proof, bundle_manifest)
        write_or_check(ARTIFACT, artifact, args.check)
        write_or_check(PROOF, proof, args.check)
        write_or_check(BUNDLE_MANIFEST, bundle_manifest, args.check)
        write_or_check(CANDIDATE, candidate, args.check)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL generate operation assertion policies: {exc}", file=sys.stderr)
        return 1
    print(f"ok operation assertion policy generation (operations={len(artifact['operations'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
