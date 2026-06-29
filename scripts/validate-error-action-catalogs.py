#!/usr/bin/env python3
"""Validate checked-in Datapan error action catalogs."""

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
        "missing dependency: install jsonschema before running error action catalog validation"
    ) from exc


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, path: pathlib.Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def default_catalogs() -> list[pathlib.Path]:
    return sorted(pathlib.Path("reports").glob("*/error-action-catalog.json"))


def validate_consistency(catalog_path: pathlib.Path, catalog: dict[str, object]) -> None:
    source_profile = catalog.get("source_profile")
    if not isinstance(source_profile, str) or not source_profile:
        raise ValueError("source_profile is required for checked-in error action catalogs")

    profile_path = pathlib.Path(source_profile)
    if not profile_path.exists():
        raise ValueError(f"source_profile does not exist: {source_profile}")

    profile = as_dict(load_json(profile_path), profile_path)
    if catalog.get("source_id") != profile.get("source_id"):
        raise ValueError("source_id does not match source_profile")
    if catalog.get("provider") != profile.get("provider"):
        raise ValueError("provider does not match source_profile")

    summary = as_dict(catalog.get("summary"), catalog_path)
    rules = catalog.get("rules")
    if not isinstance(rules, list):
        raise ValueError("rules must be an array")

    rule_ids: collections.Counter[str] = collections.Counter()
    blocking_rules = 0
    manual_review_rules = 0
    unknown_signature_rules = 0

    for index, raw_rule in enumerate(rules):
        rule = as_dict(raw_rule, catalog_path)
        rule_id = rule.get("rule_id")
        if isinstance(rule_id, str):
            rule_ids[rule_id] += 1

        classification = rule.get("classification")
        if classification == "unknown":
            unknown_signature_rules += 1

        actions = rule.get("actions")
        if not isinstance(actions, list):
            raise ValueError(f"rules[{index}].actions must be an array")

        action_automations = {
            action.get("automation")
            for action in actions
            if isinstance(action, dict)
        }
        is_blocking = (
            rule.get("status") == "blocked"
            or rule.get("severity") == "blocking"
            or "blocked" in action_automations
        )
        if is_blocking:
            blocking_rules += 1
        elif "manual_review" in action_automations:
            manual_review_rules += 1

    duplicates = sorted(rule_id for rule_id, count in rule_ids.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate rule_id values: {', '.join(duplicates)}")

    expected = {
        "rules": len(rules),
        "blocking_rules": blocking_rules,
        "manual_review_rules": manual_review_rules,
        "unknown_signature_rules": unknown_signature_rules,
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            raise ValueError(f"summary.{key} expected {value}, got {summary.get(key)}")


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
            instance = as_dict(load_json(catalog_path), catalog_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
            if not errors:
                validate_consistency(catalog_path, instance)
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

        source_id = instance.get("source_id", "<unknown>")
        rules = instance.get("summary", {}).get("rules", "<unknown>")
        print(f"ok {catalog_path} ({source_id}, rules={rules})")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
