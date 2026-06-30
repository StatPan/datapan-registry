#!/usr/bin/env python3
"""Validate checked-in Datapan registry impact plans."""

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
        "missing dependency: install jsonschema before running impact plan validation"
    ) from exc


CLIENT_SERVER_TARGETS = {"dataset-api", "sdk", "mcp"}


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, path: pathlib.Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def default_reports() -> list[pathlib.Path]:
    reports = sorted(pathlib.Path("reports").glob("*/registry-impact-plan.json"))
    root_report = pathlib.Path("reports/registry-impact-plan.json")
    if root_report.exists():
        reports.insert(0, root_report)
    return reports


def count_summary(entries: object, key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(entries, list):
        return counts
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = entry.get(key)
        count = entry.get("count")
        if isinstance(value, str) and isinstance(count, int):
            counts[value] = count
    return counts


def validate_consistency(path: pathlib.Path, plan: dict[str, object]) -> None:
    scope = plan.get("scope", "source")
    provider = plan.get("provider")
    source_id = plan.get("source_id")
    summary = as_dict(plan.get("summary"), path)
    changes = plan.get("changes")
    if not isinstance(changes, list):
        raise ValueError("changes must be an array")
    if scope not in {"source", "release"}:
        raise ValueError("scope must be source or release")

    category_counts: collections.Counter[str] = collections.Counter()
    target_counts: collections.Counter[str] = collections.Counter()
    manual_review = 0
    db_migration_review = 0
    served_contract_regeneration = 0

    for index, raw_change in enumerate(changes):
        if not isinstance(raw_change, dict):
            raise ValueError(f"changes[{index}] must be an object")
        identity = as_dict(raw_change.get("identity"), path)
        if scope == "source" and identity.get("provider") != provider:
            raise ValueError(f"changes[{index}].identity.provider does not match plan provider")
        if scope == "source" and identity.get("source_id") != source_id:
            raise ValueError(f"changes[{index}].identity.source_id does not match plan source_id")

        category = raw_change.get("category")
        if isinstance(category, str):
            category_counts[category] += 1

        promoted = raw_change.get("promoted_dataset")
        served = raw_change.get("served_dataset")
        actions = raw_change.get("actions")
        if not isinstance(actions, list):
            raise ValueError(f"changes[{index}].actions must be an array")

        for action in actions:
            if not isinstance(action, dict):
                raise ValueError(f"changes[{index}].actions must contain objects")
            target = action.get("target")
            action_kind = action.get("action")
            automation = action.get("automation")
            if isinstance(target, str):
                target_counts[target] += 1
            if automation in {"manual_review", "blocked"}:
                manual_review += 1
            if action_kind == "db_migration_review":
                db_migration_review += 1
            if target in CLIENT_SERVER_TARGETS and action_kind == "regenerate":
                served_contract_regeneration += 1
            if promoted is False and action_kind == "db_migration_review":
                raise ValueError(f"changes[{index}] cannot request db_migration_review without a promoted dataset")
            if served is False and target in CLIENT_SERVER_TARGETS and action_kind == "regenerate":
                raise ValueError(f"changes[{index}] cannot regenerate {target} without a served dataset")

    if summary.get("total") != len(changes):
        raise ValueError(f"summary.total expected {len(changes)}, got {summary.get('total')}")
    if count_summary(summary.get("by_category"), "category") != dict(category_counts):
        raise ValueError("summary.by_category does not match changes")
    if count_summary(summary.get("by_target"), "target") != dict(target_counts):
        raise ValueError("summary.by_target does not match change actions")
    if summary.get("requires_manual_review") != manual_review:
        raise ValueError("summary.requires_manual_review does not match actions")
    if summary.get("requires_db_migration_review") != db_migration_review:
        raise ValueError("summary.requires_db_migration_review does not match actions")
    if summary.get("requires_served_contract_regeneration") != served_contract_regeneration:
        raise ValueError("summary.requires_served_contract_regeneration does not match actions")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.registry-impact-plan.v1.schema.json",
        type=pathlib.Path,
        help="registry impact plan JSON Schema path",
    )
    parser.add_argument(
        "reports",
        nargs="*",
        type=pathlib.Path,
        help="impact plans to validate; defaults to reports/*/registry-impact-plan.json",
    )
    args = parser.parse_args()

    reports = args.reports or default_reports()
    if not reports:
        print("no registry impact plans found", file=sys.stderr)
        return 1

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for report_path in reports:
        try:
            instance = as_dict(load_json(report_path), report_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
            if not errors:
                validate_consistency(report_path, instance)
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

        source_id = instance.get("source_id", "<unknown>")
        total = instance.get("summary", {}).get("total", "<unknown>")
        print(f"ok {report_path} ({source_id}, changes={total})")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
