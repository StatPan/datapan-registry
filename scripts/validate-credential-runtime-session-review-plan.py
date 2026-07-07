#!/usr/bin/env python3
"""Validate a local credential runtime session review plan."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import tempfile
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before validating credential session review plans") from exc


SCHEMA_VERSION = "datapan.credential-runtime-session-review-plan.v1"
DEFAULT_PLAN = pathlib.Path(".datapan/runtime-evidence/credential-runtime-session-review-plan.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-session-review-plan.v1.schema.json")
SECRET_MARKER_RE = re.compile(
    r"(<secret>|credential_value|authorization:|bearer\s+|api[_-]?secret|service[_-]?key=)",
    re.IGNORECASE,
)
REQUIRED_REVIEWER_INPUTS = {"review_state", "review_decision", "reviewer", "reason"}


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def string_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


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


def validate_redaction(report: dict[str, Any]) -> None:
    rendered = render_json(report)
    match = SECRET_MARKER_RE.search(rendered)
    if match:
        raise ValueError(f"review plan contains secret marker: {match.group(0)}")


def validate_generated_from(report: dict[str, Any]) -> None:
    generated_from = as_dict(report.get("generated_from"), "generated_from")
    session = string_value(generated_from.get("session"), "generated_from.session")
    command = string_value(generated_from.get("session_validation_command"), "generated_from.session_validation_command")
    required_fragments = [
        "scripts/validate-credential-runtime-collection-session.py",
        session,
        "--require-complete-source-set",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in command]
    if missing:
        raise ValueError(f"session validation command missing required fragment(s): {', '.join(missing)}")


def validate_succeeded_item(item: dict[str, Any]) -> None:
    source_id = string_value(item.get("source_id"), "review_plan[].source_id")
    staged_path = string_value(item.get("staged_receipt_path"), f"{source_id}.staged_receipt_path")
    staged_command = string_value(
        item.get("staged_receipt_validation_command"),
        f"{source_id}.staged_receipt_validation_command",
    )
    promotion_command = string_value(
        item.get("reviewed_receipt_promotion_command"),
        f"{source_id}.reviewed_receipt_promotion_command",
    )
    required_inputs = {
        string_value(value, f"{source_id}.required_reviewer_inputs[]")
        for value in as_list(item.get("required_reviewer_inputs"), f"{source_id}.required_reviewer_inputs")
    }
    if not REQUIRED_REVIEWER_INPUTS.issubset(required_inputs):
        missing = sorted(REQUIRED_REVIEWER_INPUTS - required_inputs)
        raise ValueError(f"{source_id}: required reviewer inputs missing: {', '.join(missing)}")
    if "scripts/validate-credential-runtime-receipts.py" not in staged_command:
        raise ValueError(f"{source_id}: staged receipt validation command must use receipt validator")
    if "--allow-unreviewed" not in staged_command:
        raise ValueError(f"{source_id}: staged receipt validation command must allow unreviewed staged receipts")
    if staged_path not in staged_command:
        raise ValueError(f"{source_id}: staged receipt validation command must reference staged receipt path")
    required_promotion_fragments = [
        "scripts/promote-credential-runtime-receipt.py",
        staged_path,
        "--state",
        "--decision",
        "--reviewer",
        "--reason",
    ]
    missing = [fragment for fragment in required_promotion_fragments if fragment not in promotion_command]
    if missing:
        raise ValueError(f"{source_id}: reviewed receipt promotion command missing: {', '.join(missing)}")


def validate_invariants(report: dict[str, Any]) -> None:
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"review plan must use {SCHEMA_VERSION}")
    boundaries = as_dict(report.get("checked_in_boundaries"), "checked_in_boundaries")
    if boundaries.get("checked_in_session_output_allowed") is not False:
        raise ValueError("review plan must not allow checked-in session output")
    if boundaries.get("checked_in_review_plan_allowed") is not False:
        raise ValueError("review plan must not allow checked-in live review plans")
    if boundaries.get("checked_in_secrets_allowed") is not False:
        raise ValueError("review plan must not allow checked-in secrets")
    if boundaries.get("goal_completion_effect") != "progress_evidence_only_goal_remains_open":
        raise ValueError("review plan must preserve progress-only goal completion effect")

    validate_generated_from(report)
    summary = as_dict(report.get("summary"), "summary")
    items = [as_dict(item, "review_plan[]") for item in as_list(report.get("review_plan"), "review_plan")]
    counts = {
        "succeeded": sum(1 for item in items if item.get("status") == "succeeded"),
        "skipped_not_ready": sum(1 for item in items if item.get("status") == "skipped_not_ready"),
        "failed": sum(1 for item in items if item.get("status") == "failed"),
    }
    if summary.get("sources") != len(items):
        raise ValueError("summary.sources must match review_plan length")
    for key, value in counts.items():
        if summary.get(key) != value:
            raise ValueError(f"summary.{key} must match review_plan {key} count")
    if summary.get("staged_receipts_to_review") != counts["succeeded"]:
        raise ValueError("summary.staged_receipts_to_review must match succeeded item count")
    expected_next_action = (
        "validate_and_review_staged_receipts"
        if counts["succeeded"]
        else "resolve_readiness_or_failures_before_review"
    )
    if summary.get("next_action") != expected_next_action:
        raise ValueError("summary.next_action does not match review_plan status mix")

    seen: set[str] = set()
    for item in items:
        source_id = string_value(item.get("source_id"), "review_plan[].source_id")
        if source_id in seen:
            raise ValueError(f"duplicate review plan source: {source_id}")
        seen.add(source_id)
        status = string_value(item.get("status"), f"{source_id}.status")
        if status == "succeeded":
            validate_succeeded_item(item)
        elif status not in {"skipped_not_ready", "failed"}:
            raise ValueError(f"{source_id}: unsupported review plan status {status}")
    validate_redaction(report)


def validate_plan(path: pathlib.Path, schema_path: pathlib.Path) -> dict[str, Any]:
    report = load_json(path)
    validate_schema(report, schema_path)
    validate_invariants(report)
    return report


def valid_plan() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_from": {
            "session": ".datapan/runtime-evidence/credential-runtime-collection-session.json",
            "queue": "reports/credential-runtime-receipt-collection-queue.json",
            "session_schema": "schemas/datapan.credential-runtime-collection-session.v1.schema.json",
            "session_validation_command": (
                "python3 scripts/validate-credential-runtime-collection-session.py "
                ".datapan/runtime-evidence/credential-runtime-collection-session.json "
                "--queue reports/credential-runtime-receipt-collection-queue.json "
                "--require-complete-source-set"
            ),
        },
        "summary": {
            "sources": 3,
            "succeeded": 1,
            "skipped_not_ready": 1,
            "failed": 1,
            "staged_receipts_to_review": 1,
            "next_action": "validate_and_review_staged_receipts",
        },
        "checked_in_boundaries": {
            "checked_in_session_output_allowed": False,
            "checked_in_review_plan_allowed": False,
            "checked_in_secrets_allowed": False,
            "goal_completion_effect": "progress_evidence_only_goal_remains_open",
        },
        "review_plan": [
            {
                "source_id": "ready",
                "status": "succeeded",
                "review_action": "validate_staged_receipt_then_promote_or_reject",
                "staged_receipt_path": ".datapan/runtime-evidence/ready-credentialed-receipt.json",
                "reviewed_receipt_path": "reports/credential-runtime-receipts/ready-credentialed-receipt.json",
                "staged_receipt_validation_command": (
                    "python3 scripts/validate-credential-runtime-receipts.py --allow-unreviewed "
                    ".datapan/runtime-evidence/ready-credentialed-receipt.json"
                ),
                "reviewed_receipt_promotion_command": (
                    "python3 scripts/promote-credential-runtime-receipt.py "
                    ".datapan/runtime-evidence/ready-credentialed-receipt.json "
                    "--state reviewed_accepted --decision keeps_manual_review_boundary "
                    "--reviewer reviewer --reason reviewed"
                ),
                "required_reviewer_inputs": [
                    "review_state",
                    "review_decision",
                    "reviewer",
                    "reason",
                ],
                "promotion_gate": "Promote only after redaction review.",
                "next_action": "review_staged_receipt_before_promotion",
            },
            {
                "source_id": "skipped",
                "status": "skipped_not_ready",
                "review_action": "resolve_readiness_then_rerun_collection",
                "readiness_blockers": ["missing_credential_env"],
                "missing_credential_envs": ["SKIPPED_TOKEN"],
                "candidate_batch": "reports/skipped/runtime-candidates.json",
                "reviewed_receipt_path": "reports/credential-runtime-receipts/skipped-credentialed-receipt.json",
                "next_action": "resolve_readiness_then_rerun_batch",
            },
            {
                "source_id": "failed",
                "status": "failed",
                "review_action": "inspect_collection_error_then_rerun_or_keep_manual_review_boundary",
                "error": "collection command exited with status 1",
                "next_action": "inspect_source_error_then_rerun_or_keep_manual_review_boundary",
            },
        ],
    }


def expect_invalid(plan: dict[str, Any], schema_path: pathlib.Path, label: str) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        path = pathlib.Path(temp_dir) / "review-plan.json"
        path.write_text(render_json(plan), encoding="utf-8")
        try:
            validate_plan(path, schema_path)
        except ValueError:
            return
        raise ValueError(f"self-test failed: invalid review plan accepted ({label})")


def self_test(schema_path: pathlib.Path) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        path = pathlib.Path(temp_dir) / "review-plan.json"
        plan = valid_plan()
        path.write_text(render_json(plan), encoding="utf-8")
        report = validate_plan(path, schema_path)
        if report["summary"]["staged_receipts_to_review"] != 1:
            raise ValueError("self-test failed: expected one staged receipt to review")

    plan = valid_plan()
    plan["checked_in_boundaries"]["checked_in_review_plan_allowed"] = True
    expect_invalid(plan, schema_path, "checked-in review plan allowed")

    plan = valid_plan()
    plan["summary"]["succeeded"] = 2
    expect_invalid(plan, schema_path, "summary mismatch")

    plan = valid_plan()
    plan["review_plan"][0]["reviewed_receipt_promotion_command"] = "authorization: bearer secret"
    expect_invalid(plan, schema_path, "secret marker")

    plan = valid_plan()
    plan["review_plan"][0]["reviewed_receipt_promotion_command"] = "python3 scripts/validate-credential-runtime-receipts.py"
    expect_invalid(plan, schema_path, "unsafe promotion command")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", nargs="?", default=DEFAULT_PLAN, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    try:
        if args.self_test:
            self_test(args.schema)
            print("ok credential runtime session review plan validator self-test")
            return 0
        report = validate_plan(args.plan, args.schema)
    except Exception as exc:  # noqa: BLE001 - operators need the failed invariant
        print(f"FAIL credential runtime session review plan validation: {exc}", file=sys.stderr)
        return 1

    print(f"ok {args.plan} (credential runtime session review plan, items={report['summary']['sources']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
