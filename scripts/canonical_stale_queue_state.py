#!/usr/bin/env python3
"""Fixed-path reservation storage for stale canonical C queue recovery.

This adapter composes the hardened ``GitObjectStore`` supplied by
``canonical_publication_state``.  It deliberately does not use that module's
owned-merge sidecar methods: queue reservations have a separate schema, path,
empty-value digest, and successor rule.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


REPOSITORY = "StatPan/datapan-registry"
STATE_REF = "refs/heads/automation/canonical-update-state"
JOURNAL_PATH = "reports/canonical-update-promotion-receipt.json"
SIDECAR_PATH = "reports/canonical-stale-c-queue-recovery.v1.json"
STATE_SCHEMA = "datapan.canonical-stale-c-queue-recovery.v1"
RECORD_SCHEMA = "datapan.canonical-stale-c-queue-reservation.v1"
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_SIDECAR_BYTES = 256 * 1024
MAX_RECORDS = 64
SHA1_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
EMPTY_SIDECAR_BYTES = (
    b'{"records":[],"repository":"StatPan/datapan-registry",'
    b'"schema_version":"datapan.canonical-stale-c-queue-recovery.v1"}'
)


class QueueStateError(RuntimeError):
    """The fixed queue sidecar cannot be read or safely changed."""


class QueueStateConflict(QueueStateError):
    """The state branch or exact queue snapshot changed before CAS."""


class QueueStateOutcomeUncertain(QueueStateError):
    """A state push may have started, but exact child readback did not finish."""


class HardenedStore(Protocol):
    """Minimum reviewed interface implemented by canonical_publication_state."""

    def bounded_deadline(self, deadline_monotonic: float): ...
    def remote_sha(self, ref: str = STATE_REF) -> str | None: ...
    def _tree_entry(self, commit_sha: str, path: str) -> tuple[str, str, int] | None: ...
    def read_file(self, commit_sha: str, path: str, *, maximum: int) -> bytes: ...
    def changed_paths(self, base_sha: str, head_sha: str, *, deadline_monotonic: float) -> tuple[str, ...]: ...
    def tree_sha(self, commit_sha: str, *, deadline_monotonic: float) -> str: ...
    def _commit_updates(
        self, *, expected_sha: str | None, base_sha: str, updates: Mapping[str, bytes],
        create_only: frozenset[str] = frozenset(), message: str,
        update_modes: Mapping[str, str] | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class QueueStateSnapshot:
    state_ref_sha: str
    journal_bytes: bytes
    journal_mode: str
    sidecar_bytes: bytes
    sidecar_mode: str | None
    sidecar_sha256: str


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise QueueStateError("queue state value is not canonical JSON") from exc


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QueueStateError("queue state JSON contains a duplicate key")
        result[key] = value
    return result


def _timestamp(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise QueueStateError(f"{label} must be an explicit UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise QueueStateError(f"{label} is malformed") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise QueueStateError(f"{label} must be UTC")


def _positive_integer(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise QueueStateError(f"{label} must be a positive integer")


def _identity(value: Any, *, controller: bool = False) -> None:
    if not isinstance(value, dict):
        raise QueueStateError("queue reservation identity is not an object")
    expected = {
        "repository_id", "repository", "workflow_id", "workflow_path", "run_id",
        "run_attempt", "event", "branch", "head_sha",
    }
    if not controller:
        expected.update({"created_at", "updated_at"})
    if set(value) != expected:
        raise QueueStateError("queue reservation identity has unknown or missing fields")
    _positive_integer(value["repository_id"], "repository id")
    _positive_integer(value["workflow_id"], "workflow id")
    _positive_integer(value["run_id"], "run id")
    _positive_integer(value["run_attempt"], "run attempt")
    if value["repository"] != REPOSITORY:
        raise QueueStateError("queue reservation repository identity differs")
    if value["workflow_path"] != ".github/workflows/canonical-update-promotion.yml":
        raise QueueStateError("queue reservation workflow path differs")
    if value["event"] != "schedule" or value["branch"] != "main":
        raise QueueStateError("queue reservation target is not a scheduled main run")
    if not isinstance(value["head_sha"], str) or not SHA1_RE.fullmatch(value["head_sha"]):
        raise QueueStateError("queue reservation head SHA is malformed")
    if not controller:
        if value["run_attempt"] != 1:
            raise QueueStateError("queue reservation target must be attempt one")
        _timestamp(value["created_at"], "target created_at")
        _timestamp(value["updated_at"], "target updated_at")
        if datetime.fromisoformat(value["created_at"].replace("Z", "+00:00")) > datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00")):
            raise QueueStateError("queue target update time precedes creation")


def _validate_outcome(value: Any, phase: str) -> None:
    if value is None:
        if phase != "reserved":
            raise QueueStateError("non-reserved queue state requires a readback outcome")
        return
    if phase == "reserved" or not isinstance(value, dict):
        raise QueueStateError("queue readback outcome has an invalid phase")
    expected = {
        "request_disposition", "observed_at", "run_status", "conclusion",
        "job_count", "observation_count",
    }
    if set(value) != expected:
        raise QueueStateError("queue readback outcome has unknown or missing fields")
    if value["request_disposition"] not in {
        "accepted_202", "accepted_204", "denied", "transport_ambiguous",
        "not_attempted", "unknown_after_restart",
    }:
        raise QueueStateError("queue readback request disposition is invalid")
    _timestamp(value["observed_at"], "queue readback observed_at")
    if value["run_status"] not in {
        "queued", "in_progress", "completed", "waiting", "requested", "pending", "unknown",
    }:
        raise QueueStateError("queue readback status is invalid")
    if value["conclusion"] is not None and (
        not isinstance(value["conclusion"], str) or len(value["conclusion"]) > 80
    ):
        raise QueueStateError("queue readback conclusion is invalid")
    # None means that a complete jobs response could not be established. It
    # must never be collapsed to zero, which is the proof required for success.
    if value["job_count"] is not None and (
        isinstance(value["job_count"], bool)
        or not isinstance(value["job_count"], int)
        or not 0 <= value["job_count"] <= 1000
    ):
        raise QueueStateError("queue readback job count is invalid")
    if (
        isinstance(value["observation_count"], bool)
        or not isinstance(value["observation_count"], int)
        or not 1 <= value["observation_count"] <= 3
    ):
        raise QueueStateError("queue readback observation count is invalid")
    if phase == "native_cancel_confirmed" and not (
        value["run_status"] == "completed"
        and value["conclusion"] == "cancelled"
        and value["job_count"] == 0
    ):
        raise QueueStateError("cancel-confirmed state lacks exact terminal and zero-job proof")


def _validate_record(record: Any) -> None:
    if not isinstance(record, dict) or set(record) != {
        "schema_version", "target", "phase", "reserved_at", "controller", "evidence", "outcome",
    }:
        raise QueueStateError("queue reservation record has unknown or missing fields")
    if record["schema_version"] != RECORD_SCHEMA:
        raise QueueStateError("queue reservation record schema is unsupported")
    _identity(record["target"])
    _identity(record["controller"], controller=True)
    if record["controller"]["run_attempt"] < 1:
        raise QueueStateError("queue controller attempt is invalid")
    _timestamp(record["reserved_at"], "reservation timestamp")
    if record["phase"] not in {"reserved", "native_cancel_confirmed", "unresolved"}:
        raise QueueStateError("queue reservation phase is invalid")
    evidence = record["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "default_branch_sha", "concurrency_group", "policy_sha256",
        "controller_workflow_sha256", "target_workflow_sha256",
    }:
        raise QueueStateError("queue reservation evidence has unknown or missing fields")
    if not isinstance(evidence["default_branch_sha"], str) or not SHA1_RE.fullmatch(evidence["default_branch_sha"]):
        raise QueueStateError("queue reservation branch SHA is malformed")
    if evidence["concurrency_group"] != "canonical-update-promotion":
        raise QueueStateError("queue reservation concurrency group differs")
    for field in ("policy_sha256", "controller_workflow_sha256", "target_workflow_sha256"):
        if not isinstance(evidence[field], str) or not SHA256_RE.fullmatch(evidence[field]):
            raise QueueStateError(f"queue reservation {field} is malformed")
    if (
        record["controller"]["repository_id"] != record["target"]["repository_id"]
        or record["controller"]["workflow_id"] != record["target"]["workflow_id"]
        or record["controller"]["run_id"] == record["target"]["run_id"]
        or evidence["default_branch_sha"] != record["controller"]["head_sha"]
    ):
        raise QueueStateError("queue controller and target authority identities do not agree")
    _validate_outcome(record["outcome"], record["phase"])


def reservation_target_workflow_sha256(record: Mapping[str, Any]) -> str:
    """Return only the target workflow digest from a fully validated reservation."""
    try:
        value = dict(record)
    except (TypeError, ValueError) as exc:
        raise QueueStateError("queue reservation record is not a mapping") from exc
    _validate_record(value)
    return value["evidence"]["target_workflow_sha256"]


def parse_sidecar(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_SIDECAR_BYTES:
        raise QueueStateError("queue sidecar is outside its fixed byte bound")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QueueStateError("queue sidecar is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "repository", "records"}:
        raise QueueStateError("queue sidecar has unknown or missing top-level fields")
    if value["schema_version"] != STATE_SCHEMA or value["repository"] != REPOSITORY:
        raise QueueStateError("queue sidecar identity is unsupported")
    records = value["records"]
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise QueueStateError("queue reservation history exceeds its fixed bound")
    seen: set[tuple[int, int, int, int]] = set()
    for record in records:
        _validate_record(record)
        target = record["target"]
        key = (
            target["repository_id"], target["workflow_id"],
            target["run_id"], target["run_attempt"],
        )
        if key in seen:
            raise QueueStateError("queue sidecar contains a duplicate target reservation")
        seen.add(key)
    if canonical_json_bytes(value) != raw:
        raise QueueStateError("queue sidecar bytes are not canonical JSON")
    return value


def target_key(identity: Mapping[str, Any]) -> tuple[int, int, int, int]:
    return (
        identity["repository_id"], identity["workflow_id"],
        identity["run_id"], identity["run_attempt"],
    )


def validate_successor(previous_raw: bytes, next_raw: bytes) -> None:
    previous = parse_sidecar(previous_raw)
    following = parse_sidecar(next_raw)
    old_records = previous["records"]
    new_records = following["records"]
    old_by_key = {target_key(record["target"]): record for record in old_records}
    new_by_key = {target_key(record["target"]): record for record in new_records}
    if len(new_records) == len(old_records) + 1:
        if new_records[:-1] != old_records:
            raise QueueStateError("new queue reservation must append without rewriting history")
        if target_key(new_records[-1]["target"]) in old_by_key:
            raise QueueStateError("queue target already has a durable reservation")
        if new_records[-1]["phase"] != "reserved" or new_records[-1]["outcome"] is not None:
            raise QueueStateError("new queue reservation must begin readback-only")
        return
    if len(new_records) != len(old_records):
        raise QueueStateError("queue sidecar successor cannot remove or reorder history")
    if list(new_by_key) != list(old_by_key):
        raise QueueStateError("queue sidecar successor changed reservation order")
    changed = [key for key in old_by_key if old_by_key[key] != new_by_key[key]]
    if len(changed) != 1:
        raise QueueStateError("queue sidecar successor must update exactly one reservation")
    key = changed[0]
    before = old_by_key[key]
    after = new_by_key[key]
    for field in ("schema_version", "target", "reserved_at", "controller", "evidence"):
        if before[field] != after[field]:
            raise QueueStateError("queue reservation identity and evidence are immutable")
    if before["phase"] == "reserved":
        if (
            before["outcome"] is not None
            or after["phase"] not in {"unresolved", "native_cancel_confirmed"}
            or after["outcome"] is None
        ):
            raise QueueStateError("reserved queue outcome transition is not monotonic")
        return
    if before["phase"] == "unresolved":
        if after["phase"] != "native_cancel_confirmed" or after["outcome"] is None:
            raise QueueStateError("unresolved queue reservation may only become confirmed")
        if before["outcome"] is None or (
            before["outcome"]["request_disposition"]
            != after["outcome"]["request_disposition"]
        ):
            raise QueueStateError("readback-only confirmation changed the original request disposition")
        return
    raise QueueStateError("queue reservation phase cannot be rewritten")


class CanonicalStaleQueueState:
    """Reserve and record one target in the existing canonical state branch."""

    def __init__(self, git: HardenedStore, *, deadline_monotonic: float) -> None:
        self.git = git
        self.deadline_monotonic = deadline_monotonic

    @staticmethod
    def _translate(exc: Exception) -> QueueStateError:
        name = type(exc).__name__
        if name == "StateConflict":
            return QueueStateConflict("state branch or fixed queue snapshot changed")
        if name == "StateOutcomeUncertain":
            return QueueStateOutcomeUncertain("queue state push outcome is uncertain")
        if isinstance(exc, QueueStateError):
            return exc
        return QueueStateError("hardened canonical state operation failed")

    def read_snapshot(self) -> QueueStateSnapshot:
        try:
            with self.git.bounded_deadline(self.deadline_monotonic):
                state_ref_sha = self.git.remote_sha(STATE_REF)
                if not isinstance(state_ref_sha, str) or not SHA1_RE.fullmatch(state_ref_sha):
                    raise QueueStateError("canonical state branch is absent or malformed")
                journal_entry = self.git._tree_entry(state_ref_sha, JOURNAL_PATH)
                if journal_entry is None:
                    raise QueueStateError("canonical promotion journal is absent")
                _journal_oid, journal_mode, journal_size = journal_entry
                if journal_mode != "100644" or journal_size > MAX_JOURNAL_BYTES:
                    raise QueueStateError("canonical promotion journal mode or size is invalid")
                journal_bytes = self.git.read_file(
                    state_ref_sha, JOURNAL_PATH, maximum=MAX_JOURNAL_BYTES,
                )
                sidecar_entry = self.git._tree_entry(state_ref_sha, SIDECAR_PATH)
                if sidecar_entry is None:
                    sidecar_bytes = EMPTY_SIDECAR_BYTES
                    sidecar_mode = None
                else:
                    _sidecar_oid, sidecar_mode, sidecar_size = sidecar_entry
                    if sidecar_mode != "100644" or sidecar_size > MAX_SIDECAR_BYTES:
                        raise QueueStateError("queue sidecar mode or size is invalid")
                    sidecar_bytes = self.git.read_file(
                        state_ref_sha, SIDECAR_PATH, maximum=MAX_SIDECAR_BYTES,
                    )
                parse_sidecar(sidecar_bytes)
                return QueueStateSnapshot(
                    state_ref_sha=state_ref_sha,
                    journal_bytes=journal_bytes,
                    journal_mode=journal_mode,
                    sidecar_bytes=sidecar_bytes,
                    sidecar_mode=sidecar_mode,
                    sidecar_sha256=hashlib.sha256(sidecar_bytes).hexdigest(),
                )
        except Exception as exc:
            translated = self._translate(exc)
            if translated is exc:
                raise
            raise translated from exc

    def find_reservation(
        self, snapshot: QueueStateSnapshot, identity: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        state = parse_sidecar(snapshot.sidecar_bytes)
        wanted = target_key(identity)
        for record in state["records"]:
            if target_key(record["target"]) == wanted:
                return record
        return None

    def unresolved_reservations(self, snapshot: QueueStateSnapshot) -> list[dict[str, Any]]:
        state = parse_sidecar(snapshot.sidecar_bytes)
        return [
            record for record in state["records"]
            if record["phase"] in {"reserved", "unresolved"}
        ]

    def reserve(self, snapshot: QueueStateSnapshot, record: Mapping[str, Any]) -> None:
        if not isinstance(record, Mapping):
            raise QueueStateError("new queue reservation is not an object")
        next_record = dict(record)
        _validate_record(next_record)
        if next_record["phase"] != "reserved" or next_record["outcome"] is not None:
            raise QueueStateError("new queue reservation must be in the reserved phase")
        sidecar = parse_sidecar(snapshot.sidecar_bytes)
        if self.find_reservation(snapshot, next_record["target"]) is not None:
            raise QueueStateConflict("target already has an existing reservation")
        if len(sidecar["records"]) >= MAX_RECORDS:
            raise QueueStateError("queue reservation history reached its fixed bound")
        following = {
            "schema_version": STATE_SCHEMA,
            "repository": REPOSITORY,
            "records": [*sidecar["records"], next_record],
        }
        next_raw = canonical_json_bytes(following)
        validate_successor(snapshot.sidecar_bytes, next_raw)
        self._cas(snapshot, next_raw)

    def record_outcome(
        self, snapshot: QueueStateSnapshot, identity: Mapping[str, Any],
        *, phase: str, outcome: Mapping[str, Any],
    ) -> None:
        if phase not in {"native_cancel_confirmed", "unresolved"}:
            raise QueueStateError("queue outcome phase is unsupported")
        sidecar = parse_sidecar(snapshot.sidecar_bytes)
        wanted = target_key(identity)
        records = [dict(record) for record in sidecar["records"]]
        index = next((i for i, record in enumerate(records) if target_key(record["target"]) == wanted), None)
        if index is None:
            raise QueueStateError("queue outcome has no matching durable reservation")
        before = records[index]
        if not (
            (before["phase"] == "reserved" and before["outcome"] is None)
            or (
                before["phase"] == "unresolved"
                and phase == "native_cancel_confirmed"
                and before["outcome"] is not None
                and before["outcome"].get("request_disposition")
                == outcome.get("request_disposition")
            )
        ):
            raise QueueStateConflict("queue reservation already has an incompatible outcome")
        records[index]["phase"] = phase
        records[index]["outcome"] = dict(outcome)
        following = {"schema_version": STATE_SCHEMA, "repository": REPOSITORY, "records": records}
        next_raw = canonical_json_bytes(following)
        validate_successor(snapshot.sidecar_bytes, next_raw)
        self._cas(snapshot, next_raw)

    def _cas(self, snapshot: QueueStateSnapshot, next_raw: bytes) -> None:
        if not isinstance(next_raw, bytes) or not next_raw or len(next_raw) > MAX_SIDECAR_BYTES:
            raise QueueStateError("queue sidecar update exceeds its fixed bound")
        parse_sidecar(next_raw)
        child_sha: str | None = None
        try:
            with self.git.bounded_deadline(self.deadline_monotonic):
                current = self.read_snapshot()
                if (
                    current.state_ref_sha != snapshot.state_ref_sha
                    or current.journal_bytes != snapshot.journal_bytes
                    or current.journal_mode != snapshot.journal_mode
                    or current.sidecar_bytes != snapshot.sidecar_bytes
                    or current.sidecar_mode != snapshot.sidecar_mode
                    or current.sidecar_sha256 != snapshot.sidecar_sha256
                ):
                    raise QueueStateConflict("canonical queue snapshot changed before CAS")
                child_sha = self.git._commit_updates(
                    expected_sha=snapshot.state_ref_sha,
                    base_sha=snapshot.state_ref_sha,
                    updates={SIDECAR_PATH: next_raw},
                    create_only=frozenset({SIDECAR_PATH}) if snapshot.sidecar_mode is None else frozenset(),
                    message="canonical stale C queue recovery reservation",
                    update_modes={SIDECAR_PATH: snapshot.sidecar_mode or "100644"},
                )
                if not isinstance(child_sha, str) or not SHA1_RE.fullmatch(child_sha):
                    raise QueueStateOutcomeUncertain("queue CAS returned a malformed child SHA")
                parent = self.git._run(
                    (
                        "--git-dir", str(self.git.git_dir), "rev-list", "--parents", "-n", "1", child_sha,
                    ),
                    maximum_output=256,
                ).decode("ascii", "strict").strip().split()
                if parent != [child_sha, snapshot.state_ref_sha]:
                    raise QueueStateOutcomeUncertain("queue CAS child parent differs from exact state head")
                changed = self.git.changed_paths(
                    snapshot.state_ref_sha, child_sha,
                    deadline_monotonic=self.deadline_monotonic,
                )
                if changed != (SIDECAR_PATH,):
                    raise QueueStateOutcomeUncertain("queue CAS changed a path outside its fixed sidecar")
                if self.git.remote_sha(STATE_REF) != child_sha:
                    raise QueueStateOutcomeUncertain("queue CAS child is not the exact native branch head")
                journal_entry = self.git._tree_entry(child_sha, JOURNAL_PATH)
                sidecar_entry = self.git._tree_entry(child_sha, SIDECAR_PATH)
                if journal_entry is None or sidecar_entry is None:
                    raise QueueStateOutcomeUncertain("queue CAS child lacks a required fixed file")
                _journal_oid, journal_mode, journal_size = journal_entry
                _sidecar_oid, sidecar_mode, sidecar_size = sidecar_entry
                if (
                    journal_mode != snapshot.journal_mode
                    or journal_size != len(snapshot.journal_bytes)
                    or sidecar_mode != (snapshot.sidecar_mode or "100644")
                    or sidecar_size != len(next_raw)
                ):
                    raise QueueStateOutcomeUncertain("queue CAS child mode or exact size differs")
                journal_after = self.git.read_file(
                    child_sha, JOURNAL_PATH, maximum=MAX_JOURNAL_BYTES,
                )
                sidecar_after = self.git.read_file(
                    child_sha, SIDECAR_PATH, maximum=MAX_SIDECAR_BYTES,
                )
                if journal_after != snapshot.journal_bytes or sidecar_after != next_raw:
                    raise QueueStateOutcomeUncertain("queue CAS child bytes differ from exact snapshot")
                # A tree digest check complements the exact changed-path and
                # fixed-file readbacks while remaining bounded by the same D.
                tree = self.git.tree_sha(child_sha, deadline_monotonic=self.deadline_monotonic)
                if not isinstance(tree, str) or not SHA1_RE.fullmatch(tree):
                    raise QueueStateOutcomeUncertain("queue CAS child tree SHA is malformed")
        except Exception as exc:
            if child_sha is not None and not isinstance(exc, QueueStateOutcomeUncertain):
                raise QueueStateOutcomeUncertain(
                    "queue state committed but exact child preservation proof failed",
                ) from exc
            translated = self._translate(exc)
            if translated is exc:
                raise
            raise translated from exc


def create_hardened_store(
    *, repository_root: str,
    token: str,
    deadline_monotonic: float,
) -> tuple[Any, CanonicalStaleQueueState]:
    """Construct the pinned hardened store without consulting ambient origin."""
    if not isinstance(token, str) or not token or "\n" in token or "\x00" in token:
        raise QueueStateError("GitHub token is missing or malformed")
    try:
        import canonical_publication_state as publication_state
    except ImportError as exc:
        raise QueueStateError("hardened canonical publication state module is unavailable") from exc
    try:
        git = publication_state.GitObjectStore(
            repository_root,
            remote_url="https://github.com/StatPan/datapan-registry.git",
            token=token,
            timeout_seconds=120.0,
            max_commands=600,
            max_output_bytes=192 * 1024 * 1024,
            max_blob_bytes=8 * 1024 * 1024,
            deadline_monotonic=deadline_monotonic,
        )
    except Exception as exc:
        raise QueueStateError("hardened canonical state store could not be constructed") from exc
    if git.remote_url != "https://github.com/StatPan/datapan-registry.git":
        git.close()
        raise QueueStateError("hardened canonical state remote differs from the pinned repository")
    return git, CanonicalStaleQueueState(git, deadline_monotonic=deadline_monotonic)
