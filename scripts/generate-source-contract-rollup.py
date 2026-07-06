#!/usr/bin/env python3
"""Generate a release-wide Datapan source contract rollup."""

from __future__ import annotations

import argparse
import pathlib
import sys

from source_contract_rollup import build_rollup, stable_json_bytes, write_json


DEFAULT_OUTPUT = pathlib.Path("reports/source-contract-rollup.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="manifest.json", type=pathlib.Path)
    parser.add_argument("--source-glob", default="sources/*.json")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate that the checked-in rollup matches generated output",
    )
    args = parser.parse_args()

    try:
        rollup = build_rollup(manifest_path=args.manifest, source_glob=args.source_glob)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate source contract rollup: {exc}", file=sys.stderr)
        return 1

    rendered = stable_json_bytes(rollup)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing generated source contract rollup", file=sys.stderr)
            return 1
        current = args.output.read_bytes()
        if current != rendered:
            print(
                f"FAIL {args.output}: stale source contract rollup; "
                "run `python3 scripts/generate-source-contract-rollup.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (profiles={rollup['summary']['profiles']}, providers={rollup['summary']['providers']})")
        return 0

    write_json(args.output, rollup)
    print(f"wrote {args.output} (profiles={rollup['summary']['profiles']}, providers={rollup['summary']['providers']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
