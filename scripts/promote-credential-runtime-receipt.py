#!/usr/bin/env python3
"""Promote a staged credential runtime receipt into the reviewed intake path."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import sys
from typing import Any

import credential_runtime_receipts as receipts


DEFAULT_POLICY = pathlib.Path("reports/credential-runtime-evidence-policy.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-receipt.v1.schema.json")


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def as_utc_timestamp(value: str | None) -> str:
    if value:
        return value
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_policy(policy: dict[str, Any], source_id: str) -> dict[str, Any]:
    for raw_source in receipts.as_list(policy.get("sources"), "policy.sources"):
        source = receipts.as_dict(raw_source, "policy.sources[]")
        if source.get("source_id") == source_id:
            return source
    raise ValueError(f"receipt source {source_id} is not present in credential runtime policy")


def reviewed_receipt_path(source: dict[str, Any]) -> pathlib.Path:
    bounded = receipts.as_dict(source.get("bounded_live_evidence_path"), "source.bounded_live_evidence_path")
    path = bounded.get("reviewed_receipt_artifact")
    if not isinstance(path, str) or not path:
        raise ValueError("source.bounded_live_evidence_path.reviewed_receipt_artifact must be a non-empty string")
    return pathlib.Path(path)


def staged_receipt_path(source: dict[str, Any]) -> pathlib.Path:
    bounded = receipts.as_dict(source.get("bounded_live_evidence_path"), "source.bounded_live_evidence_path")
    path = bounded.get("receipt_artifact")
    if not isinstance(path, str) or not path:
        raise ValueError("source.bounded_live_evidence_path.receipt_artifact must be a non-empty string")
    return pathlib.Path(path)


def validate_review_inputs(receipt: dict[str, Any], state: str, decision: str) -> None:
    if state not in receipts.REVIEW_STATES:
        raise ValueError(f"--state must be one of {sorted(receipts.REVIEW_STATES)}")
    if state == "reviewed_rejected" and decision != "keeps_manual_review_boundary":
        raise ValueError("reviewed_rejected receipts must use --decision keeps_manual_review_boundary")
    if state == "reviewed_accepted" and decision != "allows_manual_review_reduction":
        raise ValueError("reviewed_accepted receipts must use --decision allows_manual_review_reduction")
    if state == "reviewed_accepted" and receipt.get("outcome") != "verified":
        raise ValueError("reviewed_accepted receipts must have outcome=verified")
    if state == "reviewed_accepted" and receipt.get("error_class") != "none":
        raise ValueError("reviewed_accepted receipts must have error_class=none")


def promoted_receipt(
    staged: dict[str, Any],
    *,
    state: str,
    decision: str,
    reviewer: str,
    reason: str,
    reviewed_at: str,
) -> dict[str, Any]:
    if "review" in staged:
        raise ValueError("staged receipt already contains review metadata; use an unreviewed staged receipt")
    validate_review_inputs(staged, state, decision)
    promoted = copy.deepcopy(staged)
    promoted["review"] = {
        "state": state,
        "reviewed_at": reviewed_at,
        "reviewer": reviewer,
        "decision": decision,
        "reason": reason,
    }
    return promoted


def promote(args: argparse.Namespace) -> tuple[pathlib.Path, dict[str, Any]]:
    schema = receipts.load_json(args.schema)
    policy = receipts.load_json(args.policy)
    staged = receipts.load_json(args.staged)
    source_id = staged.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("staged receipt source_id must be a non-empty string")
    source = source_policy(policy, source_id)
    expected_staged = staged_receipt_path(source)
    if args.staged != expected_staged:
        raise ValueError(f"staged receipt path expected {expected_staged}, got {args.staged}")
    expected_output = args.output or reviewed_receipt_path(source)
    if expected_output != reviewed_receipt_path(source):
        raise ValueError(f"reviewed receipt output must be {reviewed_receipt_path(source)}")
    if expected_output.exists() and not args.force:
        raise ValueError(f"reviewed receipt already exists: {expected_output}; pass --force to overwrite")

    sources_by_id = receipts.source_lookup([receipts.as_dict(item, "policy.sources[]") for item in receipts.as_list(policy.get("sources"), "policy.sources")])
    receipts.validate_receipt(staged, schema, sources_by_id, args.staged.as_posix(), require_review=False)
    reviewed = promoted_receipt(
        staged,
        state=args.state,
        decision=args.decision,
        reviewer=args.reviewer,
        reason=args.reason,
        reviewed_at=as_utc_timestamp(args.reviewed_at),
    )
    receipts.validate_receipt(reviewed, schema, sources_by_id, expected_output.as_posix(), require_review=True)
    return expected_output, reviewed


def sample_staged_receipt(source: dict[str, Any], *, outcome: str, error_class: str) -> dict[str, Any]:
    return {
        "schema_version": receipts.SCHEMA_VERSION,
        "generated_at": "2026-07-07T00:00:00Z",
        "source_id": source["source_id"],
        "provider": source["provider"],
        "candidate_batch": source["candidate_batch"],
        "runtime_evidence_plan": source["runtime_evidence_plan"],
        "credential_configured": True,
        "credential_envs": source["credential_envs"],
        "bounded": True,
        "execution": {
            "started_at": "2026-07-07T00:00:00Z",
            "finished_at": "2026-07-07T00:00:01Z",
            "duration_ms": 1000,
            "request_count": 1,
        },
        "outcome": outcome,
        "error_class": error_class,
        "redaction": {
            "secret_values_present": False,
            "secret_hashes_present": False,
            "forbidden_fields_checked": sorted(receipts.FORBIDDEN_KEYS),
        },
    }


def run_self_tests(schema: dict[str, Any], policy: dict[str, Any]) -> None:
    source = receipts.as_dict(receipts.as_list(policy.get("sources"), "policy.sources")[0], "policy.sources[0]")
    sources_by_id = receipts.source_lookup(
        [receipts.as_dict(item, "policy.sources[]") for item in receipts.as_list(policy.get("sources"), "policy.sources")]
    )

    rejected_staged = sample_staged_receipt(source, outcome="manual_review_required", error_class="credential")
    receipts.validate_receipt(rejected_staged, schema, sources_by_id, "<self-test-staged-rejected>", require_review=False)
    rejected = promoted_receipt(
        rejected_staged,
        state="reviewed_rejected",
        decision="keeps_manual_review_boundary",
        reviewer="self-test",
        reason="self-test rejected receipt keeps manual-review boundary",
        reviewed_at="2026-07-07T00:00:02Z",
    )
    receipts.validate_receipt(rejected, schema, sources_by_id, "<self-test-reviewed-rejected>", require_review=True)

    accepted_staged = sample_staged_receipt(source, outcome="verified", error_class="none")
    accepted = promoted_receipt(
        accepted_staged,
        state="reviewed_accepted",
        decision="allows_manual_review_reduction",
        reviewer="self-test",
        reason="self-test accepted receipt is source-level relief eligible",
        reviewed_at="2026-07-07T00:00:02Z",
    )
    receipts.validate_receipt(accepted, schema, sources_by_id, "<self-test-reviewed-accepted>", require_review=True)
    if not receipts.receipt_is_relief_eligible(accepted):
        raise ValueError("self-test failed: accepted receipt was not relief eligible")

    wrong_decision = sample_staged_receipt(source, outcome="verified", error_class="none")
    try:
        promoted_receipt(
            wrong_decision,
            state="reviewed_accepted",
            decision="keeps_manual_review_boundary",
            reviewer="self-test",
            reason="self-test wrong decision",
            reviewed_at="2026-07-07T00:00:02Z",
        )
    except ValueError:
        pass
    else:
        raise ValueError("self-test failed: accepted receipt allowed wrong decision")

    forbidden = sample_staged_receipt(source, outcome="verified", error_class="none")
    forbidden["credential_value"] = "abc123"
    try:
        receipts.validate_receipt(forbidden, schema, sources_by_id, "<self-test-forbidden>", require_review=False)
    except ValueError:
        pass
    else:
        raise ValueError("self-test failed: forbidden credential_value was accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("staged", nargs="?", type=pathlib.Path, help="local .datapan staged credential receipt path")
    parser.add_argument("--policy", default=DEFAULT_POLICY, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, help="reviewed receipt output path; defaults to policy path")
    parser.add_argument("--state", choices=sorted(receipts.REVIEW_STATES))
    parser.add_argument(
        "--decision",
        choices=["allows_manual_review_reduction", "keeps_manual_review_boundary"],
    )
    parser.add_argument("--reviewer")
    parser.add_argument("--reason")
    parser.add_argument("--reviewed-at", help="review timestamp; defaults to current UTC time")
    parser.add_argument("--force", action="store_true", help="overwrite an existing reviewed receipt")
    parser.add_argument("--check", action="store_true", help="validate and print the promoted receipt without writing")
    parser.add_argument("--self-test", action="store_true", help="run secret-free promotion self-tests")
    args = parser.parse_args()

    try:
        if args.self_test:
            run_self_tests(receipts.load_json(args.schema), receipts.load_json(args.policy))
            print("ok credential receipt promotion self-tests")
            return 0
        if args.staged is None:
            raise ValueError("staged receipt path is required unless --self-test is used")
        for name in ("state", "decision", "reviewer", "reason"):
            if not getattr(args, name):
                raise ValueError(f"--{name.replace('_', '-')} is required")
        output, reviewed = promote(args)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL promote credential runtime receipt: {exc}", file=sys.stderr)
        return 1

    if args.check:
        print(render_json(reviewed), end="")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_json(reviewed), encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
