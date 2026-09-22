#!/usr/bin/env python3
"""Validate the Registry completeness-proof policy and deterministic fixtures."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY = ROOT / "policy/completeness-proof.json"
POLICY_SCHEMA = ROOT / "schemas/datapan.completeness-proof-policy.v1.schema.json"
PROOF_SCHEMA = ROOT / "schemas/datapan.completeness-proof.v1.schema.json"
VALID_FIXTURE = ROOT / "fixtures/completeness-proof/valid-complete.json"
INVALID_FIXTURES = ROOT / "fixtures/completeness-proof/invalid-cases.json"

EXPECTED_SCOPE_KINDS = {
    "api_catalog_metadata": "authoritative_catalog_identity_set",
    "api_operation_manifest": "authoritative_operation_identity_set",
    "file_dataset": "authoritative_file_dataset_identity_set",
    "curated_payload_snapshot": "declared_snapshot_identity_set",
    "payload_rows": "authoritative_row_identity_set",
}
EXPECTED_CLAIM_RULES = {
    "complete": {
        "authority_available",
        "authoritative_denominator",
        "source_digest_bound",
        "identity_algorithm_bound",
        "exact_reconciliation",
        "freshness_current",
        "publication_read_back_when_published",
    },
    "current": {"complete", "freshness_current"},
    "updated": {
        "current",
        "import_durability_proven",
        "immutable_publication_bound",
        "consumer_read_back_proven",
    },
}
EXPECTED_DEPENDENCIES = {
    ("StatPan/datapan-registry", "#604"),
    ("StatPan/datapan-registry", "#605"),
    ("StatPan/datapan-registry", "#545"),
    ("StatPan/datapan-registry", "#609"),
    ("StatPan/datapan-registry", "#597"),
    ("StatPan/datapan-registry", "#589"),
    ("StatPan/datapan-registry", "#592"),
    ("StatPan/datapan-data", "StatPan/datapan-data#1190"),
    ("StatPan/datapan-health", "StatPan/datapan-health#33"),
}


def load_object(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_schema(value: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_object(schema_path)
    errors = sorted(
        jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        ).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
            for error in errors
        )
        raise ValueError(f"{schema_path.name}: {details}")


def parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_policy(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    validate_schema(policy, POLICY_SCHEMA)

    scope_items = policy["scope_kinds"]
    scope_by_kind = {item["kind"]: item for item in scope_items}
    if len(scope_by_kind) != len(scope_items):
        raise ValueError("policy scope kinds must be unique")
    if set(scope_by_kind) != set(EXPECTED_SCOPE_KINDS):
        raise ValueError("policy must distinguish all five public-data scope kinds")
    for kind, denominator_type in EXPECTED_SCOPE_KINDS.items():
        if scope_by_kind[kind]["denominator_type"] != denominator_type:
            raise ValueError(f"{kind} denominator type must be {denominator_type}")

    for claim, predicates in EXPECTED_CLAIM_RULES.items():
        if set(policy["claim_rules"][claim]) != predicates:
            raise ValueError(f"policy {claim} predicates drifted")

    non_complete = policy["non_complete_states"]
    if set(non_complete["states"]) != {"partial", "unknown", "blocked"}:
        raise ValueError("policy non-complete states must be partial, unknown, and blocked")

    dependencies = {
        (item["owner"], item["ticket"]) for item in policy["reusable_dependencies"]
    }
    if dependencies != EXPECTED_DEPENDENCIES:
        missing = sorted(EXPECTED_DEPENDENCIES - dependencies)
        extra = sorted(dependencies - EXPECTED_DEPENDENCIES)
        raise ValueError(f"policy dependency ownership drift: missing={missing}, extra={extra}")
    return scope_by_kind


def validate_proof(proof: dict[str, Any], policy: dict[str, Any]) -> None:
    validate_schema(proof, PROOF_SCHEMA)
    scope_by_kind = validate_policy(policy)

    policy_binding = proof["policy"]
    if policy_binding != {
        "id": policy["policy_id"],
        "schema_version": policy["schema_version"],
        "sha256": sha256(POLICY),
    }:
        raise ValueError("proof policy binding does not match the checked-in policy")

    scope = proof["scope"]
    scope_policy = scope_by_kind[scope["resource_kind"]]
    denominator = proof["denominator"]
    if denominator["type"] != scope_policy["denominator_type"]:
        raise ValueError("scope kind and denominator type are conflated")
    authority = scope["authority"]
    if authority["state"] == "available":
        if authority["owner"] != scope_policy["evidence_owner"]:
            raise ValueError("authority owner does not match the scope policy")
        if authority["evidence"]["sha256"] != proof["source_snapshot"]["sha256"]:
            raise ValueError("authority evidence digest does not match the source snapshot")
    if denominator["bound_source_sha256"] != proof["source_snapshot"]["sha256"]:
        raise ValueError("denominator source digest does not match the source snapshot")
    if denominator["identity_algorithm"] != scope["identity_algorithm"]:
        raise ValueError("denominator identity algorithm does not match the declared scope")

    reconciliation = proof["reconciliation"]
    if denominator["value"] is not None and reconciliation["expected"] != denominator["value"]:
        raise ValueError("reconciliation expected count does not match the denominator")
    if reconciliation["accepted"] + reconciliation["missing"] + reconciliation["rejected"] != reconciliation["expected"]:
        raise ValueError("reconciliation counts do not balance")

    freshness = proof["freshness"]
    if freshness["max_age_seconds"] != scope_policy["max_age_seconds"]:
        raise ValueError("freshness maximum age does not match the scope policy")
    if freshness["evidence_observed_at"] != proof["source_snapshot"]["observed_at"]:
        raise ValueError("freshness observation does not match the source snapshot")
    observed_at = parse_time(freshness["evidence_observed_at"], "freshness.evidence_observed_at")
    as_of = parse_time(freshness["as_of"], "freshness.as_of")
    age_seconds = (as_of - observed_at).total_seconds()
    if age_seconds < 0:
        raise ValueError("freshness evidence cannot be observed in the future")
    expected_freshness = "current" if age_seconds <= freshness["max_age_seconds"] else "stale"
    if freshness["state"] != expected_freshness:
        raise ValueError(f"freshness state must be {expected_freshness}")

    publication = proof["publication"]
    read_back = proof["consumer_read_back"]
    if publication["state"] == "published":
        if read_back["state"] != "proven":
            raise ValueError("published proof requires consumer read-back")
        if read_back["artifact_sha256"] != publication["artifact"]["sha256"]:
            raise ValueError("consumer read-back digest does not match the published artifact")
    elif read_back["state"] == "proven":
        raise ValueError("consumer read-back cannot be proven without a published artifact")

    exact_reconciliation = (
        denominator["value"] is not None
        and reconciliation["expected"] == denominator["value"]
        and reconciliation["accepted"] == reconciliation["expected"]
        and all(reconciliation[key] == 0 for key in ("missing", "extra", "duplicate", "rejected"))
    )
    complete_eligible = (
        scope["authority"]["state"] == "available"
        and denominator["type"] not in {"local_observed_identity_set", "unknown"}
        and exact_reconciliation
        and freshness["state"] == "current"
        and (publication["state"] != "published" or read_back["state"] == "proven")
    )
    state = proof["proof_state"]
    claims = proof["claims"]
    missing_evidence = proof["missing_evidence"]

    if state == "complete":
        if not complete_eligible:
            raise ValueError("complete proof does not satisfy fail-closed predicates")
        if missing_evidence:
            raise ValueError("complete proof cannot retain missing evidence")
    else:
        if not missing_evidence:
            raise ValueError("non-complete proofs require missing_evidence with owner and ticket")
        if any(claims.values()):
            raise ValueError("percentage or partial evidence cannot promote a non-complete claim")

    expected_complete = state == "complete" and complete_eligible
    expected_current = expected_complete and freshness["state"] == "current"
    expected_updated = (
        expected_current
        and proof["import_durability"]["state"] == "proven"
        and publication["state"] == "published"
        and read_back["state"] == "proven"
    )
    expected_claims = {
        "complete": expected_complete,
        "current": expected_current,
        "updated": expected_updated,
    }
    if claims != expected_claims:
        raise ValueError(f"claims do not match proof predicates: expected {expected_claims}")

    declared_dependencies = {
        (item["owner"], item["ticket"]) for item in proof["dependencies"]
    }
    if len(declared_dependencies) != len(proof["dependencies"]):
        raise ValueError("proof dependencies must be unique by owner and ticket")
    unknown_dependencies = declared_dependencies - EXPECTED_DEPENDENCIES
    if unknown_dependencies:
        raise ValueError(f"proof contains unregistered dependencies: {sorted(unknown_dependencies)}")


def apply_mutation(value: dict[str, Any], pointer: str, replacement: Any) -> None:
    if not pointer.startswith("/"):
        raise ValueError(f"mutation path must be an absolute JSON pointer: {pointer}")
    target: Any = value
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    final = parts[-1]
    if isinstance(target, list):
        target[int(final)] = replacement
    else:
        target[final] = replacement


def fixture_cases() -> list[dict[str, Any]]:
    value = json.loads(INVALID_FIXTURES.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("invalid-cases.json must contain an array of objects")
    return value


def check_fixtures() -> int:
    policy = load_object(POLICY)
    valid = load_object(VALID_FIXTURE)
    validate_proof(valid, policy)

    cases = fixture_cases()
    for case in cases:
        candidate = copy.deepcopy(valid)
        for mutation in case["mutations"]:
            apply_mutation(candidate, mutation["path"], mutation["value"])
        try:
            validate_proof(candidate, policy)
        except ValueError as exc:
            if case["expected_error"] not in str(exc):
                raise ValueError(
                    f"{case['case_id']} failed with unexpected error: {exc}"
                ) from exc
        else:
            raise ValueError(f"{case['case_id']} unexpectedly passed")
    return len(cases)


def self_test() -> None:
    policy = load_object(POLICY)
    base = load_object(VALID_FIXTURE)
    for state in ("partial", "unknown", "blocked"):
        candidate = copy.deepcopy(base)
        candidate["proof_state"] = state
        candidate["claims"] = {"complete": False, "current": False, "updated": False}
        candidate["missing_evidence"] = [
            {
                "code": f"{state}_evidence",
                "owner": "StatPan/datapan-registry",
                "ticket": "#631",
            }
        ]
        validate_proof(candidate, policy)

    promoted = copy.deepcopy(base)
    promoted["proof_state"] = "partial"
    promoted["missing_evidence"] = [
        {
            "code": "partial_evidence",
            "owner": "StatPan/datapan-registry",
            "ticket": "#631",
        }
    ]
    try:
        validate_proof(promoted, policy)
    except ValueError as exc:
        if "cannot promote" not in str(exc):
            raise
    else:
        raise ValueError("partial evidence unexpectedly promoted complete/current/updated claims")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate checked-in policy and fixtures")
    mode.add_argument("--self-test", action="store_true", help="exercise non-complete state and claim guards")
    args = parser.parse_args()

    try:
        if args.self_test:
            self_test()
            print("ok completeness proof self-test (partial, unknown, blocked, percentage promotion)")
        else:
            invalid_count = check_fixtures()
            print(f"ok completeness proof contract (invalid_fixtures={invalid_count})")
    except Exception as exc:  # noqa: BLE001 - release validation must report the failed invariant
        print(f"FAIL completeness proof contract: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
