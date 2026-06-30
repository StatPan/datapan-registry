#!/usr/bin/env python3
"""Generate the release-wide registry impact plan rollup."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib


CLIENT_SERVER_TARGETS = {"dataset-api", "sdk", "mcp"}


def load_json(path: pathlib.Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def source_plan_paths(reports_dir: pathlib.Path, output: pathlib.Path) -> list[pathlib.Path]:
    return [
        path
        for path in sorted(reports_dir.glob("*/registry-impact-plan.json"))
        if path.resolve() != output.resolve()
    ]


def count_entries(changes: list[dict[str, object]]) -> dict[str, object]:
    category_counts: collections.Counter[str] = collections.Counter()
    target_counts: collections.Counter[str] = collections.Counter()
    manual_review = 0
    db_migration_review = 0
    served_contract_regeneration = 0

    for change in changes:
        category = change.get("category")
        if isinstance(category, str):
            category_counts[category] += 1
        actions = change.get("actions", [])
        if not isinstance(actions, list):
            continue
        for action in actions:
            if not isinstance(action, dict):
                continue
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

    return {
        "total": len(changes),
        "by_category": [
            {"category": key, "count": category_counts[key]}
            for key in sorted(category_counts)
        ],
        "by_target": [
            {"target": key, "count": target_counts[key]}
            for key in sorted(target_counts)
        ],
        "requires_manual_review": manual_review,
        "requires_db_migration_review": db_migration_review,
        "requires_served_contract_regeneration": served_contract_regeneration,
    }


def common_value(plans: list[dict[str, object]], key: str, fallback: str) -> str:
    values = {plan.get(key) for plan in plans if isinstance(plan.get(key), str)}
    if len(values) == 1:
        return next(iter(values))
    return fallback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", type=pathlib.Path, default=pathlib.Path("reports"))
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=pathlib.Path("reports/registry-impact-plan.json"),
    )
    args = parser.parse_args()

    paths = source_plan_paths(args.reports_dir, args.output)
    if not paths:
        raise SystemExit("no source-scoped registry impact plans found")

    plans = [load_json(path) for path in paths]
    changes: list[dict[str, object]] = []
    generated_at_values: list[str] = []
    for plan in plans:
        generated_at = plan.get("generated_at")
        if isinstance(generated_at, str):
            generated_at_values.append(generated_at)
        plan_changes = plan.get("changes")
        if not isinstance(plan_changes, list):
            raise ValueError("source plan changes must be an array")
        for change in plan_changes:
            if not isinstance(change, dict):
                raise ValueError("source plan changes must contain objects")
            changes.append(change)

    rollup = {
        "schema_version": "datapan.registry-impact-plan.v1",
        "generated_at": max(generated_at_values),
        "datapan_version": common_value(plans, "datapan_version", "mixed"),
        "scope": "release",
        "provider": "datapan-registry",
        "source_id": "registry",
        "registry_version_from": common_value(plans, "registry_version_from", "mixed"),
        "registry_version_to": common_value(plans, "registry_version_to", "mixed"),
        "previous_registry": "reports/*/registry-impact-plan.json",
        "current_registry": str(args.output),
        "summary": count_entries(changes),
        "changes": changes,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(rollup, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"wrote {args.output} ({len(changes)} changes from {len(paths)} source plans)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
