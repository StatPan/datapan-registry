#!/usr/bin/env python3
"""Validate checked-in Datapan error action catalogs."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running error action catalog validation"
    ) from exc


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def default_catalogs() -> list[pathlib.Path]:
    return sorted(pathlib.Path("reports").glob("*/error-action-catalog.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.error-action-catalog.v1.schema.json",
        type=pathlib.Path,
        help="error action catalog JSON Schema path",
    )
    parser.add_argument(
        "catalogs",
        nargs="*",
        type=pathlib.Path,
        help="catalog files to validate; defaults to reports/*/error-action-catalog.json",
    )
    args = parser.parse_args()

    catalogs = args.catalogs or default_catalogs()
    if not catalogs:
        print("no error action catalogs found", file=sys.stderr)
        return 1

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for catalog_path in catalogs:
        try:
            instance = load_json(catalog_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
        except Exception as exc:  # noqa: BLE001 - report all validation blockers
            print(f"FAIL {catalog_path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if errors:
            failures += 1
            print(f"FAIL {catalog_path}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            continue

        source_id = instance.get("source_id", "<unknown>") if isinstance(instance, dict) else "<unknown>"
        rules = instance.get("summary", {}).get("rules", "<unknown>") if isinstance(instance, dict) else "<unknown>"
        print(f"ok {catalog_path} ({source_id}, rules={rules})")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
