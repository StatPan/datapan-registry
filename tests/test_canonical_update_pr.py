from __future__ import annotations

import hashlib
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
        body: str, pr_number: int = 77,
    ) -> dict:
        candidate = self.candidate("same-generation")
        candidate.update({"head_sha": head_sha, "registry_sha256": registry_sha})
        owner = PROMOTION.owner_id(candidate["repository"], candidate["source_id"], candidate["scope"])
        body = f"{PROMOTION.body_marker(owner, candidate['generation_id'])}\n\n{body}"
        return {
            "schema_version": PROMOTION.SCHEMA_VERSION,
            "status": status,
            "action": "refresh_owned",
            "candidate": candidate,
            "ownership": {
                "owner_id": owner,
                "branch": PROMOTION.automation_branch(candidate, "create"),
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


if __name__ == "__main__":
    unittest.main()
