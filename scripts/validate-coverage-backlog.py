#!/usr/bin/env python3
"""Validate the generated data.go.kr coverage backlog."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before validating coverage backlog") from exc


EXPECTED_SCHEMA_VERSION = "datapan.coverage-backlog.v1"
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.coverage-backlog.v1.schema.json")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: Any, path: pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def normalize_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def validate_schema(report: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = as_dict(load_json(schema_path), schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.path) or "<root>"
        raise ValueError(f"schema validation failed at {path}: {error.message}")


def validate_report(
    report_path: pathlib.Path,
    markdown_path: pathlib.Path,
    generator: pathlib.Path,
    schema_path: pathlib.Path,
) -> None:
    report = as_dict(load_json(report_path), report_path)
    if report.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version expected {EXPECTED_SCHEMA_VERSION}, got {report.get('schema_version')}"
        )
    validate_schema(report, schema_path)

    required_arrays = ["institutions", "uncovered_apis", "runtime_reactivation_apis", "runtime_repair_apis"]
    for key in required_arrays:
        if not isinstance(report.get(key), list):
            raise ValueError(f"{key} must be an array")

    summary = as_dict(report.get("summary"), report_path)
    institutions = report["institutions"]
    uncovered_apis = report["uncovered_apis"]
    runtime_reactivation_apis = report["runtime_reactivation_apis"]
    runtime_repair_apis = report["runtime_repair_apis"]

    if summary.get("institutions") != len(institutions):
        raise ValueError("summary.institutions does not match institutions array length")
    if summary.get("uncovered_api_count") != len(uncovered_apis):
        raise ValueError("summary.uncovered_api_count does not match uncovered_apis length")
    if summary.get("runtime_reactivation_api_count") != len(runtime_reactivation_apis):
        raise ValueError(
            "summary.runtime_reactivation_api_count does not match runtime_reactivation_apis length"
        )
    if summary.get("runtime_repair_api_count") != len(runtime_repair_apis):
        raise ValueError("summary.runtime_repair_api_count does not match runtime_repair_apis length")

    api_total = summary.get("api_total")
    covered_api_count = summary.get("covered_api_count")
    uncovered_api_count = summary.get("uncovered_api_count")
    if api_total != covered_api_count + uncovered_api_count:
        raise ValueError("api_total must equal covered_api_count + uncovered_api_count")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_json = pathlib.Path(temp_dir) / "coverage-backlog.json"
        temp_md = pathlib.Path(temp_dir) / "coverage-backlog.md"
        subprocess.run(
            [
                sys.executable,
                str(generator),
                "--output",
                str(temp_json),
                "--markdown-output",
                str(temp_md),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        expected = as_dict(load_json(temp_json), temp_json)
        expected_markdown = temp_md.read_text(encoding="utf-8")

    if normalize_json(report) != normalize_json(expected):
        raise ValueError("report is stale; regenerate with scripts/generate-coverage-backlog.py")
    if markdown_path.exists() and markdown_path.read_text(encoding="utf-8") != expected_markdown:
        raise ValueError(f"{markdown_path} is stale; regenerate with scripts/generate-coverage-backlog.py")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generator",
        default="scripts/generate-coverage-backlog.py",
        type=pathlib.Path,
        help="coverage backlog generator path",
    )
    parser.add_argument(
        "--markdown",
        default="docs/data-go-kr-coverage-backlog.md",
        type=pathlib.Path,
        help="generated coverage backlog markdown path",
    )
    parser.add_argument(
        "--schema",
        default=DEFAULT_SCHEMA,
        type=pathlib.Path,
        help="coverage backlog schema path",
    )
    parser.add_argument(
        "report",
        nargs="?",
        default=pathlib.Path("reports/data-go-kr/coverage-backlog.json"),
        type=pathlib.Path,
        help="coverage backlog report to validate",
    )
    args = parser.parse_args()

    try:
        validate_report(args.report, args.markdown, args.generator, args.schema)
    except Exception as exc:  # noqa: BLE001 - report all validation blockers
        print(f"FAIL {args.report}: {exc}", file=sys.stderr)
        return 1

    report = as_dict(load_json(args.report), args.report)
    summary = as_dict(report.get("summary"), args.report)
    print(
        f"ok {args.report} "
        f"(apis={summary.get('api_total')}, uncovered={summary.get('uncovered_api_count')}, "
        f"runtime_reactivation={summary.get('runtime_reactivation_api_count')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
