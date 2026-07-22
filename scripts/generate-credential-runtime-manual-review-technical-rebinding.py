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

ROOT = pathlib.Path(".")
POLICY = ROOT / "policy/health-observation-plan-technical-rebinding.json"
MANIFEST = ROOT / "manifest.json"
COMPATIBILITY = ROOT / "reports/release-consumer-compatibility.json"
DECISION = ROOT / "reports/credential-runtime-manual-review-decision.json"
OUTPUT = ROOT / "reports/credential-runtime-manual-review-technical-rebinding.json"


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory_digest(artifacts: list[dict[str, Any]]) -> str:
    return digest_bytes(json.dumps(artifacts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def render(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def expected(policy: dict[str, Any], manifest: dict[str, Any], compatibility: dict[str, Any], decision_path: pathlib.Path) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    additions = policy.get("allowed_additions")
    baseline = policy.get("baseline")
    if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
        raise ValueError("manifest artifacts must be objects")
    if not isinstance(additions, list) or not all(isinstance(item, dict) for item in additions):
        raise ValueError("policy allowed_additions must be objects")
    if not isinstance(baseline, dict):
        raise ValueError("policy baseline must be an object")
    allowed = {str(item.get("path")): item for item in additions}
    actual = {str(item.get("path")): item for item in artifacts}
    present = [path for path in allowed if path in actual]
    if not present:
        return {
            "record_type": "datapan.manual-review-technical-rebinding.v1",
            "generated_at": policy["generated_at"], "status": "not_applicable",
            "approver_scope": policy["approver_scope"], "decision_path": policy["decision_path"],
            "decision_sha256": policy["decision_sha256"], "allowed_additions": additions,
        }
    if set(present) != set(allowed):
        raise ValueError("Health technical rebinding requires every allowed addition")
    stripped = [item for item in artifacts if str(item.get("path")) not in allowed]
    if len(stripped) != baseline.get("artifact_count") or inventory_digest(stripped) != baseline.get("artifact_inventory_sha256"):
        raise ValueError("manifest delta exceeds the approved Health plan/schema allowlist")
    if len(artifacts) != int(baseline["artifact_count"]) + len(allowed):
        raise ValueError("manifest artifact count is outside the approved technical rebinding scope")
    for path, rule in allowed.items():
        for key, value in rule.items():
            if actual[path].get(key) != value:
                raise ValueError(f"approved addition mismatch: {path}.{key}")
    decision_sha = digest_bytes(decision_path.read_bytes())
    if decision_sha != policy.get("decision_sha256"):
        raise ValueError("existing human decision must remain byte-for-byte unchanged")
    decision = load(decision_path)
    old = decision.get("decision", {}).get("compatibility_sha256")
    if not isinstance(old, str) or len(old) != 64:
        raise ValueError("accepted decision has no compatibility SHA-256")
    return {
        "record_type": "datapan.manual-review-technical-rebinding.v1",
        "generated_at": policy["generated_at"], "status": "approved_artifact_only_rebinding",
        "approver_scope": policy["approver_scope"], "decision_path": policy["decision_path"],
        "decision_sha256": decision_sha, "old_compatibility_sha256": old,
        "new_compatibility_sha256": compatibility_binding_sha256(compatibility),
        "baseline": baseline, "allowed_additions": additions,
        "manifest_delta": {"added_paths": sorted(allowed), "artifact_count_before": baseline["artifact_count"], "artifact_count_after": len(artifacts)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=pathlib.Path, default=POLICY)
    parser.add_argument("--manifest", type=pathlib.Path, default=MANIFEST)
    parser.add_argument("--compatibility", type=pathlib.Path, default=COMPATIBILITY)
    parser.add_argument("--decision", type=pathlib.Path, default=DECISION)
    parser.add_argument("--output", type=pathlib.Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        value = expected(load(args.policy), load(args.manifest), load(args.compatibility), args.decision)
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
