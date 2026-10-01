from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/generate-runtime-evidence-growth.py"
SPEC = importlib.util.spec_from_file_location("runtime_evidence_growth", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
growth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(growth)
VALIDATOR_SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/validate-runtime-evidence-growth.py"
VALIDATOR_SPEC = importlib.util.spec_from_file_location("validate_runtime_evidence_growth", VALIDATOR_SCRIPT)
assert VALIDATOR_SPEC is not None and VALIDATOR_SPEC.loader is not None
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(validator)


class RuntimeEvidenceGrowthTest(unittest.TestCase):
    def build_report(self, current_rows: list[dict[str, object]]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            def write(name: str, value: object) -> str:
                path = root / f"{name}.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                return str(path)

            generated_at = "2026-09-30T00:00:00Z"
            operation_rows = [
                {
                    "identity_key": str(row["identity_key"]),
                    "dataset_id": str(row["dataset_id"]),
                    "operation": str(row["operation"]),
                }
                for row in current_rows
            ]
            dependencies = [
                {
                    "dataset_id": str(row["dataset_id"]),
                    "operation": str(row["operation"]),
                    "dependency_class": "data_go_kr_gateway",
                }
                for row in current_rows
            ]
            inputs = {
                "coverage": write("coverage", {"summary": {
                    "operations": 10,
                    "callable_operations": 10,
                    "data_go_kr_gateway_operations": 10,
                    "external_endpoint_operations": 0,
                    "registered_adapter_operations": 0,
                    "call_capable_adapters": 0,
                }}),
                "latest_verification": write("latest", {"generated_at": generated_at, "results": []}),
                "latest_verification_summary": write("latest-summary", {
                    "generated_at": generated_at,
                    "summary": {"verified": 0, "failed": 0, "skipped": 0, "unknown": 0},
                }),
                "current_runtime_evidence_projection": write("projection", {
                    "summary": {"unbound": 0, "contract_changed": 0, "ambiguous": 0, "historical": 0},
                    "operations": operation_rows,
                    "current_evidence": current_rows,
                }),
                "dependencies": write("dependencies", {"dependencies": dependencies}),
                "verification_plan": write("plan", {
                    "summary": {
                        "planned_batches": 0,
                        "planned_operations": 0,
                        "uncovered_gateway_candidates": 0,
                        "uncovered_adapter_candidates": 0,
                        "missing_adapter_hosts": 0,
                    },
                    "batches": [],
                }),
                "provider_index": write("provider-index", {"split_readiness": {
                    "status": "ready",
                    "adapter_count": 0,
                    "verification_capable_adapters": 0,
                    "call_capable_adapters": 0,
                }}),
            }
            template = root / "template.json"
            template.write_text(json.dumps({
                "schema_version": "datapan.runtime-evidence-growth.v1",
                "provider": "data.go.kr",
                "source_id": "data_go_kr",
                "source_profile": str((pathlib.Path(__file__).parents[1] / "sources/data_go_kr.json").resolve()),
                "generation_inputs": inputs,
            }), encoding="utf-8")
            report = growth.build(template)
            report_path = root / "runtime-evidence-growth.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            schema_path = pathlib.Path(__file__).parents[1] / "schemas/datapan.runtime-evidence-growth.v1.schema.json"
            import jsonschema

            jsonschema.Draft202012Validator(validator.load_json(schema_path)).validate(report)
            validator.validate_consistency(report_path, report)
            return report

    @staticmethod
    def row(index: int, disposition: str, status: str) -> dict[str, object]:
        return {
            "identity_key": f"data_go_kr:dataset-{index}:operation-{index}",
            "dataset_id": f"dataset-{index}",
            "operation": f"operation-{index}",
            "disposition": disposition,
            "status": status,
        }

    def test_observation_totals_are_separate_from_fresh_verified_success_target(self) -> None:
        rows = [
            self.row(1, "eligible", "verified"),
            self.row(2, "stale", "verified"),
            self.row(3, "recent_non_verified", "failed"),
            self.row(4, "recent_non_verified", "skipped"),
        ]

        report = self.build_report(rows)

        evidence = report["evidence"]
        target = report["growth_target"]
        self.assertEqual(evidence["total"], 4)
        self.assertEqual(evidence["current_bound_observations"], 4)
        self.assertEqual(evidence["verified"], 2)
        self.assertEqual(evidence["failed"], 1)
        self.assertEqual(evidence["skipped"], 1)
        self.assertEqual(evidence["fresh_verified"], 1)
        self.assertEqual(evidence["success_coverage_percent"], 10.0)
        self.assertEqual(target["target_basis"], "fresh_verified_current_contract")
        self.assertEqual(target["fresh_verified_total"], 1)
        self.assertEqual(target["remaining_to_target"], 0)
        self.assertEqual(target["status"], "at_target")

    def test_bound_failed_and_skipped_only_do_not_meet_success_target(self) -> None:
        report = self.build_report([
            self.row(1, "recent_non_verified", "failed"),
            self.row(2, "recent_non_verified", "skipped"),
        ])

        self.assertEqual(report["evidence"]["current_bound_observations"], 2)
        self.assertEqual(report["evidence"]["fresh_verified"], 0)
        self.assertEqual(report["evidence"]["success_coverage_percent"], 0.0)
        self.assertEqual(report["growth_target"]["remaining_to_target"], 1)
        self.assertEqual(report["growth_target"]["status"], "below_target")
        self.assertTrue(any(warning["kind"] == "runtime_evidence_below_target" for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
