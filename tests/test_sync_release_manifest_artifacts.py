from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import jsonschema


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "sync-release-manifest-artifacts.py"


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_module("sync_release_manifest_artifacts", SCRIPT)
SCHEMA_SYNC = load_module(
    "sync_release_schema_artifacts_for_queue_registration",
    ROOT / "scripts" / "sync-release-schema-artifacts.py",
)
LEDGER = load_module(
    "validate_release_ledger_ownership_for_queue_registration",
    ROOT / "scripts" / "validate-release-ledger-ownership.py",
)
POLICY_PATH = "policy/canonical-stale-queue-recovery.v1.json"
POLICY_SCHEMA_URI = "https://statpan.com/schemas/datapan.canonical-stale-queue-recovery-policy.v1.schema.json"
QUEUE_SCHEMA_PATHS = (
    "schemas/datapan.canonical-stale-c-queue-recovery-receipt.v1.schema.json",
    "schemas/datapan.canonical-stale-c-queue-recovery.v1.schema.json",
    "schemas/datapan.canonical-stale-queue-recovery-policy.v1.schema.json",
)


class SyncReleaseManifestArtifactsTest(unittest.TestCase):
    def fixture_root(self, *, with_policy: bool = True) -> pathlib.Path:
        root = pathlib.Path(tempfile.mkdtemp(prefix="release-manifest-queue-policy-"))
        pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\nsize 137735169\n"
        (root / "registry.json").write_bytes(pointer)
        if with_policy:
            policy_path = root / POLICY_PATH
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / POLICY_PATH, policy_path)
        manifest = {
            "schema_version": "datapan.release-manifest.v1",
            "generated_at": "2026-10-10T00:00:00Z",
            "datapan_version": "1.0.0",
            "provider": "fixture",
            "source_registry": "registry.json",
            "output_dir": "dist",
            "artifact_count": 1,
            "artifacts": [{
                "path": "registry.json", "kind": "registry",
                "bytes": 137735169, "sha256": "e" * 64,
            }],
        }
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return root

    def run_cli(self, root: pathlib.Path, mode: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, str(SCRIPT), mode,
                "--manifest", str(root / "manifest.json"),
                "--root", str(root),
            ],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=30,
        )

    def queue_schema_additions(self) -> list[dict[str, object]]:
        rows = []
        for relative in QUEUE_SCHEMA_PATHS:
            raw = (ROOT / relative).read_bytes()
            rows.append({
                "path": relative, "kind": "schema", "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
        return rows

    def test_missing_fixed_policy_is_registered_from_real_bytes_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            policy_dir = root / "policy"
            policy_dir.mkdir()
            shutil.copy2(ROOT / "policy/canonical-stale-queue-recovery.v1.json", policy_dir / "canonical-stale-queue-recovery.v1.json")
            (root / "schemas").mkdir()
            for relative in QUEUE_SCHEMA_PATHS:
                shutil.copy2(ROOT / relative, root / relative)
            pointer_bytes = b"version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\nsize 137735169\n"
            (root / "registry.json").write_bytes(pointer_bytes)
            manifest = {
                "schema_version": "datapan.release-manifest.v1",
                "generated_at": "2026-10-10T00:00:00Z",
                "datapan_version": "1.0.0",
                "provider": "fixture",
                "source_registry": "registry.json",
                "output_dir": "dist",
                "artifact_count": 1,
                "artifacts": [{
                    "path": "registry.json", "kind": "registry",
                    "bytes": 137735169, "sha256": "e" * 64,
                }],
            }

            index = {
                "schema_version": "datapan.schema-index.v1",
                "generated_at": manifest["generated_at"],
                "datapan_version": manifest["datapan_version"],
                "count": 0,
                "schemas": [],
            }
            previous_cwd = pathlib.Path.cwd()
            try:
                os.chdir(root)
                discovered_paths = [path.as_posix() for path in SCHEMA_SYNC.schema_paths()]
                schema_index = SCHEMA_SYNC.expected_schema_index(index, None)
                schema_index_bytes = SCHEMA_SYNC.stable_json_bytes(schema_index)
                schema_manifest = SCHEMA_SYNC.expected_manifest(
                    manifest, schema_index_bytes, schema_index, None,
                )
            finally:
                os.chdir(previous_cwd)
            self.assertEqual(discovered_paths, sorted(QUEUE_SCHEMA_PATHS))
            self.assertEqual(schema_manifest["artifact_count"], len(QUEUE_SCHEMA_PATHS) + 1)

            synced, paths = MODULE.synced_manifest(schema_manifest, root=root)
            policy_row = synced["artifacts"][-1]
            policy_bytes = (policy_dir / "canonical-stale-queue-recovery.v1.json").read_bytes()
            self.assertEqual(synced["artifact_count"], len(QUEUE_SCHEMA_PATHS) + 2)
            self.assertEqual(paths, [POLICY_PATH])
            self.assertEqual(
                [row["path"] for row in synced["artifacts"] if row["kind"] == "schema"],
                sorted(QUEUE_SCHEMA_PATHS),
            )
            self.assertEqual(synced["artifacts"][-2], manifest["artifacts"][0])
            self.assertEqual(policy_row, {
                "path": POLICY_PATH,
                "kind": "canonical_stale_queue_recovery_policy",
                "schema": POLICY_SCHEMA_URI,
                "bytes": len(policy_bytes),
                "sha256": hashlib.sha256(policy_bytes).hexdigest(),
            })
            self.assertNotIn("authority_ticket", policy_row)
            synced_again, paths_again = MODULE.synced_manifest(synced, root=root)
            self.assertEqual(synced_again, synced)
            self.assertEqual(paths_again, [POLICY_PATH])

            release_schema = json.loads((ROOT / "schemas/datapan.release-manifest.v1.schema.json").read_text())
            jsonschema.Draft202012Validator.check_schema(release_schema)
            jsonschema.Draft202012Validator(release_schema, format_checker=jsonschema.FormatChecker()).validate(synced)
            policy_schema = json.loads((ROOT / QUEUE_SCHEMA_PATHS[2]).read_text())
            jsonschema.Draft202012Validator.check_schema(policy_schema)
            self.assertEqual(policy_schema["$id"], POLICY_SCHEMA_URI)
            jsonschema.Draft202012Validator(policy_schema, format_checker=jsonschema.FormatChecker()).validate(
                json.loads(policy_bytes)
            )
            for relative in QUEUE_SCHEMA_PATHS:
                schema = json.loads((ROOT / relative).read_text())
                jsonschema.Draft202012Validator.check_schema(schema)
            entries = [SCHEMA_SYNC.schema_entry(root / relative) for relative in sorted(QUEUE_SCHEMA_PATHS)]
            self.assertEqual({entry["id"] for entry in entries}, {
                json.loads((ROOT / relative).read_text())["$id"] for relative in QUEUE_SCHEMA_PATHS
            })

    def test_check_mode_reports_missing_policy_without_writing_then_passes_after_write(self) -> None:
        root = self.fixture_root(with_policy=True)
        try:
            manifest_path = root / "manifest.json"
            before = manifest_path.read_bytes()
            missing_check = self.run_cli(root, "--check")
            self.assertEqual(missing_check.returncode, 1)
            self.assertIn(POLICY_PATH, missing_check.stderr)
            self.assertEqual(manifest_path.read_bytes(), before)

            write = self.run_cli(root, "--write")
            self.assertEqual(write.returncode, 0, write.stderr)
            registered = json.loads(manifest_path.read_text())
            self.assertEqual(registered["artifact_count"], len(registered["artifacts"]))
            self.assertEqual(registered["artifacts"][-1]["path"], POLICY_PATH)

            after_write = manifest_path.read_bytes()
            check = self.run_cli(root, "--check")
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(manifest_path.read_bytes(), after_write)
        finally:
            shutil.rmtree(root)

    def test_missing_policy_file_and_duplicate_or_conflicting_rows_fail_closed(self) -> None:
        missing_root = self.fixture_root(with_policy=False)
        try:
            manifest = json.loads((missing_root / "manifest.json").read_text())
            with self.assertRaisesRegex(ValueError, "manifest artifact is missing"):
                MODULE.synced_manifest(manifest, root=missing_root)
            before = (missing_root / "manifest.json").read_bytes()
            check = self.run_cli(missing_root, "--check")
            self.assertEqual(check.returncode, 1)
            self.assertEqual((missing_root / "manifest.json").read_bytes(), before)
        finally:
            shutil.rmtree(missing_root)

        conflicting_rows = (
            {"path": POLICY_PATH, "kind": "schema", "schema": POLICY_SCHEMA_URI},
            {"path": POLICY_PATH, "kind": "canonical_stale_queue_recovery_policy", "schema": "https://schemas.datapan.dev/wrong.json"},
            {"path": POLICY_PATH, "kind": "canonical_stale_queue_recovery_policy", "schema": POLICY_SCHEMA_URI, "authority_ticket": "StatPan/datapan-registry#767"},
        )
        root = self.fixture_root(with_policy=True)
        try:
            base = json.loads((root / "manifest.json").read_text())
            for row in conflicting_rows:
                with self.subTest(row=row):
                    candidate = json.loads(json.dumps(base))
                    candidate["artifacts"].append(row)
                    candidate["artifact_count"] += 1
                    with self.assertRaisesRegex(ValueError, "conflicts|unsupported metadata"):
                        MODULE.synced_manifest(candidate, root=root)

            duplicate = json.loads(json.dumps(base))
            duplicate["artifacts"].extend([
                {"path": POLICY_PATH, "kind": "canonical_stale_queue_recovery_policy", "schema": POLICY_SCHEMA_URI},
                {"path": POLICY_PATH, "kind": "canonical_stale_queue_recovery_policy", "schema": POLICY_SCHEMA_URI},
            ])
            duplicate["artifact_count"] += 2
            with self.assertRaisesRegex(ValueError, "duplicate fixed queue policy"):
                MODULE.synced_manifest(duplicate, root=root)
        finally:
            shutil.rmtree(root)

    def test_release_ledger_ownership_covers_policy_and_rejects_missing_or_duplicate_owner(self) -> None:
        manifest = json.loads((ROOT / "manifest.json").read_text())
        manifest["artifacts"].extend(self.queue_schema_additions())
        policy_bytes = (ROOT / POLICY_PATH).read_bytes()
        manifest["artifacts"].append({
            "path": POLICY_PATH,
            "kind": "canonical_stale_queue_recovery_policy",
            "schema": POLICY_SCHEMA_URI,
            "bytes": len(policy_bytes),
            "sha256": hashlib.sha256(policy_bytes).hexdigest(),
        })
        manifest["artifact_count"] = len(manifest["artifacts"])
        ownership = json.loads((ROOT / "docs/release-ledger-ownership.json").read_text())

        kinds, entries = LEDGER.validate_ownership(manifest, ownership)
        self.assertGreaterEqual(kinds, 1)
        self.assertEqual(entries, len(ownership["entries"]))

        missing = json.loads(json.dumps(ownership))
        missing["entries"] = [entry for entry in missing["entries"] if entry.get("id") != "canonical_stale_queue_recovery_policy"]
        with self.assertRaisesRegex(ValueError, "missing release-ledger ownership: canonical_stale_queue_recovery_policy"):
            LEDGER.validate_ownership(manifest, missing)

        duplicate = json.loads(json.dumps(ownership))
        duplicate["entries"].append(json.loads(json.dumps(next(
            entry for entry in duplicate["entries"] if entry.get("id") == "canonical_stale_queue_recovery_policy"
        ))))
        with self.assertRaisesRegex(ValueError, "covered by both"):
            LEDGER.validate_ownership(manifest, duplicate)

    def test_registry_identity_is_not_replaced_by_lfs_pointer_identity(self) -> None:
        root = self.fixture_root(with_policy=True)
        try:
            manifest = json.loads((root / "manifest.json").read_text())
            synced, paths = MODULE.synced_manifest(manifest, root=root)
            self.assertEqual(synced["artifacts"][0], manifest["artifacts"][0])
            self.assertEqual(paths, [POLICY_PATH])
            self.assertEqual(synced["artifact_count"], 2)
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
