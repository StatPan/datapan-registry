#!/usr/bin/env python3
"""Collect one authenticated, read-only current source-target disposition.

The collector joins the retained processor state, the classified source target,
the canonical-update journal, and the exact native C evidence without granting
any write, parent-selection, publication, or provider capability.  Its verified
result is an in-process capability; serialized projections are diagnostics and
cannot mint a replacement capability.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "datapan.current-target-evidence.v1"
INDEX_SCHEMA_VERSION = "datapan.current-target-evidence-index.v1"
VERIFY_WORKFLOW_PATH = ".github/workflows/verify-release.yml"
CANONICAL_WORKFLOW_PATH = ".github/workflows/canonical-update-promotion.yml"
PROCESSOR_STATE_BRANCH = "automation/upstream-catalogue-state"
CANONICAL_STATE_BRANCH = "automation/canonical-update-state"
PROCESSOR_STATE_ROOT = pathlib.PurePosixPath(".datapan/upstream-catalogue-state")
JOURNAL_PATH = pathlib.PurePosixPath("reports/canonical-update-promotion-receipt.json")
GITHUB_API_VERSION = "2026-03-10"
MAX_NATIVE_JSON_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_GIT_BLOB_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_FILES = 5000
MAX_EVIDENCE_BYTES = 1152 * 1024 * 1024
MAX_NATIVE_STDERR_BYTES = 64 * 1024
MAX_COLLECTION_SECONDS = 120.0
NATIVE_POLL_SECONDS = 0.1
NATIVE_TERMINATE_GRACE_SECONDS = 0.5
SHA1_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_VERIFIED = object()
_UNAVAILABLE = object()


class CurrentTargetEvidenceError(ValueError):
    """Authenticated input was malformed, conflicting, or out of bounds."""


class CurrentTargetNativeUnavailable(RuntimeError):
    """A bounded native read could not be completed."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CurrentTargetEvidenceError("current target evidence is not canonical JSON") from exc


def _strict_json(raw: bytes, *, label: str, maximum_bytes: int) -> Mapping[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum_bytes:
        raise CurrentTargetEvidenceError(f"{label} exceeds its byte bound")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-JSON numeric constant")

    try:
        text = raw.decode("utf-8", errors="strict")
        if text.startswith("\ufeff"):
            raise ValueError("UTF-8 BOM is not permitted")
        value = json.loads(
            text, object_pairs_hook=unique_object, parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CurrentTargetEvidenceError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise CurrentTargetEvidenceError(f"{label} is not a JSON object")
    return value


def _sha1(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA1_RE.fullmatch(value) is None:
        raise CurrentTargetEvidenceError(f"{label} must be a full Git SHA")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CurrentTargetEvidenceError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise CurrentTargetEvidenceError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise CurrentTargetEvidenceError(f"{label} must use UTC")
    return value


def _load_module(path: pathlib.Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CurrentTargetEvidenceError(f"current target dependency is unavailable: {path.name}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module


def _remaining(deadline_monotonic: float) -> float:
    if (
        isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(float(deadline_monotonic))
    ):
        raise CurrentTargetEvidenceError("current target deadline is not finite")
    remaining = float(deadline_monotonic) - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise CurrentTargetNativeUnavailable("current target deadline expired")
    return remaining


def _attempt_deadline(deadline_monotonic: float) -> float:
    remaining = _remaining(deadline_monotonic)
    if remaining > MAX_COLLECTION_SECONDS:
        raise CurrentTargetEvidenceError("current target deadline exceeds the whole-attempt bound")
    return float(deadline_monotonic)


def _process_group_alive(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        raise CurrentTargetNativeUnavailable(
            "current target process group liveness is unavailable"
        ) from exc
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the owned process group and deterministically reap its leader."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=NATIVE_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=NATIVE_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CurrentTargetNativeUnavailable("current target child could not be reaped") from exc
    cleanup_deadline = time.monotonic() + NATIVE_TERMINATE_GRACE_SECONDS
    while _process_group_alive(process) and time.monotonic() < cleanup_deadline:
        time.sleep(0.01)
    if _process_group_alive(process):
        raise CurrentTargetNativeUnavailable("current target process group could not be contained")


def _scan_owned_tree(
    root: pathlib.Path,
    *,
    deadline_monotonic: float,
    include_git: bool,
    maximum_files: int = MAX_EVIDENCE_FILES,
    maximum_bytes: int = MAX_EVIDENCE_BYTES,
    allow_mutation: bool = False,
) -> list[tuple[pathlib.Path, os.stat_result]]:
    """Preflight one private tree without following links or reading file bodies."""
    root = pathlib.Path(root)
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CurrentTargetEvidenceError("current target evidence root is unavailable") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise CurrentTargetEvidenceError("current target evidence root is not a private directory")
    files: list[tuple[pathlib.Path, os.stat_result]] = []
    total = 0
    stack = [root]
    visited_directories = 0
    while stack:
        _remaining(deadline_monotonic)
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name, reverse=True)
        except FileNotFoundError:
            if allow_mutation:
                continue
            raise CurrentTargetEvidenceError("current target evidence tree changed during scanning")
        except OSError as exc:
            raise CurrentTargetEvidenceError("current target evidence tree cannot be scanned") from exc
        visited_directories += 1
        if visited_directories > maximum_files:
            raise CurrentTargetEvidenceError("current target evidence exceeds its directory bound")
        for entry in entries:
            relative = pathlib.Path(entry.path).relative_to(root)
            if not include_git and ".git" in relative.parts:
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                if allow_mutation:
                    continue
                raise CurrentTargetEvidenceError("current target evidence changed during scanning")
            except OSError as exc:
                raise CurrentTargetEvidenceError("current target evidence metadata is unavailable") from exc
            mode = metadata.st_mode
            if stat.S_ISLNK(mode):
                raise CurrentTargetEvidenceError("current target evidence contains a symlink")
            if stat.S_ISDIR(mode):
                stack.append(pathlib.Path(entry.path))
                continue
            if not stat.S_ISREG(mode):
                raise CurrentTargetEvidenceError("current target evidence contains a special file")
            if len(files) >= maximum_files or metadata.st_size > maximum_bytes - total:
                raise CurrentTargetEvidenceError("current target evidence exceeds its aggregate bound")
            total += metadata.st_size
            files.append((pathlib.Path(entry.path), metadata))
    return sorted(files, key=lambda item: item[0].relative_to(root).as_posix())


def _bounded_process(
    argv: Sequence[str],
    *,
    cwd: pathlib.Path,
    deadline_monotonic: float,
    binary: bool,
    max_stdout_bytes: int,
    owned_root: pathlib.Path | None = None,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[Any]:
    if (
        isinstance(max_stdout_bytes, bool)
        or not isinstance(max_stdout_bytes, int)
        or max_stdout_bytes < 1
        or max_stdout_bytes > 32 * 1024 * 1024
    ):
        raise CurrentTargetEvidenceError("current target native output bound is invalid")
    _remaining(deadline_monotonic)
    environment = {
        **os.environ,
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    }
    try:
        process = subprocess.Popen(
            tuple(argv), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
            env=environment, start_new_session=True,
        )
    except OSError as exc:
        raise CurrentTargetNativeUnavailable("current target native process could not start") from exc
    selector: selectors.BaseSelector | None = None
    stdout_stream: Any = None
    stderr_stream: Any = None
    completed = False
    active_failure: BaseException | None = None
    try:
        stdout_stream = process.stdout
        stderr_stream = process.stderr
        if stdout_stream is None or stderr_stream is None:
            raise CurrentTargetNativeUnavailable("current target native pipes are unavailable")
        selector = selectors.DefaultSelector()
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        counts = {"stdout": 0, "stderr": 0}
        limits = {"stdout": max_stdout_bytes, "stderr": MAX_NATIVE_STDERR_BYTES}
        streams = {stdout_stream: "stdout", stderr_stream: "stderr"}
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while True:
            remaining = _remaining(deadline_monotonic)
            if owned_root is not None:
                _scan_owned_tree(
                    owned_root, deadline_monotonic=deadline_monotonic, include_git=True,
                    allow_mutation=True,
                )
            returncode = process.poll()
            if returncode is not None and returncode not in allowed_returncodes:
                raise CurrentTargetNativeUnavailable(
                    f"current target native read failed ({returncode}): {shlex.join(tuple(argv[:2]))}"
                )
            if not selector.get_map() and returncode is not None and not _process_group_alive(process):
                break
            if selector.get_map():
                events = selector.select(min(remaining, NATIVE_POLL_SECONDS))
            else:
                time.sleep(min(remaining, NATIVE_POLL_SECONDS))
                events = []
            for key, _events in events:
                stream = key.fileobj
                label = streams[stream]
                maximum = limits[label]
                read_size = min(65536, maximum + 1 - counts[label])
                if read_size < 1:
                    raise CurrentTargetEvidenceError(
                        f"current target native {label} exceeded its byte bound"
                    )
                try:
                    chunk = os.read(stream.fileno(), read_size)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                counts[label] += len(chunk)
                if counts[label] > maximum:
                    raise CurrentTargetEvidenceError(
                        f"current target native {label} exceeded its byte bound"
                    )
                chunks[label].append(chunk)
        remaining = _remaining(deadline_monotonic)
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise CurrentTargetNativeUnavailable("current target native read exceeded its deadline") from exc
        if owned_root is not None:
            _scan_owned_tree(
                owned_root, deadline_monotonic=deadline_monotonic, include_git=True,
            )
        stdout_bytes = b"".join(chunks["stdout"])
        stderr_bytes = b"".join(chunks["stderr"])
        if binary:
            stdout_value: bytes | str = stdout_bytes
            stderr_value: bytes | str = stderr_bytes
        else:
            try:
                stdout_value = stdout_bytes.decode("utf-8", errors="strict")
                stderr_value = stderr_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise CurrentTargetEvidenceError("current target native output is not UTF-8") from exc
        completed = True
        return subprocess.CompletedProcess(tuple(argv), returncode, stdout_value, stderr_value)
    except BaseException as exc:
        active_failure = exc
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        if not completed:
            try:
                _terminate_process_group(process)
            except BaseException as exc:
                cleanup_errors.append(exc)
        if selector is not None:
            try:
                selector.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        for stream in (stdout_stream, stderr_stream):
            if stream is not None:
                try:
                    stream.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
        if cleanup_errors:
            details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in cleanup_errors)
            if active_failure is not None:
                active_failure.add_note(f"current target native cleanup failure: {details}")
            else:
                raise CurrentTargetNativeUnavailable(
                    f"current target native cleanup failed: {details}"
                ) from cleanup_errors[0]


def _write_owned_bytes(
    path: pathlib.Path,
    raw: bytes,
    *,
    evidence_root: pathlib.Path,
    deadline_monotonic: float,
) -> None:
    """Write one attempt-owned file after enforcing the aggregate budget."""
    if not isinstance(raw, bytes):
        raise CurrentTargetEvidenceError("current target evidence write is not bytes")
    existing = _scan_owned_tree(
        evidence_root, deadline_monotonic=deadline_monotonic, include_git=False,
    )
    total = sum(metadata.st_size for _candidate, metadata in existing)
    if len(existing) >= MAX_EVIDENCE_FILES or len(raw) > MAX_EVIDENCE_BYTES - total:
        raise CurrentTargetEvidenceError("current target evidence exceeds its aggregate bound")
    path = pathlib.Path(path)
    try:
        path.relative_to(evidence_root)
    except ValueError as exc:
        raise CurrentTargetEvidenceError("current target evidence write escaped its root") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CurrentTargetEvidenceError("current target evidence file cannot be created") from exc
    try:
        offset = 0
        while offset < len(raw):
            _remaining(deadline_monotonic)
            written = os.write(descriptor, raw[offset:offset + 1024 * 1024])
            if written < 1:
                raise CurrentTargetEvidenceError("current target evidence write did not progress")
            offset += written
    finally:
        os.close(descriptor)
    _scan_owned_tree(
        evidence_root, deadline_monotonic=deadline_monotonic, include_git=False,
    )


def _stream_owned_digest(
    path: pathlib.Path,
    expected: os.stat_result,
    *,
    deadline_monotonic: float,
) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CurrentTargetEvidenceError("current target evidence file cannot be opened") from exc
    digest = hashlib.sha256()
    total = 0
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or (observed.st_dev, observed.st_ino, observed.st_size)
            != (expected.st_dev, expected.st_ino, expected.st_size)
        ):
            raise CurrentTargetEvidenceError("current target evidence changed during indexing")
        while True:
            _remaining(deadline_monotonic)
            chunk = os.read(descriptor, min(1024 * 1024, expected.st_size - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > expected.st_size:
                raise CurrentTargetEvidenceError("current target evidence grew during indexing")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    if total != expected.st_size:
        raise CurrentTargetEvidenceError("current target evidence changed during indexing")
    return digest.hexdigest()


def _git(
    root: pathlib.Path,
    *args: str,
    deadline_monotonic: float,
    allowed: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    result = _bounded_process(
        ("git", *args), cwd=root, deadline_monotonic=deadline_monotonic,
        binary=False, max_stdout_bytes=1024 * 1024,
        allowed_returncodes=allowed,
    )
    return result


def _repository_from_remote(value: str) -> str:
    raw = value.strip()
    if raw.startswith("git@github.com:"):
        path = raw.removeprefix("git@github.com:")
    else:
        parsed = urllib.parse.urlparse(raw)
        if (
            parsed.scheme not in {"https", "ssh"}
            or (parsed.hostname or "").casefold() != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise CurrentTargetEvidenceError("current target source remote is not GitHub")
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    if REPOSITORY_RE.fullmatch(path) is None:
        raise CurrentTargetEvidenceError("current target source remote repository is invalid")
    return path


class GitHubCurrentTargetNativeReader:
    """One bounded, read-only GitHub client for current-target collection."""

    __slots__ = (
        "root", "repository", "workflow_path", "event", "run_id",
        "run_attempt", "source_sha", "_origin_url", "_deadline_monotonic",
    )

    def __init__(
        self,
        root: pathlib.Path,
        repository: str,
        *,
        workflow_path: str,
        event: str,
        run_id: str,
        run_attempt: int,
        source_sha: str,
        deadline_monotonic: float,
    ) -> None:
        deadline_monotonic = _attempt_deadline(deadline_monotonic)
        root = pathlib.Path(root).resolve()
        if not root.is_dir() or (root / ".git").exists() is False:
            raise CurrentTargetEvidenceError("current target source root is not a Git checkout")
        if REPOSITORY_RE.fullmatch(repository) is None:
            raise CurrentTargetEvidenceError("current target repository is invalid")
        if workflow_path != VERIFY_WORKFLOW_PATH:
            raise CurrentTargetEvidenceError("current target current-run workflow is unsupported")
        if not isinstance(event, str) or not re.fullmatch(r"[a-z_]{2,32}", event):
            raise CurrentTargetEvidenceError("current target current-run event is invalid")
        if not isinstance(run_id, str) or re.fullmatch(r"[0-9]{1,20}", run_id) is None:
            raise CurrentTargetEvidenceError("current target current-run id is invalid")
        if isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or run_attempt < 1:
            raise CurrentTargetEvidenceError("current target current-run attempt is invalid")
        _sha1(source_sha, "current target current-run source")
        origin_url = _git(
            root, "remote", "get-url", "origin",
            deadline_monotonic=deadline_monotonic,
        ).stdout.strip()
        if _repository_from_remote(origin_url).casefold() != repository.casefold():
            raise CurrentTargetEvidenceError("current target source remote names another repository")
        self.root = root
        self.repository = repository
        self.workflow_path = workflow_path
        self.event = event
        self.run_id = run_id
        self.run_attempt = run_attempt
        self.source_sha = source_sha
        self._origin_url = origin_url
        self._deadline_monotonic = deadline_monotonic

    @staticmethod
    def _remaining(deadline_monotonic: float) -> float:
        return _remaining(deadline_monotonic)

    def _run(
        self,
        argv: Sequence[str],
        *,
        deadline_monotonic: float,
        binary: bool = False,
        max_stdout_bytes: int = MAX_NATIVE_JSON_BYTES,
        cwd: pathlib.Path | None = None,
        owned_root: pathlib.Path | None = None,
    ) -> subprocess.CompletedProcess[Any]:
        if float(deadline_monotonic) != self._deadline_monotonic:
            raise CurrentTargetEvidenceError("current target native read uses another attempt deadline")
        result = _bounded_process(
            argv, cwd=cwd or self.root, deadline_monotonic=deadline_monotonic,
            binary=binary, max_stdout_bytes=max_stdout_bytes,
            owned_root=owned_root,
        )
        return result

    def _json(
        self, argv: Sequence[str], *, deadline_monotonic: float,
        maximum_bytes: int = MAX_NATIVE_JSON_BYTES,
    ) -> Mapping[str, Any]:
        result = self._run(
            argv, deadline_monotonic=deadline_monotonic,
            max_stdout_bytes=maximum_bytes,
        )
        raw = result.stdout.encode("utf-8") if isinstance(result.stdout, str) else result.stdout
        return _strict_json(raw, label="GitHub native response", maximum_bytes=maximum_bytes)

    def _api_json(
        self, endpoint: str, *, deadline_monotonic: float,
        maximum_bytes: int = MAX_NATIVE_JSON_BYTES,
    ) -> Mapping[str, Any]:
        return self._json((
            "gh", "api", "--header", f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            "--header", "Accept: application/vnd.github+json", "--method", "GET", endpoint,
        ), deadline_monotonic=deadline_monotonic, maximum_bytes=maximum_bytes)

    def _read_remote_branch(self, branch: str, *, deadline_monotonic: float) -> str | None:
        if branch not in {PROCESSOR_STATE_BRANCH, CANONICAL_STATE_BRANCH, "main"}:
            raise CurrentTargetEvidenceError("current target native branch is unsupported")
        ref = f"refs/heads/{branch}"
        result = self._run(
            ("git", "ls-remote", "--heads", "origin", ref),
            deadline_monotonic=deadline_monotonic, max_stdout_bytes=4096,
        )
        rows = [line.split("\t", 1) for line in result.stdout.splitlines() if line.strip()]
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref:
            raise CurrentTargetEvidenceError("current target native branch ref is ambiguous")
        return _sha1(rows[0][0], "current target native branch head")

    def read_default_branch_tip(self, *, deadline_monotonic: float) -> str:
        value = self._read_remote_branch("main", deadline_monotonic=deadline_monotonic)
        if value is None:
            raise CurrentTargetNativeUnavailable("current target default branch is unavailable")
        return value

    def read_processor_state_tip(self, *, deadline_monotonic: float) -> str | None:
        return self._read_remote_branch(PROCESSOR_STATE_BRANCH, deadline_monotonic=deadline_monotonic)

    def read_state_tip(self, *, deadline_monotonic: float) -> str | None:
        return self._read_remote_branch(CANONICAL_STATE_BRANCH, deadline_monotonic=deadline_monotonic)

    def materialize_processor_state_checkout(
        self, destination: pathlib.Path, *, deadline_monotonic: float,
    ) -> Mapping[str, Any]:
        destination = pathlib.Path(destination).resolve(strict=False)
        if destination.exists() or destination.is_symlink():
            raise CurrentTargetEvidenceError("processor state evidence destination already exists")
        if destination == self.root or destination.is_relative_to(self.root):
            raise CurrentTargetEvidenceError("processor state evidence destination is inside the source checkout")
        expected = self.read_processor_state_tip(deadline_monotonic=deadline_monotonic)
        if expected is None:
            raise CurrentTargetNativeUnavailable("processor state branch is unavailable")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            ("git", "init", "-q", str(destination)),
            deadline_monotonic=deadline_monotonic, owned_root=destination,
        )
        self._run(
            ("git", "-C", str(destination), "remote", "add", "origin", self._origin_url),
            deadline_monotonic=deadline_monotonic, owned_root=destination,
        )
        tracking = f"refs/remotes/origin/{PROCESSOR_STATE_BRANCH}"
        self._run((
            "git", "-C", str(destination), "fetch", "--no-tags", "--depth=1", "origin",
            f"+refs/heads/{PROCESSOR_STATE_BRANCH}:{tracking}",
        ), deadline_monotonic=deadline_monotonic, max_stdout_bytes=1024 * 1024,
            owned_root=destination)
        fetched = self._run(
            ("git", "-C", str(destination), "rev-parse", "--verify", f"{tracking}^{{commit}}"),
            deadline_monotonic=deadline_monotonic, max_stdout_bytes=128,
            owned_root=destination,
        ).stdout.strip()
        if fetched != expected:
            raise CurrentTargetNativeUnavailable("processor state branch moved during its bounded fetch")
        self._run(
            ("git", "-C", str(destination), "checkout", "-q", "--detach", expected),
            deadline_monotonic=deadline_monotonic, max_stdout_bytes=1024 * 1024,
            owned_root=destination,
        )
        _remaining(deadline_monotonic)
        _scan_owned_tree(
            destination, deadline_monotonic=deadline_monotonic, include_git=True,
        )
        state_helper = _load_module(
            self.root / "scripts/upstream-catalogue-state-branch.py",
            f"current_target_state_branch_{id(self)}",
        )
        _remaining(deadline_monotonic)
        try:
            state_helper.validate_tree(destination, self.repository, expected)
        except Exception as exc:
            raise CurrentTargetEvidenceError("processor state branch failed its exact tree contract") from exc
        _remaining(deadline_monotonic)
        observed = self.read_processor_state_tip(deadline_monotonic=deadline_monotonic)
        if observed != expected:
            raise CurrentTargetNativeUnavailable("processor state branch moved during collection")
        return {
            "repository": self.repository,
            "branch_ref": f"refs/heads/{PROCESSOR_STATE_BRANCH}",
            "commit_sha": expected,
        }

    def _git_blob(
        self, commit_sha: str, path: str, *, max_bytes: int,
        deadline_monotonic: float,
    ) -> bytes:
        _sha1(commit_sha, "current target Git blob commit")
        if (
            not isinstance(path, str) or not path or path.startswith("/")
            or "\\" in path or "\x00" in path
            or any(part in {"", ".", ".."} for part in pathlib.PurePosixPath(path).parts)
        ):
            raise CurrentTargetEvidenceError("current target Git blob path is invalid")
        if (
            isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes < 1 or max_bytes > MAX_JOURNAL_BYTES
        ):
            raise CurrentTargetEvidenceError("current target Git blob bound is invalid")
        encoded_path = "/".join(urllib.parse.quote(part, safe="") for part in pathlib.PurePosixPath(path).parts)
        metadata = self._api_json(
            f"repos/{self.repository}/contents/{encoded_path}?ref={commit_sha}",
            deadline_monotonic=deadline_monotonic,
        )
        size = metadata.get("size")
        blob_sha = metadata.get("sha")
        if (
            metadata.get("type") != "file" or metadata.get("path") != path
            or isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > max_bytes
            or not isinstance(blob_sha, str) or SHA1_RE.fullmatch(blob_sha) is None
        ):
            raise CurrentTargetEvidenceError("current target Git blob metadata is invalid")
        blob = self._api_json(
            f"repos/{self.repository}/git/blobs/{blob_sha}",
            deadline_monotonic=deadline_monotonic,
            maximum_bytes=min(32 * 1024 * 1024, max(4096, max_bytes * 2)),
        )
        if blob.get("encoding") != "base64" or blob.get("sha") != blob_sha:
            raise CurrentTargetEvidenceError("current target Git blob response is invalid")
        content = blob.get("content")
        if not isinstance(content, str):
            raise CurrentTargetEvidenceError("current target Git blob content is unavailable")
        if re.fullmatch(r"[A-Za-z0-9+/=]*(?:\r?\n[A-Za-z0-9+/=]*)*", content) is None:
            raise CurrentTargetEvidenceError("current target Git blob base64 is invalid")
        try:
            raw = base64.b64decode(content.replace("\r", "").replace("\n", ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise CurrentTargetEvidenceError("current target Git blob base64 is invalid") from exc
        object_sha = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        if len(raw) != size or len(raw) > max_bytes or object_sha != blob_sha:
            raise CurrentTargetEvidenceError("current target Git blob identity differs from native metadata")
        return raw

    def read_journal_blob(self, commit_sha: str, *, deadline_monotonic: float) -> bytes:
        return self._git_blob(
            commit_sha, JOURNAL_PATH.as_posix(), max_bytes=MAX_JOURNAL_BYTES,
            deadline_monotonic=deadline_monotonic,
        )

    def read_git_tree_sha(self, commit_sha: str, *, deadline_monotonic: float) -> str:
        _sha1(commit_sha, "current target historical commit")
        value = self._api_json(
            f"repos/{self.repository}/git/commits/{commit_sha}",
            deadline_monotonic=deadline_monotonic,
        )
        tree = value.get("tree")
        if not isinstance(tree, Mapping):
            raise CurrentTargetEvidenceError("current target historical tree is unavailable")
        return _sha1(tree.get("sha"), "current target historical tree")

    def read_git_blob(
        self, commit_sha: str, path: str, *, max_bytes: int,
        deadline_monotonic: float,
    ) -> bytes:
        if max_bytes > MAX_GIT_BLOB_BYTES:
            raise CurrentTargetEvidenceError("current target historical blob exceeds its fixed cap")
        return self._git_blob(
            commit_sha, path, max_bytes=max_bytes,
            deadline_monotonic=deadline_monotonic,
        )

    def read_pull_request(
        self, repository: str, number: int, *, deadline_monotonic: float,
    ) -> Mapping[str, Any]:
        if repository.casefold() != self.repository.casefold() or REPOSITORY_RE.fullmatch(repository) is None:
            raise CurrentTargetEvidenceError("current target PR repository is invalid")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise CurrentTargetEvidenceError("current target PR number is invalid")
        primary = self._json((
            "gh", "pr", "view", str(number), "--repo", repository, "--json",
            "number,url,state,body,headRefName,headRefOid,baseRefName,mergeCommit",
        ), deadline_monotonic=deadline_monotonic)
        pull = self._api_json(
            f"repos/{repository}/pulls/{number}", deadline_monotonic=deadline_monotonic,
        )
        base = pull.get("base") if isinstance(pull.get("base"), Mapping) else {}
        head = pull.get("head") if isinstance(pull.get("head"), Mapping) else {}
        base_repository = base.get("repo") if isinstance(base.get("repo"), Mapping) else {}
        head_repository = head.get("repo") if isinstance(head.get("repo"), Mapping) else {}
        return {
            "number": primary.get("number"), "url": primary.get("url"),
            "state": primary.get("state"), "body": primary.get("body"),
            "headRefName": primary.get("headRefName"), "headRefOid": primary.get("headRefOid"),
            "baseRefName": primary.get("baseRefName"), "baseRefOid": base.get("sha"),
            "mergeCommit": primary.get("mergeCommit"),
            "repository": base_repository.get("full_name"),
            "headRepository": head_repository.get("full_name"),
            "merged": pull.get("merged"), "mergedAt": pull.get("merged_at"),
        }

    @staticmethod
    def _workflow_path(value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise CurrentTargetEvidenceError("current target workflow path is invalid")
        return value.split("@", 1)[0]

    def _read_actions_run(
        self, *, repository: str, run_id: str, run_attempt: int,
        include_jobs: bool, deadline_monotonic: float,
    ) -> Mapping[str, Any]:
        if (
            repository.casefold() != self.repository.casefold()
            or REPOSITORY_RE.fullmatch(repository) is None
            or not isinstance(run_id, str) or re.fullmatch(r"[0-9]{1,20}", run_id) is None
            or isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or run_attempt < 1
        ):
            raise CurrentTargetEvidenceError("current target Actions run identity is invalid")
        endpoint = f"repos/{repository}/actions/runs/{run_id}/attempts/{run_attempt}"
        for _ in range(2):
            before = self._api_json(endpoint, deadline_monotonic=deadline_monotonic)
            jobs_payload: Mapping[str, Any] | None = None
            if include_jobs:
                jobs_payload = self._api_json(
                    f"{endpoint}/jobs?per_page=100", deadline_monotonic=deadline_monotonic,
                )
            after = self._api_json(endpoint, deadline_monotonic=deadline_monotonic)
            fields = ("id", "run_attempt", "path", "event", "head_sha", "status", "conclusion")
            if any(before.get(field) != after.get(field) for field in fields):
                continue
            repository_row = after.get("repository")
            if (
                not isinstance(repository_row, Mapping)
                or str(repository_row.get("full_name", "")).casefold() != repository.casefold()
                or str(after.get("id")) != run_id
                or after.get("run_attempt") != run_attempt
                or after.get("status") not in {"queued", "in_progress", "completed"}
                or (after.get("conclusion") is not None and not isinstance(after.get("conclusion"), str))
            ):
                raise CurrentTargetEvidenceError("current target Actions run response is malformed")
            result: dict[str, Any] = {
                "repository": repository,
                "workflow_path": self._workflow_path(after.get("path")),
                "event": after.get("event"), "source_sha": after.get("head_sha"),
                "run_id": run_id, "run_attempt": run_attempt,
                "status": after.get("status"), "conclusion": after.get("conclusion"),
            }
            if include_jobs:
                assert jobs_payload is not None
                rows = jobs_payload.get("jobs")
                total = jobs_payload.get("total_count")
                if (
                    not isinstance(rows, list) or isinstance(total, bool) or not isinstance(total, int)
                    or total < 0 or total > 100 or len(rows) != total
                ):
                    raise CurrentTargetEvidenceError("current target Actions jobs exceed their bound")
                jobs: list[dict[str, Any]] = []
                for row in rows:
                    if not isinstance(row, Mapping):
                        raise CurrentTargetEvidenceError("current target Actions job is malformed")
                    raw_steps = row.get("steps", [])
                    if not isinstance(raw_steps, list) or len(raw_steps) > 128:
                        raise CurrentTargetEvidenceError("current target Actions steps exceed their bound")
                    steps: list[dict[str, Any]] = []
                    for step in raw_steps:
                        if not isinstance(step, Mapping):
                            raise CurrentTargetEvidenceError("current target Actions step is malformed")
                        steps.append({
                            "name": step.get("name"), "status": step.get("status"),
                            "conclusion": step.get("conclusion"),
                        })
                    jobs.append({
                        "name": row.get("name"), "status": row.get("status"),
                        "conclusion": row.get("conclusion"), "steps": steps,
                    })
                result["jobs"] = jobs
            return result
        raise CurrentTargetNativeUnavailable("current target Actions run changed during collection")

    def read_current_evaluation_identity(self, *, deadline_monotonic: float) -> Mapping[str, Any]:
        value = self._read_actions_run(
            repository=self.repository, run_id=self.run_id,
            run_attempt=self.run_attempt, include_jobs=False,
            deadline_monotonic=deadline_monotonic,
        )
        expected = {
            "repository": self.repository, "workflow_path": self.workflow_path,
            "event": self.event, "source_sha": self.source_sha,
            "run_id": self.run_id, "run_attempt": self.run_attempt,
        }
        if any(value.get(field) != expected[field] for field in expected):
            raise CurrentTargetEvidenceError("current target current-run identity differs from native readback")
        return {
            **expected,
            "source_tree_sha": self.read_git_tree_sha(
                self.source_sha, deadline_monotonic=deadline_monotonic,
            ),
        }

    def read_c_evaluator_run(
        self, repository: str, run_id: str, run_attempt: int, *, deadline_monotonic: float,
    ) -> Mapping[str, Any]:
        value = self._read_actions_run(
            repository=repository, run_id=run_id, run_attempt=run_attempt,
            include_jobs=True, deadline_monotonic=deadline_monotonic,
        )
        if value.get("workflow_path") != CANONICAL_WORKFLOW_PATH:
            raise CurrentTargetEvidenceError("historical C evaluator workflow is unexpected")
        return value


class _RecordingNativeReader:
    """Record bounded closed native projections and exact returned bytes."""

    def __init__(
        self, reader: Any, evidence_root: pathlib.Path, *, deadline_monotonic: float,
    ) -> None:
        self._reader = reader
        self._evidence_root = evidence_root
        self._root = evidence_root / "native"
        self._root.mkdir(parents=True, exist_ok=False)
        self._deadline_monotonic = deadline_monotonic
        self._sequence = 0
        self._journal_pin: tuple[str, bytes] | None = None
        self.repository = getattr(reader, "repository", None)
        self.root = getattr(reader, "root", None)

    def pin_journal_blob(self, commit_sha: str, raw: bytes) -> None:
        """Reuse the one recorded journal blob while rechecking its branch tip."""
        if self._journal_pin is not None:
            raise CurrentTargetEvidenceError("canonical journal blob was already pinned")
        self._journal_pin = (_sha1(commit_sha, "canonical journal commit"), bytes(raw))

    def read_journal_blob(self, commit_sha: str, *, deadline_monotonic: float) -> bytes:
        if self._journal_pin is not None:
            pinned_commit, raw = self._journal_pin
            if commit_sha != pinned_commit:
                raise CurrentTargetEvidenceError("canonical journal blob pin names another commit")
            return bytes(raw)
        recorded = self.__getattr__("read_journal_blob")
        return recorded(commit_sha, deadline_monotonic=deadline_monotonic)

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._reader, name)
        if not callable(target) or not name.startswith(("read_", "materialize_")):
            return target

        def recorded(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("deadline_monotonic") != self._deadline_monotonic:
                raise CurrentTargetEvidenceError("recorded native read uses another attempt deadline")
            _remaining(self._deadline_monotonic)
            self._sequence += 1
            prefix = self._root / f"{self._sequence:04d}-{name}"
            try:
                result = target(*args, **kwargs)
            except Exception as exc:
                document = {"method": name, "status": "unavailable", "error_type": type(exc).__name__}
                _write_owned_bytes(
                    prefix.with_suffix(".json"), _canonical_json(document) + b"\n",
                    evidence_root=self._evidence_root,
                    deadline_monotonic=self._deadline_monotonic,
                )
                raise
            _remaining(self._deadline_monotonic)
            if isinstance(result, bytes):
                if len(result) > MAX_JOURNAL_BYTES:
                    raise CurrentTargetEvidenceError("recorded native bytes exceed their fixed bound")
                _write_owned_bytes(
                    prefix.with_suffix(".bin"), result,
                    evidence_root=self._evidence_root,
                    deadline_monotonic=self._deadline_monotonic,
                )
                metadata = {
                    "method": name, "status": "returned_bytes", "bytes": len(result),
                    "sha256": hashlib.sha256(result).hexdigest(),
                }
            elif isinstance(result, (Mapping, list)):
                encoded = _canonical_json(result)
                if len(encoded) > MAX_NATIVE_JSON_BYTES:
                    raise CurrentTargetEvidenceError("recorded native JSON exceeds its fixed bound")
                _write_owned_bytes(
                    prefix.with_suffix(".result.json"), encoded + b"\n",
                    evidence_root=self._evidence_root,
                    deadline_monotonic=self._deadline_monotonic,
                )
                metadata = {
                    "method": name, "status": "returned_json", "bytes": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            else:
                metadata = {"method": name, "status": "returned_scalar", "value": result}
            _write_owned_bytes(
                prefix.with_suffix(".json"), _canonical_json(metadata) + b"\n",
                evidence_root=self._evidence_root,
                deadline_monotonic=self._deadline_monotonic,
            )
            return result

        return recorded


class VerifiedCurrentTargetEvidence:
    """Opaque current-target authority minted only by the shared collector."""

    __slots__ = ("_context", "_projection", "_source_target", "_disposition")

    def __init__(
        self, token: object, *, context: Mapping[str, Any], projection: Mapping[str, Any],
        source_target: Any, disposition: Any,
    ) -> None:
        if token is not _VERIFIED:
            raise CurrentTargetEvidenceError("verified current target evidence constructor is private")
        self._context = copy.deepcopy(dict(context))
        self._projection = copy.deepcopy(dict(projection))
        self._source_target = source_target
        self._disposition = disposition

    @property
    def context(self) -> dict[str, Any]:
        return copy.deepcopy(self._context)

    @property
    def target_applicability(self) -> dict[str, Any]:
        return copy.deepcopy(self._projection["target_applicability"])


class UnavailableCurrentTargetEvidence:
    """Closed diagnostic result that carries no target authority."""

    __slots__ = ("reason_code", "_context", "_evidence_index")

    def __init__(
        self, token: object, *, reason_code: str, context: Mapping[str, Any],
        evidence_index: Mapping[str, Any] | None,
    ) -> None:
        if token is not _UNAVAILABLE:
            raise CurrentTargetEvidenceError("unavailable current target evidence constructor is private")
        if not isinstance(reason_code, str) or re.fullmatch(r"[a-z0-9_]{1,96}", reason_code) is None:
            raise CurrentTargetEvidenceError("unavailable current target reason is invalid")
        self.reason_code = reason_code
        self._context = copy.deepcopy(dict(context))
        self._evidence_index = copy.deepcopy(dict(evidence_index)) if evidence_index is not None else None

    @property
    def context(self) -> dict[str, Any]:
        return copy.deepcopy(self._context)

    @property
    def evidence_index(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._evidence_index)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "availability": "unavailable",
            "reason_code": self.reason_code,
            "context": self.context,
            "evidence_index": self.evidence_index,
        }


def unavailable_current_target_evidence(
    *,
    reason_code: str,
    repository: str,
    release_source_sha: str,
    trusted_main_sha: str,
    evaluation_as_of: str,
) -> UnavailableCurrentTargetEvidence:
    """Mint a non-authoritative diagnostic when collection cannot start.

    This factory is for a missing local Actions identity or credential.  It
    cannot produce a verified capability and deliberately carries no evidence
    index because no native collection ran.
    """
    if REPOSITORY_RE.fullmatch(repository) is None:
        raise CurrentTargetEvidenceError("current target repository is invalid")
    context = {
        "repository": repository,
        "release_source_sha": _sha1(release_source_sha, "current target release source"),
        "trusted_main_sha": _sha1(trusted_main_sha, "current target trusted main"),
        "evaluation_as_of": _timestamp(evaluation_as_of, "current target evaluation clock"),
    }
    return UnavailableCurrentTargetEvidence(
        _UNAVAILABLE, reason_code=reason_code, context=context, evidence_index=None,
    )


def project_current_target_evidence(
    capability: VerifiedCurrentTargetEvidence,
    *,
    release_source_sha: str,
    trusted_main_sha: str,
    evaluation_as_of: str,
) -> dict[str, Any]:
    """Return the closed JSON projection after repeating its exact context join."""
    if type(capability) is not VerifiedCurrentTargetEvidence:
        raise CurrentTargetEvidenceError("current target projection requires the exact verified capability")
    expected = {
        "repository": capability._context["repository"],
        "release_source_sha": _sha1(release_source_sha, "current target release source"),
        "trusted_main_sha": _sha1(trusted_main_sha, "current target trusted main"),
        "evaluation_as_of": _timestamp(evaluation_as_of, "current target evaluation clock"),
    }
    if expected != capability._context:
        raise CurrentTargetEvidenceError("current target capability belongs to another evaluation context")
    return copy.deepcopy(capability._projection)


def _prepare_evidence_root(path: pathlib.Path) -> pathlib.Path:
    supplied = pathlib.Path(path)
    if supplied.is_symlink():
        raise CurrentTargetEvidenceError("current target evidence root cannot be a symlink")
    path = supplied.resolve(strict=False)
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise CurrentTargetEvidenceError("current target evidence root must be an empty private directory")
    else:
        path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _evidence_index(
    evidence_root: pathlib.Path, *, context: Mapping[str, Any], state: Mapping[str, Any],
    deadline_monotonic: float,
) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    files = _scan_owned_tree(
        evidence_root, deadline_monotonic=deadline_monotonic, include_git=False,
    )
    for path, metadata in files:
        relative = path.relative_to(evidence_root)
        if relative.as_posix() == "current-target-evidence-index.json":
            continue
        members.append({
            "path": relative.as_posix(), "bytes": metadata.st_size,
            "sha256": _stream_owned_digest(
                path, metadata, deadline_monotonic=deadline_monotonic,
            ),
        })
    document = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "context": dict(context), "state": dict(state), "members": members,
    }
    raw = _canonical_json(document) + b"\n"
    index_path = evidence_root / "current-target-evidence-index.json"
    _write_owned_bytes(
        index_path, raw, evidence_root=evidence_root,
        deadline_monotonic=deadline_monotonic,
    )
    return {
        "path": index_path.name, "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _unavailable(
    reason_code: str, *, context: Mapping[str, Any], evidence_root: pathlib.Path,
    deadline_monotonic: float, state: Mapping[str, Any] | None = None,
) -> UnavailableCurrentTargetEvidence:
    try:
        _remaining(deadline_monotonic)
    except CurrentTargetNativeUnavailable:
        locator = None
    else:
        locator = _evidence_index(
            evidence_root, context=context, state=state or {},
            deadline_monotonic=deadline_monotonic,
        )
    return UnavailableCurrentTargetEvidence(
        _UNAVAILABLE, reason_code=reason_code, context=context, evidence_index=locator,
    )


def collect_current_target_evidence(
    *,
    repository: str,
    release_source_sha: str,
    trusted_main_sha: str,
    evaluation_as_of: str,
    native_reader: Any,
    evidence_root: pathlib.Path,
    deadline_monotonic: float,
) -> VerifiedCurrentTargetEvidence | UnavailableCurrentTargetEvidence:
    """Collect one context-bound source-target disposition without side effects."""
    if REPOSITORY_RE.fullmatch(repository) is None:
        raise CurrentTargetEvidenceError("current target repository is invalid")
    release_source_sha = _sha1(release_source_sha, "current target release source")
    trusted_main_sha = _sha1(trusted_main_sha, "current target trusted main")
    evaluation_as_of = _timestamp(evaluation_as_of, "current target evaluation clock")
    context = {
        "repository": repository,
        "release_source_sha": release_source_sha,
        "trusted_main_sha": trusted_main_sha,
        "evaluation_as_of": evaluation_as_of,
    }
    try:
        deadline_monotonic = _attempt_deadline(deadline_monotonic)
    except CurrentTargetNativeUnavailable:
        return UnavailableCurrentTargetEvidence(
            _UNAVAILABLE, reason_code="collection_deadline_exceeded",
            context=context, evidence_index=None,
        )
    evidence_root = _prepare_evidence_root(evidence_root)
    _remaining(deadline_monotonic)
    reader_deadline = getattr(native_reader, "_deadline_monotonic", None)
    if reader_deadline is not None and reader_deadline != deadline_monotonic:
        raise CurrentTargetEvidenceError("current target reader belongs to another attempt deadline")
    if str(getattr(native_reader, "repository", "")).casefold() != repository.casefold():
        raise CurrentTargetEvidenceError("current target native reader names another repository")
    root_value = getattr(native_reader, "root", None)
    if root_value is None:
        raise CurrentTargetEvidenceError("current target native reader lacks its trusted source root")
    root = pathlib.Path(root_value).resolve()
    if not root.is_dir() or (root / ".git").exists() is False:
        raise CurrentTargetEvidenceError("current target native reader source root is invalid")
    for label, revision in (("release source", release_source_sha), ("trusted main", trusted_main_sha)):
        resolved = _git(
            root, "rev-parse", "--verify", f"{revision}^{{commit}}",
            deadline_monotonic=deadline_monotonic,
        ).stdout.strip()
        if resolved != revision:
            raise CurrentTargetEvidenceError(f"current target {label} object is unavailable")
    recording = _RecordingNativeReader(
        native_reader, evidence_root, deadline_monotonic=deadline_monotonic,
    )
    state: dict[str, Any] = {}
    try:
        native_main = recording.read_default_branch_tip(deadline_monotonic=deadline_monotonic)
    except (CurrentTargetNativeUnavailable, PermissionError, TimeoutError, OSError):
        return _unavailable(
            "trusted_main_native_read_unavailable", context=context,
            evidence_root=evidence_root, deadline_monotonic=deadline_monotonic,
        )
    if native_main != trusted_main_sha:
        raise CurrentTargetEvidenceError("trusted main differs from native default-branch readback")
    if release_source_sha != trusted_main_sha:
        return _unavailable(
            "release_source_is_candidate_or_historical", context=context,
            evidence_root=evidence_root, deadline_monotonic=deadline_monotonic,
            state={"native_main_sha": native_main},
        )
    try:
        current_identity = recording.read_current_evaluation_identity(
            deadline_monotonic=deadline_monotonic,
        )
    except (CurrentTargetNativeUnavailable, PermissionError, TimeoutError, OSError):
        return _unavailable(
            "current_evaluation_identity_unavailable", context=context,
            evidence_root=evidence_root, deadline_monotonic=deadline_monotonic,
            state={"native_main_sha": native_main},
        )
    expected_identity_fields = {
        "repository", "workflow_path", "event", "source_sha", "source_tree_sha",
        "run_id", "run_attempt",
    }
    if not isinstance(current_identity, Mapping) or set(current_identity) != expected_identity_fields:
        raise CurrentTargetEvidenceError("current evaluation identity has an unexpected field set")
    if (
        str(current_identity.get("repository", "")).casefold() != repository.casefold()
        or current_identity.get("workflow_path") != VERIFY_WORKFLOW_PATH
        or current_identity.get("source_sha") != release_source_sha
    ):
        raise CurrentTargetEvidenceError("current evaluation identity differs from its collection context")
    if current_identity.get("event") not in {"push", "schedule"}:
        return _unavailable(
            "current_evaluation_event_not_eligible", context=context,
            evidence_root=evidence_root, deadline_monotonic=deadline_monotonic,
            state={"native_main_sha": native_main},
        )
    source_tree_sha = _git(
        root, "rev-parse", "--verify", f"{trusted_main_sha}^{{tree}}",
        deadline_monotonic=deadline_monotonic,
    ).stdout.strip()
    if current_identity.get("source_tree_sha") != source_tree_sha:
        raise CurrentTargetEvidenceError("current evaluation tree differs from trusted main")
    evaluation = {
        "source_sha": trusted_main_sha,
        "source_tree_sha": source_tree_sha,
        "event": current_identity["event"],
        "run_id": str(current_identity["run_id"]),
        "run_attempt": current_identity["run_attempt"],
        "as_of": evaluation_as_of,
    }

    checkout = evidence_root / "processor-state-checkout"
    try:
        processor_provenance = recording.materialize_processor_state_checkout(
            checkout, deadline_monotonic=deadline_monotonic,
        )
    except (CurrentTargetNativeUnavailable, PermissionError, TimeoutError, OSError):
        return _unavailable(
            "processor_state_unavailable", context=context, evidence_root=evidence_root,
            deadline_monotonic=deadline_monotonic, state={"native_main_sha": native_main},
        )
    if (
        not isinstance(processor_provenance, Mapping)
        or set(processor_provenance) != {"repository", "branch_ref", "commit_sha"}
        or str(processor_provenance.get("repository", "")).casefold() != repository.casefold()
        or processor_provenance.get("branch_ref") != f"refs/heads/{PROCESSOR_STATE_BRANCH}"
    ):
        raise CurrentTargetEvidenceError("processor state provenance is malformed")
    processor_commit = _sha1(
        processor_provenance.get("commit_sha"), "current target processor state commit",
    )
    _remaining(deadline_monotonic)
    producer = _load_module(
        root / "scripts/run-canonical-update-promotion.py",
        f"current_target_producer_{id(recording)}",
    )
    _remaining(deadline_monotonic)
    try:
        source_target = producer.resolve_verified_source_target(
            root, checkout / pathlib.Path(*PROCESSOR_STATE_ROOT.parts), repository,
            trusted_main_sha, evaluation=evaluation,
        )
    except Exception as exc:
        raise CurrentTargetEvidenceError("current source target resolution failed") from exc
    try:
        _remaining(deadline_monotonic)
    except CurrentTargetNativeUnavailable:
        return _unavailable(
            "collection_deadline_exceeded", context=context, evidence_root=evidence_root,
            deadline_monotonic=deadline_monotonic,
            state={"native_main_sha": native_main, "processor_state_sha": processor_commit},
        )
    if isinstance(source_target, Mapping):
        action = source_target.get("action")
        reason = source_target.get("reason_code")
        code = (
            f"source_target_{action}_{reason}" if isinstance(action, str) and isinstance(reason, str)
            else "source_target_not_selected"
        )
        code = re.sub(r"[^a-z0-9_]+", "_", code.lower())[:96]
        return _unavailable(
            code, context=context, evidence_root=evidence_root,
            deadline_monotonic=deadline_monotonic,
            state={"native_main_sha": native_main, "processor_state_sha": processor_commit},
        )
    if type(source_target) is not producer.ResolvedSourceTarget:
        raise CurrentTargetEvidenceError("source target resolver returned an unsupported capability")
    if source_target.evaluation != evaluation or source_target.target_contract.get("source_sha") != trusted_main_sha:
        raise CurrentTargetEvidenceError("source target capability differs from its current evaluation")
    try:
        observed_processor = recording.read_processor_state_tip(deadline_monotonic=deadline_monotonic)
    except (CurrentTargetNativeUnavailable, PermissionError, TimeoutError, OSError):
        return _unavailable(
            "processor_state_recheck_unavailable", context=context,
            evidence_root=evidence_root, deadline_monotonic=deadline_monotonic,
            state={"native_main_sha": native_main, "processor_state_sha": processor_commit},
        )
    if observed_processor != processor_commit:
        raise CurrentTargetNativeUnavailable("processor state branch moved after source-target resolution")

    _remaining(deadline_monotonic)
    journal_helper = producer.load_canonical_update_pr(root)
    _remaining(deadline_monotonic)
    required_helpers = {
        "source_target_binding_from_capability", "validate_journal", "validate_source_target_noop",
        "revision_reference", "release_closure_identity", "body_marker",
    }
    if any(not callable(getattr(journal_helper, name, None)) for name in required_helpers):
        raise CurrentTargetEvidenceError("canonical source-target journal helper is incomplete")
    journal_schema = producer.load_object(
        root / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json",
    )
    _remaining(deadline_monotonic)
    try:
        journal_commit = recording.read_state_tip(deadline_monotonic=deadline_monotonic)
        journal_raw = (
            recording.read_journal_blob(journal_commit, deadline_monotonic=deadline_monotonic)
            if journal_commit is not None else None
        )
    except (CurrentTargetNativeUnavailable, PermissionError, TimeoutError, OSError):
        return _unavailable(
            "canonical_state_journal_unavailable", context=context, evidence_root=evidence_root,
            deadline_monotonic=deadline_monotonic,
            state={"native_main_sha": native_main, "processor_state_sha": processor_commit},
        )
    if journal_commit is None:
        journal: Mapping[str, Any] | None = None
    else:
        _sha1(journal_commit, "current target canonical journal commit")
        assert journal_raw is not None
        journal = _strict_json(
            journal_raw, label="canonical update journal", maximum_bytes=MAX_JOURNAL_BYTES,
        )
        try:
            journal_helper.validate_journal(journal, journal_schema)
        except Exception as exc:
            raise CurrentTargetEvidenceError("canonical update journal fails its exact schema") from exc
        recording.pin_journal_blob(journal_commit, journal_raw)
    _remaining(deadline_monotonic)
    disposition_module = _load_module(
        root / "scripts/canonical_source_target_disposition.py",
        f"current_target_disposition_{id(recording)}",
    )
    _remaining(deadline_monotonic)
    try:
        disposition = disposition_module.resolve_c_target_disposition(
            source_target=source_target, producer_module=producer,
            journal_snapshot={"journal": journal, "commit_sha": journal_commit},
            native_reader=recording, journal_helper=journal_helper,
            journal_schema=journal_schema, deadline_monotonic=deadline_monotonic,
        )
    except Exception as exc:
        raise CurrentTargetEvidenceError("current target disposition resolution failed") from exc
    try:
        _remaining(deadline_monotonic)
    except CurrentTargetNativeUnavailable:
        return _unavailable(
            "collection_deadline_exceeded", context=context, evidence_root=evidence_root,
            deadline_monotonic=deadline_monotonic,
            state={"native_main_sha": native_main, "processor_state_sha": processor_commit},
        )
    if type(disposition) is not disposition_module.VerifiedCTargetDisposition:
        raise CurrentTargetEvidenceError("current target disposition resolver returned an unsupported capability")
    binding = journal_helper.source_target_binding_from_capability(source_target, producer)
    disposition_value = disposition.as_dict()
    if (
        disposition_value.get("selection_key") != binding.get("selection_key")
        or disposition_value.get("original_observation_sha256") != binding.get("original_observation_sha256")
        or disposition_value.get("target_contract_sha256") != binding.get("target_contract_sha256")
    ):
        raise CurrentTargetEvidenceError("current target disposition differs from its selected target")
    disposition_name = disposition_value["disposition"]
    if disposition_name == "blocked":
        applicability = {
            "status": "unavailable", "reason_code": disposition_value["reason"],
            "disposition": disposition_name,
        }
    elif disposition_name == "unseen":
        applicability = {
            "status": "not_applicable", "reason_code": "no_recorded_source_target_disposition",
            "disposition": disposition_name,
        }
    else:
        applicability = {"status": "current", "reason_code": None, "disposition": disposition_name}
    state = {
        "native_main_sha": native_main,
        "processor_state_sha": processor_commit,
        "canonical_state_sha": journal_commit,
        "selection_key": binding["selection_key"],
    }
    index_locator = _evidence_index(
        evidence_root, context=context, state=state,
        deadline_monotonic=deadline_monotonic,
    )
    projection = {
        "schema_version": SCHEMA_VERSION,
        "context": context,
        "source_target": binding,
        "target_contract": copy.deepcopy(source_target.target_contract),
        "disposition": disposition_value,
        "target_applicability": applicability,
        "evidence_index": index_locator,
    }
    try:
        _remaining(deadline_monotonic)
    except CurrentTargetNativeUnavailable:
        return UnavailableCurrentTargetEvidence(
            _UNAVAILABLE, reason_code="collection_deadline_exceeded",
            context=context, evidence_index=index_locator,
        )
    return VerifiedCurrentTargetEvidence(
        _VERIFIED, context=context, projection=projection,
        source_target=source_target, disposition=disposition,
    )
