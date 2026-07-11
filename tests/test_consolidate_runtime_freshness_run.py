from __future__ import annotations

import importlib.util
import json
import pathlib
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

    def fixture(self, root: pathlib.Path, duplicate: bool = False) -> pathlib.Path:
        combined_results = []
        for shard in range(2):
            operation = "o0" if duplicate else f"o{shard}"
            directory = root / f"shard-{shard}"
            plan = {"selection": {"shard_index": shard}, "operations": [{"identity_key": f"data_go_kr:d:{operation}", "dataset_id": "d", "operation": operation}]}
            result = {"dataset_id": "d", "operation": operation, "status": "verified"}
            self.write_json(directory / "batch-plan.json", plan)
            self.write_json(directory / "verification.json", {"results": [result]})
            (directory / "exit-code.txt").write_text("0\n", encoding="utf-8")
            combined_results.append(result)
        combined = root / "consolidated" / "verification.json"
        self.write_json(combined, {"results": combined_results})
        return combined

    def test_complete_shards_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            report = MODULE.build(root, self.fixture(root), expected_shards=2, run_id="run")
            self.assertEqual(report["summary"]["planned_operations"], 2)
            self.assertEqual(report["summary"]["reported_results"], 2)
            self.assertEqual(report["summary"]["verified"], 2)

    def test_duplicate_cross_shard_identity_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                MODULE.build(root, self.fixture(root, duplicate=True), expected_shards=2, run_id="run")


if __name__ == "__main__":
    unittest.main()
