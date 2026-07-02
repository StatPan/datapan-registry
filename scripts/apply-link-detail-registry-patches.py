#!/usr/bin/env python3
"""Apply checked link-detail operation patches to the data.go.kr registry."""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


EXPECTED_SCHEMA_VERSION = "datapan.link-detail-registry-patches.v1"


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="data/data-go-kr.registry.json", type=pathlib.Path)
    parser.add_argument("--patches", default="reports/data-go-kr/link-detail-registry-patches.json", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--apply", action="store_true", help="Write the patched registry. Without this flag, only report impact.")
    args = parser.parse_args()

    registry = load_json(args.registry)
    report = load_json(args.patches)
    if report.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(f"patch report schema_version expected {EXPECTED_SCHEMA_VERSION}")
    rows_by_id = {str(row.get("id") or ""): row for row in registry if isinstance(row, dict)}
    applied = 0
    already_applied = 0
    skipped: list[dict[str, str]] = []
    operations_added = 0
    for patch in report.get("patches") or []:
        dataset_id = str(patch.get("dataset_id") or "")
        row = rows_by_id.get(dataset_id)
        if not row:
            skipped.append({"dataset_id": dataset_id, "reason": "missing_registry_row"})
            continue
        operations = patch.get("operations") or []
        if row.get("operations") == operations:
            already_applied += 1
            continue
        if row.get("operations"):
            skipped.append({"dataset_id": dataset_id, "reason": "already_has_operations"})
            continue
        row["operations"] = operations
        applied += 1
        operations_added += len(operations)
    summary = {
        "patches": len(report.get("patches") or []),
        "applied": applied,
        "already_applied": already_applied,
        "operations_added": operations_added,
        "skipped": len(skipped),
        "skipped_rows": skipped,
    }
    if args.apply:
        write_json(args.output or args.registry, registry)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
