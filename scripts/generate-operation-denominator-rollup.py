#!/usr/bin/env python3
"""Validate supported-source operation denominators and generate their rollup."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import jsonschema


POLICY = pathlib.Path("policy/sustainable-coverage.json")
DENOMINATOR_SCHEMA = pathlib.Path("schemas/datapan.operation-denominator.v1.schema.json")
ROLLUP_SCHEMA = pathlib.Path("schemas/datapan.operation-denominator-rollup.v1.schema.json")
OUTPUT = pathlib.Path("reports/operation-denominator-rollup.json")


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def validate(value: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    errors = sorted(jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).iter_errors(value), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"{label}: " + "; ".join(f"{'.'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors))


def build(policy: dict[str, Any]) -> dict[str, Any]:
    expected = policy.get("supported_sources")
    if not isinstance(expected, list) or not expected:
        raise ValueError("policy.supported_sources must be a non-empty array")
    schema = load(DENOMINATOR_SCHEMA)
    rows: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for configured in expected:
        if not isinstance(configured, dict):
            raise ValueError("supported source must be an object")
        source_id = str(configured.get("source_id", ""))
        path = pathlib.Path(str(configured.get("coverage_report", "")))
        denominator = load(path)
        validate(denominator, schema, path.as_posix())
        if denominator.get("source_id") != source_id:
            raise ValueError(f"{path}: source_id does not match policy")
        if source_id in seen_sources:
            raise ValueError(f"duplicate source denominator: {source_id}")
        seen_sources.add(source_id)
        summary = denominator["summary"]
        operations = denominator["operations"]
        if summary["callable_operations"] > summary["operations"]:
            raise ValueError(f"{path}: callable operations exceed denominator")
        if denominator["scope"]["kind"] == "enumerated_supported_operations":
            identities = [row["operation_id"] for row in operations]
            if len(identities) != len(set(identities)):
                raise ValueError(f"{path}: duplicate operation identity")
            if len(operations) != summary["operations"]:
                raise ValueError(f"{path}: enumerated operations do not match summary")
            if sum(row["callable"] is True for row in operations) != summary["callable_operations"]:
                raise ValueError(f"{path}: callable operation count does not match summary")
        elif operations:
            raise ValueError(f"{path}: aggregate denominator must not partially enumerate operations")
        rows.append({"source_id": source_id, "path": path.as_posix(), "scope_kind": denominator["scope"]["kind"], "operations": summary["operations"], "callable_operations": summary["callable_operations"]})
    generated_at = load(pathlib.Path("manifest.json"))["generated_at"]
    report = {"schema_version": "datapan.operation-denominator-rollup.v1", "generated_at": generated_at, "summary": {"sources": len(rows), "operations": sum(row["operations"] for row in rows), "callable_operations": sum(row["callable_operations"] for row in rows)}, "sources": rows}
    validate(report, load(ROLLUP_SCHEMA), OUTPUT.as_posix())
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        report = build(load(POLICY))
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
                raise ValueError(f"{OUTPUT} is stale")
            print(f"ok {OUTPUT} (sources={report['summary']['sources']}, operations={report['summary']['operations']})")
        else:
            OUTPUT.write_text(rendered, encoding="utf-8")
            print(f"wrote {OUTPUT} (sources={report['summary']['sources']}, operations={report['summary']['operations']})")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL operation denominator rollup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
