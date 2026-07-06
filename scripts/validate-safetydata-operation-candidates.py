#!/usr/bin/env python3
"""Validate checked-in Safety Data operation candidate evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before validating Safety Data operation candidates") from exc


EXPECTED_SCHEMA_VERSION = "datapan.safetydata-operation-candidates.v1"
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.safetydata-operation-candidates.v1.schema.json")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: Any, label: str | pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def validate_schema(report: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
    if errors:
        messages = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            messages.append(f"{location}: {error.message}")
        raise ValueError("; ".join(messages))


def validate_report(report_path: pathlib.Path, markdown_path: pathlib.Path, schema_path: pathlib.Path) -> None:
    report = as_dict(load_json(report_path), report_path)
    if report.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version expected {EXPECTED_SCHEMA_VERSION}, got {report.get('schema_version')}"
        )
    validate_schema(report, schema_path)
    if report.get("discovery_source") != "safetydata.go.kr":
        raise ValueError("discovery_source must be safetydata.go.kr")

    generation_inputs = as_dict(report.get("generation_inputs"), "generation_inputs")
    batch_path = generation_inputs.get("batch")
    if not isinstance(batch_path, str) or not batch_path:
        raise ValueError("generation_inputs.batch must be a non-empty path")
    if not pathlib.Path(batch_path).exists():
        raise ValueError(f"generation_inputs.batch does not exist: {batch_path}")

    summary = as_dict(report.get("summary"), "summary")
    results = as_list(report.get("results"), "results")
    counts = {"candidate": 0, "skipped": 0, "failed": 0}
    for index, raw in enumerate(results):
        row = as_dict(raw, f"results[{index}]")
        status = row.get("status")
        if status not in counts:
            raise ValueError(f"results[{index}].status is invalid: {status}")
        counts[status] += 1
        if not row.get("dataset_id"):
            raise ValueError(f"results[{index}].dataset_id must be non-empty")
        if status == "candidate":
            endpoint = row.get("endpoint")
            params = row.get("request_params")
            mapping = as_dict(row.get("operation_mapping_candidate"), f"results[{index}].operation_mapping_candidate")
            if not isinstance(endpoint, str) or not endpoint.startswith("https://www.safetydata.go.kr/V2/api/"):
                raise ValueError(f"results[{index}].endpoint must be a Safety Data API endpoint")
            if not isinstance(params, list) or not params:
                raise ValueError(f"results[{index}].request_params must be a non-empty array")
            if mapping.get("endpoint") != endpoint:
                raise ValueError(f"results[{index}].operation_mapping_candidate.endpoint mismatch")

    if summary.get("input_apis") != len(results):
        raise ValueError("summary.input_apis must equal results length")
    summary_keys = {"candidate": "candidates", "skipped": "skipped", "failed": "failed"}
    for key, expected in counts.items():
        summary_key = summary_keys[key]
        if summary.get(summary_key) != expected:
            raise ValueError(f"summary.{summary_key} expected {expected}, got {summary.get(summary_key)}")
    if counts["candidate"] <= 0:
        raise ValueError("at least one candidate is required")
    if not markdown_path.exists():
        raise ValueError(f"{markdown_path} is missing")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report",
        nargs="?",
        default=pathlib.Path("reports/data-go-kr/safetydata-operation-candidates.json"),
        type=pathlib.Path,
    )
    parser.add_argument("--markdown", default="docs/data-go-kr-safetydata-operation-candidates.md", type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    args = parser.parse_args()

    try:
        validate_report(args.report, args.markdown, args.schema)
    except Exception as exc:  # noqa: BLE001 - report all validation blockers
        print(f"FAIL {args.report}: {exc}")
        return 1

    report = as_dict(load_json(args.report), args.report)
    summary = as_dict(report.get("summary"), "summary")
    print(
        f"ok {args.report} "
        f"(candidates={summary.get('candidates')}, skipped={summary.get('skipped')}, failed={summary.get('failed')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
