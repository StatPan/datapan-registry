from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import tempfile
import unittest
import datetime as dt
import hashlib

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
        self.assertIn("steps.claim_push.outputs.claim_sha", steps[worker_position]["if"])
        self.assertIn("--require-durable-reservation", worker)
        self.assertIn("python3 scripts/process-upstream-catalogue-candidate.py", worker)

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
            self.assertEqual(outputs["expected_input_artifact_id"], "987")
            self.assertEqual(outputs["expected_input_expires_at"], expires_at)
            self.assertEqual(outputs["expected_input_name"], "upstream-catalog-refresh-12345")

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

    def test_artifact_step_uses_local_metadata_for_found_missing_and_expired_cases(self) -> None:
        steps = self.workflow["jobs"]["process"]["steps"]
        artifact_step = next(step for step in steps if step.get("id") == "input_artifact")
        script = artifact_step["run"]
        self.assertNotIn("${{ steps.input_artifact.outputs.found }}", script)

        with tempfile.TemporaryDirectory(prefix="catalogue-artifact-step-") as temp:
            root = pathlib.Path(temp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = api ]; then cat \"$GH_FIXTURE_ARTIFACTS\"; exit 0; fi\n"
                "if [ \"$1\" = run ] && [ \"$2\" = download ]; then\n"
                "  shift 2; target=\n"
                "  while [ $# -gt 0 ]; do if [ \"$1\" = --dir ]; then shift; target=$1; fi; shift; done\n"
                "  mkdir -p \"$target\"; cp -R \"$GH_FIXTURE_DOWNLOAD\"/. \"$target\"/; exit 0\n"
                "fi\n"
                "exit 9\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            download = root / "download"
            download.mkdir()
            candidate_bytes = b"[]\n"
            diff_bytes = b"{}\n"
            (download / "candidate.registry.json").write_bytes(candidate_bytes)
            (download / "catalog-diff.json").write_bytes(diff_bytes)
            evidence = {
                "collection": {"succeeded": True},
                "snapshot": {"sha256": hashlib.sha256(candidate_bytes).hexdigest()},
                "diff": {"sha256": hashlib.sha256(diff_bytes).hexdigest()},
            }
            (download / "upstream-refresh-evidence.json").write_text(json.dumps(evidence), encoding="utf-8")

            def execute(artifacts: list[dict[str, object]], *, expected_id: str = "", expected_expiry: str = "") -> tuple[dict[str, str], pathlib.Path]:
                case = root / f"case-{len(list(root.glob('case-*')))}"
                case.mkdir()
                repo = case / "datapan-registry"
                repo.mkdir()
                fixture_json = case / "artifacts.json"
                fixture_json.write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")
                output = case / "github-output"
                env = {
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "GH_FIXTURE_ARTIFACTS": str(fixture_json),
                    "GH_FIXTURE_DOWNLOAD": str(download),
                    "GITHUB_OUTPUT": str(output),
                    "REPOSITORY": REPOSITORY,
                    "PRODUCER_RUN_ID": "12345",
                    "ARTIFACT_NAME": "upstream-catalog-refresh-12345",
                    "EXPECTED_ARTIFACT_ID": expected_id,
                    "EXPECTED_EXPIRES_AT": expected_expiry,
                }
                result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
                choice_path = repo / ".datapan/ci/producer-artifact-metadata/choice.json"
                return outputs, choice_path

            expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            found_outputs, found_choice = execute([{
                "id": 987, "name": "upstream-catalog-refresh-12345", "expires_at": expiry,
                "expired": False, "workflow_run": {"id": 12345},
            }], expected_id="987", expected_expiry=expiry)
            self.assertEqual(found_outputs["found"], "true")
            self.assertTrue((found_choice.parent.parent / "upstream-refresh/candidate.registry.json").is_file())

            missing_outputs, missing_choice = execute([])
            missing = json.loads(missing_choice.read_text(encoding="utf-8"))
            self.assertEqual(missing_outputs["found"], "false")
            self.assertEqual(missing["input_error"], "artifact_missing")
            self.assertFalse(missing["expired"])

            expired_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            expired_outputs, expired_choice = execute([{
                "id": 987, "name": "upstream-catalog-refresh-12345", "expires_at": expired_at,
                "expired": True, "workflow_run": {"id": 12345},
            }], expected_id="987", expected_expiry=expired_at)
            expired = json.loads(expired_choice.read_text(encoding="utf-8"))
            self.assertEqual(expired_outputs["found"], "false")
            self.assertEqual(expired["input_error"], "artifact_missing")
            self.assertTrue(expired["expired"])


if __name__ == "__main__":
    unittest.main()
