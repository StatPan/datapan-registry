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


def recovery_evidence(
    old_fault: dict[str, Any], source: dict[str, Any] | None, current_faults: list[dict[str, Any]],
    receipt: dict[str, Any], processor_workflow_path: str, processor_workflow_events: set[str],
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
    elif stage in {"promotion", "publication"}:
        publication = source.get("canonical", {}).get("publication")
        cleared = (
            isinstance(publication, dict)
            and publication.get("status") == "read-back-confirmed"
            and publication.get("verified") is True
            and publication.get("publisher_run_verified") is True
            and not any(row.get("stage") in {"promotion", "publication"} and row.get("severity") == "error" for row in stage_faults)
        )
    else:
        cleared = False
    if not cleared:
        return None
    return {
        "verified": True,
        "stage": stage,
        "health_receipt_sha256": receipt["receipt_sha256"],
        "observed_at": observation.get("observed_at"),
        "producer_run_id": observation.get("producer_run_id"),
        "refresh_evidence_sha256": observation.get("refresh_evidence_sha256"),
        "generation_id": processor.get("generation_id"),
        "processor_status": processor.get("state"),
        "publication_revision": source.get("canonical", {}).get("publication", {}).get("publication_revision") if isinstance(source.get("canonical", {}).get("publication"), dict) else None,
        "publication_pointer_revision": source.get("canonical", {}).get("publication", {}).get("publication_pointer_revision") if isinstance(source.get("canonical", {}).get("publication"), dict) else None,
        "execution_run_id": execution.get("run_id") if stage == "processor-execution" else None,
        "execution_run_attempt": execution.get("run_attempt") if stage == "processor-execution" else None,
    }


def merge_faults(
    state: dict[str, Any], receipt: dict[str, Any], processor_workflow_path: str,
    processor_workflow_events: set[str],
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
        by_key[key] = {
            **current,
            "status": "open",
            "first_seen_at": prior.get("first_seen_at", evaluated_at) if prior else evaluated_at,
            "last_seen_at": evaluated_at,
            "observation_count": int(prior.get("observation_count", 0)) + (0 if same_receipt else 1) if prior else 1,
            "last_receipt_sha256": receipt["receipt_sha256"],
            "generation_id": source_by_id.get(current["source_id"], {}).get("processor", {}).get("generation_id"),
            "producer_run_id": source_by_id.get(current["source_id"], {}).get("observation", {}).get("producer_run_id"),
            "recovery_evidence": None,
        }
    for key, prior in by_key.items():
        if key in incoming or prior.get("status") == "recovered":
            continue
        source = source_by_id.get(prior.get("source_id"))
        evidence = recovery_evidence(
            prior, source, list(incoming.values()), receipt, processor_workflow_path, processor_workflow_events,
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


def persist(receipt_path: pathlib.Path, state_root: pathlib.Path, policy_path: pathlib.Path, repository: str) -> dict[str, Any]:
    policy = load_json(policy_path)
    validate_schema(policy, POLICY_SCHEMA, "health_policy")
    receipt = load_json(receipt_path)
    validate_schema(receipt, RECEIPT_SCHEMA, "health_receipt")
    if receipt.get("execution_mode") != "live":
        raise ValueError("fixture_receipt_not_persistable")
    if not verify_seal(receipt, "receipt_sha256"):
        raise ValueError("health_receipt_digest_mismatch")
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
