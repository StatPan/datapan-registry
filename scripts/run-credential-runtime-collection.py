#!/usr/bin/env python3
"""Preflight and run credential runtime collection from the reviewed receipt queue."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
from typing import Any

try:
    import jsonschema
except ImportError:  # pragma: no cover - operator environments may validate elsewhere
    jsonschema = None  # type: ignore[assignment]


DEFAULT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
DEFAULT_SESSION_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-collection-session.v1.schema.json")
DEFAULT_SESSION_OUTPUT = pathlib.Path(".datapan/runtime-evidence/credential-runtime-collection-session.json")


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


def queue_sources(queue: dict[str, Any]) -> list[dict[str, Any]]:
    return [as_dict(item, "queue.sources[]") for item in as_list(queue.get("sources"), "queue.sources")]


def selected_sources(queue: dict[str, Any], requested: list[str], all_sources: bool) -> list[dict[str, Any]]:
    sources = queue_sources(queue)
    if all_sources:
        return sources
    if not requested:
        raise ValueError("select at least one --source or pass --all")
    by_id = {string_value(source.get("source_id"), "source.source_id"): source for source in sources}
    missing = sorted(source_id for source_id in requested if source_id not in by_id)
    if missing:
        raise ValueError(f"unknown queue source(s): {', '.join(missing)}")
    return [by_id[source_id] for source_id in requested]


def command_args(entry: dict[str, Any]) -> list[str]:
    command = string_value(entry.get("operator_command"), "source.operator_command")
    parts = shlex.split(command)
    return [part for part in parts if not part.endswith("=<secret>")]


def env_names(entry: dict[str, Any]) -> list[str]:
    names: list[str] = []
    command = string_value(entry.get("operator_command"), "source.operator_command")
    for part in shlex.split(command):
        if part.endswith("=<secret>"):
            names.append(part.split("=", 1)[0])
    if not names:
        raise ValueError(f"{entry.get('source_id')} operator command declares no credential env placeholders")
    return names


def source_plan(entry: dict[str, Any]) -> dict[str, Any]:
    source_id = string_value(entry.get("source_id"), "source.source_id")
    candidate_batch = pathlib.Path(string_value(entry.get("candidate_batch"), f"{source_id}.candidate_batch"))
    staged_path = pathlib.Path(string_value(entry.get("staged_receipt_path"), f"{source_id}.staged_receipt_path"))
    reviewed_path = pathlib.Path(string_value(entry.get("reviewed_receipt_path"), f"{source_id}.reviewed_receipt_path"))
    envs = env_names(entry)
    missing_envs = [name for name in envs if not os.environ.get(name)]
    command = command_args(entry)
    if not command or command[0] != "datapan":
        raise ValueError(f"{source_id} collection command must start with datapan")
    candidate_exists = candidate_batch.is_file()
    reviewed_exists = reviewed_path.is_file()
    return {
        "source_id": source_id,
        "credential_envs": envs,
        "credential_envs_present": not missing_envs,
        "missing_credential_envs": missing_envs,
        "candidate_batch": candidate_batch.as_posix(),
        "candidate_batch_present": candidate_exists,
        "staged_receipt_path": staged_path.as_posix(),
        "staged_receipt_present": staged_path.is_file(),
        "reviewed_receipt_path": reviewed_path.as_posix(),
        "reviewed_receipt_present": reviewed_exists,
        "can_run": not missing_envs and candidate_exists and not reviewed_exists,
        "collection_command": " ".join(shlex.quote(part) for part in command),
        "staged_receipt_validation_command": string_value(
            entry.get("staged_receipt_validation_command"),
            f"{source_id}.staged_receipt_validation_command",
        ),
        "reviewed_receipt_promotion_command": string_value(
            entry.get("reviewed_receipt_promotion_command"),
            f"{source_id}.reviewed_receipt_promotion_command",
        ),
    }


def build_plan(queue: dict[str, Any], requested: list[str], all_sources: bool) -> dict[str, Any]:
    sources = [source_plan(source) for source in selected_sources(queue, requested, all_sources)]
    return {
        "schema_version": "datapan.credential-runtime-collection-runner-plan.v1",
        "queue": DEFAULT_QUEUE.as_posix(),
        "sources": sources,
        "summary": {
            "sources": len(sources),
            "ready_to_run": sum(1 for source in sources if source["can_run"]),
            "missing_env": sum(1 for source in sources if source["missing_credential_envs"]),
            "missing_candidate_batch": sum(1 for source in sources if not source["candidate_batch_present"]),
            "staged_receipts_present": sum(1 for source in sources if source["staged_receipt_present"]),
            "reviewed_receipts_present": sum(1 for source in sources if source["reviewed_receipt_present"]),
        },
    }


def validate_plan(plan: dict[str, Any], *, require_env: bool) -> None:
    failures: list[str] = []
    for source in as_list(plan.get("sources"), "plan.sources"):
        entry = as_dict(source, "plan.sources[]")
        source_id = string_value(entry.get("source_id"), "plan.source_id")
        if not entry.get("candidate_batch_present"):
            failures.append(f"{source_id}: candidate batch missing")
        if require_env and entry.get("missing_credential_envs"):
            failures.append(f"{source_id}: credential env missing")
    if failures:
        raise ValueError("; ".join(failures))


def run_source(entry: dict[str, Any], *, force: bool) -> dict[str, Any]:
    plan = source_plan(entry)
    source_id = string_value(entry.get("source_id"), "source.source_id")
    if plan["missing_credential_envs"]:
        raise ValueError(f"{source_id}: missing credential env(s)")
    if not plan["candidate_batch_present"]:
        raise ValueError(f"{source_id}: candidate batch is missing")
    if plan["reviewed_receipt_present"] and not force:
        raise ValueError(f"{source_id}: reviewed receipt already exists; pass --force to run anyway")
    pathlib.Path(plan["staged_receipt_path"]).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command_args(entry), check=True)
    subprocess.run(shlex.split(plan["staged_receipt_validation_command"]), check=True)
    return {
        "source_id": source_id,
        "status": "succeeded",
        "staged_receipt_path": plan["staged_receipt_path"],
        "next_action": "review_and_promote_staged_receipt",
        "reviewed_receipt_promotion_command": plan["reviewed_receipt_promotion_command"],
    }


def readiness_blockers(plan: dict[str, Any], *, force: bool) -> list[str]:
    blockers: list[str] = []
    if plan["missing_credential_envs"]:
        blockers.append("missing_credential_env")
    if not plan["candidate_batch_present"]:
        blockers.append("missing_candidate_batch")
    if plan["reviewed_receipt_present"] and not force:
        blockers.append("reviewed_receipt_present")
    return blockers


def skipped_result(plan: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    return {
        "source_id": plan["source_id"],
        "status": "skipped_not_ready",
        "reasons": blockers,
        "missing_credential_envs": plan["missing_credential_envs"],
        "candidate_batch": plan["candidate_batch"],
        "candidate_batch_present": plan["candidate_batch_present"],
        "reviewed_receipt_path": plan["reviewed_receipt_path"],
        "reviewed_receipt_present": plan["reviewed_receipt_present"],
        "next_action": "resolve_readiness_then_rerun_batch",
    }


def failed_result(entry: dict[str, Any], exc: Exception) -> dict[str, Any]:
    source_id = string_value(entry.get("source_id"), "source.source_id")
    return {
        "source_id": source_id,
        "status": "failed",
        "error": str(exc),
        "next_action": "inspect_source_error_then_rerun_or_keep_manual_review_boundary",
    }


def build_session(results: list[dict[str, Any]], *, queue_path: pathlib.Path) -> dict[str, Any]:
    return {
        "schema_version": "datapan.credential-runtime-collection-session.v1",
        "queue": queue_path.as_posix(),
        "summary": {
            "sources": len(results),
            "succeeded": sum(1 for result in results if result["status"] == "succeeded"),
            "skipped_not_ready": sum(1 for result in results if result["status"] == "skipped_not_ready"),
            "failed": sum(1 for result in results if result["status"] == "failed"),
            "checked_in_secrets_allowed": False,
            "next_action": "review_and_promote_staged_receipts",
        },
        "results": results,
    }


def validate_session_schema(session: dict[str, Any], schema_path: pathlib.Path, *, required: bool = False) -> None:
    if jsonschema is None:
        if required:
            raise ValueError("jsonschema is required to validate credential runtime collection sessions")
        return
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(session), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("; ".join(rendered))


def redact_known_env_values(message: str, entry: dict[str, Any]) -> str:
    redacted = message
    for name in env_names(entry):
        value = os.environ.get(name)
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted.replace("<secret>", "<redacted>")


def run_sources(
    entries: list[dict[str, Any]],
    *,
    queue_path: pathlib.Path,
    session_schema: pathlib.Path,
    force: bool,
    skip_not_ready: bool,
    continue_on_error: bool,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for entry in entries:
        plan = source_plan(entry)
        blockers = readiness_blockers(plan, force=force)
        if blockers:
            if skip_not_ready:
                results.append(skipped_result(plan, blockers))
                continue
            raise ValueError(f"{plan['source_id']}: not ready ({', '.join(blockers)})")
        try:
            results.append(run_source(entry, force=force))
        except Exception as exc:  # noqa: BLE001 - batch sessions must preserve per-source failure state
            if not continue_on_error:
                raise
            results.append(failed_result(entry, RuntimeError(redact_known_env_values(str(exc), entry))))
    session = build_session(results, queue_path=queue_path)
    validate_session_schema(session, session_schema)
    return session


def sample_queue(root: pathlib.Path) -> dict[str, Any]:
    candidate = root / "candidate.json"
    candidate.write_text("{}", encoding="utf-8")
    missing_candidate = root / "missing-candidate.json"

    def source(source_id: str, command_arg: str, env_name: str, candidate_path: pathlib.Path) -> dict[str, str]:
        staged = root / f"{source_id}-staged.json"
        reviewed = root / f"{source_id}-reviewed.json"
        return {
            "source_id": source_id,
            "operator_command": f"datapan {command_arg} {env_name}=<secret>",
            "candidate_batch": candidate_path.as_posix(),
            "staged_receipt_path": staged.as_posix(),
            "reviewed_receipt_path": reviewed.as_posix(),
            "staged_receipt_validation_command": "python3 -c 'pass'",
            "reviewed_receipt_promotion_command": f"promote {staged.as_posix()}",
        }

    return {
        "sources": [
            source("ready", "ok", "READY_TOKEN", candidate),
            source("missing_env", "ok", "MISSING_TOKEN", candidate),
            source("missing_candidate", "ok", "READY_TOKEN", missing_candidate),
            source("failing", "fail", "READY_TOKEN", candidate),
        ]
    }


def run_self_test(queue: dict[str, Any]) -> None:
    plan = build_plan(queue, [], True)
    validate_plan(plan, require_env=False)
    if not plan["sources"]:
        raise ValueError("self-test requires at least one queue source")
    for source in plan["sources"]:
        command = source["collection_command"]
        if "<secret>" in command:
            raise ValueError("self-test failed: collection command includes secret placeholder")
        if not source["credential_envs"]:
            raise ValueError("self-test failed: source missing credential envs")

    with tempfile.TemporaryDirectory() as temp_dir:
        root = pathlib.Path(temp_dir)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake_datapan = bin_dir / "datapan"
        fake_datapan.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"fail\" ]; then exit 7; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_datapan.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        old_ready = os.environ.get("READY_TOKEN")
        old_missing = os.environ.get("MISSING_TOKEN")
        try:
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
            os.environ["READY_TOKEN"] = "self-test-secret"
            os.environ.pop("MISSING_TOKEN", None)
            synthetic_queue = sample_queue(root)
            session = run_sources(
                queue_sources(synthetic_queue),
                queue_path=pathlib.Path("synthetic-queue.json"),
                session_schema=DEFAULT_SESSION_SCHEMA,
                force=False,
                skip_not_ready=True,
                continue_on_error=True,
            )
            validate_session_schema(session, DEFAULT_SESSION_SCHEMA, required=True)
            summary = as_dict(session.get("summary"), "session.summary")
            if summary.get("succeeded") != 1:
                raise ValueError("self-test failed: expected one succeeded batch source")
            if summary.get("skipped_not_ready") != 2:
                raise ValueError("self-test failed: expected two skipped batch sources")
            if summary.get("failed") != 1:
                raise ValueError("self-test failed: expected one failed batch source")
            rendered = render_json(session)
            if "self-test-secret" in rendered or "<secret>" in rendered:
                raise ValueError("self-test failed: batch session leaked credential material")
            output_path = root / "session-output.json"
            write_json(output_path, session)
            reloaded = load_json(output_path)
            validate_session_schema(reloaded, DEFAULT_SESSION_SCHEMA, required=True)
        finally:
            os.environ["PATH"] = old_path
            if old_ready is None:
                os.environ.pop("READY_TOKEN", None)
            else:
                os.environ["READY_TOKEN"] = old_ready
            if old_missing is None:
                os.environ.pop("MISSING_TOKEN", None)
            else:
                os.environ["MISSING_TOKEN"] = old_missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", default=DEFAULT_QUEUE, type=pathlib.Path)
    parser.add_argument("--session-schema", default=DEFAULT_SESSION_SCHEMA, type=pathlib.Path)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--all", action="store_true", help="select all queue sources")
    parser.add_argument("--require-env", action="store_true", help="fail preflight when credential env vars are absent")
    parser.add_argument("--run", action="store_true", help="execute selected credential runtime checks")
    parser.add_argument("--force", action="store_true", help="allow run mode even when reviewed receipts already exist")
    parser.add_argument("--skip-not-ready", action="store_true", help="skip selected sources that are not ready to run")
    parser.add_argument("--continue-on-error", action="store_true", help="preserve per-source failures and continue a batch run")
    parser.add_argument(
        "--session-output",
        type=pathlib.Path,
        help=f"write batch session JSON to a local handoff path such as {DEFAULT_SESSION_OUTPUT.as_posix()}",
    )
    parser.add_argument("--json", action="store_true", help="print JSON output")
    parser.add_argument("--check", action="store_true", help="validate queue-derived runner plan without requiring env vars")
    parser.add_argument("--self-test", action="store_true", help="run secret-free runner self-tests")
    args = parser.parse_args()

    try:
        queue = load_json(args.queue)
        if args.self_test:
            run_self_test(queue)
            print("ok credential runtime collection runner self-tests")
            return 0
        if args.check:
            plan = build_plan(queue, [], True)
            validate_plan(plan, require_env=False)
            print(f"ok credential runtime collection runner plan (sources={plan['summary']['sources']})")
            return 0
        if args.run:
            selected = selected_sources(queue, args.source, args.all)
            session = run_sources(
                selected,
                queue_path=args.queue,
                session_schema=args.session_schema,
                force=args.force,
                skip_not_ready=args.skip_not_ready,
                continue_on_error=args.continue_on_error,
            )
            if args.session_output:
                write_json(args.session_output, session)
            if args.json:
                print(render_json(session), end="")
            else:
                if args.session_output:
                    print(f"wrote batch session: {args.session_output.as_posix()}")
                for result in as_list(session.get("results"), "session.results"):
                    entry = as_dict(result, "session.results[]")
                    if entry["status"] == "succeeded":
                        print(f"wrote staged receipt: {entry['staged_receipt_path']}")
                        print(f"next: {entry['reviewed_receipt_promotion_command']}")
                    elif entry["status"] == "skipped_not_ready":
                        print(f"skipped {entry['source_id']}: {', '.join(entry['reasons'])}")
                    else:
                        print(f"failed {entry['source_id']}: {entry['error']}", file=sys.stderr)
            return 0
        plan = build_plan(queue, args.source, args.all)
        validate_plan(plan, require_env=args.require_env)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL credential runtime collection runner: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(render_json(plan), end="")
    else:
        summary = as_dict(plan.get("summary"), "plan.summary")
        print(
            "credential runtime collection preflight "
            f"(sources={summary['sources']}, ready_to_run={summary['ready_to_run']}, "
            f"missing_env={summary['missing_env']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
