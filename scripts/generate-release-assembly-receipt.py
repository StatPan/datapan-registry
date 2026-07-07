#!/usr/bin/env python3
"""Generate or check the release ledger assembly receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating release assembly receipts") from exc


DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_READINESS = pathlib.Path("reports/latest-release-readiness.json")
DEFAULT_COMPATIBILITY = pathlib.Path("reports/release-consumer-compatibility.json")
DEFAULT_GOAL_AUDIT = pathlib.Path("docs/release-ledger-goal-completion-audit.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.release-assembly-receipt.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/release-assembly-receipt.json")
VERIFY_WORKFLOW = pathlib.Path(".github/workflows/verify-release.yml")
RELEASE_DRAFT_WORKFLOW = pathlib.Path(".github/workflows/release-draft.yml")
SCHEMA_VERSION = "datapan.release-assembly-receipt.v1"


PHASES: list[dict[str, Any]] = [
    {
        "id": "materialize_canonical_registry",
        "coverage": ["registry"],
        "operator_commands": [
            "git lfs pull --include data/data-go-kr.registry.json",
        ],
        "check_commands": [
            "test $(wc -c < data/data-go-kr.registry.json) -gt 100000000",
            "datapan catalog release verify --manifest manifest.json",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "Confirm registry LFS file is materialized",
                "test \"${bytes}\" -gt 100000000",
            ],
            ".github/workflows/release-draft.yml": [
                "Confirm registry LFS file is materialized",
                "test \"${bytes}\" -gt",
            ],
        },
        "evidence_artifacts": ["data/data-go-kr.registry.json"],
    },
    {
        "id": "sync_schema_artifacts",
        "coverage": ["schema"],
        "operator_commands": [
            "python3 scripts/sync-release-schema-artifacts.py --write",
        ],
        "check_commands": [
            "python3 scripts/sync-release-schema-artifacts.py --check",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "python scripts/sync-release-schema-artifacts.py --check",
            ],
            ".github/workflows/release-draft.yml": [
                "python scripts/sync-release-schema-artifacts.py --check",
            ],
        },
        "evidence_artifacts": ["schemas/index.json"],
    },
    {
        "id": "sync_manifest_artifacts",
        "coverage": ["registry_artifacts", "evidence"],
        "operator_commands": [
            "python3 scripts/sync-release-manifest-artifacts.py --write",
        ],
        "check_commands": [
            "python3 scripts/sync-release-manifest-artifacts.py --check",
            "python3 scripts/validate-release-ledger-ownership.py",
            "python3 scripts/validate-release-report-artifacts.py",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "python scripts/sync-release-manifest-artifacts.py --check",
                "python scripts/validate-release-ledger-ownership.py",
                "python scripts/validate-release-report-artifacts.py",
            ],
            ".github/workflows/release-draft.yml": [
                "python scripts/sync-release-manifest-artifacts.py --check",
                "python scripts/validate-release-ledger-ownership.py",
                "python scripts/validate-release-report-artifacts.py",
            ],
        },
        "evidence_artifacts": ["docs/release-ledger-ownership.json"],
    },
    {
        "id": "source_contract_and_runtime_evidence",
        "coverage": ["source_contracts", "verification_evidence"],
        "operator_commands": [
            "python3 scripts/generate-source-contract-rollup.py",
            "python3 scripts/generate-source-report-inventory.py",
            "python3 scripts/generate-source-runtime-readiness.py",
        ],
        "check_commands": [
            "python3 scripts/generate-source-contract-rollup.py --check",
            "python3 scripts/validate-source-contract-rollups.py",
            "python3 scripts/validate-source-report-inventory.py",
            "python3 scripts/validate-source-runtime-evidence-rollup.py",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "python scripts/generate-source-contract-rollup.py --check",
                "python scripts/validate-source-report-inventory.py",
                "python scripts/validate-source-runtime-evidence-rollup.py",
            ],
            ".github/workflows/release-draft.yml": [
                "python scripts/generate-source-contract-rollup.py --check",
                "python scripts/validate-source-report-inventory.py",
                "python scripts/validate-source-runtime-evidence-rollup.py",
            ],
        },
        "evidence_artifacts": [
            "reports/source-contract-rollup.json",
            "reports/source-report-inventory.json",
            "reports/source-runtime-evidence-rollup.json",
        ],
    },
    {
        "id": "error_action_and_downstream_impact",
        "coverage": ["error_action_routing", "downstream_impact"],
        "operator_commands": [
            "python3 scripts/generate-error-action-routing-rollup.py",
            "python3 scripts/generate-impact-plan-rollup.py",
        ],
        "check_commands": [
            "python3 scripts/generate-error-action-routing-rollup.py --check",
            "python3 scripts/validate-error-action-routing-rollups.py",
            "python3 scripts/generate-impact-plan-rollup.py --check",
            "python3 scripts/validate-impact-plans.py",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "python scripts/generate-error-action-routing-rollup.py --check",
                "python scripts/generate-impact-plan-rollup.py --check",
                "python scripts/validate-impact-plans.py",
            ],
            ".github/workflows/release-draft.yml": [
                "python scripts/generate-error-action-routing-rollup.py --check",
                "python scripts/generate-impact-plan-rollup.py --check",
                "python scripts/validate-impact-plans.py",
            ],
        },
        "evidence_artifacts": [
            "reports/error-action-routing-rollup.json",
            "reports/registry-impact-plan.json",
        ],
    },
    {
        "id": "source_runtime_remediation_map",
        "coverage": ["source_contracts", "verification_evidence", "consumer_compatibility"],
        "operator_commands": [
            "python3 scripts/generate-source-runtime-remediation-map.py",
        ],
        "check_commands": [
            "python3 scripts/generate-source-runtime-remediation-map.py --check",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "python scripts/generate-source-runtime-remediation-map.py --check",
            ],
            ".github/workflows/release-draft.yml": [
                "python scripts/generate-source-runtime-remediation-map.py --check",
            ],
        },
        "evidence_artifacts": ["reports/source-runtime-remediation-map.json"],
    },
    {
        "id": "credential_runtime_evidence_policy",
        "coverage": ["verification_evidence", "consumer_compatibility"],
        "operator_commands": [
            "python3 scripts/generate-credential-runtime-evidence-policy.py",
        ],
        "check_commands": [
            "python3 scripts/generate-credential-runtime-evidence-policy.py --check",
            "python3 scripts/validate-credential-runtime-receipts.py",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "python scripts/generate-credential-runtime-evidence-policy.py --check",
                "python scripts/validate-credential-runtime-receipts.py",
            ],
            ".github/workflows/release-draft.yml": [
                "python scripts/generate-credential-runtime-evidence-policy.py --check",
                "python scripts/validate-credential-runtime-receipts.py",
            ],
        },
        "evidence_artifacts": ["reports/credential-runtime-evidence-policy.json"],
    },
    {
        "id": "credential_runtime_receipt_collection_queue",
        "coverage": ["verification_evidence", "consumer_compatibility"],
        "operator_commands": [
            "python3 scripts/generate-credential-runtime-receipt-collection-queue.py",
            "python3 scripts/generate-credential-runtime-review-handoff.py",
            "python3 scripts/validate-credential-runtime-manual-review-decision.py",
            "python3 scripts/generate-credential-runtime-manual-review-acceptance.py",
            "python3 scripts/run-credential-runtime-collection.py --source <source_id> --json",
            "python3 scripts/run-credential-runtime-collection.py --source <source_id> --run",
            "python3 scripts/promote-credential-runtime-receipt.py <staged-receipt> --state <reviewed_accepted|reviewed_rejected> --decision <decision> --reviewer <reviewer> --reason <reason>",
        ],
        "check_commands": [
            "python3 scripts/generate-credential-runtime-receipt-collection-queue.py --check",
            "python3 scripts/generate-credential-runtime-review-handoff.py --check",
            "python3 scripts/validate-credential-runtime-manual-review-decision.py",
            "python3 scripts/generate-credential-runtime-manual-review-acceptance.py --check",
            "python3 scripts/run-credential-runtime-collection.py --self-test",
            "python3 scripts/run-credential-runtime-collection.py --check",
            "python3 -m py_compile scripts/promote-credential-runtime-receipt.py",
            "python3 scripts/promote-credential-runtime-receipt.py --self-test",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "python scripts/generate-credential-runtime-receipt-collection-queue.py --check",
                "python scripts/generate-credential-runtime-review-handoff.py --check",
                "python scripts/validate-credential-runtime-manual-review-decision.py",
                "python scripts/generate-credential-runtime-manual-review-acceptance.py --check",
                "python scripts/run-credential-runtime-collection.py --self-test",
                "python scripts/run-credential-runtime-collection.py --check",
                "python -m py_compile scripts/promote-credential-runtime-receipt.py",
                "python scripts/promote-credential-runtime-receipt.py --self-test",
            ],
            ".github/workflows/release-draft.yml": [
                "python scripts/generate-credential-runtime-receipt-collection-queue.py --check",
                "python scripts/generate-credential-runtime-review-handoff.py --check",
                "python scripts/validate-credential-runtime-manual-review-decision.py",
                "python scripts/generate-credential-runtime-manual-review-acceptance.py --check",
                "python scripts/run-credential-runtime-collection.py --self-test",
                "python scripts/run-credential-runtime-collection.py --check",
                "scripts/promote-credential-runtime-receipt.py",
                "python scripts/promote-credential-runtime-receipt.py --self-test",
            ],
        },
        "evidence_artifacts": [
            "reports/credential-runtime-receipt-collection-queue.json",
            "reports/credential-runtime-review-handoff.json",
            "reports/credential-runtime-manual-review-decision.json",
            "reports/credential-runtime-manual-review-acceptance.json",
        ],
    },
    {
        "id": "consumer_compatibility",
        "coverage": ["consumer_compatibility", "release_distribution"],
        "operator_commands": [
            "python3 scripts/generate-release-distribution-footprint.py",
            "python3 scripts/generate-release-consumer-compatibility.py",
            "python3 scripts/generate-release-consumer-decision.py",
        ],
        "check_commands": [
            "python3 scripts/generate-release-distribution-footprint.py --check",
            "python3 scripts/generate-release-consumer-compatibility.py --check",
            "python3 scripts/validate-release-consumer-compatibility.py",
            "python3 scripts/generate-release-consumer-decision.py --check",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "python scripts/generate-release-distribution-footprint.py --check",
                "python scripts/generate-release-consumer-compatibility.py --check",
                "python scripts/validate-release-consumer-compatibility.py",
                "python scripts/generate-release-consumer-decision.py --check",
            ],
            ".github/workflows/release-draft.yml": [
                "python scripts/generate-release-distribution-footprint.py --check",
                "python scripts/generate-release-consumer-compatibility.py --check",
                "python scripts/validate-release-consumer-compatibility.py",
                "python scripts/generate-release-consumer-decision.py --check",
            ],
        },
        "evidence_artifacts": [
            "reports/release-distribution-footprint.json",
            "reports/release-consumer-compatibility.json",
            "reports/release-consumer-decision.json",
        ],
    },
    {
        "id": "release_verification_and_readiness",
        "coverage": ["verification_evidence", "registry_artifacts"],
        "operator_commands": [
            "datapan catalog release verify --manifest manifest.json --output reports/latest-release-verification.json --json",
            "datapan catalog release readiness --manifest manifest.json --output reports/latest-release-readiness.json --json",
        ],
        "check_commands": [
            "python3 scripts/validate-release-receipt-boundary.py",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "go run ./cmd/datapan catalog release verify",
                "go run ./cmd/datapan catalog release readiness",
                "python scripts/validate-release-receipt-boundary.py",
            ],
            ".github/workflows/release-draft.yml": [
                "go run ./cmd/datapan catalog release verify",
                "go run ./cmd/datapan catalog release readiness",
                "python scripts/validate-release-receipt-boundary.py",
            ],
        },
        "evidence_artifacts": [
            "reports/latest-release-verification.json",
            "reports/latest-release-readiness.json",
        ],
    },
    {
        "id": "shard_release_distribution",
        "coverage": ["shard_release_distribution"],
        "operator_commands": [
            "python3 scripts/generate-registry-shards.py",
            "python3 scripts/package-registry-shards.py",
        ],
        "check_commands": [
            "python3 scripts/validate-registry-shards.py",
            "python3 scripts/package-registry-shards.py --check",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "Validate full registry shard release evidence",
                "python scripts/generate-registry-shards.py",
                "python scripts/validate-registry-shards.py",
                "python scripts/package-registry-shards.py",
                "python scripts/package-registry-shards.py",
            ],
            ".github/workflows/release-draft.yml": [
                "Generate shard archive release evidence",
                "python scripts/generate-registry-shards.py",
                "python scripts/validate-registry-shards.py",
                "python scripts/package-registry-shards.py",
                "python scripts/package-registry-release.py",
            ],
        },
        "ci_outputs": [
            ".datapan/ci/full-registry-shards/registry-shards.json",
            ".datapan/ci/full-data-go-kr-shards.tar.gz",
            ".datapan/ci/full-shard-archive-check.txt",
        ],
    },
    {
        "id": "release_package_distribution",
        "coverage": ["release_distribution"],
        "operator_commands": [
            "python3 scripts/package-registry-release.py --manifest manifest.json --output .datapan/release-assets/datapan-registry-snapshot.zip",
        ],
        "check_commands": [
            "python3 scripts/package-registry-release.py --check .datapan/release-assets/datapan-registry-snapshot.zip",
            "python3 scripts/package-registry-release.py --check .datapan/release-assets/datapan-registry-snapshot.zip --shard-archive .datapan/ci/full-data-go-kr-shards.tar.gz",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "Package current release zip",
                "python scripts/package-registry-release.py",
                "--shard-archive .datapan/ci/full-data-go-kr-shards.tar.gz",
            ],
            ".github/workflows/release-draft.yml": [
                "Package release zip asset evidence",
                "python scripts/package-registry-release.py",
                "--shard-archive .datapan/release-assets/data-go-kr-shards.tar.gz",
            ],
        },
        "ci_outputs": [
            ".datapan/release-assets/datapan-registry-snapshot.zip",
            ".datapan/ci/current-release-zip-check.txt",
            ".datapan/ci/current-release-shard-consistency-check.txt",
        ],
    },
    {
        "id": "release_assembly_receipt",
        "coverage": ["evidence"],
        "operator_commands": [
            "python3 scripts/generate-release-assembly-receipt.py",
        ],
        "check_commands": [
            "python3 scripts/generate-release-assembly-receipt.py --check",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "python scripts/generate-release-assembly-receipt.py --check",
            ],
            ".github/workflows/release-draft.yml": [
                "python scripts/generate-release-assembly-receipt.py --check",
            ],
        },
    },
    {
        "id": "goal_completion_audit",
        "coverage": ["evidence"],
        "operator_commands": [
            "python3 scripts/validate-release-ledger-goal-audit.py",
            "python3 scripts/generate-release-goal-finish-preflight.py",
        ],
        "check_commands": [
            "python3 scripts/validate-release-ledger-goal-audit.py",
            "python3 scripts/generate-release-goal-finish-preflight.py --check",
        ],
        "workflow_fragments": {
            ".github/workflows/verify-release.yml": [
                "python scripts/validate-release-ledger-goal-audit.py",
                "python scripts/generate-release-goal-finish-preflight.py --check",
            ],
            ".github/workflows/release-draft.yml": [
                "python scripts/validate-release-ledger-goal-audit.py",
                "python scripts/generate-release-goal-finish-preflight.py --check",
            ],
        },
        "evidence_artifacts": [
            "docs/release-ledger-goal-completion-audit.json",
            "reports/release-goal-finish-preflight.json",
        ],
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


def file_digest(path: pathlib.Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


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
    for index, raw_artifact in enumerate(as_list(manifest.get("artifacts"), "manifest.artifacts")):
        artifact = as_dict(raw_artifact, f"manifest.artifacts[{index}]")
        path = artifact.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"manifest.artifacts[{index}].path must be a non-empty string")
        artifacts[path] = artifact
    return artifacts


def artifact_evidence(path_value: str, artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    path = pathlib.Path(path_value)
    if not path.is_file():
        raise ValueError(f"release assembly evidence path is missing: {path_value}")
    bytes_value, sha256 = file_digest(path)
    result: dict[str, Any] = {
        "path": path_value,
        "bytes": bytes_value,
        "sha256": sha256,
        "manifest_bound": path_value in artifacts,
    }
    artifact = artifacts.get(path_value)
    if artifact is not None:
        result["kind"] = artifact.get("kind")
        if "schema" in artifact:
            result["schema"] = artifact.get("schema")
        for key in ("bytes", "sha256"):
            if artifact.get(key) != result[key]:
                raise ValueError(f"manifest artifact {path_value} has stale {key}")
    return result


def phase_entry(phase: dict[str, Any], artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    entry = {
        "id": phase["id"],
        "coverage": phase["coverage"],
        "operator_commands": phase["operator_commands"],
        "check_commands": phase["check_commands"],
        "workflow_fragments": phase["workflow_fragments"],
        "status": "required",
    }
    evidence_artifacts = [
        artifact_evidence(path, artifacts)
        for path in phase.get("evidence_artifacts", [])
    ]
    if evidence_artifacts:
        entry["evidence_artifacts"] = evidence_artifacts
    if phase.get("ci_outputs"):
        entry["ci_outputs"] = phase["ci_outputs"]
    return entry


def build_report(
    manifest: dict[str, Any],
    readiness: dict[str, Any],
    compatibility: dict[str, Any],
    goal_audit: dict[str, Any],
) -> dict[str, Any]:
    artifacts = manifest_artifacts(manifest)
    readiness_summary = as_dict(readiness.get("summary"), "readiness.summary")
    compatibility_summary = as_dict(compatibility.get("summary"), "compatibility.summary")
    runtime_risk = as_dict(compatibility.get("runtime_risk_evidence"), "compatibility.runtime_risk_evidence")
    goal_summary = as_dict(goal_audit.get("summary"), "goal_audit.summary")

    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("manifest.generated_at must be a non-empty string")

    manual_review_required = runtime_risk.get("manual_review_required") is True
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "assembly_ticket": 358,
        "provider": "datapan-registry",
        "operator_command": "python3 scripts/generate-release-assembly-receipt.py --check",
        "receipt_contract": {
            "generator": "scripts/generate-release-assembly-receipt.py",
            "schema": DEFAULT_SCHEMA.as_posix(),
            "output": DEFAULT_OUTPUT.as_posix(),
            "validator": "python3 scripts/generate-release-assembly-receipt.py --check",
        },
        "summary": {
            "phase_count": len(PHASES),
            "release_ready": readiness.get("ready"),
            "readiness_failed": readiness_summary.get("failed"),
            "manual_review_required": manual_review_required,
            "assembly_status": "manual_review_required" if manual_review_required else "ready",
            "goal_status": goal_audit.get("goal_status"),
            "goal_criteria_proven": goal_summary.get("criteria_proven"),
            "goal_criteria_gap": goal_summary.get("criteria_gap"),
        },
        "release_inputs": {
            "manifest": {
                "path": DEFAULT_MANIFEST.as_posix(),
                "generated_at": generated_at,
                "artifact_count": manifest.get("artifact_count"),
                "source_registry": manifest.get("source_registry"),
            },
            "readiness": {
                "path": DEFAULT_READINESS.as_posix(),
                "ready": readiness.get("ready"),
                "gates_total": readiness_summary.get("gates_total"),
                "passed": readiness_summary.get("passed"),
                "failed": readiness_summary.get("failed"),
            },
            "consumer_compatibility": {
                "path": DEFAULT_COMPATIBILITY.as_posix(),
                "consumer_count": compatibility_summary.get("consumer_count"),
                "blocked_consumers": compatibility_summary.get("blocked_consumers"),
                "manual_review_required": manual_review_required,
                "runtime_blocking_count": runtime_risk.get("blocking_count"),
                "runtime_warning_count": runtime_risk.get("warning_count"),
            },
            "goal_audit": {
                "path": DEFAULT_GOAL_AUDIT.as_posix(),
                "goal_status": goal_audit.get("goal_status"),
                "decision": goal_summary.get("decision"),
            },
        },
        "phases": [phase_entry(phase, artifacts) for phase in PHASES],
    }


def normalized_workflow(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\\\n\s*", " ", text)
    return re.sub(r"\s+", " ", text)


def validate_workflow_fragments(report: dict[str, Any]) -> None:
    workflows = {
        VERIFY_WORKFLOW.as_posix(): normalized_workflow(VERIFY_WORKFLOW),
        RELEASE_DRAFT_WORKFLOW.as_posix(): normalized_workflow(RELEASE_DRAFT_WORKFLOW),
    }
    for phase_index, raw_phase in enumerate(as_list(report.get("phases"), "phases")):
        phase = as_dict(raw_phase, f"phases[{phase_index}]")
        phase_id = phase.get("id", f"#{phase_index}")
        fragments_by_workflow = as_dict(phase.get("workflow_fragments"), f"{phase_id}.workflow_fragments")
        for workflow_path, raw_fragments in fragments_by_workflow.items():
            workflow = workflows.get(workflow_path)
            if workflow is None:
                raise ValueError(f"{phase_id} references unexpected workflow: {workflow_path}")
            fragments = as_list(raw_fragments, f"{phase_id}.workflow_fragments.{workflow_path}")
            missing = [str(fragment) for fragment in fragments if str(fragment) not in workflow]
            if missing:
                raise ValueError(f"{phase_id} workflow {workflow_path} missing fragments: {', '.join(missing)}")


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
    parser.add_argument("--readiness", default=DEFAULT_READINESS, type=pathlib.Path)
    parser.add_argument("--compatibility", default=DEFAULT_COMPATIBILITY, type=pathlib.Path)
    parser.add_argument("--goal-audit", default=DEFAULT_GOAL_AUDIT, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when the checked-in receipt is stale")
    args = parser.parse_args()

    try:
        report = build_report(
            load_json(args.manifest),
            load_json(args.readiness),
            load_json(args.compatibility),
            load_json(args.goal_audit),
        )
        validate_schema(report, args.schema)
        validate_workflow_fragments(report)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate release assembly receipt: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing release assembly receipt", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale release assembly receipt; "
                "run `python3 scripts/generate-release-assembly-receipt.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (phases={report['summary']['phase_count']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (phases={report['summary']['phase_count']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
