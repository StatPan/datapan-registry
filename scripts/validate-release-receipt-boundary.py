#!/usr/bin/env python3
"""Validate release verification/readiness receipt generation boundaries."""

from __future__ import annotations

import argparse
import json
import pathlib
import runpy
import sys
from typing import Any


DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_REPORT_VALIDATOR = pathlib.Path("scripts/validate-release-report-artifacts.py")
DEFAULT_RELEASE_CADENCE = pathlib.Path("docs/release-cadence.md")
DEFAULT_LEDGER_OWNERSHIP = pathlib.Path("docs/release-ledger-ownership.json")
DEFAULT_VERIFY_WORKFLOW = pathlib.Path(".github/workflows/verify-release.yml")
DEFAULT_DRAFT_WORKFLOW = pathlib.Path(".github/workflows/release-draft.yml")

RECEIPTS = {
    "reports/latest-release-verification.json": {
        "command": (
            "datapan catalog release verify --manifest manifest.json "
            "--output reports/latest-release-verification.json --json"
        ),
        "ci_manifest": "--manifest ../datapan-registry/manifest.json",
        "ci_output": "--output ../datapan-registry/.datapan/ci/latest-release-verification.json",
        "draft_manifest": '--manifest "${DATAPAN_RELEASE_MANIFEST}"',
        "draft_output": "--output ../datapan-registry/.datapan/ci/latest-release-verification.json",
    },
    "reports/latest-release-readiness.json": {
        "command": (
            "datapan catalog release readiness --manifest manifest.json "
            "--output reports/latest-release-readiness.json --json"
        ),
        "ci_manifest": "--manifest ../datapan-registry/manifest.json",
        "ci_output": "--output ../datapan-registry/.datapan/ci/latest-release-readiness.json",
        "draft_manifest": '--manifest "${DATAPAN_RELEASE_MANIFEST}"',
        "draft_output": "--output ../datapan-registry/.datapan/ci/latest-release-readiness.json",
    },
}


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, label: str | pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def manifest_paths(manifest_path: pathlib.Path) -> set[str]:
    manifest = as_dict(load_json(manifest_path), manifest_path)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be an array")

    paths: set[str] = set()
    for index, artifact in enumerate(artifacts):
        item = as_dict(artifact, f"manifest.artifacts[{index}]")
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"manifest.artifacts[{index}].path must be a non-empty string")
        paths.add(path)
    return paths


def validate_manifest_exemption(manifest_path: pathlib.Path) -> None:
    paths = manifest_paths(manifest_path)
    unexpected = sorted(path for path in RECEIPTS if path in paths)
    if unexpected:
        raise ValueError(
            "release verification/readiness receipts must be manifest-derived, "
            f"not manifest-bound artifacts: {', '.join(unexpected)}"
        )


def validate_checked_in_receipts() -> None:
    failures: list[str] = []
    for receipt_path in RECEIPTS:
        path = pathlib.Path(receipt_path)
        try:
            payload = as_dict(load_json(path), path)
        except FileNotFoundError:
            failures.append(f"{receipt_path}: checked-in receipt is missing")
            continue
        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, str) or not schema_version:
            failures.append(f"{receipt_path}: receipt must carry a schema_version")
    if failures:
        raise ValueError("; ".join(failures))


def validate_report_validator(report_validator_path: pathlib.Path) -> None:
    namespace = runpy.run_path(report_validator_path)
    exempt_reports = namespace.get("EXEMPT_REPORTS")
    if not isinstance(exempt_reports, dict):
        raise ValueError(f"{report_validator_path}: EXEMPT_REPORTS must be a dictionary")

    failures: list[str] = []
    for receipt_path in RECEIPTS:
        reason = exempt_reports.get(receipt_path)
        if not isinstance(reason, str) or not reason:
            failures.append(f"{receipt_path}: missing EXEMPT_REPORTS reason")
            continue
        lowered = reason.lower()
        if "manifest" not in lowered or "generated" not in lowered:
            failures.append(
                f"{receipt_path}: EXEMPT_REPORTS reason must say it is generated from the manifest"
            )
    if failures:
        raise ValueError("; ".join(failures))


def require_snippets(path: pathlib.Path, snippets: list[str]) -> None:
    text = read_text(path)
    missing = [snippet for snippet in snippets if snippet not in text]
    if missing:
        raise ValueError(f"{path}: missing required snippet(s): {missing}")


def validate_release_docs(release_cadence_path: pathlib.Path, ledger_ownership_path: pathlib.Path) -> None:
    cadence_snippets = [
        item["command"]
        for item in RECEIPTS.values()
    ] + [
        "manifest-derived receipts",
        "not listed in `manifest.json`",
        "python scripts/validate-release-receipt-boundary.py",
    ]
    require_snippets(release_cadence_path, cadence_snippets)

    ownership = as_dict(load_json(ledger_ownership_path), ledger_ownership_path)
    entries = ownership.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"{ledger_ownership_path}: entries must be an array")

    verification_entry = None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == "verification_receipts":
            verification_entry = entry
            break
    if verification_entry is None:
        raise ValueError(f"{ledger_ownership_path}: missing verification_receipts artifact group")

    checked_by = verification_entry.get("checked_by")
    if not isinstance(checked_by, list):
        raise ValueError(f"{ledger_ownership_path}: verification_receipts.checked_by must be an array")
    if "python scripts/validate-release-receipt-boundary.py" not in checked_by:
        raise ValueError(
            f"{ledger_ownership_path}: verification_receipts.checked_by must include "
            "python scripts/validate-release-receipt-boundary.py"
        )

    boundary = verification_entry.get("exemption_boundary")
    if not isinstance(boundary, str) or "manifest-derived" not in boundary:
        raise ValueError(
            f"{ledger_ownership_path}: verification_receipts.exemption_boundary must name "
            "manifest-derived receipts"
        )


def validate_workflows(verify_workflow_path: pathlib.Path, draft_workflow_path: pathlib.Path) -> None:
    verify_snippets = [
        "python scripts/validate-release-receipt-boundary.py",
        "python -m py_compile scripts/validate-release-receipt-boundary.py",
    ]
    draft_snippets = [
        "scripts/validate-release-receipt-boundary.py",
        "python scripts/validate-release-receipt-boundary.py",
    ]
    for receipt in RECEIPTS.values():
        verify_snippets.extend([receipt["ci_manifest"], receipt["ci_output"]])
        draft_snippets.extend([receipt["draft_manifest"], receipt["draft_output"]])

    require_snippets(verify_workflow_path, verify_snippets)
    require_snippets(draft_workflow_path, draft_snippets)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    parser.add_argument("--report-validator", default=DEFAULT_REPORT_VALIDATOR, type=pathlib.Path)
    parser.add_argument("--release-cadence", default=DEFAULT_RELEASE_CADENCE, type=pathlib.Path)
    parser.add_argument("--ledger-ownership", default=DEFAULT_LEDGER_OWNERSHIP, type=pathlib.Path)
    parser.add_argument("--verify-workflow", default=DEFAULT_VERIFY_WORKFLOW, type=pathlib.Path)
    parser.add_argument("--draft-workflow", default=DEFAULT_DRAFT_WORKFLOW, type=pathlib.Path)
    args = parser.parse_args()

    try:
        validate_manifest_exemption(args.manifest)
        validate_checked_in_receipts()
        validate_report_validator(args.report_validator)
        validate_release_docs(args.release_cadence, args.ledger_ownership)
        validate_workflows(args.verify_workflow, args.draft_workflow)
    except Exception as exc:  # noqa: BLE001 - print a single operator-facing blocker
        print(f"FAIL release receipt boundary: {exc}", file=sys.stderr)
        return 1

    print("ok release receipt boundary (manifest_derived_receipts=2)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
