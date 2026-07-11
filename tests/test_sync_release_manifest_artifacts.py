from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "sync-release-manifest-artifacts.py"
SPEC = importlib.util.spec_from_file_location("sync_release_manifest_artifacts", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SyncReleaseManifestArtifactsTest(unittest.TestCase):
    def test_registry_identity_is_not_replaced_by_lfs_pointer_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            pointer = root / "registry.json"
            pointer.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\nsize 137735169\n")
            manifest = {
                "artifacts": [{
                    "path": pointer.as_posix(), "kind": "registry",
                    "bytes": 137735169, "sha256": "e" * 64,
                }]
            }
            synced, paths = MODULE.synced_manifest(manifest)
            self.assertEqual(synced, manifest)
            self.assertEqual(paths, [])


if __name__ == "__main__":
    unittest.main()
