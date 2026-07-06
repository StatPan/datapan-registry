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
from typing import Any


DEFAULT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")


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
        "staged_receipt_path": plan["staged_receipt_path"],
        "next_action": "review_and_promote_staged_receipt",
        "reviewed_receipt_promotion_command": plan["reviewed_receipt_promotion_command"],
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", default=DEFAULT_QUEUE, type=pathlib.Path)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--all", action="store_true", help="select all queue sources")
    parser.add_argument("--require-env", action="store_true", help="fail preflight when credential env vars are absent")
    parser.add_argument("--run", action="store_true", help="execute selected credential runtime checks")
    parser.add_argument("--force", action="store_true", help="allow run mode even when reviewed receipts already exist")
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
            results = [run_source(source, force=args.force) for source in selected]
            if args.json:
                print(render_json({"results": results}), end="")
            else:
                for result in results:
                    print(f"wrote staged receipt: {result['staged_receipt_path']}")
                    print(f"next: {result['reviewed_receipt_promotion_command']}")
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
