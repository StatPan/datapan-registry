from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import tempfile
import unittest
import zipfile


ROOT = pathlib.Path(__file__).parents[1]
GENERATOR_PATH = ROOT / "scripts/generate-data-go-kr-operation-manifest.py"
VALIDATOR_PATH = ROOT / "scripts/validate-data-go-kr-operation-manifest.py"
PACKAGER_PATH = ROOT / "scripts/package-registry-release.py"


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = load_module("operation_manifest_generator", GENERATOR_PATH)
VALIDATOR = load_module("operation_manifest_validator", VALIDATOR_PATH)
PACKAGER = load_module("operation_manifest_packager", PACKAGER_PATH)


class DataGoKrOperationManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads((ROOT / "data/data-go-kr.registry.json").read_text(encoding="utf-8"))
        cls.schema = json.loads((ROOT / "schemas/datapan.data-go-kr-operation-manifest.v1.schema.json").read_text(encoding="utf-8"))
        cls.release_manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        cls.manifest = json.loads((ROOT / "reports/data-go-kr/operation-manifest.json").read_text(encoding="utf-8"))
        cls.expectation = json.loads((ROOT / "policy/data-go-kr-operation-denominator-expectation.json").read_text(encoding="utf-8"))
        cls.expectation_schema = json.loads((ROOT / "schemas/datapan.data-go-kr-operation-denominator-expectation.v1.schema.json").read_text(encoding="utf-8"))

    def test_source_snapshot_reproduces_api_denominator_and_exclusions(self) -> None:
        previous = GENERATOR.REGISTRY
        GENERATOR.REGISTRY = ROOT / "data/data-go-kr.registry.json"
        try:
            expected = GENERATOR.build(self.registry)
        finally:
            GENERATOR.REGISTRY = previous
        self.assertEqual(self.manifest, expected)
        summary = self.manifest["summary"]
        self.assertEqual(summary, self.expectation["expected_summary"])
        self.assertEqual(summary["protocols"]["SOAP"], 35)
        self.assertEqual(summary["protocols"]["REST"] + summary["protocols"]["SOAP"], summary["api_operations"])
        self.assertEqual(summary["eligibility"]["excluded"], 142)
        self.assertEqual(self.manifest["summary"]["exclusions"], {"link_operations": 8871, "operationless_catalog_entries": 473, "filedata_catalog_entries": 0})
        self.assertEqual((summary["identity_collisions"], summary["identity_omissions"]), (0, 0))
        VALIDATOR.validate(
            self.manifest, self.schema, self.registry, self.release_manifest,
            self.expectation, registry_path=ROOT / "data/data-go-kr.registry.json",
            expectation_schema=self.expectation_schema,
        )

    def test_duplicate_identity_fails_closed(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["operations"][1]["operation_id"] = broken["operations"][0]["operation_id"]
        with self.assertRaisesRegex(ValueError, "deterministically match|collision"):
            VALIDATOR.validate(broken, self.schema, self.registry, self.release_manifest, self.expectation, registry_path=ROOT / "data/data-go-kr.registry.json", expectation_schema=self.expectation_schema)

    def test_unclassified_requirement_fails_schema(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["operations"][0]["eligibility"] = {"status": "excluded", "excluded_reason": None}
        with self.assertRaises(Exception):
            VALIDATOR.validate(broken, self.schema, self.registry, self.release_manifest, self.expectation, registry_path=ROOT / "data/data-go-kr.registry.json", expectation_schema=self.expectation_schema)

    def test_source_bound_expectation_rejects_a_changed_denominator(self) -> None:
        expectation = copy.deepcopy(self.expectation)
        expectation["expected_summary"]["api_operations"] += 1
        with self.assertRaisesRegex(ValueError, "does not match the source-bound expectation"):
            VALIDATOR.validate(self.manifest, self.schema, self.registry, self.release_manifest, expectation, registry_path=ROOT / "data/data-go-kr.registry.json", expectation_schema=self.expectation_schema)

    def test_source_bound_expectation_rejects_a_different_snapshot(self) -> None:
        expectation = copy.deepcopy(self.expectation)
        expectation["source_snapshot"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "denominator expectation is stale"):
            VALIDATOR.validate(self.manifest, self.schema, self.registry, self.release_manifest, expectation, registry_path=ROOT / "data/data-go-kr.registry.json", expectation_schema=self.expectation_schema)

    def test_release_archive_is_a_compatible_static_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = pathlib.Path(directory) / "datapan-registry.zip"
            summary = PACKAGER.package_release_zip(ROOT / "manifest.json", archive_path)
            self.assertGreater(summary["entries"], 1)
            with zipfile.ZipFile(archive_path) as archive:
                members = set(archive.namelist())
                self.assertIn("schemas/datapan.data-go-kr-operation-manifest.v1.schema.json", members)
                self.assertIn("policy/data-go-kr-operation-denominator-expectation.json", members)
                payload = json.loads(archive.read("reports/data-go-kr/operation-manifest.json"))
            self.assertEqual(payload["summary"], self.expectation["expected_summary"])


if __name__ == "__main__":
    unittest.main()
