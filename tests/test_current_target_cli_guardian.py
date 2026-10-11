from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from scripts import current_target_cli_guardian as guardian


ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARDIAN = ROOT / "scripts/current_target_cli_guardian.py"


class CurrentTargetCliGuardianTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="current-target-guardian-test-")
        self.root = pathlib.Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.control = self.root / "control"
        self.scratch = self.root / "scratch"
        self.ci = self.root / "ci"
        for path in (self.control, self.scratch, self.ci):
            path.mkdir(mode=0o700)
        self.worker = self.control / "worker.py"
        self.harness = self.control / "harness.py"
        self.stage_dir = self.scratch / "stage"
        self.stage_dir.mkdir(mode=0o700)
        self.output = self.ci / "current-evaluation.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, path: pathlib.Path, source: str, *, mode: int = 0o700) -> pathlib.Path:
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        os.chmod(path, mode)
        return path

    def _write_harness(
        self, *, fail_discovery: bool = False, delay_final_read: bool = False,
    ) -> None:
        self._write(
            self.harness,
            f"""
            import json, os, pathlib, shutil, sys, time
            sys.path.insert(0, {str(ROOT)!r})
            from scripts import current_target_cli_guardian as guardian
            worker = pathlib.Path(sys.argv[1])
            stage_dir = pathlib.Path(sys.argv[2])
            output = pathlib.Path(sys.argv[3])
            seconds = float(sys.argv[4])
            deadline = time.monotonic() + seconds
            environment = os.environ.copy()
            environment['TEST_GUARDIAN_DEADLINE'] = repr(deadline)
            if {fail_discovery!r}:
                original = guardian._discover_owned
                calls = [0]
                def failed_discovery():
                    calls[0] += 1
                    if calls[0] >= 1:
                        raise guardian.GuardianError('injected procfs failure after launch')
                    return original()
                guardian._discover_owned = failed_discovery
            result = guardian.run_owned_attempt(
                (sys.executable, '-I', '-B', str(worker), str(stage_dir / 'evaluation.json')),
                cwd=worker.parent, environment=environment, deadline_monotonic=deadline,
            )
            if {delay_final_read!r}:
                original_read_text = pathlib.Path.read_text
                def delayed_read_text(path, *args, **kwargs):
                    if path == stage_dir / 'evaluation.json':
                        time.sleep(1.1)
                    return original_read_text(path, *args, **kwargs)
                pathlib.Path.read_text = delayed_read_text
            promoted = False
            promotion_rejected = False
            if result.state == 'worker_exited' and result.containment_established and result.returncode == 0:
                try:
                    guardian._promote_result(
                        stage_dir, output, deadline_monotonic=deadline,
                    )
                    promoted = True
                except guardian.GuardianError:
                    promotion_rejected = True
            elif result.containment_established:
                shutil.rmtree(stage_dir, ignore_errors=True)
            print(json.dumps({{
                'state': result.state,
                'returncode': result.returncode,
                'containment_established': result.containment_established,
                'elapsed_seconds': result.elapsed_seconds,
                'stdout': result.stdout.decode('utf-8', errors='replace'),
                'stderr': result.stderr.decode('utf-8', errors='replace'),
                'promoted': promoted,
                'promotion_rejected': promotion_rejected,
                'output_exists': output.exists(),
                'stage_exists': stage_dir.exists(),
            }}, sort_keys=True))
            """,
        )

    def _run_harness(
        self, seconds: float, *, fail_discovery: bool = False,
        delay_final_read: bool = False,
    ) -> dict[str, object]:
        self._write_harness(fail_discovery=fail_discovery, delay_final_read=delay_final_read)
        environment = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        result = subprocess.run(
            [
                sys.executable, "-I", "-B", str(self.harness), str(self.worker),
                str(self.stage_dir), str(self.output), str(seconds),
            ],
            cwd=self.control,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=25,
            start_new_session=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, result.stderr)
        return json.loads(lines[-1])

    def test_launcher_rejects_caller_selected_input_and_output_paths(self) -> None:
        valid = [
            "--check",
            "--evaluate-current-release",
            "--collect-current-target-evidence",
            "--release-source-sha", "a" * 40,
            "--evaluation-as-of", "2026-10-07T00:00:00Z",
            "--evaluation-output", ".datapan/ci/completeness-current-release.json",
        ]
        self.assertEqual(guardian._validate_launcher_arguments(valid), valid)
        for extra in (
            ("--input-index", "/tmp/alternate-index.json"),
            ("--repo-root", "/tmp/other-repository"),
            ("--evaluation-output", ".datapan/ci/other-current-target.json"),
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(guardian.GuardianError):
                    guardian._validate_launcher_arguments([*valid, *extra])

    def test_contained_success_promotes_only_valid_current_evaluation(self) -> None:
        stage_path = self.stage_dir / "evaluation.json"
        self._write(
            self.worker,
            f"""
            import importlib.util, json, pathlib, sys, time
            rollup_path = pathlib.Path({str(ROOT / 'scripts/generate-completeness-proof-rollup.py')!r})
            spec = importlib.util.spec_from_file_location('current_target_rollup_test', rollup_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            deadline = module.current_target_guardian_deadline()
            assert deadline > time.monotonic()
            pathlib.Path(sys.argv[1]).write_text(json.dumps({{
                'schema_version': 'datapan.completeness-current-release-evaluation.v1',
                'claims': {{'updated': False}},
            }}), encoding='utf-8')
            print('contained evaluation complete')
            """,
        )
        result = self._run_harness(5.0)
        self.assertEqual(result["state"], "worker_exited")
        self.assertTrue(result["containment_established"])
        self.assertTrue(result["promoted"])
        self.assertTrue(self.output.is_file())
        self.assertFalse(self.stage_dir.exists())
        self.assertEqual(json.loads(self.output.read_text())["schema_version"], "datapan.completeness-current-release-evaluation.v1")
        self.assertIn("contained evaluation complete", result["stdout"])

    def test_deadline_expiring_during_real_final_read_prevents_promotion(self) -> None:
        self._write(
            self.worker,
            f"""
            import json, pathlib, sys
            pathlib.Path(sys.argv[1]).write_text(json.dumps({{
                'schema_version': 'datapan.completeness-current-release-evaluation.v1',
                'claims': {{'updated': False}},
            }}), encoding='utf-8')
            """,
        )
        result = self._run_harness(1.0, delay_final_read=True)
        self.assertEqual(result["state"], "worker_exited")
        self.assertTrue(result["containment_established"])
        self.assertTrue(result["promotion_rejected"])
        self.assertFalse(result["promoted"])
        self.assertFalse(result["output_exists"])
        self.assertTrue(result["stage_exists"])

    def test_main_does_not_promote_when_final_capture_flush_crosses_deadline(self) -> None:
        self._write(
            self.worker,
            """
            import json, pathlib, sys
            pathlib.Path(sys.argv[1]).write_text(json.dumps({
                'schema_version': 'datapan.completeness-current-release-evaluation.v1',
                'claims': {'updated': False},
            }), encoding='utf-8')
            print('contained evaluation complete')
            """,
        )
        flush_entered = self.scratch / "final-flush-entered"
        harness = self.control / "main-promotion-harness.py"
        self._write(
            harness,
            f"""
            import json, pathlib, sys, time
            sys.path.insert(0, {str(ROOT)!r})
            from scripts import current_target_cli_guardian as guardian

            root = pathlib.Path.cwd()
            worker = pathlib.Path({str(self.worker)!r})
            flush_entered = pathlib.Path({str(flush_entered)!r})
            guardian.EXECUTION_SECONDS = 4.0
            original_runner = guardian.run_owned_attempt

            def run_fixture_worker(command, *, cwd, environment, deadline_monotonic):
                output_index = command.index('--evaluation-output') + 1
                fixture_command = (
                    sys.executable, '-I', '-B', str(worker),
                    str(pathlib.Path(cwd) / command[output_index]),
                )
                result = original_runner(
                    fixture_command, cwd=cwd, environment=environment,
                    deadline_monotonic=deadline_monotonic,
                )
                remaining = deadline_monotonic - time.monotonic()
                if remaining > 0.25:
                    time.sleep(remaining - 0.25)
                return result

            guardian.run_owned_attempt = run_fixture_worker
            original_stdout = sys.stdout

            class DelayedFlush:
                buffer = original_stdout.buffer

                def flush(self):
                    flush_entered.write_text('entered', encoding='ascii')
                    time.sleep(0.45)
                    original_stdout.flush()

            guardian.sys.stdout = DelayedFlush()
            args = [
                '--check', '--evaluate-current-release', '--collect-current-target-evidence',
                '--release-source-sha', 'a' * 40,
                '--evaluation-as-of', '2026-10-07T00:00:00Z',
                '--evaluation-output', guardian.FIXED_EVALUATION_OUTPUT,
            ]
            exit_code = guardian.main(args)
            guardian.sys.stdout = original_stdout
            output = root / guardian.FIXED_EVALUATION_OUTPUT
            stage_dirs = list(output.parent.glob('.current-target-*'))
            print(json.dumps({{
                'exit_code': exit_code,
                'flush_entered': flush_entered.is_file(),
                'output_exists': output.exists(),
                'stage_count': len(stage_dirs),
            }}, sort_keys=True))
            """,
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(harness)],
            cwd=self.root,
            env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=25,
            start_new_session=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, result.stderr)
        outcome = json.loads(lines[-1])
        self.assertEqual(outcome["exit_code"], 1, result.stderr)
        self.assertTrue(outcome["flush_entered"])
        self.assertFalse(outcome["output_exists"])
        self.assertEqual(outcome["stage_count"], 1)

    def test_collector_new_session_descendant_is_reaped_when_scan_stalls(self) -> None:
        child_identity = self.scratch / "nested-child.json"
        leader_identity = self.scratch / "collector-leader.json"
        worker_identity = self.scratch / "worker.json"
        stall_started = self.scratch / "stall-started"
        fifo_path = self.scratch / "blocked-filesystem-operation"
        os.mkfifo(fifo_path, 0o600)
        owned_root = self.scratch / "owned-evidence"
        owned_root.mkdir(mode=0o700)
        fake_command = self.control / "fake-gh"
        self._write(
            fake_command,
            f"#!{pathlib.Path(sys.executable).resolve()}\n" + textwrap.dedent(
                f"""
                import json, os, pathlib, signal, time
                child = os.fork()
                if child == 0:
                    os.setsid()
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    pathlib.Path({str(child_identity)!r}).write_text(json.dumps({{
                        'pid': os.getpid(), 'sid': os.getsid(0),
                    }}), encoding='utf-8')
                    while True:
                        time.sleep(0.05)
                pathlib.Path({str(leader_identity)!r}).write_text(json.dumps({{
                    'pid': os.getpid(), 'sid': os.getsid(0),
                }}), encoding='utf-8')
                while True:
                    time.sleep(0.05)
                """
            ),
        )
        self._write(
            self.worker,
            f"""
            import os, pathlib, sys, time
            sys.path.insert(0, {str(ROOT)!r})
            from scripts import canonical_current_target_evidence as collector
            pathlib.Path({str(worker_identity)!r}).write_text(
                str(os.getpid()), encoding='ascii'
            )
            owned_root = pathlib.Path({str(owned_root)!r})
            child_identity = pathlib.Path({str(child_identity)!r})
            stall_started = pathlib.Path({str(stall_started)!r})
            fifo_path = pathlib.Path({str(fifo_path)!r})
            original_scandir = collector.os.scandir
            def blocking_scandir(path):
                if pathlib.Path(path) == owned_root:
                    limit = time.monotonic() + 3
                    while not child_identity.is_file() and time.monotonic() < limit:
                        time.sleep(0.005)
                    if not child_identity.is_file():
                        raise RuntimeError('collector-owned nested-session child did not start')
                    stall_started.write_text('scandir-entered', encoding='ascii')
                    # This blocks in the filesystem after the collector has
                    # launched its start_new_session=True native reader.
                    with fifo_path.open('rb', buffering=0) as blocked:
                        blocked.read(1)
                return original_scandir(path)
            collector.os.scandir = blocking_scandir
            collector._bounded_process(
                (str(pathlib.Path({str(fake_command)!r})),),
                cwd=pathlib.Path({str(self.control)!r}),
                deadline_monotonic=float(os.environ['TEST_GUARDIAN_DEADLINE']),
                binary=True, max_stdout_bytes=4096, owned_root=owned_root,
            )
            """,
        )
        result = self._run_harness(2.0)
        self.assertEqual(result["state"], "deadline_uncertain")
        self.assertTrue(result["containment_established"])
        self.assertFalse(result["promoted"])
        self.assertFalse(result["output_exists"])
        self.assertTrue(stall_started.is_file())
        self.assertTrue(leader_identity.is_file())
        self.assertTrue(child_identity.is_file())
        worker_pid = int(worker_identity.read_text(encoding="ascii"))
        leader = json.loads(leader_identity.read_text(encoding="utf-8"))
        child = json.loads(child_identity.read_text(encoding="utf-8"))
        self.assertEqual(leader["sid"], leader["pid"])
        self.assertEqual(child["sid"], child["pid"])
        self.assertNotEqual(leader["sid"], child["sid"])

        def state(pid: int) -> str | None:
            try:
                raw = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            except OSError:
                return None
            return raw.rsplit(")", 1)[1].strip().split()[0]

        pids = (worker_pid, int(leader["pid"]), int(child["pid"]))
        wait_until = time.monotonic() + 2.0
        while time.monotonic() < wait_until and any(state(pid) not in {None, "Z"} for pid in pids):
            time.sleep(0.025)
        self.assertTrue(all(state(pid) in {None, "Z"} for pid in pids), pids)
        self.assertFalse(self.output.exists())

    def test_outer_term_cancels_and_reaps_worker_before_returning(self) -> None:
        worker_pid_path = self.scratch / "cancelled-worker.pid"
        self._write(
            self.worker,
            f"""
            import os, pathlib, signal, sys, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            pathlib.Path({str(worker_pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')
            while True:
                time.sleep(0.05)
            """,
        )
        self._write_harness()
        process = subprocess.Popen(
            [
                sys.executable, "-I", "-B", str(self.harness), str(self.worker),
                str(self.stage_dir), str(self.output), "10",
            ],
            cwd=self.control,
            env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            wait_until = time.monotonic() + 5.0
            while time.monotonic() < wait_until and not worker_pid_path.exists():
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            self.assertTrue(worker_pid_path.is_file(), "worker did not reach the owned launch boundary")
            os.kill(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            result = json.loads([line for line in stdout.splitlines() if line.strip()][-1])
            self.assertEqual(result["state"], "cancelled_uncertain")
            self.assertTrue(result["containment_established"])
            self.assertFalse(result["promoted"])
            self.assertFalse(result["output_exists"])
            self.assertFalse(result["stage_exists"])
            worker_pid = int(worker_pid_path.read_text(encoding="ascii"))
            wait_until = time.monotonic() + 2.0
            while time.monotonic() < wait_until:
                try:
                    state = pathlib.Path(f"/proc/{worker_pid}/stat").read_text(encoding="ascii")
                except OSError:
                    state = ""
                if not state or state.rsplit(")", 1)[1].strip().split()[0] == "Z":
                    break
                time.sleep(0.025)
            try:
                state = pathlib.Path(f"/proc/{worker_pid}/stat").read_text(encoding="ascii")
            except OSError:
                state = ""
            self.assertTrue(not state or state.rsplit(")", 1)[1].strip().split()[0] == "Z")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)

    def test_unknown_procfs_containment_keeps_stage_and_never_promotes(self) -> None:
        worker_pid_path = self.scratch / "unknown-worker.pid"
        self._write(
            self.worker,
            f"""
            import os, pathlib, sys, time
            pathlib.Path({str(worker_pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')
            while True:
                time.sleep(0.05)
            """,
        )
        result = self._run_harness(5.0, fail_discovery=True)
        self.assertEqual(result["state"], "containment_unconfirmed")
        self.assertFalse(result["containment_established"])
        self.assertFalse(result["promoted"])
        self.assertFalse(result["output_exists"])
        self.assertTrue(result["stage_exists"])
        if worker_pid_path.exists():
            pid = int(worker_pid_path.read_text(encoding="ascii"))
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    unittest.main()
