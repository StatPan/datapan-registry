from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "generate-completeness-proof-rollup.py"
SPEC = importlib.util.spec_from_file_location("producer_history_rollup", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PROMOTION_SPEC = importlib.util.spec_from_file_location(
    "producer_history_promotion", ROOT / "scripts" / "run-canonical-update-promotion.py",
)
assert PROMOTION_SPEC and PROMOTION_SPEC.loader
PROMOTION = importlib.util.module_from_spec(PROMOTION_SPEC)
PROMOTION_SPEC.loader.exec_module(PROMOTION)


class _Reader:
    def __init__(
        self,
        blobs: dict[str, bytes],
        *,
        is_ancestor: bool = True,
        tree_oid: str = "b" * 40,
    ) -> None:
        self.blobs = blobs
        self.is_ancestor_result = is_ancestor
        self.tree_oid = tree_oid
        self.calls: list[tuple[str, dict[str, object]]] = []

    def read_blob(self, request: dict[str, object]) -> bytes:
        self.calls.append(("read_blob", request))
        return self.blobs[request["path"]]

    def is_ancestor(self, request: dict[str, object]) -> bool:
        self.calls.append(("is_ancestor", request))
        return self.is_ancestor_result

    def read_tree_oid(self, request: dict[str, object]) -> str:
        self.calls.append(("read_tree_oid", request))
        return self.tree_oid


class _NoCurrentInputRead:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def read_bytes(self, relative_path: str) -> bytes:
        raise AssertionError(f"unexpected current-M file read: {relative_path}")

    def list_paths(self, *, prefix: str, suffix: str) -> tuple[str, ...]:
        raise AssertionError(f"unexpected current-M path listing: {prefix}*{suffix}")


class _CurrentInputRead:
    def __init__(self, root: pathlib.Path, blobs: dict[str, bytes]) -> None:
        self.root = root
        self.blobs = blobs
        self.calls: list[str] = []

    def read_bytes(self, relative_path: str) -> bytes:
        self.calls.append(relative_path)
        return self.blobs[relative_path]

    def list_paths(self, *, prefix: str, suffix: str) -> tuple[str, ...]:
        return tuple(sorted(path for path in self.blobs if path.startswith(prefix) and path.endswith(suffix)))


class _SourceRuntimeRead:
    """Test-only exact singleton reader for the separate #768 CONTROL domain."""

    def __init__(self, module: types.ModuleType) -> None:
        if module.__name__ != "canonical_source_git":
            raise ValueError("test source runtime accepts only the canonical_source_git singleton")
        self.module = module

    def load_module(self, name: str) -> types.ModuleType:
        if name != "canonical_source_git":
            raise ValueError("test source runtime request is outside its one-module fixture")
        return self.module


class _SourceControlRead:
    """Test-only loader for the fixed Source executable/baseline pair API."""

    def __init__(
        self,
        current_input_reader: object,
        *,
        source_runtime_reader: object | None = None,
        source_bytes: dict[str, bytes] | None = None,
    ) -> None:
        self.current_input_reader = current_input_reader
        self.source_runtime_reader = source_runtime_reader
        self.source_bytes = source_bytes or {
            path: (ROOT / path).read_bytes()
            for path in MODULE.CURRENT_INPUT_CONTROL_MEMBER_PATHS
        }
        self.calls: list[tuple[str, str]] = []
        self.modules: dict[str, types.ModuleType] = {}
        self._module_prefix = f"_test_source_control_{id(self)}"

    def load_module(self, path: str, *, m_baseline_bytes: bytes) -> types.ModuleType:
        if path not in MODULE.CURRENT_INPUT_CONTROL_MEMBER_PATHS:
            raise ValueError("test source control request is outside the fixed mapping")
        if type(m_baseline_bytes) is not bytes:
            raise TypeError("test baseline must be exact bytes")
        self.calls.append((path, hashlib.sha256(m_baseline_bytes).hexdigest()))
        if path in self.modules:
            return self.modules[path]
        source = self.source_bytes[path]
        module_name = f"{self._module_prefix}_{hashlib.sha256(path.encode()).hexdigest()[:12]}"
        module = types.ModuleType(module_name)
        module.__file__ = str(pathlib.Path(self.current_input_reader.root) / path)
        module.__datapan_source_control_origin__ = "datapan.source-control.v1"
        module._SOURCE_CONTROL_ORIGIN = True
        sys.modules[module_name] = module
        self.modules[path] = module
        try:
            exec(compile(source, module.__file__, "exec"), module.__dict__)
        except BaseException:
            self.modules.pop(path, None)
            if sys.modules.get(module_name) is module:
                del sys.modules[module_name]
            raise
        return module


class ProducerHistoryEnumerationTests(unittest.TestCase):
    def _current_requirement_inputs(self) -> tuple[dict[str, bytes], tuple[str, ...], dict[str, bytes]]:
        bootstrap = {
            path: (ROOT / path).read_bytes()
            for path in MODULE.CURRENT_INPUT_REQUIREMENT_BOOTSTRAP_PATHS
        }
        source_registry = json.loads(bootstrap[MODULE.SOURCE_REGISTRY_PATH])
        inventory = tuple(sorted(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "sources").glob("*.json")
        ))
        profiles = {path: (ROOT / path).read_bytes() for path in inventory}
        return bootstrap, inventory, profiles

    def _source_control_reader(
        self,
        current_input_reader: object,
        *,
        source_runtime_reader: object | None = None,
        source_bytes: dict[str, bytes] | None = None,
    ) -> _SourceControlRead:
        return _SourceControlRead(
            current_input_reader,
            source_runtime_reader=source_runtime_reader,
            source_bytes=source_bytes,
        )

    def test_current_member_resolver_emits_exact_index_profile_and_control_rows_without_filesystem_fallback(self) -> None:
        bootstrap, inventory, profiles = self._current_requirement_inputs()
        index = json.loads(bootstrap[MODULE.INPUT_INDEX_PATH])
        scope_registry = json.loads(bootstrap[MODULE.SCOPE_REGISTRY_PATH])
        source_registry = json.loads(bootstrap[MODULE.SOURCE_REGISTRY_PATH])
        logical_root = pathlib.Path("/__authenticated_current_m_view__/root")
        reader_bytes = {**bootstrap, **profiles}
        for path in MODULE.CURRENT_INPUT_CONTROL_MEMBER_PATHS:
            reader_bytes.setdefault(path, (ROOT / path).read_bytes())
        current_input_reader = _CurrentInputRead(logical_root, reader_bytes)
        source_control_reader = self._source_control_reader(current_input_reader)
        with mock.patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("resolver used host Path.read_bytes")), \
             mock.patch.object(pathlib.Path, "glob", side_effect=AssertionError("resolver used host Path.glob")):
            rows = MODULE.enumerate_current_input_requirements(
                root=logical_root,
                current_main_sha="2693e4de28e601555b998a457816f2ad18321699",
                bootstrap_bytes=bootstrap,
                source_profile_inventory=inventory,
                source_profile_bytes=profiles,
                source_control_reader=source_control_reader,
            )

        by_domain_path = {(row["domain"], row["path"]): row for row in rows}
        self.assertEqual(len(rows), len(by_domain_path))
        expected_repository_paths = set(MODULE.CURRENT_INPUT_REQUIREMENT_BOOTSTRAP_PATHS)
        expected_repository_paths.update(path for path, _rule_id, _callsite in MODULE.CURRENT_INPUT_CONTROL_MEMBER_RULES)
        expected_repository_paths.update(MODULE.CURRENT_INPUT_CONTROL_DATA_PATHS)
        expected_repository_paths.update(row["profile"] for row in source_registry["supported_sources"])
        expected_repository_paths.update(inventory)
        expected_repository_paths.update(
            item["path"] for item in index["inputs"] if item["root"] == "repository"
        )
        self.assertEqual(
            expected_repository_paths,
            {path for domain, path in by_domain_path if domain == "m_repository"},
        )
        self.assertEqual(
            {path for item in index["inputs"] if item["root"] == "evidence" for path in [item["path"]]},
            {path for domain, path in by_domain_path if domain == "external_evidence"},
        )

        index_rows_by_position = index["inputs"]
        for position, item in enumerate(index_rows_by_position):
            domain = "m_repository" if item["root"] == "repository" else "external_evidence"
            row = by_domain_path[(domain, item["path"])]
            provenance = [
                proof for proof in row["provenance"]
                if proof["source_kind"] == "input_index" and proof["source_row_id"] == item["input_id"]
            ]
            self.assertEqual(1, len(provenance))
            self.assertEqual(("inputs", position), provenance[0]["field_path"])
            self.assertEqual(
                MODULE._canonical_history_json_sha256(item),
                provenance[0]["row_sha256"],
            )

        registered_profile_paths = {row["profile"] for row in source_registry["supported_sources"]}
        self.assertEqual(set(inventory), set(profiles))
        profile_rows = {
            path for (domain, path), row in by_domain_path.items()
            if domain == "m_repository"
            and any(proof["source_kind"] == "sustainable_source_registry" for proof in row["provenance"])
        }
        self.assertEqual(registered_profile_paths, profile_rows)
        self.assertEqual(set(inventory), {path for path in inventory if ("m_repository", path) in by_domain_path})
        self.assertEqual(2, len(MODULE.CURRENT_INPUT_SOURCE_RUNTIME_RULES))
        self.assertEqual(
            {path for path, _rule_id, _callsite in MODULE.CURRENT_INPUT_SOURCE_RUNTIME_RULES},
            {path for domain, path in by_domain_path if domain == "source_runtime"},
        )
        for runtime_path, _rule_id, _callsite in MODULE.CURRENT_INPUT_SOURCE_RUNTIME_RULES:
            self.assertNotIn(("m_repository", runtime_path), by_domain_path)

        for path, _rule_id, _callsite in MODULE.CURRENT_INPUT_CONTROL_MEMBER_RULES:
            control = by_domain_path[("m_repository", path)]
            self.assertTrue(any(proof["source_kind"] == "current_source_control" for proof in control["provenance"]))
        self.assertEqual(scope_registry["schema_version"], "datapan.completeness-proof-scopes.v1")

    def test_current_member_resolver_rejects_incomplete_source_inventory_and_profile_bytes(self) -> None:
        bootstrap, inventory, profiles = self._current_requirement_inputs()
        arguments = {
            "root": pathlib.Path("/__authenticated_current_m_view__/root"),
            "current_main_sha": "2693e4de28e601555b998a457816f2ad18321699",
            "bootstrap_bytes": bootstrap,
            "source_profile_inventory": inventory,
            "source_profile_bytes": profiles,
        }
        reader_bytes = {**bootstrap, **profiles}
        for path in MODULE.CURRENT_INPUT_CONTROL_MEMBER_PATHS:
            reader_bytes.setdefault(path, (ROOT / path).read_bytes())
        current_input_reader = _CurrentInputRead(arguments["root"], reader_bytes)
        arguments["source_control_reader"] = self._source_control_reader(current_input_reader)
        with self.assertRaisesRegex(ValueError, "complete current-M sources inventory"):
            MODULE.enumerate_current_input_requirements(
                **{**arguments, "source_profile_inventory": inventory[:-1]},
            )
        missing_profile = dict(profiles)
        missing_profile.pop(next(iter(missing_profile)))
        with self.assertRaisesRegex(ValueError, "complete current-M sources inventory"):
            MODULE.enumerate_current_input_requirements(
                **{**arguments, "source_profile_bytes": missing_profile},
            )

    def test_current_input_reader_is_required_for_historical_enumeration(self) -> None:
        with self.assertRaisesRegex(TypeError, "current_input_reader"):
            MODULE.enumerate_historical_dependencies(  # type: ignore[call-arg]
                root=ROOT,
                current_main_sha="b" * 40,
                history_reader=object(),
            )

    def test_current_file_helpers_use_root_relative_reader_without_filesystem_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="producer-history-current-reader-") as temporary:
            root = pathlib.Path(temporary).resolve()
            reader = _CurrentInputRead(root, {"policy/example.json": b'{"valid":true}'})
            session = MODULE._CurrentInputReadSession(root, reader)
            token = MODULE._ACTIVE_CURRENT_INPUT_READ_SESSION.set(session)
            try:
                self.assertEqual(b'{"valid":true}', MODULE.read_bytes(root / "policy/example.json"))
                self.assertEqual({"valid": True}, MODULE.object_at(root / "policy/example.json", "example"))
                with self.assertRaisesRegex(ValueError, "outside its authenticated root"):
                    MODULE.read_bytes(root.parent / "unlisted.json")
            finally:
                MODULE._ACTIVE_CURRENT_INPUT_READ_SESSION.reset(token)
            self.assertEqual(
                ["policy/example.json", "policy/example.json"],
                reader.calls,
                "repeated current-M reads must stay visible to the owner ledger",
            )

    def test_current_module_import_loads_source_control_bytes_not_m_baseline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="producer-history-current-module-") as temporary:
            root = pathlib.Path(temporary).resolve()
            relative = "scripts/validate-completeness-proof.py"
            module_path = root / relative
            baseline = b"M baseline bytes are authenticated but never compiled\n"
            reader = _CurrentInputRead(root, {relative: baseline})
            control = self._source_control_reader(
                reader,
                source_bytes={relative: b"VALUE = 42\n"},
            )
            current_token = MODULE._ACTIVE_CURRENT_INPUT_READ_SESSION.set(
                MODULE._CurrentInputReadSession(root, reader),
            )
            control_token = MODULE._ACTIVE_SOURCE_CONTROL_READER.set(control)
            try:
                module = MODULE.import_module("control_member_test", module_path)
            finally:
                MODULE._ACTIVE_SOURCE_CONTROL_READER.reset(control_token)
                MODULE._ACTIVE_CURRENT_INPUT_READ_SESSION.reset(current_token)
            self.assertEqual(42, module.VALUE)
            self.assertEqual([relative], reader.calls)
            self.assertEqual([(relative, hashlib.sha256(baseline).hexdigest())], control.calls)

    def test_all_fixed_control_members_import_without_host_file_or_git_access(self) -> None:
        paths = MODULE.CURRENT_INPUT_CONTROL_MEMBER_PATHS
        blobs = {path: (ROOT / path).read_bytes() for path in paths}
        reader = _CurrentInputRead(ROOT, blobs)
        control = self._source_control_reader(reader, source_bytes=blobs)
        current_token = MODULE._ACTIVE_CURRENT_INPUT_READ_SESSION.set(
            MODULE._CurrentInputReadSession(ROOT, reader),
        )
        control_token = MODULE._ACTIVE_SOURCE_CONTROL_READER.set(control)
        loaded = []
        try:
            with mock.patch("pathlib.Path.read_bytes", side_effect=AssertionError("CONTROL import read host file")), \
                 mock.patch("pathlib.Path.read_text", side_effect=AssertionError("CONTROL import read host file")), \
                 mock.patch("pathlib.Path.open", side_effect=AssertionError("CONTROL import opened host file")), \
                 mock.patch("subprocess.run", side_effect=AssertionError("CONTROL import launched Git")):
                for position, relative in enumerate(paths):
                    module = MODULE.import_module(
                        f"fixed_control_import_{position}", ROOT / relative,
                    )
                    loaded.append(module)
        finally:
            MODULE._ACTIVE_SOURCE_CONTROL_READER.reset(control_token)
            MODULE._ACTIVE_CURRENT_INPUT_READ_SESSION.reset(current_token)
        self.assertEqual(paths, tuple(path for path, _ in control.calls))
        self.assertEqual(13, len(loaded))
        for module in loaded:
            self.assertIs(module._SOURCE_CONTROL_ORIGIN, True)
            for forbidden in (
                "CURRENT_SOURCE_CONTROL_READER", "CURRENT_INPUT_READER", "CURRENT_SOURCE_RUNTIME_READER",
            ):
                self.assertNotIn(forbidden, module.__dict__)

    def test_canonical_source_git_comes_from_source_runtime_and_rejects_stale_singleton(self) -> None:
        source_path = SCRIPT.parent / "canonical_source_git.py"
        source_bytes = source_path.read_bytes()
        with tempfile.TemporaryDirectory(prefix="producer-history-canonical-source-") as temporary:
            root = pathlib.Path(temporary).resolve()
            reader = _CurrentInputRead(root, {})
            session = MODULE._CurrentInputReadSession(root, reader)
            token = MODULE._ACTIVE_CURRENT_INPUT_READ_SESSION.set(session)
            old_module = sys.modules.pop("canonical_source_git", None)
            try:
                loaded = MODULE._module_from_source_bytes(
                    "canonical_source_git", source_path, source_bytes,
                )
                runtime_reader = _SourceRuntimeRead(loaded)
                runtime_token = MODULE._ACTIVE_SOURCE_RUNTIME_READER.set(runtime_reader)
                try:
                    resolved = MODULE._load_canonical_source_git()
                    self.assertIs(loaded, resolved)
                    self.assertFalse(hasattr(loaded, "CURRENT_SOURCE_RUNTIME_READER"))
                    self.assertFalse(hasattr(loaded, "CURRENT_INPUT_READER"))
                    self.assertFalse(hasattr(loaded, "_bind_current_input_reader"))
                    self.assertEqual([], reader.calls, "source_runtime bytes must not be read from the M view")
                finally:
                    MODULE._ACTIVE_SOURCE_RUNTIME_READER.reset(runtime_token)
                stale = types.ModuleType("canonical_source_git")
                stale.__file__ = str(root / "scripts/canonical_source_git.py")
                sys.modules["canonical_source_git"] = stale
                runtime_token = MODULE._ACTIVE_SOURCE_RUNTIME_READER.set(runtime_reader)
                try:
                    with self.assertRaisesRegex(RuntimeError, "exact canonical_source_git singleton"):
                        MODULE._load_canonical_source_git()
                finally:
                    MODULE._ACTIVE_SOURCE_RUNTIME_READER.reset(runtime_token)
            finally:
                sys.modules.pop("canonical_source_git", None)
                if old_module is not None:
                    sys.modules["canonical_source_git"] = old_module
                MODULE._ACTIVE_CURRENT_INPUT_READ_SESSION.reset(token)

    def test_native_current_reader_never_falls_back_to_local_source_runtime_import(self) -> None:
        with tempfile.TemporaryDirectory(prefix="producer-history-no-runtime-fallback-") as temporary:
            root = pathlib.Path(temporary).resolve()
            reader = _CurrentInputRead(root, {})
            token = MODULE._ACTIVE_CURRENT_INPUT_READ_SESSION.set(
                MODULE._CurrentInputReadSession(root, reader)
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "separate source_runtime_reader"):
                    MODULE._load_canonical_source_git()
            finally:
                MODULE._ACTIVE_CURRENT_INPUT_READ_SESSION.reset(token)
            self.assertEqual([], reader.calls)

    def test_source_runtime_module_has_no_global_current_input_reader_binding(self) -> None:
        source_path = SCRIPT.parent / "canonical_source_git.py"
        source_bytes = source_path.read_bytes()
        with tempfile.TemporaryDirectory(prefix="producer-history-reader-binding-") as temporary:
            root = pathlib.Path(temporary).resolve()
            module = MODULE._module_from_source_bytes("canonical_source_git", source_path, source_bytes)
            try:
                self.assertFalse(hasattr(module, "CURRENT_INPUT_READER"))
                self.assertFalse(hasattr(module, "_bind_current_input_reader"))
            finally:
                sys.modules.pop("canonical_source_git", None)

    def test_publication_ack_import_closure_uses_authenticated_reader_bytes(self) -> None:
        source_dir = SCRIPT.parent
        ack_raw = (source_dir / "recover-canonical-publication-ack.py").read_bytes()
        source_git_raw = (source_dir / "canonical_source_git.py").read_bytes()
        publication_raw = (source_dir / "completeness_publication_evidence.py").read_bytes()
        with tempfile.TemporaryDirectory(prefix="producer-history-ack-import-") as temporary:
            root = pathlib.Path(temporary).resolve()
            reader = _CurrentInputRead(root, {
                "scripts/recover-canonical-publication-ack.py": ack_raw,
            })
            source_git = MODULE._module_from_source_bytes(
                "canonical_source_git", source_dir / "canonical_source_git.py", source_git_raw,
            )
            runtime_reader = _SourceRuntimeRead(source_git)
            publication = MODULE._module_from_source_bytes(
                "publication_evidence_reader_test",
                root / "scripts/completeness_publication_evidence.py",
                publication_raw,
            )
            source_control_reader = self._source_control_reader(
                reader,
                source_runtime_reader=runtime_reader,
            )
            old_module = sys.modules.pop("canonical_source_git", None)
            try:
                sys.modules["canonical_source_git"] = source_git
                ack = publication._load_ack_validator(
                    root, reader, source_control_reader=source_control_reader,
                )
                loaded = ack._load_canonical_source_git(runtime_reader)
                self.assertIs(source_git, loaded)
                self.assertFalse(hasattr(source_git, "CURRENT_INPUT_READER"))
                ack._load_canonical_source_git(runtime_reader)
            finally:
                sys.modules.pop("canonical_source_git", None)
                if old_module is not None:
                    sys.modules["canonical_source_git"] = old_module
            self.assertEqual(
                ["scripts/recover-canonical-publication-ack.py"],
                reader.calls,
            )

    def test_processor_promotion_reads_schema_and_transitive_helpers_from_current_reader(self) -> None:
        script_dir = SCRIPT.parent
        paths = (
            "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
            "schemas/datapan.catalogue-enrichment-evidence.v1.schema.json",
            "schemas/datapan.specs.v1.schema.json",
            "data/provider-index.json",
            "scripts/canonical_update_pr.py",
            "scripts/compose-upstream-catalogue-candidate.py",
            "scripts/generate-batch-link-detail-registry-patches.py",
            "scripts/seoul_oa109_operation_declaration.py",
            "contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json",
            "contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json",
        )
        blobs = {relative: (ROOT / relative).read_bytes() for relative in paths}
        blobs["schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"] = b'{}'
        blobs["schemas/datapan.catalogue-enrichment-evidence.v1.schema.json"] = b'{}'
        blobs["schemas/datapan.specs.v1.schema.json"] = b'{}'
        blobs["data/provider-index.json"] = b'{"adapters":[]}'
        with tempfile.TemporaryDirectory(prefix="producer-history-promotion-reader-") as temporary:
            root = pathlib.Path(temporary).resolve()
            reader = _CurrentInputRead(root, blobs)
            control = self._source_control_reader(reader)
            schema_path = root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
            with self.assertRaisesRegex(PROMOTION.PromotionError, "checkpoint digest"):
                PROMOTION.verify_processor_checkpoint(
                    {"schema_version": PROMOTION.PROCESSOR_SCHEMA}, schema_path,
                    current_input_reader=reader,
                )
            module = PROMOTION.load_module(
                root / "scripts/compose-upstream-catalogue-candidate.py",
                "composer_reader_only_test", current_input_reader=reader,
                source_control_reader=control,
            )
            self.assertIsNone(module.LINK_DETAIL_HELPERS)
            self.assertIsNone(module.SEOUL_OPERATION_DECLARATION)
            self.assertTrue(callable(module.load_link_detail_helpers(
                reader, source_control_reader=control,
            ).validate_link_metadata))
            declaration = module.load_seoul_operation_declaration(
                reader, source_control_reader=control,
            )
            snapshot = declaration.load_historical_subject_snapshot(root, current_input_reader=reader)
            self.assertEqual("datapan.provider-operation-historical-subject-snapshot.v1", snapshot["schema_version"])
            self.assertEqual(
                {"adapters": []},
                module.load_json(root / "data/provider-index.json", current_input_reader=reader),
            )
            self.assertEqual(
                {},
                PROMOTION.load_object(
                    root / "schemas/datapan.specs.v1.schema.json",
                    current_input_reader=reader,
                ),
            )
        self.assertIn("schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json", reader.calls)
        self.assertIn("scripts/compose-upstream-catalogue-candidate.py", reader.calls)
        self.assertIn("scripts/generate-batch-link-detail-registry-patches.py", reader.calls)
        self.assertIn("scripts/seoul_oa109_operation_declaration.py", reader.calls)
        self.assertIn("contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json", reader.calls)
        self.assertIn("contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json", reader.calls)
        self.assertIn("schemas/datapan.specs.v1.schema.json", reader.calls)
        self.assertIn("data/provider-index.json", reader.calls)

    def test_processor_composer_history_read_uses_fixed_history_callback(self) -> None:
        producer_head = "d" * 40
        payload = b"authenticated historical composer source"
        calls: list[tuple[pathlib.Path, list[str]]] = []

        def read_history(root: pathlib.Path, args: list[str]) -> bytes:
            calls.append((root, args))
            return payload

        with mock.patch.object(
            PROMOTION.subprocess, "Popen",
            side_effect=AssertionError("Native enumeration must not launch nested Git"),
        ):
            observed = PROMOTION.processor_composer_source_identity(
                ROOT, producer_head, history_git_reader=read_history,
            )
        self.assertEqual(
            {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()},
            observed,
        )
        self.assertEqual(
            [(ROOT, ["show", f"{producer_head}:scripts/compose-upstream-catalogue-candidate.py"])],
            calls,
        )

    def test_operation_manifest_transitive_helper_uses_current_input_reader(self) -> None:
        script_dir = SCRIPT.parent
        with tempfile.TemporaryDirectory(prefix="producer-history-operation-generator-") as temporary:
            root = pathlib.Path(temporary).resolve()
            paths = (
                "scripts/validate-data-go-kr-operation-manifest.py",
                "scripts/generate-data-go-kr-operation-manifest.py",
                "scripts/seoul_oa109_operation_declaration.py",
                "contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json",
                "contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json",
            )
            blobs = {relative: (script_dir.parent / relative).read_bytes() for relative in paths}
            reader = _CurrentInputRead(root, blobs)
            control = self._source_control_reader(reader)
            validator_path = root / paths[0]
            validator = MODULE._module_from_source_bytes("operation_validator_reader_test", validator_path, blobs[paths[0]])
            validator.ROOT = root
            validator.GENERATOR_PATH = root / paths[1]
            generator = validator.generator_module(reader, source_control_reader=control)
            self.assertIsNone(generator.DECLARATION_HELPER)
            declaration = generator._load_declaration_helper(reader, source_control_reader=control)
            snapshot = declaration.load_historical_subject_snapshot(root, current_input_reader=reader)
            self.assertEqual("datapan.provider-operation-historical-subject-snapshot.v1", snapshot["schema_version"])
            self.assertEqual(
                (
                    paths[1], paths[2], paths[3], paths[4],
                    paths[3], paths[3],
                ),
                tuple(reader.calls),
                "the generator, declaration module, declaration document, and snapshot must be owner-read; repeated document reads remain charged",
            )

    def _run(self, reader: _Reader, *, wrong_path: bool = False) -> tuple[dict[str, object], ...]:
        current_main = "b" * 40
        source_sha = "a" * 40
        input_id = "data-go-kr.api-operations.test-source.pipeline-run"
        scope_id = "data-go-kr.api-operations"
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{'c' * 64}\nsize 1\n"
        ).encode("ascii")
        manifest = json.dumps({
            "artifacts": [{"path": "data/data-go-kr.registry.json", "sha256": "c" * 64, "bytes": 1}],
        }).encode("utf-8")
        blobs = {
            "policy/source-refresh.json": b'{"sources":[]}',
            "schemas/datapan.source-refresh-policy.v1.schema.json": b'{"type":"object"}',
            "schemas/datapan.upstream-refresh-evidence.v1.schema.json": b'{"type":"object"}',
            "schemas/datapan.catalog-diff.v1.schema.json": b'{"type":"object"}',
            "schemas/datapan.upstream-refresh-work-packet.v1.schema.json": b'{"type":"object"}',
            "manifest.json": manifest,
            "data/data-go-kr.registry.json": pointer,
        }
        reader.blobs.update(blobs)
        with tempfile.TemporaryDirectory(prefix="producer-history-source-test-") as temporary:
            root = pathlib.Path(temporary)
            run_path = root / "evidence/source-run.json"
            archive_path = root / "evidence/source.zip"
            run_path.parent.mkdir(parents=True)
            run = {"head_sha": source_sha}
            run_raw = json.dumps(run).encode("utf-8")
            archive_raw = b"test-archive"
            run_path.write_bytes(run_raw)
            archive_path.write_bytes(archive_raw)
            rows = []
            resolved: dict[str, pathlib.Path] = {}
            for role, suffix, path in (
                ("pipeline_run", "run", "evidence/source-run.json"),
                ("pipeline_jobs", "jobs", "evidence/source-jobs.json"),
                ("pipeline_artifact_metadata", "metadata", "evidence/source-artifacts.json"),
                ("pipeline_artifact_archive", "archive", "evidence/source.zip"),
            ):
                exact_id = input_id if role == "pipeline_run" else f"test-source.{suffix}"
                item = {
                    "input_id": exact_id,
                    "scope_id": scope_id,
                    "role": role,
                    "root": "repository",
                    "path": path,
                    "subject": {"stage": "source"},
                    "producer": {"revision": source_sha},
                }
                rows.append(item)
                resolved[exact_id] = root / path
            index = {"evaluation_epoch": "2026-10-01T00:00:00Z", "inputs": rows}
            scope = {"scope_id": scope_id, "source_id": "data_go_kr", "resource_kind": "api_operation_manifest"}
            scope_registry = {"scopes": [scope]}
            with (
                mock.patch.object(MODULE, "load_and_validate_registry", return_value=(scope_registry, {}, {scope_id: scope})),
                mock.patch.object(MODULE, "validate_input_index", return_value=(index, rows, resolved)),
            ):
                def validate_run(**kwargs: object):
                    MODULE.assert_main_ancestor(root, source_sha, "source workflow source commit")
                    return run, {}, datetime.now(timezone.utc), datetime.now(timezone.utc)

                def validate_source(**kwargs: object):
                    revision = source_sha
                    path = "policy/not-registered.json" if wrong_path else "policy/source-refresh.json"
                    policy = json.loads(MODULE.git_read_only(root, ["show", f"{revision}:{path}"]))
                    for label, schema_path in MODULE._SourceStageHistoryTraversal._SCHEMA_PATHS.items():
                        MODULE.validate_schema_at_revision(
                            root=root, revision=revision, relative_path=schema_path,
                            value=policy if label == "source refresh policy" else {}, label=label,
                        )
                    MODULE.source_lfs_binding(root, revision)

                # The production traversal authenticates the direct caller
                # name for every Git read. Keep the test double's code-object
                # name identical to that production callsite; changing only
                # __name__ would not change frame.f_code.co_name.
                validate_source.__code__ = validate_source.__code__.replace(
                    co_name="validate_source_observation"
                )

                with (
                    mock.patch.object(MODULE, "validate_stage_run", side_effect=validate_run),
                    mock.patch.object(MODULE, "validate_source_observation", side_effect=validate_source),
                    mock.patch.object(MODULE, "validate_pipeline_evidence", return_value=None),
                ):
                    old_module = sys.modules.pop("canonical_source_git", None)
                    try:
                        current_bytes = {
                            **blobs,
                            "evidence/source-run.json": run_raw,
                            "evidence/source.zip": archive_raw,
                        }
                        for control_path in MODULE.CURRENT_INPUT_CONTROL_MEMBER_PATHS:
                            current_bytes.setdefault(
                                control_path,
                                (ROOT / control_path).read_bytes(),
                            )
                        current_input_reader = _CurrentInputRead(root, current_bytes)
                        source_git = MODULE._module_from_source_bytes(
                            "canonical_source_git",
                            SCRIPT.parent / "canonical_source_git.py",
                            (SCRIPT.parent / "canonical_source_git.py").read_bytes(),
                        )
                        source_runtime_reader = _SourceRuntimeRead(source_git)
                        source_control_reader = self._source_control_reader(
                            current_input_reader,
                            source_runtime_reader=source_runtime_reader,
                        )
                        return MODULE.enumerate_historical_dependencies(
                            root=root, current_main_sha=current_main, history_reader=reader,
                            current_input_reader=current_input_reader,
                            source_runtime_reader=source_runtime_reader,
                            source_control_reader=source_control_reader,
                        )
                    finally:
                        sys.modules.pop("canonical_source_git", None)
                        if old_module is not None:
                            sys.modules["canonical_source_git"] = old_module

    @staticmethod
    def _request_id(request: dict[str, object]) -> str:
        body = {key: value for key, value in request.items() if key != "request_id"}
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def test_source_stage_selectors_emit_exact_ancestry_policy_schema_manifest_and_pointer_requests(self) -> None:
        reader = _Reader({})
        requests = self._run(reader)
        self.assertEqual(8, len(requests))
        self.assertEqual(["ancestry", *(["blob"] * 7)], [row["kind"] for row in requests])
        self.assertEqual("source.validate_stage_run.main_ancestor", requests[0]["callsite_id"])
        self.assertEqual(("a" * 40, "b" * 40), (requests[0]["ancestor_sha"], requests[0]["descendant_sha"]))
        policy = requests[1]
        self.assertEqual("source.validate_source_observation.policy", policy["callsite_id"])
        self.assertEqual("policy/source-refresh.json", policy["path"])
        self.assertEqual("data-go-kr.api-operations.test-source.pipeline-run", policy["input_id"])
        self.assertEqual("a" * 40, policy["commit_sha"])
        self.assertEqual("source.validate_source_observation.policy_schema", requests[2]["callsite_id"])
        self.assertEqual((policy["request_id"],), requests[2]["depends_on"])
        pointer = requests[-1]
        self.assertEqual("data/data-go-kr.registry.json", pointer["path"])
        self.assertEqual((requests[-2]["request_id"],), pointer["depends_on"])
        self.assertEqual(pointer["request_id"], self._request_id(pointer))
        self.assertEqual(8, len(reader.calls))

    def test_non_ancestor_fails_before_any_historical_blob_read(self) -> None:
        reader = _Reader({}, is_ancestor=False)
        with self.assertRaisesRegex(ValueError, "not reachable"):
            self._run(reader)
        self.assertEqual(["is_ancestor"], [kind for kind, _ in reader.calls])

    def test_unknown_path_is_terminal_and_does_not_become_another_request(self) -> None:
        reader = _Reader({})
        with self.assertRaisesRegex(ValueError, "unregistered repository path"):
            self._run(reader, wrong_path=True)
        self.assertEqual(["is_ancestor"], [kind for kind, _ in reader.calls])

    def test_ack_verify_receipt_emits_exact_manifest_ancestry_and_commit_tree_requests(self) -> None:
        source_sha, tree_sha, workflow_sha = "a" * 40, "b" * 40, "c" * 40
        payload_sha, pointer_sha = "d" * 40, "e" * 40
        manifest_raw = json.dumps({"artifacts": []}, sort_keys=True, separators=(",", ":")).encode()
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        binding = {
            "schema_version": "datapan.registry-publication-source-binding.v1",
            "status": "bound",
            "repository": "StatPan/datapan-registry",
            "event_name": "workflow_dispatch",
            "git_ref": "refs/heads/main",
            "workflow_sha": workflow_sha,
            "source_sha": source_sha,
            "source_tree_sha": tree_sha,
            "manifest_sha256": manifest_sha,
        }
        receipt = {
            "schema_version": "datapan.registry-publication-receipt.v1",
            "status": "verified",
            "source_binding": binding,
            "publication": {
                "status": "published",
                "dataset": "StatPan/datapan-registry",
                "payload_revision": payload_sha,
                "pointer_revision": pointer_sha,
            },
            "anonymous_verification": {
                "status": "verified",
                "dataset": "StatPan/datapan-registry",
                "revision": payload_sha,
            },
        }
        rows = [
            {
                "input_id": "test.source.pipeline-run",
                "scope_id": "test.scope",
                "role": "pipeline_run",
                "root": "repository",
                "path": "reports/source-run.json",
                "subject": {"stage": "source"},
                "producer": {"revision": "f" * 40},
            },
            {
                "input_id": "test.publisher.producer-run",
                "scope_id": "test.scope",
                "role": "publication_producer_run",
                "root": "repository",
                "path": "reports/publisher-run.json",
                "subject": {"stage": "publisher"},
                "producer": {"revision": workflow_sha},
            },
            {
                "input_id": "test.publisher.receipt",
                "scope_id": "test.scope",
                "role": "publication_receipt",
                "root": "repository",
                "path": "reports/publisher-receipt.json",
                "subject": {"source_revision": source_sha},
            },
            {
                "input_id": "test.publisher.binding",
                "scope_id": "test.scope",
                "role": "publication_source_binding",
                "root": "repository",
                "path": "reports/publisher-source-binding.json",
                "subject": {"source_revision": source_sha},
            },
        ]
        source_run = {"head_sha": "f" * 40}
        run = {"head_sha": workflow_sha}
        reader = _Reader({"manifest.json": manifest_raw}, tree_oid=tree_sha)
        traversal = MODULE._SourceStageHistoryTraversal(
            root=ROOT,
            current_main_sha="9" * 40,
            history_reader=reader,
            input_id=rows[0]["input_id"],
            input_path=rows[0]["path"],
            input_index_row_index=0,
            input_index_row=rows[0],
            run=source_run,
        )
        traversal.bind_current_inputs(
            input_index={"inputs": rows},
            current_inputs=rows,
            resolved_inputs={row["input_id"]: ROOT / row["path"] for row in rows},
        )
        traversal.set_publisher_run_context(run)
        traversal.set_publisher_context(run=run, receipt=receipt, binding=binding)
        source_git = MODULE._load_canonical_source_git()
        validator = MODULE.import_module(
            "native_publication_ack_validator",
            ROOT / "scripts/recover-canonical-publication-ack.py",
        )
        receipt_raw = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        binding_raw = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        class RuntimeReader:
            def load_module(self, name):
                return source_git

        runtime_reader = RuntimeReader()
        with source_git._history_scope(traversal, source_runtime_reader=runtime_reader):
            verified = validator.verify_receipt(
                receipt_raw,
                binding_raw,
                repository="StatPan/datapan-registry",
                workflow_head_sha=workflow_sha,
                root=ROOT,
            )

        self.assertEqual(source_sha, verified["source_sha"])
        self.assertEqual(["read_blob", "is_ancestor", "read_tree_oid"], [kind for kind, _ in reader.calls])
        requests = [request for _, request in reader.calls]
        self.assertEqual(
            [
                "ack.verify_receipt.source_manifest",
                "ack.verify_receipt.source_ancestor",
                "ack.verify_receipt.commit_tree",
            ],
            [request["callsite_id"] for request in requests],
        )
        self.assertEqual("test.publisher.receipt", requests[0]["input_id"])
        self.assertEqual("optional_probe", requests[0]["absence_policy"])
        receipt_row_sha = MODULE._canonical_history_json_sha256(receipt)
        self.assertIn(
            {
                "source_request_id": None,
                "path": rows[2]["path"],
                "field_path": ("source_binding", "source_sha"),
                "row_sha256": receipt_row_sha,
            },
            requests[0]["provenance"],
        )
        self.assertIn(
            {
                "source_request_id": None,
                "path": rows[3]["path"],
                "field_path": ("source_sha",),
                "row_sha256": MODULE._canonical_history_json_sha256(binding),
            },
            requests[0]["provenance"],
        )
        self.assertEqual((requests[0]["request_id"],), requests[1]["depends_on"])
        self.assertEqual(
            tuple(sorted((requests[0]["request_id"], requests[1]["request_id"]))),
            requests[2]["depends_on"],
        )
        self.assertEqual(source_sha, requests[2]["commit_sha"])
        self.assertIn(
            {
                "source_request_id": None,
                "path": rows[2]["path"],
                "field_path": ("source_binding", "source_tree_sha"),
                "row_sha256": receipt_row_sha,
            },
            requests[2]["provenance"],
        )
        for request in requests:
            self.assertEqual(request["request_id"], self._request_id(request))


if __name__ == "__main__":
    unittest.main()
