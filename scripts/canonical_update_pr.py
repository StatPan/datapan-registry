#!/usr/bin/env python3
"""Build atomic canonical-update admission receipts and track promotion acknowledgements."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from typing import Any


SCHEMA_VERSION = "datapan.canonical-update-promotion-receipt.v1"
OWNER_PREFIX = "datapan-canonical-update:v1:"
PROMOTION_STATES = (
    "prepared",
    "pending-review",
    "merged",
    "publication-pending",
    "published",
    "read-back-confirmed",
    "failed",
    "closed",
)
TRANSITIONS = {
    "prepared": {"pending-review", "failed", "closed"},
    "pending-review": {"merged", "failed", "closed"},
    "merged": {"publication-pending", "failed"},
    "publication-pending": {"published", "failed"},
    "published": {"read-back-confirmed", "failed"},
    "read-back-confirmed": set(),
    "failed": {"publication-pending", "closed"},
    "closed": set(),
}
REQUIRED_CHECKS = (
    "composition",
    "registry_manifest",
    "denominator",
    "adapter",
    "ledger",
    "release",
    "consumer",
)
MANUAL_REVIEW_STATES = {"accepted", "revalidation_required", "unproven"}
SHA1_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class AdmissionError(ValueError):
    pass


def load_object(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AdmissionError(f"{path} must contain an object")
    return value


def render(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def update_registry_manifest_artifact(
    repository_root: pathlib.Path,
    registry_path: pathlib.Path,
    registry_bytes: int,
    registry_sha256: str,
    *,
    manifest_path: pathlib.Path | None = None,
) -> str:
    """Update only the externally materialized registry inventory entry.

    The old Hugging Face revision/payload pin in registry-distribution policy
    is intentionally preserved until its separate publication is confirmed.
    """
    manifest_file = manifest_path or repository_root / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), list):
        raise AdmissionError("release manifest must contain an artifacts array")
    matching = [
        row for row in manifest["artifacts"]
        if isinstance(row, dict) and row.get("path") == registry_path.as_posix() and row.get("kind") == "registry"
    ]
    if len(matching) != 1:
        raise AdmissionError("release manifest must have one registry artifact inventory entry for the canonical path")
    valid_sha(registry_sha256, SHA256_RE, "canonical registry artifact sha256")
    if isinstance(registry_bytes, bool) or not isinstance(registry_bytes, int) or registry_bytes < 1:
        raise AdmissionError("canonical registry artifact bytes must be a positive integer")
    matching[0]["bytes"] = registry_bytes
    matching[0]["sha256"] = registry_sha256
    encoded = render(manifest)
    temporary = manifest_file.with_name(f".{manifest_file.name}.candidate.tmp")
    temporary.write_bytes(encoded)
    temporary.replace(manifest_file)
    return digest_bytes(encoded)


def valid_sha(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value) or value == "0" * len(value):
        raise AdmissionError(f"{label} must be a full nonzero immutable digest")
    return value


def owner_id(repository: str, source_id: str, scope: str) -> str:
    if not repository or "/" not in repository or not source_id or not scope:
        raise AdmissionError("owner identity requires repository, source_id, and scope")
    identity = "\0".join((repository.lower(), source_id, scope)).encode("utf-8")
    return OWNER_PREFIX + hashlib.sha256(identity).hexdigest()


def body_marker(identity: str, generation_id: str) -> str:
    if not identity.startswith(OWNER_PREFIX) or not generation_id:
        raise AdmissionError("canonical update body marker requires an owner and generation identity")
    return f"<!-- {identity} generation={generation_id} -->"


def action_run_identity(run_url: str) -> tuple[int, int]:
    match = re.search(r"/actions/runs/(\d+)/attempts/(\d+)/?$", run_url)
    if not match:
        raise AdmissionError("acknowledgement run URL must identify an exact GitHub Actions attempt")
    return int(match.group(1)), int(match.group(2))


def render_pr_body(candidate: Mapping[str, Any], identity: str | None = None, issue_number: int = 0) -> str:
    owner = identity or owner_id(candidate["repository"], candidate["source_id"], candidate["scope"])
    marker = body_marker(owner, candidate["generation_id"])
    checks = candidate.get("checks", {})
    check_lines = [
        f"- {name}: `{checks.get(name, 'unconfigured' if name == 'finish_review_policy' else 'unknown')}`"
        for name in (*REQUIRED_CHECKS, "finish_review_policy", "diagnostic_current_source_applicability")
    ]
    check_lines.append(f"- manual review acceptance: `{checks.get('manual_review_acceptance', 'unknown')}`")
    composition = candidate.get("composition_receipt", {})
    counts = composition.get("scope", {}).get("global_counts", {})
    api_records = counts.get("api_records", {})
    dispositions = counts.get("dispositions", {})
    body = [
        marker,
        "",
        "## Canonical registry update",
        "",
        f"- Source: `{candidate['source_id']}`; scope: `{candidate['scope']}`",
        f"- Candidate base commit: `{candidate['base_sha']}`",
        f"- Registry artifact: `{candidate['registry_path']}` ({candidate['registry_bytes']} bytes, sha256 `{candidate['registry_sha256']}`)",
        f"- Manifest sha256: `{candidate['manifest_sha256']}`",
        f"- Composition receipt sha256: `{candidate['composition_receipt_sha256']}`",
        f"- Global API denominator: {api_records.get('baseline_candidate_union', 'unknown')}",
        f"- Applied: {len(composition.get('scope', {}).get('applied_api_keys', []))}; pending: {len(composition.get('scope', {}).get('retained_pending_api_keys', []))}; quarantined: {len(composition.get('scope', {}).get('quarantined_api_keys', []))}",
        f"- Disposition counts: `{json.dumps(dispositions, sort_keys=True)}`",
        "- Full-scope freshness: `false`; publication allowed: `false`",
        "",
        "## Validation and promotion state",
        *check_lines,
        "",
        "The candidate passed source-snapshot validation and an isolated Git LFS remote read-back before this branch was advanced. Publication and merge remain behind the repository's reviewed gates.",
    ]
    if issue_number > 0:
        body.extend(("", f"Closes #{issue_number}"))
    return "\n".join(body) + "\n"


def validate_composition(
    composition: Mapping[str, Any],
    output_dir: pathlib.Path,
    schema: Mapping[str, Any],
    expected_registry_sha256: str,
    *,
    expected_status: str = "ready_scoped",
) -> None:
    import jsonschema

    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(composition)
    status = composition.get("status")
    if status != expected_status:
        raise AdmissionError(f"composition status blocks promotion: {status}")
    scope = composition["scope"]
    if scope["full_scope_fresh"] is not False or scope["publication_allowed"] is not False:
        raise AdmissionError("scoped composition must keep full-scope freshness and publication disabled")
    applied = scope["applied_api_keys"]
    pending = scope["retained_pending_api_keys"]
    quarantined = scope["quarantined_api_keys"]
    if not applied:
        raise AdmissionError("promotion requires at least one safely applied API candidate")
    identities = [
        (str(item.get("provider", "")).casefold(), str(item.get("id", "")))
        for item in [*applied, *pending, *quarantined]
    ]
    if any(not provider or not api_id for provider, api_id in identities) or len(identities) != len(set(identities)):
        raise AdmissionError("composition API identity sets are incomplete or overlap")
    counts = scope["global_counts"]
    api_records = counts.get("api_records")
    if not isinstance(api_records, dict) or api_records.get("baseline_candidate_union") != len(identities):
        raise AdmissionError("composition API denominator does not equal its disjoint global identity partition")
    summary_dispositions = counts.get("dispositions")
    if (
        not isinstance(summary_dispositions, dict)
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in summary_dispositions.values())
        or sum(summary_dispositions.values()) != len(identities)
    ):
        raise AdmissionError("composition disposition arithmetic does not cover the global API denominator")
    if summary_dispositions.get("retain_deletion_pending", 0) != len(pending) or summary_dispositions.get("quarantine", 0) != len(quarantined):
        raise AdmissionError("composition pending/quarantine counts disagree with their identity partitions")
    outputs = composition["outputs"]
    required_outputs = {"composed-candidate.registry.json", "ready-scope.registry.json", "semantic-diff.json", "regeneration-queue.json", "quarantine.json"}
    if not required_outputs.issubset(outputs):
        raise AdmissionError("composition receipt is missing a required candidate output digest")
    for name, record in outputs.items():
        relative = pathlib.PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise AdmissionError(f"composition output path is unsafe: {name}")
        path = output_dir.joinpath(*relative.parts)
        if not path.is_file() or path.is_symlink():
            raise AdmissionError(f"composition output is missing: {name}")
        data = path.read_bytes()
        if (len(data), digest_bytes(data)) != (record["bytes"], record["sha256"]):
            raise AdmissionError(f"composition output digest mismatch: {name}")
    full_registry = outputs["composed-candidate.registry.json"]
    if full_registry["sha256"] != expected_registry_sha256:
        raise AdmissionError("candidate registry sha256 does not match the composed full canonical output")
    if isinstance(full_registry["bytes"], bool) or not isinstance(full_registry["bytes"], int) or full_registry["bytes"] < 1:
        raise AdmissionError("composition full canonical registry byte count is invalid")
    semantic_diff = json.loads((output_dir / "semantic-diff.json").read_text(encoding="utf-8"))
    for field, scope_field in (
        ("applied_api_keys", "applied_api_keys"),
        ("retained_pending_api_keys", "retained_pending_api_keys"),
        ("quarantined_api_keys", "quarantined_api_keys"),
    ):
        if semantic_diff.get(field) != scope[scope_field]:
            raise AdmissionError(f"composition scope identity list differs from semantic diff: {field}")
    decisions = semantic_diff.get("api_decisions")
    if not isinstance(decisions, list) or len(decisions) != len(identities):
        raise AdmissionError("composition semantic diff does not enumerate the exact global API denominator")


def validate_checks(checks: Mapping[str, Any]) -> None:
    missing = [name for name in REQUIRED_CHECKS if checks.get(name) != "passed"]
    if missing:
        raise AdmissionError("candidate validation is incomplete: " + ", ".join(missing))


def validate_candidate(
    candidate: Mapping[str, Any],
    observed_base_sha: str,
    composition_schema: Mapping[str, Any],
    *,
    require_payload_readback: bool = True,
) -> None:
    required_text = ("repository", "source_id", "scope", "registry_path", "generation_id", "composition_receipt_path", "composition_outputs_dir")
    if any(not isinstance(candidate.get(name), str) or not candidate[name] for name in required_text):
        raise AdmissionError("candidate identity is incomplete")
    valid_sha(candidate.get("base_sha"), SHA1_RE, "candidate base sha")
    valid_sha(candidate.get("head_sha"), SHA1_RE, "candidate head sha")
    valid_sha(candidate.get("manifest_sha256"), SHA256_RE, "candidate manifest sha256")
    valid_sha(candidate.get("registry_sha256"), SHA256_RE, "candidate registry sha256")
    valid_sha(observed_base_sha, SHA1_RE, "observed main sha")
    if candidate["base_sha"] != observed_base_sha:
        raise AdmissionError("stale_base: recompose and revalidate against the observed main sha")
    composition = candidate.get("composition_receipt")
    if not isinstance(composition, dict):
        raise AdmissionError("candidate is missing the composition receipt summary")
    composition_path = pathlib.Path(candidate["composition_receipt_path"])
    if not composition_path.is_file() or load_object(composition_path) != composition:
        raise AdmissionError("candidate composition receipt path does not match the supplied receipt")
    receipt_digest = digest_bytes(composition_path.read_bytes())
    if candidate.get("composition_receipt_sha256") != receipt_digest:
        raise AdmissionError("candidate composition receipt sha256 mismatch")
    output_dir = pathlib.Path(candidate["composition_outputs_dir"])
    validate_composition(composition, pathlib.Path(output_dir), composition_schema, candidate["registry_sha256"])
    registry_output = composition["outputs"]["composed-candidate.registry.json"]
    if candidate.get("registry_bytes") != registry_output["bytes"]:
        raise AdmissionError("candidate registry byte count does not match the composed full canonical output")
    checks = candidate.get("checks")
    if not isinstance(checks, dict):
        raise AdmissionError("candidate is missing validation checks")
    validate_checks(checks)
    manual_review_state = candidate.get("manual_review_acceptance_status")
    if manual_review_state not in MANUAL_REVIEW_STATES or checks.get("manual_review_acceptance") != manual_review_state:
        raise AdmissionError("candidate manual-review report must be structurally valid and source-bound; pending revalidation is allowed for PR preparation")
    source_refresh = candidate.get("source_refresh_evidence")
    if not isinstance(source_refresh, dict) or source_refresh.get("schema_version") != "datapan.canonical-source-refresh-evidence.v1":
        raise AdmissionError("candidate is missing its pinned native source-refresh evidence")
    if (source_refresh.get("registry_path"), source_refresh.get("registry_bytes"), source_refresh.get("registry_sha256")) != (
        candidate["registry_path"], candidate["registry_bytes"], candidate["registry_sha256"]
    ):
        raise AdmissionError("source-refresh evidence does not bind the exact admitted registry payload")
    if source_refresh.get("manifest_sha256") != candidate["manifest_sha256"]:
        raise AdmissionError("source-refresh evidence does not bind the exact candidate release manifest")
    if source_refresh.get("verification_path") != "reports/latest-verification.json":
        raise AdmissionError("source-refresh evidence does not name the approved latest-verification input")
    if isinstance(source_refresh.get("verification_bytes"), bool) or not isinstance(source_refresh.get("verification_bytes"), int) or source_refresh["verification_bytes"] < 1:
        raise AdmissionError("source-refresh evidence has an invalid latest-verification byte count")
    verification_sha256 = valid_sha(source_refresh.get("verification_sha256"), SHA256_RE, "latest-verification input digest")
    if source_refresh.get("output_normalization") != "replace_top_level_generated_at_with_source_observation_time_utc_then_json_indent_2_newline":
        raise AdmissionError("source-refresh evidence does not declare its deterministic output normalization")
    applicability = source_refresh.get("diagnostic_current_source_applicability")
    if not isinstance(applicability, dict):
        raise AdmissionError("source-refresh evidence is missing current-source diagnostic applicability")
    if applicability.get("path") != "reports/diagnostic-current-source-applicability.json":
        raise AdmissionError("source-refresh evidence names an unsupported current-source applicability report")
    if applicability.get("schema_path") != "schemas/datapan.diagnostic-current-source-applicability.v1.schema.json":
        raise AdmissionError("source-refresh evidence names an unsupported current-source applicability schema")
    applicability_status = applicability.get("status")
    if applicability_status not in {"historical_scope_unchanged", "revalidation_required"}:
        raise AdmissionError("source-refresh evidence has an unsupported current-source applicability status")
    if checks.get("diagnostic_current_source_applicability") != applicability_status:
        raise AdmissionError("candidate check status differs from current-source applicability evidence")
    applicability_path = pathlib.Path(str(applicability["path"]))
    applicability_schema_path = pathlib.Path(str(applicability["schema_path"]))
    if (
        applicability_path.is_symlink()
        or not applicability_path.is_file()
        or applicability_schema_path.is_symlink()
        or not applicability_schema_path.is_file()
    ):
        raise AdmissionError("current-source applicability report or schema is missing or unsafe")
    applicability_bytes = applicability_path.read_bytes()
    applicability_schema_bytes = applicability_schema_path.read_bytes()
    if (
        len(applicability_bytes) != applicability.get("bytes")
        or digest_bytes(applicability_bytes) != valid_sha(applicability.get("sha256"), SHA256_RE, "current-source applicability digest")
        or len(applicability_schema_bytes) != applicability.get("schema_bytes")
        or digest_bytes(applicability_schema_bytes) != valid_sha(applicability.get("schema_sha256"), SHA256_RE, "current-source applicability schema digest")
    ):
        raise AdmissionError("current-source applicability files differ from their source-refresh receipt")
    try:
        applicability_report = json.loads(applicability_bytes)
        applicability_schema = json.loads(applicability_schema_bytes)
        import jsonschema

        jsonschema.Draft202012Validator(
            applicability_schema, format_checker=jsonschema.FormatChecker()
        ).validate(applicability_report)
    except Exception as exc:  # noqa: BLE001 - source report must fail closed
        raise AdmissionError("current-source applicability report is malformed") from exc
    if applicability_report.get("status") != applicability_status:
        raise AdmissionError("current-source applicability status differs from the source-refresh receipt")
    authority = applicability_report.get("authority")
    if not isinstance(authority, dict) or any(value is not False for value in authority.values()):
        raise AdmissionError("current-source applicability report cannot grant approval or publication authority")
    current_inputs = applicability_report.get("current_inputs")
    if not isinstance(current_inputs, dict) or current_inputs.get("registry") != {
        "path": source_refresh["registry_path"],
        "bytes": source_refresh["registry_bytes"],
        "sha256": source_refresh["registry_sha256"],
    }:
        raise AdmissionError("current-source applicability does not bind the exact candidate registry")
    health_path = pathlib.Path("reports/health-probe-catalog.json")
    if health_path.is_symlink() or not health_path.is_file():
        raise AdmissionError("current-source applicability has no safe regenerated health catalog")
    health_bytes = health_path.read_bytes()
    if current_inputs.get("health_catalog") != {
        "path": health_path.as_posix(),
        "bytes": len(health_bytes),
        "sha256": digest_bytes(health_bytes),
    }:
        raise AdmissionError("current-source applicability does not bind the regenerated health catalog")
    valid_sha(source_refresh.get("datapan_cli_revision"), SHA1_RE, "pinned Datapan CLI revision")
    valid_sha(source_refresh.get("datapan_cli_tree"), SHA1_RE, "pinned Datapan CLI tree")
    commands = source_refresh.get("commands")
    expected_outputs = {
        ("catalog", "audit"): "reports/catalog-audit.json",
        ("catalog", "errors"): "reports/error-catalog.json",
        ("catalog", "dependencies"): "reports/dependencies.json",
        ("catalog", "adapter-targets"): "reports/adapter-targets.json",
        ("catalog", "providers"): "reports/provider-backlog.json",
        ("catalog", "diff"): "reports/catalog-diff.json",
        ("catalog", "route-disposition"): "reports/route-disposition.json",
        ("catalog", "coverage"): "reports/coverage.json",
        ("catalog", "verify", "plan"): "reports/verification-plan.json",
    }
    if not isinstance(commands, list) or len(commands) != len(expected_outputs):
        raise AdmissionError("source-refresh evidence must include the complete nine-report native source generator set")
    baseline_digest = composition.get("input_digests", {}).get("baseline", {}).get("sha256")
    if not isinstance(baseline_digest, str):
        raise AdmissionError("composition receipt does not bind the immutable baseline registry digest")
    valid_sha(baseline_digest, SHA256_RE, "composition baseline registry digest")
    seen_outputs: set[tuple[str, ...]] = set()

    def has_repository_suffix(argument: str, expected: str) -> bool:
        argument_parts = pathlib.PurePosixPath(argument).parts
        expected_parts = pathlib.PurePosixPath(expected).parts
        return len(argument_parts) >= len(expected_parts) and argument_parts[-len(expected_parts):] == expected_parts

    for item in commands:
        if (
            not isinstance(item, dict)
            or item.get("exit_code") != 0
            or item.get("input_registry_sha256") != candidate["registry_sha256"]
        ):
            raise AdmissionError("native CLI source-refresh command did not succeed against the exact candidate and verification inputs")
        argv = item.get("argv")
        if not isinstance(argv, list) or not argv[:3] == ["go", "run", "./cmd/datapan"]:
            raise AdmissionError("source-refresh command is not from the pinned Datapan CLI")
        if argv[3:5] == ["catalog", "verify"]:
            command_key = ("catalog", "verify", *argv[5:6])
        else:
            command_key = tuple(argv[3:5])
        if command_key not in expected_outputs or command_key in seen_outputs:
            raise AdmissionError("source-refresh evidence has an unexpected or duplicate native source report")
        seen_outputs.add(command_key)
        if item.get("output_path") != expected_outputs[command_key]:
            raise AdmissionError("native source report is not written to its canonical release artifact path")
        if command_key == ("catalog", "diff"):
            if "--new" not in argv or argv.index("--new") + 1 >= len(argv) or not has_repository_suffix(argv[argv.index("--new") + 1], source_refresh["registry_path"]):
                raise AdmissionError("catalog diff does not use the exact admitted candidate as its new snapshot")
            if "--old" not in argv or argv.index("--old") + 1 >= len(argv) or not has_repository_suffix(argv[argv.index("--old") + 1], ".datapan/previous/data-go-kr.registry.json"):
                raise AdmissionError("catalog diff does not use the materialized immutable baseline path")
            if item.get("input_previous_registry_sha256") != baseline_digest:
                raise AdmissionError("catalog diff does not bind the exact immutable composition baseline")
        elif item.get("input_previous_registry_sha256") is not None:
            raise AdmissionError("non-diff source report unexpectedly claims a previous registry input")
        if command_key != ("catalog", "diff"):
            if "--registry" not in argv or argv.index("--registry") + 1 >= len(argv) or not has_repository_suffix(argv[argv.index("--registry") + 1], source_refresh["registry_path"]):
                raise AdmissionError("native source report does not use the exact admitted candidate registry path")
        if "--verification" in argv and (
            argv.index("--verification") + 1 >= len(argv)
            or not has_repository_suffix(argv[argv.index("--verification") + 1], source_refresh["verification_path"])
            or item.get("input_verification_sha256") != verification_sha256
        ):
            raise AdmissionError("native source report does not use the exact latest-verification path")
        if "--verification" not in argv and item.get("input_verification_sha256") is not None:
            raise AdmissionError("native source report claims latest-verification input it did not consume")
        path = item.get("output_path")
        relative = pathlib.PurePosixPath(path) if isinstance(path, str) else None
        if relative is None or relative.is_absolute() or ".." in relative.parts or "\\" in path:
            raise AdmissionError("source-refresh output path is unsafe")
        if isinstance(item.get("output_bytes"), bool) or not isinstance(item.get("output_bytes"), int) or item["output_bytes"] < 1:
            raise AdmissionError("source-refresh output byte count is invalid")
        if isinstance(item.get("raw_output_bytes"), bool) or not isinstance(item.get("raw_output_bytes"), int) or item["raw_output_bytes"] < 1:
            raise AdmissionError("source-refresh raw output byte count is invalid")
        valid_sha(item.get("raw_output_sha256"), SHA256_RE, "raw native output digest")
        valid_sha(item.get("output_sha256"), SHA256_RE, "source-refresh native output digest")
    if seen_outputs != set(expected_outputs):
        raise AdmissionError("source-refresh evidence is missing a required native source report")
    readback = candidate.get("payload_readback")
    if require_payload_readback:
        if not isinstance(readback, dict) or readback.get("status") != "verified":
            raise AdmissionError("candidate payload has no verified Git LFS remote readback")
        if readback.get("path") != candidate["registry_path"] or readback.get("sha256") != candidate["registry_sha256"]:
            raise AdmissionError("Git LFS readback identity does not match the candidate registry")
        if readback.get("manifest_sha256") != candidate["manifest_sha256"]:
            raise AdmissionError("Git LFS readback is not bound to the exact release manifest")
        if readback.get("source_sha") != candidate["head_sha"]:
            raise AdmissionError("Git LFS readback is not bound to the candidate source commit")
        if (readback.get("bytes"), readback.get("sha256")) != (
            candidate["registry_bytes"], candidate["registry_sha256"]
        ):
            raise AdmissionError("Git LFS readback bytes/hash do not match the candidate registry")
        if readback.get("readback") != "isolated_lfs_storage_verified":
            raise AdmissionError("Git LFS readback did not use an isolated empty storage root")
        if readback.get("policy_path") != "policy/registry-distribution.json":
            raise AdmissionError("Git LFS readback does not bind the canonical candidate distribution policy")
        if isinstance(readback.get("policy_bytes"), bool) or not isinstance(readback.get("policy_bytes"), int) or readback["policy_bytes"] < 1:
            raise AdmissionError("Git LFS readback has an invalid distribution policy byte count")
        valid_sha(readback.get("policy_sha256"), SHA256_RE, "candidate distribution policy digest")
        if not isinstance(readback.get("observed_at"), str) or not readback["observed_at"]:
            raise AdmissionError("Git LFS readback is missing its observation timestamp")


def decide_existing_prs(candidate: Mapping[str, Any], existing: list[Mapping[str, Any]]) -> dict[str, Any]:
    expected_owner = owner_id(candidate["repository"], candidate["source_id"], candidate["scope"])
    related = [
        row
        for row in existing
        if row.get("repository", candidate["repository"]).lower() == candidate["repository"].lower()
        and row.get("source_id") == candidate["source_id"]
        and row.get("scope") == candidate["scope"]
    ]
    open_rows = [row for row in related if row.get("state") == "open"]
    closed_rows = [row for row in related if row.get("state") == "closed"]
    if len(open_rows) > 1:
        raise AdmissionError("duplicate_open_prs: preserve all heads and resolve duplicate ownership")
    if not open_rows:
        return {"action": "create_replacement" if closed_rows else "create", "owner_id": expected_owner,
                "expected_head_sha": "0" * 40, "supersedes_prs": [int(row["number"]) for row in closed_rows]}
    row = open_rows[0]
    if row.get("owner_id") != expected_owner:
        raise AdmissionError("unowned_open_pr: preserve branch and resolve the existing PR")
    current_head = valid_sha(row.get("head_sha"), SHA1_RE, "existing PR head sha")
    automation_head = valid_sha(row.get("automation_head_sha"), SHA1_RE, "recorded automation head sha")
    if current_head != automation_head:
        raise AdmissionError("human_head_change: preserve the human-modified branch")
    current_body_sha = valid_sha(row.get("body_sha256"), SHA256_RE, "existing PR body sha256")
    automation_body_sha = valid_sha(row.get("automation_body_sha256"), SHA256_RE, "recorded automation body sha256")
    if current_body_sha != automation_body_sha:
        raise AdmissionError("human_body_change: preserve the human-modified PR body")
    same_candidate = (
        row.get("candidate_head_sha") == candidate["head_sha"]
        and row.get("manifest_sha256") == candidate["manifest_sha256"]
        and row.get("registry_sha256") == candidate["registry_sha256"]
    )
    return {
        "action": "reuse_owned" if same_candidate else "refresh_owned",
        "owner_id": expected_owner,
        "expected_head_sha": current_head,
        "pr_number": int(row["number"]),
        "supersedes_prs": [],
    }


def automation_branch(candidate: Mapping[str, Any], action: str) -> str:
    source = re.sub(r"[^a-z0-9-]+", "-", str(candidate["source_id"]).lower()).strip("-") or "source"
    scope_hash = hashlib.sha256(str(candidate["scope"]).encode("utf-8")).hexdigest()[:12]
    branch = f"automation/canonical-update/{source}-{scope_hash}"
    if action == "create_replacement":
        generation_hash = hashlib.sha256(str(candidate["generation_id"]).encode("utf-8")).hexdigest()[:10]
        branch += f"-replacement-{generation_hash}"
    return branch


def remote_ref_sha(
    materializer: Any,
    repository_root: pathlib.Path,
    remote: str,
    ref: str,
) -> str | None:
    result = materializer.git_output(["ls-remote", "--heads", remote, ref], repository_root, availability=True)
    rows = [line.split("\t", 1) for line in result.decode("ascii", errors="replace").splitlines() if "\t" in line]
    matching = [sha for sha, returned_ref in rows if returned_ref == ref]
    if len(matching) > 1:
        raise AdmissionError(f"remote returned duplicate refs for {ref}")
    if not matching:
        return None
    return valid_sha(matching[0], SHA1_RE, f"remote ref {ref} sha")


def push_owned_branch(
    candidate: Mapping[str, Any],
    receipt: dict[str, Any],
    existing_prs: list[Mapping[str, Any]],
    observed_base_sha: str,
    *,
    repository_root: pathlib.Path,
    remote: str = "origin",
    materializer: Any | None = None,
) -> dict[str, Any]:
    """Advance only the reviewed owned ref, with a final main/branch CAS after LFS read-back."""
    proof = receipt.get("candidate", {}).get("payload_readback")
    if not isinstance(proof, dict) or proof.get("status") != "verified" or proof.get("readback") != "isolated_lfs_storage_verified":
        raise AdmissionError("candidate branch cannot advance without isolated Git LFS read-back")
    module = materializer or load_materializer(repository_root)
    decision = decide_existing_prs(candidate, existing_prs)
    if decision["action"] != receipt.get("action") or decision["owner_id"] != receipt.get("ownership", {}).get("owner_id"):
        raise AdmissionError("PR ownership changed after LFS validation; preserve the existing branch and re-read")
    branch = automation_branch(candidate, decision["action"])
    if receipt.get("ownership", {}).get("branch") != branch:
        raise AdmissionError("candidate branch name differs from the validated automation ownership receipt")
    current_main = remote_ref_sha(module, repository_root, remote, "refs/heads/main")
    if current_main != observed_base_sha:
        raise AdmissionError("stale_base: remote main moved after LFS read-back; recompose and revalidate")
    ref = f"refs/heads/{branch}"
    current_branch = remote_ref_sha(module, repository_root, remote, ref)
    expected = decision["expected_head_sha"]
    expected_remote = None if expected == "0" * 40 else expected
    if current_branch == candidate["head_sha"]:
        # A prior run may have completed the ref update and stopped before the
        # PR/state receipt write. Exact commit identity makes this retry safe.
        pass
    elif current_branch != expected_remote:
        raise AdmissionError("automation branch compare-and-swap conflict after LFS read-back; preserve the current branch")
    else:
        lease = f"--force-with-lease={ref}:{expected_remote or ''}"
        try:
            module.git_output(
                ["push", "--no-verify", lease, remote, f"{candidate['head_sha']}:{ref}"],
                repository_root,
                availability=True,
            )
        except module.AvailabilityError as exc:
            raise AdmissionError("automation branch CAS push failed; candidate PR was not advanced") from exc
    pushed = remote_ref_sha(module, repository_root, remote, ref)
    if pushed != candidate["head_sha"]:
        raise AdmissionError("automation branch read-back does not equal the exact candidate head sha")
    updated = json.loads(json.dumps(receipt))
    updated["ownership"]["expected_head_sha"] = candidate["head_sha"]
    return updated


def record_pr_readback(
    receipt: dict[str, Any],
    pr: Mapping[str, Any],
    *,
    observed_at: str,
    run_url: str,
) -> dict[str, Any]:
    """Bind the actual PR API read-back to the prepared owner, generation, head and body."""
    candidate = receipt["candidate"]
    owner = receipt["ownership"]["owner_id"]
    expected_marker = body_marker(owner, candidate["generation_id"])
    body = pr.get("body")
    if not isinstance(body, str) or expected_marker not in body:
        raise AdmissionError("PR read-back is missing this candidate's owner/generation marker")
    if digest_bytes(body.encode("utf-8")) != receipt["ownership"]["body_sha256"]:
        raise AdmissionError("human_body_change: preserve the PR body and stop automation updates")
    if pr.get("headRefName") != receipt["ownership"]["branch"] or pr.get("baseRefName") != "main":
        raise AdmissionError("PR read-back branch/base identity differs from the prepared candidate")
    head = valid_sha(pr.get("headRefOid"), SHA1_RE, "PR head sha")
    if head != candidate["head_sha"]:
        raise AdmissionError("human_head_change: preserve the PR branch and stop automation updates")
    pr_state = pr.get("state")
    if pr_state not in {"OPEN", "MERGED", "CLOSED"}:
        raise AdmissionError("PR read-back state is unknown")
    result = json.loads(json.dumps(receipt))
    result["pr"] = {
        "number": int(pr.get("number", 0)),
        "url": str(pr.get("url", "")),
        "state": {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}[pr_state],
        "merge_commit_sha": pr.get("mergeCommit", {}).get("oid") if isinstance(pr.get("mergeCommit"), dict) else None,
    }
    result.setdefault("blockers", [])
    if pr_state == "OPEN":
        if result["status"] == "prepared":
            ack = _acknowledgement_for_candidate(result, "pending-review", candidate["head_sha"], observed_at, run_url, "GitHub PR API read-back")
            result = record_acknowledgement(result, ack)
        elif result["status"] != "pending-review":
            raise AdmissionError("open PR read-back conflicts with the durable promotion status")
    elif pr_state == "MERGED":
        merge_sha = valid_sha(result["pr"]["merge_commit_sha"], SHA1_RE, "PR merge commit sha")
        if result["status"] == "prepared":
            result = record_acknowledgement(
                result,
                _acknowledgement_for_candidate(result, "pending-review", candidate["head_sha"], observed_at, run_url, "GitHub PR API read-back"),
            )
        if result["status"] == "pending-review":
            ack = _acknowledgement_for_candidate(result, "merged", merge_sha, observed_at, run_url, "GitHub PR API merge read-back")
            result = record_acknowledgement(result, ack)
            if result.get("checks", {}).get("finish_review_policy") != "configured":
                # This is a factual GitHub API observation, not permission for
                # the automation to merge or publish.
                result["blockers"] = sorted(set(result.get("blockers", [])) | {"finish_review_policy_unconfigured"})
        elif result["status"] not in {"merged", "publication-pending", "published", "read-back-confirmed"}:
            raise AdmissionError("merged PR read-back conflicts with the durable promotion status")
        if result["pr"].get("merge_commit_sha") != merge_sha:
            raise AdmissionError("PR merge SHA differs from the durable promotion receipt")
    else:
        if result["status"] == "prepared":
            result = record_acknowledgement(
                result,
                _acknowledgement_for_candidate(result, "closed", candidate["head_sha"], observed_at, run_url, "GitHub PR API closed-state read-back"),
            )
        elif result["status"] == "pending-review":
            result = record_acknowledgement(
                result,
                _acknowledgement_for_candidate(result, "closed", candidate["head_sha"], observed_at, run_url, "GitHub PR API closed-state read-back"),
            )
        elif result["status"] not in {"closed", "merged", "publication-pending", "published", "read-back-confirmed"}:
            raise AdmissionError("closed PR read-back conflicts with the durable promotion status")
    return result


def _acknowledgement_for_candidate(
    receipt: Mapping[str, Any],
    status: str,
    source_sha: str,
    observed_at: str,
    run_url: str,
    evidence_reference: str,
) -> dict[str, Any]:
    candidate = receipt["candidate"]
    run_id, run_attempt = action_run_identity(run_url)
    return {
        "status": status,
        "observed_at": observed_at,
        "source_sha": source_sha,
        "manifest_sha256": candidate["manifest_sha256"],
        "artifact_identity": {"path": candidate["registry_path"], "bytes": candidate["registry_bytes"], "sha256": candidate["registry_sha256"]},
        "evidence_reference": evidence_reference,
        "run_url": run_url,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "read_back_verified": False,
        "read_back_sha256": None,
        "read_back_bytes": None,
        "publication_revision": None,
        "publication_pointer_revision": None,
    }


def load_materializer(root: pathlib.Path) -> Any:
    path = root / "scripts/materialize-canonical-registry.py"
    spec = importlib.util.spec_from_file_location("canonical_registry_materializer", path)
    if spec is None or spec.loader is None:
        raise AdmissionError("cannot load canonical registry materializer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_lfs_upload(
    candidate: dict[str, Any],
    existing: list[Mapping[str, Any]],
    observed_base_sha: str,
    composition_schema: Mapping[str, Any],
    *,
    repository_root: pathlib.Path,
    policy_path: pathlib.Path,
    remote: str = "origin",
    upload: bool,
    issue_number: int = 0,
    issue_url: str = "",
    materializer: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Optionally upload exactly one validated LFS OID, then prove it from a fresh store."""
    validate_candidate(candidate, observed_base_sha, composition_schema, require_payload_readback=False)
    decision = decide_existing_prs(candidate, existing)
    branch = automation_branch(candidate, decision["action"])
    module = materializer or load_materializer(repository_root)
    head = module.git_output(["rev-parse", "HEAD"], repository_root).decode("ascii", errors="replace").strip()
    if head != candidate["head_sha"]:
        raise AdmissionError("candidate commit is not the exact checked-out HEAD")
    base_ancestor = module.run_git(["merge-base", "--is-ancestor", observed_base_sha, head], repository_root)
    if base_ancestor.returncode != 0:
        raise AdmissionError("candidate head is not based on the observed main sha")
    current_main = remote_ref_sha(module, repository_root, remote, "refs/heads/main")
    if current_main != observed_base_sha:
        raise AdmissionError("stale_base: remote main moved; recompose and revalidate before LFS upload")
    remote_branch = remote_ref_sha(module, repository_root, remote, f"refs/heads/{branch}")
    expected_head = decision["expected_head_sha"]
    expected_remote = None if expected_head == "0" * 40 else expected_head
    if remote_branch not in {expected_remote, candidate["head_sha"]}:
        raise AdmissionError("automation branch compare-and-swap conflict; preserve the current branch and re-read PR ownership")

    manifest_path = repository_root / "manifest.json"
    manifest = module.load_object(manifest_path)
    registry_path, expected_bytes, expected_registry_sha = module.registry_identity(manifest)
    if (registry_path.as_posix(), expected_bytes, expected_registry_sha) != (
        candidate["registry_path"], candidate["registry_bytes"], candidate["registry_sha256"]
    ):
        raise AdmissionError("checked-out manifest registry artifact does not match the validated candidate")
    expected_manifest_sha = module.manifest_sha256(manifest_path)
    if expected_manifest_sha != candidate["manifest_sha256"]:
        raise AdmissionError("checked-out release manifest digest does not match the validated candidate")
    if policy_path.is_symlink():
        raise AdmissionError("candidate distribution policy must be a tracked regular file")
    policy_path = policy_path.resolve()
    policy_identity = module.tracked_policy_binding(policy_path, repository_root, head)
    if policy_identity.get("policy_path") != "policy/registry-distribution.json":
        raise AdmissionError("candidate preparation must use the repository's canonical distribution policy path")
    try:
        base_policy_bytes = module.git_output(
            ["show", f"{candidate['base_sha']}:{policy_identity['policy_path']}"], repository_root
        )
        base_policy = json.loads(base_policy_bytes)
    except (module.IntegrityError, json.JSONDecodeError) as exc:
        raise AdmissionError("candidate base commit has no valid canonical distribution policy") from exc
    policy = module.load_object(policy_path)
    if not isinstance(base_policy, dict) or policy.get("canonical_registry") != base_policy.get("canonical_registry"):
        raise AdmissionError("candidate preparation changed the immutable published Hugging Face pin")
    backend = module.preparation_backend(policy, registry_path)
    remote_url = module.git_output(["remote", "get-url", remote], repository_root).decode("utf-8", errors="replace").strip()
    if module.repository_from_remote(remote_url).lower() != backend["repository"].lower():
        raise AdmissionError("Git LFS remote does not match the declared preparation repository")
    pointer_result = module.git_output(["show", f"{head}:{registry_path.as_posix()}"], repository_root)
    oid, pointer_bytes = module.parse_lfs_pointer(pointer_result)
    if (oid, pointer_bytes) != (expected_registry_sha, expected_bytes):
        raise AdmissionError("candidate Git LFS pointer does not match the exact manifest artifact")
    local_storage = module.lfs_storage_from_env(repository_root)
    local_object = module.lfs_object_path(local_storage, oid)
    try:
        module.validate(local_object, expected_bytes, expected_registry_sha)
    except module.IntegrityError as exc:
        raise AdmissionError("validated candidate payload is missing from local Git LFS storage") from exc

    if upload:
        try:
            module.git_output(["lfs", "push", "--object-id", remote, oid], repository_root, availability=True)
        except module.AvailabilityError as exc:
            raise AdmissionError("GitHub Git LFS object upload failed; candidate remains unprepared") from exc

    with tempfile.TemporaryDirectory(prefix="datapan-lfs-pr-preparation-") as raw:
        output = pathlib.Path(raw) / "registry.json"
        try:
            readback = module.github_lfs_materialize(
                policy,
                policy_path,
                manifest_path,
                registry_path,
                expected_bytes,
                expected_registry_sha,
                output,
                candidate_commit=head,
                expected_manifest_sha256=expected_manifest_sha,
                remote=remote,
                check_only=False,
            )
        except module.AvailabilityError as exc:
            raise AdmissionError("fresh GitHub Git LFS readback failed; candidate remains unprepared") from exc
        except module.IntegrityError as exc:
            raise AdmissionError("fresh GitHub Git LFS readback identity mismatch; candidate remains unprepared") from exc
        module.validate(output, expected_bytes, expected_registry_sha)
    if any(readback.get(key) != value for key, value in policy_identity.items()):
        raise AdmissionError("fresh GitHub Git LFS read-back used a different distribution policy than the candidate HEAD")
    payload_readback = {
        "status": "verified" if upload else "verified",
        "provider": backend["provider"],
        "repository": backend["repository"],
        "remote": remote,
        "source_sha": head,
        "manifest_sha256": expected_manifest_sha,
        "path": registry_path.as_posix(),
        "bytes": expected_bytes,
        "sha256": expected_registry_sha,
        "lfs_oid": oid,
        "upload_attempted": upload,
        "readback": readback["readback"],
        **policy_identity,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    admitted_candidate = dict(candidate)
    admitted_candidate["payload_readback"] = payload_readback
    prepared = build_receipt(
        admitted_candidate, observed_base_sha, existing, composition_schema,
        issue_number=issue_number, issue_url=issue_url,
    )
    prepared["ownership"]["branch"] = branch
    return prepared, payload_readback


def resolve_interrupted_pr_creation(readback: list[Mapping[str, Any]], expected_owner: str, generation_id: str) -> dict[str, Any]:
    matches = [
        row for row in readback
        if row.get("owner_id") == expected_owner and row.get("generation_id") == generation_id
    ]
    if len(matches) > 1:
        raise AdmissionError("duplicate_creation_readback: do not retry PR creation")
    if not matches:
        return {"action": "retryable_after_empty_readback", "pr_number": 0}
    row = matches[0]
    if row.get("state") == "closed":
        return {"action": "closed_readback", "pr_number": int(row["number"])}
    if row.get("state") != "open":
        raise AdmissionError("PR creation readback has an unsupported state")
    return {"action": "creation_confirmed_by_readback", "pr_number": int(row["number"])}


def build_receipt(
    candidate: dict[str, Any],
    observed_base_sha: str,
    existing: list[Mapping[str, Any]],
    composition_schema: Mapping[str, Any],
    *,
    issue_number: int = 0,
    issue_url: str = "",
) -> dict[str, Any]:
    validate_candidate(candidate, observed_base_sha, composition_schema)
    validation_evidence = candidate.get("validation_evidence")
    if not isinstance(validation_evidence, list):
        raise AdmissionError("candidate must include command evidence from its exact committed source")
    covered = set()
    for item in validation_evidence:
        if not isinstance(item, dict) or item.get("exit_code") != 0:
            raise AdmissionError("candidate validation evidence contains a failed or malformed command")
        if item.get("source_sha") != candidate["head_sha"] or item.get("manifest_sha256") != candidate["manifest_sha256"]:
            raise AdmissionError("candidate validation command evidence is not bound to its exact head and manifest")
        if not isinstance(item.get("command"), str) or not item["command"].strip():
            raise AdmissionError("candidate validation evidence is missing its executed command")
        covered.add(item.get("name"))
    if not set((*REQUIRED_CHECKS, "composition", "manual_review_acceptance", "diagnostic_current_source_applicability")).issubset(covered):
        raise AdmissionError("candidate validation evidence does not cover every required admission check")
    decision = decide_existing_prs(candidate, existing)
    identity = decision["owner_id"]
    owner_branch = automation_branch(candidate, decision["action"])
    checks = dict(candidate["checks"])
    checks.setdefault("finish_review_policy", "unconfigured")
    blockers = [] if checks["finish_review_policy"] == "configured" else ["finish_review_policy_unconfigured"]
    if candidate.get("manual_review_acceptance_status") == "revalidation_required":
        blockers.append("manual_review_revalidation_required")
    elif candidate.get("manual_review_acceptance_status") == "unproven":
        blockers.append("manual_review_unproven")
    if checks["diagnostic_current_source_applicability"] == "revalidation_required":
        blockers.append("diagnostic_current_source_revalidation_required")
    body = render_pr_body(candidate, identity, issue_number)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "prepared",
        "action": decision["action"],
        "source_refresh_evidence": json.loads(json.dumps(candidate["source_refresh_evidence"])),
        "candidate": {key: candidate[key] for key in (
            "repository", "source_id", "scope", "base_sha", "head_sha", "manifest_sha256",
            "registry_path", "registry_bytes", "registry_sha256", "composition_receipt_sha256", "generation_id",
            "payload_readback", "manual_review_acceptance_status",
        )},
        "checks": checks,
        "validation_evidence": json.loads(json.dumps(validation_evidence)),
        "ownership": {
            "owner_id": identity,
            "branch": owner_branch,
            "expected_head_sha": decision["expected_head_sha"],
            "body_sha256": digest_bytes(body.encode("utf-8")),
            "issue_number": issue_number,
            "issue_url": issue_url,
        },
        "pr": {"number": decision.get("pr_number", 0), "url": "", "state": "open" if decision.get("pr_number") else "missing", "merge_commit_sha": None},
        "supersedes_prs": decision["supersedes_prs"],
        "acknowledgements": [],
        "blockers": blockers,
    }


def candidate_key(receipt: Mapping[str, Any]) -> tuple[str, str, str, str]:
    candidate = receipt.get("candidate", {})
    return (
        str(candidate.get("repository", "")).lower(),
        str(candidate.get("source_id", "")),
        str(candidate.get("scope", "")),
        str(candidate.get("generation_id", "")),
    )


def assert_ci_compare_and_swap(
    journal: Mapping[str, Any] | None,
    receipt: Mapping[str, Any],
    expected_ci: Mapping[str, Any] | None,
) -> None:
    """Require the durable per-candidate CI entry to match the caller snapshot."""
    key = candidate_key(receipt)
    rows = journal.get("records", []) if isinstance(journal, Mapping) else []
    matches = [row for row in rows if isinstance(row, Mapping) and candidate_key(row) == key]
    if len(matches) != 1 or matches[0].get("ci") != expected_ci:
        raise AdmissionError("verify-release CI journal compare-and-swap conflict")


def validate_journal(journal: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    import jsonschema

    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(journal)
    keys = [candidate_key(row) for row in journal.get("records", [])]
    if len(keys) != len(set(keys)):
        raise AdmissionError("promotion journal contains duplicate source/scope/generation records")
    for row in journal.get("records", []):
        timestamps = [item["observed_at"] for item in row.get("acknowledgements", [])]
        parsed = [dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc) for value in timestamps]
        if any(parsed[index] < parsed[index - 1] for index in range(1, len(parsed))):
            raise AdmissionError("promotion journal acknowledgement timestamps are out of append order")
        ci = row.get("ci")
        if ci is not None:
            candidate = row.get("candidate", {})
            ownership = row.get("ownership", {})
            pr = row.get("pr", {})
            if (
                ci.get("repository", "").casefold() != str(candidate.get("repository", "")).casefold()
                or ci.get("head_sha") != candidate.get("head_sha")
                or ci.get("branch") != ownership.get("branch")
                or ci.get("owner_id") != ownership.get("owner_id")
                or ci.get("body_sha256") != ownership.get("body_sha256")
                or ci.get("pr_number") != pr.get("number")
            ):
                raise AdmissionError("verify-release CI receipt does not bind the exact durable candidate PR identity")


CI_STATES = frozenset({
    "intent", "uncertain", "queued", "in_progress", "success", "failure", "cancelled", "action_required",
})

CI_IMMUTABLE_FIELDS = (
    "repository", "workflow_path", "head_sha", "branch", "pr_number", "owner_id",
    "body_sha256", "request_fingerprint", "intent_at",
)


def preserve_and_validate_ci(previous: Mapping[str, Any], receipt: dict[str, Any]) -> None:
    """Retain exact-head CI evidence and reject stale or identity-changing writes."""
    old = previous.get("ci")
    new = receipt.get("ci")
    if old is None:
        return
    if not isinstance(old, Mapping):
        raise AdmissionError("durable verify-release CI receipt is malformed")
    if new is None:
        receipt["ci"] = json.loads(json.dumps(old))
        return
    if not isinstance(new, Mapping):
        raise AdmissionError("verify-release CI receipt update must be an object")
    if any(old.get(field) != new.get(field) for field in CI_IMMUTABLE_FIELDS):
        raise AdmissionError("verify-release CI update changed the exact request identity")
    old_state, new_state = old.get("state"), new.get("state")
    if old_state not in CI_STATES or new_state not in CI_STATES:
        raise AdmissionError("verify-release CI update has an unsupported state")
    try:
        old_observed = dt.datetime.fromisoformat(str(old.get("observed_at", "")).replace("Z", "+00:00"))
        new_observed = dt.datetime.fromisoformat(str(new.get("observed_at", "")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdmissionError("verify-release CI update has an invalid observation timestamp") from exc
    if old_observed.tzinfo is None or new_observed.tzinfo is None or new_observed < old_observed:
        raise AdmissionError("verify-release CI update regressed its observation timestamp")
    old_run_id, new_run_id = old.get("run_id"), new.get("run_id")
    old_attempt, new_attempt = old.get("run_attempt"), new.get("run_attempt")
    if old_run_id is not None and new_run_id != old_run_id:
        raise AdmissionError("verify-release CI update changed the dispatched run identity")
    if old_attempt is not None and (new_attempt is None or new_attempt < old_attempt):
        raise AdmissionError("verify-release CI update regressed the authoritative run attempt")


def append_journal_record(
    journal: Mapping[str, Any] | None,
    receipt: Mapping[str, Any],
    *,
    repository: str,
    observed_at: str,
) -> dict[str, Any]:
    """Keep all active generations plus one last-good record for each scope."""
    current = json.loads(json.dumps(journal)) if isinstance(journal, Mapping) else {
        "schema_version": "datapan.canonical-update-promotion-journal.v1",
        "repository": repository,
        "records": [],
        "updated_at": observed_at,
    }
    if current.get("schema_version") != "datapan.canonical-update-promotion-journal.v1" or current.get("repository", "").lower() != repository.lower():
        raise AdmissionError("promotion journal belongs to a different repository or schema")
    rows = current.setdefault("records", [])
    key = candidate_key(receipt)
    matches = [index for index, row in enumerate(rows) if candidate_key(row) == key]
    if len(matches) > 1:
        raise AdmissionError("promotion journal has duplicate source/scope/generation identities")
    if matches:
        previous = rows[matches[0]]
        immutable_fields = tuple(
            (field, receipt["candidate"].get(field))
            for field in (
                "base_sha", "registry_path", "registry_bytes", "registry_sha256",
                "manifest_sha256", "composition_receipt_sha256",
            )
        )
        if any(previous["candidate"].get(field) != value for field, value in immutable_fields):
            raise AdmissionError("a generation id was reused for different immutable candidate identity")
        old_ack = previous.get("acknowledgements", [])
        new_ack = receipt.get("acknowledgements", [])
        if new_ack[:len(old_ack)] != old_ack:
            raise AdmissionError("promotion reconciliation would alter or truncate immutable acknowledgements")
        if receipt.get("status") != previous.get("status"):
            if receipt.get("status") not in TRANSITIONS.get(str(previous.get("status")), set()):
                raise AdmissionError("promotion reconciliation would regress or skip a recorded state transition")
        updated = json.loads(json.dumps(receipt))
        preserve_and_validate_ci(previous, updated)
        rows[matches[0]] = updated
    else:
        rows.append(json.loads(json.dumps(receipt)))
    source_scope = (str(receipt["candidate"]["source_id"]), str(receipt["candidate"]["scope"]))
    same_scope = [row for row in rows if (str(row["candidate"]["source_id"]), str(row["candidate"]["scope"])) == source_scope]
    confirmed = [row for row in same_scope if row.get("status") == "read-back-confirmed"]
    def confirmed_order(row: Mapping[str, Any]) -> tuple[int, int]:
        acknowledgements = row.get("acknowledgements", [])
        if not acknowledgements or not isinstance(acknowledgements[-1], Mapping):
            raise AdmissionError("last-good promotion record is missing its final immutable acknowledgement")
        acknowledgement = acknowledgements[-1]
        run_id = acknowledgement.get("run_id")
        run_attempt = acknowledgement.get("run_attempt")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1 or isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or run_attempt < 1:
            raise AdmissionError("last-good promotion record lacks an exact Actions run/attempt ordering key")
        return run_id, run_attempt

    latest_confirmed = max(confirmed, key=confirmed_order, default=None)
    kept = []
    for row in rows:
        row_scope = (str(row["candidate"]["source_id"]), str(row["candidate"]["scope"]))
        if row_scope != source_scope:
            kept.append(row)
            continue
        if row.get("status") != "read-back-confirmed" or row is latest_confirmed:
            kept.append(row)
    current["records"] = kept
    current["updated_at"] = observed_at
    return current


def record_acknowledgement(receipt: dict[str, Any], acknowledgement: dict[str, Any]) -> dict[str, Any]:
    current = receipt.get("status")
    next_state = acknowledgement.get("status")
    if current not in TRANSITIONS or next_state not in TRANSITIONS[current]:
        raise AdmissionError(f"invalid promotion transition: {current} -> {next_state}")
    read_back_expected = next_state == "read-back-confirmed"
    if acknowledgement.get("read_back_verified") is not read_back_expected:
        raise AdmissionError("read-back confirmation is required only for the read-back-confirmed state")
    candidate = receipt["candidate"]
    if acknowledgement.get("manifest_sha256") != candidate["manifest_sha256"]:
        raise AdmissionError("acknowledgement manifest identity differs from the prepared candidate")
    identity = acknowledgement.get("artifact_identity")
    if identity != {"path": candidate["registry_path"], "bytes": candidate["registry_bytes"], "sha256": candidate["registry_sha256"]}:
        raise AdmissionError("acknowledgement artifact identity differs from the prepared candidate")
    if read_back_expected:
        if acknowledgement.get("read_back_sha256") != candidate["registry_sha256"] or acknowledgement.get("read_back_bytes") != candidate["registry_bytes"]:
            raise AdmissionError("acknowledgement read-back bytes/hash do not match the prepared candidate")
    elif acknowledgement.get("read_back_sha256") is not None or acknowledgement.get("read_back_bytes") is not None:
        raise AdmissionError("pre-read-back acknowledgement cannot claim observed bytes or digest")
    if next_state in {"published", "read-back-confirmed"}:
        publication_revision = acknowledgement.get("publication_revision")
        valid_sha(publication_revision, SHA1_RE, "publication revision")
        valid_sha(acknowledgement.get("publication_pointer_revision"), SHA1_RE, "publication pointer revision")
    elif acknowledgement.get("publication_revision") is not None or acknowledgement.get("publication_pointer_revision") is not None:
        raise AdmissionError("pre-publication acknowledgement cannot claim an immutable publication revision")
    valid_sha(acknowledgement.get("source_sha"), SHA1_RE, "acknowledgement source sha")
    observed_at = acknowledgement.get("observed_at")
    if not observed_at or not acknowledgement.get("evidence_reference") or not acknowledgement.get("run_url"):
        raise AdmissionError("acknowledgement requires observation time, run URL, and source evidence")
    run_id, run_attempt = action_run_identity(str(acknowledgement["run_url"]))
    if acknowledgement.get("run_id") != run_id or acknowledgement.get("run_attempt") != run_attempt:
        raise AdmissionError("acknowledgement run identity differs from its exact attempt URL")
    try:
        parsed_time = dt.datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdmissionError("acknowledgement observed_at must be an ISO-8601 timestamp") from exc
    if parsed_time.tzinfo is None:
        raise AdmissionError("acknowledgement observed_at must include a timezone")
    if receipt.get("acknowledgements"):
        previous_time = dt.datetime.fromisoformat(str(receipt["acknowledgements"][-1]["observed_at"]).replace("Z", "+00:00"))
        if parsed_time.astimezone(dt.timezone.utc) < previous_time.astimezone(dt.timezone.utc):
            raise AdmissionError("acknowledgement timestamps must be nondecreasing in receipt append order")
    result = json.loads(json.dumps(receipt))
    if next_state == "pending-review" and acknowledgement["source_sha"] != candidate["head_sha"]:
        raise AdmissionError("pending-review acknowledgement must name the exact prepared head sha")
    if next_state == "merged":
        result["pr"]["merge_commit_sha"] = acknowledgement["source_sha"]
        result["pr"]["state"] = "merged"
    elif current in {"merged", "publication-pending", "published"}:
        if acknowledgement["source_sha"] != receipt["pr"].get("merge_commit_sha"):
            raise AdmissionError("promotion acknowledgement source sha differs from the recorded merge sha")
    if next_state == "read-back-confirmed":
        prior_published = next(
            (item for item in reversed(receipt["acknowledgements"]) if item.get("status") == "published"),
            None,
        )
        if prior_published is None or acknowledgement.get("publication_revision") != prior_published.get("publication_revision"):
            raise AdmissionError("read-back acknowledgement publication revision differs from the published immutable revision")
        if acknowledgement.get("publication_pointer_revision") != prior_published.get("publication_pointer_revision"):
            raise AdmissionError("read-back acknowledgement pointer revision differs from the published pointer revision")
    result["acknowledgements"].append(acknowledgement)
    result["status"] = next_state
    return result


def reconcile_huggingface_publication(
    receipt: dict[str, Any],
    publication_receipt_path: pathlib.Path,
    *,
    observed_at: str,
    run_url: str,
) -> dict[str, Any]:
    """Reconcile the existing #592 publish/anonymous-readback receipt without publishing."""
    publication_receipt = load_object(publication_receipt_path)
    run_id, run_attempt = action_run_identity(run_url)
    source = publication_receipt.get("source_binding")
    if not isinstance(source, dict) or source.get("status") != "bound":
        raise AdmissionError("#592 publication receipt has no exact source binding")
    candidate = receipt["candidate"]
    merge_sha = receipt.get("pr", {}).get("merge_commit_sha")
    if not merge_sha or source.get("source_sha") != merge_sha:
        raise AdmissionError("#592 publication source sha does not match the canonical update PR merge sha")
    if source.get("manifest_sha256") != candidate["manifest_sha256"]:
        raise AdmissionError("#592 publication manifest digest does not match the canonical update candidate")
    if source.get("repository") != candidate["repository"]:
        raise AdmissionError("#592 publication repository does not match the canonical update candidate")
    if publication_receipt.get("schema_version") != "datapan.registry-publication-receipt.v1":
        raise AdmissionError("unsupported #592 publication receipt schema")
    run_reference = f"{publication_receipt_path.as_posix()} sha256={digest_bytes(publication_receipt_path.read_bytes())}"
    publication = publication_receipt.get("publication")
    verification = publication_receipt.get("anonymous_verification")
    if not isinstance(publication, dict) or not isinstance(verification, dict):
        raise AdmissionError("#592 publication receipt is missing publish or anonymous verification evidence")
    if publication_receipt.get("status") == "failed":
        if receipt.get("status") == "failed":
            latest = receipt.get("acknowledgements", [])[-1] if receipt.get("acknowledgements") else None
            if isinstance(latest, dict) and latest.get("evidence_reference") == run_reference:
                return receipt
        if receipt.get("status") not in {"merged", "publication-pending", "published", "failed"}:
            raise AdmissionError("#592 failure receipt does not match a publication-pending candidate")
        failed = {
            "status": "failed", "observed_at": observed_at, "source_sha": merge_sha,
            "manifest_sha256": candidate["manifest_sha256"],
            "artifact_identity": {"path": candidate["registry_path"], "bytes": candidate["registry_bytes"], "sha256": candidate["registry_sha256"]},
            "evidence_reference": run_reference, "run_url": run_url,
            "run_id": run_id, "run_attempt": run_attempt,
            "read_back_verified": False, "read_back_sha256": None, "read_back_bytes": None,
            "publication_revision": None, "publication_pointer_revision": None,
        }
        if receipt["status"] in {"merged", "failed"}:
            receipt = record_acknowledgement(receipt, {
                **failed,
                "status": "publication-pending",
                "evidence_reference": "awaiting_existing_manual_publication_workflow",
                "run_url": run_url,
                "run_id": run_id,
                "run_attempt": run_attempt,
            })
        return record_acknowledgement(receipt, failed)
    if publication_receipt.get("status") != "verified" or publication.get("status") != "published" or verification.get("status") != "verified":
        raise AdmissionError("#592 publication and anonymous read-back must both be verified")
    revision = valid_sha(publication.get("payload_revision"), SHA1_RE, "published payload revision")
    pointer_revision = valid_sha(publication.get("pointer_revision"), SHA1_RE, "published pointer revision")
    if verification.get("revision") != revision:
        raise AdmissionError("#592 anonymous read-back revision differs from the published immutable payload revision")
    if publication.get("dataset") != candidate["repository"] or verification.get("dataset") != candidate["repository"]:
        raise AdmissionError("#592 publication dataset does not match the canonical update repository")
    if receipt.get("status") == "read-back-confirmed":
        latest = receipt["acknowledgements"][-1]
        if latest.get("publication_revision") != revision or latest.get("publication_pointer_revision") != pointer_revision:
            raise AdmissionError("duplicate #592 reconciliation names a different immutable publication revision")
        return receipt
    if receipt.get("status") == "merged":
        receipt = record_acknowledgement(receipt, {
            "status": "publication-pending", "observed_at": observed_at, "source_sha": merge_sha,
            "manifest_sha256": candidate["manifest_sha256"],
            "artifact_identity": {"path": candidate["registry_path"], "bytes": candidate["registry_bytes"], "sha256": candidate["registry_sha256"]},
            "evidence_reference": "existing_manual_publication_workflow_completed", "run_url": run_url,
            "run_id": run_id, "run_attempt": run_attempt,
            "read_back_verified": False, "read_back_sha256": None, "read_back_bytes": None,
            "publication_revision": None, "publication_pointer_revision": None,
        })
    elif receipt.get("status") == "failed":
        receipt = record_acknowledgement(receipt, {
            "status": "publication-pending", "observed_at": observed_at, "source_sha": merge_sha,
            "manifest_sha256": candidate["manifest_sha256"],
            "artifact_identity": {"path": candidate["registry_path"], "bytes": candidate["registry_bytes"], "sha256": candidate["registry_sha256"]},
            "evidence_reference": "existing_manual_publication_retry_observed", "run_url": run_url,
            "run_id": run_id, "run_attempt": run_attempt,
            "read_back_verified": False, "read_back_sha256": None, "read_back_bytes": None,
            "publication_revision": None, "publication_pointer_revision": None,
        })
    published = {
        "status": "published", "observed_at": observed_at, "source_sha": merge_sha,
        "manifest_sha256": candidate["manifest_sha256"],
        "artifact_identity": {"path": candidate["registry_path"], "bytes": candidate["registry_bytes"], "sha256": candidate["registry_sha256"]},
        "evidence_reference": run_reference, "run_url": run_url,
        "run_id": run_id, "run_attempt": run_attempt,
        "read_back_verified": False, "read_back_sha256": None, "read_back_bytes": None,
        "publication_revision": revision, "publication_pointer_revision": pointer_revision,
    }
    if receipt.get("status") == "publication-pending":
        receipt = record_acknowledgement(receipt, published)
    elif receipt.get("status") == "published":
        previous = receipt["acknowledgements"][-1]
        if previous.get("publication_revision") != revision or previous.get("publication_pointer_revision") != pointer_revision:
            raise AdmissionError("#592 reconciliation changed an already-recorded immutable publication revision")
    else:
        raise AdmissionError("#592 receipt cannot advance the canonical update promotion state")
    read_back = dict(published)
    read_back.update({
        "status": "read-back-confirmed",
        "read_back_verified": True,
        "read_back_sha256": candidate["registry_sha256"],
        "read_back_bytes": candidate["registry_bytes"],
    })
    return record_acknowledgement(receipt, read_back)


def stage_bundle(
    output: pathlib.Path,
    artifacts: Mapping[str, bytes],
    validator: Callable[[pathlib.Path], None],
) -> list[str]:
    """Write a complete candidate bundle off-path, validate it, then expose it with one rename."""
    if output.exists():
        raise AdmissionError(f"candidate bundle destination already exists: {output}")
    if not artifacts:
        raise AdmissionError("candidate bundle must contain at least one artifact")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    paths: list[str] = []
    try:
        for raw_path, content in sorted(artifacts.items()):
            relative = pathlib.PurePosixPath(raw_path)
            if relative.is_absolute() or ".." in relative.parts or "\\" in raw_path:
                raise AdmissionError(f"unsafe candidate artifact path: {raw_path}")
            target = temporary.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            paths.append(relative.as_posix())
        validator(temporary)
        temporary.rename(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return paths


def atomic_write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = pathlib.Path(handle.name)
    try:
        temporary.write_bytes(render(value))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=pathlib.Path, required=True)
    parser.add_argument("--pull-requests", type=pathlib.Path, required=True)
    parser.add_argument("--observed-base-sha", required=True)
    parser.add_argument("--output", type=pathlib.Path, default=pathlib.Path("reports/canonical-update-promotion-receipt.json"))
    parser.add_argument("--schema", type=pathlib.Path, default=pathlib.Path("schemas/datapan.canonical-update-promotion-receipt.v1.schema.json"))
    parser.add_argument("--composition-schema", type=pathlib.Path, default=pathlib.Path("schemas/datapan.catalogue-composition-receipt.v1.schema.json"))
    args = parser.parse_args()
    try:
        candidate = load_object(args.candidate)
        prs = json.loads(args.pull_requests.read_text(encoding="utf-8"))
        if not isinstance(prs, list) or any(not isinstance(row, dict) for row in prs):
            raise AdmissionError("pull-request readback must contain an array of PR objects")
        receipt = build_receipt(candidate, args.observed_base_sha, prs, load_object(args.composition_schema))
        import jsonschema

        schema = load_object(args.schema)
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(receipt)
        atomic_write_json(args.output, receipt)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"status": receipt["status"], "action": receipt["action"], "receipt": args.output.as_posix()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
