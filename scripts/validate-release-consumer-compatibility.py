#!/usr/bin/env python3
"""Validate datapan-registry release consumer compatibility evidence."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import re
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running release consumer compatibility validation"
    ) from exc


CANONICAL_REGISTRY_PATH = "data/data-go-kr.registry.json"
COMPATIBILITY_REPORT_PATH = "reports/release-consumer-compatibility.json"
COMPATIBILITY_SCHEMA = "schemas/datapan.release-consumer-compatibility.v1.schema.json"
DEFAULT_SOURCE_RUNTIME_ROLLUP = pathlib.Path("reports/source-runtime-evidence-rollup.json")
DEFAULT_SOURCE_RUNTIME_REMEDIATION = pathlib.Path("reports/source-runtime-remediation-map.json")
DEFAULT_CREDENTIAL_RUNTIME_POLICY = pathlib.Path("reports/credential-runtime-evidence-policy.json")
DEFAULT_CREDENTIAL_COLLECTION_PREFLIGHT = pathlib.Path("reports/credential-runtime-collection-preflight.json")
DEFAULT_CREDENTIAL_RUNNER_READINESS = pathlib.Path("reports/credential-runtime-runner-readiness.json")
DEFAULT_CREDENTIAL_RECEIPT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_CREDENTIAL_REVIEW_HANDOFF = pathlib.Path("reports/credential-runtime-review-handoff.json")
DEFAULT_CREDENTIAL_MANUAL_REVIEW_DECISION = pathlib.Path("reports/credential-runtime-manual-review-decision.json")
DEFAULT_CREDENTIAL_MANUAL_REVIEW_ACCEPTANCE = pathlib.Path(
    "reports/credential-runtime-manual-review-acceptance.json"
)
DEFAULT_ERROR_ACTION_ROUTING_ROLLUP = pathlib.Path("reports/error-action-routing-rollup.json")
DEFAULT_IMPACT_ROLLUP = pathlib.Path("reports/registry-impact-plan.json")
DEFAULT_RELEASE_DISTRIBUTION_FOOTPRINT = pathlib.Path("reports/release-distribution-footprint.json")
DEFAULT_SHARD_CONSUMER_PROOF = pathlib.Path("reports/release-shard-consumer-proof.json")
REQUIRED_RUNTIME_RISK_CONTRACTS = [
    "source_runtime_evidence",
    "source_runtime_remediation",
    "credential_runtime_evidence_policy",
    "credential_runtime_collection_preflight",
    "credential_runtime_runner_readiness",
    "credential_runtime_receipt_collection_queue",
    "credential_runtime_review_handoff",
    "credential_runtime_manual_review_decision",
    "credential_runtime_manual_review_acceptance",
    "error_action_routing",
    "downstream_impact",
]
REQUIRED_RELEASE_HEALTH_SCHEMAS = {
    "schemas/datapan.install-smoke-summary.v1.schema.json",
    "schemas/datapan.doctor-smoke-summary.v1.schema.json",
    "schemas/datapan.release-health-rollup.v1.schema.json",
}
REQUIRED_CI_REPORTS = {
    ".datapan/ci/current-release-install-smoke.json",
    ".datapan/ci/current-release-doctor-smoke.json",
    ".datapan/ci/latest-release-install-smoke.json",
    ".datapan/ci/latest-release-doctor-smoke.json",
    ".datapan/ci/release-health-rollup.json",
}
EXPECTED_ROLLUP_GENERATION_CONTRACT = {
    "generator": "scripts/generate-release-health-rollup.py",
    "validator": "scripts/validate-release-health-rollups.py",
    "schema": "schemas/datapan.release-health-rollup.v1.schema.json",
    "output": ".datapan/ci/release-health-rollup.json",
    "inputs": [
        {
            "scope": "current",
            "install_summary": ".datapan/ci/current-release-install-smoke.json",
            "doctor_summary": ".datapan/ci/current-release-doctor-smoke.json",
        },
        {
            "scope": "latest",
            "install_summary": ".datapan/ci/latest-release-install-smoke.json",
            "doctor_summary": ".datapan/ci/latest-release-doctor-smoke.json",
        },
    ],
}
REQUIRED_SHARD_INSTALL_FIELDS = {
    "mode",
    "shards_asset_present",
    "shards_validated",
    "shards_inventory_present",
    "shards_count",
    "shards_records",
}
REQUIRED_SHARD_RELEASE_EVIDENCE = {
    "status": "ci_validated_optional_asset",
    "workflow": ".github/workflows/verify-release.yml",
    "gate_name": "Validate full registry shard release evidence",
    "source_registry": CANONICAL_REGISTRY_PATH,
    "generated_inventory": ".datapan/ci/full-registry-shards/registry-shards.json",
    "generated_archive": ".datapan/ci/full-data-go-kr-shards.tar.gz",
    "archive_check": ".datapan/ci/full-shard-archive-check.txt",
}
REQUIRED_SHARD_RELEASE_COMMANDS = {
    "python scripts/generate-registry-shards.py",
    "python scripts/validate-registry-shards.py",
    "python scripts/package-registry-shards.py",
    "python scripts/package-registry-shards.py --check",
}
REQUIRED_MANIFEST_EVIDENCE_CONTRACTS = {
    "source_contracts": {
        "path": "reports/source-contract-rollup.json",
        "kind": "source_contract_rollup",
        "schema": "https://schemas.datapan.dev/datapan.source-contract-rollup.v1.schema.json",
    },
    "source_runtime_evidence": {
        "path": "reports/source-runtime-evidence-rollup.json",
        "kind": "source_runtime_evidence_rollup",
        "schema": "https://schemas.datapan.dev/datapan.source-runtime-evidence-rollup.v1.schema.json",
    },
    "source_runtime_remediation": {
        "path": "reports/source-runtime-remediation-map.json",
        "kind": "source_runtime_remediation_map",
        "schema": "https://schemas.datapan.dev/datapan.source-runtime-remediation-map.v1.schema.json",
    },
    "credential_runtime_evidence_policy": {
        "path": "reports/credential-runtime-evidence-policy.json",
        "kind": "credential_runtime_evidence_policy",
        "schema": "https://schemas.datapan.dev/datapan.credential-runtime-evidence-policy.v1.schema.json",
    },
    "credential_runtime_collection_preflight": {
        "path": "reports/credential-runtime-collection-preflight.json",
        "kind": "credential_runtime_collection_preflight",
        "schema": "https://schemas.datapan.dev/datapan.credential-runtime-collection-preflight.v1.schema.json",
    },
    "credential_runtime_runner_readiness": {
        "path": "reports/credential-runtime-runner-readiness.json",
        "kind": "credential_runtime_runner_readiness",
        "schema": "https://schemas.datapan.dev/datapan.credential-runtime-runner-readiness.v1.schema.json",
    },
    "credential_runtime_receipt_collection_queue": {
        "path": "reports/credential-runtime-receipt-collection-queue.json",
        "kind": "credential_runtime_receipt_collection_queue",
        "schema": "https://schemas.datapan.dev/datapan.credential-runtime-receipt-collection-queue.v1.schema.json",
    },
    "credential_runtime_review_handoff": {
        "path": "reports/credential-runtime-review-handoff.json",
        "kind": "credential_runtime_review_handoff",
        "schema": "https://schemas.datapan.dev/datapan.credential-runtime-review-handoff.v1.schema.json",
    },
    "credential_runtime_manual_review_decision": {
        "path": "reports/credential-runtime-manual-review-decision.json",
        "kind": "credential_runtime_manual_review_decision",
        "schema": "https://schemas.datapan.dev/datapan.credential-runtime-manual-review-decision.v1.schema.json",
    },
    "credential_runtime_manual_review_acceptance": {
        "path": "reports/credential-runtime-manual-review-acceptance.json",
        "kind": "credential_runtime_manual_review_acceptance",
        "schema": "https://schemas.datapan.dev/datapan.credential-runtime-manual-review-acceptance.v1.schema.json",
    },
    "error_action_routing": {
        "path": "reports/error-action-routing-rollup.json",
        "kind": "error_action_routing_rollup",
        "schema": "https://schemas.datapan.dev/datapan.error-action-routing-rollup.v1.schema.json",
    },
    "downstream_impact": {
        "path": "reports/registry-impact-plan.json",
        "kind": "registry_impact_plan",
        "schema": "https://schemas.datapan.dev/datapan.registry-impact-plan.v1.schema.json",
    },
    "source_reference_drift": {
        "path": "reports/source-reference-drift.json",
        "kind": "source_reference_drift",
        "schema": "https://schemas.datapan.dev/datapan.source-reference-drift.v1.schema.json",
    },
    "source_report_inventory": {
        "path": "reports/source-report-inventory.json",
        "kind": "source_report_inventory",
        "schema": "https://schemas.datapan.dev/datapan.source-report-inventory.v1.schema.json",
    },
    "release_distribution_footprint": {
        "path": "reports/release-distribution-footprint.json",
        "kind": "release_distribution_footprint",
        "schema": "https://schemas.datapan.dev/datapan.release-distribution-footprint.v1.schema.json",
    },
    "release_shard_consumer_proof": {
        "path": "reports/release-shard-consumer-proof.json",
        "kind": "release_shard_consumer_proof",
        "schema": "https://schemas.datapan.dev/datapan.release-shard-consumer-proof.v1.schema.json",
    },
    "release_consumer_decision": {
        "path": "reports/release-consumer-decision.json",
        "kind": "release_consumer_decision",
        "schema": "https://schemas.datapan.dev/datapan.release-consumer-decision.v1.schema.json",
    },
}
REQUIRED_CLI_SURFACES = {
    "catalog install",
    "doctor",
    "catalog release verify",
    "catalog release readiness",
}


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalized_readiness_fingerprint(readiness: dict[str, Any]) -> str:
    normalized = dict(readiness)
    normalized.pop("generated_at", None)
    data = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def manifest_artifacts(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for index, artifact in enumerate(as_list(manifest.get("artifacts"), "manifest.artifacts")):
        item = as_dict(artifact, f"manifest.artifacts[{index}]")
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"manifest.artifacts[{index}].path must be a non-empty string")
        artifacts[path] = item
    return artifacts


def readiness_gate(readiness: dict[str, Any], gate_id: str) -> dict[str, Any]:
    for index, gate in enumerate(as_list(readiness.get("gates"), "readiness.gates")):
        item = as_dict(gate, f"readiness.gates[{index}]")
        if item.get("id") == gate_id:
            return item
    raise ValueError(f"missing readiness gate: {gate_id}")


def validate_summary(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    consumers = [as_dict(item, "consumer") for item in as_list(report.get("consumers"), "consumers")]
    status_counts: collections.Counter[str] = collections.Counter()
    for consumer in consumers:
        status = consumer.get("status")
        if isinstance(status, str):
            status_counts[status] += 1

    if summary.get("consumer_count") != len(consumers):
        raise ValueError("summary.consumer_count must match consumers length")
    if summary.get("proven_consumers") != status_counts["proven"]:
        raise ValueError("summary.proven_consumers must match proven consumer count")
    if summary.get("blocked_consumers") != status_counts["blocked"]:
        raise ValueError("summary.blocked_consumers must match blocked consumer count")
    if summary.get("canonical_registry_required") is not True:
        raise ValueError("canonical registry must remain required")
    if summary.get("shard_assets_required") is not False:
        raise ValueError("shard assets must not be required during the compatibility period")


def validate_manifest_links(report: dict[str, Any], manifest: dict[str, Any]) -> None:
    artifacts = manifest_artifacts(manifest)

    compatibility_path = as_dict(report.get("compatibility_path"), "compatibility_path")
    registry_path = compatibility_path.get("path")
    if registry_path != CANONICAL_REGISTRY_PATH:
        raise ValueError(f"compatibility_path.path must be {CANONICAL_REGISTRY_PATH}")
    registry_artifact = artifacts.get(CANONICAL_REGISTRY_PATH)
    if not registry_artifact:
        raise ValueError(f"manifest missing canonical registry artifact: {CANONICAL_REGISTRY_PATH}")
    if registry_artifact.get("kind") != "registry":
        raise ValueError("canonical registry manifest artifact must have kind=registry")
    if compatibility_path.get("status") != "manifest_bound":
        raise ValueError("compatibility_path.status must be manifest_bound")

    report_artifact = artifacts.get(COMPATIBILITY_REPORT_PATH)
    if not report_artifact:
        raise ValueError(f"manifest missing compatibility report artifact: {COMPATIBILITY_REPORT_PATH}")
    if report_artifact.get("kind") != "consumer_compatibility":
        raise ValueError("compatibility report manifest artifact must have kind=consumer_compatibility")
    if report_artifact.get("schema") != "https://schemas.datapan.dev/datapan.release-consumer-compatibility.v1.schema.json":
        raise ValueError("compatibility report manifest artifact must point to the compatibility schema")

    schema_artifact = artifacts.get(COMPATIBILITY_SCHEMA)
    if not schema_artifact or schema_artifact.get("kind") != "schema":
        raise ValueError(f"manifest missing compatibility schema artifact: {COMPATIBILITY_SCHEMA}")

    evidence = as_dict(report.get("release_health_evidence"), "release_health_evidence")
    required_schemas = set(as_list(evidence.get("required_schemas"), "release_health_evidence.required_schemas"))
    if not REQUIRED_RELEASE_HEALTH_SCHEMAS.issubset(required_schemas):
        missing = sorted(REQUIRED_RELEASE_HEALTH_SCHEMAS.difference(required_schemas))
        raise ValueError(f"release health evidence missing required schemas: {', '.join(missing)}")
    for schema_path in sorted(REQUIRED_RELEASE_HEALTH_SCHEMAS):
        artifact = artifacts.get(schema_path)
        if not artifact or artifact.get("kind") != "schema":
            raise ValueError(f"manifest missing required release-health schema artifact: {schema_path}")

    required_ci_reports = set(as_list(evidence.get("required_ci_reports"), "release_health_evidence.required_ci_reports"))
    if not REQUIRED_CI_REPORTS.issubset(required_ci_reports):
        missing = sorted(REQUIRED_CI_REPORTS.difference(required_ci_reports))
        raise ValueError(f"release health evidence missing required CI reports: {', '.join(missing)}")

    required_shard_fields = set(
        as_list(evidence.get("required_shard_install_fields"), "release_health_evidence.required_shard_install_fields")
    )
    if not REQUIRED_SHARD_INSTALL_FIELDS.issubset(required_shard_fields):
        missing = sorted(REQUIRED_SHARD_INSTALL_FIELDS.difference(required_shard_fields))
        raise ValueError(f"release health evidence missing required shard install fields: {', '.join(missing)}")


def workflow_fragment(path: str) -> str:
    return f"../datapan-registry/{path}"


def validate_rollup_generation_contract(report: dict[str, Any]) -> None:
    evidence = as_dict(report.get("release_health_evidence"), "release_health_evidence")
    contract = as_dict(
        evidence.get("rollup_generation_contract"),
        "release_health_evidence.rollup_generation_contract",
    )

    for key in ("generator", "validator", "schema", "output"):
        expected = EXPECTED_ROLLUP_GENERATION_CONTRACT[key]
        if contract.get(key) != expected:
            raise ValueError(
                f"release_health_evidence.rollup_generation_contract.{key} "
                f"expected {expected}, got {contract.get(key)}"
            )

    inputs = [
        as_dict(item, "rollup_generation_contract.input")
        for item in as_list(contract.get("inputs"), "rollup_generation_contract.inputs")
    ]
    if inputs != EXPECTED_ROLLUP_GENERATION_CONTRACT["inputs"]:
        raise ValueError(
            "release_health_evidence.rollup_generation_contract.inputs must match "
            "expected current/latest smoke summaries"
        )

    workflow_path = pathlib.Path(".github/workflows/verify-release.yml")
    if not workflow_path.is_file():
        raise ValueError(f"release-health workflow is missing: {workflow_path}")
    workflow = workflow_path.read_text(encoding="utf-8")
    normalized_workflow = re.sub(r"\\\n\s*", " ", workflow)
    normalized_workflow = re.sub(r"\s+", " ", normalized_workflow)

    required_fragments = {
        contract["generator"],
        contract["validator"],
        workflow_fragment(str(contract["schema"])),
        workflow_fragment(str(contract["output"])),
        "--output " + workflow_fragment(str(contract["output"])),
    }
    for item in inputs:
        required_fragments.update(
            {
                "--" + item["scope"] + "-install " + workflow_fragment(str(item["install_summary"])),
                "--" + item["scope"] + "-doctor " + workflow_fragment(str(item["doctor_summary"])),
            }
        )

    missing_fragments = sorted(fragment for fragment in required_fragments if fragment not in normalized_workflow)
    if missing_fragments:
        raise ValueError(
            "release-health rollup workflow is missing required fragments: "
            + ", ".join(missing_fragments)
        )


def validate_generation_inputs(
    report: dict[str, Any],
    manifest: dict[str, Any],
    readiness: dict[str, Any],
    manifest_path: pathlib.Path,
    readiness_path: pathlib.Path,
) -> None:
    inputs = as_dict(report.get("generation_inputs"), "generation_inputs")
    manifest_input = as_dict(inputs.get("manifest"), "generation_inputs.manifest")
    readiness_input = as_dict(inputs.get("readiness"), "generation_inputs.readiness")

    expected_manifest = {
        "path": manifest_path.as_posix(),
        "generated_at": manifest.get("generated_at"),
        "artifact_count": manifest.get("artifact_count"),
        "evidence_contracts": len(REQUIRED_MANIFEST_EVIDENCE_CONTRACTS),
    }
    for key, value in expected_manifest.items():
        if manifest_input.get(key) != value:
            raise ValueError(
                f"generation_inputs.manifest.{key} expected {value}, got {manifest_input.get(key)}"
            )

    readiness_path_value = readiness_input.get("path")
    if readiness_path_value != readiness_path.as_posix():
        raise ValueError(
            f"generation_inputs.readiness.path expected {readiness_path.as_posix()}, got {readiness_path_value}"
        )
    readiness_summary = as_dict(readiness.get("summary"), "readiness.summary")
    expected_readiness = {
        "normalization": "omit_generated_at",
        "normalized_sha256": normalized_readiness_fingerprint(readiness),
        "ready": readiness.get("ready"),
        "gates_total": readiness_summary.get("gates_total"),
        "passed": readiness_summary.get("passed"),
        "failed": readiness_summary.get("failed"),
    }
    for key, value in expected_readiness.items():
        if readiness_input.get(key) != value:
            raise ValueError(
                f"generation_inputs.readiness.{key} expected {value}, got {readiness_input.get(key)}"
            )


def validate_readiness(report: dict[str, Any], readiness: dict[str, Any]) -> None:
    manifest_gate = readiness_gate(readiness, "manifest_verified")
    if manifest_gate.get("status") != "pass":
        raise ValueError("manifest_verified readiness gate must pass")
    registry_gate = readiness_gate(readiness, "registry_has_specs")
    if registry_gate.get("status") != "pass":
        raise ValueError("registry_has_specs readiness gate must pass")
    if registry_gate.get("artifact_path") != CANONICAL_REGISTRY_PATH:
        raise ValueError("registry_has_specs must validate the canonical compatibility registry")
    actual = registry_gate.get("actual")
    if not isinstance(actual, int) or actual <= 0:
        raise ValueError("registry_has_specs.actual must be a positive spec count")

    proof = as_list(as_dict(report.get("compatibility_path"), "compatibility_path").get("proof"), "compatibility_path.proof")
    if "reports/latest-release-readiness.json registry_has_specs gate" not in proof:
        raise ValueError("compatibility proof must cite the registry_has_specs readiness gate")


def validate_shard_policy(report: dict[str, Any]) -> None:
    shard_policy = as_dict(report.get("shard_policy"), "shard_policy")
    if shard_policy.get("phase") != "compatibility_period":
        raise ValueError("shard_policy.phase must remain compatibility_period")
    if shard_policy.get("required_for_release") is not False:
        raise ValueError("shard assets must remain optional in the current release policy")
    if shard_policy.get("monolith_fallback_required") is not True:
        raise ValueError("shard policy must require monolith fallback")
    if shard_policy.get("downstream_tracking") != "StatPan/datapan-cli#128":
        raise ValueError("shard policy must point at the downstream CLI fallback tracking issue")
    blocked_until = set(as_list(shard_policy.get("blocked_until"), "shard_policy.blocked_until"))
    if not any("monolith fallback" in str(item) for item in blocked_until):
        raise ValueError("shard policy must be blocked on proven monolith fallback")


def validate_shard_consumer_proof(report: dict[str, Any], proof: dict[str, Any]) -> None:
    shard_proof = as_dict(report.get("shard_consumer_proof"), "shard_consumer_proof")
    proof_summary = as_dict(proof.get("summary"), "proof.summary")
    proof_boundary = as_dict(proof.get("registry_boundary"), "proof.registry_boundary")
    proof_workflow = as_dict(proof.get("workflow_proof"), "proof.workflow_proof")
    proof_policy = as_dict(proof.get("release_policy"), "proof.release_policy")

    expected = {
        "path": DEFAULT_SHARD_CONSUMER_PROOF.as_posix(),
        "proof_status": proof_summary.get("proof_status"),
        "shard_preferred_ready": proof_summary.get("shard_preferred_ready"),
        "monolith_fallback_proven": proof_summary.get("monolith_fallback_proven"),
        "distribution_action_resolved": proof_summary.get("distribution_action_resolved"),
        "canonical_registry_required": True,
        "shard_assets_required": False,
        "checked_in_large_shards": False,
        "canonical_registry_bytes": proof_boundary.get("canonical_registry_bytes"),
        "shard_archive_publication": proof_boundary.get("shard_archive_publication"),
        "workflow_contract_present": proof_workflow.get("workflow_contract_present"),
        "consumer_effect": proof_policy.get("consumer_effect"),
        "goal_completion_effect": proof_policy.get("goal_completion_effect"),
    }
    for key, value in expected.items():
        if shard_proof.get(key) != value:
            raise ValueError(f"shard_consumer_proof.{key} expected {value}, got {shard_proof.get(key)}")

    if shard_proof.get("distribution_action_resolved") is True:
        if shard_proof.get("consumer_effect") != "shard_preferred_supported_with_canonical_fallback":
            raise ValueError("resolved shard consumer proof must expose shard-preferred fallback effect")
        cli = next(
            (
                as_dict(item, "consumer")
                for item in as_list(report.get("consumers"), "consumers")
                if isinstance(item, dict) and item.get("consumer") == "datapan-cli"
            ),
            None,
        )
        if cli is None:
            raise ValueError("missing datapan-cli consumer")
        if cli.get("compatibility_mode") != "shard_preferred_with_monolith_fallback":
            raise ValueError("datapan-cli must expose shard-preferred fallback compatibility when proof is resolved")
        if DEFAULT_SHARD_CONSUMER_PROOF.as_posix() not in as_list(cli.get("evidence"), "datapan-cli.evidence"):
            raise ValueError("datapan-cli evidence must include release shard consumer proof")


def validate_shard_release_evidence(report: dict[str, Any]) -> None:
    evidence = as_dict(report.get("shard_release_evidence"), "shard_release_evidence")
    for key, value in REQUIRED_SHARD_RELEASE_EVIDENCE.items():
        if evidence.get(key) != value:
            raise ValueError(f"shard_release_evidence.{key} expected {value}, got {evidence.get(key)}")

    commands = set(as_list(evidence.get("required_commands"), "shard_release_evidence.required_commands"))
    missing_commands = sorted(REQUIRED_SHARD_RELEASE_COMMANDS.difference(commands))
    if missing_commands:
        raise ValueError(f"shard release evidence missing required commands: {', '.join(missing_commands)}")

    expected_footprint_values = {
        "release_distribution_footprint": DEFAULT_RELEASE_DISTRIBUTION_FOOTPRINT.as_posix(),
        "large_monolith_threshold_bytes": 100000000,
        "registry_footprint_status": "large_monolith_shard_additive",
        "canonical_registry_required": True,
        "shard_distribution_required": False,
        "monolith_fallback_required": True,
        "footprint_consumer_effect": "canonical_registry_required_shards_optional",
    }
    for key, value in expected_footprint_values.items():
        if evidence.get(key) != value:
            raise ValueError(f"shard_release_evidence.{key} expected {value}, got {evidence.get(key)}")
    for key in ("canonical_registry_bytes", "manifest_bound_bytes_excluding_self"):
        value = evidence.get(key)
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"shard_release_evidence.{key} must be a positive integer")

    workflow_path = pathlib.Path(str(evidence.get("workflow")))
    if not workflow_path.is_file():
        raise ValueError(f"shard release evidence workflow is missing: {workflow_path}")
    workflow = workflow_path.read_text(encoding="utf-8")
    normalized_workflow = re.sub(r"\\\n\s*", " ", workflow)
    normalized_workflow = re.sub(r"\s+", " ", normalized_workflow)
    required_fragments = {
        str(evidence["gate_name"]),
        "--source-registry data/data-go-kr.registry.json",
        "--output-dir .datapan/ci/full-registry-shards",
        str(evidence["generated_inventory"]),
        str(evidence["generated_archive"]),
        str(evidence["archive_check"]),
    }.union(commands)
    missing_fragments = sorted(fragment for fragment in required_fragments if fragment not in normalized_workflow)
    if missing_fragments:
        raise ValueError(
            "shard release evidence workflow is missing required fragments: "
            + ", ".join(missing_fragments)
        )


def validate_manifest_evidence_contracts(report: dict[str, Any], manifest: dict[str, Any]) -> None:
    artifacts = manifest_artifacts(manifest)
    contracts = [
        as_dict(item, "manifest_evidence_contract")
        for item in as_list(report.get("manifest_evidence_contracts"), "manifest_evidence_contracts")
    ]
    by_contract: dict[str, dict[str, Any]] = {}
    for item in contracts:
        contract = item.get("contract")
        if not isinstance(contract, str) or not contract:
            raise ValueError("manifest_evidence_contract.contract must be a non-empty string")
        if contract in by_contract:
            raise ValueError(f"duplicate manifest evidence contract: {contract}")
        by_contract[contract] = item

    missing_contracts = sorted(set(REQUIRED_MANIFEST_EVIDENCE_CONTRACTS).difference(by_contract))
    if missing_contracts:
        raise ValueError(f"missing manifest evidence contracts: {', '.join(missing_contracts)}")

    for contract, expected in REQUIRED_MANIFEST_EVIDENCE_CONTRACTS.items():
        item = by_contract[contract]
        for key, value in expected.items():
            if item.get(key) != value:
                raise ValueError(f"manifest_evidence_contracts.{contract}.{key} expected {value}, got {item.get(key)}")
        if item.get("required") is not True:
            raise ValueError(f"manifest_evidence_contracts.{contract}.required must be true")

        artifact = artifacts.get(expected["path"])
        if artifact is None:
            raise ValueError(f"manifest missing required evidence artifact: {expected['path']}")
        for key in ("kind", "schema", "bytes", "sha256"):
            if item.get(key) != artifact.get(key):
                raise ValueError(
                    f"manifest_evidence_contracts.{contract}.{key} must match manifest artifact {expected['path']}"
                )


def ids_from_rollup_items(items: object, label: str) -> list[str]:
    ids: list[str] = []
    for index, item in enumerate(as_list(items, label)):
        entry = as_dict(item, f"{label}[{index}]")
        item_id = entry.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"{label}[{index}].id must be a non-empty string")
        ids.append(item_id)
    return sorted(ids)


def sorted_string_items(items: object, label: str) -> list[str]:
    values: list[str] = []
    for index, item in enumerate(as_list(items, label)):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{label}[{index}] must be a non-empty string")
        values.append(item)
    return sorted(values)


def runtime_source_entries(source_runtime: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(as_list(source_runtime.get("sources"), "source_runtime.sources")):
        source = as_dict(item, f"source_runtime.sources[{index}]")
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError(f"source_runtime.sources[{index}].source_id must be a non-empty string")
        blocking_count = source.get("blocking_count")
        warning_count = source.get("warning_count")
        if not isinstance(blocking_count, int) or blocking_count < 0:
            raise ValueError(f"source_runtime.sources[{index}].blocking_count must be a non-negative integer")
        if not isinstance(warning_count, int) or warning_count < 0:
            raise ValueError(f"source_runtime.sources[{index}].warning_count must be a non-negative integer")
        if blocking_count == 0 and warning_count == 0:
            continue
        entries.append(
            {
                "source_id": source_id,
                "blocking_count": blocking_count,
                "warning_count": warning_count,
                "blocker_ids": sorted_string_items(source.get("blocker_ids"), f"{source_id}.blocker_ids"),
                "warning_ids": sorted_string_items(source.get("warning_ids"), f"{source_id}.warning_ids"),
            }
        )
    return sorted(entries, key=lambda item: str(item["source_id"]))


def validate_runtime_risk_evidence(
    report: dict[str, Any],
    source_runtime: dict[str, Any],
    source_runtime_remediation: dict[str, Any],
    credential_runtime_policy: dict[str, Any],
    credential_collection_preflight: dict[str, Any],
    credential_runner_readiness: dict[str, Any],
    credential_receipt_queue: dict[str, Any],
    credential_review_handoff: dict[str, Any],
    credential_manual_review_decision: dict[str, Any],
    credential_manual_review_acceptance: dict[str, Any],
    error_action_routing: dict[str, Any],
    downstream_impact: dict[str, Any],
    source_runtime_path: pathlib.Path,
    source_runtime_remediation_path: pathlib.Path,
    credential_runtime_policy_path: pathlib.Path,
    credential_collection_preflight_path: pathlib.Path,
    credential_runner_readiness_path: pathlib.Path,
    credential_receipt_queue_path: pathlib.Path,
    credential_review_handoff_path: pathlib.Path,
    credential_manual_review_decision_path: pathlib.Path,
    credential_manual_review_acceptance_path: pathlib.Path,
    error_action_routing_path: pathlib.Path,
    impact_path: pathlib.Path,
) -> None:
    risk = as_dict(report.get("runtime_risk_evidence"), "runtime_risk_evidence")

    expected_paths = {
        "source_runtime_rollup": source_runtime_path.as_posix(),
        "source_runtime_remediation_map": source_runtime_remediation_path.as_posix(),
        "credential_runtime_evidence_policy": credential_runtime_policy_path.as_posix(),
        "credential_runtime_collection_preflight": credential_collection_preflight_path.as_posix(),
        "credential_runtime_runner_readiness": credential_runner_readiness_path.as_posix(),
        "credential_runtime_receipt_collection_queue": credential_receipt_queue_path.as_posix(),
        "credential_runtime_review_handoff": credential_review_handoff_path.as_posix(),
        "credential_runtime_manual_review_decision": credential_manual_review_decision_path.as_posix(),
        "credential_runtime_manual_review_acceptance": credential_manual_review_acceptance_path.as_posix(),
        "error_action_routing_rollup": error_action_routing_path.as_posix(),
        "downstream_impact_rollup": impact_path.as_posix(),
    }
    for key, value in expected_paths.items():
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")

    contracts = as_list(risk.get("required_contracts"), "runtime_risk_evidence.required_contracts")
    if contracts != REQUIRED_RUNTIME_RISK_CONTRACTS:
        raise ValueError(
            "runtime_risk_evidence.required_contracts must bind source runtime, remediation, credential policy, receipt queue, review handoff, manual-review decision, manual-review acceptance, routing, and impact in order"
        )
    report_contracts = {
        as_dict(item, "manifest_evidence_contract").get("contract")
        for item in as_list(report.get("manifest_evidence_contracts"), "manifest_evidence_contracts")
    }
    missing_contracts = sorted(set(REQUIRED_RUNTIME_RISK_CONTRACTS).difference(report_contracts))
    if missing_contracts:
        raise ValueError(f"runtime risk evidence references missing manifest contracts: {', '.join(missing_contracts)}")

    runtime_summary = as_dict(source_runtime.get("summary"), "source_runtime.summary")
    expected_counts = {
        "blocking_count": runtime_summary.get("blocking_count"),
        "warning_count": runtime_summary.get("warning_count"),
        "sources_without_evidence": runtime_summary.get("sources_without_evidence"),
    }
    for key, value in expected_counts.items():
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"source_runtime.summary.{key} must be a non-negative integer")
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")

    expected_blocker_ids = ids_from_rollup_items(source_runtime.get("blockers_by_id"), "source_runtime.blockers_by_id")
    expected_warning_ids = ids_from_rollup_items(source_runtime.get("warnings_by_id"), "source_runtime.warnings_by_id")
    if risk.get("blocker_ids") != expected_blocker_ids:
        raise ValueError("runtime_risk_evidence.blocker_ids must match source runtime blocker IDs")
    if risk.get("warning_ids") != expected_warning_ids:
        raise ValueError("runtime_risk_evidence.warning_ids must match source runtime warning IDs")

    expected_sources = runtime_source_entries(source_runtime)
    if risk.get("sources") != expected_sources:
        raise ValueError("runtime_risk_evidence.sources must match source runtime sources with blockers or warnings")

    unresolved_runtime_risk = any(expected_counts.values())
    remediation_summary = as_dict(source_runtime_remediation.get("summary"), "source_runtime_remediation.summary")
    remediation_expected = {
        "remediation_follow_up_required": remediation_summary.get("follow_up_required"),
        "remediation_manual_review_boundaries": remediation_summary.get("manual_review_boundaries"),
        "remediation_credential_policy_available": remediation_summary.get("credential_policy_available"),
        "remediation_receipt_contract_available": remediation_summary.get("receipt_contract_available"),
        "remediation_reviewed_receipt_intake_available": remediation_summary.get("reviewed_receipt_intake_available"),
        "remediation_receipt_reviewed": remediation_summary.get("receipt_reviewed"),
        "remediation_receipt_relief_eligible": remediation_summary.get("receipt_relief_eligible"),
        "remediation_receipt_backed_relief_allowed": remediation_summary.get("receipt_backed_relief_allowed"),
        "remediation_receipt_backed_relief_status": remediation_summary.get("receipt_backed_relief_status"),
        "remediation_receipt_linked_findings": remediation_summary.get("receipt_linked_findings"),
        "remediation_receipt_linked_absent": remediation_summary.get("receipt_linked_absent"),
        "remediation_receipt_linked_relief_eligible": remediation_summary.get("receipt_linked_relief_eligible"),
    }
    for key, value in remediation_expected.items():
        if (
            key.endswith("_required")
            or key.endswith("_boundaries")
            or key.endswith("_findings")
            or key.endswith("_absent")
            or key.endswith("_eligible")
        ) and not isinstance(value, bool):
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} source value must be a non-negative integer")
        elif (
            key.endswith("_available")
            or key.endswith("_allowed")
            or key.endswith("_reviewed")
            or key.endswith("_eligible")
        ):
            if not isinstance(value, bool):
                raise ValueError(f"{key} source value must be a boolean")
        elif not isinstance(value, str) or not value:
            raise ValueError(f"{key} source value must be a non-empty string")
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")
    if (
        unresolved_runtime_risk
        and risk.get("remediation_follow_up_required") == 0
        and risk.get("remediation_manual_review_boundaries") == 0
    ):
        raise ValueError("runtime blockers require source runtime remediation follow-up or manual-review boundary evidence")

    credential_summary = as_dict(credential_runtime_policy.get("summary"), "credential_runtime_policy.summary")
    credential_preflight_summary = as_dict(
        credential_collection_preflight.get("summary"),
        "credential_collection_preflight.summary",
    )
    credential_runner_summary = as_dict(
        credential_runner_readiness.get("summary"),
        "credential_runner_readiness.summary",
    )
    credential_queue_summary = as_dict(credential_receipt_queue.get("summary"), "credential_receipt_queue.summary")
    credential_handoff_summary = as_dict(credential_review_handoff.get("summary"), "credential_review_handoff.summary")
    credential_handoff_boundary = as_dict(
        credential_review_handoff.get("release_boundary"),
        "credential_review_handoff.release_boundary",
    )
    credential_decision_summary = as_dict(
        credential_manual_review_decision.get("summary"),
        "credential_manual_review_decision.summary",
    )
    credential_decision_body = as_dict(
        credential_manual_review_decision.get("decision"),
        "credential_manual_review_decision.decision",
    )
    credential_acceptance_summary = as_dict(
        credential_manual_review_acceptance.get("summary"),
        "credential_manual_review_acceptance.summary",
    )
    credential_acceptance_boundary = as_dict(
        credential_manual_review_acceptance.get("release_boundary"),
        "credential_manual_review_acceptance.release_boundary",
    )
    credential_acceptance_required_evidence = as_list(
        credential_manual_review_acceptance.get("required_acceptance_evidence"),
        "credential_manual_review_acceptance.required_acceptance_evidence",
    )
    credential_boundary = as_dict(
        credential_runtime_policy.get("release_boundary"),
        "credential_runtime_policy.release_boundary",
    )
    credential_relief_gate = as_dict(
        credential_boundary.get("receipt_backed_relief_gate"),
        "credential_runtime_policy.release_boundary.receipt_backed_relief_gate",
    )
    credential_expected = {
        "credential_policy_sources": credential_summary.get("credential_gated_sources"),
        "credential_policy_manual_review_boundaries": credential_summary.get("manual_review_boundaries"),
        "credential_policy_receipt_contract_available": credential_summary.get("receipt_contract_available"),
        "credential_policy_reviewed_receipt_intake_available": credential_summary.get(
            "reviewed_receipt_intake_available"
        ),
        "credential_policy_receipt_present": credential_summary.get("receipt_present"),
        "credential_policy_receipt_validated": credential_summary.get("receipt_validated"),
        "credential_policy_receipt_reviewed": credential_summary.get("receipt_reviewed"),
        "credential_policy_receipt_relief_eligible": credential_summary.get("receipt_relief_eligible"),
        "credential_policy_manual_review_reduction_allowed": credential_summary.get("manual_review_reduction_allowed"),
        "credential_policy_live_receipts": credential_summary.get("live_credentialed_receipts_checked_in"),
        "credential_policy_reviewed_receipts": credential_summary.get("reviewed_receipts_checked_in"),
        "credential_policy_default_ci_requires_credentials": credential_summary.get("default_ci_requires_credentials"),
        "credential_policy_effect": credential_boundary.get("compatibility_effect"),
        "credential_policy_relief_gate_status": credential_relief_gate.get("status"),
    }
    for key, value in credential_expected.items():
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")

    credential_preflight_expected = {
        "credential_collection_preflight_status": credential_preflight_summary.get("preflight_status"),
        "credential_collection_preflight_candidate_batches_present": credential_preflight_summary.get(
            "candidate_batches_present"
        ),
        "credential_collection_preflight_candidate_batches_missing": credential_preflight_summary.get(
            "candidate_batches_missing"
        ),
        "credential_collection_preflight_reviewed_receipts_present": credential_preflight_summary.get(
            "reviewed_receipts_present"
        ),
        "credential_collection_preflight_reviewed_receipts_missing": credential_preflight_summary.get(
            "reviewed_receipts_missing"
        ),
        "credential_collection_preflight_default_ci_runnable_sources": credential_preflight_summary.get(
            "default_ci_runnable_sources"
        ),
        "credential_collection_preflight_operator_environment_required_sources": credential_preflight_summary.get(
            "operator_environment_required_sources"
        ),
        "credential_collection_preflight_manual_review_reduction_allowed": credential_preflight_summary.get(
            "manual_review_reduction_allowed"
        ),
    }
    for key, value in credential_preflight_expected.items():
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")
    if (
        credential_preflight_summary.get("credential_gated_sources")
        != credential_summary.get("credential_gated_sources")
    ):
        raise ValueError("credential collection preflight source count must match credential policy")
    if (
        credential_preflight_summary.get("reviewed_receipts_present")
        != credential_queue_summary.get("reviewed_receipts_checked_in")
    ):
        raise ValueError("credential collection preflight reviewed receipts must match collection queue")
    if (
        credential_preflight_summary.get("manual_review_reduction_allowed")
        != credential_queue_summary.get("manual_review_reduction_allowed")
    ):
        raise ValueError("credential collection preflight relief state must match collection queue")
    if credential_preflight_summary.get("default_ci_runnable_sources") != 0:
        raise ValueError("credential collection preflight must remain secret-free and non-runnable in default CI")

    credential_runner_expected = {
        "credential_runner_readiness_status": credential_runner_summary.get("runner_status"),
        "credential_runner_ready_to_run_without_credentials": credential_runner_summary.get(
            "ready_to_run_without_credentials"
        ),
        "credential_runner_blocked_on_operator_env": credential_runner_summary.get("blocked_on_operator_env"),
        "credential_runner_blocked_on_candidate_batch": credential_runner_summary.get("blocked_on_candidate_batch"),
        "credential_runner_reviewed_receipts_present": credential_runner_summary.get("reviewed_receipts_present"),
        "credential_runner_reviewed_receipts_missing": credential_runner_summary.get("reviewed_receipts_missing"),
        "credential_runner_default_ci_requires_credentials": credential_runner_summary.get(
            "default_ci_requires_credentials"
        ),
        "credential_runner_checked_in_secrets_allowed": credential_runner_summary.get("checked_in_secrets_allowed"),
        "credential_runner_manual_review_reduction_allowed": credential_runner_summary.get(
            "manual_review_reduction_allowed"
        ),
        "credential_runner_local_session_artifacts_checked_in": credential_runner_summary.get(
            "local_session_artifacts_checked_in"
        ),
    }
    for key, value in credential_runner_expected.items():
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")
    if credential_runner_summary.get("credential_gated_sources") != credential_summary.get("credential_gated_sources"):
        raise ValueError("credential runner readiness source count must match credential policy")
    if credential_runner_summary.get("candidate_batches_present") != credential_preflight_summary.get(
        "candidate_batches_present"
    ):
        raise ValueError("credential runner readiness candidate batches must match collection preflight")
    if credential_runner_summary.get("reviewed_receipts_present") != credential_queue_summary.get(
        "reviewed_receipts_checked_in"
    ):
        raise ValueError("credential runner readiness reviewed receipts must match collection queue")
    if credential_runner_summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("credential runner readiness must remain secret-free in default CI")
    if credential_runner_summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("credential runner readiness must not allow checked-in secrets")
    if credential_runner_summary.get("local_session_artifacts_checked_in") is not False:
        raise ValueError("credential runner readiness must not mark local session artifacts checked in")
    if credential_runner_summary.get("manual_review_reduction_allowed") != credential_queue_summary.get(
        "manual_review_reduction_allowed"
    ):
        raise ValueError("credential runner readiness relief state must match collection queue")

    credential_queue_expected = {
        "credential_queue_status": credential_queue_summary.get("queue_status"),
        "credential_queue_absent": credential_queue_summary.get("absent"),
        "credential_queue_staged_only": credential_queue_summary.get("staged_only"),
        "credential_queue_reviewed_rejected": credential_queue_summary.get("reviewed_rejected"),
        "credential_queue_reviewed_accepted": credential_queue_summary.get("reviewed_accepted"),
        "credential_queue_relief_eligible": credential_queue_summary.get("relief_eligible"),
        "credential_queue_reviewed_receipts": credential_queue_summary.get("reviewed_receipts_checked_in"),
        "credential_queue_manual_review_reduction_allowed": credential_queue_summary.get(
            "manual_review_reduction_allowed"
        ),
        "credential_queue_default_ci_requires_credentials": credential_queue_summary.get(
            "default_ci_requires_credentials"
        ),
    }
    for key, value in credential_queue_expected.items():
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")
    if credential_queue_summary.get("credential_gated_sources") != credential_summary.get("credential_gated_sources"):
        raise ValueError("credential receipt queue source count must match credential policy gated source count")
    if credential_queue_summary.get("manual_review_reduction_allowed") != credential_summary.get(
        "manual_review_reduction_allowed"
    ):
        raise ValueError("credential receipt queue manual-review reduction state must match credential policy")
    if risk.get("credential_queue_reviewed_receipts") != risk.get("credential_policy_reviewed_receipts"):
        raise ValueError("credential receipt queue reviewed count must match credential policy")
    if risk.get("credential_queue_default_ci_requires_credentials") is not False:
        raise ValueError("credential receipt queue must remain secret-free in default CI")

    credential_handoff_expected = {
        "credential_handoff_status": credential_handoff_summary.get("handoff_status"),
        "credential_handoff_pending_review_sources": credential_handoff_summary.get("pending_review_sources"),
        "credential_handoff_reviewed_receipts": credential_handoff_summary.get("reviewed_receipts_checked_in"),
        "credential_handoff_relief_eligible_sources": credential_handoff_summary.get("relief_eligible_sources"),
        "credential_handoff_global_manual_review_relief_allowed": credential_handoff_summary.get(
            "global_manual_review_relief_allowed"
        ),
        "credential_handoff_manual_review_required": credential_handoff_summary.get("manual_review_required"),
        "credential_handoff_default_ci_requires_credentials": credential_handoff_summary.get(
            "default_ci_requires_credentials"
        ),
        "credential_handoff_relief_decision": credential_handoff_boundary.get("relief_decision"),
    }
    for key, value in credential_handoff_expected.items():
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")
    if credential_handoff_summary.get("credential_gated_sources") != credential_queue_summary.get(
        "credential_gated_sources"
    ):
        raise ValueError("credential handoff source count must match credential queue source count")
    if credential_handoff_summary.get("reviewed_receipts_checked_in") != credential_queue_summary.get(
        "reviewed_receipts_checked_in"
    ):
        raise ValueError("credential handoff reviewed receipt count must match credential queue")
    if credential_handoff_summary.get("relief_eligible_sources") != credential_queue_summary.get("relief_eligible"):
        raise ValueError("credential handoff relief-eligible count must match credential queue")
    if credential_handoff_summary.get("global_manual_review_relief_allowed") != credential_queue_summary.get(
        "manual_review_reduction_allowed"
    ):
        raise ValueError("credential handoff global relief state must match credential queue")
    if credential_handoff_summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("credential review handoff must remain secret-free in default CI")
    if risk.get("credential_handoff_global_manual_review_relief_allowed") is True and (
        risk.get("credential_handoff_reviewed_receipts", 0) <= 0
        or risk.get("credential_handoff_relief_eligible_sources", 0) <= 0
        or risk.get("credential_handoff_status") != "relief_ready"
        or risk.get("credential_handoff_relief_decision") != "allowed_by_all_reviewed_validated_receipts"
    ):
        raise ValueError("credential handoff relief requires reviewed relief-eligible receipts and relief_ready status")

    credential_acceptance_expected = {
        "manual_review_decision_status": credential_decision_summary.get("decision_status"),
        "manual_review_decision_accepted": credential_decision_summary.get("accepted"),
        "manual_review_decision_reason": credential_decision_body.get("reason"),
        "manual_review_decision_boundary_accepted": credential_decision_summary.get(
            "manual_review_release_boundary_accepted"
        ),
        "manual_review_acceptance_status": credential_acceptance_summary.get("acceptance_status"),
        "manual_review_acceptance_accepted": credential_acceptance_summary.get("accepted"),
        "manual_review_acceptance_decision": credential_acceptance_summary.get("acceptance_decision"),
        "manual_review_acceptance_required_evidence": len(credential_acceptance_required_evidence),
        "manual_review_acceptance_boundary_accepted": credential_acceptance_boundary.get(
            "manual_review_release_boundary_accepted"
        ),
        "manual_review_acceptance_goal_completion_effect": credential_acceptance_boundary.get(
            "goal_completion_effect"
        ),
    }
    for key, value in credential_acceptance_expected.items():
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")
    if credential_acceptance_summary.get("accepted") != credential_decision_summary.get("accepted"):
        raise ValueError("manual-review acceptance accepted state must match manual-review decision")
    if credential_acceptance_summary.get("decision_status") != credential_decision_summary.get("decision_status"):
        raise ValueError("manual-review acceptance decision status must match manual-review decision")
    if credential_acceptance_summary.get("decision_reason") != credential_decision_body.get("reason"):
        raise ValueError("manual-review acceptance decision reason must match manual-review decision")
    if credential_acceptance_summary.get("credential_handoff_status") != credential_handoff_summary.get(
        "handoff_status"
    ):
        raise ValueError("manual-review acceptance handoff status must match credential handoff")
    if credential_acceptance_summary.get("pending_review_sources") != credential_handoff_summary.get(
        "pending_review_sources"
    ):
        raise ValueError("manual-review acceptance pending review count must match credential handoff")
    if credential_acceptance_summary.get("reviewed_receipts_checked_in") != credential_handoff_summary.get(
        "reviewed_receipts_checked_in"
    ):
        raise ValueError("manual-review acceptance reviewed receipt count must match credential handoff")
    if credential_acceptance_summary.get("relief_eligible_sources") != credential_handoff_summary.get(
        "relief_eligible_sources"
    ):
        raise ValueError("manual-review acceptance relief-eligible count must match credential handoff")
    if credential_acceptance_summary.get("compatibility_manual_review_required") != risk.get("manual_review_required"):
        raise ValueError("manual-review acceptance state must mirror compatibility manual-review requirement")
    if credential_acceptance_summary.get("compatibility_effect") != risk.get("compatibility_effect"):
        raise ValueError("manual-review acceptance compatibility effect must mirror runtime risk evidence")
    if credential_acceptance_summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("manual-review acceptance must remain secret-free in default CI")
    if credential_acceptance_summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("manual-review acceptance must not allow checked-in secrets")
    if credential_acceptance_summary.get("accepted") is True and (
        credential_acceptance_summary.get("acceptance_status") != "accepted"
        or credential_acceptance_summary.get("acceptance_decision") != "accepted_manual_review_release_boundary"
        or credential_acceptance_boundary.get("manual_review_release_boundary_accepted") is not True
    ):
        raise ValueError("accepted manual-review boundary requires accepted status, decision, and release boundary")
    if credential_acceptance_summary.get("accepted") is False and (
        credential_acceptance_summary.get("acceptance_status") != "not_accepted"
        or credential_acceptance_summary.get("acceptance_decision")
        != "blocked_until_explicit_manual_review_acceptance"
        or credential_acceptance_boundary.get("manual_review_release_boundary_accepted") is not False
    ):
        raise ValueError("unaccepted manual-review boundary must stay explicitly blocked")
    if risk.get("remediation_credential_policy_available") is not True:
        raise ValueError("source runtime remediation must expose the credential policy availability boundary")
    if risk.get("remediation_receipt_contract_available") is not True:
        raise ValueError("source runtime remediation must expose the credential receipt contract boundary")
    if risk.get("remediation_reviewed_receipt_intake_available") is not True:
        raise ValueError("source runtime remediation must expose the reviewed receipt intake boundary")
    if risk.get("remediation_receipt_linked_findings", 0) < risk.get("remediation_manual_review_boundaries", 0):
        raise ValueError("source runtime remediation must link manual-review boundaries to reviewed receipt paths")
    if risk.get("remediation_receipt_linked_relief_eligible", 0) > risk.get("remediation_receipt_linked_findings", 0):
        raise ValueError("source runtime remediation relief-eligible linkage count cannot exceed linked findings")
    if risk.get("credential_policy_receipt_contract_available") is not True:
        raise ValueError("credential runtime policy must expose the receipt contract")
    if risk.get("credential_policy_reviewed_receipt_intake_available") is not True:
        raise ValueError("credential runtime policy must expose the reviewed receipt intake path")
    if unresolved_runtime_risk and risk.get("credential_policy_default_ci_requires_credentials") is not False:
        raise ValueError("default CI must remain secret-free while runtime blockers are credential-gated")
    if unresolved_runtime_risk and risk.get("credential_policy_live_receipts") != 0:
        raise ValueError("credential policy must not claim live receipts before checked-in receipts exist")
    if unresolved_runtime_risk and risk.get("credential_policy_manual_review_boundaries") == 0:
        raise ValueError("credential policy must preserve manual-review boundaries for unresolved runtime risk")
    if risk.get("credential_policy_manual_review_reduction_allowed") is True and (
        risk.get("credential_policy_live_receipts", 0) <= 0
        or risk.get("credential_policy_reviewed_receipts", 0) <= 0
        or risk.get("credential_policy_receipt_present") is not True
        or risk.get("credential_policy_receipt_validated") is not True
        or risk.get("credential_policy_receipt_reviewed") is not True
        or risk.get("credential_policy_receipt_relief_eligible") is not True
    ):
        raise ValueError("credential runtime relief requires present, validated, reviewed, relief-eligible receipts")
    if risk.get("remediation_receipt_backed_relief_allowed") is True and (
        risk.get("credential_policy_manual_review_reduction_allowed") is not True
        or risk.get("remediation_receipt_reviewed") is not True
        or risk.get("remediation_receipt_relief_eligible") is not True
    ):
        raise ValueError("remediation relief cannot be allowed before reviewed credential policy relief is allowed")
    if risk.get("manual_review_reduction_allowed") is True and (
        risk.get("credential_policy_manual_review_reduction_allowed") is not True
        or risk.get("remediation_receipt_backed_relief_allowed") is not True
        or risk.get("credential_handoff_global_manual_review_relief_allowed") is not True
        or risk.get("manual_review_acceptance_accepted") is True
    ):
        raise ValueError("compatibility relief cannot be allowed through manual-review acceptance")

    if unresolved_runtime_risk:
        if risk.get("manual_review_required") is not True:
            raise ValueError("runtime blockers or warnings require runtime_risk_evidence.manual_review_required=true")
        if risk.get("compatibility_effect") != "manual_review_required_until_runtime_blockers_resolved":
            raise ValueError("runtime blockers or warnings must keep manual-review compatibility effect")
        if risk.get("manual_review_reduction_allowed") is not False:
            raise ValueError("manual-review reduction must remain disallowed while receipt-backed runtime relief is absent")
        if risk.get("credential_handoff_status") != "review_required":
            raise ValueError("runtime blockers require credential handoff status review_required")
        if risk.get("credential_handoff_pending_review_sources", 0) <= 0:
            raise ValueError("runtime blockers require pending credential review sources in the handoff")
        if risk.get("manual_review_acceptance_accepted") is not False:
            raise ValueError("runtime blockers must keep manual-review acceptance unaccepted by default")
        if (
            risk.get("manual_review_reduction_status")
            != "blocked_until_reviewed_validated_credential_runtime_receipts_exist"
        ):
            raise ValueError("manual-review reduction status must point at reviewed validated credential runtime receipts")
    elif risk.get("manual_review_required") is not False:
        raise ValueError("runtime_risk_evidence.manual_review_required must be false when runtime evidence is clear")

    routing_summary = as_dict(error_action_routing.get("summary"), "error_action_routing.summary")
    impact_summary = as_dict(downstream_impact.get("summary"), "downstream_impact.summary")
    routing_expected = {
        "error_action_blocking_rules": routing_summary.get("blocking_rules"),
        "error_action_manual_review_rules": routing_summary.get("manual_review_rules"),
        "downstream_manual_review_changes": impact_summary.get("requires_manual_review"),
    }
    for key, value in routing_expected.items():
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} source value must be a non-negative integer")
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")

    if unresolved_runtime_risk and risk.get("error_action_manual_review_rules") == 0:
        raise ValueError("runtime blockers require error/action manual-review routing evidence")
    if unresolved_runtime_risk and risk.get("downstream_manual_review_changes") == 0:
        raise ValueError("runtime blockers require downstream impact manual-review evidence")


def validate_consumers(report: dict[str, Any]) -> None:
    consumers = [as_dict(item, "consumer") for item in as_list(report.get("consumers"), "consumers")]
    by_name: dict[str, dict[str, Any]] = {}
    for consumer in consumers:
        name = consumer.get("consumer")
        if not isinstance(name, str):
            raise ValueError("consumer.consumer must be a string")
        if name in by_name:
            raise ValueError(f"duplicate consumer entry: {name}")
        by_name[name] = consumer
        if not as_list(consumer.get("evidence"), f"{name}.evidence"):
            raise ValueError(f"{name}.evidence must not be empty")

    cli = by_name.get("datapan-cli")
    if cli is None:
        raise ValueError("missing datapan-cli consumer entry")
    surfaces = set(as_list(cli.get("surfaces"), "datapan-cli.surfaces"))
    if not REQUIRED_CLI_SURFACES.issubset(surfaces):
        missing = sorted(REQUIRED_CLI_SURFACES.difference(surfaces))
        raise ValueError(f"datapan-cli entry missing surfaces: {', '.join(missing)}")
    shard_proof = as_dict(report.get("shard_consumer_proof"), "shard_consumer_proof")
    expected_cli_mode = (
        "shard_preferred_with_monolith_fallback"
        if shard_proof.get("distribution_action_resolved") is True
        else "canonical_monolith"
    )
    if cli.get("compatibility_mode") != expected_cli_mode or cli.get("status") != "proven":
        raise ValueError(f"datapan-cli must remain proven through {expected_cli_mode} compatibility")
    if shard_proof.get("canonical_registry_required") is not True:
        raise ValueError("datapan-cli compatibility must preserve canonical registry fallback")

    studio = by_name.get("studio")
    if studio is None:
        raise ValueError("missing studio consumer entry")
    if studio.get("compatibility_mode") != "shard_preferred_with_monolith_fallback":
        raise ValueError("studio entry must represent the future shard-preferred fallback path")
    if studio.get("status") != "blocked":
        raise ValueError("studio shard-preferred path must remain blocked until fallback is proven")


def validate_consistency(
    report: dict[str, Any],
    manifest: dict[str, Any],
    readiness: dict[str, Any],
    source_runtime: dict[str, Any],
    source_runtime_remediation: dict[str, Any],
    credential_runtime_policy: dict[str, Any],
    credential_collection_preflight: dict[str, Any],
    credential_runner_readiness: dict[str, Any],
    credential_receipt_queue: dict[str, Any],
    credential_review_handoff: dict[str, Any],
    credential_manual_review_decision: dict[str, Any],
    credential_manual_review_acceptance: dict[str, Any],
    error_action_routing: dict[str, Any],
    downstream_impact: dict[str, Any],
    release_distribution_footprint: dict[str, Any],
    shard_proof: dict[str, Any],
    manifest_path: pathlib.Path,
    readiness_path: pathlib.Path,
    source_runtime_path: pathlib.Path,
    source_runtime_remediation_path: pathlib.Path,
    credential_runtime_policy_path: pathlib.Path,
    credential_collection_preflight_path: pathlib.Path,
    credential_runner_readiness_path: pathlib.Path,
    credential_receipt_queue_path: pathlib.Path,
    credential_review_handoff_path: pathlib.Path,
    credential_manual_review_decision_path: pathlib.Path,
    credential_manual_review_acceptance_path: pathlib.Path,
    error_action_routing_path: pathlib.Path,
    impact_path: pathlib.Path,
) -> None:
    validate_generation_inputs(report, manifest, readiness, manifest_path, readiness_path)
    validate_summary(report)
    validate_manifest_links(report, manifest)
    validate_rollup_generation_contract(report)
    validate_readiness(report, readiness)
    validate_shard_policy(report)
    validate_shard_consumer_proof(report, shard_proof)
    validate_shard_release_evidence(report)
    validate_manifest_evidence_contracts(report, manifest)
    validate_distribution_footprint(report, release_distribution_footprint)
    validate_runtime_risk_evidence(
        report,
        source_runtime,
        source_runtime_remediation,
        credential_runtime_policy,
        credential_collection_preflight,
        credential_runner_readiness,
        credential_receipt_queue,
        credential_review_handoff,
        credential_manual_review_decision,
        credential_manual_review_acceptance,
        error_action_routing,
        downstream_impact,
        source_runtime_path,
        source_runtime_remediation_path,
        credential_runtime_policy_path,
        credential_collection_preflight_path,
        credential_runner_readiness_path,
        credential_receipt_queue_path,
        credential_review_handoff_path,
        credential_manual_review_decision_path,
        credential_manual_review_acceptance_path,
        error_action_routing_path,
        impact_path,
    )
    validate_consumers(report)


def validate_distribution_footprint(report: dict[str, Any], footprint: dict[str, Any]) -> None:
    summary = as_dict(footprint.get("summary"), "release_distribution_footprint.summary")
    boundary = as_dict(footprint.get("distribution_boundary"), "release_distribution_footprint.distribution_boundary")
    evidence = as_dict(report.get("shard_release_evidence"), "shard_release_evidence")
    expected = {
        "release_distribution_footprint": DEFAULT_RELEASE_DISTRIBUTION_FOOTPRINT.as_posix(),
        "canonical_registry_bytes": summary.get("canonical_registry_bytes"),
        "manifest_bound_bytes_excluding_self": summary.get("manifest_bound_bytes_excluding_self"),
        "large_monolith_threshold_bytes": summary.get("large_monolith_threshold_bytes"),
        "registry_footprint_status": summary.get("registry_footprint_status"),
        "canonical_registry_required": summary.get("canonical_registry_required"),
        "shard_distribution_required": summary.get("shard_distribution_required"),
        "monolith_fallback_required": summary.get("monolith_fallback_required"),
        "footprint_consumer_effect": boundary.get("consumer_effect"),
    }
    for key, value in expected.items():
        if evidence.get(key) != value:
            raise ValueError(f"shard_release_evidence.{key} must match release distribution footprint")
    if summary.get("canonical_registry_path") != CANONICAL_REGISTRY_PATH:
        raise ValueError("release distribution footprint must describe the canonical registry")
    if boundary.get("canonical_registry_compatible") is not True:
        raise ValueError("release distribution footprint must preserve canonical registry compatibility")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default=COMPATIBILITY_SCHEMA,
        type=pathlib.Path,
        help="release consumer compatibility JSON Schema path",
    )
    parser.add_argument("--manifest", default="manifest.json", type=pathlib.Path)
    parser.add_argument("--readiness", default="reports/latest-release-readiness.json", type=pathlib.Path)
    parser.add_argument("--source-runtime-rollup", default=DEFAULT_SOURCE_RUNTIME_ROLLUP, type=pathlib.Path)
    parser.add_argument("--source-runtime-remediation", default=DEFAULT_SOURCE_RUNTIME_REMEDIATION, type=pathlib.Path)
    parser.add_argument("--credential-runtime-policy", default=DEFAULT_CREDENTIAL_RUNTIME_POLICY, type=pathlib.Path)
    parser.add_argument(
        "--credential-collection-preflight",
        default=DEFAULT_CREDENTIAL_COLLECTION_PREFLIGHT,
        type=pathlib.Path,
    )
    parser.add_argument(
        "--credential-runner-readiness",
        default=DEFAULT_CREDENTIAL_RUNNER_READINESS,
        type=pathlib.Path,
    )
    parser.add_argument("--credential-receipt-queue", default=DEFAULT_CREDENTIAL_RECEIPT_QUEUE, type=pathlib.Path)
    parser.add_argument("--credential-review-handoff", default=DEFAULT_CREDENTIAL_REVIEW_HANDOFF, type=pathlib.Path)
    parser.add_argument(
        "--credential-manual-review-decision",
        default=DEFAULT_CREDENTIAL_MANUAL_REVIEW_DECISION,
        type=pathlib.Path,
    )
    parser.add_argument(
        "--credential-manual-review-acceptance",
        default=DEFAULT_CREDENTIAL_MANUAL_REVIEW_ACCEPTANCE,
        type=pathlib.Path,
    )
    parser.add_argument("--error-action-routing-rollup", default=DEFAULT_ERROR_ACTION_ROUTING_ROLLUP, type=pathlib.Path)
    parser.add_argument("--impact-rollup", default=DEFAULT_IMPACT_ROLLUP, type=pathlib.Path)
    parser.add_argument(
        "--release-distribution-footprint",
        default=DEFAULT_RELEASE_DISTRIBUTION_FOOTPRINT,
        type=pathlib.Path,
    )
    parser.add_argument("--shard-consumer-proof", default=DEFAULT_SHARD_CONSUMER_PROOF, type=pathlib.Path)
    parser.add_argument("report", nargs="?", default=COMPATIBILITY_REPORT_PATH, type=pathlib.Path)
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        report = as_dict(load_json(args.report), args.report.as_posix())
        manifest = as_dict(load_json(args.manifest), args.manifest.as_posix())
        readiness = as_dict(load_json(args.readiness), args.readiness.as_posix())
        source_runtime = as_dict(load_json(args.source_runtime_rollup), args.source_runtime_rollup.as_posix())
        source_runtime_remediation = as_dict(
            load_json(args.source_runtime_remediation),
            args.source_runtime_remediation.as_posix(),
        )
        credential_runtime_policy = as_dict(
            load_json(args.credential_runtime_policy),
            args.credential_runtime_policy.as_posix(),
        )
        credential_collection_preflight = as_dict(
            load_json(args.credential_collection_preflight),
            args.credential_collection_preflight.as_posix(),
        )
        credential_runner_readiness = as_dict(
            load_json(args.credential_runner_readiness),
            args.credential_runner_readiness.as_posix(),
        )
        credential_receipt_queue = as_dict(
            load_json(args.credential_receipt_queue),
            args.credential_receipt_queue.as_posix(),
        )
        credential_review_handoff = as_dict(
            load_json(args.credential_review_handoff),
            args.credential_review_handoff.as_posix(),
        )
        credential_manual_review_decision = as_dict(
            load_json(args.credential_manual_review_decision),
            args.credential_manual_review_decision.as_posix(),
        )
        credential_manual_review_acceptance = as_dict(
            load_json(args.credential_manual_review_acceptance),
            args.credential_manual_review_acceptance.as_posix(),
        )
        error_action_routing = as_dict(
            load_json(args.error_action_routing_rollup),
            args.error_action_routing_rollup.as_posix(),
        )
        downstream_impact = as_dict(load_json(args.impact_rollup), args.impact_rollup.as_posix())
        release_distribution_footprint = as_dict(
            load_json(args.release_distribution_footprint),
            args.release_distribution_footprint.as_posix(),
        )
        shard_proof = as_dict(load_json(args.shard_consumer_proof), args.shard_consumer_proof.as_posix())

        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
        if errors:
            print(f"FAIL {args.report}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            return 1

        validate_consistency(
            report,
            manifest,
            readiness,
            source_runtime,
            source_runtime_remediation,
            credential_runtime_policy,
            credential_collection_preflight,
            credential_runner_readiness,
            credential_receipt_queue,
            credential_review_handoff,
            credential_manual_review_decision,
            credential_manual_review_acceptance,
            error_action_routing,
            downstream_impact,
            release_distribution_footprint,
            shard_proof,
            args.manifest,
            args.readiness,
            args.source_runtime_rollup,
            args.source_runtime_remediation,
            args.credential_runtime_policy,
            args.credential_collection_preflight,
            args.credential_runner_readiness,
            args.credential_receipt_queue,
            args.credential_review_handoff,
            args.credential_manual_review_decision,
            args.credential_manual_review_acceptance,
            args.error_action_routing_rollup,
            args.impact_rollup,
        )
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL {args.report}: {exc}", file=sys.stderr)
        return 1

    summary = as_dict(report.get("summary"), "summary")
    print(
        f"ok {args.report} "
        f"(consumers={summary.get('consumer_count')}, proven={summary.get('proven_consumers')}, "
        f"blocked={summary.get('blocked_consumers')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
