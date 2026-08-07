#!/usr/bin/env python3
"""Focused tests for the source-release governance baseline."""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "validate_oss_governance", ROOT / "scripts/validate-oss-governance.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ValidateOssGovernanceTest(unittest.TestCase):
    def test_checked_in_governance_baseline(self) -> None:
        MODULE.validate()

    def test_export_ignored_governance_file_is_rejected(self) -> None:
        real_git_output = MODULE.git_output

        def fake_git_output(*args: str) -> str:
            if args[0] == "check-attr":
                return "LICENSE: export-ignore: set\n"
            return real_git_output(*args)

        with mock.patch.object(MODULE, "git_output", side_effect=fake_git_output):
            with self.assertRaisesRegex(ValueError, "must not be export-ignored"):
                MODULE.validate()

    def test_missing_private_reporting_policy_assertion_is_rejected(self) -> None:
        security_path = ROOT / "SECURITY.md"
        original = security_path.read_text(encoding="utf-8")
        required_assertions = (
            "GitHub private vulnerability reporting is enabled",
            "https://github.com/StatPan/datapan-registry/security/advisories/new",
        )

        for assertion in required_assertions:
            with self.subTest(assertion=assertion), tempfile.TemporaryDirectory() as temporary_directory:
                replacement = pathlib.Path(temporary_directory) / "SECURITY.md"
                replacement.write_text(original.replace(assertion, ""), encoding="utf-8")

                real_read_text = pathlib.Path.read_text

                def fake_read_text(path: pathlib.Path, *args: object, **kwargs: object) -> str:
                    if path == security_path:
                        return replacement.read_text(*args, **kwargs)
                    return real_read_text(path, *args, **kwargs)

                with mock.patch.object(pathlib.Path, "read_text", new=fake_read_text):
                    with self.assertRaisesRegex(ValueError, "SECURITY.md is missing required governance text"):
                        MODULE.validate()


if __name__ == "__main__":
    unittest.main()
