from __future__ import annotations

import contextlib
import copy
import errno
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
CHECKED_OUT_ROOT = ROOT
SCRIPT = ROOT / "scripts" / "generate-completeness-proof-rollup.py"
SPEC = importlib.util.spec_from_file_location("generate_completeness_proof_rollup", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
_PRESERVE_REPLAY_PR_NUMBER = object()

# The completeness packet validators are dynamically imported into an old-main
# fixture. Keep their source closure explicit so that the fixture executes the
# tested branch's code and schemas while preserving the fixture's authenticated
# origin/main ref. Repository-input files and published artifacts retain their
# own byte/SHA pins; unmanifested validator sources are pinned to this test
# process's exact Git commit. A generated, non-indexed schemas/index.json is
# materialized from that commit rather than from an ambient generated worktree.
COMPLETENESS_VALIDATOR_CODE_DEPENDENCIES = frozenset({
    "scripts/validate-completeness-proof.py",
    "scripts/validate-data-go-kr-operation-manifest.py",
    "scripts/generate-data-go-kr-operation-manifest.py",
    "scripts/seoul_oa109_operation_declaration.py",
    "scripts/attest-runtime-freshness-import.py",
    "scripts/run-canonical-update-promotion.py",
    "scripts/canonical_update_pr.py",
    "scripts/canonical_update_ci.py",
    "scripts/compose-upstream-catalogue-candidate.py",
    "scripts/upstream_catalogue_derivation.py",
    "scripts/upstream_catalogue_handoff.py",
    "scripts/materialize-canonical-registry.py",
    "scripts/refresh-canonical-snapshot-evidence.py",
    "scripts/persist-upstream-catalogue-health.py",
    "scripts/recover-canonical-publication-ack.py",
    "scripts/completeness_publication_evidence.py",
})
COMPLETENESS_VALIDATOR_SCHEMA_DEPENDENCIES = frozenset({
    "schemas/datapan.operation-denominator.v1.schema.json",
    "schemas/datapan.data-go-kr-operation-manifest.v1.schema.json",
    "schemas/datapan.data-go-kr-operation-denominator-expectation.v1.schema.json",
    "schemas/datapan.runtime-freshness-import-admission.v1.schema.json",
    "schemas/datapan.runtime-freshness-import-receipt.v1.schema.json",
    "schemas/datapan.runtime-freshness-import-attestation.v1.schema.json",
    "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
    "schemas/datapan.catalogue-composition-receipt.v1.schema.json",
    "schemas/datapan.canonical-update-promotion-journal.v1.schema.json",
    "schemas/datapan.canonical-update-promotion-receipt.v1.schema.json",
    "schemas/datapan.canonical-update-promotion-terminal-outcome.v1.schema.json",
    "schemas/datapan.catalog-diff.v1.schema.json",
    "schemas/datapan.catalogue-enrichment-evidence.v1.schema.json",
    "schemas/datapan.completeness-proof.v1.schema.json",
    "schemas/datapan.completeness-proof-policy.v1.schema.json",
    "schemas/datapan.completeness-proof-rollup.v1.schema.json",
    "schemas/datapan.completeness-proof-scopes.v1.schema.json",
    "schemas/datapan.completeness-proof-inputs.v1.schema.json",
    "schemas/datapan.completeness-proof-identities.v1.schema.json",
    "schemas/datapan.upstream-catalogue-health-policy.v1.schema.json",
    "schemas/datapan.upstream-catalogue-health-state-owner.v1.schema.json",
    "schemas/datapan.upstream-catalogue-health-state.v1.schema.json",
    "schemas/datapan.upstream-catalogue-health.v1.schema.json",
    "schemas/datapan.upstream-refresh-evidence.v1.schema.json",
    "schemas/datapan.diagnostic-current-source-applicability.v1.schema.json",
    "schemas/datapan.diagnostic-envelope.v1.schema.json",
    "schemas/datapan.provider-index.v1.schema.json",
    "schemas/datapan.specs.v1.schema.json",
    "schemas/index.json",
})
COMPLETENESS_VALIDATOR_SOURCE_DEPENDENCIES = (
    COMPLETENESS_VALIDATOR_CODE_DEPENDENCIES | COMPLETENESS_VALIDATOR_SCHEMA_DEPENDENCIES
)


def _fixture_file_snapshot(path: pathlib.Path) -> tuple[int, ...]:
    value = pathlib.Path(path).lstat()
    if not stat.S_ISREG(value.st_mode):
        raise AssertionError(f"fixture payload is not a regular file: {path}")
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def _stream_fixture_file_identity(path: pathlib.Path) -> tuple[dict[str, object], tuple[int, ...]]:
    """Hash one stable regular fixture file without retaining its payload."""
    path = pathlib.Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AssertionError(f"fixture payload cannot be opened safely: {path}") from exc
    digest = hashlib.sha256()
    total = 0
    try:
        stream = os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise AssertionError(f"fixture payload is not a regular file: {path}")
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    before_snapshot = (
        before.st_dev, before.st_ino, before.st_mode, before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
    )
    after_snapshot = (
        after.st_dev, after.st_ino, after.st_mode, after.st_size,
        after.st_mtime_ns, after.st_ctime_ns,
    )
    if before_snapshot != after_snapshot or total != before.st_size:
        raise AssertionError(f"fixture payload changed while it was hashed: {path}")
    return {"bytes": total, "sha256": digest.hexdigest()}, after_snapshot


def _materialize_exact_fixture_file(
    source: pathlib.Path, destination: pathlib.Path,
) -> dict[str, object]:
    """Hardlink one fixture payload, copying only across filesystems."""
    source = pathlib.Path(source)
    destination = pathlib.Path(destination)
    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copy2(source, destination)
        source_identity, source_snapshot = _stream_fixture_file_identity(source)
        destination_identity, destination_snapshot = _stream_fixture_file_identity(destination)
        if source_identity != destination_identity:
            raise AssertionError("cross-filesystem fixture copy differs from its source payload")
        # Re-read metadata after both streams so a source or destination change
        # between the two hashes cannot be accepted as an exact copy.
        if (
            source_snapshot != _fixture_file_snapshot(source)
            or destination_snapshot != _fixture_file_snapshot(destination)
        ):
            raise AssertionError("cross-filesystem fixture copy changed after verification")
        return {
            "method": "copy2",
            "source": source_identity,
            "destination": destination_identity,
        }

    source_snapshot = _fixture_file_snapshot(source)
    destination_snapshot = _fixture_file_snapshot(destination)
    if (
        source_snapshot[:2] != destination_snapshot[:2]
    ):
        raise AssertionError("fixture hardlink does not share its source inode")
    identity, snapshot = _stream_fixture_file_identity(source)
    final_source_snapshot = _fixture_file_snapshot(source)
    final_destination_snapshot = _fixture_file_snapshot(destination)
    if (
        snapshot != final_source_snapshot
        or final_source_snapshot != final_destination_snapshot
    ):
        raise AssertionError("fixture hardlink changed during verification")
    return {"method": "hardlink", "source": identity, "destination": dict(identity)}


class CompletenessProofRollupTest(unittest.TestCase):
    @staticmethod
    def _git_output(root: pathlib.Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed in fixture setup: {result.stderr[-1000:]}"
            )
        return result.stdout.rstrip("\n")

    def _inherit_native_borrowed_shallow_boundary(
        self, donor_root: pathlib.Path, child_root: pathlib.Path,
    ) -> tuple[pathlib.Path, str, str, bytes | None, bytes]:
        """Keep borrowed fixture ancestry aligned with its native Git boundary."""
        donor_root = donor_root.resolve()
        common_dir = pathlib.Path(self._git_output(
            donor_root, "rev-parse", "--path-format=absolute", "--git-common-dir",
        )).resolve()
        self.assertTrue(common_dir.is_absolute())
        donor_shallow_path = pathlib.Path(self._git_output(
            donor_root, "rev-parse", "--path-format=absolute", "--git-path", "shallow",
        )).resolve()
        self.assertEqual(donor_shallow_path, common_dir / "shallow")
        donor_main = self._git_output(
            donor_root, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}",
        )
        donor_refs = self._git_output(donor_root, "for-each-ref", "--format=%(refname) %(objectname)").encode()
        shallow_state = self._git_output(donor_root, "rev-parse", "--is-shallow-repository")
        self.assertIn(shallow_state, {"true", "false"})
        shallow_path = common_dir / "shallow"
        child_refs = self._git_output(
            child_root, "for-each-ref", "--format=%(refname) %(objectname)",
        ).encode()
        child_git_dir = pathlib.Path(self._git_output(
            child_root, "rev-parse", "--path-format=absolute", "--git-dir",
        )).resolve()
        self.assertEqual(child_git_dir, (child_root / ".git").resolve())
        child_shallow = child_git_dir / "shallow"
        self.assertFalse(child_shallow.exists() or child_shallow.is_symlink())
        alternate = child_git_dir / "objects/info/alternates"
        self.assertTrue(stat.S_ISREG(alternate.lstat().st_mode))
        self.assertFalse(alternate.is_symlink())
        self.assertEqual(
            alternate.read_bytes(), str(common_dir / "objects").encode() + b"\n",
            "borrowed child alternate must point to the donor's exact common object directory",
        )

        shallow_bytes: bytes | None
        if shallow_state == "true":
            metadata = shallow_path.lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode), "native shallow boundary must be a regular file")
            shallow_bytes = shallow_path.read_bytes()
            object_format = self._git_output(donor_root, "rev-parse", "--show-object-format")
            oid_bytes = {"sha1": 40, "sha256": 64}.get(object_format)
            self.assertIsNotNone(oid_bytes, "fixture Git object format must be recognized")
            lines = shallow_bytes.splitlines()
            self.assertTrue(lines, "native shallow boundary must be nonempty")
            self.assertTrue(shallow_bytes.endswith(b"\n"), "native shallow boundary must end on an OID line")
            self.assertEqual(len(set(lines)), len(lines), "native shallow boundary contains duplicate OIDs")
            for line in lines:
                self.assertEqual(len(line), oid_bytes)
                self.assertTrue(all(byte in b"0123456789abcdef" for byte in line))
                boundary_oid = line.decode("ascii")
                self.assertEqual(
                    self._git_output(donor_root, "cat-file", "-t", boundary_oid), "commit",
                    f"native shallow boundary commit {boundary_oid} must be available in the donor",
                )

            with child_shallow.open("xb") as stream:
                stream.write(shallow_bytes)
            self.assertEqual(child_shallow.read_bytes(), shallow_bytes)
            self.assertEqual(
                self._git_output(child_root, "rev-parse", "--is-shallow-repository"), "true",
            )
            self.assertEqual(
                pathlib.Path(self._git_output(
                    child_root, "rev-parse", "--path-format=absolute", "--git-path", "shallow",
                )).resolve(),
                child_shallow.resolve(),
            )
        else:
            self.assertFalse(shallow_path.exists() or shallow_path.is_symlink())
            shallow_bytes = None
            self.assertEqual(
                self._git_output(child_root, "rev-parse", "--is-shallow-repository"), "false",
            )
            self.assertFalse(child_shallow.exists())

        self.assertEqual(
            self._git_output(donor_root, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"),
            donor_main,
        )
        self.assertEqual(
            self._git_output(donor_root, "for-each-ref", "--format=%(refname) %(objectname)").encode(),
            donor_refs,
        )
        self.assertEqual(
            shallow_path.read_bytes() if shallow_bytes is not None else None,
            shallow_bytes,
        )
        self.assertEqual(
            self._git_output(child_root, "for-each-ref", "--format=%(refname) %(objectname)").encode(),
            child_refs,
            "installing native shallow metadata must not alter the child refs",
        )
        return common_dir, donor_main, shallow_state, shallow_bytes, donor_refs

    def _assert_native_borrowed_donor_unchanged(
        self,
        donor_root: pathlib.Path,
        snapshot: tuple[pathlib.Path, str, str, bytes | None, bytes],
    ) -> None:
        common_dir, donor_main, shallow_state, shallow_bytes, donor_refs = snapshot
        self.assertEqual(
            pathlib.Path(self._git_output(
                donor_root, "rev-parse", "--path-format=absolute", "--git-common-dir",
            )).resolve(),
            common_dir,
        )
        self.assertEqual(
            self._git_output(donor_root, "rev-parse", "--is-shallow-repository"), shallow_state,
        )
        self.assertEqual(
            self._git_output(donor_root, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"),
            donor_main,
        )
        self.assertEqual(
            self._git_output(donor_root, "for-each-ref", "--format=%(refname) %(objectname)").encode(),
            donor_refs,
        )
        shallow_path = common_dir / "shallow"
        if shallow_bytes is None:
            self.assertFalse(shallow_path.exists() or shallow_path.is_symlink())
        else:
            metadata = shallow_path.lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertFalse(shallow_path.is_symlink())
            self.assertEqual(shallow_path.read_bytes(), shallow_bytes)

    def test_borrowed_shallow_boundary_rejects_wrong_object_alternate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="completeness-wrong-borrowed-alternate-") as name:
            root = pathlib.Path(name)
            remote = root / "remote.git"
            seed = root / "seed"
            donor_root = root / "donor"
            subprocess.run(
                ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(remote)], check=True,
            )
            seed.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main"], cwd=seed, check=True,
            )
            for key, value in (("user.name", "Shallow donor fixture"), ("user.email", "shallow@example.invalid")):
                subprocess.run(["git", "config", key, value], cwd=seed, check=True)
            (seed / "history.txt").write_text("first\n")
            subprocess.run(["git", "add", "history.txt"], cwd=seed, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "first donor commit"], cwd=seed, check=True)
            with (seed / "history.txt").open("a") as stream:
                stream.write("second\n")
            subprocess.run(["git", "commit", "--quiet", "-am", "second donor commit"], cwd=seed, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=seed, check=True)
            subprocess.run(["git", "push", "--quiet", "origin", "HEAD:refs/heads/main"], cwd=seed, check=True)
            subprocess.run(
                ["git", "clone", "--quiet", "--no-tags", "--depth=1", "--branch", "main",
                 f"file://{remote}", str(donor_root)],
                check=True,
                env={**os.environ, "GIT_ALLOW_PROTOCOL": "file"},
            )
            self.assertEqual(self._git_output(donor_root, "rev-parse", "--is-shallow-repository"), "true")
            donor_common = pathlib.Path(self._git_output(
                donor_root, "rev-parse", "--path-format=absolute", "--git-common-dir",
            )).resolve()
            donor_main = self._git_output(
                donor_root, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}",
            )
            donor_refs = self._git_output(
                donor_root, "for-each-ref", "--format=%(refname) %(objectname)",
            ).encode()
            donor_shallow = donor_common / "shallow"
            donor_shallow_bytes = donor_shallow.read_bytes()
            child_root = root / "child"
            child_root.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main"], cwd=child_root, check=True,
            )
            alternate = child_root / ".git/objects/info/alternates"
            alternate.parent.mkdir(parents=True, exist_ok=True)
            alternate.write_text(str(child_root / ".git/objects") + "\n")
            with self.assertRaisesRegex(
                AssertionError, "borrowed child alternate must point to the donor's exact common object directory",
            ):
                self._inherit_native_borrowed_shallow_boundary(donor_root, child_root)
            self.assertFalse((child_root / ".git/shallow").exists())
            self.assertEqual(self._git_output(child_root, "rev-parse", "--is-shallow-repository"), "false")
            self.assertEqual(
                self._git_output(donor_root, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"),
                donor_main,
            )
            self.assertEqual(
                self._git_output(donor_root, "for-each-ref", "--format=%(refname) %(objectname)").encode(),
                donor_refs,
            )
            self.assertEqual(donor_shallow.read_bytes(), donor_shallow_bytes)

    PRECONTRACT_OUTPUT_COMMIT = "0a97b3540c4b72f4e4a1cd487266af192e3b9fbc"
    PRECONTRACT_OUTPUTS = {
        MODULE.OUTPUT_JSON_PATH: (57069, "cf5e8debfb97d3e9e6ec09a582fcebd271a1a92cb3b61687cd1b6a2a9695e6ac"),
        MODULE.OUTPUT_MARKDOWN_PATH: (23026, "b8d85de5929282c8e4ee616f6abd5a481d82555a7a01fed536d59fa62dbccdd7"),
    }
    ROUNDTRIP_OUTPUTS = (
        "manifest.json",
        MODULE.OUTPUT_JSON_PATH,
        MODULE.OUTPUT_MARKDOWN_PATH,
    )
    EXPECTED_SCOPE_IDS = {
        "data-go-kr.api-catalog",
        "data-go-kr.api-operations",
        "ecos.api-operations",
        "kosis.api-operations",
        "open-assembly.api-operations",
        "seoul-open-data.api-operations",
        "data-go-kr.file-datasets",
        "curated-payload.snapshot-portfolio",
        "curated-payload.rows",
    }
    FIXED_QUEUE_POLICY_ROW = {
        "path": "policy/canonical-stale-queue-recovery.v1.json",
        "kind": "canonical_stale_queue_recovery_policy",
        "schema": "https://statpan.com/schemas/datapan.canonical-stale-queue-recovery-policy.v1.schema.json",
    }

    def test_repository_generator_and_manifest_owner_stay_fixed_across_two_main_roundtrips(self) -> None:
        """The production repository-mode writer is stable after generated bytes become main."""
        environment = self._closed_git_environment()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        source_main = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{commit}"], cwd=ROOT, text=True, env=environment,
        ).strip()
        source_tree = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{source_main}^{{tree}}"],
            cwd=ROOT, text=True, env=environment,
        ).strip()
        source_object_dir = pathlib.Path(subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "objects"],
            cwd=ROOT, text=True, env=environment,
        ).strip())
        source_outputs = {
            relative: {
                "identity": _stream_fixture_file_identity(ROOT / relative)[0],
                "snapshot": _fixture_file_snapshot(ROOT / relative),
            }
            for relative in self.ROUNDTRIP_OUTPUTS
        }
        source_tracked_status_before = subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=no"],
            cwd=ROOT, text=True, env=environment,
        )
        trace_value = os.environ.get("DATAPAN_754_ROUNDTRIP_EVIDENCE_DIR")
        trace_dir = pathlib.Path(trace_value) if trace_value else None
        if trace_dir is not None:
            trace_dir.mkdir(parents=True, exist_ok=True)
        trace: dict[str, object] = {
            "schema_version": "datapan.registry.754-disposable-main-roundtrip-qa.v1",
            "source": {
                "head": source_main,
                "tree": source_tree,
                "generator_sha256": MODULE.sha256_file(ROOT / "scripts/generate-completeness-proof-rollup.py"),
                "manifest_owner_sha256": MODULE.sha256_file(ROOT / "scripts/sync-release-manifest-artifacts.py"),
                "test_sha256": MODULE.sha256_file(pathlib.Path(__file__).resolve()),
            },
            "precontract_output_preimage": {
                "commit": self.PRECONTRACT_OUTPUT_COMMIT,
                "tree": subprocess.check_output(
                    ["git", "rev-parse", "--verify", f"{self.PRECONTRACT_OUTPUT_COMMIT}^{{tree}}"],
                    cwd=ROOT, text=True, env=environment,
                ).strip(),
                "files": {},
            },
            "current_manifest_preimage": {},
            "source_preservation": {
                "tracked_status_before": source_tracked_status_before,
                "outputs_before": {
                    relative: value["identity"] for relative, value in source_outputs.items()
                },
                "output_snapshots_before": {
                    relative: value["snapshot"] for relative, value in source_outputs.items()
                },
            },
            "production_commands": [],
            "rounds": [],
        }

        def persist_trace() -> None:
            if trace_dir is None:
                return
            (trace_dir / "ROUNDTRIP-TRACE.json").write_text(
                json.dumps(trace, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        self.addCleanup(persist_trace)
        canonical_payload_sha256 = "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0"
        canonical_payload_bytes = 139155499
        source_registry = ROOT / "data/data-go-kr.registry.json"
        source_registry_identity, _source_registry_snapshot = _stream_fixture_file_identity(source_registry)
        if (source_registry_identity["bytes"], source_registry_identity["sha256"]) != (
            canonical_payload_bytes, canonical_payload_sha256,
        ):
            payload_value = os.environ.get("DATAPAN_754_CANONICAL_REGISTRY_PAYLOAD")
            payload_path = pathlib.Path(payload_value) if payload_value else None
            if payload_path is None or not payload_path.is_file():
                self.skipTest("the retained canonical Registry LFS payload is not materialized")
            payload_identity, _payload_snapshot = _stream_fixture_file_identity(payload_path)
            if (payload_identity["bytes"], payload_identity["sha256"]) != (
                canonical_payload_bytes, canonical_payload_sha256,
            ):
                self.fail("the supplied retained canonical Registry payload has the wrong identity")
        else:
            payload_identity = source_registry_identity
            payload_path = source_registry
        trace["canonical_payload"] = {
            "bytes": payload_identity["bytes"],
            "sha256": payload_identity["sha256"],
            "source_path": str(payload_path),
            "materialized_only_in_disposable_repository": True,
        }

        with tempfile.TemporaryDirectory(prefix="completeness-owning-main-roundtrip-") as name:
            repository = pathlib.Path(name) / "repo"
            self._materialize_source_commit_tree(
                ROOT, source_main, repository, environment=environment,
            )
            temp_registry = repository / "data/data-go-kr.registry.json"
            temp_registry_identity, _temp_registry_snapshot = _stream_fixture_file_identity(temp_registry)
            if (temp_registry_identity["bytes"], temp_registry_identity["sha256"]) != (
                canonical_payload_bytes, canonical_payload_sha256,
            ):
                # The committed tree contains the authenticated LFS pointer.
                # Reuse the payload_path already validated above; do not make
                # the disposable checkout perform a second ambient lookup.
                payload_identity_before_copy, _payload_snapshot = _stream_fixture_file_identity(payload_path)
                self.assertEqual((payload_identity_before_copy["bytes"], payload_identity_before_copy["sha256"]), (
                    canonical_payload_bytes, canonical_payload_sha256,
                ))
                temp_registry.unlink()
                shutil.copy2(payload_path, temp_registry)
                copied_identity, _copied_snapshot = _stream_fixture_file_identity(temp_registry)
                self.assertEqual((copied_identity["bytes"], copied_identity["sha256"]), (
                    canonical_payload_bytes, canonical_payload_sha256,
                ))
            manifest_path = repository / "manifest.json"
            current_manifest_raw = subprocess.check_output(
                ["git", "show", f"{source_main}:manifest.json"], cwd=ROOT, env=environment,
            )
            current_manifest = json.loads(current_manifest_raw)
            current_artifact_paths = [
                row.get("path") for row in current_manifest.get("artifacts", [])
                if isinstance(row, dict)
            ]
            self.assertEqual(len(current_artifact_paths), len(current_manifest.get("artifacts", [])))
            self.assertEqual(current_manifest.get("artifact_count"), len(current_artifact_paths))
            source_policy_rows = [
                row for row in current_manifest["artifacts"]
                if row.get("path") == self.FIXED_QUEUE_POLICY_ROW["path"]
            ]
            self.assertLessEqual(len(source_policy_rows), 1)
            for row in source_policy_rows:
                for key, value in self.FIXED_QUEUE_POLICY_ROW.items():
                    self.assertEqual(row.get(key), value)
                self.assertLessEqual(
                    set(row), set(self.FIXED_QUEUE_POLICY_ROW) | {"bytes", "sha256"},
                )
            expected_owner_artifact_paths = list(current_artifact_paths)
            if not source_policy_rows:
                expected_owner_artifact_paths.append(self.FIXED_QUEUE_POLICY_ROW["path"])
            for relative in (MODULE.OUTPUT_JSON_PATH, MODULE.OUTPUT_MARKDOWN_PATH):
                self.assertEqual(current_artifact_paths.count(relative), 1)
            manifest_path.unlink()
            manifest_path.write_bytes(current_manifest_raw)
            trace_manifest = trace["current_manifest_preimage"]
            assert isinstance(trace_manifest, dict)
            trace_manifest.update({
                "source_commit": source_main,
                "bytes": len(current_manifest_raw),
                "sha256": MODULE.sha256_bytes(current_manifest_raw),
                "artifact_count": current_manifest["artifact_count"],
                "artifact_path_count": len(current_artifact_paths),
                "all_current_registrations_retained": True,
                "expected_owner_artifact_count": len(expected_owner_artifact_paths),
                "fixed_owner_registration": self.FIXED_QUEUE_POLICY_ROW,
            })

            # Seed a real historical pre-contract output preimage. These exact
            # bytes come from the first committed completeness snapshot; the
            # checkout's ambient outputs may already be converged and cannot
            # serve as a repeatable stale baseline.
            precontract_bytes: dict[str, bytes] = {}
            for relative, (expected_bytes, expected_sha256) in self.PRECONTRACT_OUTPUTS.items():
                raw = subprocess.check_output(
                    ["git", "show", f"{self.PRECONTRACT_OUTPUT_COMMIT}:{relative}"],
                    cwd=ROOT, env=environment,
                )
                self.assertEqual((len(raw), MODULE.sha256_bytes(raw)), (expected_bytes, expected_sha256))
                precontract_bytes[relative] = raw
                trace_precontract = trace["precontract_output_preimage"]
                assert isinstance(trace_precontract, dict)
                files = trace_precontract["files"]
                assert isinstance(files, dict)
                files[relative] = {"bytes": len(raw), "sha256": expected_sha256}
                target = repository / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.unlink(missing_ok=True)
                target.write_bytes(raw)

            def git(*args: str, text: bool = True) -> str | bytes:
                result = subprocess.run(
                    ["git", *args], cwd=repository, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, check=False,
                )
                if result.returncode != 0:
                    raise AssertionError(
                        f"git {' '.join(args)} failed ({result.returncode}): "
                        f"{result.stderr!r}"
                    )
                return result.stdout.rstrip("\r\n")

            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main"],
                cwd=repository, env=environment, check=True,
            )
            for key, value in (
                ("user.name", "Completeness roundtrip test"),
                ("user.email", "completeness-roundtrip@example.invalid"),
                ("core.hooksPath", os.devnull),
                ("gc.auto", "0"),
            ):
                subprocess.run(
                    ["git", "config", "--local", key, value],
                    cwd=repository, env=environment, check=True,
                )
            alternate_file = repository / ".git/objects/info/alternates"
            alternate_file.parent.mkdir(parents=True, exist_ok=True)
            alternate_file.write_text(str(source_object_dir.resolve()) + "\n", encoding="ascii")
            git("update-ref", "refs/heads/main", source_main)
            git("update-ref", "refs/remotes/origin/main", source_main)
            git("symbolic-ref", "HEAD", "refs/heads/main")
            git("read-tree", source_main)
            self.assertEqual(git("rev-parse", f"{source_main}^{{tree}}"), git("rev-parse", "--verify", "HEAD^{tree}"))

            generator = [sys.executable, "scripts/generate-completeness-proof-rollup.py"]
            manifest_owner = [sys.executable, "scripts/sync-release-manifest-artifacts.py"]
            output_paths = self.ROUNDTRIP_OUTPUTS
            manifest_path = repository / "manifest.json"

            def assert_expected_tracked_drift() -> str:
                status = git("status", "--porcelain=v1", "--untracked-files=no")
                rows = status.splitlines() if status else []
                self.assertTrue(
                    all(row == " M data/data-go-kr.registry.json" for row in rows),
                    f"disposable repository has unexpected tracked drift: {status!r}",
                )
                self.assertFalse(any(relative in status for relative in output_paths))
                return status

            def run_production(
                argv: list[str], *, phase: str = "roundtrip",
            ) -> tuple[int, bytes, bytes]:
                started_at_ns = time.time_ns()
                started_monotonic = time.monotonic()
                result = subprocess.run(
                    argv, cwd=repository, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                )
                record: dict[str, object] = {
                    "phase": phase,
                    "argv": argv,
                    "exit_code": result.returncode,
                    "started_at_unix_ns": started_at_ns,
                    "elapsed_seconds": round(time.monotonic() - started_monotonic, 6),
                    "stdout_bytes": len(result.stdout),
                    "stdout_sha256": MODULE.sha256_bytes(result.stdout),
                    "stderr_bytes": len(result.stderr),
                    "stderr_sha256": MODULE.sha256_bytes(result.stderr),
                }
                production_commands = trace["production_commands"]
                assert isinstance(production_commands, list)
                production_commands.append(record)
                if trace_dir is not None:
                    command_number = len(production_commands)
                    (trace_dir / f"command-{command_number:02d}.stdout.raw").write_bytes(result.stdout)
                    (trace_dir / f"command-{command_number:02d}.stderr.raw").write_bytes(result.stderr)
                self.assertEqual(
                    result.returncode, 0,
                    f"{' '.join(argv)} failed ({result.returncode}): "
                    f"stdout={result.stdout[-2000:]!r} stderr={result.stderr[-4000:]!r}",
                )
                return result.returncode, result.stdout, result.stderr

            # Make the stale-output baseline internally consistent with the
            # real owner while retaining the current manifest's full path set.
            # This is fixture setup; later writes run generator then owner.
            run_production([*manifest_owner, "--write"], phase="baseline_owner_bind")

            # Commit the historical output preimage as a disposable base
            # descendant of the exact Source checkout. Only the report files
            # contain historical bytes; the current 201-row manifest structure
            # is retained and rebound by the real owner above.
            subprocess.run(
                ["git", "add", "--", *output_paths], cwd=repository,
                env=environment, check=True,
            )
            precontract_tree = str(git("write-tree"))
            precontract_commit = subprocess.check_output(
                [
                    "git", "commit-tree", precontract_tree, "-p", source_main,
                ], cwd=repository,
                input=b"test: seed authentic pre-contract completeness outputs\n",
                env={
                    **environment,
                    "GIT_AUTHOR_NAME": "Completeness roundtrip test",
                    "GIT_AUTHOR_EMAIL": "completeness-roundtrip@example.invalid",
                    "GIT_COMMITTER_NAME": "Completeness roundtrip test",
                    "GIT_COMMITTER_EMAIL": "completeness-roundtrip@example.invalid",
                    "GIT_AUTHOR_DATE": "2026-10-11T00:00:00Z",
                    "GIT_COMMITTER_DATE": "2026-10-11T00:00:00Z",
                },
            ).decode("ascii").strip()
            subprocess.run(
                ["git", "update-ref", "refs/heads/main", precontract_commit, source_main],
                cwd=repository, env=environment, check=True,
            )
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", precontract_commit, source_main],
                cwd=repository, env=environment, check=True,
            )
            git("read-tree", precontract_commit)
            self.assertNotEqual(precontract_tree, source_tree)
            self.assertEqual(git("rev-parse", "HEAD^{commit}"), precontract_commit)
            self.assertEqual(git("rev-parse", "refs/remotes/origin/main^{commit}"), precontract_commit)
            self.assertEqual(git("rev-parse", f"{precontract_commit}^{{tree}}"), precontract_tree)
            self.assertEqual(
                git("for-each-ref", "--format=%(refname)"),
                "refs/heads/main\nrefs/remotes/origin/main",
            )
            self.assertEqual(
                git("diff-tree", "--no-commit-id", "--name-only", "-r", precontract_commit),
                "\n".join(sorted(output_paths)),
                "the disposable baseline may change only manifest and generated report outputs",
            )
            baseline_tracked_status = assert_expected_tracked_drift()
            trace_precontract = trace["precontract_output_preimage"]
            assert isinstance(trace_precontract, dict)
            trace_precontract.update({
                "disposable_main_commit": precontract_commit,
                "disposable_main_tree": precontract_tree,
                "origin_main_commit": git("rev-parse", "refs/remotes/origin/main^{commit}"),
                "refs": git("for-each-ref", "--format=%(refname)"),
                "tracked_status": baseline_tracked_status,
            })

            def output_snapshot() -> dict[str, tuple[int, str, bytes]]:
                snapshot: dict[str, tuple[int, str, bytes]] = {}
                for relative in output_paths:
                    raw = (repository / relative).read_bytes()
                    snapshot[relative] = (len(raw), MODULE.sha256_bytes(raw), raw)
                return snapshot

            def check_manifest_subject() -> None:
                manifest = json.loads(manifest_path.read_bytes())
                manifest_artifact_paths = [
                    row.get("path") for row in manifest.get("artifacts", [])
                    if isinstance(row, dict)
                ]
                self.assertEqual(
                    sorted(manifest_artifact_paths),
                    sorted(expected_owner_artifact_paths),
                    "the production owner must preserve current paths and add only its fixed policy registration",
                )
                self.assertEqual(manifest.get("artifact_count"), len(expected_owner_artifact_paths))
                policy_rows = [
                    row for row in manifest.get("artifacts", [])
                    if isinstance(row, dict)
                    and row.get("path") == self.FIXED_QUEUE_POLICY_ROW["path"]
                ]
                self.assertEqual(len(policy_rows), 1)
                for key, value in self.FIXED_QUEUE_POLICY_ROW.items():
                    self.assertEqual(policy_rows[0].get(key), value)
                self.assertLessEqual(
                    set(policy_rows[0]), set(self.FIXED_QUEUE_POLICY_ROW) | {"bytes", "sha256"},
                )
                report = json.loads((repository / MODULE.OUTPUT_JSON_PATH).read_bytes())
                scope_ids = {
                    scope.get("scope_id") for scope in report.get("scopes", [])
                    if isinstance(scope, dict)
                }
                self.assertEqual(len(report.get("scopes", [])), 9)
                self.assertEqual(scope_ids, self.EXPECTED_SCOPE_IDS)
                self.assertEqual(report.get("summary", {}).get("registered_scopes"), 9)
                context = report["evaluation_context"]
                self.assertEqual(set(context), {"mode", "claim_scope", "inventory_subject"})
                self.assertEqual(context["mode"], "repository_evidence_snapshot")
                self.assertEqual(context["claim_scope"], "evaluated_inputs_only")
                self.assertEqual(set(context["inventory_subject"]), {"catalog", "operation_manifest"})
                self.assertNotIn("current_release_manifest_sha256", json.dumps(report, sort_keys=True))
                for relative in (MODULE.OUTPUT_JSON_PATH, MODULE.OUTPUT_MARKDOWN_PATH):
                    report_rows = [row for row in manifest["artifacts"] if row.get("path") == relative]
                    self.assertEqual(len(report_rows), 1)
                    report_row = report_rows[0]
                    report_raw = (repository / relative).read_bytes()
                    self.assertEqual(report_row.get("bytes"), len(report_raw))
                    self.assertEqual(report_row.get("sha256"), MODULE.sha256_bytes(report_raw))

            def refresh_and_check() -> dict[str, tuple[int, str, bytes]]:
                run_production([*generator, "--write"])
                run_production([*manifest_owner, "--write"])
                run_production([*generator, "--check"])
                run_production([*manifest_owner, "--check"])
                check_manifest_subject()
                return output_snapshot()

            def commit_generated_tree(parent: str, message: str) -> tuple[str, str]:
                subprocess.run(
                    ["git", "add", "--", *output_paths], cwd=repository,
                    env=environment, check=True,
                )
                tree = str(git("write-tree"))
                commit_environment = {
                    **environment,
                    "GIT_AUTHOR_NAME": "Completeness roundtrip test",
                    "GIT_AUTHOR_EMAIL": "completeness-roundtrip@example.invalid",
                    "GIT_COMMITTER_NAME": "Completeness roundtrip test",
                    "GIT_COMMITTER_EMAIL": "completeness-roundtrip@example.invalid",
                    "GIT_AUTHOR_DATE": "2026-10-11T00:00:00Z",
                    "GIT_COMMITTER_DATE": "2026-10-11T00:00:00Z",
                }
                commit = subprocess.check_output(
                    ["git", "commit-tree", tree, "-p", parent], cwd=repository,
                    input=(message + "\n").encode(), env=commit_environment,
                ).decode("ascii").strip()
                self.assertNotEqual(commit, parent)
                self.assertEqual(git("rev-parse", f"{commit}^{{tree}}"), tree)
                for relative in output_paths:
                    self.assertEqual(git("rev-parse", f"{commit}:{relative}"), git("hash-object", relative))
                subprocess.run(
                    ["git", "update-ref", "refs/heads/main", commit, parent],
                    cwd=repository, env=environment, check=True,
                )
                subprocess.run(
                    ["git", "update-ref", "refs/remotes/origin/main", commit, parent],
                    cwd=repository, env=environment, check=True,
                )
                self.assertEqual(git("rev-parse", "HEAD^{commit}"), commit)
                self.assertEqual(git("rev-parse", "refs/remotes/origin/main^{commit}"), commit)
                self.assertEqual(
                    git("for-each-ref", "--format=%(refname)"),
                    "refs/heads/main\nrefs/remotes/origin/main",
                )
                rounds = trace["rounds"]
                assert isinstance(rounds, list)
                rounds.append({
                    "parent_main": parent,
                    "generated_commit": commit,
                    "generated_tree": tree,
                    "advanced_main": git("rev-parse", "refs/heads/main^{commit}"),
                    "advanced_origin_main": git("rev-parse", "refs/remotes/origin/main^{commit}"),
                    "outputs": {
                        relative: {"bytes": value[0], "sha256": value[1]}
                        for relative, value in output_snapshot().items()
                    },
                })
                return commit, tree

            self.assertEqual(git("rev-parse", "HEAD^{commit}"), precontract_commit)
            base_outputs = output_snapshot()
            for relative, raw in precontract_bytes.items():
                self.assertEqual(
                    (base_outputs[relative][0], base_outputs[relative][1]),
                    (len(raw), MODULE.sha256_bytes(raw)),
                    f"disposable baseline lost its authentic pre-contract bytes for {relative}",
                )
            if trace_dir is not None:
                for relative, filename in (
                    ("manifest.json", "base-manifest.json"),
                    (MODULE.OUTPUT_JSON_PATH, "base-rollup.json"),
                    (MODULE.OUTPUT_MARKDOWN_PATH, "base-rollup.md"),
                ):
                    (trace_dir / filename).write_bytes(base_outputs[relative][2])
            first_outputs = refresh_and_check()
            self.assertNotEqual(
                first_outputs[MODULE.OUTPUT_JSON_PATH][1],
                base_outputs[MODULE.OUTPUT_JSON_PATH][1],
                "the first owner pass must replace the pre-contract snapshot",
            )
            self.assertNotEqual(
                first_outputs["manifest.json"][1], base_outputs["manifest.json"][1],
                "the first owner pass must bind the actual generated report digest",
            )
            self.assertNotEqual(
                first_outputs[MODULE.OUTPUT_MARKDOWN_PATH][1],
                base_outputs[MODULE.OUTPUT_MARKDOWN_PATH][1],
                "the first owner pass must replace the pre-contract Markdown snapshot",
            )
            first_main, first_tree = commit_generated_tree(
                precontract_commit, "test: commit generated completeness snapshot roundtrip one",
            )
            self.assertNotEqual(first_tree, precontract_tree)
            self.assertNotEqual(first_main, precontract_commit)
            first_main_snapshot = output_snapshot()
            self.assertEqual(first_main_snapshot, first_outputs)
            trace["first_main"] = {
                "commit": first_main,
                "tree": first_tree,
                "manifest_sha256": first_main_snapshot["manifest.json"][1],
                "rollup_json_sha256": first_main_snapshot[MODULE.OUTPUT_JSON_PATH][1],
                "rollup_markdown_sha256": first_main_snapshot[MODULE.OUTPUT_MARKDOWN_PATH][1],
            }
            if trace_dir is not None:
                for relative, filename in (
                    ("manifest.json", "first-main-manifest.json"),
                    (MODULE.OUTPUT_JSON_PATH, "first-main-rollup.json"),
                    (MODULE.OUTPUT_MARKDOWN_PATH, "first-main-rollup.md"),
                ):
                    (trace_dir / filename).write_bytes(first_main_snapshot[relative][2])
            self.assertEqual(
                git("rev-parse", "refs/remotes/origin/main^{tree}"), first_tree,
            )

            run_production([*generator, "--check"])
            run_production([*manifest_owner, "--check"])
            after_first_repeat = refresh_and_check()
            self.assertEqual(after_first_repeat, first_main_snapshot)

            second_outputs = refresh_and_check()
            self.assertEqual(second_outputs, first_main_snapshot)
            second_main, second_tree = commit_generated_tree(
                first_main, "test: commit generated completeness snapshot roundtrip two",
            )
            self.assertEqual(second_tree, first_tree)
            self.assertNotEqual(second_main, first_main)
            second_main_snapshot = output_snapshot()
            trace["second_main"] = {
                "commit": second_main,
                "tree": second_tree,
                "manifest_sha256": second_main_snapshot["manifest.json"][1],
                "rollup_json_sha256": second_main_snapshot[MODULE.OUTPUT_JSON_PATH][1],
                "rollup_markdown_sha256": second_main_snapshot[MODULE.OUTPUT_MARKDOWN_PATH][1],
            }
            if trace_dir is not None:
                for relative, filename in (
                    ("manifest.json", "second-main-manifest.json"),
                    (MODULE.OUTPUT_JSON_PATH, "second-main-rollup.json"),
                    (MODULE.OUTPUT_MARKDOWN_PATH, "second-main-rollup.md"),
                ):
                    (trace_dir / filename).write_bytes(second_main_snapshot[relative][2])
            run_production([*generator, "--check"])
            run_production([*manifest_owner, "--check"])
            after_second_repeat = refresh_and_check()
            self.assertEqual(after_second_repeat, second_main_snapshot)
            self.assertEqual(git("rev-parse", "refs/remotes/origin/main^{commit}"), second_main)
            self.assertEqual(git("rev-parse", "refs/heads/main^{commit}"), second_main)
            final_tracked_status = assert_expected_tracked_drift()
            trace["disposable_final"] = {
                "main": git("rev-parse", "refs/heads/main^{commit}"),
                "origin_main": git("rev-parse", "refs/remotes/origin/main^{commit}"),
                "tree": git("rev-parse", "HEAD^{tree}"),
                "tracked_status": final_tracked_status,
                "refs": git("for-each-ref", "--format=%(refname)"),
            }

        source_outputs_after = {
            relative: {
                "identity": _stream_fixture_file_identity(ROOT / relative)[0],
                "snapshot": _fixture_file_snapshot(ROOT / relative),
            }
            for relative in self.ROUNDTRIP_OUTPUTS
        }
        source_tracked_status_after = subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=no"],
            cwd=ROOT, text=True, env=environment,
        )
        source_head_after = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{commit}"], cwd=ROOT, text=True, env=environment,
        ).strip()
        source_tree_after = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True, env=environment,
        ).strip()
        trace["source_preservation"]["outputs_after"] = {
            relative: value["identity"] for relative, value in source_outputs_after.items()
        }
        trace["source_preservation"]["output_snapshots_after"] = {
            relative: value["snapshot"] for relative, value in source_outputs_after.items()
        }
        trace["source_preservation"]["tracked_status_after"] = source_tracked_status_after
        trace["source_preservation"]["head_after"] = source_head_after
        trace["source_preservation"]["tree_after"] = source_tree_after
        self.assertEqual(source_tracked_status_after, source_tracked_status_before)
        self.assertEqual(source_outputs_after, source_outputs)
        self.assertEqual(source_head_after, source_main)
        self.assertEqual(source_tree_after, source_tree)

    def test_indexed_011_processor_is_retained_as_report_only_evidence(self) -> None:
        report = MODULE.build_report(
            root=ROOT, input_root=ROOT,
            input_index_path=ROOT / MODULE.INPUT_INDEX_PATH,
        )
        _registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in scope_by_id.values()
            if scope.get("resource_kind") == "api_operation_manifest"
            and scope.get("source_id") == "data_go_kr"
        )
        row = next(scope for scope in report["scopes"] if scope["scope_id"] == operation_scope["scope_id"])
        pipeline = next(
            facet for facet in row["facets"]
            if facet["facet_id"] == "specification_pipeline"
        )
        details = pipeline["details"]
        self.assertEqual(details["processor_run_id"], "37204681028")
        self.assertEqual(details["processor_protocol"], "historical-eight-output-v1")
        self.assertFalse(details["producer_contract_matches_registered_inputs"])
        self.assertEqual(
            details["producer_contract_reason_code"],
            "historical_eight_output_protocol_not_current_eligible",
        )
        self.assertFalse(row["evaluated_claims"]["current"])
        self.assertFalse(row["evaluated_claims"]["updated"])
        self.assertFalse(any(key.startswith("current_input_contract_") for key in details))

    def test_snapshot_preserves_historical_processor_contract_reason_as_subject_fact(self) -> None:
        projected = MODULE._snapshot_facet_details({
            "current_input_contract_compatible": False,
            "current_input_contract_changed_paths": ["scripts/process-upstream-catalogue-candidate.py"],
            "current_input_contract_reason_code": "retained_processor_protocol_not_current",
            "processor_protocol": "historical-eight-output-v1",
        })
        self.assertEqual(projected, {
            "producer_contract_matches_registered_inputs": False,
            "producer_contract_changed_paths": ["scripts/process-upstream-catalogue-candidate.py"],
            "producer_contract_reason_code": "retained_processor_protocol_not_current",
            "processor_protocol": "historical-eight-output-v1",
        })
        self.assertFalse(any(key.startswith("current_input_contract_") for key in projected))

    def test_native_target_collector_is_skipped_for_nonmain_release_sources(self) -> None:
        for source_sha, trusted_sha in (("a" * 40, "b" * 40), ("b" * 40, "a" * 40)):
            with self.subTest(source_sha=source_sha[:1]), mock.patch.object(
                MODULE, "git_read_only", side_effect=AssertionError("non-main target must not collect")
            ) as git_read:
                value = MODULE.collect_current_target_evidence_for_cli(
                    root=ROOT,
                    release_source_sha=source_sha,
                    trusted_main_sha=trusted_sha,
                    evaluation_as_of="2026-10-06T00:00:00Z",
                )
                self.assertIsNone(value)
                git_read.assert_not_called()

    def test_current_target_reader_and_collector_share_one_absolute_deadline(self) -> None:
        source_sha = "a" * 40
        marker = object()
        adapter = mock.Mock()
        reader = object()
        adapter.GitHubCurrentTargetNativeReader.return_value = reader
        adapter.collect_current_target_evidence.return_value = marker
        deadline = time.monotonic() + 90.0
        environment = {
            "GITHUB_REPOSITORY": "StatPan/datapan-registry",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_RUN_ID": "123456789",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SHA": source_sha,
            "GH_TOKEN": "offline-test-token",
        }
        with (
            mock.patch.object(MODULE, "git_read_only", side_effect=(
                f"{source_sha}\n".encode("ascii"), f"{source_sha}\n".encode("ascii"),
            )),
            mock.patch.object(MODULE, "current_target_evidence_module", return_value=adapter),
            mock.patch.dict(MODULE.os.environ, environment, clear=False),
        ):
            result = MODULE.collect_current_target_evidence_for_cli(
                root=ROOT,
                release_source_sha=source_sha,
                trusted_main_sha=source_sha,
                evaluation_as_of="2026-10-06T00:00:00Z",
                deadline_monotonic=deadline,
            )
        self.assertIs(result, marker)
        reader_deadline = adapter.GitHubCurrentTargetNativeReader.call_args.kwargs["deadline_monotonic"]
        collector_deadline = adapter.collect_current_target_evidence.call_args.kwargs["deadline_monotonic"]
        self.assertEqual(reader_deadline, deadline)
        self.assertEqual(collector_deadline, deadline)
        self.assertEqual(adapter.collect_current_target_evidence.call_args.kwargs["native_reader"], reader)
        remaining = deadline - time.monotonic()
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, MODULE.CURRENT_TARGET_COLLECTION_BUDGET_SECONDS)

    def test_current_target_rejects_expired_or_unbounded_deadline_before_git_or_reader(self) -> None:
        source_sha = "a" * 40
        invalid_deadlines = (
            None,
            float("nan"),
            float("inf"),
            time.monotonic() - 1.0,
            time.monotonic() + MODULE.CURRENT_TARGET_COLLECTION_BUDGET_SECONDS + 1.0,
        )
        for deadline in invalid_deadlines:
            with self.subTest(deadline=deadline):
                with (
                    mock.patch.object(MODULE, "git_read_only") as git_read,
                    mock.patch.object(MODULE, "current_target_evidence_module") as load_adapter,
                ):
                    with self.assertRaisesRegex(
                        ValueError, "absolute checked-CLI deadline|absolute CLI deadline",
                    ):
                        MODULE.collect_current_target_evidence_for_cli(
                            root=ROOT,
                            release_source_sha=source_sha,
                            trusted_main_sha=source_sha,
                            evaluation_as_of="2026-10-06T00:00:00Z",
                            deadline_monotonic=deadline,
                        )
                    git_read.assert_not_called()
                    load_adapter.assert_not_called()

    def test_current_target_applicability_without_collector_is_never_a_claim(self) -> None:
        identity = {
            "root": ROOT,
            "release_source_sha": "a" * 40,
            "trusted_main_sha": "b" * 40,
            "evaluation_as_of": "2026-10-06T00:00:00Z",
            "target_tree_sha": "c" * 40,
            "evidence": None,
        }
        current = MODULE.current_target_applicability_projection(
            **identity, target_kind="current_main",
        )
        historical = MODULE.current_target_applicability_projection(
            **identity, target_kind="historical",
        )
        candidate = MODULE.current_target_applicability_projection(
            **identity, target_kind="candidate",
        )
        current_schema = json.loads(
            (ROOT / MODULE.CURRENT_EVALUATION_SCHEMA_PATH).read_bytes()
        )
        applicability_schema = {
            "$schema": current_schema["$schema"],
            "$defs": current_schema["$defs"],
            **current_schema["$defs"]["target_applicability"],
        }
        self.assertEqual(current["status"], "unavailable")
        self.assertEqual(current["reason_code"], "current_target_evidence_not_collected")
        self.assertEqual(historical["status"], "historical")
        self.assertEqual(candidate["status"], "not_applicable")
        for value in (current, historical, candidate):
            self.assertIsNone(value["disposition"])
            self.assertIsNone(value["evidence_index"])
            self.assertNotIn("updated", value)
            MODULE.validate_schema_value(
                value,
                applicability_schema,
                "target applicability projection",
            )
        completed_noop = {
            "schema_version": "datapan.completeness-current-release-evaluation.v1",
            "target_applicability": {
                "status": "current", "disposition": "completed",
            },
            "scopes": [{
                "scope_id": "data_go_kr.operations",
                "claims": {"complete": False, "current": False, "updated": False},
                "missing_evidence": [{"code": "current_target_not_authority"}],
                "proof_state": "historically_supported",
            }],
        }
        with self.assertRaisesRegex(ValueError, "required claim is not supported"):
            MODULE.require_claims(completed_noop, ["data_go_kr.operations:updated"])

    @staticmethod
    def _closed_git_environment() -> dict[str, str]:
        """Keep test Git local, non-promisor, and independent of ambient Git config."""
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update({
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file",
        })
        return environment

    def test_exact_fixture_file_keeps_same_filesystem_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.write_bytes(b"same-filesystem fixture payload\n")
            with mock.patch.object(shutil, "copy2") as copy_file:
                result = _materialize_exact_fixture_file(source, destination)
            copy_file.assert_not_called()
            self.assertEqual(result["method"], "hardlink")
            self.assertEqual(result["source"], result["destination"])
            self.assertEqual(source.stat().st_ino, destination.stat().st_ino)

    def test_exact_fixture_file_copies_only_after_exdev(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.write_bytes(b"cross-filesystem fixture payload\n")
            with (
                mock.patch.object(os, "link", side_effect=OSError(errno.EXDEV, "cross-device")),
                mock.patch.object(shutil, "copy2", wraps=shutil.copy2) as copy_file,
            ):
                result = _materialize_exact_fixture_file(source, destination)
            copy_file.assert_called_once_with(source, destination)
            self.assertEqual(result["method"], "copy2")
            self.assertEqual(result["source"], result["destination"])
            self.assertNotEqual(source.stat().st_ino, destination.stat().st_ino)

    def test_exact_fixture_file_propagates_non_exdev_link_failures(self) -> None:
        for error_number in (errno.ENOSPC, errno.EIO, errno.EACCES):
            with self.subTest(error_number=error_number), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                source = root / "source"
                destination = root / "destination"
                source.write_bytes(b"fixture payload\n")
                expected = OSError(error_number, os.strerror(error_number))
                with (
                    mock.patch.object(os, "link", side_effect=expected),
                    mock.patch.object(shutil, "copy2") as copy_file,
                    self.assertRaises(OSError) as raised,
                ):
                    _materialize_exact_fixture_file(source, destination)
                self.assertEqual(raised.exception.errno, error_number)
                copy_file.assert_not_called()
                self.assertFalse(destination.exists())

    def test_exact_fixture_file_does_not_accept_failed_partial_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.write_bytes(b"complete fixture payload\n")

            def partial_copy(_source: pathlib.Path, target: pathlib.Path) -> None:
                target.write_bytes(b"partial")
                raise OSError(errno.ENOSPC, "copy ran out of space")

            with (
                mock.patch.object(os, "link", side_effect=OSError(errno.EXDEV, "cross-device")),
                mock.patch.object(shutil, "copy2", side_effect=partial_copy),
                self.assertRaises(OSError) as raised,
            ):
                _materialize_exact_fixture_file(source, destination)
            self.assertEqual(raised.exception.errno, errno.ENOSPC)
            self.assertEqual(destination.read_bytes(), b"partial")

    def test_exact_fixture_file_rejects_size_or_digest_mismatch(self) -> None:
        corruptions = (b"short", b"complete fixture payloae\n")
        for corrupted in corruptions:
            with self.subTest(corrupted=corrupted), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                source = root / "source"
                destination = root / "destination"
                source.write_bytes(b"complete fixture payload\n")

                def corrupt_copy(_source: pathlib.Path, target: pathlib.Path) -> None:
                    target.write_bytes(corrupted)

                with (
                    mock.patch.object(os, "link", side_effect=OSError(errno.EXDEV, "cross-device")),
                    mock.patch.object(shutil, "copy2", side_effect=corrupt_copy),
                    self.assertRaisesRegex(AssertionError, "differs from its source payload"),
                ):
                    _materialize_exact_fixture_file(source, destination)

    def test_catalog_input_schema_requires_an_exact_revision_only_for_repository_snapshot(self) -> None:
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        schema = json.loads((ROOT / "schemas/datapan.completeness-proof-inputs.v1.schema.json").read_bytes())
        MODULE.validate_schema_value(index, schema, "input index")
        catalog_item = next(item for item in index["inputs"] if item["role"] == "source_catalog_snapshot")

        missing_subject = copy.deepcopy(index)
        missing = next(item for item in missing_subject["inputs"] if item["role"] == "source_catalog_snapshot")
        missing.pop("subject")
        with self.assertRaisesRegex(ValueError, "subject|source_revision"):
            MODULE.validate_schema_value(missing_subject, schema, "input index")

        short_revision = copy.deepcopy(index)
        short = next(item for item in short_revision["inputs"] if item["role"] == "source_catalog_snapshot")
        short["subject"]["source_revision"] = catalog_item["subject"]["source_revision"][:-1]
        with self.assertRaisesRegex(ValueError, "source_revision"):
            MODULE.validate_schema_value(short_revision, schema, "input index")

        long_revision = copy.deepcopy(index)
        long = next(item for item in long_revision["inputs"] if item["role"] == "source_catalog_snapshot")
        long["subject"]["source_revision"] += "0"
        with self.assertRaisesRegex(ValueError, "source_revision"):
            MODULE.validate_schema_value(long_revision, schema, "input index")

        other_role = copy.deepcopy(index)
        other = next(item for item in other_role["inputs"] if item["role"] == "source_profile")
        other["subject"] = {"source_revision": "a" * 64}
        MODULE.validate_schema_value(other_role, schema, "input index")

        candidate_index = copy.deepcopy(index)
        candidate_index["evaluation_context"] = {
            "mode": "candidate",
            "baseline_main_sha": "a" * 40,
            **{
                key: {"path": f"fixture:{key}", "bytes": 0, "sha256": "0" * 64}
                for key in (
                    "baseline_input_index", "baseline_registry", "baseline_operation_manifest",
                    "baseline_release_manifest", "baseline_policy", "baseline_scope_registry",
                    "candidate_registry", "candidate_operation_manifest",
                )
            },
        }
        candidate_catalog = next(
            item for item in candidate_index["inputs"] if item["role"] == "source_catalog_snapshot"
        )
        candidate_catalog.pop("subject")
        MODULE.validate_schema_value(candidate_index, schema, "candidate input index")

    def test_catalog_binding_witness_is_stable_across_main_tip_and_requires_updated_pin_for_new_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="completeness-catalog-binding-") as name:
            repository = pathlib.Path(name) / "repository"
            inputs, scope_registry, resolved, pinned_revision = self._prepare_synthetic_catalog_binding_repository(
                repository, registry_bytes=b"[]\n",
            )
            first = MODULE.validate_current_catalog_subject(
                root=repository, inputs=inputs, resolved=resolved, scope_registry=scope_registry,
            )
            self.assertEqual(first["release_manifest_sha256"], None)
            self.assertFalse(first["current_release_applicable"])
            self.assertEqual(first["catalog_binding_witness"]["source_revision"], pinned_revision)
            self.assertTrue(first["catalog_binding_witness"]["main_ancestry_verified"])

            current_manifest = json.loads((repository / "manifest.json").read_bytes())
            current_manifest["generated_at"] = "2026-10-10T00:00:00Z"
            (repository / "manifest.json").write_text(
                json.dumps(current_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            subprocess.run(["git", "add", "--", "manifest.json"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "advance trusted main without catalog change"], cwd=repository, check=True)
            advanced_tip = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", advanced_tip], cwd=repository, check=True,
            )
            after_tip_advance = MODULE.validate_current_catalog_subject(
                root=repository, inputs=inputs, resolved=resolved, scope_registry=scope_registry,
            )
            self.assertEqual(after_tip_advance, first)

            changed_registry = b"[{\"catalog_revision\":2}]\n"
            catalog_item = next(item for item in inputs if item["role"] == "source_catalog_snapshot")
            catalog_path = resolved[catalog_item["input_id"]]
            catalog_path.write_bytes(changed_registry)
            catalog_item["bytes"] = len(changed_registry)
            catalog_item["sha256"] = MODULE.sha256_bytes(changed_registry)
            with self.assertRaisesRegex(ValueError, "differs from its pinned source revision manifest and LFS pointer"):
                MODULE.validate_current_catalog_subject(
                    root=repository, inputs=inputs, resolved=resolved, scope_registry=scope_registry,
                )

            updated_pin = self._commit_synthetic_catalog_revision(
                repository, registry_bytes=changed_registry, message="update trusted catalog snapshot",
            )
            catalog_item["subject"]["source_revision"] = updated_pin
            rebound = MODULE.validate_current_catalog_subject(
                root=repository, inputs=inputs, resolved=resolved, scope_registry=scope_registry,
            )
            self.assertEqual(rebound["catalog_binding_witness"]["source_revision"], updated_pin)
            self.assertNotEqual(rebound["catalog_binding_witness"], first["catalog_binding_witness"])
            self.assertIsNone(rebound["release_manifest_sha256"])
            self.assertFalse(rebound["current_release_applicable"])

    def test_catalog_binding_witness_rejects_untrusted_revision_and_tampered_lfs_pointer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="completeness-catalog-binding-negative-") as name:
            repository = pathlib.Path(name) / "repository"
            inputs, scope_registry, resolved, pinned_revision = self._prepare_synthetic_catalog_binding_repository(
                repository, registry_bytes=b"[]\n",
            )
            catalog_item = next(item for item in inputs if item["role"] == "source_catalog_snapshot")
            subprocess.run(
                ["git", "update-ref", "refs/heads/untrusted", pinned_revision], cwd=repository, check=True,
            )
            subprocess.run(
                ["git", "symbolic-ref", "HEAD", "refs/heads/untrusted"], cwd=repository, check=True,
            )
            (repository / "untrusted.txt").write_text("not on trusted main\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", "untrusted.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "untrusted side commit"], cwd=repository, check=True)
            untrusted_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            catalog_item["subject"]["source_revision"] = untrusted_revision
            with self.assertRaisesRegex(ValueError, "not reachable from the selected trusted origin/main history"):
                MODULE.validate_current_catalog_subject(
                    root=repository, inputs=inputs, resolved=resolved, scope_registry=scope_registry,
                )

            subprocess.run(
                ["git", "update-ref", "refs/heads/main", pinned_revision], cwd=repository, check=True,
            )
            subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repository, check=True)
            bad_pointer = (
                "version https://git-lfs.github.com/spec/v1\n"
                + "oid sha256:" + "f" * 64 + "\n"
                + "size 3\n"
            ).encode("ascii")
            pointer_blob = subprocess.check_output(
                ["git", "hash-object", "-w", "--stdin"], cwd=repository, input=bad_pointer,
            ).decode("ascii").strip()
            subprocess.run(
                ["git", "update-index", "--add", "--cacheinfo", f"100644,{pointer_blob},data/data-go-kr.registry.json"],
                cwd=repository, check=True,
            )
            subprocess.run(["git", "commit", "--quiet", "-m", "tamper trusted LFS pointer"], cwd=repository, check=True)
            tampered_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", tampered_revision], cwd=repository, check=True,
            )
            catalog_item["subject"]["source_revision"] = tampered_revision
            with self.assertRaisesRegex(ValueError, "manifest and committed Git LFS pointer do not identify the same payload"):
                MODULE.validate_current_catalog_subject(
                    root=repository, inputs=inputs, resolved=resolved, scope_registry=scope_registry,
                )

    @staticmethod
    def _hardlink_worktree(source: pathlib.Path, destination: pathlib.Path) -> None:
        skipped = {".git", ".datapan", "__pycache__", ".pytest_cache"}
        for current, dirs, files in os.walk(source):
            current_path = pathlib.Path(current)
            relative = current_path.relative_to(source)
            dirs[:] = [name for name in dirs if name not in skipped]
            target_dir = destination / relative
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in files:
                if name in skipped:
                    continue
                source_file = current_path / name
                target_file = target_dir / name
                if source_file.name == ".git" or source_file.is_dir():
                    continue
                if source_file.is_symlink():
                    target_file.symlink_to(os.readlink(source_file))
                    continue
                try:
                    os.link(source_file, target_file)
                except OSError:
                    shutil.copy2(source_file, target_file)

    def _prepare_synthetic_catalog_binding_repository(
        self, repository: pathlib.Path, *, registry_bytes: bytes = b"[]\n",
    ) -> tuple[list[dict[str, object]], dict[str, object], dict[str, pathlib.Path], str]:
        """Create a tiny trusted main history with a manifest-bound LFS catalog."""
        repository.mkdir(parents=True)
        base_sha = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/main"], cwd=ROOT, text=True,
        ).strip()
        common_dir = pathlib.Path(subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], cwd=ROOT, text=True,
        ).strip()).resolve()
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=main", str(repository)], check=True,
        )
        alternates = repository / ".git/objects/info/alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(str(common_dir / "objects") + "\n")
        subprocess.run(["git", "config", "user.name", "Catalog binding test"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "catalog-binding@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "update-ref", "refs/heads/main", base_sha], cwd=repository, check=True)
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", base_sha], cwd=repository, check=True)
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repository, check=True)
        subprocess.run(["git", "read-tree", base_sha], cwd=repository, check=True)

        catalog_path = repository / "data/data-go-kr.registry.json"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_bytes(registry_bytes)
        manifest = json.loads(subprocess.check_output(
            ["git", "show", f"{base_sha}:manifest.json"], cwd=repository,
        ))
        artifact_rows = [
            row for row in manifest["artifacts"]
            if row.get("path") == "data/data-go-kr.registry.json"
        ]
        self.assertEqual(len(artifact_rows), 1)
        artifact_rows[0]["bytes"] = len(registry_bytes)
        artifact_rows[0]["sha256"] = MODULE.sha256_bytes(registry_bytes)
        (repository / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        subprocess.run(["git", "add", "--", "manifest.json"], cwd=repository, check=True)
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{MODULE.sha256_bytes(registry_bytes)}\n"
            f"size {len(registry_bytes)}\n"
        ).encode("ascii")
        pointer_blob = subprocess.check_output(
            ["git", "hash-object", "-w", "--stdin"], cwd=repository, input=pointer,
        ).decode("ascii").strip()
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"100644,{pointer_blob},data/data-go-kr.registry.json"],
            cwd=repository, check=True,
        )
        subprocess.run(["git", "commit", "--quiet", "-m", "synthetic pinned catalog"], cwd=repository, check=True)
        pinned_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", pinned_revision], cwd=repository, check=True,
        )

        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        inputs = copy.deepcopy(index["inputs"])
        catalog_item = next(item for item in inputs if item["role"] == "source_catalog_snapshot")
        catalog_item["bytes"] = len(registry_bytes)
        catalog_item["sha256"] = MODULE.sha256_bytes(registry_bytes)
        catalog_item.setdefault("subject", {})["source_revision"] = pinned_revision
        scope_registry = json.loads((ROOT / MODULE.SCOPE_REGISTRY_PATH).read_bytes())
        return inputs, scope_registry, {catalog_item["input_id"]: catalog_path}, pinned_revision

    @staticmethod
    def _commit_synthetic_catalog_revision(
        repository: pathlib.Path, *, registry_bytes: bytes, message: str,
    ) -> str:
        parent_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
        manifest = json.loads(subprocess.check_output(
            ["git", "show", f"{parent_sha}:manifest.json"], cwd=repository,
        ))
        artifact_rows = [
            row for row in manifest["artifacts"]
            if row.get("path") == "data/data-go-kr.registry.json"
        ]
        if len(artifact_rows) != 1:
            raise AssertionError("synthetic manifest lacks one canonical catalog artifact")
        artifact_rows[0]["bytes"] = len(registry_bytes)
        artifact_rows[0]["sha256"] = MODULE.sha256_bytes(registry_bytes)
        (repository / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        (repository / "data/data-go-kr.registry.json").write_bytes(registry_bytes)
        subprocess.run(["git", "add", "--", "manifest.json"], cwd=repository, check=True)
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{MODULE.sha256_bytes(registry_bytes)}\n"
            f"size {len(registry_bytes)}\n"
        ).encode("ascii")
        pointer_blob = subprocess.check_output(
            ["git", "hash-object", "-w", "--stdin"], cwd=repository, input=pointer,
        ).decode("ascii").strip()
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"100644,{pointer_blob},data/data-go-kr.registry.json"],
            cwd=repository, check=True,
        )
        subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=repository, check=True)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", revision], cwd=repository, check=True,
        )
        return revision

    @staticmethod
    def _materialize_source_commit_tree(
        source_root: pathlib.Path, source_commit: str, destination: pathlib.Path,
        *, environment: dict[str, str],
    ) -> None:
        """Create a disposable tree from committed bytes, excluding ambient G and untracked files."""
        destination.mkdir(parents=True, exist_ok=False)
        archive_path: pathlib.Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="completeness-source-commit-", suffix=".tar",
                dir=destination.parent, delete=False,
            ) as archive_file:
                archive_path = pathlib.Path(archive_file.name)
            subprocess.run(
                ["git", "archive", "--format=tar", f"--output={archive_path}", source_commit],
                cwd=source_root, env=environment, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=True,
            )
            with tarfile.open(archive_path, mode="r:") as archive:
                archive.extractall(path=destination, filter="data")
        finally:
            if archive_path is not None:
                archive_path.unlink(missing_ok=True)

    @staticmethod
    def _materialize_pinned_completeness_fixture_source(
        *, source_root: pathlib.Path, source_path: pathlib.Path,
        destination_path: pathlib.Path, relative: str,
        fixture_source_revision: str,
        indexed_pin: tuple[int, str] | None,
        manifest_pin: tuple[int, str] | None,
    ) -> tuple[int, str] | None:
        """Copy one fixture dependency using its indexed or committed identity."""
        source_bytes = source_path.read_bytes()
        materialized_bytes = source_bytes
        pin = indexed_pin
        if pin is not None and manifest_pin is not None and pin != manifest_pin:
            raise AssertionError(f"input-index and release-manifest pins conflict for {relative}")
        if pin is None and relative in COMPLETENESS_VALIDATOR_SOURCE_DEPENDENCIES:
            # Current validator code is pinned to this test candidate's exact
            # Git tree. A release-manifest row can describe a generated
            # historical artifact preimage and must not override that code
            # role. Indexed repository evidence still takes its own pin above.
            tracked_bytes = subprocess.check_output(
                ["git", "show", f"{fixture_source_revision}:{relative}"], cwd=source_root,
            )
            pin = (len(tracked_bytes), MODULE.sha256_bytes(tracked_bytes))
            if source_bytes != tracked_bytes:
                if relative != "schemas/index.json":
                    raise AssertionError(
                        f"checked-out validator source differs from its pinned Git commit {fixture_source_revision}: {relative}"
                    )
                # Only this generated, non-indexed dependency may differ in
                # the ambient worktree. Materialize the immutable Git blob in
                # the disposable repository; never bless current G bytes.
                materialized_bytes = tracked_bytes
        if pin is None:
            pin = manifest_pin
        if pin is not None and (len(materialized_bytes), MODULE.sha256_bytes(materialized_bytes)) != pin:
            raise AssertionError(
                f"checked-out source does not match its authoritative byte/SHA pin for {relative}"
            )
        if destination_path.is_file():
            destination_bytes = destination_path.read_bytes()
            if pin is not None and (len(destination_bytes), MODULE.sha256_bytes(destination_bytes)) == pin:
                return pin
            destination_path.unlink()
        elif destination_path.exists():
            raise AssertionError(f"temporary repository input path is not a file: {relative}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if materialized_bytes == source_bytes:
            shutil.copy2(source_path, destination_path)
        else:
            destination_path.write_bytes(materialized_bytes)
            os.chmod(destination_path, stat.S_IMODE(source_path.stat().st_mode))
        if pin is not None:
            destination_bytes = destination_path.read_bytes()
            if (len(destination_bytes), MODULE.sha256_bytes(destination_bytes)) != pin:
                raise AssertionError(f"materialized fixture dependency differs from its pinned bytes: {relative}")
        return pin

    @staticmethod
    def _copy_worktree(source: pathlib.Path, destination: pathlib.Path) -> None:
        """Copy an isolated fixture without touching donor inode metadata."""
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git", ".datapan", "__pycache__", ".pytest_cache",
            ),
        )

    @staticmethod
    def _write_json_file(root: pathlib.Path, relative: str, value: object) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            path.unlink()
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _write_rebound_evidence(
        input_root: pathlib.Path, item: dict[str, object], filename: str, raw: bytes,
    ) -> None:
        path = input_root / filename
        path.write_bytes(raw)
        item.update({
            "root": "evidence", "path": filename,
            "bytes": len(raw), "sha256": MODULE.sha256_bytes(raw),
        })

    def _build_synthetic_native_ack_packet(
        self, input_root: pathlib.Path, *, outcome: str = "already_acknowledged",
        publisher_identity: tuple[int, int] | None = None,
        acknowledgement_identity: tuple[int, int] | None = None,
        acknowledgement_event: str = "schedule",
        predecessor_status: str | None = None,
        standalone_replay: bool = False,
        replay_pr_number: object = _PRESERVE_REPLAY_PR_NUMBER,
    ) -> tuple[pathlib.Path, str]:
        """Build an indexed native publisher/ACK packet with a nonlegacy ACK identity."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        # Keep the exact publisher and its full immutable read-back packet, but
        # omit the independent A/B/C/Health links so this test covers only the
        # generic native publisher + ACK contract and cannot claim completeness.
        core_stages = {"source", "processor", "promotion", "health"}
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_id
            or (
                item.get("subject", {}).get("stage") not in core_stages
                and item.get("role") != "proof_v1"
            )
        ]
        ack_rows = [
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id
            and (
                item.get("subject", {}).get("stage") == "acknowledgement"
                or item.get("role") in {"acknowledgement_journal_before", "acknowledgement_journal_after"}
            )
        ]
        ack_roles = {item["role"]: item for item in ack_rows}
        current_run_id, current_attempt = acknowledgement_identity or (99972290001, 1)
        current_url = (
            f"https://github.com/StatPan/datapan-registry/actions/runs/{current_run_id}"
            f"/attempts/{current_attempt}"
        )
        current_start = "2026-10-04T13:20:00Z"
        job_start = "2026-10-04T13:20:10Z"
        job_finish = "2026-10-04T13:22:00Z"
        current_revision = "e34062309a48b0e0b6c0f38add32f0cdec088616"

        source_rows = [
            item for item in json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())["inputs"]
            if item.get("scope_id") == operation_id
        ]
        source_by_role = {item["role"]: item for item in source_rows}

        if publisher_identity is not None:
            publisher_run_id, publisher_attempt = publisher_identity
            if publisher_run_id < 1 or publisher_attempt < 1:
                raise AssertionError("synthetic publisher identity must be positive")

            def rebind_publisher_json(role: str, filename: str, update: object) -> None:
                item = next(
                    row for row in index["inputs"]
                    if row.get("scope_id") == operation_id and row["role"] == role
                )
                value = json.loads((ROOT / source_by_role[role]["path"]).read_bytes())
                update(value)
                self._write_rebound_evidence(
                    input_root, item, filename,
                    (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )

            rebind_publisher_json(
                "publication_producer_run", "synthetic-publisher-run.json",
                lambda value: value.update({"id": publisher_run_id, "run_attempt": publisher_attempt}),
            )

            def rebind_jobs(value: dict[str, object]) -> None:
                jobs_value = value.get("jobs")
                if not isinstance(jobs_value, list) or len(jobs_value) != 1 or not isinstance(jobs_value[0], dict):
                    raise AssertionError("retained publisher must have one exact successful job")
                jobs_value[0].update({"run_id": publisher_run_id, "run_attempt": publisher_attempt})

            rebind_publisher_json("publication_producer_jobs", "synthetic-publisher-jobs.json", rebind_jobs)

            def rebind_artifacts(value: dict[str, object]) -> None:
                artifacts = value.get("artifacts")
                if not isinstance(artifacts, list) or not artifacts:
                    raise AssertionError("retained publisher must have an exact artifact list")
                for artifact in artifacts:
                    if not isinstance(artifact, dict) or not isinstance(artifact.get("workflow_run"), dict):
                        raise AssertionError("retained publisher artifact lacks its workflow run identity")
                    artifact["workflow_run"]["id"] = publisher_run_id

            rebind_publisher_json("publication_output_artifact", "synthetic-publisher-artifacts.json", rebind_artifacts)

            def rebind_readback(value: dict[str, object]) -> None:
                value["publisher_run_id"] = publisher_run_id
                value["publisher_attempt"] = publisher_attempt
                checks = value.get("checks")
                if isinstance(checks, list):
                    for check in checks:
                        if not isinstance(check, dict) or not isinstance(check.get("detail"), dict):
                            continue
                        if check.get("check") == "publisher_run_is_exact_completed_success":
                            check["detail"]["id"] = publisher_run_id
                            check["detail"]["run_attempt"] = publisher_attempt

            rebind_publisher_json(
                "publication_anonymous_payload", "synthetic-publisher-readback.json", rebind_readback,
            )
            for item in index["inputs"]:
                if item.get("scope_id") != operation_id or item.get("subject", {}).get("stage") != "publisher":
                    continue
                item["producer"].update({
                    "run_id": str(publisher_run_id), "attempt": publisher_attempt,
                    "workflow_id": 311133646,
                    "workflow_path": ".github/workflows/huggingface-distribution.yml",
                    "event": "workflow_dispatch", "revision": "e34062309a48b0e0b6c0f38add32f0cdec088616",
                })

        run = json.loads((ROOT / source_by_role["acknowledgement_run"]["path"]).read_bytes())
        run.update({
            "id": current_run_id, "event": acknowledgement_event, "run_attempt": current_attempt,
            "status": "completed", "conclusion": "success", "head_sha": current_revision,
            "head_branch": "main", "created_at": current_start,
            "updated_at": job_finish, "run_started_at": current_start,
            "run_completed_at": job_finish,
        })
        jobs = json.loads((ROOT / source_by_role["acknowledgement_jobs"]["path"]).read_bytes())
        if jobs.get("total_count") != 1 or len(jobs.get("jobs", [])) != 1:
            raise AssertionError("retained ACK packet must have exactly one successful reconcile job")
        jobs["jobs"][0].update({
            "run_id": current_run_id, "run_attempt": current_attempt,
            "head_sha": current_revision, "head_branch": "main",
            "status": "completed", "conclusion": "success",
            "started_at": job_start, "completed_at": job_finish,
        })

        before_path = ROOT / source_by_role["acknowledgement_journal_before"]["path"]
        after_path = ROOT / source_by_role["acknowledgement_journal_after"]["path"]
        actual_before = json.loads(before_path.read_bytes())
        actual_after = json.loads(after_path.read_bytes())
        publisher_receipt = json.loads((ROOT / source_by_role["publication_receipt"]["path"]).read_bytes())
        publisher_record = publisher_receipt["publication"]
        source_sha = publisher_receipt["source_binding"]["source_sha"]
        manifest_sha = publisher_receipt["source_binding"]["manifest_sha256"]
        if outcome == "already_acknowledged":
            journal_before = copy.deepcopy(actual_after)
            journal_after = copy.deepcopy(actual_after)
            if standalone_replay:
                active_rows = [
                    row for row in actual_after.get("records", [])
                    if isinstance(row, dict)
                    and row.get("status") == "read-back-confirmed"
                    and row.get("superseded_by") is None
                    and row.get("candidate", {}).get("source_id") == "data_go_kr"
                    and row.get("candidate", {}).get("manifest_sha256") == manifest_sha
                    and row.get("pr", {}).get("merge_commit_sha") == source_sha
                ]
                if len(active_rows) != 1:
                    raise AssertionError("retained journal must identify one active standalone replay witness")
                standalone_row = copy.deepcopy(active_rows[0])
                for key in ("refresh_from", "refresh_target_main_sha", "superseded_by", "ci"):
                    standalone_row.pop(key, None)
                if replay_pr_number is not _PRESERVE_REPLAY_PR_NUMBER:
                    if replay_pr_number is None:
                        standalone_row["pr"].pop("number", None)
                    else:
                        standalone_row["pr"]["number"] = replay_pr_number
                journal_before["records"] = [standalone_row]
                journal_after = copy.deepcopy(journal_before)
            log_status = "already_acknowledged"
            journal_writes = 0
        elif outcome == "read-back-confirmed":
            if predecessor_status is None:
                journal_before = copy.deepcopy(actual_before)
                journal_after = copy.deepcopy(actual_after)
                catalog_rows = [
                    row for row in journal_after["records"]
                    if row.get("candidate", {}).get("manifest_sha256") == manifest_sha
                    and row.get("pr", {}).get("merge_commit_sha") == source_sha
                ]
                if len(catalog_rows) != 1:
                    raise AssertionError("retained journal must identify the exact publisher source once")
                old_acks = journal_before["records"][journal_after["records"].index(catalog_rows[0])]["acknowledgements"]
                row = catalog_rows[0]
                appended = row["acknowledgements"][len(old_acks):]
                if [item.get("status") for item in appended] != [
                    "publication-pending", "published", "read-back-confirmed",
                ]:
                    raise AssertionError("retained first ACK must have its exact three-event suffix")
                observation_times = [
                    "2026-10-04T13:20:20Z", "2026-10-04T13:21:00Z", "2026-10-04T13:21:40Z",
                ]
                for acknowledgement, observed_at in zip(appended, observation_times, strict=True):
                    acknowledgement.update({
                        "run_id": current_run_id, "run_attempt": current_attempt,
                        "run_url": current_url, "observed_at": observed_at,
                    })
                journal_before["updated_at"] = "2026-10-04T12:00:00Z"
                journal_after["updated_at"] = "2026-10-04T13:21:45Z"
            else:
                if predecessor_status not in {
                    "prepared", "pending-review", "merged", "publication-pending", "published",
                }:
                    raise AssertionError(f"unsupported synthetic ACK predecessor: {predecessor_status}")
                owner_source = MODULE.git_read_only(
                    ROOT, ["show", f"{current_revision}:scripts/canonical_update_pr.py"],
                )
                with tempfile.TemporaryDirectory(prefix="completeness-owner-ack-fixture-") as temporary:
                    owner_path = pathlib.Path(temporary) / "canonical_update_pr.py"
                    owner_path.write_bytes(owner_source)
                    owner = MODULE.import_module("synthetic_ack_owner", owner_path)
                exact_merged_rows = [
                    row for row in actual_before["records"]
                    if row.get("candidate", {}).get("manifest_sha256") == manifest_sha
                    and row.get("pr", {}).get("merge_commit_sha") == source_sha
                ]
                if len(exact_merged_rows) != 1:
                    raise AssertionError("retained journal must identify the exact publisher source once")
                prepared = copy.deepcopy(exact_merged_rows[0])
                pr_number = prepared.get("pr", {}).get("number")
                if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
                    raise AssertionError("retained candidate must have an exact positive PR number")
                prepared["status"] = "prepared"
                prepared["acknowledgements"] = []
                prepared["pr"] = {
                    "number": pr_number,
                    "url": f"https://github.com/StatPan/datapan-registry/pull/{pr_number}",
                    "state": "open", "merge_commit_sha": None,
                }
                for key in ("refresh_from", "refresh_target_main_sha", "superseded_by"):
                    prepared.pop(key, None)
                prior_url = "https://github.com/StatPan/datapan-registry/actions/runs/99972289999/attempts/1"
                prior_observed = "2026-10-04T13:19:20Z"
                pr_readback = {
                    "number": pr_number,
                    "url": f"https://github.com/StatPan/datapan-registry/pull/{pr_number}",
                    "body": prepared["ownership"]["body"],
                    "headRefName": prepared["ownership"]["branch"],
                    "baseRefName": "main",
                    "headRefOid": prepared["candidate"]["head_sha"],
                    "state": "MERGED",
                    "mergeCommit": {"oid": source_sha},
                }
                if predecessor_status == "prepared":
                    before_row = prepared
                elif predecessor_status == "pending-review":
                    before_row = owner.record_pr_readback(
                        copy.deepcopy(prepared), {**pr_readback, "state": "OPEN", "mergeCommit": None},
                        observed_at=prior_observed, run_url=prior_url,
                    )
                else:
                    merged_row = owner.record_pr_readback(
                        copy.deepcopy(prepared), pr_readback,
                        observed_at=prior_observed, run_url=prior_url,
                    )
                    if predecessor_status == "merged":
                        before_row = merged_row
                    else:
                        receipt_path = ROOT / source_by_role["publication_receipt"]["path"]
                        reconciled = owner.reconcile_huggingface_publication(
                            copy.deepcopy(merged_row), receipt_path,
                            observed_at=prior_observed, run_url=prior_url,
                        )
                        prior_suffix_length = 1 if predecessor_status == "publication-pending" else 2
                        prefix_length = len(merged_row["acknowledgements"]) + prior_suffix_length
                        before_row = copy.deepcopy(reconciled)
                        before_row["status"] = predecessor_status
                        before_row["acknowledgements"] = reconciled["acknowledgements"][:prefix_length]

                before_updated = "2026-10-04T13:19:45Z"
                after_row = copy.deepcopy(before_row)
                if predecessor_status in {"prepared", "pending-review"}:
                    after_row = owner.record_pr_readback(
                        after_row, pr_readback,
                        observed_at="2026-10-04T13:20:20Z", run_url=current_url,
                    )
                after_row = owner.reconcile_huggingface_publication(
                    after_row, ROOT / source_by_role["publication_receipt"]["path"],
                    observed_at="2026-10-04T13:20:30Z", run_url=current_url,
                )
                journal_before = {
                    "schema_version": actual_before["schema_version"],
                    "repository": actual_before["repository"],
                    "updated_at": before_updated,
                    "records": [before_row],
                }
                journal_after = {
                    "schema_version": actual_after["schema_version"],
                    "repository": actual_after["repository"],
                    "updated_at": "2026-10-04T13:21:45Z",
                    "records": [after_row],
                }
            log_status = "read-back-confirmed"
            journal_writes = None
        else:
            raise AssertionError(f"unsupported synthetic ACK outcome: {outcome}")

        def pretty(value: object) -> bytes:
            return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()

        source_stage_roles = {
            item["role"]: item for item in source_rows
            if item.get("subject", {}).get("stage") == "promotion"
        }
        base_tree = json.loads((ROOT / source_stage_roles["promotion_state_tree"]["path"]).read_bytes())
        base_commit = json.loads((ROOT / source_stage_roles["promotion_state_commit"]["path"]).read_bytes())
        base_ref = json.loads((ROOT / source_stage_roles["promotion_state_ref"]["path"]).read_bytes())
        base_blob = json.loads((ROOT / source_stage_roles["promotion_journal_blob_api"]["path"]).read_bytes())
        base_journal = MODULE.base64.b64decode(base_blob["content"])
        if base_journal != pretty(actual_after):
            # The stored evidence files retain source formatting. Compare JSON
            # meaning here; the rebuilder uses byte-exact snapshots below.
            if json.loads(base_journal) != actual_after:
                raise AssertionError("promotion state tree must contain the retained exact ACK after journal")

        def state_bundle(journal_raw: bytes, label: str) -> dict[str, bytes]:
            tree_api = copy.deepcopy(base_tree)
            tree_rows = copy.deepcopy(tree_api["tree"])
            journal_rows = [
                row for row in tree_rows
                if row.get("path") == "reports/canonical-update-promotion-receipt.json"
            ]
            if len(journal_rows) != 1:
                raise AssertionError("promotion state tree must contain one journal blob")
            blob_sha = MODULE.git_blob_sha1(journal_raw)
            journal_rows[0].update({"sha": blob_sha, "size": len(journal_raw)})
            journal_rows[0]["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{blob_sha}"
            directories = sorted(
                (row["path"] for row in tree_rows if row.get("type") == "tree"),
                key=lambda value: value.count("/"), reverse=True,
            )
            for directory in directories:
                children = [
                    row for row in tree_rows
                    if pathlib.PurePosixPath(row["path"]).parent.as_posix() == directory
                ]
                children.sort(key=lambda row: (
                    pathlib.PurePosixPath(row["path"]).name
                    + ("/" if row.get("type") == "tree" else "")
                ).encode())
                body = bytearray()
                for child in children:
                    mode = str(child["mode"]).lstrip("0") or "0"
                    body.extend(
                        mode.encode("ascii") + b" "
                        + pathlib.PurePosixPath(child["path"]).name.encode("utf-8")
                        + b"\0" + bytes.fromhex(child["sha"])
                    )
                digest = MODULE.hashlib.sha1(f"tree {len(body)}\0".encode("ascii") + body).hexdigest()
                directory_row = next(row for row in tree_rows if row.get("path") == directory)
                directory_row.update({
                    "sha": digest,
                    "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{digest}",
                })
            tree_sha = self._git_tree_sha_from_recursive_rows(tree_rows)
            tree_api.update({
                "sha": tree_sha,
                "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}",
                "tree": tree_rows,
            })
            author = "Synthetic completeness ACK <completeness-test@example.invalid>"
            commit_content = (
                f"tree {tree_sha}\nparent {base_commit['sha']}\n"
                f"author {author} 1791118800 +0000\ncommitter {author} 1791118800 +0000\n\n"
                f"Synthetic ACK {label} state snapshot\n"
            ).encode()
            commit_sha = MODULE.hashlib.sha1(
                f"commit {len(commit_content)}\0".encode() + commit_content
            ).hexdigest()
            commit_api = copy.deepcopy(base_commit)
            commit_api.update({
                "sha": commit_sha, "message": f"Synthetic ACK {label} state snapshot",
                "tree": {**base_commit["tree"], "sha": tree_sha,
                         "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}"},
                "parents": [{"sha": base_commit["sha"], "url": base_commit["url"],
                             "html_url": base_commit.get("html_url")}],
            })
            ref_api = copy.deepcopy(base_ref)
            ref_api["object"].update({
                "sha": commit_sha, "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/commits/{commit_sha}",
            })
            blob_api = copy.deepcopy(base_blob)
            blob_api.update({
                "sha": blob_sha, "size": len(journal_raw),
                "content": MODULE.base64.b64encode(journal_raw).decode("ascii"),
                "encoding": "base64",
                "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{blob_sha}",
            })
            return {
                "acknowledgement_state_ref": pretty(ref_api),
                "acknowledgement_state_commit": pretty(commit_api),
                "acknowledgement_state_tree": pretty(tree_api),
                "acknowledgement_journal_blob_api": pretty(blob_api),
            }

        after_bundle = state_bundle(pretty(journal_after), "after")
        before_bundle = (
            state_bundle(pretty(journal_before), "before")
            if outcome != "already_acknowledged" else after_bundle
        )
        output = io.BytesIO()
        log_value = {
            "status": log_status, "source_sha": source_sha,
            "manifest_sha256": manifest_sha, "run_url": current_url,
            **({"journal_writes": journal_writes} if journal_writes is not None else {}),
        }
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("reconcile.log", f"{current_start} {json.dumps(log_value, sort_keys=True)}\n")
        log_raw = output.getvalue()

        rebound_rows = {
            "acknowledgement_run": pretty(run),
            "acknowledgement_jobs": pretty(jobs),
            "acknowledgement_log_archive": log_raw,
            "acknowledgement_journal_before": pretty(journal_before),
            "acknowledgement_journal_after": pretty(journal_after),
            **after_bundle,
            **{
                f"{role}_before": raw
                for role, raw in before_bundle.items()
            },
        }
        for role, raw in rebound_rows.items():
            existing = ack_roles.get(role)
            if existing is None:
                artifact_types = {
                    "acknowledgement_state_ref": "pipeline_state_reference",
                    "acknowledgement_state_commit": "pipeline_git_commit",
                    "acknowledgement_state_tree": "pipeline_git_tree",
                    "acknowledgement_journal_blob_api": "pipeline_git_blob",
                    "acknowledgement_state_ref_before": "pipeline_state_reference",
                    "acknowledgement_state_commit_before": "pipeline_git_commit",
                    "acknowledgement_state_tree_before": "pipeline_git_tree",
                    "acknowledgement_journal_blob_api_before": "pipeline_git_blob",
                }
                existing = {
                    "input_id": f"synthetic-ack-{role}", "scope_id": operation_id,
                    "role": role, "artifact_type": artifact_types[role],
                    "root": "evidence", "path": "", "bytes": 0, "sha256": "0" * 64,
                    "namespace": "live_operational", "producer": {}, "observed_at": None,
                    "subject": {"stage": "acknowledgement"},
                }
                index["inputs"].append(existing)
                ack_roles[role] = existing
            existing["namespace"] = "live_operational"
            if role not in {"acknowledgement_journal_before", "acknowledgement_journal_after"}:
                existing["subject"] = {**existing.get("subject", {}), "stage": "acknowledgement"}
            producer = existing.setdefault("producer", {})
            producer.update({
                "repository": "StatPan/datapan-registry", "identity": f"ack-run-{current_run_id}",
                "run_id": str(current_run_id),
                "attempt": current_attempt, "workflow_id": 373708873,
                "workflow_path": ".github/workflows/canonical-update-publication-ack.yml",
                "event": acknowledgement_event, "revision": current_revision,
            })
            self._write_rebound_evidence(input_root, existing, f"synthetic-{role}.bin", raw)

        # The non-stage journal inputs also belong to this exact current ACK
        # read. They carry no new observation or journal-write timestamp.
        for role in ("acknowledgement_journal_before", "acknowledgement_journal_after"):
            item = ack_roles[role]
            item["namespace"] = "live_operational"
            item["producer"].update({
                "repository": "StatPan/datapan-registry", "identity": f"ack-run-{current_run_id}",
                "run_id": str(current_run_id),
                "attempt": current_attempt, "workflow_id": 373708873,
                "workflow_path": ".github/workflows/canonical-update-publication-ack.yml",
                "event": acknowledgement_event, "revision": current_revision,
            })

        index_path = input_root / "input-index.json"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        return index_path, source_sha

    def _write_rebound_health_archive(
        self, index: dict[str, object], input_root: pathlib.Path,
        member_name: str, member_bytes: bytes,
    ) -> pathlib.Path:
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        health_items = [
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id
            and item.get("subject", {}).get("stage") == "health"
        ]
        archive_item = next(item for item in health_items if item["role"] == "pipeline_artifact_archive")
        metadata_item = next(item for item in health_items if item["role"] == "pipeline_artifact_metadata")
        archive_raw = (ROOT / archive_item["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as source_archive:
            members = {entry.filename: source_archive.read(entry.filename) for entry in source_archive.infolist()}
        if member_name not in members:
            raise AssertionError(f"fixture Health archive lacks {member_name}")
        members[member_name] = member_bytes
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target_archive:
            for name, value in members.items():
                target_archive.writestr(name, value)
        rebound_archive = output.getvalue()
        self._write_rebound_evidence(input_root, archive_item, "rebound-health.zip", rebound_archive)

        metadata = json.loads((ROOT / metadata_item["path"]).read_bytes())
        self.assertEqual(metadata.get("total_count"), 1)
        self.assertEqual(len(metadata.get("artifacts", [])), 1)
        metadata["artifacts"][0]["size_in_bytes"] = len(rebound_archive)
        metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(rebound_archive)}"
        metadata_raw = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        self._write_rebound_evidence(input_root, metadata_item, "rebound-health-metadata.json", metadata_raw)

        index_path = input_root / "input-index.json"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        return index_path

    @staticmethod
    def _git_tree_sha_from_recursive_rows(rows: list[dict[str, object]]) -> str:
        """Recompute a Git tree id from a complete recursive GitHub tree response."""
        by_path = {str(row["path"]): row for row in rows}
        if len(by_path) != len(rows):
            raise AssertionError("synthetic Git tree has duplicate paths")

        def tree_sha(directory: str) -> str:
            children = []
            for path, row in by_path.items():
                if pathlib.PurePosixPath(path).parent.as_posix() != (directory or "."):
                    continue
                children.append((pathlib.PurePosixPath(path).name, row))
            children.sort(key=lambda item: (item[0] + ("/" if item[1].get("type") == "tree" else "")).encode())
            body = bytearray()
            for name, row in children:
                mode = str(row["mode"]).lstrip("0") or "0"
                child_sha = tree_sha(f"{directory}/{name}".strip("/")) if row.get("type") == "tree" else str(row["sha"])
                body.extend(mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(child_sha))
            raw = bytes(body)
            return MODULE.hashlib.sha1(f"tree {len(raw)}\0".encode("ascii") + raw).hexdigest()

        return tree_sha("")

    def _build_health_last_good_variant(
        self, *, variant: str, input_root: pathlib.Path,
    ) -> pathlib.Path:
        """Build a fully rebound synthetic Health packet using the real persister replay."""
        root = ROOT
        registry, _policy, scope_by_id = MODULE.load_and_validate_registry(root)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        index = json.loads((root / MODULE.INPUT_INDEX_PATH).read_bytes())
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_scope["scope_id"]
            or (
                item.get("subject", {}).get("stage") == "health"
                or not (
                    item.get("subject", {}).get("stage") in {"source", "processor", "promotion", "publisher", "acknowledgement"}
                    or item["role"].startswith(("publication_", "acknowledgement_"))
                    or item["role"] == "consumer_readback"
                )
            )
        ]
        health_items = [
            item for item in index["inputs"]
            if item.get("scope_id") == operation_scope["scope_id"]
            and item.get("subject", {}).get("stage") == "health"
        ]
        roles = {item["role"]: item for item in health_items}
        run = json.loads((root / roles["pipeline_run"]["path"]).read_bytes())
        workflow_head = run["head_sha"]
        archive_raw = (root / roles["pipeline_artifact_archive"]["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as source_archive:
            members = {entry.filename: source_archive.read(entry.filename) for entry in source_archive.infolist()}
        receipt = json.loads(members["health-receipt.json"])
        pre_state = json.loads(members["health-state/state.json"])
        source = next(row for row in receipt["sources"] if row["source_id"] == "data_go_kr")
        prior_good = copy.deepcopy(pre_state["last_good_by_source"].get("data_go_kr"))
        candidate_good = copy.deepcopy(source["canonical"].get("last_good"))
        if not isinstance(prior_good, dict) or not isinstance(candidate_good, dict):
            raise AssertionError("retained Health packet lacks its producer-shaped last-good identities")

        if variant == "null":
            prior_good = None
            candidate_good = None
        elif variant == "older":
            candidate_good["publication_run_id"] = 37199709257
            candidate_good["publication_run_jobs_completed_at"] = "2026-10-04T12:00:00Z"
        elif variant == "newer":
            candidate_good["publication_run_id"] = 37199709259
            candidate_good["publication_run_jobs_completed_at"] = "2026-10-04T13:22:00Z"
        else:
            raise AssertionError(f"unknown last-good test variant: {variant}")

        if prior_good is None:
            pre_state["last_good_by_source"].pop("data_go_kr", None)
        else:
            pre_state["last_good_by_source"]["data_go_kr"] = copy.deepcopy(prior_good)
        source["canonical"]["last_good"] = copy.deepcopy(candidate_good)
        persister = MODULE.import_health_persister_at_revision(root, workflow_head)
        pre_state = persister.seal(pre_state, "state_sha256")
        receipt = persister.seal(receipt, "receipt_sha256")
        checker_result = json.loads(members["checker-result.json"])
        checker_result["receipt_sha256"] = receipt["receipt_sha256"]
        receipt_raw = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        pre_state_raw = (json.dumps(pre_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        checker_result_raw = (json.dumps(checker_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        members["health-receipt.json"] = receipt_raw
        members["health-state/state.json"] = pre_state_raw
        members["checker-result.json"] = checker_result_raw
        post_state = MODULE.expected_health_post_state(
            root=root, workflow_head=workflow_head, pre_state=pre_state, receipt=receipt,
        )
        post_state_raw = (json.dumps(post_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        receipt_name = persister.receipt_file_name(receipt)

        # Rebuild the output state GitHub API responses from the exact resealed
        # bytes. Tree IDs and blob IDs are real Git object hashes; the synthetic
        # commit binds the recomputed tree and retained parent without claiming
        # an external write or runtime observation.
        tree_item = roles["health_state_tree"]
        tree_api = json.loads((root / tree_item["path"]).read_bytes())
        old_receipt_api = json.loads((root / roles["health_receipt_blob_api"]["path"]).read_bytes())
        old_receipt_rows = [row for row in tree_api["tree"] if row.get("sha") == old_receipt_api.get("sha")]
        if len(old_receipt_rows) != 1:
            raise AssertionError("retained Health tree does not uniquely bind its receipt blob")
        old_receipt_path = old_receipt_rows[0]["path"]
        receipt_blob_sha = MODULE.git_blob_sha1(receipt_raw)
        state_blob_sha = MODULE.git_blob_sha1(post_state_raw)
        receipt_member_path = f"health/upstream-catalogue/receipts/{receipt_name}"
        tree_rows = [row for row in tree_api["tree"] if row.get("path") != old_receipt_path]
        replacement_rows = {
            "health/upstream-catalogue/state.json": (state_blob_sha, len(post_state_raw)),
            receipt_member_path: (receipt_blob_sha, len(receipt_raw)),
        }
        for row in tree_rows:
            if row.get("path") in replacement_rows:
                row["sha"], row["size"] = replacement_rows[row["path"]]
                row["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{row['sha']}"
        if receipt_member_path not in {row.get("path") for row in tree_rows}:
            tree_rows.append({
                "path": receipt_member_path, "mode": "100644", "type": "blob",
                "sha": receipt_blob_sha, "size": len(receipt_raw),
                "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{receipt_blob_sha}",
            })
        tree_api["tree"] = tree_rows
        original_tree = json.loads((root / tree_item["path"]).read_bytes())
        self.assertEqual(
            self._git_tree_sha_from_recursive_rows(original_tree["tree"]), original_tree["sha"],
            "fixture builder must begin from a complete cryptographically consistent API tree",
        )
        tree_sha = self._git_tree_sha_from_recursive_rows(tree_rows)
        tree_api["sha"] = tree_sha
        tree_api["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}"

        def contents_api(role: str, path: str, raw: bytes) -> dict[str, object]:
            value = json.loads((root / roles[role]["path"]).read_bytes())
            value.update({
                "path": path, "sha": MODULE.git_blob_sha1(raw), "size": len(raw),
                "content": MODULE.base64.b64encode(raw).decode("ascii"), "encoding": "base64",
            })
            value["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{value['sha']}"
            return value

        state_api = contents_api("health_state_blob_api", "health/upstream-catalogue/state.json", post_state_raw)
        receipt_api = contents_api("health_receipt_blob_api", f"health/upstream-catalogue/receipts/{receipt_name}", receipt_raw)
        parent_sha = members["health-ref"].decode("ascii").strip().split("\t")[0]
        commit_api = json.loads((root / roles["health_state_commit"]["path"]).read_bytes())
        commit_api["tree"] = {
            **commit_api["tree"], "sha": tree_sha,
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}",
        }
        commit_raw = (
            f"tree {tree_sha}\nparent {parent_sha}\nauthor Completeness test <completeness-test@example.invalid> 1791118800 +0000\n"
            "committer Completeness test <completeness-test@example.invalid> 1791118800 +0000\n\n"
            f"Synthetic resealed Health {variant} last-good fixture\n"
        ).encode()
        commit_sha = MODULE.hashlib.sha1(f"commit {len(commit_raw)}\0".encode() + commit_raw).hexdigest()
        commit_api["sha"] = commit_sha
        commit_api["message"] = f"Synthetic resealed Health {variant} last-good fixture"
        commit_api["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/commits/{commit_sha}"
        ref_api = json.loads((root / roles["health_state_ref"]["path"]).read_bytes())
        ref_api["object"]["sha"] = commit_sha
        ref_api["object"]["url"] = commit_api["url"]
        ref_api["url"] = "https://api.github.com/repos/StatPan/datapan-registry/git/ref/heads/automation/upstream-catalogue-health-state"

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target_archive:
            for member_name, raw in members.items():
                target_archive.writestr(member_name, raw)
        archive_raw = output.getvalue()
        metadata = json.loads((root / roles["pipeline_artifact_metadata"]["path"]).read_bytes())
        metadata["artifacts"][0]["size_in_bytes"] = len(archive_raw)
        metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(archive_raw)}"

        rebound = {
            "pipeline_artifact_archive": archive_raw,
            "pipeline_artifact_metadata": (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            "health_state_ref": (json.dumps(ref_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            "health_state_commit": (json.dumps(commit_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            "health_state_tree": (json.dumps(tree_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            "health_state_blob_api": (json.dumps(state_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            "health_receipt_blob_api": (json.dumps(receipt_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        }
        for role, raw in rebound.items():
            item = roles[role]
            self._write_rebound_evidence(input_root, item, f"health-{variant}-{role}.json", raw)
        index_path = input_root / "input-index.json"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        return index_path

    @staticmethod
    def _commit_test_tree(root: pathlib.Path, changed_paths: list[str], parent: str, message: str) -> str:
        environment = CompletenessProofRollupTest._closed_git_environment()
        subprocess.run(["git", "add", "--", *changed_paths], cwd=root, check=True, env=environment)
        tree = subprocess.check_output(["git", "write-tree"], cwd=root, text=True, env=environment).strip()
        environment = {
            **environment,
            "GIT_AUTHOR_NAME": "Completeness rollup test",
            "GIT_AUTHOR_EMAIL": "completeness-test@example.invalid",
            "GIT_COMMITTER_NAME": "Completeness rollup test",
            "GIT_COMMITTER_EMAIL": "completeness-test@example.invalid",
            "GIT_AUTHOR_DATE": "2026-10-04T13:00:00Z",
            "GIT_COMMITTER_DATE": "2026-10-04T13:00:00Z",
        }
        commit = subprocess.check_output(
            ["git", "commit-tree", tree, "-p", parent], cwd=root,
            input=message + "\n", text=True, env=environment,
        ).strip()
        subprocess.run(["git", "update-ref", "refs/heads/main", commit], cwd=root, check=True, env=environment)
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", commit], cwd=root, check=True, env=environment)
        return commit

    def _clone_disposable_at_verified_main(
        self,
        source_root: pathlib.Path,
        repository: pathlib.Path,
        *,
        expected_main: str | None = None,
        transport: str = "bounded-main",
        reuse_existing_borrowed_main: bool = False,
    ) -> tuple[str, str]:
        """Select the exact trusted-main tree through a bounded or explicit local transport."""
        if transport not in {"bounded-main", "borrowed-source"}:
            raise ValueError("transport must be bounded-main or borrowed-source")
        if reuse_existing_borrowed_main and transport != "borrowed-source":
            raise ValueError("existing main history reuse requires borrowed-source transport")
        source_root = source_root.resolve()
        environment = self._closed_git_environment()
        environment.update({
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "gc.auto",
            "GIT_CONFIG_VALUE_0": "0",
            "GIT_CONFIG_KEY_1": "maintenance.auto",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "gc.autoDetach",
            "GIT_CONFIG_VALUE_2": "false",
        })

        def git_output(repo: pathlib.Path, *args: str) -> str:
            return subprocess.check_output(
                ["git", *args], cwd=repo, text=True, env=environment,
            ).strip()

        trusted_main = subprocess.check_output(
            ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
            cwd=source_root,
            text=True,
            env=environment,
        ).strip()
        if len(trusted_main) != 40 or any(character not in "0123456789abcdef" for character in trusted_main):
            raise ValueError("source origin/main is not an exact commit SHA")
        if expected_main is not None and expected_main != trusted_main:
            raise ValueError("expected trusted main does not match source origin/main")
        source_tree = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{trusted_main}^{{tree}}"],
            cwd=source_root,
            text=True,
            env=environment,
        ).strip()
        source_shallow_state = subprocess.check_output(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=source_root,
            text=True,
            env=environment,
        ).strip()
        if source_shallow_state not in {"true", "false"}:
            raise ValueError("source shallow state is not a recognized Git value")
        source_shallow_path = pathlib.Path(subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "shallow"],
            cwd=source_root,
            text=True,
            env=environment,
        ).strip())
        source_object_dir = pathlib.Path(git_output(
            source_root, "rev-parse", "--path-format=absolute", "--git-path", "objects",
        )).resolve()
        source_shallow_bytes: bytes | None = None
        if source_shallow_state == "true":
            try:
                source_shallow_bytes = source_shallow_path.read_bytes()
            except OSError as error:
                raise ValueError("shallow source boundary metadata is unavailable") from error
            if not source_shallow_bytes:
                raise ValueError("shallow source boundary metadata is empty")
        if transport == "borrowed-source":
            subprocess.run(
                ["git", "clone", "--quiet", "--shared", "--no-checkout", str(source_root), str(repository)],
                check=True,
                env=environment,
            )
        else:
            if repository.exists() or repository.is_symlink():
                raise ValueError("bounded-main destination already exists")
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
                env=environment,
            )
            initial_refs = git_output(repository, "for-each-ref", "--format=%(refname)")
            if initial_refs:
                raise ValueError("bounded-main init unexpectedly created refs")
            alternates_path = pathlib.Path(git_output(
                repository, "rev-parse", "--path-format=absolute", "--git-path", "objects/info/alternates",
            ))
            if alternates_path.exists():
                raise ValueError("bounded-main init unexpectedly has object alternates")
            local_config = git_output(repository, "config", "--local", "--list").splitlines()
            forbidden_config_prefixes = ("remote.", "extensions.partialclone=", "filter.")
            if any(entry.startswith(forbidden_config_prefixes) for entry in local_config):
                raise ValueError("bounded-main init unexpectedly has remote or promisor configuration")
        if transport == "borrowed-source" and source_shallow_bytes is not None:
            destination_shallow_path = pathlib.Path(git_output(
                repository, "rev-parse", "--path-format=absolute", "--git-path", "shallow",
            ))
            try:
                destination_shallow_bytes = destination_shallow_path.read_bytes()
            except FileNotFoundError:
                destination_shallow_bytes = None
            if destination_shallow_bytes is not None:
                if not destination_shallow_bytes:
                    raise ValueError("disposable clone shallow boundary metadata is empty")
                destination_shallow_state = git_output(repository, "rev-parse", "--is-shallow-repository")
                if destination_shallow_state != "true":
                    raise ValueError("disposable clone boundary metadata is not natively shallow")
            else:
                destination_object_dir = pathlib.Path(git_output(
                    repository, "rev-parse", "--path-format=absolute", "--git-path", "objects",
                )).resolve()
                source_object_dir = pathlib.Path(git_output(
                    source_root, "rev-parse", "--path-format=absolute", "--git-path", "objects",
                )).resolve()
                alternates_path = pathlib.Path(git_output(
                    repository, "rev-parse", "--path-format=absolute", "--git-path", "objects/info/alternates",
                ))
                try:
                    alternate_entries = alternates_path.read_text(encoding="utf-8").splitlines()
                except FileNotFoundError:
                    alternate_entries = []
                borrows_source_objects = False
                for entry in alternate_entries:
                    if not entry:
                        continue
                    alternate_path = pathlib.Path(entry)
                    if not alternate_path.is_absolute():
                        alternate_path = destination_object_dir / alternate_path
                    if alternate_path.resolve() == source_object_dir:
                        borrows_source_objects = True
                        break
                if borrows_source_objects:
                    destination_shallow_path.write_bytes(source_shallow_bytes)
                    destination_shallow_state = git_output(repository, "rev-parse", "--is-shallow-repository")
                    if destination_shallow_state != "true":
                        raise ValueError("shared disposable clone did not accept source shallow boundaries")
                    if destination_shallow_path.read_bytes() != source_shallow_bytes:
                        raise ValueError("shared disposable clone shallow boundary differs from source")
                else:
                    destination_shallow_state = git_output(repository, "rev-parse", "--is-shallow-repository")
                    if destination_shallow_state != "false":
                        raise ValueError("disposable clone is shallow without native boundary metadata")
        # A local borrowed-source clone from a shallow PR checkout need not retain its
        # common shallow boundary file. Preserve native boundaries selected by
        # Git, or transfer exact source bytes only when the clone borrows this
        # source's common object store. Transfer only the already authenticated
        # exact SHA from this local checkout; never consult its remote URL or
        # substitute HEAD.
        fetch_args = [
            "git", "fetch", "--no-tags", "--depth=64", str(source_root),
            f"{trusted_main}:refs/remotes/origin/main",
        ]
        try:
            if reuse_existing_borrowed_main:
                if source_shallow_state != "false":
                    raise ValueError("existing borrowed main history requires an unshallow source")
                alternate_path = pathlib.Path(git_output(
                    repository, "rev-parse", "--path-format=absolute", "--git-path", "objects/info/alternates",
                ))
                try:
                    alternate_entries = alternate_path.read_text(encoding="utf-8").splitlines()
                except OSError as error:
                    raise ValueError("existing borrowed main history lacks its exact object alternate") from error
                alternate_paths = set()
                destination_object_dir = pathlib.Path(git_output(
                    repository, "rev-parse", "--path-format=absolute", "--git-path", "objects",
                )).resolve()
                for entry in alternate_entries:
                    if not entry:
                        continue
                    alternate = pathlib.Path(entry)
                    if not alternate.is_absolute():
                        alternate = destination_object_dir / alternate
                    alternate_paths.add(str(alternate.resolve()))
                if alternate_paths != {str(source_object_dir)}:
                    raise ValueError("existing borrowed main history uses an unexpected object alternate")
                existing_commit = subprocess.run(
                    ["git", "cat-file", "-e", f"{trusted_main}^{{commit}}"],
                    cwd=repository, check=False, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                existing_tree = subprocess.run(
                    ["git", "rev-parse", "--verify", f"{trusted_main}^{{tree}}"],
                    cwd=repository, check=False, text=True, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                if existing_commit.returncode != 0 or existing_tree.returncode != 0:
                    raise ValueError("borrowed source lacks the exact pinned main commit or tree")
                if existing_tree.stdout.strip() != source_tree:
                    raise ValueError("borrowed source main tree differs from its authenticated source tree")
                subprocess.run(
                    ["git", "update-ref", "refs/remotes/origin/main", trusted_main],
                    cwd=repository, check=True, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            else:
                subprocess.run(
                    fetch_args,
                    cwd=repository,
                    check=True,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
        except subprocess.CalledProcessError as error:
            # Keep the original failure and fetch argv, but retain enough
            # local topology context to diagnose shallow linked-worktree
            # transport failures in Verify. This runs only on fetch failure;
            # it never consults a remote URL or changes the fetch behavior.
            def git_value(repo: pathlib.Path, *args: str) -> str:
                try:
                    result = subprocess.run(
                        ["git", *args], cwd=repo, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=2,
                        env=environment,
                    )
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    return "<unavailable>"
                if result.returncode != 0:
                    return f"<unavailable:{result.returncode}>"
                return result.stdout.strip()

            def repository_context(repo: pathlib.Path) -> dict[str, object]:
                common_dir = git_value(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
                git_dir = git_value(repo, "rev-parse", "--path-format=absolute", "--git-dir")
                shallow_path = git_value(repo, "rev-parse", "--path-format=absolute", "--git-path", "shallow")
                alternates_path = git_value(
                    repo, "rev-parse", "--path-format=absolute", "--git-path", "objects/info/alternates",
                )

                def file_metadata(value: str) -> dict[str, object]:
                    if value.startswith("<unavailable"):
                        return {"path": value, "exists": False}
                    try:
                        path = pathlib.Path(value)
                        size = path.stat().st_size
                        with path.open("rb") as stream:
                            sample = stream.read(4096)
                    except OSError:
                        return {"path": value, "exists": False}
                    return {
                        "path": value,
                        "exists": True,
                        "bytes": size,
                        "sampled_bytes": len(sample),
                        "sampled_lines": len(sample.splitlines()),
                        "truncated": size > len(sample),
                    }

                main = git_value(repo, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
                main_tree = (
                    git_value(repo, "rev-parse", "--verify", f"{main}^{{tree}}")
                    if not main.startswith("<unavailable") else "<unavailable>"
                )
                try:
                    filesystem_free_bytes: int | str = shutil.disk_usage(repo).free
                except Exception as metadata_error:
                    filesystem_free_bytes = f"<unavailable:{type(metadata_error).__name__}>"
                return {
                    "path": str(repo),
                    "head": git_value(repo, "rev-parse", "--verify", "HEAD^{commit}"),
                    "origin_main": main,
                    "origin_main_tree": main_tree,
                    "is_shallow": git_value(repo, "rev-parse", "--is-shallow-repository"),
                    "git_dir": git_dir,
                    "common_dir": common_dir,
                    "shallow": file_metadata(shallow_path),
                    "alternates": file_metadata(alternates_path),
                    "filesystem_free_bytes": filesystem_free_bytes,
                }

            prefix = "bounded exact-main fetch diagnostics: "
            note_limit = 2800
            stderr = error.stderr
            if isinstance(stderr, bytes):
                stderr_bytes = stderr
            elif isinstance(stderr, str):
                stderr_bytes = stderr.encode("utf-8", errors="replace")
            else:
                stderr_bytes = b""
            stderr_tail = stderr_bytes[-1024:].decode("utf-8", errors="replace")
            diagnostic: dict[str, object] = {
                "expected_main": trusted_main,
                "expected_main_tree": source_tree,
                "stderr_tail_bytes": min(len(stderr_bytes), 1024),
                "stderr_truncated": len(stderr_bytes) > 1024,
                "stderr_tail": stderr_tail,
            }

            def render_note(value: dict[str, object]) -> str:
                return prefix + json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

            def fit_required_note() -> None:
                # Keep exact source identities and the most recent stderr. The
                # original CalledProcessError.stderr remains untouched; this
                # only bounds the human-readable exception note after JSON
                # escaping, including control characters from Git/path output.
                while len(render_note(diagnostic)) >= note_limit:
                    tail = diagnostic.get("stderr_tail")
                    if not isinstance(tail, str) or not tail:
                        break
                    drop = max(1, len(tail) // 8)
                    diagnostic["stderr_tail"] = tail[drop:]
                    diagnostic["stderr_truncated"] = True

            def bounded_optional_text(value: str, encoded_limit: int = 128) -> str:
                if len(json.dumps(value, ensure_ascii=True)) <= encoded_limit:
                    return value
                marker = "<snip>"
                left = len(value) // 2
                right = len(value) - left
                while left or right:
                    candidate = value[:left] + marker + value[len(value) - right:]
                    if len(json.dumps(candidate, ensure_ascii=True)) <= encoded_limit:
                        return candidate
                    if left >= right and left:
                        left -= max(1, left // 8)
                    elif right:
                        right -= max(1, right // 8)
                return marker

            def bounded_optional(value: object) -> object:
                if isinstance(value, str):
                    return bounded_optional_text(value)
                if isinstance(value, dict):
                    return {key: bounded_optional(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [bounded_optional(item) for item in value]
                return value

            fit_required_note()
            # Optional diagnostics are evaluated only after the mandatory
            # identity/stderr payload exists. Each field is admitted separately
            # so unusually long paths cannot push the final note over budget.
            try:
                git_version = bounded_optional(git_value(source_root, "--version"))
                candidate = dict(diagnostic)
                candidate["git_version"] = git_version
                if len(render_note(candidate)) < note_limit:
                    diagnostic = candidate
                for role, repo in (("source", source_root), ("destination", repository)):
                    try:
                        context = repository_context(repo)
                    except Exception as metadata_error:
                        context = {"context_unavailable": type(metadata_error).__name__}
                    context = bounded_optional(context)
                    selected: dict[str, object] = {}
                    for key, value in context.items():
                        trial = dict(diagnostic)
                        partial = dict(selected)
                        partial[key] = value
                        trial[role] = partial
                        if len(render_note(trial)) < note_limit:
                            selected = partial
                            diagnostic = trial
            except Exception as metadata_error:  # optional metadata never replaces mandatory evidence
                trial = dict(diagnostic)
                trial["optional_metadata_error"] = type(metadata_error).__name__
                if len(render_note(trial)) < note_limit:
                    diagnostic = trial

            fit_required_note()
            note = render_note(diagnostic)
            if len(note) >= note_limit:
                # Exact Git identities are fixed-length here; this final
                # fallback protects the cap if an unexpected optional value
                # bypasses the per-field admission checks.
                diagnostic = {
                    "expected_main": trusted_main,
                    "expected_main_tree": source_tree,
                    "stderr_truncated": True,
                    "stderr_tail": str(diagnostic.get("stderr_tail", ""))[-256:],
                }
                note = render_note(diagnostic)
            try:
                if hasattr(error, "add_note"):
                    error.add_note(note)
                else:
                    print(note, file=sys.stderr)
            except Exception:
                # Adding diagnostics is best-effort and cannot replace the
                # original Git fetch error.
                pass
            raise
        copied_main = git_output(repository, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
        copied_tree = git_output(repository, "rev-parse", "--verify", f"{copied_main}^{{tree}}")
        self.assertEqual(copied_main, trusted_main)
        self.assertEqual(copied_tree, source_tree)
        if reuse_existing_borrowed_main:
            if git_output(repository, "rev-parse", "--is-shallow-repository") != "false":
                raise ValueError("existing borrowed main history unexpectedly has a shallow boundary")
        if transport == "bounded-main":
            fetched_refs = git_output(repository, "for-each-ref", "--format=%(refname)")
            self.assertEqual(fetched_refs, "refs/remotes/origin/main")
            alternates_path = pathlib.Path(git_output(
                repository, "rev-parse", "--path-format=absolute", "--git-path", "objects/info/alternates",
            ))
            self.assertFalse(alternates_path.exists())
            local_config = git_output(repository, "config", "--local", "--list").splitlines()
            forbidden_config_prefixes = ("remote.", "extensions.partialclone=", "filter.")
            self.assertFalse(any(entry.startswith(forbidden_config_prefixes) for entry in local_config))
        subprocess.run(
            ["git", "checkout", "--quiet", "--detach", trusted_main],
            cwd=repository,
            check=True,
            env=environment,
        )
        checked_out_main = git_output(repository, "rev-parse", "--verify", "HEAD^{commit}")
        checked_out_tree = git_output(repository, "rev-parse", "--verify", "HEAD^{tree}")
        self.assertEqual(checked_out_main, trusted_main)
        self.assertEqual(checked_out_tree, source_tree)
        return trusted_main, source_tree

    def _read_native_shallow_boundaries(
        self, source_root: pathlib.Path,
    ) -> tuple[str, pathlib.Path, bytes | None]:
        """Read Git's own shallow state and exact common boundary bytes."""
        source_root = source_root.resolve()
        environment = self._closed_git_environment()
        source_shallow_state = subprocess.check_output(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=source_root,
            text=True,
            env=environment,
        ).strip()
        if source_shallow_state not in {"true", "false"}:
            raise ValueError("source shallow state is not a recognized Git value")
        source_shallow_path = pathlib.Path(subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "shallow"],
            cwd=source_root,
            text=True,
            env=environment,
        ).strip())
        if not source_shallow_path.is_absolute():
            raise ValueError("source shallow boundary path is not absolute")
        source_shallow_bytes: bytes | None = None
        if source_shallow_state == "true":
            try:
                source_shallow_bytes = source_shallow_path.read_bytes()
            except OSError as error:
                raise ValueError("shallow source boundary metadata is unavailable") from error
            if not source_shallow_bytes:
                raise ValueError("shallow source boundary metadata is empty")
            # Let Git parse both the native boundary syntax and the selected
            # ref graph before these bytes are trusted by another repository.
            subprocess.run(
                ["git", "rev-list", "--all", "--parents"],
                cwd=source_root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
        return source_shallow_state, source_shallow_path, source_shallow_bytes

    def _install_borrowed_origin_shallow_boundaries(
        self,
        source_root: pathlib.Path,
        origin_git_dir: pathlib.Path,
        *,
        source_shallow_state: str,
        source_shallow_bytes: bytes | None,
    ) -> tuple[pathlib.Path, bytes | None]:
        """Preserve exact boundaries in a fresh bare origin that borrows source objects."""
        source_root = source_root.resolve()
        origin_git_dir = origin_git_dir.resolve()
        environment = self._closed_git_environment()
        destination_shallow_path = pathlib.Path(subprocess.check_output(
            [
                "git", "--git-dir", str(origin_git_dir), "rev-parse",
                "--path-format=absolute", "--git-path", "shallow",
            ],
            cwd=source_root,
            text=True,
            env=environment,
        ).strip())
        if not destination_shallow_path.is_absolute():
            raise ValueError("bare-origin shallow boundary path is not absolute")

        if source_shallow_state == "false":
            destination_state = subprocess.check_output(
                ["git", "--git-dir", str(origin_git_dir), "rev-parse", "--is-shallow-repository"],
                cwd=source_root,
                text=True,
                env=environment,
            ).strip()
            if destination_state != "false" or destination_shallow_path.exists():
                raise ValueError("full-history source produced a shallow bare origin")
            if source_shallow_bytes is not None:
                raise ValueError("full-history source unexpectedly supplied shallow boundary bytes")
            return destination_shallow_path, None

        if source_shallow_state != "true":
            raise ValueError("source shallow state is not a recognized Git value")
        if not source_shallow_bytes:
            raise ValueError("shallow source boundary metadata is empty")

        try:
            destination_bytes = destination_shallow_path.read_bytes()
        except FileNotFoundError:
            destination_bytes = None
        except OSError as error:
            raise ValueError("bare-origin shallow boundary metadata is unavailable") from error

        if destination_bytes is not None:
            if not destination_bytes:
                raise ValueError("bare-origin shallow boundary metadata is empty")
            if destination_bytes != source_shallow_bytes:
                raise ValueError("bare-origin shallow boundary metadata conflicts with source")
        else:
            with destination_shallow_path.open("xb") as stream:
                stream.write(source_shallow_bytes)

        destination_state = subprocess.check_output(
            ["git", "--git-dir", str(origin_git_dir), "rev-parse", "--is-shallow-repository"],
            cwd=source_root,
            text=True,
            env=environment,
        ).strip()
        if destination_state != "true":
            raise ValueError("bare origin did not accept source shallow boundaries")
        try:
            copied_bytes = destination_shallow_path.read_bytes()
        except OSError as error:
            raise ValueError("bare-origin shallow boundary metadata is unavailable after copy") from error
        if copied_bytes != source_shallow_bytes:
            raise ValueError("bare-origin shallow boundary metadata differs from source")
        return destination_shallow_path, copied_bytes

    def _make_genuine_shallow_local_source(
        self, directory: pathlib.Path, *, source_root: pathlib.Path = ROOT,
    ) -> tuple[pathlib.Path, str, str, str]:
        """Create a local depth-1 PR checkout and separately fetch its trusted main."""
        source_root = source_root.resolve()
        environment = self._closed_git_environment()
        pr_head = subprocess.check_output(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=source_root, text=True, env=environment,
        ).strip()
        trusted_main = subprocess.check_output(
            ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
            cwd=source_root,
            text=True,
            env=environment,
        ).strip()
        trusted_tree = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{trusted_main}^{{tree}}"], cwd=source_root, text=True, env=environment,
        ).strip()
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", trusted_main, pr_head],
            cwd=source_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            check=False, timeout=30,
        )
        if ancestry.returncode != 0:
            raise ValueError("local PR fixture is not descended from the pinned trusted main")
        producer_ancestry_count = int(subprocess.check_output(
            ["git", "rev-list", "--count", f"{trusted_main}..{pr_head}"],
            cwd=source_root, text=True, env=environment,
        ).strip())
        if not 1 <= producer_ancestry_count <= 63:
            raise ValueError("local PR fixture ancestry is outside the bounded shallow-source window")
        producer_depth = producer_ancestry_count + 1
        source_object_dir = pathlib.Path(subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "objects"],
            cwd=source_root,
            text=True,
            env=environment,
        ).strip()).resolve()
        if not source_object_dir.is_absolute():
            raise ValueError("source object directory path is not absolute")
        source_shallow_state, source_shallow_path, source_shallow_bytes = (
            self._read_native_shallow_boundaries(source_root)
        )
        source_shallow_file_before = (
            source_shallow_path.read_bytes() if source_shallow_path.exists() else None
        )

        remote = directory / "shallow-source-origin.git"
        subprocess.run(
            ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(remote)], check=True,
            env=environment,
        )
        alternate = remote / "objects/info/alternates"
        alternate.parent.mkdir(parents=True, exist_ok=True)
        alternate.write_text(str(source_object_dir) + "\n", encoding="utf-8")
        destination_shallow_path, _copied_shallow_bytes = self._install_borrowed_origin_shallow_boundaries(
            source_root,
            remote,
            source_shallow_state=source_shallow_state,
            source_shallow_bytes=source_shallow_bytes,
        )
        subprocess.run(
            ["git", "--git-dir", str(remote), "update-ref", "refs/heads/main", trusted_main], check=True,
            env=environment,
        )
        if pr_head == trusted_main:
            # This private source can be exactly at fetched main. Give the
            # isolated shallow-PR fixture its own child commit without
            # changing the source checkout's refs or object database.
            fixture_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Completeness shallow fixture",
                "GIT_AUTHOR_EMAIL": "completeness-shallow@example.invalid",
                "GIT_COMMITTER_NAME": "Completeness shallow fixture",
                "GIT_COMMITTER_EMAIL": "completeness-shallow@example.invalid",
                "GIT_AUTHOR_DATE": "2026-10-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-10-01T00:00:00Z",
            }
            pr_head = subprocess.check_output(
                ["git", "--git-dir", str(remote), "commit-tree", trusted_tree, "-p", trusted_main],
                env=fixture_env,
                text=True,
            ).strip()
        subprocess.run(
            ["git", "--git-dir", str(remote), "update-ref", "refs/heads/fixture-pr", pr_head], check=True,
            env=environment,
        )

        checkout = directory / "shallow-pr-checkout"
        subprocess.run(
            [
                "git", "clone", "--quiet", "--no-tags", "--depth=1", "--branch", "fixture-pr",
                remote.as_uri(), str(checkout),
            ],
            check=True,
            env=environment,
        )
        self.assertEqual(
            subprocess.check_output(["git", "rev-parse", "--is-shallow-repository"], cwd=checkout, text=True, env=environment).strip(),
            "true",
        )
        self.assertEqual(
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True, env=environment).strip(), pr_head,
        )
        self.assertNotEqual(pr_head, trusted_main)
        with self.assertRaises(subprocess.CalledProcessError):
            subprocess.run(
                ["git", "rev-parse", "refs/remotes/origin/main"],
                cwd=checkout,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
        subprocess.run(
            [
                "git", "fetch", "--no-tags", f"--depth={producer_depth}", "origin",
                "+refs/heads/fixture-pr:refs/remotes/origin/fixture-pr",
            ],
            cwd=checkout,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        subprocess.run(
            [
                "git", "fetch", "--no-tags", "--depth=1", "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ],
            cwd=checkout,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        fetched_main = subprocess.check_output(
            ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
            cwd=checkout,
            text=True,
            env=environment,
        ).strip()
        fetched_tree = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{fetched_main}^{{tree}}"], cwd=checkout, text=True, env=environment,
        ).strip()
        self.assertEqual(fetched_main, trusted_main)
        self.assertEqual(fetched_tree, trusted_tree)
        self.assertEqual(
            subprocess.check_output(["git", "rev-parse", "--is-shallow-repository"], cwd=checkout, text=True, env=environment).strip(),
            "true",
        )
        self.assertEqual(
            subprocess.check_output(["git", "rev-parse", "HEAD^{commit}"], cwd=source_root, text=True, env=environment).strip(),
            pr_head,
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "rev-parse", "refs/remotes/origin/main^{commit}"], cwd=source_root, text=True,
                env=environment,
            ).strip(),
            trusted_main,
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "rev-parse", "--is-shallow-repository"], cwd=source_root, text=True,
                env=environment,
            ).strip(),
            source_shallow_state,
        )
        source_shallow_file_after = source_shallow_path.read_bytes() if source_shallow_path.exists() else None
        self.assertEqual(source_shallow_file_after, source_shallow_file_before)
        if source_shallow_state == "true":
            self.assertEqual(destination_shallow_path.read_bytes(), source_shallow_bytes)
        else:
            self.assertFalse(destination_shallow_path.exists())

        # Verify Release materializes this manifest-bound LFS object before
        # running the feature suite. Reuse those local bytes without a pull.
        source_registry = source_root / "data/data-go-kr.registry.json"
        materialized_registry = checkout / "data/data-go-kr.registry.json"
        if source_registry.stat().st_size <= 1024:
            raise AssertionError("shallow caller fixture requires the already materialized canonical registry")
        materialized_registry.unlink()
        try:
            os.link(source_registry, materialized_registry)
        except OSError:
            shutil.copy2(source_registry, materialized_registry)
        return checkout, pr_head, trusted_main, trusted_tree

    def _rebind_synthetic_processor_history(
        self, *, index: dict[str, object], input_root: pathlib.Path, repository: pathlib.Path,
        variant: str,
    ) -> None:
        """Re-seal a local B checkpoint with either two distinct locators or a duplicate replay."""
        processor_rows = [
            item for item in index["inputs"]  # type: ignore[index]
            if item.get("scope_id") == "data-go-kr.api-operations"
            and item.get("subject", {}).get("stage") == "processor"
        ]
        roles = {item["role"]: item for item in processor_rows}
        generation_api = json.loads((ROOT / roles["processor_generation_api"]["path"]).read_bytes())
        checkpoint = json.loads(MODULE.base64.b64decode(generation_api["content"]))
        archive_item = roles["pipeline_artifact_archive"]
        archive_raw = (ROOT / archive_item["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as source_archive:
            original_uploaded_checkpoint = json.loads(
                source_archive.read("upstream-catalogue-checkpoint-receipt.json")
            )
        locators = copy.deepcopy(checkpoint["input_artifacts"])
        if len(locators) != 1:
            raise AssertionError("retained fixture must begin with its original single-A locator")
        latest = locators[-1]
        if variant == "distinct-second-observation":
            earlier = copy.deepcopy(latest)
            earlier.update({
                "run_id": "99972210001", "artifact_id": "99972210002",
                "name": "upstream-catalog-refresh-99972210001",
                "expires_at": "2026-10-04T13:00:00Z",
                "candidate_sha256": "a" * 64,
                "evidence_sha256": "b" * 64,
                "diff_sha256": "c" * 64,
            })
            checkpoint["input_artifacts"] = [earlier, *locators]
            checkpoint["observation_count"] = 2
        elif variant == "duplicate-replay":
            checkpoint["input_artifacts"] = [*locators, copy.deepcopy(latest)]
            checkpoint["observation_count"] = 2
        else:
            raise AssertionError(f"unknown synthetic B history variant: {variant}")

        unsigned = copy.deepcopy(checkpoint)
        unsigned.pop("checkpoint_sha256", None)
        checkpoint["checkpoint_sha256"] = MODULE.sha256_bytes(
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        checkpoint_raw = json.dumps(
            checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
        uploaded_checkpoint = copy.deepcopy(checkpoint)
        uploaded_checkpoint["output_artifact"]["artifact_id"] = None
        uploaded_checkpoint["output_artifact"]["expires_at"] = original_uploaded_checkpoint[
            "output_artifact"
        ]["expires_at"]
        uploaded_checkpoint["last_heartbeat_at"] = original_uploaded_checkpoint["last_heartbeat_at"]
        unsigned_uploaded = copy.deepcopy(uploaded_checkpoint)
        unsigned_uploaded.pop("checkpoint_sha256", None)
        uploaded_checkpoint["checkpoint_sha256"] = MODULE.sha256_bytes(
            json.dumps(unsigned_uploaded, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        uploaded_checkpoint_raw = json.dumps(
            uploaded_checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
        generation_api.update({
            "content": MODULE.base64.b64encode(checkpoint_raw).decode("ascii"),
            "encoding": "base64", "size": len(checkpoint_raw),
            "sha": MODULE.git_blob_sha1(checkpoint_raw),
        })
        generation_api["url"] = (
            f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{generation_api['sha']}"
        )

        member_name = "upstream-catalogue-checkpoint-receipt.json"
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as source_archive:
            members = {info.filename: source_archive.read(info) for info in source_archive.infolist()}
        if member_name not in members:
            raise AssertionError("retained B artifact lacks its producer checkpoint receipt member")
        members[member_name] = uploaded_checkpoint_raw
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target_archive:
            for name, raw in members.items():
                target_archive.writestr(name, raw)
        rebound_archive = output.getvalue()
        metadata = json.loads((ROOT / roles["pipeline_artifact_metadata"]["path"]).read_bytes())
        metadata["artifacts"][0]["size_in_bytes"] = len(rebound_archive)
        metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(rebound_archive)}"

        state_tree_api = json.loads((ROOT / roles["processor_state_tree"]["path"]).read_bytes())
        rows = copy.deepcopy(state_tree_api["tree"])
        generation_path = generation_api["path"]
        generation_rows = [row for row in rows if row.get("path") == generation_path]
        if len(generation_rows) != 1:
            raise AssertionError("B state tree must contain its exact generation blob once")
        generation_rows[0]["sha"] = MODULE.git_blob_sha1(checkpoint_raw)
        generation_rows[0]["size"] = len(checkpoint_raw)
        generation_rows[0]["url"] = (
            "https://api.github.com/repos/StatPan/datapan-registry/git/blobs/"
            f"{generation_rows[0]['sha']}"
        )
        directories = sorted(
            (row["path"] for row in rows if row.get("type") == "tree"),
            key=lambda value: value.count("/"), reverse=True,
        )
        for directory in directories:
            children = [
                row for row in rows
                if pathlib.PurePosixPath(row["path"]).parent.as_posix() == directory
            ]
            children.sort(key=lambda row: (
                pathlib.PurePosixPath(row["path"]).name
                + ("/" if row.get("type") == "tree" else "")
            ).encode())
            body = bytearray()
            for child in children:
                mode = str(child["mode"]).lstrip("0") or "0"
                body.extend(
                    mode.encode("ascii") + b" "
                    + pathlib.PurePosixPath(child["path"]).name.encode("utf-8")
                    + b"\0" + bytes.fromhex(child["sha"])
                )
            digest = MODULE.hashlib.sha1(f"tree {len(body)}\0".encode("ascii") + body).hexdigest()
            directory_row = next(row for row in rows if row.get("path") == directory)
            directory_row["sha"] = digest
            directory_row["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{digest}"
        tree_sha = self._git_tree_sha_from_recursive_rows(rows)
        state_tree_api.update({
            "sha": tree_sha,
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}",
            "tree": rows,
        })
        root_children = []
        for row in rows:
            if pathlib.PurePosixPath(row["path"]).parent.as_posix() == ".":
                name = pathlib.PurePosixPath(row["path"]).name
                root_children.append((
                    (name + ("/" if row.get("type") == "tree" else "")).encode(), name, row,
                ))
        root_children.sort(key=lambda value: value[0])
        tree_body = bytearray()
        for _sort_name, name, row in root_children:
            mode = str(row["mode"]).lstrip("0") or "0"
            tree_body.extend(
                mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(row["sha"])
            )
        written_tree = subprocess.check_output(
            ["git", "hash-object", "-w", "-t", "tree", "--stdin"], cwd=repository,
            input=bytes(tree_body),
        ).decode("ascii").strip()
        if written_tree != tree_sha:
            raise AssertionError("synthetic B state tree API SHA does not match its Git tree bytes")

        old_commit = json.loads((ROOT / roles["processor_state_commit"]["path"]).read_bytes())
        author = "Completeness fixture <completeness-test@example.invalid>"
        commit_content = (
            f"tree {tree_sha}\nparent {old_commit['sha']}\n"
            f"author {author} 1791110520 +0000\ncommitter {author} 1791110520 +0000\n\n"
            "Synthetic producer-shaped B observation history\n"
        ).encode()
        commit_sha = subprocess.check_output(
            ["git", "hash-object", "-w", "-t", "commit", "--stdin"], cwd=repository,
            input=commit_content,
        ).decode("ascii").strip()
        commit_api = copy.deepcopy(old_commit)
        commit_api.update({
            "sha": commit_sha,
            "tree": {**old_commit["tree"], "sha": tree_sha,
                    "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}"},
            "parents": [{"sha": old_commit["sha"], "url": old_commit["url"], "html_url": old_commit["html_url"]}],
            "message": "Synthetic producer-shaped B observation history",
        })
        ref_api = json.loads((ROOT / roles["processor_state_ref"]["path"]).read_bytes())
        ref_api["object"]["sha"] = commit_sha
        ref_api["object"]["url"] = commit_api["url"]

        for role, filename, raw in (
            ("processor_generation_api", "synthetic-b-generation-api.json", json.dumps(
                generation_api, ensure_ascii=False, indent=2, sort_keys=True,
            ).encode() + b"\n"),
            ("pipeline_artifact_archive", "synthetic-b-observation-history.zip", rebound_archive),
            ("pipeline_artifact_metadata", "synthetic-b-artifact-metadata.json", json.dumps(
                metadata, ensure_ascii=False, indent=2, sort_keys=True,
            ).encode() + b"\n"),
            ("processor_state_tree", "synthetic-b-state-tree.json", json.dumps(
                state_tree_api, ensure_ascii=False, indent=2, sort_keys=True,
            ).encode() + b"\n"),
            ("processor_state_commit", "synthetic-b-state-commit.json", json.dumps(
                commit_api, ensure_ascii=False, indent=2, sort_keys=True,
            ).encode() + b"\n"),
            ("processor_state_ref", "synthetic-b-state-ref.json", json.dumps(
                ref_api, ensure_ascii=False, indent=2, sort_keys=True,
            ).encode() + b"\n"),
        ):
            self._write_rebound_evidence(input_root, roles[role], filename, raw)

    def _make_distinct_composer_producer(
        self, source_root: pathlib.Path, directory: pathlib.Path, parent_head: str,
    ) -> tuple[pathlib.Path, str]:
        """Build a complete local producer tree with a distinct composer identity."""
        source_root = source_root.resolve()
        environment = self._closed_git_environment()
        environment.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        })
        subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(source_root), str(directory)],
            check=True, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        current_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{commit}"], cwd=directory, text=True, env=environment,
        ).strip()
        if current_head != parent_head:
            raise AssertionError("wrong-producer fixture did not begin at the positive producer commit")
        composer_relative = "scripts/compose-upstream-catalogue-candidate.py"
        composer_path = directory / composer_relative
        changed_composer = composer_path.read_bytes() + b"\n# isolated authenticated composer identity negative fixture\n"
        composer_path.write_bytes(changed_composer)
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "add", "--", composer_relative],
            check=True, env=environment, cwd=directory, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30,
        )
        commit_environment = {
            **environment,
            "GIT_AUTHOR_NAME": "Datapan isolated fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Datapan isolated fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
             "-c", "user.name=Datapan isolated fixture", "-c", "user.email=fixture@example.invalid",
             "commit", "--quiet", "-m", "Isolate composer source identity"],
            check=True, env=commit_environment, cwd=directory, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30,
        )
        wrong_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{commit}"], cwd=directory, text=True, env=environment,
        ).strip()
        observed_parent = subprocess.check_output(
            ["git", "rev-parse", "HEAD^"], cwd=directory, text=True, env=environment,
        ).strip()
        if wrong_head == parent_head or observed_parent != parent_head:
            raise AssertionError("wrong-producer commit is not a distinct direct child of the positive source")
        changed_paths = subprocess.check_output(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD^", "HEAD"],
            cwd=directory, text=True, env=environment,
        ).splitlines()
        if changed_paths != [composer_relative]:
            raise AssertionError("wrong-producer fixture changed files beyond the composer source")
        tree_paths = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", "-z", "HEAD"],
            cwd=directory, env=environment,
        ).split(b"\0")
        available = {row.decode("utf-8") for row in tree_paths if row}
        required = {
            "scripts/run-canonical-update-promotion.py",
            "scripts/canonical_update_pr.py",
            "scripts/upstream_catalogue_cache_source.py",
            "scripts/compose-upstream-catalogue-candidate.py",
            "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
            "schemas/datapan.catalogue-composition-receipt.v1.schema.json",
        }
        if not required.issubset(available):
            raise AssertionError("wrong-producer fixture lacks its modern control/schema/helper closure")
        return directory, wrong_head

    @contextlib.contextmanager
    def _candidate_processor_execution_control(
        self,
        repository: pathlib.Path,
        *,
        exercise_wrong_producer: bool = False,
        wrong_producer: tuple[pathlib.Path, str] | None = None,
        data_helper_profile: str = "historical-trusted-main",
        data_helper_head_sha: str | None = None,
    ):
        """Execute the candidate C validator against immutable historical fixture data."""
        execution_root = SCRIPT.parents[1].resolve()
        execution_contract_path = execution_root / "scripts/run-canonical-update-promotion.py"
        execution_checkpoint_schema_path = (
            execution_root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
        )
        execution_helper_path = execution_root / "scripts/canonical_update_pr.py"
        execution_composition_schema_path = (
            execution_root / "schemas/datapan.catalogue-composition-receipt.v1.schema.json"
        )
        execution_source_identity = {
            "contract": hashlib.sha256(execution_contract_path.read_bytes()).hexdigest(),
            "checkpoint_schema": hashlib.sha256(
                execution_checkpoint_schema_path.read_bytes(),
            ).hexdigest(),
            "helper": hashlib.sha256(execution_helper_path.read_bytes()).hexdigest(),
            "composition_schema": hashlib.sha256(
                execution_composition_schema_path.read_bytes(),
            ).hexdigest(),
        }
        requested_contract_path = (repository / "scripts/run-canonical-update-promotion.py").resolve()
        requested_checkpoint_schema_path = (
            repository / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
        ).resolve()
        data_helper_path = (repository / "scripts/canonical_update_pr.py").resolve()
        data_helper_before = data_helper_path.read_bytes()
        data_helper_sha256 = hashlib.sha256(data_helper_before).hexdigest()
        data_main = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/main^{commit}"],
            cwd=repository, text=True,
        ).strip()
        if data_helper_profile == "historical-trusted-main":
            selected_data_helper_head = data_main
            expected_data_helper_sha256 = (
                "25d8519b9a873315adb3abdb53c1d7812e9443715f7991bdc5677ebbf98bb475"
            )
        elif data_helper_profile == "modern-producer":
            if not isinstance(data_helper_head_sha, str) or len(data_helper_head_sha) != 40:
                raise AssertionError("modern producer profile requires its exact producer commit")
            selected_data_helper_head = subprocess.check_output(
                ["git", "rev-parse", "--verify", f"{data_helper_head_sha}^{{commit}}"],
                cwd=repository, text=True,
            ).strip()
            self.assertEqual(selected_data_helper_head, data_helper_head_sha)
            expected_data_helper_sha256 = (
                "1f30f955dc9200b3c42484831dc0a8172100be84add06fa5b1ed578a2f9e0359"
            )
        else:
            raise AssertionError(f"unsupported canonical_update_pr profile: {data_helper_profile}")
        data_helper_git_blob = subprocess.check_output(
            ["git", "show", f"{selected_data_helper_head}:scripts/canonical_update_pr.py"],
            cwd=repository,
        )
        self.assertEqual(data_helper_sha256, expected_data_helper_sha256)
        self.assertEqual(hashlib.sha256(data_helper_git_blob).hexdigest(), expected_data_helper_sha256)
        self.assertEqual(data_helper_git_blob, data_helper_before)
        expected_composition_schema = json.loads(execution_composition_schema_path.read_bytes())
        original_import_module = MODULE.import_module
        observations: dict[str, list[object]] = {
            "bundle_validation_heads": [],
            "modern_generation_validations": [],
            "modern_compatibility_mismatches": [],
            "promotion_stage_results": [],
            "execution_contract_identities": [],
            "expected_execution_contract_identities": [
                (execution_contract_path, execution_source_identity["contract"]),
            ],
            "execution_schema_requests": [],
            "execution_helper_requests": [],
            "execution_helper_loader_roots": [],
            "candidate_helper_loads": [],
            "composer_identity_requests": [],
            "wrong_head_rejections": [],
            "data_helper_profile": [(data_helper_profile, selected_data_helper_head, expected_data_helper_sha256)],
        }
        original_validate_generation_inputs = MODULE.validate_processor_generation_inputs

        def validate_generation_inputs_with_control(**kwargs: object):
            result = original_validate_generation_inputs(**kwargs)
            checkpoint = kwargs.get("checkpoint")
            root = kwargs.get("root")
            promotion = kwargs.get("promotion")
            processor_head_sha = kwargs.get("processor_head_sha")
            source_reader = kwargs.get("source_reader")
            generation_inputs = (
                checkpoint.get("generation_inputs")
                if isinstance(checkpoint, dict) else None
            )
            if (
                isinstance(result, tuple) and not result[0]
                and isinstance(root, pathlib.Path) and promotion is not None
                and isinstance(processor_head_sha, str)
                and source_reader is not None
            ):
                mismatches = []
                for path in result[1]:
                    historical = source_reader.source_file(
                        "StatPan/datapan-registry", processor_head_sha, path,
                        maximum_bytes=kwargs["cache_source_helper"].MAX_SOURCE_FILE_BYTES,
                    )
                    current = (root / path).read_bytes()
                    mismatches.append({
                        "path": path,
                        "historical_sha256": hashlib.sha256(historical).hexdigest(),
                        "current_sha256": hashlib.sha256(current).hexdigest(),
                        "current_size": len(current),
                        "git_head": subprocess.check_output(
                            ["git", "rev-parse", "HEAD^{commit}"], cwd=root, text=True,
                        ).strip(),
                        "git_blob_sha": subprocess.check_output(
                            ["git", "rev-parse", f"HEAD:{path}"], cwd=root, text=True,
                        ).strip(),
                    })
                observations["modern_compatibility_mismatches"].append(mismatches)
            observations["modern_generation_validations"].append({
                "processor_head_sha": kwargs.get("processor_head_sha"),
                "cache_source_profile": (
                    generation_inputs.get("cache_source_profile")
                    if isinstance(generation_inputs, dict) else None
                ),
                "result": result,
            })
            return result

        original_validate_promotion_stage = MODULE.validate_promotion_stage

        def validate_promotion_stage_with_control(**kwargs: object):
            result = original_validate_promotion_stage(**kwargs)
            observations["promotion_stage_results"].append(result)
            return result

        def import_with_candidate_processor_control(name: str, path: pathlib.Path):
            requested_path = path.resolve()
            execution_path = execution_contract_path if name == "completeness_processor_contract" else path
            loaded = original_import_module(name, execution_path)
            if name != "completeness_processor_contract":
                return loaded

            self.assertEqual(requested_path, requested_contract_path)
            loaded_path = pathlib.Path(loaded.__file__).resolve()
            loaded_sha256 = hashlib.sha256(loaded_path.read_bytes()).hexdigest()
            observations["execution_contract_identities"].append((loaded_path, loaded_sha256))
            self.assertEqual(loaded_path, execution_contract_path)
            self.assertEqual(loaded_sha256, execution_source_identity["contract"])

            original_verify_checkpoint = loaded.verify_processor_checkpoint

            original_load_canonical_update_pr = loaded.load_canonical_update_pr

            def load_candidate_control_helper(requested_root: pathlib.Path):
                self.assertEqual(pathlib.Path(requested_root).resolve(), repository.resolve())
                helper = original_load_canonical_update_pr(execution_root)
                helper_path = pathlib.Path(helper.__file__).resolve()
                helper_sha256 = hashlib.sha256(helper_path.read_bytes()).hexdigest()
                self.assertEqual(helper_path, execution_helper_path.resolve())
                self.assertEqual(helper_sha256, execution_source_identity["helper"])
                self.assertTrue(callable(getattr(helper, "validate_composition", None)))
                observations["execution_helper_loader_roots"].append(pathlib.Path(requested_root).resolve())
                observations["candidate_helper_loads"].append((helper_path, helper_sha256))
                return helper

            if exercise_wrong_producer:
                with self.assertRaises(AssertionError):
                    load_candidate_control_helper(repository.parent)
                with mock.patch.object(
                    loaded, "load_module", side_effect=FileNotFoundError("candidate CONTROL helper missing"),
                ):
                    with self.assertRaisesRegex(FileNotFoundError, "candidate CONTROL helper missing"):
                        load_candidate_control_helper(repository)
                with tempfile.TemporaryDirectory(prefix="completeness-wrong-control-helper-") as temporary:
                    wrong_helper_path = pathlib.Path(temporary) / "canonical_update_pr.py"
                    wrong_helper_path.write_text(
                        "def validate_composition(*args, **kwargs):\n    return None\n",
                        encoding="utf-8",
                    )
                    real_module_loader = loaded.load_module

                    def load_wrong_control_helper(_path: pathlib.Path, _name: str):
                        return real_module_loader(wrong_helper_path, "wrong_candidate_control_helper")

                    with mock.patch.object(loaded, "load_module", side_effect=load_wrong_control_helper):
                        with self.assertRaises(AssertionError):
                            load_candidate_control_helper(repository)

            loaded.load_canonical_update_pr = load_candidate_control_helper

            def verify_with_execution_schema(value: dict[str, object], schema_path: pathlib.Path):
                observations["execution_schema_requests"].append(schema_path.resolve())
                return original_verify_checkpoint(value, execution_checkpoint_schema_path)

            loaded.verify_processor_checkpoint = verify_with_execution_schema
            original_validate = loaded.validate_processor_bundle

            def validate_with_candidate_control(*args: object, **kwargs: object):
                actual_head = kwargs.get("producer_head_sha")
                observations["bundle_validation_heads"].append(actual_head)
                self.assertEqual(kwargs.get("root"), repository)
                self.assertEqual(args[2], expected_composition_schema)
                helper_path = pathlib.Path(args[3].__file__).resolve()
                observations["execution_helper_requests"].append(helper_path)
                self.assertEqual(helper_path, execution_helper_path.resolve())
                self.assertEqual(
                    hashlib.sha256(helper_path.read_bytes()).hexdigest(),
                    execution_source_identity["helper"],
                )
                result = original_validate(*args, **kwargs)
                observations["composer_identity_requests"].append((repository, str(actual_head)))
                producer_composer = loaded.processor_composer_source_identity(
                    repository, str(actual_head),
                )
                self.assertEqual(
                    loaded.authenticate_contract_diagnostic_encoding(
                        result["composition_receipt"], producer_composer,
                    ),
                    "projected",
                )
                if exercise_wrong_producer:
                    if wrong_producer is None:
                        raise AssertionError("wrong-producer case requires an isolated producer fixture")
                    wrong_root, wrong_head = wrong_producer
                    self.assertNotEqual(wrong_head, actual_head)
                    wrong_composer = loaded.processor_composer_source_identity(wrong_root, wrong_head)
                    self.assertIsNotNone(wrong_composer)
                    self.assertNotEqual(dict(wrong_composer), dict(producer_composer))
                    observations["composer_identity_requests"].append((wrong_root, wrong_head))
                    with self.assertRaisesRegex(
                        loaded.PromotionError,
                        "composition receipt composer identity differs from its authenticated producer source",
                    ) as rejected:
                        loaded.authenticate_contract_diagnostic_encoding(
                            result["composition_receipt"], wrong_composer,
                        )
                    observations["wrong_head_rejections"].append(str(rejected.exception))
                return result

            loaded.validate_processor_bundle = validate_with_candidate_control
            return loaded

        with (
            mock.patch.object(MODULE, "import_module", side_effect=import_with_candidate_processor_control),
            mock.patch.object(
                MODULE, "validate_processor_generation_inputs",
                side_effect=validate_generation_inputs_with_control,
            ),
            mock.patch.object(
                MODULE, "validate_promotion_stage", side_effect=validate_promotion_stage_with_control,
            ),
        ):
            yield observations

        self.assertTrue(observations["execution_contract_identities"])
        self.assertEqual(
            set(observations["execution_schema_requests"]),
            {requested_checkpoint_schema_path},
        )
        if observations["bundle_validation_heads"]:
            self.assertEqual(
                set(observations["execution_helper_requests"]), {execution_helper_path.resolve()},
            )
        else:
            self.assertEqual(observations["execution_helper_requests"], [])
        self.assertEqual(
            observations["execution_helper_loader_roots"],
            [repository.resolve()] * len(observations["candidate_helper_loads"]),
        )
        self.assertTrue(all(
            path == execution_helper_path.resolve() and sha == execution_source_identity["helper"]
            for path, sha in observations["candidate_helper_loads"]
        ))
        self.assertEqual(data_helper_path.read_bytes(), data_helper_before)
        self.assertEqual(hashlib.sha256(data_helper_path.read_bytes()).hexdigest(), data_helper_sha256)
        self.assertEqual(
            subprocess.check_output(
                ["git", "show", f"{selected_data_helper_head}:scripts/canonical_update_pr.py"], cwd=repository,
            ),
            data_helper_git_blob,
        )
        self.assertEqual(
            hashlib.sha256(execution_checkpoint_schema_path.read_bytes()).hexdigest(),
            execution_source_identity["checkpoint_schema"],
        )
        self.assertEqual(
            hashlib.sha256(execution_composition_schema_path.read_bytes()).hexdigest(),
            execution_source_identity["composition_schema"],
        )
        if exercise_wrong_producer:
            self.assertEqual(
                len(observations["wrong_head_rejections"]),
                len(observations["bundle_validation_heads"]),
            )
            self.assertTrue(observations["wrong_head_rejections"])
            for rejection in observations["wrong_head_rejections"]:
                self.assertIn("authenticated producer source", rejection)

    def _build_modern_processor_fixture(self, producer_head_sha: str) -> dict[str, object]:
        """Run the real local B processor/composer with a sealed offline A admission."""
        processor_tests = MODULE.import_module(
            "test_modern_report_processor_tests",
            ROOT / "tests/test_process_upstream_catalogue_candidate.py",
        )
        processor = processor_tests.MODULE
        fixture = processor_tests.UpstreamCatalogueProcessorTest("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        observed_at = "2026-10-04T13:17:00Z"
        now = processor.dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        fixture.now = observed_at
        baseline_path = ROOT / "data/data-go-kr.registry.json"
        baseline_raw = baseline_path.read_bytes()
        baseline_rows = json.loads(baseline_raw)
        new_row = fixture.real_link_row()
        new_identity = "758000000000001"
        new_url = f"https://www.data.go.kr/data/{new_identity}/openapi.do"
        new_row["id"] = new_identity
        new_row["source"]["url"] = new_url
        new_row["source"]["raw"]["api_id"] = new_identity
        new_row["source"]["raw"]["meta_url"] = new_url
        if any(str(row.get("id")) == new_identity for row in baseline_rows):
            raise AssertionError("offline B fixture identity unexpectedly exists in the canonical baseline")
        fixture.write_real_composer_inputs(
            baseline_rows, [*baseline_rows, new_row], observed_at, baseline_bytes=baseline_raw,
        )

        # Bind the synthetic local A bundle to the same file identities and
        # policy the source-stage validator reads. The observation is entirely
        # local; only the B processor and real composer execute below.
        source_policy_path = ROOT / "policy/source-refresh.json"
        source_policy = json.loads(source_policy_path.read_bytes())
        source_config_rows = [
            row for row in source_policy.get("sources", [])
            if isinstance(row, dict) and row.get("source_id") == "data_go_kr"
        ]
        if len(source_config_rows) != 1:
            raise AssertionError("offline source fixture requires one registered data_go_kr policy")
        source_config = source_config_rows[0]
        source_baseline = {
            "path": source_config["canonical_registry"],
            "bytes": len(baseline_raw),
            "sha256": MODULE.sha256_bytes(baseline_raw),
            "records": len(baseline_rows),
        }
        source_snapshot_path = ".datapan/ci/upstream-refresh/candidate.registry.json"
        source_evidence_path = ".datapan/ci/upstream-refresh/upstream-refresh-evidence.json"
        source_diff_path = ".datapan/ci/upstream-refresh/catalog-diff.json"
        evidence = json.loads(fixture.evidence_path.read_bytes())
        diff = json.loads(fixture.diff_path.read_bytes())
        candidate_raw = fixture.candidate_path.read_bytes()
        diff["old"] = source_config["canonical_registry"]
        diff["new"] = source_snapshot_path
        diff["generated_at"] = observed_at
        diff["provider"] = "data.go.kr"
        diff["limit"] = 0
        diff["truncated"] = False
        diff_raw = (json.dumps(diff, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        expected_status = "material_change"
        key_payload = {
            "source_id": "data_go_kr", "status": expected_status,
            "summary": diff["summary"], "error_class": None,
        }
        work_key_digest = MODULE.hashlib.sha256(
            json.dumps(key_payload, sort_keys=True).encode("utf-8"),
        ).hexdigest()[:16]
        work_key = f"upstream-refresh:data_go_kr:{work_key_digest}"
        evidence.update({
            "schema_version": "datapan.upstream-refresh-evidence.v1",
            "observed_at": observed_at,
            "source_id": "data_go_kr",
            "owner": source_config["owner"],
            "status": expected_status,
            "collection": {"attempted": True, "succeeded": True, "exit_code": 0, "error_class": None},
            "baseline": source_baseline,
            "snapshot": {
                "path": source_snapshot_path,
                "bytes": len(candidate_raw),
                "sha256": MODULE.sha256_bytes(candidate_raw),
                "records": len(baseline_rows) + 1,
            },
            "diff": {
                "path": source_diff_path,
                "sha256": MODULE.sha256_bytes(diff_raw),
                "summary": diff["summary"],
            },
            "review": {"action": "review_catalog_drift", "work_key": work_key},
            "publication": {
                "automatic": False, "release_allowed": False,
                "required_gates": source_config["publication"]["required_gates"],
            },
        })
        work_packet = {
            "schema_version": "datapan.upstream-refresh-work-packet.v1",
            "source_id": "data_go_kr", "observed_at": observed_at,
            "status": expected_status, "owner": source_config["owner"], "work_key": work_key,
            "action": "review_catalog_drift", "evidence": source_evidence_path,
            "snapshot": source_snapshot_path, "diff": source_diff_path,
            "automatic_publication": False,
        }
        MODULE.source_refresh_result_contract(
            source_config=source_config, evidence=evidence, diff=diff, work_packet=work_packet,
        )
        fixture.evidence_path.write_bytes(
            (json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        )
        fixture.diff_path.write_bytes(diff_raw)
        (fixture.root / "upstream-refresh-work-packet.json").write_bytes(
            (json.dumps(work_packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        )

        # The A fixture is local test input. Its envelope, archive, source
        # observation, and candidate are byte-bound by the real live-mode B
        # admission validator; no provider or external API is involved.
        members = {
            "candidate.registry.json": fixture.candidate_path.read_bytes(),
            "upstream-refresh-evidence.json": fixture.evidence_path.read_bytes(),
            "catalog-diff.json": fixture.diff_path.read_bytes(),
            "upstream-refresh-work-packet.json": (
                fixture.root / "upstream-refresh-work-packet.json"
            ).read_bytes(),
        }
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member, raw in members.items():
                archive.writestr(member, raw)
        collector_archive = archive_buffer.getvalue()
        collector_archive_path = fixture.root / "collector-artifact.zip"
        collector_archive_path.write_bytes(collector_archive)

        producer_run_id = "930099"
        collector_artifact_id = "930098"
        output_run_id = "930100"
        output_artifact_id = 930102
        collector_expiry = "2026-11-04T00:00:00Z"
        artifact_digest = MODULE.sha256_bytes(collector_archive)
        evidence_digest = MODULE.sha256_bytes(members["upstream-refresh-evidence.json"])
        admission = {
            "schema_version": "datapan.upstream-catalogue-admission-envelope.v1",
            "repository": "StatPan/datapan-registry",
            "producer_run_id": producer_run_id,
            "run_attempt": 1,
            "head_sha": producer_head_sha,
            "run_started_at": "2026-10-04T13:15:00Z",
            "run_completed_at": "2026-10-04T13:17:30Z",
            "observe_job_started_at": observed_at,
            "observe_job_completed_at": "2026-10-04T13:17:25Z",
            "artifact_id": collector_artifact_id,
            "artifact_name": f"upstream-catalog-refresh-{producer_run_id}",
            "artifact_expires_at": collector_expiry,
            "artifact_created_at": "2026-10-04T13:17:16Z",
            "artifact_digest_sha256": artifact_digest,
            "artifact_size_bytes": len(collector_archive),
            "archive_sha256": artifact_digest,
            "archive_size_bytes": len(collector_archive),
            "observed_at": observed_at,
            "refresh_evidence_sha256": evidence_digest,
            "event": "schedule",
        }
        admission_path = fixture.root / "collector-admission.json"
        admission_path.write_bytes(MODULE.canonical_json_bytes(admission))

        args = fixture.args(run_id=output_run_id, **{
            "--producer-run-id": producer_run_id,
            "--processor-run-id": f"{output_run_id}-1",
            "--processor-artifact-run-id": output_run_id,
            "--max-attempts": 24,
            "--retries-per-detail": 1,
        })
        args.execution_mode = "live"
        args.now = None
        args.producer_run_url = (
            f"https://github.com/StatPan/datapan-registry/actions/runs/{producer_run_id}"
        )
        args.fixture_composer = None
        args.allow_fixture_composer = False
        args.composer = ROOT / "scripts/compose-upstream-catalogue-candidate.py"
        args.producer_head_sha = producer_head_sha
        args.source_policy = ROOT / "policy/source-refresh.json"
        args.provider_index = ROOT / "data/provider-index.json"
        args.input_artifact_id = collector_artifact_id
        args.artifact_expires_at = collector_expiry
        args.output_artifact_expires_at = "2026-11-04T00:00:00Z"
        args.artifact_name = admission["artifact_name"]
        args.producer_run_attempt = 1
        args.producer_run_started_at = admission["run_started_at"]
        args.producer_run_completed_at = admission["run_completed_at"]
        args.producer_observe_job_started_at = admission["observe_job_started_at"]
        args.producer_observe_job_completed_at = admission["observe_job_completed_at"]
        args.producer_artifact_created_at = admission["artifact_created_at"]
        args.producer_artifact_digest_sha256 = artifact_digest
        args.producer_artifact_size_bytes = len(collector_archive)
        args.producer_event = "schedule"
        args.collector_admission_file = admission_path
        args.collector_archive = collector_archive_path

        page_bytes = (
            f'<a href="https://www.data.go.kr/guide/{new_identity}.pdf">API Guide</a>'
            '<a href="https://openapi.airport.co.kr/detail" '
            'onclick="fn_LinkApiRequest()">API</a>'
        ).encode("utf-8")
        observation = processor.DetailPageObservation(
            body=page_bytes.decode("utf-8"), page_bytes=page_bytes,
            page_url=new_url,
            effective_url=new_url,
            page_sha256=processor.sha256_bytes(page_bytes), observed_at=observed_at,
        )
        code, checkpoint = processor.process(
            args,
            fetcher=lambda *_args: observation,
            resolver_fetcher=lambda *_args: self.fail("offline B fixture must not call a resolver"),
            sleeper=lambda _delay: None,
            clock=lambda: now,
        )
        self.assertEqual(code, 0, checkpoint.get("outcome"))
        self.assertEqual(checkpoint.get("status"), "ready")
        self.assertEqual(checkpoint.get("source_scope"), "aggregate_supported_catalog")
        self.assertEqual(
            checkpoint.get("generation_inputs", {}).get("cache_source_profile"),
            processor.CACHE_SOURCE.CACHE_SOURCE_PROFILE,
        )
        self.assertEqual(
            [row["path"] for row in checkpoint.get("output_digests", [])],
            list(MODULE.import_module(
                "test_modern_report_promotion_contract",
                ROOT / "scripts/run-canonical-update-promotion.py",
            ).REQUIRED_PROCESSOR_FILES),
        )
        return {
            "artifact_id": output_artifact_id,
            "checkpoint": checkpoint,
            "collector_archive": collector_archive,
            "collector_artifact_id": collector_artifact_id,
            "collector_run_id": producer_run_id,
            "collector_head_sha": producer_head_sha,
            "collector_evidence": evidence,
            "collector_diff": diff,
            "collector_work_packet": work_packet,
            "fixture": fixture,
            "head_sha": producer_head_sha,
            "output_run_id": output_run_id,
            "source_observed_at": observed_at,
        }

    def _install_modern_source_fixture(
        self, *, input_root: pathlib.Path, index: dict[str, object],
        modern_processor: dict[str, object],
    ) -> dict[str, object]:
        """Bind the local A collector run and archive to the real B admission."""
        source_rows = [
            row for row in index["inputs"]
            if row.get("scope_id") == "data-go-kr.api-operations"
            and row.get("subject", {}).get("stage") == "source"
        ]
        roles = {row["role"]: row for row in source_rows}
        expected_roles = {
            "pipeline_run", "pipeline_jobs", "pipeline_artifact_metadata",
            "pipeline_artifact_archive",
        }
        if set(roles) != expected_roles:
            raise AssertionError("modern A fixture requires the exact four source workflow inputs")

        run_id = str(modern_processor["collector_run_id"])
        head_sha = str(modern_processor["collector_head_sha"])
        artifact_id = int(modern_processor["collector_artifact_id"])
        observed_at = str(modern_processor["source_observed_at"])
        run_started = "2026-10-04T13:15:00Z"
        job_started = "2026-10-04T13:15:05Z"
        job_completed = "2026-10-04T13:17:28Z"
        run_completed = "2026-10-04T13:17:30Z"
        artifact_created = "2026-10-04T13:17:16Z"
        artifact_expires = "2026-11-04T00:00:00Z"
        archive_raw = bytes(modern_processor["collector_archive"])
        contract = MODULE.PIPELINE_WORKFLOWS["source"]
        run = {
            "id": int(run_id), "run_attempt": 1,
            "name": contract["workflow_name"], "workflow_id": contract["workflow_id"],
            "path": contract["workflow_path"],
            "repository": {"id": 1278568329, "full_name": "StatPan/datapan-registry"},
            "head_repository": {"id": 1278568329, "full_name": "StatPan/datapan-registry"},
            "head_branch": "main", "head_sha": head_sha, "event": "schedule",
            "status": "completed", "conclusion": "success",
            "created_at": run_started, "run_started_at": run_started,
            "run_completed_at": run_completed,
            "html_url": f"https://github.com/StatPan/datapan-registry/actions/runs/{run_id}",
        }
        jobs = {
            "total_count": 1,
            "jobs": [{
                "id": int(run_id) + 1, "run_id": int(run_id), "run_attempt": 1,
                "name": contract["required_job"], "head_sha": head_sha, "head_branch": "main",
                "status": "completed", "conclusion": "success",
                "started_at": job_started, "completed_at": job_completed,
                "steps": [{
                    "name": "Observe upstream catalogue without publishing",
                    "status": "completed", "conclusion": "success", "number": 8,
                    "started_at": observed_at, "completed_at": "2026-10-04T13:17:25Z",
                }],
            }],
        }
        artifact_name = f"upstream-catalog-refresh-{run_id}"
        metadata = {
            "total_count": 1,
            "artifacts": [{
                "id": artifact_id, "name": artifact_name, "expired": False,
                "created_at": artifact_created, "updated_at": job_completed,
                "expires_at": artifact_expires, "size_in_bytes": len(archive_raw),
                "digest": f"sha256:{MODULE.sha256_bytes(archive_raw)}",
                "workflow_run": {
                    "id": int(run_id), "repository_id": 1278568329,
                    "head_repository_id": 1278568329, "head_branch": "main", "head_sha": head_sha,
                },
            }],
        }
        values: dict[str, object] = {
            "pipeline_run": run,
            "pipeline_jobs": jobs,
            "pipeline_artifact_metadata": metadata,
            "pipeline_artifact_archive": archive_raw,
        }
        filenames = {
            "pipeline_run": "modern-source-run.json",
            "pipeline_jobs": "modern-source-jobs.json",
            "pipeline_artifact_metadata": "modern-source-artifacts.json",
            "pipeline_artifact_archive": "modern-source-artifact.zip",
        }
        for row in source_rows:
            role = row["role"]
            producer = row["producer"]
            producer.update({
                "run_id": run_id, "attempt": 1, "revision": head_sha,
                "event": "schedule",
            })
            row["observed_at"] = observed_at if role == "pipeline_artifact_archive" else run_completed
            value = values[role]
            raw = value if isinstance(value, bytes) else (
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode()
            self._write_rebound_evidence(input_root, row, filenames[role], raw)
        return {
            "archive_sha256": MODULE.sha256_bytes(archive_raw),
            "artifact_id": str(artifact_id),
            "head_sha": head_sha,
            "observed_at": observed_at,
            "run_id": run_id,
        }

    def _install_modern_processor_fixture(
        self, *, input_root: pathlib.Path, index: dict[str, object],
        modern_processor: dict[str, object],
    ) -> dict[str, object]:
        """Bind actual B producer output into the local API/state/artifact fixture."""
        fixture = modern_processor["fixture"]
        if not hasattr(fixture, "output_dir") or not hasattr(fixture, "state_dir"):
            raise AssertionError("modern B fixture lost its isolated processor directories")
        promotion = MODULE.import_module(
            "test_modern_report_state_promotion",
            ROOT / "scripts/run-canonical-update-promotion.py",
        )
        processor = MODULE.import_module(
            "test_modern_report_state_processor",
            ROOT / "scripts/process-upstream-catalogue-candidate.py",
        )
        checkpoint = copy.deepcopy(modern_processor["checkpoint"])
        output_artifact = checkpoint["output_artifact"]
        output_artifact["artifact_id"] = str(modern_processor["artifact_id"])
        checkpoint = processor.seal_checkpoint(checkpoint)
        generation_id = checkpoint["generation_id"]
        output_dir = pathlib.Path(fixture.output_dir)
        state_dir = pathlib.Path(fixture.state_dir)

        required_members = tuple(promotion.REQUIRED_PROCESSOR_FILES)
        members = {name: (output_dir / name).read_bytes() for name in required_members}
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in (*required_members, promotion.RETAINED_PROCESSOR_CHECKPOINT_MEMBER):
                path = output_dir / name
                if not path.is_file():
                    raise AssertionError(f"modern producer omitted its output member: {name}")
                archive.writestr(name, path.read_bytes())
        artifact_archive = archive_buffer.getvalue()

        state_root_path = state_dir / "state-root.json"
        # The test processor owns source/index/generation state; the registered
        # root descriptor is immutable Registry state copied from the captured
        # native API response.
        base_rows = [
            row for row in index["inputs"]
            if row.get("scope_id") == "data-go-kr.api-operations"
            and row.get("subject", {}).get("stage") == "processor"
        ]
        base_by_role = {row["role"]: row for row in base_rows}
        root_item = base_by_role["processor_state_root_api"]
        root_api = json.loads((ROOT / root_item["path"]).read_bytes())
        state_root_raw = MODULE.base64.b64decode(root_api["content"])
        generation_path = (
            state_dir / "sources/data_go_kr/generations" / f"{generation_id}.json"
        )
        generation_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_raw = (
            json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        generation_path.write_bytes(checkpoint_raw)
        state_index_path = state_dir / "sources/data_go_kr/index.json"
        state_index = json.loads(state_index_path.read_bytes())
        entries = [
            row for row in state_index.get("generations", [])
            if row.get("generation_id") == generation_id
        ]
        if len(entries) != 1:
            raise AssertionError("modern B processor state index does not select its generated checkpoint")
        index_raw = (
            json.dumps(state_index, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()

        state_repo = input_root / "modern-processor-state-git"
        state_repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(state_repo)], check=True)
        subprocess.run(["git", "config", "user.name", "Offline B fixture"], cwd=state_repo, check=True)
        subprocess.run(["git", "config", "user.email", "offline-b@example.invalid"], cwd=state_repo, check=True)
        state_files = {
            ".datapan/upstream-catalogue-state/state-root.json": state_root_raw,
            ".datapan/upstream-catalogue-state/sources/data_go_kr/index.json": index_raw,
            f".datapan/upstream-catalogue-state/sources/data_go_kr/generations/{generation_id}.json": checkpoint_raw,
        }
        for relative, raw in state_files.items():
            path = state_repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        subprocess.run(["git", "add", "--all"], cwd=state_repo, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "offline modern B state snapshot"], cwd=state_repo, check=True)
        state_commit_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=state_repo, text=True,
        ).strip()
        state_tree_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=state_repo, text=True,
        ).strip()
        tree_output = subprocess.check_output(
            ["git", "ls-tree", "-r", "-t", "-z", "HEAD"], cwd=state_repo,
        )
        tree_rows: list[dict[str, object]] = []
        for record in tree_output.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split(" ")
            relative = raw_path.decode("utf-8")
            row: dict[str, object] = {
                "path": relative, "mode": mode, "type": kind, "sha": oid,
                "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/{'trees' if kind == 'tree' else 'blobs'}/{oid}",
            }
            if kind == "blob":
                row["size"] = int(subprocess.check_output(
                    ["git", "cat-file", "-s", oid], cwd=state_repo, text=True,
                ).strip())
            tree_rows.append(row)

        api_base = "https://api.github.com/repos/StatPan/datapan-registry"
        state_commit_api = {
            "sha": state_commit_sha,
            "url": f"{api_base}/git/commits/{state_commit_sha}",
            "message": "offline modern B state snapshot",
            "tree": {"sha": state_tree_sha, "url": f"{api_base}/git/trees/{state_tree_sha}"},
            "parents": [],
        }
        state_ref_api = {
            "ref": "refs/heads/automation/upstream-catalogue-state",
            "object": {
                "sha": state_commit_sha, "type": "commit",
                "url": f"{api_base}/git/commits/{state_commit_sha}",
            },
        }
        state_tree_api = {
            "sha": state_tree_sha,
            "url": f"{api_base}/git/trees/{state_tree_sha}",
            "truncated": False,
            "tree": tree_rows,
        }

        def contents_api(relative: str, raw: bytes) -> dict[str, object]:
            blob_sha = MODULE.git_blob_sha1(raw)
            return {
                "type": "file", "encoding": "base64", "path": relative,
                "sha": blob_sha, "size": len(raw),
                "content": MODULE.base64.b64encode(raw).decode("ascii"),
                "url": f"{api_base}/contents/{relative}?ref=automation/upstream-catalogue-state",
            }

        state_root_api = contents_api(
            ".datapan/upstream-catalogue-state/state-root.json", state_root_raw,
        )
        state_index_api = contents_api(
            ".datapan/upstream-catalogue-state/sources/data_go_kr/index.json", index_raw,
        )
        generation_api = contents_api(
            f".datapan/upstream-catalogue-state/sources/data_go_kr/generations/{generation_id}.json",
            checkpoint_raw,
        )

        run_id = str(modern_processor["output_run_id"])
        head_sha = str(modern_processor["head_sha"])
        run_started = "2026-10-04T13:18:00Z"
        job_started = "2026-10-04T13:18:05Z"
        artifact_created = "2026-10-04T13:18:35Z"
        job_completed = "2026-10-04T13:18:40Z"
        run_completed = "2026-10-04T13:18:45Z"
        artifact_expiry = output_artifact["expires_at"]
        metadata = {
            "total_count": 1,
            "artifacts": [{
                "id": int(modern_processor["artifact_id"]),
                "name": output_artifact["name"],
                "expired": False,
                "created_at": artifact_created,
                "updated_at": job_completed,
                "expires_at": artifact_expiry,
                "size_in_bytes": len(artifact_archive),
                "digest": f"sha256:{MODULE.sha256_bytes(artifact_archive)}",
                "workflow_run": {
                    "id": int(run_id), "repository_id": 1278568329,
                    "head_repository_id": 1278568329,
                    "head_branch": "main", "head_sha": head_sha,
                },
            }],
        }
        run = {
            "id": int(run_id), "run_attempt": 1,
            "name": MODULE.PIPELINE_WORKFLOWS["processor"]["workflow_name"],
            "workflow_id": MODULE.PIPELINE_WORKFLOWS["processor"]["workflow_id"],
            "path": MODULE.PIPELINE_WORKFLOWS["processor"]["workflow_path"],
            "repository": {"id": 1278568329, "full_name": "StatPan/datapan-registry"},
            "head_repository": {"id": 1278568329, "full_name": "StatPan/datapan-registry"},
            "head_branch": "main", "head_sha": head_sha,
            "event": "schedule", "status": "completed", "conclusion": "success",
            "created_at": run_started, "run_started_at": run_started,
            "run_completed_at": run_completed,
            "html_url": f"https://github.com/StatPan/datapan-registry/actions/runs/{run_id}",
        }
        jobs = {
            "total_count": 1,
            "jobs": [{
                "id": int(run_id) + 1, "run_id": int(run_id), "run_attempt": 1,
                "name": "Process", "head_sha": head_sha, "head_branch": "main",
                "status": "completed", "conclusion": "success",
                "started_at": job_started, "completed_at": job_completed,
            }],
        }
        values: dict[str, object] = {
            "pipeline_artifact_archive": artifact_archive,
            "pipeline_artifact_metadata": metadata,
            "pipeline_jobs": jobs,
            "pipeline_run": run,
            "processor_generation_api": generation_api,
            "processor_index_api": state_index_api,
            "processor_state_ref": state_ref_api,
            "processor_state_root_api": state_root_api,
            "processor_state_commit": state_commit_api,
            "processor_state_tree": state_tree_api,
        }
        filenames = {
            "pipeline_artifact_archive": "modern-processor-artifact.zip",
            "pipeline_artifact_metadata": "modern-processor-artifacts.json",
            "pipeline_jobs": "modern-processor-jobs.json",
            "pipeline_run": "modern-processor-run.json",
            "processor_generation_api": "modern-processor-generation-api.json",
            "processor_index_api": "modern-processor-index-api.json",
            "processor_state_ref": "modern-processor-state-ref.json",
            "processor_state_root_api": "modern-processor-state-root-api.json",
            "processor_state_commit": "modern-processor-state-commit.json",
            "processor_state_tree": "modern-processor-state-tree.json",
        }
        for row in base_rows:
            role = row["role"]
            producer = row["producer"]
            producer.update({
                "run_id": run_id, "attempt": 1, "revision": head_sha,
                "event": "schedule",
            })
            subject = row.get("subject")
            if isinstance(subject, dict) and subject.get("generation_id") is not None:
                subject["generation_id"] = generation_id
            if role == "pipeline_artifact_metadata" or role == "pipeline_run" or role == "pipeline_jobs":
                raw = (
                    json.dumps(values[role], ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                ).encode()
            elif role == "pipeline_artifact_archive":
                raw = values[role]
            else:
                raw = (
                    json.dumps(values[role], ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                ).encode()
            self._write_rebound_evidence(input_root, row, filenames[role], raw)

        return {
            "artifact_archive": artifact_archive,
            "candidate_bytes": members["composed-candidate.registry.json"],
            "checkpoint": checkpoint,
            "generation_id": generation_id,
            "head_sha": head_sha,
            "state_commit_sha": state_commit_sha,
            "state_tree_sha": state_tree_sha,
        }

    def _install_modern_health_fixture(
        self, *, input_root: pathlib.Path, index: dict[str, object],
        modern_processor: dict[str, object], modern_state: dict[str, object],
        source_fixture: dict[str, object], promotion_run: dict[str, object],
        promotion_commit_sha: str, journal_raw: bytes, candidate_head: str,
        candidate_manifest_sha: str,
    ) -> dict[str, object]:
        """Reseal Health with the exact synthetic A/B/C receipts and state transition."""
        health_rows = [
            row for row in index["inputs"]
            if row.get("scope_id") == "data-go-kr.api-operations"
            and row.get("subject", {}).get("stage") == "health"
        ]
        roles = {row["role"]: row for row in health_rows}
        expected_roles = {
            "pipeline_run", "pipeline_jobs", "pipeline_artifact_metadata", "pipeline_artifact_archive",
            "health_receipt_blob_api", "health_state_blob_api", "health_state_ref",
            "health_state_tree", "health_state_commit",
        }
        if set(roles) != expected_roles:
            raise AssertionError("modern Health fixture requires the exact workflow and state evidence roles")

        run_id = "930104"
        attempt = 1
        head_sha = str(modern_processor["head_sha"])
        evaluated_at = "2026-10-04T13:21:50Z"
        run_started = "2026-10-04T13:21:00Z"
        job_started = "2026-10-04T13:21:05Z"
        job_completed = evaluated_at
        artifact_created = "2026-10-04T13:21:45Z"
        artifact_expires = "2026-11-04T00:00:00Z"
        contract = MODULE.PIPELINE_WORKFLOWS["health"]
        run = {
            "id": int(run_id), "run_attempt": attempt,
            "name": contract["workflow_name"], "workflow_id": contract["workflow_id"],
            "path": contract["workflow_path"],
            "repository": {"id": 1278568329, "full_name": "StatPan/datapan-registry"},
            "head_repository": {"id": 1278568329, "full_name": "StatPan/datapan-registry"},
            "head_branch": "main", "head_sha": head_sha, "event": "workflow_run",
            "status": "completed", "conclusion": "success",
            "created_at": run_started, "run_started_at": run_started,
            "run_completed_at": job_completed,
            "html_url": f"https://github.com/StatPan/datapan-registry/actions/runs/{run_id}",
        }
        jobs = {
            "total_count": 1,
            "jobs": [{
                "id": int(run_id) + 1, "run_id": int(run_id), "run_attempt": attempt,
                "name": contract["required_job"], "head_sha": head_sha, "head_branch": "main",
                "status": "completed", "conclusion": "success",
                "started_at": job_started, "completed_at": job_completed,
            }],
        }
        archive_item = roles["pipeline_artifact_archive"]
        with zipfile.ZipFile(io.BytesIO((ROOT / archive_item["path"]).read_bytes())) as source_archive:
            members = {entry.filename: source_archive.read(entry.filename) for entry in source_archive.infolist()}
        receipt = json.loads(members["health-receipt.json"])
        pre_state = json.loads(members["health-state/state.json"])
        source_rows = [
            row for row in receipt.get("sources", [])
            if isinstance(row, dict) and row.get("source_id") == "data_go_kr"
        ]
        if len(source_rows) != 1:
            raise AssertionError("retained Health receipt does not uniquely summarize data_go_kr")
        source = source_rows[0]
        observed_at = str(source_fixture["observed_at"])
        observation_age = int((
            MODULE.parse_time(evaluated_at, "fixture Health time")
            - MODULE.parse_time(observed_at, "fixture A time")
        ).total_seconds())
        with zipfile.ZipFile(io.BytesIO(bytes(modern_processor["collector_archive"]))) as collector:
            refresh_evidence_raw = collector.read("upstream-refresh-evidence.json")
        source["observation"] = {
            "age_seconds": observation_age,
            "collection_status": "success",
            "execution_mode": "live",
            "max_age_seconds": 691200,
            "observed_at": observed_at,
            "producer_run_id": source_fixture["run_id"],
            "refresh_evidence_sha256": MODULE.sha256_bytes(refresh_evidence_raw),
            "state": "fresh",
        }
        canonical = source.setdefault("canonical", {})
        candidate_bytes = bytes(modern_state["candidate_bytes"])
        canonical["main"] = {
            "manifest_sha256": candidate_manifest_sha,
            "registry_bytes": len(candidate_bytes),
            "registry_path": "data/data-go-kr.registry.json",
            "registry_sha256": MODULE.sha256_bytes(candidate_bytes),
            "revision": candidate_head,
        }
        canonical.pop("already_canonical_candidate", None)
        promotion_execution = canonical.setdefault("promotion_execution", {})
        execution = {
            "conclusion": "success", "created_at": promotion_run["run_started_at"],
            "event": promotion_run["event"], "head_branch": "main",
            "head_repository": "StatPan/datapan-registry", "head_sha": promotion_run["head_sha"],
            "path": MODULE.PIPELINE_WORKFLOWS["promotion"]["workflow_path"],
            "repository": "StatPan/datapan-registry", "run_attempt": 1,
            "run_id": str(promotion_run["id"]),
            "run_started_at": promotion_run["run_started_at"],
            "status": "completed", "updated_at": promotion_run["run_completed_at"],
            "workflow_id": MODULE.PIPELINE_WORKFLOWS["promotion"]["workflow_id"],
        }
        promotion_execution["execution_failure"] = None
        promotion_execution["latest_execution_run"] = copy.deepcopy(execution)
        promotion_execution["latest_successful_execution_run"] = copy.deepcopy(execution)
        canonical["promotion_status"] = "merged-candidate-reconciled"
        processor_row = source.setdefault("processor", {})
        checkpoint = modern_state["checkpoint"]
        if not isinstance(checkpoint, dict):
            raise AssertionError("modern B fixture omitted its sealed checkpoint")
        output_artifact = checkpoint["output_artifact"]
        processor_row.update({
            "generation_id": checkpoint["generation_id"],
            "state": checkpoint["status"],
            "checkpoint_sha256": checkpoint["checkpoint_sha256"],
            "candidate_sha256": checkpoint["generation_inputs"]["candidate_sha256"],
            "outcome": copy.deepcopy(checkpoint["outcome"]),
            "output_artifact": {
                "run_id": str(modern_processor["output_run_id"]),
                "artifact_id": str(modern_processor["artifact_id"]),
                "bundle_manifest_sha256": output_artifact["bundle_manifest_sha256"],
            },
            "output_digests": copy.deepcopy(checkpoint["output_digests"]),
            "last_observation": copy.deepcopy(checkpoint.get("last_observation")),
        })
        receipt["evaluated_at"] = evaluated_at
        receipt["health_workflow"] = {
            "revision": head_sha, "run_attempt": attempt, "run_id": run_id,
        }
        persister = MODULE.import_health_persister_at_revision(ROOT, head_sha)
        receipt = persister.seal(receipt, "receipt_sha256")
        checker_result = json.loads(members["checker-result.json"])
        checker_result["receipt_sha256"] = receipt["receipt_sha256"]
        receipt_raw = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        pre_state_raw = (json.dumps(pre_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        checker_raw = (json.dumps(checker_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        members.update({
            "as-of.txt": (evaluated_at + "\n").encode("ascii"),
            "health-receipt.json": receipt_raw,
            "checker-result.json": checker_raw,
            "health-state/state.json": pre_state_raw,
            "processor-ref": (
                f"{modern_state['state_commit_sha']}\trefs/heads/automation/upstream-catalogue-state\n"
            ).encode("ascii"),
            "promotion/reports/canonical-update-promotion-receipt.json": journal_raw,
            "promotion-ref": (
                f"{promotion_commit_sha}\trefs/heads/automation/canonical-update-state\n"
            ).encode("ascii"),
        })
        post_state = MODULE.expected_health_post_state(
            root=ROOT, workflow_head=head_sha, pre_state=pre_state, receipt=receipt,
        )
        post_state_raw = (json.dumps(post_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        receipt_name = persister.receipt_file_name(receipt)
        tree_api = json.loads((ROOT / roles["health_state_tree"]["path"]).read_bytes())
        old_receipt_api = json.loads((ROOT / roles["health_receipt_blob_api"]["path"]).read_bytes())
        old_receipt_paths = [
            row["path"] for row in tree_api["tree"]
            if row.get("type") == "blob" and row.get("sha") == old_receipt_api.get("sha")
        ]
        if len(old_receipt_paths) != 1:
            raise AssertionError("Health receipt tree path is not uniquely identified")
        old_receipt_path = old_receipt_paths[0]
        receipt_path = f"health/upstream-catalogue/receipts/{receipt_name}"
        state_path = "health/upstream-catalogue/state.json"
        state_blob = MODULE.git_blob_sha1(post_state_raw)
        receipt_blob = MODULE.git_blob_sha1(receipt_raw)
        tree_rows = [row for row in tree_api["tree"] if row.get("path") != old_receipt_path]
        replacements = {
            state_path: (state_blob, len(post_state_raw)),
            receipt_path: (receipt_blob, len(receipt_raw)),
        }
        for row in tree_rows:
            if row.get("path") in replacements:
                row["sha"], row["size"] = replacements[row["path"]]
                row["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{row['sha']}"
        if receipt_path not in {row.get("path") for row in tree_rows}:
            tree_rows.append({
                "path": receipt_path, "mode": "100644", "type": "blob",
                "sha": receipt_blob, "size": len(receipt_raw),
                "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{receipt_blob}",
            })
        tree_api["tree"] = tree_rows
        tree_sha = self._git_tree_sha_from_recursive_rows(tree_rows)
        tree_api.update({
            "sha": tree_sha,
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}",
        })

        def contents_api(role: str, path: str, raw: bytes) -> dict[str, object]:
            api = json.loads((ROOT / roles[role]["path"]).read_bytes())
            blob_sha = MODULE.git_blob_sha1(raw)
            api.update({
                "path": path, "sha": blob_sha, "size": len(raw),
                "content": MODULE.base64.b64encode(raw).decode("ascii"), "encoding": "base64",
                "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{blob_sha}",
            })
            return api

        state_api = contents_api("health_state_blob_api", state_path, post_state_raw)
        receipt_api = contents_api("health_receipt_blob_api", receipt_path, receipt_raw)
        parent_sha = members["health-ref"].decode("ascii").strip().split("\t")[0]
        commit_api = json.loads((ROOT / roles["health_state_commit"]["path"]).read_bytes())
        commit_api["tree"] = {
            **commit_api["tree"], "sha": tree_sha,
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}",
        }
        commit_raw = (
            f"tree {tree_sha}\nparent {parent_sha}\n"
            "author Completeness test <completeness-test@example.invalid> 1791118800 +0000\n"
            "committer Completeness test <completeness-test@example.invalid> 1791118800 +0000\n\n"
            "Synthetic modern A-B-C Health transition\n"
        ).encode()
        commit_sha = MODULE.hashlib.sha1(f"commit {len(commit_raw)}\0".encode() + commit_raw).hexdigest()
        commit_api.update({
            "sha": commit_sha,
            "message": "Synthetic modern A-B-C Health transition",
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/commits/{commit_sha}",
            "parents": [{
                "sha": parent_sha,
                "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/commits/{parent_sha}",
                "html_url": f"https://github.com/StatPan/datapan-registry/commit/{parent_sha}",
            }],
        })
        ref_api = json.loads((ROOT / roles["health_state_ref"]["path"]).read_bytes())
        ref_api["object"].update({"sha": commit_sha, "url": commit_api["url"]})
        members["health-ref"] = (
            f"{parent_sha}\trefs/heads/automation/upstream-catalogue-health-state\n"
        ).encode("ascii")

        archive_output = io.BytesIO()
        with zipfile.ZipFile(archive_output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member_name, raw in members.items():
                archive.writestr(member_name, raw)
        archive_raw = archive_output.getvalue()
        metadata = {
            "total_count": 1,
            "artifacts": [{
                "id": 930105, "name": f"upstream-catalogue-health-{run_id}",
                "expired": False, "created_at": artifact_created,
                "updated_at": job_completed, "expires_at": artifact_expires,
                "size_in_bytes": len(archive_raw),
                "digest": f"sha256:{MODULE.sha256_bytes(archive_raw)}",
                "workflow_run": {
                    "id": int(run_id), "repository_id": 1278568329,
                    "head_repository_id": 1278568329, "head_branch": "main", "head_sha": head_sha,
                },
            }],
        }
        values: dict[str, object] = {
            "pipeline_run": run, "pipeline_jobs": jobs,
            "pipeline_artifact_metadata": metadata, "pipeline_artifact_archive": archive_raw,
            "health_state_ref": ref_api, "health_state_commit": commit_api,
            "health_state_tree": tree_api, "health_state_blob_api": state_api,
            "health_receipt_blob_api": receipt_api,
        }
        filenames = {
            "pipeline_run": "modern-health-run.json",
            "pipeline_jobs": "modern-health-jobs.json",
            "pipeline_artifact_metadata": "modern-health-artifacts.json",
            "pipeline_artifact_archive": "modern-health-artifact.zip",
            "health_state_ref": "modern-health-state-ref.json",
            "health_state_commit": "modern-health-state-commit.json",
            "health_state_tree": "modern-health-state-tree.json",
            "health_state_blob_api": "modern-health-state-blob.json",
            "health_receipt_blob_api": "modern-health-receipt-blob.json",
        }
        for row in health_rows:
            role = row["role"]
            row["producer"].update({
                "run_id": run_id, "attempt": attempt, "revision": head_sha,
                "event": "workflow_run",
            })
            row["observed_at"] = evaluated_at
            value = values[role]
            raw = value if isinstance(value, bytes) else (
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode()
            self._write_rebound_evidence(input_root, row, filenames[role], raw)
        return {
            "archive_sha256": MODULE.sha256_bytes(archive_raw),
            "commit_sha": commit_sha,
            "receipt_sha256": receipt["receipt_sha256"],
            "run_id": run_id,
            "state_sha256": post_state["state_sha256"],
        }

    def _build_synthetic_matching_b_c_packet(
        self, input_root: pathlib.Path, *, candidate_generation_override: str | None = None,
        candidate_source_id_override: str | None = None,
        candidate_manifest_override: str | None = None,
        candidate_status: str = "merged",
        include_legacy_publication: bool = True,
        processor_history_variant: str | None = None,
        only_source_processor: bool = False,
        modern_processor: dict[str, object] | None = None,
        repository_source_root: pathlib.Path | None = None,
        reuse_existing_borrowed_main: bool = False,
    ) -> tuple[pathlib.Path, pathlib.Path, str]:
        """Rebind a local, producer-shaped B→merged-C→Health packet without external writes."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(CHECKED_OUT_ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index = json.loads((CHECKED_OUT_ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())

        source_fixture: dict[str, object] | None = None
        modern_state: dict[str, object] | None = None
        if modern_processor is not None:
            source_fixture = self._install_modern_source_fixture(
                input_root=input_root, index=index, modern_processor=modern_processor,
            )
            modern_state = self._install_modern_processor_fixture(
                input_root=input_root, index=index, modern_processor=modern_processor,
            )

        repository = input_root / "repository"
        trusted_main, trusted_tree = self._clone_disposable_at_verified_main(
            repository_source_root or ROOT, repository,
            transport="borrowed-source" if modern_processor is not None else "bounded-main",
            reuse_existing_borrowed_main=reuse_existing_borrowed_main,
        )
        if processor_history_variant is not None:
            self._rebind_synthetic_processor_history(
                index=index, input_root=input_root, repository=repository,
                variant=processor_history_variant,
            )
        pinned_repository_inputs: dict[str, tuple[int, str]] = {}
        for item in index["inputs"]:
            if item.get("root") != "repository":
                continue
            relative = item["path"]
            pin = (item["bytes"], item["sha256"])
            previous = pinned_repository_inputs.setdefault(relative, pin)
            if previous != pin:
                raise AssertionError(f"input index gives conflicting repository pins for {relative}")
        needed_paths = {
            MODULE.SCOPE_REGISTRY_PATH, MODULE.POLICY_PATH,
            MODULE.ROLLUP_SCHEMA_PATH, MODULE.ROLLUP_V2_SCHEMA_PATH,
            "scripts/upstream_catalogue_cache_source.py",
            "scripts/completeness_publication_evidence.py",
            "schemas/datapan.completeness-proof-scopes.v1.schema.json",
            "schemas/datapan.completeness-proof-identities.v1.schema.json",
            "schemas/datapan.completeness-proof-inputs.v1.schema.json",
            "schemas/datapan.completeness-proof-policy.v1.schema.json",
            *COMPLETENESS_VALIDATOR_CODE_DEPENDENCIES,
            *COMPLETENESS_VALIDATOR_SCHEMA_DEPENDENCIES,
            *(item["path"] for item in index["inputs"] if item.get("root") == "repository"),
        }
        current_manifest = json.loads((CHECKED_OUT_ROOT / "manifest.json").read_bytes())
        manifest_pins: dict[str, tuple[int, str]] = {}
        for artifact in current_manifest.get("artifacts", []):
            relative = artifact.get("path")
            pin = (artifact.get("bytes"), artifact.get("sha256"))
            if not isinstance(relative, str) or not isinstance(pin[0], int) or not isinstance(pin[1], str):
                continue
            if relative in manifest_pins:
                raise AssertionError(f"current release manifest repeats an artifact path: {relative}")
            manifest_pins[relative] = pin
        fixture_source_revision = subprocess.check_output(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=CHECKED_OUT_ROOT, text=True,
        ).strip()
        for relative in sorted(needed_paths):
            source_path = CHECKED_OUT_ROOT / relative
            destination_path = repository / relative
            if not source_path.is_file():
                raise AssertionError(f"required source for pinned completeness fixture is missing: {relative}")
            pin = pinned_repository_inputs.get(relative)
            manifest_pin = manifest_pins.get(relative)
            self._materialize_pinned_completeness_fixture_source(
                source_root=CHECKED_OUT_ROOT,
                source_path=source_path,
                destination_path=destination_path,
                relative=relative,
                fixture_source_revision=fixture_source_revision,
                indexed_pin=pin,
                manifest_pin=manifest_pin,
            )
        c_run_head = (
            str(modern_processor["head_sha"])
            if modern_processor is not None
            else "01148419bef5d7f212e92ab96d0ba1c3da6c298c"
        )
        if modern_processor is None:
            self.assertEqual(
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", c_run_head, trusted_main],
                    cwd=repository, check=False,
                ).returncode,
                0,
            )
            tree_base = trusted_main
        else:
            self.assertEqual(
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", trusted_main, c_run_head],
                    cwd=repository, check=False,
                ).returncode,
                0,
            )
            tree_base = c_run_head
        tree_sha = subprocess.check_output(
            ["git", "rev-parse", f"{tree_base}^{{tree}}"], cwd=repository, text=True,
        ).strip()
        if modern_processor is None:
            self.assertEqual(tree_sha, trusted_tree)
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Synthetic completeness fixture",
            "GIT_AUTHOR_EMAIL": "completeness-test@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic completeness fixture",
            "GIT_COMMITTER_EMAIL": "completeness-test@example.invalid",
            "GIT_AUTHOR_DATE": "2026-10-04T13:19:00Z",
            "GIT_COMMITTER_DATE": "2026-10-04T13:19:00Z",
        }
        local_registry = repository / "data/data-go-kr.registry.json"
        local_registry.unlink()
        _materialize_exact_fixture_file(
            ROOT / "data/data-go-kr.registry.json", local_registry,
        )
        by_role = {
            item["role"]: item for item in index["inputs"]
            if item.get("scope_id") == operation_id and item.get("subject", {}).get("stage") == "processor"
        }
        generation_item = by_role["processor_generation_api"]
        generation_root = ROOT if generation_item["root"] == "repository" else input_root
        checkpoint_api = json.loads((generation_root / generation_item["path"]).read_bytes())
        checkpoint = json.loads(MODULE.base64.b64decode(checkpoint_api["content"]))
        candidate_output = next(
            row for row in checkpoint["output_digests"]
            if row.get("path") == "composed-candidate.registry.json"
        )
        composition_output = next(
            row for row in checkpoint["output_digests"]
            if row.get("path") == "composition-receipt.json"
        )
        self.assertNotEqual(
            checkpoint["generation_inputs"]["baseline_sha256"], candidate_output["sha256"],
            "the synthetic C row must bind B's composed output, not pretend B was a no-op",
        )

        if modern_processor is None:
            candidate_head = subprocess.check_output(
                ["git", "commit-tree", tree_sha, "-p", c_run_head], cwd=repository,
                input="Synthetic changed-B candidate with the authenticated Registry subject\n",
                text=True, env=environment,
            ).strip()
        else:
            candidate_bytes = bytes(modern_state["candidate_bytes"])
            self.assertEqual(candidate_output["sha256"], MODULE.sha256_bytes(candidate_bytes))
            self.assertEqual(candidate_output["bytes"], len(candidate_bytes))

            local_registry_mode = stat.S_IMODE(local_registry.lstat().st_mode)
            local_registry.unlink()
            with local_registry.open("xb") as stream:
                stream.write(candidate_bytes)
            os.chmod(local_registry, local_registry_mode)

            operation_manifest_path = repository / "reports/data-go-kr/operation-manifest.json"
            operation_manifest_path.parent.mkdir(parents=True, exist_ok=True)
            operation_generator = MODULE.import_module(
                "modern_candidate_operation_manifest_generator",
                repository / "scripts/generate-data-go-kr-operation-manifest.py",
            )
            prior_registry_path = operation_generator.REGISTRY
            operation_generator.REGISTRY = local_registry
            try:
                candidate_rows = json.loads(candidate_bytes)
                candidate_operation_manifest = operation_generator.build(candidate_rows)
                operation_manifest_raw = operation_generator.render(candidate_operation_manifest)
            finally:
                operation_generator.REGISTRY = prior_registry_path
            operation_manifest_path.write_bytes(operation_manifest_raw)

            expectation_path = repository / "policy/data-go-kr-operation-denominator-expectation.json"
            expectation = json.loads(expectation_path.read_bytes())
            expectation["source_snapshot"] = copy.deepcopy(candidate_operation_manifest["source_snapshot"])
            expectation["expected_summary"] = copy.deepcopy(candidate_operation_manifest["summary"])
            expectation_raw = MODULE.canonical_json_bytes(expectation)
            expectation_path.write_bytes(expectation_raw)

            manifest_raw = subprocess.check_output(
                ["git", "show", f"{c_run_head}:manifest.json"], cwd=repository,
            )
            manifest = json.loads(manifest_raw)
            generated_artifacts = {
                "data/data-go-kr.registry.json": (candidate_bytes, "payload"),
                "reports/data-go-kr/operation-manifest.json": (operation_manifest_raw, "file"),
                "policy/data-go-kr-operation-denominator-expectation.json": (expectation_raw, "file"),
            }
            for artifact_path, (raw, _kind) in generated_artifacts.items():
                artifacts = [
                    item for item in manifest.get("artifacts", [])
                    if item.get("path") == artifact_path
                ]
                if len(artifacts) != 1:
                    raise AssertionError(f"modern synthetic C merge requires one {artifact_path} manifest row")
                artifacts[0]["bytes"] = len(raw)
                artifacts[0]["sha256"] = MODULE.sha256_bytes(raw)
            manifest_next_raw = (
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode()
            pointer_raw = (
                "version https://git-lfs.github.com/spec/v1\n"
                f"oid sha256:{MODULE.sha256_bytes(candidate_bytes)}\n"
                f"size {len(candidate_bytes)}\n"
            ).encode("ascii")
            tree_updates = {
                "manifest.json": manifest_next_raw,
                "data/data-go-kr.registry.json": pointer_raw,
                "reports/data-go-kr/operation-manifest.json": operation_manifest_raw,
                "policy/data-go-kr-operation-denominator-expectation.json": expectation_raw,
            }
            subprocess.run(
                ["git", "read-tree", c_run_head], cwd=repository, check=True,
            )
            for relative, raw in tree_updates.items():
                blob = subprocess.check_output(
                    ["git", "hash-object", "-w", "--stdin"], cwd=repository, input=raw,
                ).decode("ascii").strip()
                subprocess.run(
                    ["git", "update-index", "--add", "--cacheinfo", "100644", blob, relative],
                    cwd=repository, check=True,
                )
            candidate_tree_sha = subprocess.check_output(
                ["git", "write-tree"], cwd=repository, text=True,
            ).strip()
            candidate_head = subprocess.check_output(
                ["git", "commit-tree", candidate_tree_sha, "-p", c_run_head], cwd=repository,
                input="Synthetic modern changed-B candidate with the authenticated Registry subject\n",
                text=True, env=environment,
            ).strip()
            # Evaluate with the exact current Source at the synthetic C main
            # commit. The commit is a child of the authenticated B head and its
            # tree includes the modern validator plus the candidate bindings.
            local_registry.unlink()
            subprocess.run(
                ["git", "checkout", "--quiet", "--detach", candidate_head],
                cwd=repository,
                check=True,
                env={**environment, "GIT_LFS_SKIP_SMUDGE": "1"},
            )
            # `git read-tree c_run_head` above prepared the synthetic commit's
            # index without updating its worktree. A checkout from that index
            # can therefore leave unchanged source paths at the clone's older
            # trusted-main bytes. Force the checked-out worktree to the exact
            # candidate tree before evaluating current-source compatibility.
            subprocess.run(
                ["git", "reset", "--hard", candidate_head],
                cwd=repository,
                check=True,
                env={**environment, "GIT_LFS_SKIP_SMUDGE": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(
                subprocess.check_output(["git", "rev-parse", "HEAD^{commit}"], cwd=repository, text=True).strip(),
                candidate_head,
            )
            for relative in (
                ".github/workflows/upstream-catalogue-process.yml",
                "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
                "scripts/process-upstream-catalogue-candidate.py",
            ):
                expected_blob = subprocess.check_output(
                    ["git", "rev-parse", f"{c_run_head}:{relative}"],
                    cwd=repository, text=True,
                ).strip()
                candidate_blob = subprocess.check_output(
                    ["git", "rev-parse", f"{candidate_head}:{relative}"],
                    cwd=repository, text=True,
                ).strip()
                self.assertEqual(candidate_blob, expected_blob, relative)
                expected_source = subprocess.check_output(
                    ["git", "show", f"{c_run_head}:{relative}"], cwd=repository,
                )
                self.assertEqual((repository / relative).read_bytes(), expected_source, relative)
            local_registry.unlink()
            with local_registry.open("xb") as stream:
                stream.write(candidate_bytes)
            os.chmod(local_registry, 0o664)
            (repository / "manifest.json").write_bytes(manifest_next_raw)
            operation_manifest_path.write_bytes(operation_manifest_raw)
            expectation_path.write_bytes(expectation_raw)
            catalog_items = [
                item for item in index["inputs"]
                if item.get("role") == "source_catalog_snapshot"
                and item.get("scope_id") == "data-go-kr.api-catalog"
            ]
            if len(catalog_items) != 1:
                raise AssertionError("modern synthetic C fixture requires one canonical catalog inventory row")
            catalog_items[0]["bytes"] = len(candidate_bytes)
            catalog_items[0]["sha256"] = MODULE.sha256_bytes(candidate_bytes)
            operation_items = [
                item for item in index["inputs"]
                if item.get("role") == "local_operation_manifest"
                and item.get("scope_id") == "data-go-kr.api-operations"
            ]
            if len(operation_items) != 1:
                raise AssertionError("modern synthetic C fixture requires one canonical operation-manifest inventory row")
            operation_items[0]["bytes"] = len(operation_manifest_raw)
            operation_items[0]["sha256"] = MODULE.sha256_bytes(operation_manifest_raw)
        if modern_processor is None:
            # The historical C fixture's selected source head owns the data
            # helper identity.  The disposable repository was initially
            # populated with current validator dependencies, so restore this
            # one historical helper from the candidate tree before invoking
            # the historical caller.  Keep the execution-root helper (the
            # current validator) unchanged.
            data_helper_relative = "scripts/canonical_update_pr.py"
            candidate_data_helper = subprocess.check_output(
                ["git", "show", f"{candidate_head}:{data_helper_relative}"],
                cwd=repository,
            )
            trusted_data_helper = subprocess.check_output(
                ["git", "show", f"{trusted_main}:{data_helper_relative}"],
                cwd=repository,
            )
            expected_historical_helper_sha256 = (
                "25d8519b9a873315adb3abdb53c1d7812e9443715f7991bdc5677ebbf98bb475"
            )
            self.assertEqual(candidate_data_helper, trusted_data_helper)
            self.assertEqual(
                MODULE.sha256_bytes(candidate_data_helper),
                expected_historical_helper_sha256,
            )
            data_helper_path = repository / data_helper_relative
            data_helper_mode = stat.S_IMODE(data_helper_path.lstat().st_mode)
            data_helper_path.unlink()
            with data_helper_path.open("xb") as stream:
                stream.write(candidate_data_helper)
            os.chmod(data_helper_path, data_helper_mode)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", candidate_head],
            cwd=repository, check=True,
        )
        current_artifact, candidate_manifest_sha = MODULE.source_lfs_binding(repository, candidate_head)
        self.assertEqual(candidate_output["sha256"], current_artifact["sha256"])
        self.assertEqual(candidate_output["bytes"], current_artifact["bytes"])
        catalog_item = next(item for item in index["inputs"] if item.get("role") == "source_catalog_snapshot")
        catalog_path = repository / catalog_item["path"]
        local_catalog_identity = (catalog_path.stat().st_size, MODULE.sha256_file(catalog_path))
        manifest_catalog_identity = (current_artifact["bytes"], current_artifact["sha256"])
        if local_catalog_identity != manifest_catalog_identity or local_catalog_identity != (
            catalog_item["bytes"], catalog_item["sha256"],
        ):
            raise AssertionError("synthetic trusted main does not bind the exact current catalog input")
        catalog_item.setdefault("subject", {})["source_revision"] = candidate_head

        def rebind(item: dict[str, object], filename: str, raw: bytes) -> None:
            self._write_rebound_evidence(input_root, item, filename, raw)

        def pretty(value: object) -> bytes:
            return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()

        c_rows = [
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id and item.get("subject", {}).get("stage") == "promotion"
        ]
        c_roles = {item["role"]: item for item in c_rows}
        original_journal_api = json.loads((ROOT / c_roles["promotion_journal_blob_api"]["path"]).read_bytes())
        journal_raw = MODULE.base64.b64decode(original_journal_api["content"])
        journal = json.loads(journal_raw)
        candidate_row = copy.deepcopy(journal["records"][-1])
        candidate = candidate_row["candidate"]
        candidate_generation = candidate_generation_override or checkpoint["generation_id"]
        if candidate_status not in {"merged", "publication-pending", "published", "read-back-confirmed"}:
            raise AssertionError(f"unsupported synthetic C lifecycle status: {candidate_status}")
        candidate.update({
            "base_sha": c_run_head,
            "head_sha": candidate_head,
            "manifest_sha256": candidate_manifest_override or candidate_manifest_sha,
            "registry_path": "data/data-go-kr.registry.json",
            "registry_bytes": candidate_output["bytes"],
            "registry_sha256": candidate_output["sha256"],
            "composition_receipt_sha256": composition_output["sha256"],
            "generation_id": candidate_generation,
        })
        if candidate_source_id_override is not None:
            candidate["source_id"] = candidate_source_id_override
        candidate["payload_readback"] = {
            "status": "verified", "provider": "synthetic-local-git-tree",
            "repository": "StatPan/datapan-registry", "remote": "origin",
            "source_sha": candidate_head, "manifest_sha256": candidate_manifest_sha,
            "path": candidate_output["path"], "bytes": candidate_output["bytes"],
            "sha256": candidate_output["sha256"], "lfs_oid": candidate_output["sha256"],
            "upload_attempted": False, "readback": "synthetic-producer-shaped-tree",
            "observed_at": "2026-10-04T13:19:30Z",
        }
        identity = MODULE.import_module("completeness_journal_contract", ROOT / "scripts/canonical_update_pr.py")
        owner_id = identity.owner_id("StatPan/datapan-registry", "data_go_kr", "aggregate_supported_catalog")
        candidate_row.update({
            "status": "merged", "acknowledgements": [],
            "pr": {
                "number": 999722,
                "url": "https://github.com/StatPan/datapan-registry/pull/999722",
                "state": "merged", "merge_commit_sha": candidate_head,
            },
            "ownership": {
                "owner_id": owner_id, "branch": "automation/canonical-update/synthetic-b-c",
                "expected_head_sha": candidate_head, "issue_number": 722,
                "body": (
                    f"<!-- {owner_id} generation={candidate_generation} -->\n\n"
                    "Synthetic producer-shaped offline B-to-C merge fixture; no external PR, provider, "
                    "publication, or source observation was created.\n"
                ),
            },
        })
        candidate_row["ownership"]["body_sha256"] = MODULE.sha256_bytes(
            candidate_row["ownership"]["body"].encode()
        )
        if candidate_status != "merged":
            # Exercise the production transition reducer against a clearly
            # synthetic local journal. These journal acknowledgements are not
            # independent workflow-run evidence and cannot satisfy publisher,
            # read-back, or #631 claim requirements.
            ack_times = {
                "publication-pending": "2026-10-04T13:19:40Z",
                "published": "2026-10-04T13:19:50Z",
                "read-back-confirmed": "2026-10-04T13:20:00Z",
            }
            transitions = {
                "publication-pending": ["publication-pending"],
                "published": ["publication-pending", "published"],
                "read-back-confirmed": ["publication-pending", "published", "read-back-confirmed"],
            }[candidate_status]
            publication_revision = "a" * 40
            pointer_revision = "b" * 40
            for ordinal, next_status in enumerate(transitions, start=1):
                read_back = next_status == "read-back-confirmed"
                published = next_status in {"published", "read-back-confirmed"}
                run_id = 99972200000 + ordinal
                acknowledgement = {
                    "status": next_status,
                    "observed_at": ack_times[next_status],
                    "source_sha": candidate_head,
                    "manifest_sha256": candidate["manifest_sha256"],
                    "artifact_identity": {
                        "path": candidate["registry_path"],
                        "bytes": candidate["registry_bytes"],
                        "sha256": candidate["registry_sha256"],
                    },
                    "read_back_verified": read_back,
                    "read_back_sha256": candidate["registry_sha256"] if read_back else None,
                    "read_back_bytes": candidate["registry_bytes"] if read_back else None,
                    "publication_revision": publication_revision if published else None,
                    "publication_pointer_revision": pointer_revision if published else None,
                    "run_id": run_id,
                    "run_attempt": 1,
                    "run_url": f"https://github.com/StatPan/datapan-registry/actions/runs/{run_id}/attempts/1",
                    "evidence_reference": f"synthetic local transition #{ordinal}; no external workflow exists",
                }
                candidate_row = identity.record_acknowledgement(candidate_row, acknowledgement)
            self.assertEqual(candidate_row["status"], candidate_status)
        candidate_row.pop("refresh_from", None)
        candidate_row.pop("refresh_target_main_sha", None)
        candidate_row.pop("superseded_by", None)
        candidate_row.pop("ci", None)
        journal["records"].append(candidate_row)
        journal["updated_at"] = "2026-10-04T13:21:00Z"
        journal_raw = pretty(journal)

        original_tree = json.loads((ROOT / c_roles["promotion_state_tree"]["path"]).read_bytes())
        tree_rows = copy.deepcopy(original_tree["tree"])
        journal_rows = [
            row for row in tree_rows
            if row.get("path") == "reports/canonical-update-promotion-receipt.json"
        ]
        self.assertEqual(len(journal_rows), 1)
        journal_blob_sha = MODULE.git_blob_sha1(journal_raw)
        journal_rows[0]["sha"] = journal_blob_sha
        journal_rows[0]["size"] = len(journal_raw)
        journal_rows[0]["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{journal_blob_sha}"
        directories = sorted(
            (row["path"] for row in tree_rows if row.get("type") == "tree"),
            key=lambda value: value.count("/"), reverse=True,
        )
        for directory in directories:
            children = [
                row for row in tree_rows
                if pathlib.PurePosixPath(row["path"]).parent.as_posix() == directory
            ]
            body = bytearray()
            children.sort(key=lambda row: (
                pathlib.PurePosixPath(row["path"]).name
                + ("/" if row.get("type") == "tree" else "")
            ).encode())
            for child in children:
                mode = str(child["mode"]).lstrip("0") or "0"
                body.extend(
                    mode.encode("ascii") + b" "
                    + pathlib.PurePosixPath(child["path"]).name.encode("utf-8")
                    + b"\0" + bytes.fromhex(child["sha"])
                )
            digest = MODULE.hashlib.sha1(f"tree {len(body)}\0".encode("ascii") + body).hexdigest()
            directory_row = next(row for row in tree_rows if row["path"] == directory)
            directory_row["sha"] = digest
            directory_row["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{digest}"
        c_tree_sha = self._git_tree_sha_from_recursive_rows(tree_rows)
        c_tree_api = copy.deepcopy(original_tree)
        c_tree_api.update({
            "sha": c_tree_sha,
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{c_tree_sha}",
            "tree": tree_rows,
        })
        root_children = []
        for row in tree_rows:
            if pathlib.PurePosixPath(row["path"]).parent.as_posix() == ".":
                name = pathlib.PurePosixPath(row["path"]).name
                sort_name = name + ("/" if row.get("type") == "tree" else "")
                root_children.append((sort_name.encode(), name, row))
        root_children.sort(key=lambda value: value[0])
        root_tree_body = bytearray()
        for _sort_name, name, row in root_children:
            mode = str(row["mode"]).lstrip("0") or "0"
            root_tree_body.extend(
                mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(row["sha"])
            )
        written_tree_sha = subprocess.check_output(
            ["git", "hash-object", "-w", "-t", "tree", "--stdin"], cwd=repository,
            input=bytes(root_tree_body),
        ).decode("ascii").strip()
        self.assertEqual(written_tree_sha, c_tree_sha)

        old_commit = json.loads((ROOT / c_roles["promotion_state_commit"]["path"]).read_bytes())
        author = "Completeness fixture <completeness-test@example.invalid>"
        state_commit_content = (
            f"tree {c_tree_sha}\nparent {old_commit['sha']}\n"
            f"author {author} 1791110520 +0000\ncommitter {author} 1791110520 +0000\n\n"
            "Synthetic producer-shaped C journal transition\n"
        ).encode()
        state_commit_sha = subprocess.check_output(
            ["git", "hash-object", "-w", "-t", "commit", "--stdin"], cwd=repository,
            input=state_commit_content,
        ).decode("ascii").strip()
        c_commit_api = copy.deepcopy(old_commit)
        c_commit_api.update({
            "sha": state_commit_sha,
            "tree": {**old_commit["tree"], "sha": c_tree_sha,
                     "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{c_tree_sha}"},
            "parents": [{"sha": old_commit["sha"], "url": old_commit["url"], "html_url": old_commit["html_url"]}],
            "message": "Synthetic producer-shaped C journal transition",
        })
        c_ref_api = json.loads((ROOT / c_roles["promotion_state_ref"]["path"]).read_bytes())
        c_ref_api["object"]["sha"] = state_commit_sha
        c_ref_api["object"]["url"] = c_commit_api["url"]
        journal_api = copy.deepcopy(original_journal_api)
        journal_api.update({
            "path": "reports/canonical-update-promotion-receipt.json",
            "sha": journal_blob_sha, "size": len(journal_raw),
            "content": MODULE.base64.b64encode(journal_raw).decode("ascii"),
            "encoding": "base64",
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{journal_blob_sha}",
        })
        for role, filename, value in (
            ("promotion_journal_blob_api", "synthetic-promotion-journal-blob.json", journal_api),
            ("promotion_state_ref", "synthetic-promotion-state-ref.json", c_ref_api),
            ("promotion_state_commit", "synthetic-promotion-state-commit.json", c_commit_api),
            ("promotion_state_tree", "synthetic-promotion-state-tree.json", c_tree_api),
        ):
            rebind(c_roles[role], filename, pretty(value))

        promotion_contract = MODULE.PIPELINE_WORKFLOWS["promotion"]
        if modern_processor is not None:
            promotion_run = {
                "id": 930103, "run_attempt": 1,
                "name": promotion_contract["workflow_name"],
                "workflow_id": promotion_contract["workflow_id"],
                "path": promotion_contract["workflow_path"],
                "repository": {"id": 1278568329, "full_name": "StatPan/datapan-registry"},
                "head_repository": {"id": 1278568329, "full_name": "StatPan/datapan-registry"},
                "head_branch": "main", "head_sha": c_run_head, "event": "workflow_run",
                "status": "completed", "conclusion": "success",
                "created_at": "2026-10-04T13:19:00Z",
                "run_started_at": "2026-10-04T13:19:00Z",
                "run_completed_at": "2026-10-04T13:20:00Z",
                "html_url": "https://github.com/StatPan/datapan-registry/actions/runs/930103",
            }
            promotion_jobs = {
                "total_count": 1,
                "jobs": [{
                    "id": 930104, "run_id": 930103, "run_attempt": 1,
                    "name": promotion_contract["required_job"],
                    "head_sha": c_run_head, "head_branch": "main",
                    "status": "completed", "conclusion": "success",
                    "started_at": "2026-10-04T13:19:05Z",
                    "completed_at": "2026-10-04T13:19:55Z",
                }],
            }
            for row in c_rows:
                row["producer"].update({
                    "run_id": "930103", "attempt": 1, "revision": c_run_head,
                    "event": "workflow_run",
                })
                row["observed_at"] = promotion_run["run_completed_at"]
            self._write_rebound_evidence(
                input_root, c_roles["pipeline_run"], "modern-promotion-run.json", pretty(promotion_run),
            )
            self._write_rebound_evidence(
                input_root, c_roles["pipeline_jobs"], "modern-promotion-jobs.json", pretty(promotion_jobs),
            )
        else:
            promotion_run = json.loads((ROOT / c_roles["pipeline_run"]["path"]).read_bytes())

        log_item = c_roles["pipeline_log_archive"]
        run_url = f"https://github.com/StatPan/datapan-registry/actions/runs/{promotion_run['id']}/attempts/{promotion_run['run_attempt']}"
        log_raw = MODULE.gzip.compress(
            (json.dumps({"status": "promotion-prs-reconciled", "run_url": run_url}, sort_keys=True) + "\n").encode(),
            mtime=0,
        )
        rebind(log_item, "synthetic-promotion-reconcile.log.gz", log_raw)

        if modern_processor is not None:
            if source_fixture is None or modern_state is None:
                raise AssertionError("modern A/B fixture installation is incomplete")
            self._install_modern_health_fixture(
                input_root=input_root, index=index,
                modern_processor=modern_processor, modern_state=modern_state,
                source_fixture=source_fixture, promotion_run=promotion_run,
                promotion_commit_sha=state_commit_sha, journal_raw=journal_raw,
                candidate_head=candidate_head, candidate_manifest_sha=candidate_manifest_sha,
            )
        else:
            health_roles = {
                item["role"]: item for item in index["inputs"]
                if item.get("scope_id") == operation_id and item.get("subject", {}).get("stage") == "health"
            }
            health_archive_item = health_roles["pipeline_artifact_archive"]
            health_archive_raw = (ROOT / health_archive_item["path"]).read_bytes()
            with zipfile.ZipFile(io.BytesIO(health_archive_raw)) as archive:
                health_members = {name: archive.read(name) for name in archive.namelist()}
            health_members["promotion/reports/canonical-update-promotion-receipt.json"] = journal_raw
            health_members["promotion-ref"] = (
                f"{state_commit_sha}\trefs/heads/automation/canonical-update-state\n"
            ).encode("ascii")
            health_output = io.BytesIO()
            with zipfile.ZipFile(health_output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for member_name, raw in health_members.items():
                    archive.writestr(member_name, raw)
            new_health_archive = health_output.getvalue()
            rebind(health_archive_item, "synthetic-health-chain.zip", new_health_archive)
            health_metadata = json.loads((ROOT / health_roles["pipeline_artifact_metadata"]["path"]).read_bytes())
            health_metadata["artifacts"][0]["size_in_bytes"] = len(new_health_archive)
            health_metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(new_health_archive)}"
            rebind(
                health_roles["pipeline_artifact_metadata"], "synthetic-health-artifacts.json", pretty(health_metadata),
            )

        if not include_legacy_publication:
            index["inputs"] = [
                item for item in index["inputs"]
                if item.get("subject", {}).get("stage") not in {"publisher", "acknowledgement"}
                and not item["role"].startswith(("publication_", "acknowledgement_"))
                and item["role"] != "consumer_readback"
            ]
        if only_source_processor:
            index["inputs"] = [
                item for item in index["inputs"]
                if item.get("scope_id") != operation_id
                or item.get("subject", {}).get("stage") in {None, "source", "processor"}
                and not item["role"].startswith(("publication_", "acknowledgement_"))
                and item["role"] != "consumer_readback"
            ]

        index_path = input_root / "input-index.json"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        return repository, index_path, checkpoint["generation_id"]

    def test_verify_release_fetches_bounded_main_ref_for_retained_provenance(self) -> None:
        environment = self._closed_git_environment()
        workflow = (ROOT / ".github/workflows/verify-release.yml").read_text(encoding="utf-8")
        fetch_step = "git fetch --no-tags --depth=64 origin +refs/heads/main:refs/remotes/origin/main"
        self.assertIn(fetch_step, workflow)
        self.assertLess(
            workflow.index("Fetch bounded trusted main history for retained evidence checks"),
            workflow.index("Validate registry policy artifacts"),
        )

        with tempfile.TemporaryDirectory(prefix="completeness-main-history-fetch-") as name:
            root = pathlib.Path(name)
            remote = root / "origin.git"
            seed = root / "seed"
            checkout = root / "ci-checkout"
            subprocess.run(
                ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(remote)],
                check=True,
                env=environment,
            )
            subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(seed)], check=True, env=environment)
            for repository in (seed,):
                subprocess.run(["git", "config", "user.name", "Completeness rollup test"], cwd=repository, check=True, env=environment)
                subprocess.run(["git", "config", "user.email", "completeness-test@example.invalid"], cwd=repository, check=True, env=environment)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=seed, check=True, env=environment)
            source_sha = ""
            for index in range(16):
                (seed / "history.txt").write_text(f"commit {index}\n", encoding="utf-8")
                subprocess.run(["git", "add", "history.txt"], cwd=seed, check=True, env=environment)
                subprocess.run(["git", "commit", "--quiet", "-m", f"history {index}"], cwd=seed, check=True, env=environment)
                if index == 0:
                    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=seed, text=True, env=environment).strip()
            main_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=seed, text=True, env=environment).strip()
            subprocess.run(["git", "push", "--quiet", "origin", "main"], cwd=seed, check=True, env=environment)
            subprocess.run(["git", "checkout", "--quiet", "-b", "fixture-pr"], cwd=seed, check=True, env=environment)
            (seed / "pull-request.txt").write_text("PR source\n", encoding="utf-8")
            subprocess.run(["git", "add", "pull-request.txt"], cwd=seed, check=True, env=environment)
            subprocess.run(["git", "commit", "--quiet", "-m", "PR source"], cwd=seed, check=True, env=environment)
            subprocess.run(["git", "push", "--quiet", "origin", "HEAD:refs/heads/fixture-pr"], cwd=seed, check=True, env=environment)

            subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(checkout)], check=True, env=environment)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=checkout, check=True, env=environment)
            subprocess.run([
                "git", "fetch", "--no-tags", "--depth=1", "origin",
                "+refs/heads/fixture-pr:refs/remotes/origin/fixture-pr",
            ], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
            subprocess.run(["git", "checkout", "--quiet", "--detach", "refs/remotes/origin/fixture-pr"], cwd=checkout, check=True, env=environment)
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run(
                    ["git", "rev-parse", "refs/remotes/origin/main"], cwd=checkout,
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
                )
            subprocess.run(fetch_step.split(), cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
            fetched_main = subprocess.check_output(
                ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"], cwd=checkout, text=True,
                env=environment,
            ).strip()
            self.assertEqual(fetched_main, main_sha)
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "--is-shallow-repository"], cwd=checkout, text=True, env=environment,
                ).strip(),
                "true",
            )
            pr_head = subprocess.check_output(
                ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=checkout, text=True, env=environment,
            ).strip()
            self.assertNotEqual(pr_head, fetched_main)
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", source_sha, "refs/remotes/origin/main"],
                cwd=checkout, check=True, env=environment,
            )

            disposable = root / "disposable-main-checkout"
            cloned_main, cloned_tree = self._clone_disposable_at_verified_main(checkout, disposable)
            self.assertEqual(cloned_main, main_sha)
            self.assertEqual(
                cloned_tree,
                subprocess.check_output(
                    ["git", "rev-parse", "--verify", f"{main_sha}^{{tree}}"], cwd=checkout, text=True,
                env=environment,
                ).strip(),
            )
            self.assertEqual(
                subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=disposable, text=True, env=environment).strip(),
                main_sha,
            )
            with self.assertRaisesRegex(ValueError, "expected trusted main does not match source origin/main"):
                self._clone_disposable_at_verified_main(
                    checkout, root / "wrong-main-checkout", expected_main=source_sha,
                )
            self.assertFalse((root / "wrong-main-checkout").exists())

    def test_borrowed_origin_shallow_boundary_guards_fail_before_transport(self) -> None:
        """Invalid borrowed-origin boundaries stop before refs or transport."""
        environment = self._closed_git_environment()

        def git(
            args: list[str], *, cwd: pathlib.Path | None = None, check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                ["git", *args], cwd=cwd, check=False, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment,
            )
            if check and result.returncode:
                raise AssertionError(f"git {' '.join(args)} failed: {result.stderr[-2000:]}")
            return result

        def output(args: list[str], *, cwd: pathlib.Path) -> str:
            return git(args, cwd=cwd).stdout.strip()

        def assert_no_transport(directory: pathlib.Path, *, origin_exists: bool) -> None:
            origin = directory / "shallow-source-origin.git"
            self.assertEqual(origin.exists(), origin_exists)
            self.assertFalse((directory / "shallow-pr-checkout").exists())
            if origin_exists:
                refs = git(["--git-dir", str(origin), "show-ref"], cwd=directory, check=False)
                self.assertEqual(refs.returncode, 1, refs.stderr)
                self.assertEqual(refs.stdout, "")

        with tempfile.TemporaryDirectory(prefix="completeness-borrowed-origin-guards-") as name:
            root = pathlib.Path(name)
            remote = root / "origin.git"
            seed = root / "seed"
            source = root / "source"
            git(["init", "--quiet", "--bare", "--initial-branch=main", str(remote)])
            git(["init", "--quiet", "--initial-branch=main", str(seed)])
            for key, value in (
                ("user.name", "Completeness borrowed-origin guard test"),
                ("user.email", "completeness-borrowed-origin@example.invalid"),
                ("commit.gpgsign", "false"),
            ):
                git(["config", key, value], cwd=seed)
            git(["remote", "add", "origin", str(remote)], cwd=seed)

            (seed / "main.txt").write_text("trusted main\n", encoding="utf-8")
            git(["add", "main.txt"], cwd=seed)
            git(["commit", "--quiet", "-m", "trusted main"], cwd=seed)
            for index in range(1, 81):
                with (seed / "main.txt").open("a", encoding="utf-8") as handle:
                    handle.write(f"trusted main {index}\n")
                git(["commit", "--quiet", "-am", f"trusted main {index}"], cwd=seed)
            trusted_main = output(["rev-parse", "HEAD^{commit}"], cwd=seed)
            git(["push", "--quiet", "origin", "main"], cwd=seed)

            git(["switch", "--quiet", "-c", "fixture-pr"], cwd=seed)
            (seed / "fixture-pr.txt").write_text("fixture PR\n", encoding="utf-8")
            git(["add", "fixture-pr.txt"], cwd=seed)
            git(["commit", "--quiet", "-m", "fixture PR"], cwd=seed)
            git(["push", "--quiet", "origin", "fixture-pr"], cwd=seed)

            git([
                "clone", "--quiet", "--no-tags", "--depth=32", "--branch", "fixture-pr",
                f"file://{remote}", str(source),
            ])
            git([
                "fetch", "--quiet", "--no-tags", "--depth=32", "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ], cwd=source)
            source_shallow_path = pathlib.Path(output(
                ["rev-parse", "--path-format=absolute", "--git-path", "shallow"], cwd=source,
            ))
            source_shallow_bytes = source_shallow_path.read_bytes()
            self.assertTrue(source_shallow_bytes)
            self.assertEqual(output(["rev-parse", "--is-shallow-repository"], cwd=source), "true")
            source_head = output(["rev-parse", "HEAD^{commit}"], cwd=source)
            source_main = output(["rev-parse", "refs/remotes/origin/main^{commit}"], cwd=source)
            source_tree = output(["rev-parse", f"{source_main}^{{tree}}"], cwd=source)
            source_refs = output(["show-ref"], cwd=source)
            self.assertEqual(source_main, trusted_main)
            self.assertNotEqual(source_head, source_main)
            self.assertEqual(
                git(["merge-base", "--is-ancestor", source_main, source_head], cwd=source, check=False).returncode,
                0,
                "the valid shallow fixture must preserve its trusted-main ancestry",
            )

            def assert_source_unchanged() -> None:
                self.assertEqual(source_shallow_path.read_bytes(), source_shallow_bytes)
                self.assertEqual(output(["rev-parse", "HEAD^{commit}"], cwd=source), source_head)
                self.assertEqual(output(["rev-parse", "refs/remotes/origin/main^{commit}"], cwd=source), source_main)
                self.assertEqual(output(["rev-parse", f"{source_main}^{{tree}}"], cwd=source), source_tree)
                self.assertEqual(output(["show-ref"], cwd=source), source_refs)

            unrelated_seed = root / "unrelated-seed"
            unrelated_source = root / "unrelated-pr-checkout"
            git(["init", "--quiet", "--initial-branch=unrelated", str(unrelated_seed)])
            for key, value in (
                ("user.name", "Completeness unrelated-history test"),
                ("user.email", "completeness-unrelated@example.invalid"),
                ("commit.gpgsign", "false"),
            ):
                git(["config", key, value], cwd=unrelated_seed)
            git(["remote", "add", "origin", str(remote)], cwd=unrelated_seed)
            (unrelated_seed / "unrelated.txt").write_text("unrelated history\n", encoding="utf-8")
            git(["add", "unrelated.txt"], cwd=unrelated_seed)
            git(["commit", "--quiet", "-m", "unrelated history"], cwd=unrelated_seed)
            git(["push", "--quiet", "origin", "HEAD:refs/heads/unrelated-pr"], cwd=unrelated_seed)
            git([
                "clone", "--quiet", "--no-tags", "--depth=1", "--branch", "unrelated-pr",
                f"file://{remote}", str(unrelated_source),
            ])
            git([
                "fetch", "--quiet", "--no-tags", "--depth=32", "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ], cwd=unrelated_source)
            unrelated_directory = root / "nonancestor-source-rejection"
            with self.assertRaisesRegex(ValueError, "local PR fixture is not descended from the pinned trusted main"):
                self._make_genuine_shallow_local_source(
                    unrelated_directory, source_root=unrelated_source,
                )
            assert_no_transport(unrelated_directory, origin_exists=False)

            malformed_directory = root / "malformed-source"
            try:
                source_shallow_path.write_bytes(b"not-a-git-object-id\n")
                with self.assertRaises(subprocess.CalledProcessError):
                    self._read_native_shallow_boundaries(source)
                with self.assertRaises(subprocess.CalledProcessError):
                    self._make_genuine_shallow_local_source(malformed_directory, source_root=source)
                assert_no_transport(malformed_directory, origin_exists=False)
            finally:
                source_shallow_path.write_bytes(source_shallow_bytes)
            assert_source_unchanged()

            unavailable_directory = root / "unavailable-source"
            native_read_bytes = pathlib.Path.read_bytes

            def deny_source_boundary_bytes(path: pathlib.Path) -> bytes:
                if path == source_shallow_path:
                    raise PermissionError("instrumented unavailable native shallow metadata")
                return native_read_bytes(path)

            # Permission bits are ineffective under an elevated test runner;
            # instrument only the native boundary read and leave Git commands
            # and all transport operations unmocked.
            with mock.patch.object(pathlib.Path, "read_bytes", deny_source_boundary_bytes):
                with self.assertRaisesRegex(ValueError, "shallow source boundary metadata is unavailable"):
                    self._read_native_shallow_boundaries(source)
                with self.assertRaisesRegex(ValueError, "shallow source boundary metadata is unavailable"):
                    self._make_genuine_shallow_local_source(unavailable_directory, source_root=source)
                assert_no_transport(unavailable_directory, origin_exists=False)
            assert_source_unchanged()

            # Git does not report an empty shallow file as a shallow
            # repository, so instrument the declared-shallow seam to exercise
            # the impossible race/guard state without mocking transport.
            empty_source_directory = root / "instrumented-empty-source"
            with mock.patch.object(
                self,
                "_read_native_shallow_boundaries",
                return_value=("true", source_shallow_path, b""),
            ):
                with self.assertRaisesRegex(ValueError, "shallow source boundary metadata is empty"):
                    self._make_genuine_shallow_local_source(empty_source_directory, source_root=source)
            assert_no_transport(empty_source_directory, origin_exists=True)
            assert_source_unchanged()

            for label, destination_bytes, pattern in (
                ("empty-destination", b"", "bare-origin shallow boundary metadata is empty"),
                (
                    "conflicting-destination",
                    trusted_main.encode("ascii") + b"\n",
                    "bare-origin shallow boundary metadata conflicts with source",
                ),
            ):
                with self.subTest(destination_guard=label):
                    directory = root / label
                    origin = directory / "shallow-source-origin.git"
                    origin.mkdir(parents=True)
                    git(["init", "--quiet", "--bare", "--initial-branch=main", str(origin)])
                    origin_shallow = pathlib.Path(output([
                        "--git-dir", str(origin), "rev-parse", "--path-format=absolute",
                        "--git-path", "shallow",
                    ], cwd=directory))
                    self.assertNotEqual(destination_bytes, source_shallow_bytes)
                    origin_shallow.write_bytes(destination_bytes)
                    with self.assertRaisesRegex(ValueError, pattern):
                        self._make_genuine_shallow_local_source(directory, source_root=source)
                    self.assertEqual(origin_shallow.read_bytes(), destination_bytes)
                    assert_no_transport(directory, origin_exists=True)
                    assert_source_unchanged()

    def test_shared_clone_preserves_shallow_boundary_for_linked_worktree(self) -> None:
        """A shared clone retains the linked source's authentic shallow boundary."""
        environment = self._closed_git_environment()

        def git(args: list[str], *, cwd: pathlib.Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                ["git", *args], cwd=cwd, check=False, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment,
            )
            if check and result.returncode:
                raise AssertionError(f"git {' '.join(args)} failed: {result.stderr[-2000:]}")
            return result

        def output(args: list[str], *, cwd: pathlib.Path) -> str:
            return git(args, cwd=cwd).stdout.strip()

        def invoke_and_capture_fetch(
            source_root: pathlib.Path,
            destination: pathlib.Path,
            *,
            expected_main: str | None = None,
            after_clone: object | None = None,
        ) -> tuple[tuple[str, str] | None, list[dict[str, object]], Exception | None]:
            original_run = subprocess.run
            fetch_snapshots: list[dict[str, object]] = []

            def native_output(args: list[str], *, cwd: pathlib.Path) -> str:
                result = original_run(
                    ["git", *args], cwd=cwd, check=True, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=environment,
                )
                return result.stdout.strip()

            def intercept(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                command = args[0] if args else kwargs.get("args")
                is_command = isinstance(command, list)
                if (
                    is_command
                    and command[:4] == ["git", "fetch", "--no-tags", "--depth=64"]
                    and pathlib.Path(kwargs.get("cwd", "")).resolve() == destination.resolve()
                ):
                    destination_shallow = pathlib.Path(native_output(
                        ["rev-parse", "--path-format=absolute", "--git-path", "shallow"],
                        cwd=destination,
                    ))
                    destination_objects = pathlib.Path(native_output(
                        ["rev-parse", "--path-format=absolute", "--git-path", "objects"],
                        cwd=destination,
                    )).resolve()
                    source_shallow = pathlib.Path(native_output(
                        ["rev-parse", "--path-format=absolute", "--git-path", "shallow"],
                        cwd=source_root,
                    ))
                    destination_alternates = pathlib.Path(native_output(
                        ["rev-parse", "--path-format=absolute", "--git-path", "objects/info/alternates"],
                        cwd=destination,
                    ))
                    fetch_snapshots.append({
                        "destination_shallow_bytes": destination_shallow.read_bytes() if destination_shallow.exists() else None,
                        "destination_state": native_output(["rev-parse", "--is-shallow-repository"], cwd=destination),
                        "destination_alternates": destination_alternates.read_text(encoding="utf-8") if destination_alternates.exists() else None,
                        "destination_objects": destination_objects,
                        "source_shallow_bytes": source_shallow.read_bytes(),
                    })
                    return original_run(*args, **kwargs)

                result = original_run(*args, **kwargs)
                if (
                    is_command
                    and command[:2] == ["git", "clone"]
                    and pathlib.Path(command[-1]).resolve() == destination.resolve()
                    and callable(after_clone)
                ):
                    after_clone(destination)
                return result

            try:
                with mock.patch.object(subprocess, "run", side_effect=intercept):
                    clone_result = self._clone_disposable_at_verified_main(
                        source_root, destination, expected_main=expected_main, transport="borrowed-source",
                    )
            except Exception as error:
                return None, fetch_snapshots, error
            return clone_result, fetch_snapshots, None

        with tempfile.TemporaryDirectory(prefix="completeness-shallow-linked-boundary-") as name:
            root = pathlib.Path(name)
            remote = root / "origin.git"
            seed = root / "seed"
            source = root / "source"
            linked = root / "nested-linked-source"
            old_destination = root / "old-shared-clone"
            destination = root / "verified-main-clone"

            def assert_borrowed_origin_round_trip(source_root: pathlib.Path, label: str) -> None:
                source_head_before = output(["rev-parse", "HEAD^{commit}"], cwd=source_root)
                source_head_tree_before = output(["rev-parse", "HEAD^{tree}"], cwd=source_root)
                source_main_before = output(["rev-parse", "refs/remotes/origin/main^{commit}"], cwd=source_root)
                source_main_tree_before = output(["rev-parse", f"{source_main_before}^{{tree}}"], cwd=source_root)
                source_state, source_boundary_path, source_boundary_bytes = (
                    self._read_native_shallow_boundaries(source_root)
                )
                self.assertEqual(source_state, "true")
                self.assertIsInstance(source_boundary_bytes, bytes)
                source_refs_before = output(["show-ref"], cwd=source_root)
                self.assertGreater(
                    (source_root / "data/data-go-kr.registry.json").stat().st_size, 1024,
                )

                directory = root / f"{label}-bare-origin-round-trip"
                directory.mkdir()
                checkout, pr_head, trusted_main, trusted_tree = self._make_genuine_shallow_local_source(
                    directory, source_root=source_root,
                )
                bare_origin = directory / "shallow-source-origin.git"
                origin_shallow = bare_origin / "shallow"
                self.assertEqual(origin_shallow.read_bytes(), source_boundary_bytes)
                self.assertEqual(
                    output(["--git-dir", str(bare_origin), "rev-parse", "--is-shallow-repository"], cwd=source_root),
                    "true",
                )
                self.assertEqual(pr_head, source_head_before)
                self.assertEqual(trusted_main, source_main_before)
                self.assertEqual(trusted_tree, source_main_tree_before)
                self.assertEqual(output(["rev-parse", "HEAD^{commit}"], cwd=checkout), pr_head)
                self.assertEqual(output(["rev-parse", "refs/remotes/origin/main^{commit}"], cwd=checkout), trusted_main)
                self.assertEqual(output(["rev-parse", "HEAD^{tree}"], cwd=checkout), source_head_tree_before)
                self.assertEqual(
                    output(["rev-parse", "refs/remotes/origin/main^{tree}"], cwd=checkout),
                    trusted_tree,
                )
                self.assertEqual(output(["rev-parse", "--is-shallow-repository"], cwd=checkout), "true")
                self.assertEqual(git(["rev-list", "--all", "--parents"], cwd=checkout, check=False).returncode, 0)
                self.assertEqual(source_boundary_path.read_bytes(), source_boundary_bytes)
                self.assertEqual(output(["rev-parse", "HEAD^{commit}"], cwd=source_root), source_head_before)
                self.assertEqual(
                    output(["rev-parse", "refs/remotes/origin/main^{commit}"], cwd=source_root),
                    source_main_before,
                )
                self.assertEqual(output(["show-ref"], cwd=source_root), source_refs_before)

            git(["init", "--quiet", "--bare", "--initial-branch=main", str(remote)])
            git(["init", "--quiet", "--initial-branch=main", str(seed)])
            for key, value in (
                ("user.name", "Completeness shallow-boundary test"),
                ("user.email", "completeness-shallow@example.invalid"),
                ("commit.gpgsign", "false"),
                ("gc.auto", "0"),
            ):
                git(["config", key, value], cwd=seed)
            git(["remote", "add", "origin", str(remote)], cwd=seed)

            (seed / "trusted-main.txt").write_text("trusted main\n", encoding="utf-8")
            git(["add", "trusted-main.txt"], cwd=seed)
            git(["commit", "--quiet", "-m", "trusted main"], cwd=seed)
            missing_parent = output(["rev-parse", "HEAD^{commit}"], cwd=seed)
            boundary_commit: str | None = None
            for index in range(1, 101):
                with (seed / "trusted-main.txt").open("a", encoding="utf-8") as handle:
                    handle.write(f"trusted main {index}\n")
                if index == 30:
                    fixture_registry = seed / "data/data-go-kr.registry.json"
                    fixture_registry.parent.mkdir(parents=True)
                    fixture_registry.write_text(json.dumps({"fixture": "x" * 2048}) + "\n", encoding="utf-8")
                    (seed / "boundary.txt").write_text("native shallow boundary\n", encoding="utf-8")
                    git(["add", "data/data-go-kr.registry.json", "boundary.txt"], cwd=seed)
                git(["commit", "--quiet", "-am", f"trusted main {index}"], cwd=seed)
                if index == 29:
                    missing_parent = output(["rev-parse", "HEAD^{commit}"], cwd=seed)
                if index == 30:
                    boundary_commit = output(["rev-parse", "HEAD^{commit}"], cwd=seed)
            trusted_main = output(["rev-parse", "HEAD^{commit}"], cwd=seed)
            trusted_tree = output(["rev-parse", f"{trusted_main}^{{tree}}"], cwd=seed)

            self.assertIsNotNone(boundary_commit)
            git(["switch", "--quiet", "-c", "source-history"], cwd=seed)
            (seed / "source-pr.txt").write_text("descendant PR\n", encoding="utf-8")
            git(["add", "source-pr.txt"], cwd=seed)
            git(["commit", "--quiet", "-m", "descendant source PR"], cwd=seed)
            git(["push", "--quiet", "origin", "refs/heads/main:refs/heads/main"], cwd=seed)
            git(["push", "--quiet", "origin", "refs/heads/source-history:refs/heads/prmerge"], cwd=seed)

            git([
                "clone", "--quiet", "--no-tags", "--depth=72", "--branch", "prmerge",
                f"file://{remote}", str(source),
            ])
            git([
                "fetch", "--quiet", "--no-tags", "--depth=64", "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ], cwd=source)
            source_shallow_path = pathlib.Path(output(
                ["rev-parse", "--path-format=absolute", "--git-path", "shallow"], cwd=source,
            ))
            source_shallow_bytes = source_shallow_path.read_bytes()
            source_shallow_lines = source_shallow_bytes.decode("ascii").splitlines()
            self.assertEqual(len(source_shallow_lines), 2)
            self.assertIn(boundary_commit, source_shallow_lines)
            self.assertNotIn(missing_parent, source_shallow_lines)
            assert_borrowed_origin_round_trip(source, "ordinary-source")

            standalone_destination = root / "native-standalone-clone"
            standalone_result, standalone_fetches, standalone_error = invoke_and_capture_fetch(
                source, standalone_destination, expected_main=trusted_main,
            )
            self.assertIsNone(standalone_error)
            self.assertIsNotNone(standalone_result)
            self.assertEqual(len(standalone_fetches), 1)
            standalone_before_fetch = standalone_fetches[0]
            standalone_boundaries = standalone_before_fetch["destination_shallow_bytes"]
            self.assertIsInstance(standalone_boundaries, bytes)
            self.assertNotEqual(standalone_boundaries, source_shallow_bytes)
            standalone_boundary_lines = standalone_boundaries.decode("ascii").splitlines()
            self.assertEqual(len(standalone_boundary_lines), 1)
            self.assertTrue(set(standalone_boundary_lines) < set(source_shallow_lines))
            self.assertEqual(standalone_before_fetch["destination_state"], "true")
            self.assertIsNone(standalone_before_fetch["destination_alternates"])
            self.assertEqual(standalone_before_fetch["source_shallow_bytes"], source_shallow_bytes)
            self.assertEqual(standalone_result, (trusted_main, trusted_tree))
            self.assertEqual(output(["rev-parse", "refs/remotes/origin/main^{commit}"], cwd=standalone_destination), trusted_main)
            self.assertEqual(output(["rev-parse", "HEAD^{commit}"], cwd=standalone_destination), trusted_main)
            self.assertEqual(output(["rev-parse", "HEAD^{tree}"], cwd=standalone_destination), trusted_tree)
            self.assertEqual(git(["rev-list", "--all", "--parents"], cwd=standalone_destination, check=False).returncode, 0)
            self.assertEqual(source_shallow_path.read_bytes(), source_shallow_bytes)

            git(["config", "user.name", "Completeness shallow-boundary test"], cwd=source)
            git(["config", "user.email", "completeness-shallow@example.invalid"], cwd=source)
            git(["config", "commit.gpgsign", "false"], cwd=source)
            (source / "later-descendant.txt").write_text("later linked-worktree head\n", encoding="utf-8")
            git(["add", "later-descendant.txt"], cwd=source)
            git(["commit", "--quiet", "-m", "later descendant above shallow boundary"], cwd=source)
            source_head = output(["rev-parse", "HEAD^{commit}"], cwd=source)
            git(["worktree", "add", "--quiet", "--detach", str(linked), "HEAD"], cwd=source)

            self.assertTrue((linked / ".git").is_file())
            self.assertEqual(output(["rev-parse", "--is-shallow-repository"], cwd=linked), "true")
            self.assertNotEqual(
                git(["cat-file", "-e", f"{missing_parent}^{{commit}}"], cwd=linked, check=False).returncode,
                0,
            )
            self.assertEqual(output(["rev-parse", "refs/remotes/origin/main^{commit}"], cwd=linked), trusted_main)
            self.assertEqual(output(["rev-parse", f"{trusted_main}^{{tree}}"], cwd=linked), trusted_tree)
            source_shallow_path = pathlib.Path(output(
                ["rev-parse", "--path-format=absolute", "--git-path", "shallow"], cwd=linked,
            ))
            source_shallow_bytes = source_shallow_path.read_bytes()
            self.assertTrue(source_shallow_bytes)
            self.assertIn(boundary_commit.encode("ascii"), source_shallow_bytes.splitlines())
            source_common_dir = pathlib.Path(output(
                ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=linked,
            ))
            assert_borrowed_origin_round_trip(linked, "linked-source")

            git(["clone", "--quiet", "--shared", "--no-checkout", str(linked), str(old_destination)])
            old_shallow_path = pathlib.Path(output(
                ["rev-parse", "--path-format=absolute", "--git-path", "shallow"], cwd=old_destination,
            ))
            self.assertFalse(old_shallow_path.exists())
            self.assertEqual(output(["rev-parse", "--is-shallow-repository"], cwd=old_destination), "false")
            self.assertEqual(
                git(["rev-list", "--all", "--parents"], cwd=old_destination, check=False).returncode,
                128,
            )

            wrong_destination = root / "wrong-main-clone"
            with self.assertRaisesRegex(ValueError, "expected trusted main does not match source origin/main"):
                self._clone_disposable_at_verified_main(
                    linked, wrong_destination, expected_main="0" * 40, transport="borrowed-source",
                )
            self.assertFalse(wrong_destination.exists())

            empty_boundary_destination = root / "empty-boundary-clone"
            try:
                source_shallow_path.write_bytes(b"")
                with self.assertRaisesRegex(ValueError, "shallow source boundary metadata is empty"):
                    self._clone_disposable_at_verified_main(
                        linked, empty_boundary_destination, transport="borrowed-source",
                    )
                self.assertFalse(empty_boundary_destination.exists())
            finally:
                source_shallow_path.write_bytes(source_shallow_bytes)

            malformed_boundary_destination = root / "malformed-boundary-clone"
            try:
                source_shallow_path.write_bytes(b"not-a-git-object-id\n")
                with self.assertRaises(subprocess.CalledProcessError):
                    self._clone_disposable_at_verified_main(
                        linked, malformed_boundary_destination, transport="borrowed-source",
                    )
                self.assertFalse(malformed_boundary_destination.exists())
            finally:
                source_shallow_path.write_bytes(source_shallow_bytes)

            empty_native_destination = root / "empty-native-boundary-clone"
            empty_native_result, empty_native_fetches, empty_native_error = invoke_and_capture_fetch(
                linked,
                empty_native_destination,
                after_clone=lambda clone_path: (clone_path / ".git/shallow").write_bytes(b""),
            )
            self.assertIsNone(empty_native_result)
            self.assertEqual(empty_native_fetches, [])
            self.assertIsInstance(empty_native_error, ValueError)
            self.assertRegex(str(empty_native_error), "disposable clone shallow boundary metadata is empty")

            malformed_native_destination = root / "malformed-native-boundary-clone"
            malformed_native_result, malformed_native_fetches, malformed_native_error = invoke_and_capture_fetch(
                linked,
                malformed_native_destination,
                after_clone=lambda clone_path: (clone_path / ".git/shallow").write_bytes(b"not-a-git-object-id\n"),
            )
            self.assertIsNone(malformed_native_result)
            self.assertEqual(malformed_native_fetches, [])
            self.assertIsInstance(malformed_native_error, subprocess.CalledProcessError)

            linked_result, linked_fetches, linked_error = invoke_and_capture_fetch(linked, destination)
            self.assertIsNone(linked_error)
            self.assertIsNotNone(linked_result)
            self.assertEqual(len(linked_fetches), 1)
            linked_before_fetch = linked_fetches[0]
            self.assertEqual(linked_before_fetch["destination_shallow_bytes"], source_shallow_bytes)
            self.assertEqual(linked_before_fetch["destination_state"], "true")
            self.assertEqual(linked_before_fetch["source_shallow_bytes"], source_shallow_bytes)
            linked_alternates = linked_before_fetch["destination_alternates"]
            self.assertIsInstance(linked_alternates, str)
            linked_alternate_paths = [
                pathlib.Path(entry.strip()).resolve()
                for entry in linked_alternates.splitlines()
                if entry.strip()
            ]
            self.assertIn((source_common_dir / "objects").resolve(), linked_alternate_paths)
            cloned_main, cloned_tree = linked_result
            self.assertEqual(cloned_main, trusted_main)
            self.assertEqual(cloned_tree, trusted_tree)
            self.assertEqual(output(["rev-parse", "--is-shallow-repository"], cwd=destination), "true")
            self.assertEqual(
                git(["rev-list", "--all", "--parents"], cwd=destination, check=False).returncode,
                0,
            )
            self.assertEqual(
                pathlib.Path((destination / ".git/objects/info/alternates").read_text(encoding="utf-8").strip()),
                source_common_dir / "objects",
            )
            self.assertEqual(output(["rev-parse", "refs/remotes/origin/main^{commit}"], cwd=destination), trusted_main)
            self.assertEqual(output(["rev-parse", "HEAD^{commit}"], cwd=destination), trusted_main)
            self.assertEqual(output(["rev-parse", "HEAD^{tree}"], cwd=destination), trusted_tree)
            self.assertEqual(source_shallow_path.read_bytes(), source_shallow_bytes)
            self.assertEqual(output(["rev-parse", "HEAD^{commit}"], cwd=linked), source_head)
            self.assertEqual(output(["rev-parse", "refs/remotes/origin/main^{commit}"], cwd=linked), trusted_main)
            self.assertEqual(output(["rev-parse", f"{trusted_main}^{{tree}}"], cwd=linked), trusted_tree)

            # A shallow repository can advertise a complete selected main
            # while retaining unrelated shallow boundaries for old refs.
            complete_tree = output(["rev-parse", "HEAD^{tree}"], cwd=source)
            complete_main = git(["commit-tree", complete_tree, "-m", "complete selected main"], cwd=source).stdout.strip()
            git(["update-ref", "refs/heads/complete", complete_main], cwd=source)
            git(["update-ref", "refs/remotes/origin/main", complete_main], cwd=source)
            git(["switch", "--quiet", "complete"], cwd=source)
            git(["branch", "-D", "prmerge"], cwd=source)
            git(["update-ref", "-d", "refs/remotes/origin/prmerge"], cwd=source)
            git(["symbolic-ref", "--delete", "refs/remotes/origin/HEAD"], cwd=source, check=False)
            complete_source_shallow_bytes = source_shallow_path.read_bytes()
            complete_source_refs = output(["show-ref"], cwd=source)
            self.assertEqual(output(["rev-parse", "--is-shallow-repository"], cwd=source), "true")
            self.assertEqual(output(["rev-parse", "refs/remotes/origin/main^{commit}"], cwd=source), complete_main)
            self.assertEqual(output(["rev-parse", f"{complete_main}^{{tree}}"], cwd=source), complete_tree)

            complete_destination = root / "complete-selected-main-clone"
            complete_result, complete_fetches, complete_error = invoke_and_capture_fetch(
                source, complete_destination,
            )
            self.assertIsNone(complete_error)
            self.assertIsNotNone(complete_result)
            self.assertEqual(complete_result, (complete_main, complete_tree))
            self.assertEqual(len(complete_fetches), 1)
            complete_before_fetch = complete_fetches[0]
            self.assertIsNone(complete_before_fetch["destination_shallow_bytes"])
            self.assertEqual(complete_before_fetch["destination_state"], "false")
            self.assertIsNone(complete_before_fetch["destination_alternates"])
            self.assertEqual(complete_before_fetch["source_shallow_bytes"], complete_source_shallow_bytes)
            complete_destination_shallow = pathlib.Path(output(
                ["rev-parse", "--path-format=absolute", "--git-path", "shallow"], cwd=complete_destination,
            ))
            self.assertFalse(complete_destination_shallow.exists())
            self.assertEqual(output(["rev-parse", "--is-shallow-repository"], cwd=complete_destination), "false")
            self.assertFalse((complete_destination / ".git/objects/info/alternates").exists())
            self.assertEqual(output(["rev-parse", "refs/remotes/origin/main^{commit}"], cwd=complete_destination), complete_main)
            self.assertEqual(output(["rev-parse", "HEAD^{tree}"], cwd=complete_destination), complete_tree)
            self.assertEqual(git(["rev-list", "--all", "--parents"], cwd=complete_destination, check=False).returncode, 0)
            self.assertEqual(source_shallow_path.read_bytes(), complete_source_shallow_bytes)
            self.assertEqual(output(["show-ref"], cwd=source), complete_source_refs)

            unrelated_objects = pathlib.Path(f"{source_common_dir / 'objects'} ")
            unrelated_objects.mkdir()
            mismatched_alternate_destination = root / "mismatched-alternate-clone"

            def add_unrelated_alternate(clone_path: pathlib.Path) -> None:
                alternates_path = clone_path / ".git/objects/info/alternates"
                alternates_path.parent.mkdir(parents=True, exist_ok=True)
                alternates_path.write_text(f"{unrelated_objects}\n", encoding="utf-8")

            mismatched_result, mismatched_fetches, mismatched_error = invoke_and_capture_fetch(
                source,
                mismatched_alternate_destination,
                after_clone=add_unrelated_alternate,
            )
            self.assertIsNone(mismatched_error)
            self.assertIsNotNone(mismatched_result)
            self.assertEqual(mismatched_result, (complete_main, complete_tree))
            self.assertEqual(len(mismatched_fetches), 1)
            mismatched_before_fetch = mismatched_fetches[0]
            self.assertIsNone(mismatched_before_fetch["destination_shallow_bytes"])
            self.assertEqual(mismatched_before_fetch["destination_state"], "false")
            mismatched_alternate_entries = mismatched_before_fetch["destination_alternates"].splitlines()
            self.assertEqual(mismatched_alternate_entries, [str(unrelated_objects)])
            self.assertEqual(
                pathlib.Path(mismatched_alternate_entries[0]).resolve(),
                unrelated_objects.resolve(),
            )
            self.assertNotEqual(
                pathlib.Path(mismatched_alternate_entries[0]).resolve(),
                (source_common_dir / "objects").resolve(),
            )
            mismatched_destination_shallow = pathlib.Path(output(
                ["rev-parse", "--path-format=absolute", "--git-path", "shallow"],
                cwd=mismatched_alternate_destination,
            ))
            self.assertFalse(mismatched_destination_shallow.exists())
            self.assertEqual(output(["rev-parse", "--is-shallow-repository"], cwd=mismatched_alternate_destination), "false")
            self.assertEqual(
                source_shallow_path.read_bytes(), complete_source_shallow_bytes,
            )
            self.assertEqual(output(["show-ref"], cwd=source), complete_source_refs)

    def test_nested_linked_worktree_fetch_preserves_authenticated_main(self) -> None:
        """The reader preserves exact main from pointer and materialized shallow sources."""
        maintenance_guard = mock.patch.dict(os.environ, {
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "gc.auto",
            "GIT_CONFIG_VALUE_0": "0",
            "GIT_CONFIG_KEY_1": "maintenance.auto",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "gc.autoDetach",
            "GIT_CONFIG_VALUE_2": "false",
        })
        maintenance_guard.start()
        self.addCleanup(maintenance_guard.stop)
        environment = self._closed_git_environment()
        environment.update({
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "gc.auto",
            "GIT_CONFIG_VALUE_0": "0",
            "GIT_CONFIG_KEY_1": "maintenance.auto",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "gc.autoDetach",
            "GIT_CONFIG_VALUE_2": "false",
        })
        integration = importlib.import_module("tests.test_apply_runtime_freshness_import_integration")
        fixture = integration.ApplyRuntimeFreshnessImportIntegrationTest
        pointer_source_revision = subprocess.check_output(
            ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
            cwd=integration.ROOT,
            text=True,
            env=environment,
        ).strip()
        for label in ("genuine_pointer", "already_materialized"):
            with self.subTest(source_context=label):
                # setUpClass allocates its TemporaryDirectory before building
                # the repository. Keep partial setup inside the cleanup
                # boundary and never let teardown mask the test failure.
                fixture.temporary = None
                prepared_temporary = None
                setup_or_test_failed = False
                try:
                    if label == "genuine_pointer":
                        fixture.setUpClass(
                            materialize_registry=False,
                            source_revision=pointer_source_revision,
                            source_root=integration.ROOT,
                        )
                        expected_representation = "lfs_pointer"
                    else:
                        # First produce the same committed materialized fixture
                        # that the outer import test feeds to its nested suite,
                        # then use that commit as the next fixture's source.
                        fixture.setUpClass(source_root=integration.ROOT)
                        prepared_temporary = fixture.temporary
                        prepared_source_root = fixture.repo
                        prepared_source_revision = fixture.fixture_revision
                        fixture.setUpClass(
                            materialize_registry=True,
                            source_revision=prepared_source_revision,
                            source_root=prepared_source_root,
                            authenticated_main_root=integration.ROOT,
                        )
                        expected_representation = "materialized"
                    self.assertEqual(fixture.registry_representation, expected_representation)
                    source_root = fixture.repo
                    base = fixture.temp_root
                    linked = base / f"nested-linked-source-{label}"
                    disposable = base / f"nested-disposable-main-{label}"
                    worktree_add = subprocess.run(
                        ["git", "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", str(linked), "HEAD"],
                        cwd=source_root,
                        env=environment,
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    self.assertEqual(worktree_add.returncode, 0, worktree_add.stderr.decode("utf-8", errors="replace"))
                    self.assertTrue((linked / ".git").is_file())
                    self.assertEqual(
                        subprocess.check_output(
                            ["git", "rev-parse", "--is-shallow-repository"], cwd=linked, text=True,
                            env=environment,
                        ).strip(),
                        "true",
                    )
                    source_shallow_path = pathlib.Path(subprocess.check_output(
                        ["git", "rev-parse", "--path-format=absolute", "--git-path", "shallow"],
                        cwd=linked,
                        text=True,
                        env=environment,
                    ).strip())
                    source_shallow_bytes = source_shallow_path.read_bytes()
                    self.assertTrue(source_shallow_bytes)
                    trusted_main = subprocess.check_output(
                        ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
                        cwd=linked,
                        text=True,
                        env=environment,
                    ).strip()
                    trusted_tree = subprocess.check_output(
                        ["git", "rev-parse", "--verify", f"{trusted_main}^{{tree}}"], cwd=linked, text=True,
                        env=environment,
                    ).strip()
                    common_dir = pathlib.Path(subprocess.check_output(
                        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=linked, text=True,
                        env=environment,
                    ).strip())

                    cloned_main, cloned_tree = self._clone_disposable_at_verified_main(
                        linked, disposable, transport="borrowed-source",
                    )

                    self.assertEqual(cloned_main, trusted_main)
                    self.assertEqual(cloned_tree, trusted_tree)
                    self.assertEqual(
                        subprocess.check_output(
                            ["git", "rev-parse", "HEAD^{commit}"], cwd=disposable, text=True,
                            env=environment,
                        ).strip(),
                        trusted_main,
                    )
                    self.assertEqual(
                        subprocess.check_output(
                            ["git", "rev-parse", "--is-shallow-repository"], cwd=disposable, text=True,
                            env=environment,
                        ).strip(),
                        "true",
                    )
                    alternates = disposable / ".git/objects/info/alternates"
                    self.assertTrue(alternates.is_file())
                    self.assertEqual(pathlib.Path(alternates.read_text(encoding="utf-8").strip()), common_dir / "objects")
                    graph = subprocess.run(
                        ["git", "rev-list", "--all", "--parents"],
                        cwd=disposable,
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=environment,
                        text=True,
                    )
                    self.assertEqual(graph.returncode, 0, graph.stderr)
                    self.assertEqual(source_shallow_path.read_bytes(), source_shallow_bytes)

                    source_blob = subprocess.check_output(
                        ["git", "show", f"{fixture.source_revision}:{fixture.registry_identity.path}"],
                        cwd=fixture.source_root,
                        env=environment,
                    )
                    self.assertEqual(integration.sha256_bytes(source_blob), fixture.original_registry_blob_sha256)
                    wrong_main = base / f"nested-wrong-main-{label}"
                    with self.assertRaisesRegex(ValueError, "expected trusted main does not match source origin/main"):
                        self._clone_disposable_at_verified_main(
                            linked, wrong_main, expected_main="0" * 40, transport="borrowed-source",
                        )
                    self.assertFalse(wrong_main.exists())
                except BaseException:
                    setup_or_test_failed = True
                    raise
                finally:
                    if getattr(fixture, "temporary", None) is not None:
                        try:
                            fixture.tearDownClass()
                        except BaseException:
                            if not setup_or_test_failed:
                                raise
                    if prepared_temporary is not None:
                        prepared_temporary.cleanup()

    def test_bounded_main_transport_rejects_unknown_mode_before_git(self) -> None:
        with tempfile.TemporaryDirectory(prefix="completeness-fetch-transport-mode-") as name:
            with (
                mock.patch.object(subprocess, "check_output") as check_output,
                mock.patch.object(subprocess, "run") as run,
                self.assertRaisesRegex(ValueError, "transport must be bounded-main or borrowed-source"),
            ):
                self._clone_disposable_at_verified_main(
                    pathlib.Path(name), pathlib.Path(name) / "destination", transport="refetch-all",
                )
            check_output.assert_not_called()
            run.assert_not_called()

    def test_bounded_main_fetch_fails_when_one_wanted_loose_blob_is_missing(self) -> None:
        """A fresh bounded fetch still requires every object reachable from exact main."""
        with tempfile.TemporaryDirectory(prefix="completeness-fetch-one-missing-blob-") as name:
            root = pathlib.Path(name)
            source = root / "source"
            destination = root / "disposable"
            environment = self._closed_git_environment()
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main", str(source)],
                check=True,
                env=environment,
            )

            def git_output(*args: str) -> str:
                return subprocess.check_output(["git", *args], cwd=source, text=True, env=environment).strip()

            for key, value in (
                ("user.name", "Completeness fetch test"),
                ("user.email", "completeness-fetch@example.invalid"),
                ("gc.auto", "0"),
            ):
                subprocess.run(["git", "config", key, value], cwd=source, check=True, env=environment)
            (source / "main.txt").write_text("wanted bounded-main blob\n", encoding="utf-8")
            subprocess.run(["git", "add", "main.txt"], cwd=source, check=True, env=environment)
            subprocess.run(["git", "commit", "--quiet", "-m", "trusted main"], cwd=source, check=True, env=environment)
            trusted_main = git_output("rev-parse", "HEAD^{commit}")
            trusted_tree = git_output("rev-parse", f"{trusted_main}^{{tree}}")
            wanted_blob = git_output("rev-parse", "HEAD:main.txt")
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", trusted_main],
                cwd=source,
                check=True,
                env=environment,
            )
            expected_objects = set(git_output("rev-list", "--objects", "--all").splitlines())
            expected_oids = {item.split(" ", 1)[0] for item in expected_objects}
            self.assertEqual(expected_oids, {trusted_main, trusted_tree, wanted_blob})
            self.assertIn("in-pack: 0", git_output("count-objects", "-v").splitlines())
            for oid in expected_oids:
                subprocess.run(["git", "cat-file", "-e", oid], cwd=source, check=True, env=environment)

            missing_blob_path = source / ".git/objects" / wanted_blob[:2] / wanted_blob[2:]
            self.assertTrue(missing_blob_path.is_file())
            missing_blob_path.unlink()
            missing_objects = [
                oid for oid in expected_oids
                if subprocess.run(
                    ["git", "cat-file", "-e", oid], cwd=source, check=False, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                ).returncode != 0
            ]
            self.assertEqual(missing_objects, [wanted_blob])
            self.assertEqual(git_output("rev-parse", "refs/remotes/origin/main^{commit}"), trusted_main)
            self.assertEqual(git_output("rev-parse", f"{trusted_main}^{{tree}}"), trusted_tree)

            with self.assertRaises(subprocess.CalledProcessError) as captured:
                self._clone_disposable_at_verified_main(source, destination)

            error = captured.exception
            self.assertNotEqual(error.returncode, 0)
            self.assertEqual(
                error.cmd,
                [
                    "git", "fetch", "--no-tags", "--depth=64", str(source),
                    f"{trusted_main}:refs/remotes/origin/main",
                ],
            )
            self.assertTrue(error.stderr)
            self.assertTrue(destination.is_dir())

    def test_failed_local_main_fetch_keeps_original_error_and_bounded_diagnostics(self) -> None:
        """A real local fetch failure surfaces its stderr without replacing the Git error."""
        with tempfile.TemporaryDirectory(prefix="completeness-fetch-diagnostic-") as name:
            root = pathlib.Path(name)
            source = root / "source"
            destination = root / "disposable"
            environment = self._closed_git_environment()
            source.mkdir()
            subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(source)], check=True, env=environment)
            subprocess.run(["git", "config", "user.name", "Completeness fetch test"], cwd=source, check=True, env=environment)
            subprocess.run(["git", "config", "user.email", "completeness-fetch@example.invalid"], cwd=source, check=True, env=environment)
            subprocess.run(["git", "config", "gc.auto", "0"], cwd=source, check=True, env=environment)
            (source / "main.txt").write_text("trusted main\n", encoding="utf-8")
            subprocess.run(["git", "add", "main.txt"], cwd=source, check=True, env=environment)
            subprocess.run(["git", "commit", "--quiet", "-m", "trusted main"], cwd=source, check=True, env=environment)
            trusted_main = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True, env=environment).strip()
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", trusted_main], cwd=source, check=True,
                env=environment,
            )
            loose_commit = source / ".git/objects" / trusted_main[:2] / trusted_main[2:]
            self.assertTrue(loose_commit.is_file())
            real_run = subprocess.run
            removed_commit = False

            def remove_commit_after_init(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                nonlocal removed_commit
                argv = list(args[0]) if args and isinstance(args[0], (list, tuple)) else []
                result = real_run(*args, **kwargs)
                if (
                    argv[:2] == ["git", "init"]
                    and str(destination) in argv
                    and result.returncode == 0
                ):
                    loose_commit.unlink()
                    removed_commit = True
                return result

            with mock.patch.object(subprocess, "run", side_effect=remove_commit_after_init):
                with self.assertRaises(subprocess.CalledProcessError) as captured:
                    self._clone_disposable_at_verified_main(source, destination)

            error = captured.exception
            self.assertTrue(removed_commit)
            self.assertNotEqual(error.returncode, 0)
            self.assertIn(b"not our ref", error.stderr)
            notes = "\n".join(getattr(error, "__notes__", []))
            self.assertIn("bounded exact-main fetch diagnostics", notes)
            self.assertIn(trusted_main, notes)
            self.assertIn(str(source), notes)
            self.assertIn(str(destination), notes)
            self.assertIn("filesystem_free_bytes", notes)
            self.assertIn("not our ref", notes)
            self.assertLess(len(notes), 4000)

    def test_fetch_diagnostics_bound_long_control_paths_and_keep_stderr_on_metadata_error(self) -> None:
        """Optional filesystem diagnostics cannot hide stderr or exceed the serialized note budget."""
        with tempfile.TemporaryDirectory(prefix="completeness-fetch-long-path-") as name:
            root = pathlib.Path(name)
            for index in range(5):
                root = root / (str(index) + "-" + ("p" * 74))
            root = root / "control-\x01-path"
            root.mkdir(parents=True)
            source = root / "source"
            destination = root / "disposable"
            environment = self._closed_git_environment()
            source.mkdir()
            subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(source)], check=True, env=environment)
            subprocess.run(["git", "config", "user.name", "Completeness fetch test"], cwd=source, check=True, env=environment)
            subprocess.run(["git", "config", "user.email", "completeness-fetch@example.invalid"], cwd=source, check=True, env=environment)
            subprocess.run(["git", "config", "gc.auto", "0"], cwd=source, check=True, env=environment)
            (source / "main.txt").write_text("trusted main\n", encoding="utf-8")
            subprocess.run(["git", "add", "main.txt"], cwd=source, check=True, env=environment)
            subprocess.run(["git", "commit", "--quiet", "-m", "trusted main"], cwd=source, check=True, env=environment)
            trusted_main = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True, env=environment).strip()
            trusted_tree = subprocess.check_output(
                ["git", "rev-parse", "--verify", f"{trusted_main}^{{tree}}"], cwd=source, text=True, env=environment,
            ).strip()
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", trusted_main], cwd=source, check=True,
                env=environment,
            )
            loose_commit = source / ".git/objects" / trusted_main[:2] / trusted_main[2:]
            self.assertTrue(loose_commit.is_file())
            real_run = subprocess.run
            removed_commit = False

            def remove_commit_after_init(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                nonlocal removed_commit
                argv = list(args[0]) if args and isinstance(args[0], (list, tuple)) else []
                result = real_run(*args, **kwargs)
                if (
                    argv[:2] == ["git", "init"]
                    and str(destination) in argv
                    and result.returncode == 0
                ):
                    loose_commit.unlink()
                    removed_commit = True
                return result

            with mock.patch.object(shutil, "disk_usage", side_effect=FileNotFoundError("fixture path vanished")):
                with mock.patch.object(subprocess, "run", side_effect=remove_commit_after_init):
                    with self.assertRaises(subprocess.CalledProcessError) as captured:
                        self._clone_disposable_at_verified_main(source, destination)

            error = captured.exception
            self.assertTrue(removed_commit)
            self.assertIsInstance(error, subprocess.CalledProcessError)
            self.assertIn(b"not our ref", error.stderr)
            notes = "\n".join(getattr(error, "__notes__", []))
            self.assertIn("bounded exact-main fetch diagnostics", notes)
            self.assertIn(trusted_main, notes)
            self.assertIn(trusted_tree, notes)
            self.assertIn("not our ref", notes)
            self.assertIn("FileNotFoundError", notes)
            self.assertLess(len(notes), 3000)
            self.assertTrue(notes.isascii(), "JSON escaping should keep control characters out of the note")

    def test_projection_rejects_an_unreviewed_published_605_contract(self) -> None:
        """A real newer generator revision is not executed until its source contract is reviewed."""
        with self.assertRaisesRegex(ValueError, "published source revision has an unreviewed #605 projection contract"):
            MODULE.project_registered_data_go_operation_manifest(
                root=ROOT,
                revision="fc96da30db8773d1e0b91394ca931436694c1503",
                registry_bytes=b"[]\n",
            )

    def test_build_report_admits_exact_subject_and_rejects_borrowed_release_member(self) -> None:
        """Exercise proof -> index -> native publisher/C admission without changing live authority."""
        original_index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        base_main = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/main"], cwd=ROOT, text=True,
        ).strip()
        common_dir = pathlib.Path(subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT, text=True,
        ).strip()).resolve()
        donor_snapshot: tuple[pathlib.Path, str, str, bytes | None, bytes] | None = None

        with tempfile.TemporaryDirectory(prefix="completeness-proof-rollup-e2e-", dir=ROOT.parent) as name:
            temp_root = pathlib.Path(name)
            self._hardlink_worktree(ROOT, temp_root)
            temp_manifest = temp_root / "manifest.json"
            temp_manifest.unlink()
            shutil.copy2(ROOT / "manifest.json", temp_manifest)
            subprocess.run(
                [sys.executable, "scripts/sync-release-manifest-artifacts.py", "--write"],
                cwd=temp_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=temp_root, check=True)
            alternate = temp_root / ".git/objects/info/alternates"
            alternate.parent.mkdir(parents=True, exist_ok=True)
            alternate.write_text(str(common_dir / "objects") + "\n")
            donor_snapshot = self._inherit_native_borrowed_shallow_boundary(ROOT, temp_root)
            self.assertEqual(donor_snapshot[0], common_dir)
            subprocess.run(["git", "config", "user.name", "Completeness rollup test"], cwd=temp_root, check=True)
            subprocess.run(["git", "config", "user.email", "completeness-test@example.invalid"], cwd=temp_root, check=True)
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", base_main], cwd=temp_root, check=True)
            subprocess.run(["git", "read-tree", base_main], cwd=temp_root, check=True)

            scope_registry = json.loads((temp_root / MODULE.SCOPE_REGISTRY_PATH).read_bytes())
            scope = next(row for row in scope_registry["scopes"] if row["resource_kind"] == "api_operation_manifest" and row["source_id"] == "data_go_kr")
            scope["authority_state"] = "available"
            scope["blockers"] = []
            self._write_json_file(temp_root, MODULE.SCOPE_REGISTRY_PATH, scope_registry)
            scope_registry, policy, scopes_by_id = MODULE.load_and_validate_registry(temp_root)
            index_path = temp_root / ".test-input-index.json"
            input_index = copy.deepcopy(original_index)
            input_index["evaluation_epoch"] = "2026-10-04T13:23:00Z"

            checked_index_path = temp_root / MODULE.INPUT_INDEX_PATH
            checked_index_path.parent.mkdir(parents=True, exist_ok=True)
            checked_index_path.unlink(missing_ok=True)
            checked_index_path.write_text(json.dumps(input_index, ensure_ascii=False, indent=2) + "\n")
            index, checked_inputs, resolved = MODULE.validate_input_index(
                root=temp_root, input_root=temp_root, index_path=checked_index_path,
                scope_by_id=scopes_by_id,
            )
            scoped = [item for item in checked_inputs if item["scope_id"] == scope["scope_id"]]
            publisher_roles = MODULE.stage_input_map(scoped, "publisher")
            publisher_run, publisher_job, _started, _completed = MODULE.validate_stage_run(
                stage="publisher", roles=publisher_roles, resolved=resolved,
                evaluation_epoch=index["evaluation_epoch"], root=temp_root,
            )

            def exact_role(role: str) -> bytes:
                item = MODULE.single_role(scoped, role)
                assert item is not None
                return MODULE.receipt_path_bytes(resolved[item["input_id"]], item)

            publication = MODULE.validate_native_publisher_attempt(
                root=temp_root,
                run=publisher_run,
                jobs=MODULE.object_at(resolved[publisher_roles["publication_producer_jobs"]["input_id"]], "publisher jobs"),
                artifact_response=MODULE.object_at(resolved[publisher_roles["publication_output_artifact"]["input_id"]], "publisher artifacts"),
                archive=exact_role("publication_artifact_archive"),
                receipt_raw=exact_role("publication_receipt"),
                binding_raw=exact_role("publication_source_binding"),
                evaluation_epoch=index["evaluation_epoch"],
            )

            source_revision = "6a5138c792f4b7402da0c5ab439646bd752a307f"
            published_generator = MODULE.git_read_only(
                temp_root, ["show", f"{source_revision}:scripts/generate-data-go-kr-operation-manifest.py"],
            )
            self.assertNotEqual(
                (temp_root / "scripts/generate-data-go-kr-operation-manifest.py").read_bytes(),
                published_generator,
                "fixture must exercise a newer local generator against the exact older published contract",
            )
            operation_path = "reports/data-go-kr/operation-manifest.json"
            source_bytes = MODULE.git_read_only(temp_root, ["show", f"{source_revision}:{operation_path}"])
            source_input_root = temp_root / ".test-proof-source-inputs"
            source_snapshot_path = source_input_root / operation_path
            source_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            source_snapshot_path.write_bytes(source_bytes)
            source_sha = MODULE.sha256_bytes(source_bytes)
            observed_at = "2026-09-29T23:44:39Z"
            source_manifest = json.loads(source_bytes)
            identities = sorted(item["operation_id"] for item in source_manifest["operations"])
            identity_sha = MODULE.sha256_bytes(json.dumps(identities, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            denominator_path = "reports/completeness-test/authoritative-denominator.json"
            proof_path = "reports/completeness-test/complete-proof.json"
            readback_path = "reports/completeness-test/consumer-readback.json"
            journal_item = MODULE.single_role(scoped, "promotion_journal_blob_api")
            assert journal_item is not None
            denominator = {
                "schema_version": "datapan.completeness-proof-identities.v1",
                "scope_id": scope["scope_id"], "resource_kind": scope["resource_kind"],
                "source_sha256": source_sha, "identity_algorithm": scope["identity_algorithm"],
                "identity_set_sha256": identity_sha, "authority_owner": scope["identity_owner"],
                "observed_at": observed_at, "identities": identities,
                "reconciliation": {
                    "expected": len(identities), "accepted": len(identities),
                    "missing": 0, "extra": 0, "duplicate": 0, "rejected": 0,
                },
            }
            importlib_module = MODULE.import_module(
                "completeness_proof_validator_for_e2e", temp_root / "scripts/validate-completeness-proof.py",
            )
            importlib_module.POLICY = temp_root / MODULE.POLICY_PATH
            importlib_module.POLICY_SCHEMA = temp_root / "schemas/datapan.completeness-proof-policy.v1.schema.json"
            importlib_module.PROOF_SCHEMA = temp_root / "schemas/datapan.completeness-proof.v1.schema.json"
            publication_manifest = json.loads(MODULE.git_read_only(
                temp_root, ["show", f"{publication['source_sha']}:manifest.json"],
            ))
            publisher_roles = MODULE.stage_input_map(scoped, "publisher")
            pointer = MODULE.object_at(
                resolved[MODULE.single_role(scoped, "publication_pointer_immutable")["input_id"]],
                "immutable publisher pointer",
            )
            publisher_receipt = json.loads(exact_role("publication_receipt"))
            source_script = MODULE.git_read_only(
                temp_root, ["show", f"{publication['source_sha']}:scripts/huggingface_registry_distribution.py"],
            )
            publisher_workflow = MODULE.git_read_only(
                temp_root,
                ["show", f"{publication['workflow_head_sha']}:{MODULE.PIPELINE_WORKFLOWS['publisher']['workflow_path']}"],
            )
            native_inventory = MODULE.validate_native_distribution_inventory(
                manifest=publication_manifest,
                manifest_raw=MODULE.git_read_only(temp_root, ["show", f"{publication['source_sha']}:manifest.json"]),
                pointer=pointer,
                receipt=publisher_receipt,
                source_script_raw=source_script,
                workflow_raw=publisher_workflow,
            )
            release_notes_member = next(
                row for row in native_inventory["verified_artifacts"] if row["path"] == "RELEASE_NOTES.md"
            )
            self.assertEqual(native_inventory["source_manifest_artifacts"], 191)
            self.assertEqual(native_inventory["distribution_artifacts"], 196)
            self.assertEqual(
                (release_notes_member["bytes"], release_notes_member["sha256"]),
                next(
                    (row["bytes"], row["sha256"])
                    for row in publication_manifest["artifacts"] if row["path"] == "RELEASE_NOTES.md"
                ),
            )
            publication_member = next(row for row in publication_manifest["artifacts"] if row["path"] == operation_path)
            native_readback_at = publication["native_verification_completed_at"]
            consumer_readback = {
                "publisher_run_id": publisher_run["id"],
                "publisher_attempt": publisher_run["run_attempt"],
                "generated_at": publisher_run.get("updated_at"),
                "checks": [{"detail": {
                    "path": operation_path, "bytes_streamed": publication_member["bytes"],
                    "sha256": publication_member["sha256"], "revision": publication["payload_revision"],
                }}],
            }
            proof = {
                "schema_version": "datapan.completeness-proof.v1",
                "proof_id": "e2e.data-go-kr-operation-manifest",
                "generated_at": index["evaluation_epoch"],
                "policy": {
                    "id": policy["policy_id"], "schema_version": policy["schema_version"],
                    "sha256": MODULE.sha256_file(temp_root / MODULE.POLICY_PATH),
                },
                "scope": {
                    "scope_id": scope["scope_id"], "source_id": scope["source_id"],
                    "provider": scope["provider"], "resource_kind": scope["resource_kind"],
                    "selector": scope["selector"],
                    "authority": {
                        "state": "available", "owner": scope["evidence_owner"],
                        "evidence": {"path": operation_path, "sha256": source_sha},
                    },
                    "identity_algorithm": scope["identity_algorithm"],
                },
                "source_snapshot": {
                    "observed_at": observed_at, "revision": source_revision, "sha256": source_sha,
                },
                "denominator": {
                    "type": "authoritative_operation_identity_set", "value": len(identities),
                    "bound_source_sha256": source_sha, "identity_algorithm": scope["identity_algorithm"],
                },
                "reconciliation": denominator["reconciliation"],
                "freshness": {
                    "as_of": index["evaluation_epoch"], "evidence_observed_at": observed_at,
                    "max_age_seconds": 2592000, "state": "current",
                },
                "import_durability": {
                    "state": "proven",
                    "receipt": {"path": MODULE.input_path(journal_item), "sha256": journal_item["sha256"]},
                },
                "publication": {
                    "state": "published",
                    "artifact": {"path": operation_path, "sha256": publication_member["sha256"]},
                },
                "consumer_read_back": {
                    "state": "proven", "observed_at": native_readback_at,
                    "artifact_sha256": publication_member["sha256"],
                },
                "proof_state": "complete",
                "claims": {"complete": True, "current": True, "updated": True},
                "missing_evidence": [], "dependencies": [],
            }
            self._write_json_file(temp_root, denominator_path, denominator)
            self._write_json_file(temp_root, proof_path, proof)
            self._write_json_file(temp_root, readback_path, consumer_readback)
            first_commit = self._commit_test_tree(
                temp_root,
                [MODULE.SCOPE_REGISTRY_PATH, denominator_path, proof_path, readback_path],
                base_main, "test: admit exact operation subject through native delivery",
            )

            def test_item(
                input_id: str, role: str, artifact_type: str, path: str,
                content: bytes, *, namespace: str = "live_operational",
                root_label: str = "repository",
                observed: str | None = None, producer_revision: str | None = None,
                subject: dict[str, object] | None = None, producer: dict[str, object] | None = None,
            ) -> dict[str, object]:
                return {
                    "input_id": input_id, "scope_id": scope["scope_id"], "role": role,
                    "artifact_type": artifact_type, "root": root_label, "path": path,
                    "bytes": len(content), "sha256": MODULE.sha256_bytes(content),
                    "namespace": namespace,
                    "producer": producer or {
                        "repository": "StatPan/datapan-registry", "identity": f"synthetic test {role}",
                        "revision": producer_revision or first_commit,
                    },
                    "observed_at": observed,
                    **({"subject": subject} if subject is not None else {}),
                }

            denominator_raw = (temp_root / denominator_path).read_bytes()
            proof_raw = (temp_root / proof_path).read_bytes()
            readback_raw = (temp_root / readback_path).read_bytes()
            input_index["inputs"].extend([
                test_item(
                    "e2e-authoritative-source", "authoritative_source_snapshot", "source_snapshot",
                    operation_path, source_bytes, root_label="evidence", observed=observed_at,
                    producer_revision=source_revision,
                    subject={
                        "scope_id": scope["scope_id"], "source_id": scope["source_id"],
                        "resource_kind": scope["resource_kind"], "identity_algorithm": scope["identity_algorithm"],
                        "source_sha256": source_sha, "source_revision": source_revision,
                        "admission_revision": source_revision,
                    },
                ),
                test_item(
                    "e2e-authoritative-denominator", "authoritative_denominator", "authority_denominator",
                    denominator_path, denominator_raw, observed=observed_at,
                    producer_revision=first_commit,
                    subject={
                        "scope_id": scope["scope_id"], "source_id": scope["source_id"],
                        "resource_kind": scope["resource_kind"], "identity_algorithm": scope["identity_algorithm"],
                        "source_sha256": source_sha, "identity_set_sha256": identity_sha,
                        "admission_revision": first_commit,
                    },
                ),
                test_item(
                    "e2e-proof", "proof_v1", "proof_v1", proof_path, proof_raw,
                    producer_revision=first_commit,
                    subject={"admission_revision": first_commit},
                ),
                test_item(
                    "e2e-consumer-readback", "consumer_readback", "consumer_readback",
                    readback_path, readback_raw, observed=consumer_readback["generated_at"],
                    producer={
                        "repository": "StatPan/datapan-registry", "identity": "corroborating native verifier tuple",
                        "revision": publisher_run["head_sha"], "run_id": str(publisher_run["id"]),
                        "attempt": publisher_run["run_attempt"], "workflow_id": publisher_run["workflow_id"],
                        "workflow_path": MODULE.PIPELINE_WORKFLOWS["publisher"]["workflow_path"],
                        "event": publisher_run["event"],
                    },
                    subject={"stage": "publisher"},
                ),
            ])
            index_path.write_text(json.dumps(input_index, ensure_ascii=False, indent=2) + "\n")

            report = MODULE.build_report(
                root=temp_root, input_root=source_input_root, input_index_path=index_path,
            )
            row = next(item for item in report["scopes"] if item["scope_id"] == scope["scope_id"])
            self.assertEqual(row["evaluated_claims"], {"complete": True, "current": True, "updated": True})
            delivery = next(facet for facet in row["facets"] if facet["facet_id"] == "immutable_publication_read_back")
            self.assertEqual(delivery["state"], "historical")
            self.assertEqual(delivery["details"]["subject"]["artifact_path"], operation_path)

            # Candidate inventory is rebuilt from the immutable base index;
            # its local Registry bytes cannot borrow the retained A/B/C/Health
            # history as proof for a different candidate subject.
            canonical_index_path = temp_root / MODULE.INPUT_INDEX_PATH
            canonical_index_path.parent.mkdir(parents=True, exist_ok=True)
            # Retain the admitted proof in the immutable baseline index so
            # candidate evaluation must validate its historical subject before
            # suppressing applicability for the changed local inventory.
            candidate_baseline_index = copy.deepcopy(input_index)
            canonical_index_path.write_text(json.dumps(candidate_baseline_index, ensure_ascii=False, indent=2) + "\n")
            baseline_main = self._commit_test_tree(
                temp_root,
                [
                    MODULE.INPUT_INDEX_PATH, MODULE.SCOPE_REGISTRY_PATH, MODULE.POLICY_PATH,
                    MODULE.INPUT_SCHEMA_PATH, MODULE.SCOPE_SCHEMA_PATH,
                    "schemas/datapan.completeness-proof-identities.v1.schema.json",
                    MODULE.ROLLUP_SCHEMA_PATH, "schemas/index.json", "manifest.json",
                    "reports/completeness-proof-rollup.json", "reports/completeness-proof-rollup.md",
                ],
                first_commit,
                "test: pin completeness baseline and retained delivery packet",
            )
            candidate_registry = temp_root / "data/data-go-kr.registry.json"
            baseline_registry_bytes = candidate_registry.read_bytes()
            baseline_registry_path = temp_root / ".test-baseline.registry.json"
            baseline_registry_path.write_bytes(baseline_registry_bytes)
            candidate_operation_manifest = temp_root / operation_path
            baseline_operation_manifest_bytes = candidate_operation_manifest.read_bytes()
            candidate_registry_copy = temp_root / "data/.candidate-registry.tmp"
            with candidate_registry.open("rb") as source, candidate_registry_copy.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.write(b"\n")
            candidate_registry.unlink()
            candidate_registry_copy.replace(candidate_registry)
            generator = MODULE.import_module(
                "candidate_operation_manifest_generator_for_e2e",
                temp_root / "scripts/generate-data-go-kr-operation-manifest.py",
            )
            prior_registry_path = generator.REGISTRY
            generator.REGISTRY = candidate_registry
            try:
                candidate_manifest = generator.build(json.loads(candidate_registry.read_bytes()))
                candidate_manifest_bytes = generator.render(candidate_manifest)
            finally:
                generator.REGISTRY = prior_registry_path
            candidate_operation_manifest.unlink()
            candidate_operation_manifest.write_bytes(candidate_manifest_bytes)
            candidate_args = [
                "--repo-root", str(temp_root), "--input-root", str(source_input_root),
                "--candidate-registry", str(candidate_registry),
                "--candidate-operation-manifest", str(candidate_operation_manifest),
                "--candidate-baseline-registry", str(baseline_registry_path),
            ]
            self.assertEqual(MODULE.main([*candidate_args, "--write"]), 0)
            candidate_report = json.loads((temp_root / MODULE.OUTPUT_JSON_PATH).read_bytes())
            candidate_operations = next(row for row in candidate_report["scopes"] if row["scope_id"] == scope["scope_id"])
            self.assertEqual(candidate_operations["evaluated_claims"], {"complete": False, "current": False, "updated": False})
            self.assertIsNotNone(candidate_operations["proof"])
            self.assertEqual(candidate_operations["proof"]["claims"], {"complete": True, "current": True, "updated": True})
            self.assertEqual(candidate_operations["proof_state"], "blocked")
            self.assertEqual(candidate_report["evaluation_context"]["mode"], "candidate_evidence_snapshot")
            candidate_facets = {facet["facet_id"]: facet for facet in candidate_operations["facets"]}
            self.assertEqual(candidate_facets["candidate_inventory"]["state"], "blocked")
            self.assertTrue(candidate_facets["candidate_inventory"]["details"]["historical_proof_validated"])
            self.assertTrue(candidate_facets["candidate_inventory"]["details"]["historical_claims_suppressed"])
            self.assertEqual(candidate_facets["specification_pipeline"]["state"], "historical")
            self.assertNotIn(
                "current_release_applicable",
                candidate_facets["specification_pipeline"]["details"],
            )
            self.assertEqual(candidate_facets["immutable_publication_read_back"]["state"], "historical")
            candidate_publication = candidate_facets["immutable_publication_read_back"]["details"]
            self.assertNotIn("current_release_manifest_sha256", candidate_publication)
            self.assertNotIn("current_release_subject_applicable", candidate_publication)
            self.assertNotIn("current_release_applicable", candidate_publication)
            self.assertTrue(candidate_publication["candidate_release_publication_unproven"])
            rebuilt_candidate_index = json.loads(canonical_index_path.read_bytes())
            _candidate_context, changed_scopes = MODULE.validate_candidate_input_index(
                root=temp_root, index=rebuilt_candidate_index,
                input_index_path=canonical_index_path,
            )
            catalog_scope = next(
                item for item in scopes_by_id.values()
                if item["source_id"] == "data_go_kr" and item["resource_kind"] == "api_catalog_metadata"
            )
            self.assertIn(catalog_scope["scope_id"], changed_scopes)
            self.assertIn(scope["scope_id"], changed_scopes)

            watched_paths = [canonical_index_path, temp_root / MODULE.OUTPUT_JSON_PATH, temp_root / MODULE.OUTPUT_MARKDOWN_PATH]
            watched_before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in watched_paths]
            self.assertEqual(MODULE.main([*candidate_args, "--check"]), 0)
            self.assertEqual(watched_before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in watched_paths])

            # A candidate delta must not short-circuit validation of an
            # admitted baseline proof. A malformed but byte-bound proof in the
            # pinned baseline is a hard failure, with outputs left untouched.
            valid_candidate_index_bytes = canonical_index_path.read_bytes()
            valid_proof_bytes = (temp_root / proof_path).read_bytes()
            invalid_proof = copy.deepcopy(proof)
            invalid_proof["claims"]["current"] = "true"
            self._write_json_file(temp_root, proof_path, invalid_proof)
            bad_proof_commit = self._commit_test_tree(
                temp_root, [proof_path], baseline_main,
                "test: pin a malformed baseline proof for candidate validation",
            )
            bad_baseline_index = copy.deepcopy(candidate_baseline_index)
            bad_proof_item = next(item for item in bad_baseline_index["inputs"] if item["input_id"] == "e2e-proof")
            bad_proof_item["bytes"] = (temp_root / proof_path).stat().st_size
            bad_proof_item["sha256"] = MODULE.sha256_file(temp_root / proof_path)
            bad_proof_item["producer"]["revision"] = bad_proof_commit
            bad_proof_item["subject"]["admission_revision"] = bad_proof_commit
            self._write_json_file(temp_root, MODULE.INPUT_INDEX_PATH, bad_baseline_index)
            bad_index_commit = self._commit_test_tree(
                temp_root, [MODULE.INPUT_INDEX_PATH], bad_proof_commit,
                "test: bind malformed proof bytes into candidate baseline index",
            )
            self.assertEqual(bad_index_commit, subprocess.check_output(
                ["git", "rev-parse", "refs/remotes/origin/main"], cwd=temp_root, text=True,
            ).strip())
            unchanged_before_bad_candidate = [
                (path.read_bytes(), path.stat().st_mtime_ns) for path in watched_paths
            ]
            self.assertEqual(MODULE.main([*candidate_args, "--write"]), 1)
            self.assertEqual(
                unchanged_before_bad_candidate,
                [(path.read_bytes(), path.stat().st_mtime_ns) for path in watched_paths],
            )
            # Restore the valid baseline so later independent subject-tamper
            # controls continue from the same history.
            (temp_root / proof_path).write_bytes(valid_proof_bytes)
            canonical_index_path.write_bytes(valid_candidate_index_bytes)
            subprocess.run(["git", "update-ref", "refs/heads/main", baseline_main], cwd=temp_root, check=True)
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", baseline_main], cwd=temp_root, check=True)

            candidate_index = json.loads(canonical_index_path.read_bytes())
            tampered_candidate_index = copy.deepcopy(candidate_index)
            retained_run = next(item for item in tampered_candidate_index["inputs"] if item["role"] == "pipeline_run")
            retained_run["producer"]["revision"] = baseline_main
            with self.assertRaisesRegex(ValueError, "changed evidence beyond registered local inventory rows"):
                MODULE.validate_candidate_input_index(
                    root=temp_root, index=tampered_candidate_index,
                    input_index_path=canonical_index_path,
                )

            policy_path = temp_root / MODULE.POLICY_PATH
            policy_raw = policy_path.read_bytes()
            policy_path.unlink()
            policy_path.write_bytes(policy_raw + b"\n")
            with self.assertRaisesRegex(ValueError, "cannot change completeness scope registration or policy"):
                MODULE.input_index_for_candidate(
                    root=temp_root, baseline_main_sha=baseline_main,
                    candidate_registry=candidate_registry,
                    candidate_operation_manifest=candidate_operation_manifest,
                )
            policy_path.unlink()
            policy_path.write_bytes(policy_raw)
            candidate_registry.unlink()
            candidate_registry.write_bytes(baseline_registry_bytes)
            candidate_operation_manifest.unlink()
            candidate_operation_manifest.write_bytes(baseline_operation_manifest_bytes)

            proof["publication"]["artifact"] = {
                "path": "RELEASE_NOTES.md",
                "sha256": next(row["sha256"] for row in publication_manifest["artifacts"] if row["path"] == "RELEASE_NOTES.md"),
            }
            proof["consumer_read_back"]["artifact_sha256"] = proof["publication"]["artifact"]["sha256"]
            release_notes = next(row for row in publication_manifest["artifacts"] if row["path"] == "RELEASE_NOTES.md")
            consumer_readback["checks"] = [{"detail": {
                "path": "RELEASE_NOTES.md", "bytes_streamed": release_notes["bytes"],
                "sha256": release_notes["sha256"], "revision": publication["payload_revision"],
            }}]
            self._write_json_file(temp_root, proof_path, proof)
            self._write_json_file(temp_root, readback_path, consumer_readback)
            second_commit = self._commit_test_tree(
                temp_root, [proof_path, readback_path], first_commit,
                "test: reject a different verified release member",
            )
            for item in input_index["inputs"]:
                if item["input_id"] == "e2e-proof":
                    item["bytes"] = (temp_root / proof_path).stat().st_size
                    item["sha256"] = MODULE.sha256_file(temp_root / proof_path)
                    item["producer"]["revision"] = second_commit
                    item["subject"]["admission_revision"] = second_commit
                elif item["input_id"] == "e2e-consumer-readback":
                    item["bytes"] = (temp_root / readback_path).stat().st_size
                    item["sha256"] = MODULE.sha256_file(temp_root / readback_path)
                    item["observed_at"] = consumer_readback["generated_at"]
            index_path.write_text(json.dumps(input_index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "no registered semantic subject adapter"):
                MODULE.build_report(root=temp_root, input_root=source_input_root, input_index_path=index_path)

        assert donor_snapshot is not None
        self._assert_native_borrowed_donor_unchanged(ROOT, donor_snapshot)

    def test_retained_633_import_receipt_matches_admitted_counts(self) -> None:
        admission = json.loads((ROOT / "reports/runtime-freshness-import-admissions/35798122454.json").read_text())
        receipt = json.loads((ROOT / "reports/runtime-freshness-imports/35798122454.json").read_text())

        MODULE.validate_import_receipt_arithmetic(admission=admission, receipt=receipt, source_id="data_go_kr")

    def test_import_receipt_status_count_drift_is_rejected(self) -> None:
        admission = json.loads((ROOT / "reports/runtime-freshness-import-admissions/35798122454.json").read_text())
        receipt = json.loads((ROOT / "reports/runtime-freshness-imports/35798122454.json").read_text())
        corrupted = copy.deepcopy(receipt)
        corrupted["results"][0]["status"] = "verified"

        with self.assertRaisesRegex(ValueError, "status counts differ"):
            MODULE.validate_import_receipt_arithmetic(admission=admission, receipt=corrupted, source_id="data_go_kr")

    def test_historical_633_delivery_does_not_promote_current_or_updated_claims(self) -> None:
        report = MODULE.build_report(
            root=ROOT,
            input_root=ROOT,
            input_index_path=ROOT / MODULE.INPUT_INDEX_PATH,
        )
        operations = next(row for row in report["scopes"] if row["scope_id"] == "data-go-kr.api-operations")
        import_facet = next(facet for facet in operations["facets"] if facet["facet_id"] == "import_durability")

        self.assertEqual(import_facet["state"], "historical")
        self.assertFalse(import_facet["details"]["evidence_subject_applicable"])
        self.assertIsNone(operations["proof"])
        self.assertEqual(operations["evaluated_claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(report["summary"]["registered_scopes"], 9)
        self.assertEqual(report["input_index"]["path"], "canonical-semantic-json:reports/completeness-proof-inputs.json")
        facets = {facet["facet_id"]: facet for facet in operations["facets"]}
        self.assertEqual(facets["specification_pipeline"]["state"], "historical")
        self.assertEqual(facets["immutable_publication_read_back"]["state"], "historical")
        self.assertTrue(facets["immutable_publication_read_back"]["details"]["payload_equivalent_to_registered_catalog_inventory"])
        self.assertEqual(facets["immutable_publication_read_back"]["state"], "historical")
        self.assertTrue(operations["release_relation"]["current_release_evaluation_required"])
        self.assertTrue(facets["source_observation"]["details"]["observation_authenticated"])
        self.assertNotIn("new_post_repair_observation", facets["source_observation"]["details"])
        self.assertEqual(
            facets["source_observation"]["missing_evidence"][0]["code"],
            "post_repair_observation_baseline_unbound",
        )
        self.assertEqual(
            facets["health_observation"]["missing_evidence"][0]["code"],
            "health_receipt_does_not_establish_additional_source_observations",
        )
        markdown = MODULE.render_markdown(report).decode("utf-8")
        self.assertIn("## Evidence facets", markdown)
        self.assertIn("Input index semantic identity: `canonical-semantic-json:", markdown)
        self.assertIn("immutable_publication_read_back", markdown)

    def test_retained_pipeline_chain_binds_exact_attempts_and_current_state(self) -> None:
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index, checked, resolved = MODULE.validate_input_index(
            root=ROOT, input_root=ROOT, index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            scope_by_id=scope_by_id,
        )
        scoped = [item for item in checked if item["scope_id"] == "data-go-kr.api-operations"]
        result = MODULE.validate_pipeline_evidence(
            root=ROOT, operation_inputs=scoped, all_inputs=checked,
            resolved=resolved, evaluation_epoch=index["evaluation_epoch"],
            scope_registry=scope_registry,
        )
        self.assertEqual(result["status"], "verified_historical_chain")
        self.assertEqual(result["source"]["run_id"], "36646768289")
        self.assertEqual(result["processor"]["run_id"], "37204681028")
        self.assertEqual(result["processor"]["processor_protocol"], "historical-eight-output-v1")
        self.assertFalse(result["processor"]["current_input_contract_compatible"])
        self.assertEqual(
            result["processor"]["current_input_contract_reason_code"],
            "historical_eight_output_protocol_not_current_eligible",
        )
        self.assertEqual(result["promotion"]["run_id"], "37205271032")
        self.assertEqual(result["health"]["run_id"], "37205341388")
        self.assertEqual(result["health"]["observation_count"], 1)
        self.assertEqual(result["health"]["last_good_source_sha"], "6a5138c792f4b7402da0c5ab439646bd752a307f")
        self.assertTrue(result["promotion"]["processor_generation_matches"])
        self.assertEqual(result["publication"]["status"], "verified")
        self.assertFalse(result["publication"]["details"]["currentness_established"])
        self.assertIn("main_ancestry_checked_tip", result["promotion"])
        self.assertNotIn("main_sha", result["promotion"])

        promotion_roles = MODULE.stage_input_map(scoped, "promotion")
        promotion_run, promotion_job, _started, _completed = MODULE.validate_stage_run(
            stage="promotion", roles=promotion_roles, resolved=resolved,
            evaluation_epoch=index["evaluation_epoch"], root=ROOT,
        )
        wrong_b_size = copy.deepcopy(result["processor"])
        wrong_b_size["candidate_bytes"] += 1
        with self.assertRaisesRegex(ValueError, "exact B candidate, byte count, and outcome"):
            MODULE.validate_promotion_stage(
                root=ROOT, roles=promotion_roles, resolved=resolved,
                run=promotion_run, job=promotion_job, processor=wrong_b_size,
                current_registry=result["current_registry"], evaluation_epoch=index["evaluation_epoch"],
            )

        # Publication/ACK is a separate facet. Removing both must not discard
        # the already complete and authenticated A/B/C/Health chain.
        without_publication = [
            item for item in scoped
            if item.get("subject", {}).get("stage") not in {"publisher", "acknowledgement"}
            and not item["role"].startswith(("publication_", "acknowledgement_"))
        ]
        partial = MODULE.validate_pipeline_evidence(
            root=ROOT, operation_inputs=without_publication, all_inputs=checked,
            resolved=resolved, evaluation_epoch=index["evaluation_epoch"],
            scope_registry=scope_registry,
        )
        self.assertEqual(partial["status"], "verified_historical_chain")
        self.assertEqual(partial["source"]["run_id"], result["source"]["run_id"])
        self.assertEqual(partial["processor"]["generation_id"], result["processor"]["generation_id"])
        self.assertEqual(partial["promotion"]["run_id"], result["promotion"]["run_id"])
        self.assertEqual(partial["health"]["run_id"], result["health"]["run_id"])
        self.assertIsNone(partial["publication"])
        self.assertEqual(partial["publication_missing_stages"], ["publisher", "acknowledgement"])
        historical_import = MODULE.validate_historical_import(
            root=ROOT, inputs=without_publication, resolved=resolved,
            scope=scope_by_id["data-go-kr.api-operations"],
        )
        operation_manifest_item = next(
            item for item in without_publication if item["role"] == "local_operation_manifest"
        )
        facets = MODULE.scope_facets(
            scope=scope_by_id["data-go-kr.api-operations"], scoped_inputs=without_publication,
            resolved=resolved, current_manifest_sha256=operation_manifest_item["sha256"],
            historical_import=historical_import, pipeline_evidence=partial,
            updated_delivery=None,
        )
        by_facet = {item["facet_id"]: item for item in facets}
        self.assertEqual(by_facet["specification_pipeline"]["state"], "historical")
        self.assertEqual(
            by_facet["specification_pipeline"]["details"]["processor_protocol"],
            "historical-eight-output-v1",
        )
        self.assertEqual(
            by_facet["specification_pipeline"]["details"]["current_input_contract_reason_code"],
            "historical_eight_output_protocol_not_current_eligible",
        )
        self.assertEqual(by_facet["source_observation"]["state"], "historical")
        self.assertEqual(by_facet["health_observation"]["state"], "historical")
        self.assertEqual(by_facet["immutable_publication_read_back"]["state"], "missing")
        self.assertEqual(
            by_facet["immutable_publication_read_back"]["missing_evidence"][0]["code"],
            "same_subject_publication_read_back_missing",
        )

        incomplete_ack = [
            item for item in scoped
            if not (item.get("subject", {}).get("stage") == "acknowledgement" and item["role"] == "acknowledgement_jobs")
        ]
        with self.assertRaisesRegex(ValueError, "pipeline stage acknowledgement is incomplete"):
            MODULE.validate_pipeline_evidence(
                root=ROOT, operation_inputs=incomplete_ack, all_inputs=checked,
                resolved=resolved, evaluation_epoch=index["evaluation_epoch"],
                scope_registry=scope_registry,
            )

        # A later native attempt must route through the parameterized #716
        # validator, never through the packet-specific historical adapter.
        original_stage_validator = MODULE.validate_stage_run

        def future_attempt_stage_validator(**kwargs: object):
            result = original_stage_validator(**kwargs)  # type: ignore[arg-type]
            if kwargs["stage"] == "publisher":
                run, job, started, completed = result
                future_run = copy.deepcopy(run)
                future_run["id"] = 99999999999
                return future_run, job, started, completed
            return result

        future_native_attempt = {"run_id": "99999999999", "attempt": 1, "source_sha": "a" * 40}
        future_inputs_without_ack = [
            item for item in scoped
            if item.get("subject", {}).get("stage") != "acknowledgement"
            and not item["role"].startswith("acknowledgement_")
        ]
        original_import_module = MODULE.import_module

        def reject_legacy_adapter(name: str, path: pathlib.Path):
            loaded = original_import_module(name, path)
            if name == "completeness_publication_evidence":
                loaded.validate_historical_publication_facet = mock.Mock(
                    side_effect=AssertionError("future attempt entered the legacy historical adapter")
                )
            return loaded

        with (
            mock.patch.object(MODULE, "validate_stage_run", side_effect=future_attempt_stage_validator),
            mock.patch.object(MODULE, "validate_native_publisher_attempt", return_value=future_native_attempt),
            mock.patch.object(MODULE, "validate_native_publication_readback", return_value={"future": True}),
            mock.patch.object(MODULE, "import_module", side_effect=reject_legacy_adapter),
        ):
            future = MODULE.validate_pipeline_evidence(
                root=ROOT, operation_inputs=future_inputs_without_ack, all_inputs=checked,
                resolved=resolved, evaluation_epoch=index["evaluation_epoch"],
                scope_registry=scope_registry,
            )
        self.assertEqual(future["status"], "verified_historical_chain")
        self.assertIsNone(future["publication"])
        self.assertEqual(future["publisher_attempt"], future_native_attempt)

    def test_actual_retained_historical_processor_classifier_rejects_archive_source_and_profile_tampering(self) -> None:
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index, checked, resolved = MODULE.validate_input_index(
            root=ROOT, input_root=ROOT, index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            scope_by_id=scope_by_id,
        )
        del scope_registry, index
        scoped = [item for item in checked if item["scope_id"] == "data-go-kr.api-operations"]
        roles = MODULE.stage_input_map(scoped, "processor")
        run = MODULE.object_at(resolved[roles["pipeline_run"]["input_id"]], "processor run")
        inventory = MODULE.object_at(
            resolved[roles["pipeline_artifact_metadata"]["input_id"]],
            "processor artifact inventory",
        )
        archive = MODULE.read_bytes(resolved[roles["pipeline_artifact_archive"]["input_id"]])
        generation_api = MODULE.indexed_json(roles, resolved, "processor_generation_api")
        checkpoint = json.loads(MODULE.base64.b64decode(generation_api["content"]))
        promotion = MODULE.import_module(
            "test_actual_historical_processor_classifier",
            ROOT / "scripts/run-canonical-update-promotion.py",
        )
        cache_source = MODULE.import_module(
            "test_actual_historical_cache_source_classifier",
            ROOT / "scripts/upstream_catalogue_cache_source.py",
        )

        def local_reader():
            deadline = time.monotonic() + cache_source.MAX_WHOLE_ATTEMPT_SECONDS
            return deadline, cache_source.LocalGitSourceReader(
                cwd=ROOT, deadline_monotonic=deadline,
            )

        deadline, reader = local_reader()
        contract = promotion.classify_retained_processor_bundle_for_report(
            checkpoint, run, inventory, archive,
            repository="StatPan/datapan-registry", default_branch="main",
            cache_source_helper=cache_source, source_reader=reader,
            deadline_monotonic=deadline,
        )
        self.assertEqual(contract.protocol, promotion.HISTORICAL_RETAINED_PROCESSOR_PROTOCOL)
        self.assertEqual(
            contract.expected_members,
            (*promotion.HISTORICAL_RETAINED_PROCESSOR_FILES,
             promotion.RETAINED_PROCESSOR_CHECKPOINT_MEMBER),
        )
        with self.assertRaisesRegex(promotion.PromotionError, "no modern cache capability"):
            _ = contract.cache_source_contract

        tampered_archive = bytearray(archive)
        tampered_archive[-1] ^= 1
        deadline, reader = local_reader()
        with self.assertRaisesRegex(promotion.PromotionError, "archive differs"):
            promotion.classify_retained_processor_bundle_for_report(
                checkpoint, run, inventory, bytes(tampered_archive),
                repository="StatPan/datapan-registry", default_branch="main",
                cache_source_helper=cache_source, source_reader=reader,
                deadline_monotonic=deadline,
            )

        current_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{commit}"], cwd=ROOT, text=True,
        ).strip()
        forged_run = copy.deepcopy(run)
        forged_inventory = copy.deepcopy(inventory)
        forged_run["head_sha"] = current_head
        forged_inventory["artifacts"][0]["workflow_run"]["head_sha"] = current_head
        deadline, reader = local_reader()
        with self.assertRaisesRegex(promotion.PromotionError, "source identity differs"):
            promotion.classify_retained_processor_bundle_for_report(
                checkpoint, forged_run, forged_inventory, archive,
                repository="StatPan/datapan-registry", default_branch="main",
                cache_source_helper=cache_source, source_reader=reader,
                deadline_monotonic=deadline,
            )

        stripped_modern = copy.deepcopy(checkpoint)
        stripped_modern["generation_inputs"]["cache_source_profile"] = cache_source.CACHE_SOURCE_PROFILE
        stripped_modern["generation_id"] = MODULE.sha256_bytes(
            promotion.canonical_json(stripped_modern["generation_inputs"]),
        )
        stripped_modern.pop("checkpoint_sha256", None)
        stripped_modern["checkpoint_sha256"] = MODULE.sha256_bytes(
            promotion.canonical_json(stripped_modern),
        )
        deadline, reader = local_reader()
        with self.assertRaisesRegex(promotion.PromotionError, "modern processor protocol is unauthenticated"):
            promotion.classify_retained_processor_bundle_for_report(
                stripped_modern, run, inventory, archive,
                repository="StatPan/datapan-registry", default_branch="main",
                cache_source_helper=cache_source, source_reader=reader,
                deadline_monotonic=deadline,
            )

    def test_source_refresh_result_is_derived_from_policy_for_no_change(self) -> None:
        source_config = {
            "source_id": "data_go_kr", "owner": "release-operator",
            "diff": {"material_change_fields": ["added", "removed", "changed"]},
            "publication": {"required_gates": ["release_manifest_verification", "release_readiness", "consumer_compatibility"]},
        }
        summary = {"added": 0, "removed": 0, "changed": 0, "stable": 120}
        diff = {
            "generated_at": "2026-10-04T01:02:03Z", "old": "data/data-go-kr.registry.json",
            "new": ".datapan/ci/upstream-refresh/candidate.registry.json", "summary": summary,
        }
        snapshot = {
            "path": diff["new"], "bytes": 123, "sha256": "a" * 64, "records": 120,
        }
        evidence = {
            "source_id": "data_go_kr", "owner": "release-operator", "status": "no_change",
            "collection": {"attempted": True, "succeeded": True, "exit_code": 0, "error_class": None},
            "snapshot": snapshot, "diff": {"path": ".datapan/ci/upstream-refresh/catalog-diff.json"},
            "review": {"action": "none"},
            "publication": {
                "automatic": False, "release_allowed": False,
                "required_gates": source_config["publication"]["required_gates"],
            },
        }
        packet = {
            "source_id": "data_go_kr", "owner": "release-operator", "status": "no_change",
            "action": "none", "observed_at": diff["generated_at"], "work_key": "",
            "automatic_publication": False, "snapshot": snapshot["path"], "diff": evidence["diff"]["path"],
        }
        payload = {
            "source_id": "data_go_kr", "status": "no_change", "summary": summary,
            "error_class": None,
        }
        packet["work_key"] = "upstream-refresh:data_go_kr:" + __import__("hashlib").sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        evidence["review"]["work_key"] = packet["work_key"]
        status, _ = MODULE.source_refresh_result_contract(
            source_config=source_config, evidence=evidence, diff=diff, work_packet=packet,
        )
        self.assertEqual(status, "no_change")
        self.assertNotEqual(snapshot["sha256"], "b" * 64)

        evidence["collection"]["exit_code"] = 1
        with self.assertRaisesRegex(ValueError, "producer-owned refresh policy"):
            MODULE.source_refresh_result_contract(
                source_config=source_config, evidence=evidence, diff=diff, work_packet=packet,
            )
        evidence["collection"]["exit_code"] = 0

        material = copy.deepcopy(summary)
        material["added"] = 1
        diff["summary"] = material
        payload["status"] = "material_change"
        payload["summary"] = material
        work_key = "upstream-refresh:data_go_kr:" + __import__("hashlib").sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        packet.update({"status": "material_change", "action": "review_catalog_drift", "work_key": work_key})
        evidence.update({"status": "material_change"})
        evidence["review"].update({"action": "review_catalog_drift", "work_key": work_key})
        self.assertEqual(
            MODULE.source_refresh_result_contract(
                source_config=source_config, evidence=evidence, diff=diff, work_packet=packet,
            )[0],
            "material_change",
        )

    def test_build_report_admits_policy_consistent_no_change_observation_without_peers(self) -> None:
        """Exercise A's real archive/schema/policy adapter through the indexed caller."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_id
            or (
                item.get("subject", {}).get("stage") == "source"
                or not (
                    item["role"].startswith("publication_")
                    or item["role"].startswith("acknowledgement_")
                    or item["role"] == "consumer_readback"
                    or item.get("subject", {}).get("stage") in {"processor", "promotion", "health", "publisher", "acknowledgement"}
                )
            )
        ]
        source_archive_item = next(
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id
            and item.get("subject", {}).get("stage") == "source"
            and item["role"] == "pipeline_artifact_archive"
        )
        source_metadata_item = next(
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id
            and item.get("subject", {}).get("stage") == "source"
            and item["role"] == "pipeline_artifact_metadata"
        )
        with zipfile.ZipFile(ROOT / source_archive_item["path"]) as original:
            members = {name: original.read(name) for name in original.namelist()}
        evidence = json.loads(members["upstream-refresh-evidence.json"])
        diff = json.loads(members["catalog-diff.json"])
        work_packet = json.loads(members["upstream-refresh-work-packet.json"])
        diff["summary"] = {"added": 0, "removed": 0, "changed": 0, "stable": 10}
        diff_raw = (json.dumps(diff, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        evidence["status"] = "no_change"
        evidence["diff"]["summary"] = diff["summary"]
        evidence["diff"]["sha256"] = MODULE.sha256_bytes(diff_raw)
        payload = {
            "source_id": "data_go_kr", "status": "no_change",
            "summary": diff["summary"], "error_class": None,
        }
        work_key = "upstream-refresh:data_go_kr:" + MODULE.hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        evidence["review"].update({"action": "none", "work_key": work_key})
        work_packet.update({"status": "no_change", "action": "none", "work_key": work_key})
        members["catalog-diff.json"] = diff_raw
        members["upstream-refresh-evidence.json"] = (
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        members["upstream-refresh-work-packet.json"] = (
            json.dumps(work_packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()

        archive_output = io.BytesIO()
        with zipfile.ZipFile(archive_output, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for name, raw in members.items():
                output.writestr(name, raw)
        archive_raw = archive_output.getvalue()

        with tempfile.TemporaryDirectory(prefix="completeness-no-change-indexed-") as name:
            input_root = pathlib.Path(name)
            self._write_rebound_evidence(input_root, source_archive_item, "source-no-change.zip", archive_raw)
            metadata = json.loads((ROOT / source_metadata_item["path"]).read_bytes())
            metadata["artifacts"][0]["size_in_bytes"] = len(archive_raw)
            metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(archive_raw)}"
            metadata_raw = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            self._write_rebound_evidence(input_root, source_metadata_item, "source-no-change-metadata.json", metadata_raw)
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")

            report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)
            row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
            pipeline = next(item for item in row["facets"] if item["facet_id"] == "specification_pipeline")
            self.assertEqual(report["evaluation_context"]["mode"], "repository_evidence_snapshot")
            self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
            self.assertEqual(pipeline["state"], "missing")
            self.assertEqual(set(pipeline["details"]["missing_stages"]), {"processor", "promotion", "health"})
            self.assertTrue(any(item["path"].endswith("source-no-change.zip") for item in pipeline["evidence"]))

            # Keep the index/archive/API outer hashes internally consistent, but
            # relabel a policy-material diff as no_change. The owning A adapter
            # must reject it before any report can be written.
            diff["summary"]["added"] = 1
            changed_diff_raw = (json.dumps(diff, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            members["catalog-diff.json"] = changed_diff_raw
            evidence["diff"]["summary"] = diff["summary"]
            evidence["diff"]["sha256"] = MODULE.sha256_bytes(changed_diff_raw)
            members["upstream-refresh-evidence.json"] = (
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode()
            changed_archive_output = io.BytesIO()
            with zipfile.ZipFile(changed_archive_output, "w", compression=zipfile.ZIP_DEFLATED) as output:
                for member_name, raw in members.items():
                    output.writestr(member_name, raw)
            changed_archive = changed_archive_output.getvalue()
            self._write_rebound_evidence(input_root, source_archive_item, "source-no-change.zip", changed_archive)
            metadata["artifacts"][0]["size_in_bytes"] = len(changed_archive)
            metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(changed_archive)}"
            changed_metadata_raw = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            self._write_rebound_evidence(input_root, source_metadata_item, "source-no-change-metadata.json", changed_metadata_raw)
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "producer-owned refresh policy"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_build_report_replays_resealed_health_last_good_choices_without_claim_credit(self) -> None:
        """Exercise null, older, and newer Health publication choices through indexed build_report."""
        registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        expected_run_ids = {"null": None, "older": 37199709258, "newer": 37199709259}
        for variant, expected_run_id in expected_run_ids.items():
            with self.subTest(last_good=variant), tempfile.TemporaryDirectory(prefix=f"completeness-health-{variant}-") as name:
                input_root = pathlib.Path(name)
                index_path = self._build_health_last_good_variant(variant=variant, input_root=input_root)
                report = MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )
                row = next(item for item in report["scopes"] if item["scope_id"] == operation_scope["scope_id"])
                self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
                pipeline = next(item for item in row["facets"] if item["facet_id"] == "specification_pipeline")
                self.assertEqual(pipeline["state"], "missing")
                self.assertEqual(set(pipeline["details"]["missing_stages"]), {"source", "processor", "promotion"})

                # Inspect the same packet with the stage-local production
                # validator after the full caller has accepted the conservative
                # partial result. This asserts the actual persister replay,
                # seals, ref/tree membership, and selected last-good identity.
                _, checked, resolved = MODULE.validate_input_index(
                    root=ROOT, input_root=input_root, index_path=index_path,
                    scope_by_id=scope_by_id,
                )
                scoped = [item for item in checked if item["scope_id"] == operation_scope["scope_id"]]
                health_roles = MODULE.stage_input_map(scoped, "health")
                health_run, health_job, _started, _completed = MODULE.validate_stage_run(
                    stage="health", roles=health_roles, resolved=resolved,
                    evaluation_epoch=report["evaluation_epoch"], root=ROOT,
                )
                local = MODULE.validate_health_stage_local(
                    root=ROOT, roles=health_roles, resolved=resolved, run=health_run,
                    job=health_job, evaluation_epoch=report["evaluation_epoch"],
                )
                selected = local["post_state"]["last_good_by_source"].get("data_go_kr")
                self.assertEqual(selected.get("publication_run_id") if selected else None, expected_run_id)
                self.assertTrue(local["persister"].verify_seal(local["receipt"], "receipt_sha256"))
                self.assertTrue(local["persister"].verify_seal(local["post_state"], "state_sha256"))

    def test_build_report_admits_synthetic_changed_b_to_merged_c_with_exact_lineage(self) -> None:
        """The indexed caller handles changed B→C evidence from a real shallow PR checkout."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )

        def git_state(repository: pathlib.Path) -> tuple[str, str, str, str, bytes | None]:
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD^{commit}"], cwd=repository, text=True,
            ).strip()
            origin_main = subprocess.check_output(
                ["git", "rev-parse", "refs/remotes/origin/main^{commit}"],
                cwd=repository, text=True,
            ).strip()
            head_tree = subprocess.check_output(
                ["git", "rev-parse", f"{head}^{{tree}}"], cwd=repository, text=True,
            ).strip()
            origin_tree = subprocess.check_output(
                ["git", "rev-parse", f"{origin_main}^{{tree}}"], cwd=repository, text=True,
            ).strip()
            shallow_path = pathlib.Path(subprocess.check_output(
                ["git", "rev-parse", "--path-format=absolute", "--git-path", "shallow"],
                cwd=repository, text=True,
            ).strip())
            shallow = shallow_path.read_bytes() if shallow_path.exists() else None
            return head, origin_main, head_tree, origin_tree, shallow

        with tempfile.TemporaryDirectory(
            prefix="completeness-synthetic-matching-b-c-shallow-", dir=ROOT.parent,
        ) as name:
            base = pathlib.Path(name)
            shallow_root, pr_head, trusted_main, _trusted_tree = self._make_genuine_shallow_local_source(base)
            shallow_state_before = git_state(shallow_root)
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "--is-shallow-repository"], cwd=shallow_root, text=True,
                ).strip(),
                "true",
            )
            self.assertNotEqual(pr_head, trusted_main)
            self.assertEqual(shallow_state_before[0], pr_head)
            self.assertEqual(shallow_state_before[1], trusted_main)
            self.assertEqual(shallow_state_before[3], _trusted_tree)
            local_origin = base / "shallow-source-origin.git"
            self.assertEqual(
                subprocess.check_output(
                    ["git", "--git-dir", str(local_origin), "rev-parse", "--is-shallow-repository"],
                    text=True,
                    env=self._closed_git_environment(),
                ).strip(),
                "false",
            )
            self.assertFalse((local_origin / "shallow").exists())
            historical_source_root = base / "authenticated-history-source"
            git_environment = self._closed_git_environment()
            subprocess.run(
                [
                    "git", "clone", "--quiet", "--shared", "--no-checkout",
                    str(local_origin), str(historical_source_root),
                ],
                check=True,
                env=git_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "--is-shallow-repository"],
                    cwd=historical_source_root,
                    text=True,
                    env=git_environment,
                ).strip(),
                "false",
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "refs/remotes/origin/main^{commit}"],
                    cwd=historical_source_root,
                    text=True,
                    env=git_environment,
                ).strip(),
                trusted_main,
            )
            historical_c_run = "01148419bef5d7f212e92ab96d0ba1c3da6c298c"
            subprocess.run(
                ["git", "cat-file", "-e", f"{historical_c_run}^{{commit}}"],
                cwd=historical_source_root,
                check=True,
                env=git_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", historical_c_run, trusted_main],
                    cwd=historical_source_root,
                    check=False,
                    env=git_environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).returncode,
                0,
            )
            alternate_path = pathlib.Path(subprocess.check_output(
                ["git", "rev-parse", "--path-format=absolute", "--git-path", "objects/info/alternates"],
                cwd=historical_source_root,
                text=True,
                env=git_environment,
            ).strip())
            self.assertEqual(
                [str(pathlib.Path(line).resolve()) for line in alternate_path.read_text().splitlines() if line],
                [str((local_origin / "objects").resolve())],
                "the private history source borrows only the authenticated temporary bare origin; "
                "keep both alive through the fixture",
            )
            input_root = base / "evidence"
            input_root.mkdir()
            with mock.patch(__name__ + ".ROOT", shallow_root), mock.patch.object(MODULE, "ROOT", shallow_root):
                repository, index_path, expected_generation = self._build_synthetic_matching_b_c_packet(
                    input_root,
                    repository_source_root=historical_source_root,
                    reuse_existing_borrowed_main=False,
                )
                index = json.loads(index_path.read_bytes())
                processor_run_input = next(
                    item for item in index["inputs"]
                    if item.get("scope_id") == operation_scope["scope_id"]
                    and item.get("subject", {}).get("stage") == "processor"
                    and item.get("role") == "pipeline_run"
                )
                processor_run_root = repository if processor_run_input["root"] == "repository" else input_root
                expected_processor_head = json.loads(
                    (processor_run_root / processor_run_input["path"]).read_bytes(),
                )["head_sha"]
                processor_archive_input = next(
                    item for item in index["inputs"]
                    if item.get("scope_id") == operation_scope["scope_id"]
                    and item.get("subject", {}).get("stage") == "processor"
                    and item.get("role") == "pipeline_artifact_archive"
                )
                processor_archive_root = (
                    repository if processor_archive_input["root"] == "repository" else input_root
                )
                processor_archive_path = processor_archive_root / processor_archive_input["path"]
                processor_archive_before = processor_archive_path.read_bytes()
                archive_identity = (
                    len(processor_archive_before),
                    hashlib.sha256(processor_archive_before).hexdigest(),
                )
                observed_producer_heads: list[str | None] = []
                original_import = MODULE.import_module

                def import_with_producer_head_spy(name: str, path: pathlib.Path) -> object:
                    imported = original_import(name, path)
                    if name == "completeness_processor_contract":
                        validate_bundle = imported.validate_processor_bundle

                        def validate_bundle_with_head(*args: object, **kwargs: object) -> object:
                            observed_producer_heads.append(kwargs.get("producer_head_sha"))
                            return validate_bundle(*args, **kwargs)

                        imported.validate_processor_bundle = validate_bundle_with_head
                    return imported

                fixture_state_before = git_state(repository)
                with self._candidate_processor_execution_control(
                    repository, exercise_wrong_producer=False,
                ) as control:
                    report = MODULE.build_report(
                        root=repository, input_root=input_root, input_index_path=index_path,
                    )
                self.assertEqual(
                    control["bundle_validation_heads"], [],
                    "the retained eight-output checkpoint is report-only and must not invoke the modern C validator",
                )
                self.assertEqual(control["modern_generation_validations"], [])
                self.assertEqual(len(control["promotion_stage_results"]), 1)
                promotion = control["promotion_stage_results"][0]
                self.assertEqual(promotion["status"], "merged-candidate-reconciled")
                self.assertTrue(promotion["candidate_available"])
                self.assertEqual(promotion["candidate_lifecycle_status"], "merged")
                self.assertTrue(promotion["processor_generation_matches"])
                self.assertEqual(promotion["candidate_key"][3], expected_generation)
                self.assertEqual(git_state(repository), fixture_state_before)
                self.assertEqual(processor_archive_path.read_bytes(), processor_archive_before)
                self.assertEqual(
                    (
                        processor_archive_path.stat().st_size,
                        hashlib.sha256(processor_archive_path.read_bytes()).hexdigest(),
                    ),
                    archive_identity,
                )
            self.assertEqual(git_state(shallow_root), shallow_state_before)
        row = next(item for item in report["scopes"] if item["scope_id"] == operation_scope["scope_id"])
        pipeline = next(facet for facet in row["facets"] if facet["facet_id"] == "specification_pipeline")
        delivery = next(facet for facet in row["facets"] if facet["facet_id"] == "immutable_publication_read_back")
        self.assertEqual(report["evaluation_context"]["mode"], "repository_evidence_snapshot")
        self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(pipeline["state"], "historical")
        self.assertEqual(pipeline["details"]["processor_generation_id"], expected_generation)
        self.assertTrue(pipeline["details"]["promotion_candidate_available"])
        self.assertTrue(pipeline["details"]["promotion_processor_generation_matches"])
        self.assertEqual(pipeline["details"]["promotion_candidate_key"][3], expected_generation)
        self.assertTrue(pipeline["details"]["health_processor_observation_matches"])
        self.assertEqual(delivery["state"], "historical")
        self.assertNotIn("current_release_subject_applicable", delivery["details"])
        self.assertEqual(
            delivery["details"]["subject"]["source_sha"],
            "6a5138c792f4b7402da0c5ab439646bd752a307f",
        )

    def test_fixture_uses_committed_schema_index_when_ambient_generation_changed_it(self) -> None:
        """Generated non-indexed G bytes cannot replace the validator's committed schema index."""
        fixture_source_revision = subprocess.check_output(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=CHECKED_OUT_ROOT, text=True,
        ).strip()
        tracked_schema_index = subprocess.check_output(
            ["git", "show", f"{fixture_source_revision}:schemas/index.json"],
            cwd=CHECKED_OUT_ROOT,
        )
        generated_schema_index = tracked_schema_index + b"\n"
        with tempfile.TemporaryDirectory(prefix="completeness-generated-schema-index-pin-") as name:
            root = pathlib.Path(name)
            ambient_source = root / "ambient/schemas/index.json"
            destination = root / "fixture/schemas/index.json"
            ambient_source.parent.mkdir(parents=True)
            ambient_source.write_bytes(generated_schema_index)
            generated_manifest_pin = (
                len(generated_schema_index), MODULE.sha256_bytes(generated_schema_index),
            )
            selected_pin = self._materialize_pinned_completeness_fixture_source(
                source_root=CHECKED_OUT_ROOT,
                source_path=ambient_source,
                destination_path=destination,
                relative="schemas/index.json",
                fixture_source_revision=fixture_source_revision,
                indexed_pin=None,
                manifest_pin=generated_manifest_pin,
            )
            tracked_pin = (len(tracked_schema_index), MODULE.sha256_bytes(tracked_schema_index))
            self.assertEqual(selected_pin, tracked_pin)
            self.assertEqual(destination.read_bytes(), tracked_schema_index)
            self.assertNotEqual(destination.read_bytes(), generated_schema_index)

            indexed_relative = "reports/kosis/operation-denominator.json"
            indexed_bytes = subprocess.check_output(
                ["git", "show", f"{fixture_source_revision}:{indexed_relative}"],
                cwd=CHECKED_OUT_ROOT,
            )
            mutated_indexed_source = root / "ambient/indexed-operation-denominator.json"
            mutated_indexed_source.write_bytes(indexed_bytes + b" ")
            indexed_pin = (len(indexed_bytes), MODULE.sha256_bytes(indexed_bytes))
            with self.assertRaisesRegex(
                AssertionError, "checked-out source does not match its authoritative byte/SHA pin"
            ):
                self._materialize_pinned_completeness_fixture_source(
                    source_root=CHECKED_OUT_ROOT,
                    source_path=mutated_indexed_source,
                    destination_path=root / "fixture/indexed-operation-denominator.json",
                    relative=indexed_relative,
                    fixture_source_revision=fixture_source_revision,
                    indexed_pin=indexed_pin,
                    manifest_pin=indexed_pin,
                )

    def test_build_report_rejects_tampered_materialized_repository_input(self) -> None:
        """Current candidate bytes are copied into old-main fixtures but retain exact input pins."""
        with tempfile.TemporaryDirectory(prefix="completeness-materialized-input-tamper-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            repository, index_path, _generation = self._build_synthetic_matching_b_c_packet(input_root)
            index = json.loads(index_path.read_bytes())
            kosis_input = next(
                item for item in index["inputs"]
                if item.get("root") == "repository"
                and item["path"] == "reports/kosis/operation-denominator.json"
            )
            self.assertGreater(kosis_input["bytes"], 0)
            destination = repository / kosis_input["path"]
            self.assertEqual(
                (len(destination.read_bytes()), MODULE.sha256_file(destination)),
                (kosis_input["bytes"], kosis_input["sha256"]),
            )
            destination.write_bytes(destination.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "byte count or SHA-256 differs"):
                MODULE.build_report(root=repository, input_root=input_root, input_index_path=index_path)

    def test_build_report_admits_modern_ten_output_b_to_merged_c_with_exact_lineage(self) -> None:
        """The current report caller joins a real modern B output to exact A/C/Health fixtures."""
        full_source_repository = ROOT.resolve()
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        origin_main_ref = "refs/remotes/origin/main"
        current_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{commit}"], cwd=ROOT, text=True,
        ).strip()
        merged_761 = "2693e4de28e601555b998a457816f2ad18321699"
        merged_761_tree = subprocess.check_output(
            ["git", "rev-parse", f"{merged_761}^{{tree}}"], cwd=ROOT, text=True,
        ).strip()
        self.assertEqual(merged_761_tree, "f03ccc828b6483314e821cb64d0c7f417889a6d3")
        self.assertEqual(current_head, subprocess.check_output(
            ["git", "rev-parse", "HEAD^{commit}"], cwd=ROOT, text=True,
        ).strip())
        self.assertEqual(
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", merged_761, current_head],
                cwd=ROOT, check=False,
            ).returncode,
            0,
            "the selected bounded-main commit must remain an ancestor of the Source candidate",
        )
        prior = subprocess.run(
            ["git", "rev-parse", "--verify", f"{origin_main_ref}^{{commit}}"],
            cwd=ROOT, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        prior_oid = prior.stdout.strip() if prior.returncode == 0 else None
        selected_main = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{origin_main_ref}^{{commit}}"],
            cwd=ROOT, text=True,
        ).strip()
        if prior_oid is not None:
            self.assertEqual(prior_oid, selected_main)
        if prior_oid is None:
            subprocess.run(["git", "update-ref", origin_main_ref, selected_main], cwd=ROOT, check=True)

        def restore_private_origin_main() -> None:
            current = subprocess.run(
                ["git", "rev-parse", "--verify", f"{origin_main_ref}^{{commit}}"],
                cwd=ROOT, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            current_oid = current.stdout.strip() if current.returncode == 0 else None
            if current_oid == prior_oid:
                return
            if prior_oid is None:
                if current_oid is not None:
                    subprocess.run(
                        ["git", "update-ref", "-d", origin_main_ref, current_oid], cwd=ROOT, check=True,
                    )
            elif current_oid is None:
                subprocess.run(["git", "update-ref", origin_main_ref, prior_oid], cwd=ROOT, check=True)
            else:
                subprocess.run(
                    ["git", "update-ref", origin_main_ref, prior_oid, current_oid], cwd=ROOT, check=True,
                )

        self.addCleanup(restore_private_origin_main)
        with tempfile.TemporaryDirectory(
            prefix="completeness-modern-matching-b-c-", dir=ROOT.parent,
        ) as name:
            base = pathlib.Path(name)
            shallow_root, pr_head, trusted_main, _trusted_tree = self._make_genuine_shallow_local_source(base)
            self.assertEqual(pr_head, current_head)
            self.assertNotEqual(pr_head, trusted_main)
            local_origin = base / "shallow-source-origin.git"
            self.assertEqual(
                subprocess.check_output(
                    ["git", "remote", "get-url", "origin"], cwd=shallow_root, text=True,
                ).strip(),
                local_origin.as_uri(),
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "--git-dir", str(local_origin), "rev-parse", "--is-bare-repository"],
                    text=True,
                ).strip(),
                "true",
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "--git-dir", str(local_origin), "rev-parse", "--is-shallow-repository"],
                    text=True,
                ).strip(),
                "false",
            )
            self.assertFalse((local_origin / "shallow").exists())
            self.assertEqual(
                subprocess.check_output(
                    ["git", "--git-dir", str(local_origin), "rev-parse", "refs/heads/main^{commit}"],
                    text=True,
                ).strip(),
                trusted_main,
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "--git-dir", str(local_origin), "rev-parse", "refs/heads/fixture-pr^{commit}"],
                    text=True,
                ).strip(),
                pr_head,
            )
            # Deepen only enough to connect the exact PR head to the pinned
            # main commit; older unneeded tree snapshots remain outside this
            # offline fixture. The fetch uses only the verified file:// origin.
            producer_ancestry_count = int(subprocess.check_output(
                ["git", "rev-list", "--count", f"{trusted_main}..{pr_head}"],
                cwd=shallow_root, text=True,
            ).strip())
            producer_depth = producer_ancestry_count + 1
            self.assertGreaterEqual(producer_ancestry_count, 1)
            self.assertLessEqual(producer_depth, 64)
            subprocess.run(
                [
                    "git", "fetch", "--no-tags", f"--depth={producer_depth}", "origin",
                    "+refs/heads/fixture-pr:refs/remotes/origin/fixture-pr",
                ],
                cwd=shallow_root,
                check=True,
                env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "--is-shallow-repository"], cwd=shallow_root, text=True,
                ).strip(),
                "true",
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "refs/remotes/origin/main^{commit}"], cwd=shallow_root, text=True,
                ).strip(),
                trusted_main,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", trusted_main, pr_head],
                    cwd=shallow_root, check=False,
                ).returncode,
                0,
            )
            shallow_path = pathlib.Path(subprocess.check_output(
                ["git", "rev-parse", "--path-format=absolute", "--git-path", "shallow"],
                cwd=shallow_root, text=True,
            ).strip())
            shallow_boundaries = set(shallow_path.read_text(encoding="ascii").splitlines())
            self.assertNotIn(pr_head, shallow_boundaries)
            input_root = base / "evidence"
            input_root.mkdir()
            with mock.patch(__name__ + ".ROOT", shallow_root), mock.patch.object(MODULE, "ROOT", shallow_root):
                modern_processor = self._build_modern_processor_fixture(pr_head)
                repository, index_path, expected_generation = self._build_synthetic_matching_b_c_packet(
                    input_root, modern_processor=modern_processor, include_legacy_publication=False,
                    repository_source_root=full_source_repository,
                    reuse_existing_borrowed_main=True,
                )
                historical_attestation = json.loads(
                    (repository / "reports/runtime-freshness-import-attestations/35798122454.json").read_bytes()
                )
                historical_merge = historical_attestation["import"]["merge_commit"]
                selected_c_main = subprocess.check_output(
                    ["git", "rev-parse", "refs/remotes/origin/main^{commit}"],
                    cwd=repository, text=True,
                ).strip()
                self.assertEqual(
                    subprocess.run(
                        ["git", "merge-base", "--is-ancestor", historical_merge, selected_c_main],
                        cwd=repository, check=False,
                    ).returncode,
                    0,
                    "the current C fixture must retain authentic historical-import ancestry",
                )
                self.assertEqual(
                    subprocess.check_output(
                        ["git", "rev-parse", "--is-shallow-repository"], cwd=repository, text=True,
                    ).strip(),
                    "false",
                    "the current C fixture uses the exact unshallow local object closure",
                )
                packet_index = json.loads(index_path.read_bytes())
                processor_run_input = next(
                    item for item in packet_index["inputs"]
                    if item.get("scope_id") == operation_scope["scope_id"]
                    and item.get("subject", {}).get("stage") == "processor"
                    and item.get("role") == "pipeline_run"
                )
                processor_run_root = repository if processor_run_input["root"] == "repository" else input_root
                processor_run = json.loads((processor_run_root / processor_run_input["path"]).read_bytes())
                self.assertEqual(processor_run["head_sha"], pr_head)
                processor_archive_input = next(
                    item for item in packet_index["inputs"]
                    if item.get("scope_id") == operation_scope["scope_id"]
                    and item.get("subject", {}).get("stage") == "processor"
                    and item.get("role") == "pipeline_artifact_archive"
                )
                archive_root = repository if processor_archive_input["root"] == "repository" else input_root
                archive_path = archive_root / processor_archive_input["path"]
                archive_before = archive_path.read_bytes()
                archive_identity = (len(archive_before), hashlib.sha256(archive_before).hexdigest())
                wrong_producer = self._make_distinct_composer_producer(
                    shallow_root, base / "wrong-composer-producer", pr_head,
                )
                with self._candidate_processor_execution_control(
                    repository,
                    exercise_wrong_producer=True,
                    wrong_producer=wrong_producer,
                    data_helper_profile="modern-producer",
                    data_helper_head_sha=pr_head,
                ) as control:
                    report = MODULE.build_report(
                        root=repository, input_root=input_root, input_index_path=index_path,
                    )
                self.assertEqual(len(control["bundle_validation_heads"]), 1)
                self.assertEqual(control["bundle_validation_heads"], [pr_head])
                self.assertNotEqual(wrong_producer[1], pr_head)
                self.assertEqual(
                    control["composer_identity_requests"],
                    [(repository, pr_head), wrong_producer],
                )
                self.assertEqual(len(control["wrong_head_rejections"]), 1)
                self.assertEqual(
                    control["execution_contract_identities"][0],
                    control["expected_execution_contract_identities"][0],
                )
                self.assertEqual(len(control["modern_generation_validations"]), 1)
                self.assertEqual(
                    control["modern_generation_validations"][0]["cache_source_profile"],
                    "self-contained-original-candidate-v1",
                )
                self.assertTrue(
                    control["modern_generation_validations"][0]["result"][0],
                    msg=("authenticated modern B generation differs from current C compatibility inputs: "
                         f"{control['modern_generation_validations'][0]!r}"),
                )
                self.assertEqual(len(control["promotion_stage_results"]), 1)
                self.assertTrue(control["promotion_stage_results"][0]["processor_generation_matches"])
                self.assertEqual(archive_path.read_bytes(), archive_before)
                self.assertEqual(
                    (archive_path.stat().st_size, hashlib.sha256(archive_path.read_bytes()).hexdigest()),
                    archive_identity,
                )

        row = next(item for item in report["scopes"] if item["scope_id"] == operation_scope["scope_id"])
        pipeline = next(facet for facet in row["facets"] if facet["facet_id"] == "specification_pipeline")
        delivery = next(facet for facet in row["facets"] if facet["facet_id"] == "immutable_publication_read_back")
        self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(pipeline["state"], "historical")
        self.assertEqual(pipeline["details"]["processor_generation_id"], expected_generation)
        self.assertTrue(pipeline["details"]["promotion_candidate_available"])
        self.assertTrue(pipeline["details"]["promotion_processor_generation_matches"])
        self.assertEqual(pipeline["details"]["promotion_candidate_key"][3], expected_generation)
        self.assertTrue(pipeline["details"]["health_processor_observation_matches"])
        self.assertEqual(delivery["state"], "missing")
        self.assertEqual(delivery["details"]["missing_stages"], ["publisher", "acknowledgement"])

    def test_build_report_keeps_synthetic_c_lifecycle_separate_from_native_publication(self) -> None:
        """C journal transitions remain visible but cannot stand in for publisher/ACK runs."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        transitions = {
            "publication-pending": ["publication-pending"],
            "published": ["publication-pending", "published"],
            "read-back-confirmed": ["publication-pending", "published", "read-back-confirmed"],
        }
        for final_status, expected_history in transitions.items():
            with self.subTest(final_status=final_status), tempfile.TemporaryDirectory(
                prefix=f"completeness-synthetic-c-{final_status}-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                repository, index_path, expected_generation = self._build_synthetic_matching_b_c_packet(
                    input_root, candidate_status=final_status, include_legacy_publication=False,
                )
                with self._candidate_processor_execution_control(repository):
                    report = MODULE.build_report(
                        root=repository, input_root=input_root, input_index_path=index_path,
                    )
                row = next(item for item in report["scopes"] if item["scope_id"] == operation_scope["scope_id"])
                facets = {facet["facet_id"]: facet for facet in row["facets"]}
                pipeline = facets["specification_pipeline"]
                delivery = facets["immutable_publication_read_back"]
                self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
                self.assertEqual(pipeline["state"], "historical")
                self.assertEqual(pipeline["details"]["processor_generation_id"], expected_generation)
                self.assertTrue(pipeline["details"]["promotion_candidate_available"])
                self.assertEqual(pipeline["details"]["promotion_candidate_lifecycle_status"], final_status)
                self.assertEqual(
                    pipeline["details"]["promotion_candidate_acknowledgement_statuses"], expected_history,
                )
                self.assertEqual(delivery["state"], "missing")
                self.assertEqual(delivery["details"]["missing_stages"], ["publisher", "acknowledgement"])
                self.assertEqual(
                    delivery["missing_evidence"][0]["code"], "same_subject_publication_read_back_missing",
                )

    def test_build_report_does_not_match_c_rows_with_foreign_generation_or_source(self) -> None:
        """A validly sealed candidate with the wrong B generation/source is not borrowed."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        variants = (
            {"candidate_generation_override": "f" * 64},
            {"candidate_source_id_override": "unregistered-source"},
        )
        for override in variants:
            with self.subTest(override=override), tempfile.TemporaryDirectory(
                prefix="completeness-synthetic-c-mismatch-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                repository, index_path, expected_generation = self._build_synthetic_matching_b_c_packet(
                    input_root, include_legacy_publication=False, **override,
                )
                with self._candidate_processor_execution_control(repository):
                    report = MODULE.build_report(
                        root=repository, input_root=input_root, input_index_path=index_path,
                    )
                row = next(item for item in report["scopes"] if item["scope_id"] == operation_scope["scope_id"])
                pipeline = next(facet for facet in row["facets"] if facet["facet_id"] == "specification_pipeline")
                self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
                self.assertEqual(pipeline["details"]["processor_generation_id"], expected_generation)
                self.assertFalse(pipeline["details"]["promotion_candidate_available"])
                self.assertFalse(pipeline["details"]["promotion_processor_generation_matches"])

    def test_build_report_rejects_synthetic_c_manifest_mismatch(self) -> None:
        """A merged C row cannot bind B's exact bytes to another release manifest."""
        with tempfile.TemporaryDirectory(prefix="completeness-synthetic-c-manifest-mismatch-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            repository, index_path, _generation = self._build_synthetic_matching_b_c_packet(
                input_root, candidate_manifest_override="f" * 64, include_legacy_publication=False,
            )
            with self._candidate_processor_execution_control(repository):
                with self.assertRaisesRegex(ValueError, "does not bind its exact release manifest"):
                    MODULE.build_report(
                        root=repository, input_root=input_root, input_index_path=index_path,
                    )

            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with self._candidate_processor_execution_control(repository):
                with mock.patch.object(
                    MODULE, "output_path",
                    side_effect=lambda _root, value, _label: output_paths[value],
                ):
                    result = MODULE.main([
                        "--repo-root", str(repository),
                        "--input-root", str(input_root),
                        "--input-index", str(index_path),
                        "--output-json", "rollup.json",
                        "--output-markdown", "rollup.md",
                        "--write",
                    ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

    def test_build_report_admits_second_distinct_checkpointed_a_without_new_credit(self) -> None:
        """A sealed B history can retain two distinct A locators without creating live A credit."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        with tempfile.TemporaryDirectory(prefix="completeness-synthetic-second-a-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            repository, index_path, _generation = self._build_synthetic_matching_b_c_packet(
                input_root, include_legacy_publication=False,
                processor_history_variant="distinct-second-observation",
                only_source_processor=True,
            )
            with self._candidate_processor_execution_control(repository):
                report = MODULE.build_report(
                    root=repository, input_root=input_root, input_index_path=index_path,
                )
            index = json.loads(index_path.read_bytes())
            generation_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_scope["scope_id"]
                and item.get("subject", {}).get("stage") == "processor"
                and item["role"] == "processor_generation_api"
            )
            checkpoint_api = json.loads((input_root / generation_item["path"]).read_bytes())
            checkpoint = json.loads(MODULE.base64.b64decode(checkpoint_api["content"]))
        row = next(item for item in report["scopes"] if item["scope_id"] == operation_scope["scope_id"])
        pipeline = next(facet for facet in row["facets"] if facet["facet_id"] == "specification_pipeline")
        self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(pipeline["state"], "missing")
        self.assertEqual(set(pipeline["details"]["missing_stages"]), {"promotion", "health"})
        self.assertEqual(checkpoint["observation_count"], 2)
        self.assertEqual(len(checkpoint["input_artifacts"]), 2)
        self.assertNotEqual(
            (checkpoint["input_artifacts"][0]["run_id"], checkpoint["input_artifacts"][0]["evidence_sha256"]),
            (checkpoint["input_artifacts"][1]["run_id"], checkpoint["input_artifacts"][1]["evidence_sha256"]),
        )
        self.assertEqual(
            checkpoint["last_observation"]["producer_run_id"], "36646768289",
            "the retained latest-A source identity remains unchanged in this synthetic history test",
        )

    def test_build_report_rejects_duplicate_a_replay_in_synthetic_b_history(self) -> None:
        """The full indexed B adapter rejects replayed run/evidence locators before a missing-stage return."""
        with tempfile.TemporaryDirectory(prefix="completeness-synthetic-duplicate-a-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            repository, index_path, _generation = self._build_synthetic_matching_b_c_packet(
                input_root, include_legacy_publication=False,
                processor_history_variant="duplicate-replay",
                only_source_processor=True,
            )
            with self._candidate_processor_execution_control(repository):
                with self.assertRaisesRegex(ValueError, "repeats an exact run/evidence locator"):
                    MODULE.build_report(
                        root=repository, input_root=input_root, input_index_path=index_path,
                    )

    def test_health_stage_preserves_bounded_history_and_nullable_last_good(self) -> None:
        """A validated Health snapshot may carry multiple observations and no publication."""
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index, checked, resolved = MODULE.validate_input_index(
            root=ROOT, input_root=ROOT, index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            scope_by_id=scope_by_id,
        )
        operation_inputs = [item for item in checked if item["scope_id"] == "data-go-kr.api-operations"]
        chain = MODULE.validate_pipeline_evidence(
            root=ROOT, operation_inputs=operation_inputs, all_inputs=checked,
            resolved=resolved, evaluation_epoch=index["evaluation_epoch"], scope_registry=scope_registry,
        )
        self.assertEqual(chain["status"], "verified_historical_chain")

        health_roles = MODULE.stage_input_map(operation_inputs, "health")
        health_run, health_job, _started, _completed = MODULE.validate_stage_run(
            stage="health", roles=health_roles, resolved=resolved,
            evaluation_epoch=index["evaluation_epoch"], root=ROOT,
        )
        validated_local = MODULE.validate_health_stage_local(
            root=ROOT, roles=health_roles, resolved=resolved, run=health_run,
            job=health_job, evaluation_epoch=index["evaluation_epoch"],
        )
        local = dict(validated_local)
        local["receipt"] = copy.deepcopy(validated_local["receipt"])
        local["post_state"] = copy.deepcopy(validated_local["post_state"])
        history = local["post_state"]["observations_by_source"]["data_go_kr"]
        self.assertEqual(len(history), 1)
        prior = copy.deepcopy(history[0])
        prior.update({
            "producer_run_id": "36640000000",
            "observed_at": "2026-09-28T23:44:39Z",
            "refresh_evidence_sha256": "a" * 64,
        })
        history.insert(0, prior)
        local["post_state"]["last_good_by_source"]["data_go_kr"] = None
        local["receipt"]["sources"][0]["canonical"]["last_good"] = None
        local["receipt"]["sources"][0]["canonical"]["already_canonical_candidate"] = None
        no_publication_promotion = copy.deepcopy(chain["promotion"])
        no_publication_promotion["already_canonical_generation_id"] = None
        no_publication_promotion["candidate_available"] = False

        # The local ZIP/state/ref/seal/persister transition above was validated
        # from the real retained packet. This focused adapter probe supplies a
        # producer-shaped, post-validation history extension to exercise the
        # generic bounded-history/null-publication branch without claiming it
        # is a new live observation or a new publication.
        with mock.patch.object(MODULE, "validate_health_stage_local", return_value=local):
            health = MODULE.validate_health_stage(
                root=ROOT, roles=health_roles, resolved=resolved,
                run=health_run, job=health_job,
                processor=chain["processor"], promotion=no_publication_promotion,
                source=chain["source"], current_registry=chain["current_registry"],
                evaluation_epoch=index["evaluation_epoch"],
            )
        self.assertEqual(health["observation_count"], 2)
        self.assertIsNone(health["last_good"])
        self.assertIsNone(health["last_good_source_sha"])
        self.assertTrue(health["source_observation_matches"])
        self.assertTrue(health["processor_observation_matches"])
        self.assertFalse(health["candidate_relation_valid"])

        bad_local = dict(local)
        bad_local["receipt"] = copy.deepcopy(local["receipt"])
        bad_local["receipt"]["sources"][0]["processor"]["checkpoint_sha256"] = "f" * 64
        with mock.patch.object(MODULE, "validate_health_stage_local", return_value=bad_local):
            with self.assertRaisesRegex(ValueError, "checkpoint digest differs"):
                MODULE.validate_health_stage(
                    root=ROOT, roles=health_roles, resolved=resolved,
                    run=health_run, job=health_job,
                    processor=chain["processor"], promotion=no_publication_promotion,
                    source=chain["source"], current_registry=chain["current_registry"],
                    evaluation_epoch=index["evaluation_epoch"],
                )

    def test_processor_input_history_supports_multiple_observations_and_rejects_duplicate_locator(self) -> None:
        candidate_sha = "a" * 64
        first = {
            "run_id": "100", "name": "upstream-catalog-refresh-100", "artifact_id": "200",
            "expires_at": "2026-10-10T00:00:00Z", "candidate_sha256": "f" * 64,
            "evidence_sha256": "b" * 64, "diff_sha256": "c" * 64,
        }
        latest = {
            "run_id": "101", "name": "upstream-catalog-refresh-101", "artifact_id": "201",
            "expires_at": "2026-10-11T00:00:00Z", "candidate_sha256": candidate_sha,
            "evidence_sha256": "d" * 64, "diff_sha256": "e" * 64,
        }
        checkpoint = {
            "generation_inputs": {"candidate_sha256": candidate_sha},
            "input_artifacts": [first, latest], "observation_count": 2,
            "last_observation": {
                "producer_run_id": "101", "refresh_evidence_sha256": "d" * 64,
                "observed_at": "2026-10-05T00:00:00Z",
            },
        }
        source = {
            "run_id": "101", "artifact_id": "201", "candidate_sha256": candidate_sha,
            "refresh_evidence_sha256": "d" * 64, "diff_sha256": "e" * 64,
            "observed_at": "2026-10-05T00:00:00Z",
        }
        selected, locator_count, observation_count = MODULE.validate_processor_input_history(checkpoint, source)
        self.assertEqual(selected["run_id"], "101")
        self.assertEqual(locator_count, 2)
        self.assertEqual(observation_count, 2)

        checkpoint["input_artifacts"].append(copy.deepcopy(latest))
        checkpoint["observation_count"] = 3
        with self.assertRaisesRegex(ValueError, "repeats an exact run/evidence locator"):
            MODULE.validate_processor_input_history(checkpoint, source)

    def test_rollup_offline_processor_contract_uses_exact_modern_source_closure(self) -> None:
        helper = MODULE.import_module(
            "test_completeness_cache_source_contract",
            ROOT / "scripts/upstream_catalogue_cache_source.py",
        )
        with tempfile.TemporaryDirectory(prefix="rollup-offline-cache-source-") as name:
            repository = pathlib.Path(name)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Rollup Test"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "rollup@example.invalid"], cwd=repository, check=True)
            source_values = {
                path: f"{field}\n".encode()
                for field, path in helper.MODERN_GENERATOR_INPUTS.items()
            }
            tracked = {
                "policy_sha256": "policy/source-refresh.json",
                "adapter_revision": "data/provider-index.json",
                "extractor_revision": "scripts/generate-batch-link-detail-registry-patches.py",
            }
            for field, path in tracked.items():
                source_values[path] = f"{field}\n".encode()
            for relative, raw in source_values.items():
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repository, check=True)
            head_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True,
            ).strip()
            generation_inputs = {
                field: MODULE.sha256_bytes(source_values[path])
                for field, path in tracked.items()
            }
            generation_inputs.update({
                "cache_source_profile": helper.CACHE_SOURCE_PROFILE,
                "generator_revision": helper.sha256_bytes(helper.canonical_json({
                    field: helper.sha256_bytes(source_values[path])
                    for field, path in helper.MODERN_GENERATOR_INPUTS.items()
                })),
            })
            generation_id = helper.sha256_bytes(helper.canonical_json(generation_inputs))
            outputs: list[dict[str, object]] = []
            owner = {
                "generation_id": generation_id,
                "generation_inputs": generation_inputs,
                "output_digests": outputs,
                "output_artifact": {
                    "repository": "StatPan/datapan-registry",
                    "run_id": "200",
                    "name": "upstream-catalogue-processing-200-1",
                    "artifact_id": "201",
                    "expires_at": "2026-11-04T07:02:45Z",
                    "bundle_manifest_sha256": helper.sha256_bytes(helper.canonical_json(outputs)),
                },
            }
            owner["checkpoint_sha256"] = helper.sha256_bytes(helper.canonical_json(owner))
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    helper.CHECKPOINT_RECEIPT_MEMBER,
                    helper.canonical_json(owner) + b"\n",
                )
            archive_raw = archive_buffer.getvalue()
            metadata = {
                "id": 201,
                "name": "upstream-catalogue-processing-200-1",
                "size_in_bytes": len(archive_raw),
                "digest": "sha256:" + MODULE.sha256_bytes(archive_raw),
                "expired": False,
                "created_at": "2026-10-05T07:02:45Z",
                "updated_at": "2026-10-05T07:03:00Z",
                "expires_at": "2026-11-04T07:02:45Z",
                "workflow_run": {
                    "id": 200, "head_sha": head_sha, "head_branch": "main",
                    "repository_id": 1, "head_repository_id": 1,
                },
            }
            retained_inventory = {"total_count": 1, "artifacts": [metadata]}
            retained_run = {
                "id": 200, "run_attempt": 1,
                "path": ".github/workflows/upstream-catalogue-process.yml",
                "head_branch": "main", "head_sha": head_sha,
                "status": "completed", "conclusion": "success", "event": "schedule",
                "repository": {"id": 1, "full_name": "StatPan/datapan-registry"},
                "head_repository": {"id": 1, "full_name": "StatPan/datapan-registry"},
            }
            deadline = time.monotonic() + helper.MAX_WHOLE_ATTEMPT_SECONDS
            reader = helper.LocalGitSourceReader(cwd=repository, deadline_monotonic=deadline)
            real_popen = subprocess.Popen
            child_commands: list[tuple[str, ...]] = []

            def bounded_child(argv, *args, **kwargs):
                child_commands.append(tuple(argv))
                return real_popen(argv, *args, **kwargs)

            with mock.patch.object(helper.subprocess, "Popen", side_effect=bounded_child):
                contract = helper.authenticate_owner_contract_from_bound_evidence(
                    owner,
                    repository="StatPan/datapan-registry", default_branch="main",
                    artifact_inventory=retained_inventory, run=retained_run,
                    archive=archive_raw, source_reader=reader,
                    deadline_monotonic=deadline,
                )
                promotion = type("PromotionFixture", (), {
                    "PROCESSOR_COMPOSITION_INPUTS": {},
                    "PROCESSOR_COMPATIBILITY_FILES": (
                        "scripts/process-upstream-catalogue-candidate.py",
                    ),
                    "authenticated_processor_compatibility_paths": staticmethod(
                        lambda _generation_inputs, **_kwargs: (
                            "scripts/process-upstream-catalogue-candidate.py",
                        )
                    ),
                })
                compatible, changed = MODULE.validate_processor_generation_inputs(
                    root=repository, promotion=promotion, checkpoint=owner,
                    processor_head_sha=head_sha, composition_receipt=None,
                    cache_source_helper=helper, cache_source_contract=contract,
                    source_reader=reader,
                )
            self.assertTrue(compatible)
            self.assertEqual(changed, [])
            self.assertTrue(child_commands)
            self.assertTrue(all(command[0] == "git" for command in child_commands))

            second_helper = MODULE.import_module(
                "test_completeness_cache_source_contract_other_identity",
                ROOT / "scripts/upstream_catalogue_cache_source.py",
            )
            with self.assertRaisesRegex(ValueError, "authenticated modern producer contract"):
                MODULE.validate_processor_generation_inputs(
                    root=repository, promotion=promotion, checkpoint=owner,
                    processor_head_sha=head_sha, composition_receipt=None,
                    cache_source_helper=second_helper, cache_source_contract=contract,
                    source_reader=reader,
                )

    def test_modern_derived_processor_projection_authenticates_the_eighth_source(self) -> None:
        promotion = MODULE.import_module(
            "test_derived_processor_projection_promotion",
            ROOT / "scripts/run-canonical-update-promotion.py",
        )
        helper = MODULE.import_module(
            "test_derived_processor_projection_cache_source",
            ROOT / "scripts/upstream_catalogue_cache_source.py",
        )
        derivation_helper = MODULE.import_module(
            "test_derived_processor_projection_derivation",
            ROOT / "scripts/upstream_catalogue_derivation.py",
        )

        def derivation_envelope(revision: str) -> dict[str, object]:
            original = {
                "source_id": "data_go_kr", "source_scope": "aggregate_supported_catalog",
                "producer_run_id": "100", "producer_run_attempt": 1,
                "producer_head_sha": "1" * 40, "producer_artifact_id": "101",
                "producer_artifact_name": "upstream-catalog-refresh-100",
                "producer_artifact_sha256": "2" * 64,
                "observed_at": "2026-10-05T00:00:00Z",
                "original_baseline_sha256": "3" * 64,
                "candidate_sha256": "4" * 64, "evidence_sha256": "5" * 64,
                "diff_sha256": "6" * 64, "source_policy_sha256": "7" * 64,
                "provider_index_sha256": "8" * 64, "observation_count": 1,
            }

            def parent(generation_id: str, run_id: str, artifact_id: str) -> dict[str, object]:
                return {
                    "generation_id": generation_id, "checkpoint_sha256": "9" * 64,
                    "generation_inputs_sha256": "a" * 64,
                    "bundle_manifest_sha256": "b" * 64,
                    "repository": "StatPan/datapan-registry", "artifact_run_id": run_id,
                    "artifact_id": artifact_id,
                    "artifact_name": f"upstream-catalogue-processing-{run_id}-1",
                    "artifact_expires_at": "2026-11-04T00:00:00Z",
                    "processor_generator_revision": "c" * 64,
                    "processor_extractor_revision": "d" * 64,
                    "original_baseline_sha256": "3" * 64,
                    "candidate_sha256": "4" * 64,
                    "enrichment_evidence_sha256": "e" * 64,
                    "composition_receipt_sha256": "f" * 64,
                    "composed_registry_sha256": "0" * 64,
                    "original_observation_sha256": derivation_helper.object_sha256(original),
                    "contribution_set_sha256": "2" * 64, "contribution_count": 0,
                }

            resume = parent("3" * 64, "200", "201")
            canonical = parent("4" * 64, "300", "301")
            readback = {
                "repository": "StatPan/datapan-registry", "journal_ref_sha": "5" * 40,
                "journal_sha256": "6" * 64, "journal_record_index": 0,
                "journal_record_sha256": "7" * 64,
                "canonical_producer_generation_id": canonical["generation_id"],
                "source_id": "data_go_kr", "source_scope": "aggregate_supported_catalog",
                "registry_path": "data/data-go-kr.registry.json", "registry_bytes": 1,
                "registry_sha256": canonical["composed_registry_sha256"],
                "manifest_sha256": "8" * 64, "pr_number": 1,
                "pr_branch": "automation/canonical-update/fixture",
                "pr_body_sha256": "9" * 64, "pr_head_sha": "a" * 40,
                "merge_sha": "b" * 40, "merge_ack_run_id": 400,
                "merge_ack_run_attempt": 1,
                "merge_ack_run_url": (
                    "https://github.com/StatPan/datapan-registry/actions/runs/400/attempts/1"
                ),
                "merge_ack_observed_at": "2026-10-05T01:00:00Z",
                "merge_ack_manifest_sha256": "8" * 64,
                "merge_ack_source_sha": "b" * 40,
            }
            return derivation_helper.build_derivation_envelope(
                original_observation=original,
                resume_parent_processor=resume,
                canonical_parent_processor=canonical,
                canonical_parent_readback=readback,
                composition_baseline={
                    "main_sha": "b" * 40, "manifest_sha256": "8" * 64,
                    "registry_path": "data/data-go-kr.registry.json",
                    "registry_sha256": "0" * 64, "registry_bytes": 1,
                },
                derivation_processor_revision_sha256=revision,
            )

        with tempfile.TemporaryDirectory(prefix="rollup-derived-processor-projection-") as name:
            repository = pathlib.Path(name)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Rollup Test"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "rollup@example.invalid"], cwd=repository, check=True)
            tracked = {
                "policy_sha256": "policy/source-refresh.json",
                "adapter_revision": "data/provider-index.json",
                "extractor_revision": "scripts/generate-batch-link-detail-registry-patches.py",
            }
            source_paths = set(promotion.PROCESSOR_COMPATIBILITY_FILES)
            source_paths.update(helper.MODERN_GENERATOR_INPUTS.values())
            source_paths.update(tracked.values())
            source_paths.add(promotion.PROCESSOR_DERIVATION_COMPATIBILITY_FILE)
            for relative in sorted(source_paths):
                target = repository / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
            subprocess.run(["git", "add", *sorted(source_paths)], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "derived producer"], cwd=repository, check=True)
            producer_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True,
            ).strip()

            def reader() -> object:
                deadline = time.monotonic() + helper.MAX_WHOLE_ATTEMPT_SECONDS
                return helper.LocalGitSourceReader(cwd=repository, deadline_monotonic=deadline)

            def owner_and_contract(
                generation_inputs: dict[str, object], source_reader: object,
                *, head_sha: str = producer_head,
            ) -> tuple[dict[str, object], object]:
                generation_id = helper.sha256_bytes(helper.canonical_json(generation_inputs))
                outputs: list[dict[str, object]] = []
                owner: dict[str, object] = {
                    "generation_id": generation_id, "generation_inputs": generation_inputs,
                    "output_digests": outputs,
                    "output_artifact": {
                        "repository": "StatPan/datapan-registry", "run_id": "200",
                        "name": "upstream-catalogue-processing-200-1", "artifact_id": "201",
                        "expires_at": "2026-11-04T07:02:45Z",
                        "bundle_manifest_sha256": helper.sha256_bytes(helper.canonical_json(outputs)),
                    },
                }
                owner["checkpoint_sha256"] = helper.sha256_bytes(helper.canonical_json(owner))
                archive_buffer = io.BytesIO()
                with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr(helper.CHECKPOINT_RECEIPT_MEMBER, helper.canonical_json(owner) + b"\n")
                archive_raw = archive_buffer.getvalue()
                metadata = {
                    "id": 201, "name": "upstream-catalogue-processing-200-1",
                    "size_in_bytes": len(archive_raw),
                    "digest": "sha256:" + MODULE.sha256_bytes(archive_raw),
                    "expired": False, "created_at": "2026-10-05T07:02:45Z",
                    "updated_at": "2026-10-05T07:03:00Z",
                    "expires_at": "2026-11-04T07:02:45Z",
                    "workflow_run": {
                        "id": 200, "head_sha": head_sha, "head_branch": "main",
                        "repository_id": 1, "head_repository_id": 1,
                    },
                }
                run = {
                    "id": 200, "run_attempt": 1,
                    "path": ".github/workflows/upstream-catalogue-process.yml",
                    "head_branch": "main", "head_sha": head_sha,
                    "status": "completed", "conclusion": "success", "event": "schedule",
                    "repository": {"id": 1, "full_name": "StatPan/datapan-registry"},
                    "head_repository": {"id": 1, "full_name": "StatPan/datapan-registry"},
                }
                deadline = source_reader.deadline
                contract = helper.authenticate_owner_contract_from_bound_evidence(
                    owner,
                    repository="StatPan/datapan-registry", default_branch="main",
                    artifact_inventory={"total_count": 1, "artifacts": [metadata]},
                    run=run, archive=archive_raw, source_reader=source_reader,
                    deadline_monotonic=deadline,
                )
                return owner, contract

            source_reader = reader()
            revision = helper.sha256_bytes(helper.canonical_json({
                "processor_script_sha256": MODULE.sha256_bytes(
                    (repository / "scripts/process-upstream-catalogue-candidate.py").read_bytes()
                ),
                "collector_handoff_helper_sha256": MODULE.sha256_bytes(
                    (repository / "scripts/upstream_catalogue_handoff.py").read_bytes()
                ),
                "same_observation_derivation_helper_sha256": MODULE.sha256_bytes(
                    (repository / promotion.PROCESSOR_DERIVATION_COMPATIBILITY_FILE).read_bytes()
                ),
                "cache_source_helper_sha256": MODULE.sha256_bytes(
                    (repository / "scripts/upstream_catalogue_cache_source.py").read_bytes()
                ),
            }))
            generation_inputs: dict[str, object] = {
                field: MODULE.sha256_bytes((repository / path).read_bytes())
                for field, path in tracked.items()
            }
            generation_inputs.update({
                "cache_source_profile": helper.CACHE_SOURCE_PROFILE,
                "generator_revision": helper.modern_generator_revision(
                    repository="StatPan/datapan-registry", head_sha=producer_head,
                    reader=source_reader,
                ),
                "same_observation_derivation": derivation_envelope(revision),
            })
            owner, contract = owner_and_contract(generation_inputs, source_reader)
            compatible, changed = MODULE.validate_processor_generation_inputs(
                root=repository, promotion=promotion, checkpoint=owner,
                processor_head_sha=producer_head, composition_receipt=None,
                cache_source_helper=helper, cache_source_contract=contract,
                source_reader=source_reader,
            )
            self.assertTrue(compatible)
            self.assertEqual(changed, [])

            derivation_path = repository / promotion.PROCESSOR_DERIVATION_COMPATIBILITY_FILE
            producer_derivation = derivation_path.read_bytes()
            derivation_path.write_bytes(producer_derivation + b"\n# current-only drift\n")
            source_reader = reader()
            owner, contract = owner_and_contract(generation_inputs, source_reader)
            compatible, changed = MODULE.validate_processor_generation_inputs(
                root=repository, promotion=promotion, checkpoint=owner,
                processor_head_sha=producer_head, composition_receipt=None,
                cache_source_helper=helper, cache_source_contract=contract,
                source_reader=source_reader,
            )
            self.assertFalse(compatible)
            self.assertEqual(changed, [promotion.PROCESSOR_DERIVATION_COMPATIBILITY_FILE])

            derivation_path.write_bytes(producer_derivation)
            derived_inputs = copy.deepcopy(generation_inputs)
            tampered_inputs = copy.deepcopy(generation_inputs)
            tampered_inputs["same_observation_derivation"][
                "derivation_processor_revision_sha256"
            ] = "f" * 64
            source_reader = reader()
            tampered_owner, tampered_contract = owner_and_contract(tampered_inputs, source_reader)
            with self.assertRaisesRegex(ValueError, "compatibility projection is not authenticated"):
                MODULE.validate_processor_generation_inputs(
                    root=repository, promotion=promotion, checkpoint=tampered_owner,
                    processor_head_sha=producer_head, composition_receipt=None,
                    cache_source_helper=helper, cache_source_contract=tampered_contract,
                    source_reader=source_reader,
                )

            generation_inputs.pop("same_observation_derivation")
            source_reader = reader()
            ordinary_owner, ordinary_contract = owner_and_contract(generation_inputs, source_reader)
            compatible, changed = MODULE.validate_processor_generation_inputs(
                root=repository, promotion=promotion, checkpoint=ordinary_owner,
                processor_head_sha=producer_head, composition_receipt=None,
                cache_source_helper=helper, cache_source_contract=ordinary_contract,
                source_reader=source_reader,
            )
            self.assertTrue(compatible)
            self.assertEqual(changed, [])

            derivation_path.unlink()
            subprocess.run(
                ["git", "add", promotion.PROCESSOR_DERIVATION_COMPATIBILITY_FILE],
                cwd=repository, check=True,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "producer omitted derivation helper"],
                cwd=repository, check=True,
            )
            missing_source_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True,
            ).strip()
            derivation_path.write_bytes(producer_derivation)
            source_reader = reader()
            missing_owner, missing_contract = owner_and_contract(
                derived_inputs, source_reader, head_sha=missing_source_head,
            )
            with self.assertRaisesRegex(ValueError, "compatibility projection is not authenticated"):
                MODULE.validate_processor_generation_inputs(
                    root=repository, promotion=promotion, checkpoint=missing_owner,
                    processor_head_sha=missing_source_head, composition_receipt=None,
                    cache_source_helper=helper, cache_source_contract=missing_contract,
                    source_reader=source_reader,
                )

    def test_validate_processor_stage_retains_authenticated_modern_derivation_drift(self) -> None:
        promotion_source = MODULE.import_module(
            "test_stage_derived_processor_promotion_source",
            ROOT / "scripts/run-canonical-update-promotion.py",
        )
        cache_source = MODULE.import_module(
            "test_stage_derived_processor_cache_source",
            ROOT / "scripts/upstream_catalogue_cache_source.py",
        )
        derivation_source = MODULE.import_module(
            "test_stage_derived_processor_derivation_source",
            ROOT / "scripts/upstream_catalogue_derivation.py",
        )

        with tempfile.TemporaryDirectory(prefix="rollup-derived-processor-stage-") as name:
            fixture = pathlib.Path(name)
            repository = fixture / "repository"
            repository.mkdir()
            self._hardlink_worktree(ROOT, repository)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Rollup Test"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "rollup@example.invalid"], cwd=repository, check=True)
            tracked_inputs = {
                "policy_sha256": "policy/source-refresh.json",
                "adapter_revision": "data/provider-index.json",
                "extractor_revision": "scripts/generate-batch-link-detail-registry-patches.py",
            }
            producer_paths = set(promotion_source.PROCESSOR_COMPATIBILITY_FILES)
            producer_paths.update(cache_source.MODERN_GENERATOR_INPUTS.values())
            producer_paths.update(tracked_inputs.values())
            producer_paths.add(promotion_source.PROCESSOR_DERIVATION_COMPATIBILITY_FILE)
            subprocess.run(["git", "add", *sorted(producer_paths)], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "modern derived producer"], cwd=repository, check=True)
            producer_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True,
            ).strip()
            deadline = time.monotonic() + cache_source.MAX_WHOLE_ATTEMPT_SECONDS
            producer_reader = cache_source.LocalGitSourceReader(
                cwd=repository, deadline_monotonic=deadline,
            )

            candidate_bytes = b'{"candidate":true}\n'
            candidate_sha = MODULE.sha256_bytes(candidate_bytes)
            original_baseline_sha = "3" * 64
            composition_baseline_sha = "0" * 64
            derivation_revision = cache_source.sha256_bytes(cache_source.canonical_json({
                "processor_script_sha256": MODULE.sha256_bytes(
                    (repository / "scripts/process-upstream-catalogue-candidate.py").read_bytes()
                ),
                "collector_handoff_helper_sha256": MODULE.sha256_bytes(
                    (repository / "scripts/upstream_catalogue_handoff.py").read_bytes()
                ),
                "same_observation_derivation_helper_sha256": MODULE.sha256_bytes(
                    (repository / promotion_source.PROCESSOR_DERIVATION_COMPATIBILITY_FILE).read_bytes()
                ),
                "cache_source_helper_sha256": MODULE.sha256_bytes(
                    (repository / "scripts/upstream_catalogue_cache_source.py").read_bytes()
                ),
            }))
            original_observation = {
                "source_id": "data_go_kr", "source_scope": "aggregate_supported_catalog",
                "producer_run_id": "100", "producer_run_attempt": 1,
                "producer_head_sha": "1" * 40, "producer_artifact_id": "101",
                "producer_artifact_name": "upstream-catalog-refresh-100",
                "producer_artifact_sha256": "2" * 64,
                "observed_at": "2026-10-05T00:00:00Z",
                "original_baseline_sha256": original_baseline_sha,
                "candidate_sha256": candidate_sha, "evidence_sha256": "5" * 64,
                "diff_sha256": "6" * 64, "source_policy_sha256": "7" * 64,
                "provider_index_sha256": "8" * 64, "observation_count": 1,
            }

            def parent(generation_id: str, run_id: str, artifact_id: str) -> dict[str, object]:
                return {
                    "generation_id": generation_id, "checkpoint_sha256": "9" * 64,
                    "generation_inputs_sha256": "a" * 64,
                    "bundle_manifest_sha256": "b" * 64,
                    "repository": "StatPan/datapan-registry", "artifact_run_id": run_id,
                    "artifact_id": artifact_id,
                    "artifact_name": f"upstream-catalogue-processing-{run_id}-1",
                    "artifact_expires_at": "2026-11-04T00:00:00Z",
                    "processor_generator_revision": "c" * 64,
                    "processor_extractor_revision": "d" * 64,
                    "original_baseline_sha256": original_baseline_sha,
                    "candidate_sha256": candidate_sha,
                    "enrichment_evidence_sha256": "e" * 64,
                    "composition_receipt_sha256": "f" * 64,
                    "composed_registry_sha256": composition_baseline_sha,
                    "original_observation_sha256": derivation_source.object_sha256(original_observation),
                    "contribution_set_sha256": "2" * 64, "contribution_count": 0,
                }

            resume_parent = parent("3" * 64, "200", "201")
            canonical_parent = parent("4" * 64, "300", "301")
            readback = {
                "repository": "StatPan/datapan-registry", "journal_ref_sha": "5" * 40,
                "journal_sha256": "6" * 64, "journal_record_index": 0,
                "journal_record_sha256": "7" * 64,
                "canonical_producer_generation_id": canonical_parent["generation_id"],
                "source_id": "data_go_kr", "source_scope": "aggregate_supported_catalog",
                "registry_path": "data/data-go-kr.registry.json", "registry_bytes": 1,
                "registry_sha256": composition_baseline_sha, "manifest_sha256": "8" * 64,
                "pr_number": 1, "pr_branch": "automation/canonical-update/fixture",
                "pr_body_sha256": "9" * 64, "pr_head_sha": "a" * 40,
                "merge_sha": "b" * 40, "merge_ack_run_id": 400,
                "merge_ack_run_attempt": 1,
                "merge_ack_run_url": (
                    "https://github.com/StatPan/datapan-registry/actions/runs/400/attempts/1"
                ),
                "merge_ack_observed_at": "2026-10-05T01:00:00Z",
                "merge_ack_manifest_sha256": "8" * 64,
                "merge_ack_source_sha": "b" * 40,
            }
            derivation = derivation_source.build_derivation_envelope(
                original_observation=original_observation,
                resume_parent_processor=resume_parent,
                canonical_parent_processor=canonical_parent,
                canonical_parent_readback=readback,
                composition_baseline={
                    "main_sha": "b" * 40, "manifest_sha256": "8" * 64,
                    "registry_path": "data/data-go-kr.registry.json",
                    "registry_sha256": composition_baseline_sha, "registry_bytes": 1,
                },
                derivation_processor_revision_sha256=derivation_revision,
            )
            generation_inputs: dict[str, object] = {
                "source_id": "data_go_kr", "source_scope": "aggregate_supported_catalog",
                "baseline_sha256": original_baseline_sha, "candidate_sha256": candidate_sha,
                "observation_failure_sha256": None,
                **{
                    field: MODULE.sha256_bytes((repository / path).read_bytes())
                    for field, path in tracked_inputs.items()
                },
                "cache_source_profile": cache_source.CACHE_SOURCE_PROFILE,
                "generator_revision": cache_source.modern_generator_revision(
                    repository="StatPan/datapan-registry", head_sha=producer_head,
                    reader=producer_reader,
                ),
                "same_observation_derivation": derivation,
            }
            generation_id = MODULE.sha256_bytes(promotion_source.canonical_json(generation_inputs))
            observed_at = "2026-10-05T00:00:00Z"
            checkpoint: dict[str, object] = {
                "schema_version": promotion_source.PROCESSOR_SCHEMA,
                "source_id": "data_go_kr", "source_scope": "aggregate_supported_catalog",
                "generation_id": generation_id, "generation_inputs": generation_inputs,
                "observed_at": observed_at,
                "last_observation": {
                    "observed_at": observed_at, "producer_run_id": "100",
                    "refresh_evidence_sha256": "5" * 64,
                    "collection_status": "success", "execution_mode": "fixture",
                },
                "observation_count": 1, "last_heartbeat_at": observed_at,
                "last_progress_at": observed_at, "status": "ready",
                "attempts_consumed": 0, "attempts_by_id": {},
                "detail_retry_reset_ids": [], "request_reservation": None,
                "detail_records": [], "detail_queue_cursor": 0, "lease": None,
                "fencing_token": 1, "outcome": {"reason": "ready"},
                "output_artifact": {
                    "repository": "StatPan/datapan-registry", "run_id": "70000000001",
                    "name": "upstream-catalogue-processing-70000000001-1", "artifact_id": "201",
                    "expires_at": "2026-11-04T07:02:45Z", "bundle_manifest_sha256": "0" * 64,
                },
                "output_digests": [],
                "input_artifacts": [{
                    "run_id": "100", "name": "upstream-catalog-refresh-100",
                    "artifact_id": "101", "expires_at": "2026-11-04T00:00:00Z",
                    "candidate_sha256": candidate_sha, "evidence_sha256": "5" * 64,
                    "diff_sha256": "6" * 64,
                }],
            }
            bundle = fixture / "bundle"
            bundle.mkdir()
            original_candidate = bundle / cache_source.ORIGINAL_CANDIDATE_MEMBER
            original_candidate.write_bytes(candidate_bytes)
            admission = {
                "admission_id": MODULE.sha256_bytes(
                    json.dumps(["100", 1, "5" * 64], separators=(",", ":")).encode()
                ),
                "producer_run_id": "100",
                "run_attempt": 1, "head_sha": "1" * 40, "run_started_at": observed_at,
                "artifact_id": "101", "artifact_name": "upstream-catalog-refresh-100",
                "artifact_expires_at": "2026-11-04T00:00:00Z",
                "artifact_digest_sha256": "2" * 64,
                "artifact_size_bytes": len(candidate_bytes),
                "refresh_evidence_sha256": "5" * 64, "observed_at": observed_at,
                "generation_id": "7" * 64, "candidate_sha256": candidate_sha,
                "admitted_at": "2026-10-05T00:00:01Z",
            }
            context = cache_source.build_context(
                owner_generation_id=generation_id, source_id="data_go_kr",
                source_scope="aggregate_supported_catalog",
                original_admission_ref=admission, candidate_path=original_candidate,
            )
            input_digests = {
                "baseline": {"bytes": 1, "sha256": original_baseline_sha},
                "candidate": {"bytes": len(candidate_bytes), "sha256": candidate_sha},
                "composition_baseline": {"bytes": 1, "sha256": composition_baseline_sha},
            }
            input_digests.update({
                name: {
                    "bytes": (repository / path).stat().st_size,
                    "sha256": MODULE.sha256_bytes((repository / path).read_bytes()),
                }
                for name, path in promotion_source.PROCESSOR_COMPOSITION_INPUTS.items()
            })
            composition = {
                "schema_version": "datapan.catalogue-composition-receipt.v1",
                "input_digests": input_digests, "status": "ready_scoped",
                "same_observation_derivation": derivation,
            }
            result = {
                "status": "ready", "reason": "ready", "generation_id": generation_id,
                "source_id": "data_go_kr", "producer_run_id": "100",
                "processor_run_id": "70000000001-1",
                "processor_artifact_run_id": "70000000001",
                "processing_replay": False, "candidate_available": True,
            }
            bundle_payloads = {
                "composed-candidate.registry.json": candidate_bytes,
                "ready-scope.registry.json": b"[]\n",
                "semantic-diff.json": b'{"api_decisions":[]}\n',
                "regeneration-queue.json": b'{"items":[]}\n',
                "quarantine.json": b'{"items":[]}\n',
                "composition-receipt.json": promotion_source.canonical_json(composition),
                cache_source.ORIGINAL_CANDIDATE_MEMBER: candidate_bytes,
                cache_source.CONTEXT_MEMBER: promotion_source.canonical_json(context) + b"\n",
                "upstream-catalogue-enrichment-evidence.json": b'{"records":[]}\n',
                "upstream-catalogue-processing-result.json": promotion_source.canonical_json(result),
            }
            output_digests: list[dict[str, object]] = []
            for member in promotion_source.REQUIRED_PROCESSOR_FILES:
                payload = bundle_payloads[member]
                (bundle / member).write_bytes(payload)
                output_digests.append({
                    "path": member, "bytes": len(payload), "sha256": MODULE.sha256_bytes(payload),
                })
            checkpoint["output_digests"] = output_digests
            checkpoint["output_artifact"]["bundle_manifest_sha256"] = MODULE.sha256_bytes(
                promotion_source.canonical_json(output_digests)
            )

            def seal(value: dict[str, object]) -> None:
                value.pop("checkpoint_sha256", None)
                value["checkpoint_sha256"] = MODULE.sha256_bytes(
                    promotion_source.canonical_json(value)
                )

            uploaded = copy.deepcopy(checkpoint)
            uploaded["output_artifact"]["artifact_id"] = None
            uploaded["output_artifact"]["expires_at"] = "2026-11-03T07:02:45Z"
            seal(uploaded)
            checkpoint["last_heartbeat_at"] = "2026-10-05T00:01:00Z"
            seal(checkpoint)
            checkpoint_receipt = promotion_source.canonical_json(uploaded) + b"\n"
            (bundle / promotion_source.RETAINED_PROCESSOR_CHECKPOINT_MEMBER).write_bytes(checkpoint_receipt)
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
                for member in (*promotion_source.REQUIRED_PROCESSOR_FILES,
                               promotion_source.RETAINED_PROCESSOR_CHECKPOINT_MEMBER):
                    archive_file.writestr(member, (bundle / member).read_bytes())
            archive = archive_buffer.getvalue()
            run = {
                "id": 70000000001, "run_attempt": 1,
                "name": promotion_source.PROCESSOR_WORKFLOW_NAME,
                "workflow_id": MODULE.PIPELINE_WORKFLOWS["processor"]["workflow_id"],
                "path": promotion_source.PROCESSOR_WORKFLOW_PATH,
                "repository": {"id": 1, "full_name": "StatPan/datapan-registry"},
                "head_repository": {"id": 1, "full_name": "StatPan/datapan-registry"},
                "head_branch": "main", "head_sha": producer_head, "event": "schedule",
                "status": "completed", "conclusion": "success",
            }
            artifact_metadata = {
                "id": 201, "name": "upstream-catalogue-processing-70000000001-1",
                "expired": False, "expires_at": "2026-11-04T07:02:45Z",
                "created_at": "2026-10-05T00:00:00Z",
                "updated_at": "2026-10-05T00:01:00Z",
                "size_in_bytes": len(archive),
                "digest": "sha256:" + MODULE.sha256_bytes(archive),
                "workflow_run": {
                    "id": 70000000001, "head_sha": producer_head, "head_branch": "main",
                    "repository_id": 1, "head_repository_id": 1,
                },
            }
            inventory = {"total_count": 1, "artifacts": [artifact_metadata]}
            expected_generation_path = (
                ".datapan/upstream-catalogue-state/sources/data_go_kr/generations/"
                f"{generation_id}.json"
            )
            state_bytes = {
                "processor_state_root_api": json.dumps({
                    "repository": "StatPan/datapan-registry", "source_id": "data_go_kr",
                    "state_root": ".datapan/upstream-catalogue-state",
                    "schema_version": "datapan.upstream-catalogue-state-root.v1",
                }).encode(),
                "processor_index_api": json.dumps({
                    "generations": [{
                        "generation_id": generation_id, "checkpoint": f"{generation_id}.json",
                        "status": "ready", "candidate_sha256": candidate_sha,
                    }],
                }).encode(),
                "processor_generation_api": json.dumps(checkpoint).encode(),
            }
            evidence = fixture / "evidence"
            evidence.mkdir()

            def write(name: str, value: object, *, raw: bool = False) -> pathlib.Path:
                path = evidence / name
                path.write_bytes(value if raw else (json.dumps(value) + "\n").encode())
                return path

            paths = {
                "run": write("run.json", run),
                "archive": write("archive.zip", archive, raw=True),
                "metadata": write("metadata.json", inventory),
                "generation": write("generation.json", {"path": expected_generation_path}),
                "index": write("index.json", {"generations": []}),
                "state_ref": write("state-ref.json", {
                    "ref": "refs/heads/automation/upstream-catalogue-state",
                    "object": {"sha": "c" * 40},
                }),
                "state_tree": write("state-tree.json", {"sha": "d" * 40}),
            }
            roles: dict[str, dict[str, object]] = {}
            resolved: dict[str, pathlib.Path] = {}
            for role, key in (
                ("pipeline_run", "run"), ("pipeline_artifact_archive", "archive"),
                ("pipeline_artifact_metadata", "metadata"),
                ("processor_generation_api", "generation"), ("processor_index_api", "index"),
                ("processor_state_ref", "state_ref"), ("processor_state_tree", "state_tree"),
            ):
                roles[role] = {"input_id": role, "root": "evidence", "path": paths[key].name}
                resolved[role] = paths[key]

            class CompositionHelper:
                @staticmethod
                def validate_composition(*_args: object, **_kwargs: object) -> None:
                    return None

            original_import = MODULE.import_module

            def controlled_import(module_name: str, path: pathlib.Path):
                loaded = original_import(module_name, path)
                if module_name == "completeness_processor_contract":
                    loaded.load_canonical_update_pr = lambda _root: CompositionHelper
                    loaded.validate_processor_link_metadata = lambda *_args, **_kwargs: []
                return loaded

            patches = (
                mock.patch.object(
                    MODULE, "validate_stage_artifact",
                    side_effect=lambda **_kwargs: (artifact_metadata, archive),
                ),
                mock.patch.object(MODULE, "assert_main_ancestor", return_value=producer_head),
                mock.patch.object(MODULE, "source_lfs_binding", return_value=(
                    {"sha256": candidate_sha, "bytes": len(candidate_bytes)}, "e" * 64,
                )),
                mock.patch.object(MODULE, "validate_state_tree", return_value=state_bytes),
                mock.patch.object(MODULE, "import_module", side_effect=controlled_import),
            )

            def validate_stage() -> dict[str, object]:
                with patches[0], patches[1], patches[2], patches[3], patches[4]:
                    return MODULE.validate_processor_stage(
                        root=repository, roles=roles, resolved=resolved,
                        run=run, job={}, source=None,
                        current_registry={"sha256": candidate_sha, "bytes": len(candidate_bytes)},
                        evaluation_epoch="2026-10-06T00:00:00Z",
                    )

            unchanged = validate_stage()
            self.assertTrue(unchanged["current_input_contract_compatible"])
            self.assertEqual(unchanged["current_input_contract_changed_paths"], [])
            self.assertEqual(unchanged["processor_protocol"], "modern-ten-output-v1")

            derivation_path = repository / promotion_source.PROCESSOR_DERIVATION_COMPATIBILITY_FILE
            # _hardlink_worktree deliberately shares immutable fixture bytes.
            # Replace this one path before introducing current-only drift so
            # the source checkout remains byte-for-byte unchanged.
            derivation_bytes = derivation_path.read_bytes()
            derivation_path.unlink()
            derivation_path.write_bytes(derivation_bytes + b"\n# current-only drift\n")
            drifted = validate_stage()
            self.assertFalse(drifted["current_input_contract_compatible"])
            self.assertEqual(
                drifted["current_input_contract_changed_paths"],
                [promotion_source.PROCESSOR_DERIVATION_COMPATIBILITY_FILE],
            )
            self.assertEqual(
                drifted["current_input_contract_reason_code"],
                "modern_processor_input_contract_changed",
            )

            # A producer-reachable quarantine is a successful, report-only
            # result-only marker. It keeps the authenticated modern source
            # contract while correctly carrying no candidate/composition.
            derivation_path.write_bytes(derivation_bytes)

            def replace_terminal_artifact(
                terminal_status: str,
                output_names: tuple[str, ...],
                *, result_status: str | None = None,
                zip_names: tuple[str, ...] | None = None,
            ) -> None:
                nonlocal archive
                checkpoint["status"] = terminal_status
                terminal_reason = (
                    "composer_scope_omits_worker_outcomes"
                    if terminal_status == "quarantined" else f"fixture_{terminal_status}"
                )
                checkpoint["outcome"] = {"reason": terminal_reason}
                result_payload = {
                    "status": terminal_status if result_status is None else result_status,
                    "reason": terminal_reason,
                    "generation_id": generation_id,
                    "source_id": "data_go_kr", "producer_run_id": "100",
                    "processor_run_id": "70000000001-1",
                    "processor_artifact_run_id": "70000000001",
                    "processing_replay": False, "candidate_available": False,
                }
                bundle_payloads["upstream-catalogue-processing-result.json"] = (
                    promotion_source.canonical_json(result_payload)
                )
                new_digests: list[dict[str, object]] = []
                for member in output_names:
                    payload = bundle_payloads[member]
                    (bundle / member).write_bytes(payload)
                    new_digests.append({
                        "path": member, "bytes": len(payload),
                        "sha256": MODULE.sha256_bytes(payload),
                    })
                checkpoint["output_digests"] = new_digests
                checkpoint["output_artifact"]["bundle_manifest_sha256"] = MODULE.sha256_bytes(
                    promotion_source.canonical_json(new_digests)
                )
                seal(checkpoint)
                uploaded_copy = copy.deepcopy(checkpoint)
                uploaded_copy["output_artifact"]["artifact_id"] = None
                uploaded_copy["output_artifact"]["expires_at"] = "2026-11-03T07:02:45Z"
                seal(uploaded_copy)
                checkpoint_bytes = promotion_source.canonical_json(uploaded_copy) + b"\n"
                (bundle / promotion_source.RETAINED_PROCESSOR_CHECKPOINT_MEMBER).write_bytes(
                    checkpoint_bytes
                )
                archive_buffer = io.BytesIO()
                members = zip_names or (*output_names, promotion_source.RETAINED_PROCESSOR_CHECKPOINT_MEMBER)
                with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
                    for member in members:
                        member_path = bundle / member
                        if not member_path.exists() and member != promotion_source.RETAINED_PROCESSOR_CHECKPOINT_MEMBER:
                            member_path.write_bytes(b"unexpected report member\n")
                        archive_file.writestr(member, (bundle / member).read_bytes())
                archive = archive_buffer.getvalue()
                artifact_metadata["size_in_bytes"] = len(archive)
                artifact_metadata["digest"] = "sha256:" + MODULE.sha256_bytes(archive)
                paths["metadata"].write_text(json.dumps(inventory) + "\n", encoding="utf-8")
                state_bytes["processor_generation_api"] = json.dumps(checkpoint).encode()
                index_payload = json.loads(state_bytes["processor_index_api"])
                index_payload["generations"][0]["status"] = terminal_status
                state_bytes["processor_index_api"] = json.dumps(index_payload).encode()

            retry_outputs = tuple(promotion_source.REQUIRED_PROCESSOR_FILES)
            result_only = ("upstream-catalogue-processing-result.json",)
            replace_terminal_artifact("retry", retry_outputs)
            retry_bundle = promotion_source.validate_processor_bundle(
                checkpoint, bundle, {}, CompositionHelper,
                root=None, producer_head_sha=producer_head,
                defer_seoul_declaration=True,
            )
            self.assertEqual(retry_bundle["status"], "retry")
            self.assertNotIn("registry_sha256", retry_bundle)
            self.assertNotIn("composition_receipt", retry_bundle)
            # Retry is reportable only with the producer's full-ten shape;
            # the result-only marker is reserved for quarantined status.
            # The durable full-ten retry packet remains readable, but an
            # actual failed processor run cannot pass the success-only stage.
            run["conclusion"] = "failure"
            with self.assertRaisesRegex(ValueError, "processor run or artifact metadata"):
                validate_stage()
            run["conclusion"] = "success"
            replace_terminal_artifact("retry", result_only)
            with self.assertRaisesRegex(promotion_source.PromotionError, "inventory differs"):
                promotion_source._validate_retained_processor_output_inventory(
                    checkpoint, expected_outputs=retry_outputs,
                )

            quarantined = "quarantined"
            replace_terminal_artifact(quarantined, retry_outputs)
            with self.assertRaises(promotion_source.PromotionError):
                promotion_source._validate_retained_processor_output_inventory(
                    checkpoint, expected_outputs=result_only,
                )
            replace_terminal_artifact(quarantined, result_only)
            quarantined_stage = validate_stage()
            self.assertEqual(quarantined_stage["status"], quarantined)
            self.assertFalse(quarantined_stage["candidate_available"])
            self.assertIsNone(quarantined_stage["candidate_sha256"])
            self.assertIsNone(quarantined_stage["candidate_bytes"])
            self.assertIsNone(quarantined_stage["candidate_path"])
            self.assertFalse(quarantined_stage["current_eligible"])
            self.assertTrue(quarantined_stage["report_only_terminal"])
            self.assertIsNone(quarantined_stage["composition_sha256"])
            self.assertFalse(quarantined_stage["full_scope_fresh"])
            self.assertFalse(quarantined_stage["publication_allowed"])

            # A result/checkpoint disagreement is rejected even when both are
            # individually digest-bound into an otherwise exact marker ZIP.
            replace_terminal_artifact(quarantined, result_only, result_status="retry")
            with self.assertRaisesRegex(ValueError, "archive or producer-head input provenance"):
                validate_stage()

            # The exact result-only inventory cannot hide a missing ZIP member,
            # and no unlisted member is admitted into the modern report shape.
            replace_terminal_artifact(
                quarantined, result_only,
                zip_names=(promotion_source.RETAINED_PROCESSOR_CHECKPOINT_MEMBER,),
            )
            with self.assertRaisesRegex(ValueError, "archive or producer-head input provenance"):
                validate_stage()
            replace_terminal_artifact(
                quarantined, result_only,
                zip_names=(*result_only, promotion_source.RETAINED_PROCESSOR_CHECKPOINT_MEMBER,
                           "unexpected-extra.json"),
            )
            with self.assertRaisesRegex(ValueError, "archive or producer-head input provenance"):
                validate_stage()

    def test_actual_processor_retry_and_quarantine_report_content_boundaries(self) -> None:
        origin_main_ref = "refs/remotes/origin/main"
        def read_origin_main_oid() -> str | None:
            result = subprocess.run(
                [
                    "git", "for-each-ref", "--format=%(refname) %(objectname)",
                    origin_main_ref,
                ],
                cwd=ROOT,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode != 0:
                raise AssertionError(result.stderr)
            matching = [
                line.split(" ", 1)[1]
                for line in result.stdout.splitlines()
                if line.startswith(f"{origin_main_ref} ")
            ]
            if len(matching) > 1:
                raise AssertionError("origin/main ref inventory is ambiguous")
            return matching[0] if matching else None

        prior_origin_main_oid = read_origin_main_oid()

        def restore_origin_main() -> None:
            current_origin_main_oid = read_origin_main_oid()
            if current_origin_main_oid == prior_origin_main_oid:
                return
            if prior_origin_main_oid is None:
                if current_origin_main_oid is not None:
                    subprocess.run(
                        ["git", "update-ref", "-d", origin_main_ref, current_origin_main_oid],
                        cwd=ROOT,
                        check=True,
                    )
            elif current_origin_main_oid is None:
                subprocess.run(
                    ["git", "update-ref", origin_main_ref, prior_origin_main_oid],
                    cwd=ROOT,
                    check=True,
                )
            else:
                subprocess.run(
                    [
                        "git", "update-ref", origin_main_ref,
                        prior_origin_main_oid, current_origin_main_oid,
                    ],
                    cwd=ROOT,
                    check=True,
                )
            restored_origin_main_oid = read_origin_main_oid()
            if restored_origin_main_oid != prior_origin_main_oid:
                raise AssertionError(
                    "actual processor report test failed to restore its exact prior origin/main ref"
                )

        self.addCleanup(restore_origin_main)
        promotion = MODULE.import_module(
            "test_actual_terminal_report_promotion",
            ROOT / "scripts/run-canonical-update-promotion.py",
        )
        cache_source = MODULE.import_module(
            "test_actual_terminal_report_cache_source",
            ROOT / "scripts/upstream_catalogue_cache_source.py",
        )
        processor_tests = MODULE.import_module(
            "test_actual_terminal_report_processor_tests",
            ROOT / "tests/test_process_upstream_catalogue_candidate.py",
        )
        processor = processor_tests.MODULE
        composition_schema = promotion.load_object(
            ROOT / "schemas/datapan.catalogue-composition-receipt.v1.schema.json",
        )
        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{commit}"], cwd=ROOT, text=True,
        ).strip()
        expiry = "2026-12-01T00:00:00Z"

        def producer_fixture(run_id: str):
            fixture = processor_tests.UpstreamCatalogueProcessorTest("runTest")
            fixture.setUp()
            fixture.write_real_composer_inputs([], [fixture.real_link_row()], fixture.now)
            # write_real_composer_inputs points its reader at the real current
            # index; it never writes to the repository index.
            fixture.provider_index_path = ROOT / "data/provider-index.json"
            args = fixture.args(run_id="930100", **{
                "--processor-run-id": f"{run_id}-1",
                "--processor-artifact-run-id": run_id,
                "--max-attempts": 24,
                "--retries-per-detail": 1,
            })
            args.fixture_composer = None
            args.allow_fixture_composer = False
            args.composer = ROOT / "scripts/compose-upstream-catalogue-candidate.py"
            args.producer_head_sha = head_sha
            args.source_policy = ROOT / "policy/source-refresh.json"
            args.provider_index = ROOT / "data/provider-index.json"
            args.input_artifact_id = "930099"
            args.artifact_expires_at = expiry
            args.output_artifact_expires_at = expiry
            return fixture, args

        retry_fixture, retry_args = producer_fixture("930100")
        try:
            retry_code, retry_checkpoint = processor.process(
                retry_args,
                fetcher=lambda *_args: (_ for _ in ()).throw(TimeoutError("fixture timeout")),
            )
            self.assertEqual(retry_code, 2)
            self.assertEqual(retry_checkpoint["status"], "retry")
            self.assertEqual(
                [row["path"] for row in retry_checkpoint["output_digests"]],
                list(promotion.REQUIRED_PROCESSOR_FILES),
            )
            retry_composition = promotion._validate_report_only_retry_composition(
                retry_checkpoint,
                retry_fixture.output_dir,
                composition_schema,
                cache_source,
                cache_source_contract=None,
                root=ROOT,
                require_opaque_cache_contract=False,
            )
            self.assertIn(retry_composition["status"], {"no_safe_change", "no_change"})
            bound_retry_checkpoint = copy.deepcopy(retry_checkpoint)
            bound_retry_checkpoint["output_artifact"]["artifact_id"] = "930202"
            bound_retry_checkpoint = processor.seal_checkpoint(bound_retry_checkpoint)
            live_retry = promotion.validate_processor_bundle(
                bound_retry_checkpoint, retry_fixture.output_dir, composition_schema,
                promotion.load_canonical_update_pr(ROOT),
                root=ROOT, producer_head_sha=head_sha,
                defer_seoul_declaration=True,
            )
            self.assertEqual(live_retry["status"], "retry")
            self.assertNotIn("registry_sha256", live_retry)
            self.assertNotIn("composition_receipt", live_retry)

            with tempfile.TemporaryDirectory(prefix="terminal-retry-status-") as name:
                copied = pathlib.Path(name) / "bundle"
                shutil.copytree(retry_fixture.output_dir, copied)
                receipt_path = copied / "composition-receipt.json"
                receipt = json.loads(receipt_path.read_bytes())
                receipt["status"] = "ready_scoped"
                receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                with self.assertRaisesRegex(promotion.PromotionError, "composer status"):
                    promotion._validate_report_only_retry_composition(
                        retry_checkpoint, copied, composition_schema, cache_source,
                        cache_source_contract=None, root=ROOT,
                        require_opaque_cache_contract=False,
                    )

            with self.assertRaisesRegex(promotion.PromotionError, "full-ten output inventory"):
                promotion._validate_report_only_retry_composition(
                    {**retry_checkpoint, "output_digests": retry_checkpoint["output_digests"][:-1]},
                    retry_fixture.output_dir, composition_schema, cache_source,
                    cache_source_contract=None, root=ROOT,
                    require_opaque_cache_contract=False,
                )

            with tempfile.TemporaryDirectory(prefix="terminal-retry-context-") as name:
                copied = pathlib.Path(name) / "bundle"
                shutil.copytree(retry_fixture.output_dir, copied)
                context_path = copied / cache_source.CONTEXT_MEMBER
                context = json.loads(context_path.read_bytes())
                context["candidate"]["sha256"] = "f" * 64
                context_path.write_text(json.dumps(context), encoding="utf-8")
                tampered_checkpoint = copy.deepcopy(retry_checkpoint)
                context_row = next(
                    row for row in tampered_checkpoint["output_digests"]
                    if row["path"] == cache_source.CONTEXT_MEMBER
                )
                context_row.update({
                    "bytes": context_path.stat().st_size,
                    "sha256": MODULE.sha256_bytes(context_path.read_bytes()),
                })
                tampered_checkpoint["output_artifact"]["bundle_manifest_sha256"] = (
                    MODULE.sha256_bytes(promotion.canonical_json(tampered_checkpoint["output_digests"]))
                )
                with self.assertRaisesRegex(promotion.PromotionError, "cache context"):
                    promotion._validate_report_only_retry_composition(
                        tampered_checkpoint, copied, composition_schema, cache_source,
                        cache_source_contract=None, root=ROOT,
                        require_opaque_cache_contract=False,
                    )
        finally:
            retry_fixture.tearDown()

        quarantine_fixture, quarantine_args = producer_fixture("930101")
        try:
            # Give the stage fixture the producer's real live source scope and
            # a closed local A-admission envelope; no GitHub transport is used.
            quarantine_args.execution_mode = "live"
            quarantine_args.now = None
            quarantine_args.source_scope = "aggregate_supported_catalog"
            quarantine_args.producer_head_sha = head_sha
            quarantine_args.producer_run_attempt = 1
            quarantine_args.producer_run_started_at = "2026-10-01T09:00:00Z"
            quarantine_args.producer_run_completed_at = "2026-10-01T09:30:00Z"
            quarantine_args.producer_observe_job_started_at = "2026-10-01T09:10:00Z"
            quarantine_args.producer_observe_job_completed_at = "2026-10-01T09:20:00Z"
            quarantine_args.producer_artifact_created_at = "2026-10-01T09:15:00Z"
            quarantine_args.producer_event = "schedule"
            quarantine_args.artifact_name = "upstream-catalog-refresh-930100"
            quarantine_archive = quarantine_fixture.root / "collector-artifact.zip"
            with zipfile.ZipFile(quarantine_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
                archive_file.write(quarantine_args.candidate, "candidate.registry.json")
                archive_file.write(quarantine_args.refresh_evidence, "upstream-refresh-evidence.json")
                archive_file.write(quarantine_args.diff, "catalog-diff.json")
            quarantine_archive_raw = quarantine_archive.read_bytes()
            quarantine_archive_sha = MODULE.sha256_bytes(quarantine_archive_raw)
            quarantine_args.producer_artifact_digest_sha256 = quarantine_archive_sha
            quarantine_args.producer_artifact_size_bytes = len(quarantine_archive_raw)
            quarantine_args.collector_archive = quarantine_archive
            quarantine_args.producer_run_url = (
                "https://github.com/StatPan/datapan-registry/actions/runs/930100"
            )
            admission = {
                "schema_version": "datapan.upstream-catalogue-admission-envelope.v1",
                "repository": "StatPan/datapan-registry", "producer_run_id": "930100",
                "run_attempt": 1, "head_sha": head_sha,
                "run_started_at": quarantine_args.producer_run_started_at,
                "run_completed_at": quarantine_args.producer_run_completed_at,
                "observe_job_started_at": quarantine_args.producer_observe_job_started_at,
                "observe_job_completed_at": quarantine_args.producer_observe_job_completed_at,
                "artifact_id": "930099", "artifact_name": quarantine_args.artifact_name,
                "artifact_expires_at": expiry,
                "artifact_created_at": quarantine_args.producer_artifact_created_at,
                "artifact_digest_sha256": quarantine_archive_sha,
                "artifact_size_bytes": len(quarantine_archive_raw),
                "archive_sha256": quarantine_archive_sha,
                "archive_size_bytes": len(quarantine_archive_raw),
                "observed_at": json.loads(quarantine_args.refresh_evidence.read_bytes())["observed_at"],
                "refresh_evidence_sha256": MODULE.sha256_bytes(
                    quarantine_args.refresh_evidence.read_bytes(),
                ),
                "event": "schedule",
            }
            admission_path = quarantine_fixture.root / "collector-admission.json"
            admission_path.write_text(
                json.dumps(admission, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            quarantine_args.collector_admission_file = admission_path
            page_bytes = (
                '<button type="button" onclick="fn_goUrlLink(\'2\')">Open</button>'
                '<input type="hidden" id="publicDataPk" value="2">'
                '<input type="hidden" id="publicDataDetailPk" value="uddi:fixture-2">'
            ).encode("utf-8")
            resolver_bytes = json.dumps({
                "publicDataDetailPk": "uddi:fixture-2",
                "linkUrl": "https://api.forest.go.kr/catalogue/landing",
                "status": True,
            }, separators=(",", ":")).encode("utf-8")

            def page_fetch(url: str, _timeout: float):
                return processor.DetailPageObservation(
                    body=page_bytes.decode("utf-8"), page_url=url, effective_url=url,
                    page_sha256=processor.sha256_bytes(page_bytes),
                    observed_at=quarantine_fixture.now, page_bytes=page_bytes,
                )

            def resolver_fetch(url: str, _timeout: float):
                return processor.LinkResolverObservation(
                    body=resolver_bytes, request_url=url, effective_url=url,
                    observed_at=quarantine_fixture.now,
                )

            actual_composer_call = processor.call_composer

            def omit_resolver_outcome_from_scope(*args: object, **kwargs: object):
                code, reason = actual_composer_call(*args, **kwargs)
                if code == 0:
                    receipt_path = kwargs["output_dir"] / "composition-receipt.json"
                    receipt = json.loads(receipt_path.read_bytes())
                    receipt["scope"]["retained_pending_api_keys"] = []
                    receipt["scope"]["quarantined_api_keys"] = []
                    receipt_path.write_text(
                        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                return code, reason

            with mock.patch.object(processor, "call_composer", side_effect=omit_resolver_outcome_from_scope):
                quarantine_code, quarantine_checkpoint = processor.process(
                    quarantine_args,
                    fetcher=page_fetch,
                    resolver_fetcher=resolver_fetch,
                    sleeper=lambda _delay: None,
                )
            self.assertEqual(quarantine_code, 3)
            self.assertEqual(quarantine_checkpoint["status"], "quarantined")
            self.assertEqual(
                quarantine_checkpoint["outcome"]["reason"],
                "composer_scope_omits_worker_outcomes",
            )
            self.assertIsInstance(quarantine_checkpoint["detail_records"][0].get("link_metadata"), dict)
            self.assertEqual(
                [row["path"] for row in quarantine_checkpoint["output_digests"]],
                list(promotion.RETAINED_PROCESSOR_RESULT_ONLY_FILES),
            )
            quarantine_result_path = (
                quarantine_fixture.output_dir / "upstream-catalogue-processing-result.json"
            )
            quarantine_result_raw = quarantine_result_path.read_bytes()

            def seal(value: dict[str, Any]) -> dict[str, Any]:
                value.pop("checkpoint_sha256", None)
                value["checkpoint_sha256"] = MODULE.sha256_bytes(promotion.canonical_json(value))
                return value

            def report_fixture(
                checkpoint_value: dict[str, Any],
                *,
                result_override: Mapping[str, Any] | None = None,
                add_inventory_row: bool = False,
            ):
                durable = copy.deepcopy(checkpoint_value)
                durable["output_artifact"]["artifact_id"] = "930201"
                processor_artifact_run_id = int(durable["output_artifact"]["run_id"])
                if result_override is not None:
                    result_raw = promotion.canonical_json(dict(result_override)) + b"\n"
                    quarantine_result_path.write_bytes(result_raw)
                    result_row = durable["output_digests"][0]
                    result_row.update({"bytes": len(result_raw), "sha256": MODULE.sha256_bytes(result_raw)})
                    durable["output_artifact"]["bundle_manifest_sha256"] = MODULE.sha256_bytes(
                        promotion.canonical_json(durable["output_digests"]),
                    )
                else:
                    quarantine_result_path.write_bytes(quarantine_result_raw)
                if add_inventory_row:
                    durable["output_digests"].append({
                        "path": "unexpected-extra.json", "bytes": 1, "sha256": "a" * 64,
                    })
                    durable["output_artifact"]["bundle_manifest_sha256"] = MODULE.sha256_bytes(
                        promotion.canonical_json(durable["output_digests"]),
                    )
                durable = seal(durable)
                uploaded = copy.deepcopy(durable)
                uploaded["output_artifact"]["artifact_id"] = None
                uploaded = seal(uploaded)
                result_raw = quarantine_result_path.read_bytes()
                archive_buffer = io.BytesIO()
                with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
                    archive_file.writestr("upstream-catalogue-processing-result.json", result_raw)
                    archive_file.writestr(
                        cache_source.CHECKPOINT_RECEIPT_MEMBER,
                        promotion.canonical_json(uploaded) + b"\n",
                    )
                archive_raw = archive_buffer.getvalue()
                metadata = {
                    "id": 930201,
                    "name": durable["output_artifact"]["name"],
                    "size_in_bytes": len(archive_raw),
                    "digest": "sha256:" + MODULE.sha256_bytes(archive_raw),
                    "expired": False,
                    "created_at": "2026-10-01T11:00:00Z",
                    "updated_at": "2026-10-01T11:01:00Z",
                    "expires_at": durable["output_artifact"]["expires_at"],
                    "workflow_run": {
                        "id": processor_artifact_run_id, "head_sha": head_sha, "head_branch": "main",
                        "repository_id": 1, "head_repository_id": 1,
                    },
                }
                run = {
                    "id": processor_artifact_run_id, "run_attempt": 1,
                    "name": promotion.PROCESSOR_WORKFLOW_NAME,
                    "path": promotion.PROCESSOR_WORKFLOW_PATH,
                    "head_branch": "main", "head_sha": head_sha,
                    "status": "completed", "conclusion": "success", "event": "schedule",
                    "repository": {"id": 1, "full_name": "StatPan/datapan-registry"},
                    "head_repository": {"id": 1, "full_name": "StatPan/datapan-registry"},
                }
                deadline = time.monotonic() + cache_source.MAX_WHOLE_ATTEMPT_SECONDS
                reader = cache_source.LocalGitSourceReader(cwd=ROOT, deadline_monotonic=deadline)
                contract = promotion.classify_retained_processor_bundle_for_report(
                    durable, run, {"total_count": 1, "artifacts": [metadata]}, archive_raw,
                    repository="StatPan/datapan-registry", default_branch="main",
                    cache_source_helper=cache_source, source_reader=reader,
                    deadline_monotonic=deadline,
                )
                return durable, archive_raw, metadata, contract, reader

            def validate_scope_stage(
                durable: dict[str, Any], archive_raw: bytes,
                stage_fixture: Any = quarantine_fixture,
            ) -> dict[str, Any]:
                """Run the real stage validators over closed local API evidence."""
                artifact_run_id = int(durable["output_artifact"]["run_id"])
                run = {
                    "id": artifact_run_id, "run_attempt": 1,
                    "workflow_id": MODULE.PIPELINE_WORKFLOWS["processor"]["workflow_id"],
                    "name": promotion.PROCESSOR_WORKFLOW_NAME,
                    "path": promotion.PROCESSOR_WORKFLOW_PATH,
                    "head_branch": "main", "head_sha": head_sha,
                    "status": "completed", "conclusion": "success", "event": "schedule",
                    "repository": {"id": 1278568329, "full_name": "StatPan/datapan-registry"},
                    "head_repository": {"id": 1278568329, "full_name": "StatPan/datapan-registry"},
                }
                job = {
                    "started_at": "2026-10-01T09:59:00Z",
                    "completed_at": "2026-10-01T11:01:00Z",
                }
                expires_at = durable["output_artifact"]["expires_at"]
                artifact = {
                    "id": int(durable["output_artifact"]["artifact_id"]),
                    "name": durable["output_artifact"]["name"],
                    "size_in_bytes": len(archive_raw),
                    "digest": "sha256:" + MODULE.sha256_bytes(archive_raw),
                    "expired": False,
                    "created_at": "2026-10-01T10:01:00Z",
                    "updated_at": "2026-10-01T10:02:00Z",
                    "expires_at": expires_at,
                    "workflow_run": {
                        "id": artifact_run_id, "head_sha": head_sha, "head_branch": "main",
                        "repository_id": 1278568329, "head_repository_id": 1278568329,
                    },
                }
                generation_id = durable["generation_id"]
                generation_path = (
                    ".datapan/upstream-catalogue-state/sources/data_go_kr/generations/"
                    f"{generation_id}.json"
                )
                state_objects = {
                    ".datapan/upstream-catalogue-state/state-root.json": json.dumps({
                        "repository": "StatPan/datapan-registry", "source_id": "data_go_kr",
                        "state_root": ".datapan/upstream-catalogue-state",
                        "schema_version": "datapan.upstream-catalogue-state-root.v1",
                    }, separators=(",", ":")).encode(),
                    ".datapan/upstream-catalogue-state/sources/data_go_kr/index.json": json.dumps({
                        "generations": [{
                            "generation_id": generation_id,
                            "checkpoint": f"{generation_id}.json",
                            "status": "quarantined",
                            "candidate_sha256": durable["generation_inputs"]["candidate_sha256"],
                        }],
                    }, separators=(",", ":")).encode(),
                    generation_path: promotion.canonical_json(durable),
                }

                def contents_api(path: str, raw: bytes) -> dict[str, Any]:
                    return {
                        "type": "file", "encoding": "base64", "path": path,
                        "content": __import__("base64").b64encode(raw).decode("ascii"),
                        "size": len(raw), "sha": MODULE.git_blob_sha1(raw),
                    }

                tree_sha = "d" * 40
                commit_sha = "c" * 40
                tree = {
                    "sha": tree_sha, "truncated": False,
                    "tree": [
                        {"path": path, "type": "blob", "sha": MODULE.git_blob_sha1(raw)}
                        for path, raw in state_objects.items()
                    ],
                }
                api_values = {
                    "pipeline_run": run,
                    "pipeline_jobs": {
                        "total_count": 1,
                        "jobs": [{
                            "id": 930202, "run_id": artifact_run_id, "run_attempt": 1,
                            "name": "Process", "status": "completed",
                            "conclusion": "success", "started_at": job["started_at"],
                            "completed_at": job["completed_at"],
                        }],
                    },
                    "pipeline_artifact_metadata": {"total_count": 1, "artifacts": [artifact]},
                    "pipeline_artifact_archive": archive_raw,
                    "processor_state_ref": {
                        "ref": "refs/heads/automation/upstream-catalogue-state",
                        "object": {"type": "commit", "sha": commit_sha},
                    },
                    "processor_state_commit": {"sha": commit_sha, "tree": {"sha": tree_sha}},
                    "processor_state_tree": tree,
                    "processor_state_root_api": contents_api(
                        ".datapan/upstream-catalogue-state/state-root.json",
                        state_objects[".datapan/upstream-catalogue-state/state-root.json"],
                    ),
                    "processor_index_api": contents_api(
                        ".datapan/upstream-catalogue-state/sources/data_go_kr/index.json",
                        state_objects[".datapan/upstream-catalogue-state/sources/data_go_kr/index.json"],
                    ),
                    "processor_generation_api": {
                        **contents_api(generation_path, state_objects[generation_path]),
                        "path": generation_path,
                    },
                }
                evidence = stage_fixture.root / "stage-evidence"
                evidence.mkdir(exist_ok=True)
                roles: dict[str, dict[str, Any]] = {}
                resolved: dict[str, pathlib.Path] = {}
                for role, value in api_values.items():
                    if isinstance(value, bytes):
                        raw = value
                    else:
                        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    path = evidence / f"{role}.json"
                    path.write_bytes(raw)
                    roles[role] = {
                        "input_id": role, "root": "evidence", "path": path.name,
                        "bytes": len(raw), "sha256": MODULE.sha256_bytes(raw),
                    }
                    resolved[role] = path
                subprocess.run(
                    ["git", "update-ref", "refs/remotes/origin/main", head_sha],
                    cwd=ROOT, check=True,
                )
                current_artifact, _manifest_sha = MODULE.source_lfs_binding(ROOT, head_sha)
                return MODULE.validate_processor_stage(
                    root=ROOT, roles=roles, resolved=resolved, run=run, job=job, source=None,
                    current_registry={
                        "sha256": current_artifact["sha256"], "bytes": current_artifact["bytes"],
                    },
                    evaluation_epoch="2026-10-07T00:00:00Z",
                )

            durable, archive, metadata, contract, reader = report_fixture(quarantine_checkpoint)
            with tempfile.TemporaryDirectory(prefix="terminal-quarantine-report-") as name:
                bundle_dir = pathlib.Path(name) / "bundle"
                MODULE.extract_bounded_processor_bundle(
                    archive, bundle_dir, expected_members=set(contract.expected_members),
                    max_archive_bytes=promotion.MAX_PROCESSOR_ARCHIVE_BYTES,
                    max_expanded_bytes=promotion.MAX_PROCESSOR_BUNDLE_BYTES,
                )
                composition_helper = promotion.load_canonical_update_pr(ROOT)
                with self.assertRaisesRegex(
                    promotion.PromotionError, "missing its enrichment evidence",
                ):
                    promotion.validate_processor_bundle(
                        durable, bundle_dir, composition_schema, composition_helper,
                        root=ROOT, producer_head_sha=head_sha,
                        cache_source_helper=cache_source,
                        cache_source_contract=contract.cache_source_contract,
                    )
                report_bundle = promotion.validate_retained_processor_bundle_for_report(
                    contract, durable, bundle_dir, composition_schema, composition_helper,
                    root=ROOT, producer_head_sha=head_sha, source_reader=reader,
                )
                facts = report_bundle.as_report_facts()
                self.assertEqual(facts["status"], "quarantined")
                self.assertEqual(facts["reason"], "composer_scope_omits_worker_outcomes")
                self.assertFalse(facts.get("candidate_available", False))
                self.assertNotIn("composition_receipt", facts)
                self.assertEqual(facts["contract_diagnostics"][0]["api_key"]["id"], "2")
                staged = validate_scope_stage(durable, archive)
                self.assertEqual(staged["status"], "quarantined")
                self.assertTrue(staged["report_only_terminal"])
                self.assertFalse(staged["candidate_available"])
                self.assertIsNone(staged["composition_sha256"])
                self.assertFalse(staged["current_eligible"])
                self.assertFalse(staged["publication_allowed"])

            malformed = copy.deepcopy(quarantine_checkpoint)
            malformed["detail_records"][0]["link_metadata"]["resolver"]["resolved_url"] = (
                "https://unregistered.invalid/catalogue/landing"
            )
            bad_durable, bad_archive, _bad_metadata, bad_contract, bad_reader = report_fixture(malformed)
            with tempfile.TemporaryDirectory(prefix="terminal-quarantine-malformed-") as name:
                bundle_dir = pathlib.Path(name) / "bundle"
                MODULE.extract_bounded_processor_bundle(
                    bad_archive, bundle_dir, expected_members=set(bad_contract.expected_members),
                    max_archive_bytes=promotion.MAX_PROCESSOR_ARCHIVE_BYTES,
                    max_expanded_bytes=promotion.MAX_PROCESSOR_BUNDLE_BYTES,
                )
                with self.assertRaisesRegex(promotion.PromotionError, "strict row validation"):
                    promotion.validate_retained_processor_bundle_for_report(
                        bad_contract, bad_durable, bundle_dir, composition_schema,
                        promotion.load_canonical_update_pr(ROOT), root=ROOT,
                        producer_head_sha=head_sha, source_reader=bad_reader,
                    )

            wrong_reason = copy.deepcopy(quarantine_checkpoint)
            wrong_reason["outcome"]["reason"] = "another_quarantine_reason"
            old_result = json.loads(
                (quarantine_fixture.output_dir / "upstream-catalogue-processing-result.json").read_bytes(),
            )
            wrong_result = copy.deepcopy(old_result)
            wrong_result["reason"] = "another_quarantine_reason"
            wrong_durable, wrong_archive, _wrong_meta, wrong_contract, wrong_reader = report_fixture(
                wrong_reason, result_override=wrong_result,
            )
            with tempfile.TemporaryDirectory(prefix="terminal-quarantine-reason-") as name:
                bundle_dir = pathlib.Path(name) / "bundle"
                MODULE.extract_bounded_processor_bundle(
                    wrong_archive, bundle_dir, expected_members=set(wrong_contract.expected_members),
                    max_archive_bytes=promotion.MAX_PROCESSOR_ARCHIVE_BYTES,
                    max_expanded_bytes=promotion.MAX_PROCESSOR_BUNDLE_BYTES,
                )
                with self.assertRaisesRegex(promotion.PromotionError, "missing its enrichment evidence"):
                    promotion.validate_retained_processor_bundle_for_report(
                        wrong_contract, wrong_durable, bundle_dir, composition_schema,
                        promotion.load_canonical_update_pr(ROOT), root=ROOT,
                        producer_head_sha=head_sha, source_reader=wrong_reader,
                    )

            with self.assertRaises(promotion.PromotionError):
                report_fixture(quarantine_checkpoint, add_inventory_row=True)
            wrong_status = copy.deepcopy(quarantine_checkpoint)
            wrong_status["status"] = "retry"
            with self.assertRaises(promotion.PromotionError):
                report_fixture(wrong_status)

            # Ordinary producer result-only markers keep the generic report
            # validation path.  Start with a real retry generation, then close
            # it through the processor's authenticated expired-input path.
            ordinary_fixture, ordinary_args = producer_fixture("930102")
            try:
                from datetime import datetime, timedelta, timezone

                def utc_text(value: datetime) -> str:
                    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

                ordinary_origin = datetime.now(timezone.utc).replace(microsecond=0)
                ordinary_observed_at = utc_text(ordinary_origin - timedelta(minutes=3))
                ordinary_fixture.now = ordinary_observed_at
                ordinary_fixture.write_real_composer_inputs(
                    [], [ordinary_fixture.real_link_row()], ordinary_observed_at,
                )
                ordinary_args.now = None
                ordinary_expiry = utc_text(ordinary_origin + timedelta(seconds=10))
                ordinary_args.execution_mode = "live"
                ordinary_args.source_scope = "aggregate_supported_catalog"
                ordinary_args.producer_head_sha = head_sha
                ordinary_args.producer_run_attempt = 1
                ordinary_args.producer_run_started_at = utc_text(ordinary_origin - timedelta(minutes=5))
                ordinary_args.producer_run_completed_at = utc_text(ordinary_origin - timedelta(minutes=1))
                ordinary_args.producer_observe_job_started_at = utc_text(ordinary_origin - timedelta(minutes=4))
                ordinary_args.producer_observe_job_completed_at = utc_text(ordinary_origin - timedelta(minutes=2))
                ordinary_args.producer_artifact_created_at = ordinary_observed_at
                ordinary_args.producer_event = "schedule"
                ordinary_args.artifact_name = "upstream-catalog-refresh-930100"
                ordinary_args.artifact_expires_at = ordinary_expiry
                ordinary_archive_path = ordinary_fixture.root / "collector-artifact.zip"
                with zipfile.ZipFile(ordinary_archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
                    archive_file.write(ordinary_args.candidate, "candidate.registry.json")
                    archive_file.write(ordinary_args.refresh_evidence, "upstream-refresh-evidence.json")
                    archive_file.write(ordinary_args.diff, "catalog-diff.json")
                ordinary_archive_raw = ordinary_archive_path.read_bytes()
                ordinary_archive_sha = MODULE.sha256_bytes(ordinary_archive_raw)
                ordinary_args.producer_artifact_digest_sha256 = ordinary_archive_sha
                ordinary_args.producer_artifact_size_bytes = len(ordinary_archive_raw)
                ordinary_args.collector_archive = ordinary_archive_path
                ordinary_args.producer_run_url = (
                    "https://github.com/StatPan/datapan-registry/actions/runs/930100"
                )
                ordinary_admission = {
                    "schema_version": "datapan.upstream-catalogue-admission-envelope.v1",
                    "repository": "StatPan/datapan-registry", "producer_run_id": "930100",
                    "run_attempt": 1, "head_sha": head_sha,
                    "run_started_at": ordinary_args.producer_run_started_at,
                    "run_completed_at": ordinary_args.producer_run_completed_at,
                    "observe_job_started_at": ordinary_args.producer_observe_job_started_at,
                    "observe_job_completed_at": ordinary_args.producer_observe_job_completed_at,
                    "artifact_id": "930099", "artifact_name": ordinary_args.artifact_name,
                    "artifact_expires_at": ordinary_expiry,
                    "artifact_created_at": ordinary_args.producer_artifact_created_at,
                    "artifact_digest_sha256": ordinary_archive_sha,
                    "artifact_size_bytes": len(ordinary_archive_raw),
                    "archive_sha256": ordinary_archive_sha,
                    "archive_size_bytes": len(ordinary_archive_raw),
                    "observed_at": ordinary_observed_at,
                    "refresh_evidence_sha256": MODULE.sha256_bytes(
                        ordinary_args.refresh_evidence.read_bytes(),
                    ),
                    "event": "schedule",
                }
                ordinary_admission_path = ordinary_fixture.root / "collector-admission.json"
                ordinary_admission_path.write_text(
                    json.dumps(ordinary_admission, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                ordinary_args.collector_admission_file = ordinary_admission_path
                ordinary_code, ordinary_retry = processor.process(
                    ordinary_args,
                    fetcher=lambda *_args: (_ for _ in ()).throw(TimeoutError("fixture timeout")),
                )
                self.assertEqual(ordinary_code, 2)
                seconds_until_expired = (
                    datetime.fromisoformat(ordinary_expiry.replace("Z", "+00:00"))
                    - datetime.now(timezone.utc)
                ).total_seconds()
                if seconds_until_expired > 0:
                    time.sleep(seconds_until_expired + 0.1)
                expired_args = copy.copy(ordinary_args)
                expired_args.processor_run_id = "930103-1"
                expired_args.processor_artifact_run_id = "930103"
                expired_args.target_generation_id = ordinary_retry["generation_id"]
                expired_args.input_error = "input_expired"
                expired_args.candidate = expired_args.diff = expired_args.refresh_evidence = None
                expired_args.resume_enrichment_evidence = None
                ordinary_code, ordinary_checkpoint = processor.process(
                    expired_args,
                    fetcher=lambda *_args: self.fail("expired-input marker must not fetch"),
                )
                self.assertEqual(ordinary_code, 3)
                self.assertEqual(ordinary_checkpoint["status"], "quarantined")
                self.assertEqual(ordinary_checkpoint["outcome"]["reason"], "input_expired")
                self.assertEqual(
                    [row["path"] for row in ordinary_checkpoint["output_digests"]],
                    list(promotion.RETAINED_PROCESSOR_RESULT_ONLY_FILES),
                )
                ordinary_result = json.loads(
                    (ordinary_fixture.output_dir / "upstream-catalogue-processing-result.json").read_bytes(),
                )
                ordinary_durable, ordinary_archive, _ordinary_metadata, ordinary_contract, ordinary_reader = (
                    report_fixture(ordinary_checkpoint, result_override=ordinary_result)
                )
                with tempfile.TemporaryDirectory(prefix="terminal-ordinary-quarantine-report-") as name:
                    ordinary_bundle_dir = pathlib.Path(name) / "bundle"
                    MODULE.extract_bounded_processor_bundle(
                        ordinary_archive, ordinary_bundle_dir,
                        expected_members=set(ordinary_contract.expected_members),
                        max_archive_bytes=promotion.MAX_PROCESSOR_ARCHIVE_BYTES,
                        max_expanded_bytes=promotion.MAX_PROCESSOR_BUNDLE_BYTES,
                    )
                    ordinary_report = promotion.validate_retained_processor_bundle_for_report(
                        ordinary_contract, ordinary_durable, ordinary_bundle_dir,
                        composition_schema, promotion.load_canonical_update_pr(ROOT),
                        root=ROOT, producer_head_sha=head_sha, source_reader=ordinary_reader,
                    )
                    ordinary_facts = ordinary_report.as_report_facts()
                    self.assertEqual(ordinary_facts["status"], "quarantined")
                    self.assertEqual(ordinary_facts["reason"], "input_expired")
                    self.assertFalse(ordinary_facts.get("candidate_available", False))
                    self.assertNotIn("composition_receipt", ordinary_facts)
                ordinary_stage = validate_scope_stage(
                    ordinary_durable, ordinary_archive, ordinary_fixture,
                )
                self.assertEqual(ordinary_stage["status"], "quarantined")
                self.assertTrue(ordinary_stage["report_only_terminal"])
                self.assertFalse(ordinary_stage["candidate_available"])
                self.assertFalse(ordinary_stage["current_eligible"])
                self.assertFalse(ordinary_stage["publication_allowed"])
            finally:
                ordinary_fixture.tearDown()
        finally:
            quarantine_fixture.tearDown()

    def test_retained_report_terminal_pairing_preserves_historical_eight_profile(self) -> None:
        promotion = MODULE.import_module(
            "test_terminal_pairing_historical_promotion_source",
            ROOT / "scripts/run-canonical-update-promotion.py",
        )

        class CacheSourceSentinel:
            CACHE_SOURCE_PROFILE = "self-contained-original-candidate-v1"

        legacy_outputs = tuple(promotion.HISTORICAL_RETAINED_PROCESSOR_FILES)
        digests = [
            {"path": name, "bytes": 1, "sha256": "a" * 64}
            for name in legacy_outputs
        ]
        checkpoint: dict[str, object] = {
            "generation_id": "b" * 64,
            "checkpoint_sha256": "c" * 64,
            "status": "ready",
            "output_digests": digests,
            "output_artifact": {
                "artifact_id": "201",
                "bundle_manifest_sha256": MODULE.sha256_bytes(promotion.canonical_json(digests)),
            },
        }
        with (
            mock.patch.object(promotion, "_retained_processor_checkpoint_identity", return_value=({}, {})),
            mock.patch.object(
                promotion, "_retained_processor_native_identity",
                return_value=("d" * 40, "70000000001", 1, "201", "e" * 64),
            ),
            mock.patch.object(promotion, "_authenticate_historical_retained_processor_source", return_value={}),
        ):
            contract = promotion.classify_retained_processor_bundle_for_report(
                checkpoint, {}, {}, b"fixture archive",
                repository="StatPan/datapan-registry", default_branch="main",
                cache_source_helper=CacheSourceSentinel(), source_reader=object(),
                deadline_monotonic=1.0,
            )
            self.assertEqual(contract.protocol, promotion.HISTORICAL_RETAINED_PROCESSOR_PROTOCOL)
            self.assertEqual(
                contract.expected_members,
                (*legacy_outputs, promotion.RETAINED_PROCESSOR_CHECKPOINT_MEMBER),
            )
            for terminal_status in ("retry", "quarantined"):
                checkpoint["status"] = terminal_status
                with self.assertRaisesRegex(promotion.PromotionError, "historical report protocol"):
                    promotion.classify_retained_processor_bundle_for_report(
                        checkpoint, {}, {}, b"fixture archive",
                        repository="StatPan/datapan-registry", default_branch="main",
                        cache_source_helper=CacheSourceSentinel(), source_reader=object(),
                        deadline_monotonic=1.0,
                    )

    def test_partial_pipeline_rejects_tampered_present_pointer_without_ack(self) -> None:
        """Missing ACK is conservative, but it cannot hide malformed publisher evidence."""
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id
            or (
                item.get("subject", {}).get("stage") != "acknowledgement"
                and not item["role"].startswith("acknowledgement_")
            )
        ]
        pointer = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "publication_pointer_immutable"
        )

        with tempfile.TemporaryDirectory(prefix="completeness-partial-publisher-") as name:
            input_root = pathlib.Path(name)
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            valid_report = MODULE.build_report(
                root=ROOT, input_root=input_root, input_index_path=index_path,
            )
            operation_row = next(row for row in valid_report["scopes"] if row["scope_id"] == operation_id)
            pipeline_facet = next(facet for facet in operation_row["facets"] if facet["facet_id"] == "specification_pipeline")
            publication_facet = next(
                facet for facet in operation_row["facets"]
                if facet["facet_id"] == "immutable_publication_read_back"
            )
            self.assertEqual(pipeline_facet["state"], "historical")
            self.assertEqual(publication_facet["state"], "historical")
            self.assertEqual(publication_facet["details"]["missing_stages"], ["acknowledgement"])
            self.assertEqual(publication_facet["details"]["publisher_attempt"]["run_id"], "37199628001")
            self.assertTrue(publication_facet["details"]["native_readback"])
            self.assertIn(
                "publication_acknowledgement_missing",
                {item["code"] for item in publication_facet["missing_evidence"]},
            )
            self.assertNotIn("current_release_subject_applicable", publication_facet["details"])

            tampered = b'{"tampered_pointer": true}\n'
            (input_root / "tampered-pointer.json").write_bytes(tampered)
            pointer.update({
                "root": "evidence", "path": "tampered-pointer.json",
                "bytes": len(tampered), "sha256": MODULE.sha256_bytes(tampered),
            })
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "pointer|distribution|publication"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {
                "rollup.json": json_output,
                "rollup.md": markdown_output,
            }
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT),
                    "--input-root", str(input_root),
                    "--input-index", str(index_path),
                    "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md",
                    "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

    def test_partial_pipeline_rejects_tampered_present_readback_without_ack(self) -> None:
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id
            or (
                item.get("subject", {}).get("stage") != "acknowledgement"
                and not item["role"].startswith("acknowledgement_")
            )
        ]
        readback_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "publication_anonymous_payload"
        )

        with tempfile.TemporaryDirectory(prefix="completeness-partial-readback-") as name:
            input_root = pathlib.Path(name)
            readback = json.loads((ROOT / readback_item["path"]).read_bytes())
            detail = next(
                row["detail"] for row in readback["checks"]
                if row.get("check") == "immutable_registry_stream_matches_expected_sha_and_size"
            )
            detail["sha256"] = "a" * 64
            raw = (json.dumps(readback, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            (input_root / "tampered-readback.json").write_bytes(raw)
            readback_item.update({
                "root": "evidence", "path": "tampered-readback.json",
                "bytes": len(raw), "sha256": MODULE.sha256_bytes(raw),
            })
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "anonymous read-back"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

    def test_missing_health_does_not_hide_malformed_present_source_archive(self) -> None:
        """Validate each present stage's local artifact before returning a missing-chain result."""
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id
            or item.get("subject", {}).get("stage") != "health"
        ]
        source_archive = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id
            and item.get("subject", {}).get("stage") == "source"
            and item["role"] == "pipeline_artifact_archive"
        )
        source_metadata = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id
            and item.get("subject", {}).get("stage") == "source"
            and item["role"] == "pipeline_artifact_metadata"
        )

        with tempfile.TemporaryDirectory(prefix="completeness-partial-source-") as name:
            input_root = pathlib.Path(name)
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            report = MODULE.build_report(
                root=ROOT, input_root=input_root, input_index_path=index_path,
            )
            operation = next(row for row in report["scopes"] if row["scope_id"] == operation_id)
            self.assertEqual(operation["evaluated_claims"], {"complete": False, "current": False, "updated": False})

            malformed = b"not-a-zip"
            (input_root / "malformed-source.zip").write_bytes(malformed)
            source_archive.update({
                "root": "evidence", "path": "malformed-source.zip",
                "bytes": len(malformed), "sha256": MODULE.sha256_bytes(malformed),
            })
            metadata_path = ROOT / source_metadata["path"]
            metadata = json.loads(metadata_path.read_bytes())
            metadata["artifacts"][0]["size_in_bytes"] = len(malformed)
            metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(malformed)}"
            metadata_raw = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            (input_root / "malformed-source-metadata.json").write_bytes(metadata_raw)
            source_metadata.update({
                "root": "evidence", "path": "malformed-source-metadata.json",
                "bytes": len(metadata_raw), "sha256": MODULE.sha256_bytes(metadata_raw),
            })
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")

            with self.assertRaisesRegex(ValueError, "source collector artifact|valid bounded ZIP"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

    def test_missing_health_does_not_hide_malformed_present_processor_receipt(self) -> None:
        """A valid ZIP and matching artifact hashes cannot bless a malformed checkpoint member."""
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id or item.get("subject", {}).get("stage") != "health"
        ]
        processor_archive = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id
            and item.get("subject", {}).get("stage") == "processor"
            and item["role"] == "pipeline_artifact_archive"
        )
        processor_metadata = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id
            and item.get("subject", {}).get("stage") == "processor"
            and item["role"] == "pipeline_artifact_metadata"
        )

        archive_path = ROOT / processor_archive["path"]
        original_archive = archive_path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(original_archive)) as archive:
            members = {item.filename: archive.read(item.filename) for item in archive.infolist()}
        checkpoint_name = "upstream-catalogue-checkpoint-receipt.json"
        self.assertIn(checkpoint_name, members)
        members[checkpoint_name] = b"not-json\n"
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        malformed_archive = output.getvalue()

        metadata = json.loads((ROOT / processor_metadata["path"]).read_bytes())
        self.assertEqual(metadata["total_count"], 1)
        self.assertEqual(len(metadata["artifacts"]), 1)
        metadata["artifacts"][0]["size_in_bytes"] = len(malformed_archive)
        metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(malformed_archive)}"
        metadata_raw = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()

        with tempfile.TemporaryDirectory(prefix="completeness-missing-health-bad-checkpoint-") as name:
            input_root = pathlib.Path(name)
            (input_root / "malformed-processor.zip").write_bytes(malformed_archive)
            (input_root / "malformed-processor-metadata.json").write_bytes(metadata_raw)
            processor_archive.update({
                "root": "evidence", "path": "malformed-processor.zip",
                "bytes": len(malformed_archive), "sha256": MODULE.sha256_bytes(malformed_archive),
            })
            processor_metadata.update({
                "root": "evidence", "path": "malformed-processor-metadata.json",
                "bytes": len(metadata_raw), "sha256": MODULE.sha256_bytes(metadata_raw),
            })
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")

            with self.assertRaisesRegex(ValueError, "processor archive or producer-head input provenance"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

            no_source_index = copy.deepcopy(index)
            no_source_index["inputs"] = [
                item for item in no_source_index["inputs"]
                if item["scope_id"] != operation_id or item.get("subject", {}).get("stage") != "source"
            ]
            no_source_index_path = input_root / "no-source-input-index.json"
            no_source_index_path.write_text(json.dumps(no_source_index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "processor archive or producer-head input provenance"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=no_source_index_path,
                )

            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT),
                    "--input-root", str(input_root),
                    "--input-index", str(index_path),
                    "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md",
                    "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

    def test_missing_processor_does_not_hide_malformed_present_promotion_journal(self) -> None:
        """C's own journal schema and immutable tree binding are checked without B."""
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        scope_registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id or item.get("subject", {}).get("stage") != "processor"
        ]
        journal_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "promotion_journal_blob_api"
        )
        tree_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "promotion_state_tree"
        )
        commit_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "promotion_state_commit"
        )
        ref_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "promotion_state_ref"
        )

        with tempfile.TemporaryDirectory(prefix="completeness-missing-b-bad-journal-") as name:
            input_root = pathlib.Path(name)
            invalid_journal = b"{}\n"
            blob_api = json.loads((ROOT / journal_item["path"]).read_bytes())
            blob_api.update({
                "size": len(invalid_journal),
                "sha": MODULE.git_blob_sha1(invalid_journal),
                "content": MODULE.base64.b64encode(invalid_journal).decode("ascii"),
            })
            tree = json.loads((ROOT / tree_item["path"]).read_bytes())
            tree_rows = [
                row for row in tree["tree"]
                if row.get("path") == "reports/canonical-update-promotion-receipt.json"
            ]
            self.assertEqual(len(tree_rows), 1)
            tree_rows[0]["sha"] = blob_api["sha"]
            tree["sha"] = "d" * 40
            commit = json.loads((ROOT / commit_item["path"]).read_bytes())
            commit["tree"]["sha"] = tree["sha"]
            commit["sha"] = "c" * 40
            ref = json.loads((ROOT / ref_item["path"]).read_bytes())
            ref["object"]["sha"] = commit["sha"]

            self._write_rebound_evidence(
                input_root, journal_item, "bad-promotion-journal-blob.json",
                (json.dumps(blob_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            self._write_rebound_evidence(
                input_root, tree_item, "bad-promotion-tree.json",
                (json.dumps(tree, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            self._write_rebound_evidence(
                input_root, commit_item, "bad-promotion-commit.json",
                (json.dumps(commit, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            self._write_rebound_evidence(
                input_root, ref_item, "bad-promotion-ref.json",
                (json.dumps(ref, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "promotion journal schema"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

    def test_missing_processor_does_not_hide_invalid_durable_health_seal(self) -> None:
        """Health's durable state is independently schema-, tree-, and seal-checked."""
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        scope_registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id or item.get("subject", {}).get("stage") != "processor"
        ]
        state_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "health_state_blob_api"
        )
        tree_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "health_state_tree"
        )
        commit_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "health_state_commit"
        )
        ref_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "health_state_ref"
        )

        with tempfile.TemporaryDirectory(prefix="completeness-missing-b-bad-health-state-") as name:
            input_root = pathlib.Path(name)
            blob_api = json.loads((ROOT / state_item["path"]).read_bytes())
            state_raw = MODULE.base64.b64decode(blob_api["content"])
            state = json.loads(state_raw)
            state["state_sha256"] = "0" * 64
            invalid_state = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
            blob_api.update({
                "size": len(invalid_state),
                "sha": MODULE.git_blob_sha1(invalid_state),
                "content": MODULE.base64.b64encode(invalid_state).decode("ascii"),
            })
            tree = json.loads((ROOT / tree_item["path"]).read_bytes())
            tree_rows = [
                row for row in tree["tree"]
                if row.get("path") == "health/upstream-catalogue/state.json"
            ]
            self.assertEqual(len(tree_rows), 1)
            tree_rows[0]["sha"] = blob_api["sha"]
            tree["sha"] = "d" * 40
            commit = json.loads((ROOT / commit_item["path"]).read_bytes())
            commit["tree"]["sha"] = tree["sha"]
            commit["sha"] = "c" * 40
            ref = json.loads((ROOT / ref_item["path"]).read_bytes())
            ref["object"]["sha"] = commit["sha"]
            for row, filename, value in (
                (state_item, "bad-health-state-blob.json", blob_api),
                (tree_item, "bad-health-tree.json", tree),
                (commit_item, "bad-health-commit.json", commit),
                (ref_item, "bad-health-ref.json", ref),
            ):
                self._write_rebound_evidence(
                    input_root, row, filename,
                    (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "durable Health post-state seal"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

    def test_partial_health_packet_remains_blocked_when_b_and_c_are_absent(self) -> None:
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_id
            or item.get("subject", {}).get("stage") not in {"processor", "promotion"}
        ]
        with tempfile.TemporaryDirectory(prefix="completeness-health-without-bc-") as name:
            input_root = pathlib.Path(name)
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)
        row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
        facet = next(item for item in row["facets"] if item["facet_id"] == "specification_pipeline")
        self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(facet["state"], "missing")
        self.assertEqual(set(facet["details"]["missing_stages"]), {"processor", "promotion"})

    def test_present_ack_transition_is_validated_but_not_promoted_without_c_and_health(self) -> None:
        """A locally valid ACK stays non-authoritative while independent peers are absent."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_id
            or item.get("subject", {}).get("stage") not in {"promotion", "health"}
        ]
        with tempfile.TemporaryDirectory(prefix="completeness-ack-without-c-health-") as name:
            input_root = pathlib.Path(name)
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)
        operation = next(row for row in report["scopes"] if row["scope_id"] == operation_id)
        pipeline = next(facet for facet in operation["facets"] if facet["facet_id"] == "specification_pipeline")
        delivery = next(facet for facet in operation["facets"] if facet["facet_id"] == "immutable_publication_read_back")
        self.assertEqual(operation["evaluated_claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(pipeline["state"], "missing")
        self.assertEqual(set(pipeline["details"]["missing_stages"]), {"promotion", "health"})
        self.assertEqual(delivery["details"]["subject"]["publisher_run_id"], "37199628001")
        self.assertEqual(delivery["details"]["subject"]["registry_sha256"], "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0")
        self.assertTrue(delivery["details"]["acknowledgement_present"])
        self.assertNotIn("current_release_subject_applicable", delivery["details"])
        self.assertTrue(pipeline["details"]["validated_present_stage_evidence"])

    def test_build_report_admits_nonlegacy_native_ack_and_zero_write_replay(self) -> None:
        """The indexed caller admits a new schedule ACK and preserves its old witness on replay."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        expected_source = "6a5138c792f4b7402da0c5ab439646bd752a307f"
        cases = (
            ("read-back-confirmed", None),
            ("already_acknowledged", None),
            ("already_acknowledged", (99972280001, 2)),
        )
        for outcome, publisher_identity in cases:
            with self.subTest(outcome=outcome, publisher_identity=publisher_identity), tempfile.TemporaryDirectory(
                prefix=f"completeness-generic-native-ack-{outcome}-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                index_path, source_sha = self._build_synthetic_native_ack_packet(
                    input_root, outcome=outcome, publisher_identity=publisher_identity,
                )
                report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

            self.assertEqual(source_sha, expected_source)
            row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
            facets = {item["facet_id"]: item for item in row["facets"]}
            pipeline = facets["specification_pipeline"]
            delivery = facets["immutable_publication_read_back"]
            self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
            self.assertEqual(pipeline["state"], "missing")
            self.assertEqual(
                set(pipeline["details"]["missing_stages"]),
                {"source", "processor", "promotion", "health"},
            )
            self.assertEqual(delivery["state"], "historical")
            self.assertEqual(delivery["details"]["delivery_status"], "publisher_readback_acknowledged")
            self.assertEqual(delivery["details"]["verified_artifact_count"], 196)
            self.assertEqual(delivery["details"]["subject"]["source_sha"], expected_source)
            self.assertNotIn("current_release_subject_applicable", delivery["details"])
            if publisher_identity is not None:
                self.assertEqual(delivery["details"]["subject"]["publisher_run_id"], str(publisher_identity[0]))
                self.assertEqual(delivery["details"]["subject"]["publisher_attempt"], publisher_identity[1])
            if outcome == "read-back-confirmed":
                self.assertEqual(delivery["details"]["acknowledgement_run_id"], "99972290001")
                self.assertNotIn("acknowledgement_replay_run_id", delivery["details"])
            else:
                # The original ACK remains the observation witness. The later
                # schedule execution is separately shown as a zero-write replay.
                self.assertEqual(delivery["details"]["acknowledgement_run_id"], "37199709258")
                self.assertEqual(delivery["details"]["acknowledgement_replay_run_id"], "99972290001")
                self.assertEqual(delivery["details"]["acknowledgement_replay_attempt"], 1)
                self.assertEqual(delivery["details"]["acknowledgement_replay_status"], "already_acknowledged")
                self.assertEqual(delivery["details"]["acknowledgement_replay_journal_writes"], 0)
                self.assertTrue(delivery["details"]["acknowledgement_replay_state_unchanged"])

    def test_build_report_replay_requires_positive_pr_without_ci_or_optional_links(self) -> None:
        """A standalone zero-write witness still needs its own positive selected PR number."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        valid_cases: list[object] = [_PRESERVE_REPLAY_PR_NUMBER]
        invalid_cases: list[tuple[str, object]] = [
            ("zero", 0), ("boolean", True), ("missing", None), ("negative", -1),
        ]
        for label, replay_pr_number, expected_valid in [
            ("valid", valid_cases[0], True),
            *((label, number, False) for label, number in invalid_cases),
        ]:
            with self.subTest(pr_number=label), tempfile.TemporaryDirectory(
                prefix=f"completeness-ack-standalone-replay-pr-{label}-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                index_path, expected_source = self._build_synthetic_native_ack_packet(
                    input_root,
                    outcome="already_acknowledged",
                    acknowledgement_identity=(99972290001, 1),
                    acknowledgement_event="schedule",
                    standalone_replay=True,
                    replay_pr_number=replay_pr_number,
                )
                index = json.loads(index_path.read_bytes())
                indexed = {
                    item["role"]: item for item in index["inputs"]
                    if item.get("scope_id") == operation_id
                }
                self.assertNotIn("acknowledgement_ci", indexed)
                before_raw = (input_root / indexed["acknowledgement_journal_before"]["path"]).read_bytes()
                after_raw = (input_root / indexed["acknowledgement_journal_after"]["path"]).read_bytes()
                self.assertEqual(before_raw, after_raw)
                journal = json.loads(after_raw)
                self.assertEqual(len(journal["records"]), 1)
                witness_row = journal["records"][0]
                self.assertNotIn("ci", witness_row)
                self.assertNotIn("refresh_from", witness_row)
                self.assertNotIn("refresh_target_main_sha", witness_row)
                self.assertNotIn("superseded_by", witness_row)
                if expected_valid:
                    self.assertIsInstance(witness_row["pr"].get("number"), int)
                    self.assertNotIsInstance(witness_row["pr"]["number"], bool)
                    self.assertGreater(witness_row["pr"]["number"], 0)
                for role in (
                    "acknowledgement_state_ref", "acknowledgement_state_commit",
                    "acknowledgement_state_tree", "acknowledgement_journal_blob_api",
                    "acknowledgement_state_ref_before", "acknowledgement_state_commit_before",
                    "acknowledgement_state_tree_before", "acknowledgement_journal_blob_api_before",
                ):
                    self.assertIn(role, indexed)

                if expected_valid:
                    report = MODULE.build_report(
                        root=ROOT, input_root=input_root, input_index_path=index_path,
                    )
                    row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
                    self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
                    delivery = next(
                        facet for facet in row["facets"]
                        if facet["facet_id"] == "immutable_publication_read_back"
                    )
                    self.assertEqual(delivery["details"]["delivery_status"], "publisher_readback_acknowledged")
                    self.assertEqual(delivery["details"]["acknowledgement_replay_run_id"], "99972290001")
                    self.assertEqual(delivery["details"]["acknowledgement_replay_journal_writes"], 0)
                    self.assertEqual(delivery["details"]["subject"]["source_sha"], expected_source)
                else:
                    with self.assertRaisesRegex(ValueError, "selected PR number is not a positive integer"):
                        MODULE.build_report(
                            root=ROOT, input_root=input_root, input_index_path=index_path,
                        )

    def test_build_report_admits_ack_suffixes_from_all_selected_owner_states(self) -> None:
        """Full indexed admission follows the owning runner from each selected predecessor state."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        current_ack_run = 99972290001
        for predecessor in (
            "prepared", "pending-review", "merged", "publication-pending", "published",
        ):
            with self.subTest(predecessor=predecessor), tempfile.TemporaryDirectory(
                prefix=f"completeness-ack-owner-state-{predecessor}-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                index_path, source_sha = self._build_synthetic_native_ack_packet(
                    input_root, outcome="read-back-confirmed", predecessor_status=predecessor,
                )
                report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

            row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
            delivery = next(
                facet for facet in row["facets"] if facet["facet_id"] == "immutable_publication_read_back"
            )
            self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
            self.assertEqual(delivery["details"]["delivery_status"], "publisher_readback_acknowledged")
            self.assertEqual(delivery["details"]["acknowledgement_run_id"], str(current_ack_run))
            self.assertEqual(delivery["details"]["acknowledgement_attempt"], 1)
            self.assertEqual(delivery["details"]["subject"]["source_sha"], source_sha)

        # The sixth selected state is the no-write replay of an already
        # read-back-confirmed row. This intentionally reuses the original run
        # ID with a later attempt, so legacy dispatch must not swallow it.
        with tempfile.TemporaryDirectory(prefix="completeness-ack-owner-state-read-back-confirmed-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, source_sha = self._build_synthetic_native_ack_packet(
                input_root, outcome="already_acknowledged",
                acknowledgement_identity=(37199709258, 3), acknowledgement_event="workflow_run",
            )
            report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)
        row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
        delivery = next(
            facet for facet in row["facets"] if facet["facet_id"] == "immutable_publication_read_back"
        )
        self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(delivery["details"]["subject"]["source_sha"], source_sha)
        self.assertEqual(delivery["details"]["acknowledgement_run_id"], "37199709258")
        self.assertEqual(delivery["details"]["acknowledgement_attempt"], 2)
        self.assertEqual(delivery["details"]["acknowledgement_replay_run_id"], "37199709258")
        self.assertEqual(delivery["details"]["acknowledgement_replay_attempt"], 3)
        self.assertEqual(delivery["details"]["acknowledgement_replay_journal_writes"], 0)

    def test_build_report_rejects_failed_ack_predecessor_as_unselected(self) -> None:
        """A real owning-runner failure row is not an ACK recovery predecessor."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        with tempfile.TemporaryDirectory(prefix="completeness-ack-failed-predecessor-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, _source = self._build_synthetic_native_ack_packet(
                input_root, outcome="read-back-confirmed", predecessor_status="merged",
            )
            index = json.loads(index_path.read_bytes())
            before_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_id and item["role"] == "acknowledgement_journal_before"
            )
            before = json.loads((input_root / before_item["path"]).read_bytes())
            run_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_id and item["role"] == "acknowledgement_run"
            )
            ack_run = json.loads((input_root / run_item["path"]).read_bytes())
            owner_source = MODULE.git_read_only(
                ROOT, ["show", f"{ack_run['head_sha']}:scripts/canonical_update_pr.py"],
            )
            with tempfile.TemporaryDirectory(prefix="completeness-failed-owner-contract-") as temporary:
                owner_path = pathlib.Path(temporary) / "canonical_update_pr.py"
                owner_path.write_bytes(owner_source)
                owner = MODULE.import_module("synthetic_failed_ack_owner", owner_path)
            receipt_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_id and item["role"] == "publication_receipt"
            )
            failed_receipt = json.loads((ROOT / receipt_item["path"]).read_bytes())
            failed_receipt["status"] = "failed"
            failure_path = input_root / "prior-publication-failure.json"
            failure_path.write_text(json.dumps(failed_receipt, ensure_ascii=False, indent=2) + "\n")
            ack_url = "https://github.com/StatPan/datapan-registry/actions/runs/99972289999/attempts/1"
            before["records"][0] = owner.reconcile_huggingface_publication(
                before["records"][0], failure_path,
                observed_at="2026-10-04T13:19:20Z", run_url=ack_url,
            )
            self._write_rebound_evidence(
                input_root, before_item, "failed-before-journal.json",
                (json.dumps(before, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            before_state_roles = {
                "acknowledgement_state_ref_before", "acknowledgement_state_commit_before",
                "acknowledgement_state_tree_before", "acknowledgement_journal_blob_api_before",
            }
            index["inputs"] = [
                item for item in index["inputs"]
                if not (item.get("scope_id") == operation_id and item.get("role") in before_state_roles)
            ]
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "starts from a status not selected"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_build_report_binds_pr_number_across_ack_without_ci_or_state_bundles(self) -> None:
        """Optional CI/state snapshots cannot replace the PR identity in both ACK journals."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        mutations = ("changed_after", "missing_before", "boolean_before", "zero_before", "negative_before")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                prefix=f"completeness-ack-pr-number-{mutation}-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                index_path, _source = self._build_synthetic_native_ack_packet(
                    input_root, outcome="read-back-confirmed", predecessor_status="prepared",
                )
                index = json.loads(index_path.read_bytes())
                index["inputs"] = [
                    item for item in index["inputs"]
                    if not (
                        item.get("scope_id") == operation_id
                        and item.get("role") in {
                            "acknowledgement_state_ref", "acknowledgement_state_commit",
                            "acknowledgement_state_tree", "acknowledgement_journal_blob_api",
                            "acknowledgement_state_ref_before", "acknowledgement_state_commit_before",
                            "acknowledgement_state_tree_before", "acknowledgement_journal_blob_api_before",
                        }
                    )
                ]
                snapshots: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
                for role in ("acknowledgement_journal_before", "acknowledgement_journal_after"):
                    item = next(
                        row for row in index["inputs"]
                        if row.get("scope_id") == operation_id and row["role"] == role
                    )
                    journal = json.loads((input_root / item["path"]).read_bytes())
                    target = journal["records"][0]
                    target.pop("ci", None)
                    snapshots[role] = (item, journal)

                before_item, before = snapshots["acknowledgement_journal_before"]
                after_item, after = snapshots["acknowledgement_journal_after"]
                before_pr = before["records"][0]["pr"]
                after_pr = after["records"][0]["pr"]
                if mutation == "changed_after":
                    after_pr["number"] += 1
                    after_pr["url"] = f"https://github.com/StatPan/datapan-registry/pull/{after_pr['number']}"
                elif mutation == "missing_before":
                    before_pr.pop("number")
                elif mutation == "boolean_before":
                    before_pr["number"] = True
                elif mutation == "zero_before":
                    before_pr["number"] = 0
                else:
                    before_pr["number"] = -1
                self._write_rebound_evidence(
                    input_root, before_item, f"pr-number-before-{mutation}.json",
                    (json.dumps(before, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )
                self._write_rebound_evidence(
                    input_root, after_item, f"pr-number-after-{mutation}.json",
                    (json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )
                index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
                with self.assertRaisesRegex(ValueError, "exact positive PR number"):
                    MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_build_report_rejects_ack_source_payload_manifest_pr_history_and_ci_mutations(self) -> None:
        """Rebound indexed journals still fail each independent native subject or history join."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        cases = ("source", "payload", "manifest", "pr", "history", "ci")
        for mutation in cases:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                prefix=f"completeness-ack-mutation-{mutation}-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                index_path, _source = self._build_synthetic_native_ack_packet(
                    input_root, outcome="read-back-confirmed", predecessor_status="merged",
                )
                index = json.loads(index_path.read_bytes())
                state_bundle_roles = {
                    "acknowledgement_state_ref", "acknowledgement_state_commit",
                    "acknowledgement_state_tree", "acknowledgement_journal_blob_api",
                    "acknowledgement_state_ref_before", "acknowledgement_state_commit_before",
                    "acknowledgement_state_tree_before", "acknowledgement_journal_blob_api_before",
                }
                index["inputs"] = [
                    item for item in index["inputs"]
                    if not (item.get("scope_id") == operation_id and item.get("role") in state_bundle_roles)
                ]
                after_item = next(
                    item for item in index["inputs"]
                    if item.get("scope_id") == operation_id
                    and item["role"] == "acknowledgement_journal_after"
                )
                after = json.loads((input_root / after_item["path"]).read_bytes())
                row = after["records"][0]
                last_ack = row["acknowledgements"][-1]
                if mutation == "source":
                    last_ack["source_sha"] = "a" * 40
                elif mutation == "payload":
                    last_ack["read_back_bytes"] += 1
                elif mutation == "manifest":
                    last_ack["manifest_sha256"] = "f" * 64
                elif mutation == "pr":
                    row["pr"]["number"] += 1
                elif mutation == "history":
                    row["acknowledgements"][0]["run_id"] = 99972289998
                else:
                    self.assertIsInstance(row.get("ci"), dict)
                    row["ci"]["head_sha"] = "f" * 40
                self._write_rebound_evidence(
                    input_root, after_item, f"mutated-after-{mutation}.json",
                    (json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )
                index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
                with self.assertRaises(ValueError):
                    MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_build_report_keeps_payload_equivalence_separate_from_manifest_metadata(self) -> None:
        """A metadata-only current manifest change cannot make an older delivery current."""
        base_main = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/main"], cwd=ROOT, text=True,
        ).strip()
        common_dir = pathlib.Path(subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT, text=True,
        ).strip()).resolve()
        donor_snapshot: tuple[pathlib.Path, str, str, bytes | None, bytes] | None = None
        source_index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        source_manifest_rows = [
            item for item in source_index["inputs"]
            if item.get("input_id") == "data-go-kr.api-catalog.source_catalog_snapshot"
            and item.get("scope_id") == "data-go-kr.api-catalog"
            and item.get("role") == "source_catalog_snapshot"
            and item.get("path") == "data/data-go-kr.registry.json"
        ]
        self.assertEqual(len(source_manifest_rows), 1)
        source_manifest_row = source_manifest_rows[0]
        source_revision = source_manifest_row.get("subject", {}).get("source_revision")
        self.assertIsInstance(source_revision, str)
        self.assertEqual(len(source_revision), 40)
        self.assertTrue(all(character in "0123456789abcdef" for character in source_revision))
        self.assertEqual(
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", source_revision, base_main],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            ).returncode,
            0,
            "the indexed source-manifest revision must be an ancestor of the fixture's base main",
        )
        base_manifest_raw = subprocess.check_output(
            ["git", "show", f"{base_main}:manifest.json"], cwd=ROOT,
        )
        base_manifest = json.loads(base_manifest_raw)
        source_manifest_raw = subprocess.check_output(
            ["git", "show", f"{source_revision}:manifest.json"], cwd=ROOT,
        )
        changed_generated_at = "2026-07-11T10:28:36Z"
        old_generated_at = base_manifest["generated_at"].encode()
        self.assertEqual(base_manifest_raw.count(old_generated_at), 1)
        changed_manifest_raw = base_manifest_raw.replace(old_generated_at, changed_generated_at.encode(), 1)
        changed_manifest = json.loads(changed_manifest_raw)
        self.assertEqual(changed_manifest["artifacts"], base_manifest["artifacts"])
        self.assertEqual(changed_manifest["artifact_count"], base_manifest["artifact_count"])

        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        with tempfile.TemporaryDirectory(prefix="completeness-native-publisher-metadata-manifest-") as name:
            repository = pathlib.Path(name) / "repo"
            repository.mkdir()
            self._hardlink_worktree(ROOT, repository)
            local_manifest = repository / "manifest.json"
            local_manifest.unlink()
            local_manifest.write_bytes(changed_manifest_raw)
            subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=repository, check=True)
            alternates = repository / ".git/objects/info/alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(str(common_dir / "objects") + "\n")
            donor_snapshot = self._inherit_native_borrowed_shallow_boundary(ROOT, repository)
            self.assertEqual(donor_snapshot[0], common_dir)
            subprocess.run(["git", "config", "user.name", "Completeness metadata fixture"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "completeness-test@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", base_main], cwd=repository, check=True)
            subprocess.run(["git", "update-ref", "refs/heads/main", base_main], cwd=repository, check=True)
            subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repository, check=True)
            subprocess.run(["git", "read-tree", base_main], cwd=repository, check=True)

            input_root = repository / ".test-native-publication"
            input_root.mkdir()
            index_path, _source = self._build_synthetic_native_ack_packet(
                input_root, outcome="already_acknowledged", publisher_identity=(99972280001, 2),
            )
            fixture_index = json.loads(index_path.read_bytes())
            fixture_source_manifest_rows = [
                item for item in fixture_index["inputs"]
                if item.get("input_id") == source_manifest_row["input_id"]
                and item.get("scope_id") == source_manifest_row["scope_id"]
                and item.get("role") == source_manifest_row["role"]
                and item.get("path") == source_manifest_row["path"]
            ]
            self.assertEqual(fixture_source_manifest_rows, [source_manifest_row])
            report_before = MODULE.build_report(root=repository, input_root=input_root, input_index_path=index_path)

            subprocess.run(["git", "add", "manifest.json"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "metadata-only manifest fixture"], cwd=repository, check=True)
            metadata_main = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", metadata_main], cwd=repository, check=True)
            self.assertEqual(
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", source_revision, metadata_main],
                    cwd=repository, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                ).returncode,
                0,
                "the indexed source-manifest revision must remain an ancestor after the metadata-only main advance",
            )
            self.assertEqual(
                subprocess.check_output(["git", "show", f"{metadata_main}:manifest.json"], cwd=repository),
                changed_manifest_raw,
            )
            report = MODULE.build_report(root=repository, input_root=input_root, input_index_path=index_path)

        row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
        delivery = next(item for item in row["facets"] if item["facet_id"] == "immutable_publication_read_back")
        details = delivery["details"]
        before_row = next(item for item in report_before["scopes"] if item["scope_id"] == operation_id)
        before_delivery = next(item for item in before_row["facets"] if item["facet_id"] == "immutable_publication_read_back")
        self.assertEqual(report, report_before, "advancing trusted main metadata alone must not alter the completeness report")
        self.assertEqual(row["evaluated_claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(details["subject"]["publisher_run_id"], "99972280001")
        self.assertEqual(details["subject"]["publisher_attempt"], 2)
        self.assertNotIn("current_release_manifest_sha256", details)
        self.assertNotIn("current_release_applicable", details)
        self.assertNotIn("current_release_subject_applicable", details)
        witness = details["catalog_binding_witness"]
        self.assertEqual(witness["release_manifest_sha256"], MODULE.sha256_bytes(source_manifest_raw))
        self.assertEqual(witness["source_revision"], source_revision)
        self.assertTrue(witness["main_ancestry_verified"])
        self.assertEqual(before_delivery["details"]["catalog_binding_witness"], witness)
        self.assertNotIn("payload_equivalent_to_current_registry", details)
        self.assertTrue(details["payload_equivalent_to_registered_catalog_inventory"])
        self.assertTrue(row["release_relation"]["current_release_evaluation_required"])
        self.assertEqual(delivery["state"], "historical")

        assert donor_snapshot is not None
        self._assert_native_borrowed_donor_unchanged(ROOT, donor_snapshot)

    def test_snapshot_projection_strips_only_moving_release_applicability(self) -> None:
        """Snapshot projection hides the live applicability flag but retains source evidence."""
        source_details = {
            "current_release_applicable": False,
            "current_release_manifest_sha256": "a" * 64,
            "current_release_subject_applicable": False,
            "catalog_binding_witness": {
                "source_revision": "b" * 40,
                "release_manifest_sha256": "c" * 64,
                "main_ancestry_verified": True,
            },
            "payload_equivalent_to_current_registry": False,
        }

        snapshot_details = MODULE._snapshot_facet_details(source_details)

        self.assertIs(source_details["current_release_applicable"], False)
        self.assertNotIn("current_release_applicable", snapshot_details)
        self.assertNotIn("current_release_manifest_sha256", snapshot_details)
        self.assertNotIn("current_release_subject_applicable", snapshot_details)
        self.assertEqual(snapshot_details["catalog_binding_witness"], source_details["catalog_binding_witness"])
        self.assertIs(snapshot_details["payload_equivalent_to_registered_catalog_inventory"], False)

    def test_build_report_rejects_nonlegacy_ack_replay_mutations_and_preserves_outputs(self) -> None:
        """Rebound bytes cannot turn replay into a write, another subject, or another state ref."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )

        def rewrite_log(
            input_root: pathlib.Path, index_path: pathlib.Path, *, key: str, value: object,
        ) -> None:
            index = json.loads(index_path.read_bytes())
            item = next(
                row for row in index["inputs"]
                if row.get("scope_id") == operation_id and row["role"] == "acknowledgement_log_archive"
            )
            path = input_root / item["path"]
            with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
                members = [(entry.filename, archive.read(entry.filename)) for entry in archive.infolist()]
            rewritten: list[tuple[str, bytes]] = []
            for filename, raw in members:
                if filename != "reconcile.log":
                    rewritten.append((filename, raw))
                    continue
                lines = raw.decode("utf-8").splitlines()
                self.assertEqual(len(lines), 1)
                marker = lines[0].find("{")
                payload = json.loads(lines[0][marker:])
                payload[key] = value
                lines[0] = lines[0][:marker] + json.dumps(payload, sort_keys=True)
                rewritten.append((filename, ("\n".join(lines) + "\n").encode()))
            rebuilt = io.BytesIO()
            with zipfile.ZipFile(rebuilt, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for filename, raw in rewritten:
                    archive.writestr(filename, raw)
            self._write_rebound_evidence(input_root, item, "mutated-ack-log.zip", rebuilt.getvalue())
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")

        with tempfile.TemporaryDirectory(prefix="completeness-bad-native-ack-replay-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, _source = self._build_synthetic_native_ack_packet(input_root)
            rewrite_log(input_root, index_path, key="journal_writes", value=1)
            with self.assertRaisesRegex(ValueError, "exact zero-write journal replay"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT), "--input-root", str(input_root),
                    "--input-index", str(index_path), "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md", "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

        with tempfile.TemporaryDirectory(prefix="completeness-wrong-native-ack-witness-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, _source = self._build_synthetic_native_ack_packet(input_root)
            rewrite_log(input_root, index_path, key="manifest_sha256", value="f" * 64)
            with self.assertRaisesRegex(ValueError, "exact-subject read-back witness"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

        with tempfile.TemporaryDirectory(prefix="completeness-native-ack-state-ref-mismatch-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, _source = self._build_synthetic_native_ack_packet(input_root)
            index = json.loads(index_path.read_bytes())
            ref_item = next(
                row for row in index["inputs"]
                if row.get("scope_id") == operation_id
                and row["role"] == "acknowledgement_state_ref_before"
            )
            ref = json.loads((input_root / ref_item["path"]).read_bytes())
            ref["object"]["sha"] = "a" * 40
            self._write_rebound_evidence(
                input_root, ref_item, "mismatched-before-ref.json",
                (json.dumps(ref, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "branch ref, immutable commit, and complete tree"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_partial_ack_rejects_rebound_journal_and_log_before_missing_health_return(self) -> None:
        """Missing Health cannot hide malformed supplied ACK journal or job logs."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )

        def partial_index() -> dict[str, object]:
            index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
            index["inputs"] = [
                item for item in index["inputs"]
                if item.get("scope_id") != operation_id
                or item.get("subject", {}).get("stage") != "health"
            ]
            return index

        with tempfile.TemporaryDirectory(prefix="completeness-ack-local-negative-") as name:
            input_root = pathlib.Path(name)
            index = partial_index()
            after_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_id and item["role"] == "acknowledgement_journal_after"
            )
            self._write_rebound_evidence(
                input_root, after_item, "tampered-ack-journal.json", b'{"tampered_ack":true}\n',
            )
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT),
                    "--input-root", str(input_root),
                    "--input-index", str(index_path),
                    "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md",
                    "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

        with tempfile.TemporaryDirectory(prefix="completeness-ack-invalid-owned-body-") as name:
            input_root = pathlib.Path(name)
            index = partial_index()
            for role in ("acknowledgement_journal_before", "acknowledgement_journal_after"):
                item = next(
                    row for row in index["inputs"]
                    if row.get("scope_id") == operation_id and row["role"] == role
                )
                original_path = ROOT / item["path"]
                journal = json.loads(original_path.read_bytes())
                target = next(
                    row for row in journal["records"]
                    if row.get("pr", {}).get("state") == "merged"
                )
                target["ownership"]["body"] += "\nTampered without updating the durable body digest."
                self._write_rebound_evidence(
                    input_root, item, f"invalid-owned-body-{role}.json",
                    (json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT),
                    "--input-root", str(input_root),
                    "--input-index", str(index_path),
                    "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md",
                    "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")
            with self.assertRaisesRegex(ValueError, "fails its authenticated producer contract"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

            index = partial_index()
            log_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_id and item["role"] == "acknowledgement_log_archive"
            )
            self._write_rebound_evidence(input_root, log_item, "not-ack-logs.zip", b"not-a-zip\n")
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "acknowledgement logs.*ZIP|not a valid bounded ZIP"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_health_archived_state_refs_join_each_supplied_peer_without_full_chain(self) -> None:
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        cases = (
            (
                "processor", "promotion-ref",
                b"0" * 40 + b"\trefs/heads/automation/canonical-update-state\n",
                "health archived promotion ref differs from the exact supplied C state",
            ),
            (
                "promotion", "processor-ref",
                b"0" * 40 + b"\trefs/heads/automation/upstream-catalogue-state\n",
                "health archived processor ref differs from the exact supplied B state",
            ),
        )
        for missing_stage, member_name, changed_ref, message in cases:
            with self.subTest(missing_stage=missing_stage):
                index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
                index["inputs"] = [
                    item for item in index["inputs"]
                    if item.get("scope_id") != operation_id
                    or item.get("subject", {}).get("stage") != missing_stage
                ]
                with tempfile.TemporaryDirectory(prefix="completeness-health-peer-join-") as name:
                    input_root = pathlib.Path(name)
                    index_path = self._write_rebound_health_archive(
                        index, input_root, member_name, changed_ref,
                    )
                    with self.assertRaisesRegex(ValueError, message):
                        MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_health_archived_promotion_journal_exactly_joins_c_and_preserves_write(self) -> None:
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_id
            or item.get("subject", {}).get("stage") != "processor"
        ]
        health_archive_item = next(
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id
            and item.get("subject", {}).get("stage") == "health"
            and item["role"] == "pipeline_artifact_archive"
        )
        archive_raw = (ROOT / health_archive_item["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as archive:
            journal = json.loads(archive.read("promotion/reports/canonical-update-promotion-receipt.json"))
        original_updated_at = journal["updated_at"]
        journal["updated_at"] = "2026-10-04T13:23:01Z"
        self.assertNotEqual(journal["updated_at"], original_updated_at)
        valid_schema_different_journal = (
            json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()

        with tempfile.TemporaryDirectory(prefix="completeness-health-c-journal-join-") as name:
            input_root = pathlib.Path(name)
            index_path = self._write_rebound_health_archive(
                index, input_root,
                "promotion/reports/canonical-update-promotion-receipt.json",
                valid_schema_different_journal,
            )
            with self.assertRaisesRegex(ValueError, "health archived promotion journal differs from the exact supplied C state"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT),
                    "--input-root", str(input_root),
                    "--input-index", str(index_path),
                    "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md",
                    "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

    def test_health_local_snapshot_schema_and_ref_fail_before_missing_b_c_return(self) -> None:
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        cases = (
            (
                "promotion/reports/canonical-update-promotion-receipt.json", b"{}\n",
                "health archived promotion journal",
            ),
            (
                "processor-ref", b"bad\trefs/heads/automation/upstream-catalogue-state\n",
                "health archived processor-ref",
            ),
            (
                "promotion-ref", b"0" * 40 + b"\trefs/heads/foreign-state\n",
                "health archived promotion-ref",
            ),
        )
        for member_name, bad_bytes, expected in cases:
            with self.subTest(member=member_name):
                index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
                index["inputs"] = [
                    item for item in index["inputs"]
                    if item.get("scope_id") != operation_id
                    or item.get("subject", {}).get("stage") not in {"processor", "promotion"}
                ]
                with tempfile.TemporaryDirectory(prefix="completeness-health-local-snapshot-") as name:
                    input_root = pathlib.Path(name)
                    index_path = self._write_rebound_health_archive(index, input_root, member_name, bad_bytes)
                    with self.assertRaisesRegex(ValueError, expected):
                        MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_retained_full_chain_uses_registry_defined_catalog_scope_id(self) -> None:
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index, checked, resolved = MODULE.validate_input_index(
            root=ROOT, input_root=ROOT, index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            scope_by_id=scope_by_id,
        )
        changed_registry = copy.deepcopy(scope_registry)
        catalog_scope = next(
            scope for scope in changed_registry["scopes"]
            if scope["source_id"] == "data_go_kr"
            and scope["resource_kind"] == "api_catalog_metadata"
        )
        old_scope_id = catalog_scope["scope_id"]
        catalog_scope["scope_id"] = f"{old_scope_id}-reviewed-alias"
        changed_inputs = copy.deepcopy(checked)
        for item in changed_inputs:
            if item["scope_id"] == old_scope_id:
                item["scope_id"] = catalog_scope["scope_id"]
        operation_scope = next(
            scope for scope in changed_registry["scopes"]
            if scope["source_id"] == "data_go_kr"
            and scope["resource_kind"] == "api_operation_manifest"
        )
        operation_inputs = [
            item for item in changed_inputs if item["scope_id"] == operation_scope["scope_id"]
        ]

        result = MODULE.validate_pipeline_evidence(
            root=ROOT, operation_inputs=operation_inputs, all_inputs=changed_inputs,
            resolved=resolved, evaluation_epoch=index["evaluation_epoch"],
            scope_registry=changed_registry,
        )

        self.assertEqual(result["status"], "verified_historical_chain")
        self.assertEqual(result["current_registry"]["sha256"], "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0")

    def test_native_publisher_validator_binds_actual_run_receipt_and_artifact(self) -> None:
        publication_root = ROOT / "reports/completeness-proof-evidence/issue-659/publication"
        run = json.loads((publication_root / "publisher-run.json").read_bytes())
        jobs = json.loads((publication_root / "publisher-jobs.json").read_bytes())
        artifacts = json.loads((publication_root / "publisher-artifacts.json").read_bytes())
        archive = (publication_root / "publisher-receipts.zip").read_bytes()
        receipt = (publication_root / "publisher-receipt.json").read_bytes()
        binding = (publication_root / "publisher-source-binding.json").read_bytes()

        result = MODULE.validate_native_publisher_attempt(
            root=ROOT, run=run, jobs=jobs, artifact_response=artifacts,
            archive=archive, receipt_raw=receipt, binding_raw=binding,
            evaluation_epoch="2026-10-04T13:23:00Z",
        )
        self.assertEqual(result["run_id"], "37199628001")
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(result["event"], "workflow_dispatch")
        self.assertEqual(result["source_sha"], "6a5138c792f4b7402da0c5ab439646bd752a307f")
        self.assertLessEqual(
            result["native_verification_started_at"],
            result["native_verification_completed_at"],
        )

        malformed_artifacts = copy.deepcopy(artifacts)
        malformed_artifacts["total_count"] = True
        with self.assertRaisesRegex(ValueError, "artifact_list_incomplete"):
            MODULE.validate_native_publisher_attempt(
                root=ROOT, run=run, jobs=jobs, artifact_response=malformed_artifacts,
                archive=archive, receipt_raw=receipt, binding_raw=binding,
                evaluation_epoch="2026-10-04T13:23:00Z",
            )

    def test_native_distribution_inventory_binds_191_manifest_rows_and_five_exact_extras(self) -> None:
        publication_root = ROOT / "reports/completeness-proof-evidence/issue-659/publication"
        manifest_raw = (publication_root / "publisher-source-manifest.json").read_bytes()
        manifest = json.loads(manifest_raw)
        pointer = json.loads((publication_root / "publisher-pointer-immutable.json").read_bytes())
        receipt = json.loads((publication_root / "publisher-receipt.json").read_bytes())
        source_sha = receipt["source_binding"]["source_sha"]
        workflow_sha = receipt["source_binding"]["workflow_sha"]
        source_script = subprocess.check_output(
            ["git", "show", f"{source_sha}:scripts/huggingface_registry_distribution.py"], cwd=ROOT,
        )
        workflow = subprocess.check_output(
            ["git", "show", f"{workflow_sha}:.github/workflows/huggingface-distribution.yml"], cwd=ROOT,
        )

        result = MODULE.validate_native_distribution_inventory(
            manifest=manifest, manifest_raw=manifest_raw, pointer=pointer,
            receipt=receipt, source_script_raw=source_script, workflow_raw=workflow,
        )

        self.assertEqual(result["source_manifest_artifacts"], 191)
        self.assertEqual(result["distribution_artifacts"], 196)
        self.assertEqual(len(result["verified_artifacts"]), 196)
        members = {item["path"]: item for item in result["verified_artifacts"]}
        self.assertIn("RELEASE_NOTES.md", members)
        self.assertIn("reports/latest-release-readiness.json", members)
        self.assertEqual(len(result["workflow_extras"]), 5)

        mutations = []
        truncated = copy.deepcopy(pointer)
        truncated["artifacts"].pop()
        truncated["artifact_count"] -= 1
        mutations.append((truncated, "receipt counts"))
        duplicate = copy.deepcopy(pointer)
        duplicate["artifacts"][-1] = copy.deepcopy(duplicate["artifacts"][0])
        mutations.append((duplicate, "duplicate or non-canonical"))
        changed = copy.deepcopy(pointer)
        changed["artifacts"][0]["sha256"] = "0" * 64
        mutations.append((changed, "changes a source-manifest artifact"))
        substituted = copy.deepcopy(pointer)
        extra_row = next(
            row for row in substituted["artifacts"]
            if row["path"] == "reports/latest-release-readiness.json"
        )
        extra_row["path"] = "reports/unreviewed-extra.json"
        mutations.append((substituted, "missing or unreviewed workflow extras"))
        for candidate, message in mutations:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                MODULE.validate_native_distribution_inventory(
                    manifest=manifest, manifest_raw=manifest_raw, pointer=candidate,
                    receipt=receipt, source_script_raw=source_script, workflow_raw=workflow,
                )
        with self.assertRaisesRegex(ValueError, "no reviewed all-artifact verifier contract"):
            MODULE.validate_native_distribution_inventory(
                manifest=manifest, manifest_raw=manifest_raw, pointer=pointer,
                receipt=receipt, source_script_raw=source_script + b"\n# changed",
                workflow_raw=workflow,
            )

    def test_consumer_readback_binds_a_parameterized_release_artifact(self) -> None:
        publication_root = ROOT / "reports/completeness-proof-evidence/issue-659/publication"
        run = json.loads((publication_root / "publisher-run.json").read_bytes())
        jobs = json.loads((publication_root / "publisher-jobs.json").read_bytes())
        artifact_api = json.loads((publication_root / "publisher-artifacts.json").read_bytes())
        publication = MODULE.validate_native_publisher_attempt(
            root=ROOT, run=run, jobs=jobs, artifact_response=artifact_api,
            archive=(publication_root / "publisher-receipts.zip").read_bytes(),
            receipt_raw=(publication_root / "publisher-receipt.json").read_bytes(),
            binding_raw=(publication_root / "publisher-source-binding.json").read_bytes(),
            evaluation_epoch="2026-10-04T13:23:00Z",
        )
        manifest = json.loads(subprocess.check_output(
            ["git", "show", f"{publication['source_sha']}:manifest.json"],
            cwd=ROOT,
        ))
        artifact = next(
            item for item in manifest["artifacts"]
            if item["path"] not in {"data/data-go-kr.registry.json", "manifest.json"}
        )
        native_observed_at = publication["native_verification_completed_at"]
        reported_at = run["updated_at"]
        proof = {
            "publication": {"artifact": {"path": artifact["path"], "sha256": artifact["sha256"]}},
            "consumer_read_back": {"state": "proven", "observed_at": native_observed_at, "artifact_sha256": artifact["sha256"]},
        }
        readback = {
            "publisher_run_id": run["id"],
            "publisher_attempt": run["run_attempt"],
            "generated_at": reported_at,
            "checks": [{"detail": {
                "path": artifact["path"], "bytes_streamed": artifact["bytes"],
                "sha256": artifact["sha256"], "revision": publication["payload_revision"],
            }}],
        }
        publisher_job = jobs["jobs"][0]
        verified = MODULE.validate_consumer_readback(
            readback=readback, proof=proof, publication=publication,
            publisher_job=publisher_job, evaluation_epoch="2026-10-04T13:23:00Z",
            publisher_run=run, expected_bytes=artifact["bytes"],
        )
        self.assertEqual(verified["path"], artifact["path"])
        self.assertEqual(verified["sha256"], artifact["sha256"])
        self.assertEqual(verified["revision"], publication["payload_revision"])
        self.assertEqual(verified["observed_at"], native_observed_at)

        readback["checks"][0]["detail"]["bytes_streamed"] += 1
        with self.assertRaisesRegex(ValueError, "consumer stream bytes"):
            MODULE.validate_consumer_readback(
                readback=readback, proof=proof, publication=publication,
                publisher_job=publisher_job, evaluation_epoch="2026-10-04T13:23:00Z",
                publisher_run=run, expected_bytes=artifact["bytes"],
            )

    def test_stage_job_attempt_bool_is_not_accepted_as_attempt_one(self) -> None:
        _registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index, checked, resolved = MODULE.validate_input_index(
            root=ROOT, input_root=ROOT, index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            scope_by_id=scope_by_id,
        )
        scoped = [item for item in checked if item["scope_id"] == "data-go-kr.api-operations"]
        roles = MODULE.stage_input_map(scoped, "source")
        MODULE.stage_value(roles, resolved, "pipeline_run")
        original_jobs = MODULE.stage_value(roles, resolved, "pipeline_jobs")
        corrupted = copy.deepcopy(original_jobs)
        corrupted["jobs"][0]["run_attempt"] = True
        with tempfile.TemporaryDirectory() as directory:
            mutated = pathlib.Path(directory) / "jobs.json"
            mutated.write_text(json.dumps(corrupted))
            mutated_role = copy.deepcopy(roles["pipeline_jobs"])
            mutated_role["input_id"] = "mutated-jobs"
            resolved["mutated-jobs"] = mutated
            roles["pipeline_jobs"] = mutated_role
            with self.assertRaisesRegex(ValueError, "exact successful run attempt"):
                MODULE.validate_stage_run(
                    stage="source", roles=roles, resolved=resolved,
                    evaluation_epoch=index["evaluation_epoch"], root=ROOT,
                )

    def test_indexed_pipeline_input_must_bind_the_same_trusted_repository(self) -> None:
        _registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index, checked, resolved = MODULE.validate_input_index(
            root=ROOT, input_root=ROOT, index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            scope_by_id=scope_by_id,
        )
        scoped = [item for item in checked if item["scope_id"] == "data-go-kr.api-operations"]
        roles = MODULE.stage_input_map(scoped, "source")
        roles["pipeline_artifact_archive"] = copy.deepcopy(roles["pipeline_artifact_archive"])
        roles["pipeline_artifact_archive"]["producer"]["repository"] = "Other/repository"
        with self.assertRaisesRegex(ValueError, "indexed inputs disagree"):
            MODULE.validate_stage_run(
                stage="source", roles=roles, resolved=resolved,
                evaluation_epoch=index["evaluation_epoch"], root=ROOT,
            )

    def test_unmerged_candidate_commit_is_not_trusted_main_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Completeness test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "completeness@example.invalid"], cwd=repo, check=True)
            (repo / "file").write_text("base\n")
            subprocess.run(["git", "add", "file"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "main base"], cwd=repo, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", base], cwd=repo, check=True)
            subprocess.run(["git", "checkout", "-qb", "candidate"], cwd=repo, check=True)
            (repo / "file").write_text("candidate\n")
            subprocess.run(["git", "commit", "-qam", "unmerged candidate"], cwd=repo, check=True)
            candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
            (repo / "file").write_text("main advance\n")
            subprocess.run(["git", "commit", "-qam", "main advance"], cwd=repo, check=True)
            main = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", main], cwd=repo, check=True)
            with self.assertRaisesRegex(ValueError, "not reachable from the selected trusted origin/main history"):
                MODULE.assert_main_ancestor(repo, candidate, "test candidate")

    def test_input_row_order_does_not_change_canonical_rollup(self) -> None:
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_text())
        reordered = copy.deepcopy(index)
        reordered["inputs"] = list(reversed(reordered["inputs"]))

        with tempfile.TemporaryDirectory() as directory:
            directory_path = pathlib.Path(directory)
            original_index = directory_path / "original.json"
            alternate_index = directory_path / "reordered.json"
            original_index.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n")
            alternate_index.write_text(json.dumps(reordered, ensure_ascii=False, separators=(",", ":")) + "\n")
            baseline = MODULE.build_report(
                root=ROOT,
                input_root=ROOT,
                input_index_path=original_index,
            )
            reordered_report = MODULE.build_report(
                root=ROOT,
                input_root=ROOT,
                input_index_path=alternate_index,
            )

        self.assertEqual(MODULE.render_json(baseline), MODULE.render_json(reordered_report))
        self.assertEqual(MODULE.render_markdown(baseline), MODULE.render_markdown(reordered_report))

    def test_indexed_claim_cannot_bind_bytes_different_from_committed_source(self) -> None:
        item = {
            "root": "repository",
            "path": "sources/data_go_kr.json",
            "namespace": "live_operational",
            "producer": {"repository": "StatPan/datapan-registry", "revision": "849af2936a743573358820275df985b5809d0f7b"},
            "bytes": 1,
            "sha256": "0" * 64,
        }
        with tempfile.NamedTemporaryFile() as tampered:
            tampered.write(b"{}")
            tampered.flush()
            with self.assertRaisesRegex(ValueError, "receipt bytes changed"):
                MODULE.verify_main_committed_input(
                    root=ROOT,
                    item=item,
                    path=pathlib.Path(tampered.name),
                    label="test evidence",
                )


if __name__ == "__main__":
    unittest.main()
