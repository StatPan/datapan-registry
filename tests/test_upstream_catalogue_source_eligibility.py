from __future__ import annotations

import copy
import contextlib
import datetime as dt
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import jsonschema
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/upstream_catalogue_source_eligibility.py"
SPEC = importlib.util.spec_from_file_location("upstream_catalogue_source_eligibility_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ELIGIBILITY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ELIGIBILITY
SPEC.loader.exec_module(ELIGIBILITY)
PREPARER_SPEC = importlib.util.spec_from_file_location(
    "prepare_upstream_catalogue_derivation_source_contract_test",
    ROOT / "scripts/prepare-upstream-catalogue-derivation.py",
)
assert PREPARER_SPEC is not None and PREPARER_SPEC.loader is not None
PREPARER = importlib.util.module_from_spec(PREPARER_SPEC)
sys.modules[PREPARER_SPEC.name] = PREPARER
PREPARER_SPEC.loader.exec_module(PREPARER)
PROCESSOR_SPEC = importlib.util.spec_from_file_location(
    "process_upstream_catalogue_source_epoch_test",
    ROOT / "scripts/process-upstream-catalogue-candidate.py",
)
assert PROCESSOR_SPEC is not None and PROCESSOR_SPEC.loader is not None
PROCESSOR = importlib.util.module_from_spec(PROCESSOR_SPEC)
sys.modules[PROCESSOR_SPEC.name] = PROCESSOR
PROCESSOR_SPEC.loader.exec_module(PROCESSOR)

DIGEST = "a" * 64
SHA = "b" * 40
NOW = "2026-10-07T00:00:00Z"


class SourceEligibilityTest(unittest.TestCase):
    def git(self, root: pathlib.Path, *args: str) -> str:
        result = subprocess.run(
            ("git", *args), cwd=root, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True,
        )
        return result.stdout.strip()

    def contract_repo(self) -> tuple[tempfile.TemporaryDirectory[str], pathlib.Path, str]:
        temp = tempfile.TemporaryDirectory(prefix="source-eligibility-contract-")
        root = pathlib.Path(temp.name)
        self.git(root, "init", "-q")
        self.git(root, "config", "user.name", "test")
        self.git(root, "config", "user.email", "test@example.invalid")
        for position, path in enumerate(ELIGIBILITY.CONTRACT_PATHS):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{position}:{path}\n", encoding="utf-8")
        (root / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json").write_bytes(
            (ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json").read_bytes()
        )
        self.git(root, "add", ".")
        self.git(root, "commit", "-qm", "base")
        return temp, root, self.git(root, "rev-parse", "HEAD")

    def commit_change(self, root: pathlib.Path, path: str, value: str) -> str:
        (root / path).write_text(value, encoding="utf-8")
        self.git(root, "add", path)
        self.git(root, "commit", "-qm", f"change {path}")
        return self.git(root, "rev-parse", "HEAD")

    def evaluation(self, contract: dict) -> dict:
        return {
            "source_sha": contract["source_sha"],
            "source_tree_sha": contract["source_tree_sha"],
            "event": "schedule",
            "run_id": "123456",
            "run_attempt": 1,
            "as_of": NOW,
        }

    def admission(self) -> dict:
        return {
            "admission_id": "9" * 64,
            "producer_run_id": "111111",
            "run_attempt": 1,
            "head_sha": SHA,
            "artifact_id": "222222",
            "artifact_name": "upstream-catalog-refresh-111111",
            "artifact_digest_sha256": "c" * 64,
            "refresh_evidence_sha256": "d" * 64,
            "observed_at": NOW,
            "candidate_sha256": "8" * 64,
        }

    def evidence(self, contract: dict, generation_id: str = DIGEST) -> dict:
        epoch = ELIGIBILITY.build_source_epoch(
            original_observation_sha256="e" * 64,
            target_contract_sha256=contract["contract_sha256"],
            predecessor_generation_id=generation_id,
            target_generation_id=None,
            ordinal=2,
        )
        return {
            "admission": self.admission(),
            "original_generation": {
                "generation_id": generation_id,
                "checkpoint_ref": "f" * 64,
                "epoch": 1,
                "epoch_origin": "legacy-checkpoint-v1",
            },
            "target_contract": contract,
            "source_epoch": epoch,
        }

    def candidate(
        self, contract: dict, *, prior_contract: dict | None, status: str = "no-change",
        canonical_completed: bool = False, accounting_exhausted: bool = False,
        lease: dict | None = None, generation_id: str = DIGEST,
        observed_at: str = "2026-09-01T00:00:00Z",
    ) -> dict:
        return {
            "source_id": "data_go_kr",
            "source_scope": "aggregate_supported_catalog",
            "generation_id": generation_id,
            "status": status,
            "artifact_expires_at": "2026-11-07T00:00:00Z",
            "accounting_exhausted": accounting_exhausted,
            "canonical_completed": canonical_completed,
            "lease": lease,
            "target_contract": prior_contract,
            "original_observation": {"observed_at": observed_at},
            "evidence": self.evidence(contract, generation_id),
        }

    def select(self, contract: dict, candidates: list[dict]) -> dict:
        return ELIGIBILITY.select_source_work(
            repository="StatPan/datapan-registry",
            source_id="data_go_kr",
            source_scope="aggregate_supported_catalog",
            evaluation=self.evaluation(contract),
            target_contract=contract,
            candidates=candidates,
        )

    def test_inventory_is_exact_ordered_and_digest_bound(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        contract, inventory = ELIGIBILITY.build_target_contract(root, head)
        self.assertEqual([row["path"] for row in inventory], list(ELIGIBILITY.CONTRACT_PATHS))
        self.assertEqual(contract["inventory_sha256"], ELIGIBILITY.object_sha256(inventory))
        self.assertEqual(
            contract["contract_sha256"],
            ELIGIBILITY.target_contract_digest(contract["inventory_sha256"]),
        )
        self.assertEqual(contract["changed_classes"], [ELIGIBILITY.SAME_A_PROCESSING])

    def test_target_identity_ignores_unrelated_git_carrier_changes(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        prior, inventory = ELIGIBILITY.build_target_contract(root, head)
        unrelated = self.commit_change(root, "README.md", "generated-only carrier change\n")
        current, current_inventory = ELIGIBILITY.build_target_contract(
            root, unrelated, previous_inventory=inventory,
        )

        self.assertNotEqual(current["source_sha"], prior["source_sha"])
        self.assertNotEqual(current["source_tree_sha"], prior["source_tree_sha"])
        self.assertEqual(current_inventory, inventory)
        self.assertEqual(current["inventory_sha256"], prior["inventory_sha256"])
        self.assertEqual(current["contract_sha256"], prior["contract_sha256"])
        self.assertEqual(current["changed_classes"], [])
        self.assertEqual(
            ELIGIBILITY.selection_key(self.admission(), current),
            ELIGIBILITY.selection_key(self.admission(), prior),
        )
        result = self.select(current, [self.candidate(current, prior_contract=prior)])
        self.assertEqual(
            (result["action"], result["reason_code"]),
            ("revalidate_c", "target_contract_requires_c_completion"),
        )

        tampered = copy.deepcopy(current)
        tampered["inventory_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "target_contract_digest_mismatch"):
            ELIGIBILITY.validate_target_contract(tampered)

    def test_c_only_change_is_classified_without_b_rebuild(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        _old, inventory = ELIGIBILITY.build_target_contract(root, head)
        successor = self.commit_change(root, "scripts/canonical_update_ci.py", "changed\n")
        current, _ = ELIGIBILITY.build_target_contract(root, successor, previous_inventory=inventory)
        self.assertEqual(current["changed_classes"], [ELIGIBILITY.C_ONLY_REVALIDATION])

    def test_collector_contract_change_requires_new_a(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        _old, inventory = ELIGIBILITY.build_target_contract(root, head)
        successor = self.commit_change(root, "policy/source-refresh.json", "changed\n")
        current, _ = ELIGIBILITY.build_target_contract(root, successor, previous_inventory=inventory)
        self.assertEqual(current["changed_classes"], [ELIGIBILITY.REQUIRES_NEW_A])

    def test_preparer_recomputes_changed_classes_from_the_sealed_predecessor(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        prior, inventory = ELIGIBILITY.build_target_contract(root, head)
        successor = self.commit_change(
            root, "scripts/compose-upstream-catalogue-candidate.py", "changed\n",
        )
        current, current_inventory = ELIGIBILITY.build_target_contract(
            root, successor, previous_inventory=inventory,
        )
        predecessor = {
            "generation_id": DIGEST,
            "checkpoint_sha256": "f" * 64,
            "generation_inputs": {"source_target_contract": prior},
        }
        original = {"producer_head_sha": head}
        request = {
            "evaluation": {"source_sha": successor},
            "target_contract": current,
            "target_inventory": current_inventory,
            "source_subject": ELIGIBILITY.source_work_subject(
                self.admission(), original, predecessor,
                prior, current,
            ),
        }
        PREPARER.validate_source_work_request_target(
            request,
            main_root=root,
            predecessor=predecessor,
            original_observation=original,
            admitted_parent=self.admission(),
            eligibility=ELIGIBILITY,
        )

        tampered = copy.deepcopy(request)
        tampered["target_contract"]["changed_classes"] = [ELIGIBILITY.C_ONLY_REVALIDATION]
        with self.assertRaisesRegex(ValueError, "source_work_request_classification_mismatch"):
            PREPARER.validate_source_work_request_target(
                tampered,
                main_root=root,
                predecessor=predecessor,
                original_observation=original,
                admitted_parent=self.admission(),
                eligibility=ELIGIBILITY,
            )

        legacy = {"generation_inputs": {}}
        collector_successor = self.commit_change(root, "policy/source-refresh.json", "changed\n")
        legacy_contract, _legacy_inventory = ELIGIBILITY.target_contract_from_predecessor(
            root, collector_successor, legacy, original,
        )
        self.assertEqual(legacy_contract["changed_classes"], [ELIGIBILITY.REQUIRES_NEW_A])

    def test_same_a_change_selects_rebuild(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        prior, inventory = ELIGIBILITY.build_target_contract(root, head)
        successor = self.commit_change(root, "scripts/compose-upstream-catalogue-candidate.py", "changed\n")
        current, _ = ELIGIBILITY.build_target_contract(root, successor, previous_inventory=inventory)
        result = self.select(current, [self.candidate(current, prior_contract=prior)])
        self.assertEqual(result["action"], "rebuild")
        self.assertEqual(result["reason_code"], "same_a_processing_contract_changed")
        self.assertIsNotNone(result["selection_key"])

    def test_c_only_change_selects_revalidation_without_generation_authority(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        prior, inventory = ELIGIBILITY.build_target_contract(root, head)
        successor = self.commit_change(root, "scripts/canonical_update_ci.py", "changed\n")
        current, _ = ELIGIBILITY.build_target_contract(root, successor, previous_inventory=inventory)
        result = self.select(current, [self.candidate(current, prior_contract=prior)])
        self.assertEqual(result["action"], "revalidate_c")
        self.assertEqual(result["reason_code"], "c_only_contract_changed")

    def test_completed_same_contract_is_noop_without_evidence_authority(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        current, _ = ELIGIBILITY.build_target_contract(root, head)
        result = self.select(current, [self.candidate(current, prior_contract=current, canonical_completed=True)])
        self.assertEqual(result["action"], "no_op")
        self.assertIsNone(result["selection_key"])
        self.assertIsNone(result["evidence"])

    def test_live_lease_defers_before_rebuild(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        prior, inventory = ELIGIBILITY.build_target_contract(root, head)
        successor = self.commit_change(root, "scripts/compose-upstream-catalogue-candidate.py", "changed\n")
        current, _ = ELIGIBILITY.build_target_contract(root, successor, previous_inventory=inventory)
        candidate = self.candidate(
            current, prior_contract=prior, status="retry",
            lease={"expires_at": "2026-10-07T01:00:00Z"},
        )
        result = self.select(current, [candidate])
        self.assertEqual((result["action"], result["reason_code"]), ("deferred", "predecessor_live_lease"))

    def test_push_and_schedule_converge_on_one_epoch_after_predecessor_finishes(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        prior, inventory = ELIGIBILITY.build_target_contract(root, head)
        successor = self.commit_change(
            root, "scripts/compose-upstream-catalogue-candidate.py", "changed\n",
        )
        current, _ = ELIGIBILITY.build_target_contract(
            root, successor, previous_inventory=inventory,
        )
        live = self.candidate(
            current, prior_contract=prior, status="retry",
            lease={"expires_at": "2026-10-07T01:00:00Z"},
        )
        push_evaluation = {**self.evaluation(current), "event": "push", "run_id": "123457"}
        deferred = ELIGIBILITY.select_source_work(
            repository="StatPan/datapan-registry",
            source_id="data_go_kr",
            source_scope="aggregate_supported_catalog",
            evaluation=push_evaluation,
            target_contract=current,
            candidates=[live],
        )
        self.assertEqual(
            (deferred["action"], deferred["reason_code"], deferred["selection_key"]),
            ("deferred", "predecessor_live_lease", None),
        )

        finished = copy.deepcopy(live)
        finished["status"] = "no-change"
        finished["lease"] = {"expires_at": "2026-10-06T23:59:59Z"}
        scheduled = self.select(current, [finished])
        pushed = ELIGIBILITY.select_source_work(
            repository="StatPan/datapan-registry",
            source_id="data_go_kr",
            source_scope="aggregate_supported_catalog",
            evaluation=push_evaluation,
            target_contract=current,
            candidates=[finished],
        )
        self.assertEqual((scheduled["action"], pushed["action"]), ("rebuild", "rebuild"))
        self.assertEqual(scheduled["selection_key"], pushed["selection_key"])
        self.assertEqual(
            scheduled["evidence"]["source_epoch"]["epoch_id"],
            pushed["evidence"]["source_epoch"]["epoch_id"],
        )

    def test_changed_contract_waits_for_an_active_predecessor_without_a_live_lease(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        prior, inventory = ELIGIBILITY.build_target_contract(root, head)
        successor = self.commit_change(root, "scripts/compose-upstream-catalogue-candidate.py", "changed\n")
        current, _ = ELIGIBILITY.build_target_contract(root, successor, previous_inventory=inventory)
        candidate = self.candidate(current, prior_contract=prior, status="retry")
        result = self.select(current, [candidate])
        self.assertEqual(
            (result["action"], result["reason_code"]),
            ("deferred", "predecessor_generation_still_active"),
        )

    def test_older_deferred_work_blocks_overtaking_but_closed_rows_do_not_starve(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        prior, inventory = ELIGIBILITY.build_target_contract(root, head)
        successor = self.commit_change(
            root, "scripts/compose-upstream-catalogue-candidate.py", "changed\n",
        )
        current, _ = ELIGIBILITY.build_target_contract(
            root, successor, previous_inventory=inventory,
        )
        older_active = self.candidate(
            current, prior_contract=prior, status="retry", generation_id="1" * 64,
            observed_at="2026-08-01T00:00:00Z",
        )
        later_terminal = self.candidate(
            current, prior_contract=prior, generation_id="2" * 64,
            observed_at="2026-09-01T00:00:00Z",
        )
        deferred = self.select(current, [later_terminal, older_active])
        self.assertEqual(
            (deferred["action"], deferred["reason_code"], deferred["selection_key"]),
            ("deferred", "predecessor_generation_still_active", None),
        )

        expired = copy.deepcopy(older_active)
        expired["status"] = "no-change"
        expired["artifact_expires_at"] = "2026-10-06T00:00:00Z"
        selected = self.select(current, [later_terminal, expired])
        self.assertEqual(selected["action"], "rebuild")
        self.assertEqual(
            selected["evidence"]["original_generation"]["generation_id"],
            later_terminal["generation_id"],
        )

    def test_expiry_and_budget_exhaustion_never_select(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        current, _ = ELIGIBILITY.build_target_contract(root, head)
        expired = self.candidate(current, prior_contract=None)
        expired["artifact_expires_at"] = "2026-10-06T00:00:00Z"
        self.assertEqual(self.select(current, [expired])["action"], "requires_new_a")
        exhausted = self.candidate(current, prior_contract=None, accounting_exhausted=True)
        self.assertEqual(self.select(current, [exhausted])["action"], "blocked")

    def test_state_locator_uses_durable_attempt_budget_before_selecting_rebuild(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        prior_contract, prior_inventory = ELIGIBILITY.build_target_contract(root, head)
        self.assertEqual(prior_contract["source_sha"], head)
        successor = self.commit_change(
            root, "scripts/upstream_catalogue_cache_source.py", "changed cache-source helper\n",
        )
        current_contract, current_inventory = ELIGIBILITY.build_target_contract(
            root, successor, previous_inventory=prior_inventory,
        )
        self.assertEqual(current_contract["changed_classes"], [ELIGIBILITY.SAME_A_PROCESSING])
        changed_rows = [
            row for old, row in zip(prior_inventory, current_inventory)
            if old != row
        ]
        self.assertEqual([row["path"] for row in changed_rows], ["scripts/upstream_catalogue_cache_source.py"])
        self.assertNotEqual(
            next(row["sha256"] for row in prior_inventory if row["path"] == changed_rows[0]["path"]),
            changed_rows[0]["sha256"],
        )
        checkpoint = json.loads((
            ROOT / "tests/fixtures/same-observation-derivation/native-b-9938.json"
        ).read_text(encoding="utf-8"))
        generation_id = checkpoint["generation_id"]
        state_dir = root / "state"
        generation_dir = state_dir / "sources/data_go_kr/generations"
        generation_dir.mkdir(parents=True)
        index_path = state_dir / "sources/data_go_kr/index.json"
        index = {
            "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
            "generations": [{"generation_id": generation_id, "checkpoint": f"{generation_id}.json"}],
            "collector_handoff": {"test_only": True},
        }
        index_path.write_text(json.dumps(index), encoding="utf-8")
        checkpoint_path = generation_dir / f"{generation_id}.json"
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
        ref = checkpoint["input_artifacts"][0]
        admission = {
            "admission_id": "9" * 64,
            "producer_run_id": str(ref["run_id"]),
            "run_attempt": 1,
            "head_sha": head,
            "artifact_id": str(ref["artifact_id"]),
            "artifact_name": ref["name"],
            "artifact_digest_sha256": "7" * 64,
            "refresh_evidence_sha256": ref["evidence_sha256"],
            "observed_at": checkpoint["last_observation"]["observed_at"],
            "candidate_sha256": ref["candidate_sha256"],
        }
        admitted_observations = [admission]
        handoff = types.SimpleNamespace(
            validate_ledger=lambda _value: {"admitted_observations": admitted_observations},
        )
        derivation = types.SimpleNamespace(
            validate_derivation_envelope=lambda value: value,
            original_observation_from_checkpoint=lambda candidate, _admission: {
                "producer_head_sha": head,
                "observed_at": candidate["last_observation"]["observed_at"],
            },
        )
        processor = types.SimpleNamespace(DEFAULT_MAX_ATTEMPTS=24)

        def load_module(path: pathlib.Path, _name: str):
            return {
                "upstream_catalogue_handoff.py": handoff,
                "upstream_catalogue_derivation.py": derivation,
                "process-upstream-catalogue-candidate.py": processor,
            }[path.name]

        evaluation = {
            "source_sha": successor,
            "source_tree_sha": self.git(root, "rev-parse", f"{successor}^{{tree}}"),
            "event": "schedule",
            "run_id": "123456",
            "run_attempt": 1,
            "as_of": NOW,
        }
        with mock.patch.object(ELIGIBILITY, "_load_local_module", side_effect=load_module):
            exhausted = ELIGIBILITY.source_work_request_from_state(
                root=root, state_dir=state_dir, repository="StatPan/datapan-registry",
                source_id="data_go_kr", source_scope="aggregate_supported_catalog",
                evaluation=evaluation,
            )
        self.assertEqual(
            (exhausted["action"], exhausted["reason_code"]),
            ("blocked", "retained_request_budget_exhausted"),
        )

        checkpoint["attempts_consumed"] = 23
        unsigned = dict(checkpoint)
        unsigned.pop("checkpoint_sha256")
        checkpoint["checkpoint_sha256"] = ELIGIBILITY.object_sha256(unsigned)
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
        with mock.patch.object(ELIGIBILITY, "_load_local_module", side_effect=load_module):
            selectable = ELIGIBILITY.source_work_request_from_state(
                root=root, state_dir=state_dir, repository="StatPan/datapan-registry",
                source_id="data_go_kr", source_scope="aggregate_supported_catalog",
                evaluation=evaluation,
            )
        self.assertEqual(
            (selectable["action"], selectable["reason_code"]),
            ("rebuild", "same_a_processing_contract_changed"),
        )
        self.assertEqual(selectable["target_contract"], current_contract)
        self.assertEqual(selectable["target_inventory"], current_inventory)
        self.assertEqual(
            selectable["target_contract"]["changed_classes"],
            [ELIGIBILITY.SAME_A_PROCESSING],
        )

        # The workflow-facing state adapter must preserve the same oldest-work
        # barrier as the pure selector.  In particular, a push cannot skip an
        # older live predecessor and claim a later terminal generation.
        checkpoint["status"] = "retry"
        checkpoint["lease"] = {
            "owner_run_id": "older-live-owner",
            "expires_at": "2026-10-08T00:00:00Z",
            "fencing_token": 2,
        }
        unsigned = dict(checkpoint)
        unsigned.pop("checkpoint_sha256")
        checkpoint["checkpoint_sha256"] = ELIGIBILITY.object_sha256(unsigned)
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

        later = copy.deepcopy(checkpoint)
        later_id = "8" * 64
        later["generation_id"] = later_id
        later["status"] = "no-change"
        later["lease"] = None
        later["observed_at"] = "2026-10-01T00:00:00Z"
        later["last_observation"]["observed_at"] = "2026-10-01T00:00:00Z"
        unsigned = dict(later)
        unsigned.pop("checkpoint_sha256")
        later["checkpoint_sha256"] = ELIGIBILITY.object_sha256(unsigned)
        later_path = generation_dir / f"{later_id}.json"
        later_path.write_text(json.dumps(later), encoding="utf-8")
        index["generations"].append({
            "generation_id": later_id,
            "checkpoint": f"{later_id}.json",
        })
        index_path.write_text(json.dumps(index), encoding="utf-8")
        with mock.patch.object(ELIGIBILITY, "_load_local_module", side_effect=load_module):
            deferred = ELIGIBILITY.source_work_request_from_state(
                root=root, state_dir=state_dir, repository="StatPan/datapan-registry",
                source_id="data_go_kr", source_scope="aggregate_supported_catalog",
                evaluation={**evaluation, "event": "push"}, include_active=True,
            )
        self.assertEqual(
            (
                deferred["action"], deferred["reason_code"],
                deferred["selected_generation_id"],
            ),
            ("deferred", "predecessor_live_lease", None),
        )

        checkpoint["status"] = "no-change"
        checkpoint["lease"]["expires_at"] = "2026-10-06T23:59:59Z"
        unsigned = dict(checkpoint)
        unsigned.pop("checkpoint_sha256")
        checkpoint["checkpoint_sha256"] = ELIGIBILITY.object_sha256(unsigned)
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
        with mock.patch.object(ELIGIBILITY, "_load_local_module", side_effect=load_module):
            recovered = ELIGIBILITY.source_work_request_from_state(
                root=root, state_dir=state_dir, repository="StatPan/datapan-registry",
                source_id="data_go_kr", source_scope="aggregate_supported_catalog",
                evaluation=evaluation,
            )
        with mock.patch.object(ELIGIBILITY, "_load_local_module", side_effect=load_module):
            duplicate_push = ELIGIBILITY.source_work_request_from_state(
                root=root, state_dir=state_dir, repository="StatPan/datapan-registry",
                source_id="data_go_kr", source_scope="aggregate_supported_catalog",
                evaluation={**evaluation, "event": "push", "run_id": "123457"},
                include_active=True,
            )
        for decision in (recovered, duplicate_push):
            self.assertEqual(
                (
                    decision["action"], decision["reason_code"],
                    decision["selected_generation_id"],
                ),
                ("rebuild", "same_a_processing_contract_changed", generation_id),
            )
        self.assertEqual(recovered["target_contract"], duplicate_push["target_contract"])
        self.assertEqual(recovered["target_inventory"], duplicate_push["target_inventory"])
        self.assertEqual(recovered["producer_run_id"], duplicate_push["producer_run_id"])
        self.assertEqual(recovered["artifact_id"], duplicate_push["artifact_id"])

        index["generations"].append(copy.deepcopy(index["generations"][0]))
        index_path.write_text(json.dumps(index), encoding="utf-8")
        with (
            mock.patch.object(ELIGIBILITY, "_load_local_module", side_effect=load_module),
            self.assertRaisesRegex(ValueError, "source_selection_index_generation_duplicate"),
        ):
            ELIGIBILITY.source_work_request_from_state(
                root=root, state_dir=state_dir, repository="StatPan/datapan-registry",
                source_id="data_go_kr", source_scope="aggregate_supported_catalog",
                evaluation=evaluation,
            )

    def test_source_epoch_identity_is_stable_and_generation_binding_is_non_recursive(self) -> None:
        epoch = ELIGIBILITY.build_source_epoch(
            original_observation_sha256="1" * 64,
            target_contract_sha256="2" * 64,
            predecessor_generation_id="3" * 64,
            target_generation_id=None,
            ordinal=2,
        )
        generation_id, inputs = ELIGIBILITY.bind_source_epoch_generation({"source_epoch": epoch, "x": 1})
        self.assertEqual(inputs["source_epoch"]["target_generation_id"], generation_id)
        replay_id, replay_inputs = ELIGIBILITY.bind_source_epoch_generation(inputs)
        self.assertEqual((replay_id, replay_inputs), (generation_id, inputs))
        changed_predecessor = ELIGIBILITY.build_source_epoch(
            original_observation_sha256="1" * 64,
            target_contract_sha256="2" * 64,
            predecessor_generation_id="4" * 64,
            target_generation_id=None,
            ordinal=3,
        )
        self.assertEqual(epoch["epoch_id"], changed_predecessor["epoch_id"])

    def test_source_recovery_result_separates_produced_from_durable_rebuild(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        prior, inventory = ELIGIBILITY.build_target_contract(root, head)
        successor = self.commit_change(
            root, "scripts/compose-upstream-catalogue-candidate.py", "changed\n",
        )
        current, current_inventory = ELIGIBILITY.build_target_contract(
            root, successor, previous_inventory=inventory,
        )
        checkpoint = {
            "generation_id": DIGEST,
            "checkpoint_sha256": "f" * 64,
            "generation_inputs": {"source_target_contract": prior},
        }
        request = ELIGIBILITY.validate_source_work_request({
            "schema_version": ELIGIBILITY.SOURCE_WORK_REQUEST_SCHEMA_VERSION,
            "repository": "StatPan/datapan-registry",
            "source_id": "data_go_kr",
            "source_scope": "aggregate_supported_catalog",
            "evaluation": {
                "source_sha": successor,
                "source_tree_sha": self.git(root, "rev-parse", f"{successor}^{{tree}}"),
                "event": "push",
                "run_id": "123456",
                "run_attempt": 1,
                "as_of": NOW,
            },
            "target_contract": current,
            "target_inventory": current_inventory,
            "source_subject": ELIGIBILITY.source_work_subject(
                self.admission(), {"observed_at": NOW}, checkpoint, prior, current,
            ),
            "action": "rebuild",
            "reason_code": "same_a_processing_contract_changed",
            "selected_generation_id": DIGEST,
            "producer_run_id": "111111",
            "artifact_id": "222222",
            "artifact_name": "upstream-catalog-refresh-111111",
            "selection_state_sha": "1" * 40,
        }, root=root)
        processing = {
            "status": "ready",
            "reason": "processing_complete",
            "source_id": "data_go_kr",
            "generation_id": "7" * 64,
            "processor_run_id": "123456-1",
            "processor_artifact_run_id": "123456",
        }
        interrupted = ELIGIBILITY.build_source_recovery_result(
            request, processing, root=root, repository="StatPan/datapan-registry",
            event="push", run_id="123456", run_attempt=1, head_sha=successor,
            decision="process", selected_generation_id=DIGEST,
            selected_producer_run_id="111111", persistence_status="interrupted",
            result_state_sha="2" * 40,
        )
        self.assertEqual(
            (interrupted["execution"]["outcome"], interrupted["execution"]["reason_code"]),
            ("blocked", "source_recovery_state_not_persisted"),
        )
        durable = ELIGIBILITY.build_source_recovery_result(
            request, processing, root=root, repository="StatPan/datapan-registry",
            event="push", run_id="123456", run_attempt=1, head_sha=successor,
            decision="process", selected_generation_id=DIGEST,
            selected_producer_run_id="111111", persistence_status="published",
            result_state_sha="3" * 40,
        )
        self.assertEqual(durable["execution"]["outcome"], "rebuilt")
        self.assertEqual(durable["execution"]["result_generation_id"], "7" * 64)
        self.assertEqual(durable["source_work_request"]["selection_state_sha"], "1" * 40)
        self.assertEqual(durable["execution"]["result_state_sha"], "3" * 40)
        with self.assertRaisesRegex(ValueError, "source_recovery_execution_result_state_missing"):
            ELIGIBILITY.build_source_recovery_result(
                request, processing, root=root, repository="StatPan/datapan-registry",
                event="push", run_id="123456", run_attempt=1, head_sha=successor,
                decision="process", selected_generation_id=DIGEST,
                selected_producer_run_id="111111", persistence_status="published",
            )

    def test_no_admitted_a_idle_result_is_exact_and_tamper_closed(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        request = ELIGIBILITY.source_work_request_from_state(
            root=root, state_dir=root / "empty-state",
            repository="StatPan/datapan-registry", source_id="data_go_kr",
            source_scope="aggregate_supported_catalog",
            evaluation={
                "source_sha": head,
                "source_tree_sha": self.git(root, "rev-parse", f"{head}^{{tree}}"),
                "event": "schedule", "run_id": "123456", "run_attempt": 1,
                "as_of": NOW,
            },
        )
        idle = ELIGIBILITY.build_source_recovery_idle_processing_result(
            request, root=root, processor_run_id="123456-1",
            processor_artifact_run_id="123456",
            reason="no_retained_admitted_observation",
        )
        self.assertEqual(idle["source_recovery_action"], "blocked")
        self.assertEqual(
            ELIGIBILITY.validate_source_recovery_idle_processing_result(
                request, idle, root=root, processor_run_id="123456-1",
                processor_artifact_run_id="123456",
            ),
            idle,
        )
        tampered = dict(idle)
        tampered["target_contract_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source_recovery_idle_result_mismatch"):
            ELIGIBILITY.validate_source_recovery_idle_processing_result(
                request, tampered, root=root, processor_run_id="123456-1",
                processor_artifact_run_id="123456",
            )

    def test_workflow_cli_binds_request_to_clean_prepared_state_revision(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        state = root / "state-repository"
        state.mkdir()
        self.git(state, "init", "-q")
        self.git(state, "config", "user.name", "test")
        self.git(state, "config", "user.email", "test@example.invalid")
        state_root = state / ".datapan/upstream-catalogue-state"
        state_root.mkdir(parents=True)
        marker = {
            "schema_version": "datapan.upstream-catalogue-state-root.v1",
            "repository": "StatPan/datapan-registry",
            "source_id": "data_go_kr",
            "state_root": ".datapan/upstream-catalogue-state",
        }
        (state_root / "state-root.json").write_text(
            json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self.git(state, "add", ".")
        self.git(state, "commit", "-qm", "state")
        state_sha = self.git(state, "rev-parse", "HEAD")
        output = root / "request.json"
        arguments = [
            "--root", str(root), "--state-dir", str(state_root),
            "--repository", "StatPan/datapan-registry", "--source-sha", head,
            "--event", "schedule", "--run-id", "123456", "--run-attempt", "1",
            "--as-of", NOW, "--selection-state-sha", state_sha,
            "--output", str(output),
        ]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ELIGIBILITY.main(arguments), 0)
        request = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(request["selection_state_sha"], state_sha)
        self.assertIsNone(request["source_subject"])

        wrong = [*arguments]
        wrong[wrong.index(state_sha)] = "f" * 40
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ELIGIBILITY.main(wrong), 2)
        (state_root / "state-root.json").write_text("{}\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ELIGIBILITY.main(arguments), 2)

    def test_verified_value_requires_exact_native_reread(self) -> None:
        temp, root, head = self.contract_repo()
        self.addCleanup(temp.cleanup)
        prior, inventory = ELIGIBILITY.build_target_contract(root, head)
        successor = self.commit_change(root, "scripts/compose-upstream-catalogue-candidate.py", "changed\n")
        current, _ = ELIGIBILITY.build_target_contract(root, successor, previous_inventory=inventory)
        candidate = self.candidate(current, prior_contract=prior)
        selection = self.select(current, [candidate])
        verified = ELIGIBILITY.resolve_verified_source_eligibility(
            selection, reread_candidates=lambda: [copy.deepcopy(candidate)],
        )
        self.assertEqual(verified.selection["action"], "rebuild")
        changed = copy.deepcopy(candidate)
        changed["artifact_expires_at"] = "2026-10-06T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "native_reread_mismatch"):
            ELIGIBILITY.resolve_verified_source_eligibility(selection, reread_candidates=lambda: [changed])
        with self.assertRaisesRegex(ValueError, "constructor_private"):
            ELIGIBILITY.VerifiedSourceEligibility(object(), selection)

    def test_schema_rejects_evidence_on_nonselected_action(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/datapan.upstream-catalogue-source-eligibility.v1.schema.json").read_text(encoding="utf-8")
        )
        validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
        document = {
            "schema_version": ELIGIBILITY.SCHEMA_VERSION,
            "repository": "StatPan/datapan-registry",
            "source_id": "data_go_kr",
            "source_scope": "aggregate_supported_catalog",
            "action": "blocked",
            "reason_code": "no_retained_admitted_observation",
            "evaluation": {"source_sha": SHA, "source_tree_sha": "c" * 40, "event": "schedule", "run_id": "1", "run_attempt": 1, "as_of": NOW},
            "selection_key": DIGEST,
            "evidence": {},
        }
        self.assertTrue(list(validator.iter_errors(document)))

    def test_declared_push_filter_excludes_generated_outputs(self) -> None:
        declared, ignored = ELIGIBILITY.relevant_push_paths([
            "scripts/process-upstream-catalogue-candidate.py",
            "manifest.json",
            "reports/completeness-proof-rollup.json",
            "README.md",
        ])
        self.assertEqual(declared, ["scripts/process-upstream-catalogue-candidate.py"])
        self.assertEqual(ignored, ["README.md", "manifest.json", "reports/completeness-proof-rollup.json"])

    def test_workflow_push_filter_and_schedule_order_match_the_reviewed_contract(self) -> None:
        workflow = yaml.load(
            (ROOT / ".github/workflows/upstream-catalogue-process.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        trigger_paths = workflow["on"]["push"]["paths"]
        self.assertEqual(len(trigger_paths), len(set(trigger_paths)))
        self.assertEqual(set(trigger_paths), set(ELIGIBILITY.CONTRACT_PATHS))
        process = workflow["jobs"]["process"]
        self.assertIn("github.event.deleted == false", process["if"])
        self.assertIn("github.sha == github.event.after", process["if"])
        steps = process["steps"]
        handoff_index = next(index for index, step in enumerate(steps) if step.get("id") == "handoff")
        source_index = next(index for index, step in enumerate(steps) if step.get("id") == "source_recovery")
        select_index = next(index for index, step in enumerate(steps) if step.get("id") == "select")
        self.assertLess(handoff_index, source_index)
        self.assertLess(source_index, select_index)
        selector = steps[source_index]
        self.assertEqual(selector["if"], "github.event_name == 'push' || github.event_name == 'schedule'")
        self.assertIn("--selection-state-sha", selector["run"])
        self.assertIn("--include-active", selector["run"])

    def test_source_epoch_inherits_original_a_accounting_and_caps_remaining_requests(self) -> None:
        now = dt.datetime(2026, 10, 11, tzinfo=dt.timezone.utc)
        original = {
            "source_id": "data_go_kr",
            "producer_run_id": "111111",
            "observed_at": "2026-10-01T03:04:05Z",
            "candidate_sha256": "1" * 64,
            "evidence_sha256": "2" * 64,
        }
        predecessor_id = "3" * 64
        target_id = "4" * 64
        epoch = ELIGIBILITY.build_source_epoch(
            original_observation_sha256=ELIGIBILITY.object_sha256(original),
            target_contract_sha256="5" * 64,
            predecessor_generation_id=predecessor_id,
            target_generation_id=target_id,
            ordinal=2,
        )
        prior = {
            "generation_id": predecessor_id,
            "observed_at": original["observed_at"],
            "last_observation": {
                "observed_at": original["observed_at"],
                "producer_run_id": original["producer_run_id"],
                "refresh_evidence_sha256": original["evidence_sha256"],
            },
            "observation_count": 7,
            "input_artifacts": [{
                "run_id": original["producer_run_id"],
                "name": "upstream-catalog-refresh-111111",
                "artifact_id": "222222",
                "expires_at": "2026-12-01T00:00:00Z",
                "candidate_sha256": original["candidate_sha256"],
                "evidence_sha256": original["evidence_sha256"],
            }],
            "attempts_consumed": 23,
            "attempts_by_id": {"0085": 2, "0109": 1},
            "detail_retry_reset_ids": ["0123"],
            "detail_queue_cursor": 41,
            "fencing_token": 19,
            "lease": {"expires_at": "2026-10-10T23:59:59Z"},
        }
        successor = {"generation_id": target_id, "lease": None}

        PROCESSOR.inherit_source_epoch_accounting(
            successor, prior, epoch, original, target_id, now,
        )

        for field in (
            "observed_at", "last_observation", "observation_count", "input_artifacts",
            "attempts_consumed", "attempts_by_id", "detail_retry_reset_ids",
            "detail_queue_cursor", "fencing_token",
        ):
            self.assertEqual(successor[field], prior[field], field)
        self.assertEqual(successor["input_artifacts"][0]["expires_at"], "2026-12-01T00:00:00Z")
        self.assertIsNone(successor["lease"])
        successor["generation_inputs"] = {"source_epoch": epoch}
        self.assertEqual(PROCESSOR.source_epoch_attempt_budget(
            successor, 24, queued_work=True,
        ), 1)
        successor["attempts_consumed"] = 24
        with self.assertRaisesRegex(ValueError, "source_epoch_accounting_exhausted"):
            PROCESSOR.source_epoch_attempt_budget(successor, 24, queued_work=True)
        self.assertEqual(PROCESSOR.source_epoch_attempt_budget(
            successor, 24, queued_work=True, reservation_prepaid=True,
        ), 0)
        self.assertEqual(PROCESSOR.source_epoch_attempt_budget(
            successor, 24, queued_work=False,
        ), 24)

    def test_source_epoch_rejects_live_predecessor_lease_before_mutation(self) -> None:
        now = dt.datetime(2026, 10, 11, tzinfo=dt.timezone.utc)
        original = {"a": "same observation"}
        predecessor_id = "3" * 64
        target_id = "4" * 64
        epoch = ELIGIBILITY.build_source_epoch(
            original_observation_sha256=ELIGIBILITY.object_sha256(original),
            target_contract_sha256="5" * 64,
            predecessor_generation_id=predecessor_id,
            target_generation_id=target_id,
            ordinal=2,
        )
        prior = {
            "generation_id": predecessor_id,
            "observed_at": NOW,
            "last_observation": {"observed_at": NOW},
            "observation_count": 1,
            "input_artifacts": [{"expires_at": "2026-12-01T00:00:00Z"}],
            "attempts_consumed": 0,
            "attempts_by_id": {},
            "detail_retry_reset_ids": [],
            "detail_queue_cursor": 0,
            "fencing_token": 1,
            "lease": {"expires_at": "2026-10-11T00:00:01Z"},
        }
        successor = {"generation_id": target_id, "lease": None}
        before = copy.deepcopy(successor)
        with self.assertRaisesRegex(ValueError, "source_epoch_predecessor_accounting_or_lease_invalid"):
            PROCESSOR.inherit_source_epoch_accounting(
                successor, prior, epoch, original, target_id, now,
            )
        self.assertEqual(successor, before)


if __name__ == "__main__":
    unittest.main()
