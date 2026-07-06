#!/usr/bin/env python3
"""Validate redacted credential-gated runtime evidence receipts."""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Any

try:
    import credential_runtime_receipts as receipts
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing credential runtime receipt helpers") from exc


DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-receipt.v1.schema.json")
DEFAULT_POLICY = pathlib.Path("reports/credential-runtime-evidence-policy.json")


def source_policy(policy: dict[str, Any], source_id: str) -> dict[str, Any]:
    for raw_source in receipts.as_list(policy.get("sources"), "policy.sources"):
        source = receipts.as_dict(raw_source, "policy.sources[]")
        if source.get("source_id") == source_id:
            return source
    raise ValueError(f"receipt source {source_id} is not present in credential runtime policy")


def validate_policy_contract(policy: dict[str, Any]) -> None:
    contract = receipts.as_dict(policy.get("operator_contract"), "policy.operator_contract")
    expected = {
        "receipt_schema": "schemas/datapan.credential-runtime-receipt.v1.schema.json",
        "receipt_validator": "scripts/validate-credential-runtime-receipts.py",
        "receipt_validation_command": "python3 scripts/validate-credential-runtime-receipts.py",
        "reviewed_receipt_glob": receipts.DEFAULT_RECEIPT_GLOB,
        "review_required_for_checked_in_receipts": True,
    }
    for key, value in expected.items():
        if contract.get(key) != value:
            raise ValueError(f"policy.operator_contract.{key} expected {value}, got {contract.get(key)}")
    if (
        set(
            receipts.as_list(
                contract.get("allowed_checked_in_review_states"),
                "policy.operator_contract.allowed_checked_in_review_states",
            )
        )
        != receipts.REVIEW_STATES
    ):
        raise ValueError("policy.operator_contract.allowed_checked_in_review_states must match validator review states")
    if (
        set(
            receipts.as_list(
                contract.get("relief_eligible_review_states"),
                "policy.operator_contract.relief_eligible_review_states",
            )
        )
        != receipts.RELIEF_ELIGIBLE_REVIEW_STATES
    ):
        raise ValueError("policy.operator_contract.relief_eligible_review_states must match validator relief states")


def valid_sample(policy: dict[str, Any]) -> dict[str, Any]:
    source = receipts.as_dict(receipts.as_list(policy.get("sources"), "policy.sources")[0], "policy.sources[0]")
    return {
        "schema_version": receipts.SCHEMA_VERSION,
        "generated_at": "2026-07-07T00:00:00Z",
        "source_id": source["source_id"],
        "provider": source["provider"],
        "candidate_batch": source["candidate_batch"],
        "runtime_evidence_plan": source["runtime_evidence_plan"],
        "credential_configured": True,
        "credential_envs": source["credential_envs"],
        "bounded": True,
        "execution": {
            "started_at": "2026-07-07T00:00:00Z",
            "finished_at": "2026-07-07T00:00:01Z",
            "duration_ms": 1000,
            "request_count": 1,
        },
        "outcome": "manual_review_required",
        "error_class": "credential",
        "response_metadata": {
            "http_status": 401,
            "provider_code": "credential_required",
        },
        "review": {
            "state": "reviewed_rejected",
            "reviewed_at": "2026-07-07T00:00:02Z",
            "reviewer": "release-operator",
            "decision": "keeps_manual_review_boundary",
            "reason": "sample receipt exercises reviewed intake without compatibility relief",
        },
        "redaction": {
            "secret_values_present": False,
            "secret_hashes_present": False,
            "forbidden_fields_checked": sorted(receipts.FORBIDDEN_KEYS),
        },
    }


def run_self_tests(schema: dict[str, Any], policy: dict[str, Any]) -> None:
    sample = valid_sample(policy)
    sources_by_id = receipts.source_lookup(
        [receipts.as_dict(item, "policy.sources[]") for item in receipts.as_list(policy.get("sources"), "policy.sources")]
    )
    receipts.validate_receipt(sample, schema, sources_by_id, "<self-test-valid>", require_review=True)
    invalid_key = dict(sample)
    invalid_key["credential_value"] = "abc123"
    try:
        receipts.validate_receipt(invalid_key, schema, sources_by_id, "<self-test-forbidden-key>", require_review=True)
    except ValueError:
        pass
    else:
        raise ValueError("self-test failed: forbidden credential_value was accepted")

    invalid_pattern = dict(sample)
    invalid_pattern["response_metadata"] = {"header": "Authorization: Bearer abcdef0123456789"}
    try:
        receipts.validate_receipt(
            invalid_pattern,
            schema,
            sources_by_id,
            "<self-test-forbidden-pattern>",
            require_review=True,
        )
    except ValueError:
        pass
    else:
        raise ValueError("self-test failed: bearer token pattern was accepted")

    unreviewed = dict(sample)
    unreviewed.pop("review")
    try:
        receipts.validate_receipt(unreviewed, schema, sources_by_id, "<self-test-missing-review>", require_review=True)
    except ValueError:
        pass
    else:
        raise ValueError("self-test failed: checked-in receipt without review was accepted")
    receipts.validate_receipt(unreviewed, schema, sources_by_id, "<self-test-staged-unreviewed>", require_review=False)


def default_receipts() -> list[pathlib.Path]:
    return sorted(pathlib.Path().glob(receipts.DEFAULT_RECEIPT_GLOB))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--policy", default=DEFAULT_POLICY, type=pathlib.Path)
    parser.add_argument(
        "--allow-unreviewed",
        action="store_true",
        help="allow local staged receipts without review metadata; checked-in default receipts still require review",
    )
    parser.add_argument("receipt_paths", nargs="*", type=pathlib.Path)
    args = parser.parse_args()

    try:
        schema = receipts.load_json(args.schema)
        policy = receipts.load_json(args.policy)
        validate_policy_contract(policy)
        run_self_tests(schema, policy)
        sources_by_id = receipts.source_lookup(
            [
                receipts.as_dict(item, "policy.sources[]")
                for item in receipts.as_list(policy.get("sources"), "policy.sources")
            ]
        )
        receipt_paths = args.receipt_paths or default_receipts()
        for receipt_path in receipt_paths:
            receipts.validate_receipt(
                receipts.load_json(receipt_path),
                schema,
                sources_by_id,
                receipt_path.as_posix(),
                require_review=not args.allow_unreviewed,
            )
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL credential runtime receipts: {exc}", file=sys.stderr)
        return 1

    review_mode = "optional" if args.allow_unreviewed else "required"
    print(f"ok credential runtime receipts (receipts={len(receipt_paths)}, review={review_mode}, self_tests=5)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
