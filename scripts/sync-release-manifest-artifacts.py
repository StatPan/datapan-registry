#!/usr/bin/env python3
"""Synchronize manifest digests for checked-in non-schema release artifacts."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import sys
from typing import Any


DEFAULT_MANIFEST = pathlib.Path("manifest.json")
EXTERNALLY_MATERIALIZED_KINDS = {"schema", "schema_index", "registry"}


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def stable_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def file_digest(path: pathlib.Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def synced_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    expected = copy.deepcopy(manifest)
    artifacts = expected.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be an array")

    synced_paths: list[str] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise ValueError(f"manifest.artifacts[{index}] must be an object")
        path_value = artifact.get("path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError(f"manifest.artifacts[{index}].path must be a non-empty string")
        if artifact.get("kind") in EXTERNALLY_MATERIALIZED_KINDS:
            continue

        path = pathlib.Path(path_value)
        if not path.is_file():
            raise ValueError(f"manifest artifact is missing: {path_value}")
        byte_count, sha256 = file_digest(path)
        artifact["bytes"] = byte_count
        artifact["sha256"] = sha256
        synced_paths.append(path_value)

    return expected, synced_paths


def explain_drift(current: dict[str, Any], expected: dict[str, Any]) -> None:
    current_artifacts = current.get("artifacts")
    expected_artifacts = expected.get("artifacts")
    if not isinstance(current_artifacts, list) or not isinstance(expected_artifacts, list):
        print("manifest artifact drift detected", file=sys.stderr)
        return

    for current_item, expected_item in zip(current_artifacts, expected_artifacts):
        if not isinstance(current_item, dict) or not isinstance(expected_item, dict):
            continue
        if current_item == expected_item:
            continue
        path = current_item.get("path", "<unknown>")
        for key in ("bytes", "sha256"):
            if current_item.get(key) != expected_item.get(key):
                print(
                    f"{path}: manifest {key} expected {expected_item.get(key)}, got {current_item.get(key)}",
                    file=sys.stderr,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail when manifest artifact digests are stale")
    mode.add_argument("--write", action="store_true", help="rewrite non-schema artifact bytes and sha256")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    args = parser.parse_args()

    try:
        current = load_json(args.manifest)
        expected, synced_paths = synced_manifest(current)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL sync release manifest artifacts: {exc}", file=sys.stderr)
        return 1

    if args.check:
        if current != expected:
            explain_drift(current, expected)
            return 1
        print(f"ok release manifest artifacts (artifacts={len(synced_paths)})")
        return 0

    args.manifest.write_bytes(stable_json_bytes(expected))
    print(f"wrote {args.manifest} (artifacts={len(synced_paths)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
