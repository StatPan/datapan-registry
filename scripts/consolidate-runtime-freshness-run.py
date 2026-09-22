#!/usr/bin/env python3
"""Validate rotating shard evidence and emit a secret-free run receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Any

import jsonschema


DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.runtime-freshness-run-receipt.v1.schema.json")
FORBIDDEN_KEYS = {"url", "request_url", "request_urls", "response_body", "response_bodies", "body", "credential_value", "credential_hash", "authorization", "authorization_header", "servicekey", "service_key", "apikey", "api_key", "secret", "token"}
SECRET_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (r"authorization:\s*bearer", r"bearer\s+[a-z0-9._~+/=-]{16,}", r"servicekey=", r"api[_-]?key=", r"secret=", r"token="))
IDENTITY_ALGORITHM = "data_go_kr.batch-plan-identity-key.v1"
IDENTITY_DIGEST_ALGORITHM = "sha256-canonical-json-array.v1"
SECRET_VALUE_REPLACEMENTS = (
    (re.compile(r"authorization:\s*bearer\s+[^\s,;\]\)}]+", re.IGNORECASE), "[redacted authorization]"),
    (re.compile(r"bearer\s+[a-z0-9._~+/=-]{16,}", re.IGNORECASE), "[redacted bearer credential]"),
    (
        re.compile(r"(?:service[_-]?key|api[_-]?key|secret|token)\s*=\s*[^\s&,;\]\)}]+", re.IGNORECASE),
        "[redacted credential assignment]",
    ),
)


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def file_record(path: pathlib.Path, root: pathlib.Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": path.relative_to(root).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def sanitize(value: object) -> object:
    if isinstance(value, dict):
        return {key: sanitize(child) for key, child in value.items() if key.lower() not in FORBIDDEN_KEYS}
    if isinstance(value, list):
        return [sanitize(child) for child in value]
    if isinstance(value, str):
        for pattern, replacement in SECRET_VALUE_REPLACEMENTS:
            value = pattern.sub(replacement, value)
        return value
    return value


def scan_boundary(value: object, label: str = "report") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_KEYS:
                raise ValueError(f"{label}: forbidden field {key}")
            scan_boundary(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            scan_boundary(child, f"{label}[{index}]")
    elif isinstance(value, str):
        for pattern in SECRET_PATTERNS:
            if pattern.search(value):
                raise ValueError(f"{label}: secret-like string matches {pattern.pattern}")


def identity_set_digest(identities: set[str]) -> str:
    encoded = json.dumps(sorted(identities), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def result_key(value: dict[str, Any], *, label: str) -> tuple[str, str]:
    dataset_id, operation = value.get("dataset_id"), value.get("operation")
    if not isinstance(dataset_id, str) or not dataset_id:
        raise ValueError(f"{label}: empty dataset_id")
    if not isinstance(operation, str) or not operation:
        raise ValueError(f"{label}: empty operation")
    return dataset_id, operation


def mapped_result_identity(
    result: object,
    *,
    label: str,
    planned_by_result_key: dict[tuple[str, str], str],
) -> str:
    if not isinstance(result, dict):
        raise ValueError(f"{label}: result must be an object")
    key = result_key(result, label=label)
    identity = planned_by_result_key.get(key)
    if identity is None:
        raise ValueError(f"{label}: unplanned result identity dataset_id={key[0]!r} operation={key[1]!r}")
    supplied = result.get("identity_key")
    if supplied is not None and supplied != identity:
        raise ValueError(f"{label}: result identity_key does not match its batch plan")
    return identity


def build(root: pathlib.Path, combined_path: pathlib.Path, *, expected_shards: int, run_id: str) -> dict[str, Any]:
    plans = sorted(root.rglob("batch-plan.json"))
    if len(plans) != expected_shards:
        raise ValueError(f"expected {expected_shards} shard plans, got {len(plans)}")
    combined = load(combined_path)
    scan_boundary(combined, "combined verification")
    combined_results = combined.get("results")
    if not isinstance(combined_results, list):
        raise ValueError("combined verification results must be an array")

    planned_identities: set[str] = set()
    planned_by_result_key: dict[tuple[str, str], str] = {}
    shard_inputs: list[tuple[pathlib.Path, pathlib.Path, int, set[str], list[Any]]] = []
    shard_indices: list[int] = []
    for plan_path in plans:
        directory = plan_path.parent
        verification_path = directory / "verification.json"
        exit_path = directory / "exit-code.txt"
        if not verification_path.is_file() or not exit_path.is_file():
            raise ValueError(f"{directory}: missing verification or exit code")
        plan, verification = load(plan_path), load(verification_path)
        operations, results = plan.get("operations"), verification.get("results")
        if not isinstance(operations, list) or not operations or not isinstance(results, list):
            raise ValueError(f"{directory}: invalid plan or verification results")
        selection = plan.get("selection")
        shard_index = selection.get("shard_index") if isinstance(selection, dict) else None
        if type(shard_index) is not int:
            raise ValueError(f"{plan_path}: missing shard index")
        shard_count = selection.get("shard_count") if isinstance(selection, dict) else None
        if shard_count is not None and shard_count != expected_shards:
            raise ValueError(f"{plan_path}: shard_count expected {expected_shards}, got {shard_count}")
        shard_indices.append(shard_index)
        shard_planned: set[str] = set()
        for index, operation in enumerate(operations):
            label = f"{plan_path}: operation[{index}]"
            if not isinstance(operation, dict):
                raise ValueError(f"{label}: operation must be an object")
            identity = operation.get("identity_key")
            if not isinstance(identity, str) or not identity:
                raise ValueError(f"{label}: empty planned identity")
            if identity in planned_identities:
                raise ValueError(f"{label}: duplicate planned identity {identity!r}")
            key = result_key(operation, label=label)
            prior = planned_by_result_key.get(key)
            if prior is not None:
                raise ValueError(
                    f"{label}: ambiguous result identity mapping for dataset_id={key[0]!r} "
                    f"operation={key[1]!r}: {prior!r} and {identity!r}"
                )
            planned_identities.add(identity)
            planned_by_result_key[key] = identity
            shard_planned.add(identity)
        shard_inputs.append((verification_path, exit_path, shard_index, shard_planned, results))

    expected_indices = list(range(expected_shards))
    if sorted(shard_indices) != expected_indices:
        raise ValueError(f"shard indices must equal {expected_indices}, got {sorted(shard_indices)}")

    shards: list[dict[str, Any]] = []
    shard_reported_identities: set[str] = set()
    total_results = 0
    for verification_path, exit_path, shard_index, shard_planned, results in shard_inputs:
        shard_reported: set[str] = set()
        for index, result in enumerate(results):
            label = f"{verification_path}: result[{index}]"
            identity = mapped_result_identity(result, label=label, planned_by_result_key=planned_by_result_key)
            if identity not in shard_planned:
                raise ValueError(f"{label}: result belongs to a different shard plan: {identity!r}")
            if identity in shard_reported_identities:
                raise ValueError(f"{label}: duplicate reported identity {identity!r}")
            shard_reported.add(identity)
            shard_reported_identities.add(identity)
        missing = sorted(shard_planned - shard_reported)
        if missing:
            raise ValueError(f"{verification_path}: planned identities missing results: {missing[:5]}")
        raw_exit = exit_path.read_text(encoding="utf-8").strip()
        if not raw_exit.isdigit():
            raise ValueError(f"{exit_path}: malformed exit code")
        total_results += len(results)
        shards.append({
            "shard_index": shard_index,
            "operation_count": len(shard_planned),
            "exit_code": int(raw_exit),
            "batch_plan": file_record(plan_path, root),
            "verification": file_record(verification_path, root),
        })

    combined_identities: set[str] = set()
    enriched_results: list[dict[str, Any]] = []
    for index, result in enumerate(combined_results):
        label = f"{combined_path}: result[{index}]"
        identity = mapped_result_identity(result, label=label, planned_by_result_key=planned_by_result_key)
        if identity in combined_identities:
            raise ValueError(f"{label}: duplicate reported identity {identity!r}")
        combined_identities.add(identity)
        assert isinstance(result, dict)
        enriched_results.append({**result, "identity_key": identity})
    missing = sorted(planned_identities - combined_identities)
    if missing:
        raise ValueError(f"{combined_path}: planned identities missing results: {missing[:5]}")
    if shard_reported_identities != combined_identities:
        raise ValueError("combined verification identities do not match shard verification identities")
    if total_results != len(combined_results):
        raise ValueError(f"combined result count expected {total_results}, got {len(combined_results)}")

    statuses = {name: 0 for name in ("verified", "failed", "skipped", "unknown")}
    for result in combined_results:
        status = result.get("status") if isinstance(result, dict) else None
        statuses[status if status in statuses else "unknown"] += 1
    reported_results = len(combined_identities)
    planned_operations = len(planned_identities)
    if planned_operations != reported_results or reported_results != sum(statuses.values()):
        raise ValueError(
            "planned_operations, reported_results, and status counts must reconcile exactly"
        )

    combined["results"] = enriched_results
    combined_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    planned_digest = identity_set_digest(planned_identities)
    reported_digest = identity_set_digest(combined_identities)
    shards.sort(key=lambda row: row["shard_index"])
    report = {
        "schema_version": "datapan.runtime-freshness-run-receipt.v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "run_id": run_id,
        "summary": {
            "expected_shards": expected_shards,
            "shards": len(shards),
            "planned_operations": planned_operations,
            "reported_results": reported_results,
            **statuses,
        },
        "identity_equality": {
            "identity_algorithm": IDENTITY_ALGORITHM,
            "result_identity_field": "identity_key",
            "result_mapping_fields": ["dataset_id", "operation"],
            "digest_algorithm": IDENTITY_DIGEST_ALGORITHM,
            "planned": {"count": planned_operations, "sha256": planned_digest},
            "reported": {"count": reported_results, "sha256": reported_digest},
            "equal": planned_digest == reported_digest,
        },
        "combined_verification": file_record(combined_path, root),
        "shards": shards,
        "redaction": {
            "secret_values_present": False,
            "secret_hashes_present": False,
            "request_urls_present": False,
            "response_bodies_present": False,
        },
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--combined", type=pathlib.Path, required=True)
    parser.add_argument("--sanitized-output", type=pathlib.Path, required=True)
    parser.add_argument("--expected-shards", type=int, default=8)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--schema", type=pathlib.Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        raw = load(args.combined)
        sanitized = sanitize(raw)
        scan_boundary(sanitized, "sanitized combined verification")
        args.sanitized_output.parent.mkdir(parents=True, exist_ok=True)
        args.sanitized_output.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report = build(args.root, args.sanitized_output, expected_shards=args.expected_shards, run_id=args.run_id)
        errors = list(jsonschema.Draft202012Validator(load(args.schema), format_checker=jsonschema.FormatChecker()).iter_errors(report))
        if errors:
            raise ValueError("; ".join(error.message for error in errors))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "consolidated", **report["summary"]}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL consolidate runtime freshness run: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
