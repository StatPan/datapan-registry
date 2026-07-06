#!/usr/bin/env python3
"""Validate datapan-registry doctor smoke summary artifacts."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running doctor smoke summary validation"
    ) from exc


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, path: pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def validate_consistency(path: pathlib.Path, summary: dict[str, Any]) -> None:
    registry = as_dict(summary.get("registry"), path)
    cross_check = as_dict(summary.get("install_cross_check"), path)

    performed = cross_check.get("performed")
    check_fields = ["registry", "specs"]
    present_check_fields = [field for field in check_fields if field in cross_check]
    if performed:
        missing = sorted(set(check_fields).difference(cross_check))
        if missing:
            raise ValueError(f"performed cross-check summaries must include fields: {', '.join(missing)}")
        if not isinstance(summary.get("install_json"), str) or not summary["install_json"]:
            raise ValueError("install_json is required when install cross-check is performed")
        if cross_check.get("registry") != registry.get("path"):
            raise ValueError("install_cross_check.registry must match registry.path")
        if cross_check.get("specs") != registry.get("specs"):
            raise ValueError("install_cross_check.specs must match registry.specs")
    else:
        if summary.get("install_json") is not None:
            raise ValueError("install_json must be null when install cross-check is not performed")
        if present_check_fields:
            raise ValueError(
                "non-cross-check summaries must not include fields: "
                + ", ".join(sorted(present_check_fields))
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.doctor-smoke-summary.v1.schema.json",
        type=pathlib.Path,
        help="doctor smoke summary JSON Schema path",
    )
    parser.add_argument("summaries", nargs="+", type=pathlib.Path)
    args = parser.parse_args()

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for summary_path in args.summaries:
        try:
            instance = as_dict(load_json(summary_path), summary_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
            if not errors:
                validate_consistency(summary_path, instance)
        except Exception as exc:  # noqa: BLE001 - report all validation blockers
            print(f"FAIL {summary_path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if errors:
            failures += 1
            print(f"FAIL {summary_path}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            continue

        registry = as_dict(instance.get("registry"), summary_path)
        cross_check = as_dict(instance.get("install_cross_check"), summary_path)
        print(
            f"ok {summary_path} "
            f"(specs={registry.get('specs')}, operations={registry.get('operations')}, "
            f"cross_check_performed={cross_check.get('performed')})"
        )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
