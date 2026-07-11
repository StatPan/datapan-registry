#!/usr/bin/env python3
"""Select a deterministic rotating queue slice and materialize a temporary registry."""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys
from typing import Any

import jsonschema


DEFAULT_QUEUE = pathlib.Path("reports/runtime-freshness-queue.json")
DEFAULT_REGISTRY = pathlib.Path("data/data-go-kr.registry.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.runtime-freshness-batch.v1.schema.json")


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def operation_identity(dataset_id: str, operation: dict[str, Any]) -> str:
    source = operation.get("source")
    raw = source.get("raw") if isinstance(source, dict) else None
    sequence = raw.get("operation_seq") if isinstance(raw, dict) else None
    suffix = str(sequence) if sequence is not None and str(sequence) else str(operation.get("name", ""))
    return f"data_go_kr:{dataset_id}:{suffix}"


def select(queue: dict[str, Any], *, rotation_seed: int, shard_index: int, shard_count: int, batch_size: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if rotation_seed < 0 or shard_index < 0 or shard_count < 1 or shard_index >= shard_count or batch_size < 1:
        raise ValueError("invalid rotation or shard parameters")
    eligible = [row for row in queue.get("queue", []) if isinstance(row, dict) and row.get("source_id") == "data_go_kr"]
    if not eligible:
        raise ValueError("freshness queue has no data_go_kr operations")
    if batch_size > len(eligible):
        raise ValueError("batch_size exceeds eligible operations")
    offset = ((rotation_seed * shard_count + shard_index) * batch_size) % len(eligible)
    end = offset + batch_size
    selected = eligible[offset:end] if end <= len(eligible) else eligible[offset:] + eligible[: end - len(eligible)]
    identities = [str(row.get("identity_key", "")) for row in selected]
    if not all(identities) or len(identities) != len(set(identities)):
        raise ValueError("selected queue identities must be non-empty and unique")
    selection = {"rotation_seed": rotation_seed, "shard_index": shard_index, "shard_count": shard_count, "batch_size": batch_size, "eligible_operations": len(eligible), "offset": offset, "selected_operations": len(selected), "wrapped": end > len(eligible)}
    return selected, selection


def materialize(registry: list[Any], selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted = {str(row["identity_key"]) for row in selected}
    found: set[str] = set()
    output: list[dict[str, Any]] = []
    for raw_spec in registry:
        if not isinstance(raw_spec, dict) or not isinstance(raw_spec.get("operations"), list):
            raise ValueError("invalid canonical registry entry")
        dataset_id = str(raw_spec.get("id", ""))
        operations = []
        for operation in raw_spec["operations"]:
            if not isinstance(operation, dict):
                raise ValueError(f"{dataset_id}: invalid operation")
            identity = operation_identity(dataset_id, operation)
            if identity in wanted:
                found.add(identity)
                operations.append(copy.deepcopy(operation))
        if operations:
            spec = copy.deepcopy(raw_spec)
            spec["operations"] = operations
            output.append(spec)
    missing = sorted(wanted - found)
    if missing:
        raise ValueError(f"selected identities missing from canonical registry: {missing[:5]}")
    if sum(len(spec["operations"]) for spec in output) != len(selected):
        raise ValueError("materialized registry operation count mismatch")
    return output


def build(queue_path: pathlib.Path, registry_path: pathlib.Path, *, rotation_seed: int, shard_index: int, shard_count: int, batch_size: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    queue, registry = load(queue_path), load(registry_path)
    if not isinstance(queue, dict) or not isinstance(registry, list):
        raise ValueError("queue or registry input has the wrong shape")
    selected, selection = select(queue, rotation_seed=rotation_seed, shard_index=shard_index, shard_count=shard_count, batch_size=batch_size)
    batch_registry = materialize(registry, selected)
    plan = {"schema_version": "datapan.runtime-freshness-batch.v1", "generated_at": queue["generated_at"], "queue": queue_path.as_posix(), "registry": registry_path.as_posix(), "selection": selection, "operations": [{key: row.get(key) for key in ("identity_key", "dataset_id", "operation", "operation_seq", "classification", "priority")} for row in selected]}
    return batch_registry, plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=pathlib.Path, default=DEFAULT_QUEUE)
    parser.add_argument("--registry", type=pathlib.Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--schema", type=pathlib.Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--rotation-seed", type=int, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--output-registry", type=pathlib.Path, required=True)
    parser.add_argument("--output-plan", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        registry, plan = build(args.queue, args.registry, rotation_seed=args.rotation_seed, shard_index=args.shard_index, shard_count=args.shard_count, batch_size=args.batch_size)
        errors = list(jsonschema.Draft202012Validator(load(args.schema), format_checker=jsonschema.FormatChecker()).iter_errors(plan))
        if errors:
            raise ValueError("; ".join(error.message for error in errors))
        args.output_registry.parent.mkdir(parents=True, exist_ok=True)
        args.output_plan.parent.mkdir(parents=True, exist_ok=True)
        args.output_registry.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        args.output_plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "generated", **plan["selection"]}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL runtime freshness batch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
