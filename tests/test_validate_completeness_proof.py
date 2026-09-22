from __future__ import annotations

import copy
import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "validate-completeness-proof.py"
SPEC = importlib.util.spec_from_file_location("validate_completeness_proof", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CompletenessProofTest(unittest.TestCase):
    def test_checked_in_complete_and_negative_fixtures(self) -> None:
        self.assertEqual(MODULE.check_fixtures(), 7)

    def test_each_non_complete_state_retains_owned_missing_evidence(self) -> None:
        policy = MODULE.load_object(MODULE.POLICY)
        base = MODULE.load_object(MODULE.VALID_FIXTURE)

        for state in ("partial", "unknown", "blocked"):
            with self.subTest(state=state):
                proof = copy.deepcopy(base)
                proof["proof_state"] = state
                proof["claims"] = {"complete": False, "current": False, "updated": False}
                proof["missing_evidence"] = [
                    {
                        "code": f"{state}_evidence",
                        "owner": "StatPan/datapan-registry",
                        "ticket": "#631",
                    }
                ]
                MODULE.validate_proof(proof, policy)

    def test_partial_evidence_cannot_promote_claims(self) -> None:
        policy = MODULE.load_object(MODULE.POLICY)
        proof = MODULE.load_object(MODULE.VALID_FIXTURE)
        proof["proof_state"] = "partial"
        proof["missing_evidence"] = [
            {
                "code": "incomplete_identity_set",
                "owner": "StatPan/datapan-registry",
                "ticket": "#631",
            }
        ]

        with self.assertRaisesRegex(ValueError, "cannot promote"):
            MODULE.validate_proof(proof, policy)


if __name__ == "__main__":
    unittest.main()
