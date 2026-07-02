#!/usr/bin/env python3
"""Generate institution-scoped data.go.kr runtime verification batches."""

from __future__ import annotations

import argparse
import json
import pathlib
import shlex
from typing import Any


SCHEMA_VERSION = "datapan.institution-runtime-plan.v1"


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_text(path: pathlib.Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def portable_path(path: pathlib.Path | str) -> str:
    return str(path).replace("\\", "/")


def md(value: object) -> str:
    return str(value).replace("|", "\\|")


def shell_arg(value: pathlib.Path | str) -> str:
    return shlex.quote(str(value).replace("\\", "/"))


def table(headers: list[str], rows: list[list[object]]) -> str:
    align = ["---"] + ["---:"] * (len(headers) - 1)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(align) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md(item) for item in row) + " |")
    return "\n".join(lines)


def verify_command(
    registry: pathlib.Path,
    latest_verification: pathlib.Path,
    organization: str,
    batch_size: int,
    timeout: str,
    output: str,
) -> str:
    return (
        "datapan catalog verify "
        f"--registry {shell_arg(registry)} "
        f"--org {shell_arg(organization)} "
        "--kind data_go_kr_gateway "
        f"--exclude-input {shell_arg(latest_verification)} "
        f"--limit {batch_size} "
        f"--timeout {shell_arg(timeout)} "
        f"--output {shell_arg(output)} "
        "--json"
    )


def build_report(
    backlog_path: pathlib.Path,
    registry_path: pathlib.Path,
    latest_verification_path: pathlib.Path,
    batch_size: int,
    institution_limit: int,
    timeout: str,
    output_dir: pathlib.Path,
) -> dict[str, Any]:
    backlog = as_dict(load_json(backlog_path), str(backlog_path))
    summary = as_dict(backlog.get("summary"), "coverage_backlog.summary")
    institutions = [
        row
        for row in as_list(backlog.get("institutions"), "coverage_backlog.institutions")
        if isinstance(row, dict)
        and int(row.get("runtime_reactivation_api_count") or 0) > 0
        and int(row.get("runtime_missing_evidence_count") or 0) > 0
    ]
    institutions.sort(
        key=lambda row: (
            -int(row.get("priority_score") or 0),
            -int(row.get("runtime_missing_evidence_count") or 0),
            str(row.get("organization") or ""),
        )
    )

    batches: list[dict[str, Any]] = []
    planned_ops = 0
    for index, row in enumerate(institutions[:institution_limit], start=1):
        organization = str(row.get("organization") or "Unknown")
        missing_ops = int(row.get("runtime_missing_evidence_count") or 0)
        planned = min(batch_size, missing_ops)
        output = portable_path(output_dir / f"institution-{index:02d}.json")
        batches.append(
            {
                "rank": index,
                "label": f"institution-{index:02d}",
                "organization": organization,
                "kind": "data_go_kr_gateway",
                "api_count": int(row.get("api_count") or 0),
                "covered_api_count": int(row.get("covered_api_count") or 0),
                "uncovered_api_count": int(row.get("uncovered_api_count") or 0),
                "operation_count": int(row.get("operation_count") or 0),
                "runtime_evidence_api_count": int(row.get("runtime_evidence_api_count") or 0),
                "runtime_reactivation_api_count": int(row.get("runtime_reactivation_api_count") or 0),
                "runtime_missing_evidence_count": missing_ops,
                "approval_required_operations": int(row.get("approval_required_operations") or 0),
                "priority_score": int(row.get("priority_score") or 0),
                "planned_operations": planned,
                "credential_required": True,
                "command": verify_command(
                    registry_path,
                    latest_verification_path,
                    organization,
                    planned,
                    timeout,
                    output,
                ),
                "output": output,
            }
        )
        planned_ops += planned

    first_queue = batches[0]["organization"] if batches else ""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": backlog.get("generated_at"),
        "provider": backlog.get("provider"),
        "source_id": backlog.get("source_id"),
        "generation_inputs": {
            "coverage_backlog": portable_path(backlog_path),
            "registry": portable_path(registry_path),
            "latest_verification": portable_path(latest_verification_path),
        },
        "policy": {
            "selection": "highest_priority_runtime_reactivation_institutions",
            "kind": "data_go_kr_gateway",
            "batch_size": batch_size,
            "institution_limit": institution_limit,
            "timeout": timeout,
            "credential_required": True,
            "credential_note": "data.go.kr gateway verification requires a service key; no-key runs only prove parameter readiness.",
        },
        "summary": {
            "institutions": int(summary.get("institutions") or 0),
            "planned_institutions": len(batches),
            "planned_operations": planned_ops,
            "runtime_reactivation_api_count": int(summary.get("runtime_reactivation_api_count") or 0),
            "uncovered_api_count": int(summary.get("uncovered_api_count") or 0),
            "first_queue": first_queue,
        },
        "batches": batches,
        "next": [
            {
                "label": "merge",
                "command": (
                    "datapan catalog verify merge "
                    "--input reports/latest-verification.json "
                    "--input <completed-institution-batch.json> "
                    "--output reports/latest-verification.json --json"
                ),
            },
            {
                "label": "refresh",
                "commands": [
                    "datapan catalog verify summary --input reports/latest-verification.json --output reports/latest-verification-summary.json --json",
                    "python scripts/generate-coverage-backlog.py",
                    "python scripts/generate-institution-api-overview.py",
                    "python scripts/generate-institution-runtime-plan.py",
                ],
            },
        ],
    }


def build_markdown(report: dict[str, Any]) -> str:
    summary = as_dict(report.get("summary"), "summary")
    policy = as_dict(report.get("policy"), "policy")
    batches = as_list(report.get("batches"), "batches")
    batch_rows = [
        [
            row.get("rank"),
            row.get("organization"),
            row.get("api_count"),
            row.get("covered_api_count"),
            row.get("uncovered_api_count"),
            row.get("operation_count"),
            row.get("runtime_reactivation_api_count"),
            row.get("runtime_missing_evidence_count"),
            row.get("planned_operations"),
        ]
        for row in batches
        if isinstance(row, dict)
    ]
    command_rows = [
        [row.get("rank"), row.get("organization"), f"`{row.get('output')}`"]
        for row in batches
        if isinstance(row, dict)
    ]
    commands = "\n".join(
        f"```bash\n{row.get('command')}\n```"
        for row in batches[:3]
        if isinstance(row, dict)
    )

    return (
        "# data.go.kr Institution Runtime Plan\n\n"
        "This plan is generated from `reports/data-go-kr/coverage-backlog.json` "
        "and turns the highest-priority institution runtime gaps into bounded "
        "`datapan catalog verify --org` batches.\n\n"
        f"- Generated at: `{report.get('generated_at')}`\n"
        f"- Planned institutions: `{summary.get('planned_institutions')}`\n"
        f"- Planned operations: `{summary.get('planned_operations')}`\n"
        f"- First queue: `{summary.get('first_queue')}`\n"
        f"- Batch size: `{policy.get('batch_size')}`\n"
        f"- Timeout: `{policy.get('timeout')}`\n"
        f"- Credential required: `{str(policy.get('credential_required')).lower()}`\n\n"
        f"{policy.get('credential_note')}\n\n"
        "## Planned Institution Batches\n\n"
        f"{table(['Rank', 'Institution', 'APIs', 'Covered APIs', 'Uncovered APIs', 'Ops', 'Runtime Reactivation APIs', 'Missing Evidence Ops', 'Planned Ops'], batch_rows)}\n\n"
        "## Batch Outputs\n\n"
        f"{table(['Rank', 'Institution', 'Output'], command_rows)}\n\n"
        "## First Commands\n\n"
        f"{commands}\n\n"
        "After a completed batch, merge it into `reports/latest-verification.json`, "
        "regenerate the verification summary, coverage backlog, institution "
        "overview, and this plan.\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-backlog", default="reports/data-go-kr/coverage-backlog.json", type=pathlib.Path)
    parser.add_argument("--registry", default="data/data-go-kr.registry.json", type=pathlib.Path)
    parser.add_argument("--latest-verification", default="reports/latest-verification.json", type=pathlib.Path)
    parser.add_argument("--batch-size", default=100, type=int)
    parser.add_argument("--institution-limit", default=10, type=int)
    parser.add_argument("--timeout", default="20s")
    parser.add_argument("--batch-output-dir", default="reports/data-go-kr/institution-batches", type=pathlib.Path)
    parser.add_argument("--output", default="reports/data-go-kr/institution-runtime-plan.json", type=pathlib.Path)
    parser.add_argument("--markdown-output", default="docs/data-go-kr-institution-runtime-plan.md", type=pathlib.Path)
    args = parser.parse_args()

    if args.batch_size <= 0:
        raise ValueError("--batch-size must be positive")
    if args.institution_limit <= 0:
        raise ValueError("--institution-limit must be positive")

    report = build_report(
        args.coverage_backlog,
        args.registry,
        args.latest_verification,
        args.batch_size,
        args.institution_limit,
        args.timeout,
        args.batch_output_dir,
    )
    write_json(args.output, report)
    write_text(args.markdown_output, build_markdown(report))
    print(f"wrote {args.output} and {args.markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
