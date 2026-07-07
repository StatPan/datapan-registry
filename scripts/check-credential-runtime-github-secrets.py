#!/usr/bin/env python3
"""Check GitHub repository secret readiness for credential runtime collection."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from typing import Any


DEFAULT_EXECUTION_PLAN = pathlib.Path("reports/credential-runtime-collection-execution-plan.json")
DEFAULT_REPO = "StatPan/datapan-registry"
SCHEMA_VERSION = "datapan.credential-runtime-github-secret-readiness.v1"


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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


def required_envs(execution_plan: dict[str, Any]) -> list[str]:
    environment = as_dict(execution_plan.get("operator_environment"), "execution_plan.operator_environment")
    envs = [
        string_value(item, "operator_environment.required_credential_envs[]")
        for item in as_list(environment.get("required_credential_envs"), "operator_environment.required_credential_envs")
    ]
    if not envs:
        raise ValueError("operator_environment.required_credential_envs must not be empty")
    return envs


def load_secret_entries(repo: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["gh", "secret", "list", "--repo", repo, "--json", "name,updatedAt"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh secret list failed")
    value = json.loads(result.stdout)
    if not isinstance(value, list):
        raise ValueError("gh secret list output must be an array")
    return [as_dict(item, "gh_secret_list[]") for item in value]


def build_report(*, repo: str, required: list[str], secret_entries: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {
        string_value(entry.get("name"), "secret.name"): entry
        for entry in secret_entries
    }
    present = [name for name in required if name in by_name]
    missing = [name for name in required if name not in by_name]
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "source": "gh secret list",
        "required_credential_envs": required,
        "present_credential_envs": present,
        "missing_credential_envs": missing,
        "required_credential_env_count": len(required),
        "present_credential_env_count": len(present),
        "missing_credential_env_count": len(missing),
        "current_operator_env_ready": not missing,
        "secret_values_included": False,
        "present_secret_metadata": [
            {
                "name": name,
                "updatedAt": by_name[name].get("updatedAt", ""),
            }
            for name in present
        ],
    }


def validate_report(report: dict[str, Any]) -> None:
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unexpected schema_version")
    required = as_list(report.get("required_credential_envs"), "required_credential_envs")
    present = as_list(report.get("present_credential_envs"), "present_credential_envs")
    missing = as_list(report.get("missing_credential_envs"), "missing_credential_envs")
    if len(required) != report.get("required_credential_env_count"):
        raise ValueError("required count mismatch")
    if len(present) != report.get("present_credential_env_count"):
        raise ValueError("present count mismatch")
    if len(missing) != report.get("missing_credential_env_count"):
        raise ValueError("missing count mismatch")
    if report.get("current_operator_env_ready") != (len(missing) == 0):
        raise ValueError("current_operator_env_ready must derive from missing count")
    if report.get("secret_values_included") is not False:
        raise ValueError("secret_values_included must remain false")
    rendered = render_json(report).lower()
    for marker in ("authorization:", "bearer ", "service_key=", "api_key=", "secret=", "token="):
        if marker in rendered:
            raise ValueError(f"report must not contain secret-like marker {marker!r}")


def run_self_test() -> None:
    required = ["DATAPAN_ALPHA_KEY", "DATAPAN_BETA_KEY"]
    partial = build_report(
        repo="Owner/repo",
        required=required,
        secret_entries=[{"name": "DATAPAN_ALPHA_KEY", "updatedAt": "2026-07-07T00:00:00Z"}],
    )
    validate_report(partial)
    if partial["present_credential_envs"] != ["DATAPAN_ALPHA_KEY"]:
        raise ValueError("self-test expected alpha key present")
    if partial["missing_credential_envs"] != ["DATAPAN_BETA_KEY"]:
        raise ValueError("self-test expected beta key missing")
    complete = build_report(
        repo="Owner/repo",
        required=required,
        secret_entries=[
            {"name": "DATAPAN_ALPHA_KEY", "updatedAt": "2026-07-07T00:00:00Z"},
            {"name": "DATAPAN_BETA_KEY", "updatedAt": "2026-07-07T00:00:00Z"},
        ],
    )
    validate_report(complete)
    if complete["current_operator_env_ready"] is not True:
        raise ValueError("self-test expected complete readiness")


def print_human(report: dict[str, Any]) -> None:
    print(
        "credential runtime GitHub secret readiness "
        f"(repo={report['repo']}, required={report['required_credential_env_count']}, "
        f"present={report['present_credential_env_count']}, missing={report['missing_credential_env_count']})"
    )
    missing = report["missing_credential_envs"]
    if missing:
        print("missing: " + ", ".join(missing))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-plan", default=DEFAULT_EXECUTION_PLAN, type=pathlib.Path)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--secret-list-json", type=pathlib.Path, help="read gh secret list JSON from a file")
    parser.add_argument("--json", action="store_true", help="print JSON output")
    parser.add_argument("--check", action="store_true", help="validate required env configuration without querying GitHub")
    parser.add_argument("--self-test", action="store_true", help="run secret-free self-tests")
    args = parser.parse_args()

    try:
        if args.self_test:
            run_self_test()
            print("ok credential runtime GitHub secret readiness self-test")
            return 0
        required = required_envs(as_dict(load_json(args.execution_plan), "execution_plan"))
        if args.check:
            if len(set(required)) != len(required):
                raise ValueError("required credential env names must be unique")
            print(f"ok credential runtime GitHub secret readiness config (required={len(required)})")
            return 0
        if args.secret_list_json:
            raw_entries = load_json(args.secret_list_json)
            if not isinstance(raw_entries, list):
                raise ValueError("--secret-list-json must contain an array")
            secret_entries = [as_dict(item, "secret_list_json[]") for item in raw_entries]
        else:
            secret_entries = load_secret_entries(args.repo)
        report = build_report(repo=args.repo, required=required, secret_entries=secret_entries)
        validate_report(report)
    except Exception as exc:  # noqa: BLE001 - operators need the failed invariant
        print(f"FAIL credential runtime GitHub secret readiness: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(render_json(report), end="")
    else:
        print_human(report)
    return 0 if report["current_operator_env_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
