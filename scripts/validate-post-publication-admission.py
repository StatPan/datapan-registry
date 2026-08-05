#!/usr/bin/env python3
"""Validate redacted post-publication Registry and CLI receipt evidence offline."""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

import post_publication_admission as admission


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=pathlib.Path)
    parser.add_argument("--schema", default="schemas/datapan.post-publication-admission.v1.schema.json", type=pathlib.Path)
    parser.add_argument("--evidence-root", type=pathlib.Path, help="root containing separately supplied rollback anonymous-verification evidence")
    parser.add_argument("--admission-time", default=datetime.now(timezone.utc).isoformat(), help="caller-owned RFC3339 admission time; defaults to current UTC")
    args = parser.parse_args()
    try:
        result = admission.validate_admission(
            admission.load_json(args.receipt),
            schema=admission.load_json(args.schema),
            admitted_at=admission.parse_time(args.admission_time, "--admission-time"),
            evidence_root=args.evidence_root,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL post-publication admission: {exc}", file=sys.stderr)
        return 1
    print(f"ok post-publication admission (status={result})")
    # A manual hold is valid evidence, but deliberately not a successful
    # consumer gate.  Calling automation must stop rather than mistake it for
    # a publication unlock.
    return 2 if result == "manual_hold" else 0


if __name__ == "__main__":
    raise SystemExit(main())
