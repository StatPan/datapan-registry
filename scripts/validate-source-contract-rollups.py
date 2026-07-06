#!/usr/bin/env python3
"""Validate Datapan source contract rollup reports."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before validating source contract rollups") from exc

from source_contract_rollup import build_rollup, validate_rollup_consistency


DEFAULT_REPORT = pathlib.Path("reports/source-contract-rollup.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.source-contract-rollup.v1.schema.json")


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
    parser.add_argument("--source-glob", default="sources/*.json")
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        report = as_dict(load_json(args.report), args.report)
        expected = build_rollup(manifest_path=args.manifest, source_glob=args.source_glob)

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
    print(f"ok {args.report} (profiles={summary.get('profiles')}, providers={summary.get('providers')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
