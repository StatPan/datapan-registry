from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
HEALTH_SPEC = importlib.util.spec_from_file_location(
    "check_upstream_catalogue_health_terminal_intake",
    ROOT / "scripts/check-upstream-catalogue-health.py",
)
assert HEALTH_SPEC and HEALTH_SPEC.loader
HEALTH = importlib.util.module_from_spec(HEALTH_SPEC)
HEALTH_SPEC.loader.exec_module(HEALTH)

FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "canonical_update_terminal_evidence_fixtures",
    ROOT / "tests/test_canonical_update_terminal_evidence.py",
)
assert FIXTURE_SPEC and FIXTURE_SPEC.loader
FIXTURES = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(FIXTURES)

REPOSITORY = "StatPan/datapan-registry"
WORKFLOW_ID = 373700001
RUN_ID = "37371000001"
ATTEMPT = 2
HEAD_SHA = "a" * 40
AS_OF = dt.datetime.fromisoformat("2026-10-06T11:00:00+00:00")


def _native_fixture(mode: str = "recover-ready", *, run_conclusion: str = "failure") -> dict:
    context = FIXTURES._context(mode)
    context.update({
        "run_id": RUN_ID,
        "run_attempt": ATTEMPT,
        "workflow_id": WORKFLOW_ID,
        "head_sha": HEAD_SHA,
        "default_branch": "main",
    })
    document = FIXTURES._document(mode)
    document["invocation"].update({"run_id": RUN_ID, "run_attempt": ATTEMPT})
    archive_bytes = FIXTURES._zip_document(document)

    run = FIXTURES._run(context)
    run["conclusion"] = run_conclusion
    jobs = FIXTURES._jobs(context)
    jobs["jobs_api_endpoint"] = (
        f"repos/{REPOSITORY}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs"
    )
    jobs["jobs"][0].update({
        "id": 773002,
        "run_id": int(RUN_ID),
        "run_attempt": ATTEMPT,
        "head_sha": HEAD_SHA,
        "head_branch": "main",
        "conclusion": run_conclusion,
    })

    inventory = FIXTURES._artifact_inventory(context, archive_bytes)
    inventory["run_id"] = RUN_ID
    artifact = inventory["artifacts"][0]
    artifact["id"] = 9930002
    artifact["name"] = f"canonical-update-promotion-terminal-{RUN_ID}-{ATTEMPT}-{mode}"
    artifact["workflow_run"].update({"id": int(RUN_ID), "head_sha": HEAD_SHA, "head_branch": "main"})
    inventory["details_by_id"] = {str(artifact["id"]): copy.deepcopy(artifact)}

    return {
        "context": context,
        "document": document,
        "archive_bytes": archive_bytes,
        "run": run,
        "jobs": jobs,
        "inventory": inventory,
        "detail": copy.deepcopy(artifact),
        "source_contract": FIXTURES._source_contract(context),
    }


class TerminalOutcomeHealthIntakeTests(unittest.TestCase):
    def _temporary_source_tree(self, *, replacement: tuple[str, bytes] | None = None, symlink_path: str | None = None, omit_path: str | None = None) -> tuple[pathlib.Path, str]:
        root = pathlib.Path(tempfile.mkdtemp(prefix="terminal-intake-source-tree-"))
        paths = tuple(dict.fromkeys(HEALTH.HEALTH_EVALUATOR_SOURCE_PATHS))
        for relative in paths:
            if relative == omit_path:
                continue
            source = ROOT / relative
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if relative == symlink_path:
                target.symlink_to("missing-target")
                continue
            data = replacement[1] if replacement and relative == replacement[0] else source.read_bytes()
            target.write_bytes(data)
            target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)

        def git(*args: str) -> str:
            result = subprocess.run(
                ["git", *args], cwd=root, check=True, text=True,
                capture_output=True, timeout=30,
            )
            return result.stdout.strip()

        git("init", "--quiet")
        git("config", "user.email", "terminal-intake-test@example.invalid")
        git("config", "user.name", "Terminal Intake Test")
        git("add", "--all")
        git("commit", "--quiet", "--allow-empty", "-m", "source closure fixture")
        return root, git("rev-parse", "HEAD")

    def test_actual_git_tree_closure_and_literal_producer_declaration_are_bound(self) -> None:
        expected = HEALTH.C_TERMINAL_EVALUATOR_SOURCE_PATHS
        self.assertEqual(len(expected), 25)
        self.assertEqual(len(set(expected)), 25)
        self.assertEqual(set(HEALTH.TERMINAL_EVIDENCE.EVALUATOR_SOURCE_PATHS), set(expected))
        self.assertEqual(set(HEALTH.HEALTH_EVALUATOR_SOURCE_PATHS) - set(expected), {
            ".github/workflows/upstream-catalogue-health.yml",
            "schemas/datapan.upstream-catalogue-health.v1.schema.json",
            "schemas/datapan.upstream-catalogue-health-state.v1.schema.json",
            "schemas/datapan.upstream-catalogue-health-policy.v1.schema.json",
        })
        actual_runner = (ROOT / "scripts/run-canonical-update-promotion.py").read_bytes()
        self.assertEqual(HEALTH._terminal_runner_declared_paths(actual_runner), expected)

        root, commit = self._temporary_source_tree()
        try:
            contract = HEALTH.terminal_source_contract(
                root, REPOSITORY, WORKFLOW_ID, commit, commit,
            )
            self.assertEqual(set(contract["source_files"]), set(expected))
            self.assertEqual(contract["source_sha"], commit)
            for relative in expected:
                blob = subprocess.run(
                    ["git", "show", f"{commit}:{relative}"], cwd=root,
                    check=True, capture_output=True, timeout=10,
                ).stdout
                self.assertEqual(contract["source_files"][relative], HEALTH.sha256_bytes(blob))
            self.assertEqual(
                contract["schema_sha256"],
                contract["source_files"]["schemas/datapan.canonical-update-promotion-terminal-outcome.v1.schema.json"],
            )
            self.assertEqual(HEALTH.verified_health_evaluator_source(root, commit), commit)

            transitive = root / "scripts/compose-upstream-catalogue-candidate.py"
            transitive.write_bytes(transitive.read_bytes() + b"\n# dirty after pinned checkout\n")
            self.assertIsNone(HEALTH.verified_health_evaluator_source(root, commit))
        finally:
            import shutil
            shutil.rmtree(root)

    def test_real_git_source_contract_rejects_missing_symlink_and_nonliteral_dependencies(self) -> None:
        with self.subTest(kind="missing"):
            root, commit = self._temporary_source_tree(omit_path="scripts/compose-upstream-catalogue-candidate.py")
            try:
                with self.assertRaisesRegex(RuntimeError, "terminal_source_tree_entry"):
                    HEALTH.terminal_source_contract(root, REPOSITORY, WORKFLOW_ID, commit, commit)
            finally:
                import shutil
                shutil.rmtree(root)

        with self.subTest(kind="symlink"):
            root, commit = self._temporary_source_tree(symlink_path="scripts/compose-upstream-catalogue-candidate.py")
            try:
                with self.assertRaisesRegex(RuntimeError, "terminal_source_tree_entry_not_regular_blob"):
                    HEALTH.terminal_source_contract(root, REPOSITORY, WORKFLOW_ID, commit, commit)
            finally:
                import shutil
                shutil.rmtree(root)

        for label, source, reason in (
            (
                "expression",
                b"TERMINAL_EVALUATOR_SOURCE_PATHS = tuple(['.github/workflows/canonical-update-promotion.yml'])\n",
                "terminal_source_runner_dependency_tuple_invalid",
            ),
            (
                "duplicate",
                b"TERMINAL_EVALUATOR_SOURCE_PATHS = ('a', 'a')\n",
                "terminal_source_runner_dependency_duplicate",
            ),
            (
                "unsafe_path",
                b"TERMINAL_EVALUATOR_SOURCE_PATHS = ('../outside.py',)\n",
                "terminal_source_runner_dependency_path_invalid",
            ),
            (
                "nested_assignment",
                b"TERMINAL_EVALUATOR_SOURCE_PATHS = ('a',)\ndef nested():\n    TERMINAL_EVALUATOR_SOURCE_PATHS = ('b',)\n",
                "terminal_source_runner_dependency_declaration_invalid",
            ),
        ):
            with self.subTest(kind=label):
                with self.assertRaisesRegex(RuntimeError, reason):
                    HEALTH._terminal_runner_declared_paths(source)

    def _collect(
        self, fixture: dict, *, detail_after: dict | None = None,
        jobs_after: dict | None = None, latest_after: dict | None = None,
        exact_after: dict | None = None, contract_error: bool = False,
    ):
        run = fixture["run"]
        jobs = fixture["jobs"]
        inventory_before = copy.deepcopy(fixture["inventory"])
        inventory_after = copy.deepcopy(fixture["inventory"])
        detail_calls = 0

        def artifact_detail(_repository: str, _artifact_id: str):
            nonlocal detail_calls
            detail_calls += 1
            if detail_calls > 1 and detail_after is not None:
                return copy.deepcopy(detail_after)
            return copy.deepcopy(fixture["detail"])

        source_contract = mock.Mock(side_effect=RuntimeError("terminal_source_not_in_trusted_main_history")) if contract_error else mock.Mock(return_value=fixture["source_contract"])
        with mock.patch.object(HEALTH, "gh_json", return_value={
            "id": 48151623,
            "default_branch": "main",
            "full_name": REPOSITORY,
        }), mock.patch.object(HEALTH, "terminal_source_contract", source_contract), mock.patch.object(
            HEALTH, "collect_run", side_effect=[copy.deepcopy(run), copy.deepcopy(latest_after or run)],
        ), mock.patch.object(
            HEALTH, "collect_run_attempt", side_effect=[copy.deepcopy(run), copy.deepcopy(exact_after or run)],
        ), mock.patch.object(
            HEALTH, "collect_run_attempt_jobs", side_effect=[copy.deepcopy(jobs), copy.deepcopy(jobs_after or jobs)],
        ), mock.patch.object(
            HEALTH, "collect_terminal_artifact_inventory", side_effect=[inventory_before, inventory_after],
        ), mock.patch.object(
            HEALTH, "collect_artifact", side_effect=artifact_detail,
        ), mock.patch.object(
            HEALTH, "_bounded_gh_archive", return_value=fixture["archive_bytes"],
        ) as archive_read:
            collection = HEALTH.collect_terminal_outcome_records(
                root=ROOT,
                repository=REPOSITORY,
                workflow_id=WORKFLOW_ID,
                runs=[copy.deepcopy(run)],
                previous_attempts={},
                previous_attempt_errors=set(),
                attempt_evidence={
                    f"{RUN_ID}/{ATTEMPT}": {
                        "run": copy.deepcopy(run),
                        **copy.deepcopy(jobs),
                    },
                },
                current_main_sha=HEAD_SHA,
                as_of=AS_OF,
                maximum_future_skew=300,
            )
        return collection, archive_read, source_contract, detail_calls

    def _evaluate_collection(self, collection: dict) -> dict:
        health_policy_path = ROOT / "policy/upstream-catalogue-health.json"
        source_policy_path = ROOT / "policy/source-refresh.json"
        health_policy = HEALTH.load_json(health_policy_path)
        source_policy = HEALTH.load_json(source_policy_path)
        registry_path = ROOT / "data/data-go-kr.registry.json"
        registry_identity = {
            "registry_path": "data/data-go-kr.registry.json",
            "registry_bytes": 1234,
            "registry_sha256": "c" * 64,
        }
        with tempfile.TemporaryDirectory(prefix="terminal-health-evaluate-") as directory:
            processor_state_dir = pathlib.Path(directory) / "processor-state"
            processor_state_dir.mkdir()
            with mock.patch.object(HEALTH, "manifest_registry_identity", return_value=registry_identity):
                receipt = HEALTH.evaluate(
                    as_of=AS_OF,
                    repository=REPOSITORY,
                    health_policy=health_policy,
                    source_policy=source_policy,
                    workflow_runs=[],
                    artifacts_by_run={},
                    artifact_by_id={},
                    processor_state_dir=processor_state_dir,
                    promotion_ack=None,
                    main_revision=HEAD_SHA,
                    manifest_sha256="b" * 64,
                    registry_path=registry_path,
                    last_good=None,
                    mode="live",
                    workflow_run_id="37371000099",
                    workflow_run_attempt=1,
                    source_policy_sha256=HEALTH.file_sha256(source_policy_path),
                    health_policy_sha256=HEALTH.file_sha256(health_policy_path),
                    promotion_terminal_collection=collection,
                )
        HEALTH.validate_schema(receipt, ROOT / "schemas/datapan.upstream-catalogue-health.v1.schema.json", "health_receipt")
        return receipt

    def test_exact_c_attempt_and_artifact_flow_through_real_collector_and_current_join(self) -> None:
        fixture = _native_fixture("recover-ready", run_conclusion="failure")
        collection, archive_read, source_contract, detail_calls = self._collect(fixture)
        self.assertEqual(source_contract.call_count, 1)
        self.assertEqual(archive_read.call_count, 1)
        self.assertEqual(detail_calls, 2)
        self.assertEqual(collection["status"], "pending_validation")
        self.assertEqual(collection["attempts_considered"], 1)
        self.assertEqual(collection["records"][0]["run_id"], RUN_ID)
        self.assertEqual(collection["records"][0]["mode"], "recover-ready")

        current_subject = {
            "main_identity": FIXTURES._main_identity(),
            "generations": [FIXTURES._generation()],
        }
        result = HEALTH.finalize_terminal_outcome_records(
            collection,
            current_subject=current_subject,
            as_of=AS_OF,
            mode="live",
        )
        normalized = result["records"][0]["result"]
        self.assertEqual(result["status"], "verified")
        self.assertEqual(normalized["status"], "verified")
        self.assertEqual(normalized["run_conclusion"], "failure")
        self.assertEqual(normalized["mode_step_conclusion"], "success")
        self.assertEqual(normalized["current_applicability"]["status"], "current")
        self.assertEqual(normalized["current_applicability"]["matching_generations"], [FIXTURES.GENERATION_ID])

        wrong_current = {
            **current_subject,
            "main_identity": {**current_subject["main_identity"], "revision": "f" * 40},
        }
        historical = HEALTH.finalize_terminal_outcome_records(
            collection,
            current_subject=wrong_current,
            as_of=AS_OF,
            mode="live",
        )["records"][0]["result"]
        self.assertEqual(historical["status"], "verified")
        self.assertEqual(historical["current_applicability"]["status"], "historical_only")

    def test_bad_archive_results_produce_schema_valid_health_receipts_without_unverified_identity(self) -> None:
        base = _native_fixture("recover-ready", run_conclusion="failure")
        same_size_document = copy.deepcopy(base["document"])
        same_size_document["evaluator"]["working_tree_head_sha"] = "8" * 40
        same_size_archive = FIXTURES._zip_document(same_size_document)
        self.assertEqual(len(same_size_archive), len(base["archive_bytes"]))

        seal_document = json.dumps(
            base["document"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8") + b"\n"
        bad_seal_archive = FIXTURES._zip_document(
            base["document"],
            members=[
                (FIXTURES.JSON_PATH, seal_document),
                (FIXTURES.SEAL_PATH, b"0" * 64 + b"  terminal-outcome.json\n"),
            ],
        )

        invalid_document = copy.deepcopy(base["document"])
        invalid_document["execution_status"] = "unrecognized"
        invalid_document_archive = FIXTURES._zip_document(invalid_document)

        cases = (
            ("size_mismatch", base["archive_bytes"] + b"x", False, "terminal_artifact_archive_size_mismatch", None),
            ("digest_mismatch", same_size_archive, False, "terminal_artifact_archive_digest_mismatch", None),
            ("invalid_zip", b"not a valid terminal archive", True, "terminal_archive_invalid", None),
            ("seal_mismatch", bad_seal_archive, True, "terminal_archive_seal_mismatch", None),
            # The frozen pure helper currently returns only partial artifact
            # identity for all rejected documents. Health must not fill in the
            # verified ZIP digest/size from API metadata.
            ("document_schema", invalid_document_archive, True, "terminal_document_schema_invalid", None),
            ("success", base["archive_bytes"], False, None, "complete"),
        )
        for label, archive_bytes, bind_archive_metadata, expected_reason, artifact_expectation in cases:
            with self.subTest(case=label):
                fixture = copy.deepcopy(base)
                fixture["archive_bytes"] = archive_bytes
                if bind_archive_metadata:
                    artifact = fixture["inventory"]["artifacts"][0]
                    artifact["size_in_bytes"] = len(archive_bytes)
                    artifact["digest"] = "sha256:" + HEALTH.sha256_bytes(archive_bytes)
                    fixture["detail"] = copy.deepcopy(artifact)
                    fixture["inventory"]["details_by_id"][str(artifact["id"])] = copy.deepcopy(artifact)
                collection, _archive_read, _source_contract, _detail_calls = self._collect(fixture)
                normalized = HEALTH.finalize_terminal_outcome_records(
                    collection, current_subject=None, as_of=AS_OF, mode="live",
                )
                record_result = normalized["records"][0]["result"]
                if expected_reason is None:
                    self.assertEqual(record_result["status"], "verified")
                else:
                    self.assertEqual(record_result["status"], "rejected")
                    self.assertEqual(record_result["reason_code"], expected_reason)
                if artifact_expectation is None:
                    self.assertIsNone(record_result["artifact"])
                else:
                    self.assertEqual(record_result["artifact"]["sha256"], HEALTH.sha256_bytes(archive_bytes))
                    self.assertEqual(record_result["artifact"]["bytes"], len(archive_bytes))
                receipt = self._evaluate_collection(collection)
                terminal = receipt["sources"][0]["canonical"]["promotion_terminal_evidence"]
                self.assertEqual(terminal["records"][0]["result"], record_result)
                self.assertIsNone(receipt["sources"][0]["canonical"]["already_canonical_candidate"])
                self.assertIsNone(receipt["sources"][0]["canonical"]["last_good"])
                self.assertEqual(receipt["summary"]["live_fresh_observation_count"], 0)

    def test_partial_artifact_and_unbounded_native_status_fields_are_normalized(self) -> None:
        collection = {
            "status": "pending_validation",
            "reason_code": None,
            "attempts_considered": 1,
            "records": [{
                "run_id": RUN_ID,
                "run_attempt": ATTEMPT,
                "mode": "recover-ready",
                "result": {
                    "status": "rejected",
                    "reason_code": "terminal_run_status_invalid",
                    "invocation": None,
                    "artifact": {"artifact_id": "9930002", "name": "partial", "expires_at": "2026-11-05T10:00:00Z"},
                    "execution_status": None,
                    "started_at": None,
                    "completed_at": None,
                    "failure_code": None,
                    "outcome": None,
                    "run_status": {"unexpected": "object"},
                    "run_conclusion": "x" * 33,
                    "mode_step_conclusion": None,
                    "current_applicability": {"status": "not_checked", "reason_code": "terminal_run_status_invalid", "matching_generations": []},
                },
            }],
        }
        normalized = HEALTH.finalize_terminal_outcome_records(
            collection, current_subject=None, as_of=AS_OF, mode="live",
        )
        record_result = normalized["records"][0]["result"]
        self.assertIsNone(record_result["artifact"])
        self.assertIsNone(record_result["run_status"])
        self.assertIsNone(record_result["run_conclusion"])
        self.assertEqual(record_result["reason_code"], "terminal_run_status_invalid")
        # The evaluator consumes the native collection shape and performs the
        # same normalization itself; passing the already-finalized unavailable
        # projection would correctly treat it as an empty upstream collection.
        receipt = self._evaluate_collection(collection)
        self.assertEqual(
            receipt["sources"][0]["canonical"]["promotion_terminal_evidence"]["records"][0]["result"],
            record_result,
        )

    def test_artifact_detail_change_after_download_fails_closed(self) -> None:
        fixture = _native_fixture("reconcile-prs", run_conclusion="failure")
        changed = copy.deepcopy(fixture["detail"])
        changed["expires_at"] = "2026-11-06T10:00:00Z"
        collection, archive_read, _, detail_calls = self._collect(fixture, detail_after=changed)
        self.assertEqual(archive_read.call_count, 1)
        self.assertEqual(detail_calls, 2)
        self.assertEqual(collection["records"][0]["result"]["status"], "unavailable")
        self.assertEqual(collection["records"][0]["result"]["reason_code"], "terminal_attempt_changed_during_intake")

    def test_exact_attempt_rerun_after_download_fails_closed(self) -> None:
        fixture = _native_fixture("reconcile-prs", run_conclusion="failure")
        newer = copy.deepcopy(fixture["run"])
        newer["run_attempt"] = ATTEMPT + 1
        collection, archive_read, _, _ = self._collect(fixture, latest_after=newer)
        self.assertEqual(archive_read.call_count, 1)
        self.assertEqual(collection["records"][0]["result"]["reason_code"], "terminal_attempt_changed_during_intake")

    def test_exact_attempt_job_change_after_download_fails_closed(self) -> None:
        fixture = _native_fixture("reconcile-prs", run_conclusion="failure")
        changed_jobs = copy.deepcopy(fixture["jobs"])
        changed_jobs["jobs"][0]["steps"][-1]["conclusion"] = "failure"
        collection, archive_read, _, _ = self._collect(fixture, jobs_after=changed_jobs)
        self.assertEqual(archive_read.call_count, 1)
        self.assertEqual(collection["records"][0]["result"]["reason_code"], "terminal_attempt_changed_during_intake")

    def test_source_not_ancestor_is_unavailable_without_artifact_download(self) -> None:
        fixture = _native_fixture("reconcile-prs", run_conclusion="failure")
        collection, archive_read, source_contract, detail_calls = self._collect(fixture, contract_error=True)
        self.assertEqual(source_contract.call_count, 1)
        self.assertEqual(archive_read.call_count, 0)
        self.assertEqual(detail_calls, 0)
        self.assertEqual(collection["records"][0]["result"]["reason_code"], "terminal_source_not_in_trusted_main_history")

    def test_runs_without_an_invoked_c_mode_are_not_misreported_as_unavailable(self) -> None:
        fixture = _native_fixture("reconcile-prs", run_conclusion="success")
        fixture["jobs"]["jobs"][0]["steps"][1]["conclusion"] = "skipped"
        collection, archive_read, source_contract, detail_calls = self._collect(fixture)
        self.assertEqual(collection["records"][0]["result"]["status"], "not_applicable")
        self.assertEqual(source_contract.call_count, 0)
        self.assertEqual(archive_read.call_count, 0)
        self.assertEqual(detail_calls, 0)
        normalized = HEALTH.finalize_terminal_outcome_records(
            collection, current_subject=None, as_of=AS_OF, mode="live",
        )
        self.assertEqual(normalized["status"], "not_applicable")

    def test_complete_artifact_inventory_paginates_and_rejects_mutation_or_truncation(self) -> None:
        first = {"id": 11, "name": "first"}
        second = {"id": 12, "name": "second"}
        with mock.patch.object(HEALTH, "gh_json", side_effect=[
            {"total_count": 2, "artifacts": [first]},
            {"total_count": 2, "artifacts": [second]},
        ]) as request:
            inventory = HEALTH.collect_terminal_artifact_inventory(REPOSITORY, RUN_ID)
        self.assertEqual(inventory["total_count"], 2)
        self.assertEqual(inventory["artifacts"], [first, second])
        self.assertTrue(request.call_args_list[0].args[0].endswith("?per_page=100&page=1"))
        self.assertTrue(request.call_args_list[1].args[0].endswith("?per_page=100&page=2"))

        failure_cases = [
            (
                "truncated",
                [
                    {"total_count": 2, "artifacts": [first]},
                    {"total_count": 2, "artifacts": []},
                ],
                "terminal_artifact_listing_incomplete",
            ),
            (
                "duplicate_id",
                [
                    {"total_count": 2, "artifacts": [first]},
                    {"total_count": 2, "artifacts": [first]},
                ],
                "terminal_artifact_listing_identity_invalid",
            ),
            (
                "changed_total",
                [
                    {"total_count": 2, "artifacts": [first]},
                    {"total_count": 3, "artifacts": [second]},
                ],
                "terminal_artifact_listing_changed",
            ),
        ]
        for label, responses, expected in failure_cases:
            with self.subTest(label=label), mock.patch.object(HEALTH, "gh_json", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, expected):
                    HEALTH.collect_terminal_artifact_inventory(REPOSITORY, RUN_ID)


if __name__ == "__main__":
    unittest.main()
