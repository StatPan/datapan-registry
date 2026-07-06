#!/usr/bin/env python3
"""Validate manifest coverage for top-level schema-backed release reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any


DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_REPORT_GLOB = "reports/*.json"
DEFAULT_SOURCE_REPORT_INVENTORY = pathlib.Path("reports/source-report-inventory.json")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
EXEMPT_REPORTS = {
    "reports/latest-release-readiness.json": "release readiness receipt generated from the manifest",
    "reports/latest-release-verification.json": "release verification receipt generated from the manifest",
}


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, label: str | pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def schema_uri(schema_version: str) -> str:
    return f"https://schemas.datapan.dev/{schema_version}.schema.json"


def file_digest(path: pathlib.Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def manifest_artifacts(manifest_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    manifest = as_dict(load_json(manifest_path), manifest_path)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be an array")

    by_path: dict[str, dict[str, Any]] = {}
    for index, artifact in enumerate(artifacts):
        item = as_dict(artifact, f"manifest.artifacts[{index}]")
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"manifest.artifacts[{index}].path must be a non-empty string")
        if path in by_path:
            raise ValueError(f"duplicate manifest artifact path: {path}")
        by_path[path] = item
    return by_path


def schema_backed_reports(report_glob: str) -> list[tuple[pathlib.Path, str]]:
    reports: list[tuple[pathlib.Path, str]] = []
    for path in sorted(pathlib.Path().glob(report_glob)):
        if not path.is_file():
            continue
        try:
            payload = as_dict(load_json(path), path)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON") from exc
        schema_version = payload.get("schema_version")
        if isinstance(schema_version, str) and schema_version:
            reports.append((path, schema_version))
    return reports


def validate_release_report_artifacts(manifest_path: pathlib.Path, report_glob: str) -> tuple[int, int]:
    artifacts = manifest_artifacts(manifest_path)
    reports = schema_backed_reports(report_glob)
    failures: list[str] = []
    covered = 0
    exempt = 0

    for path, schema_version in reports:
        report_path = path.as_posix()
        if report_path in EXEMPT_REPORTS:
            exempt += 1
            continue

        artifact = artifacts.get(report_path)
        if artifact is None:
            failures.append(
                f"{report_path}: schema-backed top-level report is not listed in {manifest_path}"
            )
            continue

        expected_schema = schema_uri(schema_version)
        actual_schema = artifact.get("schema")
        if actual_schema != expected_schema:
            failures.append(
                f"{report_path}: manifest schema expected {expected_schema}, got {actual_schema}"
            )

        actual_bytes, actual_sha256 = file_digest(path)
        manifest_bytes = artifact.get("bytes")
        manifest_sha256 = artifact.get("sha256")
        if not isinstance(manifest_bytes, int) or manifest_bytes <= 0:
            failures.append(
                f"{report_path}: manifest bytes must be a positive integer, got {manifest_bytes}"
            )
        elif manifest_bytes != actual_bytes:
            failures.append(
                f"{report_path}: manifest bytes expected {actual_bytes}, got {manifest_bytes}"
            )
        if not isinstance(manifest_sha256, str) or not SHA256_PATTERN.fullmatch(manifest_sha256):
            failures.append(
                f"{report_path}: manifest sha256 must be a 64-character lowercase hex digest, got {manifest_sha256}"
            )
        elif manifest_sha256 != actual_sha256:
            failures.append(
                f"{report_path}: manifest sha256 expected {actual_sha256}, got {manifest_sha256}"
            )
        covered += 1

    if failures:
        raise ValueError("; ".join(failures))
    return covered, exempt


def validate_source_report_inventory_coverage(
    manifest_path: pathlib.Path,
    source_report_inventory_path: pathlib.Path,
) -> int:
    artifacts = manifest_artifacts(manifest_path)
    inventory_path = source_report_inventory_path.as_posix()
    inventory_artifact = artifacts.get(inventory_path)
    if inventory_artifact is None:
        raise ValueError(f"{inventory_path}: source report inventory is not listed in {manifest_path}")
    if inventory_artifact.get("kind") != "source_report_inventory":
        raise ValueError(
            f"{inventory_path}: manifest kind expected source_report_inventory, got {inventory_artifact.get('kind')}"
        )

    inventory = as_dict(load_json(source_report_inventory_path), source_report_inventory_path)
    sources = inventory.get("sources")
    if not isinstance(sources, list):
        raise ValueError(f"{inventory_path}: sources must be an array")

    failures: list[str] = []
    schema_backed = 0
    schema_indexed = 0
    for source_index, source in enumerate(sources):
        if not isinstance(source, dict):
            failures.append(f"{inventory_path}: sources[{source_index}] must be an object")
            continue
        source_id = source.get("source_id", f"#{source_index}")
        present_reports = source.get("present_reports")
        if not isinstance(present_reports, list):
            failures.append(f"{inventory_path}: {source_id}.present_reports must be an array")
            continue
        for report_index, entry in enumerate(present_reports):
            if not isinstance(entry, dict):
                failures.append(
                    f"{inventory_path}: {source_id}.present_reports[{report_index}] must be an object"
                )
                continue
            schema_version = entry.get("schema_version")
            if schema_version is None:
                continue
            schema_backed += 1
            if not isinstance(schema_version, str) or not schema_version:
                failures.append(
                    f"{inventory_path}: {source_id}.present_reports[{report_index}].schema_version "
                    "must be a string"
                )
                continue

            report_path = entry.get("path")
            if not isinstance(report_path, str) or not report_path:
                failures.append(
                    f"{inventory_path}: {source_id}.present_reports[{report_index}].path "
                    "must be a non-empty string"
                )
                continue
            path = pathlib.Path(report_path)
            if not path.is_file():
                failures.append(f"{report_path}: inventory-listed source report file is missing")
                continue

            expected_schema_id = schema_uri(schema_version)
            expected_schema_path = f"schemas/{schema_version}.schema.json"
            if entry.get("expected_schema_id") != expected_schema_id:
                failures.append(
                    f"{report_path}: expected_schema_id expected {expected_schema_id}, got {entry.get('expected_schema_id')}"
                )
            if entry.get("expected_schema_path") != expected_schema_path:
                failures.append(
                    f"{report_path}: expected_schema_path expected {expected_schema_path}, got {entry.get('expected_schema_path')}"
                )
            if entry.get("schema_indexed") is not True:
                failures.append(f"{report_path}: schema_indexed must be true for release-covered source reports")
            else:
                schema_indexed += 1

            actual_bytes, actual_sha256 = file_digest(path)
            if entry.get("bytes") != actual_bytes:
                failures.append(f"{report_path}: inventory bytes expected {actual_bytes}, got {entry.get('bytes')}")
            if entry.get("sha256") != actual_sha256:
                failures.append(f"{report_path}: inventory sha256 expected {actual_sha256}, got {entry.get('sha256')}")

    summary = as_dict(inventory.get("summary"), f"{inventory_path}.summary")
    expected_summary = {
        "schema_backed_reports": schema_backed,
        "schema_indexed_reports": schema_indexed,
        "schema_missing_reports": schema_backed - schema_indexed,
    }
    for key, value in expected_summary.items():
        if summary.get(key) != value:
            failures.append(f"{inventory_path}: summary.{key} expected {value}, got {summary.get(key)}")
    if summary.get("schema_missing_reports") != 0:
        failures.append(f"{inventory_path}: summary.schema_missing_reports must be 0 for release-covered source reports")

    if failures:
        raise ValueError("; ".join(failures))
    return schema_backed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    parser.add_argument("--reports-glob", default=DEFAULT_REPORT_GLOB)
    parser.add_argument(
        "--source-report-inventory",
        default=DEFAULT_SOURCE_REPORT_INVENTORY,
        type=pathlib.Path,
        help="manifest-bound source report inventory used to validate nested schema-backed source reports",
    )
    args = parser.parse_args()

    try:
        covered, exempt = validate_release_report_artifacts(args.manifest, args.reports_glob)
        source_inventory_covered = validate_source_report_inventory_coverage(
            args.manifest,
            args.source_report_inventory,
        )
    except Exception as exc:  # noqa: BLE001 - report all release coverage blockers
        print(f"FAIL release report artifacts: {exc}", file=sys.stderr)
        return 1

    print(
        "ok release report artifacts "
        f"(manifest_bound={covered}, source_inventory_bound={source_inventory_covered}, "
        f"exempt_receipts={exempt})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
