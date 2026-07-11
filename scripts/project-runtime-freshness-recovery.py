#!/usr/bin/env python3
"""Project a sanitized freshness run into explicit failure/recovery evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import sys
from typing import Any

import jsonschema


SCHEMA = pathlib.Path("schemas/datapan.runtime-freshness-import-receipt.v1.schema.json")
OBSERVATION_SCHEMA = pathlib.Path("schemas/datapan.failure-observations.v1.schema.json")
POLICY = pathlib.Path("policy/failure-recovery.json")
CLASS_MAP = {"approval": "credential", "bad_request": "parameter", "upstream_outage": "upstream"}


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def importer_module() -> Any:
    path = pathlib.Path(__file__).with_name("import-runtime-freshness-run.py")
    spec = importlib.util.spec_from_file_location("datapan_runtime_import", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load runtime freshness importer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def subject_id(source_id: str, result: dict[str, Any]) -> str:
    raw = f"{source_id}\0{result.get('dataset_id', '')}\0{result.get('operation', '')}"
    return f"operation:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def field_value(result: dict[str, Any], field: str) -> object:
    current: object = result
    for part in field.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def matches(rule: dict[str, Any], result: dict[str, Any]) -> bool:
    scope = rule.get("scope", {})
    classes = scope.get("dependency_classes") if isinstance(scope, dict) else None
    if isinstance(classes, list) and result.get("dependency_class") not in classes:
        return False
    match = rule.get("match")
    if not isinstance(match, dict):
        return False
    kind = match.get("kind")
    if kind == "http_status":
        return result.get("http_status") == match.get("http_status")
    if kind in {"field_equals", "field_contains"}:
        value = field_value(result, str(match.get("field", "")))
        expected = match.get("value") if kind == "field_equals" else match.get("contains")
        if value is None or expected is None:
            return False
        left, right = str(value), str(expected)
        if not match.get("case_sensitive", True):
            left, right = left.casefold(), right.casefold()
        return left == right if kind == "field_equals" else right in left
    if kind == "message_contains":
        text = " ".join(str(result.get(key, "")) for key in ("reason", "message", "semantic_status"))
        expected = str(match.get("contains", ""))
        if not match.get("case_sensitive", True):
            text, expected = text.casefold(), expected.casefold()
        return bool(expected) and expected in text
    if kind == "timeout":
        return "timeout" in " ".join(str(result.get(key, "")) for key in ("reason", "semantic_status")).casefold()
    if kind == "parse_error":
        return result.get("semantic_status") == "parse_error" or "parse" in str(result.get("reason", "")).casefold()
    return False


def classify(catalog: dict[str, Any], result: dict[str, Any], allowed: set[str]) -> tuple[str, str] | None:
    for raw_rule in catalog.get("rules", []):
        if not isinstance(raw_rule, dict) or raw_rule.get("status") not in {"verified", "active"}:
            continue
        if not matches(raw_rule, result):
            continue
        classification = str(raw_rule.get("classification", ""))
        failure_class = CLASS_MAP.get(classification, classification)
        if failure_class in allowed:
            return failure_class, str(raw_rule.get("rule_id"))
        return None
    return None


def timestamp(result: dict[str, Any], fallback: str) -> str:
    for key in ("verified_at", "checked_at", "observed_at"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def build(
    report_path: pathlib.Path, run_receipt_path: pathlib.Path, catalog_path: pathlib.Path,
    observations_path: pathlib.Path, receipt_output: pathlib.Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    report, run_receipt = load(report_path), load(run_receipt_path)
    importer_module().validate_receipt(report_path, report, run_receipt)
    catalog, observations, policy = load(catalog_path), load(observations_path), load(POLICY)
    source_id = str(catalog.get("source_id"))
    allowed = {str(row["failure_class"]) for row in policy.get("classes", []) if isinstance(row, dict)}
    existing = observations.get("observations")
    results = report.get("results")
    if not isinstance(existing, list) or not isinstance(results, list):
        raise ValueError("observations and report results must be arrays")
    generated_at = str(run_receipt.get("generated_at"))
    active_by_subject: dict[str, set[str]] = {}
    for row in existing:
        if not isinstance(row, dict) or row.get("source_id") != source_id or not str(row.get("subject_id", "")).startswith("operation:"):
            continue
        key = str(row["subject_id"])
        failure_class = str(row["failure_class"])
        active_by_subject.setdefault(key, set())
        if row.get("healthy"):
            active_by_subject[key].discard(failure_class)
        else:
            active_by_subject[key].add(failure_class)
    projected: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for raw in results:
        if not isinstance(raw, dict):
            raise ValueError("report results must contain objects")
        dataset_id, operation = str(raw.get("dataset_id", "")), str(raw.get("operation", ""))
        if not dataset_id or not operation:
            raise ValueError("runtime result requires dataset_id and operation")
        subject = subject_id(source_id, raw)
        status, reason = str(raw.get("status", "unknown")), str(raw.get("reason", ""))
        observed_at = timestamp(raw, generated_at)
        base = {"subject_id": subject, "dataset_id": dataset_id, "operation": operation, "status": status, "reason": reason}
        if status == "verified":
            active = sorted(active_by_subject.get(subject, set()))
            if not active:
                rows.append({**base, "disposition": "no_active_failure"})
            for failure_class in active:
                projected.append({"observed_at": observed_at, "source_id": source_id, "failure_class": failure_class, "subject_id": subject, "healthy": True, "evidence": receipt_output.as_posix()})
                rows.append({**base, "disposition": "healthy_recovery", "failure_class": failure_class, "observed_at": observed_at})
            continue
        classified = classify(catalog, raw, allowed)
        if classified is None:
            rows.append({**base, "disposition": "unclassified_failure"})
            continue
        failure_class, rule_id = classified
        projected.append({"observed_at": observed_at, "source_id": source_id, "failure_class": failure_class, "subject_id": subject, "healthy": False, "evidence": receipt_output.as_posix()})
        rows.append({**base, "disposition": "classified_failure", "failure_class": failure_class, "rule_id": rule_id, "observed_at": observed_at})
    unique_existing = {json.dumps(row, sort_keys=True, ensure_ascii=False) for row in existing}
    appended = [row for row in projected if json.dumps(row, sort_keys=True, ensure_ascii=False) not in unique_existing]
    updated_rows = [*existing, *appended]
    updated = {**observations, "generated_at": max(str(observations.get("generated_at")), generated_at), "observations": updated_rows}
    receipt = {
        "schema_version": "datapan.runtime-freshness-import-receipt.v1", "generated_at": generated_at,
        "run_id": str(run_receipt.get("run_id")), "source_id": source_id,
        "inputs": {"sanitized_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(), "run_receipt_sha256": hashlib.sha256(run_receipt_path.read_bytes()).hexdigest(), "error_action_catalog": catalog_path.as_posix()},
        "summary": {"reported_results": len(results), "classified_failures": sum(row["disposition"] == "classified_failure" for row in rows), "unclassified_failures": sum(row["disposition"] == "unclassified_failure" for row in rows), "healthy_recoveries": sum(row["disposition"] == "healthy_recovery" for row in rows), "observations_projected": len(projected)},
        "results": rows,
    }
    jsonschema.Draft202012Validator(load(SCHEMA), format_checker=jsonschema.FormatChecker()).validate(receipt)
    jsonschema.Draft202012Validator(load(OBSERVATION_SCHEMA), format_checker=jsonschema.FormatChecker()).validate(updated)
    return receipt, updated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--run-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--catalog", type=pathlib.Path, default=pathlib.Path("reports/data-go-kr/error-action-catalog.json"))
    parser.add_argument("--observations", type=pathlib.Path, default=pathlib.Path("reports/failure-observations.json"))
    parser.add_argument("--receipt-output", type=pathlib.Path, required=True)
    parser.add_argument("--observations-output", type=pathlib.Path, default=pathlib.Path("reports/failure-observations.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        receipt, observations = build(args.report, args.run_receipt, args.catalog, args.observations, args.receipt_output)
        rendered_receipt = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
        rendered_observations = json.dumps(observations, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not args.receipt_output.is_file() or args.receipt_output.read_text(encoding="utf-8") != rendered_receipt:
                raise ValueError("import receipt is stale")
            if not args.observations_output.is_file() or args.observations_output.read_text(encoding="utf-8") != rendered_observations:
                raise ValueError("failure observations are stale")
        else:
            args.receipt_output.parent.mkdir(parents=True, exist_ok=True)
            args.receipt_output.write_text(rendered_receipt, encoding="utf-8")
            args.observations_output.write_text(rendered_observations, encoding="utf-8")
        print(json.dumps({"status": "checked" if args.check else "projected", **receipt["summary"]}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL project runtime freshness recovery: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
