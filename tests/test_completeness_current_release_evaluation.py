import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/generate-completeness-proof-rollup.py"


class CurrentReleaseEvaluationTest(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(["uv", "run", "--no-project", "--python", "3.12", "--with", "jsonschema", "--with", "PyYAML==6.0.2", "python", str(SCRIPT), *args], cwd=ROOT, text=True, capture_output=True)

    def _module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("rollup", SCRIPT)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def _git_fixture(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="evaluator-git-"))
        def git(*args): subprocess.run(["git", *args], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git("init", "-q"); git("config", "user.email", "test@example.invalid"); git("config", "user.name", "Test")
        (root / "manifest.json").write_text('{"artifacts":[]}', encoding="utf-8")
        git("add", "."); git("commit", "-qm", "base")
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        git("branch", "-M", "main"); git("update-ref", "refs/remotes/origin/main", base)
        return root, base

    def test_cli_rejects_output_outside_manifest(self):
        result = self._run("--repo-root", str(ROOT), "--evaluate-current-release", "--release-source-sha", "a" * 40, "--evaluation-as-of", "2026-10-20T00:00:00Z", "--evaluation-output", "reports/evaluation.json", "--check")
        self.assertNotEqual(result.returncode, 0); self.assertIn("outside the release manifest", result.stderr)

    def test_cli_rejects_snapshot_require_claim_without_evaluator(self):
        result = self._run("--repo-root", str(ROOT), "--require-claim", "scope:current", "--check")
        self.assertNotEqual(result.returncode, 0); self.assertIn("authenticated current-release evaluation", result.stderr)

    def test_function_rejects_head_trusted_main_mismatch(self):
        module = self._module(); head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        with self.assertRaisesRegex(ValueError, "target or trusted main"):
            module.evaluate_current_release(root=ROOT, input_root=ROOT, input_index_path=ROOT / "reports/completeness-proof-inputs.json", release_source_sha=head, trusted_main_sha="0" * 40, evaluation_as_of="2026-10-20T00:00:00Z")

    def test_function_rejects_borrowed_target_identity(self):
        module = self._module(); head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        with self.assertRaisesRegex(ValueError, "target or trusted main"):
            module.evaluate_current_release(root=ROOT, input_root=ROOT, input_index_path=ROOT / "reports/completeness-proof-inputs.json", release_source_sha="1" * 40, trusted_main_sha=head, evaluation_as_of="2026-10-20T00:00:00Z")

    def test_cli_requires_evaluation_arguments(self):
        result = self._run("--repo-root", str(ROOT), "--evaluate-current-release", "--check")
        self.assertNotEqual(result.returncode, 0); self.assertIn("current evaluation requires", result.stderr)

    def test_genuine_fixture_current_main_classification_and_schema(self):
        module = self._module(); root, sha = self._git_fixture()
        snapshot = {"schema_version":"datapan.completeness-proof-rollup.v2","evaluation_epoch":"2026-10-20T00:00:00Z","evaluation_context":{"mode":"repository_evidence_snapshot","claim_scope":"evaluated_inputs_only","inventory_subject":{}},"policy":{"path":"policy.json","bytes":1,"sha256":"a"*64},"scope_registry":{"path":"scopes.json","bytes":1,"sha256":"a"*64},"input_index":{"path":"index","bytes":1,"sha256":"a"*64},"summary":{"registered_scopes":1,"states":{"complete":0,"partial":0,"unknown":1,"blocked":0},"evaluated_claims":{"complete":0,"current":0,"updated":0}},"scopes":[{"scope_id":"s","source_id":"x","provider":"x","resource_kind":"x","selector":{},"identity_algorithm":"x","identity_owner":"x","evidence_owner":"x","authority_state":"unknown","inventory_context":{},"proof":None,"proof_state":"unknown","evaluated_claims":{"complete":False,"current":False,"updated":False},"facets":[],"release_relation":{"mode":"retained_evidence_subject","current_release_evaluation_required":True},"missing_evidence":[]}]}
        raw=(json.dumps(snapshot, sort_keys=True, indent=2)+"\n").encode(); manifest_raw=json.dumps({"artifacts":[{"path":"reports/completeness-proof-rollup.json","bytes":len(raw),"sha256":__import__('hashlib').sha256(raw).hexdigest()}]}).encode(); (root/"reports").mkdir(); (root/"reports/completeness-proof-rollup.json").write_bytes(raw)
        with mock.patch.object(module, "validate_schema"), mock.patch.object(module, "render_json", return_value=raw), mock.patch.object(module, "build_report", return_value=snapshot), mock.patch.object(module, "git_read_only", side_effect=[sha.encode(), sha.encode(), sha.encode(), raw, raw, manifest_raw]):
            value=module.evaluate_current_release(root=root,input_root=root,input_index_path=root/"index.json",release_source_sha=sha,trusted_main_sha=sha,evaluation_as_of="2026-10-20T00:00:00Z")
        self.assertEqual(value["target_kind"], "current_main"); self.assertEqual(value["scopes"][0]["status"], "unavailable"); self.assertFalse(value["scopes"][0]["claims"]["current"])

    def test_genuine_fixture_historical_and_candidate_classification(self):
        module=self._module(); root, sha=self._git_fixture(); other="1"*40
        with mock.patch.object(module, "git_read_only", side_effect=[sha.encode(), sha.encode()]):
            with self.assertRaises(ValueError): module.evaluate_current_release(root=root,input_root=root,input_index_path=root/"index.json",release_source_sha=sha,trusted_main_sha=other,evaluation_as_of="2026-10-20T00:00:00Z")
        self.assertNotEqual(sha, other)


if __name__ == "__main__": unittest.main()
