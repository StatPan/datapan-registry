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
        latest = MODULE.latest_by_identity([
            {"identity_key": "data_go_kr:d:o", "status": "verified"},
            {"identity_key": "data_go_kr:d:o", "status": "failed", "verified_at": "2026-06-01T00:00:00Z"},
            {"identity_key": "data_go_kr:d:o", "status": "verified", "verified_at": "2026-07-01T00:00:00Z"},
        ])
        self.assertEqual(latest["data_go_kr:d:o"]["status"], "verified")

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
        projection = {
            "freshness": {"as_of": latest["generated_at"]},
            "current_evidence": [{"identity_key": "data_go_kr:d:1", "status": "verified", "verified_at": "2026-07-24T00:00:00Z"}],
            "records": [],
            "operations": [{"identity_key": "data_go_kr:d:1", "contract_complete": True, "incomplete_reasons": []}],
            "summary": {},
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
                MODULE.PROJECTION: projection,
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

    def test_non_data_go_denominator_is_explicitly_outside_projection_scope(self) -> None:
        original_load = MODULE.load
        latest = {"generated_at": "2026-07-24T00:00:01Z", "results": []}
        projection = {"freshness": {"as_of": latest["generated_at"]}, "current_evidence": [], "records": [], "summary": {}}
        denominator = {
            "summary": {"operations": 1},
            "sources": [{"source_id": "ecos", "path": "reports/ecos/operation-denominator.json"}],
        }
        source_denominator = {"operations": [{"operation_id": "ecos-statistic-search-102y004"}]}
        policy = {"freshness": {"fresh_days": 30, "expire_days": 90, "evaluation_time_source": "latest_verification.generated_at"}}

        def load_fixture(path: pathlib.Path):
            values = {
                MODULE.DENOMINATORS: denominator,
                pathlib.Path("reports/ecos/operation-denominator.json"): source_denominator,
                MODULE.REGISTRY: [],
                MODULE.LATEST: latest,
                MODULE.PROJECTION: projection,
                MODULE.POLICY: policy,
            }
            return values[path] if path in values else original_load(path)

        with mock.patch.object(MODULE, "load", side_effect=load_fixture):
            report = MODULE.build()

        self.assertEqual(report["summary"]["unsupported_current_binding"], 1)
        self.assertEqual(report["summary"]["fresh_verified"], 0)
        self.assertEqual(report["queue"][0]["classification"], "unsupported_current_binding")
        self.assertEqual(report["queue"][0]["action"], "review_source_specific_receipt")
        self.assertIn("source-specific receipt contract", report["queue"][0]["reason"])

    def test_incomplete_data_go_contract_is_explicit_and_not_sent_to_runtime_batch(self) -> None:
        original_load = MODULE.load
        latest = {"generated_at": "2026-07-24T00:00:01Z", "results": []}
        projection = {
            "freshness": {"as_of": latest["generated_at"]},
            "current_evidence": [],
            "records": [],
            "operations": [
                {"identity_key": "data_go_kr:d:complete", "contract_complete": True, "incomplete_reasons": []},
                {"identity_key": "data_go_kr:d:incomplete", "contract_complete": False, "incomplete_reasons": ["missing_exact_endpoint"]},
            ],
            "summary": {},
        }
        denominator = {"summary": {"operations": 2}, "sources": []}
        policy = {"freshness": {"fresh_days": 30, "expire_days": 90, "evaluation_time_source": "latest_verification.generated_at"}}
        operations = [
            {"source_id": "data_go_kr", "dataset_id": "d", "operation": "complete", "operation_seq": None, "identity_key": "data_go_kr:d:complete"},
            {"source_id": "data_go_kr", "dataset_id": "d", "operation": "incomplete", "operation_seq": None, "identity_key": "data_go_kr:d:incomplete"},
        ]

        def load_fixture(path: pathlib.Path):
            values = {MODULE.DENOMINATORS: denominator, MODULE.REGISTRY: [], MODULE.LATEST: latest, MODULE.PROJECTION: projection, MODULE.POLICY: policy}
            return values[path] if path in values else original_load(path)

        with mock.patch.object(MODULE, "load", side_effect=load_fixture), mock.patch.object(MODULE, "supported_operations", return_value=operations):
            report = MODULE.build()

        self.assertEqual(report["summary"]["unsupported_current_binding"], 1)
        unsupported = next(row for row in report["queue"] if row["identity_key"] == "data_go_kr:d:incomplete")
        self.assertEqual(unsupported["classification"], "unsupported_current_binding")
        self.assertIn("missing_exact_endpoint", unsupported["reason"])

    def test_current_queue_reconciles_complete_denominator(self) -> None:
        registry_bytes = MODULE.REGISTRY.read_bytes() if MODULE.REGISTRY.is_file() else b""
        if registry_bytes.startswith(b"version https://git-lfs.github.com/spec/v1") or not MODULE.PROJECTION.is_file():
            self.skipTest("full registry source or regenerated current evidence projection is unavailable")
        report = MODULE.build()
        summary = report["summary"]
        self.assertEqual(summary["supported_operations"], 21260)
        self.assertEqual(summary["queued"] + summary["fresh_verified"], 21260)
        self.assertEqual(len(report["queue"]), summary["queued"])


if __name__ == "__main__":
    unittest.main()
