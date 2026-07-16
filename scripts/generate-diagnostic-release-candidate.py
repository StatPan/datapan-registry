#!/usr/bin/env python3
"""Generate or check the non-authoritative diagnostic contract release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
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


def validate_intake(consumers: list[dict[str, Any]]) -> None:
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
        elif not missing:
            raise ValueError(f"{consumer}: incomplete proof must name its missing proofs")


def build(intake: dict[str, Any]) -> dict[str, Any]:
    consumers = intake.get("consumers")
    if not isinstance(consumers, list):
        raise ValueError("intake.consumers must be a list")
    states = {item.get("consumer"): item.get("proof_state") for item in consumers}
    if set(states) != {"datapan-cli", "datapan-health", "datapan-web"}:
        raise ValueError("intake must contain exactly the three required consumers")
    validate_intake(consumers)
    missing = [
        {"consumer": item["consumer"], "proof": proof}
        for item in consumers
        for proof in item.get("missing_proofs", [])
    ]
    all_accepted = all(state == "accepted" for state in states.values())
    binding = {
        "registry": intake.get("registry"),
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
