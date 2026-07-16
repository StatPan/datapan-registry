import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import jsonschema


MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "huggingface_registry_distribution.py"
SPEC = importlib.util.spec_from_file_location("hf_distribution", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class HuggingFaceRegistryDistributionTest(unittest.TestCase):
    def test_workflow_validates_main_changes_but_publishes_only_by_explicit_dispatch(self):
        workflow_path = (
            pathlib.Path(__file__).parents[1]
            / ".github"
            / "workflows"
            / "huggingface-distribution.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")
        self.assertIn("  push:\n    branches:\n      - main\n    paths:", workflow)
        self.assertIn('      - "data/**"', workflow)
        self.assertIn('      - "manifest.json"', workflow)
        self.assertIn('      - "reports/**"', workflow)
        self.assertIn("|| 'publication' }}", workflow)
        self.assertIn("  cancel-in-progress: false", workflow)
        publish_condition = "if: github.event_name == 'workflow_dispatch' && inputs.publish"
        self.assertEqual(workflow.count(publish_condition), 2)
        self.assertIn(
            "if: always() && github.event_name == 'workflow_dispatch' && inputs.publish",
            workflow,
        )
        self.assertIn("--expected-revision", workflow)
        self.assertIn(".datapan/hf-publication.json", workflow)
        self.assertNotIn("if: github.event_name == 'push' ||", workflow)
        self.assertNotIn("github.event_name == 'pull_request' && inputs.publish", workflow)

    def fixture(self, root: pathlib.Path) -> pathlib.Path:
        data = root / "data" / "registry.json"
        report = root / "reports" / "readiness.json"
        data.parent.mkdir(parents=True)
        report.parent.mkdir(parents=True)
        data.write_text('[{"id":"one"}]\n', encoding="utf-8")
        report.write_text('{"ready":true}\n', encoding="utf-8")
        records = [
            MODULE.artifact(data, "data/registry.json", "registry"),
            MODULE.artifact(report, "reports/readiness.json", "release_readiness"),
        ]
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "datapan.release-manifest.v1",
                    "generated_at": "2026-07-11T00:00:00Z",
                    "artifacts": records,
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def test_stage_and_finalize_bind_immutable_payload(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            manifest = self.fixture(root)
            stage_dir = root / "stage"
            index = MODULE.stage(manifest, stage_dir, [])
            self.assertEqual(index["artifact_count"], 2)
            revision = "a" * 40
            pointer = MODULE.finalize(stage_dir, "StatPan/datapan-registry", revision)
            schema_path = pathlib.Path(__file__).parents[1] / "schemas" / "datapan.huggingface-distribution.v1.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(pointer)
            self.assertEqual(pointer["dataset"]["revision"], revision)
            self.assertEqual(pointer["artifact_count"], 2)
            self.assertTrue((stage_dir / MODULE.POINTER_PATH).is_file())

    def test_stage_rejects_tampered_manifest_artifact(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            manifest = self.fixture(root)
            (root / "data" / "registry.json").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.DistributionError, "byte mismatch|SHA-256 mismatch"):
                MODULE.stage(manifest, root / "stage", [])

    def test_stage_rejects_missing_extra_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            manifest = self.fixture(root)
            with self.assertRaisesRegex(MODULE.DistributionError, "artifact is missing"):
                MODULE.stage(
                    manifest,
                    root / "stage",
                    ["reports/latest-release-readiness.json=missing-readiness.json"],
                )

    def test_finalize_rejects_missing_immutable_revision(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            stage_dir = root / "stage"
            MODULE.stage(self.fixture(root), stage_dir, [])
            with self.assertRaisesRegex(MODULE.DistributionError, "full immutable commit SHA"):
                MODULE.finalize(stage_dir, "StatPan/datapan-registry", "main")

    def test_finalize_rechecks_staged_artifacts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            stage_dir = root / "stage"
            MODULE.stage(self.fixture(root), stage_dir, [])
            (stage_dir / "data" / "registry.json").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.DistributionError, "byte mismatch|SHA-256 mismatch"):
                MODULE.finalize(stage_dir, "StatPan/datapan-registry", "b" * 40)

    def test_publish_requires_token_before_importing_client(self):
        with self.assertRaisesRegex(MODULE.DistributionError, "HF_TOKEN"):
            MODULE.publish(pathlib.Path("unused"), "StatPan/datapan-registry", "")

    def test_remote_pointer_requires_real_immutable_revision_and_exact_artifacts(self):
        pointer = {
            "schema_version": MODULE.SCHEMA_VERSION,
            "dataset": {"id": "StatPan/datapan-registry", "revision": "a" * 40},
            "release_manifest": {"path": "manifest.json", "bytes": 1, "sha256": "b" * 64},
            "artifact_count": 1,
            "artifacts": [
                {"path": "schemas/diagnostic.json", "kind": "schema", "bytes": 1, "sha256": "c" * 64}
            ],
        }
        dataset, revision, records = MODULE.validate_remote_pointer(
            pointer, "a" * 40, [f"schemas/diagnostic.json={'c' * 64}"]
        )
        self.assertEqual(dataset, "StatPan/datapan-registry")
        self.assertEqual(revision, "a" * 40)
        self.assertEqual(len(records), 2)

        pointer["dataset"]["revision"] = "0" * 40
        with self.assertRaisesRegex(MODULE.DistributionError, "nonzero immutable revision"):
            MODULE.validate_remote_pointer(pointer, None, [])

    def test_remote_pointer_rejects_missing_or_changed_required_artifact(self):
        pointer = {
            "schema_version": MODULE.SCHEMA_VERSION,
            "dataset": {"id": "StatPan/datapan-registry", "revision": "a" * 40},
            "release_manifest": {"path": "manifest.json", "bytes": 1, "sha256": "b" * 64},
            "artifact_count": 0,
            "artifacts": [],
        }
        with self.assertRaisesRegex(MODULE.DistributionError, "required distribution artifact is missing"):
            MODULE.validate_remote_pointer(pointer, None, [f"schemas/diagnostic.json={'c' * 64}"])

    def test_verify_remote_downloads_and_verifies_manifest_required_artifact_and_revision(self):
        manifest_bytes = b'{"schema_version":"datapan.release-manifest.v1"}\n'
        diagnostic_bytes = b'{"schema_version":"datapan.diagnostic-envelope.v1"}\n'
        manifest_sha = MODULE.hashlib.sha256(manifest_bytes).hexdigest()
        diagnostic_sha = MODULE.hashlib.sha256(diagnostic_bytes).hexdigest()
        revision = "d" * 40
        pointer_url = "https://example.test/release/distribution-manifest.json"
        pointer = {
            "schema_version": MODULE.SCHEMA_VERSION,
            "dataset": {"id": "StatPan/datapan-registry", "revision": revision},
            "release_manifest": {
                "path": "manifest.json",
                "kind": "release_manifest",
                "bytes": len(manifest_bytes),
                "sha256": manifest_sha,
            },
            "artifact_count": 1,
            "artifacts": [
                {
                    "path": "schemas/datapan.diagnostic-envelope.v1.schema.json",
                    "kind": "schema",
                    "bytes": len(diagnostic_bytes),
                    "sha256": diagnostic_sha,
                }
            ],
        }
        downloaded = []

        def fake_download(url, destination):
            downloaded.append(url)
            if url == pointer_url:
                destination.write_text(json.dumps(pointer), encoding="utf-8")
            elif url == MODULE.resolve_url("StatPan/datapan-registry", revision, "manifest.json"):
                destination.write_bytes(manifest_bytes)
            elif url == MODULE.resolve_url(
                "StatPan/datapan-registry",
                revision,
                "schemas/datapan.diagnostic-envelope.v1.schema.json",
            ):
                destination.write_bytes(diagnostic_bytes)
            else:
                self.fail(f"unexpected download: {url}")

        required = [
            f"manifest.json={manifest_sha}",
            f"schemas/datapan.diagnostic-envelope.v1.schema.json={diagnostic_sha}",
        ]
        with mock.patch.object(MODULE, "download", side_effect=fake_download):
            receipt = MODULE.verify_remote(pointer_url, revision, required)

        self.assertEqual(
            receipt,
            {
                "status": "verified",
                "dataset": "StatPan/datapan-registry",
                "revision": revision,
                "artifacts": 1,
                "release_manifest": "verified",
            },
        )
        self.assertEqual(
            downloaded,
            [
                pointer_url,
                MODULE.resolve_url("StatPan/datapan-registry", revision, "manifest.json"),
                MODULE.resolve_url(
                    "StatPan/datapan-registry",
                    revision,
                    "schemas/datapan.diagnostic-envelope.v1.schema.json",
                ),
            ],
        )

    def test_verify_remote_rejects_tampered_release_manifest_download(self):
        manifest_bytes = b"expected manifest\n"
        revision = "e" * 40
        pointer_url = "https://example.test/release/distribution-manifest.json"
        pointer = {
            "schema_version": MODULE.SCHEMA_VERSION,
            "dataset": {"id": "StatPan/datapan-registry", "revision": revision},
            "release_manifest": {
                "path": "manifest.json",
                "kind": "release_manifest",
                "bytes": len(manifest_bytes),
                "sha256": MODULE.hashlib.sha256(manifest_bytes).hexdigest(),
            },
            "artifact_count": 0,
            "artifacts": [],
        }

        def fake_download(url, destination):
            if url == pointer_url:
                destination.write_text(json.dumps(pointer), encoding="utf-8")
            else:
                destination.write_bytes(b"tampered\n")

        with mock.patch.object(MODULE, "download", side_effect=fake_download):
            with self.assertRaisesRegex(MODULE.DistributionError, "byte mismatch|SHA-256 mismatch"):
                MODULE.verify_remote(pointer_url, revision, [])


if __name__ == "__main__":
    unittest.main()
