from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest
from collections import Counter


ROOT = pathlib.Path(__file__).parents[1].resolve()
SCRIPT = ROOT / "scripts" / "generate-completeness-proof-rollup.py"
SPEC = importlib.util.spec_from_file_location("generate_completeness_proof_rollup_scopes", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

AS_OF = "2026-10-05T00:00:00Z"
REGISTRY_FIXTURE_FILES = (
    "policy/completeness-proof.json",
    "policy/completeness-proof-scopes.json",
    "policy/sustainable-coverage.json",
    "schemas/datapan.completeness-proof-policy.v1.schema.json",
    "schemas/datapan.completeness-proof-scopes.v1.schema.json",
    "scripts/validate-completeness-proof.py",
)


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def scope_for(registry: dict, resource_kind: str, source_id: str | None = None) -> dict:
    return next(
        scope
        for scope in registry["scopes"]
        if scope["resource_kind"] == resource_kind
        and (source_id is None or scope["source_id"] == source_id)
    )


def set_scope_selector(registry: dict, resource_kind: str, value: str) -> None:
    scope_for(registry, resource_kind)["selector"]["value"] = value


def set_scope_algorithm(registry: dict, resource_kind: str, algorithm: str) -> None:
    scope_for(registry, resource_kind)["identity_algorithm"] = algorithm


def remove_scope(registry: dict, resource_kind: str, source_id: str) -> None:
    registry["scopes"].remove(scope_for(registry, resource_kind, source_id))


def set_scope_kind(registry: dict, old_kind: str, new_kind: str) -> None:
    scope_for(registry, old_kind)["resource_kind"] = new_kind


class CompletenessScopeRegistrationTest(unittest.TestCase):
    def _temporary_registry_root(self) -> pathlib.Path:
        temporary = tempfile.TemporaryDirectory(prefix="completeness-scope-registry-")
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        for relative in REGISTRY_FIXTURE_FILES:
            source = ROOT / relative
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        for source in (ROOT / "sources").glob("*.json"):
            destination = root / "sources" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        return root

    def _scope_registry(self, root: pathlib.Path = ROOT) -> dict:
        return read_json(root / MODULE.SCOPE_REGISTRY_PATH)

    def _load_scope_map(self, root: pathlib.Path = ROOT) -> dict:
        _registry, _policy, scope_by_id = MODULE.load_and_validate_registry(root)
        return scope_by_id

    def test_registered_inventory_covers_each_kind_and_configured_profile_once(self) -> None:
        registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        scopes = registry["scopes"]
        source_by_id = MODULE.validate_registered_sources(ROOT, scopes)

        self.assertEqual(len(scope_by_id), len(scopes))
        self.assertEqual(len({scope["scope_id"] for scope in scopes}), len(scopes))
        kind_counts = Counter(scope["resource_kind"] for scope in scopes)
        self.assertEqual(set(kind_counts), MODULE.RESOURCE_KINDS)
        self.assertEqual(kind_counts["api_operation_manifest"], len(source_by_id))
        for singleton_kind in (
            "api_catalog_metadata",
            "file_dataset",
            "curated_payload_snapshot",
            "payload_rows",
        ):
            self.assertEqual(kind_counts[singleton_kind], 1)

        configured_profiles = {source["profile"] for source in source_by_id.values()}
        registered_profiles = {
            scope["source_profile"] for scope in scopes if scope["source_profile"] is not None
        }
        self.assertEqual(registered_profiles, configured_profiles)
        operation_source_counts = Counter(
            scope["source_id"]
            for scope in scopes
            if scope["resource_kind"] == "api_operation_manifest"
        )
        self.assertEqual(operation_source_counts, {source_id: 1 for source_id in source_by_id})

    def test_absent_proofs_do_not_get_synthetic_authority_or_denominators(self) -> None:
        input_index = read_json(ROOT / MODULE.INPUT_INDEX_PATH)
        proof_scopes = {
            item["scope_id"] for item in input_index["inputs"] if item["role"] == "proof_v1"
        }
        report = MODULE.build_report(
            root=ROOT,
            input_root=ROOT,
            input_index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            as_of=AS_OF,
        )

        self.assertEqual(len(report["scopes"]), report["summary"]["registered_scopes"])
        for row in report["scopes"]:
            if row["scope_id"] in proof_scopes:
                continue
            self.assertIsNone(row["proof"], row["scope_id"])
            self.assertEqual(
                row["claims"], {"complete": False, "current": False, "updated": False}
            )
            self.assertFalse(row["inventory_context"]["authoritative"])
            self.assertNotIn("denominator", row)

    def test_scope_ids_are_registry_defined_but_old_unregistered_input_refs_fail(self) -> None:
        registry = self._scope_registry()
        renamed = copy.deepcopy(registry["scopes"])
        catalog = next(scope for scope in renamed if scope["resource_kind"] == "api_catalog_metadata")
        old_scope_id = catalog["scope_id"]
        catalog["scope_id"] = f"{old_scope_id}-reviewed-alias"

        # A consistent registry identity rename remains possible; the validator must not
        # turn today's inventory labels into a permanent hardcoded allowlist.
        MODULE.validate_registered_sources(ROOT, renamed)
        renamed_catalog = next(
            scope for scope in renamed if scope["resource_kind"] == "api_catalog_metadata"
        )
        renamed_by_id = {renamed_catalog["scope_id"]: renamed_catalog}
        self.assertNotIn(old_scope_id, renamed_by_id)

        input_index = read_json(ROOT / MODULE.INPUT_INDEX_PATH)
        catalog_inputs = copy.deepcopy(input_index)
        catalog_inputs["inputs"] = [
            item
            for item in input_index["inputs"]
            if item["scope_id"] == old_scope_id
            and item["role"] in {"source_profile", "source_catalog_snapshot"}
        ]
        self.assertEqual(
            {item["role"] for item in catalog_inputs["inputs"]},
            {"source_profile", "source_catalog_snapshot"},
        )
        for item in catalog_inputs["inputs"]:
            item["scope_id"] = renamed_catalog["scope_id"]
        with tempfile.TemporaryDirectory(prefix="completeness-scope-input-ref-") as directory:
            index_path = pathlib.Path(directory) / "inputs.json"
            write_json(index_path, catalog_inputs)
            MODULE.validate_input_index(
                root=ROOT,
                input_root=ROOT,
                index_path=index_path,
                scope_by_id=renamed_by_id,
            )

            write_json(index_path, input_index)
            with self.assertRaisesRegex(ValueError, "refers to unregistered scope"):
                MODULE.validate_input_index(
                    root=ROOT,
                    input_root=ROOT,
                    index_path=index_path,
                    scope_by_id=renamed_by_id,
                )

    def test_added_profile_or_configured_source_requires_scope_registration(self) -> None:
        scopes = self._scope_registry()["scopes"]

        unregistered_profile_root = self._temporary_registry_root()
        profile = read_json(unregistered_profile_root / "sources/ecos.json")
        write_json(unregistered_profile_root / "sources/not-configured.json", profile)
        with self.assertRaisesRegex(ValueError, "configured source profiles and sustainable coverage registry differ"):
            MODULE.validate_registered_sources(unregistered_profile_root, scopes)

        unscoped_source_root = self._temporary_registry_root()
        coverage_path = unscoped_source_root / "policy/sustainable-coverage.json"
        coverage = read_json(coverage_path)
        extra_source = copy.deepcopy(coverage["supported_sources"][0])
        extra_source["source_id"] = "unregistered_source"
        extra_source["profile"] = "sources/unregistered_source.json"
        coverage["supported_sources"].append(extra_source)
        write_json(coverage_path, coverage)
        extra_profile = read_json(unscoped_source_root / "sources/ecos.json")
        extra_profile["source_id"] = "unregistered_source"
        write_json(unscoped_source_root / "sources/unregistered_source.json", extra_profile)
        with self.assertRaisesRegex(
            ValueError,
            "every configured source must have exactly one registered API-operation scope",
        ):
            MODULE.validate_registered_sources(unscoped_source_root, scopes)

    def test_registry_rejects_duplicate_missing_unknown_and_wrong_kind_bindings(self) -> None:
        root = self._temporary_registry_root()
        registry_path = root / MODULE.SCOPE_REGISTRY_PATH
        original = read_json(registry_path)

        cases = (
            (
                "duplicate-scope-id",
                lambda value: value["scopes"].append(copy.deepcopy(value["scopes"][0])),
                "duplicate registered scope ID",
            ),
            (
                "missing-configured-source-scope",
                lambda value: remove_scope(value, "api_operation_manifest", "ecos"),
                "every configured source must have exactly one registered API-operation "
                "scope",
            ),
            (
                "unknown-resource-kind",
                lambda value: set_scope_kind(value, "payload_rows", "unknown_kind"),
                "scope registry schema",
            ),
            (
                "payload-row-cannot-be-operation-denominator",
                lambda value: set_scope_kind(value, "payload_rows", "api_operation_manifest"),
                "cannot register an API-operation scope without a configured source profile",
            ),
        )
        for name, mutate, expected in cases:
            with self.subTest(case=name):
                candidate = copy.deepcopy(original)
                mutate(candidate)
                write_json(registry_path, candidate)
                with self.assertRaisesRegex(ValueError, expected):
                    MODULE.load_and_validate_registry(root)

    def test_registry_rejects_selector_identity_and_policy_digest_drift(self) -> None:
        root = self._temporary_registry_root()
        registry_path = root / MODULE.SCOPE_REGISTRY_PATH
        original = read_json(registry_path)

        cases = (
            (
                "catalog-selector",
                lambda value: set_scope_selector(
                    value, "api_catalog_metadata", "sources/ecos.json"
                ),
                "data.go.kr catalog scope must name the local registry snapshot explicitly",
            ),
            (
                "operation-selector",
                lambda value: scope_for(value, "api_operation_manifest", "ecos")[
                    "selector"
                ].__setitem__("value", "reports/other.json"),
                "selector or identity fields do not match its local operation inventory",
            ),
            (
                "operation-identity-algorithm",
                lambda value: scope_for(value, "api_operation_manifest", "ecos").__setitem__(
                    "identity_algorithm", "different-v1"
                ),
                "does not use the exact declared operation-ID algorithm",
            ),
            (
                "file-placeholder-identity-algorithm",
                lambda value: set_scope_algorithm(value, "file_dataset", "different-v1"),
                None,
            ),
            (
                "file-placeholder-selector",
                lambda value: set_scope_selector(value, "file_dataset", "unreviewed snapshot"),
                None,
            ),
            (
                "snapshot-placeholder-selector",
                lambda value: set_scope_selector(
                    value, "curated_payload_snapshot", "other portfolio"
                ),
                None,
            ),
            (
                "snapshot-placeholder-identity-algorithm",
                lambda value: set_scope_algorithm(
                    value, "curated_payload_snapshot", "different-v1"
                ),
                None,
            ),
            (
                "rows-placeholder-selector",
                lambda value: set_scope_selector(value, "payload_rows", "other rows"),
                None,
            ),
            (
                "rows-placeholder-identity-algorithm",
                lambda value: set_scope_algorithm(value, "payload_rows", "different-v1"),
                None,
            ),
            (
                "curated-payload-source-identity",
                lambda value: scope_for(value, "curated_payload_snapshot").__setitem__(
                    "source_id", "unregistered_payload_source"
                ),
                None,
            ),
            (
                "curated-payload-owner",
                lambda value: scope_for(value, "curated_payload_snapshot").__setitem__(
                    "identity_owner", "Other/Owner"
                ),
                "owner binding differs from completeness policy",
            ),
        )
        for name, mutate, expected in cases:
            with self.subTest(case=name):
                candidate = copy.deepcopy(original)
                mutate(candidate)
                write_json(registry_path, candidate)
                failure = (
                    self.assertRaisesRegex(ValueError, expected)
                    if expected is not None
                    else self.assertRaises(ValueError)
                )
                with failure:
                    MODULE.load_and_validate_registry(root)

        wrong_policy_digest = copy.deepcopy(original)
        wrong_policy_digest["completeness_policy"]["sha256"] = "0" * 64
        write_json(registry_path, wrong_policy_digest)
        with self.assertRaisesRegex(ValueError, "not bound to the checked-in completeness policy bytes"):
            MODULE.load_and_validate_registry(root)

    def test_input_kind_and_digest_mismatches_are_rejected_at_admission(self) -> None:
        scope_by_id = self._load_scope_map()
        original = read_json(ROOT / MODULE.INPUT_INDEX_PATH)
        source_profile_id = next(
            item["input_id"] for item in original["inputs"]
            if item["scope_id"] == "data-go-kr.api-catalog" and item["role"] == "source_profile"
        )

        with tempfile.TemporaryDirectory(prefix="completeness-scope-input-kind-") as directory:
            index_path = pathlib.Path(directory) / "inputs.json"
            wrong_kind = copy.deepcopy(original)
            source_profile = next(
                item for item in wrong_kind["inputs"] if item["input_id"] == source_profile_id
            )
            source_profile["artifact_type"] = "source_snapshot"
            wrong_kind["inputs"].remove(source_profile)
            wrong_kind["inputs"].insert(0, source_profile)
            write_json(index_path, wrong_kind)
            with self.assertRaisesRegex(ValueError, "unregistered role/artifact-type binding"):
                MODULE.validate_input_index(
                    root=ROOT,
                    input_root=ROOT,
                    index_path=index_path,
                    scope_by_id=scope_by_id,
                )

            wrong_digest = copy.deepcopy(original)
            source_profile = next(
                item for item in wrong_digest["inputs"] if item["input_id"] == source_profile_id
            )
            source_profile["sha256"] = "0" * 64
            wrong_digest["inputs"].remove(source_profile)
            wrong_digest["inputs"].insert(0, source_profile)
            write_json(index_path, wrong_digest)
            with self.assertRaisesRegex(ValueError, "byte count or SHA-256 differs"):
                MODULE.validate_input_index(
                    root=ROOT,
                    input_root=ROOT,
                    index_path=index_path,
                    scope_by_id=scope_by_id,
                )

    def test_operation_proof_cannot_borrow_another_resource_kind_denominator(self) -> None:
        proof = read_json(ROOT / "fixtures/completeness-proof/valid-complete.json")
        proof["scope"]["resource_kind"] = "file_dataset"
        validator = MODULE.import_module(
            "completeness_proof_contract_scope_registration",
            ROOT / "scripts/validate-completeness-proof.py",
        )
        validator.POLICY = ROOT / MODULE.POLICY_PATH
        validator.POLICY_SCHEMA = ROOT / "schemas/datapan.completeness-proof-policy.v1.schema.json"
        validator.PROOF_SCHEMA = ROOT / "schemas/datapan.completeness-proof.v1.schema.json"
        policy = MODULE.object_at(ROOT / MODULE.POLICY_PATH, "completeness policy")

        with self.assertRaisesRegex(ValueError, "scope kind and denominator type are conflated"):
            validator.validate_proof(proof, policy)


if __name__ == "__main__":
    unittest.main()
