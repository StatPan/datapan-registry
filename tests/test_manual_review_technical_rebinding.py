from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "technical_rebinding", ROOT / "scripts" / "generate-credential-runtime-manual-review-technical-rebinding.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ManualReviewTechnicalRebindingTest(unittest.TestCase):
    def policy(self, decision_path: pathlib.Path, baseline: list[dict]) -> dict:
        return {
            "generated_at": "2026-07-23T00:00:00Z",
            "approver_scope": "approved artifact-only technical rebinding",
            "decision_path": decision_path.as_posix(),
            "decision_sha256": MODULE.digest_bytes(decision_path.read_bytes()),
            "baseline": {"artifact_count": len(baseline), "artifact_inventory_sha256": MODULE.inventory_digest(baseline)},
            "allowed_additions": [
                {"path": "schemas/health.schema.json", "kind": "schema"},
                {"path": "reports/health.json", "kind": "verification_plan", "schema": "https://schemas.example/health"},
            ],
        }

    def test_only_the_exact_two_health_artifacts_can_rebind(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            decision_path = root / "decision.json"
            decision_path.write_text(json.dumps({"decision": {"compatibility_sha256": "a" * 64}}), encoding="utf-8")
            baseline = [{"path": "data/registry.json", "kind": "registry", "bytes": 1, "sha256": "b" * 64}]
            artifacts = baseline + [
                {"path": "schemas/health.schema.json", "kind": "schema", "bytes": 2, "sha256": "c" * 64},
                {"path": "reports/health.json", "kind": "verification_plan", "schema": "https://schemas.example/health", "bytes": 3, "sha256": "d" * 64},
            ]
            value = MODULE.expected(self.policy(decision_path, baseline), {"artifacts": artifacts}, {"summary": {}}, decision_path)
            self.assertEqual(value["status"], "approved_artifact_only_rebinding")
            self.assertEqual(value["old_compatibility_sha256"], "a" * 64)
            self.assertEqual(value["manifest_delta"]["added_paths"], ["reports/health.json", "schemas/health.schema.json"])
            with self.assertRaisesRegex(ValueError, "allowlist"):
                MODULE.expected(self.policy(decision_path, baseline), {"artifacts": artifacts + [{"path": "reports/extra.json"}]}, {"summary": {}}, decision_path)

    def test_tampering_the_human_decision_rejects_rebinding(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            decision_path = root / "decision.json"
            decision_path.write_text(json.dumps({"decision": {"compatibility_sha256": "a" * 64}}), encoding="utf-8")
            baseline = [{"path": "data/registry.json", "kind": "registry", "bytes": 1, "sha256": "b" * 64}]
            policy = self.policy(decision_path, baseline)
            decision_path.write_text(json.dumps({"decision": {"compatibility_sha256": "e" * 64}}), encoding="utf-8")
            artifacts = baseline + [
                {"path": "schemas/health.schema.json", "kind": "schema"},
                {"path": "reports/health.json", "kind": "verification_plan", "schema": "https://schemas.example/health"},
            ]
            with self.assertRaisesRegex(ValueError, "byte-for-byte unchanged"):
                MODULE.expected(policy, {"artifacts": artifacts}, {"summary": {}}, decision_path)
