#!/usr/bin/env python3
"""Prepare a fully authenticated same-observation B derivation before claim.

The command is an intake coordinator only. It reads the durable B index, exact
Actions attempts/artifacts, the current main/LFS identity, and one frozen C
journal row. It writes local temporary evidence for the normal processor; it
never changes a checkpoint, consumes detail budget, creates a PR, or publishes.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import re
import shutil
import sys
from collections.abc import Mapping
from typing import Any


def load_module(path: pathlib.Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("derivation_validator_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_json(path: pathlib.Path, maximum_bytes: int = 16 * 1024 * 1024) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum_bytes:
        raise ValueError("derivation_input_file_invalid")
    return json.loads(path.read_bytes())


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def run_git_bytes(root: pathlib.Path, *argv: str) -> bytes:
    import subprocess

    result = subprocess.run(("git", *argv), cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise ValueError("derivation_git_identity_unavailable")
    return result.stdout


def run_git(root: pathlib.Path, *argv: str) -> str:
    """Read textual Git metadata; use run_git_bytes for content-addressed files."""
    try:
        return run_git_bytes(root, *argv).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("derivation_git_identity_unavailable") from exc


def admission_for_checkpoint(
    checkpoint: Mapping[str, Any], index: Mapping[str, Any], handoff: Any,
) -> dict[str, Any]:
    ledger = index.get("collector_handoff")
    if not isinstance(ledger, Mapping):
        raise ValueError("derivation_admission_ledger_missing")
    validated = handoff.validate_ledger(ledger)
    observation = checkpoint.get("last_observation")
    refs = checkpoint.get("input_artifacts")
    if not isinstance(observation, Mapping) or not isinstance(refs, list):
        raise ValueError("derivation_parent_admission_identity_missing")
    matches = [
        row for row in validated["admitted_observations"]
        if row.get("producer_run_id") == str(observation.get("producer_run_id") or "")
        and row.get("refresh_evidence_sha256") == observation.get("refresh_evidence_sha256")
        and any(
            isinstance(ref, Mapping)
            and str(ref.get("run_id")) == row["producer_run_id"]
            and str(ref.get("artifact_id")) == row["artifact_id"]
            and ref.get("evidence_sha256") == row["refresh_evidence_sha256"]
            for ref in refs
        )
    ]
    if len(matches) != 1:
        raise ValueError("derivation_parent_admission_ambiguous_or_missing")
    return dict(matches[0])


def authenticate_parent_bundle(
    root: pathlib.Path, repository: str, default_branch: str, state_root: pathlib.Path,
    checkpoint: Mapping[str, Any], bundle_dir: pathlib.Path, runner: Any,
    *, canonical_context: Mapping[str, Any] | None = None,
    canonical_parent_authorization: "CanonicalParentAuthorization | None" = None,
) -> dict[str, Any]:
    """Bind one terminal checkpoint to its exact B attempt and archived outputs.

    A canonical-parent authorization is created only after the selected C row's
    current payload, merged PR readback, and exact successful acknowledgement
    have been authenticated. It can exempt only that same B generation from
    historical-baseline membership when the complete B output is already the
    current C payload. Other parents keep ordinary baseline validation.
    """
    run_id, attempt, _name = runner.processor_attempt_from_locator(checkpoint)
    run = runner.processor_run_api(root, repository, run_id, attempt)
    runner.validate_trusted_processor_run(
        run, repository=repository, run_id=run_id, attempt=attempt,
        default_branch=default_branch,
    )
    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise ValueError("derivation_parent_artifact_locator_missing")
    artifact = runner.processor_artifact_api(root, repository, run_id, str(locator.get("artifact_id") or ""))
    if artifact is None:
        raise ValueError("derivation_parent_artifact_missing")
    artifact = runner.validate_processor_artifact_metadata(
        artifact, checkpoint, run, repository=repository,
    )
    composition_schema = runner.load_object(root / "schemas/datapan.catalogue-composition-receipt.v1.schema.json")
    composition_helper = runner.load_canonical_update_pr(root)
    allow_terminal_noop = False
    authorized_candidate: Mapping[str, Any] | None = None
    if (
        canonical_parent_authorization is not None
        and checkpoint.get("generation_id") == canonical_parent_authorization.generation_id
    ):
        if not isinstance(canonical_context, Mapping):
            raise ValueError("canonical_parent_current_context_missing")
        identity = canonical_context.get("identity")
        authorized_candidate = canonical_parent_authorization.candidate
        if not isinstance(identity, Mapping) or not isinstance(authorized_candidate, Mapping):
            raise ValueError("canonical_parent_authorization_invalid")
        expected_registry = (
            identity.get("registry_path"), identity.get("registry_bytes"), identity.get("registry_sha256"),
        )
        candidate_registry = (
            authorized_candidate.get("registry_path"), authorized_candidate.get("registry_bytes"),
            authorized_candidate.get("registry_sha256"),
        )
        if (
            authorized_candidate.get("generation_id") != checkpoint.get("generation_id")
            or candidate_registry != expected_registry
            or not isinstance(authorized_candidate.get("composition_receipt_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", authorized_candidate["composition_receipt_sha256"])
            or canonical_parent_authorization.readback.get("canonical_producer_generation_id")
            != checkpoint.get("generation_id")
        ):
            raise ValueError("canonical_parent_authorization_identity_mismatch")
        allow_terminal_noop = True
    validated = runner.validate_processor_bundle(
        checkpoint, bundle_dir, composition_schema, composition_helper, root=root,
        producer_head_sha=str(run["head_sha"]),
        canonical_context=canonical_context,
        allow_terminal_noop=allow_terminal_noop,
    )
    if validated.get("status") not in {"ready", "no-change"}:
        raise ValueError("derivation_parent_is_not_reviewable")
    if allow_terminal_noop:
        assert isinstance(canonical_context, Mapping)
        identity = canonical_context["identity"]
        candidate_registry = (
            authorized_candidate.get("registry_path"), authorized_candidate.get("registry_bytes"),
            authorized_candidate.get("registry_sha256"),
        )
        validated_registry = (
            validated.get("registry_path"), validated.get("registry_bytes"),
            validated.get("registry_sha256"),
        )
        current_registry = (
            identity.get("registry_path"), identity.get("registry_bytes"), identity.get("registry_sha256"),
        )
        if (
            validated_registry != candidate_registry
            or validated_registry != current_registry
            or validated.get("composition_receipt_sha256")
            != authorized_candidate.get("composition_receipt_sha256")
        ):
            raise ValueError("canonical_parent_bundle_does_not_match_authenticated_C_output")
    return dict(validated)


class CanonicalParentAuthorization:
    """Proof that one B checkpoint is the exact, live-authenticated C producer."""

    __slots__ = ("generation_id", "candidate", "readback")

    def __init__(
        self, *, generation_id: str, candidate: Mapping[str, Any], readback: Mapping[str, Any],
    ) -> None:
        self.generation_id = generation_id
        self.candidate = candidate
        self.readback = readback


def authenticate_live_canonical_parent(
    root: pathlib.Path,
    repository: str,
    default_branch: str,
    checkpoint: Mapping[str, Any],
    row: Mapping[str, Any],
    readback: Mapping[str, Any],
    canonical_context: Mapping[str, Any],
    runner: Any,
    derivation: Any,
    *,
    now: dt.datetime,
) -> CanonicalParentAuthorization:
    """Authenticate the selected C row before using it to validate parent B."""
    candidate = row.get("candidate") if isinstance(row.get("candidate"), Mapping) else None
    identity = canonical_context.get("identity") if isinstance(canonical_context, Mapping) else None
    if not isinstance(candidate, Mapping) or not isinstance(identity, Mapping):
        raise ValueError("canonical_parent_selected_row_or_context_invalid")
    generation_id = candidate.get("generation_id")
    candidate_repository = candidate.get("repository")
    if not isinstance(candidate_repository, str):
        raise ValueError("canonical_parent_selected_row_identity_mismatch")
    if (
        not isinstance(generation_id, str)
        or checkpoint.get("generation_id") != generation_id
        or candidate_repository.casefold() != repository.casefold()
        or candidate.get("source_id") != checkpoint.get("source_id")
        or candidate.get("scope") != checkpoint.get("source_scope")
        or (
            candidate.get("registry_path"), candidate.get("registry_bytes"), candidate.get("registry_sha256"),
        ) != (
            identity.get("registry_path"), identity.get("registry_bytes"), identity.get("registry_sha256"),
        )
        or readback.get("canonical_producer_generation_id") != generation_id
        or readback.get("repository") != repository
        or readback.get("source_id") != candidate.get("source_id")
        or readback.get("source_scope") != candidate.get("scope")
        or readback.get("registry_path") != candidate.get("registry_path")
        or readback.get("registry_bytes") != candidate.get("registry_bytes")
        or readback.get("registry_sha256") != candidate.get("registry_sha256")
        or readback.get("manifest_sha256") != candidate.get("manifest_sha256")
        or readback.get("pr_number") != row.get("pr", {}).get("number")
        or readback.get("pr_branch") != row.get("ownership", {}).get("branch")
        or readback.get("pr_head_sha") != row.get("ownership", {}).get("expected_head_sha")
        or readback.get("pr_body_sha256") != row.get("ownership", {}).get("body_sha256")
        or readback.get("merge_sha") != row.get("pr", {}).get("merge_commit_sha")
        or not isinstance(candidate.get("composition_receipt_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", candidate["composition_receipt_sha256"])
    ):
        raise ValueError("canonical_parent_selected_row_identity_mismatch")

    number = readback.get("pr_number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError("canonical_parent_PR_identity_invalid")
    live = runner.gh_pr_readback(root, repository, number)
    if not isinstance(live, Mapping):
        raise ValueError("canonical_parent_PR_live_readback_mismatch")
    merge_commit = live.get("mergeCommit") if isinstance(live, Mapping) else None
    merge_sha = merge_commit.get("oid") if isinstance(merge_commit, Mapping) else None
    body = live.get("body") if isinstance(live, Mapping) else None
    if (
        live.get("number") != number
        or live.get("state") != "MERGED"
        or live.get("repository") != repository
        or live.get("headRepository") != repository
        or live.get("headRefName") != readback.get("pr_branch")
        or live.get("headRefOid") != readback.get("pr_head_sha")
        or live.get("baseRefName") != default_branch
        or merge_sha != readback.get("merge_sha")
        or live.get("merged") is not True
        or not isinstance(body, str)
        or sha256_bytes(body.encode("utf-8")) != readback.get("pr_body_sha256")
        or body != row.get("ownership", {}).get("body")
    ):
        raise ValueError("canonical_parent_PR_live_readback_mismatch")
    exact_ack_run(
        root, repository, row,
        runner.load_object(root / "policy/upstream-catalogue-health.json"), now, derivation,
    )
    import subprocess

    ancestry = subprocess.run(
        ("git", "merge-base", "--is-ancestor", str(readback.get("merge_sha") or ""), str(identity.get("main_sha") or "")),
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if ancestry.returncode != 0:
        raise ValueError("canonical_parent_merge_is_not_in_authenticated_current_main")
    return CanonicalParentAuthorization(
        generation_id=generation_id,
        candidate=copy.deepcopy(dict(candidate)),
        readback=copy.deepcopy(dict(readback)),
    )


def download_parent_bundle(
    root: pathlib.Path, repository: str, default_branch: str, state_root: pathlib.Path,
    checkpoint: Mapping[str, Any], destination: pathlib.Path, runner: Any,
) -> pathlib.Path:
    run_id, _attempt, _name = runner.processor_attempt_from_locator(checkpoint)
    run = runner.processor_run_api(root, repository, run_id, _attempt)
    runner.validate_trusted_processor_run(
        run, repository=repository, run_id=run_id, attempt=_attempt,
        default_branch=default_branch,
    )
    locator = checkpoint.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise ValueError("derivation_parent_artifact_locator_missing")
    artifact = runner.processor_artifact_api(root, repository, run_id, str(locator.get("artifact_id") or ""))
    if artifact is None:
        raise ValueError("derivation_parent_artifact_missing")
    artifact = runner.validate_processor_artifact_metadata(artifact, checkpoint, run, repository=repository)
    runner.download_processor_artifact(root, repository, artifact, destination)
    return destination


def local_bundle_matches_checkpoint(checkpoint: Mapping[str, Any], bundle_dir: pathlib.Path) -> bool:
    """Treat the workflow cache as an optimization, never as a parent selector."""
    outputs = checkpoint.get("output_digests")
    if not isinstance(outputs, list) or not outputs:
        return False
    for entry in outputs:
        if not isinstance(entry, Mapping):
            return False
        relative = pathlib.PurePosixPath(str(entry.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts or "\\" in str(entry.get("path") or ""):
            return False
        path = bundle_dir.joinpath(*relative.parts)
        if (
            path.is_symlink() or not path.is_file()
            or path.stat().st_size != entry.get("bytes")
            or sha256_bytes(path.read_bytes()) != entry.get("sha256")
        ):
            return False
    return True


def exact_ack_run(
    root: pathlib.Path, repository: str, row: Mapping[str, Any],
    health_policy: Mapping[str, Any], now: dt.datetime, derivation: Any,
) -> None:
    """Require the exact successful C workflow attempt that recorded the merge."""
    derivation.authenticate_canonical_merge_ack(root, repository, row, health_policy, now=now)


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    main_root = args.main_root.resolve()
    source_root = args.source_root.resolve()
    state_root = args.state_dir.resolve()
    output_root = args.output_dir.resolve()
    repository = args.repository
    if not re.fullmatch(r"[^/]+/[^/]+", repository):
        raise ValueError("derivation_repository_invalid")
    if not args.target_generation_id:
        return {"eligible": False, "reason": "new_observation_no_existing_processor_parent"}

    runner = load_module(main_root / "scripts/run-canonical-update-promotion.py", "derivation_promotion_runner")
    handoff = load_module(main_root / "scripts/upstream_catalogue_handoff.py", "derivation_handoff")
    health = load_module(main_root / "scripts/check-upstream-catalogue-health.py", "derivation_health_trust")
    derivation = load_module(main_root / "scripts/upstream_catalogue_derivation.py", "derivation_schema")
    checkpoint_schema = main_root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
    source_state = state_root / "sources/data_go_kr"
    index = load_json(source_state / "index.json", 2 * 1024 * 1024)
    if index.get("schema_version") != "datapan.upstream-catalogue-checkpoint.v1" or not isinstance(index.get("generations"), list):
        raise ValueError("derivation_state_index_invalid")
    indexed_ids = {
        row.get("generation_id") for row in index["generations"] if isinstance(row, Mapping)
    }
    target_id = args.target_generation_id
    if target_id not in indexed_ids:
        # A target ID is supplied by the authenticated processor selector. A
        # selected-but-missing checkpoint is state corruption, not permission
        # to fall back to the legacy generation identity.
        raise ValueError("selected_generation_not_durable")
    target_path = source_state / "generations" / f"{target_id}.json"
    target = runner.verify_processor_checkpoint(load_json(target_path, 262144), checkpoint_schema)
    if target.get("source_id") != "data_go_kr" or not isinstance(target.get("source_scope"), str) or not target.get("source_scope"):
        raise ValueError("derivation_selected_source_mismatch")
    target_inputs = target.get("generation_inputs") if isinstance(target.get("generation_inputs"), Mapping) else {}
    stored_envelope_value = target_inputs.get("same_observation_derivation")
    stored_envelope = (
        derivation.validate_derivation_envelope(stored_envelope_value)
        if stored_envelope_value is not None else None
    )

    def unavailable(reason: str) -> dict[str, Any]:
        if stored_envelope is not None:
            # Never let a selected derived checkpoint downgrade to the legacy
            # B identity merely because one of its C/A prerequisites is now
            # absent. The failing step prevents the workflow claim/CAS step.
            raise ValueError(f"active_derivation_prerequisite_unavailable:{reason}")
        return {"eligible": False, "reason": reason}

    candidate_path = args.candidate.resolve()
    evidence_path = args.refresh_evidence.resolve()
    diff_path = args.diff.resolve()
    admission_envelope = load_json(args.collector_admission, 64 * 1024)
    evidence = load_json(evidence_path, 256 * 1024 * 1024)
    candidate_sha256 = sha256_bytes(candidate_path.read_bytes())
    evidence_sha256 = sha256_bytes(evidence_path.read_bytes())
    diff_sha256 = sha256_bytes(diff_path.read_bytes())
    authenticated_metadata = {
        "repository": repository,
        "producer_run_id": args.producer_run_id,
        "run_attempt": args.producer_run_attempt,
        "head_sha": args.producer_head_sha,
        "run_started_at": args.producer_run_started_at,
        "run_completed_at": args.producer_run_completed_at,
        "observe_job_started_at": args.producer_observe_job_started_at,
        "observe_job_completed_at": args.producer_observe_job_completed_at,
        "artifact_id": args.producer_artifact_id,
        "artifact_name": args.producer_artifact_name,
        "artifact_expires_at": args.producer_artifact_expires_at,
        "artifact_created_at": args.producer_artifact_created_at,
        "artifact_digest_sha256": args.producer_artifact_digest_sha256,
        "artifact_size_bytes": args.producer_artifact_size_bytes,
        "event": args.producer_event,
    }
    if (
        set(authenticated_metadata) != handoff.COLLECTOR_ADMISSION_AUTHENTICATED_FIELDS
        or any(value is None or value == "" for value in authenticated_metadata.values())
        or not args.collector_archive
        or not args.producer_run_url
    ):
        raise ValueError("derivation_collector_admission_authenticated_metadata_missing")
    admission = handoff.validate_collector_admission_envelope(
        admission_envelope,
        expected=authenticated_metadata,
        evidence=evidence,
        candidate_sha256=candidate_sha256,
        evidence_sha256=evidence_sha256,
        diff_sha256=diff_sha256,
        source_id="data_go_kr",
        archive_path=args.collector_archive,
        producer_run_url=args.producer_run_url,
        now=dt.datetime.now(dt.timezone.utc),
    )
    if not isinstance(evidence, Mapping) or evidence.get("collection", {}).get("succeeded") is not True:
        return unavailable("current_observation_not_successful")
    producer_head = str(admission.get("head_sha") or "")
    if run_git(source_root, "rev-parse", "HEAD") != producer_head:
        raise ValueError("derivation_source_checkout_does_not_match_producer_head")
    policy_bytes = (source_root / "policy/source-refresh.json").read_bytes()
    adapter_bytes = (source_root / "data/provider-index.json").read_bytes()
    for relative, working_bytes in (
        ("policy/source-refresh.json", policy_bytes),
        ("data/provider-index.json", adapter_bytes),
    ):
        if run_git_bytes(main_root, "show", f"{producer_head}:{relative}") != working_bytes:
            raise ValueError("derivation_original_A_policy_or_provider_identity_unbound")
    original = derivation.validate_original_observation({
        "source_id": "data_go_kr",
        "source_scope": target["source_scope"],
        "producer_run_id": str(admission["producer_run_id"]),
        "producer_run_attempt": admission["run_attempt"],
        "producer_head_sha": admission["head_sha"],
        "producer_artifact_id": str(admission["artifact_id"]),
        "producer_artifact_name": admission["artifact_name"],
        "producer_artifact_sha256": admission["artifact_digest_sha256"],
        "observed_at": admission["observed_at"],
        "observation_count": target.get("observation_count"),
        "original_baseline_sha256": sha256_bytes(source_root.joinpath("data/data-go-kr.registry.json").read_bytes()),
        "candidate_sha256": candidate_sha256,
        "evidence_sha256": evidence_sha256,
        "diff_sha256": sha256_bytes(diff_path.read_bytes()),
        "source_policy_sha256": sha256_bytes(policy_bytes),
        "provider_index_sha256": sha256_bytes(adapter_bytes),
    })
    target_admission = admission_for_checkpoint(target, index, handoff)
    expected_durable_admission = {
        "admission_id": handoff.admission_id(
            str(admission["producer_run_id"]), admission["run_attempt"], str(admission["refresh_evidence_sha256"]),
        ),
        "producer_run_id": str(admission["producer_run_id"]),
        "run_attempt": admission["run_attempt"],
        "head_sha": admission["head_sha"],
        "run_started_at": admission["run_started_at"],
        "artifact_id": str(admission["artifact_id"]),
        "artifact_name": admission["artifact_name"],
        "artifact_expires_at": admission["artifact_expires_at"],
        "artifact_digest_sha256": admission["artifact_digest_sha256"],
        "artifact_size_bytes": admission["artifact_size_bytes"],
        "refresh_evidence_sha256": evidence_sha256,
        "observed_at": admission["observed_at"],
        "candidate_sha256": candidate_sha256,
    }
    admission_mismatches = [
        key for key, value in expected_durable_admission.items()
        if target_admission.get(key) != value
    ]
    if admission_mismatches:
        raise ValueError("derivation_current_admission_durable_row_mismatch")
    target_original = derivation.original_observation_from_checkpoint(target, target_admission)
    if target_original != original:
        return unavailable("selected_parent_is_different_source_observation")
    active_statuses = {"queued", "validating", "enriching", "composing", "retry"}
    active_resume = stored_envelope is not None and target.get("status") in active_statuses
    outcome = target.get("outcome") if isinstance(target.get("outcome"), Mapping) else {}
    ready_has_detail_work = (
        target.get("status") == "ready"
        and int(outcome.get("detail_retry_count", 0) or 0) > 0
    )
    if stored_envelope is not None and stored_envelope["original_observation"] != original:
        raise ValueError("active_derivation_original_observation_mismatch")
    if target.get("status") not in {"ready", "no-change"} and not active_resume:
        if stored_envelope is not None:
            raise ValueError("active_derivation_status_not_resumable")
        return {"eligible": False, "reason": "selected_parent_not_reviewable"}
    selected_target = target
    prior_outcome = target.get("outcome") if isinstance(target.get("outcome"), Mapping) else {}
    has_work = active_resume or int(prior_outcome.get("detail_retry_count", 0) or 0) > 0

    journal, journal_ref_sha = runner.load_promotion_journal_snapshot(main_root)
    if not isinstance(journal, Mapping) or not isinstance(journal_ref_sha, str):
        return unavailable("canonical_journal_unavailable")
    current_head = run_git(main_root, "rev-parse", "HEAD")
    canonical_context = runner.authenticated_current_canonical_context(main_root, current_head)
    current_identity = canonical_context.get("identity") if isinstance(canonical_context, Mapping) else None
    if not isinstance(current_identity, Mapping):
        raise ValueError("authenticated_current_canonical_context_invalid")
    active_rows = [
        row for row in journal.get("records", []) if isinstance(row, Mapping)
        and row.get("superseded_by") is None
        and row.get("status") in {"merged", "publication-pending", "published", "read-back-confirmed"}
        and isinstance(row.get("candidate"), Mapping)
        and row["candidate"].get("repository", "").casefold() == repository.casefold()
        and row["candidate"].get("source_id") == original["source_id"]
        and row["candidate"].get("scope") == original["source_scope"]
        and row["candidate"].get("registry_path") == current_identity["registry_path"]
        and row["candidate"].get("registry_bytes") == current_identity["registry_bytes"]
        and row["candidate"].get("registry_sha256") == current_identity["registry_sha256"]
    ]
    same_observation_rows: list[tuple[int, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for row in active_rows:
        generation_id = str(row["candidate"].get("generation_id") or "")
        if generation_id not in indexed_ids:
            continue
        cp = runner.verify_processor_checkpoint(
            load_json(source_state / "generations" / f"{generation_id}.json", 262144), checkpoint_schema,
        )
        cp_admission = admission_for_checkpoint(cp, index, handoff)
        if derivation.original_observation_from_checkpoint(cp, cp_admission) != original:
            continue
        index_rows = journal.get("records", [])
        position = next((number for number, candidate in enumerate(index_rows) if candidate is row), -1)
        if position < 0:
            raise ValueError("canonical_journal_row_locator_unavailable")
        readback = derivation.canonical_parent_readback_reference(
            journal, journal_ref_sha=journal_ref_sha, record_index=position,
        )
        same_observation_rows.append((position, dict(row), cp, readback))
    if len(same_observation_rows) != 1:
        if len(same_observation_rows) > 1:
            raise ValueError("same_observation_canonical_lineage_ambiguous")
        return unavailable("no_same_observation_merged_canonical_lineage")

    canonical_index, canonical_row, canonical_checkpoint, readback = same_observation_rows[0]
    now = dt.datetime.now(dt.timezone.utc)
    canonical_parent_authorization: CanonicalParentAuthorization | None = None
    if (
        stored_envelope is not None and ready_has_detail_work
        and canonical_checkpoint.get("generation_id") != target_id
    ):
        # A ready derived generation with pending detail can be resumed in
        # place only while its own recorded C baseline remains current. When
        # that exact generation is itself the newly merged canonical producer,
        # it becomes the parent of a fresh derivative against the new C
        # baseline; replaying its older envelope would reject that legitimate
        # same-A continuation.
        active_resume = True
    if active_resume:
        assert stored_envelope is not None
        expected_readback = stored_envelope["canonical_parent_readback"]
        if (
            expected_readback["canonical_producer_generation_id"] != canonical_checkpoint["generation_id"]
            or expected_readback["journal_record_index"] != canonical_index
            or expected_readback["repository"] != readback["repository"]
            or expected_readback["pr_number"] != readback["pr_number"]
            or expected_readback["pr_branch"] != readback["pr_branch"]
            or expected_readback["pr_body_sha256"] != readback["pr_body_sha256"]
            or expected_readback["pr_head_sha"] != readback["pr_head_sha"]
            or expected_readback["merge_sha"] != readback["merge_sha"]
        ):
            raise ValueError("active_derivation_canonical_snapshot_changed")
        try:
            stored_row = derivation.validate_readback_against_journal(
                stored_envelope, journal, journal_ref_sha=journal_ref_sha,
                allow_monotonic_successor=True,
            )
        except derivation.DerivationError as exc:
            raise ValueError("active_derivation_canonical_snapshot_changed") from exc
        if stored_row != canonical_row:
            raise ValueError("active_derivation_canonical_row_changed")
        baseline = stored_envelope["composition_baseline"]
        if (
            current_identity["registry_path"] != baseline["registry_path"]
            or current_identity["registry_bytes"] != baseline["registry_bytes"]
            or current_identity["registry_sha256"] != baseline["registry_sha256"]
        ):
            raise ValueError("active_derivation_current_canonical_payload_changed")
        current_bytes = main_root / ".datapan/current-canonical" / pathlib.Path(*baseline["registry_path"].split("/"))
        if current_bytes.is_symlink() or not current_bytes.is_file():
            raise ValueError("active_derivation_composition_baseline_unavailable")
        composition_bytes = current_bytes.read_bytes()
        if (len(composition_bytes), sha256_bytes(composition_bytes)) != (
            baseline["registry_bytes"], baseline["registry_sha256"],
        ):
            raise ValueError("active_derivation_composition_baseline_changed")

        baseline_main = baseline["main_sha"]
        current_main = run_git(main_root, "rev-parse", "HEAD")
        import subprocess
        for ancestor in (expected_readback["merge_sha"], baseline_main):
            ancestry = subprocess.run(
                ("git", "merge-base", "--is-ancestor", ancestor, current_main),
                cwd=main_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if ancestry.returncode != 0:
                raise ValueError("active_derivation_current_main_not_descendant")
        merge_before_baseline = subprocess.run(
            ("git", "merge-base", "--is-ancestor", expected_readback["merge_sha"], baseline_main),
            cwd=main_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if merge_before_baseline.returncode != 0:
            raise ValueError("active_derivation_baseline_predates_canonical_merge")

        live_main = subprocess.run(
            ("git", "ls-remote", "--heads", "origin", "refs/heads/main"),
            cwd=main_root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        live_shas = [
            line.split("\t", 1)[0] for line in live_main.stdout.splitlines()
            if line.endswith("\trefs/heads/main")
        ]
        if live_main.returncode != 0 or live_shas != [current_main]:
            raise ValueError("active_derivation_current_main_moved")

        parent_checkpoints: dict[str, dict[str, Any]] = {}
        parent_bundles: dict[str, pathlib.Path] = {}
        for role in ("resume_parent_processor", "canonical_parent_processor"):
            reference = stored_envelope[role]
            parent_id = str(reference["generation_id"])
            if parent_id not in indexed_ids:
                raise ValueError("active_derivation_parent_not_indexed")
            parent_path = source_state / "generations" / f"{parent_id}.json"
            if not parent_path.is_file() or parent_path.is_symlink():
                raise ValueError("active_derivation_parent_checkpoint_unavailable")
            parent = runner.verify_processor_checkpoint(load_json(parent_path, 262144), checkpoint_schema)
            try:
                derivation.validate_processor_parent_checkpoint(
                    reference, parent, original_observation=original,
                )
            except derivation.DerivationError as exc:
                raise ValueError("active_derivation_parent_checkpoint_invalid") from exc
            parent_admission = admission_for_checkpoint(parent, index, handoff)
            if derivation.original_observation_from_checkpoint(parent, parent_admission) != original:
                raise ValueError("active_derivation_parent_original_mismatch")
            expiry = runner.parse_utc_timestamp(parent["output_artifact"].get("expires_at"), "active derivation parent expiry")
            if expiry <= dt.datetime.now(dt.timezone.utc):
                raise ValueError("derivation_parent_artifact_expired")
            parent_checkpoints[parent_id] = parent

        def load_parent_checkpoint(generation_id: str) -> dict[str, Any]:
            if generation_id not in parent_checkpoints:
                path = source_state / "generations" / f"{generation_id}.json"
                if generation_id not in indexed_ids or not path.is_file() or path.is_symlink():
                    raise ValueError("active_derivation_parent_graph_checkpoint_unavailable")
                parent_checkpoints[generation_id] = runner.verify_processor_checkpoint(
                    load_json(path, 262144), checkpoint_schema,
                )
            return parent_checkpoints[generation_id]

        def load_parent_admission(parent: Mapping[str, Any]) -> dict[str, Any]:
            return admission_for_checkpoint(parent, index, handoff)

        try:
            graph = derivation.validate_processor_parent_graph(
                [
                    stored_envelope["resume_parent_processor"]["generation_id"],
                    stored_envelope["canonical_parent_processor"]["generation_id"],
                ],
                load_checkpoint=load_parent_checkpoint,
                admission_for=load_parent_admission,
                original_observation=original,
                expected_ancestor_generation_ids=stored_envelope["ancestor_generation_ids"],
                forbidden_generation_id=target_id,
            )
        except (derivation.DerivationError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("active_derivation_parent_graph_invalid") from exc

        canonical_parent_authorization = authenticate_live_canonical_parent(
            main_root, repository, args.default_branch, canonical_checkpoint,
            canonical_row, readback, canonical_context, runner, derivation, now=now,
        )

        processor = load_module(main_root / "scripts/process-upstream-catalogue-candidate.py", "active_derivation_processor")
        generation_id, inputs = processor.generation_identity(
            original["source_id"], original["source_scope"], original["original_baseline_sha256"],
            original["candidate_sha256"], None, original["source_policy_sha256"],
            original["provider_index_sha256"], same_observation_derivation=stored_envelope,
        )
        if generation_id != target_id or inputs != target_inputs:
            raise ValueError("active_derivation_generation_identity_changed")
        if processor.derivation_processor_revision() != stored_envelope["derivation_processor_revision_sha256"]:
            raise ValueError("active_derivation_processor_revision_changed")

        for role in ("resume_parent_processor", "canonical_parent_processor"):
            reference = stored_envelope[role]
            parent_id = str(reference["generation_id"])
            if parent_id in parent_bundles:
                continue
            parent = parent_checkpoints[parent_id]
            destination = output_root / f"{role}-bundle"
            download_parent_bundle(main_root, repository, args.default_branch, state_root, parent, destination, runner)
            authenticate_parent_bundle(
                main_root, repository, args.default_branch, state_root, parent, destination, runner,
                canonical_context=canonical_context,
                canonical_parent_authorization=canonical_parent_authorization,
            )
            parent_bundles[parent_id] = destination

        # The envelope and generation ID stay byte-for-byte stable across an
        # interrupted derived run. Current main is independently checked
        # above; the frozen composition bytes are reused only when the
        # canonical payload still has the exact original digest.
        output_root.mkdir(parents=True, exist_ok=True)
        baseline_path = output_root / "composition-baseline.registry.json"
        baseline_path.write_bytes(composition_bytes)
        envelope_path = output_root / "same-observation-derivation.json"
        envelope_path.write_bytes(derivation.canonical_json(stored_envelope) + b"\n")
        journal_path = output_root / "canonical-update-promotion-journal.json"
        journal_path.write_bytes(runner.canonical_json(journal) + b"\n")
        resume_id = stored_envelope["resume_parent_processor"]["generation_id"]
        canonical_id = stored_envelope["canonical_parent_processor"]["generation_id"]
        return {
            "eligible": True,
            "reason": "active_same_observation_generation_rehydrated",
            "generation_id": target_id,
            "active_generation_resume": True,
            "composition_baseline_path": str(baseline_path),
            "derivation_path": str(envelope_path),
            "journal_path": str(journal_path),
            "journal_ref_sha": journal_ref_sha,
            "resume_parent_bundle_dir": str(parent_bundles[resume_id]),
            "resume_enrichment_evidence_path": str(parent_bundles[resume_id] / "upstream-catalogue-enrichment-evidence.json"),
            "canonical_parent_bundle_dir": str(parent_bundles[canonical_id]),
            "current_main_sha": current_main,
            "current_manifest_sha256": current_identity["manifest_sha256"],
            "resume_parent_generation_id": resume_id,
            "canonical_parent_generation_id": canonical_id,
            "canonical_parent_journal_record_index": canonical_index,
        }
    if canonical_checkpoint["generation_id"] == selected_target["generation_id"]:
        # The selected C producer already contains its authenticated
        # same-A contributions, so it can fill both parent roles. Do not
        # introduce an unnecessary dependency on an older, expiring B archive.
        # The role references remain explicit in the envelope even when both
        # resolve to this same checkpoint.
        resume_generation_id = str(selected_target["generation_id"])
        if resume_generation_id not in indexed_ids:
            raise ValueError("canonical_producer_resume_parent_not_indexed")
        resume_path = source_state / "generations" / f"{resume_generation_id}.json"
        if not resume_path.is_file() or resume_path.is_symlink():
            raise ValueError("canonical_producer_resume_parent_unavailable")
        target = runner.verify_processor_checkpoint(load_json(resume_path, 262144), checkpoint_schema)
        target_admission = admission_for_checkpoint(target, index, handoff)
        if (
            target.get("status") not in {"ready", "no-change"}
            or derivation.original_observation_from_checkpoint(target, target_admission) != original
        ):
            raise ValueError("canonical_producer_resume_parent_invalid")
    if not has_work and target.get("output_artifact", {}).get("bundle_manifest_sha256") == canonical_checkpoint.get("output_artifact", {}).get("bundle_manifest_sha256"):
        return unavailable("no_incremental_processor_work")

    target_expiry = runner.parse_utc_timestamp(target["output_artifact"].get("expires_at"), "resume parent artifact expiry")
    canonical_expiry = runner.parse_utc_timestamp(canonical_checkpoint["output_artifact"].get("expires_at"), "canonical parent artifact expiry")
    if min(target_expiry, canonical_expiry) <= now:
        raise ValueError("derivation_parent_artifact_expired")
    canonical_parent_authorization = authenticate_live_canonical_parent(
        main_root, repository, args.default_branch, canonical_checkpoint,
        canonical_row, readback, canonical_context, runner, derivation, now=now,
    )

    # Reuse the workflow's downloaded archive when it is the selected parent;
    # otherwise fetch the exact retained artifact named by the stored parent.
    if target["generation_id"] == selected_target["generation_id"] and local_bundle_matches_checkpoint(
        target, args.resume_bundle.resolve(),
    ):
        target_bundle_dir = args.resume_bundle.resolve()
    else:
        target_bundle_dir = output_root / "resume-parent-bundle"
        download_parent_bundle(
            main_root, repository, args.default_branch, state_root, target,
            target_bundle_dir, runner,
        )
    target_bundle = authenticate_parent_bundle(
        main_root, repository, args.default_branch, state_root, target,
        target_bundle_dir, runner, canonical_context=canonical_context,
        canonical_parent_authorization=canonical_parent_authorization,
    )
    target_evidence = load_json(target_bundle_dir / "upstream-catalogue-enrichment-evidence.json", 256 * 1024 * 1024)
    target_reference = derivation.processor_parent_reference(
        target, repository=repository, original_observation=original,
        enrichment_evidence=target_evidence,
        enrichment_evidence_bytes=(target_bundle_dir / "upstream-catalogue-enrichment-evidence.json").read_bytes(),
    )

    if canonical_checkpoint["generation_id"] == target["generation_id"]:
        canonical_dir = target_bundle_dir
    elif canonical_checkpoint["generation_id"] == selected_target["generation_id"] and local_bundle_matches_checkpoint(
        canonical_checkpoint, args.resume_bundle.resolve(),
    ):
        canonical_dir = args.resume_bundle.resolve()
    else:
        canonical_dir = output_root / "canonical-parent-bundle"
        download_parent_bundle(
            main_root, repository, args.default_branch, state_root, canonical_checkpoint,
            canonical_dir, runner,
        )
    canonical_bundle = authenticate_parent_bundle(
        main_root, repository, args.default_branch, state_root, canonical_checkpoint,
        canonical_dir, runner, canonical_context=canonical_context,
        canonical_parent_authorization=canonical_parent_authorization,
    )
    canonical_evidence = load_json(canonical_dir / "upstream-catalogue-enrichment-evidence.json", 256 * 1024 * 1024)
    canonical_reference = derivation.processor_parent_reference(
        canonical_checkpoint, repository=repository, original_observation=original,
        enrichment_evidence=canonical_evidence,
        enrichment_evidence_bytes=(canonical_dir / "upstream-catalogue-enrichment-evidence.json").read_bytes(),
    )
    if (
        canonical_bundle.get("registry_path") != readback["registry_path"]
        or canonical_bundle.get("registry_bytes") != readback["registry_bytes"]
        or canonical_bundle.get("registry_sha256") != readback["registry_sha256"]
        or canonical_bundle.get("composition_receipt_sha256")
        != canonical_row.get("candidate", {}).get("composition_receipt_sha256")
        or (
            current_identity.get("registry_path"), current_identity.get("registry_bytes"),
            current_identity.get("registry_sha256"),
        ) != (
            readback["registry_path"], readback["registry_bytes"], readback["registry_sha256"],
        )
    ):
        raise ValueError("canonical_parent_bundle_differs_from_merged_C_candidate")

    composition_baseline_path = main_root / ".datapan/current-canonical" / pathlib.Path(*current_identity["registry_path"].split("/"))
    baseline_bytes = composition_baseline_path.read_bytes()
    baseline = {
        "main_sha": current_identity["main_sha"],
        "manifest_sha256": current_identity["manifest_sha256"],
        "registry_path": current_identity["registry_path"],
        "registry_sha256": current_identity["registry_sha256"],
        "registry_bytes": current_identity["registry_bytes"],
    }
    if len(baseline_bytes) != baseline["registry_bytes"] or sha256_bytes(baseline_bytes) != baseline["registry_sha256"]:
        raise ValueError("derivation_current_canonical_materialization_mismatch")

    def load_lineage_checkpoint(generation_id: str) -> dict[str, Any]:
        if generation_id not in indexed_ids:
            raise ValueError("derivation_parent_graph_checkpoint_unavailable")
        path = source_state / "generations" / f"{generation_id}.json"
        if not path.is_file() or path.is_symlink():
            raise ValueError("derivation_parent_graph_checkpoint_unavailable")
        return runner.verify_processor_checkpoint(load_json(path, 262144), checkpoint_schema)

    def lineage_admission(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        return admission_for_checkpoint(checkpoint, index, handoff)

    resume_graph = derivation.validate_processor_parent_graph(
        [str(target["generation_id"])], load_checkpoint=load_lineage_checkpoint,
        admission_for=lineage_admission, original_observation=original,
    )
    canonical_graph = derivation.validate_processor_parent_graph(
        [str(canonical_checkpoint["generation_id"])], load_checkpoint=load_lineage_checkpoint,
        admission_for=lineage_admission, original_observation=original,
    )
    processor = load_module(main_root / "scripts/process-upstream-catalogue-candidate.py", "derivation_processor_identity")
    envelope = derivation.build_derivation_envelope(
        original_observation=original,
        resume_parent_processor=target_reference,
        canonical_parent_processor=canonical_reference,
        canonical_parent_readback=readback,
        composition_baseline=baseline,
        derivation_processor_revision_sha256=processor.derivation_processor_revision(),
        resume_parent_ancestors=sorted(resume_graph - {str(target["generation_id"])}),
        canonical_parent_ancestors=sorted(canonical_graph - {str(canonical_checkpoint["generation_id"])}),
    )
    output_root.mkdir(parents=True, exist_ok=True)
    target_bundle_dir_string = str(target_bundle_dir)
    if not pathlib.Path(target_bundle_dir_string).is_dir():
        raise ValueError("derivation_resume_parent_bundle_unavailable")
    composition_output = output_root / "composition-baseline.registry.json"
    composition_output.write_bytes(baseline_bytes)
    envelope_path = output_root / "same-observation-derivation.json"
    envelope_path.write_bytes(derivation.canonical_json(envelope) + b"\n")
    (output_root / "canonical-update-promotion-journal.json").write_bytes(
        runner.canonical_json(journal) + b"\n",
    )
    generation_id, _generation_inputs = processor.generation_identity(
        original["source_id"], original["source_scope"], original["original_baseline_sha256"],
        original["candidate_sha256"], None, original["source_policy_sha256"],
        original["provider_index_sha256"], same_observation_derivation=envelope,
    )
    return {
        "eligible": True,
        "reason": "same_observation_canonical_derivation_authenticated",
        "generation_id": generation_id,
        "composition_baseline_path": str(composition_output),
        "derivation_path": str(envelope_path),
        "journal_path": str(output_root / "canonical-update-promotion-journal.json"),
        "journal_ref_sha": journal_ref_sha,
        "resume_parent_bundle_dir": str(target_bundle_dir),
        "resume_enrichment_evidence_path": str(target_bundle_dir / "upstream-catalogue-enrichment-evidence.json"),
        "canonical_parent_bundle_dir": str(canonical_dir),
        "current_main_sha": current_identity["main_sha"],
        "current_manifest_sha256": current_identity["manifest_sha256"],
        "resume_parent_generation_id": target["generation_id"],
        "canonical_parent_generation_id": canonical_checkpoint["generation_id"],
        "canonical_parent_journal_record_index": canonical_index,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-root", type=pathlib.Path, required=True)
    parser.add_argument("--source-root", type=pathlib.Path, required=True)
    parser.add_argument("--state-dir", type=pathlib.Path, required=True)
    parser.add_argument("--target-generation-id", default="")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--default-branch", default="main")
    parser.add_argument("--collector-admission", type=pathlib.Path, required=True)
    parser.add_argument("--collector-archive", type=pathlib.Path)
    parser.add_argument("--producer-run-url")
    parser.add_argument("--producer-run-id")
    parser.add_argument("--producer-run-attempt", type=int)
    parser.add_argument("--producer-head-sha")
    parser.add_argument("--producer-run-started-at")
    parser.add_argument("--producer-run-completed-at")
    parser.add_argument("--producer-observe-job-started-at")
    parser.add_argument("--producer-observe-job-completed-at")
    parser.add_argument("--producer-artifact-id")
    parser.add_argument("--producer-artifact-name")
    parser.add_argument("--producer-artifact-expires-at")
    parser.add_argument("--producer-artifact-created-at")
    parser.add_argument("--producer-artifact-digest-sha256")
    parser.add_argument("--producer-artifact-size-bytes", type=int)
    parser.add_argument("--producer-event", choices=["schedule", "workflow_dispatch"])
    parser.add_argument("--candidate", type=pathlib.Path, required=True)
    parser.add_argument("--refresh-evidence", type=pathlib.Path, required=True)
    parser.add_argument("--diff", type=pathlib.Path, required=True)
    parser.add_argument("--resume-bundle", type=pathlib.Path, required=True)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--github-output", type=pathlib.Path)
    args = parser.parse_args()
    try:
        result = build_plan(args)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        result_path = args.output_dir / "preparation-result.json"
        result_path.write_bytes(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as handle:
                handle.write(f"derivation_enabled={str(result.get('eligible') is True).lower()}\n")
                for field in (
                    "generation_id", "composition_baseline_path", "derivation_path", "journal_path",
                    "journal_ref_sha", "resume_parent_bundle_dir", "resume_enrichment_evidence_path",
                    "canonical_parent_bundle_dir",
                ):
                    handle.write(f"{field}={result.get(field, '')}\n")
        print(json.dumps({key: value for key, value in result.items() if key != "envelope"}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"eligible": False, "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
