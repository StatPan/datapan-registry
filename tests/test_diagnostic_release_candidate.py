import copy
import hashlib
import importlib.util
import json
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
        self.assertEqual(result["missing_proofs"], 0)
        self.assertEqual(result["missing_publication_gates"], 1)

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
        self.assertTrue(candidate["decision"]["all_consumers_accepted"])
        self.assertFalse(candidate["decision"]["all_publication_gates_passed"])
        self.assertEqual(candidate["status"], "blocked")

    def test_accepted_health_proof_has_exact_bytes_schema_and_semantics(self):
        intake = MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE)
        health = next(item for item in intake["consumers"] if item["consumer"] == "datapan-health")
        MODULE.GENERATOR.validate_machine_proof(health, intake["registry"])

    def test_accepted_web_prepublication_proof_has_exact_bytes_schema_and_semantics(self):
        intake = MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE)
        web = next(item for item in intake["consumers"] if item["consumer"] == "datapan-web")
        MODULE.GENERATOR.validate_machine_proof(web, intake["registry"])
        proof = MODULE.GENERATOR.load(ROOT / web["machine_proof"]["path"])
        self.assertEqual(
            proof["rollout"]["immutable_registry_release_manifest_consumption"],
            "post_publication_required",
        )
        self.assertFalse(proof["rollout"]["runtime_authority_before_publication"])

    def test_accepted_cli_prepublication_proof_is_separate_from_distribution(self):
        intake = MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE)
        cli = next(item for item in intake["consumers"] if item["consumer"] == "datapan-cli")
        MODULE.GENERATOR.validate_machine_proof(cli, intake["registry"])
        proof = MODULE.GENERATOR.load(ROOT / cli["machine_proof"]["path"])
        self.assertEqual(proof["rollout"]["prepublication_compatibility"], "accepted")
        self.assertEqual(proof["rollout"]["anonymous_registry_distribution"], "external_publication_gate_blocked")
        self.assertFalse(proof["rollout"]["runtime_authority_before_publication"])

    def test_string_flips_cannot_create_a_ready_candidate(self):
        intake = copy.deepcopy(MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE))
        intake["publication_gates"]["anonymous_registry_distribution"] = {
            "status": "passed",
            "provider": "hugging_face",
            "runtime_authority": True,
            "publishing_allowed": True,
        }
        with self.assertRaisesRegex(ValueError, "distribution gate identity mismatch"):
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

    def test_health_joint_proof_and_intake_tamper_cannot_reanchor_bindings(self):
        intake = MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE)
        health = copy.deepcopy(next(item for item in intake["consumers"] if item["consumer"] == "datapan-health"))
        proof = MODULE.GENERATOR.load(ROOT / health["machine_proof"]["path"])
        proof["bindings"][0]["dataset_id"] = "15999999"
        encoded = json.dumps(proof["bindings"], ensure_ascii=False, separators=(",", ":")).encode()
        proof["bindings_sha256"] = hashlib.sha256(encoded).hexdigest()
        health["receipt_sha256"] = "f" * 64
        health["machine_proof"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "exact operation binding identity mismatch"):
            MODULE.GENERATOR.validate_health_proof(proof, health, intake["registry"])

    def test_health_joint_proof_and_intake_tamper_cannot_reanchor_test_or_run_identity(self):
        intake = MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE)
        health = copy.deepcopy(next(item for item in intake["consumers"] if item["consumer"] == "datapan-health"))
        proof = MODULE.GENERATOR.load(ROOT / health["machine_proof"]["path"])
        proof["tested_revision"] = "f" * 40
        health["ci_run"] = 99999999999
        with self.assertRaisesRegex(ValueError, "receipt status or exact head mismatch|tested_revision identity mismatch"):
            MODULE.GENERATOR.validate_health_proof(proof, health, intake["registry"])

        health = copy.deepcopy(next(item for item in intake["consumers"] if item["consumer"] == "datapan-health"))
        proof = MODULE.GENERATOR.load(ROOT / health["machine_proof"]["path"])
        proof["test_proof"]["tests"][0]["name"] = "TestForgedButRegexValidIdentity"
        proof["test_proof"]["manifest"]["sha256"] = "f" * 64
        proof["test_proof"]["sources"][0]["sha256"] = "f" * 64
        health["receipt_sha256"] = "e" * 64
        health["machine_proof"]["sha256"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "exact test, manifest, or source identity mismatch"):
            MODULE.GENERATOR.validate_health_proof(proof, health, intake["registry"])

    def test_web_post_public_rollout_gate_cannot_be_promoted_by_joint_tamper(self):
        intake = MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE)
        web = copy.deepcopy(next(item for item in intake["consumers"] if item["consumer"] == "datapan-web"))
        proof = MODULE.GENERATOR.load(ROOT / web["machine_proof"]["path"])
        proof["rollout"]["immutable_registry_release_manifest_consumption"] = "prepublication_passed"
        proof["rollout"]["runtime_authority_before_publication"] = True
        web["receipt_sha256"] = "f" * 64
        web["machine_proof"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "pre-publication and post-publication gates are not separated"):
            MODULE.GENERATOR.validate_web_proof(proof, web, intake["registry"])

    def test_cli_compatibility_receipt_cannot_claim_distribution_authority(self):
        intake = MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE)
        cli = copy.deepcopy(next(item for item in intake["consumers"] if item["consumer"] == "datapan-cli"))
        proof = MODULE.GENERATOR.load(ROOT / cli["machine_proof"]["path"])
        proof["rollout"]["anonymous_registry_distribution"] = "passed"
        proof["rollout"]["runtime_authority_before_publication"] = True
        cli["receipt_sha256"] = "f" * 64
        cli["machine_proof"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "compatibility and anonymous distribution gates are not separated"):
            MODULE.GENERATOR.validate_cli_proof(proof, cli, intake["registry"])


if __name__ == "__main__":
    unittest.main()
