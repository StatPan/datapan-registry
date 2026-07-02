#!/usr/bin/env python3
"""Validate the generated data.go.kr operation materialization plan."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
from typing import Any


EXPECTED_SCHEMA_VERSION = "datapan.operation-materialization-plan.v1"


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: Any, path: pathlib.Path | str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def normalize_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def validate_report(report_path: pathlib.Path, markdown_path: pathlib.Path, generator: pathlib.Path) -> None:
    report = as_dict(load_json(report_path), report_path)
    if report.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version expected {EXPECTED_SCHEMA_VERSION}, got {report.get('schema_version')}"
        )

    generation_inputs = as_dict(report.get("generation_inputs"), report_path)
    coverage_backlog = generation_inputs.get("coverage_backlog")
    if not isinstance(coverage_backlog, str) or not coverage_backlog:
        raise ValueError("generation_inputs.coverage_backlog must be a non-empty path")
    if not pathlib.Path(coverage_backlog).exists():
        raise ValueError(f"generation_inputs.coverage_backlog does not exist: {coverage_backlog}")

    policy = as_dict(report.get("policy"), report_path)
    institution_limit = int(policy.get("institution_limit") or 0)
    batch_size = int(policy.get("batch_size") or 0)
    sample_limit = int(policy.get("sample_limit") or 0)
    if institution_limit <= 0:
        raise ValueError("policy.institution_limit must be positive")
    if batch_size <= 0:
        raise ValueError("policy.batch_size must be positive")
    if sample_limit <= 0:
        raise ValueError("policy.sample_limit must be positive")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_json = pathlib.Path(temp_dir) / "operation-materialization-plan.json"
        temp_md = pathlib.Path(temp_dir) / "operation-materialization-plan.md"
        subprocess.run(
            [
                sys.executable,
                str(generator),
                "--coverage-backlog",
                coverage_backlog,
                "--institution-limit",
                str(institution_limit),
                "--batch-size",
                str(batch_size),
                "--sample-limit",
                str(sample_limit),
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
        raise ValueError("report is stale; regenerate with scripts/generate-operation-materialization-plan.py")
    if markdown_path.exists():
        actual_markdown = markdown_path.read_text(encoding="utf-8")
        if actual_markdown != expected_markdown:
            raise ValueError(
                f"{markdown_path} is stale; regenerate with scripts/generate-operation-materialization-plan.py"
            )

    summary = as_dict(report.get("summary"), report_path)
    batches = as_list(report.get("batches"), "batches")
    if summary.get("planned_institutions") != len(batches):
        raise ValueError("summary.planned_institutions does not match batches length")
    if not batches:
        raise ValueError("batches must contain at least one institution")
    if batches[0].get("organization") != summary.get("first_queue"):
        raise ValueError("summary.first_queue must match the first batch organization")

    planned_sum = 0
    for index, raw in enumerate(batches):
        batch = as_dict(raw, f"batches[{index}]")
        organization = batch.get("organization")
        apis = as_list(batch.get("apis"), f"batches[{index}].apis")
        sample_apis = as_list(batch.get("sample_apis"), f"batches[{index}].sample_apis")
        planned_api_count = batch.get("planned_api_count")
        if not isinstance(organization, str) or not organization:
            raise ValueError(f"batches[{index}].organization must be non-empty")
        if batch.get("action") != "materialize_operation_mapping":
            raise ValueError(f"batches[{index}].action must be materialize_operation_mapping")
        if not isinstance(planned_api_count, int) or planned_api_count <= 0:
            raise ValueError(f"batches[{index}].planned_api_count must be positive")
        if planned_api_count != len(apis):
            raise ValueError(f"batches[{index}].planned_api_count must match apis length")
        if planned_api_count > batch_size:
            raise ValueError(f"batches[{index}].planned_api_count exceeds policy.batch_size")
        if len(sample_apis) > sample_limit:
            raise ValueError(f"batches[{index}].sample_apis exceeds policy.sample_limit")
        for api_index, api_raw in enumerate(apis):
            api = as_dict(api_raw, f"batches[{index}].apis[{api_index}]")
            if api.get("action") != "materialize_operation_mapping":
                raise ValueError(f"batches[{index}].apis[{api_index}].action must be materialize_operation_mapping")
            if api.get("organization") != organization:
                raise ValueError(f"batches[{index}].apis[{api_index}] organization mismatch")
            if not api.get("dataset_id"):
                raise ValueError(f"batches[{index}].apis[{api_index}].dataset_id must be non-empty")
        planned_sum += planned_api_count

    if summary.get("planned_api_count") != planned_sum:
        raise ValueError(f"summary.planned_api_count expected {planned_sum}, got {summary.get('planned_api_count')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generator", default="scripts/generate-operation-materialization-plan.py", type=pathlib.Path)
    parser.add_argument("--markdown", default="docs/data-go-kr-operation-materialization-plan.md", type=pathlib.Path)
    parser.add_argument(
        "report",
        nargs="?",
        default=pathlib.Path("reports/data-go-kr/operation-materialization-plan.json"),
        type=pathlib.Path,
    )
    args = parser.parse_args()

    try:
        validate_report(args.report, args.markdown, args.generator)
    except Exception as exc:  # noqa: BLE001 - report all validation blockers
        print(f"FAIL {args.report}: {exc}", file=sys.stderr)
        return 1

    report = as_dict(load_json(args.report), args.report)
    summary = as_dict(report.get("summary"), args.report)
    print(
        f"ok {args.report} "
        f"(first_queue={summary.get('first_queue')}, planned_apis={summary.get('planned_api_count')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
