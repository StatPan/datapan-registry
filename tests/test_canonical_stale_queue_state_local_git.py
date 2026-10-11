from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import time

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import canonical_stale_queue_state as queue_state  # noqa: E402


STATE_REF = "refs/heads/automation/canonical-update-state"
STATE_BRANCH = "automation/canonical-update-state"
JOURNAL_PATH = "reports/canonical-update-promotion-receipt.json"
OWNED_MERGE_PATH = "reports/canonical-owned-c-merge-attempts.v1.json"
FOREIGN_PATH = "reports/foreign-state-note.txt"


def _git(*args: str, cwd: pathlib.Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"},
    )
    return completed.stdout.decode("ascii").strip()


def _load_hardened_state_module():  # type: ignore[no-untyped-def]
    source = ROOT / "scripts/canonical_publication_state.py"
    if not source.is_file():
        pytest.fail("the exact hardened canonical_publication_state.py is required in the deployable dependency closure")
    spec = importlib.util.spec_from_file_location("canonical_publication_state", source)
    if spec is None or spec.loader is None:
        pytest.fail("could not load the exact hardened state source")
    module = importlib.util.module_from_spec(spec)
    sys.modules["canonical_publication_state"] = module
    spec.loader.exec_module(module)
    return module


def _record() -> dict[str, object]:
    return {
        "schema_version": queue_state.RECORD_SCHEMA,
        "target": {
            "repository_id": 12345,
            "repository": queue_state.REPOSITORY,
            "workflow_id": 22,
            "workflow_path": ".github/workflows/canonical-update-promotion.yml",
            "run_id": 700,
            "run_attempt": 1,
            "event": "schedule",
            "branch": "main",
            "head_sha": "b" * 40,
            "created_at": "2026-10-07T16:54:11Z",
            "updated_at": "2026-10-07T16:54:11Z",
        },
        "phase": "reserved",
        "reserved_at": "2026-10-10T10:00:00Z",
        "controller": {
            "repository_id": 12345,
            "repository": queue_state.REPOSITORY,
            "workflow_id": 22,
            "workflow_path": ".github/workflows/canonical-update-promotion.yml",
            "run_id": 900,
            "run_attempt": 1,
            "event": "schedule",
            "branch": "main",
            "head_sha": "a" * 40,
        },
        "evidence": {
            "default_branch_sha": "a" * 40,
            "concurrency_group": "canonical-update-promotion",
            "policy_sha256": "c" * 64,
            "controller_workflow_sha256": "d" * 64,
            "target_workflow_sha256": "e" * 64,
        },
        "outcome": None,
    }


def _local_store(tmp_path: pathlib.Path):  # type: ignore[no-untyped-def]
    hardened = _load_hardened_state_module()
    bare = tmp_path / "remote.git"
    work = tmp_path / "state-source"
    _git("init", "--bare", str(bare))
    _git("init", str(work))
    _git("config", "user.name", "state test", cwd=work)
    _git("config", "user.email", "state-test@example.invalid", cwd=work)
    journal = b'{"fixture":"promotion-journal","sequence":4}\n'
    owned_merge = b'{"fixture":"owned-merge-sidecar","records":[]}\n'
    foreign = b"foreign tree entry must survive\n"
    (work / "reports").mkdir()
    (work / JOURNAL_PATH).write_bytes(journal)
    (work / OWNED_MERGE_PATH).write_bytes(owned_merge)
    (work / FOREIGN_PATH).write_bytes(foreign)
    (work / FOREIGN_PATH).chmod(0o755)
    _git("add", "reports", cwd=work)
    _git("commit", "-m", "initial state fixture", cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "origin", f"HEAD:{STATE_BRANCH}", cwd=work)
    store = hardened.GitObjectStore(
        ROOT,
        remote_url=str(bare),
        token=None,
        timeout_seconds=120.0,
        max_commands=600,
        max_output_bytes=192 * 1024 * 1024,
        max_blob_bytes=8 * 1024 * 1024,
        deadline_monotonic=time.monotonic() + 120,
    )
    state = queue_state.CanonicalStaleQueueState(
        store, deadline_monotonic=time.monotonic() + 120,
    )
    return store, state, bare, journal, owned_merge, foreign


def test_production_factory_pins_reviewed_repository_and_original_deadline():
    deadline = time.monotonic() + 120.0
    store, state = queue_state.create_hardened_store(
        repository_root=str(ROOT), token="fixture-token",
        deadline_monotonic=deadline,
    )
    try:
        assert store.remote_url == "https://github.com/StatPan/datapan-registry.git"
        assert store.budget.max_commands == 600
        assert store.budget.max_output_bytes == 192 * 1024 * 1024
        assert store.max_blob_bytes == 8 * 1024 * 1024
        assert store.budget.deadline == deadline
        assert state.deadline_monotonic == deadline
    finally:
        store.close()


def test_fixed_queue_cas_preserves_journal_owned_merge_modes_and_unrelated_tree(tmp_path):
    store, state, _bare, journal, owned_merge, foreign = _local_store(tmp_path)
    try:
        initial = state.read_snapshot()
        assert initial.journal_bytes == journal
        assert initial.sidecar_mode is None

        state.reserve(initial, _record())
        reserved = state.read_snapshot()
        assert reserved.state_ref_sha != initial.state_ref_sha
        assert reserved.journal_bytes == journal
        assert reserved.journal_mode == "100644"
        assert reserved.sidecar_mode == "100644"
        assert store.changed_paths(
            initial.state_ref_sha,
            reserved.state_ref_sha,
            deadline_monotonic=state.deadline_monotonic,
        ) == (queue_state.SIDECAR_PATH,)
        assert store.read_file(reserved.state_ref_sha, JOURNAL_PATH, maximum=1024) == journal
        assert store.read_file(reserved.state_ref_sha, OWNED_MERGE_PATH, maximum=1024) == owned_merge
        assert store.read_file(reserved.state_ref_sha, FOREIGN_PATH, maximum=1024) == foreign
        assert store._tree_entry(reserved.state_ref_sha, FOREIGN_PATH)[1] == "100755"

        unresolved = {
            "request_disposition": "transport_ambiguous",
            "observed_at": "2026-10-10T10:00:01Z",
            "run_status": "queued",
            "conclusion": None,
            "job_count": 0,
            "observation_count": 3,
        }
        state.record_outcome(
            reserved, _record()["target"], phase="unresolved", outcome=unresolved,
        )
        after_unresolved = state.read_snapshot()
        terminal = {
            "request_disposition": "transport_ambiguous",
            "observed_at": "2026-10-10T10:00:05Z",
            "run_status": "completed",
            "conclusion": "cancelled",
            "job_count": 0,
            "observation_count": 1,
        }
        state.record_outcome(
            after_unresolved, _record()["target"],
            phase="native_cancel_confirmed", outcome=terminal,
        )
        confirmed = state.read_snapshot()
        value = queue_state.parse_sidecar(confirmed.sidecar_bytes)
        assert value["records"][0]["phase"] == "native_cancel_confirmed"
        assert value["records"][0]["outcome"] == terminal
        assert store.read_file(confirmed.state_ref_sha, JOURNAL_PATH, maximum=1024) == journal
        assert store.read_file(confirmed.state_ref_sha, OWNED_MERGE_PATH, maximum=1024) == owned_merge
        assert store.read_file(confirmed.state_ref_sha, FOREIGN_PATH, maximum=1024) == foreign
        assert store._tree_entry(confirmed.state_ref_sha, FOREIGN_PATH)[1] == "100755"
    finally:
        store.close()


def test_stale_snapshot_conflicts_before_queue_sidecar_write(tmp_path):
    store, state, _bare, _journal, _owned_merge, _foreign = _local_store(tmp_path)
    try:
        snapshot = state.read_snapshot()
        # A competing exact state writer advances the real local bare remote
        # after our snapshot and before the queue adapter attempts its CAS.
        store._commit_updates(
            expected_sha=snapshot.state_ref_sha,
            base_sha=snapshot.state_ref_sha,
            updates={"reports/competing-update.txt": b"competing state update\n"},
            create_only=frozenset({"reports/competing-update.txt"}),
            message="competing state update",
        )
        after_competitor = state.read_snapshot()
        assert after_competitor.state_ref_sha != snapshot.state_ref_sha
        with pytest.raises(queue_state.QueueStateConflict):
            state.reserve(snapshot, _record())
        final = state.read_snapshot()
        assert final.state_ref_sha == after_competitor.state_ref_sha
        assert final.sidecar_mode is None
        assert store._tree_entry(final.state_ref_sha, "reports/competing-update.txt") is not None
    finally:
        store.close()
