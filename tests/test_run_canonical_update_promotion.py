from __future__ import annotations

import contextlib
import importlib.util
import hashlib
import copy
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/run-canonical-update-promotion.py"
SPEC = importlib.util.spec_from_file_location("run_canonical_update_promotion_test_module", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)
PR_SPEC = importlib.util.spec_from_file_location(
    "canonical_update_pr_refresh_test_module",
    pathlib.Path(__file__).parents[1] / "scripts/canonical_update_pr.py",
)
assert PR_SPEC and PR_SPEC.loader
PR_HELPER = importlib.util.module_from_spec(PR_SPEC)
PR_SPEC.loader.exec_module(PR_HELPER)


class DynamicHelperLoadingTests(unittest.TestCase):
    def test_production_loader_imports_dataclass_helper_in_fresh_interpreter(self) -> None:
        helper_path = SCRIPT.parents[1] / "scripts/refresh-canonical-snapshot-evidence.py"
        code = """
import pathlib
import sys
runner_spec = __import__('importlib.util', fromlist=['spec_from_file_location']).spec_from_file_location('promotion_runner', sys.argv[1])
runner = __import__('importlib.util', fromlist=['module_from_spec']).module_from_spec(runner_spec)
sys.modules[runner_spec.name] = runner
runner_spec.loader.exec_module(runner)
helper = runner.load_module(pathlib.Path(sys.argv[2]), 'fresh_refresh_canonical_snapshot_evidence')
assert sys.modules[helper.__name__] is helper
command = helper.SourceCommand(('python3', 'scripts/validate-diagnostic-publication.py'), pathlib.Path('.'))
assert command.label == 'python3 scripts/validate-diagnostic-publication.py'
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(SCRIPT), str(helper_path)],
            text=True,
            capture_output=True,
            check=False,
            env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_dynamic_import_restores_previous_module_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = pathlib.Path(raw) / "broken_helper.py"
            name = "broken_helper_rollback_test"
            previous = types.SimpleNamespace(marker="previous")
            path.write_text(
                "import sys\n"
                "assert sys.modules[__name__] is not None\n"
                "raise RuntimeError('intentional import failure')\n",
                encoding="utf-8",
            )
            sys.modules[name] = previous
            try:
                with self.assertRaisesRegex(RuntimeError, "intentional import failure"):
                    RUNNER.load_module(path, name)
                self.assertIs(sys.modules[name], previous)
            finally:
                sys.modules.pop(name, None)

    def test_failed_dynamic_import_removes_new_module_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = pathlib.Path(raw) / "broken_helper.py"
            name = "broken_helper_new_binding_test"
            path.write_text("raise RuntimeError('intentional import failure')\n", encoding="utf-8")
            sys.modules.pop(name, None)
            with self.assertRaisesRegex(RuntimeError, "intentional import failure"):
                RUNNER.load_module(path, name)
            self.assertNotIn(name, sys.modules)


class GiraFinishReviewPolicyTests(unittest.TestCase):
    def test_explicit_none_and_required_values_are_configured(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            (root / ".gira").mkdir()
            config = root / ".gira/config.yaml"
            for value in ("none", "required", "  NONE  ", " Required "):
                config.write_text(f"finish_review_policy: {value!r}\n", encoding="utf-8")
                self.assertEqual(RUNNER.finish_review_policy_status(root), "configured", value)

    def test_missing_nested_duplicate_malformed_and_unsupported_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            (root / ".gira").mkdir()
            config = root / ".gira/config.yaml"
            for contents in (
                "profiles:\n  default:\n    finish_review_policy: none\n",
                "finish_review_policy: none\nfinish_review_policy: required\n",
                "finish_review_policy: configured\n",
                "finish_review_policy: 0\n",
                "finish_review_policy: [none\n",
            ):
                config.write_text(contents, encoding="utf-8")
                self.assertEqual(RUNNER.finish_review_policy_status(root), "unconfigured", contents)

    def test_repository_none_policy_does_not_add_blocker_on_pr_readback(self) -> None:
        repository_root = SCRIPT.parents[1]
        policy_status = RUNNER.finish_review_policy_status(repository_root)
        self.assertEqual(policy_status, "configured")

        candidate = {
            "repository": "StatPan/datapan-registry",
            "source_id": "data_go_kr",
            "scope": "aggregate_supported_catalog",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "manifest_sha256": "c" * 64,
            "registry_path": "data/data-go-kr.registry.json",
            "registry_bytes": 123,
            "registry_sha256": "d" * 64,
            "composition_receipt_sha256": "e" * 64,
            "generation_id": "finish-policy-none-test",
        }
        owner = PR_HELPER.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"])
        body = PR_HELPER.render_pr_body(candidate, owner, 652)
        receipt = {
            "status": "prepared",
            "candidate": candidate,
            "checks": {"finish_review_policy": policy_status},
            "ownership": {
                "owner_id": owner,
                "branch": PR_HELPER.automation_branch(candidate, "create"),
                "expected_head_sha": candidate["head_sha"],
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "body": body,
            },
            "pr": {"number": 0, "url": "", "state": "missing", "merge_commit_sha": None},
            "acknowledgements": [],
            "blockers": [],
        }

        observed = PR_HELPER.record_pr_readback(
            receipt,
            {
                "number": 77,
                "url": "https://github.com/StatPan/datapan-registry/pull/77",
                "body": body,
                "headRefName": receipt["ownership"]["branch"],
                "headRefOid": candidate["head_sha"],
                "baseRefName": "main",
                "state": "MERGED",
                "mergeCommit": {"oid": "f" * 40},
            },
            observed_at="2026-10-03T00:00:00Z",
            run_url="https://github.com/StatPan/datapan-registry/actions/runs/123/attempts/1",
        )

        self.assertEqual(observed["status"], "merged")
        self.assertEqual(observed["checks"]["finish_review_policy"], "configured")
        self.assertNotIn("finish_review_policy_unconfigured", observed["blockers"])


class CandidateIssueReuseTests(unittest.TestCase):
    repository = "StatPan/datapan-registry"
    candidate = {"source_id": "data_go_kr", "scope": "aggregate_supported_catalog", "generation_id": "generation-1"}

    def issue(self, number: int, state: str, owner: str, generation: str = "generation-1") -> dict:
        marker = RUNNER.issue_marker(owner, generation)
        return {"number": number, "state": state, "body": f"{marker}\n\nOwned issue.\n", "url": f"https://github.com/StatPan/datapan-registry/issues/{number}"}

    def test_closed_history_and_one_open_issue_reuse_default_and_durable_paths(self) -> None:
        owner = PR_HELPER.owner_id(self.repository, self.candidate["source_id"], self.candidate["scope"])
        closed = self.issue(101, "CLOSED", owner)
        opened = self.issue(102, "OPEN", owner)
        with mock.patch.object(RUNNER, "gh_json", return_value=[closed, opened]):
            self.assertEqual(RUNNER.ensure_candidate_issue(pathlib.Path("."), self.repository, self.candidate, owner), (102, opened["url"]))
        with mock.patch.object(RUNNER, "gh_json", side_effect=[[closed, opened], opened]):
            self.assertEqual(RUNNER.ensure_candidate_issue(pathlib.Path("."), self.repository, self.candidate, owner, 102), (102, opened["url"]))

    def test_many_closed_issues_and_one_open_issue_do_not_block_reuse(self) -> None:
        owner = PR_HELPER.owner_id(self.repository, self.candidate["source_id"], self.candidate["scope"])
        rows = [self.issue(number, "CLOSED", owner) for number in (101, 99, 97)] + [self.issue(102, "OPEN", owner)]
        with mock.patch.object(RUNNER, "gh_json", return_value=rows):
            number, _ = RUNNER.ensure_candidate_issue(pathlib.Path("."), self.repository, self.candidate, owner)
        self.assertEqual(number, 102)

    def test_lost_create_response_recovers_new_open_issue_around_closed_history(self) -> None:
        owner = PR_HELPER.owner_id(self.repository, self.candidate["source_id"], self.candidate["scope"])
        closed = self.issue(101, "CLOSED", owner)
        opened = self.issue(102, "OPEN", owner)
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            with mock.patch.object(RUNNER, "gh_json", side_effect=[[closed], [closed, opened]]), \
                 mock.patch.object(RUNNER, "command", return_value=subprocess.CompletedProcess([], 0, "", "")):
                self.assertEqual(RUNNER.ensure_candidate_issue(root, self.repository, self.candidate, owner), (102, opened["url"]))

    def test_two_open_issues_and_explicit_closed_or_foreign_issue_fail_closed(self) -> None:
        owner = PR_HELPER.owner_id(self.repository, self.candidate["source_id"], self.candidate["scope"])
        opened_a, opened_b = self.issue(102, "OPEN", owner), self.issue(103, "OPEN", owner)
        with mock.patch.object(RUNNER, "gh_json", return_value=[opened_a, opened_b]):
            with self.assertRaisesRegex(RUNNER.PromotionError, "duplicate_candidate_issues"):
                RUNNER.ensure_candidate_issue(pathlib.Path("."), self.repository, self.candidate, owner)

        closed = self.issue(101, "CLOSED", owner)
        with mock.patch.object(RUNNER, "gh_json", side_effect=[[closed], closed]):
            with self.assertRaisesRegex(RUNNER.PromotionError, "closed or no longer owned"):
                RUNNER.ensure_candidate_issue(pathlib.Path("."), self.repository, self.candidate, owner, 101)

        foreign = {"number": 104, "state": "OPEN", "body": "human issue body", "url": "https://github.com/StatPan/datapan-registry/issues/104"}
        with mock.patch.object(RUNNER, "gh_json", side_effect=[[closed], foreign]):
            with self.assertRaisesRegex(RUNNER.PromotionError, "closed or no longer owned"):
                RUNNER.ensure_candidate_issue(pathlib.Path("."), self.repository, self.candidate, owner, 104)


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

    def ready_bundle_with_worker_pending_composition(
        self,
        root: pathlib.Path,
        *,
        retain_worker_pending: int = 1,
    ) -> tuple[pathlib.Path, dict, dict]:
        bundle, checkpoint, _uploaded = self.ready_bundle(root)
        candidate_path = bundle / "composed-candidate.registry.json"
        candidate_sha = RUNNER.file_sha256(candidate_path)
        applied = [{"provider": "data.go.kr", "id": "applied-1"}]
        pending = [
            {"provider": "data.go.kr", "id": "pending-deletion-1"},
            {"provider": "data.go.kr", "id": "pending-worker-1"},
        ]
        quarantined = [{"provider": "data.go.kr", "id": "quarantined-1"}]
        (bundle / "semantic-diff.json").write_bytes(RUNNER.canonical_json({
            "applied_api_keys": applied,
            "retained_pending_api_keys": pending,
            "quarantined_api_keys": quarantined,
            "api_decisions": [{"api_key": str(index)} for index in range(4)],
        }))
        output_names = (
            "composed-candidate.registry.json", "ready-scope.registry.json",
            "semantic-diff.json", "regeneration-queue.json", "quarantine.json",
        )
        outputs = {}
        for name in output_names:
            path = bundle / name
            payload = path.read_bytes()
            outputs[name] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

        generation_inputs = checkpoint["generation_inputs"]
        input_digests = {
            "baseline": {"bytes": 24, "sha256": generation_inputs["baseline_sha256"]},
            "candidate": {"bytes": candidate_path.stat().st_size, "sha256": candidate_sha},
        }
        for name in (
            "full_diff", "refresh_evidence", "provider_index", "source_policy",
            "registry_schema", "provider_index_schema", "diff_schema",
            "refresh_evidence_schema", "enrichment_evidence_schema", "composer",
            "receipt_schema",
        ):
            input_digests[name] = {"bytes": 1, "sha256": hashlib.sha256(name.encode()).hexdigest()}
        accept_new_count = 2 if retain_worker_pending == 0 else 1
        composition = {
            "schema_version": "datapan.catalogue-composition-receipt.v1",
            "producer": {
                "repository": self.repository,
                "run_id": "70000000001",
                "run_url": "https://github.com/StatPan/datapan-registry/actions/runs/70000000001",
            },
            "input_digests": input_digests,
            "status": "ready_scoped",
            "scope": {
                "full_scope_fresh": False,
                "publication_allowed": False,
                "global_counts": {
                    "api_records": {"baseline_candidate_union": 4},
                    "dispositions": {
                        "accept_new": accept_new_count,
                        "retain_deletion_pending": 1,
                        "retain_worker_pending": retain_worker_pending,
                        "quarantine": 1,
                    },
                },
                "applied_api_keys": applied,
                "retained_pending_api_keys": pending,
                "quarantined_api_keys": quarantined,
            },
            "outputs": outputs,
        }
        (bundle / "composition-receipt.json").write_bytes(RUNNER.canonical_json(composition))

        digests = []
        for name in RUNNER.REQUIRED_PROCESSOR_FILES:
            path = bundle / name
            digests.append({"path": name, "sha256": RUNNER.file_sha256(path), "bytes": path.stat().st_size})
        checkpoint["output_digests"] = digests
        checkpoint["output_artifact"]["bundle_manifest_sha256"] = hashlib.sha256(
            RUNNER.canonical_json(digests),
        ).hexdigest()
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

    def screen_candidates(
        self, root: pathlib.Path, checkpoints: list[dict], compatibility_side_effect,
        *, bundle_sha_by_artifact_id: dict[str, str] | None = None,
        journal: dict | None = None,
    ) -> tuple[dict | None, list[dict[str, str]]]:
        run_by_id = {cp["output_artifact"]["run_id"]: cp for cp in checkpoints}
        artifact_by_id = {cp["output_artifact"]["artifact_id"]: cp for cp in checkpoints}

        def run_api(_root, _repository, run_id, _attempt):
            return self.trusted_run(run_by_id[run_id])

        def artifact_api(_root, _repository, _run_id, artifact_id):
            return self.artifact(artifact_by_id[artifact_id])

        def validate_bundle(checkpoint, *_args):
            locator = checkpoint["output_artifact"]
            digest = (bundle_sha_by_artifact_id or {}).get(
                locator["artifact_id"],
                checkpoint["generation_inputs"]["candidate_sha256"],
            )
            return {"composition_receipt": {"input_digests": {}}, "registry_sha256": digest}

        with (
            mock.patch.object(RUNNER, "processor_run_api", side_effect=run_api),
            mock.patch.object(RUNNER, "processor_artifact_api", side_effect=artifact_api),
            mock.patch.object(RUNNER, "download_processor_artifact", return_value=root / "downloaded-bundle"),
            mock.patch.object(RUNNER, "validate_processor_bundle", side_effect=validate_bundle),
            mock.patch.object(RUNNER, "verify_processor_input_compatibility", side_effect=compatibility_side_effect),
        ):
            return RUNNER.select_first_eligible_processor_bundle(
                root, self.repository, checkpoints, [], journal,
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

    def test_recovery_entrypoint_uses_real_validator_for_worker_pending_receipt(self) -> None:
        repository_root = pathlib.Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as raw:
            temp_root = pathlib.Path(raw)
            bundle, checkpoint, _uploaded = self.ready_bundle_with_worker_pending_composition(temp_root)
            validation_results = []

            def select_candidate(_root, repository, candidates, blocked, **kwargs):
                self.assertEqual(repository, self.repository)
                composition_helper = kwargs["composition_helper"]
                self.assertTrue(callable(composition_helper.validate_composition))
                validation_results.append(RUNNER.validate_processor_bundle(
                    candidates[0], bundle, kwargs["composition_schema"], composition_helper,
                ))
                locator = checkpoint["output_artifact"]
                return ({
                    "bundle_dir": bundle,
                    "run_id": locator["run_id"],
                    "attempt": 2,
                    "run": {"head_sha": self.source_sha},
                    "artifact_id": locator["artifact_id"],
                }, blocked)

            args = types.SimpleNamespace(
                state_root=temp_root / "state",
                datapan_cli="datapan",
                event_run_id=None,
            )
            with (
                mock.patch.dict(RUNNER.os.environ, {
                    "GITHUB_REPOSITORY": self.repository,
                    "GITHUB_DEFAULT_BRANCH": self.default_branch,
                }, clear=False),
                mock.patch.object(RUNNER, "load_promotion_journal", return_value=None),
                mock.patch.object(RUNNER, "list_recoverable_processor_checkpoints", return_value=([checkpoint], [])),
                mock.patch.object(RUNNER, "select_first_eligible_processor_bundle", side_effect=select_candidate),
                mock.patch.object(RUNNER, "execute_candidate_preparation") as prepare,
            ):
                RUNNER.recover_ready_processor_candidate(args, repository_root)

            self.assertEqual(len(validation_results), 1)
            self.assertEqual(validation_results[0]["registry_sha256"], checkpoint["generation_inputs"]["candidate_sha256"])
            prepare.assert_called_once_with(args, repository_root)
            self.assertEqual(args.bundle_dir, bundle)
            self.assertEqual(args.processor_artifact_id, checkpoint["output_artifact"]["artifact_id"])

    def test_real_validator_rejects_worker_pending_count_mismatch(self) -> None:
        repository_root = pathlib.Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            bundle, checkpoint, _uploaded = self.ready_bundle_with_worker_pending_composition(
                root, retain_worker_pending=0,
            )
            composition_schema = RUNNER.load_object(
                repository_root / "schemas/datapan.catalogue-composition-receipt.v1.schema.json",
            )
            composition_helper = RUNNER.load_canonical_update_pr(repository_root)
            with self.assertRaisesRegex(RUNNER.PromotionError, "did not admit a valid ready_scoped") as raised:
                RUNNER.validate_processor_bundle(checkpoint, bundle, composition_schema, composition_helper)
            self.assertIsNotNone(raised.exception.__cause__)
            self.assertIn("pending/quarantine counts disagree", str(raised.exception.__cause__))

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

    def test_durable_ready_checkpoint_is_retained_until_payload_is_screened(self) -> None:
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
            self.assertEqual(selected["generation_id"], checkpoint["generation_id"])

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

    def test_durable_ready_listing_keeps_delivered_candidate_and_reports_expired_blocker(self) -> None:
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
            self.assertEqual([row["generation_id"] for row in candidates], [delivered["generation_id"]])
            self.assertEqual(blocked, [{"generation_id": expired["generation_id"], "reason": "ready_artifact_expired"}])

    def test_exact_payload_redelivery_is_idle_but_same_generation_new_payload_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            checkpoint = self.checkpoint(artifact_id="111111")
            journal = {"records": [{
                "candidate": {
                    "repository": self.repository,
                    "source_id": "data_go_kr",
                    "scope": "aggregate_supported_catalog",
                    "generation_id": checkpoint["generation_id"],
                    "registry_sha256": "d" * 64,
                },
                "status": "pending-review",
                "pr": {"number": 17},
            }]}
            same, blocked = self.screen_candidates(
                root, [checkpoint], lambda *_args, **_kwargs: None,
                bundle_sha_by_artifact_id={"111111": "d" * 64}, journal=journal,
            )
            self.assertIsNone(same)
            self.assertEqual(blocked, [])



            # Detail retry can change the composed bytes while preserving the
            # B generation identity. Selection must compare the screened C
            # payload digest, not the producer generation id alone.
            changed = copy.deepcopy(checkpoint)
            changed["output_artifact"]["run_id"] = "70000000002"
            changed["output_artifact"]["name"] = "upstream-catalogue-processing-70000000002-2"
            changed["output_artifact"]["artifact_id"] = "222222"
            selected, blocked = self.screen_candidates(
                root, [changed], lambda *_args, **_kwargs: None,
                bundle_sha_by_artifact_id={"222222": "e" * 64}, journal=journal,
            )
            self.assertEqual(selected["generation_id"], checkpoint["generation_id"])
            self.assertEqual(selected["bundle"]["registry_sha256"], "e" * 64)
            self.assertIsNone(selected["prior_revision"])
            self.assertEqual(blocked, [])

            replay_from_new_generation = self.checkpoint(
                candidate_sha256="8" * 64, run_id="70000000003", artifact_id="333333",
            )
            later_candidate = self.checkpoint(
                candidate_sha256="9" * 64, run_id="70000000004", artifact_id="444444",
            )
            screened, blocked = self.screen_candidates(
                root, [replay_from_new_generation, later_candidate],
                lambda *_args, **_kwargs: None,
                bundle_sha_by_artifact_id={"333333": "d" * 64, "444444": "1" * 64},
                journal=journal,
            )
            self.assertEqual(screened["generation_id"], later_candidate["generation_id"])
            self.assertEqual(screened["bundle"]["registry_sha256"], "1" * 64)
            self.assertEqual(blocked, [])
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

class OwnedPRRefreshRecoveryTests(unittest.TestCase):
    repository = "StatPan/datapan-registry"
    source_id = "data_go_kr"
    scope = "aggregate_supported_catalog"
    generation_id = "generation-same"
    old_head = "b" * 40
    new_head = "c" * 40
    branch = "automation/canonical-update/data-go-kr-0123456789ab"
    pr_number = 652

    def receipts(self) -> tuple[dict, dict]:
        owner = PR_HELPER.owner_id(self.repository, self.source_id, self.scope)
        old_candidate = {
            "repository": self.repository, "source_id": self.source_id,
            "scope": self.scope, "generation_id": self.generation_id,
            "head_sha": self.old_head, "manifest_sha256": "a" * 64,
            "registry_sha256": "d" * 64,
        }
        old_body = f"{PR_HELPER.body_marker(owner, self.generation_id)}\n\nOld payload.\n"
        old = {
            "schema_version": PR_HELPER.SCHEMA_VERSION,
            "status": "pending-review", "action": "create",
            "candidate": old_candidate,
            "ownership": {
                "owner_id": owner, "branch": self.branch,
                "expected_head_sha": self.old_head,
                "body_sha256": hashlib.sha256(old_body.encode()).hexdigest(),
                "body": old_body, "issue_number": 651,
            },
            "pr": {"number": self.pr_number, "state": "open"},
            "acknowledgements": [{"status": "pending-review", "observed_at": "2026-10-03T00:00:00Z"}],
        }
        new = copy.deepcopy(old)
        new["status"] = "prepared"
        new["action"] = "refresh_owned"
        new["candidate"]["head_sha"] = self.new_head
        new["candidate"]["registry_sha256"] = "e" * 64
        new_body = f"{PR_HELPER.body_marker(owner, self.generation_id)}\n\nUpdated payload.\n"
        new["ownership"]["expected_head_sha"] = self.old_head
        new["ownership"]["body_sha256"] = hashlib.sha256(new_body.encode()).hexdigest()
        new["ownership"]["body"] = new_body
        new["acknowledgements"] = []
        new["refresh_from"] = PR_HELPER.revision_reference(old)
        return old, new

    def journal(self, old: dict, new: dict) -> dict:
        schema = json.loads((pathlib.Path(__file__).parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text())
        journal = PR_HELPER.append_journal_record(
            None, old, repository=self.repository, observed_at="2026-10-03T00:00:00Z",
        )
        journal = PR_HELPER.append_journal_record(
            journal, new, repository=self.repository, observed_at="2026-10-03T00:01:00Z",
        )
        PR_HELPER.validate_journal(journal, schema)
        return journal

    def readbacks(self) -> dict[str, dict]:
        old, new = self.receipts()
        return {
            "before-push": {"head": self.old_head, "body": old["ownership"]["body"]},
            "after-push-before-body": {"head": self.new_head, "body": old["ownership"]["body"]},
            "after-body-edit": {"head": self.new_head, "body": new["ownership"]["body"]},
        }

    def github_list_row(
        self, body: str, head: str, *, number: int | None = None,
        state: str = "open", generation_id: str | None = None,
        head_ref: str | None = None,
    ) -> dict:
        owner = PR_HELPER.owner_id(self.repository, self.source_id, self.scope)
        return {
            "number": self.pr_number if number is None else number,
            "state": state, "owner_id": owner,
            "generation_id": generation_id or self.generation_id,
            "head_sha": head, "body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "head_ref": head_ref or self.branch, "base_ref": "main",
        }

    def github_api_readback(
        self, body: str, head: str, *, number: int | None = None,
        generation_id: str | None = None, head_ref: str | None = None,
    ) -> dict:
        owner = PR_HELPER.owner_id(self.repository, self.source_id, self.scope)
        return {
            "number": self.pr_number if number is None else number,
            "state": "OPEN", "body": body,
            "headRefName": head_ref or self.branch, "headRefOid": head, "baseRefName": "main",
            "repository": self.repository, "headRepository": self.repository,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "owner_id": owner, "generation_id": generation_id or self.generation_id,
        }

    def owned_receipt(
        self, *, state: str, number: int, generation_id: str,
        head: str, registry_sha: str,
    ) -> dict:
        owner = PR_HELPER.owner_id(self.repository, self.source_id, self.scope)
        candidate = {
            "repository": self.repository, "source_id": self.source_id,
            "scope": self.scope, "generation_id": generation_id,
            "head_sha": head, "manifest_sha256": "a" * 64,
            "registry_sha256": registry_sha,
        }
        branch = PR_HELPER.automation_branch(candidate, "create")
        body = f"{PR_HELPER.body_marker(owner, generation_id)}\n\nPayload {registry_sha[:8]}.\n"
        return {
            "schema_version": PR_HELPER.SCHEMA_VERSION,
            "status": "pending-review" if state == "open" else "closed",
            "action": "create", "candidate": candidate,
            "ownership": {
                "owner_id": owner, "branch": branch,
                "expected_head_sha": head,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "body": body, "issue_number": 651,
            },
            "pr": {"number": number, "state": state},
            "acknowledgements": ([{"status": "pending-review", "observed_at": "2026-10-03T00:00:00Z"}]
                                 if state == "open" else []),
        }

    def test_exact_refresh_crash_phases_recover_same_owned_pr_without_duplicate(self) -> None:
        candidate = {
            "repository": self.repository, "source_id": self.source_id,
            "scope": self.scope, "generation_id": self.generation_id,
            "head_sha": self.new_head, "manifest_sha256": "a" * 64,
            "registry_sha256": "e" * 64,
        }
        for expected_phase, snapshot in self.readbacks().items():
            with self.subTest(phase=expected_phase):
                old, target = self.receipts()
                journal = self.journal(old, target)
                listed = self.github_list_row(snapshot["body"], snapshot["head"])
                existing, issue_number = RUNNER.existing_pr_rows(
                    journal, [listed], PR_HELPER, candidate,
                )
                self.assertEqual(len(existing), 1)
                self.assertEqual(existing[0]["number"], self.pr_number)
                self.assertEqual(issue_number, 651)
                observed = self.github_api_readback(snapshot["body"], snapshot["head"])
                RUNNER.validate_existing_pr_api_readback(observed, journal, existing, PR_HELPER)

    def test_predecessor_readback_routes_to_prepared_refresh_phase(self) -> None:
        predecessor, target = self.receipts()
        journal = self.journal(predecessor, target)
        existing = [{"revision_ref": PR_HELPER.revision_reference(predecessor)}]
        observed = self.github_api_readback(
            target["ownership"]["body"], self.new_head,
        )

        with mock.patch.object(RUNNER, "refresh_pr_phase", return_value="after-body-edit") as refresh_phase:
            RUNNER.validate_existing_pr_api_readback(observed, journal, existing, PR_HELPER)

        refresh_phase.assert_called_once_with(target, predecessor, observed, PR_HELPER)

    def test_human_head_or_body_drift_fails_closed_during_refresh_recovery(self) -> None:
        old, target = self.receipts()
        journal = self.journal(old, target)
        candidate = {
            "repository": self.repository, "source_id": self.source_id,
            "scope": self.scope, "generation_id": self.generation_id,
            "head_sha": self.new_head, "manifest_sha256": "a" * 64,
            "registry_sha256": "e" * 64,
        }
        changed_snapshots = (
            {"head": "f" * 40, "body": old["ownership"]["body"]},
            {"head": self.new_head, "body": old["ownership"]["body"] + "Human edit.\n"},
        )
        for snapshot in changed_snapshots:
            with self.subTest(head=snapshot["head"], body=snapshot["body"][-12:]):
                listed = self.github_list_row(snapshot["body"], snapshot["head"])
                with self.assertRaisesRegex(RUNNER.PromotionError, "human_head_change_or_body_change"):
                    RUNNER.existing_pr_rows(journal, [listed], PR_HELPER, candidate)

    def test_interrupted_pr_creation_recovers_only_exact_prepared_head_and_body(self) -> None:
        _old, prepared = self.receipts()
        prepared["action"] = "create"
        prepared["pr"]["number"] = 0
        prepared.pop("refresh_from")
        schema = json.loads((pathlib.Path(__file__).parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text())
        journal = PR_HELPER.append_journal_record(
            None, prepared, repository=self.repository, observed_at="2026-10-03T00:00:00Z",
        )
        PR_HELPER.validate_journal(journal, schema)
        candidate = {
            "repository": self.repository, "source_id": self.source_id,
            "scope": self.scope, "generation_id": self.generation_id,
            "head_sha": self.new_head, "manifest_sha256": "a" * 64,
            "registry_sha256": "e" * 64,
        }
        listed = self.github_list_row(prepared["ownership"]["body"], self.new_head)
        existing, issue_number = RUNNER.existing_pr_rows(journal, [listed], PR_HELPER, candidate)
        self.assertEqual(len(existing), 1)
        self.assertEqual(existing[0]["number"], self.pr_number)
        self.assertEqual(issue_number, 651)
        RUNNER.validate_existing_pr_api_readback(
            self.github_api_readback(prepared["ownership"]["body"], self.new_head),
            journal, existing, PR_HELPER,
        )

        tampered = self.github_list_row(prepared["ownership"]["body"] + "human\n", self.new_head)
        with self.assertRaisesRegex(RUNNER.PromotionError, "exact prepared head/body"):
            RUNNER.existing_pr_rows(journal, [tampered], PR_HELPER, candidate)

    def test_closed_archive_routes_to_replacement_without_open_pr_readback(self) -> None:
        closed = self.owned_receipt(
            state="closed", number=650, generation_id="generation-previous",
            head="a" * 40, registry_sha="9" * 64,
        )
        candidate = {
            "repository": self.repository, "source_id": self.source_id,
            "scope": self.scope, "generation_id": self.generation_id,
            "head_sha": self.new_head, "manifest_sha256": "a" * 64,
            "registry_sha256": "e" * 64,
        }
        listed = self.github_list_row(
            closed["ownership"]["body"], closed["candidate"]["head_sha"],
            number=650, state="closed", generation_id="generation-previous",
            head_ref=closed["ownership"]["branch"],
        )
        existing, _ = RUNNER.existing_pr_rows({"records": [closed]}, [listed], PR_HELPER, candidate)

        with mock.patch.object(RUNNER, "gh_pr_readback") as readback:
            history, opened, observed, decision = RUNNER.inspect_existing_pr_route(
                pathlib.Path("."), self.repository, {"records": [closed]},
                existing, candidate, PR_HELPER,
            )

        readback.assert_not_called()
        self.assertEqual(history, existing)
        self.assertEqual(opened, [])
        self.assertIsNone(observed)
        self.assertEqual(decision["action"], "create_replacement")
        self.assertEqual(decision["supersedes_prs"], [650])
        self.assertEqual(decision["expected_head_sha"], "0" * 40)

    def test_closed_history_and_one_open_pr_route_only_open_readback_and_refresh(self) -> None:
        opened = self.owned_receipt(
            state="open", number=self.pr_number, generation_id=self.generation_id,
            head=self.old_head, registry_sha="d" * 64,
        )
        closed = self.owned_receipt(
            state="closed", number=650, generation_id="generation-previous",
            head="a" * 40, registry_sha="9" * 64,
        )
        journal = {"records": [closed, opened]}
        candidate = {
            "repository": self.repository, "source_id": self.source_id,
            "scope": self.scope, "generation_id": self.generation_id,
            "head_sha": self.new_head, "manifest_sha256": "a" * 64,
            "registry_sha256": "e" * 64,
        }
        listed_closed = self.github_list_row(
            closed["ownership"]["body"], closed["candidate"]["head_sha"],
            number=650, state="closed", generation_id="generation-previous",
            head_ref=closed["ownership"]["branch"],
        )
        listed_open = self.github_list_row(
            opened["ownership"]["body"], opened["candidate"]["head_sha"],
            number=self.pr_number, generation_id=self.generation_id,
            head_ref=opened["ownership"]["branch"],
        )
        existing, issue_number = RUNNER.existing_pr_rows(
            journal, [listed_closed, listed_open], PR_HELPER, candidate,
        )
        self.assertEqual({row["state"] for row in existing}, {"open", "closed"})
        self.assertEqual(issue_number, 651)
        api_open = self.github_api_readback(
            opened["ownership"]["body"], opened["candidate"]["head_sha"],
            number=self.pr_number, generation_id=self.generation_id,
            head_ref=opened["ownership"]["branch"],
        )

        with mock.patch.object(RUNNER, "gh_pr_readback", return_value=api_open) as readback:
            history, active, observed, decision = RUNNER.inspect_existing_pr_route(
                pathlib.Path("."), self.repository, journal, existing, candidate, PR_HELPER,
            )

        readback.assert_called_once_with(pathlib.Path("."), self.repository, self.pr_number)
        self.assertEqual(len(history), 2)
        self.assertEqual([row["number"] for row in active], [self.pr_number])
        self.assertIs(observed, api_open)
        self.assertEqual(decision["action"], "refresh_owned")
        self.assertEqual(decision["pr_number"], self.pr_number)
        self.assertEqual(decision["branch"], opened["ownership"]["branch"])

    def test_multiple_open_pr_history_still_fails_closed(self) -> None:
        first = self.owned_receipt(
            state="open", number=652, generation_id=self.generation_id,
            head=self.old_head, registry_sha="d" * 64,
        )
        second = self.owned_receipt(
            state="open", number=653, generation_id="generation-other",
            head="f" * 40, registry_sha="8" * 64,
        )
        rows = [
            self.github_list_row(
                first["ownership"]["body"], first["candidate"]["head_sha"],
                number=652, generation_id=self.generation_id,
                head_ref=first["ownership"]["branch"],
            ),
            self.github_list_row(
                second["ownership"]["body"], second["candidate"]["head_sha"],
                number=653, generation_id="generation-other",
                head_ref=second["ownership"]["branch"],
            ),
        ]
        candidate = {
            "repository": self.repository, "source_id": self.source_id,
            "scope": self.scope, "generation_id": self.generation_id,
            "head_sha": self.new_head, "manifest_sha256": "a" * 64,
            "registry_sha256": "e" * 64,
        }
        with self.assertRaisesRegex(RUNNER.PromotionError, "duplicate_open_prs"):
            RUNNER.existing_pr_rows({"records": [first, second]}, rows, PR_HELPER, candidate)


class PreparedCreateRecoveryTests(unittest.TestCase):
    fixture_path = pathlib.Path(__file__).parent / "fixtures/canonical-update-promotion/attempt-3-pr-686-recovery.json"
    controller_head = "d" * 40

    def fixture(self) -> dict:
        return json.loads(self.fixture_path.read_text(encoding="utf-8"))

    def args(self, record: dict, **overrides: object) -> dict:
        candidate = record["candidate"]
        values = {
            "source_id": candidate["source_id"],
            "scope": candidate["scope"],
            "generation_id": candidate["generation_id"],
            "registry_path": candidate["registry_path"],
            "registry_bytes": candidate["registry_bytes"],
            "registry_sha256": candidate["registry_sha256"],
            "composition_receipt_sha256": candidate["composition_receipt_sha256"],
        }
        values.update(overrides)
        return values

    def validate(self, fixture: dict, **overrides: object) -> int:
        record = fixture["state"]["records"][0]
        values = self.args(record, **overrides)
        return RUNNER.validate_prepared_create_pr_readback(
            record, fixture["open_pr_rows"], fixture["pull_request_readback"],
            fixture["remote_branch_sha"], PR_HELPER,
            repository=record["candidate"]["repository"], **values,
        )

    def test_archived_actual_attempt_three_state_and_pr_replay_exactly(self) -> None:
        fixture = self.fixture()
        schema = json.loads((SCRIPT.parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text(encoding="utf-8"))
        PR_HELPER.validate_journal(fixture["state"], schema)
        receipt = fixture["state"]["records"][0]
        pull = fixture["pull_request_readback"]

        self.assertEqual(fixture["provenance"]["state_branch_sha"], "b31d8b25450dadf39ff643748568726af4ce04bf")
        self.assertEqual(receipt["status"], "prepared")
        self.assertEqual(receipt["pr"], {"number": 0, "url": "", "state": "missing", "merge_commit_sha": None})
        self.assertEqual(receipt["candidate"]["base_sha"], "5aae73b9a99255397ee2807d58c324094757bf8c")
        self.assertEqual(receipt["candidate"]["head_sha"], "bcc306c20c424faa9e789444e5914bf1887b930d")
        self.assertEqual(receipt["ownership"]["issue_number"], 685)
        self.assertEqual(pull["number"], 686)
        self.assertEqual(pull["headRepository"], "StatPan/datapan-registry")
        self.assertEqual(fixture["remote_branch_sha"], receipt["candidate"]["head_sha"])
        self.assertEqual(self.validate(fixture), 686)

    def test_production_resolver_replays_frozen_api_reads_without_remote_writes(self) -> None:
        fixture = self.fixture()
        record = fixture["state"]["records"][0]
        root = pathlib.Path("/read-only/replay")
        materializer = object()
        with (
            mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]) as list_prs,
            mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]) as read_pr,
            mock.patch.object(PR_HELPER, "load_materializer", return_value=materializer),
            mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=fixture["remote_branch_sha"]) as read_branch,
            mock.patch.object(RUNNER, "persist_journal_record") as persist,
        ):
            number, observed = RUNNER.resolve_prepared_create_pr_readback(
                root, "StatPan/datapan-registry", record, PR_HELPER,
                source_id=record["candidate"]["source_id"],
                scope=record["candidate"]["scope"],
                generation_id=record["candidate"]["generation_id"],
                registry_path=record["candidate"]["registry_path"],
                registry_bytes=record["candidate"]["registry_bytes"],
                registry_sha256=record["candidate"]["registry_sha256"],
                composition_receipt_sha256=record["candidate"]["composition_receipt_sha256"],
            )

        self.assertEqual(number, 686)
        self.assertEqual(observed, fixture["pull_request_readback"])
        list_prs.assert_called_once_with(root, "StatPan/datapan-registry")
        read_pr.assert_called_once_with(root, "StatPan/datapan-registry", 686)
        read_branch.assert_called_once_with(materializer, root, "origin", f"refs/heads/{record['ownership']['branch']}")
        persist.assert_not_called()

    def test_immediate_nonzero_or_lost_create_response_uses_same_strict_readback(self) -> None:
        fixture = self.fixture()
        intent = fixture["state"]["records"][0]
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            body_path = root / "candidate-pr.md"
            body_path.write_text(intent["ownership"]["body"], encoding="utf-8")
            for label, command_patch in (
                ("nonzero", {"return_value": subprocess.CompletedProcess(["gh", "pr", "create"], 1, "", "response lost after server accepted")}),
                ("lost", {"side_effect": OSError("local gh response was lost")}),
            ):
                with self.subTest(result=label), \
                    mock.patch.object(RUNNER, "command", **command_patch) as create, \
                    mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]), \
                    mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]), \
                    mock.patch.object(PR_HELPER, "load_materializer", return_value=object()), \
                    mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=fixture["remote_branch_sha"]), \
                    mock.patch.object(RUNNER, "persist_journal_record") as persist:
                    receipt, number = RUNNER.create_pr_then_reconcile_prepared_create(
                        root, "StatPan/datapan-registry", intent, PR_HELPER,
                        **self.args(intent), journal_source_base_sha=self.controller_head,
                        observed_at="2026-10-03T09:00:00Z",
                        run_url="https://github.com/StatPan/datapan-registry/actions/runs/37109999999/attempts/1",
                        body_path=body_path,
                    )
                self.assertEqual(number, 686)
                self.assertEqual(receipt["status"], "pending-review")
                self.assertEqual(receipt["candidate"], intent["candidate"])
                self.assertEqual(receipt["ownership"], intent["ownership"])
                create.assert_called_once()
                self.assertIn(1, create.call_args.kwargs["allowed_returncodes"])
                persist.assert_called_once()
                self.assertEqual(persist.call_args.args[1], self.controller_head)

    def test_every_identity_and_proof_guard_fails_closed(self) -> None:
        mutations = (
            ("repository", lambda f: f["pull_request_readback"].__setitem__("repository", "Elsewhere/repo"), {}),
            ("head repository", lambda f: f["pull_request_readback"].__setitem__("headRepository", "Elsewhere/repo"), {}),
            ("base branch", lambda f: f["pull_request_readback"].__setitem__("baseRefName", "release"), {}),
            ("candidate base", lambda f: f["state"]["records"][0]["candidate"].__setitem__("base_sha", "1" * 40), {}),
            ("branch", lambda f: f["pull_request_readback"].__setitem__("headRefName", "other-branch"), {}),
            ("candidate head", lambda f: f["state"]["records"][0]["candidate"].__setitem__("head_sha", "2" * 40), {}),
            ("body bytes", lambda f: f["pull_request_readback"].__setitem__("body", f["pull_request_readback"]["body"] + "Human edit.\n"), {}),
            ("body digest", lambda f: f["state"]["records"][0]["ownership"].__setitem__("body_sha256", "3" * 64), {}),
            ("owner", lambda f: f["state"]["records"][0]["ownership"].__setitem__("owner_id", "not-the-owner"), {}),
            ("generation", lambda f: f["pull_request_readback"].__setitem__("body", f["pull_request_readback"]["body"].replace("generation=66a2", "generation=76a2")), {}),
            ("issue", lambda f: (f["state"]["records"][0]["ownership"].__setitem__("issue_number", 686), f["state"]["records"][0]["ownership"].__setitem__("issue_url", "https://github.com/StatPan/datapan-registry/issues/686")), {}),
            ("remote branch SHA", lambda f: f.__setitem__("remote_branch_sha", "4" * 40), {}),
            ("LFS proof", lambda f: f["state"]["records"][0]["candidate"]["payload_readback"].__setitem__("lfs_oid", "5" * 64), {}),
            ("source checks", lambda f: f["state"]["records"][0]["checks"].__setitem__("release", "failed"), {}),
            ("validation evidence", lambda f: f["state"]["records"][0]["validation_evidence"][0].__setitem__("exit_code", 1), {}),
            ("processor generation", lambda f: None, {"generation_id": "7" * 64}),
            ("processor payload", lambda f: None, {"registry_sha256": "8" * 64}),
            ("branch list row", lambda f: f["open_pr_rows"][0].__setitem__("head_ref", "other-branch"), {}),
            ("base branch list row", lambda f: f["open_pr_rows"][0].__setitem__("base_ref", "release"), {}),
            ("list head sha", lambda f: f["open_pr_rows"][0].__setitem__("head_sha", "9" * 40), {}),
            ("list body bytes", lambda f: f["open_pr_rows"][0].__setitem__("body", f["open_pr_rows"][0]["body"] + "Human edit.\n"), {}),
        )
        for name, mutate, overrides in mutations:
            with self.subTest(guard=name):
                fixture = self.fixture()
                mutate(fixture)
                with self.assertRaises(RUNNER.PromotionError):
                    self.validate(fixture, **overrides)

    def test_zero_closed_and_duplicate_open_readbacks_fail_closed(self) -> None:
        fixture = self.fixture()
        closed = self.fixture()
        closed["open_pr_rows"][0]["state"] = "closed"
        with self.assertRaisesRegex(RUNNER.PromotionError, "exactly one open PR"):
            self.validate(closed)

        duplicate = self.fixture()
        duplicate["open_pr_rows"].append(copy.deepcopy(duplicate["open_pr_rows"][0]))
        duplicate["open_pr_rows"][1]["number"] = 687
        with self.assertRaisesRegex(RUNNER.PromotionError, "exactly one open PR"):
            self.validate(duplicate)

        with self.assertRaisesRegex(RUNNER.PromotionError, "exactly one open PR"):
            RUNNER.validate_prepared_create_pr_readback(
                fixture["state"]["records"][0], [], fixture["pull_request_readback"],
                fixture["remote_branch_sha"], PR_HELPER,
                repository="StatPan/datapan-registry", **self.args(fixture["state"]["records"][0]),
            )

    def test_cas_conflict_is_retryable_without_recreating_or_changing_candidate(self) -> None:
        fixture = self.fixture()
        intent = fixture["state"]["records"][0]
        original_candidate = copy.deepcopy(intent["candidate"])
        root = pathlib.Path("/read-only/retry")
        with (
            mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]),
            mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]),
            mock.patch.object(PR_HELPER, "load_materializer", return_value=object()),
            mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=fixture["remote_branch_sha"]),
            mock.patch.object(RUNNER, "persist_journal_record", side_effect=[RUNNER.PromotionError("CAS conflict"), None]) as persist,
        ):
            with self.assertRaisesRegex(RUNNER.PromotionError, "CAS conflict"):
                RUNNER.reconcile_prepared_create_pr(
                    root, "StatPan/datapan-registry", intent, PR_HELPER,
                    **self.args(intent), journal_source_base_sha=self.controller_head,
                    observed_at="2026-10-03T09:10:00Z",
                    run_url="https://github.com/StatPan/datapan-registry/actions/runs/37109999999/attempts/1",
                )
            recovered, number = RUNNER.reconcile_prepared_create_pr(
                root, "StatPan/datapan-registry", intent, PR_HELPER,
                **self.args(intent), journal_source_base_sha=self.controller_head,
                observed_at="2026-10-03T09:11:00Z",
                run_url="https://github.com/StatPan/datapan-registry/actions/runs/37109999999/attempts/2",
            )

        self.assertEqual(number, 686)
        self.assertEqual(recovered["status"], "pending-review")
        self.assertEqual(recovered["candidate"], original_candidate)
        self.assertEqual(intent["candidate"], original_candidate)
        self.assertEqual(persist.call_count, 2)

    def test_early_pr_zero_path_persists_pending_review_then_ci_without_native_regeneration(self) -> None:
        fixture = self.fixture()
        intent = fixture["state"]["records"][0]
        candidate = intent["candidate"]
        controller_head = self.controller_head
        baseline_sha = "a" * 64
        checkpoint = {
            "source_id": candidate["source_id"],
            "source_scope": candidate["scope"],
            "generation_id": candidate["generation_id"],
        }
        bundle = {
            "registry_path": candidate["registry_path"],
            "registry_bytes": candidate["registry_bytes"],
            "registry_sha256": candidate["registry_sha256"],
            "composition_receipt_sha256": candidate["composition_receipt_sha256"],
            "baseline_sha256": baseline_sha,
            "status": "ready",
        }

        def fake_command(argv: tuple[str, ...], _cwd: pathlib.Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ("git", "rev-parse"):
                return subprocess.CompletedProcess(argv, 0, controller_head + "\n", "")
            if argv[:3] == ("git", "ls-remote", "--heads"):
                return subprocess.CompletedProcess(argv, 0, f"{controller_head}\trefs/heads/main\n", "")
            if any("materialize-canonical-registry.py" in str(part) for part in argv):
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[:2] == ("git", "status"):
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(f"unexpected pre-regeneration command: {argv}")

        def fake_load_object(path: pathlib.Path) -> dict:
            if path.name == "manifest.json":
                return {"artifacts": [{"path": candidate["registry_path"], "kind": "registry", "bytes": 10}]}
            return {}

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            args = types.SimpleNamespace(
                workflow_run_id="37100705274", workflow_run_attempt="1",
                bundle_dir=root / "bundle", workflow_run_head_sha="e" * 40,
                state_root=root / "processor-state", datapan_cli=root / "datapan-cli",
                processor_artifact_id="artifact-1", event_run_id=None,
            )
            sequence: list[str] = []
            with (
                mock.patch.dict(os.environ, {
                    "GITHUB_REPOSITORY": "StatPan/datapan-registry",
                    "GITHUB_RUN_ID": "37109999999", "GITHUB_RUN_ATTEMPT": "1",
                }),
                mock.patch.object(RUNNER, "validate_no_candidate_processor_result", return_value=None),
                mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=PR_HELPER),
                mock.patch.object(RUNNER, "load_object", side_effect=fake_load_object),
                mock.patch.object(RUNNER, "locate_processor_checkpoint", return_value=("run-1", checkpoint)),
                mock.patch.object(RUNNER, "validate_generation_identity"),
                mock.patch.object(RUNNER, "validate_processor_bundle", return_value=bundle),
                mock.patch.object(RUNNER, "verify_processor_input_compatibility"),
                mock.patch.object(RUNNER, "command", side_effect=fake_command),
                mock.patch.object(RUNNER, "registry_sha_from_path", return_value=(10, baseline_sha)),
                mock.patch.object(RUNNER, "load_promotion_journal", return_value=fixture["state"]),
                mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]),
                mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]),
                mock.patch.object(PR_HELPER, "load_materializer", return_value=object()),
                mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=fixture["remote_branch_sha"]),
                mock.patch.object(RUNNER, "persist_journal_record", side_effect=lambda *_a, **_kw: sequence.append("persist")),
                mock.patch.object(RUNNER, "ensure_verify_release_ci", side_effect=lambda *_a: (sequence.append("ci") or {"ci": {"state": "passed"}})),
                mock.patch.object(RUNNER, "load_module", side_effect=AssertionError("native source refresh must not run")) as load_module,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                RUNNER.execute_candidate_preparation(args, root)

        self.assertEqual(sequence, ["persist", "ci"])
        load_module.assert_not_called()
        self.assertFalse((root / candidate["registry_path"]).exists())
        self.assertFalse((root / "reports/latest-verification.json").exists())



if __name__ == "__main__":
    unittest.main()
