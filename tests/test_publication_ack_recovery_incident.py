from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
import zipfile
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1].resolve()
FIXTURE = ROOT / "tests/fixtures/canonical-publication-ack/incident-37199628001-1"
REPOSITORY = "StatPan/datapan-registry"


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = load_module(ROOT / "scripts/run-canonical-update-promotion.py", "incident_ack_runner")
PR_HELPER = load_module(ROOT / "scripts/canonical_update_pr.py", "incident_ack_pr_helper")
RECOVERY = load_module(ROOT / "scripts/recover-canonical-publication-ack.py", "incident_ack_recovery")


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def make_receipt_archive(receipt: dict, binding: dict) -> bytes:
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(RECOVERY.RECEIPT_MEMBER, json.dumps(receipt, sort_keys=True).encode())
        archive.writestr(RECOVERY.SOURCE_BINDING_MEMBER, json.dumps(binding, sort_keys=True).encode())
    return archive_bytes.getvalue()


class IncidentGitHub:
    """Read-only REST transport backed by the frozen real incident artifacts."""

    def __init__(self, journal: dict, *, remote_main_sha: str | None = None) -> None:
        self.publisher = read_json(FIXTURE / "publisher-run.json")
        self.jobs = read_json(FIXTURE / "publisher-jobs.json")
        self.artifacts = read_json(FIXTURE / "publisher-artifacts.json")
        self.archive = (FIXTURE / "publication-receipts.zip").read_bytes()
        self.repository_id = self.publisher["repository"]["id"]
        self.controller_sha = __import__("subprocess").run(
            ("git", "rev-parse", "HEAD"), cwd=ROOT, check=True, text=True, capture_output=True,
        ).stdout.strip()
        self.remote_main_sha = remote_main_sha or self.controller_sha
        self.journal = copy.deepcopy(journal)
        self.state_sha = "a" * 40
        self.requests: list[tuple[str, str]] = []
        self.pr_reads: list[int] = []
        self.publisher_runs = [copy.deepcopy(self.publisher)]
        self.run_objects = {self.publisher["id"]: copy.deepcopy(self.publisher)}
        self.job_objects = {self.publisher["id"]: copy.deepcopy(self.jobs)}
        self.artifact_objects = {self.publisher["id"]: copy.deepcopy(self.artifacts)}
        receipt_artifact = next(
            artifact for artifact in self.artifacts["artifacts"]
            if artifact.get("name") == RECOVERY.RECEIPT_ARTIFACT_NAME
        )
        self.archives = {receipt_artifact["id"]: self.archive}
        self.cas_conflict = False
        target = next(
            row for row in journal["records"]
            if row.get("candidate", {}).get("manifest_sha256")
            == "71a5ad4716ef5847210e2aeb8513ec136619e9521e757af106fdb3d048c33e50"
        )
        self.pr_record = target
        self.workflow_ids = {
            RECOVERY.PUBLISHER_WORKFLOW_PATH: self.publisher["workflow_id"],
            RECOVERY.PROMOTION_WORKFLOW_PATH: 373708872,
            RECOVERY.ACK_WORKFLOW_PATH: read_json(FIXTURE / "failed-ack-run.json")["workflow_id"],
        }

    @staticmethod
    def response(value: object, headers: dict[str, str] | None = None) -> RECOVERY.HttpResponse:
        return RECOVERY.HttpResponse(200, headers or {}, json.dumps(value, separators=(",", ":")).encode())

    def __call__(self, request, _timeout: int, _maximum: int) -> RECOVERY.HttpResponse:
        parsed = urllib.parse.urlsplit(request.full_url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        prefix = f"/repos/{REPOSITORY}"
        self.requests.append((request.get_method(), path + ("?" + parsed.query if parsed.query else "")))

        if path == prefix:
            return self.response({"id": self.repository_id, "full_name": REPOSITORY, "default_branch": "main"})
        if path.startswith(prefix + "/actions/workflows/") and path.endswith(".yml"):
            workflow_path = ".github/workflows/" + pathlib.PurePosixPath(path).name
            return self.response({"id": self.workflow_ids[workflow_path], "path": workflow_path, "state": "active"})
        if path == prefix + "/git/ref/heads/main":
            return self.response({"object": {"sha": self.remote_main_sha}})
        if path == prefix + "/git/ref/heads/automation/canonical-update-state":
            return self.response({"object": {"sha": self.state_sha}})
        if path.startswith(prefix + "/actions/workflows/") and path.endswith("/runs"):
            if query.get("event") == ["workflow_dispatch"]:
                return self.response({"total_count": len(self.publisher_runs), "workflow_runs": self.publisher_runs}, {"Link": ""})
            return self.response({"total_count": 0, "workflow_runs": []}, {"Link": ""})
        if path == prefix + "/contents/reports/canonical-update-promotion-receipt.json":
            raw = json.dumps(self.journal, ensure_ascii=False, indent=2).encode() + b"\n"
            return self.response({"encoding": "base64", "size": len(raw), "content": base64.b64encode(raw).decode()})
        run_attempt_match = re.fullmatch(r"/repos/[^/]+/[^/]+/actions/runs/(\d+)/attempts/(\d+)", path)
        run_jobs_match = re.fullmatch(r"/repos/[^/]+/[^/]+/actions/runs/(\d+)/attempts/(\d+)/jobs", path)
        run_artifacts_match = re.fullmatch(r"/repos/[^/]+/[^/]+/actions/runs/(\d+)/artifacts", path)
        artifact_zip_match = re.fullmatch(r"/repos/[^/]+/[^/]+/actions/artifacts/(\d+)/zip", path)
        run_match = re.fullmatch(r"/repos/[^/]+/[^/]+/actions/runs/(\d+)", path)
        if run_attempt_match:
            run_id, attempt = map(int, run_attempt_match.groups())
            run = self.run_objects.get(run_id)
            if run is None or run.get("run_attempt") != attempt:
                raise AssertionError(f"unexpected publisher run attempt: {path}")
            return self.response(run)
        if run_jobs_match:
            run_id, attempt = map(int, run_jobs_match.groups())
            if run_id not in self.run_objects or self.run_objects[run_id].get("run_attempt") != attempt:
                raise AssertionError(f"unexpected publisher jobs request: {path}")
            return self.response(self.job_objects[run_id], {"Link": ""})
        if run_artifacts_match:
            run_id = int(run_artifacts_match.group(1))
            if run_id not in self.run_objects:
                raise AssertionError(f"unexpected publisher artifacts request: {path}")
            return self.response(self.artifact_objects[run_id], {"Link": ""})
        if artifact_zip_match:
            artifact_id = int(artifact_zip_match.group(1))
            archive = self.archives.get(artifact_id)
            if archive is None:
                raise AssertionError(f"unexpected publisher artifact download: {path}")
            return RECOVERY.HttpResponse(200, {"Content-Length": str(len(archive))}, archive)
        if run_match:
            run_id = int(run_match.group(1))
            run = self.run_objects.get(run_id)
            if run is None:
                raise AssertionError(f"unexpected publisher run readback: {path}")
            return self.response(run)
        pr_match = re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/(\d+)", path)
        if pr_match:
            number = int(pr_match.group(1))
            self.pr_reads.append(number)
            candidate = self.pr_record["candidate"]
            ownership = self.pr_record["ownership"]
            return self.response({
                "number": number,
                "html_url": self.pr_record["pr"]["url"],
                "state": "closed",
                "merged": True,
                "body": ownership["body"],
                "merge_commit_sha": "6a5138c792f4b7402da0c5ab439646bd752a307f",
                "base": {"ref": "main", "sha": self.controller_sha, "repo": {"full_name": REPOSITORY}},
                "head": {"ref": ownership["branch"], "sha": candidate["head_sha"], "repo": {"full_name": REPOSITORY}},
            })
        raise AssertionError(f"unexpected GitHub request: {request.get_method()} {path} query={query}")

    def set_journal(self, journal: dict) -> None:
        self.journal = copy.deepcopy(journal)
        self.pr_record = next(
            row for row in self.journal["records"]
            if row.get("candidate", {}).get("manifest_sha256")
            == "71a5ad4716ef5847210e2aeb8513ec136619e9521e757af106fdb3d048c33e50"
        )


class PublicationAckRecoveryIncidentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = {
            "journal": read_json(FIXTURE / "pre-recovery-journal.json"),
            "publisher": read_json(FIXTURE / "publisher-run.json"),
        }
        self.github = IncidentGitHub(self.fixture["journal"])
        self.api = RECOVERY.GitHubApi("offline-test-token", transport=self.github)
        self.writes: list[dict] = []
        self.schema = read_json(ROOT / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json")
        self.state_sha = self.github.state_sha

    def test_frozen_receipt_source_history_is_explicitly_available(self) -> None:
        _, binding_raw = RECOVERY.extract_receipt_archive((FIXTURE / "publication-receipts.zip").read_bytes())
        binding = RECOVERY.parse_json(binding_raw, label="fixture_source_binding")
        source_sha = binding["source_sha"]
        publisher_sha = binding["workflow_sha"]
        for commit in (source_sha, publisher_sha, "1a3088f64c0ff00fbf31e0e28cb37e3fc3d7dc07"):
            result = subprocess.run(
                ("git", "cat-file", "-e", f"{commit}^{{commit}}"),
                cwd=ROOT, check=False, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, f"pinned incident commit is unavailable: {commit}")
        ancestry = subprocess.run(
            ("git", "merge-base", "--is-ancestor", source_sha, publisher_sha),
            cwd=ROOT, check=False, capture_output=True,
        )
        self.assertEqual(ancestry.returncode, 0, "frozen source must remain an ancestor of its publisher head")
        tree = subprocess.run(
            ("git", "rev-parse", f"{source_sha}^{{tree}}"),
            cwd=ROOT, check=True, text=True, capture_output=True,
        ).stdout.strip()
        self.assertEqual(tree, binding["source_tree_sha"])

    def persist_cas(self, _root, _base, receipt, *, observed_at, expected_state_sha=RUNNER.STATE_EXPECTATION_UNSET, **_kwargs):
        if self.github.cas_conflict:
            raise RUNNER.PromotionError("promotion state compare-and-swap conflict: durable journal changed")
        if expected_state_sha is not RUNNER.STATE_EXPECTATION_UNSET and expected_state_sha != self.state_sha:
            raise RUNNER.PromotionError("promotion state compare-and-swap conflict: durable journal changed")
        self.github.journal = PR_HELPER.append_journal_record(
            self.github.journal,
            receipt,
            repository=REPOSITORY,
            observed_at=observed_at,
        )
        PR_HELPER.validate_journal(self.github.journal, self.schema)
        self.state_sha = hashlib.sha1(json.dumps(self.github.journal, sort_keys=True).encode()).hexdigest()
        self.github.state_sha = self.state_sha
        self.writes.append(copy.deepcopy(dict(receipt)))
        return self.state_sha

    def execute(
        self,
        *,
        journal: dict | None = None,
        remote_main_sha: str | None = None,
        mode: str = "workflow_run",
        event: dict | None = None,
        recovery_root: pathlib.Path = ROOT,
    ):
        if journal is not None:
            self.github.set_journal(journal)
        if remote_main_sha is not None:
            self.github.remote_main_sha = remote_main_sha
        if event is None:
            event = {"workflow_run": copy.deepcopy(self.fixture["publisher"])}
        recovery = RECOVERY.PublicationAckRecovery(recovery_root, REPOSITORY, self.api, RUNNER)
        env = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_RUN_ID": "37199709258",
            "GITHUB_RUN_ATTEMPT": "2",
        }
        with mock.patch.dict("os.environ", env), mock.patch.object(RUNNER, "persist_journal_record", side_effect=self.persist_cas):
            result = recovery.run(
                mode=mode, event=event,
                current_ack_run_id=37199709258, current_ack_attempt=2,
            )
        return result

    def local_git_remote(self, journal: dict, scratch: pathlib.Path) -> tuple[pathlib.Path, str, str]:
        """Build a temporary real Git remote with a durable promotion-state ref."""
        remote = scratch / "remote.git"
        controller = scratch / "controller"
        main_sha = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=ROOT, check=True, text=True, capture_output=True,
        ).stdout.strip()
        subprocess.run(("git", "clone", "--bare", "--shared", str(ROOT), str(remote)), check=True, capture_output=True)
        subprocess.run(("git", "--git-dir", str(remote), "update-ref", "refs/heads/main", main_sha), check=True, capture_output=True)
        subprocess.run(("git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"), check=True, capture_output=True)
        clone_env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
        subprocess.run(("git", "clone", "--shared", "--branch", "main", str(remote), str(controller)), check=True, capture_output=True, env=clone_env)
        # The local CAS clone does not inherit unreferenced, fetched fixture
        # commits from the outer checkout. Hydrate the same bounded history
        # windows that Verify Release fetches before running these tests.
        for commit, depth in (
            ("e34062309a48b0e0b6c0f38add32f0cdec088616", "8"),
            ("1a3088f64c0ff00fbf31e0e28cb37e3fc3d7dc07", "1"),
        ):
            subprocess.run(
                ("git", "-C", str(controller), "fetch", "--no-tags", f"--depth={depth}", str(ROOT), commit),
                check=True, capture_output=True,
            )
        for relative in (
            "scripts/canonical_update_pr.py",
            "scripts/materialize-canonical-registry.py",
            "scripts/run-canonical-update-promotion.py",
            "scripts/recover-canonical-publication-ack.py",
            "schemas/datapan.canonical-update-promotion-journal.v1.schema.json",
        ):
            destination = controller / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        subprocess.run(("git", "checkout", "-b", "automation/canonical-update-state"), cwd=controller, check=True, capture_output=True)
        journal_path = controller / RUNNER.JOURNAL_PATH
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        subprocess.run(("git", "add", "--", RUNNER.JOURNAL_PATH.as_posix()), cwd=controller, check=True, capture_output=True)
        subprocess.run((
            "git", "-c", "user.name=Incident Fixture", "-c", "user.email=incident@example.test",
            "commit", "-m", "Seed isolated promotion journal",
        ), cwd=controller, check=True, capture_output=True)
        state_sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=controller, check=True, text=True, capture_output=True).stdout.strip()
        subprocess.run(("git", "push", "origin", "HEAD:refs/heads/automation/canonical-update-state"), cwd=controller, check=True, capture_output=True)
        subprocess.run(("git", "checkout", "main"), cwd=controller, check=True, capture_output=True)
        return controller, main_sha, state_sha

    def test_actual_failed_ack_receipt_completes_through_real_reconcile_and_cas(self) -> None:
        original = copy.deepcopy(self.github.journal)
        result = self.execute()

        self.assertEqual(result["outcome"], "recovered")
        self.assertEqual(result["selected_publisher"]["run_id"], 37199628001)
        self.assertEqual(result["reconcile"]["status"], "read-back-confirmed")
        self.assertEqual(result["reconcile"]["journal_writes"], 1)
        self.assertEqual(self.github.pr_reads, [686])
        self.assertEqual(len(self.writes), 1)
        updated = self.github.journal["records"]
        self.assertEqual(len(updated), len(original["records"]))
        for before, after in zip(original["records"][:-1], updated[:-1], strict=True):
            self.assertEqual(after, before)
        recovered = updated[-1]
        self.assertEqual(recovered["status"], "read-back-confirmed")
        self.assertEqual(recovered["pr"]["merge_commit_sha"], "6a5138c792f4b7402da0c5ab439646bd752a307f")
        self.assertEqual(recovered["acknowledgements"][:2], original["records"][-1]["acknowledgements"])
        self.assertEqual([ack["status"] for ack in recovered["acknowledgements"]][-3:], ["publication-pending", "published", "read-back-confirmed"])
        self.assertTrue(
            recovered["acknowledgements"][-1]["evidence_reference"].startswith(RECOVERY.RECOVERY_REFERENCE_PREFIX)
        )
        self.assertLessEqual(result["github_requests"], RECOVERY.MAX_GITHUB_REQUESTS)

    def test_repeated_actual_incident_is_an_exact_zero_write_noop(self) -> None:
        first = self.execute()
        self.assertEqual(first["outcome"], "recovered")
        self.assertEqual(len(self.writes), 1)
        self.writes.clear()
        pr_readbacks_before = list(self.github.pr_reads)

        repeated = self.execute()

        self.assertEqual(repeated["outcome"], "already_acknowledged")
        self.assertEqual(repeated["already_acknowledged"], 1)
        self.assertEqual(repeated["git_cas_transactions"], 0)
        self.assertEqual(repeated["reconcile"] if "reconcile" in repeated else None, None)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.github.pr_reads, pr_readbacks_before)

    def test_malformed_source_binding_fails_before_pr_readback_or_any_write(self) -> None:
        receipt = read_json(FIXTURE / "hf-publication-receipt.json")
        binding = read_json(FIXTURE / "hf-source-binding.json")
        binding["manifest_sha256"] = "0" * 64
        archive = make_receipt_archive(receipt, binding)
        receipt_artifact = next(
            row for row in self.github.artifacts["artifacts"]
            if row.get("name") == RECOVERY.RECEIPT_ARTIFACT_NAME
        )
        receipt_artifact["size_in_bytes"] = len(archive)
        receipt_artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
        self.github.artifact_objects[self.github.publisher["id"]] = copy.deepcopy(self.github.artifacts)
        self.github.archives[receipt_artifact["id"]] = archive

        with self.assertRaisesRegex(RECOVERY.RecoveryError, "publication_receipt_binding_mismatch"):
            self.execute()

        self.assertEqual(self.writes, [])
        self.assertEqual(self.github.pr_reads, [])
        self.assertEqual(self.github.state_sha, "a" * 40)

    def test_moved_main_blocks_before_journal_write(self) -> None:
        with self.assertRaisesRegex(RECOVERY.RecoveryError, "current_main_moved"):
            self.execute(remote_main_sha="f" * 40)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.github.pr_reads, [])

    def test_journal_compare_and_swap_conflict_does_not_record_a_write(self) -> None:
        self.github.cas_conflict = True
        with self.assertRaisesRegex(RUNNER.PromotionError, "compare-and-swap conflict"):
            self.execute()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.github.journal, self.fixture["journal"])

    def test_pending_review_to_publication_uses_two_ordered_cas_transactions(self) -> None:
        rewound = copy.deepcopy(self.fixture["journal"])
        target = rewound["records"][-1]
        target["status"] = "pending-review"
        target["pr"] = {**target["pr"], "state": "open", "merge_commit_sha": None}
        target["acknowledgements"] = target["acknowledgements"][:1]
        PR_HELPER.validate_journal(rewound, self.schema)

        result = self.execute(journal=rewound)

        self.assertEqual(result["outcome"], "recovered")
        self.assertEqual(result["reconcile"]["status"], "read-back-confirmed")
        self.assertEqual(result["reconcile"]["journal_writes"], 2)
        self.assertEqual(result["git_cas_transactions"], 2)
        self.assertEqual(len(self.writes), 2)
        self.assertEqual([item["status"] for item in self.writes], ["merged", "read-back-confirmed"])
        self.assertEqual(self.github.journal["records"][-1]["status"], "read-back-confirmed")
        self.assertLessEqual(result["github_requests"], RECOVERY.MAX_GITHUB_REQUESTS)

    def test_twenty_run_schedule_skips_nineteen_acknowledged_receipts_then_recovers_target(self) -> None:
        journal = copy.deepcopy(self.fixture["journal"])
        previous = copy.deepcopy(journal["records"][1])
        for key in ("refresh_from", "refresh_target_main_sha", "superseded_by"):
            previous.pop(key, None)
        previous["status"] = "read-back-confirmed"
        previous["pr"] = {
            "number": 686,
            "url": "https://github.com/StatPan/datapan-registry/pull/686",
            "state": "merged",
            "merge_commit_sha": "1a3088f64c0ff00fbf31e0e28cb37e3fc3d7dc07",
        }
        old_publication = {
            "payload_revision": "1" * 40,
            "pointer_revision": "2" * 40,
        }
        acknowledgements = copy.deepcopy(previous["acknowledgements"][:1])
        base_ack = acknowledgements[0]
        for index, status in enumerate(("merged", "publication-pending", "published", "read-back-confirmed"), start=1):
            ack = copy.deepcopy(base_ack)
            ack.update({
                "status": status,
                "observed_at": f"2026-10-03T12:00:0{index}+00:00",
                "source_sha": "1a3088f64c0ff00fbf31e0e28cb37e3fc3d7dc07",
                "manifest_sha256": "4553fef934fd83b12b32f1f8c10453e37da64662eea3813bb6d1c1be0fb8a158",
                "read_back_verified": status == "read-back-confirmed",
                "read_back_sha256": previous["candidate"]["registry_sha256"] if status == "read-back-confirmed" else None,
                "read_back_bytes": previous["candidate"]["registry_bytes"] if status == "read-back-confirmed" else None,
                "publication_revision": old_publication["payload_revision"] if status in {"published", "read-back-confirmed"} else None,
                "publication_pointer_revision": old_publication["pointer_revision"] if status in {"published", "read-back-confirmed"} else None,
            })
            acknowledgements.append(ack)
        previous["acknowledgements"] = acknowledgements
        journal["records"] = [previous, copy.deepcopy(journal["records"][-1])]
        target = journal["records"][-1]
        for key in ("refresh_from", "refresh_target_main_sha", "superseded_by"):
            target.pop(key, None)
        target["status"] = "pending-review"
        target["pr"] = {**target["pr"], "state": "open", "merge_commit_sha": None}
        target["acknowledgements"] = target["acknowledgements"][:1]
        PR_HELPER.validate_journal(journal, self.schema)

        old_receipt = read_json(FIXTURE / "hf-publication-receipt.json")
        old_binding = copy.deepcopy(old_receipt["source_binding"])
        old_binding.update({
            "workflow_sha": "1a3088f64c0ff00fbf31e0e28cb37e3fc3d7dc07",
            "source_sha": "1a3088f64c0ff00fbf31e0e28cb37e3fc3d7dc07",
            "source_tree_sha": subprocess.run(
                ("git", "rev-parse", "1a3088f64c0ff00fbf31e0e28cb37e3fc3d7dc07^{tree}"),
                cwd=ROOT, check=True, text=True, capture_output=True,
            ).stdout.strip(),
            "manifest_sha256": "4553fef934fd83b12b32f1f8c10453e37da64662eea3813bb6d1c1be0fb8a158",
        })
        old_receipt["source_binding"] = old_binding
        old_receipt["publication"]["payload_revision"] = old_publication["payload_revision"]
        old_receipt["publication"]["pointer_revision"] = old_publication["pointer_revision"]
        old_receipt["anonymous_verification"]["revision"] = old_publication["payload_revision"]
        old_archive = make_receipt_archive(old_receipt, old_binding)

        self.github.set_journal(journal)
        self.github.publisher_runs = []
        self.github.run_objects = {}
        self.github.job_objects = {}
        self.github.artifact_objects = {}
        self.github.archives = {}
        for index in range(19):
            run_id = 37199000000 + index
            artifact_id = 11310000000 + index
            run = copy.deepcopy(self.fixture["publisher"])
            run.update({
                "id": run_id,
                "head_sha": "1a3088f64c0ff00fbf31e0e28cb37e3fc3d7dc07",
                "created_at": f"2026-10-04T10:{index:02d}:00Z",
                "run_started_at": f"2026-10-04T10:{index:02d}:00Z",
                "updated_at": "2026-10-04T11:43:38Z",
            })
            jobs = read_json(FIXTURE / "publisher-jobs.json")
            jobs["jobs"][0].update({
                "run_id": run_id,
                "id": 111430000000 + index,
                "head_sha": run["head_sha"],
            })
            artifacts = {
                "total_count": 1,
                "artifacts": [{
                    "id": artifact_id,
                    "name": RECOVERY.RECEIPT_ARTIFACT_NAME,
                    "size_in_bytes": len(old_archive),
                    "digest": "sha256:" + hashlib.sha256(old_archive).hexdigest(),
                    "expired": False,
                    "created_at": "2026-10-04T11:43:34Z",
                    "expires_at": "2026-11-03T11:43:34Z",
                    "workflow_run": {
                        "id": run_id,
                        "repository_id": self.github.repository_id,
                        "head_repository_id": self.github.repository_id,
                        "head_branch": "main",
                        "head_sha": run["head_sha"],
                    },
                }],
            }
            jobs["jobs"][0]["run_attempt"] = run["run_attempt"]
            self.github.publisher_runs.append(run)
            self.github.run_objects[run_id] = run
            self.github.job_objects[run_id] = jobs
            self.github.artifact_objects[run_id] = artifacts
            self.github.archives[artifact_id] = old_archive
        target_run = copy.deepcopy(self.fixture["publisher"])
        self.github.publisher_runs.append(target_run)
        self.github.run_objects[target_run["id"]] = target_run
        self.github.job_objects[target_run["id"]] = copy.deepcopy(self.github.jobs)
        target_artifacts = {
            "total_count": 1,
            "artifacts": [copy.deepcopy(next(
                row for row in self.github.artifacts["artifacts"]
                if row.get("name") == RECOVERY.RECEIPT_ARTIFACT_NAME
            ))],
        }
        self.github.artifact_objects[target_run["id"]] = target_artifacts
        self.github.archives[target_artifacts["artifacts"][0]["id"]] = self.github.archive

        with tempfile.TemporaryDirectory(prefix="publication-ack-incident-") as temporary:
            controller, main_sha, initial_state_sha = self.local_git_remote(journal, pathlib.Path(temporary))
            self.github.controller_sha = main_sha
            self.github.remote_main_sha = main_sha
            self.github.state_sha = initial_state_sha
            self.state_sha = initial_state_sha
            self.api = RECOVERY.GitHubApi("offline-test-token", transport=self.github)
            recovery = RECOVERY.PublicationAckRecovery(controller, REPOSITORY, self.api, RUNNER)
            env = {
                "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_RUN_ID": "37199709258",
                "GITHUB_RUN_ATTEMPT": "2",
                "GIT_LFS_SKIP_SMUDGE": "1",
            }
            real_subprocess_run = subprocess.run
            network_git_commands: list[tuple[str, ...]] = []

            def count_git_network(argv, *args, **kwargs):
                if isinstance(argv, (tuple, list)) and len(argv) > 1 and argv[0] == "git":
                    command_parts = tuple(str(part) for part in argv)
                    verb = next((part for part in command_parts[1:] if part in {"ls-remote", "fetch", "push"}), None)
                    if verb is not None:
                        network_git_commands.append(command_parts)
                return real_subprocess_run(argv, *args, **kwargs)

            with mock.patch.dict("os.environ", env), mock.patch.object(subprocess, "run", side_effect=count_git_network):
                result = recovery.run(
                    mode="schedule", event=None,
                    current_ack_run_id=37199709258, current_ack_attempt=2,
                )
            verbs = [next(part for part in command[1:] if part in {"ls-remote", "fetch", "push"}) for command in network_git_commands]
            self.assertEqual(verbs.count("ls-remote"), 4, network_git_commands)
            self.assertEqual(verbs.count("fetch"), 2, network_git_commands)
            self.assertEqual(verbs.count("push"), 2, network_git_commands)
            durable = subprocess.run(
                ("git", "--git-dir", str(pathlib.Path(temporary) / "remote.git"), "show", "refs/heads/automation/canonical-update-state:" + RUNNER.JOURNAL_PATH.as_posix()),
                check=True, text=True, capture_output=True,
            ).stdout
            durable_journal = json.loads(durable)
            PR_HELPER.validate_journal(durable_journal, self.schema)
            self.assertTrue(any(row.get("status") == "read-back-confirmed" for row in durable_journal["records"]))

        self.assertEqual(result["publisher_runs_considered"], 20)
        self.assertEqual(result["already_acknowledged"], 19, result)
        self.assertEqual(result["selected_publisher"]["run_id"], target_run["id"])
        self.assertEqual(result["outcome"], "recovered")
        self.assertEqual(result["reconcile"]["journal_writes"], 2)
        self.assertEqual(result["git_cas_transactions"], 2)
        self.assertEqual(result["github_requests"], self.api.request_count)
        self.assertEqual(self.github.pr_reads, [686])
        self.assertLessEqual(result["github_requests"], RECOVERY.MAX_GITHUB_REQUESTS)


if __name__ == "__main__":
    unittest.main()
