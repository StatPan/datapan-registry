#!/usr/bin/env python3
"""Refresh the versioned operation-count expectation for one exact registry snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import tempfile
import sys
from typing import Any

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = pathlib.Path("data/data-go-kr.registry.json")
MANIFEST = pathlib.Path("reports/data-go-kr/operation-manifest.json")
POLICY = pathlib.Path("policy/data-go-kr-operation-denominator-expectation.json")
SCHEMA = pathlib.Path("schemas/datapan.data-go-kr-operation-denominator-expectation.v1.schema.json")


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build(registry_path: pathlib.Path, manifest: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    source = registry_path.read_bytes()
    source_digest = hashlib.sha256(source).hexdigest()
    snapshot = manifest.get("source_snapshot")
    if snapshot != {"path": REGISTRY.as_posix(), "bytes": len(source), "sha256": source_digest}:
        raise ValueError("operation manifest is not bound to the exact registry snapshot")
    summary = manifest.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("generated operation manifest has no summary")
    protocols = summary.get("protocols")
    eligibility = summary.get("eligibility")
    exclusions = summary.get("exclusions")
    if not isinstance(protocols, dict) or set(protocols) != {"REST", "SOAP"}:
        raise ValueError("operation manifest protocol denominator is incomplete")
    if not isinstance(eligibility, dict) or not eligibility or not isinstance(exclusions, dict):
        raise ValueError("operation manifest eligibility or exclusion denominator is incomplete")
    count_fields = [summary.get("api_operations"), *protocols.values(), *eligibility.values(), *exclusions.values(), summary.get("identity_collisions"), summary.get("identity_omissions")]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in count_fields):
        raise ValueError("operation manifest contains an invalid count")
    if summary["api_operations"] < 1 or sum(protocols.values()) != summary["api_operations"]:
        raise ValueError("API denominator does not equal the REST and SOAP partition")
    if sum(eligibility.values()) != summary["api_operations"]:
        raise ValueError("eligibility counts do not cover the exact API-operation denominator")
    if summary["identity_collisions"] != 0 or summary["identity_omissions"] != 0:
        raise ValueError("identity collisions or omissions block denominator expectation refresh")
    expected_keys = {"link_operations", "operationless_catalog_entries", "filedata_catalog_entries"}
    if set(exclusions) != expected_keys:
        raise ValueError("operation exclusion denominator is incomplete")
    return {
        "schema_version": current["schema_version"],
        "source_snapshot": {"path": REGISTRY.as_posix(), "bytes": len(source), "sha256": source_digest},
        "expected_summary": summary,
    }


def render(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=pathlib.Path, default=ROOT / REGISTRY)
    parser.add_argument("--manifest", type=pathlib.Path, default=ROOT / MANIFEST)
    parser.add_argument("--policy", type=pathlib.Path, default=ROOT / POLICY)
    parser.add_argument("--schema", type=pathlib.Path, default=ROOT / SCHEMA)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write the exact generated snapshot expectation")
    mode.add_argument("--check", action="store_true", help="require the versioned expectation to match the exact generated snapshot")
    args = parser.parse_args()
    try:
        policy, schema = load(args.policy), load(args.schema)
        updated = build(args.registry, load(args.manifest), policy)
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(updated)
        expected = render(updated)
        if args.check:
            if args.policy.read_bytes() != expected:
                raise ValueError("operation denominator expectation is stale for this registry snapshot")
        else:
            args.policy.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=args.policy.parent, prefix=f".{args.policy.name}.", delete=False) as handle:
                temporary = pathlib.Path(handle.name)
                handle.write(expected)
            try:
                temporary.replace(args.policy)
            finally:
                temporary.unlink(missing_ok=True)
        print(json.dumps({"status": "checked" if args.check else "wrote", "source_snapshot": updated["source_snapshot"], "expected_summary": updated["expected_summary"]}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL data.go.kr denominator expectation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
