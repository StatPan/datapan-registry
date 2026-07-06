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
    manifest_path: pathlib.Path,
    readiness_path: pathlib.Path,
) -> None:
    validate_generation_inputs(report, manifest, readiness, manifest_path, readiness_path)
    validate_summary(report)
    validate_manifest_links(report, manifest)
    validate_readiness(report, readiness)
    validate_shard_policy(report)
    validate_shard_release_evidence(report)
    validate_manifest_evidence_contracts(report, manifest)
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
    parser.add_argument("report", nargs="?", default=COMPATIBILITY_REPORT_PATH, type=pathlib.Path)
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        report = as_dict(load_json(args.report), args.report.as_posix())
        manifest = as_dict(load_json(args.manifest), args.manifest.as_posix())
        readiness = as_dict(load_json(args.readiness), args.readiness.as_posix())

        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
        if errors:
            print(f"FAIL {args.report}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            return 1

        validate_consistency(report, manifest, readiness, args.manifest, args.readiness)
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
