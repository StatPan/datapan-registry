#!/usr/bin/env python3
"""Generate the source runtime readiness overview."""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


DEFAULT_ROLLUP = pathlib.Path("reports/source-runtime-evidence-rollup.json")
DEFAULT_OUTPUT = pathlib.Path("docs/source-runtime-readiness.md")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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


def md(value: object) -> str:
    return str(value).replace("|", "\\|")


def id_list(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return ""
    return ", ".join(f"`{md(item)}`" for item in values)


def table(headers: list[str], rows: list[list[object]]) -> str:
    align = ["---"] + ["---:"] * (len(headers) - 1)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(align) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md(item) for item in row) + " |")
    return "\n".join(lines)


def load_plans(rollup: dict[str, Any]) -> dict[str, dict[str, Any]]:
    plans: dict[str, dict[str, Any]] = {}
    for raw_path in as_list(rollup.get("source_plan_inputs"), "source_plan_inputs"):
        plan_path = pathlib.Path(str(raw_path))
        plan = as_dict(load_json(plan_path), str(plan_path))
        source_id = str(plan.get("source_id"))
        if source_id in plans:
            raise ValueError(f"duplicate source_id in runtime evidence plans: {source_id}")
        plans[source_id] = plan
    return plans


def bullet_items(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return "- None recorded"
    return "\n".join(f"- `{md(item)}`" for item in items)


def source_detail(source: dict[str, Any], plan: dict[str, Any]) -> str:
    runtime_state = as_dict(plan.get("runtime_state"), "runtime_state")
    next_plan = as_dict(plan.get("next_evidence_plan"), "next_evidence_plan")
    blockers = as_list(plan.get("blockers"), "blockers")
    warnings = as_list(plan.get("warnings"), "warnings")

    blocker_lines = [
        f"- `{md(blocker.get('blocker_id'))}` ({md(blocker.get('owner'))}): "
        f"{md(blocker.get('expected_action'))}"
        for blocker in blockers
        if isinstance(blocker, dict)
    ]
    warning_lines = [
        f"- `{md(warning.get('warning_id'))}`: {md(warning.get('remaining'))}"
        for warning in warnings
        if isinstance(warning, dict)
    ]

    return (
        f"### {source.get('provider')} (`{source.get('source_id')}`)\n\n"
        f"- Runtime evidence: `{source.get('evidence_total')}`\n"
        f"- Verification mode: `{runtime_state.get('verification_mode')}`\n"
        f"- Adapter status: `{runtime_state.get('adapter_status')}`\n"
        f"- Credential required: `{str(runtime_state.get('credential_required')).lower()}`\n"
        f"- Candidate batch: `{plan.get('candidate_batch')}`\n"
        f"- First batch policy: {next_plan.get('first_batch_policy')}\n"
        f"- Promotion gate: {next_plan.get('promotion_gate')}\n\n"
        "Required CLI capabilities:\n\n"
        f"{bullet_items(next_plan.get('required_cli_capabilities'))}\n\n"
        "Required source reports:\n\n"
        f"{bullet_items(next_plan.get('required_artifacts'))}\n\n"
        "Open blockers:\n\n"
        f"{chr(10).join(blocker_lines) if blocker_lines else '- None recorded'}\n\n"
        "Warnings:\n\n"
        f"{chr(10).join(warning_lines) if warning_lines else '- None recorded'}\n"
    )


def build_markdown(rollup: dict[str, Any]) -> str:
    summary = as_dict(rollup.get("summary"), "summary")
    sources = as_list(rollup.get("sources"), "sources")
    plans = load_plans(rollup)

    source_rows = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_rows.append(
            [
                source.get("provider"),
                f"`{source.get('source_id')}`",
                source.get("evidence_total"),
                source.get("blocking_count"),
                source.get("warning_count"),
                id_list(source.get("blocker_ids")),
                id_list(source.get("warning_ids")),
            ]
        )

    blocker_rows = [
        [item.get("id"), item.get("count"), id_list(item.get("source_ids"))]
        for item in as_list(rollup.get("blockers_by_id"), "blockers_by_id")
        if isinstance(item, dict)
    ]
    warning_rows = [
        [item.get("id"), item.get("count"), id_list(item.get("source_ids"))]
        for item in as_list(rollup.get("warnings_by_id"), "warnings_by_id")
        if isinstance(item, dict)
    ]
    detail_sections = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id"))
        detail_sections.append(source_detail(source, plans[source_id]))

    return (
        "# Source Runtime Readiness\n\n"
        "This overview is generated from "
        "`reports/source-runtime-evidence-rollup.json` and its checked-in "
        "source runtime evidence plans. Regenerate it with "
        "`python scripts/generate-source-runtime-readiness.py` after updating "
        "a source runtime plan or rollup.\n\n"
        f"- Generated at: `{rollup.get('generated_at')}`\n"
        f"- Sources: `{summary.get('sources')}`\n"
        f"- Sources without runtime evidence: `{summary.get('sources_without_evidence')}`\n"
        f"- Runtime evidence total: `{summary.get('evidence_total')}`\n"
        f"- Verified: `{summary.get('verified')}`\n"
        f"- Failed: `{summary.get('failed')}`\n"
        f"- Skipped: `{summary.get('skipped')}`\n"
        f"- Unknown: `{summary.get('unknown')}`\n"
        f"- Blocking blocker instances: `{summary.get('blocking_count')}`\n"
        f"- Warning instances: `{summary.get('warning_count')}`\n\n"
        "## Source Summary\n\n"
        f"{table(['Source', 'Source ID', 'Evidence', 'Blockers', 'Warnings', 'Blocker IDs', 'Warning IDs'], source_rows)}\n\n"
        "## Blockers By ID\n\n"
        f"{table(['Blocker ID', 'Count', 'Sources'], blocker_rows)}\n\n"
        "## Warnings By ID\n\n"
        f"{table(['Warning ID', 'Count', 'Sources'], warning_rows)}\n\n"
        "## Source Next Actions\n\n"
        + "\n\n".join(detail_sections)
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollup", default=DEFAULT_ROLLUP, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    args = parser.parse_args()

    rollup = as_dict(load_json(args.rollup), str(args.rollup))
    write_text(args.output, build_markdown(rollup))
    print(f"wrote {args.output} from {args.rollup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
