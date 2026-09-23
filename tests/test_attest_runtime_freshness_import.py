from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "attest-runtime-freshness-import.py"
SPEC = importlib.util.spec_from_file_location("attest_runtime_freshness_import", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AttestRuntimeFreshnessImportTest(unittest.TestCase):
    def write_json(self, path: pathlib.Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def event(self, *, head: str = "automation/runtime-freshness-123", merged: bool = True) -> dict[str, object]:
        return {
            "repository": {"full_name": "StatPan/datapan-registry"},
            "pull_request": {
                "number": 42,
                "html_url": "https://github.com/StatPan/datapan-registry/pull/42",
                "state": "closed",
                "merged": merged,
                "merge_commit_sha": "b" * 40,
                "merged_at": "2026-09-22T01:00:00Z",
                "head": {"ref": head, "repo": {"full_name": "StatPan/datapan-registry"}},
                "base": {"ref": "main"},
            },
        }

    def fixture(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        import_receipt = root / "reports/runtime-freshness-imports/123.json"
        admission = root / "reports/runtime-freshness-import-admissions/123.json"
        manifest = root / "manifest.json"
        ledger = root / "reports/release-assembly-receipt.json"
        self.write_json(import_receipt, {"run_id": "123"})
        zero = {"total": 0, "verified": 0, "failed": 0, "skipped": 0, "unknown": 0}
        self.write_json(admission, {
            "schema_version": "datapan.runtime-freshness-import-admission.v1",
            "admitted_at": "2026-09-22T00:10:00Z",
            "run_id": "123",
            "producer": {
                "repository": "StatPan/datapan-registry",
                "revision": "a" * 40,
                "run_id": "123",
                "run_url": "https://github.com/StatPan/datapan-registry/actions/runs/123",
            },
            "inputs": {
                "sanitized_report_sha256": "1" * 64,
                "run_receipt_sha256": "2" * 64,
                "planned_identity_set": {"count": 1, "sha256": "3" * 64},
                "import_receipt": {
                    "path": "reports/runtime-freshness-imports/123.json",
                    "sha256": hashlib.sha256(import_receipt.read_bytes()).hexdigest(),
                },
            },
            "outcome": "no_change",
            "arithmetic": {"before": zero, "selected": zero, "after": zero, "selected_new_results": 0},
            "selected_identity_set": {"count": 0, "sha256": hashlib.sha256(b"[]").hexdigest()},
        })
        self.write_json(manifest, {"schema_version": "test"})
        self.write_json(ledger, {"schema_version": "test"})
        return admission, manifest, ledger

    def build(self, root: pathlib.Path) -> dict[str, object]:
        admission, manifest, ledger = self.fixture(root)
        original = admission.read_bytes()
        old_ancestor, old_blob = MODULE.git_is_ancestor, MODULE.git_blob
        MODULE.git_is_ancestor = lambda _root, _commit: True
        MODULE.git_blob = lambda _root, _commit, _path: original
        try:
            return MODULE.build(
                root=root,
                admission_path=admission,
                event=self.event(),
                manifest_path=manifest,
                release_ledger_path=ledger,
            )
        finally:
            MODULE.git_is_ancestor, MODULE.git_blob = old_ancestor, old_blob

    def test_pulls_api_payload_matches_webhook_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            webhook = self.build(root)
            admission, manifest, ledger = self.fixture(root)
            original = admission.read_bytes()
            old_ancestor, old_blob = MODULE.git_is_ancestor, MODULE.git_blob
            MODULE.git_is_ancestor = lambda _root, _commit: True
            MODULE.git_blob = lambda _root, _commit, _path: original
            try:
                rest = MODULE.build(
                    root=root,
                    admission_path=admission,
                    event=self.event()["pull_request"],
                    manifest_path=manifest,
                    release_ledger_path=ledger,
                )
            finally:
                MODULE.git_is_ancestor, MODULE.git_blob = old_ancestor, old_blob
        self.assertEqual(rest["import"], webhook["import"])
        pull = self.event()["pull_request"]
        self.assertEqual(MODULE.repository_name(pull, pull), "StatPan/datapan-registry")
        with self.assertRaisesRegex(ValueError, "does not contain a pull request"):
            MODULE.pull_object({})

    def test_fixed_point_materializes_lfs_registry_before_ledger_check(self) -> None:
        with mock.patch.object(MODULE.subprocess, "run") as run:
            MODULE.run_fixed_point(pathlib.Path("/repo"))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            commands,
            [
                [MODULE.sys.executable, "scripts/materialize-canonical-registry.py"],
                [MODULE.sys.executable, "scripts/refresh-release-ledger-evidence.py", "--check"],
            ],
        )
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["cwd"], pathlib.Path("/repo"))
            self.assertTrue(call.kwargs["check"])

    def test_attestation_binds_merged_pr_and_fixed_point_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            value = self.build(root)
            MODULE.validate_schema(
                value,
                pathlib.Path(__file__).parents[1] / "schemas/datapan.runtime-freshness-import-attestation.v1.schema.json",
            )
            admission = root / value["lineage"]["admission"]["path"]
            MODULE.validate_current(
                root=root,
                value=value,
                is_ancestor=lambda _root, _commit: True,
                read_blob=lambda _root, _commit, _path: admission.read_bytes(),
            )
            self.assertEqual(value["outcome"], "no_change")
            self.assertEqual(value["import"]["pull_request"], 42)
            self.assertEqual(value["import"]["merge_commit"], "b" * 40)
            self.assertEqual(value["attested_at"], "2026-09-22T01:00:00Z")

    def test_closed_unmerged_or_merely_open_pr_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            admission, manifest, ledger = self.fixture(root)
            for event in (self.event(merged=False), {**self.event(), "pull_request": {**self.event()["pull_request"], "state": "open", "merged": False}}):
                with self.subTest(event=event["pull_request"]["state"]), self.assertRaisesRegex(ValueError, "without a merge"):
                    MODULE.build(root=root, admission_path=admission, event=event, manifest_path=manifest, release_ledger_path=ledger)

    def test_wrong_merge_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            admission, _, _ = self.fixture(root)
            with self.assertRaisesRegex(ValueError, "not reachable"):
                MODULE.require_merge_lineage(
                    root=root,
                    commit="b" * 40,
                    path=admission.relative_to(root).as_posix(),
                    is_ancestor=lambda _root, _commit: False,
                    read_blob=lambda _root, _commit, _path: admission.read_bytes(),
                )

    def test_missing_or_stale_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            value = self.build(root)
            import_receipt = root / value["lineage"]["import_receipt"]["path"]
            original = import_receipt.read_bytes()
            import_receipt.unlink()
            admission = root / value["lineage"]["admission"]["path"]
            with self.assertRaisesRegex(ValueError, "import receipt is absent or stale"):
                MODULE.validate_current(
                    root=root,
                    value=value,
                    is_ancestor=lambda _root, _commit: True,
                    read_blob=lambda _root, _commit, _path: admission.read_bytes(),
                )
            import_receipt.write_bytes(original)
            import_receipt.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "import receipt is absent or stale"):
                MODULE.validate_current(
                    root=root,
                    value=value,
                    is_ancestor=lambda _root, _commit: True,
                    read_blob=lambda _root, _commit, _path: admission.read_bytes(),
                )

    def test_final_verifier_rejects_missing_main_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            event_path = root / "event.json"
            self.write_json(
                event_path,
                self.event(head="automation/runtime-freshness-attestation-123"),
            )
            args = argparse.Namespace(
                root=root,
                run_id="123",
                event=event_path,
                schema=pathlib.Path("schemas/datapan.runtime-freshness-import-attestation.v1.schema.json"),
            )
            with self.assertRaisesRegex(ValueError, "missing the run attestation"):
                MODULE.verify(args)

    def test_release_ledger_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            value = self.build(root)
            ledger = root / value["registry"]["release_ledger"]["path"]
            ledger.write_text("{}\n", encoding="utf-8")
            admission = root / value["lineage"]["admission"]["path"]
            with self.assertRaisesRegex(ValueError, "release ledger digest is stale"):
                MODULE.validate_current(
                    root=root,
                    value=value,
                    is_ancestor=lambda _root, _commit: True,
                    read_blob=lambda _root, _commit, _path: admission.read_bytes(),
                )

    def test_same_attestation_replay_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            content = MODULE.render(self.build(root))
            output = root / "reports/runtime-freshness-import-attestations/123.json"
            MODULE.write_exact(output, content)
            first = output.read_bytes()
            MODULE.write_exact(output, content)
            self.assertEqual(output.read_bytes(), first)


if __name__ == "__main__":
    unittest.main()
