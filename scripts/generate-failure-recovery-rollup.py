#!/usr/bin/env python3
"""Generate stable recurring-failure, work-item, and recovery evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sys
from collections import defaultdict
from typing import Any

try:
    import jsonschema
except ImportError as exc:
    raise SystemExit("missing dependency: install jsonschema") from exc


POLICY = pathlib.Path("policy/failure-recovery.json")
OBSERVATIONS = pathlib.Path("reports/failure-observations.json")
SCHEMA = pathlib.Path("schemas/datapan.failure-recovery-rollup.v1.schema.json")
POLICY_SCHEMA = pathlib.Path("schemas/datapan.failure-recovery-policy.v1.schema.json")
OBSERVATION_SCHEMA = pathlib.Path("schemas/datapan.failure-observations.v1.schema.json")
OUTPUT = pathlib.Path("reports/failure-recovery-rollup.json")
CREDENTIAL_READINESS = pathlib.Path("reports/credential-runtime-runner-readiness.json")
CONSUMER_COMPATIBILITY = pathlib.Path("reports/release-consumer-compatibility.json")


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def parse(value: str) -> dt.datetime:
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps require timezone")
    return result.astimezone(dt.timezone.utc)


def timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def failure_id(row: dict[str, Any]) -> str:
    identity = f"{row['source_id']}\0{row['failure_class']}\0{row['subject_id']}"
    return f"failure:{row['failure_class']}:{row['source_id']}:{hashlib.sha256(identity.encode()).hexdigest()[:16]}"


def validate(value: dict[str, Any], schema: pathlib.Path) -> None:
    jsonschema.Draft202012Validator(load(schema), format_checker=jsonschema.FormatChecker()).validate(value)


def validate_current_evidence(observations: dict[str, Any]) -> None:
    """Prevent checked-in observations from drifting away from their evidence."""
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in observations["observations"]:
        key = (row["source_id"], row["failure_class"], row["subject_id"])
        if key not in latest or parse(row["observed_at"]) >= parse(latest[key]["observed_at"]):
            latest[key] = row

    readiness = load(CREDENTIAL_READINESS)
    for source in readiness["sources"]:
        key = (source["source_id"], "credential", "reviewed-runtime-receipt")
        if key not in latest:
            raise ValueError(f"missing current credential observation for {source['source_id']}")
        expected_healthy = bool(source["reviewed_receipt_present"])
        if bool(latest[key]["healthy"]) != expected_healthy:
            raise ValueError(f"credential observation is stale for {source['source_id']}")

    compatibility = load(CONSUMER_COMPATIBILITY)
    studio = next((row for row in compatibility["consumers"] if row["consumer"] == "studio"), None)
    if studio is None:
        raise ValueError("studio consumer compatibility evidence is missing")
    key = ("multi_source", "consumer", "studio")
    if key not in latest:
        raise ValueError("missing current studio consumer observation")
    expected_healthy = studio["status"] == "proven"
    if bool(latest[key]["healthy"]) != expected_healthy:
        raise ValueError("studio consumer observation is stale")


def build(policy: dict[str, Any], observations: dict[str, Any]) -> dict[str, Any]:
    generated_at = str(observations["generated_at"])
    as_of = parse(generated_at)
    rules = {row["failure_class"]: row for row in policy["classes"]}
    if len(rules) != len(policy["classes"]):
        raise ValueError("failure policy classes must be unique")
    required = {"credential", "parameter", "adapter", "parser", "rate_limit", "upstream", "reference_drift", "catalog_drift", "consumer"}
    if set(rules) != required:
        raise ValueError(f"failure policy classes must match required taxonomy: {sorted(required)}")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations["observations"]:
        if row["failure_class"] not in rules:
            raise ValueError(f"unrouted failure class {row['failure_class']}")
        if parse(row["observed_at"]) > as_of:
            raise ValueError("observation occurs after generated_at")
        grouped[failure_id(row)].append(row)

    failures: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    work_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for identity, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: (parse(row["observed_at"]), row["evidence"], row["healthy"]))
        rule = rules[rows[-1]["failure_class"]]
        consecutive = 0
        for row in rows:
            consecutive = 0 if row["healthy"] else consecutive + 1
        status = "recovered" if rows[-1]["healthy"] else ("persistent" if consecutive >= rule["recurrence_threshold"] else "transient")
        first_failed = next((row for row in rows if not row["healthy"]), rows[0])
        due = parse(first_failed["observed_at"]) + dt.timedelta(days=int(rule["due_days"]))
        failure = {
            "failure_id": identity, "source_id": rows[-1]["source_id"], "failure_class": rows[-1]["failure_class"],
            "subject_id": rows[-1]["subject_id"], "status": status, "severity": rule["severity"],
            "owner": rule.get("owner"), "recurrence_count": consecutive, "recurrence_threshold": rule["recurrence_threshold"],
            "first_observed_at": first_failed["observed_at"], "last_observed_at": rows[-1]["observed_at"],
            "due_at": timestamp(due), "overdue": status == "persistent" and as_of > due,
            "retry": rule["retry"], "recovery_action": rule["recovery_action"],
            "evidence": sorted({row["evidence"] for row in rows}),
        }
        failures.append(failure)
        if status == "recovered":
            recoveries.append({"failure_id": identity, "recovered_at": rows[-1]["observed_at"], "evidence": rows[-1]["evidence"], "coverage_update_required": True})
        elif status == "persistent":
            group_key = f"ticket:{rule['existing_ticket']}" if rule.get("existing_ticket") else identity
            work_groups[group_key].append(failure)

    work_items = []
    for group_key, items in sorted(work_groups.items()):
        rule = rules[items[0]["failure_class"]]
        ticket = rule.get("existing_ticket")
        work_items.append({
            "work_key": f"failure-work:{group_key}", "status": "existing" if ticket else "create", "ticket": ticket,
            "owner": str(rule["owner"]), "failure_ids": sorted(item["failure_id"] for item in items),
            "evidence": sorted({path for item in items for path in item["evidence"]}),
            "next_action": rule["recovery_action"],
        })
    counts = {name: sum(row["status"] == name for row in failures) for name in ("transient", "persistent", "recovered")}
    active = counts["transient"] + counts["persistent"]
    return {
        "schema_version": "datapan.failure-recovery-rollup.v1", "generated_at": generated_at,
        "policy": POLICY.as_posix(), "observations": OBSERVATIONS.as_posix(),
        "summary": {"identities": len(failures), "active": active, **counts,
                    "owned": sum(row["status"] != "recovered" and bool(row["owner"]) for row in failures),
                    "unowned": sum(row["status"] != "recovered" and not row["owner"] for row in failures),
                    "overdue": sum(row["overdue"] for row in failures), "durable_work_items": len(work_items)},
        "failures": failures, "work_items": work_items, "recovery_receipts": recoveries,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=pathlib.Path, default=OUTPUT)
    args = parser.parse_args()
    try:
        policy, observations = load(POLICY), load(OBSERVATIONS)
        validate(policy, POLICY_SCHEMA); validate(observations, OBSERVATION_SCHEMA)
        validate_current_evidence(observations)
        report = build(policy, observations); validate(report, SCHEMA)
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
                raise ValueError(f"{args.output} is stale")
            print(f"ok {args.output} (active={report['summary']['active']}, work_items={report['summary']['durable_work_items']})")
        else:
            args.output.write_text(rendered, encoding="utf-8")
            print(f"wrote {args.output} (active={report['summary']['active']}, work_items={report['summary']['durable_work_items']})")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL failure recovery rollup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
