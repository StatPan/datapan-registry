#!/usr/bin/env python3
"""Project immutable runtime rows onto exact current operation contracts."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import jsonschema

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from runtime_evidence_projection import build_projection, file_sha256, immutable_import_binding_index


REGISTRY = pathlib.Path("data/data-go-kr.registry.json")
MANIFEST = pathlib.Path("reports/data-go-kr/operation-manifest.json")
LATEST = pathlib.Path("reports/latest-verification.json")
POLICY = pathlib.Path("policy/sustainable-coverage.json")
SCHEMA = pathlib.Path("schemas/datapan.current-runtime-evidence-projection.v1.schema.json")
OUTPUT = pathlib.Path("reports/current-runtime-evidence-projection.json")


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build(
    *,
    registry_path: pathlib.Path = REGISTRY,
    manifest_path: pathlib.Path = MANIFEST,
    latest_path: pathlib.Path = LATEST,
    policy_path: pathlib.Path = POLICY,
    root: pathlib.Path = pathlib.Path("."),
    registry_label: str = "data/data-go-kr.registry.json",
    manifest_label: str = "reports/data-go-kr/operation-manifest.json",
    latest_label: str = "reports/latest-verification.json",
) -> dict[str, Any]:
    registry = load(registry_path)
    operation_manifest = load(manifest_path)
    latest = load(latest_path)
    policy = load(policy_path)
    if not isinstance(registry, list) or not all(isinstance(row, dict) for row in registry):
        raise ValueError("current registry must be an array of objects")
    if not isinstance(operation_manifest, dict) or not isinstance(latest, dict) or not isinstance(policy.get("freshness"), dict):
        raise ValueError("operation manifest, latest verification, or freshness policy is invalid")
    freshness = policy["freshness"]
    provenance_bindings, provenance_summary = immutable_import_binding_index(root)
    projection = build_projection(
        registry,
        latest,
        operation_manifest,
        registry_sha256=file_sha256(registry_path),
        operation_manifest_sha256=file_sha256(manifest_path),
        registry_path=registry_label,
        operation_manifest_path=manifest_label,
        latest_path=latest_label,
        latest_sha256=file_sha256(latest_path),
        provenance_bindings=provenance_bindings,
        provenance_summary=provenance_summary,
        fresh_days=int(freshness["fresh_days"]),
        expire_days=int(freshness["expire_days"]),
    )
    jsonschema.Draft202012Validator(
        load(SCHEMA), format_checker=jsonschema.FormatChecker()
    ).validate(projection)
    return projection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=pathlib.Path, default=REGISTRY)
    parser.add_argument("--operation-manifest", "--manifest", dest="manifest", type=pathlib.Path, default=MANIFEST)
    parser.add_argument("--latest-verification", type=pathlib.Path, default=LATEST)
    parser.add_argument("--policy", type=pathlib.Path, default=POLICY)
    parser.add_argument("--schema", type=pathlib.Path, default=SCHEMA)
    parser.add_argument("--output", type=pathlib.Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        projection = build(
            registry_path=args.registry,
            manifest_path=args.manifest,
            latest_path=args.latest_verification,
            policy_path=args.policy,
            registry_label=REGISTRY.as_posix(),
            manifest_label=MANIFEST.as_posix(),
            latest_label=LATEST.as_posix(),
        )
        jsonschema.Draft202012Validator(
            load(args.schema), format_checker=jsonschema.FormatChecker()
        ).validate(projection)
        rendered = json.dumps(projection, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
                raise ValueError(f"{args.output} is stale")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        summary = projection["summary"]
        print(json.dumps({"status": "checked" if args.check else "wrote", "output": args.output.as_posix(), "eligible": summary["eligible"], "unbound": summary["unbound"], "contract_changed": summary["contract_changed"], "ambiguous": summary["ambiguous"], "expired": summary["expired"]}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL current runtime evidence projection: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
