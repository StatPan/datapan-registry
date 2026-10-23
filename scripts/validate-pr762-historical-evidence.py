#!/usr/bin/env python3
"""Validate the inactive PR #762 v2 evidence archive without activating it."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
from typing import Any

from jsonschema import Draft202012Validator


ROOT = pathlib.Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = ROOT / "historical-evidence/pr-762-v2"
ARTIFACT_ROOT = ARCHIVE_ROOT / "artifacts"
ARCHIVE_PREFIX = "historical-evidence/pr-762-v2/"
ARTIFACT_PREFIX = ARCHIVE_PREFIX + "artifacts/"
SOURCE_PREFIX = "reports/operation-document-evidence/v2/"
EVIDENCE_SCHEMA = ROOT / "schemas/datapan.operation-document-evidence.v2.schema.json"
RECEIPT_SCHEMA = ROOT / "schemas/datapan.operation-document-capture-receipt.v2.schema.json"


class ArchiveError(ValueError):
    """Raised when preserved evidence is incomplete, changed, or active."""


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    def pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ArchiveError(f"duplicate_json_key:{path}:{key}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs_no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"json_invalid:{path}") from exc
    if not isinstance(value, dict):
        raise ArchiveError(f"json_object_required:{path}")
    return value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe_archive_path(root: pathlib.Path, relative: str) -> pathlib.Path:
    path = root / relative
    try:
        resolved = path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ArchiveError(f"archive_path_outside_root:{relative}") from exc
    if path.is_symlink() or not path.is_file():
        raise ArchiveError(f"archive_file_invalid:{relative}")
    return root / resolved


def _git_text(root: pathlib.Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ArchiveError(f"git_read_failed:{' '.join(args)}") from exc
    return result.stdout


def _git_blob(root: pathlib.Path, ref: str, source_path: str) -> bytes:
    try:
        return subprocess.check_output(["git", "-C", str(root), "show", f"{ref}:{source_path}"])
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ArchiveError(f"git_blob_missing:{ref}:{source_path}") from exc


def _tree_paths(root: pathlib.Path, ref: str) -> set[str]:
    output = _git_text(root, "ls-tree", "-r", "--name-only", ref, "--", SOURCE_PREFIX)
    return {line for line in output.splitlines() if line}


def _is_evidence(path: str) -> bool:
    return path.startswith(SOURCE_PREFIX) and "/receipts/" not in path and path.endswith(".json")


def _is_receipt(path: str) -> bool:
    return path.startswith(SOURCE_PREFIX + "receipts/") and path.endswith(".json")


def _validate_tree_binding(
    root: pathlib.Path,
    inventory: dict[str, Any],
    *,
    source_ref: str,
    main_ref: str,
) -> tuple[int, int, int, int]:
    source_paths = _tree_paths(root, source_ref)
    main_paths = _tree_paths(root, main_ref)
    source_evidence = {path for path in source_paths if _is_evidence(path)}
    source_receipts = {path for path in source_paths if _is_receipt(path)}
    main_evidence = {path for path in main_paths if _is_evidence(path)}
    main_receipts = {path for path in main_paths if _is_receipt(path)}
    expected_pr_only = (source_evidence - main_evidence) | (source_receipts - main_receipts)
    recorded_pr_only = {record["source_path"] for record in inventory["files"]}
    if expected_pr_only != recorded_pr_only:
        raise ArchiveError("source_tree_pr_only_set_mismatch")
    expected_overlap = (source_evidence & main_evidence) | (source_receipts & main_receipts)
    recorded_overlap = {record["source_path"] for record in inventory["overlap"]}
    if expected_overlap != recorded_overlap:
        raise ArchiveError("source_tree_overlap_set_mismatch")
    for path in sorted(expected_pr_only):
        source_raw = _git_blob(root, source_ref, path)
        record = next(record for record in inventory["files"] if record["source_path"] == path)
        if record["git_blob_sha1"] != _git_text(root, "rev-parse", f"{source_ref}:{path}").strip():
            raise ArchiveError(f"source_git_blob_sha_mismatch:{path}")
        if record["sha256"] != _sha256(source_raw):
            raise ArchiveError(f"source_sha256_mismatch:{path}")
    for path in sorted(expected_overlap):
        source_raw = _git_blob(root, source_ref, path)
        main_raw = _git_blob(root, main_ref, path)
        if source_raw != main_raw:
            raise ArchiveError(f"overlap_source_bytes_changed:{path}")
    return len(source_evidence - main_evidence), len(source_receipts - main_receipts), len(source_evidence & main_evidence), len(source_receipts & main_receipts)


def validate_archive(
    root: pathlib.Path = ROOT,
    *,
    source_ref: str | None = None,
    main_ref: str | None = None,
) -> dict[str, Any]:
    archive_root = root / ARCHIVE_ROOT.relative_to(ROOT)
    artifact_root = root / ARTIFACT_ROOT.relative_to(ROOT)
    metadata = _load_json(archive_root / "metadata.json")
    inventory = _load_json(archive_root / "inventory.json")
    if metadata.get("archive_kind") != "historical_pr762_v2_operation_document_evidence":
        raise ArchiveError("archive_kind_invalid")
    if metadata.get("status") != "inactive_archive_only" or metadata.get("automatic_consumption") is not False:
        raise ArchiveError("archive_activation_metadata_invalid")
    if inventory.get("schema_version") != "datapan.historical-pr762-v2-evidence-inventory.v1":
        raise ArchiveError("inventory_schema_version_invalid")
    if inventory.get("main_commit") != metadata.get("main_commit") or inventory.get("pr762_head_commit") != metadata.get("pr762_head_commit"):
        raise ArchiveError("inventory_commit_binding_invalid")
    records = inventory.get("files")
    overlaps = inventory.get("overlap")
    if not isinstance(records, list) or not isinstance(overlaps, list):
        raise ArchiveError("inventory_arrays_required")
    if len(records) != 466 or len(overlaps) != 334:
        raise ArchiveError("inventory_count_invalid")
    if metadata.get("counts") != {
        "overlap_evidence": 167,
        "overlap_receipts": 167,
        "pr_only_evidence": 233,
        "pr_only_receipts": 233,
        "pr_only_total": 466,
    }:
        raise ArchiveError("metadata_counts_invalid")
    if len({record.get("source_path") for record in records}) != len(records):
        raise ArchiveError("duplicate_source_path")
    if len({record.get("archive_path") for record in records}) != len(records):
        raise ArchiveError("duplicate_archive_path")
    evidence_records = [record for record in records if record.get("kind") == "evidence"]
    receipt_records = [record for record in records if record.get("kind") == "receipt"]
    if len(evidence_records) != 233 or len(receipt_records) != 233:
        raise ArchiveError("pair_count_invalid")
    evidence_names = {pathlib.Path(record["source_path"]).name for record in evidence_records}
    receipt_names = {pathlib.Path(record["source_path"]).name for record in receipt_records}
    if evidence_names != receipt_names:
        raise ArchiveError("evidence_receipt_pair_set_mismatch")
    path_list = "".join(record["source_path"] + "\n" for record in sorted(records, key=lambda item: item["source_path"]))
    if _sha256(path_list.encode("utf-8")) != metadata.get("path_list_sha256"):
        raise ArchiveError("path_list_digest_invalid")

    evidence_validator = Draft202012Validator(_load_json(root / EVIDENCE_SCHEMA.relative_to(ROOT)))
    receipt_validator = Draft202012Validator(_load_json(root / RECEIPT_SCHEMA.relative_to(ROOT)))
    record_by_name = {pathlib.Path(record["source_path"]).name: record for record in records}
    for record in records:
        source_path = record.get("source_path")
        archive_path = record.get("archive_path")
        if not isinstance(source_path, str) or not source_path.startswith(SOURCE_PREFIX):
            raise ArchiveError("source_path_invalid")
        if archive_path != ARTIFACT_PREFIX + source_path:
            raise ArchiveError(f"archive_path_binding_invalid:{source_path}")
        path = _safe_archive_path(root, archive_path)
        raw = path.read_bytes()
        if record.get("bytes") != len(raw) or record.get("sha256") != _sha256(raw):
            raise ArchiveError(f"archive_digest_mismatch:{source_path}")
        value = _load_json(path)
        errors = list((evidence_validator if record["kind"] == "evidence" else receipt_validator).iter_errors(value))
        if errors:
            raise ArchiveError(f"schema_invalid:{source_path}:{errors[0].message}")
        if record["kind"] == "evidence":
            identity = value.get("identity", {})
            expected_name = f"{identity.get('dataset_id')}-{identity.get('upstream_operation_key')}.json"
            if pathlib.Path(source_path).name != expected_name:
                raise ArchiveError(f"identity_filename_mismatch:{source_path}")
            receipt_record = record_by_name.get(pathlib.Path(source_path).name)
            if receipt_record is None:
                raise ArchiveError(f"receipt_missing:{source_path}")
            receipt_path = _safe_archive_path(root, receipt_record["archive_path"])
            receipt_value = _load_json(receipt_path)
            if receipt_value.get("operation_id") != identity.get("operation_id"):
                raise ArchiveError(f"receipt_operation_binding_invalid:{source_path}")
            if receipt_value.get("evidence_sha256") != _sha256(raw):
                raise ArchiveError(f"receipt_evidence_digest_invalid:{source_path}")

    manifest = _load_json(root / "manifest.json")
    manifest_paths = {item.get("path") for item in manifest.get("artifacts", []) if isinstance(item, dict)}
    if any(path.startswith(ARCHIVE_PREFIX) for path in manifest_paths):
        raise ArchiveError("archive_registered_in_active_manifest")
    if source_ref and main_ref:
        counts = _validate_tree_binding(root, inventory, source_ref=source_ref, main_ref=main_ref)
    elif source_ref or main_ref:
        raise ArchiveError("both_source_and_main_refs_required")
    else:
        counts = (233, 233, 167, 167)
    return {
        "archive_files": len(records),
        "archive_evidence": len(evidence_records),
        "archive_receipts": len(receipt_records),
        "overlap_files": len(overlaps),
        "pr_only_evidence": counts[0],
        "pr_only_receipts": counts[1],
        "overlap_evidence": counts[2],
        "overlap_receipts": counts[3],
        "active_manifest_paths": 0,
        "status": "pass",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ref", help="PR #762 source ref for optional Git-tree byte verification")
    parser.add_argument("--main-ref", help="main source ref for optional overlap verification")
    args = parser.parse_args()
    try:
        result = validate_archive(source_ref=args.source_ref, main_ref=args.main_ref)
    except ArchiveError as exc:
        print(f"FAIL historical PR #762 evidence archive: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
