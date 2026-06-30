#!/usr/bin/env python3
"""Synchronize release schema artifacts with checked-in schema files."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import sys


SCHEMA_INDEX_PATH = pathlib.Path("schemas/index.json")
MANIFEST_PATH = pathlib.Path("manifest.json")


def load_json(path: pathlib.Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def stable_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def schema_contract_version(name: str) -> tuple[str, str]:
    name = name.removeprefix("datapan.").removesuffix(".schema.json")
    parts = name.split(".")
    if len(parts) < 2:
        return name, ""
    return ".".join(parts[:-1]), parts[-1]


def schema_paths() -> list[pathlib.Path]:
    return sorted(pathlib.Path("schemas").glob("*.schema.json"))


def existing_schema_order(index: dict[str, object]) -> list[str]:
    schemas = index.get("schemas")
    if not isinstance(schemas, list):
        return []
    return [str(item.get("path")) for item in schemas if isinstance(item, dict) and item.get("path")]


def ordered_schema_paths(index: dict[str, object]) -> list[pathlib.Path]:
    paths_by_name = {str(path): path for path in schema_paths()}
    ordered: list[pathlib.Path] = []
    for path_name in existing_schema_order(index):
        path = paths_by_name.pop(path_name, None)
        if path is not None:
            ordered.append(path)
    ordered.extend(paths_by_name[path_name] for path_name in sorted(paths_by_name))
    return ordered


def schema_entry(path: pathlib.Path) -> dict[str, object]:
    data = path.read_bytes()
    payload = load_json(path)
    contract, version = schema_contract_version(path.name)
    schema_id = payload.get("$id")
    title = payload.get("title")
    if not isinstance(schema_id, str) or not schema_id:
        raise ValueError(f"{path} missing $id")
    if not isinstance(title, str) or not title:
        raise ValueError(f"{path} missing title")
    return {
        "path": path.as_posix(),
        "id": schema_id,
        "title": title,
        "contract": contract,
        "version": version,
        "bytes": len(data),
        "sha256": sha256_bytes(data),
    }


def expected_schema_index(index: dict[str, object], generated_at: str | None) -> dict[str, object]:
    entries = [schema_entry(path) for path in ordered_schema_paths(index)]
    return {
        "schema_version": "datapan.schema-index.v1",
        "generated_at": generated_at or str(index.get("generated_at")),
        "datapan_version": str(index.get("datapan_version")),
        "count": len(entries),
        "schemas": entries,
    }


def manifest_schema_artifact(entry: dict[str, object]) -> dict[str, object]:
    return {
        "path": entry["path"],
        "kind": "schema",
        "bytes": entry["bytes"],
        "sha256": entry["sha256"],
    }


def expected_manifest(
    manifest: dict[str, object],
    index_bytes: bytes,
    index: dict[str, object],
    generated_at: str | None,
) -> dict[str, object]:
    expected = copy.deepcopy(manifest)
    if generated_at is not None:
        expected["generated_at"] = generated_at
    schema_artifacts = [manifest_schema_artifact(entry) for entry in index["schemas"]]  # type: ignore[index]
    non_schema_artifacts: list[dict[str, object]] = []
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be an array")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("manifest.artifacts entries must be objects")
        if artifact.get("kind") == "schema":
            continue
        updated = copy.deepcopy(artifact)
        if updated.get("kind") == "schema_index" and updated.get("path") == SCHEMA_INDEX_PATH.as_posix():
            updated["bytes"] = len(index_bytes)
            updated["sha256"] = sha256_bytes(index_bytes)
        non_schema_artifacts.append(updated)
    expected["artifact_count"] = len(schema_artifacts) + len(non_schema_artifacts)
    expected["artifacts"] = schema_artifacts + non_schema_artifacts
    return expected


def explain_drift(current_index: dict[str, object], expected_index: dict[str, object], current_manifest: dict[str, object], expected_manifest_value: dict[str, object]) -> None:
    current_paths = {str(item.get("path")) for item in current_index.get("schemas", []) if isinstance(item, dict)}
    expected_paths = {str(item.get("path")) for item in expected_index.get("schemas", []) if isinstance(item, dict)}
    missing_index = sorted(expected_paths.difference(current_paths))
    extra_index = sorted(current_paths.difference(expected_paths))
    if missing_index:
        print("schema index missing:", ", ".join(missing_index), file=sys.stderr)
    if extra_index:
        print("schema index extra:", ", ".join(extra_index), file=sys.stderr)
    if current_index.get("count") != expected_index.get("count"):
        print(
            f"schema index count mismatch: expected {expected_index.get('count')}, got {current_index.get('count')}",
            file=sys.stderr,
        )
    if current_manifest.get("artifact_count") != expected_manifest_value.get("artifact_count"):
        print(
            "manifest artifact_count mismatch: "
            f"expected {expected_manifest_value.get('artifact_count')}, got {current_manifest.get('artifact_count')}",
            file=sys.stderr,
        )
    if current_index != expected_index:
        print("schemas/index.json drift detected", file=sys.stderr)
    if current_manifest != expected_manifest_value:
        print("manifest.json schema artifact drift detected", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail when generated schema artifacts are out of date")
    mode.add_argument("--write", action="store_true", help="rewrite schemas/index.json and manifest.json")
    parser.add_argument(
        "--generated-at",
        help="generated_at value for schemas/index.json; defaults to the current checked-in value",
    )
    args = parser.parse_args()

    current_index = load_json(SCHEMA_INDEX_PATH)
    current_manifest = load_json(MANIFEST_PATH)
    next_index = expected_schema_index(current_index, args.generated_at)
    next_index_bytes = stable_json_bytes(next_index)
    next_manifest = expected_manifest(current_manifest, next_index_bytes, next_index, args.generated_at)

    if args.check:
        if current_index != next_index or current_manifest != next_manifest:
            explain_drift(current_index, next_index, current_manifest, next_manifest)
            return 1
        print(
            f"ok release schema artifacts (schemas={next_index['count']}, artifacts={next_manifest['artifact_count']})"
        )
        return 0

    SCHEMA_INDEX_PATH.write_bytes(next_index_bytes)
    MANIFEST_PATH.write_bytes(stable_json_bytes(next_manifest))
    print(f"wrote {SCHEMA_INDEX_PATH} (schemas={next_index['count']})")
    print(f"wrote {MANIFEST_PATH} (artifacts={next_manifest['artifact_count']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
