#!/usr/bin/env python3
"""Validate the credential runtime manual-review decision record."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any

from manual_review_evidence_digest import compatibility_binding_sha256

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before validating manual-review decision") from exc


DEFAULT_DECISION = pathlib.Path("reports/credential-runtime-manual-review-decision.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-manual-review-decision.v1.schema.json")
DEFAULT_HANDOFF = pathlib.Path("reports/credential-runtime-review-handoff.json")
DEFAULT_COMPATIBILITY = pathlib.Path("reports/release-consumer-compatibility.json")
DEFAULT_TECHNICAL_REBINDING = pathlib.Path("reports/credential-runtime-manual-review-technical-rebinding.json")
SECRET_VALUE_PATTERNS = [
    re.compile(r"(?i)(service[_-]?key|authorization|bearer\s+[a-z0-9._~+/=-]{16,})"),
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)"),
]


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def validate_schema(record: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(record), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("; ".join(rendered))


def walk_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(walk_strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(walk_strings(item))
        return result
    return []


def validate_secret_free(record: dict[str, Any]) -> None:
    for value in walk_strings(record):
        for pattern in SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                raise ValueError("manual-review decision must not contain secret-like values")


def validate_decision(record: dict[str, Any], *, decision_path: pathlib.Path, handoff_path: pathlib.Path, compatibility_path: pathlib.Path, technical_rebinding_path: pathlib.Path) -> None:
    summary = as_dict(record.get("summary"), "summary")
    decision = as_dict(record.get("decision"), "decision")
    inputs = as_dict(record.get("inputs"), "inputs")
    if inputs.get("credential_runtime_review_handoff") != handoff_path.as_posix():
        raise ValueError("decision input handoff path must match validator handoff path")
    if inputs.get("release_consumer_compatibility") != compatibility_path.as_posix():
        raise ValueError("decision input compatibility path must match validator compatibility path")
    for key in ("accepted", "decision_status"):
        if summary.get(key) != decision.get(key):
            raise ValueError(f"summary.{key} must match decision.{key}")
    if summary.get("manual_review_release_boundary_accepted") != decision.get("accepted"):
        raise ValueError("manual_review_release_boundary_accepted must match decision.accepted")
    if summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("manual-review decision must remain secret-free in default CI")
    if summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("manual-review decision must not allow checked-in secrets")

    accepted = decision.get("accepted")
    if accepted is False:
        if decision.get("decision_status") != "not_asserted":
            raise ValueError("unaccepted decision must use decision_status=not_asserted")
        for nullable_key in ("reviewer", "reviewed_at", "handoff_sha256", "compatibility_sha256", "expires_at"):
            if decision.get(nullable_key) is not None:
                raise ValueError(f"unaccepted decision must keep decision.{nullable_key}=null")
        if decision.get("reason") != "manual_review_acceptance_not_asserted":
            raise ValueError("unaccepted decision reason must be manual_review_acceptance_not_asserted")
        if as_list(decision.get("revalidation_triggers"), "decision.revalidation_triggers"):
            raise ValueError("unaccepted decision must not define revalidation triggers")
        return

    if accepted is not True:
        raise ValueError("decision.accepted must be a boolean")
    if decision.get("decision_status") != "accepted":
        raise ValueError("accepted decision must use decision_status=accepted")
    for required_key in ("reviewer", "reviewed_at", "reason", "expires_at"):
        value = decision.get(required_key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"accepted decision requires non-empty decision.{required_key}")
    if decision.get("handoff_sha256") != file_sha256(handoff_path):
        raise ValueError("accepted decision handoff_sha256 must match the current credential review handoff")
    current_compatibility = compatibility_binding_sha256(load_json(compatibility_path))
    if decision.get("compatibility_sha256") != current_compatibility:
        rebinding = load_json(technical_rebinding_path)
        if rebinding.get("status") != "approved_artifact_only_rebinding" or rebinding.get("old_compatibility_sha256") != decision.get("compatibility_sha256") or rebinding.get("new_compatibility_sha256") != current_compatibility or rebinding.get("decision_sha256") != file_sha256(decision_path):
            raise ValueError("accepted decision compatibility_sha256 must match the current consumer compatibility report")
    triggers = as_list(decision.get("revalidation_triggers"), "decision.revalidation_triggers")
    if not triggers:
        raise ValueError("accepted decision requires at least one revalidation trigger")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decision", nargs="?", default=DEFAULT_DECISION, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--handoff", default=DEFAULT_HANDOFF, type=pathlib.Path)
    parser.add_argument("--compatibility", default=DEFAULT_COMPATIBILITY, type=pathlib.Path)
    parser.add_argument("--technical-rebinding", default=DEFAULT_TECHNICAL_REBINDING, type=pathlib.Path)
    args = parser.parse_args()

    try:
        record = load_json(args.decision)
        validate_schema(record, args.schema)
        validate_secret_free(record)
        validate_decision(record, decision_path=args.decision, handoff_path=args.handoff, compatibility_path=args.compatibility, technical_rebinding_path=args.technical_rebinding)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL {args.decision}: {exc}", file=sys.stderr)
        return 1

    summary = as_dict(record.get("summary"), "summary")
    print(f"ok {args.decision} (accepted={summary.get('accepted')}, status={summary.get('decision_status')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
