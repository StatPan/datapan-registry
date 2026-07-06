#!/usr/bin/env python3
"""Build and validate release-wide source contract rollups."""

from __future__ import annotations

import collections
import hashlib
import json
import pathlib
from typing import Any


ROLLUP_SCHEMA_VERSION = "datapan.source-contract-rollup.v1"
PROFILE_SCHEMA_VERSION = "datapan.source-profile.v1"
DEFAULT_MANIFEST = pathlib.Path("manifest.json")
DEFAULT_SOURCE_GLOB = "sources/*.json"


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def stable_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(stable_json_bytes(value))


def as_dict(value: object, label: str | pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def as_list(value: object, label: str | pathlib.Path) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generated_at(manifest_path: pathlib.Path) -> str:
    if manifest_path.exists():
        manifest = as_dict(load_json(manifest_path), manifest_path)
        value = manifest.get("generated_at")
        if isinstance(value, str) and value:
            return value
    return "1970-01-01T00:00:00Z"


def compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def sorted_strings(value: object, label: str | pathlib.Path) -> list[str]:
    return sorted(str(item) for item in as_list(value or [], label) if isinstance(item, str))


def profile_paths(pattern: str) -> list[pathlib.Path]:
    return sorted(pathlib.Path().glob(pattern))


def source_profile_entry(profile_path: pathlib.Path) -> dict[str, Any]:
    profile = as_dict(load_json(profile_path), profile_path)
    if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ValueError(f"{profile_path}.schema_version must be {PROFILE_SCHEMA_VERSION}")

    source_id = profile.get("source_id")
    provider = profile.get("provider")
    display_name = profile.get("display_name")
    status = profile.get("status")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError(f"{profile_path}.source_id must be a non-empty string")
    if not isinstance(provider, str) or not provider:
        raise ValueError(f"{profile_path}.provider must be a non-empty string")
    if not isinstance(display_name, str) or not display_name:
        raise ValueError(f"{profile_path}.display_name must be a non-empty string")
    if not isinstance(status, str) or not status:
        raise ValueError(f"{profile_path}.status must be a non-empty string")

    adapter = as_dict(profile.get("adapter", {}), f"{profile_path}.adapter")
    references = as_dict(profile.get("references"), f"{profile_path}.references")
    catalogue = as_dict(profile.get("catalogue"), f"{profile_path}.catalogue")
    auth = as_dict(profile.get("auth"), f"{profile_path}.auth")
    request = as_dict(profile.get("request"), f"{profile_path}.request")
    response = as_dict(profile.get("response"), f"{profile_path}.response")
    errors = as_dict(profile.get("errors"), f"{profile_path}.errors")
    runtime = as_dict(profile.get("runtime"), f"{profile_path}.runtime")
    promotion = as_dict(profile.get("promotion", {}), f"{profile_path}.promotion")
    paging = as_dict(request.get("paging", {"type": "none"}), f"{profile_path}.request.paging")
    date_range = as_dict(request.get("date_range", {}), f"{profile_path}.request.date_range")

    known_error_codes = [
        as_dict(item, f"{profile_path}.errors.known_error_codes[{index}]")
        for index, item in enumerate(as_list(errors.get("known_error_codes", []), f"{profile_path}.errors.known_error_codes"))
    ]
    error_classifications = sorted({
        str(item["classification"])
        for item in known_error_codes
        if isinstance(item.get("classification"), str)
    })

    endpoints = sorted(
        key
        for key in ("list_endpoint", "detail_endpoint", "search_endpoint", "notice_endpoint")
        if isinstance(catalogue.get(key), str) and catalogue.get(key)
    )

    return {
        "source_id": source_id,
        "provider": provider,
        "display_name": display_name,
        "status": status,
        "source_profile": profile_path.as_posix(),
        "bytes": profile_path.stat().st_size,
        "sha256": file_sha256(profile_path),
        "profile_schema_version": str(profile["schema_version"]),
        "adapter": compact_dict(
            {
                "name": adapter.get("name"),
                "status": adapter.get("status", "none"),
                "capabilities": sorted_strings(adapter.get("capabilities", []), f"{profile_path}.adapter.capabilities"),
                "hosts": sorted_strings(adapter.get("hosts", []), f"{profile_path}.adapter.hosts"),
            }
        ),
        "references": compact_dict(
            {
                "homepage_url": references.get("homepage_url"),
                "api_docs_url": references.get("api_docs_url"),
                "key_request_url": references.get("key_request_url"),
                "notice_url": references.get("notice_url"),
                "terms_url": references.get("terms_url"),
                "metadata_standard_url": references.get("metadata_standard_url"),
                "last_reviewed_at": references.get("last_reviewed_at"),
            }
        ),
        "catalogue": compact_dict(
            {
                "model": catalogue.get("model"),
                "endpoints": endpoints,
                "update_cadence": catalogue.get("update_cadence"),
                "identity_fields": sorted_strings(catalogue.get("identity_fields", []), f"{profile_path}.catalogue.identity_fields"),
            }
        ),
        "auth": compact_dict(
            {
                "type": auth.get("type"),
                "key_parameter_names": sorted_strings(auth.get("key_parameter_names", []), f"{profile_path}.auth.key_parameter_names"),
                "key_locations": sorted_strings(auth.get("key_locations", []), f"{profile_path}.auth.key_locations"),
                "approval_scope": auth.get("approval_scope", "unknown"),
                "credential_reference_present": isinstance(auth.get("credential_reference"), str)
                and bool(auth.get("credential_reference")),
            }
        ),
        "request": compact_dict(
            {
                "methods": sorted_strings(request.get("methods", []), f"{profile_path}.request.methods"),
                "parameter_model": request.get("parameter_model"),
                "paging_type": paging.get("type", "none"),
                "date_range_params": sorted(key for key in ("start_param", "end_param", "format") if key in date_range),
                "required_param_policy": request.get("required_param_policy", "unknown"),
            }
        ),
        "response": compact_dict(
            {
                "formats": sorted_strings(response.get("formats", []), f"{profile_path}.response.formats"),
                "default_format": response.get("default_format"),
                "encoding": response.get("encoding"),
                "envelope_path_present": isinstance(response.get("envelope_path"), str) and bool(response.get("envelope_path")),
                "items_path_present": isinstance(response.get("items_path"), str) and bool(response.get("items_path")),
                "schema_policy": response.get("schema_policy", "unknown"),
            }
        ),
        "errors": {
            "taxonomy_status": errors.get("taxonomy_status"),
            "status_code_fields": sorted_strings(errors.get("status_code_fields", []), f"{profile_path}.errors.status_code_fields"),
            "message_fields": sorted_strings(errors.get("message_fields", []), f"{profile_path}.errors.message_fields"),
            "known_error_codes": len(known_error_codes),
            "known_error_classifications": error_classifications,
        },
        "runtime": compact_dict(
            {
                "verification_mode": runtime.get("verification_mode"),
                "timeout_seconds": runtime.get("timeout_seconds"),
                "retry_policy": runtime.get("retry_policy", "none"),
                "rate_limit_policy": runtime.get("rate_limit_policy", "unknown"),
                "sample_param_policy": runtime.get("sample_param_policy", "none"),
            }
        ),
        "promotion": compact_dict(
            {
                "default_status": promotion.get("default_status", "registry_only"),
                "mapping_reference": promotion.get("mapping_reference"),
            }
        ),
    }


def count_values(entries: list[dict[str, Any]], path: tuple[str, ...]) -> dict[str, int]:
    counter: collections.Counter[str] = collections.Counter()
    for entry in entries:
        value: Any = entry
        for key in path:
            value = as_dict(value, ".".join(path)).get(key)
        if isinstance(value, str):
            counter[value] += 1
    return dict(sorted(counter.items()))


def count_list_values(entries: list[dict[str, Any]], path: tuple[str, ...]) -> dict[str, int]:
    counter: collections.Counter[str] = collections.Counter()
    for entry in entries:
        value: Any = entry
        for key in path:
            value = as_dict(value, ".".join(path)).get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    counter[item] += 1
    return dict(sorted(counter.items()))


def count_present(entries: list[dict[str, Any]], path: tuple[str, ...]) -> int:
    present = 0
    for entry in entries:
        value: Any = entry
        for key in path:
            value = as_dict(value, ".".join(path)).get(key)
        if value:
            present += 1
    return present


def reviewed_bounds(entries: list[dict[str, Any]]) -> dict[str, str]:
    values = sorted(
        str(as_dict(entry.get("references"), "references").get("last_reviewed_at"))
        for entry in entries
        if isinstance(as_dict(entry.get("references"), "references").get("last_reviewed_at"), str)
    )
    if not values:
        return {}
    return {"oldest_last_reviewed_at": values[0], "newest_last_reviewed_at": values[-1]}


def build_rollup(
    *,
    manifest_path: pathlib.Path = DEFAULT_MANIFEST,
    source_glob: str = DEFAULT_SOURCE_GLOB,
) -> dict[str, Any]:
    paths = profile_paths(source_glob)
    if not paths:
        raise ValueError(f"no source profiles matched {source_glob}")

    sources = [source_profile_entry(path) for path in paths]
    providers = sorted({str(source["provider"]) for source in sources})
    source_ids = sorted({str(source["source_id"]) for source in sources})
    if len(source_ids) != len(sources):
        raise ValueError("source profile source_id values must be unique")

    summary: dict[str, Any] = {
        "profiles": len(sources),
        "sources": len(source_ids),
        "providers": len(providers),
        "profile_bytes": sum(int(source["bytes"]) for source in sources),
        "status_counts": count_values(sources, ("status",)),
        "adapter_status_counts": count_values(sources, ("adapter", "status")),
        "adapter_capability_counts": count_list_values(sources, ("adapter", "capabilities")),
        "auth_type_counts": count_values(sources, ("auth", "type")),
        "approval_scope_counts": count_values(sources, ("auth", "approval_scope")),
        "request_method_counts": count_list_values(sources, ("request", "methods")),
        "parameter_model_counts": count_values(sources, ("request", "parameter_model")),
        "response_format_counts": count_list_values(sources, ("response", "formats")),
        "response_schema_policy_counts": count_values(sources, ("response", "schema_policy")),
        "error_taxonomy_status_counts": count_values(sources, ("errors", "taxonomy_status")),
        "runtime_verification_mode_counts": count_values(sources, ("runtime", "verification_mode")),
        "runtime_retry_policy_counts": count_values(sources, ("runtime", "retry_policy")),
        "promotion_default_status_counts": count_values(sources, ("promotion", "default_status")),
        "references_with_api_docs": count_present(sources, ("references", "api_docs_url")),
        "references_with_key_request": count_present(sources, ("references", "key_request_url")),
        "references_with_terms": count_present(sources, ("references", "terms_url")),
        "references_with_metadata_standard": count_present(sources, ("references", "metadata_standard_url")),
        "reviewed_sources": count_present(sources, ("references", "last_reviewed_at")),
    }
    summary.update(reviewed_bounds(sources))
    source_inputs = [
        {
            "path": str(source["source_profile"]),
            "source_id": str(source["source_id"]),
            "provider": str(source["provider"]),
            "bytes": int(source["bytes"]),
            "sha256": str(source["sha256"]),
        }
        for source in sources
    ]

    return {
        "schema_version": ROLLUP_SCHEMA_VERSION,
        "generated_at": generated_at(manifest_path),
        "provider": "multi-source",
        "generation_inputs": {
            "release_manifest": manifest_path.as_posix(),
            "source_glob": source_glob,
            "generator": "scripts/generate-source-contract-rollup.py",
        },
        "source_inputs": source_inputs,
        "summary": summary,
        "sources": sources,
    }


def validate_rollup_consistency(rollup: dict[str, Any], expected: dict[str, Any]) -> None:
    if rollup != expected:
        raise ValueError("source contract rollup is stale or inconsistent with source profiles")
