#!/usr/bin/env python3
"""Validate registry shard inventory and recomposition invariants."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import unicodedata
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before validating registry shards") from exc


EXPECTED_SCHEMA_VERSION = "datapan.registry-shards.v1"
DEFAULT_INVENTORY = pathlib.Path("data/data-go-kr/shards/registry-shards.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.registry-shards.v1.schema.json")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: Any, label: str | pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: Any, label: str | pathlib.Path) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def institution_key(label: str) -> str:
    ascii_label = (
        unicodedata.normalize("NFKD", label)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_label).strip("-")
    if not slug:
        slug = "institution"
    return f"{slug}-{sha256_text(label)[:12]}"


def resolve_path(path_value: str, base_dir: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(path_value)
    if path.is_absolute():
        return path
    cwd_path = pathlib.Path.cwd() / path
    if cwd_path.exists():
        return cwd_path
    return base_dir / path


def identity(record: dict[str, Any], fields: list[str], label: str) -> tuple[Any, ...]:
    values: list[Any] = []
    for field in fields:
        value = record.get(field)
        if value is None or value == "":
            raise ValueError(f"{label}.{field} must be present for shard recomposition identity")
        values.append(value)
    return tuple(values)


def validate_schema(inventory: dict[str, Any], schema_path: pathlib.Path) -> None:
    if inventory.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version expected {EXPECTED_SCHEMA_VERSION}, got {inventory.get('schema_version')}"
        )
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(inventory), key=lambda error: list(error.path))
    if errors:
        messages = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            messages.append(f"{location}: {error.message}")
        raise ValueError("; ".join(messages))


def validate_summary(inventory: dict[str, Any], shards: list[dict[str, Any]]) -> None:
    summary = as_dict(inventory.get("summary"), "summary")
    expected = {
        "shards": len(shards),
        "records": sum(int(shard["records"]) for shard in shards),
        "bytes": sum(int(shard["bytes"]) for shard in shards),
        "providers": len({provider for shard in shards for provider in shard.get("providers", [])}),
        "hosts": len({host for shard in shards for host in shard.get("hosts", [])}),
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            raise ValueError(f"summary.{key} expected {value}, got {summary.get(key)}")

    aggregate = hashlib.sha256()
    for shard in shards:
        aggregate.update(str(shard["path"]).encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(str(shard["sha256"]).encode("ascii"))
        aggregate.update(b"\0")
    expected_aggregate = aggregate.hexdigest()
    if summary.get("aggregate_sha256") != expected_aggregate:
        raise ValueError(
            f"summary.aggregate_sha256 expected {expected_aggregate}, got {summary.get('aggregate_sha256')}"
        )


def canonical_records(source_registry: pathlib.Path, identity_fields: list[str]) -> dict[tuple[Any, ...], tuple[int, dict[str, Any]]]:
    registry = as_list(load_json(source_registry), source_registry)
    records: dict[tuple[Any, ...], tuple[int, dict[str, Any]]] = {}
    for index, value in enumerate(registry):
        record = as_dict(value, f"{source_registry}[{index}]")
        key = identity(record, identity_fields, f"{source_registry}[{index}]")
        if key in records:
            raise ValueError(f"duplicate canonical record identity {key!r}")
        records[key] = (index, record)
    return records


def validate_shard_files(
    inventory: dict[str, Any],
    inventory_path: pathlib.Path,
    source_registry: pathlib.Path,
    identity_fields: list[str],
) -> None:
    canonical = canonical_records(source_registry, identity_fields)
    seen: dict[tuple[Any, ...], str] = {}
    shard_records_total = 0

    shards = [as_dict(value, f"shards[{index}]") for index, value in enumerate(as_list(inventory.get("shards"), "shards"))]
    for shard_index, shard in enumerate(shards):
        path_value = str(shard["path"])
        shard_path = resolve_path(path_value, inventory_path.parent)
        if not shard_path.exists():
            raise ValueError(f"shards[{shard_index}].path missing file: {path_value}")
        actual_bytes = shard_path.stat().st_size
        if shard.get("bytes") != actual_bytes:
            raise ValueError(f"{path_value} bytes expected {actual_bytes}, got {shard.get('bytes')}")
        actual_sha256 = file_sha256(shard_path)
        if shard.get("sha256") != actual_sha256:
            raise ValueError(f"{path_value} sha256 expected {actual_sha256}, got {shard.get('sha256')}")

        records = as_list(load_json(shard_path), shard_path)
        if shard.get("records") != len(records):
            raise ValueError(f"{path_value} records expected {len(records)}, got {shard.get('records')}")

        label = str(shard["label"])
        expected_key = institution_key(label)
        if shard.get("key") != expected_key:
            raise ValueError(f"{path_value} key expected {expected_key}, got {shard.get('key')}")

        previous_canonical_index = -1
        for record_index, value in enumerate(records):
            record = as_dict(value, f"{path_value}[{record_index}]")
            organization = record.get("organization")
            if organization != label:
                raise ValueError(
                    f"{path_value}[{record_index}].organization expected {label!r}, got {organization!r}"
                )
            key = identity(record, identity_fields, f"{path_value}[{record_index}]")
            if key in seen:
                raise ValueError(f"duplicate shard record identity {key!r} in {path_value} and {seen[key]}")
            canonical_entry = canonical.get(key)
            if canonical_entry is None:
                raise ValueError(f"shard record identity {key!r} is absent from canonical registry")
            canonical_index, canonical_record = canonical_entry
            if canonical_index <= previous_canonical_index:
                raise ValueError(f"{path_value} records are not in canonical order")
            previous_canonical_index = canonical_index
            if stable_json(record) != stable_json(canonical_record):
                raise ValueError(f"shard record identity {key!r} differs from canonical registry")
            seen[key] = path_value
            shard_records_total += 1

    if shard_records_total != len(canonical):
        raise ValueError(f"shard record total expected {len(canonical)}, got {shard_records_total}")
    missing = set(canonical).difference(seen)
    if missing:
        sample = sorted(repr(value) for value in missing)[:5]
        raise ValueError(f"missing canonical record identities from shards: {', '.join(sample)}")

    validate_summary(inventory, shards)


def validate_inventory(inventory_path: pathlib.Path, schema_path: pathlib.Path) -> dict[str, Any]:
    inventory = as_dict(load_json(inventory_path), inventory_path)
    validate_schema(inventory, schema_path)

    recomposition = as_dict(inventory.get("recomposition"), "recomposition")
    identity_fields = as_list(recomposition.get("record_identity_fields"), "recomposition.record_identity_fields")
    identity_field_names = [str(field) for field in identity_fields]
    source_registry = resolve_path(str(inventory["source_registry"]), inventory_path.parent)
    if not source_registry.exists():
        raise ValueError(f"source_registry missing file: {inventory['source_registry']}")
    actual_source_sha256 = file_sha256(source_registry)
    if inventory.get("source_registry_sha256") != actual_source_sha256:
        raise ValueError(
            "source_registry_sha256 expected "
            f"{actual_source_sha256}, got {inventory.get('source_registry_sha256')}"
        )

    validate_shard_files(inventory, inventory_path, source_registry, identity_field_names)
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", nargs="?", default=DEFAULT_INVENTORY, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    args = parser.parse_args()

    try:
        inventory = validate_inventory(args.inventory, args.schema)
    except Exception as exc:  # noqa: BLE001 - report all validation blockers
        print(f"FAIL {args.inventory}: {exc}")
        return 1

    summary = as_dict(inventory.get("summary"), args.inventory)
    print(
        f"ok {args.inventory} "
        f"(shards={summary.get('shards')}, records={summary.get('records')}, bytes={summary.get('bytes')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
