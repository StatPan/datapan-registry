from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "persist-upstream-catalogue-health.py"
SPEC = importlib.util.spec_from_file_location("persist_upstream_catalogue_health", SCRIPT)
assert SPEC and SPEC.loader
PERSIST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PERSIST)

REPOSITORY = "StatPan/datapan-registry"
PROMOTION_PATH = ".github/workflows/canonical-update-promotion.yml"
PROCESSOR_PATH = ".github/workflows/upstream-catalogue-process.yml"
ALLOWED_EVENTS = {"workflow_run", "schedule", "workflow_dispatch"}
PRIOR_RUN_ID = "37101245239"
PRIOR_STARTED_AT = "2026-10-03T08:05:24Z"
AS_OF = "2026-10-03T08:10:00Z"


def c_run(
    *, run_id: str = "37110000001", attempt: int = 1,
    started_at: str = "2026-10-03T08:06:00Z", status: str = "completed",
    conclusion: str = "success", event: str = "workflow_dispatch",
    workflow_id: int = 731, path: str = PROMOTION_PATH,
    repository: str = REPOSITORY, head_repository: str = REPOSITORY,
    head_branch: str = "main", head_sha: str = "c" * 40,
) -> dict:
    return {
        "run_id": run_id,
        "run_attempt": attempt,
        "workflow_id": workflow_id,
        "path": path,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "created_at": "2026-10-03T08:00:00Z",
        "run_started_at": started_at,
        "updated_at": "2026-10-03T08:09:00Z",
        "head_branch": head_branch,
        "head_sha": head_sha,
        "repository": repository,
        "head_repository": head_repository,
    }


def promotion_fault(*, fault_key: str = "a" * 64, reason: str = "promotion_workflow_run_failed") -> dict:
    return {
        "source_id": "data_go_kr",
        "stage": "promotion-execution",
        "reason": reason,
        "severity": "error",
        "owner_ticket": 659,
        "fault_key": fault_key,
        "recommended_action": "Inspect the existing promotion run.",
        "status": "open",
        "first_seen_at": PRIOR_STARTED_AT,
        "last_seen_at": PRIOR_STARTED_AT,
        "observation_count": 1,
        "last_receipt_sha256": "e" * 64,
        "generation_id": None,
        "producer_run_id": None,
        "recovery_evidence": None,
        "execution_identity": {
            "run_id": PRIOR_RUN_ID,
            "run_attempt": 3,
            "run_started_at": PRIOR_STARTED_AT,
            "head_sha": "d" * 40,
        },
    }


def live_receipt(
    *, success: dict | None = None, latest: dict | None = None,
    faults: list[dict] | None = None, include_canonical: bool = True,
) -> dict:
    canonical: dict = {"last_good": None, "publication": None}
    if include_canonical:
        canonical["promotion_execution"] = {
            "latest_execution_run": copy.deepcopy(latest if latest is not None else success),
            "latest_successful_execution_run": copy.deepcopy(success),
            "execution_failure": None,
        }
    return {
        "repository": REPOSITORY,
        "execution_mode": "live",
        "evaluated_at": AS_OF,
        "receipt_sha256": "f" * 64,
        "faults": copy.deepcopy(faults or []),
        "sources": [{
            "source_id": "data_go_kr",
            "overall": "blocked",
            "observation": {
                "state": "fresh",
                "execution_mode": "live",
                "observed_at": "2026-09-29T02:00:00Z",
                "producer_run_id": "36646768289",
                "refresh_evidence_sha256": "b" * 64,
            },
            "processor": {
                "state": "ready",
                "generation_id": "1" * 64,
                "latest_successful_execution_run": c_run(
                    run_id="999", started_at="2026-10-03T08:08:00Z",
                    path=PROCESSOR_PATH, event="workflow_run",
                ),
            },
            "canonical": canonical,
        }],
    }


def already_canonical_receipt() -> dict:
    receipt = live_receipt()
    main_revision = "c" * 40
    manifest_sha256 = "d" * 64
    registry_bytes = b"same-main-bytes\n"
    registry_sha256 = hashlib.sha256(registry_bytes).hexdigest()
    source = receipt["sources"][0]
    processor = source["processor"]
    processor["checkpoint_sha256"] = "5" * 64
    processor["output_artifact"] = {
        "repository": REPOSITORY,
        "run_id": "123456789",
        "name": "upstream-catalogue-processing-123456789-2",
        "artifact_id": "99887766",
        "bundle_manifest_sha256": None,
    }
    processor["output_digests"] = [
        {
            "path": path,
            "sha256": registry_sha256 if path == "composed-candidate.registry.json" else f"{index + 1:x}" * 64,
            "bytes": len(registry_bytes) if path == "composed-candidate.registry.json" else 100 + index,
        }
        for index, path in enumerate(PERSIST.PROCESSOR_OUTPUT_PATHS)
    ]
    bundle_sha256 = PERSIST.digest(processor["output_digests"])
    processor["output_artifact"]["bundle_manifest_sha256"] = bundle_sha256
    source["canonical"].update({
        "main": {
            "revision": main_revision,
            "manifest_sha256": manifest_sha256,
            "registry_path": "data/data-go-kr.registry.json",
            "registry_bytes": len(registry_bytes),
            "registry_sha256": registry_sha256,
        },
        "last_good": None,
        "publication": None,
        "already_canonical_candidate": {
            "verified": True,
            "source_id": "data_go_kr",
            "generation_id": processor["generation_id"],
            "checkpoint_sha256": processor["checkpoint_sha256"],
            "processor_run_id": "123456789",
            "processor_run_attempt": 2,
            "artifact_id": "99887766",
            "output_bundle_sha256": bundle_sha256,
            "composed_registry_bytes": len(registry_bytes),
            "composed_registry_sha256": registry_sha256,
            "main_revision": main_revision,
            "main_manifest_sha256": manifest_sha256,
        },
    })
    source["canonical"]["promotion_execution"] = {
        "latest_execution_run": None,
        "latest_successful_execution_run": None,
        "execution_failure": None,
    }
    receipt["health_workflow"] = {"run_id": "37130000001", "run_attempt": 1, "revision": main_revision}
    return PERSIST.seal(receipt, "receipt_sha256")


def no_op_warning(reason: str, *, generation_id: str = "1" * 64, fault_key: str | None = None) -> dict:
    material = {
        "source_id": "data_go_kr",
        "stage": "promotion",
        "reason": reason,
        "owner_ticket": 659,
        "failure_identity": generation_id,
    }
    return {
        "source_id": "data_go_kr",
        "stage": "promotion",
        "reason": reason,
        "severity": "warning",
        "owner_ticket": 659,
        "fault_key": fault_key or PERSIST.digest(material),
        "recommended_action": "Inspect the current candidate delivery state.",
        "status": "open",
        "first_seen_at": PRIOR_STARTED_AT,
        "last_seen_at": PRIOR_STARTED_AT,
        "observation_count": 2,
        "last_receipt_sha256": "e" * 64,
        "generation_id": generation_id,
        "producer_run_id": "36646768289",
        "recovery_evidence": None,
    }


def historical_publication_fault(generation_id: str) -> dict:
    material = {
        "source_id": "data_go_kr",
        "stage": "publication",
        "reason": "publication_or_readback_pending",
        "owner_ticket": 659,
        "failure_identity": generation_id,
    }
    return {
        "source_id": "data_go_kr",
        "stage": "publication",
        "reason": "publication_or_readback_pending",
        "severity": "warning",
        "owner_ticket": 659,
        "fault_key": PERSIST.digest(material),
        "recommended_action": "Verify publication and canonical read-back.",
        "generation_id": generation_id,
    }


def initial_state() -> dict:
    observation = {
        "observed_at": "2026-09-29T02:00:00Z",
        "producer_run_id": "36646768289",
        "refresh_evidence_sha256": "b" * 64,
    }
    last_good = {"status": "read-back-confirmed", "generation_id": "2" * 64}
    return {
        "faults": [promotion_fault()],
        "last_good_by_source": {"data_go_kr": copy.deepcopy(last_good)},
        "observations_by_source": {"data_go_kr": [copy.deepcopy(observation)]},
    }


def reduce(state: dict, receipt: dict) -> None:
    PERSIST.merge_faults(
        state,
        receipt,
        PROCESSOR_PATH,
        ALLOWED_EVENTS,
        PROMOTION_PATH,
        ALLOWED_EVENTS,
        300,
    )


class PromotionExecutionRecoveryTests(unittest.TestCase):
    def test_exact_sealed_noop_relation_recovers_only_current_generation_ack_warnings(self) -> None:
        for reason in ("promotion_ack_missing", "promotion_record_for_different_generation"):
            with self.subTest(reason=reason):
                state = initial_state()
                warning = no_op_warning(reason)
                state["faults"].append(warning)
                old_last_good = copy.deepcopy(state["last_good_by_source"])
                old_observations = copy.deepcopy(state["observations_by_source"])
                receipt = already_canonical_receipt()

                PERSIST.merge_observations(state, receipt)
                reduce(state, receipt)

                current = next(row for row in state["faults"] if row["fault_key"] == warning["fault_key"])
                self.assertEqual(current["status"], "recovered")
                self.assertEqual(current["recovery_evidence"]["basis"], "exact_processor_payload_already_canonical")
                self.assertEqual(current["recovery_evidence"]["fault_reason"], reason)
                self.assertEqual(current["recovery_evidence"]["generation_id"], "1" * 64)
                prior_c_failure = next(row for row in state["faults"] if row["fault_key"] == "a" * 64)
                self.assertEqual(prior_c_failure["status"], "recovery_pending_verification")
                self.assertEqual(state["last_good_by_source"], old_last_good)
                self.assertEqual(state["observations_by_source"], old_observations)

                replay_before = copy.deepcopy(state)
                reduce(state, receipt)
                self.assertEqual(state, replay_before)

    def test_noop_fault_recovery_rejects_mismatched_or_unsealed_relation(self) -> None:
        mutations = {
            "generation": lambda receipt: receipt["sources"][0]["canonical"]["already_canonical_candidate"].update(generation_id="2" * 64),
            "main_revision": lambda receipt: receipt["sources"][0]["canonical"]["already_canonical_candidate"].update(main_revision="e" * 40),
            "manifest": lambda receipt: receipt["sources"][0]["canonical"]["already_canonical_candidate"].update(main_manifest_sha256="9" * 64),
            "artifact_locator": lambda receipt: receipt["sources"][0]["processor"]["output_artifact"].update(artifact_id="88776655"),
            "unverified": lambda receipt: receipt["sources"][0]["canonical"]["already_canonical_candidate"].update(verified=False),
            "noncomposed_inventory_row": lambda receipt: receipt["sources"][0]["processor"]["output_digests"][1].update(bytes=999),
            "duplicate_inventory_path": lambda receipt: receipt["sources"][0]["processor"]["output_digests"][1].update(path="composed-candidate.registry.json"),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                state = initial_state()
                warning = no_op_warning("promotion_ack_missing")
                state["faults"].append(warning)
                receipt = already_canonical_receipt()
                mutate(receipt)
                PERSIST.seal(receipt, "receipt_sha256")
                reduce(state, receipt)
                recovered = next(row for row in state["faults"] if row["fault_key"] == warning["fault_key"])
                self.assertEqual(recovered["status"], "recovery_pending_verification")

        state = initial_state()
        wrong_key = no_op_warning("promotion_ack_missing", fault_key="f" * 64)
        state["faults"].append(wrong_key)
        reduce(state, already_canonical_receipt())
        self.assertEqual(
            next(row for row in state["faults"] if row["fault_key"] == wrong_key["fault_key"])["status"],
            "recovery_pending_verification",
        )

    def test_legacy_live_receipt_without_optional_relation_fields_cannot_recover_noop_warning(self) -> None:
        state = initial_state()
        warning = no_op_warning("promotion_ack_missing")
        state["faults"].append(warning)
        receipt = already_canonical_receipt()
        receipt["sources"][0]["processor"].pop("checkpoint_sha256")
        receipt["sources"][0]["canonical"].pop("already_canonical_candidate")
        PERSIST.seal(receipt, "receipt_sha256")

        reduce(state, receipt)

        current = next(row for row in state["faults"] if row["fault_key"] == warning["fault_key"])
        self.assertEqual(current["status"], "recovery_pending_verification")

    def test_historical_publication_fault_keeps_generation_until_trusted_readback(self) -> None:
        historical_generation = "2" * 64
        state = {
            "faults": [historical_publication_fault(historical_generation)],
            "last_good_by_source": {},
            "observations_by_source": {},
        }

        pending = already_canonical_receipt()
        pending["sources"][0]["canonical"]["publication"] = {
            "status": "publication-pending",
            "verified": False,
            "publisher_run_verified": None,
            "source_id": "data_go_kr",
            "generation_id": historical_generation,
        }
        pending["faults"] = [historical_publication_fault(historical_generation)]
        PERSIST.seal(pending, "receipt_sha256")

        reduce(state, pending)

        pending_fault = state["faults"][0]
        self.assertEqual(pending_fault["status"], "open")
        self.assertEqual(pending_fault["generation_id"], historical_generation)
        self.assertIsNone(pending_fault["recovery_evidence"])
        old_last_good = copy.deepcopy(state["last_good_by_source"])
        old_observations = copy.deepcopy(state["observations_by_source"])

        readback = already_canonical_receipt()
        readback["evaluated_at"] = "2026-10-03T09:00:00Z"
        readback["sources"][0]["canonical"]["publication"] = {
            "status": "read-back-confirmed",
            "verified": True,
            "publisher_run_verified": True,
            "source_id": "data_go_kr",
            "generation_id": historical_generation,
        }
        PERSIST.seal(readback, "receipt_sha256")

        reduce(state, readback)

        recovered = state["faults"][0]
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["generation_id"], historical_generation)
        self.assertEqual(recovered["recovery_evidence"]["stage"], "publication")
        self.assertEqual(recovered["recovery_evidence"]["generation_id"], historical_generation)
        self.assertEqual(state["last_good_by_source"], old_last_good)
        self.assertEqual(state["observations_by_source"], old_observations)

    def test_processor_recovery_uses_processor_generation_with_historical_publication_present(self) -> None:
        processor_fault = {
            "source_id": "data_go_kr",
            "stage": "processor",
            "reason": "candidate_output_unavailable",
            "severity": "error",
            "owner_ticket": 659,
            "fault_key": "9" * 64,
            "recommended_action": "Inspect the processor output.",
            "status": "open",
            "first_seen_at": PRIOR_STARTED_AT,
            "last_seen_at": PRIOR_STARTED_AT,
            "observation_count": 1,
            "last_receipt_sha256": "e" * 64,
            "generation_id": None,
            "producer_run_id": None,
            "recovery_evidence": None,
        }
        state = {"faults": [processor_fault], "last_good_by_source": {}, "observations_by_source": {}}
        receipt = already_canonical_receipt()
        receipt["sources"][0]["canonical"]["publication"] = {
            "status": "read-back-confirmed",
            "verified": True,
            "publisher_run_verified": True,
            "source_id": "data_go_kr",
            "generation_id": "2" * 64,
        }
        PERSIST.seal(receipt, "receipt_sha256")

        reduce(state, receipt)

        recovered = state["faults"][0]
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["recovery_evidence"]["generation_id"], "1" * 64)

    def test_later_trusted_c_success_recovers_only_execution_fault(self) -> None:
        state = initial_state()
        old_last_good = copy.deepcopy(state["last_good_by_source"])
        old_observations = copy.deepcopy(state["observations_by_source"])
        success = c_run()
        receipt = live_receipt(success=success)
        PERSIST.merge_observations(state, receipt)
        reduce(state, receipt)

        recovered = state["faults"][0]
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["recovery_evidence"], {
            "verified": True,
            "stage": "promotion-execution",
            "health_receipt_sha256": "f" * 64,
            "execution_run_id": success["run_id"],
            "execution_run_attempt": success["run_attempt"],
        })
        self.assertEqual(state["last_good_by_source"], old_last_good)
        self.assertEqual(state["observations_by_source"], old_observations)

    def test_workflow_dispatch_is_an_allowed_c_recovery_event(self) -> None:
        state = initial_state()
        success = c_run(event="workflow_dispatch")
        reduce(state, live_receipt(success=success))
        self.assertEqual(state["faults"][0]["status"], "recovered")

    def test_later_trusted_success_recovers_even_if_newer_run_is_pending_or_skipped(self) -> None:
        success_after_failure = c_run(run_id="37110000001", started_at="2026-10-03T08:06:00Z")
        for pending in (
            c_run(run_id="37110000002", started_at="2026-10-03T08:07:00Z", status="in_progress", conclusion="pending"),
            c_run(run_id="37110000002", started_at="2026-10-03T08:07:00Z", status="completed", conclusion="skipped"),
            c_run(run_id="37110000002", started_at="2026-10-03T08:07:00Z", status="completed", conclusion="neutral"),
        ):
            with self.subTest(status=pending["status"], conclusion=pending["conclusion"]):
                state = initial_state()
                reduce(state, live_receipt(success=success_after_failure, latest=pending))
                self.assertEqual(state["faults"][0]["status"], "recovered")
                self.assertEqual(state["faults"][0]["recovery_evidence"]["execution_run_id"], success_after_failure["run_id"])

    def test_pending_or_skipped_alone_cannot_clear_old_c_failure(self) -> None:
        success_before_failure = c_run(run_id="37110000001", started_at="2026-10-03T08:04:00Z")
        for pending in (
            c_run(run_id="37110000002", started_at="2026-10-03T08:07:00Z", status="in_progress", conclusion="pending"),
            c_run(run_id="37110000002", started_at="2026-10-03T08:07:00Z", status="completed", conclusion="skipped"),
            c_run(run_id="37110000002", started_at="2026-10-03T08:07:00Z", status="completed", conclusion="neutral"),
        ):
            with self.subTest(status=pending["status"], conclusion=pending["conclusion"]):
                state = initial_state()
                reduce(state, live_receipt(success=success_before_failure, latest=pending))
                self.assertEqual(state["faults"][0]["status"], "recovery_pending_verification")
                self.assertFalse(state["faults"][0]["recovery_evidence"]["verified"])

    def test_created_or_updated_time_cannot_replace_later_attempt_start(self) -> None:
        success = c_run(started_at="2026-10-03T08:04:00Z")
        success["created_at"] = "2026-10-03T08:09:00Z"
        success["updated_at"] = "2026-10-03T08:09:30Z"
        state = initial_state()
        reduce(state, live_receipt(success=success))
        self.assertEqual(state["faults"][0]["status"], "recovery_pending_verification")

    def test_success_after_receipt_future_skew_is_rejected(self) -> None:
        success = c_run(started_at="2026-10-03T08:16:00Z")
        state = initial_state()
        reduce(state, live_receipt(success=success))
        self.assertEqual(state["faults"][0]["status"], "recovery_pending_verification")

    def test_historical_missing_c_summary_and_b_success_do_not_clear_fault(self) -> None:
        state = initial_state()
        receipt = live_receipt(include_canonical=False)
        reduce(state, receipt)
        self.assertEqual(state["faults"][0]["status"], "recovery_pending_verification")

        state = initial_state()
        source = live_receipt()["sources"][0]
        source["canonical"].pop("promotion_execution")
        receipt = live_receipt(include_canonical=False)
        receipt["sources"] = [source]
        reduce(state, receipt)
        self.assertEqual(state["faults"][0]["status"], "recovery_pending_verification")

    def test_current_c_execution_unavailable_error_blocks_recovery(self) -> None:
        state = initial_state()
        unavailable = promotion_fault(fault_key="c" * 64, reason="promotion_workflow_attempt_unavailable")
        unavailable.pop("execution_identity")
        reduce(state, live_receipt(success=c_run(), faults=[unavailable]))
        prior = next(row for row in state["faults"] if row["fault_key"] == "a" * 64)
        self.assertEqual(prior["status"], "recovery_pending_verification")
        self.assertEqual({row["fault_key"] for row in state["faults"]}, {"a" * 64, "c" * 64})

    def test_old_api_unavailable_fault_is_not_reclassified_as_a_failed_run(self) -> None:
        state = initial_state()
        state["faults"][0]["reason"] = "promotion_workflow_attempt_unavailable"
        reduce(state, live_receipt(success=c_run()))
        self.assertEqual(state["faults"][0]["status"], "recovery_pending_verification")

    def test_untrusted_identity_variants_do_not_clear_fault(self) -> None:
        variants = {
            "workflow_id_mismatch": lambda success, latest: latest.update(workflow_id=success["workflow_id"] + 1),
            "wrong_path": lambda success, latest: success.update(path=PROCESSOR_PATH),
            "wrong_event": lambda success, latest: success.update(event="pull_request"),
            "wrong_repository": lambda success, latest: success.update(repository="Other/repo"),
            "wrong_head_repository": lambda success, latest: success.update(head_repository="Other/repo"),
            "wrong_branch": lambda success, latest: success.update(head_branch="release"),
            "invalid_head": lambda success, latest: success.update(head_sha="not-a-revision"),
            "bad_attempt": lambda success, latest: success.update(run_attempt=0),
        }
        for name, mutate in variants.items():
            with self.subTest(name=name):
                success = c_run()
                latest = copy.deepcopy(success)
                mutate(success, latest)
                state = initial_state()
                reduce(state, live_receipt(success=success, latest=latest))
                self.assertEqual(state["faults"][0]["status"], "recovery_pending_verification")

    def test_current_failure_remains_open_and_is_deduplicated_by_fault_key(self) -> None:
        state = initial_state()
        current_failure = promotion_fault()
        receipt = live_receipt(faults=[current_failure, copy.deepcopy(current_failure)])
        reduce(state, receipt)
        self.assertEqual(len(state["faults"]), 1)
        self.assertEqual(state["faults"][0]["status"], "open")
        self.assertEqual(state["faults"][0]["observation_count"], 2)

        reduce(state, receipt)
        self.assertEqual(len(state["faults"]), 1)
        self.assertEqual(state["faults"][0]["observation_count"], 2)


if __name__ == "__main__":
    unittest.main()
