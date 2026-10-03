#!/usr/bin/env python3
"""Safely dispatch and reconcile verify-release for an owned candidate PR.

This module has no GitHub client. Callbacks keep the state machine testable and
let the caller bind persistence to the canonical promotion journal. The
dispatch callback must use the workflow-dispatch REST endpoint with
``X-GitHub-Api-Version: 2026-03-10``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


WORKFLOW_PATH = ".github/workflows/verify-release.yml"
WORKFLOW_EVENT = "workflow_dispatch"
GITHUB_API_VERSION = "2026-03-10"
MAX_MATCHING_RUNS = 100
REQUIRED_JOB_NAMES = {
    "diagnostic-candidate": ("diagnostic-candidate", "Diagnostic candidate (pre-distribution)"),
    "verify": ("verify",),
}
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STATES = frozenset({
    "intent", "uncertain", "queued", "in_progress", "success", "failure",
    "cancelled", "action_required",
})
CI_ENTRY_KEYS = (
    "repository", "workflow_path", "head_sha", "branch", "pr_number",
    "owner_id", "body_sha256", "request_fingerprint", "state", "intent_at",
    "run_id", "run_attempt", "run_url", "run_status", "conclusion",
    "observed_at", "dispatch_http_status", "blocker",
)


class VerifyReleaseDispatchError(RuntimeError):
    """Raised when an input or authoritative identity check is invalid."""


class MultipleMatchingWorkflowRunsError(VerifyReleaseDispatchError):
    """Raised when recovery finds more than one exact workflow run ID."""

    def __init__(self, run_ids: Sequence[int]) -> None:
        self.run_ids = tuple(sorted(set(run_ids)))
        super().__init__("multiple exact workflow_dispatch runs exist for this candidate head")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_int(value: Any, *, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _repository_name(value: Any) -> str | None:
    if isinstance(value, str):
        name = value
    elif isinstance(value, Mapping):
        name = value.get("full_name")
        if not isinstance(name, str):
            owner, repo = value.get("owner"), value.get("name")
            owner_name = owner.get("login") if isinstance(owner, Mapping) else None
            name = f"{owner_name}/{repo}" if isinstance(owner_name, str) and isinstance(repo, str) else None
    else:
        name = None
    return name if isinstance(name, str) and "/" in name else None


def _entry_fingerprint(
    repository: str,
    head_sha: str,
    branch: str,
    pr_number: int,
    owner_id: str,
    body_sha256: str,
) -> str:
    identity = {
        "repository": repository.casefold(),
        "workflow_path": WORKFLOW_PATH,
        "event": WORKFLOW_EVENT,
        "head_sha": head_sha,
        "branch": branch,
        "ref": branch,
        "expected_head_sha": head_sha,
        "pr_number": pr_number,
        "owner_id": owner_id,
        "body_sha256": body_sha256,
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(payload)


def _receipt_identity(repository: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
    candidate = receipt.get("candidate")
    ownership = receipt.get("ownership")
    pr = receipt.get("pr")
    if not isinstance(candidate, Mapping) or not isinstance(ownership, Mapping) or not isinstance(pr, Mapping):
        raise VerifyReleaseDispatchError("promotion receipt is missing candidate, ownership, or PR identity")

    candidate_repository = candidate.get("repository")
    if not isinstance(candidate_repository, str) or candidate_repository.casefold() != repository.casefold():
        raise VerifyReleaseDispatchError("candidate repository does not match the requested repository")
    source_id, scope = candidate.get("source_id"), candidate.get("scope")
    if not isinstance(source_id, str) or not source_id or not isinstance(scope, str) or not scope:
        raise VerifyReleaseDispatchError("candidate source and scope identity are missing")
    base_sha = candidate.get("base_sha")
    if not isinstance(base_sha, str) or not SHA1_RE.fullmatch(base_sha):
        raise VerifyReleaseDispatchError("candidate base must be a full immutable commit SHA")
    head_sha = candidate.get("head_sha")
    if not isinstance(head_sha, str) or not SHA1_RE.fullmatch(head_sha):
        raise VerifyReleaseDispatchError("candidate head must be a full immutable commit SHA")
    branch = ownership.get("branch")
    if not isinstance(branch, str) or not branch or branch.startswith("-") or any(ch.isspace() for ch in branch):
        raise VerifyReleaseDispatchError("candidate ownership branch is missing or invalid")
    owner_id = ownership.get("owner_id")
    if not isinstance(owner_id, str) or not re.fullmatch(r"datapan-canonical-update:v1:[0-9a-f]{64}", owner_id):
        raise VerifyReleaseDispatchError("candidate receipt has no canonical owner identity")
    expected_owner = "datapan-canonical-update:v1:" + _sha256(
        "\0".join((candidate_repository.lower(), source_id, scope)).encode("utf-8")
    )
    if owner_id != expected_owner:
        raise VerifyReleaseDispatchError("candidate owner identity does not match its repository/source/scope")
    generation_id = candidate.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id or any(ch.isspace() for ch in generation_id):
        raise VerifyReleaseDispatchError("candidate generation identity is missing or invalid")
    body_sha256 = ownership.get("body_sha256")
    if not isinstance(body_sha256, str) or not SHA256_RE.fullmatch(body_sha256):
        raise VerifyReleaseDispatchError("candidate receipt has no valid owned PR body digest")
    if ownership.get("expected_head_sha") != head_sha:
        raise VerifyReleaseDispatchError("candidate ownership expected head does not equal the candidate commit")
    pr_number = pr.get("number")
    if not _is_int(pr_number):
        raise VerifyReleaseDispatchError("candidate receipt has no exact positive PR number")
    source_slug = re.sub(r"[^a-z0-9-]+", "-", source_id.lower()).strip("-") or "source"
    scope_hash = _sha256(scope.encode("utf-8"))[:12]
    expected_branch = f"automation/canonical-update/{source_slug}-{scope_hash}"
    if receipt.get("action") == "create_replacement":
        generation_hash = _sha256(generation_id.encode("utf-8"))[:10]
        expected_branch += f"-replacement-{generation_hash}"
    if branch != expected_branch:
        raise VerifyReleaseDispatchError("candidate ownership branch does not match its canonical source/scope")

    return {
        "repository": candidate_repository,
        "workflow_path": WORKFLOW_PATH,
        "head_sha": head_sha,
        "branch": branch,
        "pr_number": pr_number,
        "owner_id": owner_id,
        "body_sha256": body_sha256,
        "generation_id": generation_id,
        "request_fingerprint": _entry_fingerprint(
            candidate_repository, head_sha, branch, pr_number, owner_id, body_sha256
        ),
    }


def _new_entry(identity: Mapping[str, Any], *, state: str, blocker: str | None = None) -> dict[str, Any]:
    now = _now()
    entry = {
        key: identity[key]
        for key in (
            "repository", "workflow_path", "head_sha", "branch", "pr_number",
            "owner_id", "body_sha256", "request_fingerprint",
        )
    }
    entry.update({
        "state": state,
        "intent_at": now,
        "run_id": None,
        "run_attempt": None,
        "run_url": None,
        "run_status": None,
        "conclusion": None,
        "observed_at": now,
        "dispatch_http_status": None,
        "blocker": blocker,
    })
    return entry


def _validate_prior_state(state: Mapping[str, Any] | None, identity: Mapping[str, Any]) -> dict[str, Any] | None:
    if state is None:
        return None
    if not isinstance(state, Mapping):
        raise VerifyReleaseDispatchError("durable CI state must be an object")
    missing = [key for key in CI_ENTRY_KEYS if key not in state]
    if missing:
        raise VerifyReleaseDispatchError("durable CI state is missing fields: " + ", ".join(missing))
    expected = {key: identity[key] for key in (
        "repository", "workflow_path", "head_sha", "branch", "pr_number",
        "owner_id", "body_sha256", "request_fingerprint",
    )}
    for key, value in expected.items():
        actual = state.get(key)
        if actual != value:
            raise VerifyReleaseDispatchError(f"durable CI state {key} does not match the exact candidate")
    if state.get("state") not in STATES:
        raise VerifyReleaseDispatchError("durable CI state has an unsupported state")
    if not isinstance(state.get("intent_at"), str) or not state["intent_at"]:
        raise VerifyReleaseDispatchError("durable CI state is missing its intent timestamp")
    if not isinstance(state.get("observed_at"), str) or not state["observed_at"]:
        raise VerifyReleaseDispatchError("durable CI state is missing its observation timestamp")
    if not isinstance(state.get("blocker"), (str, type(None))):
        raise VerifyReleaseDispatchError("durable CI blocker must be a string or null")
    if not isinstance(state.get("run_url"), (str, type(None))):
        raise VerifyReleaseDispatchError("durable CI run URL must be a string or null")
    if not isinstance(state.get("run_status"), (str, type(None))) or not isinstance(state.get("conclusion"), (str, type(None))):
        raise VerifyReleaseDispatchError("durable CI run status and conclusion must be strings or null")
    http_status = state.get("dispatch_http_status")
    if http_status is not None and (not _is_int(http_status, minimum=100) or http_status > 599):
        raise VerifyReleaseDispatchError("durable CI dispatch HTTP status is invalid")
    run_id, attempt = state.get("run_id"), state.get("run_attempt")
    if run_id is not None and not _is_int(run_id):
        raise VerifyReleaseDispatchError("durable CI run ID is invalid")
    if attempt is not None and not _is_int(attempt):
        raise VerifyReleaseDispatchError("durable CI run attempt is invalid")
    if state.get("state") in {"queued", "in_progress", "success", "failure", "cancelled"} and (run_id is None or attempt is None):
        raise VerifyReleaseDispatchError("observed workflow state has no exact run ID")
    entry = _new_entry(identity, state=str(state["state"]), blocker=state.get("blocker"))
    entry.update({key: copy.deepcopy(state.get(key)) for key in CI_ENTRY_KEYS if key in state})
    return entry


def _save(entry: dict[str, Any], persist: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    entry["observed_at"] = _now()
    persisted = copy.deepcopy(entry)
    persist(persisted)
    return entry


def _with_ci(receipt: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(dict(receipt))
    updated["ci"] = copy.deepcopy(dict(entry))
    return updated


def _read_pr(pr_readback: Callable[[], Mapping[str, Any]]) -> Mapping[str, Any]:
    if not callable(pr_readback):
        raise VerifyReleaseDispatchError("pr_readback must be a callback that performs a fresh PR API read")
    value = pr_readback()
    if not isinstance(value, Mapping):
        raise VerifyReleaseDispatchError("PR API read-back must return an object")
    return value


def _validate_pr(pr: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
    if not _is_int(pr.get("number")) or pr.get("number") != identity["pr_number"]:
        raise VerifyReleaseDispatchError("PR read-back number does not match the owned candidate")
    if pr.get("state") not in {"OPEN", "open"}:
        raise VerifyReleaseDispatchError("candidate PR is not open")
    body = pr.get("body")
    if not isinstance(body, str):
        raise VerifyReleaseDispatchError("candidate PR body is unavailable")
    marker = f"<!-- {identity['owner_id']} generation={identity['generation_id']} -->"
    if marker not in body:
        raise VerifyReleaseDispatchError("PR body is missing the exact candidate owner/generation marker")
    if _sha256(body.encode("utf-8")) != identity["body_sha256"]:
        raise VerifyReleaseDispatchError("human_body_change: PR body no longer matches the owned receipt")
    if pr.get("headRefName") != identity["branch"] or pr.get("baseRefName") != "main":
        raise VerifyReleaseDispatchError("PR branch/base identity differs from the owned candidate")
    if pr.get("headRefOid") != identity["head_sha"]:
        raise VerifyReleaseDispatchError("human_head_change: PR head no longer matches the candidate commit")
    for key in ("repository", "headRepository"):
        actual = pr.get(key)
        actual_name = _repository_name(actual)
        if actual_name is None or actual_name.casefold() != str(identity["repository"]).casefold():
            raise VerifyReleaseDispatchError(f"PR {key} does not match the candidate repository")


def _assert_current_pr_and_branch(
    pr_readback: Callable[[], Mapping[str, Any]],
    read_branch_sha: Callable[[str], str | None],
    identity: Mapping[str, Any],
) -> None:
    _validate_pr(_read_pr(pr_readback), identity)
    branch_sha = read_branch_sha(str(identity["branch"]))
    if branch_sha != identity["head_sha"]:
        raise VerifyReleaseDispatchError("candidate branch no longer points to the exact owned head")


def _path_matches(path_value: Any, repository: str, branch: str) -> bool:
    if not isinstance(path_value, str) or not path_value:
        return False
    path_part, separator, ref_part = path_value.partition("@")
    normalized = path_part
    prefix = repository + "/"
    if normalized.casefold().startswith(prefix.casefold()):
        normalized = normalized[len(prefix):]
    if normalized != WORKFLOW_PATH:
        return False
    return not separator or ref_part == f"refs/heads/{branch}"


def _run_matches(run: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    repository = _repository_name(run.get("repository"))
    head_repository = _repository_name(run.get("head_repository"))
    expected_ref = f"refs/heads/{identity['branch']}"
    path = run.get("path")
    path_has_ref = isinstance(path, str) and "@" in path
    path_ref_matches = not path_has_ref or path.partition("@")[2] == expected_ref
    ref = run.get("ref")
    ref_matches = ref is None or (isinstance(ref, str) and ref == expected_ref)
    return (
        _is_int(run.get("id"))
        and run.get("event") == WORKFLOW_EVENT
        and run.get("head_sha") == identity["head_sha"]
        and run.get("head_branch") == identity["branch"]
        and repository is not None
        and repository.casefold() == str(identity["repository"]).casefold()
        and head_repository is not None
        and head_repository.casefold() == str(identity["repository"]).casefold()
        and _path_matches(run.get("path"), str(identity["repository"]), str(identity["branch"]))
        and path_ref_matches
        and ref_matches
        and _is_int(run.get("run_attempt"))
    )


def _matching_runs(
    list_matching_runs: Callable[[str, str, str, str, str], Sequence[Mapping[str, Any]]],
    identity: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    rows = list_matching_runs(
        str(identity["repository"]), WORKFLOW_PATH, str(identity["branch"]),
        WORKFLOW_EVENT, str(identity["head_sha"]),
    )
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise VerifyReleaseDispatchError("matching workflow-run query must return a bounded sequence")
    if len(rows) > MAX_MATCHING_RUNS:
        raise VerifyReleaseDispatchError("matching workflow-run query exceeded the bounded result limit")
    if any(not isinstance(row, Mapping) for row in rows):
        raise VerifyReleaseDispatchError("matching workflow-run query contains a malformed row")
    exact = [row for row in rows if _run_matches(row, identity)]
    by_id: dict[int, Mapping[str, Any]] = {}
    for row in exact:
        run_id = int(row["id"])
        previous = by_id.get(run_id)
        if previous is None or int(row["run_attempt"]) > int(previous["run_attempt"]):
            by_id[run_id] = row
    if len(by_id) > 1:
        raise MultipleMatchingWorkflowRunsError(tuple(by_id))
    return list(by_id.values())


def _entry_for_run(entry: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
    status = run.get("status")
    conclusion = run.get("conclusion")
    if status in {"requested", "waiting", "pending", "queued"}:
        state, blocker = "queued", "verify_release_pending"
    elif status == "in_progress":
        state, blocker = "in_progress", "verify_release_pending"
    elif conclusion == "success":
        state, blocker = _required_jobs_outcome(run.get("jobs"))
    elif conclusion == "cancelled":
        state, blocker = "cancelled", "verify_release_cancelled"
    elif conclusion == "action_required":
        state, blocker = "action_required", "verify_release_action_required"
    elif status == "completed":
        state, blocker = "failure", "verify_release_failed"
    else:
        state, blocker = "action_required", "verify_release_unknown_conclusion"
    result = copy.deepcopy(dict(entry))
    run_url = run.get("html_url") or run.get("run_url") or result.get("run_url")
    result.update({
        "state": state,
        "run_id": int(run["id"]),
        "run_attempt": int(run["run_attempt"]),
        "run_url": run_url if isinstance(run_url, str) else None,
        "run_status": status,
        "conclusion": conclusion,
        "blocker": blocker,
    })
    return result


def _required_jobs_outcome(jobs_value: Any) -> tuple[str, str | None]:
    if not isinstance(jobs_value, Sequence) or isinstance(jobs_value, (str, bytes, bytearray)):
        return "action_required", "verify_release_required_jobs_missing"
    if any(not isinstance(job, Mapping) for job in jobs_value):
        return "action_required", "verify_release_job_readback_malformed"

    required: dict[str, list[Mapping[str, Any]]] = {key: [] for key in REQUIRED_JOB_NAMES}
    for job in jobs_value:
        name = job.get("job_id") or job.get("name")
        for required_id, aliases in REQUIRED_JOB_NAMES.items():
            if name in aliases:
                required[required_id].append(job)

    if any(len(rows) != 1 for rows in required.values()):
        return "action_required", "verify_release_required_jobs_missing_or_duplicated"

    for rows in required.values():
        job = rows[0]
        if job.get("status") != "completed":
            return "action_required", "verify_release_required_job_not_completed"
        conclusion = job.get("conclusion")
        if conclusion == "success":
            continue
        if conclusion == "cancelled":
            return "cancelled", "verify_release_required_job_cancelled"
        if conclusion in {"failure", "timed_out", "startup_failure"}:
            return "failure", "verify_release_required_job_failed"
        return "action_required", "verify_release_required_job_not_successful"
    return "success", None


def _action_required(entry: Mapping[str, Any], blocker: str) -> dict[str, Any]:
    result = copy.deepcopy(dict(entry))
    result["state"] = "action_required"
    result["blocker"] = blocker
    return result


def _read_and_record_run(
    entry: dict[str, Any],
    run_id: int,
    *,
    listed_attempt: int | None = None,
    identity: Mapping[str, Any],
    read_run: Callable[[int], Mapping[str, Any]],
    persist: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    entry = copy.deepcopy(entry)
    prior_attempt = entry.get("run_attempt")
    if listed_attempt is not None and prior_attempt is not None and listed_attempt < int(prior_attempt):
        return _save(_action_required(entry, "verify_release_run_attempt_regressed"), persist)
    attempt_floor = max(
        (int(value) for value in (prior_attempt, listed_attempt) if value is not None),
        default=None,
    )
    entry.update({
        "state": "uncertain",
        "run_id": run_id,
        "blocker": "verify_release_run_readback_pending",
    })
    if attempt_floor is not None:
        entry["run_attempt"] = attempt_floor
    entry = _save(entry, persist)
    try:
        run = read_run(run_id)
    except Exception:
        entry["state"] = "uncertain"
        entry["blocker"] = "verify_release_run_readback_failed"
        return _save(entry, persist)
    try:
        if not isinstance(run, Mapping):
            raise VerifyReleaseDispatchError("workflow-run API read-back must return an object")
        if run.get("id") != run_id or not _run_matches(run, identity):
            raise VerifyReleaseDispatchError("workflow-run API identity differs from the exact requested repository/workflow/head")
        if run.get("status") not in {"requested", "waiting", "pending", "queued", "in_progress", "completed"}:
            raise VerifyReleaseDispatchError("workflow-run API returned an unsupported status")
        if attempt_floor is not None and int(run["run_attempt"]) < attempt_floor:
            raise VerifyReleaseDispatchError("workflow-run API attempt regressed from the durable observation")
        if run.get("status") == "completed" and not isinstance(run.get("conclusion"), str):
            raise VerifyReleaseDispatchError("completed workflow run has no conclusion")
    except VerifyReleaseDispatchError as exc:
        entry["state"] = "action_required"
        entry["blocker"] = (
            "verify_release_run_attempt_regressed"
            if "attempt regressed" in str(exc)
            else "verify_release_run_identity_or_status_invalid"
        )
        return _save(entry, persist)
    return _save(_entry_for_run(entry, run), persist)


def _finalize_identity(
    entry: dict[str, Any],
    *,
    identity: Mapping[str, Any],
    pr_readback: Callable[[], Mapping[str, Any]],
    read_branch_sha: Callable[[str], str | None],
    persist: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    try:
        _assert_current_pr_and_branch(pr_readback, read_branch_sha, identity)
    except Exception:
        return _save(_action_required(entry, "verify_release_candidate_identity_changed"), persist)
    return entry


def _result(receipt: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    return _with_ci(receipt, entry)


def ensure_verify_release_run(
    repository: str,
    receipt: Mapping[str, Any],
    pr_readback: Callable[[], Mapping[str, Any]],
    dispatch_state: Mapping[str, Any] | None,
    *,
    read_branch_sha: Callable[[str], str | None],
    list_matching_runs: Callable[[str, str, str, str, str], Sequence[Mapping[str, Any]]],
    dispatch: Callable[[str, Mapping[str, str]], tuple[int | None, Mapping[str, Any] | None]],
    read_run: Callable[[int], Mapping[str, Any]],
    persist: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """Ensure the exact candidate head has a recorded verify-release run.

    ``persist`` must durably write a copy of the CI entry into the current
    promotion receipt/journal and fail on compare-and-swap conflicts. A durable
    ``intent`` is stored before POST. Any prior intent or uncertain result is
    reconciled by exact workflow/branch/event/head queries and is never
    blindly redispatched. The run-list callback must return a complete bounded
    set of matching candidates; ``read_run`` must independently fetch one run
    by ID and include the latest-attempt required-job read-back. Existing
    ``action_required`` entries may recover through those read-only callbacks,
    but never authorize another POST.
    """
    if not isinstance(repository, str) or "/" not in repository:
        raise VerifyReleaseDispatchError("repository must be an owner/name identity")
    if not isinstance(receipt, Mapping):
        raise VerifyReleaseDispatchError("promotion receipt must be an object")
    identity = _receipt_identity(repository, receipt)
    prior = _validate_prior_state(dispatch_state, identity)

    try:
        _assert_current_pr_and_branch(pr_readback, read_branch_sha, identity)
    except Exception as exc:
        if prior is None:
            raise VerifyReleaseDispatchError(f"owned candidate PR preflight failed: {exc}") from exc
        return _result(receipt, _save(_action_required(prior, "verify_release_candidate_identity_changed"), persist))

    try:
        matching = _matching_runs(list_matching_runs, identity)
    except VerifyReleaseDispatchError as exc:
        if isinstance(exc, MultipleMatchingWorkflowRunsError):
            entry = prior or _new_entry(identity, state="action_required")
            ids = ",".join(str(run_id) for run_id in exc.run_ids)
            entry = _save(_action_required(entry, f"verify_release_multiple_matching_runs:{ids}"), persist)
            return _result(receipt, entry)
        if prior is None:
            raise
        entry = copy.deepcopy(prior)
        entry["state"] = "uncertain"
        entry["blocker"] = "verify_release_run_list_readback_failed"
        return _result(receipt, _save(entry, persist))
    except Exception:
        if prior is None:
            raise VerifyReleaseDispatchError("matching workflow-run query failed before dispatch")
        entry = copy.deepcopy(prior)
        entry["state"] = "uncertain"
        entry["blocker"] = "verify_release_run_list_readback_failed"
        return _result(receipt, _save(entry, persist))

    if matching:
        run_id = int(matching[0]["id"])
        if prior is not None and prior.get("run_id") is not None and int(prior["run_id"]) != run_id:
            blocker = f"verify_release_run_id_changed:{prior['run_id']}->{run_id}"
            entry = _save(_action_required(prior, blocker), persist)
            return _result(receipt, entry)
        entry = prior or _new_entry(identity, state="uncertain", blocker="verify_release_run_readback_pending")
        entry = _read_and_record_run(
            entry, run_id, listed_attempt=int(matching[0]["run_attempt"]),
            identity=identity, read_run=read_run, persist=persist
        )
        entry = _finalize_identity(
            entry, identity=identity, pr_readback=pr_readback,
            read_branch_sha=read_branch_sha, persist=persist,
        )
        return _result(receipt, entry)

    if prior is not None:
        if prior.get("run_id") is not None:
            entry = _read_and_record_run(
                prior, int(prior["run_id"]), identity=identity,
                read_run=read_run, persist=persist,
            )
            entry = _finalize_identity(
                entry, identity=identity, pr_readback=pr_readback,
                read_branch_sha=read_branch_sha, persist=persist,
            )
            return _result(receipt, entry)
        entry = copy.deepcopy(prior)
        if entry["state"] != "action_required":
            entry["state"] = "uncertain"
            entry["blocker"] = "verify_release_dispatch_uncertain"
        return _result(receipt, _save(entry, persist))

    # The intent is the durable barrier against a duplicate POST after a crash.
    entry = _save(_new_entry(identity, state="intent"), persist)
    try:
        _assert_current_pr_and_branch(pr_readback, read_branch_sha, identity)
    except Exception:
        entry = _save(_action_required(entry, "verify_release_candidate_identity_changed_before_dispatch"), persist)
        return _result(receipt, entry)

    try:
        status_code, body = dispatch(str(identity["branch"]), {"expected_head_sha": str(identity["head_sha"])})
    except Exception:
        entry["state"] = "uncertain"
        entry["blocker"] = "verify_release_dispatch_outcome_unknown"
        entry = _save(entry, persist)
        return _reconcile_uncertain(
            receipt, entry, identity, list_matching_runs, read_run, persist,
            pr_readback, read_branch_sha,
        )

    valid_status = _is_int(status_code, minimum=100) if status_code is not None else False
    if valid_status and status_code > 599:
        valid_status = False
    run_id_value = body.get("workflow_run_id") if isinstance(body, Mapping) else None
    if _is_int(status_code, minimum=200) and status_code == 200 and _is_int(run_id_value):
        entry["state"] = "uncertain"
        entry["run_id"] = int(run_id_value)
        run_url = body.get("run_url") or body.get("html_url") if isinstance(body, Mapping) else None
        entry["run_url"] = run_url if isinstance(run_url, str) else None
        entry["dispatch_http_status"] = 200
        entry["blocker"] = "verify_release_run_readback_pending"
        entry = _save(entry, persist)
        entry = _read_and_record_run(
            entry, int(run_id_value), identity=identity, read_run=read_run, persist=persist
        )
        entry = _finalize_identity(
            entry, identity=identity, pr_readback=pr_readback,
            read_branch_sha=read_branch_sha, persist=persist,
        )
        return _result(receipt, entry)

    entry["state"] = "uncertain"
    entry["dispatch_http_status"] = int(status_code) if valid_status else None
    entry["blocker"] = "verify_release_dispatch_outcome_unknown"
    entry = _save(entry, persist)
    return _reconcile_uncertain(
        receipt, entry, identity, list_matching_runs, read_run, persist,
        pr_readback, read_branch_sha,
    )


def _reconcile_uncertain(
    receipt: Mapping[str, Any],
    entry: dict[str, Any],
    identity: Mapping[str, Any],
    list_matching_runs: Callable[[str, str, str, str, str], Sequence[Mapping[str, Any]]],
    read_run: Callable[[int], Mapping[str, Any]],
    persist: Callable[[dict[str, Any]], Any],
    pr_readback: Callable[[], Mapping[str, Any]],
    read_branch_sha: Callable[[str], str | None],
) -> dict[str, Any]:
    try:
        matching = _matching_runs(list_matching_runs, identity)
    except VerifyReleaseDispatchError as exc:
        if isinstance(exc, MultipleMatchingWorkflowRunsError):
            ids = ",".join(str(run_id) for run_id in exc.run_ids)
            entry = _save(_action_required(entry, f"verify_release_multiple_matching_runs:{ids}"), persist)
            entry = _finalize_identity(
                entry, identity=identity, pr_readback=pr_readback,
                read_branch_sha=read_branch_sha, persist=persist,
            )
            return _result(receipt, entry)
        entry["state"] = "uncertain"
        entry["blocker"] = "verify_release_run_list_readback_failed"
        entry = _save(entry, persist)
        entry = _finalize_identity(
            entry, identity=identity, pr_readback=pr_readback,
            read_branch_sha=read_branch_sha, persist=persist,
        )
        return _result(receipt, entry)
    except Exception:
        entry["state"] = "uncertain"
        entry["blocker"] = "verify_release_run_list_readback_failed"
        entry = _save(entry, persist)
        entry = _finalize_identity(
            entry, identity=identity, pr_readback=pr_readback,
            read_branch_sha=read_branch_sha, persist=persist,
        )
        return _result(receipt, entry)
    if not matching:
        entry = _finalize_identity(
            entry, identity=identity, pr_readback=pr_readback,
            read_branch_sha=read_branch_sha, persist=persist,
        )
        return _result(receipt, entry)
    run_id = int(matching[0]["id"])
    if entry.get("run_id") is not None and int(entry["run_id"]) != run_id:
        blocker = f"verify_release_run_id_changed:{entry['run_id']}->{run_id}"
        entry = _save(_action_required(entry, blocker), persist)
        entry = _finalize_identity(
            entry, identity=identity, pr_readback=pr_readback,
            read_branch_sha=read_branch_sha, persist=persist,
        )
        return _result(receipt, entry)
    entry = _read_and_record_run(
        entry, run_id, listed_attempt=int(matching[0]["run_attempt"]),
        identity=identity, read_run=read_run, persist=persist,
    )
    entry = _finalize_identity(
        entry, identity=identity, pr_readback=pr_readback,
        read_branch_sha=read_branch_sha, persist=persist,
    )
    return _result(receipt, entry)
