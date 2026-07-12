#!/usr/bin/env python3
"""Generate or check the credential runtime manual-review acceptance boundary."""

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
    raise SystemExit("missing dependency: install jsonschema before generating manual-review acceptance") from exc


DEFAULT_HANDOFF = pathlib.Path("reports/credential-runtime-review-handoff.json")
DEFAULT_COMPATIBILITY = pathlib.Path("reports/release-consumer-compatibility.json")
DEFAULT_DECISION = pathlib.Path("reports/credential-runtime-manual-review-decision.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-manual-review-acceptance.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/credential-runtime-manual-review-acceptance.json")
SCHEMA_VERSION = "datapan.credential-runtime-manual-review-acceptance.v1"
ACCEPTANCE_TICKET = 389
DECISION_TICKET = 391
RELEASE_EVIDENCE_REFRESH_COMMAND = "python3 scripts/refresh-release-ledger-evidence.py --write --max-iterations 5"
RELEASE_EVIDENCE_CHECK_COMMAND = "python3 scripts/refresh-release-ledger-evidence.py --check"
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


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def validate_input_invariants(handoff: dict[str, Any], compatibility: dict[str, Any], decision: dict[str, Any]) -> None:
    handoff_summary = as_dict(handoff.get("summary"), "handoff.summary")
    handoff_boundary = as_dict(handoff.get("release_boundary"), "handoff.release_boundary")
    compatibility_risk = as_dict(compatibility.get("runtime_risk_evidence"), "compatibility.runtime_risk_evidence")
    decision_summary = as_dict(decision.get("summary"), "decision.summary")
    decision_body = as_dict(decision.get("decision"), "decision.decision")
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
    if decision_summary.get("accepted") != decision_body.get("accepted"):
        raise ValueError("manual-review decision summary.accepted must match decision.accepted")
    if decision_summary.get("decision_status") != decision_body.get("decision_status"):
        raise ValueError("manual-review decision summary status must match decision status")
    if decision_summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("manual-review decision must remain secret-free in default CI")
    if decision_summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("manual-review decision must not allow checked-in secrets")
    validate_secret_free(decision)


def validate_decision_state(
    decision: dict[str, Any],
    *,
    handoff_path: pathlib.Path,
    compatibility_path: pathlib.Path,
) -> None:
    decision_body = as_dict(decision.get("decision"), "decision.decision")
    accepted = bool_value(decision_body.get("accepted"), "decision.accepted")
    if not accepted:
        if decision_body.get("decision_status") != "not_asserted":
            raise ValueError("unaccepted manual-review decision must use decision_status=not_asserted")
        for nullable_key in ("reviewer", "reviewed_at", "handoff_sha256", "compatibility_sha256", "expires_at"):
            if decision_body.get(nullable_key) is not None:
                raise ValueError(f"unaccepted manual-review decision must keep {nullable_key}=null")
        if decision_body.get("reason") != "manual_review_acceptance_not_asserted":
            raise ValueError("unaccepted manual-review decision has an unexpected reason")
        if as_list(decision_body.get("revalidation_triggers"), "decision.revalidation_triggers"):
            raise ValueError("unaccepted manual-review decision must not define revalidation triggers")
        return

    if decision_body.get("decision_status") != "accepted":
        raise ValueError("accepted manual-review decision must use decision_status=accepted")
    for required_key in ("reviewer", "reviewed_at", "reason", "expires_at"):
        value = decision_body.get(required_key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"accepted manual-review decision requires decision.{required_key}")
    if decision_body.get("handoff_sha256") != file_sha256(handoff_path):
        raise ValueError("accepted manual-review decision handoff_sha256 does not match current handoff")
    if decision_body.get("compatibility_sha256") != compatibility_binding_sha256(load_json(compatibility_path)):
        raise ValueError("accepted manual-review decision compatibility_sha256 does not match current compatibility")
    if not as_list(decision_body.get("revalidation_triggers"), "decision.revalidation_triggers"):
        raise ValueError("accepted manual-review decision requires revalidation triggers")


def build_acceptance_routing(
    *,
    accepted: bool,
    acceptance_status: str,
    manual_review_required: bool,
    required_acceptance_evidence: list[dict[str, Any]],
    compatibility_effect: object,
    goal_completion_effect: str,
    decision_path: pathlib.Path,
) -> dict[str, Any]:
    first_safe_action = (
        "refresh_release_evidence_after_manual_review_acceptance"
        if accepted
        else "assert_manual_review_decision_with_required_evidence"
    )
    return {
        "parent_goal_issue": 344,
        "accepted": accepted,
        "acceptance_status": acceptance_status,
        "manual_review_required": manual_review_required,
        "manual_review_release_boundary_accepted": accepted,
        "first_safe_action": first_safe_action,
        "requires_accountable_reviewer": True,
        "required_acceptance_evidence_ids": [
            str(item["id"]) for item in required_acceptance_evidence
        ],
        "decision_intake_path": decision_path.as_posix(),
        "decision_validation_command": "python3 scripts/validate-credential-runtime-manual-review-decision.py",
        "acceptance_generation_command": "python3 scripts/generate-credential-runtime-manual-review-acceptance.py",
        "acceptance_check_command": "python3 scripts/generate-credential-runtime-manual-review-acceptance.py --check",
        "acceptance_packet_check_command": "python3 scripts/generate-credential-runtime-manual-review-acceptance-packet.py --check",
        "post_decision_refresh_commands": [
            RELEASE_EVIDENCE_REFRESH_COMMAND,
            RELEASE_EVIDENCE_CHECK_COMMAND,
        ],
        "consumer_compatibility_effect": compatibility_effect,
        "goal_completion_effect": goal_completion_effect,
        "goal_closure_allowed": False,
        "default_ci_requires_credentials": False,
        "checked_in_secrets_allowed": False,
        "routing_note": (
            "Manual-review acceptance can route release evidence only after an accountable "
            "reviewer asserts the required evidence; #344 remains open until finish "
            "preflight and completion audit allow closure."
        ),
    }


def build_report(
    handoff: dict[str, Any],
    compatibility: dict[str, Any],
    decision: dict[str, Any],
    *,
    handoff_path: pathlib.Path = DEFAULT_HANDOFF,
    compatibility_path: pathlib.Path = DEFAULT_COMPATIBILITY,
    decision_path: pathlib.Path = DEFAULT_DECISION,
) -> dict[str, Any]:
    validate_input_invariants(handoff, compatibility, decision)
    validate_decision_state(decision, handoff_path=handoff_path, compatibility_path=compatibility_path)
    handoff_summary = as_dict(handoff.get("summary"), "handoff.summary")
    handoff_boundary = as_dict(handoff.get("release_boundary"), "handoff.release_boundary")
    compatibility_risk = as_dict(compatibility.get("runtime_risk_evidence"), "compatibility.runtime_risk_evidence")
    decision_summary = as_dict(decision.get("summary"), "decision.summary")
    decision_body = as_dict(decision.get("decision"), "decision.decision")
    accepted = bool_value(decision_summary.get("accepted"), "decision.summary.accepted")
    manual_review_required = bool_value(
        compatibility_risk.get("manual_review_required"),
        "compatibility.runtime_risk_evidence.manual_review_required",
    )
    acceptance_status = "accepted" if accepted else "not_accepted"
    acceptance_decision = (
        "accepted_manual_review_release_boundary"
        if accepted
        else "blocked_until_explicit_manual_review_acceptance"
    )
    goal_completion_effect = (
        "manual_review_boundary_accepted_for_release"
        if accepted
        else "goal_remains_open_until_reviewed_receipts_or_explicit_acceptance"
    )
    required_acceptance_evidence = [
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
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": handoff.get("generated_at"),
        "goal_issue": 344,
        "acceptance_ticket": ACCEPTANCE_TICKET,
        "decision_ticket": DECISION_TICKET,
        "handoff_ticket": handoff.get("handoff_ticket"),
        "consumer_compatibility_ticket": 387,
        "provider": "datapan-registry",
        "inputs": {
            "credential_runtime_review_handoff": handoff_path.as_posix(),
            "release_consumer_compatibility": compatibility_path.as_posix(),
            "credential_runtime_manual_review_decision": decision_path.as_posix(),
        },
        "summary": {
            "accepted": accepted,
            "acceptance_status": acceptance_status,
            "acceptance_decision": acceptance_decision,
            "decision_status": decision_summary.get("decision_status"),
            "decision_reason": decision_body.get("reason"),
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
            "goal_completion_effect": goal_completion_effect,
        },
        "acceptance_routing": build_acceptance_routing(
            accepted=accepted,
            acceptance_status=acceptance_status,
            manual_review_required=manual_review_required,
            required_acceptance_evidence=required_acceptance_evidence,
            compatibility_effect=compatibility_risk.get("compatibility_effect"),
            goal_completion_effect=goal_completion_effect,
            decision_path=decision_path,
        ),
        "required_acceptance_evidence": required_acceptance_evidence,
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
    parser.add_argument("--decision", default=DEFAULT_DECISION, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when the checked-in acceptance boundary is stale")
    args = parser.parse_args()

    try:
        report = build_report(
            load_json(args.handoff),
            load_json(args.compatibility),
            load_json(args.decision),
            handoff_path=args.handoff,
            compatibility_path=args.compatibility,
            decision_path=args.decision,
        )
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
