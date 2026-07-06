#!/usr/bin/env python3
"""Validate redacted credential-gated runtime evidence receipts."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before validating credential runtime receipts"
    ) from exc


DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-receipt.v1.schema.json")
DEFAULT_POLICY = pathlib.Path("reports/credential-runtime-evidence-policy.json")
DEFAULT_RECEIPT_GLOB = "reports/credential-runtime-receipts/*-credentialed-receipt.json"
SCHEMA_VERSION = "datapan.credential-runtime-receipt.v1"
REVIEW_STATES = {"reviewed_accepted", "reviewed_rejected"}
RELIEF_ELIGIBLE_REVIEW_STATES = {"reviewed_accepted"}
FORBIDDEN_KEYS = {
    "credential_value",
    "credential_hash",
    "authorization_header",
    "service_key",
    "serviceKey",
    "api_key",
    "apiKey",
    "secret",
}
SECRET_VALUE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"authorization:\s*bearer\s+",
        r"bearer\s+[a-z0-9._~+/=-]{16,}",
        r"serviceKey=",
        r"api[_-]?key=",
    )
]


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def validate_schema(instance: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ValueError(f"{label}: " + "; ".join(rendered))


def scan_redaction(value: object, label: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                raise ValueError(f"{label}: forbidden secret-bearing field {key!r}")
            scan_redaction(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            scan_redaction(child, f"{label}[{index}]")
    elif isinstance(value, str):
        for pattern in SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                raise ValueError(f"{label}: string matches forbidden secret pattern {pattern.pattern!r}")


def source_policy(policy: dict[str, Any], source_id: str) -> dict[str, Any]:
    for raw_source in as_list(policy.get("sources"), "policy.sources"):
        source = as_dict(raw_source, "policy.sources[]")
        if source.get("source_id") == source_id:
            return source
    raise ValueError(f"receipt source {source_id} is not present in credential runtime policy")


def validate_policy_contract(policy: dict[str, Any]) -> None:
    contract = as_dict(policy.get("operator_contract"), "policy.operator_contract")
    expected = {
        "receipt_schema": "schemas/datapan.credential-runtime-receipt.v1.schema.json",
        "receipt_validator": "scripts/validate-credential-runtime-receipts.py",
        "receipt_validation_command": "python3 scripts/validate-credential-runtime-receipts.py",
        "reviewed_receipt_glob": DEFAULT_RECEIPT_GLOB,
        "review_required_for_checked_in_receipts": True,
    }
    for key, value in expected.items():
        if contract.get(key) != value:
            raise ValueError(f"policy.operator_contract.{key} expected {value}, got {contract.get(key)}")
    if set(as_list(contract.get("allowed_checked_in_review_states"), "policy.operator_contract.allowed_checked_in_review_states")) != REVIEW_STATES:
        raise ValueError("policy.operator_contract.allowed_checked_in_review_states must match validator review states")
    if (
        set(
            as_list(
                contract.get("relief_eligible_review_states"),
                "policy.operator_contract.relief_eligible_review_states",
            )
        )
        != RELIEF_ELIGIBLE_REVIEW_STATES
    ):
        raise ValueError("policy.operator_contract.relief_eligible_review_states must match validator relief states")


def validate_review(receipt: dict[str, Any], label: str, *, require_review: bool) -> None:
    review = receipt.get("review")
    if review is None:
        if require_review:
            raise ValueError(f"{label}: checked-in credential runtime receipts require review metadata")
        return
    review_object = as_dict(review, f"{label}.review")
    state = review_object.get("state")
    if state not in REVIEW_STATES:
        raise ValueError(f"{label}.review.state must be one of {sorted(REVIEW_STATES)}")
    decision = review_object.get("decision")
    if state in RELIEF_ELIGIBLE_REVIEW_STATES and receipt.get("outcome") != "verified":
        raise ValueError(f"{label}: relief-eligible reviewed receipts must have outcome=verified")
    if state in RELIEF_ELIGIBLE_REVIEW_STATES and decision != "allows_manual_review_reduction":
        raise ValueError(f"{label}: relief-eligible reviewed receipts must allow manual-review reduction")
    if state == "reviewed_rejected" and decision != "keeps_manual_review_boundary":
        raise ValueError(f"{label}: rejected reviewed receipts must keep the manual-review boundary")


def validate_receipt(
    receipt: dict[str, Any],
    schema: dict[str, Any],
    policy: dict[str, Any],
    label: str,
    *,
    require_review: bool,
) -> None:
    validate_schema(receipt, schema, label)
    scan_redaction(receipt, label)
    validate_review(receipt, label, require_review=require_review)
    source_id = receipt.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError(f"{label}: source_id must be a non-empty string")
    source = source_policy(policy, source_id)
    if receipt.get("candidate_batch") != source.get("candidate_batch"):
        raise ValueError(f"{label}: candidate_batch must match credential runtime policy")
    if receipt.get("runtime_evidence_plan") != source.get("runtime_evidence_plan"):
        raise ValueError(f"{label}: runtime_evidence_plan must match credential runtime policy")
    if receipt.get("credential_envs") != source.get("credential_envs"):
        raise ValueError(f"{label}: credential_envs must match credential runtime policy")


def valid_sample(policy: dict[str, Any]) -> dict[str, Any]:
    source = as_dict(as_list(policy.get("sources"), "policy.sources")[0], "policy.sources[0]")
    return {
        "schema_version": SCHEMA_VERSION,
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
            "forbidden_fields_checked": sorted(FORBIDDEN_KEYS),
        },
    }


def run_self_tests(schema: dict[str, Any], policy: dict[str, Any]) -> None:
    sample = valid_sample(policy)
    validate_receipt(sample, schema, policy, "<self-test-valid>", require_review=True)
    invalid_key = dict(sample)
    invalid_key["credential_value"] = "abc123"
    try:
        validate_receipt(invalid_key, schema, policy, "<self-test-forbidden-key>", require_review=True)
    except ValueError:
        pass
    else:
        raise ValueError("self-test failed: forbidden credential_value was accepted")

    invalid_pattern = dict(sample)
    invalid_pattern["response_metadata"] = {"header": "Authorization: Bearer abcdef0123456789"}
    try:
        validate_receipt(invalid_pattern, schema, policy, "<self-test-forbidden-pattern>", require_review=True)
    except ValueError:
        pass
    else:
        raise ValueError("self-test failed: bearer token pattern was accepted")

    unreviewed = dict(sample)
    unreviewed.pop("review")
    try:
        validate_receipt(unreviewed, schema, policy, "<self-test-missing-review>", require_review=True)
    except ValueError:
        pass
    else:
        raise ValueError("self-test failed: checked-in receipt without review was accepted")
    validate_receipt(unreviewed, schema, policy, "<self-test-staged-unreviewed>", require_review=False)


def default_receipts() -> list[pathlib.Path]:
    return sorted(pathlib.Path().glob(DEFAULT_RECEIPT_GLOB))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--policy", default=DEFAULT_POLICY, type=pathlib.Path)
    parser.add_argument(
        "--allow-unreviewed",
        action="store_true",
        help="allow local staged receipts without review metadata; checked-in default receipts still require review",
    )
    parser.add_argument("receipts", nargs="*", type=pathlib.Path)
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        policy = load_json(args.policy)
        validate_policy_contract(policy)
        run_self_tests(schema, policy)
        receipts = args.receipts or default_receipts()
        for receipt_path in receipts:
            validate_receipt(
                load_json(receipt_path),
                schema,
                policy,
                receipt_path.as_posix(),
                require_review=not args.allow_unreviewed,
            )
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL credential runtime receipts: {exc}", file=sys.stderr)
        return 1

    review_mode = "optional" if args.allow_unreviewed else "required"
    print(f"ok credential runtime receipts (receipts={len(receipts)}, review={review_mode}, self_tests=5)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
