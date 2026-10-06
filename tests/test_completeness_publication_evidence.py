from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).parents[1].resolve()
EVIDENCE = ROOT / "reports/completeness-proof-evidence/issue-659/publication"
INPUT_INDEX = ROOT / "reports/completeness-proof-inputs.json"
FIXTURE = ROOT / "tests/fixtures/canonical-publication-ack/incident-37199628001-1"
SOURCE_SHA = "6a5138c792f4b7402da0c5ab439646bd752a307f"
MANIFEST_SHA = "71a5ad4716ef5847210e2aeb8513ec136619e9521e757af106fdb3d048c33e50"
REGISTRY_SHA = "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0"
REGISTRY_BYTES = 139155499


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_module(ROOT / "scripts/completeness_publication_evidence.py", "completeness_publication_evidence_test")


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_manifest_at_source() -> bytes:
    result = subprocess.run(
        ("git", "show", f"{SOURCE_SHA}:manifest.json"),
        cwd=ROOT,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError("the retained publication source commit is unavailable in this test checkout")
    return result.stdout


def actual_inputs() -> dict[str, object]:
    """Load the bounded checked-in run/artifact/ACK/read-back capture."""
    index = read_json(INPUT_INDEX)
    snapshot_rows = [row for row in index["inputs"] if row.get("role") == "source_catalog_snapshot"]
    if len(snapshot_rows) != 1:
        raise AssertionError("expected one local source snapshot identity")
    snapshot = snapshot_rows[0]
    return {
        "publisher_run": read_json(EVIDENCE / "publisher-run.json"),
        "publisher_jobs": read_json(EVIDENCE / "publisher-jobs.json"),
        "publisher_artifact_metadata": read_json(EVIDENCE / "publisher-artifacts.json"),
        "publisher_archive": (EVIDENCE / "publisher-receipts.zip").read_bytes(),
        "publication_receipt": (EVIDENCE / "publisher-receipt.json").read_bytes(),
        "source_binding": (EVIDENCE / "publisher-source-binding.json").read_bytes(),
        "source_commit": read_json(EVIDENCE / "publisher-source-commit.json"),
        "source_manifest": read_manifest_at_source(),
        "publisher_workflow": read_json(EVIDENCE / "publisher-workflow.json"),
        "pointer_before": {
            "repo_metadata": read_json(EVIDENCE / "publisher-repo-metadata-before.json"),
            "distribution_index": read_json(EVIDENCE / "publisher-pointer-before.json"),
        },
        "pointer_after": {
            "repo_metadata": read_json(EVIDENCE / "publisher-repo-metadata-after.json"),
            "distribution_index": read_json(EVIDENCE / "publisher-pointer-after.json"),
        },
        "pointer_immutable": read_json(EVIDENCE / "publisher-pointer-immutable.json"),
        "anonymous_manifest": (EVIDENCE / "publisher-anonymous-manifest.json").read_bytes(),
        "anonymous_payload": read_json(EVIDENCE / "publisher-readback-result.json"),
        "source_catalog_snapshot": {
            "path": snapshot["path"],
            "bytes": snapshot["bytes"],
            "sha256": snapshot["sha256"],
            "role": snapshot["role"],
        },
        "ack_run": read_json(EVIDENCE / "acknowledgement-run.json"),
        "ack_jobs": read_json(EVIDENCE / "acknowledgement-jobs.json"),
        "ack_log": (EVIDENCE / "acknowledgement-logs.zip").read_bytes(),
        "journal_before": read_json(EVIDENCE / "acknowledgement-journal-before.json"),
        "journal_after": read_json(EVIDENCE / "acknowledgement-journal-after.json"),
    }


def validate(inputs: dict[str, object], *, evaluation_epoch: str = "2026-10-04T13:23:00Z"):
    return MODULE.validate_historical_publication_facet(
        inputs=inputs,
        expected_source_sha=SOURCE_SHA,
        expected_manifest_sha256=MANIFEST_SHA,
        expected_registry_sha256=REGISTRY_SHA,
        expected_registry_bytes=REGISTRY_BYTES,
        repo_root=ROOT,
        evaluation_epoch=evaluation_epoch,
    )


class HistoricalPublicationFacetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inputs = actual_inputs()

    def test_actual_retained_publisher_ack_and_public_readback_are_historical_only(self) -> None:
        result = validate(self.inputs)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["subject"]["publisher_run_id"], 37199628001)
        self.assertEqual(result["subject"]["acknowledgement_run_id"], 37199709258)
        self.assertEqual(result["subject"]["payload_revision"], "5028b46f19b1730b0b9b56fa968825f7a7dcb90c")
        self.assertEqual(result["subject"]["pointer_revision"], "d53085285e1eaa5ab894a45514e4220eb8725be9")
        details = result["details"]
        self.assertEqual(details["classification"], "historical_delivery_only")
        self.assertFalse(details["currentness_established"])
        self.assertFalse(details["updated_claim_established"])
        self.assertFalse(details["release_authority"])
        self.assertFalse(details["cutover_established"])
        self.assertEqual(details["publisher_job_completed_at"], "2026-10-04T11:43:37Z")
        self.assertEqual(details["anonymous_readback_observed_at"], "2026-10-04T11:48:04.144691Z")
        self.assertEqual(details["acknowledgement_job_completed_at"], "2026-10-04T12:35:16Z")
        self.assertEqual(details["acknowledgement_journal_observed_at"], "2026-10-04T12:35:04.198997Z")

    def test_missing_optional_facet_is_explicit_and_never_current(self) -> None:
        result = validate({})
        self.assertEqual(result["status"], "missing")
        self.assertIn("publisher_run", result["missing"])
        self.assertFalse(result["details"]["currentness_established"])
        self.assertFalse(result["details"]["updated_claim_established"])

    def test_retained_archive_remains_historical_after_remote_artifact_expiry(self) -> None:
        result = validate(self.inputs, evaluation_epoch="2026-12-01T00:00:00Z")
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["details"]["publisher_artifact_available_at_evaluation"])
        self.assertEqual(result["details"]["classification"], "historical_delivery_only")

    def test_evaluation_before_anonymous_readback_rejects_as_of_claim(self) -> None:
        with self.assertRaisesRegex(MODULE.PublicationEvidenceError, "after_evaluation_epoch"):
            validate(self.inputs, evaluation_epoch="2026-10-04T11:47:00Z")

    def test_publisher_artifact_mutation_rejects(self) -> None:
        changed = copy.deepcopy(self.inputs)
        changed["publisher_archive"] = changed["publisher_archive"][:-1] + bytes([changed["publisher_archive"][-1] ^ 1])
        with self.assertRaises(MODULE.PublicationEvidenceError):
            validate(changed)

    def test_nonmatching_ack_attempt_rejects(self) -> None:
        changed = copy.deepcopy(self.inputs)
        changed["ack_run"]["run_attempt"] = 1
        with self.assertRaisesRegex(MODULE.PublicationEvidenceError, "acknowledgement_attempt_identity_mismatch"):
            validate(changed)

    def test_source_commit_pointer_patch_cannot_claim_other_lfs_object(self) -> None:
        changed = copy.deepcopy(self.inputs)
        source_commit = changed["source_commit"]
        registry = next(row for row in source_commit["files"] if row.get("filename") == "data/data-go-kr.registry.json")
        registry["patch"] = registry["patch"].replace(REGISTRY_SHA, "a" * 64)
        with self.assertRaises(MODULE.PublicationEvidenceError):
            validate(changed)

    def test_anonymous_manifest_bytes_must_match_source_manifest(self) -> None:
        changed = copy.deepcopy(self.inputs)
        changed["anonymous_manifest"] += b" "
        with self.assertRaisesRegex(MODULE.PublicationEvidenceError, "manifest_bytes_mismatch"):
            validate(changed)

    def test_pointer_after_revision_cannot_be_substituted(self) -> None:
        changed = copy.deepcopy(self.inputs)
        changed["pointer_after"]["repo_metadata"]["sha"] = "a" * 40
        with self.assertRaises(MODULE.PublicationEvidenceError):
            validate(changed)

    def test_readback_digest_and_size_are_both_bound(self) -> None:
        changed = copy.deepcopy(self.inputs)
        stream = next(row for row in changed["anonymous_payload"]["checks"]
                      if row.get("check") == "immutable_registry_stream_matches_expected_sha_and_size")
        stream["detail"]["bytes_streamed"] = REGISTRY_BYTES - 1
        with self.assertRaisesRegex(MODULE.PublicationEvidenceError, "anonymous_readback_subject_mismatch"):
            validate(changed)

    def test_final_journal_must_preserve_readback_identity(self) -> None:
        changed = copy.deepcopy(self.inputs)
        target = next(row for row in changed["journal_after"]["records"]
                      if row.get("candidate", {}).get("generation_id") == "66a2ae130fce7463dfbdd47caa48d3b7013e47a365a9a2745fe22b5378cd77d4"
                      and row.get("status") == "read-back-confirmed")
        target["acknowledgements"][-1]["read_back_sha256"] = "a" * 64
        with self.assertRaisesRegex(MODULE.PublicationEvidenceError, "journal_readback_identity_mismatch"):
            validate(changed)

    def test_unrelated_journal_record_mutation_rejects(self) -> None:
        changed = copy.deepcopy(self.inputs)
        changed["journal_after"]["records"][0]["status"] = "merged"
        with self.assertRaisesRegex(MODULE.PublicationEvidenceError, "unrelated_records_changed"):
            validate(changed)


if __name__ == "__main__":
    unittest.main()
