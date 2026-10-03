"""Verify historical manual-review scope and evaluate current effectiveness.

The pinned evidence under tests/fixtures is a compact trust anchor for the
accepted review. It deliberately does not copy the 137 MB source registry;
the historic manifest pins that source by path, byte count, and SHA-256.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import pathlib
from typing import Any

from manual_review_evidence_digest import compatibility_binding_sha256


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_DIR = ROOT / "tests/fixtures/manual-review-scope"
HISTORICAL_DECISION_COMMIT = "4458003e1490e70d2efc538df93aeaa3addc90ef"
HEALTH_SELECTION_COMMIT = "991f2fa346e78209f2508fdaf0cb601ce4315cf6"
HISTORICAL_SOURCE_PATH = "data/data-go-kr.registry.json"
HISTORICAL_SOURCE_BYTES = 137_735_169
HISTORICAL_SOURCE_SHA256 = "eeda72ee8590f458de8d75703662578e80edf3e61282f0e5e67547c4f6e5f644"
REVIEW_SCOPE_VERSION = "datapan.manual-review-scope.v1"
DERIVED_ACCEPTANCE_FIELDS = frozenset(
    {
        "manual_review_acceptance_status",
        "manual_review_acceptance_accepted",
        "manual_review_acceptance_decision",
        "manual_review_acceptance_required_evidence",
        "manual_review_acceptance_boundary_accepted",
        "manual_review_acceptance_goal_completion_effect",
    }
)
DECISION_MIRROR_FIELDS = frozenset(
    {
        "manual_review_decision_status",
        "manual_review_decision_accepted",
        "manual_review_decision_reason",
        "manual_review_decision_boundary_accepted",
    }
)
FIXTURE_SHA256 = {
    "decision.json": "4d0bee81a67d4b49a1ccb022ab6be7a28a76b9613cbd7c2211d5f22ce9d69151",
    "compatibility.json": "9ea04b875d96ed773f2b5bc5d0579c789e32d35040c5a79dd2d5e03bb71a6d77",
    "handoff.json": "7d8e645a0f58469fabe3f9289e20ea6835c81e088dd363519d015d1d0f501beb",
    "manifest.json": "c42f9f8fa56e31459524ac5684e8b0de05c59102822d0f1289ffe7235eca2217",
    "health-plan.json": "42d27d5c4d5c148914384bcbeb954f61b9a5390e10b7473fea724f826388639a",
    "health-selection.json": "a072096f8cd68e354e18bf410a3042f50b8f3282446e2fabf5cbcb3efe93957a",
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _source_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be an array")
    matches = [item for item in artifacts if isinstance(item, dict) and item.get("path") == HISTORICAL_SOURCE_PATH]
    if len(matches) != 1:
        raise ValueError("manifest must contain exactly one canonical source registry artifact")
    source = matches[0]
    byte_count = source.get("bytes")
    digest = source.get("sha256")
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("canonical source registry artifact requires bytes and SHA-256")
    return {"path": HISTORICAL_SOURCE_PATH, "bytes": byte_count, "sha256": digest}


def _review_risk_and_summary(compatibility: dict[str, Any]) -> dict[str, Any]:
    summary = copy.deepcopy(_object(compatibility.get("summary"), "compatibility.summary"))
    risk = copy.deepcopy(_object(compatibility.get("runtime_risk_evidence"), "compatibility.runtime_risk_evidence"))
    for key in DERIVED_ACCEPTANCE_FIELDS | DECISION_MIRROR_FIELDS:
        risk.pop(key, None)
    return {"summary": summary, "runtime_risk_evidence": risk}


def _review_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(handoff)
    normalized.pop("generated_at", None)
    return normalized


def _review_health_plan(plan: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(plan)
    normalized.pop("generated_at", None)
    manifest_binding = normalized.get("manifest_binding")
    if isinstance(manifest_binding, dict):
        manifest_binding.pop("sha256", None)
    return normalized


def _review_health_selection(selection: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(selection)
    normalized.pop("generated_at", None)
    return normalized


def _load_historical(fixture_dir: pathlib.Path = DEFAULT_FIXTURE_DIR) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for filename, expected_sha in FIXTURE_SHA256.items():
        path = fixture_dir / filename
        if not path.is_file():
            raise ValueError(f"historical review proof is missing {path}")
        data = path.read_bytes()
        if sha256_bytes(data) != expected_sha:
            raise ValueError(f"historical review proof was modified: {filename}")
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"historical review proof {filename} must contain an object")
        values[filename] = value
    return values


def verify_historical_proof(fixture_dir: pathlib.Path = DEFAULT_FIXTURE_DIR) -> dict[str, Any]:
    """Validate archived review cross-bindings; tampering is a hard failure."""
    historical = _load_historical(fixture_dir)
    decision = historical["decision.json"]
    decision_body = _object(decision.get("decision"), "historical decision.decision")
    if decision_body.get("accepted") is not True or decision_body.get("decision_status") != "accepted":
        raise ValueError("pinned historical decision is not an accepted review")
    if decision_body.get("handoff_sha256") != FIXTURE_SHA256["handoff.json"]:
        raise ValueError("pinned historical decision does not bind its archived handoff")
    old_compatibility = historical["compatibility.json"]
    if decision_body.get("compatibility_sha256") != compatibility_binding_sha256(old_compatibility):
        raise ValueError("pinned historical decision does not bind archived compatibility")
    old_source = _source_contract(historical["manifest.json"])
    if old_source != {
        "path": HISTORICAL_SOURCE_PATH,
        "bytes": HISTORICAL_SOURCE_BYTES,
        "sha256": HISTORICAL_SOURCE_SHA256,
    }:
        raise ValueError("pinned historical manifest source binding does not match the reviewed source")
    old_plan = historical["health-plan.json"]
    historical_scope = {
        "scope_version": REVIEW_SCOPE_VERSION,
        "source_registry": old_source,
        "compatibility_sha256": compatibility_review_binding_sha256(old_compatibility),
        "handoff_sha256": FIXTURE_SHA256["handoff.json"],
        "health_plan_sha256": canonical_sha256(_review_health_plan(old_plan)),
        "health_selection_sha256": canonical_sha256(_review_health_selection(historical["health-selection.json"])),
    }
    source_ref = _object(old_plan.get("source_registry"), "historical health plan.source_registry")
    selection_ref = _object(old_plan.get("selection_policy"), "historical health plan.selection_policy")
    if old_plan.get("registry_revision") != HISTORICAL_DECISION_COMMIT:
        raise ValueError("historical health plan must point to the reviewed registry revision")
    if source_ref.get("path") != HISTORICAL_SOURCE_PATH or source_ref.get("sha256") != HISTORICAL_SOURCE_SHA256:
        raise ValueError("historical Health plan source must match the reviewed registry")
    if selection_ref.get("sha256") != FIXTURE_SHA256["health-selection.json"]:
        raise ValueError("historical Health plan does not bind the archived selection policy")
    return {
        "decision_sha256": FIXTURE_SHA256["decision.json"],
        "compatibility_sha256": canonical_sha256(_review_risk_and_summary(old_compatibility)),
        "handoff_sha256": canonical_sha256(_review_handoff(historical["handoff.json"])),
        "source_registry": old_source,
        "health_plan_sha256": canonical_sha256(_review_health_plan(old_plan)),
        "health_selection_sha256": canonical_sha256(_review_health_selection(historical["health-selection.json"])),
        "review_scope_version": REVIEW_SCOPE_VERSION,
        "review_scope_sha256": canonical_sha256(historical_scope),
        "review_scope": historical_scope,
        "historical_decision_compatibility_sha256": decision_body["compatibility_sha256"],
        "historical_reviewed_at": decision_body.get("reviewed_at"),
        "historical_expires_at": decision_body.get("expires_at"),
        "reviewed_commit": HISTORICAL_DECISION_COMMIT,
        "health_selection_commit": HEALTH_SELECTION_COMMIT,
    }


def compatibility_review_binding_sha256(compatibility: dict[str, Any]) -> str:
    """Stable digest for all compatibility semantics, excluding derived outputs only."""
    normalized = copy.deepcopy(compatibility)
    risk = _object(normalized.get("runtime_risk_evidence"), "compatibility.runtime_risk_evidence")
    for key in DERIVED_ACCEPTANCE_FIELDS | DECISION_MIRROR_FIELDS:
        risk.pop(key, None)
    return compatibility_binding_sha256(normalized)


def review_scope_document(
    *,
    compatibility: dict[str, Any],
    handoff_sha256: str,
    manifest: dict[str, Any],
    health_plan: dict[str, Any],
    health_selection: dict[str, Any],
) -> dict[str, Any]:
    return {
        "scope_version": REVIEW_SCOPE_VERSION,
        "source_registry": _source_contract(manifest),
        "compatibility_sha256": compatibility_review_binding_sha256(compatibility),
        "handoff_sha256": handoff_sha256,
        "health_plan_sha256": canonical_sha256(_review_health_plan(health_plan)),
        "health_selection_sha256": canonical_sha256(_review_health_selection(health_selection)),
    }


def review_scope_sha256(**kwargs: Any) -> str:
    return canonical_sha256(review_scope_document(**kwargs))


def _parse_utc(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError(f"{label} must include a UTC timezone")
    return parsed.astimezone(dt.timezone.utc)


def evaluate_review_scope(
    *,
    decision: dict[str, Any],
    decision_sha256: str,
    compatibility: dict[str, Any],
    handoff: dict[str, Any],
    handoff_sha256: str,
    manifest: dict[str, Any],
    health_plan: dict[str, Any],
    health_selection: dict[str, Any],
    as_of: dt.datetime | None = None,
    fixture_dir: pathlib.Path = DEFAULT_FIXTURE_DIR,
) -> dict[str, Any]:
    """Compare current evidence with the pinned human review and decide effectiveness.

    ``as_of`` exists for deterministic boundary tests. Production callers should
    omit it so each release/admission boundary uses fresh UTC system time.
    """
    reviewed = verify_historical_proof(fixture_dir)
    source = _source_contract(manifest)
    scope_document = review_scope_document(
        compatibility=compatibility,
        handoff_sha256=handoff_sha256,
        manifest=manifest,
        health_plan=health_plan,
        health_selection=health_selection,
    )
    current = {
        "compatibility_sha256": canonical_sha256(_review_risk_and_summary(compatibility)),
        "source_registry": source,
        "handoff_sha256": canonical_sha256(_review_handoff(handoff)),
        "health_plan_sha256": canonical_sha256(_review_health_plan(health_plan)),
        "health_selection_sha256": canonical_sha256(_review_health_selection(health_selection)),
        "decision_sha256": decision_sha256,
        "compatibility_binding_sha256": compatibility_binding_sha256(compatibility),
        "compatibility_review_binding_sha256": compatibility_review_binding_sha256(compatibility),
        "handoff_file_sha256": handoff_sha256,
        "review_scope_version": REVIEW_SCOPE_VERSION,
        "review_scope_sha256": canonical_sha256(scope_document),
        "review_scope": scope_document,
    }
    reason_codes: list[str] = []
    if source != reviewed["source_registry"]:
        reason_codes.append("source_registry_changed")
    historical = _load_historical(fixture_dir)
    old_compatibility = historical["compatibility.json"]
    old_summary = old_compatibility.get("summary")
    new_summary = compatibility.get("summary")
    old_risk = _review_risk_and_summary(old_compatibility)["runtime_risk_evidence"]
    new_risk = _review_risk_and_summary(compatibility)["runtime_risk_evidence"]
    if old_risk != new_risk:
        reason_codes.append("runtime_risk_semantics_changed")
    if old_summary != new_summary:
        reason_codes.append("compatibility_summary_changed")
    if (
        compatibility_review_binding_sha256(compatibility)
        != reviewed["review_scope"]["compatibility_sha256"]
    ):
        reason_codes.append("compatibility_scope_changed")
    old_handoff = historical["handoff.json"]
    if _review_handoff(handoff) != _review_handoff(old_handoff):
        reason_codes.append("review_handoff_semantics_changed")
    old_health = historical["health-plan.json"]
    old_selection = historical["health-selection.json"]
    if (
        _review_health_plan(health_plan) != _review_health_plan(old_health)
        or _review_health_selection(health_selection) != _review_health_selection(old_selection)
    ):
        reason_codes.append("health_observation_scope_changed")

    decision_body = _object(decision.get("decision"), "decision.decision")
    accepted = decision_body.get("accepted") is True and decision_body.get("decision_status") == "accepted"
    expires_at = decision_body.get("expires_at")
    expired = False
    clock = as_of or dt.datetime.now(dt.timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() != dt.timedelta(0):
        raise ValueError("as_of must include a UTC timezone")
    clock = clock.astimezone(dt.timezone.utc)
    if accepted:
        expiration = _parse_utc(expires_at, "decision.expires_at")
        reviewed_at = _parse_utc(decision_body.get("reviewed_at"), "decision.reviewed_at")
        if reviewed_at >= expiration:
            raise ValueError("decision.reviewed_at must be earlier than decision.expires_at")
        if reviewed_at > clock:
            raise ValueError("decision.reviewed_at must not be in the future")
        expired = clock >= expiration
        if expired:
            reason_codes.append("manual_review_decision_expired")
    else:
        reason_codes.append("manual_review_decision_not_accepted")

    historical_decision = decision_sha256 == reviewed["decision_sha256"]
    if historical_decision:
        if decision != _load_historical(fixture_dir)["decision.json"]:
            raise ValueError("historical manual-review decision must remain byte-for-byte unchanged")
        direct_scope = not reason_codes
        explicitly_revalidated = False
    else:
        # A replacement decision is a new human assertion. It must bind the
        # current handoff and stable review semantics, not an artifact-derived
        # compatibility digest that changes when this acceptance report updates.
        reviewer_fields_ok = all(
            isinstance(decision_body.get(key), str) and bool(decision_body.get(key).strip())
            for key in ("reviewer", "reviewed_at", "reason")
        )
        direct_scope = (
            accepted
            and reviewer_fields_ok
            and decision_body.get("handoff_sha256") == handoff_sha256
            and decision_body.get("review_scope_version") == REVIEW_SCOPE_VERSION
            and decision_body.get("review_scope_sha256") == current["review_scope_sha256"]
        )
        explicitly_revalidated = direct_scope
        if accepted and not direct_scope:
            reason_codes.append("current_decision_not_bound_to_review_scope")

    if explicitly_revalidated and not expired:
        scope_status = "explicitly_revalidated"
        effective_accepted = True
        reason_codes = ["explicit_current_human_review"]
    elif not accepted:
        scope_status = "unproven" if not historical_decision else "revalidation_required"
        effective_accepted = False
    elif not historical_decision and not direct_scope:
        scope_status = "unproven"
        effective_accepted = False
    elif reason_codes:
        scope_status = "revalidation_required"
        effective_accepted = False
    else:
        scope_status = "unchanged"
        effective_accepted = not expired

    if not historical_decision and explicitly_revalidated and expired:
        scope_status = "revalidation_required"
        effective_accepted = False
        if "manual_review_decision_expired" not in reason_codes:
            reason_codes.append("manual_review_decision_expired")

    return {
        "scope_version": REVIEW_SCOPE_VERSION,
        "historical_decision_valid": True,
        "historical_decision": historical_decision,
        "scope_status": scope_status,
        "effective_accepted": effective_accepted,
        "reason_codes": sorted(set(reason_codes)),
        "decision_expires_at": expires_at if accepted else None,
        "decision_expired": expired,
        "reviewed_binding": reviewed,
        "current_binding": current,
    }
