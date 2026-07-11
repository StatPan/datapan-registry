#!/usr/bin/env python3
"""Synchronize README runtime snapshot from checked-in evidence reports."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any


README = pathlib.Path("README.md")


def load(path: str) -> dict[str, Any]:
    value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def replace(text: str, pattern: str, value: str, label: str) -> str:
    updated, count = re.subn(pattern, value, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"README snapshot block not found: {label}")
    return updated


def build(text: str) -> str:
    sustainable = load("reports/sustainable-coverage.json")
    freshness = load("reports/runtime-freshness-queue.json")
    verification = load("reports/latest-verification-summary.json")
    growth = load("reports/data-go-kr/runtime-evidence-growth.json")
    layers = {row["id"]: row for row in sustainable["layers"]}
    summary, queue, counts = sustainable["summary"], freshness["summary"], verification["summary"]
    denominator = layers["catalog_denominator"]
    runtime = layers["runtime_evidence_operation"]
    fresh = layers["fresh_verified_operation"]
    consumers = layers["required_consumer_proven"]
    recent = counts["total"] - queue["unknown_timestamp"] - queue["stale"] - queue["expired"]
    text = replace(text, r"- Sustainable coverage decision: `[^`]+` \(`\d+` of `\d+` layers meet\n  policy targets\)\.", f"- Sustainable coverage decision: `{summary['decision']}` (`{summary['layers_meeting_target']}` of `{summary['layers_total']}` layers meet\n  policy targets).", "sustainable coverage")
    text = replace(text, r"- Supported-source denominator coverage:.*\n  .*\n?", f"- Supported-source denominator coverage: `{denominator['numerator']}` of `{denominator['denominator']}` sources have an explicit\n  operation denominator (`{denominator['percent']:.1f}%`), covering `{queue['supported_operations']}` operations in total.\n", "denominator")
    text = replace(text, r"- Runtime operation evidence:.*\n  .*\n  .*\n?", f"- Runtime operation evidence: `{runtime['numerator']}` unique operation identities out of\n  `{runtime['denominator']}` (`{runtime['percent']:.1f}%`); fresh successful evidence covers `{fresh['numerator']}` unique operations\n  (`{fresh['percent']:.1f}%`) as of `{freshness['generated_at']}`.\n", "runtime evidence")
    text = replace(text, r"- Runtime freshness:.*\n  .*\n  .*\n?", f"- Runtime freshness: `{recent}` evidence records are within the `30` day fresh\n  window, `{queue['stale']}` are stale, `{queue['expired']}` are expired, and `{queue['unknown_timestamp']}` missing timestamps are\n  explicitly excluded from fresh coverage.\n", "freshness")
    text = replace(text, r"- Required consumer proof:.*\n  .*\n?", f"- Required consumer proof: `{consumers['numerator']}` of `{consumers['denominator']}` required consumers (`datapan-cli`,\n  `release-operator`, `studio`) are proven (`{consumers['percent']:.1f}%`).\n", "consumer proof")
    text = replace(text, r"- Runtime verification evidence:.*\n  .*\n  skipped\)", f"- Runtime verification evidence: `{counts['total']}` bounded checks merged into\n  `reports/latest-verification.json` (`{counts['verified']}` verified, `{counts['failed']}` failed, `{counts['skipped']}`\n  skipped)", "verification totals")
    evidence = growth["evidence"]
    target = growth["growth_target"]
    text = replace(text, r"- Runtime evidence growth target:.*\n  .*\n  target\.\n?", f"- Runtime evidence growth target: `{evidence['coverage_percent']:.1f}%` checked evidence is above the\n  unrounded `{target['target_percent']}%` release target; `{target['remaining_to_target']}` additional records are required for this\n  target.\n", "growth target")
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, default=README)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        current = args.output.read_text(encoding="utf-8")
        rendered = build(current)
        if args.check:
            if current != rendered:
                raise ValueError(f"{args.output} runtime snapshot is stale")
        else:
            args.output.write_text(rendered, encoding="utf-8")
        print(f"{'ok' if args.check else 'wrote'} {args.output} runtime snapshot")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL generate README runtime snapshot: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
