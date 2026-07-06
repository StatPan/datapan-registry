#!/usr/bin/env python3
"""Validate the generated data.go.kr institution runtime plan."""

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
    raise SystemExit("missing dependency: install jsonschema before validating institution runtime plan") from exc


EXPECTED_SCHEMA_VERSION = "datapan.institution-runtime-plan.v1"
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.institution-runtime-plan.v1.schema.json")


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

    generation_inputs = as_dict(report.get("generation_inputs"), report_path)
    required_inputs = ["coverage_backlog", "registry", "latest_verification"]
    for key in required_inputs:
        raw = generation_inputs.get(key)
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"generation_inputs.{key} must be a non-empty path")
        if not pathlib.Path(raw).exists():
            raise ValueError(f"generation_inputs.{key} does not exist: {raw}")

    policy = as_dict(report.get("policy"), report_path)
    batch_size = int(policy.get("batch_size") or 0)
    institution_limit = int(policy.get("institution_limit") or 0)
    timeout = str(policy.get("timeout") or "")
    if batch_size <= 0:
        raise ValueError("policy.batch_size must be positive")
    if institution_limit <= 0:
        raise ValueError("policy.institution_limit must be positive")
    if not timeout:
        raise ValueError("policy.timeout must be non-empty")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_json = pathlib.Path(temp_dir) / "institution-runtime-plan.json"
        temp_md = pathlib.Path(temp_dir) / "institution-runtime-plan.md"
        command = [
            sys.executable,
            str(generator),
            "--coverage-backlog",
            str(generation_inputs["coverage_backlog"]),
            "--registry",
            str(generation_inputs["registry"]),
            "--latest-verification",
            str(generation_inputs["latest_verification"]),
            "--batch-size",
            str(batch_size),
            "--institution-limit",
            str(institution_limit),
            "--timeout",
            timeout,
            "--output",
            str(temp_json),
            "--markdown-output",
            str(temp_md),
        ]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        expected = as_dict(load_json(temp_json), temp_json)
        expected_markdown = temp_md.read_text(encoding="utf-8")

    if normalize_json(report) != normalize_json(expected):
        raise ValueError("report is stale; regenerate with scripts/generate-institution-runtime-plan.py")
    if markdown_path.exists():
        actual_markdown = markdown_path.read_text(encoding="utf-8")
        if actual_markdown != expected_markdown:
            raise ValueError(
                f"{markdown_path} is stale; regenerate with scripts/generate-institution-runtime-plan.py"
            )

    batches = as_list(report.get("batches"), "batches")
    summary = as_dict(report.get("summary"), report_path)
    if summary.get("planned_institutions") != len(batches):
        raise ValueError("summary.planned_institutions does not match batches length")
    if not batches:
        raise ValueError("batches must contain at least one institution")

    planned_sum = 0
    for index, raw in enumerate(batches):
        batch = as_dict(raw, f"batches[{index}]")
        command = batch.get("command")
        organization = batch.get("organization")
        planned_operations = batch.get("planned_operations")
        if not isinstance(organization, str) or not organization:
            raise ValueError(f"batches[{index}].organization must be non-empty")
        if batch.get("kind") != "data_go_kr_gateway":
            raise ValueError(f"batches[{index}].kind must be data_go_kr_gateway")
        if not isinstance(planned_operations, int) or planned_operations <= 0:
            raise ValueError(f"batches[{index}].planned_operations must be positive")
        if planned_operations > batch_size:
            raise ValueError(f"batches[{index}].planned_operations exceeds policy.batch_size")
        if not isinstance(command, str) or "--org " not in command:
            raise ValueError(f"batches[{index}].command must include --org")
        if organization not in command:
            raise ValueError(f"batches[{index}].command must include its organization")
        if "--exclude-input " not in command:
            raise ValueError(f"batches[{index}].command must exclude latest verification evidence")
        planned_sum += planned_operations

    if summary.get("planned_operations") != planned_sum:
        raise ValueError(f"summary.planned_operations expected {planned_sum}, got {summary.get('planned_operations')}")
    if batches[0].get("organization") != summary.get("first_queue"):
        raise ValueError("summary.first_queue must match the first batch organization")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generator", default="scripts/generate-institution-runtime-plan.py", type=pathlib.Path)
    parser.add_argument("--markdown", default="docs/data-go-kr-institution-runtime-plan.md", type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument(
        "report",
        nargs="?",
        default=pathlib.Path("reports/data-go-kr/institution-runtime-plan.json"),
        type=pathlib.Path,
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
        f"(first_queue={summary.get('first_queue')}, planned_operations={summary.get('planned_operations')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
