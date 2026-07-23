from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import tempfile
import unittest
import zipfile


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "generate-release-version-decision.py"
SPEC = importlib.util.spec_from_file_location("release_version_decision", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ROOT = pathlib.Path(__file__).parents[1]
PACKAGER_PATH = ROOT / "scripts" / "package-registry-release.py"
PACKAGER_SPEC = importlib.util.spec_from_file_location("registry_packager", PACKAGER_PATH)
assert PACKAGER_SPEC and PACKAGER_SPEC.loader
PACKAGER = importlib.util.module_from_spec(PACKAGER_SPEC)
PACKAGER_SPEC.loader.exec_module(PACKAGER)


class ReleaseVersionDecisionTest(unittest.TestCase):
    def manifest(self) -> dict[str, object]:
        return {
            "schema_version": "datapan.release-manifest.v1",
            "generated_at": "2026-07-23T00:00:00Z",
            "datapan_version": "1.2.3",
            "artifact_count": 2,
            "artifacts": [
                {"path": "data/data-go-kr.registry.json", "kind": "registry", "bytes": 1, "sha256": "a" * 64},
                {"path": "reports/example.json", "kind": "report", "bytes": 2, "sha256": "b" * 64},
            ],
        }

    def policy(self, manifest: dict[str, object], version: str) -> dict[str, object]:
        return {
            "schema_version": "datapan.release-version-policy.v1",
            "allocation_authority": "release operator",
            "baseline": {"datapan_version": version, "input": MODULE.version_input(manifest)},
        }

    def test_identical_input_has_no_change_no_bump(self) -> None:
        manifest = self.manifest()
        report = MODULE.build_report(manifest, self.policy(manifest, "1.2.3"))
        self.assertEqual(report["decision"], "no_change_no_bump")
        self.assertFalse(report["change_required"])

    def test_source_or_inventory_change_requires_bump(self) -> None:
        baseline = self.manifest()
        changed = copy.deepcopy(baseline)
        changed["datapan_version"] = "1.2.4"
        changed["artifacts"][0]["sha256"] = "c" * 64  # type: ignore[index]
        report = MODULE.build_report(changed, self.policy(baseline, "1.2.3"))
        self.assertEqual(report["decision"], "changed_input_version_bumped")
        self.assertTrue(report["change_required"])

    def test_changed_input_with_unchanged_version_is_rejected(self) -> None:
        baseline = self.manifest()
        changed = copy.deepcopy(baseline)
        changed["artifacts"][1]["sha256"] = "c" * 64  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "requires a version bump"):
            MODULE.build_report(changed, self.policy(baseline, "1.2.3"))

    def test_unchanged_input_with_bumped_version_is_rejected(self) -> None:
        manifest = self.manifest()
        manifest["datapan_version"] = "1.2.4"
        baseline = self.manifest()
        with self.assertRaisesRegex(ValueError, "must not bump"):
            MODULE.build_report(manifest, self.policy(baseline, "1.2.3"))

    def test_request_only_profile_digest_is_excluded_but_its_contract_is_versioned(self) -> None:
        baseline = self.manifest()
        baseline["artifacts"].append({  # type: ignore[union-attr]
            "path": "reports/data-go-kr/request-only-client-profile.json",
            "kind": "request_only_client_profile",
            "schema": "https://schemas.datapan.dev/datapan.request-only-client-profile.v1.schema.json",
            "bytes": 10,
            "sha256": "d" * 64,
        })
        baseline["artifact_count"] = 3
        changed = copy.deepcopy(baseline)
        changed["artifacts"][2]["bytes"] = 11  # type: ignore[index]
        changed["artifacts"][2]["sha256"] = "e" * 64  # type: ignore[index]
        self.assertEqual(MODULE.version_input(baseline), MODULE.version_input(changed))
        changed["artifacts"][2]["schema"] = "https://schemas.datapan.dev/other.schema.json"  # type: ignore[index]
        self.assertNotEqual(MODULE.version_input(baseline), MODULE.version_input(changed))

    def test_release_archive_contains_version_receipt_and_operation_manifest_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = pathlib.Path(directory) / "datapan-registry.zip"
            PACKAGER.package_release_zip(ROOT / "manifest.json", archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                members = set(archive.namelist())
                self.assertTrue({
                    "reports/release-version-decision.json",
                    "schemas/datapan.release-version-decision.v1.schema.json",
                    "reports/data-go-kr/operation-manifest.json",
                    "schemas/datapan.data-go-kr-operation-manifest.v1.schema.json",
                    "reports/data-go-kr/request-only-client-profile.json",
                    "schemas/datapan.request-only-client-profile.v1.schema.json",
                }.issubset(members))
                decision = json.loads(archive.read("reports/release-version-decision.json"))
            self.assertEqual(decision["decision"], "changed_input_version_bumped")


if __name__ == "__main__":
    unittest.main()
