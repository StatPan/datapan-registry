import importlib.util
import json
import pathlib
import tempfile
import unittest

import jsonschema


MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "huggingface_registry_distribution.py"
SPEC = importlib.util.spec_from_file_location("hf_distribution", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class HuggingFaceRegistryDistributionTest(unittest.TestCase):
    def test_workflow_publishes_main_dataset_changes_but_not_pull_requests(self):
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
        publish_condition = (
            "if: github.event_name == 'push' || "
            "(github.event_name == 'workflow_dispatch' && inputs.publish)"
        )
        self.assertEqual(workflow.count(publish_condition), 2)
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


if __name__ == "__main__":
    unittest.main()
