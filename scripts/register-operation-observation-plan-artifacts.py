#!/usr/bin/env python3
"""Bind the observation-plan schema, index, and shards into manifest.json."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest.json"
INDEX_PATH = ROOT / "reports/operation-observation-plan/index.json"
SHARD_ROOT = ROOT / "reports/operation-observation-plan/shards"
DOCUMENT_EVIDENCE_ROOT = ROOT / "reports/operation-document-evidence"
SCHEMA_ID = "https://schemas.datapan.dev/datapan.operation-observation-plan.v1.schema.json"
DOCUMENT_EVIDENCE_SCHEMA_ID = "https://schemas.datapan.dev/datapan.operation-document-evidence.v1.schema.json"
DOCUMENT_EVIDENCE_V2_SCHEMA_ID = "https://schemas.datapan.dev/datapan.operation-document-evidence.v2.schema.json"
DOCUMENT_CAPTURE_RECEIPT_V2_SCHEMA_ID = "https://schemas.datapan.dev/datapan.operation-document-capture-receipt.v2.schema.json"
DOCUMENT_WORK_ITEM_V1_SCHEMA_ID = "https://schemas.datapan.dev/datapan.operation-document-work-item.v1.schema.json"
DOCUMENT_WORK_ITEM_V2_SCHEMA_ID = "https://schemas.datapan.dev/datapan.operation-document-work-item.v2.schema.json"
DOCUMENT_RECONCILIATION_V1_SCHEMA_ID = "https://schemas.datapan.dev/datapan.operation-document-reconciliation.v1.schema.json"
DOCUMENT_RECONCILIATION_V2_SCHEMA_ID = "https://schemas.datapan.dev/datapan.operation-document-reconciliation.v2.schema.json"
POLICY_SCHEMA_ID = "https://schemas.datapan.dev/datapan.operation-observation-policy.v1.schema.json"
ASSERTION_SCHEMA_ID = "https://schemas.datapan.dev/datapan.operation-response-assertion.v2.schema.json"
MANAGED_KINDS = {
    "operation_document_evidence",
    "operation_document_work_queue",
    "operation_document_reconciliation",
    "operation_observation_plan",
    "operation_observation_plan_shard",
    "operation_observation_policy",
    "operation_response_assertion",
    "operation_observation_plan_source",
}
MANAGED_PREFIX = "reports/operation-observation-plan/"
DOCUMENT_EVIDENCE_PREFIX = "reports/operation-document-evidence/"
ASSERTION_PREFIX = "reports/operation-response-assertions/"
POLICY_PATH = "policy/operation-observation-policies.v1.json"
GENERATOR_PATH = "scripts/generate-operation-observation-plan.py"
PARSER_PATH = "scripts/operation_document_evidence.py"


def digest(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def artifact(path: Path, kind: str, *, schema: str | None = None) -> dict[str, Any]:
    relative = path.relative_to(ROOT).as_posix()
    byte_count, checksum = digest(path)
    result: dict[str, Any] = {"path": relative, "kind": kind, "bytes": byte_count, "sha256": checksum}
    if schema:
        result["schema"] = schema
    return result


def operation_source_artifact(path: Path, existing: dict[str, dict[str, Any]]) -> dict[str, Any]:
    relative = path.relative_to(ROOT).as_posix()
    current = existing.get(relative)
    # The schema index is a manifest artifact in its own right, but its path
    # shares the schemas/ prefix. Preserve its distinct kind so the schema
    # synchronizer can replace only actual schema rows.
    if relative == "schemas/index.json":
        return artifact(
            path,
            "schema_index",
            schema="https://schemas.datapan.dev/datapan.schema-index.v1.schema.json",
        )
    if relative.startswith("schemas/"):
        return artifact(path, "schema")
    if relative == POLICY_PATH:
        return artifact(path, "operation_observation_policy", schema=POLICY_SCHEMA_ID)
    if relative.startswith(ASSERTION_PREFIX):
        return artifact(path, "operation_response_assertion", schema=ASSERTION_SCHEMA_ID)
    if relative == "reports/operation-document-evidence/queue.v1.jsonl":
        return artifact(path, "operation_document_work_queue", schema=DOCUMENT_WORK_ITEM_V1_SCHEMA_ID)
    if relative == "reports/operation-document-evidence/queue.v2.jsonl":
        return artifact(path, "operation_document_work_queue", schema=DOCUMENT_WORK_ITEM_V2_SCHEMA_ID)
    if relative.endswith("reconciliation.v1.json"):
        return artifact(path, "operation_document_reconciliation", schema=DOCUMENT_RECONCILIATION_V1_SCHEMA_ID)
    if relative.endswith("reconciliation.v2.json"):
        return artifact(path, "operation_document_reconciliation", schema=DOCUMENT_RECONCILIATION_V2_SCHEMA_ID)
    if relative.startswith(DOCUMENT_EVIDENCE_PREFIX):
        if "/receipts/" in relative:
            return artifact(path, "operation_document_evidence", schema=DOCUMENT_CAPTURE_RECEIPT_V2_SCHEMA_ID)
        if relative.startswith("reports/operation-document-evidence/v2/") or relative.startswith("reports/operation-document-evidence/source-scopes/"):
            return artifact(path, "operation_document_evidence", schema=DOCUMENT_EVIDENCE_V2_SCHEMA_ID)
        if relative.endswith(".jsonl"):
            return artifact(path, "operation_document_work_queue", schema=DOCUMENT_WORK_ITEM_V2_SCHEMA_ID)
        return artifact(path, "operation_document_evidence", schema=DOCUMENT_EVIDENCE_SCHEMA_ID)
    if current is not None:
        return artifact(path, str(current["kind"]), schema=current.get("schema"))
    if relative.startswith("reports/") and "operation-denominator.json" in relative:
        return artifact(path, "operation_denominator_expectation")
    if relative.startswith("sources/"):
        return artifact(path, "source_provenance")
    return artifact(path, "operation_observation_plan_source")


def expected_manifest(current: dict[str, Any]) -> dict[str, Any]:
    expected = copy.deepcopy(current)
    artifacts = expected.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be an array")

    shards = sorted(SHARD_ROOT.glob("*.json"), key=lambda path: path.name)
    if not shards:
        raise ValueError("operation observation plan has no shards")
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    existing_by_path = {
        str(row.get("path")): row
        for row in artifacts
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    }
    referenced_paths = {GENERATOR_PATH, PARSER_PATH, POLICY_PATH}
    generation_inputs = index.get("generation_inputs", {})
    for field in ("operation_manifest", "legacy_policy", "provider_index"):
        ref = generation_inputs.get(field)
        if isinstance(ref, dict) and isinstance(ref.get("path"), str):
            referenced_paths.add(ref["path"])
    for ref in generation_inputs.get("operation_denominators", []):
        if isinstance(ref, dict) and isinstance(ref.get("path"), str):
            referenced_paths.add(ref["path"])
    for ref in generation_inputs.get("document_evidence", []):
        if isinstance(ref, dict) and isinstance(ref.get("path"), str):
            referenced_paths.add(ref["path"])
    for scope in index.get("source_scopes", []):
        for ref in scope.get("source_artifacts", []):
            if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                referenced_paths.add(ref["path"])

    referenced_paths.update(
        path.relative_to(ROOT).as_posix()
        for path in DOCUMENT_EVIDENCE_ROOT.rglob("*")
        if path.is_file()
    )
    assertion_root = ROOT / ASSERTION_PREFIX
    referenced_paths.update(path.relative_to(ROOT).as_posix() for path in assertion_root.glob("*.json"))
    referenced_paths.add(INDEX_PATH.relative_to(ROOT).as_posix())
    referenced_paths.add("schemas/index.json")
    referenced_paths.update(path.relative_to(ROOT).as_posix() for path in shards)
    referenced_paths.add("schemas/datapan.operation-observation-plan.v1.schema.json")

    plan_artifacts: list[dict[str, Any]] = []
    for relative in sorted(referenced_paths):
        path = ROOT / relative
        if not path.is_file():
            raise ValueError(f"operation observation plan release closure is missing {relative}")
        if relative == INDEX_PATH.relative_to(ROOT).as_posix():
            plan_artifacts.append(artifact(path, "operation_observation_plan", schema=SCHEMA_ID))
        elif relative in {shard.relative_to(ROOT).as_posix() for shard in shards}:
            plan_artifacts.append(artifact(path, "operation_observation_plan_shard", schema=SCHEMA_ID))
        else:
            plan_artifacts.append(operation_source_artifact(path, existing_by_path))

    replacement_paths = {row["path"] for row in plan_artifacts}
    retained = [
        operation_source_artifact(ROOT / str(row["path"]), existing_by_path)
        for row in artifacts
        if row.get("path") not in replacement_paths
        and not (row.get("kind") in MANAGED_KINDS and str(row.get("path", "")).startswith(MANAGED_PREFIX))
        and not (row.get("kind") in MANAGED_KINDS and str(row.get("path", "")).startswith(DOCUMENT_EVIDENCE_PREFIX))
        and not (row.get("kind") == "operation_response_assertion" and str(row.get("path", "")).startswith(ASSERTION_PREFIX))
        and not (row.get("kind") == "operation_observation_policy" and str(row.get("path", "")) == POLICY_PATH)
    ]
    combined = retained + plan_artifacts
    schema_index = json.loads((ROOT / "schemas/index.json").read_text(encoding="utf-8"))
    schema_order = [
        str(row["path"])
        for row in schema_index.get("schemas", [])
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    ]
    schema_rows = {
        str(row["path"]): row
        for row in combined
        if row.get("kind") == "schema"
    }
    ordered_schemas = [schema_rows[path] for path in schema_order if path in schema_rows]
    non_schema_rows = [row for row in combined if row.get("kind") != "schema"]
    if len(ordered_schemas) != len(schema_rows):
        raise ValueError("manifest schema artifacts do not match schemas/index.json")
    expected["artifacts"] = ordered_schemas + non_schema_rows
    expected["artifact_count"] = len(expected["artifacts"])
    return expected


def stable_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify release-manifest bindings")
    mode.add_argument("--write", action="store_true", help="update release-manifest bindings")
    args = parser.parse_args()

    try:
        current = json.loads(MANIFEST.read_text(encoding="utf-8"))
        expected = expected_manifest(current)
        if args.check:
            if current != expected:
                print("FAIL operation observation plan release-manifest bindings are stale", file=sys.stderr)
                return 1
        else:
            MANIFEST.write_bytes(stable_bytes(expected))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL operation observation plan release-manifest bindings: {exc}", file=sys.stderr)
        return 1
    print(
        "ok operation observation plan release-manifest bindings "
        f"(index=1, shards={len(list(SHARD_ROOT.glob('*.json')))}, total={expected['artifact_count']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
