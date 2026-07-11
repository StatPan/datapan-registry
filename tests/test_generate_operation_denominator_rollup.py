from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "generate-operation-denominator-rollup.py"
SPEC = importlib.util.spec_from_file_location("generate_operation_denominator_rollup", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OperationDenominatorRollupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = MODULE.load(ROOT / "policy" / "sustainable-coverage.json")

    def test_current_policy_has_exact_five_source_denominators(self) -> None:
        result = MODULE.build(self.policy)
        self.assertEqual(result["summary"]["sources"], 5)
        self.assertEqual(result["summary"]["operations"], 21260)
        self.assertEqual({row["source_id"] for row in result["sources"]}, {"data_go_kr", "ecos", "kosis", "open_assembly", "seoul_open_data"})

    def fixture_policy(self, mutate) -> dict:
        policy = copy.deepcopy(self.policy)
        source = policy["supported_sources"][1]
        denominator = MODULE.load(ROOT / source["coverage_report"])
        mutate(denominator)
        temporary = tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8", delete=False)
        self.addCleanup(pathlib.Path(temporary.name).unlink, missing_ok=True)
        json.dump(denominator, temporary)
        temporary.close()
        source["coverage_report"] = temporary.name
        return policy

    def test_source_mismatch_fails(self) -> None:
        policy = self.fixture_policy(lambda value: value.__setitem__("source_id", "wrong"))
        with self.assertRaisesRegex(ValueError, "source_id does not match"):
            MODULE.build(policy)

    def test_duplicate_operation_identity_fails(self) -> None:
        def duplicate(value: dict) -> None:
            value["operations"].append(copy.deepcopy(value["operations"][0]))
            value["summary"] = {"operations": 2, "callable_operations": 2}
        policy = self.fixture_policy(duplicate)
        with self.assertRaisesRegex(ValueError, "duplicate operation identity"):
            MODULE.build(policy)


if __name__ == "__main__":
    unittest.main()
