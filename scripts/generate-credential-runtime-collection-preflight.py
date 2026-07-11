#!/usr/bin/env python3
"""Generate or check secret-free credential runtime collection preflight evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before generating credential collection preflight evidence"
    ) from exc


DEFAULT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-collection-preflight.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/credential-runtime-collection-preflight.json")
SCHEMA_VERSION = "datapan.credential-runtime-collection-preflight.v1"


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


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def string_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def credential_envs(operator_command: str) -> list[str]:
    envs = sorted(
        part.split("=", 1)[0]
        for part in shlex.split(operator_command)
        if part.endswith("=<secret>")
    )
    if not envs:
        raise ValueError("operator command must declare credential env placeholders")
    return envs


def redacted_collection_command(operator_command: str) -> str:
    return " ".join(part for part in shlex.split(operator_command) if not part.endswith("=<secret>"))


def source_entry(source: dict[str, Any]) -> dict[str, Any]:
    source_id = string_value(source.get("source_id"), "source.source_id")
    operator_command = string_value(source.get("operator_command"), f"{source_id}.operator_command")
    candidate_batch = pathlib.Path(string_value(source.get("candidate_batch"), f"{source_id}.candidate_batch"))
    staged_receipt_path = pathlib.Path(
        string_value(source.get("staged_receipt_path"), f"{source_id}.staged_receipt_path")
    )
    reviewed_receipt_path = pathlib.Path(
        string_value(source.get("reviewed_receipt_path"), f"{source_id}.reviewed_receipt_path")
    )
    return {
        "source_id": source_id,
        "provider": string_value(source.get("provider"), f"{source_id}.provider"),
        "credential_envs": credential_envs(operator_command),
        "operator_environment_required": True,
        "default_ci_runnable": False,
        "candidate_batch": candidate_batch.as_posix(),
        "candidate_batch_present": candidate_batch.is_file(),
        "runtime_evidence_plan": string_value(source.get("runtime_evidence_plan"), f"{source_id}.runtime_evidence_plan"),
        "staged_receipt_path": staged_receipt_path.as_posix(),
        # Staged receipts are ignored, operator-local evidence. Checked-in
        # preflight must be reproducible in a clean checkout regardless of an
        # operator's current .datapan/runtime-evidence directory.
        "staged_receipt_present": False,
        "reviewed_receipt_path": reviewed_receipt_path.as_posix(),
        "reviewed_receipt_present": reviewed_receipt_path.is_file(),
        "current_receipt_state": string_value(source.get("current_receipt_state"), f"{source_id}.current_receipt_state"),
        "receipt_relief_eligible": source.get("receipt_relief_eligible") is True,
        "collection_preflight_command": string_value(
            source.get("collection_preflight_command"),
            f"{source_id}.collection_preflight_command",
        ),
        "collection_run_command": string_value(source.get("collection_run_command"), f"{source_id}.collection_run_command"),
        "collection_command_redacted": redacted_collection_command(operator_command),
        "staged_receipt_validation_command": string_value(
            source.get("staged_receipt_validation_command"),
            f"{source_id}.staged_receipt_validation_command",
        ),
        "reviewed_receipt_validation_command": string_value(
            source.get("reviewed_receipt_validation_command"),
            f"{source_id}.reviewed_receipt_validation_command",
        ),
        "reviewed_receipt_promotion_command": string_value(
            source.get("reviewed_receipt_promotion_command"),
            f"{source_id}.reviewed_receipt_promotion_command",
        ),
        "next_action": string_value(source.get("next_action"), f"{source_id}.next_action"),
    }


def build_report(queue: dict[str, Any]) -> dict[str, Any]:
    generated_at = queue.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("queue.generated_at must be a non-empty string")
    queue_summary = as_dict(queue.get("summary"), "queue.summary")
    sources = [source_entry(as_dict(source, "queue.sources[]")) for source in as_list(queue.get("sources"), "queue.sources")]
    candidate_present = sum(1 for source in sources if source["candidate_batch_present"])
    reviewed_present = sum(1 for source in sources if source["reviewed_receipt_present"])
    candidate_missing = len(sources) - candidate_present
    reviewed_missing = len(sources) - reviewed_present
    preflight_status = (
        "reviewed_receipts_complete"
        if reviewed_missing == 0
        else "collection_inputs_ready_operator_environment_needed"
        if candidate_missing == 0
        else "collection_required_operator_environment_needed"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "preflight_ticket": 399,
        "provider": "datapan-registry",
        "inputs": {
            "queue": DEFAULT_QUEUE.as_posix(),
            "runner_plan_mode": "secret_free_default_ci_preflight",
        },
        "summary": {
            "sources": len(sources),
            "credential_gated_sources": queue_summary.get("credential_gated_sources"),
            "candidate_batches_present": candidate_present,
            "candidate_batches_missing": candidate_missing,
            "reviewed_receipts_present": reviewed_present,
            "reviewed_receipts_missing": reviewed_missing,
            "default_ci_runnable_sources": 0,
            "operator_environment_required_sources": len(sources),
            "manual_review_reduction_allowed": queue_summary.get("manual_review_reduction_allowed"),
            "preflight_status": preflight_status,
        },
        "sources": sources,
    }


def validate_invariants(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    sources = [as_dict(source, "source") for source in as_list(report.get("sources"), "sources")]
    if summary.get("sources") != len(sources):
        raise ValueError("summary.sources must match sources length")
    if summary.get("manual_review_reduction_allowed") is not False:
        raise ValueError("credential collection preflight cannot allow manual-review reduction")
    if any(source.get("default_ci_runnable") is not False for source in sources):
        raise ValueError("default CI must not be marked runnable for credential-gated collection")
    for source in sources:
        command = string_value(source.get("collection_command_redacted"), "collection_command_redacted")
        if "<secret>" in command:
            raise ValueError("collection_command_redacted must not contain secret placeholders")


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
    parser.add_argument("--queue", default=DEFAULT_QUEUE, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in preflight evidence is stale")
    args = parser.parse_args()

    try:
        report = build_report(load_json(args.queue))
        validate_invariants(report)
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate credential collection preflight: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing credential collection preflight", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale credential collection preflight; "
                "run `python3 scripts/generate-credential-runtime-collection-preflight.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (sources={report['summary']['sources']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (sources={report['summary']['sources']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
