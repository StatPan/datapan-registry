#!/usr/bin/env python3
"""Validate checked-in Datapan source profiles."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running source profile validation"
    ) from exc


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.source-profile.v1.schema.json",
        type=pathlib.Path,
        help="source profile JSON Schema path",
    )
    parser.add_argument(
        "profiles",
        nargs="*",
        type=pathlib.Path,
        help="profile files to validate; defaults to sources/*.json",
    )
    args = parser.parse_args()

    profiles = args.profiles or sorted(pathlib.Path("sources").glob("*.json"))
    if not profiles:
        print("no source profiles found", file=sys.stderr)
        return 1

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for profile_path in profiles:
        try:
            instance = load_json(profile_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
        except Exception as exc:  # noqa: BLE001 - report all validation blockers
            print(f"FAIL {profile_path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if errors:
            failures += 1
            print(f"FAIL {profile_path}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            continue

        source_id = instance.get("source_id", "<unknown>") if isinstance(instance, dict) else "<unknown>"
        print(f"ok {profile_path} ({source_id})")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
