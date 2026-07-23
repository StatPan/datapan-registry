#!/usr/bin/env python3
"""Fail closed for request-only client profile release and consumer pin drift."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import pathlib
import sys
from typing import Any

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILE = ROOT / "reports/data-go-kr/request-only-client-profile.json"
SCHEMA = ROOT / "schemas/datapan.request-only-client-profile.v1.schema.json"
SOURCE = ROOT / "reports/data-go-kr/operation-manifest.json"
MANIFEST = ROOT / "manifest.json"
FIXTURE = ROOT / "fixtures/request-only-client-profile/registry-local-consumer-proof.v1.json"
GENERATOR_PATH = ROOT / "scripts/generate-data-go-kr-request-only-client-profile.py"
FORBIDDEN_KEYS = {"provider_call", "executable_client", "typed_client", "typed_response", "retry_policy", "credential_value", "approved_value", "request_value", "response_type"}


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def generator_module():
    spec = importlib.util.spec_from_file_location("request_only_client_profile", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load request-only profile generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reject_claims(value: Any, path: str = "profile") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            fail(key not in FORBIDDEN_KEYS or (key == "typed_response" and child is False), f"{path}: forbidden executable capability {key}")
            reject_claims(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_claims(child, f"{path}[{index}]")


def validate(profile: dict[str, Any], schema: dict[str, Any], source: dict[str, Any], manifest: dict[str, Any], fixture: dict[str, Any]) -> None:
    jsonschema.Draft202012Validator(schema).validate(profile)
    reject_claims(profile)
    source_ids = [item["operation_id"] for item in source["operations"]]
    profile_ids = [item["operation_id"] for item in profile["operations"]]
    fail(len(profile_ids) == len(set(profile_ids)), "duplicate operation identity")
    fail(profile_ids == sorted(source_ids), "missing or unknown operation identity")
    generator = generator_module()
    expected = generator.build(source, manifest)
    fail(profile == expected, "profile does not deterministically match pinned release inputs")
    fail(profile["release_binding"]["source_operation_manifest"] == generator.digest_binding(SOURCE), "source operation manifest digest mismatch")
    fail(profile["release_binding"]["profile_schema"] == generator.digest_binding(SCHEMA), "profile schema digest mismatch")
    fail(profile["release_binding"]["release_manifest"]["sha256"] == generator.release_manifest_identity(manifest), "release manifest identity mismatch")

    for item in profile["operations"]:
        if item["outcome"] == "unsupported_missing_endpoint":
            fail(item["transport"] is None, f"{item['operation_id']}: missing-endpoint outcome must omit transport")
        else:
            fail(item["transport"] is not None, f"{item['operation_id']}: approved-parameter outcome must retain a request descriptor")

    artifacts = {item["path"]: item for item in manifest["artifacts"]}
    for path, kind, schema_id in ((PROFILE, "request_only_client_profile", schema["$id"]), (SCHEMA, "schema", None), (SOURCE, "data_go_kr_operation_manifest", None)):
        relative = path.relative_to(ROOT).as_posix()
        artifact = artifacts.get(relative)
        fail(artifact is not None, f"release manifest missing {relative}")
        fail(artifact["bytes"] == path.stat().st_size and artifact["sha256"] == sha256(path), f"release manifest digest mismatch: {relative}")
        fail(artifact["kind"] == kind, f"release manifest kind mismatch: {relative}")
        if schema_id:
            fail(artifact.get("schema") == schema_id, "profile release schema mismatch")

    fail(fixture["schema_version"] == "datapan.request-only-client-profile-consumer-proof.v1", "consumer fixture schema mismatch")
    pins = fixture["pins"]
    fail(pins["manifest_release_identity"] == profile["release_binding"]["release_manifest"], "consumer manifest pin mismatch")
    fail(pins["profile"] == {"path": PROFILE.relative_to(ROOT).as_posix(), "sha256": sha256(PROFILE)}, "consumer profile pin mismatch")
    fail(pins["profile_schema"] == {"path": SCHEMA.relative_to(ROOT).as_posix(), "sha256": sha256(SCHEMA)}, "consumer schema pin mismatch")
    fail(pins["source_operation_manifest"] == {"path": SOURCE.relative_to(ROOT).as_posix(), "sha256": sha256(SOURCE)}, "consumer source pin mismatch")
    outcomes = {item["outcome"] for item in profile["operations"]}
    fail(fixture["unsupported_outcomes"] == sorted(outcomes), "consumer unsupported-outcome contract mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        validate(load(PROFILE), load(SCHEMA), load(SOURCE), load(MANIFEST), load(FIXTURE))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL request-only client profile: {exc}", file=sys.stderr)
        return 1
    summary = load(PROFILE)["summary"]
    print(f"ok request-only client profile (operations={summary['operations']}, descriptors={summary['request_descriptors']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
