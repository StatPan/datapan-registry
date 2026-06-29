#!/usr/bin/env python3
"""Validate checked-in Datapan institution API overview reports."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
from typing import Any


EXPECTED_SCHEMA_VERSION = "datapan.institution-api-overview.v1"


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: Any, path: pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def default_reports() -> list[pathlib.Path]:
    return sorted(pathlib.Path("reports").glob("*/institution-api-overview.json"))


def normalize_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def validate_report(report_path: pathlib.Path, markdown_path: pathlib.Path, generator: pathlib.Path) -> None:
    report = as_dict(load_json(report_path), report_path)
    if report.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version expected {EXPECTED_SCHEMA_VERSION}, got {report.get('schema_version')}"
        )

    generation_inputs = report.get("generation_inputs")
    if not isinstance(generation_inputs, dict):
        raise ValueError("generation_inputs must be an object")

    required_inputs = ["registry", "dependencies", "latest_verification", "coverage", "provider_index"]
    for key in required_inputs:
        raw = generation_inputs.get(key)
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"generation_inputs.{key} must be a non-empty path")
        if not pathlib.Path(raw).exists():
            raise ValueError(f"generation_inputs.{key} does not exist: {raw}")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_json = pathlib.Path(temp_dir) / "institution-api-overview.json"
        temp_md = pathlib.Path(temp_dir) / "institution-api-overview.md"
        command = [
            sys.executable,
            str(generator),
            "--registry",
            generation_inputs["registry"],
            "--dependencies",
            generation_inputs["dependencies"],
            "--latest-verification",
            generation_inputs["latest_verification"],
            "--coverage",
            generation_inputs["coverage"],
            "--provider-index",
            generation_inputs["provider_index"],
            "--output",
            str(temp_json),
            "--markdown-output",
            str(temp_md),
        ]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        expected = as_dict(load_json(temp_json), temp_json)
        expected_markdown = temp_md.read_text(encoding="utf-8")

    if normalize_json(report) != normalize_json(expected):
        raise ValueError("report is stale; regenerate with scripts/generate-institution-api-overview.py")
    if markdown_path.exists():
        actual_markdown = markdown_path.read_text(encoding="utf-8")
        if actual_markdown != expected_markdown:
            raise ValueError(f"{markdown_path} is stale; regenerate with scripts/generate-institution-api-overview.py")

    summary = as_dict(report.get("summary"), report_path)
    institutions = report.get("institutions")
    if not isinstance(institutions, list) or not institutions:
        raise ValueError("institutions must be a non-empty array")
    if summary.get("institutions") != len(institutions):
        raise ValueError("summary.institutions does not match institutions array length")

    operation_sum = 0
    for index, institution in enumerate(institutions):
        if not isinstance(institution, dict):
            raise ValueError(f"institutions[{index}] must be an object")
        operation_count = institution.get("operation_count")
        api_count = institution.get("api_count")
        if not isinstance(operation_count, int) or operation_count < 0:
            raise ValueError(f"institutions[{index}].operation_count must be a non-negative integer")
        if not isinstance(api_count, int) or api_count < 0:
            raise ValueError(f"institutions[{index}].api_count must be a non-negative integer")
        operation_sum += operation_count

    if summary.get("operations") != operation_sum:
        raise ValueError(f"summary.operations expected {operation_sum}, got {summary.get('operations')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generator",
        default="scripts/generate-institution-api-overview.py",
        type=pathlib.Path,
        help="institution overview generator path",
    )
    parser.add_argument(
        "--markdown",
        default="docs/data-go-kr-institution-api-overview.md",
        type=pathlib.Path,
        help="human-readable institution overview path",
    )
    parser.add_argument(
        "reports",
        nargs="*",
        type=pathlib.Path,
        help="reports to validate; defaults to reports/*/institution-api-overview.json",
    )
    args = parser.parse_args()

    reports = args.reports or default_reports()
    if not reports:
        print("no institution API overview reports found", file=sys.stderr)
        return 1

    failures = 0
    for report_path in reports:
        try:
            validate_report(report_path, args.markdown, args.generator)
        except Exception as exc:  # noqa: BLE001 - report all validation blockers
            failures += 1
            print(f"FAIL {report_path}: {exc}", file=sys.stderr)
            continue

        report = as_dict(load_json(report_path), report_path)
        summary = as_dict(report.get("summary"), report_path)
        print(
            f"ok {report_path} "
            f"(institutions={summary.get('institutions')}, apis={summary.get('apis')}, "
            f"operations={summary.get('operations')})"
        )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
