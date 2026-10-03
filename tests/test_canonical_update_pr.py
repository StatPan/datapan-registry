from __future__ import annotations

import hashlib
import copy
import importlib.util
import pathlib
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/canonical_update_pr.py"
SPEC = importlib.util.spec_from_file_location("canonical_update_pr_test_module", SCRIPT)
assert SPEC and SPEC.loader
PROMOTION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROMOTION)


class CanonicalUpdatePromotionTests(unittest.TestCase):
    def candidate(self, generation: str = "generation-a") -> dict:
        return {
            "repository": "StatPan/datapan-registry",
            "source_id": "data_go_kr",
            "scope": "aggregate_supported_catalog",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "manifest_sha256": "c" * 64,
            "registry_path": "data/data-go-kr.registry.json",
            "registry_bytes": 123,
            "registry_sha256": "d" * 64,
            "composition_receipt_sha256": "e" * 64,
            "generation_id": generation,
        }

    def ack(self, candidate: dict, status: str, source_sha: str, **overrides) -> dict:
        ack = {
            "status": status,
            "observed_at": "2026-10-01T12:00:00Z",
            "source_sha": source_sha,
            "manifest_sha256": candidate["manifest_sha256"],
            "artifact_identity": {
                "path": candidate["registry_path"],
                "bytes": candidate["registry_bytes"],
                "sha256": candidate["registry_sha256"],
            },
            "evidence_reference": "test authoritative readback",
            "run_url": "https://github.com/StatPan/datapan-registry/actions/runs/123/attempts/2",
            "run_id": 123,
            "run_attempt": 2,
            "read_back_verified": status == "read-back-confirmed",
            "read_back_sha256": candidate["registry_sha256"] if status == "read-back-confirmed" else None,
            "read_back_bytes": candidate["registry_bytes"] if status == "read-back-confirmed" else None,
            "publication_revision": None,
            "publication_pointer_revision": None,
        }
        ack.update(overrides)
        return ack

    def revision_receipt(
        self, *, registry_sha: str, head_sha: str, status: str,
        body: str, pr_number: int = 77, generation: str = "same-generation",
    ) -> dict:
        candidate = self.candidate(generation)
        candidate.update({"head_sha": head_sha, "registry_sha256": registry_sha})
        owner = PROMOTION.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"])
        legacy_branch = (
            "automation/canonical-update/data-go-kr-"
            + hashlib.sha256(candidate["scope"].encode()).hexdigest()[:12]
        )
        body = f"{PROMOTION.body_marker(owner, candidate['generation_id'])}\n\n{body}"
        return {
            "schema_version": PROMOTION.SCHEMA_VERSION,
            "status": status,
            "action": "refresh_owned",
            "candidate": candidate,
            "ownership": {
                "owner_id": owner,
                "branch": legacy_branch,
                "expected_head_sha": head_sha,
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "body": body,
                "issue_number": 652,
                "issue_url": "https://github.com/StatPan/datapan-registry/issues/652",
            },
            "pr": {"number": pr_number, "url": "https://github.com/StatPan/datapan-registry/pull/77", "state": "open"},
            "acknowledgements": ([{
                "status": "pending-review", "observed_at": "2026-10-01T12:00:00Z",
            }] if status == "pending-review" else []),
            "blockers": ["manual_review_revalidation_required"],
        }

    def test_prepared_unconfigured_candidate_records_authoritative_merge(self) -> None:
        candidate = self.candidate()
        owner = PROMOTION.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"])
        body = PROMOTION.render_pr_body(candidate, owner, 652)
        receipt = {
            "status": "prepared",
            "candidate": {**candidate, "composition_receipt": {}},
            "checks": {"finish_review_policy": "unconfigured"},
            "ownership": {
                "owner_id": owner,
                "branch": PROMOTION.automation_branch(candidate, "create"),
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            },
            "pr": {"number": 0, "url": "", "state": "missing", "merge_commit_sha": None},
            "acknowledgements": [],
            "blockers": ["finish_review_policy_unconfigured"],
        }
        observed = PROMOTION.record_pr_readback(receipt, {
            "number": 77,
            "url": "https://github.com/StatPan/datapan-registry/pull/77",
            "state": "MERGED",
            "body": body,
            "headRefName": receipt["ownership"]["branch"],
            "headRefOid": candidate["head_sha"],
            "baseRefName": "main",
            "mergeCommit": {"oid": "f" * 40},
        }, observed_at="2026-10-01T12:00:00Z", run_url="https://github.com/StatPan/datapan-registry/actions/runs/124/attempts/1")
        self.assertEqual(observed["status"], "merged")
        self.assertEqual([row["status"] for row in observed["acknowledgements"]], ["pending-review", "merged"])
        self.assertIn("finish_review_policy_unconfigured", observed["blockers"])
        self.assertEqual(observed["pr"]["merge_commit_sha"], "f" * 40)

    def test_pending_current_diagnostic_scope_is_a_promotion_blocker(self) -> None:
        candidate = self.candidate()
        candidate.update({
            "composition_receipt": {},
            "composition_receipt_path": "receipt.json",
            "composition_outputs_dir": "composition",
            "payload_readback": {},
            "manual_review_acceptance_status": "accepted",
            "checks": {
                **{name: "passed" for name in PROMOTION.REQUIRED_CHECKS},
                "finish_review_policy": "unconfigured",
                "manual_review_acceptance": "accepted",
                "diagnostic_current_source_applicability": "revalidation_required",
            },
            "source_refresh_evidence": {
                "schema_version": "datapan.canonical-source-refresh-evidence.v1",
                "diagnostic_current_source_applicability": {"status": "revalidation_required"},
            },
            "validation_evidence": [
                {
                    "name": name,
                    "source_sha": candidate["head_sha"],
                    "manifest_sha256": candidate["manifest_sha256"],
                    "command": f"validate {name}",
                    "exit_code": 0,
                }
                for name in (*PROMOTION.REQUIRED_CHECKS, "composition", "manual_review_acceptance", "diagnostic_current_source_applicability")
            ],
        })
        with patch.object(PROMOTION, "validate_candidate", return_value=None):
            receipt = PROMOTION.build_receipt(candidate, candidate["base_sha"], [], {})
        self.assertIn("diagnostic_current_source_revalidation_required", receipt["blockers"])
        self.assertIn("finish_review_policy_unconfigured", receipt["blockers"])
        self.assertEqual(receipt["checks"]["diagnostic_current_source_applicability"], "revalidation_required")

    def test_readback_cannot_confirm_a_different_huggingface_revision(self) -> None:
        candidate = self.candidate()
        published = self.ack(candidate, "published", "f" * 40, publication_revision="1" * 40, publication_pointer_revision="2" * 40, read_back_verified=False)
        receipt = {
            "status": "published",
            "candidate": candidate,
            "pr": {"merge_commit_sha": "f" * 40},
            "acknowledgements": [published],
        }
        readback = self.ack(
            candidate,
            "read-back-confirmed",
            "f" * 40,
            publication_revision="3" * 40,
            publication_pointer_revision="2" * 40,
        )
        with self.assertRaisesRegex(PROMOTION.AdmissionError, "published immutable revision"):
            PROMOTION.record_acknowledgement(receipt, readback)

    def test_readback_accepts_only_the_same_published_payload_and_pointer_revision(self) -> None:
        candidate = self.candidate()
        published = self.ack(candidate, "published", "f" * 40, publication_revision="1" * 40, publication_pointer_revision="2" * 40)
        receipt = {
            "status": "published",
            "candidate": candidate,
            "pr": {"merge_commit_sha": "f" * 40},
            "acknowledgements": [published],
        }
        result = PROMOTION.record_acknowledgement(receipt, self.ack(
            candidate,
            "read-back-confirmed",
            "f" * 40,
            publication_revision="1" * 40,
            publication_pointer_revision="2" * 40,
        ))
        self.assertEqual(result["status"], "read-back-confirmed")

    def test_new_generation_does_not_replace_unpublished_candidate_history(self) -> None:
        old = {
            "schema_version": PROMOTION.SCHEMA_VERSION,
            "status": "publication-pending",
            "candidate": {**self.candidate("generation-a"), "payload_readback": {}, "manual_review_acceptance_status": "accepted"},
            "acknowledgements": [],
        }
        journal = PROMOTION.append_journal_record(None, old, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:00:00Z")
        new = {
            "schema_version": PROMOTION.SCHEMA_VERSION,
            "status": "prepared",
            "candidate": {**self.candidate("generation-b"), "payload_readback": {}, "manual_review_acceptance_status": "revalidation_required"},
            "acknowledgements": [],
        }
        journal = PROMOTION.append_journal_record(journal, new, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:01:00Z")
        self.assertEqual({row["candidate"]["generation_id"] for row in journal["records"]}, {"generation-a", "generation-b"})

    def test_equal_second_last_good_order_uses_actions_attempt_identity(self) -> None:
        journal = None
        for generation, run_id in (("generation-a", 100), ("generation-b", 101)):
            receipt = {
                "schema_version": PROMOTION.SCHEMA_VERSION,
                "status": "read-back-confirmed",
                "candidate": self.candidate(generation),
                "acknowledgements": [{"status": "read-back-confirmed", "observed_at": "2026-10-01T12:00:00Z", "run_id": run_id, "run_attempt": 1}],
            }
            journal = PROMOTION.append_journal_record(journal, receipt, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:00:00Z")
        self.assertEqual([row["candidate"]["generation_id"] for row in journal["records"]], ["generation-b"])

    def test_changed_pr_body_blocks_automation_ownership(self) -> None:
        candidate = self.candidate()
        row = {
            "repository": candidate["repository"],
            "source_id": candidate["source_id"],
            "scope": candidate["scope"],
            "state": "open",
            "owner_id": PROMOTION.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"]),
            "number": 5,
            "head_sha": candidate["head_sha"],
            "automation_head_sha": candidate["head_sha"],
            "body_sha256": "2" * 64,
            "automation_body_sha256": "1" * 64,
            "candidate_head_sha": candidate["head_sha"],
            "manifest_sha256": candidate["manifest_sha256"],
            "registry_sha256": candidate["registry_sha256"],
        }
        with self.assertRaisesRegex(PROMOTION.AdmissionError, "human_body_change"):
            PROMOTION.decide_existing_prs(candidate, [row])

    def test_same_generation_distinct_payloads_have_distinct_durable_revision_keys(self) -> None:
        old = self.revision_receipt(
            registry_sha="d" * 64, head_sha="b" * 40,
            status="pending-review", body="old candidate review body\n",
        )
        new = self.revision_receipt(
            registry_sha="f" * 64, head_sha="c" * 40,
            status="prepared", body="new candidate review body\n",
        )
        old_key = PROMOTION.candidate_key(old)
        new_key = PROMOTION.candidate_key(new)
        self.assertEqual(old_key[:4], new_key[:4])
        self.assertNotEqual(old_key, new_key)
        journal = PROMOTION.append_journal_record(
            None, old, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:00:00Z",
        )
        journal = PROMOTION.append_journal_record(
            journal, new, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:01:00Z",
        )
        self.assertEqual(len(journal["records"]), 2)

    def test_new_pr_branch_is_revision_specific_and_refresh_inherits_exact_legacy_branch(self) -> None:
        first = self.candidate("generation-a")
        second = {**self.candidate("generation-b"), "registry_sha256": "9" * 64}
        first_branch = PROMOTION.automation_branch(first, "create")
        second_branch = PROMOTION.automation_branch(second, "create")
        self.assertNotEqual(first_branch, second_branch)
        self.assertEqual(first_branch, PROMOTION.automation_branch(first, "create"))

        legacy_scope_hash = hashlib.sha256(first["scope"].encode()).hexdigest()[:12]
        legacy_branch = f"automation/canonical-update/data-go-kr-{legacy_scope_hash}"
        row = {
            "repository": first["repository"], "source_id": first["source_id"], "scope": first["scope"],
            "state": "open", "owner_id": PROMOTION.owner_id(first["repository"], first["source_id"], first["scope"]),
            "number": 77, "head_sha": "b" * 40, "automation_head_sha": "b" * 40,
            "body_sha256": "a" * 64, "automation_body_sha256": "a" * 64,
            "candidate_head_sha": "b" * 40, "manifest_sha256": first["manifest_sha256"],
            "registry_sha256": "d" * 64, "head_ref": legacy_branch,
        }
        decision = PROMOTION.decide_existing_prs(first, [row])
        self.assertEqual(decision["action"], "reuse_owned")
        self.assertEqual(decision["branch"], legacy_branch)

    def test_open_pr_branch_outside_owner_scope_namespace_is_rejected(self) -> None:
        candidate = self.candidate()
        row = {
            "repository": candidate["repository"], "source_id": candidate["source_id"],
            "scope": candidate["scope"], "state": "open",
            "owner_id": PROMOTION.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"]),
            "number": 77, "head_sha": "b" * 40, "automation_head_sha": "b" * 40,
            "body_sha256": "a" * 64, "automation_body_sha256": "a" * 64,
            "candidate_head_sha": candidate["head_sha"], "manifest_sha256": candidate["manifest_sha256"],
            "registry_sha256": candidate["registry_sha256"], "head_ref": "automation/canonical-update/other-source-abc",
        }
        with self.assertRaisesRegex(PROMOTION.AdmissionError, "outside the exact automation source/scope"):
            PROMOTION.decide_existing_prs(candidate, [row])

    def test_replacement_uses_empty_revision_branch_and_refresh_uses_exact_legacy_ref(self) -> None:
        class FakeGit:
            class AvailabilityError(Exception):
                pass

            def __init__(self, refs: dict[str, str]):
                self.refs = dict(refs)
                self.pushes: list[tuple[str, str]] = []

            def git_output(self, argv: list[str], _root: pathlib.Path, *, availability: bool = False) -> bytes:
                if argv[0] == "ls-remote":
                    ref = argv[-1]
                    sha = self.refs.get(ref)
                    return f"{sha}\t{ref}\n".encode() if sha else b""
                if argv[0] == "push":
                    revision, ref = argv[-1].split(":", 1)
                    self.refs[ref] = revision
                    self.pushes.append((revision, ref))
                    return b""
                raise AssertionError(argv)

        repository_root = pathlib.Path(".")
        candidate = self.candidate("generation-new")
        candidate.update({"head_sha": "c" * 40, "registry_sha256": "f" * 64})
        owner = PROMOTION.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"])
        prefix = PROMOTION.automation_branch(candidate, "create")
        old_stable = "automation/canonical-update/data-go-kr-" + hashlib.sha256(candidate["scope"].encode()).hexdigest()[:12]
        closed = {"repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"], "state": "closed", "number": 77}
        decision = PROMOTION.decide_existing_prs(candidate, [closed])
        self.assertEqual(decision["branch"], prefix)
        fake = FakeGit({"refs/heads/main": candidate["base_sha"], f"refs/heads/{old_stable}": "b" * 40})
        receipt = {
            "action": decision["action"],
            "candidate": {"payload_readback": {"status": "verified", "readback": "isolated_lfs_storage_verified"}},
            "ownership": {"owner_id": owner, "branch": decision["branch"]},
        }
        PROMOTION.push_owned_branch(
            candidate, receipt, [closed], candidate["base_sha"],
            repository_root=repository_root, materializer=fake,
        )
        self.assertEqual(fake.refs[f"refs/heads/{prefix}"], candidate["head_sha"])
        self.assertEqual(fake.refs[f"refs/heads/{old_stable}"], "b" * 40)
        self.assertEqual(fake.pushes, [(candidate["head_sha"], f"refs/heads/{prefix}")])
        self.assertEqual(PROMOTION.automation_branch(candidate, "create"), prefix)

        legacy = self.revision_receipt(registry_sha="d" * 64, head_sha="b" * 40, status="pending-review", body="legacy\n")
        legacy_branch = legacy["ownership"]["branch"]
        existing = {
            "repository": candidate["repository"], "source_id": candidate["source_id"], "scope": candidate["scope"],
            "state": "open", "owner_id": owner, "number": 77,
            "head_sha": "b" * 40, "automation_head_sha": "b" * 40,
            "body_sha256": legacy["ownership"]["body_sha256"],
            "automation_body_sha256": legacy["ownership"]["body_sha256"],
            "candidate_head_sha": "b" * 40, "manifest_sha256": candidate["manifest_sha256"],
            "registry_sha256": "d" * 64, "head_ref": legacy_branch,
        }
        refresh_decision = PROMOTION.decide_existing_prs(candidate, [existing])
        self.assertEqual(refresh_decision["action"], "refresh_owned")
        self.assertEqual(refresh_decision["branch"], legacy_branch)
        refresh_fake = FakeGit({
            "refs/heads/main": candidate["base_sha"],
            f"refs/heads/{legacy_branch}": "b" * 40,
        })
        refresh_receipt = {
            "action": refresh_decision["action"],
            "candidate": {"payload_readback": {"status": "verified", "readback": "isolated_lfs_storage_verified"}},
            "ownership": {"owner_id": owner, "branch": legacy_branch},
        }
        PROMOTION.push_owned_branch(
            candidate, refresh_receipt, [existing], candidate["base_sha"],
            repository_root=repository_root, materializer=refresh_fake,
        )
        self.assertEqual(refresh_fake.pushes, [(candidate["head_sha"], f"refs/heads/{legacy_branch}")])

        moved_fake = FakeGit({
            "refs/heads/main": candidate["base_sha"],
            f"refs/heads/{legacy_branch}": "9" * 40,
        })
        with self.assertRaisesRegex(PROMOTION.AdmissionError, "compare-and-swap conflict"):
            PROMOTION.push_owned_branch(
                candidate, refresh_receipt, [existing], candidate["base_sha"],
                repository_root=repository_root, materializer=moved_fake,
            )
        self.assertEqual(moved_fake.pushes, [])

    def test_refresh_supersession_waits_for_exact_pr_readback_and_keeps_old_revision(self) -> None:
        old = self.revision_receipt(
            registry_sha="d" * 64, head_sha="b" * 40,
            status="pending-review", body="old candidate review body\n",
        )
        new = self.revision_receipt(
            registry_sha="f" * 64, head_sha="c" * 40,
            status="prepared", body="new candidate review body\n",
        )
        new["refresh_from"] = PROMOTION.revision_reference(old)
        journal = PROMOTION.append_journal_record(
            None, old, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:00:00Z",
        )
        journal = PROMOTION.append_journal_record(
            journal, new, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:01:00Z",
        )
        schema = __import__("json").loads(
            (pathlib.Path(__file__).parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text()
        )
        PROMOTION.validate_journal(journal, schema)
        old_ref = PROMOTION.revision_reference(old)

        with self.assertRaisesRegex(PROMOTION.AdmissionError, "before exact pending-review PR read-back"):
            PROMOTION.append_journal_record(
                journal, new, repository="StatPan/datapan-registry",
                observed_at="2026-10-01T12:02:00Z", supersede_from=old_ref,
            )
        self.assertIsNone(journal["records"][0].get("superseded_by"))

        readback = PROMOTION.record_pr_readback(new, {
            "number": 77,
            "url": "https://github.com/StatPan/datapan-registry/pull/77",
            "state": "OPEN",
            "body": new["ownership"]["body"],
            "headRefName": new["ownership"]["branch"],
            "headRefOid": new["candidate"]["head_sha"],
            "baseRefName": "main",
            "mergeCommit": None,
        }, observed_at="2026-10-01T12:03:00Z", run_url="https://github.com/StatPan/datapan-registry/actions/runs/124/attempts/1")
        journal = PROMOTION.append_journal_record(
            journal, readback, repository="StatPan/datapan-registry",
            observed_at="2026-10-01T12:03:00Z", supersede_from=old_ref,
        )
        PROMOTION.validate_journal(journal, schema)
        self.assertEqual(len(journal["records"]), 2)
        self.assertEqual(journal["records"][0]["status"], "pending-review")
        self.assertEqual(journal["records"][0]["superseded_by"], PROMOTION.revision_reference(readback))
        self.assertEqual(journal["records"][1]["status"], "pending-review")
        self.assertEqual(journal["records"][1]["refresh_from"], old_ref)
        self.assertIn("manual_review_revalidation_required", journal["records"][1]["blockers"])

    def test_refresh_links_reject_a_dangling_or_cross_owner_predecessor(self) -> None:
        old = self.revision_receipt(
            registry_sha="d" * 64, head_sha="b" * 40,
            status="pending-review", body="old candidate review body\n",
        )
        new = self.revision_receipt(
            registry_sha="f" * 64, head_sha="c" * 40,
            status="prepared", body="new candidate review body\n",
        )
        bad_reference = PROMOTION.revision_reference(old)
        bad_reference["owner_id"] = "datapan-canonical-update:v1:" + "9" * 64
        new["refresh_from"] = bad_reference
        journal = PROMOTION.append_journal_record(
            None, old, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:00:00Z",
        )
        journal = PROMOTION.append_journal_record(
            journal, new, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:01:00Z",
        )
        schema = __import__("json").loads(
            (pathlib.Path(__file__).parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text()
        )
        with self.assertRaisesRegex(PROMOTION.AdmissionError, "exact durable predecessor"):
            PROMOTION.validate_journal(journal, schema)

    def test_historical_pending_witness_allows_full_lifecycle_and_retains_refresh_chain(self) -> None:
        schema = __import__("json").loads(
            (pathlib.Path(__file__).parents[1] / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json").read_text()
        )

        def pending_readback(receipt: dict, run_id: int, observed_at: str) -> dict:
            return PROMOTION.record_pr_readback(receipt, {
                "number": 77,
                "url": "https://github.com/StatPan/datapan-registry/pull/77",
                "state": "OPEN",
                "body": receipt["ownership"]["body"],
                "headRefName": receipt["ownership"]["branch"],
                "headRefOid": receipt["candidate"]["head_sha"],
                "baseRefName": "main",
                "mergeCommit": None,
            }, observed_at=observed_at,
                run_url=f"https://github.com/StatPan/datapan-registry/actions/runs/{run_id}/attempts/1")

        a = self.revision_receipt(registry_sha="d" * 64, head_sha="b" * 40, status="pending-review", body="A\n")
        a = pending_readback(a, 120, "2026-10-01T12:00:00Z")
        ci = {
            "repository": a["candidate"]["repository"],
            "workflow_path": ".github/workflows/verify-release.yml",
            "head_sha": a["candidate"]["head_sha"], "branch": a["ownership"]["branch"],
            "pr_number": a["pr"]["number"], "owner_id": a["ownership"]["owner_id"],
            "body_sha256": a["ownership"]["body_sha256"], "request_fingerprint": "1" * 64,
            "state": "success", "intent_at": "2026-10-01T12:00:00Z", "run_id": 90,
            "run_attempt": 1, "run_url": "https://github.com/StatPan/datapan-registry/actions/runs/90",
            "run_status": "completed", "conclusion": "success", "observed_at": "2026-10-01T12:00:00Z",
            "dispatch_http_status": 200, "blocker": None,
        }
        a["ci"] = copy.deepcopy(ci)
        journal = PROMOTION.append_journal_record(
            None, a, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:00:00Z",
        )

        b = self.revision_receipt(registry_sha="e" * 64, head_sha="c" * 40, status="prepared", body="B\n")
        b["refresh_from"] = PROMOTION.revision_reference(a)
        journal = PROMOTION.append_journal_record(
            journal, b, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:01:00Z",
        )
        b = pending_readback(b, 121, "2026-10-01T12:02:00Z")
        b["ci"] = {**ci, "head_sha": b["candidate"]["head_sha"], "body_sha256": b["ownership"]["body_sha256"], "intent_at": "2026-10-01T12:02:00Z", "observed_at": "2026-10-01T12:02:00Z"}
        journal = PROMOTION.append_journal_record(
            journal, b, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:02:00Z",
            supersede_from=b["refresh_from"],
        )

        c = self.revision_receipt(registry_sha="f" * 64, head_sha="d" * 40, status="prepared", body="C\n")
        c["refresh_from"] = PROMOTION.revision_reference(b)
        journal = PROMOTION.append_journal_record(
            journal, c, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:03:00Z",
        )
        c = pending_readback(c, 122, "2026-10-01T12:04:00Z")
        c["ci"] = {**ci, "head_sha": c["candidate"]["head_sha"], "body_sha256": c["ownership"]["body_sha256"], "intent_at": "2026-10-01T12:04:00Z", "observed_at": "2026-10-01T12:04:00Z"}
        journal = PROMOTION.append_journal_record(
            journal, c, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:04:00Z",
            supersede_from=c["refresh_from"],
        )
        original_ci = copy.deepcopy(c["ci"])
        original_pending = copy.deepcopy(c["acknowledgements"][0])

        transitions = (
            ("merged", "e" * 40, {}),
            ("publication-pending", "e" * 40, {}),
            ("published", "e" * 40, {"publication_revision": "1" * 40, "publication_pointer_revision": "2" * 40}),
            ("read-back-confirmed", "e" * 40, {
                "publication_revision": "1" * 40, "publication_pointer_revision": "2" * 40,
                "read_back_verified": True, "read_back_sha256": c["candidate"]["registry_sha256"],
                "read_back_bytes": c["candidate"]["registry_bytes"],
            }),
        )
        for index, (status, source_sha, extra) in enumerate(transitions, start=1):
            updated = copy.deepcopy(c)
            acknowledgement = self.ack(
                c["candidate"], status, source_sha,
                observed_at=f"2026-10-01T12:0{4 + index}:00Z",
                run_id=122 + index, run_attempt=1,
                run_url=f"https://github.com/StatPan/datapan-registry/actions/runs/{122 + index}/attempts/1",
                **extra,
            )
            c = PROMOTION.record_acknowledgement(updated, acknowledgement)
            journal = PROMOTION.append_journal_record(
                journal, c, repository="StatPan/datapan-registry",
                observed_at=f"2026-10-01T12:0{4 + index}:00Z",
            )
            PROMOTION.validate_journal(journal, schema)

        self.assertEqual(c["status"], "read-back-confirmed")
        self.assertEqual(c["ci"], original_ci)
        self.assertEqual(c["acknowledgements"][0], original_pending)

        d = self.revision_receipt(
            registry_sha="9" * 64, head_sha="9" * 40, status="read-back-confirmed",
            body="unrelated newer generation\n", generation="newer-generation",
        )
        d_ack = self.ack(d["candidate"], "read-back-confirmed", "8" * 40,
                         read_back_verified=True, read_back_sha256=d["candidate"]["registry_sha256"],
                         read_back_bytes=d["candidate"]["registry_bytes"],
                         publication_revision="3" * 40, publication_pointer_revision="4" * 40,
                         run_url="https://github.com/StatPan/datapan-registry/actions/runs/130/attempts/1")
        d_ack["run_id"] = 130
        d_ack["run_attempt"] = 1
        d["acknowledgements"] = [d_ack]
        journal = PROMOTION.append_journal_record(
            journal, d, repository="StatPan/datapan-registry", observed_at="2026-10-01T12:10:00Z",
        )
        PROMOTION.validate_journal(journal, schema)

        retained = {PROMOTION.candidate_key(row): row for row in journal["records"]}
        for receipt in (a, b, c, d):
            self.assertIn(PROMOTION.candidate_key(receipt), retained)
        self.assertEqual(retained[PROMOTION.candidate_key(a)]["ci"], ci)
        self.assertEqual(retained[PROMOTION.candidate_key(c)]["ci"], original_ci)
        self.assertEqual(retained[PROMOTION.candidate_key(c)]["acknowledgements"][0], original_pending)

    def test_supersession_requires_full_pending_readback_witness(self) -> None:
        old = self.revision_receipt(registry_sha="d" * 64, head_sha="b" * 40, status="pending-review", body="old\n")
        new = self.revision_receipt(registry_sha="f" * 64, head_sha="c" * 40, status="pending-review", body="new\n")
        new["refresh_from"] = PROMOTION.revision_reference(old)
        old["superseded_by"] = PROMOTION.revision_reference(new)
        new["acknowledgements"] = [{"status": "pending-review"}]
        journal = {"records": [old, new]}
        with self.assertRaisesRegex(PROMOTION.AdmissionError, "immutable exact pending-review"):
            PROMOTION.validate_revision_links(journal)

    def test_historical_pending_witness_binds_head_manifest_artifact_and_exact_actions_attempt(self) -> None:
        old = self.revision_receipt(registry_sha="d" * 64, head_sha="b" * 40, status="pending-review", body="old\n")
        target = self.revision_receipt(registry_sha="f" * 64, head_sha="c" * 40, status="prepared", body="new\n")
        target["refresh_from"] = PROMOTION.revision_reference(old)
        target = PROMOTION.record_pr_readback(target, {
            "number": 77, "url": "https://github.com/StatPan/datapan-registry/pull/77", "state": "OPEN",
            "body": target["ownership"]["body"], "headRefName": target["ownership"]["branch"],
            "headRefOid": target["candidate"]["head_sha"], "baseRefName": "main", "mergeCommit": None,
        }, observed_at="2026-10-01T12:00:00Z",
            run_url="https://github.com/StatPan/datapan-registry/actions/runs/124/attempts/2")
        old["superseded_by"] = PROMOTION.revision_reference(target)
        valid = {"records": [old, target]}
        PROMOTION.validate_revision_links(valid)

        tamperers = (
            lambda row: row.update(source_sha="e" * 40),
            lambda row: row.update(manifest_sha256="e" * 64),
            lambda row: row["artifact_identity"].update(sha256="e" * 64),
            lambda row: row.update(run_url="https://github.com/StatPan/datapan-registry/actions/runs/999/attempts/2"),
            lambda row: row.update(run_id=999),
            lambda row: row.update(run_attempt=1),
        )
        for tamper in tamperers:
            with self.subTest(tamper=tamper):
                altered = copy.deepcopy(valid)
                tamper(altered["records"][1]["acknowledgements"][0])
                with self.assertRaisesRegex(PROMOTION.AdmissionError, "immutable exact pending-review"):
                    PROMOTION.validate_revision_links(altered)


if __name__ == "__main__":
    unittest.main()
