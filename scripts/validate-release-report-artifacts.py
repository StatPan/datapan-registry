#!/usr/bin/env python3
"""Validate manifest coverage for top-level schema-backed release reports."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any


DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_REPORT_GLOB = "reports/*.json"
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
        covered += 1

    if failures:
        raise ValueError("; ".join(failures))
    return covered, exempt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    parser.add_argument("--reports-glob", default=DEFAULT_REPORT_GLOB)
    args = parser.parse_args()

    try:
        covered, exempt = validate_release_report_artifacts(args.manifest, args.reports_glob)
    except Exception as exc:  # noqa: BLE001 - report all release coverage blockers
        print(f"FAIL release report artifacts: {exc}", file=sys.stderr)
        return 1

    print(f"ok release report artifacts (manifest_bound={covered}, exempt_receipts={exempt})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
