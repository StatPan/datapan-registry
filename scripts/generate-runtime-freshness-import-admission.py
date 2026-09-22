#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any

import jsonschema


SCHEMA_VERSION = "datapan.runtime-freshness-import-admission.v1"
COUNT_KEYS = ("total", "verified", "failed", "skipped", "unknown")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative_path(root: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"{path} must be inside repository root {root}") from exc


def validate_counts(value: object, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(COUNT_KEYS):
        raise ValueError(f"{label} must contain exactly {', '.join(COUNT_KEYS)}")
    if any(not isinstance(value[key], int) or value[key] < 0 for key in COUNT_KEYS):
        raise ValueError(f"{label} counts must be non-negative integers")
    if value["total"] != sum(value[key] for key in COUNT_KEYS[1:]):
        raise ValueError(f"{label} status counts do not sum to total")
    return {key: value[key] for key in COUNT_KEYS}


def build(
    *,
    root: pathlib.Path,
    proposal_path: pathlib.Path,
    report_path: pathlib.Path,
    receipt_path: pathlib.Path,
    import_receipt_path: pathlib.Path,
    producer_repository: str,
    producer_revision: str,
    producer_run_id: str,
    producer_run_url: str,
) -> dict[str, Any]:
    proposal = load(proposal_path)
    receipt = load(receipt_path)
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run receipt run_id is missing")
    if proposal.get("run_id") != run_id or producer_run_id != run_id:
        raise ValueError("proposal, receipt, and producer run ids do not match")
    if not re.fullmatch(r"[^/]+/[^/]+", producer_repository):
        raise ValueError("producer repository must be owner/name")
    if not REVISION_RE.fullmatch(producer_revision):
        raise ValueError("producer revision must be a lowercase 40-character SHA")
    expected_run_url = f"https://github.com/{producer_repository}/actions/runs/{run_id}"
    if producer_run_url != expected_run_url:
        raise ValueError("producer run URL does not match repository and run id")

    report_sha = sha256(report_path)
    receipt_sha = sha256(receipt_path)
    if proposal.get("sanitized_report_sha256") != report_sha:
        raise ValueError("proposal sanitized report digest does not match artifact")
    if proposal.get("receipt_sha256") != receipt_sha:
        raise ValueError("proposal run receipt digest does not match artifact")

    before = validate_counts(proposal.get("before"), "before")
    selected = validate_counts(proposal.get("selected"), "selected")
    after = validate_counts(proposal.get("after"), "after")
    delta = proposal.get("delta")
    if not isinstance(delta, dict) or set(delta) != set(COUNT_KEYS):
        raise ValueError("delta must contain the exact count fields")
    if any(delta[key] != selected[key] for key in COUNT_KEYS):
        raise ValueError("delta must exactly equal selected status counts")
    if any(after[key] != before[key] + selected[key] for key in COUNT_KEYS):
        raise ValueError("after counts must exactly equal before plus selected")
    if proposal.get("selected_new_results") != selected["total"]:
        raise ValueError("selected_new_results does not equal selected total")

    selected_identity_set = proposal.get("selected_identity_set")
    if not isinstance(selected_identity_set, dict):
        raise ValueError("selected identity set is missing")
    if selected_identity_set.get("count") != selected["total"]:
        raise ValueError("selected identity count does not equal selected total")
    if not SHA256_RE.fullmatch(str(selected_identity_set.get("sha256", ""))):
        raise ValueError("selected identity digest is invalid")

    equality = receipt.get("identity_equality")
    if not isinstance(equality, dict) or equality.get("equal") is not True:
        raise ValueError("run receipt does not prove exact identity equality")
    planned = equality.get("planned")
    if not isinstance(planned, dict) or not isinstance(planned.get("count"), int):
        raise ValueError("planned identity set is missing")
    if not SHA256_RE.fullmatch(str(planned.get("sha256", ""))):
        raise ValueError("planned identity digest is invalid")

    admitted_at = receipt.get("generated_at")
    if not isinstance(admitted_at, str) or not admitted_at.endswith("Z"):
        raise ValueError("run receipt generated_at is missing or invalid")
    outcome = "imported" if selected["total"] else "no_change"
    return {
        "schema_version": SCHEMA_VERSION,
        "admitted_at": admitted_at,
        "run_id": run_id,
        "producer": {
            "repository": producer_repository,
            "revision": producer_revision,
            "run_id": run_id,
            "run_url": producer_run_url,
        },
        "inputs": {
            "sanitized_report_sha256": report_sha,
            "run_receipt_sha256": receipt_sha,
            "import_receipt": {
                "path": relative_path(root, import_receipt_path),
                "sha256": sha256(import_receipt_path),
            },
            "planned_identity_set": {
                "count": planned["count"],
                "sha256": planned["sha256"],
            },
        },
        "outcome": outcome,
        "arithmetic": {
            "before": before,
            "selected": selected,
            "after": after,
            "selected_new_results": selected["total"],
        },
        "selected_identity_set": {
            "count": selected_identity_set["count"],
            "sha256": selected_identity_set["sha256"],
        },
    }


def render(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def write_or_check(output: pathlib.Path, content: bytes, *, check: bool) -> None:
    if output.exists():
        if output.read_bytes() != content:
            raise ValueError("run id already has different admission bytes")
        return
    if check:
        raise ValueError(f"missing admission {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--proposal", required=True, type=pathlib.Path)
    parser.add_argument("--report", required=True, type=pathlib.Path)
    parser.add_argument("--receipt", required=True, type=pathlib.Path)
    parser.add_argument("--import-receipt", required=True, type=pathlib.Path)
    parser.add_argument("--producer-repository", required=True)
    parser.add_argument("--producer-revision", required=True)
    parser.add_argument("--producer-run-id", required=True)
    parser.add_argument("--producer-run-url", required=True)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--schema", type=pathlib.Path, default=pathlib.Path("schemas/datapan.runtime-freshness-import-admission.v1.schema.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        value = build(
            root=args.root,
            proposal_path=args.proposal,
            report_path=args.report,
            receipt_path=args.receipt,
            import_receipt_path=args.import_receipt,
            producer_repository=args.producer_repository,
            producer_revision=args.producer_revision,
            producer_run_id=args.producer_run_id,
            producer_run_url=args.producer_run_url,
        )
        jsonschema.Draft202012Validator(load(args.schema)).validate(value)
        write_or_check(args.output, render(value), check=args.check)
        print(json.dumps({"status": "valid" if args.check else "written", "run_id": value["run_id"], "outcome": value["outcome"], "output": args.output.as_posix()}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL runtime freshness import admission: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
