#!/usr/bin/env python3
"""Generate or check the release-wide source runtime evidence rollup."""

from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import json
import pathlib
import sys
from typing import Any


DEFAULT_OUTPUT = pathlib.Path("reports/source-runtime-evidence-rollup.json")


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def file_digest(path: pathlib.Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def key_counts(counter: dict[str, list[str]]) -> list[dict[str, Any]]:
    return [
        {"id": key, "count": len(counter[key]), "source_ids": sorted(counter[key])}
        for key in sorted(counter)
    ]


def build_rollup(template: dict[str, Any]) -> dict[str, Any]:
    plan_paths = sorted(pathlib.Path("reports").glob("*/runtime-evidence-plan.json"))
    if not plan_paths:
        raise ValueError("no source runtime evidence plans found")

    source_inputs: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    blockers_by_id: dict[str, list[str]] = collections.defaultdict(list)
    warnings_by_id: dict[str, list[str]] = collections.defaultdict(list)
    summary = {
        "sources": len(plan_paths),
        "sources_without_evidence": 0,
        "evidence_total": 0,
        "verified": 0,
        "failed": 0,
        "skipped": 0,
        "unknown": 0,
        "blocking_count": 0,
        "warning_count": 0,
    }

    for plan_path in plan_paths:
        plan = load_json(plan_path)
        plan_summary = as_dict(plan.get("summary"), f"{plan_path}.summary")
        runtime_state = as_dict(plan.get("runtime_state"), f"{plan_path}.runtime_state")
        source_id = plan.get("source_id")
        provider = plan.get("provider")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError(f"{plan_path}: source_id must be a non-empty string")
        if not isinstance(provider, str) or not provider:
            raise ValueError(f"{plan_path}: provider must be a non-empty string")
        blockers = plan.get("blockers")
        warnings = plan.get("warnings")
        if not isinstance(blockers, list) or not isinstance(warnings, list):
            raise ValueError(f"{plan_path}: blockers and warnings must be arrays")
        blocker_ids = sorted(
            str(item.get("blocker_id")) for item in blockers if isinstance(item, dict)
        )
        warning_ids = sorted(
            str(item.get("warning_id")) for item in warnings if isinstance(item, dict)
        )
        evidence_total = int(plan_summary.get("evidence_total", 0))
        if evidence_total == 0:
            summary["sources_without_evidence"] += 1
        summary["evidence_total"] += evidence_total
        for key in ("verified", "failed", "skipped", "unknown"):
            summary[key] += int(runtime_state.get(key, 0))
        summary["blocking_count"] += int(plan_summary.get("blocking_count", 0))
        summary["warning_count"] += int(plan_summary.get("warning_count", 0))
        for blocker_id in blocker_ids:
            blockers_by_id[blocker_id].append(source_id)
        for warning_id in warning_ids:
            warnings_by_id[warning_id].append(source_id)

        size, digest = file_digest(plan_path)
        source_inputs.append(
            {
                "path": plan_path.as_posix(),
                "source_id": source_id,
                "provider": provider,
                "evidence_total": evidence_total,
                "blocking_count": plan_summary.get("blocking_count"),
                "warning_count": plan_summary.get("warning_count"),
                "bytes": size,
                "sha256": digest,
            }
        )
        sources.append(
            {
                "source_id": source_id,
                "provider": provider,
                "runtime_evidence_plan": plan_path.as_posix(),
                "evidence_total": evidence_total,
                "blocking_count": plan_summary.get("blocking_count"),
                "warning_count": plan_summary.get("warning_count"),
                "blocker_ids": blocker_ids,
                "warning_ids": warning_ids,
                "next_action_count": plan_summary.get("next_action_count"),
            }
        )

    output = copy.deepcopy(template)
    output["source_plan_inputs"] = source_inputs
    output["summary"] = summary
    output["sources"] = sorted(sources, key=lambda item: str(item["source_id"]))
    output["blockers_by_id"] = key_counts(blockers_by_id)
    output["warnings_by_id"] = key_counts(warnings_by_id)
    output["warnings"] = [
        {
            "warning_id": warning_id,
            "affected_sources": sorted(source_ids),
            "expected": (
                f"Each affected source resolves {warning_id} according to its checked-in "
                "source runtime evidence plan."
            ),
            "actual": f"{len(source_ids)} source(s) currently report {warning_id}.",
            "remaining": (
                "Follow the source-specific next evidence plan and regenerate the release ledger "
                "after the evidence state changes."
            ),
        }
        for warning_id, source_ids in sorted(warnings_by_id.items())
    ]
    return output


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when the rollup is stale")
    args = parser.parse_args()
    try:
        rendered = render_json(build_rollup(load_json(args.output)))
        if args.check:
            if args.output.read_text(encoding="utf-8") != rendered:
                raise ValueError(
                    f"{args.output} is stale; run `python3 scripts/generate-source-runtime-evidence-rollup.py`"
                )
        else:
            args.output.write_text(rendered, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL source runtime evidence rollup: {exc}", file=sys.stderr)
        return 1
    print(f"ok source runtime evidence rollup ({'checked' if args.check else 'refreshed'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
