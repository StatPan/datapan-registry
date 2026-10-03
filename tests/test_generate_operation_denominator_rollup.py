from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "generate-operation-denominator-rollup.py"
EXPECTED_SOURCE_IDS = {
    "data_go_kr",
    "ecos",
    "kosis",
    "open_assembly",
    "seoul_open_data",
}
SPEC = importlib.util.spec_from_file_location("generate_operation_denominator_rollup", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OperationDenominatorRollupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = MODULE.load(ROOT / "policy" / "sustainable-coverage.json")

    def test_current_policy_has_exact_five_source_denominators(self) -> None:
        result = MODULE.build(self.policy)
        policy_sources = self.policy["supported_sources"]
        self.assertEqual(len(policy_sources), len(EXPECTED_SOURCE_IDS))
        self.assertEqual({source["source_id"] for source in policy_sources}, EXPECTED_SOURCE_IDS)

        # Derive each expected contribution independently from the policy-bound
        # report. Aggregate reports intentionally have no operation identities;
        # their summary counts are the source contract.
        source_reports = {
            source["source_id"]: json.loads(
                (ROOT / source["coverage_report"]).read_text(encoding="utf-8")
            )
            for source in policy_sources
        }
        result_rows = result["sources"]
        rows_by_source = {row["source_id"]: row for row in result_rows}
        self.assertEqual(len(result_rows), len(EXPECTED_SOURCE_IDS))
        self.assertEqual(set(rows_by_source), EXPECTED_SOURCE_IDS)

        for source in policy_sources:
            source_id = source["source_id"]
            report = source_reports[source_id]
            with self.subTest(source_id=source_id):
                self.assertEqual(
                    rows_by_source[source_id],
                    {
                        "source_id": source_id,
                        "path": source["coverage_report"],
                        "scope_kind": report["scope"]["kind"],
                        "operations": report["summary"]["operations"],
                        "callable_operations": report["summary"]["callable_operations"],
                    },
                )

        self.assertEqual(
            result["summary"],
            {
                "sources": len(EXPECTED_SOURCE_IDS),
                "operations": sum(report["summary"]["operations"] for report in source_reports.values()),
                "callable_operations": sum(report["summary"]["callable_operations"] for report in source_reports.values()),
            },
        )

    def test_aggregate_catalogue_growth_is_reflected_without_enumerating_rows(self) -> None:
        reports = {
            source["source_id"]: json.loads(
                (ROOT / source["coverage_report"]).read_text(encoding="utf-8")
            )
            for source in self.policy["supported_sources"]
        }
        aggregate = reports["data_go_kr"]
        self.assertEqual(aggregate["scope"]["kind"], "aggregate_supported_catalog")
        self.assertEqual(aggregate["operations"], [])
        baseline_operations = sum(report["summary"]["operations"] for report in reports.values())
        baseline_callable = sum(report["summary"]["callable_operations"] for report in reports.values())

        def add_supported_operation(value: dict) -> None:
            # Aggregate catalogues are count-based and must remain unenumerated.
            value["summary"]["operations"] += 1
            value["summary"]["callable_operations"] += 1

        policy = self.fixture_policy(add_supported_operation, source_id="data_go_kr")
        result = MODULE.build(policy)
        grown_row = next(row for row in result["sources"] if row["source_id"] == "data_go_kr")

        self.assertEqual(grown_row["scope_kind"], "aggregate_supported_catalog")
        self.assertEqual(grown_row["operations"], aggregate["summary"]["operations"] + 1)
        self.assertEqual(grown_row["callable_operations"], aggregate["summary"]["callable_operations"] + 1)
        self.assertEqual(result["summary"]["operations"], baseline_operations + 1)
        self.assertEqual(result["summary"]["callable_operations"], baseline_callable + 1)

    def fixture_policy(self, mutate, *, source_id: str = "ecos") -> dict:
        policy = copy.deepcopy(self.policy)
        source = next(row for row in policy["supported_sources"] if row["source_id"] == source_id)
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

    def test_duplicate_source_contribution_fails(self) -> None:
        policy = copy.deepcopy(self.policy)
        policy["supported_sources"].append(copy.deepcopy(policy["supported_sources"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate source denominator: data_go_kr"):
            MODULE.build(policy)


if __name__ == "__main__":
    unittest.main()
