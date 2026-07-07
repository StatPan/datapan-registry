#!/usr/bin/env python3
"""Generate a local review plan from a credential runtime collection session."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("missing dependency: install jsonschema before generating credential session review plans") from exc


SCHEMA_VERSION = "datapan.credential-runtime-session-review-plan.v1"
DEFAULT_SESSION = pathlib.Path(".datapan/runtime-evidence/credential-runtime-collection-session.json")
DEFAULT_OUTPUT = pathlib.Path(".datapan/runtime-evidence/credential-runtime-session-review-plan.json")
DEFAULT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_SESSION_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-collection-session.v1.schema.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-session-review-plan.v1.schema.json")
SESSION_VALIDATOR = pathlib.Path("scripts/validate-credential-runtime-collection-session.py")
SECRET_MARKER_RE = re.compile(
    r"(<secret>|credential_value|authorization:|bearer\s+|api[_-]?secret|service[_-]?key=)",
    re.IGNORECASE,
)


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_json(value), encoding="utf-8")


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


def queue_sources(queue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_source in as_list(queue.get("sources"), "queue.sources"):
        source = as_dict(raw_source, "queue.sources[]")
        source_id = string_value(source.get("source_id"), "queue.source_id")
        result[source_id] = source
    return result


def session_validation_command(session_path: pathlib.Path, queue_path: pathlib.Path) -> str:
    return (
        f"python3 {SESSION_VALIDATOR.as_posix()} {session_path.as_posix()} "
        f"--queue {queue_path.as_posix()} --require-complete-source-set"
    )


def validate_collection_session(session_path: pathlib.Path, queue_path: pathlib.Path) -> None:
    subprocess.run(
        [
            sys.executable,
            SESSION_VALIDATOR.as_posix(),
            session_path.as_posix(),
            "--queue",
            queue_path.as_posix(),
            "--require-complete-source-set",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def succeeded_item(result: dict[str, Any], queue_source: dict[str, Any]) -> dict[str, Any]:
    source_id = string_value(result.get("source_id"), "result.source_id")
    staged_path = string_value(result.get("staged_receipt_path"), f"{source_id}.staged_receipt_path")
    expected_staged = string_value(queue_source.get("staged_receipt_path"), f"{source_id}.queue.staged_receipt_path")
    if staged_path != expected_staged:
        raise ValueError(f"{source_id}: staged receipt path does not match queue")
    return {
        "source_id": source_id,
        "status": "succeeded",
        "review_action": "validate_staged_receipt_then_promote_or_reject",
        "staged_receipt_path": staged_path,
        "reviewed_receipt_path": string_value(
            queue_source.get("reviewed_receipt_path"),
            f"{source_id}.reviewed_receipt_path",
        ),
        "staged_receipt_validation_command": string_value(
            queue_source.get("staged_receipt_validation_command"),
            f"{source_id}.staged_receipt_validation_command",
        ),
        "reviewed_receipt_promotion_command": string_value(
            result.get("reviewed_receipt_promotion_command"),
            f"{source_id}.reviewed_receipt_promotion_command",
        ),
        "required_reviewer_inputs": [
            "review_state",
            "review_decision",
            "reviewer",
            "reason",
        ],
        "promotion_gate": string_value(queue_source.get("promotion_gate"), f"{source_id}.promotion_gate"),
        "next_action": "review_staged_receipt_before_promotion",
    }


def skipped_item(result: dict[str, Any]) -> dict[str, Any]:
    source_id = string_value(result.get("source_id"), "result.source_id")
    return {
        "source_id": source_id,
        "status": "skipped_not_ready",
        "review_action": "resolve_readiness_then_rerun_collection",
        "readiness_blockers": [
            string_value(reason, f"{source_id}.reasons[]")
            for reason in as_list(result.get("reasons"), f"{source_id}.reasons")
        ],
        "missing_credential_envs": [
            string_value(env_name, f"{source_id}.missing_credential_envs[]")
            for env_name in as_list(result.get("missing_credential_envs"), f"{source_id}.missing_credential_envs")
        ],
        "candidate_batch": string_value(result.get("candidate_batch"), f"{source_id}.candidate_batch"),
        "reviewed_receipt_path": string_value(
            result.get("reviewed_receipt_path"),
            f"{source_id}.reviewed_receipt_path",
        ),
        "next_action": "resolve_readiness_then_rerun_batch",
    }


def failed_item(result: dict[str, Any]) -> dict[str, Any]:
    source_id = string_value(result.get("source_id"), "result.source_id")
    return {
        "source_id": source_id,
        "status": "failed",
        "review_action": "inspect_collection_error_then_rerun_or_keep_manual_review_boundary",
        "error": string_value(result.get("error"), f"{source_id}.error"),
        "next_action": "inspect_source_error_then_rerun_or_keep_manual_review_boundary",
    }


def build_review_plan(
    session: dict[str, Any],
    queue: dict[str, Any],
    *,
    session_path: pathlib.Path,
    queue_path: pathlib.Path,
) -> dict[str, Any]:
    sources_by_id = queue_sources(queue)
    results = [as_dict(result, "session.results[]") for result in as_list(session.get("results"), "session.results")]
    items: list[dict[str, Any]] = []
    for result in results:
        source_id = string_value(result.get("source_id"), "session.result.source_id")
        if source_id not in sources_by_id:
            raise ValueError(f"session source not in queue: {source_id}")
        status = string_value(result.get("status"), f"{source_id}.status")
        if status == "succeeded":
            items.append(succeeded_item(result, sources_by_id[source_id]))
        elif status == "skipped_not_ready":
            items.append(skipped_item(result))
        elif status == "failed":
            items.append(failed_item(result))
        else:
            raise ValueError(f"{source_id}: unsupported status {status}")

    succeeded = sum(1 for item in items if item["status"] == "succeeded")
    skipped = sum(1 for item in items if item["status"] == "skipped_not_ready")
    failed = sum(1 for item in items if item["status"] == "failed")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_from": {
            "session": session_path.as_posix(),
            "queue": queue_path.as_posix(),
            "session_schema": DEFAULT_SESSION_SCHEMA.as_posix(),
            "session_validation_command": session_validation_command(session_path, queue_path),
        },
        "summary": {
            "sources": len(items),
            "succeeded": succeeded,
            "skipped_not_ready": skipped,
            "failed": failed,
            "staged_receipts_to_review": succeeded,
            "next_action": (
                "validate_and_review_staged_receipts"
                if succeeded
                else "resolve_readiness_or_failures_before_review"
            ),
        },
        "checked_in_boundaries": {
            "checked_in_session_output_allowed": False,
            "checked_in_review_plan_allowed": False,
            "checked_in_secrets_allowed": False,
            "goal_completion_effect": "progress_evidence_only_goal_remains_open",
        },
        "review_plan": items,
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


def validate_redaction(report: dict[str, Any]) -> None:
    rendered = render_json(report)
    match = SECRET_MARKER_RE.search(rendered)
    if match:
        raise ValueError(f"review plan contains secret marker: {match.group(0)}")


def validate_invariants(report: dict[str, Any]) -> None:
    boundaries = as_dict(report.get("checked_in_boundaries"), "checked_in_boundaries")
    summary = as_dict(report.get("summary"), "summary")
    items = [as_dict(item, "review_plan[]") for item in as_list(report.get("review_plan"), "review_plan")]
    if boundaries.get("checked_in_session_output_allowed") is not False:
        raise ValueError("review plan must not allow checked-in session output")
    if boundaries.get("checked_in_review_plan_allowed") is not False:
        raise ValueError("review plan must not allow checked-in live review plans")
    if boundaries.get("checked_in_secrets_allowed") is not False:
        raise ValueError("review plan must not allow checked-in secrets")
    if summary.get("sources") != len(items):
        raise ValueError("summary.sources must match review_plan length")
    if summary.get("staged_receipts_to_review") != sum(1 for item in items if item.get("status") == "succeeded"):
        raise ValueError("summary.staged_receipts_to_review must match succeeded items")
    validate_redaction(report)


def generate_plan(
    *,
    session_path: pathlib.Path,
    queue_path: pathlib.Path,
    schema_path: pathlib.Path,
) -> dict[str, Any]:
    validate_collection_session(session_path, queue_path)
    report = build_review_plan(
        load_json(session_path),
        load_json(queue_path),
        session_path=session_path,
        queue_path=queue_path,
    )
    validate_invariants(report)
    validate_schema(report, schema_path)
    return report


def self_test(schema_path: pathlib.Path) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = pathlib.Path(temp_dir)
        queue_path = root / "queue.json"
        session_path = root / "session.json"
        queue = {
            "sources": [
                {
                    "source_id": "ready",
                    "operator_command": "datapan ok READY_TOKEN=<secret>",
                    "candidate_batch": "reports/ready/runtime-candidates.json",
                    "staged_receipt_path": ".datapan/runtime-evidence/ready-credentialed-receipt.json",
                    "reviewed_receipt_path": "reports/credential-runtime-receipts/ready-credentialed-receipt.json",
                    "staged_receipt_validation_command": "python3 scripts/validate-credential-runtime-receipts.py --allow-unreviewed .datapan/runtime-evidence/ready-credentialed-receipt.json",
                    "reviewed_receipt_promotion_command": "python3 scripts/promote-credential-runtime-receipt.py .datapan/runtime-evidence/ready-credentialed-receipt.json --state <reviewed_accepted|reviewed_rejected> --decision <allows_manual_review_reduction|keeps_manual_review_boundary> --reviewer <reviewer> --reason <reason>",
                    "promotion_gate": "Promote only after redaction review.",
                },
                {
                    "source_id": "skipped",
                    "operator_command": "datapan skip SKIPPED_TOKEN=<secret>",
                    "candidate_batch": "reports/skipped/runtime-candidates.json",
                    "staged_receipt_path": ".datapan/runtime-evidence/skipped-credentialed-receipt.json",
                    "reviewed_receipt_path": "reports/credential-runtime-receipts/skipped-credentialed-receipt.json",
                    "staged_receipt_validation_command": "python3 scripts/validate-credential-runtime-receipts.py --allow-unreviewed .datapan/runtime-evidence/skipped-credentialed-receipt.json",
                    "reviewed_receipt_promotion_command": "python3 scripts/promote-credential-runtime-receipt.py .datapan/runtime-evidence/skipped-credentialed-receipt.json --state <reviewed_accepted|reviewed_rejected> --decision <allows_manual_review_reduction|keeps_manual_review_boundary> --reviewer <reviewer> --reason <reason>",
                    "promotion_gate": "Promote only after redaction review.",
                },
            ]
        }
        session = {
            "schema_version": "datapan.credential-runtime-collection-session.v1",
            "queue": queue_path.as_posix(),
            "summary": {
                "sources": 2,
                "succeeded": 1,
                "skipped_not_ready": 1,
                "failed": 0,
                "checked_in_secrets_allowed": False,
                "next_action": "review_and_promote_staged_receipts",
            },
            "results": [
                {
                    "source_id": "ready",
                    "status": "succeeded",
                    "staged_receipt_path": ".datapan/runtime-evidence/ready-credentialed-receipt.json",
                    "next_action": "review_and_promote_staged_receipt",
                    "reviewed_receipt_promotion_command": "python3 scripts/promote-credential-runtime-receipt.py .datapan/runtime-evidence/ready-credentialed-receipt.json --state reviewed_accepted --decision keeps_manual_review_boundary --reviewer reviewer --reason reviewed",
                },
                {
                    "source_id": "skipped",
                    "status": "skipped_not_ready",
                    "reasons": ["missing_credential_env"],
                    "missing_credential_envs": ["SKIPPED_TOKEN"],
                    "candidate_batch": "reports/skipped/runtime-candidates.json",
                    "candidate_batch_present": True,
                    "reviewed_receipt_path": "reports/credential-runtime-receipts/skipped-credentialed-receipt.json",
                    "reviewed_receipt_present": False,
                    "next_action": "resolve_readiness_then_rerun_batch",
                },
            ],
        }
        queue_path.write_text(render_json(queue), encoding="utf-8")
        session_path.write_text(render_json(session), encoding="utf-8")
        report = generate_plan(session_path=session_path, queue_path=queue_path, schema_path=schema_path)
        if report["summary"]["staged_receipts_to_review"] != 1:
            raise ValueError("self-test failed: expected one staged receipt to review")
        report["review_plan"][0]["reviewed_receipt_promotion_command"] = "authorization: bearer secret"
        try:
            validate_invariants(report)
            validate_schema(report, schema_path)
        except ValueError:
            return
        raise ValueError("self-test failed: secret marker was accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", nargs="?", default=DEFAULT_SESSION, type=pathlib.Path)
    parser.add_argument("--queue", default=DEFAULT_QUEUE, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    try:
        if args.self_test:
            self_test(args.schema)
            print("ok credential runtime session review plan self-test")
            return 0
        report = generate_plan(session_path=args.session, queue_path=args.queue, schema_path=args.schema)
    except Exception as exc:  # noqa: BLE001 - operators need the failed invariant
        print(f"FAIL credential runtime session review plan: {exc}", file=sys.stderr)
        return 1

    if args.output:
        write_json(args.output, report)
        print(f"wrote {args.output} (items={report['summary']['sources']})")
    else:
        print(render_json(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
