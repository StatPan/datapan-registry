#!/usr/bin/env python3
"""Apply checked Safety Data operation patches to the data.go.kr registry."""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


EXPECTED_SCHEMA_VERSION = "datapan.safetydata-registry-patches.v1"


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def as_dict(value: Any, label: str | pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: Any, label: str | pathlib.Path) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def patch_dataset_id(patch: dict[str, Any], index: int) -> str:
    dataset_id = str(patch.get("dataset_id") or "")
    if not dataset_id:
        raise ValueError(f"patches[{index}].dataset_id must be non-empty")
    return dataset_id


def apply_patches(registry: list[Any], patch_report: dict[str, Any]) -> dict[str, Any]:
    if patch_report.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(
            f"patch report schema_version expected {EXPECTED_SCHEMA_VERSION}, "
            f"got {patch_report.get('schema_version')}"
        )

    rows_by_id = {
        str(row.get("id") or ""): row
        for row in registry
        if isinstance(row, dict) and row.get("id")
    }
    applied: list[dict[str, Any]] = []
    already_applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for index, raw_patch in enumerate(as_list(patch_report.get("patches"), "patches")):
        patch = as_dict(raw_patch, f"patches[{index}]")
        dataset_id = patch_dataset_id(patch, index)
        row = rows_by_id.get(dataset_id)
        if not row:
            skipped.append({"dataset_id": dataset_id, "reason": "missing_registry_row"})
            continue
        operations = as_list(patch.get("operations"), f"patches[{index}].operations")
        if not operations:
            skipped.append({"dataset_id": dataset_id, "reason": "empty_operations"})
            continue
        if row.get("operations") == operations:
            already_applied.append(
                {
                    "dataset_id": dataset_id,
                    "title": row.get("title"),
                    "organization": row.get("organization"),
                    "operation_count": len(operations),
                }
            )
            continue
        if row.get("operations"):
            skipped.append({"dataset_id": dataset_id, "reason": "already_has_operations"})
            continue
        row["operations"] = operations
        applied.append(
            {
                "dataset_id": dataset_id,
                "title": row.get("title"),
                "organization": row.get("organization"),
                "operation_count": len(operations),
            }
        )

    return {
        "patches": len(as_list(patch_report.get("patches"), "patches")),
        "applied": len(applied),
        "already_applied": len(already_applied),
        "operations_added": sum(int(row["operation_count"]) for row in applied),
        "skipped": len(skipped),
        "applied_rows": applied,
        "already_applied_rows": already_applied,
        "skipped_rows": skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="data/data-go-kr.registry.json", type=pathlib.Path)
    parser.add_argument("--patches", default="reports/data-go-kr/safetydata-registry-patches.json", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--apply", action="store_true", help="Write the patched registry. Without this flag, only report the impact.")
    args = parser.parse_args()

    registry = as_list(load_json(args.registry), args.registry)
    patch_report = as_dict(load_json(args.patches), args.patches)
    summary = apply_patches(registry, patch_report)

    output = args.output or args.registry
    if args.apply:
        write_json(output, registry)
        print(
            f"applied {summary['applied']} Safety Data patches "
            f"({summary['operations_added']} operations, "
            f"{summary['already_applied']} already applied) to {output}"
        )
    else:
        print(
            f"dry-run: {summary['applied']} Safety Data patches would add "
            f"{summary['operations_added']} operations to {output}; "
            f"{summary['already_applied']} already applied; use --apply to write"
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
