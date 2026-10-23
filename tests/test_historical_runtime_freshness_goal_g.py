from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1]
ARCHIVE = ROOT / "historical-evidence/runtime-freshness-goal-g"
SCRIPT_PATH = ROOT / "scripts/verify-historical-runtime-freshness-goal-g.py"
_SPEC = importlib.util.spec_from_file_location("historical_runtime_freshness_goal_g", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
VERIFY = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(VERIFY)


class HistoricalRuntimeFreshnessGoalGTest(unittest.TestCase):
    def copy_archive(self) -> tuple[tempfile.TemporaryDirectory[str], pathlib.Path]:
        temp = tempfile.TemporaryDirectory(prefix="goal-g-archive-")
        destination = pathlib.Path(temp.name) / "archive"
        shutil.copytree(ARCHIVE, destination, copy_function=shutil.copy2)
        return temp, destination

    def assert_rejected(self, archive: pathlib.Path) -> None:
        with self.assertRaises(AssertionError):
            VERIFY.verify(ROOT, archive)

    def test_fixed_source_and_archive_verify(self) -> None:
        result = VERIFY.verify(ROOT, ARCHIVE)
        self.assertEqual(
            result,
            {
                "status": "ok",
                "prs": 10,
                "originals": 19,
                "verification_rows": 6548,
                "unique_verification_rows_absent_from_main": 5373,
                "failure_observation_rows": 3570,
                "pr_747_duplicate_rows_retained": 346,
            },
        )

    def test_digest_tampering_fails_closed(self) -> None:
        temp, archive = self.copy_archive()
        self.addCleanup(temp.cleanup)
        path = archive / "inventory.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["original_files"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        self.assert_rejected(archive)

    def test_verification_row_omission_fails_closed(self) -> None:
        temp, archive = self.copy_archive()
        self.addCleanup(temp.cleanup)
        path = archive / "deltas/pr-642/verification.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["rows"].pop()
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        self.assert_rejected(archive)

    def test_pr747_duplicate_deletion_fails_closed(self) -> None:
        temp, archive = self.copy_archive()
        self.addCleanup(temp.cleanup)
        path = archive / "deltas/pr-747/verification.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["rows"].pop()
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        self.assert_rejected(archive)


if __name__ == "__main__":
    unittest.main()
