#!/usr/bin/env python3
"""Synchronize README runtime snapshot from checked-in evidence reports."""

from __future__ import annotations

import argparse
import json
import math
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


def summary_count(summary: dict[str, Any], key: str, report: str) -> int:
    value = summary.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"{report}.summary.{key} must be a nonnegative integer")
    return value


def build(text: str) -> str:
    manifest = load("manifest.json")
    sustainable = load("reports/sustainable-coverage.json")
    coverage = load("reports/coverage.json")
    institution_overview = load("reports/data-go-kr/institution-api-overview.json")
    operation_plan = load("reports/operation-observation-plan/index.json")
    freshness = load("reports/runtime-freshness-queue.json")
    projection = load("reports/current-runtime-evidence-projection.json")
    verification = load("reports/latest-verification-summary.json")
    growth = load("reports/data-go-kr/runtime-evidence-growth.json")
    coverage_summary = coverage.get("summary")
    if not isinstance(coverage_summary, dict):
        raise ValueError("reports/coverage.json.summary must be an object")
    institution_summary = institution_overview.get("summary")
    if not isinstance(institution_summary, dict):
        raise ValueError("reports/data-go-kr/institution-api-overview.json.summary must be an object")

    specs = summary_count(coverage_summary, "specs", "reports/coverage.json")
    operations = summary_count(coverage_summary, "operations", "reports/coverage.json")
    callable_operations = summary_count(coverage_summary, "callable_operations", "reports/coverage.json")
    plan_summary = operation_plan.get("summary")
    plan_context = operation_plan.get("inventory_context")
    plan_scopes = operation_plan.get("source_scopes")
    if not isinstance(plan_summary, dict) or not isinstance(plan_context, dict) or not isinstance(plan_scopes, list):
        raise ValueError("reports/operation-observation-plan/index.json must contain summary, inventory_context, and source_scopes")
    planned_operations = summary_count(plan_summary, "known_operations", "reports/operation-observation-plan/index.json")
    plan_complete = summary_count(plan_summary, "request_plans_complete", "reports/operation-observation-plan/index.json")
    plan_incomplete = summary_count(plan_summary, "request_plans_incomplete", "reports/operation-observation-plan/index.json")
    plan_bound = summary_count(plan_summary, "runtime_bindings_bound", "reports/operation-observation-plan/index.json")
    plan_unbound = summary_count(plan_summary, "runtime_bindings_unbound", "reports/operation-observation-plan/index.json")
    plan_admitted = summary_count(plan_summary, "admitted", "reports/operation-observation-plan/index.json")
    plan_not_admitted = summary_count(plan_summary, "not_admitted", "reports/operation-observation-plan/index.json")
    plan_unknown_scopes = summary_count(plan_summary, "inventory_unknown_scopes", "reports/operation-observation-plan/index.json")
    separate_link_operations = summary_count(plan_context, "separate_link_operations", "reports/operation-observation-plan/index.json")
    provider_index_entries = summary_count(plan_context, "provider_index_adapter_entries", "reports/operation-observation-plan/index.json")
    if plan_context.get("provider_index_entries_counted_as_operations") is not False:
        raise ValueError("provider-index adapter entries must not be counted as API operations")
    known_source_complete = 0
    known_partial = 0
    observed_unknown_scopes = 0
    for scope in plan_scopes:
        if not isinstance(scope, dict):
            raise ValueError("operation observation plan source scopes must be objects")
        count = summary_count(scope, "registered_operations", "reports/operation-observation-plan/index.json")
        if scope.get("inventory_status") == "source_complete" and scope.get("inventory_unknown") is False:
            known_source_complete += count
        if scope.get("inventory_unknown") is True:
            known_partial += count
            observed_unknown_scopes += 1
    if not (
        plan_complete + plan_incomplete == planned_operations
        and plan_bound + plan_unbound == planned_operations
        and plan_admitted + plan_not_admitted == planned_operations
        and known_source_complete + known_partial == planned_operations
        and observed_unknown_scopes == plan_unknown_scopes
    ):
        raise ValueError("operation observation plan state and inventory totals do not reconcile with known operations")
    institution_report = "reports/data-go-kr/institution-api-overview.json"
    institutions = summary_count(institution_summary, "institutions", institution_report)
    institution_apis = summary_count(institution_summary, "apis", "reports/data-go-kr/institution-api-overview.json")
    institution_operations = summary_count(institution_summary, "operations", institution_report)
    institution_callable_operations = summary_count(
        institution_summary,
        "callable_operations",
        institution_report,
    )
    for label, coverage_value, institution_value in (
        ("spec/API totals", specs, institution_apis),
        ("operation totals", operations, institution_operations),
        ("callable-operation totals", callable_operations, institution_callable_operations),
    ):
        if coverage_value != institution_value:
            raise ValueError(
                f"coverage and institution report totals are inconsistent for {label}: "
                f"coverage={coverage_value}, institution={institution_value}"
            )
    if callable_operations > operations:
        raise ValueError("reports/coverage.json.summary.callable_operations exceeds operations")
    callable_percent = coverage_summary.get("callable_operation_percent")
    if (
        isinstance(callable_percent, bool)
        or not isinstance(callable_percent, (int, float))
        or not math.isfinite(callable_percent)
        or not 0 <= callable_percent <= 100
    ):
        raise ValueError("reports/coverage.json.summary.callable_operation_percent must be between 0 and 100")
    expected_callable_percent = callable_operations / operations * 100 if operations else 0.0
    if f"{callable_percent:.1f}" != f"{expected_callable_percent:.1f}":
        raise ValueError("reports/coverage.json.summary.callable_operation_percent is inconsistent with its totals")

    layers = {row["id"]: row for row in sustainable["layers"]}
    summary, queue, counts = sustainable["summary"], freshness["summary"], verification["summary"]
    denominator = layers["catalog_denominator"]
    runtime = layers["runtime_evidence_operation"]
    fresh = layers["fresh_verified_operation"]
    consumers = layers["required_consumer_proven"]
    projection_summary = projection["summary"]
    projection_freshness = projection["freshness"]
    text = replace(text, r"- Specs: `\d+`", f"- Specs: `{specs}`", "specs")
    text = replace(text, r"- Operations: `\d+`", f"- Operations: `{operations}`", "operations")
    text = replace(
        text,
        r"- Legacy catalog non-excluded count: `\d+` \(`[\d.]+%`\); this is not probe admission or a provider-call budget\.",
        f"- Legacy catalog non-excluded count: `{callable_operations}` (`{callable_percent:.1f}%`); this is not probe admission or a provider-call budget.",
        "callable operations",
    )
    text = replace(
        text,
        r"- Operation observation plans:.*",
        f"- Operation observation plans: `{planned_operations}` currently known registered API-operation IDs (`{known_source_complete}` source-complete; `{known_partial}` across `{plan_unknown_scopes}` partial source inventories with upstream coverage unknown; `{plan_complete}` complete, `{plan_bound}` runtime-bound, `{plan_admitted}` admitted).",
        "operation observation plans",
    )
    text = replace(
        text,
        r"- Operation inventory context:.*",
        f"- Operation inventory context: `{separate_link_operations}` link operations are separate; `{provider_index_entries}` provider-index entries are adapters, not API operations.",
        "operation inventory context",
    )
    institution_text = (
        f"- Institution API overview: `{institutions}` organizations, `{institution_apis}` APIs, and "
        f"`{institution_operations}`\n  operations"
    )
    text = replace(
        text,
        r"- Institution API overview: `\d+` organizations, `\d+` APIs, and `\d+`\n  operations",
        institution_text,
        "institution API overview",
    )
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
