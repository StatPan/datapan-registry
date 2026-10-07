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
MANAGED_KINDS = {"operation_document_evidence", "operation_observation_plan", "operation_observation_plan_shard"}
MANAGED_PREFIX = "reports/operation-observation-plan/"
DOCUMENT_EVIDENCE_PREFIX = "reports/operation-document-evidence/"


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


def expected_manifest(current: dict[str, Any]) -> dict[str, Any]:
    expected = copy.deepcopy(current)
    artifacts = expected.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be an array")

    plan_artifacts = [artifact(INDEX_PATH, "operation_observation_plan", schema=SCHEMA_ID)]
    shards = sorted(SHARD_ROOT.glob("*.json"), key=lambda path: path.name)
    if not shards:
        raise ValueError("operation observation plan has no shards")
    plan_artifacts.extend(
        artifact(path, "operation_observation_plan_shard", schema=SCHEMA_ID) for path in shards
    )

    document_artifacts = [
        artifact(path, "operation_document_evidence", schema=DOCUMENT_EVIDENCE_SCHEMA_ID)
        for path in sorted(DOCUMENT_EVIDENCE_ROOT.glob("[0-9]*-[0-9]*.json"), key=lambda path: path.name)
    ]
    if document_artifacts:
        schema_path = ROOT / "schemas/datapan.operation-document-evidence.v1.schema.json"
        schema_manifest_row = next(
            (row for row in artifacts if row.get("path") == schema_path.relative_to(ROOT).as_posix()),
            None,
        )
        if schema_manifest_row != artifact(schema_path, "schema"):
            raise ValueError("operation-document-evidence schema is not registered with its current digest in manifest.json")

    plan_artifacts.extend(document_artifacts)
    replacement_paths = {row["path"] for row in plan_artifacts}
    retained = [
        row
        for row in artifacts
        if row.get("path") not in replacement_paths
        and not (row.get("kind") in MANAGED_KINDS and str(row.get("path", "")).startswith(MANAGED_PREFIX))
        and not (row.get("kind") == "operation_document_evidence" and str(row.get("path", "")).startswith(DOCUMENT_EVIDENCE_PREFIX))
    ]
    expected["artifacts"] = retained + plan_artifacts
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
