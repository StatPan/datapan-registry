from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "generate-source-runtime-remediation-map.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("generate_source_runtime_remediation_map", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReceiptResolvedFindingTest(unittest.TestCase):
    def test_only_relief_eligible_manual_boundary_is_resolved(self) -> None:
        self.assertTrue(MODULE.receipt_resolves_finding({
            "status": "manual_review_boundary",
            "reviewed_receipt_linkage": {"source_relief_eligible": True},
        }))
        self.assertFalse(MODULE.receipt_resolves_finding({
            "status": "manual_review_boundary",
            "reviewed_receipt_linkage": {"source_relief_eligible": False},
        }))
        self.assertFalse(MODULE.receipt_resolves_finding({
            "status": "follow_up_required",
            "reviewed_receipt_linkage": {"source_relief_eligible": True},
        }))
        self.assertFalse(MODULE.receipt_resolves_finding({"status": "manual_review_boundary"}))


if __name__ == "__main__":
    unittest.main()
