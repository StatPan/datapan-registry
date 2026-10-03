from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check-upstream-catalogue-health.py"
SPEC = importlib.util.spec_from_file_location("check_upstream_catalogue_health", SCRIPT)
assert SPEC and SPEC.loader
HEALTH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HEALTH)
PERSIST_SPEC = importlib.util.spec_from_file_location("persist_upstream_catalogue_health", ROOT / "scripts" / "persist-upstream-catalogue-health.py")
assert PERSIST_SPEC and PERSIST_SPEC.loader
PERSIST = importlib.util.module_from_spec(PERSIST_SPEC)
PERSIST_SPEC.loader.exec_module(PERSIST)

AS_OF = dt.datetime.fromisoformat("2026-10-01T00:00:00+00:00")
POLICY = json.loads((ROOT / "policy/upstream-catalogue-health.json").read_text())
SOURCE_POLICY = json.loads((ROOT / "policy/source-refresh.json").read_text())
SOURCE_POLICY_SHA = HEALTH.file_sha256(ROOT / "policy/source-refresh.json")
HEALTH_POLICY_SHA = HEALTH.file_sha256(ROOT / "policy/upstream-catalogue-health.json")
RUN_ID = "100"
COLLECTOR_PATH = ".github/workflows/upstream-catalog-refresh.yml"
PROMOTION_PATH = ".github/workflows/canonical-update-promotion.yml"
PUBLICATION_ACK_PATH = ".github/workflows/canonical-update-publication-ack.yml"
WORKFLOW_IDS_BY_PATH = {COLLECTOR_PATH: 1101, PROMOTION_PATH: 1102, PUBLICATION_ACK_PATH: 1103}
EVIDENCE_SHA = "c" * 64
GENERATION_ID = "a" * 64
REAL_PROCESSOR_READY_BUNDLE = [
    {
        "path": "composed-candidate.registry.json",
        "sha256": "5c29a7b7d78b7000bc896dc04825dec5ef3bb120edf7c3525d625c10132b32c1",
        "bytes": 907,
    },
    {
        "path": "ready-scope.registry.json",
        "sha256": "5c29a7b7d78b7000bc896dc04825dec5ef3bb120edf7c3525d625c10132b32c1",
        "bytes": 907,
    },
    {
        "path": "semantic-diff.json",
        "sha256": "29d7179e74d2f190321973a900f6b602c85781a3dc8598e2c4d06d16d2d58521",
        "bytes": 53,
    },
    {
        "path": "regeneration-queue.json",
        "sha256": "eeb85c2675888473ec64b7580aa0c76c6fd6b2bd51828870286ef202ad89dae2",
        "bytes": 13,
    },
    {
        "path": "quarantine.json",
        "sha256": "eeb85c2675888473ec64b7580aa0c76c6fd6b2bd51828870286ef202ad89dae2",
        "bytes": 13,
    },
    {
        "path": "composition-receipt.json",
        "sha256": "f716e999fae4d38d5ec50469dbe1cecb8b0a89dbda30565db692e3e31828d687",
        "bytes": 896,
    },
    {
        "path": "upstream-catalogue-enrichment-evidence.json",
        "sha256": "936a0b772094548b3904358ab3d153981806e90c2a2e2a0cf6a69abbd5c1303a",
        "bytes": 1826,
    },
    {
        "path": "upstream-catalogue-processing-result.json",
        "sha256": "0ef237d1e4596496e45c5860487bde9b039f6c988f279b4888dfb721b23f8369",
        "bytes": 520,
    },
]
REAL_PROCESSOR_READY_BUNDLE_SHA256 = "16f5b87a7027821c66e540bb5243e0248695a009c339e6167b05c9368886821e"


def checkpoint(
    *, generation_id: str = GENERATION_ID, status: str = "no-change",
    observed_at: str = "2026-09-30T20:20:00Z", execution_mode: str = "live",
    collection_status: str = "success", last_progress_at: str = "2026-09-30T20:22:00Z",
    last_heartbeat_at: str = "2026-09-30T20:22:00Z", lease: dict | None = None,
    input_expiry: str = "2026-10-30T00:00:00Z", output_expiry: str = "2026-10-30T00:00:00Z",
    attempts_consumed: int = 0, outcome_reason: str = "genuine_no_change",
    composer_status: str = "no_change", pending_count: int = 0, producer_run_id: str = RUN_ID,
    evidence_sha: str | None = EVIDENCE_SHA,
) -> dict:
    value = {
        "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
        "source_id": "data_go_kr",
        "source_scope": "aggregate_supported_catalog",
        "generation_id": generation_id,
        "generation_inputs": {
            "source_id": "data_go_kr", "source_scope": "aggregate_supported_catalog",
            "baseline_sha256": "b" * 64, "candidate_sha256": "d" * 64,
            "observation_failure_sha256": None, "policy_sha256": SOURCE_POLICY_SHA,
            "adapter_revision": "e" * 64, "generator_revision": "f" * 64,
            "extractor_revision": "1" * 64,
        },
        "observed_at": "2026-09-30T20:20:00Z",
        "last_observation": {
            "observed_at": observed_at,
            "producer_run_id": producer_run_id,
            "refresh_evidence_sha256": evidence_sha,
            "collection_status": collection_status,
            "execution_mode": execution_mode,
        },
        "observation_count": 1,
        "last_heartbeat_at": last_heartbeat_at,
        "last_progress_at": last_progress_at,
        "status": status,
        "attempts_consumed": attempts_consumed,
        "attempts_by_id": {},
        "detail_retry_reset_ids": [],
        "request_reservation": None,
        "detail_records": [],
        "detail_queue_cursor": 0,
        "output_digests": ([
            {"path": path, "sha256": str(index + 1) * 64, "bytes": 123 + index}
            for index, path in enumerate(HEALTH.PROCESSOR_OUTPUT_PATHS)
        ] if status in {"ready", "no-change"} else []),
        "output_artifact": {
            "repository": "StatPan/datapan-registry", "run_id": "200",
            "name": "upstream-catalogue-processing-200", "artifact_id": "201",
            "expires_at": output_expiry,
            "bundle_manifest_sha256": HEALTH.sha256_bytes(HEALTH.canonical_json([
                {"path": path, "sha256": str(index + 1) * 64, "bytes": 123 + index}
                for index, path in enumerate(HEALTH.PROCESSOR_OUTPUT_PATHS)
            ])) if status in {"ready", "no-change"} else None,
        },
        "input_artifacts": [{
            "run_id": producer_run_id, "name": f"upstream-catalog-refresh-{producer_run_id}",
            "artifact_id": "101", "expires_at": input_expiry,
            "candidate_sha256": "d" * 64, "evidence_sha256": evidence_sha, "diff_sha256": "4" * 64,
        }],
        "lease": lease,
        "fencing_token": 1,
        "outcome": {"reason": outcome_reason, "composer_status": composer_status, "pending_count": pending_count},
    }
    value["checkpoint_sha256"] = HEALTH.sha256_bytes(HEALTH.canonical_json(value))
    return value


def promotion_attempt_evidence(
    run_id: int, completed_at: str, workflow_path: str, *, job_status: str = "completed",
    job_conclusion: str | None = "success", job_completed_at: str | None = None,
) -> dict:
    head_sha = "f" * 40
    plain_workflow_path = workflow_path.removesuffix("@main").removesuffix("@refs/heads/main")
    run = {
        "id": run_id, "workflow_id": WORKFLOW_IDS_BY_PATH.get(plain_workflow_path, 1999), "run_attempt": 1,
        "path": plain_workflow_path, "event": "workflow_run", "status": "completed", "conclusion": "success",
        "repository": {"full_name": "StatPan/datapan-registry"},
        "head_repository": {"full_name": "StatPan/datapan-registry"}, "head_branch": "main", "head_sha": head_sha,
        # GitHub can leave this null in the exact-attempt response. Ordering uses the attempt-bound jobs API.
        "completed_at": None, "updated_at": completed_at,
    }
    job = {
        "id": run_id + 10000, "run_id": run_id, "head_sha": head_sha,
        "status": job_status, "conclusion": job_conclusion, "completed_at": job_completed_at or completed_at,
    }
    endpoint = f"repos/StatPan/datapan-registry/actions/runs/{run_id}/attempts/1/jobs"
    return {"run": run, "attempt_number": 1, "jobs_api_endpoint": endpoint, "job_count": 1, "jobs": [job]}


def write_state(root: pathlib.Path, checkpoints: list[dict]) -> pathlib.Path:
    source_dir = root / "sources/data_go_kr"
    generation_dir = source_dir / "generations"
    generation_dir.mkdir(parents=True)
    rows = []
    for value in checkpoints:
        name = f"{value['generation_id']}.json"
        (generation_dir / name).write_text(json.dumps(value, sort_keys=True))
        rows.append({
            "generation_id": value["generation_id"], "status": value["status"],
            "checkpoint": name, "updated_at": value["last_heartbeat_at"],
            "candidate_sha256": value["generation_inputs"]["candidate_sha256"],
        })
    (source_dir / "index.json").write_text(json.dumps({
        "schema_version": "datapan.upstream-catalogue-checkpoint.v1", "generations": rows,
    }))
    return root


def collector_run(*, run_id: str = RUN_ID, conclusion: str = "success", status: str = "completed", created_at: str = "2026-09-30T20:17:00Z") -> dict:
    return {
        "id": int(run_id), "workflow_id": WORKFLOW_IDS_BY_PATH[COLLECTOR_PATH], "path": COLLECTOR_PATH,
        "event": "schedule", "status": status, "conclusion": conclusion,
        "created_at": created_at, "updated_at": "2026-09-30T20:25:00Z",
        "run_started_at": "2026-09-30T20:17:01Z", "head_branch": "main",
        "head_sha": "a" * 40,
        "repository": {"full_name": "StatPan/datapan-registry"},
        "head_repository": {"full_name": "StatPan/datapan-registry"},
    }


def promotion_receipt(generation_id: str, status: str) -> dict:
    candidate = {
        "repository": "StatPan/datapan-registry", "source_id": "data_go_kr",
        "scope": "aggregate_supported_catalog", "base_sha": "a" * 40, "head_sha": "b" * 40,
        "manifest_sha256": "c" * 64, "registry_path": "data/data-go-kr.registry.json",
        "registry_bytes": 123, "registry_sha256": "d" * 64,
        "composition_receipt_sha256": "e" * 64, "generation_id": generation_id,
    }
    merge_sha = "f" * 40
    revision = "1" * 40
    pointer_revision = "2" * 40
    sequence = ["pending-review", "merged", "publication-pending", "published", "read-back-confirmed"]
    stop = sequence.index(status) + 1 if status in sequence else 0
    acks = []
    for index, state in enumerate(sequence[:stop]):
        source_sha = candidate["head_sha"] if state == "pending-review" else merge_sha
        run_id = 300 + index
        acks.append({
            "status": state, "observed_at": "2026-09-30T21:00:00Z", "source_sha": source_sha,
            "manifest_sha256": candidate["manifest_sha256"],
            "artifact_identity": {"path": candidate["registry_path"], "bytes": candidate["registry_bytes"], "sha256": candidate["registry_sha256"]},
            "evidence_reference": "workflow:123", "run_url": f"https://github.com/StatPan/datapan-registry/actions/runs/{run_id}/attempts/1",
            "run_id": run_id, "run_attempt": 1,
            "read_back_verified": state == "read-back-confirmed",
            "read_back_sha256": candidate["registry_sha256"] if state == "read-back-confirmed" else None,
            "read_back_bytes": candidate["registry_bytes"] if state == "read-back-confirmed" else None,
            "publication_revision": revision if state in {"published", "read-back-confirmed"} else None,
            "publication_pointer_revision": pointer_revision if state in {"published", "read-back-confirmed"} else None,
        })
    return {
        "schema_version": "datapan.canonical-update-promotion-receipt.v1",
        "status": status if stop else "prepared", "action": "create", "candidate": candidate,
        "ownership": {"owner_id": "datapan-canonical-update:v1:" + "2" * 64, "branch": "automation/canonical-update/data_go_kr-x", "expected_head_sha": "a" * 40, "body_sha256": "3" * 64},
        "pr": {"number": 2, "url": "https://github.com/StatPan/datapan-registry/pull/2", "state": "merged" if "merged" in sequence[:stop] else "open", "merge_commit_sha": merge_sha if "merged" in sequence[:stop] else None},
        "supersedes_prs": [], "acknowledgements": acks,
    }


class UpstreamCatalogueHealthTest(unittest.TestCase):
    def run_health(self, temp: pathlib.Path, *, cp: list[dict] | None = None, runs: list[dict] | None = None,
                   artifacts_by_run: dict | None = None, artifacts_by_id: dict | None = None,
                   ack: dict | None = None, mode: str = "live", api_error: str | None = None,
                   as_of: dt.datetime = AS_OF, last_good: dict | None = None,
                   promotion_runs: dict | None = None) -> dict:
        checkpoints = cp if cp is not None else [checkpoint()]
        run_rows = runs if runs is not None else [collector_run()]
        artifact_rows = artifacts_by_run if artifacts_by_run is not None else {RUN_ID: [{"id": 101, "name": f"upstream-catalog-refresh-{RUN_ID}", "expired": False}]}
        state_dir = write_state(temp / "state", checkpoints)
        registry = ROOT / "data/data-go-kr.registry.json"
        durable_last_good = last_good if last_good is not None else {
            "data_go_kr": {
                "status": "read-back-confirmed", "source_id": "data_go_kr", "generation_id": "0" * 64,
                "publication_revision": "9" * 40, "publication_pointer_revision": "8" * 40,
                "artifact_identity": {"path": "data/data-go-kr.registry.json", "bytes": 123, "sha256": "d" * 64},
                "verified": True, "publication_run_jobs_completed_at": "2026-09-29T00:00:00Z",
                "publication_run_completion_basis": "max_completed_at_all_jobs_exact_run_attempt",
                "publication_run_id": 90, "publication_run_attempt": 1,
            }
        }
        run_map = dict(promotion_runs or {})
        if ack is not None:
            for record in HEALTH.promotion_records_for_source(ack, "data_go_kr"):
                for row in record.get("acknowledgements", []):
                    if not isinstance(row, dict):
                        continue
                    workflow_path = (
                        PUBLICATION_ACK_PATH
                        if row.get("status") in {"publication-pending", "published", "read-back-confirmed"}
                        else PROMOTION_PATH
                    )
                    run_map.setdefault(
                        f"{row.get('run_id')}/{row.get('run_attempt')}",
                        promotion_attempt_evidence(int(row.get("run_id")), row.get("observed_at"), workflow_path),
                    )
        return HEALTH.evaluate(
            as_of=as_of, repository="StatPan/datapan-registry", health_policy=POLICY,
            source_policy=SOURCE_POLICY, workflow_runs=run_rows,
            artifacts_by_run=artifact_rows,
            artifact_by_id=artifacts_by_id or {}, processor_state_dir=state_dir, promotion_ack=ack,
            main_revision="a" * 40, manifest_sha256="b" * 64, registry_path=registry,
            last_good=durable_last_good,
            mode=mode, workflow_run_id="900", workflow_run_attempt=1,
            source_policy_sha256=SOURCE_POLICY_SHA, health_policy_sha256=HEALTH_POLICY_SHA,
            workflow_api_error=api_error,
            producer_runs_by_id={str(row.get("id")): row for row in run_rows if isinstance(row, dict)},
            promotion_runs_by_id=run_map,
            promotion_workflow_paths=POLICY["promotion_state"],
            workflow_ids_by_path=WORKFLOW_IDS_BY_PATH,
        )

    def test_fresh_live_observation_and_terminal_no_change_are_separate_from_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory))
        HEALTH.validate_schema(report, ROOT / "schemas/datapan.upstream-catalogue-health.v1.schema.json", "receipt")
        source = report["sources"][0]
        self.assertEqual(source["observation"]["state"], "fresh")
        self.assertEqual(source["processor"]["state"], "no-change")
        self.assertEqual(report["summary"]["live_fresh_observation_count"], 1)

    def test_real_actions_run_shape_binds_plain_path_without_a_ref_field(self) -> None:
        run = collector_run()
        self.assertEqual(run["path"], COLLECTOR_PATH)
        self.assertNotIn("ref", run)
        self.assertTrue(HEALTH.trusted_main_workflow_run(
            run, "StatPan/datapan-registry", COLLECTOR_PATH,
            WORKFLOW_IDS_BY_PATH[COLLECTOR_PATH], {"schedule", "workflow_dispatch"},
        ))
        invalid_rows = []
        for field in ("repository", "head_repository", "head_sha", "workflow_id", "path", "event", "head_branch"):
            invalid = copy.deepcopy(run)
            invalid.pop(field)
            invalid_rows.append((field, invalid))
        wrong_repository = copy.deepcopy(run)
        wrong_repository["repository"]["full_name"] = "someone-else/datapan-registry"
        invalid_rows.append(("repository.full_name", wrong_repository))
        wrong_head_repository = copy.deepcopy(run)
        wrong_head_repository["head_repository"]["full_name"] = "someone-else/datapan-registry"
        invalid_rows.append(("head_repository.full_name", wrong_head_repository))
        wrong_workflow_id = copy.deepcopy(run)
        wrong_workflow_id["workflow_id"] += 1
        invalid_rows.append(("workflow_id mismatch", wrong_workflow_id))
        wrong_sha = copy.deepcopy(run)
        wrong_sha["head_sha"] = "not-a-commit"
        invalid_rows.append(("head_sha format", wrong_sha))
        for label, invalid in invalid_rows:
            with self.subTest(label=label):
                self.assertFalse(HEALTH.trusted_main_workflow_run(
                    invalid, "StatPan/datapan-registry", COLLECTOR_PATH,
                    WORKFLOW_IDS_BY_PATH[COLLECTOR_PATH], {"schedule", "workflow_dispatch"},
                ))

    def test_workflow_identity_uses_github_metadata_for_the_configured_path(self) -> None:
        with mock.patch.object(HEALTH, "gh_json", return_value={"id": 1101, "path": COLLECTOR_PATH}) as api:
            self.assertEqual(HEALTH.collect_workflow_identity("StatPan/datapan-registry", COLLECTOR_PATH), 1101)
        api.assert_called_once_with("repos/StatPan/datapan-registry/actions/workflows/upstream-catalog-refresh.yml")
        with mock.patch.object(HEALTH, "gh_json", return_value={"id": 1101, "path": ".github/workflows/other.yml"}):
            with self.assertRaisesRegex(RuntimeError, "github_workflow_identity_invalid"):
                HEALTH.collect_workflow_identity("StatPan/datapan-registry", COLLECTOR_PATH)

    def test_incomplete_actions_run_cannot_supply_schedule_or_observation_trust(self) -> None:
        incomplete = collector_run()
        incomplete.pop("repository")
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), runs=[incomplete])
        reasons = {row["reason"] for row in report["faults"]}
        self.assertIn("scheduled_execution_missing", reasons)
        self.assertIn("source_observation_run_unverified", reasons)

    def test_main_canonical_identity_uses_manifest_bound_lfs_oid_without_materializing_payload(self) -> None:
        expected_oid = "a" * 64
        expected_bytes = 12345
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{expected_oid}\n"
            f"size {expected_bytes}\n"
        ).encode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = root / "data/data-go-kr.registry.json"
            registry.parent.mkdir(parents=True)
            registry.write_bytes(pointer)
            manifest_path = root / "manifest.json"
            manifest = {
                "artifacts": [{
                    "path": "data/data-go-kr.registry.json",
                    "kind": "registry",
                    "sha256": expected_oid,
                    "bytes": expected_bytes,
                }],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.object(HEALTH, "ROOT", root):
                identity = HEALTH.manifest_registry_identity(manifest_path, registry)
                self.assertEqual(identity["registry_sha256"], expected_oid)
                self.assertEqual(identity["registry_bytes"], expected_bytes)

                manifest["artifacts"][0]["sha256"] = "b" * 64
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "main_registry_manifest_mismatch"):
                    HEALTH.manifest_registry_identity(manifest_path, registry)

    def test_never_started_collector_and_missing_processor_observation_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), runs=[], cp=[])
        reasons = {row["reason"] for row in report["faults"]}
        self.assertIn("scheduled_execution_missing", reasons)
        self.assertIn("observation_missing", reasons)

    def test_failed_before_artifact_is_a_collector_failure_not_zero_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), runs=[collector_run(conclusion="failure")], artifacts_by_run={RUN_ID: []})
        reasons = {row["reason"] for row in report["faults"]}
        self.assertIn("collector_run_failed", reasons)
        self.assertNotIn("collector_artifact_missing_or_expired", reasons)

    def test_success_without_run_bound_artifact_is_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), artifacts_by_run={RUN_ID: []})
        self.assertIn("collector_artifact_missing_or_expired", {row["reason"] for row in report["faults"]})

    def test_later_manual_observation_does_not_fail_against_older_scheduled_run(self) -> None:
        manual = collector_run(run_id="101", created_at="2026-09-30T22:00:00Z")
        manual.update({"event": "workflow_dispatch", "updated_at": "2026-09-30T22:08:00Z"})
        cp = checkpoint(observed_at="2026-09-30T22:05:00Z", producer_run_id="101")
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(
                pathlib.Path(directory), cp=[cp], runs=[collector_run(), manual],
                artifacts_by_run={
                    RUN_ID: [{"id": 101, "name": f"upstream-catalog-refresh-{RUN_ID}", "expired": False}],
                    "101": [{"id": 102, "name": "upstream-catalog-refresh-101", "expired": False}],
                },
            )
        self.assertNotIn("successful_collector_not_observed", {row["reason"] for row in report["faults"]})

    def test_newer_failed_manual_collector_run_is_not_hidden_by_older_success(self) -> None:
        failed_manual = collector_run(run_id="102", conclusion="failure", created_at="2026-09-30T23:50:00Z")
        failed_manual.update({"event": "workflow_dispatch", "updated_at": "2026-09-30T23:55:00Z"})
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(
                pathlib.Path(directory), runs=[collector_run(), failed_manual],
                artifacts_by_run={
                    RUN_ID: [{"id": 101, "name": f"upstream-catalog-refresh-{RUN_ID}", "expired": False}],
                    "102": [],
                },
            )
        source = report["sources"][0]
        self.assertIn("collector_run_failed", {row["reason"] for row in source["faults"]})
        self.assertEqual(source["collector"]["latest_scheduled_run"]["run_id"], RUN_ID)
        self.assertEqual(source["collector"]["latest_execution_run"]["run_id"], "102")

    def test_heartbeat_does_not_hide_stalled_progress(self) -> None:
        cp = checkpoint(
            status="enriching", composer_status="pending", last_progress_at="2026-09-30T21:00:00Z",
            last_heartbeat_at="2026-09-30T23:59:00Z", lease={"owner_run_id": "200", "expires_at": "2026-10-01T00:30:00Z", "fencing_token": 1},
        )
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), cp=[cp])
        reasons = {row["reason"] for row in report["faults"]}
        self.assertIn("stage_progress_stalled", reasons)
        self.assertNotIn("heartbeat_stale", reasons)

    def test_expired_lease_input_and_output_are_distinct(self) -> None:
        cases = (
            (checkpoint(status="enriching", composer_status="pending", lease={"owner_run_id": "200", "expires_at": "2026-09-30T23:00:00Z", "fencing_token": 1}), "lease_expired"),
            (checkpoint(status="validating", composer_status="pending", lease={"owner_run_id": "200", "expires_at": "2026-10-01T00:30:00Z", "fencing_token": 1}, input_expiry="2026-09-30T23:00:00Z"), "input_artifact_expired"),
            (checkpoint(status="ready", output_expiry="2026-09-30T23:00:00Z"), "candidate_output_unavailable"),
        )
        for cp, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                report = self.run_health(pathlib.Path(directory), cp=[cp])
            self.assertIn(expected, {row["reason"] for row in report["faults"]})

    def test_retry_exhaustion_and_scoped_candidate_remain_actionable(self) -> None:
        exhausted = checkpoint(status="retry", attempts_consumed=240, composer_status="retry", outcome_reason="pending_detail_or_no_safe_change")
        exhausted["attempts_by_id"] = {"42": 3}
        exhausted["detail_records"] = [{"id": "42", "status": "quarantined", "source_sha256": "5" * 64, "guide_sha256": None}]
        exhausted["checkpoint_sha256"] = HEALTH.sha256_bytes(HEALTH.canonical_json({k: v for k, v in exhausted.items() if k != "checkpoint_sha256"}))
        scoped = checkpoint(status="ready", composer_status="ready_scoped", outcome_reason="scoped_candidate_ready_pending_outcomes_retained", pending_count=3)
        for cp, expected in ((exhausted, "retry_exhausted"), (scoped, "candidate_ready_with_pending_scope")):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                report = self.run_health(pathlib.Path(directory), cp=[cp])
            self.assertIn(expected, {row["reason"] for row in report["faults"]})

        progressing = checkpoint(status="retry", attempts_consumed=240, composer_status="retry", outcome_reason="pending_detail_or_no_safe_change")
        progressing["attempts_by_id"] = {"42": 2, "43": 3}
        progressing["detail_records"] = [
            {"id": "42", "status": "retry", "source_sha256": "5" * 64, "guide_sha256": None},
            {"id": "43", "status": "enriched", "source_sha256": "6" * 64, "guide_sha256": None},
        ]
        progressing["checkpoint_sha256"] = HEALTH.sha256_bytes(HEALTH.canonical_json({k: v for k, v in progressing.items() if k != "checkpoint_sha256"}))
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), cp=[progressing])
        self.assertNotIn("retry_exhausted", {row["reason"] for row in report["faults"]})

    def test_fixture_namespace_cannot_be_counted_as_operational_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), mode="fixture", cp=[checkpoint(execution_mode="live")])
        self.assertEqual(report["sources"][0]["overall"], "fixture")
        self.assertEqual(report["sources"][0]["observation"]["state"], "fixture_only")
        self.assertEqual(report["sources"][0]["observation"]["execution_mode"], "fixture")
        self.assertEqual(report["summary"]["live_fresh_observation_count"], 0)

    def test_future_timestamp_is_rejected_before_freshness_is_claimed(self) -> None:
        cp = checkpoint(observed_at="2026-10-01T02:00:00Z")
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), cp=[cp])
        reasons = {row["reason"] for row in report["faults"]}
        self.assertTrue(any(reason.startswith("future_timestamp:") for reason in reasons))
        self.assertNotEqual(report["sources"][0]["observation"]["state"], "fresh")

    def test_policy_digest_change_is_reported(self) -> None:
        cp = checkpoint()
        cp["generation_inputs"]["policy_sha256"] = "9" * 64
        cp["checkpoint_sha256"] = HEALTH.sha256_bytes(HEALTH.canonical_json({k: v for k, v in cp.items() if k != "checkpoint_sha256"}))
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), cp=[cp])
        self.assertIn("checkpoint_policy_changed", {row["reason"] for row in report["faults"]})

    def test_pending_review_and_published_do_not_replace_last_good(self) -> None:
        for status in ("pending-review", "published"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                ack = promotion_receipt(GENERATION_ID, status)
                report = self.run_health(pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")], ack=ack)
            source = report["sources"][0]
            self.assertEqual(source["canonical"]["last_good"]["publication_revision"], "9" * 40)
            self.assertEqual(source["canonical"]["promotion_status"], status)
            if status == "pending-review":
                self.assertIn("promotion_pending_review", {row["reason"] for row in source["faults"]})
            else:
                self.assertIn("publication_or_readback_pending", {row["reason"] for row in source["faults"]})

    def test_review_wait_and_readback_lag_escalate_only_after_configured_deadlines(self) -> None:
        review_deadline = POLICY["sources"][0]["stage_deadlines_seconds"]["pending-review"]
        publication_deadline = POLICY["sources"][0]["stage_deadlines_seconds"]["publication-pending"]
        review_entry = AS_OF - dt.timedelta(seconds=review_deadline)
        for evaluated_at, overdue in (
            (review_entry + dt.timedelta(seconds=review_deadline), False),
            (review_entry + dt.timedelta(seconds=review_deadline + 1), True),
        ):
            with self.subTest(stage="review", overdue=overdue), tempfile.TemporaryDirectory() as directory:
                ack = promotion_receipt(GENERATION_ID, "pending-review")
                ack["acknowledgements"][0]["observed_at"] = review_entry.isoformat().replace("+00:00", "Z")
                report = self.run_health(pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")], ack=ack, as_of=evaluated_at)
            source = report["sources"][0]
            reasons = {row["reason"] for row in source["faults"]}
            self.assertEqual("promotion_review_wait_overdue" in reasons, overdue)
            self.assertEqual(source["canonical"]["promotion_status"], "pending-review")
            self.assertEqual(source["canonical"]["promotion_stage"]["age_seconds"], review_deadline + int(overdue))

        publication_entry = AS_OF - dt.timedelta(seconds=publication_deadline)
        for evaluated_at, overdue in (
            (publication_entry + dt.timedelta(seconds=publication_deadline), False),
            (publication_entry + dt.timedelta(seconds=publication_deadline + 1), True),
        ):
            with self.subTest(stage="publication", overdue=overdue), tempfile.TemporaryDirectory() as directory:
                ack = promotion_receipt(GENERATION_ID, "published")
                for row in ack["acknowledgements"]:
                    row["observed_at"] = publication_entry.isoformat().replace("+00:00", "Z")
                report = self.run_health(pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")], ack=ack, as_of=evaluated_at)
            source = report["sources"][0]
            reasons = {row["reason"] for row in source["faults"]}
            self.assertEqual("publication_readback_lag_overdue" in reasons, overdue)
            self.assertEqual(source["canonical"]["promotion_status"], "published")
            self.assertEqual(source["canonical"]["promotion_stage"]["age_seconds"], publication_deadline + int(overdue))

    def test_publication_retry_starts_a_new_deadline_after_failed_attempt(self) -> None:
        ack = promotion_receipt(GENERATION_ID, "publication-pending")
        failed_at = "2026-09-20T00:00:00Z"
        for row in ack["acknowledgements"]:
            row["observed_at"] = "2026-09-19T00:00:00Z"
        ack["acknowledgements"][-1]["observed_at"] = failed_at
        failed = {
            **ack["acknowledgements"][-1], "status": "failed", "observed_at": "2026-09-20T01:00:00Z",
            "run_id": 400, "run_attempt": 1,
            "run_url": "https://github.com/StatPan/datapan-registry/actions/runs/400/attempts/1",
            "publication_revision": None, "publication_pointer_revision": None,
        }
        retried_at = "2026-09-30T23:00:00Z"
        retried = {
            **failed, "status": "publication-pending", "observed_at": retried_at,
            "run_id": 401, "run_url": "https://github.com/StatPan/datapan-registry/actions/runs/401/attempts/1",
        }
        ack["acknowledgements"].extend([failed, retried])
        ack["status"] = "publication-pending"
        self.assertEqual(HEALTH.promotion_items_for_source(ack, "data_go_kr", GENERATION_ID)[-1], retried)
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")], ack=ack)
        source = report["sources"][0]
        self.assertEqual(source["canonical"]["promotion_stage"]["entered_at"], retried_at)
        self.assertEqual(source["canonical"]["promotion_stage"]["age_seconds"], 3600)
        self.assertNotIn("publication_readback_lag_overdue", {row["reason"] for row in source["faults"]})

    def test_readback_confirmation_advances_last_good_only_for_matching_bytes(self) -> None:
        ack = promotion_receipt(GENERATION_ID, "read-back-confirmed")
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")], ack=ack)
        last_good = report["sources"][0]["canonical"]["last_good"]
        self.assertEqual(last_good["publication_revision"], "1" * 40)
        self.assertEqual(last_good["publication_pointer_revision"], "2" * 40)
        self.assertTrue(last_good["verified"])

    def test_readback_does_not_advance_last_good_when_exact_ack_run_lookup_fails(self) -> None:
        ack = promotion_receipt(GENERATION_ID, "read-back-confirmed")
        final_run_id = str(ack["acknowledgements"][-1]["run_id"])
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(
                pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")], ack=ack,
                promotion_runs={f"{final_run_id}/1": {"availability_error": True}},
            )
        source = report["sources"][0]
        self.assertEqual(source["canonical"]["last_good"]["publication_revision"], "9" * 40)
        self.assertIn("promotion_readback_run_unverified", {row["reason"] for row in source["faults"]})

    def test_readback_needs_completed_jobs_for_exact_attempt_when_run_timestamp_is_null(self) -> None:
        ack = promotion_receipt(GENERATION_ID, "read-back-confirmed")
        final_run_id = str(ack["acknowledgements"][-1]["run_id"])
        incomplete_attempt = promotion_attempt_evidence(
            int(final_run_id), "2026-09-30T21:00:00Z",
            PUBLICATION_ACK_PATH,
            job_status="in_progress", job_conclusion=None, job_completed_at=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(
                pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")], ack=ack,
                promotion_runs={f"{final_run_id}/1": incomplete_attempt},
            )
        source = report["sources"][0]
        self.assertEqual(source["canonical"]["last_good"]["publication_revision"], "9" * 40)
        self.assertIn("promotion_readback_run_unverified", {row["reason"] for row in source["faults"]})

    def test_completed_attempt_jobs_supply_explicit_last_good_ordering_basis(self) -> None:
        ack = promotion_receipt(GENERATION_ID, "read-back-confirmed")
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), ack=ack)
        last_good = report["sources"][0]["canonical"]["last_good"]
        self.assertEqual(last_good["publication_run_jobs_completed_at"], "2026-09-30T21:00:00Z")
        self.assertEqual(last_good["publication_run_completion_basis"], "max_completed_at_all_jobs_exact_run_attempt")

    def test_processor_bundle_digest_and_path_inventory_are_cross_bound(self) -> None:
        bad_digest = checkpoint(status="ready", composer_status="ready_scoped")
        bad_digest["output_artifact"]["bundle_manifest_sha256"] = "9" * 64
        bad_digest["checkpoint_sha256"] = HEALTH.sha256_bytes(HEALTH.canonical_json({k: v for k, v in bad_digest.items() if k != "checkpoint_sha256"}))
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), cp=[bad_digest])
        reasons = {row["reason"] for row in report["sources"][0]["faults"]}
        self.assertIn("candidate_output_bundle_digest_mismatch", reasons)

        duplicate_path = checkpoint(status="ready", composer_status="ready_scoped")
        duplicate_path["output_digests"][1]["path"] = duplicate_path["output_digests"][0]["path"]
        duplicate_path["output_artifact"]["bundle_manifest_sha256"] = HEALTH.sha256_bytes(HEALTH.canonical_json(duplicate_path["output_digests"]))
        duplicate_path["checkpoint_sha256"] = HEALTH.sha256_bytes(HEALTH.canonical_json({k: v for k, v in duplicate_path.items() if k != "checkpoint_sha256"}))
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(pathlib.Path(directory), cp=[duplicate_path])
        reasons = {row["reason"] for row in report["sources"][0]["faults"]}
        self.assertIn("candidate_output_bundle_duplicate_path", reasons)

    def test_real_processor_eight_file_bundle_is_accepted_and_result_tampering_is_rejected(self) -> None:
        # Captured from #657's test_ready_scoped_candidate_has_exact_composer_evidence_and_durable_artifact_digests.
        self.assertEqual(tuple(row["path"] for row in REAL_PROCESSOR_READY_BUNDLE), HEALTH.PROCESSOR_OUTPUT_PATHS)
        self.assertEqual(
            HEALTH.sha256_bytes(HEALTH.canonical_json(REAL_PROCESSOR_READY_BUNDLE)),
            REAL_PROCESSOR_READY_BUNDLE_SHA256,
        )
        producer_checkpoint = {
            "status": "ready",
            "output_digests": copy.deepcopy(REAL_PROCESSOR_READY_BUNDLE),
            "output_artifact": {"bundle_manifest_sha256": REAL_PROCESSOR_READY_BUNDLE_SHA256},
        }
        self.assertEqual(HEALTH.processor_output_bundle_valid(producer_checkpoint), (True, ""))

        missing_result = copy.deepcopy(producer_checkpoint)
        missing_result["output_digests"].pop()
        missing_result["output_artifact"]["bundle_manifest_sha256"] = HEALTH.sha256_bytes(
            HEALTH.canonical_json(missing_result["output_digests"])
        )
        self.assertEqual(
            HEALTH.processor_output_bundle_valid(missing_result),
            (False, "candidate_output_bundle_paths_incomplete"),
        )

        tampered_result = copy.deepcopy(producer_checkpoint)
        tampered_result["output_digests"][-1]["sha256"] = "9" * 64
        self.assertEqual(
            HEALTH.processor_output_bundle_valid(tampered_result),
            (False, "candidate_output_bundle_digest_mismatch"),
        )

    def test_attempt_job_collection_paginates_the_exact_run_attempt(self) -> None:
        pages = [
            {"total_count": 2, "jobs": [{"id": 7, "run_id": 123}]},
            {"total_count": 2, "jobs": [{"id": 8, "run_id": 123}]},
        ]
        with mock.patch.object(HEALTH, "gh_json", side_effect=pages) as api:
            evidence = HEALTH.collect_run_attempt_jobs("StatPan/datapan-registry", "123", 2)
        self.assertEqual(evidence["attempt_number"], 2)
        self.assertEqual(evidence["job_count"], 2)
        self.assertEqual(api.call_args_list[0].args[0], "repos/StatPan/datapan-registry/actions/runs/123/attempts/2/jobs?per_page=100&page=1")
        self.assertEqual(api.call_args_list[1].args[0], "repos/StatPan/datapan-registry/actions/runs/123/attempts/2/jobs?per_page=100&page=2")

    def test_readback_does_not_advance_last_good_for_wrong_workflow_path(self) -> None:
        ack = promotion_receipt(GENERATION_ID, "read-back-confirmed")
        final_run_id = str(ack["acknowledgements"][-1]["run_id"])
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(
                pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")], ack=ack,
                promotion_runs={f"{final_run_id}/1": promotion_attempt_evidence(
                    int(final_run_id), "2026-09-30T21:00:00Z", ".github/workflows/unrelated.yml@main",
                )},
            )
        source = report["sources"][0]
        self.assertEqual(source["canonical"]["last_good"]["publication_revision"], "9" * 40)
        self.assertIn("promotion_readback_run_unverified", {row["reason"] for row in source["faults"]})

    def test_readback_requires_run_repository_workflow_id_and_head_sha(self) -> None:
        ack = promotion_receipt(GENERATION_ID, "read-back-confirmed")
        final_run_id = str(ack["acknowledgements"][-1]["run_id"])
        valid = promotion_attempt_evidence(int(final_run_id), "2026-09-30T21:00:00Z", PUBLICATION_ACK_PATH)
        for field in ("repository", "workflow_id", "head_sha"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                malformed = copy.deepcopy(valid)
                malformed["run"].pop(field)
                report = self.run_health(
                    pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")], ack=ack,
                    promotion_runs={f"{final_run_id}/1": malformed},
                )
            source = report["sources"][0]
            self.assertEqual(source["canonical"]["last_good"]["publication_revision"], "9" * 40)
            self.assertIn("promotion_readback_run_unverified", {row["reason"] for row in source["faults"]})

    def test_readback_from_older_generation_advances_last_good_while_new_candidate_waits(self) -> None:
        generation_b = "9" * 64
        older_published = promotion_receipt(GENERATION_ID, "read-back-confirmed")
        current_pending = promotion_receipt(generation_b, "pending-review")
        journal = {
            "schema_version": "datapan.canonical-update-promotion-journal.v1",
            "repository": "StatPan/datapan-registry", "updated_at": "2026-09-30T22:00:00Z",
            "records": [older_published, current_pending],
        }
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(
                pathlib.Path(directory), cp=[checkpoint(generation_id=generation_b, status="ready", composer_status="ready")],
                ack=journal,
            )
        canonical = report["sources"][0]["canonical"]
        self.assertEqual(canonical["promotion_status"], "pending-review")
        self.assertEqual(canonical["last_good"]["generation_id"], GENERATION_ID)
        self.assertEqual(canonical["last_good"]["publication_revision"], "1" * 40)

    @staticmethod
    def revision_ref(record: dict) -> dict:
        candidate = record["candidate"]
        ownership = record["ownership"]
        return {
            "repository": candidate["repository"],
            "source_id": candidate["source_id"],
            "scope": candidate["scope"],
            "generation_id": candidate["generation_id"],
            "registry_sha256": candidate["registry_sha256"],
            "head_sha": candidate["head_sha"],
            "pr_number": record["pr"]["number"],
            "owner_id": ownership["owner_id"],
            "branch": ownership["branch"],
            "body_sha256": ownership["body_sha256"],
        }

    def test_health_selects_only_superseding_same_generation_payload_and_keeps_last_good(self) -> None:
        previous = promotion_receipt(GENERATION_ID, "pending-review")
        current = promotion_receipt(GENERATION_ID, "pending-review")
        current["candidate"]["registry_sha256"] = "e" * 64
        current["candidate"]["head_sha"] = "c" * 40
        current["acknowledgements"][0]["artifact_identity"]["sha256"] = "e" * 64
        current["acknowledgements"][0]["source_sha"] = "c" * 40
        current["ownership"]["expected_head_sha"] = "c" * 40
        current["refresh_from"] = self.revision_ref(previous)
        previous["superseded_by"] = self.revision_ref(current)
        journal = {
            "schema_version": "datapan.canonical-update-promotion-journal.v1",
            "repository": "StatPan/datapan-registry",
            "updated_at": "2026-09-30T22:00:00Z",
            "records": [previous, current],
        }
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(
                pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")], ack=journal,
            )
        canonical = report["sources"][0]["canonical"]
        self.assertEqual(canonical["promotion_status"], "pending-review")
        self.assertEqual(canonical["last_good"]["generation_id"], "0" * 64)
        self.assertEqual(canonical["last_good"]["publication_revision"], "9" * 40)
        reasons = {row["reason"] for row in report["sources"][0]["faults"]}
        self.assertIn("promotion_pending_review", reasons)
        self.assertNotIn("promotion_generation_record_ambiguous", reasons)

    def test_health_keeps_predecessor_active_while_exact_refresh_intent_is_prepared(self) -> None:
        previous = promotion_receipt(GENERATION_ID, "pending-review")
        target = promotion_receipt(GENERATION_ID, "prepared")
        target["candidate"]["registry_sha256"] = "e" * 64
        target["candidate"]["head_sha"] = "c" * 40
        target["ownership"]["expected_head_sha"] = "c" * 40
        target["acknowledgements"] = []
        target["refresh_from"] = self.revision_ref(previous)
        journal = {
            "schema_version": "datapan.canonical-update-promotion-journal.v1",
            "repository": "StatPan/datapan-registry",
            "updated_at": "2026-09-30T22:00:00Z",
            "records": [previous, target],
        }
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(
                pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")], ack=journal,
            )
        canonical = report["sources"][0]["canonical"]
        self.assertEqual(canonical["promotion_status"], "pending-review")
        self.assertNotIn("promotion_generation_record_ambiguous", {row["reason"] for row in report["sources"][0]["faults"]})

    def test_older_late_ingested_readback_cannot_replace_a_newer_last_good(self) -> None:
        older = promotion_receipt(GENERATION_ID, "read-back-confirmed")
        completed_at = "2026-09-29T12:00:00Z"
        for row in older["acknowledgements"]:
            row["observed_at"] = completed_at
        newer_good = {
            "status": "read-back-confirmed", "source_id": "data_go_kr", "generation_id": "9" * 64,
            "publication_revision": "7" * 40, "publication_pointer_revision": "6" * 40,
            "artifact_identity": {"path": "data/data-go-kr.registry.json", "bytes": 123, "sha256": "d" * 64},
            "verified": True, "publication_run_jobs_completed_at": "2026-09-30T23:00:00Z",
            "publication_run_completion_basis": "max_completed_at_all_jobs_exact_run_attempt",
            "publication_run_id": 999, "publication_run_attempt": 1,
        }
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_health(
                pathlib.Path(directory), cp=[checkpoint(status="ready", composer_status="ready")],
                ack=older, last_good={"data_go_kr": newer_good},
            )
        last_good = report["sources"][0]["canonical"]["last_good"]
        self.assertEqual(last_good["generation_id"], "9" * 64)
        self.assertEqual(last_good["publication_revision"], "7" * 40)

    def test_append_order_wins_for_equal_timestamps_and_revision_mismatch_is_rejected(self) -> None:
        ack = promotion_receipt(GENERATION_ID, "read-back-confirmed")
        self.assertEqual(HEALTH.promotion_items_for_source(ack, "data_go_kr", GENERATION_ID)[-1]["status"], "read-back-confirmed")
        ack["acknowledgements"][-1]["publication_revision"] = "8" * 40
        self.assertEqual(HEALTH.promotion_items_for_source(ack, "data_go_kr", GENERATION_ID), [])
        ack = promotion_receipt(GENERATION_ID, "read-back-confirmed")
        ack["acknowledgements"][-1]["publication_pointer_revision"] = "8" * 40
        self.assertEqual(HEALTH.promotion_items_for_source(ack, "data_go_kr", GENERATION_ID), [])

    def test_fault_key_deduplicates_repeated_identical_failure(self) -> None:
        first = HEALTH.fault("data_go_kr", "collector", "scheduled_execution_missing", "error", 659, "check")
        second = HEALTH.fault("data_go_kr", "collector", "scheduled_execution_missing", "error", 659, "check")
        changed_identity = HEALTH.fault("data_go_kr", "collector", "scheduled_execution_missing", "error", 659, "check", "different-policy-digest")
        self.assertEqual(first["fault_key"], second["fault_key"])
        self.assertNotEqual(first["fault_key"], changed_identity["fault_key"])

    def write_report(self, directory: pathlib.Path, report: dict, name: str = "receipt.json") -> pathlib.Path:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        return path

    def test_durable_writer_deduplicates_faults_and_replayed_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            state_root = temp / "worktree/health/upstream-catalogue"
            first = self.run_health(temp / "first", runs=[], cp=[])
            receipt_path = self.write_report(temp, first)
            first_result = PERSIST.persist(receipt_path, state_root, ROOT / "policy/upstream-catalogue-health.json", "StatPan/datapan-registry")
            state_path = state_root / "state.json"
            state = json.loads(state_path.read_text())
            self.assertEqual(len(state["faults"]), first["summary"]["fault_count"])
            self.assertEqual(state["faults"][0]["observation_count"], 1)

            same_receipt = PERSIST.persist(receipt_path, state_root, ROOT / "policy/upstream-catalogue-health.json", "StatPan/datapan-registry")
            state = json.loads(state_path.read_text())
            self.assertEqual(same_receipt["observation_counts"], {})
            self.assertEqual(state["faults"][0]["observation_count"], 1)

            second_run = collector_run(run_id="101", created_at="2026-09-30T22:00:00Z")
            second_run["event"] = "workflow_dispatch"
            second_cp = checkpoint(observed_at="2026-09-30T22:05:00Z", producer_run_id="101", evidence_sha="d" * 64)
            second_report = self.run_health(
                temp / "second", cp=[second_cp], runs=[collector_run(), second_run],
                artifacts_by_run={
                    RUN_ID: [{"id": 101, "name": f"upstream-catalog-refresh-{RUN_ID}", "expired": False}],
                    "101": [{"id": 102, "name": "upstream-catalog-refresh-101", "expired": False}],
                },
            )
            second_report["health_workflow"]["run_id"] = "901"
            second_report = HEALTH.seal_receipt(second_report)
            receipt2 = self.write_report(temp, second_report, "receipt2.json")
            second_result = PERSIST.persist(receipt2, state_root, ROOT / "policy/upstream-catalogue-health.json", "StatPan/datapan-registry")
            state = json.loads(state_path.read_text())
            self.assertEqual(second_result["observation_counts"]["data_go_kr"], 1)
            self.assertEqual(len(state["observations_by_source"]["data_go_kr"]), 1)
            self.assertTrue(all(row["status"] == "recovered" for row in state["faults"]))
            self.assertEqual(first_result["state_status"], "blocked")

    def test_writer_archives_stale_receipt_without_regressing_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            state_root = temp / "worktree/health/upstream-catalogue"
            state_path = state_root / "state.json"
            first = self.run_health(temp / "first", runs=[], cp=[])
            first_path = self.write_report(temp, first, "first.json")
            PERSIST.persist(first_path, state_root, ROOT / "policy/upstream-catalogue-health.json", "StatPan/datapan-registry")
            before = json.loads(state_path.read_text())

            stale = self.run_health(temp / "stale", runs=[], cp=[], as_of=AS_OF - dt.timedelta(seconds=1))
            stale["health_workflow"]["run_id"] = "901"
            stale = HEALTH.seal_receipt(stale)
            stale_path = self.write_report(temp, stale, "stale.json")
            result = PERSIST.persist(stale_path, state_root, ROOT / "policy/upstream-catalogue-health.json", "StatPan/datapan-registry")
            after = json.loads(state_path.read_text())

        self.assertEqual(result["status"], "stale_receipt_archived")
        self.assertEqual(after["state_sha256"], before["state_sha256"])
        self.assertEqual(after["updated_at"], before["updated_at"])

    def test_writer_never_replaces_last_good_with_late_ingested_older_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            state_root = temp / "worktree/health/upstream-catalogue"
            state_path = state_root / "state.json"

            newest = self.run_health(temp / "newest", runs=[], cp=[])
            newest["sources"][0]["canonical"]["last_good"] = {
                "status": "read-back-confirmed", "verified": True, "source_id": "data_go_kr",
                "generation_id": "9" * 64, "publication_revision": "9" * 40,
                "publication_pointer_revision": "8" * 40,
                "artifact_identity": {"path": "data/data-go-kr.registry.json", "bytes": 123, "sha256": "d" * 64},
                "publication_run_jobs_completed_at": "2026-09-30T23:00:00Z",
                "publication_run_completion_basis": "max_completed_at_all_jobs_exact_run_attempt",
                "publication_run_id": 999,
                "publication_run_attempt": 1,
            }
            newest = HEALTH.seal_receipt(newest)
            first_path = self.write_report(temp, newest, "newest.json")
            PERSIST.persist(first_path, state_root, ROOT / "policy/upstream-catalogue-health.json", "StatPan/datapan-registry")

            older = self.run_health(temp / "older", runs=[], cp=[], as_of=AS_OF + dt.timedelta(hours=1))
            older["health_workflow"]["run_id"] = "902"
            older["sources"][0]["canonical"]["last_good"] = {
                "status": "read-back-confirmed", "verified": True, "source_id": "data_go_kr",
                "generation_id": "8" * 64, "publication_revision": "7" * 40,
                "publication_pointer_revision": "6" * 40,
                "artifact_identity": {"path": "data/data-go-kr.registry.json", "bytes": 123, "sha256": "d" * 64},
                "publication_run_jobs_completed_at": "2026-09-30T22:00:00Z",
                "publication_run_completion_basis": "max_completed_at_all_jobs_exact_run_attempt",
                "publication_run_id": 998,
                "publication_run_attempt": 1,
            }
            older = HEALTH.seal_receipt(older)
            second_path = self.write_report(temp, older, "older.json")
            PERSIST.persist(second_path, state_root, ROOT / "policy/upstream-catalogue-health.json", "StatPan/datapan-registry")
            last_good = json.loads(state_path.read_text())["last_good_by_source"]["data_go_kr"]

        self.assertEqual(last_good["generation_id"], "9" * 64)
        self.assertEqual(last_good["publication_revision"], "9" * 40)

    def test_durable_writer_requires_live_receipt_and_owned_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            receipt = self.run_health(temp / "input", mode="fixture")
            receipt_path = self.write_report(temp, receipt)
            state_root = temp / "worktree/health/upstream-catalogue"
            with self.assertRaisesRegex(ValueError, "fixture_receipt_not_persistable"):
                PERSIST.persist(receipt_path, state_root, ROOT / "policy/upstream-catalogue-health.json", "StatPan/datapan-registry")

            receipt = self.run_health(temp / "input-live")
            receipt_path = self.write_report(temp, receipt, "live.json")
            state_root.mkdir(parents=True)
            (state_root / "other.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unowned_health_state_root_not_empty"):
                PERSIST.persist(receipt_path, state_root, ROOT / "policy/upstream-catalogue-health.json", "StatPan/datapan-registry")


if __name__ == "__main__":
    unittest.main()
