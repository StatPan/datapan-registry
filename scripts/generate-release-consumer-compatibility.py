#!/usr/bin/env python3
"""Generate the datapan-registry release consumer compatibility report."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import sys
from typing import Any


CANONICAL_REGISTRY_PATH = "data/data-go-kr.registry.json"
DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_READINESS = pathlib.Path("reports/latest-release-readiness.json")
DEFAULT_SOURCE_RUNTIME_ROLLUP = pathlib.Path("reports/source-runtime-evidence-rollup.json")
DEFAULT_SOURCE_RUNTIME_REMEDIATION = pathlib.Path("reports/source-runtime-remediation-map.json")
DEFAULT_CREDENTIAL_RUNTIME_POLICY = pathlib.Path("reports/credential-runtime-evidence-policy.json")
DEFAULT_CREDENTIAL_RECEIPT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_ERROR_ACTION_ROUTING_ROLLUP = pathlib.Path("reports/error-action-routing-rollup.json")
DEFAULT_IMPACT_ROLLUP = pathlib.Path("reports/registry-impact-plan.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-consumer-compatibility.json")
REQUIRED_RUNTIME_RISK_CONTRACTS = [
    "source_runtime_evidence",
    "source_runtime_remediation",
    "credential_runtime_evidence_policy",
    "credential_runtime_receipt_collection_queue",
    "error_action_routing",
    "downstream_impact",
]
REQUIRED_RELEASE_HEALTH_SCHEMAS = [
    "schemas/datapan.install-smoke-summary.v1.schema.json",
    "schemas/datapan.doctor-smoke-summary.v1.schema.json",
    "schemas/datapan.release-health-rollup.v1.schema.json",
]
REQUIRED_CI_REPORTS = [
    ".datapan/ci/current-release-install-smoke.json",
    ".datapan/ci/current-release-doctor-smoke.json",
    ".datapan/ci/latest-release-install-smoke.json",
    ".datapan/ci/latest-release-doctor-smoke.json",
    ".datapan/ci/release-health-rollup.json",
]
ROLLUP_GENERATION_CONTRACT = {
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
REQUIRED_SHARD_INSTALL_FIELDS = [
    "mode",
    "shards_asset_present",
    "shards_validated",
    "shards_inventory_present",
    "shards_count",
    "shards_records",
]
SHARD_RELEASE_EVIDENCE = {
    "status": "ci_validated_optional_asset",
    "workflow": ".github/workflows/verify-release.yml",
    "gate_name": "Validate full registry shard release evidence",
    "source_registry": CANONICAL_REGISTRY_PATH,
    "generated_inventory": ".datapan/ci/full-registry-shards/registry-shards.json",
    "generated_archive": ".datapan/ci/full-data-go-kr-shards.tar.gz",
    "archive_check": ".datapan/ci/full-shard-archive-check.txt",
    "required_commands": [
        "python scripts/generate-registry-shards.py",
        "python scripts/validate-registry-shards.py",
        "python scripts/package-registry-shards.py",
        "python scripts/package-registry-shards.py --check",
    ],
}
REQUIRED_MANIFEST_EVIDENCE_CONTRACTS = [
    {
        "contract": "source_contracts",
        "path": "reports/source-contract-rollup.json",
        "kind": "source_contract_rollup",
        "schema": "https://schemas.datapan.dev/datapan.source-contract-rollup.v1.schema.json",
    },
    {
        "contract": "source_runtime_evidence",
        "path": "reports/source-runtime-evidence-rollup.json",
        "kind": "source_runtime_evidence_rollup",
        "schema": "https://schemas.datapan.dev/datapan.source-runtime-evidence-rollup.v1.schema.json",
    },
    {
        "contract": "source_runtime_remediation",
        "path": "reports/source-runtime-remediation-map.json",
        "kind": "source_runtime_remediation_map",
        "schema": "https://schemas.datapan.dev/datapan.source-runtime-remediation-map.v1.schema.json",
    },
    {
        "contract": "credential_runtime_evidence_policy",
        "path": "reports/credential-runtime-evidence-policy.json",
        "kind": "credential_runtime_evidence_policy",
        "schema": "https://schemas.datapan.dev/datapan.credential-runtime-evidence-policy.v1.schema.json",
    },
    {
        "contract": "credential_runtime_receipt_collection_queue",
        "path": "reports/credential-runtime-receipt-collection-queue.json",
        "kind": "credential_runtime_receipt_collection_queue",
        "schema": "https://schemas.datapan.dev/datapan.credential-runtime-receipt-collection-queue.v1.schema.json",
    },
    {
        "contract": "error_action_routing",
        "path": "reports/error-action-routing-rollup.json",
        "kind": "error_action_routing_rollup",
        "schema": "https://schemas.datapan.dev/datapan.error-action-routing-rollup.v1.schema.json",
    },
    {
        "contract": "downstream_impact",
        "path": "reports/registry-impact-plan.json",
        "kind": "registry_impact_plan",
        "schema": "https://schemas.datapan.dev/datapan.registry-impact-plan.v1.schema.json",
    },
    {
        "contract": "source_reference_drift",
        "path": "reports/source-reference-drift.json",
        "kind": "source_reference_drift",
        "schema": "https://schemas.datapan.dev/datapan.source-reference-drift.v1.schema.json",
    },
    {
        "contract": "source_report_inventory",
        "path": "reports/source-report-inventory.json",
        "kind": "source_report_inventory",
        "schema": "https://schemas.datapan.dev/datapan.source-report-inventory.v1.schema.json",
    },
]


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_json(value), encoding="utf-8")


def normalized_readiness_fingerprint(readiness: dict[str, Any]) -> str:
    normalized = dict(readiness)
    normalized.pop("generated_at", None)
    data = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def manifest_artifacts(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for index, artifact in enumerate(as_list(manifest.get("artifacts"), "manifest.artifacts")):
        if not isinstance(artifact, dict):
            raise ValueError(f"manifest.artifacts[{index}] must be an object")
        path = artifact.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"manifest.artifacts[{index}].path must be a non-empty string")
        artifacts[path] = artifact
    return artifacts


def readiness_gate(readiness: dict[str, Any], gate_id: str) -> dict[str, Any]:
    for index, gate in enumerate(as_list(readiness.get("gates"), "readiness.gates")):
        if not isinstance(gate, dict):
            raise ValueError(f"readiness.gates[{index}] must be an object")
        if gate.get("id") == gate_id:
            return gate
    raise ValueError(f"missing readiness gate: {gate_id}")


def require_release_inputs(manifest: dict[str, Any], readiness: dict[str, Any]) -> str:
    artifacts = manifest_artifacts(manifest)
    registry_artifact = artifacts.get(CANONICAL_REGISTRY_PATH)
    if not registry_artifact or registry_artifact.get("kind") != "registry":
        raise ValueError(f"manifest must include {CANONICAL_REGISTRY_PATH} with kind=registry")

    for schema_path in REQUIRED_RELEASE_HEALTH_SCHEMAS:
        artifact = artifacts.get(schema_path)
        if not artifact or artifact.get("kind") != "schema":
            raise ValueError(f"manifest must include required release-health schema: {schema_path}")

    manifest_gate = readiness_gate(readiness, "manifest_verified")
    if manifest_gate.get("status") != "pass":
        raise ValueError("readiness manifest_verified gate must pass")
    registry_gate = readiness_gate(readiness, "registry_has_specs")
    if registry_gate.get("status") != "pass":
        raise ValueError("readiness registry_has_specs gate must pass")
    if registry_gate.get("artifact_path") != CANONICAL_REGISTRY_PATH:
        raise ValueError("readiness registry_has_specs gate must validate the canonical registry path")
    actual_specs = registry_gate.get("actual")
    if not isinstance(actual_specs, int) or actual_specs <= 0:
        raise ValueError("readiness registry_has_specs.actual must be a positive integer")

    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("manifest.generated_at must be a non-empty string")
    return generated_at


def consumer_entries() -> list[dict[str, Any]]:
    return [
        {
            "consumer": "datapan-cli",
            "surfaces": [
                "catalog install",
                "doctor",
                "catalog release verify",
                "catalog release readiness",
            ],
            "compatibility_mode": "canonical_monolith",
            "status": "proven",
            "evidence": [
                "reports/latest-release-verification.json",
                "reports/latest-release-readiness.json",
                "datapan-registry-release-health/release-health-rollup.json",
            ],
            "notes": (
                "CLI release install and doctor compatibility is proven through the canonical registry path "
                "while shard support remains additive."
            ),
        },
        {
            "consumer": "release-operator",
            "surfaces": [
                "GitHub Release zip",
                "weekly release-health workflow",
                "manual release draft workflow",
            ],
            "compatibility_mode": "canonical_monolith",
            "status": "proven",
            "evidence": [
                "manifest.json",
                "reports/latest-release-readiness.json",
                "datapan-registry-release-health/release-health-rollup.json",
            ],
            "notes": (
                "Operators must keep the canonical registry in the release zip and may attach shard archives "
                "only as optional compatibility-period assets."
            ),
        },
        {
            "consumer": "datapan-api",
            "surfaces": [
                "served dataset contract review",
                "registry impact plan",
            ],
            "compatibility_mode": "registry_metadata",
            "status": "additive",
            "evidence": [
                "reports/registry-impact-plan.json",
                "docs/registry-governance-policy.md",
            ],
            "notes": (
                "Registry-only release evidence does not require API route or database changes unless an "
                "impact plan marks served datasets."
            ),
        },
        {
            "consumer": "datapan-data",
            "surfaces": [
                "promoted dataset review",
                "registry impact plan",
            ],
            "compatibility_mode": "registry_metadata",
            "status": "additive",
            "evidence": [
                "reports/registry-impact-plan.json",
                "docs/data-go-kr-mastery-plan.md",
            ],
            "notes": "Registry-only compatibility evidence does not create promoted data blocks.",
        },
        {
            "consumer": "sdk",
            "surfaces": [
                "served dataset SDK regeneration",
                "registry impact plan",
            ],
            "compatibility_mode": "no_served_contract",
            "status": "not_required",
            "evidence": [
                "reports/registry-impact-plan.json",
                "docs/registry-governance-policy.md",
            ],
            "notes": "No SDK regeneration is required for registry-only release-health and compatibility artifacts.",
        },
        {
            "consumer": "mcp",
            "surfaces": [
                "tool contract review",
                "registry impact plan",
            ],
            "compatibility_mode": "no_served_contract",
            "status": "not_required",
            "evidence": [
                "reports/registry-impact-plan.json",
                "docs/registry-governance-policy.md",
            ],
            "notes": "No MCP contract regeneration is required for registry-only release-health and compatibility artifacts.",
        },
        {
            "consumer": "studio",
            "surfaces": [
                "registry snapshot loading",
                "future shard-preferred browsing",
            ],
            "compatibility_mode": "shard_preferred_with_monolith_fallback",
            "status": "blocked",
            "evidence": [
                "docs/registry-shard-artifact-strategy.md",
                "StatPan/datapan-cli#128",
            ],
            "notes": (
                "Studio-facing shard preference remains blocked until the canonical registry fallback contract "
                "is implemented and proven downstream."
            ),
        },
    ]


def summary_for(consumers: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: collections.Counter[str] = collections.Counter()
    for consumer in consumers:
        status = consumer.get("status")
        if isinstance(status, str):
            status_counts[status] += 1
    return {
        "consumer_count": len(consumers),
        "proven_consumers": status_counts["proven"],
        "blocked_consumers": status_counts["blocked"],
        "canonical_registry_required": True,
        "shard_assets_required": False,
    }


def manifest_evidence_contracts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = manifest_artifacts(manifest)
    contracts: list[dict[str, Any]] = []
    for expected in REQUIRED_MANIFEST_EVIDENCE_CONTRACTS:
        artifact = artifacts.get(expected["path"])
        if artifact is None:
            raise ValueError(f"manifest missing required compatibility evidence: {expected['path']}")
        for key in ("kind", "schema"):
            if artifact.get(key) != expected[key]:
                raise ValueError(
                    f"manifest artifact {expected['path']} {key} expected {expected[key]}, got {artifact.get(key)}"
                )
        bytes_value = artifact.get("bytes")
        sha256 = artifact.get("sha256")
        if not isinstance(bytes_value, int) or bytes_value <= 0:
            raise ValueError(f"manifest artifact {expected['path']} bytes must be a positive integer")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError(f"manifest artifact {expected['path']} sha256 must be a 64-character string")
        contracts.append(
            {
                "contract": expected["contract"],
                "path": expected["path"],
                "kind": expected["kind"],
                "schema": expected["schema"],
                "bytes": bytes_value,
                "sha256": sha256,
                "required": True,
            }
        )
    return contracts


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


def runtime_risk_evidence(
    source_runtime: dict[str, Any],
    source_runtime_remediation: dict[str, Any],
    credential_runtime_policy: dict[str, Any],
    credential_receipt_queue: dict[str, Any],
    error_action_routing: dict[str, Any],
    downstream_impact: dict[str, Any],
    *,
    source_runtime_path: pathlib.Path,
    source_runtime_remediation_path: pathlib.Path,
    credential_runtime_policy_path: pathlib.Path,
    credential_receipt_queue_path: pathlib.Path,
    error_action_routing_path: pathlib.Path,
    impact_path: pathlib.Path,
) -> dict[str, Any]:
    runtime_summary = as_dict(source_runtime.get("summary"), "source_runtime.summary")
    remediation_summary = as_dict(source_runtime_remediation.get("summary"), "source_runtime_remediation.summary")
    credential_policy_summary = as_dict(credential_runtime_policy.get("summary"), "credential_runtime_policy.summary")
    credential_queue_summary = as_dict(credential_receipt_queue.get("summary"), "credential_receipt_queue.summary")
    credential_policy_boundary = as_dict(
        credential_runtime_policy.get("release_boundary"),
        "credential_runtime_policy.release_boundary",
    )
    credential_relief_gate = as_dict(
        credential_policy_boundary.get("receipt_backed_relief_gate"),
        "credential_runtime_policy.release_boundary.receipt_backed_relief_gate",
    )
    routing_summary = as_dict(error_action_routing.get("summary"), "error_action_routing.summary")
    impact_summary = as_dict(downstream_impact.get("summary"), "downstream_impact.summary")

    blocking_count = runtime_summary.get("blocking_count")
    warning_count = runtime_summary.get("warning_count")
    sources_without_evidence = runtime_summary.get("sources_without_evidence")
    for key, value in {
        "blocking_count": blocking_count,
        "warning_count": warning_count,
        "sources_without_evidence": sources_without_evidence,
    }.items():
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"source_runtime.summary.{key} must be a non-negative integer")

    manual_review_required = bool(blocking_count or warning_count or sources_without_evidence)
    manual_review_reduction_allowed = (
        credential_policy_summary.get("manual_review_reduction_allowed") is True
        and remediation_summary.get("receipt_backed_relief_allowed") is True
        and credential_relief_gate.get("manual_review_reduction_allowed") is True
    )
    manual_review_reduction_status = (
        "allowed_by_reviewed_validated_credential_runtime_receipts"
        if manual_review_reduction_allowed
        else "blocked_until_reviewed_validated_credential_runtime_receipts_exist"
    )
    return {
        "source_runtime_rollup": source_runtime_path.as_posix(),
        "source_runtime_remediation_map": source_runtime_remediation_path.as_posix(),
        "credential_runtime_evidence_policy": credential_runtime_policy_path.as_posix(),
        "credential_runtime_receipt_collection_queue": credential_receipt_queue_path.as_posix(),
        "error_action_routing_rollup": error_action_routing_path.as_posix(),
        "downstream_impact_rollup": impact_path.as_posix(),
        "manual_review_required": manual_review_required,
        "compatibility_effect": "manual_review_required_until_runtime_blockers_resolved"
        if manual_review_required
        else "runtime_evidence_clear",
        "required_contracts": REQUIRED_RUNTIME_RISK_CONTRACTS,
        "blocking_count": blocking_count,
        "warning_count": warning_count,
        "sources_without_evidence": sources_without_evidence,
        "remediation_follow_up_required": remediation_summary.get("follow_up_required"),
        "remediation_manual_review_boundaries": remediation_summary.get("manual_review_boundaries"),
        "remediation_credential_policy_available": remediation_summary.get("credential_policy_available"),
        "remediation_receipt_contract_available": remediation_summary.get("receipt_contract_available"),
        "remediation_reviewed_receipt_intake_available": remediation_summary.get("reviewed_receipt_intake_available"),
        "remediation_receipt_reviewed": remediation_summary.get("receipt_reviewed"),
        "remediation_receipt_relief_eligible": remediation_summary.get("receipt_relief_eligible"),
        "remediation_receipt_backed_relief_allowed": remediation_summary.get("receipt_backed_relief_allowed"),
        "remediation_receipt_backed_relief_status": remediation_summary.get("receipt_backed_relief_status"),
        "credential_policy_sources": credential_policy_summary.get("credential_gated_sources"),
        "credential_policy_manual_review_boundaries": credential_policy_summary.get("manual_review_boundaries"),
        "credential_policy_receipt_contract_available": credential_policy_summary.get("receipt_contract_available"),
        "credential_policy_reviewed_receipt_intake_available": credential_policy_summary.get(
            "reviewed_receipt_intake_available"
        ),
        "credential_policy_receipt_present": credential_policy_summary.get("receipt_present"),
        "credential_policy_receipt_validated": credential_policy_summary.get("receipt_validated"),
        "credential_policy_receipt_reviewed": credential_policy_summary.get("receipt_reviewed"),
        "credential_policy_receipt_relief_eligible": credential_policy_summary.get("receipt_relief_eligible"),
        "credential_policy_manual_review_reduction_allowed": credential_policy_summary.get(
            "manual_review_reduction_allowed"
        ),
        "credential_policy_live_receipts": credential_policy_summary.get("live_credentialed_receipts_checked_in"),
        "credential_policy_reviewed_receipts": credential_policy_summary.get("reviewed_receipts_checked_in"),
        "credential_policy_default_ci_requires_credentials": credential_policy_summary.get(
            "default_ci_requires_credentials"
        ),
        "credential_policy_effect": credential_policy_boundary.get("compatibility_effect"),
        "credential_policy_relief_gate_status": credential_relief_gate.get("status"),
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
        "manual_review_reduction_allowed": manual_review_reduction_allowed,
        "manual_review_reduction_status": manual_review_reduction_status,
        "blocker_ids": ids_from_rollup_items(source_runtime.get("blockers_by_id"), "source_runtime.blockers_by_id"),
        "warning_ids": ids_from_rollup_items(source_runtime.get("warnings_by_id"), "source_runtime.warnings_by_id"),
        "sources": runtime_source_entries(source_runtime),
        "error_action_blocking_rules": routing_summary.get("blocking_rules"),
        "error_action_manual_review_rules": routing_summary.get("manual_review_rules"),
        "downstream_manual_review_changes": impact_summary.get("requires_manual_review"),
    }


def generation_inputs(
    manifest_path: pathlib.Path,
    manifest: dict[str, Any],
    readiness_path: pathlib.Path,
    readiness: dict[str, Any],
    evidence_contracts: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest_generated_at = manifest.get("generated_at")
    artifact_count = manifest.get("artifact_count")
    if not isinstance(manifest_generated_at, str) or not manifest_generated_at:
        raise ValueError("manifest.generated_at must be a non-empty string")
    if not isinstance(artifact_count, int) or artifact_count <= 0:
        raise ValueError("manifest.artifact_count must be a positive integer")
    if readiness.get("schema_version") != "datapan.release-readiness.v1":
        raise ValueError("readiness.schema_version must be datapan.release-readiness.v1")
    readiness_summary = as_dict(readiness.get("summary"), "readiness.summary")
    return {
        "manifest": {
            "path": manifest_path.as_posix(),
            "generated_at": manifest_generated_at,
            "artifact_count": artifact_count,
            "evidence_contracts": len(evidence_contracts),
        },
        "readiness": {
            "path": readiness_path.as_posix(),
            "normalization": "omit_generated_at",
            "normalized_sha256": normalized_readiness_fingerprint(readiness),
            "ready": readiness.get("ready"),
            "gates_total": readiness_summary.get("gates_total"),
            "passed": readiness_summary.get("passed"),
            "failed": readiness_summary.get("failed"),
        },
    }


def build_report(
    manifest: dict[str, Any],
    readiness: dict[str, Any],
    source_runtime: dict[str, Any],
    source_runtime_remediation: dict[str, Any],
    credential_runtime_policy: dict[str, Any],
    credential_receipt_queue: dict[str, Any],
    error_action_routing: dict[str, Any],
    downstream_impact: dict[str, Any],
    *,
    manifest_path: pathlib.Path = DEFAULT_MANIFEST,
    readiness_path: pathlib.Path = DEFAULT_READINESS,
    source_runtime_path: pathlib.Path = DEFAULT_SOURCE_RUNTIME_ROLLUP,
    source_runtime_remediation_path: pathlib.Path = DEFAULT_SOURCE_RUNTIME_REMEDIATION,
    credential_runtime_policy_path: pathlib.Path = DEFAULT_CREDENTIAL_RUNTIME_POLICY,
    credential_receipt_queue_path: pathlib.Path = DEFAULT_CREDENTIAL_RECEIPT_QUEUE,
    error_action_routing_path: pathlib.Path = DEFAULT_ERROR_ACTION_ROUTING_ROLLUP,
    impact_path: pathlib.Path = DEFAULT_IMPACT_ROLLUP,
) -> dict[str, Any]:
    generated_at = require_release_inputs(manifest, readiness)
    consumers = consumer_entries()
    evidence_contracts = manifest_evidence_contracts(manifest)
    return {
        "schema_version": "datapan.release-consumer-compatibility.v1",
        "generated_at": generated_at,
        "provider": "data.go.kr",
        "source_id": "data_go_kr",
        "generation_inputs": generation_inputs(
            manifest_path,
            manifest,
            readiness_path,
            readiness,
            evidence_contracts,
        ),
        "summary": summary_for(consumers),
        "compatibility_path": {
            "path": CANONICAL_REGISTRY_PATH,
            "artifact_kind": "registry",
            "required": True,
            "status": "manifest_bound",
            "proof": [
                "manifest.json artifact kind registry",
                "reports/latest-release-readiness.json registry_has_specs gate",
                "Verify registry release materialized LFS check",
            ],
        },
        "release_health_evidence": {
            "artifact_bundle": "datapan-registry-release-health",
            "required_schemas": REQUIRED_RELEASE_HEALTH_SCHEMAS,
            "required_ci_reports": REQUIRED_CI_REPORTS,
            "rollup_generation_contract": ROLLUP_GENERATION_CONTRACT,
            "required_shard_install_fields": REQUIRED_SHARD_INSTALL_FIELDS,
        },
        "manifest_evidence_contracts": evidence_contracts,
        "runtime_risk_evidence": runtime_risk_evidence(
            source_runtime,
            source_runtime_remediation,
            credential_runtime_policy,
            credential_receipt_queue,
            error_action_routing,
            downstream_impact,
            source_runtime_path=source_runtime_path,
            source_runtime_remediation_path=source_runtime_remediation_path,
            credential_runtime_policy_path=credential_runtime_policy_path,
            credential_receipt_queue_path=credential_receipt_queue_path,
            error_action_routing_path=error_action_routing_path,
            impact_path=impact_path,
        ),
        "shard_policy": {
            "phase": "compatibility_period",
            "asset_name": "data-go-kr-shards.tar.gz",
            "required_for_release": False,
            "publication_status": "optional_draft_asset",
            "monolith_fallback_required": True,
            "downstream_tracking": "StatPan/datapan-cli#128",
            "blocked_until": [
                "catalog install proves shard-preferred monolith fallback",
                "doctor reports shard inventory health as additive metadata",
                "release verify and readiness understand registry_shards artifacts",
                "downstream SDK, MCP, Studio, and API consumers keep canonical registry compatibility",
            ],
        },
        "shard_release_evidence": SHARD_RELEASE_EVIDENCE,
        "consumers": consumers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    parser.add_argument("--readiness", default=DEFAULT_READINESS, type=pathlib.Path)
    parser.add_argument("--source-runtime-rollup", default=DEFAULT_SOURCE_RUNTIME_ROLLUP, type=pathlib.Path)
    parser.add_argument("--source-runtime-remediation", default=DEFAULT_SOURCE_RUNTIME_REMEDIATION, type=pathlib.Path)
    parser.add_argument("--credential-runtime-policy", default=DEFAULT_CREDENTIAL_RUNTIME_POLICY, type=pathlib.Path)
    parser.add_argument("--credential-receipt-queue", default=DEFAULT_CREDENTIAL_RECEIPT_QUEUE, type=pathlib.Path)
    parser.add_argument("--error-action-routing-rollup", default=DEFAULT_ERROR_ACTION_ROUTING_ROLLUP, type=pathlib.Path)
    parser.add_argument("--impact-rollup", default=DEFAULT_IMPACT_ROLLUP, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate that the checked-in compatibility report matches generated output",
    )
    args = parser.parse_args()

    try:
        report = build_report(
            load_json(args.manifest),
            load_json(args.readiness),
            load_json(args.source_runtime_rollup),
            load_json(args.source_runtime_remediation),
            load_json(args.credential_runtime_policy),
            load_json(args.credential_receipt_queue),
            load_json(args.error_action_routing_rollup),
            load_json(args.impact_rollup),
            manifest_path=args.manifest,
            readiness_path=args.readiness,
            source_runtime_path=args.source_runtime_rollup,
            source_runtime_remediation_path=args.source_runtime_remediation,
            credential_runtime_policy_path=args.credential_runtime_policy,
            credential_receipt_queue_path=args.credential_receipt_queue,
            error_action_routing_path=args.error_action_routing_rollup,
            impact_path=args.impact_rollup,
        )
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate release consumer compatibility: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing generated compatibility report", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale compatibility report; "
                "run `python3 scripts/generate-release-consumer-compatibility.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (consumers={report['summary']['consumer_count']})")
        return 0

    write_json(args.output, report)
    print(f"wrote {args.output} (consumers={report['summary']['consumer_count']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
