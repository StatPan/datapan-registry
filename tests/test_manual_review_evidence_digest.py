from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("manual_review_evidence_digest", ROOT / "scripts" / "manual_review_evidence_digest.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ManualReviewEvidenceDigestTest(unittest.TestCase):
    def base_record(self):
        return {
            "summary": {"consumer_count": 7, "blocked_consumers": 0},
            "manifest_evidence_contracts": [{"path": "reports/example.json", "bytes": 10, "sha256": "a" * 64}],
            "shard_release_evidence": {"canonical_registry_bytes": 100, "manifest_bound_bytes_excluding_self": 200},
        }

    def test_physical_artifact_footprint_does_not_revalidate_manual_decision(self):
        baseline = self.base_record()
        changed = self.base_record()
        changed["manifest_evidence_contracts"][0]["bytes"] = 99
        changed["manifest_evidence_contracts"][0]["sha256"] = "b" * 64
        changed["shard_release_evidence"]["manifest_bound_bytes_excluding_self"] = 999
        self.assertEqual(MODULE.compatibility_binding_sha256(baseline), MODULE.compatibility_binding_sha256(changed))

    def test_consumer_policy_change_revalidates_manual_decision(self):
        baseline = self.base_record()
        changed = self.base_record()
        changed["summary"]["blocked_consumers"] = 1
        self.assertNotEqual(MODULE.compatibility_binding_sha256(baseline), MODULE.compatibility_binding_sha256(changed))


if __name__ == "__main__":
    unittest.main()
