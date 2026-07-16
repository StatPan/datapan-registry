#!/usr/bin/env python3
"""Enforce the diagnostic release candidate's non-authoritative boundary."""

from __future__ import annotations

import pathlib
import sys

import importlib.util


ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "scripts/generate-diagnostic-release-candidate.py"
SPEC = importlib.util.spec_from_file_location("diagnostic_candidate_generator", GENERATOR_PATH)
GENERATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(GENERATOR)
MANIFEST = ROOT / "manifest.json"
SCHEMA_INDEX = ROOT / "schemas/index.json"


def validate_all() -> dict[str, int | str]:
    intake = GENERATOR.load(GENERATOR.DEFAULT_INTAKE)
    candidate = GENERATOR.load(GENERATOR.DEFAULT_OUTPUT)
    expected = GENERATOR.build(intake)
    if candidate != expected:
        raise ValueError("candidate is stale or not deterministically generated from the intake")
    authority = candidate.get("authority", {})
    if any(authority.get(key) is not False for key in (
        "release_authority", "runtime_authority", "fixture_runtime_authority",
        "manifest_inclusion", "publishing_allowed",
    )):
        raise ValueError("draft candidate must have no release, runtime, manifest, or publishing authority")
    decision = candidate.get("decision", {})
    all_accepted = decision.get("all_consumers_accepted") is True
    expected_status = "ready_for_publication_review" if all_accepted else "blocked"
    if candidate.get("status") != expected_status:
        raise ValueError("candidate status does not match consumer proof completeness")
    if not all_accepted and not decision.get("missing_proofs"):
        raise ValueError("blocked candidate must expose missing proofs")
    if all_accepted and decision.get("missing_proofs"):
        raise ValueError("complete candidate cannot retain missing proofs")
    forbidden_prefix = "drafts/diagnostic-envelope/"
    for owner, path in (("manifest", MANIFEST), ("schema index", SCHEMA_INDEX)):
        value = GENERATOR.load(path)
        entries = value.get("artifacts", value.get("schemas", []))
        if any(item.get("path", "").startswith(forbidden_prefix) for item in entries):
            raise ValueError(f"{owner} must not publish diagnostic draft artifacts")
    return {
        "consumers": len(candidate["binding"]["consumer_proofs"]),
        "missing_proofs": len(candidate["decision"]["missing_proofs"]),
        "binding_sha256": candidate["binding_sha256"],
    }


def main() -> int:
    try:
        result = validate_all()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL diagnostic release candidate boundary: {exc}", file=sys.stderr)
        return 1
    print(
        "ok diagnostic release candidate boundary "
        f"(consumers={result['consumers']}, missing={result['missing_proofs']}, binding={result['binding_sha256']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
