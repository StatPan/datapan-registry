#!/usr/bin/env python3
"""Generate or check the credential-safe runtime evidence policy."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "missing dependency: install jsonschema before generating credential runtime evidence policy"
    ) from exc


DEFAULT_RUNTIME_ROLLUP = pathlib.Path("reports/source-runtime-evidence-rollup.json")
DEFAULT_REMEDIATION = pathlib.Path("reports/source-runtime-remediation-map.json")
DEFAULT_SCHEMA = pathlib.Path("schemas/datapan.credential-runtime-evidence-policy.v1.schema.json")
DEFAULT_OUTPUT = pathlib.Path("reports/credential-runtime-evidence-policy.json")
SCHEMA_VERSION = "datapan.credential-runtime-evidence-policy.v1"
RECEIPT_SCHEMA = "schemas/datapan.credential-runtime-receipt.v1.schema.json"
RECEIPT_VALIDATOR = "scripts/validate-credential-runtime-receipts.py"
STAGED_RECEIPT_GLOB = ".datapan/runtime-evidence/*-credentialed-receipt.json"
REVIEWED_RECEIPT_GLOB = "reports/credential-runtime-receipts/*-credentialed-receipt.json"
REVIEW_STATES = ["reviewed_accepted", "reviewed_rejected"]
RELIEF_ELIGIBLE_REVIEW_STATES = ["reviewed_accepted"]

CREDENTIAL_ENVS: dict[str, list[str]] = {
    "data_go_kr": ["DATAPAN_DATA_GO_KR_SERVICE_KEY"],
    "ecos": ["DATAPAN_ECOS_API_KEY"],
    "kosis": ["DATAPAN_KOSIS_API_KEY"],
    "open_assembly": ["DATAPAN_OPEN_ASSEMBLY_API_KEY"],
    "seoul_open_data": ["DATAPAN_SEOUL_OPEN_DATA_API_KEY"],
}

BOUNDARY_BY_ID: dict[str, str] = {
    "credential_required": "credentialed_runtime_evidence_not_required_for_canonical_registry_release",
    "metadata_only_verification": "metadata_only_source_remains_manual_review",
    "non_data_runtime_evidence_not_collected": "non_data_runtime_evidence_gap_documented",
}


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def string_items(value: object, label: str) -> list[str]:
    items: list[str] = []
    for index, item in enumerate(as_list(value, label)):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{label}[{index}] must be a non-empty string")
        items.append(item)
    return items


def source_dir(source_id: str) -> str:
    return source_id.replace("_", "-")


def source_profile_path(plan: dict[str, Any]) -> pathlib.Path:
    path = plan.get("source_profile")
    if not isinstance(path, str) or not path:
        raise ValueError("runtime evidence plan must declare source_profile")
    return pathlib.Path(path)


def manual_review_boundaries(remediation: dict[str, Any], source_id: str) -> list[dict[str, str]]:
    for source in as_list(remediation.get("sources"), "remediation.sources"):
        item = as_dict(source, "remediation.sources[]")
        if item.get("source_id") != source_id:
            continue
        boundaries: list[dict[str, str]] = []
        for finding in as_list(item.get("findings"), f"{source_id}.findings"):
            entry = as_dict(finding, f"{source_id}.findings[]")
            if entry.get("status") != "manual_review_boundary":
                continue
            finding_id = entry.get("id")
            release_boundary = entry.get("release_boundary")
            if not isinstance(finding_id, str) or not finding_id:
                raise ValueError(f"{source_id} remediation finding missing id")
            if not isinstance(release_boundary, str) or not release_boundary:
                raise ValueError(f"{source_id} remediation finding missing release_boundary")
            if finding_id not in BOUNDARY_BY_ID:
                raise ValueError(f"{source_id} has unsupported credential policy boundary: {finding_id}")
            boundaries.append(
                {
                    "id": finding_id,
                    "status": "manual_review_boundary",
                    "release_boundary": release_boundary,
                }
            )
        return sorted(boundaries, key=lambda item: item["id"])
    raise ValueError(f"missing remediation source entry for {source_id}")


def source_entry(source_rollup: dict[str, Any], remediation: dict[str, Any]) -> dict[str, Any]:
    source_id = source_rollup.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("runtime rollup source missing source_id")
    plan_path_value = source_rollup.get("runtime_evidence_plan")
    if not isinstance(plan_path_value, str) or not plan_path_value:
        raise ValueError(f"{source_id} missing runtime_evidence_plan")
    plan_path = pathlib.Path(plan_path_value)
    plan = load_json(plan_path)
    state = as_dict(plan.get("runtime_state"), f"{plan_path}.runtime_state")
    profile_path = source_profile_path(plan)
    profile = load_json(profile_path)
    auth = as_dict(profile.get("auth"), f"{profile_path}.auth")

    if plan.get("source_id") != source_id:
        raise ValueError(f"{plan_path} source_id does not match runtime rollup")
    if source_id not in CREDENTIAL_ENVS:
        raise ValueError(f"missing credential env contract for {source_id}")
    if state.get("credential_required") is not True:
        raise ValueError(f"{source_id} must be credential-gated for this policy")
    if "credential_required" not in string_items(source_rollup.get("blocker_ids"), f"{source_id}.blocker_ids"):
        raise ValueError(f"{source_id} must keep credential_required as an explicit runtime blocker")
    candidate_batch = plan.get("candidate_batch")
    if not isinstance(candidate_batch, str) or not candidate_batch:
        raise ValueError(f"{source_id} runtime plan missing candidate_batch")
    if not pathlib.Path(candidate_batch).is_file():
        raise ValueError(f"{source_id} candidate_batch is missing: {candidate_batch}")

    boundaries = manual_review_boundaries(remediation, source_id)
    if not any(item["id"] == "credential_required" for item in boundaries):
        raise ValueError(f"{source_id} credential_required must remain a manual-review boundary")

    envs = CREDENTIAL_ENVS[source_id]
    env_exports = " ".join(f"{name}=<secret>" for name in envs)
    receipt_artifact = f".datapan/runtime-evidence/{source_dir(source_id)}-credentialed-receipt.json"
    return {
        "source_id": source_id,
        "provider": str(plan.get("provider")),
        "source_profile": profile_path.as_posix(),
        "runtime_evidence_plan": plan_path.as_posix(),
        "candidate_batch": candidate_batch,
        "auth_type": auth.get("type"),
        "credential_required": True,
        "credential_envs": envs,
        "default_ci_behavior": "validate_policy_without_credentials",
        "bounded_live_evidence_path": {
            "status": "defined_not_collected",
            "input_batch": candidate_batch,
            "operator_command": (
                f"{env_exports} datapan source runtime verify "
                f"--source {source_id} --candidates {candidate_batch} --bounded --json "
                f"--output {receipt_artifact}"
            ),
            "receipt_artifact": receipt_artifact,
            "reviewed_receipt_artifact": f"reports/credential-runtime-receipts/{source_dir(source_id)}-credentialed-receipt.json",
            "receipt_schema": RECEIPT_SCHEMA,
            "receipt_validator": RECEIPT_VALIDATOR,
            "promotion_gate": (
                "Promote the source only after the credentialed receipt is reviewed, contains no secret "
                "values or hashes, and is linked from source runtime remediation evidence."
            ),
        },
        "manual_review_boundaries": boundaries,
    }


def build_report(runtime_rollup: dict[str, Any], remediation: dict[str, Any]) -> dict[str, Any]:
    generated_at = runtime_rollup.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("runtime rollup must provide generated_at")

    sources = [
        source_entry(as_dict(item, "runtime_rollup.sources[]"), remediation)
        for item in as_list(runtime_rollup.get("sources"), "runtime_rollup.sources")
    ]
    sources.sort(key=lambda item: str(item["source_id"]))

    credential_required = sum(
        1
        for source in sources
        for boundary in source["manual_review_boundaries"]
        if boundary["id"] == "credential_required"
    )
    metadata_only = sum(
        1
        for source in sources
        for boundary in source["manual_review_boundaries"]
        if boundary["id"] == "metadata_only_verification"
    )
    non_data_gaps = sum(
        1
        for source in sources
        for boundary in source["manual_review_boundaries"]
        if boundary["id"] == "non_data_runtime_evidence_not_collected"
    )
    manual_boundaries = sum(len(source["manual_review_boundaries"]) for source in sources)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "goal_issue": 344,
        "policy_ticket": 364,
        "provider": "datapan-registry",
        "summary": {
            "sources": len(sources),
            "credential_gated_sources": len(sources),
            "metadata_only_sources": metadata_only,
            "non_data_runtime_evidence_gaps": non_data_gaps,
            "credential_required_blockers": credential_required,
            "manual_review_boundaries": manual_boundaries,
            "receipt_contract_available": True,
            "reviewed_receipt_intake_available": True,
            "receipt_present": False,
            "receipt_validated": False,
            "receipt_reviewed": False,
            "receipt_relief_eligible": False,
            "manual_review_reduction_allowed": False,
            "live_credentialed_receipts_checked_in": 0,
            "reviewed_receipts_checked_in": 0,
            "default_ci_requires_credentials": False,
            "checked_in_secrets_allowed": False,
        },
        "secret_handling": {
            "credential_storage": "external_secret_store_or_operator_environment",
            "injection_mode": "environment_variable",
            "redaction_policy": "never_write_secret_values_or_secret_hashes",
            "receipt_policy": "checked_in_receipts_may_record_presence_outcome_and_error_class_only",
            "default_ci_mode": "secret_free_contract_validation",
            "credential_gated_mode": "operator_opt_in_live_runtime_batch",
        },
        "operator_contract": {
            "default_check_command": "python3 scripts/generate-credential-runtime-evidence-policy.py --check",
            "policy_check_command": "python3 scripts/generate-credential-runtime-evidence-policy.py --check",
            "receipt_schema": RECEIPT_SCHEMA,
            "receipt_validator": RECEIPT_VALIDATOR,
            "receipt_validation_command": "python3 scripts/validate-credential-runtime-receipts.py",
            "staged_receipt_glob": STAGED_RECEIPT_GLOB,
            "reviewed_receipt_glob": REVIEWED_RECEIPT_GLOB,
            "staged_receipt_validation_command": (
                "python3 scripts/validate-credential-runtime-receipts.py --allow-unreviewed "
                ".datapan/runtime-evidence/<source>-credentialed-receipt.json"
            ),
            "review_required_for_checked_in_receipts": True,
            "allowed_checked_in_review_states": REVIEW_STATES,
            "relief_eligible_review_states": RELIEF_ELIGIBLE_REVIEW_STATES,
            "credential_gated_command_template": (
                "DATAPAN_<SOURCE>_API_KEY=<secret> datapan source runtime verify "
                "--source <source_id> --candidates <runtime-candidates.json> --bounded --json "
                "--output .datapan/runtime-evidence/<source>-credentialed-receipt.json"
            ),
            "required_receipt_fields": [
                "schema_version",
                "generated_at",
                "source_id",
                "credential_configured",
                "candidate_batch",
                "outcome",
                "error_class",
                "review",
            ],
            "forbidden_receipt_fields": [
                "credential_value",
                "credential_hash",
                "authorization_header",
                "service_key",
            ],
        },
        "release_boundary": {
            "canonical_registry_compatible": True,
            "compatibility_effect": "credential_safe_manual_review_boundary",
            "manual_review_required": True,
            "live_evidence_claim": "not_claimed_until_credentialed_receipts_exist",
            "reviewed_receipt_intake": {
                "status": "defined_no_reviewed_receipts",
                "staged_receipt_glob": STAGED_RECEIPT_GLOB,
                "checked_in_receipt_glob": REVIEWED_RECEIPT_GLOB,
                "review_required_for_checked_in_receipts": True,
                "allowed_checked_in_review_states": REVIEW_STATES,
                "relief_eligible_review_states": RELIEF_ELIGIBLE_REVIEW_STATES,
                "default_ci_requires_credentials": False,
            },
            "receipt_backed_relief_gate": {
                "receipt_contract_available": True,
                "reviewed_receipt_intake_available": True,
                "receipt_present": False,
                "receipt_validated": False,
                "receipt_reviewed": False,
                "receipt_relief_eligible": False,
                "manual_review_reduction_allowed": False,
                "status": "blocked_until_reviewed_validated_credential_runtime_receipts_exist",
            },
        },
        "sources": sources,
    }


def validate_schema(report: dict[str, Any], schema_path: pathlib.Path) -> None:
    schema = load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("; ".join(rendered))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-rollup", default=DEFAULT_RUNTIME_ROLLUP, type=pathlib.Path)
    parser.add_argument("--remediation", default=DEFAULT_REMEDIATION, type=pathlib.Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=pathlib.Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="fail when the checked-in policy is stale")
    args = parser.parse_args()

    try:
        report = build_report(load_json(args.runtime_rollup), load_json(args.remediation))
        validate_schema(report, args.schema)
    except Exception as exc:  # noqa: BLE001 - release operators need the failed invariant
        print(f"FAIL generate credential runtime evidence policy: {exc}", file=sys.stderr)
        return 1

    rendered = render_json(report)
    if args.check:
        if not args.output.exists():
            print(f"FAIL {args.output}: missing credential runtime evidence policy", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"FAIL {args.output}: stale credential runtime evidence policy; "
                "run `python3 scripts/generate-credential-runtime-evidence-policy.py`",
                file=sys.stderr,
            )
            return 1
        print(f"ok {args.output} (sources={report['summary']['sources']})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} (sources={report['summary']['sources']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
