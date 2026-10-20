from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/verify-release.yml"
MODULE_PATH = ROOT / "scripts/refresh-release-ledger-evidence.py"
SPEC = importlib.util.spec_from_file_location("refresh_release_ledger_evidence_gate", MODULE_PATH)
assert SPEC and SPEC.loader
refresh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = refresh
SPEC.loader.exec_module(refresh)


COMPLETENESS_MODULES = (
    "tests/test_generate_completeness_proof_rollup.py",
    "tests/test_completeness_publication_evidence.py",
    "tests/test_completeness_scope_registration.py",
    "tests/test_completeness_proof_rollup_cli.py",
    "tests/test_completeness_future_observation_chain.py",
)


def _run_steps(workflow: dict) -> list[str]:
    runs: list[str] = []
    for step in workflow["jobs"]["verify"]["steps"]:
        if isinstance(step, dict) and isinstance(step.get("run"), str):
            runs.append(step["run"])
    return runs


class VerifyReleaseGateStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def test_required_verify_job_has_exactly_one_unconditional_refresh_check(self) -> None:
        runs = _run_steps(self.workflow)
        matches = [run for run in runs if "scripts/refresh-release-ledger-evidence.py --check" in run]
        self.assertEqual(len(matches), 1)
        command = matches[0]
        self.assertNotIn("|| true", command)
        self.assertNotIn("continue-on-error", command)
        refresh_line = next(line for line in command.splitlines() if "scripts/refresh-release-ledger-evidence.py --check" in line)
        self.assertNotIn("#", refresh_line)
        verify = self.workflow["jobs"]["verify"]
        self.assertNotIn("if", verify)
        for step in verify["steps"]:
            self.assertFalse(step.get("continue-on-error", False))
        refresh_step = next(step for step in verify["steps"] if "scripts/refresh-release-ledger-evidence.py --check" in step.get("run", ""))
        self.assertNotIn("if", refresh_step)

    def test_completeness_contract_and_selector_difference_are_exact(self) -> None:
        command = refresh.COMPLETENESS_REGRESSION_COMMAND.argv
        self.assertEqual(command[0:3], ("python3", "-m", "unittest"))
        self.assertEqual(command[3:], COMPLETENESS_MODULES)
        self.assertEqual(sum(item == refresh.COMPLETENESS_REGRESSION_COMMAND for item in refresh.CHECK_COMMANDS), 1)
        with mock.patch.dict(os.environ, {refresh.COMPLETENESS_REGRESSION_SKIP_ENV: "1"}, clear=True):
            selected = refresh.selected_check_commands()
        self.assertEqual([c for c in refresh.CHECK_COMMANDS if c not in selected], [refresh.COMPLETENESS_REGRESSION_COMMAND])
        self.assertEqual(selected, tuple(c for c in refresh.CHECK_COMMANDS if c != refresh.COMPLETENESS_REGRESSION_COMMAND))

    def test_opt_out_is_not_workflow_global_job_or_step_environment(self) -> None:
        for path in (ROOT / ".github/workflows/verify-release.yml", ROOT / ".github/workflows/runtime-freshness-import.yml"):
            if not path.exists():
                continue
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertNotIn(refresh.COMPLETENESS_REGRESSION_SKIP_ENV, value.get("env", {}))
            for job in value.get("jobs", {}).values():
                self.assertNotIn(refresh.COMPLETENESS_REGRESSION_SKIP_ENV, job.get("env", {}))
                for step in job.get("steps", []):
                    self.assertNotIn(refresh.COMPLETENESS_REGRESSION_SKIP_ENV, step.get("env", {}))

    def test_default_failure_and_opt_out_failure_propagation(self) -> None:
        failing = type("Result", (), {"returncode": 1})()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(refresh.subprocess, "run", return_value=failing):
            self.assertFalse(refresh.run_check_commands(refresh.selected_check_commands()))
        with mock.patch.dict(os.environ, {refresh.COMPLETENESS_REGRESSION_SKIP_ENV: "1"}, clear=True), mock.patch.object(refresh.subprocess, "run", return_value=failing):
            self.assertFalse(refresh.run_check_commands(refresh.selected_check_commands()))
        with self.assertRaises(refresh.ConvergenceError):
            refresh.converge(max_iterations=1, refresh_once=lambda _: True, checks_pass=lambda _: False)


if __name__ == "__main__":
    unittest.main()
