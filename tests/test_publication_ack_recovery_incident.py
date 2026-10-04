from __future__ import annotations

import base64
import copy
import datetime as dt
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
        self.commit_associations: dict[str, list[dict]] = {}
        self.commit_association_headers: dict[str, dict[str, str]] = {}
        self.commit_association_errors: dict[str, int] = {}
        self.redirect_hops_per_archive = 0
        self.api: RECOVERY.GitHubApi | None = None
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
        association_match = re.fullmatch(r"/repos/[^/]+/[^/]+/commits/([a-f0-9]{40})/pulls", path)
        if association_match:
            source_sha = association_match.group(1)
            if source_sha in self.commit_association_errors:
                return RECOVERY.HttpResponse(self.commit_association_errors[source_sha], {}, b"{}");
            if source_sha in self.commit_associations:
                return self.response(
                    self.commit_associations[source_sha],
                    self.commit_association_headers.get(source_sha, {"Link": ""}),
                )
            if source_sha == "6a5138c792f4b7402da0c5ab439646bd752a307f":
                return self.response([self.association_for_record(self.pr_record, source_sha)])
            return self.response([])
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
            if self.redirect_hops_per_archive and self.api is not None:
                # Charge the same shared meter used by SafeRedirectHandler;
                # redirect mechanics themselves are exercised by the
                # dedicated transport suite.
                for _ in range(self.redirect_hops_per_archive):
                    self.api._count_redirect()
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

    def association_for_record(self, row: dict, source_sha: str, *, merged_at: str = "2026-10-04T11:41:00Z") -> dict:
        candidate = row["candidate"]
        ownership = row["ownership"]
        pr = row["pr"]
        return {
            "number": pr["number"],
            "html_url": pr["url"],
            "state": "closed",
            "merged_at": merged_at,
            "merge_commit_sha": source_sha,
            "body": ownership["body"],
            "base": {
                "ref": "main",
                "sha": self.controller_sha,
                "repo": {"id": self.repository_id, "full_name": REPOSITORY},
            },
            "head": {
                "ref": ownership["branch"],
                "sha": candidate["head_sha"],
                "repo": {"id": self.repository_id, "full_name": REPOSITORY},
            },
        }

    def set_journal(self, journal: dict) -> None:
        self.journal = copy.deepcopy(journal)
        self.pr_record = next(
            row for row in self.journal["records"]
            if row.get("candidate", {}).get("manifest_sha256")
            == "71a5ad4716ef5847210e2aeb8513ec136619e9521e757af106fdb3d048c33e50"
        )

    def set_commit_association(self, source_sha: str, rows: list[dict], *, headers: dict[str, str] | None = None) -> None:
        self.commit_associations[source_sha] = copy.deepcopy(rows)
        if headers is not None:
            self.commit_association_headers[source_sha] = dict(headers)

    def outside_association(
        self,
        source_sha: str,
        *,
        number: int = 991,
        merged_at: str = "2026-10-04T09:59:00Z",
        head_ref: str = "feature/synthetic-outside-c-publication",
        head_sha: str = "c" * 40,
        body: str = "A synthetic unrelated release PR.",
    ) -> dict:
        return {
            "number": number,
            "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
            "state": "closed",
            "merged_at": merged_at,
            "merge_commit_sha": source_sha,
            "body": body,
            "base": {
                "ref": "main",
                "sha": self.controller_sha,
                "repo": {"id": self.repository_id, "full_name": REPOSITORY},
            },
            "head": {
                "ref": head_ref,
                "sha": head_sha,
                "repo": {"id": self.repository_id, "full_name": REPOSITORY},
            },
        }

    def add_publisher(
        self,
        *,
        run_id: int,
        source_sha: str,
        artifact_id: int,
        created_at: str,
        payload_revision: str | None = None,
        pointer_revision: str | None = None,
    ) -> dict:
        """Add a synthetic but fully receipt-authenticated publisher run."""
        workflow_head = self.publisher["head_sha"]
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        job_start = created + dt.timedelta(seconds=4)
        job_end = job_start + dt.timedelta(minutes=1)
        artifact_created = job_start + dt.timedelta(seconds=1)
        iso = lambda value: value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run = copy.deepcopy(self.publisher)
        run.update({
            "id": run_id,
            "head_sha": workflow_head,
            "created_at": iso(created),
            "run_started_at": iso(created),
            "updated_at": iso(job_end),
        })
        receipt_raw, _ = RECOVERY.extract_receipt_archive(self.archive)
        receipt = json.loads(receipt_raw)
        source_manifest = subprocess.run(
            ("git", "show", f"{source_sha}:manifest.json"),
            cwd=ROOT, check=True, capture_output=True,
        ).stdout
        source_tree = subprocess.run(
            ("git", "rev-parse", f"{source_sha}^{{tree}}"),
            cwd=ROOT, check=True, text=True, capture_output=True,
        ).stdout.strip()
        binding = copy.deepcopy(receipt["source_binding"])
        binding.update({
            "workflow_sha": workflow_head,
            "source_sha": source_sha,
            "source_tree_sha": source_tree,
            "manifest_sha256": hashlib.sha256(source_manifest).hexdigest(),
        })
        payload_revision = payload_revision or hashlib.sha1(f"payload:{run_id}".encode()).hexdigest()
        pointer_revision = pointer_revision or hashlib.sha1(f"pointer:{run_id}".encode()).hexdigest()
        receipt["source_binding"] = binding
        receipt["publication"]["payload_revision"] = payload_revision
        receipt["publication"]["pointer_revision"] = pointer_revision
        receipt["anonymous_verification"]["revision"] = payload_revision
        archive = make_receipt_archive(receipt, binding)
        jobs = copy.deepcopy(self.jobs)
        jobs["jobs"][0].update({
            "run_id": run_id,
            "head_sha": workflow_head,
            "started_at": iso(job_start),
            "completed_at": iso(job_end),
        })
        jobs["jobs"][0]["run_attempt"] = run["run_attempt"]
        artifact = {
            "id": artifact_id,
            "name": RECOVERY.RECEIPT_ARTIFACT_NAME,
            "size_in_bytes": len(archive),
            "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
            "expired": False,
            "created_at": iso(artifact_created),
            "expires_at": "2026-11-30T00:00:00Z",
            "workflow_run": {
                "id": run_id,
                "repository_id": self.repository_id,
                "head_repository_id": self.repository_id,
                "head_branch": "main",
                "head_sha": workflow_head,
            },
        }
        self.publisher_runs.append(run)
        self.run_objects[run_id] = run
        self.job_objects[run_id] = jobs
        self.artifact_objects[run_id] = {"total_count": 1, "artifacts": [artifact]}
        self.archives[artifact_id] = archive
        return run


class PublicationAckRecoveryIncidentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = {
            "journal": read_json(FIXTURE / "pre-recovery-journal.json"),
            "publisher": read_json(FIXTURE / "publisher-run.json"),
        }
        self.github = IncidentGitHub(self.fixture["journal"])
        self.api = RECOVERY.GitHubApi("offline-test-token", transport=self.github)
        self.github.api = self.api
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
        self.assertEqual(self.github.pr_reads, [])
        self.assertEqual(result["commit_association_reads"], 1)
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
            "number": 687,
            "url": "https://github.com/StatPan/datapan-registry/pull/687",
            "state": "merged",
            "merge_commit_sha": "1a3088f64c0ff00fbf31e0e28cb37e3fc3d7dc07",
        }
        # Keep the acknowledged prior receipt on its own valid owner branch/PR
        # so it cannot impersonate the refreshed target PR identity.
        previous["candidate"]["generation_id"] = "a" * 64
        ownership = previous["ownership"]
        ownership["branch"] = PR_HELPER.automation_branch(previous["candidate"], "create")
        ownership["body"] = PR_HELPER.render_pr_body(
            previous["candidate"], ownership["owner_id"], ownership["issue_number"],
        )
        ownership["body_sha256"] = hashlib.sha256(ownership["body"].encode()).hexdigest()
        if isinstance(previous.get("ci"), dict):
            previous["ci"].update({
                "head_sha": previous["candidate"]["head_sha"],
                "branch": ownership["branch"],
                "pr_number": previous["pr"]["number"],
                "owner_id": ownership["owner_id"],
                "body_sha256": ownership["body_sha256"],
            })
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
            self.github.api = self.api
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
            outside_run_ids = {row["run_id"] for row in result["outside_c_publications"]}
            for record in durable_journal["records"]:
                for acknowledgement in record.get("acknowledgements", []):
                    reference = RECOVERY.parse_recovery_reference(acknowledgement.get("evidence_reference"))
                    if reference is not None:
                        self.assertNotIn(reference["publisher_run_id"], outside_run_ids)
            self.assertTrue(any(row.get("status") == "read-back-confirmed" for row in durable_journal["records"]))

        self.assertEqual(result["publisher_runs_considered"], 20)
        self.assertEqual(result["already_acknowledged"], 19, result)
        self.assertEqual(result["selected_publisher"]["run_id"], target_run["id"])
        self.assertEqual(result["outcome"], "recovered")
        self.assertEqual(result["reconcile"]["journal_writes"], 2)
        self.assertEqual(result["git_cas_transactions"], 2)
        self.assertEqual(result["github_requests"], self.api.request_count)
        self.assertEqual(self.github.pr_reads, [])
        self.assertLessEqual(result["github_requests"], RECOVERY.MAX_GITHUB_REQUESTS)

    def test_nineteen_outside_publications_do_not_starve_twentieth_c_ack_with_full_budget(self) -> None:
        journal = copy.deepcopy(self.fixture["journal"])
        target = journal["records"][-1]
        target["status"] = "pending-review"
        target["pr"] = {**target["pr"], "state": "open", "merge_commit_sha": None}
        target["acknowledgements"] = target["acknowledgements"][:1]
        PR_HELPER.validate_journal(journal, self.schema)
        self.github.set_journal(journal)
        self.github.publisher_runs = []
        self.github.run_objects = {}
        self.github.job_objects = {}
        self.github.artifact_objects = {}
        self.github.archives = {}
        self.github.redirect_hops_per_archive = 4

        outside_source = "e34062309a48b0e0b6c0f38add32f0cdec088616"
        for index in range(19):
            self.github.add_publisher(
                run_id=37220000000 + index,
                source_sha=outside_source,
                artifact_id=11320000000 + index,
                created_at=f"2026-10-04T10:{index:02d}:00Z",
            )
        outside = self.github.outside_association(outside_source)
        self.github.set_commit_association(outside_source, [outside])

        target_run = copy.deepcopy(self.fixture["publisher"])
        self.github.publisher_runs.append(target_run)
        self.github.run_objects[target_run["id"]] = target_run
        self.github.job_objects[target_run["id"]] = copy.deepcopy(self.github.jobs)
        target_artifact = copy.deepcopy(next(
            row for row in self.github.artifacts["artifacts"]
            if row.get("name") == RECOVERY.RECEIPT_ARTIFACT_NAME
        ))
        self.github.artifact_objects[target_run["id"]] = {
            "total_count": 1,
            "artifacts": [target_artifact],
        }
        self.github.archives[target_artifact["id"]] = self.github.archive

        with tempfile.TemporaryDirectory(prefix="publication-ack-outside-window-") as temporary:
            controller, main_sha, initial_state_sha = self.local_git_remote(journal, pathlib.Path(temporary))
            self.github.controller_sha = main_sha
            self.github.remote_main_sha = main_sha
            self.github.state_sha = initial_state_sha
            self.state_sha = initial_state_sha
            self.api = RECOVERY.GitHubApi("offline-test-token", transport=self.github)
            self.github.api = self.api
            recovery = RECOVERY.PublicationAckRecovery(controller, REPOSITORY, self.api, RUNNER)
            before_write_counts: list[int] = []
            after_write_guard_counts: list[int] = []
            original_assert_idle = recovery.assert_idle

            def observe_write_guard(*args, **kwargs):
                if kwargs.get("include_acknowledgements") is False:
                    before_write_counts.append(self.api.request_count)
                result = original_assert_idle(*args, **kwargs)
                if kwargs.get("include_acknowledgements") is False:
                    after_write_guard_counts.append(self.api.request_count)
                return result

            recovery.assert_idle = observe_write_guard
            env = {
                "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_RUN_ID": "37220100000",
                "GITHUB_RUN_ATTEMPT": "1",
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
                    current_ack_run_id=37220100000, current_ack_attempt=1,
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

        self.assertEqual(result["publisher_runs_considered"], 20)
        self.assertEqual(result["already_acknowledged"], 0)
        self.assertEqual(len(result["outside_c_publications"]), 19)
        self.assertEqual(len({row["run_id"] for row in result["outside_c_publications"]}), 19)
        self.assertEqual(len({row["artifact_id"] for row in result["outside_c_publications"]}), 19)
        self.assertEqual(len({row["receipt_sha256"] for row in result["outside_c_publications"]}), 19)
        self.assertTrue(all(row["source_sha"] == outside_source for row in result["outside_c_publications"]))
        self.assertEqual(result["commit_association_reads"], 20)
        self.assertEqual(
            sum("/commits/" in path and path.endswith("/pulls?per_page=100") for _method, path in self.github.requests),
            20,
        )
        self.assertEqual(result["selected_publisher"]["run_id"], target_run["id"])
        self.assertEqual(result["outcome"], "recovered")
        self.assertEqual(result["reconcile"]["status"], "read-back-confirmed")
        self.assertEqual(result["reconcile"]["journal_writes"], 2)
        self.assertEqual(result["git_cas_transactions"], 2)
        self.assertEqual([row["status"] for row in durable_journal["records"]], [row["status"] for row in journal["records"][:-1]] + ["read-back-confirmed"])
        self.assertEqual(len(durable_journal["records"][-1]["acknowledgements"]), len(journal["records"][-1]["acknowledgements"]) + 4)
        self.assertEqual(len(before_write_counts), 2)
        self.assertEqual(len(after_write_guard_counts), 2)
        self.assertLessEqual(before_write_counts[0], RECOVERY.MAX_GITHUB_REQUESTS - 32)
        self.assertEqual(after_write_guard_counts[0] - before_write_counts[0], 11)
        self.assertEqual(after_write_guard_counts[1] - before_write_counts[1], 11)
        self.assertLessEqual(result["github_requests"], RECOVERY.MAX_GITHUB_REQUESTS)
        self.assertEqual(result["github_requests"], self.api.request_count)
        self.assertEqual(self.github.pr_reads, [])

    def test_outside_publication_workflow_run_is_report_only_and_zero_write(self) -> None:
        source_sha = "e34062309a48b0e0b6c0f38add32f0cdec088616"
        run = self.github.add_publisher(
            run_id=37221000000,
            source_sha=source_sha,
            artifact_id=11321000000,
            created_at="2026-10-04T10:00:00Z",
        )
        self.github.set_commit_association(source_sha, [self.github.outside_association(source_sha)])
        before = copy.deepcopy(self.github.journal)

        result = self.execute(mode="workflow_run", event={"workflow_run": run})

        self.assertEqual(result["outcome"], "outside_c_publications_only")
        self.assertEqual(result["outside_c_publications"][0]["run_id"], run["id"])
        self.assertEqual(result["selected_publisher"], None)
        self.assertEqual(result["git_cas_transactions"], 0)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.github.journal, before)

    def _outside_workflow_run(self) -> tuple[dict, str, dict]:
        source_sha = "e34062309a48b0e0b6c0f38add32f0cdec088616"
        run = self.github.add_publisher(
            run_id=37222000000,
            source_sha=source_sha,
            artifact_id=11322000000,
            created_at="2026-10-04T10:00:00Z",
        )
        association = self.github.outside_association(source_sha)
        self.github.set_commit_association(source_sha, [association])
        return run, source_sha, association

    def test_outside_source_association_failures_are_closed(self) -> None:
        cases = (
            ("missing", lambda rows, headers: ([], headers), "commit_pull_request_association_ambiguous_or_missing"),
            ("truncated", lambda rows, headers: (rows, {"Link": '<https://api.github.com/next>; rel="next"'}), "commit_pull_request_association_incomplete"),
            ("ambiguous", lambda rows, headers: ([rows[0], {**rows[0], "number": 992, "html_url": f"https://github.com/{REPOSITORY}/pull/992"}], headers), "commit_pull_request_association_ambiguous_or_missing"),
            ("wrong-url", lambda rows, headers: ([{**rows[0], "html_url": "https://example.net/not-the-associated-pr"}], headers), "associated_pull_request_url_invalid"),
            ("wrong-base", lambda rows, headers: ([{**rows[0], "base": {**rows[0]["base"], "ref": "release"}}], headers), "associated_pull_request_repository_or_base_mismatch"),
            ("wrong-head-repository", lambda rows, headers: ([{**rows[0], "head": {**rows[0]["head"], "repo": {"id": self.github.repository_id + 1, "full_name": "Other/repo"}}}], headers), "associated_pull_request_repository_or_base_mismatch"),
            ("invalid-head-ref", lambda rows, headers: ([{**rows[0], "head": {**rows[0]["head"], "ref": "bad..ref"}}], headers), "associated_pull_request_head_ref_invalid"),
            ("late-merge", lambda rows, headers: ([{**rows[0], "merged_at": "2026-10-04T10:02:00Z"}], headers), "associated_pull_request_merged_after_publisher"),
            ("wrong-merge-sha", lambda rows, headers: ([{**rows[0], "merge_commit_sha": "f" * 40}], headers), "commit_pull_request_association_ambiguous_or_missing"),
            ("api-error", lambda rows, headers: (rows, headers), "github_api_http_500"),
            ("canonical-branch-without-journal", lambda rows, headers: ([{**rows[0], "head": {**rows[0]["head"], "ref": "Automation/Canonical-Update/fake"}}], headers), "canonical_publication_owner_missing_from_journal"),
            ("canonical-namespace-root-without-journal", lambda rows, headers: ([{**rows[0], "head": {**rows[0]["head"], "ref": "automation/canonical-update"}}], headers), "canonical_publication_owner_missing_from_journal"),
            ("owner-marker-without-journal", lambda rows, headers: ([{**rows[0], "body": "DATAPAN-CANONICAL-UPDATE:V1: forged owner"}], headers), "canonical_publication_owner_missing_from_journal"),
        )
        for name, mutate, error in cases:
            with self.subTest(case=name):
                self.setUp()
                run, source_sha, association = self._outside_workflow_run()
                rows, headers = [association], {"Link": ""}
                rows, headers = mutate(rows, headers)
                self.github.set_commit_association(source_sha, rows, headers=headers)
                if name == "api-error":
                    self.github.commit_association_errors[source_sha] = 500
                before = copy.deepcopy(self.github.journal)
                with self.assertRaisesRegex(RECOVERY.RecoveryError, error):
                    self.execute(mode="workflow_run", event={"workflow_run": run})
                self.assertEqual(self.writes, [])
                self.assertEqual(self.github.journal, before)
                self.assertEqual(self.github.pr_reads, [])

    def test_missing_outside_source_association_stops_schedule_before_later_c_run(self) -> None:
        run, source_sha, association = self._outside_workflow_run()
        # The authenticated outside source has no complete merge association.
        # The later C publisher must remain untouched until this uncertainty is resolved.
        self.github.set_commit_association(source_sha, [association], headers={"Link": '<https://api.github.com/next>; rel="next"'})
        target_id = self.fixture["publisher"]["id"]
        before = copy.deepcopy(self.github.journal)

        result = self.execute(mode="schedule", event=None)

        self.assertEqual(result["outcome"], "recovery_blocked")
        self.assertEqual(result["unresolved_candidates"][0]["run_id"], run["id"])
        self.assertEqual(result["selected_publisher"], None)
        self.assertNotIn(target_id, [row["run_id"] for row in result.get("outside_c_publications", [])])
        target_attempt_path = f"/repos/{REPOSITORY}/actions/runs/{target_id}/attempts/1"
        self.assertFalse(any(path == target_attempt_path for _method, path in self.github.requests))
        self.assertEqual(self.writes, [])
        self.assertEqual(self.github.journal, before)

    def test_same_manifest_without_exact_source_or_pr_identity_remains_outside_c(self) -> None:
        outside_source = "e34062309a48b0e0b6c0f38add32f0cdec088616"
        target_manifest = self.github.pr_record["candidate"]["manifest_sha256"]
        verified = {
            "publisher_job_started_at": "2026-10-04T10:00:04Z",
            "publication": {
                "source_sha": outside_source,
                "manifest_sha256": target_manifest,
            },
        }
        self.github.set_commit_association(
            outside_source,
            [self.github.outside_association(outside_source)],
        )
        recovery = RECOVERY.PublicationAckRecovery(ROOT, REPOSITORY, self.api, RUNNER)
        recovery.initialize()

        disposition, association = recovery.classify_publication(
            verified, self.github.journal, source_claims=[],
        )

        self.assertEqual(disposition, "outside_c")
        self.assertEqual(association["number"], 991)

    def test_source_manifest_conflict_in_journal_is_not_treated_as_outside_c(self) -> None:
        publication = {
            "source_sha": "e34062309a48b0e0b6c0f38add32f0cdec088616",
            "manifest_sha256": "1" * 64,
        }
        journal = {"records": [{
            "candidate": {"manifest_sha256": "2" * 64},
            "pr": {"merge_commit_sha": publication["source_sha"]},
            "acknowledgements": [],
        }]}
        with self.assertRaisesRegex(RECOVERY.RecoveryError, "publication_journal_source_manifest_conflict"):
            RECOVERY.PublicationAckRecovery._source_claim_rows(publication, journal)

    def test_ambiguous_durable_source_claims_are_not_treated_as_outside_c(self) -> None:
        publication = {
            "source_sha": "e34062309a48b0e0b6c0f38add32f0cdec088616",
            "manifest_sha256": "1" * 64,
        }
        row = {
            "candidate": {"manifest_sha256": publication["manifest_sha256"]},
            "pr": {"merge_commit_sha": publication["source_sha"]},
            "acknowledgements": [],
        }
        journal = {"records": [copy.deepcopy(row), copy.deepcopy(row)]}

        with self.assertRaisesRegex(RECOVERY.RecoveryError, "publication_journal_source_claim_ambiguous"):
            RECOVERY.PublicationAckRecovery._source_claim_rows(publication, journal)

    def test_source_associated_pr_selector_never_reads_or_invents_other_candidates(self) -> None:
        target = copy.deepcopy(self.github.journal["records"][-1])
        unrelated = copy.deepcopy(target)
        unrelated["pr"]["number"] = 992
        unrelated["pr"]["url"] = f"https://github.com/{REPOSITORY}/pull/992"
        observed_numbers: list[int] = []

        def readback(number: int) -> dict:
            observed_numbers.append(number)
            return {
                "number": number,
                "state": "MERGED",
                "mergeCommit": {"oid": "6a5138c792f4b7402da0c5ab439646bd752a307f"},
            }

        tracked, _ = RUNNER.select_publication_candidate(
            [unrelated, target],
            source_sha="6a5138c792f4b7402da0c5ab439646bd752a307f",
            readback=readback,
            expected_pr_number=target["pr"]["number"],
        )

        self.assertEqual(tracked["pr"]["number"], target["pr"]["number"])
        self.assertEqual(observed_numbers, [target["pr"]["number"]])
        with self.assertRaisesRegex(RUNNER.PromotionError, "expected PR number is invalid"):
            RUNNER.select_publication_candidate(
                [target], source_sha="6a5138c792f4b7402da0c5ab439646bd752a307f",
                readback=readback, expected_pr_number=True,
            )


if __name__ == "__main__":
    unittest.main()
