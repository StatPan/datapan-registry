#!/usr/bin/env python3
"""Generate or verify the pinned historical Seoul subject snapshot."""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data/data-go-kr.registry.json"


def load_declaration_helper():
    path = pathlib.Path(__file__).with_name("seoul_oa109_operation_declaration.py")
    spec = importlib.util.spec_from_file_location("seoul_oa109_snapshot_generator_helper", path)
    if spec is None or spec.loader is None:
        raise ValueError("trusted Seoul declaration helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify the checked-in artifact and optional source bytes")
    mode.add_argument("--write", action="store_true", help="rebuild from the exact pinned historical source bytes")
    parser.add_argument(
        "--source", type=pathlib.Path,
        help="already available historical 0085 registry bytes; required by --write and optional by --check",
    )
    args = parser.parse_args(argv)
    try:
        helper = load_declaration_helper()
        target = ROOT / helper.HISTORICAL_SUBJECT_SNAPSHOT_PATH
        if args.write:
            source = args.source or DEFAULT_SOURCE
            expected = helper.historical_subject_snapshot_bytes_from_source(source)
            target.write_bytes(expected)
            print(f"wrote {target.relative_to(ROOT)} ({len(expected)} bytes)")
            print(f"sha256 {helper.sha256_bytes(expected)}")
            return 0

        helper.load_historical_subject_snapshot(ROOT)
        if args.source is not None:
            expected = helper.historical_subject_snapshot_bytes_from_source(args.source)
            actual = target.read_bytes()
            if actual != expected:
                raise ValueError("historical subject snapshot does not match the pinned source bytes")
        print(f"ok historical Seoul subject snapshot ({target.stat().st_size} bytes)")
        return 0
    except Exception as exc:  # noqa: BLE001 - deterministic release tooling reports bounded diagnostics
        print(f"FAIL Seoul subject snapshot: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
