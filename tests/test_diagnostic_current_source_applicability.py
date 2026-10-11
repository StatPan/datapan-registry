import copy
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("diagnostic_source_applicability_test", ROOT / "scripts/diagnostic_source_applicability.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class DiagnosticCurrentSourceApplicabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proof = MODULE.load_json(MODULE.PROOF)
        cls.expected_dataset = cls.proof["datasets"][0]
        cls.expected_operations = [MODULE.expected_operation_identity(item) for item in cls.expected_dataset["operations"]]

    def test_checked_in_historical_scope_is_static_and_never_grants_current_authority(self):
        report = MODULE.load_json(MODULE.REPORT)
        self.assertIn(report["status"], {"historical_scope_unchanged", "revalidation_required"})
        if report["status"] == "historical_scope_unchanged":
            self.assertEqual(report["mismatches"], [])
        else:
            self.assertTrue(report["mismatches"])
        self.assertEqual(len(report["checked_facts"]["health_pointers"]), 2)
        self.assertEqual(len(report["checked_facts"]["registry_datasets"][0]["expected_operations"]), 11)
        dataset_status = report["checked_facts"]["registry_datasets"][0]["status"]
        if dataset_status == "matched":
            self.assertEqual(len(report["checked_facts"]["registry_datasets"][0]["operation_checks"]), 11)
        self.assertTrue(all(value is False for value in report["authority"].values()))
        self.assertNotIn("reports/latest-verification.json", {item["path"] for item in report["historical_inputs"]})
        self.assertNotIn("reports/credential-runtime-manual-review-decision.json", {item["path"] for item in report["historical_inputs"]})

    def test_changed_source_bytes_remain_pending_when_referenced_static_facts_match(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_path = pathlib.Path(directory) / "registry.json"
            registry_path.write_text(json.dumps([{"id": "15000017", "operations": []}]), encoding="utf-8")
            static_fact = {
                "dataset_id": self.expected_dataset["dataset_id"],
                "expected_sha256": self.expected_dataset["dataset_sha256"],
                "actual_sha256": self.expected_dataset["dataset_sha256"],
                "status": "matched",
                "expected_operations": self.expected_operations,
                "actual_operations": self.expected_operations,
                "operation_checks": [{"expected": item, "actual": item, "status": "matched"} for item in self.expected_operations],
            }
            with mock.patch.object(MODULE, "evaluate_registry_identities", return_value=([static_fact], [])):
                report = MODULE.build_report(current_paths={"data/data-go-kr.registry.json": registry_path})
        self.assertEqual(report["status"], "revalidation_required")
        self.assertEqual(report["checked_facts"]["registry_datasets"][0]["status"], "matched")
        self.assertEqual(report["mismatches"][0]["code"], "current_source_bytes_changed")
        self.assertFalse(report["authority"]["publication_allowed"])

    def test_missing_and_ambiguous_referenced_dataset_are_explicit(self):
        missing, missing_reasons = MODULE.evaluate_registry_identities([], self.proof)
        self.assertEqual(missing[0]["status"], "missing")
        self.assertIn("referenced_dataset_missing", {item["code"] for item in missing_reasons})

        duplicate = [{"id": "15000017"}, {"id": "15000017"}]
        ambiguous, ambiguous_reasons = MODULE.evaluate_registry_identities(duplicate, self.proof)
        self.assertEqual(ambiguous[0]["status"], "ambiguous")
        self.assertIn("referenced_dataset_ambiguous", {item["code"] for item in ambiguous_reasons})

    def test_changed_operation_source_digest_is_reported(self):
        current = copy.deepcopy(self.expected_operations)
        current[0]["source_sha256"] = "0" * 64
        with mock.patch.object(MODULE, "current_operation_identities", return_value=(current, True)):
            facts, reasons = MODULE.evaluate_registry_identities([{"id": "15000017", "operations": []}], self.proof)
        self.assertEqual(facts[0]["status"], "mismatch")
        self.assertIn("referenced_operation_mismatch", {item["code"] for item in reasons})

    def test_reordered_and_substituted_operation_identities_fail_closed(self):
        reordered = list(reversed(copy.deepcopy(self.expected_operations)))
        with mock.patch.object(MODULE, "current_operation_identities", return_value=(reordered, True)):
            facts, _ = MODULE.evaluate_registry_identities([{"id": "15000017", "operations": []}], self.proof)
        self.assertEqual(facts[0]["status"], "mismatch")
        self.assertTrue(all(item["status"] == "matched" for item in facts[0]["operation_checks"]))
        self.assertNotEqual(facts[0]["actual_sha256"], facts[0]["expected_sha256"])

        substituted = copy.deepcopy(self.expected_operations)
        substituted[0]["source_url"] = "https://untrusted.invalid/replacement"
        with mock.patch.object(MODULE, "current_operation_identities", return_value=(substituted, True)):
            facts, reasons = MODULE.evaluate_registry_identities([{"id": "15000017", "operations": []}], self.proof)
        self.assertEqual(facts[0]["operation_checks"][0]["status"], "missing")
        self.assertIn("referenced_operation_missing", {item["code"] for item in reasons})

    def test_changed_health_pointer_is_a_pending_fact(self):
        health = MODULE.load_json(MODULE.ROOT / "reports/health-probe-catalog.json")
        health["receipt_contract"]["policy_authority"] = "other-authority"
        with tempfile.TemporaryDirectory() as directory:
            health_path = pathlib.Path(directory) / "health.json"
            health_path.write_text(json.dumps(health), encoding="utf-8")
            registry_path = pathlib.Path(directory) / "registry.json"
            registry_path.write_text("[]", encoding="utf-8")
            with mock.patch.object(MODULE, "evaluate_registry_identities", return_value=([], [])):
                report = MODULE.build_report(current_paths={
                    "reports/health-probe-catalog.json": health_path,
                    "data/data-go-kr.registry.json": registry_path,
                })
        self.assertEqual(report["status"], "revalidation_required")
        self.assertEqual([item["status"] for item in report["checked_facts"]["health_pointers"]], ["mismatch", "mismatch"])
        self.assertEqual(sum(item["code"] == "health_pointer_mismatch" for item in report["mismatches"]), 2)

    def test_changed_non_source_authoritative_input_is_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            changed = pathlib.Path(directory) / "error-action-catalog.json"
            changed.write_text("{}\n", encoding="utf-8")
            registry = pathlib.Path(directory) / "registry.json"
            registry.write_text("[]", encoding="utf-8")
            with mock.patch.object(MODULE, "evaluate_registry_identities", return_value=([], [])):
                report = MODULE.build_report(current_paths={
                    "reports/data-go-kr/error-action-catalog.json": changed,
                    "data/data-go-kr.registry.json": registry,
                })
        self.assertEqual(report["status"], "revalidation_required")
        self.assertIn("non_source_authoritative_input_changed", {item["code"] for item in report["mismatches"]})

    def test_regenerated_authoritative_rollup_is_compared_to_its_archived_mapping_source(self):
        historical = MODULE.load_json(MODULE.ARCHIVED_ERROR_ACTION_ROLLUP)
        historical["generated_at"] = "2026-10-07T00:00:00Z"
        with tempfile.TemporaryDirectory() as directory:
            changed = pathlib.Path(directory) / "error-action-routing-rollup.json"
            changed.write_text(json.dumps(historical, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            registry = pathlib.Path(directory) / "registry.json"
            registry.write_text("[]", encoding="utf-8")
            with mock.patch.object(MODULE, "evaluate_registry_identities", return_value=([], [])):
                report = MODULE.build_report(current_paths={
                    "reports/error-action-routing-rollup.json": changed,
                    "data/data-go-kr.registry.json": registry,
                })
        mismatch = next(item for item in report["mismatches"] if item["path"] == "reports/error-action-routing-rollup.json")
        current = next(item for item in report["current_inputs"]["other_authoritative_inputs"] if item["path"] == "reports/error-action-routing-rollup.json")
        self.assertEqual(report["status"], "revalidation_required")
        self.assertEqual(mismatch["code"], "non_source_authoritative_input_changed")
        self.assertEqual(mismatch["expected"], "d66a5d68ecab483354d1c48c9ce4f443eff77e84cf747ca1bb25c452ca5d8cf1")
        self.assertNotEqual(current["sha256"], mismatch["expected"])
        self.assertEqual(report["archived_authoritative_inputs"][0]["artifact"]["sha256"], mismatch["expected"])
        self.assertTrue(all(value is False for value in report["authority"].values()))

    def test_archive_and_receipt_tampering_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            bad_archive = pathlib.Path(directory) / "health.json"
            bad_archive.write_bytes(MODULE.ARCHIVED_HEALTH.read_bytes() + b" ")
            with mock.patch.object(MODULE, "ARCHIVED_HEALTH", bad_archive):
                with self.assertRaisesRegex(ValueError, "archive digest drift"):
                    MODULE.verify_archive_provenance()

            bad_rollup = pathlib.Path(directory) / "rollup.json"
            bad_rollup.write_bytes(MODULE.ARCHIVED_ERROR_ACTION_ROLLUP.read_bytes() + b" ")
            with mock.patch.object(MODULE, "ARCHIVED_ERROR_ACTION_ROLLUP", bad_rollup):
                with self.assertRaisesRegex(ValueError, "archive digest drift"):
                    MODULE.verify_error_action_rollup_provenance()

        report = MODULE.load_json(MODULE.REPORT)
        report["historical_inputs"][0]["sha256"] = "0" * 64
        with mock.patch.object(MODULE, "build_report", return_value=MODULE.load_json(MODULE.REPORT)):
            with self.assertRaisesRegex(ValueError, "receipt differs"):
                MODULE.validate_report(report)

    def test_publication_gate_rejects_pending_and_historical_only_receipts(self):
        receipt = MODULE.load_json(MODULE.REPORT)
        if receipt["status"] == "revalidation_required":
            with self.assertRaisesRegex(ValueError, "requires revalidation"):
                MODULE.validate_current_publication_gate(receipt)
        else:
            with self.assertRaisesRegex(ValueError, "no explicit current diagnostic publication authority"):
                MODULE.validate_current_publication_gate(receipt)
        receipt["status"] = "revalidation_required"
        receipt["authority"]["current_source_compatibility_approved"] = True
        receipt["authority"]["publication_allowed"] = True
        with self.assertRaisesRegex(ValueError, "requires revalidation"):
            MODULE.validate_current_publication_gate(receipt)

    def test_unmaterialized_registry_pointer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            pointer = pathlib.Path(directory) / "registry.json"
            pointer.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 123\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "unmaterialized Git LFS pointer"):
                MODULE.build_report(current_paths={"data/data-go-kr.registry.json": pointer})


if __name__ == "__main__":
    unittest.main()
