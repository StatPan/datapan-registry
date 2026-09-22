from __future__ import annotations

import importlib.util
import hashlib
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

    @staticmethod
    def producer() -> dict[str, str]:
        return {
            "repository": "StatPan/datapan-registry",
            "revision": "a" * 40,
            "run_id": "run-1",
            "run_url": "https://github.com/StatPan/datapan-registry/actions/runs/run-1",
        }

    @staticmethod
    def copy_admission_schema(worktree: pathlib.Path) -> None:
        target = worktree / "schemas/datapan.runtime-freshness-import-admission.v1.schema.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        source = pathlib.Path(__file__).parents[1] / "schemas/datapan.runtime-freshness-import-admission.v1.schema.json"
        target.write_bytes(source.read_bytes())

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
                    MODULE.apply_transaction(
                        root,
                        report,
                        receipt,
                        "datapan",
                        pathlib.Path("reports/import.json"),
                        pathlib.Path("reports/runtime-freshness-import-admissions/run-1.json"),
                        self.producer(),
                    )
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
                    return "reports/import.json\nreports/runtime-freshness-import-admissions/run-1.json\n"
                return ""

            def fake_pipeline(worktree, report, receipt, datapan, import_receipt, admission, producer):
                path = worktree / admission
                path.parent.mkdir(parents=True)
                path.write_text("{}\n", encoding="utf-8")
                return {"selected_new_results": 1}

            with mock.patch.object(MODULE, "require_clean"), mock.patch.object(MODULE, "execute", side_effect=fake_execute), mock.patch.object(MODULE, "run_pipeline", side_effect=fake_pipeline):
                result = MODULE.apply_transaction(
                    root,
                    report,
                    receipt,
                    "datapan",
                    pathlib.Path("reports/import.json"),
                    pathlib.Path("reports/runtime-freshness-import-admissions/run-1.json"),
                    self.producer(),
                )
            apply_commands = [command for command in commands if command[:2] == ["git", "apply"]]
            self.assertEqual(len(apply_commands), 2)
            self.assertIn("--check", apply_commands[0])
            self.assertNotIn("--check", apply_commands[1])
            self.assertEqual(result["changed_files"], [
                "reports/import.json",
                "reports/runtime-freshness-import-admissions/run-1.json",
            ])
            self.assertEqual(result["outcome"], "imported")

    def test_exact_replay_short_circuits_without_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            report, receipt = self.fixture(root)
            worktree = root / "worktree"
            self.copy_admission_schema(worktree)
            admission = pathlib.Path("reports/runtime-freshness-import-admissions/run-1.json")
            import_receipt = pathlib.Path("reports/import.json")
            (worktree / admission).parent.mkdir(parents=True)
            (worktree / import_receipt).parent.mkdir(parents=True, exist_ok=True)
            (worktree / import_receipt).write_text('{"run_id":"run-1"}\n', encoding="utf-8")

            zero = {"total": 0, "verified": 0, "failed": 0, "skipped": 0, "unknown": 0}
            value = {
                "schema_version": "datapan.runtime-freshness-import-admission.v1",
                "admitted_at": "2026-09-22T00:00:00Z",
                "run_id": "run-1",
                "outcome": "no_change",
                "producer": self.producer(),
                "inputs": {
                    "sanitized_report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                    "run_receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                    "import_receipt": {
                        "path": import_receipt.as_posix(),
                        "sha256": hashlib.sha256((worktree / import_receipt).read_bytes()).hexdigest(),
                    },
                    "planned_identity_set": {"count": 0, "sha256": "0" * 64},
                },
                "arithmetic": {
                    "before": zero,
                    "selected": zero,
                    "after": zero,
                    "selected_new_results": 0,
                },
                "selected_identity_set": {"count": 0, "sha256": "0" * 64},
            }
            (worktree / admission).write_text(json.dumps(value) + "\n", encoding="utf-8")
            self.assertEqual(
                MODULE.validate_replay(
                    worktree=worktree,
                    report=report,
                    run_receipt=receipt,
                    import_receipt=import_receipt,
                    admission=admission,
                    producer=self.producer(),
                )["outcome"],
                "no_change",
            )

    def test_run_id_reuse_with_different_artifact_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            report, receipt = self.fixture(root)
            worktree = root / "worktree"
            self.copy_admission_schema(worktree)
            admission = pathlib.Path("reports/runtime-freshness-import-admissions/run-1.json")
            import_receipt = pathlib.Path("reports/import.json")
            (worktree / admission).parent.mkdir(parents=True)
            (worktree / import_receipt).parent.mkdir(parents=True, exist_ok=True)
            (worktree / import_receipt).write_text("{}\n", encoding="utf-8")
            zero = {"total": 0, "verified": 0, "failed": 0, "skipped": 0, "unknown": 0}
            one = {"total": 1, "verified": 0, "failed": 1, "skipped": 0, "unknown": 0}
            value = {
                "schema_version": "datapan.runtime-freshness-import-admission.v1",
                "admitted_at": "2026-09-22T00:00:00Z",
                "run_id": "run-1",
                "outcome": "imported",
                "producer": self.producer(),
                "inputs": {
                    "sanitized_report_sha256": "0" * 64,
                    "run_receipt_sha256": "0" * 64,
                    "import_receipt": {"path": import_receipt.as_posix(), "sha256": "0" * 64},
                    "planned_identity_set": {"count": 1, "sha256": "0" * 64},
                },
                "arithmetic": {
                    "before": zero,
                    "selected": one,
                    "after": one,
                    "selected_new_results": 1,
                },
                "selected_identity_set": {"count": 1, "sha256": "0" * 64},
            }
            (worktree / admission).write_text(json.dumps(value) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different artifact bytes"):
                MODULE.validate_replay(
                    worktree=worktree,
                    report=report,
                    run_receipt=receipt,
                    import_receipt=import_receipt,
                    admission=admission,
                    producer=self.producer(),
                )


if __name__ == "__main__":
    unittest.main()
