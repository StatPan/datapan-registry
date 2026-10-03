#!/usr/bin/env python3
"""Refresh the data.go.kr aggregate denominator from Datapan CLI coverage output."""

from __future__ import annotations

import argparse
import json
import pathlib
import tempfile
import sys
from typing import Any

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_COVERAGE = pathlib.Path("reports/coverage.json")
DEFAULT_PROFILE = pathlib.Path("sources/data_go_kr.json")
DEFAULT_OUTPUT = pathlib.Path("reports/data-go-kr/operation-denominator.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.operation-denominator.v1.schema.json")


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build(
    coverage: dict[str, Any],
    profile: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    if coverage.get("provider") != profile.get("provider") or profile.get("source_id") != "data_go_kr":
        raise ValueError("coverage and source profile do not identify data.go.kr")
    summary = coverage.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("native CLI coverage summary is missing")
    operations = summary.get("operations")
    callable_operations = summary.get("callable_operations")
    if (
        isinstance(operations, bool) or not isinstance(operations, int) or operations < 1
        or isinstance(callable_operations, bool) or not isinstance(callable_operations, int)
        or not 0 <= callable_operations <= operations
    ):
        raise ValueError("native CLI coverage denominator is invalid")
    generated_at = coverage.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("native CLI coverage timestamp is missing")
    # Static scope, identity, and maintenance semantics are review-owned policy
    # already recorded in the current denominator. Only measured inputs come
    # from the native CLI report and the source profile.
    result = dict(current)
    result["generated_at"] = generated_at
    result["source_id"] = profile["source_id"]
    result["provider"] = profile["provider"]
    provenance = dict(current.get("provenance", {}))
    provenance["source_profile"] = DEFAULT_PROFILE.as_posix()
    provenance["catalog_artifact"] = DEFAULT_COVERAGE.as_posix()
    provenance["reviewed_at"] = profile.get("references", {}).get("last_reviewed_at")
    result["provenance"] = provenance
    result["summary"] = {"operations": operations, "callable_operations": callable_operations}
    result["operations"] = []
    return result


def render(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=pathlib.Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--profile", type=pathlib.Path, default=DEFAULT_PROFILE)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--schema", type=pathlib.Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        coverage = load(args.coverage)
        profile = load(args.profile)
        current = load(args.output)
        schema = load(args.schema)
        if not all(isinstance(value, dict) for value in (coverage, profile, current, schema)):
            raise ValueError("coverage, profile, current denominator, and schema must be objects")
        report = build(coverage, profile, current)
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(report)
        actual = render(report)
        if args.check:
            if not args.output.is_file() or args.output.read_bytes() != actual:
                raise ValueError(f"{args.output} is stale")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=args.output.parent, prefix=f".{args.output.name}.", delete=False) as handle:
                temporary = pathlib.Path(handle.name)
                handle.write(actual)
            try:
                temporary.replace(args.output)
            finally:
                temporary.unlink(missing_ok=True)
        print(json.dumps({"status": "checked" if args.check else "wrote", "output": args.output.as_posix(), **report["summary"]}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL data.go.kr operation denominator: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
