from __future__ import annotations

import importlib.util
import hashlib
import copy
import json
import pathlib
import subprocess
import tempfile
import unittest
import zipfile
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/run-canonical-update-promotion.py"
SPEC = importlib.util.spec_from_file_location("run_canonical_update_promotion_test_module", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class CandidateStagingTests(unittest.TestCase):
    def git(self, root: pathlib.Path, *args: str) -> str:
        return subprocess.run(("git", *args), cwd=root, check=True, text=True, capture_output=True).stdout

    def repository(self, root: pathlib.Path) -> None:
        self.git(root, "init", "-q")
        self.git(root, "config", "user.name", "Test")
        self.git(root, "config", "user.email", "test@example.invalid")
        (root / ".gitignore").write_text(".datapan/\n", encoding="utf-8")
        (root / "data").mkdir()
        (root / "data/data-go-kr.registry.json").write_text("old\n", encoding="utf-8")
        (root / "manifest.json").write_text("{}\n", encoding="utf-8")
        (root / "reports").mkdir()
        (root / "reports/catalog-audit.json").write_text("{}\n", encoding="utf-8")
        self.git(root, "add", ".gitignore", "data/data-go-kr.registry.json", "manifest.json", "reports/catalog-audit.json")
        self.git(root, "commit", "-qm", "baseline")

    def candidate_changes(self, root: pathlib.Path) -> None:
        (root / "data/data-go-kr.registry.json").write_text("candidate\n", encoding="utf-8")
        (root / "manifest.json").write_text('{"candidate":true}\n', encoding="utf-8")
        (root / "reports/catalog-audit.json").write_text('{"candidate":true}\n', encoding="utf-8")
        (root / ".datapan").mkdir()
        (root / ".datapan/local-cache.json").write_text("ignored\n", encoding="utf-8")

    def test_stages_only_explicit_generated_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            self.repository(root)
            self.candidate_changes(root)
            paths = RUNNER.stage_candidate_outputs(root, "generation-a")
            self.assertEqual(set(paths), {"data/data-go-kr.registry.json", "manifest.json", "reports/catalog-audit.json"})
            staged = set(self.git(root, "diff", "--cached", "--name-only").splitlines())
            self.assertEqual(staged, set(paths))
            self.assertNotIn(".datapan/local-cache.json", staged)

    def test_unexpected_cache_file_blocks_before_any_staging(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            self.repository(root)
            self.candidate_changes(root)
            cache = root / "scripts/__pycache__/unrelated.cpython-312.pyc"
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"cache")
            with self.assertRaisesRegex(RUNNER.PromotionError, "outside its explicit output allowlist"):
                RUNNER.stage_candidate_outputs(root, "generation-a")
            self.assertEqual(self.git(root, "diff", "--cached", "--name-only"), "")


class IdleProcessorResultTests(unittest.TestCase):
    def result_dir(self, raw: str, result: dict) -> pathlib.Path:
        directory = pathlib.Path(raw)
        (directory / "upstream-catalogue-processing-result.json").write_text(json.dumps(result), encoding="utf-8")
        return directory

    def test_exact_producer_replay_is_a_verified_no_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = {
                "status": "idle", "reason": "exact_producer_delivery_replay",
                "processing_replay": True, "candidate_available": False,
                "source_id": "data_go_kr", "producer_run_id": "36646768289",
                "processor_run_id": "70000000001-2", "processor_artifact_run_id": "70000000001",
            }
            outcome = RUNNER.validate_no_candidate_processor_result(
                self.result_dir(raw, result), repository="StatPan/datapan-registry",
                workflow_run_id="70000000001", workflow_run_attempt="2",
            )
            self.assertEqual(outcome["status"], "no-candidate")
            self.assertEqual(outcome["reason"], "exact_producer_delivery_replay")
            self.assertFalse(outcome["candidate_available"])

    def test_replay_from_another_processor_attempt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = {
                "status": "idle", "reason": "exact_producer_delivery_replay",
                "processing_replay": True, "candidate_available": False,
                "source_id": "data_go_kr", "producer_run_id": "36646768289",
                "processor_run_id": "70000000001-1", "processor_artifact_run_id": "70000000001",
            }
            with self.assertRaisesRegex(RUNNER.PromotionError, "exact processor/source identity"):
                RUNNER.validate_no_candidate_processor_result(
                    self.result_dir(raw, result), repository="StatPan/datapan-registry",
                    workflow_run_id="70000000001", workflow_run_attempt="2",
                )


    def test_idle_artifact_with_unbound_files_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = {"status": "idle", "reason": "no_active_generation"}
            directory = self.result_dir(raw, result)
            (directory / "composed-candidate.registry.json").write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(RUNNER.PromotionError, "only file"):
                RUNNER.validate_no_candidate_processor_result(
                    directory, repository="StatPan/datapan-registry",
                    workflow_run_id="70000000001", workflow_run_attempt="1",
                )


class PublicationReconciliationTests(unittest.TestCase):
    def test_publication_can_resolve_a_merge_before_hourly_pr_journal_reconciliation(self) -> None:
        # The durable candidate still records the prepared PR/head, while the
        # immutable #592 receipt already names the squash merge commit.
        candidate = {
            "status": "pending-review",
            "candidate": {"head_sha": "a" * 40, "manifest_sha256": "c" * 64},
            "pr": {"number": 42, "state": "open", "merge_commit_sha": None},
        }
        merged_pr = {
            "number": 42,
            "state": "MERGED",
            "headRefOid": "a" * 40,
            "mergeCommit": {"oid": "b" * 40},
        }
        observed_calls = []

        def readback(number: int) -> dict:
            observed_calls.append(number)
            return merged_pr

        selected, observed = RUNNER.select_publication_candidate(
            [candidate], source_sha="b" * 40, readback=readback,
        )
        self.assertIsNot(selected, candidate)
        self.assertEqual(selected, candidate)
        self.assertEqual(observed, merged_pr)
        self.assertEqual(observed_calls, [42])

    def test_publication_requires_one_exact_live_merged_pr_readback(self) -> None:
        candidate = {
            "candidate": {"head_sha": "a" * 40, "manifest_sha256": "c" * 64},
            "pr": {"number": 42},
        }
        open_pr = {"number": 42, "state": "OPEN", "mergeCommit": None}
        with self.assertRaisesRegex(RUNNER.PromotionError, "exactly one merged canonical candidate"):
            RUNNER.select_publication_candidate(
                [candidate], source_sha="b" * 40, readback=lambda _number: open_pr,
            )


class VerifyReleaseRunAdapterTests(unittest.TestCase):
    def run_value(self, attempt: int) -> dict:
        return {
            "id": 123,
            "run_attempt": attempt,
            "path": RUNNER.VERIFY_WORKFLOW_PATH,
            "head_sha": "b" * 40,
        }

    def jobs_value(self, attempt: int) -> dict:
        return {"total_count": 1, "jobs": [{"name": f"verify-attempt-{attempt}"}]}

    def test_jobs_are_read_from_the_exact_attempt_endpoint(self) -> None:
        with mock.patch.object(RUNNER, "github_api_get", side_effect=[
            self.run_value(2), self.jobs_value(2), self.run_value(2),
        ]) as api_get:
            result = RUNNER.read_verify_release_run("StatPan/datapan-registry", 123)

        self.assertEqual(result["run_attempt"], 2)
        self.assertEqual(result["jobs"][0]["name"], "verify-attempt-2")
        self.assertEqual(
            api_get.call_args_list[1].args[0],
            "repos/StatPan/datapan-registry/actions/runs/123/attempts/2/jobs?per_page=100",
        )

    def test_rerun_between_run_and_jobs_reads_retries_on_new_exact_attempt(self) -> None:
        with mock.patch.object(RUNNER, "github_api_get", side_effect=[
            self.run_value(1), self.jobs_value(1), self.run_value(2),
            self.run_value(2), self.jobs_value(2), self.run_value(2),
        ]) as api_get:
            result = RUNNER.read_verify_release_run("StatPan/datapan-registry", 123)

        self.assertEqual(result["run_attempt"], 2)
        self.assertEqual(result["jobs"][0]["name"], "verify-attempt-2")
        self.assertIn("/attempts/1/jobs?", api_get.call_args_list[1].args[0])
        self.assertIn("/attempts/2/jobs?", api_get.call_args_list[4].args[0])

    def test_persistent_rerun_race_fails_closed(self) -> None:
        with mock.patch.object(RUNNER, "github_api_get", side_effect=[
            self.run_value(1), self.jobs_value(1), self.run_value(2),
            self.run_value(2), self.jobs_value(2), self.run_value(3),
        ]):
            with self.assertRaisesRegex(RUNNER.PromotionError, "attempt changed"):
                RUNNER.read_verify_release_run("StatPan/datapan-registry", 123)

    def test_pr_readback_uses_authoritative_base_and_head_repository_fields(self) -> None:
        cli_pr = {
            "number": 652,
            "url": "https://github.com/StatPan/datapan-registry/pull/652",
            "state": "OPEN",
            "body": "owned",
            "headRefName": "automation/canonical-update/data-go-kr-aaaaaaaaaaaa",
            "headRefOid": "b" * 40,
            "baseRefName": "main",
            "mergeCommit": None,
        }
        rest_pr = {
            "base": {"repo": {"full_name": "StatPan/datapan-registry"}},
            "head": {"repo": {"full_name": "StatPan/datapan-registry"}},
        }
        with (
            mock.patch.object(RUNNER, "gh_json", return_value=cli_pr),
            mock.patch.object(RUNNER, "gh_rest_json", return_value=rest_pr),
        ):
            observed = RUNNER.gh_pr_readback(pathlib.Path("."), "StatPan/datapan-registry", 652)

        self.assertEqual(observed["repository"], "StatPan/datapan-registry")
        self.assertEqual(observed["headRepository"], "StatPan/datapan-registry")

    def test_dispatch_request_uses_documented_api_version_header(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"workflow_run_id":123,"run_url":"https://github.com/StatPan/datapan-registry/actions/runs/123"}'

        with mock.patch.dict(RUNNER.os.environ, {"GITHUB_TOKEN": "test-token"}, clear=False):
            with mock.patch.object(RUNNER.urllib.request, "urlopen", return_value=Response()) as urlopen:
                status, body = RUNNER.github_api_request(
                    "POST", "repos/StatPan/datapan-registry/actions/workflows/verify-release.yml/dispatches",
                    {"ref": "automation/canonical-update/data-go-kr-aaaaaaaaaaaa", "inputs": {"expected_head_sha": "b" * 40}},
                )

        request = urlopen.call_args.args[0]
        self.assertEqual(status, 200)
        self.assertEqual(body["workflow_run_id"], 123)
        self.assertEqual(request.get_header("X-github-api-version"), "2026-03-10")


class ProcessorBundleContractTests(unittest.TestCase):
    def test_ready_bundle_requires_eight_bound_outputs_in_frozen_order(self) -> None:
        expected = [
            "composed-candidate.registry.json",
            "ready-scope.registry.json",
            "semantic-diff.json",
            "regeneration-queue.json",
            "quarantine.json",
            "composition-receipt.json",
            "upstream-catalogue-enrichment-evidence.json",
            "upstream-catalogue-processing-result.json",
        ]
        self.assertEqual(list(RUNNER.REQUIRED_PROCESSOR_FILES), expected)
        checkpoint = {
            "status": "ready",
            "output_digests": [{"path": name, "sha256": "0" * 64, "bytes": 1} for name in expected[:-1]],
            "output_artifact": {"bundle_manifest_sha256": "0" * 64},
        }
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(RUNNER.PromotionError, "exact ordered eight-file contract"):
                RUNNER.validate_processor_bundle(checkpoint, pathlib.Path(raw), {}, None)

        checkpoint["output_digests"] = [
            {"path": name, "sha256": "0" * 64, "bytes": 1} for name in expected
        ]
        checkpoint["output_digests"][0], checkpoint["output_digests"][-1] = (
            checkpoint["output_digests"][-1], checkpoint["output_digests"][0],
        )
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(RUNNER.PromotionError, "exact ordered eight-file contract"):
                RUNNER.validate_processor_bundle(checkpoint, pathlib.Path(raw), {}, None)

    def test_result_only_quarantine_is_verified_as_no_candidate(self) -> None:
        generation = "f" * 64
        processor_run_id = "70000000001-1"
        processor_artifact_run_id = "70000000001"
        result = {
            "status": "quarantined", "reason": "input_expired", "generation_id": generation,
            "source_id": "data_go_kr", "producer_run_id": "36646768289",
            "processor_run_id": processor_run_id, "processor_artifact_run_id": processor_artifact_run_id,
            "processing_replay": False, "candidate_available": False,
        }
        with tempfile.TemporaryDirectory() as raw:
            bundle = pathlib.Path(raw)
            result_path = bundle / "upstream-catalogue-processing-result.json"
            result_path.write_bytes(RUNNER.canonical_json(result))
            digests = [{"path": result_path.name, "sha256": RUNNER.file_sha256(result_path), "bytes": result_path.stat().st_size}]
            locator = {
                "repository": "StatPan/datapan-registry", "run_id": processor_artifact_run_id,
                "name": f"upstream-catalogue-processing-{processor_run_id}", "artifact_id": "123456",
                "expires_at": "2026-10-31T00:00:00Z",
                "bundle_manifest_sha256": hashlib.sha256(RUNNER.canonical_json(digests)).hexdigest(),
            }
            checkpoint = {
                "generation_id": generation, "status": "quarantined", "source_id": "data_go_kr",
                "source_scope": "aggregate_supported_catalog", "output_digests": digests,
                "last_heartbeat_at": "2026-10-01T12:00:00Z",
                "output_artifact": locator, "last_observation": {
                    "producer_run_id": "36646768289", "observed_at": "2026-10-01T12:00:00Z",
                },
                "outcome": {"reason": "input_expired"},
            }
            uploaded_copy = dict(checkpoint)
            uploaded_copy["output_artifact"] = {
                **locator, "artifact_id": None, "expires_at": "2026-10-30T00:00:00Z",
            }
            uploaded_copy.pop("checkpoint_sha256", None)
            uploaded_copy["checkpoint_sha256"] = hashlib.sha256(RUNNER.canonical_json(uploaded_copy)).hexdigest()
            (bundle / "upstream-catalogue-checkpoint-receipt.json").write_text(json.dumps(uploaded_copy), encoding="utf-8")
            outcome = RUNNER.validate_processor_bundle(checkpoint, bundle, {}, None)
            self.assertEqual(outcome["status"], "quarantined")
            self.assertEqual(outcome["reason"], "input_expired")


class DurableProcessorRecoveryTests(unittest.TestCase):
    repository = "StatPan/datapan-registry"
    default_branch = "main"
    source_sha = "a" * 40
    expiry = "2026-10-31T00:00:00Z"

    def generation_inputs(self, candidate_sha256: str = "d" * 64) -> dict[str, object]:
        return {
            "source_id": "data_go_kr",
            "source_scope": "aggregate_supported_catalog",
            "baseline_sha256": "c" * 64,
            "candidate_sha256": candidate_sha256,
            "observation_failure_sha256": None,
            "policy_sha256": "e" * 64,
            "adapter_revision": "1" * 64,
            "generator_revision": "2" * 64,
            "extractor_revision": "3" * 64,
        }

    @staticmethod
    def seal(checkpoint: dict) -> dict:
        checkpoint.pop("checkpoint_sha256", None)
        checkpoint["checkpoint_sha256"] = hashlib.sha256(RUNNER.canonical_json(checkpoint)).hexdigest()
        return checkpoint

    def schema(self, root: pathlib.Path) -> pathlib.Path:
        path = root / "checkpoint.schema.json"
        fixture = pathlib.Path(__file__).parent / "fixtures/schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
        production = pathlib.Path(__file__).parents[1] / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
        source = production if production.is_file() else fixture
        if production.is_file():
            self.assertEqual(production.read_bytes(), fixture.read_bytes(), "test fixture must match the producer checkpoint schema")
        path.write_bytes(source.read_bytes())
        return path

    def checkpoint(
        self, *, run_id: str = "70000000001", attempt: str = "2",
        candidate_sha256: str = "d" * 64, observed_at: str = "2026-10-02T17:00:00Z",
        **locator_updates: object,
    ) -> dict:
        generation_inputs = self.generation_inputs(candidate_sha256)
        generation = hashlib.sha256(RUNNER.canonical_json(generation_inputs)).hexdigest()
        locator = {
            "repository": self.repository,
            "run_id": run_id,
            "name": f"upstream-catalogue-processing-{run_id}-{attempt}",
            "artifact_id": "123456",
            "expires_at": self.expiry,
            "bundle_manifest_sha256": "b" * 64,
        }
        locator.update(locator_updates)
        value = {
            "schema_version": RUNNER.PROCESSOR_SCHEMA,
            "source_id": "data_go_kr",
            "source_scope": "aggregate_supported_catalog",
            "generation_id": generation,
            "generation_inputs": generation_inputs,
            "observed_at": observed_at,
            "last_observation": {
                "observed_at": observed_at,
                "producer_run_id": "36646768289",
                "refresh_evidence_sha256": "f" * 64,
                "collection_status": "success",
                "execution_mode": "fixture",
            },
            "observation_count": 1,
            "last_heartbeat_at": observed_at,
            "last_progress_at": observed_at,
            "status": "ready",
            "attempts_consumed": 0,
            "attempts_by_id": {},
            "detail_retry_reset_ids": [],
            "request_reservation": None,
            "detail_records": [],
            "detail_queue_cursor": 0,
            "lease": None,
            "fencing_token": 1,
            "outcome": {"reason": "ready"},
            "output_artifact": locator,
            "output_digests": [],
            "input_artifacts": [{
                "run_id": "36646768289",
                "name": "upstream-catalogue-refresh-36646768289",
                "artifact_id": "987654",
                "expires_at": self.expiry,
                "candidate_sha256": candidate_sha256,
                "evidence_sha256": "b" * 64,
                "diff_sha256": "c" * 64,
            }],
        }
        return self.seal(value)

    def ready_bundle(
        self,
        root: pathlib.Path,
        *,
        input_digests: dict[str, object] | None = None,
    ) -> tuple[pathlib.Path, dict, dict]:
        bundle = root / "bundle"
        bundle.mkdir()
        candidate_bytes = b'{"candidate":true}\n'
        candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
        checkpoint = self.checkpoint(candidate_sha256=candidate_sha)
        baseline_sha = checkpoint["generation_inputs"]["baseline_sha256"]
        composition_inputs = input_digests or {
            "baseline": {"bytes": 24, "sha256": baseline_sha},
            "candidate": {"bytes": len(candidate_bytes), "sha256": candidate_sha},
        }
        composition = {
            "schema_version": "datapan.catalogue-composition-receipt.v1",
            "input_digests": composition_inputs,
            "status": "ready_scoped",
        }
        result = {
            "status": "ready",
            "reason": "ready",
            "generation_id": checkpoint["generation_id"],
            "source_id": checkpoint["source_id"],
            "producer_run_id": checkpoint["last_observation"]["producer_run_id"],
            "processor_run_id": "70000000001-2",
            "processor_artifact_run_id": "70000000001",
            "processing_replay": False,
            "candidate_available": True,
        }
        contents = {
            "composed-candidate.registry.json": candidate_bytes,
            "ready-scope.registry.json": b"{}\n",
            "semantic-diff.json": b"{}\n",
            "regeneration-queue.json": b"{}\n",
            "quarantine.json": b"{}\n",
            "composition-receipt.json": RUNNER.canonical_json(composition),
            "upstream-catalogue-enrichment-evidence.json": b"{}\n",
            "upstream-catalogue-processing-result.json": RUNNER.canonical_json(result),
        }
        digests = []
        for name in RUNNER.REQUIRED_PROCESSOR_FILES:
            payload = contents[name]
            (bundle / name).write_bytes(payload)
            digests.append({"path": name, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)})
        checkpoint["output_digests"] = digests
        checkpoint["output_artifact"].update({
            "artifact_id": "123456",
            "expires_at": self.expiry,
            "bundle_manifest_sha256": hashlib.sha256(RUNNER.canonical_json(digests)).hexdigest(),
        })
        # B archives before Actions binds artifact metadata and an exact idle
        # replay advances only the heartbeat in the durable checkpoint.
        uploaded_copy = copy.deepcopy(checkpoint)
        uploaded_copy["output_artifact"]["artifact_id"] = None
        uploaded_copy["output_artifact"]["expires_at"] = "2026-10-30T00:00:00Z"
        uploaded_copy["last_heartbeat_at"] = uploaded_copy["observed_at"]
        self.seal(uploaded_copy)
        checkpoint["last_heartbeat_at"] = "2026-10-03T00:05:00Z"
        self.seal(checkpoint)
        (bundle / "upstream-catalogue-checkpoint-receipt.json").write_bytes(
            RUNNER.canonical_json(uploaded_copy),
        )
        return bundle, checkpoint, uploaded_copy

    def ready_state(self, root: pathlib.Path, *checkpoints: dict) -> pathlib.Path:
        state_root = root / "state"
        source_root = state_root / "sources/data_go_kr"
        generation_root = source_root / "generations"
        generation_root.mkdir(parents=True)
        rows = []
        for checkpoint in checkpoints:
            checkpoint_path = generation_root / f"{checkpoint['generation_id']}.json"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            rows.append({
                "generation_id": checkpoint["generation_id"],
                "status": checkpoint["status"],
                "checkpoint": checkpoint_path.name,
            })
        (source_root / "index.json").write_text(json.dumps({
            "schema_version": RUNNER.PROCESSOR_SCHEMA,
            "generations": rows,
        }), encoding="utf-8")
        return state_root

    def trusted_run(self, checkpoint: dict) -> dict:
        locator = checkpoint["output_artifact"]
        return {
            "id": int(locator["run_id"]),
            "run_attempt": 2,
            "name": RUNNER.PROCESSOR_WORKFLOW_NAME,
            "path": RUNNER.PROCESSOR_WORKFLOW_PATH,
            "repository": {"full_name": self.repository},
            "head_repository": {"full_name": self.repository},
            "head_branch": self.default_branch,
            "head_sha": self.source_sha,
            "event": "schedule",
            "status": "completed",
            "conclusion": "success",
        }

    def artifact(self, checkpoint: dict) -> dict:
        locator = checkpoint["output_artifact"]
        return {
            "id": int(locator["artifact_id"]),
            "name": locator["name"],
            "expired": False,
            "expires_at": locator["expires_at"],
            "size_in_bytes": 37,
            "workflow_run": {
                "id": int(locator["run_id"]),
                "head_sha": self.source_sha,
                "head_branch": self.default_branch,
            },
        }

    def screen_candidates(self, root: pathlib.Path, checkpoints: list[dict], compatibility_side_effect) -> tuple[dict | None, list[dict[str, str]]]:
        run_by_id = {cp["output_artifact"]["run_id"]: cp for cp in checkpoints}
        artifact_by_id = {cp["output_artifact"]["artifact_id"]: cp for cp in checkpoints}

        def run_api(_root, _repository, run_id, _attempt):
            return self.trusted_run(run_by_id[run_id])

        def artifact_api(_root, _repository, _run_id, artifact_id):
            return self.artifact(artifact_by_id[artifact_id])

        with (
            mock.patch.object(RUNNER, "processor_run_api", side_effect=run_api),
            mock.patch.object(RUNNER, "processor_artifact_api", side_effect=artifact_api),
            mock.patch.object(RUNNER, "download_processor_artifact", return_value=root / "downloaded-bundle"),
            mock.patch.object(RUNNER, "validate_processor_bundle", return_value={"composition_receipt": {"input_digests": {}}}),
            mock.patch.object(RUNNER, "verify_processor_input_compatibility", side_effect=compatibility_side_effect),
        ):
            return RUNNER.select_first_eligible_processor_bundle(
                root, self.repository, checkpoints, [],
                default_branch=self.default_branch,
                current_head_sha=self.source_sha,
                composition_schema={},
                composition_helper=object(),
            )

    def test_checkpoint_recomputes_generation_identity_after_resealing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            schema = self.schema(pathlib.Path(raw))
            checkpoint = self.checkpoint()
            RUNNER.verify_processor_checkpoint(checkpoint, schema)

            resealed = copy.deepcopy(checkpoint)
            resealed["generation_inputs"]["observation_failure_sha256"] = "9" * 64
            self.seal(resealed)
            with self.assertRaisesRegex(RUNNER.PromotionError, "generation id does not bind"):
                RUNNER.verify_processor_checkpoint(resealed, schema)

            crossed_source = copy.deepcopy(checkpoint)
            crossed_source["source_id"] = "other_source"
            self.seal(crossed_source)
            with self.assertRaisesRegex(RUNNER.PromotionError, "source identity is inconsistent"):
                RUNNER.verify_processor_checkpoint(crossed_source, schema)

            crossed_scope = copy.deepcopy(checkpoint)
            crossed_scope["generation_inputs"]["source_scope"] = "other_scope"
            crossed_scope["generation_id"] = hashlib.sha256(
                RUNNER.canonical_json(crossed_scope["generation_inputs"]),
            ).hexdigest()
            self.seal(crossed_scope)
            with self.assertRaisesRegex(RUNNER.PromotionError, "source identity is inconsistent"):
                RUNNER.verify_processor_checkpoint(crossed_scope, schema)

    def test_real_ready_bundle_allows_only_artifact_binding_and_idle_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            bundle, checkpoint, _uploaded = self.ready_bundle(root)
            RUNNER.verify_processor_checkpoint(checkpoint, self.schema(root))
            helper = mock.Mock()
            validated = RUNNER.validate_processor_bundle(checkpoint, bundle, {}, helper)
            self.assertEqual(validated["status"], "ready")
            self.assertEqual(validated["registry_sha256"], checkpoint["generation_inputs"]["candidate_sha256"])
            helper.validate_composition.assert_called_once()

    def test_resealed_checkpoint_mutations_outside_delivery_fields_are_rejected(self) -> None:
        mutations = (
            ("observed_at", lambda checkpoint: checkpoint.update(observed_at="2026-10-02T18:00:00Z")),
            ("last_observation", lambda checkpoint: checkpoint["last_observation"].update(observed_at="2026-10-02T18:00:00Z")),
            ("input_artifacts", lambda checkpoint: checkpoint["input_artifacts"][0].update(evidence_sha256="8" * 64)),
            ("outcome", lambda checkpoint: checkpoint["outcome"].update(detail_retry_count=1)),
        )
        for label, mutate in mutations:
            with self.subTest(field=label), tempfile.TemporaryDirectory() as raw:
                root = pathlib.Path(raw)
                bundle, checkpoint, _uploaded = self.ready_bundle(root)
                mutate(checkpoint)
                self.seal(checkpoint)
                RUNNER.verify_processor_checkpoint(checkpoint, self.schema(root))
                with self.assertRaisesRegex(RUNNER.PromotionError, "immutable durable generation state"):
                    RUNNER.validate_processor_bundle(checkpoint, bundle, {}, mock.Mock())

    def test_resealed_uploaded_copy_cannot_rebind_observation_or_input_artifacts(self) -> None:
        for field in ("last_observation", "input_artifacts"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw:
                root = pathlib.Path(raw)
                bundle, checkpoint, uploaded = self.ready_bundle(root)
                if field == "last_observation":
                    uploaded[field]["observed_at"] = "2026-10-02T18:00:00Z"
                else:
                    uploaded[field][0]["evidence_sha256"] = "8" * 64
                self.seal(uploaded)
                (bundle / "upstream-catalogue-checkpoint-receipt.json").write_bytes(
                    RUNNER.canonical_json(uploaded),
                )
                with self.assertRaisesRegex(RUNNER.PromotionError, "immutable durable generation state"):
                    RUNNER.validate_processor_bundle(checkpoint, bundle, {}, mock.Mock())

    def test_uploaded_heartbeat_must_be_between_observation_and_durable_replay(self) -> None:
        for heartbeat in ("2026-10-02T16:59:59Z", "2026-10-03T00:06:00Z"):
            with self.subTest(heartbeat=heartbeat), tempfile.TemporaryDirectory() as raw:
                root = pathlib.Path(raw)
                bundle, checkpoint, uploaded = self.ready_bundle(root)
                uploaded["last_heartbeat_at"] = heartbeat
                self.seal(uploaded)
                (bundle / "upstream-catalogue-checkpoint-receipt.json").write_bytes(
                    RUNNER.canonical_json(uploaded),
                )
                with self.assertRaisesRegex(RUNNER.PromotionError, "heartbeat is outside"):
                    RUNNER.validate_processor_bundle(checkpoint, bundle, {}, mock.Mock())

    def test_composition_receipt_requires_exact_baseline_and_candidate_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            candidate_bytes = b'{"candidate":true}\n'
            candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
            checkpoint = self.checkpoint(candidate_sha256=candidate_sha)
            baseline_sha = checkpoint["generation_inputs"]["baseline_sha256"]
            unrelated_digest = hashlib.sha256(b"elsewhere in the receipt").hexdigest()
            receipt_digests = {
                "baseline": {"bytes": 1, "sha256": unrelated_digest},
                "candidate": {"bytes": 1, "sha256": unrelated_digest},
                "full_diff": {"bytes": 1, "sha256": baseline_sha},
                "refresh_evidence": {"bytes": 1, "sha256": candidate_sha},
            }
            bundle, checkpoint, _uploaded = self.ready_bundle(root, input_digests=receipt_digests)
            with self.assertRaisesRegex(RUNNER.PromotionError, "exact baseline digest"):
                RUNNER.validate_processor_bundle(checkpoint, bundle, {}, mock.Mock())

    def test_schedule_recovers_the_checkpoint_locator_after_an_idle_replay(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            checkpoint = self.checkpoint()
            state_root = self.ready_state(root, checkpoint)
            selected = RUNNER.select_recoverable_processor_checkpoint(
                state_root, self.schema(root), None,
                now=RUNNER.parse_utc_timestamp("2026-10-03T00:00:00Z", "test"),
            )
            self.assertEqual(selected["generation_id"], checkpoint["generation_id"])
            self.assertEqual(selected["output_artifact"]["run_id"], "70000000001")
            self.assertEqual(selected["output_artifact"]["name"], "upstream-catalogue-processing-70000000001-2")

    def test_delivered_generation_is_not_selected_again(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            checkpoint = self.checkpoint()
            state_root = self.ready_state(root, checkpoint)
            journal = {"records": [{
                "candidate": {"source_id": "data_go_kr", "scope": "aggregate_supported_catalog", "generation_id": checkpoint["generation_id"]},
                "pr": {"number": 17},
            }]}
            selected = RUNNER.select_recoverable_processor_checkpoint(
                state_root, self.schema(root), journal,
                now=RUNNER.parse_utc_timestamp("2026-10-03T00:00:00Z", "test"),
            )
            self.assertIsNone(selected)

    def test_ready_checkpoint_with_expired_artifact_is_not_selected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            checkpoint = self.checkpoint(expires_at="2026-10-02T00:00:00Z")
            state_root = self.ready_state(root, checkpoint)
            selected = RUNNER.select_recoverable_processor_checkpoint(
                state_root, self.schema(root), None,
                now=RUNNER.parse_utc_timestamp("2026-10-03T00:00:00Z", "test"),
            )
            self.assertIsNone(selected)

    def test_all_delivered_and_expired_ready_records_report_idle_with_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            delivered = self.checkpoint(candidate_sha256="e" * 64)
            expired = self.checkpoint(candidate_sha256="d" * 64, expires_at="2026-10-02T00:00:00Z")
            state_root = self.ready_state(root, delivered, expired)
            journal = {"records": [{
                "candidate": {"source_id": "data_go_kr", "scope": "aggregate_supported_catalog", "generation_id": delivered["generation_id"]},
                "pr": {"number": 17},
            }]}
            candidates, blocked = RUNNER.list_recoverable_processor_checkpoints(
                state_root, self.schema(root), journal,
                now=RUNNER.parse_utc_timestamp("2026-10-03T00:00:00Z", "test"),
            )
            self.assertEqual(candidates, [])
            self.assertEqual(blocked, [{"generation_id": expired["generation_id"], "reason": "ready_artifact_expired"}])

    def test_deleted_old_run_does_not_starve_a_newer_ready_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            old = self.checkpoint(candidate_sha256="e" * 64, run_id="70000000001", artifact_id="111111", observed_at="2026-10-02T16:00:00Z")
            new = self.checkpoint(candidate_sha256="d" * 64, run_id="70000000002", artifact_id="222222", observed_at="2026-10-02T17:00:00Z")

            def run_then_missing(_root, _repository, run_id, _attempt):
                if run_id == old["output_artifact"]["run_id"]:
                    raise RUNNER.ProcessorCandidateError("not found")
                return self.trusted_run(new)

            artifact_api = lambda _root, _repo, _run, _artifact: self.artifact(new)
            with (
                mock.patch.object(RUNNER, "processor_run_api", side_effect=run_then_missing),
                mock.patch.object(RUNNER, "processor_artifact_api", side_effect=artifact_api),
                mock.patch.object(RUNNER, "download_processor_artifact", return_value=root / "downloaded-bundle"),
                mock.patch.object(RUNNER, "validate_processor_bundle", return_value={"composition_receipt": {"input_digests": {}}}),
                mock.patch.object(RUNNER, "verify_processor_input_compatibility", return_value=None),
            ):
                screened, blocked = RUNNER.select_first_eligible_processor_bundle(
                    root, self.repository, [old, new], [],
                    default_branch=self.default_branch,
                    current_head_sha=self.source_sha,
                    composition_schema={},
                    composition_helper=object(),
                )
            self.assertEqual(screened["generation_id"], new["generation_id"])
            self.assertEqual(blocked, [{"generation_id": old["generation_id"], "reason": "processor_run_unavailable"}])

    def test_changed_old_policy_does_not_starve_newer_compatible_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            old = self.checkpoint(candidate_sha256="e" * 64, run_id="70000000001", artifact_id="111111", observed_at="2026-10-02T16:00:00Z")
            new = self.checkpoint(candidate_sha256="d" * 64, run_id="70000000002", artifact_id="222222", observed_at="2026-10-02T17:00:00Z")
            calls = []

            def compatibility(_root, checkpoint, *_args, **_kwargs):
                calls.append(checkpoint["generation_id"])
                if checkpoint["generation_id"] == old["generation_id"]:
                    raise RUNNER.PromotionError("source policy changed")

            selected, blocked = self.screen_candidates(root, [old, new], compatibility)
            self.assertEqual(selected["generation_id"], new["generation_id"])
            self.assertEqual(calls, [old["generation_id"], new["generation_id"]])
            self.assertEqual(blocked, [{"generation_id": old["generation_id"], "reason": "processor_bundle_or_input_contract_incompatible"}])

    def test_all_unusable_ready_generations_return_visible_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            old = self.checkpoint(candidate_sha256="e" * 64, run_id="70000000001", artifact_id="111111")
            new = self.checkpoint(candidate_sha256="d" * 64, run_id="70000000002", artifact_id="222222")
            selected, blocked = self.screen_candidates(
                root, [old, new], lambda *_args, **_kwargs: (_ for _ in ()).throw(RUNNER.PromotionError("incompatible")),
            )
            self.assertIsNone(selected)
            self.assertEqual([row["generation_id"] for row in blocked], [old["generation_id"], new["generation_id"]])
            self.assertTrue(all(row["reason"] == "processor_bundle_or_input_contract_incompatible" for row in blocked))

    def test_trusted_processor_run_binds_exact_workflow_attempt_and_head(self) -> None:
        run = {
            "id": 70000000001,
            "run_attempt": 2,
            "name": RUNNER.PROCESSOR_WORKFLOW_NAME,
            "path": RUNNER.PROCESSOR_WORKFLOW_PATH + "@main",
            "repository": {"full_name": self.repository},
            "head_repository": {"full_name": self.repository},
            "head_branch": self.default_branch,
            "head_sha": self.source_sha,
            "event": "workflow_run",
            "status": "completed",
            "conclusion": "success",
        }
        accepted = RUNNER.validate_trusted_processor_run(
            run, repository=self.repository, run_id="70000000001", attempt="2",
            default_branch=self.default_branch, expected_head_sha=self.source_sha,
        )
        self.assertEqual(accepted["head_sha"], self.source_sha)
        for field, value in (
            ("path", ".github/workflows/other.yml@main"),
            ("repository", {"full_name": "Attacker/other"}),
            ("event", "pull_request"),
            ("head_sha", "d" * 40),
            ("run_attempt", 1),
            ("head_branch", "feature"),
            ("conclusion", "failure"),
        ):
            changed = dict(run)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(RUNNER.PromotionError):
                RUNNER.validate_trusted_processor_run(
                    changed, repository=self.repository, run_id="70000000001", attempt="2",
                    default_branch=self.default_branch, expected_head_sha=self.source_sha,
                )

    def test_artifact_metadata_must_match_id_attempt_run_and_expiry(self) -> None:
        checkpoint = self.checkpoint()
        run = {"head_sha": self.source_sha, "head_branch": self.default_branch}
        artifact = {
            "id": 123456,
            "name": "upstream-catalogue-processing-70000000001-2",
            "expired": False,
            "expires_at": self.expiry,
            "size_in_bytes": 37,
            "workflow_run": {"id": 70000000001, "head_sha": self.source_sha, "head_branch": self.default_branch},
        }
        valid = RUNNER.validate_processor_artifact_metadata(
            artifact, checkpoint, run, repository=self.repository,
            now=RUNNER.parse_utc_timestamp("2026-10-03T00:00:00Z", "test"),
        )
        self.assertEqual(valid["id"], 123456)
        for field, value in (
            ("id", 123457),
            ("name", "upstream-catalogue-processing-70000000001-1"),
            ("expired", True),
            ("expires_at", "2026-10-30T00:00:00Z"),
        ):
            changed = dict(artifact)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(RUNNER.PromotionError):
                RUNNER.validate_processor_artifact_metadata(
                    changed, checkpoint, run, repository=self.repository,
                    now=RUNNER.parse_utc_timestamp("2026-10-03T00:00:00Z", "test"),
                )

    def test_download_requires_exact_archive_size_and_file_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            archive_path = root / "fixture.zip"
            names = set(RUNNER.REQUIRED_PROCESSOR_FILES) | {"upstream-catalogue-checkpoint-receipt.json"}
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name in names:
                    archive.writestr(name, b"{}")
            payload = archive_path.read_bytes()
            metadata = {"id": 123456, "size_in_bytes": len(payload), "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}
            with mock.patch.object(RUNNER, "gh_rest_bytes", return_value=payload):
                output = RUNNER.download_processor_artifact(root, self.repository, metadata, root / ".datapan/bundle")
            self.assertEqual(set(path.name for path in output.iterdir()), names)
            with mock.patch.object(RUNNER, "gh_rest_bytes", return_value=payload):
                with self.assertRaisesRegex(RUNNER.PromotionError, "byte count"):
                    RUNNER.download_processor_artifact(
                        root, self.repository, {**metadata, "size_in_bytes": len(payload) + 1}, root / ".datapan/bad-bundle",
                    )

    def test_current_main_may_advance_only_if_b_input_contract_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            subprocess.run(("git", "config", "user.name", "Test"), cwd=root, check=True)
            subprocess.run(("git", "config", "user.email", "test@example.invalid"), cwd=root, check=True)
            generation_inputs = {key: None for key in RUNNER.PROCESSOR_INPUT_PROVENANCE}
            for source_path in RUNNER.PROCESSOR_COMPATIBILITY_FILES:
                path = root / source_path
                path.parent.mkdir(parents=True, exist_ok=True)
                content = ("initial " + source_path + "\n").encode("utf-8")
                path.write_bytes(content)
                for field, input_path in RUNNER.PROCESSOR_INPUT_PROVENANCE.items():
                    if input_path == source_path:
                        generation_inputs[field] = hashlib.sha256(content).hexdigest()
            subprocess.run(("git", "add", "."), cwd=root, check=True)
            subprocess.run(("git", "commit", "-qm", "processor source"), cwd=root, check=True)
            processor_head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            receipt_digests = {
                name: {
                    "bytes": (root / source_path).stat().st_size,
                    "sha256": RUNNER.file_sha256(root / source_path),
                }
                for name, source_path in RUNNER.PROCESSOR_COMPOSITION_INPUTS.items()
            }
            (root / "manifest.json").write_text('{"unrelated-main-metadata":true}\n', encoding="utf-8")
            subprocess.run(("git", "add", "manifest.json"), cwd=root, check=True)
            subprocess.run(("git", "commit", "-qm", "unrelated metadata"), cwd=root, check=True)
            current_head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            checkpoint = {"generation_inputs": generation_inputs}
            composition = {"input_digests": receipt_digests}
            RUNNER.verify_processor_input_compatibility(root, checkpoint, processor_head, current_head, composition)

            tampered_composition = {"input_digests": {**receipt_digests, "registry_schema": {"bytes": 1, "sha256": "0" * 64}}}
            with self.assertRaisesRegex(RUNNER.PromotionError, "registry_schema digest"):
                RUNNER.verify_processor_input_compatibility(root, checkpoint, processor_head, current_head, tampered_composition)

            changed = root / "policy/source-refresh.json"
            changed.write_text("changed policy\n", encoding="utf-8")
            subprocess.run(("git", "add", str(changed)), cwd=root, check=True)
            subprocess.run(("git", "commit", "-qm", "change source policy"), cwd=root, check=True)
            latest = subprocess.run(("git", "rev-parse", "HEAD"), cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            with self.assertRaisesRegex(RUNNER.PromotionError, "contract changed"):
                RUNNER.verify_processor_input_compatibility(root, checkpoint, processor_head, latest, composition)

if __name__ == "__main__":
    unittest.main()
