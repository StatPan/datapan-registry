#!/usr/bin/env python3
"""Validate datapan-registry install smoke JSON for shard-aware fallback."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import zipfile
from typing import Any

REGISTRY_PATH = "data/data-go-kr.registry.json"


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_json_bytes(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - CI should show the failed invariant
        raise ValueError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def clean_archive_path(value: Any, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if "\\" in value:
        raise ValueError(f"{label} must use portable forward-slash paths")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise ValueError(f"{label} must not escape the release archive root: {value}")
    return path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_zip_registry(release_zip: pathlib.Path) -> dict[str, Any]:
    with zipfile.ZipFile(release_zip) as archive:
        seen: set[str] = set()
        manifest_info: zipfile.ZipInfo | None = None
        registry_info: zipfile.ZipInfo | None = None
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = clean_archive_path(info.filename, "release zip member").as_posix()
            if name in seen:
                raise ValueError(f"release zip contains duplicate member: {name}")
            seen.add(name)
            if name == "manifest.json":
                manifest_info = info
            elif name == REGISTRY_PATH:
                registry_info = info

        if manifest_info is None:
            raise ValueError("release zip does not contain manifest.json")
        manifest = as_dict(load_json_bytes(archive.read(manifest_info), "manifest.json"), "manifest.json")

        if registry_info is None:
            raise ValueError(f"release zip does not contain {REGISTRY_PATH}")
        registry_data = archive.read(registry_info)

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest artifacts must be an array")

    registry_artifact: dict[str, Any] | None = None
    for index, value in enumerate(artifacts):
        artifact = as_dict(value, f"artifacts[{index}]")
        artifact_path = clean_archive_path(artifact.get("path"), f"artifacts[{index}].path").as_posix()
        if artifact_path == REGISTRY_PATH:
            registry_artifact = artifact
            break
    if registry_artifact is None:
        raise ValueError(f"manifest does not contain {REGISTRY_PATH}")

    registry_bytes = len(registry_data)
    registry_sha = sha256_bytes(registry_data)
    if registry_artifact.get("bytes") != registry_bytes:
        raise ValueError(f"{REGISTRY_PATH} byte count mismatch")
    if registry_artifact.get("sha256") != registry_sha:
        raise ValueError(f"{REGISTRY_PATH} checksum mismatch")

    return {"bytes": registry_bytes, "sha256": registry_sha}


def validate_install(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("ok") is not True:
        raise ValueError("install ok must be true")
    if payload.get("provider") != "datapan-registry":
        raise ValueError("install provider must be datapan-registry")
    if not isinstance(payload.get("registry"), str) or not payload["registry"]:
        raise ValueError("install registry path is required")
    if payload.get("installed") is not True:
        raise ValueError("install must write a registry file")
    specs = positive_int(payload.get("specs"), "install specs")
    positive_int(payload.get("bytes"), "install bytes")

    release = as_dict(payload.get("release"), "release")
    shards_present = as_bool(release.get("shards_asset_present"), "release.shards_asset_present")
    shards_validated = as_bool(release.get("shards_validated"), "release.shards_validated")
    shards_inventory_present = as_bool(
        release.get("shards_inventory_present"),
        "release.shards_inventory_present",
    )

    if shards_present:
        if not shards_validated:
            raise ValueError("shard asset is present but shards_validated is not true")
        if not shards_inventory_present:
            raise ValueError("shard asset is present but inventory is not present")
        positive_int(release.get("shards_count"), "release.shards_count")
        positive_int(release.get("shards_records"), "release.shards_records")
        mode = "shard_validated"
    else:
        if shards_validated or shards_inventory_present:
            raise ValueError("shard validation metadata is inconsistent when no shard asset is present")
        mode = "monolith_fallback"

    return {
        "mode": mode,
        "specs": specs,
        "registry": payload["registry"],
        "shards_asset_present": shards_present,
        "shards_validated": shards_validated,
    }


def validate_installed_registry(summary: dict[str, Any], release_zip: pathlib.Path) -> dict[str, Any]:
    expected = release_zip_registry(release_zip)
    registry_path = pathlib.Path(summary["registry"])
    if not registry_path.exists():
        raise ValueError(f"installed registry file is missing: {registry_path}")
    if not registry_path.is_file():
        raise ValueError(f"installed registry path is not a file: {registry_path}")

    installed_data = registry_path.read_bytes()
    installed_bytes = len(installed_data)
    installed_sha = sha256_bytes(installed_data)

    if installed_bytes != expected["bytes"]:
        raise ValueError(
            "installed registry byte count does not match release zip registry: "
            f"{installed_bytes} != {expected['bytes']}"
        )
    if installed_sha != expected["sha256"]:
        raise ValueError(
            "installed registry sha256 does not match release zip registry: "
            f"{installed_sha} != {expected['sha256']}"
        )

    summary["registry_bytes"] = installed_bytes
    summary["registry_sha256"] = installed_sha
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release-zip",
        type=pathlib.Path,
        help="optional release zip whose canonical registry must match the installed registry file",
    )
    parser.add_argument("install_json", type=pathlib.Path)
    args = parser.parse_args()

    try:
        summary = validate_install(as_dict(load_json(args.install_json), args.install_json.as_posix()))
        if args.release_zip is not None:
            summary = validate_installed_registry(summary, args.release_zip)
    except Exception as exc:  # noqa: BLE001 - CI should show the failed invariant
        print(f"FAIL {args.install_json}: {exc}")
        return 1

    details = [
        f"mode={summary['mode']}",
        f"specs={summary['specs']}",
        f"shards_asset_present={summary['shards_asset_present']}",
    ]
    if "registry_sha256" in summary:
        details.append(f"registry_sha256={summary['registry_sha256']}")
    print(f"ok {args.install_json} ({', '.join(details)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
