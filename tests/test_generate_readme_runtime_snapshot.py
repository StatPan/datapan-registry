from __future__ import annotations

import importlib.util
import pathlib
import unittest
from typing import Any
from unittest.mock import patch


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/generate-readme-runtime-snapshot.py"
SPEC = importlib.util.spec_from_file_location("readme_runtime_snapshot", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)

README_TEMPLATE = """- Provider: `data.go.kr`
- Specs: `12060`
- Operations: `21256`
- Legacy catalog non-excluded count: `21114` (`99.3%`); this is not probe admission or a provider-call budget.
- Operation observation plans: `3` currently known registered API-operation IDs (`2` source-complete; `1` across `1` partial source inventories with upstream coverage unknown; `1` complete, `1` runtime-bound, `1` admitted).
- Operation inventory context: `5` link operations are separate; `4` provider-index entries are adapters, not API operations.
- Sustainable coverage decision: `old` (`0` of `4` layers meet
  policy targets).
- Supported-source denominator coverage: old
  old
- Runtime operation evidence: old
  old
  old
- Runtime freshness: old
  old
  old
- Required consumer proof: old
  old
- Runtime verification evidence: old
  old
  skipped)
- Latest release: old
- Institution API overview: `411` organizations, `12060` APIs, and `21256`
  operations in `reports/data-go-kr/institution-api-overview.json`; readable
  tables live in `docs/data-go-kr-institution-api-overview.md`.
- Runtime evidence growth target: old
  old
  target.
"""


class ReadmeRuntimeSnapshotTest(unittest.TestCase):
    def make_reports(
        self,
        target_status: str = "below_target",
        *,
        specs: int = 12060,
        operations: int = 21256,
        callable_operations: int = 21114,
        institutions: int = 411,
        institution_apis: int | None = None,
        institution_operations: int | None = None,
        institution_callable_operations: int | None = None,
        callable_percent: float | None = None,
    ) -> dict[str, Any]:
        institution_apis = specs if institution_apis is None else institution_apis
        institution_operations = operations if institution_operations is None else institution_operations
        institution_callable_operations = (
            callable_operations if institution_callable_operations is None else institution_callable_operations
        )
        if callable_percent is None:
            callable_percent = round(callable_operations / operations * 100, 1) if operations else 0.0
        return {
            "manifest.json": {"datapan_version": "0.1.1-dev"},
            "reports/coverage.json": {
                "summary": {
                    "specs": specs,
                    "operations": operations,
                    "callable_operations": callable_operations,
                    "callable_operation_percent": callable_percent,
                },
            },
            "reports/data-go-kr/institution-api-overview.json": {
                "summary": {
                    "institutions": institutions,
                    "apis": institution_apis,
                    "operations": institution_operations,
                    "callable_operations": institution_callable_operations,
                },
            },
            "reports/operation-observation-plan/index.json": {
                "summary": {
                    "known_operations": 3,
                    "request_plans_complete": 1,
                    "request_plans_incomplete": 2,
                    "runtime_bindings_bound": 1,
                    "runtime_bindings_unbound": 2,
                    "admitted": 1,
                    "not_admitted": 2,
                    "inventory_unknown_scopes": 1,
                },
                "inventory_context": {
                    "separate_link_operations": 5,
                    "provider_index_adapter_entries": 4,
                    "provider_index_entries_counted_as_operations": False,
                },
                "source_scopes": [
                    {"inventory_status": "source_complete", "inventory_unknown": False, "registered_operations": 2},
                    {"inventory_status": "partial", "inventory_unknown": True, "registered_operations": 1},
                ],
            },
            "reports/sustainable-coverage.json": {
                "summary": {"decision": "coverage_gaps", "layers_meeting_target": 0, "layers_total": 4},
                "layers": [
                    {"id": "catalog_denominator", "numerator": 5, "denominator": 5, "percent": 100.0},
                    {"id": "runtime_evidence_operation", "numerator": 0, "denominator": 21260, "percent": 0.0},
                    {"id": "fresh_verified_operation", "numerator": 0, "denominator": 21260, "percent": 0.0},
                    {"id": "required_consumer_proven", "numerator": 0, "denominator": 3, "percent": 0.0},
                ],
            },
            "reports/runtime-freshness-queue.json": {
                "generated_at": "2026-09-30T01:45:58.529244Z",
                "summary": {"supported_operations": 21260},
            },
            "reports/current-runtime-evidence-projection.json": {
                "freshness": {"fresh_days": 30, "expire_days": 90},
                "summary": {
                    "current_operations_with_fresh_verified_evidence": 0,
                    "current_operations_within_expiry_window": 0,
                    "bound_current_stale_operations": 0,
                    "bound_current_non_verified_operations": 0,
                    "bound_current_expired_operations": 0,
                    "bound_current_unknown_timestamp_operations": 0,
                    "historical_evidence_records": 7181,
                    "unbound": 7180,
                    "ambiguous": 1,
                },
            },
            "reports/latest-verification-summary.json": {
                "summary": {"total": 7181, "verified": 3577, "failed": 946, "skipped": 2658},
            },
            "reports/data-go-kr/runtime-evidence-growth.json": {
                "evidence": {"coverage_percent": 0.0},
                "growth_target": {
                    "status": target_status,
                    "fresh_verified_total": {"below_target": 0, "at_target": 2126, "above_target": 2127}[target_status],
                    "target_evidence_total": 2126,
                    "target_percent": 10,
                    "remaining_to_target": 2126 if target_status == "below_target" else 0,
                },
            },
        }

    def render(self, target_status: str, **counts: int | float) -> str:
        reports = self.make_reports(target_status, **counts)
        with patch.object(snapshot, "load", side_effect=reports.__getitem__):
            rendered = snapshot.build(README_TEMPLATE)
            rerendered = snapshot.build(rendered)
        self.assertEqual(rendered, rerendered)
        return rendered

    def test_historical_rows_do_not_become_fresh_current_evidence(self) -> None:
        rendered = self.render("below_target")

        self.assertIn("0` current operations have fresh verified", rendered)
        self.assertIn("0` have current-bound evidence within the", rendered)
        self.assertIn("7181` historical input rows separately (`7180` unbound, `1` ambiguous)", rendered)
        self.assertNotIn("7181` evidence records are within", rendered)
        self.assertIn("0` fresh verified current-contract results are below", rendered)
        self.assertIn("2126` additional results are", rendered)
        self.assertNotIn("above the", rendered)
        self.assertIn("Prepared snapshot version: `0.1.1-dev`", rendered)
        self.assertIn("https://github.com/StatPan/datapan-registry/releases/latest", rendered)
        self.assertNotIn("Latest release:", rendered)

    def test_candidate_totals_render_and_rerender_idempotently(self) -> None:
        rendered = self.render(
            "below_target",
            specs=12282,
            operations=21533,
            callable_operations=21391,
            institutions=416,
        )

        self.assertIn("- Specs: `12282`", rendered)
        self.assertIn("- Operations: `21533`", rendered)
        self.assertIn("- Legacy catalog non-excluded count: `21391` (`99.3%`); this is not probe admission or a provider-call budget.", rendered)
        self.assertIn(
            "- Operation observation plans: `3` currently known registered API-operation IDs "
            "(`2` source-complete; `1` across `1` partial source inventories with upstream coverage unknown; "
            "`1` complete, `1` runtime-bound, `1` admitted).",
            rendered,
        )
        self.assertIn("- Operation inventory context: `5` link operations are separate; `4` provider-index entries are adapters, not API operations.", rendered)
        self.assertIn(
            "- Institution API overview: `416` organizations, `12282` APIs, and `21533`\n"
            "  operations in `reports/data-go-kr/institution-api-overview.json`",
            rendered,
        )

    def test_callable_percentage_tracks_changed_candidate_totals(self) -> None:
        rendered = self.render(
            "below_target",
            specs=100,
            operations=100,
            callable_operations=87,
            institutions=4,
        )
        self.assertIn("- Legacy catalog non-excluded count: `87` (`87.0%`); this is not probe admission or a provider-call budget.", rendered)

    def test_partial_inventory_rows_are_not_mislabeled_as_source_complete(self) -> None:
        reports = self.make_reports()
        reports["reports/operation-observation-plan/index.json"]["source_scopes"][1]["inventory_unknown"] = False
        with patch.object(snapshot, "load", side_effect=reports.__getitem__):
            with self.assertRaisesRegex(ValueError, "inventory totals do not reconcile"):
                snapshot.build(README_TEMPLATE)

    def test_coverage_and_institution_totals_must_agree(self) -> None:
        for field in ("apis", "operations", "callable_operations"):
            with self.subTest(field=field):
                reports = self.make_reports()
                institution_summary = reports["reports/data-go-kr/institution-api-overview.json"]["summary"]
                institution_summary[field] += 1
                with patch.object(snapshot, "load", side_effect=reports.__getitem__):
                    with self.assertRaisesRegex(ValueError, "coverage and institution report totals are inconsistent"):
                        snapshot.build(README_TEMPLATE)

    def test_callable_percent_must_match_reported_totals(self) -> None:
        reports = self.make_reports(callable_percent=99.0)
        with patch.object(snapshot, "load", side_effect=reports.__getitem__):
            with self.assertRaisesRegex(ValueError, "callable_operation_percent is inconsistent"):
                snapshot.build(README_TEMPLATE)

    def test_missing_snapshot_count_block_fails_closed(self) -> None:
        reports = self.make_reports()
        missing_specs = README_TEMPLATE.replace("- Specs: `12060`\n", "")
        with patch.object(snapshot, "load", side_effect=reports.__getitem__):
            with self.assertRaisesRegex(ValueError, "README snapshot block not found: specs"):
                snapshot.build(missing_specs)

    def test_growth_target_language_matches_report_status(self) -> None:
        for status, phrase in (("at_target", "results are at"), ("above_target", "results are above")):
            with self.subTest(status=status):
                rendered = self.render(status)
                self.assertIn(phrase, rendered)
                self.assertNotIn("results are below", rendered)


if __name__ == "__main__":
    unittest.main()
