from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
NATIVE_MARKER = "DATAPAN_NATIVE_GIT_FD"


def _load_script(name: str, relative_path: str, *, source_control_reader: object | None = None):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    if source_control_reader is not None:
        module.__datapan_source_control_origin__ = "datapan.source-control.v1"
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SourceControlFallbackGuardTests(unittest.TestCase):
    def _invoke(self, key: str, module: object) -> object:
        if key == "completeness":
            return module._read_bytes(ROOT / "policy/completeness-proof.json")
        if key == "operation_manifest":
            return module._read_current_bytes(ROOT / "data/data-go-kr.registry.json", None)
        if key == "runtime_import":
            return module.validate_schema({}, ROOT / "schemas/datapan.runtime-freshness-import-attestation.v1.schema.json")
        raise AssertionError(key)

    def _load_all(self, *, source_control_reader: object | None = None) -> dict[str, object]:
        return {
            "completeness": _load_script(
                "guard_completeness_proof",
                "scripts/validate-completeness-proof.py",
                source_control_reader=source_control_reader,
            ),
            "operation_manifest": _load_script(
                "guard_operation_manifest",
                "scripts/validate-data-go-kr-operation-manifest.py",
                source_control_reader=source_control_reader,
            ),
            "runtime_import": _load_script(
                "guard_runtime_import",
                "scripts/attest-runtime-freshness-import.py",
                source_control_reader=source_control_reader,
            ),
        }

    def test_restrictive_source_control_origin_forbids_implicit_current_file_reads(self) -> None:
        modules = self._load_all(source_control_reader=object())
        for key, module in modules.items():
            with self.subTest(module=key):
                with mock.patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("host read attempted")), \
                     mock.patch.object(pathlib.Path, "read_text", side_effect=AssertionError("host read attempted")), \
                     self.assertRaisesRegex(RuntimeError, "exact current-M reader"):
                    self._invoke(key, module)

    def test_native_marker_without_reader_forbids_implicit_current_file_reads(self) -> None:
        with mock.patch.dict(os.environ, {NATIVE_MARKER: "registered"}):
            modules = self._load_all()
            for key, module in modules.items():
                with self.subTest(module=key):
                    with mock.patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("host read attempted")), \
                         mock.patch.object(pathlib.Path, "read_text", side_effect=AssertionError("host read attempted")), \
                         self.assertRaisesRegex(RuntimeError, "exact current-M reader"):
                        self._invoke(key, module)

    def test_rollup_current_reads_fail_closed_without_session_in_native_mode(self) -> None:
        path = ROOT / "scripts/generate-completeness-proof-rollup.py"
        spec = importlib.util.spec_from_file_location("guard_rollup", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        token = module._ACTIVE_SOURCE_CONTROL_READER.set(object())
        try:
            with mock.patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("host read attempted")), \
                 mock.patch.object(pathlib.Path, "open", side_effect=AssertionError("host open attempted")), \
                 self.assertRaisesRegex(RuntimeError, "exact current_input_reader"):
                module.read_bytes(ROOT / "policy/completeness-proof.json")
            with mock.patch.object(pathlib.Path, "open", side_effect=AssertionError("host open attempted")), \
                 self.assertRaisesRegex(RuntimeError, "exact current_input_reader"):
                module.sha256_file(ROOT / "policy/completeness-proof.json")
        finally:
            module._ACTIVE_SOURCE_CONTROL_READER.reset(token)

    def test_ordinary_cli_mode_keeps_its_existing_filesystem_reader(self) -> None:
        modules = self._load_all()
        for key, module in modules.items():
            with self.subTest(module=key):
                if key == "runtime_import":
                    with mock.patch.object(module, "load", return_value={}) as load:
                        module.validate_schema({}, ROOT / "schemas/datapan.runtime-freshness-import-attestation.v1.schema.json")
                    load.assert_called_once()
                else:
                    with mock.patch.object(pathlib.Path, "read_bytes", return_value=b"ordinary-cli") as read_bytes:
                        self.assertEqual(b"ordinary-cli", self._invoke(key, module))
                    read_bytes.assert_called_once()

    def test_control_imports_do_not_read_sibling_files_or_launch_git(self) -> None:
        targets = (
            ("lazy_operation_manifest_generator", "scripts/generate-data-go-kr-operation-manifest.py"),
            ("lazy_catalogue_composer", "scripts/compose-upstream-catalogue-candidate.py"),
            ("lazy_seoul_declaration", "scripts/seoul_oa109_operation_declaration.py"),
        )
        for name, relative in targets:
            with self.subTest(module=relative):
                with mock.patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("import-time read_bytes")), \
                     mock.patch.object(pathlib.Path, "read_text", side_effect=AssertionError("import-time read_text")), \
                     mock.patch.object(pathlib.Path, "open", side_effect=AssertionError("import-time open")), \
                     mock.patch("subprocess.run", side_effect=AssertionError("import-time Git")):
                    module = _load_script(
                        name, relative, source_control_reader=object(),
                    )
                self.assertFalse(hasattr(module, "CURRENT_SOURCE_CONTROL_READER"))
                if relative.endswith("generate-data-go-kr-operation-manifest.py"):
                    self.assertIsNone(module.DECLARATION_HELPER)
                elif relative.endswith("compose-upstream-catalogue-candidate.py"):
                    self.assertIsNone(module.LINK_DETAIL_HELPERS)
                    self.assertIsNone(module.SEOUL_OPERATION_DECLARATION)
                else:
                    self.assertIsNone(module.DECLARATION)


if __name__ == "__main__":
    unittest.main()
