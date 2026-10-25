"""Narrow Source-side Git reads, brokered in registered Native children."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import os
import pathlib
import re
import subprocess
import sys
import threading
from types import ModuleType
from typing import Any


_BROKER_MODULE_NAME = "canonical_publication_git_owner"
_CHILD_BOOTSTRAP_MARKERS = (
    "DATAPAN_NATIVE_GIT_FD",
    "DATAPAN_NATIVE_GIT_OPERATION_ID",
    "DATAPAN_NATIVE_GIT_CHILD_ID",
    "DATAPAN_NATIVE_GIT_NONCE",
    "DATAPAN_NATIVE_GIT_REPOSITORY_ID",
    "DATAPAN_NATIVE_GIT_OWNER_PID",
    "DATAPAN_NATIVE_GIT_DEADLINE_NS",
)
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_METADATA_BYTES = 2 * 1024 * 1024
_MAX_SOURCE_METADATA_BYTES = 8 * 1024 * 1024
_MAX_BLOB_BYTES = 512 * 1024 * 1024
_LOCK = threading.RLock()
_ACTIVE_HISTORY: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "canonical_source_git_active_history", default=None,
)
_ACTIVE_SOURCE_RUNTIME_READER: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "canonical_source_git_active_source_runtime_reader", default=None,
)
_ACTIVE_HISTORY_RUNTIME_READER: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "canonical_source_git_active_history_runtime_reader", default=None,
)
_NATIVE_ATTEMPTED = False
_NATIVE_BROKEN = False
_NATIVE_MARKERS: tuple[tuple[str, str | None], ...] | None = None
_NATIVE_PID: int | None = None


class SourceGitError(RuntimeError):
    """A malformed or unavailable bounded Source Git operation."""


@contextlib.contextmanager
def _source_runtime_scope(source_runtime_reader: object):
    """Bind one operation's exact Source-runtime loader without history authority.

    Normal Native broker reads need the authenticated runtime package reader,
    but they must not acquire or simulate the historical dependency callback.
    The Native caller owns the reader and its operation binding; this scope
    only makes that exact object available while a synchronous call is in
    flight.
    """
    if not callable(getattr(source_runtime_reader, "load_module", None)):
        raise SourceGitError("Native source_runtime reader is unavailable for the Git operation")
    token = _ACTIVE_SOURCE_RUNTIME_READER.set(source_runtime_reader)
    try:
        yield
    finally:
        _ACTIVE_SOURCE_RUNTIME_READER.reset(token)


@contextlib.contextmanager
def _history_scope(history: object, *, source_runtime_reader: object):
    """Route existing Source validator Git calls through its enumerator reader.

    This is a private, same-process bridge used only while the Source-owned
    dependency enumerator replays existing validators. It does not grant a
    Native capability; the Native history reader remains responsible for each
    exact request and its own object/tree verification.
    """
    if not callable(getattr(source_runtime_reader, "load_module", None)):
        raise SourceGitError("Native source_runtime reader is unavailable for the history operation")
    history_token = _ACTIVE_HISTORY.set(history)
    history_runtime_token = _ACTIVE_HISTORY_RUNTIME_READER.set(source_runtime_reader)
    try:
        with _source_runtime_scope(source_runtime_reader):
            yield
    finally:
        _ACTIVE_HISTORY_RUNTIME_READER.reset(history_runtime_token)
        _ACTIVE_HISTORY.reset(history_token)


def _marker_snapshot() -> tuple[tuple[str, str | None], ...]:
    return tuple((name, os.environ.get(name)) for name in _CHILD_BOOTSTRAP_MARKERS)


def _has_any_marker(snapshot: tuple[tuple[str, str | None], ...]) -> bool:
    return any(value is not None for _, value in snapshot)


def _legacy_is_allowed() -> bool:
    """Report whether this call is still ordinary all-markers-absent mode."""
    global _NATIVE_BROKEN
    snapshot = _marker_snapshot()
    if _NATIVE_ATTEMPTED:
        if (
            _NATIVE_BROKEN
            or _NATIVE_PID != os.getpid()
            or snapshot != _NATIVE_MARKERS
        ):
            _NATIVE_BROKEN = True
            raise SourceGitError("Native Git bootstrap changed or its session is terminal")
        return False
    if _ACTIVE_SOURCE_RUNTIME_READER.get() is not None and not _has_any_marker(snapshot):
        raise SourceGitError("Native source_runtime reader is bound without registered Git bootstrap markers")
    return not _has_any_marker(snapshot)


def _load_exact_module(name: str) -> ModuleType:
    if name != _BROKER_MODULE_NAME:
        raise SourceGitError(f"{name} is outside the fixed #768 source_runtime module set")
    history = _ACTIVE_HISTORY.get()
    reader = _ACTIVE_SOURCE_RUNTIME_READER.get()
    if reader is None:
        raise SourceGitError("Native source_runtime module load is outside its operation-bound runtime scope")
    if history is not None and _ACTIVE_HISTORY_RUNTIME_READER.get() is not reader:
        raise SourceGitError("Native source_runtime reader differs from the active history pair")
    load_module = getattr(reader, "load_module", None)
    if not callable(load_module):
        raise SourceGitError("Native source_runtime package reader is unavailable")
    module = load_module(name)
    if (
        not isinstance(module, ModuleType)
        or getattr(module, "__name__", None) != name
        or sys.modules.get(name) is not module
    ):
        raise SourceGitError(f"{name} is not the exact Native-authenticated source_runtime singleton")
    return module


def _load_native_owner() -> ModuleType:
    try:
        return _load_exact_module(_BROKER_MODULE_NAME)
    except (FileNotFoundError, OSError) as exc:
        raise SourceGitError("Native Git broker source is not in the authenticated source_runtime closure") from exc

def _native_session(root: object) -> tuple[ModuleType | None, Any, Any | None]:
    """Get the one broker-cached child session; never reopen after failure."""
    global _NATIVE_ATTEMPTED, _NATIVE_BROKEN, _NATIVE_MARKERS, _NATIVE_PID
    snapshot = _marker_snapshot()
    if not _NATIVE_ATTEMPTED:
        if not _has_any_marker(snapshot):
            if _ACTIVE_SOURCE_RUNTIME_READER.get() is not None:
                raise SourceGitError("Native source_runtime reader is bound without registered Git bootstrap markers")
            return None, None, None
        _NATIVE_ATTEMPTED = True
        _NATIVE_MARKERS = snapshot
        _NATIVE_PID = os.getpid()
    elif (
        _NATIVE_BROKEN
        or _NATIVE_PID != os.getpid()
        or snapshot != _NATIVE_MARKERS
    ):
        _NATIVE_BROKEN = True
        raise SourceGitError("Native Git bootstrap changed or its session is terminal")

    if any(value is None or value == "" for _, value in snapshot):
        _NATIVE_BROKEN = True
        raise SourceGitError("Native Git bootstrap markers are incomplete or empty")

    try:
        owner = _load_native_owner()
        fixture_type = getattr(owner, "ChildFixtureRepository", None)
        session_type = getattr(owner, "_ChildGitSession", None)
        result_type = getattr(owner, "_ChildGitResult", None)
        if (
            not isinstance(fixture_type, type)
            or not isinstance(session_type, type)
            or not isinstance(result_type, type)
            or sys.modules.get(_BROKER_MODULE_NAME) is not owner
        ):
            raise SourceGitError("Native Git broker lacks its exact typed child interface")

        if type(root) is fixture_type:
            getter = getattr(owner, "child_git_session_for_fixture", None)
            if not callable(getter):
                raise SourceGitError("Native Git broker lacks its exact fixture-session accessor")
            session = getter(root)
            fixture_root = getattr(root, "root", None)
            assert_fixture_root = getattr(session, "assert_fixture_root", None)
            if not isinstance(fixture_root, pathlib.Path) or not callable(assert_fixture_root):
                raise SourceGitError("Native fixture root lacks its issuer-bound root assertion")
            assert_fixture_root(fixture_root, root)
            fixture = root
        elif isinstance(root, (str, os.PathLike)):
            getter = getattr(owner, "child_git_session_for_source", None)
            if not callable(getter):
                raise SourceGitError("Native Git broker lacks its exact Source-session accessor")
            source_root = pathlib.Path(root)
            session = getter(source_root)
            assert_source_root = getattr(session, "assert_source_root", None)
            if not callable(assert_source_root):
                raise SourceGitError("Native Source session lacks its exact root assertion")
            assert_source_root(source_root)
            fixture = None
        else:
            raise SourceGitError("Native Git root is neither an authenticated Source path nor a fixture capability")

        if type(session) is not session_type:
            raise SourceGitError("Native Git broker did not return its exact cached child session")
        if fixture is None and not callable(getattr(session, "run_git", None)):
            raise SourceGitError("Native Source session lacks its exact typed Git request method")
        if fixture is not None and not callable(getattr(session, "run_fixture", None)):
            raise SourceGitError("Native fixture session lacks its exact typed fixture request method")
        return owner, session, fixture
    except BaseException:
        _NATIVE_BROKEN = True
        raise


def _native_request(
    root: object,
    verb: str,
    *,
    commit_sha: str | None = None,
    path: str | None = None,
    ancestor_sha: str | None = None,
    descendant_sha: str | None = None,
) -> Any:
    global _NATIVE_BROKEN
    with _LOCK:
        owner, session, fixture = _native_session(root)
        if session is None:
            raise SourceGitError("Native Git request was attempted without a Native session")
        try:
            if fixture is None:
                request_type = getattr(owner, "_ClosedLocalGitRequest", None)
                if not isinstance(request_type, type):
                    raise SourceGitError("Native Git broker lacks its exact Source request type")
                request = request_type(
                    verb=verb,
                    commit_sha=commit_sha,
                    path=path,
                    ancestor_sha=ancestor_sha,
                    descendant_sha=descendant_sha,
                )
                if type(request) is not request_type:
                    raise SourceGitError("Native Git request constructor returned a foreign type")
                result = session.run_git(request)
            else:
                request_type = getattr(owner, "ClosedFixtureGitRequest", None)
                verb_type = getattr(owner, "FixtureGitVerb", None)
                if not isinstance(request_type, type) or not isinstance(verb_type, type):
                    raise SourceGitError("Native Git broker lacks its exact fixture request types")
                fixture_verb = {
                    # The prepared Source and disposable fixture APIs use
                    # separate closed names for the same HEAD observation.
                    "rev_parse_commit_head": "rev_parse_head",
                }.get(verb, verb)
                try:
                    exact_verb = verb_type[fixture_verb.upper()]
                except (KeyError, AttributeError) as exc:
                    raise SourceGitError("Native fixture Git verb is outside the closed read set") from exc
                request = request_type(
                    verb=exact_verb,
                    commit_sha=commit_sha,
                    path=path,
                    ancestor_sha=ancestor_sha,
                    descendant_sha=descendant_sha,
                )
                if type(request) is not request_type:
                    raise SourceGitError("Native fixture request constructor returned a foreign type")
                result = session.run_fixture(fixture, request)
            if (
                type(result) is not owner._ChildGitResult
                or type(result.returncode) is not int
                or not 0 <= result.returncode <= 255
                or type(result.stdout) is not bytes
                or type(result.stderr) is not bytes
                or result.cleanup_confirmed is not True
            ):
                raise SourceGitError("Native Git broker returned an unverified child result")
            if fixture is not None:
                trace = getattr(result, "argv_trace", None)
                if (
                    type(trace) is not tuple
                    or not trace
                    or any(not isinstance(argument, str) for argument in trace)
                ):
                    raise SourceGitError("Native fixture result lacks its exact constructed argv trace")
            return result
        except BaseException:
            _NATIVE_BROKEN = True
            raise


def _native_source_metadata_request(root: object, verb: str) -> Any:
    """Issue one Native-owned CHECK14-only Source metadata selector.

    Source metadata is a separate authority from an ordinary prepared-release
    read: the broker requires the exact registered CHECK14 child and issues a
    read-only Source capability for the prepared-release root.  This helper
    deliberately has no caller path, argv, or byte-limit input.
    """
    global _NATIVE_BROKEN
    with _LOCK:
        owner, session, fixture = _native_session(root)
        if session is None or fixture is not None:
            raise SourceGitError("Source metadata requires the authenticated prepared-release session")
        try:
            source_factory = getattr(session, "source_repository", None)
            request_type = getattr(owner, "ClosedLocalGitRequest", None)
            if not isinstance(request_type, type):
                request_type = getattr(owner, "_ClosedLocalGitRequest", None)
            runner = getattr(session, "run_source_git", None)
            source_type = getattr(owner, "ChildSourceRepository", None)
            if (
                not callable(source_factory)
                or not callable(runner)
                or not isinstance(request_type, type)
                or not isinstance(source_type, type)
                or sys.modules.get(_BROKER_MODULE_NAME) is not owner
            ):
                raise SourceGitError("Native broker lacks its exact Source metadata request interface")
            source = source_factory()
            if type(source) is not source_type:
                raise SourceGitError("Native broker returned a foreign Source capability")
            request = request_type(verb=verb)
            if type(request) is not request_type:
                raise SourceGitError("Native Source metadata request constructor returned a foreign type")
            result = runner(source, request)
            if (
                type(result) is not owner._ChildGitResult
                or type(result.returncode) is not int
                or not 0 <= result.returncode <= 255
                or type(result.stdout) is not bytes
                or type(result.stderr) is not bytes
                or result.cleanup_confirmed is not True
            ):
                raise SourceGitError("Native Source metadata result lacks exact execution evidence")
            return result
        except BaseException:
            _NATIVE_BROKEN = True
            raise


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA1_RE.fullmatch(value) is None or value == "0" * 40:
        raise ValueError(f"{label} must be a full nonzero Git SHA")
    return value


def _require_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Git blob path must be a normalized repository-relative path")
    return value


def _legacy_result(
    root: object,
    argv: tuple[str, ...],
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    if not isinstance(root, (str, os.PathLike)):
        raise SourceGitError("ordinary local Git reads require an explicit filesystem root")
    return subprocess.run(
        ("git", *argv),
        cwd=pathlib.Path(root),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _failed_command(result: Any, argv: tuple[str, ...]) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        result.returncode,
        ("git", *argv),
        output=result.stdout,
        stderr=result.stderr,
    )


def _parse_blob_info(stdout: bytes) -> tuple[str, int]:
    if type(stdout) is not bytes or not 1 <= len(stdout) <= 256:
        raise SourceGitError("Native Git blob-info result exceeds its fixed metadata shape")
    try:
        text = stdout.decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise SourceGitError("Native Git blob-info result is not ASCII") from exc
    if not text.endswith("\n") or text.count("\n") != 1:
        raise SourceGitError("Native Git blob-info result is not one canonical record")
    fields = text[:-1].split(" ")
    if (
        len(fields) != 3
        or _SHA1_RE.fullmatch(fields[0]) is None
        or fields[1] != "blob"
        or not fields[2].isascii()
        or not fields[2].isdecimal()
        or len(fields[2]) > 10
        or len(fields[2]) > 1 and fields[2].startswith("0")
    ):
        raise SourceGitError("Native Git blob-info result is malformed")
    size = int(fields[2])
    if size > _MAX_BLOB_BYTES or text != f"{fields[0]} blob {size}\n":
        raise SourceGitError("Native Git blob-info result is noncanonical or exceeds the file cap")
    return fields[0], size


def _checked_payload(data: bytes, oid: str, size: int) -> bytes:
    if type(data) is not bytes or len(data) != size:
        raise SourceGitError("Native Git blob payload length differs from its exact blob info")
    import hashlib

    actual = hashlib.sha1(b"blob " + str(size).encode("ascii") + b"\0" + data).hexdigest()
    if actual != oid:
        raise SourceGitError("Native Git blob payload differs from its exact Git object ID")
    return data


def _blob_info_native(
    root: pathlib.Path,
    revision: str,
    path: str,
    *,
    check: bool,
    git_command: str,
) -> tuple[str, int] | None:
    result = _native_request(
        root, "show_blob_info", commit_sha=revision, path=path,
    )
    if result.returncode != 0:
        if not check:
            return None
        raise _failed_command(result, (git_command, f"{revision}:{path}"))
    missing = f"{revision}:{path} missing\n".encode("ascii")
    if result.stdout == missing:
        if not check:
            return None
        raise subprocess.CalledProcessError(
            128,
            ("git", git_command, f"{revision}:{path}"),
            output=result.stdout,
            stderr=b"",
        )
    return _parse_blob_info(result.stdout)


def _validated_sha_output(result: Any, argv: tuple[str, ...], *, check: bool) -> str:
    if result.returncode != 0:
        if not check:
            return ""
        raise _failed_command(result, argv)
    try:
        text = result.stdout.decode("ascii", "strict")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise SourceGitError("Native Git identity result is not ASCII") from exc
    if not text.endswith("\n") or text.count("\n") != 1:
        raise SourceGitError("Native Git identity result is not one complete SHA record")
    value = text[:-1]
    if _SHA1_RE.fullmatch(value) is None or value == "0" * 40:
        raise SourceGitError("Native Git identity result is not a full nonzero SHA")
    return value


def read_blob(
    root: object,
    revision: str,
    path: str,
    *,
    check: bool = True,
    timeout: float | None = None,
) -> bytes:
    """Read one exact blob; Native payload reads require State blob-info first."""
    revision = _require_sha(revision, "Git revision")
    path = _require_path(path)
    history = _ACTIVE_HISTORY.get()
    if history is not None:
        reader = getattr(history, "canonical_source_read_blob", None)
        if not callable(reader):
            raise SourceGitError("active Source history traversal lacks its exact blob callback")
        return reader(
            root, revision, path, check=check, caller_frame=sys._getframe(1),
        )
    if _legacy_is_allowed():
        result = _legacy_result(root, ("show", f"{revision}:{path}"), timeout=timeout)
        if result.returncode != 0:
            if check:
                raise _failed_command(result, ("show", f"{revision}:{path}"))
            return b""
        return result.stdout

    info = _blob_info_native(
        root, revision, path, check=check, git_command="show",
    )
    if info is None:
        return b""
    oid, size = info
    if size <= _MAX_METADATA_BYTES:
        result = _native_request(
            root, "show_blob", commit_sha=revision, path=path,
        )
    else:
        result = _native_request(
            root, "show_blob_payload", commit_sha=revision, path=path,
        )
    if result.returncode != 0:
        if not check:
            return b""
        raise _failed_command(result, ("show", f"{revision}:{path}"))
    return _checked_payload(result.stdout, oid, size)


def read_blob_oid(
    root: object,
    revision: str,
    path: str,
    *,
    check: bool = True,
    timeout: float | None = None,
) -> str:
    """Return the exact Git object ID for one registered commit/path pair."""
    revision = _require_sha(revision, "Git revision")
    path = _require_path(path)
    history = _ACTIVE_HISTORY.get()
    if history is not None:
        reader = getattr(history, "canonical_source_read_blob_oid", None)
        if not callable(reader):
            raise SourceGitError("active Source history traversal lacks its exact blob-OID callback")
        return reader(
            root, revision, path, check=check, caller_frame=sys._getframe(1),
        )
    if _legacy_is_allowed():
        result = _legacy_result(
            root, ("rev-parse", f"{revision}:{path}"), timeout=timeout,
        )
        return _validated_sha_output(
            result, ("rev-parse", f"{revision}:{path}"), check=check,
        )
    info = _blob_info_native(
        root, revision, path, check=check, git_command="rev-parse",
    )
    if info is None:
        return ""
    oid, _size = info
    return oid


def read_tree_oid(
    root: object,
    revision: str,
    *,
    check: bool = True,
    timeout: float | None = None,
) -> str:
    """Return the exact root tree object ID of one registered commit."""
    revision = _require_sha(revision, "Git revision")
    argv = ("rev-parse", f"{revision}^{{tree}}")
    history = _ACTIVE_HISTORY.get()
    if history is not None:
        reader = getattr(history, "canonical_source_read_tree_oid", None)
        if not callable(reader):
            raise SourceGitError("active Source history traversal lacks its exact commit-tree callback")
        return reader(
            root, revision, check=check, caller_frame=sys._getframe(1),
        )
    if _legacy_is_allowed():
        result = _legacy_result(root, argv, timeout=timeout)
        return _validated_sha_output(result, argv, check=check)
    result = _native_request(root, "rev_parse_tree", commit_sha=revision)
    return _validated_sha_output(result, argv, check=check)


def read_head_oid(
    root: object,
    *,
    check: bool = True,
    timeout: float | None = 30,
) -> str:
    """Read the exact checked-out commit through the same bounded Source seam."""
    argv = ("rev-parse", "--verify", "HEAD^{commit}")
    if _legacy_is_allowed():
        result = _legacy_result(root, argv, timeout=timeout)
    else:
        result = _native_request(root, "rev_parse_commit_head")
    return _validated_sha_output(result, argv, check=check)


def trusted_origin_main(
    root: object,
    *,
    check: bool = True,
    timeout: float | None = None,
) -> str:
    """Return the current repository's exact Native-issued origin/main SHA."""
    argv = ("rev-parse", "refs/remotes/origin/main")
    if _legacy_is_allowed():
        result = _legacy_result(root, argv, timeout=timeout)
        return _validated_sha_output(result, argv, check=check)
    result = _native_request(root, "trusted_origin_main")
    return _validated_sha_output(result, argv, check=check)


def is_ancestor(
    root: object,
    ancestor: str,
    descendant: str,
    *,
    timeout: float | None = None,
) -> bool:
    """Return false only for Git's exact not-an-ancestor result (status 1)."""
    ancestor = _require_sha(ancestor, "ancestor revision")
    descendant = _require_sha(descendant, "descendant revision")
    argv = ("merge-base", "--is-ancestor", ancestor, descendant)
    history = _ACTIVE_HISTORY.get()
    if history is not None:
        reader = getattr(history, "canonical_source_is_ancestor", None)
        if not callable(reader):
            raise SourceGitError("active Source history traversal lacks its exact ancestry callback")
        return reader(
            root, ancestor, descendant, timeout=timeout, caller_frame=sys._getframe(1),
        )
    if _legacy_is_allowed():
        result = _legacy_result(root, argv, timeout=timeout)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise _failed_command(result, argv)
    result = _native_request(
        root, "is_ancestor", ancestor_sha=ancestor, descendant_sha=descendant,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    # Preserve subprocess semantics for a genuine child Git failure so the
    # compatibility dispatcher can honor check=False. Protocol, shape, and
    # cleanup failures were rejected before this point by _native_request.
    raise _failed_command(result, argv)


def _source_metadata_text(
    result: Any, label: str, argv: tuple[str, ...], *, maximum: int = 4096,
) -> str:
    if result.returncode != 0:
        raise _failed_command(result, argv)
    if type(result.stdout) is not bytes or not 1 <= len(result.stdout) <= maximum:
        raise SourceGitError(f"Native {label} result exceeds its fixed metadata shape")
    try:
        value = result.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SourceGitError(f"Native {label} result is not valid UTF-8") from exc
    if not value.endswith("\n") or value.count("\n") != 1 or "\0" in value:
        raise SourceGitError(f"Native {label} result is not one complete record")
    return value[:-1]


def _legacy_source_metadata_text(
    root: object, argv: tuple[str, ...], *, timeout: float | None = None,
) -> str:
    result = _legacy_result(root, argv, timeout=timeout)
    if result.returncode != 0:
        raise _failed_command(result, argv)
    try:
        value = result.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SourceGitError("ordinary Source metadata result is not valid UTF-8") from exc
    if not value.endswith("\n") or value.count("\n") != 1 or "\0" in value:
        raise SourceGitError("ordinary Source metadata result is not one complete record")
    return value[:-1]


def source_is_shallow(root: object, *, timeout: float | None = None) -> bool:
    """Return Git's exact shallow-state record for the authenticated Source."""
    if _legacy_is_allowed():
        value = _legacy_source_metadata_text(
            root, ("rev-parse", "--is-shallow-repository"), timeout=timeout,
        )
    else:
        value = _source_metadata_text(
            _native_source_metadata_request(root, "source_is_shallow"),
            "source_is_shallow", ("rev-parse", "--is-shallow-repository"),
        )
    if value not in {"true", "false"}:
        raise SourceGitError("Source shallow-state result is not exactly true or false")
    return value == "true"


def source_git_dir(root: object, *, timeout: float | None = None) -> pathlib.Path:
    """Return the canonical Git directory reported for the authenticated Source."""
    argv = ("rev-parse", "--path-format=absolute", "--git-dir")
    if _legacy_is_allowed():
        value = _legacy_source_metadata_text(root, argv, timeout=timeout)
    else:
        value = _source_metadata_text(
            _native_source_metadata_request(root, "source_git_dir"), "source_git_dir", argv,
        )
    path = pathlib.Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
        raise SourceGitError("Source Git directory is not its exact canonical directory")
    return path


def source_common_dir(root: object, *, timeout: float | None = None) -> pathlib.Path:
    """Return the canonical common Git directory reported for the Source."""
    argv = ("rev-parse", "--path-format=absolute", "--git-common-dir")
    if _legacy_is_allowed():
        value = _legacy_source_metadata_text(root, argv, timeout=timeout)
    else:
        value = _source_metadata_text(
            _native_source_metadata_request(root, "source_common_dir"), "source_common_dir", argv,
        )
    path = pathlib.Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
        raise SourceGitError("Source common Git directory is not its exact canonical directory")
    return path


def source_shallow_path(
    root: object, *, common_dir: pathlib.Path | None = None,
    timeout: float | None = None,
) -> pathlib.Path:
    """Return only the Git-owned shallow metadata path within Source common-dir."""
    argv = ("rev-parse", "--path-format=absolute", "--git-path", "shallow")
    if _legacy_is_allowed():
        value = _legacy_source_metadata_text(root, argv, timeout=timeout)
    else:
        value = _source_metadata_text(
            _native_source_metadata_request(root, "source_shallow_path"),
            "source_shallow_path", argv,
        )
    path = pathlib.Path(value)
    if not path.is_absolute():
        raise SourceGitError("Source shallow metadata path is not absolute")
    bound_common = common_dir if common_dir is not None else source_common_dir(root, timeout=timeout)
    if not isinstance(bound_common, pathlib.Path) or not bound_common.is_absolute():
        raise SourceGitError("Source shallow metadata requires its exact common directory")
    if path != bound_common / "shallow" or path.resolve(strict=False) != path:
        raise SourceGitError("Source shallow metadata path differs from its exact common directory")
    return path


def source_shallow_bytes(
    root: object, *, shallow_path: pathlib.Path | None = None,
    timeout: float | None = None,
) -> bytes | None:
    """Read only Git's bounded shallow file, preserving absent and empty states."""
    expected_path = shallow_path if shallow_path is not None else source_shallow_path(root, timeout=timeout)
    if not isinstance(expected_path, pathlib.Path):
        raise SourceGitError("Source shallow file requires its exact path result")
    if _legacy_is_allowed():
        try:
            data = expected_path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SourceGitError("Source shallow metadata is unavailable") from exc
        if len(data) > _MAX_SOURCE_METADATA_BYTES:
            raise SourceGitError("Source shallow metadata exceeds its fixed byte cap")
        return data
    result = _native_source_metadata_request(root, "source_shallow_bytes")
    if result.returncode != 0:
        raise _failed_command(result, ("source_shallow_bytes",))
    try:
        reported_path = _source_metadata_text(
            result, "source_shallow_bytes",
            ("rev-parse", "--path-format=absolute", "--git-path", "shallow"),
        )
    except SourceGitError:
        raise
    if pathlib.Path(reported_path) != expected_path:
        raise SourceGitError("Native Source shallow path differs from its prior bound path")
    if getattr(result, "metadata_kind", None) != "source_shallow":
        raise SourceGitError("Native Source shallow result lacks its exact metadata kind")
    present = getattr(result, "metadata_present", None)
    data = getattr(result, "metadata_bytes", None)
    if type(present) is not bool or type(data) is not bytes or len(data) > _MAX_SOURCE_METADATA_BYTES:
        raise SourceGitError("Native Source shallow result lacks its bounded exact bytes")
    if not present and data:
        raise SourceGitError("absent Native Source shallow metadata carried bytes")
    return data if present else None


def source_validate_ref_graph(root: object) -> bytes:
    """Ask the Native owner to validate all prepared Source refs and ancestry."""
    if _legacy_is_allowed():
        result = _legacy_result(root, ("rev-list", "--all", "--parents"))
    else:
        result = _native_source_metadata_request(root, "source_validate_ref_graph")
    if result.returncode != 0:
        raise _failed_command(result, ("rev-list", "--all", "--parents"))
    if type(result.stdout) is not bytes or len(result.stdout) > _MAX_SOURCE_METADATA_BYTES:
        raise SourceGitError("Source ref graph exceeds its fixed closure output cap")
    return result.stdout


def legacy_dispatch_allowed() -> bool:
    """Whether an unrecognized compatibility-dispatch shape may use old local Git."""
    global _NATIVE_ATTEMPTED, _NATIVE_BROKEN, _NATIVE_MARKERS, _NATIVE_PID
    snapshot = _marker_snapshot()
    if _NATIVE_ATTEMPTED:
        if (
            _NATIVE_BROKEN
            or _NATIVE_PID != os.getpid()
            or snapshot != _NATIVE_MARKERS
        ):
            _NATIVE_BROKEN = True
            raise SourceGitError("Native Git bootstrap changed or its session is terminal")
        return False
    if _has_any_marker(snapshot):
        _NATIVE_ATTEMPTED = True
        _NATIVE_MARKERS = snapshot
        _NATIVE_PID = os.getpid()
        _NATIVE_BROKEN = True
        raise SourceGitError("Native Git compatibility dispatcher rejects unregistered request shapes")
    return True
