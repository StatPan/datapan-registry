from __future__ import annotations

import importlib.util
import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from enum import Enum
from dataclasses import dataclass
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "canonical_source_git",
    ROOT / "scripts" / "canonical_source_git.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
DISPATCH_SPEC = importlib.util.spec_from_file_location(
    "generate_completeness_proof_rollup_git_dispatch_test",
    ROOT / "scripts" / "generate-completeness-proof-rollup.py",
)
assert DISPATCH_SPEC and DISPATCH_SPEC.loader
DISPATCH_MODULE = importlib.util.module_from_spec(DISPATCH_SPEC)
DISPATCH_SPEC.loader.exec_module(DISPATCH_MODULE)


def _git(root: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args), cwd=root, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


class CanonicalSourceGitTest(unittest.TestCase):
    def setUp(self):
        self._markers = {
            name: os.environ.pop(name, None)
            for name in MODULE._CHILD_BOOTSTRAP_MARKERS
        }
        MODULE._NATIVE_ATTEMPTED = False
        MODULE._NATIVE_BROKEN = False
        MODULE._NATIVE_MARKERS = None
        MODULE._NATIVE_PID = None

    def tearDown(self):
        for name in MODULE._CHILD_BOOTSTRAP_MARKERS:
            os.environ.pop(name, None)
        for name, value in self._markers.items():
            if value is not None:
                os.environ[name] = value
        MODULE._NATIVE_ATTEMPTED = False
        MODULE._NATIVE_BROKEN = False
        MODULE._NATIVE_MARKERS = None
        MODULE._NATIVE_PID = None

    def test_all_absent_markers_preserve_bounded_local_read_families(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _git(root, "init", "-q")
            _git(root, "config", "user.name", "Source Git Test")
            _git(root, "config", "user.email", "source-git-test@example.invalid")
            (root / "fixture.txt").write_bytes(b"fixture\n")
            _git(root, "add", "fixture.txt")
            _git(root, "commit", "-q", "-m", "fixture")
            commit = _git(root, "rev-parse", "HEAD")
            tree = _git(root, "rev-parse", "HEAD^{tree}")
            _git(root, "update-ref", "refs/remotes/origin/main", commit)
            _git(root, "checkout", "-q", "--orphan", "unrelated")
            (root / "unrelated.txt").write_bytes(b"separate history\n")
            _git(root, "add", "unrelated.txt")
            _git(root, "commit", "-q", "-m", "unrelated")
            unrelated = _git(root, "rev-parse", "HEAD")

            self.assertEqual(MODULE.read_blob(root, commit, "fixture.txt"), b"fixture\n")
            blob_oid = _git(root, "rev-parse", f"{commit}:fixture.txt")
            self.assertEqual(MODULE.read_blob_oid(root, commit, "fixture.txt"), blob_oid)
            self.assertEqual(MODULE.read_tree_oid(root, commit), tree)
            self.assertEqual(MODULE.read_head_oid(root), unrelated)
            self.assertEqual(MODULE.trusted_origin_main(root), commit)
            self.assertTrue(MODULE.is_ancestor(root, commit, commit))

            absent = "1" * 40
            self.assertEqual(MODULE.read_blob(root, absent, "fixture.txt", check=False), b"")
            self.assertEqual(MODULE.read_blob_oid(root, absent, "fixture.txt", check=False), "")
            self.assertFalse(MODULE.is_ancestor(root, unrelated, commit))
            with self.assertRaises(subprocess.CalledProcessError):
                MODULE.is_ancestor(root, absent, commit)

    def test_source_history_scope_routes_all_historical_git_reads_to_typed_callbacks(self):
        class History:
            def __init__(self):
                self.calls = []

            def canonical_source_read_blob(self, root, revision, path, *, check, caller_frame):
                self.calls.append(("blob", revision, path, check, caller_frame.f_code.co_name))
                return b"history blob\n"

            def canonical_source_read_blob_oid(self, root, revision, path, *, check, caller_frame):
                self.calls.append(("blob_oid", revision, path, check, caller_frame.f_code.co_name))
                return "2" * 40

            def canonical_source_read_tree_oid(self, root, revision, *, check, caller_frame):
                self.calls.append(("tree", revision, check, caller_frame.f_code.co_name))
                return "3" * 40

            def canonical_source_is_ancestor(self, root, ancestor, descendant, *, timeout, caller_frame):
                self.calls.append(("ancestor", ancestor, descendant, caller_frame.f_code.co_name))
                return True

        history = History()
        class RuntimeReader:
            def load_module(self, name):
                raise AssertionError(f"unexpected source_runtime module read: {name}")

        runtime_reader = RuntimeReader()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with mock.patch.object(MODULE.subprocess, "run") as run:
                with MODULE._history_scope(history, source_runtime_reader=runtime_reader):
                    self.assertEqual(MODULE.read_blob(root, "1" * 40, "schema.json"), b"history blob\n")
                    self.assertEqual(MODULE.read_blob_oid(root, "1" * 40, "schema.json"), "2" * 40)
                    self.assertEqual(MODULE.read_tree_oid(root, "1" * 40), "3" * 40)
                    self.assertTrue(MODULE.is_ancestor(root, "1" * 40, "4" * 40))
                run.assert_not_called()
        self.assertEqual(["blob", "blob_oid", "tree", "ancestor"], [item[0] for item in history.calls])

    def test_all_absent_source_metadata_reads_preserve_git_shallow_and_graph_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _git(root, "init", "-q", "--initial-branch=main")
            _git(root, "config", "user.name", "Source Git Test")
            _git(root, "config", "user.email", "source-git-test@example.invalid")
            (root / "fixture.txt").write_bytes(b"fixture graph\n")
            _git(root, "add", "fixture.txt")
            _git(root, "commit", "-q", "-m", "fixture")

            self.assertFalse(MODULE.source_is_shallow(root))
            common_dir = MODULE.source_common_dir(root)
            self.assertEqual(MODULE.source_git_dir(root), common_dir)
            shallow_path = MODULE.source_shallow_path(root, common_dir=common_dir)
            self.assertEqual(shallow_path, common_dir / "shallow")
            self.assertIsNone(MODULE.source_shallow_bytes(root, shallow_path=shallow_path))
            graph = MODULE.source_validate_ref_graph(root)
            self.assertIn(_git(root, "rev-parse", "HEAD").encode("ascii"), graph)

    def test_partial_or_empty_marker_is_terminal_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _git(root, "init", "-q")
            with mock.patch.dict(os.environ, {MODULE._CHILD_BOOTSTRAP_MARKERS[0]: ""}):
                with mock.patch.object(MODULE.subprocess, "run") as run:
                    with self.assertRaises(MODULE.SourceGitError):
                        MODULE.read_blob(root, "1" * 40, "fixture.txt")
                    run.assert_not_called()

    def test_compatibility_dispatcher_routes_exact_reads_and_rejects_native_unknown_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _git(root, "init", "-q")
            _git(root, "config", "user.name", "Source Git Test")
            _git(root, "config", "user.email", "source-git-test@example.invalid")
            (root / "fixture.txt").write_bytes(b"dispatcher fixture\n")
            _git(root, "add", "fixture.txt")
            _git(root, "commit", "-q", "-m", "fixture")
            commit = _git(root, "rev-parse", "HEAD")
            _git(root, "update-ref", "refs/remotes/origin/main", commit)

            with mock.patch.object(DISPATCH_MODULE, "_load_canonical_source_git", return_value=MODULE):
                self.assertEqual(
                    DISPATCH_MODULE.git_read_only(root, ["show", f"{commit}:fixture.txt"]),
                    b"dispatcher fixture\n",
                )
                self.assertEqual(
                    DISPATCH_MODULE.git_read_only(
                        root,
                        ["merge-base", "--is-ancestor", "1" * 40, commit],
                        check=False,
                    ),
                    b"",
                )

                os.environ[MODULE._CHILD_BOOTSTRAP_MARKERS[0]] = ""
                with mock.patch.object(DISPATCH_MODULE.subprocess, "run") as run:
                    with self.assertRaises(MODULE.SourceGitError):
                        DISPATCH_MODULE.git_read_only(root, ["rev-parse", "HEAD"])
                    run.assert_not_called()

                    for name in MODULE._CHILD_BOOTSTRAP_MARKERS:
                        os.environ.pop(name, None)
                    with self.assertRaises(MODULE.SourceGitError):
                        MODULE.read_blob(root, "1" * 40, "fixture.txt")
                    run.assert_not_called()

    def test_noncanonical_blob_paths_are_rejected_before_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with mock.patch.object(MODULE.subprocess, "run") as run:
                for path in ("", "/absolute", "a/../b", "a//b", "a\nb"):
                    with self.subTest(path=path), self.assertRaises(ValueError):
                        MODULE.read_blob(root, "1" * 40, path)
                run.assert_not_called()

    def test_exact_source_runtime_loader_rejects_broker_singleton_loaded_under_an_alias(self):
        owner_name = "canonical_publication_git_owner"
        alias_name = "canonical_publication_git_owner_noncanonical_test_alias"
        alias = types.ModuleType(owner_name)
        alias.__file__ = str(pathlib.Path(MODULE.__file__).resolve(strict=True))
        prior_alias = sys.modules.get(alias_name)
        prior_owner = sys.modules.pop(owner_name, None)
        history = object()

        class Reader:
            def load_module(self, name):
                self.requested = name
                return alias

        reader = Reader()
        try:
            sys.modules[alias_name] = alias
            with MODULE._source_runtime_scope(reader):
                with self.assertRaisesRegex(MODULE.SourceGitError, "exact Native-authenticated"):
                    MODULE._load_exact_module(owner_name)
            self.assertEqual(owner_name, reader.requested)
        finally:
            if prior_alias is None:
                sys.modules.pop(alias_name, None)
            else:
                sys.modules[alias_name] = prior_alias
            if prior_owner is not None:
                sys.modules[owner_name] = prior_owner

    def test_runtime_scope_loads_exact_owner_without_history_and_restores_after_error(self):
        owner_name = "canonical_publication_git_owner"
        owner = types.ModuleType(owner_name)
        reader_calls = []

        class Reader:
            def load_module(self, name):
                reader_calls.append(name)
                return owner

        reader = Reader()
        prior_owner = sys.modules.get(owner_name)
        sys.modules[owner_name] = owner
        try:
            self.assertIsNone(MODULE._ACTIVE_HISTORY.get())
            self.assertIsNone(MODULE._ACTIVE_SOURCE_RUNTIME_READER.get())
            with MODULE._source_runtime_scope(reader):
                self.assertIsNone(MODULE._ACTIVE_HISTORY.get())
                self.assertIs(MODULE._ACTIVE_SOURCE_RUNTIME_READER.get(), reader)
                self.assertIs(MODULE._load_exact_module(owner_name), owner)
                with self.assertRaisesRegex(RuntimeError, "scope sentinel"):
                    with MODULE._source_runtime_scope(reader):
                        raise RuntimeError("scope sentinel")
            self.assertEqual(reader_calls, [owner_name])
            self.assertIsNone(MODULE._ACTIVE_HISTORY.get())
            self.assertIsNone(MODULE._ACTIVE_SOURCE_RUNTIME_READER.get())
        finally:
            if prior_owner is None:
                sys.modules.pop(owner_name, None)
            else:
                sys.modules[owner_name] = prior_owner

    def test_registered_native_read_uses_unpatched_owner_loader_without_history(self):
        owner_name = MODULE._BROKER_MODULE_NAME
        revision = "4" * 40
        payload = b"registered Native read without history\n"
        blob_oid = hashlib.sha1(
            b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload,
        ).hexdigest()

        @dataclass(frozen=True)
        class ClosedLocalGitRequest:
            verb: str
            commit_sha: str | None = None
            path: str | None = None
            ancestor_sha: str | None = None
            descendant_sha: str | None = None

        @dataclass(frozen=True)
        class ChildResult:
            returncode: int
            stdout: bytes = b""
            stderr: bytes = b""
            cleanup_confirmed: bool = True

        class ChildSession:
            def __init__(self):
                self.requests = []
                self.roots = []

            def assert_source_root(self, root):
                self.roots.append(root)

            def run_git(self, request):
                self.requests.append(request)
                if request.verb == "show_blob_info":
                    return ChildResult(0, f"{blob_oid} blob {len(payload)}\n".encode("ascii"))
                if request.verb == "show_blob":
                    return ChildResult(0, payload)
                raise AssertionError(f"unexpected exact broker request: {request.verb}")

        owner = types.ModuleType(owner_name)
        owner._ClosedLocalGitRequest = ClosedLocalGitRequest
        owner._ChildGitResult = ChildResult
        owner._ChildGitSession = ChildSession
        owner.ChildFixtureRepository = type("ChildFixtureRepository", (), {})
        owner.session = ChildSession()
        owner.child_git_session_for_source = lambda root: owner.session
        expected_markers = {
            name: f"registered-{index}"
            for index, name in enumerate(MODULE._CHILD_BOOTSTRAP_MARKERS)
        }

        class ExactRuntimeReader:
            def __init__(self):
                self.loads = []

            def load_module(self, name):
                self.loads.append(name)
                if name != owner_name:
                    raise AssertionError(f"unregistered Source-runtime module: {name}")
                if any(os.environ.get(key) != value for key, value in expected_markers.items()):
                    raise MODULE.SourceGitError("stale registered Source-runtime reader")
                if sys.modules.get(owner_name) is not owner:
                    raise MODULE.SourceGitError("broker singleton changed during Native read")
                return owner

        reader = ExactRuntimeReader()
        root = pathlib.Path("/prepared/registered-source")
        prior_owner = sys.modules.get(owner_name)
        sys.modules[owner_name] = owner
        try:
            with mock.patch.dict(os.environ, expected_markers, clear=False):
                # Deliberately do not patch _load_native_owner: this follows the
                # normal Source helper -> exact runtime reader -> broker route.
                with MODULE._source_runtime_scope(reader):
                    self.assertIsNone(MODULE._ACTIVE_HISTORY.get())
                    self.assertEqual(MODULE.read_blob(root, revision, "manifest.json"), payload)
                self.assertEqual(reader.loads, [owner_name, owner_name])
                self.assertEqual(
                    [request.verb for request in owner.session.requests],
                    ["show_blob_info", "show_blob"],
                )
                self.assertEqual(owner.session.roots, [root, root])
                self.assertIsNone(MODULE._ACTIVE_SOURCE_RUNTIME_READER.get())

                # The same reader cannot outlive the registered child identity.
                os.environ[MODULE._CHILD_BOOTSTRAP_MARKERS[0]] = "changed-operation"
                with mock.patch.object(
                    MODULE.subprocess, "run", side_effect=AssertionError("local Git fallback"),
                ) as run:
                    with self.assertRaisesRegex(MODULE.SourceGitError, "bootstrap changed"):
                        with MODULE._source_runtime_scope(reader):
                            MODULE.read_blob(root, revision, "manifest.json")
                    run.assert_not_called()
        finally:
            if prior_owner is None:
                sys.modules.pop(owner_name, None)
            else:
                sys.modules[owner_name] = prior_owner

    def test_runtime_reader_without_registered_markers_cannot_fall_back_to_local_git(self):
        class Reader:
            def load_module(self, name):
                raise AssertionError("a runtime module should not be needed before marker validation")

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with MODULE._source_runtime_scope(Reader()):
                with mock.patch.object(MODULE.subprocess, "run", side_effect=AssertionError("local Git fallback")) as run:
                    with self.assertRaisesRegex(MODULE.SourceGitError, "without registered Git bootstrap markers"):
                        MODULE.read_blob(root, "1" * 40, "README.md")
                    run.assert_not_called()

    def test_registered_native_read_without_runtime_scope_fails_before_local_git(self):
        root = pathlib.Path("/prepared/registered-source")
        revision = "5" * 40
        marker_values = {
            name: f"registered-{index}"
            for index, name in enumerate(MODULE._CHILD_BOOTSTRAP_MARKERS)
        }
        with mock.patch.dict(os.environ, marker_values, clear=False):
            with mock.patch.object(
                MODULE.subprocess, "run", side_effect=AssertionError("local Git fallback"),
            ) as run:
                with self.assertRaisesRegex(MODULE.SourceGitError, "outside its operation-bound runtime scope"):
                    MODULE.read_blob(root, revision, "manifest.json")
                run.assert_not_called()
        self.assertTrue(MODULE._NATIVE_BROKEN)

    def test_generator_runtime_operation_scope_restores_nested_and_failed_bindings(self):
        class Reader:
            def load_module(self, name):
                return types.ModuleType(name)

        reader_a, reader_b = Reader(), Reader()
        contextvar = DISPATCH_MODULE._ACTIVE_SOURCE_RUNTIME_READER
        self.assertIsNone(contextvar.get())
        with self.assertRaisesRegex(RuntimeError, "scope sentinel"):
            with DISPATCH_MODULE._source_runtime_operation_scope(reader_a):
                self.assertIs(contextvar.get(), reader_a)
                with DISPATCH_MODULE._source_runtime_operation_scope(reader_b):
                    self.assertIs(contextvar.get(), reader_b)
                    raise RuntimeError("scope sentinel")
        self.assertIsNone(contextvar.get())
        with self.assertRaisesRegex(RuntimeError, "lacks exact load_module"):
            with DISPATCH_MODULE._source_runtime_operation_scope(object()):
                pass
        self.assertIsNone(contextvar.get())

    def test_runtime_scope_inside_history_cannot_replace_the_paired_reader(self):
        owner_name = "canonical_publication_git_owner"
        owner = types.ModuleType(owner_name)
        prior_owner = sys.modules.get(owner_name)
        sys.modules[owner_name] = owner

        class Reader:
            def load_module(self, name):
                return owner

        reader_a, reader_b = Reader(), Reader()
        history = object()
        try:
            with MODULE._history_scope(history, source_runtime_reader=reader_a):
                self.assertIs(MODULE._load_exact_module(owner_name), owner)
                with MODULE._source_runtime_scope(reader_b):
                    with self.assertRaisesRegex(MODULE.SourceGitError, "differs from the active history pair"):
                        MODULE._load_exact_module(owner_name)
                self.assertIs(MODULE._load_exact_module(owner_name), owner)
        finally:
            if prior_owner is None:
                sys.modules.pop(owner_name, None)
            else:
                sys.modules[owner_name] = prior_owner

    def test_history_scope_pairs_readers_and_restores_nested_or_failed_operations(self):
        class Reader:
            def __init__(self, label):
                self.label = label

            def load_module(self, name):
                return None

        history_a, history_b = object(), object()
        reader_a, reader_b = Reader("a"), Reader("b")
        self.assertIsNone(MODULE._ACTIVE_HISTORY.get())
        self.assertIsNone(MODULE._ACTIVE_SOURCE_RUNTIME_READER.get())
        self.assertIsNone(MODULE._ACTIVE_HISTORY_RUNTIME_READER.get())
        with MODULE._history_scope(history_a, source_runtime_reader=reader_a):
            self.assertIs(MODULE._ACTIVE_HISTORY.get(), history_a)
            self.assertIs(MODULE._ACTIVE_SOURCE_RUNTIME_READER.get(), reader_a)
            self.assertIs(MODULE._ACTIVE_HISTORY_RUNTIME_READER.get(), reader_a)
            with self.assertRaisesRegex(RuntimeError, "sentinel"):
                with MODULE._history_scope(history_b, source_runtime_reader=reader_b):
                    self.assertIs(MODULE._ACTIVE_HISTORY.get(), history_b)
                    self.assertIs(MODULE._ACTIVE_SOURCE_RUNTIME_READER.get(), reader_b)
                    self.assertIs(MODULE._ACTIVE_HISTORY_RUNTIME_READER.get(), reader_b)
                    raise RuntimeError("sentinel")
            self.assertIs(MODULE._ACTIVE_HISTORY.get(), history_a)
            self.assertIs(MODULE._ACTIVE_SOURCE_RUNTIME_READER.get(), reader_a)
            self.assertIs(MODULE._ACTIVE_HISTORY_RUNTIME_READER.get(), reader_a)
        self.assertIsNone(MODULE._ACTIVE_HISTORY.get())
        self.assertIsNone(MODULE._ACTIVE_SOURCE_RUNTIME_READER.get())
        self.assertIsNone(MODULE._ACTIVE_HISTORY_RUNTIME_READER.get())

    def test_history_scope_requires_runtime_reader_and_load_rejects_unpaired_context(self):
        with self.assertRaisesRegex(MODULE.SourceGitError, "reader is unavailable"):
            with MODULE._history_scope(object(), source_runtime_reader=object()):
                pass
        with self.assertRaisesRegex(MODULE.SourceGitError, "outside its operation-bound runtime scope"):
            MODULE._load_exact_module("canonical_publication_git_owner")

    def test_exact_native_blob_info_missing_record_preserves_optional_read_semantics(self):
        revision = "1" * 40
        expected = f"{revision}:missing.json missing\n".encode("ascii")
        result = mock.Mock(returncode=0, stdout=expected, stderr=b"", cleanup_confirmed=True)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            marker_values = {
                name: f"test-{index}"
                for index, name in enumerate(MODULE._CHILD_BOOTSTRAP_MARKERS)
            }
            with mock.patch.dict(os.environ, marker_values, clear=False):
                with mock.patch.object(MODULE, "_native_request", return_value=result):
                    self.assertEqual(MODULE.read_blob(root, revision, "missing.json", check=False), b"")
                    with self.assertRaises(subprocess.CalledProcessError) as error:
                        MODULE.read_blob(root, revision, "missing.json", check=True)
                    self.assertEqual(error.exception.returncode, 128)

                malformed = mock.Mock(
                    returncode=0,
                    stdout=f"{revision}:other.json missing\n".encode("ascii"),
                    stderr=b"",
                    cleanup_confirmed=True,
                )
                with mock.patch.object(MODULE, "_native_request", return_value=malformed):
                    with self.assertRaises(MODULE.SourceGitError):
                        MODULE.read_blob(root, revision, "missing.json", check=False)

    def test_fixture_head_read_uses_fixture_closed_verb_name(self):
        from dataclasses import dataclass

        class FixtureRepository:
            def __init__(self, root: pathlib.Path):
                self.root = root

        class FixtureVerb(Enum):
            REV_PARSE_HEAD = "rev_parse_head"

        @dataclass(frozen=True)
        class FixtureRequest:
            verb: FixtureVerb
            commit_sha: str | None = None
            path: str | None = None
            ancestor_sha: str | None = None
            descendant_sha: str | None = None

        class Result:
            returncode = 0
            stdout = b"a" * 40 + b"\n"
            stderr = b""
            cleanup_confirmed = True
            argv_trace = ("/usr/bin/git", "rev-parse", "HEAD")

        class Session:
            def __init__(self):
                self.requests = []

            def assert_fixture_root(self, root, fixture):
                if root != fixture.root:
                    raise AssertionError("fixture root mismatch")

            def run_fixture(self, fixture, request):
                self.requests.append((fixture, request))
                return Result()

        fixture = FixtureRepository(pathlib.Path("/native-fixture"))
        session = Session()
        owner = types.SimpleNamespace(
            ChildFixtureRepository=FixtureRepository,
            _ChildGitSession=Session,
            _ChildGitResult=Result,
            ClosedFixtureGitRequest=FixtureRequest,
            FixtureGitVerb=FixtureVerb,
            child_git_session_for_fixture=lambda value: session,
        )
        markers = {
            name: f"fixture-head-{index}"
            for index, name in enumerate(MODULE._CHILD_BOOTSTRAP_MARKERS)
        }
        with mock.patch.dict(os.environ, markers, clear=False):
            with mock.patch.object(MODULE, "_native_session", return_value=(owner, session, fixture)):
                self.assertEqual(MODULE.read_head_oid(fixture), "a" * 40)
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(session.requests[0][1].verb, FixtureVerb.REV_PARSE_HEAD)

    def test_large_native_blob_uses_exact_payload_verb_and_verifies_git_object(self):
        revision = "1" * 40
        payload = b"x" * 23_887_000
        oid = hashlib.sha1(b"blob 23887000\0" + payload).hexdigest()

        class Result:
            def __init__(self, stdout: bytes):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = b""
                self.cleanup_confirmed = True

        results = iter((
            Result(f"{oid} blob {len(payload)}\n".encode("ascii")),
            Result(payload),
        ))
        calls: list[tuple[str, dict[str, object]]] = []

        def request(_root: object, verb: str, **kwargs: object) -> Result:
            calls.append((verb, kwargs))
            return next(results)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            markers = {
                name: f"large-{index}"
                for index, name in enumerate(MODULE._CHILD_BOOTSTRAP_MARKERS)
            }
            with mock.patch.dict(os.environ, markers, clear=False):
                with mock.patch.object(MODULE, "_native_request", side_effect=request):
                    self.assertEqual(MODULE.read_blob(root, revision, "large.bin"), payload)
        self.assertEqual([verb for verb, _kwargs in calls], ["show_blob_info", "show_blob_payload"])
        self.assertTrue(all(kwargs == {"commit_sha": revision, "path": "large.bin"} for _, kwargs in calls))

    def test_native_blob_payload_length_and_object_id_must_match_declared_info(self):
        revision = "1" * 40

        class Result:
            def __init__(self, stdout: bytes):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = b""
                self.cleanup_confirmed = True

        declared_payload = b"x" * (2 * 1024 * 1024 + 1)
        wrong_payload = declared_payload[:-1]
        oid = hashlib.sha1(
            b"blob " + str(len(declared_payload)).encode("ascii") + b"\0" + declared_payload,
        ).hexdigest()
        results = iter((
            Result(f"{oid} blob {len(declared_payload)}\n".encode("ascii")),
            Result(wrong_payload),
        ))
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            markers = {
                name: f"mismatch-{index}"
                for index, name in enumerate(MODULE._CHILD_BOOTSTRAP_MARKERS)
            }
            with mock.patch.dict(os.environ, markers, clear=False):
                with mock.patch.object(MODULE, "_native_request", side_effect=lambda *_args, **_kwargs: next(results)):
                    with self.assertRaisesRegex(MODULE.SourceGitError, "payload length differs"):
                        MODULE.read_blob(root, revision, "large.bin")

    def test_native_ancestry_failure_preserves_check_false_only_for_git_exit(self):
        revision = "1" * 40
        result = mock.Mock(
            returncode=128, stdout=b"", stderr=b"fatal: bad object\n",
            cleanup_confirmed=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            marker_values = {
                name: f"test-{index}"
                for index, name in enumerate(MODULE._CHILD_BOOTSTRAP_MARKERS)
            }
            class RuntimeReader:
                def load_module(self, name):
                    raise AssertionError(f"unexpected owner import in this typed-request seam: {name}")

            with DISPATCH_MODULE._source_runtime_operation_scope(RuntimeReader()):
                with mock.patch.dict(os.environ, marker_values, clear=False):
                    with mock.patch.object(DISPATCH_MODULE, "_load_canonical_source_git", return_value=MODULE):
                        with mock.patch.object(MODULE, "_native_request", return_value=result):
                            self.assertEqual(
                                DISPATCH_MODULE.git_read_only(
                                    root, ["merge-base", "--is-ancestor", revision, "2" * 40],
                                    check=False,
                                ),
                                b"",
                            )
                            with self.assertRaisesRegex(ValueError, "local git evidence check failed"):
                                DISPATCH_MODULE.git_read_only(
                                    root, ["merge-base", "--is-ancestor", revision, "2" * 40],
                                    check=True,
                                )
            self.assertIsNone(DISPATCH_MODULE._ACTIVE_SOURCE_RUNTIME_READER.get())


    def test_fixture_cap_uses_fixture_accessor_and_only_closed_read_verbs(self):
        payload = b"fixture read\n"
        oid = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()

        class FixtureGitVerb(Enum):
            SHOW_BLOB_INFO = "show_blob_info"
            SHOW_BLOB = "show_blob"

        @dataclass(frozen=True)
        class ClosedFixtureGitRequest:
            verb: FixtureGitVerb
            commit_sha: str | None = None
            path: str | None = None
            ancestor_sha: str | None = None
            descendant_sha: str | None = None

        @dataclass(frozen=True)
        class ChildFixtureRepository:
            root: pathlib.Path

        @dataclass(frozen=True)
        class ChildResult:
            returncode: int
            stdout: bytes
            stderr: bytes
            cleanup_confirmed: bool = True
            argv_trace: tuple[str, ...] = ("git", "show")

        calls = []

        class Session:
            cleanup_mode = False

            def assert_fixture_root(self, root, cap):
                self.assertion = (root, cap)

            def run_fixture(self, cap, request):
                calls.append((cap, request))
                if self.cleanup_mode:
                    return ChildResult(128, b"", b"child cleanup uncertain\n", False, ("git", "show"))
                if request.verb is FixtureGitVerb.SHOW_BLOB_INFO:
                    return ChildResult(0, f"{oid} blob {len(payload)}\n".encode(), b"")
                if request.verb is FixtureGitVerb.SHOW_BLOB:
                    return ChildResult(0, payload, b"")
                raise AssertionError(f"unexpected fixture verb: {request.verb}")

        session = Session()
        owner = types.ModuleType(MODULE._BROKER_MODULE_NAME)
        owner.ChildFixtureRepository = ChildFixtureRepository
        owner._ChildGitSession = Session
        owner._ChildGitResult = ChildResult
        owner.FixtureGitVerb = FixtureGitVerb
        owner.ClosedFixtureGitRequest = ClosedFixtureGitRequest
        owner.child_git_session_for_fixture = mock.Mock(return_value=session)
        owner.child_git_session_for_source = mock.Mock(side_effect=AssertionError("fixture routed as Source"))
        cap = ChildFixtureRepository(pathlib.Path("/issued/fixture"))
        marker_values = {name: f"marker-{index}" for index, name in enumerate(MODULE._CHILD_BOOTSTRAP_MARKERS)}
        old_module = sys.modules.get(MODULE._BROKER_MODULE_NAME)
        try:
            sys.modules[MODULE._BROKER_MODULE_NAME] = owner
            with mock.patch.dict(os.environ, marker_values, clear=False):
                with mock.patch.object(MODULE, "_load_native_owner", return_value=owner):
                    self.assertEqual(MODULE.read_blob(cap, "1" * 40, "fixture.txt"), payload)
                    self.assertEqual(owner.child_git_session_for_fixture.call_count, 2)
                    self.assertTrue(all(call.args == (cap,) for call in owner.child_git_session_for_fixture.call_args_list))
                    owner.child_git_session_for_source.assert_not_called()
                    self.assertEqual([item[1].verb for item in calls], [
                        FixtureGitVerb.SHOW_BLOB_INFO, FixtureGitVerb.SHOW_BLOB,
                    ])
                    self.assertTrue(all(item[1].commit_sha == "1" * 40 for item in calls))
                    self.assertTrue(all(item[1].path == "fixture.txt" for item in calls))
                    session.cleanup_mode = True
                    with self.assertRaisesRegex(MODULE.SourceGitError, "unverified child result"):
                        MODULE.read_blob(cap, "1" * 40, "fixture.txt")
        finally:
            if old_module is None:
                sys.modules.pop(MODULE._BROKER_MODULE_NAME, None)
            else:
                sys.modules[MODULE._BROKER_MODULE_NAME] = old_module

    def test_native_source_reads_reuse_exact_typed_request_surface(self):
        revision = "1" * 40
        main = "2" * 40
        tree = "3" * 40
        payload = b"bounded source blob\n"
        oid = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()

        @dataclass(frozen=True)
        class ClosedLocalGitRequest:
            verb: str
            commit_sha: str | None = None
            path: str | None = None
            ancestor_sha: str | None = None
            descendant_sha: str | None = None

        class ChildResult:
            def __init__(self, returncode=0, stdout=b"", stderr=b"", cleanup_confirmed=True):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr
                self.cleanup_confirmed = cleanup_confirmed

        class Session:
            def __init__(self):
                self.requests = []

            def assert_source_root(self, root):
                self.root = root

            def run_git(self, request):
                self.requests.append(request)
                if request.verb == "show_blob_info":
                    return ChildResult(0, f"{oid} blob {len(payload)}\n".encode("ascii"))
                if request.verb == "show_blob":
                    return ChildResult(0, payload)
                if request.verb == "rev_parse_tree":
                    return ChildResult(0, f"{tree}\n".encode("ascii"))
                if request.verb == "rev_parse_commit_head":
                    return ChildResult(0, f"{revision}\n".encode("ascii"))
                if request.verb == "trusted_origin_main":
                    return ChildResult(0, f"{main}\n".encode("ascii"))
                if request.verb == "is_ancestor":
                    return ChildResult(1)
                raise AssertionError(f"unexpected typed Native Git request: {request.verb}")

        class Owner:
            _ClosedLocalGitRequest = ClosedLocalGitRequest
            ChildFixtureRepository = type("ChildFixtureRepository", (), {})
            _ChildGitSession = Session
            _ChildGitResult = ChildResult

            def __init__(self):
                self.session = Session()
                self.session_gets = 0

            def child_git_session_for_source(self, root):
                self.session_gets += 1
                return self.session

        root = pathlib.Path("/prepared/source")
        owner = Owner()
        markers = {name: f"marker-{index}" for index, name in enumerate(MODULE._CHILD_BOOTSTRAP_MARKERS)}
        old_module = sys.modules.get(MODULE._BROKER_MODULE_NAME)
        try:
            sys.modules[MODULE._BROKER_MODULE_NAME] = owner
            with mock.patch.dict(os.environ, markers, clear=False):
                with mock.patch.object(MODULE, "_load_native_owner", return_value=owner):
                    self.assertEqual(MODULE.read_blob(root, revision, "fixture.txt"), payload)
                    self.assertEqual(MODULE.read_blob_oid(root, revision, "fixture.txt"), oid)
                    self.assertEqual(MODULE.read_tree_oid(root, revision), tree)
                    self.assertEqual(MODULE.read_head_oid(root), revision)
                    self.assertEqual(MODULE.trusted_origin_main(root), main)
                    self.assertFalse(MODULE.is_ancestor(root, revision, main))
            self.assertEqual(
                [request.verb for request in owner.session.requests],
                [
                    "show_blob_info", "show_blob", "show_blob_info", "rev_parse_tree",
                    "rev_parse_commit_head", "trusted_origin_main", "is_ancestor",
                ],
            )
            self.assertTrue(all(request.commit_sha == revision for request in owner.session.requests[:4]))
            self.assertEqual(owner.session.requests[-1].ancestor_sha, revision)
            self.assertEqual(owner.session.requests[-1].descendant_sha, main)
            self.assertEqual(owner.session_gets, len(owner.session.requests))

            prior_request_count = len(owner.session.requests)
            prior_get_count = owner.session_gets
            os.environ[MODULE._CHILD_BOOTSTRAP_MARKERS[0]] = "changed-after-HELLO"
            with mock.patch.object(MODULE.subprocess, "run") as run:
                with self.assertRaisesRegex(MODULE.SourceGitError, "bootstrap changed"):
                    MODULE.read_tree_oid(root, revision)
                run.assert_not_called()
            self.assertEqual(len(owner.session.requests), prior_request_count)
            self.assertEqual(owner.session_gets, prior_get_count)
        finally:
            if old_module is None:
                sys.modules.pop(MODULE._BROKER_MODULE_NAME, None)
            else:
                sys.modules[MODULE._BROKER_MODULE_NAME] = old_module

    def test_native_check14_source_metadata_uses_only_source_capability_and_preserves_empty(self):
        @dataclass(frozen=True)
        class ChildSourceRepository:
            root: pathlib.Path

        @dataclass(frozen=True)
        class ClosedLocalGitRequest:
            verb: str
            commit_sha: str | None = None
            path: str | None = None
            ancestor_sha: str | None = None
            descendant_sha: str | None = None

        class ChildResult:
            def __init__(self, stdout=b"", *, metadata_kind=None, metadata_present=False, metadata_bytes=b""):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = b""
                self.cleanup_confirmed = True
                self.metadata_kind = metadata_kind
                self.metadata_present = metadata_present
                self.metadata_bytes = metadata_bytes

        with tempfile.TemporaryDirectory() as directory:
            source_root = pathlib.Path(directory).resolve()
            git_dir = source_root / ".git"
            git_dir.mkdir()
            shallow_path = git_dir / "shallow"
            source_cap = ChildSourceRepository(source_root)
            results = {
                "source_is_shallow": ChildResult(b"true\n"),
                "source_git_dir": ChildResult(str(git_dir).encode() + b"\n"),
                "source_common_dir": ChildResult(str(git_dir).encode() + b"\n"),
                "source_shallow_path": ChildResult(str(shallow_path).encode() + b"\n"),
                "source_shallow_bytes": ChildResult(
                    str(shallow_path).encode() + b"\n",
                    metadata_kind="source_shallow", metadata_present=True,
                    metadata_bytes=b"",
                ),
                "source_validate_ref_graph": ChildResult(b"1" * 40 + b"\n"),
            }

            class Session:
                def __init__(self):
                    self.requests = []

                def source_repository(self):
                    return source_cap

                def run_source_git(self, capability, request):
                    self.requests.append((capability, request))
                    return results[request.verb]

            session = Session()
            owner = types.ModuleType(MODULE._BROKER_MODULE_NAME)
            owner.ChildSourceRepository = ChildSourceRepository
            owner.ClosedLocalGitRequest = ClosedLocalGitRequest
            owner._ChildGitResult = ChildResult
            old_module = sys.modules.get(MODULE._BROKER_MODULE_NAME)
            marker_values = {name: f"metadata-{index}" for index, name in enumerate(MODULE._CHILD_BOOTSTRAP_MARKERS)}
            try:
                sys.modules[MODULE._BROKER_MODULE_NAME] = owner
                with mock.patch.dict(os.environ, marker_values, clear=False):
                    with mock.patch.object(MODULE, "_native_session", return_value=(owner, session, None)):
                        self.assertTrue(MODULE.source_is_shallow(source_root))
                        self.assertEqual(MODULE.source_git_dir(source_root), git_dir)
                        self.assertEqual(MODULE.source_common_dir(source_root), git_dir)
                        self.assertEqual(
                            MODULE.source_shallow_path(source_root, common_dir=git_dir),
                            shallow_path,
                        )
                        # Presence and empty contents are distinct facts; the
                        # CHECK14 caller remains responsible for rejecting true+empty.
                        self.assertEqual(
                            MODULE.source_shallow_bytes(source_root, shallow_path=shallow_path),
                            b"",
                        )
                        self.assertEqual(
                            MODULE.source_validate_ref_graph(source_root), b"1" * 40 + b"\n",
                        )
                self.assertEqual([request.verb for _cap, request in session.requests], list(results))
                self.assertTrue(all(capability is source_cap for capability, _ in session.requests))
                self.assertTrue(all(type(request) is ClosedLocalGitRequest for _, request in session.requests))
                self.assertTrue(all(request.commit_sha is None and request.path is None for _, request in session.requests))
            finally:
                if old_module is None:
                    sys.modules.pop(MODULE._BROKER_MODULE_NAME, None)
                else:
                    sys.modules[MODULE._BROKER_MODULE_NAME] = old_module

    def test_native_path_cannot_be_reinterpreted_as_fixture_root_or_fallback(self):
        class ChildFixtureRepository:
            pass

        class ChildResult:
            pass

        class Session:
            def assert_source_root(self, root):
                raise MODULE.SourceGitError("unregistered source root")

        owner = types.ModuleType(MODULE._BROKER_MODULE_NAME)
        owner.ChildFixtureRepository = ChildFixtureRepository
        owner._ChildGitSession = Session
        owner._ChildGitResult = ChildResult
        owner.child_git_session_for_source = mock.Mock(return_value=Session())
        owner.child_git_session_for_fixture = mock.Mock(side_effect=AssertionError("path auto-registered"))
        old_module = sys.modules.get(MODULE._BROKER_MODULE_NAME)
        markers = {name: f"marker-{index}" for index, name in enumerate(MODULE._CHILD_BOOTSTRAP_MARKERS)}
        try:
            sys.modules[MODULE._BROKER_MODULE_NAME] = owner
            with mock.patch.dict(os.environ, markers, clear=False):
                with mock.patch.object(MODULE, "_load_native_owner", return_value=owner):
                    with mock.patch.object(MODULE.subprocess, "run") as run:
                        with self.assertRaises(MODULE.SourceGitError):
                            MODULE.read_blob(pathlib.Path("/unregistered"), "1" * 40, "fixture.txt")
                        run.assert_not_called()
            owner.child_git_session_for_source.assert_called_once()
            owner.child_git_session_for_fixture.assert_not_called()
        finally:
            if old_module is None:
                sys.modules.pop(MODULE._BROKER_MODULE_NAME, None)
            else:
                sys.modules[MODULE._BROKER_MODULE_NAME] = old_module


if __name__ == "__main__":
    unittest.main()
