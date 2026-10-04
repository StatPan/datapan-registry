#!/usr/bin/env python3
"""Acknowledge an existing verified Hugging Face publication from its receipt.

This entrypoint never publishes. Event-triggered acknowledgement is pinned to
its publisher run; the hourly schedule searches bounded successful native
publisher runs and reconciles at most one previously-unacknowledged row.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Any


PUBLISHER_WORKFLOW_PATH = ".github/workflows/huggingface-distribution.yml"
PROMOTION_WORKFLOW_PATH = ".github/workflows/canonical-update-promotion.yml"
ACK_WORKFLOW_PATH = ".github/workflows/canonical-update-publication-ack.yml"
RECEIPT_ARTIFACT_NAME = "huggingface-registry-publication-receipts"
RECEIPT_MEMBER = "hf-publication-receipt.json"
SOURCE_BINDING_MEMBER = "hf-source-binding.json"
PUBLISH_STEP = "Publish two-phase immutable distribution"
VERIFY_STEP = "Verify published pointer anonymously"
PUBLISHER_JOB = "validate"
RECOVERY_REFERENCE_PREFIX = "publication-recovery/v1 "
MAX_RECOVERY_REFERENCE_BYTES = 4096
DISCOVERY_DAYS = 30
MAX_PUBLISHER_RUNS = 20
MAX_PR_READBACKS = 20
MAX_GIT_CAS_TRANSACTIONS = 2
MAX_JOBS_PER_RUN = 100
MAX_ARTIFACTS_PER_RUN = 100
MAX_RECEIPT_DOWNLOADS = 20
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_EXTRACTED_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_COMMIT_ASSOCIATION_READS = MAX_PUBLISHER_RUNS
MAX_PR_BODY_BYTES = 128 * 1024
MAX_GITHUB_REQUESTS = 256
MAX_RETRIES = 2
REQUEST_TIMEOUT_SECONDS = 30
CANONICAL_AUTOMATION_BRANCH_PREFIX = "automation/canonical-update/"
CANONICAL_OWNER_MARKER_PREFIX = "datapan-canonical-update:v1:"
SHA1 = re.compile(r"^[a-f0-9]{40}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow only HTTPS redirects and never carry GitHub credentials to them."""

    max_redirections = 4
    max_repeats = 2

    def __init__(self, on_redirect: Callable[[], None]) -> None:
        super().__init__()
        self.on_redirect = on_redirect

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or not target.hostname:
            return None
        self.on_redirect()
        # Artifact download URLs are signed in their URL. A minimal request
        # prevents Authorization and GitHub API headers crossing hosts.
        return urllib.request.Request(
            newurl,
            headers={"Accept": "application/octet-stream"},
            method="GET",
        )


class RecoveryError(RuntimeError):
    """A bounded, fail-closed recovery admission or API error."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError("duplicate_json_key")
        result[key] = value
    return result


def parse_json(raw: bytes, *, label: str, maximum: int = MAX_JSON_BYTES) -> Any:
    if len(raw) > maximum:
        raise RecoveryError(f"{label}_too_large")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"{label}_invalid_json") from exc


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def exact_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RecoveryError(f"{label}_invalid")
    return value


def exact_sha(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value) or value == "0" * len(value):
        raise RecoveryError(f"{label}_invalid")
    return value


def parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise RecoveryError(f"{label}_missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryError(f"{label}_invalid") from exc
    if parsed.tzinfo is None:
        raise RecoveryError(f"{label}_timezone_missing")
    return parsed.astimezone(dt.timezone.utc)


class GitHubApi:
    """Small bounded REST client with a replaceable test transport."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        transport: Callable[[urllib.request.Request, int, int], HttpResponse] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        request_limit: int = MAX_GITHUB_REQUESTS,
    ) -> None:
        if not token:
            raise RecoveryError("github_token_unavailable")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.transport = transport or self._transport
        self.sleep = sleep
        self.request_limit = request_limit
        self.request_count = 0

    def _count_redirect(self) -> None:
        if self.request_count >= self.request_limit:
            raise RecoveryError("github_request_limit_exceeded")
        self.request_count += 1

    def _transport(self, request: urllib.request.Request, timeout: int, maximum: int) -> HttpResponse:
        try:
            opener = urllib.request.build_opener(SafeRedirectHandler(self._count_redirect))
            with opener.open(request, timeout=timeout) as response:
                body = response.read(maximum + 1)
                return HttpResponse(int(response.status), dict(response.headers.items()), body)
        except urllib.error.HTTPError as exc:
            return HttpResponse(int(exc.code), dict(exc.headers.items()), exc.read(maximum + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RecoveryError("github_api_transport_failed") from exc

    def request(self, endpoint: str, *, maximum: int = MAX_JSON_BYTES) -> HttpResponse:
        if not endpoint.startswith("repos/") or "\n" in endpoint:
            raise RecoveryError("github_api_endpoint_invalid")
        request = urllib.request.Request(
            f"{self.base_url}/{endpoint}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        )
        for retry in range(MAX_RETRIES + 1):
            if self.request_count >= self.request_limit:
                raise RecoveryError("github_request_limit_exceeded")
            self.request_count += 1
            try:
                response = self.transport(request, REQUEST_TIMEOUT_SECONDS, maximum)
            except RecoveryError:
                if retry >= MAX_RETRIES:
                    raise
                self.sleep(min(0.25 * (2**retry), 2.0))
                continue
            if response.status == 200:
                if len(response.body) > maximum:
                    raise RecoveryError("github_api_response_too_large")
                return response
            if response.status not in {429, 500, 502, 503, 504} or retry >= MAX_RETRIES:
                raise RecoveryError(f"github_api_http_{response.status}")
            retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
            try:
                delay = float(retry_after) if retry_after is not None else 0.25 * (2**retry)
            except (TypeError, ValueError):
                delay = 0.25 * (2**retry)
            self.sleep(min(max(delay, 0.0), 5.0))
        raise RecoveryError("github_api_retry_exhausted")

    def json(self, endpoint: str) -> tuple[dict[str, Any], Mapping[str, str]]:
        response = self.request(endpoint)
        value = parse_json(response.body, label="github_api")
        if not isinstance(value, dict):
            raise RecoveryError("github_api_object_required")
        return value, response.headers


def has_next_page(headers: Mapping[str, str]) -> bool:
    link = headers.get("Link") or headers.get("link") or ""
    return bool(re.search(r'<[^>]+>\s*;\s*rel="next"', link))


def exact_run_identity(run: Mapping[str, Any]) -> tuple[int, int]:
    return (
        exact_positive_int(run.get("id"), "publisher_run_id"),
        exact_positive_int(run.get("run_attempt"), "publisher_attempt"),
    )


def workflow_id_for(api: GitHubApi, repository: str, path: str) -> int:
    filename = urllib.parse.quote(pathlib.PurePosixPath(path).name, safe="")
    payload, _ = api.json(f"repos/{repository}/actions/workflows/{filename}")
    workflow_id = exact_positive_int(payload.get("id"), "workflow_id")
    if payload.get("path") != path or payload.get("state") != "active":
        raise RecoveryError("workflow_identity_mismatch")
    return workflow_id


def validate_workflow_run(
    run: Mapping[str, Any],
    *,
    repository: str,
    repository_id: int,
    workflow_path: str,
    workflow_id: int,
    default_branch: str,
    required_event: str,
) -> tuple[int, int, str]:
    run_id, attempt = exact_run_identity(run)
    repository_info = run.get("repository")
    head_repository = run.get("head_repository")
    head_sha = exact_sha(run.get("head_sha"), SHA1, "publisher_head_sha")
    if (
        run.get("workflow_id") != workflow_id
        or run.get("path") != workflow_path
        or run.get("event") != required_event
        or run.get("head_branch") != default_branch
        or not isinstance(repository_info, Mapping)
        or str(repository_info.get("full_name", "")).casefold() != repository.casefold()
        or not isinstance(head_repository, Mapping)
        or str(head_repository.get("full_name", "")).casefold() != repository.casefold()
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
    ):
        raise RecoveryError("publisher_run_not_trusted")
    if (
        isinstance(repository_info.get("id"), bool)
        or repository_info.get("id") != repository_id
        or isinstance(head_repository.get("id"), bool)
        or head_repository.get("id") != repository_id
    ):
        raise RecoveryError("publisher_repository_id_mismatch")
    return run_id, attempt, head_sha


def validate_job_and_steps(
    jobs_payload: Mapping[str, Any],
    *,
    run_id: int,
    attempt: int,
    head_sha: str,
) -> tuple[str, dt.datetime, dt.datetime]:
    jobs = jobs_payload.get("jobs")
    total = jobs_payload.get("total_count")
    if (
        not isinstance(jobs, list)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total != len(jobs)
        or total > MAX_JOBS_PER_RUN
    ):
        raise RecoveryError("publisher_jobs_incomplete_or_over_limit")
    selected = [job for job in jobs if isinstance(job, Mapping) and job.get("name") == PUBLISHER_JOB]
    if len(selected) != 1:
        raise RecoveryError("publisher_validate_job_ambiguous")
    job = selected[0]
    if (
        job.get("run_id") != run_id
        or job.get("run_attempt") != attempt
        or job.get("head_sha") != head_sha
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
    ):
        raise RecoveryError("publisher_validate_job_untrusted")
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise RecoveryError("publisher_steps_missing")
    publishing = [step for step in steps if isinstance(step, Mapping) and step.get("name") == PUBLISH_STEP]
    verifying = [step for step in steps if isinstance(step, Mapping) and step.get("name") == VERIFY_STEP]
    if len(publishing) != 1 or len(verifying) != 1:
        raise RecoveryError("publisher_publish_steps_ambiguous")
    publish_step, verify_step = publishing[0], verifying[0]
    started_at = parse_time(job.get("started_at"), "publisher_job_started_at")
    completed_at = parse_time(job.get("completed_at"), "publisher_job_completed_at")
    if completed_at < started_at:
        raise RecoveryError("publisher_job_interval_invalid")
    if (
        publish_step.get("status") == "completed"
        and publish_step.get("conclusion") == "skipped"
        and verify_step.get("status") == "completed"
        and verify_step.get("conclusion") == "skipped"
    ):
        return "non_publishing_validation", started_at, completed_at
    if (
        publish_step.get("status") != "completed"
        or publish_step.get("conclusion") != "success"
        or verify_step.get("status") != "completed"
        or verify_step.get("conclusion") != "success"
    ):
        raise RecoveryError("publisher_publish_or_verify_step_untrusted")
    return "publishing", started_at, completed_at


def extract_receipt_archive(raw: bytes) -> tuple[bytes, bytes]:
    if not raw or len(raw) > MAX_ARCHIVE_BYTES:
        raise RecoveryError("publication_archive_size_invalid")
    try:
        archive = zipfile.ZipFile(BytesIO(raw))
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or set(names) != {RECEIPT_MEMBER, SOURCE_BINDING_MEMBER}:
            raise RecoveryError("publication_archive_members_invalid")
        if sum(info.file_size for info in infos) > MAX_EXTRACTED_BYTES:
            raise RecoveryError("publication_archive_expanded_size_exceeded")
        for info in infos:
            mode = info.external_attr >> 16
            file_type = mode & 0o170000
            relative = pathlib.PurePosixPath(info.filename)
            if (
                info.is_dir()
                or relative.is_absolute()
                or ".." in relative.parts
                or "\\" in info.filename
                or (file_type and file_type != 0o100000)
                or info.file_size > MAX_JSON_BYTES
            ):
                raise RecoveryError("publication_archive_member_unsafe")
        if archive.testzip() is not None:
            raise RecoveryError("publication_archive_crc_invalid")
        receipt = archive.read(RECEIPT_MEMBER)
        source_binding = archive.read(SOURCE_BINDING_MEMBER)
    except zipfile.BadZipFile as exc:
        raise RecoveryError("publication_archive_invalid") from exc
    parse_json(receipt, label="publication_receipt")
    parse_json(source_binding, label="publication_source_binding")
    return receipt, source_binding


def verify_receipt(
    receipt_raw: bytes,
    source_binding_raw: bytes,
    *,
    repository: str,
    workflow_head_sha: str,
    root: pathlib.Path,
) -> dict[str, Any]:
    receipt = parse_json(receipt_raw, label="publication_receipt")
    source_binding_file = parse_json(source_binding_raw, label="publication_source_binding")
    if not isinstance(receipt, dict) or not isinstance(source_binding_file, dict):
        raise RecoveryError("publication_receipt_object_required")
    source = receipt.get("source_binding")
    publication = receipt.get("publication")
    verification = receipt.get("anonymous_verification")
    if (
        receipt.get("schema_version") != "datapan.registry-publication-receipt.v1"
        or receipt.get("status") != "verified"
        or not isinstance(source, dict)
        or source != source_binding_file
        or source.get("schema_version") != "datapan.registry-publication-source-binding.v1"
        or source.get("status") != "bound"
        or source.get("repository") != repository
        or source.get("event_name") != "workflow_dispatch"
        or source.get("git_ref") != "refs/heads/main"
        or source.get("workflow_sha") != workflow_head_sha
        or not isinstance(publication, dict)
        or publication.get("status") != "published"
        or publication.get("dataset") != repository
        or not isinstance(verification, dict)
        or verification.get("status") != "verified"
        or verification.get("dataset") != repository
    ):
        raise RecoveryError("publication_receipt_binding_mismatch")
    source_sha = exact_sha(source.get("source_sha"), SHA1, "publication_source_sha")
    manifest_sha = exact_sha(source.get("manifest_sha256"), SHA256, "publication_manifest_sha256")
    payload_revision = exact_sha(publication.get("payload_revision"), SHA1, "publication_payload_revision")
    pointer_revision = exact_sha(publication.get("pointer_revision"), SHA1, "publication_pointer_revision")
    if verification.get("revision") != payload_revision:
        raise RecoveryError("publication_anonymous_revision_mismatch")
    try:
        source_manifest = subprocess.run(
            ("git", "show", f"{source_sha}:manifest.json"),
            cwd=root, capture_output=True, check=False, timeout=30,
        )
        workflow_ancestor = subprocess.run(
            ("git", "merge-base", "--is-ancestor", source_sha, workflow_head_sha),
            cwd=root, capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError("publication_source_history_unavailable") from exc
    if source_manifest.returncode != 0 or hashlib.sha256(source_manifest.stdout).hexdigest() != manifest_sha:
        raise RecoveryError("publication_source_manifest_mismatch")
    if workflow_ancestor.returncode != 0:
        raise RecoveryError("publication_source_not_ancestor_of_publisher")
    source_tree = source.get("source_tree_sha")
    if source_tree is not None:
        exact_sha(source_tree, SHA1, "publication_source_tree_sha")
        tree = subprocess.run(
            ("git", "rev-parse", f"{source_sha}^{{tree}}"),
            cwd=root, capture_output=True, text=True, check=False, timeout=30,
        )
        if tree.returncode != 0 or tree.stdout.strip() != source_tree:
            raise RecoveryError("publication_source_tree_mismatch")
    return {
        "source_sha": source_sha,
        "manifest_sha256": manifest_sha,
        "payload_revision": payload_revision,
        "pointer_revision": pointer_revision,
    }


def make_recovery_reference(
    *,
    repository: str,
    workflow_id: int,
    run_id: int,
    attempt: int,
    head_sha: str,
    artifact_id: int,
    receipt_sha256: str,
    publication: Mapping[str, Any],
) -> str:
    value = {
        "artifact_id": artifact_id,
        "manifest_sha256": publication["manifest_sha256"],
        "payload_revision": publication["payload_revision"],
        "pointer_revision": publication["pointer_revision"],
        "publisher_head_sha": head_sha,
        "publisher_run_attempt": attempt,
        "publisher_run_id": run_id,
        "publisher_workflow_id": workflow_id,
        "publisher_workflow_path": PUBLISHER_WORKFLOW_PATH,
        "receipt_sha256": receipt_sha256,
        "repository": repository,
        "source_sha": publication["source_sha"],
    }
    reference = RECOVERY_REFERENCE_PREFIX + canonical_json(value).decode("utf-8")
    if len(reference.encode("utf-8")) > MAX_RECOVERY_REFERENCE_BYTES:
        raise RecoveryError("publication_recovery_reference_too_large")
    return reference


def parse_recovery_reference(reference: Any) -> dict[str, Any] | None:
    if not isinstance(reference, str) or not reference.startswith(RECOVERY_REFERENCE_PREFIX):
        return None
    if len(reference.encode("utf-8")) > MAX_RECOVERY_REFERENCE_BYTES:
        raise RecoveryError("publication_recovery_reference_too_large")
    body = reference[len(RECOVERY_REFERENCE_PREFIX):]
    value = parse_json(body.encode("utf-8"), label="publication_recovery_reference", maximum=MAX_RECOVERY_REFERENCE_BYTES)
    if not isinstance(value, dict):
        raise RecoveryError("publication_recovery_reference_invalid")
    expected = {
        "artifact_id", "manifest_sha256", "payload_revision", "pointer_revision",
        "publisher_head_sha", "publisher_run_attempt", "publisher_run_id",
        "publisher_workflow_id", "publisher_workflow_path", "receipt_sha256",
        "repository", "source_sha",
    }
    if set(value) != expected or canonical_json(value).decode("utf-8") != body:
        raise RecoveryError("publication_recovery_reference_fields_invalid")
    exact_positive_int(value.get("artifact_id"), "publication_artifact_id")
    exact_positive_int(value.get("publisher_run_id"), "publisher_run_id")
    exact_positive_int(value.get("publisher_run_attempt"), "publisher_attempt")
    exact_positive_int(value.get("publisher_workflow_id"), "publisher_workflow_id")
    exact_sha(value.get("publisher_head_sha"), SHA1, "publisher_head_sha")
    exact_sha(value.get("source_sha"), SHA1, "publication_source_sha")
    exact_sha(value.get("manifest_sha256"), SHA256, "publication_manifest_sha256")
    exact_sha(value.get("receipt_sha256"), SHA256, "publication_receipt_sha256")
    exact_sha(value.get("payload_revision"), SHA1, "publication_payload_revision")
    exact_sha(value.get("pointer_revision"), SHA1, "publication_pointer_revision")
    if value.get("publisher_workflow_path") != PUBLISHER_WORKFLOW_PATH or not isinstance(value.get("repository"), str):
        raise RecoveryError("publication_recovery_reference_identity_invalid")
    return value


class PublicationAckRecovery:
    def __init__(
        self,
        root: pathlib.Path,
        repository: str,
        api: GitHubApi,
        runner: Any,
        *,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
    ) -> None:
        self.root = root.resolve()
        self.repository = repository
        self.api = api
        self.runner = runner
        self.now = now
        self.repository_id: int | None = None
        self.default_branch: str | None = None
        self.controller_sha: str | None = None
        self.publisher_workflow_id: int | None = None
        self.promotion_workflow_id: int | None = None
        self.ack_workflow_id: int | None = None
        self._pr_readbacks: dict[int, dict[str, Any]] = {}
        self.summary: dict[str, Any] = {
            "schema_version": "datapan.canonical-publication-ack-recovery.v1",
            "outcome": "not_started",
            "mode": None,
            "repository": repository,
            "controller_sha": None,
            "publisher_runs_considered": 0,
            "receipt_downloads": 0,
            "github_requests": 0,
            "pull_request_readbacks": 0,
            "commit_association_reads": 0,
            "git_cas_transactions": 0,
            "already_acknowledged": 0,
            "non_publishing_validations": 0,
            "outside_c_publications": [],
            "unresolved_candidates": [],
            "selected_publisher": None,
        }

    def _api_json(self, endpoint: str) -> tuple[dict[str, Any], Mapping[str, str]]:
        return self.api.json(f"repos/{self.repository}/{endpoint}")

    def initialize(self) -> None:
        payload, _ = self.api.json(f"repos/{self.repository}")
        self.repository_id = exact_positive_int(payload.get("id"), "repository_id")
        branch = payload.get("default_branch")
        if not isinstance(branch, str) or branch != "main":
            raise RecoveryError("default_branch_not_main")
        self.default_branch = branch
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=self.root, capture_output=True,
            text=True, check=False, timeout=30,
        )
        if head.returncode != 0:
            raise RecoveryError("controller_head_unavailable")
        self.controller_sha = exact_sha(head.stdout.strip(), SHA1, "controller_head_sha")
        self.summary["controller_sha"] = self.controller_sha
        self.publisher_workflow_id = workflow_id_for(self.api, self.repository, PUBLISHER_WORKFLOW_PATH)
        self.promotion_workflow_id = workflow_id_for(self.api, self.repository, PROMOTION_WORKFLOW_PATH)
        self.ack_workflow_id = workflow_id_for(self.api, self.repository, ACK_WORKFLOW_PATH)
        self.verify_current_main()

    def verify_current_main(self) -> None:
        if not self.default_branch or not self.controller_sha:
            raise RecoveryError("recovery_not_initialized")
        branch = urllib.parse.quote(self.default_branch, safe="")
        payload, _ = self._api_json(f"git/ref/heads/{branch}")
        obj = payload.get("object")
        sha = exact_sha(obj.get("sha") if isinstance(obj, Mapping) else None, SHA1, "remote_main_sha")
        if sha != self.controller_sha:
            raise RecoveryError("current_main_moved")

    def _workflow_runs(self, workflow_id: int, *, status: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"branch": self.default_branch, "status": status, "per_page": 100})
        payload, headers = self._api_json(f"actions/workflows/{workflow_id}/runs?{query}")
        rows = payload.get("workflow_runs")
        total = payload.get("total_count")
        if (
            not isinstance(rows, list)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total != len(rows)
            or total > 100
            or has_next_page(headers)
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise RecoveryError("active_run_list_incomplete")
        return rows

    def assert_idle(
        self,
        current_ack_run_id: int,
        current_ack_attempt: int,
        *,
        include_acknowledgements: bool = True,
    ) -> None:
        self.verify_current_main()
        if not self.publisher_workflow_id or not self.promotion_workflow_id or not self.ack_workflow_id:
            raise RecoveryError("workflow_identity_missing")
        active: list[tuple[str, dict[str, Any]]] = []
        workflows = [
            ("publisher", self.publisher_workflow_id),
            ("promotion", self.promotion_workflow_id),
        ]
        if include_acknowledgements:
            workflows.append(("acknowledgement", self.ack_workflow_id))
        for label, workflow_id in workflows:
            # A queued acknowledgement is serialized behind this run by the
            # workflow concurrency group; treating it as a blocker would
            # deadlock the queue. Publisher/C work may not be overtaken.
            statuses = ("in_progress",) if label == "acknowledgement" else (
                "in_progress", "queued", "waiting", "requested", "pending",
            )
            for status in statuses:
                active.extend((label, row) for row in self._workflow_runs(workflow_id, status=status))
        for label, run in active:
            run_id, attempt = exact_run_identity(run)
            if label == "acknowledgement" and (run_id, attempt) == (current_ack_run_id, current_ack_attempt):
                continue
            if run.get("status") in {"in_progress", "queued", "waiting", "requested", "pending"}:
                raise RecoveryError(f"active_{label}_run_present")

    def discover(self) -> list[dict[str, Any]]:
        if not self.publisher_workflow_id or not self.default_branch:
            raise RecoveryError("workflow_identity_missing")
        cutoff = self.now().astimezone(dt.timezone.utc) - dt.timedelta(days=DISCOVERY_DAYS)
        query = urllib.parse.urlencode({
            "branch": self.default_branch,
            "event": "workflow_dispatch",
            "status": "completed",
            "created": f">={cutoff.date().isoformat()}",
            "per_page": 100,
        })
        payload, headers = self._api_json(f"actions/workflows/{self.publisher_workflow_id}/runs?{query}")
        rows = payload.get("workflow_runs")
        total = payload.get("total_count")
        if (
            not isinstance(rows, list)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total != len(rows)
            or total > 100
            or has_next_page(headers)
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise RecoveryError("discovery_limit_exceeded")
        successful: list[dict[str, Any]] = []
        for row in rows:
            if row.get("status") != "completed" or row.get("conclusion") != "success":
                continue
            if parse_time(row.get("created_at"), "publisher_run_created_at") >= cutoff:
                successful.append(row)
        if len(successful) > MAX_PUBLISHER_RUNS:
            raise RecoveryError("discovery_limit_exceeded")
        def ordering(row: Mapping[str, Any]) -> tuple[dt.datetime, int]:
            return (
                parse_time(row.get("created_at"), "publisher_run_created_at"),
                exact_positive_int(row.get("id"), "publisher_run_id"),
            )

        successful.sort(key=ordering)
        return successful

    def publisher_runs(self, mode: str, event: Mapping[str, Any] | None) -> list[tuple[dict[str, Any], int | None]]:
        if mode == "schedule":
            return [(row, None) for row in self.discover()]
        if mode != "workflow_run" or not isinstance(event, Mapping):
            raise RecoveryError("workflow_run_event_missing")
        publisher = event.get("workflow_run")
        if not isinstance(publisher, Mapping):
            raise RecoveryError("workflow_run_event_missing")
        run_id = exact_positive_int(publisher.get("id"), "publisher_run_id")
        attempt = exact_positive_int(publisher.get("run_attempt"), "publisher_attempt")
        return [(dict(publisher), attempt)]

    def _api_payload(self, endpoint: str) -> dict[str, Any]:
        payload, _ = self._api_json(endpoint)
        return payload

    def load_journal_snapshot(self) -> tuple[Mapping[str, Any], str]:
        branch = str(self.runner.STATE_BRANCH)
        ref_path = urllib.parse.quote(branch, safe="/")
        ref, _ = self._api_json(f"git/ref/heads/{ref_path}")
        obj = ref.get("object")
        state_sha = exact_sha(obj.get("sha") if isinstance(obj, Mapping) else None, SHA1, "promotion_state_sha")
        journal_path = pathlib.PurePosixPath(self.runner.JOURNAL_PATH.as_posix())
        path = urllib.parse.quote(journal_path.as_posix(), safe="/")
        content, _ = self._api_json(f"contents/{path}?ref={state_sha}")
        encoded = content.get("content")
        if content.get("encoding") != "base64" or not isinstance(encoded, str):
            raise RecoveryError("promotion_journal_snapshot_encoding_invalid")
        try:
            raw = base64.b64decode("".join(encoded.split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RecoveryError("promotion_journal_snapshot_base64_invalid") from exc
        if len(raw) > 4 * 1024 * 1024 or content.get("size") != len(raw):
            raise RecoveryError("promotion_journal_snapshot_size_invalid")
        journal = parse_json(raw, label="promotion_journal")
        if not isinstance(journal, Mapping):
            raise RecoveryError("promotion_journal_snapshot_object_required")
        promotion = self.runner.load_module(
            self.root / "scripts/canonical_update_pr.py", "canonical_publication_recovery_journal_reader",
        )
        promotion.validate_journal(
            journal,
            self.runner.load_object(self.root / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json"),
        )
        return journal, state_sha

    def readback_pr(self, number: int) -> dict[str, Any]:
        number = exact_positive_int(number, "pull_request_number")
        cached = self._pr_readbacks.get(number)
        if cached is not None:
            return dict(cached)
        if self.summary["pull_request_readbacks"] >= MAX_PR_READBACKS:
            raise RecoveryError("pull_request_readback_limit_exceeded")
        self.summary["pull_request_readbacks"] += 1
        payload, _ = self._api_json(f"pulls/{number}")
        base = payload.get("base")
        head = payload.get("head")
        if not isinstance(base, Mapping) or not isinstance(head, Mapping):
            raise RecoveryError("canonical_pr_readback_invalid")
        base_repo = base.get("repo")
        head_repo = head.get("repo")
        if (
            not isinstance(base_repo, Mapping)
            or not isinstance(head_repo, Mapping)
            or str(base_repo.get("full_name", "")).casefold() != self.repository.casefold()
            or str(head_repo.get("full_name", "")).casefold() != self.repository.casefold()
        ):
            raise RecoveryError("canonical_pr_repository_mismatch")
        merged = payload.get("merged") is True
        raw_state = payload.get("state")
        state = "MERGED" if merged else (str(raw_state).upper() if raw_state in {"open", "closed"} else None)
        if state is None:
            raise RecoveryError("canonical_pr_state_invalid")
        merge_sha = payload.get("merge_commit_sha")
        result = {
            "number": payload.get("number"),
            "url": payload.get("html_url"),
            "state": state,
            "body": payload.get("body"),
            "headRefName": head.get("ref"),
            "headRefOid": head.get("sha"),
            "baseRefName": base.get("ref"),
            "baseRefOid": base.get("sha"),
            "mergeCommit": {"oid": merge_sha} if isinstance(merge_sha, str) else None,
            "repository": base_repo.get("full_name"),
            "headRepository": head_repo.get("full_name"),
        }
        self._pr_readbacks[number] = result
        return dict(result)

    def _list_jobs(self, run_id: int, attempt: int) -> dict[str, Any]:
        payload, headers = self._api_json(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
        rows = payload.get("jobs")
        total = payload.get("total_count")
        if (
            not isinstance(rows, list)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total != len(rows)
            or total > MAX_JOBS_PER_RUN
            or has_next_page(headers)
        ):
            raise RecoveryError("publisher_jobs_incomplete_or_over_limit")
        return payload

    def _list_artifacts(self, run_id: int) -> list[dict[str, Any]]:
        payload, headers = self._api_json(f"actions/runs/{run_id}/artifacts?per_page=100")
        rows = payload.get("artifacts")
        total = payload.get("total_count")
        if (
            not isinstance(rows, list)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total != len(rows)
            or total > MAX_ARTIFACTS_PER_RUN
            or has_next_page(headers)
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise RecoveryError("publisher_artifacts_incomplete_or_over_limit")
        return rows

    def authenticate_publisher(
        self,
        initial: Mapping[str, Any],
        *,
        expected_attempt: int | None,
    ) -> dict[str, Any] | None:
        if not self.repository_id or not self.default_branch or not self.publisher_workflow_id:
            raise RecoveryError("recovery_not_initialized")
        event = initial.get("event")
        if not isinstance(event, str) or not event:
            raise RecoveryError("publisher_event_missing")
        if event != "workflow_dispatch":
            return None
        if initial.get("status") != "completed" or initial.get("conclusion") != "success":
            raise RecoveryError("publisher_dispatch_not_successful")
        run_id, attempt, head_sha = validate_workflow_run(
            initial, repository=self.repository, repository_id=self.repository_id,
            workflow_path=PUBLISHER_WORKFLOW_PATH, workflow_id=self.publisher_workflow_id,
            default_branch=self.default_branch, required_event="workflow_dispatch",
        )
        if expected_attempt is not None and attempt != expected_attempt:
            raise RecoveryError("publisher_event_attempt_mismatch")
        exact = self._api_payload(f"actions/runs/{run_id}/attempts/{attempt}")
        exact_identity = validate_workflow_run(
            exact, repository=self.repository, repository_id=self.repository_id,
            workflow_path=PUBLISHER_WORKFLOW_PATH, workflow_id=self.publisher_workflow_id,
            default_branch=self.default_branch, required_event="workflow_dispatch",
        )
        if exact_identity != (run_id, attempt, head_sha):
            raise RecoveryError("publisher_attempt_identity_mismatch")
        jobs = self._list_jobs(run_id, attempt)
        disposition, job_start, job_end = validate_job_and_steps(jobs, run_id=run_id, attempt=attempt, head_sha=head_sha)
        if disposition == "non_publishing_validation":
            self.summary["non_publishing_validations"] += 1
            return None
        artifacts = self._list_artifacts(run_id)
        attempt_start = parse_time(exact.get("run_started_at") or exact.get("created_at"), "publisher_attempt_started_at")
        candidates: list[dict[str, Any]] = []
        for artifact in artifacts:
            if artifact.get("name") != RECEIPT_ARTIFACT_NAME or artifact.get("expired") is not False:
                continue
            artifact_run = artifact.get("workflow_run")
            if not isinstance(artifact_run, Mapping):
                continue
            artifact_id = exact_positive_int(artifact.get("id"), "publication_artifact_id")
            size = exact_positive_int(artifact.get("size_in_bytes"), "publication_artifact_size")
            created_at = parse_time(artifact.get("created_at"), "publication_artifact_created_at")
            expires_at = parse_time(artifact.get("expires_at"), "publication_artifact_expires_at")
            if (
                size > MAX_ARCHIVE_BYTES
                or expires_at <= self.now().astimezone(dt.timezone.utc)
                or created_at < max(attempt_start, job_start)
                or created_at > job_end
                or artifact_run.get("id") != run_id
                or artifact_run.get("repository_id") != self.repository_id
                or artifact_run.get("head_repository_id") != self.repository_id
                or artifact_run.get("head_branch") != self.default_branch
                or artifact_run.get("head_sha") != head_sha
            ):
                continue
            candidates.append(artifact)
        if len(candidates) != 1:
            raise RecoveryError("publication_receipt_artifact_missing_or_ambiguous")
        if self.summary["receipt_downloads"] >= MAX_RECEIPT_DOWNLOADS:
            raise RecoveryError("receipt_download_limit_exceeded")
        artifact = candidates[0]
        artifact_id = exact_positive_int(artifact.get("id"), "publication_artifact_id")
        self.summary["receipt_downloads"] += 1
        downloaded = self.api.request(
            f"repos/{self.repository}/actions/artifacts/{artifact_id}/zip",
            maximum=MAX_ARCHIVE_BYTES,
        )
        raw_archive = downloaded.body
        if not raw_archive or len(raw_archive) > MAX_ARCHIVE_BYTES:
            raise RecoveryError("publication_archive_size_invalid")
        if len(raw_archive) != int(artifact["size_in_bytes"]):
            raise RecoveryError("publication_archive_api_size_mismatch")
        declared_length = downloaded.headers.get("Content-Length") or downloaded.headers.get("content-length")
        if declared_length is not None and (not declared_length.isdigit() or int(declared_length) != len(raw_archive)):
            raise RecoveryError("publication_archive_content_length_mismatch")
        digest_value = artifact.get("digest")
        if digest_value is not None:
            expected_digest = str(digest_value)
            if expected_digest.startswith("sha256:"):
                expected_digest = expected_digest[len("sha256:"):]
            if not SHA256.fullmatch(expected_digest) or hashlib.sha256(raw_archive).hexdigest() != expected_digest:
                raise RecoveryError("publication_archive_digest_mismatch")
        receipt_raw, binding_raw = extract_receipt_archive(raw_archive)
        publication = verify_receipt(
            receipt_raw, binding_raw, repository=self.repository,
            workflow_head_sha=head_sha, root=self.root,
        )
        after = self._api_payload(f"actions/runs/{run_id}")
        after_identity = validate_workflow_run(
            after, repository=self.repository, repository_id=self.repository_id,
            workflow_path=PUBLISHER_WORKFLOW_PATH, workflow_id=self.publisher_workflow_id,
            default_branch=self.default_branch, required_event="workflow_dispatch",
        )
        if after_identity != (run_id, attempt, head_sha):
            raise RecoveryError("publisher_attempt_changed")
        return {
            "run_id": run_id,
            "attempt": attempt,
            "head_sha": head_sha,
            "publisher_job_started_at": job_start.isoformat(),
            "workflow_id": self.publisher_workflow_id,
            "artifact_id": artifact_id,
            "artifact_size": int(artifact["size_in_bytes"]),
            "archive_sha256": hashlib.sha256(raw_archive).hexdigest(),
            "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
            "receipt_raw": receipt_raw,
            "source_binding_raw": binding_raw,
            "publication": publication,
        }

    @staticmethod
    def _source_claim_rows(
        publication: Mapping[str, Any], journal: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        """Return journal rows that already assert this exact published source."""
        source_sha = publication.get("source_sha")
        manifest_sha = publication.get("manifest_sha256")
        records = journal.get("records")
        if not isinstance(records, list):
            raise RecoveryError("promotion_journal_records_invalid")
        claims: list[Mapping[str, Any]] = []
        for row in records:
            if not isinstance(row, Mapping):
                raise RecoveryError("promotion_journal_record_invalid")
            candidate = row.get("candidate")
            pr = row.get("pr")
            acknowledgements = row.get("acknowledgements")
            if not isinstance(candidate, Mapping) or not isinstance(pr, Mapping) or not isinstance(acknowledgements, list):
                raise RecoveryError("promotion_journal_record_identity_invalid")
            claimed = pr.get("merge_commit_sha") == source_sha
            for ack in acknowledgements:
                if not isinstance(ack, Mapping):
                    raise RecoveryError("promotion_journal_acknowledgement_invalid")
                if (
                    ack.get("status") in {"merged", "publication-pending", "published", "read-back-confirmed"}
                    and ack.get("source_sha") == source_sha
                ):
                    if ack.get("manifest_sha256") != manifest_sha:
                        raise RecoveryError("publication_journal_source_manifest_conflict")
                    claimed = True
            if claimed:
                if candidate.get("manifest_sha256") != manifest_sha:
                    raise RecoveryError("publication_journal_source_manifest_conflict")
                claims.append(row)
        if len(claims) > 1:
            raise RecoveryError("publication_journal_source_claim_ambiguous")
        return claims

    @staticmethod
    def _valid_git_ref(value: Any) -> bool:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 255
            or value.startswith("-")
            or value.startswith("/")
            or value.endswith(("/", ".", ".lock"))
            or ".." in value
            or "//" in value
            or "@{" in value
            or any(ord(character) < 32 or ord(character) == 127 or character in " ~^:?*[\\\\" for character in value)
        ):
            return False
        return True

    def _commit_pull_request(self, verified: Mapping[str, Any]) -> dict[str, Any]:
        """Read the complete PR association for the authenticated published commit."""
        publication = verified.get("publication")
        if not isinstance(publication, Mapping):
            raise RecoveryError("publication_identity_missing")
        source_sha = exact_sha(publication.get("source_sha"), SHA1, "publication_source_sha")
        if self.summary["commit_association_reads"] >= MAX_COMMIT_ASSOCIATION_READS:
            raise RecoveryError("commit_association_read_limit_exceeded")
        self.summary["commit_association_reads"] += 1
        response = self.api.request(
            f"repos/{self.repository}/commits/{source_sha}/pulls?per_page=100",
        )
        rows = parse_json(response.body, label="commit_pull_requests")
        if (
            not isinstance(rows, list)
            or len(rows) > 100
            or has_next_page(response.headers)
            or any(not isinstance(row, Mapping) for row in rows)
        ):
            raise RecoveryError("commit_pull_request_association_incomplete")
        matches = [row for row in rows if row.get("merge_commit_sha") == source_sha]
        if len(matches) != 1:
            raise RecoveryError("commit_pull_request_association_ambiguous_or_missing")
        row = matches[0]
        number = exact_positive_int(row.get("number"), "associated_pull_request_number")
        expected_url = f"https://github.com/{self.repository}/pull/{number}"
        url = row.get("html_url")
        parsed_url = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        if (
            not isinstance(url, str)
            or url != expected_url
            or parsed_url is None
            or parsed_url.scheme != "https"
            or parsed_url.netloc.casefold() != "github.com"
            or parsed_url.path != f"/{self.repository}/pull/{number}"
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise RecoveryError("associated_pull_request_url_invalid")
        if row.get("state") != "closed" or row.get("merge_commit_sha") != source_sha:
            raise RecoveryError("associated_pull_request_not_exactly_merged")
        merged_at = parse_time(row.get("merged_at"), "associated_pull_request_merged_at")
        publisher_started_at = parse_time(
            verified.get("publisher_job_started_at"), "publisher_job_started_at",
        )
        if merged_at > publisher_started_at:
            raise RecoveryError("associated_pull_request_merged_after_publisher")
        base = row.get("base")
        head = row.get("head")
        if not isinstance(base, Mapping) or not isinstance(head, Mapping):
            raise RecoveryError("associated_pull_request_refs_invalid")
        base_repo = base.get("repo")
        head_repo = head.get("repo")
        if (
            not isinstance(base_repo, Mapping)
            or not isinstance(head_repo, Mapping)
            or str(base_repo.get("full_name", "")).casefold() != self.repository.casefold()
            or str(head_repo.get("full_name", "")).casefold() != self.repository.casefold()
            or exact_positive_int(base_repo.get("id"), "associated_base_repository_id") != self.repository_id
            or exact_positive_int(head_repo.get("id"), "associated_head_repository_id") != self.repository_id
            or base.get("ref") != self.default_branch
        ):
            raise RecoveryError("associated_pull_request_repository_or_base_mismatch")
        head_ref = head.get("ref")
        if not self._valid_git_ref(head_ref):
            raise RecoveryError("associated_pull_request_head_ref_invalid")
        head_sha = exact_sha(head.get("sha"), SHA1, "associated_pull_request_head_sha")
        base_sha = exact_sha(base.get("sha"), SHA1, "associated_pull_request_base_sha")
        body = row.get("body")
        if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_PR_BODY_BYTES:
            raise RecoveryError("associated_pull_request_body_invalid")
        return {
            "number": number,
            "url": url,
            "state": "MERGED",
            "body": body,
            "head_ref": head_ref,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "merge_sha": source_sha,
            "merged_at": merged_at,
        }

    @staticmethod
    def _candidate_identity(row: Mapping[str, Any]) -> tuple[Any, ...] | None:
        candidate = row.get("candidate")
        if not isinstance(candidate, Mapping):
            return None
        identity = tuple(candidate.get(field) for field in (
            "repository", "source_id", "scope", "generation_id", "registry_sha256",
            "manifest_sha256", "head_sha",
        ))
        return (str(identity[0]).casefold(), *identity[1:])

    @classmethod
    def _supersedes_row(cls, row: Mapping[str, Any], target: Mapping[str, Any], records: list[Any]) -> bool:
        """Allow only validated journal-history predecessors of the live owner row."""
        target_identity = cls._candidate_identity(target)
        if target_identity is None:
            return False
        current: Mapping[str, Any] = row
        visited: set[int] = set()
        for _ in range(len(records)):
            link = current.get("superseded_by")
            if not isinstance(link, Mapping):
                return False
            identity = tuple(link.get(field) for field in (
                "repository", "source_id", "scope", "generation_id", "registry_sha256",
                "manifest_sha256", "head_sha",
            ))
            identity = (str(identity[0]).casefold(), *identity[1:])
            matches = [
                item for item in records
                if isinstance(item, Mapping) and cls._candidate_identity(item) == identity
            ]
            if len(matches) != 1 or id(matches[0]) in visited:
                return False
            successor = matches[0]
            if cls._candidate_identity(successor) == target_identity:
                return True
            visited.add(id(successor))
            current = successor
        return False

    def _owned_c_candidate(
        self,
        verified: Mapping[str, Any],
        journal: Mapping[str, Any],
        association: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        records = journal.get("records")
        publication = verified.get("publication")
        if not isinstance(records, list) or not isinstance(publication, Mapping):
            raise RecoveryError("promotion_journal_record_identity_invalid")
        associated_number = association["number"]
        associated_head = association["head_sha"]
        associated_ref = association["head_ref"]
        associated_url = association["url"]
        body = association["body"]
        source_sha = publication["source_sha"]
        manifest_sha = publication["manifest_sha256"]
        helper = self.runner.load_module(
            self.root / "scripts/canonical_update_pr.py", "canonical_publication_ack_owner_reader",
        )
        related: list[Mapping[str, Any]] = []
        exact: list[Mapping[str, Any]] = []
        for row in records:
            if not isinstance(row, Mapping):
                raise RecoveryError("promotion_journal_record_invalid")
            candidate = row.get("candidate")
            ownership = row.get("ownership")
            pr = row.get("pr")
            acknowledgements = row.get("acknowledgements")
            if not isinstance(candidate, Mapping) or not isinstance(ownership, Mapping) or not isinstance(pr, Mapping) or not isinstance(acknowledgements, list):
                raise RecoveryError("promotion_journal_record_identity_invalid")
            candidate_head = candidate.get("head_sha")
            branch = ownership.get("branch")
            number = pr.get("number")
            merge_sha = pr.get("merge_commit_sha")
            shares_identity = (
                number == associated_number
                or candidate_head == associated_head
                or branch == associated_ref
                or merge_sha == source_sha
            )
            for ack in acknowledgements:
                if not isinstance(ack, Mapping):
                    raise RecoveryError("promotion_journal_acknowledgement_invalid")
                if ack.get("source_sha") == source_sha and ack.get("status") in {
                    "merged", "publication-pending", "published", "read-back-confirmed",
                }:
                    shares_identity = True
            if not shares_identity:
                continue
            related.append(row)
            if candidate.get("manifest_sha256") != manifest_sha:
                continue
            if candidate.get("repository") != self.repository or not isinstance(candidate.get("source_id"), str) or not isinstance(candidate.get("scope"), str):
                continue
            expected_owner = helper.owner_id(
                candidate["repository"], candidate["source_id"], candidate["scope"],
            )
            generation_id = candidate.get("generation_id")
            if (
                number != associated_number
                or pr.get("url") != associated_url
                or candidate_head != associated_head
                or ownership.get("expected_head_sha") != associated_head
                or branch != associated_ref
                or ownership.get("owner_id") != expected_owner
                or ownership.get("body") != body
                or ownership.get("body_sha256") != hashlib.sha256(body.encode("utf-8")).hexdigest()
                or helper.body_marker(expected_owner, str(generation_id)) not in body
                or pr.get("merge_commit_sha") not in {None, source_sha}
                or row.get("status") not in {"prepared", "pending-review", "merged", "publication-pending", "published", "read-back-confirmed"}
            ):
                continue
            exact.append(row)
        if len(exact) > 1:
            raise RecoveryError("canonical_publication_journal_match_ambiguous")
        if not exact:
            if related:
                raise RecoveryError("canonical_publication_journal_identity_conflict")
            return None
        target = exact[0]
        for row in related:
            if row is target:
                continue
            if not self._supersedes_row(row, target, records):
                raise RecoveryError("canonical_publication_journal_history_conflict")
        manifest_rows = [
            row for row in records
            if isinstance(row, Mapping)
            and isinstance(row.get("candidate"), Mapping)
            and row["candidate"].get("manifest_sha256") == manifest_sha
        ]
        same_pr_rows = [
            row for row in manifest_rows
            if isinstance(row.get("pr"), Mapping) and row["pr"].get("number") == associated_number
        ]
        if len(same_pr_rows) != 1:
            raise RecoveryError("canonical_publication_manifest_candidate_ambiguous")
        self._cache_commit_association_readback(target, association)
        return target

    def _cache_commit_association_readback(
        self, target: Mapping[str, Any], association: Mapping[str, Any],
    ) -> None:
        """Cache only the actual positive row in the complete association response."""
        associated_number = association["number"]
        source_sha = association["merge_sha"]
        pr = target.get("pr")
        if not isinstance(pr, Mapping) or pr.get("number") != associated_number:
            raise RecoveryError("canonical_publication_journal_identity_conflict")
        positive = {
            "number": associated_number,
            "url": association["url"],
            "state": "MERGED",
            "body": association["body"],
            "headRefName": association["head_ref"],
            "headRefOid": association["head_sha"],
            "baseRefName": "main",
            "baseRefOid": association["base_sha"],
            "mergeCommit": {"oid": source_sha},
            "repository": self.repository,
            "headRepository": self.repository,
        }
        self._pr_readbacks[associated_number] = dict(positive)

    def classify_publication(
        self,
        verified: Mapping[str, Any],
        journal: Mapping[str, Any],
        *,
        source_claims: list[Mapping[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        publication = verified.get("publication")
        if not isinstance(publication, Mapping):
            raise RecoveryError("publication_identity_missing")
        if source_claims is None:
            source_claims = self._source_claim_rows(publication, journal)
        association = self._commit_pull_request(verified)
        owner_match = self._owned_c_candidate(verified, journal, association)
        if source_claims:
            if len(source_claims) != 1 or owner_match is not source_claims[0]:
                raise RecoveryError("publication_journal_source_association_conflict")
        if owner_match is not None:
            return "canonical", association
        branch = association["head_ref"]
        body = association["body"]
        if (
            branch.casefold() == CANONICAL_AUTOMATION_BRANCH_PREFIX.rstrip("/").casefold()
            or branch.casefold().startswith(CANONICAL_AUTOMATION_BRANCH_PREFIX.casefold())
            or CANONICAL_OWNER_MARKER_PREFIX.casefold() in body.casefold()
        ):
            raise RecoveryError("canonical_publication_owner_missing_from_journal")
        return "outside_c", association

    def _record_outside_c(self, verified: Mapping[str, Any], association: Mapping[str, Any]) -> None:
        publication = verified["publication"]
        self.summary["outside_c_publications"].append({
            "run_id": verified["run_id"],
            "run_attempt": verified["attempt"],
            "artifact_id": verified["artifact_id"],
            "receipt_sha256": verified["receipt_sha256"],
            "source_sha": publication["source_sha"],
            "manifest_sha256": publication["manifest_sha256"],
            "payload_revision": publication["payload_revision"],
            "pointer_revision": publication["pointer_revision"],
            "pull_request_number": association["number"],
            "pull_request_url": association["url"],
            "merge_sha": association["merge_sha"],
            "head_ref": association["head_ref"],
            "head_sha": association["head_sha"],
            "merged_at": association["merged_at"].isoformat(),
        })

    def recover_one(
        self,
        verified: Mapping[str, Any],
        *,
        expected_pr_number: int,
        current_ack_run_id: int,
        current_ack_attempt: int,
        journal_snapshot: tuple[Mapping[str, Any], str],
    ) -> dict[str, Any]:
        # Bind both event-triggered and scheduled acknowledgements to the
        # original publisher. Health uses that proof for publication ordering;
        # a late acknowledgement must not make an older payload look newer.
        reference = make_recovery_reference(
            repository=self.repository,
            workflow_id=int(verified["workflow_id"]),
            run_id=int(verified["run_id"]),
            attempt=int(verified["attempt"]),
            head_sha=str(verified["head_sha"]),
            artifact_id=int(verified["artifact_id"]),
            receipt_sha256=str(verified["receipt_sha256"]),
            publication=verified["publication"],
        )
        with tempfile.TemporaryDirectory(prefix="canonical-publication-ack-") as raw_dir:
            receipt_path = pathlib.Path(raw_dir) / RECEIPT_MEMBER
            receipt_path.write_bytes(verified["receipt_raw"])

            tail_reserved = False
            write_reservations = 0

            def before_write() -> None:
                nonlocal tail_reserved, write_reservations
                if write_reservations >= MAX_GIT_CAS_TRANSACTIONS:
                    raise RecoveryError("promotion_journal_cas_transaction_limit_exceeded")
                write_reservations += 1
                # Reserve both remaining publisher/C guard passes and a small
                # runner-validation tail exactly once before the first CAS.
                if not tail_reserved and self.api.request_count + 32 > MAX_GITHUB_REQUESTS:
                    raise RecoveryError("github_request_budget_reserve_exhausted")
                tail_reserved = True
                self.assert_idle(
                    current_ack_run_id, current_ack_attempt,
                    include_acknowledgements=False,
                )

            result = self.runner.reconcile_publication(
                self.root, receipt_path,
                publisher_reference=reference,
                expected_pr_number=expected_pr_number,
                before_journal_write=before_write,
                after_journal_write=lambda _state_sha: self._record_git_cas_transaction(),
                journal_snapshot=journal_snapshot,
                pr_readback=self.readback_pr,
            )
        if not isinstance(result, Mapping):
            raise RecoveryError("publication_reconcile_result_invalid")
        return dict(result)

    def _record_git_cas_transaction(self) -> None:
        transactions = self.summary["git_cas_transactions"] + 1
        if transactions > MAX_GIT_CAS_TRANSACTIONS:
            raise RecoveryError("promotion_journal_cas_transaction_limit_exceeded")
        self.summary["git_cas_transactions"] = transactions

    @staticmethod
    def already_acknowledged(
        publication: Mapping[str, Any], journal: Mapping[str, Any],
    ) -> bool:
        """Recognize an immutable publication already fully read back in state."""
        records = journal.get("records")
        if not isinstance(records, list):
            raise RecoveryError("promotion_journal_records_invalid")
        matches = []
        for row in records:
            if not isinstance(row, Mapping) or row.get("status") != "read-back-confirmed" or row.get("superseded_by") is not None:
                continue
            candidate = row.get("candidate")
            acknowledgements = row.get("acknowledgements")
            pr = row.get("pr")
            if not isinstance(candidate, Mapping) or not isinstance(acknowledgements, list) or not acknowledgements or not isinstance(pr, Mapping):
                continue
            latest = acknowledgements[-1]
            if not isinstance(latest, Mapping):
                continue
            if (
                pr.get("state") == "merged"
                and pr.get("merge_commit_sha") == publication.get("source_sha")
                and candidate.get("manifest_sha256") == publication.get("manifest_sha256")
                and latest.get("status") == "read-back-confirmed"
                and latest.get("source_sha") == publication.get("source_sha")
                and latest.get("manifest_sha256") == publication.get("manifest_sha256")
                and latest.get("publication_revision") == publication.get("payload_revision")
                and latest.get("publication_pointer_revision") == publication.get("pointer_revision")
                and latest.get("read_back_verified") is True
                and latest.get("read_back_sha256") == candidate.get("registry_sha256")
                and latest.get("read_back_bytes") == candidate.get("registry_bytes")
                and latest.get("artifact_identity") == {
                    "path": candidate.get("registry_path"),
                    "bytes": candidate.get("registry_bytes"),
                    "sha256": candidate.get("registry_sha256"),
                }
            ):
                matches.append(row)
        if len(matches) > 1:
            raise RecoveryError("already_acknowledged_publication_ambiguous")
        return len(matches) == 1

    def run(
        self,
        *,
        mode: str,
        event: Mapping[str, Any] | None,
        current_ack_run_id: int,
        current_ack_attempt: int,
    ) -> dict[str, Any]:
        if mode not in {"schedule", "workflow_run"}:
            raise RecoveryError("workflow_event_unsupported")
        self.summary["mode"] = mode
        self.initialize()
        self.assert_idle(current_ack_run_id, current_ack_attempt)
        candidates = (
            [(row, None) for row in self.discover()]
            if mode == "schedule"
            else self.publisher_runs(mode, event)
        )
        self.summary["publisher_runs_considered"] = len(candidates)
        if not candidates:
            self.summary["outcome"] = "no_publisher_runs"
            self.summary["github_requests"] = self.api.request_count
            return self.summary
        snapshot = self.load_journal_snapshot()
        if (
            not isinstance(snapshot, tuple) or len(snapshot) != 2
            or not isinstance(snapshot[0], Mapping) or not isinstance(snapshot[1], str)
        ):
            raise RecoveryError("promotion_journal_snapshot_unavailable")
        for initial, event_attempt in candidates:
            # Commit-associated PR rows are scoped to a single receipt source;
            # do not reuse a prior publisher's live read-back by PR number.
            self._pr_readbacks.clear()
            try:
                verified = self.authenticate_publisher(initial, expected_attempt=event_attempt)
            except RecoveryError as exc:
                if mode == "workflow_run":
                    raise
                self.summary["unresolved_candidates"].append({
                    "run_id": initial.get("id"),
                    "reason": str(exc),
                })
                # A successful older publisher run with uncertain proof blocks
                # later candidates. Only verified acknowledgements and explicit
                # non-publishing validations may be skipped in an ordered scan.
                self.summary["outcome"] = "recovery_blocked"
                self.summary["github_requests"] = self.api.request_count
                return self.summary
            if verified is None:
                continue
            try:
                source_claims = self._source_claim_rows(verified["publication"], snapshot[0])
                if self.already_acknowledged(verified["publication"], snapshot[0]):
                    self.summary["already_acknowledged"] += 1
                    self.summary["selected_publisher"] = None
                    continue
                disposition, association = self.classify_publication(
                    verified, snapshot[0], source_claims=source_claims,
                )
            except RecoveryError as exc:
                if mode == "workflow_run":
                    raise
                self.summary["unresolved_candidates"].append({
                    "run_id": initial.get("id"),
                    "reason": str(exc),
                })
                self.summary["outcome"] = "recovery_blocked"
                self.summary["github_requests"] = self.api.request_count
                return self.summary
            if disposition == "outside_c":
                if association is None:
                    raise RecoveryError("outside_c_association_missing")
                self._record_outside_c(verified, association)
                self.summary["selected_publisher"] = None
                continue
            if disposition != "canonical" or association is None:
                raise RecoveryError("publication_disposition_invalid")
            self.summary["selected_publisher"] = {
                "run_id": verified["run_id"],
                "run_attempt": verified["attempt"],
                "head_sha": verified["head_sha"],
                "artifact_id": verified["artifact_id"],
                "receipt_sha256": verified["receipt_sha256"],
            }
            result = self.recover_one(
                verified,
                expected_pr_number=association["number"],
                current_ack_run_id=current_ack_run_id,
                current_ack_attempt=current_ack_attempt,
                journal_snapshot=snapshot,
            )
            if result.get("status") == "already_acknowledged":
                self.summary["already_acknowledged"] += 1
                self.summary["selected_publisher"] = None
                continue
            self.summary["outcome"] = "recovered"
            self.summary["reconcile"] = dict(result)
            if result.get("journal_writes") != self.summary["git_cas_transactions"]:
                raise RecoveryError("promotion_journal_cas_summary_mismatch")
            self.summary["github_requests"] = self.api.request_count
            return self.summary
        self.summary["github_requests"] = self.api.request_count
        if self.summary["non_publishing_validations"] and not self.summary["already_acknowledged"]:
            self.summary["outcome"] = "no_publishing_receipt"
        elif self.summary["already_acknowledged"]:
            self.summary["outcome"] = "already_acknowledged"
        elif self.summary["outside_c_publications"]:
            self.summary["outcome"] = "outside_c_publications_only"
        elif self.summary["unresolved_candidates"]:
            self.summary["outcome"] = "recovery_blocked"
        else:
            self.summary["outcome"] = "no_eligible_publication"
        return self.summary


def load_runner(root: pathlib.Path) -> Any:
    runner_path = root / "scripts/run-canonical-update-promotion.py"
    spec = importlib.util.spec_from_file_location("canonical_publication_ack_runner", runner_path)
    if spec is None or spec.loader is None:
        raise RecoveryError("promotion_runner_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_summary(path: pathlib.Path, summary: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(summary, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    if len(encoded) > 64 * 1024:
        raise RecoveryError("recovery_summary_too_large")
    path.write_bytes(encoded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--summary", type=pathlib.Path, default=pathlib.Path(".datapan/publication-ack-recovery-summary.json"))
    args = parser.parse_args()
    root = args.repository_root.resolve()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    api = None
    recovery = None
    try:
        if not repository or "/" not in repository:
            raise RecoveryError("github_repository_unavailable")
        api = GitHubApi(token or "", base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        recovery = PublicationAckRecovery(root, repository, api, load_runner(root))
        event_payload = None
        if event_name == "workflow_run":
            event_path = os.environ.get("GITHUB_EVENT_PATH")
            if not event_path:
                raise RecoveryError("workflow_run_event_missing")
            value = parse_json(pathlib.Path(event_path).read_bytes(), label="workflow_event")
            if not isinstance(value, Mapping):
                raise RecoveryError("workflow_run_event_invalid")
            event_payload = value
        ack_run_id = exact_positive_int(int(os.environ.get("GITHUB_RUN_ID", "0")), "ack_run_id")
        ack_attempt = exact_positive_int(int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")), "ack_run_attempt")
        summary = recovery.run(
            mode=event_name, event=event_payload,
            current_ack_run_id=ack_run_id, current_ack_attempt=ack_attempt,
        )
        write_summary(args.summary if args.summary.is_absolute() else root / args.summary, summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 1 if summary.get("outcome") == "recovery_blocked" else 0
    except Exception as exc:  # noqa: BLE001 - never expose arbitrary API bodies or signed URLs
        summary = recovery.summary if recovery is not None else {
            "schema_version": "datapan.canonical-publication-ack-recovery.v1",
            "mode": event_name or None,
            "repository": repository or None,
        }
        summary["outcome"] = "recovery_blocked"
        summary["reason"] = str(exc) if isinstance(exc, RecoveryError) else "recovery_failed"
        summary["github_requests"] = api.request_count if api is not None else 0
        write_summary(args.summary if args.summary.is_absolute() else root / args.summary, summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
