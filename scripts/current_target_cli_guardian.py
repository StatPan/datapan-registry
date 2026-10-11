#!/usr/bin/env python3
"""Contain the current-target evaluator and its detached native-reader children.

This launcher is deliberately limited to the read-only completeness evaluator.
It starts a dedicated child before that child's Python imports, passes one
absolute monotonic deadline over a credentialed local socket, and uses Linux
subreaper plus PID/starttime/pidfd ownership to reap every descendant. It does
not perform native reads, decide current-target authority, or interpret output
from an uncontained attempt.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import math
import os
import pathlib
import re
import select
import selectors
import shutil
import signal
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Sequence


EXECUTION_SECONDS = 120.0
CLEANUP_SECONDS = 15.0
CANCEL_CLEANUP_SECONDS = 12.0
TERM_SECONDS = 2.0
POLL_SECONDS = 0.025
MAX_STDOUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 1024
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
HANDSHAKE_FD = 3
FIXED_EVALUATION_OUTPUT = ".datapan/ci/completeness-current-release.json"


class GuardianError(RuntimeError):
    """The dedicated process boundary or descendant ownership is unavailable."""


@dataclass(frozen=True)
class ProcIdentity:
    pid: int
    starttime: int
    pidfd: int


@dataclass(frozen=True)
class AttemptResult:
    state: str
    returncode: int | None
    stdout: bytes
    stderr: bytes
    containment_established: bool
    elapsed_seconds: float


def _subreaper_state() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(previous), 0, 0, 0) != 0:
        raise GuardianError("cannot query Linux child-subreaper state")
    return int(previous.value)


def _set_subreaper(value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, int(value), 0, 0, 0) != 0:
        raise GuardianError("cannot set Linux child-subreaper state")


def _children(pid: int) -> set[int]:
    try:
        tasks = tuple(pathlib.Path(f"/proc/{pid}/task").iterdir())
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise GuardianError("cannot inspect an owned process task table") from exc
    result: set[int] = set()
    for task in tasks:
        try:
            raw = (task / "children").read_text(encoding="ascii").strip()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GuardianError("cannot inspect an owned process child table") from exc
        try:
            result.update(int(value) for value in raw.split())
        except ValueError as exc:
            raise GuardianError("owned process child list is malformed") from exc
    return result


def _proc_identity(pid: int) -> tuple[int, int] | None:
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GuardianError("cannot inspect an owned process identity") from exc
    close = raw.rfind(")")
    if close < 0:
        raise GuardianError("owned process stat is malformed")
    fields = raw[close + 1 :].split()
    if len(fields) < 20:
        raise GuardianError("owned process stat is truncated")
    try:
        return int(fields[1]), int(fields[19])
    except ValueError as exc:
        raise GuardianError("owned process stat identity is malformed") from exc


def _assert_dedicated_process() -> None:
    if sys.platform != "linux":
        raise GuardianError("current-target guardian requires Linux")
    if threading.current_thread() is not threading.main_thread() or len(threading.enumerate()) != 1:
        raise GuardianError("current-target guardian requires one dedicated Python thread")
    try:
        tasks = tuple(pathlib.Path(f"/proc/{os.getpid()}/task").iterdir())
    except OSError as exc:
        raise GuardianError("cannot verify guardian OS thread count") from exc
    if len(tasks) != 1 or _children(os.getpid()):
        raise GuardianError("current-target guardian refuses unrelated threads or children")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise GuardianError("current-target guardian requires pidfd signaling")
    try:
        fd = os.pidfd_open(os.getpid(), 0)
    except OSError as exc:
        raise GuardianError("current-target guardian pidfd support is unavailable") from exc
    os.close(fd)


def _pin(pid: int, expected_parent: int) -> ProcIdentity | None:
    before = _proc_identity(pid)
    if before is None or before[0] != expected_parent:
        return None
    try:
        fd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return None
    except OSError as exc:
        raise GuardianError("cannot pin an owned process") from exc
    try:
        after = _proc_identity(pid)
    except BaseException:
        os.close(fd)
        raise
    if after is None or after[0] != expected_parent or after[1] != before[1]:
        os.close(fd)
        return None
    return ProcIdentity(pid, after[1], fd)


def _discover_owned() -> dict[tuple[int, int], ProcIdentity]:
    guardian = os.getpid()
    queue = [(guardian, pid) for pid in sorted(_children(guardian))]
    found: dict[tuple[int, int], ProcIdentity] = {}
    seen: set[int] = set()
    try:
        while queue:
            parent, pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            pinned = _pin(pid, parent)
            if pinned is None:
                continue
            key = (pinned.pid, pinned.starttime)
            found[key] = pinned
            for child in sorted(_children(pid)):
                queue.append((pid, child))
    except BaseException:
        _close_owned(found)
        raise
    return found


def _merge_owned(
    known: dict[tuple[int, int], ProcIdentity],
    fresh: Mapping[tuple[int, int], ProcIdentity],
) -> None:
    for key, identity in fresh.items():
        previous = known.get(key)
        if previous is None:
            known[key] = identity
        else:
            os.close(identity.pidfd)


def _pidfd_ready(fd: int) -> bool:
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    return bool(poller.poll(0))


def _signal_owned(known: Mapping[tuple[int, int], ProcIdentity], sig: int) -> None:
    for identity in tuple(known.values()):
        if _pidfd_ready(identity.pidfd):
            continue
        try:
            signal.pidfd_send_signal(identity.pidfd, sig, None, 0)
        except ProcessLookupError:
            pass
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise GuardianError("pidfd signaling failed for an owned process") from exc


def _reap(root_pid: int) -> int | None:
    root_code: int | None = None
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        except InterruptedError:
            continue
        if pid == 0:
            break
        if pid == root_pid:
            root_code = os.waitstatus_to_exitcode(status)
    return root_code


def _live_owned(known: Mapping[tuple[int, int], ProcIdentity]) -> bool:
    return any(not _pidfd_ready(identity.pidfd) for identity in known.values())


def _close_owned(known: Mapping[tuple[int, int], ProcIdentity]) -> None:
    for identity in known.values():
        try:
            os.close(identity.pidfd)
        except OSError:
            pass


def _set_cloexec_on_open_fds() -> None:
    try:
        entries = tuple(pathlib.Path(f"/proc/{os.getpid()}/fd").iterdir())
    except OSError as exc:
        raise GuardianError("cannot isolate guardian file descriptors") from exc
    import fcntl

    for entry in entries:
        try:
            fd = int(entry.name)
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise GuardianError("cannot isolate guardian file descriptors") from exc


def _safe_stream(selector: selectors.BaseSelector, fd: int, chunks: bytearray, limit: int) -> bool:
    try:
        value = os.read(fd, min(65536, limit + 1 - len(chunks)))
    except BlockingIOError:
        return True
    except InterruptedError:
        return True
    except OSError as exc:
        if exc.errno == errno.EINTR:
            return True
        raise GuardianError("guardian output pipe read failed") from exc
    if not value:
        try:
            selector.unregister(fd)
        except (KeyError, OSError):
            pass
        os.close(fd)
        return True
    if len(chunks) + len(value) > limit:
        return False
    chunks.extend(value)
    return True


def _write_exec_error(fd: int, exc: BaseException) -> None:
    try:
        code = exc.errno if isinstance(exc, OSError) and isinstance(exc.errno, int) else errno.EFAULT
        os.write(fd, struct.pack("!I", int(code) & 0xFFFFFFFF))
    except BaseException:
        pass


def _child_exec(
    command: Sequence[str], environment: Mapping[str, str], cwd: pathlib.Path,
    stdout_write: int, stderr_write: int, handshake_fd: int, exec_error_write: int,
) -> None:
    try:
        os.setsid()
        os.dup2(stdout_write, 1, inheritable=True)
        os.dup2(stderr_write, 2, inheritable=True)
        os.dup2(handshake_fd, HANDSHAKE_FD, inheritable=True)
        null_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null_fd, 0, inheritable=True)
        os.chdir(cwd)
        os.umask(0o077)
        os.execve(command[0], list(command), dict(environment))
    except BaseException as exc:
        _write_exec_error(exec_error_write, exc)
        os._exit(127)


def _drain(
    selector: selectors.BaseSelector,
    captures: dict[int, tuple[bytearray, int]],
) -> bool:
    safe = True
    for key, _ in selector.select(0):
        fd = int(key.fd)
        if fd not in captures:
            continue
        chunks, limit = captures[fd]
        safe = _safe_stream(selector, fd, chunks, limit) and safe
        if fd not in {int(item.fd) for item in selector.get_map().values()}:
            captures.pop(fd, None)
    return safe


def _signal_handlers() -> tuple[dict[int, object], list[bool]]:
    if threading.current_thread() is not threading.main_thread():
        raise GuardianError("guardian cancellation signals require the main thread")
    previous: dict[int, object] = {}
    expected = {signal.SIGTERM: signal.SIG_DFL, signal.SIGINT: signal.default_int_handler}
    for signum, normal in expected.items():
        observed = signal.getsignal(signum)
        if observed not in (normal, signal.SIG_DFL):
            raise GuardianError("guardian refuses a custom cancellation handler")
        previous[signum] = observed
    cancelled = [False]

    def request_cancel(_signum: int, _frame: object) -> None:
        cancelled[0] = True

    try:
        for signum in expected:
            signal.signal(signum, request_cancel)
    except BaseException:
        _restore_signal_handlers(previous)
        raise
    return previous, cancelled


def _restore_signal_handlers(previous: Mapping[int, object]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except BaseException:
            pass


def run_owned_attempt(
    command: Sequence[str], *, cwd: pathlib.Path, environment: Mapping[str, str],
    deadline_monotonic: float,
) -> AttemptResult:
    """Run one fixed worker and own its complete descendant tree."""
    started = time.monotonic()
    if (
        not isinstance(deadline_monotonic, (int, float))
        or isinstance(deadline_monotonic, bool)
        or not math.isfinite(float(deadline_monotonic))
        or deadline_monotonic <= started
        or deadline_monotonic - started > EXECUTION_SECONDS
    ):
        raise GuardianError("current-target execution deadline is outside its fixed 120-second window")
    if not command or len(command) > 128 or any(not isinstance(arg, str) or "\x00" in arg for arg in command):
        raise GuardianError("current-target worker command is invalid")
    if not pathlib.Path(command[0]).is_absolute():
        raise GuardianError("current-target worker executable must be absolute")
    _assert_dedicated_process()
    previous_subreaper = _subreaper_state()
    _set_subreaper(1)
    previous_signals: dict[int, object] = {}
    cancelled = [False]
    selector: selectors.BaseSelector | None = None
    known: dict[tuple[int, int], ProcIdentity] = {}
    open_fds: set[int] = set()
    root_pid: int | None = None
    root_code: int | None = None
    stdout_chunks = bytearray()
    stderr_chunks = bytearray()
    captures: dict[int, tuple[bytearray, int]] = {}
    state = "worker_failed"
    containment = False
    failure = False
    exec_error_read: int | None = None
    started_at = time.monotonic()
    cleanup_deadline = float(deadline_monotonic) + CLEANUP_SECONDS

    def bounded_cleanup_deadline() -> float:
        # A workflow-level timeout sends TERM before its hard kill. Finish
        # containment with three seconds of outer-watchdog margin; the normal
        # execution-deadline path still gets the full <=15-second cleanup tail.
        allowance = CANCEL_CLEANUP_SECONDS if cancelled[0] else CLEANUP_SECONDS
        return min(cleanup_deadline, time.monotonic() + allowance)

    try:
        previous_signals, cancelled = _signal_handlers()
        _set_cloexec_on_open_fds()
        stdout_read, stdout_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        stderr_read, stderr_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        exec_read, exec_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        root_pid = os.fork()
        if root_pid == 0:
            parent_sock.close()
            _child_exec(
                command, environment, pathlib.Path(cwd), stdout_write, stderr_write,
                child_sock.fileno(), exec_write,
            )
            os._exit(127)

        child_sock.close()
        os.close(stdout_write)
        os.close(stderr_write)
        os.close(exec_write)
        os.set_blocking(stdout_read, False)
        os.set_blocking(stderr_read, False)
        os.set_blocking(exec_read, False)
        selector = selectors.DefaultSelector()
        selector.register(stdout_read, selectors.EVENT_READ)
        selector.register(stderr_read, selectors.EVENT_READ)
        selector.register(exec_read, selectors.EVENT_READ)
        open_fds.update({stdout_read, stderr_read, exec_read})
        captures = {
            stdout_read: (stdout_chunks, MAX_STDOUT_BYTES),
            stderr_read: (stderr_chunks, MAX_STDERR_BYTES),
        }
        root_identity = _pin(root_pid, os.getpid())
        if root_identity is not None:
            known[(root_identity.pid, root_identity.starttime)] = root_identity
        else:
            raise GuardianError("current-target worker identity could not be pinned")
        parent_identity = _proc_identity(os.getpid())
        if parent_identity is None:
            raise GuardianError("guardian process identity is unavailable")
        peer_pid, peer_uid, _ = struct.unpack(
            "3i", parent_sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        )
        if peer_pid != os.getpid() or peer_uid != os.getuid():
            raise GuardianError("guardian handshake peer identity is inconsistent")
        frame = json.dumps(
            {
                "kind": "datapan.current-target-launch.v1",
                "guardian_pid": os.getpid(),
                "guardian_starttime": parent_identity[1],
                "worker_pid": root_identity.pid,
                "worker_starttime": root_identity.starttime,
                "deadline_monotonic": float(deadline_monotonic),
            }, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
        if len(frame) > MAX_FRAME_BYTES:
            raise GuardianError("guardian startup frame exceeds its bound")
        parent_sock.sendall(frame)
        parent_sock.close()
        exec_confirmed = False

        while True:
            now = time.monotonic()
            if cancelled[0]:
                state = "cancelled_uncertain"
                failure = True
                break
            if now >= deadline_monotonic:
                state = "deadline_uncertain"
                failure = True
                break
            try:
                _merge_owned(known, _discover_owned())
                root_code_new = _reap(root_pid)
                if root_code_new is not None:
                    root_code = root_code_new
                for key, _events in selector.select(min(POLL_SECONDS, deadline_monotonic - now)):
                    fd = int(key.fd)
                    if fd == exec_read:
                        value = os.read(exec_read, 4)
                        if value:
                            state = "worker_exec_failed"
                            failure = True
                            break
                        selector.unregister(exec_read)
                        os.close(exec_read)
                        open_fds.discard(exec_read)
                        exec_error_read = None
                        exec_confirmed = True
                    elif not _safe_stream(
                        selector, fd, captures[fd][0], captures[fd][1],
                    ):
                        state = "worker_output_exceeded_bound"
                        failure = True
                        break
                    elif fd not in {int(item.fd) for item in selector.get_map().values()}:
                        captures.pop(fd, None)
                        open_fds.discard(fd)
                if failure:
                    break
                root_live = root_code is None
                descendant_live = any(
                    identity.pid != root_pid and not _pidfd_ready(identity.pidfd)
                    for identity in known.values()
                )
                if root_code is not None and descendant_live:
                    state = "descendant_outlived_worker_uncertain"
                    failure = True
                    break
                if root_code is not None and not descendant_live:
                    if not selector.get_map() and not _children(os.getpid()):
                        containment = True
                        state = "worker_exited" if root_code == 0 else "worker_failed"
                        failure = root_code != 0
                        break
                # If the worker exits without exec EOF or with inherited pipes,
                # keep monitoring to the fixed deadline and fail closed.
                if not root_live and not exec_confirmed:
                    state = "worker_exec_status_unavailable"
                selector.select(min(POLL_SECONDS, max(0.0, deadline_monotonic - time.monotonic())))
            except BaseException as exc:
                state = "guardian_observation_failed_uncertain"
                failure = True
                break

        if not containment and root_pid is not None:
            containment = _terminate_owned(
                root_pid, known, selector, captures, open_fds,
                term_deadline=min(bounded_cleanup_deadline(), time.monotonic() + TERM_SECONDS),
                cleanup_deadline=bounded_cleanup_deadline(),
            )
            code = _reap(root_pid)
            if code is not None:
                root_code = code
        if not containment:
            state = "containment_unconfirmed"
        if failure or not containment or root_code != 0:
            return AttemptResult(state, root_code, b"", b"", containment, time.monotonic() - started_at)
        return AttemptResult(
            "worker_exited", root_code, bytes(stdout_chunks), bytes(stderr_chunks), True,
            time.monotonic() - started_at,
        )
    except BaseException:
        if root_pid is not None and not containment:
            try:
                containment = _terminate_owned(
                    root_pid, known, selector, captures, open_fds,
                    term_deadline=min(bounded_cleanup_deadline(), time.monotonic() + TERM_SECONDS),
                    cleanup_deadline=bounded_cleanup_deadline(),
                )
                code = _reap(root_pid)
                if code is not None:
                    root_code = code
            except BaseException:
                containment = False
        raise
    finally:
        if selector is not None:
            try:
                selector.close()
            except BaseException:
                pass
        for fd in tuple(open_fds):
            try:
                os.close(fd)
            except OSError:
                pass
        if "parent_sock" in locals():
            try:
                parent_sock.close()
            except OSError:
                pass
        if "child_sock" in locals():
            try:
                child_sock.close()
            except OSError:
                pass
        _close_owned(known)
        _restore_signal_handlers(previous_signals)
        _set_subreaper(previous_subreaper)


def _terminate_owned(
    root_pid: int,
    known: dict[tuple[int, int], ProcIdentity],
    selector: selectors.BaseSelector | None,
    captures: dict[int, tuple[bytearray, int]],
    open_fds: set[int],
    *, term_deadline: float,
    cleanup_deadline: float,
) -> bool:
    scan_ok = True
    while time.monotonic() < term_deadline:
        try:
            _merge_owned(known, _discover_owned())
        except BaseException:
            scan_ok = False
        try:
            _signal_owned(known, signal.SIGTERM)
            _reap(root_pid)
            if selector is not None:
                _drain(selector, captures)
            if not _children(os.getpid()) and not _live_owned(known):
                return scan_ok
        except BaseException:
            scan_ok = False
        time.sleep(min(POLL_SECONDS, max(0.0, term_deadline - time.monotonic())))
    while time.monotonic() < cleanup_deadline:
        try:
            _merge_owned(known, _discover_owned())
        except BaseException:
            scan_ok = False
        try:
            _signal_owned(known, signal.SIGKILL)
            _reap(root_pid)
            if selector is not None:
                _drain(selector, captures)
            if not _children(os.getpid()) and not _live_owned(known):
                return scan_ok
        except BaseException:
            scan_ok = False
        time.sleep(min(POLL_SECONDS, max(0.0, cleanup_deadline - time.monotonic())))
    return False


def _argument_value(argv: Sequence[str], name: str, *, required: bool = True) -> str | None:
    positions = [index for index, value in enumerate(argv) if value == name]
    if not positions:
        if required:
            raise GuardianError(f"launcher requires {name}")
        return None
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise GuardianError(f"launcher requires exactly one value for {name}")
    return argv[positions[0] + 1]


def _replace_argument(argv: Sequence[str], name: str, value: str) -> list[str]:
    result = list(argv)
    positions = [index for index, item in enumerate(result) if item == name]
    if len(positions) != 1 or positions[0] + 1 >= len(result):
        raise GuardianError(f"launcher requires exactly one value for {name}")
    result[positions[0] + 1] = value
    return result


def _validate_launcher_arguments(argv: Sequence[str]) -> list[str]:
    """Accept only the workflow's read-only current-target invocation."""
    flags = {"--check", "--evaluate-current-release", "--collect-current-target-evidence"}
    values = {"--release-source-sha", "--evaluation-as-of", "--evaluation-output"}
    seen_flags: set[str] = set()
    parsed: dict[str, str] = {}
    index = 0
    while index < len(argv):
        name = argv[index]
        if name in flags:
            if name in seen_flags:
                raise GuardianError(f"launcher accepts {name} only once")
            seen_flags.add(name)
            index += 1
            continue
        if name not in values:
            raise GuardianError(f"launcher rejects unsupported argument {name!r}")
        if name in parsed or index + 1 >= len(argv) or argv[index + 1].startswith("--"):
            raise GuardianError(f"launcher requires exactly one value for {name}")
        parsed[name] = argv[index + 1]
        index += 2
    if seen_flags != flags or set(parsed) != values:
        raise GuardianError("launcher requires the complete fixed current-target check invocation")
    if not re.fullmatch(r"[0-9a-f]{40}", parsed["--release-source-sha"]):
        raise GuardianError("launcher requires a lowercase full Git source identity")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", parsed["--evaluation-as-of"]):
        raise GuardianError("launcher requires an explicit UTC evaluation timestamp")
    if parsed["--evaluation-output"] != FIXED_EVALUATION_OUTPUT:
        raise GuardianError("launcher requires the fixed current-evaluation output path")
    return list(argv)


def _prepare_stage(argv: Sequence[str]) -> tuple[pathlib.Path, pathlib.Path, list[str], pathlib.Path]:
    argv = _validate_launcher_arguments(argv)
    root = pathlib.Path.cwd().resolve(strict=True)
    if not root.is_dir():
        raise GuardianError("repository root is not a directory")
    output_value = _argument_value(argv, "--evaluation-output") or ""
    output_parts = output_value.split("/")
    if (
        not output_value or output_value.startswith("/") or "\\" in output_value
        or any(part in {"", ".", ".."} for part in output_parts)
        or output_parts[:2] != [".datapan", "ci"]
    ):
        raise GuardianError("evaluation output must be a normalized .datapan/ci relative path")
    dot_datapan = root / ".datapan"
    dot_datapan.mkdir(exist_ok=True)
    if stat.S_ISLNK(dot_datapan.lstat().st_mode) or not dot_datapan.is_dir():
        raise GuardianError(".datapan output root must be a real directory")
    ci_root = dot_datapan / "ci"
    ci_root.mkdir(exist_ok=True)
    if stat.S_ISLNK(ci_root.lstat().st_mode) or not ci_root.is_dir():
        raise GuardianError("current-target output root must be a real directory")
    output = root.joinpath(*output_parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    cursor = ci_root
    for part in output_parts[2:-1]:
        cursor = cursor / part
        metadata = cursor.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise GuardianError("evaluation output parent is not a real directory")
    if output.exists() or output.is_symlink():
        metadata = output.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise GuardianError("existing evaluation output is not a regular file")
        output.unlink()
    stage_dir = pathlib.Path(tempfile.mkdtemp(prefix=".current-target-", dir=output.parent))
    os.chmod(stage_dir, 0o700)
    staged = stage_dir / "evaluation.json"
    relative_stage = staged.relative_to(root).as_posix()
    child_args = _replace_argument(argv, "--evaluation-output", relative_stage)
    if "--repo-root" not in child_args:
        child_args.extend(("--repo-root", str(root)))
    return root, output, child_args, stage_dir


def _require_before_deadline(deadline_monotonic: float, phase: str) -> None:
    if (
        isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(float(deadline_monotonic))
        or time.monotonic() >= float(deadline_monotonic)
    ):
        raise GuardianError(f"current-target deadline expired before {phase}; evaluation was not promoted")


def _promote_result(
    stage_dir: pathlib.Path, output: pathlib.Path, *, deadline_monotonic: float,
) -> None:
    _require_before_deadline(deadline_monotonic, "staged-result inspection")
    staged = stage_dir / "evaluation.json"
    metadata = staged.lstat()
    _require_before_deadline(deadline_monotonic, "staged-result read")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise GuardianError("contained worker did not produce a regular evaluation result")
    if metadata.st_size > 8 * 1024 * 1024:
        raise GuardianError("evaluation result exceeds its output bound")
    try:
        payload = json.loads(staged.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardianError("contained worker evaluation result is unreadable") from exc
    _require_before_deadline(deadline_monotonic, "evaluation promotion")
    if not isinstance(payload, dict) or payload.get("schema_version") != "datapan.completeness-current-release-evaluation.v1":
        raise GuardianError("contained worker did not produce the current-evaluation schema")
    _require_before_deadline(deadline_monotonic, "evaluation replacement")
    os.replace(staged, output)
    if time.monotonic() >= float(deadline_monotonic):
        # The destination is on the same filesystem as the private stage, so
        # a late return can be rolled back without exposing a current result.
        try:
            os.replace(output, staged)
        except OSError:
            try:
                output.unlink()
            except OSError as exc:
                raise GuardianError("deadline expired during promotion and late output could not be removed") from exc
        raise GuardianError("current-target deadline expired during evaluation replacement")
    try:
        stage_dir.rmdir()
    except OSError:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    started = time.monotonic()
    deadline = started + EXECUTION_SECONDS
    args = list(sys.argv[1:] if argv is None else argv)
    stage_dir: pathlib.Path | None = None
    try:
        if _subreaper_state() != 0:
            raise GuardianError("current-target guardian cannot nest inside another subreaper")
        _assert_dedicated_process()
        root, output, child_args, stage_dir = _prepare_stage(args)
        if time.monotonic() >= deadline:
            raise GuardianError("current-target launcher setup exhausted the fixed execution window")
        environment = dict(os.environ)
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
            }
        )
        python = str(pathlib.Path(sys.executable).resolve(strict=True))
        worker = str(pathlib.Path(__file__).resolve().with_name("generate-completeness-proof-rollup.py"))
        result = run_owned_attempt(
            (python, "-I", "-B", worker, *child_args), cwd=root,
            environment=environment, deadline_monotonic=deadline,
        )
        if result.state != "worker_exited" or not result.containment_established or result.returncode != 0:
            print(f"FAIL current-target evaluation guardian: {result.state}", file=sys.stderr)
            if stage_dir is not None and result.containment_established:
                shutil.rmtree(stage_dir, ignore_errors=True)
                stage_dir = None
            return 1
        _require_before_deadline(deadline, "captured-output flush")
        sys.stdout.buffer.write(result.stdout)
        sys.stdout.flush()
        sys.stderr.buffer.write(result.stderr)
        sys.stderr.flush()
        _require_before_deadline(deadline, "evaluation promotion")
        _promote_result(stage_dir, output, deadline_monotonic=deadline)
        stage_dir = None
        return 0
    except (GuardianError, OSError, ValueError) as exc:
        print(f"FAIL current-target evaluation guardian: {exc}", file=sys.stderr)
        return 1
    finally:
        # Any non-success path retains its private stage for diagnosis. The
        # contained-failure branch above removes it only after full reaping.
        pass


if __name__ == "__main__":
    raise SystemExit(main())
