from __future__ import annotations

import copy
import base64
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


COLLECTOR = load("current_target_evidence_test", ROOT / "scripts/canonical_current_target_evidence.py")
PRODUCER = load("current_target_producer_test", ROOT / "scripts/run-canonical-update-promotion.py")
PROMOTION = load("current_target_promotion_test", ROOT / "scripts/canonical_update_pr.py")
DISPOSITION = load("current_target_disposition_test", ROOT / "scripts/canonical_source_target_disposition.py")
ELIGIBILITY = load("current_target_eligibility_test", ROOT / "scripts/upstream_catalogue_source_eligibility.py")


def git(*args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=ROOT, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=True,
    ).stdout.strip()


HEAD = git("rev-parse", "HEAD^{commit}")
TREE = git("rev-parse", "HEAD^{tree}")
REPOSITORY = "StatPan/datapan-registry"
EVALUATION_AS_OF = "2026-10-07T00:00:00Z"
PROCESSOR_COMMIT = "1" * 40
JOURNAL_COMMIT = "7" * 40


class FakeNativeReader:
    def __init__(self, *, journal: dict | None = None, pull: dict | None = None) -> None:
        self.root = ROOT
        self.repository = REPOSITORY
        self.main_sha = HEAD
        self.processor_commit = PROCESSOR_COMMIT
        self.journal_commit = JOURNAL_COMMIT if journal is not None else None
        self.journal_raw = (
            json.dumps(journal, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
            if journal is not None else None
        )
        self.pull = copy.deepcopy(pull)
        self.current_identity_available = True
        self.main_available = True
        self.processor_recheck_available = True
        self.historical_run: dict | None = None
        self.historical_tree: str | None = None
        self.calls: list[str] = []

    def read_default_branch_tip(self, *, deadline_monotonic: float) -> str:
        self.calls.append("read_default_branch_tip")
        if not self.main_available:
            raise COLLECTOR.CurrentTargetNativeUnavailable("denied")
        return self.main_sha

    def read_current_evaluation_identity(self, *, deadline_monotonic: float) -> dict:
        self.calls.append("read_current_evaluation_identity")
        if not self.current_identity_available:
            raise COLLECTOR.CurrentTargetNativeUnavailable("denied")
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
        (destination / "state-marker.json").write_text('{"authenticated":true}\n')
        return {
            "repository": REPOSITORY,
            "branch_ref": "refs/heads/automation/upstream-catalogue-state",
            "commit_sha": self.processor_commit,
        }

    def read_processor_state_tip(self, *, deadline_monotonic: float) -> str:
        self.calls.append("read_processor_state_tip")
        if not self.processor_recheck_available:
            raise COLLECTOR.CurrentTargetNativeUnavailable("denied")
        return self.processor_commit

    def read_state_tip(self, *, deadline_monotonic: float) -> str | None:
        self.calls.append("read_state_tip")
        return self.journal_commit

    def read_journal_blob(self, commit_sha: str, *, deadline_monotonic: float) -> bytes:
        self.calls.append("read_journal_blob")
        if commit_sha != self.journal_commit or self.journal_raw is None:
            raise COLLECTOR.CurrentTargetNativeUnavailable("missing journal")
        return self.journal_raw

    def read_pull_request(
        self, repository: str, number: int, *, deadline_monotonic: float,
    ) -> dict:
        self.calls.append("read_pull_request")
        if repository != REPOSITORY or self.pull is None:
            raise COLLECTOR.CurrentTargetNativeUnavailable("missing PR")
        return copy.deepcopy(self.pull)

    def read_c_evaluator_run(
        self, *, repository: str, run_id: str, run_attempt: int,
        deadline_monotonic: float,
    ) -> dict:
        self.calls.append("read_c_evaluator_run")
        if repository != REPOSITORY or self.historical_run is None:
            raise COLLECTOR.CurrentTargetNativeUnavailable("missing C run")
        return copy.deepcopy(self.historical_run)

    def read_git_tree_sha(self, commit_sha: str, *, deadline_monotonic: float) -> str:
        self.calls.append("read_git_tree_sha")
        if self.historical_tree is None:
            raise COLLECTOR.CurrentTargetNativeUnavailable("missing tree")
        return self.historical_tree

    def read_git_blob(
        self, commit_sha: str, path: str, *, max_bytes: int,
        deadline_monotonic: float,
    ) -> bytes:
        self.calls.append("read_git_blob")
        raw = (ROOT / path).read_bytes()
        if len(raw) > max_bytes:
            raise COLLECTOR.CurrentTargetNativeUnavailable("oversize blob")
        return raw


class CurrentTargetEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = ELIGIBILITY.validate_inventory(
            ELIGIBILITY.contract_inventory(
                ROOT, HEAD, read_blob=lambda _revision, path: (ROOT / path).read_bytes(),
            ),
        )
        cls.inventory_sha = ELIGIBILITY.object_sha256(cls.inventory)
        cls.contract_sha = ELIGIBILITY.target_contract_digest(cls.inventory_sha)

    def target(self, *, source_sha: str = HEAD, source_tree_sha: str = TREE):
        admission = {
            "producer_run_id": "12345678", "run_attempt": 1,
            "head_sha": "2" * 40, "artifact_id": "987654",
            "artifact_digest_sha256": "3" * 64,
            "refresh_evidence_sha256": "4" * 64,
        }
        contract = {
            "schema_version": "datapan.upstream-catalogue-target-contract.v1",
            "source_sha": source_sha, "source_tree_sha": source_tree_sha,
            "inventory_sha256": self.inventory_sha,
            "contract_sha256": self.contract_sha,
            "changed_classes": ["c_only_revalidation"],
        }
        generation_id = "5" * 64
        selection_key = PRODUCER.source_target_selection_key(admission, contract)
        return PRODUCER.ResolvedSourceTarget(
            PRODUCER._RESOLVED_SOURCE_TARGET,
            selection_key=selection_key, reason_code="c_only_contract_changed",
            evaluation={
                "source_sha": source_sha, "source_tree_sha": source_tree_sha,
                "event": "schedule", "run_id": "87654321", "run_attempt": 1,
                "as_of": EVALUATION_AS_OF,
            },
            original_admission=admission,
            original_observation={
                "source_id": "data_go_kr",
                "source_scope": "aggregate_supported_catalog",
                "observed_at": "2026-10-06T01:02:03Z",
                "original_baseline_sha256": "6" * 64,
            },
            original_generation={"generation_id": generation_id, "epoch": 1},
            target_contract=contract, target_inventory=self.inventory,
            generation_id=generation_id, state_refs=[], state_snapshot={},
        )

    @staticmethod
    def empty_journal() -> dict:
        return {
            "schema_version": "datapan.canonical-update-promotion-journal.v1",
            "repository": REPOSITORY,
            "updated_at": "2026-10-07T00:00:00Z",
            "records": [],
        }

    def receipt(self, target, *, status: str, number: int = 41) -> dict:
        binding = PROMOTION.source_target_binding_from_capability(target, PRODUCER)
        owner = PROMOTION.owner_id(
            REPOSITORY, "data_go_kr", "aggregate_supported_catalog",
        )
        body = PROMOTION.body_marker(owner, target.generation_id) + "\nC target evidence\n"
        return {
            "schema_version": PROMOTION.SCHEMA_VERSION,
            "status": status,
            "candidate": {
                "repository": REPOSITORY, "source_id": "data_go_kr",
                "scope": "aggregate_supported_catalog",
                "generation_id": target.generation_id, "head_sha": "b" * 40,
                "manifest_sha256": "c" * 64, "registry_sha256": "d" * 64,
                "registry_path": "data/data-go-kr.registry.json", "registry_bytes": 42,
                "source_target": binding,
            },
            "ownership": {
                "owner_id": owner, "branch": "codex/source-target-c",
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "body": body,
            },
            "pr": {"number": number, "merge_commit_sha": None},
            "acknowledgements": [],
        }

    @staticmethod
    def pull(row: dict, *, merged: bool) -> dict:
        value = {
            "number": row["pr"]["number"], "repository": REPOSITORY,
            "headRepository": REPOSITORY, "state": "MERGED" if merged else "OPEN",
            "body": row["ownership"]["body"],
            "headRefName": row["ownership"]["branch"],
            "headRefOid": row["candidate"]["head_sha"], "baseRefName": "main",
            "merged": merged, "mergeCommit": None,
        }
        if merged:
            value["mergeCommit"] = {"oid": "e" * 40}
        return value

    def no_op_record(self, old_target):
        source_revalidation = {
            "action": "revalidate_c", "reason_code": "c_only_contract_changed",
            "original_generation_id": old_target.generation_id, "original_epoch": 2,
            "target_contract_sha256": old_target.target_contract["contract_sha256"],
            "source_sha": old_target.target_contract["source_sha"],
            "source_tree_sha": old_target.target_contract["source_tree_sha"],
        }
        verified = types.SimpleNamespace(
            selection={
                "action": "revalidate_c", "selection_key": old_target.selection_key,
                "evidence": {"original_generation": copy.deepcopy(old_target.original_generation)},
            },
            original_generation=copy.deepcopy(old_target.original_generation),
            target_contract=copy.deepcopy(old_target.target_contract),
            source_epoch={"schema_version": "test-source-epoch"},
        )
        processor_candidate = {
            "generation_id": old_target.generation_id,
            "checkpoint": {"generation_id": old_target.generation_id},
            "source_revalidation": source_revalidation,
        }
        source_eligibility = PRODUCER.ResolvedSourceEligibility(
            PRODUCER._RESOLVED_SOURCE_ELIGIBILITY, verified, processor_candidate,
        )
        identity = {
            "repository": REPOSITORY,
            "workflow_path": ".github/workflows/canonical-update-promotion.yml",
            "mode": "recover-ready", "event": "schedule",
            "run_id": old_target.evaluation["run_id"],
            "run_attempt": old_target.evaluation["run_attempt"],
            "source_sha": old_target.evaluation["source_sha"],
            "source_tree_sha": old_target.evaluation["source_tree_sha"],
        }
        terminal = {
            "kind": "recover-ready", "status": "already-canonical-payload",
            "candidate_available": False,
            "evaluated_main": {
                "revision": old_target.evaluation["source_sha"],
                "manifest_sha256": "c" * 64, "registry_sha256": "d" * 64,
                "registry_path": "data/data-go-kr.registry.json", "registry_bytes": 42,
            },
            "selected_generation_id": None, "preparation_returned": False,
            "generation_count": 1, "generation_results_truncated": False,
            "generations": [{
                "generation_id": old_target.generation_id,
                "checkpoint_sha256": "a" * 64, "source_id": "data_go_kr",
                "source_scope": "aggregate_supported_catalog",
                "status": "already_canonical", "reason_code": "source_contract_revalidated",
                "producer": {
                    "run_id": "12345678", "run_attempt": 1,
                    "head_sha": "2" * 40, "artifact_id": "987654",
                    "artifact_name": "processor-output",
                    "artifact_expires_at": "2026-11-01T00:00:00Z",
                    "bundle_manifest_sha256": "3" * 64,
                },
                "checkpoint_observation": None, "checkpoint_observation_count": 1,
                "generation_baseline_sha256": "e" * 64,
                "checkpoint_derivation_sha256": None,
                "composition": {
                    "registry_path": "data/data-go-kr.registry.json", "registry_bytes": 42,
                    "registry_sha256": "d" * 64, "baseline_sha256": "e" * 64,
                    "composition_receipt_sha256": "f" * 64,
                },
                "pending_count": 0, "detail_retry_count": 0,
                "detail_unattempted_count": 0,
            }],
        }
        projection = {
            "source_revalidation": source_revalidation,
            "target_contract": copy.deepcopy(old_target.target_contract),
        }
        validated = PRODUCER.ValidatedSourceTargetNoopEvaluation(
            PRODUCER._VALIDATED_SOURCE_TARGET_NOOP_EVALUATION,
            source_target=old_target, source_eligibility=source_eligibility,
            evaluated_base_sha=old_target.evaluation["source_sha"],
            evaluator_identity=identity, terminal_outcome=terminal,
            projection=projection, target_inventory=self.inventory,
        )
        def member(value):
            encoded = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
            return {
                "document": json.loads(encoded), "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }

        binding = PROMOTION.source_target_binding_from_capability(old_target, PRODUCER)
        retained = {
            "terminal_outcome": member(validated.terminal_outcome),
            "projection": member(validated.projection),
            "target_inventory": member(PROMOTION._source_target_inventory_document(validated.target_inventory)),
        }
        refs = [
            {"path": path, "bytes": retained[name]["bytes"], "sha256": retained[name]["sha256"]}
            for name, path in zip(
                PROMOTION.SOURCE_TARGET_NOOP_V2_MEMBER_NAMES,
                PROMOTION.SOURCE_TARGET_NOOP_V2_MEMBER_PATHS,
                strict=True,
            )
        ]
        record = {
            "schema_version": PROMOTION.SOURCE_TARGET_NOOP_V2_SCHEMA,
            "record_type": "source-target-noop",
            "source_target": binding,
            "evaluated_base_sha": validated.evaluated_base_sha,
            "evaluator_source_sha": identity["source_sha"],
            "evaluator_run_id": str(identity["run_id"]),
            "evaluator_run_attempt": identity["run_attempt"],
            "terminal_evaluator_sha256": retained["terminal_outcome"]["sha256"],
            "terminal_projection_sha256": retained["projection"]["sha256"],
            "reason_code": "validated_target_has_no_semantic_delta",
            "evidence_refs": refs,
            "evidence_sha256": "0" * 64,
            "recorded_at": "2026-10-07T01:02:03Z",
            "evaluator_repository": identity["repository"],
            "evaluator_workflow_path": identity["workflow_path"],
            "evaluator_event": identity["event"],
            "retained_evaluation": retained,
        }
        unsigned = {
            key: value for key, value in record.items()
            if key not in {"evidence_sha256", "recorded_at"}
        }
        canonical = json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        record["evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
        return record

    @staticmethod
    def successful_noop_run(record: dict) -> dict:
        return {
            "repository": record["evaluator_repository"],
            "workflow_path": record["evaluator_workflow_path"],
            "event": record["evaluator_event"],
            "source_sha": record["evaluator_source_sha"],
            "run_id": record["evaluator_run_id"],
            "run_attempt": record["evaluator_run_attempt"],
            "status": "completed", "conclusion": "success",
            "jobs": [{
                "name": "reconcile", "status": "completed", "conclusion": "success",
                "steps": [
                    {"name": "Recover at most one durable ready processor bundle", "status": "completed", "conclusion": "success"},
                    {"name": "Upload C terminal outcome (recover-ready)", "status": "completed", "conclusion": "success"},
                ],
            }],
        }

    def collect(self, target, reader: FakeNativeReader, evidence_root: pathlib.Path):
        def modules(path: pathlib.Path, name: str):
            if path.name == "run-canonical-update-promotion.py":
                return PRODUCER
            if path.name == "canonical_source_target_disposition.py":
                return DISPOSITION
            raise AssertionError(f"unexpected module load: {path}")

        with (
            mock.patch.object(COLLECTOR, "_load_module", side_effect=modules),
            mock.patch.object(PRODUCER, "resolve_verified_source_target", return_value=target),
        ):
            return COLLECTOR.collect_current_target_evidence(
                repository=REPOSITORY, release_source_sha=HEAD,
                trusted_main_sha=HEAD, evaluation_as_of=EVALUATION_AS_OF,
                native_reader=reader, evidence_root=evidence_root,
                deadline_monotonic=time.monotonic() + 30,
            )

    def test_absent_journal_is_verified_not_applicable_without_pr_or_parent_reads(self) -> None:
        target = self.target()
        reader = FakeNativeReader()
        with tempfile.TemporaryDirectory() as td:
            result = self.collect(target, reader, pathlib.Path(td) / "evidence")
            projection = COLLECTOR.project_current_target_evidence(
                result, release_source_sha=HEAD, trusted_main_sha=HEAD,
                evaluation_as_of=EVALUATION_AS_OF,
            )
        self.assertIs(type(result), COLLECTOR.VerifiedCurrentTargetEvidence)
        self.assertEqual(
            projection["target_applicability"],
            {"status": "not_applicable", "reason_code": "no_recorded_source_target_disposition", "disposition": "unseen"},
        )
        self.assertNotIn("read_pull_request", reader.calls)
        self.assertNotIn("read_c_evaluator_run", reader.calls)

    def test_pending_and_merged_pr_rows_use_exact_native_readback(self) -> None:
        target = self.target()
        for status, merged, expected in (
            ("pending-review", False, ("pending", "exact_prepared_or_open_target_revision")),
            ("merged", True, ("completed", "exact_native_merged_target_revision")),
        ):
            with self.subTest(status=status):
                row = self.receipt(target, status=status)
                journal = self.empty_journal()
                journal["records"].append(row)
                reader = FakeNativeReader(journal=journal, pull=self.pull(row, merged=merged))
                with tempfile.TemporaryDirectory() as td:
                    result = self.collect(target, reader, pathlib.Path(td) / "evidence")
                    projection = COLLECTOR.project_current_target_evidence(
                        result, release_source_sha=HEAD, trusted_main_sha=HEAD,
                        evaluation_as_of=EVALUATION_AS_OF,
                    )
                    raw_copies = list((pathlib.Path(td) / "evidence" / "native").glob("*read_journal_blob.bin"))
                    self.assertEqual(len(raw_copies), 1)
                    self.assertEqual(raw_copies[0].read_bytes(), reader.journal_raw)
                self.assertEqual(
                    (projection["disposition"]["disposition"], projection["disposition"]["reason"]),
                    expected,
                )
                self.assertEqual(projection["target_applicability"]["status"], "current")
                self.assertEqual(reader.calls.count("read_pull_request"), 1)

    def test_historical_noop_remains_completed_after_current_source_changes(self) -> None:
        old_target = self.target(source_sha="6" * 40, source_tree_sha="7" * 40)
        current_target = self.target()
        self.assertEqual(old_target.selection_key, current_target.selection_key)
        record = self.no_op_record(old_target)
        journal = self.empty_journal()
        journal["source_target_dispositions"] = [record]
        reader = FakeNativeReader(journal=journal)
        reader.historical_run = self.successful_noop_run(record)
        reader.historical_tree = old_target.target_contract["source_tree_sha"]
        with tempfile.TemporaryDirectory() as td:
            result = self.collect(current_target, reader, pathlib.Path(td) / "evidence")
            projection = COLLECTOR.project_current_target_evidence(
                result, release_source_sha=HEAD, trusted_main_sha=HEAD,
                evaluation_as_of=EVALUATION_AS_OF,
            )
        self.assertEqual(
            (projection["disposition"]["disposition"], projection["disposition"]["reason"]),
            ("completed", "validated_target_noop_receipt"),
        )
        self.assertEqual(projection["source_target"]["selection_key"], old_target.selection_key)
        self.assertIn("read_c_evaluator_run", reader.calls)
        self.assertEqual(reader.calls.count("read_git_blob"), len(self.inventory))
        self.assertNotIn("read_pull_request", reader.calls)

    def test_native_permission_failure_is_honestly_unavailable(self) -> None:
        target = self.target()
        reader = FakeNativeReader()
        reader.main_available = False
        with tempfile.TemporaryDirectory() as td:
            result = self.collect(target, reader, pathlib.Path(td) / "evidence")
        self.assertIs(type(result), COLLECTOR.UnavailableCurrentTargetEvidence)
        self.assertEqual(result.reason_code, "trusted_main_native_read_unavailable")
        self.assertIsNotNone(result.evidence_index)

        reader = FakeNativeReader()
        reader.processor_recheck_available = False
        with tempfile.TemporaryDirectory() as td:
            result = self.collect(target, reader, pathlib.Path(td) / "evidence")
        self.assertEqual(result.reason_code, "processor_state_recheck_unavailable")
        self.assertNotIn("read_state_tip", reader.calls)

    def test_candidate_release_is_unavailable_before_state_or_target_reads(self) -> None:
        parent = git("rev-parse", "HEAD^")
        reader = FakeNativeReader()
        with tempfile.TemporaryDirectory() as td:
            result = COLLECTOR.collect_current_target_evidence(
                repository=REPOSITORY, release_source_sha=parent,
                trusted_main_sha=HEAD, evaluation_as_of=EVALUATION_AS_OF,
                native_reader=reader, evidence_root=pathlib.Path(td) / "evidence",
                deadline_monotonic=time.monotonic() + 30,
            )
        self.assertEqual(result.reason_code, "release_source_is_candidate_or_historical")
        self.assertNotIn("read_current_evaluation_identity", reader.calls)
        self.assertNotIn("materialize_processor_state_checkout", reader.calls)

    def test_projection_rejects_another_context_and_serialized_capability(self) -> None:
        target = self.target()
        with tempfile.TemporaryDirectory() as td:
            result = self.collect(target, FakeNativeReader(), pathlib.Path(td) / "evidence")
        with self.assertRaisesRegex(COLLECTOR.CurrentTargetEvidenceError, "another evaluation context"):
            COLLECTOR.project_current_target_evidence(
                result, release_source_sha=HEAD, trusted_main_sha=HEAD,
                evaluation_as_of="2026-10-07T00:00:01Z",
            )
        with self.assertRaisesRegex(COLLECTOR.CurrentTargetEvidenceError, "exact verified capability"):
            COLLECTOR.project_current_target_evidence(
                {"availability": "verified"}, release_source_sha=HEAD,
                trusted_main_sha=HEAD, evaluation_as_of=EVALUATION_AS_OF,
            )

    def test_native_git_blob_accepts_github_wrapping_and_rejects_junk(self) -> None:
        raw = b"wrapped github blob response\n"
        blob_sha = hashlib.sha1(
            b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw,
        ).hexdigest()
        wrapped = "\n".join(
            base64.b64encode(raw).decode("ascii")[offset:offset + 8]
            for offset in range(0, len(base64.b64encode(raw)), 8)
        )
        reader = object.__new__(COLLECTOR.GitHubCurrentTargetNativeReader)
        reader.root = ROOT
        reader.repository = REPOSITORY
        reader.workflow_path = ".github/workflows/verify-release.yml"
        reader.event = "schedule"
        reader.run_id = "87654321"
        reader.run_attempt = 1
        reader.source_sha = HEAD
        reader._origin_url = "https://github.com/StatPan/datapan-registry.git"
        metadata = {"type": "file", "path": "example.txt", "size": len(raw), "sha": blob_sha}
        with mock.patch.object(
            COLLECTOR.GitHubCurrentTargetNativeReader, "_api_json",
            side_effect=[metadata, {"encoding": "base64", "sha": blob_sha, "content": wrapped}],
        ):
            self.assertEqual(
                reader._git_blob(
                    HEAD, "example.txt", max_bytes=1024,
                    deadline_monotonic=time.monotonic() + 5,
                ),
                raw,
            )
        with mock.patch.object(
            COLLECTOR.GitHubCurrentTargetNativeReader, "_api_json",
            side_effect=[metadata, {"encoding": "base64", "sha": blob_sha, "content": wrapped + "$"}],
        ):
            with self.assertRaisesRegex(COLLECTOR.CurrentTargetEvidenceError, "base64 is invalid"):
                reader._git_blob(
                    HEAD, "example.txt", max_bytes=1024,
                    deadline_monotonic=time.monotonic() + 5,
                )

    def test_source_only_adapter_rejects_every_purpose_bearing_candidate(self) -> None:
        self.assertIsNone(PROMOTION.release_closure_identity({}))
        for field in ("purpose", "release_closure_reference", "release_closure_evidence"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    PROMOTION.AdmissionError, "unsupported purpose-bearing candidate",
                ):
                    PROMOTION.release_closure_identity({field: None})

    def test_evidence_root_rejects_a_supplied_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            target = root / "real"
            target.mkdir()
            link = root / "evidence"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(
                COLLECTOR.CurrentTargetEvidenceError, "cannot be a symlink",
            ):
                COLLECTOR._prepare_evidence_root(link)

    def test_nonfinite_and_expired_deadlines_stop_before_filesystem_setup(self) -> None:
        reader = FakeNativeReader()
        with tempfile.TemporaryDirectory() as td:
            evidence = pathlib.Path(td) / "evidence"
            with mock.patch.object(COLLECTOR, "_prepare_evidence_root") as prepare:
                expired = COLLECTOR.collect_current_target_evidence(
                    repository=REPOSITORY, release_source_sha=HEAD,
                    trusted_main_sha=HEAD, evaluation_as_of=EVALUATION_AS_OF,
                    native_reader=reader, evidence_root=evidence,
                    deadline_monotonic=time.monotonic() - 1,
                )
            self.assertIs(type(expired), COLLECTOR.UnavailableCurrentTargetEvidence)
            self.assertEqual(expired.reason_code, "collection_deadline_exceeded")
            self.assertIsNone(expired.evidence_index)
            prepare.assert_not_called()

            for deadline in (math.nan, math.inf, -math.inf):
                with self.subTest(deadline=deadline), mock.patch.object(
                    COLLECTOR, "_prepare_evidence_root",
                ) as prepare:
                    with self.assertRaisesRegex(
                        COLLECTOR.CurrentTargetEvidenceError, "deadline is not finite",
                    ):
                        COLLECTOR.collect_current_target_evidence(
                            repository=REPOSITORY, release_source_sha=HEAD,
                            trusted_main_sha=HEAD, evaluation_as_of=EVALUATION_AS_OF,
                            native_reader=reader, evidence_root=evidence,
                            deadline_monotonic=deadline,
                        )
                    prepare.assert_not_called()

        with mock.patch.object(COLLECTOR, "_git") as local_git:
            with self.assertRaisesRegex(
                COLLECTOR.CurrentTargetEvidenceError, "deadline is not finite",
            ):
                COLLECTOR.GitHubCurrentTargetNativeReader(
                    ROOT, REPOSITORY,
                    workflow_path=".github/workflows/verify-release.yml",
                    event="schedule", run_id="87654321", run_attempt=1,
                    source_sha=HEAD, deadline_monotonic=math.inf,
                )
            local_git.assert_not_called()
        with mock.patch.object(COLLECTOR, "_git") as local_git:
            with self.assertRaisesRegex(
                COLLECTOR.CurrentTargetEvidenceError, "whole-attempt bound",
            ):
                COLLECTOR.GitHubCurrentTargetNativeReader(
                    ROOT, REPOSITORY,
                    workflow_path=".github/workflows/verify-release.yml",
                    event="schedule", run_id="87654321", run_attempt=1,
                    source_sha=HEAD,
                    deadline_monotonic=time.monotonic() + COLLECTOR.MAX_COLLECTION_SECONDS + 1,
                )
            local_git.assert_not_called()

    def test_bounded_child_timeout_output_and_descendant_cleanup(self) -> None:
        reader = object.__new__(COLLECTOR.GitHubCurrentTargetNativeReader)
        reader.root = ROOT
        reader.repository = REPOSITORY
        with self.subTest(case="stall"):
            deadline = time.monotonic() + 0.2
            reader._deadline_monotonic = deadline
            started = time.monotonic()
            with self.assertRaises(COLLECTOR.CurrentTargetNativeUnavailable):
                reader._run(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    deadline_monotonic=deadline, max_stdout_bytes=1024,
                )
            self.assertLess(time.monotonic() - started, 2.0)

        with tempfile.TemporaryDirectory() as td:
            pid_path = pathlib.Path(td) / "descendant.pid"
            script = (
                "import pathlib,subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                "sys.stdout.buffer.write(b'x'*65536); sys.stdout.buffer.flush(); time.sleep(30)"
            )
            deadline = time.monotonic() + 5
            reader._deadline_monotonic = deadline
            with self.assertRaisesRegex(
                COLLECTOR.CurrentTargetEvidenceError, "stdout exceeded",
            ):
                reader._run(
                    [sys.executable, "-c", script, str(pid_path)],
                    deadline_monotonic=deadline, max_stdout_bytes=1024,
                )
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            for _ in range(40):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.025)
            else:
                self.fail("bounded native read left its descendant alive")

        with self.subTest(case="stderr-flood"):
            deadline = time.monotonic() + 5
            reader._deadline_monotonic = deadline
            with self.assertRaisesRegex(
                COLLECTOR.CurrentTargetEvidenceError, "stderr exceeded",
            ):
                reader._run(
                    [sys.executable, "-c", (
                        "import sys,time; "
                        f"sys.stderr.buffer.write(b'x'*{COLLECTOR.MAX_NATIVE_STDERR_BYTES + 1}); "
                        "sys.stderr.buffer.flush(); time.sleep(30)"
                    )],
                    deadline_monotonic=deadline, max_stdout_bytes=1024,
                )

    def test_post_spawn_setup_failure_and_cancellation_own_the_child(self) -> None:
        reader = object.__new__(COLLECTOR.GitHubCurrentTargetNativeReader)
        reader.root = ROOT
        reader.repository = REPOSITORY
        real_popen = subprocess.Popen

        for failure in (RuntimeError("selector setup failed"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                started: list[subprocess.Popen] = []

                def spawn(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    started.append(process)
                    return process

                deadline = time.monotonic() + 5
                reader._deadline_monotonic = deadline
                with (
                    mock.patch.object(COLLECTOR.subprocess, "Popen", side_effect=spawn),
                    mock.patch.object(COLLECTOR.selectors, "DefaultSelector", side_effect=failure),
                ):
                    with self.assertRaises(type(failure)):
                        reader._run(
                            [sys.executable, "-c", "import time; time.sleep(30)"],
                            deadline_monotonic=deadline, max_stdout_bytes=1024,
                        )
                self.assertEqual(len(started), 1)
                self.assertIsNotNone(started[0].poll())

    def test_cleanup_failures_do_not_skip_selector_or_stream_cleanup(self) -> None:
        events: list[str] = []

        class Stream:
            def __init__(self, descriptor: int, label: str) -> None:
                self._descriptor = descriptor
                self._label = label

            def fileno(self) -> int:
                return self._descriptor

            def close(self) -> None:
                events.append("close-" + self._label)

        class Process:
            pid = 123456
            stdout = Stream(101, "stdout")
            stderr = Stream(102, "stderr")

        class Selector:
            def __init__(self) -> None:
                self._calls = 0

            def register(self, _stream, _events) -> None:
                self._calls += 1
                if self._calls == 2:
                    raise RuntimeError("selector registration failed")

            def close(self) -> None:
                events.append("close-selector")

        process = Process()
        selector = Selector()
        with (
            mock.patch.object(COLLECTOR.subprocess, "Popen", return_value=process),
            mock.patch.object(COLLECTOR.selectors, "DefaultSelector", return_value=selector),
            mock.patch.object(COLLECTOR.os, "set_blocking", return_value=None),
            mock.patch.object(
                COLLECTOR, "_terminate_process_group",
                side_effect=COLLECTOR.CurrentTargetNativeUnavailable("injected cleanup failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "selector registration failed") as raised:
                COLLECTOR._bounded_process(
                    ["test-child"], cwd=ROOT,
                    deadline_monotonic=time.monotonic() + 5,
                    binary=False, max_stdout_bytes=1024,
                )
        self.assertEqual(events, ["close-selector", "close-stdout", "close-stderr"])
        self.assertTrue(any("cleanup failure" in note for note in raised.exception.__notes__))

    def test_closed_pipes_do_not_stop_owned_disk_monitoring(self) -> None:
        reader = object.__new__(COLLECTOR.GitHubCurrentTargetNativeReader)
        reader.root = ROOT
        reader.repository = REPOSITORY
        with tempfile.TemporaryDirectory() as td:
            owned = pathlib.Path(td) / "state"
            owned.mkdir()
            oversized = owned / "late-oversized.pack"
            pid_path = owned / "child.pid"
            script = (
                "import os,pathlib,sys,time; "
                "pathlib.Path(sys.argv[2]).write_text(str(os.getpid())); "
                "os.close(1); os.close(2); time.sleep(0.25); "
                "pathlib.Path(sys.argv[1]).open('wb').truncate(int(sys.argv[3])); "
                "time.sleep(30)"
            )
            deadline = time.monotonic() + 5
            reader._deadline_monotonic = deadline
            started = time.monotonic()
            with self.assertRaisesRegex(
                COLLECTOR.CurrentTargetEvidenceError, "aggregate bound",
            ):
                reader._run(
                    [sys.executable, "-c", script, str(oversized), str(pid_path),
                     str(COLLECTOR.MAX_EVIDENCE_BYTES + 1)],
                    deadline_monotonic=deadline, max_stdout_bytes=1024,
                    owned_root=owned,
                )
            self.assertLess(time.monotonic() - started, 2.0)
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_owned_state_acquisition_fails_during_disk_budget_overflow(self) -> None:
        reader = object.__new__(COLLECTOR.GitHubCurrentTargetNativeReader)
        reader.root = ROOT
        reader.repository = REPOSITORY
        with tempfile.TemporaryDirectory() as td:
            owned = pathlib.Path(td) / "state"
            owned.mkdir()
            oversized = owned / "oversized.pack"
            script = (
                "import pathlib,sys,time; "
                "pathlib.Path(sys.argv[1]).open('wb').truncate(int(sys.argv[2])); "
                "time.sleep(30)"
            )
            deadline = time.monotonic() + 5
            reader._deadline_monotonic = deadline
            with self.assertRaisesRegex(
                COLLECTOR.CurrentTargetEvidenceError, "aggregate bound",
            ):
                reader._run(
                    [sys.executable, "-c", script, str(oversized),
                     str(COLLECTOR.MAX_EVIDENCE_BYTES + 1)],
                    deadline_monotonic=deadline, max_stdout_bytes=1024,
                    owned_root=owned,
                )

    def test_evidence_index_preflights_sparse_and_special_files_without_reading(self) -> None:
        context = {
            "repository": REPOSITORY, "release_source_sha": HEAD,
            "trusted_main_sha": HEAD, "evaluation_as_of": EVALUATION_AS_OF,
        }
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            with (root / "oversized.bin").open("wb") as stream:
                stream.truncate(COLLECTOR.MAX_EVIDENCE_BYTES + 1)
            with mock.patch.object(COLLECTOR, "_stream_owned_digest") as digest:
                with self.assertRaisesRegex(
                    COLLECTOR.CurrentTargetEvidenceError, "aggregate bound",
                ):
                    COLLECTOR._evidence_index(
                        root, context=context, state={},
                        deadline_monotonic=time.monotonic() + 5,
                    )
                digest.assert_not_called()
        if hasattr(os, "mkfifo"):
            with tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                os.mkfifo(root / "unexpected.fifo")
                with self.assertRaisesRegex(
                    COLLECTOR.CurrentTargetEvidenceError, "special file",
                ):
                    COLLECTOR._evidence_index(
                        root, context=context, state={},
                        deadline_monotonic=time.monotonic() + 5,
                    )

    def test_late_source_target_resolution_cannot_mint_verified_evidence(self) -> None:
        target = self.target()
        reader = FakeNativeReader()
        clock = [0.0]

        def modules(path: pathlib.Path, _name: str):
            if path.name == "run-canonical-update-promotion.py":
                return PRODUCER
            raise AssertionError(f"unexpected module load: {path}")

        def resolved_target(*_args, **_kwargs):
            clock[0] = 11.0
            return target

        def local_git(_root: pathlib.Path, *args: str, **_kwargs):
            value = TREE if str(args[-1]).endswith("^{tree}") else HEAD
            return subprocess.CompletedProcess(("git", *args), 0, value + "\n", "")

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(COLLECTOR.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(COLLECTOR, "_git", side_effect=local_git),
            mock.patch.object(COLLECTOR, "_load_module", side_effect=modules),
            mock.patch.object(PRODUCER, "resolve_verified_source_target", side_effect=resolved_target),
        ):
            result = COLLECTOR.collect_current_target_evidence(
                repository=REPOSITORY, release_source_sha=HEAD,
                trusted_main_sha=HEAD, evaluation_as_of=EVALUATION_AS_OF,
                native_reader=reader, evidence_root=pathlib.Path(td) / "evidence",
                deadline_monotonic=10.0,
            )
        self.assertIs(type(result), COLLECTOR.UnavailableCurrentTargetEvidence)
        self.assertEqual(result.reason_code, "collection_deadline_exceeded")
        self.assertIsNone(result.evidence_index)


if __name__ == "__main__":
    unittest.main()
