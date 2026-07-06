#!/usr/bin/env python3
"""Generate the release-wide registry impact plan rollup."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys


CLIENT_SERVER_TARGETS = {"dataset-api", "sdk", "mcp"}
RELEASE_OVERLAY_PROVIDER = "datapan-registry"
RELEASE_OVERLAY_SOURCE_ID = "registry"


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


def release_overlay_changes(output: pathlib.Path) -> list[dict[str, object]]:
    if not output.exists():
        return []
    existing = load_json(output)
    if existing.get("scope") != "release":
        return []
    changes = existing.get("changes")
    if not isinstance(changes, list):
        return []

    overlays: list[dict[str, object]] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        identity = change.get("identity")
        if not isinstance(identity, dict):
            continue
        if (
            identity.get("provider") == RELEASE_OVERLAY_PROVIDER
            and identity.get("source_id") == RELEASE_OVERLAY_SOURCE_ID
        ):
            overlays.append(change)
    return overlays


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


def build_rollup(reports_dir: pathlib.Path, output: pathlib.Path) -> tuple[dict[str, object], int]:
    paths = source_plan_paths(reports_dir, output)
    if not paths:
        raise ValueError("no source-scoped registry impact plans found")

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
    changes.extend(release_overlay_changes(output))

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
        "current_registry": str(output),
        "summary": count_entries(changes),
        "changes": changes,
    }
    return rollup, len(paths)


def render_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", type=pathlib.Path, default=pathlib.Path("reports"))
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=pathlib.Path("reports/registry-impact-plan.json"),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate that the checked-in rollup matches generated output without writing",
    )
    args = parser.parse_args()

    try:
        rollup, source_count = build_rollup(args.reports_dir, args.output)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    rendered = render_json(rollup)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing generated rollup", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale impact plan rollup; "
                "run `python3 scripts/generate-impact-plan-rollup.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} ({len(rollup['changes'])} changes from {source_count} source plans)")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        handle.write(rendered)
    print(f"wrote {args.output} ({len(rollup['changes'])} changes from {source_count} source plans)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
