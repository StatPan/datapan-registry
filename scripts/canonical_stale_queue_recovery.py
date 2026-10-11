#!/usr/bin/env python3
"""Recover at most one stale scheduled C queue under its native group lock.

The entrypoint is intended to run inside the existing canonical update
promotion workflow. It authenticates the running C attempt and both immutable
workflow sources, writes a durable reservation before its only possible cancel
request, and treats every later invocation as readback-only for that target.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import canonical_stale_queue_state as queue_state


REPOSITORY = "StatPan/datapan-registry"
API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
WORKFLOW_PATH = ".github/workflows/canonical-update-promotion.yml"
POLICY_PATH = "policy/canonical-stale-queue-recovery.v1.json"
POLICY_SCHEMA_PATH = "schemas/datapan.canonical-stale-queue-recovery-policy.v1.schema.json"
RECEIPT_SCHEMA_PATH = "schemas/datapan.canonical-stale-c-queue-recovery-receipt.v1.schema.json"
RECEIPT_SCHEMA = "datapan.canonical-stale-c-queue-recovery-receipt.v1"
POLICY_SCHEMA = "datapan.canonical-stale-queue-recovery-policy.v1"
SHA1_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
READBACK_SLEEP_SECONDS = 4.0


class RecoveryError(RuntimeError):
    """A native admission, proof, or bounded transport check failed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class RunIdentity:
    repository_id: int
    repository: str
    workflow_id: int
    run_id: int
    run_attempt: int
    event: str
    branch: str
    head_sha: str
    status: str
    conclusion: str | None
    created_at: str
    updated_at: str

    def reservation_identity(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "repository": REPOSITORY,
            "workflow_id": self.workflow_id,
            "workflow_path": WORKFLOW_PATH,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "event": self.event,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def controller_identity(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "repository": REPOSITORY,
            "workflow_id": self.workflow_id,
            "workflow_path": WORKFLOW_PATH,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "event": self.event,
            "branch": self.branch,
            "head_sha": self.head_sha,
        }


@dataclass(frozen=True, slots=True)
class ControllerAuthority:
    identity: RunIdentity
    workflow_sha256: str
    workflow_bytes: bytes
    policy_sha256: str
    repository_id: int
    workflow_id: int


class ApiBudget:
    def __init__(
        self, *, deadline: float, max_requests: int, max_response_bytes: int,
        max_total_response_bytes: int, request_timeout: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.deadline = deadline
        self.max_requests = max_requests
        self.max_response_bytes = max_response_bytes
        self.max_total_response_bytes = max_total_response_bytes
        self.request_timeout = request_timeout
        self.monotonic = monotonic
        self.requests = 0
        self.response_bytes = 0

    def reserve(self, maximum_response: int) -> float:
        now = self.monotonic()
        if not isinstance(now, (int, float)) or not now < self.deadline:
            raise RecoveryError("invocation_deadline_expired")
        if self.requests >= self.max_requests:
            raise RecoveryError("api_request_budget_exhausted")
        if not 1 <= maximum_response <= self.max_response_bytes:
            raise RecoveryError("api_response_bound_invalid")
        if self.response_bytes + maximum_response > self.max_total_response_bytes:
            raise RecoveryError("api_response_budget_exhausted")
        self.requests += 1
        return min(self.request_timeout, self.deadline - now)

    def check_deadline(self) -> None:
        if not self.monotonic() < self.deadline:
            raise RecoveryError("invocation_deadline_expired")

    def account(self, amount: int) -> None:
        if amount < 0 or self.response_bytes + amount > self.max_total_response_bytes:
            raise RecoveryError("api_response_budget_exhausted")
        self.response_bytes += amount


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class GitHubApi:
    """Credential-safe, no-retry GitHub API client with one cumulative budget."""

    def __init__(
        self, token: str, *, policy: Mapping[str, Any], deadline: float,
        transport: Callable[[urllib.request.Request, float], ApiResponse] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(token, str) or not token or "\n" in token or "\x00" in token:
            raise RecoveryError("github_token_missing_or_malformed")
        self._token = token
        self._transport = transport
        self._monotonic = monotonic
        self.sleep = sleep
        self.budget = ApiBudget(
            deadline=deadline,
            max_requests=policy["maximum_api_requests"],
            max_response_bytes=policy["maximum_response_bytes"],
            max_total_response_bytes=policy["maximum_total_response_bytes"],
            request_timeout=policy["request_timeout_seconds"],
            monotonic=monotonic,
        )
        self._opener = urllib.request.build_opener(NoRedirectHandler())
        self.cancel_posts = 0
        self.cancel_post_started = False

    @contextlib.contextmanager
    def deadline_scope(self, deadline: float):
        previous = self.budget.deadline
        self.budget.deadline = min(previous, deadline)
        try:
            yield
        finally:
            self.budget.deadline = previous

    @contextlib.contextmanager
    def _absolute_timeout(self, timeout: float):
        """Interrupt blocking urllib reads at the original absolute deadline.

        Socket timeouts alone are idle timeouts and can be extended forever by
        a response that drips bytes. The production entrypoint is a serial CLI
        on the main thread, so a one-shot real-time alarm bounds open and body
        reads together. Injected offline transports remain clock-checkable.
        """
        if self._transport is not None:
            yield
            return
        if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
            raise RecoveryError("absolute_api_deadline_unavailable")
        prior_seconds, prior_interval = signal.getitimer(signal.ITIMER_REAL)
        if prior_seconds or prior_interval:
            raise RecoveryError("absolute_api_deadline_signal_already_owned")
        previous_handler = signal.getsignal(signal.SIGALRM)
        remaining = min(timeout, self.budget.deadline - self._monotonic())
        if remaining <= 0:
            raise RecoveryError("invocation_deadline_expired")

        def expire(_signum, _frame):  # type: ignore[no-untyped-def]
            raise TimeoutError("absolute API deadline expired")

        signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, remaining)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)

    def _request(self, method: str, path_or_url: str, *, data: bytes | None, maximum: int) -> ApiResponse:
        timeout = self.budget.reserve(maximum)
        if path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = API_ROOT + path_or_url
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise RecoveryError("api_request_origin_invalid")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "datapan-registry-stale-queue-recovery/1",
            },
            method=method,
        )
        try:
            with self._absolute_timeout(timeout):
                if self._transport is not None:
                    if method == "POST":
                        self.cancel_post_started = True
                    response = self._transport(request, timeout)
                else:
                    if method == "POST":
                        self.cancel_post_started = True
                    with self._opener.open(request, timeout=timeout) as opened:
                        response = ApiResponse(
                            status=int(opened.status),
                            headers=dict(opened.headers.items()),
                            body=opened.read(maximum + 1),
                        )
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                with self._absolute_timeout(timeout):
                    body = exc.read(maximum + 1)
            except Exception:
                pass
            response = ApiResponse(int(exc.code), dict(exc.headers.items()) if exc.headers else {}, body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RecoveryError("api_transport_ambiguous" if method == "POST" else "api_read_failed") from exc
        except RecoveryError:
            raise
        except Exception as exc:
            raise RecoveryError("api_transport_ambiguous" if method == "POST" else "api_read_failed") from exc
        self.budget.check_deadline()
        if not isinstance(response, ApiResponse):
            raise RecoveryError("api_transport_response_invalid")
        if not isinstance(response.body, bytes) or len(response.body) > maximum:
            raise RecoveryError("api_response_too_large")
        self.budget.account(len(response.body))
        return response

    @staticmethod
    def _strict_json(raw: bytes) -> Any:
        def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise RecoveryError("api_duplicate_json_key")
                result[key] = value
            return result
        try:
            return json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError("api_json_invalid") from exc

    def get_json(self, path: str, *, maximum: int) -> tuple[dict[str, Any], Mapping[str, str]]:
        response = self._request("GET", path, data=None, maximum=maximum)
        if response.status != 200:
            raise RecoveryError(f"api_get_http_{response.status}")
        value = self._strict_json(response.body)
        if not isinstance(value, dict):
            raise RecoveryError("api_json_object_required")
        return value, response.headers

    def post_cancel(self, run_id: int) -> str:
        if self.cancel_posts != 0:
            raise RecoveryError("cancel_post_limit_exceeded")
        self.cancel_posts += 1
        response = self._request(
            "POST", f"/repos/{REPOSITORY}/actions/runs/{run_id}/cancel",
            data=b"", maximum=1024 * 64,
        )
        if response.status == 202:
            return "accepted_202"
        if response.status == 204:
            return "accepted_204"
        if (
            response.status >= 500
            or response.status in {408, 425}
            or 300 <= response.status < 400
            or 200 <= response.status < 300
        ):
            raise RecoveryError("cancel_http_outcome_ambiguous")
        return "denied"


def _parse_utc(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not UTC_RE.fullmatch(value):
        raise RecoveryError(f"{label}_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RecoveryError(f"{label}_invalid") from exc
    return parsed.astimezone(dt.timezone.utc)


def _iso_utc(value: dt.datetime) -> str:
    value = value.astimezone(dt.timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha1(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA1_RE.fullmatch(value):
        raise RecoveryError(f"{label}_sha_invalid")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RecoveryError(f"{label}_invalid")
    return value


def _extract_identity(payload: Mapping[str, Any], *, status_filter: str | None = None) -> RunIdentity:
    try:
        repository = payload["repository"]
        if not isinstance(repository, Mapping):
            raise RecoveryError("run_repository_identity_invalid")
        repository_id = _positive_int(repository.get("id"), "run_repository_id")
        full_name = repository.get("full_name")
        if not isinstance(full_name, str):
            raise RecoveryError("run_repository_name_invalid")
        workflow_id = _positive_int(payload.get("workflow_id"), "run_workflow_id")
        run_id = _positive_int(payload.get("id"), "run_id")
        run_attempt = _positive_int(payload.get("run_attempt"), "run_attempt")
        event = payload.get("event")
        branch = payload.get("head_branch")
        status = payload.get("status")
        conclusion = payload.get("conclusion")
        created_at = payload.get("created_at")
        updated_at = payload.get("updated_at")
        head_sha = _sha1(payload.get("head_sha"), "run_head")
    except KeyError as exc:
        raise RecoveryError("run_identity_field_missing") from exc
    if not all(isinstance(value, str) and value for value in (event, branch, status, created_at, updated_at)):
        raise RecoveryError("run_identity_text_invalid")
    if conclusion is not None and not isinstance(conclusion, str):
        raise RecoveryError("run_conclusion_invalid")
    _parse_utc(created_at, "run_created_at")
    _parse_utc(updated_at, "run_updated_at")
    if _parse_utc(created_at, "run_created_at") > _parse_utc(updated_at, "run_updated_at"):
        raise RecoveryError("run_updated_before_created")
    if status_filter is not None and status != status_filter:
        raise RecoveryError("run_status_filter_mismatch")
    return RunIdentity(
        repository_id=repository_id,
        repository=full_name,
        workflow_id=workflow_id,
        run_id=run_id,
        run_attempt=run_attempt,
        event=event,
        branch=branch,
        head_sha=head_sha,
        status=status,
        conclusion=conclusion,
        created_at=created_at,
        updated_at=updated_at,
    )


def _same_native_identity(left: RunIdentity, right: RunIdentity) -> bool:
    return (
        left.repository_id == right.repository_id
        and left.repository == right.repository
        and left.workflow_id == right.workflow_id
        and left.run_id == right.run_id
        and left.run_attempt == right.run_attempt
        and left.event == right.event
        and left.branch == right.branch
        and left.head_sha == right.head_sha
        and left.created_at == right.created_at
        and left.updated_at == right.updated_at
    )


def _same_run_identity(left: RunIdentity, right: RunIdentity) -> bool:
    """Compare immutable native identity while allowing status/update time to move."""
    return (
        left.repository_id == right.repository_id
        and left.repository.casefold() == right.repository.casefold()
        and left.workflow_id == right.workflow_id
        and left.run_id == right.run_id
        and left.run_attempt == right.run_attempt
        and left.event == right.event
        and left.branch == right.branch
        and left.head_sha == right.head_sha
        and left.created_at == right.created_at
    )


def _parse_policy(root: pathlib.Path) -> tuple[dict[str, Any], bytes, str]:
    path = root / POLICY_PATH
    schema_path = root / POLICY_SCHEMA_PATH
    try:
        if path.stat().st_size > 65536 or schema_path.stat().st_size > 65536:
            raise RecoveryError("policy_file_size_out_of_bounds")
        raw = path.read_bytes()
        schema_raw = schema_path.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError("policy_unavailable_or_invalid") from exc
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise RecoveryError("policy_duplicate_key")
            value[key] = item
        return value
    try:
        policy = json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs)
        policy_schema = json.loads(schema_raw.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError("policy_unavailable_or_invalid") from exc
    try:
        import jsonschema
        validator = jsonschema.Draft202012Validator(
            policy_schema, format_checker=jsonschema.FormatChecker(),
        )
        validator.check_schema(policy_schema)
        validator.validate(policy)
    except ImportError as exc:
        raise RecoveryError("policy_schema_validator_unavailable") from exc
    except Exception as exc:
        raise RecoveryError("policy_schema_validation_failed") from exc
    if not isinstance(policy, dict) or policy.get("schema_version") != POLICY_SCHEMA:
        raise RecoveryError("policy_schema_unsupported")
    required = {
        "schema_version", "repository", "default_branch", "workflow_path", "concurrency_group",
        "cancel_in_progress", "minimum_age_seconds", "maximum_runtime_seconds",
        "request_timeout_seconds", "maximum_api_requests", "maximum_response_bytes",
        "maximum_total_response_bytes", "maximum_run_pages", "maximum_queued_runs",
        "maximum_jobs_pages", "maximum_compare_pages", "maximum_compare_commits",
        "terminal_observations", "terminal_observation_window_seconds",
        "maximum_reservation_records", "maximum_journal_bytes", "maximum_sidecar_bytes",
    }
    if set(policy) != required:
        raise RecoveryError("policy_fields_not_closed")
    fixed = {
        "repository": REPOSITORY,
        "default_branch": "main",
        "workflow_path": WORKFLOW_PATH,
        "concurrency_group": "canonical-update-promotion",
        "cancel_in_progress": False,
        "minimum_age_seconds": 86400,
        "maximum_runtime_seconds": 120,
        "request_timeout_seconds": 8,
        "maximum_api_requests": 96,
        "maximum_response_bytes": 2097152,
        "maximum_total_response_bytes": 16777216,
        "maximum_run_pages": 2,
        "maximum_queued_runs": 12,
        "maximum_jobs_pages": 2,
        "maximum_compare_pages": 4,
        "maximum_compare_commits": 400,
        "terminal_observations": 3,
        "terminal_observation_window_seconds": 15,
        "maximum_reservation_records": 64,
        "maximum_journal_bytes": 8388608,
        "maximum_sidecar_bytes": 262144,
    }
    if policy != {"schema_version": POLICY_SCHEMA, **fixed}:
        raise RecoveryError("policy_value_outside_closed_contract")
    digest = hashlib.sha256(raw).hexdigest()
    if not SHA256_RE.fullmatch(digest):
        raise RecoveryError("policy_digest_invalid")
    return policy, raw, digest


def _repository_context(env: Mapping[str, str]) -> dict[str, Any]:
    def env_int(name: str) -> int:
        raw = env.get(name)
        if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
            raise RecoveryError(f"environment_{name.lower()}_invalid")
        return _positive_int(int(raw), f"environment_{name.lower()}")

    repository = env.get("GITHUB_REPOSITORY")
    if repository != REPOSITORY:
        raise RecoveryError("environment_repository_mismatch")
    event = env.get("GITHUB_EVENT_NAME")
    ref = env.get("GITHUB_REF")
    sha = _sha1(env.get("GITHUB_SHA"), "environment_head")
    workflow_ref = env.get("GITHUB_WORKFLOW_REF")
    job_name = env.get("GITHUB_JOB")
    if not isinstance(workflow_ref, str):
        raise RecoveryError("environment_workflow_ref_missing")
    prefix, sep, ref_value = workflow_ref.partition("@")
    if not sep or prefix != f"{REPOSITORY}/{WORKFLOW_PATH}" or ref_value != "refs/heads/main":
        raise RecoveryError("environment_workflow_ref_mismatch")
    if ref != "refs/heads/main" or job_name != "reconcile":
        raise RecoveryError("environment_branch_or_job_mismatch")
    if env.get("C_INITIAL_CHECKOUT_SHA") != sha or env.get("C_EVALUATOR_SOURCE_SHA") != sha:
        raise RecoveryError("trusted_checkout_sha_mismatch")
    return {
        "repository_id": env_int("GITHUB_REPOSITORY_ID"),
        "run_id": env_int("GITHUB_RUN_ID"),
        "run_attempt": env_int("GITHUB_RUN_ATTEMPT"),
        "event": event,
        "branch": "main",
        "head_sha": sha,
        "job_name": job_name,
    }


def _local_checkout_sha(
    root: pathlib.Path, *, deadline: float, monotonic: Callable[[], float],
) -> str:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise RecoveryError("invocation_deadline_expired")
    git = pathlib.Path("/usr/bin/git")
    if not git.exists():
        raise RecoveryError("trusted_git_unavailable")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
    }
    try:
        completed = subprocess.run(
            [str(git), "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=min(2.0, remaining),
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryError("trusted_checkout_unreadable") from exc
    if not monotonic() < deadline:
        raise RecoveryError("invocation_deadline_expired")
    try:
        value = completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RecoveryError("trusted_checkout_sha_invalid") from exc
    return _sha1(value, "trusted_checkout")


def _yaml_group(raw: bytes, *, expected_group: str) -> None:
    if not isinstance(raw, bytes) or not raw or len(raw) > 262144:
        raise RecoveryError("workflow_source_size_out_of_bounds")
    try:
        import yaml
    except ImportError as exc:
        raise RecoveryError("workflow_yaml_parser_unavailable") from exc

    class UniqueKeyLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
            self.flatten_mapping(node)
            result: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in result
                except TypeError as exc:
                    raise RecoveryError("workflow_yaml_unhashable_key") from exc
                if duplicate:
                    raise RecoveryError("workflow_yaml_duplicate_key")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    try:
        value = yaml.load(raw.decode("utf-8"), Loader=UniqueKeyLoader)
    except RecoveryError:
        raise
    except Exception as exc:
        raise RecoveryError("workflow_yaml_invalid") from exc
    if not isinstance(value, dict):
        raise RecoveryError("workflow_yaml_root_invalid")
    concurrency = value.get("concurrency")
    if not isinstance(concurrency, dict) or set(concurrency) != {"group", "cancel-in-progress"}:
        raise RecoveryError("workflow_group_not_literal_workflow_level")
    if concurrency.get("group") != expected_group or concurrency.get("cancel-in-progress") is not False:
        raise RecoveryError("workflow_group_not_literal_workflow_level")
    jobs = value.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise RecoveryError("workflow_jobs_invalid")
    for job in jobs.values():
        if not isinstance(job, dict) or "concurrency" in job:
            raise RecoveryError("workflow_job_level_group_rejected")


def _link_next(headers: Mapping[str, str], expected_path: str, expected_query: Mapping[str, str], current_page: int) -> bool:
    link = next((value for key, value in headers.items() if key.lower() == "link"), None)
    if link is None:
        return False
    next_links: list[str] = []
    for segment in link.split(","):
        match = re.fullmatch(r"\s*<([^>]+)>\s*;\s*rel=\"([^\"]+)\"\s*", segment)
        if not match:
            raise RecoveryError("api_pagination_link_malformed")
        if "next" in match.group(2).split():
            next_links.append(match.group(1))
    if not next_links:
        return False
    if len(next_links) != 1:
        raise RecoveryError("api_pagination_next_ambiguous")
    parsed = urllib.parse.urlsplit(next_links[0])
    if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.path != expected_path:
        raise RecoveryError("api_pagination_origin_or_path_invalid")
    values = urllib.parse.parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
    expected = {**expected_query, "per_page": "100", "page": str(current_page + 1)}
    if values != {key: [value] for key, value in expected.items()}:
        raise RecoveryError("api_pagination_query_changed")
    return True


def _paged_list(
    api: GitHubApi,
    *, path: str,
    query: Mapping[str, str],
    item_key: str,
    maximum_pages: int,
    maximum_items: int,
    maximum_bytes: int,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    total_count: int | None = None
    for page in range(1, maximum_pages + 1):
        values = {**query, "per_page": "100", "page": str(page)}
        url = path + "?" + urllib.parse.urlencode(values)
        payload, headers = api.get_json(url, maximum=maximum_bytes)
        count = payload.get("total_count")
        rows = payload.get(item_key)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RecoveryError("api_pagination_total_invalid")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RecoveryError("api_pagination_items_invalid")
        if total_count is None:
            total_count = count
        elif total_count != count:
            raise RecoveryError("api_pagination_total_changed")
        if len(rows) > 100 or len(collected) + len(rows) > maximum_items:
            raise RecoveryError("api_pagination_item_bound_exceeded")
        for row in rows:
            item_id = row.get("id")
            if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 1:
                raise RecoveryError("api_pagination_item_identity_invalid")
            if item_id in seen_ids:
                raise RecoveryError("api_pagination_duplicate_item")
            seen_ids.add(item_id)
        collected.extend(rows)
        has_next = _link_next(headers, path, query, page)
        if has_next:
            if page >= maximum_pages:
                raise RecoveryError("api_pagination_bound_incomplete")
            continue
        if len(collected) != total_count:
            raise RecoveryError("api_pagination_evidence_incomplete")
        return collected
    raise RecoveryError("api_pagination_evidence_incomplete")


def _jobs(api: GitHubApi, run_id: int, attempt: int, policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    path = f"/repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs"
    return _paged_list(
        api,
        path=path,
        query={},
        item_key="jobs",
        maximum_pages=policy["maximum_jobs_pages"],
        maximum_items=1000,
        maximum_bytes=policy["maximum_response_bytes"],
    )


def _runs(
    api: GitHubApi,
    *, workflow_id: int,
    status: str,
    policy: Mapping[str, Any],
    branch: str | None = "main",
    event: str | None = "schedule",
) -> list[dict[str, Any]]:
    path = f"/repos/{REPOSITORY}/actions/workflows/{workflow_id}/runs"
    query = {"status": status}
    if branch is not None:
        query["branch"] = branch
    if event is not None:
        query["event"] = event
    return _paged_list(
        api,
        path=path,
        query=query,
        item_key="workflow_runs",
        maximum_pages=policy["maximum_run_pages"],
        maximum_items=policy["maximum_queued_runs"],
        maximum_bytes=policy["maximum_response_bytes"],
    )


def _workflow_metadata(api: GitHubApi, workflow_id: int, policy: Mapping[str, Any]) -> None:
    payload, _headers = api.get_json(
        f"/repos/{REPOSITORY}/actions/workflows/{workflow_id}",
        maximum=policy["maximum_response_bytes"],
    )
    if (
        payload.get("id") != workflow_id
        or payload.get("path") != policy["workflow_path"]
        or payload.get("state") != "active"
    ):
        raise RecoveryError("workflow_native_identity_mismatch")


def _run_payload(api: GitHubApi, run_id: int, policy: Mapping[str, Any]) -> RunIdentity:
    payload, _headers = api.get_json(
        f"/repos/{REPOSITORY}/actions/runs/{run_id}",
        maximum=policy["maximum_response_bytes"],
    )
    identity = _extract_identity(payload)
    if identity.run_id != run_id:
        raise RecoveryError("run_id_changed")
    return identity


def _content_bytes(api: GitHubApi, sha: str, policy: Mapping[str, Any]) -> bytes:
    url = (
        f"/repos/{REPOSITORY}/contents/"
        f"{urllib.parse.quote(policy['workflow_path'], safe='/')}?"
        f"{urllib.parse.urlencode({'ref': sha})}"
    )
    payload, _headers = api.get_json(url, maximum=policy["maximum_response_bytes"])
    if (
        payload.get("type") != "file"
        or payload.get("path") != policy["workflow_path"]
        or payload.get("encoding") != "base64"
        or isinstance(payload.get("size"), bool)
        or not isinstance(payload.get("size"), int)
        or not isinstance(payload.get("sha"), str)
        or not SHA1_RE.fullmatch(payload["sha"])
        or not isinstance(payload.get("content"), str)
    ):
        raise RecoveryError("workflow_blob_identity_invalid")
    try:
        encoded = payload["content"].replace("\n", "").replace("\r", "")
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise RecoveryError("workflow_blob_base64_invalid") from exc
    blob_sha = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if len(raw) != payload["size"] or blob_sha != payload["sha"]:
        raise RecoveryError("workflow_blob_readback_mismatch")
    return raw


def _compare_ancestor(
    api: GitHubApi, controller_sha: str, target_sha: str, policy: Mapping[str, Any],
) -> bool:
    # GitHub's compare response has base_commit and merge_base_commit, but no
    # top-level head_commit. Put the requested older target in BASE so a
    # strict ancestor is proven by merge_base == target and an ahead-only
    # complete list ending at the full current controller SHA.
    path = f"/repos/{REPOSITORY}/compare/{target_sha}...{controller_sha}"
    api_url = f"{API_ROOT}{path}"
    html_url = f"https://github.com/{REPOSITORY}/compare/{target_sha}...{controller_sha}"
    total_commits: int | None = None
    first_metadata: tuple[Any, ...] | None = None
    commit_shas: list[str] = []
    seen: set[str] = set()

    for page in range(1, policy["maximum_compare_pages"] + 1):
        query = {"per_page": "100", "page": str(page)}
        payload, headers = api.get_json(
            f"{path}?{urllib.parse.urlencode(query)}",
            maximum=policy["maximum_response_bytes"],
        )
        base = payload.get("base_commit")
        merge_base = payload.get("merge_base_commit")
        status = payload.get("status")
        ahead_by = payload.get("ahead_by")
        behind_by = payload.get("behind_by")
        count = payload.get("total_commits")
        if (
            payload.get("url") != api_url
            or payload.get("html_url") != html_url
            or not isinstance(base, dict)
            or not isinstance(merge_base, dict)
            or not isinstance(base.get("sha"), str)
            or not SHA1_RE.fullmatch(base["sha"])
            or not isinstance(merge_base.get("sha"), str)
            or not SHA1_RE.fullmatch(merge_base["sha"])
            or status not in {"ahead", "behind", "identical", "diverged"}
            or isinstance(ahead_by, bool)
            or not isinstance(ahead_by, int)
            or isinstance(behind_by, bool)
            or not isinstance(behind_by, int)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or min(ahead_by, behind_by, count) < 0
        ):
            raise RecoveryError("ancestry_response_identity_invalid")
        if base["sha"] != target_sha:
            raise RecoveryError("ancestry_base_sha_mismatch")
        metadata = (merge_base["sha"], status, ahead_by, behind_by, count)
        if first_metadata is None:
            first_metadata = metadata
            total_commits = count
            if status != "ahead" or merge_base["sha"] != target_sha:
                return False
            if count < 1 or ahead_by != count or behind_by != 0:
                raise RecoveryError("ancestry_ahead_counts_inconsistent")
            if count > policy["maximum_compare_commits"]:
                raise RecoveryError("ancestry_commit_bound_exceeded")
        elif metadata != first_metadata:
            raise RecoveryError("ancestry_page_metadata_changed")

        commits = payload.get("commits")
        if not isinstance(commits, list) or any(not isinstance(item, dict) for item in commits):
            raise RecoveryError("ancestry_commit_page_invalid")
        if len(commits) > 100 or len(commit_shas) + len(commits) > policy["maximum_compare_commits"]:
            raise RecoveryError("ancestry_commit_page_bound_exceeded")
        for commit in commits:
            sha = commit.get("sha")
            if not isinstance(sha, str) or not SHA1_RE.fullmatch(sha) or sha in seen:
                raise RecoveryError("ancestry_commit_identity_invalid")
            seen.add(sha)
            commit_shas.append(sha)
        has_next = _link_next(headers, path, {"per_page": "100"}, page)
        if has_next:
            if page >= policy["maximum_compare_pages"]:
                raise RecoveryError("ancestry_pagination_bound_incomplete")
            continue
        if len(commit_shas) != total_commits:
            raise RecoveryError("ancestry_commit_list_incomplete")
        if not commit_shas or commit_shas[-1] != controller_sha:
            raise RecoveryError("ancestry_commit_list_head_mismatch")
        return True
    raise RecoveryError("ancestry_pagination_evidence_incomplete")


def _authenticated_repo(
    api: GitHubApi,
    *, expected_repository_id: int,
    policy: Mapping[str, Any],
) -> str:
    repository, _headers = api.get_json(
        f"/repos/{REPOSITORY}", maximum=policy["maximum_response_bytes"],
    )
    full_name = repository.get("full_name")
    if (
        repository.get("id") != expected_repository_id
        or not isinstance(full_name, str)
        or full_name.casefold() != REPOSITORY.casefold()
        or repository.get("default_branch") != policy["default_branch"]
    ):
        raise RecoveryError("native_repository_identity_mismatch")
    branch, _headers = api.get_json(
        f"/repos/{REPOSITORY}/branches/{urllib.parse.quote(policy['default_branch'], safe='')}",
        maximum=policy["maximum_response_bytes"],
    )
    commit = branch.get("commit")
    if branch.get("name") != policy["default_branch"] or not isinstance(commit, dict):
        raise RecoveryError("native_default_branch_response_invalid")
    return _sha1(commit.get("sha"), "native_default_branch")


def _jobs_count_for_controller(
    api: GitHubApi,
    *, identity: RunIdentity,
    job_name: str,
    policy: Mapping[str, Any],
) -> None:
    jobs = _jobs(api, identity.run_id, identity.run_attempt, policy)
    if len(jobs) != 1:
        raise RecoveryError("controller_job_set_contradictory")
    job = jobs[0]
    if (
        job.get("run_id") != identity.run_id
        or job.get("name") != job_name
        or job.get("status") != "in_progress"
        or job.get("conclusion") is not None
        or (job.get("run_attempt") is not None and job.get("run_attempt") != identity.run_attempt)
    ):
        raise RecoveryError("controller_exact_attempt_job_not_running")


def _authenticate_controller(
    api: GitHubApi,
    *,
    env: Mapping[str, str],
    expected_env: Mapping[str, Any],
    policy: Mapping[str, Any],
    policy_sha256: str,
    root: pathlib.Path,
    previous: ControllerAuthority | None = None,
) -> ControllerAuthority:
    repository_id = expected_env["repository_id"]
    main_sha = _authenticated_repo(api, expected_repository_id=repository_id, policy=policy)
    if (
        main_sha != expected_env["head_sha"]
        or _local_checkout_sha(
            root, deadline=api.budget.deadline, monotonic=api.budget.monotonic,
        ) != main_sha
    ):
        raise RecoveryError("controller_checkout_or_main_source_moved")
    _workflow_metadata(api, previous.workflow_id if previous else _workflow_id_from_run(api, expected_env, policy), policy)
    controller = _run_payload(api, expected_env["run_id"], policy)
    if (
        controller.repository_id != repository_id
        or controller.repository.casefold() != REPOSITORY.casefold()
        or controller.run_id != expected_env["run_id"]
        or controller.run_attempt != expected_env["run_attempt"]
        or controller.event != "schedule"
        or controller.branch != policy["default_branch"]
        or controller.head_sha != main_sha
        or controller.status != "in_progress"
        or controller.conclusion is not None
    ):
        raise RecoveryError("controller_native_identity_or_status_mismatch")
    workflow_id = controller.workflow_id
    if previous is not None and workflow_id != previous.workflow_id:
        raise RecoveryError("controller_workflow_id_changed")
    if expected_env.get("workflow_id") not in (None, workflow_id):
        raise RecoveryError("controller_workflow_id_mismatch")
    controller_bytes = _content_bytes(api, controller.head_sha, policy)
    _yaml_group(controller_bytes, expected_group=policy["concurrency_group"])
    controller_digest = hashlib.sha256(controller_bytes).hexdigest()
    if previous is not None and controller_digest != previous.workflow_sha256:
        raise RecoveryError("controller_workflow_source_changed")
    jobs = _jobs(api, controller.run_id, controller.run_attempt, policy)
    if len(jobs) != 1:
        raise RecoveryError("controller_job_set_contradictory")
    job = jobs[0]
    if (
        job.get("run_id") != controller.run_id
        or job.get("name") != expected_env["job_name"]
        or job.get("status") != "in_progress"
        or job.get("conclusion") is not None
        or (job.get("run_attempt") is not None and job.get("run_attempt") != controller.run_attempt)
    ):
        raise RecoveryError("controller_exact_attempt_job_not_running")
    # The literal workflow-level group is shared across events and branches.
    # A filtered list could hide a competing C run and invalidate exclusion.
    active = _runs(
        api, workflow_id=workflow_id, status="in_progress", policy=policy,
        branch=None, event=None,
    )
    if len(active) != 1:
        raise RecoveryError("contradictory_running_c_authority")
    active_identity = _extract_identity(active[0], status_filter="in_progress")
    if not _same_run_identity(active_identity, controller):
        raise RecoveryError("contradictory_running_c_authority")
    return ControllerAuthority(
        identity=controller,
        workflow_sha256=controller_digest,
        workflow_bytes=controller_bytes,
        policy_sha256=policy_sha256,
        repository_id=repository_id,
        workflow_id=workflow_id,
    )


def _workflow_id_from_run(
    api: GitHubApi, expected_env: Mapping[str, Any], policy: Mapping[str, Any],
) -> int:
    controller = _run_payload(api, expected_env["run_id"], policy)
    if controller.run_attempt != expected_env["run_attempt"]:
        raise RecoveryError("controller_attempt_changed")
    return controller.workflow_id


def _candidate_identity(
    payload: Mapping[str, Any], *, controller: ControllerAuthority, policy: Mapping[str, Any],
) -> RunIdentity:
    identity = _extract_identity(payload, status_filter="queued")
    if (
        identity.repository_id != controller.repository_id
        or identity.repository.casefold() != REPOSITORY.casefold()
        or identity.workflow_id != controller.workflow_id
        or identity.event != "schedule"
        or identity.branch != policy["default_branch"]
        or identity.conclusion is not None
    ):
        raise RecoveryError("queued_run_identity_mismatch")
    return identity


def _job_list_complete(api: GitHubApi, identity: RunIdentity, policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _jobs(api, identity.run_id, identity.run_attempt, policy)


def _excluded(identity: RunIdentity, reason: str) -> dict[str, Any]:
    return {"run_id": identity.run_id, "run_attempt": identity.run_attempt, "reason": reason}


def _discover_candidates(
    api: GitHubApi,
    *,
    controller: ControllerAuthority,
    policy: Mapping[str, Any],
    now: dt.datetime,
    excluded: list[dict[str, Any]],
) -> list[tuple[RunIdentity, str]]:
    rows = _runs(api, workflow_id=controller.workflow_id, status="queued", policy=policy)
    candidates: list[tuple[RunIdentity, str]] = []
    controller_created = _parse_utc(controller.identity.created_at, "controller_created_at")
    for row in rows:
        listed = _candidate_identity(row, controller=controller, policy=policy)
        if listed.run_id == controller.identity.run_id:
            raise RecoveryError("controller_appeared_in_queued_list")
        if listed.run_attempt != 1:
            excluded.append(_excluded(listed, "rerun"))
            continue
        created = _parse_utc(listed.created_at, "candidate_created_at")
        updated = _parse_utc(listed.updated_at, "candidate_updated_at")
        if created > now or updated > now:
            raise RecoveryError("candidate_timestamp_in_future")
        if created >= controller_created or updated >= controller_created:
            excluded.append(_excluded(listed, "not_older_than_controller"))
            continue
        minimum = dt.timedelta(seconds=policy["minimum_age_seconds"])
        if now - created < minimum or now - updated < minimum:
            excluded.append(_excluded(listed, "recent"))
            continue
        if listed.head_sha == controller.identity.head_sha:
            excluded.append(_excluded(listed, "current_sha"))
            continue
        current = _run_payload(api, listed.run_id, policy)
        if not _same_native_identity(listed, current) or current.status != "queued" or current.conclusion is not None:
            raise RecoveryError("candidate_identity_or_status_changed")
        if current.run_attempt != 1:
            excluded.append(_excluded(current, "rerun"))
            continue
        if not _compare_ancestor(api, controller.identity.head_sha, current.head_sha, policy):
            excluded.append(_excluded(current, "non_ancestor"))
            continue
        workflow = _content_bytes(api, current.head_sha, policy)
        try:
            _yaml_group(workflow, expected_group=policy["concurrency_group"])
        except RecoveryError as exc:
            if exc.code == "workflow_yaml_parser_unavailable":
                raise
            excluded.append(_excluded(current, "workflow_group_mismatch"))
            continue
        jobs = _job_list_complete(api, current, policy)
        if jobs:
            excluded.append(_excluded(current, "has_jobs"))
            continue
        candidates.append((current, hashlib.sha256(workflow).hexdigest()))
    candidates.sort(key=lambda pair: (_parse_utc(pair[0].created_at, "candidate_created_at"), pair[0].run_id))
    return candidates


def _revalidate_target(
    api: GitHubApi,
    *,
    target: RunIdentity,
    controller: ControllerAuthority,
    policy: Mapping[str, Any],
    now: dt.datetime,
    target_workflow_sha256: str,
) -> None:
    if target.run_attempt != 1 or target.run_id == controller.identity.run_id:
        raise RecoveryError("target_not_cancelable_identity")
    created = _parse_utc(target.created_at, "target_created_at")
    updated = _parse_utc(target.updated_at, "target_updated_at")
    controller_created = _parse_utc(controller.identity.created_at, "controller_created_at")
    if (
        created > now
        or updated > now
        or created >= controller_created
        or updated >= controller_created
        or now - created < dt.timedelta(seconds=policy["minimum_age_seconds"])
        or now - updated < dt.timedelta(seconds=policy["minimum_age_seconds"])
        or target.head_sha == controller.identity.head_sha
    ):
        raise RecoveryError("target_age_or_source_no_longer_eligible")
    current = _run_payload(api, target.run_id, policy)
    if not _same_native_identity(target, current) or current.status != "queued" or current.conclusion is not None:
        raise RecoveryError("target_status_or_identity_changed")
    if current.run_attempt != 1:
        raise RecoveryError("target_attempt_changed")
    if not _compare_ancestor(api, controller.identity.head_sha, target.head_sha, policy):
        raise RecoveryError("target_is_not_strict_ancestor")
    raw = _content_bytes(api, target.head_sha, policy)
    _yaml_group(raw, expected_group=policy["concurrency_group"])
    if hashlib.sha256(raw).hexdigest() != target_workflow_sha256:
        raise RecoveryError("target_workflow_source_changed")
    jobs = _job_list_complete(api, target, policy)
    if jobs:
        raise RecoveryError("target_has_started_jobs")


def _authenticate_current_and_target(
    api: GitHubApi,
    *,
    env: Mapping[str, str],
    expected_env: Mapping[str, Any],
    target: RunIdentity,
    authority: ControllerAuthority,
    policy: Mapping[str, Any],
    policy_sha256: str,
    root: pathlib.Path,
    now: dt.datetime,
    target_workflow_sha256: str,
) -> ControllerAuthority:
    refreshed = _authenticate_controller(
        api,
        env=env,
        expected_env=expected_env,
        policy=policy,
        policy_sha256=policy_sha256,
        root=root,
        previous=authority,
    )
    if not _same_run_identity(refreshed.identity, authority.identity):
        raise RecoveryError("controller_identity_changed")
    _revalidate_target(
        api,
        target=target,
        controller=refreshed,
        policy=policy,
        now=now,
        target_workflow_sha256=target_workflow_sha256,
    )
    return refreshed


def _reservation_record(
    *,
    target: RunIdentity,
    controller: ControllerAuthority,
    target_workflow_sha256: str,
    reserved_at: dt.datetime,
) -> dict[str, Any]:
    return {
        "schema_version": "datapan.canonical-stale-c-queue-reservation.v1",
        "target": target.reservation_identity(),
        "phase": "reserved",
        "reserved_at": _iso_utc(reserved_at),
        "controller": controller.identity.controller_identity(),
        "evidence": {
            "default_branch_sha": controller.identity.head_sha,
            "concurrency_group": "canonical-update-promotion",
            "policy_sha256": controller.policy_sha256,
            "controller_workflow_sha256": controller.workflow_sha256,
            "target_workflow_sha256": target_workflow_sha256,
        },
        "outcome": None,
    }


def _readback(
    api: GitHubApi,
    *,
    target: RunIdentity,
    policy: Mapping[str, Any],
    observations: list[dict[str, Any]],
    now_fn: Callable[[], dt.datetime],
    monotonic: Callable[[], float],
) -> tuple[bool, str, dict[str, Any] | None]:
    deadline = min(
        api.budget.deadline,
        monotonic() + policy["terminal_observation_window_seconds"],
    )
    last: dict[str, Any] | None = None
    with api.deadline_scope(deadline):
        for index in range(policy["terminal_observations"]):
            if monotonic() >= deadline:
                return False, "terminal_readback_window_exhausted", last
            try:
                current = _run_payload(api, target.run_id, policy)
            except RecoveryError as exc:
                return False, exc.code, last
            job_count: int | None = None
            jobs: list[dict[str, Any]] | None = None
            try:
                # Always prove the originally reserved attempt, even if the
                # native run now exposes a later attempt after a rerun.
                jobs = _jobs(api, target.run_id, target.run_attempt, policy)
                job_count = len(jobs)
            except RecoveryError as exc:
                last = {
                    "observed_at": _iso_utc(now_fn()),
                    "run_id": target.run_id,
                    "run_attempt": current.run_attempt,
                    "status": current.status if current.status in {"queued", "in_progress", "completed", "waiting", "requested", "pending"} else "unknown",
                    "conclusion": current.conclusion,
                    "head_sha": current.head_sha,
                    "job_count": None,
                }
                observations.append(last)
                return False, exc.code, last
            last = {
                "observed_at": _iso_utc(now_fn()),
                "run_id": target.run_id,
                "run_attempt": current.run_attempt,
                "status": current.status if current.status in {"queued", "in_progress", "completed", "waiting", "requested", "pending"} else "unknown",
                "conclusion": current.conclusion,
                "head_sha": current.head_sha,
                "job_count": job_count,
            }
            observations.append(last)
            if current.run_attempt != target.run_attempt:
                return False, "target_new_attempt_observed", last
            if not _same_run_identity(target, current):
                return False, "target_native_identity_changed", last
            if current.status == "completed":
                if current.conclusion == "cancelled" and jobs is not None and not jobs:
                    return True, "native_cancel_confirmed", last
                return False, "non_cancel_terminal_or_jobs_present", last
            if current.status != "queued":
                return False, "target_started_or_waiting", last
            if index + 1 < policy["terminal_observations"]:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False, "terminal_readback_window_exhausted", last
                api.sleep(min(READBACK_SLEEP_SECONDS, remaining))
    return False, "terminal_readback_exhausted", last


def _outcome_from_observation(
    *, request_disposition: str, observation: dict[str, Any] | None, count: int,
) -> dict[str, Any] | None:
    if observation is None:
        return None
    return {
        "request_disposition": request_disposition,
        "observed_at": observation["observed_at"],
        "run_status": observation["status"],
        "conclusion": observation["conclusion"],
        "job_count": observation["job_count"],
        "observation_count": count,
    }


def _existing_reservation_readback(
    api: GitHubApi,
    *,
    state: Any,
    snapshot: Any,
    record: Mapping[str, Any],
    policy: Mapping[str, Any],
    receipt: dict[str, Any],
    now_fn: Callable[[], dt.datetime],
    monotonic: Callable[[], float],
) -> str:
    receipt["request_disposition"] = "unknown_after_restart"
    try:
        target_workflow_sha256 = queue_state.reservation_target_workflow_sha256(record)
    except queue_state.QueueStateError:
        # A reservation without its original immutable workflow authority is
        # not enough to confirm a cancellation or produce a green receipt.
        # Leave it untouched and never issue another native cancellation.
        receipt["reservation_state"] = "uncertain"
        receipt["blocked_reason"] = "reservation_target_workflow_proof_missing"
        receipt["outcome"] = "blocked"
        return "blocked"
    receipt["target_workflow_sha256"] = target_workflow_sha256
    prior_outcome = record.get("outcome")
    if isinstance(prior_outcome, Mapping):
        receipt["request_disposition"] = prior_outcome["request_disposition"]
    else:
        receipt["request_disposition"] = "unknown_after_restart"
    target = record["target"]
    identity = RunIdentity(
        repository_id=target["repository_id"],
        repository=target["repository"],
        workflow_id=target["workflow_id"],
        run_id=target["run_id"],
        run_attempt=target["run_attempt"],
        event=target["event"],
        branch=target["branch"],
        head_sha=target["head_sha"],
        status="queued",
        conclusion=None,
        created_at=target["created_at"],
        updated_at=target["updated_at"],
    )
    confirmed, reason, final = _readback(
        api,
        target=identity,
        policy=policy,
        observations=receipt["observations"],
        now_fn=now_fn,
        monotonic=monotonic,
    )
    receipt["target"] = {key: value for key, value in target.items() if key != "repository" and key != "workflow_path"}
    receipt["reservation_state"] = "existing"
    if confirmed and record["phase"] != "native_cancel_confirmed":
        outcome = _outcome_from_observation(
            request_disposition=receipt["request_disposition"],
            observation=final,
            count=len(receipt["observations"]),
        )
        try:
            latest = state.read_snapshot()
            state.record_outcome(latest, target, phase="native_cancel_confirmed", outcome=outcome)
        except Exception:
            receipt["reservation_state"] = "uncertain"
            receipt["blocked_reason"] = "terminal_record_conflict_or_uncertain"
            return "blocked"
        receipt["reservation_state"] = "confirmed"
        receipt["outcome"] = "native_cancel_confirmed"
        receipt["blocked_reason"] = None
        return "native_cancel_confirmed"
    if confirmed:
        receipt["reservation_state"] = "confirmed"
        receipt["outcome"] = "native_cancel_confirmed"
        return "native_cancel_confirmed"
    receipt["blocked_reason"] = reason
    receipt["outcome"] = "reserved_reconcile_only"
    return "reserved_reconcile_only"


def perform_recovery(
    *,
    root: pathlib.Path,
    env: Mapping[str, str],
    policy: Mapping[str, Any],
    policy_sha256: str,
    api: GitHubApi,
    state_factory: Callable[[float], Any],
    now_fn: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
    receipt: dict[str, Any],
) -> str:
    if env.get("GITHUB_EVENT_NAME") != "schedule":
        receipt["outcome"] = "no_op_not_schedule"
        return "no_op_not_schedule"
    receipt["policy_sha256"] = policy_sha256
    expected_env = _repository_context(env)
    workflow_id = _workflow_id_from_run(api, expected_env, policy)
    expected_env = {**expected_env, "workflow_id": workflow_id}
    now = now_fn().astimezone(dt.timezone.utc)
    authority = _authenticate_controller(
        api,
        env=env,
        expected_env=expected_env,
        policy=policy,
        policy_sha256=policy_sha256,
        root=root,
    )
    receipt["controller"] = {
        "repository_id": authority.repository_id,
        "workflow_id": authority.workflow_id,
        "run_id": authority.identity.run_id,
        "run_attempt": authority.identity.run_attempt,
        "event": authority.identity.event,
        "branch": authority.identity.branch,
        "head_sha": authority.identity.head_sha,
        "job_name": expected_env["job_name"],
    }
    receipt["controller_workflow_sha256"] = authority.workflow_sha256
    try:
        state_value = state_factory(api.budget.deadline)
    except RecoveryError:
        raise
    except Exception as exc:
        raise RecoveryError("queue_state_store_unavailable") from exc
    if isinstance(state_value, tuple) and len(state_value) == 2:
        git_store, state = state_value
    else:
        git_store, state = None, state_value
    try:
        try:
            snapshot = state.read_snapshot()
            unresolved = state.unresolved_reservations(snapshot)
        except Exception as exc:
            raise RecoveryError("queue_state_snapshot_unavailable") from exc
        if len(unresolved) > 1:
            raise RecoveryError("multiple_unresolved_queue_reservations")
        if unresolved:
            return _existing_reservation_readback(
                api,
                state=state,
                snapshot=snapshot,
                record=unresolved[0],
                policy=policy,
                receipt=receipt,
                now_fn=now_fn,
                monotonic=monotonic,
            )
        candidates = _discover_candidates(
            api,
            controller=authority,
            policy=policy,
            now=now,
            excluded=receipt["excluded_targets"],
        )
        if not candidates:
            receipt["outcome"] = "no_eligible_target"
            return "no_eligible_target"
        selected, target_digest = candidates[0]
        receipt["target"] = {
            "repository_id": selected.repository_id,
            "workflow_id": selected.workflow_id,
            "run_id": selected.run_id,
            "run_attempt": selected.run_attempt,
            "event": selected.event,
            "branch": selected.branch,
            "head_sha": selected.head_sha,
            "created_at": selected.created_at,
            "updated_at": selected.updated_at,
        }
        receipt["target_workflow_sha256"] = target_digest
        existing = state.find_reservation(snapshot, selected.reservation_identity())
        if existing is not None:
            return _existing_reservation_readback(
                api,
                state=state,
                snapshot=snapshot,
                record=existing,
                policy=policy,
                receipt=receipt,
                now_fn=now_fn,
                monotonic=monotonic,
            )
        now = now_fn().astimezone(dt.timezone.utc)
        refreshed_authority = _authenticate_current_and_target(
            api,
            env=env,
            expected_env=expected_env,
            target=selected,
            authority=authority,
            policy=policy,
            policy_sha256=policy_sha256,
            root=root,
            now=now,
            target_workflow_sha256=target_digest,
        )
        record = _reservation_record(
            target=selected,
            controller=refreshed_authority,
            target_workflow_sha256=target_digest,
            reserved_at=now,
        )
        try:
            state.reserve(snapshot, record)
        except Exception as exc:
            receipt["reservation_state"] = "uncertain"
            name = type(exc).__name__
            receipt["blocked_reason"] = (
                "reservation_conflict" if name == "QueueStateConflict"
                else "reservation_outcome_uncertain" if name == "QueueStateOutcomeUncertain"
                else "reservation_write_failed"
            )
            receipt["outcome"] = "blocked"
            return "blocked"
        receipt["reservation_state"] = "created"
        receipt["outcome"] = "reservation_created"
        # A fresh second proof closes the reservation-to-effect interval. If it
        # fails, the permanent reservation grants readback only on later runs.
        try:
            now = now_fn().astimezone(dt.timezone.utc)
            refreshed_authority = _authenticate_current_and_target(
                api,
                env=env,
                expected_env=expected_env,
                target=selected,
                authority=refreshed_authority,
                policy=policy,
                policy_sha256=policy_sha256,
                root=root,
                now=now,
                target_workflow_sha256=target_digest,
            )
        except RecoveryError as exc:
            receipt["blocked_reason"] = f"reserved_{exc.code}"[:96]
            receipt["outcome"] = "blocked"
            return "blocked"
        try:
            request_disposition = api.post_cancel(selected.run_id)
        except RecoveryError as exc:
            request_disposition = "transport_ambiguous" if api.cancel_post_started else "not_attempted"
            receipt["request_disposition"] = request_disposition
            if request_disposition == "not_attempted":
                receipt["blocked_reason"] = f"cancel_not_attempted_{exc.code}"[:96]
        else:
            receipt["request_disposition"] = request_disposition
        confirmed, reason, final = _readback(
            api,
            target=selected,
            policy=policy,
            observations=receipt["observations"],
            now_fn=now_fn,
            monotonic=monotonic,
        )
        outcome = _outcome_from_observation(
            request_disposition=receipt["request_disposition"],
            observation=final,
            count=len(receipt["observations"]),
        )
        if outcome is not None:
            try:
                latest = state.read_snapshot()
                state.record_outcome(
                    latest,
                    selected.reservation_identity(),
                    phase="native_cancel_confirmed" if confirmed else "unresolved",
                    outcome=outcome,
                )
            except Exception:
                receipt["reservation_state"] = "uncertain"
                receipt["blocked_reason"] = "terminal_record_conflict_or_uncertain"
                receipt["outcome"] = "blocked"
                return "blocked"
        if confirmed:
            receipt["reservation_state"] = "confirmed"
            receipt["outcome"] = "native_cancel_confirmed"
            receipt["blocked_reason"] = None
            return "native_cancel_confirmed"
        receipt["reservation_state"] = "created"
        receipt["blocked_reason"] = reason
        receipt["outcome"] = "reserved_reconcile_only"
        return "reserved_reconcile_only"
    finally:
        if git_store is not None:
            try:
                git_store.close()
            except Exception:
                pass


def _blank_receipt(started_at: dt.datetime) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA,
        "repository": REPOSITORY,
        "workflow_path": WORKFLOW_PATH,
        "outcome": "blocked",
        "blocked_reason": None,
        "controller": {
            "repository_id": None,
            "workflow_id": None,
            "run_id": None,
            "run_attempt": None,
            "event": None,
            "branch": None,
            "head_sha": None,
            "job_name": None,
        },
        "target": None,
        "reservation_state": "not_created",
        "request_disposition": "not_attempted",
        "policy_sha256": None,
        "controller_workflow_sha256": None,
        "target_workflow_sha256": None,
        "observations": [],
        "excluded_targets": [],
        "api_requests": 0,
        "elapsed_ms": 0,
        "started_at": _iso_utc(started_at),
        "finished_at": _iso_utc(started_at),
    }


def _receipt_path(root: pathlib.Path, env: Mapping[str, str]) -> pathlib.Path:
    run_id = env.get("GITHUB_RUN_ID", "")
    attempt = env.get("GITHUB_RUN_ATTEMPT", "")
    if not run_id.isascii() or not run_id.isdigit() or not attempt.isascii() or not attempt.isdigit():
        run_part, attempt_part = "unverified", "unverified"
    else:
        run_part, attempt_part = str(int(run_id)), str(int(attempt))
    return root / ".datapan/ci/canonical-stale-c-queue-recovery" / run_part / attempt_part / "receipt.json"


def _write_receipt(root: pathlib.Path, env: Mapping[str, str], receipt: dict[str, Any]) -> None:
    path = _receipt_path(root, env)
    schema_path = root / RECEIPT_SCHEMA_PATH
    try:
        if schema_path.stat().st_size > 65536:
            raise RecoveryError("receipt_schema_size_out_of_bounds")
        schema_raw = schema_path.read_bytes()
        schema = json.loads(schema_raw.decode("utf-8"))
        import jsonschema
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker(),
        )
        validator.check_schema(schema)
        validator.validate(receipt)
    except RecoveryError:
        raise
    except Exception as exc:
        raise RecoveryError("receipt_schema_validation_failed") from exc
    root_resolved = root.resolve()
    current = root_resolved
    relative_parent = path.parent.relative_to(root)
    for component in relative_parent.parts:
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if current.is_symlink() or not current.is_dir():
            raise RecoveryError("receipt_directory_not_private")
    if not path.parent.resolve().is_relative_to(root_resolved):
        raise RecoveryError("receipt_path_escaped_checkout")
    raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def _main(argv: list[str] | None = None) -> int:
    if argv:
        print("This recovery has no caller-controlled command-line options.", file=sys.stderr)
        return 2
    root = pathlib.Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    started_monotonic = time.monotonic()
    started_at = dt.datetime.now(dt.timezone.utc)
    receipt = _blank_receipt(started_at)
    policy: dict[str, Any] | None = None
    api: GitHubApi | None = None
    try:
        policy, _policy_raw, policy_sha256 = _parse_policy(root)
        receipt["policy_sha256"] = policy_sha256
        deadline = started_monotonic + policy["maximum_runtime_seconds"]
        if env.get("GITHUB_EVENT_NAME") != "schedule":
            outcome = "no_op_not_schedule"
            receipt["outcome"] = outcome
        else:
            api = GitHubApi(
                env.get("GITHUB_TOKEN", ""),
                policy=policy,
                deadline=deadline,
            )
            def state_factory(original_deadline: float):
                try:
                    from canonical_stale_queue_state import create_hardened_store
                except ImportError as exc:
                    raise RecoveryError("queue_state_adapter_unavailable") from exc
                try:
                    return create_hardened_store(
                        repository_root=str(root),
                        token=env.get("GITHUB_TOKEN", ""),
                        deadline_monotonic=original_deadline,
                    )
                except Exception as exc:
                    if isinstance(exc, RecoveryError):
                        raise
                    raise RecoveryError("queue_state_store_unavailable") from exc
            outcome = perform_recovery(
                root=root,
                env=env,
                policy=policy,
                policy_sha256=policy_sha256,
                api=api,
                state_factory=state_factory,
                receipt=receipt,
            )
    except RecoveryError as exc:
        outcome = "blocked"
        receipt["outcome"] = "blocked"
        receipt["blocked_reason"] = exc.code
    except Exception:
        outcome = "blocked"
        receipt["outcome"] = "blocked"
        receipt["blocked_reason"] = "unexpected_internal_failure"
    finished = dt.datetime.now(dt.timezone.utc)
    receipt["finished_at"] = _iso_utc(finished)
    receipt["elapsed_ms"] = max(0, int((time.monotonic() - started_monotonic) * 1000))
    receipt["api_requests"] = 0 if api is None else api.budget.requests
    try:
        _write_receipt(root, env, receipt)
    except Exception:
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0 if outcome in {"no_op_not_schedule", "no_eligible_target", "native_cancel_confirmed"} else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
