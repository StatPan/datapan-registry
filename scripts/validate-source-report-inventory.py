#!/usr/bin/env python3
"""Validate the generated source-scoped report inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before validating source report inventory") from exc


EXPECTED_SCHEMA_VERSION = "datapan.source-report-inventory.v1"
DEFAULT_REPORT = pathlib.Path("reports/source-report-inventory.json")
DEFAULT_SCHEMA_INDEX = pathlib.Path("schemas/index.json")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: Any, path: pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def normalize_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def file_digest(path: pathlib.Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def schema_uri(schema_version: str) -> str:
    return f"https://schemas.datapan.dev/{schema_version}.schema.json"


def schema_path(schema_version: str) -> str:
    return f"schemas/{schema_version}.schema.json"


def indexed_schema_ids(schema_index_path: pathlib.Path) -> set[str]:
    schema_index = as_dict(load_json(schema_index_path), schema_index_path)
    schemas = schema_index.get("schemas")
    if not isinstance(schemas, list):
        raise ValueError("schemas/index.json schemas must be an array")
    ids: set[str] = set()
    for index, value in enumerate(schemas):
        if not isinstance(value, dict):
            raise ValueError(f"schemas[{index}] must be an object")
        schema_id = value.get("id")
        if isinstance(schema_id, str) and schema_id:
            ids.add(schema_id)
        path = value.get("path")
        if isinstance(path, str) and path.startswith("schemas/") and path.endswith(".schema.json"):
            version = path.removeprefix("schemas/").removesuffix(".schema.json")
            ids.add(schema_uri(version))
    return ids


def source_profile_paths() -> list[pathlib.Path]:
    return sorted(pathlib.Path("sources").glob("*.json"))


def validate_source_profile_inputs(report: dict[str, Any]) -> None:
    inputs = report.get("source_profile_inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("source_profile_inputs must be a non-empty array")

    expected_paths = [path.as_posix() for path in source_profile_paths()]
    actual_paths = [item.get("path") for item in inputs if isinstance(item, dict)]
    if actual_paths != expected_paths:
        raise ValueError(f"source_profile_inputs paths expected {expected_paths}, got {actual_paths}")

    failures: list[str] = []
    for index, raw_input in enumerate(inputs):
        if not isinstance(raw_input, dict):
            failures.append(f"source_profile_inputs[{index}] must be an object")
            continue
        path_value = raw_input.get("path")
        if not isinstance(path_value, str) or not path_value:
            failures.append(f"source_profile_inputs[{index}].path must be a non-empty string")
            continue
        profile_path = pathlib.Path(path_value)
        if not profile_path.is_file():
            failures.append(f"source_profile_inputs[{index}].path is missing: {path_value}")
            continue
        profile = as_dict(load_json(profile_path), profile_path)
        expected_bytes, expected_sha256 = file_digest(profile_path)
        expected_values = {
            "source_id": profile.get("source_id"),
            "provider": profile.get("provider"),
            "bytes": expected_bytes,
            "sha256": expected_sha256,
        }
        for key, value in expected_values.items():
            if raw_input.get(key) != value:
                failures.append(
                    f"source_profile_inputs[{index}].{key} expected {value}, got {raw_input.get(key)}"
                )

    if failures:
        raise ValueError("; ".join(failures))


def validate_schema_index_input(report: dict[str, Any], schema_index_path: pathlib.Path) -> None:
    raw_input = report.get("schema_index_input")
    if not isinstance(raw_input, dict):
        raise ValueError("schema_index_input must be an object")
    if raw_input.get("path") != schema_index_path.as_posix():
        raise ValueError(
            f"schema_index_input.path expected {schema_index_path.as_posix()}, got {raw_input.get('path')}"
        )
    schema_index = as_dict(load_json(schema_index_path), schema_index_path)
    schemas = schema_index.get("schemas")
    if not isinstance(schemas, list):
        raise ValueError("schemas/index.json schemas must be an array")
    expected_bytes, expected_sha256 = file_digest(schema_index_path)
    expected_values = {
        "schemas": len(schemas),
        "bytes": expected_bytes,
        "sha256": expected_sha256,
    }
    for key, value in expected_values.items():
        if raw_input.get(key) != value:
            raise ValueError(f"schema_index_input.{key} expected {value}, got {raw_input.get(key)}")


def validate_report_digests(report: dict[str, Any]) -> None:
    sources = report.get("sources")
    if not isinstance(sources, list):
        raise ValueError("sources must be an array")

    failures: list[str] = []
    for source_index, source in enumerate(sources):
        if not isinstance(source, dict):
            failures.append(f"sources[{source_index}] must be an object")
            continue
        source_id = source.get("source_id", f"#{source_index}")
        present_reports = source.get("present_reports")
        if not isinstance(present_reports, list):
            failures.append(f"{source_id}.present_reports must be an array")
            continue
        for report_index, entry in enumerate(present_reports):
            if not isinstance(entry, dict):
                failures.append(f"{source_id}.present_reports[{report_index}] must be an object")
                continue
            report_path = entry.get("path")
            if not isinstance(report_path, str) or not report_path:
                failures.append(f"{source_id}.present_reports[{report_index}].path must be a non-empty string")
                continue
            path = pathlib.Path(report_path)
            if not path.is_file():
                failures.append(f"{report_path}: listed source report file is missing")
                continue
            actual_bytes, actual_sha256 = file_digest(path)
            if entry.get("bytes") != actual_bytes:
                failures.append(f"{report_path}: bytes expected {actual_bytes}, got {entry.get('bytes')}")
            if entry.get("sha256") != actual_sha256:
                failures.append(f"{report_path}: sha256 expected {actual_sha256}, got {entry.get('sha256')}")

    if failures:
        raise ValueError("; ".join(failures))


def validate_schema_index_coverage(report: dict[str, Any], schema_index_path: pathlib.Path) -> None:
    schema_ids = indexed_schema_ids(schema_index_path)
    sources = report.get("sources")
    if not isinstance(sources, list):
        raise ValueError("sources must be an array")

    failures: list[str] = []
    schema_backed = 0
    schema_indexed = 0
    for source_index, source in enumerate(sources):
        if not isinstance(source, dict):
            failures.append(f"sources[{source_index}] must be an object")
            continue
        source_id = source.get("source_id", f"#{source_index}")
        present_reports = source.get("present_reports")
        if not isinstance(present_reports, list):
            failures.append(f"{source_id}.present_reports must be an array")
            continue
        for report_index, entry in enumerate(present_reports):
            if not isinstance(entry, dict):
                failures.append(f"{source_id}.present_reports[{report_index}] must be an object")
                continue
            schema_version = entry.get("schema_version")
            if schema_version is None:
                continue
            if not isinstance(schema_version, str) or not schema_version:
                failures.append(f"{source_id}.present_reports[{report_index}].schema_version must be a string")
                continue
            schema_backed += 1
            expected_id = schema_uri(schema_version)
            expected_path = schema_path(schema_version)
            expected_indexed = expected_id in schema_ids
            if expected_indexed:
                schema_indexed += 1
            report_path = entry.get("path", f"{source_id}.present_reports[{report_index}]")
            if entry.get("expected_schema_id") != expected_id:
                failures.append(f"{report_path}: expected_schema_id expected {expected_id}, got {entry.get('expected_schema_id')}")
            if entry.get("expected_schema_path") != expected_path:
                failures.append(f"{report_path}: expected_schema_path expected {expected_path}, got {entry.get('expected_schema_path')}")
            if entry.get("schema_indexed") is not expected_indexed:
                failures.append(f"{report_path}: schema_indexed expected {expected_indexed}, got {entry.get('schema_indexed')}")

    summary = as_dict(report.get("summary"), pathlib.Path("summary"))
    expected_summary = {
        "schema_backed_reports": schema_backed,
        "schema_indexed_reports": schema_indexed,
        "schema_missing_reports": schema_backed - schema_indexed,
    }
    for key, value in expected_summary.items():
        if summary.get(key) != value:
            failures.append(f"summary.{key} expected {value}, got {summary.get(key)}")

    if failures:
        raise ValueError("; ".join(failures))


def validate_report(
    report_path: pathlib.Path,
    schema_path: pathlib.Path,
    generator: pathlib.Path,
    schema_index_path: pathlib.Path,
) -> None:
    report = as_dict(load_json(report_path), report_path)
    if report.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version expected {EXPECTED_SCHEMA_VERSION}, got {report.get('schema_version')}"
        )

    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
    if errors:
        messages = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            messages.append(f"{location}: {error.message}")
        raise ValueError("; ".join(messages))

    validate_report_digests(report)
    validate_source_profile_inputs(report)
    validate_schema_index_input(report, schema_index_path)
    validate_schema_index_coverage(report, schema_index_path)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_report = pathlib.Path(temp_dir) / "source-report-inventory.json"
        subprocess.run(
            [
                sys.executable,
                str(generator),
                "--output",
                str(temp_report),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        expected = as_dict(load_json(temp_report), temp_report)

    if normalize_json(report) != normalize_json(expected):
        raise ValueError("report is stale; regenerate with scripts/generate-source-report-inventory.py")

    summary = as_dict(report.get("summary"), report_path)
    sources = report.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty array")
    if summary.get("sources") != len(sources):
        raise ValueError("summary.sources does not match sources array length")

    recommended = report.get("recommended_reports")
    if not isinstance(recommended, list) or not recommended:
        raise ValueError("recommended_reports must be a non-empty array")
    expected_slots = len(sources) * len(recommended)
    if summary.get("recommended_report_slots") != expected_slots:
        raise ValueError(
            f"summary.recommended_report_slots expected {expected_slots}, got {summary.get('recommended_report_slots')}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.source-report-inventory.v1.schema.json",
        type=pathlib.Path,
        help="source report inventory JSON Schema path",
    )
    parser.add_argument(
        "--generator",
        default="scripts/generate-source-report-inventory.py",
        type=pathlib.Path,
        help="source report inventory generator path",
    )
    parser.add_argument(
        "--schema-index",
        default=DEFAULT_SCHEMA_INDEX,
        type=pathlib.Path,
        help="schema index path used to verify source report schema coverage",
    )
    parser.add_argument(
        "report",
        nargs="?",
        default=DEFAULT_REPORT,
        type=pathlib.Path,
        help="source report inventory to validate",
    )
    args = parser.parse_args()

    try:
        validate_report(args.report, args.schema, args.generator, args.schema_index)
    except Exception as exc:  # noqa: BLE001 - report all validation blockers
        print(f"FAIL {args.report}: {exc}", file=sys.stderr)
        return 1

    report = as_dict(load_json(args.report), args.report)
    summary = as_dict(report.get("summary"), args.report)
    print(
        f"ok {args.report} "
        f"(sources={summary.get('sources')}, reports={summary.get('report_total')}, "
        f"coverage={summary.get('source_report_coverage_percent')}%, "
        f"schema_indexed={summary.get('schema_indexed_reports')}, "
        f"schema_missing={summary.get('schema_missing_reports')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
