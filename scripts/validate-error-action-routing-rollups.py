#!/usr/bin/env python3
"""Validate Datapan error/action routing rollup reports."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before validating error-action rollups") from exc

from error_action_routing_rollup import build_rollup, validate_rollup_consistency


DEFAULT_REPORT = pathlib.Path("reports/error-action-routing-rollup.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.error-action-routing-rollup.v1.schema.json")


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, label: str | pathlib.Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", default=DEFAULT_REPORT, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--manifest", default="manifest.json", type=pathlib.Path)
    parser.add_argument("--catalog-glob", default="reports/*/error-action-catalog.json")
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        report = as_dict(load_json(args.report), args.report)
        expected = build_rollup(manifest_path=args.manifest, catalog_glob=args.catalog_glob)

        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
        if errors:
            print(f"FAIL {args.report}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            return 1

        validate_rollup_consistency(report, expected)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL {args.report}: {exc}", file=sys.stderr)
        return 1

    summary = as_dict(report.get("summary"), "summary")
    print(f"ok {args.report} (catalogs={summary.get('catalogs')}, rules={summary.get('rules')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
