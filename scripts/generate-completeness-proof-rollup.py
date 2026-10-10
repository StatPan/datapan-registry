#!/usr/bin/env python3
"""Build a deterministic, fail-closed rollup for registered public-data scopes."""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCOPE_REGISTRY_PATH = "policy/completeness-proof-scopes.json"
INPUT_INDEX_PATH = "reports/completeness-proof-inputs.json"
POLICY_PATH = "policy/completeness-proof.json"
ROLLUP_SCHEMA_PATH = "schemas/datapan.completeness-proof-rollup.v1.schema.json"
SCOPE_SCHEMA_PATH = "schemas/datapan.completeness-proof-scopes.v1.schema.json"
INPUT_SCHEMA_PATH = "schemas/datapan.completeness-proof-inputs.v1.schema.json"
OUTPUT_JSON_PATH = "reports/completeness-proof-rollup.json"
OUTPUT_MARKDOWN_PATH = "reports/completeness-proof-rollup.md"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
RESOURCE_KINDS = {
    "api_catalog_metadata",
    "api_operation_manifest",
    "file_dataset",
    "curated_payload_snapshot",
    "payload_rows",
}
TICKET_RE = re.compile(r"^(#[1-9][0-9]*|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*)$")
PIPELINE_WORKFLOWS = {
    "source": {
        "workflow_name": "Upstream catalog refresh",
        "workflow_id": 311036678,
        "workflow_path": ".github/workflows/upstream-catalog-refresh.yml",
        "events": {"schedule"},
        "job_names": {"observe"},
        "required_job": "observe",
    },
    "processor": {
        "workflow_name": "Process upstream catalogue",
        "workflow_id": 373610259,
        "workflow_path": ".github/workflows/upstream-catalogue-process.yml",
        "events": {"schedule", "workflow_run", "workflow_dispatch"},
        "job_names": {"Process"},
        "required_job": "Process",
    },
    "promotion": {
        "workflow_name": "Canonical update promotion",
        "workflow_id": 373708872,
        "workflow_path": ".github/workflows/canonical-update-promotion.yml",
        "events": {"workflow_run", "workflow_dispatch", "schedule"},
        "job_names": {"reconcile"},
        "required_job": "reconcile",
    },
    "health": {
        "workflow_name": "Upstream catalogue health",
        "workflow_id": 373633151,
        "workflow_path": ".github/workflows/upstream-catalogue-health.yml",
        "events": {"workflow_run", "schedule", "workflow_dispatch"},
        "job_names": {"inspect"},
        "required_job": "inspect",
    },
    "publisher": {
        "workflow_name": "Publish Hugging Face Registry distribution",
        "workflow_id": 311133646,
        "workflow_path": ".github/workflows/huggingface-distribution.yml",
        "events": {"workflow_dispatch"},
        "job_names": {"validate"},
        "required_job": "validate",
    },
    "acknowledgement": {
        "workflow_name": "Canonical update publication acknowledgement",
        "workflow_id": 373708873,
        "workflow_path": ".github/workflows/canonical-update-publication-ack.yml",
        "events": {"workflow_run", "schedule"},
        "job_names": {"reconcile"},
        "required_job": "reconcile",
    },
}
ACKNOWLEDGEMENT_STATE_AFTER_ROLES = frozenset({
    "acknowledgement_state_ref", "acknowledgement_state_commit",
    "acknowledgement_state_tree", "acknowledgement_journal_blob_api",
})
ACKNOWLEDGEMENT_STATE_BEFORE_ROLE_MAP = {
    "acknowledgement_state_ref_before": "acknowledgement_state_ref",
    "acknowledgement_state_commit_before": "acknowledgement_state_commit",
    "acknowledgement_state_tree_before": "acknowledgement_state_tree",
    "acknowledgement_journal_blob_api_before": "acknowledgement_journal_blob_api",
}
ACKNOWLEDGEMENT_STATE_BEFORE_ROLES = frozenset(ACKNOWLEDGEMENT_STATE_BEFORE_ROLE_MAP)
NATIVE_DISTRIBUTION_CONTRACTS = {
    # This exact source/workflow pair is the reviewed native verifier that
    # covers the complete immutable distribution pointer, including the five
    # workflow-staged extras. Unknown pairs must be reviewed before admission.
    (
        "856b681fe58aa15c78e5d0de014f3d8ebe8ca3ac7fa76830f7bce267e57e4a02",
        "3c7d553c4f8925edac5640b31b9e13cb01400774046e1b3b6962615edf0abdc2",
    ): frozenset({
        "release/registry-shards.json",
        "release/data-go-kr-shards.tar.gz",
        "reports/latest-release-verification.json",
        "reports/latest-release-readiness.json",
        "reports/data-go-kr/error-action-catalog.json",
    }),
}
# Exact #605 source contracts whose deterministic Registry-to-operation
# projection may be used as a semantic subject adapter. Unknown revisions are
# not executed as evidence; add a reviewed contract when these bytes change.
DATA_GO_KR_OPERATION_PROJECTION_CONTRACT = {
    "scripts/generate-data-go-kr-operation-manifest.py": "cda98653b6cd0ec77602be196c11748a23399383682d8dc9ea8b8a0c5403902e",
    "scripts/validate-data-go-kr-operation-manifest.py": "c04e943adb38929405694e2fe109ce29dcb5c1a8f785820aa7630f16c62632e6",
    "schemas/datapan.data-go-kr-operation-manifest.v1.schema.json": "df5b44a97c63db6e8995dd04ea388fd6359afa39d73e9c8d5588952263b0e736",
    "schemas/datapan.data-go-kr-operation-denominator-expectation.v1.schema.json": "0b106d317d1d642dece760342bc8e95e998929ef6fa7d8e4518e59487425d8ad",
}
PIPELINE_INPUT_ROLES = {
    "pipeline_run": "pipeline_run",
    "pipeline_jobs": "pipeline_jobs",
    "pipeline_artifact": "pipeline_artifact",
    "pipeline_artifact_metadata": "pipeline_artifact",
    "pipeline_artifact_archive": "pipeline_artifact",
    "pipeline_log_archive": "pipeline_log",
    "processor_state_root_api": "pipeline_contents_snapshot",
    "processor_index_api": "pipeline_contents_snapshot",
    "processor_generation_api": "pipeline_contents_snapshot",
    "processor_state_ref": "pipeline_state_reference",
    "processor_state_commit": "pipeline_git_commit",
    "processor_state_tree": "pipeline_git_tree",
    "promotion_state_ref": "pipeline_state_reference",
    "promotion_state_commit": "pipeline_git_commit",
    "promotion_state_tree": "pipeline_git_tree",
    "promotion_journal_blob_api": "pipeline_git_blob",
    "health_state_ref": "pipeline_state_reference",
    "health_state_commit": "pipeline_git_commit",
    "health_state_tree": "pipeline_git_tree",
    "health_receipt_blob_api": "pipeline_git_blob",
    "health_state_blob_api": "pipeline_git_blob",
    "publication_producer_run": "pipeline_run",
    "publication_producer_jobs": "pipeline_jobs",
    "publication_output_artifact": "pipeline_artifact",
    "publication_artifact_archive": "pipeline_artifact",
    "consumer_readback": "consumer_readback",
    "acknowledgement_run": "pipeline_run",
    "acknowledgement_jobs": "pipeline_jobs",
    "acknowledgement_log_archive": "pipeline_log",
    "acknowledgement_state_ref": "pipeline_state_reference",
    "acknowledgement_state_commit": "pipeline_git_commit",
    "acknowledgement_state_tree": "pipeline_git_tree",
    "acknowledgement_journal_blob_api": "pipeline_git_blob",
    "acknowledgement_state_ref_before": "pipeline_state_reference",
    "acknowledgement_state_commit_before": "pipeline_git_commit",
    "acknowledgement_state_tree_before": "pipeline_git_tree",
    "acknowledgement_journal_blob_api_before": "pipeline_git_blob",
}
PIPELINE_STAGE_ROLES = {
    "source": {
        "pipeline_run", "pipeline_jobs", "pipeline_artifact_metadata", "pipeline_artifact_archive",
    },
    "processor": {
        "pipeline_run", "pipeline_jobs", "pipeline_artifact_metadata", "pipeline_artifact_archive",
        "processor_state_root_api", "processor_index_api", "processor_generation_api", "processor_state_ref",
        "processor_state_commit", "processor_state_tree",
    },
    "promotion": {
        "pipeline_run", "pipeline_jobs", "pipeline_log_archive", "promotion_state_ref",
        "promotion_state_commit", "promotion_state_tree", "promotion_journal_blob_api",
    },
    "health": {
        "pipeline_run", "pipeline_jobs", "pipeline_artifact_metadata", "pipeline_artifact_archive",
        "health_state_ref", "health_state_commit", "health_state_tree", "health_receipt_blob_api", "health_state_blob_api",
    },
    "publisher": {
        "publication_producer_run", "publication_producer_jobs", "publication_output_artifact", "publication_artifact_archive",
    },
    "acknowledgement": {
        "acknowledgement_run", "acknowledgement_jobs", "acknowledgement_log_archive",
    },
}
PIPELINE_STAGE_OPTIONAL_ROLES = {
    # Older retained packets predate an explicit producer-side stream summary
    # and ACK state tree. Current updated claims require these exact roles.
    "publisher": {"consumer_readback"},
    "acknowledgement": {
        "acknowledgement_state_ref", "acknowledgement_state_commit",
        "acknowledgement_state_tree", "acknowledgement_journal_blob_api",
        *ACKNOWLEDGEMENT_STATE_BEFORE_ROLES,
    },
}


def read_bytes(path: pathlib.Path) -> bytes:
    return path.read_bytes()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load JSON {path.name}: {exc}") from exc


def object_at(path: pathlib.Path, label: str) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def validate_schema(value: Any, schema_path: pathlib.Path, label: str) -> None:
    schema = object_at(schema_path, f"{label} schema")
    validate_schema_value(value, schema, label)


def validate_schema_value(value: Any, schema: Any, label: str) -> None:
    errors = sorted(
        jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        ).iter_errors(value),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        detail = "; ".join(
            f"{'/'.join(str(item) for item in error.absolute_path) or '$'}: {error.message}"
            for error in errors
        )
        raise ValueError(f"{label} schema: {detail}")


def validate_schema_at_revision(
    *, root: pathlib.Path, revision: str, relative_path: str, value: Any, label: str,
) -> None:
    try:
        schema = json.loads(git_read_only(root, ["show", f"{revision}:{relative_path}"]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} schema is unavailable at its authenticated producer revision") from exc
    validate_schema_value(value, schema, label)


def parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def normalize_relative_path(value: str, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a normalized POSIX-relative path")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or not path.parts or value != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a normalized POSIX-relative path")
    return path


def resolve_under(root: pathlib.Path, relative: str, label: str) -> pathlib.Path:
    path = normalize_relative_path(relative, label)
    resolved_root = root.resolve()
    resolved = (resolved_root / pathlib.Path(*path.parts)).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes its declared input root") from exc
    return resolved


def artifact(path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "bytes": len(data), "sha256": sha256_bytes(data)}


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def input_index_for_candidate(
    *,
    root: pathlib.Path,
    baseline_main_sha: str,
    candidate_registry: pathlib.Path,
    candidate_operation_manifest: pathlib.Path,
    baseline_registry_file: pathlib.Path | None = None,
    ) -> dict[str, Any]:
    """Rebuild a candidate index from its immutable main baseline.

    Only the registered data.go.kr catalog and operation-manifest inventory rows
    are rebound. All producer, import, proof, publication, and Health rows come
    byte-for-byte from the pinned baseline input index.
    """
    root = root.resolve()
    assert_main_ancestor(root, baseline_main_sha, "candidate baseline main revision")
    base_index_bytes = git_read_only(root, ["show", f"{baseline_main_sha}:{INPUT_INDEX_PATH}"])
    base_index = json.loads(base_index_bytes)
    if not isinstance(base_index, dict):
        raise ValueError("pinned baseline completeness input index is not an object")
    base_index = copy.deepcopy(base_index)
    base_index.pop("evaluation_context", None)
    scope_registry_bytes = git_read_only(root, ["show", f"{baseline_main_sha}:{SCOPE_REGISTRY_PATH}"])
    policy_bytes = git_read_only(root, ["show", f"{baseline_main_sha}:{POLICY_PATH}"])
    if read_bytes(root / SCOPE_REGISTRY_PATH) != scope_registry_bytes or read_bytes(root / POLICY_PATH) != policy_bytes:
        raise ValueError("candidate refresh cannot change completeness scope registration or policy")
    baseline_manifest_bytes = git_read_only(root, ["show", f"{baseline_main_sha}:manifest.json"])
    baseline_manifest = json.loads(baseline_manifest_bytes)
    current_scope_registry, _policy, scope_by_id = load_and_validate_registry(root)
    baseline_scope_registry = json.loads(scope_registry_bytes)
    if current_scope_registry != baseline_scope_registry:
        raise ValueError("candidate scope registration differs from its pinned baseline")

    candidate_paths = {
        "source_catalog_snapshot": candidate_registry,
        "local_operation_manifest": candidate_operation_manifest,
    }
    baseline_items: dict[str, dict[str, Any]] = {}
    candidate_items: dict[str, dict[str, Any]] = {}
    for role, candidate_path in candidate_paths.items():
        matching_scopes = [
            scope for scope in scope_by_id.values()
            if scope.get("inventory", {}).get("input_role") == role
            and scope.get("source_id") == "data_go_kr"
        ]
        if len(matching_scopes) != 1:
            raise ValueError(f"candidate adapter requires exactly one registered data.go.kr {role} scope")
        scope = matching_scopes[0]
        registered_path = scope["inventory"].get("path")
        try:
            candidate_relative = candidate_path.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"candidate {role} must be inside the repository root") from exc
        if candidate_relative != registered_path:
            raise ValueError(f"candidate {role} path differs from the registered inventory selector")
        rows = [
            item for item in base_index.get("inputs", [])
            if item.get("scope_id") == scope["scope_id"] and item.get("role") == role
        ]
        if len(rows) != 1 or rows[0].get("artifact_type") != "local_inventory":
            raise ValueError(f"pinned baseline input index lacks one local {role} row")
        baseline_items[role] = copy.deepcopy(rows[0])
        if role == "local_operation_manifest":
            baseline_commit_bytes = git_read_only(root, ["show", f"{baseline_main_sha}:{registered_path}"])
            if (len(baseline_commit_bytes), sha256_bytes(baseline_commit_bytes)) != (rows[0]["bytes"], rows[0]["sha256"]):
                raise ValueError(f"pinned baseline {role} row does not match its Git tree bytes")
        candidate_bytes = read_bytes(candidate_path)
        if role == "source_catalog_snapshot":
            candidate_value = json.loads(candidate_bytes)
            if not isinstance(candidate_value, list) or any(not isinstance(row, dict) for row in candidate_value):
                raise ValueError("candidate Registry must be a JSON array of objects")
            lfs_pointer = candidate_bytes.startswith(b"version https://git-lfs.github.com/spec/v1")
            if lfs_pointer:
                raise ValueError("candidate Registry is an unmaterialized Git LFS pointer")
        else:
            validate_candidate_operation_manifest(
                root, candidate_path, candidate_paths["source_catalog_snapshot"],
            )
        replacement = copy.deepcopy(rows[0])
        replacement.update({"root": "repository", "bytes": len(candidate_bytes), "sha256": sha256_bytes(candidate_bytes)})
        replacement["namespace"] = "repository_snapshot"
        replacement["producer"] = {"identity": "candidate-local-inventory", "repository": "StatPan/datapan-registry"}
        replacement["observed_at"] = None
        replacement.pop("subject", None)
        rows[0].clear()
        rows[0].update(replacement)
        candidate_items[role] = replacement

    baseline_catalog = baseline_items["source_catalog_snapshot"]
    pointer_artifact, _baseline_release_manifest_sha = source_lfs_binding(root, baseline_main_sha)
    if (pointer_artifact.get("sha256"), pointer_artifact.get("bytes")) != (baseline_catalog["sha256"], baseline_catalog["bytes"]):
        raise ValueError("pinned baseline catalog input differs from the main manifest LFS payload identity")
    if baseline_registry_file is not None:
        baseline_registry_bytes = read_bytes(baseline_registry_file)
        if (len(baseline_registry_bytes), sha256_bytes(baseline_registry_bytes)) != (baseline_catalog["bytes"], baseline_catalog["sha256"]):
            raise ValueError("supplied candidate baseline Registry differs from its pinned main payload")
    operation_item = baseline_items["local_operation_manifest"]
    operation_path = normalize_relative_path(operation_item["path"], "baseline operation manifest path").as_posix()
    baseline_operation_bytes = git_read_only(root, ["show", f"{baseline_main_sha}:{operation_path}"])
    if (len(baseline_operation_bytes), sha256_bytes(baseline_operation_bytes)) != (operation_item["bytes"], operation_item["sha256"]):
        raise ValueError("pinned baseline operation manifest differs from its main Git tree bytes")
    candidate_registry_bytes = read_bytes(candidate_registry)
    candidate_operation_bytes = read_bytes(candidate_operation_manifest)
    base_index_metadata = artifact(f"git:{baseline_main_sha}:{INPUT_INDEX_PATH}", base_index_bytes)
    context = {
        "mode": "candidate",
        "baseline_main_sha": baseline_main_sha,
        "baseline_input_index": base_index_metadata,
        "baseline_registry": {
            "path": f"git-lfs-payload:{baseline_main_sha}:{baseline_catalog['path']}",
            "bytes": baseline_catalog["bytes"], "sha256": baseline_catalog["sha256"],
        },
        "baseline_operation_manifest": artifact(f"git:{baseline_main_sha}:{operation_path}", baseline_operation_bytes),
        "baseline_release_manifest": artifact(f"git:{baseline_main_sha}:manifest.json", baseline_manifest_bytes),
        "baseline_policy": artifact(f"git:{baseline_main_sha}:{POLICY_PATH}", policy_bytes),
        "baseline_scope_registry": artifact(f"git:{baseline_main_sha}:{SCOPE_REGISTRY_PATH}", scope_registry_bytes),
        "candidate_registry": artifact(candidate_paths["source_catalog_snapshot"].resolve().relative_to(root).as_posix(), candidate_registry_bytes),
        "candidate_operation_manifest": artifact(candidate_paths["local_operation_manifest"].resolve().relative_to(root).as_posix(), candidate_operation_bytes),
    }
    base_index["evaluation_context"] = context
    base_index["inputs"] = sorted(base_index["inputs"], key=lambda item: item["input_id"])
    validate_schema(base_index, root / INPUT_SCHEMA_PATH, "candidate completeness input index")
    return base_index


def validate_candidate_input_index(
    *, root: pathlib.Path, index: dict[str, Any], input_index_path: pathlib.Path,
) -> tuple[dict[str, Any], set[str]]:
    context = index.get("evaluation_context")
    if not isinstance(context, dict) or context.get("mode") != "candidate":
        return {}, set()
    baseline_main_sha = context.get("baseline_main_sha")
    if not isinstance(baseline_main_sha, str):
        raise ValueError("candidate input index lacks its pinned baseline main revision")
    base_bytes = git_read_only(root, ["show", f"{baseline_main_sha}:{INPUT_INDEX_PATH}"])
    if artifact(f"git:{baseline_main_sha}:{INPUT_INDEX_PATH}", base_bytes) != context.get("baseline_input_index"):
        raise ValueError("candidate index baseline input-index bytes changed")
    baseline_index = json.loads(base_bytes)
    baseline_index.pop("evaluation_context", None)
    candidate_registry_path = context["candidate_registry"]["path"]
    candidate_operation_path = context["candidate_operation_manifest"]["path"]
    expected = input_index_for_candidate(
        root=root, baseline_main_sha=baseline_main_sha,
        candidate_registry=root / candidate_registry_path,
        candidate_operation_manifest=root / candidate_operation_path,
    )
    normalize = lambda value: json.dumps(
        {**value, "inputs": sorted(value["inputs"], key=lambda item: item["input_id"])},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    if normalize(index) != normalize(expected):
        raise ValueError("candidate input index changed evidence beyond registered local inventory rows")
    baseline_by_id = {item["input_id"]: item for item in baseline_index.get("inputs", [])}
    changed_scopes = {
        item["scope_id"]
        for item in index.get("inputs", [])
        if item.get("artifact_type") == "local_inventory"
        and item["input_id"] in baseline_by_id
        and (item.get("sha256"), item.get("bytes")) != (baseline_by_id[item["input_id"]].get("sha256"), baseline_by_id[item["input_id"]].get("bytes"))
    }
    changed_catalog_scope_ids = {
        item["scope_id"]
        for item in index.get("inputs", [])
        if item.get("role") == "source_catalog_snapshot" and item["scope_id"] in changed_scopes
    }
    changed_source_ids: set[str] = set()
    if changed_catalog_scope_ids:
        scope_registry = object_at(root / SCOPE_REGISTRY_PATH, "scope registry")
        source_by_scope_id = {
            scope.get("scope_id"): scope.get("source_id")
            for scope in scope_registry.get("scopes", [])
            if isinstance(scope, dict)
        }
        changed_source_ids = {
            source_by_scope_id[scope_id]
            for scope_id in changed_catalog_scope_ids
            if isinstance(source_by_scope_id.get(scope_id), str)
        }
    if changed_source_ids:
        scope_registry = object_at(root / SCOPE_REGISTRY_PATH, "scope registry")
        changed_scopes.update(
            scope["scope_id"] for scope in scope_registry["scopes"]
            if scope.get("source_id") in changed_source_ids
        )
    return context, changed_scopes


def import_module(name: str, path: pathlib.Path) -> Any:
    module_name = f"{name}_{hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load local validator {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def validate_registered_sources(root: pathlib.Path, scopes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    source_registry = object_at(root / "policy/sustainable-coverage.json", "source registry")
    configured = source_registry.get("supported_sources")
    if not isinstance(configured, list):
        raise ValueError("source registry has no supported_sources list")
    source_by_id: dict[str, dict[str, Any]] = {}
    for item in configured:
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or source_id in source_by_id:
            raise ValueError("source registry source IDs must be nonempty and unique")
        profile_path = normalize_relative_path(item.get("profile"), "source registry profile")
        profile = object_at(root / pathlib.Path(*profile_path.parts), f"source profile {source_id}")
        if profile.get("source_id") != source_id:
            raise ValueError(f"source profile ID does not match source registry: {source_id}")
        source_by_id[source_id] = {**item, "profile_value": profile}

    actual_profiles = {path.relative_to(root).as_posix() for path in (root / "sources").glob("*.json")}
    registered_profiles = {item["profile"] for item in source_by_id.values()}
    if actual_profiles != registered_profiles:
        raise ValueError(
            "configured source profiles and sustainable coverage registry differ: "
            f"unregistered={sorted(actual_profiles - registered_profiles)}, "
            f"missing={sorted(registered_profiles - actual_profiles)}"
        )

    scope_ids: set[str] = set()
    operation_scope_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    source_scope_counts: dict[tuple[str, str], int] = {}
    has_data_catalog = False
    has_file_scope = False
    has_snapshot_scope = False
    has_rows_scope = False
    for scope in scopes:
        scope_id = scope["scope_id"]
        if scope_id in scope_ids:
            raise ValueError(f"duplicate registered scope ID: {scope_id}")
        scope_ids.add(scope_id)
        source_id = scope["source_id"]
        if scope.get("source_profile") is not None:
            configured_source = source_by_id.get(source_id)
            if configured_source is None:
                raise ValueError(f"scope {scope_id} names an unregistered source profile {source_id}")
            if scope["source_profile"] != configured_source["profile"]:
                raise ValueError(f"scope {scope_id} profile path does not match the source registry")
            profile = configured_source["profile_value"]
            if scope["provider"] != profile.get("provider"):
                raise ValueError(f"scope {scope_id} provider does not match its source profile")
            if scope["identity_algorithm"] == "source-profile-identity-fields-v1":
                fields = profile.get("catalogue", {}).get("identity_fields")
                if not isinstance(fields, list) or scope["identity_fields"] != fields:
                    raise ValueError(f"scope {scope_id} identity fields do not match its source profile")
        elif source_id not in {"curated_payload"}:
            raise ValueError(f"scope {scope_id} has no profile for a configured source")

        kind = scope["resource_kind"]
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        source_scope_counts[(source_id, kind)] = source_scope_counts.get((source_id, kind), 0) + 1
        if kind not in RESOURCE_KINDS:
            raise ValueError(f"scope {scope_id} uses an unknown resource kind")
        if kind == "api_operation_manifest" and scope.get("source_profile") is not None:
            operation_scope_counts[source_id] = operation_scope_counts.get(source_id, 0) + 1
            if scope["inventory"]["kind"] == "operation_manifest":
                if scope["source_id"] != "data_go_kr" or scope["inventory"]["path"] != "reports/data-go-kr/operation-manifest.json":
                    raise ValueError(f"scope {scope_id} has an unsupported operation-manifest binding")
                if scope["identity_algorithm"] != "sha256-length-prefixed-utf8-v1":
                    raise ValueError(f"scope {scope_id} identity algorithm does not match #605")
                expected_fields = ["provider", "dataset_id", "protocol", "source_system", "upstream_operation_key", "endpoint", "method_or_action", "operation_name"]
                if scope["identity_fields"] != expected_fields or scope["selector"] != {"kind": "manifest", "value": "reports/data-go-kr/operation-manifest.json"}:
                    raise ValueError(f"scope {scope_id} selector or identity fields do not match #605")
            elif scope["inventory"]["kind"] == "operation_denominator":
                expected = source_by_id[source_id].get("coverage_report")
                if scope["inventory"]["path"] != expected:
                    raise ValueError(f"scope {scope_id} denominator path does not match its source registry")
                if scope["identity_algorithm"] != "operation-id-exact-string-set-v1":
                    raise ValueError(f"scope {scope_id} does not use the exact declared operation-ID algorithm")
                if scope["identity_fields"] != ["operation_id"] or scope["selector"] != {"kind": "manifest", "value": expected}:
                    raise ValueError(f"scope {scope_id} selector or identity fields do not match its local operation inventory")
            else:
                raise ValueError(f"scope {scope_id} has no operation inventory adapter")
        elif kind == "api_operation_manifest":
            raise ValueError(f"scope {scope_id} cannot register an API-operation scope without a configured source profile")
        if source_id == "data_go_kr" and kind == "api_catalog_metadata":
            has_data_catalog = True
            expected_inventory = {"kind": "registry_snapshot", "path": "data/data-go-kr.registry.json", "input_role": "source_catalog_snapshot"}
            if (
                scope["inventory"] != expected_inventory
                or scope["selector"] != {"kind": "catalog", "value": "data/data-go-kr.registry.json"}
                or scope["identity_algorithm"] != "source-profile-identity-fields-v1"
            ):
                raise ValueError("data.go.kr catalog scope must name the local registry snapshot explicitly")
        if source_id == "data_go_kr" and kind == "file_dataset":
            has_file_scope = True
            if (
                scope["inventory"] != {"kind": "none", "path": None, "input_role": None}
                or scope["identity_fields"]
                or scope["selector"] != {"kind": "catalog", "value": "authoritative data.go.kr file-list snapshot (not admitted)"}
                or scope["identity_algorithm"] != "unknown-until-file-dataset-snapshot-admitted"
                or scope["authority_state"] != "unavailable"
            ):
                raise ValueError("data.go.kr file scope must remain blocked without a file-list inventory")
        if kind == "curated_payload_snapshot":
            has_snapshot_scope = True
            if (
                scope["source_id"] != "curated_payload"
                or scope["provider"] != "StatPan/datapan-data"
                or scope["source_profile"] is not None
                or scope["inventory"]["kind"] != "none"
                or scope["identity_fields"]
                or scope["selector"] != {"kind": "snapshot", "value": "declared curated payload portfolio (inventory absent)"}
                or scope["identity_algorithm"] != "unknown-until-snapshot-portfolio-is-registered"
                or scope["authority_state"] != "unavailable"
            ):
                raise ValueError("curated payload scope must remain an explicit inventory placeholder")
        if kind == "payload_rows":
            has_rows_scope = True
            if (
                scope["source_id"] != "curated_payload"
                or scope["provider"] != "StatPan/datapan-data"
                or scope["source_profile"] is not None
                or scope["inventory"]["kind"] != "none"
                or scope["identity_fields"]
                or scope["selector"] != {"kind": "query", "value": "authoritative payload row identity set (not admitted)"}
                or scope["identity_algorithm"] != "unknown-until-row-authority-is-admitted"
                or scope["authority_state"] != "unavailable"
            ):
                raise ValueError("payload row scope must remain an explicit authority placeholder")

        for blocker in scope["blockers"]:
            if not TICKET_RE.fullmatch(blocker["ticket"]):
                raise ValueError(f"scope {scope_id} has a malformed blocker ticket")
        inventory = scope["inventory"]
        if inventory["kind"] == "none":
            if inventory["path"] is not None or inventory["input_role"] is not None:
                raise ValueError(f"scope {scope_id} has an inconsistent empty inventory")
        else:
            normalize_relative_path(inventory["path"], f"scope {scope_id} inventory path")
            if not inventory["input_role"]:
                raise ValueError(f"scope {scope_id} inventory path lacks an input role")

    if set(operation_scope_counts) != set(source_by_id) or any(count != 1 for count in operation_scope_counts.values()):
        raise ValueError(
            "every configured source must have exactly one registered API-operation scope: "
            f"missing={sorted(set(source_by_id) - set(operation_scope_counts))}, "
            f"duplicate={sorted(source_id for source_id, count in operation_scope_counts.items() if count != 1)}"
        )
    if not (has_data_catalog and has_file_scope and has_snapshot_scope and has_rows_scope):
        raise ValueError("scope registry must keep data.go.kr catalog/file and payload snapshot/row scopes distinct")
    for kind, expected_count in {
        "api_catalog_metadata": 1,
        "file_dataset": 1,
        "curated_payload_snapshot": 1,
        "payload_rows": 1,
    }.items():
        if kind_counts.get(kind, 0) != expected_count:
            raise ValueError(f"scope registry must define exactly one {kind} scope")
    expected_source_kinds = {
        (source_id, "api_operation_manifest"): 1 for source_id in source_by_id
    }
    expected_source_kinds.update({
        ("data_go_kr", "api_catalog_metadata"): 1,
        ("data_go_kr", "file_dataset"): 1,
        ("curated_payload", "curated_payload_snapshot"): 1,
        ("curated_payload", "payload_rows"): 1,
    })
    if source_scope_counts != expected_source_kinds:
        raise ValueError(
            "scope registry source/resource-kind bindings differ from the registered inventory: "
            f"actual={sorted((source, kind, count) for (source, kind), count in source_scope_counts.items())}"
        )
    return source_by_id


def load_and_validate_registry(root: pathlib.Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    policy_path = root / POLICY_PATH
    policy_bytes = read_bytes(policy_path)
    policy = object_at(policy_path, "completeness policy")
    proof_validator = import_module("completeness_proof_contract", root / "scripts/validate-completeness-proof.py")
    kind_policy = proof_validator.validate_policy(policy)

    registry_path = root / SCOPE_REGISTRY_PATH
    scope_registry = object_at(registry_path, "scope registry")
    validate_schema(scope_registry, root / SCOPE_SCHEMA_PATH, "scope registry")
    registry_policy = scope_registry["completeness_policy"]
    if registry_policy != {"path": POLICY_PATH, "sha256": sha256_bytes(policy_bytes)}:
        raise ValueError("scope registry is not bound to the checked-in completeness policy bytes")

    scopes = scope_registry["scopes"]
    source_by_id = validate_registered_sources(root, scopes)
    scope_by_id = {item["scope_id"]: item for item in scopes}
    for kind, item in kind_policy.items():
        if item["denominator_type"] != proof_validator.EXPECTED_SCOPE_KINDS[kind]:
            raise ValueError(f"base policy denominator type drifted for {kind}")
    for scope in scopes:
        base = kind_policy[scope["resource_kind"]]
        if scope["identity_owner"] != base["identity_owner"] or scope["evidence_owner"] != base["evidence_owner"]:
            raise ValueError(f"scope {scope['scope_id']} owner binding differs from completeness policy")
        if scope["resource_kind"] in {"api_catalog_metadata", "api_operation_manifest", "file_dataset"}:
            if scope["source_profile"] is None:
                raise ValueError(f"scope {scope['scope_id']} requires a configured source profile")
    return scope_registry, policy, scope_by_id


def validate_input_index(
    *,
    root: pathlib.Path,
    input_root: pathlib.Path,
    index_path: pathlib.Path,
    scope_by_id: dict[str, dict[str, Any]],
    index_value: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, pathlib.Path]]:
    index = index_value if index_value is not None else object_at(index_path, "input index")
    validate_schema(index, root / INPUT_SCHEMA_PATH, "input index")
    parse_time(index["evaluation_epoch"], "input index evaluation_epoch")
    input_ids: set[str] = set()
    scope_roles: set[tuple[str, str, str | None]] = set()
    resolved: dict[str, pathlib.Path] = {}
    checked_inputs: list[dict[str, Any]] = []
    expected_artifact_types = {
        "source_profile": "source_profile",
        "source_catalog_snapshot": "local_inventory",
        "local_operation_manifest": "local_inventory",
        "local_operation_denominator": "local_inventory",
        "proof_v1": "proof_v1",
        "authoritative_source_snapshot": "source_snapshot",
        "authoritative_denominator": "authority_denominator",
        "historical_import_admission": "import_admission",
        "historical_import_receipt": "import_receipt",
        "historical_import_attestation": "import_attestation",
        "pipeline_run": "pipeline_run",
        "pipeline_jobs": "pipeline_jobs",
        "pipeline_artifact": "pipeline_artifact",
        "pipeline_artifact_metadata": "pipeline_artifact",
        "pipeline_artifact_archive": "pipeline_artifact",
        "pipeline_log_archive": "pipeline_log",
        "processor_state_root_api": "pipeline_contents_snapshot",
        "processor_index_api": "pipeline_contents_snapshot",
        "processor_generation_api": "pipeline_contents_snapshot",
        "processor_state_ref": "pipeline_state_reference",
        "processor_state_commit": "pipeline_git_commit",
        "processor_state_tree": "pipeline_git_tree",
        "promotion_state_ref": "pipeline_state_reference",
        "promotion_state_commit": "pipeline_git_commit",
        "promotion_state_tree": "pipeline_git_tree",
        "promotion_journal_blob_api": "pipeline_git_blob",
        "health_state_ref": "pipeline_state_reference",
        "health_state_commit": "pipeline_git_commit",
        "health_state_tree": "pipeline_git_tree",
        "health_receipt_blob_api": "pipeline_git_blob",
        "health_state_blob_api": "pipeline_git_blob",
        "publication_repo_metadata_before": "pipeline_contents_snapshot",
        "publication_repo_metadata_after": "pipeline_contents_snapshot",
        "publication_anonymous_payload": "consumer_readback",
        "processor_checkpoint": "checkpoint",
        "composition_receipt": "composition_receipt",
        "promotion_journal": "promotion_journal",
        "publication_receipt": "publication_receipt",
        "publication_source_binding": "publication_source_binding",
        "consumer_readback": "consumer_readback",
        "health_receipt": "health_receipt",
        "health_state": "health_state",
        "publication_producer_run": "pipeline_run",
        "publication_producer_jobs": "pipeline_jobs",
        "publication_output_artifact": "pipeline_artifact",
        "publication_artifact_archive": "pipeline_artifact",
        "publication_source_commit": "pipeline_git_commit",
        "publication_source_manifest": "pipeline_contents_snapshot",
        "publication_workflow": "pipeline_contents_snapshot",
        "publication_pointer_before": "pipeline_contents_snapshot",
        "publication_pointer_after": "pipeline_contents_snapshot",
        "publication_pointer_immutable": "pipeline_contents_snapshot",
        "publication_anonymous_manifest": "pipeline_contents_snapshot",
        "acknowledgement_run": "pipeline_run",
        "acknowledgement_jobs": "pipeline_jobs",
        "acknowledgement_log_archive": "pipeline_log",
        "acknowledgement_state_ref": "pipeline_state_reference",
        "acknowledgement_state_commit": "pipeline_git_commit",
        "acknowledgement_state_tree": "pipeline_git_tree",
        "acknowledgement_journal_blob_api": "pipeline_git_blob",
        "acknowledgement_state_ref_before": "pipeline_state_reference",
        "acknowledgement_state_commit_before": "pipeline_git_commit",
        "acknowledgement_state_tree_before": "pipeline_git_tree",
        "acknowledgement_journal_blob_api_before": "pipeline_git_blob",
        "acknowledgement_journal_before": "promotion_journal",
        "acknowledgement_journal_after": "promotion_journal",
        "acknowledgement_health_receipt": "health_receipt",
    }
    for item in index["inputs"]:
        input_id = item["input_id"]
        if input_id in input_ids:
            raise ValueError(f"duplicate evidence input ID: {input_id}")
        input_ids.add(input_id)
        scope_id = item["scope_id"]
        if scope_id not in scope_by_id:
            raise ValueError(f"evidence input {input_id} refers to unregistered scope {scope_id}")
        expected_type = expected_artifact_types.get(item["role"])
        if expected_type is None or item["artifact_type"] != expected_type:
            raise ValueError(f"evidence input {input_id} has an unregistered role/artifact-type binding")
        stage = item.get("subject", {}).get("stage")
        if item["role"] in PIPELINE_INPUT_ROLES:
            if stage not in PIPELINE_WORKFLOWS:
                raise ValueError(f"pipeline evidence input {input_id} lacks a registered subject.stage")
            scope = scope_by_id[scope_id]
            if stage != "publisher" and (
                scope["source_id"] != "data_go_kr"
                or scope["resource_kind"] != "api_operation_manifest"
            ):
                raise ValueError("source, processor, promotion, Health, and ACK workflow evidence is bound to the registered data.go.kr operation pipeline")
            expected_pipeline_type = PIPELINE_INPUT_ROLES[item["role"]]
            if item["artifact_type"] != expected_pipeline_type:
                raise ValueError(f"pipeline input {input_id} has the wrong artifact type for its role")
            allowed_roles = PIPELINE_STAGE_ROLES[stage] | PIPELINE_STAGE_OPTIONAL_ROLES.get(stage, set())
            if item["role"] not in allowed_roles:
                raise ValueError(f"pipeline role {item['role']} is not registered for stage {stage}")
        elif stage is not None:
            raise ValueError(f"non-pipeline evidence input {input_id} cannot declare a pipeline stage")
        role_key = (scope_id, item["role"], stage if item["role"] in PIPELINE_INPUT_ROLES else None)
        if role_key in scope_roles and item["role"] not in {"evidence_snapshot"}:
            raise ValueError(f"duplicate evidence role {item['role']} for scope {scope_id} at stage {stage}")
        scope_roles.add(role_key)
        path_root = root if item["root"] == "repository" else input_root
        path = resolve_under(path_root, item["path"], f"input {input_id} path")
        if not path.is_file():
            raise ValueError(f"evidence input {input_id} is missing: {item['root']}:{item['path']}")
        data = read_bytes(path)
        if len(data) != item["bytes"] or sha256_bytes(data) != item["sha256"]:
            raise ValueError(f"evidence input {input_id} byte count or SHA-256 differs")
        if item["observed_at"] is not None:
            parse_time(item["observed_at"], f"input {input_id} observed_at")
        if item.get("subject"):
            for key, digest in item["subject"].items():
                if key.endswith("sha256") and not SHA256_RE.fullmatch(digest):
                    raise ValueError(f"input {input_id} has malformed {key}")
        resolved[input_id] = path
        checked_inputs.append(item)

    for scope_id in sorted(scope_by_id):
        for stage, stage_roles in PIPELINE_STAGE_ROLES.items():
            roles = {
                item["role"] for item in checked_inputs
                if item["scope_id"] == scope_id and item.get("subject", {}).get("stage") == stage
            }
            allowed_roles = stage_roles | PIPELINE_STAGE_OPTIONAL_ROLES.get(stage, set())
            if roles and (not stage_roles.issubset(roles) or not roles.issubset(allowed_roles)):
                raise ValueError(
                    f"pipeline stage {stage} evidence roles differ from its registered contract for {scope_id}: "
                    f"missing={sorted(stage_roles - roles)}, extra={sorted(roles - stage_roles)}"
                )

    checked_inputs.sort(key=lambda item: (item["scope_id"], item["role"], item["input_id"]))
    for scope in scope_by_id.values():
        if scope["source_profile"] is not None:
            matches = [item for item in checked_inputs if item["scope_id"] == scope["scope_id"] and item["role"] == "source_profile"]
            if len(matches) != 1 or matches[0]["root"] != "repository" or matches[0]["path"] != scope["source_profile"]:
                raise ValueError(f"scope {scope['scope_id']} requires exactly one byte-bound source_profile input")
        inventory = scope["inventory"]
        if inventory["kind"] != "none":
            matches = [item for item in checked_inputs if item["scope_id"] == scope["scope_id"] and item["role"] == inventory["input_role"]]
            if len(matches) != 1 or matches[0]["root"] != "repository" or matches[0]["path"] != inventory["path"]:
                raise ValueError(f"scope {scope['scope_id']} requires exactly one byte-bound {inventory['input_role']} input")
    return index, checked_inputs, resolved


def input_path(item: dict[str, Any]) -> str:
    return item["path"] if item["root"] == "repository" else f"evidence/{item['path']}"


def validate_data_go_operation_manifest(root: pathlib.Path, manifest_path: pathlib.Path) -> dict[str, Any]:
    module = import_module("data_go_operation_manifest_validator", root / "scripts/validate-data-go-kr-operation-manifest.py")
    module.ROOT = root
    module.MANIFEST = root / "reports/data-go-kr/operation-manifest.json"
    module.SCHEMA = root / "schemas/datapan.data-go-kr-operation-manifest.v1.schema.json"
    module.REGISTRY = root / "data/data-go-kr.registry.json"
    module.RELEASE_MANIFEST = root / "manifest.json"
    module.GENERATOR_PATH = root / "scripts/generate-data-go-kr-operation-manifest.py"
    module.EXPECTATION = root / "policy/data-go-kr-operation-denominator-expectation.json"
    module.EXPECTATION_SCHEMA = root / "schemas/datapan.data-go-kr-operation-denominator-expectation.v1.schema.json"

    manifest = object_at(manifest_path, "data.go.kr operation manifest")
    schema = object_at(module.SCHEMA, "data.go.kr operation manifest schema")
    registry = load_json(module.REGISTRY)
    expectation = object_at(module.EXPECTATION, "data.go.kr operation denominator expectation")
    expectation_schema = object_at(module.EXPECTATION_SCHEMA, "data.go.kr denominator expectation schema")
    release_manifest = object_at(module.RELEASE_MANIFEST, "release manifest")
    module.validate(
        manifest,
        schema,
        registry,
        release_manifest,
        expectation,
        registry_path=module.REGISTRY,
        expectation_schema=expectation_schema,
    )
    return manifest


def validate_candidate_operation_manifest(
    root: pathlib.Path, manifest_path: pathlib.Path, registry_path: pathlib.Path,
) -> dict[str, Any]:
    """Validate candidate-local operation identities without treating them as release authority."""
    manifest = object_at(manifest_path, "candidate data.go.kr operation manifest")
    validate_schema(manifest, root / "schemas/datapan.data-go-kr-operation-manifest.v1.schema.json", "candidate data.go.kr operation manifest")
    generator = import_module(
        "candidate_data_go_operation_manifest_generator",
        root / "scripts/generate-data-go-kr-operation-manifest.py",
    )
    previous_registry = generator.REGISTRY
    generator.REGISTRY = registry_path
    try:
        registry = load_json(registry_path)
        expected = generator.build(registry)
    finally:
        generator.REGISTRY = previous_registry
    if manifest != expected:
        raise ValueError("candidate operation manifest does not deterministically match candidate Registry")
    return manifest


def validate_operation_denominator(root: pathlib.Path, path: pathlib.Path, source_id: str) -> dict[str, Any]:
    value = object_at(path, f"{source_id} operation denominator")
    validate_schema(value, root / "schemas/datapan.operation-denominator.v1.schema.json", f"{source_id} operation denominator")
    if value["source_id"] != source_id or value["provenance"]["source_profile"] != f"sources/{source_id}.json":
        raise ValueError(f"{source_id} operation denominator source binding differs")
    operations = value["operations"]
    identities = [item["operation_id"] for item in operations]
    if len(identities) != len(set(identities)) or value["summary"]["operations"] != len(identities):
        raise ValueError(f"{source_id} local operation identities do not reconcile")
    if value["scope"]["unknown_upstream_operations_covered"] is not False:
        raise ValueError(f"{source_id} local operation denominator overclaims upstream coverage")
    return value


def validate_import_receipt_arithmetic(
    *, admission: dict[str, Any], receipt: dict[str, Any], source_id: str
) -> None:
    """Reconcile the persisted result rows with the separately admitted counts."""
    run_id = admission.get("run_id")
    if receipt.get("schema_version") != "datapan.runtime-freshness-import-receipt.v1":
        raise ValueError("historical import receipt schema is unsupported")
    if receipt.get("run_id") != run_id or receipt.get("source_id") != source_id:
        raise ValueError("historical import receipt run or source identity differs")
    admission_inputs = admission.get("inputs", {})
    receipt_inputs = receipt.get("inputs", {})
    if any(
        receipt_inputs.get(key) != admission_inputs.get(key)
        for key in ("sanitized_report_sha256", "run_receipt_sha256")
    ):
        raise ValueError("historical import receipt input digests differ from the admitted run")

    arithmetic = admission.get("arithmetic", {})
    selected = arithmetic.get("selected", {})
    results = receipt.get("results")
    summary = receipt.get("summary")
    if not isinstance(results, list) or not isinstance(summary, dict):
        raise ValueError("historical import receipt lacks result rows or summary")
    status_counts = {status: 0 for status in ("verified", "failed", "skipped", "unknown")}
    disposition_counts = {
        disposition: 0
        for disposition in ("classified_failure", "unclassified_failure", "healthy_recovery", "no_active_failure")
    }
    for row in results:
        if not isinstance(row, dict):
            raise ValueError("historical import receipt contains a non-object result")
        status = row.get("status")
        disposition = row.get("disposition")
        if status not in status_counts or disposition not in disposition_counts:
            raise ValueError("historical import receipt contains an unsupported result status or disposition")
        status_counts[status] += 1
        disposition_counts[disposition] += 1
    if any(status_counts[key] != selected.get(key) for key in status_counts):
        raise ValueError("historical import receipt status counts differ from admitted selected arithmetic")
    expected_outcome = "imported" if selected.get("total", 0) else "no_change"
    if admission.get("outcome") != expected_outcome:
        raise ValueError("historical import admission outcome differs from selected arithmetic")
    expected_summary = {
        "reported_results": len(results),
        "classified_failures": disposition_counts["classified_failure"],
        "unclassified_failures": disposition_counts["unclassified_failure"],
        "healthy_recoveries": disposition_counts["healthy_recovery"],
        "observations_projected": disposition_counts["classified_failure"] + disposition_counts["healthy_recovery"],
    }
    if len(results) != selected.get("total") or summary != expected_summary:
        raise ValueError("historical import receipt rows or summary differ from admitted selected arithmetic")


def git_read_only(root: pathlib.Path, args: list[str], *, check: bool = True) -> bytes:
    if not args or args[0] not in {"show", "merge-base", "rev-parse"}:
        raise ValueError("completeness evidence may use only local read-only git show/merge-base/rev-parse")
    result = subprocess.run(
        ["git", *args], cwd=root, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if check and result.returncode != 0:
        raise ValueError(f"local git evidence check failed: git {' '.join(args)}")
    if not check and result.returncode != 0:
        return b""
    return result.stdout


def registered_scope_source_path(scope: dict[str, Any]) -> str | None:
    """Return the scope's declared immutable source path, if one is registered."""
    inventory = scope.get("inventory", {})
    if not isinstance(inventory, dict):
        raise ValueError(f"scope {scope.get('scope_id')} has a malformed source inventory")
    path = inventory.get("path")
    if inventory.get("kind") == "none" or not isinstance(path, str):
        return None
    normalized = normalize_relative_path(path, "registered scope inventory path").as_posix()
    selector = scope.get("selector", {})
    if not isinstance(selector, dict) or selector.get("value") != normalized:
        raise ValueError(f"scope {scope.get('scope_id')} inventory path and selector do not identify one subject")
    return normalized


def registered_historical_import_path(scope: dict[str, Any]) -> str | None:
    """Return the only checked-in historical-import source contract (#633)."""
    expected_path = "reports/data-go-kr/operation-manifest.json"
    expected_fields = [
        "provider", "dataset_id", "protocol", "source_system", "upstream_operation_key",
        "endpoint", "method_or_action", "operation_name",
    ]
    if (
        scope.get("source_id") != "data_go_kr"
        or scope.get("resource_kind") != "api_operation_manifest"
        or scope.get("selector") != {"kind": "manifest", "value": expected_path}
        or scope.get("identity_algorithm") != "sha256-length-prefixed-utf8-v1"
        or scope.get("identity_fields") != expected_fields
        or scope.get("inventory") != {
            "kind": "operation_manifest", "path": expected_path,
            "input_role": "local_operation_manifest",
        }
    ):
        return None
    return expected_path


def validate_scope_publication_subject(
    *, root: pathlib.Path, scope: dict[str, Any], source_input: dict[str, Any],
    source_snapshot: dict[str, Any], source_bytes: bytes,
    publication_artifact: dict[str, Any], release_member: dict[str, Any],
    release_manifest: dict[str, Any], publication_source_sha: str,
    pipeline_evidence: dict[str, Any] | None = None,
    retained_registry_payload_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Join a native-verified release member to its registered proof subject.

    Exact-byte representation is generic for any registered inventory. The
    additional #605 adapter recognizes its reviewed deterministic Registry to
    operation-manifest projection. Membership in the release alone is never a
    semantic join.
    """
    registered_path = registered_scope_source_path(scope)
    if registered_path is None:
        raise ValueError(f"scope {scope['scope_id']} has no registered source-artifact publication adapter")
    source_path = normalize_relative_path(source_input.get("path"), "authoritative scope source path").as_posix()
    publication_path = normalize_relative_path(
        publication_artifact.get("path"), "published scope subject path",
    ).as_posix()
    source_sha = source_snapshot.get("sha256")
    subject = source_input.get("subject", {})
    if (
        source_path != registered_path
        or source_input.get("scope_id") != scope["scope_id"]
        or subject.get("source_id") != scope["source_id"]
        or subject.get("resource_kind") != scope["resource_kind"]
        or subject.get("identity_algorithm") != scope["identity_algorithm"]
        or source_snapshot.get("sha256") != source_input.get("sha256")
        or len(source_bytes) != source_input.get("bytes")
        or sha256_bytes(source_bytes) != source_sha
    ):
        raise ValueError("authoritative proof source does not bind the exact registered scope subject")

    if publication_path == registered_path:
        if (
            release_member.get("path") != registered_path
            or publication_artifact.get("sha256") != source_sha
            or release_member.get("sha256") != source_sha
            or release_member.get("bytes") != source_input.get("bytes")
        ):
            raise ValueError("published member does not bind the exact registered scope source subject")
        validate_committed_release_artifact(
            root=root, revision=publication_source_sha, path=registered_path,
            expected_sha256=source_sha, expected_bytes=source_input["bytes"],
            label="published registered scope source", expected_content=source_bytes,
        )
        operation_projection = None
        if (
            scope.get("source_id") == "data_go_kr"
            and scope.get("resource_kind") == "api_operation_manifest"
            and scope.get("inventory", {}).get("kind") == "operation_manifest"
        ):
            registry_rows = [
                row for row in release_manifest.get("artifacts", [])
                if isinstance(row, dict) and row.get("path") == "data/data-go-kr.registry.json"
            ]
            if len(registry_rows) == 1:
                registry_member = registry_rows[0]
                # Candidate mode may have replaced the working tree Registry.
                # Reuse only an independently materialized retained payload;
                # the committed LFS pointer and historical manifest below must
                # still prove that these exact bytes belonged to this release.
                registry_path = root / registry_member["path"]
                if retained_registry_payload_bytes is not None:
                    registry_bytes = retained_registry_payload_bytes
                elif registry_path.is_file():
                    registry_bytes = registry_path.read_bytes()
                else:
                    raise ValueError("published Registry payload is not locally available at its exact native identity")
                if (
                    len(registry_bytes) != registry_member.get("bytes")
                    or sha256_bytes(registry_bytes) != registry_member.get("sha256")
                ):
                    raise ValueError("published Registry payload is not locally available at its exact native identity")
                validate_committed_release_artifact(
                    root=root, revision=publication_source_sha, path=registry_member["path"],
                    expected_sha256=registry_member["sha256"], expected_bytes=registry_member["bytes"],
                    label="#605 projected Registry input", expected_content=registry_bytes,
                )
                projected = project_registered_data_go_operation_manifest(
                    root=root, revision=publication_source_sha, registry_bytes=registry_bytes,
                )
                if projected != source_bytes:
                    raise ValueError("published Registry does not project to the exact registered operation-manifest source")
                operation_projection = {
                    "adapter": "data-go-kr-registry-to-operation-manifest-v1",
                    "registry_path": registry_member["path"],
                    "registry_bytes": registry_member["bytes"],
                    "registry_sha256": registry_member["sha256"],
                    "operation_manifest_sha256": source_sha,
                    "projection_sha256": sha256_bytes(projected),
                    "source_revision": publication_source_sha,
                }
        return {
            "adapter": "registered-source-snapshot-exact-bytes-v1",
            "scope_id": scope["scope_id"],
            "source_id": scope["source_id"],
            "resource_kind": scope["resource_kind"],
            "identity_algorithm": scope["identity_algorithm"],
            "path": registered_path,
            "bytes": source_input["bytes"],
            "sha256": source_sha,
            "operation_projection": operation_projection,
        }

    if (
        scope.get("source_id") == "data_go_kr"
        and scope.get("resource_kind") == "api_operation_manifest"
        and scope.get("inventory", {}).get("kind") == "operation_manifest"
        and publication_path == "data/data-go-kr.registry.json"
        and release_member.get("path") == publication_path
    ):
        registry_bytes = (root / publication_path).read_bytes()
        if (
            len(registry_bytes) != release_member.get("bytes")
            or sha256_bytes(registry_bytes) != release_member.get("sha256")
        ):
            raise ValueError("published Registry projection payload is not locally available at its exact native identity")
        validate_committed_release_artifact(
            root=root, revision=publication_source_sha, path=publication_path,
            expected_sha256=release_member["sha256"], expected_bytes=release_member["bytes"],
            label="#605 projected Registry input", expected_content=registry_bytes,
        )
        projection = project_registered_data_go_operation_manifest(
            root=root, revision=publication_source_sha, registry_bytes=registry_bytes,
        )
        if projection != source_bytes:
            raise ValueError("published Registry does not project to the exact registered operation-manifest source")
        return {
            "adapter": "data-go-kr-registry-to-operation-manifest-v1",
            "scope_id": scope["scope_id"],
            "source_id": scope["source_id"],
            "resource_kind": scope["resource_kind"],
            "identity_algorithm": scope["identity_algorithm"],
            "source_path": registered_path,
            "source_bytes": len(source_bytes),
            "source_sha256": source_sha,
            "published_path": publication_path,
            "published_bytes": release_member["bytes"],
            "published_sha256": release_member["sha256"],
            "projection_sha256": sha256_bytes(projection),
            "projection_revision": publication_source_sha,
            "operation_projection": {
                "adapter": "data-go-kr-registry-to-operation-manifest-v1",
                "registry_path": publication_path,
                "registry_bytes": release_member["bytes"],
                "registry_sha256": release_member["sha256"],
                "operation_manifest_sha256": source_sha,
                "projection_sha256": sha256_bytes(projection),
                "source_revision": publication_source_sha,
            },
        }
    raise ValueError(
        f"published member {publication_path} has no registered semantic subject adapter for {scope['scope_id']}"
    )


def project_registered_data_go_operation_manifest(
    *, root: pathlib.Path, revision: str, registry_bytes: bytes,
) -> bytes:
    """Run the pinned #605 projection over exact published Registry bytes."""
    source_paths = DATA_GO_KR_OPERATION_PROJECTION_CONTRACT
    source_bytes = {
        path: git_read_only(root, ["show", f"{revision}:{path}"])
        for path in source_paths
    }
    if {path: sha256_bytes(raw) for path, raw in source_bytes.items()} != source_paths:
        raise ValueError("published source revision has an unreviewed #605 projection contract")
    try:
        registry = json.loads(registry_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("published Registry payload is not valid JSON for #605 projection") from exc
    if not isinstance(registry, list):
        raise ValueError("published Registry payload has an unsupported #605 source shape")

    with tempfile.TemporaryDirectory(prefix="completeness-605-projection-") as directory:
        projection_root = pathlib.Path(directory)
        script_path = projection_root / "scripts/generate-data-go-kr-operation-manifest.py"
        script_path.parent.mkdir(parents=True)
        script_path.write_bytes(source_bytes["scripts/generate-data-go-kr-operation-manifest.py"])
        registry_path = projection_root / "data/data-go-kr.registry.json"
        registry_path.parent.mkdir(parents=True)
        registry_path.write_bytes(registry_bytes)
        module = import_module("pinned_data_go_operation_projection", script_path)
        previous_registry = module.REGISTRY
        module.REGISTRY = registry_path
        try:
            projected = module.build(registry)
            rendered = module.render(projected)
        finally:
            module.REGISTRY = previous_registry

    schema = json.loads(source_bytes["schemas/datapan.data-go-kr-operation-manifest.v1.schema.json"])
    expectation_raw = git_read_only(
        root, ["show", f"{revision}:policy/data-go-kr-operation-denominator-expectation.json"],
    )
    expectation = json.loads(expectation_raw)
    expectation_schema = json.loads(
        source_bytes["schemas/datapan.data-go-kr-operation-denominator-expectation.v1.schema.json"]
    )
    for value, schema_value, label in (
        (projected, schema, "#605 projected manifest"),
        (expectation, expectation_schema, "#605 source-bound denominator expectation"),
    ):
        errors = sorted(
            jsonschema.Draft202012Validator(
                schema_value, format_checker=jsonschema.FormatChecker(),
            ).iter_errors(value),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            detail = "; ".join(
                f"{'/'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
                for error in errors
            )
            raise ValueError(f"{label} schema: {detail}")
    snapshot = {
        "path": "data/data-go-kr.registry.json",
        "bytes": len(registry_bytes),
        "sha256": sha256_bytes(registry_bytes),
    }
    if (
        projected.get("source_snapshot") != snapshot
        or expectation.get("source_snapshot") != snapshot
        or projected.get("summary") != expectation.get("expected_summary")
        or projected.get("summary", {}).get("identity_collisions") != 0
        or projected.get("summary", {}).get("identity_omissions") != 0
    ):
        raise ValueError("#605 projection identity, source snapshot, or denominator expectation differs")
    return rendered


def validate_scope_import_evidence(
    *, root: pathlib.Path, scope: dict[str, Any], scoped_inputs: list[dict[str, Any]],
    resolved: dict[str, pathlib.Path], proof: dict[str, Any], source_input: dict[str, Any],
    publication_subject: dict[str, Any], publication_source_sha: str,
    publication_manifest_sha256: str, release_manifest: dict[str, Any],
    pipeline_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    """Dispatch only to the import contract registered for this scope subject."""
    import_roles = (
        "historical_import_admission", "historical_import_receipt", "historical_import_attestation",
    )
    has_import_inputs = any(any(item["role"] == role for item in scoped_inputs) for role in import_roles)
    import_path = registered_historical_import_path(scope)
    imported: dict[str, Any] | None = None
    if import_path is not None and has_import_inputs:
        # Authenticate #633 against the source manifest it actually imported.
        # Applicability to this proof is a separate digest join below.
        imported = validate_historical_import(
            root=root, inputs=scoped_inputs, resolved=resolved, scope=scope,
        )
        if imported is not None and imported.get("source_artifact_sha256") == proof["source_snapshot"]["sha256"]:
            receipt = single_role(scoped_inputs, "historical_import_attestation")
            assert receipt is not None
            return {
                **imported,
                "adapter": "data-go-kr-runtime-import-633-v1",
                "receipt": {"path": receipt["path"], "sha256": receipt["sha256"]},
            }

    # The canonical promotion journal binds the accepted Registry artifact to
    # an exact merged source/manifest. Require a separately verified #605
    # projection from that same release to the registered operation subject.
    operation_projection = publication_subject.get("operation_projection")
    promotion = pipeline_evidence.get("promotion", {}) if pipeline_evidence else {}
    journal_records = promotion.get("journal_records", [])
    registry_rows = [
        row for row in release_manifest.get("artifacts", [])
        if isinstance(row, dict) and row.get("path") == "data/data-go-kr.registry.json"
    ]
    matching_c_rows: list[dict[str, Any]] = []
    if (
        import_path is not None
        and isinstance(operation_projection, dict)
        and pipeline_evidence is not None
        and pipeline_evidence.get("status") == "verified_historical_chain"
        and len(registry_rows) == 1
    ):
        registry_member = registry_rows[0]
        for row in journal_records:
            candidate = row.get("candidate") if isinstance(row, dict) else None
            pr = row.get("pr") if isinstance(row, dict) else None
            if not isinstance(candidate, dict) or not isinstance(pr, dict):
                continue
            if (
                row.get("status") == "read-back-confirmed"
                and candidate.get("repository", "").casefold() == "statpan/datapan-registry"
                and candidate.get("source_id") == scope["source_id"]
                and candidate.get("registry_path") == registry_member.get("path")
                and candidate.get("registry_sha256") == registry_member.get("sha256")
                and candidate.get("registry_bytes") == registry_member.get("bytes")
                and candidate.get("manifest_sha256") == publication_manifest_sha256
                and pr.get("merge_commit_sha") == publication_source_sha
            ):
                matching_c_rows.append(row)
    if len(matching_c_rows) == 1:
        row = matching_c_rows[0]
        candidate = row["candidate"]
        promotion_roles = stage_input_map(scoped_inputs, "promotion")
        journal_item = promotion_roles.get("promotion_journal_blob_api")
        if journal_item is None:
            raise ValueError("verified C import lacks its immutable promotion journal input")
        source_bytes = receipt_path_bytes(resolved[source_input["input_id"]], source_input)
        source_manifest = json.loads(source_bytes)
        identities = [
            operation.get("operation_id")
            for operation in source_manifest.get("operations", [])
            if isinstance(operation, dict)
        ]
        if (
            operation_projection.get("adapter") != "data-go-kr-registry-to-operation-manifest-v1"
            or operation_projection.get("operation_manifest_sha256") != proof["source_snapshot"]["sha256"]
            or operation_projection.get("source_revision") != publication_source_sha
            or not identities
            or any(not isinstance(value, str) or not value for value in identities)
            or len(identities) != len(set(identities))
        ):
            raise ValueError("C import does not join the exact #605 projection to the proof subject")
        return {
            "adapter": "data-go-kr-canonical-promotion-plus-605-projection-v1",
            "run_id": promotion["run_id"],
            "attempt": promotion["attempt"],
            "merge_commit": publication_source_sha,
            "source_artifact_path": source_input["path"],
            "source_artifact_sha256": source_input["sha256"],
            "selected_identity_count": len(identities),
            "selected_identity_set_sha256": sha256_bytes(json.dumps(
                sorted(identities), ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")),
            "receipt": {"path": journal_item["path"], "sha256": journal_item["sha256"]},
            "evidence": [artifact(input_path(journal_item), receipt_path_bytes(
                resolved[journal_item["input_id"]], journal_item,
            ))],
            "lineage": {
                "status": "verified_historical_chain",
                "journal_record_generation_id": candidate.get("generation_id"),
                "composition_receipt_sha256": candidate.get("composition_receipt_sha256"),
                "merge_commit": publication_source_sha,
                "release_manifest_sha256": publication_manifest_sha256,
                "registry_sha256": candidate.get("registry_sha256"),
                "registry_bytes": candidate.get("registry_bytes"),
                "current_pipeline_generation_id": pipeline_evidence["processor"].get("generation_id"),
                "current_pipeline_payload_sha256": pipeline_evidence["current_registry"].get("sha256"),
            },
            "historical_633": imported,
        }
    raise ValueError(
        f"scope {scope['scope_id']} has no owning import contract that admits this exact proof subject"
    )


def validate_historical_import(
    *,
    root: pathlib.Path,
    inputs: list[dict[str, Any]],
    resolved: dict[str, pathlib.Path],
    scope: dict[str, Any],
    source_input: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    scope_id = scope["scope_id"]
    scoped = [item for item in inputs if item["scope_id"] == scope_id]
    by_role: dict[str, dict[str, Any]] = {}
    for role in ("historical_import_admission", "historical_import_receipt", "historical_import_attestation"):
        matches = [item for item in scoped if item["role"] == role]
        if len(matches) > 1:
            raise ValueError(f"scope {scope_id} has duplicate {role} inputs")
        if matches:
            by_role[role] = matches[0]
    if not by_role:
        return None
    # #633 is a runtime-freshness operation import, not a generic receipt for
    # every resource produced by data.go.kr. Keep its admission contract tied
    # to the one registered operation-manifest selector it actually handled.
    # Other resource kinds need their own registered import adapter before an
    # import receipt can contribute to an updated claim.
    registered_import_path = registered_historical_import_path(scope)
    if registered_import_path is None:
        raise ValueError(
            f"scope {scope_id} has no registered historical import adapter for its resource kind and selector"
        )
    if set(by_role) != {"historical_import_admission", "historical_import_receipt", "historical_import_attestation"}:
        raise ValueError(f"scope {scope_id} has an incomplete historical import evidence bundle")

    admission_item = by_role["historical_import_admission"]
    import_item = by_role["historical_import_receipt"]
    attestation_item = by_role["historical_import_attestation"]
    if any(item["namespace"] != "historical_admitted" for item in by_role.values()):
        raise ValueError("historical import evidence must use the historical_admitted namespace")
    admission_path = resolved[admission_item["input_id"]]
    import_path = resolved[import_item["input_id"]]
    attestation_path = resolved[attestation_item["input_id"]]
    admission = object_at(admission_path, "historical import admission")
    receipt = object_at(import_path, "historical import receipt")
    attestation = object_at(attestation_path, "historical import attestation")
    module = import_module("runtime_import_attestation", root / "scripts/attest-runtime-freshness-import.py")
    module.validate_admission_contract(admission)
    module.validate_schema(admission, root / "schemas/datapan.runtime-freshness-import-admission.v1.schema.json")
    module.validate_schema(receipt, root / "schemas/datapan.runtime-freshness-import-receipt.v1.schema.json")
    module.validate_schema(attestation, root / "schemas/datapan.runtime-freshness-import-attestation.v1.schema.json")

    run_id = admission.get("run_id")
    if not isinstance(run_id, str) or receipt.get("run_id") != run_id or attestation.get("run_id") != run_id:
        raise ValueError("historical import run identities differ")
    validate_import_receipt_arithmetic(admission=admission, receipt=receipt, source_id=scope["source_id"])
    lineage = attestation.get("lineage", {})
    admitted_inputs = admission.get("inputs", {})
    import_ref = admitted_inputs.get("import_receipt")
    if lineage.get("admission") != {"path": admission_item["path"], "sha256": admission_item["sha256"]}:
        raise ValueError("historical attestation admission path or digest differs")
    if lineage.get("import_receipt") != import_ref or import_ref != {"path": import_item["path"], "sha256": import_item["sha256"]}:
        raise ValueError("historical attestation import receipt path or digest differs")
    if lineage.get("sanitized_report_sha256") != admitted_inputs.get("sanitized_report_sha256"):
        raise ValueError("historical attestation sanitized report digest differs")
    if lineage.get("run_receipt_sha256") != admitted_inputs.get("run_receipt_sha256"):
        raise ValueError("historical attestation producer receipt digest differs")
    if lineage.get("selected_identity_set") != admission.get("selected_identity_set"):
        raise ValueError("historical attestation identity-set binding differs")
    if attestation.get("producer") != admission.get("producer") or attestation.get("outcome") != admission.get("outcome"):
        raise ValueError("historical attestation producer or outcome differs")
    if attestation.get("import", {}).get("arithmetic") != admission.get("arithmetic"):
        raise ValueError("historical attestation import arithmetic differs")
    for item in by_role.values():
        producer = item["producer"]
        if producer["repository"].lower() != "statpan/datapan-registry":
            raise ValueError("historical import evidence producer repository differs")
        if producer["run_id"] not in {None, run_id}:
            raise ValueError("historical import evidence input run identity differs")

    merge_commit = attestation.get("import", {}).get("merge_commit")
    if not isinstance(merge_commit, str) or not re.fullmatch(r"[a-f0-9]{40,64}", merge_commit):
        raise ValueError("historical import merge commit is malformed")
    assert_main_ancestor(root, merge_commit, "historical import merge commit")
    admission_git_path = normalize_relative_path(admission_item["path"], "historical admission path").as_posix()
    merged_admission = git_read_only(root, ["show", f"{merge_commit}:{admission_git_path}"])
    if merged_admission != admission_path.read_bytes():
        raise ValueError("historical merge commit does not contain the admitted bytes")
    import_git_path = normalize_relative_path(import_item["path"], "historical import receipt path").as_posix()
    merged_receipt = git_read_only(root, ["show", f"{merge_commit}:{import_git_path}"])
    if merged_receipt != receipt_path_bytes(import_path, import_item):
        raise ValueError("historical merge commit does not contain the admitted import receipt bytes")
    attestation_revision = attestation_item["producer"].get("revision")
    if not isinstance(attestation_revision, str):
        raise ValueError("historical attestation input lacks its actual main revision")
    assert_main_ancestor(root, attestation_revision, "historical attestation main revision")
    attestation_git_path = normalize_relative_path(attestation_item["path"], "historical attestation path").as_posix()
    merged_attestation = git_read_only(root, ["show", f"{attestation_revision}:{attestation_git_path}"])
    if merged_attestation != receipt_path_bytes(attestation_path, attestation_item):
        raise ValueError("historical attestation bytes were not admitted at the recorded main revision")
    historical_manifest_binding = attestation.get("registry", {}).get("manifest")
    if not isinstance(historical_manifest_binding, dict):
        raise ValueError("historical attestation lacks release-manifest binding")
    historical_manifest_path = normalize_relative_path(historical_manifest_binding.get("path"), "historical manifest path").as_posix()
    historical_manifest_bytes = git_read_only(root, ["show", f"{merge_commit}:{historical_manifest_path}"])
    if sha256_bytes(historical_manifest_bytes) != historical_manifest_binding.get("sha256"):
        raise ValueError("historical release-manifest bytes do not match the attestation")
    historical_manifest = json.loads(historical_manifest_bytes)
    if source_input is not None:
        source_path = normalize_relative_path(source_input["path"], "imported source snapshot path").as_posix()
        if source_path != registered_import_path:
            raise ValueError("historical #633 import cannot attest a different registered scope source path")
        source_sha256 = source_input["sha256"]
        source_bytes = receipt_path_bytes(resolved[source_input["input_id"]], source_input)
        source_size = source_input["bytes"]
        input_subject_matches(source_input, {
            "scope_id": scope_id,
            "source_id": scope["source_id"],
            "resource_kind": scope["resource_kind"],
            "identity_algorithm": scope["identity_algorithm"],
        }, "imported authoritative source snapshot")
    else:
        source_path = registered_import_path
        source_rows = [
            row for row in historical_manifest.get("artifacts", [])
            if isinstance(row, dict) and row.get("path") == source_path
        ]
        if len(source_rows) != 1:
            raise ValueError("historical release manifest does not contain the exact registered scope source artifact")
        source_sha256 = source_rows[0].get("sha256")
        source_size = source_rows[0].get("bytes")
        source_bytes = None
    validate_committed_release_artifact(
        root=root, revision=merge_commit, path=source_path,
        expected_sha256=source_sha256, expected_bytes=source_size,
        label="historically imported scope source artifact", expected_content=source_bytes,
    )
    for label, binding in (("release ledger", attestation.get("registry", {}).get("release_ledger")),):
        if not isinstance(binding, dict):
            raise ValueError(f"historical attestation lacks {label} binding")
        path = normalize_relative_path(binding.get("path"), f"historical {label} path").as_posix()
        data = git_read_only(root, ["show", f"{merge_commit}:{path}"])
        if sha256_bytes(data) != binding.get("sha256"):
            raise ValueError(f"historical {label} digest does not match the recorded merge subject")
    attested_at = attestation.get("attested_at")
    parse_time(attested_at, "historical import attested_at")
    return {
        "run_id": run_id,
        # The retained #633 packet predates an exact attempt field. Keep it unknown
        # rather than echoing an input-index assertion as producer fact.
        "attempt": None,
        "attested_at": attested_at,
        "merge_commit": merge_commit,
        "manifest_sha256": historical_manifest_binding["sha256"],
        "source_artifact_path": source_path,
        "source_artifact_sha256": source_sha256,
        # Retain the existing facet key for the one registered operation
        # scope that historically used #633's operation-manifest import.
        "operation_manifest_sha256": source_sha256 if scope["resource_kind"] == "api_operation_manifest" else None,
        "selected_identity_count": admission["selected_identity_set"]["count"],
        "evidence": [artifact(input_path(item), read_bytes(resolved[item["input_id"]])) for item in (admission_item, import_item, attestation_item)],
    }


def receipt_path_bytes(path: pathlib.Path, item: dict[str, Any]) -> bytes:
    value = path.read_bytes()
    if len(value) != item["bytes"] or sha256_bytes(value) != item["sha256"]:
        raise ValueError("receipt bytes changed after input-index validation")
    return value


def verify_main_committed_input(
    *, root: pathlib.Path, item: dict[str, Any], path: pathlib.Path, label: str
) -> str:
    """Bind exact local bytes to their Registry admission commit.

    ``producer.revision`` remains the source producer's revision. External
    producer bytes copied into this repository use ``subject.admission_revision``
    for the distinct Registry commit that admitted those exact bytes.
    """
    subject = item.get("subject", {})
    revision = subject.get("admission_revision") or item["producer"].get("revision")
    if item["root"] not in {"repository", "evidence"} or item["namespace"] == "fixture" or not isinstance(revision, str):
        raise ValueError(f"{label} must be non-fixture evidence with a recorded Registry admission revision")
    if not subject.get("admission_revision") and item["producer"]["repository"].lower() != "statpan/datapan-registry":
        raise ValueError(f"{label} external producer input lacks a separate Registry admission revision")
    if len(revision) not in {40, 64} or not re.fullmatch(r"[a-f0-9]+", revision):
        raise ValueError(f"{label} main revision is malformed")
    assert_main_ancestor(root, revision, f"{label} Registry admission revision")
    git_path = normalize_relative_path(item["path"], f"{label} path").as_posix()
    committed = git_read_only(root, ["show", f"{revision}:{git_path}"])
    if committed != receipt_path_bytes(path, item):
        raise ValueError(f"{label} bytes do not match the recorded main revision")
    return revision


def single_role(scoped_inputs: list[dict[str, Any]], role: str, *, required: bool = True) -> dict[str, Any] | None:
    matches = [item for item in scoped_inputs if item["role"] == role]
    if len(matches) > 1 or (required and len(matches) != 1):
        raise ValueError(f"scope evidence requires exactly one {role} input")
    return matches[0] if matches else None


def input_subject_matches(item: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    subject = item.get("subject", {})
    for key, value in expected.items():
        if subject.get(key) != value:
            raise ValueError(f"{label} subject does not bind {key}")


def stage_input_map(scoped_inputs: list[dict[str, Any]], stage: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in scoped_inputs:
        if item.get("subject", {}).get("stage") != stage:
            continue
        role = item["role"]
        if role in found:
            raise ValueError(f"pipeline stage {stage} repeats role {role}")
        found[role] = item
    required_roles = PIPELINE_STAGE_ROLES[stage]
    allowed_roles = required_roles | PIPELINE_STAGE_OPTIONAL_ROLES.get(stage, set())
    if found and (not required_roles.issubset(found) or not set(found).issubset(allowed_roles)):
        raise ValueError(
            f"pipeline stage {stage} is incomplete: missing={sorted(PIPELINE_STAGE_ROLES[stage] - set(found))}"
        )
    return found


def stage_value(
    roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path], role: str,
    *, label: str | None = None,
) -> dict[str, Any]:
    item = roles.get(role)
    if item is None:
        raise ValueError(f"pipeline evidence is missing role {role}")
    value = object_at(resolved[item["input_id"]], label or role)
    return value


def indexed_json(
    roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path], role: str,
) -> dict[str, Any]:
    return stage_value(roles, resolved, role)


def validate_stage_run(
    *, stage: str, roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path],
    evaluation_epoch: str, root: pathlib.Path = ROOT,
) -> tuple[dict[str, Any], dict[str, Any], datetime, datetime]:
    contract = PIPELINE_WORKFLOWS[stage]
    run_role = "pipeline_run" if stage in {"source", "processor", "promotion", "health"} else {
        "publisher": "publication_producer_run", "acknowledgement": "acknowledgement_run",
    }[stage]
    jobs_role = "pipeline_jobs" if stage in {"source", "processor", "promotion", "health"} else {
        "publisher": "publication_producer_jobs", "acknowledgement": "acknowledgement_jobs",
    }[stage]
    run_item = roles.get(run_role)
    jobs_item = roles.get(jobs_role)
    if run_item is None or jobs_item is None:
        raise ValueError(f"pipeline stage {stage} lacks exact run and jobs evidence")
    run = object_at(resolved[run_item["input_id"]], f"{stage} workflow run")
    jobs = object_at(resolved[jobs_item["input_id"]], f"{stage} workflow jobs")
    producer = run_item["producer"]
    repository = run.get("repository")
    head_repository = run.get("head_repository")
    run_path = str(run.get("path", "")).split("@", 1)[0]
    run_id = str(run.get("id", ""))
    attempt = run.get("run_attempt")
    workflow_id = run.get("workflow_id")
    head_sha = run.get("head_sha")
    if (
        run_id != producer.get("run_id")
        or isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1
        or isinstance(producer.get("attempt"), bool) or producer.get("attempt") != attempt
        or workflow_id != contract["workflow_id"]
        or producer.get("workflow_id") != contract["workflow_id"]
        or run.get("name") != contract["workflow_name"]
        or run_path != contract["workflow_path"]
        or producer.get("workflow_path") != contract["workflow_path"]
        or producer.get("repository", "").casefold() != "statpan/datapan-registry"
        or (not isinstance(repository, dict) or str(repository.get("full_name", "")).casefold() != "statpan/datapan-registry")
        or (not isinstance(head_repository, dict) or str(head_repository.get("full_name", "")).casefold() != "statpan/datapan-registry")
        or str((repository or {}).get("id")) != "1278568329"
        or str((head_repository or {}).get("id")) != "1278568329"
        or run.get("head_branch") != "main"
        or not isinstance(head_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", head_sha)
        or run.get("event") not in contract["events"]
        or producer.get("event") != run.get("event")
        or run.get("status") != "completed" or run.get("conclusion") != "success"
        or producer.get("revision") != head_sha
        or run_item["namespace"] != "live_operational"
    ):
        raise ValueError(f"pipeline stage {stage} is not the exact trusted successful default-branch workflow run")
    assert_main_ancestor(root, head_sha, f"{stage} workflow source commit")

    started_value = run.get("run_started_at") or run.get("created_at")
    if not isinstance(started_value, str):
        raise ValueError(f"pipeline stage {stage} run lacks a start timestamp")
    run_started = parse_time(started_value, f"{stage} run start")
    cutoff = parse_time(evaluation_epoch, "pipeline evaluation epoch")
    if run_started > cutoff:
        raise ValueError(f"pipeline stage {stage} run started after the evaluation epoch")

    job_rows = jobs.get("jobs")
    if (
        isinstance(jobs.get("total_count"), bool)
        or not isinstance(jobs.get("total_count"), int)
        or jobs.get("total_count") != len(contract["job_names"])
        or not isinstance(job_rows, list) or len(job_rows) != len(contract["job_names"])
    ):
        raise ValueError(f"pipeline stage {stage} jobs API has a missing or extra job")
    matching = [row for row in job_rows if isinstance(row, dict) and row.get("name") in contract["job_names"]]
    if len(matching) != len(contract["job_names"]):
        raise ValueError(f"pipeline stage {stage} jobs API does not contain its registered job")
    job = matching[0]
    job_run_id = job.get("run_id")
    job_attempt = job.get("run_attempt")
    job_id = job.get("id")
    if (
        isinstance(job_run_id, bool) or not isinstance(job_run_id, int) or str(job_run_id) != run_id
        or isinstance(job_attempt, bool) or not isinstance(job_attempt, int) or job_attempt != attempt
        or isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1
        or job.get("head_sha") != head_sha or job.get("head_branch") != "main"
        or job.get("status") != "completed" or job.get("conclusion") != "success"
        or not isinstance(job.get("started_at"), str) or not isinstance(job.get("completed_at"), str)
    ):
        raise ValueError(f"pipeline stage {stage} job does not bind the exact successful run attempt")
    job_started = parse_time(job["started_at"], f"{stage} job start")
    job_completed = parse_time(job["completed_at"], f"{stage} job completion")
    run_completed_value = run.get("run_completed_at")
    run_completed = parse_time(run_completed_value, f"{stage} run completion") if isinstance(run_completed_value, str) else job_completed
    if not run_started <= job_started <= job_completed <= cutoff or run_completed > cutoff:
        raise ValueError(f"pipeline stage {stage} job timestamps are outside its run or after evaluation")
    for item in roles.values():
        input_producer = item["producer"]
        if (
            item.get("namespace") != "live_operational"
            or str(input_producer.get("repository", "")).casefold() != "statpan/datapan-registry"
            or input_producer.get("run_id") != run_id
            or isinstance(input_producer.get("attempt"), bool) or input_producer.get("attempt") != attempt
            or input_producer.get("workflow_id") != contract["workflow_id"]
            or input_producer.get("workflow_path") != contract["workflow_path"]
            or input_producer.get("event") != run.get("event")
            or input_producer.get("revision") != head_sha
        ):
            raise ValueError(f"pipeline stage {stage} indexed inputs disagree with the authenticated run identity")
    return run, job, run_started, job_completed


def safe_zip_members(raw: bytes, *, label: str, max_archive: int, max_expanded: int) -> dict[str, bytes]:
    if len(raw) > max_archive:
        raise ValueError(f"{label} ZIP exceeds its compressed byte limit")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise ValueError(f"{label} ZIP contains duplicate paths")
            total = 0
            output: dict[str, bytes] = {}
            for info in infos:
                path = pathlib.PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (
                    path.is_absolute() or not path.parts or ".." in path.parts or "\\" in info.filename
                    or path.as_posix() != info.filename or info.is_dir() or (mode & 0o170000) == 0o120000
                ):
                    raise ValueError(f"{label} ZIP contains an unsafe path, directory, or symlink")
                if info.file_size < 0 or info.file_size > max_expanded:
                    raise ValueError(f"{label} ZIP member exceeds its byte limit")
                total += info.file_size
                if total > max_expanded:
                    raise ValueError(f"{label} ZIP exceeds its expanded byte limit")
                output[info.filename] = archive.read(info)
            return output
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise ValueError(f"{label} is not a valid bounded ZIP") from exc


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def decode_contents_api(value: dict[str, Any], *, expected_path: str, label: str) -> bytes:
    if (
        value.get("type") != "file" or value.get("encoding") != "base64"
        or value.get("path") != expected_path or not isinstance(value.get("content"), str)
    ):
        raise ValueError(f"{label} is not the exact file contents API response")
    try:
        raw = base64.b64decode(value["content"], validate=False)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} has invalid base64 content") from exc
    if len(raw) != value.get("size") or git_blob_sha1(raw) != value.get("sha"):
        raise ValueError(f"{label} bytes do not match the GitHub contents blob identity")
    return raw


def validate_state_tree(
    *, stage: str, roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path],
    ref_role: str, commit_role: str, tree_role: str,
    content_roles: dict[str, str], blob_roles: dict[str, str] | None = None,
) -> dict[str, bytes]:
    ref = indexed_json(roles, resolved, ref_role)
    commit = indexed_json(roles, resolved, commit_role)
    tree = indexed_json(roles, resolved, tree_role)
    ref_sha = ref.get("object", {}).get("sha") if isinstance(ref.get("object"), dict) else None
    tree_sha = commit.get("tree", {}).get("sha") if isinstance(commit.get("tree"), dict) else None
    if (
        ref.get("object", {}).get("type") != "commit"
        or ref_sha != commit.get("sha")
        or tree_sha != tree.get("sha")
        or tree.get("truncated") is not False
        or not isinstance(tree.get("tree"), list)
    ):
        raise ValueError(f"{stage} branch ref, immutable commit, and complete tree API responses disagree")
    path_rows: dict[str, list[dict[str, Any]]] = {}
    for row in tree["tree"]:
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            path_rows.setdefault(row["path"], []).append(row)
    bytes_by_role: dict[str, bytes] = {}
    for role, relative_path in content_roles.items():
        api = indexed_json(roles, resolved, role)
        raw = decode_contents_api(api, expected_path=relative_path, label=f"{stage} {role}")
        rows = path_rows.get(relative_path, [])
        if len(rows) != 1 or rows[0].get("type") != "blob" or rows[0].get("sha") != api.get("sha"):
            raise ValueError(f"{stage} immutable state tree does not contain the exact {role} blob")
        bytes_by_role[role] = raw
    for role, relative_path in (blob_roles or {}).items():
        blob_api = indexed_json(roles, resolved, role)
        try:
            raw = base64.b64decode(blob_api["content"], validate=False)
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"{stage} {role} blob API has invalid base64 content") from exc
        if len(raw) != blob_api.get("size") or git_blob_sha1(raw) != blob_api.get("sha"):
            raise ValueError(f"{stage} {role} raw blob bytes do not match its Git blob identity")
        rows = path_rows.get(relative_path, [])
        if len(rows) != 1 or rows[0].get("type") != "blob" or rows[0].get("sha") != blob_api.get("sha"):
            raise ValueError(f"{stage} immutable state tree does not contain the exact {role} blob")
        bytes_by_role[role] = raw
    return bytes_by_role


def validate_stage_artifact(
    *, stage: str, roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path],
    run: dict[str, Any], job: dict[str, Any], evaluation_epoch: str,
) -> tuple[dict[str, Any], bytes]:
    metadata_role = "pipeline_artifact_metadata" if stage in {"source", "processor", "health"} else {
        "publication_output_artifact" if stage == "publisher" else "pipeline_artifact_metadata"
    }
    archive_role = "pipeline_artifact_archive" if stage in {"source", "processor", "health"} else {
        "publication_artifact_archive" if stage == "publisher" else None
    }
    if archive_role is None:
        raise ValueError(f"pipeline stage {stage} has no registered workflow artifact adapter")
    metadata_input = roles.get(metadata_role)
    archive_input = roles.get(archive_role)
    if metadata_input is None or archive_input is None:
        raise ValueError(f"pipeline stage {stage} lacks its exact artifact metadata or archive")
    response = object_at(resolved[metadata_input["input_id"]], f"{stage} artifact API")
    artifacts = response.get("artifacts")
    if response.get("total_count") != 1 or not isinstance(artifacts, list) or len(artifacts) != 1:
        raise ValueError(f"pipeline stage {stage} artifact API must contain exactly one artifact")
    metadata = artifacts[0]
    archive = receipt_path_bytes(resolved[archive_input["input_id"]], archive_input)
    workflow_run = metadata.get("workflow_run")
    created_at = metadata.get("created_at")
    expires_at = metadata.get("expires_at")
    if (
        str(workflow_run.get("id")) != str(run.get("id"))
        or str(workflow_run.get("repository_id")) != "1278568329"
        or str(workflow_run.get("head_repository_id")) != "1278568329"
        or workflow_run.get("head_branch") != "main"
        or workflow_run.get("head_sha") != run.get("head_sha")
        or metadata.get("expired") is not False
        or isinstance(metadata.get("size_in_bytes"), bool)
        or metadata.get("size_in_bytes") != len(archive)
        or str(metadata.get("digest", "")).removeprefix("sha256:") != sha256_bytes(archive)
        or not isinstance(metadata.get("id"), int)
        or not isinstance(metadata.get("name"), str)
        or not isinstance(created_at, str) or not isinstance(expires_at, str)
    ):
        raise ValueError(f"pipeline stage {stage} artifact metadata does not bind the exact run and archive bytes")
    created = parse_time(created_at, f"{stage} artifact creation")
    expires = parse_time(expires_at, f"{stage} artifact expiry")
    job_started = parse_time(job["started_at"], f"{stage} job start")
    job_completed = parse_time(job["completed_at"], f"{stage} job completion")
    if not job_started <= created <= job_completed or expires <= created:
        raise ValueError(f"pipeline stage {stage} artifact was not created by the exact job or had an invalid retention interval")
    return metadata, archive


def expected_workflow_producer(
    stage: str, roles: dict[str, dict[str, Any]], run: dict[str, Any]
) -> dict[str, Any]:
    contract = PIPELINE_WORKFLOWS[stage]
    run_role = "pipeline_run" if stage in {"source", "processor", "promotion", "health"} else {
        "publisher": "publication_producer_run", "acknowledgement": "acknowledgement_run",
    }[stage]
    producer = roles[run_role]["producer"]
    return {
        "repository": "StatPan/datapan-registry",
        "run_id": str(run["id"]),
        "attempt": run["run_attempt"],
        "workflow_id": contract["workflow_id"],
        "workflow_path": contract["workflow_path"],
        "event": run["event"],
        "revision": run["head_sha"],
        "admission_revision": roles[run_role].get("subject", {}).get("admission_revision"),
        "indexed_producer": producer,
    }


def source_lfs_binding(root: pathlib.Path, source_sha: str) -> tuple[dict[str, Any], str]:
    manifest_raw = git_read_only(root, ["show", f"{source_sha}:manifest.json"])
    manifest = json.loads(manifest_raw)
    artifacts = [row for row in manifest.get("artifacts", []) if row.get("path") == "data/data-go-kr.registry.json"]
    if len(artifacts) != 1:
        raise ValueError("source release manifest does not uniquely bind the canonical registry")
    pointer = git_read_only(root, ["show", f"{source_sha}:data/data-go-kr.registry.json"]).decode("ascii", "strict")
    match = re.fullmatch(
        r"version https://git-lfs.github.com/spec/v1\n"
        r"oid sha256:([a-f0-9]{64})\n"
        r"size ([0-9]+)\n?",
        pointer,
    )
    if not match:
        raise ValueError("source canonical registry is not a well-formed Git LFS pointer")
    digest, size_text = match.groups()
    size = int(size_text)
    artifact = artifacts[0]
    if artifact.get("sha256") != digest or artifact.get("bytes") != size:
        raise ValueError("source release manifest and committed Git LFS pointer do not identify the same payload")
    return artifact, sha256_bytes(manifest_raw)


def source_refresh_result_contract(
    *, source_config: dict[str, Any], evidence: dict[str, Any],
    diff: dict[str, Any], work_packet: dict[str, Any],
) -> tuple[str, str]:
    """Derive A's result from its producer policy and validate its paired records."""
    summary = diff.get("summary")
    material_fields = source_config.get("diff", {}).get("material_change_fields")
    if (
        not isinstance(summary, dict)
        or set(summary) != {"added", "removed", "changed", "stable"}
        or not isinstance(material_fields, list)
        or not material_fields
        or any(field not in summary for field in material_fields)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in summary.values()
        )
    ):
        raise ValueError("source diff summary or policy material-change fields are invalid")
    material_change = any(summary[field] > 0 for field in material_fields)
    expected_status = "material_change" if material_change else "no_change"
    expected_action = "review_catalog_drift" if material_change else "none"
    key_payload = {
        "source_id": "data_go_kr", "status": expected_status,
        "summary": summary, "error_class": None,
    }
    work_key_digest = hashlib.sha256(
        json.dumps(key_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    expected_work_key = f"upstream-refresh:data_go_kr:{work_key_digest}"
    if (
        evidence.get("source_id") != "data_go_kr"
        or evidence.get("owner") != source_config.get("owner")
        or evidence.get("status") != expected_status
        or evidence.get("collection", {}).get("attempted") is not True
        or evidence.get("collection", {}).get("succeeded") is not True
        or isinstance(evidence.get("collection", {}).get("exit_code"), bool)
        or evidence.get("collection", {}).get("exit_code") != 0
        or evidence.get("collection", {}).get("error_class") is not None
        or evidence.get("review", {}).get("action") != expected_action
        or evidence.get("review", {}).get("work_key") != expected_work_key
        or evidence.get("publication", {}).get("automatic") is not False
        or evidence.get("publication", {}).get("release_allowed") is not False
        or evidence.get("publication", {}).get("required_gates") != source_config.get("publication", {}).get("required_gates")
        or work_packet.get("source_id") != "data_go_kr"
        or work_packet.get("owner") != source_config.get("owner")
        or work_packet.get("status") != expected_status
        or work_packet.get("action") != expected_action
        or work_packet.get("work_key") != expected_work_key
        or work_packet.get("automatic_publication") is not False
        or work_packet.get("snapshot") != evidence.get("snapshot", {}).get("path")
        or work_packet.get("diff") != evidence.get("diff", {}).get("path")
    ):
        raise ValueError("source evidence and work packet disagree with the producer-owned refresh policy")
    return expected_status, expected_work_key


def validate_source_observation(
    *, root: pathlib.Path, roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path],
    run: dict[str, Any], job: dict[str, Any], archive: bytes, evaluation_epoch: str,
) -> dict[str, Any]:
    metadata, verified_archive = validate_stage_artifact(
        stage="source", roles=roles, resolved=resolved, run=run, job=job, evaluation_epoch=evaluation_epoch,
    )
    if verified_archive != archive:
        raise ValueError("source collector archive changed during validation")
    members = safe_zip_members(
        archive, label="source collector artifact", max_archive=128 * 1024 * 1024, max_expanded=256 * 1024 * 1024,
    )
    expected_members = {
        "candidate.registry.json", "upstream-refresh-evidence.json", "catalog-diff.json", "upstream-refresh-work-packet.json",
    }
    if set(members) != expected_members:
        raise ValueError("source collector artifact does not contain the exact four-file contract")
    evidence = json.loads(members["upstream-refresh-evidence.json"])
    diff = json.loads(members["catalog-diff.json"])
    work_packet = json.loads(members["upstream-refresh-work-packet.json"])
    try:
        policy = json.loads(git_read_only(root, ["show", f"{run['head_sha']}:policy/source-refresh.json"]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("source refresh policy is unavailable at its authenticated producer revision") from exc
    validate_schema_at_revision(
        root=root, revision=run["head_sha"],
        relative_path="schemas/datapan.source-refresh-policy.v1.schema.json",
        value=policy, label="source refresh policy",
    )
    source_configs = [
        row for row in policy.get("sources", [])
        if isinstance(row, dict) and row.get("source_id") == "data_go_kr"
    ]
    if len(source_configs) != 1:
        raise ValueError("source refresh policy does not uniquely register data_go_kr")
    source_config = source_configs[0]
    validate_schema_at_revision(
        root=root, revision=run["head_sha"],
        relative_path="schemas/datapan.upstream-refresh-evidence.v1.schema.json",
        value=evidence, label="source refresh evidence",
    )
    validate_schema_at_revision(
        root=root, revision=run["head_sha"],
        relative_path="schemas/datapan.catalog-diff.v1.schema.json",
        value=diff, label="source catalog diff",
    )
    validate_schema_at_revision(
        root=root, revision=run["head_sha"],
        relative_path="schemas/datapan.upstream-refresh-work-packet.v1.schema.json",
        value=work_packet, label="source refresh work packet",
    )
    candidate_bytes = members["candidate.registry.json"]
    observe_steps = [
        step for step in job.get("steps", [])
        if isinstance(step, dict) and step.get("name") == "Observe upstream catalogue without publishing"
    ]
    observe_start = observe_steps[0].get("started_at") if len(observe_steps) == 1 else None
    observed_at = evidence.get("observed_at")
    snapshot = evidence.get("snapshot", {})
    summary = diff.get("summary")
    expected_status, expected_work_key = source_refresh_result_contract(
        source_config=source_config, evidence=evidence, diff=diff, work_packet=work_packet,
    )
    expected_action = "review_catalog_drift" if expected_status == "material_change" else "none"
    if (
        evidence.get("status") != expected_status
        or evidence.get("collection", {}).get("succeeded") is not True
        or evidence.get("collection", {}).get("attempted") is not True
        or not isinstance(observe_start, str)
        or parse_time(observe_start, "collector observation step start") != parse_time(observed_at, "source observation time")
        or evidence.get("baseline", {}).get("path") != source_config.get("canonical_registry")
        or evidence.get("diff", {}).get("sha256") != sha256_bytes(members["catalog-diff.json"])
        or evidence.get("diff", {}).get("summary") != summary
        or diff.get("generated_at") != observed_at
        or diff.get("old") != source_config.get("canonical_registry")
        or diff.get("new") != snapshot.get("path")
        or work_packet.get("observed_at") != observed_at
        or work_packet.get("evidence") != ".datapan/ci/upstream-refresh/upstream-refresh-evidence.json"
        or work_packet.get("snapshot") != snapshot.get("path")
        or work_packet.get("diff") != ".datapan/ci/upstream-refresh/catalog-diff.json"
        or len(candidate_bytes) != snapshot.get("bytes")
        or sha256_bytes(candidate_bytes) != snapshot.get("sha256")
        or snapshot.get("path") != ".datapan/ci/upstream-refresh/candidate.registry.json"
        or roles["pipeline_artifact_archive"].get("observed_at") != observed_at
        or roles["pipeline_artifact_archive"].get("subject", {}).get("generation_id") is not None
    ):
        raise ValueError("source collector artifact does not bind the exact observed candidate and no-publication contract")
    baseline_manifest_artifact, source_manifest_sha256 = source_lfs_binding(root, run["head_sha"])
    baseline = evidence.get("baseline", {})
    if (
        baseline.get("sha256") != baseline_manifest_artifact.get("sha256")
        or baseline.get("bytes") != baseline_manifest_artifact.get("bytes")
        or baseline.get("path") != baseline_manifest_artifact.get("path")
    ):
        raise ValueError("collector baseline does not match the source revision's manifest and LFS pointer")
    return {
        "run_id": str(run["id"]),
        "attempt": run["run_attempt"],
        "workflow_id": run["workflow_id"],
        "head_sha": run["head_sha"],
        "observed_at": observed_at,
        "step_completed_at": observe_steps[0].get("completed_at"),
        "artifact_id": str(metadata["id"]),
        "artifact_sha256": sha256_bytes(archive),
        "candidate_sha256": snapshot["sha256"],
        "candidate_bytes": snapshot["bytes"],
        "refresh_evidence_sha256": sha256_bytes(members["upstream-refresh-evidence.json"]),
        "diff_sha256": evidence["diff"]["sha256"],
        "baseline_sha256": baseline["sha256"],
        "baseline_bytes": baseline["bytes"],
        "release_manifest_sha256": source_manifest_sha256,
        "diff_summary": evidence["diff"]["summary"],
        "status": expected_status,
        "review_action": expected_action,
        "evidence": [
            artifact(input_path(roles["pipeline_run"]), read_bytes(resolved[roles["pipeline_run"]["input_id"]])),
            artifact(input_path(roles["pipeline_artifact_archive"]), archive),
        ],
    }


def assert_main_ancestor(root: pathlib.Path, revision: str, label: str) -> str:
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError(f"{label} is not a full Git commit SHA")
    main_sha = git_read_only(root, ["rev-parse", "refs/remotes/origin/main"]).decode("ascii").strip()
    if not re.fullmatch(r"[a-f0-9]{40}", main_sha):
        raise ValueError("selected trusted origin/main ref is not a full Git commit SHA")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", revision, main_sha], cwd=root,
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise ValueError(f"{label} is not reachable from the selected trusted origin/main history")
    return main_sha


def validate_current_catalog_subject(
    *, root: pathlib.Path, inputs: list[dict[str, Any]], resolved: dict[str, pathlib.Path],
    scope_registry: dict[str, Any], candidate_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    matches = [item for item in inputs if item["role"] == "source_catalog_snapshot"]
    registered_catalogs = [
        scope for scope in scope_registry.get("scopes", [])
        if scope.get("source_id") == "data_go_kr"
        and scope.get("resource_kind") == "api_catalog_metadata"
        and scope.get("inventory", {}).get("input_role") == "source_catalog_snapshot"
    ]
    if len(registered_catalogs) != 1:
        raise ValueError("scope registry must identify exactly one data.go.kr catalog inventory")
    catalog_scope = registered_catalogs[0]
    if len(matches) != 1 or matches[0]["scope_id"] != catalog_scope["scope_id"]:
        raise ValueError("registered data.go.kr catalog scope lacks one exact current registry input")
    item = matches[0]
    inventory = catalog_scope["inventory"]
    if item["path"] != inventory.get("path"):
        raise ValueError("current catalog evidence does not use the registered inventory path")
    if candidate_context is None:
        if item["root"] != "repository":
            raise ValueError("current catalog evidence must use the repository inventory in repository mode")
        path = resolved[item["input_id"]]
        current_bytes = path.stat().st_size
        current_sha = sha256_file(path)
        if (current_bytes, current_sha) != (item["bytes"], item["sha256"]):
            raise ValueError("current materialized registry differs from its registered inventory input")
        source_revision = item.get("subject", {}).get("source_revision")
        main_sha = assert_main_ancestor(root, source_revision, "pinned current catalog source revision")
        main_artifact, manifest_sha = source_lfs_binding(root, source_revision)
        if (main_artifact.get("sha256"), main_artifact.get("bytes")) != (current_sha, current_bytes):
            raise ValueError("current registry inventory differs from its pinned main manifest and LFS pointer")
        catalog_binding_witness = {
            "source_revision": source_revision,
            "release_manifest_sha256": manifest_sha,
            "main_ancestry_verified": True,
        }
        evidence = [artifact(input_path(item), path.read_bytes())]
    else:
        main_sha = candidate_context["baseline_main_sha"]
        assert_main_ancestor(root, main_sha, "candidate historical main subject")
        main_artifact, manifest_sha = source_lfs_binding(root, main_sha)
        baseline = candidate_context["baseline_registry"]
        if (main_artifact.get("sha256"), main_artifact.get("bytes")) != (baseline["sha256"], baseline["bytes"]):
            raise ValueError("candidate historical Registry does not match the pinned baseline main manifest and LFS pointer")
        current_sha, current_bytes = baseline["sha256"], baseline["bytes"]
        manifest_raw = git_read_only(root, ["show", f"{main_sha}:manifest.json"])
        if artifact(f"git:{main_sha}:manifest.json", manifest_raw) != candidate_context["baseline_release_manifest"]:
            raise ValueError("candidate historical release manifest bytes changed")
        pointer_raw = git_read_only(root, ["show", f"{main_sha}:{inventory['path']}"])
        evidence = [artifact(f"git-lfs-pointer:{main_sha}:{inventory['path']}", pointer_raw)]
    return {
        "path": item["path"], "bytes": current_bytes, "sha256": current_sha,
        "release_manifest_sha256": None if candidate_context is None else manifest_sha,
        "catalog_binding_witness": catalog_binding_witness if candidate_context is None else None,
        "current_release_applicable": False,
        "evidence": evidence,
    }


def extract_bounded_processor_bundle(
    archive_bytes: bytes, destination: pathlib.Path, *, expected_members: set[str],
    max_archive_bytes: int, max_expanded_bytes: int,
) -> pathlib.Path:
    if len(archive_bytes) > max_archive_bytes:
        raise ValueError("processor artifact archive exceeds its bounded compressed limit")
    destination.mkdir(parents=True, exist_ok=False)
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or set(names) != expected_members:
                raise ValueError("processor archive has a missing, extra, or duplicate bundle member")
            for info in infos:
                path = pathlib.PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (
                    path.is_absolute() or len(path.parts) != 1 or ".." in path.parts
                    or "\\" in info.filename or path.as_posix() != info.filename
                    or info.is_dir() or (mode & 0o170000) == 0o120000
                ):
                    raise ValueError("processor archive contains an unsafe member path")
                per_member_limit = 64 * 1024 * 1024 if info.filename == "upstream-catalogue-checkpoint-receipt.json" else 512 * 1024 * 1024
                if info.file_size < 0 or info.file_size > per_member_limit:
                    raise ValueError("processor archive member exceeds its bounded size")
                total += info.file_size
                if total > max_expanded_bytes:
                    raise ValueError("processor archive expands beyond its bounded total size")
                target = destination / info.filename
                with archive.open(info, "r") as source, target.open("xb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)
                if target.stat().st_size != info.file_size:
                    raise ValueError("processor archive member size differs from its ZIP directory")
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise ValueError("processor archive is not a valid bounded ZIP bundle") from exc
    return destination


def validate_processor_generation_inputs(
    *, root: pathlib.Path, promotion: Any, checkpoint: dict[str, Any],
    processor_head_sha: str, composition_receipt: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Bind a historical processor output to the code and schemas it actually ran.

    Current-main promotion eligibility is a separate decision. A retained B
    execution remains historical evidence when its source contract later
    changes, so this validator verifies the checkpoint against its own trusted
    workflow head and reports (rather than erasing) any current-contract drift.
    """
    provenance = checkpoint.get("generation_inputs")
    if not isinstance(provenance, dict):
        raise ValueError("processor checkpoint lacks generation input provenance")
    tracked = {
        "policy_sha256": "policy/source-refresh.json",
        "adapter_revision": "data/provider-index.json",
        "extractor_revision": "scripts/generate-batch-link-detail-registry-patches.py",
        "generator_revision": "scripts/process-upstream-catalogue-candidate.py",
    }
    source_bytes: dict[str, bytes] = {}
    for field, path in tracked.items():
        try:
            source_bytes[path] = git_read_only(root, ["show", f"{processor_head_sha}:{path}"])
        except ValueError as exc:
            raise ValueError(f"processor source commit lacks required generation input {path}") from exc
        if field == "generator_revision":
            # The producer evolved from hashing its own file to hashing the
            # script plus its handoff helper. Accept only those two recorded
            # implementations and bind either to bytes at the exact run head.
            direct = sha256_bytes(source_bytes[path])
            expected = direct
            try:
                handoff = git_read_only(root, [
                    "show", f"{processor_head_sha}:scripts/upstream_catalogue_handoff.py"
                ])
            except ValueError:
                handoff = None
            if handoff is not None:
                combined = {
                    "processor_script_sha256": direct,
                    "collector_handoff_helper_sha256": sha256_bytes(handoff),
                }
                expected = sha256_bytes(json.dumps(
                    combined, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8"))
            if provenance.get(field) != expected:
                raise ValueError("processor generator revision does not match its exact producer-head implementation")
        elif provenance.get(field) != sha256_bytes(source_bytes[path]):
            raise ValueError(f"processor checkpoint provenance does not match its source commit: {path}")

    if composition_receipt is not None:
        input_digests = composition_receipt.get("input_digests")
        if not isinstance(input_digests, dict):
            raise ValueError("processor composition receipt lacks input digests")
        for input_name, path in promotion.PROCESSOR_COMPOSITION_INPUTS.items():
            try:
                raw = git_read_only(root, ["show", f"{processor_head_sha}:{path}"])
            except ValueError as exc:
                raise ValueError(f"processor source commit lacks composition input {path}") from exc
            observed = input_digests.get(input_name)
            if not isinstance(observed, dict) or observed.get("bytes") != len(raw) or observed.get("sha256") != sha256_bytes(raw):
                raise ValueError(f"processor composition receipt {input_name} differs from its exact producer-head bytes")

    changed: list[str] = []
    for path in dict.fromkeys(promotion.PROCESSOR_COMPATIBILITY_FILES):
        try:
            historical = git_read_only(root, ["show", f"{processor_head_sha}:{path}"])
            current = (root / path).read_bytes()
        except (OSError, ValueError):
            changed.append(path)
            continue
        if historical != current:
            changed.append(path)
    return not changed, sorted(set(changed))


def validate_processor_input_history(
    checkpoint: dict[str, Any], source: dict[str, Any] | None,
) -> tuple[dict[str, Any], int, int]:
    """Bind the selected observation to a bounded locator history, not count one."""
    rows = checkpoint.get("input_artifacts")
    latest = checkpoint.get("last_observation")
    count = checkpoint.get("observation_count")
    generation_inputs = checkpoint.get("generation_inputs", {})
    if (
        not isinstance(rows, list) or not 1 <= len(rows) <= 8
        or not isinstance(latest, dict)
        or isinstance(count, bool) or not isinstance(count, int) or count < len(rows)
        or not isinstance(generation_inputs, dict)
        or not isinstance(generation_inputs.get("candidate_sha256"), str)
        or not SHA256_RE.fullmatch(generation_inputs["candidate_sha256"])
    ):
        raise ValueError("processor checkpoint does not retain valid bounded collector observation history")
    keys: set[tuple[str, str | None]] = set()
    selected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("processor checkpoint collector input locator is malformed")
        run_id = row.get("run_id")
        artifact_id = row.get("artifact_id")
        key = (str(run_id), row.get("evidence_sha256"))
        if key in keys:
            raise ValueError("processor checkpoint collector history repeats an exact run/evidence locator")
        keys.add(key)
        if (
            not isinstance(run_id, str) or not re.fullmatch(r"[0-9]+", run_id)
            or not isinstance(artifact_id, (str, type(None)))
            or (artifact_id is not None and not re.fullmatch(r"[0-9]+", artifact_id))
            or row.get("name") != f"upstream-catalog-refresh-{run_id}"
            or not isinstance(row.get("expires_at"), str)
            or not isinstance(row.get("evidence_sha256"), (str, type(None)))
            or not isinstance(row.get("diff_sha256"), (str, type(None)))
            or any(
                value is not None and not SHA256_RE.fullmatch(value)
                for value in (row.get("candidate_sha256"), row.get("evidence_sha256"), row.get("diff_sha256"))
            )
        ):
            raise ValueError("processor collector locator does not match its immutable generation input contract")
        parse_time(row["expires_at"], "processor collector locator expiry")
        if (
            str(run_id) == str(latest.get("producer_run_id"))
            and row.get("evidence_sha256") == latest.get("refresh_evidence_sha256")
        ):
            selected.append(row)
    if len(selected) != 1:
        raise ValueError("processor latest observation does not select one retained collector locator")
    selected_row = selected[0]
    if selected_row.get("candidate_sha256") != generation_inputs.get("candidate_sha256"):
        raise ValueError("processor selected collector candidate differs from its immutable generation input")
    if source is not None:
        expected = {
            "run_id": source["run_id"],
            "artifact_id": source["artifact_id"],
            "name": f"upstream-catalog-refresh-{source['run_id']}",
            "candidate_sha256": source["candidate_sha256"],
            "evidence_sha256": source["refresh_evidence_sha256"],
            "diff_sha256": source["diff_sha256"],
        }
        if any(selected_row.get(field) != value for field, value in expected.items()):
            raise ValueError("processor latest collector locator differs from the exact validated source artifact")
        if latest.get("observed_at") != source["observed_at"]:
            raise ValueError("processor latest observation clock differs from the exact validated source run")
    return selected_row, len(rows), count


def validate_processor_stage(
    *, root: pathlib.Path, roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path],
    run: dict[str, Any], job: dict[str, Any], source: dict[str, Any] | None,
    current_registry: dict[str, Any],
    evaluation_epoch: str,
) -> dict[str, Any]:
    _metadata, archive = validate_stage_artifact(
        stage="processor", roles=roles, resolved=resolved, run=run, job=job, evaluation_epoch=evaluation_epoch,
    )
    committed_main = assert_main_ancestor(root, run["head_sha"], "processor workflow source commit")
    head_registry_artifact, head_manifest_sha256 = source_lfs_binding(root, run["head_sha"])
    generation_api = indexed_json(roles, resolved, "processor_generation_api")
    generation_path = generation_api.get("path")
    expected_generation = pathlib.PurePosixPath(generation_path).stem if isinstance(generation_path, str) else ""
    # The contents API path is itself the immutable generation identity. The
    # index row's path names our local evidence file, so it cannot supply this
    # identity.
    if (
        not isinstance(generation_path, str)
        or pathlib.PurePosixPath(generation_path).parent.as_posix()
        != ".datapan/upstream-catalogue-state/sources/data_go_kr/generations"
        or not SHA256_RE.fullmatch(expected_generation)
    ):
        raise ValueError("processor generation contents response has no registered immutable generation path")
    expected_generation_path = (
        ".datapan/upstream-catalogue-state/sources/data_go_kr/generations/"
        f"{expected_generation}.json"
    )
    state_bytes = validate_state_tree(
        stage="processor", roles=roles, resolved=resolved,
        ref_role="processor_state_ref", commit_role="processor_state_commit", tree_role="processor_state_tree",
        content_roles={
            "processor_state_root_api": ".datapan/upstream-catalogue-state/state-root.json",
            "processor_index_api": ".datapan/upstream-catalogue-state/sources/data_go_kr/index.json",
            "processor_generation_api": expected_generation_path,
        },
    )
    state_ref = indexed_json(roles, resolved, "processor_state_ref")
    state_root = json.loads(state_bytes["processor_state_root_api"])
    index = json.loads(state_bytes["processor_index_api"])
    checkpoint = json.loads(state_bytes["processor_generation_api"])
    if (
        state_ref.get("ref") != "refs/heads/automation/upstream-catalogue-state"
        or state_root.get("repository") != "StatPan/datapan-registry"
        or state_root.get("source_id") != "data_go_kr"
        or state_root.get("state_root") != ".datapan/upstream-catalogue-state"
        or state_root.get("schema_version") != "datapan.upstream-catalogue-state-root.v1"
    ):
        raise ValueError("processor state-root response is outside the registered owned branch and source")

    entry_rows = [row for row in index.get("generations", []) if isinstance(row, dict) and row.get("generation_id") == expected_generation]
    if len(entry_rows) != 1:
        raise ValueError("processor index does not contain the exact checkpoint generation once")
    index_entry = entry_rows[0]
    if (
        index_entry.get("checkpoint") != f"{expected_generation}.json"
        or index_entry.get("status") != checkpoint.get("status")
        or index_entry.get("candidate_sha256") != checkpoint.get("generation_inputs", {}).get("candidate_sha256")
    ):
        raise ValueError("processor generation index does not bind the immutable checkpoint fields")

    promotion = import_module("completeness_processor_contract", root / "scripts/run-canonical-update-promotion.py")
    promotion.ROOT = root
    schema_path = root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
    try:
        promotion.verify_processor_checkpoint(checkpoint, schema_path)
        promotion.validate_generation_identity(checkpoint)
    except Exception as exc:
        raise ValueError("processor checkpoint schema or immutable generation identity is invalid") from exc
    # Stage-local validation can run when A is absent. The selected locator is
    # still internally bound, but it is not promoted into an authenticated A
    # observation unless the exact source artifact is independently present.
    input_locator, locator_count, observation_count = validate_processor_input_history(checkpoint, source)
    last_observation = checkpoint.get("last_observation")
    if (
        checkpoint.get("source_id") != "data_go_kr"
        or checkpoint.get("source_scope") != "aggregate_supported_catalog"
        or not isinstance(last_observation, dict)
    ):
        raise ValueError("processor checkpoint does not preserve a valid bounded source observation history")

    artifacts_response = object_at(resolved[roles["pipeline_artifact_metadata"]["input_id"]], "processor artifact API")
    artifacts = artifacts_response.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise ValueError("processor API does not contain exactly one output artifact")
    artifact_meta = artifacts[0]
    try:
        promotion.validate_trusted_processor_run(
            run, repository="StatPan/datapan-registry", run_id=str(run["id"]),
            attempt=str(run["run_attempt"]), default_branch="main", expected_head_sha=run["head_sha"],
        )
        promotion.validate_processor_artifact_metadata(
            artifact_meta, checkpoint, run, repository="StatPan/datapan-registry",
            now=parse_time(evaluation_epoch, "processor artifact evaluation epoch"),
        )
    except Exception as exc:
        raise ValueError("processor run or artifact metadata does not satisfy the existing promotion validator") from exc

    with tempfile.TemporaryDirectory(prefix="completeness-processor-bundle-") as temporary:
        output_dir = pathlib.Path(temporary) / "bundle"
        status = checkpoint.get("status")
        output_names = {
            row.get("path") for row in checkpoint.get("output_digests", [])
            if isinstance(row, dict) and isinstance(row.get("path"), str)
        }
        required = output_names | {"upstream-catalogue-checkpoint-receipt.json"}
        if status in {"ready", "no-change"}:
            required = set(promotion.REQUIRED_PROCESSOR_FILES) | {"upstream-catalogue-checkpoint-receipt.json"}
        elif status not in {"retry", "quarantined"}:
            raise ValueError("processor checkpoint status is not a supported bounded outcome")
        extract_bounded_processor_bundle(
            archive, output_dir, expected_members=required,
            max_archive_bytes=promotion.MAX_PROCESSOR_ARCHIVE_BYTES,
            max_expanded_bytes=promotion.MAX_PROCESSOR_BUNDLE_BYTES,
        )
        composition_helper = promotion.load_canonical_update_pr(root)
        composition_schema = object_at(root / "schemas/datapan.catalogue-composition-receipt.v1.schema.json", "composition schema")
        try:
            bundle = promotion.validate_processor_bundle(
                checkpoint, output_dir, composition_schema, composition_helper,
                root=root, producer_head_sha=str(run["head_sha"]), defer_seoul_declaration=True,
            )
            current_input_contract_compatible, changed_input_paths = validate_processor_generation_inputs(
                root=root, promotion=promotion, checkpoint=checkpoint,
                processor_head_sha=run["head_sha"], composition_receipt=bundle.get("composition_receipt"),
            )
        except Exception as exc:
            raise ValueError("processor archive or producer-head input provenance does not pass validation") from exc
        result = bundle
        composition_receipt = copy.deepcopy(bundle.get("composition_receipt"))
        composition_path = bundle.get("composition_receipt_path")
        composition_sha = sha256_file(pathlib.Path(composition_path)) if composition_path else None
        checkpoint_output = checkpoint.get("output_digests", [])
        outcome = checkpoint.get("outcome", {})
        if outcome.get("full_scope_fresh") is True or outcome.get("publication_allowed") is True:
            raise ValueError("processor outcome cannot assert full-scope freshness or publication eligibility")
        candidate_sha = result.get("registry_sha256")
        candidate_bytes = result.get("registry_bytes")
        if status in {"ready", "no-change"}:
            candidate_row = [row for row in checkpoint_output if isinstance(row, dict) and row.get("path") == "composed-candidate.registry.json"]
            if len(candidate_row) != 1 or candidate_sha is None or candidate_bytes is None:
                raise ValueError("reviewable processor checkpoint does not inventory its exact composed candidate")
        current_is_composition = (
            candidate_sha == current_registry.get("sha256")
            and candidate_bytes == current_registry.get("bytes")
        )
        return {
            "run_id": str(run["id"]), "attempt": run["run_attempt"], "workflow_id": run["workflow_id"],
            "head_sha": run["head_sha"], "source_id": checkpoint["source_id"],
            "source_scope": checkpoint["source_scope"], "generation_id": checkpoint["generation_id"],
            "checkpoint_sha256": checkpoint.get("checkpoint_sha256"),
            "release_manifest_sha256": head_manifest_sha256,
            "execution_head_registry_sha256": head_registry_artifact.get("sha256"),
            "execution_head_registry_bytes": head_registry_artifact.get("bytes"),
            "status": result["status"], "candidate_sha256": candidate_sha,
            "candidate_bytes": candidate_bytes, "baseline_sha256": result.get("baseline_sha256"),
            "candidate_path": result.get("registry_path", "data/data-go-kr.registry.json"),
            "current_subject_sha256": current_registry.get("sha256"),
            "current_subject_bytes": current_registry.get("bytes"),
            "current_subject_matches_composition": current_is_composition,
            "source_observation": copy.deepcopy(last_observation),
            "source_candidate_sha256": checkpoint["generation_inputs"]["candidate_sha256"],
            "observation_count": observation_count,
            "collector_locator_count": locator_count,
            "outcome": copy.deepcopy(outcome),
            "pending_count": outcome.get("pending_count"), "detail_retry_count": outcome.get("detail_retry_count"),
            "detail_unattempted_count": outcome.get("detail_unattempted_count"),
            "full_scope_fresh": outcome.get("full_scope_fresh"), "publication_allowed": outcome.get("publication_allowed"),
            "current_input_contract_compatible": current_input_contract_compatible,
            "current_input_contract_changed_paths": changed_input_paths,
            "composition_sha256": composition_sha, "state_commit": state_ref["object"]["sha"],
            "state_tree_sha": indexed_json(roles, resolved, "processor_state_tree")["sha"],
            "artifact_id": str(artifact_meta["id"]),
            "output_bundle_sha256": checkpoint.get("output_artifact", {}).get("bundle_manifest_sha256"),
            "artifact_sha256": sha256_bytes(archive),
            "evidence": [
                artifact(input_path(roles["pipeline_run"]), read_bytes(resolved[roles["pipeline_run"]["input_id"]])),
                artifact(input_path(roles["pipeline_artifact_archive"]), archive),
                artifact(input_path(roles["processor_generation_api"]), state_bytes["processor_generation_api"]),
                artifact(input_path(roles["processor_index_api"]), state_bytes["processor_index_api"]),
            ],
        }


def bounded_gzip_bytes(raw: bytes, *, label: str, maximum_compressed: int, maximum_expanded: int) -> bytes:
    if len(raw) > maximum_compressed:
        raise ValueError(f"{label} exceeds its compressed byte limit")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as source:
            expanded = source.read(maximum_expanded + 1)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise ValueError(f"{label} is not a valid gzip stream") from exc
    if len(expanded) > maximum_expanded:
        raise ValueError(f"{label} exceeds its expanded byte limit")
    return expanded


def promotion_log_results(raw_log: bytes) -> list[dict[str, Any]]:
    text = bounded_gzip_bytes(
        raw_log, label="promotion job log", maximum_compressed=16 * 1024 * 1024,
        maximum_expanded=64 * 1024 * 1024,
    ).decode("utf-8", "strict")
    found: list[dict[str, Any]] = []
    for line in text.splitlines():
        marker = line.find("{")
        if marker < 0 or not any(token in line[marker:] for token in (
            "already_canonical_generations", "promotion-prs-reconciled",
            "candidate-prepared-locally", "prepared-pr-create-recovered",
            "prepared-source-refresh-recovered",
        )):
            continue
        try:
            value = json.loads(line[marker:])
        except json.JSONDecodeError as exc:
            raise ValueError("promotion structured result is invalid JSON") from exc
        if isinstance(value, dict) and (
            "already_canonical_generations" in value
            or value.get("status") in {
                "promotion-prs-reconciled", "candidate-prepared-locally",
                "prepared-pr-create-recovered", "prepared-source-refresh-recovered",
            }
        ):
            found.append(value)
    if not found:
        raise ValueError("promotion log does not contain a supported structured result")
    return found


def canonical_update_pr_contract_at_revision(*, root: pathlib.Path, revision: str) -> Any:
    relative = "scripts/canonical_update_pr.py"
    try:
        helper_source = git_read_only(root, ["show", f"{revision}:{relative}"])
    except ValueError as exc:
        raise ValueError("promotion journal validator is unavailable at its authenticated execution revision") from exc
    with tempfile.TemporaryDirectory(prefix="completeness-journal-contract-") as temporary:
        helper_path = pathlib.Path(temporary) / "canonical_update_pr.py"
        helper_path.write_bytes(helper_source)
        return import_module("completeness_journal_contract", helper_path)


def validate_promotion_journal_at_revision(
    *, root: pathlib.Path, revision: str, journal: dict[str, Any],
) -> None:
    relative = "schemas/datapan.canonical-update-promotion-journal.v1.schema.json"
    try:
        schema = json.loads(git_read_only(root, ["show", f"{revision}:{relative}"]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("promotion journal schema is unavailable at its authenticated execution revision") from exc
    validate_schema_value(journal, schema, "promotion journal")
    helper = canonical_update_pr_contract_at_revision(root=root, revision=revision)
    try:
        helper.validate_journal(journal, schema)
    except Exception as exc:
        raise ValueError("promotion journal fails its producer-owned revision and transition contract") from exc


def validate_promotion_stage(
    *, root: pathlib.Path, roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path],
    run: dict[str, Any], job: dict[str, Any], processor: dict[str, Any],
    current_registry: dict[str, Any], evaluation_epoch: str,
) -> dict[str, Any]:
    committed_main = assert_main_ancestor(root, run["head_sha"], "promotion workflow source commit")
    log_item = roles["pipeline_log_archive"]
    log_bytes = receipt_path_bytes(resolved[log_item["input_id"]], log_item)
    log_results = promotion_log_results(log_bytes)
    state_bytes = validate_state_tree(
        stage="promotion", roles=roles, resolved=resolved,
        ref_role="promotion_state_ref", commit_role="promotion_state_commit", tree_role="promotion_state_tree",
        content_roles={},
        blob_roles={"promotion_journal_blob_api": "reports/canonical-update-promotion-receipt.json"},
    )
    ref = indexed_json(roles, resolved, "promotion_state_ref")
    journal = json.loads(state_bytes["promotion_journal_blob_api"])
    if (
        ref.get("ref") != "refs/heads/automation/canonical-update-state"
        or journal.get("schema_version") != "datapan.canonical-update-promotion-journal.v1"
        or journal.get("repository") != "StatPan/datapan-registry"
        or not isinstance(journal.get("records"), list)
    ):
        raise ValueError("promotion state ref or immutable journal does not match its registered owner")
    validate_promotion_journal_at_revision(root=root, revision=run["head_sha"], journal=journal)
    journal_helper = import_module("completeness_journal_identity", root / "scripts/canonical_update_pr.py")
    execution_head_registry, execution_manifest_sha256 = source_lfs_binding(root, run["head_sha"])
    no_op_results = [row for row in log_results if "already_canonical_generations" in row]
    no_op_generation = None
    selected_candidate_record: dict[str, Any] | None = None
    candidate_merge_commit: str | None = None
    candidate_available = False
    result_status = "execution-without-matched-candidate"
    if no_op_results:
        if len(no_op_results) != 1:
            raise ValueError("promotion log contains ambiguous no-op candidate summaries")
        output = no_op_results[0]
        already_canonical = output.get("already_canonical_generations")
        if (
            output.get("status") not in {
                "no-eligible-ready-processor-bundle", "already-canonical-payload",
                "skipped-already-canonical-processor-bundles",
            }
            or output.get("candidate_available") is not False
            or not isinstance(already_canonical, list)
        ):
            raise ValueError("promotion output does not record a valid no-candidate reconciliation outcome")
        generations: set[str] = set()
        for row in already_canonical:
            if not isinstance(row, dict):
                raise ValueError("promotion no-op log contains a malformed generation row")
            generation_id = row.get("generation_id")
            counts = (row.get("pending_count"), row.get("detail_retry_count"), row.get("detail_unattempted_count"))
            if (
                not isinstance(generation_id, str) or not SHA256_RE.fullmatch(generation_id)
                or generation_id in generations
                or row.get("reason") != "already_canonical_payload"
                or row.get("candidate_available") is not False
                or not isinstance(row.get("registry_sha256"), str)
                or not SHA256_RE.fullmatch(row["registry_sha256"])
                or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts)
            ):
                raise ValueError("promotion no-op log contains conflicting or malformed generation identities")
            generations.add(generation_id)
        generation_rows = [
            row for row in already_canonical
            if row["generation_id"] == processor.get("generation_id")
        ]
        if len(generation_rows) > 1:
            raise ValueError("promotion no-op result repeats the exact B generation")
        if generation_rows:
            row = generation_rows[0]
            if processor.get("status") not in {"ready", "no-change"}:
                raise ValueError("promotion no-op names a B generation without a reviewable composed output")
            if (
                row.get("registry_sha256") != processor.get("candidate_sha256")
                or row.get("registry_sha256") != execution_head_registry.get("sha256")
                or processor.get("candidate_bytes") != execution_head_registry.get("bytes")
                or row.get("pending_count") != processor.get("pending_count")
                or row.get("detail_retry_count") != processor.get("detail_retry_count")
                or row.get("detail_unattempted_count") != processor.get("detail_unattempted_count")
            ):
                raise ValueError("promotion no-op does not bind the exact B candidate, byte count, and outcome")
            no_op_generation = processor["generation_id"]
            result_status = "already-canonical-noop"
        else:
            result_status = "no-op-without-matched-generation"
    else:
        reconciliations = [
            row for row in log_results
            if row.get("status") == "promotion-prs-reconciled"
            and row.get("run_url") == f"https://github.com/StatPan/datapan-registry/actions/runs/{run['id']}/attempts/{run['run_attempt']}"
        ]
        if len(reconciliations) != 1:
            raise ValueError("promotion log lacks an exact successful reconciliation summary")
        matching_merged: list[tuple[dict[str, Any], str]] = []
        matching_prepared: list[dict[str, Any]] = []
        matching_terminal: list[dict[str, Any]] = []
        merged_lifecycle_states = {
            "merged", "publication-pending", "published", "read-back-confirmed",
        }
        for row in journal["records"]:
            candidate = row.get("candidate") if isinstance(row, dict) else None
            pr = row.get("pr") if isinstance(row, dict) else None
            if not isinstance(candidate, dict) or not isinstance(pr, dict):
                continue
            if row.get("superseded_by") is not None:
                continue
            if (
                candidate.get("repository", "").casefold() == "statpan/datapan-registry"
                and candidate.get("source_id") == processor.get("source_id", "data_go_kr")
                and candidate.get("scope") == processor.get("source_scope")
                and candidate.get("generation_id") == processor["generation_id"]
                and candidate.get("registry_path") == processor.get("candidate_path", "data/data-go-kr.registry.json")
                and candidate.get("registry_sha256") == processor.get("candidate_sha256")
                and candidate.get("registry_bytes") == processor.get("candidate_bytes")
                and candidate.get("composition_receipt_sha256") == processor.get("composition_sha256")
            ):
                row_status = row.get("status")
                if row_status in merged_lifecycle_states or (
                    row_status in {"failed", "closed"}
                    and pr.get("state") == "merged"
                    and isinstance(pr.get("merge_commit_sha"), str)
                ):
                    merge = pr["merge_commit_sha"]
                    assert_main_ancestor(root, merge, "promotion journal candidate merge commit")
                    merged_artifact, merged_manifest_sha256 = source_lfs_binding(root, merge)
                    if (
                        candidate.get("manifest_sha256") == merged_manifest_sha256
                        and merged_artifact.get("sha256") == candidate.get("registry_sha256")
                        and merged_artifact.get("bytes") == candidate.get("registry_bytes")
                    ):
                        matching_merged.append((row, merge))
                    else:
                        raise ValueError("merged promotion lifecycle record does not bind its exact release manifest and Registry payload")
                elif row_status in {"prepared", "pending-review"}:
                    matching_prepared.append(row)
                elif row_status in {"failed", "closed"}:
                    # A pre-merge terminal record still identifies a real B
                    # generation transition. Preserve it as a terminal C fact,
                    # but never treat it as a merged/current candidate.
                    matching_terminal.append(row)
        active_candidate_rows = [*matching_merged, *((row, "") for row in matching_prepared)]
        candidate_keys = [journal_helper.candidate_key(row) for row, _merge in active_candidate_rows]
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ValueError("promotion journal repeats one complete candidate key")
        if len(active_candidate_rows) > 1:
            raise ValueError("promotion journal has multiple active candidates for the exact B composition")
        if matching_merged:
            selected_candidate_record, candidate_merge_commit = matching_merged[-1]
            candidate_available = True
            lifecycle_status = selected_candidate_record.get("status")
            result_status = (
                "merged-candidate-reconciled" if lifecycle_status == "merged"
                else f"merged-candidate-{lifecycle_status}"
            )
        elif matching_prepared:
            selected_candidate_record = matching_prepared[-1]
            candidate_available = True
            result_status = f"candidate-{selected_candidate_record['status']}"
        elif matching_terminal:
            if len(matching_terminal) > 1:
                raise ValueError("promotion journal repeats terminal records for the exact B composition")
            selected_candidate_record = matching_terminal[0]
            result_status = f"candidate-{selected_candidate_record['status']}"
        else:
            result_status = "reconciled-without-matched-generation"
    return {
        "run_id": str(run["id"]), "attempt": run["run_attempt"], "workflow_id": run["workflow_id"],
        "head_sha": run["head_sha"], "main_ancestry_checked_tip": committed_main,
        "state_commit": ref["object"]["sha"],
        "state_tree_sha": indexed_json(roles, resolved, "promotion_state_tree")["sha"],
        "status": result_status,
        "candidate_available": candidate_available,
        "candidate_lifecycle_status": (
            selected_candidate_record.get("status")
            if isinstance(selected_candidate_record, dict) else None
        ),
        "candidate_acknowledgement_statuses": (
            [
                acknowledgement.get("status")
                for acknowledgement in selected_candidate_record.get("acknowledgements", [])
                if isinstance(acknowledgement, dict)
            ]
            if isinstance(selected_candidate_record, dict) else []
        ),
        "already_canonical_generation_id": no_op_generation,
        "candidate_record": copy.deepcopy(selected_candidate_record),
        "candidate_key": list(journal_helper.candidate_key(selected_candidate_record)) if selected_candidate_record else None,
        "processor_generation_matches": (
            no_op_generation == processor.get("generation_id")
            or isinstance(selected_candidate_record, dict)
            and selected_candidate_record.get("candidate", {}).get("generation_id") == processor.get("generation_id")
        ),
        "candidate_merge_commit": candidate_merge_commit,
        "candidate_current_subject_matches": (
            no_op_generation is not None and current_registry.get("sha256") == processor.get("candidate_sha256")
            or candidate_merge_commit is not None and current_registry.get("sha256") == processor.get("candidate_sha256")
        ),
        "execution_head_registry_sha256": execution_head_registry.get("sha256"),
        "execution_head_registry_bytes": execution_head_registry.get("bytes"),
        "execution_head_manifest_sha256": execution_manifest_sha256,
        "journal_raw": state_bytes["promotion_journal_blob_api"],
        "journal_sha256": sha256_bytes(state_bytes["promotion_journal_blob_api"]),
        "journal_git_blob_sha": indexed_json(roles, resolved, "promotion_journal_blob_api")["sha"],
        "journal_record_count": len(journal["records"]),
        "last_journal_status": journal["records"][-1].get("status"),
        "journal_records": copy.deepcopy(journal["records"]),
        "journal_records": copy.deepcopy(journal["records"]),
        "evidence": [
            artifact(input_path(roles["pipeline_run"]), read_bytes(resolved[roles["pipeline_run"]["input_id"]])),
            artifact(input_path(log_item), log_bytes),
            artifact(input_path(roles["promotion_journal_blob_api"]), state_bytes["promotion_journal_blob_api"]),
        ],
    }


def import_health_persister_at_revision(root: pathlib.Path, revision: str) -> Any:
    source = git_read_only(root, ["show", f"{revision}:scripts/persist-upstream-catalogue-health.py"])
    module = __import__("types").ModuleType(f"health_persister_{revision}")
    module.__file__ = str(root / "scripts/persist-upstream-catalogue-health.py")
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def expected_health_post_state(
    *, root: pathlib.Path, workflow_head: str, pre_state: dict[str, Any], receipt: dict[str, Any],
) -> dict[str, Any]:
    persister = import_health_persister_at_revision(root, workflow_head)
    policy_bytes = git_read_only(root, ["show", f"{workflow_head}:policy/upstream-catalogue-health.json"])
    policy = json.loads(policy_bytes)
    if sha256_bytes(policy_bytes) != receipt.get("policy_sha256"):
        raise ValueError("health receipt policy digest differs from its exact workflow-head policy")
    state = copy.deepcopy(pre_state)
    persister.merge_observations(state, receipt)
    persister.merge_faults(
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
        selected = persister.choose_last_good(prior, candidate)
        if selected is not None:
            last_good[source["source_id"]] = selected
    state["updated_at"] = receipt["evaluated_at"]
    state["latest_receipt"] = {
        "path": f"receipts/{persister.receipt_file_name(receipt)}",
        "sha256": receipt["receipt_sha256"],
    }
    overall_states = {row.get("overall") for row in receipt.get("sources", [])}
    state["state_status"] = (
        "blocked" if "blocked" in overall_states
        else "degraded" if "degraded" in overall_states
        else "healthy"
    )
    return persister.seal(state, "state_sha256")


def parse_health_owned_state_ref(raw: bytes, *, label: str, expected_ref: str) -> dict[str, str]:
    """Parse the exact one-line state ref locator embedded in a Health bundle."""
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(f"health archived {label} is not ASCII") from exc
    if not text.endswith("\n") or text.count("\n") != 1 or "\r" in text:
        raise ValueError(f"health archived {label} must contain one newline-terminated locator")
    fields = text[:-1].split("\t")
    if (
        len(fields) != 2
        or not re.fullmatch(r"[a-f0-9]{40}", fields[0])
        or fields[1] != expected_ref
    ):
        raise ValueError(f"health archived {label} has an invalid commit or owned ref locator")
    return {"commit_sha": fields[0], "ref": fields[1]}


def validate_health_stage_local(
    *, root: pathlib.Path, roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path],
    run: dict[str, Any], job: dict[str, Any], evaluation_epoch: str,
) -> dict[str, Any]:
    """Validate the complete Health receipt/archive/state transition without A/B/C joins."""
    metadata, archive_raw = validate_stage_artifact(
        stage="health", roles=roles, resolved=resolved, run=run, job=job,
        evaluation_epoch=evaluation_epoch,
    )
    archive = safe_zip_members(
        archive_raw, label="health execution bundle", max_archive=64 * 1024 * 1024,
        max_expanded=64 * 1024 * 1024,
    )
    expected_members = {
        "as-of.txt", "health-receipt.json", "checker-result.json", "health-ref",
        "health-state/state.json", "processor-ref",
        "promotion/reports/canonical-update-promotion-receipt.json", "promotion-ref",
    }
    if set(archive) != expected_members:
        raise ValueError("health execution bundle has a missing or unexpected member")
    try:
        receipt_raw = archive["health-receipt.json"]
        receipt = json.loads(receipt_raw)
        pre_state_raw = archive["health-state/state.json"]
        pre_state = json.loads(pre_state_raw)
        promotion_journal_raw = archive["promotion/reports/canonical-update-promotion-receipt.json"]
        promotion_journal = json.loads(promotion_journal_raw)
        checker_result = json.loads(archive["checker-result.json"])
        archive_as_of = archive["as-of.txt"].decode("ascii").strip()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("health execution archive contains malformed JSON or timestamp data") from exc
    processor_ref = parse_health_owned_state_ref(
        archive["processor-ref"], label="processor-ref",
        expected_ref="refs/heads/automation/upstream-catalogue-state",
    )
    promotion_ref = parse_health_owned_state_ref(
        archive["promotion-ref"], label="promotion-ref",
        expected_ref="refs/heads/automation/canonical-update-state",
    )
    persister = import_health_persister_at_revision(root, run["head_sha"])
    validate_schema_at_revision(
        root=root, revision=run["head_sha"],
        relative_path="schemas/datapan.upstream-catalogue-health.v1.schema.json",
        value=receipt, label="health receipt",
    )
    validate_schema_at_revision(
        root=root, revision=run["head_sha"],
        relative_path="schemas/datapan.upstream-catalogue-health-state.v1.schema.json",
        value=pre_state, label="health pre-state",
    )
    validate_schema_at_revision(
        root=root, revision=run["head_sha"],
        relative_path="schemas/datapan.canonical-update-promotion-journal.v1.schema.json",
        value=promotion_journal, label="health archived promotion journal",
    )
    if promotion_journal.get("repository") != "StatPan/datapan-registry":
        raise ValueError("health archived promotion journal repository is outside its registered owner")
    policy_raw = git_read_only(root, ["show", f"{run['head_sha']}:policy/upstream-catalogue-health.json"])
    policy = json.loads(policy_raw)
    validate_schema_at_revision(
        root=root, revision=run["head_sha"],
        relative_path="schemas/datapan.upstream-catalogue-health-policy.v1.schema.json",
        value=policy, label="health policy",
    )
    workflow_receipt = receipt.get("health_workflow", {})
    if (
        not persister.verify_seal(receipt, "receipt_sha256")
        or receipt.get("execution_mode") != "live"
        or receipt.get("repository") != "StatPan/datapan-registry"
        or receipt.get("evaluated_at") != archive_as_of
        or checker_result.get("receipt_sha256") != receipt.get("receipt_sha256")
        or checker_result.get("status") != "ok"
        or not persister.verify_seal(pre_state, "state_sha256")
        or str(workflow_receipt.get("run_id")) != str(run["id"])
        or workflow_receipt.get("run_attempt") != run["run_attempt"]
        or workflow_receipt.get("revision") != run["head_sha"]
        or not parse_time(receipt.get("evaluated_at"), "health receipt evaluation")
        <= parse_time(evaluation_epoch, "health execution evaluation epoch")
    ):
        raise ValueError("health receipt, pre-state, checker result, or workflow identity fails its sealed contract")

    health_ref = archive["health-ref"].decode("ascii").strip().split("\t")
    commit_api = indexed_json(roles, resolved, "health_state_commit")
    parent_shas = {
        parent.get("sha") for parent in commit_api.get("parents", []) if isinstance(parent, dict)
    }
    if (
        len(health_ref) != 2
        or health_ref[1] != "refs/heads/automation/upstream-catalogue-health-state"
        or commit_api.get("sha") != indexed_json(roles, resolved, "health_state_ref").get("object", {}).get("sha")
        or health_ref[0] not in parent_shas
    ):
        raise ValueError("health post-persist commit does not descend from its exact archived pre-state ref")

    receipt_file = persister.receipt_file_name(receipt)
    state_bytes = validate_state_tree(
        stage="health", roles=roles, resolved=resolved,
        ref_role="health_state_ref", commit_role="health_state_commit", tree_role="health_state_tree",
        content_roles={},
        blob_roles={
            "health_state_blob_api": "health/upstream-catalogue/state.json",
            "health_receipt_blob_api": f"health/upstream-catalogue/receipts/{receipt_file}",
        },
    )
    if state_bytes["health_receipt_blob_api"] != receipt_raw:
        raise ValueError("durable Health receipt tree member differs from the receipt in its workflow archive")
    post_state = json.loads(state_bytes["health_state_blob_api"])
    validate_schema_at_revision(
        root=root, revision=run["head_sha"],
        relative_path="schemas/datapan.upstream-catalogue-health-state.v1.schema.json",
        value=post_state, label="durable Health post-state",
    )
    if not persister.verify_seal(post_state, "state_sha256"):
        raise ValueError("durable Health post-state seal is invalid")
    expected_post_state = expected_health_post_state(
        root=root, workflow_head=run["head_sha"], pre_state=pre_state, receipt=receipt,
    )
    if post_state != expected_post_state:
        raise ValueError("durable Health post-state differs from the pure replay of the trusted persister transition")
    return {
        "metadata": metadata, "archive_raw": archive_raw, "archive": archive,
        "receipt_raw": receipt_raw, "receipt": receipt,
        "pre_state_raw": pre_state_raw, "pre_state": pre_state,
        "promotion_journal_raw": promotion_journal_raw,
        "promotion_journal": promotion_journal,
        "processor_ref": processor_ref, "promotion_ref": promotion_ref,
        "checker_result": checker_result, "persister": persister,
        "state_bytes": state_bytes, "post_state": post_state,
        "commit_api": commit_api,
    }


def validate_health_stage(
    *, root: pathlib.Path, roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path],
    run: dict[str, Any], job: dict[str, Any], processor: dict[str, Any],
    promotion: dict[str, Any], source: dict[str, Any], current_registry: dict[str, Any],
    evaluation_epoch: str,
) -> dict[str, Any]:
    local = validate_health_stage_local(
        root=root, roles=roles, resolved=resolved, run=run, job=job,
        evaluation_epoch=evaluation_epoch,
    )
    archive_raw = local["archive_raw"]
    archive = local["archive"]
    receipt_raw = local["receipt_raw"]
    receipt = local["receipt"]
    pre_state_raw = local["pre_state_raw"]
    pre_state = local["pre_state"]
    persister = local["persister"]
    state_bytes = local["state_bytes"]
    post_state = local["post_state"]
    commit_api = local["commit_api"]
    if (
        local["processor_ref"]["commit_sha"] != processor["state_commit"]
        or local["promotion_ref"]["commit_sha"] != promotion["state_commit"]
        or local["promotion_journal_raw"] != promotion.get("journal_raw")
    ):
        raise ValueError("health archived producer refs do not bind the exact B/C workflow chain")
    source_rows = [row for row in receipt.get("sources", []) if isinstance(row, dict) and row.get("source_id") == "data_go_kr"]
    observations = post_state.get("observations_by_source", {}).get("data_go_kr", [])
    last_good = post_state.get("last_good_by_source", {}).get("data_go_kr")
    canonical = source_rows[0].get("canonical", {}) if len(source_rows) == 1 else {}
    health_observation = source_rows[0].get("observation", {}) if len(source_rows) == 1 else {}
    health_processor = source_rows[0].get("processor", {}) if len(source_rows) == 1 else {}
    if len(source_rows) != 1 or not isinstance(observations, list) or len(observations) > 64:
        raise ValueError("Health receipt must contain one source summary and its bounded observation history")

    def observation_identity(value: Any, label: str) -> tuple[str, str, str] | None:
        if not isinstance(value, dict):
            return None
        producer_run_id = value.get("producer_run_id")
        observed_at = value.get("observed_at")
        evidence_sha256 = value.get("refresh_evidence_sha256")
        if producer_run_id is None and observed_at is None and evidence_sha256 is None:
            return None
        if (
            not isinstance(producer_run_id, str) or not producer_run_id.isdigit()
            or not isinstance(observed_at, str)
            or not isinstance(evidence_sha256, str) or not SHA256_RE.fullmatch(evidence_sha256)
        ):
            raise ValueError(f"Health {label} has an incomplete source-observation identity")
        parse_time(observed_at, f"Health {label} observed_at")
        return producer_run_id, observed_at, evidence_sha256

    expected_observation = (
        source["run_id"], source["observed_at"], source["refresh_evidence_sha256"],
    )
    health_observation_identity = observation_identity(health_observation, "source observation")
    if health_observation_identity is not None and health_observation_identity != expected_observation:
        raise ValueError("Health source observation differs from the exact supplied A observation")
    if health_observation.get("state") == "fresh" and health_observation_identity != expected_observation:
        raise ValueError("Health fresh observation does not bind the exact supplied A observation")

    processor_observation = observation_identity(health_processor.get("last_observation"), "processor observation")
    selected_processor_observation = observation_identity(processor.get("source_observation"), "selected processor observation")
    if processor_observation is not None and processor_observation != selected_processor_observation:
        raise ValueError("Health processor checkpoint selects a different source observation from the exact supplied B generation")
    if health_processor.get("generation_id") != processor["generation_id"]:
        raise ValueError("Health processor state differs from the exact supplied B generation")
    if health_processor.get("state") != processor["status"]:
        raise ValueError("Health processor outcome state differs from the exact supplied B checkpoint")
    if health_processor.get("checkpoint_sha256") != processor.get("checkpoint_sha256"):
        raise ValueError("Health processor checkpoint digest differs from the exact supplied B checkpoint")
    if health_processor.get("candidate_sha256") != processor.get("source_candidate_sha256"):
        raise ValueError("Health processor source candidate differs from the exact supplied B generation input")
    if health_processor.get("outcome") != processor.get("outcome"):
        raise ValueError("Health processor outcome differs from the exact supplied B checkpoint outcome")
    health_artifact = health_processor.get("output_artifact")
    if not isinstance(health_artifact, dict) or (
        str(health_artifact.get("run_id")) != processor["run_id"]
        or str(health_artifact.get("artifact_id")) != processor["artifact_id"]
        or health_artifact.get("bundle_manifest_sha256") != processor.get("output_bundle_sha256")
    ):
        raise ValueError("Health processor artifact locator differs from the exact supplied B artifact")
    health_outputs = health_processor.get("output_digests")
    composed_outputs = [
        row for row in health_outputs if isinstance(row, dict)
        and row.get("path") == "composed-candidate.registry.json"
    ] if isinstance(health_outputs, list) else []
    if processor.get("candidate_sha256") is not None and (
        len(composed_outputs) != 1
        or composed_outputs[0].get("sha256") != processor["candidate_sha256"]
        or composed_outputs[0].get("bytes") != processor["candidate_bytes"]
    ):
        raise ValueError("Health processor output inventory differs from the exact supplied B composition")

    # Health's inspected main and last-good are time-specific producer facts.
    # Authenticate the main subject against its own immutable Git tree, but do
    # not equate it with the rollup's later candidate or B's run-head.
    main = canonical.get("main")
    if not isinstance(main, dict):
        raise ValueError("Health receipt lacks its inspected canonical main subject")
    main_revision = main.get("revision")
    if not isinstance(main_revision, str) or not re.fullmatch(r"[a-f0-9]{40}", main_revision):
        raise ValueError("Health inspected main revision is malformed")
    assert_main_ancestor(root, main_revision, "Health inspected canonical main revision")
    main_artifact, main_manifest_sha256 = source_lfs_binding(root, main_revision)
    if (
        main.get("manifest_sha256") != main_manifest_sha256
        or main.get("registry_path") != main_artifact.get("path")
        or main.get("registry_sha256") != main_artifact.get("sha256")
        or main.get("registry_bytes") != main_artifact.get("bytes")
    ):
        raise ValueError("Health inspected main identity does not match its exact committed manifest and LFS pointer")

    # The checker's verified flag is an internal relationship claim. Validate
    # it with the checker implementation at the Health execution revision;
    # if present, never accept the flag alone as B or C evidence.
    health_candidate = canonical.get("already_canonical_candidate")
    candidate_relation_valid = False
    if health_candidate is not None:
        verified_relation = persister.verified_already_canonical_relation(source_rows[0], receipt)
        if verified_relation is None:
            raise ValueError("Health already-canonical candidate relation fails its producer-owned inventory contract")
        candidate_relation_valid = True
        if health_candidate.get("generation_id") == processor["generation_id"] and (
            health_candidate.get("source_id") != processor.get("source_id")
            or health_candidate.get("checkpoint_sha256") != processor.get("checkpoint_sha256")
            or health_candidate.get("processor_run_id") != processor["run_id"]
            or health_candidate.get("processor_run_attempt") != processor["attempt"]
            or health_candidate.get("artifact_id") != processor["artifact_id"]
            or health_candidate.get("output_bundle_sha256") != processor.get("output_bundle_sha256")
            or health_candidate.get("composed_registry_sha256") != processor.get("candidate_sha256")
            or health_candidate.get("composed_registry_bytes") != processor.get("candidate_bytes")
        ):
            raise ValueError("Health already-canonical relation does not bind the exact supplied B generation")
    if promotion.get("already_canonical_generation_id") == processor["generation_id"]:
        if not candidate_relation_valid or health_candidate.get("generation_id") != processor["generation_id"]:
            raise ValueError("Health omitted the exact B relationship reported by the C no-op result")

    promotion_execution = canonical.get("promotion_execution", {})
    promotion_success = promotion_execution.get("latest_successful_execution_run") if isinstance(promotion_execution, dict) else None
    promotion_execution_matches = False
    if isinstance(promotion_success, dict) and str(promotion_success.get("run_id")) == promotion["run_id"]:
        promotion_execution_matches = (
            str(promotion_success.get("run_id")) == promotion["run_id"]
            and promotion_success.get("run_attempt") == promotion["attempt"]
            and promotion_success.get("head_sha") == promotion["head_sha"]
            and promotion_success.get("workflow_id") == promotion["workflow_id"]
        )
        if not promotion_execution_matches:
            raise ValueError("Health promotion execution summary conflicts with the exact supplied C attempt")

    replayed_last_good = last_good
    payload_matches_current = (
        main_artifact.get("sha256") == current_registry.get("sha256")
        and main_artifact.get("bytes") == current_registry.get("bytes")
    )
    manifest_matches_current = main_manifest_sha256 == current_registry.get("release_manifest_sha256")
    return {
        "run_id": str(run["id"]), "attempt": run["run_attempt"], "workflow_id": run["workflow_id"],
        "head_sha": run["head_sha"], "state_commit": commit_api["sha"],
        "receipt_sha256": sha256_bytes(receipt_raw), "receipt_seal": receipt["receipt_sha256"],
        "pre_state_sha256": sha256_bytes(pre_state_raw), "pre_state_seal": pre_state["state_sha256"],
        "post_state_sha256": sha256_bytes(state_bytes["health_state_blob_api"]),
        "post_state_seal": post_state["state_sha256"], "observation_count": len(observations),
        "source_observation_matches": health_observation_identity == expected_observation,
        "processor_observation_matches": processor_observation == selected_processor_observation,
        "candidate_relation_valid": candidate_relation_valid,
        "promotion_execution_matches": promotion_execution_matches,
        "health_main_revision": main_revision,
        "health_main_manifest_sha256": main_manifest_sha256,
        "health_main_registry_sha256": main_artifact["sha256"],
        "health_main_registry_bytes": main_artifact["bytes"],
        "health_main_payload_matches_current": payload_matches_current,
        "health_main_release_manifest_matches_current": manifest_matches_current,
        "health_main_matches_current_subject": payload_matches_current and manifest_matches_current,
        "last_good": copy.deepcopy(replayed_last_good),
        "last_good_source_sha": replayed_last_good.get("source_sha") if isinstance(replayed_last_good, dict) else None,
        "summary": copy.deepcopy(receipt.get("summary", {})),
        "evidence": [
            artifact(input_path(roles["pipeline_run"]), read_bytes(resolved[roles["pipeline_run"]["input_id"]])),
            artifact(input_path(roles["pipeline_artifact_archive"]), archive_raw),
            artifact(input_path(roles["health_state_blob_api"]), state_bytes["health_state_blob_api"]),
            artifact(input_path(roles["health_receipt_blob_api"]), state_bytes["health_receipt_blob_api"]),
        ],
    }


def validate_denominator_attestation(
    *,
    root: pathlib.Path,
    scope: dict[str, Any],
    scoped_inputs: list[dict[str, Any]],
    resolved: dict[str, pathlib.Path],
    proof: dict[str, Any],
    baseline_registry_bytes: bytes | None = None,
) -> dict[str, Any]:
    item = single_role(scoped_inputs, "authoritative_denominator")
    assert item is not None
    if item["namespace"] not in {"historical_admitted", "live_operational"}:
        raise ValueError("authoritative denominator cannot be fixture or repository-local inventory evidence")
    verify_main_committed_input(root=root, item=item, path=resolved[item["input_id"]], label="authoritative denominator")
    denominator = object_at(resolved[item["input_id"]], "authoritative denominator")
    if (
        denominator.get("schema_version") != "datapan.completeness-proof-identities.v1"
        or denominator.get("scope_id") != scope["scope_id"]
        or denominator.get("resource_kind") != scope["resource_kind"]
        or denominator.get("identity_algorithm") != scope["identity_algorithm"]
    ):
        raise ValueError("authoritative denominator scope or identity contract differs")
    validate_schema(
        denominator,
        root / "schemas/datapan.completeness-proof-identities.v1.schema.json",
        "authoritative denominator",
    )
    if denominator.get("source_sha256") != proof["source_snapshot"]["sha256"]:
        raise ValueError("authoritative denominator is not bound to the proof source bytes")
    identities = denominator.get("identities")
    if not isinstance(identities, list) or not identities or any(not isinstance(value, str) or not value for value in identities):
        raise ValueError("authoritative denominator must include a nonempty exact identity list")
    if len(identities) != len(set(identities)):
        raise ValueError("authoritative denominator identity set contains duplicates")
    source_item = single_role(scoped_inputs, "authoritative_source_snapshot")
    assert source_item is not None
    source_path = resolved[source_item["input_id"]]
    source_manifest_bytes = receipt_path_bytes(source_path, source_item)
    source_identities = derive_scope_identities(
        root=root, scope=scope, source_path=source_path,
        source_relative_path=source_item["path"],
        source_manifest_bytes=source_manifest_bytes,
        source_revision=proof["source_snapshot"].get("revision"),
        registry_bytes=baseline_registry_bytes,
    )
    if sorted(identities) != source_identities:
        raise ValueError("authoritative denominator identities differ from the registered source adapter")
    identity_set_sha = sha256_bytes(
        json.dumps(sorted(identities), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if denominator.get("identity_set_sha256") != identity_set_sha:
        raise ValueError("authoritative denominator identity-set digest differs from its exact IDs")
    if len(identities) != proof["denominator"]["value"]:
        raise ValueError("proof denominator count differs from the admitted exact identity set")
    if proof["denominator"]["type"] == "local_observed_identity_set":
        raise ValueError("local inventory cannot serve as an authoritative proof denominator")
    if denominator.get("authority_owner") != scope["identity_owner"]:
        raise ValueError("authoritative denominator owner differs from the registered identity owner")
    summary = denominator.get("reconciliation")
    if not isinstance(summary, dict):
        raise ValueError("authoritative denominator lacks exact reconciliation evidence")
    if summary != proof["reconciliation"]:
        raise ValueError("proof reconciliation does not match the admitted identity-set reconciliation")
    observed_at = denominator.get("observed_at")
    if not isinstance(observed_at, str):
        raise ValueError("authoritative denominator observation time is missing")
    parse_time(observed_at, "authoritative denominator observed_at")
    if observed_at != proof["source_snapshot"]["observed_at"]:
        raise ValueError("authoritative denominator and source snapshot observation times differ")
    denominator_bytes = receipt_path_bytes(resolved[item["input_id"]], item)
    if item["sha256"] != sha256_bytes(denominator_bytes):
        raise ValueError("authoritative denominator bytes changed")
    return denominator


def derive_scope_identities(
    *, root: pathlib.Path, scope: dict[str, Any], source_path: pathlib.Path,
    source_relative_path: str | None = None,
    source_manifest_bytes: bytes | None = None,
    source_revision: str | None = None,
    registry_bytes: bytes | None = None,
) -> list[str]:
    """Extract identities with the source adapter already registered for a scope."""
    inventory = scope["inventory"]
    logical_path = normalize_relative_path(
        source_relative_path if source_relative_path is not None else source_path.relative_to(root).as_posix(),
        "authoritative source identity path",
    ).as_posix()
    if inventory["kind"] == "operation_manifest" and scope["source_id"] == "data_go_kr":
        if logical_path != inventory["path"]:
            raise ValueError("data.go.kr operation proof must use the registered #605 operation-manifest source")
        if registry_bytes is None:
            manifest = validate_data_go_operation_manifest(root, source_path)
        else:
            if source_revision is None or not SHA256_RE.fullmatch(sha256_bytes(registry_bytes)):
                raise ValueError("historical #605 proof validation lacks its exact source revision")
            projected = project_registered_data_go_operation_manifest(
                root=root, revision=source_revision, registry_bytes=registry_bytes,
            )
            if source_manifest_bytes is None or projected != source_manifest_bytes:
                raise ValueError("historical operation manifest is not the exact pinned #605 projection of its source Registry")
            manifest = json.loads(projected)
        identities = [item.get("operation_id") for item in manifest.get("operations", [])]
    elif inventory["kind"] == "operation_denominator":
        if logical_path != inventory["path"]:
            raise ValueError("operation proof must use its registered source denominator path")
        denominator = validate_operation_denominator(root, source_path, scope["source_id"])
        identities = [item.get("operation_id") for item in denominator.get("operations", [])]
    else:
        raise ValueError(f"no existing authoritative identity adapter is registered for {scope['scope_id']}")
    if not identities or any(not isinstance(value, str) or not value for value in identities):
        raise ValueError(f"registered source adapter produced no exact identities for {scope['scope_id']}")
    if len(identities) != len(set(identities)):
        raise ValueError(f"registered source adapter produced duplicate identities for {scope['scope_id']}")
    return sorted(identities)


def validate_committed_release_artifact(
    *, root: pathlib.Path, revision: str, path: str, expected_sha256: str,
    expected_bytes: int, label: str, expected_content: bytes | None = None,
) -> dict[str, Any]:
    """Bind a release-manifest artifact to a main commit, including LFS objects.

    Git stores an LFS pointer rather than the large payload. The manifest row
    and pointer OID/size therefore bind the full payload; hashing `git show`
    output as if it were the materialized file would be incorrect.
    """
    assert_main_ancestor(root, revision, f"{label} Registry commit")
    manifest_raw = git_read_only(root, ["show", f"{revision}:manifest.json"])
    manifest = json.loads(manifest_raw)
    rows = [item for item in manifest.get("artifacts", []) if item.get("path") == path]
    if len(rows) != 1:
        raise ValueError(f"{label} must be listed exactly once in the bound release manifest")
    row = rows[0]
    if row.get("sha256") != expected_sha256 or row.get("bytes") != expected_bytes:
        raise ValueError(f"{label} digest or size differs from the bound release manifest")
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 0:
        raise ValueError(f"{label} byte count is invalid")
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError(f"{label} SHA-256 is invalid")
    committed = git_read_only(root, ["show", f"{revision}:{path}"])
    pointer_match = re.fullmatch(
        r"version https://git-lfs.github.com/spec/v1\n"
        r"oid sha256:([a-f0-9]{64})\n"
        r"size ([0-9]+)\n?",
        committed.decode("ascii", "strict"),
    ) if committed.startswith(b"version https://git-lfs.github.com/spec/v1\n") else None
    if pointer_match:
        pointer_sha, pointer_size = pointer_match.groups()
        if (pointer_sha, int(pointer_size)) != (expected_sha256, expected_bytes):
            raise ValueError(f"{label} committed LFS pointer differs from its full payload identity")
    elif (len(committed), sha256_bytes(committed)) != (expected_bytes, expected_sha256):
        raise ValueError(f"{label} committed bytes differ from its full payload identity")
    if expected_content is not None:
        if (len(expected_content), sha256_bytes(expected_content)) != (expected_bytes, expected_sha256):
            raise ValueError(f"{label} indexed payload differs from its expected identity")
        if not pointer_match and committed != expected_content:
            raise ValueError(f"{label} committed file bytes differ from the exact indexed payload")
    return {
        "path": path, "bytes": expected_bytes, "sha256": expected_sha256,
        "release_manifest_sha256": sha256_bytes(manifest_raw),
    }


def validate_native_distribution_inventory(
    *, manifest: dict[str, Any], manifest_raw: bytes, pointer: dict[str, Any],
    receipt: dict[str, Any], source_script_raw: bytes, workflow_raw: bytes,
) -> dict[str, Any]:
    """Bind every pointer member to the reviewed native all-artifact verifier."""
    source_binding = receipt.get("source_binding")
    publication = receipt.get("publication")
    verification = receipt.get("anonymous_verification")
    if not isinstance(source_binding, dict) or not isinstance(publication, dict) or not isinstance(verification, dict):
        raise ValueError("native distribution receipt lacks its source, publication, or verification object")
    contract_key = (sha256_bytes(source_script_raw), sha256_bytes(workflow_raw))
    extra_paths = NATIVE_DISTRIBUTION_CONTRACTS.get(contract_key)
    if extra_paths is None:
        raise ValueError("native publisher source/workflow pair has no reviewed all-artifact verifier contract")
    try:
        workflow_text = workflow_raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ValueError("native publisher workflow is not UTF-8") from exc
    extra_specs = re.findall(r"(?m)^\s*--extra\s+([^\s]+)", workflow_text)
    parsed_extra_paths: list[str] = []
    for spec in extra_specs:
        path, separator, _staged_path = spec.partition("=")
        if not separator:
            raise ValueError("native publisher workflow contains a malformed distribution extra")
        parsed_extra_paths.append(normalize_relative_path(path, "native distribution extra path").as_posix())
    if len(parsed_extra_paths) != len(set(parsed_extra_paths)) or set(parsed_extra_paths) != set(extra_paths):
        raise ValueError("native publisher workflow extras differ from the reviewed distribution contract")

    manifest_rows = manifest.get("artifacts")
    pointer_rows = pointer.get("artifacts")
    if (
        not isinstance(manifest_rows, list) or not manifest_rows
        or not isinstance(manifest.get("artifact_count"), int)
        or isinstance(manifest.get("artifact_count"), bool)
        or manifest.get("artifact_count") != len(manifest_rows)
        or not isinstance(pointer_rows, list) or not pointer_rows
        or isinstance(pointer.get("artifact_count"), bool)
        or not isinstance(pointer.get("artifact_count"), int)
        or pointer.get("artifact_count") != len(pointer_rows)
    ):
        raise ValueError("native distribution source manifest or pointer count is incomplete")
    if (
        publication.get("artifacts") != len(pointer_rows)
        or isinstance(publication.get("artifacts"), bool)
        or verification.get("artifacts") != len(pointer_rows)
        or isinstance(verification.get("artifacts"), bool)
    ):
        raise ValueError("native publisher receipt counts do not equal the complete pointer inventory")

    manifest_by_path: dict[str, tuple[str, int, str]] = {}
    pointer_by_path: dict[str, tuple[str, int, str]] = {}

    def normalize_rows(rows: list[Any], *, label: str, target: dict[str, tuple[str, int, str]]) -> None:
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{label} contains a non-object artifact row")
            path_value = row.get("path")
            normalized = normalize_relative_path(path_value, f"{label} artifact path").as_posix()
            if normalized != path_value or normalized in target:
                raise ValueError(f"{label} has a duplicate or non-canonical artifact path")
            kind = row.get("kind")
            size = row.get("bytes")
            digest = row.get("sha256")
            if (
                not isinstance(kind, str) or not kind
                or isinstance(size, bool) or not isinstance(size, int) or size < 1
                or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
            ):
                raise ValueError(f"{label} artifact identity is malformed")
            target[normalized] = (kind, size, digest)

    normalize_rows(manifest_rows, label="source release manifest", target=manifest_by_path)
    normalize_rows(pointer_rows, label="immutable distribution pointer", target=pointer_by_path)
    if "manifest.json" in manifest_by_path or "manifest.json" in pointer_by_path:
        raise ValueError("release manifest must be represented separately from distribution artifacts")
    pointer_manifest = pointer.get("release_manifest")
    if (
        not isinstance(pointer_manifest, dict)
        or pointer_manifest.get("path") != "manifest.json"
        or pointer_manifest.get("kind") != "release_manifest"
        or pointer_manifest.get("bytes") != len(manifest_raw)
        or pointer_manifest.get("sha256") != sha256_bytes(manifest_raw)
    ):
        raise ValueError("immutable distribution pointer does not bind the exact source release manifest")

    if set(manifest_by_path) - set(pointer_by_path):
        raise ValueError("immutable distribution pointer omits source-manifest artifacts")
    for path, identity in manifest_by_path.items():
        if pointer_by_path.get(path) != identity:
            raise ValueError(f"immutable distribution pointer changes a source-manifest artifact: {path}")
    actual_extras = set(pointer_by_path) - set(manifest_by_path)
    if actual_extras != set(extra_paths):
        raise ValueError("immutable distribution pointer has missing or unreviewed workflow extras")
    if any(pointer_by_path[path][0] != "distribution_asset" for path in actual_extras):
        raise ValueError("native distribution workflow extra has an unexpected artifact kind")
    return {
        "source_manifest_artifacts": len(manifest_by_path),
        "distribution_artifacts": len(pointer_by_path),
        "verified_artifacts": [
            {"path": path, "kind": identity[0], "bytes": identity[1], "sha256": identity[2]}
            for path, identity in sorted(pointer_by_path.items())
        ],
        "workflow_extras": sorted(actual_extras),
        "verifier_contract_sha256": {
            "source_script": contract_key[0], "workflow": contract_key[1],
        },
    }


def validate_native_publisher_attempt(
    *, root: pathlib.Path, run: dict[str, Any], jobs: dict[str, Any],
    artifact_response: dict[str, Any], archive: bytes, receipt_raw: bytes,
    binding_raw: bytes, evaluation_epoch: str,
) -> dict[str, Any]:
    """Validate the current native publisher contract with #716 pure checks."""
    validator = import_module("native_publication_ack_validator", root / "scripts/recover-canonical-publication-ack.py")
    contract = PIPELINE_WORKFLOWS["publisher"]
    try:
        run_id, attempt, head_sha = validator.validate_workflow_run(
            run, repository="StatPan/datapan-registry", repository_id=1278568329,
            workflow_path=contract["workflow_path"], workflow_id=contract["workflow_id"],
            default_branch="main", required_event="workflow_dispatch",
        )
        job_total = jobs.get("total_count")
        if (
            isinstance(job_total, bool) or not isinstance(job_total, int) or job_total != 1
            or not isinstance(jobs.get("jobs"), list) or len(jobs["jobs"]) != 1
        ):
            raise validator.RecoveryError("publisher_jobs_incomplete_or_over_limit")
        job_status, job_started, job_completed = validator.validate_job_and_steps(
            jobs, run_id=run_id, attempt=attempt, head_sha=head_sha,
        )
        if job_status != "publishing":
            raise validator.RecoveryError("publisher_non_publishing_validation")
        artifact_total = artifact_response.get("total_count")
        artifacts = artifact_response.get("artifacts")
        if (
            isinstance(artifact_total, bool) or not isinstance(artifact_total, int)
            or not isinstance(artifacts, list) or artifact_total != len(artifacts)
        ):
            raise validator.RecoveryError("publication_artifact_list_incomplete")
        receipt_artifacts = [
            row for row in artifacts
            if isinstance(row, dict) and row.get("name") == validator.RECEIPT_ARTIFACT_NAME
        ]
        if len(receipt_artifacts) != 1:
            raise validator.RecoveryError("publication_receipt_artifact_ambiguous")
        artifact_meta = receipt_artifacts[0]
        if (
            artifact_meta.get("name") != validator.RECEIPT_ARTIFACT_NAME
            or isinstance(artifact_meta.get("id"), bool)
            or not isinstance(artifact_meta.get("id"), int) or artifact_meta["id"] < 1
            or isinstance(artifact_meta.get("size_in_bytes"), bool)
            or not isinstance(artifact_meta.get("size_in_bytes"), int)
            or artifact_meta.get("size_in_bytes") != len(archive)
            or str(artifact_meta.get("digest", "")).removeprefix("sha256:") != sha256_bytes(archive)
            or not isinstance(artifact_meta.get("expired"), bool)
        ):
            raise validator.RecoveryError("publication_artifact_identity_mismatch")
        artifact_run = artifact_meta.get("workflow_run")
        if not isinstance(artifact_run, dict) or (
            artifact_run.get("id") != run_id
            or artifact_run.get("repository_id") != 1278568329
            or artifact_run.get("head_repository_id") != 1278568329
            or artifact_run.get("head_branch") != "main"
            or artifact_run.get("head_sha") != head_sha
        ):
            raise validator.RecoveryError("publication_artifact_run_mismatch")
        created_at = parse_time(artifact_meta.get("created_at"), "publisher artifact created_at")
        expires_at = parse_time(artifact_meta.get("expires_at"), "publisher artifact expires_at")
        if not job_started <= created_at <= job_completed or expires_at <= created_at:
            raise validator.RecoveryError("publication_artifact_time_mismatch")
        archive_receipt, archive_binding = validator.extract_receipt_archive(archive)
        if archive_receipt != receipt_raw or archive_binding != binding_raw:
            raise validator.RecoveryError("publication_archive_indexed_members_mismatch")
        receipt_ids = validator.verify_receipt(
            receipt_raw, binding_raw, repository="StatPan/datapan-registry",
            workflow_head_sha=head_sha, root=root,
        )
    except (KeyError, TypeError, ValueError, validator.RecoveryError) as exc:
        raise ValueError(f"native publisher evidence failed the #716 contract: {exc}") from exc

    workflow_started_text = run.get("run_started_at") or run.get("created_at")
    workflow_completed_text = run.get("run_completed_at") or run.get("updated_at")
    workflow_started = parse_time(workflow_started_text, "publisher run start")
    workflow_completed = parse_time(workflow_completed_text, "publisher run completion")
    cutoff = parse_time(evaluation_epoch, "publisher evaluation epoch")
    if not workflow_started <= job_started <= job_completed <= workflow_completed <= cutoff:
        raise ValueError("native publisher attempt timestamps are outside its exact run or after evaluation")
    job_rows = jobs.get("jobs")
    publisher_jobs = [
        row for row in job_rows
        if isinstance(row, dict) and row.get("name") == validator.PUBLISHER_JOB
    ]
    verify_steps = [
        step for step in publisher_jobs[0].get("steps", [])
        if isinstance(step, dict) and step.get("name") == validator.VERIFY_STEP
    ] if len(publisher_jobs) == 1 and isinstance(publisher_jobs[0].get("steps"), list) else []
    if (
        len(verify_steps) != 1
        or verify_steps[0].get("status") != "completed"
        or verify_steps[0].get("conclusion") != "success"
        or not isinstance(verify_steps[0].get("started_at"), str)
        or not isinstance(verify_steps[0].get("completed_at"), str)
    ):
        raise ValueError("native publisher lacks one exact successful anonymous-verification step")
    verification_started = parse_time(verify_steps[0]["started_at"], "publisher anonymous verification start")
    verification_completed = parse_time(verify_steps[0]["completed_at"], "publisher anonymous verification completion")
    if not job_started <= verification_started <= verification_completed <= job_completed <= cutoff:
        raise ValueError("native anonymous-verification timestamps are outside the exact publisher job")
    workflow_tip = assert_main_ancestor(root, head_sha, "publisher workflow source commit")
    source_tip = assert_main_ancestor(root, receipt_ids["source_sha"], "published Registry source commit")
    if receipt_ids["manifest_sha256"] != json.loads(receipt_raw).get("source_binding", {}).get("manifest_sha256"):
        raise ValueError("native publisher receipt source-manifest identity is inconsistent")
    return {
        "run_id": str(run_id), "attempt": attempt, "workflow_id": contract["workflow_id"],
        "workflow_path": contract["workflow_path"], "event": run["event"],
        "workflow_head_sha": head_sha, "workflow_main_ancestry_checked_tip": workflow_tip,
        "source_sha": receipt_ids["source_sha"], "source_main_ancestry_checked_tip": source_tip,
        "manifest_sha256": receipt_ids["manifest_sha256"],
        "payload_revision": receipt_ids["payload_revision"],
        "pointer_revision": receipt_ids["pointer_revision"],
        "artifact_id": artifact_meta["id"], "artifact_sha256": sha256_bytes(archive),
        "artifact_expired_at_evaluation": artifact_meta["expired"],
        "job_started_at": job_started.isoformat().replace("+00:00", "Z"),
        "job_completed_at": job_completed.isoformat().replace("+00:00", "Z"),
        "native_verification_started_at": verification_started.isoformat().replace("+00:00", "Z"),
        "native_verification_completed_at": verification_completed.isoformat().replace("+00:00", "Z"),
        "receipt_sha256": sha256_bytes(receipt_raw),
        "source_binding": json.loads(binding_raw),
        "publication": json.loads(receipt_raw).get("publication", {}),
        "anonymous_verification": json.loads(receipt_raw).get("anonymous_verification", {}),
    }


NATIVE_PUBLICATION_READBACK_ROLES = frozenset({
    "publication_source_commit", "publication_source_manifest", "publication_workflow",
    "publication_repo_metadata_before", "publication_pointer_before",
    "publication_repo_metadata_after", "publication_pointer_after",
    "publication_pointer_immutable", "publication_anonymous_manifest",
    "publication_anonymous_payload",
})


def native_publication_delivery_facet(
    *, publisher: dict[str, Any], readback: dict[str, Any],
    acknowledgement: dict[str, Any] | None,
) -> dict[str, Any]:
    """Normalize already-validated current native publisher/ACK facts.

    Callers must first run the native publisher, immutable-pointer/readback,
    and (when supplied) ACK transition validators. This function only joins
    those returned facts; it does not grant scope authority or completeness.
    """
    registry = readback.get("registry_artifact")
    if not isinstance(registry, dict):
        raise ValueError("native publication read-back lacks its validated Registry artifact")
    subject = {
        "repository": "StatPan/datapan-registry",
        "source_sha": publisher.get("source_sha"),
        "manifest_sha256": publisher.get("manifest_sha256"),
        "registry_path": registry.get("path"),
        "registry_sha256": registry.get("sha256"),
        "registry_bytes": registry.get("bytes"),
        "publisher_run_id": publisher.get("run_id"),
        "publisher_attempt": publisher.get("attempt"),
        "payload_revision": readback.get("payload_revision"),
        "pointer_revision": readback.get("pointer_revision"),
    }
    if acknowledgement is not None and (
        acknowledgement.get("source_sha") != subject["source_sha"]
        or acknowledgement.get("manifest_sha256") != subject["manifest_sha256"]
    ):
        raise ValueError("native publication ACK does not identify the exact read-back subject")
    details: dict[str, Any] = {
        "classification": "native_delivery_only",
        "currentness_established": False,
        "updated_claim_established": False,
        "release_authority": False,
        "cutover_established": False,
        "verified_artifact_count": readback.get("verified_artifact_count"),
        "publisher_job_started_at": publisher.get("job_started_at"),
        "publisher_job_completed_at": publisher.get("job_completed_at"),
        "publisher_anonymous_verify_completed_at": publisher.get("native_verification_completed_at"),
        "anonymous_readback_observed_at": readback.get("consumer_readback_observed_at"),
        "publisher_artifact_sha256": publisher.get("artifact_sha256"),
        "publisher_artifact_expired_at_evaluation": publisher.get("artifact_expired_at_evaluation"),
    }
    if acknowledgement is None:
        details["acknowledgement_missing"] = True
        details["delivery_status"] = "publisher_readback_verified_ack_pending"
    else:
        details.update({
            "delivery_status": "publisher_readback_acknowledged",
            "acknowledgement_run_id": acknowledgement.get("run_id"),
            "acknowledgement_attempt": acknowledgement.get("attempt"),
            "acknowledgement_observed_at": acknowledgement.get("acknowledgement_observed_at"),
            "acknowledgement_journal_sha256": acknowledgement.get("journal_sha256"),
            "acknowledgement_state_commit": acknowledgement.get("state_commit"),
        })
        if acknowledgement.get("replay_status") == "already_acknowledged":
            details.update({
                "acknowledgement_replay_run_id": acknowledgement.get("replay_run_id"),
                "acknowledgement_replay_attempt": acknowledgement.get("replay_attempt"),
                "acknowledgement_replay_status": acknowledgement.get("replay_status"),
                "acknowledgement_replay_journal_writes": acknowledgement.get("journal_writes"),
                "acknowledgement_replay_state_unchanged": acknowledgement.get("state_replay_unchanged"),
            })
    return {
        "status": "verified" if acknowledgement is not None else "read-back-verified",
        "subject": subject,
        "missing": [] if acknowledgement is not None else ["acknowledgement"],
        "evidence": [],
        "details": details,
    }


def validate_native_publication_readback(
    *, root: pathlib.Path, inputs: list[dict[str, Any]], resolved: dict[str, pathlib.Path],
    publisher_run: dict[str, Any], publisher: dict[str, Any] | None,
    evaluation_epoch: str,
) -> dict[str, Any] | None:
    """Validate every supplied native pointer/read-back row independently of ACK presence.

    An absent ACK makes delivery incomplete; it does not make a supplied pointer,
    manifest, or anonymous stream report trustworthy by omission. This adapter
    authenticates the publisher's complete verified inventory and its retained
    read-back subset without granting a completeness or currentness claim.
    """
    supplied = {item["role"] for item in inputs} & NATIVE_PUBLICATION_READBACK_ROLES
    if not supplied:
        return None
    if publisher is None or not publisher_run:
        raise ValueError("publication pointer/read-back inputs lack a validated native publisher attempt")
    if supplied != NATIVE_PUBLICATION_READBACK_ROLES:
        raise ValueError(
            "native publication pointer/read-back bundle is incomplete: "
            f"missing={sorted(NATIVE_PUBLICATION_READBACK_ROLES - supplied)}"
        )

    def item_for(role: str) -> dict[str, Any]:
        item = single_role(inputs, role)
        assert item is not None
        return item

    def raw_for(role: str) -> bytes:
        item = item_for(role)
        return receipt_path_bytes(resolved[item["input_id"]], item)

    def json_for(role: str) -> dict[str, Any]:
        item = item_for(role)
        return object_at(resolved[item["input_id"]], role)

    source_sha = publisher["source_sha"]
    manifest_sha = publisher["manifest_sha256"]
    workflow_head_sha = publisher["workflow_head_sha"]
    source_binding = publisher["source_binding"]
    source_manifest_raw = git_read_only(root, ["show", f"{source_sha}:manifest.json"])
    source_manifest = json.loads(source_manifest_raw)
    source_manifest_input = item_for("publication_source_manifest")
    indexed_manifest_raw = raw_for("publication_source_manifest")
    indexed_manifest = json.loads(indexed_manifest_raw)
    if (
        source_manifest_input.get("subject", {}).get("source_revision") != source_sha
        or indexed_manifest_raw != source_manifest_raw
        or indexed_manifest != source_manifest
        or sha256_bytes(source_manifest_raw) != manifest_sha
    ):
        raise ValueError("native publication source-manifest input differs from the exact receipt-bound source commit")

    source_commit = json_for("publication_source_commit")
    if (
        source_commit.get("sha") != source_sha
        or source_commit.get("commit", {}).get("tree", {}).get("sha") != source_binding.get("source_tree_sha")
    ):
        raise ValueError("native publication source-commit API differs from the receipt-bound source tree")
    workflow_api = json_for("publication_workflow")
    workflow_contract = PIPELINE_WORKFLOWS["publisher"]
    workflow_input = item_for("publication_workflow")
    if (
        workflow_api.get("id") != workflow_contract["workflow_id"]
        or workflow_api.get("path") != workflow_contract["workflow_path"]
        or workflow_api.get("state") != "active"
        or (
            workflow_input.get("subject", {}).get("source_revision") is not None
            and workflow_input["subject"]["source_revision"] != workflow_head_sha
        )
    ):
        raise ValueError("native publication workflow snapshot differs from its authenticated run source")

    receipt_raw = raw_for("publication_receipt")
    receipt = json.loads(receipt_raw)
    if (
        receipt.get("source_binding") != source_binding
        or receipt.get("publication") != publisher.get("publication")
        or receipt.get("anonymous_verification") != publisher.get("anonymous_verification")
    ):
        raise ValueError("native publication receipt row differs from the exact verified artifact receipt")
    source_script = git_read_only(root, ["show", f"{source_sha}:scripts/huggingface_registry_distribution.py"])
    workflow_raw = git_read_only(root, ["show", f"{workflow_head_sha}:{workflow_contract['workflow_path']}"])

    pointer_before = json_for("publication_pointer_before")
    pointer_after = json_for("publication_pointer_after")
    pointer_immutable = json_for("publication_pointer_immutable")
    metadata_before = json_for("publication_repo_metadata_before")
    metadata_after = json_for("publication_repo_metadata_after")
    publication_record = publisher.get("publication")
    verification_record = publisher.get("anonymous_verification")
    if not isinstance(publication_record, dict) or not isinstance(verification_record, dict):
        raise ValueError("native publication receipt lacks its exact publication and verification records")

    pointer_validator = import_module(
        "generic_distribution_pointer_contract", root / "scripts/completeness_publication_evidence.py"
    )
    registry_rows = [
        row for row in source_manifest.get("artifacts", [])
        if isinstance(row, dict) and row.get("path") == "data/data-go-kr.registry.json"
    ]
    if len(registry_rows) != 1:
        raise ValueError("native publication source manifest does not uniquely identify the Registry payload")
    registry_member = registry_rows[0]
    for label, pointer in (("after", pointer_after), ("immutable", pointer_immutable)):
        pointer_validator._check_distribution_index(
            pointer,
            payload_revision=publisher["payload_revision"],
            pointer_revision=publisher["pointer_revision"],
            manifest_sha256=manifest_sha,
            manifest_bytes=len(source_manifest_raw),
            registry_sha256=registry_member.get("sha256"),
            registry_bytes=registry_member.get("bytes"),
            artifact_count=publication_record.get("artifacts"),
            label=f"native publication {label} pointer",
        )
    if pointer_after != pointer_immutable:
        raise ValueError("native publication immutable pointer differs from the post-publication pointer")

    def validate_pointer_snapshot(pointer: dict[str, Any], label: str) -> None:
        dataset = pointer.get("dataset")
        manifest = pointer.get("release_manifest")
        rows = pointer.get("artifacts")
        count = pointer.get("artifact_count")
        if (
            pointer.get("schema_version") != "datapan.huggingface-distribution.v1"
            or not isinstance(dataset, dict)
            or dataset.get("id") != "StatPan/datapan-registry"
            or not isinstance(dataset.get("revision"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", dataset["revision"])
            or not isinstance(manifest, dict)
            or manifest.get("path") != "manifest.json"
            or manifest.get("kind") != "release_manifest"
            or isinstance(manifest.get("bytes"), bool)
            or not isinstance(manifest.get("bytes"), int)
            or not SHA256_RE.fullmatch(manifest.get("sha256", ""))
            or isinstance(count, bool)
            or not isinstance(count, int)
            or not isinstance(rows, list)
            or count != len(rows)
        ):
            raise ValueError(f"native publication {label} pointer snapshot is malformed")
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"native publication {label} pointer contains a non-object artifact")
            path = normalize_relative_path(row.get("path"), f"native publication {label} artifact path").as_posix()
            size, digest, kind = row.get("bytes"), row.get("sha256"), row.get("kind")
            if (
                path in seen or isinstance(size, bool) or not isinstance(size, int) or size < 1
                or not isinstance(kind, str) or not kind or not isinstance(digest, str)
                or not SHA256_RE.fullmatch(digest)
            ):
                raise ValueError(f"native publication {label} pointer artifact identity is malformed")
            seen.add(path)

    validate_pointer_snapshot(pointer_before, "before")
    for label, metadata in (("before", metadata_before), ("after", metadata_after)):
        siblings = metadata.get("siblings")
        if (
            metadata.get("id") != "StatPan/datapan-registry"
            or not isinstance(metadata.get("sha"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", metadata["sha"])
            or not isinstance(siblings, list)
            or not {"manifest.json", "data/data-go-kr.registry.json"}.issubset({
                item.get("rfilename") for item in siblings if isinstance(item, dict)
            })
        ):
            raise ValueError(f"native publication repository metadata {label} snapshot is malformed")
    if metadata_after.get("sha") != publisher["pointer_revision"]:
        raise ValueError("native publication repository metadata does not bind the receipt pointer revision")
    inventory = validate_native_distribution_inventory(
        manifest=source_manifest, manifest_raw=source_manifest_raw,
        pointer=pointer_immutable, receipt=receipt,
        source_script_raw=source_script, workflow_raw=workflow_raw,
    )

    anonymous_manifest_raw = raw_for("publication_anonymous_manifest")
    if anonymous_manifest_raw != source_manifest_raw:
        raise ValueError("native anonymous release-manifest read-back differs from the exact published source bytes")
    readback = json_for("publication_anonymous_payload")
    check_rows = [
        row for row in readback.get("checks", [])
        if isinstance(row, dict) and row.get("check") == "immutable_registry_stream_matches_expected_sha_and_size"
    ] if isinstance(readback.get("checks"), list) else []
    detail = check_rows[0].get("detail") if len(check_rows) == 1 else None
    try:
        readback_at = parse_time(readback.get("generated_at"), "anonymous publisher read-back time")
        verified_at = parse_time(publisher["native_verification_completed_at"], "native publisher verification time")
        cutoff = parse_time(evaluation_epoch, "publication evaluation epoch")
    except ValueError as exc:
        raise ValueError("native anonymous read-back lacks valid bounded timestamps") from exc
    if (
        str(readback.get("publisher_run_id")) != str(publisher_run.get("id"))
        or isinstance(readback.get("publisher_attempt"), bool)
        or not isinstance(readback.get("publisher_attempt"), int)
        or readback.get("publisher_attempt") != publisher_run.get("run_attempt")
        or len(check_rows) != 1 or not isinstance(detail, dict)
        or detail.get("path") != registry_member.get("path")
        or isinstance(detail.get("bytes_streamed"), bool)
        or not isinstance(detail.get("bytes_streamed"), int)
        or detail.get("bytes_streamed") != registry_member.get("bytes")
        or detail.get("sha256") != registry_member.get("sha256")
        or detail.get("revision") != publisher["payload_revision"]
        or not verified_at <= readback_at <= cutoff
    ):
        raise ValueError("native anonymous read-back does not corroborate the exact verified publisher payload")
    return {
        "source_sha": source_sha,
        "manifest_sha256": manifest_sha,
        "registry_artifact": copy.deepcopy(registry_member),
        "publisher_run_id": str(publisher_run["id"]),
        "publisher_attempt": publisher_run["run_attempt"],
        "payload_revision": publisher["payload_revision"],
        "pointer_revision": publisher["pointer_revision"],
        "native_verification_completed_at": publisher["native_verification_completed_at"],
        "consumer_readback_observed_at": readback.get("generated_at"),
        "verified_artifact_count": inventory["distribution_artifacts"],
        "evidence": [
            artifact(input_path(item_for(role)), receipt_path_bytes(resolved[item_for(role)["input_id"]], item_for(role)))
            for role in sorted(NATIVE_PUBLICATION_READBACK_ROLES)
        ],
    }


def validate_acknowledgement_state_bundles(
    *, roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path],
    journal_before_raw: bytes, journal_after_raw: bytes,
    require_before: bool = False,
) -> dict[str, Any]:
    """Authenticate optional before/after ACK state trees and their journal blobs."""
    after_roles = ACKNOWLEDGEMENT_STATE_AFTER_ROLES & set(roles)
    before_roles = ACKNOWLEDGEMENT_STATE_BEFORE_ROLES & set(roles)
    if after_roles and after_roles != ACKNOWLEDGEMENT_STATE_AFTER_ROLES:
        raise ValueError("ACK after-state evidence is only valid as a complete ref/commit/tree/blob bundle")
    if before_roles and before_roles != ACKNOWLEDGEMENT_STATE_BEFORE_ROLES:
        raise ValueError("ACK before-state evidence is only valid as a complete ref/commit/tree/blob bundle")
    if before_roles and not after_roles:
        raise ValueError("ACK before-state evidence requires its exact after-state bundle")
    if require_before and (not before_roles or not after_roles):
        raise ValueError("already-acknowledged replay requires complete before/after ACK state bundles")

    result: dict[str, Any] = {"before": None, "after": None}
    if after_roles:
        after_bytes = validate_state_tree(
            stage="acknowledgement", roles=roles, resolved=resolved,
            ref_role="acknowledgement_state_ref", commit_role="acknowledgement_state_commit",
            tree_role="acknowledgement_state_tree", content_roles={},
            blob_roles={"acknowledgement_journal_blob_api": "reports/canonical-update-promotion-receipt.json"},
        )
        after_ref = indexed_json(roles, resolved, "acknowledgement_state_ref")
        after_commit = indexed_json(roles, resolved, "acknowledgement_state_commit")
        after_tree = indexed_json(roles, resolved, "acknowledgement_state_tree")
        if (
            after_ref.get("ref") != "refs/heads/automation/canonical-update-state"
            or after_commit.get("sha") != after_ref.get("object", {}).get("sha")
            or after_bytes["acknowledgement_journal_blob_api"] != journal_after_raw
        ):
            raise ValueError("ACK after-state ref/tree does not bind the exact after-journal snapshot")
        result["after"] = {
            "ref": after_ref, "commit": after_commit, "tree": after_tree,
            "journal_blob": after_bytes["acknowledgement_journal_blob_api"],
        }

    if before_roles:
        before_role_map = {
            after_role: roles[before_role]
            for before_role, after_role in ACKNOWLEDGEMENT_STATE_BEFORE_ROLE_MAP.items()
        }
        before_bytes = validate_state_tree(
            stage="acknowledgement", roles=before_role_map, resolved=resolved,
            ref_role="acknowledgement_state_ref", commit_role="acknowledgement_state_commit",
            tree_role="acknowledgement_state_tree", content_roles={},
            blob_roles={"acknowledgement_journal_blob_api": "reports/canonical-update-promotion-receipt.json"},
        )
        before_ref = indexed_json(before_role_map, resolved, "acknowledgement_state_ref")
        before_commit = indexed_json(before_role_map, resolved, "acknowledgement_state_commit")
        before_tree = indexed_json(before_role_map, resolved, "acknowledgement_state_tree")
        if (
            before_ref.get("ref") != "refs/heads/automation/canonical-update-state"
            or before_commit.get("sha") != before_ref.get("object", {}).get("sha")
            or before_bytes["acknowledgement_journal_blob_api"] != journal_before_raw
        ):
            raise ValueError("ACK before-state ref/tree does not bind the exact before-journal snapshot")
        result["before"] = {
            "ref": before_ref, "commit": before_commit, "tree": before_tree,
            "journal_blob": before_bytes["acknowledgement_journal_blob_api"],
        }
        if require_before:
            for before_role, after_role in ACKNOWLEDGEMENT_STATE_BEFORE_ROLE_MAP.items():
                before_raw = receipt_path_bytes(resolved[roles[before_role]["input_id"]], roles[before_role])
                after_raw = receipt_path_bytes(resolved[roles[after_role]["input_id"]], roles[after_role])
                if before_raw != after_raw:
                    raise ValueError("already-acknowledged replay changed its state ref/commit/tree/blob")
            if journal_before_raw != journal_after_raw:
                raise ValueError("already-acknowledged replay changed its promotion journal bytes")
    return result


def validate_present_stage_local_artifacts(
    *, root: pathlib.Path, stage_roles: dict[str, dict[str, dict[str, Any]]],
    stage_runs: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    resolved: dict[str, pathlib.Path], current_registry: dict[str, Any], evaluation_epoch: str,
) -> None:
    """Validate present stage-local payloads before reporting a missing chain link."""
    source_roles = stage_roles["source"]
    source_result: dict[str, Any] | None = None
    if source_roles:
        run, job = stage_runs["source"]
        archive_item = source_roles["pipeline_artifact_archive"]
        source_result = validate_source_observation(
            root=root, roles=source_roles, resolved=resolved, run=run, job=job,
            archive=receipt_path_bytes(resolved[archive_item["input_id"]], archive_item),
            evaluation_epoch=evaluation_epoch,
        )

    processor_roles = stage_roles["processor"]
    processor_result: dict[str, Any] | None = None
    if processor_roles:
        run, job = stage_runs["processor"]
        processor_result = validate_processor_stage(
            root=root, roles=processor_roles, resolved=resolved, run=run, job=job,
            source=source_result, current_registry=current_registry,
            evaluation_epoch=evaluation_epoch,
        )

    promotion_roles = stage_roles["promotion"]
    promotion_result: dict[str, Any] | None = None
    promotion_state_commit: str | None = None
    promotion_journal_raw: bytes | None = None
    if promotion_roles:
        run, job = stage_runs["promotion"]
        assert_main_ancestor(root, run["head_sha"], "promotion workflow source commit")
        log_item = promotion_roles["pipeline_log_archive"]
        log_results = promotion_log_results(receipt_path_bytes(resolved[log_item["input_id"]], log_item))
        state_bytes = validate_state_tree(
            stage="promotion", roles=promotion_roles, resolved=resolved,
            ref_role="promotion_state_ref", commit_role="promotion_state_commit",
            tree_role="promotion_state_tree", content_roles={},
            blob_roles={"promotion_journal_blob_api": "reports/canonical-update-promotion-receipt.json"},
        )
        promotion_journal_raw = state_bytes["promotion_journal_blob_api"]
        promotion_state_ref = indexed_json(promotion_roles, resolved, "promotion_state_ref")
        promotion_state_commit = promotion_state_ref.get("object", {}).get("sha")
        journal = json.loads(state_bytes["promotion_journal_blob_api"])
        validate_promotion_journal_at_revision(root=root, revision=run["head_sha"], journal=journal)
        if promotion_state_ref.get("ref") != "refs/heads/automation/canonical-update-state":
            raise ValueError("promotion state ref is outside its registered owned branch")
        if processor_result is not None:
            promotion_result = validate_promotion_stage(
                root=root, roles=promotion_roles, resolved=resolved, run=run, job=job,
                processor=processor_result, current_registry=current_registry,
                evaluation_epoch=evaluation_epoch,
            )
        else:
            # When B is absent, validate C's exact run-local outcome shape and
            # authenticated state payload, while keeping its B relationship
            # explicitly unproven.
            no_op_results = [row for row in log_results if "already_canonical_generations" in row]
            if no_op_results:
                allowed_statuses = {
                    "no-eligible-ready-processor-bundle", "already-canonical-payload",
                    "skipped-already-canonical-processor-bundles",
                }
                if len(no_op_results) != 1:
                    raise ValueError("promotion log contains ambiguous no-op candidate summaries")
                output = no_op_results[0]
                rows = output.get("already_canonical_generations")
                head_registry, _head_manifest = source_lfs_binding(root, run["head_sha"])
                if (
                    output.get("status") not in allowed_statuses
                    or output.get("candidate_available") is not False
                    or not isinstance(rows, list)
                    or any(
                        not isinstance(row, dict)
                        or row.get("candidate_available") is not False
                        or row.get("reason") != "already_canonical_payload"
                        or not isinstance(row.get("generation_id"), str)
                        or not SHA256_RE.fullmatch(row["generation_id"])
                        or row.get("registry_sha256") != head_registry.get("sha256")
                        or any(
                            isinstance(row.get(field), bool)
                            or not isinstance(row.get(field), int)
                            or row[field] < 0
                            for field in ("pending_count", "detail_retry_count", "detail_unattempted_count")
                        )
                        for row in rows
                    )
                ):
                    raise ValueError("promotion no-op log lacks a valid producer-head-scoped result")
            else:
                run_url = f"https://github.com/StatPan/datapan-registry/actions/runs/{run['id']}/attempts/{run['run_attempt']}"
                reconciliations = [
                    row for row in log_results
                    if row.get("status") == "promotion-prs-reconciled" and row.get("run_url") == run_url
                ]
                if len(reconciliations) != 1:
                    raise ValueError("promotion log does not bind one successful result to the exact current run attempt")

    health_roles = stage_roles["health"]
    if health_roles:
        run, job = stage_runs["health"]
        health_local = validate_health_stage_local(
            root=root, roles=health_roles, resolved=resolved, run=run, job=job,
            evaluation_epoch=evaluation_epoch,
        )
        archive = health_local["archive"]
        if (
            processor_result is not None
            and health_local["processor_ref"]["commit_sha"] != processor_result["state_commit"]
        ):
            raise ValueError("health archived processor ref differs from the exact supplied B state")
        if (
            promotion_state_commit is not None
            and health_local["promotion_ref"]["commit_sha"] != promotion_state_commit
        ):
            raise ValueError("health archived promotion ref differs from the exact supplied C state")
        if (
            promotion_journal_raw is not None
            and health_local["promotion_journal_raw"] != promotion_journal_raw
        ):
            raise ValueError("health archived promotion journal differs from the exact supplied C state")
        if source_result is not None and processor_result is not None and promotion_result is not None:
            validate_health_stage(
                root=root, roles=health_roles, resolved=resolved, run=run, job=job,
                processor=processor_result, promotion=promotion_result, source=source_result,
                current_registry=current_registry, evaluation_epoch=evaluation_epoch,
            )


def validate_consumer_readback(
    *, readback: dict[str, Any], proof: dict[str, Any],
    publication: dict[str, Any], publisher_job: dict[str, Any],
    evaluation_epoch: str, publisher_run: dict[str, Any], expected_bytes: int,
) -> dict[str, Any]:
    """Treat a consumer report only as corroboration of native verified bytes."""
    artifact = proof.get("publication", {}).get("artifact", {})
    artifact_path = normalize_relative_path(artifact.get("path"), "proof publication artifact path").as_posix()
    artifact_sha = artifact.get("sha256")
    if not SHA256_RE.fullmatch(artifact_sha if isinstance(artifact_sha, str) else ""):
        raise ValueError("proof publication artifact digest is invalid")
    if (
        isinstance(readback.get("publisher_run_id"), bool)
        or str(readback.get("publisher_run_id")) != str(publisher_run.get("id"))
        or isinstance(readback.get("publisher_attempt"), bool)
        or readback.get("publisher_attempt") != publisher_run.get("run_attempt")
    ):
        raise ValueError("consumer read-back report does not bind the exact publisher run attempt")
    cutoff = parse_time(evaluation_epoch, "consumer read-back evaluation epoch")
    native_verified_at = parse_time(
        publication.get("native_verification_completed_at"),
        "native publisher verification completion",
    )
    if native_verified_at > cutoff:
        raise ValueError("native anonymous verification is after the evaluation epoch")
    if proof.get("consumer_read_back", {}).get("observed_at") != publication.get("native_verification_completed_at"):
        raise ValueError("proof read-back time must be the exact native anonymous-verification completion")
    checks = readback.get("checks")
    if not isinstance(checks, list):
        raise ValueError("consumer read-back report has no exact streamed-check list")
    matching: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("detail"), dict):
            continue
        detail = check["detail"]
        if detail.get("path") == artifact_path:
            matching.append(detail)
    if len(matching) != 1:
        raise ValueError("consumer read-back must contain one exact stream result for the proof artifact")
    detail = matching[0]
    observed_bytes = detail.get("bytes_streamed", detail.get("bytes"))
    if (
        isinstance(observed_bytes, bool) or not isinstance(observed_bytes, int) or observed_bytes < 0
        or observed_bytes != expected_bytes
        or detail.get("sha256") != artifact_sha
        or detail.get("revision") != publication.get("payload_revision")
        or proof.get("consumer_read_back", {}).get("artifact_sha256") != artifact_sha
    ):
        raise ValueError("consumer stream bytes do not match the exact proof artifact and immutable publication revision")
    return {
        "path": artifact_path, "sha256": artifact_sha, "bytes": observed_bytes,
        "revision": detail["revision"],
        "observed_at": publication["native_verification_completed_at"],
        "evidence_contract": "authenticated_native_anonymous_pointer_verification",
    }


def acknowledgement_log_result(raw: bytes) -> dict[str, Any]:
    members = safe_zip_members(
        raw, label="publication acknowledgement logs", max_archive=16 * 1024 * 1024,
        max_expanded=64 * 1024 * 1024,
    )
    found: list[dict[str, Any]] = []
    for member in members.values():
        try:
            text = member.decode("utf-8-sig", "strict")
        except UnicodeDecodeError:
            continue
        for line in text.splitlines():
            start = line.find("{")
            if start < 0:
                continue
            try:
                value = json.loads(line[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("status") in {
                "read-back-confirmed", "already_acknowledged",
            }:
                found.append(value)
    # GitHub's retained job archive can contain the same structured result in
    # both the step log and the job summary. Duplicate identical lines are one
    # fact; conflicting outcomes are ambiguous and must fail closed.
    unique = {canonical_json_bytes(row) for row in found}
    if len(unique) != 1:
        raise ValueError("acknowledgement logs do not contain one unambiguous successful result")
    return found[0]


def acknowledged_journal_witness(
    journal: dict[str, Any], *, source_id: str, source_sha: str, manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the unique active read-back witness for an exact merged subject."""
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    records = journal.get("records")
    if not isinstance(records, list):
        raise ValueError("acknowledgement journal has no record list")
    for row in records:
        if not isinstance(row, dict):
            continue
        candidate, pr, acknowledgements = row.get("candidate"), row.get("pr"), row.get("acknowledgements")
        if not isinstance(candidate, dict) or not isinstance(pr, dict) or not isinstance(acknowledgements, list) or not acknowledgements:
            continue
        final_ack = acknowledgements[-1]
        if not isinstance(final_ack, dict):
            continue
        if (
            row.get("status") == "read-back-confirmed"
            and row.get("superseded_by") is None
            and candidate.get("repository", "").casefold() == "statpan/datapan-registry"
            and candidate.get("source_id") == source_id
            and candidate.get("manifest_sha256") == manifest_sha256
            and pr.get("state") == "merged"
            and pr.get("merge_commit_sha") == source_sha
            and final_ack.get("status") == "read-back-confirmed"
            and final_ack.get("source_sha") == source_sha
            and final_ack.get("manifest_sha256") == manifest_sha256
        ):
            matches.append((row, final_ack))
    if len(matches) != 1:
        raise ValueError("ACK replay does not identify one active exact-subject read-back witness")
    row, witness = matches[0]
    candidate = row["candidate"]
    selected_pr = row.get("pr")
    selected_pr_number = selected_pr.get("number") if isinstance(selected_pr, dict) else None
    if (
        isinstance(selected_pr_number, bool)
        or not isinstance(selected_pr_number, int)
        or selected_pr_number < 1
    ):
        raise ValueError("ACK replay's selected PR number is not a positive integer")
    identity = witness.get("artifact_identity")
    witness_run_id = witness.get("run_id")
    witness_attempt = witness.get("run_attempt")
    witness_url = (
        f"https://github.com/StatPan/datapan-registry/actions/runs/{witness_run_id}/attempts/{witness_attempt}"
        if isinstance(witness_run_id, int) and not isinstance(witness_run_id, bool)
        and isinstance(witness_attempt, int) and not isinstance(witness_attempt, bool)
        else None
    )
    if (
        candidate.get("registry_path") != "data/data-go-kr.registry.json"
        or isinstance(candidate.get("registry_bytes"), bool)
        or not isinstance(candidate.get("registry_bytes"), int)
        or candidate.get("registry_bytes") < 1
        or not SHA256_RE.fullmatch(candidate.get("registry_sha256", ""))
        or not isinstance(identity, dict)
        or identity != {
            "path": candidate.get("registry_path"),
            "bytes": candidate.get("registry_bytes"),
            "sha256": candidate.get("registry_sha256"),
        }
        or witness.get("read_back_verified") is not True
        or witness.get("read_back_sha256") != candidate.get("registry_sha256")
        or witness.get("read_back_bytes") != candidate.get("registry_bytes")
        or isinstance(witness_run_id, bool) or not isinstance(witness_run_id, int) or witness_run_id < 1
        or isinstance(witness_attempt, bool) or not isinstance(witness_attempt, int) or witness_attempt < 1
        or witness.get("run_url") != witness_url
        or not isinstance(witness.get("publication_revision"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", witness["publication_revision"])
        or not isinstance(witness.get("publication_pointer_revision"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", witness["publication_pointer_revision"])
        or not isinstance(witness.get("evidence_reference"), str)
        or not re.search(r"sha256=[a-f0-9]{64}(?:\b|$)", witness["evidence_reference"])
    ):
        raise ValueError("ACK replay's existing read-back witness is malformed or unbound")
    parse_time(witness.get("observed_at"), "prior ACK witness observation time")
    return row, witness


def validate_acknowledgement_transition(
    *, root: pathlib.Path, scope: dict[str, Any], acknowledgement_scope: dict[str, Any],
    scoped_inputs: list[dict[str, Any]],
    roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path],
    ack_run: dict[str, Any], ack_job: dict[str, Any],
    publisher: dict[str, Any], publisher_receipt_raw: bytes,
    release_manifest: dict[str, Any], evaluation_epoch: str,
    local_transition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if local_transition is None:
        local_transition = validate_acknowledgement_local_transition(
            root=root, scope=scope, scoped_inputs=scoped_inputs, roles=roles,
            resolved=resolved, ack_run=ack_run, ack_job=ack_job,
            evaluation_epoch=evaluation_epoch,
        )
    supplied_state_roles = ACKNOWLEDGEMENT_STATE_AFTER_ROLES & set(roles)
    supplied_before_state_roles = ACKNOWLEDGEMENT_STATE_BEFORE_ROLES & set(roles)
    before_item = single_role(scoped_inputs, "acknowledgement_journal_before")
    after_item = single_role(scoped_inputs, "acknowledgement_journal_after")
    if before_item is None or after_item is None:
        raise ValueError("updated claim requires the append-only ACK journal before/after pair")
    for item in (before_item, after_item):
        producer = item["producer"]
        if (
            item.get("namespace") not in {"live_operational", "historical_admitted"}
            or producer.get("repository", "").casefold() != "statpan/datapan-registry"
        ):
            raise ValueError("ACK journal snapshot lacks its retained Registry admission identity")
        tuple_keys = {"run_id", "attempt", "workflow_id", "workflow_path", "event", "revision"}
        indexed_tuple = tuple_keys & set(producer)
        if item.get("namespace") == "live_operational" or indexed_tuple:
            if (
                indexed_tuple != tuple_keys
                or producer.get("run_id") != str(ack_run["id"])
                or isinstance(producer.get("attempt"), bool)
                or producer.get("attempt") != ack_run["run_attempt"]
                or producer.get("workflow_id") != PIPELINE_WORKFLOWS["acknowledgement"]["workflow_id"]
                or producer.get("workflow_path") != PIPELINE_WORKFLOWS["acknowledgement"]["workflow_path"]
                or producer.get("event") != ack_run["event"]
                or producer.get("revision") != ack_run["head_sha"]
            ):
                raise ValueError("ACK journal snapshots are not attributed to the exact trusted ACK attempt")

    before_raw = receipt_path_bytes(resolved[before_item["input_id"]], before_item)
    after_raw = receipt_path_bytes(resolved[after_item["input_id"]], after_item)
    before = json.loads(before_raw)
    after = json.loads(after_raw)
    validate_schema(before, root / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json", "ACK journal before")
    validate_schema(after, root / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json", "ACK journal after")
    if (
        before.get("repository", "").casefold() != "statpan/datapan-registry"
        or after.get("repository", "").casefold() != "statpan/datapan-registry"
    ):
        raise ValueError("ACK journal repository identity differs")
    log_item = roles.get("acknowledgement_log_archive")
    if log_item is None:
        raise ValueError("ACK workflow evidence lacks its exact job log archive")
    log_result = acknowledgement_log_result(
        receipt_path_bytes(resolved[log_item["input_id"]], log_item)
    )
    state = validate_acknowledgement_state_bundles(
        roles=roles, resolved=resolved, journal_before_raw=before_raw,
        journal_after_raw=after_raw,
        require_before=log_result.get("status") == "already_acknowledged",
    )

    source_binding = publisher["source_binding"]
    source_sha = source_binding["source_sha"]
    manifest_sha = publisher["manifest_sha256"]
    registry_rows = [row for row in release_manifest.get("artifacts", []) if row.get("path") == "data/data-go-kr.registry.json"]
    if len(registry_rows) != 1:
        raise ValueError("ACK subject release does not uniquely contain the canonical Registry payload")
    registry_artifact = registry_rows[0]

    def matching_rows(journal: dict[str, Any], *, allow_unmerged: bool) -> list[dict[str, Any]]:
        records = journal.get("records")
        if not isinstance(records, list):
            return []
        return [
            row for row in records
            if isinstance(row, dict)
            and isinstance(row.get("candidate"), dict)
            and isinstance(row.get("pr"), dict)
            and row["candidate"].get("repository", "").casefold() == "statpan/datapan-registry"
            and row["candidate"].get("source_id") == acknowledgement_scope["source_id"]
            and row["candidate"].get("manifest_sha256") == manifest_sha
            and row["candidate"].get("registry_path") == registry_artifact.get("path")
            and row["candidate"].get("registry_sha256") == registry_artifact.get("sha256")
            and row["candidate"].get("registry_bytes") == registry_artifact.get("bytes")
            and (
                row["pr"].get("merge_commit_sha") == source_sha
                or allow_unmerged and row["pr"].get("merge_commit_sha") is None
            )
        ]

    before_matches = matching_rows(before, allow_unmerged=True)
    after_matches = matching_rows(after, allow_unmerged=False)
    if len(before_matches) != 1 or len(after_matches) != 1:
        raise ValueError("ACK journal does not uniquely identify the exact published source subject")
    before_row, after_row = before_matches[0], after_matches[0]
    if log_result.get("status") == "already_acknowledged":
        local = local_transition
        if (
            before_raw != after_raw or before_row != after_row
            or before_row.get("status") != "read-back-confirmed"
            or after_row.get("status") != "read-back-confirmed"
        ):
            raise ValueError("ACK no-write replay does not preserve the exact read-back-confirmed journal row")
        _witness_row, witness = acknowledged_journal_witness(
            after, source_id=acknowledgement_scope["source_id"],
            source_sha=source_sha, manifest_sha256=manifest_sha,
        )
        candidate = after_row["candidate"]
        if (
            candidate.get("registry_path") != registry_artifact.get("path")
            or candidate.get("registry_sha256") != registry_artifact.get("sha256")
            or candidate.get("registry_bytes") != registry_artifact.get("bytes")
            or witness.get("publication_revision") != publisher["payload_revision"]
            or witness.get("publication_pointer_revision") != publisher["pointer_revision"]
            or f"sha256={sha256_bytes(publisher_receipt_raw)}" not in witness.get("evidence_reference", "")
            or local.get("status") != "already_acknowledged"
        ):
            raise ValueError("ACK replay's existing witness differs from the exact native publisher subject")
        ack_started = parse_time(ack_job["started_at"], "ACK job start")
        ack_completed = parse_time(ack_job["completed_at"], "ACK job completion")
        replay_started = parse_time(
            ack_run.get("run_started_at") or ack_run.get("created_at"), "ACK replay run start",
        )
        cutoff = parse_time(evaluation_epoch, "ACK evaluation epoch")
        if (
            replay_started < parse_time(publisher["job_completed_at"], "publisher job completion")
            or ack_started < parse_time(publisher["job_completed_at"], "publisher job completion")
            or ack_completed > cutoff
        ):
            raise ValueError("ACK no-write replay is outside the exact published subject's time bounds")
        return {
            "run_id": str(witness["run_id"]), "attempt": witness["run_attempt"],
            "source_sha": source_sha, "manifest_sha256": manifest_sha,
            "workflow_id": ack_run["workflow_id"], "event": ack_run["event"],
            "workflow_head_sha": ack_run["head_sha"],
            "state_commit": local["state_commit"], "state_tree_sha": local["state_tree_sha"],
            "journal_sha256": sha256_bytes(after_raw), "journal_record_count": len(after["records"]),
            "acknowledgement_observed_at": witness["observed_at"],
            "replay_run_id": str(ack_run["id"]), "replay_attempt": ack_run["run_attempt"],
            "replay_status": "already_acknowledged", "journal_writes": 0,
            "state_replay_unchanged": True,
        }
    if (
        local_transition.get("status") != "read-back-confirmed"
        or after_row.get("status") != "read-back-confirmed"
        or after_row.get("superseded_by") is not None
        or after_row.get("pr", {}).get("state") != "merged"
        or after_row.get("pr", {}).get("merge_commit_sha") != source_sha
    ):
        raise ValueError("ACK owning transition does not confirm this active merged publication subject")
    new_acks = after_row.get("acknowledgements")
    if not isinstance(new_acks, list) or not new_acks or not isinstance(new_acks[-1], dict):
        raise ValueError("ACK owning transition has no final read-back acknowledgement")
    log_item = roles["acknowledgement_log_archive"]
    log_result = acknowledgement_log_result(receipt_path_bytes(resolved[log_item["input_id"]], log_item))
    ack_started = parse_time(ack_job["started_at"], "ACK job start")
    ack_completed = parse_time(ack_job["completed_at"], "ACK job completion")
    cutoff = parse_time(evaluation_epoch, "ACK evaluation epoch")
    ack_run_url = f"https://github.com/StatPan/datapan-registry/actions/runs/{ack_run['id']}/attempts/{ack_run['run_attempt']}"
    if (
        parse_time(ack_run.get("run_started_at") or ack_run.get("created_at"), "ACK run start") < parse_time(publisher["job_completed_at"], "publisher job completion")
        or ack_completed > cutoff
        or log_result.get("source_sha") != source_sha
        or log_result.get("manifest_sha256") != manifest_sha
        or log_result.get("status") != "read-back-confirmed"
        or log_result.get("run_url") != ack_run_url
    ):
        raise ValueError("native ACK attempt does not follow and acknowledge this exact publisher subject")
    final = new_acks[-1]
    evidence_reference = final.get("evidence_reference")
    if (
        final.get("source_sha") != source_sha
        or final.get("manifest_sha256") != manifest_sha
        or final.get("run_id") != ack_run["id"]
        or final.get("run_attempt") != ack_run["run_attempt"]
        or final.get("run_url") != ack_run_url
        or not isinstance(evidence_reference, str)
        or not evidence_reference.endswith(f"sha256={publisher['receipt_sha256']}")
        or final.get("read_back_verified") is not True
        or final.get("read_back_sha256") != registry_artifact.get("sha256")
        or final.get("read_back_bytes") != registry_artifact.get("bytes")
        or final.get("publication_revision") != publisher["payload_revision"]
        or final.get("publication_pointer_revision") != publisher["pointer_revision"]
    ):
        raise ValueError("ACK journal final read-back does not bind the publisher receipt and exact Registry payload")
    return {
        "run_id": str(ack_run["id"]), "attempt": ack_run["run_attempt"],
        "source_sha": source_sha, "manifest_sha256": manifest_sha,
        "workflow_id": ack_run["workflow_id"], "event": ack_run["event"],
        "workflow_head_sha": ack_run["head_sha"],
        "state_commit": state["after"]["commit"]["sha"] if state["after"] else None,
        "state_tree_sha": state["after"]["tree"]["sha"] if state["after"] else None,
        "journal_sha256": sha256_bytes(after_raw), "journal_record_count": len(after["records"]),
        "acknowledgement_observed_at": final["observed_at"],
    }


def validate_acknowledgement_local_transition(
    *, root: pathlib.Path, scope: dict[str, Any], scoped_inputs: list[dict[str, Any]],
    roles: dict[str, dict[str, Any]], resolved: dict[str, pathlib.Path],
    ack_run: dict[str, Any], ack_job: dict[str, Any], evaluation_epoch: str,
) -> dict[str, Any]:
    """Validate a supplied ACK transition without claiming its absent peer joins.

    A missing source/B/C/Health or publisher stage keeps the overall pipeline
    blocked. It does not make a supplied ACK journal or log opaque: the local
    append-only transition still has to bind to this exact ACK run attempt.
    """
    before_item = single_role(scoped_inputs, "acknowledgement_journal_before", required=False)
    after_item = single_role(scoped_inputs, "acknowledgement_journal_after", required=False)
    if (before_item is None) != (after_item is None):
        raise ValueError("ACK local transition requires both before and after journal snapshots")
    if before_item is None or after_item is None:
        raise ValueError("ACK workflow evidence lacks its append-only journal transition")

    run_id = str(ack_run.get("id"))
    attempt = ack_run.get("run_attempt")
    workflow = PIPELINE_WORKFLOWS["acknowledgement"]
    for item in (before_item, after_item):
        producer = item.get("producer", {})
        if (
            item.get("namespace") not in {"live_operational", "historical_admitted"}
            or str(producer.get("repository", "")).casefold() != "statpan/datapan-registry"
        ):
            raise ValueError("ACK journal snapshot lacks a retained Registry producer identity")
        indexed_attempt = producer.get("attempt")
        indexed_tuple = {"run_id", "attempt", "workflow_id", "workflow_path", "event", "revision"} & set(producer)
        if item.get("namespace") == "live_operational" or indexed_tuple:
            if (
                indexed_tuple != {"run_id", "attempt", "workflow_id", "workflow_path", "event", "revision"}
                or producer.get("run_id") != run_id
                or isinstance(indexed_attempt, bool) or indexed_attempt != attempt
                or producer.get("workflow_id") != workflow["workflow_id"]
                or producer.get("workflow_path") != workflow["workflow_path"]
                or producer.get("event") != ack_run.get("event")
                or producer.get("revision") != ack_run.get("head_sha")
            ):
                raise ValueError("ACK journal snapshot is not attributed to the exact trusted ACK attempt")

    before_raw = receipt_path_bytes(resolved[before_item["input_id"]], before_item)
    after_raw = receipt_path_bytes(resolved[after_item["input_id"]], after_item)
    try:
        before = json.loads(before_raw)
        after = json.loads(after_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("ACK before/after journal snapshot is not valid JSON") from exc
    validate_schema(before, root / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json", "ACK journal before")
    validate_schema(after, root / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json", "ACK journal after")
    ack_revision = str(ack_run.get("head_sha", ""))
    try:
        ack_schema = json.loads(git_read_only(
            root, ["show", f"{ack_revision}:schemas/datapan.canonical-update-promotion-journal.v1.schema.json"],
        ))
        ack_contract = canonical_update_pr_contract_at_revision(root=root, revision=ack_revision)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("ACK journal contract is unavailable at its authenticated workflow revision") from exc
    for label, journal in (("before", before), ("after", after)):
        try:
            validate_schema_value(journal, ack_schema, f"ACK journal {label}")
            ack_contract.validate_journal(journal, ack_schema)
        except Exception as exc:
            raise ValueError(f"ACK journal {label} fails its authenticated producer contract: {exc}") from exc
    if (
        str(before.get("repository", "")).casefold() != "statpan/datapan-registry"
        or str(after.get("repository", "")).casefold() != "statpan/datapan-registry"
        or not isinstance(before.get("records"), list)
        or not isinstance(after.get("records"), list)
    ):
        raise ValueError("ACK journal snapshots have invalid Registry identity or records")

    log_item = roles.get("acknowledgement_log_archive")
    if log_item is None:
        raise ValueError("ACK workflow evidence lacks its exact job log archive")
    log_result = acknowledgement_log_result(
        receipt_path_bytes(resolved[log_item["input_id"]], log_item)
    )
    ack_url = f"https://github.com/StatPan/datapan-registry/actions/runs/{run_id}/attempts/{attempt}"
    run_started = parse_time(
        ack_run.get("run_started_at") or ack_run.get("created_at"), "ACK workflow run start",
    )
    job_started = parse_time(ack_job["started_at"], "ACK job start")
    job_completed = parse_time(ack_job["completed_at"], "ACK job completion")
    cutoff = parse_time(evaluation_epoch, "ACK evaluation epoch")
    if (
        job_completed > cutoff
        or log_result.get("status") not in {"read-back-confirmed", "already_acknowledged"}
        or log_result.get("run_url") != ack_url
        or not isinstance(log_result.get("source_sha"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", log_result["source_sha"])
        or not isinstance(log_result.get("manifest_sha256"), str)
        or not SHA256_RE.fullmatch(log_result["manifest_sha256"])
    ):
        raise ValueError("ACK log does not bind one successful result to the exact ACK attempt")

    before_updated = parse_time(before.get("updated_at"), "ACK journal before update time")
    after_updated = parse_time(after.get("updated_at"), "ACK journal after update time")
    if log_result.get("status") == "already_acknowledged":
        state = validate_acknowledgement_state_bundles(
            roles=roles, resolved=resolved, journal_before_raw=before_raw,
            journal_after_raw=after_raw, require_before=True,
        )
        writes = log_result.get("journal_writes")
        if (
            isinstance(writes, bool) or not isinstance(writes, int) or writes != 0
            or before_raw != after_raw or before_updated != after_updated
            or not before_updated <= run_started <= job_started <= job_completed <= cutoff
        ):
            raise ValueError("already-acknowledged ACK run is not an exact zero-write journal replay")
        row, witness = acknowledged_journal_witness(
            after, source_id=str(scope.get("source_id")),
            source_sha=log_result["source_sha"], manifest_sha256=log_result["manifest_sha256"],
        )
        witness_run = (witness["run_id"], witness["run_attempt"])
        if (
            witness_run == (ack_run.get("id"), attempt)
            or parse_time(witness["observed_at"], "prior ACK witness observation") > before_updated
            or parse_time(witness["observed_at"], "prior ACK witness observation") > run_started
        ):
            raise ValueError("ACK replay does not preserve an earlier independent journal witness")
        return {
            "status": "already_acknowledged", "run_id": run_id, "attempt": attempt,
            "source_sha": log_result["source_sha"],
            "manifest_sha256": log_result["manifest_sha256"],
            "journal_sha256": sha256_bytes(after_raw),
            "journal_record_count": len(after["records"]),
            "journal_writes": 0,
            "acknowledgement_observed_at": witness["observed_at"],
            "witness_run_id": witness_run[0], "witness_attempt": witness_run[1],
            "witness_status": row["status"],
            "state_commit": state["after"]["commit"]["sha"],
            "state_tree_sha": state["after"]["tree"]["sha"],
            "state_replay_unchanged": True,
            "cross_stage_join": "unproven-until-publisher-and-pipeline-stages-are-present",
        }
    if log_result.get("status") != "read-back-confirmed":
        raise ValueError("ACK log has an unsupported successful result")
    if not before_updated <= job_started <= after_updated <= job_completed <= cutoff:
        raise ValueError("ACK journal transition timestamps do not enclose the exact ACK attempt")

    after_matches: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, row in enumerate(after["records"]):
        if not isinstance(row, dict):
            continue
        candidate, pr = row.get("candidate"), row.get("pr")
        acknowledgements = row.get("acknowledgements")
        if not isinstance(candidate, dict) or not isinstance(pr, dict) or not isinstance(acknowledgements, list):
            continue
        matching_acks = [
            ack for ack in acknowledgements
            if isinstance(ack, dict)
            and str(ack.get("run_id")) == run_id
            and ack.get("run_attempt") == attempt
            and ack.get("status") == "read-back-confirmed"
        ]
        if (
            candidate.get("source_id") == scope.get("source_id")
            and candidate.get("repository", "").casefold() == "statpan/datapan-registry"
            and len(matching_acks) == 1
        ):
            after_matches.append((index, row, matching_acks[0]))
    if len(after_matches) != 1:
        raise ValueError("ACK after journal does not identify one read-back transition for this source and attempt")

    target_index, after_row, final_ack = after_matches[0]
    before_records = before["records"]
    if len(before_records) != len(after["records"]):
        raise ValueError("ACK transition changes the promotion-journal record inventory")
    before_row = before_records[target_index]
    if not isinstance(before_row, dict) or not isinstance(before_row.get("candidate"), dict):
        raise ValueError("ACK before journal target record is malformed")
    candidate = after_row["candidate"]
    pr = after_row["pr"]
    before_candidate = before_row.get("candidate")
    before_pr = before_row.get("pr")
    before_pr_number = before_pr.get("number") if isinstance(before_pr, dict) else None
    after_pr_number = pr.get("number") if isinstance(pr, dict) else None
    if (
        isinstance(before_pr_number, bool) or not isinstance(before_pr_number, int) or before_pr_number < 1
        or isinstance(after_pr_number, bool) or not isinstance(after_pr_number, int)
        or after_pr_number != before_pr_number
    ):
        raise ValueError("ACK transition does not preserve one exact positive PR number from its before snapshot")
    if (
        not isinstance(before_candidate, dict) or not isinstance(before_pr, dict)
        or after_row.get("status") != "read-back-confirmed"
        or pr.get("state") != "merged"
        or not isinstance(pr.get("merge_commit_sha"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", pr["merge_commit_sha"])
    ):
        raise ValueError("ACK transition does not identify one merged candidate and exact read-back PR")
    predecessor_suffixes = {
        "prepared": ["pending-review", "merged", "publication-pending", "published", "read-back-confirmed"],
        "pending-review": ["merged", "publication-pending", "published", "read-back-confirmed"],
        "merged": ["publication-pending", "published", "read-back-confirmed"],
        "publication-pending": ["published", "read-back-confirmed"],
        "published": ["read-back-confirmed"],
    }
    predecessor_status = before_row.get("status")
    expected_suffix = predecessor_suffixes.get(predecessor_status)
    if expected_suffix is None:
        raise ValueError("ACK transition starts from a status not selected by the publication recovery workflow")
    before_acks, after_acks = before_row.get("acknowledgements"), after_row.get("acknowledgements")
    if (
        not isinstance(before_acks, list) or not isinstance(after_acks, list)
        or after_acks[:len(before_acks)] != before_acks
        or [row.get("status") if isinstance(row, dict) else None for row in after_acks[len(before_acks):]]
        != expected_suffix
    ):
        raise ValueError("ACK transition is not the exact bounded suffix for its selected producer status")
    previous_observation = parse_time(before_acks[-1].get("observed_at"), "ACK prior observation time") if before_acks else None
    for appended in after_acks[len(before_acks):]:
        observed_at = parse_time(appended.get("observed_at"), "ACK appended observation time")
        if not job_started <= observed_at <= job_completed or (previous_observation and observed_at < previous_observation):
            raise ValueError("ACK journal observation is outside the exact attempt or out of order")
        if (
            appended.get("run_id") != ack_run.get("id")
            or isinstance(appended.get("run_attempt"), bool)
            or appended.get("run_attempt") != attempt
            or appended.get("run_url") != ack_url
        ):
            raise ValueError("ACK suffix is not attributed to the exact successful workflow attempt")
        previous_observation = observed_at
    for index, (old_row, new_row) in enumerate(zip(before_records, after["records"], strict=True)):
        if index == target_index:
            continue
        if old_row != new_row:
            raise ValueError("ACK transition changed an unrelated promotion-journal record")

    # Reconstruct the target row with the exact owner helpers from the trusted
    # ACK workflow revision. This admits only producer-supported PR-readback
    # and publication suffixes, rather than treating the status sequence as a
    # mutable whitelist.
    expected_row = copy.deepcopy(before_row)
    if predecessor_status in {"prepared", "pending-review"}:
        ownership = expected_row.get("ownership")
        expected_candidate = expected_row.get("candidate")
        if not isinstance(ownership, dict) or not isinstance(expected_candidate, dict):
            raise ValueError("ACK PR read-back lacks its durable candidate ownership")
        first_observation = after_acks[len(before_acks)].get("observed_at")
        pr_state = str(pr.get("state", "")).upper()
        readback = {
            "number": pr.get("number"),
            "url": pr.get("url"),
            "body": ownership.get("body"),
            "headRefName": ownership.get("branch"),
            "baseRefName": "main",
            "headRefOid": expected_candidate.get("head_sha"),
            "state": pr_state,
            "mergeCommit": {"oid": pr.get("merge_commit_sha")},
        }
        try:
            expected_row = ack_contract.record_pr_readback(
                expected_row, readback, observed_at=first_observation, run_url=ack_url,
            )
        except Exception as exc:
            raise ValueError("ACK PR read-back does not satisfy the owning canonical-update contract") from exc
    expected_ack_count = len(expected_row.get("acknowledgements", []))
    if after_acks[:expected_ack_count] != expected_row.get("acknowledgements"):
        raise ValueError("ACK PR-readback prefix differs from the owning canonical-update contract")
    for acknowledgement in after_acks[expected_ack_count:]:
        try:
            expected_row = ack_contract.record_acknowledgement(
                expected_row, copy.deepcopy(acknowledgement),
            )
        except Exception as exc:
            raise ValueError("ACK publication suffix fails the owning canonical-update transition contract") from exc
    if expected_row != after_row:
        raise ValueError("ACK journal target differs from the owning PR/publication reconciliation result")

    identity = final_ack.get("artifact_identity")
    source_sha = pr["merge_commit_sha"]
    if (
        final_ack.get("source_sha") != source_sha
        or source_sha != log_result.get("source_sha")
        or final_ack.get("manifest_sha256") != candidate.get("manifest_sha256")
        or final_ack.get("manifest_sha256") != log_result.get("manifest_sha256")
        or not isinstance(identity, dict)
        or identity.get("path") != candidate.get("registry_path")
        or identity.get("bytes") != candidate.get("registry_bytes")
        or identity.get("sha256") != candidate.get("registry_sha256")
        or final_ack.get("read_back_verified") is not True
        or final_ack.get("read_back_sha256") != identity.get("sha256")
        or final_ack.get("read_back_bytes") != identity.get("bytes")
        or isinstance(final_ack.get("run_attempt"), bool)
        or not isinstance(final_ack.get("run_attempt"), int)
        or final_ack.get("run_attempt") != attempt
        or final_ack.get("run_id") != ack_run.get("id")
        or final_ack.get("run_url") != ack_url
        or not isinstance(final_ack.get("evidence_reference"), str)
        or not re.search(r"sha256=[a-f0-9]{64}(?:\b|$)", final_ack["evidence_reference"])
        or not isinstance(final_ack.get("publication_revision"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", final_ack["publication_revision"])
        or not isinstance(final_ack.get("publication_pointer_revision"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", final_ack["publication_pointer_revision"])
    ):
        raise ValueError("ACK final journal identity differs from its merged candidate or exact log result")

    receipt_item = single_role(scoped_inputs, "publication_receipt", required=False)
    if receipt_item is not None:
        receipt_raw = receipt_path_bytes(resolved[receipt_item["input_id"]], receipt_item)
        receipt_sha = sha256_bytes(receipt_raw)
        evidence_reference = final_ack.get("evidence_reference")
        if not isinstance(evidence_reference, str) or f"sha256={receipt_sha}" not in evidence_reference:
            raise ValueError("ACK final journal does not reference the exact supplied publication receipt bytes")

    state = validate_acknowledgement_state_bundles(
        roles=roles, resolved=resolved, journal_before_raw=before_raw,
        journal_after_raw=after_raw,
    )

    return {
        "status": "read-back-confirmed",
        "run_id": run_id, "attempt": attempt, "source_sha": source_sha,
        "manifest_sha256": candidate["manifest_sha256"],
        "journal_sha256": sha256_bytes(after_raw),
        "acknowledgement_observed_at": final_ack.get("observed_at"),
        "state_commit": state["after"]["commit"]["sha"] if state["after"] else None,
        "state_tree_sha": state["after"]["tree"]["sha"] if state["after"] else None,
        "cross_stage_join": "unproven-until-publisher-and-pipeline-stages-are-present",
    }


def validate_updated_claim_inputs(
    *,
    root: pathlib.Path,
    scope: dict[str, Any],
    scoped_inputs: list[dict[str, Any]],
    resolved: dict[str, pathlib.Path],
    proof: dict[str, Any],
    source_input: dict[str, Any],
    scope_registry: dict[str, Any],
    evaluation_epoch: str,
    pipeline_evidence: dict[str, Any] | None = None,
    retained_registry_payload_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Prove an updated claim from exact import, main, publisher, ACK, and readback evidence."""
    if proof["import_durability"]["state"] != "proven":
        raise ValueError("updated claim does not declare the validated import lineage as proven")

    publisher_roles = stage_input_map(scoped_inputs, "publisher")
    ack_roles = stage_input_map(scoped_inputs, "acknowledgement")
    if "consumer_readback" not in publisher_roles:
        raise ValueError("updated claim requires a producer-attributed immutable consumer stream report")
    present_ack_state_after = set(ack_roles) & ACKNOWLEDGEMENT_STATE_AFTER_ROLES
    present_ack_state_before = set(ack_roles) & ACKNOWLEDGEMENT_STATE_BEFORE_ROLES
    if present_ack_state_after and present_ack_state_after != ACKNOWLEDGEMENT_STATE_AFTER_ROLES:
        raise ValueError("updated claim has an incomplete optional ACK after-state tree bundle")
    if present_ack_state_before and present_ack_state_before != ACKNOWLEDGEMENT_STATE_BEFORE_ROLES:
        raise ValueError("updated claim has an incomplete optional ACK before-state tree bundle")
    if present_ack_state_before and not present_ack_state_after:
        raise ValueError("updated claim ACK before-state bundle lacks its matching after-state bundle")
    publisher_run, publisher_job, _publisher_started, _publisher_completed = validate_stage_run(
        stage="publisher", roles=publisher_roles, resolved=resolved,
        evaluation_epoch=evaluation_epoch, root=root,
    )

    publication_item = single_role(scoped_inputs, "publication_receipt")
    binding_item = single_role(scoped_inputs, "publication_source_binding")
    readback_item = single_role(scoped_inputs, "consumer_readback")
    source_commit_item = single_role(scoped_inputs, "publication_source_commit")
    source_manifest_item = single_role(scoped_inputs, "publication_source_manifest")
    workflow_item = single_role(scoped_inputs, "publication_workflow")
    assert publication_item and binding_item and readback_item and source_commit_item and source_manifest_item and workflow_item

    def exact_input(item: dict[str, Any], label: str) -> bytes:
        return receipt_path_bytes(resolved[item["input_id"]], item)

    def require_publisher_attribution(item: dict[str, Any], label: str) -> None:
        producer = item["producer"]
        if (
            item.get("namespace") not in {"live_operational", "historical_admitted"}
            or producer.get("repository", "").casefold() != "statpan/datapan-registry"
        ):
            raise ValueError(f"updated claim {label} is not retained Registry publisher evidence")
        tuple_keys = {"run_id", "attempt", "workflow_id", "workflow_path", "event", "revision"}
        supplied_tuple = tuple_keys & set(producer)
        if supplied_tuple and (
            supplied_tuple != tuple_keys
            or producer.get("run_id") != str(publisher_run["id"])
            or isinstance(producer.get("attempt"), bool)
            or producer.get("attempt") != publisher_run["run_attempt"]
            or producer.get("workflow_id") != PIPELINE_WORKFLOWS["publisher"]["workflow_id"]
            or producer.get("workflow_path") != PIPELINE_WORKFLOWS["publisher"]["workflow_path"]
            or producer.get("event") != publisher_run["event"]
            or producer.get("revision") != publisher_run["head_sha"]
        ):
            raise ValueError(f"updated claim {label} has contradictory publisher attempt metadata")

    for label, item in (
        ("publication receipt", publication_item),
        ("source binding", binding_item),
    ):
        require_publisher_attribution(item, label)
    receipt_raw = exact_input(publication_item, "publication receipt")
    binding_raw = exact_input(binding_item, "publication source binding")
    publication_receipt = json.loads(receipt_raw)
    publication_record = publication_receipt.get("publication")
    if not isinstance(publication_record, dict):
        raise ValueError("native publisher receipt lacks its exact publication record")
    run_item = publisher_roles["publication_producer_run"]
    jobs_item = publisher_roles["publication_producer_jobs"]
    artifact_item = publisher_roles["publication_output_artifact"]
    archive_item = publisher_roles["publication_artifact_archive"]
    run = object_at(resolved[run_item["input_id"]], "publication workflow run")
    jobs = object_at(resolved[jobs_item["input_id"]], "publication workflow jobs")
    artifact_response = object_at(resolved[artifact_item["input_id"]], "publication workflow artifacts")
    archive = exact_input(archive_item, "publication artifact archive")
    publication = validate_native_publisher_attempt(
        root=root, run=run, jobs=jobs, artifact_response=artifact_response,
        archive=archive, receipt_raw=receipt_raw, binding_raw=binding_raw,
        evaluation_epoch=evaluation_epoch,
    )
    if (
        str(run["id"]) != str(publisher_run["id"])
        or run["run_attempt"] != publisher_run["run_attempt"]
        or run["head_sha"] != publisher_run["head_sha"]
    ):
        raise ValueError("publisher stage evidence and receipt verifier selected different run attempts")

    source_binding = publication["source_binding"]
    source_sha = publication["source_sha"]
    manifest_sha = publication["manifest_sha256"]
    source_snapshot = proof["source_snapshot"]
    source_bytes = exact_input(source_input, "authoritative source snapshot")
    if source_input["namespace"] == "fixture":
        raise ValueError("updated claim source snapshot must be committed Registry evidence, not a fixture")
    if (
        source_snapshot.get("sha256") != source_input["sha256"]
        or source_input["bytes"] != len(source_bytes)
        or source_snapshot.get("revision") != source_input.get("subject", {}).get("source_revision")
        or source_snapshot.get("revision") != source_input["producer"].get("revision")
        or source_input.get("observed_at") != source_snapshot.get("observed_at")
    ):
        raise ValueError("proof source identity, producer revision, observation time, and exact indexed bytes differ")
    input_subject_matches(source_input, {
        "source_sha256": source_snapshot["sha256"],
        "scope_id": scope["scope_id"],
        "source_id": scope["source_id"],
        "resource_kind": scope["resource_kind"],
        "identity_algorithm": scope["identity_algorithm"],
    }, "authoritative source snapshot")
    admission_revision = source_input.get("subject", {}).get("admission_revision")
    if not isinstance(admission_revision, str):
        if source_input["producer"].get("repository", "").casefold() == "statpan/datapan-registry":
            admission_revision = source_input["producer"].get("revision")
        else:
            raise ValueError("external proof source lacks its distinct Registry admission revision")
    validate_committed_release_artifact(
        root=root, revision=admission_revision, path=source_input["path"],
        expected_sha256=source_snapshot["sha256"], expected_bytes=source_input["bytes"],
        label="Registry-admitted proof source snapshot", expected_content=source_bytes,
    )

    manifest_raw = git_read_only(root, ["show", f"{source_sha}:manifest.json"])
    if sha256_bytes(manifest_raw) != manifest_sha:
        raise ValueError("publisher source release manifest differs from its signed receipt binding")
    release_manifest = json.loads(manifest_raw)
    publication_artifact = proof.get("publication", {}).get("artifact", {})
    publication_path = normalize_relative_path(
        publication_artifact.get("path"), "proof publication artifact path"
    ).as_posix()
    release_artifact_rows = [
        row for row in release_manifest.get("artifacts", [])
        if isinstance(row, dict) and row.get("path") == publication_path
    ]
    if len(release_artifact_rows) != 1:
        raise ValueError("proof publication artifact is absent or ambiguous in the exact published Registry manifest")
    release_artifact = release_artifact_rows[0]
    if release_artifact.get("sha256") != publication_artifact.get("sha256"):
        raise ValueError("proof publication artifact digest differs from the exact release-manifest row")
    manifest_artifacts = release_manifest.get("artifacts")
    if not isinstance(manifest_artifacts, list) or not manifest_artifacts:
        raise ValueError("native publisher source release manifest has no artifact inventory")
    published_artifact_binding = validate_committed_release_artifact(
        root=root, revision=source_sha, path=publication_path,
        expected_sha256=publication_artifact["sha256"], expected_bytes=release_artifact["bytes"],
        label="published proof artifact",
    )
    if published_artifact_binding["release_manifest_sha256"] != manifest_sha:
        raise ValueError("proof artifact and source do not share one exact published release manifest")

    source_commit = object_at(resolved[source_commit_item["input_id"]], "publisher source commit API")
    source_manifest_api = object_at(resolved[source_manifest_item["input_id"]], "publisher source manifest API")
    workflow_api = object_at(resolved[workflow_item["input_id"]], "publisher workflow source API")
    if (
        source_commit.get("sha") != source_sha
        or source_commit.get("commit", {}).get("tree", {}).get("sha") != source_binding.get("source_tree_sha")
        or source_manifest_item.get("subject", {}).get("source_revision") != source_sha
        or (
            workflow_item.get("subject", {}).get("source_revision") is not None
            and workflow_item.get("subject", {}).get("source_revision") != publication["workflow_head_sha"]
        )
    ):
        raise ValueError("publisher source commit and API snapshot roles do not bind distinct exact source/workflow revisions")
    source_manifest_content = exact_input(source_manifest_item, "publisher source manifest snapshot")
    source_script_raw = git_read_only(
        root, ["show", f"{source_sha}:scripts/huggingface_registry_distribution.py"],
    )
    workflow_path = PIPELINE_WORKFLOWS["publisher"]["workflow_path"]
    if (
        workflow_api.get("id") != PIPELINE_WORKFLOWS["publisher"]["workflow_id"]
        or workflow_api.get("path") != workflow_path
    ):
        raise ValueError("publisher workflow API does not identify the registered native publisher")
    workflow_content = git_read_only(
        root, ["show", f"{publication['workflow_head_sha']}:{workflow_path}"],
    )
    if (
        source_manifest_content != manifest_raw
        or not workflow_content
    ):
        raise ValueError("publisher source manifest or workflow file differs from its exact committed revision")

    # Authenticate the public distribution pointer and exact anonymous manifest
    # alongside the native receipt. These roles are evidence, not a readiness
    # or receipt-cutover contract.
    pointer_roles = {
        "publication_repo_metadata_before", "publication_pointer_before",
        "publication_repo_metadata_after", "publication_pointer_after",
        "publication_pointer_immutable", "publication_anonymous_manifest",
    }
    pointer_items = {role: single_role(scoped_inputs, role) for role in pointer_roles}
    if any(item is None for item in pointer_items.values()):
        raise ValueError("updated claim requires before/after/immutable public pointer and manifest evidence")
    for role, item in pointer_items.items():
        assert item is not None
        require_publisher_attribution(item, role)
    pointer_before = object_at(resolved[pointer_items["publication_pointer_before"]["input_id"]], "publisher pointer before")
    pointer_after = object_at(resolved[pointer_items["publication_pointer_after"]["input_id"]], "publisher pointer after")
    pointer_immutable = object_at(resolved[pointer_items["publication_pointer_immutable"]["input_id"]], "immutable publisher pointer")
    metadata_before = object_at(resolved[pointer_items["publication_repo_metadata_before"]["input_id"]], "publisher repo metadata before")
    metadata_after = object_at(resolved[pointer_items["publication_repo_metadata_after"]["input_id"]], "publisher repo metadata after")
    anonymous_manifest_raw = exact_input(pointer_items["publication_anonymous_manifest"], "anonymous release manifest")
    anonymous_manifest = json.loads(anonymous_manifest_raw)
    if anonymous_manifest_raw != manifest_raw or sha256_bytes(anonymous_manifest_raw) != manifest_sha:
        raise ValueError("anonymous immutable release manifest differs from the exact published source manifest")
    registry_rows = [
        row for row in release_manifest.get("artifacts", [])
        if isinstance(row, dict) and row.get("path") == "data/data-go-kr.registry.json"
    ]
    if len(registry_rows) != 1:
        raise ValueError("published release manifest lacks one exact canonical Registry artifact")
    registry_artifact = registry_rows[0]
    if registry_artifact.get("path") != "data/data-go-kr.registry.json":
        raise ValueError("updated claim artifact is not a unique exact row in the immutable release")
    pointer_validator = import_module(
        "generic_distribution_pointer_contract", root / "scripts/completeness_publication_evidence.py"
    )
    for label, metadata, index in (
        ("after", metadata_after, pointer_after),
        ("immutable", metadata_after, pointer_immutable),
    ):
        pointer_validator._check_distribution_index(
            index, payload_revision=publication["payload_revision"],
            pointer_revision=publication["pointer_revision"], manifest_sha256=manifest_sha,
            manifest_bytes=len(manifest_raw), registry_sha256=registry_artifact["sha256"],
            registry_bytes=registry_artifact["bytes"],
            artifact_count=publication_record.get("artifacts"), label=f"publisher {label} pointer",
        )
        if metadata.get("id") != "StatPan/datapan-registry" or metadata.get("sha") != publication["pointer_revision"]:
            raise ValueError(f"publisher {label} repository metadata does not bind its immutable pointer revision")
    if (
        metadata_before.get("id") != "StatPan/datapan-registry"
        or not isinstance(metadata_before.get("sha"), str)
        or not isinstance(pointer_before.get("dataset"), dict)
        or pointer_before.get("dataset", {}).get("id") != "StatPan/datapan-registry"
    ):
        raise ValueError("publisher before-pointer snapshot lacks exact public repository identity")
    if pointer_after != pointer_immutable:
        raise ValueError("publisher immutable distribution pointer differs from the post-publication pointer")
    inventory = validate_native_distribution_inventory(
        manifest=release_manifest,
        manifest_raw=manifest_raw,
        pointer=pointer_immutable,
        receipt=publication_receipt,
        source_script_raw=source_script_raw,
        workflow_raw=workflow_content,
    )
    verified_artifacts = {row["path"]: row for row in inventory["verified_artifacts"]}
    release_artifact = verified_artifacts.get(publication_path)
    if release_artifact is None:
        raise ValueError("proof publication artifact is not a member of the authenticated native verification set")
    if (
        release_artifact["sha256"] != publication_artifact.get("sha256")
        or release_artifact["sha256"] != release_artifact_rows[0].get("sha256")
        or release_artifact["bytes"] != release_artifact_rows[0].get("bytes")
    ):
        raise ValueError("proof publication artifact differs from its authenticated native inventory identity")
    publication_subject = validate_scope_publication_subject(
        root=root,
        scope=scope,
        source_input=source_input,
        source_snapshot=source_snapshot,
        source_bytes=source_bytes,
        publication_artifact=publication_artifact,
        release_member=release_artifact,
        release_manifest=release_manifest,
        publication_source_sha=source_sha,
        pipeline_evidence=pipeline_evidence,
        retained_registry_payload_bytes=retained_registry_payload_bytes,
    )
    imported = validate_scope_import_evidence(
        root=root, scope=scope, scoped_inputs=scoped_inputs, resolved=resolved,
        proof=proof, source_input=source_input, publication_subject=publication_subject,
        publication_source_sha=source_sha, publication_manifest_sha256=manifest_sha,
        release_manifest=release_manifest, pipeline_evidence=pipeline_evidence,
    )
    if proof["import_durability"].get("receipt") != imported.get("receipt"):
        raise ValueError("updated proof import receipt does not name the exact admitted scope-import evidence")
    native_inventory_fact = {
        "source_manifest_artifacts": inventory["source_manifest_artifacts"],
        "distribution_artifacts": inventory["distribution_artifacts"],
        "workflow_extras": inventory["workflow_extras"],
        "verifier_contract_sha256": inventory["verifier_contract_sha256"],
        "artifact": release_artifact,
        "verified_at": publication["native_verification_completed_at"],
    }
    readback = object_at(resolved[readback_item["input_id"]], "consumer immutable read-back")
    if readback_item.get("namespace") != "live_operational":
        raise ValueError("consumer stream report must be live operational evidence")
    readback_fact = validate_consumer_readback(
        readback=readback, proof=proof, publication=publication,
        publisher_job=publisher_job, evaluation_epoch=evaluation_epoch,
        publisher_run=publisher_run, expected_bytes=release_artifact["bytes"],
    )

    ack_fact = None
    ack_evidence_roles: set[str] = set()
    if ack_roles and present_ack_state_after:
        ack_run, ack_job, ack_started, _ack_completed = validate_stage_run(
            stage="acknowledgement", roles=ack_roles, resolved=resolved,
            evaluation_epoch=evaluation_epoch, root=root,
        )
        if ack_started < parse_time(publication["job_completed_at"], "publisher completion"):
            raise ValueError("publication acknowledgement started before the authenticated publisher completed")
        ack_subjects = [
            candidate for candidate in scope_registry.get("scopes", [])
            if candidate.get("source_id") == "data_go_kr"
            and candidate.get("resource_kind") == "api_catalog_metadata"
        ]
        if len(ack_subjects) != 1:
            raise ValueError("ACK evidence requires exactly one registered canonical Registry release subject")
        ack_fact = validate_acknowledgement_transition(
            root=root, scope=scope, acknowledgement_scope=ack_subjects[0],
            scoped_inputs=scoped_inputs, roles=ack_roles,
            resolved=resolved, ack_run=ack_run, ack_job=ack_job, publisher=publication,
            publisher_receipt_raw=receipt_raw, release_manifest=release_manifest,
            evaluation_epoch=evaluation_epoch,
        )
        ack_evidence_roles = {
            "acknowledgement_run", "acknowledgement_jobs", "acknowledgement_log_archive",
            "acknowledgement_journal_before", "acknowledgement_journal_after",
            "acknowledgement_state_ref", "acknowledgement_state_commit",
            "acknowledgement_state_tree", "acknowledgement_journal_blob_api",
            *ACKNOWLEDGEMENT_STATE_BEFORE_ROLES,
        }
    evidence_roles = {
        "authoritative_source_snapshot", "authoritative_denominator",
        "historical_import_admission", "historical_import_receipt", "historical_import_attestation",
        "publication_producer_run", "publication_producer_jobs", "publication_output_artifact",
        "publication_artifact_archive", "publication_receipt", "publication_source_binding",
        "publication_source_commit", "publication_source_manifest", "publication_workflow",
        "publication_repo_metadata_before", "publication_pointer_before",
        "publication_repo_metadata_after", "publication_pointer_after",
        "publication_pointer_immutable", "publication_anonymous_manifest", "consumer_readback",
        "promotion_journal_blob_api",
        *ack_evidence_roles,
    }
    evidence_by_path = {
        input_path(item): artifact(input_path(item), exact_input(item, item["role"]))
        for item in scoped_inputs if item["role"] in evidence_roles
    }
    proof_item = single_role(scoped_inputs, "proof_v1")
    if proof_item is not None:
        proof_raw = exact_input(proof_item, "updated completeness proof")
        evidence_by_path[input_path(proof_item)] = artifact(input_path(proof_item), proof_raw)
    return {
        "source": {
            "source_revision": source_snapshot["revision"],
            "admission_revision": admission_revision,
            "published_source_commit": source_sha,
            "release_manifest_sha256": manifest_sha,
            "artifact_path": normalize_relative_path(
                source_input["path"], "authoritative source artifact path",
            ).as_posix(),
            "artifact_sha256": source_snapshot["sha256"],
        },
        "import": imported,
        "publisher": publication,
        "native_verified_distribution": native_inventory_fact,
        "consumer_read_back": readback_fact,
        "publication_subject": publication_subject,
        "acknowledgement": ack_fact,
        "evidence": [evidence_by_path[path] for path in sorted(evidence_by_path)],
    }



def local_inventory_context(
    *,
    root: pathlib.Path,
    scope: dict[str, Any],
    scoped_inputs: list[dict[str, Any]],
    resolved: dict[str, pathlib.Path],
    candidate_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    inventory = scope["inventory"]
    source_profile = [item for item in scoped_inputs if item["role"] == "source_profile"]
    evidence: list[dict[str, Any]] = []
    for item in source_profile:
        evidence.append(artifact(input_path(item), read_bytes(resolved[item["input_id"]])))
        profile = object_at(resolved[item["input_id"]], f"{scope['scope_id']} source profile")
        if profile.get("source_id") != scope["source_id"] or profile.get("provider") != scope["provider"]:
            raise ValueError(f"scope {scope['scope_id']} source profile identity differs")

    count: int | None = None
    identity_algorithm = scope["identity_algorithm"]
    state = "missing"
    if inventory["kind"] != "none":
        matches = [item for item in scoped_inputs if item["role"] == inventory["input_role"]]
        if len(matches) != 1:
            raise ValueError(f"scope {scope['scope_id']} has an ambiguous local inventory input")
        item = matches[0]
        path = resolved[item["input_id"]]
        data = read_bytes(path)
        evidence.append(artifact(input_path(item), data))
        state = "observed"
        if inventory["kind"] == "registry_snapshot":
            # The mixed registry has no admitted catalog-level identity denominator.
            count = None
        elif inventory["kind"] == "operation_manifest":
            if candidate_context is not None and scope["source_id"] == "data_go_kr":
                manifest = validate_candidate_operation_manifest(
                    root, path, root / "data/data-go-kr.registry.json",
                )
            else:
                manifest = validate_data_go_operation_manifest(root, path)
            count = manifest["summary"]["api_operations"]
            if manifest["identity_contract"]["algorithm"] != scope["identity_algorithm"]:
                raise ValueError(f"scope {scope['scope_id']} identity algorithm differs from #605 manifest")
            if manifest["identity_contract"]["fields"] != scope["identity_fields"]:
                raise ValueError(f"scope {scope['scope_id']} identity fields differ from #605 manifest")
        elif inventory["kind"] == "operation_denominator":
            denominator = validate_operation_denominator(root, path, scope["source_id"])
            count = denominator["summary"]["operations"]
            if denominator["identity_fields"] != scope["identity_fields"]:
                raise ValueError(f"scope {scope['scope_id']} local operation identity fields differ")
        else:
            raise ValueError(f"unsupported inventory adapter: {inventory['kind']}")

    return {
        "state": state,
        "authoritative": False,
        "local_identity_count": count,
        "identity_algorithm": identity_algorithm,
        "evidence": sorted(evidence, key=lambda item: item["path"]),
    }, evidence


def missing_entry(code: str, owner: str, ticket: str) -> dict[str, str]:
    if not TICKET_RE.fullmatch(ticket):
        raise ValueError(f"invalid blocker ticket: {ticket}")
    return {"code": code, "owner": owner, "ticket": ticket}


def dedupe_missing(values: list[dict[str, str]]) -> list[dict[str, str]]:
    unique = {(item["code"], item["owner"], item["ticket"]): item for item in values}
    return [unique[key] for key in sorted(unique)]


def proof_for_scope(
    *,
    root: pathlib.Path,
    scope: dict[str, Any],
    scoped_inputs: list[dict[str, Any]],
    resolved: dict[str, pathlib.Path],
    policy: dict[str, Any],
    scope_registry: dict[str, Any],
    evaluation_epoch: str,
    pipeline_evidence: dict[str, Any] | None = None,
    candidate_subject_changed: bool = False,
    candidate_baseline_registry_bytes: bytes | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    matches = [item for item in scoped_inputs if item["role"] == "proof_v1"]
    if not matches:
        return None, None
    if len(matches) != 1:
        raise ValueError(f"scope {scope['scope_id']} has multiple v1 proofs")
    item = matches[0]
    if item["namespace"] != "live_operational":
        if item["namespace"] != "historical_admitted":
            raise ValueError(f"supplied v1 proof for {scope['scope_id']} is not admitted evidence")
    proof_path = resolved[item["input_id"]]
    proof = object_at(proof_path, f"{scope['scope_id']} v1 proof")
    module = import_module("completeness_proof_contract_for_scope", root / "scripts/validate-completeness-proof.py")
    module.POLICY = root / POLICY_PATH
    module.POLICY_SCHEMA = root / "schemas/datapan.completeness-proof-policy.v1.schema.json"
    module.PROOF_SCHEMA = root / "schemas/datapan.completeness-proof.v1.schema.json"
    module.validate_proof(proof, policy)
    registered = proof["scope"]
    if registered["scope_id"] != scope["scope_id"] or registered["source_id"] != scope["source_id"]:
        raise ValueError(f"supplied proof scope identity does not match {scope['scope_id']}")
    if registered["provider"] != scope["provider"] or registered["resource_kind"] != scope["resource_kind"]:
        raise ValueError(f"supplied proof provider or resource kind differs for {scope['scope_id']}")
    if registered["selector"] != scope["selector"] or registered["identity_algorithm"] != scope["identity_algorithm"]:
        raise ValueError(f"supplied proof selector or identity algorithm differs for {scope['scope_id']}")
    if proof["freshness"]["as_of"] != evaluation_epoch:
        raise ValueError("supplied proof freshness epoch differs from the rollup evaluation epoch")
    verify_main_committed_input(root=root, item=item, path=proof_path, label="completeness proof")
    source_input = single_role(scoped_inputs, "authoritative_source_snapshot")
    assert source_input is not None
    source_path = resolved[source_input["input_id"]]
    registered_source_path = registered_scope_source_path(scope)
    if registered_source_path is None or normalize_relative_path(
        source_input["path"], "authoritative scope source path",
    ).as_posix() != registered_source_path:
        raise ValueError("authoritative source snapshot does not use the registered scope source selector")
    verify_main_committed_input(root=root, item=source_input, path=source_path, label="authoritative source snapshot")
    source_bytes = receipt_path_bytes(source_path, source_input)
    if source_input["namespace"] == "fixture" or sha256_bytes(source_bytes) != proof["source_snapshot"]["sha256"]:
        raise ValueError(f"scope {scope['scope_id']} source-snapshot bytes do not bind the supplied proof")
    if source_input.get("observed_at") != proof["source_snapshot"]["observed_at"]:
        raise ValueError("proof source observation time differs from exact source snapshot input")
    if source_input.get("subject", {}).get("source_revision") != proof["source_snapshot"]["revision"]:
        raise ValueError("proof source revision differs from the typed source snapshot input")
    input_subject_matches(source_input, {
        "source_sha256": proof["source_snapshot"]["sha256"],
        "scope_id": scope["scope_id"],
        "source_id": scope["source_id"],
        "resource_kind": scope["resource_kind"],
        "identity_algorithm": scope["identity_algorithm"],
    }, "authoritative source snapshot")
    if any(proof["claims"].values()) or proof["scope"]["authority"]["state"] == "available":
        if scope["authority_state"] != "available" or scope["blockers"]:
            raise ValueError("positive proof is unsupported while the registered scope authority remains unresolved")
        denominator = validate_denominator_attestation(
            root=root, scope=scope, scoped_inputs=scoped_inputs, resolved=resolved, proof=proof,
            baseline_registry_bytes=(candidate_baseline_registry_bytes if candidate_subject_changed else None),
        )
        if proof["scope"]["authority"]["state"] != "available":
            raise ValueError("positive or authoritative proof must declare available authority")
        if proof["scope"]["authority"]["evidence"]["sha256"] != source_input["sha256"]:
            raise ValueError("proof authority evidence is not the exact admitted source snapshot")
    updated_delivery = None
    if proof["claims"]["updated"]:
        updated_delivery = validate_updated_claim_inputs(
            root=root, scope=scope, scoped_inputs=scoped_inputs, resolved=resolved,
            proof=proof, source_input=source_input, scope_registry=scope_registry,
            evaluation_epoch=evaluation_epoch, pipeline_evidence=pipeline_evidence,
            retained_registry_payload_bytes=(
                candidate_baseline_registry_bytes if candidate_subject_changed else None
            ),
        )
    if candidate_subject_changed:
        # Validate the retained proof and its own admitted source/import chain
        # before keeping it as historical context. A candidate-local inventory
        # change suppresses applicability and claims; it never hides malformed
        # or contradictory retained evidence.
        return proof, None
    return proof, updated_delivery


def validate_pipeline_evidence(
    *,
    root: pathlib.Path,
    operation_inputs: list[dict[str, Any]],
    all_inputs: list[dict[str, Any]],
    resolved: dict[str, pathlib.Path],
    evaluation_epoch: str,
    scope_registry: dict[str, Any],
    candidate_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Admit the retained source→processor→promotion→Health chain and publication.

    The pipeline packets are independent stage records. Every stage is validated
    against its fixed workflow identity and exact run attempt before the cross-
    stage generation, journal, source-observation, and payload identities are
    compared. The publication facet is separately historical and cannot make a
    #631 current/updated claim.
    """
    core_stages = ("source", "processor", "promotion", "health")
    relevant_stages = (*core_stages, "publisher", "acknowledgement")
    stage_roles = {stage: stage_input_map(operation_inputs, stage) for stage in relevant_stages}
    if not any(stage_roles.values()):
        return None
    scope_ids = {item.get("scope_id") for item in operation_inputs}
    registered_scopes = [
        scope for scope in scope_registry.get("scopes", [])
        if scope.get("scope_id") in scope_ids
    ]
    if len(scope_ids) != 1 or len(registered_scopes) != 1:
        raise ValueError("pipeline evidence must be bound to exactly one registered scope")
    operation_scope = registered_scopes[0]
    stage_runs: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for stage, roles in stage_roles.items():
        if roles:
            run, job, _started, _completed = validate_stage_run(
                stage=stage, roles=roles, resolved=resolved,
                evaluation_epoch=evaluation_epoch, root=root,
            )
            stage_runs[stage] = (run, job)

    current_registry = validate_current_catalog_subject(
        root=root, inputs=all_inputs, resolved=resolved, scope_registry=scope_registry,
        candidate_context=candidate_context,
    )

    def raw_role(role: str) -> bytes:
        item = single_role(operation_inputs, role, required=False)
        if item is None:
            raise ValueError(f"publication evidence is missing role {role}")
        return receipt_path_bytes(resolved[item["input_id"]], item)

    def json_role(role: str) -> dict[str, Any]:
        item = single_role(operation_inputs, role, required=False)
        if item is None:
            raise ValueError(f"publication evidence is missing role {role}")
        return object_at(resolved[item["input_id"]], role)

    publisher_roles = stage_roles["publisher"]
    publisher_run = stage_runs.get("publisher", ({}, {}))[0]
    receipt_item = single_role(operation_inputs, "publication_receipt", required=False)
    binding_item = single_role(operation_inputs, "publication_source_binding", required=False)
    if (receipt_item is None) != (binding_item is None):
        raise ValueError("native publisher receipt and source binding must be supplied together")
    publisher_attempt: dict[str, Any] | None = None
    publisher_readback: dict[str, Any] | None = None
    acknowledgement_local: dict[str, Any] | None = None
    acknowledgement_transition: dict[str, Any] | None = None
    if publisher_roles and receipt_item is not None and binding_item is not None:
        publisher_attempt = validate_native_publisher_attempt(
            root=root,
            run=publisher_run,
            jobs=json_role("publication_producer_jobs"),
            artifact_response=json_role("publication_output_artifact"),
            archive=raw_role("publication_artifact_archive"),
            receipt_raw=raw_role("publication_receipt"),
            binding_raw=raw_role("publication_source_binding"),
            evaluation_epoch=evaluation_epoch,
        )
    publisher_readback = validate_native_publication_readback(
        root=root, inputs=operation_inputs, resolved=resolved,
        publisher_run=publisher_run, publisher=publisher_attempt,
        evaluation_epoch=evaluation_epoch,
    )
    if stage_roles["acknowledgement"]:
        acknowledgement_run, acknowledgement_job = stage_runs["acknowledgement"]
        acknowledgement_local = validate_acknowledgement_local_transition(
            root=root, scope=operation_scope, scoped_inputs=operation_inputs,
            roles=stage_roles["acknowledgement"], resolved=resolved,
            ack_run=acknowledgement_run, ack_job=acknowledgement_job,
            evaluation_epoch=evaluation_epoch,
        )
        if publisher_attempt is not None and receipt_item is not None:
            publisher_manifest_raw = git_read_only(
                root, ["show", f"{publisher_attempt['source_sha']}:manifest.json"]
            )
            publisher_manifest = json.loads(publisher_manifest_raw)
            catalog_scopes = [
                candidate for candidate in scope_registry.get("scopes", [])
                if candidate.get("source_id") == operation_scope.get("source_id")
                and candidate.get("resource_kind") == "api_catalog_metadata"
            ]
            if len(catalog_scopes) != 1:
                raise ValueError("ACK cross-stage join requires one registered catalog publication subject")
            acknowledgement_transition = validate_acknowledgement_transition(
                root=root, scope=operation_scope, acknowledgement_scope=catalog_scopes[0],
                scoped_inputs=operation_inputs, roles=stage_roles["acknowledgement"],
                resolved=resolved, ack_run=acknowledgement_run, ack_job=acknowledgement_job,
                publisher=publisher_attempt,
                publisher_receipt_raw=raw_role("publication_receipt"),
                release_manifest=publisher_manifest, evaluation_epoch=evaluation_epoch,
                local_transition=acknowledgement_local,
            )
    orphan_ack_rows = [
        item for item in operation_inputs
        if item["role"].startswith("acknowledgement_")
        and item.get("subject", {}).get("stage") != "acknowledgement"
    ]
    if orphan_ack_rows and not stage_roles["acknowledgement"]:
        raise ValueError("ACK journal or state evidence is present without its exact acknowledgement run stage")

    missing_core_stages = [stage for stage in core_stages if not stage_roles[stage]]
    if missing_core_stages:
        validate_present_stage_local_artifacts(
            root=root, stage_roles=stage_roles, stage_runs=stage_runs,
            resolved=resolved, current_registry=current_registry,
            evaluation_epoch=evaluation_epoch,
        )
        publication = None
        if publisher_attempt is not None and publisher_readback is not None:
            publication = native_publication_delivery_facet(
                publisher=publisher_attempt, readback=publisher_readback,
                acknowledgement=acknowledgement_transition,
            )
        publication_roles = {
            "publication_producer_run", "publication_producer_jobs", "publication_output_artifact",
            "publication_artifact_archive", "publication_receipt", "publication_source_binding",
            "publication_source_commit", "publication_source_manifest", "publication_workflow",
            *NATIVE_PUBLICATION_READBACK_ROLES,
            "acknowledgement_run", "acknowledgement_jobs", "acknowledgement_log_archive",
            "acknowledgement_journal_before", "acknowledgement_journal_after",
            "acknowledgement_state_ref", "acknowledgement_state_commit",
            "acknowledgement_state_tree", "acknowledgement_journal_blob_api",
            *ACKNOWLEDGEMENT_STATE_BEFORE_ROLES,
        }
        partial_evidence = [
            artifact(input_path(item), receipt_path_bytes(resolved[item["input_id"]], item))
            for item in operation_inputs
            if item.get("subject", {}).get("stage") in {*core_stages, "publisher", "acknowledgement"}
            or item.get("role") in publication_roles
        ]
        publication_evidence = [
            item for item in partial_evidence
            if item["path"] in {
                input_path(row) for row in operation_inputs if row.get("role") in publication_roles
            }
        ]
        return {
            "status": "missing_core_chain",
            "missing_stages": missing_core_stages,
            "evidence": partial_evidence,
            "publication": publication,
            "publisher_attempt": publisher_attempt,
            "publisher_readback": publisher_readback,
            "acknowledgement_local": acknowledgement_local,
            "acknowledgement_transition": acknowledgement_transition,
            "current_registry": current_registry,
            "publication_evidence": publication_evidence,
            "publication_missing_stages": [
                stage for stage, roles in (
                    ("publisher", publisher_roles),
                    ("acknowledgement", stage_roles["acknowledgement"]),
                )
                if not roles or stage == "acknowledgement" and acknowledgement_transition is None
            ],
        }

    source_run, source_job = stage_runs["source"]
    source_roles = stage_roles["source"]
    source_archive_item = source_roles["pipeline_artifact_archive"]
    source_archive = receipt_path_bytes(resolved[source_archive_item["input_id"]], source_archive_item)
    source = validate_source_observation(
        root=root, roles=source_roles, resolved=resolved, run=source_run, job=source_job,
        archive=source_archive, evaluation_epoch=evaluation_epoch,
    )

    processor_run, processor_job = stage_runs["processor"]
    processor = validate_processor_stage(
        root=root, roles=stage_roles["processor"], resolved=resolved,
        run=processor_run, job=processor_job, source=source,
        current_registry=current_registry, evaluation_epoch=evaluation_epoch,
    )
    promotion_run, promotion_job = stage_runs["promotion"]
    promotion = validate_promotion_stage(
        root=root, roles=stage_roles["promotion"], resolved=resolved,
        run=promotion_run, job=promotion_job, processor=processor,
        current_registry=current_registry, evaluation_epoch=evaluation_epoch,
    )
    health_run, health_job = stage_runs["health"]
    health = validate_health_stage(
        root=root, roles=stage_roles["health"], resolved=resolved,
        run=health_run, job=health_job, processor=processor, promotion=promotion,
        source=source, current_registry=current_registry, evaluation_epoch=evaluation_epoch,
    )

    def raw_role(role: str) -> bytes:
        item = single_role(operation_inputs, role, required=False)
        if item is None:
            raise ValueError(f"publication evidence is missing role {role}")
        return receipt_path_bytes(resolved[item["input_id"]], item)

    def json_role(role: str) -> dict[str, Any]:
        item = single_role(operation_inputs, role, required=False)
        if item is None:
            raise ValueError(f"publication evidence is missing role {role}")
        return object_at(resolved[item["input_id"]], role)

    publication_helper = import_module(
        "completeness_publication_evidence", root / "scripts/completeness_publication_evidence.py"
    )
    publication: dict[str, Any] | None = None
    publication_evidence: list[dict[str, Any]] = []
    publication_roles = (
        "publication_producer_run", "publication_producer_jobs", "publication_output_artifact",
        "publication_artifact_archive", "publication_receipt", "publication_source_binding",
        "publication_source_commit", "publication_source_manifest", "publication_workflow",
        "publication_repo_metadata_before", "publication_pointer_before",
        "publication_repo_metadata_after", "publication_pointer_after", "publication_pointer_immutable",
        "publication_anonymous_manifest", "publication_anonymous_payload", "acknowledgement_run",
        "acknowledgement_jobs", "acknowledgement_log_archive", "acknowledgement_journal_before",
        "acknowledgement_journal_after",
        *ACKNOWLEDGEMENT_STATE_AFTER_ROLES, *ACKNOWLEDGEMENT_STATE_BEFORE_ROLES,
    )
    acknowledgement_roles = stage_roles["acknowledgement"]
    acknowledgement_run = stage_runs.get("acknowledgement", ({}, {}))[0]

    # Keep the exact retained legacy packet as a historical adapter. New native
    # attempts are validated through the generic #716 publisher checks in the
    # updated-claim path; their presence can no longer make this legacy-only
    # adapter reject the independently valid source→B→C→Health chain.
    legacy_packet_ids = (
        publisher_run.get("id") == publication_helper.PUBLISHER_RUN_ID
        and publisher_run.get("run_attempt") == publication_helper.PUBLISHER_ATTEMPT
        and acknowledgement_run.get("id") == publication_helper.ACK_RUN_ID
        and acknowledgement_run.get("run_attempt") == publication_helper.ACK_ATTEMPT
    )
    legacy_required_roles = set(publication_roles) - ACKNOWLEDGEMENT_STATE_AFTER_ROLES - ACKNOWLEDGEMENT_STATE_BEFORE_ROLES
    legacy_roles_present = all(
        single_role(operation_inputs, role, required=False) is not None
        for role in legacy_required_roles
    )
    if legacy_packet_ids:
        if not publisher_roles or not acknowledgement_roles or not legacy_roles_present:
            raise ValueError("retained legacy publisher/ACK packet is incomplete")
        source_binding = json_role("publication_source_binding")
        publication = publication_helper.validate_historical_publication_facet(
            inputs={
                "publisher_run": publisher_run,
                "publisher_jobs": json_role("publication_producer_jobs"),
                "publisher_artifact_metadata": json_role("publication_output_artifact"),
                "publisher_archive": raw_role("publication_artifact_archive"),
                "publication_receipt": raw_role("publication_receipt"),
                "source_binding": raw_role("publication_source_binding"),
                "source_commit": json_role("publication_source_commit"),
                "source_manifest": raw_role("publication_source_manifest"),
                "publisher_workflow": json_role("publication_workflow"),
                "pointer_before": {
                    "repo_metadata": json_role("publication_repo_metadata_before"),
                    "distribution_index": json_role("publication_pointer_before"),
                },
                "pointer_after": {
                    "repo_metadata": json_role("publication_repo_metadata_after"),
                    "distribution_index": json_role("publication_pointer_after"),
                },
                "pointer_immutable": json_role("publication_pointer_immutable"),
                "anonymous_manifest": raw_role("publication_anonymous_manifest"),
                "anonymous_payload": json_role("publication_anonymous_payload"),
                "source_catalog_snapshot": current_registry,
                "ack_run": acknowledgement_run,
                "ack_jobs": json_role("acknowledgement_jobs"),
                "ack_log": raw_role("acknowledgement_log_archive"),
                "journal_before": json_role("acknowledgement_journal_before"),
                "journal_after": json_role("acknowledgement_journal_after"),
            },
            expected_source_sha=source_binding.get("source_sha"),
            expected_manifest_sha256=source_binding.get("manifest_sha256"),
            expected_registry_sha256=current_registry["sha256"],
            expected_registry_bytes=current_registry["bytes"],
            repo_root=root,
            evaluation_epoch=evaluation_epoch,
        )
        if publication.get("status") != "verified":
            raise ValueError("retained publication and acknowledgement packet is not authenticated")
        assert_main_ancestor(root, publisher_run["head_sha"], "publisher workflow source commit")
    elif publisher_attempt is not None and publisher_readback is not None and acknowledgement_transition is not None:
        # Generic producer evidence uses the same native readback and ACK
        # validators as updated claims. Keep the resulting delivery fact even
        # when no positive #631 proof exists; this does not elevate any
        # completeness/current/updated claim.
        publication = native_publication_delivery_facet(
            publisher=publisher_attempt, readback=publisher_readback,
            acknowledgement=acknowledgement_transition,
        )

    if publication is not None:
        for role in publication_roles:
            item = single_role(operation_inputs, role, required=False)
            if item is not None:
                publication_evidence.append(
                    artifact(input_path(item), receipt_path_bytes(resolved[item["input_id"]], item))
                )
    elif publisher_attempt is not None or publisher_readback is not None or acknowledgement_local is not None:
        # Preserve each independently validated native delivery input when a
        # peer is absent. Missing ACK/core joins do not make valid publisher
        # and anonymous-readback evidence disappear.
        for role in publication_roles:
            item = single_role(operation_inputs, role, required=False)
            if item is not None:
                publication_evidence.append(
                    artifact(input_path(item), receipt_path_bytes(resolved[item["input_id"]], item))
                )

    evidence_by_path: dict[str, dict[str, Any]] = {}
    for result in (source, processor, promotion, health):
        for item in result["evidence"]:
            evidence_by_path[item["path"]] = item
    for item in publication_evidence:
        evidence_by_path[item["path"]] = item
    return {
        "status": "verified_historical_chain",
        "source": source,
        "processor": processor,
        "promotion": promotion,
        "health": health,
        "current_registry": current_registry,
        "publication": publication,
        "publisher_attempt": publisher_attempt,
        "publisher_readback": publisher_readback,
        "acknowledgement_local": acknowledgement_local,
        "acknowledgement_transition": acknowledgement_transition,
        "publication_missing_stages": [
            stage for stage, roles in (("publisher", publisher_roles), ("acknowledgement", acknowledgement_roles))
            if not roles
        ],
        "publication_evidence": publication_evidence,
        "evidence": [evidence_by_path[path] for path in sorted(evidence_by_path)],
    }


def scope_facets(
    *,
    scope: dict[str, Any],
    scoped_inputs: list[dict[str, Any]],
    resolved: dict[str, pathlib.Path],
    current_manifest_sha256: str | None,
    historical_import: dict[str, Any] | None,
    pipeline_evidence: dict[str, Any] | None,
    updated_delivery: dict[str, Any] | None,
    candidate_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    facets: list[dict[str, Any]] = []
    by_role: dict[str, list[dict[str, Any]]] = {}
    for item in scoped_inputs:
        by_role.setdefault(item["role"], []).append(item)
    operation_scope = scope["resource_kind"] == "api_operation_manifest"
    if operation_scope:
        if historical_import:
            applicable = historical_import["operation_manifest_sha256"] == current_manifest_sha256
            facets.append({
                "facet_id": "import_durability",
                "state": "proven" if applicable else "historical",
                "evidence": historical_import["evidence"],
                "details": {
                    "run_id": historical_import["run_id"],
                    "attempt": historical_import["attempt"],
                    "merged_at": historical_import["attested_at"],
                    "merge_commit": historical_import["merge_commit"],
                    "historical_manifest_sha256": historical_import["manifest_sha256"],
                    "historical_operation_manifest_sha256": historical_import["operation_manifest_sha256"],
                    "current_subject_applicable": applicable,
                },
                "missing_evidence": [] if applicable else [missing_entry("current_contract_import_missing", "StatPan/datapan-registry", "#633")],
            })
        else:
            facets.append({
                "facet_id": "import_durability", "state": "missing", "evidence": [], "details": {},
                "missing_evidence": [missing_entry("durable_import_attestation_missing", "StatPan/datapan-registry", "#633")],
            })
        if scope["source_id"] == "data_go_kr":
            facets.append({
                "facet_id": "terminal_execution", "state": "blocked", "evidence": [],
                "details": {"percentage_promotion_allowed": False},
                "missing_evidence": [missing_entry("terminal_execution_coverage_missing", "StatPan/datapan-registry", "#597")],
            })
        else:
            facets.append({
                "facet_id": "terminal_execution", "state": "unknown", "evidence": [], "details": {"local_operation_catalog_is_not_execution_evidence": True},
                "missing_evidence": [missing_entry("source_execution_evidence_missing", "StatPan/datapan-registry", "#634")],
            })
    if scope["resource_kind"] in {"api_catalog_metadata", "file_dataset"}:
        facets.append({
            "facet_id": "source_authority", "state": "blocked" if scope["authority_state"] == "unavailable" else "unknown", "evidence": [],
            "details": {"authority_state": scope["authority_state"], "local_inventory_is_not_authority": True},
            "missing_evidence": [missing_entry(item["code"], item["owner"], item["ticket"]) for item in scope["blockers"]],
        })
    if scope["resource_kind"] in {"curated_payload_snapshot", "payload_rows"}:
        facets.append({
            "facet_id": "payload_authority", "state": "blocked", "evidence": [],
            "details": {"evidence_owner": "StatPan/datapan-data", "data_catalog_receipt_is_not_row_evidence": True},
            "missing_evidence": [missing_entry(item["code"], item["owner"], item["ticket"]) for item in scope["blockers"]],
        })
    if operation_scope:
        if pipeline_evidence is None or pipeline_evidence.get("status") != "verified_historical_chain":
            stage_missing = pipeline_evidence.get("missing_stages", []) if pipeline_evidence else []
            facets.append({
                "facet_id": "specification_pipeline", "state": "missing",
                "evidence": pipeline_evidence.get("evidence", []) if pipeline_evidence else [],
                "details": {
                    "missing_stages": stage_missing,
                    "validated_present_stage_evidence": pipeline_evidence.get("evidence", []) if pipeline_evidence else [],
                },
                "missing_evidence": [missing_entry("authenticated_b_c_health_chain_missing", "StatPan/datapan-registry", "#659")],
            })
            if updated_delivery is None:
                publication = pipeline_evidence.get("publication") if pipeline_evidence else None
                readback = pipeline_evidence.get("publisher_readback") if pipeline_evidence else None
                publisher_attempt = pipeline_evidence.get("publisher_attempt") if pipeline_evidence else None
                current_registry = pipeline_evidence.get("current_registry") if pipeline_evidence else None
                if publication is not None or readback is not None and publisher_attempt is not None:
                    subject = publication["subject"] if publication is not None else {
                        "repository": "StatPan/datapan-registry",
                        "source_sha": readback["source_sha"],
                        "manifest_sha256": readback["manifest_sha256"],
                        "registry_path": readback["registry_artifact"]["path"],
                        "registry_sha256": readback["registry_artifact"]["sha256"],
                        "registry_bytes": readback["registry_artifact"]["bytes"],
                        "publisher_run_id": readback["publisher_run_id"],
                        "publisher_attempt": readback["publisher_attempt"],
                        "payload_revision": readback["payload_revision"],
                        "pointer_revision": readback["pointer_revision"],
                    }
                    release_manifest_sha = current_registry.get("release_manifest_sha256") if current_registry else None
                    same_release = candidate_context is None and subject["manifest_sha256"] == release_manifest_sha
                    publication_missing = pipeline_evidence.get("publication_missing_stages", [])
                    missing_evidence = []
                    if not same_release:
                        missing_evidence.append(missing_entry("current_release_publication_read_back_missing", "StatPan/datapan-registry", "#659"))
                    if "acknowledgement" in publication_missing:
                        missing_evidence.append(missing_entry("publication_acknowledgement_missing", "StatPan/datapan-registry", "#716"))
                    facets.append({
                        "facet_id": "immutable_publication_read_back",
                        "state": "proven" if same_release else "historical",
                        "evidence": pipeline_evidence.get("publication_evidence", []),
                        "details": {
                            **(publication.get("details", {}) if publication is not None else {}),
                            "subject": subject,
                            "native_readback": {
                                "observed_at": readback["consumer_readback_observed_at"],
                                "verified_artifact_count": readback["verified_artifact_count"],
                            } if readback is not None else None,
                            "acknowledgement_present": "acknowledgement" not in publication_missing,
                            "missing_stages": publication_missing,
                            "current_release_manifest_sha256": release_manifest_sha,
                            "current_release_subject_applicable": same_release,
                            "catalog_binding_witness": current_registry.get("catalog_binding_witness") if current_registry else None,
                            "current_release_applicable": current_registry.get("current_release_applicable", False) if current_registry else False,
                            "payload_equivalent_to_current_registry": (
                                bool(current_registry)
                                and subject["registry_sha256"] == current_registry["sha256"]
                                and subject["registry_bytes"] == current_registry["bytes"]
                            ),
                            "receipt_cutover_required": False,
                            "consumer_read_back_required_for_updated": True,
                        },
                        "missing_evidence": missing_evidence,
                    })
                else:
                    facets.append({
                        "facet_id": "immutable_publication_read_back", "state": "missing",
                        "evidence": pipeline_evidence.get("publication_evidence", []) if pipeline_evidence else [],
                        "details": {
                            "publisher_attempt": publisher_attempt,
                            "publisher_readback": readback,
                            "acknowledgement_local": pipeline_evidence.get("acknowledgement_local") if pipeline_evidence else None,
                            "missing_stages": pipeline_evidence.get("publication_missing_stages", []) if pipeline_evidence else [],
                            "receipt_cutover_required": False,
                            "consumer_read_back_required_for_updated": True,
                        },
                        "missing_evidence": [missing_entry("same_subject_publication_read_back_missing", "StatPan/datapan-registry", "#659")],
                    })
            facets.extend([
                {"facet_id": "source_observation", "state": "missing", "evidence": [], "details": {}, "missing_evidence": [missing_entry("new_authenticated_source_observation_missing", "StatPan/datapan-data", "StatPan/datapan-data#1190")]},
                {"facet_id": "health_observation", "state": "missing", "evidence": [], "details": {"health_is_not_source_observation": True}, "missing_evidence": [missing_entry("health_producer_receipt_missing", "StatPan/datapan-health", "StatPan/datapan-health#33")]},
            ])
        else:
            source = pipeline_evidence["source"]
            processor = pipeline_evidence["processor"]
            promotion = pipeline_evidence["promotion"]
            health = pipeline_evidence["health"]
            catalog_binding_witness = pipeline_evidence["current_registry"].get("catalog_binding_witness")
            current_release_applicable = pipeline_evidence["current_registry"].get("current_release_applicable", False)
            facets.append({
                "facet_id": "source_observation",
                "state": "historical",
                "evidence": source["evidence"],
                "details": {
                    "run_id": source["run_id"], "attempt": source["attempt"],
                    "observed_at": source["observed_at"], "step_completed_at": source["step_completed_at"],
                    "candidate_sha256": source["candidate_sha256"],
                    "refresh_evidence_sha256": source["refresh_evidence_sha256"],
                    "observation_authenticated": True,
                },
                "missing_evidence": [missing_entry(
                    "post_repair_observation_baseline_unbound",
                    "StatPan/datapan-registry", "StatPan/datapan-registry#722",
                )],
            })
            facets.append({
                "facet_id": "specification_pipeline",
                "state": "historical",
                "evidence": pipeline_evidence["evidence"],
                "details": {
                    "source_run_id": source["run_id"],
                    "source_observed_at": source["observed_at"],
                    "processor_run_id": processor["run_id"],
                    "processor_run_attempt": processor["attempt"],
                    "processor_generation_id": processor["generation_id"],
                    "candidate_sha256": processor["candidate_sha256"],
                    "candidate_bytes": processor["candidate_bytes"],
                    "pending_count": processor["pending_count"],
                    "detail_retry_count": processor["detail_retry_count"],
                    "detail_unattempted_count": processor["detail_unattempted_count"],
                    "current_input_contract_compatible": processor["current_input_contract_compatible"],
                    "current_input_contract_changed_paths": processor["current_input_contract_changed_paths"],
                    "promotion_run_id": promotion["run_id"],
                    "promotion_candidate_available": promotion["candidate_available"],
                    "promotion_candidate_lifecycle_status": promotion["candidate_lifecycle_status"],
                    "promotion_candidate_acknowledgement_statuses": promotion["candidate_acknowledgement_statuses"],
                    "promotion_processor_generation_matches": promotion["processor_generation_matches"],
                    "promotion_candidate_key": promotion["candidate_key"],
                    "promotion_journal_sha256": promotion["journal_sha256"],
                    "health_run_id": health["run_id"],
                    "health_receipt_sha256": health["receipt_sha256"],
                    "health_observation_count": health["observation_count"],
                    "health_last_good_source_sha": health["last_good_source_sha"],
                    "health_source_observation_matches": health["source_observation_matches"],
                    "health_processor_observation_matches": health["processor_observation_matches"],
                    "health_promotion_execution_matches": health["promotion_execution_matches"],
                    "health_candidate_relation_valid": health["candidate_relation_valid"],
                    "health_main_revision": health["health_main_revision"],
                    "health_main_manifest_sha256": health["health_main_manifest_sha256"],
                    "health_main_payload_matches_current": health["health_main_payload_matches_current"],
                    "health_main_release_manifest_matches_current": health["health_main_release_manifest_matches_current"],
                    "health_main_matches_current_subject": health["health_main_matches_current_subject"],
                    "catalog_binding_witness": catalog_binding_witness,
                    "current_release_applicable": current_release_applicable,
                    "full_scope_fresh": processor["full_scope_fresh"],
                    "publication_allowed": processor["publication_allowed"],
                },
                "missing_evidence": [missing_entry("post_fix_source_and_complete_scope_acceptance_pending", "StatPan/datapan-registry", "#634")],
            })
            facets.append({
                "facet_id": "health_observation",
                "state": "historical",
                "evidence": health["evidence"],
                "details": {
                    "run_id": health["run_id"], "attempt": health["attempt"],
                    "receipt_sha256": health["receipt_sha256"], "receipt_seal": health["receipt_seal"],
                    "state_commit": health["state_commit"],
                    "pre_state_sha256": health["pre_state_sha256"],
                    "post_state_sha256": health["post_state_sha256"],
                    "observation_count": health["observation_count"],
                    "health_is_not_new_source_observation": True,
                },
                "missing_evidence": [missing_entry(
                    "health_receipt_does_not_establish_additional_source_observations",
                    "StatPan/datapan-registry", "#659",
                )],
            })
            publication = pipeline_evidence["publication"]
            if updated_delivery is None:
                if publication is None:
                    readback = pipeline_evidence.get("publisher_readback")
                    publisher_attempt = pipeline_evidence.get("publisher_attempt")
                    if readback is None or publisher_attempt is None:
                        facets.append({
                            "facet_id": "immutable_publication_read_back",
                            "state": "missing",
                            "evidence": pipeline_evidence["publication_evidence"],
                            "details": {
                                "publisher_attempt": publisher_attempt,
                                "publisher_readback": readback,
                                "missing_stages": pipeline_evidence.get("publication_missing_stages", []),
                                "receipt_cutover_required": False,
                                "consumer_read_back_required_for_updated": True,
                            },
                            "missing_evidence": [missing_entry("same_subject_publication_read_back_missing", "StatPan/datapan-registry", "#659")],
                        })
                    else:
                        readback_subject = {
                            "repository": "StatPan/datapan-registry",
                            "source_sha": readback["source_sha"],
                            "manifest_sha256": readback["manifest_sha256"],
                            "registry_path": readback["registry_artifact"]["path"],
                            "registry_sha256": readback["registry_artifact"]["sha256"],
                            "registry_bytes": readback["registry_artifact"]["bytes"],
                            "publisher_run_id": readback["publisher_run_id"],
                            "publisher_attempt": readback["publisher_attempt"],
                            "payload_revision": readback["payload_revision"],
                            "pointer_revision": readback["pointer_revision"],
                        }
                        current_release_manifest_sha = pipeline_evidence["current_registry"]["release_manifest_sha256"]
                        compared_registry = candidate_context["candidate_registry"] if candidate_context is not None else pipeline_evidence["current_registry"]
                        same_release = candidate_context is None and readback_subject["manifest_sha256"] == current_release_manifest_sha
                        missing_evidence = []
                        if not same_release:
                            missing_evidence.append(missing_entry("current_release_publication_read_back_missing", "StatPan/datapan-registry", "#659"))
                        if "acknowledgement" in pipeline_evidence.get("publication_missing_stages", []):
                            missing_evidence.append(missing_entry("publication_acknowledgement_missing", "StatPan/datapan-registry", "#716"))
                        facets.append({
                            "facet_id": "immutable_publication_read_back",
                            "state": "proven" if same_release else "historical",
                            "evidence": pipeline_evidence["publication_evidence"],
                            "details": {
                                    "subject": readback_subject,
                                    "publisher_attempt": publisher_attempt,
                                    "native_readback": {
                                    "observed_at": readback["consumer_readback_observed_at"],
                                    "verified_artifact_count": readback["verified_artifact_count"],
                                },
                                    "acknowledgement_present": "acknowledgement" not in pipeline_evidence.get("publication_missing_stages", []),
                                    "missing_stages": pipeline_evidence.get("publication_missing_stages", []),
                                    "current_release_manifest_sha256": None if candidate_context is not None else current_release_manifest_sha,
                                "current_release_subject_applicable": same_release,
                                "catalog_binding_witness": catalog_binding_witness,
                                "current_release_applicable": current_release_applicable,
                                "payload_equivalent_to_current_registry": (
                                    readback_subject["registry_sha256"] == compared_registry["sha256"]
                                    and readback_subject["registry_bytes"] == compared_registry["bytes"]
                                ),
                                "candidate_release_publication_unproven": candidate_context is not None,
                                "receipt_cutover_required": False,
                                "consumer_read_back_required_for_updated": True,
                            },
                                "missing_evidence": missing_evidence,
                        })
                else:
                    current_release_manifest_sha = pipeline_evidence["current_registry"]["release_manifest_sha256"]
                    publication_subject = publication["subject"]
                    same_release = candidate_context is None and publication_subject["manifest_sha256"] == current_release_manifest_sha
                    compared_registry = candidate_context["candidate_registry"] if candidate_context is not None else pipeline_evidence["current_registry"]
                    facets.append({
                        "facet_id": "immutable_publication_read_back",
                        "state": "proven" if same_release else "historical",
                        "evidence": pipeline_evidence["publication_evidence"],
                        "details": {
                            **publication["details"],
                            "subject": publication_subject,
                            "current_release_manifest_sha256": None if candidate_context is not None else current_release_manifest_sha,
                            "current_release_subject_applicable": same_release,
                            "catalog_binding_witness": catalog_binding_witness,
                            "current_release_applicable": current_release_applicable,
                            "payload_equivalent_to_current_registry": (
                                publication_subject["registry_sha256"] == compared_registry["sha256"]
                                and publication_subject["registry_bytes"] == compared_registry["bytes"]
                            ),
                            "candidate_release_publication_unproven": candidate_context is not None,
                            "receipt_cutover_required": False,
                            "consumer_read_back_required_for_updated": True,
                        },
                        "missing_evidence": [] if same_release else [missing_entry("current_release_publication_read_back_missing", "StatPan/datapan-registry", "#659")],
                    })
            else:
                # Keep the retained pipeline release identity visible alongside
                # the distinct proof publication without conflating their subjects.
                if publication is not None:
                    publication_subject = publication["subject"]
                    current_release_manifest_sha = pipeline_evidence["current_registry"]["release_manifest_sha256"]
                    same_release = candidate_context is None and publication_subject["manifest_sha256"] == current_release_manifest_sha
                    pipeline_facet = next(facet for facet in facets if facet["facet_id"] == "specification_pipeline")
                    pipeline_facet["details"]["retained_publication_subject"] = publication_subject
                    pipeline_facet["details"]["retained_publication_current_release_applicable"] = same_release
        facets.append({
            "facet_id": "receipt_cutover", "state": "missing", "evidence": [],
            "details": {"cutover_active": False, "native_publication_scope_requires_cutover": False},
            "missing_evidence": [missing_entry("producer_receipt_cutover_evidence_missing", "StatPan/datapan-registry", "#592")],
        })
    elif not operation_scope and updated_delivery is None:
        facets.append({
            "facet_id": "immutable_publication_read_back", "state": "missing", "evidence": [],
            "details": {"consumer_read_back_required_for_updated": True},
            "missing_evidence": [missing_entry("same_subject_publication_read_back_missing", "StatPan/datapan-registry", "#659")],
        })
    if updated_delivery is not None:
        facets.append({
            "facet_id": "immutable_publication_read_back",
            "state": "proven",
            "evidence": updated_delivery["evidence"],
            "details": {
                "claim": "updated",
                "subject": updated_delivery["source"],
                "import": updated_delivery["import"],
                "publisher": updated_delivery["publisher"],
                "consumer_read_back": updated_delivery["consumer_read_back"],
                "acknowledgement": updated_delivery["acknowledgement"],
                "updated_predicates_validated": True,
                "receipt_cutover_required": False,
            },
            "missing_evidence": [],
        })
    return facets


def build_scope_row(
    *,
    root: pathlib.Path,
    scope: dict[str, Any],
    scoped_inputs: list[dict[str, Any]],
    resolved: dict[str, pathlib.Path],
    policy: dict[str, Any],
    evaluation_epoch: str,
    all_inputs: list[dict[str, Any]],
    scope_registry: dict[str, Any],
    candidate_context: dict[str, Any] | None = None,
    candidate_changed_scopes: set[str] | None = None,
    candidate_baseline_registry_bytes: bytes | None = None,
) -> dict[str, Any]:
    candidate_changed = scope["scope_id"] in (candidate_changed_scopes or set())
    inventory_context, evidence = local_inventory_context(
        root=root, scope=scope, scoped_inputs=scoped_inputs, resolved=resolved,
        candidate_context=candidate_context,
    )
    historical_import = validate_historical_import(
        root=root, inputs=scoped_inputs, resolved=resolved, scope=scope,
    )
    current_manifest_sha = None
    if scope["inventory"]["kind"] == "operation_manifest":
        manifest_inputs = [item for item in scoped_inputs if item["role"] == "local_operation_manifest"]
        if manifest_inputs:
            current_manifest_sha = manifest_inputs[0]["sha256"]
    pipeline_evidence = None
    if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr":
        pipeline_evidence = validate_pipeline_evidence(
            root=root, operation_inputs=scoped_inputs, all_inputs=all_inputs,
            resolved=resolved, evaluation_epoch=evaluation_epoch, scope_registry=scope_registry,
            candidate_context=candidate_context,
        )
    proof, updated_delivery = proof_for_scope(
        root=root,
        scope=scope,
        scoped_inputs=scoped_inputs,
        resolved=resolved,
        policy=policy,
        scope_registry=scope_registry,
        evaluation_epoch=evaluation_epoch,
        pipeline_evidence=pipeline_evidence,
        candidate_subject_changed=candidate_changed,
        candidate_baseline_registry_bytes=candidate_baseline_registry_bytes,
    )
    claims = {"complete": False, "current": False, "updated": False}
    proof_state = "blocked"
    authority_state = scope["authority_state"]
    if proof is not None:
        authority_state = proof["scope"]["authority"]["state"]
        if candidate_changed:
            proof_state = "blocked"
            claims = {"complete": False, "current": False, "updated": False}
        else:
            proof_state = proof["proof_state"]
            claims = dict(proof["claims"])
        evidence.append(artifact(
            input_path(next(item for item in scoped_inputs if item["role"] == "proof_v1")),
            read_bytes(resolved[next(item for item in scoped_inputs if item["role"] == "proof_v1")["input_id"]]),
        ))
    facets = scope_facets(
        scope=scope,
        scoped_inputs=scoped_inputs,
        resolved=resolved,
        current_manifest_sha256=current_manifest_sha,
        historical_import=historical_import,
        pipeline_evidence=pipeline_evidence,
        updated_delivery=updated_delivery,
        candidate_context=candidate_context,
    )
    if candidate_context is not None:
        candidate_evidence = [
            artifact(input_path(item), read_bytes(resolved[item["input_id"]]))
            for item in scoped_inputs
            if item.get("artifact_type") == "local_inventory"
        ]
        facets.append({
            "facet_id": "candidate_inventory",
            "state": "blocked" if candidate_changed else "historical",
            "evidence": candidate_evidence,
            "details": {
                "baseline_main_sha": candidate_context["baseline_main_sha"],
                "scope_inventory_changed": candidate_changed,
                "historical_proof_validated": candidate_changed and proof is not None,
                "historical_claims_suppressed": candidate_changed and proof is not None,
                "candidate_registry_sha256": candidate_context["candidate_registry"]["sha256"],
                "candidate_operation_manifest_sha256": candidate_context["candidate_operation_manifest"]["sha256"],
                "local_inventory_is_not_authority": True,
            },
            "missing_evidence": (
                [missing_entry("candidate_scope_proof_not_admitted", "StatPan/datapan-registry", "#659")]
                if candidate_changed else []
            ),
        })
    missing = [missing_entry(item["code"], item["owner"], item["ticket"]) for item in scope["blockers"]]
    if proof is None:
        missing.extend(missing_entry(item["code"], item["owner"], item["ticket"]) for item in scope["blockers"])
    if candidate_changed:
        missing.append(missing_entry("candidate_scope_proof_not_admitted", "StatPan/datapan-registry", "#659"))
    for facet in facets:
        if facet["state"] in {"missing", "unknown", "blocked"}:
            # Facet blockers remain visible but do not redefine the #631 scope-kind claim.
            pass
    missing = dedupe_missing(missing)
    return {
        "scope_id": scope["scope_id"],
        "source_id": scope["source_id"],
        "provider": scope["provider"],
        "resource_kind": scope["resource_kind"],
        "selector": scope["selector"],
        "identity_algorithm": scope["identity_algorithm"],
        "identity_owner": scope["identity_owner"],
        "evidence_owner": scope["evidence_owner"],
        "authority_state": authority_state,
        "inventory_context": inventory_context,
        "proof": proof,
        "proof_state": proof_state,
        "claims": claims,
        "facets": facets,
        "missing_evidence": missing,
    }


def build_report(
    *,
    root: pathlib.Path,
    input_root: pathlib.Path,
    input_index_path: pathlib.Path,
    as_of: str | None = None,
    input_index_override: dict[str, Any] | None = None,
    candidate_baseline_registry_bytes: bytes | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    scope_registry, policy, scope_by_id = load_and_validate_registry(root)
    raw_index = input_index_override if input_index_override is not None else object_at(input_index_path.resolve(), "input index")
    candidate_context, candidate_changed_scopes = validate_candidate_input_index(
        root=root, index=raw_index, input_index_path=input_index_path.resolve(),
    )
    if candidate_context and candidate_baseline_registry_bytes is not None:
        expected_baseline = candidate_context["baseline_registry"]
        if (len(candidate_baseline_registry_bytes), sha256_bytes(candidate_baseline_registry_bytes)) != (
            expected_baseline["bytes"], expected_baseline["sha256"],
        ):
            raise ValueError("candidate baseline Registry materialization differs from its pinned main payload")
    index, checked_inputs, resolved = validate_input_index(
        root=root, input_root=input_root.resolve(), index_path=input_index_path.resolve(),
        scope_by_id=scope_by_id, index_value=raw_index,
    )
    evaluation_epoch = as_of or index["evaluation_epoch"]
    parse_time(evaluation_epoch, "rollup evaluation epoch")
    rows = [
        build_scope_row(
            root=root,
            scope=scope_by_id[scope_id],
            scoped_inputs=[item for item in checked_inputs if item["scope_id"] == scope_id],
            resolved=resolved,
            policy=policy,
            evaluation_epoch=evaluation_epoch,
            all_inputs=checked_inputs,
            scope_registry=scope_registry,
            candidate_context=candidate_context or None,
            candidate_changed_scopes=candidate_changed_scopes,
            candidate_baseline_registry_bytes=candidate_baseline_registry_bytes,
        )
        for scope_id in sorted(scope_by_id)
    ]
    if len(rows) != len(scope_by_id) or {item["scope_id"] for item in rows} != set(scope_by_id):
        raise ValueError("rollup must contain each registered scope exactly once")
    state_counts = {state: sum(row["proof_state"] == state for row in rows) for state in ("complete", "partial", "unknown", "blocked")}
    claim_counts = {claim: sum(row["claims"][claim] for row in rows) for claim in ("complete", "current", "updated")}
    policy_bytes = read_bytes(root / POLICY_PATH)
    scope_registry_bytes = read_bytes(root / SCOPE_REGISTRY_PATH)
    # The index is semantic input. Canonicalize its row/key order so harmless
    # input reordering cannot perturb a release artifact digest.
    canonical_index = {**index, "inputs": checked_inputs}
    input_bytes = (json.dumps(canonical_index, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        input_label = input_index_path.resolve().relative_to(root).as_posix()
    except ValueError:
        input_label = "external-input-index"
    report = {
        "schema_version": "datapan.completeness-proof-rollup.v1",
        "evaluation_epoch": evaluation_epoch,
        "evaluation_context": candidate_context or {"mode": "repository"},
        "policy": artifact(POLICY_PATH, policy_bytes),
        "scope_registry": artifact(SCOPE_REGISTRY_PATH, scope_registry_bytes),
        "input_index": artifact(f"{'candidate' if candidate_context else 'canonical'}-semantic-json:{input_label}", input_bytes),
        "summary": {
            "registered_scopes": len(rows),
            "states": state_counts,
            "claims": claim_counts,
        },
        "scopes": rows,
    }
    validate_schema(report, root / ROLLUP_SCHEMA_PATH, "completeness proof rollup")
    return report


def render_json(report: dict[str, Any]) -> bytes:
    return (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def markdown_escape(value: Any) -> str:
    if value is None:
        return "unknown"
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown_compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_markdown(report: dict[str, Any]) -> bytes:
    lines = [
        "# Public-data completeness proof rollup",
        "",
        f"As of `{report['evaluation_epoch']}`. This report covers only the registered scopes below; it does not claim to cover all Korean public data.",
        "",
        (f"Evaluation mode: `{report['evaluation_context']['mode']}`; candidate source inventories are local context and cannot establish authority, publication, or an updated claim. Baseline main: `{report['evaluation_context']['baseline_main_sha']}`."
         if report["evaluation_context"]["mode"] == "candidate"
         else "Evaluation mode: `repository`."),
        "",
        f"Registered scopes: {report['summary']['registered_scopes']}; states: "
        + ", ".join(f"{key}={value}" for key, value in report["summary"]["states"].items())
        + ".",
        "",
        "Local profiles and candidate denominators are inventory context, not authoritative source-wide denominator evidence. Missing v1 proofs are shown as blocked or unknown without synthetic hashes, timestamps, or counts.",
        "",
        "| Scope | Resource kind | Local identities | Proof state | Complete | Current | Updated | Missing evidence |",
        "| --- | --- | ---: | --- | --- | --- | --- | --- |",
    ]
    for row in report["scopes"]:
        missing = ", ".join(
            f"{item['code']} ({item['owner']} {item['ticket']})" for item in row["missing_evidence"]
        ) or "none"
        lines.append(
            "| " + " | ".join(
                markdown_escape(value)
                for value in (
                    row["scope_id"],
                    row["resource_kind"],
                    row["inventory_context"]["local_identity_count"],
                    row["proof_state"],
                    row["claims"]["complete"],
                    row["claims"]["current"],
                    row["claims"]["updated"],
                    missing,
                )
            ) + " |"
        )
    lines.extend([
        "",
        "## Evidence facets",
        "",
        "Facet states describe only the recorded evidence subject. Historical publication/read-back does not imply current release applicability; payload equivalence is shown separately.",
        "",
    ])
    for row in report["scopes"]:
        for facet in row["facets"]:
            evidence_paths = ", ".join(item["path"] for item in facet["evidence"]) or "none"
            missing = ", ".join(item["code"] for item in facet["missing_evidence"]) or "none"
            details = markdown_compact_json(facet["details"])
            lines.extend([
                f"- **{markdown_escape(row['scope_id'])} / {markdown_escape(facet['facet_id'])}** — `{markdown_escape(facet['state'])}`.",
                f"  Details: `{markdown_escape(details)}`; evidence: {markdown_escape(evidence_paths)}; missing: {markdown_escape(missing)}.",
            ]
            )
    lines.extend([
        "",
        f"Input index semantic identity: `{report['input_index']['path']}` has SHA-256 `{report['input_index']['sha256']}` over canonicalized JSON; this is not the raw input file digest.",
        "",
        "`updated` requires the existing #631 import-durability, immutable-publication, and consumer-read-back predicates for the same subject. Producer-receipt cutover (#592) is a separate facet and is not silently activated here.",
        "",
        "A historical import or scheduled B/C/Health no-op remains historical evidence. It does not add a new source observation or satisfy the original #634 post-fix scheduled-chain and same-subject read-back acceptance by itself.",
        "",
    ])
    return "\n".join(lines).encode("utf-8")


def atomic_write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def require_claims(report: dict[str, Any], requirements: list[str]) -> None:
    rows = {row["scope_id"]: row for row in report["scopes"]}
    failures: list[str] = []
    for requirement in requirements:
        if ":" not in requirement:
            raise ValueError("--require-claim expects scope_id:complete|current|updated")
        scope_id, claim = requirement.split(":", 1)
        if claim not in {"complete", "current", "updated"}:
            raise ValueError(f"unknown claim: {claim}")
        row = rows.get(scope_id)
        if row is None:
            failures.append(f"{scope_id}:unregistered_scope")
        elif row["claims"][claim] is not True:
            reasons = ",".join(item["code"] for item in row["missing_evidence"])
            failures.append(f"{scope_id}:{claim}:blocked:{reasons or row['proof_state']}")
    if failures:
        raise ValueError("required claim is not supported: " + "; ".join(failures))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=pathlib.Path, default=ROOT)
    parser.add_argument("--input-root", type=pathlib.Path)
    parser.add_argument("--input-index", type=pathlib.Path)
    parser.add_argument("--candidate-registry", type=pathlib.Path, help="registered candidate Registry inventory for source-refresh evaluation")
    parser.add_argument("--candidate-operation-manifest", type=pathlib.Path, help="registered candidate operation manifest produced from that Registry")
    parser.add_argument("--candidate-baseline-registry", type=pathlib.Path, help="optional retained previous Registry whose bytes must match the pinned main payload")
    parser.add_argument("--as-of")
    parser.add_argument("--output-json", default=OUTPUT_JSON_PATH)
    parser.add_argument("--output-markdown", default=OUTPUT_MARKDOWN_PATH)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--check", action="store_true")
    parser.add_argument("--require-claim", action="append", default=[])
    return parser.parse_args(argv)


def output_path(root: pathlib.Path, value: str, label: str) -> pathlib.Path:
    relative = normalize_relative_path(value, label)
    path = resolve_under(root, relative.as_posix(), label)
    return path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.repo_root.resolve()
    input_root = (args.input_root or root).resolve()
    input_index = args.input_index or (root / INPUT_INDEX_PATH)
    json_output = output_path(root, args.output_json, "JSON output path")
    markdown_output = output_path(root, args.output_markdown, "Markdown output path")
    try:
        candidate_requested = args.candidate_registry is not None or args.candidate_operation_manifest is not None
        if (args.candidate_registry is None) != (args.candidate_operation_manifest is None):
            raise ValueError("candidate mode requires both --candidate-registry and --candidate-operation-manifest")
        candidate_index = None
        candidate_baseline_registry_bytes = None
        if candidate_requested:
            if input_index.resolve() != (root / INPUT_INDEX_PATH).resolve():
                raise ValueError("candidate mode writes only the registered completeness input index")
            baseline_main_sha = git_read_only(root, ["rev-parse", "refs/remotes/origin/main"]).decode("ascii").strip()
            candidate_index = input_index_for_candidate(
                root=root,
                baseline_main_sha=baseline_main_sha,
                candidate_registry=args.candidate_registry.resolve(),
                candidate_operation_manifest=args.candidate_operation_manifest.resolve(),
                baseline_registry_file=args.candidate_baseline_registry.resolve() if args.candidate_baseline_registry else None,
            )
            if args.candidate_baseline_registry is not None:
                candidate_baseline_registry_bytes = read_bytes(args.candidate_baseline_registry.resolve())
            if args.check:
                saved_index = object_at(input_index.resolve(), "input index")
                normalize = lambda value: json.dumps(
                    {**value, "inputs": sorted(value["inputs"], key=lambda item: item["input_id"])},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
                if normalize(saved_index) != normalize(candidate_index):
                    raise ValueError("candidate input index differs from the deterministic candidate rebuild")
        report = build_report(
            root=root,
            input_root=input_root,
            input_index_path=input_index,
            as_of=args.as_of,
            input_index_override=candidate_index,
            candidate_baseline_registry_bytes=candidate_baseline_registry_bytes,
        )
        require_claims(report, args.require_claim)
        json_bytes = render_json(report)
        markdown_bytes = render_markdown(report)
        if args.write:
            if candidate_index is not None:
                atomic_write(input_index, canonical_json_bytes(candidate_index))
            atomic_write(json_output, json_bytes)
            atomic_write(markdown_output, markdown_bytes)
            print(f"ok completeness proof rollup ({report['summary']['registered_scopes']} scopes)")
        else:
            mismatches = [
                path.as_posix()
                for path, expected in ((json_output, json_bytes), (markdown_output, markdown_bytes))
                if not path.is_file() or path.read_bytes() != expected
            ]
            if mismatches:
                raise ValueError("generated output drift: " + ", ".join(mismatches))
            print(f"ok completeness proof rollup check ({report['summary']['registered_scopes']} scopes)")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL completeness proof rollup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
