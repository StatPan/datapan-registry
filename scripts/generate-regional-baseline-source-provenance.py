#!/usr/bin/env python3
"""Generate the reviewed KOSIS provenance artifact and consumer pin proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any

POLICY = pathlib.Path("policy/kosis-regional-baseline-v0-provenance.json")
SOURCE_PROFILE = pathlib.Path("sources/kosis.json")
ARTIFACT = pathlib.Path("reports/regional-baseline-source-provenance.json")
MANIFEST = pathlib.Path("manifest.json")
FIXTURE = pathlib.Path("fixtures/source-provenance/regional-baseline-v0-pin.json")


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def build(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "datapan.source-provenance.v1",
        "generated_at": policy["generated_at"],
        "authority": "datapan-registry",
        "pack_contract": policy["pack_contract"],
        "source": {
            **policy["source"],
            "source_profile": {"path": SOURCE_PROFILE.as_posix(), "sha256": sha256(SOURCE_PROFILE)},
        },
        "inputs": policy["inputs"],
        "rights": policy["rights"],
        "freshness": policy["freshness"],
        "consumer_pin": {
            "artifact_path": ARTIFACT.as_posix(),
            "required_release_fields": [
                "registry_tag",
                "registry_manifest_sha256",
                "provenance_artifact_sha256",
            ],
        },
    }


def build_fixture(manifest: dict[str, Any]) -> dict[str, Any]:
    artifacts = {item["path"]: item for item in manifest["artifacts"]}
    artifact = artifacts.get(ARTIFACT.as_posix())
    if artifact is None:
        raise ValueError(f"manifest does not bind {ARTIFACT}")
    return {
        "schema_version": "datapan.source-provenance-consumer-pin-fixture.v1",
        "publication_status": "fixture_only_unreleased",
        "registry_tag": "issue-559-unreleased-fixture",
        "registry_manifest": {"path": MANIFEST.as_posix(), "sha256": sha256(MANIFEST)},
        "provenance_artifact": {"path": ARTIFACT.as_posix(), "sha256": artifact["sha256"]},
        "expected_inputs": [
            {"source_id": "kosis", "indicator_id": item["indicator_id"], "table_id": item["table_id"]}
            for item in load(ARTIFACT)["inputs"]
        ],
    }


def write_or_check(path: pathlib.Path, value: object, check: bool) -> None:
    expected = render(value)
    if check:
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise ValueError(f"generated artifact drift: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--artifact-only", action="store_true")
    args = parser.parse_args()
    try:
        artifact = build(load(POLICY))
        write_or_check(ARTIFACT, artifact, args.check)
        if not args.artifact_only:
            fixture = build_fixture(load(MANIFEST))
            write_or_check(FIXTURE, fixture, args.check)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL regional baseline source provenance: {exc}", file=sys.stderr)
        return 1
    print("ok regional baseline source provenance generation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
