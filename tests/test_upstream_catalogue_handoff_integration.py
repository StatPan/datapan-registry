from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_HEAD = "a" * 40
EXPIRES = "2026-11-03T00:00:00Z"
REPOSITORY = "StatPan/datapan-registry"
STATE_BRANCH = "automation/upstream-catalogue-state"
STATE_ROOT = ".datapan/upstream-catalogue-state"


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def processor_fixture(test_case: unittest.TestCase):
    support = load_module(
        ROOT / "tests/test_process_upstream_catalogue_candidate.py",
        f"processor_support_{id(test_case)}",
    )
    fixture = support.UpstreamCatalogueProcessorTest(
        "test_claim_reserves_attempts_durably_before_any_detail_request",
    )
    fixture.setUp()
    test_case.addCleanup(fixture.tearDown)
    return support, fixture, support.MODULE


def rest_row(identity: str = "11") -> dict:
    url = f"https://www.data.go.kr/data/{identity}/openapi.do"
    return {
        "id": identity,
        "title": f"Safe REST addition {identity}",
        "provider": "data.go.kr",
        "priority": "medium",
        "operations": [],
        "source": {
            "system": "data.go.kr",
            "url": url,
            "raw": {
                "api_type": "REST", "type": "REST", "title": f"Safe REST addition {identity}",
                "meta_url": url,
            },
        },
    }


def write_admission_bundle(
    fixture, processor, *, run_id: str, observed_at: str,
    started_at: str = "2026-10-03T09:00:00Z", artifact_id: str | None = None,
):
    """Create the same four-member archive/envelope shape passed by the workflow."""
    artifact_id = artifact_id or str(90_000 + int(run_id))
    archive = fixture.root / f"producer-{run_id}.zip"
    names_and_bytes = [
        ("candidate.registry.json", fixture.candidate_path.read_bytes()),
        ("catalog-diff.json", fixture.diff_path.read_bytes()),
        ("upstream-refresh-evidence.json", fixture.evidence_path.read_bytes()),
        ("upstream-refresh-work-packet.json", b'{"schema_version":"test.packet.v1"}'),
    ]
    with zipfile.ZipFile(archive, "w") as output:
        for name, payload in names_and_bytes:
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 3, 10, 3, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, payload)

    archive_sha256 = processor.file_sha256(archive)
    archive_size = archive.stat().st_size
    evidence_sha256 = processor.file_sha256(fixture.evidence_path)
    job_started = started_at.replace("09:00:00", "09:01:00")
    job_completed = "2026-10-03T10:03:00Z"
    envelope = {
        "schema_version": "datapan.upstream-catalogue-admission-envelope.v1",
        "repository": REPOSITORY,
        "producer_run_id": str(run_id),
        "run_attempt": 1,
        "head_sha": SOURCE_HEAD,
        "run_started_at": started_at,
        "run_completed_at": "2026-10-03T10:04:00Z",
        "observe_job_started_at": job_started,
        "observe_job_completed_at": job_completed,
        "artifact_id": artifact_id,
        "artifact_name": f"upstream-catalog-refresh-{run_id}",
        "artifact_expires_at": EXPIRES,
        "artifact_created_at": job_completed,
        "artifact_digest_sha256": archive_sha256,
        "artifact_size_bytes": archive_size,
        "archive_sha256": archive_sha256,
        "archive_size_bytes": archive_size,
        "observed_at": observed_at,
        "refresh_evidence_sha256": evidence_sha256,
        "event": "schedule",
    }
    admission_path = fixture.root / f"producer-{run_id}-admission.json"
    admission_path.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
    return admission_path, archive, artifact_id


def admission_args(fixture, *, run_id: str, admission_path: pathlib.Path,
                   archive: pathlib.Path, artifact_id: str, claim_only: bool):
    overrides = {
        "--producer-head-sha": SOURCE_HEAD,
        "--collector-admission-file": admission_path,
        "--collector-archive": archive,
        "--input-artifact-id": artifact_id,
        "--artifact-name": f"upstream-catalog-refresh-{run_id}",
        "--artifact-expires-at": EXPIRES,
        "--output-artifact-expires-at": EXPIRES,
        "--max-attempts": 1,
        "--max-queue": 1,
        "--retries-per-detail": 0,
    }
    if claim_only:
        overrides["--claim-only"] = None
    args = fixture.args(run_id=run_id, **overrides)
    args.claim_only = claim_only
    args.producer_head_sha = SOURCE_HEAD
    args.collector_admission_file = admission_path
    args.collector_archive = archive
    args.input_artifact_id = artifact_id
    args.artifact_name = f"upstream-catalog-refresh-{run_id}"
    args.artifact_expires_at = EXPIRES
    return args


def reserved_worker_args(fixture, *, run_id: str, artifact_id: str):
    args = fixture.args(run_id=run_id, **{
        "--input-artifact-id": artifact_id,
        "--artifact-name": f"upstream-catalog-refresh-{run_id}",
        "--artifact-expires-at": EXPIRES,
        "--max-attempts": 1,
        "--max-queue": 1,
        "--retries-per-detail": 0,
        "--require-durable-reservation": None,
    })
    args.input_artifact_id = artifact_id
    args.artifact_name = f"upstream-catalog-refresh-{run_id}"
    args.artifact_expires_at = EXPIRES
    args.require_durable_reservation = True
    return args


class FakeProducerActionsApi:
    workflow_id = 88
    repo = REPOSITORY

    def __init__(self, rows: list[dict], artifact_ids: dict[str, str] | None = None):
        self.rows = rows
        self.artifact_ids = artifact_ids or {}
        self.calls: list[str] = []

    @staticmethod
    def summary(run_id: str, started_at: str) -> dict:
        completed = "2026-10-03T10:04:00Z"
        return {
            "id": int(run_id), "workflow_id": FakeProducerActionsApi.workflow_id,
            "name": "Upstream catalog refresh",
            "path": ".github/workflows/upstream-catalog-refresh.yml",
            "event": "schedule", "status": "completed", "conclusion": "success",
            "run_attempt": 1, "head_branch": "main", "head_sha": SOURCE_HEAD,
            "repository": {"id": 123, "full_name": REPOSITORY},
            "head_repository": {"id": 123, "full_name": REPOSITORY},
            "run_started_at": started_at, "updated_at": completed,
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        }

    def get(self, endpoint: str) -> dict:
        self.calls.append(endpoint)
        if endpoint == f"repos/{self.repo}/actions/workflows/upstream-catalog-refresh.yml":
            return {"id": self.workflow_id, "name": "Upstream catalog refresh",
                    "path": ".github/workflows/upstream-catalog-refresh.yml", "state": "active"}
        if endpoint.startswith(f"repos/{self.repo}/actions/workflows/{self.workflow_id}/runs?"):
            return {"total_count": len(self.rows), "workflow_runs": self.rows}
        for row in self.rows:
            run_id = str(row["id"])
            run_endpoint = f"repos/{self.repo}/actions/runs/{run_id}"
            if endpoint == run_endpoint:
                return dict(row)
            if endpoint.startswith(f"{run_endpoint}/attempts/1/jobs?"):
                started = datetime.fromisoformat(str(row["run_started_at"]).replace("Z", "+00:00"))
                job_started = (started + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
                return {"total_count": 1, "jobs": [{
                    "id": int(run_id) + 500_000, "name": "observe", "run_id": int(run_id),
                    "run_attempt": 1, "head_sha": SOURCE_HEAD,
                    "status": "completed", "conclusion": "success",
                    "started_at": job_started,
                    "completed_at": "2026-10-03T10:03:00Z",
                }]}
            if endpoint.startswith(f"{run_endpoint}/artifacts?"):
                artifact_id = self.artifact_ids.get(run_id, str(90_000 + int(run_id)))
                return {"total_count": 1, "artifacts": [{
                    "id": int(artifact_id), "name": f"upstream-catalog-refresh-{run_id}",
                    "expired": False, "expires_at": EXPIRES,
                    "created_at": "2026-10-03T10:03:00Z", "size_in_bytes": 1234,
                    "digest": "sha256:" + ("b" * 64),
                    "workflow_run": {
                        "id": int(run_id), "head_sha": SOURCE_HEAD, "head_branch": "main",
                        "repository_id": 123, "head_repository_id": 123,
                    },
                }]}
        raise AssertionError(f"unexpected API endpoint: {endpoint}")


class UpstreamCatalogueHandoffIntegrationTests(unittest.TestCase):
    def test_initial_empty_index_claim_admits_exact_observation_and_binds_helper_revision(self):
        _support, fixture, processor = processor_fixture(self)
        observed_at = "2026-10-03T10:02:00Z"
        fixture.write_real_composer_inputs([], [rest_row()], observed_at)
        fixture.now = "2026-10-04T00:00:00Z"
        admission_path, archive, artifact_id = write_admission_bundle(
            fixture, processor, run_id="501", observed_at=observed_at,
        )
        args = admission_args(
            fixture, run_id="501", admission_path=admission_path,
            archive=archive, artifact_id=artifact_id, claim_only=True,
        )
        code, checkpoint = processor.process(
            args, fetcher=lambda *_: self.fail("claim admission must not make a provider request"),
        )
        self.assertEqual(code, 0, checkpoint.get("outcome"))
        self.assertEqual(checkpoint["status"], "enriching")
        self.assertEqual(checkpoint["request_reservation"]["owner_run_id"], "501")
        self.assertEqual(checkpoint["request_reservation"]["reserved_attempts"], 0)
        self.assertEqual(checkpoint["attempts_consumed"], 0)
        self.assertEqual(checkpoint["detail_queue_cursor"], 0)
        self.assertEqual(checkpoint["observation_count"], 1)
        self.assertEqual(checkpoint["generation_inputs"]["generator_revision"], processor.generator_revision())

        index_path = fixture.state_dir / "sources/data_go_kr/index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        ledger = index["collector_handoff"]
        self.assertEqual(ledger["legacy_discovery_floor"]["basis"], "bounded_initial_lookback")
        self.assertEqual(len(ledger["admitted_observations"]), 1)
        row = ledger["admitted_observations"][0]
        self.assertEqual(row["producer_run_id"], "501")
        self.assertEqual(row["run_attempt"], 1)
        self.assertEqual(row["artifact_id"], artifact_id)
        self.assertEqual(row["generation_id"], checkpoint["generation_id"])
        self.assertEqual(row["candidate_sha256"], checkpoint["generation_inputs"]["candidate_sha256"])
        self.assertEqual(row["observed_at"], observed_at)
        self.assertTrue(fixture.checkpoint_path(checkpoint).is_file())
        indexed = next(item for item in index["generations"] if item["generation_id"] == checkpoint["generation_id"])
        self.assertEqual(indexed["status"], "enriching")

    @unittest.skipUnless(
        (ROOT / "scripts/compose-upstream-catalogue-candidate.py").is_file(),
        "ready-with-pending-detail fixture requires the local reviewed composer",
    )
    def test_missed_observation_claim_preserves_old_ready_detail_retry_state(self):
        _support, fixture, processor = processor_fixture(self)
        old_observed_at = "2026-10-03T09:40:00Z"
        link = _support.UpstreamCatalogueProcessorTest.real_link_row()
        fixture.write_real_composer_inputs([], [link, rest_row("11")], old_observed_at)
        fixture.now = "2026-10-03T10:00:00Z"
        old_args = fixture.args(run_id="601001", **{
            "--composer": ROOT / "scripts/compose-upstream-catalogue-candidate.py",
            "--input-artifact-id": "9601001",
            "--artifact-name": "upstream-catalog-refresh-601001",
            "--artifact-expires-at": EXPIRES,
            "--max-attempts": 1,
            "--max-queue": 1,
            "--retries-per-detail": 2,
        })
        old_args.fixture_composer = None
        old_args.allow_fixture_composer = False

        def fail_detail(_url: str, _timeout: float):
            raise TimeoutError("fixture-only detail failure")

        old_code, old = processor.process(old_args, fetcher=fail_detail, sleeper=lambda _delay: None)
        self.assertEqual(old_code, 0, old.get("outcome"))
        self.assertEqual(old["status"], "ready")
        self.assertEqual(old["outcome"]["detail_retry_count"], 1)
        self.assertEqual(old["detail_queue_cursor"], 0)
        self.assertIsNone(old["lease"])
        old_checkpoint_bytes = fixture.checkpoint_path(old).read_bytes()
        index_path = fixture.state_dir / "sources/data_go_kr/index.json"
        before_index = json.loads(index_path.read_text(encoding="utf-8"))
        old_retry_state = json.loads(json.dumps(before_index["detail_retry_state"]))
        self.assertTrue(old_retry_state)

        # A later scheduled observation has a distinct candidate generation but
        # no LINK work of its own. Admission/claim must not reset the old ready
        # generation's bounded retry history or source-wide cursor.
        observed_at = "2026-10-03T10:02:00Z"
        fixture.write_real_composer_inputs([], [rest_row("12")], observed_at)
        fixture.now = "2026-10-03T10:10:00Z"
        admission_path, archive, artifact_id = write_admission_bundle(
            fixture, processor, run_id="601002", observed_at=observed_at,
            started_at="2026-10-03T09:30:00Z",
        )
        args = admission_args(
            fixture, run_id="601002", admission_path=admission_path,
            archive=archive, artifact_id=artifact_id, claim_only=True,
        )
        code, current = processor.process(
            args, fetcher=lambda *_: self.fail("claim admission must precede detail calls"),
        )
        self.assertEqual(code, 0, current.get("outcome"))
        self.assertNotEqual(current["generation_id"], old["generation_id"])
        self.assertEqual(current["request_reservation"]["reserved_attempts"], 0)
        self.assertEqual(current["detail_queue_cursor"], 0)
        self.assertEqual(old_retry_state, json.loads(index_path.read_text(encoding="utf-8"))["detail_retry_state"])
        self.assertEqual(fixture.checkpoint_path(old).read_bytes(), old_checkpoint_bytes)
        final_index = json.loads(index_path.read_text(encoding="utf-8"))
        old_indexed = next(item for item in final_index["generations"] if item["generation_id"] == old["generation_id"])
        new_indexed = next(item for item in final_index["generations"] if item["generation_id"] == current["generation_id"])
        self.assertEqual(old_indexed["status"], "ready")
        self.assertEqual(new_indexed["status"], "enriching")
        admitted = final_index["collector_handoff"]["admitted_observations"]
        self.assertEqual([item["producer_run_id"] for item in admitted], ["601002"])
        self.assertEqual(admitted[0]["generation_id"], current["generation_id"])

    def test_same_generation_admits_distinct_producers_but_exact_replay_is_idempotent(self):
        _support, fixture, processor = processor_fixture(self)
        observed_at = "2026-10-03T10:02:00Z"
        fixture.write_real_composer_inputs([], [rest_row()], observed_at)
        fixture.now = "2026-10-03T10:05:00Z"

        snapshots = {}
        artifact_ids = {}
        for run_id in ("701001", "701002"):
            admission_path, archive, artifact_id = write_admission_bundle(
                fixture, processor, run_id=run_id, observed_at=observed_at,
                started_at="2026-10-03T09:00:00Z" if run_id == "701001" else "2026-10-03T09:30:00Z",
            )
            artifact_ids[run_id] = artifact_id
            args = admission_args(
                fixture, run_id=run_id, admission_path=admission_path,
                archive=archive, artifact_id=artifact_id, claim_only=True,
            )
            code, checkpoint = processor.process(args, fetcher=lambda *_: self.fail("REST-only fixture has no detail requests"))
            self.assertEqual(code, 0, checkpoint.get("outcome"))
            worker_args = reserved_worker_args(fixture, run_id=run_id, artifact_id=artifact_id)
            code, checkpoint = processor.process(
                worker_args, fetcher=lambda *_: self.fail("REST-only fixture has no detail requests"),
            )
            self.assertEqual(code, 0, checkpoint.get("outcome"))
            snapshots[run_id] = checkpoint
            fixture.now = "2026-10-03T10:06:00Z" if run_id == "701001" else "2026-10-03T10:07:00Z"

        self.assertEqual(snapshots["701001"]["generation_id"], snapshots["701002"]["generation_id"])
        self.assertEqual(snapshots["701002"]["observation_count"], 2)
        self.assertEqual(snapshots["701002"]["last_observation"]["producer_run_id"], "701002")

        first_admission = fixture.root / "producer-701001-admission.json"
        first_archive = fixture.root / "producer-701001.zip"
        replay_args = admission_args(
            fixture, run_id="701001", admission_path=first_admission,
            archive=first_archive, artifact_id=artifact_ids["701001"], claim_only=True,
        )
        code, replay = processor.process(
            replay_args, fetcher=lambda *_: self.fail("exact replay must not issue detail requests"),
        )
        self.assertEqual(code, 0, replay.get("outcome"))
        self.assertEqual(replay["observation_count"], 2)
        self.assertEqual(replay["last_observation"]["producer_run_id"], "701002")
        index = json.loads((fixture.state_dir / "sources/data_go_kr/index.json").read_text(encoding="utf-8"))
        rows = index["collector_handoff"]["admitted_observations"]
        self.assertEqual([row["producer_run_id"] for row in rows], ["701001", "701002"])
        self.assertEqual({row["generation_id"] for row in rows}, {snapshots["701001"]["generation_id"]})

    def test_older_after_newer_replay_keeps_last_observation_and_count(self):
        _support, fixture, processor = processor_fixture(self)
        old_at = "2026-10-03T09:40:00Z"
        newer_at = "2026-10-03T10:02:00Z"
        fixture.write_real_composer_inputs([], [rest_row()], old_at)
        fixture.now = "2026-10-03T10:05:00Z"
        old_env, old_zip, old_artifact = write_admission_bundle(fixture, processor, run_id="801001", observed_at=old_at)
        code, first = processor.process(
            admission_args(fixture, run_id="801001", admission_path=old_env, archive=old_zip,
                           artifact_id=old_artifact, claim_only=True),
            fetcher=lambda *_: self.fail("REST-only fixture has no detail requests"),
        )
        self.assertEqual(code, 0, first.get("outcome"))
        code, first = processor.process(
            reserved_worker_args(fixture, run_id="801001", artifact_id=old_artifact),
            fetcher=lambda *_: self.fail("REST-only fixture has no detail requests"),
        )
        self.assertEqual(code, 0, first.get("outcome"))
        original_candidate = fixture.candidate_path.read_bytes()
        original_diff = fixture.diff_path.read_bytes()
        original_evidence = fixture.evidence_path.read_bytes()

        fixture.write_real_composer_inputs([], [rest_row()], newer_at)
        fixture.now = "2026-10-03T10:10:00Z"
        new_env, new_zip, new_artifact = write_admission_bundle(
            fixture, processor, run_id="801002", observed_at=newer_at,
            started_at="2026-10-03T09:30:00Z",
        )
        code, newer = processor.process(
            admission_args(fixture, run_id="801002", admission_path=new_env, archive=new_zip,
                           artifact_id=new_artifact, claim_only=True),
            fetcher=lambda *_: self.fail("REST-only fixture has no detail requests"),
        )
        self.assertEqual(code, 0, newer.get("outcome"))
        code, newer = processor.process(
            reserved_worker_args(fixture, run_id="801002", artifact_id=new_artifact),
            fetcher=lambda *_: self.fail("REST-only fixture has no detail requests"),
        )
        self.assertEqual(code, 0, newer.get("outcome"))
        self.assertEqual(newer["observation_count"], 2)

        fixture.candidate_path.write_bytes(original_candidate)
        fixture.diff_path.write_bytes(original_diff)
        fixture.evidence_path.write_bytes(original_evidence)
        fixture.now = "2026-10-03T10:15:00Z"
        code, replay = processor.process(
            admission_args(fixture, run_id="801001", admission_path=old_env, archive=old_zip,
                           artifact_id=old_artifact, claim_only=True),
            fetcher=lambda *_: self.fail("older replay must not issue detail requests"),
        )
        self.assertEqual(code, 0, replay.get("outcome"))
        self.assertEqual(replay["observation_count"], 2)
        self.assertEqual(replay["last_observation"]["producer_run_id"], "801002")
        self.assertEqual(replay["last_observation"]["observed_at"], newer_at)

    def test_handoff_cli_catches_missed_observation_ahead_of_old_ready_details(self):
        support, fixture, processor = processor_fixture(self)
        composer = ROOT / "scripts/compose-upstream-catalogue-candidate.py"
        self.assertTrue(composer.is_file())
        old_at = "2026-10-03T09:40:00Z"
        fixture.write_real_composer_inputs(
            [], [_support_link(support), rest_row("11")], old_at,
        )
        fixture.now = "2026-10-03T10:00:00Z"
        old_args = fixture.args(run_id="901001", **{
            "--composer": composer,
            "--input-artifact-id": "9901001",
            "--artifact-name": "upstream-catalog-refresh-901001",
            "--artifact-expires-at": EXPIRES,
            "--max-attempts": 1, "--max-queue": 1, "--retries-per-detail": 2,
        })
        old_args.fixture_composer = None
        old_args.allow_fixture_composer = False
        _, old = processor.process(old_args, fetcher=lambda *_: (_ for _ in ()).throw(TimeoutError()), sleeper=lambda _delay: None)
        self.assertEqual(old["status"], "ready")
        self.assertEqual(old["outcome"]["detail_retry_count"], 1)

        handoff = load_module(ROOT / "scripts/upstream_catalogue_handoff.py", f"handoff_cli_{id(self)}")
        rows = [
            FakeProducerActionsApi.summary("901001", "2026-10-03T09:00:00Z"),
            FakeProducerActionsApi.summary("901002", "2026-10-03T09:30:00Z"),
        ]
        api = FakeProducerActionsApi(rows, artifact_ids={"901001": "9901001"})
        stdout = io.StringIO()
        with mock.patch.object(handoff, "gh_api_json", side_effect=api.get), contextlib.redirect_stdout(stdout):
            result = handoff.main([
                "--repository", REPOSITORY, "--default-branch", "main",
                "--state-dir", str(fixture.state_dir), "--now", "2026-10-03T10:10:00Z",
            ])
        self.assertEqual(result, 0)
        output = dict(line.split("=", 1) for line in stdout.getvalue().splitlines() if "=" in line)
        self.assertEqual(output["available"], "true")
        self.assertEqual(output["producer_run_id"], "901002")
        self.assertNotEqual(output["producer_run_id"], old["last_observation"]["producer_run_id"])
        self.assertTrue(any("actions/runs/901002/attempts/1/jobs" in item for item in api.calls))

    def test_handoff_cli_skips_five_admitted_candidates_and_resolves_sixth(self):
        handoff = load_module(ROOT / "scripts/upstream_catalogue_handoff.py", f"handoff_six_{id(self)}")
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = pathlib.Path(temporary)
            index_path = state_dir / "sources/data_go_kr/index.json"
            index_path.parent.mkdir(parents=True)
            ledger = handoff.empty_ledger()
            rows = [
                FakeProducerActionsApi.summary(str(run_id), f"2026-10-03T09:{run_id - 100:02d}:00Z")
                for run_id in range(100, 106)
            ]
            for run_id in range(100, 105):
                observed = f"2026-10-03T09:{run_id - 100:02d}:00Z"
                evidence = f"{run_id:064x}"
                ledger["admitted_observations"].append({
                    "admission_id": handoff.admission_id(str(run_id), 1, evidence),
                    "producer_run_id": str(run_id), "run_attempt": 1,
                    "head_sha": SOURCE_HEAD, "run_started_at": observed,
                    "artifact_id": str(90_000 + run_id),
                    "artifact_name": f"upstream-catalog-refresh-{run_id}",
                    "artifact_expires_at": EXPIRES,
                    "artifact_digest_sha256": "b" * 64, "artifact_size_bytes": 1234,
                    "refresh_evidence_sha256": evidence, "observed_at": observed,
                    "generation_id": "c" * 64, "candidate_sha256": "d" * 64,
                    "admitted_at": "2026-10-03T10:00:00Z",
                })
            index_path.write_text(json.dumps({
                "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
                "generations": [], "detail_queue_cursor": 0,
                "detail_retry_state": {}, "collector_handoff": ledger,
            }), encoding="utf-8")
            api = FakeProducerActionsApi(rows)
            stdout = io.StringIO()
            with mock.patch.object(handoff, "gh_api_json", side_effect=api.get), contextlib.redirect_stdout(stdout):
                result = handoff.main([
                    "--repository", REPOSITORY, "--default-branch", "main",
                    "--state-dir", str(state_dir), "--now", "2026-10-03T10:10:00Z",
                ])
            self.assertEqual(result, 0)
            output = dict(line.split("=", 1) for line in stdout.getvalue().splitlines() if "=" in line)
            self.assertEqual(output["available"], "true")
            self.assertEqual(output["producer_run_id"], "105")
            self.assertLessEqual(int(output["api_requests"]), handoff.MAX_API_REQUESTS)
            self.assertFalse(any(call.endswith("/runs/100") or "/runs/101/" in call for call in api.calls))

    def test_state_branch_cas_conflict_keeps_claim_admission_remote_durable_state_unchanged(self):
        support, fixture, processor = processor_fixture(self)
        workflow_support = load_module(
            ROOT / "tests/test_upstream_catalogue_process_workflow.py",
            f"state_support_{id(self)}",
        )
        state_fixture = workflow_support.StateBranchWorkflowFixture(
            "test_first_bootstrap_and_archive_paths_use_only_owned_state_root",
        )
        state_fixture.setUp()
        self.addCleanup(state_fixture.tearDown)
        expected_empty, _ = state_fixture.prepare()
        initial = state_fixture.push(state_fixture.state, expected_empty)
        initial_sha = initial["new_sha"]

        observed_at = "2026-10-03T10:02:00Z"
        fixture.write_real_composer_inputs([], [rest_row()], observed_at)
        fixture.now = "2026-10-04T00:00:00Z"
        admission_path, archive, artifact_id = write_admission_bundle(
            fixture, processor, run_id="1001", observed_at=observed_at,
        )
        args = admission_args(
            fixture, run_id="1001", admission_path=admission_path,
            archive=archive, artifact_id=artifact_id, claim_only=True,
        )
        args.state_dir = state_fixture.state / STATE_ROOT
        code, local_claim = processor.process(args, fetcher=lambda *_: self.fail("claim made detail request"))
        self.assertEqual(code, 0, local_claim.get("outcome"))
        local_index = args.state_dir / "sources/data_go_kr/index.json"
        self.assertEqual(len(json.loads(local_index.read_text())["collector_handoff"]["admitted_observations"]), 1)

        competitor = state_fixture.root / "cas-competitor"
        support_git = lambda path, *git_args, **kwargs: subprocess.run(
            ["git", "-C", str(path), *git_args], text=True, capture_output=True,
            check=kwargs.get("check", True),
        )
        support_git(state_fixture.root, "clone", str(state_fixture.remote), str(competitor))
        support_git(competitor, "checkout", "--track", "-b", STATE_BRANCH, f"origin/{STATE_BRANCH}")
        support_git(competitor, "config", "user.name", "fixture")
        support_git(competitor, "config", "user.email", "fixture@example.test")
        quarantine = competitor / STATE_ROOT / "quarantine" / ("f" * 64 + ".json")
        quarantine.parent.mkdir(parents=True)
        quarantine.write_text('{"winner":true}\n', encoding="utf-8")
        support_git(competitor, "add", STATE_ROOT)
        support_git(competitor, "commit", "-m", "concurrent state winner")
        support_git(competitor, "push", "origin", f"HEAD:refs/heads/{STATE_BRANCH}")

        result, _payload = state_fixture.run_state(
            "commit-push", "--worktree", str(state_fixture.state), "--branch", STATE_BRANCH,
            "--repository", REPOSITORY, "--expected-old-sha", initial_sha,
            "--message", "admit producer observation", ok=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("state_branch_compare_and_swap_conflict", result.stderr)
        remote_index = subprocess.run(
            ["git", "-C", str(competitor), "show", f"HEAD:{STATE_ROOT}/sources/data_go_kr/index.json"],
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(remote_index.returncode, 0)
        self.assertTrue((args.state_dir / "sources/data_go_kr/index.json").is_file())

    def test_helper_bytes_change_generation_revision_and_identity(self):
        original_processor = ROOT / "scripts/process-upstream-catalogue-candidate.py"
        original_handoff = ROOT / "scripts/upstream_catalogue_handoff.py"
        original_detail = ROOT / "scripts/generate-batch-link-detail-registry-patches.py"
        with tempfile.TemporaryDirectory() as temporary:
            scripts = pathlib.Path(temporary) / "scripts"
            scripts.mkdir()
            copied_processor = scripts / original_processor.name
            copied_handoff = scripts / original_handoff.name
            shutil.copyfile(original_processor, copied_processor)
            shutil.copyfile(original_handoff, copied_handoff)
            shutil.copyfile(original_detail, scripts / original_detail.name)
            module_name = "upstream_catalogue_handoff"
            missing = object()
            previous_helper = sys.modules.get(module_name, missing)
            module = load_module(copied_processor, f"processor_revision_{id(self)}")
            # Importing the copied processor temporarily places its directory
            # on sys.path and may load the copied helper under its production
            # module name. Restore both process-global import registries before
            # later integration tests load the repository's real source.
            copied_path = str(scripts.resolve())
            while copied_path in sys.path:
                sys.path.remove(copied_path)
            if previous_helper is missing:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous_helper

            def expected_revision() -> str:
                payload = module.canonical_json({
                    "processor_script_sha256": hashlib.sha256(copied_processor.read_bytes()).hexdigest(),
                    "collector_handoff_helper_sha256": hashlib.sha256(copied_handoff.read_bytes()).hexdigest(),
                })
                return hashlib.sha256(payload).hexdigest()

            first_revision = module.generator_revision()
            self.assertEqual(first_revision, expected_revision())
            first_generation, first_inputs = module.generation_identity(
                "data_go_kr", "fixture:data_go_kr", "b" * 64, "c" * 64,
                None, "d" * 64, "e" * 64,
            )
            self.assertEqual(first_inputs["generator_revision"], first_revision)

            copied_handoff.write_bytes(copied_handoff.read_bytes() + b"\n# compatibility revision fixture\n")
            second_revision = module.generator_revision()
            second_generation, second_inputs = module.generation_identity(
                "data_go_kr", "fixture:data_go_kr", "b" * 64, "c" * 64,
                None, "d" * 64, "e" * 64,
            )
            self.assertNotEqual(first_revision, second_revision)
            self.assertNotEqual(first_generation, second_generation)
            self.assertEqual(second_inputs["generator_revision"], expected_revision())


def _support_link(support):
    return support.UpstreamCatalogueProcessorTest.real_link_row()


if __name__ == "__main__":
    unittest.main()
