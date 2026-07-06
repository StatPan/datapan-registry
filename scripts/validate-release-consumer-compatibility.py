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
DEFAULT_ERROR_ACTION_ROUTING_ROLLUP = pathlib.Path("reports/error-action-routing-rollup.json")
DEFAULT_IMPACT_ROLLUP = pathlib.Path("reports/registry-impact-plan.json")
REQUIRED_RUNTIME_RISK_CONTRACTS = [
    "source_runtime_evidence",
    "source_runtime_remediation",
    "credential_runtime_evidence_policy",
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


def validate_shard_release_evidence(report: dict[str, Any]) -> None:
    evidence = as_dict(report.get("shard_release_evidence"), "shard_release_evidence")
    for key, value in REQUIRED_SHARD_RELEASE_EVIDENCE.items():
        if evidence.get(key) != value:
            raise ValueError(f"shard_release_evidence.{key} expected {value}, got {evidence.get(key)}")

    commands = set(as_list(evidence.get("required_commands"), "shard_release_evidence.required_commands"))
    missing_commands = sorted(REQUIRED_SHARD_RELEASE_COMMANDS.difference(commands))
    if missing_commands:
        raise ValueError(f"shard release evidence missing required commands: {', '.join(missing_commands)}")

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
    error_action_routing: dict[str, Any],
    downstream_impact: dict[str, Any],
    source_runtime_path: pathlib.Path,
    source_runtime_remediation_path: pathlib.Path,
    credential_runtime_policy_path: pathlib.Path,
    error_action_routing_path: pathlib.Path,
    impact_path: pathlib.Path,
) -> None:
    risk = as_dict(report.get("runtime_risk_evidence"), "runtime_risk_evidence")

    expected_paths = {
        "source_runtime_rollup": source_runtime_path.as_posix(),
        "source_runtime_remediation_map": source_runtime_remediation_path.as_posix(),
        "credential_runtime_evidence_policy": credential_runtime_policy_path.as_posix(),
        "error_action_routing_rollup": error_action_routing_path.as_posix(),
        "downstream_impact_rollup": impact_path.as_posix(),
    }
    for key, value in expected_paths.items():
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")

    contracts = as_list(risk.get("required_contracts"), "runtime_risk_evidence.required_contracts")
    if contracts != REQUIRED_RUNTIME_RISK_CONTRACTS:
        raise ValueError(
            "runtime_risk_evidence.required_contracts must bind source runtime, remediation, credential policy, routing, and impact in order"
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
        "remediation_receipt_backed_relief_allowed": remediation_summary.get("receipt_backed_relief_allowed"),
        "remediation_receipt_backed_relief_status": remediation_summary.get("receipt_backed_relief_status"),
    }
    for key, value in remediation_expected.items():
        if key.endswith("_required") or key.endswith("_boundaries"):
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} source value must be a non-negative integer")
        elif key.endswith("_available") or key.endswith("_allowed"):
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
        "credential_policy_receipt_present": credential_summary.get("receipt_present"),
        "credential_policy_receipt_validated": credential_summary.get("receipt_validated"),
        "credential_policy_manual_review_reduction_allowed": credential_summary.get("manual_review_reduction_allowed"),
        "credential_policy_live_receipts": credential_summary.get("live_credentialed_receipts_checked_in"),
        "credential_policy_default_ci_requires_credentials": credential_summary.get("default_ci_requires_credentials"),
        "credential_policy_effect": credential_boundary.get("compatibility_effect"),
        "credential_policy_relief_gate_status": credential_relief_gate.get("status"),
    }
    for key, value in credential_expected.items():
        if risk.get(key) != value:
            raise ValueError(f"runtime_risk_evidence.{key} expected {value}, got {risk.get(key)}")
    if risk.get("remediation_credential_policy_available") is not True:
        raise ValueError("source runtime remediation must expose the credential policy availability boundary")
    if risk.get("remediation_receipt_contract_available") is not True:
        raise ValueError("source runtime remediation must expose the credential receipt contract boundary")
    if risk.get("credential_policy_receipt_contract_available") is not True:
        raise ValueError("credential runtime policy must expose the receipt contract")
    if unresolved_runtime_risk and risk.get("credential_policy_default_ci_requires_credentials") is not False:
        raise ValueError("default CI must remain secret-free while runtime blockers are credential-gated")
    if unresolved_runtime_risk and risk.get("credential_policy_live_receipts") != 0:
        raise ValueError("credential policy must not claim live receipts before checked-in receipts exist")
    if unresolved_runtime_risk and risk.get("credential_policy_manual_review_boundaries") == 0:
        raise ValueError("credential policy must preserve manual-review boundaries for unresolved runtime risk")
    if risk.get("credential_policy_manual_review_reduction_allowed") is True and (
        risk.get("credential_policy_live_receipts", 0) <= 0
        or risk.get("credential_policy_receipt_present") is not True
        or risk.get("credential_policy_receipt_validated") is not True
    ):
        raise ValueError("credential runtime relief requires present and validated checked-in receipts")
    if risk.get("remediation_receipt_backed_relief_allowed") is True and (
        risk.get("credential_policy_manual_review_reduction_allowed") is not True
    ):
        raise ValueError("remediation relief cannot be allowed before credential policy relief is allowed")
    if risk.get("manual_review_reduction_allowed") is True and (
        risk.get("credential_policy_manual_review_reduction_allowed") is not True
        or risk.get("remediation_receipt_backed_relief_allowed") is not True
    ):
        raise ValueError("compatibility relief cannot be allowed without validated receipt-backed policy and remediation gates")

    if unresolved_runtime_risk:
        if risk.get("manual_review_required") is not True:
            raise ValueError("runtime blockers or warnings require runtime_risk_evidence.manual_review_required=true")
        if risk.get("compatibility_effect") != "manual_review_required_until_runtime_blockers_resolved":
            raise ValueError("runtime blockers or warnings must keep manual-review compatibility effect")
        if risk.get("manual_review_reduction_allowed") is not False:
            raise ValueError("manual-review reduction must remain disallowed while receipt-backed runtime relief is absent")
        if risk.get("manual_review_reduction_status") != "blocked_until_validated_credential_runtime_receipts_exist":
            raise ValueError("manual-review reduction status must point at validated credential runtime receipts")
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
    if cli.get("compatibility_mode") != "canonical_monolith" or cli.get("status") != "proven":
        raise ValueError("datapan-cli must remain proven through canonical_monolith compatibility")

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
    error_action_routing: dict[str, Any],
    downstream_impact: dict[str, Any],
    manifest_path: pathlib.Path,
    readiness_path: pathlib.Path,
    source_runtime_path: pathlib.Path,
    source_runtime_remediation_path: pathlib.Path,
    credential_runtime_policy_path: pathlib.Path,
    error_action_routing_path: pathlib.Path,
    impact_path: pathlib.Path,
) -> None:
    validate_generation_inputs(report, manifest, readiness, manifest_path, readiness_path)
    validate_summary(report)
    validate_manifest_links(report, manifest)
    validate_rollup_generation_contract(report)
    validate_readiness(report, readiness)
    validate_shard_policy(report)
    validate_shard_release_evidence(report)
    validate_manifest_evidence_contracts(report, manifest)
    validate_runtime_risk_evidence(
        report,
        source_runtime,
        source_runtime_remediation,
        credential_runtime_policy,
        error_action_routing,
        downstream_impact,
        source_runtime_path,
        source_runtime_remediation_path,
        credential_runtime_policy_path,
        error_action_routing_path,
        impact_path,
    )
    validate_consumers(report)


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
    parser.add_argument("--error-action-routing-rollup", default=DEFAULT_ERROR_ACTION_ROUTING_ROLLUP, type=pathlib.Path)
    parser.add_argument("--impact-rollup", default=DEFAULT_IMPACT_ROLLUP, type=pathlib.Path)
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
        error_action_routing = as_dict(
            load_json(args.error_action_routing_rollup),
            args.error_action_routing_rollup.as_posix(),
        )
        downstream_impact = as_dict(load_json(args.impact_rollup), args.impact_rollup.as_posix())

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
            error_action_routing,
            downstream_impact,
            args.manifest,
            args.readiness,
            args.source_runtime_rollup,
            args.source_runtime_remediation,
            args.credential_runtime_policy,
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
