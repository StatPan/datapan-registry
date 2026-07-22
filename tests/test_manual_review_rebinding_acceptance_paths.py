from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ACCEPTANCE = load_module("manual_review_acceptance", "generate-credential-runtime-manual-review-acceptance.py")
VALIDATOR = load_module("manual_review_validator", "validate-credential-runtime-manual-review-decision.py")


class ManualReviewRebindingAcceptancePathsTest(unittest.TestCase):
    def fixture(self, root: pathlib.Path, *, rebinding: bool, decision_digest_matches: bool):
        handoff = json.loads((ROOT / "reports/credential-runtime-review-handoff.json").read_text(encoding="utf-8"))
        compatibility = json.loads((ROOT / "reports/release-consumer-compatibility.json").read_text(encoding="utf-8"))
        decision = json.loads((ROOT / "reports/credential-runtime-manual-review-decision.json").read_text(encoding="utf-8"))
        handoff_path = root / "handoff.json"
        compatibility_path = root / "compatibility.json"
        decision_path = root / "decision.json"
        rebinding_path = root / "rebinding.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        old_compatibility = ACCEPTANCE.compatibility_binding_sha256(compatibility)
        if rebinding:
            compatibility["summary"]["consumer_count"] += 1
        compatibility_path.write_text(json.dumps(compatibility), encoding="utf-8")
        decision["inputs"]["credential_runtime_review_handoff"] = handoff_path.as_posix()
        decision["inputs"]["release_consumer_compatibility"] = compatibility_path.as_posix()
        decision["decision"]["handoff_sha256"] = ACCEPTANCE.file_sha256(handoff_path)
        decision["decision"]["compatibility_sha256"] = old_compatibility
        decision_path.write_text(json.dumps(decision), encoding="utf-8")
        current_compatibility = ACCEPTANCE.compatibility_binding_sha256(compatibility)
        rebinding_path.write_text(
            json.dumps(
                {
                    "status": "approved_artifact_only_rebinding" if rebinding else "not_applicable",
                    "old_compatibility_sha256": old_compatibility,
                    "new_compatibility_sha256": current_compatibility,
                    "decision_sha256": ACCEPTANCE.file_sha256(decision_path) if decision_digest_matches else "0" * 64,
                }
            ),
            encoding="utf-8",
        )
        return handoff, compatibility, decision, decision_path, handoff_path, compatibility_path, rebinding_path

    def test_rebinding_and_non_rebinding_paths_are_explicitly_covered(self):
        cases = [
            ("current_compatibility_without_rebinding", False, True, True),
            ("approved_rebinding", True, True, True),
            ("rebinding_with_decision_digest_mismatch", True, False, False),
        ]
        for name, rebinding, digest_matches, succeeds in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                fixture = self.fixture(pathlib.Path(raw), rebinding=rebinding, decision_digest_matches=digest_matches)
                handoff, compatibility, decision, decision_path, handoff_path, compatibility_path, rebinding_path = fixture
                if succeeds:
                    ACCEPTANCE.build_report(
                        handoff,
                        compatibility,
                        decision,
                        decision_path=decision_path,
                        handoff_path=handoff_path,
                        compatibility_path=compatibility_path,
                        technical_rebinding_path=rebinding_path,
                    )
                    VALIDATOR.validate_decision(
                        decision,
                        decision_path=decision_path,
                        handoff_path=handoff_path,
                        compatibility_path=compatibility_path,
                        technical_rebinding_path=rebinding_path,
                    )
                else:
                    with self.assertRaisesRegex(ValueError, "compatibility_sha256"):
                        ACCEPTANCE.build_report(
                            handoff,
                            compatibility,
                            decision,
                            decision_path=decision_path,
                            handoff_path=handoff_path,
                            compatibility_path=compatibility_path,
                            technical_rebinding_path=rebinding_path,
                        )
                    with self.assertRaisesRegex(ValueError, "compatibility_sha256"):
                        VALIDATOR.validate_decision(
                            decision,
                            decision_path=decision_path,
                            handoff_path=handoff_path,
                            compatibility_path=compatibility_path,
                            technical_rebinding_path=rebinding_path,
                        )


if __name__ == "__main__":
    unittest.main()
