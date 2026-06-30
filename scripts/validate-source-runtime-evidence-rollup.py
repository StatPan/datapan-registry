#!/usr/bin/env python3
"""Validate the release-wide source runtime evidence rollup."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running source runtime evidence rollup validation"
    ) from exc


DEFAULT_ROLLUP = pathlib.Path("reports/source-runtime-evidence-rollup.json")


def portable_path(path: pathlib.Path) -> str:
    return path.as_posix()


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, path: pathlib.Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def key_counts(counter: dict[str, list[str]]) -> list[dict[str, object]]:
    return [
        {"id": key, "count": len(counter[key]), "source_ids": sorted(counter[key])}
        for key in sorted(counter)
    ]


def validate_rollup(path: pathlib.Path, rollup: dict[str, object]) -> None:
    source_plan_inputs = rollup.get("source_plan_inputs")
    if not isinstance(source_plan_inputs, list) or not source_plan_inputs:
        raise ValueError("source_plan_inputs must be a non-empty array")

    plan_paths = [pathlib.Path(str(item)) for item in source_plan_inputs]
    expected_inputs = [
        portable_path(item)
        for item in sorted(pathlib.Path("reports").glob("*/runtime-evidence-plan.json"))
    ]
    if [portable_path(item) for item in plan_paths] != expected_inputs:
        raise ValueError("source_plan_inputs must list all checked-in source runtime evidence plans")

    plans = [as_dict(load_json(plan_path), plan_path) for plan_path in plan_paths]
    plans_by_source = {str(plan.get("source_id")): plan for plan in plans}
    if len(plans_by_source) != len(plans):
        raise ValueError("duplicate source_id values in source runtime evidence plans")

    expected_sources: list[dict[str, object]] = []
    blockers_by_id: dict[str, list[str]] = collections.defaultdict(list)
    warnings_by_id: dict[str, list[str]] = collections.defaultdict(list)
    summary = {
        "sources": len(plans),
        "sources_without_evidence": 0,
        "evidence_total": 0,
        "verified": 0,
        "failed": 0,
        "skipped": 0,
        "unknown": 0,
        "blocking_count": 0,
        "warning_count": 0,
    }

    for plan_path, plan in zip(plan_paths, plans):
        source_id = str(plan.get("source_id"))
        runtime_state = as_dict(plan.get("runtime_state"), plan_path)
        plan_summary = as_dict(plan.get("summary"), plan_path)
        blockers = plan.get("blockers")
        warnings = plan.get("warnings")
        if not isinstance(blockers, list):
            raise ValueError(f"{plan_path}: blockers must be an array")
        if not isinstance(warnings, list):
            raise ValueError(f"{plan_path}: warnings must be an array")

        blocker_ids = sorted(
            str(blocker.get("blocker_id"))
            for blocker in blockers
            if isinstance(blocker, dict)
        )
        warning_ids = sorted(
            str(warning.get("warning_id"))
            for warning in warnings
            if isinstance(warning, dict)
        )
        for blocker_id in blocker_ids:
            blockers_by_id[blocker_id].append(source_id)
        for warning_id in warning_ids:
            warnings_by_id[warning_id].append(source_id)

        evidence_total = int(plan_summary.get("evidence_total", 0))
        if evidence_total == 0:
            summary["sources_without_evidence"] += 1
        summary["evidence_total"] += evidence_total
        for key in ["verified", "failed", "skipped", "unknown"]:
            summary[key] += int(runtime_state.get(key, 0))
        summary["blocking_count"] += int(plan_summary.get("blocking_count", 0))
        summary["warning_count"] += int(plan_summary.get("warning_count", 0))

        expected_sources.append(
            {
                "source_id": source_id,
                "provider": plan.get("provider"),
                "runtime_evidence_plan": portable_path(plan_path),
                "evidence_total": evidence_total,
                "blocking_count": plan_summary.get("blocking_count"),
                "warning_count": plan_summary.get("warning_count"),
                "blocker_ids": blocker_ids,
                "warning_ids": warning_ids,
                "next_action_count": plan_summary.get("next_action_count"),
            }
        )

    expected_sources.sort(key=lambda item: str(item["source_id"]))
    if rollup.get("summary") != summary:
        raise ValueError(f"summary expected {summary}, got {rollup.get('summary')}")
    if rollup.get("sources") != expected_sources:
        raise ValueError("sources do not match source runtime evidence plans")
    if rollup.get("blockers_by_id") != key_counts(blockers_by_id):
        raise ValueError("blockers_by_id does not match source runtime evidence plans")
    if rollup.get("warnings_by_id") != key_counts(warnings_by_id):
        raise ValueError("warnings_by_id does not match source runtime evidence plans")

    rollup_warnings = rollup.get("warnings")
    if not isinstance(rollup_warnings, list):
        raise ValueError("warnings must be an array")
    warning_sources = {
        str(warning.get("warning_id")): sorted(warning.get("affected_sources") or [])
        for warning in rollup_warnings
        if isinstance(warning, dict)
    }
    missing_warning_ids = sorted(set(warnings_by_id).difference(warning_sources))
    if missing_warning_ids:
        raise ValueError(f"missing rollup warnings: {', '.join(missing_warning_ids)}")
    for warning_id, source_ids in warnings_by_id.items():
        if warning_sources.get(warning_id) != sorted(source_ids):
            raise ValueError(f"warnings.{warning_id}.affected_sources does not match plans")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.source-runtime-evidence-rollup.v1.schema.json",
        type=pathlib.Path,
        help="source runtime evidence rollup JSON Schema path",
    )
    parser.add_argument(
        "rollup",
        nargs="?",
        default=DEFAULT_ROLLUP,
        type=pathlib.Path,
        help="rollup file to validate",
    )
    args = parser.parse_args()

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    try:
        instance = as_dict(load_json(args.rollup), args.rollup)
        errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
        if not errors:
            validate_rollup(args.rollup, instance)
    except Exception as exc:  # noqa: BLE001 - report all validation blockers
        print(f"FAIL {args.rollup}: {exc}", file=sys.stderr)
        return 1

    if errors:
        failures += 1
        print(f"FAIL {args.rollup}", file=sys.stderr)
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            print(f"  {location}: {error.message}", file=sys.stderr)

    if failures:
        return 1

    summary = instance.get("summary", {})
    print(
        "ok "
        f"{args.rollup} "
        f"(sources={summary.get('sources')}, evidence={summary.get('evidence_total')}, "
        f"warnings={summary.get('warning_count')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
