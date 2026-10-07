#!/usr/bin/env python3
"""Register digest-bound v2 operation evidence and capture receipts."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import re
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "manifest.json"
EVIDENCE_DIR = ROOT / "reports/operation-document-evidence/v2"
RECEIPT_DIR = EVIDENCE_DIR / "receipts"
EVIDENCE_SCHEMA = "https://schemas.datapan.dev/datapan.operation-document-evidence.v2.schema.json"
RECEIPT_SCHEMA = "https://schemas.datapan.dev/datapan.operation-document-capture-receipt.v2.schema.json"


class RegistrationError(ValueError):
    pass


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    def pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RegistrationError("json_duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs_no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegistrationError("json_input_invalid") from exc
    if not isinstance(value, dict):
        raise RegistrationError("json_object_required")
    return value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _relative_path(path: pathlib.Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RegistrationError("artifact_file_invalid")
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise RegistrationError("artifact_path_outside_repository") from exc


def _expected_artifacts() -> list[dict[str, Any]]:
    evidence_files = sorted(EVIDENCE_DIR.glob("*.json"))
    receipt_files = sorted(RECEIPT_DIR.glob("*.json"))
    if not evidence_files or len(evidence_files) != len(receipt_files):
        raise RegistrationError("evidence_receipt_inventory_mismatch")
    receipt_by_name = {path.name: path for path in receipt_files}
    if len(receipt_by_name) != len(receipt_files):
        raise RegistrationError("capture_receipt_filename_collision")

    expected: list[dict[str, Any]] = []
    for evidence_path in evidence_files:
        if not re.fullmatch(r"[0-9]+-[A-Za-z0-9:._-]+\.json", evidence_path.name):
            raise RegistrationError("evidence_filename_invalid")
        evidence_raw = evidence_path.read_bytes()
        evidence = _load_json(evidence_path)
        identity = evidence.get("identity")
        if (
            evidence.get("schema_version") != "datapan.operation-document-evidence.v2"
            or not isinstance(identity, dict)
            or not isinstance(identity.get("operation_id"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", identity["operation_id"])
            or not isinstance(identity.get("dataset_id"), str)
            or not re.fullmatch(r"[0-9]+", identity["dataset_id"])
            or not isinstance(identity.get("upstream_operation_key"), str)
            or f"{identity['dataset_id']}-{identity['upstream_operation_key']}.json" != evidence_path.name
        ):
            raise RegistrationError("evidence_identity_invalid")

        receipt_path = receipt_by_name.get(evidence_path.name)
        if receipt_path is None:
            raise RegistrationError("capture_receipt_missing")
        receipt = _load_json(receipt_path)
        if (
            receipt.get("schema_version") != "datapan.operation-document-capture-receipt.v2"
            or receipt.get("status") != "acquired"
            or receipt.get("operation_id") != identity["operation_id"]
            or receipt.get("evidence_sha256") != _sha256(evidence_raw)
        ):
            raise RegistrationError("capture_receipt_binding_invalid")

        evidence_rel = _relative_path(evidence_path)
        expected.append({
            "path": evidence_rel,
            "kind": "operation_document_evidence",
            "schema": EVIDENCE_SCHEMA,
            "bytes": len(evidence_raw),
            "sha256": _sha256(evidence_raw),
        })
        receipt_raw = receipt_path.read_bytes()
        expected.append({
            "path": _relative_path(receipt_path),
            "kind": "operation_document_capture_receipt",
            "schema": RECEIPT_SCHEMA,
            "bytes": len(receipt_raw),
            "sha256": _sha256(receipt_raw),
        })
    return expected


def registered_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], int]:
    expected = copy.deepcopy(manifest)
    artifacts = expected.get("artifacts")
    if not isinstance(artifacts, list):
        raise RegistrationError("manifest_artifacts_invalid")
    existing_by_path: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise RegistrationError("manifest_artifact_invalid")
        path = artifact["path"]
        if path in existing_by_path:
            raise RegistrationError("manifest_artifact_path_duplicate")
        existing_by_path[path] = artifact

    additions: list[dict[str, Any]] = []
    for artifact in _expected_artifacts():
        existing = existing_by_path.get(artifact["path"])
        if existing is None:
            additions.append(artifact)
            continue
        if (existing.get("kind"), existing.get("schema")) != (artifact["kind"], artifact["schema"]):
            raise RegistrationError("manifest_artifact_contract_mismatch")
    expected["artifacts"].extend(additions)
    return expected, len(additions)


def _stable_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail when v2 evidence or receipt artifacts are unregistered")
    mode.add_argument("--write", action="store_true", help="add missing digest-bound evidence and receipt artifacts")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=pathlib.Path)
    args = parser.parse_args()
    try:
        manifest = _load_json(args.manifest)
        expected, additions = registered_manifest(manifest)
    except RegistrationError as exc:
        print(f"FAIL register operation-document artifacts: {exc}", file=sys.stderr)
        return 1
    if args.check:
        if manifest != expected:
            print(f"FAIL operation-document artifact registration: missing={additions}", file=sys.stderr)
            return 1
        print(f"ok operation-document artifact registration (added=0)")
        return 0
    args.manifest.write_bytes(_stable_json_bytes(expected))
    print(f"registered operation-document artifacts (added={additions})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
