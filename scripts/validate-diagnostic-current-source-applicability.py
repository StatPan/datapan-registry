#!/usr/bin/env python3
"""Validate the exact current-source applicability receipt and optional diagnostic publication gate."""

from __future__ import annotations

import argparse
import json
import sys

import diagnostic_source_applicability as applicability


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-current-publication",
        action="store_true",
        help="fail unless explicit current diagnostic source approval and publication authority exist",
    )
    args = parser.parse_args()
    try:
        if not applicability.REPORT.is_file():
            raise ValueError("current-source applicability receipt is missing")
        value = json.loads(applicability.REPORT.read_text(encoding="utf-8"))
        applicability.validate_report(value)
        if args.require_current_publication:
            applicability.validate_current_publication_gate(value)
    except Exception as exc:  # noqa: BLE001 - report the failed source or authority boundary
        print(f"FAIL current-source applicability: {exc}", file=sys.stderr)
        return 1
    print(f"ok current-source applicability (status={value['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
