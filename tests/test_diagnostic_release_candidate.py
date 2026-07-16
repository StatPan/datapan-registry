import copy
import importlib.util
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "diagnostic_candidate_validator", ROOT / "scripts/validate-diagnostic-release-candidate.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class DiagnosticReleaseCandidateTest(unittest.TestCase):
    def test_checked_in_candidate_is_bound_and_blocked(self):
        result = MODULE.validate_all()
        self.assertEqual(result["consumers"], 3)
        self.assertEqual(result["missing_proofs"], 7)

    def test_generator_check_entrypoint(self):
        result = subprocess.run(
            [sys.executable, str(MODULE.GENERATOR_PATH), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("blocked", result.stdout)

    def test_incomplete_consumer_proofs_never_enable_publication(self):
        intake = MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE)
        candidate = MODULE.GENERATOR.build(intake)
        self.assertFalse(candidate["authority"]["publishing_allowed"])
        self.assertFalse(candidate["decision"]["all_consumers_accepted"])

    def test_accepted_health_proof_has_exact_bytes_schema_and_semantics(self):
        intake = MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE)
        health = next(item for item in intake["consumers"] if item["consumer"] == "datapan-health")
        MODULE.GENERATOR.validate_machine_proof(health, intake["registry"])

    def test_string_flips_cannot_create_a_ready_candidate(self):
        intake = copy.deepcopy(MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE))
        for consumer in intake["consumers"]:
            consumer["proof_state"] = "accepted"
            consumer["missing_proofs"] = []
            consumer["ci_state"] = "passed"
            consumer["review_state"] = "independent_approved"
        with self.assertRaisesRegex(ValueError, "machine proof|semantic validator"):
            MODULE.GENERATOR.build(intake)

    def test_duplicate_consumer_record_is_rejected(self):
        intake = copy.deepcopy(MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE))
        intake["consumers"] = [intake["consumers"][0], intake["consumers"][0], intake["consumers"][2]]
        with self.assertRaisesRegex(ValueError, "exactly the three required consumers"):
            MODULE.GENERATOR.build(intake)

    def test_machine_proof_digest_or_byte_drift_is_rejected(self):
        intake = copy.deepcopy(MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE))
        health = next(item for item in intake["consumers"] if item["consumer"] == "datapan-health")
        health["machine_proof"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "byte identity mismatch"):
            MODULE.GENERATOR.build(intake)

    def test_health_semantic_tamper_is_rejected(self):
        intake = MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE)
        health = next(item for item in intake["consumers"] if item["consumer"] == "datapan-health")
        proof = MODULE.GENERATOR.load(
            ROOT / health["machine_proof"]["path"]
        )
        proof["status"] = "self_asserted"
        with self.assertRaisesRegex(ValueError, "status or exact head mismatch"):
            MODULE.GENERATOR.validate_health_proof(proof, health, intake["registry"])


if __name__ == "__main__":
    unittest.main()
