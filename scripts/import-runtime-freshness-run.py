#!/usr/bin/env python3
"""Safely import one sanitized runtime-freshness run through datapan-cli."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any


FORBIDDEN_KEYS = {
    "url", "request_url", "request_urls", "response_body", "response_bodies",
    "body", "credential_value", "credential_hash", "authorization",
    "authorization_header", "servicekey", "service_key", "apikey", "api_key",
    "secret", "token",
}
SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"authorization:\s*bearer", r"bearer\s+[a-z0-9._~+/=-]{16,}",
        r"servicekey=", r"api[_-]?key=", r"secret=", r"token=",
    )
)


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
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


def counts(report: dict[str, Any]) -> dict[str, int]:
    results = report.get("results")
    if not isinstance(results, list):
        raise ValueError("verification report results must be an array")
    status_counts: collections.Counter[str] = collections.Counter()
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("verification results must contain objects")
        status = result.get("status")
        status_counts[status if status in {"verified", "failed", "skipped"} else "unknown"] += 1
    return {"total": len(results), **{key: status_counts[key] for key in ("verified", "failed", "skipped", "unknown")}}


def validate_receipt(report_path: pathlib.Path, report: dict[str, Any], receipt: dict[str, Any]) -> dict[str, int]:
    scan_boundary(report, "sanitized verification")
    redaction = receipt.get("redaction")
    if not isinstance(redaction, dict) or any(redaction.get(key) is not False for key in (
        "secret_values_present", "secret_hashes_present", "request_urls_present", "response_bodies_present"
    )):
        raise ValueError("receipt does not assert the complete redaction boundary")
    combined = receipt.get("combined_verification")
    if not isinstance(combined, dict):
        raise ValueError("receipt is missing combined_verification")
    content = report_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if combined.get("sha256") != digest or combined.get("bytes") != len(content):
        raise ValueError("sanitized verification digest or byte count does not match receipt")
    observed = counts(report)
    summary = receipt.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("receipt is missing summary")
    expected = {
        "total": summary.get("reported_results"),
        "verified": summary.get("verified"),
        "failed": summary.get("failed"),
        "skipped": summary.get("skipped"),
        "unknown": summary.get("unknown"),
    }
    if observed != expected:
        raise ValueError(f"receipt result counts do not reconcile: expected {expected}, got {observed}")
    return observed


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def import_run(
    *, report_path: pathlib.Path, receipt_path: pathlib.Path, current_path: pathlib.Path,
    summary_path: pathlib.Path, datapan_command: list[str], apply: bool,
) -> dict[str, Any]:
    report, receipt, current = load(report_path), load(receipt_path), load(current_path)
    imported = validate_receipt(report_path, report, receipt)
    before = counts(current)
    current_results = current.get("results")
    incoming_results = report.get("results")
    assert isinstance(current_results, list) and isinstance(incoming_results, list)
    existing = {json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for result in current_results}
    selected_results = [
        result for result in incoming_results
        if json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) not in existing
    ]
    with tempfile.TemporaryDirectory(prefix="datapan-freshness-import-") as directory:
        root = pathlib.Path(directory)
        selected = root / "selected.json"
        merged = root / "verification.json"
        summary = root / "summary.json"
        selected.write_text(json.dumps({**report, "results": selected_results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if selected_results:
            run(datapan_command + ["catalog", "verify", "merge", "--input", str(current_path), "--input", str(selected), "--output", str(merged), "--json"])
            run(datapan_command + ["catalog", "verify", "summary", "--input", str(merged), "--output", str(summary), "--limit", "0", "--json"])
            summary_value = load(summary)
            summary_value["source"] = current_path.as_posix()
            summary.write_text(json.dumps(summary_value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            merged.write_bytes(current_path.read_bytes())
            summary.write_bytes(summary_path.read_bytes())
        after = counts(load(merged))
        proposal = {
            "status": "applied" if apply else "dry_run",
            "run_id": receipt.get("run_id"),
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            "sanitized_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "imported": imported,
            "selected_new_results": len(selected_results),
            "before": before,
            "after": after,
            "delta": {key: after[key] - before[key] for key in before},
        }
        if apply:
            current_path.write_bytes(merged.read_bytes())
            summary_path.write_bytes(summary.read_bytes())
        return proposal


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, type=pathlib.Path)
    parser.add_argument("--receipt", required=True, type=pathlib.Path)
    parser.add_argument("--current", type=pathlib.Path, default=pathlib.Path("reports/latest-verification.json"))
    parser.add_argument("--summary", type=pathlib.Path, default=pathlib.Path("reports/latest-verification-summary.json"))
    parser.add_argument("--datapan-command", default="datapan")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        proposal = import_run(
            report_path=args.report, receipt_path=args.receipt, current_path=args.current,
            summary_path=args.summary, datapan_command=shlex.split(args.datapan_command), apply=args.apply,
        )
        print(json.dumps(proposal, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL import runtime freshness run: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
