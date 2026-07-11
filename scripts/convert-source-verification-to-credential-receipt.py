#!/usr/bin/env python3
"""Convert generic CLI source verification into a registry-owned receipt."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import credential_runtime_receipts as receipts


POLICY = pathlib.Path("reports/credential-runtime-evidence-policy.json")
SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-receipt.v1.schema.json")
VERIFICATION_SCHEMA = pathlib.Path("schemas/datapan.source-candidate-verification.v1.schema.json")
ERROR_CLASSES = {"credential", "rate_limit", "provider", "parser", "network", "timeout", "unknown"}


def source_policy(policy: dict[str, Any], source_id: str) -> dict[str, Any]:
    for item in receipts.as_list(policy.get("sources"), "policy.sources"):
        source = receipts.as_dict(item, "policy.sources[]")
        if source.get("source_id") == source_id:
            return source
    raise ValueError(f"unknown credential source: {source_id}")


def build(verification: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    receipts.validate_schema(verification, receipts.load_json(VERIFICATION_SCHEMA), "generic source verification")
    if verification.get("schema_version") != "datapan.source-candidate-verification.v1":
        raise ValueError("unsupported source verification schema")
    if verification.get("source_id") != source.get("source_id"):
        raise ValueError("verification source_id does not match policy")
    redaction = receipts.as_dict(verification.get("redaction"), "verification.redaction")
    if any(redaction.get(key) is not False for key in ("secret_values_present", "secret_hashes_present", "request_urls_present", "response_bodies_present")):
        raise ValueError("generic verification redaction boundary is not clean")
    results = [receipts.as_dict(row, "verification.results[]") for row in receipts.as_list(verification.get("results"), "verification.results")]
    failed = [row for row in results if row.get("outcome") == "failed"]
    verified = bool(results) and not failed and all(row.get("outcome") == "verified" for row in results)
    raw_error = str(failed[0].get("error_class", "unknown")) if failed else "none"
    error_class = raw_error if raw_error in ERROR_CLASSES else "provider" if failed else "none"
    statuses = [int(row["http_status"]) for row in results if isinstance(row.get("http_status"), int)]
    return {
        "schema_version": receipts.SCHEMA_VERSION,
        "generated_at": verification["generated_at"],
        "source_id": source["source_id"],
        "provider": source["provider"],
        "candidate_batch": source["candidate_batch"],
        "runtime_evidence_plan": source["runtime_evidence_plan"],
        "credential_configured": bool(verification.get("credential_configured")),
        "credential_envs": source["credential_envs"],
        "bounded": True,
        "execution": {
            "started_at": verification["generated_at"],
            "finished_at": verification["generated_at"],
            "duration_ms": sum(int(row.get("duration_ms", 0)) for row in results),
            "request_count": sum(row.get("outcome") != "skipped" for row in results),
        },
        "outcome": "verified" if verified else "failed" if failed else "skipped",
        "error_class": error_class if failed else "none" if verified else "not_run",
        "response_metadata": {
            "candidate_count": len(results),
            "verified_count": sum(row.get("outcome") == "verified" for row in results),
            "failed_count": len(failed),
            "http_status_min": min(statuses) if statuses else None,
            "http_status_max": max(statuses) if statuses else None,
        },
        "redaction": {
            "secret_values_present": False,
            "secret_hashes_present": False,
            "forbidden_fields_checked": sorted(receipts.FORBIDDEN_KEYS),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verification", required=True, type=pathlib.Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        policy = receipts.load_json(POLICY)
        source = source_policy(policy, args.source)
        result = build(receipts.load_json(args.verification), source)
        receipts.validate_receipt(result, receipts.load_json(SCHEMA), receipts.source_lookup([source]), args.output.as_posix(), require_review=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.output}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL convert source verification receipt: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
