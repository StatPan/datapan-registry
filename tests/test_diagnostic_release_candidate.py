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
        self.assertEqual(result["missing_proofs"], 5)

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

    def test_even_complete_intake_requires_separate_publication_review(self):
        intake = copy.deepcopy(MODULE.GENERATOR.load(MODULE.GENERATOR.DEFAULT_INTAKE))
        for consumer in intake["consumers"]:
            consumer["proof_state"] = "accepted"
            consumer["missing_proofs"] = []
            consumer["ci_state"] = "passed"
            consumer["review_state"] = "independent_approved"
        candidate = MODULE.GENERATOR.build(intake)
        self.assertEqual(candidate["status"], "ready_for_publication_review")
        self.assertTrue(candidate["decision"]["all_consumers_accepted"])
        self.assertFalse(candidate["authority"]["publishing_allowed"])
        self.assertEqual(candidate["decision"]["next_gate"], "independent_publication_review")


if __name__ == "__main__":
    unittest.main()
