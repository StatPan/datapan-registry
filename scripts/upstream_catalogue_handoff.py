#!/usr/bin/env python3
"""Bounded missed-observation discovery and durable admission ledger helpers.

The Actions workflow supplies authenticated run/artifact metadata.  This module
owns the shared ordering and state invariants used by schedule, event, and
explicit producer delivery.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any


LEDGER_VERSION = "datapan.upstream-catalogue-handoff-ledger.v1"
MAX_ADMITTED_OBSERVATIONS = 256
LOOKBACK_DAYS = 30
HEX64 = re.compile(r"[a-f0-9]{64}")
SHA256_API = re.compile(r"sha256:([a-f0-9]{64})")
MAX_DISCOVERY_PAGES = 3
MAX_EXACT_RUNS = 5
MAX_ARTIFACT_RESOLUTIONS = 5
MAX_API_REQUESTS = 40
DISCOVERY_API_REQUEST_LIMIT = 31
PAGE_SIZE = 100
PRODUCER_WORKFLOW_NAME = "Upstream catalog refresh"
PRODUCER_WORKFLOW_PATH = ".github/workflows/upstream-catalog-refresh.yml"
PRODUCER_JOB_NAME = "observe"


class HandoffError(ValueError):
    """A fail-closed collector handoff or ledger validation failure."""


class GitHubReadBudget:
    """Count all API reads and allow one retry for a failed read."""

    def __init__(self, get_json, *, maximum: int = MAX_API_REQUESTS, initial: int = 0):
        self._get_json = get_json
        self.requests = initial
        self.maximum = maximum

    def get(self, endpoint: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for _attempt in range(2):
            if self.requests >= self.maximum:
                raise HandoffError("handoff_discovery_api_budget_exhausted")
            self.requests += 1
            try:
                result = self._get_json(endpoint)
                if not isinstance(result, dict):
                    raise HandoffError("handoff_api_response_not_object")
                return result
            except Exception as exc:  # one bounded retry per failed read
                last_error = exc
        raise HandoffError("handoff_api_read_failed") from last_error


def gh_api_json(endpoint: str) -> dict[str, Any]:
    """Read one GitHub API endpoint through the installed authenticated gh CLI."""
    result = subprocess.run(
        ["gh", "api", "--method", "GET", endpoint],
        text=True, capture_output=True, check=False, timeout=30,
    )  # noqa: S603
    if result.returncode:
        raise HandoffError("handoff_github_api_failed")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HandoffError("handoff_github_api_invalid_json") from exc
    if not isinstance(value, dict):
        raise HandoffError("handoff_api_response_not_object")
    return value


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 and str(value) == str(parsed) else None


def _trusted_completed_summary(
    row: Mapping[str, Any], *, workflow_id: int, repository: str, default_branch: str,
) -> bool:
    repository_row = row.get("repository")
    head_repository_row = row.get("head_repository")
    if (
        _positive_int(row.get("id")) is None
        or _positive_int(row.get("run_attempt")) is None
        or row.get("workflow_id") != workflow_id
        or row.get("name") != PRODUCER_WORKFLOW_NAME
        or row.get("path") != PRODUCER_WORKFLOW_PATH
        or row.get("event") not in {"schedule", "workflow_dispatch"}
        or row.get("status") != "completed"
        or row.get("conclusion") != "success"
        or row.get("head_branch") != default_branch
        or not isinstance(repository_row, dict)
        or repository_row.get("full_name") != repository
        or _positive_int(repository_row.get("id")) is None
        or not isinstance(head_repository_row, dict)
        or head_repository_row.get("full_name") != repository
        or _positive_int(head_repository_row.get("id")) is None
        or str(repository_row.get("id")) != str(head_repository_row.get("id"))
        or not re.fullmatch(r"[a-f0-9]{40}", str(row.get("head_sha") or ""))
    ):
        return False
    parse_time(str(row.get("run_started_at") or ""))
    return True


def _same_run_identity(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    keys = (
        "id", "workflow_id", "name", "path", "event", "status", "conclusion", "run_attempt",
        "head_branch", "head_sha", "run_started_at", "updated_at",
    )
    if any(first.get(key) != second.get(key) for key in keys):
        return False
    first_repo = first.get("repository")
    second_repo = second.get("repository")
    first_head_repo = first.get("head_repository")
    second_head_repo = second.get("head_repository")
    return (
        isinstance(first_repo, dict) and isinstance(second_repo, dict)
        and first_repo.get("full_name") == second_repo.get("full_name")
        and str(first_repo.get("id")) == str(second_repo.get("id"))
        and isinstance(first_head_repo, dict) and isinstance(second_head_repo, dict)
        and first_head_repo.get("full_name") == second_head_repo.get("full_name")
        and str(first_head_repo.get("id")) == str(second_head_repo.get("id"))
    )


def _page_collection(
    budget: GitHubReadBudget, base_endpoint: str, *, collection_key: str,
    max_pages: int, required_total: bool = True,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    expected_total: int | None = None
    for page in range(1, max_pages + 1):
        separator = "&" if "?" in base_endpoint else "?"
        value = budget.get(f"{base_endpoint}{separator}per_page={PAGE_SIZE}&page={page}")
        items = value.get(collection_key)
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise HandoffError("handoff_api_page_shape_invalid")
        if required_total:
            total = value.get("total_count")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise HandoffError("handoff_api_total_count_invalid")
            if expected_total is None:
                expected_total = total
            elif expected_total != total:
                raise HandoffError("handoff_api_pagination_total_changed")
        output.extend(items)
        item_ids = [str(item.get("id") or "") for item in output]
        if any(not value for value in item_ids) or len(item_ids) != len(set(item_ids)):
            raise HandoffError("handoff_api_page_identity_duplicate_or_missing")
        if len(items) < PAGE_SIZE:
            if expected_total is not None and len(output) != expected_total:
                raise HandoffError("handoff_api_pagination_incomplete")
            return output
        if expected_total is not None and len(output) == expected_total:
            return output
    if expected_total is not None and len(output) != expected_total:
        raise HandoffError("handoff_api_pagination_bound_exhausted")
    raise HandoffError("handoff_api_pagination_bound_exhausted")


def _run_rows(
    budget: GitHubReadBudget, *, repository: str, default_branch: str, now: dt.datetime,
) -> tuple[int, list[dict[str, Any]]]:
    repo_q = urllib.parse.quote(repository, safe="/")
    workflow = budget.get(f"repos/{repo_q}/actions/workflows/upstream-catalog-refresh.yml")
    workflow_id = _positive_int(workflow.get("id"))
    if (
        workflow_id is None or workflow.get("name") != PRODUCER_WORKFLOW_NAME
        or workflow.get("path") != PRODUCER_WORKFLOW_PATH or workflow.get("state") != "active"
    ):
        raise HandoffError("handoff_producer_workflow_identity_invalid")
    cutoff = (now - dt.timedelta(days=LOOKBACK_DAYS)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    query = urllib.parse.urlencode({"branch": default_branch, "created": f">={cutoff}"})
    endpoint = f"repos/{repo_q}/actions/workflows/{workflow_id}/runs?{query}"
    rows = _page_collection(budget, endpoint, collection_key="workflow_runs", max_pages=MAX_DISCOVERY_PAGES)
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        run_id = str(row.get("id") or "")
        if not run_id:
            raise HandoffError("handoff_run_summary_missing_id")
        prior = by_id.get(run_id)
        if prior is not None and not _same_run_identity(prior, row):
            raise HandoffError("handoff_run_summary_identity_conflict")
        by_id[run_id] = row
    return workflow_id, list(by_id.values())


def _exact_observation(
    budget: GitHubReadBudget, summary: Mapping[str, Any], *, workflow_id: int,
    repository: str, default_branch: str, now: dt.datetime,
    run_before: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = str(summary["id"])
    attempt = _positive_int(summary.get("run_attempt"))
    if attempt is None:
        raise HandoffError("handoff_attempt_identity_invalid")
    repo_q = urllib.parse.quote(repository, safe="/")
    run_endpoint = f"repos/{repo_q}/actions/runs/{run_id}"
    before = dict(run_before) if run_before is not None else budget.get(run_endpoint)
    if not _same_run_identity(summary, before) or not _trusted_completed_summary(
        before, workflow_id=workflow_id, repository=repository, default_branch=default_branch,
    ):
        raise HandoffError("handoff_exact_run_identity_changed")
    if parse_time(str(before["run_started_at"])) > now:
        raise HandoffError("handoff_producer_run_started_in_future")
    run_updated = parse_time(str(before.get("updated_at") or ""))
    run_started = parse_time(str(before["run_started_at"]))
    if run_updated < run_started or run_updated > now + dt.timedelta(minutes=5):
        raise HandoffError("handoff_producer_run_completion_time_invalid")
    jobs = _page_collection(
        budget,
        f"repos/{repo_q}/actions/runs/{run_id}/attempts/{attempt}/jobs",
        collection_key="jobs", max_pages=1,
    )
    if not jobs or any(job.get("status") != "completed" or job.get("conclusion") != "success" for job in jobs):
        raise HandoffError("handoff_exact_attempt_jobs_not_successful")
    observe_jobs = [job for job in jobs if job.get("name") == PRODUCER_JOB_NAME]
    if len(observe_jobs) != 1:
        raise HandoffError("handoff_observe_job_identity_ambiguous")
    job = observe_jobs[0]
    for item in jobs:
        if (
            str(item.get("run_id")) != run_id
            or _positive_int(item.get("run_attempt")) != attempt
            or item.get("head_sha") != before.get("head_sha")
        ):
            raise HandoffError("handoff_job_attempt_identity_mismatch")
    job_started = parse_time(str(job.get("started_at") or ""))
    job_completed = parse_time(str(job.get("completed_at") or ""))
    if job_completed < job_started:
        raise HandoffError("handoff_observe_job_interval_invalid")
    artifacts = _page_collection(
        budget, f"repos/{repo_q}/actions/runs/{run_id}/artifacts",
        collection_key="artifacts", max_pages=2,
    )
    expected_name = f"upstream-catalog-refresh-{run_id}"
    matches = [artifact for artifact in artifacts if artifact.get("name") == expected_name]
    if len(matches) != 1:
        raise HandoffError("handoff_expected_artifact_not_unique")
    artifact = matches[0]
    artifact_id = _positive_int(artifact.get("id"))
    size = artifact.get("size_in_bytes")
    digest_match = SHA256_API.fullmatch(str(artifact.get("digest") or ""))
    expiry = parse_time(str(artifact.get("expires_at") or ""))
    created = parse_time(str(artifact.get("created_at") or ""))
    workflow_run = artifact.get("workflow_run")
    if (
        artifact_id is None or not isinstance(size, int) or isinstance(size, bool) or size <= 0
        or not digest_match or artifact.get("expired") is not False or expiry <= now
        or created < job_started or created > job_completed
        or not isinstance(workflow_run, dict)
        or str(workflow_run.get("id")) != run_id
        or workflow_run.get("head_sha") != before.get("head_sha")
        or workflow_run.get("head_branch") != default_branch
        or str(workflow_run.get("repository_id")) != str((before.get("repository") or {}).get("id"))
        or str(workflow_run.get("head_repository_id")) != str((before.get("head_repository") or {}).get("id"))
    ):
        raise HandoffError("handoff_artifact_not_bound_to_exact_attempt")
    after = budget.get(run_endpoint)
    if not _same_run_identity(before, after) or _positive_int(after.get("run_attempt")) != attempt:
        raise HandoffError("handoff_producer_attempt_changed_during_discovery")
    return {
        "producer_run_id": run_id,
        "run_attempt": attempt,
        "head_sha": str(before["head_sha"]),
        "run_started_at": str(before["run_started_at"]),
        # `updated_at` is used only to detect a change between the initial and
        # final API reads. The authenticated observe job interval supplies the
        # completion boundary used by the archive/envelope contract.
        "run_completed_at": job_completed.isoformat().replace("+00:00", "Z"),
        "run_updated_at": str(before["updated_at"]),
        "observe_job_started_at": job_started.isoformat().replace("+00:00", "Z"),
        "observe_job_completed_at": job_completed.isoformat().replace("+00:00", "Z"),
        "artifact_id": str(artifact_id),
        "artifact_name": expected_name,
        "artifact_expires_at": str(artifact["expires_at"]),
        "artifact_digest_sha256": digest_match.group(1),
        "artifact_size_bytes": size,
        "artifact_created_at": str(artifact["created_at"]),
        "repository_id": str((before.get("repository") or {}).get("id") or ""),
        "head_repository_id": str((before.get("head_repository") or {}).get("id") or ""),
        "producer_url": str(before.get("html_url") or ""),
        "event": str(before.get("event") or ""),
    }


def inspect_producer_run(
    get_json, *, repository: str, default_branch: str, run_id: str,
    expected_head_sha: str | None = None, expected_run_attempt: int | None = None,
    prior_api_requests: int = 0, reserve_api_requests: int = 0,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Resolve one event/dispatch-selected run to an exact trusted attempt and artifact."""
    if (
        not isinstance(prior_api_requests, int) or isinstance(prior_api_requests, bool) or prior_api_requests < 0
        or not isinstance(reserve_api_requests, int) or isinstance(reserve_api_requests, bool) or reserve_api_requests < 0
        or prior_api_requests + reserve_api_requests >= MAX_API_REQUESTS
    ):
        raise HandoffError("handoff_api_budget_invalid")
    budget = GitHubReadBudget(
        get_json, maximum=MAX_API_REQUESTS - reserve_api_requests,
        initial=prior_api_requests,
    )
    repo_q = urllib.parse.quote(repository, safe="/")
    workflow = budget.get(f"repos/{repo_q}/actions/workflows/upstream-catalog-refresh.yml")
    workflow_id = _positive_int(workflow.get("id"))
    if (
        workflow_id is None or workflow.get("name") != PRODUCER_WORKFLOW_NAME
        or workflow.get("path") != PRODUCER_WORKFLOW_PATH or workflow.get("state") != "active"
    ):
        raise HandoffError("handoff_producer_workflow_identity_invalid")
    if not str(run_id).isdigit():
        raise HandoffError("handoff_run_id_invalid")
    run = budget.get(f"repos/{repo_q}/actions/runs/{run_id}")
    if (
        not _trusted_completed_summary(run, workflow_id=workflow_id, repository=repository, default_branch=default_branch)
        or str(run.get("id")) != str(run_id)
    ):
        raise HandoffError("handoff_producer_run_untrusted")
    if expected_head_sha and run.get("head_sha") != expected_head_sha:
        raise HandoffError("handoff_producer_event_head_mismatch")
    if expected_run_attempt is not None and _positive_int(run.get("run_attempt")) != expected_run_attempt:
        raise HandoffError("handoff_producer_event_attempt_mismatch")
    result = _exact_observation(
        budget, run, workflow_id=workflow_id, repository=repository,
        default_branch=default_branch, now=now or dt.datetime.now(dt.timezone.utc),
        run_before=run,
    )
    result["producer_url"] = str(run.get("html_url") or "")
    result["event"] = str(run.get("event") or "")
    result["api_requests"] = budget.requests
    return result


def discover_oldest_unseen(
    get_json, *, repository: str, default_branch: str, now: dt.datetime,
    ledger_value: Any, legacy_floor: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str], int]:
    """Discover bounded A handoffs and validate exact run/job/artifact provenance.

    Returns the oldest authenticated unseen producer, explicit diagnostic codes,
    and the number of actual API reads.  It never downloads an artifact.
    """
    budget = GitHubReadBudget(get_json, maximum=DISCOVERY_API_REQUEST_LIMIT)
    workflow_id, rows = _run_rows(
        budget, repository=repository, default_branch=default_branch, now=now,
    )
    trusted = []
    for row in rows:
        if row.get("workflow_id") != workflow_id:
            raise HandoffError("handoff_run_workflow_id_mismatch")
        # Non-observation runs from the trusted workflow (failed, forked, or
        # another event/branch) are intentionally not candidates.
        if _trusted_completed_summary(row, workflow_id=workflow_id, repository=repository, default_branch=default_branch):
            trusted.append(row)
    trusted.sort(key=lambda item: (parse_time(str(item.get("run_started_at"))), int(str(item["id"]))))
    ledger = validate_ledger(ledger_value)
    floor = validate_floor(legacy_floor) if legacy_floor is not None else ledger["legacy_discovery_floor"]
    if floor is not None and floor.get("run_started_at") is None and floor.get("producer_run_id"):
        floor_summary = next((row for row in trusted if str(row.get("id")) == str(floor["producer_run_id"])), None)
        if floor_summary is not None:
            floor = dict(floor, run_started_at=str(floor_summary["run_started_at"]))
    floor_cutoff = None
    if floor is not None:
        floor_cutoff = parse_time(
            floor.get("run_started_at") or floor.get("lookback_started_at") or floor.get("observed_at")
        )
    unseen = []
    for row in trusted:
        run_id = str(row["id"])
        start = parse_time(str(row.get("run_started_at")))
        if floor is not None:
            if run_id == str(floor.get("producer_run_id")):
                unseen.append(row)  # artifact identity decides whether this is the old floor or a new attempt
                continue
            if floor_cutoff is not None and start <= floor_cutoff:
                continue
        unseen.append(row)
    if not unseen:
        return None, [], budget.requests
    diagnostics: list[str] = []
    floor_artifact = (floor or {}).get("artifact_id")
    admitted_attempts = {
        (item["producer_run_id"], item["run_attempt"])
        for item in ledger["admitted_observations"]
    }
    # Filter exact event/schedule replays before spending the bounded exact
    # job/artifact resolutions. A later run_attempt of the same workflow run
    # remains eligible because its key differs.
    exact_candidates = [
        row for row in unseen
        if (str(row["id"]), _positive_int(row.get("run_attempt"))) not in admitted_attempts
    ]
    if len(exact_candidates) > MAX_EXACT_RUNS:
        diagnostics.append("handoff_exact_run_bound_reached")
    for row in exact_candidates[:MAX_EXACT_RUNS]:
        exact = _exact_observation(
            budget, row, workflow_id=workflow_id, repository=repository,
            default_branch=default_branch, now=now,
        )
        if (
            floor is not None and exact["producer_run_id"] == str(floor.get("producer_run_id"))
            and floor_artifact is not None and exact["artifact_id"] == str(floor_artifact)
            and (floor.get("run_attempt") is None or exact["run_attempt"] == floor.get("run_attempt"))
        ):
            continue
        candidate = dict(exact)
        candidate.pop("artifact_created_at", None)
        candidate.pop("producer_url", None)
        candidate.pop("event", None)
        if was_admitted(ledger, candidate):
            continue
        if any(
            item["producer_run_id"] == exact["producer_run_id"]
            and item["run_attempt"] == exact["run_attempt"]
            for item in ledger["admitted_observations"]
        ):
            raise HandoffError("handoff_admitted_attempt_identity_conflict")
        # Discovery has only run/job/API evidence. The source timestamp and
        # evidence digest are authenticated after the single selected archive
        # download in the normal admission path.
        return candidate, diagnostics, budget.requests
    if len(exact_candidates) > MAX_EXACT_RUNS:
        diagnostics.append("handoff_unexamined_trusted_runs_remain")
    return None, diagnostics, budget.requests


def parse_time(value: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise HandoffError("handoff_timestamp_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HandoffError("handoff_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise HandoffError("handoff_timestamp_timezone_missing")
    return parsed.astimezone(dt.timezone.utc)


def admission_order_key(value: Mapping[str, Any]) -> tuple[dt.datetime, dt.datetime, int, int]:
    """Order authenticated observations by source time, attempt start and identity."""
    observed = parse_time(str(value["observed_at"]))
    started = parse_time(str(value["run_started_at"]))
    producer_id = str(value["producer_run_id"])
    attempt = value["run_attempt"]
    if (
        not producer_id.isdigit() or not isinstance(attempt, int)
        or isinstance(attempt, bool) or attempt < 1
    ):
        raise HandoffError("handoff_admission_identity_invalid")
    return observed, started, int(producer_id), attempt


def admission_id(producer_run_id: str, run_attempt: int, evidence_sha256: str) -> str:
    if (
        not str(producer_run_id).isdigit() or not isinstance(run_attempt, int)
        or isinstance(run_attempt, bool) or run_attempt < 1
    ):
        raise HandoffError("handoff_admission_identity_invalid")
    if not isinstance(evidence_sha256, str) or not HEX64.fullmatch(evidence_sha256):
        raise HandoffError("handoff_evidence_digest_invalid")
    canonical = json.dumps(
        [str(producer_run_id), run_attempt, evidence_sha256],
        separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_floor(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict) and value.get("basis") == "bounded_initial_lookback":
        if set(value) != {"basis", "lookback_started_at"}:
            raise HandoffError("corrupt_legacy_discovery_floor")
        parse_time(value["lookback_started_at"])
        return dict(value)
    required = {"observed_at", "generation_id", "evidence_sha256", "basis"}
    optional = {"producer_run_id", "artifact_id", "run_attempt", "run_started_at"}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - required - optional:
        raise HandoffError("corrupt_legacy_discovery_floor")
    parse_time(value["observed_at"])
    if not HEX64.fullmatch(str(value["generation_id"])) or not HEX64.fullmatch(str(value["evidence_sha256"])):
        raise HandoffError("corrupt_legacy_discovery_floor")
    if not isinstance(value["basis"], str) or not value["basis"]:
        raise HandoffError("corrupt_legacy_discovery_floor")
    if "producer_run_id" in value and not str(value["producer_run_id"]).isdigit():
        raise HandoffError("corrupt_legacy_discovery_floor")
    if "artifact_id" in value and not str(value["artifact_id"]).isdigit():
        raise HandoffError("corrupt_legacy_discovery_floor")
    if "run_attempt" in value and (
        not isinstance(value["run_attempt"], int) or isinstance(value["run_attempt"], bool)
        or value["run_attempt"] < 1
    ):
        raise HandoffError("corrupt_legacy_discovery_floor")
    if "run_started_at" in value:
        parse_time(value["run_started_at"])
    return dict(value)


def empty_ledger(floor: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": LEDGER_VERSION,
        "legacy_discovery_floor": validate_floor(floor),
        "admitted_observations": [],
    }


def validate_admission_row(value: Any) -> dict[str, Any]:
    required = {
        "admission_id", "producer_run_id", "run_attempt", "head_sha", "run_started_at",
        "artifact_id", "artifact_name", "artifact_expires_at", "artifact_digest_sha256",
        "artifact_size_bytes", "refresh_evidence_sha256", "observed_at", "generation_id",
        "candidate_sha256", "admitted_at",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise HandoffError("corrupt_admitted_observation")
    expected_id = admission_id(
        str(value["producer_run_id"]), value["run_attempt"], value["refresh_evidence_sha256"],
    )
    if value["admission_id"] != expected_id:
        raise HandoffError("corrupt_admitted_observation")
    if not re.fullmatch(r"[a-f0-9]{40}", str(value["head_sha"])):
        raise HandoffError("corrupt_admitted_observation")
    if not str(value["artifact_id"]).isdigit() or not str(value["producer_run_id"]).isdigit():
        raise HandoffError("corrupt_admitted_observation")
    if value["artifact_name"] != f"upstream-catalog-refresh-{value['producer_run_id']}":
        raise HandoffError("corrupt_admitted_observation")
    if not isinstance(value["run_attempt"], int) or isinstance(value["run_attempt"], bool) or value["run_attempt"] < 1:
        raise HandoffError("corrupt_admitted_observation")
    if not isinstance(value["artifact_size_bytes"], int) or isinstance(value["artifact_size_bytes"], bool) or value["artifact_size_bytes"] <= 0:
        raise HandoffError("corrupt_admitted_observation")
    for name in ("artifact_digest_sha256", "refresh_evidence_sha256", "generation_id", "candidate_sha256"):
        if not isinstance(value[name], str) or not HEX64.fullmatch(value[name]):
            raise HandoffError("corrupt_admitted_observation")
    for name in ("run_started_at", "artifact_expires_at", "observed_at", "admitted_at"):
        parse_time(value[name])
    return dict(value)


def validate_ledger(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "legacy_discovery_floor", "admitted_observations",
    } or value.get("schema_version") != LEDGER_VERSION:
        raise HandoffError("corrupt_collector_handoff_ledger")
    floor = validate_floor(value.get("legacy_discovery_floor"))
    rows = value.get("admitted_observations")
    if not isinstance(rows, list) or len(rows) > MAX_ADMITTED_OBSERVATIONS:
        raise HandoffError("collector_handoff_ledger_capacity_or_shape")
    validated = [validate_admission_row(row) for row in rows]
    ids = [row["admission_id"] for row in validated]
    if len(ids) != len(set(ids)):
        raise HandoffError("corrupt_collector_handoff_ledger_duplicate")
    attempt_ids = [(row["producer_run_id"], row["run_attempt"]) for row in validated]
    if len(attempt_ids) != len(set(attempt_ids)):
        raise HandoffError("corrupt_collector_handoff_ledger_attempt_conflict")
    orders = [admission_order_key(row) for row in validated]
    for earlier, later in zip(orders, orders[1:]):
        if later < earlier:
            raise HandoffError("corrupt_collector_handoff_ledger_order")
    return {
        "schema_version": LEDGER_VERSION,
        "legacy_discovery_floor": floor,
        "admitted_observations": validated,
    }


def ledger_from_index(index: Mapping[str, Any], *, legacy_floor: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = index.get("collector_handoff")
    if existing is None:
        return empty_ledger(legacy_floor)
    ledger = validate_ledger(existing)
    if legacy_floor is not None and ledger["legacy_discovery_floor"] != validate_floor(legacy_floor):
        raise HandoffError("legacy_discovery_floor_rewrite")
    return ledger


def derive_legacy_floor(
    state_dir, index: Mapping[str, Any], *, now: dt.datetime | None = None,
) -> dict[str, Any] | None:
    """Find the newest old observation provable by a sealed checkpoint and exact input reference.

    Historical attempts are intentionally absent from this floor: older state
    did not persist them, and this migration must not invent one.
    """
    from pathlib import Path

    generations = index.get("generations")
    if not isinstance(generations, list):
        raise HandoffError("corrupt_generation_index")
    if not generations:
        current = now or dt.datetime.now(dt.timezone.utc)
        boundary = current - dt.timedelta(days=LOOKBACK_DAYS)
        return {
            "basis": "bounded_initial_lookback",
            "lookback_started_at": boundary.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
    root = Path(state_dir) / "sources" / "data_go_kr" / "generations"
    proven: list[tuple[dt.datetime, dict[str, Any]]] = []
    for row in generations:
        if not isinstance(row, dict):
            raise HandoffError("corrupt_generation_index")
        generation_id = row.get("generation_id")
        if not isinstance(generation_id, str) or not HEX64.fullmatch(generation_id):
            raise HandoffError("corrupt_generation_index")
        checkpoint_path = root / f"{generation_id}.json"
        if not checkpoint_path.is_file():
            raise HandoffError("legacy_floor_checkpoint_missing")
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HandoffError("legacy_floor_checkpoint_invalid") from exc
        if not isinstance(checkpoint, dict) or checkpoint.get("generation_id") != generation_id:
            raise HandoffError("legacy_floor_checkpoint_identity_invalid")
        unsigned = dict(checkpoint)
        claimed = unsigned.pop("checkpoint_sha256", None)
        canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if not isinstance(claimed, str) or hashlib.sha256(canonical).hexdigest() != claimed:
            raise HandoffError("legacy_floor_checkpoint_digest_invalid")
        if checkpoint.get("source_id") != "data_go_kr":
            continue
        observation = checkpoint.get("last_observation")
        if not isinstance(observation, dict):
            continue
        producer_id = str(observation.get("producer_run_id") or "")
        evidence_sha = str(observation.get("refresh_evidence_sha256") or "")
        if not producer_id.isdigit() or not HEX64.fullmatch(evidence_sha):
            continue
        refs = checkpoint.get("input_artifacts")
        if not isinstance(refs, list):
            continue
        matching = [
            ref for ref in refs if isinstance(ref, dict)
            and str(ref.get("run_id")) == producer_id
            and ref.get("name") == f"upstream-catalog-refresh-{producer_id}"
            and ref.get("evidence_sha256") == evidence_sha
        ]
        if len(matching) != 1:
            continue
        artifact_id = str(matching[0].get("artifact_id") or "")
        if not artifact_id.isdigit():
            continue
        observed_at = observation.get("observed_at")
        observed = parse_time(str(observed_at or ""))
        floor = {
            "producer_run_id": producer_id,
            "artifact_id": artifact_id,
            "observed_at": str(observed_at),
            "generation_id": generation_id,
            "evidence_sha256": evidence_sha,
            "basis": "sealed_checkpoint_and_matching_input_artifact_reference",
        }
        proven.append((observed, floor))
    if not proven:
        return None
    return max(proven, key=lambda item: item[0])[1]


def add_admission(
    index: dict[str, Any], row: dict[str, Any], *, now: str,
    legacy_floor: dict[str, Any] | None = None,
) -> None:
    """Append one validated observation identity; never silently evict live rows."""
    ledger = ledger_from_index(index, legacy_floor=legacy_floor)
    admission = validate_admission_row(row)
    rows = ledger["admitted_observations"]
    prior = next((item for item in rows if item["admission_id"] == admission["admission_id"]), None)
    if prior is not None:
        # Admission time and generation are outcomes of processing, not part
        # of the producer's immutable observation identity.  A later event or
        # scheduled replay must preserve the first durable record verbatim.
        immutable_fields = {
            "producer_run_id", "run_attempt", "head_sha", "run_started_at",
            "artifact_id", "artifact_name", "artifact_expires_at", "artifact_digest_sha256",
            "artifact_size_bytes", "refresh_evidence_sha256", "observed_at", "candidate_sha256",
        }
        if any(prior.get(field) != admission.get(field) for field in immutable_fields):
            raise HandoffError("collector_handoff_admission_identity_conflict")
        index["collector_handoff"] = ledger
        return
    same_attempt = next((
        item for item in rows
        if item["producer_run_id"] == admission["producer_run_id"]
        and item["run_attempt"] == admission["run_attempt"]
    ), None)
    if same_attempt is not None:
        raise HandoffError("collector_handoff_attempt_identity_conflict")
    incoming_order = admission_order_key(admission)
    if rows and incoming_order < admission_order_key(rows[-1]):
        raise HandoffError("older_unadmitted_observation_requires_operator_review")
    floor = ledger["legacy_discovery_floor"]
    if floor is not None:
        floor_value = floor.get("observed_at") or floor.get("lookback_started_at")
        if floor_value is None:
            raise HandoffError("corrupt_legacy_discovery_floor")
        floor_time = parse_time(floor_value)
        incoming_observed = parse_time(admission["observed_at"])
        if incoming_observed < floor_time:
            raise HandoffError("older_unadmitted_observation_requires_operator_review")
        floor_is_exact = (
            floor.get("producer_run_id") == admission["producer_run_id"]
            and floor.get("artifact_id") == admission["artifact_id"]
            and floor.get("evidence_sha256") == admission["refresh_evidence_sha256"]
            and floor.get("generation_id") == admission["generation_id"]
        )
        if (
            floor.get("basis") != "bounded_initial_lookback"
            and incoming_observed == floor_time and not floor_is_exact
        ):
            raise HandoffError("collector_handoff_floor_tie_requires_operator_review")
    cutoff = parse_time(now) - dt.timedelta(days=LOOKBACK_DAYS)
    retained = [item for item in rows if parse_time(item["run_started_at"]) >= cutoff]
    if len(retained) >= MAX_ADMITTED_OBSERVATIONS:
        raise HandoffError("collector_handoff_ledger_capacity_exhausted")
    retained.append(admission)
    retained.sort(key=admission_order_key)
    ledger["admitted_observations"] = retained
    index["collector_handoff"] = ledger


def was_admitted(ledger_value: Any, candidate: Mapping[str, Any]) -> bool:
    ledger = validate_ledger(ledger_value)
    return any(
        row["producer_run_id"] == str(candidate.get("producer_run_id"))
        and row["run_attempt"] == candidate.get("run_attempt")
        and row["artifact_id"] == str(candidate.get("artifact_id"))
        and row["artifact_digest_sha256"] == candidate.get("artifact_digest_sha256")
        for row in ledger["admitted_observations"]
    )


def find_admitted_observation(
    ledger_value: Any, *, producer_run_id: str, evidence_sha256: str,
    generation_id: str, artifact_id: str | None = None,
) -> dict[str, Any] | None:
    ledger = validate_ledger(ledger_value)
    matches = [
        row for row in ledger["admitted_observations"]
        if row["producer_run_id"] == str(producer_run_id)
        and row["refresh_evidence_sha256"] == evidence_sha256
        and row["generation_id"] == generation_id
        and (artifact_id is None or row["artifact_id"] == str(artifact_id))
    ]
    return max(matches, key=admission_order_key) if matches else None


def choose_oldest_unseen(
    candidates: Sequence[Mapping[str, Any]], ledger_value: Any, *,
    legacy_floor: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Choose by authenticated attempt start; source time is checked after one download."""
    ledger = validate_ledger(ledger_value)
    floor = validate_floor(legacy_floor) if legacy_floor is not None else ledger["legacy_discovery_floor"]
    floor_started = None
    if floor is not None:
        floor_started = parse_time(
            floor.get("run_started_at") or floor.get("lookback_started_at") or floor["observed_at"]
        )
    ordered = sorted(
        (dict(item) for item in candidates),
        key=lambda item: (parse_time(str(item["run_started_at"])), int(str(item["producer_run_id"]))),
    )
    for candidate in ordered:
        if was_admitted(ledger, candidate):
            continue
        if floor is not None and candidate.get("producer_run_id") == floor.get("producer_run_id"):
            same_historical_artifact = (
                floor.get("artifact_id") is not None
                and str(candidate.get("artifact_id")) == str(floor["artifact_id"])
                and (
                    floor.get("run_attempt") is None
                    or candidate.get("run_attempt") == floor.get("run_attempt")
                )
            )
            if same_historical_artifact:
                continue
        if floor_started is not None and parse_time(str(candidate["run_started_at"])) <= floor_started:
            continue
        return candidate
    return None


def load_index(path) -> dict[str, Any]:
    from pathlib import Path

    index_path = Path(path)
    if not index_path.exists():
        return {
            "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
            "generations": [], "detail_queue_cursor": 0, "detail_retry_state": {},
        }
    try:
        value = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffError("corrupt_generation_index") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "datapan.upstream-catalogue-checkpoint.v1"
        or not isinstance(value.get("generations"), list)
    ):
        raise HandoffError("corrupt_generation_index")
    return value


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--now")
    parser.add_argument("--inspect-run-id")
    parser.add_argument("--expected-head-sha")
    parser.add_argument("--expected-run-attempt", type=int)
    parser.add_argument("--expected-artifact-id")
    parser.add_argument("--expected-artifact-digest")
    parser.add_argument("--expected-artifact-size", type=int)
    parser.add_argument("--prior-api-requests", type=int, default=0)
    parser.add_argument("--reserve-api-requests", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        now = parse_time(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
        if args.inspect_run_id:
            candidate = inspect_producer_run(
                gh_api_json, repository=args.repository, default_branch=args.default_branch,
                run_id=args.inspect_run_id, expected_head_sha=args.expected_head_sha,
                expected_run_attempt=args.expected_run_attempt, now=now,
                prior_api_requests=args.prior_api_requests,
                reserve_api_requests=args.reserve_api_requests,
            )
            if args.expected_artifact_id and candidate["artifact_id"] != args.expected_artifact_id:
                raise HandoffError("handoff_selected_artifact_id_changed")
            if args.expected_artifact_digest and candidate["artifact_digest_sha256"] != args.expected_artifact_digest:
                raise HandoffError("handoff_selected_artifact_digest_changed")
            if args.expected_artifact_size is not None and candidate["artifact_size_bytes"] != args.expected_artifact_size:
                raise HandoffError("handoff_selected_artifact_size_changed")
            print(f"api_requests={candidate.pop('api_requests')}")
            for key, value in candidate.items():
                print(f"{key}={value}")
        else:
            index_path = pathlib.Path(args.state_dir) / "sources" / "data_go_kr" / "index.json"
            index = load_index(index_path)
            existing = index.get("collector_handoff")
            if existing is not None:
                ledger = validate_ledger(existing)
                floor = ledger["legacy_discovery_floor"]
            else:
                floor = derive_legacy_floor(args.state_dir, index, now=now)
                if index["generations"] and floor is None:
                    raise HandoffError("legacy_discovery_floor_unverifiable")
                ledger = empty_ledger(floor)
            candidate, diagnostics, api_requests = discover_oldest_unseen(
                gh_api_json, repository=args.repository, default_branch=args.default_branch,
                now=now, ledger_value=ledger, legacy_floor=floor,
            )
            print(f"api_requests={api_requests}")
            print(f"available={'true' if candidate else 'false'}")
            if candidate:
                for key in (
                    "producer_run_id", "run_attempt", "head_sha", "run_started_at",
                    "artifact_id", "artifact_name", "artifact_expires_at",
                    "artifact_digest_sha256", "artifact_size_bytes",
                ):
                    print(f"{key}={candidate[key]}")
            else:
                print("reason=no_unseen_trusted_observation")
            if diagnostics:
                print("diagnostics=" + ",".join(diagnostics))
        return 0
    except (HandoffError, OSError, ValueError) as exc:
        print(f"FAIL collector handoff discovery: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
