#!/usr/bin/env python3
"""Validate datapan-registry release-health rollup artifacts."""

from __future__ import annotations

import argparse
import pathlib
import sys

from release_health_rollup import as_dict, load_json, validate_rollup_consistency

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running release-health rollup validation"
    ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.release-health-rollup.v1.schema.json",
        type=pathlib.Path,
        help="release-health rollup JSON Schema path",
    )
    parser.add_argument("rollups", nargs="+", type=pathlib.Path)
    args = parser.parse_args()

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for rollup_path in args.rollups:
        try:
            instance = as_dict(load_json(rollup_path), rollup_path.as_posix())
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
            if not errors:
                validate_rollup_consistency(instance)
        except Exception as exc:  # noqa: BLE001 - report all validation blockers
            print(f"FAIL {rollup_path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if errors:
            failures += 1
            print(f"FAIL {rollup_path}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            continue

        summary = as_dict(instance.get("summary"), f"{rollup_path}.summary")
        print(
            f"ok {rollup_path} "
            f"(checks={summary.get('checks_passed')}/{summary.get('checks_total')}, "
            f"scopes={','.join(summary.get('scopes', []))})"
        )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
