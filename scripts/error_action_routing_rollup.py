#!/usr/bin/env python3
"""Build and validate release-wide error/action routing rollups."""

from __future__ import annotations

import collections
import hashlib
import json
import pathlib
from typing import Any


ROLLUP_SCHEMA_VERSION = "datapan.error-action-routing-rollup.v1"
CATALOG_SCHEMA_VERSION = "datapan.error-action-catalog.v1"
DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_CATALOG_GLOB = "reports/*/error-action-catalog.json"


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def stable_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(stable_json_bytes(value))


def as_dict(value: object, label: str | pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def as_list(value: object, label: str | pathlib.Path) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generated_at(manifest_path: pathlib.Path) -> str:
    if manifest_path.exists():
        manifest = as_dict(load_json(manifest_path), manifest_path)
        value = manifest.get("generated_at")
        if isinstance(value, str) and value:
            return value
    return "1970-01-01T00:00:00Z"


def catalog_paths(pattern: str) -> list[pathlib.Path]:
    return sorted(pathlib.Path().glob(pattern))


def source_slug(path: pathlib.Path) -> str:
    parent = path.parent.name
    if parent:
        return parent
    return path.stem


def count_rule_summary(catalog_path: pathlib.Path, catalog: dict[str, Any]) -> dict[str, Any]:
    rules = [as_dict(rule, f"{catalog_path}.rules[{index}]") for index, rule in enumerate(as_list(catalog.get("rules"), catalog_path))]
    classifications: collections.Counter[str] = collections.Counter()
    action_targets: collections.Counter[str] = collections.Counter()
    action_names: collections.Counter[str] = collections.Counter()
    automations: collections.Counter[str] = collections.Counter()
    impact_categories: collections.Counter[str] = collections.Counter()
    blocking_rules = 0
    manual_review_rules = 0
    unknown_signature_rules = 0
    actions_total = 0

    for rule_index, rule in enumerate(rules):
        classification = rule.get("classification")
        if isinstance(classification, str):
            classifications[classification] += 1
            if classification == "unknown":
                unknown_signature_rules += 1

        actions = [as_dict(action, f"{catalog_path}.rules[{rule_index}].actions[{action_index}]") for action_index, action in enumerate(as_list(rule.get("actions"), f"{catalog_path}.rules[{rule_index}].actions"))]
        action_automations = set()
        for action in actions:
            target = action.get("target")
            if isinstance(target, str):
                action_targets[target] += 1
            name = action.get("action")
            if isinstance(name, str):
                action_names[name] += 1
            automation = action.get("automation")
            if isinstance(automation, str):
                automations[automation] += 1
                action_automations.add(automation)
            actions_total += 1

        if (
            rule.get("status") == "blocked"
            or rule.get("severity") == "blocking"
            or "blocked" in action_automations
        ):
            blocking_rules += 1
        elif "manual_review" in action_automations:
            manual_review_rules += 1

        for category in as_list(rule.get("impact_categories", []), f"{catalog_path}.rules[{rule_index}].impact_categories"):
            if isinstance(category, str):
                impact_categories[category] += 1

    return {
        "rules": len(rules),
        "actions": actions_total,
        "blocking_rules": blocking_rules,
        "manual_review_rules": manual_review_rules,
        "unknown_signature_rules": unknown_signature_rules,
        "classifications": dict(sorted(classifications.items())),
        "action_targets": dict(sorted(action_targets.items())),
        "actions_by_name": dict(sorted(action_names.items())),
        "automations": dict(sorted(automations.items())),
        "impact_categories": dict(sorted(impact_categories.items())),
    }


def catalog_entry(catalog_path: pathlib.Path) -> dict[str, Any]:
    catalog = as_dict(load_json(catalog_path), catalog_path)
    if catalog.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ValueError(f"{catalog_path}.schema_version must be {CATALOG_SCHEMA_VERSION}")

    source_id = catalog.get("source_id")
    provider = catalog.get("provider")
    source_profile = catalog.get("source_profile")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError(f"{catalog_path}.source_id must be a non-empty string")
    if not isinstance(provider, str) or not provider:
        raise ValueError(f"{catalog_path}.provider must be a non-empty string")
    if not isinstance(source_profile, str) or not source_profile:
        raise ValueError(f"{catalog_path}.source_profile must be a non-empty string")

    counted = count_rule_summary(catalog_path, catalog)
    summary = as_dict(catalog.get("summary"), f"{catalog_path}.summary")
    for key in ("rules", "blocking_rules", "manual_review_rules", "unknown_signature_rules"):
        if summary.get(key) != counted[key]:
            raise ValueError(f"{catalog_path}.summary.{key} expected {counted[key]}, got {summary.get(key)}")

    return {
        "source_id": source_id,
        "provider": provider,
        "catalog": catalog_path.as_posix(),
        "source_profile": source_profile,
        "bytes": catalog_path.stat().st_size,
        "sha256": file_sha256(catalog_path),
        **counted,
    }


def merge_counts(entries: list[dict[str, Any]], key: str) -> dict[str, int]:
    counter: collections.Counter[str] = collections.Counter()
    for entry in entries:
        values = as_dict(entry.get(key), key)
        for name, value in values.items():
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{entry['catalog']}.{key}.{name} must be a non-negative integer")
            counter[name] += value
    return dict(sorted(counter.items()))


def build_rollup(
    *,
    manifest_path: pathlib.Path = DEFAULT_MANIFEST,
    catalog_glob: str = DEFAULT_CATALOG_GLOB,
) -> dict[str, Any]:
    paths = catalog_paths(catalog_glob)
    if not paths:
        raise ValueError(f"no error action catalogs matched {catalog_glob}")

    catalogs = [catalog_entry(path) for path in paths]
    source_ids = sorted({str(catalog["source_id"]) for catalog in catalogs})
    providers = sorted({str(catalog["provider"]) for catalog in catalogs})
    rules = sum(int(catalog["rules"]) for catalog in catalogs)
    actions = sum(int(catalog["actions"]) for catalog in catalogs)
    blocking_rules = sum(int(catalog["blocking_rules"]) for catalog in catalogs)
    manual_review_rules = sum(int(catalog["manual_review_rules"]) for catalog in catalogs)
    unknown_signature_rules = sum(int(catalog["unknown_signature_rules"]) for catalog in catalogs)
    source_inputs = [
        {
            "path": str(catalog["catalog"]),
            "source_id": str(catalog["source_id"]),
            "provider": str(catalog["provider"]),
            "bytes": int(catalog["bytes"]),
            "sha256": str(catalog["sha256"]),
            "rules": int(catalog["rules"]),
            "actions": int(catalog["actions"]),
        }
        for catalog in catalogs
    ]

    return {
        "schema_version": ROLLUP_SCHEMA_VERSION,
        "generated_at": generated_at(manifest_path),
        "provider": "multi-source",
        "generation_inputs": {
            "release_manifest": manifest_path.as_posix(),
            "catalog_glob": catalog_glob,
            "generator": "scripts/generate-error-action-routing-rollup.py",
        },
        "source_inputs": source_inputs,
        "summary": {
            "catalogs": len(catalogs),
            "sources": len(source_ids),
            "providers": len(providers),
            "rules": rules,
            "actions": actions,
            "blocking_rules": blocking_rules,
            "manual_review_rules": manual_review_rules,
            "unknown_signature_rules": unknown_signature_rules,
            "classifications": merge_counts(catalogs, "classifications"),
            "action_targets": merge_counts(catalogs, "action_targets"),
            "actions_by_name": merge_counts(catalogs, "actions_by_name"),
            "automations": merge_counts(catalogs, "automations"),
            "impact_categories": merge_counts(catalogs, "impact_categories"),
        },
        "catalogs": catalogs,
    }


def validate_rollup_consistency(rollup: dict[str, Any], expected: dict[str, Any]) -> None:
    if rollup != expected:
        raise ValueError("error-action routing rollup is stale or inconsistent with source catalogs")
