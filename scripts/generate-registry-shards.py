#!/usr/bin/env python3
"""Generate deterministic registry shards from a canonical registry artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import unicodedata
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = "datapan.registry-shards.v1"
DEFAULT_SOURCE_REGISTRY = pathlib.Path("data/data-go-kr.registry.json")
DEFAULT_OUTPUT_DIR = pathlib.Path("data/data-go-kr/shards")
DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_PROVIDER_INDEX = pathlib.Path("data/provider-index.json")
DEFAULT_SOURCE_PROFILE = pathlib.Path("sources/data_go_kr.json")


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


def stable_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_json(path: pathlib.Path, value: Any) -> int:
    data = stable_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def portable_path(path: pathlib.Path) -> str:
    return path.as_posix()


def relative_path(path: pathlib.Path) -> pathlib.Path:
    try:
        return path.relative_to(pathlib.Path.cwd())
    except ValueError:
        return path


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


def operation_sources(record: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    source = record.get("source")
    if isinstance(source, dict):
        sources.append(source)
    operations = record.get("operations")
    if isinstance(operations, list):
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            operation_source = operation.get("source")
            if isinstance(operation_source, dict):
                sources.append(operation_source)
    return sources


def host_from_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return parsed.netloc.lower()


def source_hosts(source: dict[str, Any]) -> set[str]:
    hosts: set[str] = set()
    for key in ("url", "endpoint", "endpoint_url"):
        host = host_from_url(source.get(key))
        if host:
            hosts.add(host)
    raw = source.get("raw")
    if isinstance(raw, dict):
        for key in ("end_point_url", "operation_url", "guide_url", "meta_url", "url"):
            host = host_from_url(raw.get(key))
            if host:
                hosts.add(host)
    return hosts


def record_hosts(record: dict[str, Any]) -> set[str]:
    hosts: set[str] = set()
    for source in operation_sources(record):
        hosts.update(source_hosts(source))
    return hosts


def record_providers(record: dict[str, Any]) -> set[str]:
    providers: set[str] = set()
    provider = record.get("provider")
    if isinstance(provider, str) and provider:
        providers.add(provider)
    for source in operation_sources(record):
        system = source.get("system")
        if isinstance(system, str) and system:
            providers.add(system)
    return providers


def generated_at(manifest_path: pathlib.Path, override: str | None) -> str:
    if override:
        return override
    if manifest_path.exists():
        manifest = as_dict(load_json(manifest_path), manifest_path)
        value = manifest.get("generated_at")
        if isinstance(value, str) and value:
            return value
    return "1970-01-01T00:00:00Z"


def shard_inventory(
    *,
    source_registry: pathlib.Path,
    output_dir: pathlib.Path,
    manifest_path: pathlib.Path,
    provider_index: pathlib.Path,
    source_profile: pathlib.Path,
    generator: pathlib.Path,
    generated_at_value: str,
    shard_entries: list[dict[str, Any]],
    aggregate_sha256: str,
) -> dict[str, Any]:
    providers = sorted({provider for shard in shard_entries for provider in shard.get("providers", [])})
    hosts = sorted({host for shard in shard_entries for host in shard.get("hosts", [])})
    source_registry_rel = portable_path(relative_path(source_registry))
    generation_inputs: dict[str, Any] = {
        "source_registry": source_registry_rel,
        "release_manifest": portable_path(relative_path(manifest_path)),
        "generator": portable_path(relative_path(generator)),
    }
    if provider_index.exists():
        generation_inputs["provider_index"] = portable_path(relative_path(provider_index))
    if source_profile.exists():
        generation_inputs["source_profile"] = portable_path(relative_path(source_profile))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at_value,
        "source_id": "data_go_kr",
        "provider": "data.go.kr",
        "strategy": "by_institution",
        "source_registry": source_registry_rel,
        "source_registry_sha256": file_sha256(source_registry),
        "generation_inputs": generation_inputs,
        "recomposition": {
            "policy": "canonical_record_set",
            "primary_shard_key": "institution",
            "record_identity_fields": ["id"],
            "requires_exactly_once_records": True,
            "requires_canonical_order": True,
        },
        "summary": {
            "shards": len(shard_entries),
            "records": sum(int(shard["records"]) for shard in shard_entries),
            "bytes": sum(int(shard["bytes"]) for shard in shard_entries),
            "providers": len(providers),
            "hosts": len(hosts),
            "aggregate_sha256": aggregate_sha256,
        },
        "indexes": {
            "provider_indexed": True,
            "host_indexed": True,
            "institution_indexed": True,
        },
        "shards": shard_entries,
    }


def build_shards(
    registry: list[Any],
    output_dir: pathlib.Path,
    *,
    clean: bool,
) -> tuple[list[dict[str, Any]], str]:
    if clean and output_dir.exists():
        shutil.rmtree(output_dir)

    groups: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(registry):
        record = as_dict(value, f"registry[{index}]")
        organization = record.get("organization")
        if not isinstance(organization, str) or not organization.strip():
            raise ValueError(f"registry[{index}].organization must be a non-empty string")
        label = organization.strip()
        key = institution_key(label)
        group = groups.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "records": [],
                "providers": set(),
                "hosts": set(),
            },
        )
        group["records"].append(record)
        group["providers"].update(record_providers(record))
        group["hosts"].update(record_hosts(record))

    shard_entries: list[dict[str, Any]] = []
    by_institution = output_dir / "by-institution"
    for group in sorted(groups.values(), key=lambda item: (str(item["label"]), str(item["key"]))):
        shard_path = by_institution / f"{group['key']}.registry.json"
        records = group["records"]
        byte_count = write_json(shard_path, records)
        shard_entries.append(
            {
                "path": portable_path(relative_path(shard_path)),
                "key": group["key"],
                "label": group["label"],
                "records": len(records),
                "bytes": byte_count,
                "sha256": file_sha256(shard_path),
                "providers": sorted(group["providers"]),
                "hosts": sorted(group["hosts"]),
            }
        )

    aggregate = hashlib.sha256()
    for shard in shard_entries:
        aggregate.update(str(shard["path"]).encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(str(shard["sha256"]).encode("ascii"))
        aggregate.update(b"\0")
    return shard_entries, aggregate.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-registry", default=DEFAULT_SOURCE_REGISTRY, type=pathlib.Path)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, type=pathlib.Path)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    parser.add_argument("--provider-index", default=DEFAULT_PROVIDER_INDEX, type=pathlib.Path)
    parser.add_argument("--source-profile", default=DEFAULT_SOURCE_PROFILE, type=pathlib.Path)
    parser.add_argument("--generated-at")
    parser.add_argument("--clean", action="store_true", help="remove the output directory before writing shards")
    args = parser.parse_args()

    registry = as_list(load_json(args.source_registry), args.source_registry)
    shard_entries, aggregate_sha256 = build_shards(registry, args.output_dir, clean=args.clean)
    inventory = shard_inventory(
        source_registry=args.source_registry,
        output_dir=args.output_dir,
        manifest_path=args.manifest,
        provider_index=args.provider_index,
        source_profile=args.source_profile,
        generator=pathlib.Path(__file__),
        generated_at_value=generated_at(args.manifest, args.generated_at),
        shard_entries=shard_entries,
        aggregate_sha256=aggregate_sha256,
    )
    inventory_path = args.output_dir / "registry-shards.json"
    write_json(inventory_path, inventory)
    summary = inventory["summary"]
    print(
        f"wrote {inventory_path} "
        f"(shards={summary['shards']}, records={summary['records']}, bytes={summary['bytes']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
