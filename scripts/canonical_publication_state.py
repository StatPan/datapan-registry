"""Bounded Git-backed storage for canonical-publication coordination.

The store uses a private bare object database for every process, writes only
the reviewed state-branch paths, and publishes each transaction with an exact
force-with-lease expectation.  Git LFS filters are disabled; this module only
reads Git objects and never downloads LFS payloads.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


REPOSITORY = "StatPan/datapan-registry"
STATE_BRANCH = "automation/canonical-update-state"
STATE_REF = f"refs/heads/{STATE_BRANCH}"
SUBPROCESS_CREDENTIAL_ENV_NAMES = (
    "DATAPAN_PUBLICATION_HF_SECRET", "HF_TOKEN", "HF_HUB_TOKEN",
    "HUGGINGFACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN",
    "HUGGING_FACE_HUB_TOKEN", "HF_API_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
)
ADMISSIONS_DIR = "reports/canonical-publication-admissions"
DISPATCH_DIR = "reports/canonical-publication/dispatch"
LEASE_DIR = "reports/canonical-publication/leases"
RECEIVERS_DIR = "reports/canonical-publication/receiver-registrations"
PUBLISHER_PHASES_DIR = "reports/canonical-publication/publisher-phases"
MANUAL_RECEIVERS_DIR = "reports/canonical-publication/manual-receiver-registrations"
MANUAL_PUBLISHER_PHASES_DIR = "reports/canonical-publication/manual-publisher-phases"
TERMINAL_OBSERVATIONS_DIR = "reports/canonical-publication/terminal-observations"
LEASE_SCHEMA = "datapan.canonical-publication-lease.v1"
PUBLISHER_PHASE_SCHEMA = "datapan.canonical-publication-publisher-phase.v1"
MANUAL_RECEIVER_SCHEMA = "datapan.registry-manual-publication-receiver.v1"
MANUAL_PUBLISHER_PHASE_SCHEMA = "datapan.registry-manual-publication-phase.v1"
TERMINAL_OBSERVATION_SCHEMA = "datapan.publisher-terminal-observation.v1"
ROLE_AUTOMATIC_OWNED_C = "automatic_owned_c"
ROLE_MANUAL_EXACT_SOURCE = "manual_exact_source"
SHA1_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
MAX_RECORD_BYTES = 1 * 1024 * 1024
MAX_TERMINAL_WITNESS_BYTES = 2 * 1024 * 1024
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_HISTORY_COMMITS = 2048
MAX_CANONICAL_STREAM_BYTES = 160 * 1024 * 1024
MAX_PYTHON_MODULE_INDEX_BYTES = 2 * 1024 * 1024
MAX_PYTHON_MODULE_INDEX_ENTRIES = 4096
MAX_CHANGED_PATHS_BYTES = 2 * 1024 * 1024
MAX_CHANGED_PATHS = 4096
MAX_PUBLISHER_CONTROL_FILES = 38
MAX_PUBLISHER_CONTROL_METADATA_BYTES = 2 * 1024 * 1024
MAX_PUBLISHER_CONTROL_FILE_BYTES = 2 * 1024 * 1024
MAX_PUBLISHER_CONTROL_TOTAL_BYTES = 32 * 1024 * 1024
PUBLISHER_CONTROL_BATCH_FRAME_BYTES = 128
OWNED_MERGE_JOURNAL_PATH = "reports/canonical-update-promotion-receipt.json"
OWNED_MERGE_SIDECAR_PATH = "reports/canonical-owned-c-merge-attempts.v1.json"
MAX_OWNED_MERGE_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_OWNED_MERGE_SIDECAR_BYTES = 1 * 1024 * 1024
TRUSTED_GIT_EXECUTABLE = pathlib.Path("/usr/bin/git")
TRUSTED_GIT_HELPER_PATH = "/usr/bin:/bin"
OWNED_MERGE_EMPTY_SIDECAR_BYTES = (
    b'{"records":[],"repository":"StatPan/datapan-registry",'
    b'"schema_version":"datapan.canonical-owned-c-merge-attempts.v1"}'
)


class StateStoreError(RuntimeError):
    """A bounded, fail-closed Git state operation failed."""


class StateConflict(StateStoreError):
    """The exact observed state ref or receiver claim changed concurrently."""


class StateOutcomeUncertain(StateStoreError):
    """A remote state write started but exact readback did not prove its result."""

    def __init__(
        self, message: str, *, child_sha: str | None = None,
        remote_ref_sha: str | None = None, remote_write_verified: bool | None = None,
        process_cleanup_confirmed: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.child_sha = child_sha
        self.remote_ref_sha = remote_ref_sha
        self.remote_write_verified = remote_write_verified
        self.process_cleanup_confirmed = process_cleanup_confirmed


class StateCleanupUncertain(StateStoreError):
    """A child or helper process group could not be proven fully reaped."""

    def __init__(self, message: str, *, command_returncode: int | None = None) -> None:
        super().__init__(message)
        self.command_returncode = command_returncode


@dataclass(frozen=True, slots=True)
class PythonModuleIndexEntry:
    """One exact regular Python source blob from an authenticated Git tree."""

    path: str
    mode: str
    blob_oid: str


@dataclass(frozen=True, slots=True)
class PythonModuleIndex:
    """Immutable Python namespace index for one exact commit/tree."""

    repository: str
    commit_sha: str
    tree_sha: str
    entries: tuple[PythonModuleIndexEntry, ...]


@dataclass(frozen=True, slots=True)
class WorktreeBlobIdentity:
    """Exact local bytes for one regular path in a clean Git worktree."""

    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class OwnedGitBlobMaterialization:
    """One streamed Git blob sealed as a private, store-owned regular file."""

    blob_oid: str
    byte_length: int
    sha256: str
    expected_sha256: str | None
    path: pathlib.Path
    device: int
    inode: int
    mode: int
    nlink: int
    owner_uid: int
    stdout_bytes: int
    stderr_bytes: int
    commands_before: int
    commands_after: int
    output_bytes_before: int
    output_bytes_after: int
    store_owner: Any = field(repr=False, compare=False)

    @property
    def git_blob_sha1(self) -> str:
        return self.blob_oid

    @property
    def command_count(self) -> int:
        return self.commands_after - self.commands_before

    @property
    def transport_bytes(self) -> int:
        return self.stdout_bytes + self.stderr_bytes


class _GitBlobFileSink:
    """Hash Git's canonical blob framing while streaming into one open file."""

    def __init__(self, handle: Any, *, expected_size: int) -> None:
        self.handle = handle
        self.expected_size = expected_size
        self.byte_length = 0
        self.git_sha1 = hashlib.sha1(
            b"blob " + str(expected_size).encode("ascii") + b"\0",
        )
        self.sha256 = hashlib.sha256()

    def write(self, chunk: bytes) -> int:
        if not isinstance(chunk, bytes) or not chunk:
            raise StateStoreError("raw Git blob stream supplied an empty or non-byte chunk")
        if self.byte_length + len(chunk) > self.expected_size:
            raise StateStoreError("raw Git blob stream exceeded its authenticated declared size")
        written = self.handle.write(chunk)
        if written != len(chunk):
            raise StateStoreError("raw Git blob temporary file accepted a partial write")
        self.byte_length += written
        self.git_sha1.update(chunk)
        self.sha256.update(chunk)
        return written

    def tell(self) -> int:
        return self.byte_length


@dataclass(frozen=True, slots=True)
class VerifiedCleanWorktree:
    """One exact clean worktree/commit/tree and its required local blobs."""

    repository: str
    root: pathlib.Path
    commit_sha: str
    tree_sha: str
    files: tuple[WorktreeBlobIdentity, ...]


@dataclass(frozen=True, slots=True)
class PublisherControlBlobMetadata:
    """One exact regular-file tree entry retained beside its blob readback."""

    path: str
    mode: str
    git_blob_sha1: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class PublisherControlClosureReadback:
    """Publisher-only fixed-order Git readback with exact file modes."""

    commit_readback: Any
    metadata: tuple[PublisherControlBlobMetadata, ...]


@dataclass(frozen=True, slots=True)
class OwnedMergeSnapshot:
    """One immutable state-branch commit and the fixed owned-C merge files."""

    state_ref_sha: str
    journal_bytes: bytes
    sidecar_bytes: bytes | None
    journal_mode: str
    sidecar_mode: str | None


class StateBudget:
    """One monotonic subprocess/deadline/output budget for a Git operation."""

    def __init__(
        self, *, timeout_seconds: float = 20.0, max_commands: int = 160,
        max_output_bytes: int = 16 * 1024 * 1024, clock: Any = time.monotonic,
        deadline_monotonic: float | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or max_commands < 1
            or max_output_bytes < 1
        ):
            raise ValueError("Git budget values must be positive")
        self.clock = clock
        now = clock()
        timeout_deadline = now + float(timeout_seconds)
        if deadline_monotonic is not None:
            if (
                isinstance(deadline_monotonic, bool)
                or not isinstance(deadline_monotonic, (int, float))
                or not math.isfinite(deadline_monotonic)
                or deadline_monotonic <= now
            ):
                raise ValueError("Git absolute deadline must be finite and in the future")
            timeout_deadline = min(timeout_deadline, float(deadline_monotonic))
        self.deadline = timeout_deadline
        self.max_commands = max_commands
        self.max_output_bytes = max_output_bytes
        self.commands = 0
        self.output_bytes = 0

    def reserve(self, *, maximum_output: int = 0) -> float:
        if self.clock() >= self.deadline:
            raise StateStoreError("git operation deadline expired before subprocess")
        if self.commands >= self.max_commands:
            raise StateStoreError("git subprocess budget exhausted")
        if maximum_output < 0 or self.output_bytes + maximum_output > self.max_output_bytes:
            raise StateStoreError("git output budget exhausted before subprocess")
        self.commands += 1
        return max(0.001, self.deadline - self.clock())

    def account_output(self, amount: int) -> None:
        if amount < 0 or self.output_bytes + amount > self.max_output_bytes:
            raise StateStoreError("git output budget exhausted")
        self.output_bytes += amount


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StateStoreError("state value is not canonical JSON") from exc


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateStoreError("state JSON contains a duplicate key")
        result[key] = value
    return result


def strict_json(raw: bytes, *, label: str, maximum: int) -> Any:
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum:
        raise StateStoreError(f"{label} bytes are outside the fixed bound")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateStoreError(f"{label} is not strict UTF-8 JSON") from exc


def _key(value: Any, label: str = "subject key") -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value) or value == "0" * 64:
        raise StateStoreError(f"{label} must be a full nonzero SHA-256")
    return value


def _safe_path(value: str) -> str:
    if (
        not isinstance(value, str) or not value or value.startswith("/")
        or "\\" in value or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise StateStoreError("Git state path is not a normalized relative POSIX path")
    return value


class GitObjectStore:
    """Process-unique bare Git object reader/writer with LFS filters disabled."""

    def __init__(
        self, repository_root: pathlib.Path, *, remote_url: str | None = None,
        token: str | None = None, timeout_seconds: float = 20.0,
        max_commands: int = 600, max_output_bytes: int = 192 * 1024 * 1024,
        max_blob_bytes: int = 8 * 1024 * 1024,
        deadline_monotonic: float | None = None,
    ) -> None:
        self.repository_root = pathlib.Path(repository_root).resolve()
        # The production runner is Linux. Never select an executable through
        # the caller's PATH: even an absolute path discovered there is not a
        # trusted pin. Resolve only the fixed system installation and verify
        # its ownership and every parent directory before invoking it.
        try:
            self.git_executable = TRUSTED_GIT_EXECUTABLE.resolve(strict=True)
            git_stat = self.git_executable.stat()
        except OSError as exc:
            raise StateStoreError("trusted Git executable cannot be inspected") from exc
        if (
            not stat.S_ISREG(git_stat.st_mode)
            or not os.access(self.git_executable, os.X_OK)
            or git_stat.st_uid != 0
            or git_stat.st_mode & 0o022
        ):
            raise StateStoreError("trusted Git executable ownership or mode is invalid")
        for parent in self.git_executable.parents:
            try:
                parent_stat = parent.stat()
            except OSError as exc:
                raise StateStoreError("trusted Git executable parent cannot be inspected") from exc
            if (
                not stat.S_ISDIR(parent_stat.st_mode)
                or parent_stat.st_uid != 0
                or parent_stat.st_mode & 0o022
            ):
                raise StateStoreError("trusted Git executable path is writable by an untrusted owner")
        self.budget = StateBudget(
            timeout_seconds=timeout_seconds, max_commands=max_commands,
            max_output_bytes=max_output_bytes,
            deadline_monotonic=deadline_monotonic,
        )
        # The default remote discovery is a normal constructor path.  It uses
        # ``_source_git`` and therefore ``_environment`` before the private
        # object store exists, so every attribute read by that bootstrap call
        # must already be initialized.
        self.token = token
        self._caller_deadline = (
            float(deadline_monotonic) if deadline_monotonic is not None else None
        )
        if remote_url is None:
            remote_url = self._source_git(("remote", "get-url", "origin"), maximum_output=8192).decode().strip()
        if not remote_url or "\n" in remote_url or "\x00" in remote_url:
            raise StateStoreError("Git remote URL is missing or malformed")
        self.remote_url = remote_url
        self.max_blob_bytes = max_blob_bytes
        self.temp_root = pathlib.Path(tempfile.mkdtemp(prefix="canonical-publication-git-"))
        os.chmod(self.temp_root, 0o700)
        self._local_materialization_read_bytes = 0
        self._owned_raw_materializations: dict[str, OwnedGitBlobMaterialization] = {}
        self._raw_blob_root = self.temp_root / "raw-blobs"
        try:
            self._raw_blob_root.mkdir(mode=0o700)
            os.chmod(self._raw_blob_root, 0o700)
        except BaseException:
            shutil.rmtree(self.temp_root, ignore_errors=True)
            raise
        temp_root_stat = self.temp_root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(temp_root_stat.st_mode)
            or temp_root_stat.st_uid != os.geteuid()
            or stat.S_IMODE(temp_root_stat.st_mode) != 0o700
        ):
            shutil.rmtree(self.temp_root, ignore_errors=True)
            raise StateStoreError("Git object store temporary root is not privately owned")
        self._temp_root_identity = (
            temp_root_stat.st_dev, temp_root_stat.st_ino,
            temp_root_stat.st_uid, stat.S_IMODE(temp_root_stat.st_mode),
        )
        raw_blob_root_stat = self._raw_blob_root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(raw_blob_root_stat.st_mode)
            or raw_blob_root_stat.st_uid != os.geteuid()
            or stat.S_IMODE(raw_blob_root_stat.st_mode) != 0o700
            or raw_blob_root_stat.st_dev != temp_root_stat.st_dev
        ):
            shutil.rmtree(self.temp_root, ignore_errors=True)
            raise StateStoreError("raw Git blob owner directory is not private")
        self._raw_blob_root_identity = (
            raw_blob_root_stat.st_dev, raw_blob_root_stat.st_ino,
            raw_blob_root_stat.st_uid, stat.S_IMODE(raw_blob_root_stat.st_mode),
        )
        self.git_dir = self.temp_root / "objects.git"
        try:
            self._run(("init", "--bare", str(self.git_dir)), git_dir=None, maximum_output=64 * 1024)
            self._run(("--git-dir", str(self.git_dir), "remote", "add", "origin", remote_url), maximum_output=64 * 1024)
            self._run(("--git-dir", str(self.git_dir), "config", "core.hooksPath", "/dev/null"), maximum_output=64 * 1024)
            self._index_path = self.temp_root / "transaction.index"
        except BaseException:
            shutil.rmtree(self.temp_root, ignore_errors=True)
            raise

    def close(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)

    @property
    def local_materialization_read_bytes(self) -> int:
        """Cumulative local revalidation bytes, separate from Git transport."""
        return self._local_materialization_read_bytes

    @contextmanager
    def bounded_deadline(self, deadline_monotonic: float):
        """Bound every nested Git subprocess by one finite caller deadline."""
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
            or deadline_monotonic <= self.budget.clock()
        ):
            raise StateStoreError("caller Git deadline is invalid or expired")
        previous = self._caller_deadline
        self._caller_deadline = (
            deadline_monotonic if previous is None
            else min(previous, deadline_monotonic)
        )
        try:
            yield self
        finally:
            self._caller_deadline = previous

    def _environment(self, *, index: pathlib.Path | None = None) -> dict[str, str]:
        # Build a fixed Linux subprocess environment. Git configuration, URL
        # rewriting, indexes, object alternates, SSH/askpass, credentials,
        # proxies, locale and helper overrides are never inherited.
        env = {
            "PATH": TRUSTED_GIT_HELPER_PATH,
            "HOME": str(getattr(self, "temp_root", pathlib.Path("/tmp"))),
            "TMPDIR": "/tmp",
            "TEMP": "/tmp",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
        env.update({
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        })
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        if self.token:
            import base64
            value = base64.b64encode(("x-access-token:" + self.token).encode()).decode("ascii")
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {value}"
        return env

    def _source_git(self, args: tuple[str, ...], *, maximum_output: int) -> bytes:
        return self._execute((str(self.git_executable), "-C", str(self.repository_root), *args), maximum_output=maximum_output)

    def _run(
        self, args: tuple[str, ...], *, git_dir: pathlib.Path | None = None,
        maximum_output: int = 1024 * 1024, input_bytes: bytes | None = None,
        allow: frozenset[int] = frozenset({0}), index: pathlib.Path | None = None,
        author: bool = False,
    ) -> bytes:
        argv = (str(self.git_executable), *( ("--git-dir", str(git_dir)) if git_dir is not None else ()), *args)
        return self._execute(
            argv, maximum_output=maximum_output, input_bytes=input_bytes, allow=allow,
            index=index, author=author,
        )

    def _open_raw_blob_root(self) -> int:
        """Open and revalidate the private materialization directory by identity."""
        try:
            root_fd = os.open(
                self._raw_blob_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(root_fd)
            named = self._raw_blob_root.stat(follow_symlinks=False)
            temp_named = self.temp_root.stat(follow_symlinks=False)
        except OSError as exc:
            try:
                os.close(root_fd)
            except (OSError, UnboundLocalError):
                pass
            raise StateStoreError("private raw Git blob directory cannot be opened safely") from exc
        opened_identity = (
            opened.st_dev, opened.st_ino, opened.st_uid, stat.S_IMODE(opened.st_mode),
        )
        named_identity = (
            named.st_dev, named.st_ino, named.st_uid, stat.S_IMODE(named.st_mode),
        )
        temp_identity = (
            temp_named.st_dev, temp_named.st_ino, temp_named.st_uid,
            stat.S_IMODE(temp_named.st_mode),
        )
        if (
            opened_identity != self._raw_blob_root_identity
            or named_identity != self._raw_blob_root_identity
            or temp_identity != self._temp_root_identity
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_nlink < 2
        ):
            os.close(root_fd)
            raise StateStoreError("private raw Git blob directory identity changed")
        return root_fd

    def _materialize_blob_to_owned_file(
        self, *, blob_oid: str, declared_size: int,
        expected_sha256: str | None = None,
    ) -> OwnedGitBlobMaterialization:
        """Stream one authenticated native blob into a private sealed file.

        Callers must first authenticate the exact tree entry and size. This
        primitive accepts only the native blob identity, never a caller path,
        command, remote, or relaxed metadata ceiling.
        """
        if not isinstance(blob_oid, str) or SHA1_RE.fullmatch(blob_oid) is None:
            raise StateStoreError("raw Git blob object ID is malformed")
        if type(declared_size) is not int or declared_size < 0:
            raise StateStoreError("raw Git blob declared size is malformed")
        if declared_size > self.budget.max_output_bytes:
            raise StateStoreError("raw Git blob exceeds the shared Git output budget")
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str)
            or SHA256_RE.fullmatch(expected_sha256) is None
        ):
            raise StateStoreError("raw Git blob expected SHA-256 is malformed")

        # Reserve the exact maximum stream plus one sentinel byte (to detect
        # overlong output) and the fixed stderr ceiling before starting Git.
        stderr_limit = 64 * 1024
        stdout_sentinel = 1
        reservation = declared_size + stdout_sentinel + stderr_limit
        if reservation > self.budget.max_output_bytes:
            raise StateStoreError("raw Git blob and bounded diagnostics cannot fit the shared output budget")
        effective_deadline = self.budget.deadline
        if self._caller_deadline is not None:
            if (
                isinstance(self._caller_deadline, bool)
                or not isinstance(self._caller_deadline, (int, float))
                or not math.isfinite(self._caller_deadline)
            ):
                raise StateStoreError("raw Git blob caller deadline is invalid")
            effective_deadline = min(effective_deadline, float(self._caller_deadline))
        if not math.isfinite(effective_deadline) or effective_deadline <= self.budget.clock():
            raise StateStoreError("raw Git blob deadline expired before materialization")
        if self.budget.commands >= self.budget.max_commands:
            raise StateStoreError("raw Git blob subprocess budget exhausted before materialization")
        if self.budget.output_bytes + reservation > self.budget.max_output_bytes:
            raise StateStoreError("raw Git blob output budget exhausted before materialization")

        root_fd = self._open_raw_blob_root()
        temporary_name = f".pending-{secrets.token_hex(20)}"
        sealed_name = f"blob-{blob_oid}-{secrets.token_hex(20)}.raw"
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        file_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            output_fd = os.open(temporary_name, file_flags, 0o600, dir_fd=root_fd)
            os.fchmod(output_fd, 0o600)
        except OSError as exc:
            try:
                os.close(output_fd)
            except (OSError, UnboundLocalError):
                pass
            try:
                os.unlink(temporary_name, dir_fd=root_fd)
            except OSError:
                pass
            os.close(root_fd)
            raise StateStoreError("raw Git blob exclusive temporary file could not be created") from exc

        sealed_path = self._raw_blob_root / sealed_name
        try:
            output_handle = os.fdopen(output_fd, "wb", closefd=True)
        except BaseException:
            try:
                os.close(output_fd)
            except OSError:
                pass
            try:
                os.unlink(temporary_name, dir_fd=root_fd)
            except OSError:
                pass
            try:
                os.fsync(root_fd)
            except OSError:
                pass
            os.close(root_fd)
            raise
        sink = _GitBlobFileSink(output_handle, expected_size=declared_size)
        commands_before = self.budget.commands
        output_before = self.budget.output_bytes
        final_link_created = False
        try:
            self._execute(
                (
                    str(self.git_executable), "--git-dir", str(self.git_dir),
                    "cat-file", "blob", blob_oid,
                ),
                maximum_output=declared_size,
                output_sink=sink,
                reserve_output_bytes=reservation,
            )
            # _execute returns only after child exit, pipe closure and confirmed
            # process-group cleanup. Do not expose a usable identity earlier.
            if sink.byte_length != declared_size:
                raise StateStoreError("raw Git blob stream was shorter than its authenticated size")
            git_blob_sha1 = sink.git_sha1.hexdigest()
            payload_sha256 = sink.sha256.hexdigest()
            if git_blob_sha1 != blob_oid:
                raise StateStoreError("raw Git blob bytes do not match the native object ID")
            if expected_sha256 is not None and payload_sha256 != expected_sha256:
                raise StateStoreError("raw Git blob bytes do not match the authenticated SHA-256")
            output_handle.flush()
            os.fchmod(output_handle.fileno(), 0o400)
            os.fsync(output_handle.fileno())
            temporary_stat = os.fstat(output_handle.fileno())
            if (
                not stat.S_ISREG(temporary_stat.st_mode)
                or temporary_stat.st_size != declared_size
                or temporary_stat.st_uid != os.geteuid()
                or stat.S_IMODE(temporary_stat.st_mode) != 0o400
                or temporary_stat.st_nlink != 1
                or temporary_stat.st_dev != self._raw_blob_root_identity[0]
            ):
                raise StateStoreError("raw Git blob temporary file identity is invalid")
            output_handle.close()
            path_stat = os.stat(temporary_name, dir_fd=root_fd, follow_symlinks=False)
            if (
                path_stat.st_dev != temporary_stat.st_dev
                or path_stat.st_ino != temporary_stat.st_ino
                or path_stat.st_size != temporary_stat.st_size
                or not stat.S_ISREG(path_stat.st_mode)
            ):
                raise StateStoreError("raw Git blob temporary path changed before sealing")

            # A random exclusive link name seals the exact inode without an
            # overwrite-capable rename. Remove the temporary link before the
            # immutable identity is returned, leaving nlink exactly one.
            os.link(
                temporary_name, sealed_name,
                src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False,
            )
            final_link_created = True
            os.unlink(temporary_name, dir_fd=root_fd)
            os.fsync(root_fd)
            sealed_stat = os.stat(sealed_name, dir_fd=root_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(sealed_stat.st_mode)
                or sealed_stat.st_dev != temporary_stat.st_dev
                or sealed_stat.st_ino != temporary_stat.st_ino
                or sealed_stat.st_size != declared_size
                or sealed_stat.st_uid != os.geteuid()
                or stat.S_IMODE(sealed_stat.st_mode) != 0o400
                or sealed_stat.st_nlink != 1
            ):
                raise StateStoreError("sealed raw Git blob identity is invalid")
            accepted_at = self.budget.clock()
            if not math.isfinite(accepted_at) or accepted_at >= effective_deadline:
                raise StateStoreError("raw Git blob deadline expired during sealing")
            output_after = self.budget.output_bytes
            materialization = OwnedGitBlobMaterialization(
                blob_oid=blob_oid,
                byte_length=declared_size,
                sha256=payload_sha256,
                expected_sha256=expected_sha256,
                path=sealed_path,
                device=sealed_stat.st_dev,
                inode=sealed_stat.st_ino,
                mode=stat.S_IMODE(sealed_stat.st_mode),
                nlink=sealed_stat.st_nlink,
                owner_uid=sealed_stat.st_uid,
                stdout_bytes=sink.byte_length,
                stderr_bytes=self.budget.output_bytes - output_before - sink.byte_length,
                commands_before=commands_before,
                commands_after=self.budget.commands,
                output_bytes_before=output_before,
                output_bytes_after=output_after,
                store_owner=self,
            )
            if sealed_name in self._owned_raw_materializations:
                raise StateStoreError("raw Git blob sealed identity collided with an existing owner record")
            self._owned_raw_materializations[sealed_name] = materialization
            return materialization
        except BaseException as exc:
            if not output_handle.closed:
                try:
                    output_handle.close()
                except OSError:
                    pass
            for name in (temporary_name, sealed_name if final_link_created else None):
                if name is None:
                    continue
                try:
                    os.unlink(name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            try:
                os.fsync(root_fd)
            except OSError:
                pass
            if isinstance(exc, OSError):
                raise StateStoreError("raw Git blob owned-file sealing failed") from exc
            raise
        finally:
            os.close(root_fd)

    def require_owned_blob_materialization(
        self, value: OwnedGitBlobMaterialization,
    ) -> OwnedGitBlobMaterialization:
        """Revalidate a sealed file and its exact owner-bound Git identity."""
        if not isinstance(value, OwnedGitBlobMaterialization):
            raise StateStoreError("raw Git blob materialization has the wrong type")
        if value.store_owner is not self:
            raise StateStoreError("raw Git blob materialization belongs to another store")
        if (
            not isinstance(value.path, pathlib.Path)
            or self._owned_raw_materializations.get(value.path.name) is not value
        ):
            raise StateStoreError("raw Git blob materialization was not issued by this store")
        if (
            not isinstance(value.blob_oid, str)
            or SHA1_RE.fullmatch(value.blob_oid) is None
            or type(value.byte_length) is not int
            or value.byte_length < 0
            or not isinstance(value.sha256, str)
            or SHA256_RE.fullmatch(value.sha256) is None
            or value.expected_sha256 is not None
            and (
                not isinstance(value.expected_sha256, str)
                or SHA256_RE.fullmatch(value.expected_sha256) is None
                or value.expected_sha256 != value.sha256
            )
        ):
            raise StateStoreError("raw Git blob materialization metadata is malformed")
        if (
            not isinstance(value.path, pathlib.Path)
            or value.path.parent != self._raw_blob_root
            or re.fullmatch(rf"blob-{value.blob_oid}-[a-f0-9]{{40}}\.raw", value.path.name) is None
        ):
            raise StateStoreError("raw Git blob materialization path is outside its owner")
        counters = (
            value.stdout_bytes, value.stderr_bytes, value.commands_before,
            value.commands_after, value.output_bytes_before, value.output_bytes_after,
        )
        if (
            any(type(counter) is not int or counter < 0 for counter in counters)
            or type(value.nlink) is not int
            or value.nlink != 1
            or value.device != self._raw_blob_root_identity[0]
            or value.owner_uid != os.geteuid()
            or value.mode != 0o400
            or value.stdout_bytes != value.byte_length
            or value.stderr_bytes > 64 * 1024
            or value.commands_after - value.commands_before != 1
            or value.output_bytes_after - value.output_bytes_before
            != value.stdout_bytes + value.stderr_bytes
        ):
            raise StateStoreError("raw Git blob materialization accounting is malformed")

        effective_deadline = self.budget.deadline
        if self._caller_deadline is not None:
            if (
                isinstance(self._caller_deadline, bool)
                or not isinstance(self._caller_deadline, (int, float))
                or not math.isfinite(self._caller_deadline)
            ):
                raise StateStoreError("raw Git blob local verification deadline is invalid")
            effective_deadline = min(effective_deadline, float(self._caller_deadline))
        if self.budget.clock() >= effective_deadline:
            raise StateStoreError("raw Git blob local verification deadline expired")
        root_fd = self._open_raw_blob_root()
        file_fd: int | None = None
        try:
            file_fd = os.open(
                value.path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=root_fd,
            )
            opened = os.fstat(file_fd)
            before = os.stat(value.path.name, dir_fd=root_fd, follow_symlinks=False)
            expected_identity = (
                value.device, value.inode, value.owner_uid, value.mode,
                value.byte_length,
            )

            def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
                return (
                    info.st_dev, info.st_ino, info.st_uid,
                    stat.S_IMODE(info.st_mode), info.st_size,
                )

            if (
                not stat.S_ISREG(opened.st_mode)
                or identity(opened) != expected_identity
                or identity(before) != expected_identity
                or opened.st_nlink < value.nlink
                or before.st_nlink < value.nlink
                or opened.st_nlink != before.st_nlink
            ):
                raise StateStoreError("raw Git blob materialization file identity changed")
            git_sha1 = hashlib.sha1(
                b"blob " + str(value.byte_length).encode("ascii") + b"\0",
            )
            payload_sha256 = hashlib.sha256()
            total = 0
            while True:
                if self.budget.clock() >= effective_deadline:
                    raise StateStoreError("raw Git blob local verification deadline expired")
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > value.byte_length:
                    raise StateStoreError("raw Git blob sealed file grew during verification")
                git_sha1.update(chunk)
                payload_sha256.update(chunk)
            after = os.fstat(file_fd)
            named_after = os.stat(value.path.name, dir_fd=root_fd, follow_symlinks=False)
            if (
                identity(after) != expected_identity
                or identity(named_after) != expected_identity
                or after.st_nlink < value.nlink
                or named_after.st_nlink < value.nlink
                or after.st_nlink != opened.st_nlink
                or named_after.st_nlink != opened.st_nlink
                or total != value.byte_length
                or git_sha1.hexdigest() != value.blob_oid
                or payload_sha256.hexdigest() != value.sha256
            ):
                raise StateStoreError("raw Git blob sealed bytes no longer match their identity")
            if value.expected_sha256 is not None and payload_sha256.hexdigest() != value.expected_sha256:
                raise StateStoreError("raw Git blob sealed bytes no longer match expected SHA-256")
            accepted_at = self.budget.clock()
            if not math.isfinite(accepted_at) or accepted_at >= effective_deadline:
                raise StateStoreError("raw Git blob local verification deadline expired")
            self._local_materialization_read_bytes += total
            return value
        except OSError as exc:
            raise StateStoreError("raw Git blob materialization could not be verified") from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(root_fd)

    def _execute(
        self, argv: tuple[str, ...], *, maximum_output: int,
        input_bytes: bytes | None = None, allow: frozenset[int] = frozenset({0}),
        index: pathlib.Path | None = None, author: bool = False,
        output_sink: _GitBlobFileSink | None = None,
        reserve_output_bytes: int | None = None,
    ) -> bytes:
        if output_sink is None and reserve_output_bytes is not None:
            raise StateStoreError("bounded Git output reservation requires a private stream sink")
        if output_sink is not None and (
            not isinstance(output_sink, _GitBlobFileSink)
            or reserve_output_bytes is None
            or maximum_output != output_sink.expected_size
            or reserve_output_bytes != maximum_output + 1 + 64 * 1024
            or input_bytes is not None
        ):
            raise StateStoreError("private Git blob stream bounds are inconsistent")
        self.budget.reserve(
            maximum_output=(maximum_output if reserve_output_bytes is None else reserve_output_bytes),
        )
        operation_deadline = self.budget.deadline
        if self._caller_deadline is not None:
            operation_deadline = min(operation_deadline, self._caller_deadline)
        started = self.budget.clock()
        if not math.isfinite(operation_deadline) or operation_deadline <= started:
            raise StateStoreError("bounded Git caller deadline expired before subprocess")
        remaining = operation_deadline - started
        cleanup_reserve = min(1.0, remaining / 4.0)
        work_deadline = operation_deadline - cleanup_reserve
        if work_deadline <= started:
            raise StateStoreError("bounded Git work deadline expired before subprocess")
        env = self._environment(index=index)
        if author:
            env.update({
                "GIT_AUTHOR_NAME": "datapan-canonical-publication[bot]",
                "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
                "GIT_COMMITTER_NAME": "datapan-canonical-publication[bot]",
                "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            })
        if self.budget.clock() >= work_deadline:
            raise StateStoreError("bounded Git work deadline expired before subprocess")
        process: subprocess.Popen[bytes] | None = None
        process_group_id: int | None = None
        selector: selectors.BaseSelector | None = None
        streams: tuple[Any, Any, Any] = (None, None, None)
        stdout = bytearray()
        stderr = bytearray()
        input_offset = 0
        returncode: int | None = None
        child_reaped = False
        primary_error: BaseException | None = None
        primary_traceback: Any = None
        cleanup_interrupt: BaseException | None = None
        cleanup_interrupt_traceback: Any = None
        cleanup_uncertainty: list[str] = []

        def note_uncertainty(note: str) -> None:
            if note not in cleanup_uncertainty and len(cleanup_uncertainty) < 8:
                cleanup_uncertainty.append(note)

        def note_cleanup_interrupt(exc: BaseException) -> None:
            nonlocal cleanup_interrupt, cleanup_interrupt_traceback
            if not isinstance(exc, Exception) and cleanup_interrupt is None:
                cleanup_interrupt = exc
                cleanup_interrupt_traceback = exc.__traceback__
                note_uncertainty("subprocess cleanup was interrupted")

        def cleanup() -> None:
            nonlocal child_reaped
            if process_group_id is not None:
                try:
                    def group_alive() -> bool:
                        try:
                            os.killpg(process_group_id, 0)
                            return True
                        except ProcessLookupError:
                            return False
                        except BaseException as exc:
                            note_cleanup_interrupt(exc)
                            note_uncertainty("subprocess process-group state could not be confirmed")
                            return True

                    alive = group_alive()
                    if alive:
                        try:
                            os.killpg(process_group_id, signal.SIGTERM)
                        except ProcessLookupError:
                            alive = False
                        except BaseException as exc:
                            note_cleanup_interrupt(exc)
                            note_uncertainty("subprocess process-group termination could not be confirmed")
                        grace_started = time.monotonic()

                        # Reap the direct group leader before treating
                        # killpg(..., 0) as proof that the session is gone.
                        # A killed but unreaped leader remains a zombie and
                        # otherwise makes that probe report a live group for
                        # the entire remaining invocation deadline.
                        if process is not None and not child_reaped:
                            try:
                                cleanup_now = self.budget.clock()
                            except BaseException as exc:
                                note_cleanup_interrupt(exc)
                                cleanup_now = operation_deadline
                            reap_timeout = min(
                                0.05,
                                max(0.0, operation_deadline - cleanup_now),
                                max(0.0, grace_started + 0.1 - time.monotonic()),
                            )
                            try:
                                process.wait(timeout=reap_timeout)
                                child_reaped = True
                            except subprocess.TimeoutExpired:
                                pass
                            except BaseException as exc:
                                note_cleanup_interrupt(exc)
                            if not child_reaped:
                                try:
                                    child_reaped = process.poll() is not None
                                except BaseException as exc:
                                    note_cleanup_interrupt(exc)
                        while alive:
                            try:
                                cleanup_now = self.budget.clock()
                            except BaseException as exc:
                                note_cleanup_interrupt(exc)
                                cleanup_now = operation_deadline
                            if (
                                cleanup_now >= operation_deadline
                                or time.monotonic() >= grace_started + 0.1
                            ):
                                break
                            try:
                                time.sleep(min(0.01, max(0.0, grace_started + 0.1 - time.monotonic())))
                            except BaseException as exc:
                                note_cleanup_interrupt(exc)
                                note_uncertainty("subprocess process-group grace wait was interrupted")
                                break
                            alive = group_alive()
                        if alive:
                            try:
                                os.killpg(process_group_id, signal.SIGKILL)
                            except ProcessLookupError:
                                alive = False
                            except BaseException as exc:
                                note_cleanup_interrupt(exc)
                                note_uncertainty("subprocess process-group kill could not be confirmed")
                        if process is not None and not child_reaped:
                            try:
                                cleanup_now = self.budget.clock()
                            except BaseException as exc:
                                note_cleanup_interrupt(exc)
                                cleanup_now = operation_deadline
                            try:
                                process.wait(
                                    timeout=max(0.0, operation_deadline - cleanup_now),
                                )
                                child_reaped = True
                            except BaseException as exc:
                                note_cleanup_interrupt(exc)
                            if not child_reaped:
                                try:
                                    child_reaped = process.poll() is not None
                                except BaseException as exc:
                                    note_cleanup_interrupt(exc)
                        try:
                            cleanup_now = self.budget.clock()
                        except BaseException as exc:
                            note_cleanup_interrupt(exc)
                            cleanup_now = operation_deadline
                        confirmation_wall_deadline = time.monotonic() + max(
                            0.0, operation_deadline - cleanup_now,
                        )
                        while alive and time.monotonic() < confirmation_wall_deadline:
                            try:
                                time.sleep(min(0.01, max(0.0, confirmation_wall_deadline - time.monotonic())))
                            except BaseException as exc:
                                note_cleanup_interrupt(exc)
                                note_uncertainty("subprocess process-group confirmation was interrupted")
                                break
                            alive = group_alive()
                        if alive:
                            note_uncertainty("subprocess process-group reap could not be confirmed")
                except BaseException as exc:
                    note_cleanup_interrupt(exc)
                    note_uncertainty("subprocess process-group cleanup failed")

            if process is not None and not child_reaped:
                observed: int | None = None
                try:
                    observed = process.poll()
                except BaseException as exc:
                    note_cleanup_interrupt(exc)
                if observed is not None:
                    child_reaped = True
                if not child_reaped:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        # A concurrent exit is resolved by the bounded wait below.
                        pass
                    except BaseException as exc:
                        note_cleanup_interrupt(exc)
                    try:
                        try:
                            cleanup_now = self.budget.clock()
                        except BaseException as exc:
                            note_cleanup_interrupt(exc)
                            cleanup_now = operation_deadline
                        wait_timeout = max(0.0, operation_deadline - cleanup_now)
                        process.wait(timeout=wait_timeout)
                        child_reaped = True
                    except BaseException as exc:
                        note_cleanup_interrupt(exc)
                    if not child_reaped:
                        try:
                            observed = process.poll()
                        except BaseException as exc:
                            note_cleanup_interrupt(exc)
                        else:
                            child_reaped = observed is not None
                if not child_reaped:
                    note_uncertainty("direct child reap could not be confirmed")

            if selector is not None:
                try:
                    selector.close()
                except BaseException as exc:
                    note_cleanup_interrupt(exc)
                    note_uncertainty("subprocess selector close could not be confirmed")
            for label, stream in zip(("stdin", "stdout", "stderr"), streams):
                if stream is None:
                    continue
                try:
                    stream.close()
                except BaseException as exc:
                    note_cleanup_interrupt(exc)
                    note_uncertainty(f"subprocess {label} pipe close could not be confirmed")

        def attach_cleanup_notes(exc: BaseException) -> None:
            add_note = getattr(exc, "add_note", None)
            if add_note is None:
                return
            for note in cleanup_uncertainty[:8]:
                try:
                    add_note(note)
                except BaseException:
                    break

        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                start_new_session=True,
            )
            process_group_id = process.pid
            # Retain every Popen stream before selector construction or any
            # operation that can fail and transfer control to cleanup.
            streams = (process.stdin, process.stdout, process.stderr)
            selector = selectors.DefaultSelector()
            assert process.stdout is not None and process.stderr is not None
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            if input_bytes is not None:
                assert process.stdin is not None
                os.set_blocking(process.stdin.fileno(), False)
                if input_bytes:
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            while selector.get_map():
                remaining = work_deadline - self.budget.clock()
                if remaining <= 0:
                    raise StateStoreError("bounded Git subprocess deadline expired")
                events = selector.select(timeout=min(0.1, remaining))
                if not events and process.poll() is not None:
                    # Drain EOF notifications before leaving the loop.
                    continue
                for key, _mask in events:
                    stream = key.fileobj
                    kind = key.data
                    if kind == "stdin":
                        assert input_bytes is not None
                        try:
                            written = os.write(stream.fileno(), input_bytes[input_offset:input_offset + 64 * 1024])
                        except BrokenPipeError:
                            written = 0
                            input_offset = len(input_bytes)
                        input_offset += written
                        if input_offset >= len(input_bytes):
                            selector.unregister(stream)
                            stream.close()
                        continue
                    if output_sink is not None:
                        if kind == "stdout":
                            read_limit = min(
                                64 * 1024,
                                max(1, output_sink.expected_size - output_sink.byte_length + 1),
                            )
                        else:
                            read_limit = min(64 * 1024, max(1, 64 * 1024 - len(stderr) + 1))
                    else:
                        read_limit = 64 * 1024
                    chunk = os.read(stream.fileno(), read_limit)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    target = stdout if kind == "stdout" else stderr
                    limit = maximum_output if kind == "stdout" else 64 * 1024
                    if output_sink is not None:
                        self.budget.account_output(len(chunk))
                        if len(target) + len(chunk) > limit:
                            raise StateStoreError("Git subprocess output exceeded its fixed bound")
                        if kind == "stdout":
                            output_sink.write(chunk)
                        else:
                            target.extend(chunk)
                        continue
                    if len(target) + len(chunk) > limit:
                        raise StateStoreError("Git subprocess output exceeded its fixed bound")
                    target.extend(chunk)
            wait_remaining = max(0.0, work_deadline - self.budget.clock())
            returncode = process.wait(timeout=wait_remaining)
            child_reaped = True
        except BaseException as exc:
            primary_error = exc
            primary_traceback = exc.__traceback__
        finally:
            cleanup()

        if primary_error is not None:
            if cleanup_uncertainty and isinstance(primary_error, Exception):
                error = StateCleanupUncertain(
                    "bounded Git subprocess cleanup could not be confirmed",
                    command_returncode=returncode,
                )
                attach_cleanup_notes(error)
                raise error from primary_error
            if isinstance(primary_error, (OSError, subprocess.TimeoutExpired)):
                translated = StateStoreError("bounded Git subprocess failed or timed out")
                attach_cleanup_notes(translated)
                raise translated from primary_error
            attach_cleanup_notes(primary_error)
            raise primary_error.with_traceback(primary_traceback)
        if cleanup_interrupt is not None:
            attach_cleanup_notes(cleanup_interrupt)
            raise cleanup_interrupt.with_traceback(cleanup_interrupt_traceback)
        if cleanup_uncertainty:
            error = StateCleanupUncertain(
                "bounded Git subprocess cleanup could not be confirmed",
                command_returncode=returncode,
            )
            attach_cleanup_notes(error)
            raise error
        if returncode is None:
            raise StateStoreError("bounded Git subprocess returned without a confirmed exit")
        if output_sink is None:
            self.budget.account_output(len(stdout))
            self.budget.account_output(len(stderr))
        accepted_at = self.budget.clock()
        if not math.isfinite(accepted_at) or accepted_at >= operation_deadline:
            raise StateStoreError("bounded Git subprocess deadline expired during cleanup")
        if returncode not in allow:
            # stderr is intentionally omitted: it can include remote URLs or
            # credential-helper details supplied by the host environment.
            raise StateStoreError(f"Git subprocess exited {returncode}")
        self.last_returncode = returncode
        return b"" if output_sink is not None else bytes(stdout)

    def _object_exists(self, commit_sha: str) -> bool:
        # cat-file -e has no stdout; a second type read gives an unambiguous result.
        kind = self._run(
            ("--git-dir", str(self.git_dir), "cat-file", "-t", commit_sha),
            maximum_output=128, allow=frozenset({0, 1, 128}),
        )
        return kind.strip() == b"commit"

    def ensure_commit(self, commit_sha: str) -> None:
        if self._object_exists(commit_sha):
            return
        self._run(
            ("--git-dir", str(self.git_dir), "fetch", "--no-tags", "--no-recurse-submodules", "origin", commit_sha),
            maximum_output=128 * 1024,
        )
        if not self._object_exists(commit_sha):
            raise StateStoreError("requested commit was not read back as a native Git commit")

    def remote_sha(self, ref: str = STATE_REF) -> str | None:
        if ref != STATE_REF:
            raise StateStoreError("only the reviewed canonical state ref can be read")
        raw = self._run(
            ("--git-dir", str(self.git_dir), "ls-remote", "--heads", "origin", ref),
            maximum_output=16 * 1024,
        )
        rows = [line for line in raw.decode("ascii", "strict").splitlines() if line.endswith("\t" + ref)]
        if len(rows) > 1:
            raise StateStoreError("state branch returned duplicate native refs")
        if not rows:
            return None
        sha, _tab, _name = rows[0].partition("\t")
        if not SHA1_RE.fullmatch(sha) or sha == "0" * 40:
            raise StateStoreError("state branch returned a malformed Git SHA")
        self.ensure_commit(sha)
        return sha

    def fetch_state_base(self, base_sha: str) -> str:
        if not SHA1_RE.fullmatch(base_sha) or base_sha == "0" * 40:
            raise StateStoreError("state branch base commit is malformed")
        self.ensure_commit(base_sha)
        return base_sha

    def tree_sha(self, commit_sha: str, *, deadline_monotonic: float) -> str:
        with self.bounded_deadline(deadline_monotonic):
            if not SHA1_RE.fullmatch(commit_sha) or commit_sha == "0" * 40:
                raise StateStoreError("tree query contains a malformed commit SHA")
            self.ensure_commit(commit_sha)
            value = self._run(
                ("--git-dir", str(self.git_dir), "rev-parse", f"{commit_sha}^{{tree}}"),
                maximum_output=128,
            ).decode("ascii", "strict").strip()
            if not SHA1_RE.fullmatch(value) or value == "0" * 40:
                raise StateStoreError("tree query returned a malformed tree SHA")
            return value

    def verify_clean_worktree(
        self, worktree_root: pathlib.Path, *, commit_sha: str,
        required_paths: Sequence[str], deadline_monotonic: float,
    ) -> VerifiedCleanWorktree:
        """Authenticate one clean local DATA worktree without executing it."""
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
            or deadline_monotonic <= self.budget.clock()
        ):
            raise StateStoreError("DATA worktree deadline is invalid or expired")
        root = pathlib.Path(worktree_root)
        try:
            if root.is_symlink() or not root.is_dir():
                raise StateStoreError("DATA worktree root is unavailable or symbolic")
            root = root.resolve(strict=True)
        except OSError as exc:
            raise StateStoreError("DATA worktree root cannot be resolved") from exc
        if not SHA1_RE.fullmatch(commit_sha) or commit_sha == "0" * 40:
            raise StateStoreError("DATA worktree commit SHA is malformed")
        if (
            not isinstance(required_paths, Sequence)
            or isinstance(required_paths, (str, bytes))
            or not 1 <= len(required_paths) <= 8
        ):
            raise StateStoreError("DATA worktree required paths exceed their closed bound")
        paths = tuple(_safe_path(path) for path in required_paths)
        if len(set(paths)) != len(paths):
            raise StateStoreError("DATA worktree required paths contain duplicates")

        with self.bounded_deadline(deadline_monotonic):
            top = self._execute(
                (str(self.git_executable), "-C", str(root), "rev-parse", "--show-toplevel"),
                maximum_output=16 * 1024,
            ).decode("utf-8", "strict").strip()
            try:
                top_root = pathlib.Path(top).resolve(strict=True)
            except OSError as exc:
                raise StateStoreError("DATA worktree top-level path cannot be resolved") from exc
            head = self._execute(
                (str(self.git_executable), "-C", str(root), "rev-parse", "HEAD^{commit}"),
                maximum_output=128,
            ).decode("ascii", "strict").strip()
            tree = self._execute(
                (str(self.git_executable), "-C", str(root), "rev-parse", "HEAD^{tree}"),
                maximum_output=128,
            ).decode("ascii", "strict").strip()
            dirty = self._execute(
                (
                    str(self.git_executable), "-C", str(root),
                    "-c", "filter.lfs.process=", "-c", "filter.lfs.clean=",
                    "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false",
                    "status", "--porcelain=v1", "-z", "--untracked-files=all",
                    "--ignored=matching",
                ),
                maximum_output=2 * 1024 * 1024,
            )
            if (
                top_root != root or head != commit_sha or dirty
                or not SHA1_RE.fullmatch(tree) or tree == "0" * 40
            ):
                raise StateStoreError("DATA worktree root/HEAD/tree/cleanliness is not exact")
            self.ensure_commit(commit_sha)
            expected_tree = self.tree_sha(commit_sha, deadline_monotonic=deadline_monotonic)
            if tree != expected_tree:
                raise StateStoreError("DATA worktree tree differs from authenticated Git objects")

            files: list[WorktreeBlobIdentity] = []
            for path in paths:
                expected = self.read_file(commit_sha, path, maximum=self.max_blob_bytes)
                candidate = root.joinpath(*pathlib.PurePosixPath(path).parts)
                try:
                    fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as handle:
                        info = os.fstat(handle.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_blob_bytes:
                            raise StateStoreError("DATA worktree required path is not one bounded regular file")
                        output = bytearray()
                        while len(output) <= self.max_blob_bytes:
                            if self.budget.clock() >= deadline_monotonic:
                                raise StateStoreError("DATA worktree file read deadline expired")
                            chunk = handle.read(min(64 * 1024, self.max_blob_bytes + 1 - len(output)))
                            if not chunk:
                                break
                            output.extend(chunk)
                except OSError as exc:
                    raise StateStoreError("DATA worktree required path cannot be read safely") from exc
                raw = bytes(output)
                if raw != expected:
                    raise StateStoreError("DATA worktree required path differs from its exact Git blob")
                files.append(WorktreeBlobIdentity(
                    path=path, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                ))
        return VerifiedCleanWorktree(
            repository=REPOSITORY, root=root, commit_sha=commit_sha,
            tree_sha=tree, files=tuple(files),
        )

    def is_ancestor(
        self, ancestor_sha: str, descendant_sha: str, *,
        deadline_monotonic: float | None = None,
    ) -> bool:
        if deadline_monotonic is not None:
            with self.bounded_deadline(deadline_monotonic):
                return self.is_ancestor(ancestor_sha, descendant_sha)
        if not SHA1_RE.fullmatch(ancestor_sha) or not SHA1_RE.fullmatch(descendant_sha):
            raise StateStoreError("ancestry query contains a malformed commit SHA")
        self.ensure_commit(ancestor_sha)
        self.ensure_commit(descendant_sha)
        self._run(
            ("--git-dir", str(self.git_dir), "merge-base", "--is-ancestor", ancestor_sha, descendant_sha),
            maximum_output=4096, allow=frozenset({0, 1}),
        )
        return getattr(self, "last_returncode", 1) == 0

    def changed_paths(
        self, base_sha: str, head_sha: str, *, deadline_monotonic: float,
    ) -> tuple[str, ...]:
        """Return one bounded, exact NUL-delimited Git path delta."""
        with self.bounded_deadline(deadline_monotonic):
            if (
                not SHA1_RE.fullmatch(base_sha) or base_sha == "0" * 40
                or not SHA1_RE.fullmatch(head_sha) or head_sha == "0" * 40
            ):
                raise StateStoreError("changed-path query contains a malformed commit SHA")
            self.ensure_commit(base_sha)
            self.ensure_commit(head_sha)
            raw = self._run(
                (
                    "--git-dir", str(self.git_dir), "diff", "--name-only", "-z",
                    "--no-renames", base_sha, head_sha, "--",
                ),
                maximum_output=MAX_CHANGED_PATHS_BYTES,
            )
        values: list[str] = []
        seen: set[str] = set()
        for raw_path in (item for item in raw.split(b"\0") if item):
            try:
                path = _safe_path(raw_path.decode("utf-8", "strict"))
            except UnicodeError as exc:
                raise StateStoreError("changed-path query returned a non-UTF-8 path") from exc
            if path in seen:
                raise StateStoreError("changed-path query returned a duplicate path")
            seen.add(path)
            values.append(path)
            if len(values) > MAX_CHANGED_PATHS:
                raise StateStoreError("changed-path query exceeds its fixed path bound")
        return tuple(values)

    def _tree_entry(self, commit_sha: str, path: str) -> tuple[str, str, int] | None:
        path = _safe_path(path)
        raw = self._run(
            ("--git-dir", str(self.git_dir), "ls-tree", "-r", "-z", commit_sha, "--", path),
            maximum_output=256 * 1024,
        )
        if not raw:
            return None
        rows = raw.split(b"\0")
        rows = [row for row in rows if row]
        if len(rows) != 1:
            raise StateStoreError("Git path resolved to missing or ambiguous tree entries")
        meta, separator, raw_path = rows[0].partition(b"\t")
        if not separator or raw_path.decode("utf-8", "strict") != path:
            raise StateStoreError("Git tree returned a different path than requested")
        parts = meta.decode("ascii", "strict").split()
        if len(parts) != 3:
            raise StateStoreError("Git tree entry metadata is malformed")
        mode, kind, oid = parts
        if kind != "blob" or mode not in {"100644", "100755"} or not SHA1_RE.fullmatch(oid):
            raise StateStoreError("Git state path is not a regular file blob")
        size_raw = self._run(
            ("--git-dir", str(self.git_dir), "cat-file", "-s", oid), maximum_output=128,
        )
        try:
            size = int(size_raw)
        except ValueError as exc:
            raise StateStoreError("Git blob size is malformed") from exc
        if size < 0:
            raise StateStoreError("Git blob size is negative")
        return oid, mode, size

    def read_python_module_index(
        self, commit_sha: str, *, deadline_monotonic: float,
    ) -> PythonModuleIndex:
        """List the bounded repository-root/scripts Python namespace at one commit."""
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
            or deadline_monotonic <= self.budget.clock()
        ):
            raise StateStoreError("Python module index deadline expired")
        with self.bounded_deadline(deadline_monotonic):
            if not SHA1_RE.fullmatch(commit_sha) or commit_sha == "0" * 40:
                raise StateStoreError("Python module index commit is malformed")
            self.ensure_commit(commit_sha)
            if self.budget.clock() >= deadline_monotonic:
                raise StateStoreError("Python module index deadline expired")
            tree_sha = self._run(
                ("--git-dir", str(self.git_dir), "rev-parse", f"{commit_sha}^{{tree}}"),
                maximum_output=128,
            ).decode("ascii", "strict").strip()
            if not SHA1_RE.fullmatch(tree_sha) or tree_sha == "0" * 40:
                raise StateStoreError("Python module index tree SHA is malformed")
            if self.budget.clock() >= deadline_monotonic:
                raise StateStoreError("Python module index deadline expired")
            raw = self._run(
                ("--git-dir", str(self.git_dir), "ls-tree", "-r", "-z", commit_sha),
                maximum_output=MAX_PYTHON_MODULE_INDEX_BYTES,
            )
        if self.budget.clock() >= deadline_monotonic:
            raise StateStoreError("Python module index deadline expired")
        rows = [row for row in raw.split(b"\0") if row]
        if len(rows) > MAX_PYTHON_MODULE_INDEX_ENTRIES:
            raise StateStoreError("Git tree exceeds the Python module index entry bound")
        selected: dict[str, PythonModuleIndexEntry] = {}
        module_names: dict[str, str] = {}
        for row in rows:
            meta, separator, raw_path = row.partition(b"\t")
            if not separator:
                raise StateStoreError("Python module index tree entry is malformed")
            try:
                path = _safe_path(raw_path.decode("utf-8", "strict"))
                parts = meta.decode("ascii", "strict").split()
            except UnicodeError as exc:
                raise StateStoreError("Python module index path or metadata is not UTF-8/ASCII") from exc
            if len(parts) != 3:
                raise StateStoreError("Python module index tree metadata is malformed")
            mode, kind, oid = parts
            root_python = "/" not in path and path.endswith(".py")
            scripts_python = path.startswith("scripts/") and path.endswith(".py")
            if not (root_python or scripts_python):
                continue
            if path in selected:
                raise StateStoreError("Python module index contains a duplicate path")
            if kind != "blob" or mode not in {"100644", "100755"} or not SHA1_RE.fullmatch(oid):
                raise StateStoreError("Python module index contains a symlink, submodule, or invalid blob")
            entry = PythonModuleIndexEntry(path=path, mode=mode, blob_oid=oid)
            selected[path] = entry

            relative = path[:-3]
            components = relative.split("/")
            aliases: set[str] = set()
            if all(component.isidentifier() for component in components):
                aliases.add(".".join(components))
                if path.startswith("scripts/") and len(components) > 1:
                    aliases.add(".".join(components[1:]))
            if components[-1] == "__init__" and all(
                component.isidentifier() for component in components[:-1]
            ):
                aliases.add(".".join(components[:-1]))
                if path.startswith("scripts/") and len(components) > 2:
                    aliases.add(".".join(components[1:-1]))
            for alias in aliases:
                previous = module_names.get(alias)
                if previous is not None and previous != path:
                    raise StateStoreError(
                        "Python module index contains a local module-shadow ambiguity",
                    )
                module_names[alias] = path
        if len(selected) > MAX_PYTHON_MODULE_INDEX_ENTRIES:
            raise StateStoreError("Python module index exceeds its selected entry bound")
        return PythonModuleIndex(
            repository=REPOSITORY,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            entries=tuple(selected[path] for path in sorted(selected)),
        )

    def read_file(self, commit_sha: str, path: str, *, maximum: int) -> bytes:
        self.ensure_commit(commit_sha)
        entry = self._tree_entry(commit_sha, path)
        if entry is None:
            raise StateStoreError("requested Git path is absent at the exact commit")
        oid, _mode, size = entry
        if size > maximum:
            raise StateStoreError("Git blob exceeds its reviewed metadata byte bound")
        raw = self._run(("--git-dir", str(self.git_dir), "cat-file", "blob", oid), maximum_output=maximum + 1)
        if len(raw) != size:
            raise StateStoreError("Git blob bytes differ from exact native object size")
        expected_oid = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        if expected_oid != oid:
            raise StateStoreError("Git blob content does not match its native object ID")
        return raw

    def read_owned_merge_snapshot(
        self, *, deadline_monotonic: float, max_journal_bytes: int,
        max_sidecar_bytes: int,
    ) -> OwnedMergeSnapshot:
        """Read the fixed journal and owned-C sidecar at one exact state commit."""
        if (
            type(max_journal_bytes) is not int
            or not 1 <= max_journal_bytes <= MAX_OWNED_MERGE_JOURNAL_BYTES
            or type(max_sidecar_bytes) is not int
            or not 1 <= max_sidecar_bytes <= MAX_OWNED_MERGE_SIDECAR_BYTES
        ):
            raise StateStoreError("owned-C state file bound is outside its fixed cap")
        with self.bounded_deadline(deadline_monotonic):
            state_ref_sha = self.remote_sha(STATE_REF)
            if state_ref_sha is None:
                raise StateStoreError("owned-C state branch is absent")
            journal_entry = self._tree_entry(state_ref_sha, OWNED_MERGE_JOURNAL_PATH)
            if journal_entry is None:
                raise StateStoreError("owned-C promotion journal is absent")
            _journal_oid, journal_mode, journal_size = journal_entry
            if journal_size > max_journal_bytes:
                raise StateStoreError("owned-C promotion journal exceeds its fixed byte bound")
            journal_bytes = self.read_file(
                state_ref_sha, OWNED_MERGE_JOURNAL_PATH, maximum=max_journal_bytes,
            )

            sidecar_entry = self._tree_entry(state_ref_sha, OWNED_MERGE_SIDECAR_PATH)
            if sidecar_entry is None:
                sidecar_bytes = None
                sidecar_mode = None
            else:
                _sidecar_oid, sidecar_mode, sidecar_size = sidecar_entry
                if sidecar_size > max_sidecar_bytes:
                    raise StateStoreError("owned-C sidecar exceeds its fixed byte bound")
                sidecar_bytes = self.read_file(
                    state_ref_sha, OWNED_MERGE_SIDECAR_PATH, maximum=max_sidecar_bytes,
                )
            return OwnedMergeSnapshot(
                state_ref_sha=state_ref_sha,
                journal_bytes=journal_bytes,
                sidecar_bytes=sidecar_bytes,
                journal_mode=journal_mode,
                sidecar_mode=sidecar_mode,
            )

    def compare_and_swap_owned_merge_sidecar(
        self, *, expected_state_ref_sha: str, expected_journal_sha256: str,
        expected_sidecar_sha256: str, new_sidecar_bytes: bytes,
        deadline_monotonic: float,
    ) -> None:
        """CAS only the fixed sidecar, preserving the exact journal and tree context."""
        if (
            not SHA1_RE.fullmatch(expected_state_ref_sha)
            or expected_state_ref_sha == "0" * 40
            or not SHA256_RE.fullmatch(expected_journal_sha256)
            or not SHA256_RE.fullmatch(expected_sidecar_sha256)
        ):
            raise StateStoreError("owned-C CAS precondition digest is malformed")
        if (
            not isinstance(new_sidecar_bytes, bytes) or not new_sidecar_bytes
            or len(new_sidecar_bytes) > MAX_OWNED_MERGE_SIDECAR_BYTES
        ):
            raise StateStoreError("owned-C sidecar update is outside its fixed byte bound")
        with self.bounded_deadline(deadline_monotonic):
            before = self.read_owned_merge_snapshot(
                deadline_monotonic=deadline_monotonic,
                max_journal_bytes=MAX_OWNED_MERGE_JOURNAL_BYTES,
                max_sidecar_bytes=MAX_OWNED_MERGE_SIDECAR_BYTES,
            )
            current_sidecar = (
                OWNED_MERGE_EMPTY_SIDECAR_BYTES
                if before.sidecar_bytes is None else before.sidecar_bytes
            )
            if before.state_ref_sha != expected_state_ref_sha:
                raise StateConflict("owned-C state ref differs from the exact snapshot")
            if hashlib.sha256(before.journal_bytes).hexdigest() != expected_journal_sha256:
                raise StateConflict("owned-C journal differs from the exact snapshot")
            if hashlib.sha256(current_sidecar).hexdigest() != expected_sidecar_sha256:
                raise StateConflict("owned-C sidecar differs from the exact snapshot")
            try:
                import canonical_owned_merge_state as owned_merge_state
            except ImportError as exc:
                raise StateStoreError("owned-C sidecar policy module is unavailable") from exc
            try:
                previous_sidecar = owned_merge_state.parse_sidecar(current_sidecar)
                following_sidecar = owned_merge_state.parse_sidecar(new_sidecar_bytes)
                owned_merge_state.validate_sidecar_successor(
                    current_sidecar,
                    new_sidecar_bytes,
                    expected_state_ref_sha=expected_state_ref_sha,
                )
            except owned_merge_state.StateStoreError as exc:
                raise StateStoreError(f"owned-C sidecar successor rejected: {exc.code}") from exc
            if (
                len(following_sidecar["records"]) == len(previous_sidecar["records"]) + 1
                and following_sidecar["records"][-1]["journal_sha256"] != expected_journal_sha256
            ):
                raise StateStoreError("new owned-C candidate must bind the current journal digest")

            child_sha = self._commit_updates(
                expected_sha=expected_state_ref_sha,
                base_sha=expected_state_ref_sha,
                updates={OWNED_MERGE_SIDECAR_PATH: new_sidecar_bytes},
                message="canonical owned-C merge sidecar",
                update_modes={
                    OWNED_MERGE_SIDECAR_PATH: before.sidecar_mode or "100644",
                },
            )
            parent_line = self._run(
                ("--git-dir", str(self.git_dir), "rev-list", "--parents", "-n", "1", child_sha),
                maximum_output=256,
            ).decode("ascii", "strict").strip().split()
            if parent_line != [child_sha, expected_state_ref_sha]:
                raise StateOutcomeUncertain("owned-C sidecar child parent readback differs")
            changed = self.changed_paths(
                expected_state_ref_sha, child_sha, deadline_monotonic=deadline_monotonic,
            )
            if changed != (OWNED_MERGE_SIDECAR_PATH,):
                raise StateOutcomeUncertain("owned-C sidecar commit changed an unrelated path")
            after_journal_entry = self._tree_entry(child_sha, OWNED_MERGE_JOURNAL_PATH)
            after_sidecar_entry = self._tree_entry(child_sha, OWNED_MERGE_SIDECAR_PATH)
            if after_journal_entry is None or after_sidecar_entry is None:
                raise StateOutcomeUncertain("owned-C sidecar child is missing a fixed state file")
            _after_journal_oid, after_journal_mode, after_journal_size = after_journal_entry
            _after_sidecar_oid, after_sidecar_mode, after_sidecar_size = after_sidecar_entry
            if (
                after_journal_size > MAX_OWNED_MERGE_JOURNAL_BYTES
                or after_sidecar_size > MAX_OWNED_MERGE_SIDECAR_BYTES
            ):
                raise StateOutcomeUncertain("owned-C sidecar child exceeds a fixed state file bound")
            after_journal_bytes = self.read_file(
                child_sha, OWNED_MERGE_JOURNAL_PATH,
                maximum=MAX_OWNED_MERGE_JOURNAL_BYTES,
            )
            after_sidecar_bytes = self.read_file(
                child_sha, OWNED_MERGE_SIDECAR_PATH,
                maximum=MAX_OWNED_MERGE_SIDECAR_BYTES,
            )
            if (
                after_journal_bytes != before.journal_bytes
                or after_journal_mode != before.journal_mode
                or after_sidecar_bytes != new_sidecar_bytes
                or after_sidecar_mode != (before.sidecar_mode or "100644")
            ):
                raise StateOutcomeUncertain("owned-C sidecar immutable readback differs")

    def read_ref_file(self, commit_sha: str, path: str, *, maximum: int) -> bytes:
        return self.read_file(commit_sha, path, maximum=maximum)

    def read_commit_blobs(
        self, commit_sha: str, paths: tuple[str, ...], *, maximum_blob_bytes_each: int,
    ) -> Any:
        from canonical_publication_authority import (
            GIT_BLOB_READBACK_SCHEMA, GIT_COMMIT_READBACK_SCHEMA,
            GitBlobReadback, GitCommitReadback, REPOSITORY as AUTHORITY_REPOSITORY,
        )
        self.ensure_commit(commit_sha)
        tree_sha = self._run(
            ("--git-dir", str(self.git_dir), "rev-parse", f"{commit_sha}^{{tree}}"), maximum_output=128,
        ).decode("ascii").strip()
        if not SHA1_RE.fullmatch(tree_sha):
            raise StateStoreError("commit tree SHA is malformed")
        files = {}
        for path in paths:
            content = self.read_file(commit_sha, path, maximum=maximum_blob_bytes_each)
            entry = self._tree_entry(commit_sha, path)
            assert entry is not None
            oid = entry[0]
            files[path] = GitBlobReadback(
                schema_version=GIT_BLOB_READBACK_SCHEMA,
                repository=AUTHORITY_REPOSITORY,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                path=path,
                git_blob_sha1=oid,
                byte_length=len(content),
                content=content,
            )
        return GitCommitReadback(
            schema_version=GIT_COMMIT_READBACK_SCHEMA,
            repository=AUTHORITY_REPOSITORY,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            files=files,
        )

    def read_publisher_control_closure(
        self, commit_sha: str, paths: tuple[str, ...], *, maximum_total_bytes: int,
    ) -> PublisherControlClosureReadback:
        """Read one exact publisher CONTROL tree through two bounded Git batches."""
        from canonical_publication_authority import (
            GIT_BLOB_READBACK_SCHEMA, GIT_COMMIT_READBACK_SCHEMA,
            GitBlobReadback, GitCommitReadback, REPOSITORY as AUTHORITY_REPOSITORY,
        )

        operation_deadline = self.budget.deadline
        if self._caller_deadline is not None:
            if (
                isinstance(self._caller_deadline, bool)
                or not isinstance(self._caller_deadline, (int, float))
                or not math.isfinite(self._caller_deadline)
            ):
                raise StateStoreError("publisher CONTROL Git deadline is invalid or expired")
            operation_deadline = min(operation_deadline, self._caller_deadline)
        if (
            not math.isfinite(operation_deadline)
            or operation_deadline <= self.budget.clock()
        ):
            raise StateStoreError("publisher CONTROL Git deadline is invalid or expired")
        if not SHA1_RE.fullmatch(commit_sha) or commit_sha == "0" * 40:
            raise StateStoreError("publisher CONTROL commit SHA is malformed")
        if (
            type(paths) is not tuple
            or not 1 <= len(paths) <= MAX_PUBLISHER_CONTROL_FILES
        ):
            raise StateStoreError("publisher CONTROL path list is empty or over bound")
        if (
            isinstance(maximum_total_bytes, bool)
            or not isinstance(maximum_total_bytes, int)
            or not 1 <= maximum_total_bytes <= MAX_PUBLISHER_CONTROL_TOTAL_BYTES
        ):
            raise StateStoreError("publisher CONTROL aggregate allowance is invalid")
        normalized_paths: list[str] = []
        for raw_path in paths:
            path = _safe_path(raw_path)
            if any(character in path for character in ("\r", "\n", "\t")):
                raise StateStoreError("publisher CONTROL path contains unsafe control characters")
            normalized_paths.append(path)
        if len(set(normalized_paths)) != len(normalized_paths):
            raise StateStoreError("publisher CONTROL path list contains duplicates")

        self.ensure_commit(commit_sha)
        tree_raw = self._run(
            ("--git-dir", str(self.git_dir), "rev-parse", f"{commit_sha}^{{tree}}"),
            maximum_output=128,
        )
        try:
            tree_sha = tree_raw.decode("ascii", "strict").strip()
        except UnicodeError as exc:
            raise StateStoreError("publisher CONTROL tree SHA is not ASCII") from exc
        if not SHA1_RE.fullmatch(tree_sha) or tree_sha == "0" * 40:
            raise StateStoreError("publisher CONTROL tree SHA is malformed")

        metadata_raw = self._run(
            (
                "--literal-pathspecs", "--git-dir", str(self.git_dir),
                "ls-tree", "-r", "-l", "-z", commit_sha, "--",
                *normalized_paths,
            ),
            maximum_output=MAX_PUBLISHER_CONTROL_METADATA_BYTES,
        )
        if not metadata_raw or not metadata_raw.endswith(b"\0"):
            raise StateStoreError("publisher CONTROL tree metadata is absent or unterminated")
        requested = set(normalized_paths)
        by_path: dict[str, PublisherControlBlobMetadata] = {}
        aggregate = 0
        for row in (value for value in metadata_raw.split(b"\0") if value):
            metadata, separator, raw_path = row.partition(b"\t")
            try:
                path = _safe_path(raw_path.decode("utf-8", "strict"))
                parts = metadata.decode("ascii", "strict").split()
            except UnicodeError as exc:
                raise StateStoreError("publisher CONTROL tree metadata is not UTF-8/ASCII") from exc
            if not separator or len(parts) != 4:
                raise StateStoreError("publisher CONTROL tree metadata is malformed")
            mode, kind, oid, size_text = parts
            if path not in requested:
                raise StateStoreError("publisher CONTROL tree returned an unrequested path")
            if path in by_path:
                raise StateStoreError("publisher CONTROL tree returned a duplicate path")
            if (
                mode not in {"100644", "100755"}
                or kind != "blob"
                or not SHA1_RE.fullmatch(oid)
                or oid == "0" * 40
                or re.fullmatch(r"[1-9][0-9]*", size_text) is None
            ):
                raise StateStoreError("publisher CONTROL tree entry is not one bounded regular blob")
            size = int(size_text)
            if size > MAX_PUBLISHER_CONTROL_FILE_BYTES:
                raise StateStoreError("publisher CONTROL blob exceeds its per-file bound")
            aggregate += size
            if aggregate > maximum_total_bytes:
                raise StateStoreError("publisher CONTROL closure exceeds its remaining byte allowance")
            by_path[path] = PublisherControlBlobMetadata(
                path=path, mode=mode, git_blob_sha1=oid, byte_length=size,
            )
        if set(by_path) != requested:
            raise StateStoreError("publisher CONTROL tree omitted an exact requested path")

        ordered_metadata = tuple(by_path[path] for path in normalized_paths)
        query = b"".join(
            item.git_blob_sha1.encode("ascii") + b"\n" for item in ordered_metadata
        )
        batch_raw = self._run(
            ("--git-dir", str(self.git_dir), "cat-file", "--batch"),
            maximum_output=(
                aggregate + PUBLISHER_CONTROL_BATCH_FRAME_BYTES * len(ordered_metadata)
            ),
            input_bytes=query,
        )
        contents: dict[str, bytes] = {}
        offset = 0
        for item in ordered_metadata:
            header_end = batch_raw.find(b"\n", offset)
            if (
                header_end < 0
                or header_end == offset
                or header_end - offset >= PUBLISHER_CONTROL_BATCH_FRAME_BYTES
            ):
                raise StateStoreError("publisher CONTROL batch header is absent or over bound")
            header = batch_raw[offset:header_end]
            offset = header_end + 1
            try:
                header_parts = header.decode("ascii", "strict").split(" ")
            except UnicodeError as exc:
                raise StateStoreError("publisher CONTROL batch header is not ASCII") from exc
            if (
                len(header_parts) != 3
                or header_parts[0] != item.git_blob_sha1
                or header_parts[1] != "blob"
                or re.fullmatch(r"[1-9][0-9]*", header_parts[2]) is None
                or int(header_parts[2]) != item.byte_length
            ):
                raise StateStoreError("publisher CONTROL batch header differs from tree metadata")
            content_end = offset + item.byte_length
            if content_end >= len(batch_raw) or batch_raw[content_end:content_end + 1] != b"\n":
                raise StateStoreError("publisher CONTROL batch body is truncated or unterminated")
            content = batch_raw[offset:content_end]
            offset = content_end + 1
            expected_oid = hashlib.sha1(
                b"blob " + str(len(content)).encode("ascii") + b"\0" + content,
            ).hexdigest()
            if expected_oid != item.git_blob_sha1:
                raise StateStoreError("publisher CONTROL batch content differs from its Git object ID")
            contents[item.path] = content
        if offset != len(batch_raw):
            raise StateStoreError("publisher CONTROL batch returned trailing bytes")

        files = {
            item.path: GitBlobReadback(
                schema_version=GIT_BLOB_READBACK_SCHEMA,
                repository=AUTHORITY_REPOSITORY,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                path=item.path,
                git_blob_sha1=item.git_blob_sha1,
                byte_length=item.byte_length,
                content=contents[item.path],
            )
            for item in ordered_metadata
        }
        return PublisherControlClosureReadback(
            commit_readback=GitCommitReadback(
                schema_version=GIT_COMMIT_READBACK_SCHEMA,
                repository=AUTHORITY_REPOSITORY,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                files=files,
            ),
            metadata=ordered_metadata,
        )

    def stream_blob_digest(
        self, commit_sha: str, path: str, *, maximum_bytes: int,
        chunk_bytes: int = 1024 * 1024,
    ) -> dict[str, Any]:
        """Hash a materialized blob incrementally; never return its payload."""
        if chunk_bytes < 4096 or chunk_bytes > 4 * 1024 * 1024:
            raise StateStoreError("canonical stream chunk is outside its reviewed bound")
        if maximum_bytes < 1 or maximum_bytes > MAX_CANONICAL_STREAM_BYTES:
            raise StateStoreError("canonical stream ceiling exceeds the explicit 160 MiB bound")
        self.ensure_commit(commit_sha)
        entry = self._tree_entry(commit_sha, path)
        if entry is None:
            raise StateStoreError("streamed Git path is absent")
        oid, _mode, declared_size = entry
        if declared_size > maximum_bytes:
            raise StateStoreError("materialized canonical object exceeds its explicit stream ceiling")

        # Metadata may have consumed most of a caller's lease. Recompute the
        # effective absolute deadline after it, before reserving/spawning the
        # blob reader. The budget's command and cumulative output counters are
        # shared with the metadata commands and are deliberately not reset.
        operation_deadline = self.budget.deadline
        if self._caller_deadline is not None:
            if (
                isinstance(self._caller_deadline, bool)
                or not isinstance(self._caller_deadline, (int, float))
                or not math.isfinite(self._caller_deadline)
            ):
                raise StateStoreError("canonical Git stream caller deadline is invalid")
            operation_deadline = min(operation_deadline, float(self._caller_deadline))
        started = self.budget.clock()
        if not math.isfinite(operation_deadline) or operation_deadline <= started:
            raise StateStoreError("canonical Git stream deadline expired after metadata")
        self.budget.reserve(maximum_output=0)
        started = self.budget.clock()
        remaining = operation_deadline - started
        if remaining <= 0:
            raise StateStoreError("canonical Git stream deadline expired before subprocess")
        cleanup_reserve = min(1.0, remaining / 4.0)
        work_deadline = operation_deadline - cleanup_reserve
        if work_deadline <= started:
            raise StateStoreError("canonical Git stream work deadline expired before subprocess")

        process: subprocess.Popen[bytes] | None = None
        selector: selectors.BaseSelector | None = None
        streams: tuple[Any, Any] = (None, None)
        child_reaped = False
        primary_error: BaseException | None = None
        primary_traceback: Any = None
        cleanup_interrupt: BaseException | None = None
        cleanup_interrupt_traceback: Any = None
        cleanup_uncertainty: list[str] = []
        return_code: int | None = None
        total = 0
        stderr_bytes = bytearray()
        digest = hashlib.sha256()

        def note_uncertainty(note: str) -> None:
            if note not in cleanup_uncertainty and len(cleanup_uncertainty) < 8:
                cleanup_uncertainty.append(note)

        def note_cleanup_interrupt(exc: BaseException) -> None:
            nonlocal cleanup_interrupt, cleanup_interrupt_traceback
            if not isinstance(exc, Exception) and cleanup_interrupt is None:
                cleanup_interrupt = exc
                cleanup_interrupt_traceback = exc.__traceback__
                note_uncertainty("canonical Git stream cleanup was interrupted")

        def cleanup() -> None:
            nonlocal child_reaped
            if process is not None and not child_reaped:
                observed: int | None = None
                try:
                    observed = process.poll()
                except BaseException as exc:
                    note_cleanup_interrupt(exc)
                    note_uncertainty("canonical Git stream child status could not be read")
                if observed is not None:
                    child_reaped = True
                if not child_reaped:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        # A concurrent exit is resolved by the bounded wait.
                        pass
                    except BaseException as exc:
                        note_cleanup_interrupt(exc)
                        note_uncertainty("canonical Git stream child kill could not be confirmed")
                    try:
                        try:
                            cleanup_now = self.budget.clock()
                        except BaseException as exc:
                            note_cleanup_interrupt(exc)
                            cleanup_now = operation_deadline
                        process.wait(timeout=max(0.0, operation_deadline - cleanup_now))
                        child_reaped = True
                    except BaseException as exc:
                        note_cleanup_interrupt(exc)
                    if not child_reaped:
                        try:
                            observed = process.poll()
                        except BaseException as exc:
                            note_cleanup_interrupt(exc)
                            note_uncertainty("canonical Git stream child reap could not be confirmed")
                        else:
                            child_reaped = observed is not None
                if not child_reaped:
                    note_uncertainty("canonical Git stream direct child reap could not be confirmed")

            if selector is not None:
                try:
                    selector.close()
                except BaseException as exc:
                    note_cleanup_interrupt(exc)
                    note_uncertainty("canonical Git stream selector close could not be confirmed")
            for label, stream in zip(("stdout", "stderr"), streams):
                if stream is None:
                    continue
                try:
                    stream.close()
                except BaseException as exc:
                    note_cleanup_interrupt(exc)
                    note_uncertainty(f"canonical Git stream {label} close could not be confirmed")

        def attach_cleanup_notes(exc: BaseException) -> None:
            add_note = getattr(exc, "add_note", None)
            if add_note is None:
                return
            for note in cleanup_uncertainty[:8]:
                try:
                    add_note(note)
                except BaseException:
                    break

        try:
            if self.budget.clock() >= work_deadline:
                raise StateStoreError("canonical Git stream work deadline expired before subprocess")
            process = subprocess.Popen(
                (str(self.git_executable), "--git-dir", str(self.git_dir), "cat-file", "blob", oid),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._environment(),
                start_new_session=True,
            )
            # Own both streams before selector setup so every later failure
            # still reaches independent close attempts.
            streams = (process.stdout, process.stderr)
            assert process.stdout is not None and process.stderr is not None
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = work_deadline - self.budget.clock()
                if remaining <= 0:
                    raise StateStoreError("canonical Git stream work deadline expired")
                events = selector.select(timeout=min(0.1, remaining))
                for key, _mask in events:
                    if self.budget.clock() >= work_deadline:
                        raise StateStoreError("canonical Git stream work deadline expired before read")
                    stream = key.fileobj
                    kind = key.data
                    chunk = os.read(stream.fileno(), chunk_bytes if kind == "stdout" else 64 * 1024)
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    if kind == "stderr":
                        if len(stderr_bytes) + len(chunk) > 64 * 1024:
                            raise StateStoreError("canonical Git stderr exceeded its fixed bound")
                        stderr_bytes.extend(chunk)
                        self.budget.account_output(len(chunk))
                        continue
                    total += len(chunk)
                    if total > maximum_bytes or total > declared_size:
                        raise StateStoreError("canonical Git stream exceeded its declared or explicit bound")
                    digest.update(chunk)
                    self.budget.account_output(len(chunk))
            wait_remaining = work_deadline - self.budget.clock()
            if wait_remaining <= 0:
                raise StateStoreError("canonical Git stream work deadline expired before wait")
            return_code = process.wait(timeout=wait_remaining)
            child_reaped = True
            finished_at = self.budget.clock()
            if finished_at >= work_deadline or finished_at >= operation_deadline:
                raise StateStoreError("canonical Git stream deadline expired before acceptance")
            if return_code != 0 or stderr_bytes or total != declared_size:
                raise StateStoreError("canonical Git stream failed exact process/length readback")
        except BaseException as exc:
            primary_error = exc
            primary_traceback = exc.__traceback__
        finally:
            cleanup()

        if primary_error is not None:
            if isinstance(primary_error, (OSError, subprocess.TimeoutExpired)):
                translated = StateStoreError("canonical Git stream failed or timed out")
                attach_cleanup_notes(translated)
                raise translated from primary_error
            attach_cleanup_notes(primary_error)
            raise primary_error.with_traceback(primary_traceback)
        if cleanup_interrupt is not None:
            attach_cleanup_notes(cleanup_interrupt)
            raise cleanup_interrupt.with_traceback(cleanup_interrupt_traceback)
        if cleanup_uncertainty:
            error = StateStoreError("canonical Git stream cleanup could not be confirmed")
            attach_cleanup_notes(error)
            raise error
        if return_code is None:
            raise StateStoreError("canonical Git stream returned without a confirmed exit")
        if total != declared_size:
            raise StateStoreError("canonical Git stream returned a truncated or oversized blob")
        accepted_at = self.budget.clock()
        if not math.isfinite(accepted_at) or accepted_at >= operation_deadline:
            raise StateStoreError("canonical Git stream deadline expired during cleanup")
        return {
            "commit_sha": commit_sha,
            "path": path,
            "git_blob_sha1": oid,
            "bytes": total,
            "sha256": digest.hexdigest(),
            "chunk_bytes": chunk_bytes,
        }

    def _commit_updates(
        self, *, expected_sha: str | None, base_sha: str, updates: Mapping[str, bytes],
        create_only: frozenset[str] = frozenset(), message: str,
        update_modes: Mapping[str, str] | None = None,
    ) -> str:
        if not updates or any(not isinstance(raw, bytes) or not raw or len(raw) > MAX_RECORD_BYTES for raw in updates.values()):
            raise StateStoreError("state commit requires bounded nonempty byte updates")
        for path in updates:
            _safe_path(path)
        modes = dict(update_modes or {})
        if set(modes) - set(updates) or any(mode not in {"100644", "100755"} for mode in modes.values()):
            raise StateStoreError("state commit update mode is not a regular-file mode")
        current = self.remote_sha()
        if expected_sha is None:
            if current is not None:
                raise StateConflict("state branch appeared after the no-intent read")
            parent = self.fetch_state_base(base_sha)
        else:
            if not SHA1_RE.fullmatch(expected_sha) or current != expected_sha:
                raise StateConflict("state branch moved from the exact expected commit")
            parent = self.ensure_commit(expected_sha) or expected_sha
        for path in create_only:
            if path in updates and self._tree_entry(parent, path) is not None:
                raise StateConflict("create-only state file already exists")
        self._run(("--git-dir", str(self.git_dir), "read-tree", parent), maximum_output=64 * 1024, index=self._index_path)
        for path, raw in updates.items():
            blob = self._run(
                ("--git-dir", str(self.git_dir), "hash-object", "-w", "--stdin"),
                maximum_output=128, input_bytes=raw,
            ).decode("ascii").strip()
            if not SHA1_RE.fullmatch(blob):
                raise StateStoreError("Git did not return an exact blob object ID")
            self._run(
                ("--git-dir", str(self.git_dir), "update-index", "--add", "--cacheinfo", f"{modes.get(path, '100644')},{blob},{path}"),
                maximum_output=64 * 1024, index=self._index_path,
            )
        tree = self._run(("--git-dir", str(self.git_dir), "write-tree"), maximum_output=128, index=self._index_path).decode("ascii").strip()
        if not SHA1_RE.fullmatch(tree):
            raise StateStoreError("Git did not return an exact tree object ID")
        new_sha = self._run(
            ("--git-dir", str(self.git_dir), "commit-tree", tree, "-p", parent, "-m", message[:120]),
            maximum_output=128, author=True,
        ).decode("ascii").strip()
        if not SHA1_RE.fullmatch(new_sha):
            raise StateStoreError("Git did not return an exact commit ID")
        expected = expected_sha or ""
        try:
            self._run((
                "--git-dir", str(self.git_dir), "push", "--no-verify",
                f"--force-with-lease={STATE_REF}:{expected}", "origin", f"{new_sha}:{STATE_REF}",
            ), maximum_output=256 * 1024)
        except StateCleanupUncertain as exc:
            # Record the remote-write fact separately from process cleanup.
            # Exact ref/blob readback can prove the write landed, but it cannot
            # make an unconfirmed local process group safe to continue past.
            observed: str | None = None
            write_verified: bool | None = None
            try:
                observed = self.remote_sha()
                if observed == new_sha:
                    self.ensure_commit(new_sha)
                    for path, expected_bytes in updates.items():
                        if self.read_file(new_sha, path, maximum=MAX_RECORD_BYTES) != expected_bytes:
                            raise StateStoreError("state cleanup readback differs from exact transaction bytes")
                    write_verified = True
            except Exception:
                write_verified = None
            raise StateOutcomeUncertain(
                "state branch push outcome is uncertain because subprocess cleanup is unconfirmed",
                child_sha=new_sha,
                remote_ref_sha=observed,
                remote_write_verified=write_verified,
                process_cleanup_confirmed=False,
            ) from exc
        except StateStoreError:
            # A failed local command can still have delivered the request.
            # Exact remote readback below is the only success proof.
            pass
        try:
            observed = self.remote_sha()
        except StateCleanupUncertain as exc:
            raise StateOutcomeUncertain(
                "state branch push outcome is uncertain because post-push ref readback cleanup is unconfirmed",
                child_sha=new_sha,
                remote_ref_sha=None,
                remote_write_verified=None,
                process_cleanup_confirmed=False,
            ) from exc
        except Exception as exc:
            raise StateOutcomeUncertain(
                "state branch push outcome remains uncertain",
                child_sha=new_sha,
                remote_ref_sha=None,
                remote_write_verified=None,
                process_cleanup_confirmed=None,
            ) from exc
        if observed != new_sha:
            raise StateOutcomeUncertain(
                "state branch push outcome remains uncertain",
                child_sha=new_sha,
                remote_ref_sha=observed,
                remote_write_verified=None,
                process_cleanup_confirmed=True,
            )
        try:
            self.ensure_commit(new_sha)
            for path, expected_bytes in updates.items():
                actual = self.read_file(new_sha, path, maximum=MAX_RECORD_BYTES)
                if actual != expected_bytes:
                    raise StateStoreError("state commit readback differs from exact transaction bytes")
        except StateCleanupUncertain as exc:
            raise StateOutcomeUncertain(
                "state child commit readback cleanup is unconfirmed",
                child_sha=new_sha,
                remote_ref_sha=observed,
                remote_write_verified=None,
                process_cleanup_confirmed=False,
            ) from exc
        except Exception as exc:
            raise StateOutcomeUncertain(
                "state child commit readback remains uncertain",
                child_sha=new_sha,
                remote_ref_sha=observed,
                remote_write_verified=None,
                process_cleanup_confirmed=None,
            ) from exc
        return new_sha


class StateBranchStore:
    """One branch-wide CAS store for archives, leases, intents and receivers."""

    def __init__(
        self, git: GitObjectStore, *, base_sha: str,
        clock: Any = lambda: datetime.now(timezone.utc), max_cas_retries: int = 3,
    ) -> None:
        if not SHA1_RE.fullmatch(base_sha) or base_sha == "0" * 40:
            raise StateStoreError("state branch base SHA is malformed")
        self.git = git
        self.base_sha = base_sha
        self.clock = clock
        self.max_cas_retries = max_cas_retries
        self._observed_head: dict[str, str | None] = {}

    @staticmethod
    def _archive_path(subject_key: str) -> str:
        return f"{ADMISSIONS_DIR}/{_key(subject_key)}.json"

    @staticmethod
    def _dispatch_path(subject_key: str) -> str:
        return f"{DISPATCH_DIR}/{_key(subject_key)}.json"

    @staticmethod
    def _lease_path(subject_key: str) -> str:
        return f"{LEASE_DIR}/{_key(subject_key)}.json"

    @staticmethod
    def _receiver_path(subject_key: str, run_id: int, attempt: int) -> str:
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1 or isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise StateStoreError("receiver run/attempt identity is malformed")
        return f"{RECEIVERS_DIR}/{_key(subject_key)}/{run_id}-{attempt}.json"

    @staticmethod
    def _publisher_phase_path(subject_key: str, run_id: int, attempt: int) -> str:
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1 or isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise StateStoreError("publisher phase run/attempt identity is malformed")
        return f"{PUBLISHER_PHASES_DIR}/{_key(subject_key)}/{run_id}-{attempt}.json"

    @staticmethod
    def _manual_receiver_path(manual_execution_key: str) -> str:
        return f"{MANUAL_RECEIVERS_DIR}/{_key(manual_execution_key, 'manual execution key')}.json"

    @staticmethod
    def _manual_publisher_phase_path(manual_execution_key: str) -> str:
        return (
            f"{MANUAL_PUBLISHER_PHASES_DIR}/"
            f"{_key(manual_execution_key, 'manual execution key')}.json"
        )

    @staticmethod
    def _terminal_observation_path(role: str, stable_key: str) -> str:
        if role == ROLE_AUTOMATIC_OWNED_C:
            namespace = "owned-c"
            key = _key(stable_key)
        elif role == ROLE_MANUAL_EXACT_SOURCE:
            namespace = "manual"
            key = _key(stable_key, "manual execution key")
        else:
            raise StateStoreError("terminal observation role is unsupported")
        return f"{TERMINAL_OBSERVATIONS_DIR}/{namespace}/{key}.json"

    def _head(self) -> str | None:
        return self.git.remote_sha()

    def load_state(self, subject_key: str) -> tuple[str | None, Mapping[str, Any] | None]:
        subject_key = _key(subject_key)
        head = self._head()
        if head is None:
            self._observed_head[subject_key] = None
            return None, None
        path = self._dispatch_path(subject_key)
        try:
            raw = self.git.read_file(head, path, maximum=MAX_STATE_BYTES)
        except StateStoreError as exc:
            if "path is absent" not in str(exc):
                raise
            # The branch is shared across subjects.  A branch head that only
            # contains another archive/lease is not a dispatch intent for this
            # key; preserve its exact SHA privately for the next CAS.
            self._observed_head[subject_key] = head
            return None, None
        value = strict_json(raw, label="dispatch state", maximum=MAX_STATE_BYTES)
        if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
            raise StateStoreError("dispatch state is not a canonical JSON object")
        self._validate_dispatch(value)
        self._observed_head[subject_key] = head
        return head, dict(value)

    def _validate_dispatch(self, state: Mapping[str, Any]) -> dict[str, Any]:
        try:
            import canonical_publication_coordinator as coordinator
            return coordinator.validate_dispatch_state(state)
        except Exception as exc:
            raise StateStoreError("dispatch state failed the trusted coordinator schema validator") from exc

    def acquire_lease(self, subject_key: str, owner: str, seconds: int) -> Mapping[str, Any]:
        subject_key = _key(subject_key)
        if not isinstance(owner, str) or not owner or len(owner) > 160 or "\n" in owner:
            raise StateStoreError("lease owner is malformed")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 300:
            raise StateStoreError("lease duration is outside the 300-second hard bound")
        path = self._lease_path(subject_key)
        for _ in range(self.max_cas_retries):
            head = self._head()
            parent = head or self.base_sha
            raw_old = None
            if head:
                try:
                    raw_old = self.git.read_file(head, path, maximum=16 * 1024)
                except StateStoreError as exc:
                    if "path is absent" not in str(exc):
                        raise
            prior = strict_json(raw_old, label="lease record", maximum=16 * 1024) if raw_old else None
            now = self.clock().astimezone(timezone.utc)
            if prior is not None:
                if (
                    not isinstance(prior, Mapping)
                    or set(prior) != {"schema_version", "subject_key", "owner", "deadline", "fence"}
                    or prior.get("schema_version") != LEASE_SCHEMA
                    or prior.get("subject_key") != subject_key
                ):
                    raise StateStoreError("existing lease record has an unsupported schema or subject")
                try:
                    expires = datetime.fromisoformat(str(prior["deadline"]).replace("Z", "+00:00"))
                except ValueError as exc:
                    raise StateStoreError("existing lease deadline is malformed") from exc
                if expires.tzinfo is None:
                    raise StateStoreError("existing lease deadline lacks timezone")
                if expires > now and prior.get("owner") != owner:
                    raise StateConflict("an unexpired state lease belongs to another coordinator")
                old_fence = prior.get("fence")
                if isinstance(old_fence, bool) or not isinstance(old_fence, int) or old_fence < 0:
                    raise StateStoreError("existing lease fence is malformed")
            else:
                old_fence = 0
            fence = old_fence + 1
            deadline = (now + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
            value = {
                "schema_version": LEASE_SCHEMA,
                "subject_key": subject_key,
                "owner": owner,
                "deadline": deadline,
                "fence": fence,
            }
            expected = head
            try:
                new_sha = self.git._commit_updates(
                    expected_sha=expected, base_sha=self.base_sha,
                    updates={path: canonical_json_bytes(value)},
                    create_only=frozenset({path}) if prior is None else frozenset(),
                    message="Advance canonical publication lease",
                )
            except StateConflict:
                continue
            # Read the exact commit and its immutable lease bytes before use.
            readback = strict_json(self.git.read_file(new_sha, path, maximum=16 * 1024), label="lease readback", maximum=16 * 1024)
            if readback != value:
                raise StateStoreError("lease readback differs from exact CAS bytes")
            return {"owner": owner, "deadline": deadline, "fence": fence, "commit_sha": new_sha}
        raise StateConflict("lease CAS retry budget exhausted")

    def validate_current_lease(self, subject_key: str, expected: Mapping[str, Any]) -> bool:
        subject_key = _key(subject_key)
        if set(expected) != {"owner", "deadline", "fence"}:
            raise StateStoreError("expected lease identity has an unsupported shape")
        head = self._head()
        if head is None:
            raise StateConflict("state branch disappeared before the external dispatch")
        raw = self.git.read_file(head, self._lease_path(subject_key), maximum=16 * 1024)
        lease = strict_json(raw, label="current dispatch lease", maximum=16 * 1024)
        if (
            not isinstance(lease, Mapping)
            or set(lease) != {"schema_version", "subject_key", "owner", "deadline", "fence"}
            or lease.get("schema_version") != LEASE_SCHEMA
            or lease.get("subject_key") != subject_key
            or any(lease.get(key) != expected.get(key) for key in ("owner", "deadline", "fence"))
        ):
            raise StateConflict("current state-branch lease differs from the reserved dispatch owner/fence")
        try:
            deadline = datetime.fromisoformat(str(lease["deadline"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as exc:
            raise StateStoreError("current dispatch lease deadline is malformed") from exc
        if deadline.tzinfo is None or deadline <= self.clock().astimezone(timezone.utc):
            raise StateConflict("reserved dispatch lease expired before the external POST")
        return True

    def compare_and_swap(self, expected_commit: str | None, state: Mapping[str, Any], *, fence: int) -> str:
        state = self._validate_dispatch(state)
        subject_key = _key(state.get("subject_key"))
        path = self._dispatch_path(subject_key)
        if isinstance(fence, bool) or not isinstance(fence, int) or state["lease"]["fence"] != fence:
            raise StateStoreError("dispatch CAS fence differs from the exact state lease")
        sentinel = object()
        observed = self._observed_head.get(subject_key, sentinel)
        if observed is sentinel:
            raise StateStoreError("dispatch CAS lacks a preceding exact load_state observation")
        current = self._head()
        expected_head = expected_commit if expected_commit is not None else observed
        if current != expected_head:
            raise StateConflict("dispatch CAS head differs from exact load_state observation")
        if not isinstance(expected_head, str):
            raise StateStoreError("initial dispatch CAS has no durable lease branch parent")
        lease_path = self._lease_path(subject_key)
        lease_raw = self.git.read_file(current, lease_path, maximum=16 * 1024)
        lease = strict_json(lease_raw, label="dispatch lease validation", maximum=16 * 1024)
        now = self.clock().astimezone(timezone.utc)
        deadline = datetime.fromisoformat(str(lease.get("deadline", "")).replace("Z", "+00:00"))
        if (
            lease.get("subject_key") != subject_key
            or lease.get("fence") != fence
            or lease.get("owner") != state["lease"]["owner"]
            or deadline.tzinfo is None or deadline <= now
        ):
            raise StateConflict("dispatch CAS lease owner/fence/deadline is no longer current")
        raw = canonical_json_bytes(state)
        if len(raw) > MAX_STATE_BYTES:
            raise StateStoreError("dispatch state exceeds its fixed metadata bound")
        new_sha = self.git._commit_updates(
            expected_sha=current, base_sha=self.base_sha, updates={path: raw},
            message="Update canonical publication dispatch state",
        )
        self._observed_head[subject_key] = new_sha
        return new_sha

    def readback_state(self, commit_sha: str, subject_key: str) -> Mapping[str, Any]:
        if not SHA1_RE.fullmatch(commit_sha):
            raise StateStoreError("dispatch state readback commit is malformed")
        raw = self.git.read_file(commit_sha, self._dispatch_path(subject_key), maximum=MAX_STATE_BYTES)
        value = strict_json(raw, label="dispatch state readback", maximum=MAX_STATE_BYTES)
        if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
            raise StateStoreError("dispatch state readback is not exact canonical JSON")
        return self._validate_dispatch(value)

    def put_if_absent(self, subject_key: str, record_bytes: bytes) -> Mapping[str, Any]:
        subject_key = _key(subject_key)
        if not isinstance(record_bytes, bytes) or not record_bytes or len(record_bytes) > MAX_RECORD_BYTES:
            raise StateStoreError("admission archive bytes are outside the fixed bound")
        value = strict_json(record_bytes, label="admission archive", maximum=MAX_RECORD_BYTES)
        if not isinstance(value, Mapping) or canonical_json_bytes(value) != record_bytes or value.get("subject_key") != subject_key:
            raise StateStoreError("admission archive is not canonical JSON for its stable subject")
        existing = self.read_subject(subject_key)
        if existing is not None:
            if existing["record_bytes"] != record_bytes:
                raise StateConflict("stable admission key already has different immutable bytes")
            return {key: existing[key] for key in ("subject_key", "path", "commit_sha", "record_sha256", "bytes")}
        path = self._archive_path(subject_key)
        head = self._head()
        expected = head
        base = head or self.base_sha
        try:
            commit = self.git._commit_updates(
                expected_sha=expected, base_sha=base,
                updates={path: record_bytes}, create_only=frozenset({path}),
                message="Create canonical publication admission archive",
            )
        except (StateConflict, StateStoreError):
            # A lost create-only race is resolved by immutable readback, never
            # by replacing the winner or minting another archive.
            existing = self.read_subject(subject_key)
            if existing is not None and existing["record_bytes"] == record_bytes:
                return {key: existing[key] for key in ("subject_key", "path", "commit_sha", "record_sha256", "bytes")}
            raise
        result = {
            "subject_key": subject_key, "path": path, "commit_sha": commit,
            "record_sha256": hashlib.sha256(record_bytes).hexdigest(), "bytes": len(record_bytes),
        }
        if self.readback(result)["record_bytes"] != record_bytes:
            raise StateStoreError("create-only admission archive failed exact immutable readback")
        return result

    def readback(self, result: Mapping[str, Any]) -> Mapping[str, Any]:
        subject_key = _key(result.get("subject_key"))
        path = self._archive_path(subject_key)
        if result.get("path") != path or not isinstance(result.get("commit_sha"), str) or not SHA1_RE.fullmatch(result["commit_sha"]):
            raise StateStoreError("admission archive locator is outside its exact namespace")
        raw = self.git.read_file(result["commit_sha"], path, maximum=MAX_RECORD_BYTES)
        if (
            result.get("bytes") != len(raw)
            or result.get("record_sha256") != hashlib.sha256(raw).hexdigest()
        ):
            raise StateStoreError("admission archive locator digest/size differs from immutable bytes")
        return {
            "subject_key": subject_key, "state_branch": STATE_BRANCH, "path": path,
            "commit_sha": result["commit_sha"], "record_bytes": raw,
            "bytes": len(raw), "record_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def _first_immutable_version(self, subject_key: str, path: str) -> tuple[str, bytes] | None:
        head = self._head()
        if head is None or self.git._tree_entry(head, path) is None:
            return None
        commits_raw = self.git._run(
            ("--git-dir", str(self.git.git_dir), "log", "--format=%H", "--reverse", head, "--", path),
            maximum_output=MAX_HISTORY_COMMITS * 41,
        )
        commits = [line.decode("ascii") for line in commits_raw.splitlines() if line]
        if not commits or len(commits) > MAX_HISTORY_COMMITS or any(not SHA1_RE.fullmatch(item) for item in commits):
            raise StateStoreError("immutable archive history is empty, malformed, or over its scan bound")
        first_raw: bytes | None = None
        first_sha = ""
        for commit in commits:
            try:
                raw = self.git.read_file(commit, path, maximum=MAX_RECORD_BYTES)
            except StateStoreError as exc:
                if "path is absent" in str(exc):
                    raise StateStoreError("immutable archive path was deleted or replaced in history") from exc
                raise
            if first_raw is None:
                first_raw, first_sha = raw, commit
            elif raw != first_raw:
                raise StateStoreError("immutable archive bytes changed in state-branch history")
        current_raw = self.git.read_file(head, path, maximum=MAX_RECORD_BYTES)
        if first_raw != current_raw:
            raise StateStoreError("immutable archive current readback differs from its first committed bytes")
        return first_sha, first_raw

    def read_subject(self, subject_key: str) -> Mapping[str, Any] | None:
        subject_key = _key(subject_key)
        path = self._archive_path(subject_key)
        item = self._first_immutable_version(subject_key, path)
        if item is None:
            return None
        commit_sha, raw = item
        value = strict_json(raw, label="immutable admission archive", maximum=MAX_RECORD_BYTES)
        if not isinstance(value, Mapping) or value.get("subject_key") != subject_key or canonical_json_bytes(value) != raw:
            raise StateStoreError("immutable admission archive subject or canonical JSON is invalid")
        return {
            "subject_key": subject_key, "state_branch": STATE_BRANCH, "path": path,
            "commit_sha": commit_sha, "record_bytes": raw,
            "bytes": len(raw), "record_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def register_receiver(
        self, *, subject_key: str, intent_id: str, fence: int, run_id: int,
        run_attempt: int, workflow_sha: str, source_sha: str,
        status: str, conclusion: str | None,
    ) -> Mapping[str, Any]:
        subject_key = _key(subject_key)
        if not SHA256_RE.fullmatch(intent_id) or isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise StateStoreError("receiver intent/fence is malformed")
        if not SHA1_RE.fullmatch(workflow_sha) or not SHA1_RE.fullmatch(source_sha):
            raise StateStoreError("receiver workflow/source SHA is malformed")
        if status != "in_progress" or conclusion is not None:
            raise StateStoreError("receiver registration requires the exact active publisher attempt before side effects")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (run_id, run_attempt)):
            raise StateStoreError("receiver run/attempt identity is malformed")
        state_sha, state = self.load_state(subject_key)
        if state is None or state_sha is None:
            raise StateConflict("receiver registration requires the durable dispatch intent")
        if (
            state.get("intent_id") != intent_id
            or state.get("dispatch_fence") != fence
            or state.get("workflow_sha") != workflow_sha
            or state.get("source_sha") != source_sha
            or state.get("dispatch_attempts") != 1
        ):
            raise StateConflict("receiver run does not match the reserved durable dispatch intent")
        registration = {
            "registered": True, "subject_key": subject_key, "intent_id": intent_id,
            "fence": fence, "run_id": run_id, "run_attempt": run_attempt,
            "evidence_ref": self._receiver_path(subject_key, run_id, run_attempt),
        }
        existing_run = state.get("run")
        if existing_run is not None:
            if state.get("state") not in {"queued", "running"}:
                raise StateConflict("receiver registration cannot reopen a terminal dispatch state")
            if (
                existing_run.get("run_id") != run_id
                or existing_run.get("run_attempt") != run_attempt
                or existing_run.get("receiver_registration", {}).get("subject_key") != subject_key
            ):
                raise StateConflict("another receiver run/attempt already consumed this dispatch fence")
            return registration
        if state.get("state") not in {"intent", "uncertain", "queued"}:
            raise StateConflict("receiver cannot register after dispatch moved beyond its unregistered state")
        record = {
            "schema_version": "datapan.canonical-publication-receiver-registration.v1",
            "repository": REPOSITORY,
            "workflow_path": ".github/workflows/huggingface-distribution.yml",
            "event": "workflow_dispatch", "workflow_sha": workflow_sha,
            "source_sha": source_sha, "subject_key": subject_key,
            "intent_id": intent_id, "dispatch_fence": fence,
            "run_id": run_id, "run_attempt": run_attempt,
            "status": status, "conclusion": conclusion,
        }
        receiver_path = self._receiver_path(subject_key, run_id, run_attempt)
        state = dict(state)
        state["run"] = {
            "repository": REPOSITORY,
            "workflow_path": ".github/workflows/huggingface-distribution.yml",
            "event": "workflow_dispatch", "workflow_sha": workflow_sha,
            "source_sha": source_sha, "run_id": run_id, "run_attempt": run_attempt,
            "status": status, "conclusion": conclusion,
            "receiver_registration": {
                "registered": True, "subject_key": subject_key,
                "intent_id": intent_id, "fence": fence, "run_id": run_id,
                "run_attempt": run_attempt, "evidence_ref": receiver_path,
            },
        }
        state["state"] = "running"
        state["blocker"] = None
        if state["cas_writes"] >= 16:
            raise StateStoreError("receiver transition CAS budget is exhausted")
        state["cas_writes"] += 1
        state["evidence_refs"] = list(dict.fromkeys([*state["evidence_refs"], receiver_path]))[:24]
        history = list(state["history"])
        now = self.clock().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        history.append({"state": state["state"], "observed_at": now, "fence": fence, "detail": "exact_receiver_run_registered_before_publisher_effect"})
        if len(history) > 32:
            raise StateStoreError("receiver transition history budget is exhausted")
        state["history"] = history
        state_raw = canonical_json_bytes(state)
        receiver_raw = canonical_json_bytes(record)
        new_sha = self.git._commit_updates(
            expected_sha=state_sha, base_sha=self.base_sha,
            updates={self._dispatch_path(subject_key): state_raw, receiver_path: receiver_raw},
            create_only=frozenset({receiver_path}),
            message="Register exact canonical publisher receiver run",
        )
        readback = self.readback_state(new_sha, subject_key)
        if canonical_json_bytes(readback) != state_raw:
            raise StateStoreError("receiver dispatch-state CAS readback differs from exact bytes")
        sidecar = strict_json(self.git.read_file(new_sha, receiver_path, maximum=64 * 1024), label="receiver registration readback", maximum=64 * 1024)
        if sidecar != record:
            raise StateStoreError("receiver sidecar readback differs from exact native run registration")
        self._observed_head[subject_key] = new_sha
        return registration

    def read_receiver_registration(self, subject_key: str, run_id: int, run_attempt: int) -> Mapping[str, Any]:
        subject_key = _key(subject_key)
        path = self._receiver_path(subject_key, run_id, run_attempt)
        item = self._first_immutable_version(subject_key, path)
        if item is None:
            raise StateStoreError("exact receiver registration sidecar is absent")
        commit_sha, raw = item
        record = strict_json(raw, label="receiver sidecar", maximum=64 * 1024)
        if (
            not isinstance(record, Mapping)
            or canonical_json_bytes(record) != raw
            or record.get("subject_key") != subject_key
            or record.get("run_id") != run_id
            or record.get("run_attempt") != run_attempt
        ):
            raise StateStoreError("receiver sidecar is not bound to the exact admitted run/attempt")
        return {
            "record": dict(record), "record_bytes": raw,
            "state_branch": STATE_BRANCH, "path": path, "commit_sha": commit_sha,
            "bytes": len(raw), "record_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def record_receiver_phase(
        self, *, subject_key: str, intent_id: str, fence: int,
        run_id: int, run_attempt: int, phase: str,
        details: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Create and read back the one durable pre-publication phase record.

        The phase is a create-once sidecar bound to the already registered
        receiver and the exact dispatch row.  Retrying the same bytes is
        idempotent; a changed phase/detail document for the spent run slot is
        rejected without rewriting history.
        """
        subject_key = _key(subject_key)
        if not SHA256_RE.fullmatch(intent_id) or isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise StateStoreError("publisher phase intent/fence is malformed")
        if phase != "publisher_worker_may_mutate":
            raise StateStoreError("publisher phase is outside the closed prelaunch set")
        if not isinstance(details, Mapping):
            raise StateStoreError("publisher phase details must be an object")
        detail_copy = strict_json(
            canonical_json_bytes(dict(details)),
            label="publisher phase details", maximum=64 * 1024,
        )
        if not isinstance(detail_copy, Mapping):
            raise StateStoreError("publisher phase details are not a canonical object")
        expected_detail_keys = {
            "subject_key", "intent_id", "receiver_fence", "repository",
            "run_id", "run_attempt", "job_id", "workflow_sha", "source_sha",
            "merged_sha", "entrypoint", "deadline_remaining_ms", "receipt_path",
        }
        if set(detail_copy) != expected_detail_keys:
            raise StateStoreError("publisher phase details have unsupported fields")
        if (
            detail_copy.get("subject_key") != subject_key
            or detail_copy.get("intent_id") != intent_id
            or detail_copy.get("receiver_fence") != str(fence)
            or detail_copy.get("repository") != REPOSITORY
            or detail_copy.get("run_id") != str(run_id)
            or detail_copy.get("run_attempt") != str(run_attempt)
            or detail_copy.get("source_sha") != detail_copy.get("merged_sha")
            or not SHA1_RE.fullmatch(str(detail_copy.get("workflow_sha", "")))
            or not SHA1_RE.fullmatch(str(detail_copy.get("source_sha", "")))
        ):
            raise StateStoreError("publisher phase details differ from the receiver identity")
        job_id = detail_copy.get("job_id")
        entrypoint = detail_copy.get("entrypoint")
        receipt_path = detail_copy.get("receipt_path")
        remaining_ms = detail_copy.get("deadline_remaining_ms")
        if (
            not isinstance(job_id, str) or not job_id or len(job_id.encode("utf-8")) > 128
            or not isinstance(entrypoint, str) or _safe_path(entrypoint) != entrypoint
            or not isinstance(receipt_path, str) or _safe_path(receipt_path) != receipt_path
            or isinstance(remaining_ms, bool) or not isinstance(remaining_ms, int)
            or not 0 <= remaining_ms <= 15 * 60 * 1000
        ):
            raise StateStoreError("publisher phase execution details are malformed or over bound")

        receiver = self.read_receiver_registration(subject_key, run_id, run_attempt)["record"]
        if (
            receiver.get("intent_id") != intent_id
            or receiver.get("dispatch_fence") != fence
            or receiver.get("workflow_sha") != detail_copy["workflow_sha"]
            or receiver.get("source_sha") != detail_copy["source_sha"]
            or receiver.get("status") != "in_progress"
            or receiver.get("conclusion") is not None
        ):
            raise StateConflict("publisher phase receiver sidecar differs from the exact active attempt")

        path = self._publisher_phase_path(subject_key, run_id, run_attempt)
        for _ in range(self.max_cas_retries):
            head = self._head()
            if head is None:
                raise StateConflict("publisher phase requires the durable state branch")
            state_raw = self.git.read_file(head, self._dispatch_path(subject_key), maximum=MAX_STATE_BYTES)
            state = strict_json(state_raw, label="publisher phase dispatch state", maximum=MAX_STATE_BYTES)
            if not isinstance(state, Mapping) or canonical_json_bytes(state) != state_raw:
                raise StateStoreError("publisher phase dispatch state is not canonical JSON")
            state = self._validate_dispatch(state)
            run = state.get("run")
            registration = run.get("receiver_registration") if isinstance(run, Mapping) else None
            if (
                state.get("intent_id") != intent_id
                or state.get("dispatch_fence") != fence
                or state.get("state") != "running"
                or not isinstance(run, Mapping)
                or run.get("run_id") != run_id
                or run.get("run_attempt") != run_attempt
                or not isinstance(registration, Mapping)
                or registration.get("evidence_ref") != self._receiver_path(subject_key, run_id, run_attempt)
            ):
                raise StateConflict("publisher phase dispatch row differs from the registered receiver")

            try:
                existing_raw = self.git.read_file(head, path, maximum=64 * 1024)
            except StateStoreError as exc:
                if "path is absent" not in str(exc):
                    raise
                existing_raw = None
            if existing_raw is not None:
                existing = strict_json(existing_raw, label="publisher phase sidecar", maximum=64 * 1024)
                if (
                    not isinstance(existing, Mapping)
                    or canonical_json_bytes(existing) != existing_raw
                    or existing.get("schema_version") != PUBLISHER_PHASE_SCHEMA
                    or existing.get("phase") != phase
                    or existing.get("details") != detail_copy
                ):
                    raise StateConflict("publisher phase run slot already carries different bytes")
                return {
                    "record": dict(existing), "record_bytes": existing_raw,
                    "state_branch": STATE_BRANCH, "path": path, "commit_sha": head,
                    "bytes": len(existing_raw),
                    "record_sha256": hashlib.sha256(existing_raw).hexdigest(),
                    "created": False,
                }

            record = {
                "schema_version": PUBLISHER_PHASE_SCHEMA,
                "phase": phase,
                "subject_key": subject_key,
                "intent_id": intent_id,
                "dispatch_fence": fence,
                "run_id": run_id,
                "run_attempt": run_attempt,
                "recorded_at": self.clock().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "details": dict(detail_copy),
            }
            phase_raw = canonical_json_bytes(record)
            next_state = dict(state)
            if next_state["cas_writes"] >= 16:
                raise StateStoreError("publisher phase transition CAS budget is exhausted")
            next_state["cas_writes"] += 1
            next_state["evidence_refs"] = list(dict.fromkeys([*next_state["evidence_refs"], path]))[:24]
            history = list(next_state["history"])
            history.append({
                "state": next_state["state"], "observed_at": record["recorded_at"],
                "fence": fence, "detail": "publisher_worker_may_mutate_durably_recorded",
            })
            if len(history) > 32:
                raise StateStoreError("publisher phase transition history budget is exhausted")
            next_state["history"] = history
            next_state_raw = canonical_json_bytes(next_state)
            try:
                new_sha = self.git._commit_updates(
                    expected_sha=head, base_sha=self.base_sha,
                    updates={self._dispatch_path(subject_key): next_state_raw, path: phase_raw},
                    create_only=frozenset({path}),
                    message="Record canonical publisher prelaunch phase",
                )
            except StateConflict:
                continue
            state_readback = self.readback_state(new_sha, subject_key)
            phase_readback = self.git.read_file(new_sha, path, maximum=64 * 1024)
            if canonical_json_bytes(state_readback) != next_state_raw or phase_readback != phase_raw:
                raise StateStoreError("publisher phase transaction readback differs from exact bytes")
            self._observed_head[subject_key] = new_sha
            return {
                "record": record, "record_bytes": phase_raw,
                "state_branch": STATE_BRANCH, "path": path, "commit_sha": new_sha,
                "bytes": len(phase_raw),
                "record_sha256": hashlib.sha256(phase_raw).hexdigest(),
                "created": True,
            }
        raise StateConflict("publisher phase CAS retry budget exhausted")

    def register_manual_receiver(
        self, *, manual_execution_key: str, capability_sha256: str,
        workflow_sha: str, source_sha: str, run_id: int, run_attempt: int,
        status: str, conclusion: str | None,
    ) -> Mapping[str, Any]:
        """Create the separate durable receiver fence for one manual run.

        The execution key already binds repository, workflow/source commits,
        and the native run/attempt.  Its own immutable namespace therefore
        starts at fence 1 and never creates or mutates a C dispatch, lease,
        admission archive, intent, or spent slot.
        """
        execution_key = _key(manual_execution_key, "manual execution key")
        capability_sha256 = _key(capability_sha256, "manual capability SHA-256")
        if not SHA1_RE.fullmatch(workflow_sha) or not SHA1_RE.fullmatch(source_sha):
            raise StateStoreError("manual receiver workflow/source SHA is malformed")
        if status != "in_progress" or conclusion is not None:
            raise StateStoreError(
                "manual receiver requires the exact active publisher attempt",
            )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (run_id, run_attempt)
        ):
            raise StateStoreError("manual receiver run/attempt identity is malformed")
        path = self._manual_receiver_path(execution_key)
        identity = {
            "manual_execution_key": execution_key,
            "capability_sha256": capability_sha256,
            "repository": REPOSITORY,
            "workflow_path": ".github/workflows/huggingface-distribution.yml",
            "event": "workflow_dispatch",
            "ref": "refs/heads/main",
            "workflow_sha": workflow_sha,
            "source_sha": source_sha,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "status": status,
            "conclusion": conclusion,
            "manual_receiver_fence": 1,
        }
        for _ in range(self.max_cas_retries):
            existing = self._first_immutable_version(execution_key, path)
            if existing is not None:
                commit_sha, raw = existing
                record = strict_json(
                    raw, label="manual receiver registration", maximum=64 * 1024,
                )
                if (
                    not isinstance(record, Mapping)
                    or canonical_json_bytes(record) != raw
                    or record.get("schema_version") != MANUAL_RECEIVER_SCHEMA
                    or any(record.get(key) != value for key, value in identity.items())
                ):
                    raise StateConflict(
                        "manual execution key already has different receiver bytes",
                    )
                return {
                    "registered": True,
                    "manual_execution_key": execution_key,
                    "manual_receiver_fence": 1,
                    "run_id": run_id,
                    "run_attempt": run_attempt,
                    "state_branch": STATE_BRANCH,
                    "path": path,
                    "commit_sha": commit_sha,
                    "record_bytes": raw,
                    "bytes": len(raw),
                    "record_sha256": hashlib.sha256(raw).hexdigest(),
                    "created": False,
                }
            head = self._head()
            record = {
                "schema_version": MANUAL_RECEIVER_SCHEMA,
                **identity,
                "registered_at": self.clock().astimezone(timezone.utc).isoformat(
                    timespec="seconds",
                ).replace("+00:00", "Z"),
            }
            raw = canonical_json_bytes(record)
            try:
                commit_sha = self.git._commit_updates(
                    expected_sha=head,
                    base_sha=self.base_sha,
                    updates={path: raw},
                    create_only=frozenset({path}),
                    message="Register exact manual publisher receiver run",
                )
            except StateConflict:
                continue
            readback = self.git.read_file(commit_sha, path, maximum=64 * 1024)
            if readback != raw:
                raise StateStoreError(
                    "manual receiver registration readback differs from exact bytes",
                )
            return {
                "registered": True,
                "manual_execution_key": execution_key,
                "manual_receiver_fence": 1,
                "run_id": run_id,
                "run_attempt": run_attempt,
                "state_branch": STATE_BRANCH,
                "path": path,
                "commit_sha": commit_sha,
                "record_bytes": raw,
                "bytes": len(raw),
                "record_sha256": hashlib.sha256(raw).hexdigest(),
                "created": True,
            }
        raise StateConflict("manual receiver registration CAS retry budget exhausted")

    def read_manual_receiver_registration(
        self, manual_execution_key: str,
    ) -> Mapping[str, Any]:
        execution_key = _key(manual_execution_key, "manual execution key")
        path = self._manual_receiver_path(execution_key)
        item = self._first_immutable_version(execution_key, path)
        if item is None:
            raise StateStoreError("manual receiver registration sidecar is absent")
        commit_sha, raw = item
        record = strict_json(
            raw, label="manual receiver registration", maximum=64 * 1024,
        )
        if (
            not isinstance(record, Mapping)
            or canonical_json_bytes(record) != raw
            or record.get("schema_version") != MANUAL_RECEIVER_SCHEMA
            or record.get("manual_execution_key") != execution_key
            or type(record.get("manual_receiver_fence")) is not int
            or record.get("manual_receiver_fence") != 1
        ):
            raise StateStoreError(
                "manual receiver sidecar is not bound to its exact execution fence",
            )
        return {
            "record": dict(record),
            "record_bytes": raw,
            "state_branch": STATE_BRANCH,
            "path": path,
            "commit_sha": commit_sha,
            "bytes": len(raw),
            "record_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def record_manual_receiver_phase(
        self, *, manual_execution_key: str, capability_sha256: str,
        manual_receiver_fence: int, run_id: int, run_attempt: int,
        phase: str, details: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Spend one manual run's mutation boundary exactly once.

        An existing phase record, including byte-identical evidence from an
        uncertain prior launch, is a replay stop rather than an idempotent
        permission to launch another publisher child.
        """
        execution_key = _key(manual_execution_key, "manual execution key")
        capability_sha256 = _key(capability_sha256, "manual capability SHA-256")
        if type(manual_receiver_fence) is not int or manual_receiver_fence != 1:
            raise StateStoreError("manual receiver fence is outside its closed domain")
        if phase != "publisher_worker_may_mutate":
            raise StateStoreError("manual publisher phase is outside the closed prelaunch set")
        if not isinstance(details, Mapping):
            raise StateStoreError("manual publisher phase details must be an object")
        detail_copy = strict_json(
            canonical_json_bytes(dict(details)),
            label="manual publisher phase details", maximum=64 * 1024,
        )
        expected_keys = {
            "manual_execution_key", "capability_sha256", "manual_receiver_fence",
            "repository", "run_id", "run_attempt", "job_id", "workflow_sha",
            "source_sha", "entrypoint", "deadline_remaining_ms", "receipt_path",
        }
        if not isinstance(detail_copy, Mapping) or set(detail_copy) != expected_keys:
            raise StateStoreError("manual publisher phase details have unsupported fields")
        if (
            detail_copy.get("manual_execution_key") != execution_key
            or detail_copy.get("capability_sha256") != capability_sha256
            or detail_copy.get("manual_receiver_fence") != "1"
            or detail_copy.get("repository") != REPOSITORY
            or detail_copy.get("run_id") != str(run_id)
            or detail_copy.get("run_attempt") != str(run_attempt)
            or not SHA1_RE.fullmatch(str(detail_copy.get("workflow_sha", "")))
            or not SHA1_RE.fullmatch(str(detail_copy.get("source_sha", "")))
        ):
            raise StateStoreError("manual publisher phase differs from receiver identity")
        job_id = detail_copy.get("job_id")
        entrypoint = detail_copy.get("entrypoint")
        receipt_path = detail_copy.get("receipt_path")
        remaining_ms = detail_copy.get("deadline_remaining_ms")
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in (run_id, run_attempt)
            )
            or not isinstance(job_id, str) or not job_id
            or len(job_id.encode("utf-8")) > 128
            or not isinstance(entrypoint, str) or _safe_path(entrypoint) != entrypoint
            or not isinstance(receipt_path, str) or _safe_path(receipt_path) != receipt_path
            or isinstance(remaining_ms, bool) or not isinstance(remaining_ms, int)
            or not 0 <= remaining_ms <= 15 * 60 * 1000
        ):
            raise StateStoreError("manual publisher phase execution details are malformed")
        registration = self.read_manual_receiver_registration(execution_key)["record"]
        if (
            registration.get("capability_sha256") != capability_sha256
            or registration.get("manual_receiver_fence") != manual_receiver_fence
            or registration.get("run_id") != run_id
            or registration.get("run_attempt") != run_attempt
            or registration.get("workflow_sha") != detail_copy["workflow_sha"]
            or registration.get("source_sha") != detail_copy["source_sha"]
            or registration.get("status") != "in_progress"
            or registration.get("conclusion") is not None
        ):
            raise StateConflict(
                "manual publisher phase receiver differs from the exact active attempt",
            )
        path = self._manual_publisher_phase_path(execution_key)
        if self._first_immutable_version(execution_key, path) is not None:
            raise StateConflict(
                "manual publisher mutation boundary is already spent; replay is forbidden",
            )
        record = {
            "schema_version": MANUAL_PUBLISHER_PHASE_SCHEMA,
            "phase": phase,
            "manual_execution_key": execution_key,
            "capability_sha256": capability_sha256,
            "manual_receiver_fence": manual_receiver_fence,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "recorded_at": self.clock().astimezone(timezone.utc).isoformat(
                timespec="seconds",
            ).replace("+00:00", "Z"),
            "details": dict(detail_copy),
        }
        raw = canonical_json_bytes(record)
        for _ in range(self.max_cas_retries):
            head = self._head()
            if head is None:
                raise StateConflict("manual publisher phase requires the durable state branch")
            if self.git._tree_entry(head, path) is not None:
                raise StateConflict(
                    "manual publisher mutation boundary is already spent; replay is forbidden",
                )
            try:
                commit_sha = self.git._commit_updates(
                    expected_sha=head,
                    base_sha=self.base_sha,
                    updates={path: raw},
                    create_only=frozenset({path}),
                    message="Record manual publisher prelaunch phase",
                )
            except StateConflict:
                continue
            readback = self.git.read_file(commit_sha, path, maximum=64 * 1024)
            if readback != raw:
                raise StateStoreError(
                    "manual publisher phase readback differs from exact bytes",
                )
            return {
                "record": record,
                "record_bytes": raw,
                "state_branch": STATE_BRANCH,
                "path": path,
                "commit_sha": commit_sha,
                "bytes": len(raw),
                "record_sha256": hashlib.sha256(raw).hexdigest(),
                "created": True,
            }
        raise StateConflict("manual publisher phase CAS retry budget exhausted")

    @staticmethod
    def _terminal_locator(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {
            "state_branch", "path", "commit_sha", "bytes", "record_sha256",
        }:
            raise StateStoreError(f"{label} locator fields are unsupported")
        path = value.get("path")
        size = value.get("bytes")
        if (
            value.get("state_branch") != STATE_BRANCH
            or not isinstance(path, str) or _safe_path(path) != path
            or not SHA1_RE.fullmatch(str(value.get("commit_sha", "")))
            or isinstance(size, bool) or not isinstance(size, int)
            or not 1 <= size <= MAX_RECORD_BYTES
            or not SHA256_RE.fullmatch(str(value.get("record_sha256", "")))
        ):
            raise StateStoreError(f"{label} locator is malformed")
        return dict(value)

    @staticmethod
    def _terminal_context(role: str, value: Any) -> dict[str, Any]:
        common = {
            "repository", "workflow_path", "workflow_sha", "source_sha",
            "run_id", "run_attempt", "job_id",
        }
        if role == ROLE_AUTOMATIC_OWNED_C:
            expected = common | {
                "subject_key", "intent_id", "receiver_fence", "merged_sha",
            }
        elif role == ROLE_MANUAL_EXACT_SOURCE:
            expected = common | {
                "manual_execution_key", "capability_sha256",
                "manual_receiver_fence", "source_tree_sha",
            }
        else:
            raise StateStoreError("terminal observation role is unsupported")
        if not isinstance(value, Mapping) or set(value) != expected:
            raise StateStoreError("terminal observation context fields are unsupported")
        context = dict(value)
        if (
            context.get("repository") != REPOSITORY
            or context.get("workflow_path")
            != ".github/workflows/huggingface-distribution.yml"
            or not SHA1_RE.fullmatch(str(context.get("workflow_sha", "")))
            or not SHA1_RE.fullmatch(str(context.get("source_sha", "")))
            or any(
                isinstance(context.get(key), bool)
                or not isinstance(context.get(key), int)
                or context[key] < 1
                for key in ("run_id", "run_attempt")
            )
            or not isinstance(context.get("job_id"), str)
            or not context["job_id"] or "\x00" in context["job_id"]
            or len(context["job_id"].encode("utf-8")) > 128
        ):
            raise StateStoreError("terminal observation common context is malformed")
        if role == ROLE_AUTOMATIC_OWNED_C:
            if (
                not SHA256_RE.fullmatch(str(context.get("subject_key", "")))
                or not SHA256_RE.fullmatch(str(context.get("intent_id", "")))
                or not isinstance(context.get("receiver_fence"), str)
                or not re.fullmatch(r"[1-9][0-9]*", context["receiver_fence"])
                or not SHA1_RE.fullmatch(str(context.get("merged_sha", "")))
                or context["source_sha"] != context["merged_sha"]
            ):
                raise StateStoreError("terminal owned-C context is malformed")
        elif (
            not SHA256_RE.fullmatch(str(context.get("manual_execution_key", "")))
            or not SHA256_RE.fullmatch(str(context.get("capability_sha256", "")))
            or type(context.get("manual_receiver_fence")) is not int
            or context["manual_receiver_fence"] != 1
            or not SHA1_RE.fullmatch(str(context.get("source_tree_sha", "")))
        ):
            raise StateStoreError("terminal manual context is malformed")
        return context

    @staticmethod
    def _terminal_measurement(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {
            "path", "kind", "bytes", "sha256",
        }:
            raise StateStoreError(f"{label} measurement fields are unsupported")
        path = value.get("path")
        kind = value.get("kind")
        size = value.get("bytes")
        if (
            not isinstance(path, str) or _safe_path(path) != path
            or not isinstance(kind, str) or not kind or "\x00" in kind
            or len(kind.encode("utf-8")) > 128
            or isinstance(size, bool) or not isinstance(size, int)
            or not 1 <= size <= 1_000_000_000_000
            or not SHA256_RE.fullmatch(str(value.get("sha256", "")))
        ):
            raise StateStoreError(f"{label} measurement is malformed")
        return dict(value)

    @classmethod
    def _terminal_observation(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {
            "status", "reason", "observed_at", "evidence",
        }:
            raise StateStoreError("terminal observation fields are unsupported")
        observation = dict(value)
        status = observation.get("status")
        reason = observation.get("reason")
        observed_at = observation.get("observed_at")
        if (
            not isinstance(observed_at, str)
            or not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
                observed_at,
            )
        ):
            raise StateStoreError("terminal observation timestamp is malformed")
        evidence = observation.get("evidence")
        if status == "readback_verified_pending_native_completion":
            if reason != "verified_readback" or not isinstance(evidence, Mapping) or set(evidence) != {
                "witness", "pointer", "payload_revision", "source_binding", "records",
            }:
                raise StateStoreError("verified terminal evidence fields are unsupported")
            witness = evidence.get("witness")
            pointer = evidence.get("pointer")
            source_binding = evidence.get("source_binding")
            records = evidence.get("records")
            if (
                not isinstance(witness, Mapping)
                or set(witness) != {"path", "bytes", "sha256", "schema_version"}
                or witness.get("path")
                != "publication-preparation/.datapan/hf-publication-readback-witness.json"
                or isinstance(witness.get("bytes"), bool)
                or not isinstance(witness.get("bytes"), int)
                or not 1 <= witness["bytes"] <= MAX_TERMINAL_WITNESS_BYTES
                or not SHA256_RE.fullmatch(str(witness.get("sha256", "")))
                or witness.get("schema_version")
                not in {
                    "datapan.registry-publication-readback-witness.v1",
                    "datapan.registry-publication-readback-witness.v2",
                }
                or not isinstance(pointer, Mapping)
                or set(pointer) != {"path", "revision", "bytes", "sha256"}
                or pointer.get("path") != "release/distribution-manifest.json"
                or not SHA1_RE.fullmatch(str(pointer.get("revision", "")))
                or isinstance(pointer.get("bytes"), bool)
                or not isinstance(pointer.get("bytes"), int)
                or not 1 <= pointer["bytes"] <= 2 * 1024 * 1024
                or not SHA256_RE.fullmatch(str(pointer.get("sha256", "")))
                or not SHA1_RE.fullmatch(str(evidence.get("payload_revision", "")))
                or not isinstance(source_binding, Mapping)
                or set(source_binding) != {
                    "schema_version", "repository", "event_name", "git_ref",
                    "workflow_sha", "source_sha", "source_tree_sha",
                    "manifest_sha256", "status",
                }
                or source_binding.get("schema_version")
                != "datapan.registry-publication-source-binding.v1"
                or source_binding.get("repository") != REPOSITORY
                or source_binding.get("event_name") != "workflow_dispatch"
                or source_binding.get("git_ref") != "refs/heads/main"
                or source_binding.get("status") != "bound"
                or any(
                    not SHA1_RE.fullmatch(str(source_binding.get(key, "")))
                    for key in ("workflow_sha", "source_sha", "source_tree_sha")
                )
                or not SHA256_RE.fullmatch(str(source_binding.get("manifest_sha256", "")))
                or not isinstance(records, list) or not 2 <= len(records) <= 4096
            ):
                raise StateStoreError("verified terminal evidence is malformed")
            normalized_records = [
                cls._terminal_measurement(row, f"terminal readback record {index}")
                for index, row in enumerate(records)
            ]
            paths = [row["path"] for row in normalized_records]
            if paths != sorted(paths) or len(paths) != len(set(paths)):
                raise StateStoreError("terminal readback records are duplicate or unsorted")
            observation["evidence"] = {
                "witness": dict(witness), "pointer": dict(pointer),
                "payload_revision": evidence["payload_revision"],
                "source_binding": dict(source_binding), "records": normalized_records,
            }
            return observation
        reasons = {
            "missing_witness", "unavailable_readback", "pointer_changed",
            "invalid_witness", "insufficient_observation_budget",
        }
        if (
            status != "unconfirmed" or reason not in reasons
            or not isinstance(evidence, Mapping)
            or set(evidence) != {"witness", "pointer_revision", "payload_revision"}
        ):
            raise StateStoreError("unconfirmed terminal evidence fields are unsupported")
        witness = evidence.get("witness")
        if witness is not None and (
            not isinstance(witness, Mapping)
            or set(witness) != {"path", "bytes", "sha256"}
            or witness.get("path")
            != "publication-preparation/.datapan/hf-publication-readback-witness.json"
            or isinstance(witness.get("bytes"), bool)
            or not isinstance(witness.get("bytes"), int)
            or not 1 <= witness["bytes"] <= MAX_TERMINAL_WITNESS_BYTES
            or not SHA256_RE.fullmatch(str(witness.get("sha256", "")))
        ):
            raise StateStoreError("unconfirmed terminal witness identity is malformed")
        for key in ("pointer_revision", "payload_revision"):
            revision = evidence.get(key)
            if revision is not None and not SHA1_RE.fullmatch(str(revision)):
                raise StateStoreError("unconfirmed terminal revision is malformed")
        observation["evidence"] = {
            "witness": dict(witness) if isinstance(witness, Mapping) else None,
            "pointer_revision": evidence.get("pointer_revision"),
            "payload_revision": evidence.get("payload_revision"),
        }
        return observation

    def _read_phase_reference(
        self, *, role: str, context: Mapping[str, Any], required: bool,
    ) -> Mapping[str, Any] | None:
        if role == ROLE_AUTOMATIC_OWNED_C:
            stable_key = context["subject_key"]
            path = self._publisher_phase_path(
                stable_key, context["run_id"], context["run_attempt"],
            )
            schema = PUBLISHER_PHASE_SCHEMA
        else:
            stable_key = context["manual_execution_key"]
            path = self._manual_publisher_phase_path(stable_key)
            schema = MANUAL_PUBLISHER_PHASE_SCHEMA
        item = self._first_immutable_version(stable_key, path)
        if item is None:
            if required:
                raise StateConflict(
                    "verified terminal observation requires the durable prelaunch phase",
                )
            return None
        commit_sha, raw = item
        record = strict_json(raw, label="publisher phase readback", maximum=64 * 1024)
        if (
            not isinstance(record, Mapping)
            or canonical_json_bytes(record) != raw
            or record.get("schema_version") != schema
            or record.get("phase") != "publisher_worker_may_mutate"
        ):
            raise StateStoreError("publisher phase sidecar is malformed")
        details = record.get("details")
        if not isinstance(details, Mapping):
            raise StateStoreError("publisher phase sidecar lacks exact details")
        if role == ROLE_AUTOMATIC_OWNED_C:
            if any(details.get(key) != value for key, value in {
                "subject_key": context["subject_key"],
                "intent_id": context["intent_id"],
                "receiver_fence": context["receiver_fence"],
                "workflow_sha": context["workflow_sha"],
                "source_sha": context["source_sha"],
                "merged_sha": context["merged_sha"],
                "run_id": str(context["run_id"]),
                "run_attempt": str(context["run_attempt"]),
            }.items()):
                raise StateConflict("publisher phase differs from terminal owned-C context")
        elif any(details.get(key) != value for key, value in {
            "manual_execution_key": context["manual_execution_key"],
            "capability_sha256": context["capability_sha256"],
            "manual_receiver_fence": "1",
            "workflow_sha": context["workflow_sha"],
            "source_sha": context["source_sha"],
            "run_id": str(context["run_id"]),
            "run_attempt": str(context["run_attempt"]),
        }.items()):
            raise StateConflict("publisher phase differs from terminal manual context")
        return {
            "state_branch": STATE_BRANCH, "path": path, "commit_sha": commit_sha,
            "bytes": len(raw), "record_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def read_terminal_observation(
        self, *, role: str, stable_key: str,
    ) -> Mapping[str, Any] | None:
        path = self._terminal_observation_path(role, stable_key)
        item = self._first_immutable_version(stable_key, path)
        if item is None:
            return None
        commit_sha, raw = item
        record = strict_json(raw, label="terminal observation", maximum=MAX_RECORD_BYTES)
        if (
            not isinstance(record, Mapping)
            or canonical_json_bytes(record) != raw
            or record.get("schema_version") != TERMINAL_OBSERVATION_SCHEMA
            or record.get("role") != role
        ):
            raise StateStoreError("terminal observation sidecar is malformed")
        context = self._terminal_context(role, record.get("context"))
        expected_key = (
            context["subject_key"] if role == ROLE_AUTOMATIC_OWNED_C
            else context["manual_execution_key"]
        )
        if expected_key != stable_key:
            raise StateStoreError("terminal observation path differs from its role key")
        self._terminal_locator(record.get("registration"), "terminal registration")
        phase = record.get("phase")
        if phase is not None:
            self._terminal_locator(phase, "terminal phase")
        self._terminal_observation(record.get("observation"))
        return {
            "record": dict(record), "record_bytes": raw,
            "state_branch": STATE_BRANCH, "path": path, "commit_sha": commit_sha,
            "bytes": len(raw), "record_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def record_terminal_observation(
        self, *, role: str, context: Mapping[str, Any],
        registration: Mapping[str, Any], phase: Mapping[str, Any] | None,
        observation: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        context_copy = self._terminal_context(role, context)
        registration_copy = self._terminal_locator(
            registration, "terminal registration",
        )
        observation_copy = self._terminal_observation(observation)
        stable_key = (
            context_copy["subject_key"] if role == ROLE_AUTOMATIC_OWNED_C
            else context_copy["manual_execution_key"]
        )
        if role == ROLE_AUTOMATIC_OWNED_C:
            fresh_registration = self.read_receiver_registration(
                stable_key, context_copy["run_id"], context_copy["run_attempt"],
            )
            registration_record = fresh_registration["record"]
            if (
                registration_record.get("intent_id") != context_copy["intent_id"]
                or str(registration_record.get("dispatch_fence"))
                != context_copy["receiver_fence"]
                or registration_record.get("workflow_sha") != context_copy["workflow_sha"]
                or registration_record.get("source_sha") != context_copy["source_sha"]
            ):
                raise StateConflict("terminal owned-C registration differs from context")
        else:
            fresh_registration = self.read_manual_receiver_registration(stable_key)
            registration_record = fresh_registration["record"]
            if (
                registration_record.get("capability_sha256")
                != context_copy["capability_sha256"]
                or type(registration_record.get("manual_receiver_fence")) is not int
                or registration_record.get("manual_receiver_fence") != 1
                or registration_record.get("workflow_sha") != context_copy["workflow_sha"]
                or registration_record.get("source_sha") != context_copy["source_sha"]
                or registration_record.get("run_id") != context_copy["run_id"]
                or registration_record.get("run_attempt") != context_copy["run_attempt"]
            ):
                raise StateConflict("terminal manual registration differs from context")
        fresh_locator = {
            key: fresh_registration[key]
            for key in ("state_branch", "path", "commit_sha", "bytes", "record_sha256")
        }
        if registration_copy != fresh_locator:
            raise StateConflict("terminal registration locator changed before persistence")
        required_phase = (
            observation_copy["status"]
            == "readback_verified_pending_native_completion"
        )
        fresh_phase = self._read_phase_reference(
            role=role, context=context_copy, required=required_phase,
        )
        phase_copy = (
            self._terminal_locator(phase, "terminal phase")
            if phase is not None else None
        )
        if phase_copy != fresh_phase:
            raise StateConflict("terminal phase locator changed before persistence")
        record = {
            "schema_version": TERMINAL_OBSERVATION_SCHEMA,
            "role": role,
            "context": context_copy,
            "registration": registration_copy,
            "phase": phase_copy,
            "observation": observation_copy,
        }
        raw = canonical_json_bytes(record)
        if not raw or len(raw) > MAX_RECORD_BYTES:
            raise StateStoreError("terminal observation exceeds its fixed metadata bound")
        path = self._terminal_observation_path(role, stable_key)
        for _ in range(self.max_cas_retries):
            existing = self.read_terminal_observation(
                role=role, stable_key=stable_key,
            )
            if existing is not None:
                if existing["record_bytes"] != raw:
                    raise StateConflict(
                        "terminal observation already carries different immutable bytes",
                    )
                return {**existing, "created": False}
            head = self._head()
            if head is None:
                raise StateConflict("terminal observation requires the durable state branch")
            try:
                commit_sha = self.git._commit_updates(
                    expected_sha=head, base_sha=self.base_sha,
                    updates={path: raw}, create_only=frozenset({path}),
                    message="Record bounded publisher terminal observation",
                )
            except StateConflict:
                continue
            readback = self.git.read_file(commit_sha, path, maximum=MAX_RECORD_BYTES)
            if readback != raw:
                raise StateStoreError(
                    "terminal observation readback differs from exact committed bytes",
                )
            return {
                "record": record, "record_bytes": raw,
                "state_branch": STATE_BRANCH, "path": path,
                "commit_sha": commit_sha, "bytes": len(raw),
                "record_sha256": hashlib.sha256(raw).hexdigest(),
                "created": True,
            }
        raise StateConflict("terminal observation CAS retry budget exhausted")
