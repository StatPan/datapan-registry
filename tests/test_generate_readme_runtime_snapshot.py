from __future__ import annotations

import importlib.util
import pathlib
import unittest
from unittest.mock import patch


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/generate-readme-runtime-snapshot.py"
SPEC = importlib.util.spec_from_file_location("readme_runtime_snapshot", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)

README_TEMPLATE = """- Sustainable coverage decision: `old` (`0` of `4` layers meet
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
- Runtime evidence growth target: old
  old
  target.
"""


class ReadmeRuntimeSnapshotTest(unittest.TestCase):
    def render(self, target_status: str) -> str:
        reports = {
            "manifest.json": {"datapan_version": "0.1.1-dev"},
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

    def test_growth_target_language_matches_report_status(self) -> None:
        for status, phrase in (("at_target", "results are at"), ("above_target", "results are above")):
            with self.subTest(status=status):
                rendered = self.render(status)
                self.assertIn(phrase, rendered)
                self.assertNotIn("results are below", rendered)


if __name__ == "__main__":
    unittest.main()
