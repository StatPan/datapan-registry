#!/usr/bin/env python3
"""Strict, offline identities for same-observation catalogue derivations.

This module deliberately does not fetch artifacts or contact GitHub. Callers
must authenticate those inputs first and pass the resulting immutable records
to the validators below.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import re
from collections.abc import Callable, Mapping
from typing import Any


SCHEMA_VERSION = "datapan.same-observation-derivation.v1"
DERIVATION_REVISION = "same-observation-composition/v1"
HEX64 = re.compile(r"^[a-f0-9]{64}$")
HEX40 = re.compile(r"^[a-f0-9]{40}$")
SAFE_PATH = "data/data-go-kr.registry.json"
PROCESSOR_BUNDLE_FILES = (
    "composed-candidate.registry.json",
    "ready-scope.registry.json",
    "semantic-diff.json",
    "regeneration-queue.json",
    "quarantine.json",
    "composition-receipt.json",
    "upstream-catalogue-enrichment-evidence.json",
    "upstream-catalogue-processing-result.json",
)
CHECKPOINT_RECEIPT = "upstream-catalogue-checkpoint-receipt.json"


class DerivationError(ValueError):
    """A same-observation lineage record is absent, malformed, or conflicting."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def object_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise DerivationError(f"{field}_invalid")
    return value


def _sha1(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HEX40.fullmatch(value):
        raise DerivationError(f"{field}_invalid")
    return value


def _integer(value: Any, field: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DerivationError(f"{field}_invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DerivationError(f"{field}_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DerivationError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise DerivationError(f"{field}_invalid")
    return value


def validate_original_observation(value: Any) -> dict[str, Any]:
    required = {
        "source_id", "source_scope", "producer_run_id", "producer_run_attempt",
        "producer_head_sha", "producer_artifact_id", "producer_artifact_name",
        "producer_artifact_sha256", "observed_at", "original_baseline_sha256",
        "candidate_sha256", "evidence_sha256", "diff_sha256", "source_policy_sha256",
        "provider_index_sha256", "observation_count",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise DerivationError("original_observation_shape_invalid")
    for key in ("source_id", "source_scope"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise DerivationError("original_observation_scope_invalid")
    if not str(value["producer_run_id"]).isdigit() or not str(value["producer_artifact_id"]).isdigit():
        raise DerivationError("original_observation_run_identity_invalid")
    if not isinstance(value.get("producer_artifact_name"), str) or value["producer_artifact_name"] != f"upstream-catalog-refresh-{value['producer_run_id']}":
        raise DerivationError("original_observation_artifact_name_invalid")
    _integer(value.get("producer_run_attempt"), "original_observation_attempt")
    _integer(value.get("observation_count"), "original_observation_count")
    _sha1(value.get("producer_head_sha"), "original_observation_head")
    _timestamp(value.get("observed_at"), "original_observation_clock")
    for key in (
        "producer_artifact_sha256", "original_baseline_sha256", "candidate_sha256",
        "evidence_sha256", "diff_sha256", "source_policy_sha256", "provider_index_sha256",
    ):
        _digest(value.get(key), f"original_observation_{key}")
    return dict(value)


def validate_processor_parent(value: Any, field: str) -> dict[str, Any]:
    required = {
        "generation_id", "checkpoint_sha256", "generation_inputs_sha256", "bundle_manifest_sha256", "repository",
        "artifact_run_id", "artifact_id", "artifact_name", "artifact_expires_at",
        "processor_generator_revision", "processor_extractor_revision",
        "original_baseline_sha256", "candidate_sha256", "enrichment_evidence_sha256",
        "composition_receipt_sha256", "composed_registry_sha256", "original_observation_sha256",
        "contribution_set_sha256", "contribution_count",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise DerivationError(f"{field}_shape_invalid")
    for key in (
        "generation_id", "checkpoint_sha256", "generation_inputs_sha256", "bundle_manifest_sha256",
        "processor_generator_revision", "processor_extractor_revision",
        "original_baseline_sha256", "candidate_sha256", "enrichment_evidence_sha256",
        "composition_receipt_sha256", "composed_registry_sha256", "original_observation_sha256",
        "contribution_set_sha256",
    ):
        _digest(value.get(key), f"{field}_{key}")
    if not isinstance(value.get("repository"), str) or not re.fullmatch(r"[^/]+/[^/]+", value["repository"]):
        raise DerivationError(f"{field}_repository_invalid")
    for key in ("artifact_run_id", "artifact_id"):
        if not str(value.get(key, "")).isdigit():
            raise DerivationError(f"{field}_{key}_invalid")
    if not re.fullmatch(
        rf"upstream-catalogue-processing-{re.escape(str(value['artifact_run_id']))}-[1-9][0-9]*",
        str(value.get("artifact_name", "")),
    ):
        raise DerivationError(f"{field}_artifact_name_invalid")
    _timestamp(value.get("artifact_expires_at"), f"{field}_artifact_expiry")
    _integer(value.get("contribution_count"), f"{field}_contribution_count", minimum=0)
    return dict(value)


def validate_parent_readback(value: Any) -> dict[str, Any]:
    required = {
        "repository", "journal_ref_sha", "journal_sha256", "journal_record_index",
        "journal_record_sha256", "canonical_producer_generation_id", "source_id", "source_scope",
        "registry_path", "registry_bytes", "registry_sha256", "manifest_sha256",
        "pr_number", "pr_branch", "pr_body_sha256", "pr_head_sha", "merge_sha",
        "merge_ack_run_id", "merge_ack_run_attempt", "merge_ack_run_url", "merge_ack_observed_at",
        "merge_ack_manifest_sha256", "merge_ack_source_sha",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise DerivationError("canonical_parent_readback_shape_invalid")
    if not isinstance(value.get("repository"), str) or not re.fullmatch(r"[^/]+/[^/]+", value["repository"]):
        raise DerivationError("canonical_parent_readback_repository_invalid")
    _sha1(value.get("journal_ref_sha"), "canonical_parent_journal_ref")
    for key in ("journal_sha256", "journal_record_sha256", "journal_record_index", "registry_sha256", "manifest_sha256", "pr_body_sha256", "merge_ack_manifest_sha256"):
        if key in {"journal_record_index"}:
            _integer(value.get(key), f"canonical_parent_{key}", minimum=0)
        else:
            _digest(value.get(key), f"canonical_parent_{key}")
    _digest(value.get("canonical_producer_generation_id"), "canonical_parent_generation")
    _integer(value.get("registry_bytes"), "canonical_parent_registry_bytes")
    _integer(value.get("pr_number"), "canonical_parent_pr_number")
    _sha1(value.get("pr_head_sha"), "canonical_parent_pr_head")
    _sha1(value.get("merge_sha"), "canonical_parent_merge_sha")
    _sha1(value.get("merge_ack_source_sha"), "canonical_parent_ack_source")
    _integer(value.get("merge_ack_run_id"), "canonical_parent_ack_run")
    _integer(value.get("merge_ack_run_attempt"), "canonical_parent_ack_attempt")
    _timestamp(value.get("merge_ack_observed_at"), "canonical_parent_ack_clock")
    if not isinstance(value.get("merge_ack_run_url"), str) or not re.fullmatch(
        rf"https://github\.com/{re.escape(str(value['repository']))}/actions/runs/[0-9]+/attempts/[1-9][0-9]*",
        value["merge_ack_run_url"],
    ):
        raise DerivationError("canonical_parent_ack_url_invalid")
    if value.get("registry_path") != SAFE_PATH:
        raise DerivationError("canonical_parent_registry_path_invalid")
    if not isinstance(value.get("source_id"), str) or not isinstance(value.get("source_scope"), str):
        raise DerivationError("canonical_parent_scope_invalid")
    if not isinstance(value.get("pr_branch"), str) or not value["pr_branch"]:
        raise DerivationError("canonical_parent_pr_branch_invalid")
    if value["merge_ack_source_sha"] != value["merge_sha"]:
        raise DerivationError("canonical_parent_ack_merge_mismatch")
    return dict(value)


def validate_composition_baseline(value: Any) -> dict[str, Any]:
    required = {"main_sha", "manifest_sha256", "registry_path", "registry_sha256", "registry_bytes"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise DerivationError("composition_baseline_shape_invalid")
    _sha1(value.get("main_sha"), "composition_baseline_main")
    _digest(value.get("manifest_sha256"), "composition_baseline_manifest")
    _digest(value.get("registry_sha256"), "composition_baseline_registry")
    _integer(value.get("registry_bytes"), "composition_baseline_registry_bytes")
    if value.get("registry_path") != SAFE_PATH:
        raise DerivationError("composition_baseline_registry_path_invalid")
    return dict(value)


def validate_derivation_envelope(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "derivation_revision", "original_observation",
        "resume_parent_processor", "canonical_parent_processor", "canonical_parent_readback",
        "composition_baseline", "derivation_processor_revision_sha256",
        "ancestor_generation_ids",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("schema_version") != SCHEMA_VERSION:
        raise DerivationError("derivation_envelope_shape_invalid")
    if value.get("derivation_revision") != DERIVATION_REVISION:
        raise DerivationError("derivation_revision_unsupported")
    original = validate_original_observation(value.get("original_observation"))
    resume = validate_processor_parent(value.get("resume_parent_processor"), "resume_parent_processor")
    canonical = validate_processor_parent(value.get("canonical_parent_processor"), "canonical_parent_processor")
    readback = validate_parent_readback(value.get("canonical_parent_readback"))
    baseline = validate_composition_baseline(value.get("composition_baseline"))
    _digest(value.get("derivation_processor_revision_sha256"), "derivation_processor_revision")
    ancestors = value.get("ancestor_generation_ids")
    if (
        not isinstance(ancestors, list) or len(ancestors) > 64
        or any(not isinstance(item, str) or not HEX64.fullmatch(item) for item in ancestors)
        or len(ancestors) != len(set(ancestors))
    ):
        raise DerivationError("derivation_ancestor_graph_invalid")
    # The two roles are independently authenticated even when one checkpoint
    # legitimately fills both roles (for example, an incremental retry whose
    # current C producer is also its only retained B parent).
    parent_ids = {resume["generation_id"], canonical["generation_id"]}
    if not parent_ids.issubset(set(ancestors)):
        raise DerivationError("derivation_parent_missing_from_ancestor_graph")
    if readback["canonical_producer_generation_id"] != canonical["generation_id"]:
        raise DerivationError("canonical_parent_readback_generation_mismatch")
    if (
        original["source_id"] != readback["source_id"]
        or original["source_scope"] != readback["source_scope"]
        or readback["registry_sha256"] != canonical["composed_registry_sha256"]
        or baseline["registry_sha256"] != readback["registry_sha256"]
        or baseline["registry_bytes"] != readback["registry_bytes"]
        or baseline["registry_path"] != readback["registry_path"]
    ):
        raise DerivationError("derivation_parent_payload_or_scope_mismatch")
    original_hash = object_sha256(original)
    for name, parent in (("resume", resume), ("canonical", canonical)):
        if (
            parent["original_observation_sha256"] != original_hash
            or parent["original_baseline_sha256"] != original["original_baseline_sha256"]
            or parent["candidate_sha256"] != original["candidate_sha256"]
        ):
            raise DerivationError(f"{name}_parent_original_observation_mismatch")
    if (
        readback["source_id"] != original["source_id"]
        or readback["source_scope"] != original["source_scope"]
        or readback["merge_ack_manifest_sha256"] != readback["manifest_sha256"]
    ):
        raise DerivationError("canonical_parent_readback_identity_mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "derivation_revision": DERIVATION_REVISION,
        "original_observation": original,
        "resume_parent_processor": resume,
        "canonical_parent_processor": canonical,
        "canonical_parent_readback": readback,
        "composition_baseline": baseline,
        "derivation_processor_revision_sha256": value["derivation_processor_revision_sha256"],
        "ancestor_generation_ids": list(ancestors),
    }


def original_observation_from_checkpoint(
    checkpoint: Mapping[str, Any], admission: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve the immutable A subject from its sealed B parent and handoff ledger."""
    inputs = checkpoint.get("generation_inputs")
    if not isinstance(inputs, Mapping):
        raise DerivationError("parent_generation_inputs_missing")
    prior = inputs.get("same_observation_derivation")
    if prior is not None:
        original = validate_derivation_envelope(prior)["original_observation"]
        observation = checkpoint.get("last_observation")
        refs = checkpoint.get("input_artifacts")
        observation_count = checkpoint.get("observation_count")
        matching = [
            row for row in refs if isinstance(row, Mapping)
            and str(row.get("run_id")) == original["producer_run_id"]
            and str(row.get("artifact_id")) == original["producer_artifact_id"]
            and row.get("candidate_sha256") == original["candidate_sha256"]
            and row.get("evidence_sha256") == original["evidence_sha256"]
            and row.get("diff_sha256") == original["diff_sha256"]
        ] if isinstance(refs, list) else []
        if (
            checkpoint.get("source_id") != original["source_id"]
            or checkpoint.get("source_scope") != original["source_scope"]
            or checkpoint.get("observed_at") != original["observed_at"]
            or observation_count != original["observation_count"]
            or inputs.get("baseline_sha256") != original["original_baseline_sha256"]
            or inputs.get("candidate_sha256") != original["candidate_sha256"]
            or str(admission.get("producer_run_id")) != original["producer_run_id"]
            or admission.get("run_attempt") != original["producer_run_attempt"]
            or admission.get("head_sha") != original["producer_head_sha"]
            or str(admission.get("artifact_id")) != original["producer_artifact_id"]
            or admission.get("artifact_digest_sha256") != original["producer_artifact_sha256"]
            or admission.get("observed_at") != original["observed_at"]
            or admission.get("candidate_sha256") != original["candidate_sha256"]
            or admission.get("refresh_evidence_sha256") != original["evidence_sha256"]
            or inputs.get("policy_sha256") != original["source_policy_sha256"]
            or inputs.get("adapter_revision") != original["provider_index_sha256"]
            or not isinstance(observation, Mapping)
            or observation.get("observed_at") != original["observed_at"]
            or observation.get("producer_run_id") != original["producer_run_id"]
            or observation.get("refresh_evidence_sha256") != original["evidence_sha256"]
            or observation.get("collection_status") != "success"
            or observation.get("execution_mode") != "live"
            or isinstance(observation_count, bool)
            or not isinstance(observation_count, int)
            or observation_count < 1
            or len(matching) != 1
        ):
            raise DerivationError("parent_admission_differs_from_original_observation")
        return dict(original)
    refs = checkpoint.get("input_artifacts")
    observation = checkpoint.get("last_observation")
    if not isinstance(refs, list) or not isinstance(observation, Mapping):
        raise DerivationError("parent_input_artifact_identity_missing")
    matching = [
        row for row in refs if isinstance(row, Mapping)
        and str(row.get("run_id")) == str(admission.get("producer_run_id"))
        and str(row.get("artifact_id")) == str(admission.get("artifact_id"))
        and row.get("evidence_sha256") == admission.get("refresh_evidence_sha256")
        and row.get("candidate_sha256") == admission.get("candidate_sha256")
    ]
    if len(matching) != 1:
        raise DerivationError("parent_input_artifact_identity_ambiguous")
    ref = matching[0]
    original = {
        "source_id": checkpoint.get("source_id"),
        "source_scope": checkpoint.get("source_scope"),
        "producer_run_id": str(admission.get("producer_run_id") or ""),
        "producer_run_attempt": admission.get("run_attempt"),
        "producer_head_sha": admission.get("head_sha"),
        "producer_artifact_id": str(admission.get("artifact_id") or ""),
        "producer_artifact_name": admission.get("artifact_name"),
        "producer_artifact_sha256": admission.get("artifact_digest_sha256"),
        "observed_at": admission.get("observed_at"),
        "original_baseline_sha256": inputs.get("baseline_sha256"),
        "candidate_sha256": inputs.get("candidate_sha256"),
        "evidence_sha256": admission.get("refresh_evidence_sha256"),
        "diff_sha256": ref.get("diff_sha256"),
        "source_policy_sha256": inputs.get("policy_sha256"),
        "provider_index_sha256": inputs.get("adapter_revision"),
        "observation_count": checkpoint.get("observation_count"),
    }
    if (
        checkpoint.get("source_id") != original["source_id"]
        or checkpoint.get("source_scope") != original["source_scope"]
        or checkpoint.get("observed_at") != original["observed_at"]
        or observation.get("producer_run_id") != original["producer_run_id"]
        or observation.get("refresh_evidence_sha256") != original["evidence_sha256"]
        or observation.get("observed_at") != original["observed_at"]
        or ref.get("name") != original["producer_artifact_name"]
    ):
        raise DerivationError("parent_checkpoint_differs_from_admitted_observation")
    return validate_original_observation(original)


def validate_processor_parent_graph(
    parent_generation_ids: list[str] | tuple[str, ...] | set[str],
    *,
    load_checkpoint: Callable[[str], Mapping[str, Any]],
    admission_for: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    original_observation: Mapping[str, Any],
    expected_ancestor_generation_ids: list[str] | None = None,
    forbidden_generation_id: str | None = None,
) -> set[str]:
    """Rebuild a bounded parent graph from sealed checkpoints and A admission.

    Generation IDs and the envelope's ancestor list are indexes only. Every
    edge is read from the referenced checkpoint, and each derived node's
    declared ancestor set must equal the recursively resolved graph.
    """
    original = validate_original_observation(original_observation)
    seeds = list(parent_generation_ids)
    if not seeds:
        raise DerivationError("derivation_parent_graph_seed_invalid")
    # Resume and canonical are role references, not necessarily distinct
    # nodes. Traverse a shared parent once while retaining edge-cycle checks.
    seeds = list(dict.fromkeys(seeds))
    active: set[str] = set()
    resolved: dict[str, set[str]] = {}

    def visit(generation_id: str) -> set[str]:
        if not isinstance(generation_id, str) or not HEX64.fullmatch(generation_id):
            raise DerivationError("derivation_parent_graph_generation_invalid")
        if generation_id == forbidden_generation_id or generation_id in active:
            raise DerivationError("derivation_parent_graph_cycle")
        if generation_id in resolved:
            return resolved[generation_id]
        # Enforce the visit bound before loading another checkpoint. A final
        # closure-size check alone still permits attacker-controlled deep
        # ancestry to make the validator traverse and read an unbounded graph.
        if len(active) + len(resolved) >= 64:
            raise DerivationError("derivation_parent_graph_too_large")
        checkpoint = load_checkpoint(generation_id)
        if not isinstance(checkpoint, Mapping) or checkpoint.get("generation_id") != generation_id:
            raise DerivationError("derivation_parent_graph_checkpoint_identity_mismatch")
        admission = admission_for(checkpoint)
        if original_observation_from_checkpoint(checkpoint, admission) != original:
            raise DerivationError("derivation_parent_graph_cross_observation")
        active.add(generation_id)
        inputs = checkpoint.get("generation_inputs")
        if not isinstance(inputs, Mapping):
            raise DerivationError("derivation_parent_graph_inputs_missing")
        envelope = inputs.get("same_observation_derivation")
        closure = {generation_id}
        if envelope is not None:
            prior = validate_derivation_envelope(envelope)
            if prior["original_observation"] != original:
                raise DerivationError("derivation_parent_graph_cross_observation")
            children = {
                prior["resume_parent_processor"]["generation_id"],
                prior["canonical_parent_processor"]["generation_id"],
            }
            if generation_id in children:
                raise DerivationError("derivation_parent_graph_cycle")
            for child in children:
                closure.update(visit(child))
            declared = set(prior["ancestor_generation_ids"])
            if declared != closure - {generation_id}:
                raise DerivationError("derivation_parent_graph_incomplete_or_conflicting")
        active.remove(generation_id)
        resolved[generation_id] = closure
        return closure

    closure: set[str] = set()
    for seed in seeds:
        closure.update(visit(seed))
    if len(closure) > 64:
        raise DerivationError("derivation_parent_graph_too_large")
    if expected_ancestor_generation_ids is not None:
        expected = set(expected_ancestor_generation_ids)
        if len(expected) != len(expected_ancestor_generation_ids) or expected != closure:
            raise DerivationError("derivation_parent_graph_incomplete_or_conflicting")
    return closure


def contribution_identity(evidence: Any) -> tuple[str, int]:
    """Hash the complete set of successful enrichment records deterministically."""
    if not isinstance(evidence, Mapping) or evidence.get("schema_version") != "datapan.catalogue-enrichment-evidence.v1":
        raise DerivationError("parent_enrichment_evidence_invalid")
    records = evidence.get("records")
    if not isinstance(records, list):
        raise DerivationError("parent_enrichment_records_invalid")
    normalized: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for row in records:
        if not isinstance(row, Mapping) or not isinstance(row.get("api_key"), Mapping):
            raise DerivationError("parent_enrichment_record_invalid")
        provider, identity = row["api_key"].get("provider"), row["api_key"].get("id")
        if provider != "data.go.kr" or not isinstance(identity, str) or not identity.isdigit():
            raise DerivationError("parent_enrichment_identity_invalid")
        token = (provider, identity)
        if token in identities:
            raise DerivationError("parent_enrichment_identity_duplicate")
        identities.add(token)
        operations = row.get("operations")
        if (
            row.get("status") != "enriched" or not isinstance(operations, list)
            or row.get("operations_sha256") != object_sha256(operations)
        ):
            raise DerivationError("parent_enrichment_contribution_invalid")
        normalized.append(dict(row))
    normalized.sort(key=lambda row: (row["api_key"]["provider"], row["api_key"]["id"]))
    return object_sha256(normalized), len(normalized)


def validate_contribution_bytes(reference: Mapping[str, Any], evidence_bytes: bytes) -> None:
    """Bind a parent reference to exact archived enrichment evidence bytes."""
    try:
        evidence = json.loads(evidence_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DerivationError("parent_enrichment_evidence_json_invalid") from exc
    if sha256_bytes(evidence_bytes) != reference.get("enrichment_evidence_sha256"):
        raise DerivationError("parent_enrichment_evidence_digest_mismatch")
    contribution_sha, contribution_count = contribution_identity(evidence)
    if (
        contribution_sha != reference.get("contribution_set_sha256")
        or contribution_count != reference.get("contribution_count")
    ):
        raise DerivationError("parent_contribution_set_mismatch")


def processor_parent_reference(
    checkpoint: Mapping[str, Any], *, repository: str, original_observation: Mapping[str, Any],
    enrichment_evidence: Mapping[str, Any], enrichment_evidence_bytes: bytes,
) -> dict[str, Any]:
    """Create the compact exact reference to a sealed B checkpoint/artifact."""
    claimed = checkpoint.get("checkpoint_sha256")
    if not isinstance(claimed, str) or object_sha256({key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}) != claimed:
        raise DerivationError("parent_processor_checkpoint_seal_invalid")
    outputs = checkpoint.get("output_digests")
    locator = checkpoint.get("output_artifact")
    inputs = checkpoint.get("generation_inputs")
    if checkpoint.get("status") not in {"ready", "no-change"}:
        raise DerivationError("parent_processor_not_terminal_candidate")
    if not isinstance(outputs, list) or not isinstance(locator, Mapping) or not isinstance(inputs, Mapping):
        raise DerivationError("parent_processor_bundle_identity_missing")
    by_path = {str(row.get("path")): row for row in outputs if isinstance(row, Mapping)}
    required = {
        "upstream-catalogue-enrichment-evidence.json": "enrichment_evidence_sha256",
        "composition-receipt.json": "composition_receipt_sha256",
        "composed-candidate.registry.json": "composed_registry_sha256",
    }
    values = {field: by_path.get(path, {}).get("sha256") for path, field in required.items()}
    if any(not isinstance(value, str) or not HEX64.fullmatch(value) for value in values.values()):
        raise DerivationError("parent_processor_bundle_files_missing")
    artifact_run_id = str(locator.get("run_id") or "")
    artifact_id = str(locator.get("artifact_id") or "")
    if (
        not artifact_run_id.isdigit() or not artifact_id.isdigit()
        or locator.get("repository") != repository
        or locator.get("name") is None
        or not re.fullmatch(rf"upstream-catalogue-processing-{artifact_run_id}-[1-9][0-9]*", str(locator.get("name")))
    ):
        raise DerivationError("parent_processor_artifact_locator_invalid")
    original = validate_original_observation(original_observation)
    original_hash = object_sha256(original)
    contribution_sha, contribution_count = contribution_identity(enrichment_evidence)
    try:
        decoded_evidence = json.loads(enrichment_evidence_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DerivationError("parent_enrichment_evidence_json_invalid") from exc
    if decoded_evidence != enrichment_evidence or values["enrichment_evidence_sha256"] != sha256_bytes(enrichment_evidence_bytes):
        raise DerivationError("parent_enrichment_evidence_checkpoint_digest_mismatch")
    return validate_processor_parent({
        "generation_id": checkpoint.get("generation_id"),
        "checkpoint_sha256": claimed,
        "generation_inputs_sha256": object_sha256(inputs),
        "bundle_manifest_sha256": locator.get("bundle_manifest_sha256"),
        "repository": repository,
        "artifact_run_id": artifact_run_id,
        "artifact_id": artifact_id,
        "artifact_name": locator.get("name"),
        "artifact_expires_at": locator.get("expires_at"),
        "processor_generator_revision": inputs.get("generator_revision"),
        "processor_extractor_revision": inputs.get("extractor_revision"),
        "original_baseline_sha256": inputs.get("baseline_sha256"),
        "candidate_sha256": inputs.get("candidate_sha256"),
        **values,
        "original_observation_sha256": original_hash,
        "contribution_set_sha256": contribution_sha,
        "contribution_count": contribution_count,
    }, "processor_parent")


def composition_baseline(checkpoint: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a validated derivative composition baseline, else legacy baseline."""
    inputs = checkpoint.get("generation_inputs")
    if not isinstance(inputs, Mapping):
        raise DerivationError("generation_inputs_missing")
    envelope = inputs.get("same_observation_derivation")
    if envelope is None:
        return None
    validated = validate_derivation_envelope(envelope)
    return validated["composition_baseline"]


def validate_readback_against_journal(
    envelope: Mapping[str, Any], journal: Mapping[str, Any], *, journal_ref_sha: str,
    allow_monotonic_successor: bool = False,
) -> Mapping[str, Any]:
    """Bind the selected merged PR row to its frozen or monotonic journal form.

    Before a B claim, the exact snapshot and row must still match. Later C runs
    may observe a journal row advanced through the existing publication/readback
    lifecycle; that does not invalidate its immutable merge acknowledgement.
    """
    value = validate_derivation_envelope(envelope)
    reference = value["canonical_parent_readback"]
    observed_ref = _sha1(journal_ref_sha, "journal_ref")
    if not allow_monotonic_successor:
        if observed_ref != reference["journal_ref_sha"]:
            raise DerivationError("canonical_parent_journal_ref_changed")
        if object_sha256(journal) != reference["journal_sha256"]:
            raise DerivationError("canonical_parent_journal_snapshot_changed")
    rows = journal.get("records") if isinstance(journal, Mapping) else None
    if not isinstance(rows, list):
        raise DerivationError("canonical_parent_journal_row_missing")
    if allow_monotonic_successor:
        matches = []
        for candidate_row in rows:
            if not isinstance(candidate_row, Mapping):
                continue
            candidate_candidate = candidate_row.get("candidate") if isinstance(candidate_row.get("candidate"), Mapping) else {}
            candidate_owner = candidate_row.get("ownership") if isinstance(candidate_row.get("ownership"), Mapping) else {}
            candidate_pr = candidate_row.get("pr") if isinstance(candidate_row.get("pr"), Mapping) else {}
            if (
                candidate_candidate.get("generation_id") == reference["canonical_producer_generation_id"]
                and candidate_candidate.get("source_id") == reference["source_id"]
                and candidate_candidate.get("scope") == reference["source_scope"]
                and candidate_candidate.get("repository") == reference["repository"]
                and candidate_pr.get("number") == reference["pr_number"]
                and candidate_owner.get("branch") == reference["pr_branch"]
                and candidate_owner.get("body_sha256") == reference["pr_body_sha256"]
                and candidate_owner.get("expected_head_sha") == reference["pr_head_sha"]
                and candidate_pr.get("merge_commit_sha") == reference["merge_sha"]
            ):
                matches.append(candidate_row)
        if len(matches) != 1:
            raise DerivationError("canonical_parent_journal_successor_ambiguous_or_missing")
        row = matches[0]
    else:
        if reference["journal_record_index"] >= len(rows):
            raise DerivationError("canonical_parent_journal_row_missing")
        row = rows[reference["journal_record_index"]]
        if not isinstance(row, Mapping) or object_sha256(row) != reference["journal_record_sha256"]:
            raise DerivationError("canonical_parent_journal_row_changed")
    candidate = row.get("candidate") if isinstance(row.get("candidate"), Mapping) else {}
    ownership = row.get("ownership") if isinstance(row.get("ownership"), Mapping) else {}
    pr = row.get("pr") if isinstance(row.get("pr"), Mapping) else {}
    acknowledgements = row.get("acknowledgements")
    merged_acks = [item for item in acknowledgements if isinstance(item, Mapping) and item.get("status") == "merged"] if isinstance(acknowledgements, list) else []
    if len(merged_acks) != 1:
        raise DerivationError("canonical_parent_merge_ack_ambiguous")
    ack = merged_acks[0]
    expected = reference
    if (
        row.get("status") not in {"merged", "publication-pending", "published", "read-back-confirmed"}
        or row.get("superseded_by") is not None
        or candidate.get("repository") != expected["repository"]
        or candidate.get("source_id") != expected["source_id"]
        or candidate.get("scope") != expected["source_scope"]
        or candidate.get("generation_id") != expected["canonical_producer_generation_id"]
        or candidate.get("registry_path") != expected["registry_path"]
        or candidate.get("registry_bytes") != expected["registry_bytes"]
        or candidate.get("registry_sha256") != expected["registry_sha256"]
        or candidate.get("manifest_sha256") != expected["manifest_sha256"]
        or candidate.get("head_sha") != expected["pr_head_sha"]
        or pr.get("number") != expected["pr_number"]
        or pr.get("state") != "merged"
        or pr.get("merge_commit_sha") != expected["merge_sha"]
        or ownership.get("branch") != expected["pr_branch"]
        or ownership.get("body_sha256") != expected["pr_body_sha256"]
        or ownership.get("expected_head_sha") != expected["pr_head_sha"]
        or ack.get("source_sha") != expected["merge_ack_source_sha"]
        or ack.get("source_sha") != expected["merge_sha"]
        or ack.get("manifest_sha256") != expected["merge_ack_manifest_sha256"]
        or ack.get("run_id") != expected["merge_ack_run_id"]
        or ack.get("run_attempt") != expected["merge_ack_run_attempt"]
        or ack.get("run_url") != expected["merge_ack_run_url"]
        or ack.get("observed_at") != expected["merge_ack_observed_at"]
    ):
        raise DerivationError("canonical_parent_merge_readback_identity_mismatch")
    return row


def canonical_parent_readback_reference(
    journal: Mapping[str, Any], *, journal_ref_sha: str, record_index: int,
) -> dict[str, Any]:
    """Capture the one exact merged C row from a validated frozen snapshot."""
    rows = journal.get("records")
    if not isinstance(rows, list) or isinstance(record_index, bool) or not 0 <= record_index < len(rows):
        raise DerivationError("canonical_parent_journal_row_missing")
    row = rows[record_index]
    if not isinstance(row, Mapping):
        raise DerivationError("canonical_parent_journal_row_invalid")
    candidate = row.get("candidate") if isinstance(row.get("candidate"), Mapping) else {}
    ownership = row.get("ownership") if isinstance(row.get("ownership"), Mapping) else {}
    pr = row.get("pr") if isinstance(row.get("pr"), Mapping) else {}
    acks = row.get("acknowledgements")
    merged = [item for item in acks if isinstance(item, Mapping) and item.get("status") == "merged"] if isinstance(acks, list) else []
    if len(merged) != 1:
        raise DerivationError("canonical_parent_merge_ack_ambiguous")
    ack = merged[0]
    source_sha = ack.get("source_sha")
    manifest_sha = candidate.get("manifest_sha256")
    registry_path = candidate.get("registry_path")
    registry_bytes = candidate.get("registry_bytes")
    registry_sha = candidate.get("registry_sha256")
    reference = {
        "repository": candidate.get("repository"),
        "journal_ref_sha": journal_ref_sha,
        "journal_sha256": object_sha256(journal),
        "journal_record_index": record_index,
        "journal_record_sha256": object_sha256(row),
        "canonical_producer_generation_id": candidate.get("generation_id"),
        "source_id": candidate.get("source_id"),
        "source_scope": candidate.get("scope"),
        "registry_path": registry_path,
        "registry_bytes": registry_bytes,
        "registry_sha256": registry_sha,
        "manifest_sha256": manifest_sha,
        "pr_number": pr.get("number"),
        "pr_branch": ownership.get("branch"),
        "pr_body_sha256": ownership.get("body_sha256"),
        "pr_head_sha": ownership.get("expected_head_sha"),
        "merge_sha": pr.get("merge_commit_sha"),
        "merge_ack_run_id": ack.get("run_id"),
        "merge_ack_run_attempt": ack.get("run_attempt"),
        "merge_ack_run_url": ack.get("run_url"),
        "merge_ack_observed_at": ack.get("observed_at"),
        "merge_ack_manifest_sha256": ack.get("manifest_sha256"),
        "merge_ack_source_sha": source_sha,
    }
    if row.get("status") not in {"merged", "publication-pending", "published", "read-back-confirmed"}:
        raise DerivationError("canonical_parent_is_not_merged")
    return validate_parent_readback(reference)


def authenticate_canonical_merge_ack(
    root: Any, repository: str, row: Mapping[str, Any], health_policy: Mapping[str, Any],
    *, now: dt.datetime,
) -> dict[str, Any]:
    """Reauthenticate the exact C merge acknowledgement run and attempt.

    The journal is durable evidence, but its row is not a substitute for the
    Actions run and completed jobs. Publication ACKs are deliberately not
    required here: composition depends on the reviewed canonical merge.
    """
    candidate = row.get("candidate") if isinstance(row.get("candidate"), Mapping) else {}
    acknowledgements = row.get("acknowledgements")
    merged = [
        item for item in acknowledgements
        if isinstance(item, Mapping) and item.get("status") == "merged"
    ] if isinstance(acknowledgements, list) else []
    pr = row.get("pr") if isinstance(row.get("pr"), Mapping) else {}
    if (
        len(merged) != 1
        or candidate.get("repository", "").casefold() != repository.casefold()
        or row.get("status") not in {"merged", "publication-pending", "published", "read-back-confirmed"}
        or pr.get("state") != "merged"
        or pr.get("merge_commit_sha") != merged[0].get("source_sha")
        or candidate.get("manifest_sha256") != merged[0].get("manifest_sha256")
    ):
        raise DerivationError("canonical_parent_merge_ack_identity_invalid")
    health_path = root / "scripts/check-upstream-catalogue-health.py"
    spec = importlib.util.spec_from_file_location("same_observation_merge_ack_health", health_path)
    if spec is None or spec.loader is None:
        raise DerivationError("canonical_parent_merge_ack_validator_unavailable")
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    promotion = health_policy.get("promotion_state") if isinstance(health_policy, Mapping) else None
    workflow_path = promotion.get("promotion_workflow_path") if isinstance(promotion, Mapping) else None
    if not isinstance(workflow_path, str) or not workflow_path:
        raise DerivationError("canonical_parent_promotion_workflow_missing")
    clock = health_policy.get("clock") if isinstance(health_policy, Mapping) else None
    maximum_future_skew = clock.get("maximum_future_skew_seconds", 300) if isinstance(clock, Mapping) else 300
    try:
        valid_now = isinstance(now, dt.datetime) and now.tzinfo is not None and now.utcoffset() is not None
    except (OverflowError, TypeError, ValueError):
        valid_now = False
    if (
        isinstance(maximum_future_skew, bool)
        or not isinstance(maximum_future_skew, int)
        or not 0 <= maximum_future_skew <= 3600
        or not valid_now
    ):
        raise DerivationError("canonical_parent_merge_ack_clock_invalid")
    try:
        now_utc = now.astimezone(dt.timezone.utc)
    except (OverflowError, TypeError, ValueError) as exc:
        raise DerivationError("canonical_parent_merge_ack_clock_invalid") from exc
    ack = dict(merged[0])
    run_id = str(ack.get("run_id") or "")
    attempt = ack.get("run_attempt")
    if not run_id.isdigit() or isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise DerivationError("canonical_parent_merge_ack_attempt_invalid")
    try:
        workflow_id = health.collect_workflow_identity(repository, workflow_path)
        evidence = health.collect_run_attempt_evidence(repository, run_id, attempt)
    except Exception as exc:
        raise DerivationError("canonical_parent_merge_ack_evidence_unavailable") from exc
    run_key = f"{run_id}/{attempt}"
    trusted = health.trusted_promotion_run(
        ack, {run_key: evidence}, repository,
        {"promotion_workflow_path": workflow_path}, {workflow_path: workflow_id},
        now_utc, maximum_future_skew,
    )
    if trusted is None:
        raise DerivationError("canonical_parent_merge_ack_run_untrusted")
    try:
        observed = health.parse_time(ack.get("observed_at"), "canonical_parent_merge_ack.observed_at")
        # `trusted_promotion_run` deliberately returns presentation timestamps
        # rounded to seconds. Keep the temporal decision at native precision.
        completed = max(
            health.parse_time(job.get("completed_at"), "promotion_job.completed_at")
            for job in evidence["jobs"]
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DerivationError("canonical_parent_merge_ack_clock_invalid") from exc
    skew = dt.timedelta(seconds=maximum_future_skew)
    emitted_during_successful_job = any(
        _timestamp_in_successful_job_window(job, observed, skew, health)
        for job in evidence.get("jobs", [])
        if isinstance(job, Mapping)
    )
    # The ACK writer runs inside the reconcile job, before the workflow attempt
    # can complete. Keep the old after-all-jobs case as a separate compatible
    # branch; skipped/neutral jobs still count for finality but never establish
    # an emission window.
    if observed > now_utc + skew or not (observed >= completed or emitted_during_successful_job):
        raise DerivationError("canonical_parent_merge_ack_order_invalid")
    return {
        "run_id": run_id,
        "run_attempt": attempt,
        "jobs_completed_at": trusted["jobs_completed_at"],
        "observed_at": ack["observed_at"],
    }


def _timestamp_in_successful_job_window(
    job: Mapping[str, Any], observed: dt.datetime, skew: dt.timedelta, health: Any,
) -> bool:
    """Whether an ACK observation falls within one exact successful job interval plus clock skew."""
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        return False
    try:
        started = health.parse_time(job.get("started_at"), "promotion_job.started_at")
        completed = health.parse_time(job.get("completed_at"), "promotion_job.completed_at")
    except (OverflowError, ValueError):
        return False
    if started > completed:
        return False
    try:
        return started - skew <= observed <= completed + skew
    except OverflowError:
        return False


def build_derivation_envelope(
    *,
    original_observation: Mapping[str, Any],
    resume_parent_processor: Mapping[str, Any],
    canonical_parent_processor: Mapping[str, Any],
    canonical_parent_readback: Mapping[str, Any],
    composition_baseline: Mapping[str, Any],
    derivation_processor_revision_sha256: str,
    resume_parent_ancestors: list[str] | None = None,
    canonical_parent_ancestors: list[str] | None = None,
) -> dict[str, Any]:
    """Build a deterministic envelope from two separately authenticated B parents."""
    resume = validate_processor_parent(resume_parent_processor, "resume_parent_processor")
    canonical = validate_processor_parent(canonical_parent_processor, "canonical_parent_processor")
    ancestors = sorted({
        resume["generation_id"], canonical["generation_id"],
        *(resume_parent_ancestors or []), *(canonical_parent_ancestors or []),
    })
    return validate_derivation_envelope({
        "schema_version": SCHEMA_VERSION,
        "derivation_revision": DERIVATION_REVISION,
        "original_observation": dict(original_observation),
        "resume_parent_processor": resume,
        "canonical_parent_processor": canonical,
        "canonical_parent_readback": dict(canonical_parent_readback),
        "composition_baseline": dict(composition_baseline),
        "derivation_processor_revision_sha256": derivation_processor_revision_sha256,
        "ancestor_generation_ids": ancestors,
    })


def validate_processor_parent_checkpoint(
    reference: Mapping[str, Any], checkpoint: Mapping[str, Any], *, original_observation: Mapping[str, Any],
) -> None:
    """Check a derivation reference against its durable B state checkpoint."""
    from copy import deepcopy

    ref = validate_processor_parent(reference, "processor_parent")
    cp = deepcopy(dict(checkpoint))
    claimed = cp.pop("checkpoint_sha256", None)
    if not isinstance(claimed, str) or object_sha256(cp) != claimed or claimed != ref["checkpoint_sha256"]:
        raise DerivationError("parent_processor_checkpoint_seal_mismatch")
    inputs = checkpoint.get("generation_inputs")
    locator = checkpoint.get("output_artifact")
    outputs = checkpoint.get("output_digests")
    if (
        checkpoint.get("generation_id") != ref["generation_id"]
        or not isinstance(inputs, Mapping)
        or not isinstance(locator, Mapping)
        or not isinstance(outputs, list)
        or object_sha256(outputs) != ref["bundle_manifest_sha256"]
        or locator.get("bundle_manifest_sha256") != ref["bundle_manifest_sha256"]
        or locator.get("repository") != ref["repository"]
        or str(locator.get("run_id")) != ref["artifact_run_id"]
        or str(locator.get("artifact_id")) != ref["artifact_id"]
        or locator.get("name") != ref["artifact_name"]
        or locator.get("expires_at") != ref["artifact_expires_at"]
        or object_sha256(inputs) != ref["generation_inputs_sha256"]
        or inputs.get("baseline_sha256") != ref["original_baseline_sha256"]
        or inputs.get("candidate_sha256") != ref["candidate_sha256"]
        or inputs.get("generator_revision") != ref["processor_generator_revision"]
        or inputs.get("extractor_revision") != ref["processor_extractor_revision"]
        or inputs.get("policy_sha256") != original_observation.get("source_policy_sha256")
        or inputs.get("adapter_revision") != original_observation.get("provider_index_sha256")
    ):
        raise DerivationError("parent_processor_checkpoint_reference_mismatch")
    evidence = next((row for row in outputs if isinstance(row, Mapping) and row.get("path") == "upstream-catalogue-enrichment-evidence.json"), None)
    receipt = next((row for row in outputs if isinstance(row, Mapping) and row.get("path") == "composition-receipt.json"), None)
    candidate = next((row for row in outputs if isinstance(row, Mapping) and row.get("path") == "composed-candidate.registry.json"), None)
    if (
        not isinstance(evidence, Mapping) or evidence.get("sha256") != ref["enrichment_evidence_sha256"]
        or not isinstance(receipt, Mapping) or receipt.get("sha256") != ref["composition_receipt_sha256"]
        or not isinstance(candidate, Mapping) or candidate.get("sha256") != ref["composed_registry_sha256"]
    ):
        raise DerivationError("parent_processor_output_reference_mismatch")
    expected_original_hash = object_sha256(validate_original_observation(original_observation))
    if inputs.get("same_observation_derivation") is None:
        observation = checkpoint.get("last_observation")
        if not isinstance(observation, Mapping) or (
            checkpoint.get("source_id") != original_observation.get("source_id")
            or checkpoint.get("source_scope") != original_observation.get("source_scope")
            or observation.get("producer_run_id") != original_observation.get("producer_run_id")
            or observation.get("observed_at") != original_observation.get("observed_at")
            or observation.get("refresh_evidence_sha256") != original_observation.get("evidence_sha256")
        ):
            raise DerivationError("legacy_parent_original_observation_mismatch")
    else:
        prior = validate_derivation_envelope(inputs["same_observation_derivation"])
        if object_sha256(prior["original_observation"]) != expected_original_hash:
            raise DerivationError("derived_parent_original_observation_mismatch")


def validate_processor_parent_bundle(
    reference: Mapping[str, Any], checkpoint: Mapping[str, Any], bundle_dir: Any,
) -> dict[str, Any]:
    """Validate every archived parent output before it can seed recomposition.

    This is intentionally local and bounded. The workflow authenticates the
    Actions run/artifact API response and downloads the ZIP; this function then
    binds each extracted member to the checkpoint's complete eight-file
    inventory and validates the checkpoint copy shipped inside that archive.
    """
    import pathlib

    ref = validate_processor_parent(reference, "processor_parent")
    root = pathlib.Path(bundle_dir)
    expected_members = set(PROCESSOR_BUNDLE_FILES) | {CHECKPOINT_RECEIPT}
    if any(path.is_symlink() or not path.is_file() for path in root.iterdir()) or {path.name for path in root.iterdir()} != expected_members:
        raise DerivationError("parent_bundle_member_set_invalid")
    outputs = checkpoint.get("output_digests")
    if not isinstance(outputs, list) or [row.get("path") for row in outputs if isinstance(row, Mapping)] != list(PROCESSOR_BUNDLE_FILES):
        raise DerivationError("parent_bundle_inventory_invalid")
    if object_sha256(outputs) != ref["bundle_manifest_sha256"]:
        raise DerivationError("parent_bundle_inventory_digest_mismatch")
    for row in outputs:
        name = row.get("path") if isinstance(row, Mapping) else None
        if not isinstance(name, str) or name not in PROCESSOR_BUNDLE_FILES:
            raise DerivationError("parent_bundle_member_path_invalid")
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise DerivationError("parent_bundle_member_missing")
        if path.stat().st_size != row.get("bytes") or sha256_bytes(path.read_bytes()) != row.get("sha256"):
            raise DerivationError("parent_bundle_member_digest_mismatch")
    checkpoint_path = root / CHECKPOINT_RECEIPT
    if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
        raise DerivationError("parent_bundle_checkpoint_copy_missing")
    try:
        archived_checkpoint = json.loads(checkpoint_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DerivationError("parent_bundle_checkpoint_copy_invalid") from exc
    if not isinstance(archived_checkpoint, Mapping):
        raise DerivationError("parent_bundle_checkpoint_copy_invalid")
    archived_unsigned = dict(archived_checkpoint)
    archived_seal = archived_unsigned.pop("checkpoint_sha256", None)
    if not isinstance(archived_seal, str) or object_sha256(archived_unsigned) != archived_seal:
        raise DerivationError("parent_bundle_checkpoint_copy_seal_mismatch")
    durable_unsigned = dict(checkpoint)
    durable_seal = durable_unsigned.pop("checkpoint_sha256", None)
    if durable_seal != ref["checkpoint_sha256"]:
        raise DerivationError("parent_bundle_durable_checkpoint_seal_mismatch")
    for value in (archived_unsigned, durable_unsigned):
        value["last_heartbeat_at"] = None
        locator = value.get("output_artifact")
        if isinstance(locator, dict):
            locator["artifact_id"] = None
            locator["expires_at"] = None
    if archived_unsigned != durable_unsigned:
        raise DerivationError("parent_bundle_checkpoint_copy_differs_from_state")
    evidence_path = root / "upstream-catalogue-enrichment-evidence.json"
    validate_contribution_bytes(ref, evidence_path.read_bytes())
    result = json.loads((root / "upstream-catalogue-processing-result.json").read_bytes())
    if (
        not isinstance(result, Mapping)
        or result.get("generation_id") != ref["generation_id"]
        or result.get("status") != checkpoint.get("status")
        or result.get("source_id") != checkpoint.get("source_id")
        or result.get("processor_artifact_run_id") != ref["artifact_run_id"]
        or result.get("processor_run_id") != str(ref["artifact_name"]).removeprefix("upstream-catalogue-processing-")
        or result.get("candidate_available") is not True
    ):
        raise DerivationError("parent_bundle_result_identity_mismatch")
    return {"enrichment_evidence": json.loads(evidence_path.read_bytes()), "result": dict(result)}


def original_observation_hash(value: Mapping[str, Any]) -> str:
    """Hash a fully admitted A identity, never a legacy checkpoint projection."""
    return object_sha256(validate_original_observation(value))
