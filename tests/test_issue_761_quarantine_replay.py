from __future__ import annotations

import copy
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import textwrap
import unittest


ROOT = pathlib.Path(__file__).parents[1]
TESTS = pathlib.Path(__file__).parent
sys.path.insert(0, str(TESTS))
import test_process_upstream_catalogue_candidate as fixtures  # noqa: E402


PROCESSOR = ROOT / "scripts/process-upstream-catalogue-candidate.py"
WORKFLOW = ROOT / ".github/workflows/upstream-catalogue-process.yml"
STATE_BRANCH = "automation/upstream-catalogue-state"


def workflow_python(step_name: str) -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index(f"      - name: {step_name}")
    end = text.find("\n      - name:", start + 1)
    step = text[start:] if end < 0 else text[start:end]
    match = re.search(
        r'^          python3 - <<\'PY\' >> "\$\{GITHUB_OUTPUT\}"\n(.*?)\n          PY$',
        step,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"workflow Python body not found for {step_name}")
    return textwrap.dedent(match.group(1))


def git(repo: pathlib.Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


class Issue761QuarantineReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.UpstreamCatalogueProcessorTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def init_state_repo(self, state_repo: pathlib.Path, bare_repo: pathlib.Path) -> None:
        subprocess.run(["git", "init", "-b", STATE_BRANCH, str(state_repo)], check=True, capture_output=True)
        git(state_repo, "config", "user.name", "issue-761-fixture")
        git(state_repo, "config", "user.email", "issue-761-fixture@example.test")
        subprocess.run(["git", "init", "--bare", str(bare_repo)], check=True, capture_output=True)
        git(state_repo, "remote", "add", "origin", str(bare_repo))

    def commit_state(self, state_repo: pathlib.Path, message: str) -> str:
        git(state_repo, "add", ".datapan/upstream-catalogue-state")
        subprocess.run(["git", "-C", str(state_repo), "commit", "-m", message], check=True, capture_output=True)
        return git(state_repo, "rev-parse", "HEAD")

    def push_state(self, state_repo: pathlib.Path, *, expected: str | None = None) -> None:
        args = ["push"]
        if expected is not None:
            args.append(f"--force-with-lease=refs/heads/{STATE_BRANCH}:{expected}")
        args.extend(["origin", f"HEAD:refs/heads/{STATE_BRANCH}"])
        git(state_repo, *args)

    def workflow_root(self, label: str) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        root = self.fixture.root / f"{label}-workflow"
        root.mkdir()
        (root / "bootstrap").symlink_to(ROOT, target_is_directory=True)
        checkout = root / "datapan-registry"
        checkout.mkdir()
        state_repo = root / "state-repo"
        return root, checkout, state_repo

    def run_processor(self, args: object, *, expected_exit: int) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [sys.executable, "-B", str(PROCESSOR), *self.fixture.namespace_argv(args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(completed.returncode, expected_exit, completed.stderr or completed.stdout)
        return completed

    def run_workflow_body(
        self, code: str, *, cwd: pathlib.Path, output_file: pathlib.Path, env: dict[str, str],
    ) -> dict[str, str]:
        completed = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=cwd,
            text=True,
            capture_output=True,
            env={**os.environ, **env, "GITHUB_OUTPUT": str(output_file), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        output_file.write_text(completed.stdout, encoding="utf-8")
        return dict(
            line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line
        )

    def test_live_quarantine_keeps_exit_and_status_and_is_not_replayed(self) -> None:
        f = self.fixture
        f.write_real_composer_inputs([], [f.real_link_row()], f.now)
        flow_root, checkout, state_repo = self.workflow_root("quarantine")
        bare_repo = flow_root / "state.git"
        self.init_state_repo(state_repo, bare_repo)
        state_dir = state_repo / ".datapan/upstream-catalogue-state"
        worker_output = f.root / "quarantine-worker-output"

        claim_args = f.authenticated_live_args(
            processor_run_id="777-1", processor_artifact_run_id="777",
            state_dir=state_dir, output_dir=worker_output,
        )
        claim_args.claim_only = True
        claim_code, claim = fixtures.MODULE.process(
            claim_args, fetcher=lambda *_args: self.fail("claim must not call provider"),
        )
        self.assertEqual(claim_code, 0, claim)
        self.assertEqual(claim["status"], "enriching")
        claim_sha = self.commit_state(state_repo, "seed authenticated request reservation")
        self.push_state(state_repo)

        invalid_resume = f.root / "invalid-resume-evidence.json"
        invalid_resume.write_text("{", encoding="utf-8")
        worker_args = copy.deepcopy(claim_args)
        worker_args.claim_only = False
        worker_args.require_durable_reservation = True
        worker_args.resume_enrichment_evidence = invalid_resume
        quarantine_output = checkout / ".datapan/ci/upstream-catalogue-processing"
        worker_args.output_dir = quarantine_output
        worker = self.run_processor(worker_args, expected_exit=3)
        worker_result = json.loads((quarantine_output / "upstream-catalogue-processing-result.json").read_text())
        quarantined = json.loads((quarantine_output / "upstream-catalogue-checkpoint-receipt.json").read_text())
        self.assertEqual(worker_result["status"], "quarantined")
        self.assertEqual(worker_result["reason"], "valueerror")
        self.assertFalse(worker_result["processing_replay"])
        self.assertEqual(quarantined["status"], "quarantined")
        self.assertEqual(quarantined["outcome"]["reason"], "valueerror")
        self.assertEqual(quarantined["generation_id"], claim["generation_id"])
        quarantined_sha = self.commit_state(state_repo, "persist actual quarantined processor result")
        self.push_state(state_repo)

        replay_output = checkout / ".datapan/ci/upstream-catalogue-processing"
        replay_args = f.authenticated_live_args(
            processor_run_id="888-2", processor_artifact_run_id="888",
            state_dir=state_dir, output_dir=replay_output, state_head_sha=quarantined_sha,
        )
        replay = self.run_processor(replay_args, expected_exit=3)
        result = json.loads((replay_output / "upstream-catalogue-processing-result.json").read_text())
        checkpoint_receipt = json.loads((replay_output / "upstream-catalogue-checkpoint-receipt.json").read_text())
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["reason"], "valueerror")
        self.assertEqual(result["generation_id"], claim["generation_id"])
        self.assertFalse(result["processing_replay"])
        self.assertEqual(checkpoint_receipt["status"], "quarantined")
        self.assertFalse((replay_output / fixtures.MODULE.REPLAY_PERSISTENCE_RECEIPT).exists())
        self.assertNotIn("exact_producer_delivery_replay", replay.stdout)

        claim_output = self.run_workflow_body(
            workflow_python("Claim bounded request budget"),
            cwd=checkout,
            output_file=flow_root / "claim.outputs",
            env={"EXPECTED_STATE_HEAD_SHA": quarantined_sha, "PROCESS_EXIT_CODE": "3"},
        )
        self.assertEqual(claim_output["replay_candidate"], "false")
        self.assertEqual(claim_output["checkpoint_persisted"], "true")
        self.assertEqual(claim_output["replay_authenticated"], "false")
        self.assertEqual(git(state_repo, "rev-parse", "HEAD"), quarantined_sha)
        self.assertNotEqual(claim_sha, quarantined_sha)

    def run_positive_replay_case(self, terminal_status: str) -> None:
        f = self.fixture
        if terminal_status == "ready":
            f.write_real_composer_inputs([], [f.real_link_row()], f.now)
        elif terminal_status == "no-change":
            f.write_real_composer_inputs([], [], f.now)
        else:
            raise AssertionError(terminal_status)

        flow_root, checkout, state_repo = self.workflow_root(terminal_status)
        bare_repo = flow_root / "state.git"
        self.init_state_repo(state_repo, bare_repo)
        state_dir = state_repo / ".datapan/upstream-catalogue-state"
        worker_output = f.root / f"{terminal_status}-worker-output"
        claim_args = f.authenticated_live_args(
            processor_run_id="777-1", processor_artifact_run_id="777",
            state_dir=state_dir, output_dir=worker_output,
        )
        claim_args.claim_only = True
        claim_code, claim = fixtures.MODULE.process(
            claim_args, fetcher=lambda *_args: self.fail("claim must not call provider"),
        )
        self.assertEqual(claim_code, 0, claim)
        self.commit_state(state_repo, f"seed {terminal_status} request reservation")
        self.push_state(state_repo)

        worker_args = copy.deepcopy(claim_args)
        worker_args.claim_only = False
        worker_args.require_durable_reservation = True
        if terminal_status == "ready":
            page_bytes = (
                b'<a href="https://www.data.go.kr/guide/2.pdf">API Guide</a>'
                b'<a href="https://openapi.airport.co.kr/detail" onclick="fn_LinkApiRequest()">API</a>'
            )
            page = fixtures.MODULE.DetailPageObservation(
                body=page_bytes.decode("utf-8"), page_bytes=page_bytes,
                page_url="https://www.data.go.kr/data/2/openapi.do",
                effective_url="https://www.data.go.kr/data/2/openapi.do",
                page_sha256=fixtures.MODULE.sha256_bytes(page_bytes), observed_at=f.now,
            )
            resolver_bytes = json.dumps({
                "publicDataDetailPk": "uddi:fixture-2",
                "linkUrl": "https://openapi.airport.co.kr/catalogue/landing",
                "status": True,
            }, separators=(",", ":")).encode()

            def resolver_fetch(url: str, _timeout: float):
                return fixtures.MODULE.LinkResolverObservation(
                    body=resolver_bytes, request_url=url, effective_url=url, observed_at=f.now,
                )

            worker_code, terminal = fixtures.MODULE.process(
                worker_args,
                fetcher=lambda _url, _timeout: page,
                resolver_fetcher=resolver_fetch,
                sleeper=lambda _delay: None,
            )
        else:
            worker_code, terminal = fixtures.MODULE.process(
                worker_args,
                fetcher=lambda *_args: self.fail("no-change replay must not request provider data"),
                sleeper=lambda _delay: None,
            )
        self.assertEqual(worker_code, 0, terminal)
        self.assertEqual(terminal["status"], terminal_status)
        self.commit_state(state_repo, f"persist {terminal_status} terminal processor result")
        self.push_state(state_repo)
        selected_sha = git(state_repo, "rev-parse", "HEAD")

        replay_output = checkout / ".datapan/ci/upstream-catalogue-processing"
        replay_args = f.authenticated_live_args(
            processor_run_id="888-2", processor_artifact_run_id="888",
            state_dir=state_dir, output_dir=replay_output, state_head_sha=selected_sha,
        )
        time.sleep(1.05)  # State receipts use second-resolution timestamps.
        replay = self.run_processor(replay_args, expected_exit=0)
        result = json.loads((replay_output / "upstream-catalogue-processing-result.json").read_text())
        checkpoint_receipt = json.loads((replay_output / "upstream-catalogue-checkpoint-receipt.json").read_text())
        receipt = json.loads((replay_output / fixtures.MODULE.REPLAY_PERSISTENCE_RECEIPT).read_text())
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["reason"], "exact_producer_delivery_replay")
        self.assertTrue(result["processing_replay"])
        self.assertEqual(checkpoint_receipt["status"], terminal_status)
        self.assertEqual(receipt["terminal_status"], terminal_status)
        self.assertNotIn("FAIL", replay.stderr)

        claim_outputs = self.run_workflow_body(
            workflow_python("Claim bounded request budget"),
            cwd=checkout,
            output_file=flow_root / "claim.outputs",
            env={"EXPECTED_STATE_HEAD_SHA": selected_sha, "PROCESS_EXIT_CODE": "0"},
        )
        self.assertEqual(claim_outputs["replay_candidate"], "true")
        self.assertEqual(claim_outputs["checkpoint_persisted"], "true")
        self.assertEqual(claim_outputs["replay_authenticated"], "true")

        changed = set(git(state_repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines())
        self.assertEqual(len(changed), 2, changed)
        claim_sha = self.commit_state(state_repo, f"CAS publish {terminal_status} replay transition")
        self.assertNotEqual(claim_sha, selected_sha)
        self.push_state(state_repo, expected=selected_sha)

        bundle_outputs = self.run_workflow_body(
            workflow_python("Validate the processor bundle and write a failure receipt if needed"),
            cwd=flow_root,
            output_file=flow_root / "bundle.outputs",
            env={
                "DECISION": "process",
                "PROCESSOR_RUN_ID": "888-2",
                "PROCESSOR_ARTIFACT_RUN_ID": "888",
                "PRODUCER_RUN_ID": "37548691738",
                "REPOSITORY": "StatPan/datapan-registry",
                "STATE_HEAD_SHA": selected_sha,
                "CLAIM_SHA": claim_sha,
                "CLAIM_CHANGED": "true",
                "CLAIM_OUTCOME": "success",
            },
        )
        self.assertEqual(bundle_outputs["verified"], "true")
        self.assertEqual(bundle_outputs["replay"], "true")
        self.assertEqual(bundle_outputs["status"], "idle")
        self.assertEqual(git(state_repo, "rev-parse", "HEAD"), claim_sha)

        missing_auth_output = checkout / ".datapan/ci/missing-auth"
        missing_auth_args = f.authenticated_live_args(
            processor_run_id="889-3", processor_artifact_run_id="889",
            state_dir=state_dir, output_dir=missing_auth_output, state_head_sha=claim_sha,
        )
        missing_auth_args.collector_admission_file = None
        missing_auth_args.collector_archive = None
        missing_auth = self.run_processor(missing_auth_args, expected_exit=1)
        missing_auth_result = json.loads(
            (missing_auth_output / "upstream-catalogue-processing-result.json").read_text(),
        )
        self.assertEqual(missing_auth_result["status"], "retry")
        self.assertFalse((missing_auth_output / "upstream-catalogue-checkpoint-receipt.json").exists())
        self.assertFalse((missing_auth_output / fixtures.MODULE.REPLAY_PERSISTENCE_RECEIPT).exists())
        self.assertNotIn("FAIL", missing_auth.stdout)
        self.assertEqual(git(state_repo, "status", "--porcelain=v1", "--untracked-files=all"), "")
        self.assertEqual(git(state_repo, "rev-parse", "HEAD"), claim_sha)

        blocked_output = checkout / ".datapan/ci/blocked-replay-receipt"
        (blocked_output / fixtures.MODULE.REPLAY_PERSISTENCE_RECEIPT).mkdir(parents=True)
        blocked_args = f.authenticated_live_args(
            processor_run_id="890-4", processor_artifact_run_id="890",
            state_dir=state_dir, output_dir=blocked_output, state_head_sha=claim_sha,
        )
        blocked = self.run_processor(blocked_args, expected_exit=1)
        blocked_result = json.loads(
            (blocked_output / "upstream-catalogue-processing-result.json").read_text(),
        )
        self.assertEqual(blocked_result["status"], "retry")
        self.assertNotEqual(blocked_result.get("reason"), "exact_producer_delivery_replay")
        self.assertTrue((blocked_output / fixtures.MODULE.REPLAY_PERSISTENCE_RECEIPT).is_dir())
        self.assertIn("FAIL upstream catalogue processor", blocked.stderr)

    def test_ready_and_no_change_replay_pass_actual_workflow_pre_and_post_cas_checks(self) -> None:
        self.run_positive_replay_case("ready")
        self.run_positive_replay_case("no-change")


if __name__ == "__main__":
    unittest.main()
