"""Offline validation helpers for immutable Registry release-admission receipts."""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import jsonschema

SCHEMA_VERSION = "datapan.release-receipt-admission.v1"
RUNTIME_KIND = "runtime_freshness_shard"
LIVE_KIND = "health_live_observation"
RUNTIME_SCOPE = {"provider": "data_go_kr", "subject": "runtime_freshness_rotating_shard"}
HEALTH_PLAN_VERSION_DECISION = "reports/release-version-decision.json"


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    return as_dict(value, path.as_posix())


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rooted_file(root: pathlib.Path, raw_path: object, label: str) -> pathlib.Path:
    if not isinstance(raw_path, str):
        raise ValueError(f"{label} must be a string path")
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} escapes its root")
    resolved_root = root.resolve()
    candidate = (resolved_root / pathlib.Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes its root") from exc
    if not candidate.is_file():
        raise ValueError(f"{label} must be a regular file inside its root")
    return candidate


def canonical_digest(receipt: dict[str, Any]) -> str:
    value = copy.deepcopy(receipt)
    value.pop("receipt_digest", None)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def health_plan_manifest_binding(manifest: dict[str, Any]) -> str:
    """Canonical Health-plan binding shared by its generator and admission gate.

    The plan excludes its own mutable digest entry and the release-version
    decision, which is intentionally generated after the plan.  All other
    manifest facts remain part of the immutable execution-policy binding.
    """
    projection = copy.deepcopy(manifest)
    artifacts = as_list(projection.get("artifacts"), "manifest.artifacts")
    projection["artifacts"] = [item for item in artifacts if item.get("path") != HEALTH_PLAN_VERSION_DECISION]
    projection["artifact_count"] = len(projection["artifacts"])
    bound = [item for item in projection["artifacts"] if item.get("path") == "reports/health-runtime-observation-plan.v1.json"]
    if len(bound) != 1 or bound[0].get("kind") != "verification_plan" or bound[0].get("schema") != "https://schemas.datapan.dev/datapan.health-runtime-observation-plan.v1.schema.json":
        raise ValueError("Health execution plan manifest identity does not match")
    bound[0].pop("bytes", None)
    bound[0].pop("sha256", None)
    return hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


FORMAT_CHECKER = jsonschema.FormatChecker()


@FORMAT_CHECKER.checks("date-time")
def is_rfc3339_datetime(value: object) -> bool:
    try:
        parse_time(value, "date-time")
    except ValueError:
        return False
    return True


def validate_schema(receipt: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    errors = sorted(
        jsonschema.Draft202012Validator(schema, format_checker=FORMAT_CHECKER).iter_errors(receipt),
        key=lambda item: list(item.path),
    )
    if errors:
        # Never render an invalid value or unknown property name: receipts are an
        # untrusted boundary and diagnostics must not echo a secret or identity.
        rendered = "; ".join(
            f"{'.'.join(map(str, error.path)) or '<root>'}: schema validation {error.validator}"
            for error in errors
        )
        raise ValueError(f"{label}: {rendered}")


def scan_redaction(value: object, policy: dict[str, Any], label: str) -> None:
    redaction = as_dict(policy.get("redaction"), "policy.redaction")
    forbidden_keys = set(as_list(redaction.get("forbidden_field_names"), "policy.redaction.forbidden_field_names"))
    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in as_list(redaction.get("forbidden_value_patterns"), "policy.redaction.forbidden_value_patterns")]
    if isinstance(value, dict):
        for key, child in value.items():
            if key in forbidden_keys:
                raise ValueError(f"{label}: forbidden secret-bearing field {key!r}")
            scan_redaction(child, policy, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            scan_redaction(child, policy, f"{label}[{index}]")
    elif isinstance(value, str):
        for pattern in patterns:
            if pattern.search(value):
                raise ValueError(f"{label}: value matches forbidden secret pattern {pattern.pattern!r}")


def validate_manifest(manifest_path: pathlib.Path, *, check_artifacts: bool) -> tuple[dict[str, Any], str, str]:
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != "datapan.release-manifest.v1":
        raise ValueError("manifest.schema_version must be datapan.release-manifest.v1")
    source = manifest.get("source_registry")
    if not isinstance(source, str) or not source:
        raise ValueError("manifest.source_registry must be a non-empty path")
    artifacts = as_list(manifest.get("artifacts"), "manifest.artifacts")
    if manifest.get("artifact_count") != len(artifacts):
        raise ValueError("manifest.artifact_count must equal manifest.artifacts length")
    paths: set[str] = set()
    root = manifest_path.parent
    for index, raw_artifact in enumerate(artifacts):
        artifact = as_dict(raw_artifact, f"manifest.artifacts[{index}]")
        path = artifact.get("path")
        if not isinstance(path, str) or not path or path in paths:
            raise ValueError(f"manifest.artifacts[{index}].path must be unique and non-empty")
        paths.add(path)
        if check_artifacts:
            artifact_path = rooted_file(root, path, f"manifest.artifacts[{index}].path")
            if artifact.get("sha256") != file_digest(artifact_path):
                raise ValueError(f"manifest artifact digest mismatch: {path}")
    source_path = rooted_file(root, source, "manifest.source_registry")
    return manifest, file_digest(manifest_path), file_digest(source_path)


def producer_artifact_path(
    producer: dict[str, Any], artifact_roots: dict[str, pathlib.Path], field: str, label: str
) -> pathlib.Path:
    repository = producer.get("repository")
    root = artifact_roots.get(repository)
    if root is None:
        raise ValueError(f"{label}: producer artifact root is required for the authorized producer")
    raw_path = producer.get(field)
    if not isinstance(raw_path, str):
        raise ValueError(f"{label}: producer receipt path must be a string")
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label}: producer artifact path escapes its artifact root")
    resolved_root = root.resolve()
    candidate = (resolved_root / pathlib.Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label}: producer artifact path escapes its artifact root") from exc
    if not candidate.is_file():
        raise ValueError(f"{label}: producer artifact is missing")
    return candidate


def validate_health_aggregate(
    producer: dict[str, Any],
    execution: dict[str, Any],
    scope: dict[str, Any],
    registry: dict[str, Any],
    redaction: dict[str, Any],
    artifact_roots: dict[str, pathlib.Path],
    label: str,
) -> None:
    aggregate_path = producer_artifact_path(producer, artifact_roots, "aggregate_path", label)
    if producer.get("aggregate_sha256") != file_digest(aggregate_path):
        raise ValueError(f"{label}: producer aggregate digest does not match artifact bytes")
    aggregate = load_json(aggregate_path)
    if aggregate.get("schema_version") != "datapan.health-bounded-observation-run.v1":
        raise ValueError(f"{label}: producer aggregate schema is not the bounded Health run contract")
    aggregate_producer = as_dict(aggregate.get("producer"), f"{label}.producer_aggregate.producer")
    if aggregate_producer.get("repository") != producer.get("repository") or aggregate_producer.get("revision") != producer.get("revision"):
        raise ValueError(f"{label}: producer aggregate immutable producer binding does not match")
    aggregate_registry = as_dict(aggregate.get("registry"), f"{label}.producer_aggregate.registry")
    for field in ("manifest_sha256", "source_sha256", "policy_sha256"):
        if aggregate_registry.get(field) != registry.get(field):
            raise ValueError(f"{label}: producer aggregate Registry policy binding does not match")
    run = as_dict(aggregate.get("run"), f"{label}.producer_aggregate.run")
    if run.get("run_id") != execution.get("run_id") or run.get("shard_count") != 8:
        raise ValueError(f"{label}: producer aggregate run binding does not match")
    if run.get("batch_size") != execution.get("batch_size") or run.get("max_parallel") != execution.get("max_parallelism") or run.get("timeout_ms") != execution.get("per_operation_timeout_seconds") * 1000:
        raise ValueError(f"{label}: producer aggregate bounded execution values do not match")
    aggregate_summary = as_dict(aggregate.get("aggregate"), f"{label}.producer_aggregate.aggregate")
    if (
        aggregate_summary.get("completeness") != "complete"
        or aggregate_summary.get("timed_out") is not False
        or aggregate_summary.get("terminal_state") != execution.get("terminal_state")
    ):
        raise ValueError(f"{label}: producer aggregate does not assert complete eight-shard coverage")
    shards = as_list(aggregate.get("shards"), f"{label}.producer_aggregate.shards")
    aggregate_indexes = [
        as_dict(item, f"{label}.producer_aggregate.shards[]").get("index")
        for item in shards
        if isinstance(item, dict)
    ]
    if sorted(aggregate_indexes) != list(range(8)):
        raise ValueError(f"{label}: producer aggregate does not enumerate shard indexes 0 through 7")
    for aggregate_shard in shards:
        item = as_dict(aggregate_shard, f"{label}.producer_aggregate.shards[]")
        if item.get("receipt_available") is not True or item.get("completed") is not True:
            raise ValueError(f"{label}: producer aggregate includes an unavailable or incomplete shard")
        if (
            item.get("manifest_sha256") != registry.get("manifest_sha256")
            or item.get("policy_sha256") != registry.get("policy_sha256")
            or item.get("scope") != scope
        ):
            raise ValueError(f"{label}: producer aggregate shard Registry policy binding does not match")
    matches = [
        as_dict(item, f"{label}.producer_aggregate.shards[]")
        for item in shards
        if isinstance(item, dict) and item.get("index") == execution.get("shard_index")
    ]
    if execution.get("shard_index") is not None:
        if len(matches) != 1:
            raise ValueError(f"{label}: producer aggregate must contain exactly one matching shard")
        shard = matches[0]
        expected = {
            "receipt_path": producer.get("receipt_path"),
            "receipt_sha256": producer.get("receipt_sha256"),
            "shard_digest": execution.get("shard_digest"),
            "scope": scope,
            "terminal_state": execution.get("terminal_state"),
        }
        for key, expected_value in expected.items():
            if shard.get(key) != expected_value:
                raise ValueError(f"{label}: producer aggregate shard mapping does not match outer admission envelope")
        if shard.get("receipt_available") is not True or shard.get("completed") is not True:
            raise ValueError(f"{label}: producer aggregate shard is not an available completed receipt")
    elif producer.get("receipt_path") != producer.get("aggregate_path") or producer.get("receipt_sha256") != producer.get("aggregate_sha256"):
        raise ValueError(f"{label}: live Health receipt must be the complete aggregate artifact")
    aggregate_redaction = as_dict(aggregate.get("redaction"), f"{label}.producer_aggregate.redaction")
    if any(as_dict(item, f"{label}.producer_aggregate.shards[]").get("redaction") != aggregate_redaction for item in shards) or not all(value is True for value in aggregate_redaction.values()):
        raise ValueError(f"{label}: producer aggregate redaction assertions do not prove a fully redacted shard")
    if redaction.get("secret_values_present") is not False or redaction.get("secret_hashes_present") is not False or redaction.get("request_urls_present") is not False or redaction.get("response_bodies_present") is not False:
        raise ValueError(f"{label}: outer redaction mapping is not safe")


def validate_execution_plan(execution_plan: dict[str, Any], manifest_path: pathlib.Path, label: str) -> None:
    expected_path = "reports/health-runtime-observation-plan.v1.json"
    if execution_plan.get("path") != expected_path:
        raise ValueError(f"{label}: Health execution plan path is not authorized")
    plan_path = rooted_file(manifest_path.parent, expected_path, f"{label}.execution_plan.path")
    if execution_plan.get("sha256") != file_digest(plan_path):
        raise ValueError(f"{label}: Health execution plan digest does not match")
    manifest = load_json(manifest_path)
    entries = [item for item in as_list(manifest.get("artifacts"), "manifest.artifacts") if isinstance(item, dict) and item.get("path") == expected_path]
    if len(entries) != 1 or entries[0].get("sha256") != execution_plan.get("sha256"):
        raise ValueError(f"{label}: Health execution plan is not manifest-bound")
    # Keep this projection exactly aligned with the static-plan generator.
    plan = load_json(plan_path)
    digest = health_plan_manifest_binding(manifest)
    if execution_plan.get("manifest_binding_sha256") != digest or plan.get("manifest_binding", {}).get("sha256") != digest:
        raise ValueError(f"{label}: Health execution plan manifest binding does not match")


def validate_receipt(receipt: dict[str, Any], *, schema: dict[str, Any], policy: dict[str, Any], policy_path: pathlib.Path, manifest_path: pathlib.Path, manifest_sha256: str, source_sha256: str, artifact_roots: dict[str, pathlib.Path], admitted_at: datetime, label: str) -> None:
    validate_schema(receipt, schema, label)
    scan_redaction(receipt, policy, label)
    if receipt.get("receipt_digest") != canonical_digest(receipt):
        raise ValueError(f"{label}: receipt_digest does not match canonical receipt bytes")
    producer_contracts = as_dict(policy.get("producer_contracts"), "policy.producer_contracts")
    kind = receipt.get("receipt_kind")
    contract = as_dict(producer_contracts.get(kind), f"policy.producer_contracts.{kind}")
    producer = as_dict(receipt.get("producer"), f"{label}.producer")
    if producer.get("repository") != contract.get("repository"):
        raise ValueError(f"{label}: producer.repository is not authorized for {kind}")
    scope = as_dict(receipt.get("scope"), f"{label}.scope")
    if scope.get("subject") != contract.get("subject"):
        raise ValueError(f"{label}: scope.subject is not authorized for {kind}")
    if "provider" in contract and scope.get("provider") != contract.get("provider"):
        raise ValueError(f"{label}: scope.provider is not authorized for {kind}")
    if receipt.get("outcome") not in as_list(contract.get("outcomes"), f"policy.producer_contracts.{kind}.outcomes"):
        raise ValueError(f"{label}: outcome is not valid for {kind}")
    generated_at = parse_time(receipt.get("generated_at"), f"{label}.generated_at")
    if generated_at > admitted_at:
        raise ValueError(f"{label}: generated_at is in the future of admission time")
    max_age_seconds = contract.get("max_age_seconds")
    if not isinstance(max_age_seconds, int) or max_age_seconds <= 0:
        raise ValueError(f"policy.producer_contracts.{kind}.max_age_seconds must be a positive integer")
    if (admitted_at - generated_at).total_seconds() > max_age_seconds:
        raise ValueError(f"{label}: receipt is stale at admission time")
    artifact_path = producer_artifact_path(producer, artifact_roots, "receipt_path", label)
    if producer.get("receipt_sha256") != file_digest(artifact_path):
        raise ValueError(f"{label}: producer receipt digest does not match artifact bytes")
    registry = as_dict(receipt.get("registry"), f"{label}.registry")
    if registry.get("manifest_path") != manifest_path.as_posix() or registry.get("manifest_sha256") != manifest_sha256:
        raise ValueError(f"{label}: registry manifest binding does not match the admitted manifest")
    manifest = load_json(manifest_path)
    if registry.get("source_path") != manifest.get("source_registry"):
        raise ValueError(f"{label}: registry source path does not match the admitted manifest")
    if registry.get("source_sha256") != source_sha256:
        raise ValueError(f"{label}: registry source digest does not match the admitted manifest source")
    if registry.get("policy_path") != policy_path.as_posix() or registry.get("policy_sha256") != file_digest(policy_path):
        raise ValueError(f"{label}: registry admission policy binding does not match")
    if kind in {RUNTIME_KIND, LIVE_KIND}:
        field = "execution" if kind == RUNTIME_KIND else "live_execution"
        execution = as_dict(receipt.get(field), f"{label}.{field}")
        validate_execution_plan(as_dict(receipt.get("execution_plan"), f"{label}.execution_plan"), manifest_path, label)
        for field, policy_field in (("shard_count", "shard_count"), ("batch_size", "batch_size_max"), ("max_parallelism", "max_parallelism_max"), ("per_operation_timeout_seconds", "per_operation_timeout_seconds_max")):
            if field == "shard_count":
                if execution.get(field) != contract.get(policy_field):
                    raise ValueError(f"{label}: execution.{field} must match policy")
            elif not isinstance(execution.get(field), int) or execution[field] > contract.get(policy_field):
                raise ValueError(f"{label}: execution.{field} exceeds policy")
        if execution.get("terminal_state") != receipt.get("outcome"):
            raise ValueError(f"{label}: execution.terminal_state must equal outcome")
        aggregate_scope = scope if kind == RUNTIME_KIND else RUNTIME_SCOPE
        validate_health_aggregate(
            producer,
            execution,
            aggregate_scope,
            registry,
            as_dict(receipt.get("redaction"), f"{label}.redaction"),
            artifact_roots,
            label,
        )


def validate_required_live_release(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    live = [receipt for receipt in receipts if receipt.get("receipt_kind") == LIVE_KIND]
    if len(live) != 1:
        raise ValueError("pre-publication admission requires exactly one health_live_observation receipt")
    return live[0]


def validate_runtime_completeness(receipts: list[dict[str, Any]]) -> str:
    runtime = [receipt for receipt in receipts if receipt.get("receipt_kind") == RUNTIME_KIND]
    if len(runtime) != 8:
        raise ValueError(f"runtime completeness requires exactly 8 shard receipts, got {len(runtime)}")
    run_ids = {as_dict(receipt["execution"], "runtime.execution").get("run_id") for receipt in runtime}
    if len(run_ids) != 1:
        raise ValueError("runtime completeness requires one shared run_id")
    repositories = {as_dict(receipt["producer"], "runtime.producer").get("repository") for receipt in runtime}
    revisions = {as_dict(receipt["producer"], "runtime.producer").get("revision") for receipt in runtime}
    registry_bindings = {
        json.dumps(as_dict(receipt["registry"], "runtime.registry"), sort_keys=True, separators=(",", ":"))
        for receipt in runtime
    }
    scopes = {
        json.dumps(as_dict(receipt["scope"], "runtime.scope"), sort_keys=True, separators=(",", ":"))
        for receipt in runtime
    }
    if len(repositories) != 1 or len(revisions) != 1:
        raise ValueError("runtime completeness requires one producer repository and revision")
    if len(registry_bindings) != 1:
        raise ValueError("runtime completeness requires one Registry manifest/source binding")
    if len(scopes) != 1:
        raise ValueError("runtime completeness requires one runtime scope")
    indices = [as_dict(receipt["execution"], "runtime.execution").get("shard_index") for receipt in runtime]
    if sorted(indices) != list(range(8)):
        raise ValueError("runtime completeness requires shard indexes 0 through 7 exactly once")
    digests = [as_dict(receipt["execution"], "runtime.execution").get("shard_digest") for receipt in runtime]
    if len(set(digests)) != 8:
        raise ValueError("runtime completeness requires unique shard_digest values")
    return str(next(iter(run_ids)))
