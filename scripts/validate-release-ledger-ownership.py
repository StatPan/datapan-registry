#!/usr/bin/env python3
"""Validate release-ledger ownership coverage for manifest artifact kinds."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any


DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_OWNERSHIP = pathlib.Path("docs/release-ledger-ownership.json")


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def as_non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def artifact_kinds(manifest: dict[str, Any]) -> set[str]:
    kinds: set[str] = set()
    for index, artifact in enumerate(as_list(manifest.get("artifacts"), "manifest.artifacts")):
        if not isinstance(artifact, dict):
            raise ValueError(f"manifest.artifacts[{index}] must be an object")
        kinds.add(as_non_empty_string(artifact.get("kind"), f"manifest.artifacts[{index}].kind"))
    return kinds


def validate_entry(entry: dict[str, Any], index: int) -> list[str]:
    entry_id = as_non_empty_string(entry.get("id"), f"entries[{index}].id")
    as_non_empty_string(entry.get("owner"), f"{entry_id}.owner")
    as_non_empty_string(entry.get("manifest_relationship"), f"{entry_id}.manifest_relationship")
    as_non_empty_string(entry.get("schema_relationship"), f"{entry_id}.schema_relationship")
    as_non_empty_string(entry.get("package_relationship"), f"{entry_id}.package_relationship")
    as_non_empty_string(entry.get("exemption_boundary"), f"{entry_id}.exemption_boundary")

    generated_by = as_list(entry.get("generated_by"), f"{entry_id}.generated_by")
    checked_by = as_list(entry.get("checked_by"), f"{entry_id}.checked_by")
    if not generated_by:
        raise ValueError(f"{entry_id}.generated_by must not be empty")
    if not checked_by:
        raise ValueError(f"{entry_id}.checked_by must not be empty")
    for command_index, command in enumerate(generated_by):
        as_non_empty_string(command, f"{entry_id}.generated_by[{command_index}]")
    for command_index, command in enumerate(checked_by):
        as_non_empty_string(command, f"{entry_id}.checked_by[{command_index}]")

    kinds = as_list(entry.get("artifact_kinds"), f"{entry_id}.artifact_kinds")
    if not kinds:
        raise ValueError(f"{entry_id}.artifact_kinds must not be empty")
    result: list[str] = []
    for kind_index, kind in enumerate(kinds):
        result.append(as_non_empty_string(kind, f"{entry_id}.artifact_kinds[{kind_index}]"))
    return result


def validate_distribution_artifacts(ownership: dict[str, Any]) -> None:
    for index, artifact in enumerate(as_list(ownership.get("distribution_artifacts", []), "distribution_artifacts")):
        if not isinstance(artifact, dict):
            raise ValueError(f"distribution_artifacts[{index}] must be an object")
        artifact_id = as_non_empty_string(artifact.get("id"), f"distribution_artifacts[{index}].id")
        as_non_empty_string(artifact.get("owner"), f"{artifact_id}.owner")
        as_non_empty_string(artifact.get("relationship"), f"{artifact_id}.relationship")
        for field in ("generated_by", "checked_by"):
            commands = as_list(artifact.get(field), f"{artifact_id}.{field}")
            if not commands:
                raise ValueError(f"{artifact_id}.{field} must not be empty")
            for command_index, command in enumerate(commands):
                as_non_empty_string(command, f"{artifact_id}.{field}[{command_index}]")


def validate_ownership(manifest: dict[str, Any], ownership: dict[str, Any]) -> tuple[int, int]:
    if ownership.get("schema_version") != "datapan.release-ledger-ownership.v1":
        raise ValueError("unexpected release ledger ownership schema_version")

    manifest_kinds = artifact_kinds(manifest)
    entries = as_list(ownership.get("entries"), "entries")
    if not entries:
        raise ValueError("entries must not be empty")

    covered_by_kind: dict[str, str] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"entries[{index}] must be an object")
        entry_id = as_non_empty_string(entry.get("id"), f"entries[{index}].id")
        for kind in validate_entry(entry, index):
            previous = covered_by_kind.get(kind)
            if previous is not None:
                raise ValueError(f"artifact kind {kind!r} is covered by both {previous!r} and {entry_id!r}")
            covered_by_kind[kind] = entry_id

    missing = sorted(manifest_kinds - set(covered_by_kind))
    extra = sorted(set(covered_by_kind) - manifest_kinds)
    if missing:
        raise ValueError(f"manifest artifact kinds missing release-ledger ownership: {', '.join(missing)}")
    if extra:
        raise ValueError(f"release-ledger ownership references non-manifest artifact kinds: {', '.join(extra)}")

    validate_distribution_artifacts(ownership)
    return len(manifest_kinds), len(entries)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    parser.add_argument("--ownership", default=DEFAULT_OWNERSHIP, type=pathlib.Path)
    args = parser.parse_args()

    try:
        manifest = load_json(args.manifest)
        ownership = load_json(args.ownership)
        kind_count, entry_count = validate_ownership(manifest, ownership)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL release ledger ownership: {exc}", file=sys.stderr)
        return 1

    print(f"ok release ledger ownership (artifact_kinds={kind_count}, entries={entry_count})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
