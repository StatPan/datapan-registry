import importlib.util
import pathlib
import subprocess
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "bind_registry_publication_source.py"
SPEC = importlib.util.spec_from_file_location("source_binding", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RegistryPublicationSourceBindingTest(unittest.TestCase):
    def git(self, repo: pathlib.Path, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    def repository(self, root: pathlib.Path) -> tuple[str, str]:
        self.git(root, "init", "-b", "main")
        self.git(root, "config", "user.email", "test@example.invalid")
        self.git(root, "config", "user.name", "Test")
        (root / "manifest.json").write_text('{"version":1}\n', encoding="utf-8")
        self.git(root, "add", "manifest.json")
        self.git(root, "commit", "-m", "first")
        first = self.git(root, "rev-parse", "HEAD")
        (root / "manifest.json").write_text('{"version":2}\n', encoding="utf-8")
        self.git(root, "commit", "-am", "second")
        return first, self.git(root, "rev-parse", "HEAD")

    def bind(self, root: pathlib.Path, source: str, workflow: str, **overrides):
        values = {
            "event_name": "workflow_dispatch",
            "git_ref": "refs/heads/main",
            "repository": "StatPan/datapan-registry",
        }
        values.update(overrides)
        return MODULE.bind(root, source_sha=source, workflow_sha=workflow, **values)

    def test_dispatch_binds_an_exact_ancestor_after_main_moves(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            source, workflow = self.repository(root)
            receipt = self.bind(root, source, workflow)
            self.assertEqual(receipt["source_sha"], source)
            self.assertEqual(receipt["workflow_sha"], workflow)
            self.assertEqual(receipt["status"], "bound")
            self.assertEqual(len(receipt["manifest_sha256"]), 64)

    def test_rejects_malformed_missing_and_nonexistent_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            _, workflow = self.repository(root)
            for source in ("", "ABC", "a" * 39, "f" * 40):
                with self.subTest(source=source), self.assertRaises(MODULE.BindingError):
                    self.bind(root, source, workflow)

    def test_rejects_non_ancestor_and_non_main_dispatch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            _, workflow = self.repository(root)
            self.git(root, "checkout", "--orphan", "unrelated")
            (root / "manifest.json").write_text('{"other":true}\n', encoding="utf-8")
            self.git(root, "add", "manifest.json")
            self.git(root, "commit", "-m", "unrelated")
            unrelated = self.git(root, "rev-parse", "HEAD")
            self.git(root, "checkout", "main")
            with self.assertRaisesRegex(MODULE.BindingError, "not an ancestor"):
                self.bind(root, unrelated, workflow)
            with self.assertRaisesRegex(MODULE.BindingError, "refs/heads/main"):
                self.bind(root, workflow, workflow, git_ref="refs/heads/release")

    def test_rejects_worktree_or_validation_source_mismatch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            source, workflow = self.repository(root)
            with self.assertRaisesRegex(MODULE.BindingError, "workflow SHA"):
                self.bind(root, source, source)
            with self.assertRaisesRegex(MODULE.BindingError, "bind source_sha"):
                self.bind(root, source, workflow, event_name="push", git_ref="refs/heads/main")


if __name__ == "__main__":
    unittest.main()
