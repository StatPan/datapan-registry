"""Committed-State replay verification against real temporary Git repositories.

Packet B requires the evidence loader to sit outside the v1/v2 dispatcher:
ordinary and older-admitted receipts are rebuilt from committed bytes, and an
older delivery is accepted only when the caller-supplied delivery context
matches the FIRST admission ledger row.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROCESSOR_SCRIPT = ROOT / "scripts" / "process-upstream-catalogue-candidate.py"
SCHEMA_PATH = ROOT / "schemas" / "datapan.upstream-catalogue-checkpoint.v1.schema.json"
STATE_ROOT = ".datapan/upstream-catalogue-state"
REPOSITORY = "StatPan/datapan-registry"

_SPEC = importlib.util.spec_from_file_location("committed_state_replay_processor", PROCESSOR_SCRIPT)
PROCESSOR = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(PROCESSOR)


def git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {args[0]} failed: {completed.stderr or completed.stdout}")
    return completed


def write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def init_repo(root: pathlib.Path) -> pathlib.Path:
    repo = root / "state"
    repo.mkdir()
    git(repo, "init", "-b", "automation/upstream-catalogue-state")
    git(repo, "config", "user.name", "fixture")
    git(repo, "config", "user.email", "fixture@example.test")
    return repo


def commit_tree(repo: pathlib.Path, message: str) -> str:
    git(repo, "add", STATE_ROOT)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def checkpoint(
    *, generation_id: str, generation_inputs: dict, heartbeat: str, producer_run_id: str,
    evidence_sha: str, candidate_sha: str, artifact_id: str = "654321",
    output_digests: list | None = None, output_artifact: dict | None = None,
) -> dict:
    sealed = PROCESSOR.seal_checkpoint({
        "schema_version": PROCESSOR.CHECKPOINT_SCHEMA,
        "source_id": "data_go_kr",
        "source_scope": "aggregate_supported_catalog",
        "generation_id": generation_id,
        "generation_inputs": generation_inputs,
        "observed_at": "2026-10-01T10:00:00Z",
        "last_observation": {
            "observed_at": "2026-10-01T10:00:00Z",
            "producer_run_id": producer_run_id,
            "refresh_evidence_sha256": evidence_sha,
            "collection_status": "success",
            "execution_mode": "live",
        },
        "observation_count": 1,
        "last_heartbeat_at": heartbeat,
        "last_progress_at": heartbeat,
        "status": "ready",
        "attempts_consumed": 24,
        "attempts_by_id": {},
        "detail_retry_reset_ids": [],
        "request_reservation": None,
        "detail_records": [],
        "detail_queue_cursor": 0,
        "output_digests": output_digests if output_digests is not None else [
            {"path": "composition-receipt.json", "sha256": "b" * 64, "bytes": 321},
        ],
        "output_artifact": output_artifact or {
            "repository": REPOSITORY,
            "run_id": "700",
            "name": "upstream-catalogue-processing-700-1",
            "artifact_id": "7654321",
            "expires_at": "2026-11-01T00:00:00Z",
            "bundle_manifest_sha256": "c" * 64,
        },
        "input_artifacts": [{
            "run_id": producer_run_id,
            "name": f"upstream-catalog-refresh-{producer_run_id}",
            "artifact_id": artifact_id,
            "expires_at": "2026-11-01T00:00:00Z",
            "candidate_sha256": candidate_sha,
            "evidence_sha256": evidence_sha,
            "diff_sha256": "d" * 64,
        }],
        "lease": None,
        "fencing_token": 1,
        "outcome": {"reason": "ready_scoped"},
    })
    PROCESSOR.verify_checkpoint(sealed, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    return sealed


def admission_for(
    *, producer_run_id: str, generation_id: str, evidence_sha: str, candidate_sha: str,
    artifact_id: str = "654321", artifact_digest: str = "4" * 64, admitted_at: str = "2026-10-01T10:00:00Z",
    run_attempt: int = 1, head_sha: str = "5" * 40,
) -> dict:
    return {
        "admission_id": PROCESSOR.admission_id(producer_run_id, run_attempt, evidence_sha),
        "producer_run_id": producer_run_id,
        "run_attempt": run_attempt,
        "head_sha": head_sha,
        "run_started_at": "2026-10-01T09:55:00Z",
        "artifact_id": artifact_id,
        "artifact_name": f"upstream-catalog-refresh-{producer_run_id}",
        "artifact_expires_at": "2026-11-01T00:00:00Z",
        "artifact_digest_sha256": artifact_digest,
        "artifact_size_bytes": 1234,
        "refresh_evidence_sha256": evidence_sha,
        "observed_at": "2026-10-01T10:00:00Z",
        "generation_id": generation_id,
        "candidate_sha256": candidate_sha,
        "admitted_at": admitted_at,
    }


def index_for(rows: list[dict], ledger_rows: list[dict]) -> dict:
    value = {
        "schema_version": PROCESSOR.CHECKPOINT_SCHEMA,
        "generations": rows,
        "detail_queue_cursor": 0,
        "detail_retry_state": {},
    }
    for row in ledger_rows:
        PROCESSOR.add_admission(value, row, now=row["admitted_at"])
    return value


def index_row(checkpoint_value: dict) -> dict:
    return {
        "generation_id": checkpoint_value["generation_id"],
        "status": checkpoint_value["status"],
        "checkpoint": f"{checkpoint_value['generation_id']}.json",
        "updated_at": checkpoint_value["last_heartbeat_at"],
        "candidate_sha256": checkpoint_value["generation_inputs"]["candidate_sha256"],
    }


def paths(generation_id: str) -> tuple[pathlib.Path, pathlib.Path]:
    return (
        pathlib.Path(STATE_ROOT) / "sources/data_go_kr/generations" / f"{generation_id}.json",
        pathlib.Path(STATE_ROOT) / "sources/data_go_kr/index.json",
    )


def result_for(producer_run_id: str) -> dict:
    return {
        "status": "idle",
        "reason": "exact_producer_delivery_replay",
        "processing_replay": True,
        "candidate_available": False,
        "source_id": "data_go_kr",
        "producer_run_id": producer_run_id,
        "processor_run_id": "777-2",
        "processor_artifact_run_id": "777",
    }


def ordinary_transition(root: pathlib.Path, *, heartbeat_change: bool) -> dict:
    repo = init_repo(root)
    candidate_sha = "2" * 64
    evidence_sha = "3" * 64
    producer_run_id = "37548691738"
    inputs = {
        "source_id": "data_go_kr",
        "source_scope": "aggregate_supported_catalog",
        "baseline_sha256": "6" * 64,
        "candidate_sha256": candidate_sha,
        "observation_failure_sha256": None,
        "policy_sha256": "7" * 64,
        "adapter_revision": "8" * 64,
        "generator_revision": "9" * 64,
        "extractor_revision": "a" * 64,
    }
    generation_id = PROCESSOR._object_sha256(inputs)
    prior = checkpoint(
        generation_id=generation_id, generation_inputs=inputs, heartbeat="2026-10-07T01:00:00Z",
        producer_run_id=producer_run_id, evidence_sha=evidence_sha, candidate_sha=candidate_sha,
    )
    ledger = admission_for(
        producer_run_id=producer_run_id, generation_id=generation_id,
        evidence_sha=evidence_sha, candidate_sha=candidate_sha,
    )
    prior_index = index_for([index_row(prior)], [ledger])
    generation_rel, index_rel = paths(generation_id)
    write_json(repo / generation_rel, prior)
    write_json(repo / index_rel, prior_index)
    prior_sha = commit_tree(repo, "seed ordinary replay")
    current = copy.deepcopy(prior)
    current_index = copy.deepcopy(prior_index)
    if heartbeat_change:
        current["last_heartbeat_at"] = "2026-10-07T01:01:00Z"
        PROCESSOR.seal_checkpoint(current)
        current_index["generations"][0]["updated_at"] = current["last_heartbeat_at"]
        write_json(repo / generation_rel, current)
        write_json(repo / index_rel, current_index)
        current_sha = commit_tree(repo, "ordinary replay heartbeat")
    else:
        current_sha = prior_sha
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    result = result_for(producer_run_id)
    receipt = PROCESSOR.build_replay_persistence_receipt(
        prior_checkpoint=prior, current_checkpoint=current,
        prior_index=prior_index, current_index=current_index,
        result=result, state_head_sha=prior_sha, checkpoint_schema=schema,
    )
    return {
        "repo": repo, "prior_sha": prior_sha, "current_sha": current_sha,
        "receipt": receipt, "result": result, "current": current,
        "generation_rel": generation_rel,
    }


def older_transition(root: pathlib.Path) -> dict:
    repo = init_repo(root)
    candidate_sha = "2" * 64
    latest_run = "37548691739"
    delivered_run = "37548691738"
    inputs = {
        "source_id": "data_go_kr",
        "source_scope": "aggregate_supported_catalog",
        "baseline_sha256": "6" * 64,
        "candidate_sha256": candidate_sha,
        "observation_failure_sha256": None,
        "policy_sha256": "7" * 64,
        "adapter_revision": "8" * 64,
        "generator_revision": "9" * 64,
        "extractor_revision": "a" * 64,
    }
    generation_id = PROCESSOR._object_sha256(inputs)
    prior = checkpoint(
        generation_id=generation_id, generation_inputs=inputs, heartbeat="2026-10-07T01:00:00Z",
        producer_run_id=latest_run, evidence_sha="3" * 64, candidate_sha=candidate_sha,
    )
    latest = admission_for(
        producer_run_id=latest_run, generation_id=generation_id, evidence_sha="3" * 64,
        candidate_sha=candidate_sha, admitted_at="2026-10-01T10:00:00Z",
    )
    delivered = admission_for(
        producer_run_id=delivered_run, generation_id=generation_id, evidence_sha="e" * 64,
        candidate_sha=candidate_sha, artifact_id="654320", artifact_digest="f" * 64,
        admitted_at="2026-10-01T09:58:00Z",
    )
    prior_index = index_for([index_row(prior)], [delivered, latest])
    generation_rel, index_rel = paths(generation_id)
    write_json(repo / generation_rel, prior)
    write_json(repo / index_rel, prior_index)
    prior_sha = commit_tree(repo, "seed older admitted replay")
    current = copy.deepcopy(prior)
    current["last_heartbeat_at"] = "2026-10-07T01:01:00Z"
    PROCESSOR.seal_checkpoint(current)
    current_index = copy.deepcopy(prior_index)
    current_index["generations"][0]["updated_at"] = current["last_heartbeat_at"]
    write_json(repo / generation_rel, current)
    write_json(repo / index_rel, current_index)
    current_sha = commit_tree(repo, "older admitted replay heartbeat")
    delivery = {
        "producer_run_id": delivered["producer_run_id"],
        "run_attempt": delivered["run_attempt"],
        "artifact_id": delivered["artifact_id"],
        "head_sha": delivered["head_sha"],
        "artifact_digest_sha256": delivered["artifact_digest_sha256"],
    }
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    result = result_for(delivered_run)
    receipt = PROCESSOR.build_older_admitted_replay_persistence_receipt(
        prior_checkpoint=prior, current_checkpoint=current,
        prior_index=prior_index, current_index=current_index,
        result=result, state_head_sha=prior_sha,
        delivered_admission_id=delivered["admission_id"],
        expected_delivery=delivery, checkpoint_schema=schema,
    )
    return {
        "repo": repo, "prior_sha": prior_sha, "current_sha": current_sha,
        "receipt": receipt, "result": result, "current": current, "delivery": delivery,
    }


def derived_transition(root: pathlib.Path, *, tamper_parent: bool = False) -> dict:
    repo = init_repo(root)
    candidate_sha = "2" * 64
    evidence_sha = "3" * 64
    producer_run_id = "37548691738"
    parent_inputs = {
        "source_id": "data_go_kr",
        "source_scope": "aggregate_supported_catalog",
        "baseline_sha256": "6" * 64,
        "candidate_sha256": candidate_sha,
        "observation_failure_sha256": None,
        "policy_sha256": "7" * 64,
        "adapter_revision": "8" * 64,
        "generator_revision": "9" * 64,
        "extractor_revision": "a" * 64,
    }
    parent_generation = PROCESSOR._object_sha256(parent_inputs)
    output_digests = [
        {"path": "composed-candidate.registry.json", "sha256": "1" * 64, "bytes": 10},
        {"path": "ready-scope.registry.json", "sha256": "2" * 64, "bytes": 11},
        {"path": "semantic-diff.json", "sha256": "3" * 64, "bytes": 12},
        {"path": "regeneration-queue.json", "sha256": "4" * 64, "bytes": 13},
        {"path": "quarantine.json", "sha256": "5" * 64, "bytes": 14},
        {"path": "composition-receipt.json", "sha256": "b" * 64, "bytes": 15},
        {"path": "upstream-catalogue-enrichment-evidence.json", "sha256": "c" * 64, "bytes": 16},
        {"path": "upstream-catalogue-processing-result.json", "sha256": "d" * 64, "bytes": 17},
    ]
    bundle_manifest = PROCESSOR._object_sha256(output_digests)
    parent = checkpoint(
        generation_id=parent_generation, generation_inputs=parent_inputs,
        heartbeat="2026-10-06T01:00:00Z", producer_run_id=producer_run_id,
        evidence_sha=evidence_sha, candidate_sha=candidate_sha,
        output_digests=output_digests,
        output_artifact={
            "repository": REPOSITORY, "run_id": "700",
            "name": "upstream-catalogue-processing-700-1", "artifact_id": "7654321",
            "expires_at": "2026-11-01T00:00:00Z", "bundle_manifest_sha256": bundle_manifest,
        },
    )
    parent_admission = admission_for(
        producer_run_id=producer_run_id, generation_id=parent_generation,
        evidence_sha=evidence_sha, candidate_sha=candidate_sha,
    )
    original = PROCESSOR.DERIVATION.original_observation_from_checkpoint(parent, parent_admission)
    original_hash = PROCESSOR._object_sha256(original)
    parent_reference = {
        "generation_id": parent_generation,
        "checkpoint_sha256": parent["checkpoint_sha256"],
        "generation_inputs_sha256": PROCESSOR._object_sha256(parent_inputs),
        "bundle_manifest_sha256": bundle_manifest,
        "repository": REPOSITORY,
        "artifact_run_id": "700",
        "artifact_id": "7654321",
        "artifact_name": "upstream-catalogue-processing-700-1",
        "artifact_expires_at": "2026-11-01T00:00:00Z",
        "processor_generator_revision": parent_inputs["generator_revision"],
        "processor_extractor_revision": parent_inputs["extractor_revision"],
        "original_baseline_sha256": parent_inputs["baseline_sha256"],
        "candidate_sha256": candidate_sha,
        "enrichment_evidence_sha256": "c" * 64,
        "composition_receipt_sha256": "b" * 64,
        "composed_registry_sha256": "1" * 64,
        "original_observation_sha256": original_hash,
        "contribution_set_sha256": "e" * 64,
        "contribution_count": 1,
    }
    readback = {
        "repository": REPOSITORY,
        "journal_ref_sha": "a" * 40,
        "journal_sha256": "f" * 64,
        "journal_record_index": 0,
        "journal_record_sha256": "0" * 64,
        "canonical_producer_generation_id": parent_generation,
        "source_id": "data_go_kr",
        "source_scope": "aggregate_supported_catalog",
        "registry_path": "data/data-go-kr.registry.json",
        "registry_bytes": 20,
        "registry_sha256": "1" * 64,
        "manifest_sha256": "9" * 64,
        "pr_number": 700,
        "pr_branch": "automation/upstream-catalogue",
        "pr_body_sha256": "8" * 64,
        "pr_head_sha": "b" * 40,
        "merge_sha": "c" * 40,
        "merge_ack_run_id": 800,
        "merge_ack_run_attempt": 1,
        "merge_ack_run_url": f"https://github.com/{REPOSITORY}/actions/runs/800/attempts/1",
        "merge_ack_observed_at": "2026-10-06T02:00:00Z",
        "merge_ack_manifest_sha256": "9" * 64,
        "merge_ack_source_sha": "c" * 40,
    }
    derivation = {
        "schema_version": PROCESSOR.DERIVATION.SCHEMA_VERSION,
        "derivation_revision": PROCESSOR.DERIVATION.DERIVATION_REVISION,
        "original_observation": original,
        "resume_parent_processor": parent_reference,
        "canonical_parent_processor": parent_reference,
        "canonical_parent_readback": readback,
        "composition_baseline": {
            "main_sha": "d" * 40,
            "manifest_sha256": "9" * 64,
            "registry_path": "data/data-go-kr.registry.json",
            "registry_sha256": "1" * 64,
            "registry_bytes": 20,
        },
        "derivation_processor_revision_sha256": "7" * 64,
        "ancestor_generation_ids": [parent_generation],
    }
    PROCESSOR.DERIVATION.validate_derivation_envelope(derivation)
    derived_inputs = dict(parent_inputs)
    derived_inputs["same_observation_derivation"] = derivation
    derived_generation = PROCESSOR._object_sha256(derived_inputs)
    derived = checkpoint(
        generation_id=derived_generation, generation_inputs=derived_inputs,
        heartbeat="2026-10-07T01:00:00Z", producer_run_id=producer_run_id,
        evidence_sha=evidence_sha, candidate_sha=candidate_sha,
    )
    derived_admission = admission_for(
        producer_run_id=producer_run_id, generation_id=parent_generation,
        evidence_sha=evidence_sha, candidate_sha=candidate_sha,
    )
    stored_parent = copy.deepcopy(parent)
    if tamper_parent:
        stored_parent["fencing_token"] = 99
        PROCESSOR.seal_checkpoint(stored_parent)
    prior_index = index_for(
        [index_row(stored_parent), index_row(derived)],
        [derived_admission],
    )
    parent_rel, index_rel = paths(parent_generation)
    derived_rel, _ = paths(derived_generation)
    write_json(repo / parent_rel, stored_parent)
    write_json(repo / derived_rel, derived)
    write_json(repo / index_rel, prior_index)
    prior_sha = commit_tree(repo, "seed derived replay")
    current = copy.deepcopy(derived)
    current["last_heartbeat_at"] = "2026-10-07T01:01:00Z"
    PROCESSOR.seal_checkpoint(current)
    current_index = copy.deepcopy(prior_index)
    for row in current_index["generations"]:
        if row["generation_id"] == derived_generation:
            row["updated_at"] = current["last_heartbeat_at"]
    write_json(repo / derived_rel, current)
    write_json(repo / index_rel, current_index)
    current_sha = commit_tree(repo, "derived replay heartbeat")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    result = result_for(producer_run_id)
    receipt = PROCESSOR.build_replay_persistence_receipt(
        prior_checkpoint=derived, current_checkpoint=current,
        prior_index=prior_index, current_index=current_index,
        result=result, state_head_sha=prior_sha, checkpoint_schema=schema,
    )
    return {
        "repo": repo, "prior_sha": prior_sha, "current_sha": current_sha,
        "receipt": receipt, "result": result, "current": current,
    }


class CommittedStateReplayTests(unittest.TestCase):
    def verify(self, built: dict, *, phase: str, current_sha: str, delivery=None, checkpoint_receipt=None):
        repo = built["repo"]
        if phase == PROCESSOR._REPLAY_VERIFY_PRE_CAS:
            git(repo, "checkout", "--detach", built["prior_sha"])
            if checkpoint_receipt is not None:
                generation_id = built["receipt"]["generation_id"]
                source_id = built["receipt"]["source_id"]
                generation_rel = (
                    pathlib.Path(STATE_ROOT) / "sources" / source_id / "generations"
                    / f"{generation_id}.json"
                )
                index_rel = pathlib.Path(STATE_ROOT) / "sources" / source_id / "index.json"
                write_json(repo / generation_rel, checkpoint_receipt)
                current_index = json.loads(git(
                    repo, "show", f"{built['current_sha']}:{index_rel.as_posix()}",
                ).stdout)
                write_json(repo / index_rel, current_index)
        else:
            git(repo, "checkout", "--force", "--detach", current_sha)
        return PROCESSOR.verify_replay_persistence_receipt_from_committed_state(
            built["receipt"],
            state_repository=built["repo"],
            prior_state_commit=built["prior_sha"],
            expected_current_state_commit=current_sha,
            phase=phase,
            result=built["result"],
            checkpoint_receipt=checkpoint_receipt,
            expected_delivery=delivery,
        )

    def test_ordinary_v1_replay_accepts_pre_and_post_cas(self) -> None:
        with tempfile.TemporaryDirectory(prefix="committed-ordinary-") as temp:
            built = ordinary_transition(pathlib.Path(temp), heartbeat_change=True)
            pre = self.verify(
                built, phase=PROCESSOR._REPLAY_VERIFY_PRE_CAS, current_sha=built["prior_sha"],
                checkpoint_receipt=built["current"],
            )
            self.assertEqual(pre["schema_version"], PROCESSOR.REPLAY_PERSISTENCE_SCHEMA)
            post = self.verify(
                built, phase=PROCESSOR._REPLAY_VERIFY_POST_CAS, current_sha=built["current_sha"],
            )
            self.assertEqual(post, built["receipt"])
            self.assertTrue(post["state_changes"])

    def test_older_v2_replay_accepts_only_ledger_delivery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="committed-older-") as temp:
            built = older_transition(pathlib.Path(temp))
            pre = self.verify(
                built, phase=PROCESSOR._REPLAY_VERIFY_PRE_CAS, current_sha=built["prior_sha"],
                delivery=built["delivery"], checkpoint_receipt=built["current"],
            )
            self.assertEqual(pre["disposition"], "older_admitted")
            post = self.verify(
                built, phase=PROCESSOR._REPLAY_VERIFY_POST_CAS, current_sha=built["current_sha"],
                delivery=built["delivery"],
            )
            self.assertEqual(post["schema_version"], PROCESSOR.REPLAY_PERSISTENCE_SCHEMA_V2)
            claimed = copy.deepcopy(built["delivery"])
            claimed["artifact_id"] = "654999"
            with self.assertRaises(ValueError) as caught:
                self.verify(
                    built, phase=PROCESSOR._REPLAY_VERIFY_POST_CAS,
                    current_sha=built["current_sha"], delivery=claimed,
                )
            self.assertIn("replay_", str(caught.exception))
            with self.assertRaises(ValueError) as missing:
                self.verify(
                    built, phase=PROCESSOR._REPLAY_VERIFY_POST_CAS,
                    current_sha=built["current_sha"], delivery=None,
                )
            self.assertEqual(str(missing.exception), "replay_expected_delivery_missing")

    def test_derived_exact_replay_reconstructs_parent_graph(self) -> None:
        with tempfile.TemporaryDirectory(prefix="committed-derived-") as temp:
            built = derived_transition(pathlib.Path(temp))
            verified = self.verify(
                built, phase=PROCESSOR._REPLAY_VERIFY_POST_CAS, current_sha=built["current_sha"],
            )
            self.assertEqual(verified["schema_version"], PROCESSOR.REPLAY_PERSISTENCE_SCHEMA)
            self.assertEqual(verified, built["receipt"])

    def test_tampered_parent_lineage_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="committed-tampered-") as temp:
            built = derived_transition(pathlib.Path(temp), tamper_parent=True)
            with self.assertRaises(ValueError) as caught:
                self.verify(
                    built, phase=PROCESSOR._REPLAY_VERIFY_POST_CAS,
                    current_sha=built["current_sha"],
                )
            self.assertIn("replay_", str(caught.exception))

    def test_wrong_current_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="committed-wrong-") as temp:
            built = ordinary_transition(pathlib.Path(temp), heartbeat_change=True)
            with self.assertRaises(ValueError) as caught:
                self.verify(
                    built, phase=PROCESSOR._REPLAY_VERIFY_POST_CAS, current_sha=built["prior_sha"],
                )
            self.assertIn("replay_", str(caught.exception))

    def test_noop_replay_accepts_identical_committed_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="committed-noop-") as temp:
            built = ordinary_transition(pathlib.Path(temp), heartbeat_change=False)
            self.assertEqual(built["prior_sha"], built["current_sha"])
            self.assertEqual(built["receipt"]["state_changes"], [])
            verified = self.verify(
                built, phase=PROCESSOR._REPLAY_VERIFY_POST_CAS, current_sha=built["current_sha"],
            )
            self.assertEqual(verified["state_changes"], [])
            self.assertEqual(verified, built["receipt"])


if __name__ == "__main__":
    unittest.main()
