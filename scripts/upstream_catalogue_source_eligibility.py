#!/usr/bin/env python3
"""Closed source-contract selection for retained upstream observations.

This module owns no GitHub or state-branch mutation.  It builds a deterministic
contract from exact Git blobs, chooses one bounded retained-observation action,
and turns that decision into an internal verified value only after the caller
has re-read all native evidence through supplied readers.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "datapan.upstream-catalogue-source-eligibility.v1"
TARGET_CONTRACT_SCHEMA_VERSION = "datapan.upstream-catalogue-target-contract.v1"
SOURCE_EPOCH_SCHEMA_VERSION = "datapan.upstream-catalogue-source-epoch.v1"
SOURCE_WORK_REQUEST_SCHEMA_VERSION = "datapan.upstream-catalogue-source-work-request.v1"
SOURCE_RECOVERY_RESULT_SCHEMA_VERSION = "datapan.upstream-catalogue-source-recovery-result.v1"
SELECTION_DOMAIN = b"datapan/upstream-catalogue-source-selection/v1\0"
CONTRACT_DOMAIN = b"datapan/upstream-catalogue-target-contract/v1\0"
EPOCH_DOMAIN = b"datapan/upstream-catalogue-source-epoch/v1\0"
HEX40 = re.compile(r"^[a-f0-9]{40}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
REPOSITORY = re.compile(r"^[^/\s]+/[^/\s]+$")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$")

SAME_A_PROCESSING = "same_a_processing"
C_ONLY_REVALIDATION = "c_only_revalidation"
REQUIRES_NEW_A = "requires_new_a"
CONTRACT_CLASSES = (SAME_A_PROCESSING, C_ONLY_REVALIDATION, REQUIRES_NEW_A)
SELECTED_ACTIONS = frozenset({"rebuild", "resume", "revalidate_c"})
ACTIONS = frozenset((*SELECTED_ACTIONS, "no_op", "deferred", "requires_new_a", "blocked"))
TERMINAL_STATUSES = frozenset({"ready", "no-change"})
ACTIVE_STATUSES = frozenset({"queued", "validating", "enriching", "composing", "retry"})

# One ordered inventory is shared by the push filter, the B selector and the C
# revalidator.  Paths that affect only C stay out of B generation semantics.
# The two declarations are processing inputs: changing their reviewed shape
# may rebuild from the same immutable A, but never grants new acquisition.
CONTRACT_INPUTS: tuple[tuple[str, str], ...] = tuple(sorted((
    ("policy/source-refresh.json", REQUIRES_NEW_A),
    ("data/provider-index.json", REQUIRES_NEW_A),
    (".github/workflows/upstream-catalog-refresh.yml", REQUIRES_NEW_A),
    ("schemas/datapan.provider-index.v1.schema.json", REQUIRES_NEW_A),
    ("schemas/datapan.upstream-refresh-evidence.v1.schema.json", REQUIRES_NEW_A),

    ("scripts/process-upstream-catalogue-candidate.py", SAME_A_PROCESSING),
    ("scripts/generate-batch-link-detail-registry-patches.py", SAME_A_PROCESSING),
    ("scripts/upstream-catalogue-state-branch.py", SAME_A_PROCESSING),
    ("scripts/upstream_catalogue_handoff.py", SAME_A_PROCESSING),
    ("scripts/upstream_catalogue_derivation.py", SAME_A_PROCESSING),
    ("scripts/upstream_catalogue_cache_source.py", SAME_A_PROCESSING),
    ("scripts/prepare-upstream-catalogue-derivation.py", SAME_A_PROCESSING),
    ("scripts/upstream_catalogue_source_eligibility.py", SAME_A_PROCESSING),
    ("scripts/seoul_oa109_operation_declaration.py", SAME_A_PROCESSING),
    ("scripts/generate-seoul-oa109-subject-snapshot.py", SAME_A_PROCESSING),
    ("scripts/compose-upstream-catalogue-candidate.py", SAME_A_PROCESSING),
    ("contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json", SAME_A_PROCESSING),
    ("contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json", SAME_A_PROCESSING),
    (".github/workflows/upstream-catalogue-process.yml", SAME_A_PROCESSING),
    ("schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json", SAME_A_PROCESSING),
    ("schemas/datapan.catalogue-composition-receipt.v1.schema.json", SAME_A_PROCESSING),
    ("schemas/datapan.specs.v1.schema.json", SAME_A_PROCESSING),
    ("schemas/datapan.catalog-diff.v1.schema.json", SAME_A_PROCESSING),
    ("schemas/datapan.catalogue-enrichment-evidence.v1.schema.json", SAME_A_PROCESSING),
    ("schemas/datapan.upstream-catalogue-source-eligibility.v1.schema.json", SAME_A_PROCESSING),

    (".github/workflows/canonical-update-promotion.yml", C_ONLY_REVALIDATION),
    (".github/workflows/upstream-catalogue-health.yml", C_ONLY_REVALIDATION),
    ("scripts/run-canonical-update-promotion.py", C_ONLY_REVALIDATION),
    ("scripts/canonical_current_target_evidence.py", C_ONLY_REVALIDATION),
    ("scripts/canonical_source_target_disposition.py", C_ONLY_REVALIDATION),
    ("scripts/canonical_update_pr.py", C_ONLY_REVALIDATION),
    ("scripts/canonical_update_ci.py", C_ONLY_REVALIDATION),
    ("scripts/refresh-canonical-snapshot-evidence.py", C_ONLY_REVALIDATION),
    ("scripts/materialize-canonical-registry.py", C_ONLY_REVALIDATION),
    ("scripts/check-upstream-catalogue-health.py", C_ONLY_REVALIDATION),
    ("scripts/persist-upstream-catalogue-health.py", C_ONLY_REVALIDATION),
    ("scripts/prepare-upstream-catalogue-health-state-worktree.sh", C_ONLY_REVALIDATION),
    ("scripts/canonical_update_terminal_evidence.py", C_ONLY_REVALIDATION),
    ("schemas/datapan.canonical-update-promotion-terminal-outcome.v1.schema.json", C_ONLY_REVALIDATION),
    ("schemas/datapan.canonical-update-promotion-terminal-outcome.v2.schema.json", C_ONLY_REVALIDATION),
    ("schemas/datapan.canonical-update-promotion-journal.v1.schema.json", C_ONLY_REVALIDATION),
    ("schemas/datapan.canonical-update-promotion-receipt.v1.schema.json", C_ONLY_REVALIDATION),
    ("schemas/datapan.upstream-catalogue-health-policy.v1.schema.json", C_ONLY_REVALIDATION),
    ("schemas/datapan.upstream-catalogue-health-state-owner.v1.schema.json", C_ONLY_REVALIDATION),
    ("schemas/datapan.upstream-catalogue-health-state.v1.schema.json", C_ONLY_REVALIDATION),
    ("schemas/datapan.upstream-catalogue-health.v1.schema.json", C_ONLY_REVALIDATION),
    ("policy/upstream-catalogue-health.json", C_ONLY_REVALIDATION),
), key=lambda row: row[0]))

CONTRACT_PATHS = tuple(path for path, _effect in CONTRACT_INPUTS)
CONTRACT_CLASS_BY_PATH = dict(CONTRACT_INPUTS)
GENERATED_OR_OUTPUT_PREFIXES = (
    "data/data-go-kr.registry.json",
    "manifest.json",
    "schemas/index.json",
    "reports/",
    "fixtures/",
    "drafts/",
    "docs/",
    "README",
    ".datapan/",
)


class SourceEligibilityError(ValueError):
    """Source eligibility evidence is malformed, conflicting or unavailable."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def object_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise SourceEligibilityError(f"{field}_invalid")
    return value


def _sha1(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HEX40.fullmatch(value):
        raise SourceEligibilityError(f"{field}_invalid")
    return value


def _positive_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SourceEligibilityError(f"{field}_invalid")
    return value


def parse_timestamp(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise SourceEligibilityError(f"{field}_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceEligibilityError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise SourceEligibilityError(f"{field}_invalid")
    return parsed.astimezone(dt.timezone.utc)


def _git(root: pathlib.Path, *args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ("git", *args), cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise SourceEligibilityError("target_contract_git_object_unavailable")
    if binary:
        return result.stdout
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SourceEligibilityError("target_contract_git_identity_invalid") from exc


def contract_inventory(
    root: pathlib.Path,
    source_sha: str,
    *,
    read_blob: Callable[[str, str], bytes] | None = None,
) -> list[dict[str, Any]]:
    """Read every declared input from one immutable Git commit."""
    _sha1(source_sha, "target_contract_source_sha")
    reader = read_blob or (lambda revision, path: _git(root, "show", f"{revision}:{path}", binary=True))
    rows: list[dict[str, Any]] = []
    for position, (path, effect) in enumerate(CONTRACT_INPUTS):
        try:
            payload = reader(source_sha, path)
        except (OSError, SourceEligibilityError) as exc:
            raise SourceEligibilityError(f"target_contract_input_unavailable:{path}") from exc
        if not isinstance(payload, bytes):
            raise SourceEligibilityError(f"target_contract_input_invalid:{path}")
        rows.append({
            "position": position,
            "path": path,
            "class": effect,
            "bytes": len(payload),
            "sha256": sha256_bytes(payload),
        })
    return rows


def validate_inventory(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(CONTRACT_INPUTS):
        raise SourceEligibilityError("target_contract_inventory_shape_invalid")
    rows: list[dict[str, Any]] = []
    for position, expected in enumerate(CONTRACT_INPUTS):
        row = value[position]
        if (
            not isinstance(row, Mapping)
            or set(row) != {"position", "path", "class", "bytes", "sha256"}
            or row.get("position") != position
            or (row.get("path"), row.get("class")) != expected
        ):
            raise SourceEligibilityError("target_contract_inventory_order_invalid")
        _positive_int(row.get("bytes"), "target_contract_input_bytes", minimum=0)
        _digest(row.get("sha256"), "target_contract_input_sha256")
        rows.append(dict(row))
    return rows


def changed_contract_classes(
    previous_inventory: Sequence[Mapping[str, Any]] | None,
    current_inventory: Sequence[Mapping[str, Any]],
) -> list[str]:
    current = validate_inventory(list(current_inventory))
    if previous_inventory is None:
        return [SAME_A_PROCESSING]
    previous = validate_inventory(list(previous_inventory))
    changed = {
        current_row["class"]
        for previous_row, current_row in zip(previous, current, strict=True)
        if previous_row["sha256"] != current_row["sha256"]
        or previous_row["bytes"] != current_row["bytes"]
    }
    return [effect for effect in CONTRACT_CLASSES if effect in changed]


def target_contract_digest(inventory_sha256: str) -> str:
    """Identify one ordered target inventory independently of its Git carrier."""
    _digest(inventory_sha256, "target_contract_inventory_sha256")
    identity = {
        "schema_version": TARGET_CONTRACT_SCHEMA_VERSION,
        "inventory_sha256": inventory_sha256,
    }
    return sha256_bytes(CONTRACT_DOMAIN + canonical_json(identity))


def build_target_contract(
    root: pathlib.Path,
    source_sha: str,
    *,
    previous_inventory: Sequence[Mapping[str, Any]] | None = None,
    read_blob: Callable[[str, str], bytes] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    inventory = validate_inventory(contract_inventory(root, source_sha, read_blob=read_blob))
    tree = _git(root, "rev-parse", f"{source_sha}^{{tree}}")
    assert isinstance(tree, str)
    _sha1(tree, "target_contract_source_tree")
    inventory_sha = object_sha256(inventory)
    identity = {
        "schema_version": TARGET_CONTRACT_SCHEMA_VERSION,
        "source_sha": source_sha,
        "source_tree_sha": tree,
        "inventory_sha256": inventory_sha,
    }
    contract = {
        **identity,
        "contract_sha256": target_contract_digest(inventory_sha),
        "changed_classes": changed_contract_classes(previous_inventory, inventory),
    }
    return validate_target_contract(contract), inventory


def validate_target_contract(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "source_sha", "source_tree_sha", "inventory_sha256",
        "contract_sha256", "changed_classes",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise SourceEligibilityError("target_contract_shape_invalid")
    if value.get("schema_version") != TARGET_CONTRACT_SCHEMA_VERSION:
        raise SourceEligibilityError("target_contract_version_unsupported")
    _sha1(value.get("source_sha"), "target_contract_source_sha")
    _sha1(value.get("source_tree_sha"), "target_contract_source_tree")
    _digest(value.get("inventory_sha256"), "target_contract_inventory_sha256")
    _digest(value.get("contract_sha256"), "target_contract_sha256")
    evidence = {key: value[key] for key in (
        "schema_version", "source_sha", "source_tree_sha", "inventory_sha256",
    )}
    if value["contract_sha256"] != target_contract_digest(value["inventory_sha256"]):
        raise SourceEligibilityError("target_contract_digest_mismatch")
    classes = value.get("changed_classes")
    if (
        not isinstance(classes, list)
        or classes != [item for item in CONTRACT_CLASSES if item in set(classes)]
        or len(classes) != len(set(classes))
    ):
        raise SourceEligibilityError("target_contract_changed_classes_invalid")
    return {**evidence, "contract_sha256": value["contract_sha256"], "changed_classes": list(classes)}


def validate_evaluation(value: Any) -> dict[str, Any]:
    required = {"source_sha", "source_tree_sha", "event", "run_id", "run_attempt", "as_of"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise SourceEligibilityError("source_evaluation_shape_invalid")
    _sha1(value.get("source_sha"), "source_evaluation_sha")
    _sha1(value.get("source_tree_sha"), "source_evaluation_tree")
    if value.get("event") not in {"push", "schedule"}:
        raise SourceEligibilityError("source_evaluation_event_invalid")
    if not str(value.get("run_id", "")).isdigit():
        raise SourceEligibilityError("source_evaluation_run_invalid")
    _positive_int(value.get("run_attempt"), "source_evaluation_attempt")
    parse_timestamp(value.get("as_of"), "source_evaluation_clock")
    return dict(value)


def normalize_original_generation(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    generation_id = _digest(checkpoint.get("generation_id"), "original_generation_id")
    checkpoint_sha = _digest(checkpoint.get("checkpoint_sha256"), "original_checkpoint")
    inputs = checkpoint.get("generation_inputs")
    if not isinstance(inputs, Mapping):
        raise SourceEligibilityError("original_generation_inputs_missing")
    epoch = inputs.get("source_epoch")
    if epoch is None:
        return {
            "generation_id": generation_id,
            "checkpoint_ref": checkpoint_sha,
            "epoch": 1,
            "epoch_origin": "legacy-checkpoint-v1",
        }
    validated = validate_source_epoch(epoch)
    if validated["target_generation_id"] != generation_id:
        raise SourceEligibilityError("original_generation_epoch_target_mismatch")
    return {
        "generation_id": generation_id,
        "checkpoint_ref": checkpoint_sha,
        "epoch": validated["ordinal"],
        "epoch_origin": SOURCE_EPOCH_SCHEMA_VERSION,
    }


def build_source_epoch(
    *,
    original_observation_sha256: str,
    target_contract_sha256: str,
    predecessor_generation_id: str,
    target_generation_id: str | None,
    ordinal: int,
) -> dict[str, Any]:
    _digest(original_observation_sha256, "source_epoch_original_observation")
    _digest(target_contract_sha256, "source_epoch_target_contract")
    _digest(predecessor_generation_id, "source_epoch_predecessor")
    if target_generation_id is not None:
        _digest(target_generation_id, "source_epoch_target_generation")
    _positive_int(ordinal, "source_epoch_ordinal")
    identity = {
        "schema_version": SOURCE_EPOCH_SCHEMA_VERSION,
        "original_observation_sha256": original_observation_sha256,
        "target_contract_sha256": target_contract_sha256,
    }
    value = {
        **identity,
        "predecessor_generation_id": predecessor_generation_id,
        "target_generation_id": target_generation_id,
        "ordinal": ordinal,
    }
    return {
        **value,
        "epoch_id": sha256_bytes(EPOCH_DOMAIN + canonical_json(identity)),
    }


def validate_source_epoch(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "epoch_id", "original_observation_sha256",
        "target_contract_sha256", "predecessor_generation_id",
        "target_generation_id", "ordinal",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise SourceEligibilityError("source_epoch_shape_invalid")
    if value.get("schema_version") != SOURCE_EPOCH_SCHEMA_VERSION:
        raise SourceEligibilityError("source_epoch_version_unsupported")
    _digest(value.get("epoch_id"), "source_epoch_id")
    _digest(value.get("original_observation_sha256"), "source_epoch_original_observation")
    _digest(value.get("target_contract_sha256"), "source_epoch_target_contract")
    _digest(value.get("predecessor_generation_id"), "source_epoch_predecessor")
    if value.get("target_generation_id") is not None:
        _digest(value.get("target_generation_id"), "source_epoch_target_generation")
    _positive_int(value.get("ordinal"), "source_epoch_ordinal")
    # Work identity is original immutable A plus the classified target
    # contract.  Predecessor, target generation and ordinal explain lineage;
    # they must not create a second epoch for the same A/contract pair.
    identity = {key: value[key] for key in (
        "schema_version", "original_observation_sha256", "target_contract_sha256",
    )}
    if value["epoch_id"] != sha256_bytes(EPOCH_DOMAIN + canonical_json(identity)):
        raise SourceEligibilityError("source_epoch_digest_mismatch")
    return dict(value)


def bind_source_epoch_generation(
    generation_inputs: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Bind a B successor without placing its digest inside its own preimage.

    Legacy generation IDs remain the hash of the complete input object.  A
    source-epoch generation normalizes only ``target_generation_id`` to null
    for its identity preimage, derives the generation ID, then requires the
    explanatory field to equal that ID.
    """
    inputs = dict(generation_inputs)
    epoch_value = inputs.get("source_epoch")
    if epoch_value is None:
        return object_sha256(inputs), inputs
    epoch = validate_source_epoch(epoch_value)
    normalized = dict(epoch)
    normalized["target_generation_id"] = None
    preimage = dict(inputs)
    preimage["source_epoch"] = normalized
    generation_id = object_sha256(preimage)
    if epoch["target_generation_id"] not in {None, generation_id}:
        raise SourceEligibilityError("source_epoch_target_generation_mismatch")
    epoch["target_generation_id"] = generation_id
    inputs["source_epoch"] = epoch
    return generation_id, inputs


def selection_key(admission: Mapping[str, Any], target_contract: Mapping[str, Any]) -> str:
    immutable = {
        "producer_run_id": admission.get("producer_run_id"),
        "run_attempt": admission.get("run_attempt"),
        "head_sha": admission.get("head_sha"),
        "artifact_id": admission.get("artifact_id"),
        "artifact_digest_sha256": admission.get("artifact_digest_sha256"),
        "refresh_evidence_sha256": admission.get("refresh_evidence_sha256"),
        "target_contract_sha256": target_contract.get("contract_sha256"),
    }
    if (
        not str(immutable["producer_run_id"] or "").isdigit()
        or not str(immutable["artifact_id"] or "").isdigit()
    ):
        raise SourceEligibilityError("selection_admission_identity_invalid")
    _positive_int(immutable["run_attempt"], "selection_admission_attempt")
    _sha1(immutable["head_sha"], "selection_admission_head")
    for field in ("artifact_digest_sha256", "refresh_evidence_sha256", "target_contract_sha256"):
        _digest(immutable[field], f"selection_{field}")
    return sha256_bytes(SELECTION_DOMAIN + canonical_json(immutable))


def source_work_subject(
    admission: Mapping[str, Any], original_observation: Mapping[str, Any],
    checkpoint: Mapping[str, Any], prior_target_contract: Mapping[str, Any] | None,
    target_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the immutable retained-A subject used by a workflow locator."""
    reference_keys = (
        "admission_id", "producer_run_id", "run_attempt", "head_sha",
        "artifact_id", "artifact_name", "artifact_digest_sha256",
        "refresh_evidence_sha256", "observed_at", "candidate_sha256",
    )
    reference = {key: admission.get(key) for key in reference_keys}
    key = selection_key(reference, target_contract)
    return {
        "selection_key": key,
        "original_admission": reference,
        "original_observation_sha256": object_sha256(original_observation),
        "original_generation": normalize_original_generation(checkpoint),
        "prior_target_contract": (
            validate_target_contract(prior_target_contract)
            if prior_target_contract is not None else None
        ),
    }


def validate_source_work_subject(value: Any, target_contract: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "selection_key", "original_admission", "original_observation_sha256",
        "original_generation", "prior_target_contract",
    }:
        raise SourceEligibilityError("source_work_subject_shape_invalid")
    admission = value.get("original_admission")
    required_admission = {
        "admission_id", "producer_run_id", "run_attempt", "head_sha",
        "artifact_id", "artifact_name", "artifact_digest_sha256",
        "refresh_evidence_sha256", "observed_at", "candidate_sha256",
    }
    if not isinstance(admission, Mapping) or set(admission) != required_admission:
        raise SourceEligibilityError("source_work_subject_admission_invalid")
    if (
        not isinstance(admission.get("admission_id"), str)
        or not HEX64.fullmatch(admission["admission_id"])
        or not str(admission.get("producer_run_id", "")).isdigit()
        or not str(admission.get("artifact_id", "")).isdigit()
        or admission.get("artifact_name") != f"upstream-catalog-refresh-{admission.get('producer_run_id')}"
    ):
        raise SourceEligibilityError("source_work_subject_admission_invalid")
    _positive_int(admission.get("run_attempt"), "source_work_subject_admission_attempt")
    _sha1(admission.get("head_sha"), "source_work_subject_admission_head")
    for field in ("artifact_digest_sha256", "refresh_evidence_sha256", "candidate_sha256"):
        _digest(admission.get(field), f"source_work_subject_{field}")
    parse_timestamp(admission.get("observed_at"), "source_work_subject_observed_at")
    _digest(value.get("original_observation_sha256"), "source_work_subject_original_observation")
    generation = value.get("original_generation")
    if not isinstance(generation, Mapping) or set(generation) != {
        "generation_id", "checkpoint_ref", "epoch", "epoch_origin",
    }:
        raise SourceEligibilityError("source_work_subject_generation_invalid")
    _digest(generation.get("generation_id"), "source_work_subject_generation")
    _digest(generation.get("checkpoint_ref"), "source_work_subject_checkpoint")
    _positive_int(generation.get("epoch"), "source_work_subject_epoch")
    if generation.get("epoch_origin") not in {"legacy-checkpoint-v1", SOURCE_EPOCH_SCHEMA_VERSION}:
        raise SourceEligibilityError("source_work_subject_epoch_origin_invalid")
    prior = value.get("prior_target_contract")
    if prior is not None:
        prior = validate_target_contract(prior)
    expected_key = selection_key(admission, target_contract)
    if value.get("selection_key") != expected_key:
        raise SourceEligibilityError("source_work_subject_selection_key_invalid")
    return {
        "selection_key": expected_key,
        "original_admission": dict(admission),
        "original_observation_sha256": value["original_observation_sha256"],
        "original_generation": dict(generation),
        "prior_target_contract": prior,
    }


def validate_source_work_request(
    value: Any, *, root: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Validate the closed workflow locator without upgrading it to authority."""
    required = {
        "schema_version", "repository", "source_id", "source_scope", "evaluation",
        "target_contract", "target_inventory", "source_subject", "action", "reason_code",
        "selected_generation_id", "producer_run_id", "artifact_id", "artifact_name",
        "selection_state_sha",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise SourceEligibilityError("source_work_request_shape_invalid")
    if (
        value.get("schema_version") != SOURCE_WORK_REQUEST_SCHEMA_VERSION
        or not REPOSITORY.fullmatch(str(value.get("repository", "")))
        or value.get("source_id") != "data_go_kr"
        or value.get("source_scope") != "aggregate_supported_catalog"
        or value.get("action") not in ACTIONS
        or not isinstance(value.get("reason_code"), str)
        or not re.fullmatch(r"[a-z0-9_]{1,96}", value["reason_code"])
    ):
        raise SourceEligibilityError("source_work_request_identity_invalid")
    evaluation = validate_evaluation(value.get("evaluation"))
    selection_state_sha = value.get("selection_state_sha")
    if selection_state_sha is not None:
        _sha1(selection_state_sha, "source_work_request_selection_state")
    target = validate_target_contract(value.get("target_contract"))
    inventory = validate_inventory(value.get("target_inventory"))
    if (
        (evaluation["source_sha"], evaluation["source_tree_sha"])
        != (target["source_sha"], target["source_tree_sha"])
        or object_sha256(inventory) != target["inventory_sha256"]
    ):
        raise SourceEligibilityError("source_work_request_target_invalid")
    subject_value = value.get("source_subject")
    if subject_value is None:
        if any(value.get(field) is not None for field in (
            "selected_generation_id", "producer_run_id", "artifact_id", "artifact_name",
        )):
            raise SourceEligibilityError("source_work_request_subject_locator_invalid")
        subject = None
    else:
        subject = validate_source_work_subject(subject_value, target)
        admission = subject["original_admission"]
        if (
            str(value.get("producer_run_id")) != str(admission["producer_run_id"])
            or str(value.get("artifact_id")) != str(admission["artifact_id"])
            or value.get("artifact_name") != admission["artifact_name"]
        ):
            raise SourceEligibilityError("source_work_request_subject_locator_invalid")
        selected = value.get("action") in SELECTED_ACTIONS
        expected_generation = subject["original_generation"]["generation_id"] if selected else None
        if value.get("selected_generation_id") != expected_generation:
            raise SourceEligibilityError("source_work_request_selected_generation_invalid")
    if root is not None:
        resolved_root = root.resolve()
        tree = _git(resolved_root, "rev-parse", f"{evaluation['source_sha']}^{{tree}}")
        if tree != evaluation["source_tree_sha"]:
            raise SourceEligibilityError("source_work_request_git_tree_mismatch")
        actual_inventory = validate_inventory(contract_inventory(resolved_root, evaluation["source_sha"]))
        if actual_inventory != inventory:
            raise SourceEligibilityError("source_work_request_git_inventory_mismatch")
        if target_contract_digest(object_sha256(actual_inventory)) != target["contract_sha256"]:
            raise SourceEligibilityError("source_work_request_git_contract_mismatch")
    return {
        "schema_version": SOURCE_WORK_REQUEST_SCHEMA_VERSION,
        "repository": value["repository"],
        "source_id": value["source_id"],
        "source_scope": value["source_scope"],
        "evaluation": evaluation,
        "target_contract": target,
        "target_inventory": inventory,
        "source_subject": subject,
        "action": value["action"],
        "reason_code": value["reason_code"],
        "selected_generation_id": value.get("selected_generation_id"),
        "producer_run_id": value.get("producer_run_id"),
        "artifact_id": value.get("artifact_id"),
        "artifact_name": value.get("artifact_name"),
        "selection_state_sha": selection_state_sha,
    }


def validate_source_recovery_result(
    value: Any, *, root: pathlib.Path | None = None,
) -> dict[str, Any]:
    required = {
        "schema_version", "repository", "source_id", "source_scope",
        "source_work_request", "execution",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise SourceEligibilityError("source_recovery_result_shape_invalid")
    if value.get("schema_version") != SOURCE_RECOVERY_RESULT_SCHEMA_VERSION:
        raise SourceEligibilityError("source_recovery_result_version_invalid")
    request = validate_source_work_request(value.get("source_work_request"), root=root)
    if any(value.get(field) != request[field] for field in ("repository", "source_id", "source_scope")):
        raise SourceEligibilityError("source_recovery_result_subject_invalid")
    execution = value.get("execution")
    required_execution = {
        "event", "run_id", "run_attempt", "head_sha", "selected", "outcome",
        "reason_code", "decision", "selected_generation_id", "selected_producer_run_id",
        "processing_status", "processing_reason", "result_generation_id", "persistence_status",
        "result_state_sha",
    }
    if not isinstance(execution, Mapping) or set(execution) != required_execution:
        raise SourceEligibilityError("source_recovery_execution_shape_invalid")
    evaluation = request["evaluation"]
    if (
        execution.get("event") != evaluation["event"]
        or str(execution.get("run_id")) != str(evaluation["run_id"])
        or execution.get("run_attempt") != evaluation["run_attempt"]
        or execution.get("head_sha") != evaluation["source_sha"]
        or not isinstance(execution.get("selected"), bool)
        or execution.get("decision") not in {"process", "idle"}
        or execution.get("outcome") not in {
            "rebuilt", "resumed", "c_revalidation_required", "verified_no_op",
            "deferred", "requires_new_a", "blocked",
        }
        or execution.get("processing_status") not in {
            "ready", "no-change", "retry", "quarantined", "idle",
        }
        or execution.get("persistence_status") not in {
            "not_required", "published", "replay_recorded", "replay_unmodified",
            "checkpoint_recorded", "interrupted",
        }
    ):
        raise SourceEligibilityError("source_recovery_execution_identity_invalid")
    for field in ("reason_code", "processing_reason"):
        if not isinstance(execution.get(field), str) or not re.fullmatch(r"[a-z0-9_]{1,96}", execution[field]):
            raise SourceEligibilityError("source_recovery_execution_reason_invalid")
    for field in ("selected_generation_id", "result_generation_id"):
        item = execution.get(field)
        if item is not None:
            _digest(item, f"source_recovery_{field}")
    result_state_sha = execution.get("result_state_sha")
    if result_state_sha is not None:
        _sha1(result_state_sha, "source_recovery_result_state")
    producer = execution.get("selected_producer_run_id")
    if producer is not None and not str(producer).isdigit():
        raise SourceEligibilityError("source_recovery_execution_producer_invalid")
    if execution["selected"]:
        if execution["decision"] == "process":
            if request["action"] not in {"rebuild", "resume"}:
                raise SourceEligibilityError("source_recovery_execution_action_invalid")
            if (
                execution["selected_generation_id"] != request["selected_generation_id"]
                or str(execution["selected_producer_run_id"]) != str(request["producer_run_id"])
            ):
                raise SourceEligibilityError("source_recovery_execution_selection_invalid")
        elif request["action"] in {"rebuild", "resume"}:
            raise SourceEligibilityError("source_recovery_execution_action_invalid")
    elif execution["outcome"] != "deferred" or execution["reason_code"] != "shared_work_unit_selected":
        raise SourceEligibilityError("source_recovery_execution_deferred_invalid")
    action = request["action"]
    if execution["selected"]:
        expected_outcomes = {
            "rebuild": {"rebuilt", "blocked"},
            "resume": {"resumed", "blocked"},
            "revalidate_c": {"c_revalidation_required"},
            "no_op": {"verified_no_op"},
            "deferred": {"deferred"},
            "requires_new_a": {"requires_new_a"},
            "blocked": {"blocked"},
        }
        if execution["outcome"] not in expected_outcomes[action]:
            raise SourceEligibilityError("source_recovery_execution_outcome_invalid")
        if action in {"rebuild", "resume"}:
            durable = (
                execution["processing_status"] in {"ready", "no-change"}
                and execution["persistence_status"] == "published"
            )
            if durable != (execution["outcome"] in {"rebuilt", "resumed"}):
                raise SourceEligibilityError("source_recovery_execution_persistence_invalid")
            if durable and result_state_sha is None:
                raise SourceEligibilityError("source_recovery_execution_result_state_missing")
        elif execution["persistence_status"] != "not_required":
            raise SourceEligibilityError("source_recovery_execution_idle_persistence_invalid")
    return {
        "schema_version": SOURCE_RECOVERY_RESULT_SCHEMA_VERSION,
        "repository": request["repository"],
        "source_id": request["source_id"],
        "source_scope": request["source_scope"],
        "source_work_request": request,
        "execution": dict(execution),
    }


def build_source_recovery_result(
    source_work_request: Mapping[str, Any], processing_result: Mapping[str, Any],
    *, root: pathlib.Path, repository: str, event: str, run_id: str,
    run_attempt: int, head_sha: str, decision: str,
    selected_generation_id: str | None, selected_producer_run_id: str | None,
    persistence_status: str, result_state_sha: str | None = None,
) -> dict[str, Any]:
    """Bind one selector outcome to the exact B workflow result that observed it."""
    request = validate_source_work_request(source_work_request, root=root)
    if (
        request["repository"].casefold() != repository.casefold()
        or request["evaluation"]["event"] != event
        or request["evaluation"]["run_id"] != run_id
        or request["evaluation"]["run_attempt"] != run_attempt
        or request["evaluation"]["source_sha"] != head_sha
        or decision not in {"process", "idle"}
        or persistence_status not in {
            "not_required", "published", "replay_recorded", "replay_unmodified",
            "checkpoint_recorded", "interrupted",
        }
        or not isinstance(processing_result, Mapping)
    ):
        raise SourceEligibilityError("source_recovery_builder_identity_invalid")
    status = processing_result.get("status")
    reason = processing_result.get("reason")
    if (
        status not in {"ready", "no-change", "retry", "quarantined", "idle"}
        or not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_]{1,96}", reason)
        or processing_result.get("source_id", "data_go_kr") != "data_go_kr"
        or processing_result.get("processor_run_id") not in {None, f"{run_id}-{run_attempt}"}
        or str(processing_result.get("processor_artifact_run_id", run_id)) != run_id
    ):
        raise SourceEligibilityError("source_recovery_builder_processing_result_invalid")
    action = request["action"]
    selected = bool(
        (action in {"rebuild", "resume"} and decision == "process"
         and selected_generation_id == request["selected_generation_id"]
         and str(selected_producer_run_id) == str(request["producer_run_id"]))
        or (action not in {"rebuild", "resume"} and decision == "idle")
    )
    durable_process = status in {"ready", "no-change"} and persistence_status == "published"
    outcome = {
        "rebuild": "rebuilt" if status in {"ready", "no-change"} and durable_process else "blocked",
        "resume": "resumed" if durable_process else "blocked",
        "revalidate_c": "c_revalidation_required",
        "no_op": "verified_no_op",
        "deferred": "deferred",
        "requires_new_a": "requires_new_a",
        "blocked": "blocked",
    }[action] if selected else "deferred"
    outcome_reason = request["reason_code"] if selected else "shared_work_unit_selected"
    if selected and action in {"rebuild", "resume"} and not durable_process:
        outcome_reason = (
            "source_recovery_state_not_persisted"
            if status in {"ready", "no-change"}
            else reason
        )
    result_generation = processing_result.get("generation_id")
    if result_generation is not None:
        _digest(result_generation, "source_recovery_result_generation")
    record = {
        "schema_version": SOURCE_RECOVERY_RESULT_SCHEMA_VERSION,
        "repository": request["repository"],
        "source_id": request["source_id"],
        "source_scope": request["source_scope"],
        "source_work_request": request,
        "execution": {
            "event": event,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "head_sha": head_sha,
            "selected": selected,
            "outcome": outcome,
            "reason_code": outcome_reason,
            "decision": decision,
            "selected_generation_id": selected_generation_id,
            "selected_producer_run_id": selected_producer_run_id,
            "processing_status": status,
            "processing_reason": reason,
            "result_generation_id": result_generation,
            "persistence_status": persistence_status,
            "result_state_sha": result_state_sha,
        },
    }
    return validate_source_recovery_result(record, root=root)


def build_source_recovery_idle_processing_result(
    source_work_request: Mapping[str, Any], *, root: pathlib.Path,
    processor_run_id: str, processor_artifact_run_id: str, reason: str,
) -> dict[str, Any]:
    """Create the exact no-candidate B result for one validated source request."""
    request = validate_source_work_request(source_work_request, root=root)
    if (
        request["action"] in {"rebuild", "resume"}
        or not re.fullmatch(r"[0-9]{1,20}-[1-9][0-9]*", processor_run_id)
        or not processor_artifact_run_id.isdigit()
        or not re.fullmatch(r"[a-z0-9_]{1,96}", reason)
        or reason != request["reason_code"]
    ):
        raise SourceEligibilityError("source_recovery_idle_result_identity_invalid")
    return {
        "status": "idle",
        "reason": reason,
        "source_recovery_action": request["action"],
        "source_id": request["source_id"],
        "source_sha": request["evaluation"]["source_sha"],
        "source_tree_sha": request["evaluation"]["source_tree_sha"],
        "target_contract_sha256": request["target_contract"]["contract_sha256"],
        "processor_run_id": processor_run_id,
        "processor_artifact_run_id": processor_artifact_run_id,
    }


def validate_source_recovery_idle_processing_result(
    source_work_request: Mapping[str, Any], processing_result: Any, *, root: pathlib.Path,
    processor_run_id: str, processor_artifact_run_id: str,
) -> dict[str, Any]:
    if not isinstance(processing_result, Mapping):
        raise SourceEligibilityError("source_recovery_idle_result_shape_invalid")
    reason = processing_result.get("reason")
    if not isinstance(reason, str):
        raise SourceEligibilityError("source_recovery_idle_result_reason_invalid")
    expected = build_source_recovery_idle_processing_result(
        source_work_request, root=root, processor_run_id=processor_run_id,
        processor_artifact_run_id=processor_artifact_run_id, reason=reason,
    )
    if canonical_json(processing_result) != canonical_json(expected):
        raise SourceEligibilityError("source_recovery_idle_result_mismatch")
    return expected


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[str, str]:
    original = candidate.get("original_observation")
    observed_at = original.get("observed_at") if isinstance(original, Mapping) else ""
    return str(observed_at), str(candidate.get("generation_id", ""))


def _candidate_action(
    candidate: Mapping[str, Any], target_contract: Mapping[str, Any], as_of: dt.datetime,
) -> tuple[str, str]:
    expiry = parse_timestamp(candidate.get("artifact_expires_at"), "retained_artifact_expiry")
    if expiry <= as_of:
        return "requires_new_a", "retained_observation_expired"
    if candidate.get("accounting_exhausted") is True:
        return "blocked", "retained_request_budget_exhausted"
    prior_contract = candidate.get("target_contract")
    prior_digest = prior_contract.get("contract_sha256") if isinstance(prior_contract, Mapping) else None
    current_digest = target_contract["contract_sha256"]
    status = candidate.get("status")
    lease = candidate.get("lease")
    live_lease = False
    if isinstance(lease, Mapping):
        try:
            live_lease = parse_timestamp(lease.get("expires_at"), "retained_lease_expiry") > as_of
        except SourceEligibilityError:
            return "blocked", "retained_lease_invalid"
    if prior_digest == current_digest:
        if status in TERMINAL_STATUSES and candidate.get("canonical_completed") is True:
            return "no_op", "target_contract_already_completed"
        if live_lease:
            return "deferred", "target_contract_live_lease"
        if status in ACTIVE_STATUSES:
            return "resume", "target_contract_resume"
        if status in TERMINAL_STATUSES:
            return "revalidate_c", "target_contract_requires_c_completion"
        return "blocked", "retained_generation_status_unsupported"
    changed = target_contract.get("changed_classes")
    if not isinstance(changed, list) or not changed:
        return "blocked", "target_contract_change_unclassified"
    if REQUIRES_NEW_A in changed:
        return "requires_new_a", "target_contract_requires_new_a"
    if live_lease:
        return "deferred", "predecessor_live_lease"
    if status in ACTIVE_STATUSES:
        # A changed contract never mutates an in-flight generation.  Its
        # ordinary recovery keeps priority; a later push/schedule can derive a
        # successor after that checkpoint reaches a terminal state.
        return "deferred", "predecessor_generation_still_active"
    if set(changed) == {C_ONLY_REVALIDATION}:
        return "revalidate_c", "c_only_contract_changed"
    if SAME_A_PROCESSING in changed:
        return "rebuild", "same_a_processing_contract_changed"
    return "blocked", "target_contract_change_unsupported"


def _decision(
    *, repository: str, source_id: str, source_scope: str, action: str,
    reason_code: str, evaluation: Mapping[str, Any], target_contract: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise SourceEligibilityError("source_selection_action_invalid")
    selected = action in SELECTED_ACTIONS
    if selected != isinstance(evidence, Mapping):
        raise SourceEligibilityError("source_selection_evidence_authority_invalid")
    key = selection_key(evidence["admission"], target_contract) if selected else None
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "source_id": source_id,
        "source_scope": source_scope,
        "action": action,
        "reason_code": reason_code,
        "evaluation": validate_evaluation(evaluation),
        "selection_key": key,
        "evidence": dict(evidence) if evidence is not None else None,
    }


def select_source_work(
    *,
    repository: str,
    source_id: str,
    source_scope: str,
    evaluation: Mapping[str, Any],
    target_contract: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Choose at most one retained work unit without mutating durable state."""
    if not REPOSITORY.fullmatch(repository):
        raise SourceEligibilityError("source_selection_repository_invalid")
    if not source_id or not source_scope:
        raise SourceEligibilityError("source_selection_scope_invalid")
    evaluated = validate_evaluation(evaluation)
    target = validate_target_contract(target_contract)
    if (evaluated["source_sha"], evaluated["source_tree_sha"]) != (
        target["source_sha"], target["source_tree_sha"],
    ):
        raise SourceEligibilityError("source_selection_target_identity_mismatch")
    as_of = parse_timestamp(evaluated["as_of"], "source_evaluation_clock")
    if len(candidates) > 64:
        raise SourceEligibilityError("source_selection_candidate_bound_exceeded")
    decisions: list[tuple[Mapping[str, Any], str, str]] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        if (
            candidate.get("source_id") != source_id
            or candidate.get("source_scope") != source_scope
            or not HEX64.fullmatch(str(candidate.get("generation_id", "")))
        ):
            raise SourceEligibilityError("source_selection_candidate_identity_invalid")
        action, reason = _candidate_action(candidate, target, as_of)
        decisions.append((candidate, action, reason))
    if not decisions:
        return _decision(
            repository=repository, source_id=source_id, source_scope=source_scope,
            action="blocked", reason_code="no_retained_admitted_observation",
            evaluation=evaluated, target_contract=target, evidence=None,
        )
    # Preserve ordinary oldest-work ordering. A live lease or an active
    # predecessor is a work-order barrier, while terminal no-op/expired/
    # exhausted rows may be skipped in favor of the oldest eligible unit.
    # This keeps push from overtaking work that the ordinary schedule must
    # recover, without letting permanently closed rows starve later work.
    candidate, action, reason = next(
        (row for row in decisions if row[1] in SELECTED_ACTIONS or row[1] == "deferred"),
        decisions[0],
    )
    evidence = candidate.get("evidence") if action in SELECTED_ACTIONS else None
    return _decision(
        repository=repository, source_id=source_id, source_scope=source_scope,
        action=action, reason_code=reason, evaluation=evaluated,
        target_contract=target, evidence=evidence,
    )


_VERIFIED = object()


@dataclasses.dataclass(frozen=True, slots=True, init=False)
class VerifiedSourceEligibility:
    """Internal capability returned only after native evidence re-resolution."""

    selection: Mapping[str, Any]
    original_generation: Mapping[str, Any]
    target_contract: Mapping[str, Any]
    source_epoch: Mapping[str, Any]

    def __init__(self, token: object, selection: Mapping[str, Any]) -> None:
        if token is not _VERIFIED:
            raise SourceEligibilityError("verified_source_eligibility_constructor_private")
        evidence = selection.get("evidence")
        if not isinstance(evidence, Mapping):
            raise SourceEligibilityError("verified_source_eligibility_evidence_missing")
        object.__setattr__(self, "selection", dict(selection))
        object.__setattr__(self, "original_generation", dict(evidence["original_generation"]))
        object.__setattr__(self, "target_contract", dict(evidence["target_contract"]))
        object.__setattr__(self, "source_epoch", dict(evidence["source_epoch"]))


def resolve_verified_source_eligibility(
    selection: Mapping[str, Any],
    *,
    reread_candidates: Callable[[], Sequence[Mapping[str, Any]]],
) -> VerifiedSourceEligibility:
    """Recompute a selected result from native readers before granting use."""
    if not isinstance(selection, Mapping) or selection.get("action") not in SELECTED_ACTIONS:
        raise SourceEligibilityError("source_eligibility_not_selected")
    target = selection.get("evidence", {}).get("target_contract")
    if not isinstance(target, Mapping):
        raise SourceEligibilityError("source_eligibility_target_missing")
    recomputed = select_source_work(
        repository=str(selection.get("repository", "")),
        source_id=str(selection.get("source_id", "")),
        source_scope=str(selection.get("source_scope", "")),
        evaluation=selection.get("evaluation", {}),
        target_contract=target,
        candidates=reread_candidates(),
    )
    if canonical_json(recomputed) != canonical_json(selection):
        raise SourceEligibilityError("source_eligibility_native_reread_mismatch")
    evidence = recomputed["evidence"]
    original_generation = evidence.get("original_generation")
    if not isinstance(original_generation, Mapping) or set(original_generation) != {
        "generation_id", "checkpoint_ref", "epoch", "epoch_origin",
    }:
        raise SourceEligibilityError("source_eligibility_original_generation_invalid")
    _digest(original_generation.get("generation_id"), "source_eligibility_original_generation")
    _digest(original_generation.get("checkpoint_ref"), "source_eligibility_original_checkpoint")
    _positive_int(original_generation.get("epoch"), "source_eligibility_original_epoch")
    if original_generation.get("epoch_origin") not in {"legacy-checkpoint-v1", SOURCE_EPOCH_SCHEMA_VERSION}:
        raise SourceEligibilityError("source_eligibility_epoch_origin_invalid")
    validate_target_contract(evidence.get("target_contract"))
    validate_source_epoch(evidence.get("source_epoch"))
    return VerifiedSourceEligibility(_VERIFIED, recomputed)


def relevant_push_paths(paths: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return declared paths and ignored output paths; reject ambiguous input."""
    if len(paths) > 300:
        raise SourceEligibilityError("push_path_bound_exceeded")
    declared: list[str] = []
    ignored: list[str] = []
    for path in paths:
        if not isinstance(path, str) or not path or path.startswith("/") or ".." in pathlib.PurePosixPath(path).parts:
            raise SourceEligibilityError("push_path_invalid")
        if path in CONTRACT_CLASS_BY_PATH:
            declared.append(path)
        elif path.startswith(GENERATED_OR_OUTPUT_PREFIXES):
            ignored.append(path)
    return sorted(set(declared)), sorted(set(ignored))


def select_source_locator(
    *,
    target_contract: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Choose a workflow locator without granting source-work authority."""
    target = validate_target_contract(target_contract)
    evaluated = validate_evaluation(evaluation)
    as_of = parse_timestamp(evaluated["as_of"], "source_evaluation_clock")
    if len(candidates) > 64:
        raise SourceEligibilityError("source_selection_candidate_bound_exceeded")
    decisions = [
        (candidate, *_candidate_action(candidate, target, as_of))
        for candidate in sorted(candidates, key=_candidate_sort_key)
    ]
    if not decisions:
        return {
            "schema_version": "datapan.upstream-catalogue-source-work-locator.v1",
            "action": "blocked",
            "reason_code": "no_retained_admitted_observation",
            "generation_id": None,
        }
    candidate, action, reason = next(
        (row for row in decisions if row[1] in SELECTED_ACTIONS or row[1] == "deferred"),
        decisions[0],
    )
    return {
        "schema_version": "datapan.upstream-catalogue-source-work-locator.v1",
        "action": action,
        "reason_code": reason,
        "generation_id": candidate.get("generation_id") if action in SELECTED_ACTIONS else None,
    }


def _load_local_module(path: pathlib.Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SourceEligibilityError("source_selection_validator_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_json(path: pathlib.Path, maximum_bytes: int) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum_bytes:
        raise SourceEligibilityError("source_selection_state_file_invalid")
    try:
        return json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceEligibilityError("source_selection_state_json_invalid") from exc


def _legacy_changed_classes(
    root: pathlib.Path, original_source_sha: str, current_source_sha: str,
) -> list[str]:
    """Conservatively upgrade a checkpoint written before target contracts."""
    for path, effect in CONTRACT_INPUTS:
        if effect != REQUIRES_NEW_A:
            continue
        try:
            prior = _git(root, "show", f"{original_source_sha}:{path}", binary=True)
            current = _git(root, "show", f"{current_source_sha}:{path}", binary=True)
        except SourceEligibilityError:
            return [REQUIRES_NEW_A]
        if prior != current:
            return [REQUIRES_NEW_A]
    # The versioned inventory/helper did not exist in the legacy checkpoint,
    # so its first explicit transition is a same-A processing successor.
    return [SAME_A_PROCESSING]


def target_contract_from_predecessor(
    root: pathlib.Path,
    current_source_sha: str,
    predecessor: Mapping[str, Any],
    original_observation: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Recompute the target classification from Git and one sealed predecessor."""
    inputs = predecessor.get("generation_inputs")
    if not isinstance(inputs, Mapping):
        raise SourceEligibilityError("source_selection_generation_inputs_invalid")
    original_source_sha = original_observation.get("producer_head_sha")
    _sha1(original_source_sha, "source_selection_original_source_sha")
    current_contract, current_inventory = build_target_contract(root, current_source_sha)
    prior_contract_value = inputs.get("source_target_contract")
    if prior_contract_value is None:
        return {
            **current_contract,
            "changed_classes": _legacy_changed_classes(
                root, original_source_sha, current_source_sha,
            ),
        }, current_inventory

    prior_contract = validate_target_contract(prior_contract_value)
    prior_recomputed, prior_inventory = build_target_contract(
        root, prior_contract["source_sha"],
    )
    if any(
        prior_recomputed[field] != prior_contract[field]
        for field in (
            "schema_version", "source_sha", "source_tree_sha",
            "inventory_sha256", "contract_sha256",
        )
    ):
        raise SourceEligibilityError("source_selection_stored_target_contract_mismatch")
    return build_target_contract(
        root, current_source_sha, previous_inventory=prior_inventory,
    )


def source_work_request_from_state(
    *,
    root: pathlib.Path,
    state_dir: pathlib.Path,
    repository: str,
    source_id: str,
    source_scope: str,
    evaluation: Mapping[str, Any],
    include_active: bool = False,
    selection_state_sha: str | None = None,
) -> dict[str, Any]:
    """Build a non-authoritative bounded locator from sealed processor state.

    The preparer must later authenticate the chosen A/B/C evidence and call
    ``resolve_verified_source_eligibility``.  This request only lets the
    workflow decide which immutable A artifact it should download.
    """
    evaluated = validate_evaluation(evaluation)
    root = root.resolve()
    state_dir = state_dir.resolve()
    index_path = state_dir / "sources" / source_id / "index.json"
    if not index_path.is_file():
        current_contract, current_inventory = build_target_contract(root, evaluated["source_sha"])
        return validate_source_work_request({
            "schema_version": SOURCE_WORK_REQUEST_SCHEMA_VERSION,
            "repository": repository,
            "source_id": source_id,
            "source_scope": source_scope,
            "evaluation": evaluated,
            "target_contract": current_contract,
            "target_inventory": current_inventory,
            "source_subject": None,
            "action": "blocked",
            "reason_code": "no_retained_admitted_observation",
            "selected_generation_id": None,
            "producer_run_id": None,
            "artifact_id": None,
            "artifact_name": None,
            "selection_state_sha": selection_state_sha,
        }, root=root)
    index = _load_json(index_path, 2 * 1024 * 1024)
    if (
        not isinstance(index, Mapping)
        or index.get("schema_version") != "datapan.upstream-catalogue-checkpoint.v1"
        or not isinstance(index.get("generations"), list)
        or len(index["generations"]) > 64
    ):
        raise SourceEligibilityError("source_selection_index_invalid")
    handoff = _load_local_module(root / "scripts/upstream_catalogue_handoff.py", "source_selection_handoff")
    derivation = _load_local_module(root / "scripts/upstream_catalogue_derivation.py", "source_selection_derivation")
    processor = _load_local_module(
        root / "scripts/process-upstream-catalogue-candidate.py", "source_selection_processor",
    )
    ledger = index.get("collector_handoff")
    if not isinstance(ledger, Mapping):
        raise SourceEligibilityError("source_selection_admission_ledger_missing")
    admitted = handoff.validate_ledger(ledger)["admitted_observations"]
    schema = _load_json(root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json", 1024 * 1024)
    import jsonschema

    indexed_ids: list[str] = []
    for row in index["generations"]:
        if not isinstance(row, Mapping) or not isinstance(row.get("generation_id"), str):
            raise SourceEligibilityError("source_selection_index_generation_invalid")
        indexed_ids.append(_digest(row["generation_id"], "source_selection_generation"))
    if len(indexed_ids) != len(set(indexed_ids)):
        raise SourceEligibilityError("source_selection_index_generation_duplicate")
    checkpoints: dict[str, dict[str, Any]] = {}
    shadowed: set[str] = set()
    for generation_id in sorted(indexed_ids):
        path = state_dir / "sources" / source_id / "generations" / f"{generation_id}.json"
        checkpoint = _load_json(path, 256 * 1024)
        if not isinstance(checkpoint, dict):
            raise SourceEligibilityError("source_selection_checkpoint_invalid")
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(checkpoint)
        unsigned = dict(checkpoint)
        claimed = unsigned.pop("checkpoint_sha256", None)
        if claimed != object_sha256(unsigned) or checkpoint.get("generation_id") != generation_id:
            raise SourceEligibilityError("source_selection_checkpoint_digest_invalid")
        inputs = checkpoint.get("generation_inputs")
        if not isinstance(inputs, Mapping):
            raise SourceEligibilityError("source_selection_generation_inputs_invalid")
        envelope = inputs.get("same_observation_derivation")
        if envelope is not None:
            shadowed.update(derivation.validate_derivation_envelope(envelope)["ancestor_generation_ids"])
        epoch = inputs.get("source_epoch")
        if epoch is not None:
            shadowed.add(validate_source_epoch(epoch)["predecessor_generation_id"])
        checkpoints[generation_id] = checkpoint

    current_contract, current_inventory = build_target_contract(root, evaluated["source_sha"])
    candidates: list[dict[str, Any]] = []
    candidate_contracts: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    candidate_subjects: dict[str, dict[str, Any]] = {}
    for generation_id, checkpoint in checkpoints.items():
        if generation_id in shadowed:
            continue
        status = checkpoint.get("status")
        if status not in TERMINAL_STATUSES and (not include_active or status not in ACTIVE_STATUSES):
            continue
        if checkpoint.get("source_id") != source_id or checkpoint.get("source_scope") != source_scope:
            continue
        observation = checkpoint.get("last_observation")
        refs = checkpoint.get("input_artifacts")
        if not isinstance(observation, Mapping) or not isinstance(refs, list):
            raise SourceEligibilityError("source_selection_observation_identity_missing")
        matches = [
            row for row in admitted
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
            raise SourceEligibilityError("source_selection_admission_ambiguous_or_missing")
        admission = matches[0]
        original = derivation.original_observation_from_checkpoint(checkpoint, admission)
        inputs = checkpoint["generation_inputs"]
        prior_contract_value = inputs.get("source_target_contract")
        prior_contract = (
            validate_target_contract(prior_contract_value)
            if prior_contract_value is not None else None
        )
        target_contract, target_inventory = target_contract_from_predecessor(
            root, evaluated["source_sha"], checkpoint, original,
        )
        exact_refs = [
            ref for ref in refs if isinstance(ref, Mapping)
            and str(ref.get("run_id")) == str(admission["producer_run_id"])
            and str(ref.get("artifact_id")) == str(admission["artifact_id"])
            and ref.get("evidence_sha256") == admission["refresh_evidence_sha256"]
        ]
        if len(exact_refs) != 1:
            raise SourceEligibilityError("source_selection_artifact_reference_ambiguous")
        candidate_contracts[generation_id] = (target_contract, target_inventory)
        candidate_subjects[generation_id] = source_work_subject(
            admission, original, checkpoint, prior_contract, target_contract,
        )
        candidates.append({
            "source_id": source_id,
            "source_scope": source_scope,
            "generation_id": generation_id,
            "status": status,
            "artifact_expires_at": exact_refs[0].get("expires_at"),
            "accounting_exhausted": (
                int(checkpoint.get("attempts_consumed", 0)) >= processor.DEFAULT_MAX_ATTEMPTS
            ),
            "canonical_completed": False,
            "lease": checkpoint.get("lease"),
            "target_contract": prior_contract,
            "original_observation": original,
            "producer_run_id": str(admission["producer_run_id"]),
            "artifact_id": str(admission["artifact_id"]),
            "artifact_name": admission["artifact_name"],
        })

    # Each retained generation may compare the current source against a
    # different predecessor contract. Evaluate in ordinary order and keep the
    # first selected unit; otherwise retain the oldest closed outcome.
    ordered = sorted(candidates, key=_candidate_sort_key)
    outcomes: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    for candidate in ordered:
        contract, inventory = candidate_contracts[candidate["generation_id"]]
        outcomes.append((candidate, select_source_locator(
            target_contract=contract, evaluation=evaluated, candidates=[candidate],
        ), inventory))
    if outcomes:
        candidate, locator, target_inventory = next(
            (
                row for row in outcomes
                if row[1]["action"] in SELECTED_ACTIONS or row[1]["action"] == "deferred"
            ),
            outcomes[0],
        )
        target_contract = candidate_contracts[candidate["generation_id"]][0]
    else:
        locator = select_source_locator(
            target_contract=current_contract, evaluation=evaluated, candidates=[],
        )
        candidate = None
        target_contract = current_contract
        target_inventory = current_inventory
    request = {
        "schema_version": SOURCE_WORK_REQUEST_SCHEMA_VERSION,
        "repository": repository,
        "source_id": source_id,
        "source_scope": source_scope,
        "evaluation": evaluated,
        "target_contract": target_contract,
        "target_inventory": target_inventory,
        "source_subject": (
            candidate_subjects[candidate["generation_id"]]
            if candidate is not None else None
        ),
        "action": locator["action"],
        "reason_code": locator["reason_code"],
        "selected_generation_id": locator["generation_id"],
        "producer_run_id": candidate.get("producer_run_id") if candidate is not None else None,
        "artifact_id": candidate.get("artifact_id") if candidate is not None else None,
        "artifact_name": candidate.get("artifact_name") if candidate is not None else None,
        "selection_state_sha": selection_state_sha,
    }
    return validate_source_work_request(request, root=root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--state-dir", type=pathlib.Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-id", default="data_go_kr")
    parser.add_argument("--source-scope", default="aggregate_supported_catalog")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--event", choices=("push", "schedule"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--include-active", action="store_true")
    parser.add_argument("--selection-state-sha")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--github-output", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        source_sha = _sha1(args.source_sha, "source_evaluation_sha")
        source_tree = _git(root, "rev-parse", f"{source_sha}^{{tree}}")
        assert isinstance(source_tree, str)
        selection_state_sha = args.selection_state_sha or None
        if selection_state_sha is not None:
            _sha1(selection_state_sha, "source_selection_state")
            state_worktree = args.state_dir.resolve().parent.parent
            actual_state_sha = _git(state_worktree, "rev-parse", "HEAD")
            if actual_state_sha != selection_state_sha:
                raise SourceEligibilityError("source_selection_state_revision_mismatch")
            if _git(state_worktree, "status", "--porcelain=v1", "--untracked-files=all"):
                raise SourceEligibilityError("source_selection_state_worktree_dirty")
        request = source_work_request_from_state(
            root=root,
            state_dir=args.state_dir,
            repository=args.repository,
            source_id=args.source_id,
            source_scope=args.source_scope,
            evaluation={
                "source_sha": source_sha,
                "source_tree_sha": source_tree,
                "event": args.event,
                "run_id": args.run_id,
                "run_attempt": args.run_attempt,
                "as_of": args.as_of,
            },
            include_active=args.include_active,
            selection_state_sha=selection_state_sha,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        if args.github_output is not None:
            with args.github_output.open("a", encoding="utf-8") as handle:
                for key in (
                    "action", "reason_code", "selected_generation_id",
                    "producer_run_id", "artifact_id", "artifact_name",
                ):
                    handle.write(f"{key}={request.get(key) or ''}\n")
                handle.write(f"request_path={args.output}\n")
        print(json.dumps({
            "action": request["action"],
            "reason_code": request["reason_code"],
            "selected_generation_id": request["selected_generation_id"],
        }, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"action": "blocked", "reason_code": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
