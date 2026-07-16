#!/usr/bin/env python3
"""Generate or check the non-authoritative diagnostic contract release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
DRAFT = ROOT / "drafts/diagnostic-envelope"
DEFAULT_INTAKE = DRAFT / "release-candidate/consumer-proof-intake.v1.json"
DEFAULT_OUTPUT = DRAFT / "release-candidate/diagnostic-release-candidate.v1.json"
CONTRACTS = (
    "drafts/diagnostic-envelope/datapan.diagnostic-envelope.v1.schema.json",
    "drafts/diagnostic-envelope/consumer-contract.v1.json",
    "drafts/diagnostic-envelope/data-go-kr-evidence-mapping.v1.json",
    "drafts/diagnostic-envelope/consumer-compatibility/datapan-cli.v1.json",
    "drafts/diagnostic-envelope/consumer-compatibility/datapan-health.v1.json",
    "drafts/diagnostic-envelope/consumer-compatibility/datapan-web.v1.json",
)
PROOF_ROOT = DRAFT / "release-candidate/proofs"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")

EXPECTED_HEALTH_IDENTITY = {
    "head_commit": "4e8e1fc1c2af3ef63a59abde8c3fc52387355bb8",
    "tested_revision": "c51a3442f6438272c58f02756ed0af40f939f9bc",
    "ci_run": 29481036102,
    "bindings_sha256": "2ca4039da73c61dcb2aceab305c98154ec30363f261f638d2a6e79a9fa8c79b0",
    "test_manifest_sha256": "274d394133eb90fe5553bb47947644d45f338ad2e193345e13759f7bb9e2619b",
}
EXPECTED_HEALTH_BINDINGS = (
    ("dpr-op-00000001", "15000480", "public-data_holiday-emergency-clinics"),
    ("dpr-op-00000002", "15000897", "public-data_election-codes"),
    ("dpr-op-00000003", "15001697", "public-data_medical-institution-codes"),
    ("dpr-op-00000004", "15001839", "public-data_private-resource-services"),
    ("dpr-op-00000005", "15000863", "public-data_culture-facility-restaurants"),
    ("dpr-op-00000006", "15025329", "public-data_qnet-practical-pass-rate"),
    ("dpr-op-00000007", "15004206", "public-data_weather-nearby-realtime"),
    ("dpr-op-00000008", "15052669", "public-data_transit-card-chargers"),
    ("dpr-op-00000009", "15109030", "public-data_bus-depot-status"),
    ("dpr-op-00000010", "15029006", "public-data_university-majors"),
)
EXPECTED_HEALTH_SOURCES = (
    ("internal/health/diagnostic_test.go", "328612400e99fb8cf1989738eb073c809ae1f7b425a002ad7ddf75e4bead499f"),
    ("internal/health/health_test.go", "895fb5c3dd531dca8288d047e9029b26c01d7c1dba1027df5ce813dfa3c2a387"),
)
EXPECTED_HEALTH_TESTS = (
    ("TestAcceptedDiagnosticFixturesMatchExactRegistryContract", "internal/health/diagnostic_test.go"),
    ("TestDiagnosticCauseCannotChangeGatusProjection", "internal/health/diagnostic_test.go"),
    ("TestDiagnosticCompatibilityCLIRejectsNonBijectiveCanaryFiles", "internal/health/diagnostic_test.go"),
    ("TestDiagnosticCompatibilityReceiptBindsHeadContractsFixturesAndServices", "internal/health/diagnostic_test.go"),
    ("TestDiagnosticCompatibilityRejectsNonBijectiveOrIncompleteServiceMap", "internal/health/diagnostic_test.go"),
    ("TestDiagnosticDecoderFailsClosedForUnknownVersionEnumAndExtraField", "internal/health/diagnostic_test.go"),
    ("TestDiagnosticPinRejectsRevisionAndArtifactDrift", "internal/health/diagnostic_test.go"),
    ("TestDiagnosticProducerBoundaryRejectsEveryRedactionLeakClass", "internal/health/diagnostic_test.go"),
    ("TestDiagnosticSubjectBindsExactlyOnceToConfiguredService", "internal/health/diagnostic_test.go"),
    ("TestDiagnosticSubjectRejectsUnknownCrossOperationDuplicateAndStaleRevision", "internal/health/diagnostic_test.go"),
    ("TestDiagnosticTestManifestRejectsSourceDriftAndReceiptIsReproducible", "internal/health/diagnostic_test.go"),
    ("TestPinnedSchemaAndCLIStyleFixturesAreCompatible", "internal/health/health_test.go"),
)

EXPECTED_WEB_IDENTITY = {
    "head_commit": "79da6545df44b8d3933e482bb86895028522de73",
    "schema_sha256": "da254b40947462347fcda90fdd7686b6632c76943b438f2046a28f079f33e403",
    "consumer_contract_sha256": "02146e5cbc84a4f7e9b6883ff049c62b7f188cccd731c300e762395b486483a5",
}
EXPECTED_WEB_JOURNEYS = (
    "approval_entry", "approval_propagation", "credential_repair", "input_repair",
    "rate_limit_recovery", "provider_outage_evidence", "http_success_quality",
)
EXPECTED_WEB_SOURCES = (
    ("src/diagnostics/decoder.test.ts", "961b0fd04885dfb102ecde6a8bf411987539625ff28b99ec104a4f0e52322626"),
    ("src/diagnostics/decoder.ts", "aa6885905019c272a60e619f347a3846bd09d608007d177f0adf28e4ddb45bfa"),
    ("src/diagnostics/domain.ts", "6665578c9a0e01911633ccd175b515d18adfe8dcd22c1cece4bbd6e87c35c35d"),
    ("src/diagnostics/healthEvidenceAdapter.test.ts", "0d1667bc8573d7e4d36567b18adb7d36f7a225afa05c26ca9e56380957847bba"),
    ("src/diagnostics/healthEvidenceAdapter.ts", "28b2aedc31b80b8b4a64f21d7e214fe6c3a0bfbbfd8accdf3110e2b117e3e101"),
    ("src/diagnostics/journeyContract.test.ts", "dc96a29bca81eae8af329cb540a7dede80dee4feb39b7dd836003a091d271814"),
    ("src/diagnostics/journeyContract.ts", "5583b9fe01175132c317f3ec206569e5caa6112b7f070af21ad021b3550cfc11"),
    ("src/diagnostics/operationEvidenceAdapter.test.ts", "a8bc940a887b6754a3f5e0023054a88d0277d9b49bb101d42abcb23f60801d97"),
    ("src/diagnostics/operationEvidenceAdapter.ts", "a9e17d510dd72670b7fa37977b3c39cd41edf7dac15c6d0654fdba77f436c1ba"),
)
EXPECTED_CLI_IDENTITY = {
    "head_commit": "1800bef05c62c918b34a430d8d703ce2ed1afc1f",
    "merged_main_commit": "416d568bc21632c4305666ea9f6ef2327b5f627b",
    "ci_run": 29483717200,
    "registry_journey_run": 29483717293,
    "gira_finish_receipt_comment": 4989994299,
    "schema_sha256": "da254b40947462347fcda90fdd7686b6632c76943b438f2046a28f079f33e403",
    "mapping_sha256": "da55d52d2ee1f197969ac63a1d5ab5b98e3b88fd65f90d6a48800d2e3c522d33",
}
EXPECTED_CLI_SOURCES = (
    ("internal/cli/diagnostic_contract.go", "f4b192424def2a4024990558fb3235159055063e9fed0ac5a41251455027af2c"),
    ("internal/cli/diagnostic_contract_test.go", "9ab4695f93e96621e3bbcb62f5ecdeb0d4c550afe320c1ce1d4c33959a2a056f"),
    ("internal/cli/diagnostic.go", "dafbdfe868f9fdd87e64743e3cdeee927b687fafb8bbc214c1208f8a21661c24"),
    ("internal/cli/diagnostic_test.go", "de2aa13c189aa0baff147803f28c2368b891d2d053b62b19c3f236de12d9da1a"),
    ("internal/cli/cli.go", "36227e506ef06290d223a015e381d4d656edab06f7f3ad0de0b1b879bd14c0da"),
    ("docs/diagnostic-consumer-handoff.md", "6dbff4c9a0a6414a3b8a9341e5f887f888b0ec50f3c3661cf35b44e3e6fdfeed"),
    ("docs/cli-contract.md", "8b02f285172601d9b687735225ccbeb1790930f4f4a1f86db726708bdc595356"),
)
EXPECTED_FIXTURE_IDENTITIES = (
    ("approval-propagating.json", "fe000f4082f948d6a96f045d7fae91c6bdf7288c6746196a8c7b0868d6416099", "approval_propagating", "inferred"),
    ("approval-required.json", "d11fc4e18aee6fe1a7f5c9c0a94a1e6e1bae0177447f2f6bcc0dddbe6961e7d3", "approval_required", "observed"),
    ("contract-drift.json", "13bc8af8c6b1540ef91a49f60ed9aab5514fabee3316868dc9f946adbe1da470", "contract_drift", "inferred"),
    ("credential-invalid.json", "c5796d7bf59c6f282f9f75b14717a51ff716859e06f6325e784c48d507816497", "credential_invalid", "observed"),
    ("invalid-input.json", "80adb4fce6ede5c34223468bf26b69e90c20c92d74661777c458a5238ad6ab07", "invalid_input", "observed"),
    ("provider-outage.json", "33c0160c4cf136b34dc3befa1ff5803c71f3c37d7946fa2b56e70c69d4be6200", "provider_outage", "inferred"),
    ("rate-limited.json", "8be4fb69e91ae42c2a03510458b9b9fd23cf4780d551ea1ddaa505c8bc40d318", "rate_limited", "observed"),
    ("ready.json", "7ae2306176bdd007ca2a4ca822240e4e515c76967b2af0c816584463aee420fc", "ready", "observed"),
    ("semantic-quality.json", "06fcc308aa039861b38f6da2fee8f23150ea7d2cac998b5d9e05b011ca1ca9b0", "semantic_quality", "inferred"),
    ("stale-data.json", "f4cc2d0f34bdfb74bbed9a3bcb5cad7b0f5444fa5ded6ca1742c916799aecf92", "stale_data", "observed"),
    ("unknown.json", "e0635cf4980438141007607c66eca821383f605393a69bdb03522b3873c1dcf0", "unknown", "unknown"),
)


def load(path: pathlib.Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def artifact(path: str) -> dict[str, str]:
    absolute = ROOT / path
    return {"path": path, "sha256": sha256_bytes(absolute.read_bytes())}


def expected_fixtures() -> list[dict[str, str]]:
    records = [
        {"name": name, "sha256": digest, "cause": cause, "determination": determination}
        for name, digest, cause, determination in EXPECTED_FIXTURE_IDENTITIES
    ]
    actual = []
    for path in sorted((DRAFT / "fixtures").glob("*.json")):
        fixture = load(path)
        actual.append({
            "name": path.name,
            "sha256": sha256_bytes(path.read_bytes()),
            "cause": fixture["cause"]["code"],
            "determination": fixture["cause"]["determination"],
        })
    if actual != records:
        raise ValueError("diagnostic contract fixture identity drift")
    return records


def validate_health_proof(proof: dict[str, Any], item: dict[str, Any], registry: dict[str, Any]) -> None:
    if proof.get("schema_version") != "datapan.health-diagnostic-compatibility-receipt.v1":
        raise ValueError("datapan-health: unsupported machine proof schema")
    if (
        item.get("repository") != "StatPan/datapan-health"
        or item.get("issue") != 19
        or item.get("pull_request") != 23
        or item.get("review_state") != "independent_approved"
        or item.get("ci_state") != "passed"
        or item.get("head_commit") != EXPECTED_HEALTH_IDENTITY["head_commit"]
        or item.get("ci_run") != EXPECTED_HEALTH_IDENTITY["ci_run"]
        or proof.get("status") != "consumer_compatible"
        or proof.get("health_head") != EXPECTED_HEALTH_IDENTITY["head_commit"]
        or proof.get("health_head") != item.get("head_commit")
    ):
        raise ValueError("datapan-health: receipt status or exact head mismatch")
    if proof.get("tested_revision") != EXPECTED_HEALTH_IDENTITY["tested_revision"]:
        raise ValueError("datapan-health: tested_revision identity mismatch")
    if proof.get("registry_revision") != registry.get("contract_commit"):
        raise ValueError("datapan-health: receipt is not bound to the candidate Registry revision")
    contracts = proof.get("contracts", {})
    expected_contracts = {
        "schema": ("diagnostic/datapan.diagnostic-envelope.v1.schema.json", artifact(CONTRACTS[0])["sha256"]),
        "mapping": ("diagnostic/data-go-kr-evidence-mapping.v1.json", artifact(CONTRACTS[2])["sha256"]),
        "consumer": ("diagnostic/datapan-health.v1.json", artifact(CONTRACTS[4])["sha256"]),
    }
    for name, (path, digest) in expected_contracts.items():
        if contracts.get(name) != {"path": path, "sha256": digest}:
            raise ValueError(f"datapan-health: {name} contract identity mismatch")

    if proof.get("fixtures") != expected_fixtures():
        raise ValueError("datapan-health: fixture proof is not the exact 11-artifact contract")

    bindings = proof.get("bindings")
    expected_bindings = [
        {"operation_id": operation, "dataset_id": dataset, "service_id": service,
         "registry_revision": registry.get("contract_commit")}
        for operation, dataset, service in EXPECTED_HEALTH_BINDINGS
    ]
    if bindings != expected_bindings:
        raise ValueError("datapan-health: exact operation binding identity mismatch")
    encoded_bindings = json.dumps(bindings, ensure_ascii=False, separators=(",", ":")).encode()
    if (
        proof.get("bindings_sha256") != EXPECTED_HEALTH_IDENTITY["bindings_sha256"]
        or proof.get("bindings_sha256") != sha256_bytes(encoded_bindings)
    ):
        raise ValueError("datapan-health: bindings digest mismatch")
    test_proof = proof.get("test_proof", {})
    tests = test_proof.get("tests")
    expected_tests = [{"name": name, "source_path": source} for name, source in EXPECTED_HEALTH_TESTS]
    expected_sources = [{"path": path, "sha256": digest} for path, digest in EXPECTED_HEALTH_SOURCES]
    if (
        test_proof.get("count") != 12
        or tests != expected_tests
        or test_proof.get("sources") != expected_sources
        or test_proof.get("manifest") != {
            "path": "diagnostic-test-manifest.json",
            "sha256": EXPECTED_HEALTH_IDENTITY["test_manifest_sha256"],
        }
    ):
        raise ValueError("datapan-health: exact test, manifest, or source identity mismatch")
    boundaries = proof.get("boundaries")
    if boundaries != {
        "existing_health_probe_v1": "preserved",
        "gatus_projection": "unchanged_enum_only",
        "sensitive_evidence": "rejected_before_normalization",
        "public_api": "not_implemented",
        "deployment": "not_performed",
    }:
        raise ValueError("datapan-health: compatibility boundaries mismatch")


def validate_web_proof(proof: dict[str, Any], item: dict[str, Any], registry: dict[str, Any]) -> None:
    if proof.get("schema_version") != "datapan.web-diagnostic-compatibility-receipt.v1":
        raise ValueError("datapan-web: unsupported machine proof schema")
    if (
        item.get("repository") != "StatPan/datapan"
        or item.get("issue") != 8
        or item.get("pull_request") != 12
        or item.get("review_state") != "independent_approved"
        or item.get("ci_state") != "machine_receipt_local_passed_no_hosted_ci"
        or proof.get("status") != "consumer_compatible_prepublication"
        or proof.get("web_head") != EXPECTED_WEB_IDENTITY["head_commit"]
        or item.get("head_commit") != EXPECTED_WEB_IDENTITY["head_commit"]
        or proof.get("web_head") != item.get("head_commit")
        or proof.get("registry_revision") != registry.get("contract_commit")
    ):
        raise ValueError("datapan-web: receipt status, exact head, or Registry revision mismatch")
    expected_contracts = {
        "schema": {
            "path": "src/diagnostics/contracts/datapan.diagnostic-envelope.v1.schema.json",
            "sha256": EXPECTED_WEB_IDENTITY["schema_sha256"],
        },
        "consumer": {
            "path": "tests/fixtures/diagnostic-envelope/consumer-contract.v1.json",
            "sha256": EXPECTED_WEB_IDENTITY["consumer_contract_sha256"],
        },
    }
    if proof.get("contracts") != expected_contracts or proof.get("fixtures") != expected_fixtures():
        raise ValueError("datapan-web: exact contract or 11-fixture identity mismatch")
    verification = proof.get("verification", {})
    if verification != {
        "diagnosis_codes": 11,
        "fixture_cause_action_redaction": "exact",
        "journeys": list(EXPECTED_WEB_JOURNEYS),
        "journey_count": 7,
        "test_command": "npm run check",
        "tests_passed": 55,
        "production_build": "passed",
        "audit_command": "npm audit --audit-level=high",
        "audit_vulnerabilities": 0,
    }:
        raise ValueError("datapan-web: exact code, journey, build, test, or audit proof mismatch")
    expected_sources = [{"path": path, "sha256": digest} for path, digest in EXPECTED_WEB_SOURCES]
    if proof.get("sources") != expected_sources:
        raise ValueError("datapan-web: exact diagnostic source identity mismatch")
    if proof.get("package_identity") != {
        "package_json_sha256": "7200e707c8449ac791179ffe7f824ab879b2b2e80ba45ac30d27a0fbd4916566",
        "package_lock_sha256": "a596c00dd6de8aeab7b2f1979537e65678505922846f99df4628f33e4a23a234",
    }:
        raise ValueError("datapan-web: package identity mismatch")
    if proof.get("rollout") != {
        "prepublication_compatibility": "accepted",
        "immutable_registry_release_manifest_consumption": "post_publication_required",
        "runtime_authority_before_publication": False,
    }:
        raise ValueError("datapan-web: pre-publication and post-publication gates are not separated")


def validate_cli_proof(proof: dict[str, Any], item: dict[str, Any], registry: dict[str, Any]) -> None:
    if proof.get("schema_version") != "datapan.cli-diagnostic-compatibility-receipt.v1":
        raise ValueError("datapan-cli: unsupported machine proof schema")
    if (
        item.get("repository") != "StatPan/datapan-cli"
        or item.get("issue") != 160
        or item.get("pull_request") != 162
        or item.get("review_state") != "independent_approved"
        or item.get("ci_state") != "passed"
        or proof.get("status") != "consumer_compatible_prepublication"
        or proof.get("cli_head") != EXPECTED_CLI_IDENTITY["head_commit"]
        or item.get("head_commit") != EXPECTED_CLI_IDENTITY["head_commit"]
        or item.get("ci_run") != EXPECTED_CLI_IDENTITY["ci_run"]
        or proof.get("registry_revision") != registry.get("contract_commit")
    ):
        raise ValueError("datapan-cli: receipt status, exact head, CI, or Registry revision mismatch")
    if proof.get("contracts") != {
        "schema": {"path": "internal/cli/testdata/diagnostic-envelope/schema.json", "sha256": EXPECTED_CLI_IDENTITY["schema_sha256"]},
        "mapping": {"path": "internal/cli/testdata/diagnostic-envelope/mapping.json", "sha256": EXPECTED_CLI_IDENTITY["mapping_sha256"]},
    } or proof.get("fixtures") != expected_fixtures():
        raise ValueError("datapan-cli: exact contract or 11-fixture identity mismatch")
    if proof.get("lifecycle") != {
        "approved_source_head": EXPECTED_CLI_IDENTITY["head_commit"],
        "merged_main_commit": EXPECTED_CLI_IDENTITY["merged_main_commit"],
        "issue_state": "closed_done",
        "gira_finish_receipt_comment": EXPECTED_CLI_IDENTITY["gira_finish_receipt_comment"],
        "ordinary_ci_run": EXPECTED_CLI_IDENTITY["ci_run"],
        "registry_journey_run": EXPECTED_CLI_IDENTITY["registry_journey_run"],
        "registry_journey_attempt": 2,
        "registry_journey_operating_systems": ["ubuntu", "macos", "windows"],
    }:
        raise ValueError("datapan-cli: approved source, merged main, or Gira lifecycle identity mismatch")
    if proof.get("verification") != {
        "real_run_journeys": 7,
        "runtime_owned_failure_metrics": True,
        "time_to_diagnosis_serialized": True,
        "time_to_first_success_serialized": True,
        "json_export": "passed",
        "csv_write_boundary": "writeCSV_exact_value_newline_1_newline",
        "consumed_ignored_unsupported_fields": "documented_exact",
        "mapping_and_redaction": "passed",
        "local_commands": [
            "go test -count=1 ./...", "go vet ./...", "go build ./cmd/datapan ./cmd/dp", "git diff --check",
        ],
        "ordinary_ci_operating_systems": ["ubuntu", "macos", "windows"],
    }:
        raise ValueError("datapan-cli: exact journey, metric, export, mapping, or build proof mismatch")
    if proof.get("sources") != [{"path": path, "sha256": digest} for path, digest in EXPECTED_CLI_SOURCES]:
        raise ValueError("datapan-cli: exact source and handoff identity mismatch")
    if proof.get("rollout") != {
        "prepublication_compatibility": "accepted",
        "anonymous_registry_distribution": "verified_by_exact_head_3os_ci_separate_gate",
        "runtime_authority_before_publication": False,
    }:
        raise ValueError("datapan-cli: compatibility and anonymous distribution gates are not separated")


def validate_machine_proof(item: dict[str, Any], registry: dict[str, Any]) -> None:
    consumer = item["consumer"]
    reference = item.get("machine_proof")
    if not isinstance(reference, dict):
        raise ValueError(f"{consumer}: accepted proof requires a machine proof artifact")
    relative = reference.get("path")
    if not isinstance(relative, str):
        raise ValueError(f"{consumer}: machine proof path is required")
    path = (ROOT / relative).resolve()
    proof_root = PROOF_ROOT.resolve()
    if proof_root not in path.parents or path.suffix != ".json" or not path.is_file():
        raise ValueError(f"{consumer}: machine proof must be a checked-in JSON artifact under {PROOF_ROOT.relative_to(ROOT)}")
    data = path.read_bytes()
    if (
        not isinstance(reference.get("bytes"), int)
        or reference["bytes"] <= 0
        or not SHA256_PATTERN.fullmatch(str(reference.get("sha256", "")))
        or reference.get("bytes") != len(data)
        or reference.get("sha256") != sha256_bytes(data)
    ):
        raise ValueError(f"{consumer}: machine proof byte identity mismatch")
    if item.get("receipt_sha256") is not None and item.get("receipt_sha256") != reference.get("sha256"):
        raise ValueError(f"{consumer}: receipt and machine proof digest mismatch")
    proof = load(path)
    if reference.get("schema_version") != proof.get("schema_version"):
        raise ValueError(f"{consumer}: machine proof schema identity mismatch")
    validators = {
        "datapan-health": validate_health_proof,
        "datapan-web": validate_web_proof,
        "datapan-cli": validate_cli_proof,
    }
    validator = validators.get(consumer)
    if validator is None:
        raise ValueError(f"{consumer}: accepted proof has no consumer-specific semantic validator")
    validator(proof, item, registry)


def validate_intake(consumers: list[dict[str, Any]], registry: dict[str, Any]) -> None:
    for item in consumers:
        consumer = item.get("consumer", "<unknown>")
        head = item.get("head_commit")
        if not isinstance(head, str) or len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
            raise ValueError(f"{consumer}: head_commit must be a full lowercase Git commit")
        state = item.get("proof_state")
        missing = item.get("missing_proofs")
        if state not in {"accepted", "partial", "blocked"}:
            raise ValueError(f"{consumer}: unsupported proof_state {state!r}")
        if not isinstance(missing, list):
            raise ValueError(f"{consumer}: missing_proofs must be a list")
        if state == "accepted":
            if missing:
                raise ValueError(f"{consumer}: accepted proof cannot have missing proofs")
            if item.get("ci_state") not in {"passed", "machine_receipt_local_passed_no_hosted_ci"}:
                raise ValueError(f"{consumer}: accepted proof requires passed exact-head verification")
            if item.get("review_state") != "independent_approved":
                raise ValueError(f"{consumer}: accepted proof requires independent exact-head approval")
            validate_machine_proof(item, registry)
        elif not missing:
            raise ValueError(f"{consumer}: incomplete proof must name its missing proofs")


def validate_publication_gates(gates: Any) -> list[dict[str, str]]:
    if not isinstance(gates, dict) or set(gates) != {"anonymous_registry_distribution"}:
        raise ValueError("intake must define the anonymous Registry distribution publication gate")
    distribution = gates["anonymous_registry_distribution"]
    expected = {
        "status": "passed",
        "provider": "hugging_face",
        "ci_run": 29483717293,
        "result": "registry_journey_passed_exact_head_3os",
        "attempt": 2,
        "operating_systems": ["ubuntu", "macos", "windows"],
        "runtime_authority": False,
        "publishing_allowed": False,
    }
    if distribution != expected:
        raise ValueError("anonymous Registry distribution gate identity mismatch")
    return []


def build(intake: dict[str, Any]) -> dict[str, Any]:
    consumers = intake.get("consumers")
    if not isinstance(consumers, list):
        raise ValueError("intake.consumers must be a list")
    if len(consumers) != 3:
        raise ValueError("intake must contain exactly three consumer records")
    states = {item.get("consumer"): item.get("proof_state") for item in consumers}
    if len(states) != 3 or set(states) != {"datapan-cli", "datapan-health", "datapan-web"}:
        raise ValueError("intake must contain exactly the three required consumers")
    registry = intake.get("registry")
    if not isinstance(registry, dict) or not COMMIT_PATTERN.fullmatch(str(registry.get("contract_commit", ""))):
        raise ValueError("intake.registry must bind an exact contract commit")
    validate_intake(consumers, registry)
    missing_publication_gates = validate_publication_gates(intake.get("publication_gates"))
    missing = [
        {"consumer": item["consumer"], "proof": proof}
        for item in consumers
        for proof in item.get("missing_proofs", [])
    ]
    all_accepted = all(state == "accepted" for state in states.values())
    all_publication_gates_passed = not missing_publication_gates
    ready = all_accepted and all_publication_gates_passed
    binding = {
        "registry": registry,
        "contracts": [artifact(path) for path in CONTRACTS],
        "consumer_proofs": consumers,
        "publication_gates": intake["publication_gates"],
    }
    binding_sha256 = sha256_bytes(render(binding).encode())
    return {
        "schema_version": "datapan.diagnostic-release-candidate.v1",
        "status": "ready_for_publication_review" if ready else "blocked",
        "captured_at": intake.get("captured_at"),
        "authority": {
            "release_authority": False,
            "runtime_authority": False,
            "fixture_runtime_authority": False,
            "manifest_inclusion": False,
            "publishing_allowed": False,
        },
        "binding_sha256": binding_sha256,
        "binding": binding,
        "decision": {
            "required_consumers": ["datapan-cli", "datapan-health", "datapan-web"],
            "accepted_consumers": sorted(name for name, state in states.items() if state == "accepted"),
            "all_consumers_accepted": all_accepted,
            "missing_proofs": missing,
            "publication_gates": intake["publication_gates"],
            "all_publication_gates_passed": all_publication_gates_passed,
            "missing_publication_gates": missing_publication_gates,
            "next_gate": (
                "independent_publication_review" if ready
                else "collect_missing_consumer_proofs" if not all_accepted
                else "restore_anonymous_registry_distribution"
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intake", type=pathlib.Path, default=DEFAULT_INTAKE)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        candidate = build(load(args.intake))
        rendered = render(candidate)
        if args.check:
            if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
                raise ValueError(f"{args.output} is stale; regenerate it")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL diagnostic release candidate: {exc}", file=sys.stderr)
        return 1
    print(f"ok diagnostic release candidate ({candidate['status']}, binding={candidate['binding_sha256']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
