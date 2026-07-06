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


def validate_report(report_path: pathlib.Path, schema_path: pathlib.Path, generator: pathlib.Path) -> None:
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
        "report",
        nargs="?",
        default=DEFAULT_REPORT,
        type=pathlib.Path,
        help="source report inventory to validate",
    )
    args = parser.parse_args()

    try:
        validate_report(args.report, args.schema, args.generator)
    except Exception as exc:  # noqa: BLE001 - report all validation blockers
        print(f"FAIL {args.report}: {exc}", file=sys.stderr)
        return 1

    report = as_dict(load_json(args.report), args.report)
    summary = as_dict(report.get("summary"), args.report)
    print(
        f"ok {args.report} "
        f"(sources={summary.get('sources')}, reports={summary.get('report_total')}, "
        f"coverage={summary.get('source_report_coverage_percent')}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
