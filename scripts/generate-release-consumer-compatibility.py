#!/usr/bin/env python3
"""Generate the datapan-registry release consumer compatibility report."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
from typing import Any


CANONICAL_REGISTRY_PATH = "data/data-go-kr.registry.json"
DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_READINESS = pathlib.Path("reports/latest-release-readiness.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-consumer-compatibility.json")
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
REQUIRED_SHARD_INSTALL_FIELDS = [
    "mode",
    "shards_asset_present",
    "shards_validated",
    "shards_inventory_present",
    "shards_count",
    "shards_records",
]
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


def build_report(manifest: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    generated_at = require_release_inputs(manifest, readiness)
    consumers = consumer_entries()
    return {
        "schema_version": "datapan.release-consumer-compatibility.v1",
        "generated_at": generated_at,
        "provider": "data.go.kr",
        "source_id": "data_go_kr",
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
            "required_shard_install_fields": REQUIRED_SHARD_INSTALL_FIELDS,
        },
        "manifest_evidence_contracts": manifest_evidence_contracts(manifest),
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
        "consumers": consumers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    parser.add_argument("--readiness", default=DEFAULT_READINESS, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate that the checked-in compatibility report matches generated output",
    )
    args = parser.parse_args()

    try:
        report = build_report(load_json(args.manifest), load_json(args.readiness))
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
