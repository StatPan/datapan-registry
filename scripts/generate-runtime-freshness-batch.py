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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from runtime_evidence_projection import file_sha256, make_contract_binding, operation_contract, operation_manifest_index, upstream_operation_key


DEFAULT_QUEUE = pathlib.Path("reports/runtime-freshness-queue.json")
DEFAULT_REGISTRY = pathlib.Path("data/data-go-kr.registry.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.runtime-freshness-batch.v1.schema.json")
DEFAULT_OPERATION_MANIFEST = pathlib.Path("reports/data-go-kr/operation-manifest.json")


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def operation_identity(dataset_id: str, operation: dict[str, Any]) -> str:
    sequence = upstream_operation_key(operation)
    suffix = sequence or str(operation.get("name", ""))
    return f"data_go_kr:{dataset_id}:{suffix}"


def select(queue: dict[str, Any], *, rotation_seed: int, shard_index: int, shard_count: int, batch_size: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if rotation_seed < 0 or shard_index < 0 or shard_count < 1 or shard_index >= shard_count or batch_size < 1:
        raise ValueError("invalid rotation or shard parameters")
    eligible = [
        row for row in queue.get("queue", [])
        if isinstance(row, dict)
        and row.get("source_id") == "data_go_kr"
        and row.get("classification") != "unsupported_current_binding"
    ]
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


def build(queue_path: pathlib.Path, registry_path: pathlib.Path, *, rotation_seed: int, shard_index: int, shard_count: int, batch_size: int, operation_manifest_path: pathlib.Path = DEFAULT_OPERATION_MANIFEST) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    queue, registry = load(queue_path), load(registry_path)
    operation_manifest = load(operation_manifest_path)
    if not isinstance(queue, dict) or not isinstance(registry, list):
        raise ValueError("queue or registry input has the wrong shape")
    if not isinstance(operation_manifest, dict):
        raise ValueError("operation manifest input must be an object")
    selected, selection = select(queue, rotation_seed=rotation_seed, shard_index=shard_index, shard_count=shard_count, batch_size=batch_size)
    batch_registry = materialize(registry, selected)
    registry_sha256 = file_sha256(registry_path)
    operation_manifest_sha256 = file_sha256(operation_manifest_path)
    snapshot = operation_manifest.get("source_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("sha256") != registry_sha256:
        raise ValueError("operation manifest is not bound to the exact registry snapshot")
    manifest_by_key = operation_manifest_index(operation_manifest)
    contract_by_key: dict[str, list[dict[str, Any]]] = {}
    for dataset in registry:
        if not isinstance(dataset, dict) or not isinstance(dataset.get("operations"), list):
            continue
        dataset_id = str(dataset.get("id", ""))
        for op_index, operation in enumerate(dataset["operations"]):
            if not isinstance(operation, dict):
                continue
            source = operation.get("source")
            source_system = str(source.get("system") or "data.go.kr") if isinstance(source, dict) else "data.go.kr"
            operation_key = upstream_operation_key(operation) or ""
            manifest_matches = manifest_by_key.get((dataset_id, source_system, operation_key), [])
            contract = operation_contract(dataset, operation, op_index, manifest_matches[0] if len(manifest_matches) == 1 else None)
            if len(manifest_matches) != 1:
                contract["contract_complete"] = False
                contract["incomplete_reasons"].append("missing_or_ambiguous_operation_manifest_identity")
            contract_by_key.setdefault(contract["identity_key"], []).append(contract)
    plan_operations = []
    for row in selected:
        item = {key: row.get(key) for key in ("identity_key", "dataset_id", "operation", "operation_seq", "classification", "priority")}
        matches = contract_by_key.get(str(row.get("identity_key")), [])
        contract = matches[0] if len(matches) == 1 else None
        binding = None
        if contract is not None and contract["contract_complete"]:
            binding = make_contract_binding(
                contract,
                source_snapshot_sha256=registry_sha256,
                operation_manifest_sha256=operation_manifest_sha256,
                identity_key=str(row["identity_key"]),
            )
        item["contract_binding"] = binding
        plan_operations.append(item)
    plan = {
        "schema_version": "datapan.runtime-freshness-batch.v1",
        "generated_at": queue["generated_at"],
        "queue": queue_path.as_posix(),
        "registry": registry_path.as_posix(),
        "registry_snapshot": {"source_sha256": registry_sha256, "operation_manifest_sha256": operation_manifest_sha256},
        "selection": selection,
        "operations": plan_operations,
    }
    return batch_registry, plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=pathlib.Path, default=DEFAULT_QUEUE)
    parser.add_argument("--registry", type=pathlib.Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--operation-manifest", "--manifest", dest="operation_manifest", type=pathlib.Path, default=DEFAULT_OPERATION_MANIFEST)
    parser.add_argument("--schema", type=pathlib.Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--rotation-seed", type=int, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--output-registry", type=pathlib.Path, required=True)
    parser.add_argument("--output-plan", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        registry, plan = build(args.queue, args.registry, rotation_seed=args.rotation_seed, shard_index=args.shard_index, shard_count=args.shard_count, batch_size=args.batch_size, operation_manifest_path=args.operation_manifest)
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
