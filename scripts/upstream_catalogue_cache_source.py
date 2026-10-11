#!/usr/bin/env python3
"""Authenticate and restore one selected processor cache's original A input.

The processing cache can outlive the Actions artifact that carried its input
catalogue.  New processor bundles therefore retain that exact admitted
candidate plus a closed descriptor.  Historical bundles use one bounded
fallback to the already selected owner's admitted A artifact; they never scan
for a different observation or treat claimed row fingerprints as authority.
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
import math
import os
import pathlib
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import urllib.parse
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "datapan.upstream-catalogue-cache-source-context.v1"
CACHE_SOURCE_PROFILE = "self-contained-original-candidate-v1"
CONTEXT_MEMBER = "upstream-catalogue-cache-source-context.json"
ORIGINAL_CANDIDATE_MEMBER = "upstream-catalogue-original-candidate.registry.json"
ENRICHMENT_MEMBER = "upstream-catalogue-enrichment-evidence.json"
CHECKPOINT_RECEIPT_MEMBER = "upstream-catalogue-checkpoint-receipt.json"
COLLECTOR_MEMBERS = frozenset({
    "candidate.registry.json",
    "catalog-diff.json",
    "upstream-refresh-evidence.json",
    "upstream-refresh-work-packet.json",
})
MODERN_GENERATOR_INPUTS = {
    "processor_script_sha256": "scripts/process-upstream-catalogue-candidate.py",
    "collector_handoff_helper_sha256": "scripts/upstream_catalogue_handoff.py",
    "cache_source_helper_sha256": "scripts/upstream_catalogue_cache_source.py",
    "seoul_operation_declaration_helper_sha256": "scripts/seoul_oa109_operation_declaration.py",
    "seoul_historical_subject_snapshot_generator_sha256": "scripts/generate-seoul-oa109-subject-snapshot.py",
    "seoul_operation_declaration_sha256": "contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json",
    "seoul_historical_subject_snapshot_sha256": "contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json",
}
HEX40 = re.compile(r"^[a-f0-9]{40}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
MAX_CONTEXT_BYTES = 64 * 1024
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATE_BYTES = 256 * 1024 * 1024
MAX_NATIVE_JSON_BYTES = 8 * 1024 * 1024
MAX_NATIVE_STDERR_BYTES = 64 * 1024
MAX_SOURCE_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 64 * 1024 * 1024
# Preserve the existing bounded nine-member processor bundle allowance, then
# add one exact original candidate and its closed descriptor.  The descriptor
# must not consume bytes from either pre-existing bounded payload.
MAX_LEGACY_CACHE_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_CACHE_ARCHIVE_BYTES = MAX_LEGACY_CACHE_BUNDLE_BYTES + MAX_CANDIDATE_BYTES + MAX_CONTEXT_BYTES
MAX_CACHE_EXPANDED_BYTES = MAX_LEGACY_CACHE_BUNDLE_BYTES + MAX_CANDIDATE_BYTES + MAX_CONTEXT_BYTES
MAX_COLLECTOR_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_COLLECTOR_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_WHOLE_ATTEMPT_SECONDS = 120.0
MAX_NATIVE_OPERATION_SECONDS = 2700.0
POLL_SECONDS = 0.1
TERMINATE_GRACE_SECONDS = 0.5
CLEANUP_RESERVE_SECONDS = TERMINATE_GRACE_SECONDS * 2 + POLL_SECONDS
_CAPABILITY = object()
_NATIVE_READER_CAPABILITY = object()
_HANDOFF: Any | None = None


class CacheSourceError(ValueError):
    """Authenticated state, descriptor, or archive bytes are malformed."""


class CacheSourceUnavailable(RuntimeError):
    """The selected legacy owner's exact A bytes are no longer available."""


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CacheSourceError("cache source value is not canonical JSON") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git_blob_sha1(value: bytes) -> str:
    return hashlib.sha1(f"blob {len(value)}\0".encode("ascii") + value).hexdigest()


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_bounded_regular_file(
    path: pathlib.Path, *, maximum_bytes: int, deadline_monotonic: float | None = None,
) -> bytes:
    if deadline_monotonic is not None:
        _remaining(deadline_monotonic)
    try:
        before = path.lstat()
    except OSError as exc:
        raise CacheSourceUnavailable("cache source retained archive is unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
        or before.st_size < 1 or before.st_size > maximum_bytes
    ):
        raise CacheSourceError("cache source retained archive is not a bounded regular file")
    chunks: list[bytes] = []
    observed = 0
    try:
        with path.open("rb") as handle:
            while True:
                if deadline_monotonic is not None:
                    _remaining(deadline_monotonic)
                chunk = handle.read(min(1024 * 1024, before.st_size + 1 - observed))
                if not chunk:
                    break
                observed += len(chunk)
                if observed > before.st_size:
                    raise CacheSourceError("cache source retained archive grew while reading")
                chunks.append(chunk)
        after = path.lstat()
    except CacheSourceError:
        raise
    except OSError as exc:
        raise CacheSourceUnavailable("cache source retained archive could not be read") from exc
    if (
        observed != before.st_size
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    ):
        raise CacheSourceError("cache source retained archive changed while reading")
    if deadline_monotonic is not None:
        _remaining(deadline_monotonic)
    return b"".join(chunks)


def write_bounded_regular_file(
    path: pathlib.Path, raw: bytes, *, maximum_bytes: int,
    deadline_monotonic: float | None = None,
) -> None:
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum_bytes:
        raise CacheSourceError("cache source retained archive exceeds its byte bound")
    if deadline_monotonic is not None:
        _remaining(deadline_monotonic)
    if path.exists() or path.is_symlink():
        raise CacheSourceError("cache source retained archive path already exists")
    try:
        with path.open("xb") as handle:
            for offset in range(0, len(raw), 1024 * 1024):
                if deadline_monotonic is not None:
                    _remaining(deadline_monotonic)
                handle.write(raw[offset:offset + 1024 * 1024])
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if deadline_monotonic is not None:
        _remaining(deadline_monotonic)


def parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CacheSourceError(f"{label} is not an RFC3339 UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise CacheSourceError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise CacheSourceError(f"{label} is not UTC")
    return parsed


def strict_json_bytes(raw: bytes, *, label: str, maximum_bytes: int) -> Any:
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum_bytes:
        raise CacheSourceError(f"{label} exceeds its byte bound")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-JSON numeric constant")

    try:
        text = raw.decode("utf-8", errors="strict")
        if text.startswith("\ufeff"):
            raise ValueError("UTF-8 BOM is not permitted")
        return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CacheSourceError(f"{label} is not strict UTF-8 JSON") from exc


def load_strict_json(path: pathlib.Path, *, label: str, maximum_bytes: int) -> Any:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CacheSourceError(f"{label} is not a regular file")
        if metadata.st_size < 1 or metadata.st_size > maximum_bytes:
            raise CacheSourceError(f"{label} exceeds its byte bound")
        raw = path.read_bytes()
    except CacheSourceError:
        raise
    except OSError as exc:
        raise CacheSourceError(f"{label} is unavailable") from exc
    if len(raw) != metadata.st_size:
        raise CacheSourceError(f"{label} changed while reading")
    return strict_json_bytes(raw, label=label, maximum_bytes=maximum_bytes)


def _load_handoff() -> Any:
    global _HANDOFF
    module_name = "upstream_catalogue_cache_source_handoff"
    helper_module = sys.modules.get(__name__)
    helper_sha = globals().get("__datapan_cache_source_helper_sha256__")
    helper_operation = globals().get("__datapan_cache_source_helper_operation_id__")
    native_markers = (
        "__datapan_cache_source_helper_sha256__",
        "__datapan_cache_source_helper_operation_id__",
        "__datapan_cache_source_snapshot_proxy__",
        "__datapan_cache_source_deadline_monotonic__",
        "__datapan_cache_source_handoff_sha256__",
        "__datapan_cache_source_handoff_operation_id__",
        "__datapan_cache_source_handoff_path__",
        "__datapan_cache_source_handoff_head_sha__",
    )
    if any(name in globals() for name in native_markers):
        handoff_markers = {
            "__datapan_cache_source_handoff_sha256__": globals().get(
                "__datapan_cache_source_handoff_sha256__"
            ),
            "__datapan_cache_source_handoff_operation_id__": globals().get(
                "__datapan_cache_source_handoff_operation_id__"
            ),
            "__datapan_cache_source_handoff_path__": globals().get(
                "__datapan_cache_source_handoff_path__"
            ),
            "__datapan_cache_source_handoff_head_sha__": globals().get(
                "__datapan_cache_source_handoff_head_sha__"
            ),
        }
        snapshot_proxy = globals().get("__datapan_cache_source_snapshot_proxy__")
        deadline_monotonic = globals().get("__datapan_cache_source_deadline_monotonic__")
        native_module = sys.modules.get("canonical_publication_native")
        if (
            type(helper_module) is not types.ModuleType
            or helper_module.__name__ != __name__
            or helper_module.__dict__ is not globals()
            or sys.modules.get(__name__) is not helper_module
            or not isinstance(helper_sha, str)
            or HEX64.fullmatch(helper_sha) is None
            or not isinstance(helper_operation, str)
            or not helper_operation
            or len(helper_operation) > 256
            or snapshot_proxy is None
            or type(deadline_monotonic) is not float
            or not math.isfinite(deadline_monotonic)
            or type(native_module) is not types.ModuleType
            or native_module.__name__ != "canonical_publication_native"
            or sys.modules.get("canonical_publication_native") is not native_module
        ):
            raise CacheSourceError("collector handoff authenticated helper binding is invalid")
        require_handoff = native_module.__dict__.get(
            "require_verified_cache_source_handoff_binding"
        )
        if (
            type(require_handoff) is not types.FunctionType
            or require_handoff.__module__ != native_module.__name__
            or require_handoff.__globals__ is not native_module.__dict__
            or native_module.__dict__.get(
                "require_verified_cache_source_handoff_binding"
            ) is not require_handoff
        ):
            raise CacheSourceUnavailable("collector handoff Native validator is unavailable")
        try:
            binding = require_handoff(
                helper_module=helper_module,
                helper_snapshot_proxy=snapshot_proxy,
                deadline_monotonic=float(deadline_monotonic),
            )
        except Exception as exc:
            raise CacheSourceUnavailable(
                "collector handoff Native binding is unavailable"
            ) from exc
        handoff = sys.modules.get(module_name)
        if type(binding) is tuple and len(binding) == 2:
            evidence, executable = binding
        else:
            evidence, executable = None, None
        if (
            type(evidence) is not tuple
            or len(evidence) != 4
            or not isinstance(evidence[0], str)
            or REPOSITORY_RE.fullmatch(evidence[0]) is None
            or not isinstance(evidence[1], str)
            or HEX40.fullmatch(evidence[1]) is None
            or evidence[2] != MODERN_GENERATOR_INPUTS["collector_handoff_helper_sha256"]
            or not isinstance(evidence[3], str)
            or HEX64.fullmatch(evidence[3]) is None
            or type(executable) is not tuple
            or len(executable) != 3
            or executable[0] != MODERN_GENERATOR_INPUTS["collector_handoff_helper_sha256"]
            or not isinstance(executable[1], str)
            or HEX64.fullmatch(executable[1]) is None
            or executable[2] != helper_operation
            or type(handoff) is not types.ModuleType
            or handoff.__name__ != module_name
            or sys.modules.get(module_name) is not handoff
            or handoff_markers["__datapan_cache_source_handoff_sha256__"] != executable[1]
            or handoff_markers["__datapan_cache_source_handoff_operation_id__"] != executable[2]
            or handoff_markers["__datapan_cache_source_handoff_path__"] != executable[0]
            or handoff_markers["__datapan_cache_source_handoff_head_sha__"] != evidence[1]
            or not isinstance(handoff_markers["__datapan_cache_source_handoff_sha256__"], str)
            or HEX64.fullmatch(handoff_markers["__datapan_cache_source_handoff_sha256__"]) is None
            or not isinstance(handoff_markers["__datapan_cache_source_handoff_head_sha__"], str)
            or HEX40.fullmatch(handoff_markers["__datapan_cache_source_handoff_head_sha__"]) is None
        ):
            raise CacheSourceError("collector handoff authenticated module binding is invalid")
        if any(
            handoff.__dict__.get(name) != expected
            for name, expected in handoff_markers.items()
        ):
            raise CacheSourceError("collector handoff authenticated module binding is invalid")
        return handoff
    if _HANDOFF is not None:
        return _HANDOFF
    path = pathlib.Path(__file__).with_name("upstream_catalogue_handoff.py")
    spec = importlib.util.spec_from_file_location("upstream_catalogue_cache_source_handoff", path)
    if spec is None or spec.loader is None:
        raise CacheSourceError("collector handoff validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _HANDOFF = module
    return module


def validate_context(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "owner_generation_id", "source_id", "source_scope",
        "original_admission_ref", "candidate",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise CacheSourceError("cache source context shape is invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise CacheSourceError("cache source context version is invalid")
    owner_generation_id = value.get("owner_generation_id")
    if not isinstance(owner_generation_id, str) or HEX64.fullmatch(owner_generation_id) is None:
        raise CacheSourceError("cache source owner generation is invalid")
    for field in ("source_id", "source_scope"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise CacheSourceError(f"cache source {field} is invalid")
    candidate = value.get("candidate")
    if (
        not isinstance(candidate, Mapping)
        or set(candidate) != {"path", "bytes", "sha256"}
        or candidate.get("path") != ORIGINAL_CANDIDATE_MEMBER
        or isinstance(candidate.get("bytes"), bool)
        or not isinstance(candidate.get("bytes"), int)
        or not 0 < candidate["bytes"] <= MAX_CANDIDATE_BYTES
        or not isinstance(candidate.get("sha256"), str)
        or HEX64.fullmatch(candidate["sha256"]) is None
    ):
        raise CacheSourceError("cache source candidate locator is invalid")
    try:
        admission = _load_handoff().validate_admission_row(value.get("original_admission_ref"))
    except Exception as exc:
        raise CacheSourceError("cache source original admission is invalid") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "owner_generation_id": owner_generation_id,
        "source_id": value["source_id"],
        "source_scope": value["source_scope"],
        "original_admission_ref": admission,
        "candidate": dict(candidate),
    }


def build_context(
    *, owner_generation_id: str, source_id: str, source_scope: str,
    original_admission_ref: Mapping[str, Any], candidate_path: pathlib.Path,
) -> dict[str, Any]:
    if candidate_path.is_symlink() or not candidate_path.is_file():
        raise CacheSourceError("cache source candidate is not a regular file")
    value = {
        "schema_version": SCHEMA_VERSION,
        "owner_generation_id": owner_generation_id,
        "source_id": source_id,
        "source_scope": source_scope,
        "original_admission_ref": dict(original_admission_ref),
        "candidate": {
            "path": ORIGINAL_CANDIDATE_MEMBER,
            "bytes": candidate_path.stat().st_size,
            "sha256": file_sha256(candidate_path),
        },
    }
    return validate_context(value)


def write_context_bundle(
    output_dir: pathlib.Path, *, candidate_path: pathlib.Path,
    owner_generation_id: str, source_id: str, source_scope: str,
    original_admission_ref: Mapping[str, Any],
) -> tuple[pathlib.Path, pathlib.Path, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_target = output_dir / ORIGINAL_CANDIDATE_MEMBER
    context_target = output_dir / CONTEXT_MEMBER
    if candidate_path.is_symlink() or not candidate_path.is_file():
        raise CacheSourceError("cache source candidate is unavailable")
    size = candidate_path.stat().st_size
    if size < 1 or size > MAX_CANDIDATE_BYTES:
        raise CacheSourceError("cache source candidate exceeds its byte bound")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{candidate_target.name}.", dir=output_dir)
    temporary = pathlib.Path(temporary_name)
    try:
        digest = hashlib.sha256()
        copied = 0
        with candidate_path.open("rb") as source, os.fdopen(fd, "wb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                copied += len(chunk)
                if copied > MAX_CANDIDATE_BYTES:
                    raise CacheSourceError("cache source candidate exceeds its byte bound")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if copied != size:
            raise CacheSourceError("cache source candidate changed while copying")
        temporary.replace(candidate_target)
    finally:
        temporary.unlink(missing_ok=True)
    context = build_context(
        owner_generation_id=owner_generation_id,
        source_id=source_id,
        source_scope=source_scope,
        original_admission_ref=original_admission_ref,
        candidate_path=candidate_target,
    )
    raw = canonical_json(context) + b"\n"
    if len(raw) > MAX_CONTEXT_BYTES:
        raise CacheSourceError("cache source context exceeds its byte bound")
    context_target.write_bytes(raw)
    return candidate_target, context_target, context


class HistoricalCacheSource:
    """Opaque, exact-type capability over one selected owner's original rows."""

    __slots__ = (
        "_owner_generation_id", "_candidate_sha256", "_admission", "_rows",
        "_provenance", "_token",
    )

    def __init__(
        self, token: object, *, owner_generation_id: str, candidate_sha256: str,
        admission: Mapping[str, Any], rows: Mapping[str, Mapping[str, Any]], provenance: str,
    ) -> None:
        if token is not _CAPABILITY:
            raise TypeError("HistoricalCacheSource cannot be caller-constructed")
        self._owner_generation_id = owner_generation_id
        self._candidate_sha256 = candidate_sha256
        self._admission = types.MappingProxyType(copy.deepcopy(dict(admission)))
        self._rows = types.MappingProxyType(dict(rows))
        self._provenance = provenance
        self._token = token

    @property
    def owner_generation_id(self) -> str:
        return self._owner_generation_id

    @property
    def candidate_sha256(self) -> str:
        return self._candidate_sha256

    @property
    def provenance(self) -> str:
        return self._provenance

    @property
    def original_admission_ref(self) -> Mapping[str, Any]:
        return self._admission

    def row(self, identity: str) -> dict[str, Any] | None:
        value = self._rows.get(identity)
        return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else None


class AuthenticatedCacheContract:
    """Opaque native/source authentication for one selected B owner."""

    __slots__ = (
        "_owner_generation_id", "_owner_checkpoint_sha256",
        "_bundle_manifest_sha256", "_artifact_id", "_profile",
        "_producer_head_sha", "_token",
    )

    def __init__(
        self, token: object, *, owner_generation_id: str, profile: str,
        producer_head_sha: str, owner_checkpoint_sha256: str,
        bundle_manifest_sha256: str, artifact_id: str,
    ) -> None:
        if token is not _CAPABILITY:
            raise TypeError("AuthenticatedCacheContract cannot be caller-constructed")
        self._owner_generation_id = owner_generation_id
        self._owner_checkpoint_sha256 = owner_checkpoint_sha256
        self._bundle_manifest_sha256 = bundle_manifest_sha256
        self._artifact_id = artifact_id
        self._profile = profile
        self._producer_head_sha = producer_head_sha
        self._token = token

    @property
    def owner_generation_id(self) -> str:
        return self._owner_generation_id

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def owner_checkpoint_sha256(self) -> str:
        return self._owner_checkpoint_sha256

    @property
    def bundle_manifest_sha256(self) -> str:
        return self._bundle_manifest_sha256

    @property
    def artifact_id(self) -> str:
        return self._artifact_id

    @property
    def producer_head_sha(self) -> str:
        return self._producer_head_sha


def _checkpoint_input_binding(
    owner: Mapping[str, Any], admission: Mapping[str, Any], candidate_sha256: str,
) -> None:
    if (
        owner.get("source_id") is None
        or not isinstance(owner.get("generation_inputs"), Mapping)
        or owner["generation_inputs"].get("candidate_sha256") != candidate_sha256
    ):
        raise CacheSourceError("cache source owner input identity is invalid")
    refs = owner.get("input_artifacts")
    if not isinstance(refs, list):
        raise CacheSourceError("cache source owner input artifacts are missing")
    matches = [
        row for row in refs if isinstance(row, Mapping)
        and str(row.get("run_id")) == str(admission.get("producer_run_id"))
        and str(row.get("artifact_id")) == str(admission.get("artifact_id"))
        and row.get("name") == admission.get("artifact_name")
        and row.get("expires_at") == admission.get("artifact_expires_at")
        and row.get("candidate_sha256") == admission.get("candidate_sha256") == candidate_sha256
        and row.get("evidence_sha256") == admission.get("refresh_evidence_sha256")
    ]
    if len(matches) != 1:
        raise CacheSourceError("cache source original admission is not bound to owner inputs")
    observation = owner.get("last_observation")
    if (
        not isinstance(observation, Mapping)
        or str(observation.get("producer_run_id")) != str(admission.get("producer_run_id"))
        or observation.get("refresh_evidence_sha256") != admission.get("refresh_evidence_sha256")
        or observation.get("observed_at") != admission.get("observed_at")
    ):
        raise CacheSourceError("cache source original observation is not bound to owner")


def _ledger_contains_admission(index: Mapping[str, Any], admission: Mapping[str, Any]) -> None:
    ledger = index.get("collector_handoff")
    try:
        validated = _load_handoff().validate_ledger(ledger)
    except Exception as exc:
        raise CacheSourceError("cache source collector handoff ledger is invalid") from exc
    matches = [row for row in validated["admitted_observations"] if row == admission]
    if len(matches) != 1:
        raise CacheSourceError("cache source original admission is not in the authenticated ledger")


def validate_bound_context_bundle(
    *, context_path: pathlib.Path, owner_checkpoint: Mapping[str, Any],
    state_index: Mapping[str, Any] | None = None,
    owner_contract: AuthenticatedCacheContract | None = None,
) -> tuple[dict[str, Any], pathlib.Path, str]:
    context = validate_context(load_strict_json(
        context_path, label="cache source context", maximum_bytes=MAX_CONTEXT_BYTES,
    ))
    if (
        context_path.name != CONTEXT_MEMBER
        or context["owner_generation_id"] != owner_checkpoint.get("generation_id")
        or context["source_id"] != owner_checkpoint.get("source_id")
        or context["source_scope"] != owner_checkpoint.get("source_scope")
    ):
        raise CacheSourceError("cache source context does not belong to selected owner")
    candidate_path = context_path.with_name(context["candidate"]["path"])
    if candidate_path.is_symlink() or not candidate_path.is_file():
        raise CacheSourceError("cache source candidate is missing")
    if (
        candidate_path.stat().st_size != context["candidate"]["bytes"]
        or file_sha256(candidate_path) != context["candidate"]["sha256"]
    ):
        raise CacheSourceError("cache source candidate differs from its descriptor")
    admission = context["original_admission_ref"]
    _checkpoint_input_binding(owner_checkpoint, admission, context["candidate"]["sha256"])
    if state_index is not None:
        _ledger_contains_admission(state_index, admission)
    outputs = owner_checkpoint.get("output_digests")
    inputs = owner_checkpoint.get("generation_inputs")
    if not isinstance(outputs, list) or not isinstance(inputs, Mapping):
        raise CacheSourceError("cache source owner output inventory is missing")
    entries = {row.get("path"): row for row in outputs if isinstance(row, Mapping)}
    candidate_entry = entries.get(ORIGINAL_CANDIDATE_MEMBER)
    context_entry = entries.get(CONTEXT_MEMBER)
    profile = inputs.get("cache_source_profile")
    locator = owner_checkpoint.get("output_artifact")
    contract_matches_owner = (
        type(owner_contract) is AuthenticatedCacheContract
        and owner_contract._token is _CAPABILITY
        and owner_contract.owner_generation_id == owner_checkpoint.get("generation_id")
        and owner_contract.owner_checkpoint_sha256 == owner_checkpoint.get("checkpoint_sha256")
        and isinstance(locator, Mapping)
        and owner_contract.bundle_manifest_sha256 == locator.get("bundle_manifest_sha256")
        and owner_contract.artifact_id == str(locator.get("artifact_id") or "")
    )
    if profile == CACHE_SOURCE_PROFILE:
        if candidate_entry is None or context_entry is None:
            raise CacheSourceError("modern cache source bundle is incomplete")
        if (
            not contract_matches_owner
            or owner_contract.profile != CACHE_SOURCE_PROFILE
        ):
            raise CacheSourceError("modern cache source producer contract is unauthenticated")
        provenance = "modern"
    elif profile is None:
        if candidate_entry is not None or context_entry is not None:
            raise CacheSourceError("legacy cache source bundle carries an unsupported modern inventory")
        if (
            not contract_matches_owner
            or owner_contract.profile != "legacy-two-file-v1"
        ):
            raise CacheSourceError("legacy cache source producer contract is unauthenticated")
        provenance = "legacy"
    else:
        raise CacheSourceUnavailable("cache source contract profile is unsupported")
    if provenance == "modern":
        for path, entry in ((candidate_path, candidate_entry), (context_path, context_entry)):
            if (
                not isinstance(entry, Mapping)
                or entry.get("bytes") != path.stat().st_size
                or entry.get("sha256") != file_sha256(path)
            ):
                raise CacheSourceError("modern cache source member differs from owner output inventory")
    return context, candidate_path, provenance


def load_historical_cache_source(
    context_path: pathlib.Path, *, owner_checkpoint: Mapping[str, Any],
    state_index: Mapping[str, Any], owner_contract: AuthenticatedCacheContract | None = None,
) -> HistoricalCacheSource:
    context, candidate_path, provenance = validate_bound_context_bundle(
        context_path=context_path, owner_checkpoint=owner_checkpoint, state_index=state_index,
        owner_contract=owner_contract,
    )
    value = load_strict_json(
        candidate_path, label="cache source original candidate", maximum_bytes=MAX_CANDIDATE_BYTES,
    )
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise CacheSourceError("cache source original candidate is not an object array")
    rows: dict[str, Mapping[str, Any]] = {}
    for row in value:
        identity = str(row.get("id") or row.get("dataset_id") or "").strip()
        if not identity or identity in rows:
            raise CacheSourceError("cache source original candidate identity is missing or duplicated")
        rows[identity] = dict(row)
    return HistoricalCacheSource(
        _CAPABILITY,
        owner_generation_id=context["owner_generation_id"],
        candidate_sha256=context["candidate"]["sha256"],
        admission=context["original_admission_ref"],
        rows=rows,
        provenance=provenance,
    )


def _remaining(deadline: float) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(float(deadline)):
        raise CacheSourceError("cache source deadline is not finite")
    remaining = float(deadline) - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise CacheSourceUnavailable("cache source native deadline expired")
    return remaining


def _process_group_alive(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        raise CacheSourceUnavailable("cache source process group liveness is unavailable") from exc
    return True


def _work_deadline(deadline: float) -> float:
    """Reserve a finite tail of one absolute deadline for owned-child cleanup."""
    remaining = _remaining(deadline)
    reserve = min(CLEANUP_RESERVE_SECONDS, remaining / 2)
    work_deadline = float(deadline) - reserve
    _remaining(work_deadline)
    return work_deadline


def _terminate_process_group(
    process: subprocess.Popen[bytes], *, deadline_monotonic: float,
) -> None:
    """Contain one owned process group without extending its absolute deadline."""
    errors: list[BaseException] = []

    def send(sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        except OSError as exc:
            errors.append(exc)
            try:
                process.terminate() if sig == signal.SIGTERM else process.kill()
            except OSError as fallback_exc:
                errors.append(fallback_exc)

    def reap() -> None:
        timeout = max(
            0.0,
            min(TERMINATE_GRACE_SECONDS, float(deadline_monotonic) - time.monotonic()),
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return
        except BaseException as exc:
            errors.append(exc)

    # Liveness observations are evidence only.  They must never suppress the
    # unconditional escalation from TERM to KILL.
    send(signal.SIGTERM)
    reap()
    try:
        _process_group_alive(process)
    except BaseException as exc:
        errors.append(exc)
    send(signal.SIGKILL)
    reap()

    contained = False
    while True:
        try:
            contained = not _process_group_alive(process)
        except BaseException as exc:
            errors.append(exc)
            break
        if contained or time.monotonic() >= float(deadline_monotonic):
            break
        time.sleep(min(0.01, max(0.0, float(deadline_monotonic) - time.monotonic())))
    try:
        leader_reaped = process.poll() is not None
    except BaseException as exc:
        errors.append(exc)
        leader_reaped = False
    if not leader_reaped:
        errors.append(CacheSourceUnavailable("cache source native child could not be reaped"))
    if not contained:
        errors.append(CacheSourceUnavailable("cache source native process group could not be contained"))
    if errors:
        details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
        raise CacheSourceUnavailable(f"cache source native cleanup is uncertain: {details}") from errors[0]


class GitHubNativeReader:
    """One bounded gh-backed reader sharing a whole-attempt deadline."""

    def __init__(self, *, cwd: pathlib.Path, deadline_monotonic: float) -> None:
        remaining = _remaining(deadline_monotonic)
        if remaining > MAX_WHOLE_ATTEMPT_SECONDS:
            raise CacheSourceError("cache source deadline exceeds the whole-attempt bound")
        self.cwd = cwd
        self.deadline = float(deadline_monotonic)

    def _run(self, endpoint: str, *, maximum_bytes: int) -> bytes:
        if (
            not isinstance(endpoint, str) or not endpoint.startswith("repos/") or "\n" in endpoint
            or isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int)
            or maximum_bytes < 1 or maximum_bytes > MAX_CACHE_ARCHIVE_BYTES
        ):
            raise CacheSourceError("cache source native request is invalid")
        work_deadline = _work_deadline(self.deadline)
        argv = (
            "gh", "api", "--header", "X-GitHub-Api-Version: 2026-03-10",
            "--header", "Accept: application/vnd.github+json", endpoint,
        )
        try:
            process = subprocess.Popen(
                argv, cwd=self.cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise CacheSourceUnavailable("cache source native process could not start") from exc
        selector: selectors.BaseSelector | None = None
        streams: dict[Any, str] = {}
        completed = False
        active: BaseException | None = None
        try:
            if process.stdout is None or process.stderr is None:
                raise CacheSourceUnavailable("cache source native pipes are unavailable")
            streams = {process.stdout: "stdout", process.stderr: "stderr"}
            selector = selectors.DefaultSelector()
            limits = {"stdout": maximum_bytes, "stderr": MAX_NATIVE_STDERR_BYTES}
            chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
            counts = {"stdout": 0, "stderr": 0}
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while True:
                remaining = _remaining(work_deadline)
                returncode = process.poll()
                if returncode is not None and returncode != 0:
                    raise CacheSourceUnavailable(
                        f"cache source native read failed ({returncode}): {shlex.join(argv[:2])}"
                    )
                if not selector.get_map() and returncode is not None and not _process_group_alive(process):
                    break
                events = selector.select(min(remaining, POLL_SECONDS)) if selector.get_map() else []
                if not selector.get_map():
                    time.sleep(min(remaining, POLL_SECONDS))
                for key, _events in events:
                    stream = key.fileobj
                    label = streams[stream]
                    available = limits[label] + 1 - counts[label]
                    if available < 1:
                        raise CacheSourceError(f"cache source native {label} exceeds its byte bound")
                    try:
                        chunk = os.read(stream.fileno(), min(65536, available))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    counts[label] += len(chunk)
                    if counts[label] > limits[label]:
                        raise CacheSourceError(f"cache source native {label} exceeds its byte bound")
                    chunks[label].append(chunk)
            process.wait(timeout=_remaining(work_deadline))
            completed = True
            return b"".join(chunks["stdout"])
        except BaseException as exc:
            active = exc
            raise
        finally:
            errors: list[BaseException] = []
            if not completed:
                try:
                    _terminate_process_group(process, deadline_monotonic=self.deadline)
                except BaseException as exc:
                    errors.append(exc)
            if selector is not None:
                try:
                    selector.close()
                except BaseException as exc:
                    errors.append(exc)
            for stream in streams:
                try:
                    stream.close()
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
                if active is not None:
                    active.add_note(f"cache source native cleanup failure: {details}")
                else:
                    raise CacheSourceUnavailable(f"cache source native cleanup failed: {details}") from errors[0]

    def json(self, endpoint: str) -> Any:
        raw = self._run(endpoint, maximum_bytes=MAX_NATIVE_JSON_BYTES)
        return strict_json_bytes(raw, label="cache source native response", maximum_bytes=MAX_NATIVE_JSON_BYTES)

    def archive(self, repository: str, artifact_id: str, *, expected_bytes: int) -> bytes:
        if expected_bytes < 1 or expected_bytes > MAX_CACHE_ARCHIVE_BYTES:
            raise CacheSourceUnavailable("cache source native archive size is unavailable")
        raw = self._run(
            f"repos/{repository}/actions/artifacts/{artifact_id}/zip",
            maximum_bytes=expected_bytes,
        )
        if len(raw) != expected_bytes:
            raise CacheSourceUnavailable("cache source native archive byte count changed")
        return raw

    def source_file(
        self, repository: str, head_sha: str, path: str, *, maximum_bytes: int,
    ) -> bytes:
        if (
            REPOSITORY_RE.fullmatch(repository) is None
            or HEX40.fullmatch(head_sha) is None
            or maximum_bytes < 1 or maximum_bytes > MAX_SOURCE_FILE_BYTES
        ):
            raise CacheSourceError("cache source historical source locator is invalid")
        relative = pathlib.PurePosixPath(path)
        if relative.is_absolute() or len(relative.parts) < 2 or ".." in relative.parts or "\\" in path:
            raise CacheSourceError("cache source historical source path is invalid")
        endpoint = (
            f"repos/{repository}/contents/{urllib.parse.quote(path, safe='/')}"
            f"?ref={head_sha}"
        )
        value = self.json(endpoint)
        if (
            not isinstance(value, Mapping)
            or value.get("type") != "file"
            or value.get("path") != path
            or value.get("name") != relative.name
            or value.get("encoding") != "base64"
            or isinstance(value.get("size"), bool)
            or not isinstance(value.get("size"), int)
            or not 0 < value["size"] <= maximum_bytes
            or not isinstance(value.get("sha"), str)
            or HEX40.fullmatch(value["sha"]) is None
            or not isinstance(value.get("content"), str)
        ):
            raise CacheSourceError("cache source historical source response is invalid")
        try:
            encoded = value["content"].encode("ascii")
            if any(byte in encoded for byte in (b" ", b"\t")):
                raise ValueError("base64 source contains horizontal whitespace")
            compact = encoded.replace(b"\r", b"").replace(b"\n", b"")
            raw = base64.b64decode(compact, validate=True)
        except (UnicodeEncodeError, ValueError, TypeError) as exc:
            raise CacheSourceError("cache source historical source encoding is invalid") from exc
        if len(raw) != value["size"] or git_blob_sha1(raw) != value["sha"]:
            raise CacheSourceError("cache source historical source bytes are invalid")
        return raw


class LocalGitSourceReader:
    """Bounded, no-lazy local Git reader for already captured native evidence."""

    def __init__(self, *, cwd: pathlib.Path, deadline_monotonic: float) -> None:
        remaining = _remaining(deadline_monotonic)
        if remaining > MAX_WHOLE_ATTEMPT_SECONDS:
            raise CacheSourceError("cache source deadline exceeds the whole-attempt bound")
        self.cwd = cwd
        self.deadline = float(deadline_monotonic)
        self._cache: dict[tuple[str, str, str], bytes] = {}
        self._cached_bytes = 0
        self._native_binding: object | None = None
        self._native_module: types.ModuleType | None = None
        self._native_helper_module: types.ModuleType | None = None
        self._native_snapshot: Any | None = None

    @classmethod
    def from_native_snapshot(
        cls, snapshot: Any, *, deadline_monotonic: float,
    ) -> LocalGitSourceReader:
        """Bind the exact authenticated CONTROL snapshot without local Git access."""
        if cls is not LocalGitSourceReader:
            raise CacheSourceError("cache source native reader subclass is not permitted")
        remaining = _remaining(deadline_monotonic)
        if remaining > MAX_NATIVE_OPERATION_SECONDS:
            raise CacheSourceError("cache source native deadline exceeds its operation bound")
        helper_module = sys.modules.get(__name__)
        if (
            type(helper_module) is not types.ModuleType
            or helper_module.__name__ != __name__
            or sys.modules.get(__name__) is not helper_module
            or helper_module.__dict__.get("LocalGitSourceReader") is not cls
        ):
            raise CacheSourceError("cache source helper module is not the operation singleton")
        native_module = sys.modules.get("canonical_publication_native")
        if (
            type(native_module) is not types.ModuleType
            or native_module.__name__ != "canonical_publication_native"
            or sys.modules.get("canonical_publication_native") is not native_module
        ):
            raise CacheSourceUnavailable("canonical publication Native singleton is unavailable")
        require_snapshot = native_module.__dict__.get("require_verified_cache_source_snapshot")
        read_snapshot_file = native_module.__dict__.get("read_verified_cache_source_file")
        for function, name in (
            (require_snapshot, "require_verified_cache_source_snapshot"),
            (read_snapshot_file, "read_verified_cache_source_file"),
        ):
            if (
                type(function) is not types.FunctionType
                or function.__module__ != native_module.__name__
                or function.__globals__ is not native_module.__dict__
                or native_module.__dict__.get(name) is not function
            ):
                raise CacheSourceUnavailable("canonical publication Native cache-source API is unavailable")
        try:
            verified = require_snapshot(
                snapshot, helper_module=helper_module,
                deadline_monotonic=float(deadline_monotonic),
            )
        except Exception as exc:
            raise CacheSourceUnavailable("cache source Native snapshot is unavailable") from exc
        if verified is not snapshot:
            raise CacheSourceError("cache source Native snapshot identity changed during validation")
        reader = cls.__new__(cls)
        reader.cwd = None
        reader.deadline = float(deadline_monotonic)
        reader._cache = {}
        reader._cached_bytes = 0
        reader._native_binding = _NATIVE_READER_CAPABILITY
        reader._native_module = native_module
        reader._native_helper_module = helper_module
        reader._native_snapshot = snapshot
        return reader

    def _require_native_snapshot(self) -> None:
        native_module = sys.modules.get("canonical_publication_native")
        helper_module = sys.modules.get(__name__)
        if (
            self._native_binding is not _NATIVE_READER_CAPABILITY
            or self._native_snapshot is None
            or type(native_module) is not types.ModuleType
            or native_module is not self._native_module
            or native_module.__name__ != "canonical_publication_native"
            or sys.modules.get("canonical_publication_native") is not native_module
            or type(helper_module) is not types.ModuleType
            or helper_module is not self._native_helper_module
            or helper_module.__dict__.get("LocalGitSourceReader") is not LocalGitSourceReader
        ):
            raise CacheSourceError("cache source Native snapshot binding is invalid")
        require_snapshot = native_module.__dict__.get("require_verified_cache_source_snapshot")
        if (
            type(require_snapshot) is not types.FunctionType
            or require_snapshot.__module__ != native_module.__name__
            or require_snapshot.__globals__ is not native_module.__dict__
        ):
            raise CacheSourceError("cache source Native snapshot validator is unavailable")
        try:
            verified = require_snapshot(
                self._native_snapshot,
                helper_module=self._native_helper_module,
                deadline_monotonic=self.deadline,
            )
        except Exception as exc:
            raise CacheSourceUnavailable("cache source Native snapshot is unavailable") from exc
        if verified is not self._native_snapshot:
            raise CacheSourceError("cache source Native snapshot identity changed during validation")

    def check_deadline(self) -> None:
        remaining = _remaining(self.deadline)
        native_mode = (
            self._native_binding is not None
            or self._native_module is not None
            or self._native_helper_module is not None
            or self._native_snapshot is not None
            or self.cwd is None
        )
        if native_mode and remaining > MAX_NATIVE_OPERATION_SECONDS:
            raise CacheSourceError("cache source native deadline exceeds its operation bound")
        if native_mode:
            self._require_native_snapshot()

    def source_file(
        self, repository: str, head_sha: str, path: str, *, maximum_bytes: int,
    ) -> bytes:
        if (
            REPOSITORY_RE.fullmatch(repository) is None
            or HEX40.fullmatch(head_sha) is None
            or isinstance(maximum_bytes, bool)
            or not isinstance(maximum_bytes, int)
            or maximum_bytes < 1
            or maximum_bytes > MAX_SOURCE_FILE_BYTES
        ):
            raise CacheSourceError("cache source historical source locator is invalid")
        relative = pathlib.PurePosixPath(path)
        if relative.is_absolute() or len(relative.parts) < 2 or ".." in relative.parts or "\\" in path:
            raise CacheSourceError("cache source historical source path is invalid")
        self.check_deadline()
        key = (repository, head_sha, path)
        cached = self._cache.get(key)
        native_mode = (
            self._native_binding is not None
            or self._native_module is not None
            or self._native_helper_module is not None
            or self._native_snapshot is not None
            or self.cwd is None
        )
        if native_mode:
            self._require_native_snapshot()
            if self._native_module is None or self._native_helper_module is None:
                raise CacheSourceError("cache source Native reader binding is invalid")
            read_snapshot_file = self._native_module.__dict__.get("read_verified_cache_source_file")
            if (
                type(read_snapshot_file) is not types.FunctionType
                or read_snapshot_file.__module__ != self._native_module.__name__
                or read_snapshot_file.__globals__ is not self._native_module.__dict__
            ):
                raise CacheSourceError("cache source Native reader is unavailable")
            try:
                raw = read_snapshot_file(
                    self._native_snapshot,
                    helper_module=self._native_helper_module,
                    repository=repository,
                    head_sha=head_sha,
                    path=path,
                    maximum_bytes=maximum_bytes,
                    deadline_monotonic=self.deadline,
                )
            except Exception as exc:
                raise CacheSourceUnavailable("cache source Native history bytes are unavailable") from exc
            self.check_deadline()
            if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
                raise CacheSourceError("cache source Native history bytes are invalid")
            if cached is not None:
                if cached != raw:
                    raise CacheSourceError("cache source Native history bytes changed within one operation")
                return cached
            if self._cached_bytes + len(raw) > MAX_TOTAL_SOURCE_BYTES:
                raise CacheSourceError("cache source local Git aggregate exceeds its byte bound")
            self._cache[key] = raw
            self._cached_bytes += len(raw)
            return raw
        if cached is not None:
            if len(cached) > maximum_bytes:
                raise CacheSourceError("cache source local Git stdout exceeds its byte bound")
            return cached
        argv = ("git", "--no-pager", "show", f"{head_sha}:{path}")
        environment = os.environ.copy()
        environment.update({
            "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat", "PAGER": "cat", "LC_ALL": "C",
        })
        work_deadline = _work_deadline(self.deadline)
        try:
            process = subprocess.Popen(
                argv, cwd=self.cwd, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise CacheSourceUnavailable("cache source local Git process could not start") from exc
        selector: selectors.BaseSelector | None = None
        streams: dict[Any, str] = {}
        completed = False
        active: BaseException | None = None
        try:
            if process.stdout is None or process.stderr is None:
                raise CacheSourceUnavailable("cache source local Git pipes are unavailable")
            streams = {process.stdout: "stdout", process.stderr: "stderr"}
            selector = selectors.DefaultSelector()
            limits = {"stdout": maximum_bytes, "stderr": MAX_NATIVE_STDERR_BYTES}
            chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
            counts = {"stdout": 0, "stderr": 0}
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while True:
                remaining = _remaining(work_deadline)
                returncode = process.poll()
                if returncode is not None and returncode != 0:
                    raise CacheSourceUnavailable(
                        f"cache source local Git read failed ({returncode}): {path}"
                    )
                if not selector.get_map() and returncode is not None and not _process_group_alive(process):
                    break
                events = selector.select(min(remaining, POLL_SECONDS)) if selector.get_map() else []
                if not selector.get_map():
                    time.sleep(min(remaining, POLL_SECONDS))
                for key, _events in events:
                    stream = key.fileobj
                    label = streams[stream]
                    available = limits[label] + 1 - counts[label]
                    if available < 1:
                        raise CacheSourceError(f"cache source local Git {label} exceeds its byte bound")
                    try:
                        chunk = os.read(stream.fileno(), min(65536, available))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    counts[label] += len(chunk)
                    if counts[label] > limits[label]:
                        raise CacheSourceError(f"cache source local Git {label} exceeds its byte bound")
                    chunks[label].append(chunk)
            process.wait(timeout=_remaining(work_deadline))
            completed = True
            raw = b"".join(chunks["stdout"])
            if not raw:
                raise CacheSourceError("cache source local Git source is empty")
            if self._cached_bytes + len(raw) > MAX_TOTAL_SOURCE_BYTES:
                raise CacheSourceError("cache source local Git aggregate exceeds its byte bound")
            self._cache[key] = raw
            self._cached_bytes += len(raw)
            return raw
        except BaseException as exc:
            active = exc
            raise
        finally:
            errors: list[BaseException] = []
            if not completed:
                try:
                    _terminate_process_group(process, deadline_monotonic=self.deadline)
                except BaseException as exc:
                    errors.append(exc)
            if selector is not None:
                try:
                    selector.close()
                except BaseException as exc:
                    errors.append(exc)
            for stream in streams:
                try:
                    stream.close()
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
                if active is not None:
                    active.add_note(f"cache source local Git cleanup failure: {details}")
                else:
                    raise CacheSourceUnavailable(
                        f"cache source local Git cleanup failed: {details}"
                    ) from errors[0]

def _safe_extract(
    raw: bytes, destination: pathlib.Path, *, expected: Mapping[str, Mapping[str, Any] | None],
    maximum_archive_bytes: int, maximum_expanded_bytes: int,
    deadline_monotonic: float | None = None,
) -> None:
    if deadline_monotonic is not None:
        _remaining(deadline_monotonic)
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum_archive_bytes:
        raise CacheSourceError("cache source archive exceeds its compressed byte bound")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or set(names) != set(expected):
                raise CacheSourceError("cache source archive member set is invalid")
            total = 0
            for info in infos:
                if deadline_monotonic is not None:
                    _remaining(deadline_monotonic)
                name = info.filename
                path = pathlib.PurePosixPath(name)
                mode = info.external_attr >> 16
                if (
                    path.is_absolute() or len(path.parts) != 1 or ".." in path.parts or "\\" in name
                    or info.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                    or info.flag_bits & 0x1
                    or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or info.file_size < 1 or info.file_size > MAX_CANDIDATE_BYTES
                    or info.compress_size < 1
                ):
                    raise CacheSourceError("cache source archive contains an unsafe member")
                total += info.file_size
                if total > maximum_expanded_bytes:
                    raise CacheSourceError("cache source archive exceeds its expanded byte bound")
                expected_entry = expected[name]
                if expected_entry is not None and (
                    info.file_size != expected_entry.get("bytes")
                    or not isinstance(expected_entry.get("sha256"), str)
                    or HEX64.fullmatch(str(expected_entry.get("sha256"))) is None
                ):
                    raise CacheSourceError("cache source archive metadata differs from owner inventory")
            for info in infos:
                if deadline_monotonic is not None:
                    _remaining(deadline_monotonic)
                target = destination / info.filename
                digest = hashlib.sha256()
                written = 0
                with archive.open(info) as source, target.open("xb") as output:
                    while True:
                        if deadline_monotonic is not None:
                            _remaining(deadline_monotonic)
                        chunk = source.read(min(1024 * 1024, info.file_size + 1 - written))
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size:
                            raise CacheSourceError("cache source archive member exceeds declared size")
                        digest.update(chunk)
                        output.write(chunk)
                expected_entry = expected[info.filename]
                if written != info.file_size or (
                    expected_entry is not None and digest.hexdigest() != expected_entry.get("sha256")
                ):
                    raise CacheSourceError("cache source archive member digest is invalid")
        if deadline_monotonic is not None:
            _remaining(deadline_monotonic)
    except CacheSourceError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, EOFError, ValueError) as exc:
        raise CacheSourceError("cache source archive is invalid") from exc


def _sealed_checkpoint(path: pathlib.Path) -> dict[str, Any]:
    value = load_strict_json(path, label="cache owner checkpoint", maximum_bytes=MAX_STATE_BYTES)
    if not isinstance(value, Mapping):
        raise CacheSourceError("cache owner checkpoint is not an object")
    unsigned = dict(value)
    claimed = unsigned.pop("checkpoint_sha256", None)
    if not isinstance(claimed, str) or sha256_bytes(canonical_json(unsigned)) != claimed:
        raise CacheSourceError("cache owner checkpoint seal is invalid")
    return dict(value)


def select_cache_owner(
    state_dir: pathlib.Path, *, target_generation_id: str | None, now: dt.datetime,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if target_generation_id is not None and HEX64.fullmatch(target_generation_id) is None:
        raise CacheSourceError("cache target generation identity is invalid")
    index_path = state_dir / "sources/data_go_kr/index.json"
    if not index_path.is_file():
        return None
    index = load_strict_json(index_path, label="cache state index", maximum_bytes=MAX_STATE_BYTES)
    if not isinstance(index, Mapping) or not isinstance(index.get("generations"), list):
        raise CacheSourceError("cache state index is invalid")
    _load_handoff().validate_ledger(index.get("collector_handoff"))
    choices: list[dict[str, Any]] = []
    seen_generation_ids: set[str] = set()
    for row in index["generations"]:
        if not isinstance(row, Mapping):
            raise CacheSourceError("cache state generation index is invalid")
        generation_id = row.get("generation_id")
        if not isinstance(generation_id, str) or HEX64.fullmatch(generation_id) is None:
            raise CacheSourceError("cache state generation identity is invalid")
        if generation_id in seen_generation_ids:
            raise CacheSourceError("cache state generation identity is duplicated")
        seen_generation_ids.add(generation_id)
        checkpoint_path = state_dir / "sources/data_go_kr/generations" / f"{generation_id}.json"
        if not checkpoint_path.is_file():
            raise CacheSourceError("cache state generation checkpoint is missing")
        checkpoint = _sealed_checkpoint(checkpoint_path)
        if checkpoint.get("generation_id") != generation_id:
            raise CacheSourceError("cache state generation checkpoint identity is invalid")
        locator = checkpoint.get("output_artifact")
        outputs = checkpoint.get("output_digests")
        if not isinstance(locator, Mapping) or not isinstance(outputs, list):
            continue
        manifest = locator.get("bundle_manifest_sha256")
        evidence = next(
            (item for item in outputs if isinstance(item, Mapping) and item.get("path") == ENRICHMENT_MEMBER),
            None,
        )
        if evidence is None or manifest is None or sha256_bytes(canonical_json(outputs)) != manifest:
            continue
        try:
            expiry = parse_time(locator.get("expires_at"), "cache owner artifact expiry")
        except CacheSourceError:
            raise CacheSourceError("cache owner artifact locator is invalid")
        if expiry <= now:
            continue
        choices.append(checkpoint)
    if not choices:
        return None
    if target_generation_id is not None:
        exact = [row for row in choices if row.get("generation_id") == target_generation_id]
        if len(exact) > 1:
            raise CacheSourceError("cache target generation owner is duplicated")
        # A requested target is an authority boundary.  If its own output is
        # unavailable, an unrelated generation is not a substitute.  The
        # caller may start a fresh claim only after this selected-target lookup
        # reports no eligible cache owner.
        return (exact[0], dict(index)) if exact else None
    choices.sort(
        key=lambda row: (
            row.get("generation_id") == target_generation_id,
            str(row.get("last_progress_at") or ""),
        ),
        reverse=True,
    )
    return choices[0], dict(index)


def _artifact_metadata(
    value: Any, *, run_id: str, artifact_id: str, name: str,
    expires_at: str, default_branch: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CacheSourceError("cache source artifact metadata is invalid")
    workflow_run = value.get("workflow_run")
    if (
        str(value.get("id")) != artifact_id or value.get("name") != name
        or value.get("expired") is not False or value.get("expires_at") != expires_at
        or not isinstance(value.get("size_in_bytes"), int) or isinstance(value.get("size_in_bytes"), bool)
        or not 0 < value["size_in_bytes"] <= MAX_CACHE_ARCHIVE_BYTES
        or not isinstance(value.get("digest"), str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", value["digest"]) is None
        or not isinstance(workflow_run, Mapping) or str(workflow_run.get("id")) != run_id
        or workflow_run.get("head_branch") != default_branch
        or isinstance(workflow_run.get("repository_id"), bool)
        or not isinstance(workflow_run.get("repository_id"), int)
        or workflow_run["repository_id"] < 1
        or isinstance(workflow_run.get("head_repository_id"), bool)
        or not isinstance(workflow_run.get("head_repository_id"), int)
        or workflow_run["head_repository_id"] < 1
    ):
        raise CacheSourceError("cache source artifact no longer matches its owner locator")
    parse_time(value.get("created_at"), "cache source artifact creation")
    parse_time(expires_at, "cache source artifact expiry")
    return dict(value)


def _artifact_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project the fields shared by artifact-list and artifact-detail reads."""
    workflow_run = value.get("workflow_run")
    if not isinstance(workflow_run, Mapping):
        raise CacheSourceError("cache source artifact workflow identity is invalid")
    return {
        "id": str(value.get("id") or ""),
        "name": value.get("name"),
        "size_in_bytes": value.get("size_in_bytes"),
        "digest": value.get("digest"),
        "expired": value.get("expired"),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "expires_at": value.get("expires_at"),
        "workflow_run": {
            "id": str(workflow_run.get("id") or ""),
            "head_sha": workflow_run.get("head_sha"),
            "head_branch": workflow_run.get("head_branch"),
            "repository_id": str(workflow_run.get("repository_id") or ""),
            "head_repository_id": str(workflow_run.get("head_repository_id") or ""),
        },
    }


def _run_attempt(
    value: Any, *, repository: str, run_id: str, attempt: int, default_branch: str,
    expected_head_sha: str | None, workflow_path: str,
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping) or str(value.get("id")) != run_id
        or value.get("run_attempt") != attempt or value.get("path") != workflow_path
        or value.get("head_branch") != default_branch
        or (value.get("repository") or {}).get("full_name") != repository
        or (value.get("head_repository") or {}).get("full_name") != repository
        or value.get("status") != "completed"
        or value.get("conclusion") != "success"
        or value.get("event") not in {"schedule", "workflow_dispatch", "workflow_run"}
        or not isinstance(value.get("head_sha"), str) or HEX40.fullmatch(value["head_sha"]) is None
        or expected_head_sha is not None and value.get("head_sha") != expected_head_sha
    ):
        raise CacheSourceError("cache source native run identity is invalid")
    return dict(value)


def _artifact_run_binding(artifact: Mapping[str, Any], run: Mapping[str, Any]) -> None:
    workflow_run = artifact.get("workflow_run")
    repository = run.get("repository")
    head_repository = run.get("head_repository")
    if (
        not isinstance(workflow_run, Mapping)
        or not isinstance(repository, Mapping)
        or not isinstance(head_repository, Mapping)
        or str(workflow_run.get("id")) != str(run.get("id"))
        or workflow_run.get("head_sha") != run.get("head_sha")
        or workflow_run.get("head_branch") != run.get("head_branch")
        or workflow_run.get("repository_id") != repository.get("id")
        or workflow_run.get("head_repository_id") != head_repository.get("id")
    ):
        raise CacheSourceError("cache source artifact is not bound to its native run")


def _exact_artifact_from_list(value: Any, artifact_id: str, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("artifacts"), list):
        raise CacheSourceError("cache source artifact inventory is invalid")
    matches = [
        row for row in value["artifacts"] if isinstance(row, Mapping)
        and str(row.get("id")) == artifact_id and row.get("name") == name
    ]
    if len(matches) != 1:
        raise CacheSourceUnavailable("cache source exact artifact is unavailable")
    return dict(matches[0])


def _legacy_original_admission(
    owner: Mapping[str, Any], index: Mapping[str, Any], *, now: dt.datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    refs = owner.get("input_artifacts")
    if not isinstance(refs, list):
        raise CacheSourceError("legacy cache owner input references are missing")
    inputs = owner.get("generation_inputs")
    observation = owner.get("last_observation")
    if not isinstance(inputs, Mapping) or not isinstance(observation, Mapping):
        raise CacheSourceError("legacy cache owner source identity is missing")
    ledger = _load_handoff().validate_ledger(index.get("collector_handoff"))
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        for admission in ledger["admitted_observations"]:
            if (
                str(ref.get("run_id")) == str(admission.get("producer_run_id"))
                and str(ref.get("artifact_id")) == str(admission.get("artifact_id"))
                and ref.get("name") == admission.get("artifact_name")
                and ref.get("expires_at") == admission.get("artifact_expires_at")
                and ref.get("candidate_sha256") == admission.get("candidate_sha256") == inputs.get("candidate_sha256")
                and ref.get("evidence_sha256") == admission.get("refresh_evidence_sha256") == observation.get("refresh_evidence_sha256")
                and str(observation.get("producer_run_id")) == str(admission.get("producer_run_id"))
                and observation.get("observed_at") == admission.get("observed_at")
            ):
                candidates.append((dict(ref), dict(admission)))
    if len(candidates) != 1:
        raise CacheSourceError("legacy cache owner does not bind one original admission")
    if parse_time(candidates[0][1]["artifact_expires_at"], "legacy original A expiry") <= now:
        raise CacheSourceUnavailable("selected cache owner's original A artifact expired")
    return candidates[0]


def _validate_legacy_generator_contract(
    owner: Mapping[str, Any], *, repository: str, run: Mapping[str, Any], reader: Any,
) -> None:
    """Authenticate the exact historical two-file producer contract."""
    inputs = owner.get("generation_inputs")
    head_sha = run.get("head_sha")
    if (
        not isinstance(inputs, Mapping)
        or inputs.get("cache_source_profile") is not None
        or not isinstance(head_sha, str)
        or HEX40.fullmatch(head_sha) is None
        or owner.get("generation_id") != sha256_bytes(canonical_json(inputs))
    ):
        raise CacheSourceError("legacy cache source generation identity is invalid")
    processor_path = "scripts/process-upstream-catalogue-candidate.py"
    handoff_path = "scripts/upstream_catalogue_handoff.py"
    try:
        processor = reader.source_file(
            repository, head_sha, processor_path, maximum_bytes=MAX_SOURCE_FILE_BYTES,
        )
        handoff = reader.source_file(
            repository, head_sha, handoff_path, maximum_bytes=MAX_SOURCE_FILE_BYTES,
        )
    except AttributeError as exc:
        raise CacheSourceError("legacy cache source native source reader is unavailable") from exc
    revision = sha256_bytes(canonical_json({
        "processor_script_sha256": sha256_bytes(processor),
        "collector_handoff_helper_sha256": sha256_bytes(handoff),
    }))
    if inputs.get("generator_revision") != revision:
        raise CacheSourceError("legacy cache source generator contract is invalid")


def modern_generator_revision(
    *, repository: str, head_sha: str, reader: Any,
) -> str:
    """Derive the one authenticated seven-source modern B producer revision."""
    if REPOSITORY_RE.fullmatch(repository) is None or HEX40.fullmatch(head_sha) is None:
        raise CacheSourceError("modern cache source producer identity is invalid")
    try:
        material = {
            field: sha256_bytes(reader.source_file(
                repository, head_sha, path, maximum_bytes=MAX_SOURCE_FILE_BYTES,
            ))
            for field, path in MODERN_GENERATOR_INPUTS.items()
        }
    except AttributeError as exc:
        raise CacheSourceError("modern cache source native source reader is unavailable") from exc
    return sha256_bytes(canonical_json(material))


def _contract_from_bound_run(
    owner: Mapping[str, Any], *, repository: str, run: Mapping[str, Any], reader: Any,
) -> AuthenticatedCacheContract:
    inputs = owner.get("generation_inputs")
    head_sha = run.get("head_sha")
    if (
        not isinstance(inputs, Mapping)
        or not isinstance(head_sha, str)
        or HEX40.fullmatch(head_sha) is None
        or owner.get("generation_id") != sha256_bytes(canonical_json(inputs))
    ):
        raise CacheSourceError("cache source generation identity is invalid")
    profile = inputs.get("cache_source_profile")
    if profile is None:
        _validate_legacy_generator_contract(
            owner, repository=repository, run=run, reader=reader,
        )
        authenticated_profile = "legacy-two-file-v1"
    elif profile == CACHE_SOURCE_PROFILE:
        if inputs.get("generator_revision") != modern_generator_revision(
            repository=repository, head_sha=head_sha, reader=reader,
        ):
            raise CacheSourceError("modern cache source generator contract is invalid")
        authenticated_profile = CACHE_SOURCE_PROFILE
    else:
        raise CacheSourceUnavailable("cache source contract profile is unsupported")
    locator = owner.get("output_artifact")
    if (
        not isinstance(locator, Mapping)
        or not isinstance(owner.get("checkpoint_sha256"), str)
        or HEX64.fullmatch(owner["checkpoint_sha256"]) is None
        or not isinstance(locator.get("bundle_manifest_sha256"), str)
        or HEX64.fullmatch(locator["bundle_manifest_sha256"]) is None
        or not str(locator.get("artifact_id") or "").isdigit()
    ):
        raise CacheSourceError("cache source owner checkpoint binding is invalid")
    return AuthenticatedCacheContract(
        _CAPABILITY,
        owner_generation_id=str(owner["generation_id"]),
        profile=authenticated_profile,
        producer_head_sha=head_sha,
        owner_checkpoint_sha256=owner["checkpoint_sha256"],
        bundle_manifest_sha256=locator["bundle_manifest_sha256"],
        artifact_id=str(locator["artifact_id"]),
    )


def _validate_owner_archive(
    owner: Mapping[str, Any], *, archive: bytes, contract: AuthenticatedCacheContract,
    deadline_monotonic: float | None = None,
) -> AuthenticatedCacheContract:
    if deadline_monotonic is not None:
        _remaining(deadline_monotonic)
    outputs = owner.get("output_digests")
    if not isinstance(outputs, list):
        raise CacheSourceError("cache owner output inventory is missing")
    entries = {row.get("path"): dict(row) for row in outputs if isinstance(row, Mapping)}
    if len(entries) != len(outputs) or any(not isinstance(path, str) for path in entries):
        raise CacheSourceError("cache owner output inventory contains duplicate or malformed paths")
    with tempfile.TemporaryDirectory(prefix="cache-source-owner-") as temporary:
        extracted = pathlib.Path(temporary)
        _safe_extract(
            archive, extracted,
            expected={**entries, CHECKPOINT_RECEIPT_MEMBER: None},
            maximum_archive_bytes=MAX_CACHE_ARCHIVE_BYTES,
            maximum_expanded_bytes=MAX_CACHE_EXPANDED_BYTES,
            deadline_monotonic=deadline_monotonic,
        )
        archived = _sealed_checkpoint(extracted / CHECKPOINT_RECEIPT_MEMBER)
    if deadline_monotonic is not None:
        _remaining(deadline_monotonic)
    return _compare_archived_owner(
        owner, archived=archived, contract=contract,
        deadline_monotonic=deadline_monotonic,
    )


def _compare_archived_owner(
    owner: Mapping[str, Any], *, archived: Mapping[str, Any],
    contract: AuthenticatedCacheContract,
    deadline_monotonic: float | None = None,
) -> AuthenticatedCacheContract:
    if deadline_monotonic is not None:
        _remaining(deadline_monotonic)
    archived_unsigned = copy.deepcopy(dict(archived))
    durable_unsigned = copy.deepcopy(dict(owner))
    archived_unsigned.pop("checkpoint_sha256", None)
    durable_unsigned.pop("checkpoint_sha256", None)
    for value in (archived_unsigned, durable_unsigned):
        value["last_heartbeat_at"] = None
        value_locator = value.get("output_artifact")
        if isinstance(value_locator, dict):
            value_locator["artifact_id"] = None
            value_locator["expires_at"] = None
    if archived_unsigned != durable_unsigned:
        raise CacheSourceError("cache source archived checkpoint differs from durable owner")
    if deadline_monotonic is not None:
        _remaining(deadline_monotonic)
    return contract


def authenticate_owner_contract_from_bound_evidence(
    owner: Mapping[str, Any], *, repository: str, default_branch: str,
    artifact_inventory: Mapping[str, Any], run: Mapping[str, Any], archive: bytes,
    source_reader: Any, deadline_monotonic: float,
) -> AuthenticatedCacheContract:
    """Mint the same opaque contract from an already retained native packet.

    The caller supplies the exact native artifact/run responses and ZIP bytes it
    has already authenticated and retained. This function repeats all shape,
    identity, source-closure, digest, member, and checkpoint-copy validation; it
    performs no network access and accepts no caller assertion flag.
    """
    remaining = _remaining(deadline_monotonic)
    native_reader = (
        type(source_reader) is LocalGitSourceReader
        and (
            source_reader._native_binding is not None
            or source_reader._native_module is not None
            or source_reader._native_helper_module is not None
            or source_reader._native_snapshot is not None
            or source_reader.cwd is None
        )
    )
    if native_reader and remaining > MAX_NATIVE_OPERATION_SECONDS:
        raise CacheSourceError("cache source native deadline exceeds its operation bound")
    if not native_reader and remaining > MAX_WHOLE_ATTEMPT_SECONDS:
        raise CacheSourceError("cache source deadline exceeds the whole-attempt bound")
    if type(source_reader) is not LocalGitSourceReader:
        raise CacheSourceError("cache source retained evidence requires the bounded local Git reader")
    if source_reader.deadline != float(deadline_monotonic):
        raise CacheSourceError("cache source retained evidence deadline differs from its local Git reader")
    if native_reader:
        source_reader.check_deadline()
    locator = owner.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise CacheSourceError("cache owner output locator is invalid")
    run_id = str(locator.get("run_id") or "")
    artifact_id = str(locator.get("artifact_id") or "")
    name = str(locator.get("name") or "")
    attempt_match = re.fullmatch(
        rf"upstream-catalogue-processing-{re.escape(run_id)}-([1-9][0-9]*)", name,
    )
    if not run_id.isdigit() or not artifact_id.isdigit() or attempt_match is None:
        raise CacheSourceError("cache owner output locator is invalid")
    attempt = int(attempt_match.group(1))
    if (
        not isinstance(artifact_inventory, Mapping)
        or artifact_inventory.get("total_count") != 1
        or not isinstance(artifact_inventory.get("artifacts"), list)
        or len(artifact_inventory["artifacts"]) != 1
    ):
        raise CacheSourceError("cache source retained artifact inventory is not exact")
    listed = _exact_artifact_from_list(artifact_inventory, artifact_id, name)
    metadata = _artifact_metadata(
        listed, run_id=run_id, artifact_id=artifact_id, name=name,
        expires_at=str(locator.get("expires_at")), default_branch=default_branch,
    )
    bound_run = _run_attempt(
        run, repository=repository, run_id=run_id, attempt=attempt,
        default_branch=default_branch,
        expected_head_sha=(metadata.get("workflow_run") or {}).get("head_sha"),
        workflow_path=".github/workflows/upstream-catalogue-process.yml",
    )
    _artifact_run_binding(metadata, bound_run)
    contract = _contract_from_bound_run(
        owner, repository=repository, run=bound_run, reader=source_reader,
    )
    if (
        not isinstance(archive, bytes)
        or len(archive) != metadata["size_in_bytes"]
        or sha256_bytes(archive) != metadata["digest"].removeprefix("sha256:")
    ):
        raise CacheSourceError("cache source archive differs from native metadata")
    return _validate_owner_archive(
        owner, archive=archive, contract=contract,
        deadline_monotonic=deadline_monotonic,
    )


def authenticate_owner_contract(
    owner: Mapping[str, Any], *, repository: str, default_branch: str, reader: Any,
    archive_bytes: bytes | None = None,
) -> AuthenticatedCacheContract:
    """Reauthenticate the selected B producer, source closure, and exact archive."""
    locator = owner.get("output_artifact")
    if not isinstance(locator, Mapping):
        raise CacheSourceError("cache owner output locator is invalid")
    run_id = str(locator.get("run_id") or "")
    artifact_id = str(locator.get("artifact_id") or "")
    name = str(locator.get("name") or "")
    attempt_match = re.fullmatch(
        rf"upstream-catalogue-processing-{re.escape(run_id)}-([1-9][0-9]*)", name,
    )
    if not run_id.isdigit() or not artifact_id.isdigit() or attempt_match is None:
        raise CacheSourceError("cache owner output locator is invalid")
    attempt = int(attempt_match.group(1))
    listed = _exact_artifact_from_list(
        reader.json(f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"),
        artifact_id, name,
    )
    detail = _artifact_metadata(
        reader.json(f"repos/{repository}/actions/artifacts/{artifact_id}"),
        run_id=run_id, artifact_id=artifact_id, name=name,
        expires_at=str(locator.get("expires_at")), default_branch=default_branch,
    )
    if _artifact_identity(listed) != _artifact_identity(detail):
        raise CacheSourceError("cache source artifact list/detail readbacks differ")
    run = _run_attempt(
        reader.json(f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}"),
        repository=repository, run_id=run_id, attempt=attempt,
        default_branch=default_branch,
        expected_head_sha=(detail.get("workflow_run") or {}).get("head_sha"),
        workflow_path=".github/workflows/upstream-catalogue-process.yml",
    )
    _artifact_run_binding(detail, run)
    contract = _contract_from_bound_run(
        owner, repository=repository, run=run, reader=reader,
    )
    archive = (
        reader.archive(repository, artifact_id, expected_bytes=detail["size_in_bytes"])
        if archive_bytes is None else archive_bytes
    )
    if not isinstance(archive, bytes) or len(archive) != detail["size_in_bytes"]:
        raise CacheSourceError("cache source archive byte count differs from native metadata")
    if sha256_bytes(archive) != detail["digest"].removeprefix("sha256:"):
        raise CacheSourceError("cache source archive digest differs from native metadata")
    return _validate_owner_archive(
        owner, archive=archive, contract=contract,
        deadline_monotonic=getattr(reader, "deadline", None),
    )


def authenticate_owner_contract_from_archive_path(
    owner: Mapping[str, Any], *, repository: str, default_branch: str,
    reader: Any, archive_path: pathlib.Path,
) -> AuthenticatedCacheContract:
    """Re-read native/source authority and validate one already downloaded ZIP."""
    raw = read_bounded_regular_file(
        archive_path, maximum_bytes=MAX_CACHE_ARCHIVE_BYTES,
        deadline_monotonic=getattr(reader, "deadline", None),
    )
    return authenticate_owner_contract(
        owner, repository=repository, default_branch=default_branch,
        reader=reader, archive_bytes=raw,
    )


def restore_selected_cache(
    *, state_dir: pathlib.Path, target_generation_id: str | None,
    repository: str, default_branch: str, output_dir: pathlib.Path,
    reader: Any, now: dt.datetime,
) -> dict[str, Any]:
    if REPOSITORY_RE.fullmatch(repository) is None or not default_branch or "/" in default_branch:
        raise CacheSourceError("cache source repository identity is invalid")
    archive_path = output_dir.parent / f"{output_dir.name}-owner.zip"
    if archive_path.exists() or archive_path.is_symlink():
        if archive_path.is_dir() and not archive_path.is_symlink():
            raise CacheSourceError("cache source retained archive path is a directory")
        archive_path.unlink()
    selected = select_cache_owner(state_dir, target_generation_id=target_generation_id, now=now)
    if selected is None:
        return {"available": False, "reason": "no_selected_cache_owner"}
    owner, index = selected
    locator = owner["output_artifact"]
    run_id = str(locator["run_id"])
    artifact_id = str(locator["artifact_id"])
    name = str(locator["name"])
    attempt_match = re.fullmatch(rf"upstream-catalogue-processing-{re.escape(run_id)}-([1-9][0-9]*)", name)
    if not run_id.isdigit() or not artifact_id.isdigit() or attempt_match is None:
        raise CacheSourceError("cache owner output locator is invalid")
    attempt = int(attempt_match.group(1))
    inventory_before = reader.json(f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100")
    listed_before = _exact_artifact_from_list(inventory_before, artifact_id, name)
    detail_before = reader.json(f"repos/{repository}/actions/artifacts/{artifact_id}")
    metadata = _artifact_metadata(
        detail_before, run_id=run_id, artifact_id=artifact_id,
        name=name, expires_at=str(locator["expires_at"]), default_branch=default_branch,
    )
    if _artifact_identity(listed_before) != _artifact_identity(detail_before):
        raise CacheSourceError("cache source artifact list/detail readbacks differ")
    run_before = _run_attempt(
        reader.json(f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}"),
        repository=repository, run_id=run_id, attempt=attempt, default_branch=default_branch,
        expected_head_sha=(metadata.get("workflow_run") or {}).get("head_sha"),
        workflow_path=".github/workflows/upstream-catalogue-process.yml",
    )
    _artifact_run_binding(metadata, run_before)
    owner_contract = _contract_from_bound_run(
        owner, repository=repository, run=run_before, reader=reader,
    )
    archive = reader.archive(repository, artifact_id, expected_bytes=metadata["size_in_bytes"])
    if sha256_bytes(archive) != metadata["digest"].removeprefix("sha256:"):
        raise CacheSourceError("cache source archive digest differs from native metadata")
    outputs = owner["output_digests"]
    entries = {row["path"]: dict(row) for row in outputs if isinstance(row, Mapping)}
    if len(entries) != len(outputs):
        raise CacheSourceError("cache owner output inventory contains duplicate or malformed paths")
    expected: dict[str, Mapping[str, Any] | None] = {**entries, CHECKPOINT_RECEIPT_MEMBER: None}
    if output_dir.exists():
        import shutil
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    _safe_extract(
        archive, output_dir, expected=expected,
        maximum_archive_bytes=MAX_CACHE_ARCHIVE_BYTES,
        maximum_expanded_bytes=MAX_CACHE_EXPANDED_BYTES,
        deadline_monotonic=getattr(reader, "deadline", None),
    )
    archived_owner = _sealed_checkpoint(output_dir / CHECKPOINT_RECEIPT_MEMBER)
    _compare_archived_owner(
        owner, archived=archived_owner, contract=owner_contract,
        deadline_monotonic=getattr(reader, "deadline", None),
    )
    context_present = CONTEXT_MEMBER in entries
    candidate_present = ORIGINAL_CANDIDATE_MEMBER in entries
    if context_present != candidate_present:
        raise CacheSourceError("modern cache source bundle is incomplete")
    if owner_contract.profile == CACHE_SOURCE_PROFILE and not context_present:
        raise CacheSourceError("modern cache source bundle is incomplete")
    if owner_contract.profile == "legacy-two-file-v1" and context_present:
        raise CacheSourceError("legacy cache source bundle carries an unsupported modern inventory")
    provenance = "modern" if owner_contract.profile == CACHE_SOURCE_PROFILE else "legacy"
    if provenance == "legacy":
        ref, admission = _legacy_original_admission(owner, index, now=now)
        a_run_id = str(admission["producer_run_id"])
        a_attempt = int(admission["run_attempt"])
        a_artifact_id = str(admission["artifact_id"])
        a_name = str(admission["artifact_name"])
        a_run_before = _run_attempt(
            reader.json(f"repos/{repository}/actions/runs/{a_run_id}/attempts/{a_attempt}"),
            repository=repository, run_id=a_run_id, attempt=a_attempt, default_branch=default_branch,
            expected_head_sha=str(admission["head_sha"]),
            workflow_path=".github/workflows/upstream-catalog-refresh.yml",
        )
        if a_run_before.get("conclusion") != "success":
            raise CacheSourceError("legacy original A run did not succeed")
        a_inventory_before = reader.json(f"repos/{repository}/actions/runs/{a_run_id}/artifacts?per_page=100")
        a_listed_before = _exact_artifact_from_list(a_inventory_before, a_artifact_id, a_name)
        a_detail_before = reader.json(f"repos/{repository}/actions/artifacts/{a_artifact_id}")
        a_metadata = _artifact_metadata(
            a_detail_before, run_id=a_run_id, artifact_id=a_artifact_id,
            name=a_name, expires_at=str(admission["artifact_expires_at"]), default_branch=default_branch,
        )
        _artifact_run_binding(a_metadata, a_run_before)
        if (
            _artifact_identity(a_listed_before) != _artifact_identity(a_detail_before)
            or a_metadata["size_in_bytes"] != admission["artifact_size_bytes"]
            or a_metadata["digest"] != "sha256:" + admission["artifact_digest_sha256"]
        ):
            raise CacheSourceError("legacy original A native metadata differs from admission")
        a_archive = reader.archive(repository, a_artifact_id, expected_bytes=a_metadata["size_in_bytes"])
        if sha256_bytes(a_archive) != admission["artifact_digest_sha256"]:
            raise CacheSourceError("legacy original A archive digest differs from admission")
        legacy_dir = output_dir / ".legacy-original-a"
        legacy_dir.mkdir()
        with zipfile.ZipFile(io.BytesIO(a_archive)) as bundle:
            collector_sizes = {info.filename: info.file_size for info in bundle.infolist()}
        collector_expected: dict[str, Mapping[str, Any] | None] = {
            "candidate.registry.json": {
                "bytes": collector_sizes.get("candidate.registry.json"),
                "sha256": admission["candidate_sha256"],
            },
            "catalog-diff.json": {
                "bytes": collector_sizes.get("catalog-diff.json"), "sha256": ref["diff_sha256"],
            },
            "upstream-refresh-evidence.json": {
                "bytes": collector_sizes.get("upstream-refresh-evidence.json"),
                "sha256": ref["evidence_sha256"],
            },
            "upstream-refresh-work-packet.json": None,
        }
        _safe_extract(
            a_archive, legacy_dir, expected=collector_expected,
            maximum_archive_bytes=MAX_COLLECTOR_ARCHIVE_BYTES,
            maximum_expanded_bytes=MAX_COLLECTOR_EXPANDED_BYTES,
            deadline_monotonic=getattr(reader, "deadline", None),
        )
        candidate = legacy_dir / "candidate.registry.json"
        evidence = load_strict_json(
            legacy_dir / "upstream-refresh-evidence.json",
            label="legacy original A evidence", maximum_bytes=MAX_NATIVE_JSON_BYTES,
        )
        if (
            not isinstance(evidence, Mapping) or evidence.get("source_id") != owner.get("source_id")
            or (evidence.get("snapshot") or {}).get("sha256") != admission["candidate_sha256"]
            or (evidence.get("diff") or {}).get("sha256") != ref["diff_sha256"]
            or evidence.get("observed_at") != admission["observed_at"]
        ):
            raise CacheSourceError("legacy original A evidence differs from admission")
        target_candidate = output_dir / ORIGINAL_CANDIDATE_MEMBER
        candidate.replace(target_candidate)
        context = build_context(
            owner_generation_id=str(owner["generation_id"]),
            source_id=str(owner["source_id"]), source_scope=str(owner["source_scope"]),
            original_admission_ref=admission, candidate_path=target_candidate,
        )
        (output_dir / CONTEXT_MEMBER).write_bytes(canonical_json(context) + b"\n")
        import shutil
        shutil.rmtree(legacy_dir)
        if canonical_json(reader.json(f"repos/{repository}/actions/runs/{a_run_id}/attempts/{a_attempt}")) != canonical_json(a_run_before):
            raise CacheSourceError("legacy original A run changed during restore")
        if canonical_json(reader.json(f"repos/{repository}/actions/artifacts/{a_artifact_id}")) != canonical_json(a_detail_before):
            raise CacheSourceError("legacy original A artifact changed during restore")
    context_path = output_dir / CONTEXT_MEMBER
    validate_bound_context_bundle(
        context_path=context_path, owner_checkpoint=owner, state_index=index,
        owner_contract=owner_contract,
    )
    if canonical_json(reader.json(f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}")) != canonical_json(run_before):
        raise CacheSourceError("cache source run changed during restore")
    if canonical_json(reader.json(f"repos/{repository}/actions/artifacts/{artifact_id}")) != canonical_json(detail_before):
        raise CacheSourceError("cache source artifact changed during restore")
    write_bounded_regular_file(
        archive_path, archive, maximum_bytes=MAX_CACHE_ARCHIVE_BYTES,
        deadline_monotonic=getattr(reader, "deadline", None),
    )
    return {
        "available": True,
        "owner_generation_id": owner["generation_id"],
        "evidence_path": str(output_dir / ENRICHMENT_MEMBER),
        "source_context_path": str(context_path),
        "owner_archive_path": str(archive_path),
        "provenance": provenance,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--state-dir", type=pathlib.Path, required=True)
    restore.add_argument("--target-generation-id")
    restore.add_argument("--repository", required=True)
    restore.add_argument("--default-branch", required=True)
    restore.add_argument("--output-dir", type=pathlib.Path, required=True)
    restore.add_argument("--github-output", type=pathlib.Path)
    restore.add_argument("--deadline-seconds", type=float, default=MAX_WHOLE_ATTEMPT_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        not math.isfinite(args.deadline_seconds)
        or args.deadline_seconds <= 0
        or args.deadline_seconds > MAX_WHOLE_ATTEMPT_SECONDS
    ):
        raise CacheSourceError("cache source deadline is invalid")
    deadline = time.monotonic() + args.deadline_seconds
    reader = GitHubNativeReader(cwd=pathlib.Path.cwd(), deadline_monotonic=deadline)
    result = restore_selected_cache(
        state_dir=args.state_dir,
        target_generation_id=args.target_generation_id or None,
        repository=args.repository,
        default_branch=args.default_branch,
        output_dir=args.output_dir,
        reader=reader,
        now=dt.datetime.now(dt.timezone.utc),
    )
    if args.github_output:
        lines = [f"available={str(bool(result['available'])).lower()}"]
        for key in ("owner_generation_id", "evidence_path", "source_context_path", "owner_archive_path", "provenance"):
            if result.get(key) is not None:
                lines.append(f"{key}={result[key]}")
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CacheSourceUnavailable as exc:
        print(f"UNAVAILABLE {exc}", file=sys.stderr)
        raise SystemExit(4)
    except CacheSourceError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(3)
