#!/usr/bin/env python3
"""Validate the manifest-bound KOSIS provenance contract for regional_baseline_v0."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from typing import Any

import jsonschema

ARTIFACT = pathlib.Path("reports/regional-baseline-source-provenance.json")
SCHEMA = pathlib.Path("schemas/datapan.source-provenance.v1.schema.json")
SOURCE_PROFILE = pathlib.Path("sources/kosis.json")
MANIFEST = pathlib.Path("manifest.json")
FIXTURE = pathlib.Path("fixtures/source-provenance/regional-baseline-v0-pin.json")

EXPECTED_INPUTS = {
    ("registered_population", "816", "DT_1YL20651E", "Ministry of the Interior and Safety"),
    ("one_person_household_rate", "1157", "DT_1YL21161", "National Data Agency Population Census Division"),
    ("registered_business_count", "949", "DT_1YL20832", "National Data Agency Economic Census Division"),
}
EXPECTED_TRIGGERS = {
    "usage_guide_changed",
    "terms_changed",
    "table_notice_changed",
    "publisher_or_table_identity_changed",
    "new_public_release",
}
FORBIDDEN_KEYS = {
    "current", "current_json", "current_pointer", "data_artifact", "data_artifact_locator",
    "csv", "story", "consumer_evidence", "source_snapshots", "materialization",
    "health_observation", "health_observations", "health_history", "credential", "credentials",
    "response_row", "response_rows", "cli_runtime", "dataset_api",
}


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reject_forbidden(value: object, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                raise ValueError(f"forbidden ownership field at {location}.{key}")
            reject_forbidden(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_forbidden(child, f"{location}[{index}]")


def validate() -> None:
    artifact = load(ARTIFACT)
    schema = load(SCHEMA)
    manifest = load(MANIFEST)
    fixture = load(FIXTURE)
    source_profile = load(SOURCE_PROFILE)

    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(artifact)
    reject_forbidden(artifact)

    entries = [item for item in manifest["artifacts"] if item["path"] == ARTIFACT.as_posix()]
    if len(entries) != 1:
        raise ValueError("manifest must bind the provenance artifact exactly once")
    entry = entries[0]
    if entry.get("kind") != "source_provenance":
        raise ValueError("manifest artifact kind must be source_provenance")
    if entry.get("schema") != schema["$id"]:
        raise ValueError("manifest schema binding does not match the provenance schema")
    if entry.get("bytes") != ARTIFACT.stat().st_size or entry.get("sha256") != sha256(ARTIFACT):
        raise ValueError("manifest provenance artifact bytes or digest drift")

    if source_profile.get("source_id") != "kosis":
        raise ValueError("source profile is not KOSIS")
    if artifact["source"]["source_profile"] != {
        "path": SOURCE_PROFILE.as_posix(), "sha256": sha256(SOURCE_PROFILE)
    }:
        raise ValueError("source profile identity or digest drift")

    actual_inputs = {
        (item["metric_id"], item["indicator_id"], item["table_id"], item["publisher"])
        for item in artifact["inputs"]
    }
    if actual_inputs != EXPECTED_INPUTS:
        raise ValueError("configured KOSIS indicator, table, or publisher identity drift")

    rights = artifact["rights"]
    if rights["assessment"] != "conditional_domestic_statistics_reuse":
        raise ValueError("rights must remain conditional")
    if rights["usage_surface"] != "kosis_web_parsing_domestic_statistics":
        raise ValueError("rights must identify the eRegion web parsing surface")
    if rights["credential_scope"] != "no_openapi_credential_or_entitlement_asserted":
        raise ValueError("provenance must not imply OpenAPI credentials or entitlement")
    prohibited = rights["prohibited_uses"]
    if len(prohibited) != 1 or prohibited[0].get("code") != "unchanged_raw_paid_redistribution":
        raise ValueError("unchanged raw paid redistribution prohibition is required")
    if set(rights["revalidation_triggers"]) != EXPECTED_TRIGGERS:
        raise ValueError("rights revalidation triggers drift")
    if artifact["freshness"]["mode"] != "not_asserted":
        raise ValueError("Registry must not assert KOSIS snapshot freshness")

    if fixture.get("publication_status") != "fixture_only_unreleased":
        raise ValueError("consumer pin proof must remain an unreleased fixture")
    if not fixture.get("registry_tag"):
        raise ValueError("consumer pin fixture requires a tag-shaped identity")
    if fixture.get("registry_manifest") != {"path": MANIFEST.as_posix(), "sha256": sha256(MANIFEST)}:
        raise ValueError("consumer pin manifest digest drift")
    if fixture.get("provenance_artifact") != {
        "path": ARTIFACT.as_posix(), "sha256": entry["sha256"]
    }:
        raise ValueError("consumer pin provenance digest drift")
    expected_fixture_inputs = {
        (item["source_id"], item["indicator_id"], item["table_id"])
        for item in fixture.get("expected_inputs", [])
    }
    if expected_fixture_inputs != {("kosis", indicator, table) for _, indicator, table, _ in EXPECTED_INPUTS}:
        raise ValueError("consumer pin input identity drift")


def main() -> int:
    try:
        validate()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL regional baseline source provenance: {exc}", file=sys.stderr)
        return 1
    print("ok regional baseline source provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
