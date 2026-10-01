#!/usr/bin/env python3
"""Generate the deterministic current-source diagnostic applicability receipt."""

from __future__ import annotations

import argparse
import sys

import diagnostic_source_applicability as applicability


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write the deterministic applicability receipt")
    mode.add_argument("--check", action="store_true", help="check the checked-in receipt against exact current inputs")
    args = parser.parse_args()
    try:
        report = applicability.build_report()
        applicability.validate_schema(report)
        expected = applicability.render_report(report)
        if args.check:
            if not applicability.REPORT.is_file() or applicability.REPORT.read_bytes() != expected:
                raise ValueError("generated current-source applicability receipt drift")
            print("ok current-source applicability receipt")
            return 0
        applicability.REPORT.parent.mkdir(parents=True, exist_ok=True)
        applicability.REPORT.write_bytes(expected)
    except Exception as exc:  # noqa: BLE001 - surface exact source-binding failures in CI
        print(f"FAIL current-source applicability receipt: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {applicability.REPORT.relative_to(applicability.ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
