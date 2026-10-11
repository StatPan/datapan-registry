from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import pathlib
import sys
import time
import urllib.parse
import threading

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import canonical_stale_queue_recovery as recovery  # noqa: E402
import canonical_stale_queue_recovery_gate as recovery_gate  # noqa: E402
import canonical_stale_queue_state as queue_state  # noqa: E402


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
CURRENT_SHA = "a" * 40
TARGET_SHA = "b" * 40
WORKFLOW_ID = 22
CONTROLLER_ID = 900
TARGET_ID = 700
REPOSITORY_ID = 12345
WORKFLOW_BYTES = b"""name: canonical update promotion\nconcurrency:\n  group: canonical-update-promotion\n  cancel-in-progress: false\njobs:\n  reconcile:\n    runs-on: ubuntu-latest\n"""
TARGET_WORKFLOW_BYTES = WORKFLOW_BYTES + b"# target revision captured at the reserved SHA\n"


def _run(
    run_id: int,
    *,
    sha: str,
    status: str,
    created: str,
    updated: str,
    attempt: int = 1,
    conclusion: str | None = None,
) -> dict[str, object]:
    return {
        "id": run_id,
        "workflow_id": WORKFLOW_ID,
        "run_attempt": attempt,
        "event": "schedule",
        "head_branch": "main",
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "created_at": created,
        "updated_at": updated,
        "repository": {"id": REPOSITORY_ID, "full_name": recovery.REPOSITORY},
    }


def _controller() -> dict[str, object]:
    return _run(
        CONTROLLER_ID,
        sha=CURRENT_SHA,
        status="in_progress",
        created="2026-10-10T09:00:00Z",
        updated="2026-10-10T09:59:00Z",
    )


def _target() -> dict[str, object]:
    return _run(
        TARGET_ID,
        sha=TARGET_SHA,
        status="queued",
        created="2026-10-07T16:54:11Z",
        updated="2026-10-07T16:54:11Z",
    )


def _environment() -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY": recovery.REPOSITORY,
        "GITHUB_REPOSITORY_ID": str(REPOSITORY_ID),
        "GITHUB_RUN_ID": str(CONTROLLER_ID),
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": CURRENT_SHA,
        "GITHUB_WORKFLOW_REF": f"{recovery.REPOSITORY}/{recovery.WORKFLOW_PATH}@refs/heads/main",
        "GITHUB_JOB": "reconcile",
        "C_INITIAL_CHECKOUT_SHA": CURRENT_SHA,
        "C_EVALUATOR_SOURCE_SHA": CURRENT_SHA,
        "GITHUB_TOKEN": "fixture-secret-token",
    }


def _blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


class OfflineGitHub:
    """Small deterministic REST fixture; no request can reach the network."""

    def __init__(
        self,
        *,
        targets: list[dict[str, object]] | None = None,
        post_status: int = 202,
        terminal_on_post: bool = True,
        post_error: bool = False,
        rerun_after_post: bool = False,
        post_conclusion: str = "cancelled",
        jobs_error: bool = False,
        candidate_jobs: dict[int, list[dict[str, object]]] | None = None,
        workflow_by_sha: dict[str, bytes] | None = None,
        extra_active_runs: list[dict[str, object]] | None = None,
        main_sha: str = CURRENT_SHA,
        non_ancestor_shas: set[str] | None = None,
        mutate_target_on_read: dict[int, tuple[int, str]] | None = None,
        compare_mode: str = "single",
    ) -> None:
        self.controller = _controller()
        self.targets = {int(row["id"]): row for row in (targets or [_target()])}
        self.post_status = post_status
        self.terminal_on_post = terminal_on_post
        self.post_error = post_error
        self.rerun_after_post = rerun_after_post
        self.post_conclusion = post_conclusion
        self.jobs_error = jobs_error
        self.candidate_jobs = candidate_jobs or {}
        self.workflow_by_sha = workflow_by_sha or {}
        self.extra_active_runs = list(extra_active_runs or [])
        self.main_sha = main_sha
        self.non_ancestor_shas = non_ancestor_shas or set()
        self.mutate_target_on_read = mutate_target_on_read or {}
        self.compare_mode = compare_mode
        self.target_read_counts: dict[int, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.request_urls: list[str] = []
        self.compare_payloads: list[dict[str, object]] = []
        self.post_count = 0
        self.posted_run_ids: list[int] = []
        self.queued_list_calls = 0
        self.queued_total_override: int | None = None

    def __call__(self, request, timeout):  # type: ignore[no-untyped-def]
        method = request.get_method()
        parsed = urllib.parse.urlsplit(request.full_url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        self.calls.append((method, path))
        self.request_urls.append(request.full_url)
        if method == "POST":
            self.post_count += 1
            run_id = int(path.rsplit("/", 2)[1])
            self.posted_run_ids.append(run_id)
            assert request.get_header("Authorization") == "Bearer fixture-secret-token"
            if self.post_error:
                raise TimeoutError("simulated response loss")
            if self.rerun_after_post:
                row = self.targets[run_id]
                row["run_attempt"] = 2
                row["status"] = "queued"
                row["updated_at"] = "2026-10-10T10:00:00Z"
            elif self.terminal_on_post and self.post_status in {202, 204}:
                row = self.targets[run_id]
                row["status"] = "completed"
                row["conclusion"] = self.post_conclusion
                row["updated_at"] = "2026-10-10T10:00:00Z"
            return recovery.ApiResponse(self.post_status, {}, b"")

        payload: dict[str, object]
        if path == f"/repos/{recovery.REPOSITORY}":
            payload = {"id": REPOSITORY_ID, "full_name": recovery.REPOSITORY, "default_branch": "main"}
        elif path == f"/repos/{recovery.REPOSITORY}/branches/main":
            payload = {"name": "main", "commit": {"sha": self.main_sha}}
        elif path == f"/repos/{recovery.REPOSITORY}/actions/workflows/{WORKFLOW_ID}":
            payload = {
                "id": WORKFLOW_ID,
                "path": recovery.WORKFLOW_PATH,
                "state": "active",
            }
        elif path == f"/repos/{recovery.REPOSITORY}/actions/runs/{CONTROLLER_ID}":
            payload = self.controller
        elif path.startswith(f"/repos/{recovery.REPOSITORY}/actions/runs/") and "/attempts/" not in path:
            run_id = int(path.rsplit("/", 1)[1])
            if run_id in self.targets:
                self.target_read_counts[run_id] = self.target_read_counts.get(run_id, 0) + 1
                change = self.mutate_target_on_read.get(run_id)
                if change and self.target_read_counts[run_id] == change[0]:
                    self.targets[run_id]["status"] = change[1]
            payload = self.targets[run_id]
        elif path == f"/repos/{recovery.REPOSITORY}/actions/workflows/{WORKFLOW_ID}/runs":
            status = query.get("status", [""])[0]
            if status == "in_progress":
                rows = [self.controller, *self.extra_active_runs]
            elif status == "queued":
                self.queued_list_calls += 1
                rows = [row for row in self.targets.values() if row["status"] == "queued"]
            else:
                raise AssertionError(f"unexpected workflow run status: {status}")
            total = len(rows)
            if status == "queued" and self.queued_total_override is not None:
                total = self.queued_total_override
            payload = {"total_count": total, "workflow_runs": rows}
        elif path.startswith(f"/repos/{recovery.REPOSITORY}/actions/runs/") and "/attempts/" in path:
            parts = path.split("/")
            run_id, attempt = int(parts[-4]), int(parts[-2])
            if self.jobs_error and run_id != CONTROLLER_ID and self.post_count > 0:
                return recovery.ApiResponse(503, {}, b"unavailable")
            if run_id == CONTROLLER_ID and attempt == 1:
                jobs = [{
                    "id": 1,
                    "run_id": CONTROLLER_ID,
                    "run_attempt": 1,
                    "name": "reconcile",
                    "status": "in_progress",
                    "conclusion": None,
                }]
            else:
                # Exact original attempt only: a later attempt does not replace
                # the zero-job evidence for the originally reserved attempt.
                jobs = self.candidate_jobs.get(run_id, []) if attempt == 1 else []
            payload = {"total_count": len(jobs), "jobs": jobs}
        elif path == f"/repos/{recovery.REPOSITORY}/contents/{recovery.WORKFLOW_PATH}":
            source_sha = query.get("ref", [""])[0]
            raw = self.workflow_by_sha.get(source_sha, WORKFLOW_BYTES)
            payload = {
                "type": "file",
                "path": recovery.WORKFLOW_PATH,
                "encoding": "base64",
                "size": len(raw),
                "sha": _blob_sha(raw),
                "content": base64.b64encode(raw).decode("ascii"),
            }
        elif path.startswith(f"/repos/{recovery.REPOSITORY}/compare/"):
            base_sha, head_sha = path.rsplit("/", 1)[1].split("...", 1)
            assert base_sha in {str(row["head_sha"]) for row in self.targets.values()}
            assert head_sha == CURRENT_SHA
            page = int(query.get("page", ["1"])[0])
            non_ancestor = base_sha in self.non_ancestor_shas
            if non_ancestor:
                status, ahead_by, behind_by, total, merge_sha = "diverged", 1, 1, 2, "c" * 40
                commits = [{"sha": CURRENT_SHA}]
            elif self.compare_mode == "multi":
                status, ahead_by, behind_by, total, merge_sha = "ahead", 2, 0, 2, base_sha
                commits = [{"sha": "c" * 40}] if page == 1 else [{"sha": CURRENT_SHA}]
            elif self.compare_mode == "missing_link":
                status, ahead_by, behind_by, total, merge_sha = "ahead", 2, 0, 2, base_sha
                commits = [{"sha": "c" * 40}]
            elif self.compare_mode == "duplicate":
                status, ahead_by, behind_by, total, merge_sha = "ahead", 2, 0, 2, base_sha
                commits = [{"sha": "c" * 40}]
            else:
                status, ahead_by, behind_by, total, merge_sha = "ahead", 1, 0, 1, base_sha
                commits = [{"sha": CURRENT_SHA}]
            payload = {
                "url": f"{recovery.API_ROOT}{path}",
                "html_url": f"https://github.com/{recovery.REPOSITORY}/compare/{base_sha}...{head_sha}",
                "status": status,
                "ahead_by": ahead_by,
                "behind_by": behind_by,
                "total_commits": total,
                "base_commit": {"sha": base_sha},
                "merge_base_commit": {"sha": merge_sha},
                "commits": commits,
                "files": [],
            }
            self.compare_payloads.append(payload)
            headers = {"Content-Type": "application/json"}
            if self.compare_mode in {"multi", "duplicate"} and page == 1:
                next_commits = [{"sha": CURRENT_SHA}] if self.compare_mode == "multi" else [{"sha": "c" * 40}]
                if self.compare_mode == "duplicate":
                    payload["commits"] = [{"sha": "c" * 40}]
                self._next_compare_commits = next_commits
                headers["Link"] = (
                    f'<{recovery.API_ROOT}{path}?per_page=100&page=2>; rel="next"'
                )
            elif self.compare_mode in {"multi", "duplicate"} and page == 2:
                payload["commits"] = self._next_compare_commits
            return recovery.ApiResponse(
                200, headers,
                json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            )
        else:
            raise AssertionError(f"unexpected offline request: {method} {request.full_url}")
        return recovery.ApiResponse(
            200,
            {"Content-Type": "application/json"},
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )


class MemoryState:
    """Caller boundary fixture; real CAS is exercised by the local bare-Git test."""

    def __init__(self, records: list[dict[str, object]] | None = None) -> None:
        self.records = list(records or [])

    def read_snapshot(self):  # type: ignore[no-untyped-def]
        return object()

    def unresolved_reservations(self, snapshot):  # type: ignore[no-untyped-def]
        return [row for row in self.records if row["phase"] in {"reserved", "unresolved"}]

    def find_reservation(self, snapshot, identity):  # type: ignore[no-untyped-def]
        key = queue_state.target_key(identity)
        return next((row for row in self.records if queue_state.target_key(row["target"]) == key), None)

    def reserve(self, snapshot, record):  # type: ignore[no-untyped-def]
        queue_state._validate_record(dict(record))
        self.records.append(dict(record))

    def record_outcome(self, snapshot, identity, *, phase, outcome):  # type: ignore[no-untyped-def]
        key = queue_state.target_key(identity)
        record = next(row for row in self.records if queue_state.target_key(row["target"]) == key)
        record["phase"] = phase
        record["outcome"] = dict(outcome)


def _reservation_record() -> dict[str, object]:
    target = _target()
    controller = _controller()
    authority = recovery.ControllerAuthority(
        identity=recovery._extract_identity(controller),
        workflow_sha256=hashlib.sha256(WORKFLOW_BYTES).hexdigest(),
        workflow_bytes=WORKFLOW_BYTES,
        policy_sha256="c" * 64,
        repository_id=REPOSITORY_ID,
        workflow_id=WORKFLOW_ID,
    )
    target_identity = recovery._extract_identity(target)
    return recovery._reservation_record(
        target=target_identity,
        controller=authority,
        target_workflow_sha256=hashlib.sha256(WORKFLOW_BYTES).hexdigest(),
        reserved_at=NOW,
    )


def _policy():  # type: ignore[no-untyped-def]
    return recovery._parse_policy(ROOT)[0]


def _run_caller(monkeypatch, native, state=None):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(recovery, "_local_checkout_sha", lambda _root, **_kwargs: CURRENT_SHA)
    policy, _raw, digest = recovery._parse_policy(ROOT)
    deadline = time.monotonic() + policy["maximum_runtime_seconds"]
    api = recovery.GitHubApi(
        "fixture-secret-token",
        policy=policy,
        deadline=deadline,
        transport=native,
        sleep=lambda _seconds: None,
    )
    state = state or MemoryState()
    receipt = recovery._blank_receipt(NOW)
    outcome = recovery.perform_recovery(
        root=ROOT,
        env=_environment(),
        policy=policy,
        policy_sha256=digest,
        api=api,
        state_factory=lambda _deadline: state,
        now_fn=lambda: NOW,
        receipt=receipt,
    )
    return outcome, receipt, api, state


def test_eligible_stale_target_uses_one_post_then_exact_terminal_readback(monkeypatch):
    native = OfflineGitHub()
    outcome, receipt, api, state = _run_caller(monkeypatch, native)

    assert outcome == "native_cancel_confirmed"
    assert native.post_count == api.cancel_posts == 1
    assert receipt["request_disposition"] == "accepted_202"
    assert receipt["observations"][-1]["status"] == "completed"
    assert receipt["observations"][-1]["conclusion"] == "cancelled"
    assert receipt["observations"][-1]["job_count"] == 0
    assert state.records[0]["phase"] == "native_cancel_confirmed"
    assert state.records[0]["outcome"]["job_count"] == 0


def test_complete_caller_uses_actual_hardened_transport_over_local_bare_git(monkeypatch, tmp_path):
    from test_canonical_stale_queue_state_local_git import _local_store

    store, state, _bare, journal, owned_merge, foreign = _local_store(tmp_path)
    try:
        before = state.read_snapshot()
        native = OfflineGitHub()
        # Preserve test ownership of the store until assertions finish; the
        # production factory returns the store for perform_recovery to close.
        outcome, receipt, api, _state = _run_caller(monkeypatch, native, (None, state))
        after = state.read_snapshot()

        assert outcome == "native_cancel_confirmed"
        assert native.post_count == api.cancel_posts == 1
        assert receipt["request_disposition"] == "accepted_202"
        assert after.state_ref_sha != before.state_ref_sha
        assert store.changed_paths(
            before.state_ref_sha,
            after.state_ref_sha,
            deadline_monotonic=state.deadline_monotonic,
        ) == (queue_state.SIDECAR_PATH,)
        assert store.read_file(after.state_ref_sha, queue_state.JOURNAL_PATH, maximum=1024) == journal
        assert store.read_file(after.state_ref_sha, "reports/canonical-owned-c-merge-attempts.v1.json", maximum=1024) == owned_merge
        assert store.read_file(after.state_ref_sha, "reports/foreign-state-note.txt", maximum=1024) == foreign
        assert store._tree_entry(after.state_ref_sha, "reports/foreign-state-note.txt")[1] == "100755"
        record = queue_state.parse_sidecar(after.sidecar_bytes)["records"][0]
        assert record["phase"] == "native_cancel_confirmed"
        assert record["outcome"]["job_count"] == 0
        gate_root = tmp_path / "gate-root"
        _write_gate_receipt(gate_root, receipt)
        assert recovery_gate.check_final_gate(
            root=gate_root,
            env=_gate_env(),
            recovery_step_outcome="success",
            artifact_step_outcome="success",
        ) == "native_cancel_confirmed"
    finally:
        store.close()


def test_local_bare_git_restart_reloads_reserved_digest_and_passes_gate_without_second_post(
    monkeypatch, tmp_path,
):
    from test_canonical_stale_queue_state_local_git import _load_hardened_state_module, _local_store

    store, first_state, bare, journal, owned_merge, foreign = _local_store(tmp_path)
    fresh_store = None
    try:
        initial = first_state.read_snapshot()
        first_native = OfflineGitHub(
            terminal_on_post=True,
            workflow_by_sha={CURRENT_SHA: WORKFLOW_BYTES, TARGET_SHA: TARGET_WORKFLOW_BYTES},
        )

        def crash_before_outcome_persist(_snapshot, _identity, *, phase, outcome):  # type: ignore[no-untyped-def]
            raise SystemExit("simulated process crash after POST before outcome CAS")

        monkeypatch.setattr(first_state, "record_outcome", crash_before_outcome_persist)
        with pytest.raises(SystemExit, match="simulated process crash after POST"):
            _run_caller(monkeypatch, first_native, (None, first_state))
        assert first_native.post_count == 1
        reserved = first_state.read_snapshot()
        assert reserved.state_ref_sha != initial.state_ref_sha
        assert first_state.unresolved_reservations(reserved)[0]["phase"] == "reserved"
        stored_record = queue_state.parse_sidecar(reserved.sidecar_bytes)["records"][0]
        stored_target_digest = stored_record["evidence"]["target_workflow_sha256"]
        assert stored_target_digest == hashlib.sha256(TARGET_WORKFLOW_BYTES).hexdigest()
        assert store.changed_paths(
            initial.state_ref_sha, reserved.state_ref_sha,
            deadline_monotonic=first_state.deadline_monotonic,
        ) == (queue_state.SIDECAR_PATH,)
        assert store.read_file(reserved.state_ref_sha, queue_state.JOURNAL_PATH, maximum=1024) == journal
        assert store.read_file(reserved.state_ref_sha, "reports/canonical-owned-c-merge-attempts.v1.json", maximum=1024) == owned_merge
        assert store.read_file(reserved.state_ref_sha, "reports/foreign-state-note.txt", maximum=1024) == foreign
        assert store._tree_entry(reserved.state_ref_sha, "reports/foreign-state-note.txt")[1] == "100755"
    finally:
        store.close()

    policy = _policy()
    deadline = time.monotonic() + policy["maximum_runtime_seconds"]
    hardened = _load_hardened_state_module()
    fresh_store = hardened.GitObjectStore(
        ROOT,
        remote_url=str(bare),
        token=None,
        timeout_seconds=120.0,
        max_commands=600,
        max_output_bytes=192 * 1024 * 1024,
        max_blob_bytes=8 * 1024 * 1024,
        deadline_monotonic=deadline,
    )
    fresh_state = queue_state.CanonicalStaleQueueState(
        fresh_store, deadline_monotonic=deadline,
    )
    try:
        assert fresh_state is not first_state
        reloaded = fresh_state.read_snapshot()
        reloaded_record = queue_state.parse_sidecar(reloaded.sidecar_bytes)["records"][0]
        assert reloaded_record["phase"] == "reserved"
        assert reloaded_record["evidence"]["target_workflow_sha256"] == stored_target_digest

        terminal_target = _target()
        terminal_target["status"] = "completed"
        terminal_target["conclusion"] = "cancelled"
        terminal_target["updated_at"] = "2026-10-10T10:00:05Z"
        restart_native = OfflineGitHub(
            targets=[terminal_target],
            terminal_on_post=False,
            workflow_by_sha={CURRENT_SHA: WORKFLOW_BYTES},
        )
        restart_outcome, receipt, restart_api, _ = _run_caller(
            monkeypatch, restart_native, (None, fresh_state),
        )
        assert restart_outcome == "native_cancel_confirmed"
        assert restart_native.post_count == restart_api.cancel_posts == 0
        assert receipt["request_disposition"] == "unknown_after_restart"
        assert receipt["target_workflow_sha256"] == stored_target_digest
        assert receipt["target_workflow_sha256"] != receipt["controller_workflow_sha256"]

        confirmed = fresh_state.read_snapshot()
        confirmed_record = queue_state.parse_sidecar(confirmed.sidecar_bytes)["records"][0]
        assert confirmed_record["phase"] == "native_cancel_confirmed"
        assert confirmed_record["evidence"]["target_workflow_sha256"] == stored_target_digest
        assert fresh_store.changed_paths(
            reloaded.state_ref_sha, confirmed.state_ref_sha,
            deadline_monotonic=fresh_state.deadline_monotonic,
        ) == (queue_state.SIDECAR_PATH,)
        assert fresh_store.read_file(confirmed.state_ref_sha, queue_state.JOURNAL_PATH, maximum=1024) == journal
        assert fresh_store.read_file(confirmed.state_ref_sha, "reports/canonical-owned-c-merge-attempts.v1.json", maximum=1024) == owned_merge
        assert fresh_store.read_file(confirmed.state_ref_sha, "reports/foreign-state-note.txt", maximum=1024) == foreign
        assert fresh_store._tree_entry(confirmed.state_ref_sha, "reports/foreign-state-note.txt")[1] == "100755"

        gate_root = tmp_path / "gate-root"
        _write_gate_receipt(gate_root, receipt)
        assert recovery_gate.check_final_gate(
            root=gate_root, env=_gate_env(), recovery_step_outcome="success",
            artifact_step_outcome="success",
        ) == "native_cancel_confirmed"
    finally:
        if fresh_store is not None:
            fresh_store.close()


def test_real_captured_github_compare_response_has_no_top_level_head_commit():
    policy, _raw, _digest = recovery._parse_policy(ROOT)
    fixture_path = ROOT / "tests/fixtures/github-compare-response-c2a-feaa-contract.json"
    fixture_raw = fixture_path.read_bytes()
    fixture = json.loads(fixture_raw)
    assert "head_commit" not in fixture
    assert fixture["base_commit"]["sha"] == "c2a73621d11a32f84af047471adb5acf217ba8b4"
    assert fixture["merge_base_commit"]["sha"] == fixture["base_commit"]["sha"]
    assert fixture["commits"][-1]["sha"] == "feaa43860986bca5da942cde9880c7db58e0aa37"

    def captured_compare(request, _timeout):  # type: ignore[no-untyped-def]
        assert request.full_url == f"{fixture['url']}?per_page=100&page=1"
        return recovery.ApiResponse(200, {}, fixture_raw)

    api = recovery.GitHubApi(
        "fixture-secret-token",
        policy=policy,
        deadline=time.monotonic() + policy["maximum_runtime_seconds"],
        transport=captured_compare,
    )
    assert recovery._compare_ancestor(
        api,
        "feaa43860986bca5da942cde9880c7db58e0aa37",
        "c2a73621d11a32f84af047471adb5acf217ba8b4",
        policy,
    )


def test_captured_compare_contract_paginates_full_sha_commit_list_without_head_commit():
    policy, _raw, _digest = recovery._parse_policy(ROOT)
    native = OfflineGitHub(compare_mode="multi")
    api = recovery.GitHubApi(
        "fixture-secret-token",
        policy=policy,
        deadline=time.monotonic() + policy["maximum_runtime_seconds"],
        transport=native,
    )

    assert recovery._compare_ancestor(api, CURRENT_SHA, TARGET_SHA, policy)
    assert len(native.compare_payloads) == 2
    assert all("head_commit" not in payload for payload in native.compare_payloads)
    assert native.compare_payloads[0]["url"] == (
        f"{recovery.API_ROOT}/repos/{recovery.REPOSITORY}/compare/{TARGET_SHA}...{CURRENT_SHA}"
    )
    assert native.compare_payloads[-1]["commits"][-1]["sha"] == CURRENT_SHA


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("missing_link", "ancestry_commit_list_incomplete"), ("duplicate", "ancestry_commit_identity_invalid")],
)
def test_compare_partial_or_duplicate_pages_are_not_ancestor_evidence(mode, reason):
    policy, _raw, _digest = recovery._parse_policy(ROOT)
    native = OfflineGitHub(compare_mode=mode)
    api = recovery.GitHubApi(
        "fixture-secret-token",
        policy=policy,
        deadline=time.monotonic() + policy["maximum_runtime_seconds"],
        transport=native,
    )

    with pytest.raises(recovery.RecoveryError, match=reason):
        recovery._compare_ancestor(api, CURRENT_SHA, TARGET_SHA, policy)


def test_existing_reservation_is_readback_only_before_candidate_discovery(monkeypatch):
    native = OfflineGitHub(terminal_on_post=False)
    state = MemoryState([_reservation_record()])
    outcome, receipt, api, _state = _run_caller(monkeypatch, native, state)

    assert outcome == "reserved_reconcile_only"
    assert native.post_count == api.cancel_posts == 0
    assert native.queued_list_calls == 0
    assert receipt["reservation_state"] == "existing"
    assert receipt["request_disposition"] == "unknown_after_restart"


def test_ambiguous_cancel_timeout_restart_uses_persisted_target_digest_and_passes_gate(
    monkeypatch, tmp_path,
):
    state = MemoryState()
    first_native = OfflineGitHub(
        post_error=True,
        terminal_on_post=False,
        workflow_by_sha={CURRENT_SHA: WORKFLOW_BYTES, TARGET_SHA: TARGET_WORKFLOW_BYTES},
    )
    first_outcome, _first_receipt, first_api, _ = _run_caller(
        monkeypatch, first_native, state,
    )
    assert first_outcome == "reserved_reconcile_only"
    assert first_native.post_count == first_api.cancel_posts == 1
    assert state.records[0]["phase"] == "unresolved"
    stored_target_digest = state.records[0]["evidence"]["target_workflow_sha256"]
    assert stored_target_digest == hashlib.sha256(TARGET_WORKFLOW_BYTES).hexdigest()

    terminal_target = _target()
    terminal_target["status"] = "completed"
    terminal_target["conclusion"] = "cancelled"
    terminal_target["updated_at"] = "2026-10-10T10:00:05Z"
    restart_native = OfflineGitHub(
        targets=[terminal_target],
        terminal_on_post=False,
        workflow_by_sha={CURRENT_SHA: WORKFLOW_BYTES},
    )
    restart_outcome, receipt, restart_api, _ = _run_caller(
        monkeypatch, restart_native, state,
    )

    assert restart_outcome == "native_cancel_confirmed"
    assert restart_native.post_count == restart_api.cancel_posts == 0
    assert restart_native.queued_list_calls == 0
    assert state.records[0]["phase"] == "native_cancel_confirmed"
    assert receipt["target_workflow_sha256"] == stored_target_digest
    assert receipt["target_workflow_sha256"] != receipt["controller_workflow_sha256"]
    _write_gate_receipt(tmp_path, receipt)
    assert recovery_gate.check_final_gate(
        root=tmp_path, env=_gate_env(), recovery_step_outcome="success",
        artifact_step_outcome="success",
    ) == "native_cancel_confirmed"


def test_post_crash_restart_confirms_original_reservation_without_reposting(
    monkeypatch, tmp_path,
):
    class CrashOnceState(MemoryState):
        crash_once = True

        def record_outcome(self, snapshot, identity, *, phase, outcome):  # type: ignore[no-untyped-def]
            if self.crash_once:
                self.crash_once = False
                raise SystemExit("simulated process crash after native POST")
            super().record_outcome(snapshot, identity, phase=phase, outcome=outcome)

    state = CrashOnceState()
    first_native = OfflineGitHub(
        terminal_on_post=True,
        workflow_by_sha={CURRENT_SHA: WORKFLOW_BYTES, TARGET_SHA: TARGET_WORKFLOW_BYTES},
    )
    with pytest.raises(SystemExit, match="simulated process crash"):
        _run_caller(monkeypatch, first_native, state)
    assert first_native.post_count == 1
    assert state.records[0]["phase"] == "reserved"
    assert state.records[0]["outcome"] is None
    stored_target_digest = state.records[0]["evidence"]["target_workflow_sha256"]

    terminal_target = _target()
    terminal_target["status"] = "completed"
    terminal_target["conclusion"] = "cancelled"
    terminal_target["updated_at"] = "2026-10-10T10:00:05Z"
    restart_native = OfflineGitHub(
        targets=[terminal_target],
        terminal_on_post=False,
        workflow_by_sha={CURRENT_SHA: WORKFLOW_BYTES},
    )
    restart_outcome, receipt, restart_api, _ = _run_caller(
        monkeypatch, restart_native, state,
    )

    assert restart_outcome == "native_cancel_confirmed"
    assert restart_native.post_count == restart_api.cancel_posts == 0
    assert receipt["request_disposition"] == "unknown_after_restart"
    assert receipt["target_workflow_sha256"] == stored_target_digest
    assert state.records[0]["phase"] == "native_cancel_confirmed"
    _write_gate_receipt(tmp_path, receipt)
    assert recovery_gate.check_final_gate(
        root=tmp_path, env=_gate_env(), recovery_step_outcome="success",
        artifact_step_outcome="success",
    ) == "native_cancel_confirmed"


def test_existing_reservation_missing_original_workflow_proof_blocks_gate_without_post(
    monkeypatch, tmp_path,
):
    record = _reservation_record()
    record["phase"] = "unresolved"
    record["outcome"] = {
        "request_disposition": "transport_ambiguous",
        "observed_at": recovery._iso_utc(NOW),
        "run_status": "queued",
        "conclusion": None,
        "job_count": 0,
        "observation_count": 1,
    }
    del record["evidence"]["target_workflow_sha256"]
    native = OfflineGitHub(terminal_on_post=False)
    outcome, receipt, api, _ = _run_caller(
        monkeypatch, native, MemoryState([record]),
    )

    assert outcome == "blocked"
    assert native.post_count == api.cancel_posts == 0
    assert receipt["outcome"] == "blocked"
    assert receipt["blocked_reason"] == "reservation_target_workflow_proof_missing"
    assert receipt["target_workflow_sha256"] is None
    assert receipt["request_disposition"] == "unknown_after_restart"
    _write_gate_receipt(tmp_path, receipt)
    with pytest.raises(recovery_gate.GateError, match="receipt_outcome_not_successful"):
        recovery_gate.check_final_gate(
            root=tmp_path, env=_gate_env(), recovery_step_outcome="success",
            artifact_step_outcome="success",
        )


@pytest.mark.parametrize(
    ("native_kwargs", "expected_disposition"),
    [
        ({"post_status": 403, "terminal_on_post": False}, "denied"),
        ({"post_status": 200, "terminal_on_post": False}, "transport_ambiguous"),
        ({"post_error": True, "terminal_on_post": False}, "transport_ambiguous"),
        ({"post_status": 202, "terminal_on_post": False}, "accepted_202"),
    ],
)
def test_nonterminal_cancel_outcomes_remain_reserved_and_never_retry(
    monkeypatch, native_kwargs, expected_disposition,
):
    native = OfflineGitHub(**native_kwargs)
    outcome, receipt, api, state = _run_caller(monkeypatch, native)

    assert outcome == "reserved_reconcile_only"
    assert native.post_count == api.cancel_posts == 1
    assert receipt["request_disposition"] == expected_disposition
    assert state.records[0]["phase"] == "unresolved"
    assert state.records[0]["outcome"]["run_status"] == "queued"
    assert receipt["outcome"] == "reserved_reconcile_only"


def test_missing_jobs_readback_is_unknown_not_zero_and_cannot_confirm(monkeypatch):
    native = OfflineGitHub(jobs_error=True, terminal_on_post=True)
    outcome, receipt, api, state = _run_caller(monkeypatch, native)

    assert outcome == "reserved_reconcile_only"
    assert native.post_count == api.cancel_posts == 1
    assert receipt["observations"][-1]["job_count"] is None
    assert state.records[0]["phase"] == "unresolved"
    assert state.records[0]["outcome"]["job_count"] is None


@pytest.mark.parametrize("case", ["recent", "same_sha", "rerun", "has_jobs", "non_ancestor", "wrong_group"])
def test_ineligible_queued_candidates_are_noop_without_reservation_or_post(monkeypatch, tmp_path, case):
    target = _target()
    options: dict[str, object] = {}
    if case == "recent":
        target["created_at"] = "2026-10-09T10:01:00Z"
        target["updated_at"] = "2026-10-09T10:01:00Z"
    elif case == "same_sha":
        target["head_sha"] = CURRENT_SHA
    elif case == "rerun":
        target["run_attempt"] = 2
    elif case == "has_jobs":
        options["candidate_jobs"] = {TARGET_ID: [{"id": 11}]}
    elif case == "non_ancestor":
        options["non_ancestor_shas"] = {TARGET_SHA}
    elif case == "wrong_group":
        options["workflow_by_sha"] = {
            TARGET_SHA: WORKFLOW_BYTES.replace(b"canonical-update-promotion", b"different-group"),
        }
    native = OfflineGitHub(**options)
    native.targets[TARGET_ID] = target

    outcome, receipt, api, state = _run_caller(monkeypatch, native)

    assert outcome == "no_eligible_target"
    assert native.post_count == api.cancel_posts == 0
    assert state.records == []
    gate_root = tmp_path / case
    _write_gate_receipt(gate_root, receipt)
    assert recovery_gate.check_final_gate(
        root=gate_root,
        env=_gate_env(),
        recovery_step_outcome="success",
        artifact_step_outcome="success",
    ) == "no_eligible_target"
    if case == "recent":
        assert receipt["excluded_targets"][0]["reason"] == "recent"
    elif case == "same_sha":
        assert receipt["excluded_targets"][0]["reason"] == "current_sha"
    elif case == "rerun":
        assert receipt["excluded_targets"][0]["reason"] == "rerun"
    elif case == "has_jobs":
        assert receipt["excluded_targets"][0]["reason"] == "has_jobs"
    elif case == "non_ancestor":
        assert receipt["excluded_targets"][0]["reason"] == "non_ancestor"
    else:
        assert receipt["excluded_targets"][0]["reason"] == "workflow_group_mismatch"


def test_two_eligible_targets_choose_only_the_deterministic_oldest(monkeypatch):
    older = _target()
    newer = _run(
        701,
        sha="c" * 40,
        status="queued",
        created="2026-10-08T16:54:11Z",
        updated="2026-10-08T16:54:11Z",
    )
    native = OfflineGitHub(targets=[newer, older])

    outcome, receipt, api, state = _run_caller(monkeypatch, native)

    assert outcome == "native_cancel_confirmed"
    assert native.posted_run_ids == [TARGET_ID]
    assert api.cancel_posts == 1
    assert len(state.records) == 1
    assert state.records[0]["target"]["run_id"] == TARGET_ID
    assert receipt["excluded_targets"] == []


@pytest.mark.parametrize("mutation_call", [2, 3])
def test_target_starting_between_discovery_and_post_never_receives_cancel(monkeypatch, mutation_call):
    native = OfflineGitHub(mutate_target_on_read={TARGET_ID: (mutation_call, "in_progress")})
    state = MemoryState()

    if mutation_call == 2:
        with pytest.raises(recovery.RecoveryError):
            _run_caller(monkeypatch, native, state)
        assert state.records == []
    else:
        outcome, receipt, api, _state = _run_caller(monkeypatch, native, state)
        assert outcome == "blocked"
        assert receipt["reservation_state"] == "created"
        assert state.records[0]["phase"] == "reserved"
        assert api.cancel_posts == 0
    assert native.post_count == 0


def test_native_authority_contradictions_and_incomplete_pages_block_before_reservation(monkeypatch):
    contradictory = _run(
        901,
        sha="d" * 40,
        status="in_progress",
        created="2026-10-10T09:30:00Z",
        updated="2026-10-10T09:59:00Z",
    )
    manual_active = _run(
        902,
        sha="e" * 40,
        status="in_progress",
        created="2026-10-10T09:20:00Z",
        updated="2026-10-10T09:59:00Z",
    )
    manual_active["event"] = "workflow_dispatch"
    manual_active["head_branch"] = "review-branch"
    for index, native in enumerate((
        OfflineGitHub(extra_active_runs=[contradictory]),
        OfflineGitHub(extra_active_runs=[manual_active]),
        OfflineGitHub(main_sha="c" * 40),
    )):
        with pytest.raises(recovery.RecoveryError):
            _run_caller(monkeypatch, native)
        assert native.post_count == 0
        if index < 2:
            active_url = next(url for url in native.request_urls if "/runs?" in url and "status=in_progress" in url)
            assert "branch=" not in active_url and "event=" not in active_url

    incomplete = OfflineGitHub()
    incomplete.queued_total_override = 2
    with pytest.raises(recovery.RecoveryError, match="pagination"):
        _run_caller(monkeypatch, incomplete)
    assert incomplete.post_count == 0


def test_new_attempt_and_wrong_terminal_conclusion_never_become_success(monkeypatch):
    rerun = OfflineGitHub(rerun_after_post=True)
    outcome, receipt, api, state = _run_caller(monkeypatch, rerun)
    assert outcome == "reserved_reconcile_only"
    assert api.cancel_posts == 1
    assert receipt["observations"][-1]["run_attempt"] == 2
    assert receipt["blocked_reason"] == "target_new_attempt_observed"
    assert state.records[0]["phase"] == "unresolved"

    failed = OfflineGitHub(post_conclusion="failure")
    outcome, receipt, api, state = _run_caller(monkeypatch, failed)
    assert outcome == "reserved_reconcile_only"
    assert api.cancel_posts == 1
    assert receipt["observations"][-1]["conclusion"] == "failure"
    assert state.records[0]["phase"] == "unresolved"


def test_controller_source_requires_literal_workflow_level_group():
    for value in (
        b"concurrency: {group: ${{ github.workflow }}, cancel-in-progress: false}\njobs: {x: {runs-on: ubuntu-latest}}\n",
        b"concurrency: {group: canonical-update-promotion, cancel-in-progress: true}\njobs: {x: {runs-on: ubuntu-latest}}\n",
        b"jobs: {x: {runs-on: ubuntu-latest, concurrency: canonical-update-promotion}}\n",
    ):
        with pytest.raises(recovery.RecoveryError):
            recovery._yaml_group(value, expected_group="canonical-update-promotion")


def test_policy_and_receipt_schemas_are_closed_and_valid():
    policy, raw, digest = recovery._parse_policy(ROOT)
    assert hashlib.sha256(raw).hexdigest() == digest
    assert policy["minimum_age_seconds"] == 86400
    assert policy["maximum_compare_pages"] == 4
    assert policy["maximum_compare_commits"] == 400
    receipt = recovery._blank_receipt(NOW)
    receipt["finished_at"] = recovery._iso_utc(NOW)
    recovery._write_receipt(ROOT, {"GITHUB_RUN_ID": "987", "GITHUB_RUN_ATTEMPT": "1"}, receipt)
    path = ROOT / ".datapan/ci/canonical-stale-c-queue-recovery/987/1/receipt.json"
    try:
        assert json.loads(path.read_text("utf-8")) == receipt
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        path.parent.parent.rmdir()


def _write_gate_receipt(root: pathlib.Path, receipt: dict[str, object]) -> None:
    schema_parent = root / "schemas"
    schema_parent.mkdir(parents=True, exist_ok=True)
    (schema_parent / "datapan.canonical-stale-c-queue-recovery-receipt.v1.schema.json").write_bytes(
        (ROOT / "schemas/datapan.canonical-stale-c-queue-recovery-receipt.v1.schema.json").read_bytes(),
    )
    path = root / ".datapan/ci/canonical-stale-c-queue-recovery/900/1/receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _valid_no_action_receipt() -> dict[str, object]:
    receipt = recovery._blank_receipt(NOW)
    receipt.update({
        "outcome": "no_eligible_target",
        "blocked_reason": None,
        "controller": {
            "repository_id": REPOSITORY_ID,
            "workflow_id": WORKFLOW_ID,
            "run_id": CONTROLLER_ID,
            "run_attempt": 1,
            "event": "schedule",
            "branch": "main",
            "head_sha": CURRENT_SHA,
            "job_name": "reconcile",
        },
        "policy_sha256": "c" * 64,
        "controller_workflow_sha256": "d" * 64,
        "elapsed_ms": 5,
        "finished_at": recovery._iso_utc(NOW),
    })
    return receipt


def _gate_env() -> dict[str, str]:
    return {
        **_environment(),
        "GITHUB_REPOSITORY_ID": str(REPOSITORY_ID),
        "QUEUE_RECOVERY_STEP_OUTCOME": "success",
        "QUEUE_RECEIPT_ARTIFACT_STEP_OUTCOME": "success",
    }


def test_scheduled_final_gate_accepts_valid_no_action_and_skips_other_events(tmp_path):
    receipt = _valid_no_action_receipt()
    _write_gate_receipt(tmp_path, receipt)
    assert recovery_gate.check_final_gate(
        root=tmp_path, env=_gate_env(), recovery_step_outcome="success",
        artifact_step_outcome="success",
    ) == "no_eligible_target"

    non_schedule = {**_gate_env(), "GITHUB_EVENT_NAME": "workflow_run"}
    assert recovery_gate.check_final_gate(
        root=tmp_path, env=non_schedule, recovery_step_outcome="skipped",
        artifact_step_outcome="skipped",
    ) is None


def test_scheduled_final_gate_rejects_missing_receipt_and_missing_controller(tmp_path):
    with pytest.raises(recovery_gate.GateError, match="receipt_file_unavailable"):
        recovery_gate.check_final_gate(
            root=tmp_path, env=_gate_env(), recovery_step_outcome="success",
            artifact_step_outcome="success",
        )

    receipt = _valid_no_action_receipt()
    receipt["controller"] = recovery._blank_receipt(NOW)["controller"]
    _write_gate_receipt(tmp_path, receipt)
    with pytest.raises(recovery_gate.GateError, match="receipt_controller_identity_or_proof_mismatch"):
        recovery_gate.check_final_gate(
            root=tmp_path, env=_gate_env(), recovery_step_outcome="success",
            artifact_step_outcome="success",
        )


def test_scheduled_final_gate_fails_blocked_recovery_and_missing_artifact(tmp_path):
    blocked = _valid_no_action_receipt()
    blocked["outcome"] = "blocked"
    blocked["blocked_reason"] = "queue_state_unavailable"
    _write_gate_receipt(tmp_path, blocked)
    with pytest.raises(recovery_gate.GateError, match="scheduled_recovery_step_failed"):
        recovery_gate.check_final_gate(
            root=tmp_path, env=_gate_env(), recovery_step_outcome="failure",
            artifact_step_outcome="success",
        )
    with pytest.raises(recovery_gate.GateError, match="scheduled_receipt_artifact_step_failed"):
        recovery_gate.check_final_gate(
            root=tmp_path, env=_gate_env(), recovery_step_outcome="success",
            artifact_step_outcome="failure",
        )
    with pytest.raises(recovery_gate.GateError, match="receipt_outcome_not_successful"):
        recovery_gate.check_final_gate(
            root=tmp_path, env=_gate_env(), recovery_step_outcome="success",
            artifact_step_outcome="success",
        )


def test_scheduled_final_gate_rejects_reserved_reconcile_only_receipt(tmp_path):
    receipt = _valid_no_action_receipt()
    receipt.update({
        "outcome": "reserved_reconcile_only",
        "blocked_reason": "terminal_readback_exhausted",
        "target": {
            "repository_id": REPOSITORY_ID,
            "workflow_id": WORKFLOW_ID,
            "run_id": TARGET_ID,
            "run_attempt": 1,
            "event": "schedule",
            "branch": "main",
            "head_sha": TARGET_SHA,
            "created_at": "2026-10-07T16:54:11Z",
            "updated_at": "2026-10-07T16:54:11Z",
        },
        "reservation_state": "created",
        "request_disposition": "accepted_202",
        "target_workflow_sha256": "e" * 64,
        "observations": [{
            "observed_at": recovery._iso_utc(NOW),
            "run_id": TARGET_ID,
            "run_attempt": 1,
            "status": "queued",
            "conclusion": None,
            "head_sha": TARGET_SHA,
            "job_count": 0,
        }],
    })
    _write_gate_receipt(tmp_path, receipt)
    with pytest.raises(recovery_gate.GateError, match="receipt_outcome_not_successful"):
        recovery_gate.check_final_gate(
            root=tmp_path, env=_gate_env(), recovery_step_outcome="success",
            artifact_step_outcome="success",
        )


def test_scheduled_final_gate_accepts_only_exact_cancel_terminal_readback(tmp_path):
    receipt = _valid_no_action_receipt()
    receipt.update({
        "outcome": "native_cancel_confirmed",
        "target": {
            "repository_id": REPOSITORY_ID,
            "workflow_id": WORKFLOW_ID,
            "run_id": TARGET_ID,
            "run_attempt": 1,
            "event": "schedule",
            "branch": "main",
            "head_sha": TARGET_SHA,
            "created_at": "2026-10-07T16:54:11Z",
            "updated_at": "2026-10-07T16:54:11Z",
        },
        "reservation_state": "confirmed",
        "request_disposition": "accepted_202",
        "target_workflow_sha256": "e" * 64,
        "observations": [{
            "observed_at": recovery._iso_utc(NOW),
            "run_id": TARGET_ID,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "cancelled",
            "head_sha": TARGET_SHA,
            "job_count": 0,
        }],
    })
    _write_gate_receipt(tmp_path, receipt)
    assert recovery_gate.check_final_gate(
        root=tmp_path, env=_gate_env(), recovery_step_outcome="success",
        artifact_step_outcome="success",
    ) == "native_cancel_confirmed"

    receipt["observations"][-1]["job_count"] = None  # type: ignore[index]
    _write_gate_receipt(tmp_path, receipt)
    with pytest.raises(recovery_gate.GateError, match="receipt_schema_or_json_invalid|receipt_terminal_confirmation_invalid"):
        recovery_gate.check_final_gate(
            root=tmp_path, env=_gate_env(), recovery_step_outcome="success",
            artifact_step_outcome="success",
        )


def test_duplicate_api_json_keys_fail_closed():
    with pytest.raises(recovery.RecoveryError, match="duplicate"):
        recovery.GitHubApi._strict_json(b'{"total_count":0,"total_count":1}')


def test_late_injected_transport_response_is_rejected_at_original_deadline():
    policy, _raw, _digest = recovery._parse_policy(ROOT)
    now = [10.0]

    def delayed_response(_request, _timeout):  # type: ignore[no-untyped-def]
        now[0] = 11.01
        return recovery.ApiResponse(200, {}, b"{}")

    api = recovery.GitHubApi(
        "fixture-secret-token", policy=policy, deadline=11.0,
        transport=delayed_response, monotonic=lambda: now[0],
    )
    with pytest.raises(recovery.RecoveryError, match="invocation_deadline_expired"):
        api.get_json("/repos/StatPan/datapan-registry", maximum=1024)


def test_production_http_body_read_is_interrupted_at_absolute_deadline(monkeypatch):
    policy, _raw, _digest = recovery._parse_policy(ROOT)

    class SlowBody:
        status = 200

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return False

        @property
        def headers(self):  # type: ignore[no-untyped-def]
            return {}

        def read(self, _maximum):  # type: ignore[no-untyped-def]
            time.sleep(0.6)
            return b"{}"

    class SlowOpener:
        def open(self, _request, timeout):  # type: ignore[no-untyped-def]
            assert 0 < timeout < 0.5
            return SlowBody()

    monkeypatch.setattr(recovery.urllib.request, "build_opener", lambda *_handlers: SlowOpener())
    api = recovery.GitHubApi(
        "fixture-secret-token", policy=policy,
        deadline=time.monotonic() + 0.15,
    )
    started = time.monotonic()
    with pytest.raises(recovery.RecoveryError, match="api_read_failed"):
        api.get_json("/repos/StatPan/datapan-registry", maximum=1024)
    assert time.monotonic() - started < 0.5


def test_absolute_http_timeout_requires_unowned_main_thread_alarm():
    policy, _raw, _digest = recovery._parse_policy(ROOT)
    api = recovery.GitHubApi(
        "fixture-secret-token", policy=policy, deadline=time.monotonic() + 1,
    )
    original = threading.current_thread
    try:
        class NotMainThread:
            pass

        recovery.threading.current_thread = lambda: NotMainThread()  # type: ignore[assignment]
        with pytest.raises(recovery.RecoveryError, match="absolute_api_deadline_unavailable"):
            with api._absolute_timeout(0.1):
                raise AssertionError("deadline guard unexpectedly yielded")
    finally:
        recovery.threading.current_thread = original  # type: ignore[assignment]


def test_fixed_sidecar_accepts_unknown_job_count_only_for_unresolved_phase():
    record = _reservation_record()
    record["phase"] = "unresolved"
    record["outcome"] = {
        "request_disposition": "unknown_after_restart",
        "observed_at": "2026-10-10T10:00:00Z",
        "run_status": "completed",
        "conclusion": "failure",
        "job_count": None,
        "observation_count": 1,
    }
    raw = queue_state.canonical_json_bytes({
        "schema_version": queue_state.STATE_SCHEMA,
        "repository": queue_state.REPOSITORY,
        "records": [record],
    })
    assert queue_state.parse_sidecar(raw)["records"][0]["outcome"]["job_count"] is None

    record["phase"] = "native_cancel_confirmed"
    with pytest.raises(queue_state.QueueStateError):
        queue_state.parse_sidecar(queue_state.canonical_json_bytes({
            "schema_version": queue_state.STATE_SCHEMA,
            "repository": queue_state.REPOSITORY,
            "records": [record],
        }))
