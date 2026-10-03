#!/usr/bin/env python3
"""Build a read-only, explicit-time health receipt for the upstream catalogue pipeline."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import urllib.parse
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKPOINT_SCHEMA = ROOT / "schemas" / "datapan.upstream-catalogue-checkpoint.v1.schema.json"
HEALTH_POLICY_SCHEMA = ROOT / "schemas" / "datapan.upstream-catalogue-health-policy.v1.schema.json"
HEALTH_RECEIPT_SCHEMA = ROOT / "schemas" / "datapan.upstream-catalogue-health.v1.schema.json"
PROMOTION_SCHEMA = ROOT / "schemas" / "datapan.canonical-update-promotion-receipt.v1.schema.json"
PROMOTION_JOURNAL_SCHEMA = ROOT / "schemas" / "datapan.canonical-update-promotion-journal.v1.schema.json"
SOURCE_POLICY_DEFAULT = pathlib.Path("policy/source-refresh.json")
HEALTH_POLICY_DEFAULT = pathlib.Path("policy/upstream-catalogue-health.json")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
REVISION = re.compile(r"^[a-f0-9]{40,64}$")
MAX_PROMOTION_JOB_PAGES = 5
MAX_PROMOTION_JOBS = 500
PROCESSOR_OUTPUT_PATHS = (
    "composed-candidate.registry.json",
    "ready-scope.registry.json",
    "semantic-diff.json",
    "regeneration-queue.json",
    "quarantine.json",
    "composition-receipt.json",
    "upstream-catalogue-enrichment-evidence.json",
    "upstream-catalogue-processing-result.json",
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_registry_identity(manifest_path: pathlib.Path, registry_path: pathlib.Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    try:
        registry_name = registry_path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("main_registry_path_outside_repository") from exc
    matches = [
        row for row in artifacts or []
        if isinstance(row, dict) and row.get("path") == registry_name and row.get("kind") == "registry"
    ]
    if len(matches) != 1:
        raise ValueError("main_registry_manifest_identity_missing")
    entry = matches[0]
    expected_bytes = entry.get("bytes")
    expected_sha256 = entry.get("sha256")
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 1 or not DIGEST.fullmatch(str(expected_sha256 or "")):
        raise ValueError("main_registry_manifest_identity_invalid")
    try:
        raw = registry_path.read_bytes()
    except OSError as exc:
        raise ValueError("main_registry_pointer_unavailable") from exc
    if raw.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        try:
            lines = raw.decode("ascii").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError("main_registry_lfs_pointer_invalid") from exc
        if len(lines) != 3 or not re.fullmatch(r"oid sha256:[a-f0-9]{64}", lines[1]) or not re.fullmatch(r"size [0-9]+", lines[2]):
            raise ValueError("main_registry_lfs_pointer_invalid")
        actual_sha256 = lines[1].removeprefix("oid sha256:")
        actual_bytes = int(lines[2].removeprefix("size "))
    else:
        actual_sha256 = sha256_bytes(raw)
        actual_bytes = len(raw)
    if actual_sha256 != expected_sha256 or actual_bytes != expected_bytes:
        raise ValueError("main_registry_manifest_mismatch")
    return {"registry_path": registry_name, "registry_bytes": actual_bytes, "registry_sha256": actual_sha256}


def load_json(path: pathlib.Path, *, maximum_bytes: int = 4 * 1024 * 1024) -> Any:
    if path.stat().st_size > maximum_bytes:
        raise ValueError(f"input_too_large:{path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid_timestamp:{label}")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid_timestamp:{label}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timezone_required:{label}")
    return parsed.astimezone(dt.timezone.utc)


def utc_timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def seconds_since(now: dt.datetime, value: Any, label: str, maximum_future_skew: int) -> int:
    parsed = parse_time(value, label)
    delta = (now - parsed).total_seconds()
    if delta < -maximum_future_skew:
        raise ValueError(f"future_timestamp:{label}")
    return max(0, int(delta))


def cadence_interval_seconds(cron: str) -> int:
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError("source_cadence_unsupported")
    minute, hour, day_of_month, month, day_of_week = fields
    if not minute.isdigit() or not hour.isdigit() or day_of_month != "*" or month != "*":
        raise ValueError("source_cadence_unsupported")
    if not 0 <= int(minute) <= 59 or not 0 <= int(hour) <= 23:
        raise ValueError("source_cadence_unsupported")
    if day_of_week == "*":
        return 24 * 60 * 60
    if day_of_week.isdigit() and 0 <= int(day_of_week) <= 6:
        return 7 * 24 * 60 * 60
    raise ValueError("source_cadence_unsupported")


def fault(
    source_id: str, stage: str, reason: str, severity: str, owner_ticket: int, action: str,
    failure_identity: str = "",
) -> dict[str, Any]:
    key_material = {
        "source_id": source_id, "stage": stage, "reason": reason,
        "owner_ticket": owner_ticket, "failure_identity": failure_identity,
    }
    return {
        "source_id": source_id,
        "stage": stage,
        "reason": reason,
        "severity": severity,
        "owner_ticket": owner_ticket,
        "fault_key": sha256_bytes(canonical_json(key_material)),
        "recommended_action": action,
    }


def seal_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    receipt["receipt_sha256"] = sha256_bytes(canonical_json(unsigned))
    return receipt


def verify_sealed(value: Any, digest_field: str) -> bool:
    if not isinstance(value, dict):
        return False
    claimed = value.get(digest_field)
    if not isinstance(claimed, str) or not DIGEST.fullmatch(claimed):
        return False
    unsigned = dict(value)
    unsigned.pop(digest_field, None)
    return claimed == sha256_bytes(canonical_json(unsigned))


def gh_json(endpoint: str) -> Any:
    result = subprocess.run(
        ["gh", "api", endpoint], text=True, capture_output=True, check=False, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("github_api_unavailable")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("github_api_invalid_json") from exc


def collect_workflow_runs(repository: str, workflow_path: str, limit: int) -> list[dict[str, Any]]:
    workflow_filename = pathlib.PurePosixPath(workflow_path).name
    if not workflow_filename.endswith((".yml", ".yaml")):
        raise RuntimeError("collector_workflow_filename_invalid")
    payload = gh_json(f"repos/{repository}/actions/workflows/{workflow_filename}/runs?per_page={limit}")
    rows = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("github_workflow_runs_missing")
    return [row for row in rows if isinstance(row, dict)]


def collect_workflow_identity(repository: str, workflow_path: str) -> int:
    """Resolve the configured workflow path to GitHub's authoritative workflow ID."""
    workflow_filename = pathlib.PurePosixPath(workflow_path).name
    if not workflow_filename.endswith((".yml", ".yaml")):
        raise RuntimeError("collector_workflow_filename_invalid")
    payload = gh_json(f"repos/{repository}/actions/workflows/{workflow_filename}")
    workflow_id = payload.get("id") if isinstance(payload, dict) else None
    if (
        isinstance(workflow_id, bool)
        or not isinstance(workflow_id, int)
        or workflow_id < 1
        or not isinstance(payload.get("path"), str)
        or not workflow_path_matches(payload["path"], workflow_path)
    ):
        raise RuntimeError("github_workflow_identity_invalid")
    return workflow_id


def collect_run_artifacts(repository: str, run_id: str) -> list[dict[str, Any]]:
    if not run_id.isdigit():
        return []
    payload = gh_json(f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100")
    rows = payload.get("artifacts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("github_run_artifacts_missing")
    return [row for row in rows if isinstance(row, dict)]


def collect_run(repository: str, run_id: str) -> dict[str, Any]:
    if not run_id.isdigit():
        raise RuntimeError("github_workflow_run_id_invalid")
    payload = gh_json(f"repos/{repository}/actions/runs/{run_id}")
    if not isinstance(payload, dict):
        raise RuntimeError("github_workflow_run_missing")
    return payload


def collect_run_attempt(repository: str, run_id: str, run_attempt: int) -> dict[str, Any]:
    if not run_id.isdigit() or isinstance(run_attempt, bool) or run_attempt < 1:
        raise RuntimeError("github_workflow_run_attempt_invalid")
    payload = gh_json(f"repos/{repository}/actions/runs/{run_id}/attempts/{run_attempt}")
    if not isinstance(payload, dict):
        raise RuntimeError("github_workflow_run_attempt_missing")
    return payload


def collect_run_attempt_jobs(repository: str, run_id: str, run_attempt: int) -> dict[str, Any]:
    """Fetch a bounded, exact-attempt job list for stable completion ordering."""
    if not run_id.isdigit() or isinstance(run_attempt, bool) or run_attempt < 1:
        raise RuntimeError("github_workflow_run_attempt_invalid")
    jobs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    expected_count: int | None = None
    endpoint = f"repos/{repository}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
    for page in range(1, MAX_PROMOTION_JOB_PAGES + 1):
        payload = gh_json(f"{endpoint}?per_page=100&page={page}")
        rows = payload.get("jobs") if isinstance(payload, dict) else None
        total_count = payload.get("total_count") if isinstance(payload, dict) else None
        if (
            not isinstance(rows, list)
            or isinstance(total_count, bool)
            or not isinstance(total_count, int)
            or total_count < 1
            or total_count > MAX_PROMOTION_JOBS
        ):
            raise RuntimeError("github_workflow_attempt_jobs_invalid")
        if expected_count is None:
            expected_count = total_count
        elif total_count != expected_count:
            raise RuntimeError("github_workflow_attempt_jobs_count_changed")
        if not rows and len(jobs) < expected_count:
            raise RuntimeError("github_workflow_attempt_jobs_incomplete")
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("github_workflow_attempt_job_invalid")
            job_id = row.get("id")
            if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1 or str(job_id) in seen_ids:
                raise RuntimeError("github_workflow_attempt_job_identity_invalid")
            seen_ids.add(str(job_id))
            jobs.append(row)
        if len(jobs) > expected_count:
            raise RuntimeError("github_workflow_attempt_jobs_excess")
        if len(jobs) == expected_count:
            return {
                "attempt_number": run_attempt,
                "jobs_api_endpoint": endpoint,
                "job_count": expected_count,
                "jobs": jobs,
            }
    raise RuntimeError("github_workflow_attempt_jobs_page_limit_exceeded")


def collect_run_attempt_evidence(repository: str, run_id: str, run_attempt: int) -> dict[str, Any]:
    run = collect_run_attempt(repository, run_id, run_attempt)
    jobs = collect_run_attempt_jobs(repository, run_id, run_attempt)
    return {"run": run, **jobs}


def collect_artifact(repository: str, artifact_id: str) -> dict[str, Any] | None:
    if not artifact_id.isdigit():
        return None
    payload = gh_json(f"repos/{repository}/actions/artifacts/{artifact_id}")
    return payload if isinstance(payload, dict) else None


def load_processor_checkpoints(
    state_dir: pathlib.Path, source_id: str, max_bytes: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load only generation files named by that source's processor index."""
    index_path = state_dir / "sources" / source_id / "index.json"
    if not index_path.exists():
        return [], ["processor_index_missing"]
    issues: list[str] = []
    try:
        index = load_json(index_path, maximum_bytes=max_bytes)
    except (OSError, ValueError, json.JSONDecodeError):
        return [], ["processor_index_corrupt"]
    if not isinstance(index, dict) or index.get("schema_version") != "datapan.upstream-catalogue-checkpoint.v1" or not isinstance(index.get("generations"), list):
        return [], ["processor_index_corrupt"]
    checkpoints: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in index["generations"]:
        if not isinstance(row, dict):
            issues.append("processor_index_corrupt")
            continue
        generation_id = row.get("generation_id")
        name = row.get("checkpoint")
        if not isinstance(generation_id, str) or not DIGEST.fullmatch(generation_id) or name != f"{generation_id}.json" or generation_id in seen:
            issues.append("processor_index_corrupt")
            continue
        seen.add(generation_id)
        path = state_dir / "sources" / source_id / "generations" / name
        if not path.exists():
            issues.append("checkpoint_missing")
            continue
        try:
            value = load_json(path, maximum_bytes=max_bytes)
        except (OSError, ValueError, json.JSONDecodeError):
            issues.append("checkpoint_corrupt")
            continue
        if not isinstance(value, dict) or value.get("generation_id") != generation_id or not verify_sealed(value, "checkpoint_sha256"):
            issues.append("checkpoint_corrupt")
            continue
        if CHECKPOINT_SCHEMA.exists():
            try:
                import jsonschema

                jsonschema.Draft202012Validator(load_json(CHECKPOINT_SCHEMA)).validate(value)
            except ImportError:
                issues.append("checkpoint_schema_dependency_missing")
                continue
            except Exception:
                issues.append("checkpoint_corrupt")
                continue
        checkpoints.append(value)
    return checkpoints, list(dict.fromkeys(issues))


def latest_observation(checkpoints: list[dict[str, Any]], now: dt.datetime, future_skew: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    observations: list[tuple[dt.datetime, dict[str, Any], dict[str, Any]]] = []
    for checkpoint in checkpoints:
        observation = checkpoint.get("last_observation")
        if not isinstance(observation, dict):
            continue
        try:
            observed_at = parse_time(observation.get("observed_at"), "last_observation.observed_at")
            seconds_since(now, observation.get("observed_at"), "last_observation.observed_at", future_skew)
        except ValueError as exc:
            return None, checkpoint, str(exc)
        observations.append((observed_at, observation, checkpoint))
    if not observations:
        return None, None, None
    _, observation, checkpoint = max(observations, key=lambda item: item[0])
    return observation, checkpoint, None


def workflow_run_order(run: dict[str, Any]) -> tuple[dt.datetime, int]:
    try:
        created = parse_time(run.get("created_at"), "workflow_run.created_at")
    except ValueError:
        created = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    try:
        identifier = int(run.get("id", 0))
    except (TypeError, ValueError):
        identifier = 0
    return created, identifier


def workflow_path_matches(actual: Any, expected: str) -> bool:
    if not isinstance(actual, str):
        return False
    return actual in {expected, f"{expected}@main", f"{expected}@refs/heads/main"}


def run_matches_workflow(run: dict[str, Any], expected: str) -> bool:
    return workflow_path_matches(run.get("path"), expected)


def trusted_main_workflow_run(
    run: dict[str, Any], repository: str, workflow_path: str, workflow_id: int | None,
    allowed_events: set[str] | None,
) -> bool:
    """Require authoritative run metadata to bind workflow, repository, ref, and commit."""
    actual_workflow_id = run.get("workflow_id")
    head_repository = run.get("head_repository")
    run_repository = run.get("repository")
    run_id = run.get("id")
    return bool(
        isinstance(workflow_id, int)
        and not isinstance(workflow_id, bool)
        and workflow_id > 0
        and isinstance(run_id, int)
        and not isinstance(run_id, bool)
        and run_id > 0
        and isinstance(actual_workflow_id, int)
        and not isinstance(actual_workflow_id, bool)
        and actual_workflow_id == workflow_id
        and run_matches_workflow(run, workflow_path)
        and isinstance(run.get("event"), str)
        and bool(run.get("event"))
        and (allowed_events is None or run.get("event") in allowed_events)
        and run.get("head_branch") == "main"
        and isinstance(run_repository, dict)
        and str(run_repository.get("full_name", "")).casefold() == repository.casefold()
        and isinstance(head_repository, dict)
        and str(head_repository.get("full_name", "")).casefold() == repository.casefold()
        and isinstance(run.get("head_sha"), str)
        and REVISION.fullmatch(run["head_sha"])
    )


def report_workflow_run(run: dict[str, Any], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(artifacts, list):
        artifacts = []
    return {
        "run_id": str(run.get("id", "")),
        "event": str(run.get("event", "unknown")),
        "status": str(run.get("status", "unknown")),
        "conclusion": str(run.get("conclusion") or "pending"),
        "created_at": str(run.get("created_at", "")),
        "updated_at": str(run.get("updated_at", "")),
        "head_branch": str(run.get("head_branch", "")),
        "head_sha": str(run.get("head_sha", "")),
        "artifacts": [
            {
                "artifact_id": str(row.get("id", "")),
                "name": str(row.get("name", "")),
                "expired": bool(row.get("expired", False)),
                "expires_at": str(row.get("expires_at", "")),
            }
            for row in artifacts
        ],
    }


def _fault_action(source: dict[str, Any], key: str) -> str:
    actions = source.get("recovery_commands", {})
    return str(actions.get(key, "Inspect the health receipt and preserve the last-good canonical identity."))


def evaluate_source(
    *, source: dict[str, Any], repository: str, refresh_source: dict[str, Any] | None, policy_sha256: str,
    as_of: dt.datetime, workflow_runs: list[dict[str, Any]], artifacts_by_run: dict[str, list[dict[str, Any]]],
    state_dir: pathlib.Path, artifact_by_id: dict[str, dict[str, Any]],
    promotion_ack: dict[str, Any] | None, main_identity: dict[str, Any], last_good: dict[str, Any] | None,
    mode: str, maximum_future_skew: int, max_checkpoint_bytes: int,
    producer_runs_by_id: dict[str, dict[str, Any]] | None = None,
    promotion_runs_by_id: dict[str, dict[str, Any]] | None = None,
    promotion_workflow_paths: dict[str, str] | None = None,
    workflow_ids_by_path: dict[str, int] | None = None,
    promotion_ack_error: str | None = None,
    workflow_api_error: str | None = None,
    health_state_error: str | None = None,
) -> dict[str, Any]:
    source_id = str(source["source_id"])
    owner_ticket = int(source["owner_ticket"])
    faults: list[dict[str, Any]] = []

    def add(stage: str, reason: str, severity: str, action: str, failure_identity: str = "") -> None:
        faults.append(fault(source_id, stage, reason, severity, owner_ticket, action, failure_identity))

    if health_state_error:
        add("health-state", "durable_health_state_unavailable", "error", _fault_action(source, "processor_stalled"), health_state_error)
    if promotion_ack_error:
        add("promotion", "promotion_journal_unavailable", "error", _fault_action(source, "promotion_wait"), promotion_ack_error)

    refresh = refresh_source or {}
    collector_path = str(refresh.get("workflow_path") or "")
    collector_workflow_id = (workflow_ids_by_path or {}).get(collector_path)
    matching_runs = [
        row for row in workflow_runs
        if isinstance(row, dict)
        and collector_path
        and trusted_main_workflow_run(
            row, repository, collector_path, collector_workflow_id, {"schedule", "workflow_dispatch"},
        )
    ]
    scheduled_runs = [row for row in matching_runs if row.get("event") == "schedule"]
    scheduled_runs.sort(key=workflow_run_order)
    latest_scheduled_run = scheduled_runs[-1] if scheduled_runs else None
    collector_runs = [row for row in matching_runs if row.get("event") in {"schedule", "workflow_dispatch"}]
    collector_runs.sort(key=workflow_run_order)
    latest_run = collector_runs[-1] if collector_runs else None
    baseline_value = (refresh_source or {}).get("last_successful_observation")
    schedule_anchor = None
    if latest_scheduled_run is not None:
        schedule_anchor = latest_scheduled_run.get("created_at")
    elif baseline_value:
        schedule_anchor = baseline_value
    if workflow_api_error:
        add("collector", "workflow_observations_unavailable", "error", _fault_action(source, "schedule_missing"), workflow_api_error)
        schedule_age = None
    elif schedule_anchor:
        try:
            schedule_age = seconds_since(as_of, schedule_anchor, "collector_schedule_anchor", maximum_future_skew)
        except ValueError as exc:
            add("schedule", str(exc), "error", _fault_action(source, "schedule_missing"))
            schedule_age = 0
        if schedule_age > int(source["expected_interval_seconds"]) + int(source["observation_grace_seconds"]):
            add("collector", "scheduled_execution_missing", "error", _fault_action(source, "schedule_missing"))
    else:
        schedule_age = None
        add("collector", "scheduled_execution_missing", "error", _fault_action(source, "schedule_missing"))

    latest_run_artifacts: list[dict[str, Any]] = []
    latest_scheduled_artifacts = artifacts_by_run.get(str(latest_scheduled_run.get("id", "")), []) if latest_scheduled_run else []
    if latest_run is not None:
        latest_run_artifacts = artifacts_by_run.get(str(latest_run.get("id", "")), [])
        if latest_run.get("status") == "in_progress":
            try:
                run_age = seconds_since(as_of, latest_run.get("run_started_at") or latest_run.get("created_at"), "collector_run.started_at", maximum_future_skew)
                if run_age > int(source["stage_deadlines_seconds"]["collector"]):
                    add("collector", "collector_run_stalled", "error", _fault_action(source, "schedule_missing"))
            except ValueError as exc:
                add("collector", str(exc), "error", _fault_action(source, "schedule_missing"))
        elif latest_run.get("conclusion") not in {"success", None}:
            add("collector", "collector_run_failed", "error", _fault_action(source, "provider_failure"))
        elif latest_run.get("conclusion") == "success":
            expected_artifact = f"upstream-catalog-refresh-{latest_run.get('id')}"
            if isinstance(latest_run_artifacts, dict) and latest_run_artifacts.get("availability_error"):
                add("collector", "collector_artifact_api_unavailable", "error", _fault_action(source, "provider_failure"))
            elif not any(row.get("name") == expected_artifact and not row.get("expired") for row in latest_run_artifacts):
                add("collector", "collector_artifact_missing_or_expired", "error", _fault_action(source, "provider_failure"))

    checkpoints, checkpoint_issues = load_processor_checkpoints(state_dir, source_id, max_checkpoint_bytes)
    for issue in checkpoint_issues:
        stage = "checkpoint" if issue.startswith("checkpoint") or issue == "processor_index_corrupt" else "processor"
        add(stage, issue, "error", _fault_action(source, "processor_stalled"))
    observation, checkpoint, observation_error = latest_observation(checkpoints, as_of, maximum_future_skew)
    observation_age: int | None = None
    if observation_error:
        add("observation", observation_error, "error", _fault_action(source, "provider_failure"))
    if observation is None:
        observation_state = "missing"
        if not any(item["reason"] == "observation_missing" for item in faults):
            add("observation", "observation_missing", "error", _fault_action(source, "provider_failure"))
    elif mode == "fixture" or observation.get("execution_mode") != "live":
        observation_state = "fixture_only"
        add("observation", "fixture_observation_excluded", "error", _fault_action(source, "provider_failure"))
    elif observation.get("collection_status") != "success":
        observation_state = "provider_failure" if observation.get("collection_status") == "failure" else "collection_missing"
        add("observation", observation_state, "error", _fault_action(source, "provider_failure"))
    else:
        producer_id = str(observation.get("producer_run_id") or "")
        producer_run = (producer_runs_by_id or {}).get(producer_id)
        producer_artifacts = artifacts_by_run.get(producer_id, [])
        expected_artifact = f"upstream-catalog-refresh-{producer_id}"
        run_is_trusted = bool(
            producer_run
            and str(producer_run.get("id", "")) == producer_id
            and trusted_main_workflow_run(
                producer_run, repository, collector_path, collector_workflow_id, {"schedule", "workflow_dispatch"},
            )
            and producer_run.get("status") == "completed"
            and producer_run.get("conclusion") == "success"
        )
        artifact_is_trusted = any(
            row.get("name") == expected_artifact and not row.get("expired")
            for row in producer_artifacts if isinstance(row, dict)
        )
        if mode == "live" and not run_is_trusted:
            observation_state = "producer_unverified"
            add("observation", "source_observation_run_unverified", "error", _fault_action(source, "provider_failure"), producer_id)
        elif mode == "live" and not artifact_is_trusted:
            observation_state = "producer_artifact_missing"
            add("observation", "source_observation_artifact_unavailable", "error", _fault_action(source, "provider_failure"), producer_id)
        else:
            try:
                observation_age = seconds_since(as_of, observation.get("observed_at"), "last_observation.observed_at", maximum_future_skew)
                if not DIGEST.fullmatch(str(observation.get("refresh_evidence_sha256") or "")):
                    observation_state = "evidence_digest_missing"
                    add("observation", "observation_evidence_digest_missing", "error", _fault_action(source, "provider_failure"))
                elif observation_age > int(source["max_observation_age_seconds"]):
                    observation_state = "stale"
                    add("observation", "source_observation_stale", "error", _fault_action(source, "schedule_missing"))
                else:
                    observation_state = "fresh"
            except ValueError as exc:
                observation_age = None
                observation_state = "invalid_time"
                if observation_error is None:
                    add("observation", str(exc), "error", _fault_action(source, "provider_failure"))

    if latest_run is not None and latest_run.get("conclusion") == "success" and observation is not None:
        try:
            observation_time = parse_time(observation.get("observed_at"), "last_observation.observed_at")
            completed_at = parse_time(latest_run.get("updated_at") or latest_run.get("created_at"), "collector_run.updated_at")
            collection_age = seconds_since(as_of, completed_at.isoformat(), "collector_run.updated_at", maximum_future_skew)
            processed_run_ids = {
                str(row.get("last_observation", {}).get("producer_run_id"))
                for row in checkpoints if isinstance(row.get("last_observation"), dict)
            }
            if (
                str(latest_run.get("id", "")) not in processed_run_ids
                and observation_time < completed_at
                and collection_age > int(source["stage_deadlines_seconds"]["queued"])
            ):
                add("processor", "successful_collector_not_observed", "error", _fault_action(source, "processor_stalled"))
        except ValueError as exc:
            add("processor", str(exc), "error", _fault_action(source, "processor_stalled"))

    processor_state = "missing" if checkpoint is None else str(checkpoint.get("status", "unknown"))
    generation_id = str(checkpoint.get("generation_id", "")) if checkpoint else ""
    if checkpoint is not None:
        generation_inputs = checkpoint.get("generation_inputs") if isinstance(checkpoint.get("generation_inputs"), dict) else {}
        if policy_sha256 and generation_inputs.get("policy_sha256") != policy_sha256:
            add("processor", "checkpoint_policy_changed", "warning", _fault_action(source, "processor_stalled"))
        status = str(checkpoint.get("status", "unknown"))
        outcome = checkpoint.get("outcome") if isinstance(checkpoint.get("outcome"), dict) else {}
        if status in {"queued", "validating", "enriching", "composing", "retry"}:
            try:
                progress_age = seconds_since(as_of, checkpoint.get("last_progress_at"), "checkpoint.last_progress_at", maximum_future_skew)
                if progress_age > int(source["stage_deadlines_seconds"].get(status, source["stage_deadlines_seconds"]["retry"])):
                    add("processor", "stage_progress_stalled", "error", _fault_action(source, "processor_stalled"))
            except ValueError as exc:
                add("processor", str(exc), "error", _fault_action(source, "processor_stalled"))
            if status in {"validating", "enriching", "composing"}:
                try:
                    heartbeat_age = seconds_since(as_of, checkpoint.get("last_heartbeat_at"), "checkpoint.last_heartbeat_at", maximum_future_skew)
                    if heartbeat_age > int(source["heartbeat_timeout_seconds"]):
                        add("processor", "heartbeat_stale", "error", _fault_action(source, "processor_stalled"))
                except ValueError as exc:
                    add("processor", str(exc), "error", _fault_action(source, "processor_stalled"))
            lease = checkpoint.get("lease")
            if isinstance(lease, dict):
                try:
                    if parse_time(lease.get("expires_at"), "checkpoint.lease.expires_at") <= as_of:
                        add("processor", "lease_expired", "error", _fault_action(source, "processor_stalled"))
                except ValueError as exc:
                    add("processor", str(exc), "error", _fault_action(source, "processor_stalled"))
            elif status in {"validating", "enriching", "composing"}:
                add("processor", "active_stage_lease_missing", "error", _fault_action(source, "processor_stalled"))
        if status == "retry":
            attempts_by_id = checkpoint.get("attempts_by_id") if isinstance(checkpoint.get("attempts_by_id"), dict) else {}
            detail_budget = int(source.get("retries_per_detail", 0)) + 1
            for detail in checkpoint.get("detail_records", []):
                if not isinstance(detail, dict) or detail.get("status") != "quarantined":
                    continue
                identity = str(detail.get("id", ""))
                if identity and int(attempts_by_id.get(identity, 0)) >= detail_budget:
                    failure_identity = sha256_bytes(canonical_json({"id": identity, "source_sha256": detail.get("source_sha256"), "guide_sha256": detail.get("guide_sha256")}))
                    add("processor", "retry_exhausted", "error", _fault_action(source, "processor_stalled"), failure_identity)
                    break
        if status == "quarantined":
            add("processor", "generation_quarantined", "error", _fault_action(source, "processor_stalled"))
        if status in {"queued", "validating", "enriching", "composing", "retry"}:
            for reference in checkpoint.get("input_artifacts", []):
                try:
                    expiry = parse_time(reference.get("expires_at"), "checkpoint.input_artifact.expires_at")
                    artifact_id = str(reference.get("artifact_id") or "")
                    artifact_meta = artifact_by_id.get(artifact_id)
                    if artifact_meta and artifact_meta.get("availability_error") is True:
                        add("processor", "artifact_availability_unavailable", "error", _fault_action(source, "processor_stalled"), artifact_id)
                    elif expiry <= as_of or (artifact_meta and artifact_meta.get("expired") is True):
                        add("processor", "input_artifact_expired", "error", _fault_action(source, "processor_stalled"))
                        break
                except ValueError as exc:
                    add("processor", str(exc), "error", _fault_action(source, "processor_stalled"))
                    break
        if status in {"ready", "no-change"}:
            if outcome.get("composer_status") == "ready_scoped" or int(outcome.get("pending_count", 0) or 0) > 0:
                add("candidate", "candidate_ready_with_pending_scope", "warning", _fault_action(source, "processor_stalled"))
            output_artifact = checkpoint.get("output_artifact") if isinstance(checkpoint.get("output_artifact"), dict) else {}
            artifact_id = str(output_artifact.get("artifact_id") or "")
            artifact_meta = artifact_by_id.get(artifact_id)
            try:
                output_expiry = parse_time(output_artifact.get("expires_at"), "checkpoint.output_artifact.expires_at")
                if artifact_meta and artifact_meta.get("availability_error") is True:
                    add("candidate", "artifact_availability_unavailable", "error", _fault_action(source, "processor_stalled"), artifact_id)
                elif not artifact_id or output_expiry <= as_of or (artifact_meta and artifact_meta.get("expired") is True):
                    add("candidate", "candidate_output_unavailable", "error", _fault_action(source, "processor_stalled"))
            except ValueError as exc:
                add("candidate", str(exc), "error", _fault_action(source, "processor_stalled"))
            bundle_valid, bundle_issue = processor_output_bundle_valid(checkpoint)
            if not bundle_valid:
                add("candidate", bundle_issue, "error", _fault_action(source, "processor_stalled"))

    ordered_checkpoints = []
    for row in checkpoints:
        current_observation = row.get("last_observation")
        if isinstance(current_observation, dict):
            try:
                ordered_checkpoints.append((parse_time(current_observation.get("observed_at"), "last_observation.observed_at"), current_observation))
            except ValueError:
                continue
    ordered_checkpoints.sort(key=lambda entry: entry[0], reverse=True)
    repeated_failures = 0
    for _, prior in ordered_checkpoints:
        if prior.get("execution_mode") != "live" or prior.get("collection_status") != "failure":
            break
        repeated_failures += 1
    if repeated_failures >= int(source["provider_failure_threshold"]):
        latest_failure = checkpoint.get("outcome", {}).get("reason", "provider_failure") if checkpoint else "provider_failure"
        safe_identity = latest_failure if isinstance(latest_failure, str) and re.fullmatch(r"[a-z0-9_-]{1,80}", latest_failure) else "provider_failure"
        add("observation", "repeated_provider_failures", "error", _fault_action(source, "provider_failure"), safe_identity)

    promotion_state = "unavailable"
    publication = None
    promotion_stage: dict[str, Any] | None = None
    last_good_for_source = last_good.get(source_id) if isinstance(last_good, dict) else None
    promotion_runs = promotion_runs_by_id or {}
    source_records = promotion_records_for_source(promotion_ack, source_id)
    current_records: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    trusted_readbacks: list[tuple[tuple[dt.datetime, int, int], dict[str, Any]]] = []
    for record in source_records:
        candidate = record.get("candidate") if isinstance(record, dict) else None
        if not isinstance(candidate, dict) or str(candidate.get("repository", repository)).casefold() != repository.casefold():
            add("promotion", "promotion_record_identity_invalid", "error", _fault_action(source, "promotion_wait"))
            continue
        record_generation = str(candidate.get("generation_id", ""))
        accepted = promotion_items_for_source(record, source_id, record_generation)
        if not accepted:
            add("promotion", "promotion_record_transition_invalid", "error", _fault_action(source, "promotion_wait"), record_generation)
            continue
        final_item = accepted[-1]
        if record_generation == generation_id and record.get("superseded_by") is None:
            current_records.append((record, accepted))
        if final_item.get("status") != "read-back-confirmed":
            continue
        claimed_publication = promotion_publication(final_item)
        trusted_run = trusted_promotion_run(
            final_item, promotion_runs, repository, promotion_workflow_paths or {}, workflow_ids_by_path or {},
            as_of, maximum_future_skew,
        )
        if claimed_publication is None or claimed_publication.get("verified") is not True:
            if record_generation == generation_id:
                add("publication", "readback_receipt_invalid", "error", _fault_action(source, "publication_failure"), record_generation)
            continue
        if trusted_run is None:
            if record_generation == generation_id:
                add("publication", "promotion_readback_run_unverified", "error", _fault_action(source, "publication_failure"), str(final_item.get("run_id", "")))
            continue
        claimed_publication.update({
            "source_id": source_id,
            "generation_id": record_generation,
            "publication_run_jobs_completed_at": trusted_run["jobs_completed_at"],
            "publication_run_completion_basis": trusted_run["completion_basis"],
            "publication_run_id": trusted_run["run_id"],
            "publication_run_attempt": trusted_run["run_attempt"],
            "publisher_run_verified": True,
        })
        trusted_readbacks.append((publication_order(claimed_publication), claimed_publication))

    if len(current_records) > 1:
        # A durable refresh intent intentionally overlaps its predecessor
        # until the exact new PR head/body has been read back. Keep reporting
        # the predecessor as active while the target is only prepared.
        prepared_refreshes = [
            record for record, _accepted in current_records
            if record.get("status") == "prepared" and isinstance(record.get("refresh_from"), dict)
        ]
        if len(prepared_refreshes) == 1:
            predecessor_ref = prepared_refreshes[0]["refresh_from"]
            predecessors = [
                item for item in current_records
                if (
                    item[0].get("candidate", {}).get("registry_sha256") == predecessor_ref.get("registry_sha256")
                    and item[0].get("candidate", {}).get("head_sha") == predecessor_ref.get("head_sha")
                    and item[0].get("ownership", {}).get("body_sha256") == predecessor_ref.get("body_sha256")
                    and item[0].get("ownership", {}).get("branch") == predecessor_ref.get("branch")
                )
            ]
            if len(predecessors) == 1:
                current_records = predecessors
            else:
                add("promotion", "promotion_generation_record_ambiguous", "error", _fault_action(source, "promotion_wait"), generation_id)
        else:
            add("promotion", "promotion_generation_record_ambiguous", "error", _fault_action(source, "promotion_wait"), generation_id)
    current_items = current_records[0][1] if len(current_records) == 1 else []
    if current_items:
        current_item = current_items[-1]
        promotion_state = str(current_item.get("status", "unknown"))
        current_run = None if promotion_state == "prepared" else trusted_promotion_run(
            current_item, promotion_runs, repository, promotion_workflow_paths or {}, workflow_ids_by_path or {},
            as_of, maximum_future_skew,
        )
        publication = promotion_publication(current_item)
        if publication is not None:
            publication["source_id"] = source_id
            publication["generation_id"] = generation_id
            publication["publisher_run_verified"] = current_run is not None if promotion_state == "read-back-confirmed" else None

        if promotion_state == "pending-review":
            add("promotion", "promotion_pending_review", "info", _fault_action(source, "promotion_wait"))
            stage_deadline_key = "pending-review"
        elif promotion_state in {"merged", "publication-pending", "published"}:
            add("publication", "publication_or_readback_pending", "warning", _fault_action(source, "promotion_wait"))
            stage_deadline_key = "publication-pending"
        else:
            stage_deadline_key = None

        if stage_deadline_key is not None:
            stage_entered_at = current_item.get("observed_at")
            stage_deadline = int(source["stage_deadlines_seconds"][stage_deadline_key])
            try:
                stage_age = seconds_since(as_of, stage_entered_at, "promotion_stage.observed_at", maximum_future_skew)
                promotion_stage = {
                    "status": promotion_state,
                    "entered_at": stage_entered_at,
                    "age_seconds": stage_age,
                    "deadline_seconds": stage_deadline,
                }
                if stage_age > stage_deadline:
                    if stage_deadline_key == "pending-review":
                        add("promotion", "promotion_review_wait_overdue", "warning", _fault_action(source, "promotion_wait"))
                    else:
                        add("publication", "publication_readback_lag_overdue", "error", _fault_action(source, "promotion_wait"))
            except ValueError as exc:
                add("promotion", str(exc), "error", _fault_action(source, "promotion_wait"))

        if promotion_state not in {"prepared"}:
            if current_run is None:
                reason = "promotion_readback_run_unverified" if promotion_state == "read-back-confirmed" else "promotion_ack_run_unverified"
                stage = "publication" if promotion_state == "read-back-confirmed" else "promotion"
                add(stage, reason, "error", _fault_action(source, "publication_failure" if stage == "publication" else "promotion_wait"), str(current_item.get("run_id", "")))

        if promotion_state == "failed":
            add("publication", "publication_or_promotion_failed", "error", _fault_action(source, "publication_failure"))
        elif promotion_state == "closed":
            add("promotion", "candidate_closed_without_acknowledged_publication", "warning", _fault_action(source, "promotion_wait"))
        elif promotion_state == "read-back-confirmed":
            if publication is None or publication.get("verified") is not True:
                add("publication", "readback_receipt_invalid", "error", _fault_action(source, "publication_failure"))
            elif publication.get("publisher_run_verified") is not True:
                add("publication", "promotion_readback_run_unverified", "error", _fault_action(source, "publication_failure"), str(current_item.get("run_id", "")))
    elif checkpoint is not None and checkpoint.get("status") == "ready":
        add("promotion", "promotion_ack_missing", "warning", _fault_action(source, "promotion_wait"))
        if source_records:
            add("promotion", "promotion_record_for_different_generation", "warning", _fault_action(source, "promotion_wait"), generation_id)

    if trusted_readbacks:
        latest_order, latest_publication = max(trusted_readbacks, key=lambda entry: entry[0])
        existing_order = publication_order(last_good_for_source)
        if last_good_for_source is None:
            last_good_for_source = latest_publication
        elif existing_order is not None:
            if latest_order > existing_order:
                last_good_for_source = latest_publication
            elif latest_order == existing_order and latest_publication != last_good_for_source:
                add("publication", "last_good_publication_order_ambiguous", "warning", _fault_action(source, "publication_failure"))
        elif (
            isinstance(last_good_for_source, dict)
            and last_good_for_source.get("source_id") == latest_publication.get("source_id")
            and last_good_for_source.get("generation_id") == latest_publication.get("generation_id")
            and last_good_for_source.get("publication_revision") == latest_publication.get("publication_revision")
            and last_good_for_source.get("publication_pointer_revision") == latest_publication.get("publication_pointer_revision")
            and last_good_for_source.get("artifact_identity") == latest_publication.get("artifact_identity")
        ):
            last_good_for_source = latest_publication
        else:
            add("publication", "last_good_publication_order_ambiguous", "warning", _fault_action(source, "publication_failure"))

    canonical_report = {
        "main": main_identity,
        "last_good": last_good_for_source,
        "promotion_status": promotion_state,
        "publication": publication,
        "promotion_stage": promotion_stage,
    }
    unique_faults = {row["fault_key"]: row for row in faults}
    faults = [unique_faults[key] for key in sorted(unique_faults)]
    severity_rank = {"info": 0, "warning": 1, "error": 2}
    worst = max((severity_rank.get(row["severity"], 2) for row in faults), default=0)
    overall = "healthy" if worst == 0 else "degraded" if worst == 1 else "blocked"
    if mode == "fixture":
        overall = "fixture"
    return {
        "source_id": source_id,
        "overall": overall,
        "observation": {
            "state": observation_state,
            "observed_at": observation.get("observed_at") if observation else None,
            "producer_run_id": observation.get("producer_run_id") if observation else None,
            "refresh_evidence_sha256": observation.get("refresh_evidence_sha256") if observation else None,
            "execution_mode": "fixture" if mode == "fixture" and observation else (observation.get("execution_mode") if observation else None),
            "collection_status": observation.get("collection_status") if observation else None,
            "age_seconds": observation_age,
            "max_age_seconds": int(source["max_observation_age_seconds"]),
        },
        "processor": {
            "state": processor_state,
            "generation_id": generation_id or None,
            "candidate_sha256": (checkpoint.get("generation_inputs", {}).get("candidate_sha256") if checkpoint and isinstance(checkpoint.get("generation_inputs"), dict) else None),
            "checkpoint_observed_at": checkpoint.get("observed_at") if checkpoint else None,
            "last_observation": checkpoint.get("last_observation") if checkpoint else None,
            "last_heartbeat_at": checkpoint.get("last_heartbeat_at") if checkpoint else None,
            "last_progress_at": checkpoint.get("last_progress_at") if checkpoint else None,
            "attempts_consumed": checkpoint.get("attempts_consumed") if checkpoint else None,
            "attempts_by_id": checkpoint.get("attempts_by_id") if checkpoint else None,
            "request_reservation": checkpoint.get("request_reservation") if checkpoint else None,
            "detail_records": checkpoint.get("detail_records") if checkpoint else None,
            "detail_queue_cursor": checkpoint.get("detail_queue_cursor") if checkpoint else None,
            "lease": checkpoint.get("lease") if checkpoint else None,
            "output_artifact": checkpoint.get("output_artifact") if checkpoint else None,
            "output_digests": checkpoint.get("output_digests") if checkpoint else None,
            "outcome": checkpoint.get("outcome") if checkpoint else None,
        },
        "collector": {
            "latest_scheduled_run": report_workflow_run(latest_scheduled_run, latest_scheduled_artifacts) if latest_scheduled_run else None,
            "latest_execution_run": report_workflow_run(latest_run, latest_run_artifacts) if latest_run else None,
            "age_seconds": schedule_age,
            "expected_interval_seconds": int(source["expected_interval_seconds"]),
            "grace_seconds": int(source["observation_grace_seconds"]),
        },
        "canonical": canonical_report,
        "faults": faults,
        "recommended_recovery": sorted({row["recommended_action"] for row in faults if row["severity"] != "info"}),
    }


def promotion_items_for_source(ack: dict[str, Any], source_id: str, generation_id: str) -> list[dict[str, Any]]:
    """Bind the append-only acknowledgement history to its one canonical candidate."""
    candidate = ack.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("source_id") != source_id or candidate.get("generation_id") != generation_id:
        return []
    rows = ack.get("acknowledgements")
    if not isinstance(rows, list):
        return []
    previous_status = "prepared"
    merge_sha = None
    accepted: list[dict[str, Any]] = []
    transitions = {
        "prepared": {"pending-review", "failed", "closed"},
        "pending-review": {"merged", "failed", "closed"},
        "merged": {"publication-pending", "failed"},
        "publication-pending": {"published", "failed"},
        "published": {"read-back-confirmed", "failed"},
        "read-back-confirmed": set(), "failed": {"publication-pending", "closed"}, "closed": set(),
    }
    expected_artifact = {
        "path": candidate.get("registry_path"),
        "bytes": candidate.get("registry_bytes"),
        "sha256": candidate.get("registry_sha256"),
    }
    previous_observed_at: dt.datetime | None = None
    published_revision: str | None = None
    published_pointer_revision: str | None = None
    for row in rows:
        if not isinstance(row, dict):
            return []
        status = row.get("status")
        if status not in transitions.get(previous_status, set()):
            return []
        if row.get("manifest_sha256") != candidate.get("manifest_sha256") or row.get("artifact_identity") != expected_artifact:
            return []
        try:
            observed_at = parse_time(row.get("observed_at"), "promotion_ack.observed_at")
        except ValueError:
            return []
        run_id = row.get("run_id")
        run_attempt = row.get("run_attempt")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1 or isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or run_attempt < 1:
            return []
        run_url = row.get("run_url")
        try:
            parsed_url = urllib.parse.urlsplit(str(run_url))
        except ValueError:
            return []
        expected_url_path = f"/{candidate.get('repository')}/actions/runs/{run_id}/attempts/{run_attempt}"
        if parsed_url.scheme != "https" or parsed_url.netloc != "github.com" or parsed_url.path.casefold() != expected_url_path.casefold() or parsed_url.query or parsed_url.fragment:
            return []
        if previous_observed_at is not None and observed_at < previous_observed_at:
            return []
        previous_observed_at = observed_at
        if status == "pending-review":
            if row.get("source_sha") != candidate.get("head_sha"):
                return []
        elif status == "merged":
            merge_sha = row.get("source_sha")
        elif merge_sha is not None and status in {"publication-pending", "published", "read-back-confirmed", "failed"} and row.get("source_sha") != merge_sha:
            return []
        if status == "published":
            published_revision = row.get("publication_revision")
            published_pointer_revision = row.get("publication_pointer_revision")
            if not re.fullmatch(r"[a-f0-9]{40}", str(published_revision or "")) or not re.fullmatch(r"[a-f0-9]{40}", str(published_pointer_revision or "")):
                return []
        elif status == "read-back-confirmed":
            if (
                row.get("publication_revision") != published_revision
                or row.get("publication_pointer_revision") != published_pointer_revision
            ):
                return []
        if status == "read-back-confirmed":
            if (
                row.get("read_back_verified") is not True
                or row.get("read_back_sha256") != candidate.get("registry_sha256")
                or row.get("read_back_bytes") != candidate.get("registry_bytes")
                or row.get("publication_revision") is None
            ):
                return []
        previous_status = str(status)
        accepted.append(row)
    if ack.get("status") != previous_status:
        return []
    if not accepted and ack.get("status") == "prepared":
        accepted.append({"status": "prepared", "observed_at": ack.get("candidate", {}).get("payload_readback", {}).get("observed_at")})
    return accepted


def promotion_records_for_source(journal: dict[str, Any] | None, source_id: str) -> list[dict[str, Any]]:
    if not isinstance(journal, dict):
        return []
    if journal.get("schema_version") == "datapan.canonical-update-promotion-journal.v1":
        records = journal.get("records")
        return [row for row in records if isinstance(row, dict) and isinstance(row.get("candidate"), dict) and row["candidate"].get("source_id") == source_id] if isinstance(records, list) else []
    candidate = journal.get("candidate")
    return [journal] if isinstance(candidate, dict) and candidate.get("source_id") == source_id else []


def promotion_run_references(journal: dict[str, Any] | None, source_ids: set[str]) -> set[tuple[str, int]]:
    references: set[tuple[str, int]] = set()
    if not isinstance(journal, dict):
        return references
    for source_id in source_ids:
        for record in promotion_records_for_source(journal, source_id):
            acknowledgements = record.get("acknowledgements")
            if not isinstance(acknowledgements, list) or not acknowledgements:
                continue
            final_item = acknowledgements[-1]
            if not isinstance(final_item, dict):
                continue
            run_id = final_item.get("run_id")
            run_attempt = final_item.get("run_attempt")
            if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0 and isinstance(run_attempt, int) and not isinstance(run_attempt, bool) and run_attempt > 0:
                references.add((str(run_id), run_attempt))
    return references


def trusted_promotion_run(
    item: dict[str, Any], promotion_runs_by_id: dict[str, dict[str, Any]], repository: str,
    workflow_paths: dict[str, str], workflow_ids_by_path: dict[str, int],
    as_of: dt.datetime, maximum_future_skew: int,
) -> dict[str, Any] | None:
    run_id = item.get("run_id")
    run_attempt = item.get("run_attempt")
    evidence = promotion_runs_by_id.get(f"{run_id}/{run_attempt}")
    if not isinstance(evidence, dict) or evidence.get("availability_error") is True:
        return None
    run = evidence.get("run")
    jobs = evidence.get("jobs")
    if (
        evidence.get("attempt_number") != run_attempt
        or evidence.get("jobs_api_endpoint") != f"repos/{repository}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
        or isinstance(evidence.get("job_count"), bool)
        or not isinstance(evidence.get("job_count"), int)
        or not isinstance(jobs, list)
        or len(jobs) != evidence.get("job_count")
        or not jobs
        or not isinstance(run, dict)
    ):
        return None
    status = item.get("status")
    if status in {"publication-pending", "published", "read-back-confirmed"}:
        expected_path = workflow_paths.get("publication_ack_workflow_path")
    elif status == "failed":
        allowed_paths = {workflow_paths.get("promotion_workflow_path"), workflow_paths.get("publication_ack_workflow_path")}
        if not any(path and workflow_path_matches(run.get("path"), path) for path in allowed_paths):
            return None
        expected_path = next(path for path in allowed_paths if path and workflow_path_matches(run.get("path"), path))
    else:
        expected_path = workflow_paths.get("promotion_workflow_path")
    if (
        str(run.get("id", "")) != str(run_id)
        or run.get("run_attempt") != run_attempt
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or not expected_path
        or not trusted_main_workflow_run(
            run, repository, expected_path, workflow_ids_by_path.get(expected_path), None,
        )
    ):
        return None
    completed_jobs: list[dt.datetime] = []
    seen_job_ids: set[str] = set()
    allowed_job_conclusions = {"success", "skipped", "neutral"}
    expected_head_sha = run["head_sha"]
    for job in jobs:
        if not isinstance(job, dict):
            return None
        job_id = job.get("id")
        if (
            isinstance(job_id, bool)
            or not isinstance(job_id, int)
            or job_id < 1
            or str(job_id) in seen_job_ids
            or str(job.get("run_id", "")) != str(run_id)
            or job.get("status") != "completed"
            or job.get("conclusion") not in allowed_job_conclusions
            or (expected_head_sha and job.get("head_sha") != expected_head_sha)
        ):
            return None
        seen_job_ids.add(str(job_id))
        try:
            job_completed_at = parse_time(job.get("completed_at"), "promotion_job.completed_at")
            seconds_since(as_of, job.get("completed_at"), "promotion_job.completed_at", maximum_future_skew)
        except ValueError:
            return None
        completed_jobs.append(job_completed_at)
    if len(completed_jobs) != evidence.get("job_count"):
        return None
    jobs_completed_at = max(completed_jobs)
    if run.get("completed_at") is not None:
        try:
            seconds_since(as_of, run.get("completed_at"), "promotion_run.completed_at", maximum_future_skew)
        except ValueError:
            return None
    return {
        "jobs_completed_at": utc_timestamp(jobs_completed_at),
        "completion_basis": "max_completed_at_all_jobs_exact_run_attempt",
        "run_id": int(run_id),
        "run_attempt": int(run_attempt),
    }


def publication_order(value: dict[str, Any] | None) -> tuple[dt.datetime, int, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        if value.get("publication_run_completion_basis") != "max_completed_at_all_jobs_exact_run_attempt":
            return None
        completed_at = parse_time(value.get("publication_run_jobs_completed_at"), "last_good.publication_run_jobs_completed_at")
        run_id = int(value.get("publication_run_id"))
        run_attempt = int(value.get("publication_run_attempt"))
    except (ValueError, TypeError):
        return None
    if run_id < 1 or run_attempt < 1:
        return None
    return completed_at, run_id, run_attempt


def promotion_publication(item: dict[str, Any]) -> dict[str, Any] | None:
    artifact = item.get("artifact_identity")
    if not isinstance(artifact, dict):
        return None
    status = item.get("status")
    verified = (
        status == "read-back-confirmed"
        and item.get("read_back_verified") is True
        and item.get("read_back_sha256") == artifact.get("sha256")
        and item.get("read_back_bytes") == artifact.get("bytes")
        and isinstance(item.get("publication_revision"), str)
        and bool(re.fullmatch(r"[a-f0-9]{40}", item["publication_revision"]))
        and isinstance(item.get("publication_pointer_revision"), str)
        and bool(re.fullmatch(r"[a-f0-9]{40}", item["publication_pointer_revision"]))
    )
    return {
        "status": status,
        "observed_at": item.get("observed_at"),
        "source_sha": item.get("source_sha"),
        "manifest_sha256": item.get("manifest_sha256"),
        "publication_revision": item.get("publication_revision"),
        "publication_pointer_revision": item.get("publication_pointer_revision"),
        "artifact_identity": artifact,
        "verified": verified,
    }


def processor_output_bundle_valid(checkpoint: dict[str, Any]) -> tuple[bool, str]:
    """Cross-bind the declared artifact bundle without downloading its payload."""
    rows = checkpoint.get("output_digests")
    locator = checkpoint.get("output_artifact")
    if not isinstance(rows, list) or not rows or len(rows) > 16 or not isinstance(locator, dict):
        return False, "candidate_output_bundle_shape_invalid"
    paths: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            return False, "candidate_output_bundle_entry_invalid"
        path = row.get("path")
        digest = row.get("sha256")
        byte_count = row.get("bytes")
        if (
            not isinstance(path, str)
            or path not in PROCESSOR_OUTPUT_PATHS
            or not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
            or byte_count > 536870912
        ):
            return False, "candidate_output_bundle_entry_invalid"
        paths.append(path)
    if len(paths) != len(set(paths)):
        return False, "candidate_output_bundle_duplicate_path"
    if checkpoint.get("status") in {"ready", "no-change"} and tuple(paths) != PROCESSOR_OUTPUT_PATHS:
        return False, "candidate_output_bundle_paths_incomplete"
    bundle_digest = locator.get("bundle_manifest_sha256")
    if not isinstance(bundle_digest, str) or not DIGEST.fullmatch(bundle_digest):
        return False, "candidate_output_digest_missing"
    if bundle_digest != sha256_bytes(canonical_json(rows)):
        return False, "candidate_output_bundle_digest_mismatch"
    return True, ""


def read_last_good(history_path: pathlib.Path | None) -> dict[str, Any] | None:
    if history_path is None or not history_path.exists():
        return None
    value = load_json(history_path)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "datapan.upstream-catalogue-health-state.v1"
        or not verify_sealed(value, "state_sha256")
    ):
        raise ValueError("health_state_corrupt")
    last_good = value.get("last_good_by_source")
    return last_good if isinstance(last_good, dict) else None


def evaluate(
    *, as_of: dt.datetime, repository: str, health_policy: dict[str, Any], source_policy: dict[str, Any],
    workflow_runs: list[dict[str, Any]], artifacts_by_run: dict[str, list[dict[str, Any]]],
    artifact_by_id: dict[str, dict[str, Any]], processor_state_dir: pathlib.Path,
    promotion_ack: dict[str, Any] | None, main_revision: str, manifest_sha256: str, registry_path: pathlib.Path,
    last_good: dict[str, Any] | None, mode: str, workflow_run_id: str, workflow_run_attempt: int,
    source_policy_sha256: str, health_policy_sha256: str, workflow_api_error: str | None = None,
    workflow_ids_by_path: dict[str, int] | None = None,
    producer_runs_by_id: dict[str, dict[str, Any]] | None = None,
    promotion_runs_by_id: dict[str, dict[str, Any]] | None = None,
    promotion_workflow_paths: dict[str, str] | None = None,
    promotion_ack_error: str | None = None,
    health_state_error: str | None = None,
) -> dict[str, Any]:
    if mode not in {"live", "fixture"}:
        raise ValueError("invalid_execution_mode")
    future_skew = int(health_policy["clock"]["maximum_future_skew_seconds"])
    if not REVISION.fullmatch(main_revision):
        raise ValueError("invalid_main_revision")
    if not DIGEST.fullmatch(manifest_sha256):
        raise ValueError("invalid_manifest_digest")
    source_rows = {row["source_id"]: row for row in source_policy.get("sources", []) if isinstance(row, dict) and isinstance(row.get("source_id"), str)}
    monitor_rows = health_policy.get("sources", [])
    if not isinstance(monitor_rows, list) or not monitor_rows:
        raise ValueError("health_policy_sources_missing")
    if len({row.get("source_id") for row in monitor_rows if isinstance(row, dict)}) != len(monitor_rows):
        raise ValueError("health_policy_source_id_duplicate")
    declared = set(source_rows)
    monitored = {row["source_id"] for row in monitor_rows}
    if declared != monitored:
        raise ValueError(f"health_policy_source_binding_mismatch:refresh={sorted(declared)}:health={sorted(monitored)}")
    for source in monitor_rows:
        refresh_source = source_rows[source["source_id"]]
        cadence = refresh_source.get("cadence", {})
        cron = cadence.get("cron") if isinstance(cadence, dict) else None
        if cadence.get("timezone") != "UTC" or not isinstance(cron, str):
            raise ValueError(f"source_cadence_invalid:{source['source_id']}")
        try:
            computed_interval = cadence_interval_seconds(cron)
        except ValueError as exc:
            raise ValueError(f"source_cadence_unsupported:{source['source_id']}") from exc
        if int(source["expected_interval_seconds"]) != computed_interval:
            raise ValueError(f"source_cadence_interval_mismatch:{source['source_id']}")
        if int(source["max_observation_age_seconds"]) != int(source["expected_interval_seconds"]) + int(source["observation_grace_seconds"]):
            raise ValueError(f"source_observation_ttl_mismatch:{source['source_id']}")
    main_registry_identity = manifest_registry_identity(ROOT / "manifest.json", registry_path)
    main_identity = {
        "revision": main_revision,
        "manifest_sha256": manifest_sha256,
        **main_registry_identity,
    }
    policy_sha = source_policy_sha256
    sources = [
        evaluate_source(
            source=source,
            repository=repository,
            refresh_source={**source_rows[source["source_id"]], "workflow_path": health_policy["health_workflow"]["collector_workflow_path"]},
            policy_sha256=policy_sha,
            as_of=as_of,
            workflow_runs=workflow_runs,
            artifacts_by_run=artifacts_by_run,
            state_dir=processor_state_dir,
            artifact_by_id=artifact_by_id,
            promotion_ack=promotion_ack,
            main_identity=main_identity,
            last_good=last_good,
            mode=mode,
            maximum_future_skew=future_skew,
            max_checkpoint_bytes=int(health_policy["processor_state"]["max_bytes_per_file"]),
            workflow_api_error=workflow_api_error,
            producer_runs_by_id=producer_runs_by_id,
            promotion_runs_by_id=promotion_runs_by_id,
            promotion_workflow_paths=promotion_workflow_paths,
            workflow_ids_by_path=workflow_ids_by_path,
            promotion_ack_error=promotion_ack_error,
            health_state_error=health_state_error,
        )
        for source in monitor_rows
    ]
    all_faults = [entry for row in sources for entry in row["faults"]]
    receipt = {
        "schema_version": "datapan.upstream-catalogue-health.v1",
        "repository": repository,
        "evaluated_at": utc_timestamp(as_of),
        "execution_mode": mode,
        "health_workflow": {
            "run_id": workflow_run_id,
            "run_attempt": workflow_run_attempt,
            "revision": main_revision,
        },
        "policy_sha256": health_policy_sha256,
        "source_policy_sha256": policy_sha,
        "sources": sources,
        "faults": all_faults,
        "summary": {
            "source_count": len(sources),
            "healthy": sum(row["overall"] == "healthy" for row in sources),
            "degraded": sum(row["overall"] == "degraded" for row in sources),
            "blocked": sum(row["overall"] == "blocked" for row in sources),
            "fixture": sum(row["overall"] == "fixture" for row in sources),
            "fault_count": len(all_faults),
            "live_fresh_observation_count": 0 if mode == "fixture" else sum(row["observation"]["state"] == "fresh" and row["observation"]["execution_mode"] == "live" for row in sources),
            "last_good_count": sum(row["canonical"]["last_good"] is not None for row in sources),
        },
    }
    return seal_receipt(receipt)


def validate_schema(value: Any, schema_path: pathlib.Path, label: str) -> None:
    if not schema_path.exists():
        raise ValueError(f"schema_missing:{label}")
    try:
        import jsonschema
    except ImportError as exc:
        raise ValueError("missing_dependency:jsonschema") from exc
    jsonschema.Draft202012Validator(load_json(schema_path)).validate(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, help="Explicit UTC evaluation time (RFC 3339).")
    parser.add_argument("--repository", default="StatPan/datapan-registry")
    parser.add_argument("--health-policy", type=pathlib.Path, default=HEALTH_POLICY_DEFAULT)
    parser.add_argument("--source-policy", type=pathlib.Path, default=SOURCE_POLICY_DEFAULT)
    parser.add_argument("--processor-state-dir", type=pathlib.Path, default=pathlib.Path(".datapan/upstream-catalogue-state"))
    parser.add_argument("--promotion-ack", type=pathlib.Path, default=pathlib.Path(".datapan/canonical-update-state/reports/canonical-update-promotion-receipt.json"))
    parser.add_argument("--health-state", type=pathlib.Path)
    parser.add_argument("--registry", type=pathlib.Path, default=pathlib.Path("data/data-go-kr.registry.json"))
    parser.add_argument("--main-revision", default="")
    parser.add_argument("--workflow-run-id", default="local")
    parser.add_argument("--workflow-run-attempt", type=int, default=1)
    parser.add_argument("--fixture-input", type=pathlib.Path, help="Explicit local-only inputs; never persisted as live health state.")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        as_of = parse_time(args.as_of, "as_of")
        health_policy = load_json(args.health_policy)
        source_policy = load_json(args.source_policy)
        validate_schema(health_policy, ROOT / "schemas/datapan.upstream-catalogue-health-policy.v1.schema.json", "health_policy")
        validate_schema(source_policy, ROOT / "schemas/datapan.source-refresh-policy.v1.schema.json", "source_policy")
        workflow_api_error = None
        health_state_error = None
        promotion_ack_error = None
        workflow_ids_by_path: dict[str, int] = {}
        if args.fixture_input:
            mode = "fixture"
            fixture = load_json(args.fixture_input)
            workflow_runs = fixture.get("workflow_runs", [])
            artifacts_by_run = fixture.get("artifacts_by_run", {})
            artifact_by_id = fixture.get("artifacts_by_id", {})
            producer_runs_by_id = fixture.get("producer_runs_by_id", {})
            promotion_ack = fixture.get("promotion_ack")
            promotion_runs_by_id = fixture.get("promotion_runs_by_id", {})
            workflow_ids_by_path = fixture.get("workflow_ids_by_path", {})
            main_revision = args.main_revision or fixture.get("main_revision", "")
            last_good = fixture.get("last_good")
        else:
            mode = "live"
            workflow = health_policy["health_workflow"]
            try:
                workflow_ids_by_path[workflow["collector_workflow_path"]] = collect_workflow_identity(
                    args.repository, workflow["collector_workflow_path"],
                )
                workflow_runs = collect_workflow_runs(args.repository, workflow["collector_workflow_path"], int(health_policy["processor_state"]["workflow_run_limit"]))
            except RuntimeError as exc:
                workflow_runs = []
                workflow_api_error = str(exc)
            scheduled = sorted((
                row for row in workflow_runs
                if trusted_main_workflow_run(
                    row, args.repository, workflow["collector_workflow_path"],
                    workflow_ids_by_path.get(workflow["collector_workflow_path"]), {"schedule"},
                )
            ), key=workflow_run_order)
            artifacts_by_run = {}
            artifact_by_id = {}
            producer_runs_by_id = {}
            promotion_runs_by_id = {}
            promotion_ack = load_json(args.promotion_ack) if args.promotion_ack and args.promotion_ack.exists() else None
            if promotion_ack is not None:
                try:
                    if promotion_ack.get("schema_version") == "datapan.canonical-update-promotion-journal.v1":
                        validate_schema(promotion_ack, PROMOTION_JOURNAL_SCHEMA, "promotion_journal")
                        if str(promotion_ack.get("repository", "")).casefold() != args.repository.casefold():
                            raise ValueError("promotion_journal_repository_mismatch")
                        records = promotion_ack.get("records")
                        if not isinstance(records, list):
                            raise ValueError("promotion_journal_records_invalid")
                        record_keys: set[tuple[str, str, str, str]] = set()
                        for record in records:
                            validate_schema(record, PROMOTION_SCHEMA, "promotion_record")
                            candidate = record.get("candidate") if isinstance(record, dict) else None
                            if not isinstance(candidate, dict) or str(candidate.get("repository", "")).casefold() != args.repository.casefold():
                                raise ValueError("promotion_record_repository_mismatch")
                            key = (
                                str(candidate.get("source_id", "")), str(candidate.get("scope", "")),
                                str(candidate.get("generation_id", "")), str(candidate.get("registry_sha256", "")),
                            )
                            if key in record_keys:
                                raise ValueError("promotion_journal_duplicate_candidate")
                            record_keys.add(key)
                    else:
                        validate_schema(promotion_ack, PROMOTION_SCHEMA, "promotion_ack")
                        candidate = promotion_ack.get("candidate") if isinstance(promotion_ack, dict) else None
                        if not isinstance(candidate, dict) or str(candidate.get("repository", "")).casefold() != args.repository.casefold():
                            raise ValueError("promotion_receipt_repository_mismatch")
                except Exception as exc:  # Schema validation errors must become a health fault.
                    promotion_ack_error = str(exc)
                    promotion_ack = None
            main_revision = args.main_revision
            if not main_revision:
                result = subprocess.run(["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=False, timeout=10)
                if result.returncode != 0:
                    raise ValueError("main_revision_unavailable")
                main_revision = result.stdout.strip()
            referenced_artifact_ids: set[str] = set()
            observed_run_ids: set[str] = set()
            for source in health_policy["sources"]:
                checkpoints, _ = load_processor_checkpoints(args.processor_state_dir, source["source_id"], int(health_policy["processor_state"]["max_bytes_per_file"]))
                for checkpoint in checkpoints:
                    observation = checkpoint.get("last_observation")
                    if isinstance(observation, dict) and str(observation.get("producer_run_id") or "").isdigit():
                        observed_run_ids.add(str(observation["producer_run_id"]))
                    for collection in (checkpoint.get("input_artifacts", []), [checkpoint.get("output_artifact", {})]):
                        for ref in collection:
                            if not isinstance(ref, dict) or not ref.get("artifact_id"):
                                continue
                            referenced_artifact_ids.add(str(ref["artifact_id"]))
            run_ids_to_inspect = {str(scheduled[-1].get("id", ""))} if scheduled else set()
            latest_collector = sorted(
                (
                    row for row in workflow_runs
                    if trusted_main_workflow_run(
                        row, args.repository, workflow["collector_workflow_path"],
                        workflow_ids_by_path.get(workflow["collector_workflow_path"]),
                        {"schedule", "workflow_dispatch"},
                    )
                ),
                key=workflow_run_order,
            )
            if latest_collector:
                run_ids_to_inspect.add(str(latest_collector[-1].get("id", "")))
            run_ids_to_inspect |= observed_run_ids
            for run_id in sorted(run_ids_to_inspect):
                if not run_id.isdigit():
                    continue
                try:
                    run_info = collect_run(args.repository, run_id)
                    if run_id in observed_run_ids:
                        producer_runs_by_id[run_id] = run_info
                except RuntimeError:
                    run_info = None
                try:
                    artifacts_by_run[run_id] = collect_run_artifacts(args.repository, run_id)
                except RuntimeError:
                    artifacts_by_run[run_id] = {"availability_error": True}
                if run_info is not None:
                    for row in artifacts_by_run.get(run_id, []) if isinstance(artifacts_by_run.get(run_id), list) else []:
                        if isinstance(row, dict) and str(row.get("id", "")).isdigit():
                            referenced_artifact_ids.add(str(row["id"]))
            source_ids = {str(row.get("source_id")) for row in health_policy.get("sources", []) if isinstance(row, dict)}
            promotion_references = promotion_run_references(promotion_ack, source_ids)
            if len(promotion_references) > 500:
                promotion_ack_error = "promotion_run_reference_limit_exceeded"
                promotion_references = set(sorted(promotion_references)[-500:])
            if promotion_references:
                for workflow_key in ("promotion_workflow_path", "publication_ack_workflow_path"):
                    workflow_path = health_policy.get("promotion_state", {}).get(workflow_key)
                    if not isinstance(workflow_path, str) or not workflow_path:
                        promotion_ack_error = promotion_ack_error or "promotion_workflow_path_missing"
                        continue
                    try:
                        workflow_ids_by_path[workflow_path] = collect_workflow_identity(args.repository, workflow_path)
                    except RuntimeError as exc:
                        promotion_ack_error = promotion_ack_error or str(exc)
            for promotion_run_id, promotion_attempt in sorted(promotion_references):
                key = f"{promotion_run_id}/{promotion_attempt}"
                try:
                    promotion_runs_by_id[key] = collect_run_attempt_evidence(args.repository, promotion_run_id, promotion_attempt)
                except RuntimeError:
                    promotion_runs_by_id[key] = {"availability_error": True}
            for artifact_id in sorted(referenced_artifact_ids):
                try:
                    metadata = collect_artifact(args.repository, artifact_id)
                except RuntimeError:
                    metadata = {"availability_error": True}
                if metadata is not None:
                    artifact_by_id[artifact_id] = metadata
            try:
                last_good = read_last_good(args.health_state) if args.health_state else None
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                last_good = None
                health_state_error = str(exc)
        if mode == "fixture":
            workflow_runs = workflow_runs if isinstance(workflow_runs, list) else []
            artifacts_by_run = artifacts_by_run if isinstance(artifacts_by_run, dict) else {}
            artifact_by_id = artifact_by_id if isinstance(artifact_by_id, dict) else {}
        receipt = evaluate(
            as_of=as_of,
            repository=args.repository,
            health_policy=health_policy,
            source_policy=source_policy,
            workflow_runs=workflow_runs,
            artifacts_by_run=artifacts_by_run,
            artifact_by_id=artifact_by_id,
            processor_state_dir=args.processor_state_dir,
            promotion_ack=promotion_ack if isinstance(promotion_ack, dict) else None,
            main_revision=main_revision,
            manifest_sha256=file_sha256(ROOT / "manifest.json"),
            registry_path=args.registry,
            last_good=last_good if isinstance(last_good, dict) else None,
            mode=mode,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            source_policy_sha256=file_sha256(args.source_policy),
            health_policy_sha256=file_sha256(args.health_policy),
            workflow_api_error=workflow_api_error,
            producer_runs_by_id=producer_runs_by_id,
            promotion_runs_by_id=promotion_runs_by_id,
            promotion_workflow_paths=health_policy.get("promotion_state", {}),
            workflow_ids_by_path=workflow_ids_by_path,
            promotion_ack_error=promotion_ack_error,
            health_state_error=health_state_error,
        )
        validate_schema(receipt, HEALTH_RECEIPT_SCHEMA, "health_receipt")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": "ok", "execution_mode": mode, "fault_count": receipt["summary"]["fault_count"], "receipt_sha256": receipt["receipt_sha256"]}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL upstream catalogue health: {type(exc).__name__}:{str(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
