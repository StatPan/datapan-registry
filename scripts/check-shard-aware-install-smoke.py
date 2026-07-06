#!/usr/bin/env python3
"""Validate datapan-registry install smoke JSON for shard-aware fallback."""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("install_json", type=pathlib.Path)
    args = parser.parse_args()

    try:
        summary = validate_install(as_dict(load_json(args.install_json), args.install_json.as_posix()))
    except Exception as exc:  # noqa: BLE001 - CI should show the failed invariant
        print(f"FAIL {args.install_json}: {exc}")
        return 1

    print(
        f"ok {args.install_json} "
        f"(mode={summary['mode']}, specs={summary['specs']}, shards_asset_present={summary['shards_asset_present']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
