from __future__ import annotations

import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1].resolve()
SCRIPT = ROOT / "scripts" / "generate-completeness-proof-rollup.py"
PINNED_AS_OF = "2026-10-05T00:00:00Z"


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
        if len(command) < 2 or command[1] not in _read_only_git:
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
        return subprocess.run(
            command,
            cwd=ROOT,
            env=self._env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _valid_index(self, name: str = "inputs.json") -> pathlib.Path:
        return self._write_index(name, self.original_index)

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
                self.assertEqual(column, str(row["claims"][claim]))

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
            if row["claims"]["updated"] is False
        )
        rejected = self._run_cli(
            action="--write",
            index_path=index_path,
            outputs=outputs,
            extra_args=("--require-claim", unsupported),
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("required claim is not supported", rejected.stderr)
        self.assertIn(unsupported, rejected.stderr)
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


if __name__ == "__main__":
    unittest.main()
