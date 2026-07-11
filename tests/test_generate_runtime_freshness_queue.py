from __future__ import annotations

import importlib.util
import pathlib
import unittest
from datetime import datetime, timezone


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "generate-runtime-freshness-queue.py"
SPEC = importlib.util.spec_from_file_location("generate_runtime_freshness_queue", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RuntimeFreshnessQueueTest(unittest.TestCase):
    def test_latest_timestamp_wins_over_unknown_and_older_result(self) -> None:
        latest = MODULE.latest_evidence([
            {"dataset_id": "d", "operation": "o", "status": "verified"},
            {"dataset_id": "d", "operation": "o", "status": "failed", "verified_at": "2026-06-01T00:00:00Z"},
            {"dataset_id": "d", "operation": "o", "status": "verified", "verified_at": "2026-07-01T00:00:00Z"},
        ])
        self.assertEqual(latest[("d", "o")]["status"], "verified")

    def test_missing_timestamp_never_counts_as_fresh(self) -> None:
        result = MODULE.classify({"status": "verified"}, datetime(2026, 7, 4, tzinfo=timezone.utc), 30, 90)
        self.assertEqual(result, ("unknown_timestamp", 1, "repair_evidence_timestamp"))

    def test_current_queue_reconciles_complete_denominator(self) -> None:
        report = MODULE.build()
        summary = report["summary"]
        self.assertEqual(summary["supported_operations"], 21260)
        self.assertEqual(summary["queued"] + summary["fresh_verified"], 21260)
        self.assertEqual(len(report["queue"]), summary["queued"])


if __name__ == "__main__":
    unittest.main()
