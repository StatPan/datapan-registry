from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
import datetime as dt
import hashlib
import zipfile

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE_TOOL = ROOT / "scripts" / "upstream-catalogue-state-branch.py"
WORKFLOW = ROOT / ".github" / "workflows" / "upstream-catalogue-process.yml"
REPOSITORY = "StatPan/datapan-registry"
BRANCH = "automation/upstream-catalogue-state"
STATE_ROOT = ".datapan/upstream-catalogue-state"


def git(path: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(path), *args], text=True, capture_output=True,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result


class StateBranchWorkflowFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="catalogue-state-workflow-")
        self.root = pathlib.Path(self.temp.name)
        self.remote = self.root / "origin.git"
        self.source = self.root / "source"
        self.state = self.root / "state"
        self.competitor = self.root / "competitor"
        git(self.root, "init", "--bare", str(self.remote))
        self.source.mkdir()
        git(self.source, "init", "-b", "main")
        git(self.source, "config", "user.name", "fixture")
        git(self.source, "config", "user.email", "fixture@example.test")
        (self.source / "README.md").write_text("workflow fixture\n", encoding="utf-8")
        git(self.source, "add", "README.md")
        git(self.source, "commit", "-m", "seed")
        git(self.source, "remote", "add", "origin", str(self.remote))
        git(self.source, "push", "-u", "origin", "main")
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_state(self, command: str, *args: str, ok: bool = True) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        result = subprocess.run(
            ["python3", str(STATE_TOOL), command, *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if ok and result.returncode:
            self.fail(result.stderr or result.stdout)
        payload = json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}
        return result, payload

    def prepare(self, path: pathlib.Path | None = None) -> tuple[str, dict[str, str]]:
        worktree = path or self.state
        _result, payload = self.run_state(
            "prepare", "--source-repo", str(self.source), "--worktree", str(worktree),
            "--branch", BRANCH, "--repository", REPOSITORY,
        )
        return payload["old_sha"], payload

    def push(self, worktree: pathlib.Path, old_sha: str, *, ok: bool = True) -> dict[str, str]:
        _result, payload = self.run_state(
            "commit-push", "--worktree", str(worktree), "--branch", BRANCH,
            "--repository", REPOSITORY, "--expected-old-sha", old_sha,
            "--message", "fixture state update", ok=ok,
        )
        return payload

    def test_first_bootstrap_and_archive_paths_use_only_owned_state_root(self) -> None:
        old_sha, _ = self.prepare()
        self.assertEqual(old_sha, "")
        marker = self.state / STATE_ROOT / "state-root.json"
        self.assertTrue(marker.is_file())
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["repository"], REPOSITORY)
        generation_id = "a" * 64
        generation = self.state / STATE_ROOT / "sources" / "data_go_kr" / "generations" / f"{generation_id}.json"
        generation.parent.mkdir(parents=True)
        generation.write_text('{"fixture":true}\n', encoding="utf-8")
        first = self.push(self.state, old_sha)
        self.assertEqual(first["changed"], "true")

        second_old_sha, _ = self.prepare(self.root / "state-again")
        self.assertEqual(second_old_sha, first["new_sha"])
        entries = git(self.root / "state-again", "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
        self.assertIn(f"{STATE_ROOT}/state-root.json", entries)
        self.assertIn(f"{STATE_ROOT}/sources/data_go_kr/generations/{generation_id}.json", entries)
        self.assertTrue(entries)
        self.assertTrue(all(path.startswith(f"{STATE_ROOT}/") for path in entries))

    def test_remote_change_after_checkout_fails_compare_and_swap(self) -> None:
        old_sha, _ = self.prepare()
        self.push(self.state, old_sha)
        stale_sha, _ = self.prepare(self.root / "stale")
        stale_file = self.root / "stale" / STATE_ROOT / "quarantine" / f"{'b' * 64}.json"
        stale_file.parent.mkdir(parents=True)
        stale_file.write_text('{"stale":true}\n', encoding="utf-8")

        git(self.root, "clone", str(self.remote), str(self.competitor))
        git(self.competitor, "checkout", "--track", "-b", BRANCH, f"origin/{BRANCH}")
        git(self.competitor, "config", "user.name", "fixture")
        git(self.competitor, "config", "user.email", "fixture@example.test")
        winner = self.competitor / STATE_ROOT / "quarantine" / f"{'c' * 64}.json"
        winner.parent.mkdir(parents=True)
        winner.write_text('{"winner":true}\n', encoding="utf-8")
        git(self.competitor, "add", STATE_ROOT)
        git(self.competitor, "commit", "-m", "concurrent state update")
        git(self.competitor, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
        winner_sha = git(self.competitor, "rev-parse", "HEAD").stdout.strip()

        result, _ = self.run_state(
            "commit-push", "--worktree", str(self.root / "stale"), "--branch", BRANCH,
            "--repository", REPOSITORY, "--expected-old-sha", stale_sha,
            "--message", "stale update", ok=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compare_and_swap_conflict", result.stderr)
        self.assertEqual(git(self.competitor, "ls-remote", "origin", f"refs/heads/{BRANCH}").stdout.split()[0], winner_sha)
        self.assertFalse((self.competitor / STATE_ROOT / "quarantine" / f"{'b' * 64}.json").exists())

    def test_out_of_root_edit_is_preserved_and_never_pushed(self) -> None:
        old_sha, _ = self.prepare()
        self.push(self.state, old_sha)
        previous_remote = git(self.source, "ls-remote", "origin", f"refs/heads/{BRANCH}").stdout.split()[0]
        human_edit = self.state / "README.md"
        human_edit.write_text("human edit\n", encoding="utf-8")
        result, _ = self.run_state(
            "commit-push", "--worktree", str(self.state), "--branch", BRANCH,
            "--repository", REPOSITORY, "--expected-old-sha", previous_remote,
            ok=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside_owned_root", result.stderr)
        self.assertEqual(human_edit.read_text(encoding="utf-8"), "human edit\n")
        self.assertEqual(git(self.source, "ls-remote", "origin", f"refs/heads/{BRANCH}").stdout.split()[0], previous_remote)

    def test_force_with_lease_rejects_a_change_during_the_push(self) -> None:
        old_sha, _ = self.prepare()
        self.push(self.state, old_sha)
        stale_sha, _ = self.prepare(self.root / "stale-push")
        stale_file = self.root / "stale-push" / STATE_ROOT / "quarantine" / f"{'d' * 64}.json"
        stale_file.parent.mkdir(parents=True)
        stale_file.write_text('{"stale":true}\n', encoding="utf-8")

        git(self.root, "clone", str(self.remote), str(self.competitor))
        git(self.competitor, "checkout", "--track", "-b", BRANCH, f"origin/{BRANCH}")
        git(self.competitor, "config", "user.name", "fixture")
        git(self.competitor, "config", "user.email", "fixture@example.test")
        winner = self.competitor / STATE_ROOT / "quarantine" / f"{'e' * 64}.json"
        winner.parent.mkdir(parents=True)
        winner.write_text('{"winner":true}\n', encoding="utf-8")
        git(self.competitor, "add", STATE_ROOT)
        git(self.competitor, "commit", "-m", "concurrent winner")
        winner_sha = git(self.competitor, "rev-parse", "HEAD").stdout.strip()

        hook_dir = pathlib.Path(git(self.root / "stale-push", "rev-parse", "--git-path", "hooks").stdout.strip())
        if not hook_dir.is_absolute():
            hook_dir = self.root / "stale-push" / hook_dir
        hook_dir.mkdir(parents=True, exist_ok=True)
        hook = hook_dir / "pre-push"
        hook.write_text(
            "#!/bin/sh\n"
            "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX\n"
            f"git -C {shlex.quote(str(self.competitor))} push origin HEAD:refs/heads/{BRANCH}\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

        result, _ = self.run_state(
            "commit-push", "--worktree", str(self.root / "stale-push"), "--branch", BRANCH,
            "--repository", REPOSITORY, "--expected-old-sha", stale_sha,
            "--message", "stale push", ok=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("git_push_failed", result.stderr)
        remote_sha = git(self.competitor, "ls-remote", "origin", f"refs/heads/{BRANCH}").stdout.split()[0]
        self.assertEqual(remote_sha, winner_sha)
        self.assertFalse((self.competitor / STATE_ROOT / "quarantine" / f"{'d' * 64}.json").exists())


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_process_install_declares_yaml_and_real_derivation_cli_reaches_durable_index_guard(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        install_position = next(
            index for index, step in enumerate(steps)
            if step.get("name") == "Materialize canonical registry and install validators"
        )
        derivation_position = next(
            index for index, step in enumerate(steps) if step.get("id") == "derivation"
        )
        install = steps[install_position]
        self.assertEqual(install["if"], "steps.select.outputs.decision == 'process'")
        self.assertIn("'jsonschema==4.25.1' 'PyYAML==6.0.2'", install["run"])
        self.assertLess(install_position, derivation_position)

        with tempfile.TemporaryDirectory(prefix="catalogue-derivation-bootstrap-") as temp:
            root = pathlib.Path(temp)
            state_dir = root / "state"
            index_path = state_dir / "sources/data_go_kr/index.json"
            index_path.parent.mkdir(parents=True)
            index_bytes = json.dumps({
                "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
                "generations": [],
                "detail_queue_cursor": 0,
                "detail_retry_state": {},
            }, sort_keys=True).encode("utf-8") + b"\n"
            index_path.write_bytes(index_bytes)
            output_dir = root / "derivation-output"
            github_output = root / "github-output"
            unavailable_path = root / "not-read-before-index-selection"
            env = {
                **os.environ,
                "PATH": str(root / "empty-bin"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            env.pop("GH_TOKEN", None)
            env.pop("GITHUB_TOKEN", None)
            command = [
                sys.executable,
                str(ROOT / "scripts/prepare-upstream-catalogue-derivation.py"),
                "--main-root", str(ROOT),
                "--source-root", str(ROOT),
                "--state-dir", str(state_dir),
                "--target-generation-id", "a" * 64,
                "--repository", REPOSITORY,
                "--default-branch", "main",
                "--collector-admission", str(unavailable_path / "admission.json"),
                "--candidate", str(unavailable_path / "candidate.json"),
                "--refresh-evidence", str(unavailable_path / "refresh.json"),
                "--diff", str(unavailable_path / "diff.json"),
                "--resume-bundle", str(unavailable_path / "resume-bundle"),
                "--output-dir", str(output_dir),
                "--github-output", str(github_output),
            ]
            result = subprocess.run(
                command, cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(json.loads(result.stderr), {
                "eligible": False,
                "reason": "selected_generation_not_durable",
            })
            self.assertEqual(index_path.read_bytes(), index_bytes)
            self.assertFalse(output_dir.exists())
            self.assertFalse(github_output.exists())
            self.assertFalse(unavailable_path.exists())

    def test_new_observation_without_selected_parent_returns_without_reading_admission_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="catalogue-new-observation-preparation-") as temp:
            root = pathlib.Path(temp)
            state_dir = root / "state"
            index_path = state_dir / "sources/data_go_kr/index.json"
            index_path.parent.mkdir(parents=True)
            index_bytes = json.dumps({
                "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
                "generations": [], "detail_queue_cursor": 0, "detail_retry_state": {},
            }, sort_keys=True).encode("utf-8") + b"\n"
            index_path.write_bytes(index_bytes)
            output_dir = root / "output"
            github_output = root / "github-output"
            unavailable = root / "must-not-be-read"
            command = [
                sys.executable,
                str(ROOT / "scripts/prepare-upstream-catalogue-derivation.py"),
                "--main-root", str(unavailable / "main"),
                "--source-root", str(unavailable / "source"),
                "--state-dir", str(state_dir),
                "--repository", REPOSITORY, "--default-branch", "main",
                "--collector-admission", str(unavailable / "admission.json"),
                "--candidate", str(unavailable / "candidate.json"),
                "--refresh-evidence", str(unavailable / "evidence.json"),
                "--diff", str(unavailable / "diff.json"),
                "--resume-bundle", str(unavailable / "bundle"),
                "--output-dir", str(output_dir), "--github-output", str(github_output),
            ]
            result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {
                "eligible": False,
                "reason": "new_observation_no_existing_processor_parent",
            })
            self.assertIn("derivation_enabled=false", github_output.read_text(encoding="utf-8"))
            self.assertEqual(index_path.read_bytes(), index_bytes)
            self.assertFalse(unavailable.exists())

    def test_triggers_and_trusted_run_gates_are_explicit(self) -> None:
        triggers = self.workflow["on"]
        self.assertEqual(triggers["workflow_run"]["workflows"], ["Upstream catalog refresh"])
        self.assertEqual(triggers["workflow_run"]["types"], ["completed"])
        self.assertEqual(triggers["schedule"][0]["cron"], "17 * * * *")
        self.assertIn("workflow_dispatch", triggers)
        dispatch = triggers["workflow_dispatch"].get("inputs", {})
        self.assertEqual(dispatch.get("producer_run_id", {}).get("required"), "false")
        job_if = self.workflow["jobs"]["process"]["if"]
        self.assertIn("workflow_run.conclusion == 'success'", job_if)
        self.assertIn("workflow_run.head_repository.full_name == github.repository", job_if)
        self.assertIn("workflow_run.head_branch == github.event.repository.default_branch", job_if)
        self.assertIn("github.ref == format('refs/heads/{0}', github.event.repository.default_branch)", job_if)
        self.assertIn('"path": ".github/workflows/upstream-catalog-refresh.yml"', self.text)
        dispatch = triggers["workflow_dispatch"].get("inputs", {})
        self.assertEqual(dispatch.get("recover_failed_processor_run_id", {}).get("required"), "false")

    def test_durable_reservation_and_output_artifact_contract_are_wired(self) -> None:
        self.assertIn("automation/upstream-catalogue-state", self.text)
        self.assertIn("--claim-only", self.text)
        self.assertIn("--require-durable-reservation", self.text)
        self.assertIn("--processor-run-id", self.text)
        self.assertIn("--processor-artifact-run-id", self.text)
        self.assertIn("--bind-output-artifact-id", self.text)
        self.assertIn("upstream-catalogue-processing-${{ github.run_id }}-${{ github.run_attempt }}", self.text)
        self.assertIn("retention-days: 30", self.text)
        self.assertIn("processing_artifact_id", self.text)
        self.assertIn("processing_status", self.text)

    def test_scheduled_handoff_uses_authenticated_bounded_discovery_before_selection(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        handoff = next(step for step in steps if step.get("id") == "handoff")
        selector = next(step for step in steps if step.get("id") == "select")
        producer_attempt = next(step for step in steps if step.get("id") == "producer_attempt")
        artifact = next(step for step in steps if step.get("id") == "input_artifact")
        self.assertEqual(handoff["if"], "github.event_name == 'schedule'")
        self.assertEqual(handoff["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertIn("--state-dir", handoff["run"])
        self.assertIn("--reserve-api-requests 3", producer_attempt["run"])
        self.assertLess(steps.index(handoff), steps.index(selector))
        self.assertLess(steps.index(selector), steps.index(producer_attempt))
        self.assertLess(steps.index(producer_attempt), steps.index(artifact))

    def test_state_compare_and_swap_precedes_the_live_detail_worker(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        ids = [step.get("id", "") for step in steps]
        claim_position = ids.index("claim")
        push_position = ids.index("claim_push")
        worker_position = ids.index("run_processor")
        self.assertLess(claim_position, push_position)
        self.assertLess(push_position, worker_position)
        claim = steps[claim_position]["run"]
        push = steps[push_position]["run"]
        worker = steps[worker_position]["run"]
        self.assertIn("--claim-only", claim)
        self.assertIn("--expected-old-sha", push)
        self.assertIn("steps.claim.outputs.checkpoint_persisted == 'true'", steps[push_position]["if"])
        self.assertIn("steps.claim.outputs.exit_code != '1'", steps[push_position]["if"])
        self.assertIn("checkpoint_persisted=${checkpoint_persisted}", claim)
        self.assertIn("rm -f .datapan/ci/upstream-catalogue-processing/upstream-catalogue-checkpoint-receipt.json", claim)
        self.assertIn("steps.claim_push.outputs.claim_sha", steps[worker_position]["if"])
        self.assertIn("--require-durable-reservation", worker)
        self.assertIn("python3 ../bootstrap/scripts/process-upstream-catalogue-candidate.py", worker)

    def test_failed_same_observation_parent_authentication_cannot_fall_back_to_legacy_claim(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        derivation_position = next(
            index for index, step in enumerate(steps) if step.get("id") == "derivation"
        )
        claim_position = next(index for index, step in enumerate(steps) if step.get("id") == "claim")
        derivation = steps[derivation_position]
        claim = steps[claim_position]
        self.assertLess(derivation_position, claim_position)
        self.assertIn("steps.derivation.outcome != 'failure'", claim["if"])
        # A failed strict intake (for example, an unavailable journal or a
        # same-A canonical row that no longer matches) must prevent the
        # legacy claim step from manufacturing a different generation ID.
        self.assertIn("--claim-only", claim["run"])
        self.assertEqual(claim["env"]["TARGET_GENERATION_ID"], "${{ steps.select.outputs.generation_id }}")
        self.assertEqual(derivation["id"], "derivation")
        self.assertEqual(
            derivation["env"]["PRODUCER_RUN_ATTEMPT"],
            "${{ steps.producer_attempt.outputs.run_attempt }}",
        )
        self.assertEqual(
            derivation["env"]["ARTIFACT_DIGEST"],
            "${{ steps.producer_attempt.outputs.artifact_digest_sha256 }}",
        )
        self.assertIn("--collector-archive", derivation["run"])
        self.assertIn("--producer-observe-job-started-at", derivation["run"])
        self.assertIn("--producer-artifact-size-bytes", derivation["run"])
        for step_id in ("claim", "run_processor"):
            consumer = next(step for step in steps if step.get("id") == step_id)
            self.assertEqual(
                consumer["env"]["PRODUCER_RUN_ATTEMPT"],
                "${{ steps.producer_attempt.outputs.run_attempt }}",
            )
            self.assertIn("--producer-run-attempt", consumer["run"])
            self.assertIn("--producer-artifact-digest-sha256", consumer["run"])

    def test_trusted_bootstrap_code_uses_exact_producer_worktree_inputs(self) -> None:
        job = self.workflow["jobs"]["process"]
        steps = job["steps"]
        by_id = {step.get("id"): step for step in steps if step.get("id")}
        checkout = next(step for step in steps if step.get("name") == "Checkout workflow source")
        self.assertEqual(checkout["with"]["path"], "bootstrap")
        producer = by_id["producer"]["run"]
        self.assertIn('git worktree add --detach ../datapan-registry "${source_sha}"', producer)
        materialize = next(step for step in steps if step.get("name") == "Materialize canonical registry and install validators")["run"]
        self.assertIn("python3 ../bootstrap/scripts/materialize-canonical-registry.py", materialize)
        self.assertIn("python3 ../bootstrap/scripts/validate-source-refresh-policy.py", materialize)
        self.assertIn("--schema ../bootstrap/schemas/datapan.source-refresh-policy.v1.schema.json", materialize)

        processor_path = "../bootstrap/scripts/process-upstream-catalogue-candidate.py"
        composer_path = "../bootstrap/scripts/compose-upstream-catalogue-candidate.py"
        checkpoint_path = "../bootstrap/schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
        for step_id in ("claim", "run_processor"):
            run = by_id[step_id]["run"]
            self.assertIn(f"python3 {processor_path}", run)
            self.assertIn(f"--composer {composer_path}", run)
            self.assertIn(f"--checkpoint-schema {checkpoint_path}", run)
            self.assertNotIn("python3 scripts/process-upstream-catalogue-candidate.py", run)
        bind = by_id["bind_push"]["run"]
        self.assertIn(f"python3 {processor_path}", bind)
        self.assertIn(f"--checkpoint-schema {checkpoint_path}", bind)

        self.assertEqual(job["outputs"]["producer_head_sha"], "${{ steps.producer.outputs.head_sha }}")
        self.assertEqual(job["outputs"]["processor_head_sha"], "${{ steps.processor_revision.outputs.head_sha }}")
        revision_step = by_id["processor_revision"]
        self.assertIn("git rev-parse HEAD", revision_step["run"])
        summary = by_id["publish_result"]
        self.assertEqual(summary["env"]["PRODUCER_HEAD_SHA"], "${{ steps.producer.outputs.head_sha }}")
        self.assertEqual(summary["env"]["PROCESSOR_HEAD_SHA"], "${{ steps.processor_revision.outputs.head_sha }}")
        for identity in ("baseline_sha256", "policy_sha256", "adapter_revision", "generator_revision", "extractor_revision"):
            self.assertIn(identity, summary["run"])

    def test_ready_is_bundle_verified_and_artifact_locator_is_bound_before_success(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        by_id = {step.get("id"): step for step in steps if step.get("id")}
        bundle = by_id["bundle_check"]["run"]
        bind = by_id["bind_push"]
        self.assertIn('composition_status in {"ready", "ready_scoped"}', bundle)
        self.assertIn('entry_paths == ready_bundle_paths', bundle)
        self.assertIn('"upstream-catalogue-processing-result.json"', bundle)
        self.assertIn("steps.bundle_check.outputs.verified == 'true'", bind["if"])
        self.assertIn("--bind-output-artifact-id", bind["run"])
        self.assertIn("--bind-output-artifact-expires-at", bind["run"])
        self.assertIn("--bind-generation-id", bind["run"])
        self.assertIn("steps.bundle_check.outputs.generation_id != ''", bind["if"])
        self.assertIn("steps.bundle_check.outputs.replay != 'true'", bind["if"])
        final_gate = next(step for step in steps if step.get("name", "").startswith("Fail non-ready"))
        self.assertIn("state_publication_status", final_gate["if"])

    def test_failed_processor_recovery_is_exact_authenticated_result_only_cas(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        by_id = {step.get("id"): step for step in steps if step.get("id")}
        select = by_id["select"]["run"]
        self.assertIn("recover_failed_processor_run_id are mutually exclusive", select)
        self.assertIn("decision=recover", select)
        self.assertIn("expected_checkpoint_sha256", select)
        artifact = by_id["failed_artifact"]
        self.assertEqual(artifact["if"], "steps.select.outputs.decision == 'recover'")
        self.assertIn('run.get("conclusion")', artifact["run"])
        self.assertIn("sha256:", artifact["run"])
        self.assertIn("actions/artifacts/${artifact_id}/zip", artifact["run"])
        self.assertIn("downloaded failed processor artifact digest mismatch", artifact["run"])
        recovery = by_id["run_recovery"]
        self.assertEqual(recovery["if"], "steps.select.outputs.decision == 'recover'")
        self.assertIn("--recover-failed-processor-run-id", recovery["run"])
        self.assertIn("--expected-checkpoint-sha256", recovery["run"])
        self.assertIn("--expected-state-head-sha", recovery["run"])
        self.assertNotIn("--candidate", recovery["run"])
        bind = by_id["bind_recovery"]
        self.assertIn("steps.bundle_check.outputs.verified == 'true'", bind["if"])
        self.assertIn("--expected-old-sha", bind["run"])
        self.assertIn('${{ steps.state.outputs.old_sha }}', bind["env"]["EXPECTED_STATE_HEAD_SHA"])
        self.assertIn('--expected-old-sha "${EXPECTED_STATE_HEAD_SHA}"', bind["run"])
        self.assertIn("steps.bind_recovery.outputs.published", by_id["publish_result"]["run"])

    def test_exact_terminal_replay_is_a_verified_no_candidate_noop(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        bundle_step = next(step for step in steps if step.get("id") == "bundle_check")
        ensure_step = next(step for step in steps if step.get("name", "").startswith("Ensure the processor artifact"))
        bind = next(step for step in steps if step.get("id") == "bind_push")
        publish = next(step for step in steps if step.get("id") == "publish_result")
        self.assertIn("processing_replay:", self.text)
        self.assertIn("candidate_available:", self.text)
        replay_receipt_lines = [
            line for line in publish["run"].splitlines()
            if line.lstrip().startswith("elif ") and "steps.bundle_check.outputs.replay" in line
        ]
        self.assertEqual(len(replay_receipt_lines), 2)
        self.assertTrue(all('${{ steps.claim_push.outcome }}" = "success"' in line for line in replay_receipt_lines))
        with tempfile.TemporaryDirectory(prefix="catalogue-terminal-replay-") as temp:
            repo = pathlib.Path(temp)
            root = repo / "datapan-registry/.datapan/ci/upstream-catalogue-processing"
            root.mkdir(parents=True)
            result_path = root / "upstream-catalogue-processing-result.json"
            result = {
                "status": "idle",
                "reason": "exact_producer_delivery_replay",
                "processing_replay": True,
                "candidate_available": False,
                "source_id": "data_go_kr",
                "producer_run_id": "12345",
                "processor_run_id": "777-2",
                "processor_artifact_run_id": "777",
            }
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            output = repo / "bundle-output"
            env = {
                **os.environ,
                "DECISION": "process",
                "PROCESSOR_RUN_ID": "777-2",
                "PROCESSOR_ARTIFACT_RUN_ID": "777",
                "PRODUCER_RUN_ID": "12345",
                "REPOSITORY": REPOSITORY,
                "GITHUB_OUTPUT": str(output),
            }
            bundle = subprocess.run(["bash", "-c", bundle_step["run"]], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(bundle.returncode, 0, bundle.stderr or bundle.stdout)
            outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
            self.assertEqual(outputs["verified"], "true")
            self.assertEqual(outputs["replay"], "true")
            self.assertEqual(outputs["candidate_available"], "false")
            self.assertEqual(outputs["status"], "idle")
            self.assertEqual(outputs["generation_id"], "")

            ensure = subprocess.run(
                ["bash", "-c", ensure_step["run"]], cwd=repo,
                env={**os.environ, "BUNDLE_CHECK_OUTCOME": "success", "BUNDLE_CHECK_VERIFIED": "true"},
                text=True, capture_output=True,
            )
            self.assertEqual(ensure.returncode, 0, ensure.stderr or ensure.stdout)
            self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), result)
            self.assertIn("steps.bundle_check.outputs.replay != 'true'", bind["if"])

    def test_missing_continuation_artifact_targets_its_existing_generation(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        claim = next(step for step in steps if step.get("id") == "claim")
        script = claim["run"]
        self.assertIn('if [ -n "${TARGET_GENERATION_ID}" ] && [ -n "${INPUT_ERROR}" ]; then', script)
        self.assertIn("--target-generation-id", script)
        self.assertIn("--input-error input_expired", script)
        self.assertIn("--input-error artifact_missing", script)
        self.assertEqual(claim["env"]["INPUT_EXPIRED"], "${{ steps.input_artifact.outputs.expired }}")

    def test_trusted_redelivery_selects_its_exact_existing_generation_and_artifact(self) -> None:
        select_step = next(step for step in self.workflow["jobs"]["process"]["steps"] if step.get("id") == "select")
        with tempfile.TemporaryDirectory(prefix="catalogue-redelivery-select-") as temp:
            root = pathlib.Path(temp)
            (root / "scripts").mkdir()
            (root / "scripts/upstream_catalogue_derivation.py").write_bytes(
                (ROOT / "scripts/upstream_catalogue_derivation.py").read_bytes(),
            )
            state = root / "state"
            generation_id = "a" * 64
            expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            checkpoint = {
                "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
                "source_id": "data_go_kr",
                "generation_id": generation_id,
                "status": "ready",
                "last_observation": {"producer_run_id": "12345"},
                "input_artifacts": [{
                    "run_id": "12345", "name": "upstream-catalog-refresh-12345",
                    "artifact_id": "987", "expires_at": expires_at,
                    "candidate_sha256": "b" * 64,
                }],
            }
            canonical = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            checkpoint["checkpoint_sha256"] = hashlib.sha256(canonical).hexdigest()
            source_dir = state / "sources/data_go_kr"
            (source_dir / "generations").mkdir(parents=True)
            (source_dir / "generations" / f"{generation_id}.json").write_text(
                json.dumps(checkpoint, sort_keys=True) + "\n", encoding="utf-8",
            )
            (source_dir / "index.json").write_text(json.dumps({
                "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
                "generations": [{"generation_id": generation_id}],
            }), encoding="utf-8")
            output = root / "github-output"
            result = subprocess.run(
                ["bash", "-c", select_step["run"]], cwd=root,
                env={
                    **os.environ,
                    "EVENT_NAME": "workflow_run",
                    "EVENT_PRODUCER_RUN_ID": "12345",
                    "EVENT_PRODUCER_HEAD_SHA": "c" * 40,
                    "EVENT_PRODUCER_RUN_ATTEMPT": "1",
                    "INPUT_PRODUCER_RUN_ID": "",
                    "REPOSITORY": REPOSITORY,
                    "STATE_DIR": str(state),
                    "GITHUB_OUTPUT": str(output),
                },
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
            self.assertEqual(outputs["generation_id"], generation_id)
            self.assertEqual(outputs["expected_input_artifact_id"], "")
            self.assertEqual(outputs["expected_producer_attempt"], "1")
            self.assertEqual(outputs["expected_input_name"], "upstream-catalog-refresh-12345")

    def test_scheduled_handoff_precedes_an_older_active_generation(self) -> None:
        select_step = next(step for step in self.workflow["jobs"]["process"]["steps"] if step.get("id") == "select")
        with tempfile.TemporaryDirectory(prefix="catalogue-scheduled-handoff-select-") as temp:
            root = pathlib.Path(temp)
            state = root / "state"
            generation_id = "a" * 64
            old_checkpoint = {
                "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
                "source_id": "data_go_kr", "generation_id": generation_id,
                "status": "ready", "outcome": {"detail_retry_count": 1},
                "last_progress_at": "2026-10-01T00:00:00Z",
                "generation_inputs": {"candidate_sha256": "b" * 64},
                "input_artifacts": [{
                    "run_id": "100", "name": "upstream-catalog-refresh-100",
                    "artifact_id": "1100", "expires_at": "2026-11-01T00:00:00Z",
                    "candidate_sha256": "b" * 64,
                }],
            }
            canonical = json.dumps(old_checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            old_checkpoint["checkpoint_sha256"] = hashlib.sha256(canonical).hexdigest()
            source_dir = state / "sources/data_go_kr"
            (source_dir / "generations").mkdir(parents=True)
            (source_dir / "generations" / f"{generation_id}.json").write_text(
                json.dumps(old_checkpoint, sort_keys=True) + "\n", encoding="utf-8",
            )
            (source_dir / "index.json").write_text(json.dumps({
                "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
                "generations": [{"generation_id": generation_id}],
            }), encoding="utf-8")
            output = root / "github-output"
            result = subprocess.run(
                ["bash", "-c", select_step["run"]], cwd=root,
                env={
                    **os.environ,
                    "EVENT_NAME": "schedule", "EVENT_PRODUCER_RUN_ID": "", "EVENT_PRODUCER_HEAD_SHA": "",
                    "INPUT_PRODUCER_RUN_ID": "", "INPUT_RECOVER_FAILED_PROCESSOR_RUN_ID": "",
                    "HANDOFF_AVAILABLE": "true", "HANDOFF_PRODUCER_RUN_ID": "200",
                    "HANDOFF_PRODUCER_HEAD_SHA": "c" * 40, "HANDOFF_RUN_ATTEMPT": "1",
                    "HANDOFF_RUN_STARTED_AT": "2026-10-04T00:00:00Z",
                    "HANDOFF_ARTIFACT_ID": "1200", "HANDOFF_ARTIFACT_NAME": "upstream-catalog-refresh-200",
                    "HANDOFF_ARTIFACT_EXPIRES_AT": "2026-11-02T00:00:00Z",
                    "HANDOFF_ARTIFACT_DIGEST": "d" * 64, "HANDOFF_ARTIFACT_SIZE": "1234",
                    "REPOSITORY": REPOSITORY, "STATE_DIR": str(state), "GITHUB_OUTPUT": str(output),
                },
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
            self.assertEqual(outputs["decision"], "process")
            self.assertEqual(outputs["producer_run_id"], "200")
            self.assertEqual(outputs["expected_producer_attempt"], "1")
            self.assertEqual(outputs["expected_input_artifact_id"], "1200")
            self.assertEqual(outputs["generation_id"], "")

    def test_valid_marker_result_digest_survives_bundle_and_actionable_receipt_steps(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        bundle_step = next(step for step in steps if step.get("id") == "bundle_check")
        ensure_step = next(step for step in steps if step.get("name", "").startswith("Ensure the processor artifact"))

        with tempfile.TemporaryDirectory(prefix="catalogue-marker-result-") as temp:
            repo = pathlib.Path(temp)
            root = repo / "datapan-registry/.datapan/ci/upstream-catalogue-processing"
            root.mkdir(parents=True)
            generation_id = "f" * 64
            result = {
                "status": "quarantined", "generation_id": generation_id,
                "reason": "input_artifact_missing", "attempts_consumed": 0,
                "observed_at": "2026-10-01T00:00:00Z",
                "last_heartbeat_at": "2026-10-01T00:01:00Z",
                "last_progress_at": "2026-10-01T00:01:00Z",
                "processing_replay": False, "candidate_available": False,
                "source_id": "data_go_kr", "producer_run_id": "12345",
                "processor_run_id": "777-1", "processor_artifact_run_id": "777",
            }
            result_path = root / "upstream-catalogue-processing-result.json"
            result_bytes = (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            result_path.write_bytes(result_bytes)
            entries = [{
                "path": result_path.name,
                "sha256": hashlib.sha256(result_bytes).hexdigest(),
                "bytes": len(result_bytes),
            }]
            canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            checkpoint = {
                "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
                "generation_id": generation_id,
                "status": "quarantined",
                "output_digests": entries,
                "output_artifact": {
                    "repository": REPOSITORY, "run_id": "777",
                    "name": "upstream-catalogue-processing-777-1",
                    "artifact_id": None, "expires_at": "2026-10-31T00:00:00Z",
                    "bundle_manifest_sha256": hashlib.sha256(canonical(entries)).hexdigest(),
                },
            }
            checkpoint["checkpoint_sha256"] = hashlib.sha256(canonical(checkpoint)).hexdigest()
            (root / "upstream-catalogue-checkpoint-receipt.json").write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            )

            output = repo / "bundle-output"
            env = {
                **os.environ,
                "DECISION": "process",
                "PROCESSOR_RUN_ID": "777-1",
                "PROCESSOR_ARTIFACT_RUN_ID": "777",
                "PRODUCER_RUN_ID": "12345",
                "REPOSITORY": REPOSITORY,
                "GITHUB_OUTPUT": str(output),
            }
            result_run = subprocess.run(["bash", "-c", bundle_step["run"]], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(result_run.returncode, 0, result_run.stderr or result_run.stdout)
            self.assertIn("verified=true", output.read_text(encoding="utf-8"))
            self.assertIn("candidate_available=false", output.read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(result_path.read_bytes()).hexdigest(), entries[0]["sha256"])

            ensure_env = {
                **os.environ,
                "BUNDLE_CHECK_OUTCOME": "success",
                "BUNDLE_CHECK_VERIFIED": "true",
            }
            ensure_run = subprocess.run(["bash", "-c", ensure_step["run"]], cwd=repo, env=ensure_env, text=True, capture_output=True)
            self.assertEqual(ensure_run.returncode, 0, ensure_run.stderr or ensure_run.stdout)
            self.assertEqual(hashlib.sha256(result_path.read_bytes()).hexdigest(), entries[0]["sha256"])

            final_gate = next(step for step in steps if step.get("name", "").startswith("Fail non-ready"))
            self.assertIn("outputs.status == 'quarantined'", final_gate["if"])
            self.assertIn("outputs.candidate_available == 'false'", final_gate["if"])
            self.assertIn("steps.bundle_check.outputs.verified == 'true'", final_gate["if"])
            self.assertIn("outputs.state_publication_status == 'published'", final_gate["if"])

    def test_ready_bundle_verifies_exact_eight_file_order_including_result(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        bundle_step = next(step for step in steps if step.get("id") == "bundle_check")
        ensure_step = next(step for step in steps if step.get("name", "").startswith("Ensure the processor artifact"))
        bundle_order = [
            "composed-candidate.registry.json", "ready-scope.registry.json", "semantic-diff.json",
            "regeneration-queue.json", "quarantine.json", "composition-receipt.json",
            "upstream-catalogue-enrichment-evidence.json", "upstream-catalogue-processing-result.json",
        ]

        with tempfile.TemporaryDirectory(prefix="catalogue-ready-eight-") as temp:
            repo = pathlib.Path(temp)
            root = repo / "datapan-registry/.datapan/ci/upstream-catalogue-processing"
            root.mkdir(parents=True)
            generation_id = "e" * 64
            payloads = {
                "composed-candidate.registry.json": {"schema_version": "datapan.registry.v1", "apis": []},
                "ready-scope.registry.json": {"schema_version": "datapan.registry.v1", "apis": []},
                "semantic-diff.json": {"changes": []},
                "regeneration-queue.json": {"items": []},
                "quarantine.json": {"items": []},
                "composition-receipt.json": {"status": "ready_scoped"},
                "upstream-catalogue-enrichment-evidence.json": {"records": []},
                "upstream-catalogue-processing-result.json": {
                    "status": "ready", "generation_id": generation_id,
                    "reason": "scoped_candidate_ready_pending_outcomes_retained",
                    "processing_replay": False, "candidate_available": True,
                    "source_id": "data_go_kr", "producer_run_id": "12345",
                    "processor_run_id": "777-1", "processor_artifact_run_id": "777",
                },
            }
            entries = []
            for name in bundle_order:
                path = root / name
                content = (json.dumps(payloads[name], ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
                path.write_bytes(content)
                entries.append({"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})

            canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            checkpoint = {
                "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
                "generation_id": generation_id,
                "status": "ready",
                "output_digests": entries,
                "output_artifact": {
                    "repository": REPOSITORY, "run_id": "777",
                    "name": "upstream-catalogue-processing-777-1",
                    "artifact_id": None, "expires_at": "2026-10-31T00:00:00Z",
                    "bundle_manifest_sha256": hashlib.sha256(canonical(entries)).hexdigest(),
                },
            }
            checkpoint["checkpoint_sha256"] = hashlib.sha256(canonical(checkpoint)).hexdigest()
            (root / "upstream-catalogue-checkpoint-receipt.json").write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            )

            output = repo / "bundle-output"
            env = {
                **os.environ,
                "DECISION": "process",
                "PROCESSOR_RUN_ID": "777-1",
                "PROCESSOR_ARTIFACT_RUN_ID": "777",
                "PRODUCER_RUN_ID": "12345",
                "REPOSITORY": REPOSITORY,
                "GITHUB_OUTPUT": str(output),
            }
            checked = subprocess.run(["bash", "-c", bundle_step["run"]], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(checked.returncode, 0, checked.stderr or checked.stdout)
            outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
            self.assertEqual(outputs["verified"], "true")
            self.assertEqual(outputs["status"], "ready")
            self.assertEqual(outputs["candidate_available"], "true")
            self.assertEqual([entry["path"] for entry in entries], bundle_order)
            result_hash = entries[-1]["sha256"]
            self.assertEqual(hashlib.sha256((root / bundle_order[-1]).read_bytes()).hexdigest(), result_hash)

            ensured = subprocess.run(
                ["bash", "-c", ensure_step["run"]], cwd=repo,
                env={**os.environ, "BUNDLE_CHECK_OUTCOME": "success", "BUNDLE_CHECK_VERIFIED": "true"},
                text=True, capture_output=True,
            )
            self.assertEqual(ensured.returncode, 0, ensured.stderr or ensured.stdout)
            self.assertEqual(hashlib.sha256((root / bundle_order[-1]).read_bytes()).hexdigest(), result_hash)

    def test_only_the_trusted_collector_revision_is_checked_out_and_no_publication_is_wired(self) -> None:
        producer = next(step for step in self.workflow["jobs"]["process"]["steps"] if step.get("id") == "producer")
        script = producer["run"]
        self.assertIn('"path": ".github/workflows/upstream-catalog-refresh.yml"', script)
        self.assertIn('value.get("path") != expected["path"]', script)
        self.assertIn('value.get("event") not in {"schedule", "workflow_dispatch"}', script)
        self.assertIn('git worktree add --detach ../datapan-registry "${source_sha}"', script)
        self.assertFalse(any("pull_request" in trigger for trigger in self.workflow["on"]))
        self.assertFalse(any("canonical-update" in step.get("name", "") for step in self.workflow["jobs"]["process"]["steps"]))
        self.assertNotIn("pull-requests: write", self.text)

    def test_artifact_step_downloads_and_revalidates_the_exact_admitted_artifact(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        artifact_step = next(step for step in steps if step.get("id") == "input_artifact")
        script = artifact_step["run"]
        self.assertNotIn("gh run download", script)
        self.assertIn('actions/artifacts/${ARTIFACT_ID}/zip', script)
        self.assertIn('actions/artifacts/${ARTIFACT_ID}', script)
        self.assertIn('actions/runs/${PRODUCER_RUN_ID}', script)
        self.assertIn('archive_sha != env["ARTIFACT_DIGEST"]', script)
        self.assertIn('size <= 268435456', script)

        with tempfile.TemporaryDirectory(prefix="catalogue-artifact-step-") as temp:
            root = pathlib.Path(temp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/bin/sh\n"
                "[ \"$1\" = api ] || exit 9\n"
                "endpoint=\n"
                "for arg in \"$@\"; do endpoint=$arg; done\n"
                "printf '%s\\n' \"$endpoint\" >> \"$GH_CALL_LOG\"\n"
                "case \"$endpoint\" in\n"
                "  */actions/artifacts/987/zip) cat \"$GH_FIXTURE_ZIP\" ;;\n"
                "  */actions/artifacts/987) cat \"$GH_FIXTURE_METADATA\" ;;\n"
                "  */actions/runs/12345) cat \"$GH_FIXTURE_RUN\" ;;\n"
                "  *) exit 9 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            bundle_path = root / "collector.zip"
            candidate_bytes = b"[]\n"
            diff_bytes = b"{}\n"
            evidence = {
                "source_id": "data_go_kr",
                "observed_at": "2026-10-04T00:01:00Z",
                "collection": {"succeeded": True},
                "snapshot": {"sha256": hashlib.sha256(candidate_bytes).hexdigest()},
                "diff": {"sha256": hashlib.sha256(diff_bytes).hexdigest()},
            }
            with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("candidate.registry.json", candidate_bytes)
                archive.writestr("catalog-diff.json", diff_bytes)
                archive.writestr("upstream-refresh-evidence.json", json.dumps(evidence))
                archive.writestr("upstream-refresh-work-packet.json", "{}\n")
            archive_bytes = bundle_path.read_bytes()
            digest = hashlib.sha256(archive_bytes).hexdigest()
            expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            created = "2026-10-04T00:01:30Z"
            metadata_path = root / "metadata.json"
            metadata_path.write_text(json.dumps({
                "id": 987, "name": "upstream-catalog-refresh-12345", "expires_at": expires,
                "created_at": created, "expired": False, "digest": f"sha256:{digest}",
                "size_in_bytes": len(archive_bytes),
                "workflow_run": {
                    "id": 12345, "head_sha": "a" * 40,
                    "repository_id": 111, "head_repository_id": 111,
                },
            }), encoding="utf-8")
            run_path = root / "run.json"
            run_path.write_text(json.dumps({
                "id": 12345, "name": "Upstream catalog refresh",
                "path": ".github/workflows/upstream-catalog-refresh.yml",
                "repository": {"id": 111, "full_name": REPOSITORY},
                "head_repository": {"id": 111, "full_name": REPOSITORY},
                "head_branch": "main", "head_sha": "a" * 40,
                "event": "schedule", "html_url": f"https://github.com/{REPOSITORY}/actions/runs/12345",
                "run_attempt": 1, "status": "completed", "conclusion": "success",
                "run_started_at": "2026-10-04T00:00:00Z", "updated_at": "2026-10-04T00:02:00Z",
            }), encoding="utf-8")
            env = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "GH_FIXTURE_METADATA": str(metadata_path),
                "GH_FIXTURE_RUN": str(run_path),
                "GH_FIXTURE_ZIP": str(bundle_path),
                "GH_CALL_LOG": str(root / "gh-calls.log"),
                "GITHUB_OUTPUT": str(root / "github-output"),
                "REPOSITORY": REPOSITORY,
                "DEFAULT_BRANCH": "main",
                "PRODUCER_RUN_ID": "12345", "PRODUCER_HEAD_SHA": "a" * 40,
                "PRODUCER_ATTEMPT": "1", "PRODUCER_RUN_STARTED_AT": "2026-10-04T00:00:00Z",
                "PRODUCER_RUN_COMPLETED_AT": "2026-10-04T00:01:45Z",
                "PRODUCER_RUN_UPDATED_AT": "2026-10-04T00:02:00Z",
                "OBSERVE_JOB_STARTED_AT": "2026-10-04T00:00:30Z",
                "OBSERVE_JOB_COMPLETED_AT": "2026-10-04T00:01:45Z",
                "ARTIFACT_ID": "987", "ARTIFACT_NAME": "upstream-catalog-refresh-12345",
                "ARTIFACT_EXPIRES_AT": expires, "ARTIFACT_CREATED_AT": created,
                "ARTIFACT_DIGEST": digest, "ARTIFACT_SIZE": str(len(archive_bytes)),
                "REPOSITORY_ID": "111", "HEAD_REPOSITORY_ID": "111",
                "PRODUCER_EVENT": "schedule",
                "PRODUCER_URL": f"https://github.com/{REPOSITORY}/actions/runs/12345",
            }
            repo = root / "datapan-registry"
            repo.mkdir()
            result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            outputs = dict(line.split("=", 1) for line in (root / "github-output").read_text().splitlines())
            self.assertEqual(outputs["found"], "true")
            self.assertEqual(outputs["artifact_id"], "987")
            archive_path = repo / ".datapan/ci/upstream-refresh/collector-artifact.zip"
            self.assertEqual(hashlib.sha256(archive_path.read_bytes()).hexdigest(), digest)
            admission = json.loads((repo / ".datapan/ci/upstream-refresh/collector-admission.json").read_text())
            self.assertEqual(admission["run_attempt"], 1)
            self.assertEqual(admission["archive_sha256"], digest)
            self.assertTrue((repo / ".datapan/ci/upstream-refresh/candidate.registry.json").is_file())
            calls = (root / "gh-calls.log").read_text().splitlines()
            self.assertEqual(calls, [
                f"repos/{REPOSITORY}/actions/artifacts/987",
                f"repos/{REPOSITORY}/actions/artifacts/987/zip",
                f"repos/{REPOSITORY}/actions/runs/12345",
            ])

            invalid_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            invalid_metadata["size_in_bytes"] = 268435457
            metadata_path.write_text(json.dumps(invalid_metadata), encoding="utf-8")
            (root / "gh-calls.log").unlink()
            rejected = subprocess.run(["bash", "-c", script], cwd=repo, env=env, text=True, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("metadata changed before download", rejected.stderr)
            self.assertEqual((root / "gh-calls.log").read_text().splitlines(), [
                f"repos/{REPOSITORY}/actions/artifacts/987",
            ])


if __name__ == "__main__":
    unittest.main()
