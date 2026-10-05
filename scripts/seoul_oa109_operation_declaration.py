#!/usr/bin/env python3
"""Pinned local declaration for Seoul's OA-109 last-train operation.

This module validates only the reviewed portal/UDDI/service join and emits its
source-backed Registry operation.  It never contacts the provider or includes
sample values, credentials, or response-shape assumptions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit


ROOT = pathlib.Path(__file__).resolve().parents[1]
DECLARATION_PATH = ROOT / "contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json"
DECLARATION_SHA256 = "06e8bc790be4c2511bda454f2adae8bc1c45746e2dfee0eb19a5ed4fb999328b"
DECLARATION_ID = "data-go-kr:OA-109:SearchLastTrainTimeByIDService:v1"
PROVENANCE_METHOD = "data_go_kr_seoul_target_navigation_declaration_v1"
OPERATION_KEY = "OA-109:SearchLastTrainTimeByIDService"
HISTORICAL_SUBJECT_SNAPSHOT_PATH = (
    "contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json"
)
HISTORICAL_SUBJECT_SNAPSHOT_SHA256 = "9336a1fe033af542b270364969264a26ae021906ed0f9e988f79775833119739"
HISTORICAL_SUBJECT_SNAPSHOT_BYTES = 13937
HISTORICAL_SUBJECT_ROW_SHA256 = "2146cee4cdeeeef451611c4e7edfba8962d02e4896145d021dc7ddde04ee8ca4"
HISTORY_ENDPOINTS = (
    "http://data.seoul.go.kr/dataList/datasetView.do?infId=OA-109&srvType=A&serviceKind=1",
    "http://data.seoul.go.kr/dataList/datasetView.do?infId=OA-101&srvType=A&serviceKind=1",
    "http://data.seoul.go.kr/dataList/datasetView.do?infId=OA-108&srvType=A&serviceKind=1",
)
PAGE_URL = "https://www.data.go.kr/data/15056854/openapi.do"
RESOLVER_URL = "https://www.data.go.kr/tcs/dss/selectApiLinkUrl.do?publicDataPk=15056854"


class DeclarationError(ValueError):
    """A fixed, safe rejection of the pinned declaration contract."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def source_fingerprint(row: Mapping[str, Any]) -> str:
    source, raw = _source(row)
    source_raw = dict(raw)
    source_raw.pop("request_cnt", None)
    view = {
        "provider": row.get("provider"),
        "id": str(row.get("id") or ""),
        "source_system": source.get("system"),
        "source_url": source.get("url"),
        "source_raw": source_raw,
    }
    return digest_json(view)


def guide_fingerprint(row: Mapping[str, Any]) -> str | None:
    _source_value, raw = _source(row)
    value = raw.get("guide_url")
    if not isinstance(value, str) or not value.strip():
        return None
    return sha256_bytes(value.encode("utf-8"))


def _read_declaration() -> dict[str, Any]:
    try:
        raw = DECLARATION_PATH.read_bytes()
    except OSError as exc:
        raise DeclarationError("pinned declaration is unavailable") from exc
    if sha256_bytes(raw) != DECLARATION_SHA256:
        raise DeclarationError("pinned declaration digest mismatch")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeclarationError("pinned declaration is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "datapan.provider-operation-declaration.v1"
        or value.get("declaration_id") != DECLARATION_ID
    ):
        raise DeclarationError("pinned declaration identity mismatch")
    prefix = value.get("historical_operation_prefix")
    if (
        not isinstance(prefix, Mapping)
        or prefix.get("subject_snapshot_path") != HISTORICAL_SUBJECT_SNAPSHOT_PATH
        or prefix.get("subject_snapshot_sha256") != HISTORICAL_SUBJECT_SNAPSHOT_SHA256
        or prefix.get("subject_snapshot_bytes") != HISTORICAL_SUBJECT_SNAPSHOT_BYTES
        or prefix.get("subject_snapshot_row_sha256") != HISTORICAL_SUBJECT_ROW_SHA256
        or prefix.get("subject_snapshot_row_index") != 1131
        or prefix.get("source_registry_rows") != 12282
    ):
        raise DeclarationError("pinned historical subject snapshot reference mismatch")
    return value


DECLARATION = _read_declaration()


def declaration_sha256() -> str:
    return DECLARATION_SHA256


def native_document_sha256s() -> dict[str, str]:
    return {
        name: str(value["sha256"])
        for name, value in DECLARATION["native_documents"].items()
    }


def native_document_acquisition_provenance() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, document in DECLARATION["native_documents"].items():
        acquisition = document.get("acquisition_provenance")
        if not isinstance(acquisition, Mapping):
            raise DeclarationError("declaration_document_acquisition_provenance_missing")
        result[name] = copy.deepcopy(dict(acquisition))
    if set(result) != {"guide", "service_download", "service_fragment"}:
        raise DeclarationError("declaration_document_acquisition_set_invalid")
    return result


def source_review_provenance() -> dict[str, Any]:
    review = DECLARATION.get("source_review")
    if not isinstance(review, Mapping):
        raise DeclarationError("declaration_source_review_provenance_missing")
    keys = (
        "review_decision_comment_id", "review_decision_comment_url", "review_decision_recorded_at",
        "review_decision_author", "review_decision_author_association", "review_decision_body_sha256",
        "review_decision_native_readback_sha256", "facts_sha256", "navigation_review_sha256",
    )
    value = {key: copy.deepcopy(review.get(key)) for key in keys}
    if (
        value["review_decision_comment_id"] != 5987467565
        or value["review_decision_comment_url"] != "https://github.com/StatPan/datapan-registry/issues/726#issuecomment-5987467565"
        or value["review_decision_recorded_at"] != "2026-10-05T03:14:39Z"
        or value["review_decision_author"] != "StatPan"
        or value["review_decision_author_association"] != "OWNER"
        or value["review_decision_body_sha256"] != "4c1d12657f355d3c2134cf8b9771f6b57979ee59328497cb46ff61673d652692"
        or value["review_decision_native_readback_sha256"] != "6c12eefda06025011a316261afd12a0de49c4befee9bdce79c6f2e48d0f4099b"
        or value["facts_sha256"] != "86c3d5f57fe4bd03c23e72f98db1cc695ea188f4fa3950a6f6878d63568ff31b"
        or value["navigation_review_sha256"] != "05b98551a908018053796845a393564c6b961351391bbc57e49abbdd09ad126c"
    ):
        raise DeclarationError("declaration_source_review_record_invalid")
    _timestamp(value["review_decision_recorded_at"])
    return value


def _source(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    source = row.get("source")
    raw = source.get("raw") if isinstance(source, Mapping) else None
    return (source if isinstance(source, Mapping) else {}, raw if isinstance(raw, Mapping) else {})


def _validate_subject_row_structure(row: Mapping[str, Any]) -> None:
    """Validate the fixed subject identity and exact ordered three-operation prefix."""
    subject = DECLARATION["subject"]
    source, raw = _source(row)
    if (
        row.get("provider") != subject["provider"]
        or str(row.get("id")) != subject["portal_dataset_id"]
        or source.get("system") != subject["source_system"]
        or source.get("url") != subject["source_url"]
        or raw.get("api_type") != subject["source_api_type"]
        or raw.get("id") != subject["source_uddi"]
        or raw.get("list_id") != subject["portal_dataset_id"]
        or raw.get("meta_url") != subject["source_meta_url"]
        or raw.get("guide_url") != subject["source_guide_url"]
    ):
        raise DeclarationError("portal_source_subject_mismatch")
    operations = row.get("operations")
    prefix = DECLARATION.get("historical_operation_prefix")
    if (
        not isinstance(prefix, Mapping)
        or prefix.get("schema") != "datapan.seoul-historical-operation-prefix.v1"
        or prefix.get("source_path") != "data/data-go-kr.registry.json"
        or prefix.get("source_main_commit") != "0085b357289755d6b3fb60a7259bd85da42d2962"
        or prefix.get("source_file_git_blob_sha1") != "fcd4a814348b7beff832737402b5ec22642d4228"
        or prefix.get("source_file_sha256") != "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0"
        or prefix.get("source_manifest_sha256") != "89afa12758dd3eded5ce5a96275572a8826574d4e7ead020412f7a76d6bb60ba"
        or prefix.get("source_lfs_bytes") != 139155499
        or prefix.get("source_lfs_sha256") != "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0"
        or prefix.get("provider") != subject["provider"]
        or prefix.get("api_id") != subject["portal_dataset_id"]
        or prefix.get("source_url") != subject["source_url"]
        or prefix.get("operation_count") != len(HISTORY_ENDPOINTS)
        or prefix.get("ordered_endpoints") != list(HISTORY_ENDPOINTS)
        or not isinstance(operations, list)
        or len(operations) != prefix.get("operation_count")
        or digest_json(operations) != prefix.get("canonical_operations_sha256")
    ):
        raise DeclarationError("portal_historical_operation_set_mismatch")
    endpoints: list[str] = []
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise DeclarationError("portal_historical_operation_invalid")
        operation_source, operation_raw = _source(operation)
        if operation_source.get("system") != "data.go.kr" or operation_raw.get("api_type") != "LINK":
            raise DeclarationError("portal_historical_operation_invalid")
        endpoint = operation.get("endpoint")
        if not isinstance(endpoint, str) or operation_raw.get("operation_url") != endpoint:
            raise DeclarationError("portal_historical_operation_endpoint_mismatch")
        endpoints.append(endpoint)
    if tuple(endpoints) != HISTORY_ENDPOINTS:
        raise DeclarationError("portal_historical_operation_order_or_target_mismatch")
    for operation in operations:
        operation_source, operation_raw = _source(operation)
        current_raw = copy.deepcopy(dict(raw))
        historical_raw = copy.deepcopy(dict(operation_raw))
        for volatile_operation_field in ("operation_nm", "operation_url"):
            current_raw.pop(volatile_operation_field, None)
            historical_raw.pop(volatile_operation_field, None)
        if operation_source.get("system") != subject["source_system"] or operation_source.get("url") != subject["source_url"] or historical_raw != current_raw:
            raise DeclarationError("portal_historical_operation_source_mismatch")


def _snapshot_source_provenance() -> dict[str, Any]:
    prefix = DECLARATION["historical_operation_prefix"]
    return {
        "repository": "StatPan/datapan-registry",
        "commit": prefix["source_main_commit"],
        "manifest_sha256": prefix["source_manifest_sha256"],
        "path": prefix["source_path"],
        "git_blob_sha1": prefix["source_file_git_blob_sha1"],
        "lfs_bytes": prefix["source_lfs_bytes"],
        "lfs_sha256": prefix["source_lfs_sha256"],
        "row_index": prefix["subject_snapshot_row_index"],
        "registry_row_count": prefix["source_registry_rows"],
    }


def historical_subject_snapshot_bytes_from_source(source_path: pathlib.Path) -> bytes:
    """Rebuild the pinned compact subject artifact from the exact authenticated 0085 LFS bytes."""
    prefix = DECLARATION["historical_operation_prefix"]
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        raise DeclarationError("portal_historical_source_payload_unavailable") from exc
    if (
        len(source_bytes) != prefix.get("source_lfs_bytes")
        or sha256_bytes(source_bytes) != prefix.get("source_lfs_sha256")
    ):
        raise DeclarationError("portal_historical_source_payload_digest_mismatch")
    try:
        registry = json.loads(source_bytes.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeclarationError("portal_historical_source_payload_invalid") from exc
    if not isinstance(registry, list) or len(registry) != prefix.get("source_registry_rows"):
        raise DeclarationError("portal_historical_source_row_inventory_mismatch")
    subject = DECLARATION["subject"]
    matches = [
        (index, row) for index, row in enumerate(registry)
        if isinstance(row, Mapping)
        and row.get("provider") == subject["provider"]
        and str(row.get("id") or "") == subject["portal_dataset_id"]
    ]
    if len(matches) != 1 or matches[0][0] != prefix.get("subject_snapshot_row_index"):
        raise DeclarationError("portal_historical_source_subject_identity_mismatch")
    row = copy.deepcopy(dict(matches[0][1]))
    _validate_subject_row_structure(row)
    row_bytes = canonical_json(row)
    operations = row.get("operations")
    if not isinstance(operations, list):
        raise DeclarationError("portal_historical_operation_set_mismatch")
    row_sha256 = sha256_bytes(row_bytes)
    operation_sha256 = digest_json(operations)
    if (
        row_sha256 != prefix.get("subject_snapshot_row_sha256")
        or len(row_bytes) != 10589
        or operation_sha256 != prefix.get("canonical_operations_sha256")
    ):
        raise DeclarationError("portal_historical_subject_snapshot_content_mismatch")
    snapshot = {
        "schema_version": "datapan.provider-operation-historical-subject-snapshot.v1",
        "source": _snapshot_source_provenance(),
        "subject_identity": {
            "provider": subject["provider"],
            "id": subject["portal_dataset_id"],
        },
        "row_bytes_canonical_json": len(row_bytes),
        "row_sha256_canonical_json": row_sha256,
        "ordered_operations_count": len(operations),
        "ordered_operations_sha256_canonical_json": operation_sha256,
        "subject_row": row,
    }
    return (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def load_historical_subject_snapshot(root: pathlib.Path = ROOT) -> dict[str, Any]:
    """Load the independently source-pinned historical row without consulting current canonical data."""
    path = root / HISTORICAL_SUBJECT_SNAPSHOT_PATH
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
        if path.is_symlink() or not path.is_file():
            raise OSError("snapshot path is not a regular file")
        raw = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise DeclarationError("portal_historical_subject_snapshot_unavailable") from exc
    if len(raw) != HISTORICAL_SUBJECT_SNAPSHOT_BYTES or sha256_bytes(raw) != HISTORICAL_SUBJECT_SNAPSHOT_SHA256:
        raise DeclarationError("portal_historical_subject_snapshot_digest_mismatch")
    try:
        snapshot = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeclarationError("portal_historical_subject_snapshot_invalid") from exc
    required = {
        "schema_version", "source", "subject_identity", "row_bytes_canonical_json",
        "row_sha256_canonical_json", "ordered_operations_count",
        "ordered_operations_sha256_canonical_json", "subject_row",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != required:
        raise DeclarationError("portal_historical_subject_snapshot_shape_invalid")
    prefix = DECLARATION["historical_operation_prefix"]
    subject = DECLARATION["subject"]
    row = snapshot.get("subject_row")
    if (
        snapshot.get("schema_version") != "datapan.provider-operation-historical-subject-snapshot.v1"
        or snapshot.get("source") != _snapshot_source_provenance()
        or snapshot.get("subject_identity") != {
            "provider": subject["provider"], "id": subject["portal_dataset_id"],
        }
        or not isinstance(row, Mapping)
    ):
        raise DeclarationError("portal_historical_subject_snapshot_source_mismatch")
    row_bytes = canonical_json(row)
    operations = row.get("operations")
    if (
        snapshot.get("row_bytes_canonical_json") != len(row_bytes)
        or snapshot.get("row_sha256_canonical_json") != HISTORICAL_SUBJECT_ROW_SHA256
        or sha256_bytes(row_bytes) != HISTORICAL_SUBJECT_ROW_SHA256
        or snapshot.get("ordered_operations_count") != prefix.get("operation_count")
        or not isinstance(operations, list)
        or snapshot.get("ordered_operations_sha256_canonical_json") != prefix.get("canonical_operations_sha256")
        or digest_json(operations) != prefix.get("canonical_operations_sha256")
    ):
        raise DeclarationError("portal_historical_subject_snapshot_content_mismatch")
    try:
        _validate_subject_row_structure(row)
    except (TypeError, ValueError) as exc:
        raise DeclarationError("portal_historical_subject_snapshot_subject_invalid") from exc
    return copy.deepcopy(snapshot)


def validate_subject_row(row: Mapping[str, Any], *, root: pathlib.Path = ROOT) -> None:
    """Require an exact original source row, allowing only volatile request count changes."""
    _validate_subject_row_structure(row)
    snapshot = load_historical_subject_snapshot(root)
    actual = copy.deepcopy(dict(row))
    expected = copy.deepcopy(snapshot["subject_row"])
    for candidate in (actual, expected):
        source, raw = _source(candidate)
        if isinstance(source, dict) and isinstance(raw, dict):
            raw.pop("request_cnt", None)
    if actual != expected:
        raise DeclarationError("portal_source_row_differs_from_pinned_historical_snapshot")


def validate_stored_subject_row(
    row: Mapping[str, Any], *, root: pathlib.Path = ROOT,
) -> dict[str, Any]:
    """Require the pinned source row plus exactly its reviewed declaration operation."""
    snapshot = load_historical_subject_snapshot(root)
    original = copy.deepcopy(snapshot["subject_row"])
    operations = row.get("operations")
    if not isinstance(operations, list) or len(operations) != len(original["operations"]) + 1:
        raise DeclarationError("portal_stored_operation_set_mismatch")
    declared = [
        operation for operation in operations
        if isinstance(operation, Mapping)
        and _source(operation)[1].get("operation_declaration_id") == DECLARATION_ID
    ]
    if len(declared) != 1 or operations[-1] != declared[0]:
        raise DeclarationError("portal_stored_declaration_operation_missing_or_misordered")
    source, raw = _source(row)
    guide = raw.get("guide_url")
    if guide is None:
        observed_guide: str | None = None
    elif isinstance(guide, str) and guide.strip():
        parsed = urlsplit(guide)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DeclarationError("portal_stored_guide_url_invalid")
        observed_guide = guide
    else:
        # The reviewed enrichment transform removes the original empty guide
        # field. Keeping it empty would be a different serialized state.
        raise DeclarationError("portal_stored_empty_guide_was_not_normalized")
    expected = copy.deepcopy(original)
    expected_source = expected.get("source")
    if not isinstance(expected_source, dict) or not isinstance(expected_source.get("raw"), dict):
        raise DeclarationError("portal_historical_subject_snapshot_invalid")
    expected_source_raw = expected_source["raw"]
    if observed_guide is None:
        expected_source_raw.pop("guide_url", None)
    else:
        expected_source_raw["guide_url"] = observed_guide
    expected["operations"] = [
        *copy.deepcopy(original["operations"]), build_operation(original, observed_guide, root=root),
    ]
    actual = copy.deepcopy(dict(row))
    for candidate in (actual, expected):
        candidate_source = candidate.get("source")
        candidate_raw = candidate_source.get("raw") if isinstance(candidate_source, dict) else None
        if isinstance(candidate_raw, dict):
            candidate_raw.pop("request_cnt", None)
    if actual != expected:
        raise DeclarationError("portal_stored_subject_differs_from_pinned_historical_transform")
    return expected


def validate_committed_prefix_snapshot(
    root: pathlib.Path = ROOT, *, verify_materialized_row: bool = True,
) -> dict[str, Any]:
    """Verify the reviewed commit, manifest, Git LFS pointer, and hydrated bytes when available.

    The prefix operations themselves are separately checked by validate_subject_row
    against the declaration's pinned canonical digest. A pointer-only checkout can
    authenticate the committed LFS object identity without claiming that its payload
    was materialized in that checkout.
    """
    prefix = DECLARATION.get("historical_operation_prefix")
    if not isinstance(prefix, Mapping):
        raise DeclarationError("portal_historical_operation_pin_missing")
    commit = str(prefix.get("source_main_commit") or "")
    source_path = str(prefix.get("source_path") or "")

    def git_bytes(*args: str) -> bytes:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise DeclarationError("portal_historical_source_commit_unavailable") from exc
        return result.stdout

    resolved = git_bytes("rev-parse", "--verify", f"{commit}^{{commit}}").decode("ascii", errors="strict").strip()
    if resolved != commit:
        raise DeclarationError("portal_historical_source_commit_mismatch")
    manifest = git_bytes("show", f"{commit}:manifest.json")
    if sha256_bytes(manifest) != prefix.get("source_manifest_sha256"):
        raise DeclarationError("portal_historical_source_manifest_mismatch")
    pointer = git_bytes("show", f"{commit}:{source_path}")
    git_blob_sha1 = hashlib.sha1(b"blob " + str(len(pointer)).encode("ascii") + b"\0" + pointer).hexdigest()
    if git_blob_sha1 != prefix.get("source_file_git_blob_sha1"):
        raise DeclarationError("portal_historical_source_pointer_blob_mismatch")
    try:
        lines = pointer.decode("ascii", errors="strict").splitlines()
    except UnicodeError as exc:
        raise DeclarationError("portal_historical_source_pointer_invalid") from exc
    expected_pointer = [
        "version https://git-lfs.github.com/spec/v1",
        f"oid sha256:{prefix.get('source_lfs_sha256')}",
        f"size {prefix.get('source_lfs_bytes')}",
    ]
    if lines != expected_pointer:
        raise DeclarationError("portal_historical_source_pointer_invalid")

    result: dict[str, Any] = {
        "commit": commit,
        "source_path": source_path,
        "source_manifest_sha256": sha256_bytes(manifest),
        "source_file_git_blob_sha1": git_blob_sha1,
        "source_lfs_sha256": prefix.get("source_lfs_sha256"),
        "source_lfs_bytes": prefix.get("source_lfs_bytes"),
        "materialized_registry_verified": False,
    }
    materialized_path = root / source_path
    try:
        materialized = materialized_path.read_bytes()
    except OSError as exc:
        raise DeclarationError("portal_historical_materialized_source_unavailable") from exc
    if materialized == pointer:
        return result
    if (
        len(materialized) != prefix.get("source_lfs_bytes")
        or sha256_bytes(materialized) != prefix.get("source_lfs_sha256")
    ):
        raise DeclarationError("portal_historical_materialized_source_mismatch")
    result["materialized_registry_bytes_verified"] = True
    result["materialized_registry_sha256"] = sha256_bytes(materialized)
    if not verify_materialized_row:
        return result
    try:
        registry = json.loads(materialized.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeclarationError("portal_historical_materialized_source_invalid") from exc
    if not isinstance(registry, list):
        raise DeclarationError("portal_historical_materialized_source_invalid")
    subject = DECLARATION["subject"]
    matches = [
        item for item in registry
        if isinstance(item, Mapping)
        and item.get("provider") == subject["provider"]
        and str(item.get("id") or "") == subject["portal_dataset_id"]
    ]
    if len(matches) != 1:
        raise DeclarationError("portal_historical_materialized_source_identity_mismatch")
    validate_subject_row(matches[0])
    result["materialized_registry_verified"] = True
    return result


def validate_historical_operation(
    row: Mapping[str, Any], operation: Mapping[str, Any], *, observed_guide_url: str | None,
    root: pathlib.Path = ROOT,
) -> None:
    """Accept only one exact historical operation from the pinned prefix."""
    subject = DECLARATION["subject"]
    _current_source, current_raw = _source(row)
    current_guide = current_raw.get("guide_url")
    if observed_guide_url is None:
        if current_guide not in (None, ""):
            raise DeclarationError("portal_source_guide_observation_mismatch")
    elif current_guide != observed_guide_url:
        raise DeclarationError("portal_source_guide_observation_mismatch")
    validate_stored_subject_row(row, root=root)
    original = copy.deepcopy(dict(row))
    original_source = original.get("source")
    original_source = copy.deepcopy(dict(original_source)) if isinstance(original_source, Mapping) else {}
    original_raw = original_source.get("raw")
    original_raw = copy.deepcopy(dict(original_raw)) if isinstance(original_raw, Mapping) else {}
    original_raw["guide_url"] = subject["source_guide_url"]
    original_source["raw"] = original_raw
    original["source"] = original_source
    all_operations = original.get("operations")
    if not isinstance(all_operations, list):
        raise DeclarationError("portal_historical_operation_set_mismatch")
    declared = [
        item for item in all_operations
        if isinstance(item, Mapping)
        and _source(item)[1].get("operation_declaration_id") == DECLARATION_ID
    ]
    if declared:
        if len(declared) != 1:
            raise DeclarationError("portal_historical_operation_set_mismatch")
        all_operations = list(all_operations)
        all_operations.remove(declared[0])
    original["operations"] = all_operations
    validate_subject_row(original, root=root)
    if not any(isinstance(item, Mapping) and dict(item) == dict(operation) for item in all_operations):
        raise DeclarationError("portal_historical_operation_not_in_pinned_prefix")


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise DeclarationError("declaration_observation_time_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeclarationError("declaration_observation_time_invalid") from exc
    if parsed.tzinfo is None:
        raise DeclarationError("declaration_observation_time_unzoned")
    return parsed.astimezone(timezone.utc)


def _safe_exact_resolver_context(
    row: Mapping[str, Any], link_metadata: Mapping[str, Any], *, root: pathlib.Path = ROOT,
) -> None:
    validate_subject_row(row, root=root)
    subject = DECLARATION["subject"]
    page = link_metadata.get("page")
    resolver = link_metadata.get("resolver")
    if not isinstance(page, Mapping) or not isinstance(resolver, Mapping):
        raise DeclarationError("declaration_page_resolver_provenance_missing")
    if (
        link_metadata.get("method") != "data_go_kr_select_api_link_url_v1"
        or link_metadata.get("dataset_id") != subject["portal_dataset_id"]
        or link_metadata.get("public_data_pk") != subject["portal_dataset_id"]
        or link_metadata.get("public_data_detail_pk") != subject["source_uddi"]
        or page.get("url") != PAGE_URL
        or page.get("effective_url") != PAGE_URL
        or resolver.get("request_url") != RESOLVER_URL
        or resolver.get("effective_url") != RESOLVER_URL
        or resolver.get("public_data_detail_pk") != subject["source_uddi"]
        or resolver.get("resolved_url") != subject["source_metadata_target"]
        or resolver.get("resolved_url_sha256") != sha256_bytes(subject["source_metadata_target"].encode("utf-8"))
    ):
        raise DeclarationError("declaration_page_resolver_identity_mismatch")
    for row_value, digest_key in ((page, "sha256"), (resolver, "sha256")):
        if not re.fullmatch(r"[a-f0-9]{64}", str(row_value.get(digest_key) or "")):
            raise DeclarationError("declaration_observation_digest_invalid")
        _timestamp(row_value.get("observed_at"))
        if isinstance(row_value.get("bytes"), bool) or not isinstance(row_value.get("bytes"), int) or row_value["bytes"] <= 0:
            raise DeclarationError("declaration_observation_size_invalid")
    page_time = _timestamp(page.get("observed_at"))
    resolver_time = _timestamp(resolver.get("observed_at"))
    if resolver_time < page_time:
        raise DeclarationError("declaration_observation_order_invalid")


def request_parameters() -> list[dict[str, Any]]:
    """Project only the exact named request facts; no sample values/defaults."""
    out: list[dict[str, Any]] = []
    enums = DECLARATION["enums"]
    for item in DECLARATION["request_parameters"]:
        value: dict[str, Any] = {
            "name": item["name"],
            "label": item["label"],
            "type": item["type"],
            "required": item["required"],
            "location": item["location"],
        }
        if item["name"] == "TYPE":
            value["enum_values"] = list(DECLARATION["transport"]["format_selectors"])
        elif item["name"] in enums:
            value["enum_values"] = list(enums[item["name"]])
            value["enum_labels"] = copy.deepcopy(enums[item["name"]])
        if item["name"] == "KEY":
            value["auth"] = True
        out.append(value)
    return out


def response_parameters() -> list[dict[str, str]]:
    # The reviewed documents declare names and labels only.  Do not add guessed
    # response types, requiredness, nullability, envelope, or media types.
    return [
        {"name": item["name"], "label": item["label"]}
        for item in DECLARATION["response_parameters"]
    ]


def build_operation(
    row: Mapping[str, Any], observed_guide_url: str | None, *, root: pathlib.Path = ROOT,
) -> dict[str, Any]:
    validate_subject_row(row, root=root)
    subject = DECLARATION["subject"]
    raw: dict[str, Any] = {
        "api_type": "REST",
        "operation_declaration_id": DECLARATION_ID,
        "operation_declaration_key": OPERATION_KEY,
        "operation_declaration_sha256": DECLARATION_SHA256,
        "provider_dataset_id": subject["provider_dataset_id"],
        "service_name": subject["service_name"],
        "operation_url": DECLARATION["transport"]["endpoint_template"],
        "method_evidence": DECLARATION["transport"]["method_evidence"],
        "method_evidence_classification": DECLARATION["transport"]["method_evidence_classification"],
        "native_document_sha256s": native_document_sha256s(),
    }
    if observed_guide_url:
        raw["guide_url"] = observed_guide_url
    return {
        "name": subject["service_name"],
        "endpoint": DECLARATION["transport"]["endpoint_template"],
        "http_method": DECLARATION["transport"]["method"],
        "method_evidence": DECLARATION["transport"]["method_evidence"],
        "request_params": request_parameters(),
        "response_params": response_parameters(),
        "source": {
            "system": "data.go.kr",
            "url": PAGE_URL,
            "raw": raw,
        },
    }


def build_provenance(
    row: Mapping[str, Any],
    *,
    source_sha256: str,
    guide_sha256: str | None,
    observed_guide_url: str | None,
    observed_guide_url_sha256: str | None,
    link_metadata: Mapping[str, Any],
    operation: Mapping[str, Any],
    root: pathlib.Path = ROOT,
) -> dict[str, Any]:
    _safe_exact_resolver_context(row, link_metadata, root=root)
    if not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
        raise DeclarationError("declaration_source_fingerprint_invalid")
    if guide_sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}", guide_sha256):
        raise DeclarationError("declaration_guide_fingerprint_invalid")
    expected_observed_digest = sha256_bytes(observed_guide_url.encode("utf-8")) if observed_guide_url else None
    if observed_guide_url_sha256 != expected_observed_digest:
        raise DeclarationError("declaration_observed_guide_binding_invalid")
    return {
        "method": PROVENANCE_METHOD,
        "declaration_id": DECLARATION_ID,
        "declaration_sha256": DECLARATION_SHA256,
        "subject": copy.deepcopy(DECLARATION["subject"]),
        "native_document_sha256s": native_document_sha256s(),
        "native_document_acquisition": native_document_acquisition_provenance(),
        "source_review_record": source_review_provenance(),
        "historical_operation_prefix": copy.deepcopy(DECLARATION["historical_operation_prefix"]),
        "method_derivation": {
            "id": DECLARATION["transport"]["method_evidence"],
            "classification": DECLARATION["transport"]["method_evidence_classification"],
            "execution_verified": False,
            "exclusive_method_claim": False,
        },
        "source_binding": {
            "source_sha256": source_sha256,
            "guide_sha256": guide_sha256,
            "observed_guide_url": observed_guide_url,
            "observed_guide_url_sha256": observed_guide_url_sha256,
        },
        "page_resolver": copy.deepcopy(dict(link_metadata)),
        "operation_sha256": digest_json(dict(operation)),
    }


def validate_enriched_record(
    row: Mapping[str, Any], record: Mapping[str, Any], *, root: pathlib.Path = ROOT,
) -> None:
    """Validate exact declaration, live metadata chain, and additive operation."""
    provenance = record.get("declaration_provenance")
    operations = record.get("operations")
    if not isinstance(provenance, Mapping) or not isinstance(operations, list):
        raise DeclarationError("declaration_enrichment_provenance_missing")
    validate_subject_row(row, root=root)
    if record.get("source_sha256") != source_fingerprint(row) or record.get("guide_sha256") != guide_fingerprint(row):
        raise DeclarationError("declaration_source_binding_invalid")
    expected_count = len(row["operations"]) + 1
    if len(operations) != expected_count or operations[:len(row["operations"])] != row["operations"]:
        raise DeclarationError("declaration_historical_operations_not_preserved")
    expected_operation = build_operation(row, record.get("observed_guide_url"), root=root)
    if operations[-1] != expected_operation:
        raise DeclarationError("declaration_generated_operation_mismatch")
    context = provenance.get("page_resolver")
    if not isinstance(context, Mapping):
        raise DeclarationError("declaration_page_resolver_provenance_missing")
    _safe_exact_resolver_context(row, context, root=root)
    source_provenance = record.get("source_provenance")
    page = context.get("page")
    if (
        not isinstance(source_provenance, Mapping)
        or not isinstance(page, Mapping)
        or source_provenance.get("system") != "data.go.kr"
        or source_provenance.get("page_url") != page.get("url")
        or source_provenance.get("effective_url") != page.get("effective_url")
        or source_provenance.get("page_sha256") != page.get("sha256")
        or source_provenance.get("observed_at") != page.get("observed_at")
    ):
        raise DeclarationError("declaration_page_source_binding_mismatch")
    expected = build_provenance(
        row,
        source_sha256=str(record.get("source_sha256") or ""),
        guide_sha256=record.get("guide_sha256"),
        observed_guide_url=record.get("observed_guide_url"),
        observed_guide_url_sha256=record.get("observed_guide_url_sha256"),
        link_metadata=context,
        operation=expected_operation,
        root=root,
    )
    if dict(provenance) != expected:
        raise DeclarationError("declaration_enrichment_provenance_mismatch")


def validate_declared_operation(
    row: Mapping[str, Any], operation: Mapping[str, Any], *, root: pathlib.Path = ROOT,
) -> None:
    """Validate a registry operation against the pinned declaration bytes."""
    subject_row = dict(row)
    source = subject_row.get("source")
    source = dict(source) if isinstance(source, Mapping) else {}
    raw = source.get("raw")
    raw = dict(raw) if isinstance(raw, Mapping) else {}
    operation_source = operation.get("source")
    operation_raw = operation_source.get("raw") if isinstance(operation_source, Mapping) else None
    operation_guide = operation_raw.get("guide_url") if isinstance(operation_raw, Mapping) else None
    expected_source_guide = DECLARATION["subject"]["source_guide_url"]
    current_source_guide = raw.get("guide_url")
    # Applying a valid page observation can remove the source row's empty
    # guide_url or replace it with the observed one. The record-level validator
    # binds that observation to the untouched candidate digest and operation;
    # here retain only those documented forms while checking the pinned source
    # identity against its original value.
    if current_source_guide not in (expected_source_guide, operation_guide) and not (
        current_source_guide is None and expected_source_guide == "" and operation_guide is None
    ):
        raise DeclarationError("portal_source_guide_observation_mismatch")
    raw["guide_url"] = expected_source_guide
    source["raw"] = raw
    subject_row["source"] = source
    operations = row.get("operations")
    if isinstance(operations, list):
        # A composed Registry row includes the declared operation alongside
        # the three historical metadata links.  Validate the original subject
        # set separately, without allowing that declared operation to replace
        # or rewrite any historical entry.
        remaining = list(operations)
        if any(
            isinstance(item, Mapping)
            and _source(item)[1].get("operation_declaration_id") == DECLARATION_ID
            for item in operations
        ):
            validate_stored_subject_row(row, root=root)
        try:
            remaining.remove(dict(operation))
        except ValueError:
            pass
        subject_row["operations"] = remaining
    source = operation.get("source")
    raw = source.get("raw") if isinstance(source, Mapping) else None
    observed_guide = raw.get("guide_url") if isinstance(raw, Mapping) else None
    expected = build_operation(subject_row, observed_guide, root=root)
    if dict(operation) != expected:
        raise DeclarationError("registry_declared_operation_mismatch")
