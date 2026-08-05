"""Fail-closed, offline validation for Registry post-publication evidence.

This module deliberately consumes only redacted receipt metadata.  It does not
fetch a public pointer, invoke the Datapan CLI, access Hugging Face, or make a
publication decision.  Those actions belong to their respective operators;
this boundary only decides whether their immutable observed evidence is
internally coherent at one caller-owned time.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema


SCHEMA_VERSION = "datapan.post-publication-admission.v1"
MAX_AGE_SECONDS = 600
REQUIRED_CLI_CHECKS = {"install", "doctor", "journey"}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("post-publication admission input cannot be read") from exc
    if not isinstance(value, dict):
        raise ValueError("post-publication admission input must be an object")
    return value


def canonical_digest(value: dict[str, Any]) -> str:
    projected = copy.deepcopy(value)
    projected.pop("admission_digest", None)
    encoded = json.dumps(projected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


FORMAT_CHECKER = jsonschema.FormatChecker()


@FORMAT_CHECKER.checks("date-time")
def is_rfc3339_datetime(value: object) -> bool:
    try:
        parse_time(value, "date-time")
    except ValueError:
        return False
    return True


def validate_schema(value: dict[str, Any], schema: dict[str, Any]) -> None:
    errors = sorted(
        jsonschema.Draft202012Validator(schema, format_checker=FORMAT_CHECKER).iter_errors(value),
        key=lambda item: list(item.path),
    )
    if errors:
        # Inputs are an untrusted receipt boundary.  Do not render their values
        # (which could contain credential-looking text) in a diagnostic.
        fields = ", ".join(".".join(map(str, error.path)) or "<root>" for error in errors)
        raise ValueError(f"post-publication admission schema validation failed at {fields}")


def require_current(when: datetime, *, earlier: datetime | None, admitted_at: datetime, label: str) -> None:
    if earlier is not None and when < earlier:
        raise ValueError(f"{label} is out of order")
    if when > admitted_at:
        raise ValueError(f"{label} is in the future of caller admission time")
    if (admitted_at - when).total_seconds() > MAX_AGE_SECONDS:
        raise ValueError(f"{label} is stale at caller admission time")


def binding(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} binding is missing")
    return value


def require_equal_binding(actual: object, expected: object, label: str) -> None:
    if binding(actual, label) != binding(expected, label):
        raise ValueError(f"{label} does not match the anonymously verified immutable binding")


def validate_cli_observation(value: object, *, verified_binding: dict[str, Any], earlier: datetime, admitted_at: datetime, label: str) -> datetime:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is missing")
    when = parse_time(value.get("observed_at"), f"{label}.observed_at")
    require_current(when, earlier=earlier, admitted_at=admitted_at, label=f"{label}.observed_at")
    require_equal_binding(value.get("binding"), verified_binding, f"{label}.binding")
    if value.get("outcome") != "verified":
        raise ValueError(f"{label} must be verified")
    checks = value.get("checks")
    if not isinstance(checks, list) or set(checks) != REQUIRED_CLI_CHECKS or len(checks) != len(REQUIRED_CLI_CHECKS):
        raise ValueError(f"{label} must contain exactly install, doctor, and journey checks")
    return when


def validate_admission(value: dict[str, Any], *, schema: dict[str, Any], admitted_at: datetime) -> str:
    validate_schema(value, schema)
    if value.get("admission_digest") != canonical_digest(value):
        raise ValueError("post-publication admission digest does not match canonical bytes")

    anonymous = value["anonymous_verification"]
    anonymous_time = parse_time(anonymous["verified_at"], "anonymous_verification.verified_at")
    require_current(anonymous_time, earlier=None, admitted_at=admitted_at, label="anonymous_verification.verified_at")
    public_binding = binding(anonymous["binding"], "anonymous_verification")

    cli = value["cli_observation"]
    cli_time = parse_time(cli["observed_at"], "cli_observation.observed_at")
    require_current(cli_time, earlier=anonymous_time, admitted_at=admitted_at, label="cli_observation.observed_at")
    require_equal_binding(cli["binding"], public_binding, "cli_observation.binding")

    resolution = value["resolution"]
    outcome = resolution["outcome"]
    if outcome == "accepted":
        validate_cli_observation(cli, verified_binding=public_binding, earlier=anonymous_time, admitted_at=admitted_at, label="cli_observation")
        return "accepted"
    if outcome == "manual_hold":
        if cli.get("outcome") == "verified":
            raise ValueError("manual_hold cannot replace a verified CLI receipt")
        return "manual_hold"

    if cli.get("outcome") == "verified":
        raise ValueError("rollback requires a failed or unknown CLI observation")
    rollback = resolution["rollback"]
    rollback_time = parse_time(rollback["observed_at"], "resolution.rollback.observed_at")
    require_current(rollback_time, earlier=cli_time, admitted_at=admitted_at, label="resolution.rollback.observed_at")
    prior_binding = binding(rollback["prior_binding"], "resolution.rollback.prior")
    if prior_binding["pointer_sha256"] == public_binding["pointer_sha256"] or prior_binding["payload_revision"] == public_binding["payload_revision"]:
        raise ValueError("rollback must observe a distinct prior pointer and payload revision")
    validate_cli_observation(
        resolution["recovery_cli_observation"],
        verified_binding=prior_binding,
        earlier=rollback_time,
        admitted_at=admitted_at,
        label="resolution.recovery_cli_observation",
    )
    return "rolled_back"
