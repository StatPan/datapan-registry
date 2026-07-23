#!/usr/bin/env python3
"""Generate a deterministic request-only profile from the immutable operation manifest.

The generated file is intentionally descriptive.  It never contains credential
values, approved runtime values, response types, retries, or provider-call code.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest.json"
SOURCE = ROOT / "reports/data-go-kr/operation-manifest.json"
SCHEMA = ROOT / "schemas/datapan.request-only-client-profile.v1.schema.json"
OUTPUT = ROOT / "reports/data-go-kr/request-only-client-profile.json"
FIXTURE = ROOT / "fixtures/request-only-client-profile/registry-local-consumer-proof.v1.json"


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def render(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def digest_binding(path: pathlib.Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": path.relative_to(ROOT).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def release_manifest_identity(manifest: dict[str, Any]) -> str:
    """Hash the release ledger except this artifact, avoiding a hash cycle.

    The consumer still verifies this profile's own raw SHA-256 through the
    manifest.  Omitting only that self-referential descriptor makes the rest of
    the pinned manifest immutable and independently reproducible.
    """
    value = copy.deepcopy(manifest)
    profile_path = OUTPUT.relative_to(ROOT).as_posix()
    value["artifacts"] = [item for item in value["artifacts"] if item.get("path") != profile_path]
    value["artifact_count"] = len(value["artifacts"])
    return hashlib.sha256(render(value)).hexdigest()


def build(source: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for source_operation in source["operations"]:
        transport = source_operation["transport"]
        if transport["endpoint"] is None:
            outcome = "unsupported_missing_endpoint"
            profile_transport = None
        else:
            outcome = "unsupported_requires_approved_parameters"
            profile_transport = transport
        operations.append({
            "operation_id": source_operation["operation_id"],
            "protocol": source_operation["protocol"],
            "provenance": source_operation["provenance"],
            "transport": profile_transport,
            "outcome": outcome,
            "requirements": source_operation["requirements"],
        })
    operations.sort(key=lambda item: item["operation_id"])
    unsupported_approved = sum(item["outcome"] == "unsupported_requires_approved_parameters" for item in operations)
    unsupported_endpoint = sum(item["outcome"] == "unsupported_missing_endpoint" for item in operations)
    return {
        "schema_version": "datapan.request-only-client-profile.v1",
        "authority": "datapan-registry",
        "capabilities": {
            "request_preparation": True,
            "provider_invocation": False,
            "typed_response": False,
            "all_operations_executable": False,
        },
        "release_binding": {
            "release_manifest": {
                "path": "manifest.json",
                "algorithm": "sha256-canonical-release-manifest-excluding-profile-artifact-v1",
                "sha256": release_manifest_identity(manifest),
                "excluded_artifact_path": OUTPUT.relative_to(ROOT).as_posix(),
            },
            "profile_schema": digest_binding(SCHEMA),
            "source_operation_manifest": digest_binding(SOURCE),
        },
        "summary": {
            "operations": len(operations),
            "request_descriptors": len(operations) - unsupported_endpoint,
            "unsupported_requires_approved_parameters": unsupported_approved,
            "unsupported_missing_endpoint": unsupported_endpoint,
        },
        "operations": operations,
    }


def build_consumer_fixture(profile: dict[str, Any]) -> dict[str, Any]:
    """Build an offline Registry-local consumer pin proof for this profile."""
    return {
        "schema_version": "datapan.request-only-client-profile-consumer-proof.v1",
        "consumer": "registry-local-static-proof",
        "verification_mode": "offline_pin_validation_only",
        "pins": {
            "manifest_release_identity": profile["release_binding"]["release_manifest"],
            "profile": {
                "path": OUTPUT.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(render(profile)).hexdigest(),
            },
            "profile_schema": {
                "path": SCHEMA.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(SCHEMA.read_bytes()).hexdigest(),
            },
            "source_operation_manifest": {
                "path": SOURCE.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
            },
        },
        "unsupported_outcomes": sorted({item["outcome"] for item in profile["operations"]}),
        "consumer_rule": "After every pin validates, retain the request descriptor only when present and surface the explicit unsupported outcome without attempting a provider request.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when the checked-in profile drifts")
    parser.add_argument("--manifest", type=pathlib.Path, default=MANIFEST)
    parser.add_argument("--source", type=pathlib.Path, default=SOURCE)
    parser.add_argument("--output", type=pathlib.Path, default=OUTPUT)
    args = parser.parse_args()
    try:
        profile = build(load(args.source), load(args.manifest))
        expected = render(profile)
        expected_fixture = render(build_consumer_fixture(profile))
        if args.check:
            if not args.output.is_file() or args.output.read_bytes() != expected:
                raise ValueError(f"generated artifact drift: {args.output}")
            if not FIXTURE.is_file() or FIXTURE.read_bytes() != expected_fixture:
                raise ValueError(f"generated consumer fixture drift: {FIXTURE}")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(expected)
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_bytes(expected_fixture)
    except Exception as exc:  # noqa: BLE001 - a release check must fail closed
        print(f"FAIL generate request-only client profile: {exc}", file=sys.stderr)
        return 1
    print(f"ok request-only client profile (operations={len(load(args.source)['operations'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
