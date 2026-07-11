from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "run-upstream-refresh.py"
SPEC = importlib.util.spec_from_file_location("run_upstream_refresh", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ROOT = pathlib.Path(__file__).parents[1]


class UpstreamRefreshTest(unittest.TestCase):
    def files(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        baseline = root / "baseline.json"
        baseline.write_text('[{"id":"1","title":"one"}]\n')
        policy = root / "policy.json"
        policy.write_text(json.dumps({
            "sources": [{
                "source_id": "data_go_kr", "owner": "release-operator",
                "canonical_registry": baseline.as_posix(), "credential_env": "TEST_KEY",
                "importer": {"arguments": ["catalog", "import", "--output", "{candidate_registry}"]},
                "diff": {"material_change_fields": ["added", "removed", "changed"]},
                "publication": {"required_gates": ["release_manifest_verification", "release_readiness", "consumer_compatibility"]},
            }]
        }))
        return baseline, policy

    def invoke(self, policy: pathlib.Path, output: pathlib.Path, side_effect: list[subprocess.CompletedProcess[str]]) -> int:
        argv = ["run-upstream-refresh.py", "--policy", str(policy), "--schema", str(ROOT / "schemas/datapan.upstream-refresh-evidence.v1.schema.json"), "--datapan", "fake", "--output-dir", str(output), "--observed-at", "2026-07-11T00:00:00Z"]
        with mock.patch.object(MODULE.sys, "argv", argv), mock.patch.object(MODULE, "run", side_effect=side_effect):
            return MODULE.main()

    def successful_commands(self, output: pathlib.Path, changed: int) -> list[subprocess.CompletedProcess[str]]:
        candidate = output / "candidate.registry.json"
        candidate.parent.mkdir(parents=True)
        candidate.write_text('[{"id":"1","title":"one"}]\n' if not changed else '[{"id":"1","title":"changed"}]\n')
        diff = output / "catalog-diff.json"
        diff.write_text(json.dumps({
            "generated_at": "2020-01-01T00:00:00Z", "provider": "data.go.kr", "old": "old", "new": "new",
            "limit": 0, "truncated": False, "counts": {"old": 1, "new": 1},
            "summary": {"added": 0, "removed": 0, "changed": changed, "stable": 1 - changed},
            "added": [], "removed": [],
            "changed": [] if not changed else [{"id": "1", "old_title": "one", "new_title": "changed", "fields": ["title"], "old_digest": "1" * 64, "new_digest": "2" * 64}],
        }))
        return [subprocess.CompletedProcess([], 0, "{}", ""), subprocess.CompletedProcess([], 0, "{}", "")]

    def test_no_change_and_material_change_are_distinct(self) -> None:
        for changed, expected in ((0, "no_change"), (1, "material_change")):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory); _, policy = self.files(root); output = root / "output"
                code = self.invoke(policy, output, self.successful_commands(output, changed))
                evidence = json.loads((output / "upstream-refresh-evidence.json").read_text())
                self.assertEqual(code, 0); self.assertEqual(evidence["status"], expected)
                self.assertIsNotNone(evidence["diff"])

    def test_collection_failure_never_reports_zero_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory); _, policy = self.files(root); output = root / "output"
            failure = subprocess.CompletedProcess([], 4, '{"ok":false,"error":"missing_auth"}', "")
            with mock.patch.dict(os.environ, {}, clear=True):
                code = self.invoke(policy, output, [failure])
            evidence = json.loads((output / "upstream-refresh-evidence.json").read_text())
            self.assertEqual(code, 2); self.assertEqual(evidence["status"], "collection_failure")
            self.assertIsNone(evidence["diff"]); self.assertIsNone(evidence["snapshot"])
            self.assertEqual(evidence["review"]["action"], "investigate_collection_failure")


if __name__ == "__main__":
    unittest.main()
