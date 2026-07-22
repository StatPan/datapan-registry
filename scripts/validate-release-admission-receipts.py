#!/usr/bin/env python3
"""Validate Registry-native immutable receipt admission without consumer execution."""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

import release_admission_receipts as admission


def producer_artifact_roots(values: list[str]) -> dict[str, pathlib.Path]:
    roots: dict[str, pathlib.Path] = {}
    for value in values:
        repository, separator, raw_path = value.partition("=")
        if not separator or not repository or not raw_path or repository in roots:
            raise ValueError("--producer-artifact-root must be unique StatPan/repository=path")
        roots[repository] = pathlib.Path(raw_path)
    return roots


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", nargs="+", type=pathlib.Path)
    parser.add_argument("--schema", default="schemas/datapan.release-receipt-admission.v1.schema.json", type=pathlib.Path)
    parser.add_argument("--policy", default="policy/release-receipt-admission.json", type=pathlib.Path)
    parser.add_argument("--manifest", default="manifest.json", type=pathlib.Path)
    parser.add_argument("--check-manifest-artifacts", action="store_true")
    parser.add_argument("--require-runtime-completeness", action="store_true")
    parser.add_argument("--producer-artifact-root", action="append", default=[], metavar="REPOSITORY=PATH")
    parser.add_argument("--admission-time", default=datetime.now(timezone.utc).isoformat(), help="RFC3339 admission time; defaults to current UTC time")
    args = parser.parse_args()
    try:
        schema = admission.load_json(args.schema)
        policy = admission.load_json(args.policy)
        manifest, manifest_sha256, source_sha256 = admission.validate_manifest(args.manifest, check_artifacts=args.check_manifest_artifacts)
        del manifest
        admitted_at = admission.parse_time(args.admission_time, "--admission-time")
        artifact_roots = producer_artifact_roots(args.producer_artifact_root)
        receipts = [admission.load_json(path) for path in args.receipts]
        for path, receipt in zip(args.receipts, receipts, strict=True):
            admission.validate_receipt(receipt, schema=schema, policy=policy, policy_path=args.policy, manifest_path=args.manifest, manifest_sha256=manifest_sha256, source_sha256=source_sha256, artifact_roots=artifact_roots, admitted_at=admitted_at, label=path.as_posix())
        run_id = admission.validate_runtime_completeness(receipts) if args.require_runtime_completeness else "not_required"
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL release receipt admission: {exc}", file=sys.stderr)
        return 1
    print(f"ok release receipt admission (receipts={len(receipts)}, runtime_run_id={run_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
