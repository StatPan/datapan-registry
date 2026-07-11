from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "materialize-canonical-registry.py"
SPEC = importlib.util.spec_from_file_location("materialize_canonical_registry", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MaterializeCanonicalRegistryTest(unittest.TestCase):
    def fixture(self, root: pathlib.Path, payload: bytes = b"canonical\n") -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        digest = hashlib.sha256(payload).hexdigest()
        manifest = root / "manifest.json"
        policy = root / "policy.json"
        output = root / "data" / "registry.json"
        manifest.write_text(json.dumps({
            "source_registry": "data/registry.json",
            "artifacts": [{"path": "data/registry.json", "bytes": len(payload), "sha256": digest}],
        }))
        policy.write_text(json.dumps({"canonical_registry": {
            "repository": "StatPan/datapan-registry",
            "revision": "1" * 40,
            "path": "data/registry.json",
            "manifest_sha256": digest,
        }}))
        return manifest, policy, output

    def test_download_is_promoted_only_after_manifest_identity_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest, policy, output = self.fixture(root)

            def fake_download(_url: str, destination: pathlib.Path) -> None:
                destination.write_bytes(b"canonical\n")

            with mock.patch.object(MODULE, "download", fake_download):
                result = MODULE.materialize(policy, manifest, output)
            self.assertEqual(result["status"], "materialized")
            self.assertEqual(output.read_bytes(), b"canonical\n")

    def test_corrupt_download_is_integrity_failure_and_does_not_replace_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest, policy, output = self.fixture(root)
            output.parent.mkdir(parents=True)
            output.write_text("version https://git-lfs.github.com/spec/v1\n")

            def fake_download(_url: str, destination: pathlib.Path) -> None:
                destination.write_bytes(b"wrong")

            with mock.patch.object(MODULE, "download", fake_download):
                with self.assertRaises(MODULE.IntegrityError):
                    MODULE.materialize(policy, manifest, output)
            self.assertTrue(output.read_text().startswith("version https://git-lfs"))

    def test_stale_policy_is_rejected_before_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest, policy, output = self.fixture(root)
            value = json.loads(policy.read_text())
            value["canonical_registry"]["manifest_sha256"] = "0" * 64
            policy.write_text(json.dumps(value))
            with mock.patch.object(MODULE, "download") as download:
                with self.assertRaisesRegex(MODULE.IntegrityError, "stale"):
                    MODULE.materialize(policy, manifest, output)
            download.assert_not_called()

    def test_lfs_pointer_never_matches_canonical_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest, _policy, output = self.fixture(root)
            output.parent.mkdir(parents=True)
            output.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\nsize 10\n")
            value = MODULE.load_object(manifest)
            _path, size, digest = MODULE.registry_identity(value)
            with self.assertRaises(MODULE.IntegrityError):
                MODULE.validate(output, size, digest)


if __name__ == "__main__":
    unittest.main()
