from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "apply-runtime-freshness-import.py"
SPEC = importlib.util.spec_from_file_location("apply_runtime_freshness_import", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ApplyRuntimeFreshnessImportTest(unittest.TestCase):
    def test_execute_can_force_lfs_smudge_off(self) -> None:
        with mock.patch.object(MODULE.subprocess, "run") as runner:
            runner.return_value.stdout = ""
            MODULE.execute(["git", "status"], cwd=pathlib.Path.cwd(), env={"GIT_LFS_SKIP_SMUDGE": "1"})
        self.assertEqual(runner.call_args.kwargs["env"]["GIT_LFS_SKIP_SMUDGE"], "1")

    def fixture(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        report, receipt = root / "report.json", root / "receipt.json"
        report.write_text('{"results": []}\n', encoding="utf-8")
        receipt.write_text(json.dumps({"run_id": "run-1"}) + "\n", encoding="utf-8")
        return report, receipt

    def test_pipeline_failure_never_applies_patch_to_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            report, receipt = self.fixture(root)
            commands: list[list[str]] = []

            def fake_execute(command, **kwargs):
                commands.append(command)
                return ""

            with mock.patch.object(MODULE, "require_clean"), mock.patch.object(MODULE, "execute", side_effect=fake_execute), mock.patch.object(MODULE, "run_pipeline", side_effect=RuntimeError("generation failed")):
                with self.assertRaisesRegex(RuntimeError, "generation failed"):
                    MODULE.apply_transaction(root, report, receipt, "datapan", pathlib.Path("reports/import.json"))
            self.assertFalse(any(command[:2] == ["git", "apply"] for command in commands))

    def test_success_checks_patch_before_single_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            report, receipt = self.fixture(root)
            commands: list[list[str]] = []

            def fake_execute(command, **kwargs):
                commands.append(command)
                if command[:3] == ["git", "diff", "--binary"]:
                    return "diff --git a/a b/a\n"
                if command[:3] == ["git", "diff", "--name-only"]:
                    return "reports/import.json\n"
                return ""

            with mock.patch.object(MODULE, "require_clean"), mock.patch.object(MODULE, "execute", side_effect=fake_execute), mock.patch.object(MODULE, "run_pipeline"):
                result = MODULE.apply_transaction(root, report, receipt, "datapan", pathlib.Path("reports/import.json"))
            apply_commands = [command for command in commands if command[:2] == ["git", "apply"]]
            self.assertEqual(len(apply_commands), 2)
            self.assertIn("--check", apply_commands[0])
            self.assertNotIn("--check", apply_commands[1])
            self.assertEqual(result["changed_files"], ["reports/import.json"])


if __name__ == "__main__":
    unittest.main()
