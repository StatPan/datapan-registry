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



# Exact argv strings removed from the policy-artifact step because refresh
# --check repeats them on the same checkout. Each is read-only under --check,
# nothing earlier in the step writes the checkout, and none is a fragment that
# generate-release-assembly-receipt.py requires to stay in verify-release.yml.
# The other exact duplicates stay in the step: the assembly receipt fails when
# their text is absent, so removing them would make the required gate red.
WORKFLOW_EXACT_DUPLICATE_FAST_CHECKS = (
    "python3 scripts/sync-release-schema-artifacts.py --check",
    "python3 scripts/sync-release-manifest-artifacts.py --check",
    "python3 scripts/validate-source-reference-drift.py",
    "python3 scripts/validate-source-runtime-candidates.py",
    "python3 scripts/validate-source-runtime-evidence-plans.py",
    "python3 scripts/generate-diagnostic-current-source-applicability.py --check",
    "python3 scripts/validate-diagnostic-current-source-applicability.py",
    "python3 -m unittest tests/test_diagnostic_current_source_applicability.py",
    "python3 scripts/validate-regional-baseline-source-provenance.py",
    "python3 scripts/generate-failure-recovery-rollup.py --check",
    "python3 scripts/generate-operation-denominator-rollup.py --check",
    "python3 scripts/generate-completeness-proof-rollup.py --check",
    "python3 scripts/validate-data-go-kr-operation-manifest.py",
    "python3 scripts/generate-current-runtime-evidence-projection.py --check",
    "python3 scripts/generate-runtime-freshness-queue.py --check",
    "python3 scripts/generate-sustainable-coverage.py --check",
    "python3 scripts/validate-coverage-backlog.py",
    "python3 scripts/validate-institution-api-overview.py",
    "python3 scripts/generate-runtime-evidence-growth.py --check",
    "python3 scripts/generate-readme-runtime-snapshot.py --check",
    "python3 scripts/generate-release-shard-consumer-proof.py --check",
    "python3 scripts/generate-release-operational-pressure.py --check",
)


def _policy_artifact_step(workflow: dict) -> str:
    verify = workflow["jobs"]["verify"]
    step = next(item for item in verify["steps"] if item.get("name") == "Validate registry policy artifacts")
    return step["run"]


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


    def test_deduplicated_fast_checks_still_run_inside_required_refresh_check(self) -> None:
        labels = [command.label for command in refresh.CHECK_COMMANDS]
        for label in WORKFLOW_EXACT_DUPLICATE_FAST_CHECKS:
            self.assertEqual(labels.count(label), 1, label)
        selected = refresh.selected_check_commands()
        self.assertEqual(
            [command.label for command in selected if command.label in WORKFLOW_EXACT_DUPLICATE_FAST_CHECKS],
            list(WORKFLOW_EXACT_DUPLICATE_FAST_CHECKS),
        )
        run = _policy_artifact_step(self.workflow)
        invocations = [line.strip() for line in run.splitlines() if line.strip().startswith("python")]
        for label in WORKFLOW_EXACT_DUPLICATE_FAST_CHECKS:
            workflow_form = label.replace("python3 ", "python ", 1)
            self.assertNotIn(workflow_form, invocations, workflow_form)
        self.assertEqual(invocations.count("python scripts/refresh-release-ledger-evidence.py --check"), 1)
        self.assertIn("python scripts/refresh-release-ledger-evidence.py --check", invocations)



if __name__ == "__main__":
    unittest.main()
