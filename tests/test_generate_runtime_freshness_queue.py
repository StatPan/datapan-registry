from __future__ import annotations

import importlib.util
import pathlib
import unittest
from datetime import datetime, timezone
from unittest import mock


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

    def test_latest_verification_time_allows_evidence_newer_than_release(self) -> None:
        original_load = MODULE.load
        denominator = {
            "generated_at": "2026-07-11T00:00:00Z",
            "summary": {"operations": 1},
            "sources": [],
        }
        latest = {
            "generated_at": "2026-07-24T00:00:01Z",
            "results": [{
                "dataset_id": "d",
                "operation": "o",
                "status": "verified",
                "verified_at": "2026-07-24T00:00:00Z",
            }],
        }
        policy = {
            "freshness": {
                "fresh_days": 30,
                "expire_days": 90,
                "evaluation_time_source": "latest_verification.generated_at",
            },
        }

        def load_fixture(path: pathlib.Path):
            values = {
                MODULE.DENOMINATORS: denominator,
                MODULE.REGISTRY: [],
                MODULE.LATEST: latest,
                MODULE.POLICY: policy,
            }
            return values[path] if path in values else original_load(path)

        operation = {
            "source_id": "data_go_kr",
            "dataset_id": "d",
            "operation": "o",
            "operation_seq": "1",
            "identity_key": "data_go_kr:d:1",
        }
        with (
            mock.patch.object(MODULE, "load", side_effect=load_fixture),
            mock.patch.object(MODULE, "supported_operations", return_value=[operation]),
        ):
            report = MODULE.build()

        self.assertEqual(report["generated_at"], latest["generated_at"])
        self.assertEqual(report["freshness"]["as_of"], latest["generated_at"])
        self.assertEqual(report["summary"]["fresh_verified"], 1)

    def test_current_queue_reconciles_complete_denominator(self) -> None:
        report = MODULE.build()
        summary = report["summary"]
        self.assertEqual(summary["supported_operations"], 21260)
        self.assertEqual(summary["queued"] + summary["fresh_verified"], 21260)
        self.assertEqual(len(report["queue"]), summary["queued"])


if __name__ == "__main__":
    unittest.main()
