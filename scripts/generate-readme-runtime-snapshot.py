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
    manifest = load("manifest.json")
    sustainable = load("reports/sustainable-coverage.json")
    freshness = load("reports/runtime-freshness-queue.json")
    projection = load("reports/current-runtime-evidence-projection.json")
    verification = load("reports/latest-verification-summary.json")
    growth = load("reports/data-go-kr/runtime-evidence-growth.json")
    layers = {row["id"]: row for row in sustainable["layers"]}
    summary, queue, counts = sustainable["summary"], freshness["summary"], verification["summary"]
    denominator = layers["catalog_denominator"]
    runtime = layers["runtime_evidence_operation"]
    fresh = layers["fresh_verified_operation"]
    consumers = layers["required_consumer_proven"]
    projection_summary = projection["summary"]
    projection_freshness = projection["freshness"]
    text = replace(text, r"- Sustainable coverage decision: `[^`]+` \(`\d+` of `\d+` layers meet\n  policy targets\)\.", f"- Sustainable coverage decision: `{summary['decision']}` (`{summary['layers_meeting_target']}` of `{summary['layers_total']}` layers meet\n  policy targets).", "sustainable coverage")
    text = replace(text, r"- Supported-source denominator coverage:.*\n  .*\n?", f"- Supported-source denominator coverage: `{denominator['numerator']}` of `{denominator['denominator']}` sources have an explicit\n  operation denominator (`{denominator['percent']:.1f}%`), covering `{queue['supported_operations']}` operations in total.\n", "denominator")
    text = replace(text, r"- Runtime operation evidence:.*\n  .*\n  .*\n?", f"- Runtime operation evidence: `{runtime['numerator']}` unique operation identities out of\n  `{runtime['denominator']}` (`{runtime['percent']:.1f}%`); fresh successful evidence covers `{fresh['numerator']}` unique operations\n  (`{fresh['percent']:.1f}%`) as of `{freshness['generated_at']}`.\n", "runtime evidence")
    freshness_text = (
        f"- Runtime freshness: `{projection_summary['current_operations_with_fresh_verified_evidence']}` current operations have fresh verified\n"
        f"  evidence within the `{projection_freshness['fresh_days']}` day window; `{projection_summary['current_operations_within_expiry_window']}` have current-bound evidence within the\n"
        f"  `{projection_freshness['expire_days']}` day expiry window (`{projection_summary['bound_current_stale_operations']}` stale, `{projection_summary['bound_current_non_verified_operations']}` recent non-verified).\n"
        f"  `{projection_summary['bound_current_expired_operations']}` are expired and `{projection_summary['bound_current_unknown_timestamp_operations']}` have unknown timestamps. The projection retains\n"
        f"  `{projection_summary['historical_evidence_records']}` historical input rows separately (`{projection_summary['unbound']}` unbound, `{projection_summary['ambiguous']}` ambiguous).\n"
    )
    text = replace(
        text,
        r"- Runtime freshness:.*(?:\n  .*)*\n?",
        freshness_text,
        "freshness",
    )
    text = replace(text, r"- Required consumer proof:.*\n  .*\n?", f"- Required consumer proof: `{consumers['numerator']}` of `{consumers['denominator']}` required consumers (`datapan-cli`,\n  `release-operator`, `studio`) are proven (`{consumers['percent']:.1f}%`).\n", "consumer proof")
    text = replace(text, r"- Runtime verification evidence:.*\n  .*\n  skipped\)", f"- Runtime verification evidence: `{counts['total']}` bounded checks merged into\n  `reports/latest-verification.json` (`{counts['verified']}` verified, `{counts['failed']}` failed, `{counts['skipped']}`\n  skipped)", "verification totals")
    text = replace(
        text,
        r"- (?:Latest release|Prepared snapshot version):.*",
        f"- Prepared snapshot version: `{manifest['datapan_version']}` (from `manifest.json`); published releases: [GitHub Releases](https://github.com/StatPan/datapan-registry/releases/latest).",
        "snapshot version",
    )
    target = growth["growth_target"]
    target_language = {
        "below_target": "below",
        "at_target": "at",
        "above_target": "above",
    }
    try:
        target_position = target_language[target["status"]]
    except KeyError as exc:
        raise ValueError(f"unsupported runtime evidence growth target status: {target.get('status')}") from exc
    text = replace(
        text,
        r"- Runtime evidence growth target:.*(?:\n  .*)*\n?",
        f"- Runtime evidence growth target: `{target['fresh_verified_total']}` fresh verified current-contract results are {target_position}\n  the unrounded `{target['target_percent']}%` release target (`{target['target_evidence_total']}` results); `{target['remaining_to_target']}` additional results are\n  required.\n",
        "growth target",
    )
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
