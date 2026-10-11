from __future__ import annotations

import hashlib
from dataclasses import replace
import errno
import importlib.util
import os
import pathlib
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock


SOURCE = pathlib.Path(__file__).parents[1] / "scripts" / "canonical_publication_state.py"
SPEC = importlib.util.spec_from_file_location("canonical_publication_state_raw_owner_test", SOURCE)
assert SPEC is not None and SPEC.loader is not None
STATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = STATE
SPEC.loader.exec_module(STATE)


class OwnedRawGitBlobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="state-raw-owner-test-")
        self.root = pathlib.Path(self.temp.name)
        self.store = STATE.GitObjectStore(
            self.root,
            remote_url="https://example.invalid/StatPan/datapan-registry.git",
        )
        self.addCleanup(self.store.close)
        self.addCleanup(self.temp.cleanup)

    def seed_blob(self, payload: bytes) -> str:
        return self.store._run(
            ("hash-object", "-w", "--stdin"),
            git_dir=self.store.git_dir,
            maximum_output=128,
            input_bytes=payload,
        ).decode("ascii").strip()

    def no_owned_files(self) -> None:
        self.assertEqual(list(self.store._raw_blob_root.iterdir()), [])

    def fake_git(self, script: str) -> pathlib.Path:
        executable = self.root / "bounded-git-fixture"
        executable.write_text("#!/bin/sh\nset -eu\n" + script, encoding="utf-8")
        executable.chmod(0o700)
        self.store.git_executable = executable
        return executable

    def test_real_bare_git_large_blob_stream_and_metadata_limit(self) -> None:
        payload_size = 8 * 1024 * 1024 + 8193
        pattern = b"datapan-state-raw-blob-v1\x00"
        payload = (pattern * ((payload_size + len(pattern) - 1) // len(pattern)))[:payload_size]
        blob_oid = self.seed_blob(payload)
        expected_sha256 = hashlib.sha256(payload).hexdigest()

        materialization = self.store._materialize_blob_to_owned_file(
            blob_oid=blob_oid,
            declared_size=len(payload),
            expected_sha256=expected_sha256,
        )

        self.assertIs(
            self.store.require_owned_blob_materialization(materialization),
            materialization,
        )
        with self.assertRaisesRegex(STATE.StateStoreError, "another store"):
            self.store.require_owned_blob_materialization(
                replace(materialization, store_owner=object()),
            )
        with self.assertRaisesRegex(STATE.StateStoreError, "not issued"):
            self.store.require_owned_blob_materialization(
                replace(materialization, stdout_bytes=materialization.stdout_bytes + 1),
            )
        with self.assertRaisesRegex(STATE.StateStoreError, "not issued"):
            self.store.require_owned_blob_materialization(
                replace(materialization, store_owner=self.store),
            )
        self.assertEqual(materialization.git_blob_sha1, blob_oid)
        self.assertEqual(materialization.sha256, expected_sha256)
        self.assertEqual(materialization.byte_length, payload_size)
        self.assertIs(materialization.store_owner, self.store)
        self.assertEqual(materialization.mode, 0o400)
        self.assertEqual(materialization.stdout_bytes, payload_size)
        self.assertLessEqual(materialization.stderr_bytes, 64 * 1024)
        self.assertEqual(materialization.command_count, 1)
        self.assertEqual(
            materialization.output_bytes_after - materialization.output_bytes_before,
            materialization.transport_bytes,
        )
        self.assertEqual(materialization.path.read_bytes(), payload)
        self.assertEqual(self.store.local_materialization_read_bytes, payload_size)

        foreign_store = STATE.GitObjectStore(
            self.root / "foreign-owner",
            remote_url="https://example.invalid/StatPan/datapan-registry.git",
        )
        self.addCleanup(foreign_store.close)
        with self.assertRaisesRegex(STATE.StateStoreError, "another store"):
            foreign_store.require_owned_blob_materialization(materialization)

        tree_input = f"100644 blob {blob_oid}\tlarge-report.json\n".encode("ascii")
        tree_oid = self.store._run(
            ("mktree",),
            git_dir=self.store.git_dir,
            maximum_output=128,
            input_bytes=tree_input,
        ).decode("ascii").strip()
        commit_oid = self.store._run(
            ("commit-tree", tree_oid, "-m", "raw owner test"),
            git_dir=self.store.git_dir,
            maximum_output=128,
            author=True,
        ).decode("ascii").strip()
        with self.assertRaisesRegex(STATE.StateStoreError, "metadata byte bound"):
            self.store.read_file(
                commit_oid,
                "large-report.json",
                maximum=self.store.max_blob_bytes,
            )

    def test_wrong_oid_size_and_expected_digest_fail_without_sealing(self) -> None:
        payload = b"a real local blob with a bounded identity\n"
        blob_oid = self.seed_blob(payload)
        wrong_oid = hashlib.sha1(b"blob 7\0missing").hexdigest()

        with self.assertRaises(STATE.StateStoreError):
            self.store._materialize_blob_to_owned_file(
                blob_oid=wrong_oid,
                declared_size=7,
            )
        self.no_owned_files()

        with self.assertRaises(STATE.StateStoreError):
            self.store._materialize_blob_to_owned_file(
                blob_oid=blob_oid,
                declared_size=len(payload) - 1,
            )
        self.no_owned_files()

        with self.assertRaisesRegex(STATE.StateStoreError, "SHA-256"):
            self.store._materialize_blob_to_owned_file(
                blob_oid=blob_oid,
                declared_size=len(payload),
                expected_sha256="0" * 64,
            )
        self.no_owned_files()

    def test_real_bare_git_empty_blob_is_exactly_materialized(self) -> None:
        blob_oid = self.seed_blob(b"")
        materialization = self.store._materialize_blob_to_owned_file(
            blob_oid=blob_oid,
            declared_size=0,
            expected_sha256=hashlib.sha256(b"").hexdigest(),
        )
        self.assertEqual(materialization.byte_length, 0)
        self.assertEqual(materialization.stdout_bytes, 0)
        self.assertEqual(materialization.path.read_bytes(), b"")
        self.assertIs(
            self.store.require_owned_blob_materialization(materialization),
            materialization,
        )

    def test_truncated_and_extra_child_output_fail_closed(self) -> None:
        expected_oid = hashlib.sha1(b"blob 5\0abcde").hexdigest()
        self.fake_git("printf abc\nexit 0\n")
        with self.assertRaisesRegex(STATE.StateStoreError, "shorter"):
            self.store._materialize_blob_to_owned_file(
                blob_oid=expected_oid,
                declared_size=5,
            )
        self.no_owned_files()

        self.fake_git("printf abcdeX\nexit 0\n")
        with self.assertRaisesRegex(STATE.StateStoreError, "exceeded"):
            self.store._materialize_blob_to_owned_file(
                blob_oid=expected_oid,
                declared_size=5,
            )
        self.no_owned_files()

    def test_stderr_is_counted_once_and_oversized_diagnostics_fail(self) -> None:
        payload = b"abcde"
        expected_oid = hashlib.sha1(b"blob 5\0abcde").hexdigest()
        self.fake_git("printf abcde\nprintf diagnostic >&2\nexit 0\n")
        materialization = self.store._materialize_blob_to_owned_file(
            blob_oid=expected_oid,
            declared_size=len(payload),
        )
        self.assertEqual(materialization.stderr_bytes, len(b"diagnostic"))
        self.assertEqual(materialization.stdout_bytes, len(payload))
        self.assertEqual(
            materialization.output_bytes_after - materialization.output_bytes_before,
            len(payload) + len(b"diagnostic"),
        )
        self.assertIs(
            self.store.require_owned_blob_materialization(materialization),
            materialization,
        )

        self.fake_git("head -c 65537 /dev/zero >&2\nexit 0\n")
        with self.assertRaisesRegex(STATE.StateStoreError, "fixed bound"):
            self.store._materialize_blob_to_owned_file(
                blob_oid=expected_oid,
                declared_size=len(payload),
            )
        self.assertEqual(list(self.store._raw_blob_root.iterdir()), [materialization.path])

    def test_timeout_and_cleanup_uncertainty_never_return_materialization(self) -> None:
        expected_oid = hashlib.sha1(b"blob 5\0abcde").hexdigest()
        self.fake_git("printf x\nexec /bin/sleep 10\n")

        with self.store.bounded_deadline(time.monotonic() + 0.25):
            with self.assertRaises(STATE.StateStoreError):
                self.store._materialize_blob_to_owned_file(
                    blob_oid=expected_oid,
                    declared_size=5,
                )
        self.no_owned_files()

        self.fake_git("printf x\nexec /bin/sleep 10\n")
        real_killpg = os.killpg

        def deny_group_probe_but_allow_owned_signals(pgid: int, sig: int) -> None:
            if sig == 0:
                raise PermissionError("fixture group state cannot be confirmed")
            real_killpg(pgid, sig)

        with self.store.bounded_deadline(time.monotonic() + 0.25):
            with mock.patch.object(
                STATE.os, "killpg", side_effect=deny_group_probe_but_allow_owned_signals,
            ):
                with self.assertRaises(STATE.StateCleanupUncertain):
                    self.store._materialize_blob_to_owned_file(
                        blob_oid=expected_oid,
                        declared_size=5,
                    )
        self.no_owned_files()

    def test_remaining_command_and_output_caps_block_before_spawn(self) -> None:
        payload = b"cap fixture\n"
        blob_oid = self.seed_blob(payload)
        before_commands = self.store.budget.commands
        self.store.budget.max_commands = before_commands
        with self.assertRaisesRegex(STATE.StateStoreError, "subprocess budget"):
            self.store._materialize_blob_to_owned_file(
                blob_oid=blob_oid,
                declared_size=len(payload),
            )
        self.assertEqual(self.store.budget.commands, before_commands)
        self.no_owned_files()

        self.store.budget.max_output_bytes = 192 * 1024 * 1024
        before_commands = self.store.budget.commands
        with self.assertRaisesRegex(STATE.StateStoreError, "shared Git output budget"):
            self.store._materialize_blob_to_owned_file(
                blob_oid=blob_oid,
                declared_size=self.store.budget.max_output_bytes + 1,
            )
        self.assertEqual(self.store.budget.commands, before_commands)
        self.no_owned_files()

        self.store.budget.max_commands = 600
        before_commands = self.store.budget.commands
        before_output = self.store.budget.output_bytes
        self.store.budget.max_output_bytes = before_output + len(payload) + 1 + 64 * 1024 - 1
        with self.assertRaisesRegex(STATE.StateStoreError, "output budget"):
            self.store._materialize_blob_to_owned_file(
                blob_oid=blob_oid,
                declared_size=len(payload),
            )
        self.assertEqual(self.store.budget.commands, before_commands)
        self.no_owned_files()

    def test_storage_full_during_file_fsync_never_seals(self) -> None:
        payload = b"storage failure fixture\n"
        blob_oid = self.seed_blob(payload)
        real_fsync = os.fsync

        def fail_regular_file_fsync(fd: int) -> None:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.ENOSPC, "fixture storage exhausted")
            real_fsync(fd)

        with mock.patch.object(STATE.os, "fsync", side_effect=fail_regular_file_fsync):
            with self.assertRaisesRegex(STATE.StateStoreError, "owned-file sealing failed"):
                self.store._materialize_blob_to_owned_file(
                    blob_oid=blob_oid,
                    declared_size=len(payload),
                )
        self.no_owned_files()

    def test_deadline_expiry_during_seal_and_final_revalidation_rejects(self) -> None:
        payload = b"deadline finalization fixture\n"
        blob_oid = self.seed_blob(payload)
        clock_value = [100.0]
        self.store.budget.clock = lambda: clock_value[0]
        self.store.budget.deadline = 200.0

        real_fsync = os.fsync
        fsync_count = [0]

        def expire_at_directory_fsync(fd: int) -> None:
            real_fsync(fd)
            fsync_count[0] += 1
            if fsync_count[0] == 2:
                clock_value[0] = 200.0

        with mock.patch.object(STATE.os, "fsync", side_effect=expire_at_directory_fsync):
            with self.assertRaisesRegex(STATE.StateStoreError, "deadline expired during sealing"):
                self.store._materialize_blob_to_owned_file(
                    blob_oid=blob_oid,
                    declared_size=len(payload),
                )
        self.assertGreaterEqual(fsync_count[0], 2)
        self.no_owned_files()

        self.store.budget.clock = time.monotonic
        self.store.budget.deadline = time.monotonic() + 60.0
        materialization = self.store._materialize_blob_to_owned_file(
            blob_oid=blob_oid,
            declared_size=len(payload),
        )
        clock_value[0] = 100.0
        self.store.budget.clock = lambda: clock_value[0]
        self.store.budget.deadline = 200.0
        real_stat = os.stat
        named_stat_count = [0]

        def expire_at_final_named_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == materialization.path.name and kwargs.get("dir_fd") is not None:
                named_stat_count[0] += 1
                if named_stat_count[0] == 2:
                    clock_value[0] = 200.0
            return result

        before_local_reads = self.store.local_materialization_read_bytes
        with mock.patch.object(STATE.os, "stat", side_effect=expire_at_final_named_stat):
            with self.assertRaisesRegex(STATE.StateStoreError, "verification deadline expired"):
                self.store.require_owned_blob_materialization(materialization)
        self.assertEqual(named_stat_count[0], 2)
        self.assertEqual(self.store.local_materialization_read_bytes, before_local_reads)

    def test_symlink_replacement_is_rejected_by_owner_validation(self) -> None:
        payload = b"symlink race fixture\n"
        blob_oid = self.seed_blob(payload)
        materialization = self.store._materialize_blob_to_owned_file(
            blob_oid=blob_oid,
            declared_size=len(payload),
        )
        readonly_view = self.root / "native-readonly-view"
        os.link(materialization.path, readonly_view)
        try:
            self.assertEqual(os.stat(materialization.path).st_nlink, 2)
            self.assertIs(
                self.store.require_owned_blob_materialization(materialization),
                materialization,
            )
        finally:
            readonly_view.unlink(missing_ok=True)

        original_path = materialization.path
        held_path = self.store._raw_blob_root / ".held-original"
        outside_path = self.root / "outside-file"
        outside_path.write_bytes(payload)
        original_path.rename(held_path)
        original_path.symlink_to(outside_path)
        try:
            with self.assertRaises(STATE.StateStoreError):
                self.store.require_owned_blob_materialization(materialization)
        finally:
            original_path.unlink(missing_ok=True)
            held_path.rename(original_path)

        raced_held_path = self.store._raw_blob_root / ".race-held-original"
        real_open = os.open

        def replace_with_symlink_before_open(path, flags, *args, **kwargs):
            if path == original_path.name and kwargs.get("dir_fd") is not None:
                original_path.rename(raced_held_path)
                original_path.symlink_to(outside_path)
            return real_open(path, flags, *args, **kwargs)

        try:
            with mock.patch.object(STATE.os, "open", side_effect=replace_with_symlink_before_open):
                with self.assertRaises(STATE.StateStoreError):
                    self.store.require_owned_blob_materialization(materialization)
        finally:
            original_path.unlink(missing_ok=True)
            raced_held_path.rename(original_path)
        self.assertIs(
            self.store.require_owned_blob_materialization(materialization),
            materialization,
        )


if __name__ == "__main__":
    unittest.main()
