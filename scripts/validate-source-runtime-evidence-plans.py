#!/usr/bin/env python3
"""Validate source-scoped runtime evidence plans."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before running source runtime evidence plan validation"
    ) from exc


def load_json(path: pathlib.Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def as_dict(value: object, path: pathlib.Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def default_plans() -> list[pathlib.Path]:
    return sorted(pathlib.Path("reports").glob("*/runtime-evidence-plan.json"))


def source_dir(source_id: str) -> str:
    return source_id.replace("_", "-")


def required_blockers(profile: dict[str, object], state: dict[str, object]) -> set[str]:
    blockers: set[str] = set()
    runtime = as_dict(profile.get("runtime"), pathlib.Path("<source_profile>"))
    adapter = as_dict(profile.get("adapter"), pathlib.Path("<source_profile>"))
    auth = as_dict(profile.get("auth"), pathlib.Path("<source_profile>"))
    errors = as_dict(profile.get("errors"), pathlib.Path("<source_profile>"))

    if runtime.get("verification_mode") == "metadata_only":
        blockers.add("metadata_only_verification")
    if adapter.get("status") != "registered" or not {"verification", "call"}.issubset(
        set(adapter.get("capabilities") or [])
    ):
        blockers.add("adapter_not_registered")
    if auth.get("type") != "none":
        blockers.add("credential_required")
    if runtime.get("sample_param_policy") == "manual":
        blockers.add("sample_parameters_not_pinned")
    if state.get("evidence_total") == 0:
        blockers.add("runtime_catalog_not_materialized")
    if errors.get("taxonomy_status") != "verified":
        blockers.add("source_specific_error_taxonomy_pending")

    return blockers


def official_urls(profile: dict[str, object]) -> list[str]:
    references = as_dict(profile.get("references"), pathlib.Path("<source_profile>"))
    return sorted(
        {
            value
            for key, value in references.items()
            if key.endswith("_url") and isinstance(value, str)
        }
    )


def validate_consistency(plan_path: pathlib.Path, plan: dict[str, object]) -> None:
    profile_path = pathlib.Path(str(plan.get("source_profile")))
    if not profile_path.exists():
        raise ValueError(f"source_profile does not exist: {profile_path}")

    profile = as_dict(load_json(profile_path), profile_path)
    source_id = profile.get("source_id")
    if not isinstance(source_id, str):
        raise ValueError("source_profile.source_id must be a string")
    if plan_path.parent.name != source_dir(source_id):
        raise ValueError(f"path parent expected reports/{source_dir(source_id)}")
    if plan.get("source_id") != source_id:
        raise ValueError("source_id does not match source_profile")
    if plan.get("provider") != profile.get("provider"):
        raise ValueError("provider does not match source_profile")

    profile_runtime = as_dict(profile.get("runtime"), profile_path)
    profile_adapter = as_dict(profile.get("adapter"), profile_path)
    profile_auth = as_dict(profile.get("auth"), profile_path)
    profile_references = as_dict(profile.get("references"), profile_path)

    state = as_dict(plan.get("runtime_state"), plan_path)
    expected_state = {
        "verification_mode": profile_runtime.get("verification_mode"),
        "adapter_status": profile_adapter.get("status"),
        "adapter_capabilities": profile_adapter.get("capabilities"),
        "auth_type": profile_auth.get("type"),
        "credential_required": profile_auth.get("type") != "none",
        "sample_param_policy": profile_runtime.get("sample_param_policy"),
    }
    for key, value in expected_state.items():
        if state.get(key) != value:
            raise ValueError(f"runtime_state.{key} expected {value}, got {state.get(key)}")

    evidence_total = int(state.get("evidence_total", 0))
    if evidence_total != sum(int(state.get(key, 0)) for key in ["verified", "failed", "skipped", "unknown"]):
        raise ValueError("runtime_state evidence counts must sum to evidence_total")
    expected_status = "no_runtime_evidence" if evidence_total == 0 else "partial_runtime_evidence"
    if state.get("evidence_status") != expected_status:
        raise ValueError(f"runtime_state.evidence_status expected {expected_status}")

    plan_references = as_dict(plan.get("references"), plan_path)
    expected_urls = official_urls(profile)
    if plan_references.get("official_reference_urls") != expected_urls:
        raise ValueError("references.official_reference_urls must match source profile URL references")
    if plan_references.get("last_reviewed_at") != profile_references.get("last_reviewed_at"):
        raise ValueError("references.last_reviewed_at must match source profile")

    blockers = plan.get("blockers")
    if not isinstance(blockers, list):
        raise ValueError("blockers must be an array")
    blocker_ids = [blocker.get("blocker_id") for blocker in blockers if isinstance(blocker, dict)]
    duplicates = sorted(item for item, count in collections.Counter(blocker_ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate blocker_id values: {', '.join(str(item) for item in duplicates)}")
    missing_blockers = sorted(required_blockers(profile, state).difference(blocker_ids))
    if missing_blockers:
        raise ValueError(f"missing required blocker_id values: {', '.join(missing_blockers)}")

    warnings = plan.get("warnings")
    if not isinstance(warnings, list):
        raise ValueError("warnings must be an array")
    warning_ids = [warning.get("warning_id") for warning in warnings if isinstance(warning, dict)]
    if evidence_total == 0 and "non_data_runtime_evidence_not_collected" not in warning_ids:
        raise ValueError("warnings must include non_data_runtime_evidence_not_collected when evidence_total is 0")
    if "adapter_not_registered" in blocker_ids and "source_runtime_adapter_not_registered" not in warning_ids:
        raise ValueError("warnings must include source_runtime_adapter_not_registered")
    if "sample_parameters_not_pinned" in blocker_ids and "source_runtime_manual_samples_unpinned" not in warning_ids:
        raise ValueError("warnings must include source_runtime_manual_samples_unpinned")
    if "source_specific_error_taxonomy_pending" in blocker_ids and "source_runtime_error_taxonomy_pending" not in warning_ids:
        raise ValueError("warnings must include source_runtime_error_taxonomy_pending")

    next_plan = as_dict(plan.get("next_evidence_plan"), plan_path)
    next_actions = next_plan.get("required_cli_capabilities")
    if not isinstance(next_actions, list):
        raise ValueError("next_evidence_plan.required_cli_capabilities must be an array")

    summary = as_dict(plan.get("summary"), plan_path)
    blocking_count = sum(1 for blocker in blockers if isinstance(blocker, dict) and blocker.get("severity") == "blocking")
    expected_summary = {
        "evidence_total": evidence_total,
        "blocking_count": blocking_count,
        "warning_count": len(warnings),
        "next_action_count": len(next_actions),
    }
    for key, value in expected_summary.items():
        if summary.get(key) != value:
            raise ValueError(f"summary.{key} expected {value}, got {summary.get(key)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="schemas/datapan.source-runtime-evidence-plan.v1.schema.json",
        type=pathlib.Path,
        help="source runtime evidence plan JSON Schema path",
    )
    parser.add_argument(
        "plans",
        nargs="*",
        type=pathlib.Path,
        help="plans to validate; defaults to reports/*/runtime-evidence-plan.json",
    )
    args = parser.parse_args()

    plans = args.plans or default_plans()
    if not plans:
        print("no source runtime evidence plans found", file=sys.stderr)
        return 1

    schema = load_json(args.schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    for plan_path in plans:
        try:
            instance = as_dict(load_json(plan_path), plan_path)
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
            if not errors:
                validate_consistency(plan_path, instance)
        except Exception as exc:  # noqa: BLE001 - report all validation blockers
            print(f"FAIL {plan_path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if errors:
            failures += 1
            print(f"FAIL {plan_path}", file=sys.stderr)
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}", file=sys.stderr)
            continue

        source_id = instance.get("source_id", "<unknown>")
        evidence = instance.get("summary", {}).get("evidence_total", "<unknown>")
        warnings = instance.get("summary", {}).get("warning_count", "<unknown>")
        print(f"ok {plan_path} ({source_id}, evidence={evidence}, warnings={warnings})")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
