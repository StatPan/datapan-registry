from __future__ import annotations

import importlib.util
import io
import pathlib
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "generate-completeness-proof-rollup.py"
SPEC = importlib.util.spec_from_file_location("prepared_input_scope_rollup", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _PreparedReader:
    def __init__(self, root: pathlib.Path, values: dict[str, bytes]) -> None:
        self.root = root
        self.values = values
        self.calls: list[str] = []
        self.list_calls: list[tuple[str, str]] = []
        self.errors: dict[str, BaseException] = {}

    def read_bytes(self, relative_path: str) -> bytes:
        self.calls.append(relative_path)
        error = self.errors.get(relative_path)
        if error is not None:
            raise error
        try:
            return self.values[relative_path]
        except KeyError as exc:
            raise FileNotFoundError(relative_path) from exc

    def list_paths(self, *, prefix: str, suffix: str) -> tuple[str, ...]:
        self.list_calls.append((prefix, suffix))
        return tuple(sorted(path for path in self.values if path.startswith(prefix) and path.endswith(suffix)))


class _ControlReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.module = types.ModuleType("prepared_scope_control_validator")
        self.module._SOURCE_CONTROL_ORIGIN = True
        sys.modules[self.module.__name__] = self.module

    def load_module(self, path: str, *, m_baseline_bytes: bytes) -> types.ModuleType:
        self.calls.append((path, m_baseline_bytes))
        return self.module


class PreparedInputOperationScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        sys.modules.pop("prepared_scope_control_validator", None)
        self.temp.cleanup()

    def _scope(self, reader: _PreparedReader, control: _ControlReader):
        return MODULE._current_input_operation_scope(
            root=self.root,
            current_input_reader=reader,
            source_control_reader=control,
        )

    def test_scope_routes_bytes_profiles_and_control_baseline_without_history_or_path_fallback(self) -> None:
        control_path = MODULE.CURRENT_INPUT_CONTROL_MEMBER_PATHS[0]
        reader = _PreparedReader(
            self.root,
            {
                "reports/candidate.json": b"prepared candidate bytes",
                "sources/a.json": b"profile-a",
                "sources/b.json": b"profile-b",
                control_path: b"authenticated M baseline",
            },
        )
        control = _ControlReader()
        with mock.patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("host Path.read_bytes fallback")):
            with self._scope(reader, control):
                self.assertIsNone(MODULE._ACTIVE_PRODUCER_HISTORY.get())
                self.assertEqual(
                    MODULE.read_bytes(self.root / "reports/candidate.json"),
                    b"prepared candidate bytes",
                )
                session = MODULE._current_input_read_session()
                assert session is not None
                self.assertEqual(
                    session.paths(prefix="sources/", suffix=".json"),
                    {"sources/a.json", "sources/b.json"},
                )
                loaded = MODULE._source_control_module(self.root / control_path)
                self.assertIs(loaded, control.module)
        self.assertEqual(reader.calls, ["reports/candidate.json", control_path])
        self.assertEqual(reader.list_calls, [("sources/", ".json")])
        self.assertEqual(control.calls, [(control_path, b"authenticated M baseline")])
        self.assertIsNone(MODULE._current_input_read_session())
        self.assertIsNone(MODULE._ACTIVE_SOURCE_CONTROL_READER.get())

    def test_reader_missing_member_never_falls_back_to_existing_host_file(self) -> None:
        (self.root / "evidence.json").write_bytes(b"host-only bytes")
        reader = _PreparedReader(self.root, {})
        control = _ControlReader()
        with mock.patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("host Path.read_bytes fallback")):
            with self._scope(reader, control):
                with self.assertRaises(FileNotFoundError):
                    MODULE.read_bytes(self.root / "evidence.json")

    def test_expired_scope_does_not_leave_a_reader_or_enable_native_path_fallback(self) -> None:
        reader = _PreparedReader(self.root, {"evidence.json": b"prepared"})
        control = _ControlReader()
        with mock.patch.dict("os.environ", {"DATAPAN_NATIVE_GIT_FD": "registered"}):
            with mock.patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("host Path.read_bytes fallback")):
                with self._scope(reader, control):
                    self.assertEqual(MODULE.read_bytes(self.root / "evidence.json"), b"prepared")
                self.assertIsNone(MODULE._current_input_read_session())
                self.assertIsNone(MODULE._ACTIVE_SOURCE_CONTROL_READER.get())
                with self.assertRaisesRegex(RuntimeError, "exact current_input_reader"):
                    MODULE.read_bytes(self.root / "evidence.json")

    def test_scope_rejects_wrong_root_missing_control_loader_and_history_nesting(self) -> None:
        wrong_root = _PreparedReader(self.root / "different", {})
        control = _ControlReader()
        with self.assertRaisesRegex(ValueError, "root differs"):
            with self._scope(wrong_root, control):
                self.fail("wrong-root scope entered")

        reader = _PreparedReader(self.root, {})
        with self.assertRaisesRegex(TypeError, "source_control_reader"):
            with MODULE._current_input_operation_scope(
                root=self.root,
                current_input_reader=reader,
                source_control_reader=object(),
            ):
                self.fail("reader without loader entered")

        history_token = MODULE._ACTIVE_PRODUCER_HISTORY.set(object())
        try:
            with self.assertRaisesRegex(RuntimeError, "inside producer-history scope"):
                with self._scope(reader, control):
                    self.fail("prepared scope nested in history")
        finally:
            MODULE._ACTIVE_PRODUCER_HISTORY.reset(history_token)

    def test_processor_validator_keeps_ordinary_cli_reader_optional(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(MODULE, "_current_input_read_session", return_value=None):
            self.assertIsNone(MODULE._processor_current_input_reader_argument())

    def test_processor_validator_requires_native_reader_and_forwards_exact_reader(self) -> None:
        active_contexts = (
            MODULE._ACTIVE_PRODUCER_HISTORY,
            MODULE._ACTIVE_SOURCE_RUNTIME_READER,
            MODULE._ACTIVE_SOURCE_CONTROL_READER,
        )
        for active_context in active_contexts:
            token = active_context.set(object())
            try:
                with mock.patch.dict("os.environ", {}, clear=True), \
                     mock.patch.object(MODULE, "_current_input_read_session", return_value=None):
                    with self.assertRaisesRegex(RuntimeError, "lost its exact current-input reader"):
                        MODULE._processor_current_input_reader_argument()
            finally:
                active_context.reset(token)

        for marker in MODULE._SOURCE_RUNTIME_MARKERS:
            with mock.patch.dict("os.environ", {marker: ""}, clear=True), \
                 mock.patch.object(MODULE, "_current_input_read_session", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "lost its exact current-input reader"):
                    MODULE._processor_current_input_reader_argument()

        exact_reader = object()
        session = types.SimpleNamespace(reader=exact_reader)
        with mock.patch.dict("os.environ", {MODULE._SOURCE_RUNTIME_MARKERS[0]: "registered"}, clear=True), \
             mock.patch.object(MODULE, "_current_input_read_session", return_value=session):
            self.assertIs(MODULE._processor_current_input_reader_argument(), exact_reader)

    def test_nested_scope_and_exception_restore_outer_readers(self) -> None:
        outer = _PreparedReader(self.root, {"outer.json": b"outer"})
        inner = _PreparedReader(self.root, {"inner.json": b"inner"})
        outer_control = _ControlReader()
        inner_control = _ControlReader()
        with self._scope(outer, outer_control):
            self.assertEqual(MODULE.read_bytes(self.root / "outer.json"), b"outer")
            with self.assertRaisesRegex(RuntimeError, "inner failure"):
                with self._scope(inner, inner_control):
                    self.assertEqual(MODULE.read_bytes(self.root / "inner.json"), b"inner")
                    raise RuntimeError("inner failure")
            self.assertEqual(MODULE.read_bytes(self.root / "outer.json"), b"outer")
            self.assertIs(MODULE._ACTIVE_SOURCE_CONTROL_READER.get(), outer_control)
        self.assertIsNone(MODULE._current_input_read_session())
        self.assertIsNone(MODULE._ACTIVE_SOURCE_CONTROL_READER.get())

    def _run_check(self, reader: _PreparedReader) -> tuple[int, str]:
        control = _ControlReader()
        report = {"summary": {"registered_scopes": 1}, "scopes": []}
        stderr = io.StringIO()
        with self._scope(reader, control):
            with mock.patch.object(MODULE, "build_report", return_value=report), \
                 mock.patch.object(MODULE, "render_json", return_value=b"expected-json"), \
                 mock.patch.object(MODULE, "render_markdown", return_value=b"expected-markdown"), \
                 mock.patch.object(pathlib.Path, "is_file", side_effect=AssertionError("host is_file probe")), \
                 mock.patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("host read_bytes probe")), \
                 redirect_stderr(stderr):
                result = MODULE.main([
                    "--repo-root", str(self.root),
                    "--check",
                    "--output-json", "reports/out.json",
                    "--output-markdown", "reports/out.md",
                ])
        return result, stderr.getvalue()

    def test_check_reads_both_prepared_outputs_through_current_input_reader(self) -> None:
        reader = _PreparedReader(
            self.root,
            {
                "reports/out.json": b"expected-json",
                "reports/out.md": b"expected-markdown",
            },
        )
        result, stderr = self._run_check(reader)
        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(reader.calls, ["reports/out.json", "reports/out.md"])

    def test_check_maps_only_missing_output_to_drift(self) -> None:
        missing = _PreparedReader(
            self.root,
            {
                "reports/out.json": b"expected-json",
            },
        )
        result, stderr = self._run_check(missing)
        self.assertEqual(result, 1)
        self.assertIn("generated output drift", stderr)
        self.assertIn("reports/out.md", stderr)

        denied = _PreparedReader(
            self.root,
            {
                "reports/out.json": b"expected-json",
                "reports/out.md": b"expected-markdown",
            },
        )
        denied.errors["reports/out.md"] = PermissionError("prepared view deadline expired")
        result, stderr = self._run_check(denied)
        self.assertEqual(result, 1)
        self.assertIn("prepared view deadline expired", stderr)
        self.assertNotIn("generated output drift", stderr)

    def test_check_mismatch_is_reported_from_prepared_scratch_bytes(self) -> None:
        reader = _PreparedReader(
            self.root,
            {
                "reports/out.json": b"expected-json",
                "reports/out.md": b"stale generated output",
            },
        )
        result, stderr = self._run_check(reader)
        self.assertEqual(result, 1)
        self.assertIn("generated output drift", stderr)
        self.assertIn("reports/out.md", stderr)


if __name__ == "__main__":
    unittest.main()
