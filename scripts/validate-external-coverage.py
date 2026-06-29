#!/usr/bin/env python3
"""Validate checked-in Datapan external coverage reports."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running external coverage validation"
    ) from exc


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def default_reports() -> list[pathlib.Path]:
    return sorted(pathlib.Path("reports").glob("*/external-coverage-summary.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.external-coverage.v1.schema.json",
        type=pathlib.Path,
        help="external coverage JSON Schema path",
    )
    parser.add_argument(
        "reports",
        nargs="*",
        type=pathlib.Path,
        help="coverage reports to validate; defaults to reports/*/external-coverage-summary.json",
    )
    args = parser.parse_args()

    reports = args.reports or default_reports()
    if not reports:
        print("no external coverage reports found", file=sys.stderr)
        return 1

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for report_path in reports:
        try:
            instance = load_json(report_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
        except Exception as exc:  # noqa: BLE001 - report all validation blockers
            print(f"FAIL {report_path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if errors:
            failures += 1
            print(f"FAIL {report_path}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            continue

        source_id = instance.get("source_id", "<unknown>") if isinstance(instance, dict) else "<unknown>"
        candidates = (
            instance.get("summary", {}).get("evidence_adjusted_adapter_candidate_operations", "<unknown>")
            if isinstance(instance, dict)
            else "<unknown>"
        )
        print(f"ok {report_path} ({source_id}, evidence_adjusted_candidates={candidates})")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
