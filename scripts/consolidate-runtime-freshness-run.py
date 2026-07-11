#!/usr/bin/env python3
"""Validate rotating shard evidence and emit a secret-free run receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any

import jsonschema


DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.runtime-freshness-run-receipt.v1.schema.json")


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def file_record(path: pathlib.Path, root: pathlib.Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": path.relative_to(root).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def build(root: pathlib.Path, combined_path: pathlib.Path, *, expected_shards: int, run_id: str) -> dict[str, Any]:
    plans = sorted(root.rglob("batch-plan.json"))
    if len(plans) != expected_shards:
        raise ValueError(f"expected {expected_shards} shard plans, got {len(plans)}")
    combined = load(combined_path)
    combined_results = combined.get("results")
    if not isinstance(combined_results, list):
        raise ValueError("combined verification results must be an array")
    all_identities: set[str] = set()
    allowed_pairs: set[tuple[str, str]] = set()
    shards: list[dict[str, Any]] = []
    total_results = 0
    for plan_path in plans:
        directory = plan_path.parent
        verification_path = directory / "verification.json"
        exit_path = directory / "exit-code.txt"
        if not verification_path.is_file() or not exit_path.is_file():
            raise ValueError(f"{directory}: missing verification or exit code")
        plan, verification = load(plan_path), load(verification_path)
        operations, results = plan.get("operations"), verification.get("results")
        if not isinstance(operations, list) or not operations or not isinstance(results, list):
            raise ValueError(f"{directory}: invalid plan or verification results")
        shard_index = plan.get("selection", {}).get("shard_index")
        if not isinstance(shard_index, int):
            raise ValueError(f"{plan_path}: missing shard index")
        for operation in operations:
            if not isinstance(operation, dict):
                raise ValueError(f"{plan_path}: invalid operation")
            identity = str(operation.get("identity_key", ""))
            if not identity or identity in all_identities:
                raise ValueError(f"duplicate or empty cross-shard identity: {identity}")
            all_identities.add(identity)
            allowed_pairs.add((str(operation.get("dataset_id", "")), str(operation.get("operation", ""))))
        for result in results:
            if not isinstance(result, dict) or (str(result.get("dataset_id", "")), str(result.get("operation", ""))) not in allowed_pairs:
                raise ValueError(f"{verification_path}: result is outside batch plans")
        raw_exit = exit_path.read_text(encoding="utf-8").strip()
        if not raw_exit.isdigit():
            raise ValueError(f"{exit_path}: malformed exit code")
        total_results += len(results)
        shards.append({"shard_index": shard_index, "operation_count": len(operations), "exit_code": int(raw_exit), "batch_plan": file_record(plan_path, root), "verification": file_record(verification_path, root)})
    if len({row["shard_index"] for row in shards}) != expected_shards:
        raise ValueError("shard indices are not unique")
    if total_results != len(combined_results):
        raise ValueError(f"combined result count expected {total_results}, got {len(combined_results)}")
    statuses = {name: 0 for name in ("verified", "failed", "skipped", "unknown")}
    for result in combined_results:
        status = result.get("status") if isinstance(result, dict) else None
        statuses[status if status in statuses else "unknown"] += 1
    shards.sort(key=lambda row: row["shard_index"])
    report = {"schema_version": "datapan.runtime-freshness-run-receipt.v1", "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "run_id": run_id, "summary": {"expected_shards": expected_shards, "shards": len(shards), "planned_operations": len(all_identities), "reported_results": len(combined_results), **statuses}, "combined_verification": file_record(combined_path, root), "shards": shards, "redaction": {"secret_values_present": False, "secret_hashes_present": False, "request_urls_present": False, "response_bodies_present": False}}
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--combined", type=pathlib.Path, required=True)
    parser.add_argument("--expected-shards", type=int, default=8)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--schema", type=pathlib.Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        report = build(args.root, args.combined, expected_shards=args.expected_shards, run_id=args.run_id)
        errors = list(jsonschema.Draft202012Validator(load(args.schema), format_checker=jsonschema.FormatChecker()).iter_errors(report))
        if errors:
            raise ValueError("; ".join(error.message for error in errors))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "consolidated", **report["summary"]}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL consolidate runtime freshness run: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
