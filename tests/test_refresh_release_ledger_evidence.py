from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import unittest
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts/refresh-release-ledger-evidence.py"
SPEC = importlib.util.spec_from_file_location("refresh_release_ledger_evidence", MODULE_PATH)
assert SPEC and SPEC.loader
refresh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = refresh
SPEC.loader.exec_module(refresh)


class RefreshSelectorTests(unittest.TestCase):
    def test_default_command_inventory_is_the_golden_inventory(self) -> None:
        # Keep the inventory anchored to the checked-in command tuple: the selector
        # must not alter ordering or silently select a different default set.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(refresh.selected_check_commands(), refresh.CHECK_COMMANDS)
        self.assertEqual(refresh.CHECK_COMMANDS[0].label, "python3 scripts/sync-release-schema-artifacts.py --check")
        self.assertEqual(refresh.CHECK_COMMANDS[-1].label, "python3 scripts/generate-release-assembly-receipt.py --check")

    def test_opt_out_removes_only_completeness_regression_and_preserves_order(self) -> None:
        with mock.patch.dict(os.environ, {refresh.COMPLETENESS_REGRESSION_SKIP_ENV: "1"}, clear=True):
            selected = refresh.selected_check_commands()
        self.assertEqual(
            [command for command in refresh.CHECK_COMMANDS if command not in selected],
            [refresh.COMPLETENESS_REGRESSION_COMMAND],
        )
        self.assertEqual(selected, tuple(command for command in refresh.CHECK_COMMANDS if command != refresh.COMPLETENESS_REGRESSION_COMMAND))

    def test_only_literal_one_opts_out(self) -> None:
        for value in ("0", "", "true", "TRUE", "yes"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {refresh.COMPLETENESS_REGRESSION_SKIP_ENV: value}, clear=True):
                self.assertEqual(refresh.selected_check_commands(), refresh.CHECK_COMMANDS)

    def test_default_regression_failure_is_check_failure(self) -> None:
        calls = []

        def run(argv, check=False):
            calls.append(tuple(argv))
            return type("Result", (), {"returncode": 7 if tuple(argv) == refresh.COMPLETENESS_REGRESSION_COMMAND.argv else 0})()

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(refresh.subprocess, "run", side_effect=run):
            self.assertFalse(refresh.run_check_commands(refresh.selected_check_commands()))
        self.assertIn(refresh.COMPLETENESS_REGRESSION_COMMAND.argv, calls)

    def test_opt_out_still_propagates_other_failure_and_nonconvergence(self) -> None:
        other = refresh.CHECK_COMMANDS[0]
        with mock.patch.dict(os.environ, {refresh.COMPLETENESS_REGRESSION_SKIP_ENV: "1"}, clear=True), mock.patch.object(
            refresh.subprocess, "run", return_value=type("Result", (), {"returncode": 9})()
        ):
            self.assertFalse(refresh.run_check_commands(refresh.selected_check_commands()))
        with self.assertRaises(refresh.ConvergenceError):
            refresh.converge(max_iterations=2, refresh_once=lambda _iteration: True, checks_pass=lambda _iteration: False)
        self.assertNotEqual(other, refresh.COMPLETENESS_REGRESSION_COMMAND)


if __name__ == "__main__":
    unittest.main()
