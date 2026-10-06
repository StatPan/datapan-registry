#!/usr/bin/env python3
"""Append a health receipt and merge its deduplicated state in the owned branch root."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY_SCHEMA = ROOT / "schemas/datapan.upstream-catalogue-health-policy.v1.schema.json"
RECEIPT_SCHEMA = ROOT / "schemas/datapan.upstream-catalogue-health.v1.schema.json"
STATE_SCHEMA = ROOT / "schemas/datapan.upstream-catalogue-health-state.v1.schema.json"
OWNER_SCHEMA = ROOT / "schemas/datapan.upstream-catalogue-health-state-owner.v1.schema.json"
DIGEST = re.compile(r"^[a-f0-9]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_RETAINED_FAULTS = 5000
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
LINK_CONTRACT_FAILURES = {
    "no_reviewed_declaration": {
        "unresolved_requirements": ["reviewed_operation_declaration"],
        "next_action": "review_authoritative_declaration",
    },
    "subject_binding_unproven": {
        "unresolved_requirements": ["subject_binding"],
        "next_action": "verify_subject_binding",
    },
    "declaration_evidence_rejected": {
        "unresolved_requirements": ["declaration_source_binding", "operation_contract_validation"],
        "next_action": "review_declaration_evidence",
    },
    "validation_detail_unknown": {
        "unresolved_requirements": [],
        "next_action": "inspect_bound_validation_evidence",
    },
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError(f"invalid_timestamp:{label}")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid_timestamp:{label}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timezone_required:{label}")
    return parsed.astimezone(dt.timezone.utc)


def timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: pathlib.Path, maximum: int = 32 * 1024 * 1024) -> Any:
    if path.stat().st_size > maximum:
        raise ValueError(f"input_too_large:{path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_schema(value: Any, path: pathlib.Path, label: str) -> None:
    try:
        import jsonschema
    except ImportError as exc:
        raise ValueError("missing_dependency:jsonschema") from exc
    schema = load_json(path)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)


def verify_seal(value: Any, field: str) -> bool:
    if not isinstance(value, dict):
        return False
    claimed = value.get(field)
    if not isinstance(claimed, str) or not DIGEST.fullmatch(claimed):
        return False
    unsigned = dict(value)
    unsigned.pop(field, None)
    return claimed == digest(unsigned)


def seal(value: dict[str, Any], field: str) -> dict[str, Any]:
    value.pop(field, None)
    value[field] = digest(value)
    return value


def atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def initial_state(repository: str, branch: str, root: str) -> dict[str, Any]:
    return {
        "schema_version": "datapan.upstream-catalogue-health-state.v1",
        "repository": repository,
        "branch": branch,
        "root": root,
        "updated_at": None,
        "latest_receipt": {"path": "", "sha256": "0" * 64},
        "faults": [],
        "last_good_by_source": {},
        "observations_by_source": {},
        "state_status": "blocked",
    }


def initialize_owner(root: pathlib.Path, policy: dict[str, Any], repository: str, created_at: str) -> dict[str, Any]:
    state_policy = policy["health_state"]
    owner_path = root / pathlib.Path(state_policy["ownership_marker"]).name
    if owner_path.exists():
        if owner_path.is_symlink():
            raise ValueError("health_state_owner_marker_symlink")
        owner = load_json(owner_path)
        validate_schema(owner, OWNER_SCHEMA, "state_owner")
        if not verify_seal(owner, "ownership_sha256"):
            raise ValueError("state_ownership_digest_mismatch")
        expected = {
            "repository": repository,
            "branch": state_policy["branch"],
            "root": state_policy["root"],
            "policy_id": policy["policy_id"],
        }
        if any(owner.get(key) != value for key, value in expected.items()):
            raise ValueError("state_ownership_binding_mismatch")
        return owner

    root.mkdir(parents=True, exist_ok=True)
    unexpected = [path.name for path in root.iterdir()]
    if unexpected:
        raise ValueError("unowned_health_state_root_not_empty")
    owner = seal({
        "schema_version": "datapan.upstream-catalogue-health-state-owner.v1",
        "repository": repository,
        "branch": state_policy["branch"],
        "root": state_policy["root"],
        "policy_id": policy["policy_id"],
        "created_at": created_at,
    }, "ownership_sha256")
    validate_schema(owner, OWNER_SCHEMA, "state_owner")
    atomic_json(owner_path, owner)
    return owner


def receipt_file_name(receipt: dict[str, Any]) -> str:
    workflow = receipt["health_workflow"]
    run_id = str(workflow["run_id"])
    attempt = int(workflow["run_attempt"])
    if not SAFE_ID.fullmatch(run_id):
        raise ValueError("health_workflow_run_id_unsafe")
    return f"receipt-{run_id}-{attempt}-{receipt['receipt_sha256'][:16]}.json"


def current_faults_by_key(receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in receipt.get("faults", []):
        if not isinstance(item, dict):
            raise ValueError("health_receipt_fault_invalid")
        key = item.get("fault_key")
        if not isinstance(key, str) or not DIGEST.fullmatch(key):
            raise ValueError("health_receipt_fault_key_invalid")
        found[key] = item
    return found


def observation_record(source: dict[str, Any], receipt_sha256: str) -> dict[str, Any] | None:
    observation = source.get("observation", {})
    if (
        source.get("overall") == "fixture"
        or observation.get("state") != "fresh"
        or observation.get("execution_mode") != "live"
    ):
        return None
    producer = observation.get("producer_run_id")
    evidence = observation.get("refresh_evidence_sha256")
    observed_at = observation.get("observed_at")
    if not isinstance(producer, str) or not producer.isdigit() or not isinstance(evidence, str) or not DIGEST.fullmatch(evidence):
        return None
    parse_time(observed_at, "observation.observed_at")
    processor = source.get("processor", {})
    outcome = processor.get("outcome") if isinstance(processor.get("outcome"), dict) else {}
    generation_inputs = processor.get("generation_inputs") if isinstance(processor.get("generation_inputs"), dict) else {}
    return {
        "observed_at": observed_at,
        "producer_run_id": producer,
        "refresh_evidence_sha256": evidence,
        "generation_id": processor.get("generation_id"),
        "candidate_sha256": processor.get("candidate_sha256"),
        "processor_status": str(processor.get("state", "unknown")),
        "composer_status": outcome.get("composer_status"),
        "pending_count": int(outcome.get("pending_count", 0) or 0),
        "health_receipt_sha256": receipt_sha256,
    }


def promotion_execution_order(run: Any) -> tuple[dt.datetime, int, int] | None:
    """Return C attempt order from its immutable start time and exact identity."""
    if not isinstance(run, dict):
        return None
    run_id = run.get("run_id")
    attempt = run.get("run_attempt")
    if (
        not isinstance(run_id, str)
        or not run_id.isdigit()
        or int(run_id) < 1
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
    ):
        return None
    try:
        started_at = parse_time(run.get("run_started_at"), "promotion_execution.run_started_at")
    except ValueError:
        return None
    return started_at, int(run_id), attempt


def trusted_promotion_success(
    source: dict[str, Any], receipt: dict[str, Any], workflow_path: str,
    workflow_events: set[str], maximum_future_skew: int,
) -> dict[str, Any] | None:
    """Validate a C success summary emitted after the checker authenticated the configured workflow."""
    canonical = source.get("canonical")
    if not isinstance(canonical, dict):
        return None
    execution_state = canonical.get("promotion_execution")
    if not isinstance(execution_state, dict):
        return None
    success = execution_state.get("latest_successful_execution_run")
    latest = execution_state.get("latest_execution_run")
    if not isinstance(success, dict) or not isinstance(latest, dict):
        return None

    repository = receipt.get("repository")
    workflow_id = success.get("workflow_id")
    if (
        not isinstance(repository, str)
        or not repository
        or not isinstance(workflow_path, str)
        or not workflow_path
        or not workflow_events
        or isinstance(workflow_id, bool)
        or not isinstance(workflow_id, int)
        or workflow_id < 1
        or latest.get("workflow_id") != workflow_id
        or isinstance(latest.get("workflow_id"), bool)
    ):
        return None

    order = promotion_execution_order(success)
    latest_order = promotion_execution_order(latest)
    if order is None or latest_order is None or latest_order < order:
        return None
    try:
        evaluated_at = parse_time(receipt.get("evaluated_at"), "receipt.evaluated_at")
    except ValueError:
        return None
    if order[0] > evaluated_at + dt.timedelta(seconds=maximum_future_skew):
        return None

    expected_repository = repository.casefold()
    for run in (success, latest):
        head_sha = run.get("head_sha")
        run_workflow_id = run.get("workflow_id")
        if (
            run.get("path") != workflow_path
            or run.get("event") not in workflow_events
            or run.get("head_branch") != "main"
            or not isinstance(run.get("repository"), str)
            or run["repository"].casefold() != expected_repository
            or not isinstance(run.get("head_repository"), str)
            or run["head_repository"].casefold() != expected_repository
            or not isinstance(head_sha, str)
            or not re.fullmatch(r"[a-f0-9]{40,64}", head_sha)
            or isinstance(run_workflow_id, bool)
            or not isinstance(run_workflow_id, int)
            or run_workflow_id < 1
        ):
            return None
    if success.get("status") != "completed" or success.get("conclusion") != "success":
        return None
    if latest_order == order:
        identity_fields = (
            "run_id", "run_attempt", "workflow_id", "path", "event", "run_started_at",
            "head_branch", "head_sha", "repository", "head_repository",
        )
        if (
            any(latest.get(field) != success.get(field) for field in identity_fields)
            or latest.get("status") != "completed"
            or latest.get("conclusion") != "success"
        ):
            return None
    elif not (
        latest.get("status") != "completed"
        or latest.get("conclusion") in {"pending", "skipped", "neutral"}
    ):
        # A later nonterminal/neutral run does not undo an independently trusted
        # success after the recorded failure, but a later terminal failure does.
        return None
    return success


def merge_observations(state: dict[str, Any], receipt: dict[str, Any]) -> None:
    per_source = state.setdefault("observations_by_source", {})
    for source in receipt.get("sources", []):
        item = observation_record(source, receipt["receipt_sha256"])
        if item is None:
            continue
        history = per_source.setdefault(source["source_id"], [])
        if not isinstance(history, list):
            raise ValueError("observation_history_corrupt")
        identity = (item["producer_run_id"], item["refresh_evidence_sha256"])
        if any((row.get("producer_run_id"), row.get("refresh_evidence_sha256")) == identity for row in history if isinstance(row, dict)):
            continue
        if history and parse_time(item["observed_at"], "observation.observed_at") <= max(parse_time(row["observed_at"], "stored_observation.observed_at") for row in history):
            continue
        history.append(item)
        per_source[source["source_id"]] = history[-64:]


def sealed_health_receipt(receipt: dict[str, Any]) -> bool:
    claimed = receipt.get("receipt_sha256")
    if not isinstance(claimed, str) or not DIGEST.fullmatch(claimed):
        return False
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    return claimed == digest(unsigned)


def verified_already_canonical_relation(source: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the checker relation against its sealed processor and main identities."""
    if receipt.get("execution_mode") != "live" or not sealed_health_receipt(receipt):
        return None
    processor = source.get("processor")
    canonical = source.get("canonical")
    if not isinstance(processor, dict) or not isinstance(canonical, dict):
        return None
    relation = canonical.get("already_canonical_candidate")
    main = canonical.get("main")
    locator = processor.get("output_artifact")
    digests = processor.get("output_digests")
    if (
        not isinstance(relation, dict) or relation.get("verified") is not True
        or not isinstance(main, dict) or not isinstance(locator, dict)
        or not isinstance(digests, list)
    ):
        return None
    inventory_valid = (
        [row.get("path") for row in digests if isinstance(row, dict)] == list(PROCESSOR_OUTPUT_PATHS)
        and all(
            isinstance(row, dict)
            and set(row) == {"path", "sha256", "bytes"}
            and isinstance(row.get("sha256"), str)
            and DIGEST.fullmatch(row["sha256"])
            and isinstance(row.get("bytes"), int)
            and not isinstance(row.get("bytes"), bool)
            and 0 <= row["bytes"] <= 536870912
            for row in digests
        )
        and digest(digests) == locator.get("bundle_manifest_sha256")
    )
    if not inventory_valid:
        return None
    source_id = source.get("source_id")
    generation_id = processor.get("generation_id")
    checkpoint_sha = processor.get("checkpoint_sha256")
    artifact_id = str(locator.get("artifact_id", ""))
    run_id = str(locator.get("run_id", ""))
    artifact_name = locator.get("name")
    match = re.fullmatch(r"upstream-catalogue-processing-([0-9]{6,20})-([1-9][0-9]*)", str(artifact_name or ""))
    main_revision = main.get("revision")
    manifest_sha256 = main.get("manifest_sha256")
    composition_rows = [
        row for row in digests
        if isinstance(row, dict) and row.get("path") == "composed-candidate.registry.json"
    ]
    health_workflow = receipt.get("health_workflow")
    relation_attempt = relation.get("processor_run_attempt")
    relation_bytes = relation.get("composed_registry_bytes")
    registry_bytes = main.get("registry_bytes")
    if (
        source_id != "data_go_kr"
        or processor.get("state") not in {"ready", "no-change"}
        or not isinstance(generation_id, str) or not DIGEST.fullmatch(generation_id)
        or not isinstance(checkpoint_sha, str) or not DIGEST.fullmatch(checkpoint_sha)
        or match is None
        or match.group(1) != run_id
        or str(relation.get("source_id", "")) != source_id
        or relation.get("generation_id") != generation_id
        or relation.get("checkpoint_sha256") != checkpoint_sha
        or str(relation.get("processor_run_id", "")) != run_id
        or isinstance(relation_attempt, bool)
        or not isinstance(relation_attempt, int)
        or relation_attempt != int(match.group(2))
        or str(relation.get("artifact_id", "")) != artifact_id
        or not artifact_id.isdigit()
        or not DIGEST.fullmatch(str(locator.get("bundle_manifest_sha256", "")))
        or relation.get("output_bundle_sha256") != locator.get("bundle_manifest_sha256")
        or len(composition_rows) != 1
        or isinstance(relation_bytes, bool)
        or not isinstance(relation_bytes, int)
        or relation_bytes != composition_rows[0].get("bytes")
        or relation.get("composed_registry_sha256") != composition_rows[0].get("sha256")
        or isinstance(registry_bytes, bool)
        or not isinstance(registry_bytes, int)
        or relation_bytes != registry_bytes
        or relation.get("composed_registry_sha256") != main.get("registry_sha256")
        or not isinstance(main_revision, str) or not re.fullmatch(r"[a-f0-9]{40,64}", main_revision)
        or relation.get("main_revision") != main_revision
        or not isinstance(manifest_sha256, str) or not DIGEST.fullmatch(manifest_sha256)
        or relation.get("main_manifest_sha256") != manifest_sha256
        or not isinstance(health_workflow, dict)
        or health_workflow.get("revision") != main_revision
        or receipt.get("repository") != locator.get("repository")
        or main.get("registry_path") != "data/data-go-kr.registry.json"
    ):
        return None
    return relation


def already_canonical_fault_key_valid(old_fault: dict[str, Any], generation_id: str) -> bool:
    source_id = old_fault.get("source_id")
    stage = old_fault.get("stage")
    reason = old_fault.get("reason")
    owner_ticket = old_fault.get("owner_ticket")
    if (
        not isinstance(source_id, str)
        or stage != "promotion"
        or reason not in {"promotion_ack_missing", "promotion_record_for_different_generation"}
        or isinstance(owner_ticket, bool)
        or not isinstance(owner_ticket, int)
        or old_fault.get("severity") != "warning"
    ):
        return False
    expected = digest({
        "source_id": source_id,
        "stage": stage,
        "reason": reason,
        "owner_ticket": owner_ticket,
        "failure_identity": generation_id,
    })
    return old_fault.get("fault_key") == expected


def recovery_evidence(
    old_fault: dict[str, Any], source: dict[str, Any] | None, current_faults: list[dict[str, Any]],
    receipt: dict[str, Any], processor_workflow_path: str, processor_workflow_events: set[str],
    promotion_workflow_path: str = "", promotion_workflow_events: set[str] | None = None,
    maximum_future_skew: int = 0, collector_workflow_path: str = "",
    collector_workflow_events: set[str] | None = None,
) -> dict[str, Any] | None:
    if source is None or receipt.get("execution_mode") != "live":
        return None
    observation = source.get("observation", {})
    processor = source.get("processor", {})
    stage = old_fault.get("stage")
    observed_live = (
        observation.get("state") == "fresh"
        and observation.get("execution_mode") == "live"
        and bool(observation.get("producer_run_id"))
        and bool(observation.get("refresh_evidence_sha256"))
    )
    stage_faults = [row for row in current_faults if row.get("source_id") == old_fault.get("source_id")]
    if stage in {"schedule", "collector", "observation"}:
        cleared = observed_live and not any(row.get("stage") in {"schedule", "collector", "observation"} and row.get("severity") == "error" for row in stage_faults)
    elif stage in {"processor", "checkpoint", "candidate"}:
        cleared = (
            observed_live
            and processor.get("state") in {"ready", "no-change"}
            and not any(row.get("stage") in {"processor", "checkpoint", "candidate"} and row.get("severity") == "error" for row in stage_faults)
        )
    elif stage == "processor-execution":
        execution = processor.get("latest_successful_execution_run") if isinstance(processor.get("latest_successful_execution_run"), dict) else {}
        run_attempt = execution.get("run_attempt")
        execution_order = None
        prior_order = None
        try:
            execution_order = (
                parse_time(execution.get("run_started_at"), "processor_success.run_started_at"),
                int(execution.get("run_id", "")),
                run_attempt,
            )
            identity = old_fault.get("execution_identity")
            if isinstance(identity, dict):
                prior_order = (
                    parse_time(identity.get("run_started_at"), "processor_failure.run_started_at"),
                    int(identity.get("run_id", "")),
                    int(identity.get("run_attempt")),
                )
            else:
                prior_order = None
        except (TypeError, ValueError):
            execution_order = None
            prior_order = None
        cleared = (
            execution.get("path") == processor_workflow_path
            and execution.get("event") in processor_workflow_events
            and execution.get("status") == "completed"
            and execution.get("conclusion") == "success"
            and isinstance(execution.get("run_id"), str)
            and execution.get("run_id", "").isdigit()
            and isinstance(run_attempt, int)
            and not isinstance(run_attempt, bool)
            and run_attempt > 0
            and isinstance(execution.get("workflow_id"), int)
            and not isinstance(execution.get("workflow_id"), bool)
            and execution.get("workflow_id", 0) > 0
            and execution.get("head_branch") == "main"
            and execution.get("repository") == receipt.get("repository")
            and execution.get("head_repository") == receipt.get("repository")
            and isinstance(execution.get("head_sha"), str)
            and re.fullmatch(r"[a-f0-9]{40,64}", execution["head_sha"])
            and execution_order is not None
            and (execution_order > prior_order if prior_order is not None else (
                execution == processor.get("latest_execution_run")
                and execution_order[0] > parse_time(old_fault.get("last_seen_at"), "fault.last_seen_at")
            ))
            and not any(row.get("stage") == "processor-execution" and row.get("severity") == "error" for row in stage_faults)
        )
    elif stage == "promotion-execution":
        execution = trusted_promotion_success(
            source, receipt, promotion_workflow_path, promotion_workflow_events or set(), maximum_future_skew,
        )
        identity = old_fault.get("execution_identity")
        prior_order = promotion_execution_order(identity) if isinstance(identity, dict) else None
        execution_order = promotion_execution_order(execution)
        prior_head_sha = identity.get("head_sha") if isinstance(identity, dict) else None
        cleared = (
            old_fault.get("reason") == "promotion_workflow_run_failed"
            and old_fault.get("severity") == "error"
            and execution is not None
            and execution_order is not None
            and prior_order is not None
            and isinstance(prior_head_sha, str)
            and re.fullmatch(r"[a-f0-9]{40,64}", prior_head_sha)
            and execution_order > prior_order
            and not any(row.get("stage") == "promotion-execution" and row.get("severity") == "error" for row in stage_faults)
        )
    elif stage == "collector-execution":
        collector = source.get("collector", {})
        latest = collector.get("latest_execution_run") if isinstance(collector, dict) else None
        execution = latest.get("latest_successful_execution_run") if isinstance(latest, dict) else None
        identity = old_fault.get("execution_identity")
        prior_order = promotion_execution_order(identity) if isinstance(identity, dict) else None
        execution_order = promotion_execution_order(execution)
        evidence = execution.get("attempt_evidence") if isinstance(execution, dict) else None
        repository = receipt.get("repository")
        try:
            evaluation_time = parse_time(receipt.get("evaluated_at"), "receipt.evaluated_at")
            if execution_order is not None and execution_order[0] > evaluation_time + dt.timedelta(seconds=maximum_future_skew):
                execution_order = None
        except ValueError:
            execution_order = None
        run_id = execution.get("run_id") if isinstance(execution, dict) else None
        attempt = execution.get("run_attempt") if isinstance(execution, dict) else None
        workflow_id = execution.get("workflow_id") if isinstance(execution, dict) else None
        jobs_endpoint = evidence.get("jobs_api_endpoint") if isinstance(evidence, dict) else None
        job_count = evidence.get("job_count") if isinstance(evidence, dict) else None
        jobs_sha256 = evidence.get("jobs_sha256") if isinstance(evidence, dict) else None
        prior_head_sha = identity.get("head_sha") if isinstance(identity, dict) else None
        expected_jobs_endpoint = (
            f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs"
            if isinstance(repository, str) and isinstance(run_id, str) and isinstance(attempt, int) else None
        )
        valid_success = bool(
            isinstance(execution, dict)
            and isinstance(repository, str)
            and repository
            and isinstance(collector_workflow_path, str)
            and bool(collector_workflow_path)
            and workflow_id and isinstance(workflow_id, int) and not isinstance(workflow_id, bool) and workflow_id > 0
            and execution.get("path") in {collector_workflow_path, f"{collector_workflow_path}@main", f"{collector_workflow_path}@refs/heads/main"}
            and execution.get("event") in (collector_workflow_events or set())
            and execution.get("status") == "completed"
            and execution.get("conclusion") == "success"
            and execution.get("disposition") == "success"
            and execution.get("head_branch") == "main"
            and isinstance(execution.get("head_sha"), str)
            and re.fullmatch(r"[a-f0-9]{40,64}", execution["head_sha"])
            and execution.get("repository") == repository
            and execution.get("head_repository") == repository
            and isinstance(run_id, str) and run_id.isdigit() and int(run_id) > 0
            and isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0
            and isinstance(jobs_endpoint, str) and jobs_endpoint == expected_jobs_endpoint
            and isinstance(job_count, int) and not isinstance(job_count, bool) and 1 <= job_count <= 500
            and isinstance(jobs_sha256, str) and bool(re.fullmatch(r"[a-f0-9]{64}", jobs_sha256))
            and execution_order is not None
            and prior_order is not None
            and isinstance(prior_head_sha, str)
            and re.fullmatch(r"[a-f0-9]{40,64}", prior_head_sha)
            and execution_order > prior_order
        )
        cleared = (
            old_fault.get("reason") in {
                "repeated_collector_execution_failures",
                "collector_run_attempt_unavailable",
            }
            and old_fault.get("severity") == "error"
            and valid_success
            and not any(
                row.get("stage") == "collector-execution"
                and row.get("reason") == "collector_run_attempt_unavailable"
                and row.get("severity") == "error"
                for row in current_faults
            )
        )
    elif stage in {"promotion", "publication"}:
        publication = source.get("canonical", {}).get("publication")
        relation = verified_already_canonical_relation(source, receipt) if (
            stage == "promotion"
            and old_fault.get("reason") in {"promotion_ack_missing", "promotion_record_for_different_generation"}
        ) else None
        processor_generation = source.get("processor", {}).get("generation_id")
        canonical_recovery = (
            relation is not None
            and old_fault.get("severity") == "warning"
            and old_fault.get("generation_id") == processor_generation == relation.get("generation_id")
            and already_canonical_fault_key_valid(old_fault, str(relation.get("generation_id", "")))
        )
        publication_recovery = (
            isinstance(publication, dict)
            and publication.get("status") == "read-back-confirmed"
            and publication.get("verified") is True
            and publication.get("publisher_run_verified") is True
            and publication.get("generation_id") == old_fault.get("generation_id")
            and not any(row.get("stage") in {"promotion", "publication"} and row.get("severity") == "error" for row in stage_faults)
        )
        cleared = canonical_recovery or publication_recovery
    else:
        cleared = False
    if not cleared:
        return None
    if stage == "promotion-execution":
        return {
            "verified": True,
            "stage": stage,
            "health_receipt_sha256": receipt["receipt_sha256"],
            "execution_run_id": execution["run_id"],
            "execution_run_attempt": execution["run_attempt"],
        }
    if stage == "collector-execution":
        return {
            "verified": True,
            "stage": stage,
            "health_receipt_sha256": receipt["receipt_sha256"],
            "execution_run_id": execution["run_id"],
            "execution_run_attempt": execution["run_attempt"],
            "attempt_jobs_sha256": evidence["jobs_sha256"],
            "fault_reason": old_fault["reason"],
        }
    if (
        stage == "promotion"
        and old_fault.get("reason") in {"promotion_ack_missing", "promotion_record_for_different_generation"}
        and relation is not None
        and canonical_recovery
    ):
        return {
            "verified": True,
            "stage": "promotion",
            "basis": "exact_processor_payload_already_canonical",
            "fault_reason": old_fault["reason"],
            "health_receipt_sha256": receipt["receipt_sha256"],
            "generation_id": relation["generation_id"],
            "checkpoint_sha256": relation["checkpoint_sha256"],
            "processor_run_id": relation["processor_run_id"],
            "processor_run_attempt": relation["processor_run_attempt"],
            "artifact_id": relation["artifact_id"],
            "output_bundle_sha256": relation["output_bundle_sha256"],
            "composed_registry_bytes": relation["composed_registry_bytes"],
            "composed_registry_sha256": relation["composed_registry_sha256"],
            "main_revision": relation["main_revision"],
            "main_manifest_sha256": relation["main_manifest_sha256"],
        }
    publication = source.get("canonical", {}).get("publication") if source is not None else None
    return {
        "verified": True,
        "stage": stage,
        "health_receipt_sha256": receipt["receipt_sha256"],
        "observed_at": observation.get("observed_at"),
        "producer_run_id": observation.get("producer_run_id"),
        "refresh_evidence_sha256": observation.get("refresh_evidence_sha256"),
        "generation_id": (
            publication.get("generation_id")
            if stage in {"promotion", "publication"} and isinstance(publication, dict)
            else processor.get("generation_id")
        ),
        "processor_status": processor.get("state"),
        "publication_revision": source.get("canonical", {}).get("publication", {}).get("publication_revision") if isinstance(source.get("canonical", {}).get("publication"), dict) else None,
        "publication_pointer_revision": source.get("canonical", {}).get("publication", {}).get("publication_pointer_revision") if isinstance(source.get("canonical", {}).get("publication"), dict) else None,
        "execution_run_id": execution.get("run_id") if stage == "processor-execution" else None,
        "execution_run_attempt": execution.get("run_attempt") if stage == "processor-execution" else None,
    }


def merge_faults(
    state: dict[str, Any], receipt: dict[str, Any], processor_workflow_path: str,
    processor_workflow_events: set[str], promotion_workflow_path: str = "",
    promotion_workflow_events: set[str] | None = None, maximum_future_skew: int = 0,
    collector_workflow_path: str = "", collector_workflow_events: set[str] | None = None,
) -> None:
    evaluated_at = receipt["evaluated_at"]
    now = parse_time(evaluated_at, "receipt.evaluated_at")
    source_by_id = {row["source_id"]: row for row in receipt.get("sources", []) if isinstance(row, dict)}
    incoming = current_faults_by_key(receipt)
    old_rows = state.get("faults", [])
    if not isinstance(old_rows, list):
        raise ValueError("health_fault_state_corrupt")
    by_key = {row["fault_key"]: dict(row) for row in old_rows if isinstance(row, dict) and isinstance(row.get("fault_key"), str)}
    if len(by_key) != len(old_rows):
        raise ValueError("health_fault_state_corrupt")
    for key, current in incoming.items():
        prior = by_key.get(key)
        same_receipt = prior is not None and prior.get("last_receipt_sha256") == receipt["receipt_sha256"]
        declared_generation = current.get("generation_id")
        if declared_generation is not None:
            if current.get("stage") not in {"promotion", "publication"} or not isinstance(declared_generation, str) or not DIGEST.fullmatch(declared_generation):
                raise ValueError("health_receipt_fault_generation_invalid")
            if current.get("reason") in {"promotion_ack_missing", "promotion_record_for_different_generation"}:
                if not already_canonical_fault_key_valid(current, declared_generation):
                    raise ValueError("health_receipt_fault_generation_mismatch")
        by_key[key] = {
            **current,
            "status": "open",
            "first_seen_at": prior.get("first_seen_at", evaluated_at) if prior else evaluated_at,
            "last_seen_at": evaluated_at,
            "observation_count": int(prior.get("observation_count", 0)) + (0 if same_receipt else 1) if prior else 1,
            "last_receipt_sha256": receipt["receipt_sha256"],
            "generation_id": declared_generation if declared_generation is not None else source_by_id.get(current["source_id"], {}).get("processor", {}).get("generation_id"),
            "producer_run_id": source_by_id.get(current["source_id"], {}).get("observation", {}).get("producer_run_id"),
            "recovery_evidence": None,
        }
    for key, prior in by_key.items():
        if key in incoming or prior.get("status") == "recovered":
            continue
        source = source_by_id.get(prior.get("source_id"))
        evidence = recovery_evidence(
            prior, source, list(incoming.values()), receipt, processor_workflow_path, processor_workflow_events,
            promotion_workflow_path, promotion_workflow_events, maximum_future_skew,
            collector_workflow_path, collector_workflow_events,
        )
        if evidence is not None:
            prior["status"] = "recovered"
            prior["recovery_evidence"] = evidence
        elif prior.get("status") == "open":
            prior["status"] = "recovery_pending_verification"
            prior["recovery_evidence"] = {
                "verified": False,
                "reason": "fault_absent_but_stage_specific_evidence_not_yet_sufficient",
                "checked_at": evaluated_at,
                "health_receipt_sha256": receipt["receipt_sha256"],
            }
    retention_start = now - dt.timedelta(days=3650)
    retained = [
        row for row in by_key.values()
        if row.get("status") != "recovered" or parse_time(row["last_seen_at"], "fault.last_seen_at") >= retention_start
    ]
    active = [row for row in retained if row.get("status") != "recovered"]
    if len(active) > MAX_RETAINED_FAULTS:
        raise ValueError("active_health_fault_capacity_exceeded")
    recovered = sorted(
        (row for row in retained if row.get("status") == "recovered"),
        key=lambda row: parse_time(row["last_seen_at"], "fault.last_seen_at"),
        reverse=True,
    )[: MAX_RETAINED_FAULTS - len(active)]
    state["faults"] = sorted(
        active + recovered,
        key=lambda row: (row["source_id"], row["stage"], row["fault_key"]),
    )


def publication_order(value: Any) -> tuple[dt.datetime, int, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        if value.get("publication_run_completion_basis") != "max_completed_at_all_jobs_exact_run_attempt":
            return None
        completed_at = parse_time(value.get("publication_run_jobs_completed_at"), "last_good.publication_run_jobs_completed_at")
        run_id = int(value.get("publication_run_id"))
        attempt = int(value.get("publication_run_attempt"))
    except (TypeError, ValueError):
        return None
    if run_id < 1 or attempt < 1:
        return None
    return completed_at, run_id, attempt


def same_publication_identity(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    keys = (
        "source_id", "generation_id", "publication_revision", "publication_pointer_revision", "artifact_identity",
    )
    return all(left.get(key) == right.get(key) for key in keys)


def choose_last_good(existing: Any, candidate: Any) -> Any:
    if not isinstance(candidate, dict) or candidate.get("status") != "read-back-confirmed" or candidate.get("verified") is not True:
        return existing
    candidate_order = publication_order(candidate)
    if existing is None:
        return candidate if candidate_order is not None else None
    existing_order = publication_order(existing)
    if existing_order is None:
        return candidate if same_publication_identity(existing, candidate) and candidate_order is not None else existing
    if candidate_order is not None and candidate_order > existing_order:
        return candidate
    return existing


def prune_receipts(directory: pathlib.Path, current_name: str, evaluated_at: dt.datetime, policy: dict[str, Any]) -> None:
    retention_start = evaluated_at - dt.timedelta(days=int(policy["receipt_retention_days"]))
    files = sorted(directory.glob("receipt-*.json"))
    keep: list[pathlib.Path] = []
    for path in files:
        value = load_json(path)
        date = parse_time(value.get("evaluated_at"), "stored_receipt.evaluated_at")
        if path.name == current_name or date >= retention_start:
            keep.append(path)
        else:
            path.unlink()
    if len(keep) > int(policy["maximum_retained_receipts"]):
        raise ValueError("health_receipt_retention_limit_exceeded")


def validate_link_contract_diagnostics(receipt: dict[str, Any]) -> None:
    """Validate the additive diagnostic view before any state-owner mutation."""
    sources = receipt.get("sources")
    if not isinstance(sources, list):
        raise ValueError("health_link_contract_sources_invalid")
    for source in sources:
        processor = source.get("processor") if isinstance(source, dict) else None
        if not isinstance(processor, dict):
            raise ValueError("health_link_contract_processor_invalid")
        projection = processor.get("link_contract_diagnostics")
        if projection is None:
            # Old sealed receipts predate this additive projection.
            details = processor.get("detail_records")
            if isinstance(details, list) and any(
                isinstance(detail, dict)
                and isinstance(detail.get("failure_diagnostic"), dict)
                and detail["failure_diagnostic"].get("code")
                == "resolved_link_operation_contract_unproven"
                and "contract_failure" in detail["failure_diagnostic"]
                for detail in details
            ):
                raise ValueError("health_link_contract_projection_missing_for_modern_detail")
            continue
        if not isinstance(projection, dict):
            raise ValueError("health_link_contract_projection_invalid")
        status = projection.get("status")
        applicability = projection.get("applicability")
        records = projection.get("records")
        if not isinstance(records, list) or len(records) > 128:
            raise ValueError("health_link_contract_projection_invalid")
        if status != "verified":
            expected_applicability = {
                "rejected": "unavailable",
                "unavailable": "unavailable",
                "not_applicable": "not_applicable",
            }.get(status)
            if (
                expected_applicability is None
                or records
                or projection.get("generation_id") is not None
                or projection.get("checkpoint_sha256") is not None
                or projection.get("producer") is not None
                or applicability != expected_applicability
            ):
                raise ValueError("health_link_contract_unverified_claim_invalid")
            continue

        reason_code = projection.get("reason_code")
        canonical = source.get("canonical") if isinstance(source, dict) else None
        current_evaluation = (
            canonical.get("current_candidate_evaluation") if isinstance(canonical, dict) else None
        )
        producer = projection.get("producer")
        locator = processor.get("output_artifact")
        generation_id = projection.get("generation_id")
        checkpoint_sha256 = projection.get("checkpoint_sha256")
        if not isinstance(current_evaluation, dict):
            raise ValueError("health_link_contract_evaluation_missing")
        if applicability == "current":
            allowed_contexts = {
                ("verified", None),
                ("rejected", "candidate_payload_requires_promotion"),
            }
            if (current_evaluation.get("status"), current_evaluation.get("reason_code")) not in allowed_contexts:
                raise ValueError("health_link_contract_current_context_invalid")
            if reason_code != current_evaluation.get("reason_code"):
                raise ValueError("health_link_contract_current_context_invalid")
        elif applicability == "historical":
            if (
                current_evaluation.get("status") != "rejected"
                or current_evaluation.get("reason_code") != "candidate_baseline_stale_for_current_main"
                or reason_code != "candidate_baseline_stale_for_current_main"
            ):
                raise ValueError("health_link_contract_historical_context_invalid")
        else:
            raise ValueError("health_link_contract_verified_applicability_invalid")
        if (
            not isinstance(producer, dict)
            or not isinstance(locator, dict)
            or projection.get("schema_version") != "datapan.upstream-catalogue-link-contract-diagnostics.v1"
            or generation_id != processor.get("generation_id")
            or checkpoint_sha256 != processor.get("checkpoint_sha256")
            or current_evaluation.get("producer") != producer
            or producer.get("generation_id") != generation_id
            or producer.get("checkpoint_sha256") != checkpoint_sha256
            or producer.get("bundle_manifest_sha256") != locator.get("bundle_manifest_sha256")
            or producer.get("artifact_id") != str(locator.get("artifact_id"))
            or producer.get("run_id") != str(locator.get("run_id"))
            or not isinstance(producer.get("head_sha"), str)
            or not re.fullmatch(r"[a-f0-9]{40,64}", producer["head_sha"])
        ):
            raise ValueError("health_link_contract_producer_binding_invalid")
        name_match = re.fullmatch(
            r"upstream-catalogue-processing-([0-9]{1,20})-([1-9][0-9]*)",
            str(locator.get("name", "")),
        )
        if (
            not name_match
            or name_match.group(1) != producer.get("run_id")
            or int(name_match.group(2)) != producer.get("run_attempt")
        ):
            raise ValueError("health_link_contract_producer_attempt_invalid")

        details = processor.get("detail_records")
        if not isinstance(details, list):
            raise ValueError("health_link_contract_detail_records_invalid")
        detail_by_id: dict[str, dict[str, Any]] = {}
        for detail in details:
            diagnostic = detail.get("failure_diagnostic") if isinstance(detail, dict) else None
            if (
                not isinstance(diagnostic, dict)
                or diagnostic.get("code") != "resolved_link_operation_contract_unproven"
                or diagnostic.get("phase") != "resolver"
            ):
                continue
            identity = detail.get("id")
            if not isinstance(identity, str) or not identity or identity in detail_by_id:
                raise ValueError("health_link_contract_detail_identity_invalid")
            detail_by_id[identity] = detail

        record_by_id: dict[str, dict[str, Any]] = {}
        for record in records:
            api_key = record.get("api_key") if isinstance(record, dict) else None
            identity = api_key.get("id") if isinstance(api_key, dict) else None
            if (
                not isinstance(identity, str) or not identity
                or api_key.get("provider") != "data.go.kr"
                or identity in record_by_id
            ):
                raise ValueError("health_link_contract_record_identity_invalid")
            detail = detail_by_id.get(identity)
            diagnostic = detail.get("failure_diagnostic") if isinstance(detail, dict) else None
            admitted_outcome = {
                "api_key": {"provider": "data.go.kr", "id": identity},
                "status": detail.get("status") if isinstance(detail, dict) else None,
                "source_sha256": detail.get("source_sha256") if isinstance(detail, dict) else None,
                "guide_sha256": detail.get("guide_sha256") if isinstance(detail, dict) else None,
            }
            if isinstance(detail, dict) and "failure_diagnostic" in detail:
                admitted_outcome["failure_diagnostic"] = detail["failure_diagnostic"]
            if isinstance(detail, dict) and "link_metadata" in detail:
                admitted_outcome["link_metadata"] = detail["link_metadata"]
            if (
                not isinstance(detail, dict)
                or record.get("worker_status") != detail.get("status")
                or record.get("source_sha256") != detail.get("source_sha256")
                or record.get("guide_sha256") != detail.get("guide_sha256")
                or not isinstance(detail.get("link_metadata"), dict)
                or not isinstance(record.get("worker_outcome_sha256"), str)
                or not DIGEST.fullmatch(record["worker_outcome_sha256"])
                or record.get("worker_outcome_sha256") != digest(admitted_outcome)
            ):
                raise ValueError("health_link_contract_record_binding_invalid")
            contract_failure = diagnostic.get("contract_failure") if isinstance(diagnostic, dict) else None
            if record.get("detail_status") == "verified":
                reason = contract_failure.get("reason") if isinstance(contract_failure, dict) else None
                mapping = LINK_CONTRACT_FAILURES.get(reason)
                expected = {
                    "version": 1,
                    "reason": reason,
                    "unresolved_requirements": list(mapping["unresolved_requirements"]) if mapping else None,
                    "next_action": mapping["next_action"] if mapping else None,
                }
                seoul_subject = identity == "15056854"
                if (
                    not mapping
                    or contract_failure != expected
                    or record.get("contract_failure") != expected
                    or record.get("next_action") != mapping["next_action"]
                    or reason == "no_reviewed_declaration" and seoul_subject
                    or reason in {"subject_binding_unproven", "declaration_evidence_rejected"} and not seoul_subject
                ):
                    raise ValueError("health_link_contract_failure_mapping_invalid")
            elif record.get("detail_status") == "legacy_detail_unknown":
                if (
                    contract_failure is not None
                    or "contract_failure" in record
                    or record.get("next_action") != "inspect_bound_validation_evidence"
                ):
                    raise ValueError("health_link_contract_legacy_projection_invalid")
            else:
                raise ValueError("health_link_contract_detail_status_invalid")
            record_by_id[identity] = record

        if set(record_by_id) != set(detail_by_id):
            raise ValueError("health_link_contract_projection_coverage_invalid")
        ordered = sorted(records, key=lambda row: (row["api_key"]["provider"], row["api_key"]["id"]))
        if records != ordered:
            raise ValueError("health_link_contract_projection_order_invalid")


def persist(receipt_path: pathlib.Path, state_root: pathlib.Path, policy_path: pathlib.Path, repository: str) -> dict[str, Any]:
    policy = load_json(policy_path)
    validate_schema(policy, POLICY_SCHEMA, "health_policy")
    receipt = load_json(receipt_path)
    validate_schema(receipt, RECEIPT_SCHEMA, "health_receipt")
    if receipt.get("execution_mode") != "live":
        raise ValueError("fixture_receipt_not_persistable")
    if not verify_seal(receipt, "receipt_sha256"):
        raise ValueError("health_receipt_digest_mismatch")
    validate_link_contract_diagnostics(receipt)
    state_policy = policy["health_state"]
    configured_root = pathlib.PurePosixPath(state_policy["root"])
    configured_marker = pathlib.PurePosixPath(state_policy["ownership_marker"])
    actual_parts = state_root.resolve().parts
    expected_parts = configured_root.parts
    if (
        not state_policy["root"]
        or configured_root.is_absolute()
        or ".." in expected_parts
        or len(actual_parts) < len(expected_parts)
        or actual_parts[-len(expected_parts):] != expected_parts
        or state_root.absolute() != state_root.resolve()
        or configured_marker.parent != configured_root
        or configured_marker.name != "ownership.json"
    ):
        raise ValueError("health_state_root_mismatch")
    evaluated_at = parse_time(receipt["evaluated_at"], "receipt.evaluated_at")
    receipt_name = receipt_file_name(receipt)
    receipt_dir = state_root / "receipts"
    state_path = state_root / "state.json"
    owner_path = state_root / pathlib.Path(state_policy["ownership_marker"]).name
    if state_path.is_symlink() or receipt_dir.is_symlink() or owner_path.is_symlink():
        raise ValueError("health_state_path_symlink")
    if owner_path.exists() and state_root.exists():
        unexpected = {path.name for path in state_root.iterdir()} - {owner_path.name, state_path.name, receipt_dir.name}
        if unexpected:
            raise ValueError("unexpected_health_state_root_entries")
    if receipt_dir.exists():
        if not receipt_dir.is_dir() or any(path.is_symlink() or not re.fullmatch(r"receipt-[A-Za-z0-9][A-Za-z0-9._-]{0,127}-[1-9][0-9]*-[a-f0-9]{16}\.json", path.name) for path in receipt_dir.iterdir()):
            raise ValueError("health_receipt_directory_corrupt")
    initialize_owner(state_root, policy, repository, receipt["evaluated_at"])
    if state_path.exists():
        state = load_json(state_path)
        validate_schema(state, STATE_SCHEMA, "health_state")
        if not verify_seal(state, "state_sha256"):
            raise ValueError("health_state_digest_mismatch")
        if any(state.get(key) != expected for key, expected in {
            "repository": repository, "branch": state_policy["branch"], "root": state_policy["root"],
        }.items()):
            raise ValueError("health_state_binding_mismatch")
    else:
        state = initial_state(repository, state_policy["branch"], state_policy["root"])
    existing_receipt = receipt_dir / receipt_name
    if existing_receipt.exists():
        old_receipt = load_json(existing_receipt)
        if old_receipt.get("receipt_sha256") != receipt["receipt_sha256"]:
            raise ValueError("health_receipt_filename_collision")
    else:
        atomic_json(existing_receipt, receipt)

    if state.get("updated_at") is not None and evaluated_at < parse_time(state["updated_at"], "health_state.updated_at"):
        prune_receipts(receipt_dir, receipt_name, evaluated_at, state_policy)
        return {
            "status": "stale_receipt_archived",
            "receipt_path": f"{state_policy['root']}/receipts/{receipt_name}",
            "receipt_sha256": receipt["receipt_sha256"],
            "state_sha256": state.get("state_sha256"),
            "fault_count": len(state.get("faults", [])),
            "observation_counts": {source_id: len(rows) for source_id, rows in state.get("observations_by_source", {}).items()},
            "state_status": state.get("state_status"),
        }

    merge_observations(state, receipt)
    merge_faults(
        state, receipt, policy["processor_state"]["workflow_path"],
        set(policy["processor_state"]["allowed_events"]),
        policy["promotion_state"]["promotion_workflow_path"],
        set(policy["processor_state"]["allowed_events"]),
        int(policy["clock"]["maximum_future_skew_seconds"]),
        policy["health_workflow"]["collector_workflow_path"],
        {"schedule", "workflow_dispatch"},
    )
    last_good = state.setdefault("last_good_by_source", {})
    for source in receipt.get("sources", []):
        candidate = source.get("canonical", {}).get("last_good")
        prior = last_good.get(source["source_id"])
        selected = choose_last_good(prior, candidate)
        if selected is not None:
            last_good[source["source_id"]] = selected
    state["updated_at"] = receipt["evaluated_at"]
    state["latest_receipt"] = {"path": f"receipts/{receipt_name}", "sha256": receipt["receipt_sha256"]}
    overall_states = {row.get("overall") for row in receipt.get("sources", [])}
    state["state_status"] = "blocked" if "blocked" in overall_states else "degraded" if "degraded" in overall_states else "healthy"
    state = seal(state, "state_sha256")
    validate_schema(state, STATE_SCHEMA, "health_state")
    atomic_json(state_path, state)
    prune_receipts(receipt_dir, receipt_name, evaluated_at, state_policy)
    validate_schema(load_json(owner_path), OWNER_SCHEMA, "state_owner")
    return {
        "status": "persisted",
        "receipt_path": f"{state_policy['root']}/receipts/{receipt_name}",
        "receipt_sha256": receipt["receipt_sha256"],
        "state_sha256": state["state_sha256"],
        "fault_count": len(state["faults"]),
        "observation_counts": {source_id: len(rows) for source_id, rows in state["observations_by_source"].items()},
        "state_status": state["state_status"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=pathlib.Path, required=True)
    parser.add_argument("--state-root", type=pathlib.Path, required=True)
    parser.add_argument("--policy", type=pathlib.Path, default=ROOT / "policy/upstream-catalogue-health.json")
    parser.add_argument("--repository", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = persist(args.receipt, args.state_root, args.policy, args.repository)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL persist upstream catalogue health: {type(exc).__name__}:{str(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
