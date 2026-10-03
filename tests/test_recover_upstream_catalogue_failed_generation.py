from __future__ import annotations

import base64
import copy
import gzip
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "recover_upstream_catalogue_failed_generation.py"
SPEC = importlib.util.spec_from_file_location("recover_failed_generation", SCRIPT)
assert SPEC and SPEC.loader
RECOVERY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RECOVERY
SPEC.loader.exec_module(RECOVERY)

PROCESSOR_SCRIPT = ROOT / "scripts" / "process-upstream-catalogue-candidate.py"
PROCESSOR_SPEC = importlib.util.spec_from_file_location("process_upstream_catalogue_candidate", PROCESSOR_SCRIPT)
assert PROCESSOR_SPEC and PROCESSOR_SPEC.loader
PROCESSOR = importlib.util.module_from_spec(PROCESSOR_SPEC)
sys.modules[PROCESSOR_SPEC.name] = PROCESSOR
PROCESSOR_SPEC.loader.exec_module(PROCESSOR)

# These are the compressed bytes of the actual frozen durable claim and failed
# artifact checkpoint receipt from run 37091592758, kept small and portable so
# CI can exercise the production evidence without depending on local .datapan data.
DURABLE_CHECKPOINT_GZIP_B64 = (
    "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"
)

FAILED_RECEIPT_GZIP_B64 = (
    "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"
    "D/8B150MsMQ1AAA="
)


def unpack_json(compressed_b64: str) -> tuple[dict, bytes]:
    raw = gzip.decompress(base64.b64decode(compressed_b64))
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value, raw


def frozen_inputs():
    checkpoint, _ = unpack_json(DURABLE_CHECKPOINT_GZIP_B64)
    receipt, receipt_bytes = unpack_json(FAILED_RECEIPT_GZIP_B64)
    reservation = checkpoint["request_reservation"]
    retry_state = {
        row["id"]: {
            "source_sha256": row["source_sha256"],
            "guide_sha256": row["guide_sha256"],
            "attempts": checkpoint["attempts_by_id"][row["id"]],
            "last_attempt_at": reservation["reserved_at"],
        }
        for row in reservation["records"]
    }
    index = {
        "schema_version": checkpoint["schema_version"],
        "detail_queue_cursor": checkpoint["detail_queue_cursor"],
        "detail_retry_state": retry_state,
        "generations": [{
            "generation_id": checkpoint["generation_id"],
            "status": checkpoint["status"],
            "checkpoint": f"{checkpoint['generation_id']}.json",
            "updated_at": checkpoint["last_heartbeat_at"],
            "candidate_sha256": checkpoint["generation_inputs"]["candidate_sha256"],
        }],
    }
    state = {"state_branch_sha": RECOVERY.STATE_BRANCH_SHA, "index": index}
    run = {
        "repository": "StatPan/datapan-registry",
        "id": 37091592758,
        "name": "Process upstream catalogue",
        "path": ".github/workflows/upstream-catalogue-process.yml",
        "head_branch": "main",
        "head_sha": "446016d15d6c16b599bdaf89a97bc450b71205f4",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 1,
        "default_branch": "main",
    }
    artifact = {
        "repository": "StatPan/datapan-registry",
        "run_id": "37091592758",
        "run_attempt": 1,
        "id": 11262805213,
        "name": "upstream-catalogue-processing-37091592758-1",
        "expired": False,
        "expires_at": "2026-11-02T03:00:08Z",
        "created_at": "2026-10-03T03:00:12Z",
        "size_in_bytes": 17093081,
        "digest": "sha256:b3ef63c8dcfd22e04d3d54cac3763733d24883400d36a0eacfbbe834ad5496cf",
        "workflow_run": {
            "id": 37091592758,
            "head_sha": "446016d15d6c16b599bdaf89a97bc450b71205f4",
            "head_branch": "main",
            "repository_id": 1278568329,
            "head_repository_id": 1278568329,
        },
    }
    result = (
        b'{\n  "generation_id": "48bd59cf7d2da0fc75fb6e9eb6dcee205be066bbfefa5d7f88cebdee3ddd5b50",\n'
        b'  "reason": "processor_bundle_not_verified",\n  "status": "retry"\n}\n'
    )
    payload = {
        "upstream-catalogue-checkpoint-receipt.json": receipt_bytes,
        "upstream-catalogue-processing-result.json": result,
    }
    current_run = {
        "repository": "StatPan/datapan-registry",
        "processor_run_id": "38000000000-1",
        "processor_artifact_run_id": "38000000000",
        "run_attempt": 1,
        "head_sha": "8f" * 20,
        "artifact_name": "upstream-catalogue-processing-38000000000-1",
        "expires_at": "2026-11-03T04:00:00Z",
    }
    return state, checkpoint, run, artifact, payload, current_run


class RecoverFailedGenerationTest(unittest.TestCase):
    def plan(self, *, now: datetime | None = None, values=None):
        state, checkpoint, run, artifact, payload, current_run = values or frozen_inputs()
        return RECOVERY.recover_failed_generation(
            state, checkpoint, run, artifact, payload,
            RECOVERY.GENERATION_CHECKPOINT_SHA256, current_run,
            now or datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc),
        )

    def test_real_frozen_run_produces_new_fenced_result_only_quarantine_marker(self) -> None:
        values = frozen_inputs()
        before = copy.deepcopy(values)
        state, checkpoint, _run, _artifact, _payload, current_run = values
        plan = self.plan(values=values)
        marker = plan.checkpoint

        self.assertEqual(plan.expected_state_head_sha, RECOVERY.STATE_BRANCH_SHA)
        self.assertEqual(marker["status"], "quarantined")
        self.assertIsNone(marker["lease"])
        self.assertEqual(marker["fencing_token"], 2)
        self.assertNotIn("checkpoint_sha256", marker)
        self.assertEqual(marker["outcome"]["reason"], RECOVERY.RECOVERY_REASON)
        evidence = marker["outcome"]["recovery_evidence"]
        self.assertEqual(evidence["original_scope_error"], "composer_scope_omits_worker_outcomes")
        self.assertEqual(evidence["original_checkpoint_sha256"], RECOVERY.GENERATION_CHECKPOINT_SHA256)
        self.assertEqual(evidence["failed_run"]["id"], 37091592758)
        self.assertEqual(evidence["failed_artifact"]["id"], 11262805213)
        self.assertEqual(evidence["failed_artifact"]["archive_sha256"], values[3]["digest"].removeprefix("sha256:"))
        self.assertEqual(marker["output_artifact"]["run_id"], current_run["processor_artifact_run_id"])
        self.assertEqual(marker["output_artifact"]["name"], current_run["artifact_name"])
        self.assertIsNone(marker["output_artifact"]["artifact_id"])
        self.assertEqual(marker["output_digests"], [])
        self.assertEqual(marker["generation_inputs"], checkpoint["generation_inputs"])
        preserved_fields = (
            "input_artifacts", "request_reservation", "attempts_by_id",
            "attempts_consumed", "detail_queue_cursor", "detail_records",
            "detail_retry_reset_ids", "last_progress_at",
        )
        for field in preserved_fields:
            self.assertEqual(marker[field], checkpoint[field], field)
        self.assertEqual(marker["request_reservation"]["attempts_made"], 0)
        self.assertEqual(values, before, "the pure planner must not mutate caller inputs")

        result = PROCESSOR.processing_result(
            marker,
            producer_run_id=checkpoint["last_observation"]["producer_run_id"],
            processor_run_id=current_run["processor_run_id"],
            processor_artifact_run_id=current_run["processor_artifact_run_id"],
        )
        self.assertFalse(result["candidate_available"])
        self.assertEqual(result["reason"], RECOVERY.RECOVERY_REASON)

    def test_replay_is_deterministic_and_index_writer_conserves_retry_budget(self) -> None:
        values = frozen_inputs()
        first = self.plan(values=values)
        second = self.plan(values=values)
        self.assertEqual(first, second)

        state = values[0]
        original_index = copy.deepcopy(state["index"])
        with tempfile.TemporaryDirectory() as temporary:
            index_path = pathlib.Path(temporary) / "index.json"
            index_path.write_text(json.dumps(original_index), encoding="utf-8")
            sealed = PROCESSOR.seal_checkpoint(copy.deepcopy(first.checkpoint))
            checkpoint_path = pathlib.Path(temporary) / f"{sealed['generation_id']}.json"
            PROCESSOR.append_generation_index(index_path, checkpoint_path, sealed)
            updated_index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_index["detail_queue_cursor"], original_index["detail_queue_cursor"])
        self.assertEqual(updated_index["detail_retry_state"], original_index["detail_retry_state"])
        row = next(row for row in updated_index["generations"] if row["generation_id"] == RECOVERY.GENERATION_ID)
        self.assertEqual(row["status"], "quarantined")

    def test_processor_recovery_adapter_persists_only_the_current_result_marker(self) -> None:
        state, checkpoint, run, artifact, payload, current_run = frozen_inputs()
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            state_dir = root / "state"
            source_dir = state_dir / "sources" / "data_go_kr"
            generation_dir = source_dir / "generations"
            output_dir = root / "output"
            evidence_dir = root / "failed-artifact"
            generation_dir.mkdir(parents=True)
            evidence_dir.mkdir()
            (source_dir / "index.json").write_text(json.dumps(state["index"]), encoding="utf-8")
            checkpoint_path = generation_dir / f"{checkpoint['generation_id']}.json"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            run_path = root / "failed-run.json"
            run_path.write_text(json.dumps(run), encoding="utf-8")
            artifact_metadata_path = root / "failed-artifact.json"
            artifact_metadata_path.write_text(json.dumps(artifact), encoding="utf-8")
            for name, body in payload.items():
                (evidence_dir / name).write_bytes(body)

            args = PROCESSOR.build_parser().parse_args([])
            args.source = "data_go_kr"
            args.state_dir = state_dir
            args.output_dir = output_dir
            args.checkpoint_schema = ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
            args.repository = "StatPan/datapan-registry"
            args.producer_run_id = str(checkpoint["last_observation"]["producer_run_id"])
            args.processor_run_id = current_run["processor_run_id"]
            args.processor_artifact_run_id = current_run["processor_artifact_run_id"]
            args.output_artifact_expires_at = current_run["expires_at"]
            args.current_head_sha = current_run["head_sha"]
            args.recover_failed_processor_run_id = str(run["id"])
            args.recover_failed_generation_id = checkpoint["generation_id"]
            args.failed_run_metadata = run_path
            args.failed_artifact_metadata = artifact_metadata_path
            args.failed_artifact_dir = evidence_dir
            args.expected_checkpoint_sha256 = RECOVERY.GENERATION_CHECKPOINT_SHA256
            args.expected_state_head_sha = RECOVERY.STATE_BRANCH_SHA
            args.now = "2026-10-03T04:00:00Z"

            checkpoint_bytes = checkpoint_path.read_bytes()
            index_path = source_dir / "index.json"
            index_bytes = index_path.read_bytes()
            args.recover_failed_processor_run_id = "37000000000"
            with self.assertRaisesRegex(ValueError, "failed_processor_recovery_target_mismatch"):
                PROCESSOR.recover_failed_processor_generation(args)
            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_bytes)
            self.assertEqual(index_path.read_bytes(), index_bytes)
            self.assertFalse(output_dir.exists())
            args.recover_failed_processor_run_id = str(run["id"])

            with mock.patch.object(PROCESSOR, "fetch_public_detail") as provider_call:
                status, sealed = PROCESSOR.recover_failed_processor_generation(args)
                provider_call.assert_not_called()

            self.assertEqual(status, 3)
            self.assertEqual(PROCESSOR.verify_checkpoint(
                json.loads(checkpoint_path.read_text(encoding="utf-8")),
                json.loads(args.checkpoint_schema.read_text(encoding="utf-8")),
            ), sealed)
            self.assertEqual(sealed["status"], "quarantined")
            self.assertEqual(sealed["outcome"]["reason"], RECOVERY.RECOVERY_REASON)
            self.assertEqual(sealed["fencing_token"], 2)
            self.assertEqual(sealed["output_artifact"]["run_id"], current_run["processor_artifact_run_id"])
            self.assertIsNone(sealed["output_artifact"]["artifact_id"])
            self.assertEqual([row["path"] for row in sealed["output_digests"]], ["upstream-catalogue-processing-result.json"])
            result = json.loads((output_dir / "upstream-catalogue-processing-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "quarantined")
            self.assertEqual(result["reason"], RECOVERY.RECOVERY_REASON)
            self.assertFalse(result["candidate_available"])
            self.assertFalse((output_dir / "composed-candidate.registry.json").exists())
            self.assertFalse((output_dir / "upstream-catalogue-enrichment-evidence.json").exists())
            updated_index = json.loads((source_dir / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(updated_index["detail_queue_cursor"], state["index"]["detail_queue_cursor"])
            self.assertEqual(updated_index["detail_retry_state"], state["index"]["detail_retry_state"])
            generation_row = next(row for row in updated_index["generations"] if row["generation_id"] == checkpoint["generation_id"])
            self.assertEqual(generation_row["status"], "quarantined")

    def test_terminalized_generation_rejects_a_repeated_recovery(self) -> None:
        values = frozen_inputs()
        plan = self.plan(values=values)
        terminal = PROCESSOR.seal_checkpoint(plan.checkpoint)
        state = copy.deepcopy(values[0])
        state["index"]["generations"][0].update(
            status="quarantined",
            updated_at=terminal["last_heartbeat_at"],
        )
        repeated = (state, terminal, *values[2:])
        with self.assertRaises(RECOVERY.RecoveryRejected) as error:
            self.plan(values=repeated)
        self.assertEqual(error.exception.reason, "recovery_expected_checkpoint_sha_mismatch")

    def test_active_lease_is_rejected(self) -> None:
        with self.assertRaises(RECOVERY.RecoveryRejected) as error:
            self.plan(now=datetime(2026, 10, 3, 3, 43, 30, tzinfo=timezone.utc))
        self.assertEqual(error.exception.reason, "recovery_lease_still_active")

    def test_failed_run_artifact_and_payload_must_match_exact_frozen_evidence(self) -> None:
        mutations = (
            (
                "run", lambda values: values[2].update(head_sha="0" * 40),
                "recovery_failed_run_identity_mismatch",
            ),
            (
                "artifact", lambda values: values[3].update(id=11262805214),
                "recovery_failed_artifact_identity_mismatch",
            ),
            (
                "artifact run binding",
                lambda values: values[3]["workflow_run"].update(head_sha="0" * 40),
                "recovery_failed_artifact_run_binding_mismatch",
            ),
            (
                "artifact payload", lambda values: values[4].update({"../checkpoint.json": b"bad"}),
                "recovery_artifact_members_invalid",
            ),
            (
                "artifact content",
                lambda values: values[4].update({
                    "upstream-catalogue-processing-result.json": b"tampered",
                }),
                "recovery_artifact_content_digest_mismatch",
            ),
            (
                "checkpoint sha",
                lambda values: values[1].update(checkpoint_sha256="0" * 64),
                "recovery_expected_checkpoint_sha_mismatch",
            ),
            (
                "invalid CAS head",
                lambda values: values[0].update(state_branch_sha="not-a-git-sha"),
                "recovery_state_head_sha_invalid",
            ),
        )
        for label, mutate, reason in mutations:
            with self.subTest(label=label):
                values = frozen_inputs()
                mutate(values)
                with self.assertRaises(RECOVERY.RecoveryRejected) as error:
                    self.plan(values=values)
                self.assertEqual(error.exception.reason, reason)

    def test_owner_fence_and_source_retry_state_conflicts_fail_closed(self) -> None:
        values = frozen_inputs()
        values[1]["lease"]["owner_run_id"] = "someone-else"
        with self.assertRaises(RECOVERY.RecoveryRejected) as error:
            self.plan(values=values)
        self.assertEqual(error.exception.reason, "recovery_checkpoint_digest_mismatch")

        values = frozen_inputs()
        values[0]["index"]["detail_queue_cursor"] = 23
        with self.assertRaises(RECOVERY.RecoveryRejected) as error:
            self.plan(values=values)
        self.assertEqual(error.exception.reason, "recovery_state_index_cursor_mismatch")

if __name__ == "__main__":
    unittest.main()
