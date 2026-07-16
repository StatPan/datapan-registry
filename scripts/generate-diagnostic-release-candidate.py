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


def validate_health_proof(proof: dict[str, Any], item: dict[str, Any], registry: dict[str, Any]) -> None:
    if proof.get("schema_version") != "datapan.health-diagnostic-compatibility-receipt.v1":
        raise ValueError("datapan-health: unsupported machine proof schema")
    if proof.get("status") != "consumer_compatible" or proof.get("health_head") != item.get("head_commit"):
        raise ValueError("datapan-health: receipt status or exact head mismatch")
    if not COMMIT_PATTERN.fullmatch(str(proof.get("tested_revision", ""))):
        raise ValueError("datapan-health: tested_revision must be an exact commit")
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

    expected_fixtures = []
    for path in sorted((DRAFT / "fixtures").glob("*.json")):
        fixture = load(path)
        expected_fixtures.append({
            "name": path.name,
            "sha256": sha256_bytes(path.read_bytes()),
            "cause": fixture["cause"]["code"],
            "determination": fixture["cause"]["determination"],
        })
    if proof.get("fixtures") != expected_fixtures or len(expected_fixtures) != 11:
        raise ValueError("datapan-health: fixture proof is not the exact 11-artifact contract")

    bindings = proof.get("bindings")
    if not isinstance(bindings, list) or len(bindings) != 10:
        raise ValueError("datapan-health: exact ten operation bindings are required")
    operation_ids, dataset_ids, service_ids = set(), set(), set()
    for binding in bindings:
        operation_id = binding.get("operation_id")
        dataset_id = binding.get("dataset_id")
        service_id = binding.get("service_id")
        if (
            not re.fullmatch(r"dpr-op-[0-9]{8}", str(operation_id))
            or not re.fullmatch(r"[0-9]{8}", str(dataset_id))
            or not re.fullmatch(r"public-data_[a-z0-9-]+", str(service_id))
            or binding.get("registry_revision") != registry.get("contract_commit")
        ):
            raise ValueError("datapan-health: invalid operation binding semantics")
        operation_ids.add(operation_id)
        dataset_ids.add(dataset_id)
        service_ids.add(service_id)
    if len(operation_ids) != 10 or len(dataset_ids) != 10 or len(service_ids) != 10:
        raise ValueError("datapan-health: operation bindings must be one-to-one")
    encoded_bindings = json.dumps(bindings, ensure_ascii=False, separators=(",", ":")).encode()
    if proof.get("bindings_sha256") != sha256_bytes(encoded_bindings):
        raise ValueError("datapan-health: bindings digest mismatch")
    test_proof = proof.get("test_proof", {})
    tests = test_proof.get("tests")
    if test_proof.get("count") != 12 or not isinstance(tests, list) or len(tests) != 12:
        raise ValueError("datapan-health: exact test proof is required")
    if len({test.get("name") for test in tests}) != 12:
        raise ValueError("datapan-health: test proof names must be unique")
    boundaries = proof.get("boundaries")
    if boundaries != {
        "existing_health_probe_v1": "preserved",
        "gatus_projection": "unchanged_enum_only",
        "sensitive_evidence": "rejected_before_normalization",
        "public_api": "not_implemented",
        "deployment": "not_performed",
    }:
        raise ValueError("datapan-health: compatibility boundaries mismatch")


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
    validators = {"datapan-health": validate_health_proof}
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
            if item.get("ci_state") != "passed":
                raise ValueError(f"{consumer}: accepted proof requires passed exact-head CI")
            if item.get("review_state") != "independent_approved":
                raise ValueError(f"{consumer}: accepted proof requires independent exact-head approval")
            validate_machine_proof(item, registry)
        elif not missing:
            raise ValueError(f"{consumer}: incomplete proof must name its missing proofs")


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
    missing = [
        {"consumer": item["consumer"], "proof": proof}
        for item in consumers
        for proof in item.get("missing_proofs", [])
    ]
    all_accepted = all(state == "accepted" for state in states.values())
    binding = {
        "registry": registry,
        "contracts": [artifact(path) for path in CONTRACTS],
        "consumer_proofs": consumers,
    }
    binding_sha256 = sha256_bytes(render(binding).encode())
    return {
        "schema_version": "datapan.diagnostic-release-candidate.v1",
        "status": "ready_for_publication_review" if all_accepted else "blocked",
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
            "next_gate": "independent_publication_review" if all_accepted else "collect_missing_consumer_proofs",
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
