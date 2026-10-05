from __future__ import annotations

import contextlib
import importlib.util
import hashlib
import copy
import datetime as dt
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock

import yaml


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
CI_SPEC = importlib.util.spec_from_file_location(
    "canonical_update_ci_recovery_test_module",
    pathlib.Path(__file__).parents[1] / "scripts/canonical_update_ci.py",
)
assert CI_SPEC and CI_SPEC.loader
CI_HELPER = importlib.util.module_from_spec(CI_SPEC)
CI_SPEC.loader.exec_module(CI_HELPER)


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


class PublicationAcknowledgementWorkflowDependencyTests(unittest.TestCase):
    def test_real_receipt_path_installs_yaml_before_runner_invocation(self) -> None:
        workflow_path = SCRIPT.parents[1] / ".github/workflows/canonical-update-publication-ack.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["reconcile"]["steps"]
        install_index, install_step = next(
            (index, step) for index, step in enumerate(steps)
            if step.get("name") == "Install publication acknowledgement dependencies"
        )
        runner_index, runner_step = next(
            (index, step) for index, step in enumerate(steps)
            if "scripts/recover-canonical-publication-ack.py --repository-root ." in step.get("run", "")
        )

        self.assertEqual(
            shlex.split(install_step["run"]),
            ["python", "-m", "pip", "install", "--disable-pip-version-check", "jsonschema", "PyYAML==6.0.2"],
        )
        self.assertLess(install_index, runner_index)
        self.assertEqual(
            runner_step.get("env", {}).get("GITHUB_EVENT_NAME"),
            "${{ github.event_name }}",
        )
        self.assertEqual(workflow["on"]["schedule"], [{"cron": "57 * * * *"}])
        self.assertEqual(workflow["concurrency"]["group"], "canonical-update-publication-ack")
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"] == "true")


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
    def test_open_pr_merge_metadata_is_replaced_by_the_later_merged_witness(self) -> None:
        fixture_path = pathlib.Path(__file__).parent / "fixtures/canonical-update-promotion/attempt-4-pr-686-pending-recovery.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        journal = copy.deepcopy(fixture["state"])
        original = journal["records"][0]
        open_readback = copy.deepcopy(fixture["pull_request_readback"])
        self.assertEqual(open_readback["state"], "OPEN")
        self.assertIsNotNone(open_readback["mergeCommit"].get("oid"))

        open_observed = PR_HELPER.record_pr_readback(
            copy.deepcopy(original), open_readback,
            observed_at="2026-10-04T12:10:00Z",
            run_url="https://github.com/StatPan/datapan-registry/actions/runs/37200000001/attempts/1",
        )
        journal = PR_HELPER.append_journal_record(
            journal, open_observed, repository=original["candidate"]["repository"],
            observed_at="2026-10-04T12:10:00Z",
        )
        schema = json.loads(
            (pathlib.Path(__file__).parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text(encoding="utf-8")
        )
        PR_HELPER.validate_journal(journal, schema)
        self.assertEqual(journal["records"][0]["pr"]["merge_commit_sha"], open_readback["mergeCommit"]["oid"])

        merged_sha = "f" * 40
        merged_readback = copy.deepcopy(open_readback)
        merged_readback.update({"state": "MERGED", "mergeCommit": {"oid": merged_sha}})
        merged = PR_HELPER.record_pr_readback(
            copy.deepcopy(open_observed), merged_readback,
            observed_at="2026-10-04T12:11:00Z",
            run_url="https://github.com/StatPan/datapan-registry/actions/runs/37200000002/attempts/1",
        )
        journal = PR_HELPER.append_journal_record(
            journal, merged, repository=original["candidate"]["repository"],
            observed_at="2026-10-04T12:11:00Z",
        )
        PR_HELPER.validate_journal(journal, schema)
        stored = journal["records"][0]
        self.assertEqual(stored["status"], "merged")
        self.assertEqual(stored["pr"]["merge_commit_sha"], merged_sha)
        self.assertEqual(stored["acknowledgements"][-1]["status"], "merged")
        self.assertEqual(stored["acknowledgements"][-1]["source_sha"], merged_sha)

        tampered = copy.deepcopy(merged)
        tampered["pr"]["merge_commit_sha"] = "9" * 40
        with self.assertRaisesRegex(PR_HELPER.AdmissionError, "changed the witnessed PR merge commit"):
            PR_HELPER.append_journal_record(
                journal, tampered, repository=original["candidate"]["repository"],
                observed_at="2026-10-04T12:12:00Z",
            )

    def test_runner_persists_the_complete_verified_publication_acknowledgement_chain(self) -> None:
        fixture_path = pathlib.Path(__file__).parent / "fixtures/canonical-update-promotion/attempt-4-pr-686-pending-recovery.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        journal = copy.deepcopy(fixture["state"])
        durable = journal["records"][0]
        candidate = durable["candidate"]
        merge_sha = "f" * 40
        run_url = "https://github.com/StatPan/datapan-registry/actions/runs/37199628001/attempts/1"
        merged_pr = copy.deepcopy(fixture["pull_request_readback"])
        merged_pr.update({"state": "MERGED", "mergeCommit": {"oid": merge_sha}})
        publication_receipt = {
            "schema_version": "datapan.registry-publication-receipt.v1",
            "status": "verified",
            "source_binding": {
                "status": "bound",
                "repository": candidate["repository"],
                "source_sha": merge_sha,
                "manifest_sha256": candidate["manifest_sha256"],
            },
            "publication": {
                "status": "published",
                "dataset": candidate["repository"],
                "payload_revision": "1" * 40,
                "pointer_revision": "2" * 40,
            },
            "anonymous_verification": {
                "status": "verified",
                "dataset": candidate["repository"],
                "revision": "1" * 40,
            },
        }
        schema = json.loads(
            (pathlib.Path(__file__).parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text(encoding="utf-8")
        )
        writes: list[dict] = []
        initial_state_sha = "a" * 40
        state_cas_inputs: list[str] = []
        with tempfile.TemporaryDirectory(prefix="canonical-publication-reconcile-") as raw:
            root = pathlib.Path(raw)
            receipt_path = root / "publication-receipt.json"
            receipt_path.write_text(json.dumps(publication_receipt), encoding="utf-8")

            def persist(
                _root: pathlib.Path,
                _base_sha: str,
                receipt: dict,
                *,
                observed_at: str,
                expected_state_sha: str,
                **_kwargs: object,
            ) -> str:
                nonlocal journal
                self.assertEqual(expected_state_sha, "a" * 40 if not writes else "b" * 40)
                state_cas_inputs.append(expected_state_sha)
                journal = PR_HELPER.append_journal_record(
                    journal, receipt, repository=candidate["repository"], observed_at=observed_at,
                )
                PR_HELPER.validate_journal(journal, schema)
                writes.append(copy.deepcopy(receipt))
                return "b" * 40 if len(writes) == 1 else "c" * 40

            def command(argv: tuple[str, ...], _root: pathlib.Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertEqual(argv, ("git", "rev-parse", "HEAD"))
                return subprocess.CompletedProcess(argv, 0, "8" * 40 + "\n", "")

            with (
                mock.patch.dict(RUNNER.os.environ, {
                    "GITHUB_REPOSITORY": candidate["repository"],
                    "GITHUB_RUN_ID": "37199709258",
                    "GITHUB_RUN_ATTEMPT": "2",
                }),
                mock.patch.object(RUNNER, "load_module", return_value=PR_HELPER),
                mock.patch.object(
                    RUNNER, "load_promotion_journal_snapshot",
                    return_value=(copy.deepcopy(journal), initial_state_sha),
                ),
                mock.patch.object(RUNNER, "gh_pr_readback", return_value=merged_pr),
                mock.patch.object(RUNNER, "command", side_effect=command),
                mock.patch.object(RUNNER, "persist_journal_record", side_effect=persist),
            ):
                RUNNER.reconcile_publication(root, receipt_path)

        self.assertEqual(len(writes), 2)
        self.assertEqual(state_cas_inputs, ["a" * 40, "b" * 40])
        self.assertEqual([receipt["status"] for receipt in writes], ["merged", "read-back-confirmed"])
        stored = journal["records"][0]
        self.assertEqual(stored["status"], "read-back-confirmed")
        self.assertEqual(
            [row["status"] for row in stored["acknowledgements"]],
            ["pending-review", "merged", "publication-pending", "published", "read-back-confirmed"],
        )
        self.assertEqual(stored["acknowledgements"][0], durable["acknowledgements"][0])
        self.assertEqual(stored["pr"]["merge_commit_sha"], merge_sha)

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
            "base": {
                "sha": "a" * 40,
                "repo": {"full_name": "StatPan/datapan-registry"},
            },
            "head": {"repo": {"full_name": "StatPan/datapan-registry"}},
        }
        with (
            mock.patch.object(RUNNER, "gh_json", return_value=cli_pr),
            mock.patch.object(RUNNER, "gh_rest_json", return_value=rest_pr),
        ):
            observed = RUNNER.gh_pr_readback(pathlib.Path("."), "StatPan/datapan-registry", 652)

        self.assertEqual(observed["repository"], "StatPan/datapan-registry")
        self.assertEqual(observed["headRepository"], "StatPan/datapan-registry")
        self.assertEqual(observed["baseRefOid"], "a" * 40)

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

    def test_c_bundle_validator_binds_resolver_metadata_to_quarantine_and_registered_host(self) -> None:
        page_time = "2026-10-01T10:00:00Z"
        resolved_url = "http://data.seoul.go.kr/dataList/datasetView.do?infId=OA-109"
        metadata = {
            "method": "data_go_kr_select_api_link_url_v1",
            "dataset_id": "2",
            "public_data_pk": "2",
            "public_data_detail_pk": "uddi:fixture-2",
            "page": {
                "url": "https://www.data.go.kr/data/2/openapi.do",
                "effective_url": "https://www.data.go.kr/data/2/openapi.do",
                "sha256": "a" * 64, "bytes": 100, "observed_at": page_time,
            },
            "resolver": {
                "request_url": "https://www.data.go.kr/tcs/dss/selectApiLinkUrl.do?publicDataPk=2",
                "effective_url": "https://www.data.go.kr/tcs/dss/selectApiLinkUrl.do?publicDataPk=2",
                "sha256": "b" * 64, "bytes": 190, "observed_at": page_time,
                "public_data_detail_pk": "uddi:fixture-2",
                "resolved_url": resolved_url,
                "resolved_url_sha256": hashlib.sha256(resolved_url.encode()).hexdigest(),
            },
        }
        source_sha = "c" * 64
        guide_sha = "d" * 64
        diagnostic = {"code": "resolved_link_operation_contract_unproven", "phase": "resolver"}
        outcome = {
            "api_key": {"provider": "data.go.kr", "id": "2"}, "status": "quarantined",
            "source_sha256": source_sha, "guide_sha256": guide_sha,
            "failure_diagnostic": diagnostic, "link_metadata": metadata,
        }
        checkpoint = {"detail_records": [{
            "id": "2", "status": "quarantined", "source_sha256": source_sha,
            "guide_sha256": guide_sha, "failure_diagnostic": diagnostic, "link_metadata": metadata,
        }]}
        evidence = {
            "schema_version": "datapan.catalogue-enrichment-evidence.v1",
            "original_candidate_sha256": "e" * 64,
            "provider_index_sha256": "f" * 64,
            "adapter_revision": "f" * 64,
            "extractor_revision": "a" * 64,
            "records": [], "worker_outcomes": [outcome],
        }
        with tempfile.TemporaryDirectory() as raw:
            bundle = pathlib.Path(raw)
            evidence_path = bundle / "upstream-catalogue-enrichment-evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            RUNNER.validate_processor_link_metadata(checkpoint, bundle, root=SCRIPT.parents[1])

            invalid = copy.deepcopy(evidence)
            bad_metadata = invalid["worker_outcomes"][0]["link_metadata"]
            bad_metadata["resolver"]["resolved_url"] = "http://unregistered.example.test/guide"
            bad_metadata["resolver"]["resolved_url_sha256"] = hashlib.sha256(
                bad_metadata["resolver"]["resolved_url"].encode(),
            ).hexdigest()
            checkpoint["detail_records"][0]["link_metadata"] = bad_metadata
            evidence_path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(RUNNER.PromotionError, "link metadata provenance is invalid"):
                RUNNER.validate_processor_link_metadata(checkpoint, bundle, root=SCRIPT.parents[1])

            secret_evidence = copy.deepcopy(evidence)
            secret_metadata = secret_evidence["worker_outcomes"][0]["link_metadata"]
            secret_url = "http://data.seoul.go.kr/dataList?refresh%5Ftoken=SYNTHETIC_TEST_VALUE"
            secret_metadata["resolver"]["resolved_url"] = secret_url
            secret_metadata["resolver"]["resolved_url_sha256"] = hashlib.sha256(
                secret_url.encode("utf-8"),
            ).hexdigest()
            checkpoint["detail_records"][0]["link_metadata"] = copy.deepcopy(secret_metadata)
            evidence_path.write_text(json.dumps(secret_evidence), encoding="utf-8")
            with self.assertRaisesRegex(RUNNER.PromotionError, "link metadata provenance is invalid"):
                RUNNER.validate_processor_link_metadata(checkpoint, bundle, root=SCRIPT.parents[1])

            stripped = copy.deepcopy(evidence)
            stripped["worker_outcomes"][0].pop("link_metadata")
            stripped_checkpoint = copy.deepcopy(checkpoint)
            stripped_checkpoint["detail_records"][0].pop("link_metadata")
            evidence_path.write_text(json.dumps(stripped), encoding="utf-8")
            with self.assertRaisesRegex(RUNNER.PromotionError, "does not match the trusted schema"):
                RUNNER.validate_processor_link_metadata(stripped_checkpoint, bundle, root=SCRIPT.parents[1])

            evidence_path.unlink()
            with self.assertRaisesRegex(RUNNER.PromotionError, "missing its enrichment evidence"):
                RUNNER.validate_processor_link_metadata(checkpoint, bundle, root=SCRIPT.parents[1])


class DurableProcessorRecoveryTests(unittest.TestCase):
    repository = "StatPan/datapan-registry"
    default_branch = "main"
    source_sha = "a" * 40
    expiry = "2026-10-31T00:00:00Z"

    def generation_inputs(
        self,
        candidate_sha256: str = "d" * 64,
        baseline_sha256: str = "c" * 64,
    ) -> dict[str, object]:
        return {
            "source_id": "data_go_kr",
            "source_scope": "aggregate_supported_catalog",
            "baseline_sha256": baseline_sha256,
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
        baseline_sha256: str = "c" * 64,
        **locator_updates: object,
    ) -> dict:
        generation_inputs = self.generation_inputs(candidate_sha256, baseline_sha256)
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
        bundle_bytes_by_artifact_id: dict[str, int] | None = None,
        canonical_identity: dict[str, object] | None = None,
        already_canonical_out: list[dict[str, object]] | None = None,
        journal: dict | None = None,
    ) -> tuple[dict | None, list[dict[str, str]]]:
        run_by_id = {cp["output_artifact"]["run_id"]: cp for cp in checkpoints}
        artifact_by_id = {cp["output_artifact"]["artifact_id"]: cp for cp in checkpoints}

        def run_api(_root, _repository, run_id, _attempt):
            return self.trusted_run(run_by_id[run_id])

        def artifact_api(_root, _repository, _run_id, artifact_id):
            return self.artifact(artifact_by_id[artifact_id])

        def validate_bundle(checkpoint, *_args, **_kwargs):
            locator = checkpoint["output_artifact"]
            digest = (bundle_sha_by_artifact_id or {}).get(
                locator["artifact_id"],
                checkpoint["generation_inputs"]["candidate_sha256"],
            )
            registry_bytes = (bundle_bytes_by_artifact_id or {}).get(locator["artifact_id"], 37)
            return {
                "composition_receipt": {"input_digests": {}},
                "registry_path": "data/data-go-kr.registry.json",
                "registry_bytes": registry_bytes,
                "registry_sha256": digest,
                "baseline_sha256": checkpoint["generation_inputs"]["baseline_sha256"],
            }

        current_identity = canonical_identity or {
            "main_sha": self.source_sha,
            "registry_path": "data/data-go-kr.registry.json",
            "registry_bytes": 999,
            "registry_sha256": "c" * 64,
        }

        with (
            mock.patch.object(RUNNER, "processor_run_api", side_effect=run_api),
            mock.patch.object(RUNNER, "processor_artifact_api", side_effect=artifact_api),
            mock.patch.object(RUNNER, "download_processor_artifact", return_value=root / "downloaded-bundle"),
            mock.patch.object(RUNNER, "validate_processor_bundle", side_effect=validate_bundle),
            mock.patch.object(RUNNER, "verify_processor_input_compatibility", side_effect=compatibility_side_effect),
            mock.patch.object(RUNNER, "authenticated_current_canonical_registry", return_value=current_identity),
        ):
            selection_options = {}
            if already_canonical_out is not None:
                selection_options["already_canonical"] = already_canonical_out
            return RUNNER.select_first_eligible_processor_bundle(
                root, self.repository, checkpoints, [], journal,
                default_branch=self.default_branch,
                current_head_sha=self.source_sha,
                composition_schema={},
                composition_helper=object(),
                **selection_options,
            )

    def test_current_canonical_is_materialized_from_pinned_lfs_and_manifest_checked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            registry_path = "data/data-go-kr.registry.json"
            payload = b'[{"provider":"data.go.kr","id":"pinned"}]\n'
            digest = hashlib.sha256(payload).hexdigest()
            manifest = {
                "source_registry": registry_path,
                "artifacts": [{
                    "path": registry_path, "kind": "registry",
                    "bytes": len(payload), "sha256": digest,
                }],
            }
            (root / "manifest.json").write_bytes(RUNNER.canonical_json(manifest))
            pointer = root / registry_path
            pointer.parent.mkdir(parents=True)
            pointer.write_text(
                f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize {len(payload)}\n",
                encoding="ascii",
            )
            materialized = root / ".datapan/current-canonical" / registry_path
            command_calls: list[tuple[str, ...]] = []
            committed_manifest_bytes = [(root / "manifest.json").read_bytes()]
            committed_pointer_bytes = [pointer.read_bytes()]

            def fake_command(argv: tuple[str, ...], cwd: pathlib.Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                command_calls.append(argv)
                if argv[:3] == ("git", "rev-parse", "HEAD"):
                    return subprocess.CompletedProcess(argv, 0, f"{self.source_sha}\n", "")
                if argv[:3] == ("git", "ls-remote", "--heads"):
                    return subprocess.CompletedProcess(argv, 0, f"{self.source_sha}\trefs/heads/main\n", "")
                if argv[:2] == ("git", "show") and argv[2] == f"{self.source_sha}:manifest.json":
                    return subprocess.CompletedProcess(argv, 0, committed_manifest_bytes[0].decode("utf-8"), "")
                if argv[:2] == ("git", "show") and argv[2] == f"{self.source_sha}:{registry_path}":
                    return subprocess.CompletedProcess(argv, 0, committed_pointer_bytes[0].decode("ascii"), "")
                if "materialize-canonical-registry.py" in argv[1]:
                    self.assertIn("--backend", argv)
                    self.assertEqual(argv[argv.index("--backend") + 1], "github-git-lfs")
                    self.assertEqual(argv[argv.index("--candidate-commit") + 1], self.source_sha)
                    self.assertEqual(
                        argv[argv.index("--expected-manifest-sha256") + 1],
                        hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
                    )
                    pathlib.Path(argv[argv.index("--output") + 1]).parent.mkdir(parents=True, exist_ok=True)
                    pathlib.Path(argv[argv.index("--output") + 1]).write_bytes(payload)
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected command: {argv}")

            with mock.patch.object(RUNNER, "command", side_effect=fake_command):
                identity = RUNNER.authenticated_current_canonical_registry(root, self.source_sha)
            self.assertEqual(identity, {
                "main_sha": self.source_sha,
                "registry_path": registry_path,
                "registry_bytes": len(payload),
                "registry_sha256": digest,
            })
            self.assertEqual(materialized.read_bytes(), payload)
            self.assertEqual(command_calls.count(("git", "ls-remote", "--heads", "origin", "refs/heads/main")), 2)

            changed = copy.deepcopy(manifest)
            changed["artifacts"][0]["bytes"] = len(payload) + 1
            changed["artifacts"][0]["sha256"] = "0" * 64
            tampered_working = RUNNER.canonical_json(changed)
            (root / "manifest.json").write_bytes(tampered_working)
            with mock.patch.object(RUNNER, "command", side_effect=fake_command):
                with self.assertRaisesRegex(RUNNER.PromotionError, "working bytes do not match the pinned main commit"):
                    RUNNER.authenticated_current_canonical_registry(root, self.source_sha)

            for field, invalid in (("bytes", len(payload) + 1), ("sha256", "0" * 64)):
                with self.subTest(field=field):
                    changed = copy.deepcopy(manifest)
                    changed["artifacts"][0][field] = invalid
                    committed_manifest_bytes[0] = RUNNER.canonical_json(changed)
                    committed_pointer_bytes[0] = (
                        "version https://git-lfs.github.com/spec/v1\n"
                        f"oid sha256:{changed['artifacts'][0]['sha256']}\n"
                        f"size {changed['artifacts'][0]['bytes']}\n"
                    ).encode("ascii")
                    (root / "manifest.json").write_bytes(committed_manifest_bytes[0])
                    materialized.unlink()
                    with mock.patch.object(RUNNER, "command", side_effect=fake_command):
                        with self.assertRaisesRegex(RUNNER.PromotionError, "current canonical registry bytes do not match"):
                            RUNNER.authenticated_current_canonical_registry(root, self.source_sha)

    def test_already_canonical_older_bundle_is_skipped_for_later_distinct_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            old = self.checkpoint(
                candidate_sha256="d" * 64,
                baseline_sha256="c" * 64,
                run_id="70000000001", artifact_id="111111",
            )
            later = self.checkpoint(
                candidate_sha256="e" * 64,
                baseline_sha256="d" * 64,
                run_id="70000000002", artifact_id="222222",
            )
            noops: list[dict[str, object]] = []
            identity = {
                "main_sha": self.source_sha,
                "registry_path": "data/data-go-kr.registry.json",
                "registry_bytes": 37,
                "registry_sha256": "d" * 64,
            }
            old["outcome"].update({"pending_count": 12, "detail_retry_count": 4, "detail_unattempted_count": 4})
            screened, blocked = self.screen_candidates(
                root, [old, later], lambda *_args, **_kwargs: None,
                bundle_sha_by_artifact_id={"111111": "d" * 64, "222222": "e" * 64},
                canonical_identity=identity,
                already_canonical_out=noops,
            )
            self.assertEqual(screened["generation_id"], later["generation_id"])
            self.assertEqual(blocked, [])
            self.assertEqual(noops, [{
                "generation_id": old["generation_id"],
                "reason": "already_canonical_payload",
                "registry_sha256": "d" * 64,
                "pending_count": 12,
                "detail_retry_count": 4,
                "detail_unattempted_count": 4,
                "candidate_available": False,
            }])

    def test_same_digest_with_wrong_byte_count_is_not_already_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            candidate = self.checkpoint(candidate_sha256="d" * 64, baseline_sha256="c" * 64)
            noops: list[dict[str, object]] = []
            identity = {
                "main_sha": self.source_sha,
                "registry_path": "data/data-go-kr.registry.json",
                "registry_bytes": 37,
                "registry_sha256": "d" * 64,
            }
            screened, blocked = self.screen_candidates(
                root, [candidate], lambda *_args, **_kwargs: None,
                bundle_sha_by_artifact_id={"123456": "d" * 64},
                bundle_bytes_by_artifact_id={"123456": 36},
                canonical_identity=identity,
                already_canonical_out=noops,
            )
            self.assertIsNone(screened)
            self.assertEqual(noops, [])
            self.assertEqual(blocked, [{
                "generation_id": candidate["generation_id"],
                "reason": "processor_baseline_stale_for_current_canonical",
            }])

    def test_stale_different_payload_does_not_starve_fresh_baseline_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            old = self.checkpoint(
                candidate_sha256="e" * 64,
                baseline_sha256="c" * 64,
                run_id="70000000001", artifact_id="111111",
            )
            fresh = self.checkpoint(
                candidate_sha256="f" * 64,
                baseline_sha256="d" * 64,
                run_id="70000000002", artifact_id="222222",
            )
            identity = {
                "main_sha": self.source_sha,
                "registry_path": "data/data-go-kr.registry.json",
                "registry_bytes": 37,
                "registry_sha256": "d" * 64,
            }
            screened, blocked = self.screen_candidates(
                root, [old, fresh], lambda *_args, **_kwargs: None,
                bundle_sha_by_artifact_id={"111111": "e" * 64, "222222": "f" * 64},
                canonical_identity=identity,
            )
            self.assertEqual(screened["generation_id"], fresh["generation_id"])
            self.assertEqual(blocked, [{
                "generation_id": old["generation_id"],
                "reason": "processor_baseline_stale_for_current_canonical",
            }])

    def test_all_stale_different_payloads_remain_blocked_without_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            old = self.checkpoint(candidate_sha256="e" * 64, baseline_sha256="c" * 64)
            identity = {
                "main_sha": self.source_sha,
                "registry_path": "data/data-go-kr.registry.json",
                "registry_bytes": 37,
                "registry_sha256": "d" * 64,
            }
            screened, blocked = self.screen_candidates(
                root, [old], lambda *_args, **_kwargs: None,
                bundle_sha_by_artifact_id={"123456": "e" * 64},
                canonical_identity=identity,
            )
            self.assertIsNone(screened)
            self.assertEqual(blocked, [{
                "generation_id": old["generation_id"],
                "reason": "processor_baseline_stale_for_current_canonical",
            }])

    def test_untrusted_bundle_cannot_be_treated_as_already_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            candidate = self.checkpoint(candidate_sha256="d" * 64, baseline_sha256="c" * 64)
            noops: list[dict[str, object]] = []
            selected, blocked = self.screen_candidates(
                root, [candidate],
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RUNNER.PromotionError("untrusted input")),
                canonical_identity={
                    "main_sha": self.source_sha,
                    "registry_path": "data/data-go-kr.registry.json",
                    "registry_bytes": 37,
                    "registry_sha256": "d" * 64,
                },
                already_canonical_out=noops,
            )
            self.assertIsNone(selected)
            self.assertEqual(noops, [])
            self.assertEqual(blocked, [{
                "generation_id": candidate["generation_id"],
                "reason": "processor_bundle_or_input_contract_incompatible",
            }])

    def test_direct_different_payload_with_stale_baseline_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            bundle_dir = root / "bundle"
            bundle_dir.mkdir()
            checkpoint = self.checkpoint(candidate_sha256="e" * 64, baseline_sha256="c" * 64)
            bundle = {
                "status": "ready",
                "composition_receipt": {"input_digests": {}},
                "registry_path": "data/data-go-kr.registry.json",
                "registry_bytes": 37,
                "registry_sha256": "e" * 64,
                "baseline_sha256": "c" * 64,
            }
            args = types.SimpleNamespace(
                workflow_run_id="70000000001", workflow_run_attempt="2",
                bundle_dir=bundle_dir, workflow_run_head_sha=self.source_sha,
                state_root=root / "state", datapan_cli=root / "datapan-cli",
                processor_artifact_id="123456", source_refresh_predecessor=None,
                source_refresh_target_main_sha=None,
            )

            def fake_command(argv: tuple[str, ...], _cwd: pathlib.Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ("git", "rev-parse", "HEAD"):
                    return subprocess.CompletedProcess(argv, 0, f"{self.source_sha}\n", "")
                if "materialize-canonical-registry.py" in argv[1]:
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected command: {argv}")

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(RUNNER.os.environ, {"GITHUB_REPOSITORY": self.repository}))
                stack.enter_context(mock.patch.object(RUNNER, "validate_no_candidate_processor_result", return_value=None))
                stack.enter_context(mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=object()))
                stack.enter_context(mock.patch.object(RUNNER, "load_object", side_effect=lambda path: (
                    {"artifacts": [{"path": bundle["registry_path"], "kind": "registry", "bytes": 37}]}
                    if pathlib.Path(path).name == "manifest.json" else {}
                )))
                stack.enter_context(mock.patch.object(RUNNER, "locate_processor_checkpoint", return_value=(root / "state.json", checkpoint)))
                stack.enter_context(mock.patch.object(RUNNER, "validate_generation_identity"))
                stack.enter_context(mock.patch.object(RUNNER, "validate_processor_bundle", return_value=bundle))
                stack.enter_context(mock.patch.object(RUNNER, "verify_processor_input_compatibility"))
                stack.enter_context(mock.patch.object(RUNNER, "authenticated_current_canonical_registry", return_value={
                    "main_sha": self.source_sha,
                    "registry_path": bundle["registry_path"],
                    "registry_bytes": 37,
                    "registry_sha256": "d" * 64,
                }))
                stack.enter_context(mock.patch.object(RUNNER, "registry_sha_from_path", return_value=(37, "d" * 64)))
                materialize = stack.enter_context(mock.patch.object(RUNNER, "command", side_effect=fake_command))
                journal = stack.enter_context(mock.patch.object(RUNNER, "load_promotion_journal_snapshot"))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                with self.assertRaisesRegex(RUNNER.PromotionError, "materialized source differs from the processor's immutable baseline"):
                    RUNNER.execute_candidate_preparation(args, root)
            self.assertEqual(
                sum(any("materialize-canonical-registry.py" in part for part in call.args[0]) for call in materialize.call_args_list),
                1,
            )
            journal.assert_not_called()

    def test_direct_already_canonical_bundle_returns_without_prepare_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            bundle_dir = root / "bundle"
            bundle_dir.mkdir()
            checkpoint = self.checkpoint(candidate_sha256="d" * 64, baseline_sha256="c" * 64)
            checkpoint["outcome"].update({"pending_count": 9, "detail_retry_count": 3, "detail_unattempted_count": 6})
            bundle = {
                "status": "ready",
                "composition_receipt": {"input_digests": {}},
                "registry_path": "data/data-go-kr.registry.json",
                "registry_bytes": 37,
                "registry_sha256": "d" * 64,
                "baseline_sha256": "c" * 64,
            }
            args = types.SimpleNamespace(
                workflow_run_id="70000000001", workflow_run_attempt="2",
                bundle_dir=bundle_dir, workflow_run_head_sha=self.source_sha,
                state_root=root / "state", datapan_cli=root / "datapan-cli",
                processor_artifact_id="123456", source_refresh_predecessor=None,
                source_refresh_target_main_sha=None,
            )

            def fake_command(argv: tuple[str, ...], _cwd: pathlib.Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ("git", "rev-parse", "HEAD"):
                    return subprocess.CompletedProcess(argv, 0, f"{self.source_sha}\n", "")
                raise AssertionError(f"unexpected prepare command for already-canonical bundle: {argv}")

            output = io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(RUNNER.os.environ, {"GITHUB_REPOSITORY": self.repository}))
                stack.enter_context(mock.patch.object(RUNNER, "validate_no_candidate_processor_result", return_value=None))
                stack.enter_context(mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=object()))
                stack.enter_context(mock.patch.object(RUNNER, "load_object", return_value={}))
                stack.enter_context(mock.patch.object(RUNNER, "locate_processor_checkpoint", return_value=(root / "state.json", checkpoint)))
                stack.enter_context(mock.patch.object(RUNNER, "validate_generation_identity"))
                stack.enter_context(mock.patch.object(RUNNER, "validate_processor_bundle", return_value=bundle))
                stack.enter_context(mock.patch.object(RUNNER, "verify_processor_input_compatibility"))
                stack.enter_context(mock.patch.object(RUNNER, "authenticated_current_canonical_registry", return_value={
                    "main_sha": self.source_sha,
                    "registry_path": bundle["registry_path"],
                    "registry_bytes": 37,
                    "registry_sha256": "d" * 64,
                }))
                command = stack.enter_context(mock.patch.object(RUNNER, "command", side_effect=fake_command))
                materialize = stack.enter_context(mock.patch.object(RUNNER, "load_promotion_journal_snapshot"))
                stack.enter_context(contextlib.redirect_stdout(output))
                RUNNER.execute_candidate_preparation(args, root)
            self.assertEqual(command.call_count, 1)
            materialize.assert_not_called()
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "already-canonical-payload")
            self.assertFalse(result["candidate_available"])
            self.assertEqual(result["pending_count"], 9)
            self.assertEqual(result["detail_retry_count"], 3)
            self.assertEqual(result["detail_unattempted_count"], 6)

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
                mock.patch.object(RUNNER, "validate_processor_bundle", side_effect=lambda checkpoint, *_args, **_kwargs: {
                    "composition_receipt": {"input_digests": {}},
                    "registry_path": "data/data-go-kr.registry.json",
                    "registry_bytes": 37,
                    "registry_sha256": checkpoint["generation_inputs"]["candidate_sha256"],
                    "baseline_sha256": checkpoint["generation_inputs"]["baseline_sha256"],
                }),
                mock.patch.object(RUNNER, "verify_processor_input_compatibility", return_value=None),
                mock.patch.object(RUNNER, "authenticated_current_canonical_registry", return_value={
                    "main_sha": self.source_sha,
                    "registry_path": "data/data-go-kr.registry.json",
                    "registry_bytes": 37,
                    "registry_sha256": "c" * 64,
                }),
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
            generator_material = {
                "processor_script_sha256": hashlib.sha256((root / "scripts/process-upstream-catalogue-candidate.py").read_bytes()).hexdigest(),
                "collector_handoff_helper_sha256": hashlib.sha256((root / "scripts/upstream_catalogue_handoff.py").read_bytes()).hexdigest(),
            }
            generation_inputs["generator_revision"] = hashlib.sha256(
                json.dumps(generator_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
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

            handoff_path = root / "scripts/upstream_catalogue_handoff.py"
            prior_handoff = handoff_path.read_bytes()
            handoff_path.write_bytes(prior_handoff + b"\n# changed admission semantics\n")
            subprocess.run(("git", "add", str(handoff_path)), cwd=root, check=True)
            subprocess.run(("git", "commit", "-qm", "change collector handoff semantics"), cwd=root, check=True)
            handoff_head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            with self.assertRaisesRegex(RUNNER.PromotionError, "input contract changed since observation: scripts/upstream_catalogue_handoff.py"):
                RUNNER.verify_processor_input_compatibility(root, checkpoint, processor_head, handoff_head, composition)
            handoff_path.write_bytes(prior_handoff)
            subprocess.run(("git", "add", str(handoff_path)), cwd=root, check=True)
            subprocess.run(("git", "commit", "-qm", "restore collector handoff"), cwd=root, check=True)

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
            "registry_path": "data/data-go-kr.registry.json",
            "registry_bytes": 1,
            "composition_receipt_sha256": "f" * 64,
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

    def actual_adopted_same_payload_refresh(self) -> tuple[dict, dict, dict]:
        """Build the source-refresh route from the frozen real #686 recovery fixture."""
        fixture = PreparedCreateRecoveryTests().fixture()
        prepared = fixture["state"]["records"][0]
        root = pathlib.Path("/read-only/issue-689-real-686-refresh")
        with (
            mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]),
            mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]),
            mock.patch.object(PR_HELPER, "load_materializer", return_value=object()),
            mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=fixture["remote_branch_sha"]),
            mock.patch.object(RUNNER, "persist_journal_record"),
        ):
            adopted, number = RUNNER.reconcile_prepared_create_pr(
                root, self.repository, prepared, PR_HELPER,
                **PreparedCreateRecoveryTests().args(prepared),
                journal_source_base_sha=PreparedCreateRecoveryTests.controller_head,
                observed_at="2026-10-03T10:00:00Z",
                run_url="https://github.com/StatPan/datapan-registry/actions/runs/37101245239/attempts/4",
            )
        self.assertEqual(number, 686)
        self.assertEqual(adopted["status"], "pending-review")
        # The native #693 adoption route must bind the receipt before it can
        # serve as a source-refresh predecessor.
        self.assertEqual(adopted["ownership"]["expected_head_sha"], adopted["candidate"]["head_sha"])
        self.assertNotEqual(adopted["ownership"]["expected_head_sha"], "0" * 40)

        target_main = "60042f6be0e8e593830e7b2433974e814f4a0a55"
        successor_head = "d" * 40
        successor = copy.deepcopy(adopted)
        successor["status"] = "prepared"
        successor["action"] = "refresh_owned"
        successor["acknowledgements"] = []
        successor["candidate"]["base_sha"] = target_main
        successor["candidate"]["head_sha"] = successor_head
        successor["candidate"]["manifest_sha256"] = "2" * 64
        successor["candidate"]["payload_readback"]["source_sha"] = successor_head
        successor["candidate"]["payload_readback"]["manifest_sha256"] = "2" * 64
        successor["ownership"]["expected_head_sha"] = adopted["candidate"]["head_sha"]
        successor["refresh_from"] = PR_HELPER.revision_reference(adopted)
        successor["refresh_target_main_sha"] = target_main
        successor["ownership"]["body"] = PR_HELPER.render_pr_body(
            successor["candidate"], successor["ownership"]["owner_id"],
        )
        successor["ownership"]["body_sha256"] = hashlib.sha256(
            successor["ownership"]["body"].encode("utf-8"),
        ).hexdigest()
        journal = PR_HELPER.append_journal_record(
            None, adopted, repository=self.repository, observed_at="2026-10-03T10:00:00Z",
        )
        journal = PR_HELPER.append_journal_record(
            journal, successor, repository=self.repository, observed_at="2026-10-03T10:01:00Z",
        )
        schema = json.loads((pathlib.Path(__file__).parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text())
        PR_HELPER.validate_journal(journal, schema)
        return fixture, adopted, successor

    def test_body_edit_ambiguous_response_uses_fresh_exact_readback_before_ack(self) -> None:
        fixture, predecessor, intent = self.actual_adopted_same_payload_refresh()
        current_main = "f" * 40
        first = dict(fixture["pull_request_readback"])
        first["headRefOid"] = intent["candidate"]["head_sha"]
        first["body"] = predecessor["ownership"]["body"]
        fresh = dict(first)
        fresh["body"] = intent["ownership"]["body"]
        temp_root = tempfile.TemporaryDirectory(prefix="issue-689-refresh-reread-")
        self.addCleanup(temp_root.cleanup)
        root = pathlib.Path(temp_root.name)
        state_sha = "9" * 40
        with (
            mock.patch.object(RUNNER, "assert_remote_main_sha", side_effect=lambda _root, sha: self.assertEqual(sha, current_main)) as main_check,
            mock.patch.object(RUNNER, "command", side_effect=RUNNER.PromotionError("gh edit response lost after server acceptance")) as edit,
            mock.patch.object(RUNNER, "gh_pr_readback", return_value=fresh) as reread,
            mock.patch.object(RUNNER, "persist_journal_record") as persist,
        ):
            acknowledged = RUNNER.complete_prepared_refresh(
                root, self.repository, intent, predecessor, PR_HELPER, first,
                controller_head_sha=current_main,
                journal_source_base_sha=current_main,
                expected_state_sha=state_sha,
                observed_at="2026-10-03T10:02:00Z",
                run_url="https://github.com/StatPan/datapan-registry/actions/runs/37109999999/attempts/1",
            )
        self.assertEqual(acknowledged["status"], "pending-review")
        self.assertEqual(acknowledged["candidate"], intent["candidate"])
        self.assertEqual(acknowledged["ownership"]["expected_head_sha"], intent["candidate"]["head_sha"])
        edit.assert_called_once()
        reread.assert_called_once_with(root, self.repository, predecessor["pr"]["number"])
        self.assertEqual(main_check.call_count, 2)
        persist.assert_called_once()
        self.assertEqual(persist.call_args.kwargs["expected_state_sha"], state_sha)
        self.assertEqual(persist.call_args.kwargs["supersede_from"], PR_HELPER.revision_reference(predecessor))
        self.assertEqual(persist.call_args.args[2], acknowledged)

    def test_execute_refresh_keeps_trusted_base_after_candidate_checkout_and_rereads_body(self) -> None:
        fixture = PreparedCreateRecoveryTests().fixture()
        predecessor = fixture["state"]["records"][0]
        predecessor = PR_HELPER.record_pr_readback(
            copy.deepcopy(predecessor), fixture["pull_request_readback"],
            observed_at="2026-10-03T10:00:00Z",
            run_url="https://github.com/StatPan/datapan-registry/actions/runs/37101245239/attempts/4",
        )
        predecessor["ownership"]["expected_head_sha"] = predecessor["candidate"]["head_sha"]
        controller_head = "60042f6be0e8e593830e7b2433974e814f4a0a55"
        candidate_head = "d" * 40
        state_sha = "9" * 40
        prepared_state_sha = "8" * 40
        generation = predecessor["candidate"]["generation_id"]
        registry_path = predecessor["candidate"]["registry_path"]
        registry_bytes = predecessor["candidate"]["registry_bytes"]
        registry_sha = predecessor["candidate"]["registry_sha256"]
        composition_sha = predecessor["candidate"]["composition_receipt_sha256"]
        owner_id = predecessor["ownership"]["owner_id"]
        branch = predecessor["ownership"]["branch"]
        target_body = f"{PR_HELPER.body_marker(owner_id, generation)}\n\nUpdated source snapshot.\n"
        old_readback = dict(fixture["pull_request_readback"])
        after_push = dict(old_readback)
        after_push["headRefOid"] = candidate_head
        exact_readback = dict(after_push)
        exact_readback["body"] = target_body
        journal = PR_HELPER.append_journal_record(
            None, predecessor, repository=self.repository, observed_at="2026-10-03T10:00:00Z",
        )
        bundle = {
            "status": "ready", "registry_path": registry_path,
            "registry_bytes": registry_bytes, "registry_sha256": registry_sha,
            "composition_receipt_sha256": composition_sha,
            "composition_receipt": {"schema_version": "fixture"},
            "composition_receipt_path": "reports/composition.json",
            "composition_outputs_dir": "reports/composition",
            "baseline_sha256": "a" * 64,
        }
        checkpoint = {
            "source_id": predecessor["candidate"]["source_id"],
            "source_scope": predecessor["candidate"]["scope"],
            "generation_id": generation,
            "last_observation": {"observed_at": "2026-09-29T00:00:00Z"},
        }
        evidence = {
            "diagnostic_current_source_applicability": {"status": "revalidation_required"},
            "commands": [{
                "output_path": "reports/generated-fixture.json",
                "output_bytes": 3, "output_sha256": "c" * 64,
            }],
        }
        refresh_module = types.SimpleNamespace(
            run_source_refresh=lambda **_kwargs: (registry_sha, [], copy.deepcopy(evidence)),
            run_ledger_refresh=lambda _root: None,
        )
        existing = [{
            "number": predecessor["pr"]["number"],
            "record": predecessor,
            "revision_ref": PR_HELPER.revision_reference(predecessor),
        }]
        open_existing = [{
            "number": predecessor["pr"]["number"],
            "revision_ref": PR_HELPER.revision_reference(predecessor),
        }]
        route = (existing, open_existing, None, {"branch": branch})
        prepared_holder: dict[str, dict] = {}

        def prepare_upload(candidate: dict, *_args: object, **_kwargs: object) -> tuple[dict, dict]:
            candidate = copy.deepcopy(candidate)
            candidate["payload_readback"] = {
                "status": "verified", "readback": "isolated_lfs_storage_verified",
                "sha256": registry_sha, "source_sha": candidate_head,
            }
            prepared = {
                "schema_version": PR_HELPER.SCHEMA_VERSION,
                "status": "prepared", "action": "refresh_owned",
                "candidate": candidate,
                "ownership": {
                    "owner_id": owner_id, "branch": branch,
                    "expected_head_sha": predecessor["candidate"]["head_sha"],
                    "issue_number": predecessor["ownership"]["issue_number"],
                    "issue_url": predecessor["ownership"]["issue_url"],
                    "body": "", "body_sha256": "",
                },
                "pr": copy.deepcopy(predecessor["pr"]),
                "acknowledgements": [], "blockers": [],
            }
            prepared_holder["value"] = prepared
            return prepared, {"branch": branch}

        def fake_command(argv: tuple[str, ...], _root: pathlib.Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ("git", "rev-parse", "HEAD"):
                sha = controller_head if not hasattr(fake_command, "seen_base") else candidate_head
                setattr(fake_command, "seen_base", True)
                return subprocess.CompletedProcess(argv, 0, f"{sha}\n", "")
            if argv[:3] == ("git", "ls-remote", "--heads"):
                return subprocess.CompletedProcess(argv, 0, f"{controller_head}\trefs/heads/main\n", "")
            if argv[:2] == ("git", "write-tree"):
                return subprocess.CompletedProcess(argv, 0, "e" * 40 + "\n", "")
            if argv[:3] == ("gh", "pr", "edit"):
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory(prefix="issue-689-execute-refresh-") as raw:
            root = pathlib.Path(raw)
            (root / "reports").mkdir()
            (root / "reports/generated-fixture.json").write_bytes(b"abc")
            (root / "bundle").mkdir()
            (root / "bundle/composed-candidate.registry.json").write_bytes(b"fixture registry")
            args = types.SimpleNamespace(
                workflow_run_id="37100705274", workflow_run_attempt="1",
                bundle_dir=root / "bundle", workflow_run_head_sha="a" * 40,
                state_root=root / "processor-state", datapan_cli=root / "datapan-cli",
                processor_artifact_id="artifact-11266965270", prepare_only=False,
                source_refresh_predecessor=predecessor,
                source_refresh_target_main_sha=controller_head,
                source_refresh_expected_state_sha=state_sha,
                source_refresh_successor_head_sha=None,
            )
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {
                    "GITHUB_REPOSITORY": self.repository,
                    "GITHUB_RUN_ID": "37115233183", "GITHUB_RUN_ATTEMPT": "1",
                }))
                stack.enter_context(mock.patch.object(RUNNER, "validate_no_candidate_processor_result", return_value=None))
                stack.enter_context(mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=PR_HELPER))
                stack.enter_context(mock.patch.object(RUNNER, "load_module", return_value=refresh_module))
                stack.enter_context(mock.patch.object(RUNNER, "load_object", side_effect=lambda path: (
                    {"artifacts": [{"path": registry_path, "kind": "registry", "bytes": registry_bytes}]}
                    if pathlib.Path(path).name == "manifest.json"
                    else {"canonical_registry": {"sha256": "published-pin"}}
                    if pathlib.Path(path).name == "registry-distribution.json"
                    else {}
                )))
                stack.enter_context(mock.patch.object(RUNNER, "locate_processor_checkpoint", return_value=("run-attempt", checkpoint)))
                stack.enter_context(mock.patch.object(RUNNER, "validate_generation_identity"))
                stack.enter_context(mock.patch.object(RUNNER, "validate_processor_bundle", return_value=bundle))
                stack.enter_context(mock.patch.object(RUNNER, "verify_processor_input_compatibility"))
                stack.enter_context(mock.patch.object(
                    RUNNER, "authenticated_current_canonical_registry",
                    side_effect=AssertionError("explicit source refresh must retain its authorized path"),
                ))
                stack.enter_context(mock.patch.object(RUNNER, "registry_sha_from_path", return_value=(registry_bytes, "a" * 64)))
                stack.enter_context(mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=(journal, state_sha)))
                stack.enter_context(mock.patch.object(RUNNER, "update_registry_review_artifacts", return_value=root / "reports/review"))
                stack.enter_context(mock.patch.object(RUNNER, "run_bound_validation", return_value=[]))
                stack.enter_context(mock.patch.object(RUNNER, "stage_candidate_outputs", return_value=[registry_path]))
                stack.enter_context(mock.patch.object(RUNNER, "finish_review_policy_status", return_value="configured"))
                stack.enter_context(mock.patch.object(RUNNER, "manual_review_status", return_value="revalidation_required"))
                stack.enter_context(mock.patch.object(RUNNER, "file_sha256", return_value="c" * 64))
                stack.enter_context(mock.patch.object(RUNNER, "existing_pr_rows", return_value=(existing, predecessor["ownership"]["issue_number"])))
                stack.enter_context(mock.patch.object(RUNNER, "inspect_existing_pr_route", return_value=route))
                stack.enter_context(mock.patch.object(RUNNER, "ensure_candidate_issue", return_value=(predecessor["ownership"]["issue_number"], predecessor["ownership"]["issue_url"])))
                stack.enter_context(mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]))
                readback = stack.enter_context(mock.patch.object(RUNNER, "gh_pr_readback", side_effect=(old_readback, after_push, exact_readback)))
                main_check = stack.enter_context(mock.patch.object(RUNNER, "assert_remote_main_sha", side_effect=lambda _root, sha: self.assertEqual(sha, controller_head)))
                persist = stack.enter_context(mock.patch.object(RUNNER, "persist_journal_record", side_effect=(prepared_state_sha, "7" * 40)))
                stack.enter_context(mock.patch.object(RUNNER, "verify_release_ci_observation", return_value=("queued", None)))
                command = stack.enter_context(mock.patch.object(RUNNER, "command", side_effect=fake_command))
                stack.enter_context(mock.patch.object(PR_HELPER, "update_registry_manifest_artifact"))
                stack.enter_context(mock.patch.object(PR_HELPER, "prepare_lfs_upload", side_effect=prepare_upload))
                stack.enter_context(mock.patch.object(PR_HELPER, "push_owned_branch", side_effect=lambda _candidate, receipt, *_a, **_kw: receipt))
                stack.enter_context(mock.patch.object(PR_HELPER, "render_pr_body", return_value=target_body))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                (root / registry_path).parent.mkdir(parents=True, exist_ok=True)
                RUNNER.execute_candidate_preparation(args, root)

        self.assertEqual(command.call_args_list[0].args[0], ("git", "rev-parse", "HEAD"))
        self.assertIn(("gh", "pr", "edit", "686", "--repo", self.repository, "--title", RUNNER.pr_title(prepared_holder["value"]["candidate"]), "--body-file", str(root / ".datapan/candidate-pr.md")), [call.args[0] for call in command.call_args_list])
        self.assertEqual(readback.call_count, 3)
        self.assertEqual(main_check.call_count, 4)
        self.assertTrue(all(call.args[1] == controller_head for call in main_check.call_args_list))
        self.assertEqual(persist.call_args_list[0].args[1], controller_head)
        self.assertEqual(persist.call_args_list[0].kwargs["expected_state_sha"], state_sha)
        self.assertEqual(persist.call_args_list[1].args[1], controller_head)
        self.assertEqual(persist.call_args_list[1].kwargs["expected_state_sha"], prepared_state_sha)
        self.assertEqual(persist.call_args_list[1].args[2]["ownership"]["expected_head_sha"], candidate_head)

    def test_refresh_edit_readback_and_state_cas_fail_closed_on_drift(self) -> None:
        fixture, predecessor, intent = self.actual_adopted_same_payload_refresh()
        current_main = "f" * 40
        first = dict(fixture["pull_request_readback"])
        first["headRefOid"] = intent["candidate"]["head_sha"]
        first["body"] = predecessor["ownership"]["body"]
        exact = dict(first)
        exact["body"] = intent["ownership"]["body"]
        drifted = dict(exact)
        drifted["body"] += "unowned edit\n"

        for label, observed, persist_error, error_pattern in (
            ("old_body_after_ambiguous_edit", first, None, "body edit did not read back"),
            ("human_body_edit", drifted, None, "human_head_change_or_body_change"),
            ("state_cas_conflict", exact, RUNNER.PromotionError("promotion state compare-and-swap conflict"), "compare-and-swap conflict"),
        ):
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory(prefix="issue-689-refresh-fail-closed-") as raw:
                    with (
                        mock.patch.object(RUNNER, "assert_remote_main_sha", side_effect=lambda _root, sha: self.assertEqual(sha, current_main)),
                        mock.patch.object(RUNNER, "command", side_effect=RUNNER.PromotionError("gh edit response lost")),
                        mock.patch.object(RUNNER, "gh_pr_readback", return_value=observed),
                        mock.patch.object(RUNNER, "persist_journal_record", side_effect=persist_error) as persist,
                    ):
                        with self.assertRaisesRegex(RUNNER.PromotionError, error_pattern):
                            RUNNER.complete_prepared_refresh(
                                pathlib.Path(raw), self.repository, intent, predecessor, PR_HELPER, first,
                                controller_head_sha=current_main,
                                journal_source_base_sha=current_main,
                                expected_state_sha="9" * 40,
                                observed_at="2026-10-03T10:04:00Z",
                                run_url="https://github.com/StatPan/datapan-registry/actions/runs/37109999999/attempts/1",
                            )
                    self.assertEqual(persist.call_count, 1 if label == "state_cas_conflict" else 0)

    def test_reconcile_finishes_exact_pushed_intent_after_main_advances(self) -> None:
        fixture, predecessor, intent = self.actual_adopted_same_payload_refresh()
        current_main = "f" * 40
        state_sha = "9" * 40
        fresh_state_sha = "8" * 40
        first = dict(fixture["pull_request_readback"])
        first["headRefOid"] = intent["candidate"]["head_sha"]
        first["body"] = predecessor["ownership"]["body"]
        edited = dict(first)
        edited["body"] = intent["ownership"]["body"]
        adopted_journal = PR_HELPER.append_journal_record(
            None, predecessor, repository=self.repository, observed_at="2026-10-03T10:00:00Z",
        )
        journal = PR_HELPER.append_journal_record(
            adopted_journal, intent, repository=self.repository, observed_at="2026-10-03T10:01:00Z",
        )
        final_ack = PR_HELPER.record_pr_readback(
            {**intent, "ownership": {**intent["ownership"], "expected_head_sha": intent["candidate"]["head_sha"]}},
            edited, observed_at="2026-10-03T10:02:00Z",
            run_url="https://github.com/StatPan/datapan-registry/actions/runs/37109999999/attempts/1",
        )
        final_journal = PR_HELPER.append_journal_record(
            journal, final_ack, repository=self.repository, observed_at="2026-10-03T10:02:00Z",
            supersede_from=PR_HELPER.revision_reference(predecessor),
        )
        open_row = {"number": predecessor["pr"]["number"]}
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": self.repository,
                "GITHUB_RUN_ID": "99999999", "GITHUB_RUN_ATTEMPT": "1",
            }),
            mock.patch.object(RUNNER, "load_module", return_value=PR_HELPER),
            mock.patch.object(RUNNER, "load_promotion_journal_snapshot", side_effect=((journal, state_sha), (final_journal, fresh_state_sha))),
            mock.patch.object(RUNNER, "command", side_effect=lambda argv, _root, **_kwargs: subprocess.CompletedProcess(
                argv, 0, stdout=f"{current_main}\n", stderr="",
            ) if argv[:3] == ("git", "rev-parse", "HEAD") else subprocess.CompletedProcess(argv, 0, "", "")) as command,
            mock.patch.object(RUNNER, "assert_remote_main_sha", side_effect=lambda _root, sha: self.assertEqual(sha, current_main)) as main_check,
            mock.patch.object(RUNNER, "gh_pr_readback", side_effect=(first, edited, edited)) as readback,
            mock.patch.object(RUNNER, "gh_open_prs", return_value=[open_row]),
            mock.patch.object(RUNNER, "persist_journal_record") as persist,
            mock.patch.object(RUNNER, "ensure_verify_release_ci", return_value={"ci": {"state": "queued"}}),
            contextlib.redirect_stdout(output),
        ):
            temp_root = tempfile.TemporaryDirectory(prefix="issue-689-refresh-main-moved-")
            self.addCleanup(temp_root.cleanup)
            RUNNER.reconcile_open_promotions(pathlib.Path(temp_root.name))

        self.assertEqual(command.call_args_list[0].args[0], ("git", "rev-parse", "HEAD"))
        edit_calls = [call for call in command.call_args_list if call.args[0][:3] == ("gh", "pr", "edit")]
        self.assertEqual(len(edit_calls), 1)
        self.assertEqual(main_check.call_count, 2)
        self.assertTrue(all(call.args[1] == current_main for call in main_check.call_args_list))
        self.assertEqual(readback.call_count, 3)
        self.assertEqual(persist.call_args_list[0].args[1], current_main)
        self.assertEqual(persist.call_args_list[0].args[2]["status"], "pending-review")
        self.assertEqual(persist.call_args_list[0].kwargs["expected_state_sha"], state_sha)
        self.assertEqual(persist.call_args_list[0].kwargs["supersede_from"], PR_HELPER.revision_reference(predecessor))
        self.assertIn("prepared-source-refresh-recovered", output.getvalue())

    def test_legacy_predecessor_reference_is_preserved_when_refresh_is_finalized(self) -> None:
        fixture, predecessor, intent = self.actual_adopted_same_payload_refresh()
        legacy_ref = dict(intent["refresh_from"])
        legacy_ref.pop("manifest_sha256", None)
        intent["refresh_from"] = legacy_ref
        journal = PR_HELPER.append_journal_record(
            None, predecessor, repository=self.repository, observed_at="2026-10-03T10:00:00Z",
        )
        journal = PR_HELPER.append_journal_record(
            journal, intent, repository=self.repository, observed_at="2026-10-03T10:01:00Z",
        )
        schema = json.loads((pathlib.Path(__file__).parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text())
        PR_HELPER.validate_journal(journal, schema)
        observed = dict(fixture["pull_request_readback"])
        observed["headRefOid"] = intent["candidate"]["head_sha"]
        observed["body"] = intent["ownership"]["body"]

        def append_exact(_root: pathlib.Path, _base: str, receipt: dict, *, observed_at: str, supersede_from: dict, expected_state_sha: str) -> None:
            self.assertEqual(supersede_from, legacy_ref)
            self.assertEqual(expected_state_sha, "9" * 40)
            updated = PR_HELPER.append_journal_record(
                journal, receipt, repository=self.repository, observed_at=observed_at,
                supersede_from=supersede_from,
            )
            PR_HELPER.validate_journal(updated, schema)

        with (
            mock.patch.object(RUNNER, "assert_remote_main_sha") as main_check,
            mock.patch.object(RUNNER, "persist_journal_record", side_effect=append_exact) as persist,
        ):
            refreshed = RUNNER.complete_prepared_refresh(
                pathlib.Path("/read-only/legacy-refresh"), self.repository,
                intent, predecessor, PR_HELPER, observed,
                controller_head_sha="f" * 40,
                journal_source_base_sha="f" * 40,
                expected_state_sha="9" * 40,
                observed_at="2026-10-03T10:02:00Z",
                run_url="https://github.com/StatPan/datapan-registry/actions/runs/37109999999/attempts/1",
            )
        self.assertEqual(refreshed["status"], "pending-review")
        persist.assert_called_once()
        main_check.assert_called_once_with(pathlib.Path("/read-only/legacy-refresh"), "f" * 40)

    def test_ordinary_refresh_completion_allows_a_new_generation_and_payload(self) -> None:
        predecessor, intent = self.receipts()
        for receipt in (predecessor, intent):
            receipt["candidate"]["registry_path"] = "data/registry.json"
            receipt["candidate"]["registry_bytes"] = 1
        intent["candidate"]["generation_id"] = "generation-next"
        intent["candidate"]["registry_sha256"] = "1" * 64
        intent["candidate"]["composition_receipt_sha256"] = "2" * 64
        intent["ownership"]["body"] = (
            f"{PR_HELPER.body_marker(intent['ownership']['owner_id'], intent['candidate']['generation_id'])}"
            "\n\nA new B generation produced this payload.\n"
        )
        intent["ownership"]["body_sha256"] = hashlib.sha256(intent["ownership"]["body"].encode()).hexdigest()
        journal = self.journal(predecessor, intent)
        current_main = "f" * 40
        state_sha = "9" * 40
        first = self.github_api_readback(predecessor["ownership"]["body"], intent["candidate"]["head_sha"])
        edited = self.github_api_readback(intent["ownership"]["body"], intent["candidate"]["head_sha"], generation_id="generation-next")
        final_ack = PR_HELPER.record_pr_readback(
            {**intent, "ownership": {**intent["ownership"], "expected_head_sha": intent["candidate"]["head_sha"]}},
            edited, observed_at="2026-10-03T10:03:00Z",
            run_url="https://github.com/StatPan/datapan-registry/actions/runs/37109999999/attempts/1",
        )
        final_journal = PR_HELPER.append_journal_record(
            journal, final_ack, repository=self.repository, observed_at="2026-10-03T10:03:00Z",
            supersede_from=intent["refresh_from"],
        )
        output = io.StringIO()
        temp_root = tempfile.TemporaryDirectory(prefix="issue-689-ordinary-refresh-")
        self.addCleanup(temp_root.cleanup)
        with (
            mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": self.repository,
                "GITHUB_RUN_ID": "99999999", "GITHUB_RUN_ATTEMPT": "1",
            }),
            mock.patch.object(RUNNER, "load_module", return_value=PR_HELPER),
            mock.patch.object(RUNNER, "load_promotion_journal_snapshot", side_effect=((journal, state_sha), (final_journal, "8" * 40))),
            mock.patch.object(RUNNER, "command", side_effect=lambda argv, _root, **_kwargs: subprocess.CompletedProcess(
                argv, 0, stdout=f"{current_main}\n", stderr="",
            ) if argv[:3] == ("git", "rev-parse", "HEAD") else subprocess.CompletedProcess(argv, 0, "", "")),
            mock.patch.object(RUNNER, "assert_remote_main_sha", side_effect=lambda _root, sha: self.assertEqual(sha, current_main)) as main_check,
            mock.patch.object(RUNNER, "gh_pr_readback", side_effect=(first, edited, edited)) as readback,
            mock.patch.object(RUNNER, "gh_open_prs", return_value=[{"number": self.pr_number}]),
            mock.patch.object(RUNNER, "persist_journal_record") as persist,
            mock.patch.object(RUNNER, "ensure_verify_release_ci", return_value={"ci": {"state": "queued"}}),
            contextlib.redirect_stdout(output),
        ):
            RUNNER.reconcile_open_promotions(pathlib.Path(temp_root.name))
        self.assertEqual(persist.call_args_list[0].args[2]["status"], "pending-review")
        self.assertEqual(persist.call_args_list[0].args[2]["candidate"]["generation_id"], "generation-next")
        self.assertEqual(persist.call_args_list[0].args[2]["candidate"]["registry_sha256"], "1" * 64)
        self.assertEqual(persist.call_args_list[0].kwargs["expected_state_sha"], state_sha)
        self.assertEqual(main_check.call_count, 2)
        self.assertEqual(readback.call_count, 3)
        self.assertIn("prepared-source-refresh-recovered", output.getvalue())

    def test_ordinary_refresh_keeps_legacy_prepared_predecessor_compatibility(self) -> None:
        predecessor, intent = self.receipts()
        predecessor["status"] = "prepared"
        for receipt in (predecessor, intent):
            receipt["candidate"]["registry_path"] = "data/registry.json"
            receipt["candidate"]["registry_bytes"] = 1
        intent["candidate"]["generation_id"] = "generation-next"
        intent["candidate"]["registry_sha256"] = "1" * 64
        intent["candidate"]["composition_receipt_sha256"] = "2" * 64
        intent["ownership"]["body"] = (
            f"{PR_HELPER.body_marker(intent['ownership']['owner_id'], intent['candidate']['generation_id'])}"
            "\n\nA new B generation produced this payload.\n"
        )
        intent["ownership"]["body_sha256"] = hashlib.sha256(intent["ownership"]["body"].encode()).hexdigest()
        observed = self.github_api_readback(intent["ownership"]["body"], intent["candidate"]["head_sha"], generation_id="generation-next")
        with (
            mock.patch.object(RUNNER, "assert_remote_main_sha") as main_check,
            mock.patch.object(RUNNER, "persist_journal_record") as persist,
        ):
            refreshed = RUNNER.complete_prepared_refresh(
                pathlib.Path("/read-only/legacy-prepared-predecessor"), self.repository,
                intent, predecessor, PR_HELPER, observed,
                controller_head_sha="f" * 40,
                journal_source_base_sha="f" * 40,
                expected_state_sha="9" * 40,
                observed_at="2026-10-03T10:05:00Z",
                run_url="https://github.com/StatPan/datapan-registry/actions/runs/37109999999/attempts/1",
            )
        self.assertEqual(refreshed["status"], "pending-review")
        self.assertEqual(refreshed["candidate"]["generation_id"], "generation-next")
        main_check.assert_called_once_with(pathlib.Path("/read-only/legacy-prepared-predecessor"), "f" * 40)
        persist.assert_called_once()

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

    def test_same_payload_source_successor_requires_exact_head_lookup(self) -> None:
        old, new = self.receipts()
        new["candidate"]["registry_sha256"] = old["candidate"]["registry_sha256"]
        target_main = "d" * 40
        new["candidate"]["base_sha"] = target_main
        new["refresh_from"] = PR_HELPER.revision_reference(old)
        new["refresh_target_main_sha"] = target_main
        journal = self.journal(old, new)

        with self.assertRaisesRegex(RUNNER.PromotionError, "payload lookup is ambiguous"):
            RUNNER.journal_record_for(
                journal, self.source_id, self.scope, self.generation_id,
                old["candidate"]["registry_sha256"],
            )
        self.assertEqual(
            RUNNER.journal_record_for(
                journal, self.source_id, self.scope, self.generation_id,
                old["candidate"]["registry_sha256"], candidate_head_sha=self.old_head,
            ),
            old,
        )
        self.assertEqual(
            RUNNER.journal_record_for(
                journal, self.source_id, self.scope, self.generation_id,
                new["candidate"]["registry_sha256"], candidate_head_sha=self.new_head,
            ),
            new,
        )

    def test_trusted_source_refresh_intent_binds_target_main_in_all_crash_phases(self) -> None:
        old, new = self.receipts()
        target_main = "e" * 40
        new["candidate"]["base_sha"] = target_main
        new["refresh_from"] = PR_HELPER.revision_reference(old)
        new["refresh_target_main_sha"] = target_main
        normalized = self.github_api_readback(old["ownership"]["body"], self.old_head)
        self.assertEqual(RUNNER.refresh_pr_phase(new, old, normalized, PR_HELPER), "before-push")
        new["candidate"]["base_sha"] = "f" * 40
        with self.assertRaisesRegex(RUNNER.PromotionError, "exact trusted target main"):
            RUNNER.refresh_pr_phase(new, old, normalized, PR_HELPER)

    def test_stale_pre_push_refresh_is_scoped_and_unrelated_pr_reconciliation_continues(self) -> None:
        old, intent = self.receipts()
        target_main = "e" * 40
        current_main = "f" * 40
        intent["candidate"]["base_sha"] = target_main
        intent["refresh_from"] = PR_HELPER.revision_reference(old)
        intent["refresh_target_main_sha"] = target_main
        journal = self.journal(old, intent)
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": self.repository,
                "GITHUB_RUN_ID": "99999999",
                "GITHUB_RUN_ATTEMPT": "1",
            }),
            mock.patch.object(RUNNER, "load_module", return_value=PR_HELPER),
            mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=(journal, "9" * 40)),
            mock.patch.object(RUNNER, "command", return_value=subprocess.CompletedProcess(["git"], 0, stdout=f"{current_main}\n", stderr="")),
            mock.patch.object(RUNNER, "assert_remote_main_sha") as main_check,
            mock.patch.object(RUNNER, "gh_pr_readback", side_effect=(
                self.github_api_readback(old["ownership"]["body"], self.old_head),
                self.github_api_readback(old["ownership"]["body"], self.old_head),
            )) as readback,
            mock.patch.object(RUNNER, "gh_open_prs", return_value=[{"number": self.pr_number}]) as open_prs,
            mock.patch.object(RUNNER, "persist_journal_record") as persist,
            mock.patch.object(RUNNER, "reconcile_prepared_create_pr", return_value=(old, self.pr_number)) as create_recovery,
            mock.patch.object(RUNNER, "ensure_verify_release_ci", return_value={"ci": {"state": "queued"}}),
            contextlib.redirect_stdout(output),
        ):
            RUNNER.reconcile_open_promotions(pathlib.Path("."))
        main_check.assert_not_called()
        self.assertEqual(readback.call_count, 2)
        open_prs.assert_called_once()
        create_recovery.assert_called_once()
        persist.assert_not_called()
        report = json.loads(next(line for line in output.getvalue().splitlines() if "prepared-source-refresh-stale-before-push" in line))
        self.assertEqual(report["target_main_sha"], target_main)
        self.assertEqual(report["current_main_sha"], current_main)
        self.assertIn("explicit retirement and re-request", report["action"])

    def test_canonical_promotion_workflow_refresh_dispatch_is_complete_and_main_only(self) -> None:
        import yaml

        workflow_path = pathlib.Path(__file__).parents[1] / ".github/workflows/canonical-update-promotion.yml"
        workflow = yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)
        dispatch = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(
            set(dispatch),
            {
                "refresh_pr_number", "expected_predecessor_head_sha",
                "expected_predecessor_body_sha256", "expected_predecessor_manifest_sha256",
                "target_main_sha", "processor_state_sha",
            },
        )
        self.assertTrue(all(row["default"] == "" for row in dispatch.values()))
        job_condition = workflow["jobs"]["reconcile"]["if"]
        self.assertIn("github.ref == format('refs/heads/{0}', github.event.repository.default_branch)", job_condition)
        steps = workflow["jobs"]["reconcile"]["steps"]
        refresh = next(step for step in steps if step.get("name") == "Refresh one explicitly bound owned source revision")
        self.assertIn("inputs.refresh_pr_number != ''", refresh["if"])
        self.assertIn("inputs.processor_state_sha != ''", refresh["if"])
        self.assertIn("--mode refresh-owned-source", refresh["run"])
        self.assertIn("--target-main-sha", refresh["run"])
        self.assertIn("--processor-state-repo .datapan/processor-state", refresh["run"])
        self.assertIn("--processor-state-sha", refresh["run"])
        for step_name in ("Reconcile owned PRs and exact-head CI", "Recover at most one durable ready processor bundle"):
            normal = next(step for step in steps if step.get("name") == step_name)
            self.assertIn("inputs.processor_state_sha == ''", normal["if"])
            self.assertIn("inputs.refresh_pr_number == ''", normal["if"])
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
    controller_head = "8a1bf19aa3495fc515e24a604650da3b376d521d"

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
        self.assertNotEqual(receipt["candidate"]["base_sha"], self.controller_head)
        self.assertEqual(receipt["candidate"]["head_sha"], "bcc306c20c424faa9e789444e5914bf1887b930d")
        self.assertEqual(pull["baseRefOid"], receipt["candidate"]["base_sha"])
        self.assertEqual(receipt["ownership"]["issue_number"], 685)
        self.assertEqual(pull["number"], 686)
        self.assertEqual(pull["headRepository"], "StatPan/datapan-registry")
        self.assertEqual(fixture["remote_branch_sha"], receipt["candidate"]["head_sha"])
        self.assertEqual(self.validate(fixture), 686)

    def test_current_main_advance_does_not_rewrite_original_candidate_base(self) -> None:
        fixture = self.fixture()
        receipt = fixture["state"]["records"][0]
        self.assertEqual(receipt["candidate"]["base_sha"], "5aae73b9a99255397ee2807d58c324094757bf8c")
        fixture["pull_request_readback"]["baseRefOid"] = self.controller_head

        self.assertEqual(self.validate(fixture), 686)
        self.assertEqual(receipt["candidate"]["base_sha"], "5aae73b9a99255397ee2807d58c324094757bf8c")

    def test_create_replacement_uses_its_exact_owned_branch(self) -> None:
        fixture = self.fixture()
        receipt = fixture["state"]["records"][0]
        receipt["action"] = "create_replacement"
        branch = PR_HELPER.automation_branch(receipt["candidate"], "create_replacement")
        receipt["ownership"]["branch"] = branch
        fixture["open_pr_rows"][0]["head_ref"] = branch
        fixture["pull_request_readback"]["headRefName"] = branch

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
                expected_ownership = copy.deepcopy(intent["ownership"])
                expected_ownership["expected_head_sha"] = intent["candidate"]["head_sha"]
                self.assertEqual(receipt["ownership"], expected_ownership)
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
            ("merge commit type", lambda f: f["pull_request_readback"].__setitem__("mergeCommit", "not-an-object"), {}),
            ("merge commit digest", lambda f: f["pull_request_readback"].__setitem__("mergeCommit", {"oid": "invalid"}), {}),
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

    def test_reconcile_pr_zero_recovers_from_durable_evidence_without_loading_b_artifact(self) -> None:
        fixture = self.fixture()
        intent = fixture["state"]["records"][0]
        root = pathlib.Path("/read-only/reconcile")
        materializer = object()
        with (
            mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": "StatPan/datapan-registry",
                "GITHUB_RUN_ID": "37109999999", "GITHUB_RUN_ATTEMPT": "4",
            }),
            mock.patch.object(RUNNER, "load_module", return_value=PR_HELPER),
            mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=(fixture["state"], "a" * 40)),
            mock.patch.object(RUNNER, "command", return_value=subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, self.controller_head + "\n", "")),
            mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]) as list_prs,
            mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]) as read_pr,
            mock.patch.object(PR_HELPER, "load_materializer", return_value=materializer),
            mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=fixture["remote_branch_sha"]) as read_branch,
            mock.patch.object(RUNNER, "persist_journal_record") as persist,
            mock.patch.object(RUNNER, "ensure_verify_release_ci", return_value={"ci": {"state": "queued"}}) as ensure_ci,
            mock.patch.object(RUNNER, "download_processor_artifact", side_effect=AssertionError("C PR reconciliation must not download B artifacts")) as download,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            RUNNER.reconcile_open_promotions(root)

        self.assertEqual(persist.call_count, 1)
        recovered = persist.call_args.args[2]
        self.assertEqual(recovered["status"], "pending-review")
        self.assertEqual(recovered["pr"]["number"], 686)
        self.assertEqual(recovered["candidate"]["base_sha"], "5aae73b9a99255397ee2807d58c324094757bf8c")
        self.assertEqual(recovered["candidate"]["head_sha"], "bcc306c20c424faa9e789444e5914bf1887b930d")
        self.assertEqual(recovered["candidate"], intent["candidate"])
        ensure_ci.assert_called_once_with(root, recovered)
        self.assertEqual(len(list_prs.call_args_list), 2)
        read_pr.assert_called_once_with(root, "StatPan/datapan-registry", 686)
        read_branch.assert_called_once_with(materializer, root, "origin", f"refs/heads/{intent['ownership']['branch']}")
        download.assert_not_called()

    def test_reconcile_pr_zero_prepare_only_performs_readback_without_state_or_ci_writes(self) -> None:
        fixture = self.fixture()
        intent = fixture["state"]["records"][0]
        root = pathlib.Path("/read-only/reconcile-prepare-only")
        materializer = object()
        with (
            mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": "StatPan/datapan-registry",
                "GITHUB_RUN_ID": "37109999999", "GITHUB_RUN_ATTEMPT": "4",
            }),
            mock.patch.object(RUNNER, "load_module", return_value=PR_HELPER),
            mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=(fixture["state"], "a" * 40)),
            mock.patch.object(RUNNER, "command", return_value=subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, self.controller_head + "\n", "")),
            mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]) as list_prs,
            mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]) as read_pr,
            mock.patch.object(PR_HELPER, "load_materializer", return_value=materializer),
            mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=fixture["remote_branch_sha"]) as read_branch,
            mock.patch.object(RUNNER, "persist_journal_record") as persist,
            mock.patch.object(RUNNER, "ensure_verify_release_ci") as ensure_ci,
            mock.patch.object(RUNNER, "download_processor_artifact", side_effect=AssertionError("C PR reconciliation must not download B artifacts")) as download,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            RUNNER.reconcile_open_promotions(root, prepare_only=True)

        persist.assert_not_called()
        ensure_ci.assert_not_called()
        self.assertEqual(len(list_prs.call_args_list), 1)
        read_pr.assert_called_once_with(root, "StatPan/datapan-registry", 686)
        self.assertEqual(read_branch.call_count, 1)
        download.assert_not_called()

    def test_reconcile_pr_zero_mismatches_fail_closed_without_state_or_ci_writes(self) -> None:
        cases = ("zero", "duplicate", "body-drift")
        for case in cases:
            with self.subTest(case=case):
                fixture = self.fixture()
                rows = fixture["open_pr_rows"]
                observed = fixture["pull_request_readback"]
                if case == "zero":
                    rows = []
                elif case == "duplicate":
                    duplicate = copy.deepcopy(rows[0])
                    duplicate["number"] = 687
                    rows = [rows[0], duplicate]
                else:
                    observed = {**observed, "body": observed["body"] + "unowned edit\n"}
                root = pathlib.Path(f"/read-only/reconcile-{case}")
                with (
                    mock.patch.dict(os.environ, {
                        "GITHUB_REPOSITORY": "StatPan/datapan-registry",
                        "GITHUB_RUN_ID": "37109999999", "GITHUB_RUN_ATTEMPT": "4",
                    }),
                    mock.patch.object(RUNNER, "load_module", return_value=PR_HELPER),
                    mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=(fixture["state"], "a" * 40)),
                    mock.patch.object(RUNNER, "command", return_value=subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, self.controller_head + "\n", "")),
                    mock.patch.object(RUNNER, "gh_open_prs", side_effect=[rows, rows]),
                    mock.patch.object(RUNNER, "gh_pr_readback", return_value=observed),
                    mock.patch.object(PR_HELPER, "load_materializer", return_value=object()),
                    mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=fixture["remote_branch_sha"]),
                    mock.patch.object(RUNNER, "persist_journal_record") as persist,
                    mock.patch.object(RUNNER, "ensure_verify_release_ci") as ensure_ci,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    with self.assertRaises(RUNNER.PromotionError):
                        RUNNER.reconcile_open_promotions(root)

                persist.assert_not_called()
                ensure_ci.assert_not_called()

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

        for prepare_only in (False, True):
            with self.subTest(prepare_only=prepare_only), tempfile.TemporaryDirectory() as raw:
                root = pathlib.Path(raw)
                args = types.SimpleNamespace(
                    workflow_run_id="37100705274", workflow_run_attempt="1",
                    bundle_dir=root / "bundle", workflow_run_head_sha="e" * 40,
                    state_root=root / "processor-state", datapan_cli=root / "datapan-cli",
                    processor_artifact_id="artifact-1", event_run_id=None,
                    prepare_only=prepare_only,
                )
                sequence: list[str] = []
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.dict(os.environ, {
                        "GITHUB_REPOSITORY": "StatPan/datapan-registry",
                        "GITHUB_RUN_ID": "37109999999", "GITHUB_RUN_ATTEMPT": "1",
                    }))
                    stack.enter_context(mock.patch.object(RUNNER, "validate_no_candidate_processor_result", return_value=None))
                    stack.enter_context(mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=PR_HELPER))
                    stack.enter_context(mock.patch.object(RUNNER, "load_object", side_effect=fake_load_object))
                    stack.enter_context(mock.patch.object(RUNNER, "locate_processor_checkpoint", return_value=("run-1", checkpoint)))
                    stack.enter_context(mock.patch.object(RUNNER, "validate_generation_identity"))
                    stack.enter_context(mock.patch.object(RUNNER, "validate_processor_bundle", return_value=bundle))
                    stack.enter_context(mock.patch.object(RUNNER, "verify_processor_input_compatibility"))
                    stack.enter_context(mock.patch.object(RUNNER, "authenticated_current_canonical_registry", return_value={
                        "main_sha": controller_head,
                        "registry_path": candidate["registry_path"],
                        "registry_bytes": 10,
                        "registry_sha256": baseline_sha,
                    }))
                    stack.enter_context(mock.patch.object(RUNNER, "command", side_effect=fake_command))
                    stack.enter_context(mock.patch.object(RUNNER, "registry_sha_from_path", return_value=(10, baseline_sha)))
                    stack.enter_context(mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=(fixture["state"], "a" * 40)))
                    stack.enter_context(mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]))
                    stack.enter_context(mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]))
                    stack.enter_context(mock.patch.object(PR_HELPER, "load_materializer", return_value=object()))
                    stack.enter_context(mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=fixture["remote_branch_sha"]))
                    persist = stack.enter_context(mock.patch.object(RUNNER, "persist_journal_record", side_effect=lambda *_a, **_kw: sequence.append("persist")))
                    ensure_ci = stack.enter_context(mock.patch.object(RUNNER, "ensure_verify_release_ci", side_effect=lambda *_a: (sequence.append("ci") or {"ci": {"state": "passed"}})))
                    load_module = stack.enter_context(mock.patch.object(RUNNER, "load_module", side_effect=AssertionError("native source refresh must not run")))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    RUNNER.execute_candidate_preparation(args, root)

                self.assertEqual(sequence, [] if prepare_only else ["persist", "ci"])
                if prepare_only:
                    persist.assert_not_called()
                    ensure_ci.assert_not_called()
                else:
                    persist.assert_called_once()
                    ensure_ci.assert_called_once()
                load_module.assert_not_called()
                self.assertFalse((root / candidate["registry_path"]).exists())
                self.assertFalse((root / "reports/latest-verification.json").exists())



class ProcessorStatePinExportTests(unittest.TestCase):
    repository = "StatPan/datapan-registry"
    old_composition_sha = "1af99ad2e3a553a9be8c99ab9aca040d4c73691f1a05d49dd0a43668c0a8a31c"
    current_composition_sha = "2e9f0b218409c0582d5d4e59c662f599bf769516c225677c5460bbd45578f9df"

    @staticmethod
    def git(path: pathlib.Path, *args: str) -> str:
        result = subprocess.run(("git", *args), cwd=path, text=True, capture_output=True, check=False)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return result.stdout.strip()

    @classmethod
    def build_state_history(cls, root: pathlib.Path) -> dict[str, object]:
        checkout = root / "processor-state-repository"
        checkout.mkdir(parents=True)
        cls.git(checkout, "init", "-q", "-b", RUNNER.PROCESSOR_STATE_BRANCH)
        cls.git(checkout, "config", "user.name", "State fixture")
        cls.git(checkout, "config", "user.email", "state-fixture@example.invalid")
        cls.git(checkout, "remote", "add", "origin", "https://github.com/StatPan/datapan-registry.git")

        maker = DurableProcessorRecoveryTests()
        old = maker.checkpoint(
            run_id="37100705274", attempt="1", observed_at="2026-09-29T00:00:00Z",
            candidate_sha256="d" * 64,
        )
        old["attempts_consumed"] = 48
        old["attempts_by_id"] = {"data.go.kr:detail": 48}
        old["detail_queue_cursor"] = 48
        old["output_artifact"].update({
            "artifact_id": "11266965270",
            "expires_at": "2026-11-02T05:52:36Z",
        })
        old["output_digests"] = [{
            "path": "composition-receipt.json",
            "sha256": cls.old_composition_sha,
            "bytes": 1,
        }]
        old["output_artifact"]["bundle_manifest_sha256"] = hashlib.sha256(
            RUNNER.canonical_json(old["output_digests"]),
        ).hexdigest()
        maker.seal(old)

        current = copy.deepcopy(old)
        current["attempts_consumed"] = 72
        current["attempts_by_id"] = {"data.go.kr:detail": 72}
        current["detail_queue_cursor"] = 72
        current["last_heartbeat_at"] = "2026-10-03T10:00:00Z"
        current["output_artifact"].update({
            "run_id": "37120180628",
            "name": "upstream-catalogue-processing-37120180628-1",
            "artifact_id": "11272800001",
        })
        current["output_digests"] = [{
            "path": "composition-receipt.json",
            "sha256": cls.current_composition_sha,
            "bytes": 1,
        }]
        current["output_artifact"]["bundle_manifest_sha256"] = hashlib.sha256(
            RUNNER.canonical_json(current["output_digests"]),
        ).hexdigest()
        maker.seal(current)

        state_root = checkout / RUNNER.PROCESSOR_STATE_ROOT.as_posix()
        source_root = state_root / "sources/data_go_kr"
        generation_root = source_root / "generations"
        generation_root.mkdir(parents=True)
        index = {
            "schema_version": RUNNER.PROCESSOR_SCHEMA,
            "generations": [{
                "generation_id": old["generation_id"],
                "status": "ready",
                "checkpoint": f"{old['generation_id']}.json",
            }],
        }
        checkpoint_path = generation_root / f"{old['generation_id']}.json"
        checkpoint_path.write_bytes(RUNNER.canonical_json(old))
        (source_root / "index.json").write_bytes(RUNNER.canonical_json(index))
        cls.git(checkout, "add", RUNNER.PROCESSOR_STATE_ROOT.as_posix())
        cls.git(checkout, "commit", "-qm", "archive original ready processor attempt")
        old_sha = cls.git(checkout, "rev-parse", "HEAD")

        checkpoint_path.write_bytes(RUNNER.canonical_json(current))
        cls.git(checkout, "add", RUNNER.PROCESSOR_STATE_ROOT.as_posix())
        cls.git(checkout, "commit", "-qm", "advance retries without changing candidate payload")
        current_sha = cls.git(checkout, "rev-parse", "HEAD")
        cls.git(checkout, "update-ref", f"refs/remotes/origin/{RUNNER.PROCESSOR_STATE_BRANCH}", current_sha)
        return {
            "checkout": checkout,
            "state_root": state_root,
            "old": old,
            "current": current,
            "old_sha": old_sha,
            "current_sha": current_sha,
        }

    def command_with_authoritative_remote(self, history: dict[str, object]):
        original = RUNNER.command
        checkout = pathlib.Path(history["checkout"])
        current_sha = str(history["current_sha"])
        ref = f"refs/heads/{RUNNER.PROCESSOR_STATE_BRANCH}"

        def run(argv, cwd, **kwargs):
            if tuple(argv) == ("git", "-C", str(checkout), "ls-remote", "--heads", "origin", ref):
                return subprocess.CompletedProcess(argv, 0, f"{current_sha}\t{ref}\n", "")
            return original(argv, cwd, **kwargs)

        return run

    def test_export_selects_original_ready_checkpoint_from_authoritative_ancestor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="processor-state-pin-export-") as raw:
            root = pathlib.Path(raw)
            history = self.build_state_history(root)
            checkout = pathlib.Path(history["checkout"])
            before_head = self.git(checkout, "rev-parse", "HEAD")
            before_ref = self.git(checkout, "rev-parse", f"refs/remotes/origin/{RUNNER.PROCESSOR_STATE_BRANCH}")
            destination = root / "export"
            fixed_now = dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc)
            with (
                mock.patch.object(RUNNER, "command", side_effect=self.command_with_authoritative_remote(history)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                pinned_root, provenance = RUNNER.export_processor_state_pin(
                    checkout, str(history["old_sha"]), self.repository, destination,
                    command_cwd=pathlib.Path(__file__).parents[1],
                )
                pinned_rows, pinned_blocked = RUNNER.list_recoverable_processor_checkpoints(
                    pinned_root,
                    pathlib.Path(__file__).parents[1] / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
                    None, now=fixed_now,
                )
                current_rows, current_blocked = RUNNER.list_recoverable_processor_checkpoints(
                    pathlib.Path(history["state_root"]),
                    pathlib.Path(__file__).parents[1] / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
                    None, now=fixed_now,
                )

            self.assertEqual(pinned_blocked, [])
            self.assertEqual(current_blocked, [])
            self.assertEqual(len(pinned_rows), 1)
            self.assertEqual(len(current_rows), 1)
            pinned = pinned_rows[0]
            current = current_rows[0]
            self.assertEqual(pinned["generation_id"], current["generation_id"])
            self.assertEqual(pinned["generation_inputs"], current["generation_inputs"])
            self.assertEqual(pinned["attempts_consumed"], 48)
            self.assertEqual(current["attempts_consumed"], 72)
            self.assertEqual(pinned["detail_queue_cursor"], 48)
            self.assertEqual(current["detail_queue_cursor"], 72)
            self.assertEqual(pinned["output_artifact"]["artifact_id"], "11266965270")
            self.assertEqual(current["output_artifact"]["artifact_id"], "11272800001")
            self.assertEqual(pinned["output_digests"][0]["sha256"], self.old_composition_sha)
            self.assertEqual(current["output_digests"][0]["sha256"], self.current_composition_sha)
            self.assertEqual(provenance["processor_state_sha"], history["old_sha"])
            self.assertEqual(provenance["branch_head_sha"], history["current_sha"])
            self.assertEqual(before_head, self.git(checkout, "rev-parse", "HEAD"))
            self.assertEqual(before_ref, self.git(checkout, "rev-parse", f"refs/remotes/origin/{RUNNER.PROCESSOR_STATE_BRANCH}"))
            self.assertFalse((pinned_root / ".git").exists())

    def test_export_rejects_nonancestor_wrong_repository_and_tracking_ref_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="processor-state-pin-reject-") as raw:
            root = pathlib.Path(raw)
            history = self.build_state_history(root)
            checkout = pathlib.Path(history["checkout"])
            fake_remote = self.command_with_authoritative_remote(history)
            with mock.patch.object(RUNNER, "command", side_effect=fake_remote), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RUNNER.PromotionError, "full immutable"):
                    RUNNER.export_processor_state_pin(checkout, "bad-pin", self.repository, root / "bad", command_cwd=root)
                with self.assertRaisesRegex(RUNNER.PromotionError, "differs from GITHUB_REPOSITORY"):
                    RUNNER.export_processor_state_pin(checkout, str(history["old_sha"]), "Other/repo", root / "wrong-repo", command_cwd=root)

                tree = self.git(checkout, "write-tree")
                unrelated = self.git(checkout, "commit-tree", tree, "-m", "unrelated state commit")
                with self.assertRaisesRegex(RUNNER.PromotionError, "not an ancestor"):
                    RUNNER.export_processor_state_pin(checkout, unrelated, self.repository, root / "unrelated", command_cwd=root)

                self.git(checkout, "update-ref", f"refs/remotes/origin/{RUNNER.PROCESSOR_STATE_BRANCH}", str(history["old_sha"]))
                with self.assertRaisesRegex(RUNNER.PromotionError, "stale relative"):
                    RUNNER.export_processor_state_pin(checkout, str(history["old_sha"]), self.repository, root / "stale", command_cwd=root)


class TrustedSourceRefreshEntryPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old, _new = OwnedPRRefreshRecoveryTests().receipts()
        self.old["candidate"]["composition_receipt_sha256"] = "f" * 64
        self.old["candidate"]["base_sha"] = "d" * 40
        self.target_main = "e" * 40
        self.state_sha = "9" * 40
        self.repository = "StatPan/datapan-registry"
        self.checkpoint = {
            "generation_id": self.old["candidate"]["generation_id"],
            "source_id": "data_go_kr", "source_scope": "aggregate_supported_catalog",
            "status": "ready",
            "output_artifact": {"artifact_id": "1234", "run_id": "37100705274"},
        }
        self.run = {"head_sha": "a" * 40}
        self.bundle = {
            "status": "ready", "registry_sha256": self.old["candidate"]["registry_sha256"],
            "composition_receipt_sha256": self.old["candidate"]["composition_receipt_sha256"],
            "composition_receipt": {"schema_version": "test"},
        }
        self.args = types.SimpleNamespace(
            refresh_pr_number=self.old["pr"]["number"],
            expected_predecessor_head_sha=self.old["candidate"]["head_sha"],
            expected_predecessor_body_sha256=self.old["ownership"]["body_sha256"],
            expected_predecessor_manifest_sha256=self.old["candidate"]["manifest_sha256"],
            target_main_sha=self.target_main,
            state_root=pathlib.Path("state"), datapan_cli=pathlib.Path("cli"),
            prepare_only=False,
        )

    def run_with_read_only_stubs(self, *, command_sha: str | None = None) -> tuple[mock._patch, mock._patch, mock._patch]:
        environment = mock.patch.dict(os.environ, {
            "GITHUB_REPOSITORY": self.repository,
            "GITHUB_DEFAULT_BRANCH": "main",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
        })
        environment.start()
        self.addCleanup(environment.stop)
        command_patch = mock.patch.object(
            RUNNER, "command",
            return_value=subprocess.CompletedProcess(["git"], 0, stdout=f"{command_sha or self.target_main}\n", stderr=""),
        )
        command_patch.start()
        self.addCleanup(command_patch.stop)
        main_patch = mock.patch.object(RUNNER, "assert_remote_main_sha")
        main_patch.start()
        self.addCleanup(main_patch.stop)
        return environment, command_patch, main_patch

    def test_refresh_resolves_exact_adopted_receipt_and_replays_same_b_payload(self) -> None:
        self.run_with_read_only_stubs()
        with (
            mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=PR_HELPER),
            mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=({"records": [self.old]}, self.state_sha)),
            mock.patch.object(RUNNER, "gh_pr_readback", return_value={}) as readback,
            mock.patch.object(RUNNER, "validate_exact_open_pr") as validate_pr,
            mock.patch.object(RUNNER, "list_recoverable_processor_checkpoints", return_value=([self.checkpoint], [])),
            mock.patch.object(RUNNER, "processor_attempt_from_locator", return_value=("37100705274", "1", "upstream-catalogue-processing-37100705274-1")),
            mock.patch.object(RUNNER, "processor_run_api", return_value={}) as run_api,
            mock.patch.object(RUNNER, "validate_trusted_processor_run", return_value=self.run),
            mock.patch.object(RUNNER, "processor_artifact_api", return_value={"id": "1234"}) as artifact_api,
            mock.patch.object(RUNNER, "validate_processor_artifact_metadata", return_value={"id": "1234"}),
            mock.patch.object(RUNNER, "download_processor_artifact", return_value=pathlib.Path("bundle")) as download,
            mock.patch.object(RUNNER, "load_object", return_value={}),
            mock.patch.object(RUNNER, "validate_processor_bundle", return_value=self.bundle),
            mock.patch.object(RUNNER, "verify_processor_input_compatibility") as compatibility,
            mock.patch.object(RUNNER, "execute_candidate_preparation") as execute,
        ):
            RUNNER.run_source_refresh(self.args, pathlib.Path("."))

        validate_pr.assert_called_once_with(self.old, {}, PR_HELPER)
        readback.assert_called_once_with(pathlib.Path("."), self.repository, self.old["pr"]["number"])
        run_api.assert_called_once()
        artifact_api.assert_called_once()
        download.assert_called_once()
        compatibility.assert_called_once()
        execute.assert_called_once()
        self.assertEqual(self.args.source_refresh_predecessor["candidate"]["generation_id"], self.checkpoint["generation_id"])
        self.assertEqual(self.args.source_refresh_expected_state_sha, self.state_sha)
        self.assertEqual(self.args.source_refresh_target_main_sha, self.target_main)

    def test_refresh_rejects_partial_or_changed_predecessor_before_b_api_reads(self) -> None:
        self.run_with_read_only_stubs()
        self.args.expected_predecessor_body_sha256 = "0" * 64
        with (
            mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=PR_HELPER),
            mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=({"records": [self.old]}, self.state_sha)),
            mock.patch.object(RUNNER, "processor_run_api") as run_api,
        ):
            with self.assertRaisesRegex(RUNNER.PromotionError, "does not identify exactly one adopted predecessor"):
                RUNNER.run_source_refresh(self.args, pathlib.Path("."))
        run_api.assert_not_called()

        self.args.expected_predecessor_body_sha256 = ""
        with self.assertRaisesRegex(RUNNER.PromotionError, "body must be a full SHA-256"):
            RUNNER.run_source_refresh(self.args, pathlib.Path("."))

    def test_processor_state_pin_alone_routes_to_refresh_and_fails_missing_five_inputs(self) -> None:
        self.run_with_read_only_stubs()
        self.args.refresh_pr_number = None
        self.args.processor_state_sha = "a" * 40
        self.args.processor_state_repo = pathlib.Path("processor-state")
        with mock.patch.object(RUNNER, "export_processor_state_pin") as export:
            with self.assertRaisesRegex(RUNNER.PromotionError, "positive predecessor PR number"):
                RUNNER.run_source_refresh(self.args, pathlib.Path("."))
        export.assert_not_called()

    def test_pin_uses_same_historical_export_for_selection_and_full_preparation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pinned-source-refresh-flow-") as raw:
            temp_root = pathlib.Path(raw)
            history = ProcessorStatePinExportTests.build_state_history(temp_root)
            checkout = pathlib.Path(history["checkout"])
            state_root = pathlib.Path(history["state_root"])
            old_checkpoint = history["old"]
            old_composition_sha = ProcessorStatePinExportTests.old_composition_sha
            original_head = ProcessorStatePinExportTests.git(checkout, "rev-parse", "HEAD")

            self.old["candidate"]["generation_id"] = old_checkpoint["generation_id"]
            self.old["candidate"]["registry_sha256"] = old_checkpoint["generation_inputs"]["candidate_sha256"]
            self.old["candidate"]["composition_receipt_sha256"] = old_composition_sha
            journal = {"records": [self.old]}
            self.args.state_root = state_root
            self.args.processor_state_sha = str(history["old_sha"])
            self.args.processor_state_repo = checkout

            ready_bundle = {
                "status": "ready",
                "generation_id": old_checkpoint["generation_id"],
                "registry_sha256": self.old["candidate"]["registry_sha256"],
                "composition_receipt_sha256": old_composition_sha,
                "composition_receipt": {"schema_version": "fixture"},
            }
            retry_bundle = {"status": "retry", "reason": "fixture_no_candidate"}
            root = pathlib.Path(__file__).parents[1].resolve()
            original_command = RUNNER.command
            branch_ref = f"refs/heads/{RUNNER.PROCESSOR_STATE_BRANCH}"
            selected_roots: list[pathlib.Path] = []
            execute_roots: list[pathlib.Path] = []

            def command_proxy(argv, cwd, **kwargs):
                values = tuple(argv)
                if values == ("git", "rev-parse", "HEAD") and pathlib.Path(cwd).resolve() == root:
                    return subprocess.CompletedProcess(values, 0, f"{self.target_main}\n", "")
                if values == ("git", "-C", str(checkout), "ls-remote", "--heads", "origin", branch_ref):
                    return subprocess.CompletedProcess(values, 0, f"{history['current_sha']}\t{branch_ref}\n", "")
                return original_command(values, cwd, **kwargs)

            original_list = RUNNER.list_recoverable_processor_checkpoints

            def list_proxy(selected_root, *args, **kwargs):
                selected_roots.append(pathlib.Path(selected_root))
                self.assertTrue(pathlib.Path(selected_root).is_dir(), "the selected state export exists during checkpoint selection")
                return original_list(pathlib.Path(selected_root), *args, **kwargs)

            original_locate = RUNNER.locate_processor_checkpoint

            def locate_proxy(selected_root, *args, **kwargs):
                execute_roots.append(pathlib.Path(selected_root))
                self.assertTrue(pathlib.Path(selected_root).is_dir(), "the same export remains available during full candidate preparation")
                return original_locate(pathlib.Path(selected_root), *args, **kwargs)

            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {
                    "GITHUB_REPOSITORY": self.repository,
                    "GITHUB_DEFAULT_BRANCH": "main",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_RUN_ID": "37130000001",
                    "GITHUB_RUN_ATTEMPT": "1",
                }),
                mock.patch.object(RUNNER, "command", side_effect=command_proxy),
                mock.patch.object(RUNNER, "assert_remote_main_sha"),
                mock.patch.object(RUNNER, "assert_predecessor_base_is_ancestor"),
                mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=PR_HELPER),
                mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=(journal, self.state_sha)),
                mock.patch.object(RUNNER, "gh_pr_readback", return_value={"headRefOid": self.old["candidate"]["head_sha"]}),
                mock.patch.object(RUNNER, "validate_exact_open_pr"),
                mock.patch.object(RUNNER, "list_recoverable_processor_checkpoints", side_effect=list_proxy),
                mock.patch.object(RUNNER, "processor_run_api", return_value={"id": 37100705274}),
                mock.patch.object(RUNNER, "validate_trusted_processor_run", return_value={"head_sha": "a" * 40}),
                mock.patch.object(RUNNER, "processor_artifact_api", return_value={"id": "11266965270"}) as artifact_api,
                mock.patch.object(RUNNER, "validate_processor_artifact_metadata", return_value={"id": "11266965270"}),
                mock.patch.object(RUNNER, "download_processor_artifact", return_value=temp_root / "bundle"),
                mock.patch.object(RUNNER, "validate_processor_bundle", side_effect=(ready_bundle, retry_bundle)),
                mock.patch.object(RUNNER, "verify_processor_input_compatibility"),
                mock.patch.object(RUNNER, "validate_no_candidate_processor_result", return_value=None),
                mock.patch.object(RUNNER, "locate_processor_checkpoint", side_effect=locate_proxy),
                contextlib.redirect_stdout(output),
            ):
                (temp_root / "bundle").mkdir()
                RUNNER.run_source_refresh(self.args, root)

            self.assertEqual(len(selected_roots), 1)
            self.assertEqual(len(execute_roots), 1)
            self.assertEqual(selected_roots[0], execute_roots[0])
            self.assertTrue(selected_roots[0].is_absolute())
            self.assertFalse(selected_roots[0].exists(), "the temporary export is removed after full preparation")
            self.assertEqual(self.args.state_root, state_root, "the caller's current state path is restored")
            self.assertEqual(artifact_api.call_args.args[2], "37100705274")
            self.assertEqual(artifact_api.call_args.args[3], "11266965270")
            self.assertEqual(ProcessorStatePinExportTests.git(checkout, "rev-parse", "HEAD"), original_head)
            self.assertEqual(ProcessorStatePinExportTests.git(checkout, "rev-parse", f"refs/remotes/origin/{RUNNER.PROCESSOR_STATE_BRANCH}"), original_head)
            pin_logs = [
                json.loads(line) for line in output.getvalue().splitlines()
                if line.startswith("{") and json.loads(line).get("event") == "processor_state_pin_selected"
            ]
            self.assertEqual(len(pin_logs), 1, output.getvalue())
            pin_log = pin_logs[0]
            self.assertEqual(pin_log["processor_state_sha"], history["old_sha"])
            self.assertEqual(pin_log["branch_head_sha"], history["current_sha"])
            self.assertEqual(pin_log["checkpoint_sha256"], hashlib.sha256(RUNNER.canonical_json(old_checkpoint)).hexdigest())
            self.assertEqual(pin_log["processor_run_id"], "37100705274")
            self.assertEqual(pin_log["run_attempt"], "1")
            self.assertEqual(pin_log["artifact_id"], "11266965270")
            self.assertEqual(pin_log["artifact_expires_at"], "2026-11-02T05:52:36Z")

    def test_pin_export_is_cleaned_and_caller_state_restored_after_preparation_failure(self) -> None:
        self.run_with_read_only_stubs()
        self.args.processor_state_sha = "a" * 40
        self.args.processor_state_repo = pathlib.Path("processor-state")
        original_state_root = self.args.state_root
        exported_paths: list[pathlib.Path] = []

        def export(_checkout, _pin, _repository, destination, *, command_cwd):
            self.assertIsInstance(command_cwd, pathlib.Path)
            destination.mkdir(parents=True)
            exported_paths.append(destination)
            return destination, {
                "repository": self.repository,
                "branch_ref": f"refs/heads/{RUNNER.PROCESSOR_STATE_BRANCH}",
                "branch_head_sha": "b" * 40,
                "processor_state_sha": "a" * 40,
            }

        def fail_preparation(args, _root, *_extra):
            self.assertEqual(args.state_root, exported_paths[0])
            self.assertTrue(args.state_root.is_dir())
            raise RUNNER.PromotionError("fixture preparation failure")

        with (
            mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=PR_HELPER),
            mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=({"records": [self.old]}, self.state_sha)),
            mock.patch.object(RUNNER, "gh_pr_readback", return_value={}),
            mock.patch.object(RUNNER, "validate_exact_open_pr"),
            mock.patch.object(RUNNER, "export_processor_state_pin", side_effect=export) as export_call,
            mock.patch.object(RUNNER, "prepare_source_refresh_candidate", side_effect=fail_preparation) as prepare,
        ):
            with self.assertRaisesRegex(RUNNER.PromotionError, "fixture preparation failure"):
                RUNNER.run_source_refresh(self.args, pathlib.Path("."))

        export_call.assert_called_once()
        prepare.assert_called_once()
        self.assertEqual(self.args.state_root, original_state_root)
        self.assertEqual(len(exported_paths), 1)
        self.assertFalse(exported_paths[0].exists(), "TemporaryDirectory cleanup runs on the failure path")

    def test_no_pin_still_rejects_advanced_composition_before_preparation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="unpinned-source-refresh-flow-") as raw:
            temp_root = pathlib.Path(raw)
            history = ProcessorStatePinExportTests.build_state_history(temp_root)
            checkout = pathlib.Path(history["checkout"])
            state_root = pathlib.Path(history["state_root"])
            current_checkpoint = history["current"]
            self.old["candidate"]["generation_id"] = current_checkpoint["generation_id"]
            self.old["candidate"]["registry_sha256"] = current_checkpoint["generation_inputs"]["candidate_sha256"]
            self.old["candidate"]["composition_receipt_sha256"] = ProcessorStatePinExportTests.old_composition_sha
            self.args.state_root = state_root
            self.args.processor_state_sha = ""
            self.args.processor_state_repo = checkout
            current_bundle = {
                "status": "ready",
                "generation_id": current_checkpoint["generation_id"],
                "registry_sha256": self.old["candidate"]["registry_sha256"],
                "composition_receipt_sha256": ProcessorStatePinExportTests.current_composition_sha,
                "composition_receipt": {"schema_version": "fixture"},
            }
            state_head = ProcessorStatePinExportTests.git(checkout, "rev-parse", "HEAD")
            self.run_with_read_only_stubs()
            with (
                mock.patch.object(RUNNER, "assert_predecessor_base_is_ancestor"),
                mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=PR_HELPER),
                mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=({"records": [self.old]}, self.state_sha)),
                mock.patch.object(RUNNER, "gh_pr_readback", return_value={"headRefOid": self.old["candidate"]["head_sha"]}),
                mock.patch.object(RUNNER, "validate_exact_open_pr"),
                mock.patch.object(RUNNER, "processor_run_api", return_value={}),
                mock.patch.object(RUNNER, "validate_trusted_processor_run", return_value={"head_sha": "a" * 40}),
                mock.patch.object(RUNNER, "processor_artifact_api", return_value={"id": "11272800001"}),
                mock.patch.object(RUNNER, "validate_processor_artifact_metadata", return_value={"id": "11272800001"}),
                mock.patch.object(RUNNER, "download_processor_artifact", return_value=temp_root / "bundle"),
                mock.patch.object(RUNNER, "validate_processor_bundle", return_value=current_bundle),
                mock.patch.object(RUNNER, "verify_processor_input_compatibility"),
                mock.patch.object(RUNNER, "export_processor_state_pin") as export,
                mock.patch.object(RUNNER, "execute_candidate_preparation") as execute,
            ):
                with self.assertRaisesRegex(RUNNER.PromotionError, "does not reproduce the exact reviewed generation and payload"):
                    RUNNER.run_source_refresh(self.args, pathlib.Path(__file__).parents[1])

            export.assert_not_called()
            execute.assert_not_called()
            self.assertEqual(ProcessorStatePinExportTests.git(checkout, "rev-parse", "HEAD"), state_head)

    def test_refresh_requires_predecessor_base_ancestor_before_pr_or_b_reads(self) -> None:
        self.run_with_read_only_stubs()
        command_results = iter((
            subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, f"{self.target_main}\n", ""),
            subprocess.CompletedProcess(["git", "merge-base", "--is-ancestor"], 1, "", ""),
        ))
        with (
            mock.patch.object(RUNNER, "command", side_effect=lambda *_a, **_kw: next(command_results)),
            mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=PR_HELPER),
            mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=({"records": [self.old]}, self.state_sha)),
            mock.patch.object(RUNNER, "gh_pr_readback") as readback,
            mock.patch.object(RUNNER, "processor_run_api") as run_api,
        ):
            with self.assertRaisesRegex(RUNNER.PromotionError, "not a descendant of the predecessor candidate base"):
                RUNNER.run_source_refresh(self.args, pathlib.Path("."))

        readback.assert_not_called()
        run_api.assert_not_called()

    def test_new_target_cannot_retarget_an_existing_pre_push_intent(self) -> None:
        self.run_with_read_only_stubs()
        predecessor, intent = OwnedPRRefreshRecoveryTests().receipts()
        predecessor["candidate"]["composition_receipt_sha256"] = self.old["candidate"]["composition_receipt_sha256"]
        predecessor["candidate"]["base_sha"] = "d" * 40
        intent["candidate"]["generation_id"] = predecessor["candidate"]["generation_id"]
        intent["candidate"]["registry_sha256"] = predecessor["candidate"]["registry_sha256"]
        intent["candidate"]["composition_receipt_sha256"] = predecessor["candidate"]["composition_receipt_sha256"]
        intent["candidate"]["base_sha"] = "a" * 40
        intent["refresh_from"] = PR_HELPER.revision_reference(predecessor)
        intent["refresh_target_main_sha"] = "a" * 40
        self.args.target_main_sha = "e" * 40
        journal = {"records": [predecessor, intent]}
        with (
            mock.patch.object(RUNNER, "load_canonical_update_pr", return_value=PR_HELPER),
            mock.patch.object(RUNNER, "load_promotion_journal_snapshot", return_value=(journal, self.state_sha)),
            mock.patch.object(RUNNER, "gh_pr_readback") as readback,
            mock.patch.object(RUNNER, "processor_run_api") as run_api,
        ):
            with self.assertRaisesRegex(RUNNER.PromotionError, "conflicts with the requested target or B payload"):
                RUNNER.run_source_refresh(self.args, pathlib.Path("."))
        readback.assert_not_called()
        run_api.assert_not_called()

    def test_refresh_requires_current_main_and_trusted_default_branch_ref(self) -> None:
        self.run_with_read_only_stubs(command_sha="0" * 40)
        with mock.patch.object(RUNNER, "assert_remote_main_sha") as remote_check:
            with self.assertRaisesRegex(RUNNER.PromotionError, "checkout does not equal"):
                RUNNER.run_source_refresh(self.args, pathlib.Path("."))
        remote_check.assert_not_called()

        self.run_with_read_only_stubs()
        with mock.patch.dict(os.environ, {"GITHUB_REF": "refs/heads/topic"}):
            with self.assertRaisesRegex(RUNNER.PromotionError, "trusted default branch"):
                RUNNER.run_source_refresh(self.args, pathlib.Path("."))

        self.run_with_read_only_stubs()
        with (
            mock.patch.object(RUNNER, "assert_remote_main_sha", side_effect=RUNNER.PromotionError("main moved")) as remote_check,
            mock.patch.object(RUNNER, "load_promotion_journal_snapshot") as journal_read,
        ):
            with self.assertRaisesRegex(RUNNER.PromotionError, "main moved"):
                RUNNER.run_source_refresh(self.args, pathlib.Path("."))
        remote_check.assert_called_once_with(pathlib.Path("."), self.target_main)
        journal_read.assert_not_called()


class RecoveredPendingExpectedHeadIntegrationTests(unittest.TestCase):
    fixture_path = pathlib.Path(__file__).parent / "fixtures/canonical-update-promotion/attempt-4-pr-686-pending-recovery.json"

    def fixture(self) -> dict:
        return json.loads(self.fixture_path.read_text(encoding="utf-8"))

    def _run_recovery(self, fixture: dict) -> tuple[dict, dict, list[dict], list[tuple[str, str, object]], int, int, str]:
        journal = copy.deepcopy(fixture["state"])
        state_sha = "a" * 40
        original = copy.deepcopy(journal["records"][0])
        candidate = original["candidate"]
        ownership = original["ownership"]
        repo, branch, head = candidate["repository"], ownership["branch"], candidate["head_sha"]
        current_main = fixture["provenance"]["workflow_checkout_sha"]
        schema = json.loads((SCRIPT.parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text(encoding="utf-8"))
        PR_HELPER.validate_journal(journal, schema)
        writes: list[dict] = []
        api_calls: list[tuple[str, str, object]] = []
        dispatch_count = 0
        run_id = 9901

        def current_receipt() -> dict:
            rows = [row for row in journal["records"] if PR_HELPER.candidate_key(row) == PR_HELPER.candidate_key(original)]
            self.assertEqual(len(rows), 1)
            return rows[0]

        def persist_journal(
            _root: pathlib.Path,
            _base: str,
            receipt: dict,
            *,
            observed_at: str,
            expected_ci: object = RUNNER.CI_EXPECTATION_UNSET,
            expected_state_sha: object = RUNNER.STATE_EXPECTATION_UNSET,
            supersede_from: dict | None = None,
        ) -> None:
            nonlocal journal, state_sha
            if expected_state_sha is not RUNNER.STATE_EXPECTATION_UNSET:
                self.assertEqual(expected_state_sha, state_sha)
            if expected_ci is not RUNNER.CI_EXPECTATION_UNSET:
                PR_HELPER.assert_ci_compare_and_swap(journal, receipt, expected_ci)
            journal = PR_HELPER.append_journal_record(
                journal, receipt, repository=repo, observed_at=observed_at,
                supersede_from=supersede_from,
            )
            PR_HELPER.validate_journal(journal, schema)
            state_sha = hashlib.sha1(json.dumps(journal, sort_keys=True).encode("utf-8")).hexdigest()
            writes.append(copy.deepcopy(receipt))

        run = {
            "id": run_id,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "head_sha": head,
            "head_branch": branch,
            "path": ".github/workflows/verify-release.yml",
            "repository": {"full_name": repo},
            "head_repository": {"full_name": repo},
            "status": "completed",
            "conclusion": "success",
            "html_url": f"https://github.com/{repo}/actions/runs/{run_id}",
        }
        jobs = {
            "total_count": 2,
            "jobs": [
                {"job_id": "diagnostic-candidate", "name": "Diagnostic candidate (pre-distribution)", "status": "completed", "conclusion": "success"},
                {"job_id": "verify", "name": "verify", "status": "completed", "conclusion": "success"},
            ],
        }

        def api_request(method: str, endpoint: str, body: object = None) -> tuple[int, object]:
            nonlocal dispatch_count
            api_calls.append((method, endpoint, copy.deepcopy(body)))
            if method == "GET" and "/git/ref/heads/" in endpoint:
                return 200, {"object": {"sha": head}}
            if method == "GET" and "/actions/workflows/" in endpoint and "/runs?" in endpoint:
                return 200, {"total_count": 0, "workflow_runs": []}
            if method == "POST" and endpoint.endswith("/dispatches"):
                dispatch_count += 1
                durable = current_receipt()
                self.assertEqual(durable["status"], "pending-review")
                self.assertEqual(durable["ownership"]["expected_head_sha"], head)
                self.assertEqual(durable["pr"]["number"], 686)
                self.assertEqual(durable["acknowledgements"], original["acknowledgements"])
                self.assertEqual(durable["candidate"], original["candidate"])
                self.assertEqual(durable["ownership"]["body"], original["ownership"]["body"])
                self.assertEqual(durable.get("ci", {}).get("state"), "intent")
                self.assertEqual(body, {"ref": branch, "inputs": {"expected_head_sha": head}})
                return 200, {"workflow_run_id": run_id, "run_url": run["html_url"]}
            if method == "GET" and f"/actions/runs/{run_id}/attempts/1/jobs?per_page=100" in endpoint:
                return 200, jobs
            if method == "GET" and endpoint.endswith(f"/actions/runs/{run_id}"):
                return 200, copy.deepcopy(run)
            raise AssertionError(f"unexpected GitHub API request: {method} {endpoint}")

        def load_module(path: pathlib.Path, name: str) -> object:
            if path.name == "canonical_update_pr.py":
                return PR_HELPER
            if path.name == "canonical_update_ci.py":
                return CI_HELPER
            raise AssertionError(f"unexpected dynamic helper import: {path} as {name}")

        def command(argv: tuple[str, ...], _root: pathlib.Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ("git", "rev-parse", "HEAD"):
                return subprocess.CompletedProcess(argv, 0, current_main + "\n", "")
            raise AssertionError(f"unexpected command during recovery: {argv}")

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as raw, contextlib.ExitStack() as stack:
            root = pathlib.Path(raw)
            stack.enter_context(mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": repo,
                "GITHUB_RUN_ID": "37101245239",
                "GITHUB_RUN_ATTEMPT": "5",
            }))
            stack.enter_context(mock.patch.object(RUNNER, "load_module", side_effect=load_module))
            stack.enter_context(mock.patch.object(
                RUNNER, "load_promotion_journal_snapshot",
                side_effect=lambda _root: (copy.deepcopy(journal), state_sha),
            ))
            stack.enter_context(mock.patch.object(RUNNER, "persist_journal_record", side_effect=persist_journal))
            stack.enter_context(mock.patch.object(RUNNER, "command", side_effect=command))
            stack.enter_context(mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]))
            stack.enter_context(mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]))
            stack.enter_context(mock.patch.object(PR_HELPER, "load_materializer", return_value=object()))
            stack.enter_context(mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=head))
            stack.enter_context(mock.patch.object(RUNNER, "github_api_request", side_effect=api_request))
            ci_call = stack.enter_context(mock.patch.object(CI_HELPER, "ensure_verify_release_run", wraps=CI_HELPER.ensure_verify_release_run))
            stack.enter_context(contextlib.redirect_stdout(output))
            RUNNER.reconcile_open_promotions(root)

        updated = current_receipt()
        return updated, original, writes, api_calls, dispatch_count, ci_call.call_count, output.getvalue()

    def test_actual_pending_receipt_binds_verified_head_before_real_ci_dispatch(self) -> None:
        fixture = self.fixture()
        updated, original, writes, api_calls, dispatch_count, ci_call_count, output = self._run_recovery(fixture)
        candidate = original["candidate"]
        head = candidate["head_sha"]
        expected_without_ci = copy.deepcopy(original)
        expected_without_ci["ownership"]["expected_head_sha"] = head
        expected_without_ci["pr"]["merge_commit_sha"] = fixture["pull_request_readback"]["mergeCommit"]["oid"]
        updated_without_ci = copy.deepcopy(updated)
        updated_without_ci.pop("ci")
        self.assertEqual(dispatch_count, 1, output)
        self.assertEqual(updated_without_ci, expected_without_ci)
        self.assertEqual(updated["status"], "pending-review")
        self.assertEqual(updated["pr"]["number"], 686)
        self.assertEqual(updated["ownership"]["expected_head_sha"], head)
        self.assertEqual(updated["acknowledgements"], original["acknowledgements"])
        self.assertEqual(updated["candidate"], original["candidate"])
        self.assertEqual(updated["checks"], original["checks"])
        self.assertEqual(updated["source_refresh_evidence"], original["source_refresh_evidence"])
        self.assertEqual(updated["validation_evidence"], original["validation_evidence"])
        self.assertEqual(updated["candidate"]["payload_readback"], original["candidate"]["payload_readback"])
        self.assertEqual(updated["ci"]["state"], "success")
        self.assertEqual(updated["ci"]["head_sha"], head)
        self.assertEqual(updated["ci"]["run_id"], 9901)
        self.assertEqual(updated["ci"]["run_attempt"], 1)
        self.assertEqual(len([ack for ack in updated["acknowledgements"] if ack["status"] == "pending-review"]), 1)
        self.assertEqual(ci_call_count, 1)
        self.assertGreaterEqual(len(writes), 4)
        self.assertTrue(any(write.get("ci", {}).get("state") == "intent" for write in writes))

    def test_legacy_bare_and_replacement_branches_recover_through_real_ci_caller(self) -> None:
        for action in ("create", "create_replacement"):
            with self.subTest(action=action):
                fixture = self.fixture()
                record = fixture["state"]["records"][0]
                candidate = record["candidate"]
                generated = PR_HELPER.automation_branch(candidate, "create")
                prefix = generated[:-21]
                if action == "create":
                    branch = prefix
                else:
                    generation_hash = hashlib.sha256(candidate["generation_id"].encode("utf-8")).hexdigest()[:10]
                    branch = f"{prefix}-replacement-{generation_hash}"
                record["action"] = action
                record["ownership"]["branch"] = branch
                record["ownership"]["expected_head_sha"] = candidate["head_sha"]
                fixture["open_pr_rows"][0]["head_ref"] = branch
                fixture["pull_request_readback"]["headRefName"] = branch

                updated, original, _writes, _api_calls, dispatch_count, ci_call_count, output = self._run_recovery(fixture)
                self.assertEqual(dispatch_count, 1, output)
                self.assertEqual(ci_call_count, 1)
                self.assertEqual(updated["ownership"]["branch"], branch)
                self.assertEqual(updated["ownership"]["expected_head_sha"], candidate["head_sha"])
                self.assertEqual(updated["acknowledgements"], original["acknowledgements"])
                self.assertEqual(updated["ci"]["state"], "success")

    def test_unbound_revision_branch_fails_in_caller_before_journal_or_ci_write(self) -> None:
        fixture = self.fixture()
        receipt = fixture["state"]["records"][0]
        candidate = receipt["candidate"]
        generated = PR_HELPER.automation_branch(candidate, "create")
        bad_branch = generated + "-unbound"
        receipt["ownership"]["branch"] = bad_branch
        receipt["ownership"]["expected_head_sha"] = candidate["head_sha"]
        fixture["open_pr_rows"][0]["head_ref"] = bad_branch
        fixture["pull_request_readback"]["headRefName"] = bad_branch
        current_main = fixture["provenance"]["workflow_checkout_sha"]
        api_calls: list[tuple[str, str]] = []

        def load_module(path: pathlib.Path, name: str) -> object:
            if path.name == "canonical_update_pr.py":
                return PR_HELPER
            if path.name == "canonical_update_ci.py":
                return CI_HELPER
            raise AssertionError(f"unexpected dynamic helper import: {path} as {name}")

        def command(argv: tuple[str, ...], _root: pathlib.Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ("git", "rev-parse", "HEAD"):
                return subprocess.CompletedProcess(argv, 0, current_main + "\n", "")
            raise AssertionError(f"unexpected command during recovery: {argv}")

        with tempfile.TemporaryDirectory() as raw, contextlib.ExitStack() as stack:
            root = pathlib.Path(raw)
            stack.enter_context(mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": candidate["repository"],
                "GITHUB_RUN_ID": "37101245239",
                "GITHUB_RUN_ATTEMPT": "5",
            }))
            stack.enter_context(mock.patch.object(RUNNER, "load_module", side_effect=load_module))
            stack.enter_context(mock.patch.object(
                RUNNER, "load_promotion_journal_snapshot",
                return_value=(copy.deepcopy(fixture["state"]), "a" * 40),
            ))
            persist = stack.enter_context(mock.patch.object(RUNNER, "persist_journal_record"))
            stack.enter_context(mock.patch.object(RUNNER, "command", side_effect=command))
            stack.enter_context(mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]))
            stack.enter_context(mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]))
            stack.enter_context(mock.patch.object(PR_HELPER, "load_materializer", return_value=object()))
            stack.enter_context(mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=candidate["head_sha"]))
            stack.enter_context(mock.patch.object(RUNNER, "github_api_request", side_effect=lambda method, endpoint, _body=None: (api_calls.append((method, endpoint)) or (500, None))))
            ci_call = stack.enter_context(mock.patch.object(CI_HELPER, "ensure_verify_release_run", wraps=CI_HELPER.ensure_verify_release_run))
            with self.assertRaisesRegex(RUNNER.PromotionError, "not an exact canonical or inherited owned branch"):
                RUNNER.reconcile_open_promotions(root)

        persist.assert_not_called()
        ci_call.assert_not_called()
        self.assertFalse(any(method == "POST" for method, _endpoint in api_calls))

    def test_unexpected_nonzero_expected_head_is_rejected_before_persistence(self) -> None:
        invalid_receipts = []
        fixture = self.fixture()
        wrong_head = copy.deepcopy(fixture["state"]["records"][0])
        wrong_head["ownership"]["expected_head_sha"] = "a" * 40
        invalid_receipts.append((wrong_head, "cannot normalize an arbitrary ownership head"))
        terminal = copy.deepcopy(fixture["state"]["records"][0])
        terminal["status"] = "merged"
        invalid_receipts.append((terminal, "cannot bind a refresh, superseded, or terminal receipt"))
        refresh = copy.deepcopy(fixture["state"]["records"][0])
        refresh["action"] = "refresh_owned"
        refresh["refresh_from"] = {"generation_id": "prior"}
        invalid_receipts.append((refresh, "cannot bind a refresh, superseded, or terminal receipt"))
        superseded = copy.deepcopy(fixture["state"]["records"][0])
        superseded["superseded_by"] = {"generation_id": "later"}
        invalid_receipts.append((superseded, "cannot bind a refresh, superseded, or terminal receipt"))

        with mock.patch.object(RUNNER, "persist_journal_record") as persist:
            for receipt, expected_error in invalid_receipts:
                with self.subTest(status=receipt["status"], action=receipt["action"]):
                    with self.assertRaisesRegex(RUNNER.PromotionError, expected_error):
                        RUNNER.bind_verified_recovery_head(receipt)
        persist.assert_not_called()

    def test_pending_review_recovery_rejects_resealed_or_mismatched_attempt_witness(self) -> None:
        fixture = self.fixture()
        receipt = fixture["state"]["records"][0]
        receipt["acknowledgements"][0]["run_attempt"] = 5
        candidate = receipt["candidate"]
        with self.assertRaisesRegex(RUNNER.PromotionError, "one exact immutable head/manifest/artifact/run-attempt acknowledgement"):
            RUNNER.validate_prepared_create_pr_readback(
                receipt, fixture["open_pr_rows"], fixture["pull_request_readback"],
                fixture["remote_branch_sha"], PR_HELPER,
                repository=candidate["repository"],
                source_id=candidate["source_id"], scope=candidate["scope"],
                generation_id=candidate["generation_id"],
                registry_path=candidate["registry_path"], registry_bytes=candidate["registry_bytes"],
                registry_sha256=candidate["registry_sha256"],
                composition_receipt_sha256=candidate["composition_receipt_sha256"],
            )

    def test_failed_state_cas_stops_before_real_ci_helper_or_dispatch(self) -> None:
        fixture = self.fixture()
        record = fixture["state"]["records"][0]
        repo = record["candidate"]["repository"]
        current_main = fixture["provenance"]["workflow_checkout_sha"]
        api_calls: list[tuple[str, str]] = []

        def load_module(path: pathlib.Path, name: str) -> object:
            if path.name == "canonical_update_pr.py":
                return PR_HELPER
            if path.name == "canonical_update_ci.py":
                return CI_HELPER
            raise AssertionError(f"unexpected dynamic helper import: {path} as {name}")

        def command(argv: tuple[str, ...], _root: pathlib.Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ("git", "rev-parse", "HEAD"):
                return subprocess.CompletedProcess(argv, 0, current_main + "\n", "")
            raise AssertionError(f"unexpected command during recovery: {argv}")

        with tempfile.TemporaryDirectory() as raw, contextlib.ExitStack() as stack:
            root = pathlib.Path(raw)
            stack.enter_context(mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": repo,
                "GITHUB_RUN_ID": "37101245239",
                "GITHUB_RUN_ATTEMPT": "5",
            }))
            stack.enter_context(mock.patch.object(RUNNER, "load_module", side_effect=load_module))
            stack.enter_context(mock.patch.object(
                RUNNER, "load_promotion_journal_snapshot",
                return_value=(copy.deepcopy(fixture["state"]), "a" * 40),
            ))
            persist = stack.enter_context(mock.patch.object(RUNNER, "persist_journal_record", side_effect=RUNNER.PromotionError("state branch CAS rejected")))
            stack.enter_context(mock.patch.object(RUNNER, "command", side_effect=command))
            stack.enter_context(mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]))
            stack.enter_context(mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]))
            stack.enter_context(mock.patch.object(PR_HELPER, "load_materializer", return_value=object()))
            stack.enter_context(mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=record["candidate"]["head_sha"]))
            stack.enter_context(mock.patch.object(RUNNER, "github_api_request", side_effect=lambda method, endpoint, _body=None: (api_calls.append((method, endpoint)) or (500, None))))
            ci_call = stack.enter_context(mock.patch.object(CI_HELPER, "ensure_verify_release_run", wraps=CI_HELPER.ensure_verify_release_run))
            with self.assertRaisesRegex(RUNNER.PromotionError, "state branch CAS rejected"):
                RUNNER.reconcile_open_promotions(root)

        persist.assert_called_once()
        ci_call.assert_not_called()
        self.assertFalse(any(method == "POST" for method, _endpoint in api_calls))

    def test_retry_after_committed_but_unacknowledged_cas_does_not_duplicate_pending_witness(self) -> None:
        fixture = self.fixture()
        initial = copy.deepcopy(fixture["state"]["records"][0])
        journal = copy.deepcopy(fixture["state"])
        expected_acknowledgements = copy.deepcopy(initial["acknowledgements"])
        repo = initial["candidate"]["repository"]
        current_main = fixture["provenance"]["workflow_checkout_sha"]
        schema = json.loads((SCRIPT.parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text(encoding="utf-8"))
        calls = 0

        def persist(_root: pathlib.Path, _base: str, receipt: dict, *, observed_at: str, **kwargs: object) -> None:
            nonlocal journal, calls
            calls += 1
            journal = PR_HELPER.append_journal_record(journal, receipt, repository=repo, observed_at=observed_at)
            PR_HELPER.validate_journal(journal, schema)
            if calls == 1:
                raise RUNNER.PromotionError("state push succeeded but response was lost")

        def command(argv: tuple[str, ...], _root: pathlib.Path, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ("git", "rev-parse", "HEAD"):
                return subprocess.CompletedProcess(argv, 0, current_main + "\n", "")
            raise AssertionError(f"unexpected command during recovery: {argv}")

        candidate = initial["candidate"]
        validation_args = {
            "source_id": candidate["source_id"],
            "scope": candidate["scope"],
            "generation_id": candidate["generation_id"],
            "registry_path": candidate["registry_path"],
            "registry_bytes": candidate["registry_bytes"],
            "registry_sha256": candidate["registry_sha256"],
            "composition_receipt_sha256": candidate["composition_receipt_sha256"],
        }
        with tempfile.TemporaryDirectory() as raw, contextlib.ExitStack() as stack:
            root = pathlib.Path(raw)
            stack.enter_context(mock.patch.object(RUNNER, "command", side_effect=command))
            stack.enter_context(mock.patch.object(RUNNER, "gh_open_prs", return_value=fixture["open_pr_rows"]))
            stack.enter_context(mock.patch.object(RUNNER, "gh_pr_readback", return_value=fixture["pull_request_readback"]))
            stack.enter_context(mock.patch.object(PR_HELPER, "load_materializer", return_value=object()))
            stack.enter_context(mock.patch.object(PR_HELPER, "remote_ref_sha", return_value=candidate["head_sha"]))
            stack.enter_context(mock.patch.object(RUNNER, "persist_journal_record", side_effect=persist))
            with self.assertRaisesRegex(RUNNER.PromotionError, "response was lost"):
                RUNNER.reconcile_prepared_create_pr(
                    root, repo, initial, PR_HELPER, **validation_args,
                    journal_source_base_sha=current_main,
                    observed_at="2026-10-03T09:20:00Z",
                    run_url="https://github.com/StatPan/datapan-registry/actions/runs/37101245239/attempts/5",
                )
            bound = copy.deepcopy(journal["records"][0])
            recovered, number = RUNNER.reconcile_prepared_create_pr(
                root, repo, bound, PR_HELPER, **validation_args,
                journal_source_base_sha=current_main,
                observed_at="2026-10-03T09:21:00Z",
                run_url="https://github.com/StatPan/datapan-registry/actions/runs/37101245239/attempts/5",
            )

        self.assertEqual(number, 686)
        self.assertEqual(calls, 2)
        self.assertEqual(recovered["ownership"]["expected_head_sha"], candidate["head_sha"])
        self.assertEqual(recovered["acknowledgements"], expected_acknowledgements)
        self.assertEqual(len([ack for ack in recovered["acknowledgements"] if ack["status"] == "pending-review"]), 1)


if __name__ == "__main__":
    unittest.main()
