from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "project-runtime-freshness-recovery.py"
SPEC = importlib.util.spec_from_file_location("project_runtime_freshness_recovery", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProjectRuntimeFreshnessRecoveryTest(unittest.TestCase):
    def write(self, path: pathlib.Path, value: object) -> None:
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def run_fixture(self, root: pathlib.Path, result: dict[str, object], observations: dict[str, object], run_id: str):
        report, run_receipt = root / f"{run_id}-report.json", root / f"{run_id}-run-receipt.json"
        catalog, observation_path = root / "catalog.json", root / f"{run_id}-observations.json"
        output = root / f"{run_id}-import-receipt.json"
        self.write(report, {"results": [result]})
        data = report.read_bytes()
        statuses = {"verified": 0, "failed": 0, "skipped": 0, "unknown": 0}
        status = str(result.get("status", "unknown"))
        statuses[status if status in statuses else "unknown"] += 1
        self.write(run_receipt, {
            "generated_at": "2026-07-11T12:00:00Z", "run_id": run_id,
            "summary": {"reported_results": 1, **statuses},
            "combined_verification": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
            "redaction": {"secret_values_present": False, "secret_hashes_present": False, "request_urls_present": False, "response_bodies_present": False},
        })
        self.write(catalog, {
            "source_id": "data_go_kr",
            "rules": [{"rule_id": "missing-params", "status": "verified", "match": {"kind": "field_equals", "field": "reason", "value": "missing_required_params", "case_sensitive": False}, "classification": "bad_request"}],
        })
        self.write(observation_path, observations)
        return MODULE.build(report, run_receipt, catalog, observation_path, output)

    def test_classified_failure_then_healthy_result_emits_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            empty = {"schema_version": "datapan.failure-observations.v1", "generated_at": "2026-07-11T10:00:00Z", "observations": []}
            failed = {"dataset_id": "d", "operation": "o", "dependency_class": "data_go_kr_gateway", "status": "skipped", "reason": "missing_required_params", "verified_at": "2026-07-11T11:00:00Z"}
            receipt, observations = self.run_fixture(root, failed, empty, "failed")
            self.assertEqual(receipt["summary"]["classified_failures"], 1)
            self.assertEqual(observations["observations"][0]["failure_class"], "parameter")
            healthy = {"dataset_id": "d", "operation": "o", "dependency_class": "data_go_kr_gateway", "status": "verified", "reason": "ok", "verified_at": "2026-07-11T12:00:00Z"}
            recovered, updated = self.run_fixture(root, healthy, observations, "healthy")
            self.assertEqual(recovered["summary"]["healthy_recoveries"], 1)
            self.assertTrue(updated["observations"][-1]["healthy"])
            self.assertEqual(updated["observations"][-1]["subject_id"], observations["observations"][0]["subject_id"])

    def test_unmatched_failure_is_explicit_and_not_observed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            empty = {"schema_version": "datapan.failure-observations.v1", "generated_at": "2026-07-11T10:00:00Z", "observations": []}
            failed = {"dataset_id": "d", "operation": "o", "status": "failed", "reason": "HTTP 403", "verified_at": "2026-07-11T11:00:00Z"}
            receipt, observations = self.run_fixture(root, failed, empty, "unclassified")
            self.assertEqual(receipt["summary"]["unclassified_failures"], 1)
            self.assertEqual(receipt["results"][0]["disposition"], "unclassified_failure")
            self.assertEqual(observations["observations"], [])


if __name__ == "__main__":
    unittest.main()
