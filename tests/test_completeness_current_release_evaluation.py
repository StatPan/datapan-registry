import hashlib
import importlib.util
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/generate-completeness-proof-rollup.py"
SNAPSHOT_PATH = "reports/completeness-proof-rollup.json"
AS_OF = "2026-10-20T00:00:00Z"


def _git(root: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


class CurrentReleaseEvaluationTest(unittest.TestCase):
    """Exercise the manifest-outside evaluator against a real Git repository.

    The fixture borrows the repository's committed evidence inputs and object
    store, then commits a genuinely generated v2 snapshot. Classification,
    schema validation, and the CLI output path are asserted from real calls:
    no validator, rebuild, or git read is mocked.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = cls._load_module()
        cls.fixture = pathlib.Path(tempfile.mkdtemp(prefix="completeness-current-release-"))
        cls.addClassCleanup(shutil.rmtree, cls.fixture, True)
        checkout = cls.fixture / "checkout"
        subprocess.run(
            ["git", "init", "--quiet", str(checkout)], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # Borrow the repository object store read-only so the fixture can resolve
        # the admission commits its committed evidence names, without copying them.
        alternates = checkout / ".git/objects/info/alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        object_store = subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "objects"], cwd=ROOT, text=True,
        ).strip()
        alternates.write_text(object_store + "\n", encoding="utf-8")
        _git(checkout, "config", "user.email", "completeness-test@example.invalid")
        _git(checkout, "config", "user.name", "Completeness Test")
        _git(checkout, "fetch", "--no-tags", str(ROOT), "HEAD")
        fetched = _git(checkout, "rev-parse", "FETCH_HEAD").stdout.strip()
        _git(checkout, "checkout", "--quiet", "--detach", fetched)
        # Evidence admission commits are ancestors of the borrowed HEAD, and the
        # snapshot rebuild resolves them against the fetched trusted main.
        _git(checkout, "update-ref", "refs/remotes/origin/main", fetched)
        cls._commit_generated_snapshot(checkout)
        cls.historical_sha = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        (checkout / ".current-release-successor").write_text("successor\n", encoding="utf-8")
        _git(checkout, "add", ".current-release-successor")
        _git(checkout, "commit", "--quiet", "-m", "fixture successor of the evaluated snapshot")
        cls.current_main_sha = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        _git(checkout, "update-ref", "refs/remotes/origin/main", cls.current_main_sha)
        _git(checkout, "checkout", "--quiet", "-b", "fixture-candidate", cls.historical_sha)
        (checkout / ".current-release-candidate").write_text("candidate\n", encoding="utf-8")
        _git(checkout, "add", ".current-release-candidate")
        _git(checkout, "commit", "--quiet", "-m", "fixture candidate absent from trusted main")
        cls.candidate_sha = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        _git(checkout, "checkout", "--quiet", "--detach", cls.current_main_sha)
        cls.checkout = checkout
        cls.index_path = checkout / "reports/completeness-proof-inputs.json"

    @staticmethod
    def _load_module():
        spec = importlib.util.spec_from_file_location("completeness_current_release_rollup", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @classmethod
    def _commit_generated_snapshot(cls, checkout: pathlib.Path) -> None:
        """Commit the snapshot the generator actually produces from named inputs."""
        snapshot = cls.module.build_report(
            root=checkout, input_root=checkout, input_index_path=checkout / "reports/completeness-proof-inputs.json",
        )
        snapshot_raw = cls.module.render_json(snapshot)
        (checkout / SNAPSHOT_PATH).write_bytes(snapshot_raw)
        (checkout / "reports/completeness-proof-rollup.md").write_bytes(cls.module.render_markdown(snapshot))
        manifest_path = checkout / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        bound = [
            row for row in manifest["artifacts"] if row.get("path") == SNAPSHOT_PATH
        ]
        if len(bound) != 1:
            raise AssertionError("fixture release manifest must bind exactly one completeness snapshot")
        bound[0]["bytes"] = len(snapshot_raw)
        bound[0]["sha256"] = hashlib.sha256(snapshot_raw).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        _git(checkout, "add", SNAPSHOT_PATH, "reports/completeness-proof-rollup.md", "manifest.json")
        _git(checkout, "commit", "--quiet", "-m", "fixture generated v2 completeness snapshot")

    def _run(self, *args: str, repo: pathlib.Path = ROOT) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["uv", "run", "--no-project", "--python", "3.12", "--with", "jsonschema", "--with", "PyYAML==6.0.2",
             "python", str(SCRIPT), *args],
            cwd=repo, text=True, capture_output=True,
        )

    def _checkout(self, sha: str) -> None:
        _git(self.checkout, "checkout", "--quiet", "--detach", sha)

    def _evaluate(self, sha: str) -> dict:
        self._checkout(sha)
        return self.module.evaluate_current_release(
            root=self.checkout, input_root=self.checkout, input_index_path=self.index_path,
            release_source_sha=sha, trusted_main_sha=self.current_main_sha, evaluation_as_of=AS_OF,
        )

    def _assert_not_current(self, evaluation: dict, kind: str) -> None:
        self.assertEqual(evaluation["target_kind"], kind)
        self.assertEqual(evaluation["summary"]["claims"]["current"], 0)
        self.assertEqual(evaluation["summary"]["claims"]["updated"], 0)
        self.assertTrue(evaluation["scopes"])
        for scope in evaluation["scopes"]:
            self.assertEqual(scope["relation"], kind)
            self.assertFalse(scope["claims"]["current"])
            self.assertFalse(scope["claims"]["updated"])
        self.module.validate_schema(
            evaluation, self.checkout / self.module.CURRENT_EVALUATION_SCHEMA_PATH,
            "current release evaluation",
        )

    def test_cli_rejects_output_outside_manifest(self) -> None:
        result = self._run(
            "--repo-root", str(ROOT), "--evaluate-current-release", "--release-source-sha", "a" * 40,
            "--evaluation-as-of", AS_OF, "--evaluation-output", "reports/evaluation.json", "--check",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the release manifest", result.stderr)

    def test_cli_rejects_snapshot_require_claim_without_evaluator(self) -> None:
        result = self._run("--repo-root", str(ROOT), "--require-claim", "scope:current", "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authenticated current-release evaluation", result.stderr)

    def test_function_rejects_head_trusted_main_mismatch(self) -> None:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        with self.assertRaisesRegex(ValueError, "target or trusted main"):
            self.module.evaluate_current_release(
                root=ROOT, input_root=ROOT, input_index_path=ROOT / "reports/completeness-proof-inputs.json",
                release_source_sha=head, trusted_main_sha="0" * 40, evaluation_as_of=AS_OF,
            )

    def test_function_rejects_borrowed_target_identity(self) -> None:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        with self.assertRaisesRegex(ValueError, "target or trusted main"):
            self.module.evaluate_current_release(
                root=ROOT, input_root=ROOT, input_index_path=ROOT / "reports/completeness-proof-inputs.json",
                release_source_sha="1" * 40, trusted_main_sha=head, evaluation_as_of=AS_OF,
            )

    def test_cli_requires_evaluation_arguments(self) -> None:
        result = self._run("--repo-root", str(ROOT), "--evaluate-current-release", "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("current evaluation requires", result.stderr)

    def test_checked_out_trusted_main_is_current_main_and_unavailable_without_live_evidence(self) -> None:
        """HEAD equal to fetched origin/main is current, but not a current claim."""
        evaluation = self._evaluate(self.current_main_sha)
        self._assert_not_current(evaluation, "current_main")
        self.assertEqual(evaluation["trusted_main_sha"], self.current_main_sha)
        self.assertEqual(evaluation["target"]["source_sha"], self.current_main_sha)
        self.assertEqual(evaluation["summary"]["statuses"]["unavailable"], len(evaluation["scopes"]))
        self.assertEqual(evaluation["summary"]["statuses"]["applicable"], 0)
        self.assertEqual(evaluation["target_applicability"]["status"], "unavailable")
        for scope in evaluation["scopes"]:
            self.assertEqual(scope["status"], "unavailable")

    def test_ancestor_of_trusted_main_is_historical_without_current_claims(self) -> None:
        """A commit strictly behind fetched origin/main is historical, never current."""
        self.assertEqual(
            _git(self.checkout, "merge-base", "--is-ancestor", self.historical_sha, self.current_main_sha).returncode,
            0,
        )
        evaluation = self._evaluate(self.historical_sha)
        self._assert_not_current(evaluation, "historical")
        self.assertEqual(evaluation["summary"]["statuses"]["historical"], len(evaluation["scopes"]))
        self.assertEqual(evaluation["target_applicability"]["status"], "not_applicable")
        for scope in evaluation["scopes"]:
            self.assertEqual(scope["status"], "historical")

    def test_commit_absent_from_trusted_main_is_candidate_without_current_claims(self) -> None:
        """A commit unreachable from fetched origin/main is a candidate, never current."""
        self.assertEqual(
            _git(
                self.checkout, "merge-base", "--is-ancestor", self.candidate_sha, self.current_main_sha, check=False,
            ).returncode,
            1,
        )
        evaluation = self._evaluate(self.candidate_sha)
        self._assert_not_current(evaluation, "candidate")
        self.assertEqual(evaluation["summary"]["statuses"]["historical"], len(evaluation["scopes"]))
        self.assertEqual(evaluation["summary"]["statuses"]["unavailable"], 0)
        self.assertEqual(evaluation["target_applicability"]["status"], "not_applicable")

    def test_cli_writes_evaluation_under_datapan_ci_and_keeps_manifest_unregistered(self) -> None:
        """The evaluator output lands only under .datapan/ci and never in the manifest."""
        self._checkout(self.current_main_sha)
        output = self.checkout / ".datapan/ci/current-release-evaluation.json"
        result = self._run(
            "--repo-root", str(self.checkout), "--input-root", str(self.checkout),
            "--input-index", str(self.index_path), "--evaluate-current-release",
            "--release-source-sha", self.current_main_sha, "--evaluation-as-of", AS_OF,
            "--evaluation-output", ".datapan/ci/current-release-evaluation.json", "--write",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(output.is_file(), result.stdout + result.stderr)
        written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(written["schema_version"], "datapan.completeness-current-release-evaluation.v1")
        self.assertEqual(written["target_kind"], "current_main")
        self.module.validate_schema(
            written, self.checkout / self.module.CURRENT_EVALUATION_SCHEMA_PATH, "current release evaluation",
        )
        manifest_after = (self.checkout / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn("current-release-evaluation", manifest_after)
        self.assertNotIn(".datapan/ci/", manifest_after)
        registered_paths = {row["path"] for row in json.loads(manifest_after)["artifacts"]}
        self.assertNotIn(".datapan/ci/current-release-evaluation.json", registered_paths)


if __name__ == "__main__":
    unittest.main()
