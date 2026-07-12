#!/usr/bin/env python3
"""Generate or check the manual-review acceptance operator packet."""

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
    raise SystemExit("missing dependency: install jsonschema before generating manual-review packet") from exc


DEFAULT_DECISION = pathlib.Path("reports/credential-runtime-manual-review-decision.json")
DEFAULT_ACCEPTANCE = pathlib.Path("reports/credential-runtime-manual-review-acceptance.json")
DEFAULT_HANDOFF = pathlib.Path("reports/credential-runtime-review-handoff.json")
DEFAULT_COMPATIBILITY = pathlib.Path("reports/release-consumer-compatibility.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-manual-review-acceptance-packet.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/credential-runtime-manual-review-acceptance-packet.json")
SCHEMA_VERSION = "datapan.credential-runtime-manual-review-acceptance-packet.v1"
PACKET_TICKET = 407
DECISION_TICKET = 391
ACCEPTANCE_TICKET = 389
RELEASE_EVIDENCE_REFRESH_COMMAND = "python3 scripts/refresh-release-ledger-evidence.py --write --max-iterations 5"
RELEASE_EVIDENCE_CHECK_COMMAND = "python3 scripts/refresh-release-ledger-evidence.py --check"

SECRET_VALUE_PATTERNS = [
    re.compile(r"(?i)(service[_-]?key|authorization|bearer\\s+[a-z0-9._~+/=-]{16,})"),
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)"),
]

REQUIRED_REVALIDATION_TRIGGERS = [
    "credential_runtime_review_handoff_changed",
    "release_consumer_compatibility_changed",
    "reviewed_credential_receipt_state_changed",
    "release_consumer_decision_changed",
]

POST_DECISION_COMMANDS = [
    RELEASE_EVIDENCE_REFRESH_COMMAND,
    RELEASE_EVIDENCE_CHECK_COMMAND,
]


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


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


def bool_value(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def string_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


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
                raise ValueError("manual-review acceptance packet must not contain secret-like values")


def validate_inputs(decision: dict[str, Any], acceptance: dict[str, Any], handoff: dict[str, Any], compatibility: dict[str, Any]) -> None:
    decision_summary = as_dict(decision.get("summary"), "decision.summary")
    decision_body = as_dict(decision.get("decision"), "decision.decision")
    acceptance_summary = as_dict(acceptance.get("summary"), "acceptance.summary")
    handoff_summary = as_dict(handoff.get("summary"), "handoff.summary")
    compatibility_risk = as_dict(compatibility.get("runtime_risk_evidence"), "compatibility.runtime_risk_evidence")
    if decision_summary.get("accepted") != decision_body.get("accepted"):
        raise ValueError("manual-review decision summary accepted state must match decision body")
    if acceptance_summary.get("accepted") != decision_summary.get("accepted"):
        raise ValueError("manual-review acceptance summary accepted state must match decision summary")
    if handoff_summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("manual-review packet requires secret-free default CI handoff")
    if handoff_summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("manual-review packet requires checked_in_secrets_allowed=false")
    validate_secret_free(decision)
    validate_secret_free(acceptance)


def accepted_decision_template(*, handoff_sha256: str, compatibility_sha256: str) -> dict[str, Any]:
    return {
        "accepted": True,
        "decision_status": "accepted",
        "reviewer": "<reviewer>",
        "reviewed_at": "<ISO-8601 UTC timestamp>",
        "reason": "<manual-review acceptance reason>",
        "handoff_sha256": handoff_sha256,
        "compatibility_sha256": compatibility_sha256,
        "expires_at": "<ISO-8601 UTC timestamp>",
        "revalidation_triggers": REQUIRED_REVALIDATION_TRIGGERS,
    }


def build_report(
    decision: dict[str, Any],
    acceptance: dict[str, Any],
    handoff: dict[str, Any],
    compatibility: dict[str, Any],
    *,
    decision_path: pathlib.Path = DEFAULT_DECISION,
    acceptance_path: pathlib.Path = DEFAULT_ACCEPTANCE,
    handoff_path: pathlib.Path = DEFAULT_HANDOFF,
    compatibility_path: pathlib.Path = DEFAULT_COMPATIBILITY,
) -> dict[str, Any]:
    validate_inputs(decision, acceptance, handoff, compatibility)
    decision_summary = as_dict(decision.get("summary"), "decision.summary")
    acceptance_summary = as_dict(acceptance.get("summary"), "acceptance.summary")
    handoff_summary = as_dict(handoff.get("summary"), "handoff.summary")
    compatibility_risk = as_dict(compatibility.get("runtime_risk_evidence"), "compatibility.runtime_risk_evidence")
    accepted = bool_value(decision_summary.get("accepted"), "decision.summary.accepted")
    handoff_digest = file_sha256(handoff_path)
    compatibility_digest = compatibility_binding_sha256(compatibility)
    goal_closure_allowed = bool(
        accepted
        and acceptance_summary.get("accepted") is True
        and compatibility_risk.get("manual_review_acceptance_boundary_accepted") is True
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": string_value(acceptance.get("generated_at"), "acceptance.generated_at"),
        "goal_issue": 344,
        "acceptance_packet_ticket": PACKET_TICKET,
        "decision_ticket": DECISION_TICKET,
        "acceptance_ticket": ACCEPTANCE_TICKET,
        "provider": "datapan-registry",
        "inputs": {
            "credential_runtime_manual_review_decision": decision_path.as_posix(),
            "credential_runtime_manual_review_acceptance": acceptance_path.as_posix(),
            "credential_runtime_review_handoff": handoff_path.as_posix(),
            "release_consumer_compatibility": compatibility_path.as_posix(),
        },
        "summary": {
            "accepted": accepted,
            "decision_status": decision_summary.get("decision_status"),
            "acceptance_status": acceptance_summary.get("acceptance_status"),
            "manual_review_required": compatibility_risk.get("manual_review_required"),
            "pending_review_sources": handoff_summary.get("pending_review_sources"),
            "reviewed_receipts_checked_in": handoff_summary.get("reviewed_receipts_checked_in"),
            "relief_eligible_sources": handoff_summary.get("relief_eligible_sources"),
            "manual_review_accepted": acceptance_summary.get("accepted"),
            "goal_closure_allowed": goal_closure_allowed,
            "default_ci_requires_credentials": False,
            "checked_in_secrets_allowed": False,
            "packet_status": "accepted_decision_ready_for_regeneration" if accepted else "acceptance_not_asserted",
        },
        "current_digests": {
            "handoff_path": handoff_path.as_posix(),
            "handoff_sha256": handoff_digest,
            "compatibility_path": compatibility_path.as_posix(),
            "compatibility_sha256": compatibility_digest,
        },
        "required_human_inputs": [
            {
                "field": "reviewer",
                "required": True,
                "description": "Named accountable reviewer or release owner.",
            },
            {
                "field": "reviewed_at",
                "required": True,
                "description": "UTC timestamp when the manual-review boundary was accepted.",
            },
            {
                "field": "reason",
                "required": True,
                "description": "Reason stating this release is manual-review only for affected consumers.",
            },
            {
                "field": "expires_at",
                "required": True,
                "description": "UTC timestamp when the accepted manual-review boundary expires.",
            },
        ],
        "accepted_decision_template": accepted_decision_template(
            handoff_sha256=handoff_digest,
            compatibility_sha256=compatibility_digest,
        ),
        "revalidation_triggers": REQUIRED_REVALIDATION_TRIGGERS,
        "operator_commands": {
            "validate_current_decision": "python3 scripts/validate-credential-runtime-manual-review-decision.py",
            "generate_acceptance_boundary": "python3 scripts/generate-credential-runtime-manual-review-acceptance.py",
            "release_evidence_refresh_command": RELEASE_EVIDENCE_REFRESH_COMMAND,
            "release_evidence_check_command": RELEASE_EVIDENCE_CHECK_COMMAND,
            "post_decision_regeneration": POST_DECISION_COMMANDS,
        },
        "release_boundary": {
            "canonical_registry_compatible": True,
            "manual_review_required": compatibility_risk.get("manual_review_required"),
            "manual_review_release_boundary_accepted": acceptance_summary.get("accepted"),
            "goal_closure_allowed": goal_closure_allowed,
            "goal_closure_effect": (
                "manual_review_acceptance_can_feed_goal_preflight"
                if goal_closure_allowed
                else "goal_remains_open_until_reviewed_receipts_or_explicit_acceptance"
            ),
        },
    }


def validate_invariants(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    if summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("manual-review acceptance packet must preserve secret-free default CI")
    if summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("manual-review acceptance packet must not allow checked-in secrets")
    if summary.get("accepted") is False and summary.get("goal_closure_allowed") is not False:
        raise ValueError("unaccepted manual-review packet cannot allow goal closure")
    if summary.get("accepted") is False and summary.get("packet_status") != "acceptance_not_asserted":
        raise ValueError("unaccepted manual-review packet must use acceptance_not_asserted status")
    operator_commands = as_dict(report.get("operator_commands"), "operator_commands")
    if operator_commands.get("release_evidence_refresh_command") != RELEASE_EVIDENCE_REFRESH_COMMAND:
        raise ValueError("manual-review packet must expose the fixed-point refresh command")
    if operator_commands.get("release_evidence_check_command") != RELEASE_EVIDENCE_CHECK_COMMAND:
        raise ValueError("manual-review packet must expose the fixed-point check command")
    if as_list(operator_commands.get("post_decision_regeneration"), "operator_commands.post_decision_regeneration") != POST_DECISION_COMMANDS:
        raise ValueError("post-decision regeneration must refresh and check release evidence")
    template = as_dict(report.get("accepted_decision_template"), "accepted_decision_template")
    digests = as_dict(report.get("current_digests"), "current_digests")
    if template.get("handoff_sha256") != digests.get("handoff_sha256"):
        raise ValueError("accepted decision template handoff digest must match current digest")
    if template.get("compatibility_sha256") != digests.get("compatibility_sha256"):
        raise ValueError("accepted decision template compatibility digest must match current digest")


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
    parser.add_argument("--decision", default=DEFAULT_DECISION, type=pathlib.Path)
    parser.add_argument("--acceptance", default=DEFAULT_ACCEPTANCE, type=pathlib.Path)
    parser.add_argument("--handoff", default=DEFAULT_HANDOFF, type=pathlib.Path)
    parser.add_argument("--compatibility", default=DEFAULT_COMPATIBILITY, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in manual-review packet is stale")
    args = parser.parse_args()

    try:
        report = build_report(
            load_json(args.decision),
            load_json(args.acceptance),
            load_json(args.handoff),
            load_json(args.compatibility),
            decision_path=args.decision,
            acceptance_path=args.acceptance,
            handoff_path=args.handoff,
            compatibility_path=args.compatibility,
        )
        validate_invariants(report)
        validate_schema(report, args.schema)
        validate_secret_free(report)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate manual-review acceptance packet: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing manual-review acceptance packet", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale manual-review acceptance packet; "
                "run `python3 scripts/generate-credential-runtime-manual-review-acceptance-packet.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (status={report['summary']['packet_status']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (status={report['summary']['packet_status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
