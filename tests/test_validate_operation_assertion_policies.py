from __future__ import annotations

import copy
import importlib.util
import pathlib
import subprocess
import sys
import unittest
from datetime import datetime, timezone

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/validate-operation-assertion-policies.py"
SPEC = importlib.util.spec_from_file_location("operation_assertion_validator", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class OperationAssertionPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.artifact = MODULE.load(ROOT / MODULE.GENERATOR.ARTIFACT)
        cls.proof = MODULE.load(ROOT / MODULE.GENERATOR.PROOF)
        cls.bundle_manifest = MODULE.load(ROOT / MODULE.GENERATOR.BUNDLE_MANIFEST)
        cls.candidate = MODULE.load(ROOT / MODULE.GENERATOR.CANDIDATE)
        cls.schema = MODULE.load(ROOT / MODULE.GENERATOR.SCHEMA)
        cls.catalog = MODULE.load(ROOT / MODULE.GENERATOR.CATALOG)
        cls.registry = MODULE.load(ROOT / MODULE.GENERATOR.REGISTRY)

    def validate(self, artifact=None, proof=None, candidate=None, bundle_manifest=None) -> None:
        MODULE.validate_all(
            artifact or self.artifact,
            proof or self.proof,
            bundle_manifest or self.bundle_manifest,
            candidate or self.candidate,
            self.schema,
            self.catalog,
            self.registry,
        )

    def test_checked_in_artifacts(self) -> None:
        self.validate()

    def test_generator_is_deterministic(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/generate-operation-assertion-policies.py"), "--check"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_canary_identity_mismatch_is_unknown_to_health(self) -> None:
        case = copy.deepcopy(self.proof["cases"][0])
        case["operation_id"] = "dpr-op-99999999"
        self.assertEqual(MODULE.project_case(self.artifact, self.proof, case), "unknown")

    def test_policy_digest_mismatch_is_unknown_to_health(self) -> None:
        case = copy.deepcopy(self.proof["cases"][0])
        case["policy_binding_override"] = {"artifact_sha256": "a" * 64}
        self.assertEqual(MODULE.project_case(self.artifact, self.proof, case), "unknown")

    def test_missing_null_and_malformed_policy_bindings_fail_closed(self) -> None:
        case = copy.deepcopy(self.proof["cases"][0])
        bindings = (
            ("missing", None),
            ("null", None),
            ("string", "not-an-object"),
            ("array", []),
            ("partial", {"artifact_sha256": "a" * 64}),
        )
        for name, binding in bindings:
            with self.subTest(name=name):
                proof = copy.deepcopy(self.proof)
                if name == "missing":
                    proof.pop("policy_binding")
                else:
                    proof["policy_binding"] = binding
                self.assertEqual(MODULE.project_case(self.artifact, proof, case), "unknown")

    def test_malformed_policy_binding_override_fails_closed(self) -> None:
        case = copy.deepcopy(self.proof["cases"][0])
        case["policy_binding_override"] = "not-an-object"
        self.assertEqual(MODULE.project_case(self.artifact, self.proof, case), "unknown")

    def test_unsupported_policy_version_is_rejected(self) -> None:
        value = copy.deepcopy(self.artifact)
        value["policy_set"]["version"] = 2
        value["policy_set"]["supersedes"] = {"policy_set_version": 1, "artifact_sha256": "a" * 64}
        with self.assertRaisesRegex(ValueError, "unsupported policy set version"):
            self.validate(artifact=value)

    def test_first_version_cannot_claim_supersession(self) -> None:
        value = copy.deepcopy(self.artifact)
        value["policy_set"]["supersedes"] = {"policy_set_version": 1, "artifact_sha256": "a" * 64}
        with self.assertRaisesRegex(ValueError, "must not supersede"):
            MODULE.validate_supersession(value["policy_set"])

    def test_later_version_must_bind_immediate_predecessor(self) -> None:
        for supersedes in (None, {"policy_set_version": 3, "artifact_sha256": "a" * 64}):
            with self.subTest(supersedes=supersedes):
                policy_set = {"version": 2, "supersedes": supersedes}
                with self.assertRaisesRegex(ValueError, "predecessor"):
                    MODULE.validate_supersession(policy_set)

    def test_old_superseded_pin_is_unknown(self) -> None:
        case = next(
            item for item in self.proof["cases"]
            if item["name"] == "superseded_old_policy_pin_is_unknown"
        )
        transition = self.proof["supersession_transition_model"]
        self.assertEqual(case["policy_binding"], transition["from"])
        self.assertEqual(case["active_policy_binding"], transition["to"])
        self.assertEqual(MODULE.project_case(self.artifact, self.proof, case), "unknown")

    def test_unsupported_semantic_operator_is_rejected(self) -> None:
        value = copy.deepcopy(self.artifact)
        value["operations"][0]["dimensions"]["semantic"] = {
            "state": "asserted",
            "assertion_type": "allowlisted_rules",
            "rules": [{"field": "resultCode", "operator": "exec", "value": "00"}],
            "evidence": value["operations"][0]["dimensions"]["contract"]["evidence"],
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(self.schema).validate(value)

    def test_incomplete_freshness_semantics_are_rejected(self) -> None:
        value = copy.deepcopy(self.artifact)
        value["operations"][0]["dimensions"]["freshness"] = {
            "state": "asserted",
            "assertion_type": "maximum_age",
            "reference_time_field": "updatedAt",
            "maximum_age_seconds": 300,
            "evidence": value["operations"][0]["dimensions"]["contract"]["evidence"],
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(self.schema).validate(value)

    def test_assertion_missing_evidence_is_rejected(self) -> None:
        value = copy.deepcopy(self.artifact)
        value["operations"][0]["dimensions"]["contract"].pop("evidence")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(self.schema).validate(value)

    def test_missing_dimension_is_distinct_from_not_asserted(self) -> None:
        value = copy.deepcopy(self.artifact)
        value["operations"][0]["dimensions"].pop("semantic")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(self.schema).validate(value)

    def test_freshness_exact_boundary_passes_and_one_second_over_fails(self) -> None:
        assertion = {
            "state": "asserted",
            "actual_time_source": "health_observed_at",
            "reference_time_source": "response_field",
            "calendar": "gregorian",
            "timestamp_format": "%Y-%m-%dT%H:%M:%SZ",
            "timezone": "UTC",
            "maximum_age_seconds": 300,
            "maximum_age_boundary": "inclusive",
            "future_tolerance_seconds": 5,
            "empty_result_policy": "not_observed",
        }
        now = datetime(2026, 7, 17, 0, 5, 0, tzinfo=timezone.utc)
        self.assertEqual(MODULE.freshness_result(assertion, "2026-07-17T00:00:00Z", now), "pass")
        self.assertEqual(MODULE.freshness_result(assertion, "2026-07-16T23:59:59Z", now), "fail")

    def test_future_timestamp_outside_tolerance_fails(self) -> None:
        assertion = {
            "state": "asserted",
            "actual_time_source": "health_observed_at",
            "reference_time_source": "response_field",
            "calendar": "gregorian",
            "timestamp_format": "%Y-%m-%dT%H:%M:%SZ",
            "timezone": "UTC",
            "maximum_age_seconds": 300,
            "maximum_age_boundary": "inclusive",
            "future_tolerance_seconds": 5,
            "empty_result_policy": "not_observed",
        }
        now = datetime(2026, 7, 17, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(MODULE.freshness_result(assertion, "2026-07-17T00:00:06Z", now), "fail")

    def test_empty_freshness_observation_uses_explicit_policy(self) -> None:
        assertion = {
            "state": "asserted",
            "actual_time_source": "health_observed_at",
            "reference_time_source": "response_field",
            "calendar": "gregorian",
            "timestamp_format": "%Y-%m-%dT%H:%M:%SZ",
            "timezone": "UTC",
            "maximum_age_seconds": 300,
            "maximum_age_boundary": "inclusive",
            "future_tolerance_seconds": 5,
            "empty_result_policy": "not_observed",
        }
        self.assertEqual(
            MODULE.freshness_result(assertion, None, datetime.now(timezone.utc)),
            "not_observed",
        )

    def test_secret_and_private_runtime_fields_are_rejected(self) -> None:
        for value in (
            {"service_key": "redacted"},
            {"request_url": "https://example.test/api?serviceKey=secret"},
            {"note": "/api/items?serviceKey=secret"},
            {"note": "items?serviceKey=secret"},
            {"note": "//example.test/api?serviceKey=secret"},
            {"note": "Bearer abcdefghijklmnop"},
            {"response_rows": []},
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "forbidden|secret-shaped|query values"):
                    MODULE.reject_leaks(value)

    def test_not_asserted_does_not_become_pass_or_fail(self) -> None:
        case = next(item for item in self.proof["cases"] if item["dimension"] == "semantic")
        self.assertEqual(MODULE.project_case(self.artifact, self.proof, case), "not_observed")

    def test_release_candidate_has_no_publish_or_runtime_authority(self) -> None:
        self.assertFalse(any(self.candidate["authority"].values()))

    def test_reference_model_does_not_claim_health_execution(self) -> None:
        self.assertEqual(self.proof["proof_kind"], "reference_model_only")
        self.assertEqual(self.proof["consumer_status"], "not_executed_by_datapan_health")
        self.assertNotIn("consumer", self.proof)
        self.assertNotIn("consumer_proof", self.candidate)


if __name__ == "__main__":
    unittest.main()
