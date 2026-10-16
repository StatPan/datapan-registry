"""Private broker for tokenless Git work in a Native-owned operation.

This module is intentionally not a CLI or a general subprocess API. Production
issuance is limited to the exact initial-C Native context and its verified
release CONTROL. Git execution and accounting remain owned by the one existing
``GitObjectStore`` and its ``StateBudget``.
"""

from __future__ import annotations

import hashlib
import base64
import importlib
import json
import math
import os
import pathlib
import re
import selectors
import secrets
import socket
import stat
import struct
import sys
import threading
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


_IDENTITY_TOKEN = object()
_REPOSITORY_TOKEN = object()
_OWNER_TOKEN = object()
_LEDGER_TOKEN = object()
_CHILD_BOOTSTRAP_TOKEN = object()
_FIXTURE_REPOSITORY_TOKEN = object()
_SHA1 = re.compile(r"^[a-f0-9]{40}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_HEX32 = re.compile(r"^[a-f0-9]{32}$")
_MAX_CHILD_FRAME_BYTES = 64 * 1024
_MAX_CHILD_METADATA_RESULT_BYTES = 2 * 1024 * 1024 + 64 * 1024
_MAX_CHILD_SOURCE_GRAPH_BYTES = 8 * 1024 * 1024
_MAX_CHILD_METADATA_FILE_BYTES = 8 * 1024 * 1024
_MAX_CHILD_PAYLOAD_BYTES = 512 * 1024 * 1024
_MAX_CHILD_PAYLOAD_RESULT_BYTES = _MAX_CHILD_PAYLOAD_BYTES + 64 * 1024
_MAX_CHILD_INPUT_BYTES = 512 * 1024 * 1024
_MAX_CHILD_INPUT_LIST_BYTES = 64 * 1024
_MAX_STATE_GIT_OUTPUT_BYTES = 192 * 1024 * 1024
_CHILD_RESULT_CHUNK_BYTES = 48 * 1024
_CHILD_PROTOCOL_VERSION = 4
_MAX_GIT_METADATA_ENTRIES = 32_768
_MAX_GIT_METADATA_DEPTH = 32
_MAX_GIT_METADATA_PATH_BYTES = 4096
_MODULE_NAME = "canonical_publication_git_owner"
_STATE_MODULE_NAME = "canonical_publication_state"
_NATIVE_MODULE_NAME = "canonical_publication_native"
_CHILD_BOOTSTRAP_MARKERS = (
    "DATAPAN_NATIVE_GIT_FD",
    "DATAPAN_NATIVE_GIT_OPERATION_ID",
    "DATAPAN_NATIVE_GIT_CHILD_ID",
    "DATAPAN_NATIVE_GIT_NONCE",
    "DATAPAN_NATIVE_GIT_REPOSITORY_ID",
    "DATAPAN_NATIVE_GIT_OWNER_PID",
    "DATAPAN_NATIVE_GIT_DEADLINE_NS",
    "DATAPAN_NATIVE_GIT_COMMAND_ID",
    "DATAPAN_NATIVE_SOURCE_PACKAGE_SHA256",
    "DATAPAN_NATIVE_SOURCE_BUNDLE_SHA256",
)
_CHILD_SESSION_LOCK = threading.RLock()
_CHILD_SESSION_SINGLETON: "_ChildGitSession | None" = None
_CHILD_SESSION_PID: int | None = None
_CHILD_SESSION_MARKERS: tuple[tuple[str, str | None], ...] | None = None
_CHILD_SESSION_INITIALIZED = False
_CHILD_SESSION_TERMINAL = False
_CHILD_SESSION_IMPORT_PID = os.getpid()
_OWNER_REGISTRY_LOCK = threading.RLock()
_OWNERS_BY_CONTEXT_ID: dict[int, tuple[object, "_NativeGitOperationOwner"]] = {}


class NativeGitOwnerError(RuntimeError):
    """A bounded, fail-closed local Git owner operation failed."""


class _LocalRepositoryRole(str, Enum):
    CONTROL_CHECKOUT = "control_checkout"
    PREPARED_RELEASE = "prepared_release"
    VERIFIED_CLI = "verified_cli"
    DISPOSABLE_FIXTURE = "disposable_fixture"


class FixtureRepositoryKind(str, Enum):
    WORKING = "working"
    BARE = "bare"
    PATH = "path"
    DIAGNOSTIC_LAYOUT = "diagnostic_layout"


class FixtureGitVerb(str, Enum):
    INIT_WORKING = "init_working"
    INIT_WORKING_MAIN = "init_working_main"
    INIT_BARE = "init_bare"
    CONFIG_IDENTITY = "config_identity"
    CONFIG_FIXED = "config_fixed"
    ADD = "add"
    COMMIT = "commit"
    HASH_OBJECT = "hash_object"
    WRITE_TREE = "write_tree"
    COMMIT_TREE = "commit_tree"
    UPDATE_REF = "update_ref"
    SYMBOLIC_HEAD = "symbolic_head"
    READ_TREE = "read_tree"
    CHECKOUT = "checkout"
    CHECKOUT_REF = "checkout_ref"
    SWITCH_BRANCH = "switch_branch"
    SWITCH_ORPHAN = "switch_orphan"
    DELETE_BRANCH = "delete_branch"
    DELETE_REF = "delete_ref"
    DELETE_SYMBOLIC_REF = "delete_symbolic_ref"
    REMOTE_ADD = "remote_add"
    WORKTREE_ADD = "worktree_add"
    WORKTREE_REMOVE = "worktree_remove"
    CLONE_SHARED = "clone_shared"
    CLONE_SHARED_CHECKOUT = "clone_shared_checkout"
    CLONE_FILE_DEPTH1 = "clone_file_depth1"
    FETCH = "fetch"
    FETCH_COMMIT = "fetch_commit"
    FETCH_EXACT_REF_64 = "fetch_exact_ref_64"
    FETCH_DEEPEN = "fetch_deepen"
    PUSH = "push"
    SHOW_BLOB = "show_blob"
    SHOW_BLOB_INFO = "show_blob_info"
    SHOW_BLOB_PAYLOAD = "show_blob_payload"
    REV_PARSE_HEAD = "rev_parse_head"
    REV_PARSE_TREE = "rev_parse_tree"
    TRUSTED_ORIGIN_MAIN = "trusted_origin_main"
    IS_ANCESTOR = "is_ancestor"
    IS_SHALLOW = "is_shallow"
    GIT_PATH = "git_path"
    COMMON_DIR = "common_dir"
    ALTERNATES = "alternates"
    SHALLOW_BYTES = "shallow_bytes"
    REV_LIST = "rev_list"
    CAT_FILE_EXISTS = "cat_file_exists"
    SHOW_REF = "show_ref"
    SHOW_REFS = "show_refs"
    WRITE_ALTERNATES = "write_alternates"
    WRITE_UNRELATED_ALTERNATES = "write_unrelated_alternates"
    WRITE_SHALLOW = "write_shallow"
    WRITE_SHALLOW_FROM_SOURCE = "write_shallow_from_source"


@dataclass(frozen=True, slots=True)
class ClosedFixtureGitRequest:
    """Finite fixture operation; repository authority is carried by exact caps."""

    verb: FixtureGitVerb
    source: "ChildFixtureRepository | ChildSourceRepository | None" = None
    destination: "ChildFixtureRepository | None" = None
    ref: str | None = None
    refspec: str | None = None
    depth: int | None = None
    commit_sha: str | None = None
    object_type: str | None = None
    parent_sha: str | None = None
    ancestor_sha: str | None = None
    descendant_sha: str | None = None
    revision: str | None = None
    path: str | None = None
    paths: tuple[str, ...] = ()
    message: str | None = None
    content: bytes | None = field(default=None, repr=False, compare=False)
    key: str | None = None
    value: str | None = None


class ChildFixtureRepository:
    """One disposable repo/path allocated by the registered test child."""

    __slots__ = (
        "_session", "_repository_id", "_root", "_kind", "_root_identity",
        "_workspace", "_workspace_identity", "_token",
    )

    def __init__(
        self, *, session: "_ChildGitSession", repository_id: str,
        root: pathlib.Path, kind: FixtureRepositoryKind,
        root_identity: tuple[int, int, int, int] | None,
        workspace: pathlib.Path, workspace_identity: tuple[int, int, int, int],
        _token: object,
    ) -> None:
        if _token is not _CHILD_FIXTURE_CAP_TOKEN:
            raise TypeError("fixture repository capabilities are broker-issued only")
        self._session = session
        self._repository_id = repository_id
        self._root = root
        self._kind = kind
        self._root_identity = root_identity
        self._workspace = workspace
        self._workspace_identity = workspace_identity
        self._token = _token

    @property
    def root(self) -> pathlib.Path:
        return self._root

    @property
    def repository_id(self) -> str:
        return self._repository_id

    @property
    def kind(self) -> FixtureRepositoryKind:
        return self._kind

    @property
    def root_identity(self) -> tuple[int, int, int, int] | None:
        return self._root_identity


class ChildSourceRepository:
    """Read-only view of the Native HELLO-bound prepared source repository."""

    __slots__ = ("_session", "_repository_id", "_root", "_root_identity", "_token")

    def __init__(
        self, *, session: "_ChildGitSession", repository_id: str,
        root: pathlib.Path, root_identity: tuple[int, int, int, int],
        _token: object,
    ) -> None:
        if _token is not _CHILD_SOURCE_CAP_TOKEN:
            raise TypeError("source repository capabilities are session-issued only")
        self._session = session
        self._repository_id = repository_id
        self._root = root
        self._root_identity = root_identity
        self._token = _token


class ChildDiagnosticCloneReceipt:
    """Opaque one-use evidence that an exact diagnostic shared clone succeeded."""

    __slots__ = (
        "_session", "_receipt_id", "_layout_id", "_source_id",
        "_destination_id", "_used", "_token",
    )

    def __init__(
        self, *, session: "_ChildGitSession", receipt_id: str,
        layout_id: str, source_id: str, destination_id: str, _token: object,
    ) -> None:
        if _token is not _DIAGNOSTIC_CLONE_TOKEN:
            raise TypeError("diagnostic clone receipts are broker-issued only")
        self._session = session
        self._receipt_id = receipt_id
        self._layout_id = layout_id
        self._source_id = source_id
        self._destination_id = destination_id
        self._used = False
        self._token = _token


_CHILD_SOURCE_CAP_TOKEN = object()


_CHILD_FIXTURE_CAP_TOKEN = object()
_DIAGNOSTIC_CLONE_TOKEN = object()
_CHILD_PREPARED_INPUT_READER_TOKEN = object()


class _ChildPreparedInputReader:
    """Child-side adapter for one State-issued prepared-scratch input epoch."""

    __slots__ = ("_session", "root", "_token")

    def __init__(self, session: "_ChildGitSession", *, _token: object) -> None:
        if _token is not _CHILD_PREPARED_INPUT_READER_TOKEN:
            raise TypeError("child prepared-input reader is owner-issued")
        if (
            type(session) is not _ChildGitSession
            or session._prepared_input_enabled is not True
            or session._root_asserted is not True
            or session._repository_role != _LocalRepositoryRole.PREPARED_RELEASE.value
            or session._repository_root is None
        ):
            raise NativeGitOwnerError("prepared-input reader lacks its exact prepared-release HELLO")
        self._session = session
        self.root = session._repository_root
        self._token = _token

    def read_bytes(self, relative_path: str) -> bytes:
        if type(relative_path) is not str or _safe_path(relative_path) != relative_path:
            raise NativeGitOwnerError("prepared-input read path is not canonical")
        value = self._session._request_prepared_input(
            operation="read_bytes", path=relative_path,
        )
        if value is None:
            raise FileNotFoundError(relative_path)
        if type(value) is not bytes:
            raise NativeGitOwnerError("prepared-input read returned a foreign value")
        return value

    def list_paths(
        self, *, prefix: str = "sources/", suffix: str = ".json",
    ) -> tuple[str, ...]:
        if prefix != "sources/" or suffix != ".json":
            raise NativeGitOwnerError("prepared-input reader supports only the complete source inventory")
        value = self._session._request_prepared_input(
            operation="list_paths", prefix=prefix, suffix=suffix,
        )
        if type(value) is not tuple:
            raise NativeGitOwnerError("prepared-input inventory returned a foreign value")
        return value


@dataclass(frozen=True, slots=True)
class _ChildGitResult:
    """Typed child view of one broker-executed local Git request."""

    returncode: int
    stdout: bytes
    stderr: bytes
    execution_ordinal: int
    commands_before: int
    commands_after: int
    output_bytes_before: int
    output_bytes_after: int
    started_monotonic_ns: int
    finished_monotonic_ns: int
    process_id: int
    process_group_id: int
    cleanup_confirmed: bool
    argv_trace: tuple[str, ...]
    fixture_write_kind: str | None = None
    fixture_write_bytes: int = 0
    metadata_kind: str | None = None
    metadata_present: bool = False
    metadata_bytes: bytes = field(default=b"", repr=False, compare=False)
    diagnostic_clone_receipt: ChildDiagnosticCloneReceipt | None = field(
        default=None, repr=False, compare=False,
    )


class _RegisteredGitChild:
    """One Native-spawned process and its one-use authenticated Git channel."""

    __slots__ = (
        "child_id", "repository", "bootstrap", "channel", "process",
        "pid", "process_group_id", "session_id", "start_ticks", "uid",
        "hello_complete", "next_sequence", "pending_sequence",
        "pending_request_id", "pending_request_sha256", "pending_packets",
        "goodbye_complete", "closed", "command", "fixture_workspace",
        "launch_argv", "inner_command_id", "source_package_sha256",
        "source_bundle_sha256",
        "fixture_workspace_identity", "fixture_capabilities", "fixture_roots",
        "source_snapshot_binding", "source_snapshot_refs", "source_snapshot_head",
        "diagnostic_clone_receipts", "pending_diagnostic_clone_receipt_id",
        "prepared_input_reader",
    )

    def __init__(
        self, *, child_id: str, repository: "_LocalRepositoryCapability",
        bootstrap: "_GitChildBootstrap", process: object,
        pid: int, process_group_id: int, session_id: int, start_ticks: int,
        uid: int,
        command: object | None = None,
        launch_argv: tuple[str, ...] | None = None,
        source_package_sha256: str | None = None,
        source_bundle_sha256: str | None = None,
        fixture_workspace: pathlib.Path | None = None,
        fixture_workspace_identity: tuple[int, int, int, int] | None = None,
        prepared_input_reader: Any | None = None,
    ) -> None:
        self.child_id = child_id
        self.repository = repository
        self.bootstrap = bootstrap
        self.channel = bootstrap._parent_socket
        self.process = process
        self.pid = pid
        self.process_group_id = process_group_id
        self.session_id = session_id
        self.start_ticks = start_ticks
        self.uid = uid
        self.hello_complete = False
        self.next_sequence = 1
        self.pending_sequence: int | None = None
        self.pending_request_id: str | None = None
        self.pending_request_sha256: str | None = None
        self.pending_packets: tuple[bytes, ...] = ()
        self.goodbye_complete = False
        self.closed = False
        self.command = command
        self.launch_argv = launch_argv
        self.inner_command_id = getattr(command, "command_id", None)
        self.source_package_sha256 = source_package_sha256
        self.source_bundle_sha256 = source_bundle_sha256
        self.fixture_workspace = fixture_workspace
        self.fixture_workspace_identity = fixture_workspace_identity
        self.fixture_capabilities: dict[str, _FixtureRepositoryCapability] = {}
        self.fixture_roots: list[tuple[pathlib.Path, tuple[int, int, int, int] | None]] = []
        self.source_snapshot_binding: tuple[Any, ...] | None = None
        self.source_snapshot_refs: bytes | None = None
        self.source_snapshot_head: bytes | None = None
        self.diagnostic_clone_receipts: dict[str, _OwnedDiagnosticCloneReceipt] = {}
        self.pending_diagnostic_clone_receipt_id: str | None = None
        self.prepared_input_reader = prepared_input_reader


class _OwnedDiagnosticCloneReceipt:
    __slots__ = (
        "receipt_id", "layout", "source", "destination", "commit_sha", "tree_sha",
        "used", "token",
    )

    def __init__(
        self, *, receipt_id: str, layout: "_FixtureRepositoryCapability",
        source: "_FixtureRepositoryCapability | _LocalRepositoryCapability",
        destination: "_FixtureRepositoryCapability", commit_sha: str, tree_sha: str,
    ) -> None:
        self.receipt_id = receipt_id
        self.layout = layout
        self.source = source
        self.destination = destination
        self.commit_sha = commit_sha
        self.tree_sha = tree_sha
        self.used = False
        self.token = _DIAGNOSTIC_CLONE_TOKEN


class _FixtureRepositoryCapability:
    """Parent-side fixture cap rooted beneath one registered test child."""

    __slots__ = (
        "_owner_identity", "_child_id", "_repository_id", "_root",
        "_workspace", "_workspace_identity", "_root_identity", "_kind",
        "_token", "_relations", "_known_objects", "_verified_blob_facts",
        "_diagnostic_layout", "_diagnostic_role",
    )

    def __init__(
        self, *, owner_identity: "_NativeGitOperationIdentity", child_id: str,
        repository_id: str, root: pathlib.Path, workspace: pathlib.Path,
        workspace_identity: tuple[int, int, int, int],
        root_identity: tuple[int, int, int, int] | None,
        kind: FixtureRepositoryKind, _token: object,
        diagnostic_layout: "_FixtureRepositoryCapability | None" = None,
        diagnostic_role: str | None = None,
    ) -> None:
        if _token is not _FIXTURE_REPOSITORY_TOKEN:
            raise TypeError("fixture repository capabilities are owner-issued only")
        self._owner_identity = owner_identity
        self._child_id = child_id
        self._repository_id = repository_id
        self._root = root
        self._workspace = workspace
        self._workspace_identity = workspace_identity
        self._root_identity = root_identity
        self._kind = kind
        self._token = _token
        self._relations: dict[str, "_FixtureRepositoryCapability"] = {}
        self._known_objects: set[str] = set()
        self._verified_blob_facts: dict[tuple[str, str], tuple[str, int]] = {}
        self._diagnostic_layout = diagnostic_layout
        self._diagnostic_role = diagnostic_role


class _GitChildBootstrap:
    """One-use Native launch handoff; contains no command, path, or environment."""

    __slots__ = (
        "_owner", "_operation_id", "_child_id", "_nonce", "_repository_id",
        "_parent_socket", "_child_socket", "_consumed", "_command_id",
        "_source_package_sha256", "_source_bundle_sha256", "_token",
    )

    def __init__(
        self, *, owner: "_NativeGitOperationOwner", child_id: str,
        nonce: str, repository: "_LocalRepositoryCapability",
        parent_socket: socket.socket, child_socket: socket.socket,
        _token: object,
    ) -> None:
        if _token is not _CHILD_BOOTSTRAP_TOKEN:
            raise TypeError("Git child bootstraps are broker-issued only")
        self._owner = owner
        self._operation_id = owner._identity._operation_id
        self._child_id = child_id
        self._nonce = nonce
        self._repository_id = repository._repository_id
        self._parent_socket = parent_socket
        self._child_socket = child_socket
        self._consumed = False
        self._command_id: str | None = None
        self._source_package_sha256: str | None = None
        self._source_bundle_sha256: str | None = None
        self._token = _token

    @property
    def child_fd(self) -> int:
        self._require_unused()
        return self._child_socket.fileno()

    @property
    def child_id(self) -> str:
        self._require_unused()
        return self._child_id

    @property
    def repository_id(self) -> str:
        self._require_unused()
        return self._repository_id

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self.child_fd,)

    @property
    def environment(self) -> MappingProxyType:
        self._require_unused()
        if self._command_id is None:
            raise NativeGitOwnerError("Native child launch identity was not bound before spawn")
        return MappingProxyType({
            "DATAPAN_NATIVE_GIT_FD": str(self._child_socket.fileno()),
            "DATAPAN_NATIVE_GIT_OPERATION_ID": self._operation_id,
            "DATAPAN_NATIVE_GIT_CHILD_ID": self._child_id,
            "DATAPAN_NATIVE_GIT_NONCE": self._nonce,
            "DATAPAN_NATIVE_GIT_REPOSITORY_ID": self._repository_id,
            "DATAPAN_NATIVE_GIT_OWNER_PID": str(os.getpid()),
            "DATAPAN_NATIVE_GIT_DEADLINE_NS": str(
                int(self._owner._identity._deadline_monotonic * 1_000_000_000)
            ),
            "DATAPAN_NATIVE_GIT_COMMAND_ID": self._command_id,
            "DATAPAN_NATIVE_SOURCE_PACKAGE_SHA256": self._source_package_sha256 or "",
            "DATAPAN_NATIVE_SOURCE_BUNDLE_SHA256": self._source_bundle_sha256 or "",
        })

    def _bind_native_launch_identity(
        self, *, command_id: str,
        source_package_sha256: str | None,
        source_bundle_sha256: str | None,
    ) -> None:
        self._require_unused()
        if (
            self._command_id is not None
            or type(command_id) is not str or not _SHA256.fullmatch(command_id)
            or (source_package_sha256 is None) != (source_bundle_sha256 is None)
            or source_package_sha256 is not None and (
                type(source_package_sha256) is not str
                or not _SHA256.fullmatch(source_package_sha256)
                or type(source_bundle_sha256) is not str
                or not _SHA256.fullmatch(source_bundle_sha256)
            )
        ):
            raise NativeGitOwnerError("Native child outer/inner launch binding is malformed or repeated")
        self._command_id = command_id
        self._source_package_sha256 = source_package_sha256
        self._source_bundle_sha256 = source_bundle_sha256

    def _require_unused(self) -> None:
        if (
            self._token is not _CHILD_BOOTSTRAP_TOKEN
            or self._consumed is not False
            or self._owner._identity._active is not True
        ):
            raise NativeGitOwnerError("one-use Git child bootstrap is revoked")

    def _consume_after_spawn(self, *, owner: "_NativeGitOperationOwner") -> None:
        self._require_unused()
        if owner is not self._owner or self._command_id is None:
            raise NativeGitOwnerError("Git child bootstrap was returned to a foreign owner")
        self._consumed = True
        self._child_socket.close()

    def _close_unconsumed(self) -> None:
        if self._consumed is False:
            self._consumed = True
            self._child_socket.close()
            self._parent_socket.close()


@dataclass(frozen=True, slots=True)
class _ClosedLocalGitRequest:
    """Closed Git request; no argv, cwd, environment, or caller bounds."""

    verb: str
    commit_sha: str | None = None
    path: str | None = None
    ancestor_sha: str | None = None
    descendant_sha: str | None = None
    stdin_bytes: bytes | None = field(default=None, repr=False, compare=False)
    _child_registration: Any | None = field(default=None, repr=False, compare=False)


# Exact typed request constructor consumed by the adjacent Native source. It
# remains a closed request with no caller argv, path, environment, or bounds.
ClosedLocalGitRequest = _ClosedLocalGitRequest


@dataclass(frozen=True, slots=True)
class _GitExecutionLedgerEntry:
    operation_kind: str
    repository_id: str
    verb: str
    request_sha256: str
    returncode: int
    stdout_bytes: int
    stdout_sha256: str
    stderr_bytes: int
    stderr_sha256: str
    execution_ordinal: int
    commands_before: int
    commands_after: int
    output_bytes_before: int
    output_bytes_after: int
    started_monotonic_ns: int
    finished_monotonic_ns: int
    process_id: int
    process_group_id: int
    cleanup_confirmed: bool


class _SealedGitExecutionLedger:
    """Immutable owner-issued audit of the physical local Git launches."""

    __slots__ = (
        "_operation_id", "_workflow_sha", "_tree_sha", "_entries",
        "_commands_before", "_commands_after", "_output_bytes_before",
        "_output_bytes_after", "_token", "_owner_identity",
    )

    def __init__(
        self, *, owner_identity: "_NativeGitOperationIdentity",
        entries: tuple[_GitExecutionLedgerEntry, ...], _token: object,
    ) -> None:
        if _token is not _LEDGER_TOKEN:
            raise TypeError("local Git execution ledgers are broker-issued only")
        self._owner_identity = owner_identity
        self._operation_id = owner_identity._operation_id
        self._workflow_sha = owner_identity._workflow_sha
        self._tree_sha = owner_identity._tree_sha
        self._entries = tuple(entries)
        self._commands_before = owner_identity._commands_before
        self._commands_after = owner_identity._budget.commands
        self._output_bytes_before = owner_identity._output_bytes_before
        self._output_bytes_after = owner_identity._budget.output_bytes
        self._token = _token

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def workflow_sha(self) -> str:
        return self._workflow_sha

    @property
    def tree_sha(self) -> str:
        return self._tree_sha

    @property
    def entries(self) -> tuple[_GitExecutionLedgerEntry, ...]:
        return self._entries

    @property
    def command_count(self) -> int:
        return self._commands_after - self._commands_before

    @property
    def transport_bytes(self) -> int:
        return self._output_bytes_after - self._output_bytes_before


class _NativeGitOperationIdentity:
    """Private identity shared by one exact Native context and Git store."""

    __slots__ = (
        "_context", "_control", "_git", "_budget", "_deadline_monotonic",
        "_workflow_sha", "_tree_sha", "_operation_id", "_active", "_seal",
        "_owner", "_repository_capabilities", "_commands_before",
        "_output_bytes_before", "_fixture_only",
    )

    def __init__(
        self, *, context: object, control: object, git: object,
        deadline_monotonic: float, workflow_sha: str, tree_sha: str,
        operation_id: str, _token: object, fixture_only: bool = False,
    ) -> None:
        if _token is not _IDENTITY_TOKEN:
            raise TypeError("Native Git operation identities are issuer-only")
        self._context = context
        self._control = control
        self._git = git
        self._budget = git.budget
        self._deadline_monotonic = deadline_monotonic
        self._workflow_sha = workflow_sha
        self._tree_sha = tree_sha
        self._operation_id = operation_id
        self._active = True
        self._seal = object()
        self._owner: _NativeGitOperationOwner | None = None
        self._repository_capabilities: dict[str, _LocalRepositoryCapability] = {}
        self._commands_before = git.budget.commands
        self._output_bytes_before = git.budget.output_bytes
        self._fixture_only = fixture_only


class _LocalRepositoryCapability:
    """Owner-issued local repository identity with a closed read surface."""

    __slots__ = (
        "_owner_identity", "_repository_id", "_role", "_root",
        "_boundary_root", "_allowed_paths", "_allowed_verbs",
        "_allowed_commits", "_root_identity", "_boundary_identity",
        "_git_metadata_identity", "_submodules_absent",
        "_expected_commit_sha", "_expected_tree_sha",
        "_submodules_tree_sha", "_submodules_index_sha256",
        "_provisional", "_native_role_proof", "_normal_commits",
        "_producer_history", "_allowed_read_pairs", "_allowed_ancestry_pairs",
        "_commit_tree_rows", "_history_read_rows", "_history_ancestry_rows",
        "_history_commit_tree_rows", "_verified_blob_facts", "_token",
    )

    def __init__(
        self, *, owner_identity: _NativeGitOperationIdentity,
        repository_id: str, role: _LocalRepositoryRole,
        root: pathlib.Path, boundary_root: pathlib.Path,
        allowed_paths: frozenset[str], allowed_verbs: frozenset[str],
        allowed_commits: frozenset[str], submodules_absent: bool,
        expected_commit_sha: str, expected_tree_sha: str | None,
        submodules_tree_sha: str | None,
        submodules_index_sha256: str | None,
        provisional: bool, native_role_proof: object | None,
        normal_commits: frozenset[str] | None = None,
        producer_history: object | None = None,
        allowed_read_pairs: tuple[tuple[str, str], ...] = (),
        allowed_ancestry_pairs: tuple[tuple[str, str], ...] = (),
        commit_tree_rows: tuple[Any, ...] = (),
        _token: object,
    ) -> None:
        if _token is not _REPOSITORY_TOKEN:
            raise TypeError("local repository capabilities are broker-issued only")
        root, boundary_root = root.resolve(strict=True), boundary_root.resolve(strict=True)
        root_identity = _directory_identity(root)
        boundary_identity = _directory_identity(boundary_root)
        git_metadata_identity = _git_metadata_directory_identity(
            root, deadline_monotonic=owner_identity._deadline_monotonic,
            clock=owner_identity._budget.clock,
        )
        if not _contained(root, boundary_root):
            raise NativeGitOwnerError("local repository escaped its registered boundary")
        if not repository_id or not _HEX32.fullmatch(repository_id):
            raise NativeGitOwnerError("local repository ID is malformed")
        if any(not isinstance(path, str) or not path for path in allowed_paths):
            raise NativeGitOwnerError("local repository path allowlist is malformed")
        if any(not _SHA1.fullmatch(commit) or commit == "0" * 40 for commit in allowed_commits):
            raise NativeGitOwnerError("local repository commit allowlist is malformed")
        if type(submodules_absent) is not bool:
            raise NativeGitOwnerError("local repository submodule proof is malformed")
        if (
            not isinstance(expected_commit_sha, str)
            or not _SHA1.fullmatch(expected_commit_sha)
            or expected_commit_sha not in allowed_commits
            or expected_tree_sha is not None
            and (not isinstance(expected_tree_sha, str) or not _SHA1.fullmatch(expected_tree_sha))
            or type(provisional) is not bool
            or provisional != (expected_tree_sha is None)
            or submodules_absent and (
                provisional or submodules_tree_sha != expected_tree_sha
                or not isinstance(submodules_index_sha256, str)
                or not _SHA256.fullmatch(submodules_index_sha256)
            )
            or not submodules_absent and (
                submodules_tree_sha is not None or submodules_index_sha256 is not None
            )
        ):
            raise NativeGitOwnerError("local repository proof fields are inconsistent")
        exact_normal_commits = (
            frozenset({expected_commit_sha})
            if normal_commits is None else frozenset(normal_commits)
        )
        if (
            not exact_normal_commits
            or any(not isinstance(value, str) or not _SHA1.fullmatch(value)
                   for value in exact_normal_commits)
            or expected_commit_sha not in exact_normal_commits
            or type(allowed_read_pairs) is not tuple
            or allowed_read_pairs != tuple(sorted(set(allowed_read_pairs)))
            or any(
                type(pair) is not tuple or len(pair) != 2
                or not isinstance(pair[0], str) or not _SHA1.fullmatch(pair[0])
                or not isinstance(pair[1], str) or _safe_path(pair[1]) != pair[1]
                for pair in allowed_read_pairs
            )
            or type(allowed_ancestry_pairs) is not tuple
            or allowed_ancestry_pairs != tuple(sorted(set(allowed_ancestry_pairs)))
            or any(
                type(pair) is not tuple or len(pair) != 2
                or any(not isinstance(value, str) or not _SHA1.fullmatch(value) for value in pair)
                for pair in allowed_ancestry_pairs
            )
            or type(commit_tree_rows) is not tuple
            or commit_tree_rows != tuple(sorted(commit_tree_rows, key=lambda row: row.commit_sha))
        ):
            raise NativeGitOwnerError("local repository history authority is malformed")
        if role is not _LocalRepositoryRole.PREPARED_RELEASE and (
            producer_history is not None or allowed_read_pairs
            or allowed_ancestry_pairs or commit_tree_rows
        ):
            raise NativeGitOwnerError("producer-history authority is limited to prepared release")
        if role is _LocalRepositoryRole.PREPARED_RELEASE:
            if producer_history is None:
                # The offline owner is intentionally a distinct disposable role.
                if allowed_read_pairs or allowed_ancestry_pairs or commit_tree_rows:
                    raise NativeGitOwnerError("prepared history rows lack their Native history issuer")
            else:
                native = _exact_adjacent_module(
                    _NATIVE_MODULE_NAME, "canonical_publication_native.py",
                )
                history_type = getattr(native, "_VerifiedInitialProducerHistory", None)
                if type(producer_history) is not history_type:
                    raise NativeGitOwnerError("prepared history is not the exact Native capability")
                read_rows = tuple(getattr(producer_history, "read_pairs", ()))
                ancestry_rows = tuple(getattr(producer_history, "ancestry_pairs", ()))
                expected_reads = tuple(sorted(
                    (row.commit_sha, row.path) for row in read_rows
                ))
                expected_ancestry = tuple(sorted(
                    (row.ancestor_sha, row.descendant_sha) for row in ancestry_rows
                ))
                expected_commit_trees = tuple(
                    getattr(producer_history, "commit_tree_rows", ()),
                )
                if (
                    allowed_read_pairs != expected_reads
                    or allowed_ancestry_pairs != expected_ancestry
                    or commit_tree_rows != expected_commit_trees
                ):
                    raise NativeGitOwnerError("prepared history authority differs from Native rows")
        self._owner_identity = owner_identity
        self._repository_id = repository_id
        self._role = role
        self._root = root
        self._boundary_root = boundary_root
        self._allowed_paths = allowed_paths
        self._allowed_verbs = allowed_verbs
        self._allowed_commits = allowed_commits
        self._root_identity = root_identity
        self._boundary_identity = boundary_identity
        self._git_metadata_identity = git_metadata_identity
        self._submodules_absent = submodules_absent
        self._expected_commit_sha = expected_commit_sha
        self._expected_tree_sha = expected_tree_sha
        self._submodules_tree_sha = submodules_tree_sha
        self._submodules_index_sha256 = submodules_index_sha256
        self._provisional = provisional
        self._native_role_proof = native_role_proof
        self._normal_commits = exact_normal_commits
        self._producer_history = producer_history
        self._allowed_read_pairs = frozenset(allowed_read_pairs)
        self._allowed_ancestry_pairs = frozenset(allowed_ancestry_pairs)
        self._commit_tree_rows = commit_tree_rows
        self._history_read_rows = {
            (row.commit_sha, row.path): row
            for row in tuple(getattr(producer_history, "read_pairs", ()))
        }
        self._history_ancestry_rows = {
            (row.ancestor_sha, row.descendant_sha): row
            for row in tuple(getattr(producer_history, "ancestry_pairs", ()))
        }
        self._history_commit_tree_rows = {
            row.commit_sha: row for row in commit_tree_rows
        }
        self._verified_blob_facts: dict[tuple[str, str], tuple[str, int]] = {}
        self._token = _token


class _NativeGitOperationOwner:
    """Serialized local Git facade bound to one Native execution identity."""

    __slots__ = (
        "_identity", "_state", "_borrower", "_native", "_lock",
        "_entries", "_children", "_finished", "_uncertain", "_token",
        "_producer_history_consumed", "_producer_history_results",
    )

    def __init__(
        self, *, identity: _NativeGitOperationIdentity,
        state: Any, native: Any, _token: object,
    ) -> None:
        if _token is not _OWNER_TOKEN:
            raise TypeError("Native local Git owners are binder-issued only")
        self._identity = identity
        self._state = state
        self._native = native
        self._lock = threading.RLock()
        self._entries: list[_GitExecutionLedgerEntry] = []
        self._children: dict[str, Any] = {}
        self._finished = False
        self._uncertain = False
        self._producer_history_consumed: dict[str, object] = {}
        self._producer_history_results: dict[str, Any] = {}
        self._token = _token
        self._borrower = state.GitObjectStore._borrow_local_executor(
            identity._git, owner_identity=identity,
            deadline_monotonic=identity._deadline_monotonic,
        )
        identity._owner = self

    def repository(self, role: _LocalRepositoryRole | str) -> _LocalRepositoryCapability:
        self._require_active()
        try:
            exact_role = role if type(role) is _LocalRepositoryRole else _LocalRepositoryRole(role)
        except (TypeError, ValueError) as exc:
            raise NativeGitOwnerError("local repository role is outside the closed allowlist") from exc
        cap = self._identity._repository_capabilities.get(exact_role.value)
        if cap is None:
            raise NativeGitOwnerError("this Native operation has no repository for the requested role")
        self._identity._git._validate_local_repository_capability(
            self._identity, cap,
        )
        return cap

    def _register_repository_role(self, proof: object) -> _LocalRepositoryCapability:
        """Install one exact Native-sealed role or its single monotone proof upgrade."""
        with self._lock:
            self._require_active()
            if self._identity._fixture_only is True:
                raise NativeGitOwnerError("offline fixture authority cannot register production roles")
            native = _exact_adjacent_module(
                _NATIVE_MODULE_NAME, "canonical_publication_native.py",
            )
            proof_type = getattr(native, "_VerifiedInitialGitRepositoryRole", None)
            validator = getattr(native, "require_verified_initial_git_repository_role", None)
            if (
                native is not self._native
                or not isinstance(proof_type, type)
                or type(proof) is not proof_type
                or not callable(validator)
                or proof.initial_context is not self._identity._context
                or proof.execution_control is not getattr(
                    self._identity._context, "_issued_generator_control", None,
                )
                or proof.execution_control._initial_context is not self._identity._context
                or proof.execution_control._release_control is not self._identity._control
                or proof.execution_control._repository_roles.get(proof.role) is not proof
                or proof.deadline_monotonic != self._identity._deadline_monotonic
            ):
                raise NativeGitOwnerError("repository role is not the exact current Native-issued proof")
            try:
                validator(
                    proof,
                    initial_context=self._identity._context,
                    release_closure_control=self._identity._control,
                    role=proof.role,
                    deadline_monotonic=self._identity._deadline_monotonic,
                )
            except Exception as exc:
                raise NativeGitOwnerError(
                    "Native repository role validator rejected or found stale proof",
                ) from exc
            try:
                role = _LocalRepositoryRole(proof.role)
            except (TypeError, ValueError) as exc:
                raise NativeGitOwnerError("Native repository role is outside the closed role set") from exc
            if role is _LocalRepositoryRole.DISPOSABLE_FIXTURE:
                raise NativeGitOwnerError("Native cannot register an offline fixture role")
            root = proof.root
            boundary = proof.boundary_root
            if not isinstance(root, pathlib.Path) or not isinstance(boundary, pathlib.Path):
                raise NativeGitOwnerError("Native repository proof paths are malformed")
            try:
                root_real = root.resolve(strict=True)
                boundary_real = boundary.resolve(strict=True)
                root_identity = _directory_identity(root_real)
                boundary_identity = _directory_identity(boundary_real)
                metadata_path = root_real / ".git"
                metadata_identity = _git_metadata_directory_identity(
                    root_real, deadline_monotonic=self._identity._deadline_monotonic,
                    clock=self._identity._budget.clock,
                )
            except (OSError, ValueError) as exc:
                raise NativeGitOwnerError("Native repository proof root is unavailable") from exc
            if (
                root_real != root or boundary_real != boundary
                or not _contained(root_real, boundary_real)
                or proof.root_identity != root_identity
                or proof.boundary_identity != boundary_identity
                or proof.git_metadata_path != metadata_path
                or proof.git_metadata_identity != metadata_identity
                or proof.expected_commit_sha not in proof.allowed_commits
            ):
                raise NativeGitOwnerError("Native repository proof identity changed before registration")
            allowed_commits = tuple(proof.allowed_commits)
            allowed_paths = tuple(proof.allowed_paths)
            allowed_verbs = tuple(proof.allowed_verbs)
            allowed_verb_set = frozenset(verb.value for verb in self._state._LocalGitVerb)
            if (
                not allowed_commits or allowed_commits != tuple(sorted(set(allowed_commits))
                ) or any(not isinstance(commit, str) or not _SHA1.fullmatch(commit) for commit in allowed_commits)
                or allowed_paths != tuple(sorted(set(allowed_paths)))
                or any(
                    not isinstance(path, str) or _safe_path(path) != path
                    for path in allowed_paths
                )
                or not allowed_verbs or allowed_verbs != tuple(sorted(set(allowed_verbs)))
                or not set(allowed_verbs).issubset(allowed_verb_set)
                or type(proof.submodules_absent) is not bool
                or proof.provisional != (proof.expected_tree_sha is None)
            ):
                raise NativeGitOwnerError("Native repository role allowlists or state are malformed")
            identity_verbs = frozenset({
                self._state._LocalGitVerb.INDEX_GITLINKS.value,
                self._state._LocalGitVerb.REV_PARSE_COMMIT_HEAD.value,
                self._state._LocalGitVerb.REV_PARSE_TREE.value,
                self._state._LocalGitVerb.TREE_GITLINKS.value,
            })
            full_verbs = frozenset({
                *identity_verbs,
                self._state._LocalGitVerb.IS_ANCESTOR.value,
                self._state._LocalGitVerb.SHOW_BLOB.value,
                self._state._LocalGitVerb.SHOW_BLOB_INFO.value,
                self._state._LocalGitVerb.SHOW_BLOB_PAYLOAD.value,
                self._state._LocalGitVerb.TRUSTED_ORIGIN_MAIN.value,
            })
            if role is _LocalRepositoryRole.VERIFIED_CLI:
                if allowed_paths:
                    raise NativeGitOwnerError("CLI repository role unexpectedly allows blob paths")
                expected_verbs = identity_verbs | (
                    {self._state._LocalGitVerb.STATUS_PORCELAIN_UNTRACKED.value}
                    if proof.submodules_absent else frozenset()
                )
                if proof.provisional and allowed_verbs != tuple(sorted(identity_verbs)):
                    raise NativeGitOwnerError("provisional CLI role exceeds fixed identity reads")
            else:
                expected_verbs = full_verbs | (
                    {self._state._LocalGitVerb.STATUS_PORCELAIN_UNTRACKED.value}
                    if proof.submodules_absent else frozenset()
                )
            if frozenset(allowed_verbs) != frozenset(expected_verbs):
                raise NativeGitOwnerError("Native repository role verbs exceed their fixed role policy")
            if proof.submodules_absent:
                if (
                    proof.provisional is True
                    or proof.expected_tree_sha is None
                    or proof.submodules_tree_sha != proof.expected_tree_sha
                    or not isinstance(proof.submodules_index_sha256, str)
                    or not _SHA256.fullmatch(proof.submodules_index_sha256)
                ):
                    raise NativeGitOwnerError("submodule-free role lacks exact tree/index digests")
            elif proof.submodules_tree_sha is not None or proof.submodules_index_sha256 is not None:
                raise NativeGitOwnerError("unverified role carries submodule-free digests")
            normal_commits = frozenset({proof.expected_commit_sha})
            producer_history = None
            allowed_read_pairs: tuple[tuple[str, str], ...] = ()
            allowed_ancestry_pairs: tuple[tuple[str, str], ...] = ()
            commit_tree_rows: tuple[Any, ...] = ()
            if role in {
                _LocalRepositoryRole.CONTROL_CHECKOUT,
                _LocalRepositoryRole.PREPARED_RELEASE,
            }:
                try:
                    parent_sha = _sha1(
                        proof.execution_control._parent_evidence.parent_reference.get(
                            "merged_sha",
                        ),
                        "Native archived parent commit",
                    )
                except Exception as exc:
                    raise NativeGitOwnerError(
                        "Native registry role lacks exact archived parent identity",
                    ) from exc
                normal_commits = frozenset({proof.expected_commit_sha, parent_sha})
            if role is _LocalRepositoryRole.PREPARED_RELEASE:
                producer_history = getattr(proof, "producer_history", None)
                history_validator = getattr(
                    native, "require_verified_initial_producer_history", None,
                )
                if producer_history is None or not callable(history_validator):
                    raise NativeGitOwnerError(
                        "prepared release role lacks its exact completed Native history",
                    )
                try:
                    history_validator(
                        producer_history,
                        initial_context=self._identity._context,
                        execution_control=proof.execution_control,
                        git_store=self._identity._git,
                        deadline_monotonic=self._identity._deadline_monotonic,
                    )
                except Exception as exc:
                    raise NativeGitOwnerError(
                        "Native prepared release history is stale or invalid",
                    ) from exc
                allowed_read_pairs = tuple(getattr(proof, "allowed_read_pairs", ()))
                allowed_ancestry_pairs = tuple(
                    getattr(proof, "allowed_ancestry_pairs", ()),
                )
                commit_tree_rows = tuple(getattr(proof, "commit_tree_rows", ()))
                expected_read_pairs = tuple(sorted(
                    (row.commit_sha, row.path)
                    for row in producer_history.read_pairs
                ))
                expected_ancestry_pairs = tuple(sorted(
                    (row.ancestor_sha, row.descendant_sha)
                    for row in producer_history.ancestry_pairs
                ))
                if (
                    allowed_read_pairs != expected_read_pairs
                    or allowed_ancestry_pairs != expected_ancestry_pairs
                    or commit_tree_rows != producer_history.commit_tree_rows
                    or any(
                        commit not in proof.allowed_commits
                        for commit in (
                            *(pair[0] for pair in allowed_read_pairs),
                            *(value for pair in allowed_ancestry_pairs for value in pair),
                            *(row.commit_sha for row in commit_tree_rows),
                        )
                    )
                ):
                    raise NativeGitOwnerError(
                        "prepared role history pair authority differs from Native evidence",
                    )
            elif any((
                getattr(proof, "producer_history", None) is not None,
                getattr(proof, "allowed_read_pairs", ()),
                getattr(proof, "allowed_ancestry_pairs", ()),
                getattr(proof, "commit_tree_rows", ()),
            )):
                raise NativeGitOwnerError(
                    "non-prepared repository role unexpectedly carries producer history",
                )
            existing = self._identity._repository_capabilities.get(role.value)
            if existing is not None:
                same_binding = (
                    existing._root == root_real
                    and existing._boundary_root == boundary_real
                    and existing._root_identity == root_identity
                    and existing._boundary_identity == boundary_identity
                    and existing._git_metadata_identity == metadata_identity
                    and existing._expected_commit_sha == proof.expected_commit_sha
                    and existing._allowed_commits == frozenset(allowed_commits)
                    and existing._allowed_paths == frozenset(allowed_paths)
                    and existing._role is role
                    and existing._producer_history is producer_history
                    and existing._allowed_read_pairs == frozenset(allowed_read_pairs)
                    and existing._allowed_ancestry_pairs == frozenset(allowed_ancestry_pairs)
                    and existing._commit_tree_rows == commit_tree_rows
                )
                same_proof = existing._native_role_proof is proof
                monotone_submodule_upgrade = (
                    existing._submodules_absent is False
                    and proof.submodules_absent is True
                    and (existing._expected_tree_sha == proof.expected_tree_sha
                         or existing._provisional is True
                         and existing._expected_tree_sha is None
                         and proof.expected_tree_sha is not None)
                    and proof.provisional is False
                    and frozenset(allowed_verbs)
                    == existing._allowed_verbs | {
                        self._state._LocalGitVerb.STATUS_PORCELAIN_UNTRACKED.value,
                    }
                )
                if not same_binding or not (same_proof or monotone_submodule_upgrade):
                    raise NativeGitOwnerError("repository role replacement is not the exact monotone proof upgrade")
            repository_id = _repository_id(
                self._identity._operation_id, role, root_real,
            )
            cap = _LocalRepositoryCapability(
                owner_identity=self._identity, repository_id=repository_id,
                role=role, root=root_real, boundary_root=boundary_real,
                allowed_paths=frozenset(allowed_paths),
                allowed_verbs=frozenset(allowed_verbs),
                allowed_commits=frozenset(allowed_commits),
                submodules_absent=proof.submodules_absent,
                expected_commit_sha=proof.expected_commit_sha,
                expected_tree_sha=proof.expected_tree_sha,
                submodules_tree_sha=proof.submodules_tree_sha,
                submodules_index_sha256=proof.submodules_index_sha256,
                provisional=proof.provisional,
                native_role_proof=proof,
                normal_commits=normal_commits,
                producer_history=producer_history,
                allowed_read_pairs=allowed_read_pairs,
                allowed_ancestry_pairs=allowed_ancestry_pairs,
                commit_tree_rows=commit_tree_rows,
                _token=_REPOSITORY_TOKEN,
            )
            if existing is not None:
                existing_id = existing._repository_id
                self._identity._repository_capabilities.pop(existing_id, None)
            self._identity._repository_capabilities[role.value] = cap
            self._identity._repository_capabilities[repository_id] = cap
            self._identity._git._validate_local_repository_capability(
                self._identity, cap,
            )
            return cap

    def run_git(
        self, repository: _LocalRepositoryCapability,
        request: _ClosedLocalGitRequest,
    ) -> Any:
        return self._run_git(repository, request, operation_kind="native")

    def run_producer_history_request(self, request: object) -> Any:
        """Run one exact Native selector against the existing bare object store."""
        with self._lock:
            self._require_active()
            native = _exact_adjacent_module(
                _NATIVE_MODULE_NAME, "canonical_publication_native.py",
            )
            request_type = getattr(
                native, "_VerifiedInitialProducerHistoryReadRequest", None,
            )
            validator = getattr(
                native, "require_verified_initial_producer_history_read_request", None,
            )
            context = getattr(request, "initial_context", None)
            execution = getattr(request, "execution_control", None)
            request_id = getattr(request, "request_id", None)
            request_bytes = getattr(request, "request_bytes", None)
            if (
                not isinstance(request_type, type)
                or type(request) is not request_type
                or not callable(validator)
                or native is not self._native
                or context is not self._identity._context
                or execution is None
                or getattr(execution, "_initial_context", None) is not context
                or getattr(execution, "_git_owner", None) is not self
                or getattr(request, "git_store", None) is not self._identity._git
                or getattr(request, "deadline_monotonic", None)
                != self._identity._deadline_monotonic
                or type(request_id) is not str or not _SHA256.fullmatch(request_id)
                or type(request_bytes) is not bytes
                or hashlib.sha256(request_bytes).hexdigest()
                != getattr(request, "request_sha256", None)
                or getattr(context, "_producer_history_read_requests", {}).get(request_id)
                is not request
                or request_id not in getattr(context, "_producer_history_read_inflight", set())
            ):
                raise NativeGitOwnerError(
                    "producer-history query lacks its exact active Native request",
                )
            try:
                exact = validator(
                    request, initial_context=context, execution_control=execution,
                    git_store=self._identity._git,
                    deadline_monotonic=self._identity._deadline_monotonic,
                )
            except Exception as exc:
                raise NativeGitOwnerError(
                    "Native rejected the producer-history query request",
                ) from exc
            if exact is not request:
                raise NativeGitOwnerError("Native query validator changed request identity")
            cached = self._producer_history_results.get(request_id)
            if cached is not None:
                if self._producer_history_consumed.get(request_id) is not request:
                    raise NativeGitOwnerError("producer-history result cache lost request identity")
                return cached
            if request_id in self._producer_history_consumed:
                raise NativeGitOwnerError("failed producer-history query cannot be replayed")
            self._producer_history_consumed[request_id] = request
            commands_before = self._identity._budget.commands
            output_before = self._identity._budget.output_bytes
            try:
                result = self._identity._git._read_initial_producer_history_request(
                    self._borrower, request,
                )
            except BaseException as exc:
                observed = tuple(getattr(exc, "git_results", ()))
                self._record_producer_history_results(request, observed)
                observed_output = sum(
                    len(row.stdout) + len(row.stderr)
                    for row in observed if type(row) is self._state._LocalGitResult
                )
                if (
                    self._identity._budget.commands - commands_before != len(observed)
                    or self._identity._budget.output_bytes - output_before != observed_output
                ):
                    # Any launched command or output without a complete cleanup
                    # receipt makes this owner outcome unsealable, even if earlier
                    # commands in the same request completed normally.
                    self._uncertain = True
                raise
            result_require = getattr(
                self._state, "require_owned_producer_history_read_result", None,
            )
            if not callable(result_require):
                self._uncertain = True
                raise NativeGitOwnerError("State producer-history result validator is unavailable")
            try:
                result = result_require(
                    result, request=request, git=self._identity._git,
                    deadline_monotonic=self._identity._deadline_monotonic,
                )
            except Exception as exc:
                self._uncertain = True
                self._record_producer_history_results(
                    request, getattr(result, "git_results", ()),
                )
                raise NativeGitOwnerError(
                    "State producer-history result failed exact readback",
                ) from exc
            self._record_producer_history_results(request, result.git_results)
            self._producer_history_results[request_id] = result
            return result

    def _record_producer_history_results(
        self, request: object, results: Any,
    ) -> None:
        values = tuple(results) if isinstance(results, (tuple, list)) else ()
        repository_id = hashlib.sha256(
            (
                self._identity._operation_id + ":producer-history:"
                + str(self._identity._git.git_dir)
            ).encode("utf-8", "strict"),
        ).hexdigest()[:32]
        for result in values:
            if type(result) is not self._state._LocalGitResult:
                self._uncertain = True
                continue
            self._entries.append(_GitExecutionLedgerEntry(
                operation_kind="native",
                repository_id=repository_id,
                verb="producer_history_" + str(getattr(request, "kind", "unknown")),
                request_sha256=str(getattr(request, "request_sha256", "")),
                returncode=result.returncode,
                stdout_bytes=len(result.stdout),
                stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
                stderr_bytes=len(result.stderr),
                stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
                execution_ordinal=result.execution_ordinal,
                commands_before=result.commands_before,
                commands_after=result.commands_after,
                output_bytes_before=result.output_bytes_before,
                output_bytes_after=result.output_bytes_after,
                started_monotonic_ns=int(result.started_monotonic * 1_000_000_000),
                finished_monotonic_ns=int(result.finished_monotonic * 1_000_000_000),
                process_id=result.process_id,
                process_group_id=result.process_group_id,
                cleanup_confirmed=result.cleanup_confirmed,
            ))

    def _new_fixture_repository(
        self, registration: _RegisteredGitChild, kind: FixtureRepositoryKind,
    ) -> _FixtureRepositoryCapability:
        with self._lock:
            self._require_active()
            if (
                type(registration) is not _RegisteredGitChild
                or registration.closed is not False
                or self._children.get(registration.child_id) is not registration
                or not _is_exact_fixture_test_command(registration.command)
                or type(kind) is not FixtureRepositoryKind
            ):
                raise NativeGitOwnerError("fixture allocation is outside the exact registered CHECK14 child")
            if registration.fixture_workspace is None:
                workspace = self._identity._git.temp_root / f"native-fixture-{registration.child_id}"
                try:
                    workspace.mkdir(mode=0o700)
                    info = workspace.lstat()
                except FileExistsError as exc:
                    raise NativeGitOwnerError("fixture workspace already exists for this child") from exc
                if (
                    not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700 or info.st_nlink < 2
                ):
                    raise NativeGitOwnerError("fixture workspace did not seal as an owned private directory")
                registration.fixture_workspace = workspace
                registration.fixture_workspace_identity = _directory_identity(workspace)
            workspace = registration.fixture_workspace
            workspace_identity = registration.fixture_workspace_identity
            if workspace_identity is None or _directory_identity(workspace) != workspace_identity:
                raise NativeGitOwnerError("fixture workspace identity changed")
            fixture_id = secrets.token_hex(16)
            root = (
                _diagnostic_layout_path(workspace)
                if kind is FixtureRepositoryKind.DIAGNOSTIC_LAYOUT
                else workspace / f"repo-{fixture_id}"
            )
            root_identity: tuple[int, int, int, int] | None
            if kind is FixtureRepositoryKind.PATH:
                if root.exists() or root.is_symlink():
                    raise NativeGitOwnerError("new fixture path unexpectedly exists")
                root_identity = None
            elif kind is FixtureRepositoryKind.DIAGNOSTIC_LAYOUT:
                try:
                    _create_diagnostic_layout(workspace, root)
                    root_identity = _directory_identity(root)
                except OSError as exc:
                    raise NativeGitOwnerError("diagnostic layout could not be created safely") from exc
            else:
                root.mkdir(mode=0o700)
                info = root.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700 or info.st_nlink < 2
                ):
                    raise NativeGitOwnerError("fixture repository root is not an owned private directory")
                root_identity = _directory_identity(root)
            cap = _FixtureRepositoryCapability(
                owner_identity=self._identity, child_id=registration.child_id,
                repository_id=fixture_id, root=root, workspace=workspace,
                workspace_identity=workspace_identity, root_identity=root_identity,
                kind=kind, _token=_FIXTURE_REPOSITORY_TOKEN,
            )
            registration.fixture_capabilities[fixture_id] = cap
            registration.fixture_roots.append((root, root_identity))
            return cap

    def _new_diagnostic_repository(
        self, registration: _RegisteredGitChild,
        layout: _FixtureRepositoryCapability, role: str,
    ) -> _FixtureRepositoryCapability:
        with self._lock:
            self._require_active()
            if (
                type(registration) is not _RegisteredGitChild
                or registration.closed is not False
                or self._children.get(registration.child_id) is not registration
                or not _is_exact_fixture_test_command(registration.command)
                or type(layout) is not _FixtureRepositoryCapability
                or layout._kind is not FixtureRepositoryKind.DIAGNOSTIC_LAYOUT
                or registration.fixture_capabilities.get(layout._repository_id) is not layout
                or role not in {"source", "disposable"}
            ):
                raise NativeGitOwnerError("diagnostic fixture role lacks its exact layout capability")
            self._validate_fixture_capability(registration, layout)
            fixture_id = secrets.token_hex(16)
            root = layout._root / role
            root_identity: tuple[int, int, int, int] | None
            if role == "source":
                try:
                    root.mkdir(mode=0o700)
                    root_identity = _directory_identity(root)
                except FileExistsError as exc:
                    raise NativeGitOwnerError("diagnostic source path was already allocated") from exc
            else:
                if root.exists() or root.is_symlink():
                    raise NativeGitOwnerError("diagnostic disposable path unexpectedly exists")
                root_identity = None
            capability = _FixtureRepositoryCapability(
                owner_identity=self._identity, child_id=registration.child_id,
                repository_id=fixture_id, root=root, workspace=registration.fixture_workspace,
                workspace_identity=registration.fixture_workspace_identity,
                root_identity=root_identity, kind=FixtureRepositoryKind.PATH,
                diagnostic_layout=layout, diagnostic_role=role,
                _token=_FIXTURE_REPOSITORY_TOKEN,
            )
            registration.fixture_capabilities[fixture_id] = capability
            registration.fixture_roots.append((root, root_identity))
            return capability

    def _remove_diagnostic_cloned_commit(
        self, registration: _RegisteredGitChild,
        layout: _FixtureRepositoryCapability, receipt_id: str,
    ) -> tuple[int, str, str]:
        """Remove one verified loose commit exposed by a successful shared clone."""
        with self._lock:
            self._require_active()
            if (
                type(registration) is not _RegisteredGitChild
                or registration.closed is not False
                or self._children.get(registration.child_id) is not registration
                or type(layout) is not _FixtureRepositoryCapability
                or layout._kind is not FixtureRepositoryKind.DIAGNOSTIC_LAYOUT
                or registration.fixture_capabilities.get(layout._repository_id) is not layout
                or not isinstance(receipt_id, str) or not _HEX32.fullmatch(receipt_id)
            ):
                raise NativeGitOwnerError("diagnostic removal lacks its registered layout and receipt")
            receipt = registration.diagnostic_clone_receipts.get(receipt_id)
            if (
                type(receipt) is not _OwnedDiagnosticCloneReceipt
                or receipt.token is not _DIAGNOSTIC_CLONE_TOKEN
                or receipt.used is not False
                or receipt.layout is not layout
                or receipt.source._diagnostic_layout is not layout
                or receipt.source._diagnostic_role != "source"
                or receipt.destination._diagnostic_layout is not layout
                or receipt.destination._diagnostic_role != "disposable"
                or receipt.destination._owner_identity is not self._identity
                or registration.fixture_capabilities.get(
                    receipt.destination._repository_id,
                ) is not receipt.destination
                or receipt.destination._relations.get("alternates") is not receipt.source
                or receipt.destination._relations.get("origin") is not receipt.source
            ):
                raise NativeGitOwnerError("diagnostic receipt is used, foreign, or outside its exact clone pair")
            self._validate_fixture_capability(registration, layout)
            self._validate_fixture_capability(registration, receipt.destination)

            source_head_result = self._run_fixture(
                registration, receipt.source,
                ClosedFixtureGitRequest(FixtureGitVerb.REV_PARSE_HEAD),
            )
            destination_head_result = self._run_fixture(
                registration, receipt.destination,
                ClosedFixtureGitRequest(FixtureGitVerb.REV_PARSE_HEAD),
            )
            destination_tree_result = self._run_fixture(
                registration, receipt.destination,
                ClosedFixtureGitRequest(
                    FixtureGitVerb.REV_PARSE_TREE, commit_sha=receipt.commit_sha,
                ),
            )
            if any(
                result.returncode != 0
                for result in (source_head_result, destination_head_result, destination_tree_result)
            ):
                raise NativeGitOwnerError("diagnostic clone identity readback did not complete")
            try:
                source_head = source_head_result.stdout.decode("ascii", "strict").strip()
                destination_head = destination_head_result.stdout.decode("ascii", "strict").strip()
                destination_tree = destination_tree_result.stdout.decode("ascii", "strict").strip()
            except UnicodeDecodeError as exc:
                raise NativeGitOwnerError("diagnostic clone identity readback is malformed") from exc
            if (
                source_head != receipt.commit_sha
                or destination_head != receipt.commit_sha
                or destination_tree != receipt.tree_sha
            ):
                raise NativeGitOwnerError("diagnostic source or clone changed after receipt issuance")

            # Consume before filesystem mutation. An uncertain delete must not
            # allow this receipt to authorize a second unlink attempt.
            receipt.used = True
            state = self._state
            metadata_root = state._fixture_metadata_directory(receipt.source)
            removed_bytes = _remove_verified_loose_commit(
                state, metadata_root, receipt.commit_sha,
            )
            return (
                removed_bytes, receipt.source._repository_id,
                receipt.destination._repository_id,
            )

    def _validate_fixture_capability(
        self, registration: _RegisteredGitChild,
        capability: _FixtureRepositoryCapability,
    ) -> pathlib.Path:
        if (
            type(capability) is not _FixtureRepositoryCapability
            or capability._token is not _FIXTURE_REPOSITORY_TOKEN
            or capability._owner_identity is not self._identity
            or capability._child_id != registration.child_id
            or registration.fixture_capabilities.get(capability._repository_id) is not capability
            or registration.fixture_workspace != capability._workspace
            or registration.fixture_workspace_identity != capability._workspace_identity
            or not _is_exact_fixture_test_command(registration.command)
        ):
            raise NativeGitOwnerError("fixture repository capability is foreign, revoked, or unregistered")
        try:
            workspace_info = capability._workspace.lstat()
            workspace_identity = _directory_identity(capability._workspace)
            if (
                not stat.S_ISDIR(workspace_info.st_mode)
                or stat.S_IMODE(workspace_info.st_mode) != 0o700
                or workspace_identity != capability._workspace_identity
                or capability._workspace.resolve(strict=True) != capability._workspace
                or not _contained(capability._root, capability._workspace)
                or (
                    capability._kind is FixtureRepositoryKind.DIAGNOSTIC_LAYOUT
                    and capability._root != _diagnostic_layout_path(capability._workspace)
                )
                or (
                    capability._kind is not FixtureRepositoryKind.DIAGNOSTIC_LAYOUT
                    and capability._diagnostic_layout is None
                    and capability._root.parent != capability._workspace
                )
                or capability._diagnostic_layout is not None
                and (
                    registration.fixture_capabilities.get(capability._diagnostic_layout._repository_id)
                    is not capability._diagnostic_layout
                    or capability._diagnostic_layout._kind is not FixtureRepositoryKind.DIAGNOSTIC_LAYOUT
                    or capability._root.parent != capability._diagnostic_layout._root
                    or capability._diagnostic_role not in {"source", "disposable"}
                )
            ):
                raise ValueError("fixture workspace or containment changed")
            if capability._root_identity is None:
                if capability._kind is not FixtureRepositoryKind.PATH:
                    raise ValueError("only a path capability can be initially absent")
                if capability._root.exists() or capability._root.is_symlink():
                    raise ValueError("absent fixture path appeared outside an owner Git operation")
            else:
                root_info = capability._root.lstat()
                if (
                    not stat.S_ISDIR(root_info.st_mode)
                    or stat.S_IMODE(root_info.st_mode) != 0o700
                    or _directory_identity(capability._root) != capability._root_identity
                ):
                    raise ValueError("fixture root identity changed")
                if capability._kind is FixtureRepositoryKind.DIAGNOSTIC_LAYOUT:
                    _validate_diagnostic_layout(
                        capability._workspace, capability._root,
                        capability._root_identity,
                    )
        except (OSError, ValueError) as exc:
            raise NativeGitOwnerError("fixture repository path escaped its registered workspace") from exc
        return capability._root

    def _run_fixture(
        self, registration: _RegisteredGitChild,
        capability: _FixtureRepositoryCapability,
        request: ClosedFixtureGitRequest,
    ) -> Any:
        with self._lock:
            self._require_active()
            self._validate_fixture_capability(registration, capability)
            _validate_fixture_request(self, registration, capability, request)
            commands_before = self._identity._budget.commands
            launch = self._state.GitObjectStore._seal_fixture_git_launch(
                self._identity._git, borrower=self._borrower,
                repository_capability=capability, request=request,
                source=request.source,
                destination=request.destination,
            )
            try:
                result = self._borrower.execute(launch)
            except BaseException:
                if self._identity._budget.commands > commands_before:
                    self._revoke_after_uncertain_execution()
                raise
            if type(result) is not self._state._LocalGitResult or result.cleanup_confirmed is not True:
                self._revoke_after_uncertain_execution()
                raise NativeGitOwnerError("fixture Git result lacks exact cleanup and State identity")
            if request.verb in {
                FixtureGitVerb.CLONE_SHARED, FixtureGitVerb.CLONE_SHARED_CHECKOUT,
                FixtureGitVerb.CLONE_FILE_DEPTH1,
                FixtureGitVerb.WORKTREE_ADD,
            }:
                assert request.destination is not None
                _refresh_fixture_path_identity(request.destination, should_exist=True)
            elif request.verb is FixtureGitVerb.WORKTREE_REMOVE:
                assert request.destination is not None
                _refresh_fixture_path_identity(request.destination, should_exist=False)
            if result.returncode == 0:
                _remember_fixture_objects(capability, request, result.stdout)
                if request.verb in {
                    FixtureGitVerb.CLONE_SHARED, FixtureGitVerb.CLONE_SHARED_CHECKOUT,
                    FixtureGitVerb.CLONE_FILE_DEPTH1,
                } and request.source is not None and request.destination is not None:
                    request.destination._relations["alternates"] = request.source
                    request.destination._relations["origin"] = request.source
                elif request.verb in {FixtureGitVerb.CLONE_FILE_DEPTH1, FixtureGitVerb.REMOTE_ADD} and request.source is not None:
                    target = request.destination or capability
                    target._relations["origin"] = request.source
                elif request.verb is FixtureGitVerb.WORKTREE_ADD and request.source is not None and request.destination is not None:
                    request.destination._relations["common_dir"] = request.source
            receipt_issued = False
            if (
                result.returncode == 0
                and request.verb in {
                    FixtureGitVerb.CLONE_SHARED, FixtureGitVerb.CLONE_SHARED_CHECKOUT,
                }
                and type(request.source) is _FixtureRepositoryCapability
                and request.source._diagnostic_role == "source"
                and request.source._diagnostic_layout is not None
                and request.destination is not None
            ):
                if registration.pending_diagnostic_clone_receipt_id is not None:
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError("a diagnostic clone receipt is still awaiting acknowledgment")
                # Record the clone before its bounded readbacks so ledger order
                # agrees with State command ordinals.
                self._record_fixture_result(registration, capability, request, result)
                receipt_issued = True
                source_head_result = self._run_fixture(
                    registration, request.source,
                    ClosedFixtureGitRequest(FixtureGitVerb.REV_PARSE_HEAD),
                )
                destination_head_result = self._run_fixture(
                    registration, request.destination,
                    ClosedFixtureGitRequest(FixtureGitVerb.REV_PARSE_HEAD),
                )
                if source_head_result.returncode != 0 or destination_head_result.returncode != 0:
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError("diagnostic clone HEAD readback did not complete")
                try:
                    source_head = source_head_result.stdout.decode("ascii", "strict").strip()
                    destination_head = destination_head_result.stdout.decode("ascii", "strict").strip()
                except UnicodeDecodeError as exc:
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError("diagnostic clone HEAD readback is malformed") from exc
                if (
                    not _SHA1.fullmatch(source_head)
                    or source_head != destination_head
                    or source_head not in request.source._known_objects
                ):
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError("diagnostic clone did not retain the exact observed source HEAD")
                destination_tree_result = self._run_fixture(
                    registration, request.destination,
                    ClosedFixtureGitRequest(
                        FixtureGitVerb.REV_PARSE_TREE, commit_sha=destination_head,
                    ),
                )
                if destination_tree_result.returncode != 0:
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError("diagnostic clone tree readback did not complete")
                try:
                    tree_sha = destination_tree_result.stdout.decode("ascii", "strict").strip()
                except UnicodeDecodeError as exc:
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError("diagnostic clone tree readback is malformed") from exc
                if not _SHA1.fullmatch(tree_sha):
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError("diagnostic clone tree identity is malformed")
                receipt_id = secrets.token_hex(16)
                receipt = _OwnedDiagnosticCloneReceipt(
                    receipt_id=receipt_id, layout=request.source._diagnostic_layout,
                    source=request.source, destination=request.destination,
                    commit_sha=destination_head, tree_sha=tree_sha,
                )
                registration.diagnostic_clone_receipts[receipt_id] = receipt
                registration.pending_diagnostic_clone_receipt_id = receipt_id
            if not receipt_issued:
                self._record_fixture_result(registration, capability, request, result)
            return result

    def _record_fixture_result(
        self, registration: _RegisteredGitChild,
        capability: _FixtureRepositoryCapability,
        request: ClosedFixtureGitRequest, result: Any,
    ) -> _GitExecutionLedgerEntry:
        request_bytes = _canonical_json_bytes(_fixture_request_dict(request))
        entry = _GitExecutionLedgerEntry(
            operation_kind="child_fixture",
            repository_id=capability._repository_id,
            verb=request.verb.value,
            request_sha256=hashlib.sha256(request_bytes).hexdigest(),
            returncode=result.returncode,
            stdout_bytes=len(result.stdout),
            stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
            stderr_bytes=len(result.stderr),
            stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
            execution_ordinal=result.execution_ordinal,
            commands_before=result.commands_before,
            commands_after=result.commands_after,
            output_bytes_before=result.output_bytes_before,
            output_bytes_after=result.output_bytes_after,
            started_monotonic_ns=int(result.started_monotonic * 1_000_000_000),
            finished_monotonic_ns=int(result.finished_monotonic * 1_000_000_000),
            process_id=result.process_id,
            process_group_id=result.process_group_id,
            cleanup_confirmed=result.cleanup_confirmed,
        )
        self._entries.append(entry)
        return entry

    def _run_git(
        self, repository: _LocalRepositoryCapability,
        request: _ClosedLocalGitRequest, *, operation_kind: str,
        source_guard: bool = True,
    ) -> Any:
        with self._lock:
            self._require_active()
            self._validate_request(repository, request)
            if operation_kind not in {"native", "child"}:
                raise NativeGitOwnerError("local Git operation kind is outside its closed set")
            source_metadata_verbs = {
                self._state._LocalGitVerb.SOURCE_IS_SHALLOW.value,
                self._state._LocalGitVerb.SOURCE_GIT_DIR.value,
                self._state._LocalGitVerb.SOURCE_COMMON_DIR.value,
                self._state._LocalGitVerb.SOURCE_SHALLOW_PATH.value,
                self._state._LocalGitVerb.SOURCE_SHALLOW_BYTES.value,
                self._state._LocalGitVerb.SOURCE_VALIDATE_REF_GRAPH.value,
                self._state._LocalGitVerb.SOURCE_REF_INVENTORY.value,
            }
            source_registration = None
            source_before: tuple[bytes, bytes] | None = None
            if source_guard and request.verb in source_metadata_verbs:
                source_registration = request._child_registration
                source_before = self._capture_source_inventory(
                    repository, source_registration,
                    operation_kind=operation_kind,
                )
                binding = self._source_snapshot_binding(repository, source_registration)
                if source_registration.source_snapshot_binding is None:
                    source_registration.source_snapshot_binding = binding
                    source_registration.source_snapshot_refs = source_before[0]
                    source_registration.source_snapshot_head = source_before[1]
                elif (
                    source_registration.source_snapshot_binding != binding
                    or source_registration.source_snapshot_refs != source_before[0]
                    or source_registration.source_snapshot_head != source_before[1]
                ):
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError(
                        "prepared Source refs or HEAD changed after the registered child snapshot",
                    )
            commands_before = self._identity._budget.commands
            launch = self._state.GitObjectStore._seal_local_git_launch(
                self._identity._git, borrower=self._borrower,
                repository_capability=repository, request=request,
            )
            try:
                result = self._borrower.execute(launch)
            except BaseException:
                if self._identity._budget.commands > commands_before:
                    self._revoke_after_uncertain_execution()
                raise
            if type(result) is not self._state._LocalGitResult or not result.cleanup_confirmed:
                self._revoke_after_uncertain_execution()
                raise NativeGitOwnerError("local Git process cleanup or result identity is uncertain")
            history_read_row = (
                repository._history_read_rows.get((request.commit_sha, request.path))
                if isinstance(request.commit_sha, str) and isinstance(request.path, str)
                else None
            )
            if request.verb == self._state._LocalGitVerb.SHOW_BLOB.value and history_read_row is not None:
                if (
                    history_read_row.outcome != "present"
                    or result.returncode != 0
                    or len(result.stdout) != history_read_row.byte_length
                    or hashlib.sha1(
                        b"blob " + str(len(result.stdout)).encode("ascii") + b"\0"
                        + result.stdout,
                    ).hexdigest() != history_read_row.blob_oid
                    or hashlib.sha256(result.stdout).hexdigest() != history_read_row.sha256
                ):
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError(
                        "historical blob bytes differ from the exact Native read pair",
                    )
            if request.verb == self._state._LocalGitVerb.SHOW_BLOB_PAYLOAD.value and history_read_row is not None:
                if (
                    history_read_row.outcome != "present"
                    or result.returncode != 0
                    or len(result.stdout) != history_read_row.byte_length
                    or hashlib.sha1(
                        b"blob " + str(len(result.stdout)).encode("ascii") + b"\0"
                        + result.stdout,
                    ).hexdigest() != history_read_row.blob_oid
                    or hashlib.sha256(result.stdout).hexdigest() != history_read_row.sha256
                ):
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError(
                        "historical payload differs from the exact Native read pair",
                    )
            if request.verb == self._state._LocalGitVerb.SHOW_BLOB_INFO.value and history_read_row is not None:
                if history_read_row.outcome == "absent":
                    expected_missing = (
                        f"{request.commit_sha}:{request.path} missing\n".encode("ascii")
                    )
                    if result.returncode != 0 or result.stdout != expected_missing:
                        self._revoke_after_uncertain_execution()
                        raise NativeGitOwnerError(
                            "historical absent path differs from its exact Native tree chain",
                        )
                    self._record_result(operation_kind, repository, request, result)
                    return result
                expected_info = (
                    f"{history_read_row.blob_oid} blob {history_read_row.byte_length}\n"
                    .encode("ascii")
                )
                if result.returncode != 0 or result.stdout != expected_info:
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError(
                        "historical blob metadata differs from the exact Native read pair",
                    )
                repository._verified_blob_facts[(request.commit_sha, request.path)] = (
                    history_read_row.blob_oid, history_read_row.byte_length,
                )
                self._record_result(operation_kind, repository, request, result)
                return result
            if request.verb == self._state._LocalGitVerb.IS_ANCESTOR.value:
                history_ancestry = repository._history_ancestry_rows.get(
                    (request.ancestor_sha, request.descendant_sha),
                )
                if history_ancestry is not None and (
                    result.returncode != (0 if history_ancestry.is_ancestor else 1)
                    or result.stdout
                ):
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError(
                        "historical ancestry result differs from its exact Native proof row",
                    )
            if request.verb == self._state._LocalGitVerb.REV_PARSE_TREE.value:
                history_tree = repository._history_commit_tree_rows.get(request.commit_sha)
                expected_tree = (
                    history_tree.tree_oid if history_tree is not None
                    else repository._expected_tree_sha
                    if request.commit_sha == repository._expected_commit_sha
                    else None
                )
                if expected_tree is not None and (
                    result.returncode != 0
                    or result.stdout != (expected_tree + "\n").encode("ascii")
                ):
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError(
                        "Git tree result differs from the exact Native commit-tree identity",
                    )
            if (
                request.verb == self._state._LocalGitVerb.TRUSTED_ORIGIN_MAIN.value
                and result.returncode == 0
            ):
                expected_ref = (repository._expected_commit_sha + "\n").encode("ascii")
                if result.stdout != expected_ref:
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError(
                        "trusted origin/main differs from the exact Native current-M identity",
                    )
            if request.verb == self._state._LocalGitVerb.SHOW_BLOB_INFO.value:
                missing_record = (
                    f"{request.commit_sha}:{request.path} missing\n".encode("utf-8", "strict")
                )
                if result.returncode != 0:
                    self._record_result(operation_kind, repository, request, result)
                    return result
                if result.stdout == missing_record:
                    self._record_result(operation_kind, repository, request, result)
                    return result
                try:
                    info_text = result.stdout.decode("ascii", "strict")
                    fields = info_text[:-1].split(" ") if info_text.endswith("\n") else ()
                except UnicodeDecodeError as exc:
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError("blob info returned malformed output") from exc
                if (
                    len(fields) != 3
                    or not _SHA1.fullmatch(fields[0])
                    or fields[1] != "blob"
                    or not fields[2].isascii()
                    or not fields[2].isdecimal()
                    or len(fields[2]) > 10
                    or len(fields[2]) > 1 and fields[2].startswith("0")
                ):
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError("blob info returned a noncanonical OID/type/size")
                byte_length = int(fields[2])
                if (
                    byte_length > self._state.MAX_LOCAL_GIT_PAYLOAD_OUTPUT_BYTES
                    or info_text != f"{fields[0]} blob {byte_length}\n"
                ):
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError("blob info is noncanonical or exceeds the existing per-file ceiling")
                repository._verified_blob_facts[(request.commit_sha, request.path)] = (
                    fields[0], byte_length,
                )
            if source_guard and request.verb in source_metadata_verbs:
                source_after = self._capture_source_inventory(
                    repository, source_registration,
                    operation_kind=operation_kind,
                )
                if source_after != source_before:
                    self._revoke_after_uncertain_execution()
                    raise NativeGitOwnerError(
                        "prepared Source refs or HEAD changed during its metadata read sequence",
                    )
            self._record_result(operation_kind, repository, request, result)
            return result

    def spawn(self, command: object) -> Any:
        """Launch only through the exact Native supervisor bridge.

        The bridge is supplied by the Native context after its full command
        graph is authenticated. This broker never creates a Popen itself.
        """
        with self._lock:
            self._require_active()
            method = getattr(
                self._identity._context,
                "_spawn_initial_owned_generator_child",
                None,
            )
            if not callable(method):
                raise NativeGitOwnerError("exact Native child supervisor bridge is unavailable")
            return _spawn_owned_child(self, command, method)

    def _serve_child_requests(self, handle: object, *, timeout: float = 0.0) -> bool:
        """Service at most one authenticated request from one registered child.

        Native's own wait/select loop calls this while it drains the child's
        bounded output and supervises its process group. A timeout only bounds
        this wait; it never changes the operation deadline or State budget.
        """
        with self._lock:
            self._require_active()
            registration = _registered_child_for_handle(self, handle)
            if (
                isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0 or timeout > 0.05
            ):
                raise NativeGitOwnerError("child Git service wait is outside its fixed bound")
            remaining = self._identity._deadline_monotonic - self._identity._budget.clock()
            if remaining <= 0:
                raise NativeGitOwnerError("Native child Git channel reached its original deadline")
            try:
                return _serve_one_child_message(
                    self, registration, timeout=min(float(timeout), remaining),
                )
            except BaseException:
                self._uncertain = True
                try:
                    registration.channel.close()
                except OSError:
                    pass
                if self._identity._active is True:
                    self._revoke_after_child_failure()
                raise

    def _complete_child(self, handle: object) -> None:
        """Close one channel after the exact Native supervisor proves reap."""
        with self._lock:
            if (
                self._finished is not False
                or self._token is not _OWNER_TOKEN
                or self._identity._owner is not self
                or self._identity._seal is None
            ):
                raise NativeGitOwnerError("Native child owner identity is revoked")
            if type(handle) is not getattr(self._native, "NativeOwnedGeneratorChildProcess", None):
                raise NativeGitOwnerError("Native supervisor supplied a foreign child handle")
            matches = [item for item in self._children.values() if item.process is handle]
            if len(matches) != 1 or matches[0].closed is not False:
                raise NativeGitOwnerError("Native child is not uniquely registered or already closed")
            registration = matches[0]
            if (
                registration.hello_complete is not True
                or registration.goodbye_complete is not True
                or registration.pending_sequence is not None
            ):
                raise NativeGitOwnerError("Native child completed without a complete HELLO/GOODBYE and result ACK")
            _require_native_child_cleanup(self, handle, registration)
            registration.closed = True
            registration.channel.close()
            del self._children[registration.child_id]

    def _abort_child(self, handle: object) -> None:
        """Retire a failed child channel only after Native proves group cleanup.

        Aborting never recovers an operation or makes its ledger sealable. It
        only releases the registration after the exact Native cleanup receipt
        proves that the registered process group is gone.
        """
        with self._lock:
            if (
                self._finished is not False
                or self._token is not _OWNER_TOKEN
                or self._identity._owner is not self
                or self._identity._seal is None
                or type(handle) is not getattr(
                    self._native, "NativeOwnedGeneratorChildProcess", None,
                )
            ):
                raise NativeGitOwnerError("Native abort handle or owner identity is invalid")
            matches = [item for item in self._children.values() if item.process is handle]
            if len(matches) != 1 or matches[0].closed is not False:
                raise NativeGitOwnerError("Native abort handle is not uniquely registered")
            registration = matches[0]
            _require_native_child_cleanup(self, handle, registration)
            try:
                registration.channel.close()
            except OSError as exc:
                raise NativeGitOwnerError("failed child channel could not be closed") from exc
            registration.closed = True
            del self._children[registration.child_id]
            if self._identity._active is True:
                self._revoke_after_child_failure()
            else:
                self._uncertain = True
                if self._borrower._active is True:
                    self._borrower.close()

    def finish(self) -> _SealedGitExecutionLedger:
        with self._lock:
            if self._uncertain is True:
                raise NativeGitOwnerError("uncertain child Git execution cannot seal an owner ledger")
            self._require_active()
            if self._children:
                raise NativeGitOwnerError("cannot seal local Git ledger with registered children")
            self._borrower.close()
            self._identity._active = False
            self._finished = True
            with _OWNER_REGISTRY_LOCK:
                current = _OWNERS_BY_CONTEXT_ID.get(id(self._identity._context))
                if current is not None and current[0] is self._identity._context:
                    del _OWNERS_BY_CONTEXT_ID[id(self._identity._context)]
            return _SealedGitExecutionLedger(
                owner_identity=self._identity, entries=tuple(self._entries),
                _token=_LEDGER_TOKEN,
            )

    def _require_active(self) -> None:
        if (
            self._finished is not False
            or self._identity._active is not True
            or self._identity._owner is not self
            or self._token is not _OWNER_TOKEN
            or self._identity._seal is None
        ):
            raise NativeGitOwnerError("Native local Git owner is closed or revoked")
        _require_native_operation_identity(self._identity)

    def _validate_request(
        self, repository: _LocalRepositoryCapability,
        request: _ClosedLocalGitRequest,
    ) -> None:
        if (
            type(repository) is not _LocalRepositoryCapability
            or repository._owner_identity is not self._identity
            or self._identity._repository_capabilities.get(repository._role.value) is not repository
            or type(request) is not _ClosedLocalGitRequest
        ):
            raise NativeGitOwnerError("local Git request has a foreign repository or request type")
        verb = request.verb
        source_metadata_verbs = {
            self._state._LocalGitVerb.SOURCE_IS_SHALLOW.value,
            self._state._LocalGitVerb.SOURCE_GIT_DIR.value,
            self._state._LocalGitVerb.SOURCE_COMMON_DIR.value,
            self._state._LocalGitVerb.SOURCE_SHALLOW_PATH.value,
            self._state._LocalGitVerb.SOURCE_SHALLOW_BYTES.value,
            self._state._LocalGitVerb.SOURCE_VALIDATE_REF_GRAPH.value,
            self._state._LocalGitVerb.SOURCE_REF_INVENTORY.value,
        }
        if not isinstance(verb, str) or (
            verb not in repository._allowed_verbs and verb not in source_metadata_verbs
        ):
            raise NativeGitOwnerError("local Git verb is outside this repository capability")
        if verb in source_metadata_verbs:
            registration = request._child_registration
            if (
                repository._role is not _LocalRepositoryRole.PREPARED_RELEASE
                or type(registration) is not _RegisteredGitChild
                or self._children.get(registration.child_id) is not registration
                or registration.closed is not False
                or registration.repository is not repository
                or not _is_exact_fixture_test_command(registration.command)
            ):
                raise NativeGitOwnerError(
                    "Source metadata read is limited to the exact registered CHECK14 child",
                )
        if request.stdin_bytes is not None:
            raise NativeGitOwnerError("local Git read requests do not accept input bytes")
        if (
            verb == self._state._LocalGitVerb.STATUS_PORCELAIN_UNTRACKED.value
            and repository._submodules_absent is not True
        ):
            raise NativeGitOwnerError("status requires a Native-issued submodule-free repository proof")
        if verb == self._state._LocalGitVerb.SHOW_BLOB.value:
            history_row = (
                repository._history_read_rows.get((request.commit_sha, request.path))
                if isinstance(request.commit_sha, str) and isinstance(request.path, str)
                else None
            )
            if (
                not isinstance(request.commit_sha, str)
                or not isinstance(request.path, str)
                or request.ancestor_sha is not None
                or request.descendant_sha is not None
                or history_row is not None and history_row.outcome != "present"
                or history_row is None and (
                    request.commit_sha not in repository._normal_commits
                    or request.commit_sha not in repository._allowed_commits
                    or request.path not in repository._allowed_paths
                )
            ):
                raise NativeGitOwnerError("local Git blob request differs from its repository capability")
        elif verb in {
            self._state._LocalGitVerb.SHOW_BLOB_INFO.value,
            self._state._LocalGitVerb.SHOW_BLOB_PAYLOAD.value,
        }:
            history_row = (
                repository._history_read_rows.get((request.commit_sha, request.path))
                if isinstance(request.commit_sha, str) and isinstance(request.path, str)
                else None
            )
            if (
                not isinstance(request.commit_sha, str)
                or not isinstance(request.path, str)
                or request.ancestor_sha is not None
                or request.descendant_sha is not None
                or history_row is None and (
                    request.commit_sha not in repository._normal_commits
                    or request.commit_sha not in repository._allowed_commits
                    or request.path not in repository._allowed_paths
                )
                or history_row is not None
                and history_row.outcome == "absent"
                and verb != self._state._LocalGitVerb.SHOW_BLOB_INFO.value
            ):
                raise NativeGitOwnerError("local Git blob request differs from its repository capability")
            if (
                verb == self._state._LocalGitVerb.SHOW_BLOB_PAYLOAD.value
                and (request.commit_sha, request.path)
                not in repository._verified_blob_facts
            ):
                raise NativeGitOwnerError(
                    "local Git payload requires an exact prior State blob-info read",
                )
        elif verb == self._state._LocalGitVerb.TRUSTED_ORIGIN_MAIN.value:
            if any(
                getattr(request, key) is not None
                for key in ("commit_sha", "path", "ancestor_sha", "descendant_sha")
            ):
                raise NativeGitOwnerError("trusted origin/main request carries unexpected arguments")
        elif verb == self._state._LocalGitVerb.IS_ANCESTOR.value:
            history_pair = (
                (request.ancestor_sha, request.descendant_sha)
                if isinstance(request.ancestor_sha, str)
                and isinstance(request.descendant_sha, str)
                else None
            )
            if (
                not isinstance(request.ancestor_sha, str)
                or not isinstance(request.descendant_sha, str)
                or history_pair not in repository._allowed_ancestry_pairs
                and (
                    request.ancestor_sha not in repository._normal_commits
                    or request.descendant_sha not in repository._normal_commits
                )
                or request.commit_sha is not None or request.path is not None
            ):
                raise NativeGitOwnerError("local Git ancestry request differs from its repository capability")
        elif verb == self._state._LocalGitVerb.REV_PARSE_TREE.value:
            commit_tree_row = (
                repository._history_commit_tree_rows.get(request.commit_sha)
                if isinstance(request.commit_sha, str) else None
            )
            if (
                not isinstance(request.commit_sha, str)
                or commit_tree_row is None
                and request.commit_sha not in repository._normal_commits
                or any(getattr(request, key) is not None for key in ("path", "ancestor_sha", "descendant_sha"))
            ):
                raise NativeGitOwnerError("local Git tree request differs from its repository capability")
        elif verb == self._state._LocalGitVerb.TREE_GITLINKS.value:
            if (
                request.commit_sha not in repository._normal_commits
                or request.commit_sha not in repository._allowed_commits
                or any(getattr(request, key) is not None for key in ("path", "ancestor_sha", "descendant_sha"))
            ):
                raise NativeGitOwnerError("local Git tree-gitlink request differs from its repository capability")
        elif verb == self._state._LocalGitVerb.INDEX_GITLINKS.value:
            if any(
                getattr(request, key) is not None
                for key in ("commit_sha", "path", "ancestor_sha", "descendant_sha")
            ):
                raise NativeGitOwnerError("local Git index-gitlink request carries unexpected arguments")
        elif verb in {
            self._state._LocalGitVerb.REV_PARSE_COMMIT_HEAD.value,
            self._state._LocalGitVerb.STATUS_PORCELAIN_UNTRACKED.value,
        }:
            if any(
                getattr(request, key) is not None
                for key in ("commit_sha", "path", "ancestor_sha", "descendant_sha")
            ):
                raise NativeGitOwnerError("local Git HEAD/status request carries unexpected arguments")
        elif verb in {
            self._state._LocalGitVerb.DIFF_NAMES_FROM_HEAD.value,
            self._state._LocalGitVerb.UNTRACKED_NAMES.value,
        }:
            if (
                repository._role is not _LocalRepositoryRole.PREPARED_RELEASE
                or repository._submodules_absent is not True
                or any(
                    getattr(request, key) is not None
                    for key in ("commit_sha", "path", "ancestor_sha", "descendant_sha")
                )
            ):
                raise NativeGitOwnerError(
                    "prepared worktree query requires its exact submodule-free repository role",
                )
        elif verb in source_metadata_verbs:
            allowed_fields = {
                self._state._LocalGitVerb.SOURCE_SHALLOW_PATH.value,
            }
            if any(
                getattr(request, key) is not None
                for key in ("commit_sha", "ancestor_sha", "descendant_sha")
            ) or request.path is not None:
                raise NativeGitOwnerError("Source metadata request carries an unexpected field")
        else:
            raise NativeGitOwnerError("local Git request verb has no broker translation")

    def _record_result(
        self, operation_kind: str, repository: _LocalRepositoryCapability,
        request: _ClosedLocalGitRequest, result: Any,
    ) -> _GitExecutionLedgerEntry:
        request_bytes = _canonical_json_bytes(_request_dict(request))
        entry = _GitExecutionLedgerEntry(
            operation_kind=operation_kind,
            repository_id=repository._repository_id,
            verb=request.verb,
            request_sha256=hashlib.sha256(request_bytes).hexdigest(),
            returncode=result.returncode,
            stdout_bytes=len(result.stdout),
            stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
            stderr_bytes=len(result.stderr),
            stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
            execution_ordinal=result.execution_ordinal,
            commands_before=result.commands_before,
            commands_after=result.commands_after,
            output_bytes_before=result.output_bytes_before,
            output_bytes_after=result.output_bytes_after,
            started_monotonic_ns=int(result.started_monotonic * 1_000_000_000),
            finished_monotonic_ns=int(result.finished_monotonic * 1_000_000_000),
            process_id=result.process_id,
            process_group_id=result.process_group_id,
            cleanup_confirmed=result.cleanup_confirmed,
        )
        self._entries.append(entry)
        return entry

    def _source_snapshot_binding(
        self, repository: _LocalRepositoryCapability,
        registration: _RegisteredGitChild,
    ) -> tuple[Any, ...]:
        proof = repository._native_role_proof
        return (
            id(self._identity), id(registration), id(repository), id(proof),
            registration.pid, registration.process_group_id,
            registration.session_id, registration.start_ticks, registration.uid,
            str(repository._root), repository._root_identity,
            str(proof.git_metadata_path), repository._git_metadata_identity,
            repository._expected_commit_sha, repository._expected_tree_sha,
            self._identity._deadline_monotonic,
        )

    def _capture_source_inventory(
        self, repository: _LocalRepositoryCapability,
        registration: _RegisteredGitChild, *, operation_kind: str,
    ) -> tuple[bytes, bytes]:
        if (
            type(registration) is not _RegisteredGitChild
            or self._children.get(registration.child_id) is not registration
            or registration.closed is not False
            or registration.repository is not repository
            or not _is_exact_fixture_test_command(registration.command)
            or repository._role is not _LocalRepositoryRole.PREPARED_RELEASE
        ):
            raise NativeGitOwnerError("Source snapshot requires the exact registered CHECK14 prepared role")
        refs = self._run_git(
            repository,
            _ClosedLocalGitRequest(
                verb=self._state._LocalGitVerb.SOURCE_REF_INVENTORY.value,
                _child_registration=registration,
            ),
            operation_kind=operation_kind,
            source_guard=False,
        )
        head = self._run_git(
            repository,
            _ClosedLocalGitRequest(
                verb=self._state._LocalGitVerb.REV_PARSE_COMMIT_HEAD.value,
            ),
            operation_kind=operation_kind,
            source_guard=False,
        )
        if (
            refs.returncode != 0
            or type(refs.stdout) is not bytes
            or len(refs.stdout) > _MAX_CHILD_SOURCE_GRAPH_BYTES
            or head.returncode != 0
            or type(head.stdout) is not bytes
            or not re.fullmatch(rb"[a-f0-9]{40}\n", head.stdout)
        ):
            self._revoke_after_uncertain_execution()
            raise NativeGitOwnerError("prepared Source ref/HEAD snapshot could not be captured exactly")
        _validate_source_ref_inventory(refs.stdout)
        return refs.stdout, head.stdout

    def _revoke_after_uncertain_execution(self) -> None:
        self._identity._active = False
        self._finished = True
        if self._borrower._active:
            self._borrower.close()

    def _revoke_after_child_failure(self) -> None:
        self._identity._active = False
        self._uncertain = True
        if self._borrower._active:
            self._borrower.close()


def _bind_git_operation_owner(
    *, context: object, git: object, control: object,
) -> _NativeGitOperationOwner:
    """Bind one exact Native initial-C context to its existing Git owner."""
    native = _exact_adjacent_module(_NATIVE_MODULE_NAME, "canonical_publication_native.py")
    state = _exact_adjacent_module(_STATE_MODULE_NAME, "canonical_publication_state.py")
    if sys.modules.get(_MODULE_NAME) is not sys.modules.get(__name__):
        raise NativeGitOwnerError("local Git broker is not its canonical module singleton")
    context_type = getattr(native, "NativeInitialCPreparationContext", None)
    control_type = getattr(native, "VerifiedInitialReleaseClosureControl", None)
    if (
        not isinstance(context_type, type) or type(context) is not context_type
        or not isinstance(control_type, type) or type(control) is not control_type
        or type(git) is not state.GitObjectStore
    ):
        raise NativeGitOwnerError("exact Native context, CONTROL, and State store are required")
    deadline = getattr(context, "_deadline_monotonic", None)
    workflow_sha = getattr(context, "_workflow_sha", None)
    tree_sha = getattr(context, "_tree_sha", None)
    eligibility = getattr(control, "_verified_source_eligibility", None)
    try:
        native.require_native_initial_c_preparation_context(
            context, deadline_monotonic=deadline,
        )
        native.require_verified_initial_release_closure_control(
            control, initial_context=context,
            verified_source_eligibility=eligibility,
            deadline_monotonic=deadline,
        )
    except Exception as exc:
        raise NativeGitOwnerError("Native initial-C context or release CONTROL is not current") from exc
    if (
        getattr(context, "_state_store", None) is None
        or context._state_store.git is not git
        or getattr(context, "_native_reader", None) is None
        or context._native_reader.git is not git
        or getattr(control, "_initial_context", None) is not context
        or getattr(control, "_workflow_sha", None) != workflow_sha
        or getattr(control, "_tree_sha", None) != tree_sha
        or isinstance(deadline, bool) or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline) or deadline <= time.monotonic()
        or git.budget.deadline > float(deadline)
        or git._caller_deadline != float(deadline)
        or git.budget.max_commands != 600
        or git.budget.max_output_bytes != 192 * 1024 * 1024
        or git.budget.clock is not time.monotonic
    ):
        raise NativeGitOwnerError("Native context does not own the exact shared State budget and deadline")
    if not isinstance(workflow_sha, str) or not _SHA1.fullmatch(workflow_sha) or workflow_sha == "0" * 40:
        raise NativeGitOwnerError("Native workflow commit identity is malformed")
    if not isinstance(tree_sha, str) or not _SHA1.fullmatch(tree_sha) or tree_sha == "0" * 40:
        raise NativeGitOwnerError("Native workflow tree identity is malformed")
    with _OWNER_REGISTRY_LOCK:
        prior = _OWNERS_BY_CONTEXT_ID.get(id(context))
        if prior is not None and prior[0] is context:
            raise NativeGitOwnerError("Native context already has an active local Git owner")
        operation_id = secrets.token_hex(16)
        identity = _NativeGitOperationIdentity(
            context=context, control=control, git=git,
            deadline_monotonic=float(deadline), workflow_sha=workflow_sha,
            tree_sha=tree_sha, operation_id=operation_id,
            _token=_IDENTITY_TOKEN,
        )
        owner = _NativeGitOperationOwner(
            identity=identity, state=state, native=native, _token=_OWNER_TOKEN,
        )
        _OWNERS_BY_CONTEXT_ID[id(context)] = (context, owner)
        return owner


def _exact_adjacent_module(name: str, expected_filename: str) -> Any:
    module = sys.modules.get(name)
    if module is None:
        raise NativeGitOwnerError(f"exact {name} singleton is not loaded")
    actual_value = getattr(module, "__file__", None)
    if not isinstance(actual_value, str):
        raise NativeGitOwnerError(f"exact {name} source path is unavailable")
    try:
        actual = pathlib.Path(actual_value).resolve(strict=True)
        expected = pathlib.Path(__file__).resolve(strict=True).with_name(expected_filename)
    except OSError as exc:
        raise NativeGitOwnerError(f"exact {name} source path cannot be verified") from exc
    if actual != expected:
        raise NativeGitOwnerError(f"exact {name} is not the adjacent source singleton")
    return module


def _require_native_operation_identity(identity: _NativeGitOperationIdentity) -> None:
    if (
        type(identity) is not _NativeGitOperationIdentity
        or identity._active is not True
        or identity._seal is None
        or identity._owner is None
    ):
        raise NativeGitOwnerError("Native local Git operation identity was revoked")
    if identity._fixture_only is True:
        if (
            identity._context is not None or identity._control is not None
            or identity._owner is None
            or any(
                cap._role is not _LocalRepositoryRole.DISPOSABLE_FIXTURE
                or cap._owner_identity is not identity
                for cap in identity._repository_capabilities.values()
            )
        ):
            raise NativeGitOwnerError("offline fixture owner escaped its fixture-only repository scope")
        return
    native = _exact_adjacent_module(_NATIVE_MODULE_NAME, "canonical_publication_native.py")
    try:
        native.require_native_initial_c_preparation_context(
            identity._context, deadline_monotonic=identity._deadline_monotonic,
        )
        native.require_verified_initial_release_closure_control(
            identity._control, initial_context=identity._context,
            verified_source_eligibility=identity._control._verified_source_eligibility,
            deadline_monotonic=identity._deadline_monotonic,
        )
    except Exception as exc:
        raise NativeGitOwnerError("Native local Git operation context or CONTROL drifted") from exc
    if (
        identity._context._state_store.git is not identity._git
        or identity._git.budget is not identity._budget
        or identity._context._deadline_monotonic != identity._deadline_monotonic
        or identity._control._initial_context is not identity._context
        or identity._context._workflow_sha != identity._workflow_sha
        or identity._context._tree_sha != identity._tree_sha
        or identity._budget.clock() >= identity._deadline_monotonic
    ):
        raise NativeGitOwnerError("Native local Git operation binding changed or expired")


def _validate_state_local_git_issuance(
    *, owner_identity: object, borrower: object,
    repository_capability: object, request: object, git: object,
) -> None:
    """State-side independent issuer check immediately before it seals argv."""
    state = _exact_adjacent_module(_STATE_MODULE_NAME, "canonical_publication_state.py")
    if (
        type(owner_identity) is not _NativeGitOperationIdentity
        or type(borrower) is not state._BorrowedLocalGitExecutor
        or borrower._git is not git
        or borrower._owner_identity is not owner_identity
        or borrower._active is not True
        or owner_identity._git is not git
        or owner_identity._budget is not git.budget
        or owner_identity._owner is None
        or type(repository_capability) is not _LocalRepositoryCapability
        or owner_identity._repository_capabilities.get(
            repository_capability._repository_id,
        ) is not repository_capability
        or owner_identity._repository_capabilities.get(
            repository_capability._role.value,
        ) is not repository_capability
    ):
        raise NativeGitOwnerError("State launch lacks the exact current Native owner and repository")
    _require_native_operation_identity(owner_identity)
    owner = owner_identity._owner
    if type(owner) is not _NativeGitOperationOwner:
        raise NativeGitOwnerError("State launch owner type is not the exact Native broker")
    owner._validate_request(repository_capability, request)


def _validate_state_fixture_git_issuance(
    *, owner_identity: object, borrower: object,
    repository_capability: object, request: object, git: object,
) -> pathlib.Path:
    """Independently revalidate a fixture launch before State translates it."""
    state = _exact_adjacent_module(_STATE_MODULE_NAME, "canonical_publication_state.py")
    if (
        type(owner_identity) is not _NativeGitOperationIdentity
        or type(borrower) is not state._BorrowedLocalGitExecutor
        or borrower._git is not git or borrower._active is not True
        or borrower._owner_identity is not owner_identity
        or owner_identity._git is not git or owner_identity._budget is not git.budget
        or owner_identity._owner is None
        or type(repository_capability) is not _FixtureRepositoryCapability
        or repository_capability._owner_identity is not owner_identity
        or type(request) is not ClosedFixtureGitRequest
        or type(request.verb) is not FixtureGitVerb
        or owner_identity._active is not True
        or owner_identity._budget.clock() >= owner_identity._deadline_monotonic
    ):
        raise NativeGitOwnerError("State fixture launch lacks the exact active owner and typed request")
    _require_native_operation_identity(owner_identity)
    owner = owner_identity._owner
    if type(owner) is not _NativeGitOperationOwner or owner._borrower is not borrower:
        raise NativeGitOwnerError("State fixture launch owner or borrowed executor is foreign")
    registration = owner._children.get(repository_capability._child_id)
    if (
        type(registration) is not _RegisteredGitChild
        or registration.closed is not False
        or registration.fixture_capabilities.get(repository_capability._repository_id)
        is not repository_capability
    ):
        raise NativeGitOwnerError("State fixture launch is not tied to an active registered child")
    _validate_fixture_request(owner, registration, repository_capability, request)
    return owner._validate_fixture_capability(registration, repository_capability)


def _for_offline_transport_qa(
    *, git: object, root: pathlib.Path, commit_sha: str, tree_sha: str,
    allowed_paths: frozenset[str], deadline_monotonic: float | None = None,
) -> tuple[_NativeGitOperationOwner, _LocalRepositoryCapability]:
    """Issue read-only authority for a test repo inside this store's private temp.

    This helper cannot bind production repositories or candidate evidence. It
    exists solely to exercise the actual tokenless State launcher offline.
    """
    state = _exact_adjacent_module(_STATE_MODULE_NAME, "canonical_publication_state.py")
    if type(git) is not state.GitObjectStore:
        raise NativeGitOwnerError("offline QA requires the exact State Git object store")
    root = pathlib.Path(root).resolve(strict=True)
    store_root = git.temp_root.resolve(strict=True)
    if not _contained(root, store_root) or root == store_root:
        raise NativeGitOwnerError("offline QA repository must be below the private State temp root")
    if (
        not isinstance(commit_sha, str) or not _SHA1.fullmatch(commit_sha)
        or commit_sha == "0" * 40
        or not isinstance(tree_sha, str) or not _SHA1.fullmatch(tree_sha)
        or tree_sha == "0" * 40
    ):
        raise NativeGitOwnerError("offline QA commit/tree identity is malformed")
    deadline = git.budget.deadline if deadline_monotonic is None else deadline_monotonic
    if (
        isinstance(deadline, bool) or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline) or deadline > git.budget.deadline
        or git._caller_deadline is not None and deadline > git._caller_deadline
        or deadline <= git.budget.clock()
    ):
        raise NativeGitOwnerError("offline QA cannot extend its State deadline")
    identity = _NativeGitOperationIdentity(
        context=None, control=None, git=git,
        deadline_monotonic=float(deadline), workflow_sha=commit_sha,
        tree_sha=tree_sha, operation_id=secrets.token_hex(16),
        _token=_IDENTITY_TOKEN, fixture_only=True,
    )
    repo_id = _repository_id(
        identity._operation_id, _LocalRepositoryRole.DISPOSABLE_FIXTURE, root,
    )
    verbs = frozenset({
        state._LocalGitVerb.SHOW_BLOB.value,
        state._LocalGitVerb.SHOW_BLOB_INFO.value,
        state._LocalGitVerb.SHOW_BLOB_PAYLOAD.value,
        state._LocalGitVerb.TRUSTED_ORIGIN_MAIN.value,
        state._LocalGitVerb.IS_ANCESTOR.value,
        state._LocalGitVerb.REV_PARSE_COMMIT_HEAD.value,
        state._LocalGitVerb.REV_PARSE_TREE.value,
        state._LocalGitVerb.TREE_GITLINKS.value,
        state._LocalGitVerb.INDEX_GITLINKS.value,
        state._LocalGitVerb.STATUS_PORCELAIN_UNTRACKED.value,
    })
    cap = _LocalRepositoryCapability(
        owner_identity=identity, repository_id=repo_id,
        role=_LocalRepositoryRole.DISPOSABLE_FIXTURE,
        root=root, boundary_root=store_root,
        allowed_paths=frozenset(allowed_paths), allowed_verbs=verbs,
        allowed_commits=frozenset({commit_sha}), submodules_absent=True,
        expected_commit_sha=commit_sha, expected_tree_sha=tree_sha,
        submodules_tree_sha=tree_sha,
        submodules_index_sha256=hashlib.sha256(
            (root / ".git" / "index").read_bytes(),
        ).hexdigest(),
        provisional=False, native_role_proof=None,
        _token=_REPOSITORY_TOKEN,
    )
    identity._repository_capabilities[cap._role.value] = cap
    identity._repository_capabilities[cap._repository_id] = cap
    owner = _NativeGitOperationOwner(
        identity=identity, state=state, native=None, _token=_OWNER_TOKEN,
    )
    return owner, cap


def _directory_identity(path: pathlib.Path) -> tuple[int, int, int, int]:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise NativeGitOwnerError("registered repository directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise NativeGitOwnerError("registered repository directory ownership or mode is invalid")
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
    )


def _contained(path: pathlib.Path, boundary: pathlib.Path) -> bool:
    try:
        return path == boundary or path.is_relative_to(boundary)
    except AttributeError:
        return os.path.commonpath((str(path), str(boundary))) == str(boundary)


def _git_metadata_directory_identity(
    root: pathlib.Path, *, deadline_monotonic: float | None = None,
    clock: Any | None = None,
) -> tuple[int, int, int, int]:
    git_entry = root / ".git"
    try:
        metadata = git_entry.lstat()
    except OSError as exc:
        raise NativeGitOwnerError("registered local repository has no Git metadata directory") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise NativeGitOwnerError("registered local repository Git metadata is not a private directory")
    _validate_git_metadata_descendants(
        git_entry, metadata, deadline_monotonic=deadline_monotonic, clock=clock,
    )
    for relative in (
        "commondir", "config.worktree", "objects/info/alternates",
        "objects/info/http-alternates",
    ):
        try:
            (git_entry / relative).lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NativeGitOwnerError("local Git metadata redirection cannot be checked") from exc
        raise NativeGitOwnerError("local Git metadata redirection is outside the registered repository")
    for relative in ("objects", "objects/info", "objects/pack", "refs"):
        path = git_entry / relative
        try:
            child = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NativeGitOwnerError("local Git metadata directory cannot be checked") from exc
        if not stat.S_ISDIR(child.st_mode) or child.st_uid != os.geteuid():
            raise NativeGitOwnerError("local Git object or ref directory escapes its metadata root")
    for relative in ("HEAD", "config", "index", "packed-refs"):
        path = git_entry / relative
        try:
            child = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NativeGitOwnerError("local Git metadata file cannot be checked") from exc
        if not stat.S_ISREG(child.st_mode) or child.st_uid != os.geteuid():
            raise NativeGitOwnerError("local Git metadata file is not a regular in-root file")
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
    )


def _validate_git_metadata_descendants(
    metadata_root: pathlib.Path, expected_root_stat: os.stat_result, *,
    deadline_monotonic: float | None, clock: Any | None,
) -> None:
    """Boundedly verify every Git metadata descendant without following links."""
    if (deadline_monotonic is None) != (clock is None) or clock is not None and not callable(clock):
        raise NativeGitOwnerError("local Git metadata deadline binding is malformed")
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    close_flag = getattr(os, "O_CLOEXEC", 0)
    expected_identity = (
        expected_root_stat.st_dev, expected_root_stat.st_ino,
        expected_root_stat.st_uid, stat.S_IMODE(expected_root_stat.st_mode),
    )
    try:
        root_fd = os.open(
            metadata_root,
            os.O_RDONLY | directory_flag | nofollow_flag | close_flag,
        )
        root_stat = os.fstat(root_fd)
    except OSError as exc:
        raise NativeGitOwnerError("local Git metadata root cannot be opened without following links") from exc
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.geteuid()
        or root_stat.st_mode & 0o022
        or (root_stat.st_dev, root_stat.st_ino, root_stat.st_uid, stat.S_IMODE(root_stat.st_mode))
        != expected_identity
    ):
        os.close(root_fd)
        raise NativeGitOwnerError("local Git metadata root identity changed during validation")

    open_fds = {root_fd}
    stack: list[tuple[int, int, bytes]] = [(root_fd, 0, b"")]
    entry_count = 0
    try:
        while stack:
            parent_fd, depth, parent_relative = stack.pop()
            try:
                with os.scandir(parent_fd) as children:
                    for child in children:
                        entry_count += 1
                        if (
                            entry_count % 128 == 0
                            and clock is not None
                            and clock() >= deadline_monotonic
                        ):
                            raise NativeGitOwnerError("local Git metadata validation deadline expired")
                        if entry_count > _MAX_GIT_METADATA_ENTRIES:
                            raise NativeGitOwnerError("local Git metadata tree exceeds its entry bound")
                        name = child.name
                        name_bytes = os.fsencode(name)
                        relative = name_bytes if not parent_relative else parent_relative + b"/" + name_bytes
                        if not name_bytes or b"/" in name_bytes or len(relative) > _MAX_GIT_METADATA_PATH_BYTES:
                            raise NativeGitOwnerError("local Git metadata path exceeds its bound")
                        try:
                            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                        except OSError as exc:
                            raise NativeGitOwnerError("local Git metadata entry changed during validation") from exc
                        if before.st_dev != root_stat.st_dev or before.st_uid != os.geteuid():
                            raise NativeGitOwnerError("local Git metadata descendant escapes owner or filesystem")
                        if before.st_mode & 0o022:
                            raise NativeGitOwnerError("local Git metadata descendant is group or world writable")
                        if stat.S_ISDIR(before.st_mode):
                            if depth >= _MAX_GIT_METADATA_DEPTH:
                                raise NativeGitOwnerError("local Git metadata tree exceeds its depth bound")
                            child_fd: int | None = None
                            try:
                                child_fd = os.open(
                                    name,
                                    os.O_RDONLY | directory_flag | nofollow_flag | close_flag,
                                    dir_fd=parent_fd,
                                )
                                after = os.fstat(child_fd)
                            except OSError as exc:
                                if child_fd is not None:
                                    os.close(child_fd)
                                raise NativeGitOwnerError("local Git metadata directory changed during validation") from exc
                            if (
                                not stat.S_ISDIR(after.st_mode)
                                or (before.st_dev, before.st_ino, before.st_uid, stat.S_IMODE(before.st_mode))
                                != (after.st_dev, after.st_ino, after.st_uid, stat.S_IMODE(after.st_mode))
                            ):
                                os.close(child_fd)
                                raise NativeGitOwnerError("local Git metadata directory identity changed")
                            open_fds.add(child_fd)
                            stack.append((child_fd, depth + 1, relative))
                        elif stat.S_ISREG(before.st_mode):
                            if before.st_nlink != 1:
                                raise NativeGitOwnerError("local Git metadata file has an external hard-link alias")
                            child_fd = None
                            try:
                                child_fd = os.open(
                                    name,
                                    os.O_RDONLY | nofollow_flag | close_flag | getattr(os, "O_NONBLOCK", 0),
                                    dir_fd=parent_fd,
                                )
                                after = os.fstat(child_fd)
                            except OSError as exc:
                                raise NativeGitOwnerError("local Git metadata file changed during validation") from exc
                            finally:
                                if child_fd is not None:
                                    os.close(child_fd)
                            if (
                                not stat.S_ISREG(after.st_mode)
                                or (before.st_dev, before.st_ino, before.st_uid, stat.S_IMODE(before.st_mode), before.st_nlink)
                                != (after.st_dev, after.st_ino, after.st_uid, stat.S_IMODE(after.st_mode), after.st_nlink)
                            ):
                                raise NativeGitOwnerError("local Git metadata file identity changed")
                        else:
                            raise NativeGitOwnerError("local Git metadata tree contains a symlink or special file")
            finally:
                open_fds.discard(parent_fd)
                os.close(parent_fd)
        if clock is not None and clock() >= deadline_monotonic:
            raise NativeGitOwnerError("local Git metadata validation deadline expired")
    finally:
        for fd in tuple(open_fds):
            try:
                os.close(fd)
            except OSError:
                pass


def _repository_id(
    operation_id: str, role: _LocalRepositoryRole, root: pathlib.Path,
) -> str:
    return hashlib.sha256(
        b"datapan.native-local-git-repository.v1\0"
        + operation_id.encode("ascii") + b"\0"
        + role.value.encode("ascii") + b"\0"
        + str(root).encode("utf-8"),
    ).hexdigest()[:32]


def _diagnostic_layout_components() -> tuple[str, ...]:
    return tuple(f"{index}-{'p' * 74}" for index in range(5)) + ("control-\x01-path",)


def _diagnostic_layout_path(workspace: pathlib.Path) -> pathlib.Path:
    return workspace.joinpath(*_diagnostic_layout_components())


def _remove_verified_loose_commit(
    state: Any, metadata_root: pathlib.Path, commit_sha: str,
) -> int:
    """Unlink only a single-link loose commit whose compressed bytes hash to its OID."""
    if not isinstance(commit_sha, str) or not _SHA1.fullmatch(commit_sha):
        raise NativeGitOwnerError("diagnostic loose commit identity is malformed")
    parent_fd, leaf = state._open_metadata_parent(
        metadata_root, ("objects", commit_sha[:2], commit_sha[2:]),
    )
    object_fd: int | None = None
    try:
        object_fd = os.open(
            leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        before = os.fstat(object_fd)
        parent = os.fstat(parent_fd)
        max_compressed = 2 * 1024 * 1024
        if (
            not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or before.st_dev != parent.st_dev or before.st_nlink != 1
            or before.st_mode & 0o022 or not 0 < before.st_size <= max_compressed
        ):
            raise NativeGitOwnerError("diagnostic loose commit is not a bounded owned object file")
        compressed = bytearray()
        while len(compressed) <= max_compressed:
            chunk = os.read(object_fd, min(64 * 1024, max_compressed + 1 - len(compressed)))
            if not chunk:
                break
            compressed.extend(chunk)
            if len(compressed) > max_compressed:
                raise NativeGitOwnerError("diagnostic loose commit exceeds the fixed compressed-byte bound")
        after = os.fstat(object_fd)
        if (
            (before.st_dev, before.st_ino, before.st_uid, before.st_mode, before.st_nlink, before.st_size)
            != (after.st_dev, after.st_ino, after.st_uid, after.st_mode, after.st_nlink, after.st_size)
            or len(compressed) != before.st_size
        ):
            raise NativeGitOwnerError("diagnostic loose commit changed during its bounded read")
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(bytes(compressed), 2 * 1024 * 1024 + 1)
        except zlib.error as exc:
            raise NativeGitOwnerError("diagnostic loose commit is not a valid compressed Git object") from exc
        if (
            len(raw) > 2 * 1024 * 1024 or not decoder.eof
            or decoder.unused_data or decoder.unconsumed_tail
        ):
            raise NativeGitOwnerError("diagnostic loose commit expands beyond its exact bounded object")
        header, separator, body = raw.partition(b"\0")
        if (
            not separator or not header.startswith(b"commit ")
            or header[7:] != str(len(body)).encode("ascii")
            or hashlib.sha1(raw).hexdigest() != commit_sha
        ):
            raise NativeGitOwnerError("diagnostic loose object bytes do not prove the exact commit OID")
        named = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if (
            (named.st_dev, named.st_ino, named.st_uid, named.st_mode, named.st_nlink, named.st_size)
            != (before.st_dev, before.st_ino, before.st_uid, before.st_mode, before.st_nlink, before.st_size)
        ):
            raise NativeGitOwnerError("diagnostic loose commit directory entry changed before unlink")
        os.unlink(leaf, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return len(compressed)
    except OSError as exc:
        raise NativeGitOwnerError("diagnostic loose commit could not be removed safely") from exc
    finally:
        if object_fd is not None:
            os.close(object_fd)
        os.close(parent_fd)


def _create_diagnostic_layout(workspace: pathlib.Path, expected: pathlib.Path) -> None:
    if expected != _diagnostic_layout_path(workspace):
        raise NativeGitOwnerError("diagnostic layout path differs from the fixed long-path fixture")
    current = workspace
    for component in _diagnostic_layout_components():
        current = current / component
        current.mkdir(mode=0o700)
        info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or current.resolve(strict=True) != current
        ):
            raise NativeGitOwnerError("diagnostic layout component is not a sealed real directory")


def _validate_diagnostic_layout(
    workspace: pathlib.Path, root: pathlib.Path,
    expected_root_identity: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    if root != _diagnostic_layout_path(workspace):
        raise NativeGitOwnerError("diagnostic layout path differs from its fixed nested form")
    current = workspace
    workspace_info = workspace.lstat()
    parent_device = workspace_info.st_dev
    for component in _diagnostic_layout_components():
        current = current / component
        info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_dev != parent_device or stat.S_IMODE(info.st_mode) != 0o700
            or current.resolve(strict=True) != current
        ):
            raise NativeGitOwnerError("diagnostic layout has a replaced or redirected directory")
        parent_device = info.st_dev
    identity = _directory_identity(root)
    if expected_root_identity is not None and identity != expected_root_identity:
        raise NativeGitOwnerError("diagnostic layout root identity changed")
    return identity


def _request_dict(request: _ClosedLocalGitRequest) -> dict[str, Any]:
    return {
        "verb": request.verb,
        "commit_sha": request.commit_sha,
        "path": request.path,
        "ancestor_sha": request.ancestor_sha,
        "descendant_sha": request.descendant_sha,
    }


def _fixture_request_dict(request: ClosedFixtureGitRequest) -> dict[str, Any]:
    if type(request) is not ClosedFixtureGitRequest or type(request.verb) is not FixtureGitVerb:
        raise NativeGitOwnerError("fixture Git request is not the exact closed request type")
    def cap_id(value: ChildFixtureRepository | ChildSourceRepository | None) -> str | None:
        if value is None:
            return None
        if type(value) is ChildFixtureRepository:
            if value._token is not _CHILD_FIXTURE_CAP_TOKEN:
                raise NativeGitOwnerError("fixture Git request contains a forged repository capability")
            return value._repository_id
        if type(value) is ChildSourceRepository:
            if value._token is not _CHILD_SOURCE_CAP_TOKEN:
                raise NativeGitOwnerError("fixture Git request contains a forged source capability")
            return value._repository_id
        if type(value) is _FixtureRepositoryCapability:
            if value._token is not _FIXTURE_REPOSITORY_TOKEN:
                raise NativeGitOwnerError("fixture Git request contains a forged repository capability")
            return value._repository_id
        if type(value) is _LocalRepositoryCapability:
            if value._owner_identity is None:
                raise NativeGitOwnerError("fixture Git request contains an invalid Native source capability")
            return value._repository_id
        else:
            raise NativeGitOwnerError("fixture Git request contains a forged repository capability")
    content = request.content
    if content is not None and type(content) is not bytes:
        raise NativeGitOwnerError("fixture Git request content is not bytes")
    return {
        "commit_sha": request.commit_sha,
        "content_b64": None if content is None else base64.b64encode(content).decode("ascii"),
        "depth": request.depth,
        "destination_id": cap_id(request.destination),
        "ancestor_sha": request.ancestor_sha,
        "descendant_sha": request.descendant_sha,
        "key": request.key,
        "message": request.message,
        "parent_sha": request.parent_sha,
        "path": request.path,
        "paths": list(request.paths),
        "ref": request.ref,
        "refspec": request.refspec,
        "object_type": request.object_type,
        "revision": request.revision,
        "source_id": cap_id(request.source),
        "value": request.value,
        "verb": request.verb.value,
    }


def _validate_source_ref_inventory(data: bytes) -> None:
    """Validate the bounded, immutable for-each-ref snapshot used for drift checks."""
    if type(data) is not bytes or len(data) > _MAX_CHILD_SOURCE_GRAPH_BYTES:
        raise NativeGitOwnerError("prepared Source ref inventory is outside its fixed bound")
    if not data:
        return
    if not data.endswith(b"\n"):
        raise NativeGitOwnerError("prepared Source ref inventory lacks its canonical record terminator")
    previous_ref: bytes | None = None
    for record in data[:-1].split(b"\n"):
        fields = record.split(b"\0")
        if len(fields) != 3:
            raise NativeGitOwnerError("prepared Source ref inventory has a malformed record")
        name, oid, symref = fields
        if (
            not name.startswith(b"refs/") or not oid or not re.fullmatch(rb"[a-f0-9]{40}", oid)
            or b"\x00" in symref or any(byte < 0x21 or byte > 0x7e for byte in name)
            or b".." in name or b"@{" in name or b"//" in name
            or name.endswith((b"/", b"."))
            or any(part.startswith(b".") or part.endswith((b".", b".lock")) for part in name.split(b"/"))
            or symref and (not symref.startswith(b"refs/") or any(byte < 0x21 or byte > 0x7e for byte in symref))
            or previous_ref is not None and name <= previous_ref
        ):
            raise NativeGitOwnerError("prepared Source ref inventory has an invalid or unsorted ref record")
        previous_ref = name


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _spawn_owned_child(
    owner: _NativeGitOperationOwner, command: object, native_spawn: Any,
) -> Any:
    """Spawn through Native, then bind the returned exact handle to one channel."""
    native = owner._native
    native_module = _exact_adjacent_module(
        _NATIVE_MODULE_NAME, "canonical_publication_native.py",
    )
    command_type = getattr(native_module, "_VerifiedGeneratorCommand", None)
    command_validator = getattr(
        native_module, "_require_initial_verified_generator_command", None,
    )
    child_type = getattr(native_module, "NativeOwnedGeneratorChildProcess", None)
    if (
        not isinstance(command_type, type) or type(command) is not command_type
        or not callable(command_validator)
        or not isinstance(child_type, type)
        or native is not native_module
    ):
        raise NativeGitOwnerError("exact Native verified command or child handle API is unavailable")
    try:
        command_validator(
            command, context=owner._identity._context,
            control=owner._identity._control,
        )
    except Exception as exc:
        raise NativeGitOwnerError("Native generator command is not current and sealed") from exc
    cwd_role = getattr(command, "cwd_role", None)
    role_by_cwd = {
        "generator_root": _LocalRepositoryRole.PREPARED_RELEASE,
        "pinned_cli_checkout": _LocalRepositoryRole.VERIFIED_CLI,
    }
    if type(cwd_role) is not str or cwd_role not in role_by_cwd:
        raise NativeGitOwnerError("Native generator command has an unknown repository role")
    repository = owner.repository(role_by_cwd[cwd_role])
    bootstrap = _new_child_bootstrap(owner, repository)
    try:
        handle = native_spawn(owner._identity._control, command, bootstrap)
        if type(handle) is not child_type:
            raise NativeGitOwnerError("Native child supervisor returned a foreign process handle")
        _register_native_child(owner, handle, command, repository, bootstrap)
        bootstrap._consume_after_spawn(owner=owner)
        return handle
    except BaseException as exc:
        bootstrap._close_unconsumed()
        if owner._identity._active is True:
            owner._revoke_after_uncertain_execution()
        if isinstance(exc, NativeGitOwnerError):
            raise
        raise NativeGitOwnerError("Native child spawn or registration outcome is uncertain") from exc


def _new_child_bootstrap(
    owner: _NativeGitOperationOwner,
    repository: _LocalRepositoryCapability,
) -> _GitChildBootstrap:
    owner._require_active()
    if (
        type(repository) is not _LocalRepositoryCapability
        or repository._owner_identity is not owner._identity
        or owner._identity._repository_capabilities.get(repository._repository_id) is not repository
    ):
        raise NativeGitOwnerError("child bootstrap requires this owner’s registered repository")
    owner._identity._git._validate_local_repository_capability(
        owner._identity, repository,
    )
    if not hasattr(socket, "SOCK_SEQPACKET") or not hasattr(socket, "SCM_CREDENTIALS"):
        raise NativeGitOwnerError("platform lacks the required authenticated local socket protocol")
    parent_socket: socket.socket | None = None
    child_socket: socket.socket | None = None
    try:
        parent_socket, child_socket = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_SEQPACKET,
        )
        parent_socket.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        child_socket.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        parent_socket.setblocking(False)
        child_socket.setblocking(True)
        remaining = owner._identity._deadline_monotonic - owner._identity._budget.clock()
        if remaining <= 0:
            raise NativeGitOwnerError("Native operation expired before child channel creation")
        child_socket.settimeout(remaining)
        return _GitChildBootstrap(
            owner=owner, child_id=secrets.token_hex(16), nonce=secrets.token_hex(16),
            repository=repository, parent_socket=parent_socket,
            child_socket=child_socket, _token=_CHILD_BOOTSTRAP_TOKEN,
        )
    except BaseException:
        if parent_socket is not None:
            parent_socket.close()
        if child_socket is not None:
            child_socket.close()
        raise


def _register_native_child(
    owner: _NativeGitOperationOwner, handle: object, command: object,
    repository: _LocalRepositoryCapability, bootstrap: _GitChildBootstrap,
) -> None:
    native = _exact_adjacent_module(
        _NATIVE_MODULE_NAME, "canonical_publication_native.py",
    )
    child_validator = getattr(
        native, "_require_initial_owned_generator_child", None,
    )
    if not callable(child_validator):
        raise NativeGitOwnerError("Native child identity validator is unavailable")
    try:
        child_validator(
            handle, context=owner._identity._context,
            control=owner._identity._control, command=command,
            bootstrap=bootstrap,
        )
    except Exception as exc:
        raise NativeGitOwnerError("Native child handle is not bound to this command/channel") from exc
    launch_binding_validator = getattr(
        native, "_require_initial_owned_generator_child_launch_binding", None,
    )
    if not callable(launch_binding_validator):
        raise NativeGitOwnerError("Native child outer/inner launch binding validator is unavailable")
    try:
        launch_binding = launch_binding_validator(
            handle, context=owner._identity._context,
            control=owner._identity._control, command=command,
        )
    except Exception as exc:
        raise NativeGitOwnerError("Native child outer/inner launch identity is not registered") from exc
    if (
        type(launch_binding) is not tuple or len(launch_binding) != 3
        or type(launch_binding[0]) is not tuple
        or any(type(arg) is not str or not arg for arg in launch_binding[0])
        or getattr(handle, "launch_argv", None) != launch_binding[0]
        or launch_binding[1] != getattr(handle, "source_runtime_package_sha256", None)
        or launch_binding[2] != getattr(handle, "source_runtime_bundle_sha256", None)
        or bootstrap._command_id != getattr(command, "command_id", None)
        or bootstrap._source_package_sha256 != launch_binding[1]
        or bootstrap._source_bundle_sha256 != launch_binding[2]
    ):
        raise NativeGitOwnerError("Native launch binding facts do not match the child handle")
    prepared_input_reader = getattr(handle, "prepared_input_reader", None)
    if prepared_input_reader is not None:
        input_validator = getattr(
            native, "require_initial_prepared_input_reader", None,
        )
        if not callable(input_validator):
            raise NativeGitOwnerError("Native prepared-input reader validator is unavailable")
        try:
            prepared_input_reader = input_validator(
                prepared_input_reader, context=owner._identity._context,
                control=owner._identity._control, command=command,
                child_id=bootstrap._child_id,
                child=handle,
            )
        except Exception as exc:
            raise NativeGitOwnerError("prepared-input reader is not issued to this child") from exc
    pid = _required_native_int(handle, "pid", minimum=1)
    process_group_id = _required_native_int(handle, "process_group_id", minimum=1)
    session_id = _required_native_int(handle, "session_id", minimum=1)
    command_id = getattr(command, "command_id", None)
    if getattr(handle, "command_id", None) != command_id:
        raise NativeGitOwnerError("Native child handle changed its verified command identity")
    proc_pgrp, proc_session, start_ticks, uid, gid = _process_identity(pid)
    if (
        proc_pgrp != process_group_id or proc_session != session_id
        or uid != os.geteuid() or gid != os.getegid()
    ):
        raise NativeGitOwnerError("Native child PID/PGID/session/UID differs from the registered process")
    supplied_start_ticks = _required_native_int(handle, "start_ticks", minimum=1)
    if supplied_start_ticks != start_ticks:
        raise NativeGitOwnerError("Native child process start identity changed during registration")
    registration = _RegisteredGitChild(
        child_id=bootstrap._child_id, repository=repository,
        bootstrap=bootstrap, process=handle, pid=pid,
        process_group_id=process_group_id, session_id=session_id,
        start_ticks=start_ticks, uid=uid, command=command,
        launch_argv=launch_binding[0],
        source_package_sha256=launch_binding[1],
        source_bundle_sha256=launch_binding[2],
        prepared_input_reader=prepared_input_reader,
    )
    if registration.child_id in owner._children:
        raise NativeGitOwnerError("Native child ID was already registered")
    owner._children[registration.child_id] = registration


_CHECK14_TEST_ARGV = (
    "python3", "-m", "unittest",
    "tests/test_generate_completeness_proof_rollup.py",
    "tests/test_completeness_publication_evidence.py",
    "tests/test_completeness_scope_registration.py",
    "tests/test_completeness_proof_rollup_cli.py",
    "tests/test_completeness_future_observation_chain.py",
)


def _is_exact_fixture_test_command(command: object | None) -> bool:
    if command is None:
        return False
    argv = getattr(command, "argv", None)
    return (
        type(argv) is tuple and argv == _CHECK14_TEST_ARGV
        and getattr(command, "phase", None) == "final_check"
        and getattr(command, "cwd_role", None) == "prepared_release"
        and getattr(command, "source_path", None)
        == "scripts/refresh-release-ledger-evidence.py"
    )


def _registered_child_for_handle(
    owner: _NativeGitOperationOwner, handle: object,
) -> _RegisteredGitChild:
    if type(handle) is not getattr(owner._native, "NativeOwnedGeneratorChildProcess", None):
        raise NativeGitOwnerError("Native supervisor supplied a foreign child handle")
    matches = [item for item in owner._children.values() if item.process is handle]
    if len(matches) != 1 or matches[0].closed is not False:
        raise NativeGitOwnerError("Native child is not uniquely registered or already closed")
    _verify_registered_child_identity(owner, matches[0])
    return matches[0]


def _require_native_child_cleanup(
    owner: _NativeGitOperationOwner, handle: object,
    registration: _RegisteredGitChild,
) -> None:
    native = _exact_adjacent_module(
        _NATIVE_MODULE_NAME, "canonical_publication_native.py",
    )
    validator = getattr(
        native, "_require_initial_owned_generator_child_cleanup", None,
    )
    if not callable(validator):
        raise NativeGitOwnerError("Native child cleanup receipt validator is unavailable")
    try:
        validator(
            handle, context=owner._identity._context,
            control=owner._identity._control,
        )
    except Exception as exc:
        raise NativeGitOwnerError("Native child process-group cleanup is not confirmed") from exc
    if handle is not registration.process:
        raise NativeGitOwnerError("Native child cleanup receipt is foreign to its registration")


def _required_native_int(value: object, field_name: str, *, minimum: int) -> int:
    result = getattr(value, field_name, None)
    if type(result) is not int or result < minimum:
        raise NativeGitOwnerError(f"Native child field {field_name} is malformed")
    return result


def _process_identity(pid: int) -> tuple[int, int, int, int, int]:
    if type(pid) is not int or pid < 1:
        raise NativeGitOwnerError("Native child PID is malformed")
    try:
        stat_bytes = pathlib.Path(f"/proc/{pid}/stat").read_bytes()
        right_paren = stat_bytes.rfind(b")")
        if right_paren < 0:
            raise ValueError("malformed proc stat")
        tail = stat_bytes[right_paren + 2 :].split()
        if len(tail) <= 19:
            raise ValueError("short proc stat")
        process_group_id = int(tail[2])
        session_id = int(tail[3])
        start_ticks = int(tail[19])
        metadata = pathlib.Path(f"/proc/{pid}").stat()
        status = pathlib.Path(f"/proc/{pid}/status").read_text(encoding="ascii")
        uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
        gid_line = next(line for line in status.splitlines() if line.startswith("Gid:"))
        uid = int(uid_line.split()[2])
        gid = int(gid_line.split()[2])
    except (OSError, ValueError, StopIteration, IndexError) as exc:
        raise NativeGitOwnerError("Native child process identity cannot be read") from exc
    if metadata.st_uid != os.geteuid() or start_ticks < 1:
        raise NativeGitOwnerError("Native child process owner or start identity is invalid")
    return process_group_id, session_id, start_ticks, uid, gid


def _verify_registered_child_identity(
    owner: _NativeGitOperationOwner,
    registration: _RegisteredGitChild,
    *,
    peer_pid: int | None = None,
    peer_uid: int | None = None,
    peer_gid: int | None = None,
) -> None:
    if (
        registration.process is None
        or registration.closed is not False
        or registration.child_id not in owner._children
        or owner._children[registration.child_id] is not registration
    ):
        raise NativeGitOwnerError("registered Native child channel is revoked")
    if peer_pid is not None and (
        peer_pid != registration.pid or peer_uid != registration.uid
        or peer_gid != os.getegid()
    ):
        raise NativeGitOwnerError("child socket peer credentials do not match its registered process")
    pgrp, session, start_ticks, uid, gid = _process_identity(registration.pid)
    if (
        pgrp != registration.process_group_id
        or session != registration.session_id
        or start_ticks != registration.start_ticks
        or uid != registration.uid or gid != os.getegid()
    ):
        raise NativeGitOwnerError("registered Native child process identity drifted")


def _serve_one_child_message(
    owner: _NativeGitOperationOwner,
    registration: _RegisteredGitChild,
    *, timeout: float,
) -> bool:
    if registration.goodbye_complete is True:
        return False
    if registration.hello_complete is False:
        _verify_registered_child_identity(owner, registration)
    selector = selectors.DefaultSelector()
    try:
        selector.register(registration.channel, selectors.EVENT_READ)
        if not selector.select(timeout):
            return False
    finally:
        selector.close()
    payload, credentials = _receive_packet(registration.channel)
    _verify_registered_child_identity(
        owner, registration, peer_pid=credentials[0],
        peer_uid=credentials[1], peer_gid=credentials[2],
    )
    message = _decode_json_packet(payload)
    if registration.hello_complete is False:
        _validate_child_hello(owner, registration, message)
        registration.hello_complete = True
        _send_json_packet(registration.channel, {
            "child_id": registration.child_id,
            "kind": "hello_ack",
            "nonce": registration.bootstrap._nonce,
            "operation_id": owner._identity._operation_id,
            "repository_id": registration.repository._repository_id,
            "command_id": registration.inner_command_id,
            "source_package_sha256": registration.source_package_sha256 or "",
            "source_bundle_sha256": registration.source_bundle_sha256 or "",
            "repository_role": registration.repository._role.value,
            "repository_root": str(registration.repository._root),
            "repository_root_identity": list(registration.repository._root_identity),
            "prepared_input_enabled": registration.prepared_input_reader is not None,
            "version": _CHILD_PROTOCOL_VERSION,
        }, deadline_monotonic=owner._identity._deadline_monotonic)
        return True
    kind = message.get("kind")
    if kind == "diagnostic_remove":
        return _serve_diagnostic_remove(owner, registration, message)
    if kind == "prepared_input_request":
        return _serve_prepared_input_request(owner, registration, message)
    if kind == "diagnostic_allocate":
        expected_keys = {
            "child_id", "kind", "layout_id", "operation_id", "repository_id",
            "request_id", "role", "sequence", "version",
        }
        sequence = message.get("sequence")
        request_id = message.get("request_id")
        layout_id = message.get("layout_id")
        if (
            set(message) != expected_keys
            or type(message.get("version")) is not int
            or message.get("version") != _CHILD_PROTOCOL_VERSION
            or message.get("child_id") != registration.child_id
            or message.get("operation_id") != owner._identity._operation_id
            or message.get("repository_id") != registration.repository._repository_id
            or message.get("role") not in {"source", "disposable"}
            or not isinstance(layout_id, str) or not _HEX32.fullmatch(layout_id)
            or type(sequence) is not int or not (1 <= sequence < 2**63)
            or not isinstance(request_id, str) or not _HEX32.fullmatch(request_id)
        ):
            raise NativeGitOwnerError("diagnostic allocation request differs from its closed protocol")
        request_sha = hashlib.sha256(_canonical_json_bytes(message)).hexdigest()
        if sequence == registration.pending_sequence:
            if (
                request_id != registration.pending_request_id
                or request_sha != registration.pending_request_sha256
                or not registration.pending_packets
            ):
                raise NativeGitOwnerError("diagnostic allocation replay changed its retained identity")
            _send_packet_sequence(
                registration.channel, registration.pending_packets,
                deadline_monotonic=owner._identity._deadline_monotonic,
            )
            return True
        if sequence != registration.next_sequence or registration.pending_sequence is not None:
            raise NativeGitOwnerError("diagnostic allocation sequence is stale, skipped, or unacknowledged")
        layout = registration.fixture_capabilities.get(layout_id)
        if type(layout) is not _FixtureRepositoryCapability:
            raise NativeGitOwnerError("diagnostic allocation layout is not registered to this child")
        capability = owner._new_diagnostic_repository(registration, layout, message["role"])
        root_identity = capability._root_identity
        packet = _canonical_json_bytes({
            "child_id": registration.child_id,
            "fixture_id": capability._repository_id,
            "fixture_kind": capability._kind.value,
            "kind": "fixture_cap",
            "operation_id": owner._identity._operation_id,
            "request_id": request_id,
            "repository_id": registration.repository._repository_id,
            "root": str(capability._root),
            "root_identity": None if root_identity is None else list(root_identity),
            "sequence": sequence,
            "version": _CHILD_PROTOCOL_VERSION,
            "workspace": str(capability._workspace),
            "workspace_identity": list(capability._workspace_identity),
        })
        registration.pending_sequence = sequence
        registration.pending_request_id = request_id
        registration.pending_request_sha256 = request_sha
        registration.pending_packets = (packet,)
        registration.next_sequence += 1
        _send_packet_sequence(
            registration.channel, registration.pending_packets,
            deadline_monotonic=owner._identity._deadline_monotonic,
        )
        return True
    if kind == "prepared_input_request":
        return _serve_prepared_input_request(owner, registration, message)
    if kind in {"fixture_allocate", "fixture_request"}:
        sequence = message.get("sequence")
        request_id = message.get("request_id")
        fixture_id = message.get("fixture_id")
        if (
            set(message) != (
                {"child_id", "kind", "operation_id", "repository_id", "request_id", "request", "sequence", "version"}
                if kind == "fixture_allocate" else
                {"child_id", "kind", "operation_id", "repository_id", "request_id", "fixture_id", "request", "sequence", "version"}
            )
            or type(message.get("version")) is not int
            or message.get("version") != _CHILD_PROTOCOL_VERSION
            or message.get("child_id") != registration.child_id
            or message.get("operation_id") != owner._identity._operation_id
            or message.get("repository_id") != registration.repository._repository_id
            or type(sequence) is not int or not (1 <= sequence < 2**63)
            or not isinstance(request_id, str) or not _HEX32.fullmatch(request_id)
            or kind == "fixture_request"
            and (not isinstance(fixture_id, str) or not _HEX32.fullmatch(fixture_id))
            or not isinstance(message.get("request"), dict)
        ):
            raise NativeGitOwnerError("fixture child request differs from its bounded protocol envelope")
        raw_request = message["request"]
        request_digest_bytes = _canonical_json_bytes({
            "fixture_id": fixture_id, "kind": kind, "request": raw_request,
        })
        request_sha = hashlib.sha256(request_digest_bytes).hexdigest()
        if sequence == registration.pending_sequence:
            if (
                request_id != registration.pending_request_id
                or request_sha != registration.pending_request_sha256
                or not registration.pending_packets
            ):
                raise NativeGitOwnerError("fixture request replay changed its retained identity")
            _send_packet_sequence(
                registration.channel, registration.pending_packets,
                deadline_monotonic=owner._identity._deadline_monotonic,
            )
            return True
        if sequence != registration.next_sequence or registration.pending_sequence is not None:
            raise NativeGitOwnerError("fixture request sequence is stale, skipped, or unacknowledged")
        if kind == "fixture_allocate":
            if set(raw_request) != {"kind"} or not isinstance(raw_request.get("kind"), str):
                raise NativeGitOwnerError("fixture allocation request schema is malformed")
            try:
                fixture_kind = FixtureRepositoryKind(raw_request["kind"])
            except ValueError as exc:
                raise NativeGitOwnerError("fixture allocation kind is outside the closed set") from exc
            capability = owner._new_fixture_repository(registration, fixture_kind)
            root_identity = capability._root_identity
            packet = _canonical_json_bytes({
                "child_id": registration.child_id,
                "fixture_id": capability._repository_id,
                "fixture_kind": capability._kind.value,
                "kind": "fixture_cap",
                "operation_id": owner._identity._operation_id,
                "request_id": request_id,
                "repository_id": registration.repository._repository_id,
                "root": str(capability._root),
                "root_identity": None if root_identity is None else list(root_identity),
                "sequence": sequence,
                "version": _CHILD_PROTOCOL_VERSION,
                "workspace": str(capability._workspace),
                "workspace_identity": list(capability._workspace_identity),
            })
            packets = (packet,)
        else:
            capability, request = _decode_fixture_request(
                owner, registration, fixture_id, raw_request,
            )
            result = owner._run_fixture(registration, capability, request)
            packets = _result_packet_sequence(
                owner, registration, request, result, sequence, request_id,
                request_sha, fixture_id=fixture_id,
            )
        registration.pending_sequence = sequence
        registration.pending_request_id = request_id
        registration.pending_request_sha256 = request_sha
        registration.pending_packets = packets
        registration.next_sequence += 1
        _send_packet_sequence(
            registration.channel, packets,
            deadline_monotonic=owner._identity._deadline_monotonic,
        )
        return True
    if kind == "goodbye":
        expected = {
            "child_id": registration.child_id,
            "kind": "goodbye",
            "operation_id": owner._identity._operation_id,
            "repository_id": registration.repository._repository_id,
            "version": _CHILD_PROTOCOL_VERSION,
        }
        if (
            message != expected or type(message.get("version")) is not int
            or registration.pending_sequence is not None
        ):
            raise NativeGitOwnerError("child Git GOODBYE arrived with an active result or malformed identity")
        registration.goodbye_complete = True
        _send_json_packet(registration.channel, {
            "child_id": registration.child_id,
            "kind": "goodbye_ack",
            "operation_id": owner._identity._operation_id,
            "repository_id": registration.repository._repository_id,
            "version": _CHILD_PROTOCOL_VERSION,
        }, deadline_monotonic=owner._identity._deadline_monotonic)
        return True
    if kind == "ack":
        _accept_child_ack(owner, registration, message)
        _send_json_packet(registration.channel, {
            "child_id": registration.child_id,
            "kind": "ack_confirm",
            "operation_id": owner._identity._operation_id,
            "repository_id": registration.repository._repository_id,
            "request_id": message["request_id"],
            "sequence": message["sequence"],
            "version": _CHILD_PROTOCOL_VERSION,
        }, deadline_monotonic=owner._identity._deadline_monotonic)
        return True
    if kind != "request":
        raise NativeGitOwnerError("child Git channel sent an unknown message type")
    request, sequence, request_id = _decode_child_request(
        owner, registration, message,
    )
    request_sha = hashlib.sha256(_canonical_json_bytes(_request_dict(request))).hexdigest()
    if sequence == registration.pending_sequence:
        if (
            request_id != registration.pending_request_id
            or request_sha != registration.pending_request_sha256
            or not registration.pending_packets
        ):
            raise NativeGitOwnerError("child replay changed the retained request identity")
        _send_packet_sequence(
            registration.channel, registration.pending_packets,
            deadline_monotonic=owner._identity._deadline_monotonic,
        )
        return True
    if (
        sequence != registration.next_sequence
        or registration.pending_sequence is not None
    ):
        raise NativeGitOwnerError("child Git request sequence is stale, skipped, or unacknowledged")
    result = owner._run_git(
        registration.repository, request, operation_kind="child",
    )
    packets = _result_packet_sequence(
        owner, registration, request, result, sequence, request_id,
        request_sha,
    )
    registration.pending_sequence = sequence
    registration.pending_request_id = request_id
    registration.pending_request_sha256 = request_sha
    registration.pending_packets = packets
    registration.next_sequence += 1
    _send_packet_sequence(
        registration.channel, packets,
        deadline_monotonic=owner._identity._deadline_monotonic,
    )
    return True


def _serve_diagnostic_remove(
    owner: _NativeGitOperationOwner,
    registration: _RegisteredGitChild,
    message: dict[str, Any],
) -> bool:
    expected_keys = {
        "child_id", "kind", "layout_id", "operation_id", "receipt_id",
        "repository_id", "request_id", "sequence", "version",
    }
    sequence = message.get("sequence")
    request_id = message.get("request_id")
    layout_id = message.get("layout_id")
    receipt_id = message.get("receipt_id")
    if (
        set(message) != expected_keys
        or message.get("kind") != "diagnostic_remove"
        or type(message.get("version")) is not int
        or message.get("version") != _CHILD_PROTOCOL_VERSION
        or message.get("child_id") != registration.child_id
        or message.get("operation_id") != owner._identity._operation_id
        or message.get("repository_id") != registration.repository._repository_id
        or type(sequence) is not int or not (1 <= sequence < 2**63)
        or not isinstance(request_id, str) or not _HEX32.fullmatch(request_id)
        or not isinstance(layout_id, str) or not _HEX32.fullmatch(layout_id)
        or not isinstance(receipt_id, str) or not _HEX32.fullmatch(receipt_id)
    ):
        raise NativeGitOwnerError("diagnostic removal request differs from its closed protocol")
    request_sha = hashlib.sha256(_canonical_json_bytes(message)).hexdigest()
    if sequence == registration.pending_sequence:
        if (
            request_id != registration.pending_request_id
            or request_sha != registration.pending_request_sha256
            or not registration.pending_packets
        ):
            raise NativeGitOwnerError("diagnostic removal replay changed its retained identity")
        _send_packet_sequence(
            registration.channel, registration.pending_packets,
            deadline_monotonic=owner._identity._deadline_monotonic,
        )
        return True
    if sequence != registration.next_sequence or registration.pending_sequence is not None:
        raise NativeGitOwnerError("diagnostic removal sequence is stale, skipped, or unacknowledged")
    layout = registration.fixture_capabilities.get(layout_id)
    if type(layout) is not _FixtureRepositoryCapability:
        raise NativeGitOwnerError("diagnostic removal layout is not registered to this child")
    try:
        removed_bytes, source_id, destination_id = owner._remove_diagnostic_cloned_commit(
            registration, layout, receipt_id,
        )
    except BaseException:
        owner._revoke_after_uncertain_execution()
        raise
    packet = _canonical_json_bytes({
        "child_id": registration.child_id,
        "destination_id": destination_id,
        "kind": "diagnostic_remove_result",
        "layout_id": layout_id,
        "operation_id": owner._identity._operation_id,
        "receipt_id": receipt_id,
        "removed_bytes": removed_bytes,
        "repository_id": registration.repository._repository_id,
        "request_id": request_id,
        "sequence": sequence,
        "source_id": source_id,
        "version": _CHILD_PROTOCOL_VERSION,
    })
    registration.pending_sequence = sequence
    registration.pending_request_id = request_id
    registration.pending_request_sha256 = request_sha
    registration.pending_packets = (packet,)
    registration.next_sequence += 1
    _send_packet_sequence(
        registration.channel, registration.pending_packets,
        deadline_monotonic=owner._identity._deadline_monotonic,
    )
    return True


def _serve_prepared_input_request(
    owner: _NativeGitOperationOwner,
    registration: _RegisteredGitChild,
    message: dict[str, Any],
) -> bool:
    """Serve one State-owned read from the exact current prepared scratch."""
    expected_keys = {
        "child_id", "kind", "operation", "operation_id", "path", "prefix",
        "repository_id", "request_id", "sequence", "suffix", "version",
    }
    if (
        set(message) != expected_keys
        or message.get("kind") != "prepared_input_request"
        or type(message.get("version")) is not int
        or message.get("version") != _CHILD_PROTOCOL_VERSION
        or message.get("child_id") != registration.child_id
        or message.get("operation_id") != owner._identity._operation_id
        or message.get("repository_id") != registration.repository._repository_id
        or type(message.get("sequence")) is not int
        or not (1 <= message["sequence"] < 2**63)
        or not isinstance(message.get("request_id"), str)
        or not _HEX32.fullmatch(message["request_id"])
    ):
        raise NativeGitOwnerError("prepared-input request differs from its closed protocol")
    operation = message["operation"]
    path = message["path"]
    prefix = message["prefix"]
    suffix = message["suffix"]
    if operation == "read_bytes":
        if (
            type(path) is not str or not path
            or _safe_path(path) != path
            or prefix is not None or suffix is not None
        ):
            raise NativeGitOwnerError("prepared-input read path is not one canonical member")
    elif operation == "list_paths":
        if path is not None or prefix != "sources/" or suffix != ".json":
            raise NativeGitOwnerError("prepared-input inventory selector is outside its fixed form")
    else:
        raise NativeGitOwnerError("prepared-input operation is outside its closed set")
    if registration.prepared_input_reader is None:
        raise NativeGitOwnerError("child has no State-issued prepared-input reader")

    sequence = message["sequence"]
    request_id = message["request_id"]
    request_sha = hashlib.sha256(_canonical_json_bytes(message)).hexdigest()
    if sequence == registration.pending_sequence:
        if (
            request_id != registration.pending_request_id
            or request_sha != registration.pending_request_sha256
            or not registration.pending_packets
        ):
            raise NativeGitOwnerError("prepared-input replay changed its retained identity")
        _send_packet_sequence(
            registration.channel, registration.pending_packets,
            deadline_monotonic=owner._identity._deadline_monotonic,
        )
        return True
    if sequence != registration.next_sequence or registration.pending_sequence is not None:
        raise NativeGitOwnerError("prepared-input sequence is stale, skipped, or unacknowledged")

    native = _exact_adjacent_module(_NATIVE_MODULE_NAME, "canonical_publication_native.py")
    serve = getattr(native, "_serve_initial_prepared_input_request", None)
    if not callable(serve):
        raise NativeGitOwnerError("Native prepared-input State bridge is unavailable")
    try:
        value = serve(
            registration.process, reader=registration.prepared_input_reader,
            operation=operation, path=path, prefix=prefix, suffix=suffix,
        )
    except FileNotFoundError:
        if operation != "read_bytes":
            raise NativeGitOwnerError("prepared-input inventory cannot be absent")
        value = None
    if operation == "read_bytes":
        if value is not None and type(value) is not bytes:
            raise NativeGitOwnerError("State prepared-input read did not return exact bytes")
        present = value is not None
        payload = b"" if value is None else value
        payload_kind = "bytes"
        result_path: str | None = path
    else:
        if (
            type(value) not in {tuple, list}
            or len(value) > 4096
            or any(
                type(item) is not str or not item.startswith("sources/")
                or not item.endswith(".json") or _safe_path(item) != item
                for item in value
            )
            or tuple(value) != tuple(sorted(set(value), key=lambda item: item.encode("utf-8")))
        ):
            raise NativeGitOwnerError("State source-profile inventory is not its exact sorted path set")
        payload = _canonical_json_bytes(list(value))
        if len(payload) > _MAX_CHILD_INPUT_LIST_BYTES:
            raise NativeGitOwnerError("State source-profile inventory exceeds its fixed frame bound")
        present = True
        payload_kind = "paths"
        result_path = None
    if len(payload) > (_MAX_CHILD_INPUT_BYTES if operation == "read_bytes" else _MAX_CHILD_INPUT_LIST_BYTES):
        raise NativeGitOwnerError("prepared-input State response exceeds its fixed bound")
    metadata = _canonical_json_bytes({
        "child_id": registration.child_id,
        "kind": "prepared_input_result",
        "operation": operation,
        "operation_id": owner._identity._operation_id,
        "payload_kind": payload_kind,
        "payload_length": len(payload),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "path": result_path,
        "present": present,
        "repository_id": registration.repository._repository_id,
        "request_id": request_id,
        "request_sha256": request_sha,
        "sequence": sequence,
        "version": _CHILD_PROTOCOL_VERSION,
    })
    packets = [metadata]
    for offset in range(0, len(payload), _CHILD_RESULT_CHUNK_BYTES):
        packets.append(
            b"D" + b"I" + struct.pack(">I", offset)
            + payload[offset:offset + _CHILD_RESULT_CHUNK_BYTES]
        )
    if any(len(packet) > _MAX_CHILD_FRAME_BYTES for packet in packets):
        raise NativeGitOwnerError("prepared-input result frame exceeds its fixed size cap")
    registration.pending_sequence = sequence
    registration.pending_request_id = request_id
    registration.pending_request_sha256 = request_sha
    registration.pending_packets = tuple(packets)
    registration.next_sequence += 1
    _send_packet_sequence(
        registration.channel, registration.pending_packets,
        deadline_monotonic=owner._identity._deadline_monotonic,
    )
    return True


def _validate_child_hello(
    owner: _NativeGitOperationOwner,
    registration: _RegisteredGitChild,
    message: dict[str, Any],
) -> None:
    expected = {
        "child_id": registration.child_id,
        "kind": "hello",
        "nonce": registration.bootstrap._nonce,
        "operation_id": owner._identity._operation_id,
        "owner_pid": os.getpid(),
        "repository_id": registration.repository._repository_id,
        "command_id": registration.inner_command_id,
        "source_package_sha256": registration.source_package_sha256 or "",
        "source_bundle_sha256": registration.source_bundle_sha256 or "",
        "version": _CHILD_PROTOCOL_VERSION,
    }
    if message != expected:
        raise NativeGitOwnerError("child one-use HELLO differs from its registered bootstrap")
    if (
        type(message.get("version")) is not int
        or type(message.get("owner_pid")) is not int
    ):
        raise NativeGitOwnerError("child one-use HELLO identity types are malformed")


def _accept_child_ack(
    owner: _NativeGitOperationOwner,
    registration: _RegisteredGitChild,
    message: dict[str, Any],
) -> None:
    expected = {
        "child_id": registration.child_id,
        "kind": "ack",
        "operation_id": owner._identity._operation_id,
        "repository_id": registration.repository._repository_id,
        "request_id": registration.pending_request_id,
        "sequence": registration.pending_sequence,
        "version": _CHILD_PROTOCOL_VERSION,
    }
    if registration.pending_sequence is None or message != expected:
        raise NativeGitOwnerError("child Git acknowledgment does not match its retained result")
    if type(message.get("version")) is not int or type(message.get("sequence")) is not int:
        raise NativeGitOwnerError("child Git acknowledgment identity types are malformed")
    registration.pending_sequence = None
    registration.pending_request_id = None
    registration.pending_request_sha256 = None
    registration.pending_packets = ()
    registration.pending_diagnostic_clone_receipt_id = None


def _decode_child_request(
    owner: _NativeGitOperationOwner,
    registration: _RegisteredGitChild,
    message: dict[str, Any],
) -> tuple[_ClosedLocalGitRequest, int, str]:
    expected_keys = {
        "child_id", "kind", "operation_id", "repository_id", "request_id",
        "request", "sequence", "version",
    }
    if set(message) != expected_keys:
        raise NativeGitOwnerError("child Git request fields differ from the closed protocol")
    if (
        message.get("kind") != "request"
        or type(message.get("version")) is not int
        or message.get("version") != _CHILD_PROTOCOL_VERSION
        or message.get("operation_id") != owner._identity._operation_id
        or message.get("child_id") != registration.child_id
        or message.get("repository_id") != registration.repository._repository_id
    ):
        raise NativeGitOwnerError("child Git request names another operation, child, or repository")
    sequence = message.get("sequence")
    request_id = message.get("request_id")
    if type(sequence) is not int or not (1 <= sequence < 2**63):
        raise NativeGitOwnerError("child Git request sequence is malformed")
    if not isinstance(request_id, str) or not _HEX32.fullmatch(request_id):
        raise NativeGitOwnerError("child Git request ID is malformed")
    raw_request = message.get("request")
    request_keys = {"ancestor_sha", "commit_sha", "descendant_sha", "path", "verb"}
    if not isinstance(raw_request, dict) or set(raw_request) != request_keys:
        raise NativeGitOwnerError("child Git request arguments differ from the closed schema")
    if any(
        value is not None and not isinstance(value, str)
        for key, value in raw_request.items() if key != "verb"
    ) or not isinstance(raw_request.get("verb"), str):
        raise NativeGitOwnerError("child Git request argument types are malformed")
    request = _ClosedLocalGitRequest(
        verb=raw_request["verb"], commit_sha=raw_request["commit_sha"],
        path=raw_request["path"], ancestor_sha=raw_request["ancestor_sha"],
        descendant_sha=raw_request["descendant_sha"],
        _child_registration=registration,
    )
    owner._validate_request(registration.repository, request)
    return request, sequence, request_id


def _decode_fixture_request(
    owner: _NativeGitOperationOwner,
    registration: _RegisteredGitChild,
    fixture_id: str,
    raw: dict[str, Any],
) -> tuple[_FixtureRepositoryCapability, ClosedFixtureGitRequest]:
    expected = {
        "ancestor_sha", "commit_sha", "content_b64", "depth", "destination_id",
        "descendant_sha", "key",
        "message", "parent_sha", "path", "paths", "ref", "refspec",
        "source_id", "value", "verb", "revision", "object_type",
    }
    if set(raw) != expected:
        raise NativeGitOwnerError("fixture request fields differ from its exact schema")
    capability = registration.fixture_capabilities.get(fixture_id)
    if capability is None:
        raise NativeGitOwnerError("fixture request names an unissued disposable repository")
    try:
        verb = FixtureGitVerb(raw["verb"])
    except (TypeError, ValueError) as exc:
        raise NativeGitOwnerError("fixture verb is outside the fixed operation set") from exc

    def resolve(identifier: Any, *, required_fixture: bool) -> Any:
        if identifier is None:
            return None
        if not isinstance(identifier, str) or not _HEX32.fullmatch(identifier):
            raise NativeGitOwnerError("fixture repository reference is malformed")
        if identifier == registration.repository._repository_id:
            if required_fixture:
                raise NativeGitOwnerError("mutation destination cannot be the Native source repository")
            return registration.repository
        result = registration.fixture_capabilities.get(identifier)
        if result is None:
            raise NativeGitOwnerError("fixture request references an unknown repository capability")
        return result

    source = resolve(raw["source_id"], required_fixture=False)
    destination = resolve(raw["destination_id"], required_fixture=True)
    if raw["content_b64"] is None:
        content = None
    elif isinstance(raw["content_b64"], str) and len(raw["content_b64"]) <= 2 * 1024 * 1024:
        try:
            content = base64.b64decode(raw["content_b64"], validate=True)
            if base64.b64encode(content).decode("ascii") != raw["content_b64"]:
                raise ValueError("noncanonical base64")
        except (ValueError, base64.binascii.Error) as exc:
            raise NativeGitOwnerError("fixture request content encoding is invalid") from exc
    else:
        raise NativeGitOwnerError("fixture request content exceeds its fixed wire bound")
    for name in ("commit_sha", "parent_sha", "ancestor_sha", "descendant_sha"):
        value = raw[name]
        if value is not None and (
            not isinstance(value, str) or not _SHA1.fullmatch(value) or value == "0" * 40
        ):
            raise NativeGitOwnerError(f"fixture request {name} is malformed")
    if raw["object_type"] is not None and raw["object_type"] not in {"blob", "tree", "commit"}:
        raise NativeGitOwnerError("fixture hash-object type is outside the fixed Git object types")
    for name in ("ref", "refspec", "path", "message", "key", "value"):
        value = raw[name]
        if value is not None and (not isinstance(value, str) or "\0" in value):
            raise NativeGitOwnerError(f"fixture request {name} is malformed")
    depth = raw["depth"]
    if depth is not None and (type(depth) is not int or depth not in {1, 8, 64}):
        raise NativeGitOwnerError("fixture depth is outside the exact observed set")
    paths = raw["paths"]
    if (
        not isinstance(paths, list) or len(paths) > 256
        or any(not isinstance(path, str) for path in paths)
    ):
        raise NativeGitOwnerError("fixture path list is malformed or oversized")
    request = ClosedFixtureGitRequest(
        verb=verb, source=source, destination=destination,
        ref=raw["ref"], refspec=raw["refspec"], depth=depth,
        commit_sha=raw["commit_sha"], parent_sha=raw["parent_sha"],
        object_type=raw["object_type"],
        ancestor_sha=raw["ancestor_sha"], descendant_sha=raw["descendant_sha"],
        revision=raw["revision"],
        path=raw["path"], paths=tuple(paths), message=raw["message"],
        content=content, key=raw["key"], value=raw["value"],
    )
    _validate_fixture_request(owner, registration, capability, request)
    return capability, request


_FIXTURE_REF = re.compile(
    r"^(?:HEAD|[A-Za-z0-9][A-Za-z0-9._/-]*|refs/(?:heads|remotes/origin|ci-fixture)/[A-Za-z0-9._/-]+)$",
)
_FIXTURE_REFSPEC = re.compile(
    r"^(?:\+)?(?:HEAD|refs/heads/[A-Za-z0-9._/-]+|[a-f0-9]{40}):refs/(?:heads|remotes/origin|ci-fixture)/[A-Za-z0-9._/-]+$",
)
_FIXTURE_IDENTITY = {
    ("user.name", "Local Fixture"),
    ("user.name", "Fixture User"),
    ("user.email", "local-fixture@example.invalid"),
    ("user.email", "fixture@example.invalid"),
    ("gc.auto", "0"),
    ("commit.gpgsign", "false"),
    ("user.name", "Completeness fetch test"),
    ("user.email", "completeness-fetch@example.invalid"),
}


def _safe_path(value: str) -> str:
    if (
        not isinstance(value, str) or not value or value.startswith("/")
        or "\\" in value or "\0" in value or value.startswith(":")
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(char in value for char in "*?[]")
    ):
        raise ValueError("fixture path is not a canonical repository-relative path")
    return value


def _validate_fixture_request(
    owner: _NativeGitOperationOwner,
    registration: _RegisteredGitChild,
    current: _FixtureRepositoryCapability,
    request: ClosedFixtureGitRequest,
) -> None:
    if (
        type(current) is not _FixtureRepositoryCapability
        or type(request) is not ClosedFixtureGitRequest
        or type(request.verb) is not FixtureGitVerb
        or not _is_exact_fixture_test_command(registration.command)
    ):
        raise NativeGitOwnerError("fixture operation is not bound to the exact CHECK14 child")
    owner._validate_fixture_capability(registration, current)
    source, destination = request.source, request.destination
    if source is not None:
        if type(source) is _FixtureRepositoryCapability:
            owner._validate_fixture_capability(registration, source)
        elif (
            type(source) is not _LocalRepositoryCapability
            or source is not registration.repository
            or source._role is not _LocalRepositoryRole.PREPARED_RELEASE
            or source._owner_identity is not owner._identity
        ):
            raise NativeGitOwnerError("fixture source is not a registered fixture or prepared-release role")
    if destination is not None:
        if type(destination) is not _FixtureRepositoryCapability:
            raise NativeGitOwnerError("fixture destination is not a registered disposable repository")
        owner._validate_fixture_capability(registration, destination)
    if request.depth is not None and (type(request.depth) is not int or request.depth not in {1, 8, 64}):
        raise NativeGitOwnerError("fixture depth is outside the finite observed set")
    for value, label in ((request.ref, "ref"),):
        if value is not None and (not isinstance(value, str) or not _FIXTURE_REF.fullmatch(value) or ".." in value):
            raise NativeGitOwnerError(f"fixture {label} is outside its fixed ref grammar")
    if request.refspec is not None and (
        not isinstance(request.refspec, str)
        or not _FIXTURE_REFSPEC.fullmatch(request.refspec)
        or ".." in request.refspec
    ):
        raise NativeGitOwnerError("fixture refspec is outside its fixed local grammar")
    if request.path is not None:
        try:
            if _safe_path(request.path) != request.path:
                raise ValueError("noncanonical")
        except (TypeError, ValueError) as exc:
            raise NativeGitOwnerError("fixture path is not a safe relative path") from exc
    if (
        not isinstance(request.paths, tuple) or len(request.paths) > 256
        or any(not isinstance(path, str) or _safe_path(path) != path for path in request.paths)
        or len(set(request.paths)) != len(request.paths)
    ):
        raise NativeGitOwnerError("fixture path list is malformed")
    if request.message is not None and (
        not isinstance(request.message, str) or not request.message
        or len(request.message.encode("utf-8", "strict")) > 4096
        or "\0" in request.message
    ):
        raise NativeGitOwnerError("fixture commit message is malformed")
    if request.content is not None and (type(request.content) is not bytes or len(request.content) > 1024 * 1024):
        raise NativeGitOwnerError("fixture content exceeds its fixed 1 MiB input bound")
    if request.commit_sha is not None and (
        not isinstance(request.commit_sha, str) or not _SHA1.fullmatch(request.commit_sha)
        or request.commit_sha == "0" * 40
    ):
        raise NativeGitOwnerError("fixture commit/object identity is malformed")
    if request.revision is not None and (
        not isinstance(request.revision, str)
        or not re.fullmatch(
            r"[a-f0-9]{40}(?:\^\{commit\}|:[A-Za-z0-9._/-]+)?",
            request.revision,
        )
        or ".." in request.revision
        or request.revision.endswith("/")
    ):
        raise NativeGitOwnerError("fixture object expression is outside the exact SHA/path grammar")
    if request.parent_sha is not None and (
        not isinstance(request.parent_sha, str) or not _SHA1.fullmatch(request.parent_sha)
        or request.parent_sha == "0" * 40
    ):
        raise NativeGitOwnerError("fixture parent identity is malformed")
    if request.object_type is not None and request.object_type not in {"blob", "tree", "commit"}:
        raise NativeGitOwnerError("fixture object type is outside the fixed Git type set")
    if request.verb in {FixtureGitVerb.CONFIG_IDENTITY, FixtureGitVerb.CONFIG_FIXED}:
        if (request.key, request.value) not in _FIXTURE_IDENTITY:
            raise NativeGitOwnerError("fixture identity setting is outside the fixed values")
    else:
        if request.key is not None or request.value is not None:
            raise NativeGitOwnerError("fixture request carries an unexpected config field")

    fields_by_verb: dict[FixtureGitVerb, frozenset[str]] = {
        FixtureGitVerb.INIT_WORKING: frozenset(),
        FixtureGitVerb.INIT_WORKING_MAIN: frozenset(),
        FixtureGitVerb.INIT_BARE: frozenset(),
        FixtureGitVerb.CONFIG_IDENTITY: frozenset({"key", "value"}),
        FixtureGitVerb.CONFIG_FIXED: frozenset({"key", "value"}),
        FixtureGitVerb.ADD: frozenset({"paths"}),
        FixtureGitVerb.COMMIT: frozenset({"message"}),
        FixtureGitVerb.HASH_OBJECT: frozenset({"content", "object_type"}),
        FixtureGitVerb.WRITE_TREE: frozenset(),
        FixtureGitVerb.COMMIT_TREE: frozenset({"commit_sha", "parent_sha", "message"}),
        FixtureGitVerb.UPDATE_REF: frozenset({"ref", "commit_sha"}),
        FixtureGitVerb.SYMBOLIC_HEAD: frozenset({"ref"}),
        FixtureGitVerb.READ_TREE: frozenset({"commit_sha"}),
        FixtureGitVerb.CHECKOUT: frozenset({"ref", "commit_sha"}),
        FixtureGitVerb.CHECKOUT_REF: frozenset({"ref"}),
        FixtureGitVerb.SWITCH_BRANCH: frozenset({"ref"}),
        FixtureGitVerb.SWITCH_ORPHAN: frozenset({"ref"}),
        FixtureGitVerb.DELETE_BRANCH: frozenset({"ref"}),
        FixtureGitVerb.DELETE_REF: frozenset({"ref"}),
        FixtureGitVerb.DELETE_SYMBOLIC_REF: frozenset({"ref"}),
        FixtureGitVerb.REMOTE_ADD: frozenset({"source"}),
        FixtureGitVerb.WORKTREE_ADD: frozenset({"source", "destination", "ref", "commit_sha"}),
        FixtureGitVerb.WORKTREE_REMOVE: frozenset({"source", "destination"}),
        FixtureGitVerb.CLONE_SHARED: frozenset({"source", "destination"}),
        FixtureGitVerb.CLONE_SHARED_CHECKOUT: frozenset({"source", "destination", "ref"}),
        FixtureGitVerb.CLONE_FILE_DEPTH1: frozenset({"source", "destination", "ref", "depth"}),
        FixtureGitVerb.FETCH: frozenset({"source", "destination", "refspec", "depth"}),
        FixtureGitVerb.FETCH_COMMIT: frozenset({"source", "destination", "commit_sha", "depth"}),
        FixtureGitVerb.FETCH_EXACT_REF_64: frozenset({"source", "destination", "commit_sha", "refspec"}),
        FixtureGitVerb.FETCH_DEEPEN: frozenset({"source", "destination", "refspec"}),
        FixtureGitVerb.PUSH: frozenset({"source", "destination", "refspec"}),
        FixtureGitVerb.SHOW_BLOB: frozenset({"commit_sha", "path"}),
        FixtureGitVerb.SHOW_BLOB_INFO: frozenset({"commit_sha", "path"}),
        FixtureGitVerb.SHOW_BLOB_PAYLOAD: frozenset({"commit_sha", "path"}),
        FixtureGitVerb.REV_PARSE_HEAD: frozenset(),
        FixtureGitVerb.REV_PARSE_TREE: frozenset({"commit_sha"}),
        FixtureGitVerb.TRUSTED_ORIGIN_MAIN: frozenset(),
        FixtureGitVerb.IS_ANCESTOR: frozenset({"ancestor_sha", "descendant_sha"}),
        FixtureGitVerb.IS_SHALLOW: frozenset(),
        FixtureGitVerb.GIT_PATH: frozenset({"path"}),
        FixtureGitVerb.COMMON_DIR: frozenset(),
        FixtureGitVerb.ALTERNATES: frozenset(),
        FixtureGitVerb.SHALLOW_BYTES: frozenset(),
        FixtureGitVerb.REV_LIST: frozenset(),
        FixtureGitVerb.CAT_FILE_EXISTS: frozenset({"revision"}),
        FixtureGitVerb.SHOW_REF: frozenset({"ref"}),
        FixtureGitVerb.SHOW_REFS: frozenset(),
        FixtureGitVerb.WRITE_ALTERNATES: frozenset({"source"}),
        FixtureGitVerb.WRITE_UNRELATED_ALTERNATES: frozenset({"source"}),
        FixtureGitVerb.WRITE_SHALLOW: frozenset({"source", "content"}),
        FixtureGitVerb.WRITE_SHALLOW_FROM_SOURCE: frozenset({"source"}),
    }
    allowed_fields = fields_by_verb.get(request.verb)
    if allowed_fields is None:
        raise NativeGitOwnerError("fixture verb has no exact field contract")
    present_fields: set[str] = set()
    for name in (
        "ref", "refspec", "depth", "commit_sha", "parent_sha",
        "ancestor_sha", "descendant_sha", "path", "message", "content",
        "key", "value", "revision", "object_type",
    ):
        value = getattr(request, name)
        if value is not None:
            present_fields.add(name)
    if request.paths:
        present_fields.add("paths")
    if source is not None:
        present_fields.add("source")
    if destination is not None:
        present_fields.add("destination")
    optional_fields = {
        FixtureGitVerb.CLONE_FILE_DEPTH1: frozenset({"depth"}),
        FixtureGitVerb.WORKTREE_ADD: frozenset({"ref", "commit_sha"}),
        FixtureGitVerb.CHECKOUT: frozenset({"ref", "commit_sha"}),
        FixtureGitVerb.WRITE_SHALLOW: frozenset({"source", "content"}),
        FixtureGitVerb.COMMIT_TREE: frozenset({"parent_sha"}),
    }.get(request.verb, frozenset())
    if not present_fields.issubset(allowed_fields) or not allowed_fields - optional_fields <= present_fields:
        raise NativeGitOwnerError("fixture request fields differ from the exact per-verb contract")
    if request.verb is FixtureGitVerb.CHECKOUT and (request.ref is None) == (request.commit_sha is None):
        raise NativeGitOwnerError("fixture checkout requires exactly one revision locator")
    if request.verb in {
        FixtureGitVerb.CHECKOUT_REF, FixtureGitVerb.SWITCH_BRANCH,
        FixtureGitVerb.SWITCH_ORPHAN, FixtureGitVerb.DELETE_BRANCH,
        FixtureGitVerb.DELETE_REF, FixtureGitVerb.DELETE_SYMBOLIC_REF,
    } and request.ref is None:
        raise NativeGitOwnerError("branch/ref operation requires one exact ref")
    if request.verb is FixtureGitVerb.IS_ANCESTOR and any(
        not isinstance(value, str) or not _SHA1.fullmatch(value) or value == "0" * 40
        for value in (request.ancestor_sha, request.descendant_sha)
    ):
        raise NativeGitOwnerError("fixture ancestry pair is malformed")
    if request.verb is FixtureGitVerb.HASH_OBJECT:
        if request.object_type is None or request.content is None:
            raise NativeGitOwnerError("hash-object requires an exact type and bounded bytes")
    elif request.object_type is not None:
        raise NativeGitOwnerError("non-hash fixture request carries an object type")

    cross = request.verb in {
        FixtureGitVerb.REMOTE_ADD, FixtureGitVerb.WORKTREE_ADD,
        FixtureGitVerb.WORKTREE_REMOVE, FixtureGitVerb.CLONE_SHARED,
        FixtureGitVerb.CLONE_SHARED_CHECKOUT,
        FixtureGitVerb.CLONE_FILE_DEPTH1, FixtureGitVerb.FETCH,
        FixtureGitVerb.FETCH_COMMIT,
        FixtureGitVerb.FETCH_EXACT_REF_64,
        FixtureGitVerb.FETCH_DEEPEN,
        FixtureGitVerb.PUSH,
        FixtureGitVerb.WRITE_ALTERNATES, FixtureGitVerb.WRITE_SHALLOW,
        FixtureGitVerb.WRITE_SHALLOW_FROM_SOURCE,
        FixtureGitVerb.WRITE_UNRELATED_ALTERNATES,
    }
    if not cross and (source is not None or destination is not None):
        raise NativeGitOwnerError("non-cross-repository fixture operation carries repository caps")
    if request.verb in {
        FixtureGitVerb.CLONE_SHARED, FixtureGitVerb.CLONE_SHARED_CHECKOUT,
        FixtureGitVerb.CLONE_FILE_DEPTH1,
    }:
        if destination is not current or source is None:
            raise NativeGitOwnerError("clone requires its exact source and destination")
        if request.verb in {
            FixtureGitVerb.CLONE_SHARED_CHECKOUT, FixtureGitVerb.CLONE_FILE_DEPTH1,
        } and request.ref is None:
            raise NativeGitOwnerError("checked-out clone requires its exact branch ref")
        if request.verb in {
            FixtureGitVerb.CLONE_SHARED, FixtureGitVerb.CLONE_SHARED_CHECKOUT,
        } and request.depth is not None:
            raise NativeGitOwnerError("shared clone does not accept a depth override")
        if request.verb is FixtureGitVerb.CLONE_FILE_DEPTH1 and request.depth not in {None, 1}:
            raise NativeGitOwnerError("file clone depth differs from the observed depth-1 fixture")
    elif request.verb is FixtureGitVerb.REMOTE_ADD:
        if source is None or destination is not None or current._kind is FixtureRepositoryKind.PATH:
            raise NativeGitOwnerError("remote registration requires one exact remote source")
    elif request.verb is FixtureGitVerb.FETCH:
        if destination is not current or source is None or request.refspec is None or request.depth not in {1, 8, 64}:
            raise NativeGitOwnerError("fetch requires exact registered remote, refspec, and observed depth")
        if current._relations.get("origin") is not source:
            raise NativeGitOwnerError("fetch remote is not the exact registered local source")
    elif request.verb is FixtureGitVerb.FETCH_COMMIT:
        if (
            destination is not current or source is None or request.commit_sha is None
            or request.depth not in {1, 8}
            or current._relations.get("origin") is not source
        ):
            raise NativeGitOwnerError("FETCH_HEAD request requires an exact source, SHA, depth, and destination")
        if type(source) is _FixtureRepositoryCapability:
            if request.commit_sha not in source._known_objects:
                raise NativeGitOwnerError("FETCH_HEAD commit was not previously observed in its fixture source")
        elif type(source) is _LocalRepositoryCapability:
            if request.commit_sha not in source._allowed_commits:
                raise NativeGitOwnerError("FETCH_HEAD commit is outside the Native source role")
    elif request.verb is FixtureGitVerb.FETCH_EXACT_REF_64:
        expected_refspec = f"{request.commit_sha}:refs/remotes/origin/main"
        if (
            destination is not current or source is None or request.commit_sha is None
            or request.refspec != expected_refspec
            or current._relations.get("origin") is not source
        ):
            raise NativeGitOwnerError("exact origin/main fetch differs from the fixed depth-64 contract")
        if type(source) is _FixtureRepositoryCapability:
            if request.commit_sha not in source._known_objects:
                raise NativeGitOwnerError("depth-64 origin/main commit was not observed in its fixture source")
        elif type(source) is _LocalRepositoryCapability:
            if request.commit_sha not in source._allowed_commits:
                raise NativeGitOwnerError("depth-64 origin/main commit is outside the Native source role")
    elif request.verb is FixtureGitVerb.FETCH_DEEPEN:
        if (
            destination is not current or source is None or request.refspec is None
            or current._relations.get("origin") is not source
        ):
            raise NativeGitOwnerError("deepen fetch requires the exact registered local source")
    elif request.verb is FixtureGitVerb.PUSH:
        if source is not current or destination is None or request.refspec is None:
            raise NativeGitOwnerError("push requires the exact source and disposable bare destination")
        if source._relations.get("origin") is not destination:
            raise NativeGitOwnerError("push remote is not the exact registered bare destination")
    elif request.verb in {FixtureGitVerb.WORKTREE_ADD, FixtureGitVerb.WORKTREE_REMOVE}:
        if source is not current or destination is None:
            raise NativeGitOwnerError("worktree operation requires exact source and destination capabilities")
        if destination._kind is not FixtureRepositoryKind.PATH:
            raise NativeGitOwnerError("linked worktree destination must be an absent path capability")
    if request.verb in {FixtureGitVerb.INIT_WORKING, FixtureGitVerb.INIT_WORKING_MAIN} and (
        current._kind not in {FixtureRepositoryKind.WORKING, FixtureRepositoryKind.DIAGNOSTIC_LAYOUT}
        and not (
            current._kind is FixtureRepositoryKind.PATH
            and current._diagnostic_role == "source"
            and current._diagnostic_layout is not None
        )
    ):
        raise NativeGitOwnerError("working init requires a working fixture capability")
    if request.verb is FixtureGitVerb.INIT_BARE and current._kind is not FixtureRepositoryKind.BARE:
        raise NativeGitOwnerError("bare init requires a bare fixture capability")
    if request.verb is FixtureGitVerb.SHOW_REF and request.ref is None:
        raise NativeGitOwnerError("show-ref requires one exact ref")
    if request.verb is FixtureGitVerb.CAT_FILE_EXISTS and request.revision is None:
        raise NativeGitOwnerError("cat-file existence check requires one exact object expression")
    if request.verb is FixtureGitVerb.WRITE_ALTERNATES:
        if current._kind is not FixtureRepositoryKind.BARE or source is current:
            raise NativeGitOwnerError("alternates metadata write requires a bare fixture and distinct source")
    if request.verb is FixtureGitVerb.WRITE_UNRELATED_ALTERNATES:
        if (
            current._kind is not FixtureRepositoryKind.PATH
            or current._root_identity is None
            or type(source) is not _FixtureRepositoryCapability
            or source is current
            or current._relations.get("origin") is not source
            or source._workspace != current._workspace
        ):
            raise NativeGitOwnerError(
                "mismatched-alternates write requires its exact successful local clone pair",
            )
    if request.verb is FixtureGitVerb.WRITE_SHALLOW:
        if (source is None) == (request.content is None):
            raise NativeGitOwnerError("shallow metadata write requires exactly one registered source or finite payload")
        if source is not None and type(source) is not _FixtureRepositoryCapability:
            raise NativeGitOwnerError("shallow copy source must be a registered disposable fixture")
        if request.content is not None:
            value = request.content
            valid_hash_line = (
                len(value) == 41 and value.endswith(b"\n")
                and _SHA1.fullmatch(value[:-1].decode("ascii", "ignore")) is not None
            )
            if value not in {b"", b"not-a-git-object-id\n"} and not valid_hash_line:
                raise NativeGitOwnerError("shallow test payload is outside the finite fixture byte set")
    if request.verb is FixtureGitVerb.WRITE_SHALLOW_FROM_SOURCE:
        if (
            current._kind is not FixtureRepositoryKind.BARE
            or type(source) is not _LocalRepositoryCapability
            or source is not registration.repository
            or source._role is not _LocalRepositoryRole.PREPARED_RELEASE
        ):
            raise NativeGitOwnerError("Source shallow copy requires the exact prepared Source and bare fixture")
    if request.verb in {
        FixtureGitVerb.WRITE_ALTERNATES, FixtureGitVerb.WRITE_SHALLOW,
        FixtureGitVerb.WRITE_UNRELATED_ALTERNATES,
    }:
        if current._kind is FixtureRepositoryKind.PATH and current._root_identity is None:
            raise NativeGitOwnerError("metadata write target path has not been created by an owner operation")


def _remember_fixture_objects(
    capability: _FixtureRepositoryCapability,
    request: ClosedFixtureGitRequest,
    stdout: bytes,
) -> None:
    if request.verb in {
        FixtureGitVerb.HASH_OBJECT, FixtureGitVerb.WRITE_TREE,
        FixtureGitVerb.COMMIT_TREE, FixtureGitVerb.REV_PARSE_TREE,
        FixtureGitVerb.TRUSTED_ORIGIN_MAIN, FixtureGitVerb.REV_PARSE_HEAD,
    }:
        try:
            value = stdout.decode("ascii", "strict").strip()
        except UnicodeDecodeError:
            return
        if _SHA1.fullmatch(value):
            capability._known_objects.add(value)
    elif request.verb is FixtureGitVerb.SHOW_BLOB_INFO and request.commit_sha and request.path:
        fields = stdout.rstrip(b"\n").split(b" ")
        if (
            len(fields) == 3 and _SHA1.fullmatch(fields[0].decode("ascii", "ignore"))
            and fields[1] == b"blob" and fields[2].isdigit()
        ):
            capability._verified_blob_facts[(request.commit_sha, request.path)] = (
                fields[0].decode("ascii"), int(fields[2]),
            )


def _refresh_fixture_path_identity(
    capability: _FixtureRepositoryCapability, *, should_exist: bool,
) -> None:
    if type(capability) is not _FixtureRepositoryCapability or capability._kind is not FixtureRepositoryKind.PATH:
        raise NativeGitOwnerError("only an exact absent-path capability can change fixture path identity")
    try:
        if should_exist is False:
            if capability._root.exists() or capability._root.is_symlink():
                raise NativeGitOwnerError("fixture worktree removal left its registered path behind")
            capability._root_identity = None
            return
        info = capability._root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or capability._root.resolve(strict=True) != capability._root
            or not _contained(capability._root, capability._workspace)
        ):
            raise NativeGitOwnerError("fixture Git operation created an unsealed path")
        capability._root_identity = _directory_identity(capability._root)
    except OSError as exc:
        raise NativeGitOwnerError("fixture Git operation did not leave its expected path state") from exc


def _result_packet_sequence(
    owner: _NativeGitOperationOwner,
    registration: _RegisteredGitChild,
    request: _ClosedLocalGitRequest,
    result: Any,
    sequence: int,
    request_id: str,
    request_sha256: str,
    *, fixture_id: str | None = None,
) -> tuple[bytes, ...]:
    stdout, stderr = result.stdout, result.stderr
    is_payload = (
        request.verb == owner._state._LocalGitVerb.SHOW_BLOB_PAYLOAD.value
        if type(request) is _ClosedLocalGitRequest
        else request.verb is FixtureGitVerb.SHOW_BLOB_PAYLOAD
    )
    is_source_graph = (
        type(request) is _ClosedLocalGitRequest
        and request.verb in {
            "source_validate_ref_graph", "source_ref_inventory",
        }
    )
    result_cap = (
        _MAX_CHILD_PAYLOAD_RESULT_BYTES if is_payload
        else _MAX_CHILD_SOURCE_GRAPH_BYTES + 64 * 1024
        if is_source_graph else _MAX_CHILD_METADATA_RESULT_BYTES
    )
    stdout_cap = (
        _MAX_CHILD_PAYLOAD_BYTES if is_payload
        else _MAX_CHILD_SOURCE_GRAPH_BYTES if is_source_graph else 2 * 1024 * 1024
    )
    metadata_bytes = getattr(result, "metadata_bytes", b"")
    metadata_kind = getattr(result, "metadata_kind", None)
    metadata_present = getattr(result, "metadata_present", False)
    if (
        type(metadata_bytes) is not bytes
        or len(metadata_bytes) > _MAX_CHILD_METADATA_FILE_BYTES
        or type(metadata_present) is not bool
        or metadata_kind is not None and not isinstance(metadata_kind, str)
        or metadata_kind is None and (metadata_present or metadata_bytes)
    ):
        raise NativeGitOwnerError("State returned malformed bounded metadata bytes")
    if (
        type(stdout) is not bytes or type(stderr) is not bytes
        or len(stdout) > stdout_cap
        or len(stderr) > 64 * 1024
        or len(stdout) + len(stderr) > result_cap
        or type(result.output_bytes_before) is not int
        or type(result.output_bytes_after) is not int
        or result.output_bytes_before < 0
        or result.output_bytes_after > _MAX_STATE_GIT_OUTPUT_BYTES
        or result.output_bytes_after - result.output_bytes_before != len(stdout) + len(stderr)
    ):
        raise NativeGitOwnerError("State returned an oversized or malformed child Git result")
    metadata = {
        "child_id": registration.child_id,
        "cleanup_confirmed": result.cleanup_confirmed,
        "commands_after": result.commands_after,
        "commands_before": result.commands_before,
        "execution_ordinal": result.execution_ordinal,
        "finished_monotonic_ns": int(result.finished_monotonic * 1_000_000_000),
        "kind": "result",
        "operation_id": owner._identity._operation_id,
        "output_bytes_after": result.output_bytes_after,
        "output_bytes_before": result.output_bytes_before,
        "process_group_id": result.process_group_id,
        "process_id": result.process_id,
        "fixture_write_bytes": result.fixture_write_bytes,
        "fixture_write_kind": result.fixture_write_kind,
        "diagnostic_clone_receipt_id": registration.pending_diagnostic_clone_receipt_id,
        "metadata_kind": metadata_kind,
        "metadata_present": metadata_present,
        "metadata_length": len(metadata_bytes),
        "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
        "repository_id": registration.repository._repository_id,
        "request_id": request_id,
        "request_sha256": request_sha256,
        "returncode": result.returncode,
        "sequence": sequence,
        "started_monotonic_ns": int(result.started_monotonic * 1_000_000_000),
        "stderr_length": len(stderr),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "stdout_length": len(stdout),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "version": _CHILD_PROTOCOL_VERSION,
        "argv_trace": list(result.argv_trace),
    }
    packets = [_canonical_json_bytes(metadata)]
    for stream_id, data in ((b"O", stdout), (b"E", stderr)):
        for offset in range(0, len(data), _CHILD_RESULT_CHUNK_BYTES):
            chunk = data[offset : offset + _CHILD_RESULT_CHUNK_BYTES]
            packets.append(b"D" + stream_id + struct.pack(">I", offset) + chunk)
    for offset in range(0, len(metadata_bytes), _CHILD_RESULT_CHUNK_BYTES):
        chunk = metadata_bytes[offset : offset + _CHILD_RESULT_CHUNK_BYTES]
        packets.append(b"D" + b"M" + struct.pack(">I", offset) + chunk)
    if any(len(packet) > _MAX_CHILD_FRAME_BYTES for packet in packets):
        raise NativeGitOwnerError("child Git result frame exceeds its fixed size cap")
    return tuple(packets)


def _send_packet_sequence(
    channel: socket.socket, packets: tuple[bytes, ...], *,
    deadline_monotonic: float,
) -> None:
    for packet in packets:
        _send_packet(channel, packet, deadline_monotonic=deadline_monotonic)


def _send_json_packet(
    channel: socket.socket, value: dict[str, Any], *,
    deadline_monotonic: float,
) -> None:
    payload = _canonical_json_bytes(value)
    if len(payload) > _MAX_CHILD_FRAME_BYTES:
        raise NativeGitOwnerError("child Git JSON frame exceeds its fixed size cap")
    _send_packet(channel, payload, deadline_monotonic=deadline_monotonic)


def _send_packet(
    channel: socket.socket, payload: bytes, *, deadline_monotonic: float,
) -> None:
    if type(payload) is not bytes or len(payload) > _MAX_CHILD_FRAME_BYTES:
        raise NativeGitOwnerError("child Git packet exceeds its fixed frame cap")
    frame = struct.pack(">I", len(payload)) + payload
    deadline = min(deadline_monotonic, time.monotonic() + 0.25)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NativeGitOwnerError("child Git channel write did not complete within its fixed wait")
        selector = selectors.DefaultSelector()
        try:
            selector.register(channel, selectors.EVENT_WRITE)
            if not selector.select(remaining):
                raise NativeGitOwnerError("child Git channel write timed out")
            try:
                written = channel.send(frame)
            except BlockingIOError:
                continue
            if written != len(frame):
                raise NativeGitOwnerError("child Git packet was not sent atomically")
            return
        except (OSError, ValueError) as exc:
            raise NativeGitOwnerError("child Git channel write failed") from exc
        finally:
            selector.close()


def _receive_packet(channel: socket.socket) -> tuple[bytes, tuple[int, int, int]]:
    credentials_size = struct.calcsize("3i")
    try:
        frame, ancillary, flags, _ = channel.recvmsg(
            _MAX_CHILD_FRAME_BYTES + 4,
            socket.CMSG_SPACE(credentials_size),
        )
    except OSError as exc:
        raise NativeGitOwnerError("child Git channel read failed") from exc
    if not frame or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        raise NativeGitOwnerError("child Git channel packet was empty or truncated")
    if len(frame) < 4:
        raise NativeGitOwnerError("child Git packet lacks its length prefix")
    declared = struct.unpack(">I", frame[:4])[0]
    payload = frame[4:]
    if declared != len(payload) or declared > _MAX_CHILD_FRAME_BYTES:
        raise NativeGitOwnerError("child Git packet length differs from its bounded frame")
    if any(
        level != socket.SOL_SOCKET or kind != socket.SCM_CREDENTIALS
        for level, kind, _data in ancillary
    ):
        raise NativeGitOwnerError("child Git packet carries unexpected ancillary data")
    creds = [
        struct.unpack("3i", data[:credentials_size])
        for level, kind, data in ancillary
        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS
        and len(data) == credentials_size
    ]
    if len(creds) != 1:
        raise NativeGitOwnerError("child Git packet lacks exactly one kernel peer credential")
    return payload, creds[0]


def _decode_json_packet(payload: bytes) -> dict[str, Any]:
    def reject_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON field")
            value[key] = item
        return value

    try:
        decoded = json.loads(
            payload.decode("utf-8"), object_pairs_hook=reject_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid constant")),
        )
        if (
            not isinstance(decoded, dict)
            or _canonical_json_bytes(decoded) != payload
        ):
            raise ValueError("noncanonical child JSON")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise NativeGitOwnerError("child Git packet is not canonical bounded JSON") from exc
    return decoded


def child_git_session_for_source(
    root: str | os.PathLike[str] | ChildSourceRepository,
) -> "_ChildGitSession | None":
    """Return one cached child session after asserting its authenticated root.

    ``None`` is allowed only for an unchanged ordinary process whose seven
    reserved markers are all absent. Any attempted or changed bootstrap is a
    terminal error, so callers cannot fall back to direct subprocess Git.
    """
    global _CHILD_SESSION_INITIALIZED, _CHILD_SESSION_MARKERS
    global _CHILD_SESSION_PID, _CHILD_SESSION_SINGLETON, _CHILD_SESSION_TERMINAL
    pid = os.getpid()
    current_module = sys.modules.get(_MODULE_NAME)
    if __name__ != _MODULE_NAME or current_module is not sys.modules.get(__name__):
        with _CHILD_SESSION_LOCK:
            _CHILD_SESSION_TERMINAL = True
        raise NativeGitOwnerError("Native child Git broker is not its canonical module singleton")
    if type(root) is ChildSourceRepository:
        if root._token is not _CHILD_SOURCE_CAP_TOKEN:
            raise NativeGitOwnerError("Source capability has a foreign issuance token")
        session = root._session
        with _CHILD_SESSION_LOCK:
            snapshot = tuple((name, os.environ.get(name)) for name in _CHILD_BOOTSTRAP_MARKERS)
            if (
                _CHILD_SESSION_TERMINAL is True
                or _CHILD_SESSION_SINGLETON is not session
                or _CHILD_SESSION_PID != pid
                or _CHILD_SESSION_MARKERS != snapshot
                or session._closed is not False
                or root._repository_id != session._repository_id
            ):
                _CHILD_SESSION_TERMINAL = True
                raise NativeGitOwnerError("Source capability session is stale, terminal, or foreign")
        try:
            session.assert_source_root(root.root)
        except BaseException:
            _mark_child_session_terminal(session)
            raise
        return session
    snapshot = tuple(
        (name, os.environ.get(name)) for name in _CHILD_BOOTSTRAP_MARKERS
    )
    has_any = any(value is not None for _name, value in snapshot)
    has_all = all(value is not None for _name, value in snapshot)
    stale_session: _ChildGitSession | None = None
    with _CHILD_SESSION_LOCK:
        if _CHILD_SESSION_TERMINAL:
            raise NativeGitOwnerError("Native child Git session is terminal")
        if pid != _CHILD_SESSION_IMPORT_PID:
            _CHILD_SESSION_TERMINAL = True
            stale_session = _CHILD_SESSION_SINGLETON
            error = "forked process cannot reuse a Native child Git session"
        elif not _CHILD_SESSION_INITIALIZED:
            _CHILD_SESSION_INITIALIZED = True
            _CHILD_SESSION_PID = pid
            _CHILD_SESSION_MARKERS = snapshot
            error = None
        elif pid != _CHILD_SESSION_PID or snapshot != _CHILD_SESSION_MARKERS:
            _CHILD_SESSION_TERMINAL = True
            stale_session = _CHILD_SESSION_SINGLETON
            error = "Native child Git bootstrap changed after first observation"
        else:
            error = None
        if error is not None:
            pass
        elif not has_any:
            return None
        elif not has_all or any(snapshot[index][1] == "" for index in range(8)) or (
            (snapshot[8][1] == "") != (snapshot[9][1] == "")
            or snapshot[8][1] and not _SHA256.fullmatch(snapshot[8][1])
            or snapshot[9][1] and not _SHA256.fullmatch(snapshot[9][1])
        ):
            _CHILD_SESSION_TERMINAL = True
            error = "Native child Git bootstrap is partial or empty"
        else:
            session = _CHILD_SESSION_SINGLETON
            if session is None:
                try:
                    session = _ChildGitSession.from_environment()
                except BaseException as exc:
                    _CHILD_SESSION_TERMINAL = True
                    if isinstance(exc, NativeGitOwnerError):
                        raise
                    raise NativeGitOwnerError("Native child Git bootstrap is invalid") from exc
                _CHILD_SESSION_SINGLETON = session
    if error is not None:
        if stale_session is not None:
            stale_session._terminate()
        raise NativeGitOwnerError(error)
    try:
        session._ensure_hello()
        session.assert_source_root(root)
        return session
    except BaseException as exc:
        with _CHILD_SESSION_LOCK:
            _CHILD_SESSION_TERMINAL = True
        session._terminate()
        if isinstance(exc, NativeGitOwnerError):
            raise
        raise NativeGitOwnerError("Native child Git source root could not be authenticated") from exc


def child_git_session_for_fixture(
    capability: ChildFixtureRepository,
) -> "_ChildGitSession":
    """Resolve only a fixture cap minted by the one cached authenticated child session."""
    global _CHILD_SESSION_TERMINAL
    if (
        __name__ != _MODULE_NAME
        or sys.modules.get(_MODULE_NAME) is not sys.modules.get(__name__)
        or type(capability) is not ChildFixtureRepository
        or capability._token is not _CHILD_FIXTURE_CAP_TOKEN
    ):
        raise NativeGitOwnerError("fixture accessor requires the canonical exact fixture capability")
    session = capability._session
    with _CHILD_SESSION_LOCK:
        snapshot = tuple((name, os.environ.get(name)) for name in _CHILD_BOOTSTRAP_MARKERS)
        if (
            _CHILD_SESSION_TERMINAL is True
            or _CHILD_SESSION_SINGLETON is not session
            or _CHILD_SESSION_PID != os.getpid()
            or _CHILD_SESSION_MARKERS != snapshot
            or session._closed is not False
            or session._fixture_capabilities.get(capability.repository_id) is not capability
        ):
            _CHILD_SESSION_TERMINAL = True
            raise NativeGitOwnerError("fixture capability session is stale, terminal, or foreign")
    try:
        session.assert_fixture_root(capability.root, capability)
    except BaseException:
        _mark_child_session_terminal(session)
        raise
    return session


def _child_git_session() -> "_ChildGitSession":
    """Rootless access is intentionally unavailable; use the source accessor."""
    raise NativeGitOwnerError("Native child Git access requires an authenticated source root")


def _mark_child_session_terminal(session: "_ChildGitSession") -> None:
    global _CHILD_SESSION_TERMINAL
    with _CHILD_SESSION_LOCK:
        if _CHILD_SESSION_SINGLETON is session:
            _CHILD_SESSION_TERMINAL = True
    session._terminate()


class _ChildGitSession:
    __slots__ = (
        "_channel", "_operation_id", "_child_id", "_nonce", "_repository_id",
        "_command_id", "_source_package_sha256", "_source_bundle_sha256",
        "_owner_pid", "_deadline_ns", "_sequence", "_hello", "_closed",
        "_io_lock", "_repository_role", "_repository_root",
        "_repository_root_identity", "_root_asserted",
        "_fixture_capabilities", "_diagnostic_layout_by_cap",
        "_source_runtime_modules", "_source_runtime_module_digests",
        "_source_runtime_binding", "_prepared_input_enabled",
    )

    def __init__(
        self, *, channel: socket.socket, operation_id: str, child_id: str,
        nonce: str, repository_id: str, owner_pid: int, deadline_ns: int,
        command_id: str, source_package_sha256: str | None,
        source_bundle_sha256: str | None,
    ) -> None:
        self._channel = channel
        self._operation_id = operation_id
        self._child_id = child_id
        self._nonce = nonce
        self._repository_id = repository_id
        self._command_id = command_id
        self._source_package_sha256 = source_package_sha256
        self._source_bundle_sha256 = source_bundle_sha256
        self._owner_pid = owner_pid
        self._deadline_ns = deadline_ns
        self._sequence = 1
        self._hello = False
        self._closed = False
        self._io_lock = threading.RLock()
        self._repository_role: str | None = None
        self._repository_root: pathlib.Path | None = None
        self._repository_root_identity: tuple[int, int, int, int] | None = None
        self._root_asserted = False
        self._fixture_capabilities: dict[str, ChildFixtureRepository] = {}
        self._diagnostic_layout_by_cap: dict[str, tuple[str, str]] = {}
        self._source_runtime_modules: MappingProxyType | None = None
        self._source_runtime_module_digests: MappingProxyType | None = None
        self._source_runtime_binding: tuple[str, str, str, int] | None = None
        self._prepared_input_enabled = False

    @classmethod
    def from_environment(cls) -> "_ChildGitSession":
        names = {
            "fd": "DATAPAN_NATIVE_GIT_FD",
            "operation_id": "DATAPAN_NATIVE_GIT_OPERATION_ID",
            "child_id": "DATAPAN_NATIVE_GIT_CHILD_ID",
            "nonce": "DATAPAN_NATIVE_GIT_NONCE",
            "repository_id": "DATAPAN_NATIVE_GIT_REPOSITORY_ID",
            "owner_pid": "DATAPAN_NATIVE_GIT_OWNER_PID",
            "deadline_ns": "DATAPAN_NATIVE_GIT_DEADLINE_NS",
            "command_id": "DATAPAN_NATIVE_GIT_COMMAND_ID",
            "source_package_sha256": "DATAPAN_NATIVE_SOURCE_PACKAGE_SHA256",
            "source_bundle_sha256": "DATAPAN_NATIVE_SOURCE_BUNDLE_SHA256",
        }
        values = {key: os.environ.get(name) for key, name in names.items()}
        required = (
            "fd", "operation_id", "child_id", "nonce", "repository_id",
            "owner_pid", "deadline_ns", "command_id",
        )
        if any(not isinstance(values[key], str) or not values[key] for key in required) or any(
            not isinstance(values[key], str)
            for key in ("source_package_sha256", "source_bundle_sha256")
        ):
            raise NativeGitOwnerError("Native child Git bootstrap environment is incomplete")
        try:
            fd = int(values["fd"])
            owner_pid = int(values["owner_pid"])
            deadline_ns = int(values["deadline_ns"])
        except (TypeError, ValueError) as exc:
            raise NativeGitOwnerError("Native child Git bootstrap numeric fields are malformed") from exc
        operation_id = values["operation_id"]
        child_id = values["child_id"]
        nonce = values["nonce"]
        repository_id = values["repository_id"]
        command_id = values["command_id"]
        source_package_sha256 = values["source_package_sha256"]
        source_bundle_sha256 = values["source_bundle_sha256"]
        if (
            fd < 3 or owner_pid < 1 or deadline_ns <= time.monotonic_ns()
            or any(not isinstance(item, str) or not _HEX32.fullmatch(item)
                   for item in (operation_id, child_id, nonce, repository_id))
            or not _SHA256.fullmatch(command_id)
            or (source_package_sha256 == "") != (source_bundle_sha256 == "")
            or source_package_sha256 and not _SHA256.fullmatch(source_package_sha256)
            or source_bundle_sha256 and not _SHA256.fullmatch(source_bundle_sha256)
            or os.getppid() != owner_pid
        ):
            raise NativeGitOwnerError("Native child Git bootstrap identity or deadline is invalid")
        try:
            channel = socket.socket(fileno=fd)
            if channel.family != socket.AF_UNIX or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET:
                raise NativeGitOwnerError("Native child Git descriptor is not the registered local socket")
            channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            channel.settimeout((deadline_ns - time.monotonic_ns()) / 1_000_000_000)
            peer = channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            peer_pid, peer_uid, peer_gid = struct.unpack("3i", peer)
            if peer_pid != owner_pid or peer_uid != os.geteuid() or peer_gid != os.getegid():
                raise NativeGitOwnerError("Native child Git socket peer is not its owner process")
        except OSError as exc:
            raise NativeGitOwnerError("Native child Git descriptor cannot be authenticated") from exc
        return cls(
            channel=channel, operation_id=operation_id, child_id=child_id,
            nonce=nonce, repository_id=repository_id, owner_pid=owner_pid,
            deadline_ns=deadline_ns, command_id=command_id,
            source_package_sha256=source_package_sha256 or None,
            source_bundle_sha256=source_bundle_sha256 or None,
        )

    def run_git(self, request: _ClosedLocalGitRequest) -> _ChildGitResult:
        with self._io_lock:
            try:
                return self._run_git_locked(request)
            except BaseException:
                _mark_child_session_terminal(self)
                raise

    def _run_git_locked(self, request: _ClosedLocalGitRequest) -> _ChildGitResult:
        if (
            self._closed is not False
            or self._root_asserted is not True
            or type(request) is not _ClosedLocalGitRequest
            or time.monotonic_ns() >= self._deadline_ns
        ):
            raise NativeGitOwnerError("Native child Git session is closed, unrooted, expired, or untyped")
        if self._hello is False:
            self._send_hello()
        request_id = secrets.token_hex(16)
        sequence = self._sequence
        envelope = {
            "child_id": self._child_id,
            "kind": "request",
            "operation_id": self._operation_id,
            "repository_id": self._repository_id,
            "request_id": request_id,
            "request": _request_dict(request),
            "sequence": sequence,
            "version": _CHILD_PROTOCOL_VERSION,
        }
        payload = _canonical_json_bytes(envelope)
        if len(payload) > _MAX_CHILD_FRAME_BYTES:
            raise NativeGitOwnerError("Native child Git request exceeds its fixed frame cap")
        _send_packet(self._channel, payload, deadline_monotonic=self._deadline_ns / 1_000_000_000)
        result = self._receive_result(request, sequence, request_id)
        ack = {
            "child_id": self._child_id,
            "kind": "ack",
            "operation_id": self._operation_id,
            "repository_id": self._repository_id,
            "request_id": request_id,
            "sequence": sequence,
            "version": _CHILD_PROTOCOL_VERSION,
        }
        _send_json_packet(
            self._channel, ack,
            deadline_monotonic=self._deadline_ns / 1_000_000_000,
        )
        payload, credentials = _receive_packet(self._channel)
        self._verify_owner_credentials(credentials)
        confirmed = _decode_json_packet(payload)
        expected_confirmation = {
            "child_id": self._child_id,
            "kind": "ack_confirm",
            "operation_id": self._operation_id,
            "repository_id": self._repository_id,
            "request_id": request_id,
            "sequence": sequence,
            "version": _CHILD_PROTOCOL_VERSION,
        }
        if confirmed != expected_confirmation or type(confirmed.get("version")) is not int or type(confirmed.get("sequence")) is not int:
            raise NativeGitOwnerError("Native child Git acknowledgment confirmation is malformed")
        self._sequence += 1
        return result

    def close(self) -> None:
        with self._io_lock:
            try:
                if self._closed is False:
                    if self._hello is False:
                        self._send_hello()
                    goodbye = {
                        "child_id": self._child_id,
                        "kind": "goodbye",
                        "operation_id": self._operation_id,
                        "repository_id": self._repository_id,
                        "version": _CHILD_PROTOCOL_VERSION,
                    }
                    _send_json_packet(
                        self._channel, goodbye,
                        deadline_monotonic=self._deadline_ns / 1_000_000_000,
                    )
                    payload, credentials = _receive_packet(self._channel)
                    self._verify_owner_credentials(credentials)
                    ack = _decode_json_packet(payload)
                    expected = {
                        "child_id": self._child_id, "kind": "goodbye_ack",
                        "operation_id": self._operation_id,
                        "repository_id": self._repository_id,
                        "version": _CHILD_PROTOCOL_VERSION,
                    }
                    if ack != expected or type(ack.get("version")) is not int:
                        raise NativeGitOwnerError("Native child Git GOODBYE acknowledgment is malformed")
                    self._closed = True
                    self._channel.close()
            except BaseException:
                _mark_child_session_terminal(self)
                raise
            _mark_child_session_terminal(self)

    def _ensure_hello(self) -> None:
        with self._io_lock:
            if self._closed:
                raise NativeGitOwnerError("Native child Git session is already closed")
            if self._hello is False:
                try:
                    self._send_hello()
                except BaseException:
                    _mark_child_session_terminal(self)
                    raise

    def assert_source_root(self, root: str | os.PathLike[str]) -> None:
        with self._io_lock:
            if (
                self._closed is not False or self._hello is not True
                or self._repository_root is None
                or self._repository_root_identity is None
            ):
                raise NativeGitOwnerError("Native source root has no authenticated HELLO binding")
            try:
                supplied = pathlib.Path(root)
                resolved = supplied.resolve(strict=True)
                identity = _directory_identity(resolved)
            except (OSError, TypeError, ValueError) as exc:
                raise NativeGitOwnerError("Native source root is unavailable") from exc
            if (
                resolved != self._repository_root
                or str(resolved) != str(root)
                or identity != self._repository_root_identity
            ):
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError(
                    "Native source root differs from its authenticated repository role",
                )
            self._root_asserted = True

    def bind_source_runtime_modules(
        self, modules: Mapping[str, Any], *, digests: Mapping[str, str],
    ) -> None:
        """Bind only the two canonical, Native-bootstrapped runtime modules.

        The caller is the fixed child entry, after this session has completed
        HELLO and authenticated its prepared Source root. Module bytes are
        compiled from the Native bootstrap bundle; this method does not accept
        paths, imports, or arbitrary module names.
        """
        with self._io_lock:
            allowed = {"canonical_source_git", "canonical_publication_git_owner"}
            if (
                self._closed is not False or self._hello is not True
                or self._root_asserted is not True
                or self._source_runtime_modules is not None
                or self._source_runtime_module_digests is not None
                or self._source_runtime_binding is not None
                or type(modules) is not dict or set(modules) != allowed
                or type(digests) is not dict or set(digests) != allowed
                or time.monotonic_ns() >= self._deadline_ns
            ):
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError(
                    "source-runtime binding is stale, repeated, or outside its fixed module set",
                )
            checked_modules: dict[str, Any] = {}
            checked_digests: dict[str, str] = {}
            for name in sorted(allowed):
                module = modules[name]
                digest = digests[name]
                if (
                    type(module) is not type(sys)
                    or getattr(module, "__name__", None) != name
                    or sys.modules.get(name) is not module
                    or not isinstance(digest, str)
                    or not _SHA256.fullmatch(digest)
                    or getattr(module, "__datapan_source_runtime_sha256__", None) != digest
                    or getattr(module, "__datapan_source_runtime_operation_id__", None)
                    != self._operation_id
                ):
                    _mark_child_session_terminal(self)
                    raise NativeGitOwnerError(
                        "source-runtime module is not its exact operation-bound singleton",
                    )
                checked_modules[name] = module
                checked_digests[name] = digest
            self._source_runtime_modules = MappingProxyType(checked_modules)
            self._source_runtime_module_digests = MappingProxyType(checked_digests)
            self._source_runtime_binding = (
                self._operation_id, self._child_id, self._repository_id, os.getpid(),
            )

    def load_module(self, name: str) -> Any:
        """Return one exact operation-bound runtime module, with no path fallback."""
        with self._io_lock:
            binding = self._source_runtime_binding
            modules = self._source_runtime_modules
            digests = self._source_runtime_module_digests
            if (
                self._closed is not False or self._root_asserted is not True
                or self._hello is not True
                or binding != (
                    self._operation_id, self._child_id, self._repository_id, os.getpid(),
                )
                or type(name) is not str
                or name not in {"canonical_source_git", "canonical_publication_git_owner"}
                or not isinstance(modules, MappingProxyType)
                or not isinstance(digests, MappingProxyType)
                or time.monotonic_ns() >= self._deadline_ns
            ):
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError(
                    "source-runtime module request is stale, unbound, or outside its closed set",
                )
            module = modules.get(name)
            digest = digests.get(name)
            if (
                module is None or sys.modules.get(name) is not module
                or getattr(module, "__datapan_source_runtime_sha256__", None) != digest
                or getattr(module, "__datapan_source_runtime_operation_id__", None)
                != self._operation_id
            ):
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError(
                    "source-runtime singleton changed after its Native bootstrap",
                )
            return module

    def new_fixture_repository(
        self, kind: FixtureRepositoryKind | str,
    ) -> ChildFixtureRepository:
        """Ask the owner to allocate one root beneath this exact CHECK14 child."""
        with self._io_lock:
            try:
                exact_kind = kind if type(kind) is FixtureRepositoryKind else FixtureRepositoryKind(kind)
            except (TypeError, ValueError) as exc:
                raise NativeGitOwnerError("fixture repository kind is outside the closed set") from exc
            if self._closed is not False or self._root_asserted is not True:
                raise NativeGitOwnerError("fixture allocation requires the active rooted child session")
            if self._hello is False:
                self._send_hello()
            request_id = secrets.token_hex(16)
            sequence = self._sequence
            request = {"kind": exact_kind.value}
            envelope = {
                "child_id": self._child_id, "kind": "fixture_allocate",
                "operation_id": self._operation_id,
                "repository_id": self._repository_id,
                "request_id": request_id, "request": request,
                "sequence": sequence, "version": _CHILD_PROTOCOL_VERSION,
            }
            _send_json_packet(
                self._channel, envelope,
                deadline_monotonic=self._deadline_ns / 1_000_000_000,
            )
            payload, credentials = _receive_packet(self._channel)
            self._verify_owner_credentials(credentials)
            response = _decode_json_packet(payload)
            expected_keys = {
                "child_id", "fixture_id", "fixture_kind", "kind", "operation_id",
                "request_id", "repository_id", "root", "root_identity",
                "sequence", "version", "workspace", "workspace_identity",
            }
            if (
                set(response) != expected_keys
                or response.get("kind") != "fixture_cap"
                or response.get("version") != _CHILD_PROTOCOL_VERSION
                or response.get("child_id") != self._child_id
                or response.get("operation_id") != self._operation_id
                or response.get("repository_id") != self._repository_id
                or response.get("request_id") != request_id
                or response.get("sequence") != sequence
                or response.get("fixture_kind") != exact_kind.value
                or not isinstance(response.get("fixture_id"), str)
                or not _HEX32.fullmatch(response["fixture_id"])
                or not isinstance(response.get("root"), str)
                or not isinstance(response.get("workspace"), str)
                or not isinstance(response.get("workspace_identity"), list)
                or len(response["workspace_identity"]) != 4
                or any(type(value) is not int or value < 0 for value in response["workspace_identity"])
                or response.get("root_identity") is not None
                and (
                    not isinstance(response.get("root_identity"), list)
                    or len(response["root_identity"]) != 4
                    or any(type(value) is not int or value < 0 for value in response["root_identity"])
                )
            ):
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError("fixture allocation response does not match its exact request")
            try:
                root = pathlib.Path(response["root"])
                workspace = pathlib.Path(response["workspace"])
                if root.resolve(strict=False) != root or workspace.resolve(strict=True) != workspace:
                    raise ValueError("fixture paths are not canonical")
                workspace_identity = _directory_identity(workspace)
                root_identity = (
                    None if response["root_identity"] is None
                    else tuple(response["root_identity"])
                )
                if tuple(response["workspace_identity"]) != workspace_identity:
                    raise ValueError("fixture workspace identity changed")
                if root_identity is not None and _directory_identity(root) != root_identity:
                    raise ValueError("fixture root identity changed")
                if not _contained(root, workspace):
                    raise ValueError("fixture root escaped its workspace")
            except (OSError, ValueError) as exc:
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError("fixture allocation path identity is invalid") from exc
            cap = ChildFixtureRepository(
                session=self, repository_id=response["fixture_id"], root=root,
                kind=exact_kind, root_identity=root_identity,
                workspace=workspace, workspace_identity=workspace_identity,
                _token=_CHILD_FIXTURE_CAP_TOKEN,
            )
            if cap.repository_id in self._fixture_capabilities:
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError("fixture allocation duplicated a capability ID")
            self._fixture_capabilities[cap.repository_id] = cap
            self._send_child_ack(request_id, sequence)
            self._sequence += 1
            return cap

    def new_diagnostic_layout(self) -> ChildFixtureRepository:
        return self.new_fixture_repository(FixtureRepositoryKind.DIAGNOSTIC_LAYOUT)

    def new_diagnostic_repository(
        self, layout: ChildFixtureRepository, role: str,
    ) -> ChildFixtureRepository:
        """Return the fixed long-path source or an owner-created disposable destination."""
        with self._io_lock:
            if (
                type(layout) is not ChildFixtureRepository
                or layout._token is not _CHILD_FIXTURE_CAP_TOKEN
                or layout._session is not self
                or self._fixture_capabilities.get(layout.repository_id) is not layout
                or layout.kind is not FixtureRepositoryKind.DIAGNOSTIC_LAYOUT
                or self._closed is not False
            ):
                raise NativeGitOwnerError("diagnostic repository requires this session's exact layout cap")
            self.assert_fixture_root(layout.root, layout)
            if role not in {"source", "disposable"}:
                raise NativeGitOwnerError("diagnostic repository role is outside the fixed pair")
            if self._hello is False:
                self._send_hello()
            request_id = secrets.token_hex(16)
            sequence = self._sequence
            envelope = {
                "child_id": self._child_id,
                "kind": "diagnostic_allocate",
                "layout_id": layout.repository_id,
                "operation_id": self._operation_id,
                "repository_id": self._repository_id,
                "request_id": request_id,
                "role": role,
                "sequence": sequence,
                "version": _CHILD_PROTOCOL_VERSION,
            }
            _send_json_packet(
                self._channel, envelope,
                deadline_monotonic=self._deadline_ns / 1_000_000_000,
            )
            payload, credentials = _receive_packet(self._channel)
            self._verify_owner_credentials(credentials)
            response = _decode_json_packet(payload)
            expected_keys = {
                "child_id", "fixture_id", "fixture_kind", "kind", "operation_id",
                "request_id", "repository_id", "root", "root_identity",
                "sequence", "version", "workspace", "workspace_identity",
            }
            if (
                set(response) != expected_keys
                or response.get("kind") != "fixture_cap"
                or response.get("version") != _CHILD_PROTOCOL_VERSION
                or response.get("child_id") != self._child_id
                or response.get("operation_id") != self._operation_id
                or response.get("repository_id") != self._repository_id
                or response.get("request_id") != request_id
                or response.get("sequence") != sequence
                or response.get("fixture_kind") != FixtureRepositoryKind.PATH.value
                or not isinstance(response.get("fixture_id"), str)
                or not _HEX32.fullmatch(response["fixture_id"])
                or not isinstance(response.get("root"), str)
                or not isinstance(response.get("workspace"), str)
                or not isinstance(response.get("workspace_identity"), list)
                or len(response["workspace_identity"]) != 4
                or any(type(value) is not int or value < 0 for value in response["workspace_identity"])
                or role == "source" and (
                    not isinstance(response.get("root_identity"), list)
                    or len(response["root_identity"]) != 4
                    or any(type(value) is not int or value < 0 for value in response["root_identity"])
                )
                or role == "disposable" and response.get("root_identity") is not None
            ):
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError("diagnostic role capability differs from its exact request")
            try:
                root = pathlib.Path(response["root"])
                workspace = pathlib.Path(response["workspace"])
                workspace_identity = _directory_identity(workspace)
                root_identity = (
                    None if response["root_identity"] is None
                    else tuple(response["root_identity"])
                )
                if (
                    root.resolve(strict=False) != root
                    or workspace != layout._workspace
                    or tuple(response["workspace_identity"]) != layout._workspace_identity
                    or workspace_identity != layout._workspace_identity
                    or root != layout.root / role
                    or root.parent != layout.root
                    or not _contained(root, layout.root)
                    or role == "source" and (
                        root_identity is None or _directory_identity(root) != root_identity
                    )
                    or role == "disposable" and (root.exists() or root.is_symlink())
                ):
                    raise ValueError("diagnostic role path escaped its exact layout")
            except (OSError, ValueError) as exc:
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError("diagnostic role path identity is invalid") from exc
            cap = ChildFixtureRepository(
                session=self, repository_id=response["fixture_id"], root=root,
                kind=FixtureRepositoryKind.PATH, root_identity=root_identity,
                workspace=workspace, workspace_identity=workspace_identity,
                _token=_CHILD_FIXTURE_CAP_TOKEN,
            )
            if cap.repository_id in self._fixture_capabilities:
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError("diagnostic role capability ID was duplicated")
            self._fixture_capabilities[cap.repository_id] = cap
            self._diagnostic_layout_by_cap[cap.repository_id] = (layout.repository_id, role)
            self._send_child_ack(request_id, sequence)
            self._sequence += 1
            return cap

    def remove_diagnostic_cloned_commit(
        self, layout: ChildFixtureRepository, receipt: ChildDiagnosticCloneReceipt,
    ) -> int:
        """Consume one successful shared-clone receipt to remove its source loose commit."""
        with self._io_lock:
            if (
                self._closed is not False or self._root_asserted is not True
                or type(layout) is not ChildFixtureRepository
                or layout._token is not _CHILD_FIXTURE_CAP_TOKEN
                or layout._session is not self
                or self._fixture_capabilities.get(layout.repository_id) is not layout
                or layout.kind is not FixtureRepositoryKind.DIAGNOSTIC_LAYOUT
                or type(receipt) is not ChildDiagnosticCloneReceipt
                or receipt._token is not _DIAGNOSTIC_CLONE_TOKEN
                or receipt._session is not self
                or receipt._used is not False
                or receipt._layout_id != layout.repository_id
                or receipt._destination_id not in self._fixture_capabilities
                or self._diagnostic_layout_by_cap.get(receipt._source_id)
                != (layout.repository_id, "source")
                or self._diagnostic_layout_by_cap.get(receipt._destination_id)
                != (layout.repository_id, "disposable")
                or time.monotonic_ns() >= self._deadline_ns
            ):
                raise NativeGitOwnerError("diagnostic commit removal lacks its exact unused clone receipt")
            destination = self._fixture_capabilities[receipt._destination_id]
            if type(destination) is not ChildFixtureRepository:
                raise NativeGitOwnerError("diagnostic clone receipt destination is not a fixture repository")
            self.assert_fixture_root(layout.root, layout)
            self.assert_fixture_root(destination.root, destination)
            receipt._used = True
            if self._hello is False:
                self._send_hello()
            request_id = secrets.token_hex(16)
            sequence = self._sequence
            envelope = {
                "child_id": self._child_id,
                "kind": "diagnostic_remove",
                "layout_id": layout.repository_id,
                "operation_id": self._operation_id,
                "receipt_id": receipt._receipt_id,
                "repository_id": self._repository_id,
                "request_id": request_id,
                "sequence": sequence,
                "version": _CHILD_PROTOCOL_VERSION,
            }
            _send_json_packet(
                self._channel, envelope,
                deadline_monotonic=self._deadline_ns / 1_000_000_000,
            )
            payload, credentials = _receive_packet(self._channel)
            self._verify_owner_credentials(credentials)
            result = _decode_json_packet(payload)
            expected = {
                "child_id": self._child_id,
                "kind": "diagnostic_remove_result",
                "layout_id": layout.repository_id,
                "operation_id": self._operation_id,
                "receipt_id": receipt._receipt_id,
                "repository_id": self._repository_id,
                "request_id": request_id,
                "sequence": sequence,
                "version": _CHILD_PROTOCOL_VERSION,
            }
            if (
                set(result) != set(expected) | {"removed_bytes", "source_id", "destination_id"}
                or any(result.get(key) != value for key, value in expected.items())
                or result.get("source_id") != receipt._source_id
                or result.get("destination_id") != receipt._destination_id
                or type(result.get("removed_bytes")) is not int
                or not 1 <= result["removed_bytes"] <= 2 * 1024 * 1024
            ):
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError("diagnostic removal result does not bind its one-use clone receipt")
            self._send_child_ack(request_id, sequence)
            self._sequence += 1
            return result["removed_bytes"]

    def assert_fixture_root(
        self, root: str | os.PathLike[str], capability: ChildFixtureRepository,
    ) -> None:
        with self._io_lock:
            if (
                type(capability) is not ChildFixtureRepository
                or capability._token is not _CHILD_FIXTURE_CAP_TOKEN
                or capability._session is not self
                or self._fixture_capabilities.get(capability.repository_id) is not capability
                or self._closed is not False
            ):
                raise NativeGitOwnerError("fixture root assertion requires this session's exact capability")
            try:
                supplied = pathlib.Path(root)
                if supplied != capability.root or supplied.resolve(strict=False) != supplied:
                    raise ValueError("fixture path differs from issued capability")
                if capability.root_identity is not None:
                    if _directory_identity(supplied) != capability.root_identity:
                        raise ValueError("fixture root identity changed")
                elif supplied.exists():
                    raise ValueError("issued absent fixture path appeared without an owner operation")
                if (
                    capability._workspace.resolve(strict=True) != capability._workspace
                    or _directory_identity(capability._workspace) != capability._workspace_identity
                    or not _contained(supplied, capability._workspace)
                ):
                    raise ValueError("fixture path is invalid")
            except (OSError, TypeError, ValueError) as exc:
                _mark_child_session_terminal(self)
                raise NativeGitOwnerError("fixture root differs from its issued identity") from exc

    def source_repository(self) -> ChildSourceRepository:
        """Return only the exact read-only prepared-release root from HELLO."""
        with self._io_lock:
            if (
                self._closed is not False or self._hello is not True
                or self._root_asserted is not True
                or self._repository_role != _LocalRepositoryRole.PREPARED_RELEASE.value
                or self._repository_root is None
                or self._repository_root_identity is None
                or _directory_identity(self._repository_root) != self._repository_root_identity
            ):
                raise NativeGitOwnerError("source capability requires the exact prepared-release HELLO role")
            return ChildSourceRepository(
                session=self, repository_id=self._repository_id,
                root=self._repository_root,
                root_identity=self._repository_root_identity,
                _token=_CHILD_SOURCE_CAP_TOKEN,
            )

    def prepared_input_reader(self) -> _ChildPreparedInputReader:
        """Return the exact State-served prepared input view for this child epoch."""
        with self._io_lock:
            if (
                self._closed is not False or self._hello is not True
                or self._root_asserted is not True
                or self._repository_role != _LocalRepositoryRole.PREPARED_RELEASE.value
                or self._prepared_input_enabled is not True
                or time.monotonic_ns() >= self._deadline_ns
            ):
                raise NativeGitOwnerError("prepared-input capability is absent or stale")
            return _ChildPreparedInputReader(
                self, _token=_CHILD_PREPARED_INPUT_READER_TOKEN,
            )

    def _request_prepared_input(
        self, *, operation: str, path: str | None = None,
        prefix: str | None = None, suffix: str | None = None,
    ) -> bytes | tuple[str, ...] | None:
        with self._io_lock:
            if (
                self._closed is not False or self._hello is not True
                or self._root_asserted is not True
                or self._prepared_input_enabled is not True
                or self._repository_role != _LocalRepositoryRole.PREPARED_RELEASE.value
                or time.monotonic_ns() >= self._deadline_ns
            ):
                raise NativeGitOwnerError("prepared-input request is stale, unrooted, or unavailable")
            if operation == "read_bytes":
                if (
                    type(path) is not str or _safe_path(path) != path
                    or prefix is not None or suffix is not None
                ):
                    raise NativeGitOwnerError("prepared-input request path is malformed")
            elif operation == "list_paths":
                if path is not None or prefix != "sources/" or suffix != ".json":
                    raise NativeGitOwnerError("prepared-input inventory selector is malformed")
            else:
                raise NativeGitOwnerError("prepared-input operation is outside its closed set")
            sequence = self._sequence
            request_id = secrets.token_hex(16)
            message = {
                "child_id": self._child_id,
                "kind": "prepared_input_request",
                "operation": operation,
                "operation_id": self._operation_id,
                "path": path,
                "prefix": prefix,
                "repository_id": self._repository_id,
                "request_id": request_id,
                "sequence": sequence,
                "suffix": suffix,
                "version": _CHILD_PROTOCOL_VERSION,
            }
            request_sha = hashlib.sha256(_canonical_json_bytes(message)).hexdigest()
            try:
                _send_json_packet(
                    self._channel, message,
                    deadline_monotonic=self._deadline_ns / 1_000_000_000,
                )
                payload, credentials = _receive_packet(self._channel)
                self._verify_owner_credentials(credentials)
                metadata = _decode_json_packet(payload)
                expected_keys = {
                    "child_id", "kind", "operation", "operation_id", "payload_kind",
                    "payload_length", "payload_sha256", "path", "present",
                    "repository_id", "request_id", "request_sha256", "sequence", "version",
                }
                maximum = (
                    _MAX_CHILD_INPUT_BYTES if operation == "read_bytes"
                    else _MAX_CHILD_INPUT_LIST_BYTES
                )
                length = _bounded_wire_int(metadata.get("payload_length"), maximum)
                digest = metadata.get("payload_sha256")
                present = metadata.get("present")
                expected_payload_kind = "bytes" if operation == "read_bytes" else "paths"
                if (
                    set(metadata) != expected_keys
                    or metadata.get("kind") != "prepared_input_result"
                    or metadata.get("version") != _CHILD_PROTOCOL_VERSION
                    or metadata.get("operation_id") != self._operation_id
                    or metadata.get("child_id") != self._child_id
                    or metadata.get("repository_id") != self._repository_id
                    or metadata.get("request_id") != request_id
                    or metadata.get("request_sha256") != request_sha
                    or metadata.get("sequence") != sequence
                    or metadata.get("operation") != operation
                    or metadata.get("path") != path
                    or metadata.get("payload_kind") != expected_payload_kind
                    or type(present) is not bool
                    or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
                    or operation == "list_paths" and present is not True
                    or operation == "read_bytes" and not present and length != 0
                ):
                    raise NativeGitOwnerError("prepared-input result does not bind its exact request")
                output = self._receive_stream(b"I", length)
                if hashlib.sha256(output).hexdigest() != digest:
                    raise NativeGitOwnerError("prepared-input result digest is invalid")
                if operation == "list_paths":
                    values = _decode_json_packet(output)
                    if (
                        type(values) is not list or len(values) > 4096
                        or any(
                            type(item) is not str or not item.startswith("sources/")
                            or not item.endswith(".json") or _safe_path(item) != item
                            for item in values
                        )
                        or values != sorted(set(values), key=lambda item: item.encode("utf-8"))
                    ):
                        raise NativeGitOwnerError("prepared-input inventory payload is malformed")
                    result: bytes | tuple[str, ...] | None = tuple(values)
                elif present:
                    result = output
                else:
                    result = None
                ack = {
                    "child_id": self._child_id, "kind": "ack",
                    "operation_id": self._operation_id,
                    "repository_id": self._repository_id,
                    "request_id": request_id, "sequence": sequence,
                    "version": _CHILD_PROTOCOL_VERSION,
                }
                _send_json_packet(
                    self._channel, ack,
                    deadline_monotonic=self._deadline_ns / 1_000_000_000,
                )
                payload, credentials = _receive_packet(self._channel)
                self._verify_owner_credentials(credentials)
                confirmed = _decode_json_packet(payload)
                if confirmed != {
                    "child_id": self._child_id, "kind": "ack_confirm",
                    "operation_id": self._operation_id,
                    "repository_id": self._repository_id,
                    "request_id": request_id, "sequence": sequence,
                    "version": _CHILD_PROTOCOL_VERSION,
                }:
                    raise NativeGitOwnerError("prepared-input acknowledgment is malformed")
                self._sequence += 1
                return result
            except BaseException:
                _mark_child_session_terminal(self)
                raise

    def run_source_git(
        self, capability: ChildSourceRepository,
        request: _ClosedLocalGitRequest,
    ) -> _ChildGitResult:
        """Run only the fixed CHECK14 metadata reads against the HELLO root."""
        allowed = {
            "source_is_shallow", "source_git_dir", "source_common_dir",
            "source_shallow_path", "source_shallow_bytes",
            "source_validate_ref_graph", "source_ref_inventory",
        }
        with self._io_lock:
            if (
                type(capability) is not ChildSourceRepository
                or capability._token is not _CHILD_SOURCE_CAP_TOKEN
                or capability._session is not self
                or capability._repository_id != self._repository_id
                or type(request) is not _ClosedLocalGitRequest
                or request.verb not in allowed
                or self._closed is not False
                or self._root_asserted is not True
            ):
                raise NativeGitOwnerError("Source metadata request lacks its exact child role or closed verb")
            self.assert_source_root(capability.root)
        return self.run_git(request)

    def run_fixture(
        self, capability: ChildFixtureRepository,
        request: ClosedFixtureGitRequest,
    ) -> _ChildGitResult:
        with self._io_lock:
            if (
                self._closed is not False or self._root_asserted is not True
                or type(capability) is not ChildFixtureRepository
                or capability._token is not _CHILD_FIXTURE_CAP_TOKEN
                or capability._session is not self
                or self._fixture_capabilities.get(capability.repository_id) is not capability
                or type(request) is not ClosedFixtureGitRequest
                or type(request.verb) is not FixtureGitVerb
                or time.monotonic_ns() >= self._deadline_ns
            ):
                raise NativeGitOwnerError("fixture Git session, capability, request, or deadline is invalid")
            if request.source is not None and (
                type(request.source) not in {ChildFixtureRepository, ChildSourceRepository}
                or request.source._session is not self
                or request.source._token not in {
                    _CHILD_FIXTURE_CAP_TOKEN, _CHILD_SOURCE_CAP_TOKEN,
                }
            ):
                raise NativeGitOwnerError("fixture source is not a capability from this child session")
            if request.destination is not None and (
                type(request.destination) is not ChildFixtureRepository
                or request.destination._session is not self
                or request.destination._token is not _CHILD_FIXTURE_CAP_TOKEN
            ):
                raise NativeGitOwnerError("fixture destination is not a disposable child capability")
            self.assert_fixture_root(capability.root, capability)
            if self._hello is False:
                self._send_hello()
            request_id = secrets.token_hex(16)
            sequence = self._sequence
            wire_request = _fixture_request_dict(request)
            envelope = {
                "child_id": self._child_id, "kind": "fixture_request",
                "operation_id": self._operation_id,
                "repository_id": self._repository_id,
                "request_id": request_id,
                "fixture_id": capability.repository_id,
                "request": wire_request,
                "sequence": sequence, "version": _CHILD_PROTOCOL_VERSION,
            }
            _send_json_packet(
                self._channel, envelope,
                deadline_monotonic=self._deadline_ns / 1_000_000_000,
            )
            result = self._receive_result(
                request, sequence, request_id, fixture_id=capability.repository_id,
            )
            if request.verb in {
                FixtureGitVerb.CLONE_SHARED, FixtureGitVerb.CLONE_SHARED_CHECKOUT,
                FixtureGitVerb.CLONE_FILE_DEPTH1,
                FixtureGitVerb.WORKTREE_ADD,
            } and request.destination is not None:
                diagnostic = self._diagnostic_layout_by_cap.get(
                    request.destination.repository_id,
                )
                diagnostic_layout = (
                    None if diagnostic is None
                    else self._fixture_capabilities.get(diagnostic[0])
                )
                _refresh_child_fixture_path_identity(
                    request.destination, should_exist=True,
                    diagnostic_layout=diagnostic_layout,
                )
            elif request.verb is FixtureGitVerb.WORKTREE_REMOVE and request.destination is not None:
                _refresh_child_fixture_path_identity(request.destination, should_exist=False)
            self._send_child_ack(request_id, sequence)
            self._sequence += 1
            return result

    def _send_child_ack(self, request_id: str, sequence: int) -> None:
        ack = {
            "child_id": self._child_id, "kind": "ack",
            "operation_id": self._operation_id,
            "repository_id": self._repository_id,
            "request_id": request_id, "sequence": sequence,
            "version": _CHILD_PROTOCOL_VERSION,
        }
        _send_json_packet(self._channel, ack, deadline_monotonic=self._deadline_ns / 1_000_000_000)
        payload, credentials = _receive_packet(self._channel)
        self._verify_owner_credentials(credentials)
        confirmed = _decode_json_packet(payload)
        expected = {
            "child_id": self._child_id, "kind": "ack_confirm",
            "operation_id": self._operation_id,
            "repository_id": self._repository_id,
            "request_id": request_id, "sequence": sequence,
            "version": _CHILD_PROTOCOL_VERSION,
        }
        if confirmed != expected:
            _mark_child_session_terminal(self)
            raise NativeGitOwnerError("fixture child request acknowledgment is malformed")

    def _terminate(self) -> None:
        with self._io_lock:
            if self._closed is False:
                self._closed = True
                try:
                    self._channel.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self._channel.close()
                except OSError:
                    pass

    def _send_hello(self) -> None:
        hello = {
            "child_id": self._child_id,
            "kind": "hello",
            "nonce": self._nonce,
            "operation_id": self._operation_id,
            "owner_pid": self._owner_pid,
            "repository_id": self._repository_id,
            "command_id": self._command_id,
            "source_package_sha256": self._source_package_sha256 or "",
            "source_bundle_sha256": self._source_bundle_sha256 or "",
            "version": _CHILD_PROTOCOL_VERSION,
        }
        _send_json_packet(
            self._channel, hello,
            deadline_monotonic=self._deadline_ns / 1_000_000_000,
        )
        payload, credentials = _receive_packet(self._channel)
        self._verify_owner_credentials(credentials)
        ack = _decode_json_packet(payload)
        expected = {
            "child_id": self._child_id, "kind": "hello_ack",
            "nonce": self._nonce,
            "operation_id": self._operation_id,
            "repository_id": self._repository_id,
            "command_id": self._command_id,
            "source_package_sha256": self._source_package_sha256 or "",
            "source_bundle_sha256": self._source_bundle_sha256 or "",
            "repository_role": None,
            "repository_root": None,
            "repository_root_identity": None,
            "prepared_input_enabled": None,
            "version": _CHILD_PROTOCOL_VERSION,
        }
        if set(ack) != set(expected) or any(
            ack.get(key) != value for key, value in expected.items()
            if key not in {
                "repository_role", "repository_root", "repository_root_identity",
                "prepared_input_enabled",
            }
        ):
            raise NativeGitOwnerError("Native child Git HELLO acknowledgment is malformed")
        role = ack.get("repository_role")
        root_text = ack.get("repository_root")
        root_identity = ack.get("repository_root_identity")
        prepared_input_enabled = ack.get("prepared_input_enabled")
        if (
            type(role) is not str
            or role not in {item.value for item in _LocalRepositoryRole}
            or type(root_text) is not str
            or not root_text.startswith("/")
            or type(root_identity) is not list
            or len(root_identity) != 4
            or any(type(item) is not int or item < 0 for item in root_identity)
            or type(prepared_input_enabled) is not bool
            or prepared_input_enabled is True and role != _LocalRepositoryRole.PREPARED_RELEASE.value
        ):
            raise NativeGitOwnerError("Native child Git HELLO root binding is malformed")
        try:
            repository_root = pathlib.Path(root_text)
            if repository_root.resolve(strict=True) != repository_root:
                raise ValueError("registered root is not canonical")
            exact_identity = _directory_identity(repository_root)
        except (OSError, ValueError) as exc:
            raise NativeGitOwnerError("Native child Git HELLO root is unavailable") from exc
        if tuple(root_identity) != exact_identity:
            raise NativeGitOwnerError("Native child Git HELLO root identity changed")
        self._repository_role = role
        self._repository_root = repository_root
        self._repository_root_identity = exact_identity
        self._prepared_input_enabled = prepared_input_enabled
        self._hello = True

    def _receive_result(
        self, request: _ClosedLocalGitRequest | ClosedFixtureGitRequest,
        sequence: int, request_id: str, *, fixture_id: str | None = None,
    ) -> _ChildGitResult:
        payload, credentials = _receive_packet(self._channel)
        self._verify_owner_credentials(credentials)
        metadata = _decode_json_packet(payload)
        expected_keys = {
            "child_id", "cleanup_confirmed", "commands_after", "commands_before",
            "execution_ordinal", "finished_monotonic_ns", "kind", "operation_id",
            "output_bytes_after", "output_bytes_before", "process_group_id",
            "process_id", "repository_id", "request_id", "request_sha256",
            "returncode", "sequence", "started_monotonic_ns", "stderr_length",
            "stderr_sha256", "stdout_length", "stdout_sha256", "version",
            "argv_trace",
            "fixture_write_bytes", "fixture_write_kind",
            "diagnostic_clone_receipt_id",
            "metadata_kind", "metadata_present", "metadata_length",
            "metadata_sha256",
        }
        request_value = (
            _request_dict(request) if type(request) is _ClosedLocalGitRequest
            else _fixture_request_dict(request)
        )
        if type(request) is ClosedFixtureGitRequest:
            if fixture_id is None or not _HEX32.fullmatch(fixture_id):
                raise NativeGitOwnerError("fixture result lacks its exact capability identity")
            request_value = {
                "fixture_id": fixture_id, "kind": "fixture_request",
                "request": request_value,
            }
        request_sha = hashlib.sha256(_canonical_json_bytes(request_value)).hexdigest()
        if (
            set(metadata) != expected_keys or metadata.get("kind") != "result"
            or type(metadata.get("version")) is not int
            or metadata.get("version") != _CHILD_PROTOCOL_VERSION
            or metadata.get("operation_id") != self._operation_id
            or metadata.get("child_id") != self._child_id
            or metadata.get("repository_id") != self._repository_id
            or metadata.get("sequence") != sequence
            or metadata.get("request_id") != request_id
            or metadata.get("request_sha256") != request_sha
        ):
            raise NativeGitOwnerError("Native child Git result does not bind its request")
        is_payload = (
            request.verb == "show_blob_payload"
            or type(request) is ClosedFixtureGitRequest
            and request.verb is FixtureGitVerb.SHOW_BLOB_PAYLOAD
        )
        is_source_graph = (
            type(request) is _ClosedLocalGitRequest
            and request.verb in {
                "source_validate_ref_graph", "source_ref_inventory",
            }
        )
        result_cap = (
            _MAX_CHILD_PAYLOAD_RESULT_BYTES if is_payload
            else _MAX_CHILD_SOURCE_GRAPH_BYTES + 64 * 1024
            if is_source_graph else _MAX_CHILD_METADATA_RESULT_BYTES
        )
        stdout_cap = (
            _MAX_CHILD_PAYLOAD_BYTES if is_payload
            else _MAX_CHILD_SOURCE_GRAPH_BYTES if is_source_graph else 2 * 1024 * 1024
        )
        stdout_len = _bounded_wire_int(metadata["stdout_length"], stdout_cap)
        stderr_len = _bounded_wire_int(metadata["stderr_length"], 64 * 1024)
        if (
            stdout_len + stderr_len > result_cap
            or type(metadata.get("output_bytes_before")) is not int
            or type(metadata.get("output_bytes_after")) is not int
            or metadata["output_bytes_before"] < 0
            or metadata["output_bytes_after"] > _MAX_STATE_GIT_OUTPUT_BYTES
        ):
            raise NativeGitOwnerError("Native child Git result exceeds its aggregate response cap")
        stdout = self._receive_stream(b"O", stdout_len)
        stderr = self._receive_stream(b"E", stderr_len)
        metadata_kind = metadata.get("metadata_kind")
        metadata_present = metadata.get("metadata_present")
        metadata_len = _bounded_wire_int(
            metadata.get("metadata_length"), _MAX_CHILD_METADATA_FILE_BYTES,
        )
        metadata_sha256 = metadata.get("metadata_sha256")
        if (
            metadata_kind is not None and not isinstance(metadata_kind, str)
            or type(metadata_present) is not bool
            or not isinstance(metadata_sha256, str)
            or not _SHA256.fullmatch(metadata_sha256)
            or metadata_kind is None and (metadata_present or metadata_len)
            or metadata_len == 0 and metadata_sha256 != hashlib.sha256(b"").hexdigest()
        ):
            raise NativeGitOwnerError("Native child Git metadata result identity is malformed")
        diagnostic_receipt_id = metadata.get("diagnostic_clone_receipt_id")
        source_diagnostic_binding = (
            self._diagnostic_layout_by_cap.get(request.source.repository_id)
            if type(request) is ClosedFixtureGitRequest
            and type(request.source) is ChildFixtureRepository else None
        )
        destination_diagnostic_binding = (
            self._diagnostic_layout_by_cap.get(request.destination.repository_id)
            if type(request) is ClosedFixtureGitRequest
            and type(request.destination) is ChildFixtureRepository else None
        )
        expects_diagnostic_receipt = (
            type(request) is ClosedFixtureGitRequest
            and metadata.get("returncode") == 0
            and request.verb in {
                FixtureGitVerb.CLONE_SHARED, FixtureGitVerb.CLONE_SHARED_CHECKOUT,
            }
            and type(request.source) is ChildFixtureRepository
            and type(request.destination) is ChildFixtureRepository
            and source_diagnostic_binding is not None
            and source_diagnostic_binding[1] == "source"
            and destination_diagnostic_binding == (
                source_diagnostic_binding[0], "disposable",
            )
            and self._fixture_capabilities.get(source_diagnostic_binding[0]) is not None
            and self._fixture_capabilities[source_diagnostic_binding[0]].kind
            is FixtureRepositoryKind.DIAGNOSTIC_LAYOUT
        )
        if (
            expects_diagnostic_receipt
            and (not isinstance(diagnostic_receipt_id, str) or not _HEX32.fullmatch(diagnostic_receipt_id))
            or not expects_diagnostic_receipt and diagnostic_receipt_id is not None
        ):
            raise NativeGitOwnerError("diagnostic clone receipt does not match its exact successful clone request")
        metadata_bytes = self._receive_stream(b"M", metadata_len)
        if (
            hashlib.sha256(stdout).hexdigest() != metadata["stdout_sha256"]
            or hashlib.sha256(stderr).hexdigest() != metadata["stderr_sha256"]
            or hashlib.sha256(metadata_bytes).hexdigest() != metadata_sha256
            or metadata["cleanup_confirmed"] is not True
        ):
            raise NativeGitOwnerError("Native child Git result bytes or cleanup proof are invalid")
        integer_names = (
            "commands_before", "commands_after", "execution_ordinal",
            "finished_monotonic_ns", "output_bytes_after", "output_bytes_before",
            "process_group_id", "process_id", "returncode", "started_monotonic_ns",
        )
        if any(type(metadata[name]) is not int for name in integer_names):
            raise NativeGitOwnerError("Native child Git result counters or process identity are malformed")
        if (
            metadata["commands_after"] != metadata["commands_before"] + 1
            or metadata["output_bytes_after"] - metadata["output_bytes_before"] != len(stdout) + len(stderr)
            or metadata["execution_ordinal"] != metadata["commands_after"]
            or not 0 <= metadata["returncode"] <= 255
            or metadata["process_id"] < 1 or metadata["process_group_id"] < 1
            or metadata["started_monotonic_ns"] < 0
            or metadata["finished_monotonic_ns"] < metadata["started_monotonic_ns"]
        ):
            raise NativeGitOwnerError("Native child Git result counters or bounds are inconsistent")
        argv_trace = metadata.get("argv_trace")
        if (
            not isinstance(argv_trace, list)
            or not argv_trace
            or any(not isinstance(part, str) or "\0" in part for part in argv_trace)
            or len(argv_trace) > 256
        ):
            raise NativeGitOwnerError("Native child Git result lacks its bounded actual argv trace")
        diagnostic_receipt = None
        if diagnostic_receipt_id is not None:
            assert type(request) is ClosedFixtureGitRequest
            assert type(request.source) is ChildFixtureRepository
            assert type(request.destination) is ChildFixtureRepository
            diagnostic_receipt = ChildDiagnosticCloneReceipt(
                session=self, receipt_id=diagnostic_receipt_id,
                layout_id=source_diagnostic_binding[0],
                source_id=request.source.repository_id,
                destination_id=request.destination.repository_id,
                _token=_DIAGNOSTIC_CLONE_TOKEN,
            )
        return _ChildGitResult(
            returncode=metadata["returncode"], stdout=stdout, stderr=stderr,
            execution_ordinal=metadata["execution_ordinal"],
            commands_before=metadata["commands_before"],
            commands_after=metadata["commands_after"],
            output_bytes_before=metadata["output_bytes_before"],
            output_bytes_after=metadata["output_bytes_after"],
            started_monotonic_ns=metadata["started_monotonic_ns"],
            finished_monotonic_ns=metadata["finished_monotonic_ns"],
            process_id=metadata["process_id"],
            process_group_id=metadata["process_group_id"],
            cleanup_confirmed=True,
            argv_trace=tuple(argv_trace),
            fixture_write_kind=metadata["fixture_write_kind"],
            fixture_write_bytes=metadata["fixture_write_bytes"],
            metadata_kind=metadata_kind,
            metadata_present=metadata_present,
            metadata_bytes=metadata_bytes,
            diagnostic_clone_receipt=diagnostic_receipt,
        )

    def _receive_stream(self, stream_id: bytes, length: int) -> bytes:
        output = bytearray()
        while len(output) < length:
            payload, credentials = _receive_packet(self._channel)
            self._verify_owner_credentials(credentials)
            if len(payload) < 6 or payload[:2] != b"D" + stream_id:
                raise NativeGitOwnerError("Native child Git result stream is out of order")
            offset = struct.unpack(">I", payload[2:6])[0]
            chunk = payload[6:]
            if (
                not chunk or offset != len(output)
                or len(chunk) > _CHILD_RESULT_CHUNK_BYTES
                or len(output) + len(chunk) > length
            ):
                raise NativeGitOwnerError("Native child Git result stream chunk is malformed")
            output.extend(chunk)
        return bytes(output)

    def _verify_owner_credentials(self, credentials: tuple[int, int, int]) -> None:
        if credentials != (self._owner_pid, os.geteuid(), os.getegid()):
            raise NativeGitOwnerError("Native child Git response came from a foreign peer")


def _bounded_wire_int(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise NativeGitOwnerError("child Git wire length is malformed")
    return value


def _refresh_child_fixture_path_identity(
    capability: ChildFixtureRepository, *, should_exist: bool,
    diagnostic_layout: ChildFixtureRepository | None = None,
) -> None:
    if type(capability) is not ChildFixtureRepository or capability._kind is not FixtureRepositoryKind.PATH:
        raise NativeGitOwnerError("only an exact child path capability can change fixture identity")
    try:
        if not should_exist:
            if capability.root.exists() or capability.root.is_symlink():
                raise NativeGitOwnerError("fixture path remains after the owner removal operation")
            capability._root_identity = None
            return
        info = capability.root.lstat()
        valid_parent = capability.root.parent == capability._workspace
        if diagnostic_layout is not None:
            valid_parent = (
                type(diagnostic_layout) is ChildFixtureRepository
                and diagnostic_layout._token is _CHILD_FIXTURE_CAP_TOKEN
                and diagnostic_layout._session is capability._session
                and capability._session._fixture_capabilities.get(
                    diagnostic_layout.repository_id,
                ) is diagnostic_layout
                and diagnostic_layout.kind is FixtureRepositoryKind.DIAGNOSTIC_LAYOUT
                and capability._session._diagnostic_layout_by_cap.get(
                    capability.repository_id,
                ) == (diagnostic_layout.repository_id, "disposable")
                and capability.root.parent == diagnostic_layout.root
            )
            capability._session.assert_fixture_root(
                diagnostic_layout.root, diagnostic_layout,
            )
        if (
            not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or capability.root.resolve(strict=True) != capability.root
            or not valid_parent
            or _directory_identity(capability._workspace) != capability._workspace_identity
        ):
            raise NativeGitOwnerError("fixture operation created an unsealed child path")
        capability._root_identity = _directory_identity(capability.root)
    except OSError as exc:
        raise NativeGitOwnerError("fixture operation path state cannot be verified") from exc
