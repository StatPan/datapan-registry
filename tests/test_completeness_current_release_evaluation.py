import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/generate-completeness-proof-rollup.py"


class CurrentReleaseEvaluationTest(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(["uv", "run", "--no-project", "--python", "3.12", "--with", "jsonschema", "--with", "PyYAML==6.0.2", "python", str(SCRIPT), *args], cwd=ROOT, text=True, capture_output=True)

    def test_cli_rejects_output_outside_manifest(self):
        result = self._run("--repo-root", str(ROOT), "--evaluate-current-release", "--release-source-sha", "a" * 40, "--evaluation-as-of", "2026-10-20T00:00:00Z", "--evaluation-output", "reports/evaluation.json", "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the release manifest", result.stderr)

    def test_cli_rejects_snapshot_require_claim_without_evaluator(self):
        result = self._run("--repo-root", str(ROOT), "--require-claim", "scope:current", "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authenticated current-release evaluation", result.stderr)

    def test_function_rejects_head_trusted_main_mismatch(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("rollup", SCRIPT)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        with self.assertRaisesRegex(ValueError, "target or trusted main"):
            module.evaluate_current_release(root=ROOT, input_root=ROOT, input_index_path=ROOT / "reports/completeness-proof-inputs.json", release_source_sha=head, trusted_main_sha="0" * 40, evaluation_as_of="2026-10-20T00:00:00Z")

    def test_function_rejects_borrowed_target_identity(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("rollup", SCRIPT)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        with self.assertRaisesRegex(ValueError, "target or trusted main"):
            module.evaluate_current_release(root=ROOT, input_root=ROOT, input_index_path=ROOT / "reports/completeness-proof-inputs.json", release_source_sha="1" * 40, trusted_main_sha=head, evaluation_as_of="2026-10-20T00:00:00Z")

    def test_cli_requires_evaluation_arguments(self):
        result = self._run("--repo-root", str(ROOT), "--evaluate-current-release", "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("current evaluation requires", result.stderr)


if __name__ == "__main__":
    unittest.main()
