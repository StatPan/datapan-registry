#!/usr/bin/env python3
"""Generate the minimized Registry identity proof used by diagnostic CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
MAPPING_PATH = ROOT / "drafts/diagnostic-envelope/data-go-kr-evidence-mapping.v1.json"
MANIFEST_PATH = ROOT / "manifest.json"
REGISTRY_PATH = ROOT / "data/data-go-kr.registry.json"
OUTPUT_PATH = ROOT / "drafts/diagnostic-envelope/data-go-kr-registry-identity-proof.v1.json"


def load(path: pathlib.Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def build() -> dict[str, Any]:
    mapping = load(MAPPING_PATH)
    manifest = load(MANIFEST_PATH)
    registry_bytes = REGISTRY_PATH.read_bytes()
    registry_sha256 = hashlib.sha256(registry_bytes).hexdigest()
    registry_artifact = next(item for item in manifest["artifacts"] if item["path"] == "data/data-go-kr.registry.json")
    mapping_artifact = next(item for item in mapping["authoritative_inputs"] if item["path"] == "data/data-go-kr.registry.json")
    if len(registry_bytes) != registry_artifact["bytes"] or registry_sha256 != registry_artifact["sha256"] or registry_sha256 != mapping_artifact["sha256"]:
        raise ValueError("canonical Registry identity does not match manifest and mapping")

    referenced_dataset_ids = sorted(
        {
            case.get("subject_overrides", {}).get("dataset_id")
            for case in mapping["proof_cases"]
            if str(case.get("subject_overrides", {}).get("dataset_id", "")).isdigit()
        }
    )
    registry = load(REGISTRY_PATH)
    datasets_by_id = {item["id"]: item for item in registry}
    datasets = []
    for dataset_id in referenced_dataset_ids:
        dataset = datasets_by_id.get(dataset_id)
        if dataset is None:
            raise ValueError(f"mapping dataset identity is absent: {dataset_id}")
        operations = []
        for operation in dataset.get("operations", []):
            source = operation.get("source", {})
            if source.get("system") != "data.go.kr":
                continue
            operations.append(
                {
                    "operation_name": operation["name"],
                    "source_operation_seq": source.get("raw", {}).get("operation_seq"),
                    "source_system": source["system"],
                    "source_url": source["url"],
                    "source_sha256": digest(source),
                }
            )
        operations.sort(key=lambda item: (item["source_operation_seq"], item["operation_name"]))
        if not operations:
            raise ValueError(f"mapping dataset has no data.go.kr operation: {dataset_id}")
        datasets.append({"dataset_id": dataset_id, "dataset_sha256": digest(dataset), "operations": operations})

    return {
        "schema_version": "datapan.diagnostic-registry-identity-proof.v1",
        "source_registry": {
            "path": "data/data-go-kr.registry.json",
            "bytes": len(registry_bytes),
            "sha256": registry_sha256,
        },
        "identity_contract": mapping["operation_application_path_contract"]["registry_identity"],
        "datasets": datasets,
    }


def render(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        expected = render(build())
        if args.check:
            if not OUTPUT_PATH.is_file() or OUTPUT_PATH.read_bytes() != expected:
                raise ValueError(f"generated proof drift: {OUTPUT_PATH.relative_to(ROOT)}")
        else:
            OUTPUT_PATH.write_bytes(expected)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL diagnostic Registry identity proof: {exc}", file=sys.stderr)
        return 1
    print("ok diagnostic Registry identity proof")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
