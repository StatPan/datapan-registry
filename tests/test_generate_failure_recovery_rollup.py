from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "generate-failure-recovery-rollup.py"
SPEC = importlib.util.spec_from_file_location("failure_recovery", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def rule(name: str, threshold: int = 2, ticket: int | None = None) -> dict:
    value = {"failure_class": name, "owner": "owner", "severity": "high", "retry": {"max_attempts": 2, "backoff": "fixed"}, "recurrence_threshold": threshold, "due_days": 2, "recovery_action": "repair_failure"}
    if ticket: value["existing_ticket"] = ticket
    return value


class FailureRecoveryRollupTest(unittest.TestCase):
    def policy(self) -> dict:
        names = ["credential", "parameter", "adapter", "parser", "rate_limit", "upstream", "reference_drift", "catalog_drift", "consumer"]
        return {"classes": [rule(name, 2, 10 if name == "credential" else None) for name in names]}

    @staticmethod
    def event(when: str, source: str, kind: str, subject: str, healthy: bool = False) -> dict:
        return {"observed_at": when, "source_id": source, "failure_class": kind, "subject_id": subject, "healthy": healthy, "evidence": f"evidence/{when}-{source}.json"}

    def test_transient_retries_then_persistent_events_dedupe_to_one_ticket(self) -> None:
        policy = self.policy()
        one = {"generated_at": "2026-01-02T00:00:00Z", "observations": [self.event("2026-01-01T00:00:00Z", "a", "credential", "x")]}
        transient = MODULE.build(policy, one)
        self.assertEqual(transient["summary"]["transient"], 1)
        self.assertEqual(transient["work_items"], [])
        repeated = {"generated_at": "2026-01-04T00:00:00Z", "observations": one["observations"] + [self.event("2026-01-02T00:00:00Z", "a", "credential", "x"), self.event("2026-01-01T00:00:00Z", "b", "credential", "y"), self.event("2026-01-02T00:00:00Z", "b", "credential", "y")]}
        report = MODULE.build(policy, repeated)
        self.assertEqual(report["summary"]["persistent"], 2)
        self.assertEqual(report["summary"]["overdue"], 2)
        self.assertEqual(len(report["work_items"]), 1)
        self.assertEqual(report["work_items"][0]["ticket"], 10)
        self.assertEqual(len(report["work_items"][0]["failure_ids"]), 2)

    def test_healthy_observation_creates_recovery_receipt_and_removes_active_work(self) -> None:
        observations = {"generated_at": "2026-01-03T00:00:00Z", "observations": [self.event("2026-01-01T00:00:00Z", "a", "parser", "x"), self.event("2026-01-02T00:00:00Z", "a", "parser", "x"), self.event("2026-01-03T00:00:00Z", "a", "parser", "x", True)]}
        report = MODULE.build(self.policy(), observations)
        self.assertEqual(report["summary"]["active"], 0)
        self.assertEqual(report["summary"]["recovered"], 1)
        self.assertEqual(report["work_items"], [])
        self.assertTrue(report["recovery_receipts"][0]["coverage_update_required"])

    def test_unknown_failure_class_is_rejected(self) -> None:
        observations = {"generated_at": "2026-01-01T00:00:00Z", "observations": [self.event("2026-01-01T00:00:00Z", "a", "mystery", "x")]}
        with self.assertRaisesRegex(ValueError, "unrouted"):
            MODULE.build(self.policy(), observations)

    def test_current_evidence_rejects_stale_observation(self) -> None:
        observations = {"observations": [self.event("2026-01-01T00:00:00Z", "a", "credential", "reviewed-runtime-receipt", False), self.event("2026-01-01T00:00:00Z", "multi_source", "consumer", "studio", False)]}
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            readiness = root / "readiness.json"
            compatibility = root / "compatibility.json"
            readiness.write_text(json.dumps({"sources": [{"source_id": "a", "reviewed_receipt_present": True}]}))
            compatibility.write_text(json.dumps({"consumers": [{"consumer": "studio", "status": "blocked"}]}))
            old_readiness, old_compatibility = MODULE.CREDENTIAL_READINESS, MODULE.CONSUMER_COMPATIBILITY
            try:
                MODULE.CREDENTIAL_READINESS, MODULE.CONSUMER_COMPATIBILITY = readiness, compatibility
                with self.assertRaisesRegex(ValueError, "credential observation is stale"):
                    MODULE.validate_current_evidence(observations)
            finally:
                MODULE.CREDENTIAL_READINESS, MODULE.CONSUMER_COMPATIBILITY = old_readiness, old_compatibility


if __name__ == "__main__":
    unittest.main()
