from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from manual_review_evidence_digest import compatibility_binding_sha256


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
    def test_artifact_rebinding_status_cannot_promote_an_unbound_decision(self):
        handoff = json.loads((ROOT / "reports/credential-runtime-review-handoff.json").read_text(encoding="utf-8"))
        compatibility = json.loads((ROOT / "reports/release-consumer-compatibility.json").read_text(encoding="utf-8"))
        decision = copy.deepcopy(
            json.loads((ROOT / "reports/credential-runtime-manual-review-decision.json").read_text(encoding="utf-8"))
        )

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            handoff_path = root / "handoff.json"
            compatibility_path = root / "compatibility.json"
            decision_path = root / "decision.json"
            rebinding_path = root / "rebinding.json"
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            compatibility["summary"]["consumer_count"] += 1
            compatibility_path.write_text(json.dumps(compatibility), encoding="utf-8")

            decision["inputs"]["credential_runtime_review_handoff"] = handoff_path.as_posix()
            decision["inputs"]["release_consumer_compatibility"] = compatibility_path.as_posix()
            decision_path.write_text(json.dumps(decision), encoding="utf-8")
            rebinding_path.write_text(
                json.dumps(
                    {
                        "status": "approved_artifact_only_rebinding",
                        "old_compatibility_sha256": decision["decision"]["compatibility_sha256"],
                        "new_compatibility_sha256": compatibility_binding_sha256(compatibility),
                        "decision_sha256": ACCEPTANCE.file_sha256(decision_path),
                    }
                ),
                encoding="utf-8",
            )

            report = ACCEPTANCE.build_report(
                handoff,
                compatibility,
                decision,
                handoff_path=handoff_path,
                compatibility_path=compatibility_path,
                decision_path=decision_path,
                technical_rebinding_path=rebinding_path,
                as_of=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
            )

            self.assertFalse(report["summary"]["accepted"])
            self.assertEqual(report["summary"]["acceptance_status"], "unproven")
            self.assertIn("current_decision_not_bound_to_review_scope", report["review_scope"]["reason_codes"])

            scope = VALIDATOR.validate_decision(
                decision,
                decision_path=decision_path,
                handoff_path=handoff_path,
                compatibility_path=compatibility_path,
                technical_rebinding_path=rebinding_path,
            )
            self.assertFalse(scope["effective_accepted"])
            self.assertEqual(scope["scope_status"], "unproven")


if __name__ == "__main__":
    unittest.main()
