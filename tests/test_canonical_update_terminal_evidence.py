from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
import zipfile
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "canonical_update_terminal_evidence.py"
SPEC = importlib.util.spec_from_file_location("canonical_update_terminal_evidence", MODULE_PATH)
assert SPEC and SPEC.loader
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)

AS_OF = "2026-10-06T11:00:00Z"
SOURCE_SHA = "a" * 40
REGISTRY_SHA = "b" * 64
MANIFEST_SHA = "c" * 64
GENERATION_ID = "d" * 64
CHECKPOINT_SHA = "e" * 64
ARTIFACT_SHA = "f" * 64
ARTIFACT_NAME = "canonical-update-promotion-terminal-37200000001-2-reconcile-prs"
ARTIFACT_ID = "9930001"
RUN_ID = "37200000001"
REPO = "StatPan/datapan-registry"
JSON_PATH = "terminal-outcome.json"
SEAL_PATH = "terminal-outcome.sha256"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _main_identity() -> dict:
    return {
        "revision": SOURCE_SHA,
        "manifest_sha256": MANIFEST_SHA,
        "registry_path": "data/data-go-kr.registry.json",
        "registry_bytes": 1234,
        "registry_sha256": REGISTRY_SHA,
    }


def _generation(status: str = "already_canonical") -> dict:
    return {
        "generation_id": GENERATION_ID,
        "checkpoint_sha256": CHECKPOINT_SHA,
        "source_id": "data_go_kr",
        "source_scope": "aggregate_supported_catalog",
        "status": status,
        "reason_code": "already_canonical_payload" if status == "already_canonical" else None,
        "producer": {
            "run_id": "37199999999",
            "run_attempt": 1,
            "head_sha": "1" * 40,
            "artifact_id": "9920001",
            "artifact_name": "upstream-catalogue-processing-37199999999-1",
            "artifact_expires_at": "2026-11-06T10:00:00Z",
            "bundle_manifest_sha256": "2" * 64,
        },
        "checkpoint_observation": {
            "claim": "checkpoint_reported_last_observation",
            "observed_at": "2026-10-05T10:00:00Z",
            "producer_run_id": "37188888888",
            "refresh_evidence_sha256": "3" * 64,
            "collection_status": "success",
            "execution_mode": "live",
        },
        "checkpoint_observation_count": 2,
        "generation_baseline_sha256": "4" * 64,
        "checkpoint_derivation_sha256": None,
        "composition": {
            "baseline_sha256": "4" * 64,
            "registry_path": "data/data-go-kr.registry.json",
            "registry_bytes": 1234,
            "registry_sha256": REGISTRY_SHA,
            "composition_receipt_sha256": "5" * 64,
        },
        "pending_count": 3,
        "detail_retry_count": 1,
        "detail_unattempted_count": 0,
    }


def _outcome(mode: str) -> dict:
    if mode != "recover-ready":
        return {
            "kind": mode,
            "status": "mode_completed_without_recovery_outcome",
            "candidate_available": False,
            "evaluated_main": None,
            "selected_generation_id": None,
            "preparation_returned": False,
            "generation_count": 0,
            "generation_results_truncated": False,
            "generations": [],
        }
    return {
        "kind": mode,
        "status": "already-canonical-payload",
        "candidate_available": False,
        "evaluated_main": _main_identity(),
        "selected_generation_id": None,
        "preparation_returned": False,
        "generation_count": 1,
        "generation_results_truncated": False,
        "generations": [_generation()],
    }


def _document(mode: str = "reconcile-prs", **changes) -> dict:
    document = {
        "schema_version": EVIDENCE.SCHEMA_VERSION,
        "repository": REPO,
        "workflow_path": EVIDENCE.WORKFLOW_PATH,
        "invocation": {
            "mode": mode,
            "run_id": RUN_ID,
            "run_attempt": 2,
            "event": "schedule",
        },
        "evaluator": {
            "source_sha": SOURCE_SHA,
            "workflow_checkout_sha": SOURCE_SHA,
            "working_tree_head_sha": "9" * 40,
            "checkout_matches_source": True,
            "loaded_files_match_source": True,
        },
        "execution_status": "completed",
        "started_at": "2026-10-06T10:00:20Z",
        "completed_at": "2026-10-06T10:00:28Z",
        "failure_code": None,
        "outcome": _outcome(mode),
    }
    for key, value in changes.items():
        document[key] = value
    return document


def _zip_document(document: dict, *, raw_json: bytes | None = None, members: list[tuple[str, bytes]] | None = None) -> bytes:
    json_bytes = raw_json if raw_json is not None else json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8") + b"\n"
    seal = f"{_hash(json_bytes)}  terminal-outcome.json\n".encode("ascii")
    chosen = members if members is not None else [(JSON_PATH, json_bytes), (SEAL_PATH, seal)]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in chosen:
            archive.writestr(name, payload)
    return output.getvalue()


def _context(mode: str = "reconcile-prs") -> dict:
    return {
        "repository": REPO,
        "workflow_id": 373700001,
        "workflow_path": EVIDENCE.WORKFLOW_PATH,
        "default_branch": "main",
        "run_id": RUN_ID,
        "run_attempt": 2,
        "event": "schedule",
        "head_sha": SOURCE_SHA,
        "mode": mode,
    }


def _source_contract(context: dict, *, source_bytes: dict[str, bytes] | None = None) -> dict:
    if source_bytes is None:
        source_bytes = {
            path: (ROOT / path).read_bytes()
            for path in EVIDENCE.EVALUATOR_SOURCE_PATHS
        }
    schema_bytes = source_bytes[EVIDENCE.SCHEMA_PATH]
    schema_sha = _hash(schema_bytes)
    return {
        "repository": context["repository"],
        "workflow_id": context["workflow_id"],
        "workflow_path": context["workflow_path"],
        "source_sha": context["head_sha"],
        "schema_path": EVIDENCE.SCHEMA_PATH,
        "schema_bytes": schema_bytes,
        "schema_sha256": schema_sha,
        "source_files": {path: _hash(raw) for path, raw in source_bytes.items()},
        "mode_steps": copy.deepcopy(EVIDENCE.MODE_STEPS),
    }


def _run(context: dict) -> dict:
    return {
        "id": int(context["run_id"]),
        "run_attempt": context["run_attempt"],
        "workflow_id": context["workflow_id"],
        "path": context["workflow_path"],
        "event": context["event"],
        "head_sha": context["head_sha"],
        "head_branch": context["default_branch"],
        "status": "completed",
        "conclusion": "failure",
        "created_at": "2026-10-06T10:00:00Z",
        "run_started_at": "2026-10-06T10:00:05Z",
        "repository": {"id": 48151623, "full_name": context["repository"]},
        "head_repository": {"id": 48151623, "full_name": context["repository"]},
    }


def _steps(mode: str, *, invocation_conclusion: str = "success", upload_conclusion: str = "success") -> list[dict]:
    contract = EVIDENCE.MODE_STEPS[mode]
    return [
        {"number": 1, "name": "Check out the current trusted default branch", "status": "completed", "conclusion": "success", "started_at": "2026-10-06T10:00:10Z", "completed_at": "2026-10-06T10:00:15Z"},
        {"number": 2, "name": contract["invocation_step"], "status": "completed", "conclusion": invocation_conclusion, "started_at": "2026-10-06T10:00:20Z", "completed_at": "2026-10-06T10:00:30Z"},
        {"number": 3, "name": contract["upload_step"], "status": "completed", "conclusion": upload_conclusion, "started_at": "2026-10-06T10:00:31Z", "completed_at": "2026-10-06T10:00:35Z"},
    ]


def _jobs(context: dict, *, invocation_conclusion: str = "success", upload_conclusion: str = "success") -> dict:
    endpoint = f"repos/{REPO}/actions/runs/{RUN_ID}/attempts/{context['run_attempt']}/jobs"
    return {
        "attempt_number": context["run_attempt"],
        "jobs_api_endpoint": endpoint,
        "job_count": 1,
        "jobs": [{
            "id": 773001,
            "run_id": int(RUN_ID),
            "run_attempt": context["run_attempt"],
            "head_sha": context["head_sha"],
            "head_branch": context["default_branch"],
            "name": "reconcile",
            "status": "completed",
            "conclusion": "failure",
            "started_at": "2026-10-06T10:00:10Z",
            "completed_at": "2026-10-06T10:00:35Z",
            "steps": _steps(context["mode"], invocation_conclusion=invocation_conclusion, upload_conclusion=upload_conclusion),
        }],
    }


def _artifact_inventory(context: dict, archive: bytes, *, created_at: str = "2026-10-06T10:00:33Z", expires_at: str = "2026-11-05T10:00:00Z") -> dict:
    workflow_run = {
        "id": int(RUN_ID),
        "head_sha": context["head_sha"],
        "head_branch": context["default_branch"],
        "repository_id": 48151623,
        "head_repository_id": 48151623,
    }
    row = {
        "id": int(ARTIFACT_ID),
        "name": f"canonical-update-promotion-terminal-{RUN_ID}-{context['run_attempt']}-{context['mode']}",
        "expired": False,
        "created_at": created_at,
        "expires_at": expires_at,
        "size_in_bytes": len(archive),
        "digest": f"sha256:{_hash(archive)}",
        "workflow_run": copy.deepcopy(workflow_run),
    }
    return {
        "run_id": RUN_ID,
        "total_count": 1,
        "artifacts": [copy.deepcopy(row)],
        "details_by_id": {ARTIFACT_ID: row},
    }


def _fixture(mode: str = "reconcile-prs", document: dict | None = None) -> dict:
    context = _context(mode)
    doc = copy.deepcopy(document) if document is not None else _document(mode)
    archive = _zip_document(doc)
    return {
        "archive_bytes": archive,
        "run": _run(context),
        "exact_attempt": context["run_attempt"],
        "jobs": _jobs(context),
        "artifact_inventory": _artifact_inventory(context, archive),
        "expected_context": context,
        "source_contract": _source_contract(context),
        "as_of": AS_OF,
    }


def _validate(**overrides) -> dict:
    args = _fixture()
    args.update(overrides)
    return EVIDENCE.validate_terminal_evidence(**args)


class TerminalEvidenceTests(unittest.TestCase):
    def test_exact_completed_mode_is_verified_even_if_later_c_step_failed(self) -> None:
        result = _validate()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["run_conclusion"], "failure")
        self.assertEqual(result["mode_step_conclusion"], "success")
        self.assertEqual(result["outcome"]["kind"], "reconcile-prs")
        self.assertEqual(result["current_applicability"]["status"], "not_applicable")

    def test_each_supported_mode_uses_its_exact_step_and_artifact_namespace(self) -> None:
        for mode in ("reconcile-prs", "refresh-owned-source", "recover-ready"):
            with self.subTest(mode=mode):
                fixture = _fixture(mode)
                result = EVIDENCE.validate_terminal_evidence(**fixture)
                self.assertEqual(result["status"], "verified")
                self.assertEqual(result["invocation"]["mode"], mode)
                self.assertEqual(result["artifact"]["name"], f"canonical-update-promotion-terminal-{RUN_ID}-2-{mode}")

    def test_real_git_tree_source_contract_uses_exact_production_blob_hashes(self) -> None:
        relative_paths = tuple(sorted(EVIDENCE.EVALUATOR_SOURCE_PATHS))
        self.assertEqual(len(relative_paths), 25)
        self.assertNotIn("scripts/validate-diagnostic-publication.py", relative_paths)
        working_bytes = {relative: (ROOT / relative).read_bytes() for relative in relative_paths}

        with tempfile.TemporaryDirectory(prefix="terminal-source-contract-") as temporary:
            tree = pathlib.Path(temporary)
            subprocess.run(
                ["git", "-c", "init.defaultBranch=main", "init", "--quiet"],
                cwd=tree, check=True, capture_output=True,
            )
            for relative, content in working_bytes.items():
                destination = tree.joinpath(*pathlib.PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            subprocess.run(["git", "add", "--", *relative_paths], cwd=tree, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Terminal Test", "-c", "user.email=terminal-test@example.invalid",
                 "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "source closure fixture"],
                cwd=tree, check=True, capture_output=True,
            )
            source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tree, text=True).strip()
            committed_bytes = {
                relative: subprocess.check_output(["git", "show", f"{source_sha}:{relative}"], cwd=tree)
                for relative in relative_paths
            }

        self.assertEqual(committed_bytes, working_bytes)
        context = _context()
        context["head_sha"] = source_sha
        document = _document()
        document["evaluator"]["source_sha"] = source_sha
        document["evaluator"]["workflow_checkout_sha"] = source_sha
        fixture = _fixture(document=document)
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context, source_bytes=committed_bytes)
        fixture["run"] = _run(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])

        result = EVIDENCE.validate_terminal_evidence(**fixture)

        self.assertEqual(result["status"], "verified")
        self.assertEqual(set(fixture["source_contract"]["source_files"]), set(relative_paths))
        self.assertEqual(
            fixture["source_contract"]["source_files"][EVIDENCE.SCHEMA_PATH],
            _hash(committed_bytes[EVIDENCE.SCHEMA_PATH]),
        )

    def test_recover_ready_already_canonical_joins_exact_current_subject(self) -> None:
        doc = _document("recover-ready")
        context = _context("recover-ready")
        current = {
            "main_identity": _main_identity(),
            "generations": [copy.deepcopy(_generation())],
        }
        fixture = _fixture("recover-ready", doc)
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        result = EVIDENCE.validate_terminal_evidence(**fixture, current_subject=current)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["current_applicability"]["status"], "current")
        self.assertEqual(result["current_applicability"]["matching_generations"], [GENERATION_ID])

    def test_historical_already_canonical_does_not_match_a_different_current_main(self) -> None:
        doc = _document("recover-ready")
        context = _context("recover-ready")
        fixture = _fixture("recover-ready", doc)
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        current = {
            "main_identity": {**_main_identity(), "revision": "7" * 40},
            "generations": [copy.deepcopy(_generation())],
        }
        result = EVIDENCE.validate_terminal_evidence(**fixture, current_subject=current)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["current_applicability"]["status"], "historical_only")

    def test_failed_terminal_is_verified_failure_not_noop(self) -> None:
        doc = _document(
            "reconcile-prs",
            execution_status="failed",
            failure_code="controlled_execution_failure",
            completed_at="2026-10-06T10:00:28Z",
            outcome=None,
        )
        fixture = _fixture("reconcile-prs", doc)
        fixture["jobs"] = _jobs(_context(), invocation_conclusion="failure")
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["execution_status"], "failed")
        self.assertIsNone(result["outcome"])
        self.assertEqual(result["failure_code"], "controlled_execution_failure")

    def test_started_terminal_record_is_not_a_completed_intake(self) -> None:
        doc = _document(
            "reconcile-prs",
            execution_status="started",
            completed_at=None,
            failure_code=None,
            outcome=None,
        )
        fixture = _fixture("reconcile-prs", doc)
        fixture["artifact_inventory"] = _artifact_inventory(fixture["expected_context"], fixture["archive_bytes"])
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason_code"], "terminal_execution_not_complete")

    def test_wrong_attempt_argument_and_rerun_snapshot_are_rejected(self) -> None:
        fixture = _fixture()
        fixture["exact_attempt"] = 1
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason_code"], "exact_attempt_argument_mismatch")

        fixture = _fixture()
        fixture["run"] = {**fixture["run"], "run_attempt": 3}
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["reason_code"], "exact_attempt_run_identity_mismatch")

    def test_wrong_repository_workflow_head_event_or_mode_is_rejected(self) -> None:
        for field, value in (
            ("repository", "Other/datapan-registry"),
            ("workflow_id", 999),
            ("workflow_path", ".github/workflows/other.yml"),
            ("head_sha", "8" * 40),
            ("event", "workflow_dispatch"),
            ("mode", "recover-ready"),
        ):
            with self.subTest(field=field):
                fixture = _fixture()
                fixture["expected_context"] = {**fixture["expected_context"], field: value}
                result = EVIDENCE.validate_terminal_evidence(**fixture)
                self.assertIn(result["status"], {"unavailable", "rejected"})

    def test_default_branch_is_required_and_native_branch_fields_must_match(self) -> None:
        fixture = _fixture()
        fixture["expected_context"].pop("default_branch")
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason_code"], "expected_invocation_incomplete")

        fixture = _fixture()
        fixture["expected_context"]["default_branch"] = "  "
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "expected_invocation_incomplete")

        fixture = _fixture()
        fixture["run"] = {**fixture["run"], "head_branch": None}
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "exact_attempt_run_identity_mismatch")

        fixture = _fixture()
        job = copy.deepcopy(fixture["jobs"]["jobs"][0])
        job["head_branch"] = None
        fixture["jobs"] = {**fixture["jobs"], "jobs": [job]}
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "exact_attempt_job_identity_mismatch")

        fixture = _fixture()
        inventory = copy.deepcopy(fixture["artifact_inventory"])
        inventory["artifacts"][0]["workflow_run"]["head_branch"] = None
        inventory["details_by_id"][ARTIFACT_ID]["workflow_run"]["head_branch"] = None
        fixture["artifact_inventory"] = inventory
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_artifact_repository_or_time_binding_mismatch")

    def test_local_invocation_is_not_native_evidence(self) -> None:
        fixture = _fixture()
        fixture["expected_context"] = {**fixture["expected_context"], "event": "local"}
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["status"], "unavailable")

    def test_unavailable_or_incomplete_native_inputs_fail_closed(self) -> None:
        fixture = _fixture()
        fixture["jobs"] = {**fixture["jobs"], "job_count": 2}
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason_code"], "exact_attempt_jobs_incomplete")

        fixture = _fixture()
        fixture["artifact_inventory"] = {**fixture["artifact_inventory"], "total_count": 2}
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason_code"], "artifact_inventory_incomplete")

        fixture = _fixture()
        fixture["artifact_inventory"] = {**fixture["artifact_inventory"], "details_by_id": {}}
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason_code"], "terminal_artifact_readback_missing")

    def test_wrong_jobs_endpoint_and_job_head_are_rejected(self) -> None:
        fixture = _fixture()
        fixture["jobs"] = {**fixture["jobs"], "jobs_api_endpoint": "repos/StatPan/datapan-registry/actions/runs/37200000001/jobs"}
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["status"], "unavailable")

        fixture = _fixture()
        row = copy.deepcopy(fixture["jobs"]["jobs"][0])
        row["head_sha"] = "8" * 40
        fixture["jobs"] = {**fixture["jobs"], "jobs": [row]}
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "exact_attempt_job_identity_mismatch")

    def test_wrong_or_missing_mode_step_and_upload_failure_are_not_accepted(self) -> None:
        fixture = _fixture()
        job = copy.deepcopy(fixture["jobs"]["jobs"][0])
        job["steps"][1]["name"] = "Different step"
        fixture["jobs"] = {**fixture["jobs"], "jobs": [job]}
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["status"], "unavailable")

        fixture = _fixture()
        job = copy.deepcopy(fixture["jobs"]["jobs"][0])
        job["steps"][2]["conclusion"] = "failure"
        fixture["jobs"] = {**fixture["jobs"], "jobs": [job]}
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_mode_step_timing_invalid")

    def test_artifact_must_be_unique_unexpired_and_in_exact_upload_interval(self) -> None:
        fixture = _fixture()
        duplicate = copy.deepcopy(fixture["artifact_inventory"]["artifacts"][0])
        duplicate["id"] = 9930002
        fixture["artifact_inventory"] = {
            **fixture["artifact_inventory"],
            "total_count": 2,
            "artifacts": [fixture["artifact_inventory"]["artifacts"][0], duplicate],
            "details_by_id": {**fixture["artifact_inventory"]["details_by_id"], "9930002": duplicate},
        }
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["reason_code"], "terminal_artifact_name_not_unique")

        fixture = _fixture()
        fixture["artifact_inventory"] = _artifact_inventory(
            fixture["expected_context"], fixture["archive_bytes"], expires_at="2026-10-06T10:30:00Z",
        )
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason_code"], "terminal_artifact_expired_or_time_invalid")

        fixture = _fixture()
        fixture["artifact_inventory"] = _artifact_inventory(
            fixture["expected_context"], fixture["archive_bytes"], created_at="2026-10-06T10:00:29Z",
        )
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["reason_code"], "terminal_artifact_repository_or_time_binding_mismatch")

    def test_artifact_list_detail_run_and_archive_digests_are_bound(self) -> None:
        fixture = _fixture()
        rows = copy.deepcopy(fixture["artifact_inventory"]["artifacts"])
        rows[0]["workflow_run"]["head_sha"] = "8" * 40
        fixture["artifact_inventory"] = {**fixture["artifact_inventory"], "artifacts": rows}
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_artifact_list_detail_mismatch")

        fixture = _fixture()
        detail = copy.deepcopy(fixture["artifact_inventory"]["details_by_id"][ARTIFACT_ID])
        detail["workflow_run"]["id"] = 37200000002
        fixture["artifact_inventory"] = {**fixture["artifact_inventory"], "details_by_id": {ARTIFACT_ID: detail}}
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_artifact_list_detail_mismatch")

        fixture = _fixture()
        fixture["archive_bytes"] += b"x"
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_artifact_archive_size_mismatch")

        fixture = _fixture()
        fixture["artifact_inventory"] = _artifact_inventory(fixture["expected_context"], fixture["archive_bytes"])
        detail = copy.deepcopy(fixture["artifact_inventory"]["details_by_id"][ARTIFACT_ID])
        detail["digest"] = "sha256:" + "0" * 64
        fixture["artifact_inventory"] = {**fixture["artifact_inventory"], "details_by_id": {ARTIFACT_ID: detail}}
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_artifact_list_detail_mismatch")

    def test_zip_member_set_seal_duplicate_keys_and_canonical_bytes_are_strict(self) -> None:
        fixture = _fixture()
        doc = _document()
        extra = [(JSON_PATH, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode() + b"\n"),
                 (SEAL_PATH, b"0" * 64 + b"  terminal-outcome.json\n"),
                 ("other.txt", b"x")]
        fixture["archive_bytes"] = _zip_document(doc, members=extra)
        fixture["artifact_inventory"] = _artifact_inventory(fixture["expected_context"], fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_archive_members_invalid")

        fixture = _fixture()
        raw = b'{"schema_version":"x","schema_version":"y"}'
        fixture["archive_bytes"] = _zip_document(_document(), raw_json=raw)
        fixture["artifact_inventory"] = _artifact_inventory(fixture["expected_context"], fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_document_json_invalid")

        fixture = _fixture()
        pretty = json.dumps(_document(), ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
        fixture["archive_bytes"] = _zip_document(_document(), raw_json=pretty)
        fixture["artifact_inventory"] = _artifact_inventory(fixture["expected_context"], fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_document_not_canonical_json")

        fixture = _fixture()
        damaged = bytearray(fixture["archive_bytes"])
        damaged[-1] ^= 1
        fixture["archive_bytes"] = bytes(damaged)
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["status"], "rejected")

    def test_schema_source_contract_digest_and_evaluator_claim_are_not_self_authenticating(self) -> None:
        fixture = _fixture()
        fixture["source_contract"] = {**fixture["source_contract"], "source_sha": "8" * 40}
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["status"], "unavailable")

        fixture = _fixture()
        contract = copy.deepcopy(fixture["source_contract"])
        contract["schema_bytes"] += b" "
        fixture["source_contract"] = contract
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "source_contract_schema_digest_mismatch")

        doc = _document()
        doc["evaluator"]["source_sha"] = "8" * 40
        fixture = _fixture(document=doc)
        fixture["artifact_inventory"] = _artifact_inventory(fixture["expected_context"], fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_document_source_or_invocation_mismatch")

        doc = _document()
        doc["evaluator"]["loaded_files_match_source"] = False
        fixture = _fixture(document=doc)
        fixture["artifact_inventory"] = _artifact_inventory(fixture["expected_context"], fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_document_source_or_invocation_mismatch")

    def test_external_schema_reference_forms_are_rejected_before_any_resolver_call(self) -> None:
        import jsonschema

        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            with self.subTest(keyword=keyword):
                fixture = _fixture()
                contract = copy.deepcopy(fixture["source_contract"])
                schema = json.loads(contract["schema_bytes"])
                schema["x-review-probe"] = {keyword: "https://invalid.example/remote-schema.json"}
                schema_bytes = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
                schema_sha = _hash(schema_bytes)
                contract["schema_bytes"] = schema_bytes
                contract["schema_sha256"] = schema_sha
                contract["source_files"][EVIDENCE.SCHEMA_PATH] = schema_sha
                fixture["source_contract"] = contract

                with mock.patch.object(EVIDENCE, "_deny_external_schema_resource", side_effect=AssertionError("external resolver reached")) as resolver:
                    result = EVIDENCE.validate_terminal_evidence(**fixture)

                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["reason_code"], "schema_external_reference_forbidden")
                resolver.assert_not_called()

        fixture = _fixture()
        contract = copy.deepcopy(fixture["source_contract"])
        schema = json.loads(contract["schema_bytes"])
        schema["x-relative-scope-probe"] = {
            "$id": "https://invalid.example/nested-resource.json",
            "$ref": "#/properties/schema_version",
        }
        schema_bytes = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        schema_sha = _hash(schema_bytes)
        contract["schema_bytes"] = schema_bytes
        contract["schema_sha256"] = schema_sha
        contract["source_files"][EVIDENCE.SCHEMA_PATH] = schema_sha
        fixture["source_contract"] = contract
        with mock.patch.object(EVIDENCE, "_deny_external_schema_resource", side_effect=AssertionError("external resolver reached")) as resolver:
            result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["reason_code"], "schema_external_reference_forbidden")
        resolver.assert_not_called()

    def test_offline_schema_resolver_refuses_remote_fetch_as_defense_in_depth(self) -> None:
        import jsonschema

        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://invalid.example/root.json",
            "type": "object",
            "properties": {
                "child": {"$ref": "https://invalid.example/child.json"},
            },
        }
        url = "https://invalid.example/child.json"
        with mock.patch.object(EVIDENCE, "_deny_external_schema_resource", wraps=EVIDENCE._deny_external_schema_resource) as resolver:
            validator = EVIDENCE._offline_schema_validator(schema, jsonschema)
            with self.assertRaises(Exception):
                validator.validate({"child": {}})
        resolver.assert_called_once_with(url)

    def test_outcome_semantics_reject_mixed_or_truncated_all_canonical_claim(self) -> None:
        doc = _document("recover-ready")
        mixed = copy.deepcopy(doc["outcome"])
        represented = _generation("already_represented")
        represented["generation_id"] = "8" * 64
        represented["checkpoint_sha256"] = "9" * 64
        represented["reason_code"] = "active_promotion_payload_exists"
        mixed["generations"].append(represented)
        mixed["generation_count"] = 2
        doc["outcome"] = mixed
        fixture = _fixture("recover-ready", doc)
        context = _context("recover-ready")
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_all_canonical_claim_invalid")

    def test_selected_generation_and_blocked_noop_rows_are_semantically_bound(self) -> None:
        doc = _document("recover-ready")
        selected = copy.deepcopy(doc["outcome"])
        selected.update({
            "status": "candidate_preparation_returned",
            "candidate_available": True,
            "selected_generation_id": GENERATION_ID,
            "preparation_returned": True,
            "generations": [_generation("selected")],
        })
        doc["outcome"] = selected
        fixture = _fixture("recover-ready", doc)
        context = _context("recover-ready")
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["status"], "verified")

        selected["selected_generation_id"] = "8" * 64
        doc["outcome"] = selected
        fixture = _fixture("recover-ready", doc)
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_selected_generation_mismatch")

        blocked = _generation("blocked")
        blocked["reason_code"] = "processor_checkpoint_unavailable"
        no_candidate = {
            "kind": "recover-ready",
            "status": "no-eligible-ready-processor-bundle",
            "candidate_available": False,
            "evaluated_main": _main_identity(),
            "selected_generation_id": None,
            "preparation_returned": False,
            "generation_count": 1,
            "generation_results_truncated": False,
            "generations": [blocked],
        }
        doc = _document("recover-ready")
        doc["outcome"] = no_candidate
        fixture = _fixture("recover-ready", doc)
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["status"], "verified")

    def test_no_eligible_blocked_history_without_main_is_retained_historically(self) -> None:
        blocked = _generation("blocked")
        blocked["reason_code"] = "processor_bundle_or_input_contract_incompatible"
        doc = _document("recover-ready")
        doc["outcome"] = {
            "kind": "recover-ready",
            "status": "no-eligible-ready-processor-bundle",
            "candidate_available": False,
            "evaluated_main": None,
            "selected_generation_id": None,
            "preparation_returned": False,
            "generation_count": 1,
            "generation_results_truncated": False,
            "generations": [blocked],
        }
        fixture = _fixture("recover-ready", doc)
        context = _context("recover-ready")
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        current = {"main_identity": _main_identity(), "generations": [blocked]}

        result = EVIDENCE.validate_terminal_evidence(**fixture, current_subject=current)

        self.assertEqual(result["status"], "verified")
        self.assertIsNone(result["outcome"]["evaluated_main"])
        self.assertEqual(result["current_applicability"]["status"], "historical_only")
        self.assertEqual(result["current_applicability"]["matching_generations"], [])

        doc["outcome"]["generations"][0]["status"] = "already_represented"
        doc["outcome"]["generations"][0]["reason_code"] = "active_promotion_payload_exists"
        fixture = _fixture("recover-ready", doc)
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        self.assertEqual(
            EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"],
            "terminal_no_eligible_rows_without_main_invalid",
        )

    def test_truncated_selected_history_is_retained_without_current_proof(self) -> None:
        rows = []
        for index in range(64):
            row = _generation("blocked")
            row["generation_id"] = f"{index + 1:064x}"
            row["checkpoint_sha256"] = f"{index + 100:064x}"
            row["reason_code"] = "processor_checkpoint_unavailable"
            rows.append(row)
        selected_id = f"{65:064x}"  # Selected generation lies after the returned 64-row window.
        doc = _document("recover-ready")
        doc["outcome"] = {
            "kind": "recover-ready",
            "status": "candidate_preparation_returned",
            "candidate_available": True,
            "evaluated_main": _main_identity(),
            "selected_generation_id": selected_id,
            "preparation_returned": True,
            "generation_count": 65,
            "generation_results_truncated": True,
            "generations": rows,
        }
        fixture = _fixture("recover-ready", doc)
        context = _context("recover-ready")
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        current = {"main_identity": _main_identity(), "generations": rows}

        result = EVIDENCE.validate_terminal_evidence(**fixture, current_subject=current)

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["outcome"]["selected_generation_id"], selected_id)
        self.assertEqual(len(result["outcome"]["generations"]), 64)
        self.assertEqual(result["current_applicability"]["status"], "historical_only")
        self.assertEqual(result["current_applicability"]["reason_code"], "terminal_generation_results_truncated")
        self.assertEqual(result["current_applicability"]["matching_generations"], [])

        # Prefix truncation occurs before the selected row; matching a retained
        # blocked row would contradict the producer's stop-at-selection order.
        doc["outcome"]["selected_generation_id"] = rows[0]["generation_id"]
        fixture = _fixture("recover-ready", doc)
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        result = EVIDENCE.validate_terminal_evidence(**fixture, current_subject=current)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason_code"], "terminal_selected_generation_mismatch")

    def test_multiple_selected_rows_are_rejected_even_if_one_matches_selected_id(self) -> None:
        first = _generation("selected")
        second = _generation("selected")
        second["generation_id"] = "8" * 64
        second["checkpoint_sha256"] = "9" * 64
        doc = _document("recover-ready")
        doc["outcome"] = {
            "kind": "recover-ready",
            "status": "candidate_preparation_returned",
            "candidate_available": True,
            "evaluated_main": _main_identity(),
            "selected_generation_id": GENERATION_ID,
            "preparation_returned": True,
            "generation_count": 2,
            "generation_results_truncated": False,
            "generations": [first, second],
        }
        fixture = _fixture("recover-ready", doc)
        context = _context("recover-ready")
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])

        result = EVIDENCE.validate_terminal_evidence(**fixture)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason_code"], "terminal_selected_generation_mismatch")

    def test_empty_no_undelivered_is_not_already_canonical(self) -> None:
        outcome = {
            "kind": "recover-ready",
            "status": "no-undelivered-ready-processor-bundle",
            "candidate_available": False,
            "evaluated_main": None,
            "selected_generation_id": None,
            "preparation_returned": False,
            "generation_count": 0,
            "generation_results_truncated": False,
            "generations": [],
        }
        doc = _document("recover-ready")
        doc["outcome"] = outcome
        fixture = _fixture("recover-ready", doc)
        context = _context("recover-ready")
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        result = EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(result["status"], "verified")
        self.assertIsNone(result["outcome"]["evaluated_main"])

        outcome["evaluated_main"] = _main_identity()
        doc["outcome"] = outcome
        fixture = _fixture("recover-ready", doc)
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_empty_recovery_claim_invalid")

        doc = _document("recover-ready")
        truncated = copy.deepcopy(doc["outcome"])
        truncated["generation_count"] = 65
        truncated["generation_results_truncated"] = True
        truncated_rows = []
        for index in range(64):
            row = _generation()
            row["generation_id"] = f"{index + 1:064x}"
            row["checkpoint_sha256"] = f"{index + 101:064x}"
            truncated_rows.append(row)
        truncated["generations"] = truncated_rows
        doc["outcome"] = truncated
        fixture = _fixture("recover-ready", doc)
        context = _context("recover-ready")
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_all_canonical_claim_invalid")

    def test_nonrecover_mode_cannot_claim_recovery_or_current_main(self) -> None:
        doc = _document("reconcile-prs")
        doc["outcome"]["evaluated_main"] = _main_identity()
        fixture = _fixture(document=doc)
        fixture["artifact_inventory"] = _artifact_inventory(fixture["expected_context"], fixture["archive_bytes"])
        self.assertEqual(EVIDENCE.validate_terminal_evidence(**fixture)["reason_code"], "terminal_nonrecovery_outcome_invalid")

    def test_incomplete_current_subject_only_limits_applicability(self) -> None:
        fixture = _fixture("recover-ready", _document("recover-ready"))
        context = _context("recover-ready")
        fixture["expected_context"] = context
        fixture["source_contract"] = _source_contract(context)
        fixture["jobs"] = _jobs(context)
        fixture["artifact_inventory"] = _artifact_inventory(context, fixture["archive_bytes"])
        result = EVIDENCE.validate_terminal_evidence(**fixture, current_subject={})
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["current_applicability"]["status"], "unavailable")

    def test_input_snapshots_are_not_mutated(self) -> None:
        fixture = _fixture()
        before = copy.deepcopy(fixture)
        EVIDENCE.validate_terminal_evidence(**fixture)
        self.assertEqual(fixture, before)


if __name__ == "__main__":
    unittest.main()
