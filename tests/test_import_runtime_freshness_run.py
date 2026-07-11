from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "import-runtime-freshness-run.py"
SPEC = importlib.util.spec_from_file_location("import_runtime_freshness_run", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ImportRuntimeFreshnessRunTest(unittest.TestCase):
    def write_json(self, path: pathlib.Path, value: object) -> None:
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def fixture(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
        report = root / "incoming.json"
        receipt = root / "receipt.json"
        current = root / "current.json"
        summary = root / "summary.json"
        incoming = {"results": [{"dataset_id": "d", "operation": "o", "status": "failed", "checked_at": "2026-07-11T00:00:00Z"}]}
        self.write_json(report, incoming)
        data = report.read_bytes()
        self.write_json(receipt, {
            "run_id": "run-1",
            "summary": {"reported_results": 1, "verified": 0, "failed": 1, "skipped": 0, "unknown": 0},
            "combined_verification": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
            "redaction": {"secret_values_present": False, "secret_hashes_present": False, "request_urls_present": False, "response_bodies_present": False},
        })
        self.write_json(current, {"results": [{"dataset_id": "old", "operation": "o", "status": "verified"}]})
        return report, receipt, current, summary

    @staticmethod
    def fake_datapan(command: list[str]) -> None:
        output = pathlib.Path(command[command.index("--output") + 1])
        if "merge" in command:
            inputs = [pathlib.Path(command[index + 1]) for index, value in enumerate(command) if value == "--input"]
            results = []
            for path in inputs:
                results.extend(json.loads(path.read_text(encoding="utf-8"))["results"])
            output.write_text(json.dumps({"results": results}) + "\n", encoding="utf-8")
        else:
            source = pathlib.Path(command[command.index("--input") + 1])
            results = json.loads(source.read_text(encoding="utf-8"))["results"]
            output.write_text(json.dumps({"summary": {"total": len(results)}}) + "\n", encoding="utf-8")

    def test_dry_run_is_non_mutating_and_failure_does_not_raise_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report, receipt, current, summary = self.fixture(pathlib.Path(directory))
            original = current.read_bytes()
            with mock.patch.object(MODULE, "run", side_effect=self.fake_datapan):
                proposal = MODULE.import_run(report_path=report, receipt_path=receipt, current_path=current, summary_path=summary, datapan_command=["datapan"], apply=False)
            self.assertEqual(current.read_bytes(), original)
            self.assertFalse(summary.exists())
            self.assertEqual(proposal["delta"]["verified"], 0)
            self.assertEqual(proposal["delta"]["failed"], 1)

    def test_apply_writes_only_after_validated_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report, receipt, current, summary = self.fixture(pathlib.Path(directory))
            with mock.patch.object(MODULE, "run", side_effect=self.fake_datapan):
                proposal = MODULE.import_run(report_path=report, receipt_path=receipt, current_path=current, summary_path=summary, datapan_command=["datapan"], apply=True)
            self.assertEqual(proposal["status"], "applied")
            self.assertEqual(len(json.loads(current.read_text(encoding="utf-8"))["results"]), 2)
            self.assertTrue(summary.is_file())

    def test_reimport_of_exact_results_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report, receipt, current, summary = self.fixture(pathlib.Path(directory))
            current.write_bytes(report.read_bytes())
            self.write_json(summary, {"summary": {"total": 1}})
            with mock.patch.object(MODULE, "run", side_effect=self.fake_datapan) as runner:
                proposal = MODULE.import_run(report_path=report, receipt_path=receipt, current_path=current, summary_path=summary, datapan_command=["datapan"], apply=False)
            self.assertEqual(proposal["selected_new_results"], 0)
            self.assertEqual(proposal["delta"]["total"], 0)
            self.assertEqual(runner.call_count, 0)  # exact replay preserves both checked-in artifacts

    def test_digest_mismatch_fails_before_datapan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report, receipt, current, summary = self.fixture(pathlib.Path(directory))
            report.write_text(report.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with mock.patch.object(MODULE, "run") as runner, self.assertRaisesRegex(ValueError, "digest"):
                MODULE.import_run(report_path=report, receipt_path=receipt, current_path=current, summary_path=summary, datapan_command=["datapan"], apply=True)
            runner.assert_not_called()

    def test_unsafe_field_fails_before_datapan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report, receipt, current, summary = self.fixture(pathlib.Path(directory))
            unsafe = {"results": [{"dataset_id": "d", "operation": "o", "status": "failed", "url": "https://example.test"}]}
            self.write_json(report, unsafe)
            data = report.read_bytes()
            receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
            receipt_value["combined_verification"] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            self.write_json(receipt, receipt_value)
            with mock.patch.object(MODULE, "run") as runner, self.assertRaisesRegex(ValueError, "forbidden field"):
                MODULE.import_run(report_path=report, receipt_path=receipt, current_path=current, summary_path=summary, datapan_command=["datapan"], apply=True)
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
