#!/usr/bin/env python3
"""Generate a complete freshness work queue from supported operation identities."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import jsonschema


REGISTRY = pathlib.Path("data/data-go-kr.registry.json")
DENOMINATORS = pathlib.Path("reports/operation-denominator-rollup.json")
LATEST = pathlib.Path("reports/latest-verification.json")
POLICY = pathlib.Path("policy/sustainable-coverage.json")
SCHEMA = pathlib.Path("schemas/datapan.runtime-freshness-queue.v1.schema.json")
OUTPUT = pathlib.Path("reports/runtime-freshness-queue.json")


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("evidence timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def latest_evidence(results: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[tuple[datetime | None, int, dict[str, Any]]]] = {}
    for index, row in enumerate(results):
        dataset_id, operation = row.get("dataset_id"), row.get("operation")
        if not isinstance(dataset_id, str) or not dataset_id or not isinstance(operation, str) or not operation:
            raise ValueError(f"latest verification result {index} lacks dataset_id/operation")
        raw = row.get("verified_at")
        timestamp = parse_time(raw) if isinstance(raw, str) and raw else None
        grouped.setdefault((dataset_id, operation), []).append((timestamp, index, row))
    return {
        key: max(rows, key=lambda item: (item[0] is not None, item[0] or datetime.min.replace(tzinfo=timezone.utc), item[1]))[2]
        for key, rows in grouped.items()
    }


def supported_operations(registry: list[Any], denominator_rollup: dict[str, Any]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for spec in registry:
        if not isinstance(spec, dict) or not isinstance(spec.get("operations"), list):
            raise ValueError("canonical registry entry lacks operations")
        dataset_id = str(spec.get("id", ""))
        for operation in spec["operations"]:
            if not isinstance(operation, dict) or not isinstance(operation.get("name"), str):
                raise ValueError(f"{dataset_id}: invalid operation")
            raw = operation.get("source", {}).get("raw", {}) if isinstance(operation.get("source"), dict) else {}
            seq = str(raw.get("operation_seq")) if isinstance(raw, dict) and raw.get("operation_seq") is not None else None
            operations.append({"source_id": "data_go_kr", "dataset_id": dataset_id, "operation": operation["name"], "operation_seq": seq, "identity_key": f"data_go_kr:{dataset_id}:{seq or operation['name']}"})
    for source in denominator_rollup["sources"]:
        if source["source_id"] == "data_go_kr":
            continue
        denominator = load(pathlib.Path(source["path"]))
        for operation in denominator["operations"]:
            operation_id = operation["operation_id"]
            operations.append({"source_id": source["source_id"], "dataset_id": None, "operation": operation_id, "operation_seq": None, "identity_key": f"{source['source_id']}:{operation_id}"})
    identities = [row["identity_key"] for row in operations]
    if len(identities) != len(set(identities)):
        raise ValueError("supported operation identity is not unique")
    expected = int(denominator_rollup["summary"]["operations"])
    if len(operations) != expected:
        raise ValueError(f"supported operations expected {expected}, got {len(operations)}")
    return operations


def classify(evidence: dict[str, Any] | None, as_of: datetime, fresh_days: int, expire_days: int) -> tuple[str | None, int | None, str | None]:
    if evidence is None:
        return "never_evidenced", 2, "collect_initial_evidence"
    raw = evidence.get("verified_at")
    if not isinstance(raw, str) or not raw:
        return "unknown_timestamp", 1, "repair_evidence_timestamp"
    observed = parse_time(raw)
    if observed > as_of:
        raise ValueError("evidence timestamp is after evaluation time")
    if observed < as_of - timedelta(days=expire_days):
        return "expired", 0, "reverify_expired_operation"
    if observed < as_of - timedelta(days=fresh_days):
        return "stale", 1, "reverify_stale_operation"
    if evidence.get("status") != "verified":
        return "recent_non_verified", 2, "retry_or_route_failure"
    return None, None, None


def build() -> dict[str, Any]:
    denominator_rollup = load(DENOMINATORS)
    registry = load(REGISTRY)
    latest = load(LATEST)
    policy = load(POLICY)
    if not isinstance(registry, list) or not isinstance(latest, dict):
        raise ValueError("invalid registry or latest verification input")
    as_of_text = denominator_rollup["generated_at"]
    as_of = parse_time(as_of_text)
    fresh_days = int(policy["freshness"]["fresh_days"])
    expire_days = int(policy["freshness"]["expire_days"])
    evidence = latest_evidence(latest["results"])
    operations = supported_operations(registry, denominator_rollup)
    counts = {name: 0 for name in ("never_evidenced", "unknown_timestamp", "stale", "expired", "recent_non_verified")}
    queue: list[dict[str, Any]] = []
    fresh_verified = 0
    for operation in operations:
        row = evidence.get((operation["dataset_id"], operation["operation"])) if operation["source_id"] == "data_go_kr" else None
        classification, priority, action = classify(row, as_of, fresh_days, expire_days)
        if classification is None:
            fresh_verified += 1
            continue
        counts[classification] += 1
        queue.append({**operation, "classification": classification, "priority": priority, "action": action, "last_status": row.get("status") if row else None, "last_verified_at": row.get("verified_at") if row and isinstance(row.get("verified_at"), str) else None})
    queue.sort(key=lambda row: (row["priority"], row["source_id"], row["identity_key"]))
    supported = len(operations)
    if len(queue) + fresh_verified != supported:
        raise ValueError("queue reconciliation failed")
    report = {"schema_version": "datapan.runtime-freshness-queue.v1", "generated_at": as_of_text, "inputs": {"canonical_registry": REGISTRY.as_posix(), "operation_denominator_rollup": DENOMINATORS.as_posix(), "latest_verification": LATEST.as_posix(), "coverage_policy": POLICY.as_posix()}, "freshness": {"as_of": as_of_text, "fresh_days": fresh_days, "expire_days": expire_days}, "summary": {"supported_operations": supported, "fresh_verified": fresh_verified, "queued": len(queue), **counts}, "queue": queue}
    errors = list(jsonschema.Draft202012Validator(load(SCHEMA), format_checker=jsonschema.FormatChecker()).iter_errors(report))
    if errors:
        raise ValueError("; ".join(error.message for error in errors[:10]))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        rendered = json.dumps(build(), ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
                raise ValueError(f"{OUTPUT} is stale")
            print(f"ok {OUTPUT}")
        else:
            OUTPUT.write_text(rendered, encoding="utf-8")
            print(f"wrote {OUTPUT}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL runtime freshness queue: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
