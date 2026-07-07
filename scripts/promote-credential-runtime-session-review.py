#!/usr/bin/env python3
"""Initialize, validate, or run batch promotion for credential session review decisions."""

from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import subprocess
import sys
import tempfile
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before promoting session review decisions") from exc


DEFAULT_REVIEW_PLAN = pathlib.Path(".datapan/runtime-evidence/credential-runtime-session-review-plan.json")
DEFAULT_DECISIONS = pathlib.Path(".datapan/runtime-evidence/credential-runtime-session-review-decisions.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-session-review-decisions.v1.schema.json")
PROMOTION_SCRIPT = pathlib.Path("scripts/promote-credential-runtime-receipt.py")
SCHEMA_VERSION = "datapan.credential-runtime-session-review-decisions.v1"


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_json(value), encoding="utf-8")


def render_json(value: dict[str, Any]) -> str:
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


def bool_value(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def validate_secret_free(value: dict[str, Any], label: str) -> None:
    rendered = render_json(value).lower()
    for marker in ("authorization:", "bearer ", "service_key=", "api_key=", "secret=", "token="):
        if marker in rendered:
            raise ValueError(f"{label} must not contain secret-like marker {marker!r}")


def validate_schema(value: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("; ".join(rendered))


def generated_from(review_plan: dict[str, Any], review_plan_path: pathlib.Path) -> dict[str, str]:
    source = as_dict(review_plan.get("generated_from"), "review_plan.generated_from")
    return {
        "review_plan": review_plan_path.as_posix(),
        "session": string_value(source.get("session"), "review_plan.generated_from.session"),
        "queue": string_value(source.get("queue"), "review_plan.generated_from.queue"),
    }


def succeeded_items(review_plan: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for raw_item in as_list(review_plan.get("review_plan"), "review_plan.review_plan"):
        item = as_dict(raw_item, "review_plan.review_plan[]")
        if item.get("status") == "succeeded":
            items.append(item)
    return items


def build_decision_template(review_plan: dict[str, Any], review_plan_path: pathlib.Path) -> dict[str, Any]:
    items = succeeded_items(review_plan)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_from": generated_from(review_plan, review_plan_path),
        "summary": {
            "sources": len(items),
            "decisions": len(items),
            "ready_to_promote": False,
            "default_ci_requires_credentials": False,
            "checked_in_decisions_allowed": False,
            "checked_in_secrets_allowed": False,
            "goal_closure_allowed": False,
        },
        "checked_in_boundaries": {
            "checked_in_session_output_allowed": False,
            "checked_in_review_plan_allowed": False,
            "checked_in_decisions_allowed": False,
            "checked_in_secrets_allowed": False,
            "goal_completion_effect": (
                "progress_evidence_only_goal_remains_open_until_reviewed_receipts_refresh_release_evidence"
            ),
        },
        "reviewer": {
            "reviewer": None,
            "reviewed_at": None,
        },
        "decisions": [
            {
                "source_id": string_value(item.get("source_id"), "review_plan.review_plan[].source_id"),
                "staged_receipt_path": string_value(
                    item.get("staged_receipt_path"),
                    "review_plan.review_plan[].staged_receipt_path",
                ),
                "reviewed_receipt_path": string_value(
                    item.get("reviewed_receipt_path"),
                    "review_plan.review_plan[].reviewed_receipt_path",
                ),
                "state": "pending_review",
                "decision": "pending_review",
                "reason": "pending_review",
                "ready_to_promote": False,
            }
            for item in items
        ],
    }


def expected_decisions(review_plan: dict[str, Any]) -> dict[str, dict[str, str]]:
    expected = {}
    for item in succeeded_items(review_plan):
        source_id = string_value(item.get("source_id"), "review_plan.review_plan[].source_id")
        expected[source_id] = {
            "staged_receipt_path": string_value(item.get("staged_receipt_path"), f"{source_id}.staged_receipt_path"),
            "reviewed_receipt_path": string_value(item.get("reviewed_receipt_path"), f"{source_id}.reviewed_receipt_path"),
        }
    return expected


def validate_decisions(
    decisions: dict[str, Any],
    *,
    review_plan: dict[str, Any],
    schema_path: pathlib.Path,
    require_ready: bool,
) -> None:
    validate_schema(decisions, schema_path)
    validate_secret_free(decisions, "session review decisions")
    summary = as_dict(decisions.get("summary"), "decisions.summary")
    boundaries = as_dict(decisions.get("checked_in_boundaries"), "decisions.checked_in_boundaries")
    reviewer = as_dict(decisions.get("reviewer"), "decisions.reviewer")
    raw_decisions = [as_dict(item, "decisions.decisions[]") for item in as_list(decisions.get("decisions"), "decisions.decisions")]
    expected = expected_decisions(review_plan)
    seen: set[str] = set()
    for item in raw_decisions:
        source_id = string_value(item.get("source_id"), "decisions.decisions[].source_id")
        if source_id in seen:
            raise ValueError(f"duplicate decision for source {source_id}")
        seen.add(source_id)
        if source_id not in expected:
            raise ValueError(f"decision source {source_id} is not present in succeeded review-plan items")
        if item.get("staged_receipt_path") != expected[source_id]["staged_receipt_path"]:
            raise ValueError(f"{source_id} staged_receipt_path does not match review plan")
        if item.get("reviewed_receipt_path") != expected[source_id]["reviewed_receipt_path"]:
            raise ValueError(f"{source_id} reviewed_receipt_path does not match review plan")
        state = string_value(item.get("state"), f"{source_id}.state")
        decision = string_value(item.get("decision"), f"{source_id}.decision")
        ready = bool_value(item.get("ready_to_promote"), f"{source_id}.ready_to_promote")
        if state == "reviewed_accepted" and decision != "allows_manual_review_reduction":
            raise ValueError(f"{source_id} accepted review must allow manual review reduction")
        if state == "reviewed_rejected" and decision != "keeps_manual_review_boundary":
            raise ValueError(f"{source_id} rejected review must keep manual review boundary")
        if state == "pending_review" and (decision != "pending_review" or ready is not False):
            raise ValueError(f"{source_id} pending review must keep decision=pending_review and ready_to_promote=false")
        if ready and state == "pending_review":
            raise ValueError(f"{source_id} cannot be ready_to_promote with pending_review state")
    if seen != set(expected):
        missing = sorted(set(expected) - seen)
        raise ValueError(f"decisions must cover every succeeded review-plan item; missing={missing}")
    if summary.get("sources") != len(expected) or summary.get("decisions") != len(raw_decisions):
        raise ValueError("summary counts must match review-plan and decision counts")
    for key in ("default_ci_requires_credentials", "checked_in_decisions_allowed", "checked_in_secrets_allowed", "goal_closure_allowed"):
        if summary.get(key) is not False:
            raise ValueError(f"summary.{key} must remain false")
    for key in ("checked_in_session_output_allowed", "checked_in_review_plan_allowed", "checked_in_decisions_allowed", "checked_in_secrets_allowed"):
        if boundaries.get(key) is not False:
            raise ValueError(f"checked_in_boundaries.{key} must remain false")
    if require_ready:
        if summary.get("ready_to_promote") is not True:
            raise ValueError("summary.ready_to_promote must be true before promotion")
        if not reviewer.get("reviewer") or not reviewer.get("reviewed_at"):
            raise ValueError("reviewer.reviewer and reviewer.reviewed_at are required before promotion")
        for item in raw_decisions:
            if item.get("ready_to_promote") is not True:
                raise ValueError(f"{item.get('source_id')} is not ready_to_promote")
            if item.get("state") not in {"reviewed_accepted", "reviewed_rejected"}:
                raise ValueError(f"{item.get('source_id')} must have a finalized review state")
            if item.get("reason") == "pending_review":
                raise ValueError(f"{item.get('source_id')} must replace pending_review reason")
    else:
        if summary.get("ready_to_promote") is True:
            for item in raw_decisions:
                if item.get("ready_to_promote") is not True:
                    raise ValueError("summary.ready_to_promote=true requires every item ready_to_promote=true")


def promotion_steps(decisions: dict[str, Any], *, force: bool) -> list[dict[str, Any]]:
    reviewer = as_dict(decisions.get("reviewer"), "decisions.reviewer")
    steps = []
    for item in [as_dict(raw, "decisions.decisions[]") for raw in as_list(decisions.get("decisions"), "decisions.decisions")]:
        argv = [
            "python3",
            PROMOTION_SCRIPT.as_posix(),
            string_value(item.get("staged_receipt_path"), "decision.staged_receipt_path"),
            "--state",
            string_value(item.get("state"), "decision.state"),
            "--decision",
            string_value(item.get("decision"), "decision.decision"),
            "--reviewer",
            string_value(reviewer.get("reviewer"), "reviewer.reviewer"),
            "--reason",
            string_value(item.get("reason"), "decision.reason"),
            "--reviewed-at",
            string_value(reviewer.get("reviewed_at"), "reviewer.reviewed_at"),
        ]
        if force:
            argv.append("--force")
        steps.append(
            {
                "source_id": item["source_id"],
                "command": shlex.join(argv),
                "argv": argv,
                "reviewed_receipt_path": item["reviewed_receipt_path"],
                "writes_reviewed_receipt": True,
            }
        )
    return steps


def build_promotion_report(decisions_path: pathlib.Path, decisions: dict[str, Any], *, force: bool, run: bool) -> dict[str, Any]:
    return {
        "schema_version": "datapan.credential-runtime-session-review-promotion.v1",
        "decisions": decisions_path.as_posix(),
        "run_mode": "run" if run else "check",
        "summary": {
            "decisions": len(as_list(decisions.get("decisions"), "decisions.decisions")),
            "requires_explicit_run": True,
            "default_ci_requires_credentials": False,
            "checked_in_decisions_allowed": False,
            "checked_in_secrets_allowed": False,
            "goal_closure_allowed": False,
        },
        "steps": promotion_steps(decisions, force=force),
        "execution": {
            "executed": False,
            "results": [],
        },
    }


def run_steps(report: dict[str, Any], *, json_mode: bool) -> dict[str, Any]:
    execution = {"executed": True, "results": []}
    for step in report["steps"]:
        entry = as_dict(step, "steps[]")
        argv = as_list(entry.get("argv"), "steps[].argv")
        if not all(isinstance(part, str) for part in argv):
            raise ValueError("promotion argv must contain only strings")
        if not json_mode:
            print(f"+ {entry['command']}", flush=True)
            result = subprocess.run(argv, check=False)
            stdout_bytes = 0
            stderr_bytes = 0
        else:
            result = subprocess.run(argv, check=False, text=True, capture_output=True)
            stdout_bytes = len(result.stdout.encode("utf-8"))
            stderr_bytes = len(result.stderr.encode("utf-8"))
        execution["results"].append(
            {
                "source_id": entry["source_id"],
                "command": entry["command"],
                "returncode": result.returncode,
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": stderr_bytes,
            }
        )
        if result.returncode != 0:
            raise RuntimeError(f"promotion step failed for {entry['source_id']} ({result.returncode})")
    return execution


def sample_review_plan() -> dict[str, Any]:
    return {
        "schema_version": "datapan.credential-runtime-session-review-plan.v1",
        "generated_from": {
            "session": ".datapan/runtime-evidence/credential-runtime-collection-session.json",
            "queue": "reports/credential-runtime-receipt-collection-queue.json",
            "session_schema": "schemas/datapan.credential-runtime-collection-session.v1.schema.json",
            "session_validation_command": "python3 scripts/validate-credential-runtime-collection-session.py .datapan/runtime-evidence/credential-runtime-collection-session.json --require-complete-source-set",
        },
        "summary": {
            "sources": 1,
            "succeeded": 1,
            "skipped_not_ready": 0,
            "failed": 0,
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
                "source_id": "data_go_kr",
                "status": "succeeded",
                "review_action": "validate_staged_receipt_then_promote_or_reject",
                "staged_receipt_path": ".datapan/runtime-evidence/data-go-kr-credentialed-receipt.json",
                "reviewed_receipt_path": "reports/credential-runtime-receipts/data-go-kr-credentialed-receipt.json",
                "staged_receipt_validation_command": "python3 scripts/validate-credential-runtime-receipts.py --allow-unreviewed .datapan/runtime-evidence/data-go-kr-credentialed-receipt.json",
                "reviewed_receipt_promotion_command": "python3 scripts/promote-credential-runtime-receipt.py .datapan/runtime-evidence/data-go-kr-credentialed-receipt.json --state <reviewed_accepted|reviewed_rejected> --decision <allows_manual_review_reduction|keeps_manual_review_boundary> --reviewer <reviewer> --reason <reason>",
                "required_reviewer_inputs": ["reviewer", "reason", "state", "decision"],
                "promotion_gate": "reviewed_receipt_must_validate_before_check_in",
                "next_action": "promote_reviewed_receipt_after_human_review",
            }
        ],
    }


def finalized_sample_decisions(review_plan: dict[str, Any], review_plan_path: pathlib.Path) -> dict[str, Any]:
    decisions = build_decision_template(review_plan, review_plan_path)
    decisions["summary"]["ready_to_promote"] = True
    decisions["reviewer"] = {
        "reviewer": "self-test-reviewer",
        "reviewed_at": "2026-07-07T00:00:00Z",
    }
    decisions["decisions"][0].update(
        {
            "state": "reviewed_accepted",
            "decision": "allows_manual_review_reduction",
            "reason": "self-test accepted verified redacted staged receipt",
            "ready_to_promote": True,
        }
    )
    return decisions


def self_test(schema_path: pathlib.Path) -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = pathlib.Path(raw_tmp)
        review_plan_path = tmp / "review-plan.json"
        review_plan = sample_review_plan()
        write_json(review_plan_path, review_plan)
        draft = build_decision_template(review_plan, review_plan_path)
        validate_decisions(draft, review_plan=review_plan, schema_path=schema_path, require_ready=False)
        finalized = finalized_sample_decisions(review_plan, review_plan_path)
        validate_decisions(finalized, review_plan=review_plan, schema_path=schema_path, require_ready=True)
        report = build_promotion_report(tmp / "decisions.json", finalized, force=False, run=False)
        if len(report["steps"]) != 1 or "--run" in report["steps"][0]["argv"]:
            raise ValueError("self-test failed: promotion report has unexpected command shape")
        invalid = finalized_sample_decisions(review_plan, review_plan_path)
        invalid["decisions"][0]["decision"] = "keeps_manual_review_boundary"
        try:
            validate_decisions(invalid, review_plan=review_plan, schema_path=schema_path, require_ready=True)
        except ValueError:
            pass
        else:
            raise ValueError("self-test failed: invalid accepted decision was allowed")
        secret = finalized_sample_decisions(review_plan, review_plan_path)
        secret["decisions"][0]["reason"] = "token=abc"
        try:
            validate_decisions(secret, review_plan=review_plan, schema_path=schema_path, require_ready=True)
        except ValueError:
            pass
        else:
            raise ValueError("self-test failed: secret-like reason was allowed")


def print_human(report: dict[str, Any]) -> None:
    print(
        "credential session review promotion "
        f"(mode={report['run_mode']}, decisions={report['summary']['decisions']})"
    )
    for step in report["steps"]:
        entry = as_dict(step, "steps[]")
        print(f"- {entry['source_id']}: {entry['command']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-plan", default=DEFAULT_REVIEW_PLAN, type=pathlib.Path)
    parser.add_argument("--decisions", default=DEFAULT_DECISIONS, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--init-decisions", action="store_true", help="write a local draft decision file from the review plan")
    parser.add_argument("--check", action="store_true", help="validate finalized local decisions without writing receipts")
    parser.add_argument("--run", action="store_true", help="promote finalized local decisions into reviewed receipt artifacts")
    parser.add_argument("--force", action="store_true", help="pass --force to per-receipt promotion commands")
    parser.add_argument("--json", action="store_true", help="print JSON output")
    parser.add_argument("--self-test", action="store_true", help="run credential-free workflow self-tests")
    args = parser.parse_args()

    try:
        if args.self_test:
            self_test(args.schema)
            print("ok credential session review promotion workflow self-test")
            return 0
        if args.init_decisions:
            review_plan = load_json(args.review_plan)
            template = build_decision_template(review_plan, args.review_plan)
            validate_decisions(template, review_plan=review_plan, schema_path=args.schema, require_ready=False)
            write_json(args.decisions, template)
            if args.json:
                print(render_json(template), end="")
            else:
                print(f"wrote {args.decisions} (decisions={template['summary']['decisions']})")
            return 0
        if not args.decisions.exists():
            if args.check:
                print(f"ok credential session review promotion workflow (decisions_present=false)")
                return 0
            raise ValueError(f"local decisions file is required: {args.decisions}")
        if not args.review_plan.exists():
            raise ValueError(f"local review plan is required: {args.review_plan}")
        review_plan = load_json(args.review_plan)
        decisions = load_json(args.decisions)
        validate_decisions(decisions, review_plan=review_plan, schema_path=args.schema, require_ready=True)
        report = build_promotion_report(args.decisions, decisions, force=args.force, run=args.run)
        if args.run:
            report["execution"] = run_steps(report, json_mode=args.json)
    except Exception as exc:  # noqa: BLE001 - operators need the failed invariant
        print(f"FAIL credential session review promotion workflow: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(render_json(report), end="")
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
