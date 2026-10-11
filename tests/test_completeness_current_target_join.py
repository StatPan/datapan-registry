from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY = "StatPan/datapan-registry"
AS_OF = "2026-10-07T00:00:00Z"


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module


ROLLUP = load("current_target_join_rollup", ROOT / "scripts/generate-completeness-proof-rollup.py")
PRODUCER = ROLLUP.import_module(
    "current_target_join_producer", ROOT / "scripts/run-canonical-update-promotion.py",
)
COLLECTOR = ROLLUP.current_target_evidence_module(ROOT)
DISPOSITION = load(
    "current_target_join_disposition", ROOT / "scripts/canonical_source_target_disposition.py",
)
ELIGIBILITY = load(
    "current_target_join_eligibility", ROOT / "scripts/upstream_catalogue_source_eligibility.py",
)


def git(*args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=ROOT, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=True,
    ).stdout.strip()


HEAD = git("rev-parse", "HEAD^{commit}")
TREE = git("rev-parse", "HEAD^{tree}")


class ReadOnlyNativeFixture:
    """Offline native-reader fixture; it returns bounded synthetic identities."""

    def __init__(self) -> None:
        self.root = ROOT
        self.repository = REPOSITORY
        self.calls: list[str] = []

    def read_default_branch_tip(self, *, deadline_monotonic: float) -> str:
        self.calls.append("read_default_branch_tip")
        return HEAD

    def read_current_evaluation_identity(self, *, deadline_monotonic: float) -> dict:
        self.calls.append("read_current_evaluation_identity")
        return {
            "repository": REPOSITORY,
            "workflow_path": ".github/workflows/verify-release.yml",
            "event": "schedule",
            "source_sha": HEAD,
            "source_tree_sha": TREE,
            "run_id": "87654321",
            "run_attempt": 1,
        }

    def materialize_processor_state_checkout(
        self, destination: pathlib.Path, *, deadline_monotonic: float,
    ) -> dict:
        self.calls.append("materialize_processor_state_checkout")
        destination.mkdir(parents=True)
        return {
            "repository": REPOSITORY,
            "branch_ref": "refs/heads/automation/upstream-catalogue-state",
            "commit_sha": "1" * 40,
        }

    def read_processor_state_tip(self, *, deadline_monotonic: float) -> str:
        self.calls.append("read_processor_state_tip")
        return "1" * 40

    def read_state_tip(self, *, deadline_monotonic: float) -> None:
        self.calls.append("read_state_tip")
        return None


class CurrentTargetCollectorEvaluatorJoinTests(unittest.TestCase):
    def target(self):
        inventory = ELIGIBILITY.contract_inventory(
            ROOT, HEAD, read_blob=lambda _revision, path: (ROOT / path).read_bytes(),
        )
        inventory_sha = ELIGIBILITY.object_sha256(inventory)
        contract = {
            "schema_version": "datapan.upstream-catalogue-target-contract.v1",
            "source_sha": HEAD,
            "source_tree_sha": TREE,
            "inventory_sha256": inventory_sha,
            "contract_sha256": ELIGIBILITY.target_contract_digest(inventory_sha),
            "changed_classes": ["c_only_revalidation"],
        }
        admission = {
            "producer_run_id": "12345678", "run_attempt": 1,
            "head_sha": "2" * 40, "artifact_id": "987654",
            "artifact_digest_sha256": "3" * 64,
            "refresh_evidence_sha256": "4" * 64,
        }
        generation_id = "5" * 64
        return PRODUCER.ResolvedSourceTarget(
            PRODUCER._RESOLVED_SOURCE_TARGET,
            selection_key=PRODUCER.source_target_selection_key(admission, contract),
            reason_code="c_only_contract_changed",
            evaluation={
                "source_sha": HEAD, "source_tree_sha": TREE, "event": "schedule",
                "run_id": "87654321", "run_attempt": 1, "as_of": AS_OF,
            },
            original_admission=admission,
            original_observation={
                "source_id": "data_go_kr", "source_scope": "aggregate_supported_catalog",
                "observed_at": "2026-10-06T01:02:03Z",
                "original_baseline_sha256": "6" * 64,
            },
            original_generation={"generation_id": generation_id, "epoch": 1},
            target_contract=contract, target_inventory=inventory,
            generation_id=generation_id, state_refs=[], state_snapshot={},
        )

    def test_opaque_collector_result_projects_through_current_release_evaluator(self) -> None:
        target = self.target()
        reader = ReadOnlyNativeFixture()

        def load_trusted(path: pathlib.Path, _name: str):
            if path.name == "run-canonical-update-promotion.py":
                return PRODUCER
            if path.name == "canonical_source_target_disposition.py":
                return DISPOSITION
            raise AssertionError(f"unexpected collector dependency {path}")

        with (
            mock.patch.object(COLLECTOR, "_load_module", side_effect=load_trusted),
            mock.patch.object(PRODUCER, "resolve_verified_source_target", return_value=target),
            tempfile.TemporaryDirectory() as temp,
        ):
            evidence = COLLECTOR.collect_current_target_evidence(
                repository=REPOSITORY,
                release_source_sha=HEAD,
                trusted_main_sha=HEAD,
                evaluation_as_of=AS_OF,
                native_reader=reader,
                evidence_root=pathlib.Path(temp) / "evidence",
                deadline_monotonic=time.monotonic() + 30,
            )

        self.assertIs(type(evidence), COLLECTOR.VerifiedCurrentTargetEvidence)
        projected = ROLLUP.current_target_applicability_projection(
            root=ROOT,
            release_source_sha=HEAD,
            trusted_main_sha=HEAD,
            evaluation_as_of=AS_OF,
            target_tree_sha=TREE,
            target_kind="current_main",
            evidence=evidence,
        )
        self.assertEqual(projected["status"], "not_applicable")
        self.assertEqual(projected["disposition"], "unseen")
        self.assertEqual(projected["source_target"]["selection_key"], target.selection_key)
        self.assertNotIn("updated", projected)
        with self.assertRaisesRegex(ValueError, "opaque result"):
            ROLLUP.current_target_applicability_projection(
                root=ROOT,
                release_source_sha=HEAD,
                trusted_main_sha=HEAD,
                evaluation_as_of=AS_OF,
                target_tree_sha=TREE,
                target_kind="current_main",
                evidence={"availability": "verified"},
            )

    def test_real_runner_resolver_fails_closed_without_retained_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            subprocess.run(("git", "init", "-q", str(root)), check=True)
            subprocess.run(("git", "-C", str(root), "config", "user.name", "Offline test"), check=True)
            subprocess.run(("git", "-C", str(root), "config", "user.email", "offline@example.invalid"), check=True)
            subprocess.run((
                "git", "-C", str(root), "remote", "add", "origin",
                "https://github.com/StatPan/datapan-registry.git",
            ), check=True)
            for relative in ELIGIBILITY.CONTRACT_PATHS:
                source = ROOT / relative
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            subprocess.run(("git", "-C", str(root), "add", "--", *ELIGIBILITY.CONTRACT_PATHS), check=True)
            subprocess.run(("git", "-C", str(root), "commit", "-qm", "synthetic current-source contract"), check=True)
            source_sha = subprocess.run(
                ("git", "-C", str(root), "rev-parse", "HEAD^{commit}"), check=True,
                text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            source_tree = subprocess.run(
                ("git", "-C", str(root), "rev-parse", "HEAD^{tree}"), check=True,
                text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            state_root = root / ".datapan/upstream-catalogue-state"
            evaluation = {
                "source_sha": source_sha, "source_tree_sha": source_tree,
                "event": "schedule", "run_id": "87654321", "run_attempt": 1,
                "as_of": AS_OF,
            }

            resolved = PRODUCER.resolve_verified_source_target(
                root, state_root, REPOSITORY, source_sha, evaluation=evaluation,
            )
            self.assertIs(type(resolved), dict)
            self.assertEqual(resolved["action"], "blocked")
            self.assertEqual(resolved["reason_code"], "no_retained_admitted_observation")

            class SyntheticReader(ReadOnlyNativeFixture):
                def __init__(self) -> None:
                    super().__init__()
                    self.root = root

                def read_default_branch_tip(self, *, deadline_monotonic: float) -> str:
                    return source_sha

                def read_current_evaluation_identity(self, *, deadline_monotonic: float) -> dict:
                    return {
                        "repository": REPOSITORY,
                        "workflow_path": ".github/workflows/verify-release.yml",
                        "event": "schedule", "source_sha": source_sha,
                        "source_tree_sha": source_tree, "run_id": "87654321",
                        "run_attempt": 1,
                    }

            native_reader = SyntheticReader()
            with tempfile.TemporaryDirectory() as evidence_temp:
                unavailable = COLLECTOR.collect_current_target_evidence(
                    repository=REPOSITORY,
                    release_source_sha=source_sha,
                    trusted_main_sha=source_sha,
                    evaluation_as_of=AS_OF,
                    native_reader=native_reader,
                    evidence_root=pathlib.Path(evidence_temp) / "evidence",
                    deadline_monotonic=time.monotonic() + 30,
                )
            self.assertIs(type(unavailable), COLLECTOR.UnavailableCurrentTargetEvidence)
            self.assertEqual(
                unavailable.reason_code,
                "source_target_blocked_no_retained_admitted_observation",
            )
            projected = ROLLUP.current_target_applicability_projection(
                root=ROOT,
                release_source_sha=source_sha,
                trusted_main_sha=source_sha,
                evaluation_as_of=AS_OF,
                target_tree_sha=source_tree,
                target_kind="current_main",
                evidence=unavailable,
            )
            self.assertEqual(projected["status"], "unavailable")
            self.assertEqual(projected["reason_code"], unavailable.reason_code)
            self.assertNotIn("updated", projected)

    def test_runner_checkpoint_verifier_binds_source_epoch_with_control_helpers(self) -> None:
        schema_path = ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
        schema_document = json.loads(schema_path.read_text(encoding="utf-8"))
        checkpoint = json.loads((
            ROOT / "tests/fixtures/same-observation-derivation/native-b-9938.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(
            PRODUCER.verify_processor_checkpoint(checkpoint, schema_path)["generation_id"],
            checkpoint["generation_id"],
        )

        target_contract, _inventory = ELIGIBILITY.build_target_contract(ROOT, HEAD)
        generation_inputs = dict(checkpoint["generation_inputs"])
        generation_inputs["source_epoch"] = ELIGIBILITY.build_source_epoch(
            original_observation_sha256="a" * 64,
            target_contract_sha256=target_contract["contract_sha256"],
            predecessor_generation_id="b" * 64,
            target_generation_id=None,
            ordinal=2,
        )
        generation_inputs["source_target_contract"] = target_contract
        generation_id, generation_inputs = ELIGIBILITY.bind_source_epoch_generation(generation_inputs)
        checkpoint["generation_inputs"] = generation_inputs
        checkpoint["generation_id"] = generation_id

        def reseal(value: dict) -> dict:
            unsigned = dict(value)
            unsigned.pop("checkpoint_sha256", None)
            value["checkpoint_sha256"] = ELIGIBILITY.object_sha256(unsigned)
            return value

        reseal(checkpoint)
        # The exact schema and eligibility module are supplied by the replay's
        # authenticated CONTROL closure. An unusable local path proves that this
        # route does not fall back to reading a host schema.
        self.assertEqual(
            PRODUCER.verify_processor_checkpoint(
                checkpoint,
                ROOT / "missing-local-checkpoint-schema.json",
                eligibility_module=ELIGIBILITY,
                schema_document=schema_document,
            )["generation_id"],
            generation_id,
        )
        self.assertEqual(
            PRODUCER.verify_processor_checkpoint(checkpoint, schema_path)["generation_id"],
            generation_id,
        )

        def alter_target_contract(value: dict) -> None:
            contract = value["generation_inputs"]["source_target_contract"]
            contract["inventory_sha256"] = "e" * 64
            contract["contract_sha256"] = ELIGIBILITY.target_contract_digest("e" * 64)

        mutations = (
            ("null target generation", lambda value: value["generation_inputs"]["source_epoch"].__setitem__("target_generation_id", None)),
            ("wrong target generation", lambda value: value["generation_inputs"]["source_epoch"].__setitem__("target_generation_id", "c" * 64)),
            ("changed predecessor", lambda value: value["generation_inputs"]["source_epoch"].__setitem__("predecessor_generation_id", "d" * 64)),
            ("changed ordinal", lambda value: value["generation_inputs"]["source_epoch"].__setitem__("ordinal", 3)),
            ("changed target contract", alter_target_contract),
            ("changed generation input", lambda value: value["generation_inputs"].__setitem__("baseline_sha256", "f" * 64)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                altered = copy.deepcopy(checkpoint)
                mutate(altered)
                reseal(altered)
                with self.assertRaises((PRODUCER.PromotionError, jsonschema.ValidationError)):
                    PRODUCER.verify_processor_checkpoint(
                        altered,
                        schema_path,
                        eligibility_module=ELIGIBILITY,
                        schema_document=schema_document,
                    )

    def test_runner_resolver_guard_rejects_stubbed_advanced_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="current-target-selection-race-") as raw:
            root = pathlib.Path(raw) / "source"
            root.mkdir()
            subprocess.run(("git", "init", "-q", str(root)), check=True)
            subprocess.run(("git", "-C", str(root), "config", "user.name", "Offline test"), check=True)
            subprocess.run(("git", "-C", str(root), "config", "user.email", "offline@example.invalid"), check=True)
            subprocess.run((
                "git", "-C", str(root), "remote", "add", "origin",
                "https://github.com/StatPan/datapan-registry.git",
            ), check=True)
            for relative in ELIGIBILITY.CONTRACT_PATHS:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)
            subprocess.run(("git", "-C", str(root), "add", "--", *ELIGIBILITY.CONTRACT_PATHS), check=True)
            subprocess.run(("git", "-C", str(root), "commit", "-qm", "synthetic current-source contract"), check=True)
            source_sha = subprocess.run(
                ("git", "-C", str(root), "rev-parse", "HEAD^{commit}"), check=True,
                text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            source_tree = subprocess.run(
                ("git", "-C", str(root), "rev-parse", "HEAD^{tree}"), check=True,
                text=True, stdout=subprocess.PIPE,
            ).stdout.strip()

            state_checkout = root / "state-checkout"
            state_checkout.mkdir()
            subprocess.run(("git", "-C", str(state_checkout), "init", "-q"), check=True)
            subprocess.run(("git", "-C", str(state_checkout), "config", "user.name", "Offline test"), check=True)
            subprocess.run(("git", "-C", str(state_checkout), "config", "user.email", "offline@example.invalid"), check=True)
            subprocess.run((
                "git", "-C", str(state_checkout), "remote", "add", "origin",
                "https://github.com/StatPan/datapan-registry.git",
            ), check=True)
            state_root = state_checkout / ".datapan/upstream-catalogue-state"
            state_root.mkdir(parents=True)
            marker = {
                "schema_version": "datapan.upstream-catalogue-state-root.v1",
                "repository": REPOSITORY,
                "source_id": "data_go_kr",
                "state_root": ".datapan/upstream-catalogue-state",
            }
            (state_root / "state-root.json").write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            subprocess.run(("git", "-C", str(state_checkout), "add", "."), check=True)
            subprocess.run(("git", "-C", str(state_checkout), "commit", "-qm", "initial state"), check=True)
            selected_state_sha = subprocess.run(
                ("git", "-C", str(state_checkout), "rev-parse", "HEAD"), check=True,
                text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            selected_generation = "5" * 64
            selection_state_shas: list[str | None] = []

            class EligibilityStub:
                @staticmethod
                def validate_evaluation(value):
                    return dict(value)

                @staticmethod
                def source_work_request_from_state(**kwargs):
                    selection_state_shas.append(kwargs.get("selection_state_sha"))
                    index_path = state_root / "sources/data_go_kr/index.json"
                    index_path.parent.mkdir(parents=True)
                    index_path.write_text('{"generations":[]}\n', encoding="utf-8")
                    subprocess.run(("git", "-C", str(state_checkout), "add", "."), check=True)
                    subprocess.run(("git", "-C", str(state_checkout), "commit", "-qm", "new state after selection"), check=True)
                    return {
                        "action": "revalidate_c",
                        "selected_generation_id": selected_generation,
                        "selection_state_sha": kwargs.get("selection_state_sha"),
                    }

            def first_snapshot(*_args, **_kwargs):
                advanced_sha = subprocess.run(
                    ("git", "-C", str(state_checkout), "rev-parse", "HEAD"), check=True,
                    text=True, stdout=subprocess.PIPE,
                ).stdout.strip()
                return {"state_commit": advanced_sha}

            evaluation = {
                "source_sha": source_sha,
                "source_tree_sha": source_tree,
                "event": "schedule",
                "run_id": "87654321",
                "run_attempt": 1,
                "as_of": AS_OF,
            }
            with (
                mock.patch.object(PRODUCER, "load_module", return_value=EligibilityStub),
                mock.patch.object(PRODUCER, "_source_eligibility_state_snapshot", side_effect=first_snapshot),
                self.assertRaisesRegex(
                    PRODUCER.PromotionError,
                    "source target selection state changed before immutable target snapshot",
                ),
            ):
                PRODUCER.resolve_verified_source_target(
                    root, state_root, REPOSITORY, source_sha, evaluation=evaluation,
                )

            self.assertEqual(selection_state_shas, [selected_state_sha])
            advanced_state_sha = subprocess.run(
                ("git", "-C", str(state_checkout), "rev-parse", "HEAD"), check=True,
                text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertNotEqual(advanced_state_sha, selected_state_sha)

    def test_real_selector_and_snapshot_reject_state_advance_to_new_eligible_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="current-target-real-selection-race-") as raw:
            temp_root = pathlib.Path(raw)

            def git_at(repository: pathlib.Path, *args: str) -> str:
                result = subprocess.run(
                    ("git", *args), cwd=repository, check=True, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                return result.stdout.strip()

            source_root = temp_root / "source"
            source_root.mkdir()
            git_at(temp_root, "init", "-q", str(source_root))
            for key, value in (("user.name", "Offline race test"), ("user.email", "race@example.invalid")):
                git_at(source_root, "config", key, value)
            git_at(source_root, "remote", "add", "origin", "https://github.com/StatPan/datapan-registry.git")
            for relative in ELIGIBILITY.CONTRACT_PATHS:
                destination = source_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)
            cache_helper = source_root / "scripts/upstream_catalogue_cache_source.py"
            shutil.copyfile(ROOT / "scripts/upstream_catalogue_cache_source.py", cache_helper)
            git_at(source_root, "add", "--", *ELIGIBILITY.CONTRACT_PATHS)
            git_at(source_root, "add", "scripts/upstream_catalogue_cache_source.py")
            git_at(source_root, "commit", "-qm", "source contract before C-only change")
            previous_source_sha = git_at(source_root, "rev-parse", "HEAD^{commit}")
            prior_contract, _prior_inventory = ELIGIBILITY.build_target_contract(
                source_root, previous_source_sha,
            )
            ci_path = source_root / "scripts/canonical_update_ci.py"
            ci_path.write_text(ci_path.read_text(encoding="utf-8") + "\n# C-only selection race fixture\n", encoding="utf-8")
            git_at(source_root, "add", "scripts/canonical_update_ci.py")
            git_at(source_root, "commit", "-qm", "C-only contract change")
            current_source_sha = git_at(source_root, "rev-parse", "HEAD^{commit}")
            current_source_tree = git_at(source_root, "rev-parse", "HEAD^{tree}")
            current_contract, _current_inventory = ELIGIBILITY.build_target_contract(
                source_root, current_source_sha, previous_inventory=_prior_inventory,
            )
            self.assertEqual(current_contract["changed_classes"], [ELIGIBILITY.C_ONLY_REVALIDATION])

            handoff = load(
                "current_target_race_handoff",
                ROOT / "scripts/upstream_catalogue_handoff.py",
            )
            derivation = load(
                "current_target_race_derivation",
                ROOT / "scripts/upstream_catalogue_derivation.py",
            )
            base_checkpoint = json.loads((
                ROOT / "tests/fixtures/same-observation-derivation/native-b-9938.json"
            ).read_text(encoding="utf-8"))
            artifact_ref = base_checkpoint["input_artifacts"][0]
            observation = base_checkpoint["last_observation"]
            producer_run_id = str(artifact_ref["run_id"])
            refresh_sha = observation["refresh_evidence_sha256"]
            admission = {
                "admission_id": handoff.admission_id(producer_run_id, 1, refresh_sha),
                "producer_run_id": producer_run_id,
                "run_attempt": 1,
                "head_sha": previous_source_sha,
                "run_started_at": "2026-09-29T23:00:00Z",
                "artifact_id": str(artifact_ref["artifact_id"]),
                "artifact_name": artifact_ref["name"],
                "artifact_expires_at": artifact_ref["expires_at"],
                "artifact_digest_sha256": "6" * 64,
                "artifact_size_bytes": 1024,
                "refresh_evidence_sha256": refresh_sha,
                "observed_at": observation["observed_at"],
                "generation_id": base_checkpoint["generation_id"],
                "candidate_sha256": artifact_ref["candidate_sha256"],
                "admitted_at": "2026-09-30T00:00:00Z",
            }
            original_observation = derivation.original_observation_from_checkpoint(
                base_checkpoint, admission,
            )

            def source_epoch_checkpoint(predecessor_id: str, ordinal: int) -> dict:
                checkpoint = copy.deepcopy(base_checkpoint)
                checkpoint["attempts_consumed"] = 0
                generation_inputs = dict(checkpoint["generation_inputs"])
                generation_inputs["source_target_contract"] = prior_contract
                generation_inputs["source_epoch"] = ELIGIBILITY.build_source_epoch(
                    original_observation_sha256=ELIGIBILITY.object_sha256(original_observation),
                    target_contract_sha256=prior_contract["contract_sha256"],
                    predecessor_generation_id=predecessor_id,
                    target_generation_id=None,
                    ordinal=ordinal,
                )
                generation_id, bound_inputs = ELIGIBILITY.bind_source_epoch_generation(generation_inputs)
                checkpoint["generation_id"] = generation_id
                checkpoint["generation_inputs"] = bound_inputs
                unsigned = dict(checkpoint)
                unsigned.pop("checkpoint_sha256", None)
                checkpoint["checkpoint_sha256"] = ELIGIBILITY.object_sha256(unsigned)
                return checkpoint

            first_checkpoint = source_epoch_checkpoint("9" * 64, 2)
            admission["generation_id"] = first_checkpoint["generation_id"]
            index = {
                "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
                "generations": [{
                    "generation_id": first_checkpoint["generation_id"],
                    "checkpoint": f"{first_checkpoint['generation_id']}.json",
                }],
                "collector_handoff": handoff.empty_ledger(),
            }
            handoff.add_admission(index, admission, now="2026-09-30T00:00:01Z")

            state_checkout = temp_root / "state"
            state_checkout.mkdir()
            git_at(temp_root, "init", "-q", str(state_checkout))
            for key, value in (("user.name", "Offline race test"), ("user.email", "race@example.invalid")):
                git_at(state_checkout, "config", key, value)
            git_at(state_checkout, "remote", "add", "origin", "https://github.com/StatPan/datapan-registry.git")
            state_root = state_checkout / ".datapan/upstream-catalogue-state"
            generations_root = state_root / "sources/data_go_kr/generations"
            generations_root.mkdir(parents=True)
            (state_root / "state-root.json").write_text(
                json.dumps({
                    "schema_version": "datapan.upstream-catalogue-state-root.v1",
                    "repository": REPOSITORY,
                    "source_id": "data_go_kr",
                    "state_root": ".datapan/upstream-catalogue-state",
                }, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            index_path = state_root / "sources/data_go_kr/index.json"
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_text(
                json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            first_checkpoint_path = generations_root / f"{first_checkpoint['generation_id']}.json"
            first_checkpoint_path.write_text(
                json.dumps(first_checkpoint, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            git_at(state_checkout, "add", ".")
            git_at(state_checkout, "commit", "-qm", "one valid eligible source epoch")
            selected_state_sha = git_at(state_checkout, "rev-parse", "HEAD")
            first_checkpoint_bytes = first_checkpoint_path.read_bytes()

            eligibility_module = load(
                "current_target_race_source_eligibility",
                source_root / "scripts/upstream_catalogue_source_eligibility.py",
            )
            real_selector = eligibility_module.source_work_request_from_state
            selection_facts: dict[str, Any] = {}

            def select_then_advance(**kwargs):
                old_request = real_selector(**kwargs)
                selection_facts["old_request"] = old_request
                self.assertEqual(old_request["action"], "revalidate_c")
                self.assertEqual(old_request["selected_generation_id"], first_checkpoint["generation_id"])

                preferred_checkpoint = source_epoch_checkpoint(first_checkpoint["generation_id"], 3)
                current_index = json.loads(index_path.read_text(encoding="utf-8"))
                current_index["generations"].append({
                    "generation_id": preferred_checkpoint["generation_id"],
                    "checkpoint": f"{preferred_checkpoint['generation_id']}.json",
                })
                preferred_path = generations_root / f"{preferred_checkpoint['generation_id']}.json"
                preferred_path.write_text(
                    json.dumps(preferred_checkpoint, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                index_path.write_text(
                    json.dumps(current_index, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                git_at(state_checkout, "add", ".datapan/upstream-catalogue-state")
                git_at(state_checkout, "commit", "-qm", "advance to a newly eligible source epoch")
                selection_facts["advanced_state_sha"] = git_at(state_checkout, "rev-parse", "HEAD")
                selection_facts["preferred_generation_id"] = preferred_checkpoint["generation_id"]
                selection_facts["preferred_request"] = real_selector(**kwargs)
                return old_request

            eligibility_module.source_work_request_from_state = select_then_advance
            real_module_loader = PRODUCER.load_module

            def module_loader(path: pathlib.Path, name: str):
                if path.name == "upstream_catalogue_source_eligibility.py":
                    return eligibility_module
                return real_module_loader(path, name)

            real_snapshot = PRODUCER._source_eligibility_state_snapshot
            snapshots: list[dict[str, Any]] = []

            def tracked_snapshot(*args, **kwargs):
                snapshot = real_snapshot(*args, **kwargs)
                snapshots.append(snapshot)
                return snapshot

            evaluation = {
                "source_sha": current_source_sha,
                "source_tree_sha": current_source_tree,
                "event": "schedule",
                "run_id": "87654321",
                "run_attempt": 1,
                "as_of": AS_OF,
            }
            with (
                mock.patch.object(PRODUCER, "load_module", side_effect=module_loader),
                mock.patch.object(PRODUCER, "_source_eligibility_state_snapshot", side_effect=tracked_snapshot),
                mock.patch.object(sys, "path", [str(source_root / "scripts"), *sys.path]),
                self.assertRaisesRegex(
                    PRODUCER.PromotionError,
                    "source target selection state changed before immutable target snapshot",
                ),
            ):
                PRODUCER.resolve_verified_source_target(
                    source_root, state_root, REPOSITORY, current_source_sha,
                    evaluation=evaluation,
                )

            self.assertEqual(selection_facts["old_request"]["selection_state_sha"], selected_state_sha)
            preferred_request = selection_facts["preferred_request"]
            self.assertEqual(preferred_request["action"], "revalidate_c")
            self.assertEqual(
                preferred_request["selected_generation_id"],
                selection_facts["preferred_generation_id"],
            )
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["state_commit"], selection_facts["advanced_state_sha"])
            self.assertNotEqual(snapshots[0]["state_commit"], selected_state_sha)
            self.assertEqual(first_checkpoint_path.read_bytes(), first_checkpoint_bytes)
            self.assertEqual(git_at(state_checkout, "status", "--porcelain=v1", "--untracked-files=all"), "")


if __name__ == "__main__":
    unittest.main()
