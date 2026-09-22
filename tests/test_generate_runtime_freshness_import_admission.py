from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "generate-runtime-freshness-import-admission.py"
SPEC = importlib.util.spec_from_file_location("generate_runtime_freshness_import_admission", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GenerateRuntimeFreshnessImportAdmissionTest(unittest.TestCase):
    def write_json(self, path: pathlib.Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def fixture(self, root: pathlib.Path, selected_total: int = 1) -> dict[str, pathlib.Path]:
        report = root / ".datapan" / "verification.json"
        receipt = root / ".datapan" / "run-receipt.json"
        proposal = root / ".datapan" / "proposal.json"
        import_receipt = root / "reports" / "runtime-freshness-imports" / "123.json"
        self.write_json(report, {"results": []})
        self.write_json(receipt, {
            "run_id": "123",
            "generated_at": "2026-09-22T00:10:00Z",
            "identity_equality": {
                "equal": True,
                "planned": {"count": 1, "sha256": "1" * 64},
            },
        })
        self.write_json(import_receipt, {"run_id": "123"})
        zero = {"total": 0, "verified": 0, "failed": 0, "skipped": 0, "unknown": 0}
        before = {"total": 10, "verified": 8, "failed": 1, "skipped": 1, "unknown": 0}
        selected = dict(zero)
        selected["total"] = selected_total
        selected["failed"] = selected_total
        after = {key: before[key] + selected[key] for key in before}
        self.write_json(proposal, {
            "run_id": "123",
            "sanitized_report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            "before": before,
            "selected": selected,
            "after": after,
            "delta": selected,
            "selected_new_results": selected_total,
            "selected_identity_set": {
                "count": selected_total,
                "sha256": hashlib.sha256(b"[]" if not selected_total else b"identity").hexdigest(),
            },
        })
        return {"report": report, "receipt": receipt, "proposal": proposal, "import_receipt": import_receipt}

    def build(self, root: pathlib.Path, paths: dict[str, pathlib.Path]) -> dict[str, object]:
        return MODULE.build(
            root=root,
            proposal_path=paths["proposal"],
            report_path=paths["report"],
            receipt_path=paths["receipt"],
            import_receipt_path=paths["import_receipt"],
            producer_repository="StatPan/datapan-registry",
            producer_revision="a" * 40,
            producer_run_id="123",
            producer_run_url="https://github.com/StatPan/datapan-registry/actions/runs/123",
        )

    def test_imported_admission_binds_exact_arithmetic_and_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = self.fixture(root)
            value = self.build(root, paths)
            self.assertEqual(value["outcome"], "imported")
            self.assertEqual(value["arithmetic"]["after"]["total"], 11)
            self.assertEqual(value["inputs"]["import_receipt"]["path"], "reports/runtime-freshness-imports/123.json")

    def test_first_no_change_is_durable_and_exact_replay_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = self.fixture(root, selected_total=0)
            value = self.build(root, paths)
            self.assertEqual(value["outcome"], "no_change")
            output = root / "reports" / "runtime-freshness-import-admissions" / "123.json"
            content = MODULE.render(value)
            MODULE.write_or_check(output, content, check=False)
            first = output.read_bytes()
            MODULE.write_or_check(output, content, check=False)
            self.assertEqual(output.read_bytes(), first)

    def test_reused_run_id_with_different_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = self.fixture(root)
            output = root / "reports" / "runtime-freshness-import-admissions" / "123.json"
            MODULE.write_or_check(output, MODULE.render(self.build(root, paths)), check=False)
            with self.assertRaisesRegex(ValueError, "different admission bytes"):
                MODULE.write_or_check(output, b"{}\n", check=False)

    def test_arithmetic_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = self.fixture(root)
            proposal = json.loads(paths["proposal"].read_text(encoding="utf-8"))
            proposal["after"]["total"] += 1
            proposal["after"]["failed"] += 1
            self.write_json(paths["proposal"], proposal)
            with self.assertRaisesRegex(ValueError, "before plus selected"):
                self.build(root, paths)


if __name__ == "__main__":
    unittest.main()
