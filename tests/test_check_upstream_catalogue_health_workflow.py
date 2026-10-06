from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml


ROOT = pathlib.Path(__file__).parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/upstream-catalogue-health.yml"
POLICY_PATH = ROOT / "policy/upstream-catalogue-health.json"


class UpstreamCatalogueHealthWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.workflow = yaml.load(cls.text, Loader=yaml.BaseLoader)
        cls.policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    def test_independent_hourly_clock_and_pipeline_completion_triggers(self) -> None:
        events = self.workflow["on"]
        schedule = events["schedule"]
        self.assertEqual([row["cron"] for row in schedule], [self.policy["health_workflow"]["watchdog_cron"]])
        self.assertEqual(events["workflow_run"]["workflows"], [
            "Upstream catalog refresh", "Process upstream catalogue",
            "Canonical update promotion", "Canonical update publication acknowledgement",
        ])
        self.assertEqual(events["workflow_run"]["types"], ["completed"])
        self.assertNotIn("pull_request", events)

    def test_permissions_are_limited_to_observing_and_writing_owned_health_state(self) -> None:
        self.assertEqual(self.workflow["permissions"], {"actions": "read", "contents": "write"})
        self.assertEqual(self.workflow["concurrency"]["group"], "upstream-catalogue-health-state")
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.text)
        self.assertNotIn("secrets.", self.text)
        self.assertNotIn("actions: write", self.text)
        self.assertNotIn("pull-requests: write", self.text)

    def test_workflow_reads_all_three_durable_inputs_and_runs_checker_and_writer(self) -> None:
        inputs = self.workflow["jobs"]["inspect"]["steps"][1:]
        text = "\n".join(str(step.get("run", "")) for step in inputs)
        self.assertIn(".processor_state.branch", text)
        self.assertIn(".promotion_state.branch", text)
        self.assertIn(".health_state.branch", text)
        self.assertIn("date -u", text)
        self.assertIn("scripts/check-upstream-catalogue-health.py", text)
        self.assertIn("scripts/persist-upstream-catalogue-health.py", text)
        self.assertIn("git archive", text)
        self.assertIn("git show", text)

    def test_checker_checkout_has_ancestry_and_promotion_verifier_dependencies(self) -> None:
        checkout = next(
            step for step in self.workflow["jobs"]["inspect"]["steps"]
            if step.get("name") == "Checkout the trusted default branch"
        )
        self.assertEqual(checkout["with"]["fetch-depth"], "0")
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        setup = next(
            step for step in self.workflow["jobs"]["inspect"]["steps"]
            if step.get("name") == "Set up explicit UTC evaluation time and dependencies"
        )
        self.assertIn("'PyYAML==6.0.2'", setup["run"])
        evaluate = next(
            step for step in self.workflow["jobs"]["inspect"]["steps"]
            if step.get("name") == "Evaluate GitHub observations and durable checkpoints"
        )
        self.assertEqual(evaluate["env"]["HEALTH_EVALUATOR_SOURCE_SHA"], "${{ github.sha }}")
        self.assertIn("--evaluator-source-sha \"${HEALTH_EVALUATOR_SOURCE_SHA}\"", evaluate["run"])

    def test_state_update_uses_bounded_non_force_fast_forward_push(self) -> None:
        steps = self.workflow["jobs"]["inspect"]["steps"]
        persist_step = next(step for step in steps if "Persist receipt" in step.get("name", ""))
        script = persist_step["run"]
        self.assertIn(".health_state.push_attempts", script)
        self.assertIn("for attempt in $(seq 1", script)
        self.assertIn("prepare-upstream-catalogue-health-state-worktree.sh", script)
        self.assertNotIn("push origin --force", script)
        self.assertNotIn("push origin --force-with-lease", script)

    def test_uploaded_evidence_is_run_bound_and_retained(self) -> None:
        upload = next(step for step in self.workflow["jobs"]["inspect"]["steps"] if "upload-artifact" in step.get("uses", ""))
        self.assertIn("${{ github.run_id }}-${{ github.run_attempt }}", upload["with"]["name"])
        self.assertEqual(upload["with"]["retention-days"], "30")
        self.assertEqual(upload["if"], "always()")

    def test_orphan_branch_bootstrap_and_checkpoint_archive_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            remote = base / "remote.git"
            checkout = base / "checkout"
            subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "init", "--initial-branch=main", str(checkout)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Health test"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.email", "health-test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(remote)], check=True)
            (checkout / ".gitattributes").write_text("data/registry.json filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
            (checkout / "data").mkdir()
            (checkout / "data/registry.json").write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:" + "a" * 64 + "\nsize 123456789\n", encoding="utf-8",
            )
            state_dir = checkout / ".datapan/upstream-catalogue-state/sources/data_go_kr"
            (state_dir / "generations").mkdir(parents=True)
            (state_dir / "index.json").write_text("{}\n", encoding="utf-8")
            (state_dir / "generations/generation.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(checkout), "config", "filter.lfs.clean", "cat"], check=True)
            subprocess.run([
                "git", "-C", str(checkout), "config", "filter.lfs.smudge",
                "bash -c 'test \"$GIT_LFS_SKIP_SMUDGE\" = 1 || { touch \"$LFS_SMUDGE_PROBE\"; exit 9; }; cat' --",
            ], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "filter.lfs.required", "true"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "lfs.allowincompletepush", "true"], check=True)
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-m", "fixture main state"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)

            health_branch = self.policy["health_state"]["branch"]
            state_worktree = base / "health-state-worktree"
            env = dict(os.environ, LFS_SMUDGE_PROBE=str(base / "lfs-smudge-ran"))
            prepared = subprocess.run([
                "bash", str(ROOT / "scripts/prepare-upstream-catalogue-health-state-worktree.sh"),
                str(checkout), health_branch, str(state_worktree),
            ], check=True, capture_output=True, text=True, env=env)
            self.assertIn("branch_exists=false", prepared.stdout)
            self.assertEqual((state_worktree / ".git").exists(), True)
            self.assertFalse((base / "lfs-smudge-ran").exists())
            self.assertEqual(subprocess.run(["git", "-C", str(state_worktree), "branch", "--show-current"], check=True, capture_output=True, text=True).stdout.strip(), health_branch)

            archive_target = base / "processor-input"
            archive_target.mkdir()
            processor_branch = self.policy["processor_state"]["branch"]
            subprocess.run(["git", "-C", str(checkout), "checkout", "-b", processor_branch], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "push", "origin", processor_branch], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "fetch", "origin", f"refs/heads/{processor_branch}:refs/remotes/origin/{processor_branch}"], check=True, capture_output=True)
            subprocess.run([
                "bash", "-euo", "pipefail", "-c",
                f'git -C "{checkout}" archive refs/remotes/origin/{processor_branch} "{self.policy["processor_state"]["root"]}" | tar -x -C "{archive_target}"',
            ], check=True, capture_output=True)
            expected_index = archive_target / self.policy["processor_state"]["root"] / "sources/data_go_kr/index.json"
            self.assertTrue(expected_index.is_file())


if __name__ == "__main__":
    unittest.main()
