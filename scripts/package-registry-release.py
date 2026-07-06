#!/usr/bin/env python3
"""Package and inspect installable datapan-registry release zip assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import tarfile
import zipfile
from typing import Any


DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_OUTPUT = pathlib.Path(".datapan/release-assets/datapan-registry-snapshot.zip")
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
REGISTRY_PATH = "data/data-go-kr.registry.json"
SHARD_INVENTORY = "registry-shards.json"


def load_json_bytes(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - CLI should report parse context
        raise ValueError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def load_json(path: pathlib.Path) -> Any:
    return load_json_bytes(path.read_bytes(), path.as_posix())


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
        raise ValueError(f"{label} must not escape the release archive root: {value}")
    return path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_paths(manifest: dict[str, Any]) -> list[str]:
    paths = ["manifest.json"]
    seen = set(paths)
    for index, value in enumerate(as_list(manifest.get("artifacts"), "artifacts")):
        artifact = as_dict(value, f"artifacts[{index}]")
        path = clean_archive_path(artifact.get("path"), f"artifacts[{index}].path").as_posix()
        if path in seen:
            raise ValueError(f"duplicate release archive path: {path}")
        seen.add(path)
        paths.append(path)
    return paths


def zip_add_file(archive: zipfile.ZipFile, archive_name: str, source_path: pathlib.Path) -> None:
    data = source_path.read_bytes()
    info = zipfile.ZipInfo(archive_name, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def package_release_zip(manifest_path: pathlib.Path, output: pathlib.Path) -> dict[str, Any]:
    manifest = as_dict(load_json(manifest_path), manifest_path.as_posix())
    root = manifest_path.parent
    paths = release_paths(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as archive:
        for archive_name in paths:
            source_path = manifest_path if archive_name == "manifest.json" else root / archive_name
            if not source_path.exists():
                raise ValueError(f"release manifest references missing file: {archive_name}")
            if not source_path.is_file():
                raise ValueError(f"release manifest references non-file path: {archive_name}")
            zip_add_file(archive, archive_name, source_path)
    return inspect_release_zip(output, shard_archive=None)


def zip_members(path: pathlib.Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = clean_archive_path(info.filename, "zip member").as_posix()
            if info.is_dir():
                continue
            if name in members:
                raise ValueError(f"release zip contains duplicate member: {name}")
            members[name] = archive.read(info)
    return members


def shard_source_sha256(path: pathlib.Path) -> str:
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            member_name = clean_archive_path(member.name, "shard archive member").as_posix()
            if member_name != SHARD_INVENTORY:
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"cannot read {SHARD_INVENTORY} from shard archive")
            inventory = as_dict(load_json_bytes(extracted.read(), SHARD_INVENTORY), SHARD_INVENTORY)
            value = inventory.get("source_registry_sha256")
            if not isinstance(value, str) or not value:
                raise ValueError("shard inventory source_registry_sha256 is required")
            return value
    raise ValueError(f"shard archive does not contain {SHARD_INVENTORY}")


def inspect_release_zip(path: pathlib.Path, shard_archive: pathlib.Path | None) -> dict[str, Any]:
    members = zip_members(path)
    manifest_data = members.get("manifest.json")
    if manifest_data is None:
        raise ValueError("release zip does not contain manifest.json")
    manifest = as_dict(load_json_bytes(manifest_data, "manifest.json"), "manifest.json")
    expected_paths = release_paths(manifest)
    actual_paths = sorted(members)
    if sorted(expected_paths) != actual_paths:
        missing = sorted(set(expected_paths).difference(actual_paths))
        extra = sorted(set(actual_paths).difference(expected_paths))
        raise ValueError(f"release zip entries do not match manifest paths; missing={missing} extra={extra}")

    registry_sha = ""
    for index, value in enumerate(as_list(manifest.get("artifacts"), "artifacts")):
        artifact = as_dict(value, f"artifacts[{index}]")
        artifact_path = clean_archive_path(artifact.get("path"), f"artifacts[{index}].path").as_posix()
        data = members[artifact_path]
        if artifact.get("bytes") != len(data):
            raise ValueError(f"{artifact_path} byte count mismatch")
        actual_sha = sha256_bytes(data)
        if artifact.get("sha256") != actual_sha:
            raise ValueError(f"{artifact_path} checksum mismatch")
        if artifact_path == REGISTRY_PATH:
            registry_sha = actual_sha
    if not registry_sha:
        raise ValueError(f"release zip does not contain {REGISTRY_PATH}")

    shard_mode = "not_checked"
    if shard_archive is not None:
        shard_sha = shard_source_sha256(shard_archive)
        if shard_sha != registry_sha:
            raise ValueError(
                "shard archive source registry sha does not match release zip registry sha: "
                f"{shard_sha} != {registry_sha}"
            )
        shard_mode = "matched"

    return {
        "entries": len(actual_paths),
        "registry_sha256": registry_sha,
        "shard_archive": shard_mode,
        "bytes": path.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", type=pathlib.Path, help="inspect an existing release zip instead of writing one")
    parser.add_argument("--shard-archive", type=pathlib.Path, help="optional data-go-kr-shards.tar.gz consistency check")
    args = parser.parse_args()

    try:
        if args.check:
            summary = inspect_release_zip(args.check, shard_archive=args.shard_archive)
            print(
                f"ok {args.check} "
                f"(entries={summary['entries']}, registry_sha256={summary['registry_sha256']}, "
                f"shard_archive={summary['shard_archive']})"
            )
            return 0
        summary = package_release_zip(args.manifest, args.output)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL package registry release: {exc}")
        return 1

    print(f"wrote {args.output} (entries={summary['entries']}, bytes={summary['bytes']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
