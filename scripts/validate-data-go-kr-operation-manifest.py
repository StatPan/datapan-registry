#!/usr/bin/env python3
"""Fail closed when the checked-in data.go.kr operation manifest drifts."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import sys
from typing import Any

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "reports/data-go-kr/operation-manifest.json"
SCHEMA = ROOT / "schemas/datapan.data-go-kr-operation-manifest.v1.schema.json"
REGISTRY = ROOT / "data/data-go-kr.registry.json"
RELEASE_MANIFEST = ROOT / "manifest.json"
GENERATOR_PATH = ROOT / "scripts/generate-data-go-kr-operation-manifest.py"
EXPECTATION = ROOT / "policy/data-go-kr-operation-denominator-expectation.json"
EXPECTATION_SCHEMA = ROOT / "schemas/datapan.data-go-kr-operation-denominator-expectation.v1.schema.json"


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def generator_module():
    spec = importlib.util.spec_from_file_location("data_go_kr_operation_manifest", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load operation manifest generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def artifact_by_path(release_manifest: dict[str, Any], path: pathlib.Path) -> dict[str, Any]:
    relative = path.relative_to(ROOT).as_posix()
    matches = [item for item in release_manifest.get("artifacts", []) if item.get("path") == relative]
    fail(len(matches) == 1, f"release manifest must bind exactly one {relative}")
    return matches[0]


def validate(
    manifest: dict[str, Any],
    schema: dict[str, Any],
    registry: list[dict[str, Any]],
    release_manifest: dict[str, Any],
    expectation: dict[str, Any] | None = None,
    *,
    registry_path: pathlib.Path = REGISTRY,
    expectation_schema: dict[str, Any] | None = None,
) -> None:
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(manifest)
    expectation = expectation if expectation is not None else load(EXPECTATION)
    expectation_schema = expectation_schema if expectation_schema is not None else load(EXPECTATION_SCHEMA)
    jsonschema.Draft202012Validator(expectation_schema, format_checker=jsonschema.FormatChecker()).validate(expectation)
    source_bytes = registry_path.read_bytes()
    snapshot = {"path": registry_path.relative_to(ROOT).as_posix(), "bytes": len(source_bytes), "sha256": hashlib.sha256(source_bytes).hexdigest()}
    fail(manifest["source_snapshot"] == snapshot, "source snapshot binding drift")
    fail(expectation["source_snapshot"] == snapshot, "denominator expectation is stale for the source snapshot")
    generator = generator_module()
    previous_registry = generator.REGISTRY
    generator.REGISTRY = registry_path
    try:
        expected = generator.build(registry)
    finally:
        generator.REGISTRY = previous_registry
    fail(manifest == expected, "operation manifest does not deterministically match source snapshot")
    summary = manifest["summary"]
    fail(summary["identity_collisions"] == 0 and summary["identity_omissions"] == 0, "operation identity collision or omission")
    fail(summary == expectation["expected_summary"], "operation denominator summary does not match the source-bound expectation")
    fail(summary["api_operations"] == sum(summary["protocols"].values()), "API denominator does not equal its REST and SOAP split")
    for path, kind, expected_schema in (
        (MANIFEST, "data_go_kr_operation_manifest", schema["$id"]),
        (SCHEMA, "schema", None),
        (REGISTRY, "registry", None),
        (EXPECTATION, "operation_denominator_expectation", expectation_schema["$id"]),
        (EXPECTATION_SCHEMA, "schema", None),
    ):
        artifact = artifact_by_path(release_manifest, path)
        data = path.read_bytes()
        fail((artifact.get("bytes"), artifact.get("sha256")) == (len(data), hashlib.sha256(data).hexdigest()), f"release manifest digest drift: {path.relative_to(ROOT)}")
        if expected_schema is not None:
            fail(artifact.get("kind") == kind and artifact.get("schema") == expected_schema, "operation manifest release artifact contract drift")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        validate(load(MANIFEST), load(SCHEMA), load(REGISTRY), load(RELEASE_MANIFEST))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL data.go.kr operation manifest: {exc}", file=sys.stderr)
        return 1
    summary = load(MANIFEST)["summary"]
    print(f"ok data.go.kr operation manifest (api={summary['api_operations']}, excluded_link={summary['exclusions']['link_operations']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
