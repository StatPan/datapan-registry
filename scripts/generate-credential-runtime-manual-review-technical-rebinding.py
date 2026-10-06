#!/usr/bin/env python3
"""Generate a narrowly-scoped technical rebind record for Health plan artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any

from manual_review_evidence_digest import compatibility_binding_sha256
from manual_review_scope import evaluate_review_scope

ROOT = pathlib.Path(".")
POLICY = ROOT / "policy/health-observation-plan-technical-rebinding.json"
MANIFEST = ROOT / "manifest.json"
COMPATIBILITY = ROOT / "reports/release-consumer-compatibility.json"
DECISION = ROOT / "reports/credential-runtime-manual-review-decision.json"
OUTPUT = ROOT / "reports/credential-runtime-manual-review-technical-rebinding.json"
HANDOFF = ROOT / "reports/credential-runtime-review-handoff.json"
HEALTH_PLAN = ROOT / "reports/health-runtime-observation-plan.v1.json"
HEALTH_SELECTION = ROOT / "policy/health-runtime-observation-selection.json"

EXPECTED_INDEPENDENT_ADDITIONS = [
    {"path": "schemas/datapan.completeness-proof-policy.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#631"},
    {"path": "schemas/datapan.completeness-proof.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#631"},
    {
        "path": "policy/completeness-proof.json",
        "kind": "completeness_proof_policy",
        "schema": "https://schemas.datapan.dev/datapan.completeness-proof-policy.v1.schema.json",
        "authority_ticket": "StatPan/datapan-registry#631",
    },
    {"path": "schemas/datapan.completeness-proof-identities.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#722"},
    {"path": "schemas/datapan.completeness-proof-inputs.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#722"},
    {"path": "schemas/datapan.completeness-proof-rollup.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#722"},
    {"path": "schemas/datapan.completeness-proof-scopes.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#722"},
    {
        "path": "policy/completeness-proof-scopes.json",
        "kind": "completeness_proof_scope_registry",
        "schema": "https://schemas.datapan.dev/datapan.completeness-proof-scopes.v1.schema.json",
        "authority_ticket": "StatPan/datapan-registry#722",
    },
    {
        "path": "reports/completeness-proof-inputs.json",
        "kind": "completeness_proof_input_index",
        "schema": "https://schemas.datapan.dev/datapan.completeness-proof-inputs.v1.schema.json",
        "authority_ticket": "StatPan/datapan-registry#722",
    },
    {
        "path": "reports/completeness-proof-rollup.json",
        "kind": "completeness_proof_rollup",
        "schema": "https://schemas.datapan.dev/datapan.completeness-proof-rollup.v1.schema.json",
        "authority_ticket": "StatPan/datapan-registry#722",
    },
    {
        "path": "reports/completeness-proof-rollup.md",
        "kind": "completeness_proof_rollup_markdown",
        "authority_ticket": "StatPan/datapan-registry#722",
    },
    {"path": "schemas/datapan.runtime-freshness-import-admission.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#633"},
    {"path": "schemas/datapan.runtime-freshness-import-attestation.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#633"},
    {"path": "schemas/datapan.catalogue-composition-receipt.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#656"},
    {"path": "schemas/datapan.catalogue-enrichment-evidence.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#656"},
    {
        "path": "reports/current-runtime-evidence-projection.json",
        "kind": "current_runtime_evidence_projection",
        "schema": "https://schemas.datapan.dev/datapan.current-runtime-evidence-projection.v1.schema.json",
        "authority_ticket": "StatPan/datapan-registry#660",
    },
    {
        "path": "schemas/datapan.current-runtime-evidence-projection.v1.schema.json",
        "kind": "schema",
        "authority_ticket": "StatPan/datapan-registry#660",
    },
    {
        "path": "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
        "kind": "schema",
        "authority_ticket": "StatPan/datapan-registry#657",
    },
    {"path": "schemas/datapan.manual-review-scope.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#661"},
    {"path": "schemas/datapan.upstream-catalogue-health-policy.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#659"},
    {"path": "schemas/datapan.upstream-catalogue-health-state-owner.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#659"},
    {"path": "schemas/datapan.upstream-catalogue-health-state.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#659"},
    {"path": "schemas/datapan.upstream-catalogue-health.v1.schema.json", "kind": "schema", "authority_ticket": "StatPan/datapan-registry#659"},
    {
        "path": "policy/upstream-catalogue-health.json",
        "kind": "upstream_catalogue_health_policy",
        "schema": "https://schemas.datapan.dev/datapan.upstream-catalogue-health-policy.v1.schema.json",
        "authority_ticket": "StatPan/datapan-registry#659",
    },
    {
        "path": "reports/diagnostic-current-source-applicability.json",
        "kind": "diagnostic_current_source_applicability",
        "schema": "https://schemas.datapan.dev/datapan.diagnostic-current-source-applicability.v1.schema.json",
        "authority_ticket": "StatPan/datapan-registry#666",
    },
    {
        "path": "schemas/datapan.diagnostic-current-source-applicability.v1.schema.json",
        "kind": "schema",
        "authority_ticket": "StatPan/datapan-registry#666",
    },
    {
        "path": "schemas/datapan.canonical-update-promotion-journal.v1.schema.json",
        "kind": "schema",
        "authority_ticket": "StatPan/datapan-registry#658",
    },
    {
        "path": "schemas/datapan.canonical-update-promotion-receipt.v1.schema.json",
        "kind": "schema",
        "authority_ticket": "StatPan/datapan-registry#658",
    },
    {
        "path": "schemas/datapan.data-go-kr-operation-denominator-expectation.v1.schema.json",
        "kind": "schema",
        "authority_ticket": "StatPan/datapan-registry#658",
    },
    {
        "path": "schemas/datapan.registry-distribution.v1.schema.json",
        "kind": "schema",
        "authority_ticket": "StatPan/datapan-registry#658",
    },
    {
        "path": "policy/data-go-kr-operation-denominator-expectation.json",
        "kind": "operation_denominator_expectation",
        "schema": "https://schemas.datapan.dev/datapan.data-go-kr-operation-denominator-expectation.v1.schema.json",
        "authority_ticket": "StatPan/datapan-registry#658",
    },
    {
        "path": "contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json",
        "kind": "historical_subject_snapshot",
        "authority_ticket": "StatPan/datapan-registry#739",
    },
    {
        "path": "schemas/datapan.canonical-update-promotion-terminal-outcome.v1.schema.json",
        "kind": "schema",
        "authority_ticket": "StatPan/datapan-registry#741",
    },
]


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_contract(artifacts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return the stable, non-content contract for a manifest artifact set."""
    contracts: list[dict[str, str]] = []
    paths: set[str] = set()
    for item in artifacts:
        path = item.get("path")
        kind = item.get("kind")
        if not isinstance(path, str) or not path or not isinstance(kind, str) or not kind:
            raise ValueError("manifest artifacts require non-empty path and kind")
        if path in paths:
            raise ValueError(f"manifest artifacts contain duplicate path: {path}")
        paths.add(path)
        contract = {"path": path, "kind": kind}
        if "schema" in item:
            schema = item["schema"]
            if not isinstance(schema, str) or not schema:
                raise ValueError(f"manifest artifact schema must be a non-empty string: {path}")
            contract["schema"] = schema
        contracts.append(contract)
    return sorted(contracts, key=lambda item: item["path"])


def artifact_contract_digest(artifacts: list[dict[str, Any]]) -> str:
    return digest_bytes(json.dumps(artifact_contract(artifacts), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def render(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def validate_independent_authorities(additions: list[dict[str, Any]]) -> None:
    canonical = lambda rows: sorted(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows
    )
    if canonical(additions) != canonical(EXPECTED_INDEPENDENT_ADDITIONS):
        raise ValueError("independent additions must match the exact separately authorized artifact set")


def expected(
    policy: dict[str, Any],
    manifest: dict[str, Any],
    compatibility: dict[str, Any],
    decision_path: pathlib.Path,
    *,
    scope_evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    additions = policy.get("allowed_additions")
    independent_additions = policy.get("independent_additions", [])
    baseline = policy.get("baseline")
    if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
        raise ValueError("manifest artifacts must be objects")
    if not isinstance(additions, list) or not all(isinstance(item, dict) for item in additions):
        raise ValueError("policy allowed_additions must be objects")
    if not isinstance(independent_additions, list) or not all(
        isinstance(item, dict) for item in independent_additions
    ):
        raise ValueError("policy independent_additions must be objects")
    if not isinstance(baseline, dict):
        raise ValueError("policy baseline must be an object")
    allowed = {str(item.get("path")): item for item in additions}
    independent = {str(item.get("path")): item for item in independent_additions}
    if len(allowed) != len(additions) or len(independent) != len(independent_additions):
        raise ValueError("technical rebinding additions must have unique artifact paths")
    if set(allowed).intersection(independent):
        raise ValueError("Health additions and independently authorized additions must not overlap")
    if scope_evaluation is not None:
        validate_independent_authorities(independent_additions)
    actual = {str(item.get("path")): item for item in artifacts}
    present = [path for path in allowed if path in actual]
    if not present:
        record = {
            "record_type": "datapan.manual-review-technical-rebinding.v1",
            "generated_at": policy["generated_at"], "status": "not_applicable",
            "approver_scope": policy["approver_scope"], "decision_path": policy["decision_path"],
            "decision_sha256": policy["decision_sha256"], "allowed_additions": additions,
            "independent_additions": independent_additions,
        }
        if scope_evaluation is not None:
            record["review_scope"] = scope_evaluation
        return record
    if set(present) != set(allowed):
        raise ValueError("Health technical rebinding requires every allowed addition")
    present_independent = [path for path in independent if path in actual]
    if set(present_independent) != set(independent):
        raise ValueError("technical rebinding requires every independently authorized addition")
    excluded_paths = set(allowed) | set(independent)
    stripped = [item for item in artifacts if str(item.get("path")) not in excluded_paths]
    if len(stripped) != baseline.get("artifact_count") or artifact_contract_digest(stripped) != baseline.get("artifact_contract_sha256"):
        raise ValueError("manifest delta exceeds the approved Health plan/schema allowlist")
    if len(artifacts) != int(baseline["artifact_count"]) + len(excluded_paths):
        raise ValueError("manifest artifact count is outside the approved technical rebinding scope")
    for rules, metadata_keys in ((allowed, set()), (independent, {"authority_ticket"})):
        for path, rule in rules.items():
            for key, value in rule.items():
                if key in metadata_keys:
                    continue
                if actual[path].get(key) != value:
                    raise ValueError(f"approved addition mismatch: {path}.{key}")
    decision_sha = digest_bytes(decision_path.read_bytes())
    historical_decision_sha = policy.get("decision_sha256")
    if scope_evaluation is not None:
        reviewed_binding = scope_evaluation.get("reviewed_binding")
        if not isinstance(reviewed_binding, dict):
            raise ValueError("review scope must include the verified historical binding")
        if historical_decision_sha != reviewed_binding.get("decision_sha256"):
            raise ValueError("technical rebinding policy must preserve the pinned historical decision")
        if scope_evaluation.get("historical_decision_valid") is not True:
            raise ValueError("pinned historical manual-review proof is invalid")
    elif decision_sha != historical_decision_sha:
        # Callers without the scope evaluator cannot authorize a replacement
        # decision. Production always supplies a scope evaluation, which keeps
        # the immutable archive pin distinct from a newly validated decision.
        raise ValueError("existing human decision must remain byte-for-byte unchanged")
    decision = load(decision_path)
    old = (
        scope_evaluation.get("reviewed_binding", {}).get("historical_decision_compatibility_sha256")
        if scope_evaluation is not None
        else decision.get("decision", {}).get("compatibility_sha256")
    )
    if not isinstance(old, str) or len(old) != 64:
        raise ValueError("accepted decision has no compatibility SHA-256")
    status = "approved_artifact_only_rebinding"
    if scope_evaluation is not None and scope_evaluation.get("effective_accepted") is not True:
        status = (
            "revalidation_required"
            if scope_evaluation.get("scope_status") == "revalidation_required"
            else "scope_unproven"
        )
    record = {
        "record_type": "datapan.manual-review-technical-rebinding.v1",
        "generated_at": policy["generated_at"], "status": status,
        "approver_scope": policy["approver_scope"], "decision_path": policy["decision_path"],
        "decision_sha256": decision_sha,
        "historical_decision_sha256": historical_decision_sha,
        "old_compatibility_sha256": old,
        "new_compatibility_sha256": compatibility_binding_sha256(compatibility),
        "baseline": baseline, "allowed_additions": additions,
        "independent_additions": independent_additions,
        "manifest_delta": {
            "added_paths": sorted(allowed),
            "independent_paths": sorted(independent),
            "artifact_count_before": baseline["artifact_count"],
            "artifact_count_after": len(artifacts),
        },
    }
    if scope_evaluation is not None:
        record["review_scope"] = scope_evaluation
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=pathlib.Path, default=POLICY)
    parser.add_argument("--manifest", type=pathlib.Path, default=MANIFEST)
    parser.add_argument("--compatibility", type=pathlib.Path, default=COMPATIBILITY)
    parser.add_argument("--decision", type=pathlib.Path, default=DECISION)
    parser.add_argument("--handoff", type=pathlib.Path, default=HANDOFF)
    parser.add_argument("--health-plan", type=pathlib.Path, default=HEALTH_PLAN)
    parser.add_argument("--health-selection", type=pathlib.Path, default=HEALTH_SELECTION)
    parser.add_argument("--output", type=pathlib.Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        manifest = load(args.manifest)
        compatibility = load(args.compatibility)
        decision = load(args.decision)
        handoff = load(args.handoff)
        scope = evaluate_review_scope(
            decision=decision,
            decision_sha256=digest_bytes(args.decision.read_bytes()),
            compatibility=compatibility,
            handoff=handoff,
            handoff_sha256=digest_bytes(args.handoff.read_bytes()),
            manifest=manifest,
            health_plan=load(args.health_plan),
            health_selection=load(args.health_selection),
        )
        value = expected(
            load(args.policy),
            manifest,
            compatibility,
            args.decision,
            scope_evaluation=scope,
        )
        text = render(value)
        if args.check:
            if not args.output.is_file() or args.output.read_text(encoding="utf-8") != text:
                raise ValueError("technical rebinding record is stale")
        else:
            args.output.write_text(text, encoding="utf-8")
    except Exception as exc:
        print(f"FAIL manual-review technical rebinding: {exc}", file=sys.stderr)
        return 1
    print(f"ok manual-review technical rebinding ({value['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
