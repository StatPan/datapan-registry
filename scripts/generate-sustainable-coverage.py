#!/usr/bin/env python3
"""Generate or check the layered, freshness-aware registry coverage report."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover
    raise SystemExit("missing dependency: install jsonschema") from exc


POLICY_PATH = pathlib.Path("policy/sustainable-coverage.json")
POLICY_SCHEMA_PATH = pathlib.Path("schemas/datapan.sustainable-coverage-policy.v1.schema.json")
REPORT_SCHEMA_PATH = pathlib.Path("schemas/datapan.sustainable-coverage.v1.schema.json")
OUTPUT_PATH = pathlib.Path("reports/sustainable-coverage.json")
INPUT_PATHS = {
    "manifest": pathlib.Path("manifest.json"),
    "source_contract_rollup": pathlib.Path("reports/source-contract-rollup.json"),
    "source_runtime_evidence_rollup": pathlib.Path("reports/source-runtime-evidence-rollup.json"),
    "latest_verification": pathlib.Path("reports/latest-verification.json"),
    "credential_runtime_runner_readiness": pathlib.Path("reports/credential-runtime-runner-readiness.json"),
    "release_consumer_compatibility": pathlib.Path("reports/release-consumer-compatibility.json"),
    "failure_recovery_rollup": pathlib.Path("reports/failure-recovery-rollup.json"),
}


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def validate_schema(value: dict[str, Any], schema_path: pathlib.Path) -> None:
    validator = jsonschema.Draft202012Validator(
        load_json(schema_path), format_checker=jsonschema.FormatChecker()
    )
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(
            f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors
        )
        raise ValueError(f"{schema_path}: {details}")


def parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a date-time string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def objects(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{label} must be an array of objects")
    return value


def require_exact_ids(label: str, expected: set[str], actual: set[str]) -> None:
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{label} must match supported sources exactly; missing={missing}, extra={extra}")


def percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        raise ValueError("coverage denominator must be positive")
    return round(numerator / denominator * 100, 1)


def identity(result: dict[str, Any]) -> tuple[str, str, str]:
    values = (result.get("provider"), result.get("dataset_id"), result.get("operation"))
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("verification result lacks provider/dataset_id/operation identity")
    return values  # type: ignore[return-value]


def freshness_counts(
    results: list[dict[str, Any]], as_of: datetime, fresh_days: int, expire_days: int
) -> tuple[dict[str, int], set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    if expire_days <= fresh_days:
        raise ValueError("freshness.expire_days must be greater than fresh_days")
    counts = {"fresh": 0, "stale": 0, "expired": 0, "unknown_timestamp": 0, "fresh_verified": 0}
    evidenced: set[tuple[str, str, str]] = set()
    fresh_verified: set[tuple[str, str, str]] = set()
    fresh_boundary = as_of - timedelta(days=fresh_days)
    expire_boundary = as_of - timedelta(days=expire_days)
    for index, result in enumerate(results):
        operation_identity = identity(result)
        evidenced.add(operation_identity)
        raw_time = result.get("verified_at")
        if raw_time is None:
            counts["unknown_timestamp"] += 1
            continue
        verified_at = parse_time(raw_time, f"results[{index}].verified_at")
        if verified_at > as_of:
            raise ValueError(f"results[{index}].verified_at is after the evaluation time")
        if verified_at >= fresh_boundary:
            bucket = "fresh"
        elif verified_at >= expire_boundary:
            bucket = "stale"
        else:
            bucket = "expired"
        counts[bucket] += 1
        if bucket == "fresh" and result.get("status") == "verified":
            counts["fresh_verified"] += 1
            fresh_verified.add(operation_identity)
    return counts, evidenced, fresh_verified


def layer(
    layer_id: str,
    numerator: int,
    denominator: int,
    target: float,
    scope: str,
    meaning: str,
) -> dict[str, Any]:
    actual = percent(numerator, denominator)
    return {
        "id": layer_id,
        "numerator": numerator,
        "denominator": denominator,
        "percent": actual,
        "target_percent": target,
        "meets_target": actual >= target,
        "scope": scope,
        "meaning": meaning,
    }


def build_report(policy: dict[str, Any], inputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    manifest = inputs["manifest"]
    as_of_text = manifest.get("generated_at")
    as_of = parse_time(as_of_text, "manifest.generated_at")
    supported = objects(policy.get("supported_sources"), "policy.supported_sources")
    supported_ids = [str(item.get("source_id")) for item in supported]
    if len(supported_ids) != len(set(supported_ids)):
        raise ValueError("policy supported source IDs must be unique")

    contract_sources = objects(inputs["source_contract_rollup"].get("sources"), "source contract sources")
    contract_by_id = {str(item.get("source_id")): item for item in contract_sources}
    require_exact_ids("source contract rollup", set(supported_ids), set(contract_by_id))
    runtime_sources = objects(inputs["source_runtime_evidence_rollup"].get("sources"), "runtime sources")
    runtime_by_id = {str(item.get("source_id")): item for item in runtime_sources}
    require_exact_ids("runtime evidence rollup", set(supported_ids), set(runtime_by_id))
    runner_sources = objects(inputs["credential_runtime_runner_readiness"].get("sources"), "runner sources")
    runner_by_id = {str(item.get("source_id")): item for item in runner_sources}
    require_exact_ids("credential runner readiness", set(supported_ids), set(runner_by_id))
    recovery = inputs["failure_recovery_rollup"]
    recovery_summary = recovery.get("summary")
    if not isinstance(recovery_summary, dict):
        raise ValueError("failure recovery rollup summary must be an object")
    recovery_failures = objects(recovery.get("failures"), "failure recovery failures")
    active_recovery_by_source = {
        source_id: sum(row.get("source_id") == source_id and row.get("status") != "recovered" for row in recovery_failures)
        for source_id in supported_ids
    }
    recovered_by_source = {
        source_id: sum(row.get("source_id") == source_id and row.get("status") == "recovered" for row in recovery_failures)
        for source_id in supported_ids
    }

    operation_sources = [item for item in supported if item.get("catalog_scope") == "operation_denominator"]
    operations = callable_operations = 0
    for item in operation_sources:
        path = pathlib.Path(str(item["coverage_report"]))
        coverage = load_json(path)
        summary = coverage.get("summary")
        if not isinstance(summary, dict):
            raise ValueError(f"{path}.summary must be an object")
        operations += int(summary.get("operations", 0))
        callable_operations += int(summary.get("callable_operations", 0))
    if operations <= 0:
        raise ValueError("at least one operation denominator with operations is required")

    freshness_policy = policy["freshness"]
    results = objects(inputs["latest_verification"].get("results"), "latest verification results")
    freshness, evidenced_operations, fresh_verified_operations = freshness_counts(
        results, as_of, int(freshness_policy["fresh_days"]), int(freshness_policy["expire_days"])
    )
    if len(evidenced_operations) > operations:
        raise ValueError("unique evidenced operations exceed the declared operation denominator")

    required_consumers = list(policy["required_consumers"])
    consumers = objects(inputs["release_consumer_compatibility"].get("consumers"), "consumers")
    consumer_by_id = {str(item.get("consumer")): item for item in consumers}
    missing_consumers = sorted(set(required_consumers).difference(consumer_by_id))
    if missing_consumers:
        raise ValueError(f"required consumers missing from compatibility report: {missing_consumers}")

    target = policy["targets"]
    source_count = len(supported)
    source_status = []
    for item in supported:
        source_id = str(item["source_id"])
        contract = contract_by_id[source_id]
        runtime = runtime_by_id[source_id]
        runner = runner_by_id[source_id]
        capabilities = contract.get("adapter", {}).get("capabilities", [])
        source_status.append({
            "source_id": source_id,
            "profile_present": pathlib.Path(str(item["profile"])).is_file(),
            "catalog_denominator": item.get("catalog_scope") == "operation_denominator",
            "call_capability": isinstance(capabilities, list) and "call" in capabilities,
            "reviewed_runtime_receipt": bool(runner.get("reviewed_receipt_present")),
            "runtime_evidence": int(runtime.get("evidence_total", 0)),
            "active_recovery_failures": active_recovery_by_source[source_id],
            "recovered_failures": recovered_by_source[source_id],
        })

    layers = [
        layer("source_contract", sum(row["profile_present"] for row in source_status), source_count, target["source_contract_percent"], "supported_sources", "Supported sources with a checked-in validated profile."),
        layer("catalog_denominator", len(operation_sources), source_count, target["catalog_denominator_percent"], "supported_sources", "Supported sources with an explicit operation denominator; contract-only sources remain uncovered."),
        layer("operation_routable", callable_operations, operations, target["operation_routable_percent"], "operation_denominator_sources", "Operations statically marked callable; this is not runtime success."),
        layer("source_call_capability", sum(row["call_capability"] for row in source_status), source_count, target["source_call_capability_percent"], "supported_sources", "Sources whose registered adapter declares call capability."),
        layer("reviewed_runtime_receipt_source", sum(row["reviewed_runtime_receipt"] for row in source_status), source_count, target["reviewed_runtime_receipt_source_percent"], "supported_sources", "Sources with a reviewed, redacted credential runtime receipt; secret presence itself is never persisted as coverage."),
        layer("runtime_evidence_source", sum(row["runtime_evidence"] > 0 for row in source_status), source_count, target["runtime_evidence_source_percent"], "supported_sources", "Sources with at least one runtime evidence record."),
        layer("runtime_evidence_operation", len(evidenced_operations), operations, target["runtime_evidence_operation_percent"], "operation_denominator_sources", "Unique operation identities with evidence, regardless of result or age."),
        layer("fresh_verified_operation", len(fresh_verified_operations), operations, target["fresh_verified_operation_percent"], "operation_denominator_sources", "Unique operation identities with a successful result inside the fresh window."),
        layer("required_consumer_proven", sum(consumer_by_id[name].get("status") == "proven" for name in required_consumers), len(required_consumers), target["required_consumer_proven_percent"], "required_consumers", "Required consumers with proven compatibility evidence."),
    ]
    met = sum(item["meets_target"] for item in layers)
    next_actions = [
        item["id"] for item in layers if not item["meets_target"]
    ]
    return {
        "schema_version": "datapan.sustainable-coverage.v1",
        "generated_at": as_of_text,
        "policy": {"path": POLICY_PATH.as_posix(), "policy_id": policy["policy_id"], "supported_source_count": source_count, "required_consumer_count": len(required_consumers)},
        "inputs": {name: path.as_posix() for name, path in INPUT_PATHS.items()},
        "summary": {"decision": "sustainable" if met == len(layers) else "coverage_gaps", "layers_total": len(layers), "layers_meeting_target": met, "layers_below_target": len(layers) - met, "unknown_timestamp_records": freshness["unknown_timestamp"], "stale_records": freshness["stale"], "expired_records": freshness["expired"]},
        "layers": layers,
        "freshness": {"as_of": as_of_text, "fresh_days": freshness_policy["fresh_days"], "expire_days": freshness_policy["expire_days"], **freshness},
        "failure_recovery": {key: int(recovery_summary.get(key, 0)) for key in ("active", "persistent", "recovered", "unowned", "overdue", "durable_work_items")},
        "source_status": source_status,
        "maintenance_boundary": {"routable_is_not_runtime_verified": True, "missing_denominators_are_uncovered": True, "missing_timestamps_are_not_fresh": True, "recovery_updates_coverage": True, "next_actions": next_actions},
    }


def self_test() -> None:
    as_of = datetime(2026, 7, 10, tzinfo=timezone.utc)
    sample = lambda when, status="verified": {"provider": "p", "dataset_id": "d", "operation": "o" + str(when), "status": status, **({"verified_at": when} if when else {})}
    counts, evidenced, verified = freshness_counts([
        sample("2026-07-09T00:00:00Z"), sample("2026-06-01T00:00:00Z"), sample("2026-01-01T00:00:00Z"), sample(None, "skipped")
    ], as_of, 30, 90)
    assert counts == {"fresh": 1, "stale": 1, "expired": 1, "unknown_timestamp": 1, "fresh_verified": 1}
    assert len(evidenced) == 4 and len(verified) == 1
    assert percent(0, 5) == 0.0
    assert not layer("catalog_denominator", 1, 5, 100.0, "sources", "test")["meets_target"]
    try:
        require_exact_ids("fixture", {"a", "b"}, {"a"})
    except ValueError:
        pass
    else:
        raise AssertionError("inconsistent source inputs must fail")
    try:
        freshness_counts([sample("2027-01-01T00:00:00Z")], as_of, 30, 90)
    except ValueError:
        pass
    else:
        raise AssertionError("future evidence must fail")
    print("ok sustainable coverage self-test (missing denominator, timestamps, stale evidence, source mismatch)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--output", type=pathlib.Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
            return 0
        policy = load_json(POLICY_PATH)
        validate_schema(policy, POLICY_SCHEMA_PATH)
        report = build_report(policy, {name: load_json(path) for name, path in INPUT_PATHS.items()})
        validate_schema(report, REPORT_SCHEMA_PATH)
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
                raise ValueError(f"{args.output} is stale; run this script without --check")
            print(f"ok {args.output} (layers_meeting_target={report['summary']['layers_meeting_target']}/9)")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
            print(f"wrote {args.output} (layers_meeting_target={report['summary']['layers_meeting_target']}/9)")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL sustainable coverage: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
