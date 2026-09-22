from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "consolidate-runtime-freshness-run.py"
SPEC = importlib.util.spec_from_file_location("consolidate_runtime_freshness_run", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ConsolidateRuntimeFreshnessRunTest(unittest.TestCase):
    def write_json(self, path: pathlib.Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    def read_json(self, path: pathlib.Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))


    def fixture(self, root: pathlib.Path, duplicate: bool = False) -> pathlib.Path:
        combined_results = []
        for shard in range(2):
            operation = "o0" if duplicate else f"o{shard}"
            directory = root / f"shard-{shard}"
            plan = {
                "selection": {"shard_index": shard, "shard_count": 2},
                "operations": [{
                    "identity_key": f"data_go_kr:d:{operation}",
                    "dataset_id": "d",
                    "operation": operation,
                }],
            }
            result = {"dataset_id": "d", "operation": operation, "status": "verified"}
            self.write_json(directory / "batch-plan.json", plan)
            self.write_json(directory / "verification.json", {"results": [result]})
            (directory / "exit-code.txt").write_text("0\n", encoding="utf-8")
            combined_results.append(result)
        combined = root / "consolidated" / "verification.json"
        self.write_json(combined, {"results": combined_results})
        return combined

    def test_complete_shards_reconcile_and_bind_identity_sets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            combined = self.fixture(root)
            report = MODULE.build(root, combined, expected_shards=2, run_id="run")
            self.assertEqual(report["summary"]["planned_operations"], 2)
            self.assertEqual(report["summary"]["reported_results"], 2)
            self.assertEqual(report["summary"]["verified"], 2)
            equality = report["identity_equality"]
            self.assertTrue(equality["equal"])
            self.assertEqual(equality["planned"], equality["reported"])
            self.assertEqual(equality["planned"]["count"], 2)
            self.assertEqual(
                [row["identity_key"] for row in self.read_json(combined)["results"]],
                ["data_go_kr:d:o0", "data_go_kr:d:o1"],
            )

    def test_duplicate_cross_shard_identity_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with self.assertRaisesRegex(ValueError, "duplicate planned identity"):
                MODULE.build(root, self.fixture(root, duplicate=True), expected_shards=2, run_id="run")

    def test_planned_but_unreported_identity_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            combined = self.fixture(root)
            self.write_json(root / "shard-1" / "verification.json", {"results": []})
            payload = self.read_json(combined)
            payload["results"].pop()
            self.write_json(combined, payload)
            with self.assertRaisesRegex(ValueError, "planned identities missing results"):
                MODULE.build(root, combined, expected_shards=2, run_id="run")

    def test_duplicate_reported_identity_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            combined = self.fixture(root)
            verification = root / "shard-0" / "verification.json"
            payload = self.read_json(verification)
            payload["results"].append(dict(payload["results"][0]))
            self.write_json(verification, payload)
            with self.assertRaisesRegex(ValueError, "duplicate reported identity"):
                MODULE.build(root, combined, expected_shards=2, run_id="run")

    def test_unplanned_result_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            combined = self.fixture(root)
            verification = root / "shard-0" / "verification.json"
            payload = self.read_json(verification)
            payload["results"].append({"dataset_id": "d", "operation": "extra", "status": "verified"})
            self.write_json(verification, payload)
            with self.assertRaisesRegex(ValueError, "unplanned result identity"):
                MODULE.build(root, combined, expected_shards=2, run_id="run")

    def test_empty_planned_identity_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            combined = self.fixture(root)
            plan_path = root / "shard-0" / "batch-plan.json"
            plan = self.read_json(plan_path)
            plan["operations"][0]["identity_key"] = ""
            self.write_json(plan_path, plan)
            with self.assertRaisesRegex(ValueError, "empty planned identity"):
                MODULE.build(root, combined, expected_shards=2, run_id="run")

    def test_empty_result_identity_component_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            combined = self.fixture(root)
            payload = self.read_json(combined)
            payload["results"][0]["operation"] = ""
            self.write_json(combined, payload)
            with self.assertRaisesRegex(ValueError, "empty operation"):
                MODULE.build(root, combined, expected_shards=2, run_id="run")

    def test_ambiguous_result_identity_mapping_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            combined = self.fixture(root)
            plan_path = root / "shard-1" / "batch-plan.json"
            plan = self.read_json(plan_path)
            plan["operations"][0]["operation"] = "o0"
            self.write_json(plan_path, plan)
            with self.assertRaisesRegex(ValueError, "ambiguous result identity mapping"):
                MODULE.build(root, combined, expected_shards=2, run_id="run")

    def test_shifted_shard_indices_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            combined = self.fixture(root)
            for shard, shifted in enumerate((1, 2)):
                path = root / f"shard-{shard}" / "batch-plan.json"
                plan = self.read_json(path)
                plan["selection"]["shard_index"] = shifted
                self.write_json(path, plan)
            with self.assertRaisesRegex(ValueError, r"shard indices must equal \[0, 1\]"):
                MODULE.build(root, combined, expected_shards=2, run_id="run")

    def test_missing_shard_index_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            combined = self.fixture(root)
            path = root / "shard-1" / "batch-plan.json"
            plan = self.read_json(path)
            del plan["selection"]["shard_index"]
            self.write_json(path, plan)
            with self.assertRaisesRegex(ValueError, "missing shard index"):
                MODULE.build(root, combined, expected_shards=2, run_id="run")

    def test_duplicate_shard_indices_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            combined = self.fixture(root)
            path = root / "shard-1" / "batch-plan.json"
            plan = self.read_json(path)
            plan["selection"]["shard_index"] = 0
            self.write_json(path, plan)
            with self.assertRaisesRegex(ValueError, r"shard indices must equal \[0, 1\]"):
                MODULE.build(root, combined, expected_shards=2, run_id="run")

    def test_sanitize_removes_request_and_secret_bearing_fields(self) -> None:
        value = {"results": [{"url": "https://example.test?serviceKey=REDACTED", "body": "hidden", "params": {"apiKey": "hidden", "safe": "yes"}}]}
        sanitized = MODULE.sanitize(value)
        MODULE.scan_boundary(sanitized)
        self.assertEqual(sanitized, {"results": [{"params": {"safe": "yes"}}]})

    def test_sanitize_redacts_credential_assignment_in_reason(self) -> None:
        value = {"results": [{"reason": "upstream rejected serviceKey=actual-value; retry later"}]}
        sanitized = MODULE.sanitize(value)
        MODULE.scan_boundary(sanitized)
        self.assertEqual(
            sanitized,
            {"results": [{"reason": "upstream rejected [redacted credential assignment]; retry later"}]},
        )

    def test_sanitize_redacts_other_secret_like_diagnostics(self) -> None:
        value = {
            "reason": "authorization: bearer abcdefghijklmnopqrstuvwxyz token=actual-token api_key=actual-key",
        }
        sanitized = MODULE.sanitize(value)
        MODULE.scan_boundary(sanitized)
        self.assertEqual(
            sanitized,
            {"reason": "[redacted authorization] [redacted credential assignment] [redacted credential assignment]"},
        )

    def test_cli_consolidates_reason_with_credential_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            combined = self.fixture(root)
            payload = json.loads(combined.read_text(encoding="utf-8"))
            payload["results"][0]["reason"] = "upstream rejected servicekey=actual-value"
            self.write_json(combined, payload)
            sanitized = root / "output" / "verification.json"
            receipt = root / "output" / "run-receipt.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--root",
                    str(root),
                    "--combined",
                    str(combined),
                    "--sanitized-output",
                    str(sanitized),
                    "--expected-shards",
                    "2",
                    "--run-id",
                    "test-run",
                    "--output",
                    str(receipt),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            MODULE.scan_boundary(json.loads(sanitized.read_text(encoding="utf-8")))
            self.assertTrue(receipt.is_file())

    def test_secret_like_string_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "secret-like"):
            MODULE.scan_boundary({"note": "serviceKey=actual-value"})


if __name__ == "__main__":
    unittest.main()
