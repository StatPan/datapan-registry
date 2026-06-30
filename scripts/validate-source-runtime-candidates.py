#!/usr/bin/env python3
"""Validate source-scoped runtime candidate batches."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running source runtime candidate validation"
    ) from exc


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, path: pathlib.Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def default_candidate_batches() -> list[pathlib.Path]:
    return sorted(pathlib.Path("reports").glob("*/runtime-candidates.json"))


def source_dir(source_id: str) -> str:
    return source_id.replace("_", "-")


def endpoint_placeholders(endpoint_template: object) -> set[str]:
    if not isinstance(endpoint_template, str):
        return set()
    return set(re.findall(r"\{([^{}]+)\}", endpoint_template))


def official_urls(profile: dict[str, object]) -> list[str]:
    references = as_dict(profile.get("references"), pathlib.Path("<source_profile>"))
    return sorted(
        {
            value
            for key, value in references.items()
            if key.endswith("_url") and isinstance(value, str)
        }
    )


def validate_consistency(batch_path: pathlib.Path, batch: dict[str, object]) -> None:
    profile_path = pathlib.Path(str(batch.get("source_profile")))
    if not profile_path.exists():
        raise ValueError(f"source_profile does not exist: {profile_path}")

    profile = as_dict(load_json(profile_path), profile_path)
    source_id = profile.get("source_id")
    if not isinstance(source_id, str):
        raise ValueError("source_profile.source_id must be a string")
    if batch_path.parent.name != source_dir(source_id):
        raise ValueError(f"path parent expected reports/{source_dir(source_id)}")
    if batch.get("source_id") != source_id:
        raise ValueError("source_id does not match source_profile")
    if batch.get("provider") != profile.get("provider"):
        raise ValueError("provider does not match source_profile")

    profile_references = as_dict(profile.get("references"), profile_path)
    batch_references = as_dict(batch.get("references"), batch_path)
    if batch_references.get("official_reference_urls") != official_urls(profile):
        raise ValueError("references.official_reference_urls must match source profile URL references")
    if batch_references.get("last_reviewed_at") != profile_references.get("last_reviewed_at"):
        raise ValueError("references.last_reviewed_at must match source profile")

    profile_auth = as_dict(profile.get("auth"), profile_path)
    profile_request = as_dict(profile.get("request"), profile_path)
    profile_response = as_dict(profile.get("response"), profile_path)
    profile_runtime = as_dict(profile.get("runtime"), profile_path)

    if profile_runtime.get("sample_param_policy") not in {"static", "catalogue_sample", "generated"}:
        raise ValueError("source_profile.runtime.sample_param_policy must be pinned before candidate validation")

    candidates = batch.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidates must be an array")

    candidate_ids: collections.Counter[str] = collections.Counter()
    pinned_sample_count = 0
    methods = set(profile_request.get("methods") or [])
    formats = set(profile_response.get("formats") or [])
    expected_credential_required = profile_auth.get("type") != "none"
    expected_key_names = sorted(profile_auth.get("key_parameter_names") or [])
    expected_key_locations = list(profile_auth.get("key_locations") or [])
    expected_injection_location = expected_key_locations[0] if expected_key_locations else "none"

    for index, raw_candidate in enumerate(candidates):
        candidate = as_dict(raw_candidate, batch_path)
        candidate_id = candidate.get("candidate_id")
        if isinstance(candidate_id, str):
            candidate_ids[candidate_id] += 1

        if candidate.get("method") not in methods:
            raise ValueError(f"candidates[{index}].method is not allowed by source profile")
        if candidate.get("format") not in formats:
            raise ValueError(f"candidates[{index}].format is not allowed by source profile")

        sample_parameters = candidate.get("sample_parameters")
        if isinstance(sample_parameters, dict) and sample_parameters:
            pinned_sample_count += 1

        credential_policy = as_dict(candidate.get("credential_policy"), batch_path)
        if credential_policy.get("required") != expected_credential_required:
            raise ValueError(f"candidates[{index}].credential_policy.required does not match source profile")
        if sorted(credential_policy.get("key_names") or []) != expected_key_names:
            raise ValueError(f"candidates[{index}].credential_policy.key_names does not match source profile")
        if credential_policy.get("injection_location") != expected_injection_location:
            raise ValueError(
                f"candidates[{index}].credential_policy.injection_location does not match source profile"
            )
        if expected_injection_location == "path":
            placeholders = endpoint_placeholders(candidate.get("endpoint_template"))
            missing_key_placeholders = sorted(set(expected_key_names).difference(placeholders))
            if missing_key_placeholders:
                raise ValueError(
                    f"candidates[{index}].endpoint_template missing path credential placeholders: "
                    f"{', '.join(missing_key_placeholders)}"
                )
        if candidate.get("evidence_status") != "not_collected":
            raise ValueError(f"candidates[{index}].evidence_status must be not_collected")
        if candidate.get("promotion_status") != "registry_only":
            raise ValueError(f"candidates[{index}].promotion_status must be registry_only")

    duplicates = sorted(candidate_id for candidate_id, count in candidate_ids.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate candidate_id values: {', '.join(duplicates)}")

    summary = as_dict(batch.get("summary"), batch_path)
    expected_summary = {
        "candidates": len(candidates),
        "pinned_sample_count": pinned_sample_count,
        "credential_required": expected_credential_required,
        "evidence_total": 0,
    }
    for key, value in expected_summary.items():
        if summary.get(key) != value:
            raise ValueError(f"summary.{key} expected {value}, got {summary.get(key)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.source-runtime-candidates.v1.schema.json",
        type=pathlib.Path,
        help="source runtime candidates JSON Schema path",
    )
    parser.add_argument(
        "candidate_batches",
        nargs="*",
        type=pathlib.Path,
        help="candidate batch files to validate; defaults to reports/*/runtime-candidates.json",
    )
    args = parser.parse_args()

    batches = args.candidate_batches or default_candidate_batches()
    if not batches:
        print("no source runtime candidate batches found", file=sys.stderr)
        return 1

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for batch_path in batches:
        try:
            instance = as_dict(load_json(batch_path), batch_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
            if not errors:
                validate_consistency(batch_path, instance)
        except Exception as exc:  # noqa: BLE001 - report all validation blockers
            print(f"FAIL {batch_path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if errors:
            failures += 1
            print(f"FAIL {batch_path}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            continue

        source_id = instance.get("source_id", "<unknown>")
        candidates = instance.get("summary", {}).get("candidates", "<unknown>")
        print(f"ok {batch_path} ({source_id}, candidates={candidates})")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
