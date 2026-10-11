"""C-owned, read-only disposition for an admitted #743 source target.

This module deliberately runs before a mutable C parent or B bundle is selected.
The caller supplies the exact private #743 capability, the journal snapshot it
already loaded, and a bounded native reader that rechecks the state-branch tip,
the exact journal blob, and any matching PR. Caller-shaped dictionaries cannot
construct the returned capability.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from typing import Any


SCHEMA_VERSION = "datapan.canonical-update-source-target-disposition.v1"
_VERIFIED_DISPOSITION = object()
SHA1_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
PREPARED = frozenset({"prepared"})
PENDING = frozenset({"pending-review"})
MERGED_OR_PUBLISHED = frozenset({"merged", "publication-pending", "published", "read-back-confirmed"})
TERMINAL_FAILURE = frozenset({"failed", "closed"})
NOOP_PROOF_KIND = "canonical_update_source_target_noop_v2"
NOOP_PROOF_FIELDS = frozenset({
    "kind", "journal_commit_sha", "selection_key", "record_sha256",
    "evaluator_repository", "evaluator_workflow_path", "evaluator_event",
    "evaluator_source_sha", "evaluator_run_id", "evaluator_run_attempt",
    "terminal_evaluator_sha256", "terminal_projection_sha256",
    "target_inventory_sha256", "native_status", "native_conclusion",
})
NOOP_WORKFLOW_PATH = ".github/workflows/canonical-update-promotion.yml"
NOOP_EVENTS = frozenset({"schedule", "workflow_run", "workflow_dispatch"})
NOOP_CONCLUSIONS = frozenset({
    "success", "failure", "cancelled", "timed_out", "action_required",
    "neutral", "skipped", "stale", "startup_failure",
})
NOOP_INVOCATION_STEP = "Recover at most one durable ready processor bundle"
NOOP_UPLOAD_STEP = "Upload C terminal outcome (recover-ready)"
NOOP_CONTRACT_SCHEMA = "datapan.upstream-catalogue-target-contract.v1"
NOOP_MAX_BLOB_BYTES = 4 * 1024 * 1024
NOOP_MAX_TOTAL_BLOB_BYTES = 64 * 1024 * 1024


class TargetDispositionError(ValueError):
    pass


class VerifiedCTargetDisposition:
    """Opaque output of the native C target resolver."""

    __slots__ = (
        "schema_version", "selection_key", "original_observation_sha256",
        "target_contract_sha256", "disposition", "reason", "journal_commit",
        "evidence_refs", "revision_ref", "purpose_ref",
    )

    def __init__(
        self,
        token: object,
        *,
        selection_key: str,
        original_observation_sha256: str,
        target_contract_sha256: str,
        disposition: str,
        reason: str,
        journal_commit: str | None,
        evidence_refs: list[Mapping[str, Any]],
        revision_ref: Mapping[str, Any] | None = None,
        purpose_ref: Mapping[str, Any] | None = None,
    ) -> None:
        if token is not _VERIFIED_DISPOSITION:
            raise TargetDispositionError("C target disposition constructor is private")
        if disposition not in {"unseen", "pending", "completed", "blocked"}:
            raise TargetDispositionError("C target disposition value is unsupported")
        if not SHA256_RE.fullmatch(selection_key) or not SHA256_RE.fullmatch(original_observation_sha256) or not SHA256_RE.fullmatch(target_contract_sha256):
            raise TargetDispositionError("C target disposition identity is malformed")
        if journal_commit is not None and not SHA1_RE.fullmatch(journal_commit):
            raise TargetDispositionError("C target disposition journal commit is malformed")
        if not re.fullmatch(r"[a-z0-9_]{1,96}", reason):
            raise TargetDispositionError("C target disposition reason is malformed")
        if disposition == "unseen" and (revision_ref is not None or purpose_ref is not None):
            raise TargetDispositionError("unseen C target disposition cannot carry an existing revision")
        if disposition == "pending" and not isinstance(revision_ref, Mapping) and reason != "source_target_noop_evaluator_pending":
            raise TargetDispositionError("pending C target disposition must bind its exact revision")
        if disposition == "completed" and revision_ref is None and reason != "validated_target_noop_receipt":
            raise TargetDispositionError("completed C target disposition must bind its exact revision")
        base_revision_fields = {
            "repository", "source_id", "scope", "generation_id", "registry_sha256",
            "manifest_sha256", "head_sha", "pr_number", "owner_id", "branch", "body_sha256",
        }
        if revision_ref is not None:
            if not isinstance(revision_ref, Mapping) or not base_revision_fields.issubset(revision_ref):
                raise TargetDispositionError("C target disposition revision reference is incomplete")
            optional_revision_fields = {
                "purpose", "release_closure_reference", "release_closure_evidence", "source_target",
            }
            if set(revision_ref) - base_revision_fields - optional_revision_fields:
                raise TargetDispositionError("C target disposition revision reference has unknown fields")
            if revision_ref.get("source_target") is not None:
                revision_binding = revision_ref["source_target"]
                if not isinstance(revision_binding, Mapping) or revision_binding.get("selection_key") != selection_key:
                    raise TargetDispositionError("C target disposition revision is bound to another source target")
        if purpose_ref is not None:
            purpose_fields = {
                "schema_version", "purpose", "refresh_key", "admitted_publication_subject_sha256",
                "admitted_candidate_id", "generation_id", "canonical_sha256",
                "admitted_parent_projection_sha256", "current_base_delta_sha256",
                "generated_projection_sha256", "branch",
            }
            if not isinstance(purpose_ref, Mapping) or set(purpose_ref) != purpose_fields:
                raise TargetDispositionError("C target disposition purpose reference has an unexpected field set")
            if purpose_ref.get("purpose") != "release-closure-refresh" or not SHA256_RE.fullmatch(str(purpose_ref.get("refresh_key", ""))):
                raise TargetDispositionError("C target disposition purpose reference is invalid")
        validated_evidence: list[dict[str, Any]] = []
        for item in evidence_refs:
            if not isinstance(item, Mapping):
                raise TargetDispositionError("C target disposition evidence reference must be an object")
            kind = item.get("kind")
            if kind == "canonical_update_state_journal":
                if set(item) != {"kind", "commit_sha", "bytes", "sha256"} or not SHA1_RE.fullmatch(str(item.get("commit_sha", ""))) or type(item.get("bytes")) is not int or item["bytes"] < 1 or not SHA256_RE.fullmatch(str(item.get("sha256", ""))):
                    raise TargetDispositionError("C target disposition journal evidence is invalid")
            elif kind == "github_pull_request":
                fields = {"kind", "repository", "number", "head_sha", "body_sha256", "state"}
                if "merge_sha" in item:
                    fields.add("merge_sha")
                if set(item) != fields or not isinstance(item.get("repository"), str) or not re.fullmatch(r"[^/]+/[^/]+", item["repository"]):
                    raise TargetDispositionError("C target disposition PR evidence is invalid")
                if type(item.get("number")) is not int or item["number"] < 1 or not SHA1_RE.fullmatch(str(item.get("head_sha", ""))) or not SHA256_RE.fullmatch(str(item.get("body_sha256", ""))) or item.get("state") not in {"open", "merged"}:
                    raise TargetDispositionError("C target disposition PR evidence is invalid")
                if "merge_sha" in item and not SHA1_RE.fullmatch(str(item["merge_sha"])):
                    raise TargetDispositionError("C target disposition merge evidence is invalid")
            elif kind == NOOP_PROOF_KIND:
                if set(item) != NOOP_PROOF_FIELDS:
                    raise TargetDispositionError("C target disposition no-op proof reference has an unexpected field set")
                if (
                    not SHA1_RE.fullmatch(str(item.get("journal_commit_sha", "")))
                    or not SHA256_RE.fullmatch(str(item.get("selection_key", "")))
                    or not SHA256_RE.fullmatch(str(item.get("record_sha256", "")))
                    or not isinstance(item.get("evaluator_repository"), str)
                    or not re.fullmatch(r"[^/]+/[^/]+", item["evaluator_repository"])
                    or item.get("evaluator_workflow_path") != NOOP_WORKFLOW_PATH
                    or item.get("evaluator_event") not in NOOP_EVENTS
                    or not SHA1_RE.fullmatch(str(item.get("evaluator_source_sha", "")))
                    or not isinstance(item.get("evaluator_run_id"), str)
                    or not re.fullmatch(r"[0-9]{6,20}", item["evaluator_run_id"])
                    or type(item.get("evaluator_run_attempt")) is not int
                    or item["evaluator_run_attempt"] < 1
                    or not SHA256_RE.fullmatch(str(item.get("terminal_evaluator_sha256", "")))
                    or not SHA256_RE.fullmatch(str(item.get("terminal_projection_sha256", "")))
                    or not SHA256_RE.fullmatch(str(item.get("target_inventory_sha256", "")))
                    or item.get("native_status") not in {"queued", "in_progress", "completed"}
                    or item.get("native_conclusion") is not None
                    and item.get("native_conclusion") not in NOOP_CONCLUSIONS
                ):
                    raise TargetDispositionError("C target disposition no-op proof reference is invalid")
            else:
                raise TargetDispositionError("C target disposition evidence kind is unsupported")
            validated_evidence.append(dict(item))
        noop_proofs = [item for item in validated_evidence if item.get("kind") == NOOP_PROOF_KIND]
        if len(noop_proofs) > 1:
            raise TargetDispositionError("C target disposition has duplicate no-op proof references")
        if noop_proofs and (
            noop_proofs[0].get("selection_key") != selection_key
            or noop_proofs[0].get("journal_commit_sha") != journal_commit
        ):
            raise TargetDispositionError("C target disposition no-op proof differs from its journal or selection binding")
        if noop_proofs:
            proof = noop_proofs[0]
            inventory_contract_sha = hashlib.sha256(
                b"datapan/upstream-catalogue-target-contract/v1\0"
                + json.dumps(
                    {"schema_version": NOOP_CONTRACT_SCHEMA, "inventory_sha256": proof["target_inventory_sha256"]},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            if inventory_contract_sha != target_contract_sha256:
                raise TargetDispositionError("C target disposition no-op proof differs from its target inventory")
        if disposition == "completed" and reason == "validated_target_noop_receipt":
            if (
                len(noop_proofs) != 1
                or noop_proofs[0].get("native_status") != "completed"
                or noop_proofs[0].get("native_conclusion") != "success"
            ):
                raise TargetDispositionError("completed no-op disposition lacks the exact successful native proof")
        elif disposition == "pending" and reason == "source_target_noop_evaluator_pending":
            if (
                len(noop_proofs) != 1
                or noop_proofs[0].get("native_status") not in {"queued", "in_progress"}
                or noop_proofs[0].get("native_conclusion") is not None
            ):
                raise TargetDispositionError("pending no-op disposition lacks the exact pending native proof")
        elif noop_proofs and disposition != "blocked":
            raise TargetDispositionError("C target disposition no-op proof has an inconsistent state")
        self.schema_version = SCHEMA_VERSION
        self.selection_key = selection_key
        self.original_observation_sha256 = original_observation_sha256
        self.target_contract_sha256 = target_contract_sha256
        self.disposition = disposition
        self.reason = reason
        self.journal_commit = journal_commit
        self.evidence_refs = validated_evidence
        self.revision_ref = dict(revision_ref) if revision_ref is not None else None
        self.purpose_ref = dict(purpose_ref) if purpose_ref is not None else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "selection_key": self.selection_key,
            "original_observation_sha256": self.original_observation_sha256,
            "target_contract_sha256": self.target_contract_sha256,
            "disposition": self.disposition,
            "reason": self.reason,
            "journal_commit": self.journal_commit,
            "evidence_refs": [dict(item) for item in self.evidence_refs],
            "revision_ref": dict(self.revision_ref) if self.revision_ref is not None else None,
            "purpose_ref": dict(self.purpose_ref) if self.purpose_ref is not None else None,
        }


def _block(
    binding: Mapping[str, Any], *, reason: str, journal_commit: str | None,
    evidence_refs: list[Mapping[str, Any]] | None = None,
) -> VerifiedCTargetDisposition:
    return VerifiedCTargetDisposition(
        _VERIFIED_DISPOSITION,
        selection_key=str(binding["selection_key"]),
        original_observation_sha256=str(binding["original_observation_sha256"]),
        target_contract_sha256=str(binding["target_contract_sha256"]),
        disposition="blocked", reason=reason, journal_commit=journal_commit,
        evidence_refs=list(evidence_refs or []),
    )


def _read_native_snapshot(
    journal_snapshot: Any,
    native_reader: Any,
    *,
    journal_helper: Any,
    journal_schema: Mapping[str, Any],
    deadline_monotonic: float,
) -> tuple[Mapping[str, Any] | None, str | None, list[dict[str, Any]], str | None]:
    if time.monotonic() >= deadline_monotonic:
        return None, None, [], "deadline_exceeded"
    if not isinstance(journal_snapshot, Mapping) or set(journal_snapshot) != {"journal", "commit_sha"}:
        return None, None, [], "journal_snapshot_unavailable"
    expected_journal = journal_snapshot.get("journal")
    expected_commit = journal_snapshot.get("commit_sha")
    if expected_commit is not None and (not isinstance(expected_commit, str) or not SHA1_RE.fullmatch(expected_commit)):
        return None, None, [], "journal_snapshot_malformed"
    try:
        observed_commit = native_reader.read_state_tip(deadline_monotonic=deadline_monotonic)
    except Exception:  # native availability errors are deliberately opaque here
        return None, expected_commit, [], "journal_unavailable"
    if time.monotonic() >= deadline_monotonic:
        return None, expected_commit, [], "deadline_exceeded"
    if observed_commit != expected_commit:
        return None, expected_commit, [], "journal_snapshot_mismatch"
    if expected_commit is None:
        if expected_journal is not None:
            return None, None, [], "journal_snapshot_malformed"
        return None, None, [], None
    if not isinstance(expected_journal, Mapping):
        return None, expected_commit, [], "journal_malformed"
    try:
        raw = native_reader.read_journal_blob(expected_commit, deadline_monotonic=deadline_monotonic)
    except Exception:
        return None, expected_commit, [], "journal_unavailable"
    if time.monotonic() >= deadline_monotonic:
        return None, expected_commit, [], "deadline_exceeded"
    if not isinstance(raw, bytes) or not raw or len(raw) > 16 * 1024 * 1024:
        return None, expected_commit, [], "journal_malformed"
    try:
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON object key")
                result[key] = value
            return result

        def reject_constant(_value: str) -> None:
            raise ValueError("non-JSON numeric constant")

        # A journal is UTF-8 JSON by contract.  Passing bytes directly to
        # json.loads also auto-detects UTF-16/32, which would silently widen
        # the wire format and make the raw-byte binding ambiguous.
        text = raw.decode("utf-8", errors="strict")
        if text.startswith("\ufeff"):
            raise ValueError("journal UTF-8 BOM is not permitted")
        parsed = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if not isinstance(parsed, Mapping):
            raise ValueError("journal root is not an object")
        journal_helper.validate_journal(parsed, journal_schema)
    except Exception:
        return None, expected_commit, [], "journal_malformed"
    if dict(parsed) != dict(expected_journal):
        return None, expected_commit, [], "journal_snapshot_mismatch"
    evidence = [{
        "kind": "canonical_update_state_journal",
        "commit_sha": expected_commit,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }]
    return parsed, expected_commit, evidence, None


def _validate_native_pr(
    row: Mapping[str, Any], *, repository: str, native_reader: Any,
    journal_helper: Any, deadline_monotonic: float,
) -> tuple[Mapping[str, Any] | None, str | None]:
    candidate = row.get("candidate")
    pr_row = row.get("pr")
    ownership = row.get("ownership")
    if not isinstance(candidate, Mapping) or not isinstance(pr_row, Mapping) or not isinstance(ownership, Mapping):
        return None, "target_record_malformed"
    number = pr_row.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        return None, "target_record_malformed"
    if number == 0:
        if row.get("status") not in PREPARED:
            return None, "target_record_malformed"
        return None, None
    if time.monotonic() >= deadline_monotonic:
        return None, "deadline_exceeded"
    try:
        observed = native_reader.read_pull_request(repository, number, deadline_monotonic=deadline_monotonic)
    except Exception:
        return None, "pr_readback_unavailable"
    if time.monotonic() >= deadline_monotonic:
        return None, "deadline_exceeded"
    if not isinstance(observed, Mapping):
        return None, "pr_readback_mismatch"
    expected_head = candidate.get("head_sha")
    expected_body_sha = ownership.get("body_sha256")
    body = observed.get("body")
    actual_head = observed.get("headRefOid", observed.get("head_sha"))
    if (
        str(observed.get("repository", "")).casefold() != repository.casefold()
        or str(observed.get("headRepository", "")).casefold() != repository.casefold()
        or
        observed.get("number", number) != number
        or observed.get("headRefName", observed.get("head_ref")) != ownership.get("branch")
        or actual_head != expected_head
        or not isinstance(body, str)
        or hashlib.sha256(body.encode("utf-8")).hexdigest() != expected_body_sha
        or not isinstance(expected_head, str)
        or not SHA1_RE.fullmatch(expected_head)
        or journal_helper.body_marker(str(ownership.get("owner_id", "")), str(candidate.get("generation_id", ""))) not in body
    ):
        return None, "pr_readback_mismatch"
    state = observed.get("state")
    if state == "OPEN":
        if observed.get("merged") is not False or observed.get("baseRefName") != "main":
            return None, "pr_readback_mismatch"
        return observed, None
    if state == "MERGED":
        merge = observed.get("mergeCommit")
        merge_sha = merge.get("oid") if isinstance(merge, Mapping) else observed.get("merge_commit_sha")
        durable_merge_sha = pr_row.get("merge_commit_sha")
        if (
            not isinstance(merge_sha, str) or not SHA1_RE.fullmatch(merge_sha)
            or (durable_merge_sha is not None and durable_merge_sha != merge_sha)
            or observed.get("merged") is not True
            or observed.get("baseRefName", observed.get("base_ref", "main")) != "main"
        ):
            return None, "pr_readback_mismatch"
        return observed, None
    return None, "pr_readback_mismatch"


def _record_rows_for_target(journal: Mapping[str, Any], selection_key: str) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    candidate_rows: list[Mapping[str, Any]] = []
    noop_rows: list[Mapping[str, Any]] = []
    for row in journal.get("records", []):
        if not isinstance(row, Mapping):
            raise TargetDispositionError("journal row is not an object")
        candidate = row.get("candidate")
        binding = candidate.get("source_target") if isinstance(candidate, Mapping) else None
        if isinstance(binding, Mapping) and binding.get("selection_key") == selection_key:
            candidate_rows.append(row)
    for row in journal.get("source_target_dispositions", []):
        if isinstance(row, Mapping):
            binding = row.get("source_target")
            if isinstance(binding, Mapping) and binding.get("selection_key") == selection_key:
                noop_rows.append(row)
    return candidate_rows, noop_rows


def _source_target_noop_proof_ref(
    record: Mapping[str, Any], *, journal_commit: str, native_status: str,
    native_conclusion: str | None,
) -> dict[str, Any]:
    retained = record["retained_evaluation"]
    return {
        "kind": NOOP_PROOF_KIND,
        "journal_commit_sha": journal_commit,
        "selection_key": record["source_target"]["selection_key"],
        "record_sha256": hashlib.sha256(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest(),
        "evaluator_repository": record["evaluator_repository"],
        "evaluator_workflow_path": record["evaluator_workflow_path"],
        "evaluator_event": record["evaluator_event"],
        "evaluator_source_sha": record["evaluator_source_sha"],
        "evaluator_run_id": record["evaluator_run_id"],
        "evaluator_run_attempt": record["evaluator_run_attempt"],
        "terminal_evaluator_sha256": record["terminal_evaluator_sha256"],
        "terminal_projection_sha256": record["terminal_projection_sha256"],
        "target_inventory_sha256": retained["target_inventory"]["sha256"],
        "native_status": native_status,
        "native_conclusion": native_conclusion,
    }


def _authenticate_source_target_noop(
    record: Mapping[str, Any], *, source_target: Any, binding: Mapping[str, Any],
    journal_commit: str, native_reader: Any, deadline_monotonic: float,
) -> tuple[str, str, dict[str, Any] | None]:
    """Authenticate retained no-op evidence against the exact native C run and old Git tree."""
    repository = getattr(native_reader, "repository", None)
    if (
        not isinstance(repository, str)
        or not re.fullmatch(r"[^/]+/[^/]+", repository)
        or repository.casefold() != record["evaluator_repository"].casefold()
    ):
        return "blocked", "historical_noop_repository_mismatch", None
    if time.monotonic() >= deadline_monotonic:
        return "blocked", "deadline_exceeded", None
    try:
        run = native_reader.read_c_evaluator_run(
            repository=repository,
            run_id=record["evaluator_run_id"],
            run_attempt=record["evaluator_run_attempt"],
            deadline_monotonic=deadline_monotonic,
        )
    except Exception:
        return "blocked", "historical_noop_native_unavailable", None
    if time.monotonic() >= deadline_monotonic:
        return "blocked", "deadline_exceeded", None
    run_fields = {
        "repository", "workflow_path", "event", "source_sha", "run_id",
        "run_attempt", "status", "conclusion", "jobs",
    }
    if not isinstance(run, Mapping) or set(run) != run_fields:
        return "blocked", "historical_noop_native_malformed", None
    if (
        str(run.get("repository", "")).casefold() != repository.casefold()
        or run.get("workflow_path") != NOOP_WORKFLOW_PATH
        or run.get("event") != record["evaluator_event"]
        or run.get("source_sha") != record["evaluator_source_sha"]
        or str(run.get("run_id")) != record["evaluator_run_id"]
        or type(run.get("run_attempt")) is not int
        or run.get("run_attempt") != record["evaluator_run_attempt"]
        or run.get("status") not in {"queued", "in_progress", "completed"}
    ):
        return "blocked", "historical_noop_native_identity_mismatch", None
    conclusion = run.get("conclusion")
    if conclusion is not None and conclusion not in NOOP_CONCLUSIONS:
        return "blocked", "historical_noop_native_malformed", None
    if run["status"] in {"queued", "in_progress"}:
        if conclusion is not None or not isinstance(run.get("jobs"), list):
            return "blocked", "historical_noop_native_malformed", None
        proof = _source_target_noop_proof_ref(
            record, journal_commit=journal_commit, native_status=run["status"], native_conclusion=None,
        )
        return "pending", "source_target_noop_evaluator_pending", proof
    if conclusion != "success":
        proof = _source_target_noop_proof_ref(
            record, journal_commit=journal_commit, native_status="completed", native_conclusion=conclusion,
        )
        return "blocked", "historical_noop_evaluator_failed", proof
    jobs = run.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != 1:
        return "blocked", "historical_noop_native_malformed", None
    matching_jobs = [job for job in jobs if isinstance(job, Mapping) and job.get("name") == "reconcile"]
    if len(matching_jobs) != 1:
        return "blocked", "historical_noop_evaluator_job_mismatch", None
    job = matching_jobs[0]
    if set(job) != {"name", "status", "conclusion", "steps"}:
        return "blocked", "historical_noop_native_malformed", None
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        return "blocked", "historical_noop_evaluator_job_failed", None
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > 128:
        return "blocked", "historical_noop_evaluator_steps_unavailable", None
    selected_steps: dict[str, Mapping[str, Any]] = {}
    step_names: set[str] = set()
    for step in steps:
        if not isinstance(step, Mapping) or set(step) != {"name", "status", "conclusion"}:
            return "blocked", "historical_noop_native_malformed", None
        name = step.get("name")
        if not isinstance(name, str) or name in step_names:
            return "blocked", "historical_noop_native_malformed", None
        step_names.add(name)
        if name in {NOOP_INVOCATION_STEP, NOOP_UPLOAD_STEP}:
            selected_steps[name] = step
    if set(selected_steps) != {NOOP_INVOCATION_STEP, NOOP_UPLOAD_STEP} or any(
        row.get("status") != "completed" or row.get("conclusion") != "success"
        for row in selected_steps.values()
    ):
        return "blocked", "historical_noop_evaluator_steps_failed", None

    retained = record["retained_evaluation"]
    old_contract = retained["projection"]["document"]["target_contract"]
    try:
        tree_sha = native_reader.read_git_tree_sha(
            record["evaluator_source_sha"], deadline_monotonic=deadline_monotonic,
        )
    except Exception:
        return "blocked", "historical_noop_git_tree_unavailable", None
    if time.monotonic() >= deadline_monotonic:
        return "blocked", "deadline_exceeded", None
    if tree_sha != old_contract["source_tree_sha"]:
        return "blocked", "historical_noop_git_tree_mismatch", None
    old_inventory = retained["target_inventory"]["document"]
    current_inventory = getattr(source_target, "target_inventory", None)
    current_contract = getattr(source_target, "target_contract", None)
    if not isinstance(current_contract, Mapping) or not isinstance(current_inventory, list):
        return "blocked", "current_target_inventory_unavailable", None
    if (
        current_contract.get("contract_sha256") != binding["target_contract_sha256"]
        or current_contract.get("inventory_sha256") != old_contract.get("inventory_sha256")
        or current_contract.get("changed_classes") != old_contract.get("changed_classes")
        or current_inventory != old_inventory
    ):
        return "blocked", "historical_noop_target_contract_changed", None
    total_blob_bytes = 0
    for row in old_inventory:
        if time.monotonic() >= deadline_monotonic:
            return "blocked", "deadline_exceeded", None
        try:
            blob = native_reader.read_git_blob(
                record["evaluator_source_sha"], row["path"],
                max_bytes=NOOP_MAX_BLOB_BYTES,
                deadline_monotonic=deadline_monotonic,
            )
        except Exception:
            return "blocked", "historical_noop_git_blob_unavailable", None
        if not isinstance(blob, bytes) or len(blob) > NOOP_MAX_BLOB_BYTES:
            return "blocked", "historical_noop_git_blob_malformed", None
        total_blob_bytes += len(blob)
        if total_blob_bytes > NOOP_MAX_TOTAL_BLOB_BYTES:
            return "blocked", "historical_noop_git_inventory_exceeds_bound", None
        if len(blob) != row["bytes"] or hashlib.sha256(blob).hexdigest() != row["sha256"]:
            return "blocked", "historical_noop_git_inventory_mismatch", None
    proof = _source_target_noop_proof_ref(
        record, journal_commit=journal_commit, native_status="completed", native_conclusion="success",
    )
    return "completed", "validated_target_noop_receipt", proof


def resolve_c_target_disposition(
    *,
    source_target: Any,
    producer_module: Any,
    journal_snapshot: Mapping[str, Any],
    native_reader: Any,
    journal_helper: Any,
    journal_schema: Mapping[str, Any],
    deadline_monotonic: float,
) -> VerifiedCTargetDisposition:
    """Resolve exact existing C work before any new parent or preparation read.

    ``journal_snapshot`` is exactly ``{"journal": mapping-or-None,
    "commit_sha": full-sha-or-None}``. ``native_reader`` must implement
    ``read_state_tip(deadline_monotonic=...)``,
    ``read_journal_blob(commit_sha, deadline_monotonic=...)`` and
    ``read_pull_request(repository, number, deadline_monotonic=...)``. A v2
    no-op row additionally requires ``read_c_evaluator_run(...)``,
    ``read_git_tree_sha(commit_sha, deadline_monotonic=...)`` and bounded
    ``read_git_blob(commit_sha, path, max_bytes=..., deadline_monotonic=...)``.
    An authoritative absent state-branch ref is the only route to ``unseen``
    with no journal. Missing journal content under an existing ref blocks.
    """
    try:
        binding = journal_helper.source_target_binding_from_capability(source_target, producer_module)
    except Exception:
        raise TargetDispositionError("C target disposition requires the exact runner-owned source target capability") from None
    journal, commit_sha, evidence, error = _read_native_snapshot(
        journal_snapshot, native_reader, journal_helper=journal_helper,
        journal_schema=journal_schema, deadline_monotonic=deadline_monotonic,
    )
    if error is not None:
        return _block(binding, reason=error, journal_commit=commit_sha, evidence_refs=evidence)
    if journal is None:
        return VerifiedCTargetDisposition(
            _VERIFIED_DISPOSITION,
            selection_key=binding["selection_key"],
            original_observation_sha256=binding["original_observation_sha256"],
            target_contract_sha256=binding["target_contract_sha256"],
            disposition="unseen", reason="no_authoritative_state_branch_ref",
            journal_commit=None, evidence_refs=[],
        )
    try:
        candidate_rows, noop_rows = _record_rows_for_target(journal, binding["selection_key"])
    except Exception:
        return _block(binding, reason="journal_malformed", journal_commit=commit_sha, evidence_refs=evidence)
    active_rows = [row for row in candidate_rows if row.get("superseded_by") is None]
    if noop_rows and candidate_rows:
        return _block(binding, reason="target_record_ambiguous", journal_commit=commit_sha, evidence_refs=evidence)
    if len(noop_rows) > 1 or len(active_rows) > 1:
        return _block(binding, reason="target_record_ambiguous", journal_commit=commit_sha, evidence_refs=evidence)
    if not active_rows and not noop_rows:
        if candidate_rows:
            return _block(binding, reason="target_revision_superseded_without_current_owner", journal_commit=commit_sha, evidence_refs=evidence)
        return VerifiedCTargetDisposition(
            _VERIFIED_DISPOSITION,
            selection_key=binding["selection_key"],
            original_observation_sha256=binding["original_observation_sha256"],
            target_contract_sha256=binding["target_contract_sha256"],
            disposition="unseen", reason="no_matching_c_owned_target_record",
            journal_commit=commit_sha, evidence_refs=evidence,
        )
    if noop_rows:
        try:
            record = journal_helper.validate_source_target_noop(noop_rows[0])
        except Exception:
            return _block(binding, reason="target_record_malformed", journal_commit=commit_sha, evidence_refs=evidence)
        if record["source_target"] != binding:
            return _block(binding, reason="target_record_binding_mismatch", journal_commit=commit_sha, evidence_refs=evidence)
        if record.get("schema_version") != journal_helper.SOURCE_TARGET_NOOP_V2_SCHEMA:
            return _block(binding, reason="historical_noop_proof_unavailable", journal_commit=commit_sha, evidence_refs=evidence)
        if commit_sha is None:
            return _block(binding, reason="journal_snapshot_malformed", journal_commit=None, evidence_refs=evidence)
        disposition, reason, proof_ref = _authenticate_source_target_noop(
            record, source_target=source_target, binding=binding,
            journal_commit=commit_sha, native_reader=native_reader,
            deadline_monotonic=deadline_monotonic,
        )
        if proof_ref is not None:
            evidence = [*evidence, proof_ref]
        if disposition == "completed":
            return VerifiedCTargetDisposition(
                _VERIFIED_DISPOSITION,
                selection_key=binding["selection_key"],
                original_observation_sha256=binding["original_observation_sha256"],
                target_contract_sha256=binding["target_contract_sha256"],
                disposition="completed", reason=reason,
                journal_commit=commit_sha, evidence_refs=evidence,
            )
        if disposition == "pending":
            return VerifiedCTargetDisposition(
                _VERIFIED_DISPOSITION,
                selection_key=binding["selection_key"],
                original_observation_sha256=binding["original_observation_sha256"],
                target_contract_sha256=binding["target_contract_sha256"],
                disposition="pending", reason=reason,
                journal_commit=commit_sha, evidence_refs=evidence,
            )
        return _block(binding, reason=reason, journal_commit=commit_sha, evidence_refs=evidence)

    row = active_rows[0]
    candidate = row.get("candidate")
    if not isinstance(candidate, Mapping):
        return _block(binding, reason="target_record_malformed", journal_commit=commit_sha, evidence_refs=evidence)
    try:
        row_binding = journal_helper.validate_source_target_binding(candidate.get("source_target"))
    except Exception:
        return _block(binding, reason="target_record_malformed", journal_commit=commit_sha, evidence_refs=evidence)
    if row_binding != binding:
        return _block(binding, reason="target_record_binding_mismatch", journal_commit=commit_sha, evidence_refs=evidence)
    if candidate.get("generation_id") != binding["original_admission_ref"]["generation_id"]:
        return _block(binding, reason="target_record_binding_mismatch", journal_commit=commit_sha, evidence_refs=evidence)
    observation = getattr(source_target, "original_observation", {})
    expected_repository = getattr(native_reader, "repository", None)
    if (
        not isinstance(expected_repository, str)
        or not re.fullmatch(r"[^/]+/[^/]+", expected_repository)
        or candidate.get("repository", "").casefold() != expected_repository.casefold()
        or candidate.get("source_id") != (observation.get("source_id") if isinstance(observation, Mapping) else None)
        or candidate.get("scope") != (observation.get("source_scope") if isinstance(observation, Mapping) else None)
    ):
        return _block(binding, reason="target_record_binding_mismatch", journal_commit=commit_sha, evidence_refs=evidence)
    status = row.get("status")
    revision_ref: Mapping[str, Any] | None = None
    purpose_ref: Mapping[str, Any] | None = None
    try:
        revision_ref = journal_helper.revision_reference(row)
        closure = journal_helper.release_closure_identity(candidate)
        if closure is not None:
            purpose_ref = closure[0]
    except Exception:
        return _block(binding, reason="target_record_malformed", journal_commit=commit_sha, evidence_refs=evidence)
    if status in TERMINAL_FAILURE:
        return VerifiedCTargetDisposition(
            _VERIFIED_DISPOSITION,
            selection_key=binding["selection_key"],
            original_observation_sha256=binding["original_observation_sha256"],
            target_contract_sha256=binding["target_contract_sha256"],
            disposition="blocked", reason="target_revision_terminal_failure",
            journal_commit=commit_sha, evidence_refs=evidence,
            revision_ref=revision_ref, purpose_ref=purpose_ref,
        )
    if status not in PREPARED | PENDING | MERGED_OR_PUBLISHED:
        return _block(binding, reason="target_record_malformed", journal_commit=commit_sha, evidence_refs=evidence)
    repository = candidate.get("repository")
    if not isinstance(repository, str) or not re.fullmatch(r"[^/]+/[^/]+", repository):
        return _block(binding, reason="target_record_malformed", journal_commit=commit_sha, evidence_refs=evidence)
    observed, pr_error = _validate_native_pr(
        row, repository=repository, native_reader=native_reader,
        journal_helper=journal_helper,
        deadline_monotonic=deadline_monotonic,
    )
    if pr_error is not None:
        return _block(binding, reason=pr_error, journal_commit=commit_sha, evidence_refs=evidence)
    if observed is not None:
        actual_head = observed.get("headRefOid", observed.get("head_sha"))
        evidence.append({
            "kind": "github_pull_request",
            "repository": repository,
            "number": int(row.get("pr", {}).get("number", 0)),
            "head_sha": actual_head,
            "body_sha256": hashlib.sha256(str(observed.get("body", "")).encode("utf-8")).hexdigest(),
            "state": str(observed.get("state", "")).lower(),
        })
        if observed.get("state") == "MERGED":
            merge = observed.get("mergeCommit")
            merge_sha = merge.get("oid") if isinstance(merge, Mapping) else observed.get("merge_commit_sha")
            evidence[-1]["merge_sha"] = merge_sha
            return VerifiedCTargetDisposition(
                _VERIFIED_DISPOSITION,
                selection_key=binding["selection_key"],
                original_observation_sha256=binding["original_observation_sha256"],
                target_contract_sha256=binding["target_contract_sha256"],
                disposition="completed", reason="exact_native_merged_target_revision",
                journal_commit=commit_sha, evidence_refs=evidence,
                revision_ref=revision_ref, purpose_ref=purpose_ref,
            )
    if status in PREPARED | PENDING:
        return VerifiedCTargetDisposition(
            _VERIFIED_DISPOSITION,
            selection_key=binding["selection_key"],
            original_observation_sha256=binding["original_observation_sha256"],
            target_contract_sha256=binding["target_contract_sha256"],
            disposition="pending", reason="exact_prepared_or_open_target_revision",
            journal_commit=commit_sha, evidence_refs=evidence,
            revision_ref=revision_ref, purpose_ref=purpose_ref,
        )
    if status in MERGED_OR_PUBLISHED:
        return _block(binding, reason="pr_readback_mismatch", journal_commit=commit_sha, evidence_refs=evidence)
    return _block(binding, reason="target_record_malformed", journal_commit=commit_sha, evidence_refs=evidence)
