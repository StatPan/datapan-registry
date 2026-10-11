from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).parents[1].resolve()
SCRIPT = ROOT / "scripts" / "generate-completeness-proof-rollup.py"
PINNED_AS_OF = "2026-10-05T00:00:00Z"
PINNED_POLICY_COMMIT = "01148419bef5d7f212e92ab96d0ba1c3da6c298c"
PINNED_POLICY_PATH = "policy/source-refresh.json"
PINNED_POLICY_BYTES = 1127
PINNED_POLICY_SHA256 = "61dfb817f743085918c9f613fcd4a2dcf0d2f3de9df31b005cae1cd0250d5cfc"


class CompletenessProofRollupCliTest(unittest.TestCase):
    """Exercise the public CLI without touching checked-in or shared reports."""

    def setUp(self) -> None:
        self.output_tmp = tempfile.TemporaryDirectory(
            prefix=".completeness-rollup-cli-", dir=ROOT
        )
        self.addCleanup(self.output_tmp.cleanup)
        self.output_root = pathlib.Path(self.output_tmp.name)

        self.index_tmp = tempfile.TemporaryDirectory(prefix="completeness-rollup-index-")
        self.addCleanup(self.index_tmp.cleanup)
        self.index_root = pathlib.Path(self.index_tmp.name)

        self.guard_tmp = tempfile.TemporaryDirectory(prefix="completeness-rollup-guard-")
        self.addCleanup(self.guard_tmp.cleanup)
        self.guard_root = pathlib.Path(self.guard_tmp.name)
        self._install_offline_guard()

        self.original_index = json.loads(
            (ROOT / "reports/completeness-proof-inputs.json").read_text(encoding="utf-8")
        )

    def _install_offline_guard(self) -> None:
        guard_dir = self.guard_root / "guard"
        guard_dir.mkdir()
        (guard_dir / "sitecustomize.py").write_text(
            """
import http.client
import pathlib
import shlex
import socket
import subprocess
import urllib.request

def _deny_network(*args, **kwargs):
    raise RuntimeError("unexpected network or provider access during offline rollup CLI")

socket.create_connection = _deny_network
socket.socket.connect = _deny_network
socket.socket.connect_ex = _deny_network
http.client.HTTPConnection.connect = _deny_network
urllib.request.urlopen = _deny_network

_real_popen = subprocess.Popen
_read_only_git = {"show", "merge-base", "rev-parse"}

class _GuardedPopen(_real_popen):
    def __init__(self, args, *positional, **keywords):
        command = shlex.split(args) if isinstance(args, str) else list(args)
        if not command or pathlib.Path(command[0]).name != "git":
            raise RuntimeError("unexpected non-Git subprocess during offline rollup CLI")
        subcommand_index = 1
        if len(command) > 1 and command[1] == "--no-pager":
            subcommand_index = 2
        if len(command) <= subcommand_index or command[subcommand_index] not in _read_only_git:
            raise RuntimeError("unexpected Git mutation or unsupported command during offline rollup CLI")
        super().__init__(args, *positional, **keywords)

subprocess.Popen = _GuardedPopen
""",
            encoding="utf-8",
        )

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        old_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            str(self.guard_root / "guard")
            if not old_pythonpath
            else os.pathsep.join((str(self.guard_root / "guard"), old_pythonpath))
        )
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _write_index(self, name: str, value: dict) -> pathlib.Path:
        path = self.index_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _output_pair(self, name: str) -> tuple[str, str, pathlib.Path, pathlib.Path]:
        directory = self.output_root / name
        json_path = directory / "rollup.json"
        markdown_path = directory / "rollup.md"
        return (
            json_path.relative_to(ROOT).as_posix(),
            markdown_path.relative_to(ROOT).as_posix(),
            json_path,
            markdown_path,
        )

    def _run_cli(
        self,
        *,
        action: str,
        index_path: pathlib.Path,
        outputs: tuple[str, str, pathlib.Path, pathlib.Path],
        extra_args: tuple[str, ...] = (),
        repo_root_arg: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        json_arg, markdown_arg, _json_path, _markdown_path = outputs
        command = [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            repo_root_arg or str(ROOT),
            "--input-root",
            str(ROOT),
            "--input-index",
            str(index_path),
            "--as-of",
            PINNED_AS_OF,
            "--output-json",
            json_arg,
            "--output-markdown",
            markdown_arg,
            action,
            *extra_args,
        ]
        environment = self._env()
        environment.update(env_overrides or {})
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _valid_index(self, name: str = "inputs.json") -> pathlib.Path:
        return self._write_index(name, self.original_index)

    def _run_guarded_python(self, code: str, *args: str, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-c", code, *args],
            cwd=cwd or ROOT,
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_write_check_and_markdown_render_the_same_scope_claims(self) -> None:
        index_path = self._valid_index()
        outputs = self._output_pair("write-check")
        json_arg, markdown_arg, json_path, markdown_path = outputs

        written = self._run_cli(action="--write", index_path=index_path, outputs=outputs)
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        self.assertIn("ok completeness proof rollup", written.stdout)

        json_before = json_path.read_bytes()
        markdown_before = markdown_path.read_bytes()
        json_mtime = json_path.stat().st_mtime_ns
        markdown_mtime = markdown_path.stat().st_mtime_ns
        report = json.loads(json_before)
        scope_rows = {row["scope_id"]: row for row in report["scopes"]}
        self.assertEqual(len(scope_rows), report["summary"]["registered_scopes"])

        markdown_rows: dict[str, list[str]] = {}
        for line in markdown_before.decode("utf-8").splitlines():
            if not line.startswith("| ") or line.startswith("| Scope |") or line.startswith("| ---"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            self.assertEqual(len(cells), 8, line)
            markdown_rows[cells[0]] = cells
        self.assertEqual(set(markdown_rows), set(scope_rows))
        for scope_id, row in scope_rows.items():
            cells = markdown_rows[scope_id]
            self.assertEqual(cells[1], row["resource_kind"])
            self.assertEqual(cells[3], row["proof_state"])
            for column, claim in zip(cells[4:7], ("complete", "current", "updated")):
                self.assertEqual(column, str(row["evaluated_claims"][claim]))

        checked = self._run_cli(action="--check", index_path=index_path, outputs=outputs)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("ok completeness proof rollup check", checked.stdout)
        self.assertEqual(json_path.read_bytes(), json_before)
        self.assertEqual(markdown_path.read_bytes(), markdown_before)
        self.assertEqual(json_path.stat().st_mtime_ns, json_mtime)
        self.assertEqual(markdown_path.stat().st_mtime_ns, markdown_mtime)

        # A false claim is an actionable CLI failure and must not replace either report.
        unsupported = next(
            f"{scope_id}:updated"
            for scope_id, row in scope_rows.items()
            if row["evaluated_claims"]["updated"] is False
        )
        rejected = self._run_cli(
            action="--write",
            index_path=index_path,
            outputs=outputs,
            extra_args=("--require-claim", unsupported),
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("--require-claim requires --evaluate-current-release", rejected.stderr)
        self.assertNotIn(unsupported, rejected.stderr)
        self.assertEqual(json_path.read_bytes(), json_before)
        self.assertEqual(markdown_path.read_bytes(), markdown_before)

    def test_same_input_epoch_is_portable_across_external_index_and_nested_output_paths(self) -> None:
        first_index = self._write_index("a/index.json", self.original_index)
        second_index = self._write_index("nested/b/index.json", self.original_index)
        first_outputs = self._output_pair("portable/first")
        second_outputs = self._output_pair("different/nested/second")

        first = self._run_cli(
            action="--write",
            index_path=first_index,
            outputs=first_outputs,
            repo_root_arg=".",
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self._run_cli(
            action="--write",
            index_path=second_index,
            outputs=second_outputs,
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

        self.assertEqual(first_outputs[2].read_bytes(), second_outputs[2].read_bytes())
        self.assertEqual(first_outputs[3].read_bytes(), second_outputs[3].read_bytes())

    def test_invalid_hash_duplicate_stage_and_scope_reference_preserve_both_reports(self) -> None:
        valid_index = self._valid_index("baseline.json")
        outputs = self._output_pair("fail-closed")
        baseline = self._run_cli(action="--write", index_path=valid_index, outputs=outputs)
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
        json_before = outputs[2].read_bytes()
        markdown_before = outputs[3].read_bytes()

        source_profile = next(
            row for row in self.original_index["inputs"]
            if row["scope_id"] == "data-go-kr.api-catalog" and row["role"] == "source_profile"
        )
        bad_hash = copy.deepcopy(self.original_index)
        item = next(row for row in bad_hash["inputs"] if row["input_id"] == source_profile["input_id"])
        item["sha256"] = "0" * 64
        bad_hash["inputs"].remove(item)
        bad_hash["inputs"].insert(0, item)

        pipeline_run = next(
            row for row in self.original_index["inputs"]
            if row["role"] == "pipeline_run" and row.get("subject", {}).get("stage") == "source"
        )
        duplicate_stage = copy.deepcopy(self.original_index)
        original_run = next(
            row for row in duplicate_stage["inputs"] if row["input_id"] == pipeline_run["input_id"]
        )
        duplicate_run = copy.deepcopy(original_run)
        duplicate_run["input_id"] += ".duplicate"
        duplicate_stage["inputs"].remove(original_run)
        duplicate_stage["inputs"][0:0] = [original_run, duplicate_run]

        wrong_scope = copy.deepcopy(self.original_index)
        mismatched_ref = next(
            row for row in wrong_scope["inputs"] if row["input_id"] == source_profile["input_id"]
        )
        mismatched_ref["scope_id"] = "unregistered.scope"
        wrong_scope["inputs"].remove(mismatched_ref)
        wrong_scope["inputs"].insert(0, mismatched_ref)

        cases = (
            ("bad-hash.json", bad_hash, "byte count or SHA-256 differs"),
            ("duplicate-stage.json", duplicate_stage, "duplicate evidence role pipeline_run"),
            ("wrong-scope-ref.json", wrong_scope, "refers to unregistered scope"),
        )
        for filename, value, expected_error in cases:
            with self.subTest(case=filename):
                invalid_index = self._write_index(filename, value)
                result = self._run_cli(
                    action="--write", index_path=invalid_index, outputs=outputs
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(expected_error, result.stderr)
                self.assertEqual(outputs[2].read_bytes(), json_before)
                self.assertEqual(outputs[3].read_bytes(), markdown_before)

    def test_current_target_collection_requires_checked_current_evaluation(self) -> None:
        index_path = self._valid_index()
        outputs = self._output_pair("current-target-mode-guard")
        result = self._run_cli(
            action="--check",
            index_path=index_path,
            outputs=outputs,
            extra_args=("--collect-current-target-evidence",),
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("--collect-current-target-evidence requires --evaluate-current-release", result.stderr)
        self.assertFalse(outputs[2].exists())
        self.assertFalse(outputs[3].exists())

        direct_collection = self._run_cli(
            action="--check",
            index_path=index_path,
            outputs=outputs,
            extra_args=(
                "--evaluate-current-release",
                "--collect-current-target-evidence",
                "--release-source-sha", "a" * 40,
                "--evaluation-as-of", PINNED_AS_OF,
                "--evaluation-output", ".datapan/ci/direct-current-target.json",
            ),
            env_overrides={"DATAPAN_CURRENT_TARGET_GUARDED": "true"},
        )
        self.assertEqual(direct_collection.returncode, 1, direct_collection.stdout + direct_collection.stderr)
        self.assertIn("current-target collection requires the dedicated launcher", direct_collection.stderr)
        self.assertFalse(outputs[2].exists())
        self.assertFalse(outputs[3].exists())

        evaluated_write = self._run_cli(
            action="--write",
            index_path=index_path,
            outputs=outputs,
            extra_args=(
                "--evaluate-current-release",
                "--collect-current-target-evidence",
                "--release-source-sha", "a" * 40,
                "--evaluation-as-of", PINNED_AS_OF,
                "--evaluation-output", ".datapan/ci/current-target-mode-guard.json",
            ),
        )
        self.assertEqual(evaluated_write.returncode, 1, evaluated_write.stdout + evaluated_write.stderr)
        self.assertIn("--evaluate-current-release must be combined with --check", evaluated_write.stderr)
        self.assertFalse(outputs[2].exists())
        self.assertFalse(outputs[3].exists())

    def test_offline_git_guard_accepts_only_single_no_pager_prefix_for_pinned_policy(self) -> None:
        child = (
            "import subprocess,sys\n"
            "process = subprocess.Popen(sys.argv[2:], cwd=sys.argv[1], "
            "stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
            "stdout, stderr = process.communicate()\n"
            "sys.stdout.buffer.write(stdout)\n"
            "sys.stderr.buffer.write(stderr)\n"
            "raise SystemExit(process.returncode)\n"
        )
        revision_path = f"{PINNED_POLICY_COMMIT}:{PINNED_POLICY_PATH}"
        for prefix in ((), ("--no-pager",)):
            with self.subTest(prefix=prefix):
                result = self._run_guarded_python(
                    child,
                    str(ROOT),
                    "git",
                    *prefix,
                    "show",
                    revision_path,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
                self.assertEqual(len(result.stdout), PINNED_POLICY_BYTES)
                self.assertEqual(hashlib.sha256(result.stdout).hexdigest(), PINNED_POLICY_SHA256)
                self.assertEqual(result.stderr, b"")

    def test_offline_git_guard_rejects_other_global_options_mutations_and_non_git(self) -> None:
        absent_git_dir = str(self.guard_root / "absent-git-dir")
        absent_work_tree = str(self.guard_root / "absent-work-tree")
        cases = [
            ["git"],
            ["git", "--no-pager"],
            ["git", "--no-pager", "--no-pager", "show", "HEAD:manifest.json"],
            ["git", "--unknown-global-option", "show", "HEAD:manifest.json"],
            ["git", "--paginate", "show", "HEAD:manifest.json"],
            ["git", "-c", "core.pager=cat", "show", "HEAD:manifest.json"],
            ["git", "-C", str(self.guard_root), "show", "HEAD:manifest.json"],
            ["git", "--git-dir", absent_git_dir, "show", "HEAD:manifest.json"],
            ["git", "--work-tree", absent_work_tree, "show", "HEAD:manifest.json"],
            ["git", "fetch", "origin"],
            ["git", "--no-pager", "fetch", "origin"],
            ["git", "push", "origin", "main"],
            ["git", "--no-pager", "push", "origin", "main"],
            [sys.executable, "-c", "pass"],
        ]
        child = (
            "import json,subprocess,sys\n"
            "commands=json.loads(sys.argv[2])\n"
            "outcomes=[]\n"
            "for command in commands:\n"
            " try:\n"
            "  process=subprocess.Popen(command,cwd=sys.argv[1],"
            "stdout=subprocess.PIPE,stderr=subprocess.PIPE)\n"
            " except RuntimeError as exc:\n"
            "  outcomes.append({'status':'denied','message':str(exc)})\n"
            " else:\n"
            "  try:\n"
            "   process.communicate(timeout=3)\n"
            "  except BaseException:\n"
            "   process.kill(); process.communicate()\n"
            "  outcomes.append({'status':'launched','returncode':process.returncode})\n"
            "print(json.dumps(outcomes,separators=(',',':')))\n"
        )
        result = self._run_guarded_python(
            child,
            str(self.guard_root),
            json.dumps(cases),
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        outcomes = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(len(outcomes), len(cases))
        self.assertTrue(all(row.get("status") == "denied" for row in outcomes), outcomes)
        self.assertEqual(result.stderr, b"")

    def test_verify_workflow_scopes_read_only_actions_token_to_collection_step(self) -> None:
        workflow = (ROOT / ".github/workflows/verify-release.yml").read_text(encoding="utf-8")
        verify_job = workflow.split("  verify:\n", 1)[1]
        self.assertIn("    permissions:\n      contents: read\n      actions: read", verify_job)
        collection_step = verify_job.split("      - name: Evaluate current release target applicability\n", 1)[1]
        collection_step = collection_step.split("\n      - name:", 1)[0]
        self.assertIn("          GH_TOKEN: ${{ github.token }}", collection_step)
        self.assertIn("--collect-current-target-evidence", collection_step)
        self.assertIn("        timeout-minutes: 3", collection_step)
        self.assertIn("timeout --signal=TERM --kill-after=15s 120s", collection_step)
        self.assertIn("python scripts/current_target_cli_guardian.py", collection_step)
        self.assertNotIn("python scripts/generate-completeness-proof-rollup.py", collection_step)
        self.assertNotIn("--foreground", collection_step)
        self.assertNotIn("actions: write", workflow)

    def test_outer_timeout_contains_startup_stall_and_leaves_no_evaluation_output(self) -> None:
        if not sys.platform.startswith("linux") or not pathlib.Path("/proc/self/stat").exists():
            self.skipTest("the Verify runner uses Linux process-group semantics")
        timeout = shutil.which("timeout")
        self.assertIsNotNone(timeout, "GNU coreutils timeout is required by the Verify workflow")
        version = subprocess.run([timeout, "--version"], capture_output=True, text=True, check=False)
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn("GNU coreutils", version.stdout)

        with tempfile.TemporaryDirectory(prefix="current-target-outer-timeout-") as raw:
            temporary = pathlib.Path(raw)
            parent_pid_file = temporary / "parent.pid"
            child_pid_file = temporary / "child.pid"
            (temporary / "sitecustomize.py").write_text(
                "import pathlib, signal, subprocess, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"child_pid_file = pathlib.Path({str(child_pid_file)!r})\n"
                "child_code = (\n"
                "    'import os,pathlib,signal,sys,time\\n'\n"
                "    'signal.signal(signal.SIGTERM,signal.SIG_IGN)\\n'\n"
                "    'pathlib.Path(sys.argv[1]).write_text(str(os.getpid()),encoding=\"ascii\")\\n'\n"
                "    'while True: time.sleep(0.05)\\n'\n"
                ")\n"
                "child = subprocess.Popen([sys.executable, '-S', '-c', child_code, str(child_pid_file)])\n"
                f"pathlib.Path({str(parent_pid_file)!r}).write_text(str(__import__('os').getpid()), encoding='ascii')\n"
                "while True: time.sleep(0.05)\n",
                encoding="utf-8",
            )
            output = temporary / ".datapan/ci/current-release.json"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(temporary)
            env.pop("GH_TOKEN", None)
            command = [
                timeout, "--signal=TERM", "--kill-after=1s", "1s",
                sys.executable, str(SCRIPT),
                "--repo-root", str(temporary), "--check",
                "--evaluate-current-release", "--collect-current-target-evidence",
                "--release-source-sha", "a" * 40,
                "--evaluation-as-of", PINNED_AS_OF,
                "--evaluation-output", ".datapan/ci/current-release.json",
            ]
            def process_state(pid: int) -> str | None:
                try:
                    raw_stat = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
                except OSError:
                    return None
                return raw_stat.rsplit(")", 1)[1].strip().split()[0]

            try:
                result = subprocess.run(
                    command, cwd=ROOT, env=env, capture_output=True, text=True,
                    check=False, timeout=5,
                )
                # Coreutils 9 may terminate its own timeout process with the command
                # group; the workflow shell reports that as 137, while subprocess
                # reports the raw signal as -SIGKILL.
                self.assertIn(result.returncode, {124, 137, -signal.SIGKILL}, result.stdout + result.stderr)
                self.assertNotIn("ok completeness proof rollup", result.stdout)
                self.assertFalse(output.exists())
                self.assertTrue(parent_pid_file.is_file())
                self.assertTrue(child_pid_file.is_file())
                owned_pids = [int(parent_pid_file.read_text()), int(child_pid_file.read_text())]
                cleanup_deadline = time.monotonic() + 3.0
                while time.monotonic() < cleanup_deadline:
                    if all(process_state(pid) in {None, "Z"} for pid in owned_pids):
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    all(process_state(pid) in {None, "Z"} for pid in owned_pids),
                    "the timeout process group left a live CLI descendant",
                )
            finally:
                for pid_file in (parent_pid_file, child_pid_file):
                    if not pid_file.is_file():
                        continue
                    try:
                        pid = int(pid_file.read_text(encoding="ascii"))
                    except (OSError, ValueError):
                        continue
                    if process_state(pid) not in {None, "Z"}:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
