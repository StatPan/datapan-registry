#!/usr/bin/env python3
"""Generate a datapan-registry release-health rollup artifact."""

from __future__ import annotations

import argparse
import pathlib
import sys

from release_health_rollup import build_rollup, validate_rollup_consistency, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-install", required=True, type=pathlib.Path)
    parser.add_argument("--current-doctor", required=True, type=pathlib.Path)
    parser.add_argument("--latest-install", required=True, type=pathlib.Path)
    parser.add_argument("--latest-doctor", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    try:
        rollup = build_rollup(
            current_install=args.current_install,
            current_doctor=args.current_doctor,
            latest_install=args.latest_install,
            latest_doctor=args.latest_doctor,
        )
        validate_rollup_consistency(rollup)
        write_json(args.output, rollup)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate release health rollup: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {args.output} (checks={rollup['summary']['checks_passed']}/{rollup['summary']['checks_total']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
