#!/usr/bin/env python3
"""Package generated registry shards into a deterministic release asset."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import pathlib
import tarfile
from typing import Any


DEFAULT_SHARD_DIR = pathlib.Path("data/data-go-kr/shards")
DEFAULT_OUTPUT = pathlib.Path(".datapan/release-assets/data-go-kr-shards.tar.gz")
INVENTORY_NAME = "registry-shards.json"


def load_json_bytes(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - report parse context for operators
        raise ValueError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def clean_archive_path(value: Any, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if "\\" in value:
        raise ValueError(f"{label} must use portable forward-slash paths")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"{label} must not escape the shard archive root: {value}")
    return path


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def inventory_paths(inventory: dict[str, Any]) -> list[str]:
    if inventory.get("schema_version") != "datapan.registry-shards.v1":
        raise ValueError(f"unsupported shard inventory schema_version: {inventory.get('schema_version')}")
    paths = [INVENTORY_NAME]
    seen = {INVENTORY_NAME}
    for index, value in enumerate(as_list(inventory.get("shards"), "shards")):
        shard = as_dict(value, f"shards[{index}]")
        path = clean_archive_path(shard.get("path"), f"shards[{index}].path").as_posix()
        if path == INVENTORY_NAME:
            raise ValueError(f"shards[{index}].path must not point at {INVENTORY_NAME}")
        if path in seen:
            raise ValueError(f"duplicate archive path: {path}")
        seen.add(path)
        paths.append(path)
    return paths


def read_inventory(shard_dir: pathlib.Path) -> dict[str, Any]:
    inventory_path = shard_dir / INVENTORY_NAME
    if not inventory_path.exists():
        raise ValueError(f"shard directory does not contain {INVENTORY_NAME}: {shard_dir}")
    return as_dict(load_json_bytes(inventory_path.read_bytes(), INVENTORY_NAME), INVENTORY_NAME)


def resolve_shard_path(path_value: str, shard_dir: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(path_value)
    if path.is_absolute():
        return path
    cwd_path = pathlib.Path.cwd() / path
    if cwd_path.exists():
        return cwd_path
    return shard_dir / path


def archive_relative_path(source_path: pathlib.Path, shard_dir: pathlib.Path, fallback: str) -> str:
    try:
        rel = source_path.resolve().relative_to(shard_dir.resolve())
        return rel.as_posix()
    except ValueError:
        return clean_archive_path(fallback, "shard path").as_posix()


def aggregate_sha256(shards: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for shard in shards:
        digest.update(str(shard["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(shard["sha256"]).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def archive_inventory_and_sources(shard_dir: pathlib.Path) -> tuple[dict[str, Any], dict[str, pathlib.Path]]:
    source_inventory = read_inventory(shard_dir)
    inventory = copy.deepcopy(source_inventory)
    sources: dict[str, pathlib.Path] = {}
    shards = [
        as_dict(value, f"shards[{index}]")
        for index, value in enumerate(as_list(inventory.get("shards"), "shards"))
    ]
    for index, shard in enumerate(shards):
        original_path = clean_archive_path(shard.get("path"), f"shards[{index}].path").as_posix()
        source_path = resolve_shard_path(original_path, shard_dir)
        archive_path = archive_relative_path(source_path, shard_dir, original_path)
        if archive_path == INVENTORY_NAME:
            raise ValueError(f"shards[{index}].path must not point at {INVENTORY_NAME}")
        shard["path"] = archive_path
        sources[archive_path] = source_path

    summary = as_dict(inventory.get("summary"), "summary")
    summary["aggregate_sha256"] = aggregate_sha256(shards)
    return inventory, sources


def add_file(tar: tarfile.TarFile, archive_name: str, source_path: pathlib.Path) -> None:
    data = source_path.read_bytes()
    info = tarfile.TarInfo(archive_name)
    info.size = len(data)
    info.mode = 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def package_archive(shard_dir: pathlib.Path, output: pathlib.Path) -> dict[str, int]:
    inventory, sources = archive_inventory_and_sources(shard_dir)
    paths = inventory_paths(inventory)
    if sorted(sources) != sorted(paths[1:]):
        raise ValueError("normalized shard archive paths do not match inventory")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for archive_name in paths:
                    if archive_name == INVENTORY_NAME:
                        data = stable_json_bytes(inventory)
                        info = tarfile.TarInfo(INVENTORY_NAME)
                        info.size = len(data)
                        info.mode = 0o644
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        tar.addfile(info, io.BytesIO(data))
                        continue
                    source_path = sources[archive_name]
                    if not source_path.exists():
                        raise ValueError(f"shard inventory references missing file: {archive_name}")
                    if not source_path.is_file():
                        raise ValueError(f"shard inventory references non-file path: {archive_name}")
                    add_file(tar, archive_name, source_path)
    check = inspect_archive(output)
    return {"entries": check["entries"], "bytes": output.stat().st_size}


def archive_members(path: pathlib.Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            name = clean_archive_path(member.name, "archive member").as_posix()
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"archive contains unsupported member type: {name}")
            if name in members:
                raise ValueError(f"archive contains duplicate member: {name}")
            extracted = tar.extractfile(member)
            if extracted is None:
                raise ValueError(f"archive member cannot be read: {name}")
            members[name] = extracted.read()
    return members


def inspect_archive(path: pathlib.Path) -> dict[str, int]:
    members = archive_members(path)
    inventory_data = members.get(INVENTORY_NAME)
    if inventory_data is None:
        raise ValueError(f"archive does not contain {INVENTORY_NAME} at root")
    inventory = as_dict(load_json_bytes(inventory_data, INVENTORY_NAME), INVENTORY_NAME)
    expected_paths = inventory_paths(inventory)
    actual_paths = sorted(members)
    if sorted(expected_paths) != actual_paths:
        missing = sorted(set(expected_paths).difference(actual_paths))
        extra = sorted(set(actual_paths).difference(expected_paths))
        raise ValueError(f"archive entries do not match inventory paths; missing={missing} extra={extra}")

    records = 0
    for index, value in enumerate(as_list(inventory.get("shards"), "shards")):
        shard = as_dict(value, f"shards[{index}]")
        path_name = clean_archive_path(shard.get("path"), f"shards[{index}].path").as_posix()
        data = members[path_name]
        if shard.get("bytes") != len(data):
            raise ValueError(f"{path_name} byte count mismatch")
        if shard.get("sha256") != bytes_sha256(data):
            raise ValueError(f"{path_name} checksum mismatch")
        shard_records = as_list(load_json_bytes(data, path_name), path_name)
        if shard.get("records") != len(shard_records):
            raise ValueError(f"{path_name} record count mismatch")
        records += len(shard_records)
    summary = as_dict(inventory.get("summary"), "summary")
    shards = [
        as_dict(value, f"shards[{index}]")
        for index, value in enumerate(as_list(inventory.get("shards"), "shards"))
    ]
    expected_aggregate = aggregate_sha256(shards)
    if summary.get("aggregate_sha256") != expected_aggregate:
        raise ValueError("registry shard archive aggregate checksum mismatch")
    return {"entries": len(actual_paths), "shards": len(actual_paths) - 1, "records": records}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-dir", default=DEFAULT_SHARD_DIR, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", type=pathlib.Path, help="inspect an existing archive instead of writing one")
    args = parser.parse_args()

    try:
        if args.check:
            summary = inspect_archive(args.check)
            print(
                f"ok {args.check} "
                f"(entries={summary['entries']}, shards={summary['shards']}, records={summary['records']})"
            )
            return 0
        summary = package_archive(args.shard_dir, args.output)
    except Exception as exc:  # noqa: BLE001 - CLI should report all validation blockers
        print(f"FAIL package registry shards: {exc}")
        return 1

    print(f"wrote {args.output} (entries={summary['entries']}, bytes={summary['bytes']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
