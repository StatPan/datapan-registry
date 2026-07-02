#!/usr/bin/env python3
"""Validate Safety Data registry operation patch plans."""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


EXPECTED_SCHEMA_VERSION = "datapan.safetydata-registry-patches.v1"


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


def validate_report(report_path: pathlib.Path, markdown_path: pathlib.Path) -> None:
    report = as_dict(load_json(report_path), report_path)
    if report.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version expected {EXPECTED_SCHEMA_VERSION}, got {report.get('schema_version')}"
        )

    generation_inputs = as_dict(report.get("generation_inputs"), "generation_inputs")
    for key in ["registry", "safetydata_operation_candidates"]:
        raw = generation_inputs.get(key)
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"generation_inputs.{key} must be a non-empty path")
        if not pathlib.Path(raw).exists():
            raise ValueError(f"generation_inputs.{key} does not exist: {raw}")

    summary = as_dict(report.get("summary"), "summary")
    patches = as_list(report.get("patches"), "patches")
    skipped = as_list(report.get("skipped"), "skipped")
    if summary.get("patches") != len(patches):
        raise ValueError("summary.patches must equal patches length")
    if summary.get("skipped") != len(skipped):
        raise ValueError("summary.skipped must equal skipped length")
    if summary.get("operations_to_add") != sum(len(as_list(row.get("operations"), "operations")) for row in patches if isinstance(row, dict)):
        raise ValueError("summary.operations_to_add must equal operation payload count")
    if len(patches) <= 0:
        raise ValueError("at least one patch is required")

    saw_aed = False
    for index, raw in enumerate(patches):
        patch = as_dict(raw, f"patches[{index}]")
        if patch.get("action") != "add_operation_mapping":
            raise ValueError(f"patches[{index}].action must be add_operation_mapping")
        if patch.get("operation_count") != 1:
            raise ValueError(f"patches[{index}].operation_count must be 1")
        operations = as_list(patch.get("operations"), f"patches[{index}].operations")
        if len(operations) != 1:
            raise ValueError(f"patches[{index}].operations must contain exactly one operation")
        operation = as_dict(operations[0], f"patches[{index}].operations[0]")
        endpoint = operation.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://www.safetydata.go.kr/V2/api/"):
            raise ValueError(f"patches[{index}].operations[0].endpoint must be a Safety Data endpoint")
        request_params = as_list(operation.get("request_params"), f"patches[{index}].request_params")
        response_params = as_list(operation.get("response_params"), f"patches[{index}].response_params")
        if not any(isinstance(row, dict) and row.get("name") == "serviceKey" and row.get("required") is True for row in request_params):
            raise ValueError(f"patches[{index}] must include required serviceKey")
        if len(response_params) <= 0:
            raise ValueError(f"patches[{index}] must include response params")
        if patch.get("dataset_id") == "15147982":
            saw_aed = endpoint == "https://www.safetydata.go.kr/V2/api/DSSP-IF-00068"
    if not saw_aed:
        raise ValueError("AED dataset 15147982 patch must target DSSP-IF-00068")
    if not markdown_path.exists():
        raise ValueError(f"{markdown_path} is missing")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report",
        nargs="?",
        default=pathlib.Path("reports/data-go-kr/safetydata-registry-patches.json"),
        type=pathlib.Path,
    )
    parser.add_argument("--markdown", default="docs/data-go-kr-safetydata-registry-patches.md", type=pathlib.Path)
    args = parser.parse_args()

    try:
        validate_report(args.report, args.markdown)
    except Exception as exc:  # noqa: BLE001 - report all validation blockers
        print(f"FAIL {args.report}: {exc}")
        return 1

    report = as_dict(load_json(args.report), args.report)
    summary = as_dict(report.get("summary"), "summary")
    print(
        f"ok {args.report} "
        f"(patches={summary.get('patches')}, operations={summary.get('operations_to_add')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
