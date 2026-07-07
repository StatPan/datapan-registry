#!/usr/bin/env python3
"""Generate or check shard-preferred consumer proof evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating shard consumer proof") from exc


DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_FOOTPRINT = pathlib.Path("reports/release-distribution-footprint.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.release-shard-consumer-proof.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-shard-consumer-proof.json")
VERIFY_WORKFLOW = pathlib.Path(".github/workflows/verify-release.yml")
SCHEMA_VERSION = "datapan.release-shard-consumer-proof.v1"
CANONICAL_REGISTRY_PATH = "data/data-go-kr.registry.json"
SHARD_ARCHIVE_NAME = "data-go-kr-shards.tar.gz"
REQUIRED_CI_SUMMARIES = [
    ".datapan/ci/current-release-install-smoke.json",
    ".datapan/ci/current-release-doctor-smoke.json",
    ".datapan/ci/latest-release-install-smoke.json",
    ".datapan/ci/latest-release-doctor-smoke.json",
    ".datapan/ci/release-health-rollup.json",
]
REQUIRED_COMMAND_FRAGMENTS = [
    "python scripts/generate-registry-shards.py",
    "python scripts/validate-registry-shards.py",
    "python scripts/package-registry-shards.py",
    "scripts/check-shard-aware-install-smoke.py",
    "scripts/check-release-doctor-smoke.py",
    "scripts/generate-release-health-rollup.py",
    "scripts/validate-release-health-rollups.py",
    "catalog install datapan-registry",
    "go run ./cmd/datapan doctor --json",
]


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def count(value: object, label: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def workflow_text(path: pathlib.Path) -> str:
    if not path.is_file():
        raise ValueError(f"verify workflow is missing: {path}")
    raw = path.read_text(encoding="utf-8")
    normalized = re.sub(r"\\\n\s*", " ", raw)
    return re.sub(r"\s+", " ", normalized)


def workflow_has_fragments(path: pathlib.Path, fragments: list[str]) -> tuple[bool, list[str]]:
    text = workflow_text(path)
    missing = sorted(fragment for fragment in fragments if fragment not in text)
    return not missing, missing


def manifest_artifact(manifest: dict[str, Any], path: str) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be an array")
    for index, artifact in enumerate(artifacts):
        item = as_dict(artifact, f"manifest.artifacts[{index}]")
        if item.get("path") == path:
            return item
    raise ValueError(f"manifest missing artifact: {path}")


def build_report(
    manifest: dict[str, Any],
    footprint: dict[str, Any],
    *,
    workflow_path: pathlib.Path,
) -> dict[str, Any]:
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("manifest.generated_at must be a non-empty string")

    registry = manifest_artifact(manifest, CANONICAL_REGISTRY_PATH)
    if registry.get("kind") != "registry":
        raise ValueError(f"{CANONICAL_REGISTRY_PATH} must be a registry artifact")

    footprint_summary = as_dict(footprint.get("summary"), "footprint.summary")
    footprint_boundary = as_dict(footprint.get("distribution_boundary"), "footprint.distribution_boundary")
    registry_bytes = count(footprint_summary.get("canonical_registry_bytes"), "footprint.summary.canonical_registry_bytes")
    threshold_bytes = count(
        footprint_summary.get("large_monolith_threshold_bytes"),
        "footprint.summary.large_monolith_threshold_bytes",
    )

    workflow_ok, missing_fragments = workflow_has_fragments(workflow_path, REQUIRED_COMMAND_FRAGMENTS)
    monolith_fallback_proven = (
        workflow_ok
        and footprint_summary.get("canonical_registry_required") is True
        and footprint_summary.get("monolith_fallback_required") is True
        and footprint_boundary.get("canonical_registry_compatible") is True
        and footprint_boundary.get("release_package_includes_monolith") is True
    )
    shard_preferred_ready = (
        monolith_fallback_proven
        and registry_bytes > threshold_bytes
        and footprint_summary.get("shard_distribution_required") is False
    )
    distribution_action_resolved = shard_preferred_ready and footprint_boundary.get(
        "next_distribution_action"
    ) == "prove_shard_preferred_install_with_canonical_fallback_before_requiring_shards"

    proof_scope = [
        {
            "surface": "current_release_install",
            "summary_artifact": ".datapan/ci/current-release-install-smoke.json",
            "required_mode": "monolith_fallback_or_shard_validated",
            "fallback_required": True,
            "checked_by": "scripts/check-shard-aware-install-smoke.py",
        },
        {
            "surface": "current_release_doctor",
            "summary_artifact": ".datapan/ci/current-release-doctor-smoke.json",
            "required_mode": "doctor_matches_installed_registry",
            "fallback_required": True,
            "checked_by": "scripts/check-release-doctor-smoke.py",
        },
        {
            "surface": "latest_release_install",
            "summary_artifact": ".datapan/ci/latest-release-install-smoke.json",
            "required_mode": "monolith_fallback_or_shard_validated",
            "fallback_required": True,
            "checked_by": "scripts/check-shard-aware-install-smoke.py",
        },
        {
            "surface": "latest_release_doctor",
            "summary_artifact": ".datapan/ci/latest-release-doctor-smoke.json",
            "required_mode": "doctor_matches_installed_registry",
            "fallback_required": True,
            "checked_by": "scripts/check-release-doctor-smoke.py",
        },
        {
            "surface": "release_health_rollup",
            "summary_artifact": ".datapan/ci/release-health-rollup.json",
            "required_mode": "current_and_latest_smoke_summaries_pass",
            "fallback_required": True,
            "checked_by": "scripts/validate-release-health-rollups.py",
        },
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "proof_ticket": 441,
        "provider": "datapan-registry",
        "inputs": {
            "manifest": DEFAULT_MANIFEST.as_posix(),
            "release_distribution_footprint": DEFAULT_FOOTPRINT.as_posix(),
            "verify_workflow": workflow_path.as_posix(),
            "ci_summaries": REQUIRED_CI_SUMMARIES,
        },
        "summary": {
            "proof_status": "shard_preferred_fallback_proven"
            if shard_preferred_ready
            else "shard_preferred_fallback_unproven",
            "shard_preferred_ready": shard_preferred_ready,
            "monolith_fallback_proven": monolith_fallback_proven,
            "distribution_action_resolved": distribution_action_resolved,
            "canonical_registry_required": True,
            "shard_assets_required": False,
            "checked_in_large_shards": False,
        },
        "registry_boundary": {
            "canonical_registry_path": CANONICAL_REGISTRY_PATH,
            "canonical_registry_bytes": registry_bytes,
            "large_monolith_threshold_bytes": threshold_bytes,
            "canonical_registry_sha256": registry.get("sha256"),
            "release_package_includes_monolith": footprint_boundary.get("release_package_includes_monolith"),
            "monolith_fallback_required": footprint_summary.get("monolith_fallback_required"),
            "shard_archive_name": SHARD_ARCHIVE_NAME,
            "shard_archive_publication": "optional_release_asset",
        },
        "workflow_proof": {
            "workflow": workflow_path.as_posix(),
            "required_fragments": REQUIRED_COMMAND_FRAGMENTS,
            "missing_fragments": missing_fragments,
            "workflow_contract_present": workflow_ok,
        },
        "consumer_proof_scope": proof_scope,
        "release_policy": {
            "consumer_effect": "shard_preferred_supported_with_canonical_fallback"
            if distribution_action_resolved
            else "canonical_registry_required_shards_optional",
            "required_for_release": False,
            "require_shards_next": False,
            "goal_completion_effect": "distribution_pressure_evidence_only_goal_still_requires_credential_and_manual_review_gates",
        },
    }


def validate_invariants(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    boundary = as_dict(report.get("registry_boundary"), "registry_boundary")
    workflow = as_dict(report.get("workflow_proof"), "workflow_proof")
    policy = as_dict(report.get("release_policy"), "release_policy")

    if summary.get("canonical_registry_required") is not True:
        raise ValueError("canonical registry must remain required")
    if summary.get("shard_assets_required") is not False:
        raise ValueError("shard assets must remain optional")
    if summary.get("checked_in_large_shards") is not False:
        raise ValueError("large shard JSON files must not be checked in")
    if boundary.get("monolith_fallback_required") is not True:
        raise ValueError("monolith fallback must remain required")
    if boundary.get("release_package_includes_monolith") is not True:
        raise ValueError("release package must include the monolith registry")
    if summary.get("shard_preferred_ready") is True and workflow.get("workflow_contract_present") is not True:
        raise ValueError("shard_preferred_ready requires workflow proof")
    if summary.get("distribution_action_resolved") is True:
        if policy.get("consumer_effect") != "shard_preferred_supported_with_canonical_fallback":
            raise ValueError("resolved distribution action must expose shard-preferred fallback effect")
        if policy.get("required_for_release") is not False:
            raise ValueError("resolved proof must not make shards required for release")
    if policy.get("require_shards_next") is not False:
        raise ValueError("consumer proof must not require shards as the next release policy")


def validate_schema(report: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("; ".join(rendered))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    parser.add_argument("--footprint", default=DEFAULT_FOOTPRINT, type=pathlib.Path)
    parser.add_argument("--workflow", default=VERIFY_WORKFLOW, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in proof evidence is stale")
    args = parser.parse_args()

    try:
        report = build_report(load_json(args.manifest), load_json(args.footprint), workflow_path=args.workflow)
        validate_invariants(report)
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate release shard consumer proof: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing release shard consumer proof", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale release shard consumer proof; "
                "run `python3 scripts/generate-release-shard-consumer-proof.py`",
                file=sys.stderr,
            )
            return 1
        print(
            "ok "
            f"{args.output} "
            f"(proof_status={report['summary']['proof_status']})"
        )
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(
        "wrote "
        f"{args.output} "
        f"(proof_status={report['summary']['proof_status']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
