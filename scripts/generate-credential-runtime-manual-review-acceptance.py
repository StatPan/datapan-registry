#!/usr/bin/env python3
"""Generate or check the credential runtime manual-review acceptance boundary."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating manual-review acceptance") from exc


DEFAULT_HANDOFF = pathlib.Path("reports/credential-runtime-review-handoff.json")
DEFAULT_COMPATIBILITY = pathlib.Path("reports/release-consumer-compatibility.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-manual-review-acceptance.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/credential-runtime-manual-review-acceptance.json")
SCHEMA_VERSION = "datapan.credential-runtime-manual-review-acceptance.v1"
ACCEPTANCE_TICKET = 389


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def bool_value(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def validate_input_invariants(handoff: dict[str, Any], compatibility: dict[str, Any]) -> None:
    handoff_summary = as_dict(handoff.get("summary"), "handoff.summary")
    handoff_boundary = as_dict(handoff.get("release_boundary"), "handoff.release_boundary")
    compatibility_risk = as_dict(compatibility.get("runtime_risk_evidence"), "compatibility.runtime_risk_evidence")
    if handoff_summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("manual-review acceptance boundary requires default_ci_requires_credentials=false")
    if handoff_summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("manual-review acceptance boundary requires checked_in_secrets_allowed=false")
    if handoff_boundary.get("canonical_registry_compatible") is not True:
        raise ValueError("manual-review acceptance boundary must preserve canonical registry compatibility")
    if compatibility_risk.get("credential_handoff_status") != handoff_summary.get("handoff_status"):
        raise ValueError("compatibility handoff status must match credential review handoff")
    if compatibility_risk.get("credential_handoff_pending_review_sources") != handoff_summary.get(
        "pending_review_sources"
    ):
        raise ValueError("compatibility pending review count must match credential review handoff")


def build_report(handoff: dict[str, Any], compatibility: dict[str, Any]) -> dict[str, Any]:
    validate_input_invariants(handoff, compatibility)
    handoff_summary = as_dict(handoff.get("summary"), "handoff.summary")
    handoff_boundary = as_dict(handoff.get("release_boundary"), "handoff.release_boundary")
    compatibility_risk = as_dict(compatibility.get("runtime_risk_evidence"), "compatibility.runtime_risk_evidence")
    accepted = False
    manual_review_required = bool_value(
        compatibility_risk.get("manual_review_required"),
        "compatibility.runtime_risk_evidence.manual_review_required",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": handoff.get("generated_at"),
        "goal_issue": 344,
        "acceptance_ticket": ACCEPTANCE_TICKET,
        "handoff_ticket": handoff.get("handoff_ticket"),
        "consumer_compatibility_ticket": 387,
        "provider": "datapan-registry",
        "inputs": {
            "credential_runtime_review_handoff": DEFAULT_HANDOFF.as_posix(),
            "release_consumer_compatibility": DEFAULT_COMPATIBILITY.as_posix(),
        },
        "summary": {
            "accepted": accepted,
            "acceptance_status": "not_accepted",
            "acceptance_decision": "blocked_until_explicit_manual_review_acceptance",
            "credential_handoff_status": handoff_summary.get("handoff_status"),
            "pending_review_sources": handoff_summary.get("pending_review_sources"),
            "reviewed_receipts_checked_in": handoff_summary.get("reviewed_receipts_checked_in"),
            "relief_eligible_sources": handoff_summary.get("relief_eligible_sources"),
            "global_manual_review_relief_allowed": handoff_summary.get("global_manual_review_relief_allowed"),
            "compatibility_manual_review_required": manual_review_required,
            "compatibility_effect": compatibility_risk.get("compatibility_effect"),
            "default_ci_requires_credentials": False,
            "checked_in_secrets_allowed": False,
        },
        "release_boundary": {
            "canonical_registry_compatible": True,
            "manual_review_required": manual_review_required,
            "manual_review_release_boundary_accepted": accepted,
            "live_evidence_claim": handoff_boundary.get("live_evidence_claim"),
            "consumer_compatibility_effect": compatibility_risk.get("compatibility_effect"),
            "goal_completion_effect": "goal_remains_open_until_reviewed_receipts_or_explicit_acceptance",
        },
        "required_acceptance_evidence": [
            {
                "id": "reviewer_identity",
                "required": True,
                "description": "A named accountable reviewer or release owner must accept the manual-review boundary.",
            },
            {
                "id": "reviewed_handoff_packet",
                "required": True,
                "description": "The reviewer must inspect the current credential runtime review handoff and pending source list.",
            },
            {
                "id": "consumer_compatibility_decision",
                "required": True,
                "description": "The acceptance must state the release is manual-review only for affected consumers.",
            },
            {
                "id": "expiry_or_revalidation_trigger",
                "required": True,
                "description": "The acceptance must define when it expires or must be revalidated.",
            },
            {
                "id": "no_secret_material",
                "required": True,
                "description": "The acceptance record must not include credentials, hashes, headers, or secret-derived values.",
            },
        ],
    }


def validate_schema(report: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("; ".join(rendered))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", default=DEFAULT_HANDOFF, type=pathlib.Path)
    parser.add_argument("--compatibility", default=DEFAULT_COMPATIBILITY, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when the checked-in acceptance boundary is stale")
    args = parser.parse_args()

    try:
        report = build_report(load_json(args.handoff), load_json(args.compatibility))
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate credential runtime manual-review acceptance: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing credential runtime manual-review acceptance", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale credential runtime manual-review acceptance; "
                "run `python3 scripts/generate-credential-runtime-manual-review-acceptance.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (accepted={report['summary']['accepted']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (accepted={report['summary']['accepted']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
