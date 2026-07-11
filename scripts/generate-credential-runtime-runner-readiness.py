#!/usr/bin/env python3
"""Generate or check checked-in credential runtime runner readiness evidence."""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
from types import ModuleType
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before generating credential runner readiness evidence"
    ) from exc


DEFAULT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_RUNNER = pathlib.Path("scripts/run-credential-runtime-collection.py")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-runner-readiness.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/credential-runtime-runner-readiness.json")
LOCAL_SESSION_OUTPUT = ".datapan/runtime-evidence/credential-runtime-collection-session.json"
LOCAL_SESSION_REVIEW_PLAN_OUTPUT = ".datapan/runtime-evidence/credential-runtime-session-review-plan.json"
SCHEMA_VERSION = "datapan.credential-runtime-runner-readiness.v1"
RUNNER_PLAN_SCHEMA_VERSION = "datapan.credential-runtime-collection-runner-plan.v1"


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


def load_runner(path: pathlib.Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("credential_runtime_collection_runner", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load runner module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def source_entry(source: dict[str, Any], runner: ModuleType) -> dict[str, Any]:
    source_id = string_value(source.get("source_id"), "source.source_id")
    envs = list(runner.env_names(source))
    candidate_batch = pathlib.Path(string_value(source.get("candidate_batch"), f"{source_id}.candidate_batch"))
    staged_receipt_path = pathlib.Path(
        string_value(source.get("staged_receipt_path"), f"{source_id}.staged_receipt_path")
    )
    reviewed_receipt_path = pathlib.Path(
        string_value(source.get("reviewed_receipt_path"), f"{source_id}.reviewed_receipt_path")
    )
    plan = {
        "source_id": source_id,
        "credential_envs": envs,
        "credential_envs_present": False,
        "missing_credential_envs": envs,
        "candidate_batch": candidate_batch.as_posix(),
        "candidate_batch_present": candidate_batch.is_file(),
        "staged_receipt_path": staged_receipt_path.as_posix(),
        # This checked-in report models clean default CI. Operator-local staged
        # receipts must not make its content depend on ignored workspace state.
        "staged_receipt_present": False,
        "reviewed_receipt_path": reviewed_receipt_path.as_posix(),
        "reviewed_receipt_present": reviewed_receipt_path.is_file(),
        "can_run": False,
        "collection_command": " ".join(runner.shlex.quote(part) for part in runner.command_args(source)),
    }
    blockers = list(runner.readiness_blockers(plan, force=False))
    if not plan["candidate_batch_present"]:
        next_action = "restore_candidate_batch_before_operator_collection"
    elif plan["reviewed_receipt_present"]:
        next_action = "reviewed_receipt_already_present"
    else:
        next_action = "provide_operator_credentials_then_run_collection"
    return {
        "source_id": source_id,
        "provider": string_value(source.get("provider"), f"{source_id}.provider"),
        "credential_envs": envs,
        "credential_envs_present": False,
        "missing_credential_envs": envs,
        "readiness_blockers": blockers,
        "candidate_batch": plan["candidate_batch"],
        "candidate_batch_present": plan["candidate_batch_present"],
        "staged_receipt_path": plan["staged_receipt_path"],
        "staged_receipt_present": plan["staged_receipt_present"],
        "reviewed_receipt_path": plan["reviewed_receipt_path"],
        "reviewed_receipt_present": plan["reviewed_receipt_present"],
        "can_run_without_credentials": False,
        "collection_command": plan["collection_command"],
        "staged_receipt_validation_command": string_value(
            source.get("staged_receipt_validation_command"),
            f"{source_id}.staged_receipt_validation_command",
        ),
        "reviewed_receipt_promotion_command": string_value(
            source.get("reviewed_receipt_promotion_command"),
            f"{source_id}.reviewed_receipt_promotion_command",
        ),
        "next_action": next_action,
    }


def runner_status(sources: list[dict[str, Any]]) -> str:
    if all(source["reviewed_receipt_present"] for source in sources):
        return "reviewed_receipts_complete"
    if any(not source["candidate_batch_present"] for source in sources):
        return "candidate_batches_missing"
    return "operator_credentials_required"


def build_report(queue: dict[str, Any], runner: ModuleType) -> dict[str, Any]:
    generated_at = queue.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("queue.generated_at must be a non-empty string")
    queue_summary = as_dict(queue.get("summary"), "queue.summary")
    sources = [
        source_entry(as_dict(source, "queue.sources[]"), runner)
        for source in as_list(queue.get("sources"), "queue.sources")
    ]
    reviewed_present = sum(1 for source in sources if source["reviewed_receipt_present"])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "runner_readiness_ticket": 431,
        "provider": "datapan-registry",
        "inputs": {
            "queue": DEFAULT_QUEUE.as_posix(),
            "runner": DEFAULT_RUNNER.as_posix(),
            "runner_plan_schema_version": RUNNER_PLAN_SCHEMA_VERSION,
            "runner_plan_mode": "secret_free_default_ci_readiness",
            "environment_mode": "credential_env_values_not_read",
            "local_session_output": LOCAL_SESSION_OUTPUT,
            "local_session_review_plan_output": LOCAL_SESSION_REVIEW_PLAN_OUTPUT,
        },
        "summary": {
            "sources": len(sources),
            "credential_gated_sources": queue_summary.get("credential_gated_sources"),
            "candidate_batches_present": sum(1 for source in sources if source["candidate_batch_present"]),
            "candidate_batches_missing": sum(1 for source in sources if not source["candidate_batch_present"]),
            "ready_to_run_without_credentials": 0,
            "blocked_on_operator_env": sum(
                1 for source in sources if "missing_credential_env" in source["readiness_blockers"]
            ),
            "blocked_on_candidate_batch": sum(
                1 for source in sources if "missing_candidate_batch" in source["readiness_blockers"]
            ),
            "staged_receipts_present": sum(1 for source in sources if source["staged_receipt_present"]),
            "reviewed_receipts_present": reviewed_present,
            "reviewed_receipts_missing": len(sources) - reviewed_present,
            "default_ci_requires_credentials": False,
            "checked_in_secrets_allowed": False,
            "manual_review_reduction_allowed": queue_summary.get("manual_review_reduction_allowed"),
            "local_session_artifacts_checked_in": False,
            "runner_status": runner_status(sources),
            "release_evidence_status": "checked_in_runner_readiness_only",
        },
        "sources": sources,
    }


def validate_invariants(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    sources = [as_dict(source, "source") for source in as_list(report.get("sources"), "sources")]
    if summary.get("sources") != len(sources):
        raise ValueError("summary.sources must match sources length")
    if summary.get("manual_review_reduction_allowed") is not False:
        raise ValueError("runner readiness cannot allow manual-review reduction")
    if summary.get("default_ci_requires_credentials") is not False:
        raise ValueError("runner readiness must keep default CI secret-free")
    if summary.get("checked_in_secrets_allowed") is not False:
        raise ValueError("runner readiness must not allow checked-in secrets")
    if summary.get("ready_to_run_without_credentials") != 0:
        raise ValueError("default CI readiness must not mark credential-gated sources runnable")
    for source in sources:
        if source.get("credential_envs") != source.get("missing_credential_envs"):
            raise ValueError(f"{source.get('source_id')}: default CI must mark all credential envs missing")
        if source.get("credential_envs_present") is not False:
            raise ValueError(f"{source.get('source_id')}: credential envs must not be present in default CI report")
        if source.get("can_run_without_credentials") is not False:
            raise ValueError(f"{source.get('source_id')}: can_run_without_credentials must remain false")
        command = string_value(source.get("collection_command"), "source.collection_command")
        rendered = render_json(source)
        if "<secret>" in command or "<secret>" in rendered:
            raise ValueError(f"{source.get('source_id')}: readiness report contains a secret placeholder")


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
    parser.add_argument("--runner", default=DEFAULT_RUNNER, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when checked-in runner readiness evidence is stale")
    args = parser.parse_args()

    try:
        report = build_report(load_json(args.queue), load_runner(args.runner))
        validate_invariants(report)
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate credential runner readiness: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing credential runner readiness", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale credential runner readiness; "
                "run `python3 scripts/generate-credential-runtime-runner-readiness.py`",
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
