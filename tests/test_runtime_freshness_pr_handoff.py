from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "runtime-freshness-pr-handoff.py"
SPEC = importlib.util.spec_from_file_location("runtime_freshness_pr_handoff", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


REPO = "StatPan/datapan-registry"
RUN_ID = "36650152644"
PR = "650"


class RuntimeFreshnessPrHandoffTest(unittest.TestCase):
    def fake_gh(
        self,
        *,
        allow_auto_merge: bool = False,
        states: list[dict[str, object]] | None = None,
        error: str | None = None,
    ) -> tuple[mock.Mock, list[tuple[list[str], str | None]]]:
        calls: list[tuple[list[str], str | None]] = []
        state_values = list(states or [{"state": "open", "merged": False}])

        def run(args: list[str], *, input_text: str | None = None) -> str:
            calls.append((args, input_text))
            if error is not None:
                raise MODULE.HandoffError(error)
            if args[:2] == ["api", f"repos/{REPO}"]:
                return "true" if allow_auto_merge else "false"
            if args[:2] == ["api", f"repos/{REPO}/pulls/{PR}"]:
                value = state_values.pop(0) if len(state_values) > 1 else state_values[0]
                return json.dumps(value)
            if args[:2] == ["pr", "merge"]:
                return ""
            if args[:3] == ["api", "--method", "POST"]:
                return ""
            raise AssertionError(f"unexpected gh command: {args}")

        return mock.Mock(side_effect=run), calls

    def advance(self, gh: mock.Mock, **overrides: object) -> tuple[str, str]:
        values: dict[str, object] = {
            "repo": REPO,
            "run_id": RUN_ID,
            "pr": PR,
            "event": "runtime-freshness-import-attest",
            "timeout_seconds": 1,
            "poll_interval_seconds": 1,
            "clock": mock.Mock(side_effect=[0.0, 0.0]),
            "sleep": lambda _seconds: None,
        }
        values.update(overrides)
        with mock.patch.object(MODULE, "gh", gh):
            return MODULE.advance(**values)

    def test_disabled_auto_merge_leaves_visible_pending_handoff_without_dispatch(self) -> None:
        gh, calls = self.fake_gh(allow_auto_merge=False)
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "output.txt"
            summary = pathlib.Path(directory) / "summary.md"
            output.touch()
            summary.touch()
            stdout = io.StringIO()
            with mock.patch.object(MODULE, "gh", gh), mock.patch.dict(
                os.environ,
                {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)},
            ), contextlib.redirect_stdout(stdout):
                result = MODULE.main([
                    "--repo", REPO,
                    "--run-id", RUN_ID,
                    "--pr", PR,
                    "--resume-event", "runtime-freshness-import-attest",
                ])

            self.assertEqual(result, 0)
            status = json.loads(stdout.getvalue())
            self.assertEqual(status["status"], "pending")
            self.assertIn('"run_id": "36650152644"', status["resume_command"])
            self.assertIn('"pr_number": "650"', status["resume_command"])
            self.assertIn("status=pending", output.read_text(encoding="utf-8"))
            self.assertIn("delivery is still pending", summary.read_text(encoding="utf-8"))
            self.assertIn('"run_id": "36650152644"', summary.read_text(encoding="utf-8"))
            self.assertIn('"pr_number": "650"', summary.read_text(encoding="utf-8"))
            self.assertIn("runtime-freshness-import-attest", summary.read_text(encoding="utf-8"))
        commands = [args for args, _ in calls]
        self.assertFalse(any(args[:2] == ["pr", "merge"] for args in commands))
        self.assertFalse(any(args[:3] == ["api", "--method", "POST"] for args in commands))

    def test_merged_pr_skips_merge_request_and_dispatches_exact_next_event(self) -> None:
        gh, calls = self.fake_gh(states=[{"state": "closed", "merged": True}])
        status, _ = self.advance(gh, event="runtime-freshness-import-attestation-verify")
        self.assertEqual(status, "dispatched")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1][0], [
            "api", "--method", "POST", f"repos/{REPO}/dispatches", "--input", "-"
        ])
        self.assertEqual(json.loads(calls[-1][1] or "{}"), {
            "event_type": "runtime-freshness-import-attestation-verify",
            "client_payload": {"run_id": RUN_ID, "pr_number": PR},
        })

    def test_enabled_auto_merge_uses_only_auto_squash_and_waits_for_merge(self) -> None:
        gh, calls = self.fake_gh(
            allow_auto_merge=True,
            states=[
                {"state": "open", "merged": False},
                {"state": "closed", "merged": True},
            ],
        )
        status, _ = self.advance(gh)
        self.assertEqual(status, "dispatched")
        merge = [args for args, _ in calls if args[:2] == ["pr", "merge"]]
        self.assertEqual(merge, [["pr", "merge", PR, "--repo", REPO, "--auto", "--squash"]])
        self.assertEqual(sum(args[:3] == ["api", "--method", "POST"] for args, _ in calls), 1)

    def test_timeout_remains_pending_and_never_dispatches(self) -> None:
        gh, calls = self.fake_gh(allow_auto_merge=True)
        status, _ = self.advance(gh, timeout_seconds=0)
        self.assertEqual(status, "pending")
        self.assertTrue(any(args[:2] == ["pr", "merge"] for args, _ in calls))
        self.assertFalse(any(args[:3] == ["api", "--method", "POST"] for args, _ in calls))

    def test_closed_unmerged_pr_fails_without_dispatch(self) -> None:
        gh, calls = self.fake_gh(states=[{"state": "closed", "merged": False}])
        with self.assertRaisesRegex(MODULE.HandoffError, "closed without a merge"):
            self.advance(gh)
        self.assertFalse(any(args[:3] == ["api", "--method", "POST"] for args, _ in calls))

    def test_api_failure_fails_without_dispatch(self) -> None:
        gh, calls = self.fake_gh(error="API unavailable\npermission denied")
        with self.assertRaisesRegex(MODULE.HandoffError, "API unavailable"):
            self.advance(gh)
        self.assertFalse(any(args[:3] == ["api", "--method", "POST"] for args, _ in calls))

    def test_invalid_pr_state_shapes_fail_closed(self) -> None:
        for response in ("[]", "null", '"open"', '{"state":"open","merged":true}'):
            with self.subTest(response=response):
                with mock.patch.object(MODULE, "gh", return_value=response):
                    with self.assertRaises(MODULE.HandoffError):
                        MODULE.pull_request_state(REPO, PR)

    def test_poll_interval_must_be_positive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                MODULE.parse_args([
                    "--repo", REPO,
                    "--run-id", RUN_ID,
                    "--pr", PR,
                    "--resume-event", "runtime-freshness-import-attest",
                    "--poll-interval-seconds", "0",
                ])

    def test_multiline_api_failure_is_written_as_a_complete_single_line_outcome(self) -> None:
        gh, _ = self.fake_gh(error="API unavailable\npermission denied")
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "output.txt"
            summary = pathlib.Path(directory) / "summary.md"
            output.touch()
            summary.touch()
            with mock.patch.object(MODULE, "gh", gh), mock.patch.dict(
                os.environ,
                {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)},
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = MODULE.main([
                    "--repo", REPO,
                    "--run-id", RUN_ID,
                    "--pr", PR,
                    "--resume-event", "runtime-freshness-import-attest",
                ])
            self.assertEqual(result, 1)
            self.assertIn("permission denied", output.read_text(encoding="utf-8"))
            self.assertIn("API unavailable permission denied", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
