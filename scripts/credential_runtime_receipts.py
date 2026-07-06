"""Shared helpers for redacted credential runtime receipts."""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import jsonschema


SCHEMA_VERSION = "datapan.credential-runtime-receipt.v1"
DEFAULT_RECEIPT_GLOB = "reports/credential-runtime-receipts/*-credentialed-receipt.json"
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
CREDENTIAL_ENVS: dict[str, list[str]] = {
    "data_go_kr": ["DATAPAN_DATA_GO_KR_SERVICE_KEY"],
    "ecos": ["DATAPAN_ECOS_API_KEY"],
    "kosis": ["DATAPAN_KOSIS_API_KEY"],
    "open_assembly": ["DATAPAN_OPEN_ASSEMBLY_API_KEY"],
    "seoul_open_data": ["DATAPAN_SEOUL_OPEN_DATA_API_KEY"],
}


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


def source_lookup(sources: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for source in sources:
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("receipt source lookup requires non-empty source_id")
        lookup[source_id] = source
    return lookup


def validate_receipt(
    receipt: dict[str, Any],
    schema: dict[str, Any],
    sources_by_id: dict[str, dict[str, Any]],
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
    source = sources_by_id.get(source_id)
    if source is None:
        raise ValueError(f"{label}: receipt source {source_id} is not present in credential runtime policy")
    if receipt.get("candidate_batch") != source.get("candidate_batch"):
        raise ValueError(f"{label}: candidate_batch must match credential runtime policy")
    if receipt.get("runtime_evidence_plan") != source.get("runtime_evidence_plan"):
        raise ValueError(f"{label}: runtime_evidence_plan must match credential runtime policy")
    if receipt.get("credential_envs") != source.get("credential_envs"):
        raise ValueError(f"{label}: credential_envs must match credential runtime policy")


def receipt_is_relief_eligible(receipt: dict[str, Any]) -> bool:
    review = as_dict(receipt.get("review"), "receipt.review")
    return (
        review.get("state") in RELIEF_ELIGIBLE_REVIEW_STATES
        and review.get("decision") == "allows_manual_review_reduction"
        and receipt.get("outcome") == "verified"
        and receipt.get("error_class") == "none"
    )


def discover_reviewed_receipts(
    *,
    receipt_glob: str,
    schema_path: pathlib.Path,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    schema = load_json(schema_path)
    sources_by_id = source_lookup(sources)
    receipt_paths = sorted(pathlib.Path().glob(receipt_glob))
    relief_eligible_sources: set[str] = set()
    reviewed_sources: set[str] = set()
    receipt_records: list[dict[str, Any]] = []
    for receipt_path in receipt_paths:
        receipt = load_json(receipt_path)
        label = receipt_path.as_posix()
        validate_receipt(receipt, schema, sources_by_id, label, require_review=True)
        source_id = str(receipt["source_id"])
        reviewed_sources.add(source_id)
        relief_eligible = receipt_is_relief_eligible(receipt)
        if relief_eligible:
            relief_eligible_sources.add(source_id)
        receipt_records.append(
            {
                "path": label,
                "source_id": source_id,
                "review_state": as_dict(receipt.get("review"), f"{label}.review").get("state"),
                "outcome": receipt.get("outcome"),
                "relief_eligible": relief_eligible,
            }
        )

    all_source_ids = set(sources_by_id)
    receipt_count = len(receipt_records)
    all_sources_relief_eligible = bool(all_source_ids) and relief_eligible_sources == all_source_ids
    if receipt_count == 0:
        intake_status = "defined_no_reviewed_receipts"
    elif all_sources_relief_eligible:
        intake_status = "reviewed_receipts_relief_eligible"
    else:
        intake_status = "reviewed_receipts_present"
    return {
        "receipt_records": receipt_records,
        "receipt_count": receipt_count,
        "reviewed_sources": sorted(reviewed_sources),
        "relief_eligible_sources": sorted(relief_eligible_sources),
        "receipt_present": receipt_count > 0,
        "receipt_validated": receipt_count > 0,
        "receipt_reviewed": bool(reviewed_sources),
        "receipt_relief_eligible": all_sources_relief_eligible,
        "manual_review_reduction_allowed": all_sources_relief_eligible,
        "intake_status": intake_status,
        "relief_gate_status": (
            "allowed_by_reviewed_validated_credential_runtime_receipts"
            if all_sources_relief_eligible
            else "blocked_until_reviewed_validated_credential_runtime_receipts_exist"
        ),
    }
