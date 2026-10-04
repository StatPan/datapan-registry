from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "upstream_catalogue_handoff", ROOT / "scripts/upstream_catalogue_handoff.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CollectorHandoffLedgerTests(unittest.TestCase):
    def row(self, run_id: str, observed_at: str, *, attempt: int = 1, evidence: str | None = None) -> dict:
        evidence = evidence or run_id.rjust(64, "0")[-64:]
        return {
            "admission_id": MODULE.admission_id(run_id, attempt, evidence),
            "producer_run_id": run_id,
            "run_attempt": attempt,
            "head_sha": "a" * 40,
            "run_started_at": observed_at,
            "artifact_id": str(int(run_id) + (1000 * attempt)),
            "artifact_name": f"upstream-catalog-refresh-{run_id}",
            "artifact_expires_at": "2026-11-03T00:00:00Z",
            "artifact_digest_sha256": "b" * 64,
            "artifact_size_bytes": 1234,
            "refresh_evidence_sha256": evidence,
            "observed_at": observed_at,
            "generation_id": "c" * 64,
            "candidate_sha256": "d" * 64,
            "admitted_at": "2026-10-04T00:00:00Z",
        }

    @staticmethod
    def candidate(run_id: str, started_at: str, attempt: int = 1) -> dict:
        started = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        return {
            "producer_run_id": run_id,
            "run_attempt": attempt,
            "artifact_id": str(int(run_id) + (1000 * attempt)),
            "artifact_digest_sha256": "b" * 64,
            "run_started_at": started_at,
            "run_completed_at": (started + dt.timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        }

    def test_old_ready_generation_does_not_hide_new_unseen_observation(self) -> None:
        ledger = MODULE.empty_ledger({
            "producer_run_id": "100",
            "observed_at": "2026-10-01T00:00:00Z",
            "run_started_at": "2026-10-01T00:00:00Z",
            "generation_id": "c" * 64,
            "evidence_sha256": "1" * 64,
            "basis": "sealed_checkpoint_input_reference",
        })
        # The oldest checkpoint can still be ready with pending detail work;
        # selection is from trusted observations and the durable ledger.
        candidates = [
            self.candidate("100", "2026-10-01T00:00:00Z"),
            self.candidate("200", "2026-10-03T00:00:00Z"),
        ]
        self.assertEqual(
            MODULE.choose_oldest_unseen(candidates, ledger)["producer_run_id"], "200",
        )

    def test_distinct_producers_with_same_generation_are_retained_oldest_first(self) -> None:
        index = {"schema_version": "v1", "generations": []}
        first = self.row("200", "2026-10-02T00:00:00Z", evidence="1" * 64)
        second = self.row("201", "2026-10-03T00:00:00Z", evidence="2" * 64)
        MODULE.add_admission(index, first, now="2026-10-04T00:00:00Z")
        MODULE.add_admission(index, second, now="2026-10-04T00:00:00Z")
        ledger = MODULE.validate_ledger(index["collector_handoff"])
        self.assertEqual([item["producer_run_id"] for item in ledger["admitted_observations"]], ["200", "201"])
        self.assertEqual({item["generation_id"] for item in ledger["admitted_observations"]}, {"c" * 64})
        self.assertTrue(MODULE.was_admitted(ledger, self.candidate("201", "2026-10-03T00:00:00Z")))

    def test_exact_redelivery_is_idempotent_but_identity_conflict_is_rejected(self) -> None:
        index = {"schema_version": "v1", "generations": []}
        row = self.row("200", "2026-10-02T00:00:00Z")
        MODULE.add_admission(index, row, now="2026-10-04T00:00:00Z")
        before = copy.deepcopy(index["collector_handoff"])
        MODULE.add_admission(index, row, now="2026-10-04T00:00:00Z")
        self.assertEqual(index["collector_handoff"], before)
        later_replay = dict(row, admitted_at="2026-10-04T00:02:00Z", generation_id="e" * 64)
        MODULE.add_admission(index, later_replay, now="2026-10-04T00:02:00Z")
        self.assertEqual(index["collector_handoff"], before)
        changed = dict(row, artifact_id="9999")
        with self.assertRaisesRegex(MODULE.HandoffError, "identity_conflict"):
            MODULE.add_admission(index, changed, now="2026-10-04T00:00:00Z")

    def test_legacy_floor_binds_observation_not_generator_derived_generation(self) -> None:
        row = self.row("200", "2026-10-02T00:00:00Z")
        floor = {
            "producer_run_id": row["producer_run_id"],
            "artifact_id": row["artifact_id"],
            "observed_at": row["observed_at"],
            "generation_id": "e" * 64,
            "evidence_sha256": row["refresh_evidence_sha256"],
            "basis": "sealed_checkpoint_input_reference",
        }
        index = {"schema_version": "v1", "generations": []}

        MODULE.add_admission(index, row, now="2026-10-04T00:00:00Z", legacy_floor=floor)

        ledger = MODULE.validate_ledger(index["collector_handoff"])
        self.assertEqual(ledger["legacy_discovery_floor"], floor)
        self.assertEqual(len(ledger["admitted_observations"]), 1)
        self.assertEqual(ledger["admitted_observations"][0]["generation_id"], row["generation_id"])

    def test_conflicting_artifact_for_same_exact_attempt_is_rejected(self) -> None:
        index = {"schema_version": "v1", "generations": []}
        first = self.row("200", "2026-10-02T00:00:00Z", evidence="1" * 64)
        second = dict(
            self.row("200", "2026-10-02T00:00:01Z", evidence="2" * 64),
            artifact_id="9999", artifact_digest_sha256="f" * 64,
        )
        MODULE.add_admission(index, first, now="2026-10-04T00:00:00Z")
        with self.assertRaisesRegex(MODULE.HandoffError, "attempt_identity_conflict"):
            MODULE.add_admission(index, second, now="2026-10-04T00:00:00Z")

    def test_older_unadmitted_source_time_is_actionable_not_replayed(self) -> None:
        floor = {
            "producer_run_id": "200",
            "observed_at": "2026-10-03T00:00:00Z",
            "generation_id": "c" * 64,
            "evidence_sha256": "1" * 64,
            "basis": "sealed_checkpoint_input_reference",
        }
        index = {"schema_version": "v1", "generations": []}
        with self.assertRaisesRegex(MODULE.HandoffError, "older_unadmitted"):
            MODULE.add_admission(index, self.row("199", "2026-10-02T00:00:00Z"),
                                 now="2026-10-04T00:00:00Z", legacy_floor=floor)

    def test_floor_without_attempt_skips_older_history_but_not_a_later_attempt(self) -> None:
        floor = {
            "producer_run_id": "200", "artifact_id": "1200",
            "observed_at": "2026-10-02T00:05:00Z",
            "generation_id": "c" * 64, "evidence_sha256": "1" * 64,
            "basis": "sealed_checkpoint_input_reference",
        }
        ledger = MODULE.empty_ledger(floor)
        candidates = [
            self.candidate("199", "2026-10-01T23:00:00Z"),
            self.candidate("200", "2026-10-02T00:00:00Z", attempt=1),
            self.candidate("200", "2026-10-03T00:00:00Z", attempt=2),
            self.candidate("201", "2026-10-04T00:00:00Z"),
        ]
        self.assertEqual(MODULE.choose_oldest_unseen(candidates, ledger)["producer_run_id"], "200")
        chosen = MODULE.choose_oldest_unseen(candidates[1:], ledger)
        self.assertEqual(chosen["run_attempt"], 2)

    def test_corrupt_duplicate_or_unbounded_ledger_fails_closed(self) -> None:
        row = self.row("200", "2026-10-02T00:00:00Z")
        duplicate = MODULE.empty_ledger()
        duplicate["admitted_observations"] = [row, row]
        with self.assertRaisesRegex(MODULE.HandoffError, "duplicate"):
            MODULE.validate_ledger(duplicate)
        oversized = MODULE.empty_ledger()
        oversized["admitted_observations"] = [dict(row, admission_id=f"{i:064x}") for i in range(257)]
        with self.assertRaises(MODULE.HandoffError):
            MODULE.validate_ledger(oversized)
        with self.assertRaisesRegex(MODULE.HandoffError, "invalid"):
            MODULE.admission_id("200", True, "0" * 64)


class FakeActionsApi:
    workflow_id = 88
    repo = "StatPan/datapan-registry"
    run_id = "200"
    attempt = 1
    head = "a" * 40
    started = "2026-10-03T00:00:00Z"
    completed = "2026-10-03T00:02:00Z"
    observe_completed = "2026-10-03T00:01:45Z"
    artifact_id = "1200"
    artifact_digest = "b" * 64

    def __init__(self, *, drift_after: bool = False) -> None:
        self.calls: list[str] = []
        self.run_reads = 0
        self.drift_after = drift_after

    def run(self) -> dict:
        attempt = self.attempt + (1 if self.drift_after and self.run_reads >= 1 else 0)
        return {
            "id": int(self.run_id), "workflow_id": self.workflow_id,
            "name": "Upstream catalog refresh", "path": ".github/workflows/upstream-catalog-refresh.yml",
            "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
            "run_attempt": attempt, "head_branch": "main", "head_sha": self.head,
            "repository": {"id": 123, "full_name": self.repo},
            "head_repository": {"id": 123, "full_name": self.repo},
            "run_started_at": self.started, "updated_at": self.completed,
            "html_url": f"https://github.com/{self.repo}/actions/runs/{self.run_id}",
        }

    def get(self, endpoint: str) -> dict:
        self.calls.append(endpoint)
        if endpoint == f"repos/{self.repo}/actions/workflows/upstream-catalog-refresh.yml":
            return {"id": self.workflow_id, "name": "Upstream catalog refresh",
                    "path": ".github/workflows/upstream-catalog-refresh.yml", "state": "active"}
        if endpoint.startswith(f"repos/{self.repo}/actions/workflows/{self.workflow_id}/runs?"):
            return {"total_count": 1, "workflow_runs": [self.run()]}
        if endpoint == f"repos/{self.repo}/actions/runs/{self.run_id}":
            value = self.run()
            self.run_reads += 1
            return value
        if endpoint.startswith(f"repos/{self.repo}/actions/runs/{self.run_id}/attempts/{self.attempt}/jobs?"):
            return {"total_count": 1, "jobs": [{
                "name": "observe", "status": "completed", "conclusion": "success",
                "id": 99, "run_id": int(self.run_id), "run_attempt": self.attempt,
                "head_sha": self.head,
                "started_at": self.started, "completed_at": self.observe_completed,
            }]}
        if endpoint.startswith(f"repos/{self.repo}/actions/runs/{self.run_id}/artifacts?"):
            return {"total_count": 1, "artifacts": [{
                "id": int(self.artifact_id), "name": f"upstream-catalog-refresh-{self.run_id}",
                "expired": False, "expires_at": "2026-11-02T00:00:00Z",
                "created_at": "2026-10-03T00:01:00Z", "size_in_bytes": 1234,
                "digest": f"sha256:{self.artifact_digest}",
                "workflow_run": {
                    "id": int(self.run_id), "head_sha": self.head, "head_branch": "main",
                    "repository_id": 123, "head_repository_id": 123,
                },
            }]}
        raise AssertionError(f"unexpected API endpoint: {endpoint}")


class BoundedDiscoveryTests(unittest.TestCase):
    def test_discovery_selects_oldest_authenticated_exact_observation(self) -> None:
        api = FakeActionsApi()
        selected, diagnostics, requests = MODULE.discover_oldest_unseen(
            api.get, repository=api.repo, default_branch="main",
            now=dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc),
            ledger_value=MODULE.empty_ledger(),
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual(selected["producer_run_id"], api.run_id)
        self.assertEqual(selected["run_attempt"], 1)
        self.assertEqual(selected["artifact_id"], api.artifact_id)
        self.assertEqual(selected["run_completed_at"], api.observe_completed)
        self.assertEqual(selected["run_updated_at"], api.completed)
        self.assertLessEqual(requests, MODULE.MAX_API_REQUESTS)
        self.assertTrue(any("/attempts/1/jobs" in call for call in api.calls))
        self.assertEqual(api.calls.count(f"repos/{api.repo}/actions/runs/{api.run_id}"), 2)

    def test_discovery_fails_closed_when_attempt_changes_during_artifact_read(self) -> None:
        api = FakeActionsApi(drift_after=True)
        with self.assertRaisesRegex(MODULE.HandoffError, "attempt_changed"):
            MODULE.discover_oldest_unseen(
                api.get, repository=api.repo, default_branch="main",
                now=dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc),
                ledger_value=MODULE.empty_ledger(),
            )

    def test_discovery_requires_artifact_creation_inside_observe_attempt(self) -> None:
        api = FakeActionsApi()
        original = api.get

        def altered(endpoint: str) -> dict:
            result = original(endpoint)
            if "/artifacts?" in endpoint:
                result["artifacts"][0]["created_at"] = "2026-10-02T23:59:00Z"
            return result

        with self.assertRaisesRegex(MODULE.HandoffError, "not_bound_to_exact_attempt"):
            MODULE.discover_oldest_unseen(
                altered, repository=api.repo, default_branch="main",
                now=dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc),
                ledger_value=MODULE.empty_ledger(),
            )

    def test_discovery_skips_five_admitted_attempts_and_finds_the_next_observation(self) -> None:
        api = FakeActionsApi()
        api.run_id = "205"
        api.artifact_id = "1205"
        api.started = "2026-10-03T00:05:00Z"
        api.completed = "2026-10-03T00:07:00Z"
        api.observe_completed = "2026-10-03T00:06:45Z"
        original = api.get
        summary_rows = []
        for offset, run_id in enumerate(range(200, 206)):
            item = api.run()
            item.update({
                "id": run_id,
                "run_started_at": f"2026-10-03T00:0{offset}:00Z",
                "updated_at": api.completed if run_id == 205 else f"2026-10-03T00:0{offset}:45Z",
                "html_url": f"https://github.com/{api.repo}/actions/runs/{run_id}",
            })
            summary_rows.append(item)

        def get(endpoint: str) -> dict:
            if "/actions/workflows/88/runs?" in endpoint:
                api.calls.append(endpoint)
                return {"total_count": len(summary_rows), "workflow_runs": summary_rows}
            if "/actions/runs/205/artifacts?" in endpoint:
                result = original(endpoint)
                result["artifacts"][0]["created_at"] = "2026-10-03T00:06:00Z"
                return result
            if "/actions/runs/" in endpoint:
                match = re.search(r"/actions/runs/(\d+)", endpoint)
                if match and match.group(1) != api.run_id:
                    raise AssertionError(f"already-admitted run was unnecessarily inspected: {endpoint}")
            return original(endpoint)

        ledger_index = {"schema_version": "v1", "generations": []}
        for offset, run_id in enumerate(range(200, 205)):
            start = f"2026-10-03T00:0{offset}:00Z"
            row = CollectorHandoffLedgerTests().row(str(run_id), start)
            MODULE.add_admission(ledger_index, row, now="2026-10-04T00:00:00Z")
        ledger = ledger_index["collector_handoff"]

        selected, diagnostics, _requests = MODULE.discover_oldest_unseen(
            get, repository=api.repo, default_branch="main",
            now=dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc), ledger_value=ledger,
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual(selected["producer_run_id"], "205")
        self.assertEqual(selected["artifact_id"], "1205")
        self.assertTrue(any("/actions/runs/205/attempts/1/jobs" in call for call in api.calls))


if __name__ == "__main__":
    unittest.main()
