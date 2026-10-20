import json
import pathlib
import unittest

import importlib.util
ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rollup", ROOT / "scripts/generate-completeness-proof-rollup.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)



class CurrentReleaseEvaluationTest(unittest.TestCase):
    def _evaluation(self, target_kind="historical", status="historical"):
        claims = {"complete": True, "current": False, "updated": False}
        return {
            "schema_version": "datapan.completeness-current-release-evaluation.v1",
            "evaluation_as_of": "2026-10-20T00:00:00Z",
            "evaluator_source": {"revision": "a" * 40, "path": "scripts/generate-completeness-proof-rollup.py", "bytes": 1, "sha256": "b" * 64},
            "trusted_main_sha": "c" * 40,
            "target_kind": target_kind,
            "target": {"source_sha": "a" * 40, "tree_sha": "d" * 40, "release_manifest": {"path": "manifest.json", "bytes": 1, "sha256": "b" * 64}, "canonical_registry": {"path": "manifest.json", "bytes": 1, "sha256": "b" * 64}, "operation_manifest": {"path": "manifest.json", "bytes": 1, "sha256": "b" * 64}},
            "snapshot": {"path": "reports/completeness-proof-rollup.json", "bytes": 1, "sha256": "b" * 64},
            "target_applicability": {"schema_version": "datapan.completeness-current-target-applicability.v1", "status": "unavailable", "reason_code": "current_release_evaluation_evidence_unavailable", "disposition": None, "context": {"repository": "StatPan/datapan-registry", "release_source_sha": "a" * 40, "trusted_main_sha": "c" * 40, "evaluation_as_of": "2026-10-20T00:00:00Z"}, "source_target": None, "target_contract": None, "evidence_index": None},
            "summary": {"registered_scopes": 1, "statuses": {"applicable": 0, "historical": 1 if status == "historical" else 0, "missing": 0, "unavailable": 0 if status == "historical" else 1, "rejected": 0}, "claims": {"complete": 1, "current": 0, "updated": 0}},
            "scopes": [{"scope_id": "scope", "relation": target_kind, "status": status, "claims": claims, "missing_evidence": [{"code": "current_release_evaluation_evidence_unavailable", "owner": "StatPan/datapan-registry", "ticket": "#754"}]}],
        }

    def test_historical_and_candidate_cannot_claim_current(self):
        for kind in ("historical", "candidate"):
            value = self._evaluation(kind)
            self.assertFalse(value["scopes"][0]["claims"]["current"])
            self.assertFalse(value["scopes"][0]["claims"]["updated"])

    def test_missing_evidence_is_unavailable(self):
        value = self._evaluation("current_main", "unavailable")
        self.assertEqual(value["scopes"][0]["status"], "unavailable")
        self.assertFalse(value["scopes"][0]["claims"]["current"])

    def test_borrowed_historical_claim_is_not_promoted(self):
        value = self._evaluation("historical")
        value["scopes"][0]["claims"]["current"] = True
        self.assertRaises(ValueError, module.require_claims, value, ["scope:current"])

    def test_evaluator_schema_and_manifest_exclusion(self):
        value = self._evaluation()
        module.validate_schema(value, ROOT / module.CURRENT_EVALUATION_SCHEMA_PATH, "test evaluation")
        manifest = json.loads((ROOT / "manifest.json").read_text())
        self.assertNotIn(".datapan/ci/current-release-evaluation.json", {row["path"] for row in manifest["artifacts"]})

    def test_require_claims_accepts_only_evaluator_discriminator(self):
        value = self._evaluation()
        with self.assertRaisesRegex(ValueError, "authenticated current-release"):
            module.require_claims({"schema_version": "datapan.completeness-proof-rollup.v2"}, ["scope:current"])


if __name__ == "__main__":
    unittest.main()
