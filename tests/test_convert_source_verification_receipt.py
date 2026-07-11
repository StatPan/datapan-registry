from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "convert-source-verification-to-credential-receipt.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("convert_source_verification", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ConvertSourceVerificationReceiptTest(unittest.TestCase):
    def source(self) -> dict:
        return {
            "source_id": "sample",
            "provider": "Sample",
            "candidate_batch": "reports/sample/runtime-candidates.json",
            "runtime_evidence_plan": "reports/sample/runtime-evidence-plan.json",
            "credential_envs": ["SAMPLE_TOKEN"],
        }

    def verification(self, *, outcome: str = "verified", error_class: str = "none") -> dict:
        return {
            "schema_version": "datapan.source-candidate-verification.v1",
            "generated_at": "2026-07-11T00:00:00Z",
            "source_id": "sample",
            "provider": "Sample",
            "source_profile": "profiles/sample.json",
            "candidate_batch": "reports/sample/runtime-candidates.json",
            "bounded": True,
            "credential_configured": True,
            "credential_env_names": ["SAMPLE_TOKEN"],
            "summary": {
                "candidates": 1,
                "verified": 1 if outcome == "verified" else 0,
                "failed": 1 if outcome == "failed" else 0,
                "skipped": 1 if outcome == "skipped" else 0,
            },
            "results": [{"candidate_id": "one", "outcome": outcome, "error_class": error_class, "http_status": 200, "duration_ms": 8}],
            "redaction": {"secret_values_present": False, "secret_hashes_present": False, "request_urls_present": False, "response_bodies_present": False},
        }

    def test_verified_generic_result_becomes_secret_free_receipt(self) -> None:
        result = MODULE.build(self.verification(), self.source())
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(result["error_class"], "none")
        self.assertEqual(result["execution"]["request_count"], 1)
        self.assertFalse(result["redaction"]["secret_values_present"])

    def test_bad_request_maps_to_registry_provider_failure(self) -> None:
        result = MODULE.build(self.verification(outcome="failed", error_class="bad_request"), self.source())
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["error_class"], "provider")

    def test_unclean_generic_redaction_is_rejected(self) -> None:
        verification = self.verification()
        verification["redaction"]["request_urls_present"] = True
        with self.assertRaisesRegex(ValueError, "redaction"):
            MODULE.build(verification, self.source())


if __name__ == "__main__":
    unittest.main()
