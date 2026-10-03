from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import jsonschema


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

    def lfs_fixture(self, root: pathlib.Path, payload: bytes = b"candidate registry\n"):
        digest = hashlib.sha256(payload).hexdigest()
        manifest = root / "manifest.json"
        policy = root / "policy.json"
        output = root / "data" / "registry.json"
        manifest.write_text(json.dumps({
            "schema_version": "datapan.release-manifest.v1",
            "source_registry": "data/registry.json",
            "artifacts": [{"path": "data/registry.json", "bytes": len(payload), "sha256": digest}],
        }))
        policy.write_text(json.dumps({
            "canonical_registry": {
                "repository": "StatPan/datapan-registry",
                "revision": "1" * 40,
                "path": "data/registry.json",
                "manifest_sha256": "0" * 64,
            },
            "preparation_backend": {
                "provider": "github-git-lfs",
                "repository": "StatPan/datapan-registry",
                "path": "data/registry.json",
                "manifest_binding": "release-manifest-sha256",
                "upload": "object-oid-only",
                "readback": "isolated-lfs-storage",
            },
        }))
        manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{digest}\n"
            f"size {len(payload)}\n"
        ).encode("ascii")
        committed_policy = policy.read_bytes()
        commit = "a" * 40
        calls: list[list[str]] = []

        def fake_git(arguments: list[str], _cwd: pathlib.Path, **_kwargs):
            calls.append(arguments)
            if arguments == ["rev-parse", "HEAD"]:
                return b"a" * 40
            if arguments == ["show", f"{commit}:manifest.json"]:
                return manifest.read_bytes()
            if arguments == ["show", f"{commit}:policy.json"]:
                return committed_policy
            if arguments == ["remote", "get-url", "origin"]:
                return b"https://github.com/StatPan/datapan-registry.git\n"
            if arguments[:3] == ["check-attr", "filter", "--"]:
                return b"data/registry.json: filter: lfs\n"
            if arguments[0:2] == ["cat-file", "-e"]:
                return b""
            if arguments == ["show", f"{commit}:data/registry.json"]:
                return pointer
            if arguments[0] == "-c" and arguments[2:4] == ["lfs", "fetch"]:
                storage = pathlib.Path(arguments[1].removeprefix("lfs.storage="))
                object_path = storage / "objects" / digest[:2] / digest[2:4] / digest
                object_path.parent.mkdir(parents=True)
                object_path.write_bytes(payload)
                return b""
            self.fail(f"unexpected git command: {arguments}")

        return manifest, policy, output, payload, digest, manifest_digest, commit, calls, fake_git

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

    def test_policy_schema_keeps_published_hf_pin_separate_from_git_lfs_preparation(self) -> None:
        root = pathlib.Path(__file__).parents[1]
        policy = json.loads((root / "policy/registry-distribution.json").read_text(encoding="utf-8"))
        schema = json.loads((root / "schemas/datapan.registry-distribution.v1.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(policy)
        self.assertEqual(policy["canonical_registry"]["provider"], "huggingface-dataset")
        self.assertEqual(
            policy["canonical_registry"],
            {
                "provider": "huggingface-dataset",
                "repository": "StatPan/datapan-registry",
                "revision": "7b5d3d37973308ba8ff3558809c1ff4ed217768d",
                "path": "data/data-go-kr.registry.json",
                "manifest_sha256": "eeda72ee8590f458de8d75703662578e80edf3e61282f0e5e67547c4f6e5f644",
            },
        )
        self.assertEqual(policy["preparation_backend"]["provider"], "github-git-lfs")
        self.assertEqual(policy["preparation_backend"]["manifest_binding"], "release-manifest-sha256")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("policy/registry-distribution.json", {item["path"] for item in manifest["artifacts"]})

    def test_auto_materializer_reads_manifest_bound_lfs_from_fresh_storage_without_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest, policy, output, payload, digest, manifest_digest, commit, calls, fake_git = self.lfs_fixture(root)
            with mock.patch.object(MODULE, "git_output", side_effect=fake_git):
                result = MODULE.materialize(policy, manifest, output, candidate_commit=commit)
            self.assertEqual(result["status"], "materialized_after_remote_readback")
            self.assertEqual(result["manifest_sha256"], manifest_digest)
            self.assertEqual(result["policy_path"], "policy.json")
            self.assertEqual(result["policy_bytes"], policy.stat().st_size)
            self.assertEqual(result["policy_sha256"], hashlib.sha256(policy.read_bytes()).hexdigest())
            self.assertEqual(result["sha256"], digest)
            self.assertEqual(output.read_bytes(), payload)
            self.assertTrue(any(args[0] == "-c" and args[2:4] == ["lfs", "fetch"] for args in calls))
            self.assertFalse(any("push" in args for args in calls))

    def test_working_policy_must_match_the_exact_candidate_commit_before_readback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest, policy, output, _payload, _digest, manifest_digest, commit, calls, fake_git = self.lfs_fixture(root)
            pinned_policy = policy.read_bytes()
            policy.write_bytes(pinned_policy + b" ")
            with mock.patch.object(MODULE, "git_output", side_effect=fake_git):
                with self.assertRaisesRegex(MODULE.IntegrityError, "do not match the exact candidate commit"):
                    MODULE.materialize(
                        policy, manifest, output, candidate_commit=commit,
                        expected_manifest_sha256=manifest_digest,
                    )
            self.assertFalse(any(args[0] == "-c" and args[2:4] == ["lfs", "fetch"] for args in calls))

    def test_lfs_remote_mismatch_does_not_replace_existing_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest, policy, output, _payload, _digest, manifest_digest, commit, calls, fake_git = self.lfs_fixture(root)
            output.parent.mkdir(parents=True)
            output.write_bytes(b"previous candidate\n")

            def corrupt_remote(arguments: list[str], cwd: pathlib.Path, **kwargs):
                if arguments[0] == "-c" and arguments[2:4] == ["lfs", "fetch"]:
                    storage = pathlib.Path(arguments[1].removeprefix("lfs.storage="))
                    oid = hashlib.sha256(b"candidate registry\n").hexdigest()
                    object_path = storage / "objects" / oid[:2] / oid[2:4] / oid
                    object_path.parent.mkdir(parents=True)
                    object_path.write_bytes(b"mismatched remote object")
                    calls.append(arguments)
                    return b""
                return fake_git(arguments, cwd, **kwargs)

            previous = output.read_bytes()
            with mock.patch.object(MODULE, "git_output", side_effect=corrupt_remote):
                with self.assertRaisesRegex(MODULE.IntegrityError, "byte mismatch|SHA-256 mismatch|registry bytes expected"):
                    MODULE.materialize(policy, manifest, output, backend="github-git-lfs", candidate_commit=commit, expected_manifest_sha256=manifest_digest)
            self.assertEqual(output.read_bytes(), previous)
            self.assertFalse(any("push" in args for args in calls))

    def test_lfs_check_requires_exact_manifest_digest_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest, policy, output, _payload, _digest, manifest_digest, commit, calls, fake_git = self.lfs_fixture(root)
            with mock.patch.object(MODULE, "git_output", side_effect=fake_git):
                with self.assertRaisesRegex(MODULE.IntegrityError, "does not match the requested preparation identity"):
                    MODULE.materialize(policy, manifest, output, backend="github-git-lfs", candidate_commit=commit, expected_manifest_sha256="0" * 64, check_only=True)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
