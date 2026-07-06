#!/usr/bin/env python3
"""Validate datapan-registry install smoke summary artifacts."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running install smoke summary validation"
    ) from exc


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, path: pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def validate_consistency(path: pathlib.Path, summary: dict[str, Any]) -> None:
    registry = as_dict(summary.get("registry"), path)
    release = as_dict(summary.get("release"), path)

    checksum_checked = registry.get("checksum_checked")
    release_zip_checked = release.get("release_zip_checked")
    if checksum_checked != release_zip_checked:
        raise ValueError("registry.checksum_checked must match release.release_zip_checked")

    checksum_fields = ["canonical_archive_path", "bytes", "sha256"]
    present_checksum_fields = [field for field in checksum_fields if field in registry]
    if release_zip_checked:
        missing = sorted(set(checksum_fields).difference(registry))
        if missing:
            raise ValueError(f"checksum-checked summaries must include registry fields: {', '.join(missing)}")
        if not isinstance(release.get("release_zip"), str) or not release["release_zip"]:
            raise ValueError("release.release_zip is required when release_zip_checked is true")
        if registry.get("bytes") != registry.get("install_bytes"):
            raise ValueError("registry.bytes must match registry.install_bytes when checksum-checked")
    else:
        if release.get("release_zip") is not None:
            raise ValueError("release.release_zip must be null when release_zip_checked is false")
        if present_checksum_fields:
            raise ValueError(
                "non-checksum summaries must not include registry fields: "
                + ", ".join(sorted(present_checksum_fields))
            )

    mode = summary.get("mode")
    shards_present = release.get("shards_asset_present")
    if mode == "shard_validated":
        if shards_present is not True or release.get("shards_validated") is not True:
            raise ValueError("shard_validated mode requires present and validated shard metadata")
        if release.get("shards_inventory_present") is not True:
            raise ValueError("shard_validated mode requires shard inventory metadata")
        if int(release.get("shards_count", 0)) <= 0 or int(release.get("shards_records", 0)) <= 0:
            raise ValueError("shard_validated mode requires positive shard counts")
    elif mode == "monolith_fallback":
        if shards_present is not False or release.get("shards_validated") is not False:
            raise ValueError("monolith_fallback mode must not report validated shard metadata")
        if release.get("shards_inventory_present") is not False:
            raise ValueError("monolith_fallback mode must not report shard inventory metadata")
        if release.get("shards_count") != 0 or release.get("shards_records") != 0:
            raise ValueError("monolith_fallback mode requires zero shard counts")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.install-smoke-summary.v1.schema.json",
        type=pathlib.Path,
        help="install smoke summary JSON Schema path",
    )
    parser.add_argument("summaries", nargs="+", type=pathlib.Path)
    args = parser.parse_args()

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for summary_path in args.summaries:
        try:
            instance = as_dict(load_json(summary_path), summary_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
            if not errors:
                validate_consistency(summary_path, instance)
        except Exception as exc:  # noqa: BLE001 - report all validation blockers
            print(f"FAIL {summary_path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if errors:
            failures += 1
            print(f"FAIL {summary_path}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            continue

        release = as_dict(instance.get("release"), summary_path)
        print(
            f"ok {summary_path} "
            f"(mode={instance.get('mode')}, release_zip_checked={release.get('release_zip_checked')})"
        )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
