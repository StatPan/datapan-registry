from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import pprint
import sys
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from datetime import timezone
from typing import Any
from unittest import mock
import shutil
import zipfile
from contextlib import contextmanager

import jsonschema


ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import upstream_catalogue_handoff as HANDOFF


def load_script(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DERIVATION = load_script("same_observation_derivation_tests", ROOT / "scripts/upstream_catalogue_derivation.py")
PROCESSOR = load_script("same_observation_processor_tests", ROOT / "scripts/process-upstream-catalogue-candidate.py")
PREPARATION = load_script("same_observation_preparation_tests", ROOT / "scripts/prepare-upstream-catalogue-derivation.py")
PROMOTION = load_script("same_observation_promotion_tests", ROOT / "scripts/run-canonical-update-promotion.py")
import upstream_catalogue_source_eligibility as ELIGIBILITY
COMPOSER = load_script("same_observation_composer_tests", ROOT / "scripts/compose-upstream-catalogue-candidate.py")
FIXTURES = ROOT / "tests/fixtures/same-observation-derivation"
PROCESSOR_TESTS = load_script(
    "same_observation_processor_test_helpers",
    ROOT / "tests/test_process_upstream_catalogue_candidate.py",
)
HEALTH_TESTS = load_script(
    "same_observation_health_test_helpers",
    ROOT / "tests/test_check_upstream_catalogue_health.py",
)


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


_USE_READBACK_MERGED = object()


def primary_pr_transport(
    readback: Mapping[str, Any],
    *,
    cli_state: str | None = None,
    rest_merged: Any = _USE_READBACK_MERGED,
    include_rest_merged: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split one normalized fixture into the two native GitHub response shapes."""
    state = cli_state or str(readback.get("state") or "")
    cli = {
        key: copy.deepcopy(readback.get(key))
        for key in (
            "number", "url", "body", "headRefName", "headRefOid",
            "baseRefName", "mergeCommit",
        )
    }
    cli["state"] = state
    merged = readback.get("merged") if rest_merged is _USE_READBACK_MERGED else rest_merged
    rest = {
        "state": "closed" if state in {"CLOSED", "MERGED"} else "open",
        "merged_at": "2026-10-01T00:00:00Z" if merged is True else None,
        "base": {
            "sha": readback.get("baseRefOid") or "0" * 40,
            "repo": {"full_name": readback.get("repository")},
        },
        "head": {
            "sha": readback.get("headRefOid"),
            "repo": {"full_name": readback.get("headRepository")},
        },
    }
    if include_rest_merged:
        rest["merged"] = merged
    return cli, rest


def actual_admission(checkpoint: dict, summary: dict) -> dict:
    original = summary["original_a"]
    reference = checkpoint["input_artifacts"][0]
    return {
        "producer_run_id": original["producer_run_id"],
        "run_attempt": original["producer_run_attempt"],
        "head_sha": original["producer_head_sha"],
        "artifact_id": original["producer_artifact_id"],
        "artifact_name": original["producer_artifact_name"],
        "artifact_digest_sha256": "a" * 64,
        "observed_at": original["observed_at"],
        "candidate_sha256": reference["candidate_sha256"],
        "refresh_evidence_sha256": reference["evidence_sha256"],
    }


def native_parent_reference(name: str, checkpoint: dict, original: dict) -> dict:
    evidence_bytes = (FIXTURES / f"native-b-{name}-enrichment.json").read_bytes()
    evidence = json.loads(evidence_bytes)
    return DERIVATION.processor_parent_reference(
        checkpoint,
        repository="StatPan/datapan-registry",
        original_observation=original,
        enrichment_evidence=evidence,
        enrichment_evidence_bytes=evidence_bytes,
    )


def native_c_journal() -> dict:
    fixture = read_json(FIXTURES / "native-merged-c-row.json")
    return {"records": [copy.deepcopy(fixture["journal_record"])]}


def synthetic_contribution(identity: str, endpoint: str) -> dict:
    operation = {
        "id": f"synthetic-operation-{identity}",
        "endpoint": endpoint,
        "source": {"system": "test-only synthetic fixture", "url": f"https://www.data.go.kr/data/{identity}/openapi.do"},
    }
    row = {
        "api_key": {"provider": "data.go.kr", "id": identity},
        "status": "enriched",
        "source_sha256": "c" * 64,
        "guide_sha256": None,
        "observed_guide_url": None,
        "observed_guide_url_sha256": None,
        "operations": [operation],
        "operations_sha256": DERIVATION.object_sha256([operation]),
        "source_provenance": {"system": "test-only synthetic fixture"},
    }
    return row


def synthetic_readback(
    *, generation_id: str, registry_sha256: str, registry_bytes: int,
    main_sha: str, merge_sha: str, pr_number: int, journal_ref_sha: str,
    manifest_sha256: str,
) -> tuple[dict, dict]:
    body = f"Synthetic test-only canonical merge fixture for {generation_id}; no remote PR exists."
    row = {
        "schema_version": "datapan.canonical-update-promotion-journal-record.v1",
        "status": "merged",
        "superseded_by": None,
        "candidate": {
            "repository": "StatPan/datapan-registry", "source_id": "data_go_kr",
            "scope": "aggregate_supported_catalog", "generation_id": generation_id,
            "registry_path": DERIVATION.SAFE_PATH, "registry_bytes": registry_bytes,
            "registry_sha256": registry_sha256, "manifest_sha256": manifest_sha256,
            "head_sha": "d" * 40,
        },
        "ownership": {
            "branch": f"automation/canonical-update/test-{pr_number}",
            "expected_head_sha": "d" * 40,
            "body": body,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        },
        "pr": {"number": pr_number, "state": "merged", "merge_commit_sha": merge_sha},
        "acknowledgements": [{
            "status": "merged", "observed_at": "2026-10-05T00:00:00Z",
            "source_sha": merge_sha, "manifest_sha256": manifest_sha256,
            "run_id": 99000000000 + pr_number, "run_attempt": 1,
            "run_url": f"https://github.com/StatPan/datapan-registry/actions/runs/{99000000000 + pr_number}/attempts/1",
        }],
        "test_only": True,
    }
    journal = {"records": [row], "test_only": True}
    readback = DERIVATION.canonical_parent_readback_reference(
        journal, journal_ref_sha=journal_ref_sha, record_index=0,
    )
    composition_baseline = {
        "main_sha": main_sha,
        "manifest_sha256": manifest_sha256,
        "registry_path": DERIVATION.SAFE_PATH,
        "registry_sha256": registry_sha256,
        "registry_bytes": registry_bytes,
    }
    return journal, {"readback": readback, "composition_baseline": composition_baseline}


def synthetic_bundle_checkpoint(
    template: dict, original: dict, envelope: dict, *, registry_bytes: bytes,
    evidence: dict, run_id: str, artifact_id: str,
) -> tuple[dict, dict[str, bytes]]:
    """Build a sealed synthetic B output from native A identity; never provider proof."""
    generation_id, inputs = PROCESSOR.generation_identity(
        original["source_id"], original["source_scope"], original["original_baseline_sha256"],
        original["candidate_sha256"], None, original["source_policy_sha256"],
        original["provider_index_sha256"], same_observation_derivation=envelope,
    )
    evidence_bytes = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    synthetic_outputs = {
        "composed-candidate.registry.json": registry_bytes,
        "ready-scope.registry.json": registry_bytes,
        "semantic-diff.json": b'{"test_only":true,"summary":{"added":1,"changed":0,"removed":0}}',
        "regeneration-queue.json": b'{"test_only":true,"items":[]}',
        "quarantine.json": b'{"test_only":true,"items":[]}',
        "composition-receipt.json": b'{"test_only":true,"status":"ready_scoped"}',
        "upstream-catalogue-enrichment-evidence.json": evidence_bytes,
        "upstream-catalogue-processing-result.json": b'{}',
    }
    checkpoint = copy.deepcopy(template)
    checkpoint.update({
        "generation_id": generation_id,
        "generation_inputs": inputs,
        "source_id": original["source_id"],
        "source_scope": original["source_scope"],
        "observed_at": original["observed_at"],
        "observation_count": original["observation_count"],
        "last_observation": {
            "observed_at": original["observed_at"],
            "producer_run_id": original["producer_run_id"],
            "refresh_evidence_sha256": original["evidence_sha256"],
            "collection_status": "success", "execution_mode": "live",
        },
        "status": "ready",
        "output_digests": [
            {"path": name, "sha256": hashlib.sha256(synthetic_outputs[name]).hexdigest(), "bytes": len(synthetic_outputs[name])}
            for name in DERIVATION.PROCESSOR_BUNDLE_FILES
        ],
        "outcome": copy.deepcopy(template.get("outcome", {})),
    })
    checkpoint["output_artifact"] = {
        "repository": "StatPan/datapan-registry", "run_id": run_id,
        "name": f"upstream-catalogue-processing-{run_id}-1", "artifact_id": artifact_id,
        "expires_at": "2026-11-04T00:00:00Z",
        "bundle_manifest_sha256": DERIVATION.object_sha256(checkpoint["output_digests"]),
    }
    PROCESSOR.seal_checkpoint(checkpoint)
    result = {
        "generation_id": generation_id, "status": "ready", "source_id": original["source_id"],
        "processor_artifact_run_id": run_id,
        "processor_run_id": f"{run_id}-1", "candidate_available": True,
    }
    synthetic_outputs["upstream-catalogue-processing-result.json"] = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    result_row = next(row for row in checkpoint["output_digests"] if row["path"] == "upstream-catalogue-processing-result.json")
    result_row.update({"sha256": hashlib.sha256(synthetic_outputs["upstream-catalogue-processing-result.json"]).hexdigest(), "bytes": len(synthetic_outputs["upstream-catalogue-processing-result.json"])})
    checkpoint["output_artifact"]["bundle_manifest_sha256"] = DERIVATION.object_sha256(checkpoint["output_digests"])
    PROCESSOR.seal_checkpoint(checkpoint)
    synthetic_outputs[DERIVATION.CHECKPOINT_RECEIPT] = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return checkpoint, synthetic_outputs


class SameObservationNativeLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.summary = read_json(FIXTURES / "native-lineage-identities.json")
        self.parents = {
            name: read_json(FIXTURES / f"native-b-{name}.json")
            for name in ("66a", "9938")
        }
        self.schema = read_json(ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json")

    def test_retained_native_parents_preserve_one_original_a_and_distinct_b_cursors(self) -> None:
        originals = {}
        references = {}
        for name, checkpoint in self.parents.items():
            jsonschema.Draft202012Validator(self.schema).validate(checkpoint)
            self.assertEqual(
                checkpoint["checkpoint_sha256"],
                PROCESSOR.checkpoint_digest(checkpoint),
            )
            admission = actual_admission(checkpoint, self.summary)
            originals[name] = DERIVATION.original_observation_from_checkpoint(checkpoint, admission)
            references[name] = native_parent_reference(name, checkpoint, originals[name])

        self.assertEqual(originals["66a"], originals["9938"])
        original = originals["9938"]
        self.assertEqual(original["producer_run_id"], "36646768289")
        self.assertEqual(original["observed_at"], "2026-09-29T23:44:39Z")
        self.assertEqual(original["observation_count"], 1)
        self.assertEqual(original["original_baseline_sha256"], "eeda72ee8590f458de8d75703662578e80edf3e61282f0e5e67547c4f6e5f644")
        self.assertEqual(original["candidate_sha256"], "90bc22e0ad61dd3672b09b7c52e4898be5afb535f87a8d7fa9e5e5c983e6e3b5")

        # The two retained B checkpoints belong to the same A but were made by
        # distinct processor revisions and carry distinct retry windows.
        self.assertNotEqual(
            references["66a"]["processor_generator_revision"],
            references["9938"]["processor_generator_revision"],
        )
        self.assertNotEqual(
            references["66a"]["processor_extractor_revision"],
            references["9938"]["processor_extractor_revision"],
        )
        self.assertEqual(self.parents["66a"]["attempts_consumed"], 48)
        self.assertEqual(self.parents["66a"]["detail_queue_cursor"], 72)
        self.assertEqual(self.parents["9938"]["attempts_consumed"], 24)
        self.assertEqual(self.parents["9938"]["detail_queue_cursor"], 248)
        self.assertEqual(len(self.parents["9938"]["attempts_by_id"]), 248)
        self.assertEqual(sum(self.parents["9938"]["attempts_by_id"].values()), 264)
        self.assertEqual(self.parents["9938"]["observation_count"], 1)

        # These are the retained native parent outputs: no successful detail
        # contribution existed to copy. Additional test operations are marked
        # synthetic elsewhere and cannot be mistaken for live provider proof.
        self.assertEqual(references["66a"]["contribution_count"], 0)
        self.assertEqual(references["9938"]["contribution_count"], 0)
        self.assertIn("synthetic", self.summary["test_disclaimer"])

    def test_native_merged_c_row_binds_exact_producer_manifest_pr_and_run_attempt(self) -> None:
        journal = native_c_journal()
        row = journal["records"][0]
        original = DERIVATION.original_observation_from_checkpoint(
            self.parents["9938"], actual_admission(self.parents["9938"], self.summary),
        )
        resume = native_parent_reference("9938", self.parents["9938"], original)
        canonical = native_parent_reference("66a", self.parents["66a"], original)
        reference = DERIVATION.canonical_parent_readback_reference(
            journal, journal_ref_sha="a" * 40, record_index=0,
        )
        envelope = DERIVATION.build_derivation_envelope(
            original_observation=original,
            resume_parent_processor=resume,
            canonical_parent_processor=canonical,
            canonical_parent_readback=reference,
            composition_baseline={
                "main_sha": "6a5138c792f4b7402da0c5ab439646bd752a307f",
                "manifest_sha256": reference["manifest_sha256"],
                "registry_path": reference["registry_path"],
                "registry_sha256": reference["registry_sha256"],
                "registry_bytes": reference["registry_bytes"],
            },
            derivation_processor_revision_sha256="b" * 64,
        )
        self.assertEqual(reference["canonical_producer_generation_id"], self.summary["merged_c"]["generation_id"])
        self.assertEqual(reference["pr_number"], 686)
        self.assertEqual(reference["pr_head_sha"], "8b086826fb1e30ea949cfb8222a252e7aa09fa40")
        self.assertEqual(reference["merge_sha"], "6a5138c792f4b7402da0c5ab439646bd752a307f")
        self.assertEqual(reference["merge_ack_run_id"], 37101245239)
        self.assertEqual(reference["merge_ack_run_attempt"], 12)
        self.assertEqual(reference["manifest_sha256"], "71a5ad4716ef5847210e2aeb8513ec136619e9521e757af106fdb3d048c33e50")
        self.assertEqual(reference["registry_sha256"], self.summary["canonical_payload"]["sha256"])
        self.assertIs(DERIVATION.validate_readback_against_journal(
            envelope, journal, journal_ref_sha="a" * 40,
        ), row)

        # No sibling generation, PR body edit, or substituted C attempt can be
        # treated as the exact selected canonical merge.
        mutated = copy.deepcopy(row)
        mutated["candidate"]["generation_id"] = "f" * 64
        with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_journal_snapshot_changed"):
            DERIVATION.validate_readback_against_journal(
                envelope, {"records": [mutated]}, journal_ref_sha="a" * 40,
            )

    def test_preparer_parent_identity_core_rejects_row_or_source_substitution(self) -> None:
        journal = native_c_journal()
        row = journal["records"][0]
        candidate = row["candidate"]
        readback = DERIVATION.canonical_parent_readback_reference(
            journal, journal_ref_sha="a" * 40, record_index=0,
        )
        checkpoint = {
            "generation_id": candidate["generation_id"],
            "source_id": candidate["source_id"],
            "source_scope": candidate["scope"],
        }
        context = {"identity": {
            "registry_path": candidate["registry_path"],
            "registry_bytes": candidate["registry_bytes"],
            "registry_sha256": candidate["registry_sha256"],
        }}
        generation_id, validated = PREPARATION.validate_canonical_parent_row_identity(
            "StatPan/datapan-registry", checkpoint, row, readback, context,
        )
        self.assertEqual(generation_id, candidate["generation_id"])
        self.assertEqual(validated, candidate)

        wrong_source = copy.deepcopy(context)
        wrong_source["identity"]["registry_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "canonical_parent_selected_row_identity_mismatch"):
            PREPARATION.validate_canonical_parent_row_identity(
                "StatPan/datapan-registry", checkpoint, row, readback, wrong_source,
            )
        wrong_row = copy.deepcopy(row)
        wrong_row["ownership"]["expected_head_sha"] = "e" * 40
        with self.assertRaisesRegex(ValueError, "canonical_parent_selected_row_identity_mismatch"):
            PREPARATION.validate_canonical_parent_row_identity(
                "StatPan/datapan-registry", checkpoint, wrong_row, readback, context,
            )

    def test_original_parent_relation_keeps_p_base_merge_and_replay_m_distinct(self) -> None:
        # Pure relation regression only: native capability authenticity and
        # actual ancestry are established by the Native resolver, not by this
        # fixture. A historical PR may have been based on an earlier commit;
        # its own PR base, exact merge SHA, and replay M are three bindings.
        p_base = "1" * 40
        p_merge = "2" * 40
        replay_m = "3" * 40
        generation_id = "4" * 64
        checkpoint_sha = "5" * 64
        candidate = {"base_sha": p_base, "generation_id": generation_id}
        row = {"candidate": copy.deepcopy(candidate), "pr": {"merge_commit_sha": p_merge}}
        checkpoint = {"generation_id": generation_id, "checkpoint_sha256": checkpoint_sha}
        base = {"commit_sha": replay_m, "tree_sha": "6" * 40}
        checkpoint_identity = {
            "generation_id": generation_id, "checkpoint_sha256": checkpoint_sha,
        }
        context = {"identity": {"main_sha": replay_m}}
        readback = {"merge_sha": p_merge}
        native_pr = {"base": {"sha": p_base}}

        PREPARATION._validate_original_native_parent_identity(
            "StatPan/datapan-registry", checkpoint, context,
            base=base, checkpoint_identity=checkpoint_identity,
            candidate=candidate, native_pr=native_pr, row=row, readback=readback,
            verified_repository="StatPan/datapan-registry", merge_is_ancestor=True,
        )
        self.assertNotEqual(p_base, replay_m)
        self.assertNotEqual(p_merge, replay_m)

        wrong_pr_base = {"base": {"sha": replay_m}}
        with self.assertRaisesRegex(ValueError, "canonical_parent_native_facts_identity_mismatch"):
            PREPARATION._validate_original_native_parent_identity(
                "StatPan/datapan-registry", checkpoint, context,
                base=base, checkpoint_identity=checkpoint_identity,
                candidate=candidate, native_pr=wrong_pr_base, row=row, readback=readback,
                verified_repository="StatPan/datapan-registry", merge_is_ancestor=True,
            )

        wrong_replay_m = {"identity": {"main_sha": p_merge}}
        with self.assertRaisesRegex(ValueError, "canonical_parent_native_facts_identity_mismatch"):
            PREPARATION._validate_original_native_parent_identity(
                "StatPan/datapan-registry", checkpoint, wrong_replay_m,
                base=base, checkpoint_identity=checkpoint_identity,
                candidate=candidate, native_pr=native_pr, row=row, readback=readback,
                verified_repository="StatPan/datapan-registry", merge_is_ancestor=True,
            )

        with self.assertRaisesRegex(ValueError, "canonical_parent_native_facts_identity_mismatch"):
            PREPARATION._validate_original_native_parent_identity(
                "StatPan/datapan-registry", checkpoint, context,
                base=base, checkpoint_identity=checkpoint_identity,
                candidate=candidate, native_pr=native_pr, row=row, readback=readback,
                verified_repository="StatPan/datapan-registry", merge_is_ancestor=False,
            )

    def test_original_native_parent_adapter_rejects_unsealed_input_before_local_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical_parent_native_facts_validator_unavailable"):
            PREPARATION.authenticate_original_native_canonical_parent(
                "StatPan/datapan-registry", {}, {}, {}, parent_facts={},
                native_module=object(), derivation=object(), health=object(),
                now=dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc),
            )

    def test_exact_c_merge_ack_accepts_authenticated_in_job_emission_with_bounded_order(self) -> None:
        journal = native_c_journal()
        row = journal["records"][0]
        ack = next(item for item in row["acknowledgements"] if item["status"] == "merged")
        run_id, attempt = str(ack["run_id"]), int(ack["run_attempt"])
        workflow_path = ".github/workflows/canonical-update-promotion.yml"
        workflow_id = 912345
        # These are the retained PR686 readback identities/times. The run head
        # is the workflow_run source, while the ACK source_sha is the merge SHA.
        head = "dc79e3931d6cfbbe5b19917487d356f88d0682f0"
        evidence_template = {
            "attempt_number": attempt,
            "jobs_api_endpoint": f"repos/StatPan/datapan-registry/actions/runs/{run_id}/attempts/{attempt}/jobs",
            "job_count": 1,
            "run": {
                "id": int(run_id), "run_attempt": attempt, "workflow_id": workflow_id,
                "path": workflow_path, "event": "workflow_run", "head_branch": "main",
                "head_sha": head, "status": "completed", "conclusion": "success",
                "run_started_at": "2026-10-03T14:52:04Z", "completed_at": "2026-10-03T14:53:12Z",
                "repository": {"full_name": "StatPan/datapan-registry"},
                "head_repository": {"full_name": "StatPan/datapan-registry"},
            },
            "jobs": [{
                "id": 111226407734, "run_id": int(run_id), "run_attempt": attempt,
                "head_sha": head, "status": "completed", "conclusion": "success",
                "started_at": "2026-10-03T14:52:08Z", "completed_at": "2026-10-03T14:53:12Z",
            }],
        }
        default_now = dt.datetime(2026, 10, 5, 0, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory(prefix="same-a-c-ack-") as temp:
            scripts = pathlib.Path(temp) / "scripts"
            scripts.mkdir()
            (scripts / "canonical_update_pr.py").write_bytes((ROOT / "scripts/canonical_update_pr.py").read_bytes())
            shutil.copy2(
                ROOT / "scripts/canonical_update_terminal_evidence.py",
                scripts / "canonical_update_terminal_evidence.py",
            )
            checker = scripts / "check-upstream-catalogue-health.py"
            health_source = (ROOT / "scripts/check-upstream-catalogue-health.py").read_text(encoding="utf-8")

            def authenticate(
                *, observed_at: str | None = None, jobs: list[dict[str, Any]] | None = None,
                now: dt.datetime = default_now, skew: Any = 300,
                row_override: dict[str, Any] | None = None,
                run_changes: dict[str, Any] | None = None,
                collectors_fail: bool = False,
            ) -> dict[str, Any]:
                selected_row = copy.deepcopy(row_override if row_override is not None else row)
                selected_ack = next(item for item in selected_row["acknowledgements"] if item["status"] == "merged")
                if observed_at is not None:
                    selected_ack["observed_at"] = observed_at
                evidence = copy.deepcopy(evidence_template)
                if jobs is not None:
                    evidence["jobs"] = copy.deepcopy(jobs)
                    evidence["job_count"] = len(jobs)
                if run_changes:
                    evidence["run"].update(run_changes)
                if collectors_fail:
                    collector_definitions = (
                        "def collect_workflow_identity(repository, workflow_path):\n"
                        "    raise AssertionError('native workflow read must not run')\n"
                        "def collect_run_attempt_evidence(repository, run_id, run_attempt):\n"
                        "    raise AssertionError('native attempt read must not run')\n"
                    )
                else:
                    collector_definitions = (
                        "def collect_workflow_identity(repository, workflow_path):\n    return 912345\n"
                        "def collect_run_attempt_evidence(repository, run_id, run_attempt):\n"
                        "    return TEST_RUN_EVIDENCE.get(f'{run_id}/{run_attempt}', {'availability_error': True})\n"
                    )
                checker.write_text(
                    health_source
                    + "\n\n# Test-only exact-attempt API fixture; no network is used.\n"
                    + f"TEST_RUN_EVIDENCE = {pprint.pformat({f'{run_id}/{attempt}': evidence})}\n"
                    + collector_definitions,
                    encoding="utf-8",
                )
                policy = {
                    "promotion_state": {"promotion_workflow_path": workflow_path},
                    "clock": {"maximum_future_skew_seconds": skew},
                }
                return DERIVATION.authenticate_canonical_merge_ack(
                    pathlib.Path(temp), "StatPan/datapan-registry", selected_row, policy, now=now,
                )

            trusted = authenticate()
            self.assertEqual(trusted["run_id"], run_id)
            self.assertEqual(trusted["run_attempt"], attempt)
            self.assertEqual(trusted["jobs_completed_at"], "2026-10-03T14:53:12Z")
            self.assertEqual(trusted["observed_at"], "2026-10-03T14:52:44.916636+00:00")

            # Exact equality at both ends of the successful emitting job is
            # admissible, while policy skew applies inclusively around it.
            self.assertEqual(authenticate(observed_at="2026-10-03T14:52:08Z", skew=0)["observed_at"], "2026-10-03T14:52:08Z")
            self.assertEqual(authenticate(observed_at="2026-10-03T14:53:12Z", skew=0)["observed_at"], "2026-10-03T14:53:12Z")
            self.assertEqual(authenticate(observed_at="2026-10-03T14:52:03Z", skew=5)["observed_at"], "2026-10-03T14:52:03Z")

            # Keep a later successful job in the exact attempt so the upper
            # in-job bound and an uncovered inter-job gap are both observable
            # before the all-job finality timestamp.
            later_job = {
                "id": 111226407735, "run_id": int(run_id), "run_attempt": attempt,
                "head_sha": head, "status": "completed", "conclusion": "success",
                "started_at": "2026-10-03T14:53:30Z", "completed_at": "2026-10-03T14:54:00Z",
            }
            later_jobs = copy.deepcopy(evidence_template["jobs"]) + [later_job]
            self.assertEqual(
                authenticate(
                    observed_at="2026-10-03T14:53:17Z", jobs=later_jobs, skew=5,
                    now=dt.datetime(2026, 10, 3, 14, 54, tzinfo=dt.timezone.utc),
                    run_changes={"completed_at": "2026-10-03T14:54:00Z"},
                )["observed_at"],
                "2026-10-03T14:53:17Z",
            )
            with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_order_invalid"):
                authenticate(observed_at="2026-10-03T14:52:02.999999Z", skew=5)
            with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_order_invalid"):
                authenticate(
                    observed_at="2026-10-03T14:53:17.000001Z", jobs=later_jobs, skew=5,
                    now=dt.datetime(2026, 10, 3, 14, 54, tzinfo=dt.timezone.utc),
                    run_changes={"completed_at": "2026-10-03T14:54:00Z"},
                )

            # The compatible old branch still accepts observations at/after
            # the maximum completion of every job in the attempt.
            self.assertEqual(
                authenticate(
                    observed_at="2026-10-03T14:54:00Z", jobs=later_jobs, skew=0,
                    now=dt.datetime(2026, 10, 3, 14, 54, tzinfo=dt.timezone.utc),
                    run_changes={"completed_at": "2026-10-03T14:54:00Z"},
                )["jobs_completed_at"],
                "2026-10-03T14:54:00Z",
            )
            self.assertEqual(
                authenticate(
                    observed_at="2026-10-03T14:54:05Z", jobs=later_jobs, skew=5,
                    now=dt.datetime(2026, 10, 3, 14, 54, tzinfo=dt.timezone.utc),
                    run_changes={"completed_at": "2026-10-03T14:54:00Z"},
                )["observed_at"],
                "2026-10-03T14:54:05Z",
            )
            with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_order_invalid"):
                authenticate(
                    observed_at="2026-10-03T14:54:05.000001Z", jobs=later_jobs, skew=5,
                    now=dt.datetime(2026, 10, 3, 14, 54, tzinfo=dt.timezone.utc),
                    run_changes={"completed_at": "2026-10-03T14:54:00Z"},
                )

            # Missing/reversed intervals and skipped jobs cannot establish an
            # in-job emission window. They do not remove the old after-complete
            # branch when its independent temporal condition is met.
            missing_start = copy.deepcopy(evidence_template["jobs"])
            missing_start[0].pop("started_at")
            with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_order_invalid"):
                authenticate(jobs=missing_start)
            naive_start = copy.deepcopy(evidence_template["jobs"])
            naive_start[0]["started_at"] = "2026-10-03T14:52:08"
            with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_order_invalid"):
                authenticate(jobs=naive_start)
            reversed_interval = copy.deepcopy(evidence_template["jobs"])
            reversed_interval[0]["started_at"] = "2026-10-03T14:53:13Z"
            with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_order_invalid"):
                authenticate(jobs=reversed_interval)
            skipped_only = copy.deepcopy(evidence_template["jobs"])
            skipped_only[0]["conclusion"] = "skipped"
            with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_order_invalid"):
                authenticate(jobs=skipped_only)
            self.assertEqual(
                authenticate(
                    observed_at="2026-10-03T14:53:13Z", jobs=missing_start,
                )["observed_at"],
                "2026-10-03T14:53:13Z",
            )
            fractional_completion = copy.deepcopy(evidence_template["jobs"])
            fractional_completion[0]["conclusion"] = "skipped"
            fractional_completion[0]["completed_at"] = "2026-10-03T14:53:12.999999Z"
            self.assertEqual(
                authenticate(
                    observed_at="2026-10-03T14:53:12.999999Z", jobs=fractional_completion,
                    run_changes={"completed_at": "2026-10-03T14:53:12.999999Z"},
                )["jobs_completed_at"],
                "2026-10-03T14:53:12Z",
            )
            with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_order_invalid"):
                authenticate(
                    observed_at="2026-10-03T14:53:12.500000Z", jobs=fractional_completion,
                    run_changes={"completed_at": "2026-10-03T14:53:12.999999Z"},
                )

            for bad_skew in (True, -1, 3601, 1.5, float("inf"), float("-inf"), float("nan")):
                with self.subTest(bad_skew=bad_skew), self.assertRaisesRegex(
                    DERIVATION.DerivationError, "canonical_parent_merge_ack_clock_invalid",
                ):
                    authenticate(skew=bad_skew, collectors_fail=True)
            with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_clock_invalid"):
                authenticate(now=dt.datetime(2026, 10, 5), collectors_fail=True)
            with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_clock_invalid"):
                authenticate(observed_at="2026-10-03T14:52:44.916636", collectors_fail=False)

            for run_changes, job_changes in (
                ({"repository": {"full_name": "Other/repository"}}, None),
                ({"path": ".github/workflows/other.yml"}, None),
                ({"event": "push"}, None),
                ({"status": "in_progress", "conclusion": None}, None),
                ({"status": "failure", "conclusion": "failure"}, None),
                ({}, {"head_sha": "e" * 40}),
                ({}, {"conclusion": "failure"}),
            ):
                bad_jobs = copy.deepcopy(evidence_template["jobs"])
                if job_changes:
                    bad_jobs[0].update(job_changes)
                with self.subTest(run_changes=run_changes, job_changes=job_changes), self.assertRaisesRegex(
                    DERIVATION.DerivationError, "canonical_parent_merge_ack_run_untrusted",
                ):
                    authenticate(jobs=bad_jobs, run_changes=run_changes)

            wrong_attempt = copy.deepcopy(row)
            selected = next(item for item in wrong_attempt["acknowledgements"] if item["status"] == "merged")
            selected["run_attempt"] = attempt + 1
            with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_run_untrusted"):
                authenticate(row_override=wrong_attempt)

    def test_live_canonical_parent_preparation_admits_in_job_ack_before_next_step(self) -> None:
        journal = native_c_journal()
        row = journal["records"][0]
        readback = DERIVATION.canonical_parent_readback_reference(
            journal, journal_ref_sha="a" * 40, record_index=0,
        )
        candidate = row["candidate"]
        checkpoint = {
            "generation_id": candidate["generation_id"],
            "source_id": candidate["source_id"],
            "source_scope": candidate["scope"],
        }
        canonical_context = {"identity": {
            "registry_path": candidate["registry_path"],
            "registry_bytes": candidate["registry_bytes"],
            "registry_sha256": candidate["registry_sha256"],
            "main_sha": "968a76d04e73016db7433018ddc4e27e9f3b2dcb",
        }}
        run_id, attempt = "37101245239", 12
        workflow_path = ".github/workflows/canonical-update-promotion.yml"
        head = "dc79e3931d6cfbbe5b19917487d356f88d0682f0"
        evidence = {
            "attempt_number": attempt,
            "jobs_api_endpoint": f"repos/StatPan/datapan-registry/actions/runs/{run_id}/attempts/{attempt}/jobs",
            "job_count": 1,
            "run": {
                "id": int(run_id), "run_attempt": attempt, "workflow_id": 912345,
                "path": workflow_path, "event": "workflow_run", "head_branch": "main",
                "head_sha": head, "status": "completed", "conclusion": "success",
                "run_started_at": "2026-10-03T14:52:04Z", "completed_at": "2026-10-03T14:53:12Z",
                "repository": {"full_name": "StatPan/datapan-registry"},
                "head_repository": {"full_name": "StatPan/datapan-registry"},
            },
            "jobs": [{
                "id": 111226407734, "run_id": int(run_id), "run_attempt": attempt,
                "head_sha": head, "status": "completed", "conclusion": "success",
                "started_at": "2026-10-03T14:52:08Z", "completed_at": "2026-10-03T14:53:12Z",
            }],
        }
        policy = {
            "promotion_state": {"promotion_workflow_path": workflow_path},
            "clock": {"maximum_future_skew_seconds": 300},
        }

        class ReadOnlyRunner:
            def __init__(self, selected_readback: Mapping[str, Any]) -> None:
                self.selected_readback = selected_readback
                self.calls: list[str] = []

            def load_object(self, _path: pathlib.Path) -> dict[str, Any]:
                return policy

            def gh_pr_readback(self, _root: pathlib.Path, _repository: str, _number: int) -> dict[str, Any]:
                self.calls.append("read_pr")
                return {
                    "number": self.selected_readback["pr_number"],
                    "state": "MERGED",
                    "repository": "StatPan/datapan-registry",
                    "headRepository": "StatPan/datapan-registry",
                    "headRefName": self.selected_readback["pr_branch"],
                    "headRefOid": self.selected_readback["pr_head_sha"],
                    "baseRefName": "main",
                    "mergeCommit": {"oid": self.selected_readback["merge_sha"]},
                    "merged": True,
                    "body": row["ownership"]["body"],
                }

        with tempfile.TemporaryDirectory(prefix="same-a-c-preparation-ack-") as temp:
            root = pathlib.Path(temp)
            scripts = root / "scripts"
            scripts.mkdir()
            (root / ".git").symlink_to(ROOT / ".git", target_is_directory=True)
            (scripts / "canonical_update_pr.py").write_bytes((ROOT / "scripts/canonical_update_pr.py").read_bytes())
            shutil.copy2(
                ROOT / "scripts/canonical_update_terminal_evidence.py",
                scripts / "canonical_update_terminal_evidence.py",
            )
            checker = scripts / "check-upstream-catalogue-health.py"

            def install_offline_native_reads(selected_evidence: dict[str, Any]) -> None:
                checker.write_text(
                    (ROOT / "scripts/check-upstream-catalogue-health.py").read_text(encoding="utf-8")
                    + "\n\n# Test-only exact-attempt API fixture; no network is used.\n"
                    + f"TEST_RUN_EVIDENCE = {pprint.pformat({f'{run_id}/{attempt}': selected_evidence})}\n"
                    + "def collect_workflow_identity(repository, workflow_path):\n    return 912345\n"
                    + "def collect_run_attempt_evidence(repository, run_id, run_attempt):\n"
                    + "    return TEST_RUN_EVIDENCE.get(f'{run_id}/{run_attempt}', {'availability_error': True})\n",
                    encoding="utf-8",
                )

            install_offline_native_reads(evidence)
            runner = ReadOnlyRunner(readback)
            authorized = PREPARATION.authenticate_live_canonical_parent(
                root, "StatPan/datapan-registry", "main", checkpoint, row, readback,
                canonical_context, runner, DERIVATION,
                now=dt.datetime(2026, 10, 5, 0, 0, tzinfo=dt.timezone.utc),
            )
            self.assertEqual(authorized.generation_id, candidate["generation_id"])
            self.assertEqual(authorized.readback["merge_ack_observed_at"], "2026-10-03T14:52:44.916636+00:00")
            self.assertEqual(runner.calls, ["read_pr"])

            # The same real preparation gate rejects an out-of-window ACK
            # before it attempts the subsequent local ancestry step. The only
            # preceding operation is the exact read-only PR identity check.
            bad_journal = copy.deepcopy(journal)
            bad_ack = next(item for item in bad_journal["records"][0]["acknowledgements"] if item["status"] == "merged")
            bad_ack["observed_at"] = "2026-10-03T14:52:02.999999Z"
            bad_readback = DERIVATION.canonical_parent_readback_reference(
                bad_journal, journal_ref_sha="a" * 40, record_index=0,
            )
            policy["clock"]["maximum_future_skew_seconds"] = 5
            install_offline_native_reads(evidence)
            negative_runner = ReadOnlyRunner(bad_readback)
            with mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0)) as ancestry:
                with self.assertRaisesRegex(DERIVATION.DerivationError, "canonical_parent_merge_ack_order_invalid"):
                    PREPARATION.authenticate_live_canonical_parent(
                        root, "StatPan/datapan-registry", "main", checkpoint,
                        bad_journal["records"][0], bad_readback, canonical_context,
                        negative_runner, DERIVATION,
                        now=dt.datetime(2026, 10, 5, 0, 0, tzinfo=dt.timezone.utc),
                    )
                ancestry.assert_not_called()
            self.assertEqual(negative_runner.calls, ["read_pr"])

    def test_same_payload_from_a_different_original_observation_is_not_a_parent(self) -> None:
        summary = read_json(FIXTURES / "native-lineage-identities.json")
        parents = {
            name: read_json(FIXTURES / f"native-b-{name}.json")
            for name in ("66a", "9938")
        }
        original = DERIVATION.original_observation_from_checkpoint(
            parents["9938"], actual_admission(parents["9938"], summary),
        )
        resume = native_parent_reference("9938", parents["9938"], original)
        canonical = native_parent_reference("66a", parents["66a"], original)
        journal = native_c_journal()
        readback = DERIVATION.canonical_parent_readback_reference(
            journal, journal_ref_sha="a" * 40, record_index=0,
        )
        baseline = {
            "main_sha": "6a5138c792f4b7402da0c5ab439646bd752a307f",
            "manifest_sha256": readback["manifest_sha256"],
            "registry_path": readback["registry_path"],
            "registry_sha256": readback["registry_sha256"],
            "registry_bytes": readback["registry_bytes"],
        }
        envelope = DERIVATION.build_derivation_envelope(
            original_observation=original,
            resume_parent_processor=resume,
            canonical_parent_processor=canonical,
            canonical_parent_readback=readback,
            composition_baseline=baseline,
            derivation_processor_revision_sha256="b" * 64,
        )
        # The canonical payload and C row remain byte-identical. Changing only
        # the claimed A candidate must still fail because neither B parent was
        # admitted for that source observation.
        forged = copy.deepcopy(envelope)
        forged["original_observation"]["candidate_sha256"] = "f" * 64
        with self.assertRaisesRegex(DERIVATION.DerivationError, "resume_parent_original_observation_mismatch"):
            DERIVATION.validate_derivation_envelope(forged)

    def test_recursive_parent_graph_rejects_a_sealed_compound_cycle(self) -> None:
        summary = read_json(FIXTURES / "native-lineage-identities.json")
        parents = {
            name: read_json(FIXTURES / f"native-b-{name}.json")
            for name in ("66a", "9938")
        }
        original = DERIVATION.original_observation_from_checkpoint(
            parents["9938"], actual_admission(parents["9938"], summary),
        )
        resume = native_parent_reference("9938", parents["9938"], original)
        canonical = native_parent_reference("66a", parents["66a"], original)
        journal = native_c_journal()
        readback = DERIVATION.canonical_parent_readback_reference(
            journal, journal_ref_sha="a" * 40, record_index=0,
        )
        baseline = {
            "main_sha": "6a5138c792f4b7402da0c5ab439646bd752a307f",
            "manifest_sha256": readback["manifest_sha256"],
            "registry_path": readback["registry_path"],
            "registry_sha256": readback["registry_sha256"],
            "registry_bytes": readback["registry_bytes"],
        }
        cycle_id = "f" * 64
        resume["generation_id"] = cycle_id
        envelope = DERIVATION.build_derivation_envelope(
            original_observation=original,
            resume_parent_processor=resume,
            canonical_parent_processor=canonical,
            canonical_parent_readback=readback,
            composition_baseline=baseline,
            derivation_processor_revision_sha256="b" * 64,
        )
        derived = copy.deepcopy(parents["9938"])
        derived["generation_id"] = cycle_id
        derived["generation_inputs"] = dict(derived["generation_inputs"])
        derived["generation_inputs"]["same_observation_derivation"] = envelope
        PROCESSOR.seal_checkpoint(derived)
        by_id = {cycle_id: derived, parents["66a"]["generation_id"]: parents["66a"], parents["9938"]["generation_id"]: parents["9938"]}

        with self.assertRaisesRegex(DERIVATION.DerivationError, "derivation_parent_graph_cycle"):
            DERIVATION.validate_processor_parent_graph(
                [cycle_id],
                load_checkpoint=lambda generation_id: by_id[generation_id],
                admission_for=lambda checkpoint: actual_admission(checkpoint, summary),
                original_observation=original,
            )

    def test_parent_graph_rejects_a_two_generation_compound_cycle(self) -> None:
        summary = self.summary
        original = DERIVATION.original_observation_from_checkpoint(
            self.parents["9938"], actual_admission(self.parents["9938"], summary),
        )
        resume_template = native_parent_reference("9938", self.parents["9938"], original)
        canonical = native_parent_reference("66a", self.parents["66a"], original)
        journal = native_c_journal()
        readback = DERIVATION.canonical_parent_readback_reference(
            journal, journal_ref_sha="a" * 40, record_index=0,
        )
        baseline = {
            "main_sha": "6a5138c792f4b7402da0c5ab439646bd752a307f",
            "manifest_sha256": readback["manifest_sha256"],
            "registry_path": readback["registry_path"],
            "registry_sha256": readback["registry_sha256"],
            "registry_bytes": readback["registry_bytes"],
        }
        generation_a, generation_b = "a" * 64, "b" * 64
        resume_for_a = copy.deepcopy(resume_template)
        resume_for_a["generation_id"] = generation_b
        resume_for_b = copy.deepcopy(resume_template)
        resume_for_b["generation_id"] = generation_a
        envelope_a = DERIVATION.build_derivation_envelope(
            original_observation=original,
            resume_parent_processor=resume_for_a,
            canonical_parent_processor=canonical,
            canonical_parent_readback=readback,
            composition_baseline=baseline,
            derivation_processor_revision_sha256="c" * 64,
            resume_parent_ancestors=[generation_a],
        )
        envelope_b = DERIVATION.build_derivation_envelope(
            original_observation=original,
            resume_parent_processor=resume_for_b,
            canonical_parent_processor=canonical,
            canonical_parent_readback=readback,
            composition_baseline=baseline,
            derivation_processor_revision_sha256="d" * 64,
            resume_parent_ancestors=[generation_b],
        )
        checkpoint_a = copy.deepcopy(self.parents["9938"])
        checkpoint_a["generation_id"] = generation_a
        checkpoint_a["generation_inputs"] = dict(checkpoint_a["generation_inputs"])
        checkpoint_a["generation_inputs"]["same_observation_derivation"] = envelope_a
        checkpoint_b = copy.deepcopy(self.parents["9938"])
        checkpoint_b["generation_id"] = generation_b
        checkpoint_b["generation_inputs"] = dict(checkpoint_b["generation_inputs"])
        checkpoint_b["generation_inputs"]["same_observation_derivation"] = envelope_b
        by_id = {
            generation_a: checkpoint_a,
            generation_b: checkpoint_b,
            self.parents["66a"]["generation_id"]: self.parents["66a"],
        }
        with self.assertRaisesRegex(DERIVATION.DerivationError, "derivation_parent_graph_cycle"):
            DERIVATION.validate_processor_parent_graph(
                [generation_a], load_checkpoint=lambda generation_id: by_id[generation_id],
                admission_for=lambda checkpoint: actual_admission(checkpoint, summary),
                original_observation=original,
            )

    def test_parent_graph_enforces_visit_cap_before_loading_excess_nodes(self) -> None:
        summary = self.summary
        original = DERIVATION.original_observation_from_checkpoint(
            self.parents["9938"], actual_admission(self.parents["9938"], summary),
        )
        resume_template = native_parent_reference("9938", self.parents["9938"], original)
        canonical = native_parent_reference("66a", self.parents["66a"], original)
        journal = native_c_journal()
        readback = DERIVATION.canonical_parent_readback_reference(
            journal, journal_ref_sha="a" * 40, record_index=0,
        )
        baseline = {
            "main_sha": "6a5138c792f4b7402da0c5ab439646bd752a307f",
            "manifest_sha256": readback["manifest_sha256"],
            "registry_path": readback["registry_path"],
            "registry_sha256": readback["registry_sha256"],
            "registry_bytes": readback["registry_bytes"],
        }
        native_leaf_id = self.parents["9938"]["generation_id"]
        canonical_leaf_id = self.parents["66a"]["generation_id"]
        by_id = {
            native_leaf_id: self.parents["9938"],
            canonical_leaf_id: self.parents["66a"],
        }
        previous_id = native_leaf_id
        previous_closure = {native_leaf_id}
        for number in range(1, 64):
            current_id = hashlib.sha256(f"bounded-derivation-{number}".encode()).hexdigest()
            resume = copy.deepcopy(resume_template)
            resume["generation_id"] = previous_id
            envelope = DERIVATION.build_derivation_envelope(
                original_observation=original,
                resume_parent_processor=resume,
                canonical_parent_processor=canonical,
                canonical_parent_readback=readback,
                composition_baseline=baseline,
                derivation_processor_revision_sha256=hashlib.sha256(str(number).encode()).hexdigest(),
                resume_parent_ancestors=sorted(previous_closure - {previous_id}),
            )
            checkpoint = copy.deepcopy(self.parents["9938"])
            checkpoint["generation_id"] = current_id
            checkpoint["generation_inputs"] = dict(checkpoint["generation_inputs"])
            checkpoint["generation_inputs"]["same_observation_derivation"] = envelope
            by_id[current_id] = checkpoint
            previous_closure = set(envelope["ancestor_generation_ids"]) | {current_id}
            previous_id = current_id

        loaded: list[str] = []

        def load_checkpoint(generation_id: str) -> Mapping[str, Any]:
            loaded.append(generation_id)
            return by_id[generation_id]

        with self.assertRaisesRegex(DERIVATION.DerivationError, "derivation_parent_graph_too_large"):
            DERIVATION.validate_processor_parent_graph(
                [previous_id], load_checkpoint=load_checkpoint,
                admission_for=lambda checkpoint: actual_admission(checkpoint, summary),
                original_observation=original,
            )
        self.assertEqual(len(loaded), 64)
        self.assertEqual(len(set(loaded)), 64)


class SameObservationProcessorFlowTests(unittest.TestCase):
    """Run the real B claim/worker/composer path on a labelled synthetic A."""

    def setUp(self) -> None:
        self.helper = PROCESSOR_TESTS.UpstreamCatalogueProcessorTest()
        self.helper.setUp()
        self.now = dt.datetime.now(timezone.utc).replace(microsecond=0)
        self.now_text = self.now.isoformat().replace("+00:00", "Z")
        self.observation_run = "99000000001"
        self.observation_artifact = "99000000002"
        self.observation_head = "a" * 40
        self.artifact_expiry = "2026-11-04T00:00:00Z"
        self.output_expiry = "2026-11-05T00:00:00Z"
        self.output_by_generation: dict[str, pathlib.Path] = {}
        self.checkpoints: dict[str, dict[str, Any]] = {}
        self.current_manifests: dict[str, bytes] = {}
        self.claim_runs: list[str] = []
        self.detail_calls: list[str] = []
        self.operation_endpoint_overrides: dict[str, str] = {}
        self.cache_source_archives: dict[str, bytes] = {}
        self.cache_source_snapshots: dict[str, dict[str, bytes]] = {}
        self.generator_sources_by_revision: dict[str, dict[str, bytes]] = {}
        self.extractor_sources_by_revision: dict[str, bytes] = {}
        (self.helper.root / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
        (self.helper.root / "schemas").symlink_to(ROOT / "schemas", target_is_directory=True)
        (self.helper.root / "policy").mkdir(exist_ok=True)
        shutil.copy2(ROOT / "policy/upstream-catalogue-health.json", self.helper.root / "policy/upstream-catalogue-health.json")
        self._write_synthetic_observation()
        self.admission_path, self.archive_path = self._write_admission()
        current_sources = self._generator_source_files()
        current_revision = self._register_generator_sources(current_sources)
        self.assertEqual(current_revision, PROCESSOR.generator_revision())
        alternate_sources = dict(current_sources)
        alternate_sources["scripts/process-upstream-catalogue-candidate.py"] += (
            b"\n# Test-only source-epoch marker; behavior is unchanged.\n"
        )
        self.alternate_generator_revision = self._register_generator_sources(alternate_sources)
        current_extractor = (ROOT / "scripts/generate-batch-link-detail-registry-patches.py").read_bytes()
        self.extractor_sources_by_revision[hashlib.sha256(current_extractor).hexdigest()] = current_extractor
        alternate_extractor = current_extractor + b"\n# Test-only extractor-epoch marker.\n"
        self.alternate_extractor_revision = hashlib.sha256(alternate_extractor).hexdigest()
        self.extractor_sources_by_revision[self.alternate_extractor_revision] = alternate_extractor

    def tearDown(self) -> None:
        self.helper.tearDown()

    def test_source_work_rebuild_uses_committed_state_snapshot_in_the_real_preparer(self) -> None:
        self.observation_head = "2693e4de28e601555b998a457816f2ad18321699"
        self.admission_path, self.archive_path = self._write_admission()
        target = self._run_claim_and_worker(1)
        journal, canonical = self._synthetic_c_journal(target, 1)

        state_repository = self.helper.root / "source-state-repository"
        state_repository.mkdir()
        state_root = state_repository / ".datapan/upstream-catalogue-state"
        shutil.copytree(self.helper.state_dir, state_root)

        def git_state(*argv: str) -> str:
            result = subprocess.run(
                ("git", *argv), cwd=state_repository, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        git_state("init", "-q")
        git_state("config", "user.name", "fixture")
        git_state("config", "user.email", "fixture@example.test")
        git_state("remote", "add", "origin", "https://github.com/StatPan/datapan-registry.git")
        git_state("add", ".datapan/upstream-catalogue-state")
        git_state("commit", "-q", "-m", "test-only committed processor state")
        selection_state_sha = git_state("rev-parse", "HEAD")
        current_source_sha = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.strip()
        current_source_tree = subprocess.run(
            ("git", "rev-parse", f"{current_source_sha}^{{tree}}"), cwd=ROOT,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.strip()
        # These three inputs are known outstanding C/#767 joins in the current
        # candidate. Synthetic bytes let the offline fixture exercise B's
        # caller and identity bindings without claiming their implementation.
        source_contract_git_overrides = {
            "schemas/datapan.canonical-update-promotion-terminal-outcome.v2.schema.json": (
                b'{"$schema":"https://json-schema.org/draft/2020-12/schema",'
                b'"type":"object","test_only":true}\n'
            ),
            "scripts/canonical_current_target_evidence.py": (
                b"# test-only placeholder for a pending joined C source input\n"
            ),
            "scripts/canonical_source_target_disposition.py": (
                b"# test-only placeholder for a pending joined C source input\n"
            ),
        }
        original_eligibility_git = ELIGIBILITY._git

        def source_contract_git(
            root: pathlib.Path, *git_args: str, binary: bool = False,
        ) -> bytes | str:
            if len(git_args) >= 2 and git_args[0] == "show" and ":" in git_args[1]:
                revision, path = git_args[1].split(":", 1)
                payload = source_contract_git_overrides.get(path)
                if revision == current_source_sha and payload is not None:
                    return payload if binary else payload.decode("utf-8").strip()
            return original_eligibility_git(root, *git_args, binary=binary)

        evaluation = {
            "source_sha": current_source_sha,
            "source_tree_sha": current_source_tree,
            "event": "schedule", "run_id": "99500000001", "run_attempt": 1,
            "as_of": self.now_text,
        }
        with mock.patch.object(ELIGIBILITY, "_git", side_effect=source_contract_git):
            request = ELIGIBILITY.source_work_request_from_state(
                root=ROOT, state_dir=state_root, repository="StatPan/datapan-registry",
                source_id="data_go_kr", source_scope="aggregate_supported_catalog",
                evaluation=evaluation, selection_state_sha=selection_state_sha,
            )
        self.assertEqual(request["action"], "rebuild")
        self.assertEqual(request["selected_generation_id"], target["generation_id"])
        self.assertEqual(request["selection_state_sha"], selection_state_sha)
        self.assertEqual(request["target_contract"]["changed_classes"], [ELIGIBILITY.SAME_A_PROCESSING])

        committed_index = (state_root / "sources/data_go_kr/index.json").read_bytes()
        committed_checkpoint = (
            state_root / "sources/data_go_kr/generations" / f"{target['generation_id']}.json"
        ).read_bytes()
        plan = self._prepare_authenticated_plan(
            target, journal, canonical, label="source-work-real-preparer",
            current_main_sha=current_source_sha,
            source_work_request=request, source_state_dir=state_root,
            source_contract_git_overrides=source_contract_git_overrides,
        )
        self.assertTrue(plan["eligible"], plan)
        self.assertEqual(plan["reason"], "same_a_source_contract_rebuild_authenticated")
        epoch = read_json(pathlib.Path(plan["source_epoch_path"]))
        contract = read_json(pathlib.Path(plan["source_target_contract_path"]))
        eligibility = read_json(pathlib.Path(plan["source_eligibility_path"]))
        self.assertEqual(epoch["predecessor_generation_id"], target["generation_id"])
        self.assertEqual(epoch["target_generation_id"], plan["generation_id"])
        self.assertEqual(contract, request["target_contract"])
        self.assertEqual(eligibility["action"], "rebuild")
        self.assertEqual(eligibility["reason_code"], request["reason_code"])
        self.assertEqual((state_root / "sources/data_go_kr/index.json").read_bytes(), committed_index)
        self.assertEqual(
            (state_root / "sources/data_go_kr/generations" / f"{target['generation_id']}.json").read_bytes(),
            committed_checkpoint,
        )
        self.assertEqual(git_state("rev-parse", "HEAD"), selection_state_sha)
        self.assertEqual(git_state("status", "--porcelain=v1", "--untracked-files=all"), "")

        forged_selection = copy.deepcopy(request)
        forged_selection["selection_state_sha"] = "1" * 40
        with self.assertRaisesRegex(ValueError, "source_work_request_selection_state_revision_mismatch"):
            self._prepare_authenticated_plan(
                target, journal, canonical, label="source-work-selection-revision-mismatch",
                current_main_sha=current_source_sha,
                source_work_request=forged_selection, source_state_dir=state_root,
                source_contract_git_overrides=source_contract_git_overrides,
            )

        index_path = state_root / "sources/data_go_kr/index.json"
        index_path.write_bytes(committed_index + b" ")
        try:
            with self.assertRaisesRegex(ValueError, "source eligibility state checkout is dirty"):
                self._prepare_authenticated_plan(
                    target, journal, canonical, label="source-work-dirty-state-rejected",
                    current_main_sha=current_source_sha,
                    source_work_request=request, source_state_dir=state_root,
                    source_contract_git_overrides=source_contract_git_overrides,
                )
        finally:
            index_path.write_bytes(committed_index)
        self.assertEqual(git_state("status", "--porcelain=v1", "--untracked-files=all"), "")

    def _write_synthetic_observation(self, identities: list[str] | None = None) -> None:
        rows = []
        selected_ids = identities if identities is not None else [str(number) for number in range(1, 5)]
        for identity in selected_ids:
            row = self.helper.real_link_row()
            identity = str(identity)
            row["id"] = identity
            row["title"] = f"Test-only synthetic LINK {identity}"
            row["source"]["url"] = f"https://www.data.go.kr/data/{identity}/openapi.do"
            row["source"]["raw"].update({
                "api_id": str(identity), "meta_url": row["source"]["url"],
                "title": row["title"],
            })
            rows.append(row)
        self.helper.write_real_composer_inputs([], rows, self.now_text)

    def _write_admission(self) -> tuple[pathlib.Path, pathlib.Path]:
        archive_path = self.helper.root / "synthetic-observation.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(self.helper.candidate_path, "candidate.registry.json")
            archive.write(self.helper.evidence_path, "upstream-refresh-evidence.json")
            archive.write(self.helper.diff_path, "catalog-diff.json")
        archive_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        archive_size = archive_path.stat().st_size
        started = (self.now - dt.timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
        completed = (self.now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        value = {
            "schema_version": "datapan.upstream-catalogue-admission-envelope.v1",
            "repository": "StatPan/datapan-registry",
            "producer_run_id": self.observation_run,
            "run_attempt": 1,
            "head_sha": self.observation_head,
            "run_started_at": started,
            "run_completed_at": completed,
            "observe_job_started_at": started,
            "observe_job_completed_at": completed,
            "artifact_id": self.observation_artifact,
            "artifact_name": f"upstream-catalog-refresh-{self.observation_run}",
            "artifact_expires_at": self.artifact_expiry,
            "artifact_created_at": (self.now - dt.timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
            "artifact_digest_sha256": archive_digest,
            "artifact_size_bytes": archive_size,
            "archive_sha256": archive_digest,
            "archive_size_bytes": archive_size,
            "observed_at": self.now_text,
            "refresh_evidence_sha256": hashlib.sha256(self.helper.evidence_path.read_bytes()).hexdigest(),
            "event": "schedule",
        }
        self.admission_metadata = {
            "repository": value["repository"],
            "producer_run_id": value["producer_run_id"],
            "run_attempt": value["run_attempt"],
            "head_sha": value["head_sha"],
            "run_started_at": value["run_started_at"],
            "run_completed_at": value["run_completed_at"],
            "observe_job_started_at": value["observe_job_started_at"],
            "observe_job_completed_at": value["observe_job_completed_at"],
            "artifact_id": value["artifact_id"],
            "artifact_name": value["artifact_name"],
            "artifact_expires_at": value["artifact_expires_at"],
            "artifact_created_at": value["artifact_created_at"],
            "artifact_digest_sha256": value["artifact_digest_sha256"],
            "artifact_size_bytes": value["artifact_size_bytes"],
            "event": value["event"],
        }
        path = self.helper.root / "collector-admission.json"
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return path, archive_path

    @staticmethod
    def _generator_source_files() -> dict[str, bytes]:
        paths = (
            "scripts/process-upstream-catalogue-candidate.py",
            "scripts/upstream_catalogue_handoff.py",
            "scripts/upstream_catalogue_cache_source.py",
            "scripts/seoul_oa109_operation_declaration.py",
            "scripts/generate-seoul-oa109-subject-snapshot.py",
            "contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json",
            "contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json",
        )
        return {path: (ROOT / path).read_bytes() for path in paths}

    def _register_generator_sources(self, sources: dict[str, bytes]) -> str:
        fields = (
            "processor_script_sha256", "collector_handoff_helper_sha256",
            "cache_source_helper_sha256", "seoul_operation_declaration_helper_sha256",
            "seoul_historical_subject_snapshot_generator_sha256",
            "seoul_operation_declaration_sha256", "seoul_historical_subject_snapshot_sha256",
        )
        revision = PROCESSOR.sha256_bytes(PROCESSOR.canonical_json({
            field: hashlib.sha256(raw).hexdigest()
            for field, raw in zip(fields, sources.values(), strict=True)
        }))
        self.generator_sources_by_revision[revision] = dict(sources)
        return revision

    def _cache_source_snapshot(self, checkpoint: Mapping[str, Any]) -> tuple[str, dict[str, bytes]]:
        inputs = checkpoint["generation_inputs"]
        generator_revision = str(inputs["generator_revision"])
        extractor_revision = str(inputs["extractor_revision"])
        if generator_revision not in self.generator_sources_by_revision:
            raise AssertionError(f"unregistered fixture generator revision: {generator_revision}")
        if extractor_revision not in self.extractor_sources_by_revision:
            raise AssertionError(f"unregistered fixture extractor revision: {extractor_revision}")
        head_sha = hashlib.sha1(PROCESSOR.canonical_json(inputs)).hexdigest()
        snapshot = self.cache_source_snapshots.get(head_sha)
        if snapshot is None:
            snapshot = {
                path: (ROOT / path).read_bytes()
                for path in dict.fromkeys(PROMOTION.PROCESSOR_COMPATIBILITY_FILES)
            }
            snapshot.update(self.generator_sources_by_revision[generator_revision])
            snapshot["scripts/generate-batch-link-detail-registry-patches.py"] = (
                self.extractor_sources_by_revision[extractor_revision]
            )
            self.cache_source_snapshots[head_sha] = snapshot
        return head_sha, snapshot

    def _cache_source_archive(self, checkpoint: Mapping[str, Any]) -> bytes:
        artifact_id = str(checkpoint["output_artifact"]["artifact_id"])
        cached = self.cache_source_archives.get(artifact_id)
        if cached is not None:
            return cached
        bundle = self.output_by_generation[str(checkpoint["generation_id"])]
        output = io.BytesIO()
        names = [row["path"] for row in checkpoint["output_digests"]]
        names.append("upstream-catalogue-checkpoint-receipt.json")
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                info = zipfile.ZipInfo(name, date_time=(2026, 10, 7, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, (bundle / name).read_bytes())
        raw = output.getvalue()
        self.cache_source_archives[artifact_id] = raw
        return raw

    def _cache_source_archive_path(self, checkpoint: Mapping[str, Any]) -> pathlib.Path:
        bundle = self.output_by_generation[str(checkpoint["generation_id"])]
        archive_path = bundle.with_suffix(".zip")
        raw = self._cache_source_archive(checkpoint)
        if archive_path.exists():
            self.assertEqual(archive_path.read_bytes(), raw)
        else:
            archive_path.write_bytes(raw)
        return archive_path

    def _checkpoint_for_bundle(self, bundle: pathlib.Path) -> dict[str, Any]:
        matches = [
            self.checkpoints[generation_id]
            for generation_id, candidate in self.output_by_generation.items()
            if candidate == bundle
        ]
        self.assertEqual(len(matches), 1, str(bundle))
        return matches[0]

    def _cache_source_reader(self):
        owner = self

        class Reader:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def checkpoint_for(self, *, run_id: str | None = None, artifact_id: str | None = None):
                matches = []
                for checkpoint in owner.checkpoints.values():
                    locator = checkpoint.get("output_artifact", {})
                    if run_id is not None and str(locator.get("run_id")) != run_id:
                        continue
                    if artifact_id is not None and str(locator.get("artifact_id")) != artifact_id:
                        continue
                    matches.append(checkpoint)
                if len(matches) != 1:
                    raise AssertionError(
                        f"fixture cache owner lookup is not unique: run={run_id}, artifact={artifact_id}"
                    )
                return matches[0]

            def metadata(self, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
                locator = checkpoint["output_artifact"]
                raw = owner._cache_source_archive(checkpoint)
                head_sha, _snapshot = owner._cache_source_snapshot(checkpoint)
                return {
                    "id": int(locator["artifact_id"]), "name": locator["name"],
                    "size_in_bytes": len(raw),
                    "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    "expired": False,
                    "created_at": "2026-10-07T00:00:00Z",
                    "updated_at": "2026-10-07T00:00:01Z",
                    "expires_at": locator["expires_at"],
                    "workflow_run": {
                        "id": int(locator["run_id"]), "head_sha": head_sha,
                        "head_branch": "main", "repository_id": 1,
                        "head_repository_id": 1,
                    },
                }

            def json(self, endpoint: str):
                self.calls.append(("json", endpoint))
                if "/actions/runs/" in endpoint and endpoint.endswith("/artifacts?per_page=100"):
                    run_id = endpoint.split("/actions/runs/", 1)[1].split("/", 1)[0]
                    checkpoint = self.checkpoint_for(run_id=run_id)
                    return {"total_count": 1, "artifacts": [self.metadata(checkpoint)]}
                if "/actions/artifacts/" in endpoint:
                    artifact_id = endpoint.rsplit("/", 1)[1]
                    return self.metadata(self.checkpoint_for(artifact_id=artifact_id))
                if "/actions/runs/" in endpoint and "/attempts/" in endpoint:
                    run_id = endpoint.split("/actions/runs/", 1)[1].split("/", 1)[0]
                    attempt = int(endpoint.rsplit("/", 1)[1])
                    checkpoint = self.checkpoint_for(run_id=run_id)
                    locator = checkpoint["output_artifact"]
                    expected_attempt = int(str(locator["name"]).rsplit("-", 1)[1])
                    if attempt != expected_attempt:
                        raise AssertionError("fixture cache owner attempt differs")
                    head_sha, _snapshot = owner._cache_source_snapshot(checkpoint)
                    return {
                        "id": int(run_id), "run_attempt": attempt,
                        "path": ".github/workflows/upstream-catalogue-process.yml",
                        "head_branch": "main", "head_sha": head_sha,
                        "status": "completed", "conclusion": "success", "event": "schedule",
                        "repository": {"id": 1, "full_name": "StatPan/datapan-registry"},
                        "head_repository": {"id": 1, "full_name": "StatPan/datapan-registry"},
                    }
                raise AssertionError(f"unexpected cache-source fixture endpoint: {endpoint}")

            def archive(self, repository: str, artifact_id: str, *, expected_bytes: int) -> bytes:
                self.calls.append(("archive", artifact_id))
                if repository != "StatPan/datapan-registry":
                    raise AssertionError("unexpected cache-source fixture repository")
                raw = owner._cache_source_archive(self.checkpoint_for(artifact_id=artifact_id))
                if len(raw) != expected_bytes:
                    raise AssertionError("fixture cache archive size differs")
                return raw

            def source_file(
                self, repository: str, head_sha: str, path: str, *, maximum_bytes: int,
            ) -> bytes:
                self.calls.append(("source_file", path))
                if repository != "StatPan/datapan-registry":
                    raise AssertionError("unexpected cache-source fixture repository")
                raw = owner.cache_source_snapshots[head_sha][path]
                if not 0 < len(raw) <= maximum_bytes:
                    raise AssertionError("fixture cache source size differs")
                return raw

        return Reader()

    def _authenticated_cache_source_contract(
        self, checkpoint: Mapping[str, Any],
    ) -> tuple[Any, Any]:
        helper = PROCESSOR.CACHE_SOURCE
        contract = helper.authenticate_owner_contract(
            checkpoint, repository="StatPan/datapan-registry", default_branch="main",
            reader=self._cache_source_reader(),
        )
        return helper, contract

    def _args(
        self, processor_attempt: int, *, generator_revision: str | None = None,
        max_attempts: int = 1, max_queue: int = 1, retries_per_detail: int = 2,
    ) -> argparse.Namespace:
        processor_artifact_run = str(99000000010 + processor_attempt)
        args = self.helper.args(run_id=self.observation_run, **{
            "--execution-mode": "live",
            "--processor-run-id": f"{processor_artifact_run}-1",
            "--processor-artifact-run-id": processor_artifact_run,
            "--producer-head-sha": self.observation_head,
            "--producer-run-attempt": self.admission_metadata["run_attempt"],
            "--producer-run-started-at": self.admission_metadata["run_started_at"],
            "--producer-run-completed-at": self.admission_metadata["run_completed_at"],
            "--producer-observe-job-started-at": self.admission_metadata["observe_job_started_at"],
            "--producer-observe-job-completed-at": self.admission_metadata["observe_job_completed_at"],
            "--producer-artifact-created-at": self.admission_metadata["artifact_created_at"],
            "--producer-artifact-digest-sha256": self.admission_metadata["artifact_digest_sha256"],
            "--producer-artifact-size-bytes": self.admission_metadata["artifact_size_bytes"],
            "--producer-event": self.admission_metadata["event"],
            "--artifact-name": f"upstream-catalog-refresh-{self.observation_run}",
            "--input-artifact-id": self.observation_artifact,
            "--artifact-expires-at": self.artifact_expiry,
            "--output-artifact-expires-at": self.output_expiry,
            "--collector-admission-file": self.admission_path,
            "--collector-archive": self.archive_path,
            "--max-attempts": max_attempts,
            "--max-queue": max_queue,
            "--retries-per-detail": retries_per_detail,
            "--composer": ROOT / "scripts/compose-upstream-catalogue-candidate.py",
            "--state-dir": self.helper.state_dir,
        })
        args.fixture_composer = None
        args.allow_fixture_composer = False
        args.now = None
        if generator_revision is not None:
            # Model a later processor revision without changing any A input.
            # The old parent's revision is preserved in its exact checkpoint.
            args.test_generator_revision = generator_revision
        return args

    def _fetch(self, url: str, _timeout: float) -> Any:
        self.detail_calls.append(url)
        identity = url.split("/data/", 1)[1].split("/", 1)[0]
        body = (
            f'<a href="https://www.data.go.kr/guide/{identity}.pdf">API Guide</a>'
            f'<a href="{self.operation_endpoint_overrides.get(identity, f"https://openapi.airport.co.kr/test/{identity}")}" '
            'onclick="fn_LinkApiRequest()">API</a>'
        ).encode("utf-8")
        return PROCESSOR.DetailPageObservation(
            body=body.decode("utf-8"), page_bytes=body, page_url=url, effective_url=url,
            page_sha256=PROCESSOR.sha256_bytes(body), observed_at=self.now_text,
        )

    def _run_claim_and_worker(
        self, attempt: int, *, resume: pathlib.Path | None = None,
        derivation: dict[str, Any] | None = None,
        expected_generation_id: str | None = None,
        claim_only_return: bool = False,
        generator_revision: str | None = None,
        composition_baseline: pathlib.Path | None = None,
        resume_parent_bundle: pathlib.Path | None = None,
        canonical_parent_bundle: pathlib.Path | None = None,
        journal: dict[str, Any] | None = None,
        journal_ref_sha: str | None = None,
        max_attempts: int = 1,
        max_queue: int = 1,
        retries_per_detail: int = 2,
        fetcher: Any | None = None,
        resolver_fetcher: Any | None = None,
        expected_worker_code: int = 0,
        expected_worker_status: str = "ready",
        expected_claim_code: int = 0,
        expected_claim_status: str = "enriching",
        current_main_sha: str | None = None,
        expected_claim_error: str | None = None,
        extractor_revision_value: str | None = None,
    ) -> dict[str, Any]:
        args = self._args(
            attempt, generator_revision=generator_revision,
            max_attempts=max_attempts, max_queue=max_queue,
            retries_per_detail=retries_per_detail,
        )
        args.expected_generation_id = expected_generation_id
        if resume is not None:
            args.resume_enrichment_evidence = resume
            resume_owner = self._checkpoint_for_bundle(resume.parent)
            args.resume_source_context = resume.parent / PROCESSOR.CACHE_SOURCE.CONTEXT_MEMBER
            args.resume_owner_archive = self._cache_source_archive_path(resume_owner)
        if derivation is not None:
            envelope_path = self.helper.root / f"derivation-{attempt}.json"
            envelope_path.write_bytes(DERIVATION.canonical_json(derivation) + b"\n")
            baseline_path = self.helper.root / f"composition-baseline-{attempt}.json"
            baseline_path.write_bytes(composition_baseline.read_bytes())
            journal_path = self.helper.root / f"journal-{attempt}.json"
            journal_path.write_text(json.dumps(journal, sort_keys=True), encoding="utf-8")
            args.same_observation_derivation = envelope_path
            args.composition_baseline = baseline_path
            args.current_main_root = self.helper.root
            args.derivation_journal = journal_path
            args.derivation_journal_ref_sha = journal_ref_sha
            args.resume_parent_bundle_dir = resume_parent_bundle
            args.canonical_parent_bundle_dir = canonical_parent_bundle
            args.resume_parent_archive = self._cache_source_archive_path(
                self._checkpoint_for_bundle(resume_parent_bundle),
            )
            args.canonical_parent_archive = self._cache_source_archive_path(
                self._checkpoint_for_bundle(canonical_parent_bundle),
            )
            args.resume_enrichment_evidence = resume_parent_bundle / "upstream-catalogue-enrichment-evidence.json"
            args.resume_source_context = resume_parent_bundle / PROCESSOR.CACHE_SOURCE.CONTEXT_MEMBER
            args.resume_owner_archive = args.resume_parent_archive
            args.canonical_update_pr_helper = ROOT / "scripts/canonical_update_pr.py"
            args.canonical_update_journal_schema = ROOT / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json"

        processor_run = str(args.processor_run_id)
        self.claim_runs.append(processor_run)
        worker_fetcher = fetcher or self._fetch
        cache_source_reader = self._cache_source_reader()
        fixed_clock = lambda: self.now
        revision_patch = (
            mock.patch.object(PROCESSOR, "generator_revision", return_value=args.test_generator_revision)
            if hasattr(args, "test_generator_revision")
            else mock.patch.object(PROCESSOR, "generator_revision", wraps=PROCESSOR.generator_revision)
        )
        extractor_patch = (
            mock.patch.object(PROCESSOR, "extractor_revision", return_value=extractor_revision_value)
            if extractor_revision_value is not None
            else mock.patch.object(PROCESSOR, "extractor_revision", wraps=PROCESSOR.extractor_revision)
        )
        with revision_patch, extractor_patch, self._derivation_git(
            snapshot=journal, journal_ref_sha=journal_ref_sha, derivation=derivation,
            current_main_sha=current_main_sha,
        ):
            args.claim_only = True
            if expected_claim_error is not None:
                with self.assertRaisesRegex(ValueError, expected_claim_error):
                    PROCESSOR.process(
                        args, fetcher=worker_fetcher, resolver_fetcher=resolver_fetcher,
                        sleeper=lambda _delay: None, clock=fixed_clock,
                        cache_source_reader=cache_source_reader,
                    )
                return {}
            claim_code, claim = PROCESSOR.process(
                args, fetcher=worker_fetcher, resolver_fetcher=resolver_fetcher,
                sleeper=lambda _delay: None, clock=fixed_clock,
                cache_source_reader=cache_source_reader,
            )
            self.assertEqual(claim_code, expected_claim_code, json.dumps(claim, sort_keys=True))
            self.assertEqual(claim["status"], expected_claim_status)
            if claim_code != 0:
                return claim
            if claim_only_return:
                return claim
            args.claim_only = False
            args.require_durable_reservation = True
            if derivation is None:
                worker_code, worker = PROCESSOR.process(
                    args, fetcher=worker_fetcher, resolver_fetcher=resolver_fetcher,
                    sleeper=lambda _delay: None, clock=fixed_clock,
                    cache_source_reader=cache_source_reader,
                )
            else:
                worker_code, worker = PROCESSOR.process(
                    args, fetcher=worker_fetcher, resolver_fetcher=resolver_fetcher,
                    sleeper=lambda _delay: None, clock=fixed_clock,
                    cache_source_reader=cache_source_reader,
                )
        self.assertEqual(worker_code, expected_worker_code, worker.get("outcome"))
        self.assertEqual(worker["status"], expected_worker_status)
        self.assertEqual(worker["observation_count"], 1)
        self.assertEqual(worker["observed_at"], self.now_text)
        checkpoint_path = self.helper.state_dir / "sources/data_go_kr/generations" / f"{worker['generation_id']}.json"
        PROCESSOR.bind_output_artifact_id(
            self.helper.state_dir, "data_go_kr", worker["generation_id"],
            str(99000000020 + attempt), ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
            processor_run_id=processor_run,
            processor_artifact_run_id=str(args.processor_artifact_run_id),
            artifact_expires_at=self.output_expiry,
        )
        bound = PROCESSOR.load_json(checkpoint_path, maximum_bytes=PROCESSOR.STATE_FILE_LIMIT)
        bundle = self.helper.root / f"bundle-{attempt}"
        if bundle.exists():
            shutil.rmtree(bundle)
        shutil.copytree(self.helper.output_dir, bundle)
        self.output_by_generation[bound["generation_id"]] = bundle
        self.checkpoints[bound["generation_id"]] = bound
        return bound

    @contextmanager
    def _derivation_git(
        self, *, snapshot: dict[str, Any] | None, journal_ref_sha: str | None,
        derivation: dict[str, Any] | None, current_main_sha: str | None = None,
    ):
        """Stub only git's remote/ref boundary; keep validator and local content checks real."""
        if derivation is None:
            yield
            return
        original_run = subprocess.run
        baseline = derivation["composition_baseline"]
        main_sha = current_main_sha or baseline["main_sha"]
        # Claim rejects a moved main before consuming its current manifest.
        # The exact content is therefore irrelevant in that negative case;
        # keep the old manifest available so the test fails on the intended
        # head guard rather than in the Git adapter.
        main_manifest = self.current_manifests.get(main_sha, self.current_manifests[baseline["main_sha"]])
        c_row = snapshot["records"][derivation["canonical_parent_readback"]["journal_record_index"]]
        c_ack = next(row for row in c_row["acknowledgements"] if row.get("status") == "merged")
        c_run_id = int(c_ack["run_id"])
        c_run_attempt = int(c_ack["run_attempt"])
        c_workflow_id = 912345
        c_completed_at = (self.now - dt.timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
        c_policy = read_json(ROOT / "policy/upstream-catalogue-health.json")
        c_run = {
            "id": c_run_id, "run_attempt": c_run_attempt, "workflow_id": c_workflow_id,
            "path": c_policy["promotion_state"]["promotion_workflow_path"],
            "event": "workflow_run", "head_branch": "main", "head_sha": c_ack["source_sha"],
            "status": "completed", "conclusion": "success", "completed_at": c_completed_at,
            "repository": {"full_name": "StatPan/datapan-registry"},
            "head_repository": {"full_name": "StatPan/datapan-registry"},
        }
        c_job = {
            "id": 912346, "run_id": c_run_id, "run_attempt": c_run_attempt,
            "head_sha": c_ack["source_sha"], "status": "completed", "conclusion": "success",
            "completed_at": c_completed_at,
        }

        def fake_run(argv, *args, **kwargs):
            command = tuple(str(item) for item in argv)
            if command[:2] == ("gh", "api"):
                endpoint = command[2]
                if endpoint.endswith("/actions/workflows/canonical-update-promotion.yml"):
                    value: Any = {
                        "id": c_workflow_id,
                        "path": c_policy["promotion_state"]["promotion_workflow_path"],
                    }
                elif endpoint == f"repos/StatPan/datapan-registry/actions/runs/{c_run_id}/attempts/{c_run_attempt}":
                    value = c_run
                elif endpoint.startswith(f"repos/StatPan/datapan-registry/actions/runs/{c_run_id}/attempts/{c_run_attempt}/jobs?"):
                    value = {"total_count": 1, "jobs": [c_job]}
                else:
                    raise AssertionError(f"unexpected exact C trust endpoint: {endpoint}")
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(value), stderr="")
            if command[:2] == ("git", "ls-remote"):
                ref = command[-1]
                sha = journal_ref_sha if ref.endswith("canonical-update-state") else main_sha
                result = subprocess.CompletedProcess(argv, 0, stdout=f"{sha}\t{ref}\n", stderr="")
                return result
            if command[:3] == ("git", "rev-parse", "HEAD"):
                return subprocess.CompletedProcess(argv, 0, stdout=f"{main_sha}\n", stderr="")
            if len(command) == 3 and command[:2] == ("git", "show"):
                revision = command[2]
                if revision.endswith(":manifest.json"):
                    observed_main = revision.split(":", 1)[0]
                    data = self.current_manifests.get(observed_main, main_manifest)
                elif revision.endswith(":data/data-go-kr.registry.json"):
                    data = (
                        f"version https://git-lfs.github.com/spec/v1\n"
                        f"oid sha256:{baseline['registry_sha256']}\n"
                        f"size {baseline['registry_bytes']}\n"
                    ).encode("ascii")
                else:
                    return original_run(argv, *args, **kwargs)
                if kwargs.get("text"):
                    data = data.decode("utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout=data, stderr="")
            if command[:2] == ("git", "merge-base") and len(command) == 5 and command[2] == "--is-ancestor":
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            result = original_run(argv, *args, **kwargs)
            if len(command) > 1 and command[1].endswith("compose-upstream-catalogue-candidate.py") and result.returncode:
                raise AssertionError(f"composer failed: {result.stderr[-4000:]}")
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            yield

    def _admitted_original(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        index = PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")
        handoff = HANDOFF
        admission = next(
            row for row in handoff.validate_ledger(index["collector_handoff"])["admitted_observations"]
            if row["producer_run_id"] == self.observation_run
        )
        return DERIVATION.original_observation_from_checkpoint(checkpoint, admission)

    def _parent_ref(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        bundle = self.output_by_generation[checkpoint["generation_id"]]
        evidence_bytes = (bundle / "upstream-catalogue-enrichment-evidence.json").read_bytes()
        return DERIVATION.processor_parent_reference(
            checkpoint, repository="StatPan/datapan-registry",
            original_observation=self._admitted_original(checkpoint),
            enrichment_evidence=json.loads(evidence_bytes), enrichment_evidence_bytes=evidence_bytes,
        )

    def _synthetic_c_journal(self, checkpoint: dict[str, Any], number: int) -> tuple[dict[str, Any], dict[str, Any]]:
        bundle = self.output_by_generation[checkpoint["generation_id"]]
        payload = (bundle / "composed-candidate.registry.json").read_bytes()
        registry_sha = hashlib.sha256(payload).hexdigest()
        registry_path = DERIVATION.SAFE_PATH
        source_manifest = {
            "source_registry": registry_path,
            "artifacts": [{"kind": "registry", "path": registry_path, "bytes": len(payload), "sha256": registry_sha}],
        }
        merge_manifest_sha = DERIVATION.object_sha256(source_manifest)
        current_manifest = {**source_manifest, "test_only_source_commit": number}
        current_manifest_bytes = DERIVATION.canonical_json(current_manifest)
        current_manifest_sha = hashlib.sha256(current_manifest_bytes).hexdigest()
        merge_sha = f"{number:040x}"
        source_head = f"{number + 100:040x}"
        main_sha = f"{number + 300:040x}"
        journal_ref_sha = f"{number + 200:040x}"
        self.current_manifests[main_sha] = current_manifest_bytes
        pr_number = 9000 + number
        run_id = 99000001000 + number
        body = f"Synthetic test-only PR {pr_number}; no remote PR exists.\n"
        row = copy.deepcopy(read_json(FIXTURES / "native-merged-c-row.json")["journal_record"])
        row.pop("superseded_by", None)
        candidate = row["candidate"]
        candidate.update({
            "repository": "StatPan/datapan-registry", "source_id": "data_go_kr",
            "scope": "aggregate_supported_catalog", "generation_id": checkpoint["generation_id"],
            "head_sha": source_head, "manifest_sha256": merge_manifest_sha,
            "registry_path": DERIVATION.SAFE_PATH, "registry_bytes": len(payload),
            "registry_sha256": registry_sha,
            "composition_receipt_sha256": hashlib.sha256(
                (bundle / "composition-receipt.json").read_bytes(),
            ).hexdigest(),
        })
        row["ownership"].update({
            "branch": f"automation/canonical-update/synthetic-{number}",
            "expected_head_sha": source_head, "body": body,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        })
        row["pr"].update({"number": pr_number, "state": "merged", "merge_commit_sha": merge_sha})
        row["acknowledgements"] = [{
            "status": "merged", "observed_at": (self.now - dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "source_sha": merge_sha, "manifest_sha256": merge_manifest_sha,
            "run_id": run_id, "run_attempt": 1,
            "run_url": f"https://github.com/StatPan/datapan-registry/actions/runs/{run_id}/attempts/1",
            "evidence_reference": "test-only synthetic C merge acknowledgement",
            "artifact_identity": {"path": DERIVATION.SAFE_PATH, "bytes": len(payload), "sha256": registry_sha},
            "publication_revision": None, "publication_pointer_revision": None,
            "read_back_sha256": None, "read_back_bytes": None, "read_back_verified": False,
        }]
        journal = {
            "schema_version": "datapan.canonical-update-promotion-journal.v1",
            "repository": "StatPan/datapan-registry", "updated_at": self.now_text,
            "records": [row],
        }
        readback = DERIVATION.canonical_parent_readback_reference(
            journal, journal_ref_sha=journal_ref_sha, record_index=0,
        )
        baseline = {
            "main_sha": main_sha, "manifest_sha256": current_manifest_sha,
            "registry_path": DERIVATION.SAFE_PATH, "registry_sha256": registry_sha,
            "registry_bytes": len(payload),
        }
        return journal, {"readback": readback, "baseline": baseline, "payload": payload}

    def _prepare_authenticated_plan(
        self,
        target: dict[str, Any],
        journal: dict[str, Any],
        c: dict[str, Any],
        *,
        label: str,
        mutate_pr_readback: bool = False,
        current_main_sha: str | None = None,
        current_manifest_bytes: bytes | None = None,
        journal_unavailable: bool = False,
        current_identity_override: dict[str, Any] | None = None,
        admission_value_override: Any | None = None,
        authenticated_overrides: dict[str, Any] | None = None,
        source_checkout_head: str | None = None,
        omitted_authenticated_fields: set[str] | None = None,
        pr_cli_state: str = "MERGED",
        pr_rest_merged: Any = True,
        include_pr_rest_merged: bool = True,
        source_work_request: dict[str, Any] | None = None,
        source_state_dir: pathlib.Path | None = None,
        source_contract_git_overrides: dict[str, bytes] | None = None,
    ) -> dict[str, Any]:
        """Run the actual prepare CLI coordinator with remote boundaries stubbed."""
        main_root = self.helper.root / f"prepared-main-{label}"
        source_root = self.helper.root / f"prepared-source-{label}"
        output_root = self.helper.root / f"prepared-output-{label}"
        source_state_repository = (
            source_state_dir.parent.parent.resolve() if source_state_dir is not None else None
        )
        source_contract_git_overrides = source_contract_git_overrides or {}
        original_subprocess_run = subprocess.run
        main_root.mkdir(parents=True, exist_ok=True)
        source_root.mkdir(parents=True, exist_ok=True)
        for name in ("scripts", "schemas", "policy"):
            shutil.copytree(ROOT / name, main_root / name)
        # The production snapshot loader rejects paths escaping a checkout via
        # symlinked parents, so this test checkout needs the pinned source files
        # as ordinary local files rather than a contracts symlink.
        shutil.copytree(ROOT / "contracts", main_root / "contracts")
        (main_root / "data").mkdir()
        (main_root / "data/provider-index.json").write_bytes(self.helper.provider_index_path.read_bytes())

        baseline = c["baseline"]
        prepared_main_sha = current_main_sha or baseline["main_sha"]
        processor_head_sha, processor_source_snapshot = self._cache_source_snapshot(target)
        for relative, raw in processor_source_snapshot.items():
            path = main_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        payload = c["payload"]
        manifest_bytes = current_manifest_bytes or self.current_manifests[baseline["main_sha"]]
        self.current_manifests[prepared_main_sha] = manifest_bytes
        (main_root / "manifest.json").write_bytes(manifest_bytes)
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{baseline['registry_sha256']}\n"
            f"size {baseline['registry_bytes']}\n"
        ).encode("ascii")
        (main_root / "data/data-go-kr.registry.json").write_bytes(pointer)
        canonical_path = main_root / ".datapan/current-canonical/data/data-go-kr.registry.json"
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_bytes(payload)
        (source_root / "policy").mkdir()
        (source_root / "data").mkdir()
        (source_root / "policy/source-refresh.json").write_bytes(self.helper.policy_path.read_bytes())
        (source_root / "data/provider-index.json").write_bytes(self.helper.provider_index_path.read_bytes())
        (source_root / "data/data-go-kr.registry.json").write_bytes(self.helper.baseline_path.read_bytes())

        archives: dict[str, bytes] = {}
        checkpoints = dict(self.checkpoints)
        checkpoints.update({
            str(cp["generation_id"]): cp
            for cp in (target,)
        })
        for generation_id, checkpoint in checkpoints.items():
            bundle_dir = self.output_by_generation.get(generation_id)
            if bundle_dir is None:
                continue
            archives[str(checkpoint["output_artifact"]["artifact_id"])] = self._cache_source_archive(checkpoint)

        metadata_by_id: dict[str, dict[str, Any]] = {}
        run_by_id: dict[str, dict[str, Any]] = {}
        for checkpoint in checkpoints.values():
            locator = checkpoint["output_artifact"]
            if str(locator.get("artifact_id")) not in archives:
                continue
            run_id, attempt, _name = PROMOTION.processor_attempt_from_locator(checkpoint)
            head_sha, _snapshot = self._cache_source_snapshot(checkpoint)
            run_by_id[run_id] = {
                "id": int(run_id), "run_attempt": int(attempt),
                "name": PROMOTION.PROCESSOR_WORKFLOW_NAME,
                "path": PROMOTION.PROCESSOR_WORKFLOW_PATH,
                "repository": {"full_name": "StatPan/datapan-registry"},
                "head_repository": {"full_name": "StatPan/datapan-registry"},
                "head_branch": "main", "head_sha": head_sha,
                "event": "schedule", "status": "completed", "conclusion": "success",
            }
            raw_archive = archives[str(locator["artifact_id"])]
            metadata_by_id[str(locator["artifact_id"])] = {
                "id": int(locator["artifact_id"]), "name": locator["name"],
                "expired": False, "expires_at": locator["expires_at"],
                "size_in_bytes": len(raw_archive),
                "workflow_run": {"id": int(run_id), "head_sha": head_sha, "head_branch": "main"},
            }

        policy = json.loads((ROOT / "policy/upstream-catalogue-health.json").read_text(encoding="utf-8"))
        current_c = journal["records"][c["readback"]["journal_record_index"]]
        ack = next(row for row in current_c["acknowledgements"] if row.get("status") == "merged")
        c_run_id = int(ack["run_id"])
        c_run_attempt = int(ack["run_attempt"])
        c_workflow_id = 912345
        c_completed_at = (self.now - dt.timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
        c_run = {
            "id": c_run_id, "run_attempt": c_run_attempt,
            "workflow_id": c_workflow_id,
            "path": policy["promotion_state"]["promotion_workflow_path"],
            "event": "workflow_run", "head_branch": "main",
            "head_sha": ack["source_sha"], "status": "completed", "conclusion": "success",
            "completed_at": c_completed_at,
            "repository": {"full_name": "StatPan/datapan-registry"},
            "head_repository": {"full_name": "StatPan/datapan-registry"},
        }
        c_job = {
            "id": 912346, "run_id": c_run_id, "run_attempt": c_run_attempt,
            "head_sha": ack["source_sha"], "status": "completed", "conclusion": "success",
            "completed_at": c_completed_at,
        }

        def fake_health_subprocess(argv, *args, **kwargs):
            command = tuple(str(part) for part in argv)
            if (
                source_work_request is not None
                and command[:1] == ("git",)
                and pathlib.Path(kwargs.get("cwd", ".")).resolve() == ROOT.resolve()
            ):
                return original_subprocess_run(argv, *args, **kwargs)
            if (
                source_state_repository is not None
                and pathlib.Path(kwargs.get("cwd", ".")).resolve() == source_state_repository
            ):
                return original_subprocess_run(argv, *args, **kwargs)
            if command[:3] == ("git", "merge-base", "--is-ancestor"):
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if command == ("git", "ls-remote", "--heads", "origin", "refs/heads/main"):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=f"{prepared_main_sha}\trefs/heads/main\n", stderr="",
                )
            if command[:2] == ("gh", "api"):
                endpoint = command[2]
                if endpoint.endswith("/actions/workflows/canonical-update-promotion.yml"):
                    value: Any = {
                        "id": c_workflow_id,
                        "path": policy["promotion_state"]["promotion_workflow_path"],
                    }
                elif endpoint == f"repos/StatPan/datapan-registry/actions/runs/{c_run_id}/attempts/{c_run_attempt}":
                    value = c_run
                elif endpoint.startswith(f"repos/StatPan/datapan-registry/actions/runs/{c_run_id}/attempts/{c_run_attempt}/jobs?"):
                    value = {"total_count": 1, "jobs": [c_job]}
                else:
                    raise AssertionError(f"unexpected test GitHub API endpoint: {endpoint}")
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(value), stderr="")
            raise AssertionError(f"unexpected subprocess in preparation fixture: {command!r}")

        def fake_runner_command(argv, cwd, **_kwargs):
            command = tuple(str(part) for part in argv)
            if source_state_repository is not None and pathlib.Path(cwd).resolve() == source_state_repository:
                return original_subprocess_run(
                    argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=False,
                )
            if command == ("git", "rev-parse", "HEAD"):
                stdout = prepared_main_sha
            elif command == ("git", "ls-remote", "--heads", "origin", "refs/heads/main"):
                stdout = f"{prepared_main_sha}\trefs/heads/main\n"
            elif command[:3] == ("git", "merge-base", "--is-ancestor"):
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            elif command == ("git", "show", f"{prepared_main_sha}:manifest.json"):
                stdout = manifest_bytes.decode("utf-8")
            elif command == ("git", "show", f"{prepared_main_sha}:{DERIVATION.SAFE_PATH}"):
                stdout = pointer.decode("ascii")
            elif command[:2] == ("git", "show") and command[2].startswith(f"{processor_head_sha}:"):
                relative = command[2].split(":", 1)[1]
                stdout = processor_source_snapshot[relative].decode("utf-8")
            else:
                raise AssertionError(f"unexpected runner Git command in preparation fixture: {command!r}")
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        def authenticate_cache_source_fixture(
            _root: pathlib.Path, checkpoint: Mapping[str, Any], *,
            repository: str, default_branch: str, native_reader: Any | None = None,
            archive_path: pathlib.Path | None = None,
        ) -> tuple[Any, Any]:
            helper = PROCESSOR.CACHE_SOURCE
            reader = native_reader or self._cache_source_reader()
            contract = (
                helper.authenticate_owner_contract_from_archive_path(
                    checkpoint, repository=repository, default_branch=default_branch,
                    reader=reader, archive_path=archive_path,
                )
                if archive_path is not None
                else helper.authenticate_owner_contract(
                    checkpoint, repository=repository, default_branch=default_branch, reader=reader,
                )
            )
            return helper, contract

        def fake_preparation_git(root: pathlib.Path, *argv: str) -> str:
            if tuple(argv) == ("rev-parse", "HEAD") and root == source_root:
                return source_checkout_head or self.observation_head
            if tuple(argv) == ("rev-parse", "HEAD") and root == main_root:
                return prepared_main_sha
            if tuple(argv) == ("show", f"{self.observation_head}:policy/source-refresh.json"):
                return (source_root / "policy/source-refresh.json").read_text(encoding="utf-8")
            if tuple(argv) == ("show", f"{self.observation_head}:data/provider-index.json"):
                return (source_root / "data/provider-index.json").read_text(encoding="utf-8")
            raise AssertionError(f"unexpected preparation Git command: {root} {argv!r}")

        def fake_preparation_git_bytes(root: pathlib.Path, *argv: str) -> bytes:
            if tuple(argv) == ("show", f"{self.observation_head}:policy/source-refresh.json"):
                return (source_root / "policy/source-refresh.json").read_bytes()
            if tuple(argv) == ("show", f"{self.observation_head}:data/provider-index.json"):
                return (source_root / "data/provider-index.json").read_bytes()
            raise AssertionError(f"unexpected preparation Git byte command: {root} {argv!r}")

        module_by_filename = {
            "run-canonical-update-promotion.py": PROMOTION,
            "upstream_catalogue_handoff.py": HANDOFF,
            "upstream_catalogue_source_eligibility.py": ELIGIBILITY,
            "check-upstream-catalogue-health.py": load_script(
                "same_observation_health_for_plan", ROOT / "scripts/check-upstream-catalogue-health.py",
            ),
            "upstream_catalogue_derivation.py": DERIVATION,
            "process-upstream-catalogue-candidate.py": PROCESSOR,
        }
        index_path = self.helper.state_dir / "sources/data_go_kr/index.json"
        original_index = index_path.read_bytes()
        original_index_value = PROCESSOR.load_json(index_path)
        generations_dir = self.helper.state_dir / "sources/data_go_kr/generations"
        original_generation_paths = sorted(
            path.relative_to(generations_dir).as_posix()
            for path in generations_dir.rglob("*") if path.is_file()
        )
        original_detail_calls = list(self.detail_calls)
        original_admission_rows = HANDOFF.validate_ledger(
            original_index_value["collector_handoff"],
        )["admitted_observations"]
        original_admission = next(
            row for row in original_admission_rows
            if row["producer_run_id"] == self.observation_run
            and row["run_attempt"] == self.admission_metadata["run_attempt"]
        )
        reusable_bundle = self.output_by_generation.get(target["generation_id"], self.helper.root)
        reusable_archive = (
            self._cache_source_archive_path(target)
            if target["generation_id"] in self.output_by_generation
            else None
        )
        args = argparse.Namespace(
            main_root=main_root, source_root=source_root,
            state_dir=source_state_dir or self.helper.state_dir,
            target_generation_id=target["generation_id"], repository="StatPan/datapan-registry",
            default_branch="main", collector_admission=self.helper.root / f"admitted-a-{label}.json",
            collector_archive=self.archive_path,
            producer_run_url=f"https://github.com/StatPan/datapan-registry/actions/runs/{self.observation_run}",
            producer_run_id=self.observation_run,
            producer_run_attempt=self.admission_metadata["run_attempt"],
            producer_head_sha=self.admission_metadata["head_sha"],
            producer_run_started_at=self.admission_metadata["run_started_at"],
            producer_run_completed_at=self.admission_metadata["run_completed_at"],
            producer_observe_job_started_at=self.admission_metadata["observe_job_started_at"],
            producer_observe_job_completed_at=self.admission_metadata["observe_job_completed_at"],
            producer_artifact_id=self.admission_metadata["artifact_id"],
            producer_artifact_name=self.admission_metadata["artifact_name"],
            producer_artifact_expires_at=self.admission_metadata["artifact_expires_at"],
            producer_artifact_created_at=self.admission_metadata["artifact_created_at"],
            producer_artifact_digest_sha256=self.admission_metadata["artifact_digest_sha256"],
            producer_artifact_size_bytes=self.admission_metadata["artifact_size_bytes"],
            producer_event=self.admission_metadata["event"],
            candidate=self.helper.candidate_path, refresh_evidence=self.helper.evidence_path,
            diff=self.helper.diff_path,
            resume_bundle=reusable_bundle,
            resume_bundle_archive=reusable_archive,
            output_dir=output_root,
        )
        for field, value in (authenticated_overrides or {}).items():
            if not hasattr(args, field):
                raise AssertionError(f"unknown authenticated producer field: {field}")
            setattr(args, field, value)
        args.github_output = output_root / "github-output"
        preparation_argv = [
            str(PREPARATION.__file__),
            "--main-root", str(args.main_root), "--source-root", str(args.source_root),
            "--state-dir", str(args.state_dir), "--target-generation-id", str(args.target_generation_id),
            "--repository", args.repository, "--default-branch", args.default_branch,
            "--collector-admission", str(args.collector_admission),
            "--collector-archive", str(args.collector_archive),
            "--producer-run-url", args.producer_run_url,
            "--producer-run-id", args.producer_run_id,
            "--producer-run-attempt", str(args.producer_run_attempt),
            "--producer-head-sha", args.producer_head_sha,
            "--producer-run-started-at", args.producer_run_started_at,
            "--producer-run-completed-at", args.producer_run_completed_at,
            "--producer-observe-job-started-at", args.producer_observe_job_started_at,
            "--producer-observe-job-completed-at", args.producer_observe_job_completed_at,
            "--producer-artifact-id", args.producer_artifact_id,
            "--producer-artifact-name", args.producer_artifact_name,
            "--producer-artifact-expires-at", args.producer_artifact_expires_at,
            "--producer-artifact-created-at", args.producer_artifact_created_at,
            "--producer-artifact-digest-sha256", args.producer_artifact_digest_sha256,
            "--producer-artifact-size-bytes", str(args.producer_artifact_size_bytes),
            "--producer-event", args.producer_event,
            "--candidate", str(args.candidate), "--refresh-evidence", str(args.refresh_evidence),
            "--diff", str(args.diff), "--resume-bundle", str(args.resume_bundle),
            "--output-dir", str(args.output_dir), "--github-output", str(args.github_output),
        ]
        source_request_path = None
        if source_work_request is not None:
            source_request_path = self.helper.root / f"source-work-request-{label}.json"
            source_request_path.write_text(
                json.dumps(source_work_request, sort_keys=True), encoding="utf-8",
            )
            preparation_argv.extend(("--source-work-request", str(source_request_path)))
        if args.resume_bundle_archive is not None:
            preparation_argv.extend(("--resume-bundle-archive", str(args.resume_bundle_archive)))
        for field in omitted_authenticated_fields or set():
            position = preparation_argv.index(field)
            del preparation_argv[position:position + 2]
        if admission_value_override is None:
            args.collector_admission.write_bytes(self.admission_path.read_bytes())
        else:
            args.collector_admission.write_text(json.dumps(admission_value_override, sort_keys=True), encoding="utf-8")
        normalized_pr_readback = {
            "number": current_c["pr"]["number"], "url": current_c["pr"].get("url"),
            "state": pr_cli_state, "repository": "StatPan/datapan-registry",
            "headRepository": "StatPan/datapan-registry",
            "headRefName": current_c["ownership"]["branch"],
            "headRefOid": "f" * 40 if mutate_pr_readback else current_c["ownership"]["expected_head_sha"],
            "baseRefName": "main", "baseRefOid": prepared_main_sha,
            "mergeCommit": {"oid": current_c["pr"]["merge_commit_sha"]},
            "merged": pr_rest_merged, "body": current_c["ownership"]["body"],
        }
        cli_pr, rest_pr = primary_pr_transport(
            normalized_pr_readback,
            cli_state=pr_cli_state,
            rest_merged=pr_rest_merged,
            include_rest_merged=include_pr_rest_merged,
        )
        pr_cli_reads: list[int] = []
        pr_rest_reads: list[str] = []

        def pr_cli_transport(_root: pathlib.Path, *argv: str) -> dict[str, Any]:
            if argv[:2] != ("pr", "view"):
                raise AssertionError(f"unexpected PR CLI transport request: {argv!r}")
            pr_cli_reads.append(int(argv[2]))
            return copy.deepcopy(cli_pr)

        def pr_rest_transport(_root: pathlib.Path, endpoint: str, *argv: str) -> dict[str, Any]:
            expected = f"repos/StatPan/datapan-registry/pulls/{current_c['pr']['number']}"
            if endpoint != expected or argv != ("--method", "GET"):
                raise AssertionError(f"unexpected PR REST transport request: {(endpoint, *argv)!r}")
            pr_rest_reads.append(endpoint)
            return copy.deepcopy(rest_pr)

        journal_snapshot = (None, None) if journal_unavailable else (journal, c["readback"]["journal_ref_sha"])
        current_identity_patch = (
            mock.patch.object(PROMOTION, "authenticated_current_canonical_registry", return_value=current_identity_override)
            if current_identity_override is not None
            else mock.patch.object(PROMOTION, "authenticated_current_canonical_registry", wraps=PROMOTION.authenticated_current_canonical_registry)
        )
        canonical_context_calls: list[tuple[Any, ...]] = []
        original_canonical_context = PROMOTION.authenticated_current_canonical_context
        processor_bundle_head_calls: list[tuple[str, str]] = []
        original_validate_processor_bundle = PROMOTION.validate_processor_bundle

        def record_canonical_context(*context_args: Any, **context_kwargs: Any) -> Any:
            canonical_context_calls.append(context_args)
            return original_canonical_context(*context_args, **context_kwargs)

        def record_processor_bundle_head(*bundle_args: Any, **bundle_kwargs: Any) -> Any:
            checkpoint = bundle_args[0]
            run_id, _attempt, _name = PROMOTION.processor_attempt_from_locator(checkpoint)
            expected_head = str(run_by_id[run_id]["head_sha"])
            supplied_head = bundle_kwargs.get("producer_head_sha")
            self.assertEqual(supplied_head, expected_head)
            processor_bundle_head_calls.append((run_id, supplied_head))
            return original_validate_processor_bundle(*bundle_args, **bundle_kwargs)

        original_eligibility_git = ELIGIBILITY._git

        def source_request_git(root: pathlib.Path, *git_args: str, binary: bool = False) -> bytes | str:
            if source_work_request is not None and root.resolve() == main_root.resolve():
                root = ROOT
            if (
                source_work_request is not None
                and len(git_args) >= 2 and git_args[0] == "show"
                and ":" in git_args[1]
            ):
                revision, path = git_args[1].split(":", 1)
                payload = source_contract_git_overrides.get(path)
                if revision == source_work_request["evaluation"]["source_sha"] and payload is not None:
                    return payload if binary else payload.decode("utf-8").strip()
            return original_eligibility_git(root, *git_args, binary=binary)

        source_request_git_patch = (
            mock.patch.object(ELIGIBILITY, "_git", side_effect=source_request_git)
            if source_work_request is not None else contextlib.nullcontext()
        )

        with (
            mock.patch.object(PREPARATION, "load_module", side_effect=lambda path, _name: module_by_filename[path.name]),
            mock.patch.object(PREPARATION, "run_git", side_effect=fake_preparation_git),
            mock.patch.object(PREPARATION, "run_git_bytes", side_effect=fake_preparation_git_bytes),
            mock.patch.object(PREPARATION, "dt", mock.Mock(
                datetime=mock.Mock(now=mock.Mock(return_value=self.now)),
                timezone=dt.timezone,
            )),
            mock.patch.object(PROMOTION, "load_promotion_journal_snapshot", return_value=journal_snapshot),
            current_identity_patch,
            mock.patch.object(PROMOTION, "authenticated_current_canonical_context", side_effect=record_canonical_context),
            mock.patch.object(PROMOTION, "validate_processor_bundle", side_effect=record_processor_bundle_head),
            mock.patch.object(PROMOTION, "command", side_effect=fake_runner_command),
            mock.patch.object(
                PROMOTION, "authenticate_processor_cache_source_contract",
                side_effect=authenticate_cache_source_fixture,
            ),
            mock.patch.object(PROMOTION, "processor_run_api", side_effect=lambda _root, _repo, run_id, _attempt: run_by_id[run_id]),
            mock.patch.object(PROMOTION, "processor_artifact_api", side_effect=lambda _root, _repo, _run_id, artifact_id: metadata_by_id.get(artifact_id)),
            mock.patch.object(
                PROMOTION, "gh_rest_bytes",
                side_effect=lambda _root, endpoint: archives[endpoint.split("/actions/artifacts/", 1)[1].split("/", 1)[0]],
            ),
            mock.patch.object(PROMOTION, "gh_json", side_effect=pr_cli_transport),
            mock.patch.object(PROMOTION, "gh_rest_json", side_effect=pr_rest_transport),
            mock.patch("subprocess.run", side_effect=fake_health_subprocess),
            source_request_git_patch,
        ):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(sys, "argv", preparation_argv),
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr),
            ):
                result_code = PREPARATION.main()
            if pr_cli_reads or pr_rest_reads:
                self.assertEqual(pr_cli_reads, [current_c["pr"]["number"]])
                self.assertEqual(
                    pr_rest_reads,
                    [f"repos/StatPan/datapan-registry/pulls/{current_c['pr']['number']}"],
                )
            if result_code != 0:
                detail = json.loads(stderr.getvalue())
                self.assertEqual(
                    index_path.read_bytes(), original_index,
                )
                self.assertEqual(
                    sorted(
                        path.relative_to(generations_dir).as_posix()
                        for path in generations_dir.rglob("*") if path.is_file()
                    ),
                    original_generation_paths,
                )
                self.assertEqual(self.detail_calls, original_detail_calls)
                current_rows = HANDOFF.validate_ledger(
                    PROCESSOR.load_json(index_path)["collector_handoff"],
                )["admitted_observations"]
                current_admission = next(
                    row for row in current_rows
                    if row["producer_run_id"] == self.observation_run
                    and row["run_attempt"] == self.admission_metadata["run_attempt"]
                )
                self.assertEqual(current_admission, original_admission)
                self.assertFalse((output_root / "preparation-result.json").exists())
                self.assertFalse(args.github_output.exists())
                raise ValueError(str(detail.get("reason") or "derivation_cli_failed"))
            plan = read_json(output_root / "preparation-result.json")
            outputs = dict(line.split("=", 1) for line in args.github_output.read_text(encoding="utf-8").splitlines())
            self.assertEqual(outputs["derivation_enabled"], str(plan.get("eligible") is True).lower())
            if plan.get("eligible") is True:
                self.assertEqual(len(canonical_context_calls), 1)
                self.assertTrue(processor_bundle_head_calls)
                self.assertEqual(outputs["derivation_path"], plan["derivation_path"])
                self.assertEqual(outputs["generation_id"], plan["generation_id"])
        self.assertEqual(index_path.read_bytes(), original_index)
        self.assertEqual(
            sorted(
                path.relative_to(generations_dir).as_posix()
                for path in generations_dir.rglob("*") if path.is_file()
            ),
            original_generation_paths,
        )
        self.assertEqual(self.detail_calls, original_detail_calls)
        current_rows = HANDOFF.validate_ledger(
            PROCESSOR.load_json(index_path)["collector_handoff"],
        )["admitted_observations"]
        current_admission = next(
            row for row in current_rows
            if row["producer_run_id"] == self.observation_run
            and row["run_attempt"] == self.admission_metadata["run_attempt"]
        )
        self.assertEqual(current_admission, original_admission)
        self.assertEqual(current_admission["admitted_at"], original_admission["admitted_at"])
        self.assertEqual(current_admission["generation_id"], original_admission["generation_id"])
        return plan

    def _validate_c_derivation(
        self, checkpoint: dict[str, Any], journal: dict[str, Any], c: dict[str, Any], *,
        wrong_attempt: bool = False,
        current_head_sha: str | None = None,
        canonical_identity: Mapping[str, Any] | None = None,
        pr_cli_state: str = "MERGED",
        pr_rest_merged: Any = True,
        include_pr_rest_merged: bool = True,
    ) -> None:
        """Exercise the production C-side lineage validator with local API/Git adapters."""
        bundle_dir = self.output_by_generation[checkpoint["generation_id"]]
        cache_source_helper, cache_source_contract = self._authenticated_cache_source_contract(checkpoint)
        bundle = PROMOTION.validate_processor_bundle(
            checkpoint, bundle_dir,
            read_json(ROOT / "schemas/datapan.catalogue-composition-receipt.v1.schema.json"),
            PROMOTION.load_canonical_update_pr(ROOT), root=ROOT,
            cache_source_helper=cache_source_helper,
            cache_source_contract=cache_source_contract,
        )
        envelope = checkpoint["generation_inputs"]["same_observation_derivation"]
        baseline = envelope["composition_baseline"]
        row = journal["records"][envelope["canonical_parent_readback"]["journal_record_index"]]
        ack = next(value for value in row["acknowledgements"] if value.get("status") == "merged")
        run_id, attempt = str(ack["run_id"]), int(ack["run_attempt"])
        policy = read_json(ROOT / "policy/upstream-catalogue-health.json")
        workflow_path = policy["promotion_state"]["promotion_workflow_path"]
        workflow_id = 912345
        completed = (self.now - dt.timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
        run_attempt_value = attempt + 1 if wrong_attempt else attempt
        run = {
            "id": int(run_id), "run_attempt": run_attempt_value, "workflow_id": workflow_id,
            "path": workflow_path, "event": "workflow_run", "head_branch": "main",
            "head_sha": ack["source_sha"], "status": "completed", "conclusion": "success",
            "run_started_at": (self.now - dt.timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "completed_at": completed,
            "repository": {"full_name": "StatPan/datapan-registry"},
            "head_repository": {"full_name": "StatPan/datapan-registry"},
        }
        job = {
            "id": 912346, "run_id": int(run_id), "run_attempt": attempt,
            "head_sha": ack["source_sha"], "status": "completed", "conclusion": "success",
            "completed_at": completed,
        }
        body = row["ownership"]["body"]
        pr_readback = {
            "number": row["pr"]["number"], "url": row["pr"].get("url"), "state": pr_cli_state,
            "repository": "StatPan/datapan-registry", "headRepository": "StatPan/datapan-registry",
            "headRefName": row["ownership"]["branch"],
            "headRefOid": row["ownership"]["expected_head_sha"], "baseRefName": "main",
            "baseRefOid": current_head_sha or baseline["main_sha"],
            "mergeCommit": {"oid": row["pr"]["merge_commit_sha"]}, "merged": True,
            "body": body,
        }
        cli_pr, rest_pr = primary_pr_transport(
            pr_readback,
            cli_state=pr_cli_state,
            rest_merged=pr_rest_merged,
            include_rest_merged=include_pr_rest_merged,
        )
        cli_reads: list[int] = []
        rest_reads: list[str] = []

        def pr_cli_transport(_root: pathlib.Path, *argv: str) -> dict[str, Any]:
            if argv[:2] != ("pr", "view"):
                raise AssertionError(f"unexpected C PR CLI transport request: {argv!r}")
            cli_reads.append(int(argv[2]))
            return copy.deepcopy(cli_pr)

        def pr_rest_transport(_root: pathlib.Path, endpoint: str, *argv: str) -> dict[str, Any]:
            expected = f"repos/StatPan/datapan-registry/pulls/{row['pr']['number']}"
            if endpoint != expected or argv != ("--method", "GET"):
                raise AssertionError(f"unexpected C PR REST transport request: {(endpoint, *argv)!r}")
            rest_reads.append(endpoint)
            return copy.deepcopy(rest_pr)

        def fake_command(argv, cwd, *, allowed_returncodes=frozenset({0}), env=None):
            del cwd, env
            command = tuple(str(part) for part in argv)
            if command[:2] == ("git", "show") and len(command) == 3:
                revision, relative = command[2].split(":", 1)
                if relative == "manifest.json":
                    data = self.current_manifests[revision].decode("utf-8")
                elif relative == DERIVATION.SAFE_PATH:
                    data = (
                        "version https://git-lfs.github.com/spec/v1\n"
                        f"oid sha256:{baseline['registry_sha256']}\n"
                        f"size {baseline['registry_bytes']}\n"
                    )
                else:
                    raise AssertionError(f"unexpected C git show path: {relative}")
                return subprocess.CompletedProcess(argv, 0, stdout=data, stderr="")
            if command[:3] == ("git", "merge-base", "--is-ancestor"):
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            raise AssertionError(f"unexpected C git command: {command!r}; allowed={allowed_returncodes}")

        def fake_subprocess(argv, *args, **kwargs):
            del args, kwargs
            command = tuple(str(part) for part in argv)
            if command[:2] != ("gh", "api") or len(command) < 3:
                raise AssertionError(f"unexpected external C subprocess: {command!r}")
            endpoint = command[2]
            if endpoint == f"repos/StatPan/datapan-registry/actions/workflows/{pathlib.PurePosixPath(workflow_path).name}":
                value: Any = {"id": workflow_id, "path": workflow_path}
            elif endpoint == f"repos/StatPan/datapan-registry/actions/runs/{run_id}/attempts/{attempt}":
                value = run
            elif endpoint.startswith(f"repos/StatPan/datapan-registry/actions/runs/{run_id}/attempts/{attempt}/jobs?"):
                value = {"total_count": 1, "jobs": [job]}
            else:
                raise AssertionError(f"unexpected exact C trust endpoint: {endpoint}")
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(value), stderr="")

        with (
            mock.patch.object(PROMOTION, "command", side_effect=fake_command),
            mock.patch.object(PROMOTION, "gh_json", side_effect=pr_cli_transport),
            mock.patch.object(PROMOTION, "gh_rest_json", side_effect=pr_rest_transport),
            mock.patch("subprocess.run", side_effect=fake_subprocess),
        ):
            validated = False
            try:
                PROMOTION.validate_same_observation_derivation_for_c(
                    self.helper.root, self.helper.state_dir, checkpoint, bundle, journal, c["readback"]["journal_ref_sha"],
                    current_head_sha=current_head_sha or baseline["main_sha"],
                    canonical_identity=dict(canonical_identity or baseline),
                )
                validated = True
            finally:
                if validated or cli_reads or rest_reads:
                    self.assertEqual(cli_reads, [row["pr"]["number"]])
                    self.assertEqual(rest_reads, [f"repos/StatPan/datapan-registry/pulls/{row['pr']['number']}"])

    def _select_actual_derived_candidate(
        self, checkpoint: dict[str, Any], journal: dict[str, Any], c: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[dict[str, str]], list[dict[str, Any]], list[int]]:
        """Run the actual C candidate selector with only external GETs stubbed."""
        locator = checkpoint["output_artifact"]
        run_id, attempt, artifact_name = PROMOTION.processor_attempt_from_locator(checkpoint)
        processor_head_sha, _processor_snapshot = self._cache_source_snapshot(checkpoint)
        run = {
            "id": int(run_id), "run_attempt": int(attempt),
            "name": PROMOTION.PROCESSOR_WORKFLOW_NAME,
            "path": PROMOTION.PROCESSOR_WORKFLOW_PATH,
            "repository": {"full_name": "StatPan/datapan-registry"},
            "head_repository": {"full_name": "StatPan/datapan-registry"},
            "head_branch": "main", "head_sha": processor_head_sha,
            "event": "schedule", "status": "completed", "conclusion": "success",
        }
        artifact = {
            "id": int(locator["artifact_id"]), "name": artifact_name,
            "expired": False, "expires_at": locator["expires_at"],
            "size_in_bytes": 101,
            "workflow_run": {
                "id": int(run_id), "head_sha": processor_head_sha, "head_branch": "main",
            },
        }
        row = journal["records"][0]
        pr_readback = {
            "number": row["pr"]["number"], "url": row["pr"].get("url"), "state": "MERGED",
            "repository": "StatPan/datapan-registry", "headRepository": "StatPan/datapan-registry",
            "headRefName": row["ownership"]["branch"],
            "headRefOid": row["ownership"]["expected_head_sha"], "baseRefName": "main",
            "baseRefOid": c["baseline"]["main_sha"],
            "mergeCommit": {"oid": row["pr"]["merge_commit_sha"]}, "merged": True,
            "body": row["ownership"]["body"],
        }
        cli_pr, rest_pr = primary_pr_transport(pr_readback)
        pr_reads: list[int] = []
        rest_reads: list[str] = []

        def pr_cli_transport(_root: pathlib.Path, *argv: str) -> dict[str, Any]:
            if argv[:2] != ("pr", "view"):
                raise AssertionError(f"unexpected selector PR CLI transport request: {argv!r}")
            pr_reads.append(int(argv[2]))
            return copy.deepcopy(cli_pr)

        def pr_rest_transport(_root: pathlib.Path, endpoint: str, *argv: str) -> dict[str, Any]:
            expected = f"repos/StatPan/datapan-registry/pulls/{row['pr']['number']}"
            if endpoint != expected or argv != ("--method", "GET"):
                raise AssertionError(f"unexpected selector PR REST transport request: {(endpoint, *argv)!r}")
            rest_reads.append(endpoint)
            return copy.deepcopy(rest_pr)

        composition_schema = read_json(ROOT / "schemas/datapan.catalogue-composition-receipt.v1.schema.json")
        composition_helper = PROMOTION.load_canonical_update_pr(ROOT)
        already_canonical: list[dict[str, Any]] = []
        selected_bundle = self.output_by_generation[checkpoint["generation_id"]]
        self._cache_source_archive_path(checkpoint)

        def authenticate_cache_source_fixture(
            _root: pathlib.Path, value: Mapping[str, Any], *,
            repository: str, default_branch: str, native_reader: Any | None = None,
            archive_path: pathlib.Path | None = None,
        ) -> tuple[Any, Any]:
            helper = PROCESSOR.CACHE_SOURCE
            reader = native_reader or self._cache_source_reader()
            contract = (
                helper.authenticate_owner_contract_from_archive_path(
                    value, repository=repository, default_branch=default_branch,
                    reader=reader, archive_path=archive_path,
                )
                if archive_path is not None
                else helper.authenticate_owner_contract(
                    value, repository=repository, default_branch=default_branch, reader=reader,
                )
            )
            return helper, contract

        with (
            self._derivation_git(
                snapshot=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
                derivation=checkpoint["generation_inputs"].get("same_observation_derivation"),
                current_main_sha=c["baseline"]["main_sha"],
            ),
            mock.patch.object(PROMOTION, "processor_run_api", return_value=run),
            mock.patch.object(PROMOTION, "processor_artifact_api", return_value=artifact),
            mock.patch.object(PROMOTION, "download_processor_artifact", return_value=selected_bundle),
            mock.patch.object(
                PROMOTION, "authenticate_processor_cache_source_contract",
                side_effect=authenticate_cache_source_fixture,
            ),
            mock.patch.object(PROMOTION, "verify_processor_input_compatibility", return_value=None),
            mock.patch.object(PROMOTION, "authenticated_current_canonical_registry", return_value=c["baseline"]),
            mock.patch.object(PROMOTION, "authenticated_current_canonical_context", return_value={
                "identity": c["baseline"], "rows": json.loads(c["payload"]),
            }),
            mock.patch.object(PROMOTION, "gh_json", side_effect=pr_cli_transport),
            mock.patch.object(PROMOTION, "gh_rest_json", side_effect=pr_rest_transport),
        ):
            selected, blocked = PROMOTION.select_first_eligible_processor_bundle(
                ROOT, "StatPan/datapan-registry", [checkpoint], [], journal,
                c["readback"]["journal_ref_sha"], self.helper.state_dir,
                default_branch="main", current_head_sha=c["baseline"]["main_sha"],
                composition_schema=composition_schema, composition_helper=composition_helper,
                already_canonical=already_canonical,
            )
        if pr_reads or rest_reads:
            self.assertEqual(rest_reads, [f"repos/StatPan/datapan-registry/pulls/{row['pr']['number']}"])
        return selected, blocked, already_canonical, pr_reads

    def _health_receipt_for_derived_candidate(
        self, checkpoint: dict[str, Any], journal: dict[str, Any], c: dict[str, Any],
    ) -> dict[str, Any]:
        """Run Health's real checkpoint reader/evaluator on a sealed derived B."""
        health = HEALTH_TESTS.HEALTH
        bundle_dir = self.output_by_generation[checkpoint["generation_id"]]
        payload = (bundle_dir / "composed-candidate.registry.json").read_bytes()
        registry_sha = hashlib.sha256(payload).hexdigest()
        current_payload = c["payload"]
        locator = checkpoint["output_artifact"]
        run_id, attempt, _name = PROMOTION.processor_attempt_from_locator(checkpoint)
        screen = {
            "generation_id": checkpoint["generation_id"],
            "run_id": run_id,
            "attempt": attempt,
            "artifact_id": locator["artifact_id"],
            "run": {"id": run_id, "run_attempt": attempt},
            "bundle": {
                "status": checkpoint["status"],
                "registry_path": DERIVATION.SAFE_PATH,
                "registry_bytes": len(payload),
                "registry_sha256": registry_sha,
                "composition_outputs_dir": str(bundle_dir),
            },
        }
        # The full archived output was already accepted by the actual C
        # selector in the caller test. Health receives the exact verified
        # screen summary and independently checks the sealed checkpoint and
        # composed bytes against current main identity.
        main_identity = {
            "registry_path": DERIVATION.SAFE_PATH,
            "registry_bytes": len(current_payload),
            "registry_sha256": hashlib.sha256(current_payload).hexdigest(),
        }
        original = checkpoint["generation_inputs"]["same_observation_derivation"]["original_observation"]
        collector_run = HEALTH_TESTS.collector_run(
            run_id=original["producer_run_id"],
            created_at=(self.now - dt.timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            run_started_at=(self.now - dt.timedelta(seconds=29)).isoformat().replace("+00:00", "Z"),
            head_sha=original["producer_head_sha"],
        )
        collector_run["updated_at"] = (self.now + dt.timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
        artifacts = {
            original["producer_run_id"]: [{
                "id": int(original["producer_artifact_id"]),
                "name": original["producer_artifact_name"],
                "expired": False,
            }],
        }
        readback = checkpoint["generation_inputs"]["same_observation_derivation"]["canonical_parent_readback"]
        ack_attempt = HEALTH_TESTS.promotion_attempt_evidence(
            readback["merge_ack_run_id"], readback["merge_ack_observed_at"], HEALTH_TESTS.PROMOTION_PATH,
        )
        ack_attempt["run"]["head_sha"] = readback["merge_ack_source_sha"]
        ack_attempt["jobs"][0]["head_sha"] = readback["merge_ack_source_sha"]
        promotion_runs = {
            f"{readback['merge_ack_run_id']}/{readback['merge_ack_run_attempt']}": ack_attempt,
        }
        prior_last_good = {
            "status": "read-back-confirmed", "source_id": "data_go_kr", "generation_id": "0" * 64,
            "publication_revision": "9" * 40, "publication_pointer_revision": "8" * 40,
            "artifact_identity": {
                "path": DERIVATION.SAFE_PATH, "bytes": 123, "sha256": "d" * 64,
            },
            "verified": True, "publication_run_jobs_completed_at": "2026-09-29T00:00:00Z",
            "publication_run_completion_basis": "max_completed_at_all_jobs_exact_run_attempt",
            "publication_run_id": 90, "publication_run_attempt": 1,
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            health, "manifest_registry_identity", return_value=main_identity,
        ):
            test_case = HEALTH_TESTS.UpstreamCatalogueHealthTest()
            report = test_case.run_health(
                pathlib.Path(directory), cp=[checkpoint], runs=[collector_run],
                artifacts_by_run=artifacts, ack=journal, as_of=self.now + dt.timedelta(minutes=2),
                main_revision=checkpoint["generation_inputs"]["same_observation_derivation"]["composition_baseline"]["main_sha"],
                manifest_sha256=checkpoint["generation_inputs"]["same_observation_derivation"]["composition_baseline"]["manifest_sha256"],
                last_good={"data_go_kr": prior_last_good}, promotion_runs=promotion_runs,
                processor_candidate_screen=lambda _checkpoint: copy.deepcopy(screen),
            )
        self.expected_health_last_good = prior_last_good
        health.validate_schema(report, ROOT / "schemas/datapan.upstream-catalogue-health.v1.schema.json", "derived Health receipt")
        return report

    def _run_fresh_replay_verifier(
        self,
        *,
        state_repository: pathlib.Path,
        prior_state_commit: str,
        expected_current_state_commit: str,
        phase: str,
        receipt: Mapping[str, Any],
        result: Mapping[str, Any],
        checkpoint_receipt: Mapping[str, Any] | None = None,
        expect_rejection: bool = False,
        expire_after_receipt_validation: bool = False,
        expire_after_parent_graph: bool = False,
        expire_after_worktree_reads: bool = False,
        extra_environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        request_path = self.helper.root / "fresh-process-replay-verifier-input.json"
        request_path.write_text(json.dumps({
            "receipt": dict(receipt),
            "result": dict(result),
            "checkpoint_receipt": dict(checkpoint_receipt) if checkpoint_receipt is not None else None,
            "expire_after_receipt_validation": expire_after_receipt_validation,
            "expire_after_parent_graph": expire_after_parent_graph,
            "expire_after_worktree_reads": expire_after_worktree_reads,
        }, sort_keys=True), encoding="utf-8")
        driver = r"""
import importlib.util
import json
import pathlib
import sys
import types

processor_path = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(processor_path.parent))
spec = importlib.util.spec_from_file_location("fresh_replay_verifier_processor", processor_path)
if spec is None or spec.loader is None:
    raise SystemExit("fresh processor import unavailable")
processor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(processor)
request = json.loads(pathlib.Path(sys.argv[6]).read_text(encoding="utf-8"))
if (
    request.get("expire_after_receipt_validation")
    or request.get("expire_after_parent_graph")
    or request.get("expire_after_worktree_reads")
):
    class _VerifierClock:
        expired = False
        def monotonic(self):
            return 61.0 if self.expired else 0.0
    _clock = _VerifierClock()
    processor.time = types.SimpleNamespace(monotonic=_clock.monotonic)
    if request.get("expire_after_receipt_validation"):
        _verify_receipt = processor.verify_replay_persistence_receipt
        def _verify_then_expire(*args, **kwargs):
            value = _verify_receipt(*args, **kwargs)
            _clock.expired = True
            return value
        processor.verify_replay_persistence_receipt = _verify_then_expire
    if request.get("expire_after_parent_graph"):
        _validate_graph = processor.DERIVATION.validate_processor_parent_graph
        def _validate_then_expire(*args, **kwargs):
            value = _validate_graph(*args, **kwargs)
            _clock.expired = True
            return value
        processor.DERIVATION.validate_processor_parent_graph = _validate_then_expire
    if request.get("expire_after_worktree_reads"):
        _read_worktree_json = processor._replay_worktree_json
        _worktree_reads = [0]
        def _read_then_expire(*args, **kwargs):
            value = _read_worktree_json(*args, **kwargs)
            _worktree_reads[0] += 1
            if _worktree_reads[0] == 2:
                _clock.expired = True
            return value
        processor._replay_worktree_json = _read_then_expire
try:
    verified = processor.verify_replay_persistence_receipt_from_committed_state(
        request["receipt"],
        state_repository=pathlib.Path(sys.argv[2]),
        prior_state_commit=sys.argv[3],
        expected_current_state_commit=sys.argv[4],
        phase=sys.argv[5],
        result=request["result"],
        checkpoint_receipt=request["checkpoint_receipt"],
    )
except Exception as exc:
    if sys.argv[7] == "reject":
        print(f"REJECTED:{type(exc).__name__}:{exc}")
        raise SystemExit(0)
    raise
if sys.argv[7] == "reject":
    raise SystemExit("unexpected verifier acceptance")
print("VERIFIED:" + str(verified["generation_id"]))
"""
        environment = os.environ.copy()
        if extra_environment is not None:
            environment.update(extra_environment)
        return subprocess.run(
            (
                sys.executable, "-I", "-B", "-c", driver,
                str(ROOT / "scripts/process-upstream-catalogue-candidate.py"),
                str(state_repository), prior_state_commit, expected_current_state_commit,
                phase, str(request_path), "reject" if expect_rejection else "accept",
            ),
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_two_claim_process_cycles_union_same_a_parents_without_refreshing_a_or_retry_state(self) -> None:
        # Two ordinary B executions model separate processor revisions on the
        # same admitted A. Each consumes one physical request and preserves the
        # same observation clock. C0 records B0 as the canonical producer.
        b0 = self._run_claim_and_worker(1)
        first_revision = PROCESSOR.generator_revision()
        b1 = self._run_claim_and_worker(
            2,
            resume=self.output_by_generation[b0["generation_id"]] / "upstream-catalogue-enrichment-evidence.json",
            generator_revision=self.alternate_generator_revision,
        )
        self.assertNotEqual(first_revision, b1["generation_inputs"]["generator_revision"])
        journal1, c1 = self._synthetic_c_journal(b0, 1)
        stale_selected, stale_blocked, stale_existing, stale_pr_reads = self._select_actual_derived_candidate(
            b1, journal1, c1,
        )
        self.assertIsNone(stale_selected)
        self.assertEqual(stale_blocked, [{
            "generation_id": b1["generation_id"],
            "reason": "processor_baseline_stale_for_current_canonical",
        }])
        self.assertEqual(stale_existing, [])
        self.assertEqual(stale_pr_reads, [])
        duplicate_journal = copy.deepcopy(journal1)
        duplicate_journal["records"].append(copy.deepcopy(duplicate_journal["records"][0]))
        with self.assertRaisesRegex(ValueError, "same_observation_canonical_lineage_ambiguous"):
            self._prepare_authenticated_plan(
                b1, duplicate_journal, c1, label="ambiguous-c-lineage",
            )
        with self.assertRaisesRegex(ValueError, "canonical_parent_PR_live_readback_mismatch"):
            self._prepare_authenticated_plan(
                b1, journal1, c1, label="mismatched-c-pr-readback", mutate_pr_readback=True,
            )
        native_merge_failures = (
            ("cli-closed", "CLOSED", True, True),
            ("cli-open", "OPEN", True, True),
            ("rest-not-merged", "MERGED", False, True),
            ("rest-merged-missing", "MERGED", True, False),
        )
        for label, cli_state, rest_merged, include_rest_merged in native_merge_failures:
            with self.subTest(preparation_native_merge_failure=label):
                with self.assertRaisesRegex(ValueError, "canonical_parent_PR_live_readback_mismatch"):
                    self._prepare_authenticated_plan(
                        b1, journal1, c1, label=f"native-{label}-c-pr-readback",
                        pr_cli_state=cli_state,
                        pr_rest_merged=rest_merged,
                        include_pr_rest_merged=include_rest_merged,
                    )
        plan1 = self._prepare_authenticated_plan(b1, journal1, c1, label="first-recursive-intake")
        self.assertTrue(plan1["eligible"], plan1)
        env1 = read_json(pathlib.Path(plan1["derivation_path"]))

        # The claim and worker both run the production process path. Only the
        # remote main/journal/PR API boundary is replaced by the independently
        # validated frozen C reference; no detail/provider call is mocked away.
        b2 = self._run_claim_and_worker(
            3, derivation=env1, composition_baseline=pathlib.Path(plan1["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[b1["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
            journal=journal1, journal_ref_sha=c1["readback"]["journal_ref_sha"],
        )
        self._validate_c_derivation(b2, journal1, c1)
        # The ordinary C selector must admit this fully screened same-A B
        # against the exact merged canonical producer, even though B's
        # original immutable A baseline still differs from today's registry.
        selected_b2, blocked_b2, existing_b2, pr_reads_b2 = self._select_actual_derived_candidate(
            b2, journal1, c1,
        )
        self.assertEqual(blocked_b2, [])
        self.assertEqual(existing_b2, [])
        self.assertIsNotNone(selected_b2)
        self.assertEqual(selected_b2["generation_id"], b2["generation_id"])
        self.assertEqual(pr_reads_b2, [journal1["records"][0]["pr"]["number"]])
        for label, cli_state, rest_merged, include_rest_merged in native_merge_failures:
            with self.subTest(c_validation_native_merge_failure=label):
                with self.assertRaisesRegex(PROMOTION.PromotionError, "strict C validation"):
                    self._validate_c_derivation(
                        b2, journal1, c1,
                        pr_cli_state=cli_state,
                        pr_rest_merged=rest_merged,
                        include_pr_rest_merged=include_rest_merged,
                    )
        with self.assertRaisesRegex(PROMOTION.PromotionError, "strict C validation"):
            self._validate_c_derivation(b2, journal1, c1, wrong_attempt=True)
        journal2, c2 = self._synthetic_c_journal(b2, 2)
        journal_after_b2 = copy.deepcopy(journal1)
        journal_after_b2["records"].extend(copy.deepcopy(journal2["records"]))
        c2["readback"] = DERIVATION.canonical_parent_readback_reference(
            journal_after_b2,
            journal_ref_sha=c2["readback"]["journal_ref_sha"],
            record_index=1,
        )
        # Once the exact derived B is already the canonical C row, the normal
        # selector must classify it as an existing canonical candidate. A
        # replay may not create another owned PR or append a duplicate ACK.
        replay_selected, replay_blocked, replay_already_canonical, replay_pr_reads = (
            self._select_actual_derived_candidate(b2, journal_after_b2, c2)
        )
        self.assertIsNone(replay_selected)
        self.assertEqual(replay_blocked, [])
        self.assertEqual(len(replay_already_canonical), 1)
        self.assertEqual(replay_already_canonical[0]["generation_id"], b2["generation_id"])
        self.assertEqual(replay_pr_reads, [])
        processor_run_id, processor_attempt, _processor_artifact_name = PROMOTION.processor_attempt_from_locator(b2)
        noop_args = type("CandidatePreparationArgs", (), {
            "workflow_run_id": processor_run_id,
            "workflow_run_attempt": processor_attempt,
            "workflow_run_head_sha": self._cache_source_snapshot(b2)[0],
            "bundle_dir": self.output_by_generation[b2["generation_id"]],
            "state_root": self.helper.state_dir,
            "processor_artifact_id": b2["output_artifact"]["artifact_id"],
            "source_refresh_predecessor": None,
            "source_refresh_target_main_sha": None,
            "_authenticated_canonical_context": {
                "identity": c2["baseline"], "rows": json.loads(c2["payload"]),
            },
        })()

        def no_extra_candidate_git_command(argv, _root, **_kwargs):
            command = tuple(str(part) for part in argv)
            if command == ("git", "rev-parse", "HEAD"):
                return subprocess.CompletedProcess(argv, 0, f"{c2['baseline']['main_sha']}\n", "")
            raise AssertionError(f"already-canonical derived replay attempted preparation command: {command!r}")

        candidate_output = io.StringIO()
        noop_cache_helper, noop_cache_contract = self._authenticated_cache_source_contract(b2)
        with (
            mock.patch.dict(PROMOTION.os.environ, {"GITHUB_REPOSITORY": "StatPan/datapan-registry"}),
            mock.patch.object(PROMOTION, "locate_processor_checkpoint", return_value=(
                self.helper.state_dir / "sources/data_go_kr/generations" / f"{b2['generation_id']}.json", b2,
            )),
            mock.patch.object(PROMOTION, "verify_processor_input_compatibility"),
            mock.patch.object(
                PROMOTION, "authenticate_processor_cache_source_contract",
                return_value=(noop_cache_helper, noop_cache_contract),
            ),
            mock.patch.object(PROMOTION, "authenticated_current_canonical_registry", return_value=c2["baseline"]),
            mock.patch.object(PROMOTION, "command", side_effect=no_extra_candidate_git_command),
            mock.patch.object(PROMOTION, "load_promotion_journal_snapshot") as journal_read,
            contextlib.redirect_stdout(candidate_output),
        ):
            PROMOTION.execute_candidate_preparation(noop_args, self.helper.root)
        candidate_result = json.loads(candidate_output.getvalue())
        self.assertEqual(candidate_result["status"], "already-canonical-payload")
        self.assertFalse(candidate_result["candidate_available"])
        journal_read.assert_not_called()
        self.assertIs(DERIVATION.validate_readback_against_journal(
            env1, journal1, journal_ref_sha=c1["readback"]["journal_ref_sha"],
        ), journal1["records"][0])
        self.assertEqual(c2["readback"]["registry_sha256"], b2["output_artifact"].get("registry_sha256", c2["readback"]["registry_sha256"]))

        # Recurse from exact B1 contribution history and the newly merged B2 C
        # baseline. The fourth synthetic LINK must be processed once; the first
        # three operations survive the real composer without deletion.
        self.assertEqual(b2["status"], "ready")
        self.assertGreater(b2["outcome"]["detail_retry_count"], 0)
        plan2 = self._prepare_authenticated_plan(b2, journal_after_b2, c2, label="second-recursive-intake")
        self.assertTrue(plan2["eligible"], plan2)
        self.assertEqual(plan2["reason"], "same_observation_canonical_derivation_authenticated")
        self.assertFalse(plan2.get("active_generation_resume", False))
        self.assertNotEqual(plan2["generation_id"], b2["generation_id"])
        env2 = read_json(pathlib.Path(plan2["derivation_path"]))
        self.assertEqual(
            env2["resume_parent_processor"]["generation_id"],
            env2["canonical_parent_processor"]["generation_id"],
        )
        self.assertEqual(env2["canonical_parent_processor"]["generation_id"], b2["generation_id"])
        self.assertEqual(
            env2["canonical_parent_readback"]["canonical_producer_generation_id"],
            b2["generation_id"],
        )
        self.assertEqual(
            env2["composition_baseline"]["registry_sha256"],
            c2["baseline"]["registry_sha256"],
        )
        b3 = self._run_claim_and_worker(
            4, derivation=env2, composition_baseline=pathlib.Path(plan2["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[env2["resume_parent_processor"]["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b2["generation_id"]],
            journal=journal_after_b2, journal_ref_sha=c2["readback"]["journal_ref_sha"],
        )
        # The second real B composition is independently accepted by the
        # strict C lineage reader against its own exact merged/read-back row.
        self._validate_c_derivation(b3, journal_after_b2, c2)
        self.assertEqual(b3["status"], "ready")
        # B1 was the distinct resume parent in B2's first derivative, then a
        # later exact same-A descendant B3 shadowed it. The historical resume
        # exception is per-envelope; it must not be subtracted from the union
        # and revived as an independent active slot.
        with mock.patch.object(PROCESSOR, "DEFAULT_MAX_ACTIVE_GENERATIONS", 1):
            retained = PROCESSOR.plan_generation_retention(
                PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json"),
                self.helper.state_dir / "sources/data_go_kr/generations",
                b3,
            )
        self.assertIn(b1["generation_id"], retained)
        self.assertIn(b2["generation_id"], retained)
        self.assertIn(b3["generation_id"], retained)
        output = json.loads((self.output_by_generation[b3["generation_id"]] / "composed-candidate.registry.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {row["id"] for row in output}, {"1", "2", "3", "4"},
            json.dumps({
                "ids": [row["id"] for row in output],
                "b1_enrichment": [row["api_key"]["id"] for row in json.loads((self.output_by_generation[b1["generation_id"]] / "upstream-catalogue-enrichment-evidence.json").read_text())["records"]],
                "b2_enrichment": [row["api_key"]["id"] for row in json.loads((self.output_by_generation[b2["generation_id"]] / "upstream-catalogue-enrichment-evidence.json").read_text())["records"]],
                "b3_enrichment": [row["api_key"]["id"] for row in json.loads((self.output_by_generation[b3["generation_id"]] / "upstream-catalogue-enrichment-evidence.json").read_text())["records"]],
                "b3_outcomes": json.loads((self.output_by_generation[b3["generation_id"]] / "upstream-catalogue-enrichment-evidence.json").read_text())["worker_outcomes"],
            }, sort_keys=True),
        )
        final_by_id = {row["id"]: row for row in output}
        for parent in (b0, b1, b2):
            parent_rows = json.loads(
                (self.output_by_generation[parent["generation_id"]] / "composed-candidate.registry.json").read_text(encoding="utf-8"),
            )
            for prior in parent_rows:
                final = final_by_id[prior["id"]]
                self.assertTrue(
                    all(operation in final["operations"] for operation in prior.get("operations", [])),
                    f"derived composition omitted an authenticated parent operation for {prior['id']}",
                )
        self.assertEqual(len(self.detail_calls), 4)
        self.assertEqual(len(self.claim_runs), 4)
        for checkpoint in (b0, b1, b2, b3):
            self.assertEqual(checkpoint["observation_count"], 1)
            self.assertEqual(checkpoint["observed_at"], self.now_text)
            self.assertEqual(checkpoint["last_observation"]["observed_at"], self.now_text)
        index = PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")
        ledger = HANDOFF.validate_ledger(index["collector_handoff"])
        self.assertEqual(len(ledger["admitted_observations"]), 1)
        self.assertEqual(index["detail_queue_cursor"], b3["detail_queue_cursor"])
        self.assertGreaterEqual(index["detail_queue_cursor"], 0)
        self.assertLess(index["detail_queue_cursor"], len(output))
        self.assertEqual(sum(cp["attempts_consumed"] for cp in (b0, b1, b2, b3)), 4)
        attempted_ids = [
            str(row["id"])
            for cp in (b0, b1, b2, b3)
            for row in cp["detail_records"]
            if row.get("status") == "enriched"
        ]
        self.assertCountEqual(attempted_ids, ["1", "2", "3", "4"])
        self.assertEqual(index["detail_retry_state"], {})
        # Every generated artifact is verified by the same bundle validator C
        # consumes, including the exact modern ten-file inventory and checkpoint copy.
        runner = load_script("same_observation_bundle_validator", ROOT / "scripts/run-canonical-update-promotion.py")
        composition_schema = read_json(ROOT / "schemas/datapan.catalogue-composition-receipt.v1.schema.json")
        composition_helper = load_script("same_observation_composition_helper", ROOT / "scripts/canonical_update_pr.py")
        for checkpoint in (b0, b1, b2, b3):
            cache_source_helper, cache_source_contract = self._authenticated_cache_source_contract(checkpoint)
            verified = runner.validate_processor_bundle(
                checkpoint, self.output_by_generation[checkpoint["generation_id"]], composition_schema, composition_helper,
                root=ROOT,
                cache_source_helper=cache_source_helper,
                cache_source_contract=cache_source_contract,
            )
            self.assertEqual(verified["status"], "ready")
            self.assertEqual(
                verified["registry_sha256"],
                hashlib.sha256((self.output_by_generation[checkpoint["generation_id"]] / "composed-candidate.registry.json").read_bytes()).hexdigest(),
            )

        # Parent raw bytes are independently required, not inferred from the
        # envelope's digest metadata. A missing archived member fails closed.
        missing_bundle = self.helper.root / "missing-parent-member"
        shutil.copytree(self.output_by_generation[b0["generation_id"]], missing_bundle)
        (missing_bundle / "semantic-diff.json").unlink()
        cache_source_helper, cache_source_contract = self._authenticated_cache_source_contract(b0)
        with self.assertRaisesRegex(DERIVATION.DerivationError, "parent_bundle_member_set_invalid"):
            DERIVATION.validate_processor_parent_bundle(
                self._parent_ref(b0), b0, missing_bundle,
                cache_source_helper=cache_source_helper,
                owner_contract=cache_source_contract,
            )

        # Every direct and recursive B ancestor remains protected from the
        # generation-file GC. A similarly sealed but unrelated old checkpoint
        # is still eligible for pruning when the cap is full.
        generation_dir = self.helper.state_dir / "sources/data_go_kr/generations"
        protected = PROCESSOR.protected_lineage_generations(generation_dir)
        self.assertTrue({b0["generation_id"], b1["generation_id"], b2["generation_id"]}.issubset(protected))
        orphan = copy.deepcopy(b0)
        orphan_id = "e" * 64
        orphan["generation_id"] = orphan_id
        orphan["status"] = "no-change"
        orphan["outcome"]["detail_retry_count"] = 0
        orphan["lease"] = None
        orphan["request_reservation"] = None
        PROCESSOR.seal_checkpoint(orphan)
        orphan_path = generation_dir / f"{orphan_id}.json"
        orphan_path.write_text(json.dumps(orphan, sort_keys=True), encoding="utf-8")
        current_checkpoint_path = generation_dir / f"{b3['generation_id']}.json"
        PROCESSOR.prune_generation_files(generation_dir, current_checkpoint_path, max_generations=4)
        self.assertTrue(all((generation_dir / f"{generation}.json").is_file() for generation in (
            b0["generation_id"], b1["generation_id"], b2["generation_id"], b3["generation_id"],
        )))
        self.assertFalse(orphan_path.exists())

        # Exact redelivery is idempotent: it must not repeat detail requests,
        # spend retry budget, move the cursor, or rebind the output artifact.
        replay_args = self._args(5)
        replay_envelope_path = self.helper.root / "derivation-replay.json"
        replay_envelope_path.write_bytes(DERIVATION.canonical_json(env2) + b"\n")
        replay_baseline_path = self.helper.root / "composition-baseline-replay.json"
        replay_baseline_path.write_bytes(pathlib.Path(plan2["composition_baseline_path"]).read_bytes())
        replay_journal_path = self.helper.root / "journal-replay.json"
        replay_journal_path.write_text(json.dumps(journal_after_b2, sort_keys=True), encoding="utf-8")
        replay_args.same_observation_derivation = replay_envelope_path
        replay_args.composition_baseline = replay_baseline_path
        replay_args.current_main_root = self.helper.root
        replay_args.derivation_journal = replay_journal_path
        replay_args.derivation_journal_ref_sha = c2["readback"]["journal_ref_sha"]
        replay_args.resume_parent_bundle_dir = self.output_by_generation[env2["resume_parent_processor"]["generation_id"]]
        replay_args.canonical_parent_bundle_dir = self.output_by_generation[b2["generation_id"]]
        replay_args.resume_parent_archive = self._cache_source_archive_path(
            self.checkpoints[env2["resume_parent_processor"]["generation_id"]],
        )
        replay_args.canonical_parent_archive = self._cache_source_archive_path(b2)
        replay_args.resume_enrichment_evidence = replay_args.resume_parent_bundle_dir / "upstream-catalogue-enrichment-evidence.json"
        replay_args.resume_source_context = replay_args.resume_parent_bundle_dir / PROCESSOR.CACHE_SOURCE.CONTEXT_MEMBER
        replay_args.resume_owner_archive = replay_args.resume_parent_archive
        replay_args.canonical_update_pr_helper = ROOT / "scripts/canonical_update_pr.py"
        replay_args.canonical_update_journal_schema = ROOT / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json"
        replay_checkpoint_path = generation_dir / f"{b3['generation_id']}.json"
        before_checkpoint = PROCESSOR.load_json(replay_checkpoint_path)
        before_index = PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")
        schema = PROCESSOR.load_json(ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json")
        before_index_bytes = (self.helper.state_dir / "sources/data_go_kr/index.json").read_bytes()
        before_checkpoint_bytes = replay_checkpoint_path.read_bytes()
        replay_state_repository = self.helper.root / "exact-replay-state-repository"
        replay_state_repository.mkdir()
        committed_state_root = replay_state_repository / ".datapan/upstream-catalogue-state"
        shutil.copytree(self.helper.state_dir, committed_state_root)

        def git_replay_state(*argv: str) -> str:
            result = subprocess.run(
                ("git", *argv), cwd=replay_state_repository, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        git_replay_state("init", "-q")
        git_replay_state("config", "user.name", "fixture")
        git_replay_state("config", "user.email", "fixture@example.test")
        git_replay_state("remote", "add", "origin", "https://github.com/StatPan/datapan-registry.git")
        git_replay_state("add", ".datapan/upstream-catalogue-state")
        git_replay_state("commit", "-q", "-m", "exact ready-state replay fixture")
        replay_state_head_sha = git_replay_state("rev-parse", "HEAD")
        self.assertEqual(
            subprocess.run(
                ("git", "show", f"{replay_state_head_sha}:.datapan/upstream-catalogue-state/sources/data_go_kr/index.json"),
                cwd=replay_state_repository, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=True,
            ).stdout,
            before_index_bytes,
        )
        self.assertEqual(
            subprocess.run(
                ("git", "show", f"{replay_state_head_sha}:.datapan/upstream-catalogue-state/sources/data_go_kr/generations/{b3['generation_id']}.json"),
                cwd=replay_state_repository, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=True,
            ).stdout,
            before_checkpoint_bytes,
        )
        replay_args.replay_state_head_sha = replay_state_head_sha
        before_calls = len(self.detail_calls)
        missing_state_head_args = copy.copy(replay_args)
        missing_state_head_args.replay_state_head_sha = None
        with self._derivation_git(
            snapshot=journal_after_b2, journal_ref_sha=c2["readback"]["journal_ref_sha"], derivation=env2,
        ):
            with self.assertRaisesRegex(ValueError, "replay_state_head_sha_invalid"):
                PROCESSOR.process(
                    missing_state_head_args, fetcher=self._fetch,
                    sleeper=lambda _delay: None, clock=lambda: self.now,
                    cache_source_reader=self._cache_source_reader(),
                )
        self.assertEqual((self.helper.state_dir / "sources/data_go_kr/index.json").read_bytes(), before_index_bytes)
        self.assertEqual(replay_checkpoint_path.read_bytes(), before_checkpoint_bytes)
        self.assertEqual(len(self.detail_calls), before_calls)
        real_process = PROCESSOR.process

        def process_with_local_fixture_io(parsed_args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
            self.assertIs(parsed_args, replay_args)
            return real_process(
                parsed_args, fetcher=self._fetch, sleeper=lambda _delay: None, clock=lambda: self.now,
                cache_source_reader=self._cache_source_reader(),
            )

        parser = mock.Mock()
        parser.parse_args.return_value = replay_args
        with self._derivation_git(
            snapshot=journal_after_b2, journal_ref_sha=c2["readback"]["journal_ref_sha"], derivation=env2,
        ):
            # Exercise the real CLI terminal-replay branch and its durable
            # receipt writes.  Only parsing and the external I/O dependencies
            # are fixture-bound; the production process and main bodies run.
            with mock.patch.object(PROCESSOR, "build_parser", return_value=parser):
                with mock.patch.object(PROCESSOR, "process", side_effect=process_with_local_fixture_io):
                    with contextlib.redirect_stdout(io.StringIO()):
                        replay_code = PROCESSOR.main([])
        replayed = PROCESSOR.load_json(replay_checkpoint_path)
        after_index = PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")
        self.assertEqual(replay_code, 0)
        self.assertEqual(replayed["generation_id"], b3["generation_id"])
        self.assertEqual(replayed["output_artifact"]["artifact_id"], before_checkpoint["output_artifact"]["artifact_id"])
        self.assertEqual(replayed["observation_count"], before_checkpoint["observation_count"])
        self.assertEqual(replayed["attempts_consumed"], before_checkpoint["attempts_consumed"])
        self.assertEqual(after_index["detail_queue_cursor"], before_index["detail_queue_cursor"])
        self.assertEqual(after_index["detail_retry_state"], before_index["detail_retry_state"])
        self.assertEqual(after_index["collector_handoff"], before_index["collector_handoff"])
        self.assertEqual(len(self.detail_calls), before_calls)

        lineage_proof = replay_args.exact_delivery_replay_context["validated_derivation_lineage"]
        origin_admission = next(
            row for row in before_index["collector_handoff"]["admitted_observations"]
            if row["producer_run_id"] == self.observation_run
        )
        self.assertNotEqual(origin_admission["generation_id"], b3["generation_id"])
        self.assertIn(origin_admission["generation_id"], lineage_proof.ancestor_generation_ids)
        self.assertEqual(
            DERIVATION.original_observation_from_checkpoint(before_checkpoint, origin_admission),
            env2["original_observation"],
        )
        replay_result = PROCESSOR.processing_result(
            replayed,
            exact_delivery_replay=True,
            producer_run_id=self.observation_run,
            processor_run_id=replay_args.processor_run_id,
            processor_artifact_run_id=replay_args.processor_artifact_run_id,
        )
        replay_receipt_args = {
            "prior_checkpoint": before_checkpoint,
            "current_checkpoint": replayed,
            "prior_index": before_index,
            "current_index": after_index,
            "result": replay_result,
            "state_head_sha": replay_state_head_sha,
            "checkpoint_schema": schema,
            "validated_derivation_lineage": lineage_proof,
        }
        persisted_receipt_path = replay_args.output_dir / PROCESSOR.REPLAY_PERSISTENCE_RECEIPT
        replay_receipt = PROCESSOR.load_json(persisted_receipt_path)
        self.assertIs(
            PROCESSOR.verify_replay_persistence_receipt(replay_receipt, **replay_receipt_args),
            replay_receipt,
        )
        with self.assertRaisesRegex(ValueError, "replay_original_admission_not_unique"):
            PROCESSOR.build_replay_persistence_receipt(
                **{key: value for key, value in replay_receipt_args.items() if key != "validated_derivation_lineage"},
            )
        unrelated_lineage = PROCESSOR._ValidatedReplayLineage(
            token=PROCESSOR._REPLAY_LINEAGE_PROOF_TOKEN,
            target_generation_id=lineage_proof.target_generation_id,
            derivation_sha256=lineage_proof.derivation_sha256,
            original_observation_sha256=lineage_proof.original_observation_sha256,
            ancestor_generation_ids=frozenset(
                lineage_proof.ancestor_generation_ids - {origin_admission["generation_id"]},
            ),
        )
        with self.assertRaisesRegex(ValueError, "replay_original_admission_binding_mismatch"):
            PROCESSOR.build_replay_persistence_receipt(
                **{**replay_receipt_args, "validated_derivation_lineage": unrelated_lineage},
            )
        wrong_target_lineage = PROCESSOR._ValidatedReplayLineage(
            token=PROCESSOR._REPLAY_LINEAGE_PROOF_TOKEN,
            target_generation_id="f" * 64,
            derivation_sha256=lineage_proof.derivation_sha256,
            original_observation_sha256=lineage_proof.original_observation_sha256,
            ancestor_generation_ids=lineage_proof.ancestor_generation_ids,
        )
        with self.assertRaisesRegex(ValueError, "replay_original_admission_not_unique"):
            PROCESSOR.build_replay_persistence_receipt(
                **{**replay_receipt_args, "validated_derivation_lineage": wrong_target_lineage},
            )
        tampered_candidate = copy.deepcopy(before_checkpoint)
        tampered_candidate["generation_inputs"]["candidate_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "replay_original_admission_binding_mismatch"):
            PROCESSOR._replay_admission(
                tampered_candidate, before_index,
                validated_derivation_lineage=lineage_proof,
            )
        tampered_observation = copy.deepcopy(before_checkpoint)
        tampered_observation["last_observation"]["observed_at"] = "2026-10-02T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "replay_original_admission_binding_mismatch"):
            PROCESSOR._replay_admission(
                tampered_observation, before_index,
                validated_derivation_lineage=lineage_proof,
            )
        duplicate_admission_index = copy.deepcopy(before_index)
        duplicate_admission_index["collector_handoff"]["admitted_observations"].append(
            copy.deepcopy(origin_admission),
        )
        with self.assertRaises(ValueError):
            PROCESSOR._replay_admission(
                before_checkpoint, duplicate_admission_index,
                validated_derivation_lineage=lineage_proof,
            )

        # A second exact replay uses a newly committed snapshot of the state
        # after the first replay and remains idempotent without provider work.
        committed_state_root = replay_state_repository / ".datapan/upstream-catalogue-state"
        shutil.rmtree(committed_state_root)
        shutil.copytree(self.helper.state_dir, committed_state_root)
        pre_cas_verification = self._run_fresh_replay_verifier(
            state_repository=replay_state_repository,
            prior_state_commit=replay_state_head_sha,
            expected_current_state_commit=replay_state_head_sha,
            phase=PROCESSOR._REPLAY_VERIFY_PRE_CAS,
            receipt=replay_receipt,
            result=replay_result,
            checkpoint_receipt=replayed,
        )
        self.assertEqual(pre_cas_verification.returncode, 0, pre_cas_verification.stderr)
        self.assertIn(f"VERIFIED:{b3['generation_id']}", pre_cas_verification.stdout)
        expired_after_worktree_reads = self._run_fresh_replay_verifier(
            state_repository=replay_state_repository,
            prior_state_commit=replay_state_head_sha,
            expected_current_state_commit=replay_state_head_sha,
            phase=PROCESSOR._REPLAY_VERIFY_PRE_CAS,
            receipt=replay_receipt,
            result=replay_result,
            checkpoint_receipt=replayed,
            expect_rejection=True,
            expire_after_worktree_reads=True,
        )
        self.assertEqual(expired_after_worktree_reads.returncode, 0, expired_after_worktree_reads.stderr)
        self.assertIn(
            "REJECTED:ValueError:replay_state_verification_deadline_exceeded",
            expired_after_worktree_reads.stdout,
        )
        git_replay_state("add", ".datapan/upstream-catalogue-state")
        git_replay_state("commit", "-q", "--allow-empty", "-m", "updated exact ready-state replay fixture")
        replay_state_head_sha = git_replay_state("rev-parse", "HEAD")
        post_cas_verification = self._run_fresh_replay_verifier(
            state_repository=replay_state_repository,
            prior_state_commit=replay_receipt["state_head_sha"],
            expected_current_state_commit=replay_state_head_sha,
            phase=PROCESSOR._REPLAY_VERIFY_POST_CAS,
            receipt=replay_receipt,
            result=replay_result,
            extra_environment={"GIT_DIR": str(ROOT / ".git")},
        )
        self.assertEqual(post_cas_verification.returncode, 0, post_cas_verification.stderr)
        self.assertIn(f"VERIFIED:{b3['generation_id']}", post_cas_verification.stdout)
        expired_after_receipt = self._run_fresh_replay_verifier(
            state_repository=replay_state_repository,
            prior_state_commit=replay_receipt["state_head_sha"],
            expected_current_state_commit=replay_state_head_sha,
            phase=PROCESSOR._REPLAY_VERIFY_POST_CAS,
            receipt=replay_receipt,
            result=replay_result,
            expect_rejection=True,
            expire_after_receipt_validation=True,
        )
        self.assertEqual(expired_after_receipt.returncode, 0, expired_after_receipt.stderr)
        self.assertIn(
            "REJECTED:ValueError:replay_state_verification_deadline_exceeded",
            expired_after_receipt.stdout,
        )
        expired_after_parent_graph = self._run_fresh_replay_verifier(
            state_repository=replay_state_repository,
            prior_state_commit=replay_receipt["state_head_sha"],
            expected_current_state_commit=replay_state_head_sha,
            phase=PROCESSOR._REPLAY_VERIFY_POST_CAS,
            receipt=replay_receipt,
            result=replay_result,
            expect_rejection=True,
            expire_after_parent_graph=True,
        )
        self.assertEqual(expired_after_parent_graph.returncode, 0, expired_after_parent_graph.stderr)
        self.assertIn(
            "REJECTED:ValueError:replay_state_verification_deadline_exceeded",
            expired_after_parent_graph.stdout,
        )
        wrong_head_verification = self._run_fresh_replay_verifier(
            state_repository=replay_state_repository,
            prior_state_commit=replay_receipt["state_head_sha"],
            expected_current_state_commit=replay_receipt["state_head_sha"],
            phase=PROCESSOR._REPLAY_VERIFY_PRE_CAS,
            receipt=replay_receipt,
            result=replay_result,
            checkpoint_receipt=replayed,
            expect_rejection=True,
        )
        self.assertEqual(wrong_head_verification.returncode, 0, wrong_head_verification.stderr)
        self.assertIn("REJECTED:ValueError:replay_state_current_head_mismatch", wrong_head_verification.stdout)
        wrong_prior_verification = self._run_fresh_replay_verifier(
            state_repository=replay_state_repository,
            prior_state_commit="0" * 40,
            expected_current_state_commit=replay_state_head_sha,
            phase=PROCESSOR._REPLAY_VERIFY_POST_CAS,
            receipt=replay_receipt,
            result=replay_result,
            expect_rejection=True,
        )
        self.assertEqual(wrong_prior_verification.returncode, 0, wrong_prior_verification.stderr)
        self.assertIn("REJECTED:ValueError:replay_state_commit_binding_invalid", wrong_prior_verification.stdout)
        tampered_receipt = copy.deepcopy(replay_receipt)
        tampered_receipt["processor_run_id"] = "999999"
        tampered_receipt_verification = self._run_fresh_replay_verifier(
            state_repository=replay_state_repository,
            prior_state_commit=replay_receipt["state_head_sha"],
            expected_current_state_commit=replay_state_head_sha,
            phase=PROCESSOR._REPLAY_VERIFY_POST_CAS,
            receipt=tampered_receipt,
            result=replay_result,
            expect_rejection=True,
        )
        self.assertEqual(tampered_receipt_verification.returncode, 0, tampered_receipt_verification.stderr)
        self.assertIn("REJECTED:ValueError:replay_persistence_receipt_digest_invalid", tampered_receipt_verification.stdout)

        lineage_variant_repository = self.helper.root / "replay-lineage-negative-state-repository"
        clone_result = subprocess.run(
            ("git", "clone", "--no-hardlinks", str(replay_state_repository), str(lineage_variant_repository)),
            cwd=self.helper.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(clone_result.returncode, 0, clone_result.stderr)

        def git_lineage_variant(*argv: str) -> str:
            completed = subprocess.run(
                ("git", *argv), cwd=lineage_variant_repository, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return completed.stdout.strip()

        def assert_committed_lineage_rejected(name: str, mutate_prior) -> None:
            git_lineage_variant("checkout", "-q", "-B", f"bad-{name}", replay_receipt["state_head_sha"])
            variant_state_root = lineage_variant_repository / ".datapan/upstream-catalogue-state"
            variant_index_path = variant_state_root / "sources/data_go_kr/index.json"
            mutate_prior(variant_state_root, variant_index_path)
            git_lineage_variant("add", ".datapan/upstream-catalogue-state")
            git_lineage_variant("commit", "-q", "-m", f"invalid committed replay lineage: {name}")
            bad_prior_sha = git_lineage_variant("rev-parse", "HEAD")
            if name == "tampered-parent":
                committed_parent = subprocess.run(
                    ("git", "show", f"{bad_prior_sha}:{parent_checkpoint_relative}"),
                    cwd=lineage_variant_repository, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(committed_parent.returncode, 0, committed_parent.stderr)
                self.assertEqual(committed_parent.stdout, tampered_parent_snapshot[0])

            target_checkpoint_relative = (
                f".datapan/upstream-catalogue-state/sources/data_go_kr/generations/{b3['generation_id']}.json"
            )
            current_index_relative = ".datapan/upstream-catalogue-state/sources/data_go_kr/index.json"
            shutil.copy2(
                replay_checkpoint_path,
                lineage_variant_repository / target_checkpoint_relative,
            )
            current_index_for_variant = PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")
            if isinstance(current_index_for_variant.get("collector_handoff"), Mapping):
                index_after_mutation = PROCESSOR.load_json(variant_index_path)
                if name == "cross-a-admission":
                    current_index_for_variant["collector_handoff"] = index_after_mutation["collector_handoff"]
            (lineage_variant_repository / current_index_relative).write_text(
                json.dumps(current_index_for_variant, sort_keys=True) + "\n", encoding="utf-8",
            )
            git_lineage_variant("add", target_checkpoint_relative, current_index_relative)
            git_lineage_variant("commit", "-q", "--allow-empty", "-m", f"invalid replay lineage state transition: {name}")
            bad_current_sha = git_lineage_variant("rev-parse", "HEAD")
            bad_receipt = copy.deepcopy(replay_receipt)
            bad_receipt["state_head_sha"] = bad_prior_sha
            bad_receipt.pop("receipt_sha256", None)
            bad_receipt["receipt_sha256"] = PROCESSOR._object_sha256(bad_receipt)
            rejected = self._run_fresh_replay_verifier(
                state_repository=lineage_variant_repository,
                prior_state_commit=bad_prior_sha,
                expected_current_state_commit=bad_current_sha,
                phase=PROCESSOR._REPLAY_VERIFY_POST_CAS,
                receipt=bad_receipt,
                result=replay_result,
                expect_rejection=True,
            )
            self.assertEqual(rejected.returncode, 0, f"{name}: {rejected.stderr}")
            self.assertIn("REJECTED:ValueError:", rejected.stdout, f"{name}: {rejected.stdout}")
            expected_errors = {
                "tampered-parent": "replay_parent_graph_invalid",
                "missing-parent": "replay_parent_graph_invalid",
                "unindexed-parent": "replay_parent_graph_invalid",
                "duplicate-index-entry": "replay_generation_index_duplicate",
                "incomplete-ancestor-set": "replay_parent_graph_invalid",
                "cross-a-admission": "replay_parent_graph_invalid",
                "cyclic-lineage": "replay_parent_graph_invalid",
            }
            self.assertIn(
                f"REJECTED:ValueError:{expected_errors[name]}", rejected.stdout,
                f"{name}: {rejected.stdout}",
            )

        parent_generation_id = env2["resume_parent_processor"]["generation_id"]
        parent_checkpoint_relative = (
            f".datapan/upstream-catalogue-state/sources/data_go_kr/generations/{parent_generation_id}.json"
        )
        tampered_parent_snapshot: list[bytes] = []

        def mutate_tampered_parent(state_root: pathlib.Path, _index_path: pathlib.Path) -> None:
            parent_path = state_root / "sources/data_go_kr/generations" / f"{parent_generation_id}.json"
            altered_checkpoint = PROCESSOR.load_json(parent_path)
            altered_checkpoint["last_heartbeat_at"] = "2026-10-01T00:00:01Z"
            altered = json.dumps(altered_checkpoint, sort_keys=True).encode("utf-8") + b"\n"
            tampered_parent_snapshot[:] = [altered]
            parent_path.write_bytes(altered)

        def mutate_missing_parent(state_root: pathlib.Path, _index_path: pathlib.Path) -> None:
            (state_root / "sources/data_go_kr/generations" / f"{parent_generation_id}.json").unlink()

        def mutate_unindexed_parent(state_root: pathlib.Path, index_path: pathlib.Path) -> None:
            del state_root
            index_value = PROCESSOR.load_json(index_path)
            index_value["generations"] = [
                row for row in index_value["generations"]
                if row.get("generation_id") != parent_generation_id
            ]
            index_path.write_text(json.dumps(index_value, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_duplicate_index_entry(state_root: pathlib.Path, index_path: pathlib.Path) -> None:
            del state_root
            index_value = PROCESSOR.load_json(index_path)
            target = next(row for row in index_value["generations"] if row["generation_id"] == b3["generation_id"])
            index_value["generations"].append(copy.deepcopy(target))
            index_path.write_text(json.dumps(index_value, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_incomplete_ancestor_set(state_root: pathlib.Path, _index_path: pathlib.Path) -> None:
            ancestor_id = env1["ancestor_generation_ids"][0]
            ancestor_path = state_root / "sources/data_go_kr/generations" / f"{b2['generation_id']}.json"
            ancestor_checkpoint = PROCESSOR.load_json(ancestor_path)
            ancestor_inputs = dict(ancestor_checkpoint["generation_inputs"])
            envelope = copy.deepcopy(ancestor_inputs["same_observation_derivation"])
            envelope["ancestor_generation_ids"].remove(ancestor_id)
            ancestor_inputs["same_observation_derivation"] = envelope
            ancestor_checkpoint["generation_inputs"] = ancestor_inputs
            PROCESSOR.seal_checkpoint(ancestor_checkpoint)
            ancestor_path.write_text(json.dumps(ancestor_checkpoint, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_cross_a_admission(state_root: pathlib.Path, index_path: pathlib.Path) -> None:
            index_value = PROCESSOR.load_json(index_path)
            fake_admission = copy.deepcopy(origin_admission)
            fake_run_id = str(int(fake_admission["producer_run_id"]) + 1000)
            fake_artifact_id = str(int(fake_admission["artifact_id"]) + 1000)
            fake_evidence_sha = "e" * 64
            fake_admission.update({
                "producer_run_id": fake_run_id,
                "run_started_at": "2026-10-02T00:00:01Z",
                "head_sha": "c" * 40,
                "artifact_id": fake_artifact_id,
                "artifact_name": f"upstream-catalog-refresh-{fake_run_id}",
                "artifact_digest_sha256": "d" * 64,
                "refresh_evidence_sha256": fake_evidence_sha,
                "generation_id": "f" * 64,
                "admission_id": HANDOFF.admission_id(
                    fake_run_id, fake_admission["run_attempt"], fake_evidence_sha,
                ),
            })
            ledger = HANDOFF.validate_ledger(index_value["collector_handoff"])
            ledger["admitted_observations"].append(fake_admission)
            ledger["admitted_observations"].sort(key=HANDOFF.admission_order_key)
            try:
                HANDOFF.validate_admission_row(fake_admission)
                HANDOFF.validate_ledger(ledger)
            except HANDOFF.HandoffError as exc:
                raise AssertionError(f"cross-A test admission is malformed: {exc}") from exc
            index_value["collector_handoff"] = ledger
            index_path.write_text(json.dumps(index_value, sort_keys=True) + "\n", encoding="utf-8")

            original_parent = PROCESSOR.load_json(
                state_root / "sources/data_go_kr/generations" / f"{b0['generation_id']}.json",
            )
            original_parent["last_observation"]["producer_run_id"] = fake_run_id
            original_parent["last_observation"]["refresh_evidence_sha256"] = fake_evidence_sha
            for reference in original_parent["input_artifacts"]:
                if reference.get("run_id") == origin_admission["producer_run_id"]:
                    reference.update({
                        "run_id": fake_run_id,
                        "artifact_id": fake_artifact_id,
                        "name": fake_admission["artifact_name"],
                        "evidence_sha256": fake_evidence_sha,
                    })
            PROCESSOR.seal_checkpoint(original_parent)
            (state_root / "sources/data_go_kr/generations" / f"{b0['generation_id']}.json").write_text(
                json.dumps(original_parent, sort_keys=True) + "\n", encoding="utf-8",
            )

        def mutate_cyclic_lineage(state_root: pathlib.Path, _index_path: pathlib.Path) -> None:
            cycle_envelope = copy.deepcopy(env2)
            cycle_envelope["resume_parent_processor"] = self._parent_ref(b2)
            cycle_envelope["canonical_parent_processor"] = self._parent_ref(b2)
            cycle_envelope["ancestor_generation_ids"] = sorted(set(env2["ancestor_generation_ids"]))
            DERIVATION.validate_derivation_envelope(cycle_envelope)
            original_parent = PROCESSOR.load_json(
                state_root / "sources/data_go_kr/generations" / f"{b0['generation_id']}.json",
            )
            original_parent["generation_inputs"] = dict(original_parent["generation_inputs"])
            original_parent["generation_inputs"]["same_observation_derivation"] = cycle_envelope
            PROCESSOR.seal_checkpoint(original_parent)
            (state_root / "sources/data_go_kr/generations" / f"{b0['generation_id']}.json").write_text(
                json.dumps(original_parent, sort_keys=True) + "\n", encoding="utf-8",
            )

        for variant_name, mutate in (
            ("tampered-parent", mutate_tampered_parent),
            ("missing-parent", mutate_missing_parent),
            ("unindexed-parent", mutate_unindexed_parent),
            ("duplicate-index-entry", mutate_duplicate_index_entry),
            ("incomplete-ancestor-set", mutate_incomplete_ancestor_set),
            ("cross-a-admission", mutate_cross_a_admission),
            ("cyclic-lineage", mutate_cyclic_lineage),
        ):
            assert_committed_lineage_rejected(variant_name, mutate)

        git_lineage_variant("checkout", "-q", "-B", "bad-terminal-current", replay_state_head_sha)
        terminal_state_root = lineage_variant_repository / ".datapan/upstream-catalogue-state"
        terminal_checkpoint_path = terminal_state_root / "sources/data_go_kr/generations" / f"{b3['generation_id']}.json"
        terminal_checkpoint = PROCESSOR.load_json(terminal_checkpoint_path)
        changed_time = dt.datetime.fromisoformat(terminal_checkpoint["last_heartbeat_at"].replace("Z", "+00:00"))
        terminal_checkpoint["last_heartbeat_at"] = (changed_time + dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        PROCESSOR.seal_checkpoint(terminal_checkpoint)
        terminal_checkpoint_path.write_text(json.dumps(terminal_checkpoint, sort_keys=True) + "\n", encoding="utf-8")
        terminal_index_path = terminal_state_root / "sources/data_go_kr/index.json"
        terminal_index = PROCESSOR.load_json(terminal_index_path)
        terminal_index_row = next(row for row in terminal_index["generations"] if row["generation_id"] == b3["generation_id"])
        terminal_index_row["updated_at"] = terminal_checkpoint["last_heartbeat_at"]
        terminal_index_path.write_text(json.dumps(terminal_index, sort_keys=True) + "\n", encoding="utf-8")
        git_lineage_variant("add", ".datapan/upstream-catalogue-state")
        git_lineage_variant("commit", "-q", "-m", "changed terminal replay checkpoint state")
        changed_terminal_sha = git_lineage_variant("rev-parse", "HEAD")
        changed_terminal_verification = self._run_fresh_replay_verifier(
            state_repository=lineage_variant_repository,
            prior_state_commit=replay_receipt["state_head_sha"],
            expected_current_state_commit=changed_terminal_sha,
            phase=PROCESSOR._REPLAY_VERIFY_POST_CAS,
            receipt=replay_receipt,
            result=replay_result,
            expect_rejection=True,
        )
        self.assertEqual(changed_terminal_verification.returncode, 0, changed_terminal_verification.stderr)
        self.assertIn("REJECTED:ValueError:replay_persistence_receipt_binding_mismatch", changed_terminal_verification.stdout)

        replay_args.replay_state_head_sha = replay_state_head_sha
        second_before_checkpoint = PROCESSOR.load_json(replay_checkpoint_path)
        second_before_index = PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")
        second_before_index_bytes = (self.helper.state_dir / "sources/data_go_kr/index.json").read_bytes()
        second_before_checkpoint_bytes = replay_checkpoint_path.read_bytes()
        self.assertEqual(
            subprocess.run(
                ("git", "show", f"{replay_state_head_sha}:.datapan/upstream-catalogue-state/sources/data_go_kr/index.json"),
                cwd=replay_state_repository, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            ).stdout,
            second_before_index_bytes,
        )
        self.assertEqual(
            subprocess.run(
                ("git", "show", f"{replay_state_head_sha}:.datapan/upstream-catalogue-state/sources/data_go_kr/generations/{b3['generation_id']}.json"),
                cwd=replay_state_repository, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            ).stdout,
            second_before_checkpoint_bytes,
        )
        second_calls = len(self.detail_calls)
        post_first_replay_now = self.now
        post_first_replay_now_text = self.now_text
        self.now += dt.timedelta(seconds=1)
        self.now_text = self.now.isoformat().replace("+00:00", "Z")
        with self._derivation_git(
            snapshot=journal_after_b2, journal_ref_sha=c2["readback"]["journal_ref_sha"], derivation=env2,
        ):
            second_code, second_replayed = PROCESSOR.process(
                replay_args, fetcher=self._fetch, sleeper=lambda _delay: None, clock=lambda: self.now,
                cache_source_reader=self._cache_source_reader(),
            )
        self.now = post_first_replay_now
        self.now_text = post_first_replay_now_text
        second_after_index = PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")
        self.assertEqual(second_code, 0)
        self.assertEqual(second_replayed["generation_id"], b3["generation_id"])
        self.assertEqual(second_replayed["output_artifact"], second_before_checkpoint["output_artifact"])
        self.assertEqual(second_replayed["output_digests"], second_before_checkpoint["output_digests"])
        self.assertEqual(second_replayed["attempts_consumed"], second_before_checkpoint["attempts_consumed"])
        self.assertEqual(second_replayed["attempts_by_id"], second_before_checkpoint["attempts_by_id"])
        self.assertEqual(second_replayed["request_reservation"], second_before_checkpoint["request_reservation"])
        self.assertEqual(second_after_index["collector_handoff"], second_before_index["collector_handoff"])
        self.assertEqual(second_after_index["detail_queue_cursor"], second_before_index["detail_queue_cursor"])
        self.assertEqual(second_after_index["detail_retry_state"], second_before_index["detail_retry_state"])
        self.assertEqual(len(self.detail_calls), second_calls)
        second_replay_context = replay_args.exact_delivery_replay_context
        second_replay_result = PROCESSOR.processing_result(
            second_replayed,
            exact_delivery_replay=True,
            producer_run_id=self.observation_run,
            processor_run_id=replay_args.processor_run_id,
            processor_artifact_run_id=replay_args.processor_artifact_run_id,
        )
        second_receipt_args = {
            "prior_checkpoint": second_before_checkpoint,
            "current_checkpoint": second_replayed,
            "prior_index": second_before_index,
            "current_index": second_after_index,
            "result": second_replay_result,
            "state_head_sha": replay_state_head_sha,
            "checkpoint_schema": schema,
            "validated_derivation_lineage": second_replay_context["validated_derivation_lineage"],
        }
        second_receipt = PROCESSOR.build_replay_persistence_receipt(**second_receipt_args)
        self.assertIs(
            PROCESSOR.verify_replay_persistence_receipt(second_receipt, **second_receipt_args),
            second_receipt,
        )

        # A pure same-observation recomposition can have no newly eligible
        # detail requests. It must still compose accumulated parent evidence,
        # while leaving the current-index retry cursor exactly untouched.
        # C still points at B0, while B3 carries later same-A contributions.
        pure_plan = self._prepare_authenticated_plan(
            b3, journal1, c1, label="pure-no-request-recomposition",
        )
        self.assertTrue(pure_plan["eligible"], pure_plan)
        pure_envelope = read_json(pathlib.Path(pure_plan["derivation_path"]))
        self.assertEqual(pure_envelope["resume_parent_processor"]["generation_id"], b3["generation_id"])
        self.assertEqual(pure_envelope["canonical_parent_processor"]["generation_id"], b0["generation_id"])
        index_path = self.helper.state_dir / "sources/data_go_kr/index.json"
        before_pure_index = PROCESSOR.load_json(index_path)
        before_pure_index["detail_queue_cursor"] = 248
        index_path.write_text(json.dumps(before_pure_index, sort_keys=True), encoding="utf-8")
        before_pure_calls = len(self.detail_calls)
        b4 = self._run_claim_and_worker(
            5, derivation=pure_envelope,
            composition_baseline=pathlib.Path(pure_plan["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[b3["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
            journal=journal1, journal_ref_sha=c1["readback"]["journal_ref_sha"],
        )
        after_pure_index = PROCESSOR.load_json(index_path)
        self.assertEqual(len(self.detail_calls), before_pure_calls)
        self.assertEqual(b4["attempts_consumed"], 0)
        self.assertEqual(b4["attempts_by_id"], {})
        self.assertEqual(after_pure_index["detail_queue_cursor"], 248)
        self.assertEqual(after_pure_index["detail_retry_state"], before_pure_index["detail_retry_state"])
        self.assertEqual(b4["observation_count"], b3["observation_count"])
        self.assertEqual(b4["observed_at"], b3["observed_at"])
        self._validate_c_derivation(b4, journal1, c1)

        # An old protected parent outside the recent index window is retained,
        # while enough unrelated generations are evicted to fit the cap.
        protected_ids = PROCESSOR.protected_lineage_generations(generation_dir)
        current_rows = {
            row["generation_id"]: row for row in after_pure_index["generations"]
            if isinstance(row, Mapping) and isinstance(row.get("generation_id"), str)
        }
        protected_rows = [current_rows[item] for item in protected_ids if item in current_rows]
        self.assertEqual({row["generation_id"] for row in protected_rows}, protected_ids)
        unrelated = []
        for number in range(70):
            unrelated_id = hashlib.sha256(f"unrelated-{number}".encode()).hexdigest()
            unrelated_checkpoint = copy.deepcopy(orphan)
            unrelated_checkpoint["generation_id"] = unrelated_id
            PROCESSOR.seal_checkpoint(unrelated_checkpoint)
            (generation_dir / f"{unrelated_id}.json").write_text(
                json.dumps(unrelated_checkpoint, sort_keys=True), encoding="utf-8",
            )
            unrelated.append({
                "generation_id": unrelated_id,
                "status": "no-change", "checkpoint": f"{unrelated_id}.json",
                "updated_at": self.now_text,
                "candidate_sha256": unrelated_checkpoint["generation_inputs"]["candidate_sha256"],
            })
        expanded = dict(after_pure_index)
        expanded["generations"] = protected_rows + unrelated
        index_path.write_text(json.dumps(expanded, sort_keys=True), encoding="utf-8")
        PROCESSOR.append_generation_index(
            index_path, generation_dir / f"{b4['generation_id']}.json", b4,
        )
        retained_index = PROCESSOR.load_json(index_path)
        retained_ids = {row["generation_id"] for row in retained_index["generations"]}
        self.assertEqual(len(retained_ids), PROCESSOR.DEFAULT_MAX_GENERATIONS)
        self.assertTrue(protected_ids.issubset(retained_ids))
        self.assertIn(unrelated[-1]["generation_id"], retained_ids)
        self.assertNotIn(unrelated[0]["generation_id"], retained_ids)

        # A new independent A still gets its own active slot after the later
        # B3 descendant has shadowed B1, the distinct resume parent of B2.
        # This catches implementations that subtract historical resume
        # exceptions only after unioning every derivation's ancestors.
        self.now += dt.timedelta(minutes=1)
        self.now_text = self.now.isoformat().replace("+00:00", "Z")
        self.observation_run = "99000000005"
        self.observation_artifact = "99000000006"
        self.observation_head = "c" * 40
        self._write_synthetic_observation(["9001", "9002", "9003", "9004"])
        self.admission_path, self.archive_path = self._write_admission()
        independent = self._run_claim_and_worker(
            6, max_attempts=1, max_queue=1, retries_per_detail=2,
        )
        self.assertIsNone(independent["generation_inputs"].get("same_observation_derivation"))
        self.assertGreater(independent["outcome"]["detail_retry_count"], 0)
        with mock.patch.object(PROCESSOR, "DEFAULT_MAX_ACTIVE_GENERATIONS", 1):
            retained = PROCESSOR.plan_generation_retention(
                PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json"),
                self.helper.state_dir / "sources/data_go_kr/generations",
                independent,
            )
        self.assertIn(b1["generation_id"], retained)
        self.assertIn(independent["generation_id"], retained)

    def test_single_current_b_can_fill_both_roles_and_continue_without_revision_change(self) -> None:
        b0 = self._run_claim_and_worker(1)
        self.assertGreater(b0["outcome"]["detail_unattempted_count"], 0)
        journal, c = self._synthetic_c_journal(b0, 1)
        plan = self._prepare_authenticated_plan(
            b0, journal, c, label="same-checkpoint-both-parent-roles",
        )
        self.assertTrue(plan["eligible"], plan)
        envelope = read_json(pathlib.Path(plan["derivation_path"]))
        self.assertEqual(
            envelope["resume_parent_processor"]["generation_id"],
            envelope["canonical_parent_processor"]["generation_id"],
        )
        b1 = self._run_claim_and_worker(
            2, derivation=envelope,
            composition_baseline=pathlib.Path(plan["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[b0["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
            journal=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
        )
        self.assertNotEqual(b1["generation_id"], b0["generation_id"])
        self.assertEqual(
            b1["generation_inputs"]["generator_revision"],
            b0["generation_inputs"]["generator_revision"],
        )
        self.assertEqual(b1["observation_count"], b0["observation_count"])
        self.assertEqual(b1["observed_at"], b0["observed_at"])
        self._validate_c_derivation(b1, journal, c)
        journal_before = DERIVATION.canonical_json(journal)
        # The normal C candidate selector screens the derivative through the
        # exact archived B artifact, compatibility path and strict C lineage
        # validator. Replaying selection stays deterministic and performs no
        # journal/owned-PR writes.
        for _attempt in range(2):
            selected, blocked, already_canonical, pr_reads = self._select_actual_derived_candidate(
                b1, journal, c,
            )
            self.assertIsNotNone(selected, {"blocked": blocked, "already_canonical": already_canonical})
            self.assertEqual(selected["generation_id"], b1["generation_id"])
            self.assertEqual(blocked, [])
            self.assertEqual(already_canonical, [])
            self.assertEqual(pr_reads, [journal["records"][0]["pr"]["number"]])
        self.assertEqual(DERIVATION.canonical_json(journal), journal_before)

        # Health must read the real sealed derivative checkpoint without
        # turning the same-A continuation into a second source observation or
        # claiming publication/read-back for its already-canonical payload.
        receipt = self._health_receipt_for_derived_candidate(b1, journal, c)
        source = receipt["sources"][0]
        self.assertEqual(receipt["summary"]["live_fresh_observation_count"], 1)
        self.assertEqual(source["observation"]["producer_run_id"], self.observation_run)
        self.assertEqual(source["observation"]["observed_at"], self.now_text)
        self.assertIsNone(source["canonical"]["already_canonical_candidate"])
        self.assertEqual(source["canonical"]["last_good"], self.expected_health_last_good)
        self.assertIsNone(source["canonical"]["publication"])

    def test_interrupted_derived_worker_keeps_precharged_reservation_and_a_clock(self) -> None:
        b0 = self._run_claim_and_worker(1)
        journal, c = self._synthetic_c_journal(b0, 1)
        plan = self._prepare_authenticated_plan(b0, journal, c, label="interrupted-derived-worker")
        self.assertTrue(plan["eligible"], plan)
        envelope = read_json(pathlib.Path(plan["derivation_path"]))

        args = self._args(2, max_attempts=3, max_queue=1, retries_per_detail=2)
        envelope_path = self.helper.root / "interrupted-derivation.json"
        envelope_path.write_bytes(DERIVATION.canonical_json(envelope) + b"\n")
        baseline_path = self.helper.root / "interrupted-composition-baseline.json"
        baseline_path.write_bytes(pathlib.Path(plan["composition_baseline_path"]).read_bytes())
        journal_path = self.helper.root / "interrupted-journal.json"
        journal_path.write_text(json.dumps(journal, sort_keys=True), encoding="utf-8")
        args.same_observation_derivation = envelope_path
        args.composition_baseline = baseline_path
        args.current_main_root = self.helper.root
        args.derivation_journal = journal_path
        args.derivation_journal_ref_sha = c["readback"]["journal_ref_sha"]
        args.resume_parent_bundle_dir = self.output_by_generation[b0["generation_id"]]
        args.canonical_parent_bundle_dir = self.output_by_generation[b0["generation_id"]]
        parent_archive = self._cache_source_archive_path(b0)
        args.resume_parent_archive = parent_archive
        args.canonical_parent_archive = parent_archive
        args.resume_enrichment_evidence = args.resume_parent_bundle_dir / "upstream-catalogue-enrichment-evidence.json"
        args.resume_source_context = args.resume_parent_bundle_dir / PROCESSOR.CACHE_SOURCE.CONTEXT_MEMBER
        args.resume_owner_archive = parent_archive
        args.canonical_update_pr_helper = ROOT / "scripts/canonical_update_pr.py"
        args.canonical_update_journal_schema = ROOT / "schemas/datapan.canonical-update-promotion-journal.v1.schema.json"
        processor_run = str(args.processor_run_id)
        fixed_clock = lambda: self.now

        class WorkerInterrupted(BaseException):
            pass

        fetched: list[str] = []

        def interrupt_after_page_slot(url: str, _timeout: float) -> Any:
            fetched.append(url)
            raise WorkerInterrupted("test interruption after durable charge")

        with (
            mock.patch.object(PROCESSOR, "generator_revision", wraps=PROCESSOR.generator_revision),
            mock.patch.object(PROCESSOR, "extractor_revision", wraps=PROCESSOR.extractor_revision),
            self._derivation_git(
                snapshot=journal, journal_ref_sha=c["readback"]["journal_ref_sha"], derivation=envelope,
            ),
        ):
            args.claim_only = True
            claim_code, claim = PROCESSOR.process(
                args, fetcher=self._fetch, sleeper=lambda _delay: None, clock=fixed_clock,
                cache_source_reader=self._cache_source_reader(),
            )
            self.assertEqual(claim_code, 0, json.dumps(claim, sort_keys=True))
            self.assertEqual(claim["status"], "enriching")
            self.assertEqual(claim["observation_count"], 1)
            self.assertEqual(claim["observed_at"], b0["observed_at"])
            self.assertEqual(claim["request_reservation"]["reserved_attempts"], 3)
            self.assertEqual(claim["request_reservation"]["attempts_made"], 0)
            self.assertEqual(claim["attempts_consumed"], 3)
            self.assertEqual(sum(claim["attempts_by_id"].values()), 3)

            args.claim_only = False
            args.require_durable_reservation = True
            with self.assertRaises(WorkerInterrupted):
                PROCESSOR.process(
                    args, fetcher=interrupt_after_page_slot, sleeper=lambda _delay: None, clock=fixed_clock,
                    cache_source_reader=self._cache_source_reader(),
                )

        self.assertEqual(len(fetched), 1)
        generation_path = self.helper.state_dir / "sources/data_go_kr/generations" / f"{claim['generation_id']}.json"
        interrupted = PROCESSOR.verify_checkpoint(
            PROCESSOR.load_json(generation_path),
            read_json(ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"),
        )
        self.assertEqual(interrupted["status"], "enriching")
        self.assertEqual(interrupted["observation_count"], 1)
        self.assertEqual(interrupted["observed_at"], b0["observed_at"])
        self.assertEqual(interrupted["request_reservation"]["owner_run_id"], processor_run)
        self.assertEqual(interrupted["request_reservation"]["reserved_attempts"], 3)
        self.assertEqual(interrupted["request_reservation"]["attempts_made"], 1)
        self.assertEqual(interrupted["attempts_consumed"], 3)
        self.assertEqual(sum(interrupted["attempts_by_id"].values()), 3)
        self.assertIsNone(interrupted["output_artifact"]["artifact_id"])
        self.assertIsNone(interrupted["output_artifact"]["bundle_manifest_sha256"])
        current_index = PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")
        self.assertEqual(current_index["detail_queue_cursor"], claim["detail_queue_cursor"])
        reserved_row = claim["request_reservation"]["records"][0]
        self.assertEqual(
            current_index["detail_retry_state"][reserved_row["id"]]["attempts"],
            reserved_row["attempts_reserved"],
        )
        self.assertEqual(
            current_index["detail_retry_state"][reserved_row["id"]]["source_sha256"],
            reserved_row["source_sha256"],
        )

    def test_derivation_parent_cache_rejects_stale_source_or_guide_binding(self) -> None:
        parent = self._run_claim_and_worker(1)
        evidence_path = self.output_by_generation[parent["generation_id"]] / "upstream-catalogue-enrichment-evidence.json"
        candidate_rows = copy.deepcopy(PROCESSOR.load_registry(self.helper.candidate_path))
        candidate_by_id = PROCESSOR.unique_rows(candidate_rows, "test candidate")
        first_identity = next(iter(candidate_by_id))
        raw = candidate_by_id[first_identity]["source"]["raw"]
        raw["guide_url"] = "https://www.data.go.kr/guide/changed-after-parent.pdf"
        registered_hosts = PROCESSOR.DETAIL_HELPERS.registered_hosts(
            PROCESSOR.load_json(self.helper.provider_index_path),
        )
        validation_args = {
            "path": evidence_path,
            "checkpoint": parent,
            "state_dir": self.helper.state_dir,
            "source_id": "data_go_kr",
            "checkpoint_schema": read_json(ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"),
            "provider_index_sha256": parent["generation_inputs"]["adapter_revision"],
            "candidate_by_id": candidate_by_id,
            "registered_hosts": registered_hosts,
            "now": self.now,
            "allow_parent_extractor_revision": True,
            "expected_owner_generation_id": parent["generation_id"],
            "repository": "StatPan/datapan-registry",
            "default_branch": "main",
            "cache_source_reader": self._cache_source_reader(),
        }
        # Changing the current row without changing the candidate identity is
        # corruption, not an ordinary newer-A cache miss.  Both normal and
        # same-A parent reads therefore fail closed.
        with self.assertRaisesRegex(ValueError, "resume_enrichment_source_or_guide_identity_mismatch"):
            PROCESSOR.validated_resume_records(**validation_args)
        with self.assertRaisesRegex(ValueError, "resume_enrichment_source_or_guide_identity_mismatch"):
            PROCESSOR.validated_resume_records(
                **validation_args, reject_stale_source_or_guide_contract=True,
            )

    def test_main_movement_after_intake_fails_before_claim_or_request(self) -> None:
        b0 = self._run_claim_and_worker(1)
        journal, c = self._synthetic_c_journal(b0, 1)
        plan = self._prepare_authenticated_plan(b0, journal, c, label="main-movement-after-intake")
        self.assertTrue(plan["eligible"], plan)
        envelope = read_json(pathlib.Path(plan["derivation_path"]))

        index_path = self.helper.state_dir / "sources/data_go_kr/index.json"
        index_before = index_path.read_bytes()
        generation_dir = self.helper.state_dir / "sources/data_go_kr/generations"
        generation_files_before = {path.name for path in generation_dir.glob("*.json")}
        requests_before = len(self.detail_calls)
        # The current main moved after intake authenticated the exact plan.
        # The local plan remains sealed, but claim must stop before mutation.
        self._run_claim_and_worker(
            2, derivation=envelope,
            composition_baseline=pathlib.Path(plan["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[b0["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
            journal=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
            current_main_sha="f" * 40, expected_claim_error="derivation_current_main_head_mismatch",
        )
        self.assertEqual(index_path.read_bytes(), index_before)
        self.assertEqual({path.name for path in generation_dir.glob("*.json")}, generation_files_before)
        self.assertEqual(len(self.detail_calls), requests_before)

    def test_conflicting_same_a_parent_operation_contribution_quarantines_before_io(self) -> None:
        b0 = self._run_claim_and_worker(1)
        journal, c = self._synthetic_c_journal(b0, 1)
        index_path = self.helper.state_dir / "sources/data_go_kr/index.json"
        index = PROCESSOR.load_json(index_path)
        index["detail_queue_cursor"] = 0
        index_path.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")

        # A processor/extractor revision refreshes one API's operation route
        # under the same immutable A. Both outputs are valid parent archives,
        # but they disagree about one source/guide-bound contribution.
        self.operation_endpoint_overrides["1"] = "https://openapi.airport.co.kr/alternate/1"
        b1 = self._run_claim_and_worker(
            2,
            resume=self.output_by_generation[b0["generation_id"]] / "upstream-catalogue-enrichment-evidence.json",
            generator_revision=self.alternate_generator_revision,
            extractor_revision_value=self.alternate_extractor_revision,
        )
        b0_evidence = read_json(self.output_by_generation[b0["generation_id"]] / "upstream-catalogue-enrichment-evidence.json")
        b1_evidence = read_json(self.output_by_generation[b1["generation_id"]] / "upstream-catalogue-enrichment-evidence.json")
        b0_record = next(row for row in b0_evidence["records"] if row["api_key"]["id"] == "1")
        b1_record = next(row for row in b1_evidence["records"] if row["api_key"]["id"] == "1")
        self.assertEqual(b0_record["source_sha256"], b1_record["source_sha256"])
        self.assertEqual(b0_record["guide_sha256"], b1_record["guide_sha256"])
        self.assertNotEqual(b0_record["operations_sha256"], b1_record["operations_sha256"])

        plan = self._prepare_authenticated_plan(b1, journal, c, label="conflicting-parent-operation")
        self.assertTrue(plan["eligible"], plan)
        envelope = read_json(pathlib.Path(plan["derivation_path"]))
        requests_before = len(self.detail_calls)
        # Contribution conflicts are discovered in the actual claim path
        # before a reservation is created. Expose the stable internal reason
        # in this isolated test, while production continues to persist only
        # the bounded exception class.
        with mock.patch.object(PROCESSOR, "safe_error_class", side_effect=lambda error: str(error)):
            b2 = self._run_claim_and_worker(
                3, derivation=envelope,
                composition_baseline=pathlib.Path(plan["composition_baseline_path"]),
                resume_parent_bundle=self.output_by_generation[b1["generation_id"]],
                canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
                journal=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
                expected_claim_code=3, expected_claim_status="quarantined",
            )
        self.assertEqual(len(self.detail_calls), requests_before)
        self.assertEqual(b2["observation_count"], b1["observation_count"])
        self.assertEqual(b2["observed_at"], b1["observed_at"])
        self.assertEqual(b2["outcome"]["reason"], "derivation_parent_enrichment_contribution_conflict")
        self.assertEqual(b2["attempts_consumed"], 0)

    def test_same_observation_candidate_cannot_delete_a_merged_operation_without_evidence(self) -> None:
        b0 = self._run_claim_and_worker(1)
        baseline_path = self.output_by_generation[b0["generation_id"]] / "composed-candidate.registry.json"
        baseline_bytes = baseline_path.read_bytes()
        baseline_rows = json.loads(baseline_bytes)
        baseline_row = next(row for row in baseline_rows if row["id"] == "1")
        self.assertTrue(baseline_row["operations"])

        # A same-A source snapshot that simply omits an operation is not
        # authoritative deletion evidence. Run the real composer with that
        # candidate and the exact prior canonical bytes; the old operation
        # must remain in the result while the omission stays visible.
        candidate_rows = copy.deepcopy(baseline_rows)
        candidate_row = next(row for row in candidate_rows if row["id"] == "1")
        candidate_row["operations"] = []
        candidate_bytes = COMPOSER.canonical_json_bytes(candidate_rows)
        provider_index_bytes = (ROOT / "data/provider-index.json").read_bytes()
        result = COMPOSER.compose_registries(
            baseline_rows,
            candidate_rows,
            PROCESSOR.load_json(ROOT / "data/provider-index.json"),
            baseline_sha256=hashlib.sha256(baseline_bytes).hexdigest(),
            candidate_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
            provider_index_sha256=hashlib.sha256(provider_index_bytes).hexdigest(),
        )

        composed_row = next(row for row in result["composed_registry"] if row["id"] == "1")
        decision = next(row for row in result["semantic_diff"]["api_decisions"] if row["api_key"]["id"] == "1")
        self.assertEqual(composed_row["operations"], baseline_row["operations"])
        self.assertEqual(decision["disposition"], "unchanged")
        self.assertIn("retained_enrichment_not_refreshed", decision["tags"])
        self.assertEqual(len(self.detail_calls), 1)

    def test_c_accepts_source_only_main_advance_with_same_authenticated_canonical_bytes(self) -> None:
        b0 = self._run_claim_and_worker(1)
        journal, c = self._synthetic_c_journal(b0, 1)
        # Intake itself sees a source-only main advance after C's original
        # merge. Its new manifest still names exactly the same canonical
        # registry bytes and pointer.
        prepared_main_sha = "d" * 40
        prepared_manifest_value = json.loads(self.current_manifests[c["baseline"]["main_sha"]])
        prepared_manifest_value["test_only_source_commit"] = "source-only-before-intake"
        prepared_manifest = DERIVATION.canonical_json(prepared_manifest_value)
        self.current_manifests[prepared_main_sha] = prepared_manifest
        plan = self._prepare_authenticated_plan(
            b0, journal, c, label="source-only-main-advance",
            current_main_sha=prepared_main_sha, current_manifest_bytes=prepared_manifest,
        )
        self.assertTrue(plan["eligible"], plan)
        envelope = read_json(pathlib.Path(plan["derivation_path"]))
        b1 = self._run_claim_and_worker(
            2, derivation=envelope,
            composition_baseline=pathlib.Path(plan["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[b0["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
            journal=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
        )

        # Model a second source-only main commit after B claim: its manifest
        # digest changes again while canonical bytes remain identical.
        source_only_sha = "c" * 40
        previous_manifest = json.loads(prepared_manifest)
        previous_manifest["test_only_source_commit"] = "source-only-after-claim"
        source_only_manifest = DERIVATION.canonical_json(previous_manifest)
        self.current_manifests[source_only_sha] = source_only_manifest
        checkout = self.helper.root / "authenticated-source-only-main"
        checkout.mkdir()
        (checkout / "manifest.json").write_bytes(source_only_manifest)
        current_canonical = checkout / ".datapan/current-canonical" / c["baseline"]["registry_path"]
        current_canonical.parent.mkdir(parents=True)
        current_canonical.write_bytes(c["payload"])
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{c['baseline']['registry_sha256']}\n"
            f"size {c['baseline']['registry_bytes']}\n"
        )

        def source_only_command(argv, root, **_kwargs):
            del root
            command = tuple(str(part) for part in argv)
            if command == ("git", "rev-parse", "HEAD"):
                stdout = source_only_sha
            elif command == ("git", "ls-remote", "--heads", "origin", "refs/heads/main"):
                stdout = f"{source_only_sha}\trefs/heads/main\n"
            elif command == ("git", "show", f"{source_only_sha}:manifest.json"):
                stdout = source_only_manifest.decode("utf-8")
            elif command == ("git", "show", f"{source_only_sha}:{c['baseline']['registry_path']}"):
                stdout = pointer
            else:
                raise AssertionError(f"unexpected source-only identity command: {command!r}")
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        with mock.patch.object(PROMOTION, "command", side_effect=source_only_command):
            current_identity = PROMOTION.authenticated_current_canonical_registry(checkout, source_only_sha)
        self.assertEqual(current_identity["main_sha"], source_only_sha)
        self.assertNotEqual(
            current_identity["manifest_sha256"],
            b1["generation_inputs"]["same_observation_derivation"]["composition_baseline"]["manifest_sha256"],
        )
        self.assertEqual(current_identity["registry_sha256"], c["baseline"]["registry_sha256"])
        self.assertEqual(current_identity["registry_bytes"], c["baseline"]["registry_bytes"])
        b1_envelope = b1["generation_inputs"]["same_observation_derivation"]
        self.assertEqual(
            b1_envelope["composition_baseline"]["registry_sha256"], current_identity["registry_sha256"],
        )
        self.assertEqual(
            b1_envelope["composition_baseline"]["registry_bytes"], current_identity["registry_bytes"],
        )
        self.assertEqual(
            b1_envelope["composition_baseline"]["registry_path"], current_identity["registry_path"],
        )
        cache_source_helper, cache_source_contract = self._authenticated_cache_source_contract(b1)
        bundle = PROMOTION.validate_processor_bundle(
            b1, self.output_by_generation[b1["generation_id"]],
            read_json(ROOT / "schemas/datapan.catalogue-composition-receipt.v1.schema.json"),
            PROMOTION.load_canonical_update_pr(ROOT), root=ROOT,
            cache_source_helper=cache_source_helper,
            cache_source_contract=cache_source_contract,
        )
        original = b1_envelope["original_observation"]
        baseline = b1_envelope["composition_baseline"]
        self.assertEqual(b1["source_id"], original["source_id"])
        self.assertEqual(b1["source_scope"], original["source_scope"])
        self.assertEqual(b1["generation_inputs"]["baseline_sha256"], original["original_baseline_sha256"])
        self.assertEqual(b1["generation_inputs"]["candidate_sha256"], original["candidate_sha256"])
        self.assertEqual(b1["generation_inputs"]["policy_sha256"], original["source_policy_sha256"])
        self.assertEqual(b1["generation_inputs"]["adapter_revision"], original["provider_index_sha256"])
        self.assertEqual(bundle["original_baseline_sha256"], original["original_baseline_sha256"])
        self.assertEqual(bundle["baseline_sha256"], baseline["registry_sha256"])
        self.assertEqual(
            (baseline["registry_path"], baseline["registry_bytes"], baseline["registry_sha256"]),
            (current_identity["registry_path"], current_identity["registry_bytes"], current_identity["registry_sha256"]),
        )
        self._validate_c_derivation(
            b1, journal, c, current_head_sha=source_only_sha,
            canonical_identity=current_identity,
        )

        # A current source manifest and materialization may both be internally
        # consistent yet still describe different canonical bytes. C must not
        # treat that as a harmless source-only advance.
        changed_sha = "e" * 40
        changed_payload = c["payload"] + b"\n"
        changed_manifest_value = copy.deepcopy(previous_manifest)
        changed_manifest_value["artifacts"][0].update({
            "bytes": len(changed_payload), "sha256": hashlib.sha256(changed_payload).hexdigest(),
        })
        changed_manifest = DERIVATION.canonical_json(changed_manifest_value)
        self.current_manifests[changed_sha] = changed_manifest
        changed_checkout = self.helper.root / "authenticated-changed-canonical-main"
        changed_checkout.mkdir()
        (changed_checkout / "manifest.json").write_bytes(changed_manifest)
        changed_current = changed_checkout / ".datapan/current-canonical" / c["baseline"]["registry_path"]
        changed_current.parent.mkdir(parents=True)
        changed_current.write_bytes(changed_payload)
        changed_pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{hashlib.sha256(changed_payload).hexdigest()}\n"
            f"size {len(changed_payload)}\n"
        )

        def changed_command(argv, root, **_kwargs):
            del root
            command = tuple(str(part) for part in argv)
            if command == ("git", "rev-parse", "HEAD"):
                stdout = changed_sha
            elif command == ("git", "ls-remote", "--heads", "origin", "refs/heads/main"):
                stdout = f"{changed_sha}\trefs/heads/main\n"
            elif command == ("git", "show", f"{changed_sha}:manifest.json"):
                stdout = changed_manifest.decode("utf-8")
            elif command == ("git", "show", f"{changed_sha}:{c['baseline']['registry_path']}"):
                stdout = changed_pointer
            else:
                raise AssertionError(f"unexpected changed identity command: {command!r}")
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        with mock.patch.object(PROMOTION, "command", side_effect=changed_command):
            changed_identity = PROMOTION.authenticated_current_canonical_registry(changed_checkout, changed_sha)
        with self.assertRaisesRegex(PROMOTION.PromotionError, "same-observation lineage failed strict C validation"):
            self._validate_c_derivation(
                b1, journal, c, current_head_sha=changed_sha,
                canonical_identity=changed_identity,
            )

    def test_native_248_attempt_distribution_bounds_derived_claim_and_worker(self) -> None:
        native_checkpoint = read_json(FIXTURES / "native-b-9938.json")
        native_attempts = native_checkpoint["attempts_by_id"]
        self.assertEqual(len(native_attempts), 248)
        self.assertEqual(sum(native_attempts.values()), 264)
        self.assertEqual({count: list(native_attempts.values()).count(count) for count in set(native_attempts.values())}, {1: 240, 3: 8})

        # Reuse the native 248 identity/count distribution with clearly
        # synthetic catalogue bytes. No provider data is represented here.
        identities = sorted(native_attempts)
        self._write_synthetic_observation(identities)
        self.admission_path, self.archive_path = self._write_admission()
        b0 = self._run_claim_and_worker(1, max_attempts=24, max_queue=48)
        self.assertEqual(b0["request_reservation"]["attempts_made"], 8)
        journal, c = self._synthetic_c_journal(b0, 1)
        plan = self._prepare_authenticated_plan(
            b0, journal, c, label="native-248-derived-accounting",
        )
        self.assertTrue(plan["eligible"], plan)
        envelope = read_json(pathlib.Path(plan["derivation_path"]))

        # Inject the retained native retry-accounting distribution into the
        # current source index. This is the authoritative current index for
        # the derived claim; the parent checkpoint's older counters cannot
        # reset or refill it.
        candidate_rows = PROCESSOR.load_registry(self.helper.candidate_path)
        candidate_by_id = {PROCESSOR.record_id(row): row for row in candidate_rows}
        index_path = self.helper.state_dir / "sources/data_go_kr/index.json"
        index = PROCESSOR.load_json(index_path)
        index["detail_queue_cursor"] = 248
        index["detail_retry_state"] = {
            identity: {
                "source_sha256": PROCESSOR.source_fingerprint(candidate_by_id[identity]),
                "guide_sha256": PROCESSOR.guide_fingerprint(candidate_by_id[identity]),
                "attempts": count, "last_attempt_at": self.now_text,
            }
            for identity, count in native_attempts.items()
        }
        index_path.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")

        successful_parent_ids = {
            row["api_key"]["id"] for row in json.loads(
                (self.output_by_generation[b0["generation_id"]] / "upstream-catalogue-enrichment-evidence.json").read_text()
            )["records"]
        }
        queued_ids = sorted(set(identities) - successful_parent_ids)
        rotated = queued_ids[248 % len(queued_ids):] + queued_ids[:248 % len(queued_ids)]
        expected_reservations: dict[str, int] = {}
        inspected = []
        budget_left = 24
        eligible_inspected = 0
        for identity in rotated:
            inspected.append(identity)
            allocation = min(3 - native_attempts[identity], budget_left)
            if allocation > 0:
                eligible_inspected += 1
                expected_reservations[identity] = allocation
                budget_left -= allocation
            if budget_left == 0 or eligible_inspected >= 48:
                break
        self.assertEqual(sum(expected_reservations.values()), 24)

        request_calls: list[str] = []
        per_identity_calls: dict[str, int] = {}

        def fail_once_then_succeed(url: str, timeout: float) -> Any:
            identity = url.split("/data/", 1)[1].split("/", 1)[0]
            request_calls.append(identity)
            per_identity_calls[identity] = per_identity_calls.get(identity, 0) + 1
            if per_identity_calls[identity] == 1:
                raise TimeoutError("test-only first physical request failure")
            return self._fetch(url, timeout)

        before_detail_calls = len(self.detail_calls)
        b1 = self._run_claim_and_worker(
            2, derivation=envelope,
            composition_baseline=pathlib.Path(plan["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[b0["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
            journal=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
            max_attempts=24, max_queue=48, retries_per_detail=2,
            fetcher=fail_once_then_succeed,
        )
        actual_reservations = {
            row["id"]: row["attempts_reserved"]
            for row in b1["request_reservation"]["records"]
        }
        self.assertEqual(actual_reservations, expected_reservations)
        self.assertEqual(b1["attempts_consumed"], 24)
        self.assertEqual(b1["request_reservation"]["attempts_made"], 24)
        self.assertEqual(len(request_calls), 24)
        self.assertEqual(set(request_calls), set(expected_reservations))
        self.assertTrue(all(per_identity_calls[item] == 2 for item in expected_reservations))
        exhausted = {identity for identity, count in native_attempts.items() if count == 3}
        self.assertTrue(exhausted.isdisjoint(actual_reservations))
        self.assertTrue(all(b1["attempts_by_id"].get(identity, 0) <= 3 for identity in identities))
        self.assertEqual(
            b1["detail_queue_cursor"],
            (248 + len(inspected)) % len(queued_ids),
        )
        self.assertEqual(len(self.detail_calls) - before_detail_calls, 12)
        self._validate_c_derivation(b1, journal, c)

    def test_production_slice_scans_past_exhausted_48_prefix_without_starving_eligible_row(self) -> None:
        identities = [str(value) for value in range(1, 80)]
        self._write_synthetic_observation(identities)
        self.admission_path, self.archive_path = self._write_admission()
        b0 = self._run_claim_and_worker(1, max_attempts=1, max_queue=1, retries_per_detail=2)
        journal, c = self._synthetic_c_journal(b0, 9)
        plan = self._prepare_authenticated_plan(b0, journal, c, label="exhausted-prefix-48")
        self.assertTrue(plan["eligible"], plan)
        envelope = read_json(pathlib.Path(plan["derivation_path"]))

        candidate_by_id = {
            PROCESSOR.record_id(row): row for row in PROCESSOR.load_registry(self.helper.candidate_path)
        }
        parent_ids = {
            row["api_key"]["id"] for row in json.loads(
                (self.output_by_generation[b0["generation_id"]] / "upstream-catalogue-enrichment-evidence.json").read_text()
            )["records"]
        }
        queue_ids = sorted(set(identities) - parent_ids)
        self.assertGreaterEqual(len(queue_ids), 49)
        exhausted_prefix = queue_ids[:48]
        expected_identity = queue_ids[48]
        index_path = self.helper.state_dir / "sources/data_go_kr/index.json"
        index = PROCESSOR.load_json(index_path)
        index["detail_queue_cursor"] = 0
        index["detail_retry_state"] = {
            identity: {
                "source_sha256": PROCESSOR.source_fingerprint(candidate_by_id[identity]),
                "guide_sha256": PROCESSOR.guide_fingerprint(candidate_by_id[identity]),
                "attempts": 3,
                "last_attempt_at": self.now_text,
            }
            for identity in exhausted_prefix
        }
        index_path.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")

        calls: list[str] = []
        reservation_at_call: list[dict[str, Any]] = []

        def count_selected(url: str, timeout: float) -> Any:
            calls.append(url.split("/data/", 1)[1].split("/", 1)[0])
            active = PROCESSOR.load_json(
                self.helper.state_dir / "sources/data_go_kr/generations" / f"{plan['generation_id']}.json",
            )
            reservation_at_call.append(copy.deepcopy(active["request_reservation"]))
            raise TimeoutError("bounded fixture timeout")

        b1 = self._run_claim_and_worker(
            2, derivation=envelope,
            expected_generation_id=plan["generation_id"],
            composition_baseline=pathlib.Path(plan["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[b0["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
            journal=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
            max_attempts=3, max_queue=48, retries_per_detail=2,
            fetcher=count_selected,
            expected_worker_code=2, expected_worker_status="retry",
        )
        self.assertEqual(b1["request_reservation"]["attempt_budget"], 3)
        self.assertEqual(reservation_at_call[0]["reserved_attempts"], 3)
        self.assertEqual(reservation_at_call[0]["attempts_made"], 1)
        self.assertEqual(b1["request_reservation"]["reserved_attempts"], 3)
        self.assertEqual(b1["request_reservation"]["attempts_made"], 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual([row["id"] for row in b1["request_reservation"]["records"]], [expected_identity])
        self.assertEqual(set(calls), {expected_identity})
        self.assertEqual(b1["detail_queue_cursor"], 49 % len(queue_ids))
        self.assertTrue(all(b1["attempts_by_id"][identity] == 3 for identity in exhausted_prefix))
        self.assertEqual(b1["attempts_by_id"][expected_identity], 3)

    def test_active_derived_generation_rehydrates_exact_plan_and_resumes_fenced_claim(self) -> None:
        identities = [str(value) for value in range(1, 33)]
        self._write_synthetic_observation(identities)
        self.admission_path, self.archive_path = self._write_admission()
        b0 = self._run_claim_and_worker(1)
        journal, c = self._synthetic_c_journal(b0, 10)
        initial_plan = self._prepare_authenticated_plan(b0, journal, c, label="active-derived-initial")
        self.assertTrue(initial_plan["eligible"], initial_plan)
        original_envelope = read_json(pathlib.Path(initial_plan["derivation_path"]))

        interrupted_claim = self._run_claim_and_worker(
            2, derivation=original_envelope,
            expected_generation_id=initial_plan["generation_id"],
            composition_baseline=pathlib.Path(initial_plan["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[b0["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
            journal=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
            max_attempts=3, max_queue=1, retries_per_detail=2,
            claim_only_return=True,
        )
        self.assertEqual(interrupted_claim["status"], "enriching")
        self.assertEqual(interrupted_claim["generation_id"], initial_plan["generation_id"])
        self.assertEqual(interrupted_claim["request_reservation"]["reserved_attempts"], 3)
        self.assertEqual(interrupted_claim["request_reservation"]["attempts_made"], 0)
        self.assertEqual(interrupted_claim["observation_count"], 1)
        self.assertEqual(interrupted_claim["observed_at"], b0["observed_at"])

        checkpoint_path = self.helper.state_dir / "sources/data_go_kr/generations" / f"{interrupted_claim['generation_id']}.json"
        active_on_disk = PROCESSOR.verify_checkpoint(
            PROCESSOR.load_json(checkpoint_path),
            read_json(ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"),
        )
        # Model elapsed lease time without touching durable retry accounting.
        active_on_disk["lease"]["expires_at"] = PROCESSOR.timestamp(
            self.now - dt.timedelta(seconds=1),
        )
        PROCESSOR.atomic_write_json(checkpoint_path, PROCESSOR.seal_checkpoint(active_on_disk))
        before_retry_state = copy.deepcopy(
            PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")["detail_retry_state"],
        )

        resumed_plan = self._prepare_authenticated_plan(
            active_on_disk, journal, c, label="active-derived-resume",
        )
        self.assertTrue(resumed_plan["eligible"], resumed_plan)
        self.assertTrue(resumed_plan["active_generation_resume"])
        self.assertEqual(resumed_plan["generation_id"], interrupted_claim["generation_id"])
        resumed_envelope = read_json(pathlib.Path(resumed_plan["derivation_path"]))
        self.assertEqual(DERIVATION.canonical_json(resumed_envelope), DERIVATION.canonical_json(original_envelope))
        self.assertEqual(pathlib.Path(resumed_plan["composition_baseline_path"]).read_bytes(), pathlib.Path(initial_plan["composition_baseline_path"]).read_bytes())

        calls: list[str] = []

        def record_resumed_call(url: str, timeout: float) -> Any:
            calls.append(url.split("/data/", 1)[1].split("/", 1)[0])
            return self._fetch(url, timeout)

        resumed = self._run_claim_and_worker(
            3, derivation=resumed_envelope,
            expected_generation_id=interrupted_claim["generation_id"],
            composition_baseline=pathlib.Path(resumed_plan["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[b0["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
            journal=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
            max_attempts=3, max_queue=48, retries_per_detail=2,
            fetcher=record_resumed_call,
        )
        self.assertEqual(resumed["generation_id"], interrupted_claim["generation_id"])
        self.assertGreater(resumed["fencing_token"], interrupted_claim["fencing_token"])
        self.assertEqual(resumed["observation_count"], 1)
        self.assertEqual(resumed["observed_at"], b0["observed_at"])
        self.assertEqual(resumed["attempts_consumed"], 4)
        self.assertEqual(resumed["attempts_by_id"], PROCESSOR.load_json(checkpoint_path)["attempts_by_id"])
        self.assertTrue(all(
            resumed["attempts_by_id"].get(identity, 0) == count
            for identity, count in interrupted_claim["attempts_by_id"].items()
        ))
        self.assertTrue(all(
            PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")["detail_retry_state"].get(identity, {}).get("attempts") == row["attempts"]
            for identity, row in before_retry_state.items()
            if identity in PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")["detail_retry_state"]
        ))
        self.assertEqual(
            resumed["generation_inputs"]["same_observation_derivation"],
            original_envelope,
        )
        self.assertEqual(len(calls), 1)

    def test_active_derived_prerequisite_loss_fails_before_legacy_claim_or_state_change(self) -> None:
        b0 = self._run_claim_and_worker(1)
        journal, c = self._synthetic_c_journal(b0, 20)
        initial = self._prepare_authenticated_plan(b0, journal, c, label="active-prerequisite-seed")
        self.assertTrue(initial["eligible"], initial)
        envelope = read_json(pathlib.Path(initial["derivation_path"]))
        b1 = self._run_claim_and_worker(
            2, derivation=envelope, expected_generation_id=initial["generation_id"],
            composition_baseline=pathlib.Path(initial["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[b0["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
            journal=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
        )
        self.assertIsNotNone(b1["generation_inputs"].get("same_observation_derivation"))
        self.assertEqual(b1["status"], "ready")
        self.assertGreater(b1["outcome"]["detail_retry_count"], 0)

        state_index = self.helper.state_dir / "sources/data_go_kr/index.json"
        checkpoint_path = self.helper.state_dir / "sources/data_go_kr/generations" / f"{b1['generation_id']}.json"
        before_index = state_index.read_bytes()
        before_checkpoint = checkpoint_path.read_bytes()
        calls_before = (len(self.claim_runs), len(self.detail_calls))

        unrelated_identity = dict(c["baseline"])
        unrelated_identity.update({
            "manifest_sha256": "f" * 64,
            "registry_sha256": "e" * 64,
            "registry_bytes": c["baseline"]["registry_bytes"] + 1,
        })
        unrelated_journal = copy.deepcopy(journal)
        unrelated_journal["records"][0]["status"] = "pending-review"

        blocked_cases = (
            ("journal-unavailable", journal, {"journal_unavailable": True}, "canonical_journal_unavailable"),
            ("canonical-row-missing", unrelated_journal, {}, "no_same_observation_merged_canonical_lineage"),
            ("unrelated-current-canonical", journal, {"current_identity_override": unrelated_identity}, "authenticated current-canonical bytes changed after manifest verification"),
        )
        for label, current_journal, options, reason in blocked_cases:
            expected_reason = (
                reason if label == "unrelated-current-canonical"
                else f"active_derivation_prerequisite_unavailable:{reason}"
            )
            with self.subTest(case=label), self.assertRaisesRegex(
                ValueError, expected_reason,
            ):
                self._prepare_authenticated_plan(
                    b1, current_journal, c, label=f"active-prerequisite-{label}", **options,
                )
            self.assertEqual(state_index.read_bytes(), before_index)
            self.assertEqual(checkpoint_path.read_bytes(), before_checkpoint)
            self.assertEqual((len(self.claim_runs), len(self.detail_calls)), calls_before)
            output_dir = self.helper.root / f"prepared-output-active-prerequisite-{label}"
            self.assertFalse(output_dir.exists())

    def test_ninth_same_observation_continuation_keeps_shadowed_parents_out_of_active_slots(self) -> None:
        # One fresh A and unchanged processor/extractor code produce eight
        # derived generations. Each parent remains ready with retry work, but
        # the authenticated derivation lineage makes those old outputs
        # shadowed by the next same-A candidate in the canonical selector.
        self._write_synthetic_observation([str(value) for value in range(1, 16)])
        self.admission_path, self.archive_path = self._write_admission()
        current = self._run_claim_and_worker(1, max_attempts=1, max_queue=1, retries_per_detail=2)
        generator_revision = current["generation_inputs"]["generator_revision"]
        extractor_revision = current["generation_inputs"]["extractor_revision"]
        journal, c = self._synthetic_c_journal(current, 100)
        canonical = current
        generated = [current]

        for continuation in range(1, 9):
            plan = self._prepare_authenticated_plan(
                current, journal, c, label=f"ninth-continuation-{continuation}-prepare",
            )
            self.assertTrue(plan["eligible"], plan)
            derivation = read_json(pathlib.Path(plan["derivation_path"]))
            next_checkpoint = self._run_claim_and_worker(
                continuation + 1,
                derivation=derivation,
                expected_generation_id=plan["generation_id"],
                composition_baseline=pathlib.Path(plan["composition_baseline_path"]),
                resume_parent_bundle=self.output_by_generation[current["generation_id"]],
                canonical_parent_bundle=self.output_by_generation[canonical["generation_id"]],
                journal=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
                max_attempts=1, max_queue=1, retries_per_detail=2,
            )
            self.assertEqual(next_checkpoint["generation_id"], plan["generation_id"])
            self.assertEqual(next_checkpoint["status"], "ready")
            self.assertGreater(next_checkpoint["outcome"]["detail_retry_count"], 0)
            self.assertEqual(next_checkpoint["generation_inputs"]["generator_revision"], generator_revision)
            self.assertEqual(next_checkpoint["generation_inputs"]["extractor_revision"], extractor_revision)
            self.assertEqual(next_checkpoint["observation_count"], 1)
            self.assertEqual(next_checkpoint["observed_at"], self.now_text)
            generated.append(next_checkpoint)

            # Add a normal authenticated subsequent C row while retaining the
            # prior history. The current readback index/ref is the only row
            # that can authorize the next composition baseline.
            next_journal, next_c = self._synthetic_c_journal(next_checkpoint, 100 + continuation)
            journal["records"].extend(copy.deepcopy(next_journal["records"]))
            journal["updated_at"] = self.now_text
            next_c["readback"] = DERIVATION.canonical_parent_readback_reference(
                journal,
                journal_ref_sha=next_c["readback"]["journal_ref_sha"],
                record_index=len(journal["records"]) - 1,
            )
            current = next_checkpoint
            canonical = next_checkpoint
            c = next_c

        self.assertEqual(len(generated), 9)
        self.assertEqual(len(self.claim_runs), 9)
        self.assertEqual(len(self.detail_calls), 9)
        self.assertEqual({checkpoint["observation_count"] for checkpoint in generated}, {1})
        self.assertEqual({checkpoint["observed_at"] for checkpoint in generated}, {self.now_text})
        protected = PROCESSOR.protected_lineage_generations(
            self.helper.state_dir / "sources/data_go_kr/generations",
        )
        self.assertTrue({checkpoint["generation_id"] for checkpoint in generated[:-1]}.issubset(protected))

        # An independent new A has no derivation envelope, but the authenticated
        # old lineage still makes its eight shadowed ancestors non-selectable.
        # Keep the current old-A retry candidate active alongside the new-A
        # claim, and retain every historical parent file for audit/replay.
        prior_generation_ids = {checkpoint["generation_id"] for checkpoint in generated}
        self.now += dt.timedelta(minutes=1)
        self.now_text = self.now.isoformat().replace("+00:00", "Z")
        self.observation_run = "99000000003"
        self.observation_artifact = "99000000004"
        self.observation_head = "b" * 40
        self._write_synthetic_observation(["9001", "9002", "9003", "9004"])
        self.admission_path, self.archive_path = self._write_admission()
        independent = self._run_claim_and_worker(
            10, max_attempts=1, max_queue=1, retries_per_detail=2,
        )
        self.assertIsNone(independent["generation_inputs"].get("same_observation_derivation"))
        self.assertEqual(independent["status"], "ready")
        self.assertGreater(independent["outcome"]["detail_retry_count"], 0)
        self.assertEqual(independent["last_observation"]["producer_run_id"], self.observation_run)
        index = PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")
        generation_dir = self.helper.state_dir / "sources/data_go_kr/generations"
        self.assertTrue(prior_generation_ids.issubset({row["generation_id"] for row in index["generations"]}))
        self.assertTrue(all((generation_dir / f"{generation_id}.json").is_file() for generation_id in prior_generation_ids))
        with mock.patch.object(PROCESSOR, "DEFAULT_MAX_ACTIVE_GENERATIONS", 2):
            retained = PROCESSOR.plan_generation_retention(index, generation_dir, independent)
        self.assertTrue(prior_generation_ids.issubset(retained))
        self.assertIn(independent["generation_id"], retained)

    def test_distinct_selectable_resume_parent_keeps_its_active_work_slot(self) -> None:
        b0 = self._run_claim_and_worker(1)
        journal, c = self._synthetic_c_journal(b0, 110)
        initial = self._prepare_authenticated_plan(b0, journal, c, label="distinct-resume-seed")
        self.assertTrue(initial["eligible"], initial)
        b1 = self._run_claim_and_worker(
            2, derivation=read_json(pathlib.Path(initial["derivation_path"])),
            expected_generation_id=initial["generation_id"],
            composition_baseline=pathlib.Path(initial["composition_baseline_path"]),
            resume_parent_bundle=self.output_by_generation[b0["generation_id"]],
            canonical_parent_bundle=self.output_by_generation[b0["generation_id"]],
            journal=journal, journal_ref_sha=c["readback"]["journal_ref_sha"],
        )
        self.assertEqual(b1["status"], "ready")
        self.assertGreater(b1["outcome"]["detail_retry_count"], 0)

        # The current canonical producer B0 can be shadowed, but the distinct
        # selected B1 resume parent is still a selectable ready candidate.
        # Construct its exact two-role proof from the real sealed checkpoints
        # and parent bundles, then exercise the real retention planner with a
        # one-slot cap: B1 plus the prospective continuation must be rejected.
        original = self._admitted_original(b1)
        resume_parent = self._parent_ref(b1)
        canonical_parent = self._parent_ref(b0)
        resume_ancestors = DERIVATION.validate_processor_parent_graph(
            [b1["generation_id"]],
            load_checkpoint=lambda generation: self.checkpoints[generation],
            admission_for=lambda _checkpoint: HANDOFF.validate_ledger(
                PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")["collector_handoff"],
            )["admitted_observations"][0],
            original_observation=original,
        ) - {b1["generation_id"]}
        canonical_ancestors = DERIVATION.validate_processor_parent_graph(
            [b0["generation_id"]],
            load_checkpoint=lambda generation: self.checkpoints[generation],
            admission_for=lambda _checkpoint: HANDOFF.validate_ledger(
                PROCESSOR.load_json(self.helper.state_dir / "sources/data_go_kr/index.json")["collector_handoff"],
            )["admitted_observations"][0],
            original_observation=original,
        ) - {b0["generation_id"]}
        envelope = DERIVATION.build_derivation_envelope(
            original_observation=original,
            resume_parent_processor=resume_parent,
            canonical_parent_processor=canonical_parent,
            canonical_parent_readback=c["readback"],
            composition_baseline=c["baseline"],
            derivation_processor_revision_sha256=PROCESSOR.derivation_processor_revision(),
            resume_parent_ancestors=sorted(resume_ancestors),
            canonical_parent_ancestors=sorted(canonical_ancestors),
        )
        prospective_id, prospective_inputs = PROCESSOR.generation_identity(
            original["source_id"], original["source_scope"], original["original_baseline_sha256"],
            original["candidate_sha256"], None, original["source_policy_sha256"],
            original["provider_index_sha256"], same_observation_derivation=envelope,
        )
        prospective = copy.deepcopy(b1)
        prospective.update({
            "generation_id": prospective_id,
            "generation_inputs": prospective_inputs,
            "status": "queued",
            "lease": None,
            "request_reservation": None,
        })
        PROCESSOR.seal_checkpoint(prospective)
        index_path = self.helper.state_dir / "sources/data_go_kr/index.json"
        index = PROCESSOR.load_json(index_path)
        generation_dir = self.helper.state_dir / "sources/data_go_kr/generations"
        with mock.patch.object(PROCESSOR, "DEFAULT_MAX_ACTIVE_GENERATIONS", 1):
            with self.assertRaisesRegex(ValueError, "active_generation_queue_full"):
                PROCESSOR.plan_generation_retention(index, generation_dir, prospective)

        # If both roles are the already merged B0, B1 itself is the only
        # selectable active slot and fits the same cap. This pins the
        # distinction to exact parent roles rather than retained-lineage size.
        with mock.patch.object(PROCESSOR, "DEFAULT_MAX_ACTIVE_GENERATIONS", 1):
            retained = PROCESSOR.plan_generation_retention(index, generation_dir, b1)
        self.assertIn(b0["generation_id"], retained)
        self.assertIn(b1["generation_id"], retained)

    def test_expired_parent_artifact_blocks_authenticated_plan_without_claim_mutation(self) -> None:
        original_now = self.now
        original_now_text = self.now_text
        self.output_expiry = (self.now + dt.timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        b0 = self._run_claim_and_worker(1)
        b1 = self._run_claim_and_worker(
            2, resume=self.output_by_generation[b0["generation_id"]] / "upstream-catalogue-enrichment-evidence.json",
            generator_revision=self.alternate_generator_revision,
        )
        journal, c = self._synthetic_c_journal(b0, 1)
        index_path = self.helper.state_dir / "sources/data_go_kr/index.json"
        before_index = index_path.read_bytes()

        # The API/readback evidence itself is still recent and authentic. Only
        # the exact B parent archive locator has crossed its original expiry.
        self.now = original_now + dt.timedelta(minutes=2)
        self.now_text = self.now.isoformat().replace("+00:00", "Z")
        with self.assertRaisesRegex(ValueError, "derivation_parent_artifact_expired"):
            self._prepare_authenticated_plan(b1, journal, c, label="expired-parent")
        self.assertEqual(index_path.read_bytes(), before_index)
        self.assertEqual(len(self.claim_runs), 2)
        self.assertEqual(len(self.detail_calls), 2)

    def test_preparation_rejects_durable_row_or_wrong_api_identity_before_claim(self) -> None:
        target = self._run_claim_and_worker(1)
        journal, c = self._synthetic_c_journal(target, 1)
        index_path = self.helper.state_dir / "sources/data_go_kr/index.json"
        admitted_rows = HANDOFF.validate_ledger(
            PROCESSOR.load_json(index_path)["collector_handoff"],
        )["admitted_observations"]
        durable_row = next(row for row in admitted_rows if row["producer_run_id"] == self.observation_run)
        calls_before = (len(self.claim_runs), len(self.detail_calls))

        with self.assertRaisesRegex(ValueError, "collector_handoff_admission_metadata_shape_invalid"):
            self._prepare_authenticated_plan(
                target, journal, c, label="reject-durable-row-as-envelope",
                admission_value_override=durable_row,
            )
        self.assertEqual((len(self.claim_runs), len(self.detail_calls)), calls_before)

        with self.assertRaisesRegex(ValueError, "collector_handoff_admission_identity_mismatch"):
            self._prepare_authenticated_plan(
                target, journal, c, label="reject-api-artifact-id-mismatch",
                authenticated_overrides={"producer_artifact_id": "99000000999"},
            )
        self.assertEqual((len(self.claim_runs), len(self.detail_calls)), calls_before)

        with self.assertRaisesRegex(ValueError, "derivation_collector_admission_authenticated_metadata_missing"):
            self._prepare_authenticated_plan(
                target, journal, c, label="reject-missing-selected-attempt",
                omitted_authenticated_fields={"--producer-run-attempt"},
            )
        self.assertEqual((len(self.claim_runs), len(self.detail_calls)), calls_before)

        with self.assertRaisesRegex(ValueError, "derivation_source_checkout_does_not_match_producer_head"):
            self._prepare_authenticated_plan(
                target, journal, c, label="reject-source-checkout-mismatch",
                source_checkout_head="b" * 40,
            )
        self.assertEqual((len(self.claim_runs), len(self.detail_calls)), calls_before)

        index = PROCESSOR.load_json(index_path)
        selected_row = next(
            row for row in index["collector_handoff"]["admitted_observations"]
            if row["producer_run_id"] == self.observation_run
        )
        selected_row["candidate_sha256"] = "f" * 64
        index_path.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "derivation_current_admission_durable_row_mismatch"):
            self._prepare_authenticated_plan(
                target, journal, c, label="reject-selected-durable-row-substitution",
            )
        self.assertEqual((len(self.claim_runs), len(self.detail_calls)), calls_before)


if __name__ == "__main__":
    unittest.main()
