from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

from scripts import upstream_catalogue_cache_source as SOURCE
from scripts import upstream_catalogue_handoff as HANDOFF


NOW = dt.datetime(2026, 10, 7, 1, 0, tzinfo=dt.timezone.utc)
EXPIRY = "2026-11-04T07:02:45Z"
REPOSITORY = "StatPan/datapan-registry"
DEFAULT_BRANCH = "main"


def zip_bytes(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    return output.getvalue()


def digest_entry(name: str, raw: bytes) -> dict[str, object]:
    return {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def artifact(
    *, run_id: str, artifact_id: str, name: str, raw: bytes, head_sha: str,
) -> dict[str, object]:
    return {
        "id": int(artifact_id),
        "name": name,
        "size_in_bytes": len(raw),
        "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "expired": False,
        "created_at": "2026-10-05T07:02:45Z",
        "updated_at": "2026-10-05T07:03:00Z",
        "expires_at": EXPIRY,
        "workflow_run": {
            "id": int(run_id),
            "head_sha": head_sha,
            "head_branch": DEFAULT_BRANCH,
            "repository_id": 1,
            "head_repository_id": 1,
        },
    }


def run(
    *, run_id: str, attempt: int, head_sha: str, workflow_path: str,
) -> dict[str, object]:
    return {
        "id": int(run_id),
        "run_attempt": attempt,
        "path": workflow_path,
        "head_branch": DEFAULT_BRANCH,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "success",
        "event": "schedule",
        "repository": {"id": 1, "full_name": REPOSITORY},
        "head_repository": {"id": 1, "full_name": REPOSITORY},
    }


class FakeReader:
    def __init__(
        self, responses: dict[str, object], archives: dict[str, bytes],
        source_files: dict[tuple[str, str], bytes] | None = None,
    ) -> None:
        self.responses = responses
        self.archives = archives
        self.source_files = source_files or {}
        self.calls: list[tuple[str, str]] = []

    def json(self, endpoint: str):
        self.calls.append(("json", endpoint))
        return copy.deepcopy(self.responses[endpoint])

    def archive(self, repository: str, artifact_id: str, *, expected_bytes: int) -> bytes:
        self.calls.append(("archive", artifact_id))
        self.assert_repository(repository)
        raw = self.archives[artifact_id]
        if len(raw) != expected_bytes:
            raise AssertionError("fixture archive size differs")
        return raw

    def source_file(
        self, repository: str, head_sha: str, path: str, *, maximum_bytes: int,
    ) -> bytes:
        self.calls.append(("source_file", path))
        self.assert_repository(repository)
        raw = self.source_files[(head_sha, path)]
        if not 0 < len(raw) <= maximum_bytes:
            raise AssertionError("fixture source size differs")
        return raw

    @staticmethod
    def assert_repository(repository: str) -> None:
        if repository != REPOSITORY:
            raise AssertionError("unexpected repository")


class CacheSourceTest(unittest.TestCase):
    def test_native_snapshot_factory_requires_the_canonical_control_singleton(self) -> None:
        deadline = time.monotonic() + 2400.0
        with mock.patch.dict(sys.modules, {"canonical_publication_native": None}):
            with self.assertRaisesRegex(
                SOURCE.CacheSourceUnavailable,
                "canonical publication Native singleton is unavailable",
            ):
                SOURCE.LocalGitSourceReader.from_native_snapshot(
                    object(), deadline_monotonic=deadline,
                )

    def test_local_reader_keeps_the_standalone_deadline_ceiling(self) -> None:
        with self.assertRaisesRegex(
            SOURCE.CacheSourceError,
            "deadline exceeds the whole-attempt bound",
        ):
            SOURCE.LocalGitSourceReader(
                cwd=pathlib.Path.cwd(),
                deadline_monotonic=time.monotonic() + 2400.0,
            )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.state = self.root / "state"
        self.output = self.root / "restored"
        self.owner_generation = "7" * 64
        self.legacy_processor = b"historical processor source\n"
        self.legacy_handoff = b"historical handoff source\n"
        self.legacy_revision = SOURCE.sha256_bytes(SOURCE.canonical_json({
            "processor_script_sha256": SOURCE.sha256_bytes(self.legacy_processor),
            "collector_handoff_helper_sha256": SOURCE.sha256_bytes(self.legacy_handoff),
        }))
        self.modern_sources = {
            "scripts/process-upstream-catalogue-candidate.py": self.legacy_processor,
            "scripts/upstream_catalogue_handoff.py": self.legacy_handoff,
            "scripts/upstream_catalogue_cache_source.py": b"modern cache source\n",
            "scripts/seoul_oa109_operation_declaration.py": b"modern declaration helper\n",
            "scripts/generate-seoul-oa109-subject-snapshot.py": b"modern snapshot generator\n",
            "contracts/provider-operation-declarations/data-go-kr-15056854-oa-109-search-last-train-time.v1.json": b"{}\n",
            "contracts/provider-operation-declarations/data-go-kr-15056854-historical-subject-0085.v1.json": b"{}\n",
        }
        fields = (
            "processor_script_sha256", "collector_handoff_helper_sha256",
            "cache_source_helper_sha256", "seoul_operation_declaration_helper_sha256",
            "seoul_historical_subject_snapshot_generator_sha256",
            "seoul_operation_declaration_sha256", "seoul_historical_subject_snapshot_sha256",
        )
        self.modern_revision = SOURCE.sha256_bytes(SOURCE.canonical_json({
            field: SOURCE.sha256_bytes(raw)
            for field, raw in zip(fields, self.modern_sources.values(), strict=True)
        }))
        self.candidate = json.dumps([
            {
                "id": "1", "provider": "data.go.kr", "type": "LINK", "operations": [],
                "source": {"system": "data.go.kr", "url": "https://www.data.go.kr/data/1/openapi.do", "raw": {"type": "LINK"}},
            },
        ], sort_keys=True, separators=(",", ":")).encode() + b"\n"
        self.evidence = b'{"schema_version":"datapan.catalogue-enrichment-evidence.v1"}\n'
        self.admission = {
            "admission_id": HANDOFF.admission_id("100", 1, "a" * 64),
            "producer_run_id": "100",
            "run_attempt": 1,
            "head_sha": "1" * 40,
            "run_started_at": "2026-10-01T00:00:00Z",
            "artifact_id": "101",
            "artifact_name": "upstream-catalog-refresh-100",
            "artifact_expires_at": EXPIRY,
            "artifact_digest_sha256": "2" * 64,
            "artifact_size_bytes": 100,
            "refresh_evidence_sha256": "a" * 64,
            "observed_at": "2026-10-01T00:00:10Z",
            "generation_id": "6" * 64,
            "candidate_sha256": hashlib.sha256(self.candidate).hexdigest(),
            "admitted_at": "2026-10-01T00:01:00Z",
        }
        HANDOFF.validate_admission_row(self.admission)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_native_handoff_loader_uses_only_exact_authenticated_singleton(self) -> None:
        handoff_name = "upstream_catalogue_cache_source_handoff"
        native_name = "canonical_publication_native"
        previous_handoff = sys.modules.get(handoff_name)
        previous_native = sys.modules.get(native_name)
        previous_cached = SOURCE._HANDOFF
        helper_marker_names = (
            "__datapan_cache_source_helper_sha256__",
            "__datapan_cache_source_helper_operation_id__",
            "__datapan_cache_source_snapshot_proxy__",
            "__datapan_cache_source_deadline_monotonic__",
            "__datapan_cache_source_handoff_sha256__",
            "__datapan_cache_source_handoff_operation_id__",
            "__datapan_cache_source_handoff_path__",
            "__datapan_cache_source_handoff_head_sha__",
        )
        previous_helper_markers = {
            name: SOURCE.__dict__.get(name) for name in helper_marker_names
        }
        handoff_markers = {
            "__datapan_cache_source_handoff_sha256__": "a" * 64,
            "__datapan_cache_source_handoff_operation_id__": "operation-1",
            "__datapan_cache_source_handoff_path__": (
                "scripts/upstream_catalogue_handoff.py"
            ),
            "__datapan_cache_source_handoff_head_sha__": "b" * 40,
        }
        binding = (
            (
                "StatPan/datapan-registry", "b" * 40,
                "scripts/upstream_catalogue_handoff.py", "d" * 64,
            ),
            (
                "scripts/upstream_catalogue_handoff.py", "a" * 64,
                "operation-1",
            ),
        )
        self.assertNotEqual(binding[0][3], binding[1][1])
        proxy = object()
        deadline = time.monotonic() + 1200.0
        handoff = SOURCE.types.ModuleType(handoff_name)
        handoff.__dict__.update(handoff_markers)
        SOURCE.__dict__["__datapan_cache_source_helper_sha256__"] = "c" * 64
        SOURCE.__dict__["__datapan_cache_source_helper_operation_id__"] = "operation-1"
        SOURCE.__dict__["__datapan_cache_source_snapshot_proxy__"] = proxy
        SOURCE.__dict__["__datapan_cache_source_deadline_monotonic__"] = deadline
        SOURCE.__dict__.update(handoff_markers)
        native = SOURCE.types.ModuleType(native_name)
        native.__dict__["expected_binding"] = binding
        exec(
            "def require_verified_cache_source_handoff_binding(*, helper_module, "
            "helper_snapshot_proxy, deadline_monotonic):\n"
            "    assert helper_module.__name__ == 'scripts.upstream_catalogue_cache_source'\n"
            "    assert helper_snapshot_proxy is helper_module.__datapan_cache_source_snapshot_proxy__\n"
            "    assert deadline_monotonic == helper_module.__datapan_cache_source_deadline_monotonic__\n"
            "    return expected_binding\n",
            native.__dict__,
        )
        sys.modules[native_name] = native
        sys.modules[handoff_name] = handoff
        SOURCE._HANDOFF = HANDOFF
        try:
            with mock.patch.object(
                SOURCE.importlib.util, "spec_from_file_location",
                side_effect=AssertionError("native mode must not load a local path"),
            ):
                self.assertIs(SOURCE._load_handoff(), handoff)
                handoff.__dict__["__datapan_cache_source_handoff_path__"] = (
                    "scripts/untrusted_handoff.py"
                )
                with self.assertRaisesRegex(
                    SOURCE.CacheSourceError, "authenticated module binding is invalid",
                ):
                    SOURCE._load_handoff()
                handoff.__dict__.update(handoff_markers)
                SOURCE.__dict__.pop("__datapan_cache_source_helper_operation_id__")
                with self.assertRaisesRegex(
                    SOURCE.CacheSourceError, "authenticated helper binding is invalid",
                ):
                    SOURCE._load_handoff()
                SOURCE.__dict__["__datapan_cache_source_helper_operation_id__"] = "operation-1"
                native.__dict__.pop("require_verified_cache_source_handoff_binding")
                with self.assertRaisesRegex(
                    SOURCE.CacheSourceUnavailable, "Native validator is unavailable",
                ):
                    SOURCE._load_handoff()
        finally:
            SOURCE._HANDOFF = previous_cached
            for name, value in previous_helper_markers.items():
                if value is None:
                    SOURCE.__dict__.pop(name, None)
                else:
                    SOURCE.__dict__[name] = value
            if previous_handoff is None:
                sys.modules.pop(handoff_name, None)
            else:
                sys.modules[handoff_name] = previous_handoff
            if previous_native is None:
                sys.modules.pop(native_name, None)
            else:
                sys.modules[native_name] = previous_native

    def context_bytes(self) -> bytes:
        candidate_path = self.root / SOURCE.ORIGINAL_CANDIDATE_MEMBER
        candidate_path.write_bytes(self.candidate)
        value = SOURCE.build_context(
            owner_generation_id=self.owner_generation,
            source_id="data_go_kr",
            source_scope="aggregate_supported_catalog",
            original_admission_ref=self.admission,
            candidate_path=candidate_path,
        )
        return SOURCE.canonical_json(value) + b"\n"

    def install_state(
        self, outputs: list[dict[str, object]], *, run_id: str = "200",
        artifact_id: str = "201", contract: str = "modern",
    ) -> dict[str, object]:
        generation_inputs = {
            "candidate_sha256": hashlib.sha256(self.candidate).hexdigest(),
            "generator_revision": self.legacy_revision if contract == "legacy" else self.modern_revision,
        }
        if contract == "modern":
            generation_inputs["cache_source_profile"] = SOURCE.CACHE_SOURCE_PROFILE
        self.owner_generation = SOURCE.sha256_bytes(SOURCE.canonical_json(generation_inputs))
        checkpoint = {
            "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
            "source_id": "data_go_kr",
            "source_scope": "aggregate_supported_catalog",
            "generation_id": self.owner_generation,
            "generation_inputs": generation_inputs,
            "last_observation": {
                "producer_run_id": self.admission["producer_run_id"],
                "refresh_evidence_sha256": self.admission["refresh_evidence_sha256"],
                "observed_at": self.admission["observed_at"],
            },
            "last_progress_at": "2026-10-05T07:02:45Z",
            "input_artifacts": [{
                "run_id": self.admission["producer_run_id"],
                "name": self.admission["artifact_name"],
                "artifact_id": self.admission["artifact_id"],
                "expires_at": self.admission["artifact_expires_at"],
                "candidate_sha256": self.admission["candidate_sha256"],
                "evidence_sha256": self.admission["refresh_evidence_sha256"],
                "diff_sha256": "b" * 64,
            }],
            "output_digests": outputs,
            "output_artifact": {
                "repository": REPOSITORY,
                "run_id": run_id,
                "name": f"upstream-catalogue-processing-{run_id}-1",
                "artifact_id": artifact_id,
                "expires_at": EXPIRY,
                "bundle_manifest_sha256": SOURCE.sha256_bytes(SOURCE.canonical_json(outputs)),
            },
        }
        checkpoint["checkpoint_sha256"] = SOURCE.sha256_bytes(SOURCE.canonical_json(checkpoint))
        generation_dir = self.state / "sources/data_go_kr/generations"
        generation_dir.mkdir(parents=True, exist_ok=True)
        (generation_dir / f"{self.owner_generation}.json").write_bytes(
            SOURCE.canonical_json(checkpoint) + b"\n",
        )
        index = {
            "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
            "generations": [{"generation_id": self.owner_generation}],
            "detail_queue_cursor": 0,
            "detail_retry_state": {},
            "collector_handoff": HANDOFF.empty_ledger(),
        }
        HANDOFF.add_admission(
            index, self.admission, now="2026-10-07T00:00:00Z",
        )
        (self.state / "sources/data_go_kr/index.json").write_bytes(
            SOURCE.canonical_json(index) + b"\n",
        )
        return checkpoint

    def b_reader(
        self, archive_raw: bytes, *, run_id: str = "200", artifact_id: str = "201",
        extra: dict[str, object] | None = None,
    ) -> FakeReader:
        name = f"upstream-catalogue-processing-{run_id}-1"
        metadata = artifact(
            run_id=run_id, artifact_id=artifact_id, name=name, raw=archive_raw,
            head_sha="3" * 40,
        )
        responses: dict[str, object] = {
            f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100": {
                "artifacts": [copy.deepcopy(metadata)],
            },
            f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}": {
                **copy.deepcopy(metadata), "archive_download_url": "ignored",
            },
            f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/1": run(
                run_id=run_id, attempt=1, head_sha="3" * 40,
                workflow_path=".github/workflows/upstream-catalogue-process.yml",
            ),
        }
        if extra:
            responses.update(extra)
        source_files = {
            ("3" * 40, "scripts/process-upstream-catalogue-candidate.py"): self.legacy_processor,
            ("3" * 40, "scripts/upstream_catalogue_handoff.py"): self.legacy_handoff,
            **{("3" * 40, path): raw for path, raw in self.modern_sources.items()},
        }
        return FakeReader(responses, {artifact_id: archive_raw}, source_files)

    def test_modern_self_contained_restore_never_reads_original_a_artifact(self) -> None:
        self.admission["artifact_expires_at"] = "2026-10-06T00:00:00Z"
        self.install_state([], contract="modern")
        context = self.context_bytes()
        members = {
            SOURCE.ENRICHMENT_MEMBER: self.evidence,
            SOURCE.ORIGINAL_CANDIDATE_MEMBER: self.candidate,
            SOURCE.CONTEXT_MEMBER: context,
        }
        outputs = [digest_entry(name, raw) for name, raw in members.items()]
        checkpoint = self.install_state(outputs, contract="modern")
        receipt = SOURCE.canonical_json(checkpoint) + b"\n"
        archive_raw = zip_bytes({**members, SOURCE.CHECKPOINT_RECEIPT_MEMBER: receipt})
        reader = self.b_reader(archive_raw)

        result = SOURCE.restore_selected_cache(
            state_dir=self.state,
            target_generation_id=self.owner_generation,
            repository=REPOSITORY,
            default_branch=DEFAULT_BRANCH,
            output_dir=self.output,
            reader=reader,
            now=NOW,
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["provenance"], "modern")
        self.assertEqual((self.output / SOURCE.ORIGINAL_CANDIDATE_MEMBER).read_bytes(), self.candidate)
        self.assertFalse(any("/actions/runs/100/" in endpoint for kind, endpoint in reader.calls if kind == "json"))
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "producer contract is unauthenticated"):
            SOURCE.load_historical_cache_source(
                self.output / SOURCE.CONTEXT_MEMBER,
                owner_checkpoint=checkpoint,
                state_index=json.loads((self.state / "sources/data_go_kr/index.json").read_text()),
            )
        owner_contract = SOURCE.authenticate_owner_contract(
            checkpoint,
            repository=REPOSITORY,
            default_branch=DEFAULT_BRANCH,
            reader=reader,
        )
        capability = SOURCE.load_historical_cache_source(
            self.output / SOURCE.CONTEXT_MEMBER,
            owner_checkpoint=checkpoint,
            state_index=json.loads((self.state / "sources/data_go_kr/index.json").read_text()),
            owner_contract=owner_contract,
        )
        self.assertEqual(capability.row("1")["id"], "1")

    def test_cache_selection_prefers_exact_target_and_never_substitutes_foreign_owner(self) -> None:
        context = self.context_bytes()
        members = {
            SOURCE.ENRICHMENT_MEMBER: self.evidence,
            SOURCE.ORIGINAL_CANDIDATE_MEMBER: self.candidate,
            SOURCE.CONTEXT_MEMBER: context,
        }
        outputs = [digest_entry(name, raw) for name, raw in members.items()]
        target = self.install_state(outputs)
        target_id = str(target["generation_id"])

        index_path = self.state / "sources/data_go_kr/index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        foreign = copy.deepcopy(target)
        foreign_inputs = copy.deepcopy(foreign["generation_inputs"])
        foreign_inputs["candidate_sha256"] = "c" * 64
        foreign_id = SOURCE.sha256_bytes(SOURCE.canonical_json(foreign_inputs))
        foreign["generation_inputs"] = foreign_inputs
        foreign["generation_id"] = foreign_id
        foreign["last_progress_at"] = "2026-10-06T12:00:00Z"
        foreign["output_artifact"]["run_id"] = "300"
        foreign["output_artifact"]["artifact_id"] = "301"
        foreign["output_artifact"]["name"] = "upstream-catalogue-processing-300-1"
        foreign.pop("checkpoint_sha256")
        foreign["checkpoint_sha256"] = SOURCE.sha256_bytes(SOURCE.canonical_json(foreign))
        generation_dir = self.state / "sources/data_go_kr/generations"
        (generation_dir / f"{foreign_id}.json").write_bytes(
            SOURCE.canonical_json(foreign) + b"\n",
        )
        index["generations"].append({"generation_id": foreign_id})
        index_path.write_bytes(SOURCE.canonical_json(index) + b"\n")

        selected_exact = SOURCE.select_cache_owner(
            self.state, target_generation_id=target_id, now=NOW,
        )
        selected_blank = SOURCE.select_cache_owner(
            self.state, target_generation_id=None, now=NOW,
        )
        selected_missing = SOURCE.select_cache_owner(
            self.state, target_generation_id="9" * 64, now=NOW,
        )

        self.assertIsNotNone(selected_exact)
        self.assertEqual(selected_exact[0]["generation_id"], target_id)
        self.assertIsNotNone(selected_blank)
        self.assertEqual(selected_blank[0]["generation_id"], foreign_id)
        self.assertIsNone(selected_missing)

    def test_cache_selection_rejects_duplicate_generation_ownership(self) -> None:
        context = self.context_bytes()
        outputs = [
            digest_entry(SOURCE.ENRICHMENT_MEMBER, self.evidence),
            digest_entry(SOURCE.ORIGINAL_CANDIDATE_MEMBER, self.candidate),
            digest_entry(SOURCE.CONTEXT_MEMBER, context),
        ]
        checkpoint = self.install_state(outputs)
        index_path = self.state / "sources/data_go_kr/index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["generations"].append({"generation_id": checkpoint["generation_id"]})
        index_path.write_bytes(SOURCE.canonical_json(index) + b"\n")
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "generation identity is duplicated"):
            SOURCE.select_cache_owner(
                self.state, target_generation_id=str(checkpoint["generation_id"]), now=NOW,
            )

    def test_retained_owner_archive_path_is_untrusted_and_native_reauthenticated(self) -> None:
        self.install_state([], contract="modern")
        context = self.context_bytes()
        members = {
            SOURCE.ENRICHMENT_MEMBER: self.evidence,
            SOURCE.ORIGINAL_CANDIDATE_MEMBER: self.candidate,
            SOURCE.CONTEXT_MEMBER: context,
        }
        outputs = [digest_entry(name, raw) for name, raw in members.items()]
        checkpoint = self.install_state(outputs, contract="modern")
        archive_raw = zip_bytes({
            **members,
            SOURCE.CHECKPOINT_RECEIPT_MEMBER: SOURCE.canonical_json(checkpoint) + b"\n",
        })
        archive_path = self.root / "retained-owner.zip"
        archive_path.write_bytes(archive_raw)
        reader = self.b_reader(archive_raw)

        with mock.patch.object(
            reader, "archive", side_effect=AssertionError("retained bytes must not be downloaded again"),
        ):
            contract = SOURCE.authenticate_owner_contract_from_archive_path(
                checkpoint,
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                reader=reader,
                archive_path=archive_path,
            )

        self.assertIs(type(contract), SOURCE.AuthenticatedCacheContract)
        self.assertEqual(contract.profile, SOURCE.CACHE_SOURCE_PROFILE)
        self.assertEqual(contract.producer_head_sha, "3" * 40)
        self.assertEqual(
            [kind for kind, _value in reader.calls].count("json"),
            3,
        )
        self.assertEqual(
            [kind for kind, _value in reader.calls].count("source_file"),
            len(self.modern_sources),
        )
        self.assertNotIn("archive", [kind for kind, _value in reader.calls])

        missing = self.root / "missing-owner.zip"
        with self.assertRaisesRegex(SOURCE.CacheSourceUnavailable, "retained archive is unavailable"):
            SOURCE.authenticate_owner_contract_from_archive_path(
                checkpoint,
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                reader=self.b_reader(archive_raw),
                archive_path=missing,
            )

        archive_path.write_bytes(archive_raw + b"tampered")
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "archive byte count differs"):
            SOURCE.authenticate_owner_contract_from_archive_path(
                checkpoint,
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                reader=self.b_reader(archive_raw),
                archive_path=archive_path,
            )

        target = self.root / "owner-target.zip"
        target.write_bytes(archive_raw)
        symlink = self.root / "owner-symlink.zip"
        symlink.symlink_to(target)
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "not a bounded regular file"):
            SOURCE.authenticate_owner_contract_from_archive_path(
                checkpoint,
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                reader=self.b_reader(archive_raw),
                archive_path=symlink,
            )

    def test_bound_retained_packet_mints_offline_contract_from_exact_local_source(self) -> None:
        self.install_state([], contract="modern")
        context = self.context_bytes()
        members = {
            SOURCE.ENRICHMENT_MEMBER: self.evidence,
            SOURCE.ORIGINAL_CANDIDATE_MEMBER: self.candidate,
            SOURCE.CONTEXT_MEMBER: context,
        }
        outputs = [digest_entry(name, raw) for name, raw in members.items()]
        checkpoint = self.install_state(outputs, contract="modern")
        archive_raw = zip_bytes({
            **members,
            SOURCE.CHECKPOINT_RECEIPT_MEMBER: SOURCE.canonical_json(checkpoint) + b"\n",
        })
        repository = self.root / "source-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Cache Source Test"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "cache-source@example.invalid"], cwd=repository, check=True)
        for relative, raw in self.modern_sources.items():
            path = repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repository, check=True)
        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True,
        ).strip()
        metadata = artifact(
            run_id="200", artifact_id="201",
            name="upstream-catalogue-processing-200-1",
            raw=archive_raw, head_sha=head_sha,
        )
        retained_inventory = {"total_count": 1, "artifacts": [metadata]}
        retained_run = run(
            run_id="200", attempt=1, head_sha=head_sha,
            workflow_path=".github/workflows/upstream-catalogue-process.yml",
        )
        deadline = time.monotonic() + SOURCE.MAX_WHOLE_ATTEMPT_SECONDS
        source_reader = SOURCE.LocalGitSourceReader(
            cwd=repository, deadline_monotonic=deadline,
        )
        contract = SOURCE.authenticate_owner_contract_from_bound_evidence(
            checkpoint,
            repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
            artifact_inventory=retained_inventory, run=retained_run,
            archive=archive_raw, source_reader=source_reader,
            deadline_monotonic=deadline,
        )
        self.assertIs(type(contract), SOURCE.AuthenticatedCacheContract)
        self.assertEqual(contract.profile, SOURCE.CACHE_SOURCE_PROFILE)
        self.assertEqual(contract.producer_head_sha, head_sha)
        self.assertGreater(source_reader._cached_bytes, 0)

        duplicated = {"total_count": 2, "artifacts": [metadata, copy.deepcopy(metadata)]}
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "inventory is not exact"):
            SOURCE.authenticate_owner_contract_from_bound_evidence(
                checkpoint,
                repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
                artifact_inventory=duplicated, run=retained_run,
                archive=archive_raw, source_reader=source_reader,
                deadline_monotonic=deadline,
            )

        missing_head = "f" * 40
        missing_metadata = artifact(
            run_id="200", artifact_id="201",
            name="upstream-catalogue-processing-200-1",
            raw=archive_raw, head_sha=missing_head,
        )
        missing_run = run(
            run_id="200", attempt=1, head_sha=missing_head,
            workflow_path=".github/workflows/upstream-catalogue-process.yml",
        )
        missing_deadline = time.monotonic() + SOURCE.MAX_WHOLE_ATTEMPT_SECONDS
        missing_reader = SOURCE.LocalGitSourceReader(
            cwd=repository, deadline_monotonic=missing_deadline,
        )
        with self.assertRaisesRegex(SOURCE.CacheSourceUnavailable, "local Git read failed"):
            SOURCE.authenticate_owner_contract_from_bound_evidence(
                checkpoint,
                repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
                artifact_inventory={"total_count": 1, "artifacts": [missing_metadata]},
                run=missing_run, archive=archive_raw,
                source_reader=missing_reader, deadline_monotonic=missing_deadline,
            )

    def restore_legacy_cache_fixture(
        self, *, candidate: bytes | None = None, evidence: bytes | None = None,
        checkpoint_template: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Restore an authenticated legacy B/A pair for joined consumer tests."""
        if candidate is not None:
            self.candidate = candidate
            self.admission["candidate_sha256"] = hashlib.sha256(candidate).hexdigest()
        if evidence is not None:
            self.evidence = evidence
        legacy_payloads = {
            "composed-candidate.registry.json": b"[]\n",
            "ready-scope.registry.json": b"[]\n",
            "semantic-diff.json": b"{}\n",
            "regeneration-queue.json": b'{"items":[]}\n',
            "quarantine.json": b'{"items":[]}\n',
            "composition-receipt.json": b"{}\n",
            SOURCE.ENRICHMENT_MEMBER: self.evidence,
            "upstream-catalogue-processing-result.json": b"{}\n",
        }
        b_outputs = [digest_entry(name, raw) for name, raw in legacy_payloads.items()]
        collector_members = {
            "candidate.registry.json": self.candidate,
            "catalog-diff.json": b"{}\n",
            "upstream-refresh-evidence.json": SOURCE.canonical_json({
                "source_id": "data_go_kr",
                "observed_at": self.admission["observed_at"],
                "snapshot": {"sha256": self.admission["candidate_sha256"]},
                "diff": {"sha256": hashlib.sha256(b"{}\n").hexdigest()},
            }) + b"\n",
            "upstream-refresh-work-packet.json": b"{}\n",
        }
        a_archive = zip_bytes(collector_members)
        self.admission["artifact_digest_sha256"] = hashlib.sha256(a_archive).hexdigest()
        self.admission["artifact_size_bytes"] = len(a_archive)
        self.admission["refresh_evidence_sha256"] = hashlib.sha256(
            collector_members["upstream-refresh-evidence.json"],
        ).hexdigest()
        self.admission["admission_id"] = HANDOFF.admission_id(
            "100", 1, self.admission["refresh_evidence_sha256"],
        )
        # Reinstall state with the exact legacy admission and corrected diff/evidence references.
        self.state = self.root / "legacy-state"
        index_path = self.state / "sources/data_go_kr/index.json"
        if checkpoint_template is None:
            checkpoint = self.install_state(b_outputs, contract="legacy")
            index = json.loads(index_path.read_text())
            owner_path = self.state / f"sources/data_go_kr/generations/{self.owner_generation}.json"
            checkpoint = json.loads(owner_path.read_text())
        else:
            checkpoint = copy.deepcopy(checkpoint_template)
            generation_inputs = copy.deepcopy(checkpoint["generation_inputs"])
            generation_inputs.pop("cache_source_profile", None)
            generation_inputs["candidate_sha256"] = hashlib.sha256(self.candidate).hexdigest()
            generation_inputs["generator_revision"] = self.legacy_revision
            self.owner_generation = SOURCE.sha256_bytes(SOURCE.canonical_json(generation_inputs))
            checkpoint["generation_inputs"] = generation_inputs
            checkpoint["generation_id"] = self.owner_generation
            checkpoint["observed_at"] = self.admission["observed_at"]
            checkpoint["last_observation"] = {
                "producer_run_id": self.admission["producer_run_id"],
                "refresh_evidence_sha256": self.admission["refresh_evidence_sha256"],
                "observed_at": self.admission["observed_at"],
                "collection_status": "success",
                "execution_mode": "live",
            }
            checkpoint["observation_count"] = 1
            checkpoint["status"] = "quarantined"
            checkpoint["attempts_consumed"] = 3
            checkpoint["attempts_by_id"] = {"15056854": 3}
            checkpoint["detail_retry_reset_ids"] = []
            checkpoint["request_reservation"] = None
            evidence_value = json.loads(self.evidence)
            checkpoint["detail_records"] = [{
                "id": row["api_key"]["id"],
                "source_sha256": row["source_sha256"],
                "guide_sha256": row["guide_sha256"],
                "status": row["status"],
                "failure_diagnostic": copy.deepcopy(row["failure_diagnostic"]),
                "link_metadata": copy.deepcopy(row["link_metadata"]),
            } for row in evidence_value.get("worker_outcomes", [])]
            checkpoint["detail_queue_cursor"] = 19
            checkpoint["output_digests"] = b_outputs
            checkpoint["output_artifact"] = {
                "repository": REPOSITORY,
                "run_id": "200",
                "name": "upstream-catalogue-processing-200-1",
                "artifact_id": "201",
                "expires_at": EXPIRY,
                "bundle_manifest_sha256": SOURCE.sha256_bytes(SOURCE.canonical_json(b_outputs)),
            }
            checkpoint["input_artifacts"] = [{
                "run_id": self.admission["producer_run_id"],
                "name": self.admission["artifact_name"],
                "artifact_id": self.admission["artifact_id"],
                "expires_at": self.admission["artifact_expires_at"],
                "candidate_sha256": self.admission["candidate_sha256"],
                "evidence_sha256": self.admission["refresh_evidence_sha256"],
                "diff_sha256": hashlib.sha256(b"{}\n").hexdigest(),
            }]
            checkpoint["lease"] = None
            checkpoint["outcome"] = {
                "reason": "details_pending",
                "detail_failure_counts": {"resolved_link_operation_contract_unproven": 1},
            }
            owner_path = self.state / f"sources/data_go_kr/generations/{self.owner_generation}.json"
            owner_path.parent.mkdir(parents=True, exist_ok=True)
            index = {
                "schema_version": "datapan.upstream-catalogue-checkpoint.v1",
                "generations": [{"generation_id": self.owner_generation}],
                "detail_queue_cursor": 19,
                "detail_retry_state": {"15056854": {
                    "source_sha256": checkpoint["detail_records"][0]["source_sha256"],
                    "guide_sha256": None,
                    "attempts": 3,
                    "last_attempt_at": self.admission["observed_at"],
                    "failure_diagnostic": copy.deepcopy(
                        checkpoint["detail_records"][0]["failure_diagnostic"],
                    ),
                }},
                "collector_handoff": HANDOFF.empty_ledger(),
            }
            index_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint["input_artifacts"][0]["evidence_sha256"] = self.admission["refresh_evidence_sha256"]
        checkpoint["input_artifacts"][0]["diff_sha256"] = hashlib.sha256(b"{}\n").hexdigest()
        checkpoint["last_observation"]["refresh_evidence_sha256"] = self.admission["refresh_evidence_sha256"]
        checkpoint.pop("checkpoint_sha256")
        checkpoint["checkpoint_sha256"] = SOURCE.sha256_bytes(SOURCE.canonical_json(checkpoint))
        owner_path.write_bytes(SOURCE.canonical_json(checkpoint) + b"\n")
        b_archive = zip_bytes({
            **legacy_payloads,
            SOURCE.CHECKPOINT_RECEIPT_MEMBER: SOURCE.canonical_json(checkpoint) + b"\n",
        })
        index["collector_handoff"] = HANDOFF.empty_ledger()
        HANDOFF.add_admission(index, self.admission, now="2026-10-07T00:00:00Z")
        index_path.write_bytes(SOURCE.canonical_json(index) + b"\n")

        b_name = "upstream-catalogue-processing-200-1"
        b_metadata = artifact(
            run_id="200", artifact_id="201", name=b_name, raw=b_archive, head_sha="3" * 40,
        )
        a_metadata = artifact(
            run_id="100", artifact_id="101", name=self.admission["artifact_name"],
            raw=a_archive, head_sha=self.admission["head_sha"],
        )
        a_metadata["expires_at"] = self.admission["artifact_expires_at"]
        responses = {
            f"repos/{REPOSITORY}/actions/runs/200/artifacts?per_page=100": {"artifacts": [b_metadata]},
            f"repos/{REPOSITORY}/actions/artifacts/201": b_metadata,
            f"repos/{REPOSITORY}/actions/runs/200/attempts/1": run(
                run_id="200", attempt=1, head_sha="3" * 40,
                workflow_path=".github/workflows/upstream-catalogue-process.yml",
            ),
            f"repos/{REPOSITORY}/actions/runs/100/attempts/1": run(
                run_id="100", attempt=1, head_sha=self.admission["head_sha"],
                workflow_path=".github/workflows/upstream-catalog-refresh.yml",
            ),
            f"repos/{REPOSITORY}/actions/runs/100/artifacts?per_page=100": {"artifacts": [a_metadata]},
            f"repos/{REPOSITORY}/actions/artifacts/101": a_metadata,
        }
        reader = FakeReader(responses, {"201": b_archive, "101": a_archive}, {
            ("3" * 40, "scripts/process-upstream-catalogue-candidate.py"): self.legacy_processor,
            ("3" * 40, "scripts/upstream_catalogue_handoff.py"): self.legacy_handoff,
        })
        result = SOURCE.restore_selected_cache(
            state_dir=self.state,
            target_generation_id=self.owner_generation,
            repository=REPOSITORY,
            default_branch=DEFAULT_BRANCH,
            output_dir=self.output,
            reader=reader,
            now=NOW,
        )
        return {
            **result,
            "checkpoint": checkpoint,
            "reader": reader,
            "state_dir": self.state,
            "output_dir": self.output,
            "b_archive": b_archive,
        }

    def test_legacy_restore_uses_only_selected_owner_original_a(self) -> None:
        result = self.restore_legacy_cache_fixture()
        self.assertEqual(result["provenance"], "legacy")
        self.assertEqual((self.output / SOURCE.ORIGINAL_CANDIDATE_MEMBER).read_bytes(), self.candidate)
        self.assertEqual(pathlib.Path(result["owner_archive_path"]).read_bytes(), result["b_archive"])
        self.assertEqual(
            [value for kind, value in result["reader"].calls if kind == "archive"],
            ["201", "101"],
        )

    def test_legacy_missing_original_a_admission_fails_before_any_processor_claim(self) -> None:
        result = self.restore_legacy_cache_fixture()
        index_path = pathlib.Path(result["state_dir"]) / "sources/data_go_kr/index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["collector_handoff"] = HANDOFF.empty_ledger()
        index_path.write_bytes(SOURCE.canonical_json(index) + b"\n")
        state_before = {
            str(path.relative_to(result["state_dir"])): path.read_bytes()
            for path in pathlib.Path(result["state_dir"]).rglob("*") if path.is_file()
        }

        with self.assertRaisesRegex(SOURCE.CacheSourceError, "does not bind one original admission"):
            SOURCE.restore_selected_cache(
                state_dir=pathlib.Path(result["state_dir"]),
                target_generation_id=self.owner_generation,
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                output_dir=self.output,
                reader=result["reader"],
                now=NOW,
            )

        state_after = {
            str(path.relative_to(result["state_dir"])): path.read_bytes()
            for path in pathlib.Path(result["state_dir"]).rglob("*") if path.is_file()
        }
        self.assertEqual(state_after, state_before)
        self.assertFalse((self.output / SOURCE.CONTEXT_MEMBER).exists())

    def test_legacy_forged_original_a_candidate_fails_before_any_processor_claim(self) -> None:
        result = self.restore_legacy_cache_fixture()
        reader = result["reader"]
        original_a = reader.archives["101"]
        with zipfile.ZipFile(io.BytesIO(original_a)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members["candidate.registry.json"] = self.candidate + b" "
        forged_a = zip_bytes(members)
        reader.archives["101"] = forged_a
        for endpoint in (
            f"repos/{REPOSITORY}/actions/runs/100/artifacts?per_page=100",
            f"repos/{REPOSITORY}/actions/artifacts/101",
        ):
            response = reader.responses[endpoint]
            if endpoint.endswith("per_page=100"):
                response["artifacts"][0]["size_in_bytes"] = len(forged_a)
                response["artifacts"][0]["digest"] = "sha256:" + hashlib.sha256(forged_a).hexdigest()
            else:
                response["size_in_bytes"] = len(forged_a)
                response["digest"] = "sha256:" + hashlib.sha256(forged_a).hexdigest()
        index_path = pathlib.Path(result["state_dir"]) / "sources/data_go_kr/index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        admission = index["collector_handoff"]["admitted_observations"][0]
        admission["artifact_size_bytes"] = len(forged_a)
        admission["artifact_digest_sha256"] = hashlib.sha256(forged_a).hexdigest()
        index_path.write_bytes(SOURCE.canonical_json(index) + b"\n")
        state_before = {
            str(path.relative_to(result["state_dir"])): path.read_bytes()
            for path in pathlib.Path(result["state_dir"]).rglob("*") if path.is_file()
        }

        with self.assertRaisesRegex(SOURCE.CacheSourceError, "archive member digest is invalid"):
            SOURCE.restore_selected_cache(
                state_dir=pathlib.Path(result["state_dir"]),
                target_generation_id=self.owner_generation,
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                output_dir=self.output,
                reader=reader,
                now=NOW,
            )

        state_after = {
            str(path.relative_to(result["state_dir"])): path.read_bytes()
            for path in pathlib.Path(result["state_dir"]).rglob("*") if path.is_file()
        }
        self.assertEqual(state_after, state_before)
        self.assertFalse((self.output / SOURCE.CONTEXT_MEMBER).exists())

    def test_modern_partial_bundle_never_downgrades_to_legacy(self) -> None:
        self.install_state([], contract="modern")
        context = self.context_bytes()
        members = {
            SOURCE.ENRICHMENT_MEMBER: self.evidence,
            SOURCE.CONTEXT_MEMBER: context,
        }
        outputs = [digest_entry(name, raw) for name, raw in members.items()]
        checkpoint = self.install_state(outputs, contract="modern")
        archive_raw = zip_bytes({
            **members,
            SOURCE.CHECKPOINT_RECEIPT_MEMBER: SOURCE.canonical_json(checkpoint) + b"\n",
        })
        reader = self.b_reader(archive_raw)
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "incomplete"):
            SOURCE.restore_selected_cache(
                state_dir=self.state,
                target_generation_id=self.owner_generation,
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                output_dir=self.output,
                reader=reader,
                now=NOW,
            )
        self.assertFalse(any("/actions/runs/100/" in endpoint for kind, endpoint in reader.calls if kind == "json"))

    def test_stripped_modern_bundle_cannot_enter_legacy_fallback(self) -> None:
        members = {SOURCE.ENRICHMENT_MEMBER: self.evidence}
        outputs = [digest_entry(name, raw) for name, raw in members.items()]
        checkpoint = self.install_state(outputs, contract="stripped")
        archive_raw = zip_bytes({
            **members,
            SOURCE.CHECKPOINT_RECEIPT_MEMBER: SOURCE.canonical_json(checkpoint) + b"\n",
        })
        reader = self.b_reader(archive_raw)
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "generator contract"):
            SOURCE.restore_selected_cache(
                state_dir=self.state,
                target_generation_id=self.owner_generation,
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                output_dir=self.output,
                reader=reader,
                now=NOW,
            )
        self.assertFalse(any("/actions/runs/100/" in endpoint for kind, endpoint in reader.calls if kind == "json"))

        reader = self.b_reader(archive_raw)
        reader.source_files[("3" * 40, "scripts/process-upstream-catalogue-candidate.py")] = b"wrong source\n"
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "generator contract"):
            SOURCE.restore_selected_cache(
                state_dir=self.state,
                target_generation_id=self.owner_generation,
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                output_dir=self.output,
                reader=reader,
                now=NOW,
            )
        self.assertFalse(any("/actions/runs/100/" in endpoint for kind, endpoint in reader.calls if kind == "json"))

    def test_expired_legacy_original_a_is_unavailable_without_state_mutation(self) -> None:
        self.admission["artifact_expires_at"] = "2026-10-06T00:00:00Z"
        outputs = [digest_entry(SOURCE.ENRICHMENT_MEMBER, self.evidence)]
        checkpoint = self.install_state(outputs, contract="legacy")
        archive_raw = zip_bytes({
            SOURCE.ENRICHMENT_MEMBER: self.evidence,
            SOURCE.CHECKPOINT_RECEIPT_MEMBER: SOURCE.canonical_json(checkpoint) + b"\n",
        })
        reader = self.b_reader(archive_raw)
        state_before = {
            str(path.relative_to(self.state)): path.read_bytes()
            for path in self.state.rglob("*") if path.is_file()
        }
        with self.assertRaisesRegex(SOURCE.CacheSourceUnavailable, "expired"):
            SOURCE.restore_selected_cache(
                state_dir=self.state,
                target_generation_id=self.owner_generation,
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                output_dir=self.output,
                reader=reader,
                now=NOW,
            )
        state_after = {
            str(path.relative_to(self.state)): path.read_bytes()
            for path in self.state.rglob("*") if path.is_file()
        }
        self.assertEqual(state_after, state_before)
        self.assertFalse(any("/actions/runs/100/" in endpoint for kind, endpoint in reader.calls if kind == "json"))

    def test_archive_expanded_bound_is_exact_and_rejects_overflow(self) -> None:
        members = {"a.json": b"a" * 7, "b.json": b"b" * 5}
        raw = zip_bytes(members)
        expected = {
            name: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
            for name, value in members.items()
        }
        exact = self.root / "exact"
        SOURCE._safe_extract(
            raw, exact, expected=expected,
            maximum_archive_bytes=len(raw),
            maximum_expanded_bytes=12,
        )
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "expanded byte bound"):
            SOURCE._safe_extract(
                raw, self.root / "overflow", expected=expected,
                maximum_archive_bytes=len(raw),
                maximum_expanded_bytes=11,
            )

    def test_archive_compressed_and_member_bounds_are_exact(self) -> None:
        self.assertEqual(
            SOURCE.MAX_CACHE_EXPANDED_BYTES,
            SOURCE.MAX_LEGACY_CACHE_BUNDLE_BYTES + SOURCE.MAX_CANDIDATE_BYTES + SOURCE.MAX_CONTEXT_BYTES,
        )
        self.assertEqual(SOURCE.MAX_CACHE_ARCHIVE_BYTES, SOURCE.MAX_CACHE_EXPANDED_BYTES)
        raw = zip_bytes({"only.json": b"abcd"})
        expected = {
            "only.json": {"bytes": 4, "sha256": hashlib.sha256(b"abcd").hexdigest()},
        }
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "compressed byte bound"):
            SOURCE._safe_extract(
                raw, self.root / "compressed-overflow", expected=expected,
                maximum_archive_bytes=len(raw) - 1, maximum_expanded_bytes=4,
            )
        with mock.patch.object(SOURCE, "MAX_CANDIDATE_BYTES", 3):
            with self.assertRaisesRegex(SOURCE.CacheSourceError, "unsafe member"):
                SOURCE._safe_extract(
                    raw, self.root / "member-overflow", expected=expected,
                    maximum_archive_bytes=len(raw), maximum_expanded_bytes=4,
                )

    def test_archive_rejects_paths_special_members_and_collisions(self) -> None:
        cases: list[tuple[str, bytes]] = []
        cases.append(("path", zip_bytes({"../escape.json": b"{}"})))

        special = io.BytesIO()
        with zipfile.ZipFile(special, "w") as archive:
            info = zipfile.ZipInfo("link.json")
            info.external_attr = stat.S_IFLNK << 16
            archive.writestr(info, b"target")
        cases.append(("special", special.getvalue()))

        duplicate = io.BytesIO()
        with zipfile.ZipFile(duplicate, "w") as archive:
            with self.assertWarns(UserWarning):
                archive.writestr("same.json", b"one")
                archive.writestr("same.json", b"two")
        cases.append(("collision", duplicate.getvalue()))

        for label, raw in cases:
            with self.subTest(label=label), self.assertRaisesRegex(
                SOURCE.CacheSourceError, "member set|unsafe member",
            ):
                SOURCE._safe_extract(
                    raw, self.root / label,
                    expected={"link.json" if label == "special" else "same.json" if label == "collision" else "../escape.json": None},
                    maximum_archive_bytes=len(raw), maximum_expanded_bytes=1024,
                )

    def test_artifact_run_repository_binding_is_strict(self) -> None:
        context = self.context_bytes()
        members = {
            SOURCE.ENRICHMENT_MEMBER: self.evidence,
            SOURCE.ORIGINAL_CANDIDATE_MEMBER: self.candidate,
            SOURCE.CONTEXT_MEMBER: context,
        }
        outputs = [digest_entry(name, raw) for name, raw in members.items()]
        checkpoint = self.install_state(outputs)
        archive_raw = zip_bytes({
            **members,
            SOURCE.CHECKPOINT_RECEIPT_MEMBER: SOURCE.canonical_json(checkpoint) + b"\n",
        })
        reader = self.b_reader(archive_raw)
        endpoint = f"repos/{REPOSITORY}/actions/runs/200/attempts/1"
        reader.responses[endpoint]["repository"]["id"] = 2
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "not bound"):
            SOURCE.restore_selected_cache(
                state_dir=self.state,
                target_generation_id=self.owner_generation,
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                output_dir=self.output,
                reader=reader,
                now=NOW,
            )

    def test_nonfinite_or_overlong_deadline_is_rejected_before_native_child(self) -> None:
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "not finite"):
            SOURCE.GitHubNativeReader(cwd=self.root, deadline_monotonic=float("nan"))
        with self.assertRaisesRegex(SOURCE.CacheSourceError, "whole-attempt bound"):
            SOURCE.GitHubNativeReader(
                cwd=self.root,
                deadline_monotonic=SOURCE.time.monotonic() + SOURCE.MAX_WHOLE_ATTEMPT_SECONDS + 1,
            )

    def test_post_spawn_selector_failure_closes_pipes_and_reaps_both_reader_children(self) -> None:
        real_popen = subprocess.Popen
        for kind in ("native", "local-git"):
            with self.subTest(kind=kind):
                started: list[subprocess.Popen[bytes]] = []

                def spawn(_argv, **kwargs):
                    process = real_popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs,
                    )
                    started.append(process)
                    return process

                deadline = time.monotonic() + 3
                with (
                    mock.patch.object(SOURCE.subprocess, "Popen", side_effect=spawn),
                    mock.patch.object(
                        SOURCE.selectors, "DefaultSelector",
                        side_effect=RuntimeError("selector construction failed"),
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "selector construction failed"):
                        if kind == "native":
                            SOURCE.GitHubNativeReader(
                                cwd=self.root, deadline_monotonic=deadline,
                            )._run("repos/StatPan/datapan-registry/test", maximum_bytes=1024)
                        else:
                            SOURCE.LocalGitSourceReader(
                                cwd=self.root, deadline_monotonic=deadline,
                            ).source_file(
                                REPOSITORY, "1" * 40, "scripts/test.py", maximum_bytes=1024,
                            )
                self.assertEqual(len(started), 1)
                self.assertIsNotNone(started[0].poll())
                self.assertTrue(started[0].stdout.closed)
                self.assertTrue(started[0].stderr.closed)

    def test_liveness_fault_never_suppresses_sigkill(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable, "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, start_new_session=True,
        )
        real_alive = SOURCE._process_group_alive
        real_killpg = os.killpg
        liveness_calls = 0
        signals: list[int] = []

        def liveness(target):
            nonlocal liveness_calls
            liveness_calls += 1
            if liveness_calls == 1:
                raise SOURCE.CacheSourceUnavailable("injected liveness failure")
            return real_alive(target)

        def killpg(pid: int, sig: int) -> None:
            signals.append(sig)
            real_killpg(pid, sig)

        try:
            with (
                mock.patch.object(SOURCE, "_process_group_alive", side_effect=liveness),
                mock.patch.object(SOURCE.os, "killpg", side_effect=killpg),
            ):
                with self.assertRaisesRegex(
                    SOURCE.CacheSourceUnavailable, "cleanup is uncertain",
                ):
                    SOURCE._terminate_process_group(
                        process, deadline_monotonic=time.monotonic() + 2,
                    )
            self.assertIn(signal.SIGKILL, signals)
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                real_killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_read_failure_and_cancellation_contain_both_reader_children(self) -> None:
        real_popen = subprocess.Popen
        real_read = os.read
        for kind in ("native", "local-git"):
            for failure in (OSError("injected read failure"), KeyboardInterrupt()):
                with self.subTest(kind=kind, failure=type(failure).__name__):
                    started: list[subprocess.Popen[bytes]] = []

                    def spawn(_argv, **kwargs):
                        process = real_popen(
                            [
                                sys.executable, "-c",
                                "import sys,time; sys.stdout.write('x'); sys.stdout.flush(); time.sleep(30)",
                            ],
                            **kwargs,
                        )
                        started.append(process)
                        return process

                    def read(descriptor: int, maximum_bytes: int) -> bytes:
                        if not started:
                            return real_read(descriptor, maximum_bytes)
                        raise failure

                    deadline = time.monotonic() + 3
                    with (
                        mock.patch.object(SOURCE.subprocess, "Popen", side_effect=spawn),
                        mock.patch.object(SOURCE.os, "read", side_effect=read),
                    ):
                        with self.assertRaises(type(failure)):
                            if kind == "native":
                                SOURCE.GitHubNativeReader(
                                    cwd=self.root, deadline_monotonic=deadline,
                                )._run("repos/StatPan/datapan-registry/test", maximum_bytes=1024)
                            else:
                                SOURCE.LocalGitSourceReader(
                                    cwd=self.root, deadline_monotonic=deadline,
                                ).source_file(
                                    REPOSITORY, "1" * 40, "scripts/test.py", maximum_bytes=1024,
                                )
                    self.assertEqual(len(started), 1)
                    self.assertIsNotNone(started[0].poll())
                    self.assertTrue(started[0].stdout.closed)
                    self.assertTrue(started[0].stderr.closed)

    def test_exited_leader_with_term_resistant_descendant_is_contained_within_deadline(self) -> None:
        real_popen = subprocess.Popen
        pid_path = self.root / "held-pipe-descendant.pid"
        script = (
            "import pathlib,subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')"
        )
        started: list[subprocess.Popen[bytes]] = []

        def spawn(_argv, **kwargs):
            process = real_popen([sys.executable, "-c", script, str(pid_path)], **kwargs)
            started.append(process)
            return process

        deadline = time.monotonic() + 1.5
        began = time.monotonic()
        with mock.patch.object(SOURCE.subprocess, "Popen", side_effect=spawn):
            with self.assertRaisesRegex(
                SOURCE.CacheSourceUnavailable, "deadline expired",
            ):
                SOURCE.LocalGitSourceReader(
                    cwd=self.root, deadline_monotonic=deadline,
                ).source_file(
                    REPOSITORY, "1" * 40, "scripts/test.py", maximum_bytes=1024,
                )
        self.assertLessEqual(time.monotonic(), deadline + 0.15)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll())
        descendant = int(pid_path.read_text(encoding="utf-8"))
        for _ in range(40):
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            self.fail("cache source local Git reader left its owned descendant alive")
        self.assertLess(time.monotonic() - began, 2.0)

    def test_exhausted_cleanup_deadline_still_attempts_sigkill_without_fresh_grace(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable, "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, start_new_session=True,
        )
        real_killpg = os.killpg
        signals: list[int] = []

        def killpg(pid: int, sig: int) -> None:
            signals.append(sig)
            real_killpg(pid, sig)

        began = time.monotonic()
        try:
            with mock.patch.object(SOURCE.os, "killpg", side_effect=killpg):
                try:
                    SOURCE._terminate_process_group(
                        process, deadline_monotonic=time.monotonic() - 0.001,
                    )
                except SOURCE.CacheSourceUnavailable:
                    # An exhausted deadline may leave containment unconfirmed;
                    # the key invariant is immediate escalation without a new
                    # cleanup grace period.
                    pass
            self.assertIn(signal.SIGKILL, signals)
            self.assertLess(time.monotonic() - began, 0.2)
        finally:
            if process.poll() is None:
                real_killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_registration_and_cleanup_faults_still_close_each_owned_resource(self) -> None:
        for kind in ("native", "local-git"):
            with self.subTest(kind=kind):
                events: list[str] = []

                class Stream:
                    def __init__(self, descriptor: int, label: str, *, fail: bool = False) -> None:
                        self.descriptor = descriptor
                        self.label = label
                        self.fail = fail

                    def fileno(self) -> int:
                        return self.descriptor

                    def close(self) -> None:
                        events.append("close-" + self.label)
                        if self.fail:
                            raise OSError("injected stream close failure")

                class Process:
                    pid = 123456
                    stdout = Stream(101, "stdout", fail=True)
                    stderr = Stream(102, "stderr")

                class Selector:
                    def __init__(self) -> None:
                        self.calls = 0

                    def register(self, _stream, _events) -> None:
                        self.calls += 1
                        if self.calls == 2:
                            raise RuntimeError("selector registration failed")

                    def close(self) -> None:
                        events.append("close-selector")
                        raise OSError("injected selector close failure")

                with (
                    mock.patch.object(SOURCE.subprocess, "Popen", return_value=Process()),
                    mock.patch.object(SOURCE.selectors, "DefaultSelector", return_value=Selector()),
                    mock.patch.object(SOURCE.os, "set_blocking", return_value=None),
                    mock.patch.object(
                        SOURCE, "_terminate_process_group",
                        side_effect=SOURCE.CacheSourceUnavailable("injected cleanup failure"),
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "selector registration failed",
                    ) as raised:
                        if kind == "native":
                            SOURCE.GitHubNativeReader(
                                cwd=self.root, deadline_monotonic=time.monotonic() + 3,
                            )._run("repos/StatPan/datapan-registry/test", maximum_bytes=1024)
                        else:
                            SOURCE.LocalGitSourceReader(
                                cwd=self.root, deadline_monotonic=time.monotonic() + 3,
                            ).source_file(
                                REPOSITORY, "1" * 40, "scripts/test.py", maximum_bytes=1024,
                            )
                self.assertEqual(events, ["close-selector", "close-stdout", "close-stderr"])
                self.assertTrue(any("cleanup failure" in note for note in raised.exception.__notes__))


if __name__ == "__main__":
    unittest.main()
