from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/canonical_update_ci.py"
SPEC = importlib.util.spec_from_file_location("canonical_update_ci_test_module", SCRIPT)
assert SPEC and SPEC.loader
CI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CI)
PROMOTION_SPEC = importlib.util.spec_from_file_location(
    "canonical_update_pr_ci_integration_test_module",
    pathlib.Path(__file__).parents[1] / "scripts/canonical_update_pr.py",
)
assert PROMOTION_SPEC and PROMOTION_SPEC.loader
PROMOTION = importlib.util.module_from_spec(PROMOTION_SPEC)
PROMOTION_SPEC.loader.exec_module(PROMOTION)


REPOSITORY = "StatPan/datapan-registry"
SOURCE_ID = "data_go_kr"
SCOPE = "aggregate_supported_catalog"
BASE = "a" * 40
HEAD = "b" * 40
OWNER = "datapan-canonical-update:v1:" + hashlib.sha256(
    "\0".join((REPOSITORY.lower(), SOURCE_ID, SCOPE)).encode("utf-8")
).hexdigest()
BRANCH = f"automation/canonical-update/data-go-kr-{hashlib.sha256(SCOPE.encode()).hexdigest()[:12]}"
WORKFLOW_PATH = ".github/workflows/verify-release.yml"


def successful_jobs() -> list[dict[str, str]]:
    return [
        {"name": "Diagnostic candidate (pre-distribution)", "status": "completed", "conclusion": "success"},
        {"name": "verify", "status": "completed", "conclusion": "success"},
    ]


def run_record(
    run_id: int = 123,
    *,
    head_sha: str = HEAD,
    status: str = "completed",
    conclusion: str | None = "success",
    attempt: int = 1,
    jobs: list[dict[str, str]] | None = None,
) -> dict:
    return {
        "id": run_id,
        "run_number": run_id,
        "run_attempt": attempt,
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
        # GitHub's Actions REST run object supplies the workflow path and head
        # identity; it normally has no `ref` field and no @refs suffix here.
        "path": WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "head_branch": BRANCH,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        "jobs": copy.deepcopy(successful_jobs() if jobs is None else jobs),
    }


class CanonicalUpdateCITests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = f"<!-- {OWNER} generation=generation-a -->\n\nCandidate review body.\n"
        self.receipt = {
            "schema_version": "datapan.canonical-update-promotion.v1",
            "status": "pending-review",
            "candidate": {
                "repository": REPOSITORY,
                "source_id": SOURCE_ID,
                "scope": SCOPE,
                "base_sha": BASE,
                "generation_id": "generation-a",
                "head_sha": HEAD,
            },
            "ownership": {
                "owner_id": OWNER,
                "branch": BRANCH,
                "expected_head_sha": HEAD,
                "body_sha256": hashlib.sha256(self.body.encode("utf-8")).hexdigest(),
            },
            "pr": {"number": 652, "url": "https://github.com/StatPan/datapan-registry/pull/652", "state": "open"},
            "action": "create",
            "blockers": ["manual_review_revalidation_required"],
        }
        self.pr = {
            "number": 652,
            "state": "OPEN",
            "body": self.body,
            "headRefName": BRANCH,
            "headRefOid": HEAD,
            "baseRefName": "main",
            "repository": {"full_name": REPOSITORY},
            "headRepository": {"full_name": REPOSITORY},
        }
        self.persisted: list[dict] = []
        self.dispatches: list[tuple[str, dict]] = []
        self.run_lists: list[tuple] = []
        self.run_reads: list[int] = []
        self.available_runs: list[dict] = []
        self.read_by_id: dict[int, dict] = {}
        self.pr_values: list[dict] = []
        self.branch_values: list[str | None] = []
        self.dispatch_response: tuple[int | None, dict | None] = (200, {"workflow_run_id": 123, "run_url": "https://github.com/StatPan/datapan-registry/actions/runs/123"})
        self.dispatch_exception: Exception | None = None
        self.on_dispatch = None
        self.read_run_exception: Exception | None = None

    def read_pr(self) -> dict:
        if self.pr_values:
            return copy.deepcopy(self.pr_values.pop(0))
        return copy.deepcopy(self.pr)

    def read_branch_sha(self, branch: str) -> str | None:
        self.assertEqual(branch, BRANCH)
        if self.branch_values:
            return self.branch_values.pop(0)
        return HEAD

    def list_matching_runs(self, repository: str, workflow: str, branch: str, event: str, head_sha: str) -> list[dict]:
        self.run_lists.append((repository, workflow, branch, event, head_sha))
        return copy.deepcopy(self.available_runs)

    def dispatch(self, ref: str, inputs: dict[str, str]) -> tuple[int | None, dict | None]:
        self.dispatches.append((ref, dict(inputs)))
        self.assertTrue(self.persisted, "intent must be durable before the dispatch POST")
        self.assertEqual(self.persisted[-1]["state"], "intent")
        if self.on_dispatch is not None:
            self.on_dispatch()
        if self.dispatch_exception is not None:
            raise self.dispatch_exception
        return self.dispatch_response

    def read_run(self, run_id: int) -> dict:
        self.run_reads.append(run_id)
        if self.read_run_exception is not None:
            raise self.read_run_exception
        return copy.deepcopy(self.read_by_id.get(run_id, run_record(run_id)))

    def persist(self, entry: dict) -> None:
        self.persisted.append(copy.deepcopy(entry))

    def ensure(self, dispatch_state: dict | None = None) -> dict:
        return CI.ensure_verify_release_run(
            REPOSITORY,
            self.receipt,
            self.read_pr,
            dispatch_state,
            read_branch_sha=self.read_branch_sha,
            list_matching_runs=self.list_matching_runs,
            dispatch=self.dispatch,
            read_run=self.read_run,
            persist=self.persist,
        )

    def test_dispatch_persists_intent_and_verifies_exact_run_and_required_jobs(self) -> None:
        result = self.ensure()

        ci = result["ci"]
        self.assertEqual(ci["state"], "success")
        self.assertEqual(ci["run_id"], 123)
        self.assertEqual(ci["run_attempt"], 1)
        self.assertIsNone(ci["blocker"])
        self.assertEqual(ci["dispatch_http_status"], 200)
        self.assertEqual(self.dispatches, [(BRANCH, {"expected_head_sha": HEAD})])
        self.assertEqual(self.run_lists[0], (REPOSITORY, WORKFLOW_PATH, BRANCH, "workflow_dispatch", HEAD))
        self.assertEqual([entry["state"] for entry in self.persisted], ["intent", "uncertain", "uncertain", "success"])
        self.assertEqual(result["blockers"], ["manual_review_revalidation_required"])
        self.assertEqual(result["ci"]["workflow_path"], WORKFLOW_PATH)

    def test_204_after_dispatch_adopts_one_exact_matching_run(self) -> None:
        self.dispatch_response = (204, {})
        observed = run_record(201)
        self.read_by_id[201] = observed
        self.on_dispatch = lambda: setattr(self, "available_runs", [observed])

        result = self.ensure()

        self.assertEqual(result["ci"]["state"], "success")
        self.assertEqual(result["ci"]["dispatch_http_status"], 204)
        self.assertEqual(result["ci"]["run_id"], 201)
        self.assertEqual(len(self.dispatches), 1)

    def test_dispatch_timeout_with_matching_run_is_reconciled_without_retry(self) -> None:
        observed = run_record(202)
        self.read_by_id[202] = observed
        self.on_dispatch = lambda: setattr(self, "available_runs", [observed])
        self.dispatch_exception = TimeoutError("response lost after server accepted request")

        result = self.ensure()

        self.assertEqual(result["ci"]["state"], "success")
        self.assertEqual(result["ci"]["run_id"], 202)
        self.assertEqual(result["ci"]["dispatch_http_status"], None)
        self.assertEqual(len(self.dispatches), 1)

    def test_204_without_matching_run_stays_uncertain_and_is_never_redispatched(self) -> None:
        self.dispatch_response = (204, {})
        first = self.ensure()
        self.assertEqual(first["ci"]["state"], "uncertain")
        self.assertEqual(first["ci"]["blocker"], "verify_release_dispatch_outcome_unknown")
        self.assertEqual(first["ci"]["dispatch_http_status"], 204)

        second = self.ensure(first["ci"])
        self.assertEqual(second["ci"]["state"], "uncertain")
        self.assertEqual(len(self.dispatches), 1)

    def test_crash_after_intent_without_run_does_not_blindly_redispatch(self) -> None:
        self.dispatch_exception = RuntimeError("simulated process interruption before network write")
        first = self.ensure()
        self.assertEqual(first["ci"]["state"], "uncertain")
        self.assertIsNone(first["ci"]["run_id"])

        second = self.ensure(first["ci"])
        self.assertEqual(second["ci"]["state"], "uncertain")
        self.assertEqual(len(self.dispatches), 1)

    def test_adopts_one_preexisting_matching_run_without_dispatch(self) -> None:
        observed = run_record(203)
        self.available_runs = [observed]
        self.read_by_id[203] = observed

        result = self.ensure()

        self.assertEqual(result["ci"]["state"], "success")
        self.assertEqual(result["ci"]["run_id"], 203)
        self.assertEqual(self.dispatches, [])


    def test_realistic_actions_run_path_without_ref_is_a_match(self) -> None:
        observed = run_record(211)
        self.assertEqual(observed["path"], WORKFLOW_PATH)
        self.assertNotIn("ref", observed)
        self.available_runs = [observed]
        self.read_by_id[211] = observed

        result = self.ensure()

        self.assertEqual(result["ci"]["state"], "success")
        self.assertEqual(result["ci"]["run_id"], 211)
        self.assertEqual(self.dispatches, [])

    def test_contradictory_optional_ref_or_path_suffix_is_not_adopted(self) -> None:
        for field, value in (
            ("ref", "refs/heads/another-branch"),
            ("path", f"{WORKFLOW_PATH}@refs/heads/another-branch"),
        ):
            with self.subTest(field=field):
                self.setUp()
                contradictory = run_record(212)
                contradictory[field] = value
                self.available_runs = [contradictory]

                result = self.ensure()

                self.assertEqual(result["ci"]["state"], "success")
                self.assertEqual(result["ci"]["run_id"], 123)
                self.assertEqual([row[0] for row in self.dispatches], [BRANCH])
                self.assertNotIn(212, self.run_reads)

    def test_pr_readback_requires_base_and_head_repository_identity(self) -> None:
        for key, value in (
            ("repository", None),
            ("headRepository", None),
            ("repository", {"full_name": "Elsewhere/repo"}),
            ("headRepository", {"full_name": "Elsewhere/repo"}),
        ):
            with self.subTest(key=key, value=value):
                self.setUp()
                self.pr.pop(key) if value is None else self.pr.__setitem__(key, value)

                with self.assertRaisesRegex(CI.VerifyReleaseDispatchError, "preflight failed"):
                    self.ensure()

                self.assertEqual(self.persisted, [])
                self.assertEqual(self.dispatches, [])

    def test_multiple_matching_run_ids_are_action_required_with_ids_preserved(self) -> None:
        first, second = run_record(204), run_record(205)
        self.available_runs = [first, second]

        result = self.ensure()

        self.assertEqual(result["ci"]["state"], "action_required")
        self.assertEqual(result["ci"]["blocker"], "verify_release_multiple_matching_runs:204,205")
        self.assertEqual(self.dispatches, [])

    def test_pending_run_is_blocked_and_not_redispatched(self) -> None:
        pending = run_record(206, status="in_progress", conclusion=None)
        self.available_runs = [pending]
        self.read_by_id[206] = pending

        first = self.ensure()
        self.assertEqual(first["ci"]["state"], "in_progress")
        self.assertEqual(first["ci"]["blocker"], "verify_release_pending")
        second = self.ensure(first["ci"])
        self.assertEqual(second["ci"]["state"], "in_progress")
        self.assertEqual(self.dispatches, [])

    def test_requested_waiting_and_pending_statuses_are_pending_not_terminal(self) -> None:
        for status in ("requested", "waiting", "pending"):
            with self.subTest(status=status):
                self.setUp()
                pending = run_record(213, status=status, conclusion=None)
                self.available_runs = [pending]
                self.read_by_id[213] = pending

                result = self.ensure()

                self.assertEqual(result["ci"]["state"], "queued")
                self.assertEqual(result["ci"]["run_status"], status)
                self.assertEqual(result["ci"]["blocker"], "verify_release_pending")
                self.assertEqual(self.dispatches, [])

    def test_waiting_run_can_be_read_only_reconciled_to_success(self) -> None:
        waiting = run_record(214, status="waiting", conclusion=None)
        self.available_runs = [waiting]
        self.read_by_id[214] = waiting
        first = self.ensure()
        self.assertEqual(first["ci"]["state"], "queued")

        completed = run_record(214)
        self.read_by_id[214] = completed
        second = self.ensure(first["ci"])

        self.assertEqual(second["ci"]["state"], "success")
        self.assertEqual(self.dispatches, [])

    def test_known_failure_is_preserved_without_automatic_retry(self) -> None:
        failed = run_record(207, conclusion="failure")
        self.available_runs = [failed]
        self.read_by_id[207] = failed

        result = self.ensure()
        again = self.ensure(result["ci"])

        self.assertEqual(result["ci"]["state"], "failure")
        self.assertEqual(again["ci"]["state"], "failure")
        self.assertEqual(again["ci"]["blocker"], "verify_release_failed")
        self.assertEqual(self.dispatches, [])

    def test_latest_attempt_of_single_run_controls_the_recorded_outcome(self) -> None:
        attempt_one = run_record(208, attempt=1, conclusion="success")
        attempt_two = run_record(208, attempt=2, conclusion="failure")
        self.available_runs = [attempt_one, attempt_two]
        self.read_by_id[208] = attempt_two

        result = self.ensure()

        self.assertEqual(result["ci"]["run_attempt"], 2)
        self.assertEqual(result["ci"]["state"], "failure")
        self.assertEqual(self.dispatches, [])

    def test_stale_run_readback_cannot_replace_newer_attempt(self) -> None:
        listed = run_record(216, attempt=2, conclusion="failure")
        self.available_runs = [listed]
        self.read_by_id[216] = run_record(216, attempt=1, conclusion="success")

        result = self.ensure()

        self.assertEqual(result["ci"]["state"], "action_required")
        self.assertEqual(result["ci"]["blocker"], "verify_release_run_attempt_regressed")
        self.assertEqual(result["ci"]["run_attempt"], 2)
        self.assertEqual(self.dispatches, [])

    def test_missing_or_skipped_required_job_cannot_produce_success(self) -> None:
        for jobs, expected_blocker in (
            (None, "verify_release_required_jobs_missing"),
            ([{"name": "verify", "status": "completed", "conclusion": "success"}], "verify_release_required_jobs_missing_or_duplicated"),
            ([
                {"name": "Diagnostic candidate (pre-distribution)", "status": "completed", "conclusion": "skipped"},
                {"name": "verify", "status": "completed", "conclusion": "success"},
            ], "verify_release_required_job_not_successful"),
        ):
            with self.subTest(expected_blocker=expected_blocker):
                self.setUp()
                observed = run_record(209, jobs=jobs)
                if jobs is None:
                    observed.pop("jobs")
                self.available_runs = [observed]
                self.read_by_id[209] = observed

                result = self.ensure()

                self.assertEqual(result["ci"]["state"], "action_required")
                self.assertEqual(result["ci"]["blocker"], expected_blocker)
                self.assertEqual(self.dispatches, [])

    def test_action_required_missing_jobs_can_recover_read_only(self) -> None:
        incomplete = run_record(215)
        incomplete.pop("jobs")
        self.available_runs = [incomplete]
        self.read_by_id[215] = incomplete

        first = self.ensure()
        self.assertEqual(first["ci"]["state"], "action_required")
        self.assertEqual(first["ci"]["blocker"], "verify_release_required_jobs_missing")

        self.read_by_id[215] = run_record(215)
        second = self.ensure(first["ci"])

        self.assertEqual(second["ci"]["state"], "success")
        self.assertEqual(self.dispatches, [])

    def test_body_change_before_dispatch_rejects_after_durable_intent(self) -> None:
        changed = copy.deepcopy(self.pr)
        changed["body"] += "human edit\n"
        self.pr_values = [copy.deepcopy(self.pr), changed]

        result = self.ensure()

        self.assertEqual(result["ci"]["state"], "action_required")
        self.assertEqual(result["ci"]["blocker"], "verify_release_candidate_identity_changed_before_dispatch")
        self.assertEqual([entry["state"] for entry in self.persisted], ["intent", "action_required"])
        self.assertEqual(self.dispatches, [])

    def test_head_movement_after_success_readback_revokes_success(self) -> None:
        self.pr_values = [copy.deepcopy(self.pr), copy.deepcopy(self.pr), {**self.pr, "headRefOid": "c" * 40}]
        self.branch_values = [HEAD, HEAD, "c" * 40]

        result = self.ensure()

        self.assertEqual(result["ci"]["state"], "action_required")
        self.assertEqual(result["ci"]["blocker"], "verify_release_candidate_identity_changed")
        self.assertEqual(result["ci"]["run_id"], 123)
        self.assertEqual(result["ci"]["conclusion"], "success")

    def test_identity_blocker_can_recover_read_only_after_head_is_restored(self) -> None:
        first = self.ensure()
        self.assertEqual(first["ci"]["state"], "success")

        changed = {**self.pr, "body": self.pr["body"] + "human edit\n"}
        self.pr = changed
        second = self.ensure(first["ci"])
        self.assertEqual(second["ci"]["state"], "action_required")

        self.pr = {
            **self.pr,
            "body": self.body,
            "headRefName": BRANCH,
            "headRefOid": HEAD,
            "baseRefName": "main",
            "repository": {"full_name": REPOSITORY},
            "headRepository": {"full_name": REPOSITORY},
        }
        third = self.ensure(second["ci"])

        self.assertEqual(third["ci"]["state"], "success")
        self.assertEqual(third["ci"]["run_id"], 123)
        self.assertEqual(len(self.dispatches), 1)

    def test_success_refresh_transport_error_then_recovery_never_redispatches(self) -> None:
        first = self.ensure()
        self.assertEqual(first["ci"]["state"], "success")

        self.read_run_exception = TimeoutError("temporary API read failure")
        second = self.ensure(first["ci"])
        self.assertEqual(second["ci"]["state"], "uncertain")
        self.assertEqual(second["ci"]["run_id"], 123)

        self.read_run_exception = None
        third = self.ensure(second["ci"])
        self.assertEqual(third["ci"]["state"], "success")
        self.assertEqual(len(self.dispatches), 1)

    def test_returned_run_with_wrong_head_is_not_adopted_as_success(self) -> None:
        wrong = run_record(210, head_sha="d" * 40)
        self.read_by_id[210] = wrong
        self.dispatch_response = (200, {"workflow_run_id": 210, "run_url": "https://example.test/run/210"})

        result = self.ensure()

        self.assertEqual(result["ci"]["state"], "action_required")
        self.assertEqual(result["ci"]["blocker"], "verify_release_run_identity_or_status_invalid")
        self.assertEqual(result["ci"]["run_id"], 210)

    def test_foreign_body_fails_preflight_without_any_state_or_dispatch(self) -> None:
        changed = {**self.pr, "body": "not the owned body"}
        self.pr_values = [changed]

        with self.assertRaisesRegex(CI.VerifyReleaseDispatchError, "preflight failed"):
            self.ensure()

        self.assertEqual(self.persisted, [])
        self.assertEqual(self.dispatches, [])

    def test_state_for_another_head_is_rejected_before_callbacks(self) -> None:
        foreign_state = {
            "repository": REPOSITORY,
            "workflow_path": WORKFLOW_PATH,
            "head_sha": "c" * 40,
            "branch": BRANCH,
            "pr_number": 652,
            "owner_id": OWNER,
            "body_sha256": hashlib.sha256(self.body.encode()).hexdigest(),
            "request_fingerprint": "d" * 64,
            "state": "uncertain",
            "intent_at": "2026-10-03T00:00:00Z",
            "run_id": None,
            "run_attempt": None,
            "run_url": None,
            "run_status": None,
            "conclusion": None,
            "observed_at": "2026-10-03T00:00:00Z",
            "dispatch_http_status": None,
            "blocker": None,
        }

        with self.assertRaisesRegex(CI.VerifyReleaseDispatchError, "head_sha"):
            self.ensure(foreign_state)

        self.assertEqual(self.run_lists, [])
        self.assertEqual(self.dispatches, [])

    def test_readback_transport_failure_preserves_run_id_for_later_reconciliation(self) -> None:
        self.read_run_exception = TimeoutError("run readback unavailable")
        first = self.ensure()
        self.assertEqual(first["ci"]["state"], "uncertain")
        self.assertEqual(first["ci"]["run_id"], 123)

        self.read_run_exception = None
        self.read_by_id[123] = run_record(123)
        second = self.ensure(first["ci"])
        self.assertEqual(second["ci"]["state"], "success")
        self.assertEqual(len(self.dispatches), 1)

    def test_verify_release_workflow_has_optional_expected_head_guards_before_checkout(self) -> None:
        import yaml

        workflow_path = pathlib.Path(__file__).parents[1] / ".github/workflows/verify-release.yml"
        workflow = yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)
        self.assertIn("expected_head_sha", workflow["on"]["workflow_dispatch"]["inputs"])
        self.assertEqual(
            workflow["on"]["workflow_dispatch"]["inputs"]["expected_head_sha"]["default"],
            "",
        )
        for job_name in ("diagnostic-candidate", "verify"):
            steps = workflow["jobs"][job_name]["steps"]
            guard_index = next(
                index for index, step in enumerate(steps)
                if step.get("name") == "Check dispatched candidate head"
            )
            checkout_index = next(
                index for index, step in enumerate(steps)
                if step.get("name") == "Checkout registry"
            )
            self.assertLess(guard_index, checkout_index)
            self.assertEqual(
                steps[guard_index]["if"],
                "github.event_name == 'workflow_dispatch' && inputs.expected_head_sha != ''",
            )
            self.assertIn("${{ github.sha }}", steps[guard_index]["env"]["ACTUAL_HEAD_SHA"])


class CanonicalUpdateCIJournalIntegrationTests(unittest.TestCase):
    """Exercise helper state transitions through the real promotion journal."""

    def setUp(self) -> None:
        body = f"<!-- {OWNER} generation=generation-a -->\n\nCandidate review body.\n"
        self.receipt = {
            "schema_version": "datapan.canonical-update-promotion-receipt.v1",
            "status": "pending-review",
            "candidate": {
                "repository": REPOSITORY,
                "source_id": SOURCE_ID,
                "scope": SCOPE,
                "base_sha": BASE,
                "generation_id": "generation-a",
                "head_sha": HEAD,
                "manifest_sha256": "c" * 64,
                "registry_sha256": "d" * 64,
                "registry_path": "data/data-go-kr.registry.json",
                "registry_bytes": 123,
                "composition_receipt_sha256": "e" * 64,
            },
            "ownership": {
                "owner_id": OWNER,
                "branch": BRANCH,
                "expected_head_sha": HEAD,
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            },
            "pr": {"number": 652, "url": "https://github.com/StatPan/datapan-registry/pull/652", "state": "open"},
            "acknowledgements": [],
            "blockers": [],
        }
        self.body = body
        self.pr = {
            "number": 652,
            "state": "OPEN",
            "body": body,
            "headRefName": BRANCH,
            "headRefOid": HEAD,
            "baseRefName": "main",
            "repository": {"full_name": REPOSITORY},
            "headRepository": {"full_name": REPOSITORY},
        }
        self.journal_schema = json.loads(
            (pathlib.Path(__file__).parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text()
        )
        self.receipt_schema = json.loads(
            (pathlib.Path(__file__).parents[1] / "schemas/datapan.canonical-update-promotion-receipt.v1.schema.json").read_text()
        )
        now = CI._now()
        self.journal = PROMOTION.append_journal_record(
            None, self.receipt, repository=REPOSITORY, observed_at=now,
        )
        PROMOTION.validate_journal(self.journal, self.journal_schema)
        self.expected_ci = None
        self.persist_history: list[dict] = []
        self.available_runs: list[dict] = []
        self.current_run: dict | None = None
        self.read_error: Exception | None = None
        self.branch_sha = HEAD

    def journal_record(self, receipt: dict | None = None) -> dict:
        selected = receipt or self.receipt
        key = PROMOTION.candidate_key(selected)
        matches = [row for row in self.journal["records"] if PROMOTION.candidate_key(row) == key]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def persist_ci(self, entry: dict) -> None:
        current = self.journal_record()
        PROMOTION.assert_ci_compare_and_swap(self.journal, current, self.expected_ci)
        updated = copy.deepcopy(current)
        updated["ci"] = copy.deepcopy(entry)
        self.journal = PROMOTION.append_journal_record(
            self.journal, updated, repository=REPOSITORY,
            observed_at=entry["observed_at"],
        )
        PROMOTION.validate_journal(self.journal, self.journal_schema)
        self.expected_ci = copy.deepcopy(entry)
        self.persist_history.append(copy.deepcopy(entry))

    def read_pr(self) -> dict:
        return copy.deepcopy(self.pr)

    def read_branch(self, branch: str) -> str:
        self.assertEqual(branch, BRANCH)
        return self.branch_sha

    def list_runs(self, *_args) -> list[dict]:
        return copy.deepcopy(self.available_runs)

    def read_run(self, run_id: int) -> dict:
        if self.read_error:
            raise self.read_error
        value = copy.deepcopy(self.current_run or run_record(run_id))
        value["id"] = run_id
        return value

    def ensure(self, state: dict | None = None) -> dict:
        return CI.ensure_verify_release_run(
            REPOSITORY, self.receipt, self.read_pr, state,
            read_branch_sha=self.read_branch,
            list_matching_runs=self.list_runs,
            dispatch=lambda *_args: self.fail("journal reconciliation must not dispatch"),
            read_run=self.read_run,
            persist=self.persist_ci,
        )

    def test_queued_then_successful_run_persists_through_real_journal(self) -> None:
        queued = run_record(123, status="pending", conclusion=None)
        self.available_runs = [queued]
        self.current_run = queued

        first = self.ensure()
        self.assertEqual(first["ci"]["state"], "queued")
        self.assertEqual(self.journal_record()["ci"]["state"], "queued")
        self.assertEqual(len(self.persist_history), 2)

        completed = run_record(123, status="completed", conclusion="success")
        self.available_runs = [completed]
        self.current_run = completed
        second = self.ensure(first["ci"])

        self.assertEqual(second["ci"]["state"], "success")
        self.assertEqual(second["ci"]["run_id"], 123)
        self.assertEqual(self.journal_record()["ci"]["state"], "success")
        self.assertEqual([row["state"] for row in self.persist_history], ["uncertain", "queued", "uncertain", "success"])

    def test_transient_run_read_failure_is_durable_and_recovers_without_dispatch(self) -> None:
        successful = run_record(123)
        self.available_runs = [successful]
        self.current_run = successful
        good = self.ensure()
        self.assertEqual(good["ci"]["state"], "success")

        self.read_error = TimeoutError("temporary Actions API outage")
        transient = self.ensure(good["ci"])
        self.assertEqual(transient["ci"]["state"], "uncertain")
        self.assertEqual(self.journal_record()["ci"]["state"], "uncertain")

        self.read_error = None
        recovered = self.ensure(transient["ci"])
        self.assertEqual(recovered["ci"]["state"], "success")
        self.assertEqual(self.journal_record()["ci"]["state"], "success")
        self.assertEqual(len(self.persist_history), 6)

    def test_same_attempt_success_is_revoked_when_owned_pr_identity_changes(self) -> None:
        self.available_runs = [run_record(123)]
        self.current_run = run_record(123)
        succeeded = self.ensure()
        self.assertEqual(succeeded["ci"]["state"], "success")
        old_attempt = self.journal_record()["ci"]["run_attempt"]

        self.pr["body"] = self.body + "human edit\n"
        revoked = self.ensure(succeeded["ci"])

        self.assertEqual(revoked["ci"]["state"], "action_required")
        self.assertEqual(revoked["ci"]["run_attempt"], old_attempt)
        self.assertEqual(self.journal_record()["ci"]["state"], "action_required")

    def test_journal_compare_and_swap_rejects_a_stale_ci_observation(self) -> None:
        self.available_runs = [run_record(123)]
        self.current_run = run_record(123)
        succeeded = self.ensure()
        external = copy.deepcopy(self.journal_record())
        external["ci"]["blocker"] = "external-observation"
        external["ci"]["observed_at"] = "2999-01-01T00:00:00Z"
        self.journal = PROMOTION.append_journal_record(
            self.journal, external, repository=REPOSITORY,
            observed_at="2999-01-01T00:00:00Z",
        )
        PROMOTION.validate_journal(self.journal, self.journal_schema)

        with self.assertRaisesRegex(PROMOTION.AdmissionError, "compare-and-swap conflict"):
            self.ensure(succeeded["ci"])

    def test_new_payload_revision_keeps_old_ci_and_requires_its_own_exact_head_run(self) -> None:
        self.available_runs = [run_record(123)]
        self.current_run = run_record(123)
        first = self.ensure()
        old_ci = copy.deepcopy(first["ci"])
        old = copy.deepcopy(self.journal_record())

        new = copy.deepcopy(old)
        new["status"] = "prepared"
        new["action"] = "refresh_owned"
        new["candidate"]["head_sha"] = "c" * 40
        new["candidate"]["registry_sha256"] = "f" * 64
        new["ownership"]["expected_head_sha"] = "c" * 40
        new_body = self.body + "Updated composed payload.\n"
        new["ownership"]["body"] = new_body
        new["ownership"]["body_sha256"] = hashlib.sha256(new_body.encode("utf-8")).hexdigest()
        new["refresh_from"] = PROMOTION.revision_reference(old)
        new.pop("ci", None)
        new["acknowledgements"] = []
        self.journal = PROMOTION.append_journal_record(
            self.journal, new, repository=REPOSITORY, observed_at=CI._now(),
        )
        new_readback = PROMOTION.record_pr_readback(new, {
            **self.pr,
            "body": new_body,
            "headRefOid": "c" * 40,
        }, observed_at="2026-10-03T00:00:00Z", run_url="https://github.com/StatPan/datapan-registry/actions/runs/124/attempts/1")
        self.journal = PROMOTION.append_journal_record(
            self.journal, new_readback, repository=REPOSITORY,
            observed_at="2026-10-03T00:00:00Z",
            supersede_from=new["refresh_from"],
        )
        PROMOTION.validate_journal(self.journal, self.journal_schema)
        self.assertEqual(self.journal_record(old)["ci"], old_ci)
        self.assertEqual(self.journal_record(old)["superseded_by"], PROMOTION.revision_reference(new_readback))
        self.assertNotIn("ci", self.journal_record(new_readback))

        self.receipt = copy.deepcopy(new_readback)
        self.body = new_body
        self.pr = {**self.pr, "body": new_body, "headRefOid": "c" * 40}
        self.branch_sha = "c" * 40
        self.expected_ci = None
        self.persist_history = []
        self.available_runs = [run_record(124, head_sha="c" * 40)]
        self.current_run = run_record(124, head_sha="c" * 40)
        current = self.ensure()

        self.assertEqual(current["ci"]["state"], "success")
        self.assertEqual(current["ci"]["head_sha"], "c" * 40)
        self.assertNotEqual(current["ci"]["request_fingerprint"], old_ci["request_fingerprint"])
        self.assertEqual(self.journal_record(old)["ci"], old_ci)
        self.assertEqual(self.journal_record(new_readback)["ci"], current["ci"])


if __name__ == "__main__":
    unittest.main()
