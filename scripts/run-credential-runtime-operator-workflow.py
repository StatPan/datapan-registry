#!/usr/bin/env python3
"""Plan or run the credential runtime operator workflow."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
from typing import Any


DEFAULT_EXECUTION_PLAN = pathlib.Path("reports/credential-runtime-collection-execution-plan.json")
DEFAULT_SESSION_OUTPUT = pathlib.Path(".datapan/runtime-evidence/credential-runtime-collection-session.json")
DEFAULT_REVIEW_PLAN_OUTPUT = pathlib.Path(".datapan/runtime-evidence/credential-runtime-session-review-plan.json")
DEFAULT_QUEUE = pathlib.Path("reports/credential-runtime-receipt-collection-queue.json")
WORKFLOW_SCRIPT = pathlib.Path("scripts/run-credential-runtime-operator-workflow.py")
SCHEMA_VERSION = "datapan.credential-runtime-operator-workflow.v1"


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


def count_value(value: object, label: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def command_step(
    step_id: str,
    argv: list[str],
    *,
    requires_credentials: bool,
    writes_local_artifact: bool,
    checked_in_artifact_allowed: bool,
    purpose: str,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "command": shlex.join(argv),
        "argv": argv,
        "requires_credentials": requires_credentials,
        "writes_local_artifact": writes_local_artifact,
        "checked_in_artifact_allowed": checked_in_artifact_allowed,
        "purpose": purpose,
    }


def build_steps(
    *,
    session_output: pathlib.Path,
    review_plan_output: pathlib.Path,
    queue_path: pathlib.Path,
) -> list[dict[str, Any]]:
    session = session_output.as_posix()
    review_plan = review_plan_output.as_posix()
    queue = queue_path.as_posix()
    return [
        command_step(
            "secret_free_batch_preflight",
            ["python3", "scripts/run-credential-runtime-collection.py", "--all", "--json"],
            requires_credentials=False,
            writes_local_artifact=False,
            checked_in_artifact_allowed=False,
            purpose="Show the batch collection plan without requiring credential env vars.",
        ),
        command_step(
            "require_env_batch_preflight",
            ["python3", "scripts/run-credential-runtime-collection.py", "--all", "--require-env"],
            requires_credentials=True,
            writes_local_artifact=False,
            checked_in_artifact_allowed=False,
            purpose="Fail before collection if required operator credential env vars are absent.",
        ),
        command_step(
            "batch_collection_session",
            [
                "python3",
                "scripts/run-credential-runtime-collection.py",
                "--all",
                "--run",
                "--skip-not-ready",
                "--continue-on-error",
                "--session-output",
                session,
                "--json",
            ],
            requires_credentials=True,
            writes_local_artifact=True,
            checked_in_artifact_allowed=False,
            purpose="Run credentialed checks and write the local redacted batch session handoff.",
        ),
        command_step(
            "session_validation",
            [
                "python3",
                "scripts/validate-credential-runtime-collection-session.py",
                session,
                "--require-complete-source-set",
            ],
            requires_credentials=False,
            writes_local_artifact=False,
            checked_in_artifact_allowed=False,
            purpose="Validate the local session output before review planning.",
        ),
        command_step(
            "review_plan_generation",
            [
                "python3",
                "scripts/generate-credential-runtime-session-review-plan.py",
                session,
                "--output",
                review_plan,
            ],
            requires_credentials=False,
            writes_local_artifact=True,
            checked_in_artifact_allowed=False,
            purpose="Generate the local reviewer plan from the validated session output.",
        ),
        command_step(
            "review_plan_validation",
            [
                "python3",
                "scripts/validate-credential-runtime-session-review-plan.py",
                review_plan,
                "--queue",
                queue,
            ],
            requires_credentials=False,
            writes_local_artifact=False,
            checked_in_artifact_allowed=False,
            purpose="Validate the local review plan against the checked-in receipt queue.",
        ),
    ]


def credential_env_names(execution_plan: dict[str, Any]) -> list[str]:
    environment = as_dict(execution_plan.get("operator_environment"), "execution_plan.operator_environment")
    return [
        str(item)
        for item in execution_plan_envs(environment)
    ]


def execution_plan_envs(environment: dict[str, Any]) -> list[str]:
    envs = environment.get("required_credential_envs")
    if not isinstance(envs, list) or not envs:
        raise ValueError("execution_plan.operator_environment.required_credential_envs must be a non-empty array")
    result = []
    for index, item in enumerate(envs):
        if not isinstance(item, str) or not item:
            raise ValueError(f"required_credential_envs[{index}] must be a non-empty string")
        result.append(item)
    return result


def credential_environment_status(
    execution_plan: dict[str, Any],
    *,
    environment: Any,
) -> dict[str, Any]:
    required_envs = credential_env_names(execution_plan)
    present_envs = [name for name in required_envs if bool(environment.get(name))]
    missing_envs = [name for name in required_envs if name not in present_envs]
    sources = []
    for raw_source in execution_plan.get("sources", []):
        source = as_dict(raw_source, "execution_plan.sources[]")
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("execution_plan.sources[].source_id must be a non-empty string")
        source_envs = source.get("credential_envs")
        if not isinstance(source_envs, list) or not source_envs:
            raise ValueError(f"{source_id}.credential_envs must be a non-empty array")
        source_required = []
        for index, item in enumerate(source_envs):
            if not isinstance(item, str) or not item:
                raise ValueError(f"{source_id}.credential_envs[{index}] must be a non-empty string")
            source_required.append(item)
        source_present = [name for name in source_required if name in present_envs]
        source_missing = [name for name in source_required if name not in present_envs]
        sources.append(
            {
                "source_id": source_id,
                "required_credential_envs": source_required,
                "present_credential_envs": source_present,
                "missing_credential_envs": source_missing,
                "credential_env_ready": not source_missing,
            }
        )
    return {
        "required_credential_envs": required_envs,
        "required_credential_env_count": len(required_envs),
        "present_credential_envs": present_envs,
        "present_credential_env_count": len(present_envs),
        "missing_credential_envs": missing_envs,
        "missing_credential_env_count": len(missing_envs),
        "current_operator_env_ready": not missing_envs,
        "checked_in_credentials_allowed": False,
        "secret_values_included": False,
        "sources": sources,
    }


def build_workflow(
    *,
    execution_plan: dict[str, Any],
    execution_plan_path: pathlib.Path,
    session_output: pathlib.Path,
    review_plan_output: pathlib.Path,
    queue_path: pathlib.Path,
    run: bool,
    environment: Any | None = None,
) -> dict[str, Any]:
    plan_summary = as_dict(execution_plan.get("summary"), "execution_plan.summary")
    batch = as_dict(execution_plan.get("batch_execution"), "execution_plan.batch_execution")
    env_status = credential_environment_status(
        execution_plan,
        environment=os.environ if environment is None else environment,
    )
    steps = build_steps(
        session_output=session_output,
        review_plan_output=review_plan_output,
        queue_path=queue_path,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_plan": execution_plan_path.as_posix(),
        "workflow_script": WORKFLOW_SCRIPT.as_posix(),
        "workflow_plan_command": f"python3 {WORKFLOW_SCRIPT.as_posix()} --json",
        "workflow_run_command": f"python3 {WORKFLOW_SCRIPT.as_posix()} --run --json",
        "workflow_self_test_command": f"python3 {WORKFLOW_SCRIPT.as_posix()} --self-test",
        "run_mode": "run" if run else "plan",
        "summary": {
            "session_plan_status": plan_summary.get("session_plan_status"),
            "next_action": plan_summary.get("next_action"),
            "operator_sources_ready": count_value(
                plan_summary.get("operator_sources_ready"),
                "execution_plan.summary.operator_sources_ready",
            ),
            "reviewed_receipts_missing": count_value(
                plan_summary.get("reviewed_receipts_missing"),
                "execution_plan.summary.reviewed_receipts_missing",
            ),
            "required_credential_env_count": env_status["required_credential_env_count"],
            "present_credential_env_count": env_status["present_credential_env_count"],
            "missing_credential_env_count": env_status["missing_credential_env_count"],
            "current_operator_env_ready": env_status["current_operator_env_ready"],
            "requires_operator_credentials": True,
            "workflow_run_requires_explicit_run": True,
            "default_ci_requires_credentials": False,
            "checked_in_secrets_allowed": False,
            "checked_in_session_output_allowed": bool_value(
                batch.get("checked_in_session_output_allowed"),
                "execution_plan.batch_execution.checked_in_session_output_allowed",
            ),
            "checked_in_review_plan_allowed": bool_value(
                batch.get("checked_in_review_plan_allowed"),
                "execution_plan.batch_execution.checked_in_review_plan_allowed",
            ),
            "goal_closure_allowed": bool_value(
                plan_summary.get("goal_closure_allowed"),
                "execution_plan.summary.goal_closure_allowed",
            ),
        },
        "local_artifacts": {
            "session_output_path": session_output.as_posix(),
            "session_review_plan_output_path": review_plan_output.as_posix(),
            "checked_in_session_output_allowed": False,
            "checked_in_review_plan_allowed": False,
        },
        "operator_environment": env_status,
        "steps": steps,
        "execution": {
            "executed": False,
            "results": [],
        },
    }


def validate_workflow(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    artifacts = as_dict(report.get("local_artifacts"), "local_artifacts")
    environment = as_dict(report.get("operator_environment"), "operator_environment")
    steps = report.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty array")
    step_ids = [as_dict(step, "steps[]").get("id") for step in steps]
    expected = [
        "secret_free_batch_preflight",
        "require_env_batch_preflight",
        "batch_collection_session",
        "session_validation",
        "review_plan_generation",
        "review_plan_validation",
    ]
    if step_ids != expected:
        raise ValueError("workflow steps are not in the required operator order")
    for key in (
        "workflow_run_requires_explicit_run",
        "requires_operator_credentials",
    ):
        if summary.get(key) is not True:
            raise ValueError(f"summary.{key} must remain true")
    for key in (
        "default_ci_requires_credentials",
        "checked_in_secrets_allowed",
        "checked_in_session_output_allowed",
        "checked_in_review_plan_allowed",
        "goal_closure_allowed",
    ):
        if summary.get(key) is not False:
            raise ValueError(f"summary.{key} must remain false")
    for key in ("checked_in_session_output_allowed", "checked_in_review_plan_allowed"):
        if artifacts.get(key) is not False:
            raise ValueError(f"local_artifacts.{key} must remain false")
    if environment.get("checked_in_credentials_allowed") is not False:
        raise ValueError("operator_environment.checked_in_credentials_allowed must remain false")
    if environment.get("secret_values_included") is not False:
        raise ValueError("operator_environment.secret_values_included must remain false")
    for key in (
        "required_credential_env_count",
        "present_credential_env_count",
        "missing_credential_env_count",
    ):
        count_value(environment.get(key), f"operator_environment.{key}")
        if summary.get(key) != environment.get(key):
            raise ValueError(f"summary.{key} must match operator_environment.{key}")
    required_envs = environment.get("required_credential_envs")
    present_envs = environment.get("present_credential_envs")
    missing_envs = environment.get("missing_credential_envs")
    if not isinstance(required_envs, list) or not isinstance(present_envs, list) or not isinstance(missing_envs, list):
        raise ValueError("operator_environment env lists must be arrays")
    if len(required_envs) != environment["required_credential_env_count"]:
        raise ValueError("required credential env count must match required env list")
    if len(present_envs) != environment["present_credential_env_count"]:
        raise ValueError("present credential env count must match present env list")
    if len(missing_envs) != environment["missing_credential_env_count"]:
        raise ValueError("missing credential env count must match missing env list")
    if summary.get("current_operator_env_ready") != environment.get("current_operator_env_ready"):
        raise ValueError("summary.current_operator_env_ready must match operator_environment")
    if environment.get("current_operator_env_ready") != (environment["missing_credential_env_count"] == 0):
        raise ValueError("operator environment readiness must be derived from missing env count")
    sources = environment.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("operator_environment.sources must be a non-empty array")
    for raw_source in sources:
        source = as_dict(raw_source, "operator_environment.sources[]")
        source_missing = source.get("missing_credential_envs")
        if not isinstance(source_missing, list):
            raise ValueError("operator environment source missing envs must be an array")
        if source.get("credential_env_ready") != (len(source_missing) == 0):
            raise ValueError("source credential_env_ready must be derived from missing envs")
    batch_step = as_dict(steps[2], "steps[2]")
    if "--run" not in batch_step.get("argv", []):
        raise ValueError("batch collection step must require --run")
    if "--session-output" not in batch_step.get("argv", []):
        raise ValueError("batch collection step must write an explicit session output")
    for step in steps:
        entry = as_dict(step, "steps[]")
        if entry.get("checked_in_artifact_allowed") is not False:
            raise ValueError(f"{entry.get('id')} must not allow checked-in local artifacts")
    rendered = render_json(report).lower()
    for marker in ("authorization:", "bearer ", "service_key=", "api_key=", "secret=", "token="):
        if marker in rendered:
            raise ValueError(f"workflow plan must not contain secret-like marker {marker!r}")


def run_steps(report: dict[str, Any], *, json_mode: bool) -> dict[str, Any]:
    execution = {"executed": True, "results": []}
    for step in report["steps"]:
        entry = as_dict(step, "steps[]")
        argv = entry.get("argv")
        if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
            raise ValueError(f"{entry.get('id')} argv must be an array of strings")
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
                "id": entry["id"],
                "command": entry["command"],
                "returncode": result.returncode,
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": stderr_bytes,
            }
        )
        if result.returncode != 0:
            raise RuntimeError(f"workflow step failed: {entry['id']} ({result.returncode})")
    return execution


def require_operator_env(report: dict[str, Any], *, json_mode: bool, emit: bool = True) -> int:
    environment = as_dict(report.get("operator_environment"), "operator_environment")
    missing_envs = environment.get("missing_credential_envs")
    if not isinstance(missing_envs, list):
        raise ValueError("operator_environment.missing_credential_envs must be an array")
    result = {
        "schema_version": "datapan.credential-runtime-operator-env-gate.v1",
        "required_credential_envs": environment.get("required_credential_envs", []),
        "present_credential_envs": environment.get("present_credential_envs", []),
        "missing_credential_envs": missing_envs,
        "required_credential_env_count": environment.get("required_credential_env_count"),
        "present_credential_env_count": environment.get("present_credential_env_count"),
        "missing_credential_env_count": environment.get("missing_credential_env_count"),
        "current_operator_env_ready": environment.get("current_operator_env_ready"),
        "secret_values_included": False,
    }
    if not missing_envs:
        if emit:
            if json_mode:
                print(render_json(result), end="")
            else:
                print("ok credential runtime operator env gate")
        return 0
    if emit:
        if json_mode:
            print(render_json(result), end="")
        else:
            print(
                "FAIL credential runtime operator env gate: missing "
                + ", ".join(str(item) for item in missing_envs),
                file=sys.stderr,
            )
    return 1


def self_test(plan_path: pathlib.Path) -> None:
    plan = load_json(plan_path)
    report = build_workflow(
        execution_plan=plan,
        execution_plan_path=plan_path,
        session_output=DEFAULT_SESSION_OUTPUT,
        review_plan_output=DEFAULT_REVIEW_PLAN_OUTPUT,
        queue_path=DEFAULT_QUEUE,
        run=False,
        environment={},
    )
    validate_workflow(report)
    if report["summary"]["current_operator_env_ready"] is not False:
        raise ValueError("self-test expected empty env to be not ready")
    if report["summary"]["missing_credential_env_count"] != report["summary"]["required_credential_env_count"]:
        raise ValueError("self-test expected every credential env to be missing")
    sample_env = {report["operator_environment"]["required_credential_envs"][0]: "redacted-value"}
    partial_report = build_workflow(
        execution_plan=plan,
        execution_plan_path=plan_path,
        session_output=DEFAULT_SESSION_OUTPUT,
        review_plan_output=DEFAULT_REVIEW_PLAN_OUTPUT,
        queue_path=DEFAULT_QUEUE,
        run=False,
        environment=sample_env,
    )
    validate_workflow(partial_report)
    if partial_report["summary"]["present_credential_env_count"] != 1:
        raise ValueError("self-test expected one present credential env name")
    if "redacted-value" in render_json(partial_report):
        raise ValueError("self-test leaked an env value into the workflow plan")
    if require_operator_env(report, json_mode=True, emit=False) != 1:
        raise ValueError("self-test expected empty env gate to fail")
    complete_env = {name: "redacted-value" for name in report["operator_environment"]["required_credential_envs"]}
    complete_report = build_workflow(
        execution_plan=plan,
        execution_plan_path=plan_path,
        session_output=DEFAULT_SESSION_OUTPUT,
        review_plan_output=DEFAULT_REVIEW_PLAN_OUTPUT,
        queue_path=DEFAULT_QUEUE,
        run=False,
        environment=complete_env,
    )
    validate_workflow(complete_report)
    if require_operator_env(complete_report, json_mode=True, emit=False) != 0:
        raise ValueError("self-test expected complete env gate to pass")
    if "redacted-value" in render_json(complete_report):
        raise ValueError("self-test leaked a complete env value into the workflow plan")
    run_report = build_workflow(
        execution_plan=plan,
        execution_plan_path=plan_path,
        session_output=DEFAULT_SESSION_OUTPUT,
        review_plan_output=DEFAULT_REVIEW_PLAN_OUTPUT,
        queue_path=DEFAULT_QUEUE,
        run=True,
        environment={},
    )
    validate_workflow(run_report)
    if run_report["run_mode"] != "run":
        raise ValueError("self-test failed: run workflow did not report run mode")


def print_human(report: dict[str, Any]) -> None:
    summary = as_dict(report.get("summary"), "summary")
    print(
        "credential runtime operator workflow "
        f"(mode={report['run_mode']}, status={summary['session_plan_status']}, "
        f"missing_receipts={summary['reviewed_receipts_missing']})"
    )
    environment = as_dict(report.get("operator_environment"), "operator_environment")
    missing_envs = environment.get("missing_credential_envs")
    if isinstance(missing_envs, list) and missing_envs:
        print(f"missing credential envs: {', '.join(str(item) for item in missing_envs)}")
    print(f"run command: {report['workflow_run_command']}")
    for step in report["steps"]:
        entry = as_dict(step, "steps[]")
        print(f"- {entry['id']}: {entry['command']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-plan", default=DEFAULT_EXECUTION_PLAN, type=pathlib.Path)
    parser.add_argument("--session-output", default=DEFAULT_SESSION_OUTPUT, type=pathlib.Path)
    parser.add_argument("--review-plan-output", default=DEFAULT_REVIEW_PLAN_OUTPUT, type=pathlib.Path)
    parser.add_argument("--queue", default=DEFAULT_QUEUE, type=pathlib.Path)
    parser.add_argument("--run", action="store_true", help="execute the operator workflow")
    parser.add_argument("--json", action="store_true", help="print JSON output")
    parser.add_argument("--check", action="store_true", help="validate the workflow plan without credentials")
    parser.add_argument("--require-env", action="store_true", help="fail unless all required credential env vars are present")
    parser.add_argument("--self-test", action="store_true", help="run credential-free workflow self-tests")
    args = parser.parse_args()

    try:
        if args.self_test:
            self_test(args.execution_plan)
            print("ok credential runtime operator workflow self-test")
            return 0
        report = build_workflow(
            execution_plan=load_json(args.execution_plan),
            execution_plan_path=args.execution_plan,
            session_output=args.session_output,
            review_plan_output=args.review_plan_output,
            queue_path=args.queue,
            run=args.run,
        )
        validate_workflow(report)
        if args.check:
            print(
                "ok credential runtime operator workflow "
                f"(status={report['summary']['session_plan_status']})"
            )
            return 0
        if args.require_env:
            return require_operator_env(report, json_mode=args.json)
        if args.run:
            report["execution"] = run_steps(report, json_mode=args.json)
    except Exception as exc:  # noqa: BLE001 - operators need the failed invariant
        print(f"FAIL credential runtime operator workflow: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(render_json(report), end="")
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
