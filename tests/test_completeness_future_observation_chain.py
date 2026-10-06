from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import pathlib
import shutil
import tempfile
import unittest
import zipfile


ROOT = pathlib.Path(__file__).parents[1]
GENERATOR_PATH = ROOT / "scripts/generate-completeness-proof-rollup.py"
SPEC = importlib.util.spec_from_file_location("future_observation_rollup", GENERATOR_PATH)
assert SPEC and SPEC.loader
ROLLUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROLLUP)

PRIMARY_TEST_PATH = ROOT / "tests/test_generate_completeness_proof_rollup.py"
PRIMARY_SPEC = importlib.util.spec_from_file_location("primary_rollup_test_helpers", PRIMARY_TEST_PATH)
assert PRIMARY_SPEC and PRIMARY_SPEC.loader
PRIMARY = importlib.util.module_from_spec(PRIMARY_SPEC)
PRIMARY_SPEC.loader.exec_module(PRIMARY)
HELPERS = PRIMARY.CompletenessProofRollupTest()

OPERATION_SCOPE_ID = "data-go-kr.api-operations"
NEW_A_RUN_ID = "99972210001"
NEW_A_ARTIFACT_ID = "99972210002"
NEW_A_OBSERVED_AT = "2026-10-04T13:10:00Z"
NEW_A_RUN_HEAD = "01148419bef5d7f212e92ab96d0ba1c3da6c298c"
CURRENT_REGISTRY_SHA256 = "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pretty(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _zip_streamed_registry(registry_path: pathlib.Path, small_members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for name, raw in small_members.items():
            archive.writestr(name, raw)
        with registry_path.open("rb") as source, archive.open(
            "candidate.registry.json", "w", force_zip64=True,
        ) as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
    return output.getvalue()


def _zip_rebound(original: bytes, replacements: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original), "r") as source_archive:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as target:
            for info in source_archive.infolist():
                if info.filename in replacements:
                    target.writestr(info.filename, replacements[info.filename])
                else:
                    with source_archive.open(info, "r") as source, target.open(
                        info.filename, "w", force_zip64=info.file_size >= (1 << 31),
                    ) as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
    return output.getvalue()


def _contents_api(template: dict[str, object], *, path: str, raw: bytes) -> dict[str, object]:
    sha = ROLLUP.git_blob_sha1(raw)
    value = copy.deepcopy(template)
    value.update({
        "name": pathlib.PurePosixPath(path).name,
        "path": path,
        "sha": sha,
        "size": len(raw),
        "url": f"https://api.github.com/repos/StatPan/datapan-registry/contents/{path}?ref=automation/upstream-catalogue-state",
        "git_url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{sha}",
        "download_url": f"https://raw.githubusercontent.com/StatPan/datapan-registry/automation/upstream-catalogue-state/{path}",
        "type": "file",
        "content": base64.b64encode(raw).decode("ascii"),
        "encoding": "base64",
    })
    value["_links"] = {
        "self": value["url"], "git": value["git_url"],
        "html": f"https://github.com/StatPan/datapan-registry/blob/automation/upstream-catalogue-state/{path}",
    }
    return value


def _blob_api(template: dict[str, object], *, raw: bytes) -> dict[str, object]:
    sha = ROLLUP.git_blob_sha1(raw)
    value = copy.deepcopy(template)
    value.update({
        "sha": sha,
        "size": len(raw),
        "content": base64.b64encode(raw).decode("ascii"),
        "encoding": "base64",
        "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{sha}",
    })
    return value


def _refresh_recursive_tree(rows: list[dict[str, object]]) -> str:
    directories = sorted(
        (row["path"] for row in rows if row.get("type") == "tree"),
        key=lambda value: str(value).count("/"), reverse=True,
    )
    for directory in directories:
        children = [row for row in rows if pathlib.PurePosixPath(str(row["path"])).parent.as_posix() == directory]
        children.sort(key=lambda row: (
            pathlib.PurePosixPath(str(row["path"])).name + ("/" if row.get("type") == "tree" else "")
        ).encode("utf-8"))
        body = bytearray()
        for child in children:
            mode = str(child["mode"]).lstrip("0") or "0"
            body.extend(
                mode.encode("ascii") + b" "
                + pathlib.PurePosixPath(str(child["path"])).name.encode("utf-8")
                + b"\0" + bytes.fromhex(str(child["sha"]))
            )
        digest = hashlib.sha1(f"tree {len(body)}\0".encode("ascii") + body).hexdigest()
        entry = next(row for row in rows if row.get("path") == directory)
        entry["sha"] = digest
        entry["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{digest}"
    return HELPERS._git_tree_sha_from_recursive_rows(rows)


def _rebind_state_commit(
    *, index: dict[str, object], input_root: pathlib.Path, stage: str,
    tree_role: str, commit_role: str, ref_role: str,
    original_parent: str, message: str,
) -> str:
    rows = index["inputs"]
    role_items = {
        item["role"]: item for item in rows
        if item.get("scope_id") == OPERATION_SCOPE_ID and item.get("subject", {}).get("stage") == stage
    }
    tree_path = input_root / role_items[tree_role]["path"]
    if not tree_path.is_file():
        tree_path = ROOT / role_items[tree_role]["path"]
    tree_api = json.loads(tree_path.read_bytes())
    tree_rows = copy.deepcopy(tree_api["tree"])
    tree_sha = _refresh_recursive_tree(tree_rows)
    tree_api.update({
        "sha": tree_sha,
        "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}",
        "tree": tree_rows,
    })
    old_commit = json.loads((ROOT / role_items[commit_role]["path"]).read_bytes())
    author = "Completeness fixture <completeness-test@example.invalid>"
    timestamp = "1791110520 +0000"
    commit_raw = (
        f"tree {tree_sha}\nparent {original_parent}\n"
        f"author {author} {timestamp}\ncommitter {author} {timestamp}\n\n{message}\n"
    ).encode("utf-8")
    commit_sha = hashlib.sha1(f"commit {len(commit_raw)}\0".encode("ascii") + commit_raw).hexdigest()
    commit_api = copy.deepcopy(old_commit)
    commit_api.update({
        "sha": commit_sha,
        "tree": {
            **old_commit["tree"], "sha": tree_sha,
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}",
        },
        "parents": [{"sha": original_parent}],
        "message": message,
        "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/commits/{commit_sha}",
    })
    ref_api = json.loads((ROOT / role_items[ref_role]["path"]).read_bytes())
    ref_api["object"]["sha"] = commit_sha
    ref_api["object"]["url"] = commit_api["url"]
    ref_api["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/ref/heads/{ref_api['ref'].removeprefix('refs/heads/')}"
    for role, suffix, value in (
        (tree_role, "tree", tree_api),
        (commit_role, "commit", commit_api),
        (ref_role, "ref", ref_api),
    ):
        HELPERS._write_rebound_evidence(input_root, role_items[role], f"future-{stage}-{suffix}.json", _pretty(value))
    return commit_sha


def _new_source_no_change_artifact() -> tuple[bytes, dict[str, object], int]:
    registry_path = ROOT / "data/data-go-kr.registry.json"
    registry_size = registry_path.stat().st_size
    digest = hashlib.sha256()
    record_count = 0
    with registry_path.open("rb") as source:
        for line in source:
            digest.update(line)
            if line.startswith(b"  {") and not line.startswith(b"   {"):
                record_count += 1
    registry_sha = digest.hexdigest()
    if (registry_size, registry_sha) != (139155499, CURRENT_REGISTRY_SHA256):
        raise AssertionError("fixture canonical registry bytes differ from its pinned no-change baseline")
    observed_at = NEW_A_OBSERVED_AT
    diff_summary = {"added": 0, "removed": 0, "changed": 0, "stable": record_count}
    key_payload = {
        "source_id": "data_go_kr", "status": "no_change",
        "summary": diff_summary, "error_class": None,
    }
    work_key = "upstream-refresh:data_go_kr:" + hashlib.sha256(
        json.dumps(key_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    diff = {
        "generated_at": observed_at, "provider": "data.go.kr",
        "old": "data/data-go-kr.registry.json",
        "new": ".datapan/ci/upstream-refresh/candidate.registry.json",
        "limit": 1200, "truncated": False,
        "counts": {"old": record_count, "new": record_count},
        "summary": diff_summary, "added": [], "removed": [], "changed": [],
    }
    diff_raw = _pretty(diff)
    evidence = {
        "schema_version": "datapan.upstream-refresh-evidence.v1",
        "observed_at": observed_at, "source_id": "data_go_kr", "owner": "release-operator",
        "status": "no_change",
        "collection": {"attempted": True, "succeeded": True, "exit_code": 0, "error_class": None},
        "baseline": {
            "path": "data/data-go-kr.registry.json", "bytes": registry_size,
            "sha256": registry_sha, "records": record_count,
        },
        "snapshot": {
            "path": ".datapan/ci/upstream-refresh/candidate.registry.json", "bytes": registry_size,
            "sha256": registry_sha, "records": record_count,
        },
        "diff": {"path": ".datapan/ci/upstream-refresh/catalog-diff.json", "sha256": _sha(diff_raw), "summary": diff_summary},
        "review": {"action": "none", "work_key": work_key},
        "publication": {
            "automatic": False, "release_allowed": False,
            "required_gates": ["release_manifest_verification", "release_readiness", "consumer_compatibility"],
        },
    }
    work_packet = {
        "schema_version": "datapan.upstream-refresh-work-packet.v1", "source_id": "data_go_kr",
        "observed_at": observed_at, "status": "no_change", "owner": "release-operator",
        "work_key": work_key, "action": "none",
        "evidence": ".datapan/ci/upstream-refresh/upstream-refresh-evidence.json",
        "snapshot": ".datapan/ci/upstream-refresh/candidate.registry.json",
        "diff": ".datapan/ci/upstream-refresh/catalog-diff.json", "automatic_publication": False,
    }
    members = {
        "upstream-refresh-evidence.json": _pretty(evidence),
        "catalog-diff.json": diff_raw,
        "upstream-refresh-work-packet.json": _pretty(work_packet),
    }
    return _zip_streamed_registry(registry_path, members), evidence, record_count


class FutureObservationChainTest(unittest.TestCase):
    """Exercise a synthetic A continuation without adding any live A credit."""

    def _put(self, input_root: pathlib.Path, item: dict[str, object], name: str, raw: bytes) -> None:
        HELPERS._write_rebound_evidence(input_root, item, name, raw)

    @staticmethod
    def _stage_roles(index: dict[str, object], stage: str) -> dict[str, dict[str, object]]:
        return {
            item["role"]: item for item in index["inputs"]
            if item.get("scope_id") == OPERATION_SCOPE_ID and item.get("subject", {}).get("stage") == stage
        }

    @staticmethod
    def _input_copy(input_root: pathlib.Path) -> tuple[dict[str, object], pathlib.Path]:
        index = json.loads((ROOT / ROLLUP.INPUT_INDEX_PATH).read_bytes())
        for item in index["inputs"]:
            if item.get("root") != "evidence":
                continue
            source = ROOT / item["path"]
            target = input_root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                try:
                    target.hardlink_to(source)
                except OSError:
                    shutil.copyfile(source, target)
        return index, input_root / "input-index.json"

    def _rebind_source_a(self, index: dict[str, object], input_root: pathlib.Path) -> tuple[dict[str, object], bytes]:
        roles = self._stage_roles(index, "source")
        source_run = json.loads((ROOT / roles["pipeline_run"]["path"]).read_bytes())
        tree = ROLLUP.git_read_only(ROOT, ["rev-parse", f"{NEW_A_RUN_HEAD}^{{tree}}"]).decode().strip()
        for key, value in {
            "id": int(NEW_A_RUN_ID), "head_sha": NEW_A_RUN_HEAD,
            "created_at": "2026-10-04T13:09:35Z", "run_started_at": "2026-10-04T13:09:35Z",
            "updated_at": "2026-10-04T13:10:20Z", "run_number": 999,
        }.items():
            source_run[key] = value
        source_run["head_commit"] = {**source_run["head_commit"], "id": NEW_A_RUN_HEAD, "tree_id": tree}
        source_run["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/actions/runs/{NEW_A_RUN_ID}"
        source_run["html_url"] = f"https://github.com/StatPan/datapan-registry/actions/runs/{NEW_A_RUN_ID}"
        source_run["jobs_url"] = f"{source_run['url']}/jobs"
        source_run["artifacts_url"] = f"{source_run['url']}/artifacts"
        self._put(input_root, roles["pipeline_run"], "future-a-run.json", _pretty(source_run))

        jobs = json.loads((ROOT / roles["pipeline_jobs"]["path"]).read_bytes())
        job = jobs["jobs"][0]
        job.update({
            "id": 199972210001, "run_id": int(NEW_A_RUN_ID), "run_attempt": 1,
            "head_sha": NEW_A_RUN_HEAD, "run_url": source_run["url"],
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/actions/jobs/199972210001",
            "html_url": f"{source_run['html_url']}/job/199972210001",
            "created_at": "2026-10-04T13:09:40Z", "started_at": "2026-10-04T13:09:45Z",
            "completed_at": "2026-10-04T13:10:20Z",
        })
        for step in job["steps"]:
            name = step.get("name")
            if name == "Observe upstream catalogue without publishing":
                step.update({"started_at": NEW_A_OBSERVED_AT, "completed_at": "2026-10-04T13:10:10Z"})
            elif name == "Upload snapshot, diff, evidence, and work packet":
                step.update({"started_at": "2026-10-04T13:10:15Z", "completed_at": "2026-10-04T13:10:18Z"})
            elif name in {"Set up job", "Checkout registry without LFS", "Materialize manifest-bound canonical registry"}:
                step.update({"started_at": "2026-10-04T13:09:45Z", "completed_at": "2026-10-04T13:09:50Z"})
        jobs["jobs"] = [job]
        self._put(input_root, roles["pipeline_jobs"], "future-a-jobs.json", _pretty(jobs))

        artifact, evidence, records = _new_source_no_change_artifact()
        self._put(input_root, roles["pipeline_artifact_archive"], "future-a-archive.zip", artifact)
        with zipfile.ZipFile(io.BytesIO(artifact)) as source_archive:
            evidence_raw = source_archive.read("upstream-refresh-evidence.json")
            diff_raw = source_archive.read("catalog-diff.json")
        evidence = json.loads(evidence_raw)
        roles["pipeline_artifact_archive"]["observed_at"] = NEW_A_OBSERVED_AT
        metadata = json.loads((ROOT / roles["pipeline_artifact_metadata"]["path"]).read_bytes())
        api_artifact = metadata["artifacts"][0]
        api_artifact.update({
            "id": int(NEW_A_ARTIFACT_ID), "name": f"upstream-catalog-refresh-{NEW_A_RUN_ID}",
            "size_in_bytes": len(artifact), "digest": f"sha256:{_sha(artifact)}",
            "created_at": "2026-10-04T13:10:15Z", "updated_at": "2026-10-04T13:10:18Z",
            "expires_at": "2026-10-25T13:10:00Z", "expired": False,
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/actions/artifacts/{NEW_A_ARTIFACT_ID}",
            "archive_download_url": f"https://api.github.com/repos/StatPan/datapan-registry/actions/artifacts/{NEW_A_ARTIFACT_ID}/zip",
            "workflow_run": {
                "id": int(NEW_A_RUN_ID), "repository_id": 1278568329, "head_repository_id": 1278568329,
                "head_branch": "main", "head_sha": NEW_A_RUN_HEAD,
            },
        })
        self._put(input_root, roles["pipeline_artifact_metadata"], "future-a-artifacts.json", _pretty(metadata))
        for item in roles.values():
            item["producer"]["run_id"] = NEW_A_RUN_ID
            item["producer"]["revision"] = NEW_A_RUN_HEAD
            item["producer"]["attempt"] = 1
            item["observed_at"] = "2026-10-04T13:10:20Z"
        roles["pipeline_artifact_archive"]["observed_at"] = NEW_A_OBSERVED_AT
        return {
            "run": source_run, "evidence": evidence, "evidence_raw": evidence_raw,
            "diff_raw": diff_raw, "records": records, "artifact_sha256": _sha(artifact),
        }, artifact

    def _rebind_processor(self, index: dict[str, object], input_root: pathlib.Path, source: dict[str, object]) -> tuple[str, str]:
        roles = self._stage_roles(index, "processor")
        generation_api = json.loads((ROOT / roles["processor_generation_api"]["path"]).read_bytes())
        checkpoint = json.loads(base64.b64decode(generation_api["content"]))
        original_generation_id = checkpoint["generation_id"]
        uploaded_checkpoint = None
        archive_item = roles["pipeline_artifact_archive"]
        old_archive = (ROOT / archive_item["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(old_archive)) as source_zip:
            original_members = {info.filename: source_zip.read(info.filename) for info in source_zip.infolist()}
        uploaded_checkpoint = json.loads(original_members["upstream-catalogue-checkpoint-receipt.json"])
        composition = json.loads(original_members["composition-receipt.json"])
        composition["input_digests"]["baseline"].update({
            "bytes": 139155499, "sha256": CURRENT_REGISTRY_SHA256,
        })
        composition["input_digests"]["candidate"].update({
            "bytes": 139155499, "sha256": CURRENT_REGISTRY_SHA256,
        })
        composition["input_digests"]["refresh_evidence"].update({
            "bytes": len(source["evidence_raw"]), "sha256": _sha(source["evidence_raw"]),
        })
        composition["input_digests"]["full_diff"].update({
            "bytes": len(source["diff_raw"]),
            "sha256": _sha(source["diff_raw"]),
        })
        composition["producer"]["run_id"] = NEW_A_RUN_ID
        composition["producer"]["run_url"] = f"https://github.com/StatPan/datapan-registry/actions/runs/{NEW_A_RUN_ID}"
        composition_raw = _pretty(composition)

        # Build the new generation from only its own source-validated A
        # observation; Health separately retains the old A in source history.
        input_locator = {
            "run_id": NEW_A_RUN_ID, "name": f"upstream-catalog-refresh-{NEW_A_RUN_ID}",
            "artifact_id": NEW_A_ARTIFACT_ID, "expires_at": "2026-10-25T13:10:00Z",
            "candidate_sha256": CURRENT_REGISTRY_SHA256,
            "evidence_sha256": source["evidence_sha256"], "diff_sha256": source["diff_sha256"],
        }
        # This is a distinct generation because its immutable baseline changed.
        # Its lineage contains only the new A; the Health source history below
        # independently retains the older A observation.
        checkpoint["input_artifacts"] = [input_locator]
        checkpoint["observation_count"] = 1
        checkpoint["observed_at"] = NEW_A_OBSERVED_AT
        checkpoint["last_observation"] = {
            "observed_at": NEW_A_OBSERVED_AT, "producer_run_id": NEW_A_RUN_ID,
            "refresh_evidence_sha256": source["evidence_sha256"],
            "collection_status": "success", "execution_mode": "live",
        }
        generation_inputs = checkpoint["generation_inputs"]
        generation_inputs["baseline_sha256"] = CURRENT_REGISTRY_SHA256
        generation_inputs["candidate_sha256"] = CURRENT_REGISTRY_SHA256
        generation_id = _sha(_canonical(generation_inputs))
        checkpoint["generation_id"] = generation_id

        result = json.loads(original_members["upstream-catalogue-processing-result.json"])
        result.update({
            "generation_id": generation_id, "observed_at": NEW_A_OBSERVED_AT,
            "producer_run_id": NEW_A_RUN_ID,
        })
        result_raw = _pretty(result)
        checkpoint["output_digests"] = [
            {**row, "bytes": len(composition_raw), "sha256": _sha(composition_raw)}
            if row["path"] == "composition-receipt.json"
            else {**row, "bytes": len(result_raw), "sha256": _sha(result_raw)}
            if row["path"] == "upstream-catalogue-processing-result.json"
            else row
            for row in checkpoint["output_digests"]
        ]
        checkpoint["output_artifact"]["bundle_manifest_sha256"] = _sha(_canonical(checkpoint["output_digests"]))
        checkpoint.pop("checkpoint_sha256", None)
        checkpoint["checkpoint_sha256"] = _sha(_canonical(checkpoint))
        checkpoint_raw = _canonical(checkpoint)

        uploaded_checkpoint.update(copy.deepcopy(checkpoint))
        uploaded_checkpoint["output_artifact"]["artifact_id"] = None
        uploaded_checkpoint["output_artifact"]["expires_at"] = json.loads(
            original_members["upstream-catalogue-checkpoint-receipt.json"]
        )["output_artifact"]["expires_at"]
        uploaded_checkpoint["last_heartbeat_at"] = json.loads(
            original_members["upstream-catalogue-checkpoint-receipt.json"]
        )["last_heartbeat_at"]
        uploaded_checkpoint.pop("checkpoint_sha256", None)
        uploaded_checkpoint["checkpoint_sha256"] = _sha(_canonical(uploaded_checkpoint))
        uploaded_checkpoint_raw = _canonical(uploaded_checkpoint)

        replacements = {
            "composition-receipt.json": composition_raw,
            "upstream-catalogue-checkpoint-receipt.json": uploaded_checkpoint_raw,
            "upstream-catalogue-processing-result.json": result_raw,
        }
        archive = _zip_rebound(old_archive, replacements)
        self._put(input_root, archive_item, "future-b-archive.zip", archive)
        metadata = json.loads((ROOT / roles["pipeline_artifact_metadata"]["path"]).read_bytes())
        metadata["artifacts"][0]["size_in_bytes"] = len(archive)
        metadata["artifacts"][0]["digest"] = f"sha256:{_sha(archive)}"
        self._put(input_root, roles["pipeline_artifact_metadata"], "future-b-artifacts.json", _pretty(metadata))

        old_path = generation_api["path"]
        new_path = f".datapan/upstream-catalogue-state/sources/data_go_kr/generations/{generation_id}.json"
        generation_api = _contents_api(generation_api, path=new_path, raw=checkpoint_raw)
        generation_api["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/contents/{new_path}?ref=automation/upstream-catalogue-state"

        index_api = json.loads((ROOT / roles["processor_index_api"]["path"]).read_bytes())
        state_index = json.loads(base64.b64decode(index_api["content"]))
        old_rows = [row for row in state_index["generations"] if row.get("generation_id") == original_generation_id]
        if len(old_rows) != 1:
            raise AssertionError("fixture must have one selected generation row to rebind")
        old_entry = old_rows[0]
        old_entry.update({
            "generation_id": generation_id,
            "checkpoint": f"{generation_id}.json",
            "candidate_sha256": CURRENT_REGISTRY_SHA256,
        })
        state_index_raw = _pretty(state_index)
        index_api = _contents_api(index_api, path=index_api["path"], raw=state_index_raw)
        self._put(input_root, roles["processor_generation_api"], "future-b-generation-api.json", _pretty(generation_api))
        self._put(input_root, roles["processor_index_api"], "future-b-index-api.json", _pretty(index_api))

        tree_api = json.loads((ROOT / roles["processor_state_tree"]["path"]).read_bytes())
        tree_rows = copy.deepcopy(tree_api["tree"])
        tree_rows = [row for row in tree_rows if row.get("path") != old_path]
        for row in tree_rows:
            if row.get("path") == index_api["path"]:
                row.update({"sha": index_api["sha"], "size": index_api["size"], "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{index_api['sha']}"})
        tree_rows.append({
            "path": new_path, "mode": "100644", "type": "blob",
            "sha": generation_api["sha"], "size": generation_api["size"],
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{generation_api['sha']}",
        })
        original_commit = json.loads((ROOT / roles["processor_state_commit"]["path"]).read_bytes())
        # Install the final generation/index inventory before sealing the
        # state tree, commit, and ref in one pass.
        tree_api.update({"tree": tree_rows})
        tree_api["sha"] = _refresh_recursive_tree(tree_api["tree"])
        tree_api["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_api['sha']}"
        self._put(input_root, roles["processor_state_tree"], "future-b-state-tree-pre.json", _pretty(tree_api))
        processor_commit = _rebind_state_commit(
            index=index, input_root=input_root, stage="processor",
            tree_role="processor_state_tree", commit_role="processor_state_commit", ref_role="processor_state_ref",
            original_parent=original_commit["sha"], message="Synthetic selected no-change A processor generation",
        )
        # Update the archived pre-run processor ref consumed by Health later.
        return generation_id, processor_commit

    def _rebind_noop_log(self, index: dict[str, object], input_root: pathlib.Path, generation_id: str) -> None:
        roles = self._stage_roles(index, "promotion")
        item = roles["pipeline_log_archive"]
        raw = (ROOT / item["path"]).read_bytes()
        text = gzip.decompress(raw).decode("utf-8")
        changed = False
        lines = []
        for line in text.splitlines():
            marker = line.find("{")
            if marker >= 0 and "already_canonical_generations" in line[marker:]:
                record = json.loads(line[marker:])
                matches = [row for row in record.get("already_canonical_generations", []) if row.get("generation_id") == "8c56b9ad08719bcf99ed9a99d488c26e4bfdf517023a9ca01a548b5d2167e8f9"]
                if len(matches) != 1:
                    raise AssertionError("retained C no-op log must uniquely name the old B generation")
                matches[0]["generation_id"] = generation_id
                lines.append(line[:marker] + json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                changed = True
            else:
                lines.append(line)
        if not changed:
            raise AssertionError("retained C run log has no exact already-canonical generation summary")
        self._put(input_root, item, "future-c-noop.log.gz", gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0))

    def _rebind_health(
        self, index: dict[str, object], input_root: pathlib.Path, source: dict[str, object],
        generation_id: str, checkpoint_sha: str, processor_commit: str,
    ) -> None:
        roles = self._stage_roles(index, "health")
        archive_item = roles["pipeline_artifact_archive"]
        old_archive = (ROOT / archive_item["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(old_archive)) as archive:
            members = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        receipt = json.loads(members["health-receipt.json"])
        pre_state = json.loads(members["health-state/state.json"])
        source_row = next(row for row in receipt["sources"] if row["source_id"] == "data_go_kr")
        age = int((ROLLUP.parse_time(receipt["evaluated_at"], "receipt time") - ROLLUP.parse_time(NEW_A_OBSERVED_AT, "A observed time")).total_seconds())
        source_row["observation"].update({
            "age_seconds": age, "collection_status": "success", "execution_mode": "live",
            "observed_at": NEW_A_OBSERVED_AT, "producer_run_id": NEW_A_RUN_ID,
            "refresh_evidence_sha256": source["evidence_sha256"], "state": "fresh",
        })
        processor = source_row["processor"]
        processor.update({
            "generation_id": generation_id, "checkpoint_sha256": checkpoint_sha,
            "candidate_sha256": CURRENT_REGISTRY_SHA256,
            "last_observation": {
                "collection_status": "success", "execution_mode": "live",
                "observed_at": NEW_A_OBSERVED_AT, "producer_run_id": NEW_A_RUN_ID,
                "refresh_evidence_sha256": source["evidence_sha256"],
            },
        })
        # Keep the original exact 8/9 preservation chain but bind the output
        # locator and no-op candidate to the newly sealed same-payload B.
        b_roles = self._stage_roles(index, "processor")
        b_api = json.loads((input_root / b_roles["processor_generation_api"]["path"]).read_bytes())
        b_checkpoint = json.loads(base64.b64decode(b_api["content"]))
        processor["checkpoint_observed_at"] = b_checkpoint["observed_at"]
        processor["output_artifact"] = copy.deepcopy(b_checkpoint["output_artifact"])
        processor["output_digests"] = copy.deepcopy(b_checkpoint["output_digests"])
        processor["outcome"] = copy.deepcopy(b_checkpoint["outcome"])
        candidate_relation = source_row["canonical"].get("already_canonical_candidate")
        candidate_relation.update({
            "generation_id": generation_id,
            "checkpoint_sha256": checkpoint_sha,
            "output_bundle_sha256": b_checkpoint["output_artifact"]["bundle_manifest_sha256"],
            "composed_registry_sha256": CURRENT_REGISTRY_SHA256,
            "composed_registry_bytes": 139155499,
            "processor_run_id": str(b_checkpoint["output_artifact"]["run_id"]),
            "processor_run_attempt": 1,
            "artifact_id": str(b_checkpoint["output_artifact"]["artifact_id"]),
        })

        # The retained pre-state already contains the older A observation. The
        # producer replay below must append the new run without replacing it.
        persister = ROLLUP.import_health_persister_at_revision(ROOT, receipt["health_workflow"]["revision"])
        receipt = persister.seal(receipt, "receipt_sha256")
        checker = json.loads(members["checker-result.json"])
        checker["receipt_sha256"] = receipt["receipt_sha256"]
        members["health-receipt.json"] = _pretty(receipt)
        members["checker-result.json"] = _pretty(checker)
        post_state = ROLLUP.expected_health_post_state(
            root=ROOT, workflow_head=receipt["health_workflow"]["revision"],
            pre_state=pre_state, receipt=receipt,
        )
        post_state_raw = _pretty(post_state)
        receipt_raw = members["health-receipt.json"]
        receipt_name = persister.receipt_file_name(receipt)
        members["processor-ref"] = (
            f"{processor_commit}\trefs/heads/automation/upstream-catalogue-state\n"
        ).encode("ascii")

        tree_item = roles["health_state_tree"]
        tree_api = json.loads((ROOT / tree_item["path"]).read_bytes())
        receipt_api_old = json.loads((ROOT / roles["health_receipt_blob_api"]["path"]).read_bytes())
        old_receipt_rows = [row for row in tree_api["tree"] if row.get("sha") == receipt_api_old.get("sha")]
        if len(old_receipt_rows) != 1:
            raise AssertionError("health tree must identify the previous receipt blob once")
        state_path = "health/upstream-catalogue/state.json"
        receipt_path = f"health/upstream-catalogue/receipts/{receipt_name}"
        state_api = _blob_api(json.loads((ROOT / roles["health_state_blob_api"]["path"]).read_bytes()), raw=post_state_raw)
        state_api.update({"path": state_path})
        state_api["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{state_api['sha']}"
        receipt_api = _blob_api(receipt_api_old, raw=receipt_raw)
        receipt_api.update({"path": receipt_path})
        receipt_api["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{receipt_api['sha']}"
        tree_rows = [row for row in copy.deepcopy(tree_api["tree"]) if row.get("path") != old_receipt_rows[0]["path"]]
        replacement = {
            state_path: (state_api["sha"], state_api["size"]),
            receipt_path: (receipt_api["sha"], receipt_api["size"]),
        }
        for row in tree_rows:
            path = row.get("path")
            if path in replacement:
                row.update({
                    "sha": replacement[path][0], "size": replacement[path][1],
                    "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{replacement[path][0]}",
                })
        if receipt_path not in {row.get("path") for row in tree_rows}:
            tree_rows.append({
                "path": receipt_path, "mode": "100644", "type": "blob",
                "sha": receipt_api["sha"], "size": receipt_api["size"],
                "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{receipt_api['sha']}",
            })
        tree_api["tree"] = tree_rows
        tree_sha = _refresh_recursive_tree(tree_rows)
        tree_api.update({"sha": tree_sha, "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}"})
        old_commit = json.loads((ROOT / roles["health_state_commit"]["path"]).read_bytes())
        parent = members["health-ref"].decode("ascii").strip().split("\t")[0]
        author = "Completeness fixture <completeness-test@example.invalid>"
        timestamp = "1791110520 +0000"
        commit_raw = (
            f"tree {tree_sha}\nparent {parent}\nauthor {author} {timestamp}\n"
            f"committer {author} {timestamp}\n\nSynthetic future-observation Health state\n"
        ).encode()
        health_commit = hashlib.sha1(f"commit {len(commit_raw)}\0".encode() + commit_raw).hexdigest()
        commit_api = copy.deepcopy(old_commit)
        commit_api.update({
            "sha": health_commit,
            "tree": {**old_commit["tree"], "sha": tree_sha, "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}"},
            "parents": [{"sha": parent}], "message": "Synthetic future-observation Health state",
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/commits/{health_commit}",
        })
        ref_api = json.loads((ROOT / roles["health_state_ref"]["path"]).read_bytes())
        ref_api["object"]["sha"] = health_commit
        ref_api["object"]["url"] = commit_api["url"]
        # The archive records the parent ref observed before persistence;
        # the API ref below records the post-persist commit.
        members["health-state/state.json"] = members["health-state/state.json"]
        members["health-receipt.json"] = receipt_raw
        archive = _zip_rebound(old_archive, members)
        self._put(input_root, archive_item, "future-health-archive.zip", archive)
        metadata = json.loads((ROOT / roles["pipeline_artifact_metadata"]["path"]).read_bytes())
        metadata["artifacts"][0]["size_in_bytes"] = len(archive)
        metadata["artifacts"][0]["digest"] = f"sha256:{_sha(archive)}"
        self._put(input_root, roles["pipeline_artifact_metadata"], "future-health-artifacts.json", _pretty(metadata))
        self._put(input_root, roles["health_state_blob_api"], "future-health-post-state-blob.json", _pretty(state_api))
        self._put(input_root, roles["health_receipt_blob_api"], "future-health-receipt-blob.json", _pretty(receipt_api))
        # _rebind_state_commit reads the current state tree from repository, so
        # write the actual rebound recursive tree before resealing ref/commit.
        self._put(input_root, roles["health_state_tree"], "future-health-tree-pre.json", _pretty(tree_api))
        self._put(input_root, roles["health_state_commit"], "future-health-commit-pre.json", _pretty(commit_api))
        self._put(input_root, roles["health_state_ref"], "future-health-ref-pre.json", _pretty(ref_api))
        # Bind blob rows in the tree to the two exact retained API blobs.
        final_rows = copy.deepcopy(tree_api["tree"])
        for path, api in ((state_path, state_api), (receipt_path, receipt_api)):
            matches = [row for row in final_rows if row.get("path") == path]
            if len(matches) != 1:
                raise AssertionError(f"health state tree must contain {path} exactly once")
            matches[0].update({"sha": api["sha"], "size": api["size"], "url": api["url"]})
        tree_api["tree"] = final_rows
        tree_api["sha"] = _refresh_recursive_tree(final_rows)
        self._put(input_root, roles["health_state_tree"], "future-health-tree.json", _pretty(tree_api))
        # Finalize a real Git-object-shaped commit against the final tree.
        final_tree_sha = tree_api["sha"]
        final_commit_raw = (
            f"tree {final_tree_sha}\nparent {parent}\nauthor {author} {timestamp}\n"
            f"committer {author} {timestamp}\n\nSynthetic future-observation Health state\n"
        ).encode()
        health_commit = hashlib.sha1(f"commit {len(final_commit_raw)}\0".encode() + final_commit_raw).hexdigest()
        commit_api["sha"] = health_commit
        commit_api["tree"]["sha"] = final_tree_sha
        commit_api["tree"]["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{final_tree_sha}"
        commit_api["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/commits/{health_commit}"
        ref_api["object"]["sha"] = health_commit
        ref_api["object"]["url"] = commit_api["url"]
        # The archive must be repacked after final Health ref is known.
        archive = _zip_rebound(old_archive, {
            "health-receipt.json": receipt_raw,
            "checker-result.json": _pretty(checker),
            "health-ref": members["health-ref"],
            "health-state/state.json": _pretty(pre_state),
            "processor-ref": members["processor-ref"],
        })
        self._put(input_root, archive_item, "future-health-archive-final.zip", archive)
        metadata["artifacts"][0]["size_in_bytes"] = len(archive)
        metadata["artifacts"][0]["digest"] = f"sha256:{_sha(archive)}"
        self._put(input_root, roles["pipeline_artifact_metadata"], "future-health-artifacts-final.json", _pretty(metadata))
        self._put(input_root, roles["health_state_tree"], "future-health-tree-final.json", _pretty(tree_api))
        self._put(input_root, roles["health_state_commit"], "future-health-commit-final.json", _pretty(commit_api))
        self._put(input_root, roles["health_state_ref"], "future-health-ref-final.json", _pretty(ref_api))

    def _build_chain(self, input_root: pathlib.Path) -> tuple[pathlib.Path, dict[str, object], str]:
        index, index_path = self._input_copy(input_root)
        # Publication and ACK are a separate historical delivery chain. This
        # scenario exercises only A -> B -> C no-op -> Health, so keep those
        # optional stages absent instead of fabricating their state commits.
        index["inputs"] = [
            item for item in index["inputs"]
            if not (
                item.get("scope_id") == OPERATION_SCOPE_ID
                and (
                    item.get("subject", {}).get("stage") in {"publisher", "acknowledgement"}
                    or item.get("role", "").startswith(("publication_", "acknowledgement_"))
                )
            )
        ]
        source_info, _source_zip = self._rebind_source_a(index, input_root)
        source_info["evidence_sha256"] = _sha(source_info["evidence_raw"])
        source_info["diff_sha256"] = _sha(source_info["diff_raw"])
        if source_info["evidence"]["diff"]["sha256"] != source_info["diff_sha256"]:
            raise AssertionError("synthetic A evidence does not bind its exact ZIP diff member")
        generation_id, b_commit = self._rebind_processor(index, input_root, source_info)
        # Read the final B API fields after the durable state lineage is re-bound.
        b_roles = self._stage_roles(index, "processor")
        b_api_item = b_roles["processor_generation_api"]
        b_api = json.loads((input_root / b_api_item["path"]).read_bytes())
        checkpoint = json.loads(base64.b64decode(b_api["content"]))
        self._rebind_noop_log(index, input_root, generation_id)
        self._rebind_health(index, input_root, source_info, generation_id, checkpoint["checkpoint_sha256"], b_commit)
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        return index_path, index, generation_id

    def test_future_no_change_observation_flows_through_b_c_noop_and_health(self) -> None:
        self.assertNotIn(NEW_A_RUN_ID, {"36646768289", "37204681028", "37205271032", "37205341388"})
        with tempfile.TemporaryDirectory(prefix="completeness-future-observation-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, index, expected_generation = self._build_chain(input_root)
            first = ROLLUP.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)
            replay = ROLLUP.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

            self.assertEqual(ROLLUP.render_json(first), ROLLUP.render_json(replay))
            operation = next(row for row in first["scopes"] if row["scope_id"] == OPERATION_SCOPE_ID)
            facets = {row["facet_id"]: row for row in operation["facets"]}
            source_facet = facets["source_observation"]
            pipeline = facets["specification_pipeline"]
            health_facet = facets["health_observation"]
            delivery_facet = facets["immutable_publication_read_back"]
            details = pipeline["details"]
            self.assertEqual(source_facet["details"]["run_id"], NEW_A_RUN_ID)
            self.assertTrue(source_facet["details"]["observation_authenticated"])
            self.assertEqual(source_facet["details"]["candidate_sha256"], CURRENT_REGISTRY_SHA256)
            self.assertEqual(source_facet["state"], "historical")
            self.assertEqual(pipeline["state"], "historical")
            self.assertEqual(details["source_run_id"], NEW_A_RUN_ID)
            self.assertEqual(details["source_observed_at"], NEW_A_OBSERVED_AT)
            self.assertEqual(details["processor_generation_id"], expected_generation)
            self.assertEqual(details["candidate_sha256"], CURRENT_REGISTRY_SHA256)
            self.assertEqual(details["candidate_bytes"], 139155499)
            self.assertEqual(details["pending_count"], 4382)
            self.assertEqual(details["detail_retry_count"], 908)
            self.assertEqual(details["detail_unattempted_count"], 884)
            self.assertFalse(details["current_input_contract_compatible"])
            self.assertEqual(details["promotion_candidate_available"], False)
            self.assertEqual(details["promotion_processor_generation_matches"], True)
            self.assertEqual(details["health_source_observation_matches"], True)
            self.assertEqual(details["health_processor_observation_matches"], True)
            self.assertEqual(details["health_candidate_relation_valid"], True)
            self.assertEqual(details["health_observation_count"], 2)
            self.assertEqual(health_facet["details"]["observation_count"], 2)
            self.assertTrue(health_facet["details"]["health_is_not_new_source_observation"])
            self.assertEqual(delivery_facet["state"], "missing")
            self.assertEqual(set(delivery_facet["details"]["missing_stages"]), {"publisher", "acknowledgement"})
            self.assertEqual(operation["claims"], {"complete": False, "current": False, "updated": False})

            # Verify the retained prior observation itself, not only the count.
            roles = self._stage_roles(index, "health")
            archive_item = roles["pipeline_artifact_archive"]
            with zipfile.ZipFile(input_root / archive_item["path"]) as archive:
                health_receipt = json.loads(archive.read("health-receipt.json"))
                self.assertEqual(
                    health_receipt["sources"][0]["observation"]["producer_run_id"], NEW_A_RUN_ID,
                )
            state_api = json.loads((input_root / roles["health_state_blob_api"]["path"]).read_bytes())
            post_state = json.loads(base64.b64decode(state_api["content"]))
            history = post_state["observations_by_source"]["data_go_kr"]
            observed_runs = {row["producer_run_id"] for row in history}
            self.assertEqual(len(history), 2)
            self.assertIn("36646768289", observed_runs)
            self.assertIn(NEW_A_RUN_ID, observed_runs)

            # Replay the exact receipt against the persisted POST state using
            # the producer-owned merge function. It must neither append a
            # duplicate observation nor advance any timestamp/seal.
            observation_rows_before = [
                (row["producer_run_id"], row["refresh_evidence_sha256"], row["observed_at"])
                for row in history
            ]
            redelivery_state = copy.deepcopy(post_state)
            receipt_before = copy.deepcopy(health_receipt)
            state_before = copy.deepcopy(redelivery_state)
            persister = ROLLUP.import_health_persister_at_revision(
                ROOT, health_receipt["health_workflow"]["revision"],
            )
            persister.merge_observations(redelivery_state, health_receipt)
            self.assertEqual(redelivery_state, state_before)
            self.assertEqual(health_receipt, receipt_before)
            self.assertEqual(
                [
                    (row["producer_run_id"], row["refresh_evidence_sha256"], row["observed_at"])
                    for row in redelivery_state["observations_by_source"]["data_go_kr"]
                ],
                observation_rows_before,
            )

            b_roles = self._stage_roles(index, "processor")
            b_api = json.loads((input_root / b_roles["processor_generation_api"]["path"]).read_bytes())
            checkpoint = json.loads(base64.b64decode(b_api["content"]))
            source_roles = self._stage_roles(index, "source")
            with zipfile.ZipFile(input_root / source_roles["pipeline_artifact_archive"]["path"]) as source_zip:
                exact_diff_raw = source_zip.read("catalog-diff.json")
            with zipfile.ZipFile(input_root / b_roles["pipeline_artifact_archive"]["path"]) as b_zip:
                composition = json.loads(b_zip.read("composition-receipt.json"))
            full_diff_input = composition["input_digests"]["full_diff"]
            self.assertEqual(full_diff_input["bytes"], len(exact_diff_raw))
            self.assertEqual(full_diff_input["sha256"], _sha(exact_diff_raw))
            self.assertEqual(checkpoint["observation_count"], 1)
            self.assertEqual(checkpoint["last_observation"]["producer_run_id"], NEW_A_RUN_ID)
            self.assertEqual([row["run_id"] for row in checkpoint["input_artifacts"]], [NEW_A_RUN_ID])
            self.assertEqual(checkpoint["outcome"]["pending_count"], 4382)
            self.assertEqual(checkpoint["outcome"]["detail_retry_count"], 908)
            self.assertEqual(checkpoint["outcome"]["detail_unattempted_count"], 884)
            self.assertFalse(any(operation.get("claims", {}).get(key) is True for key in ("complete", "current", "updated")))

    def test_conflicting_same_run_evidence_cannot_replace_selected_observation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="completeness-conflicting-observation-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, index, _generation = self._build_chain(input_root)
            conflict = copy.deepcopy(index)
            roles = self._stage_roles(conflict, "source")
            archive_item = roles["pipeline_artifact_archive"]
            with zipfile.ZipFile(input_root / archive_item["path"]) as source_zip:
                members = {member.filename: source_zip.read(member.filename) for member in source_zip.infolist()}
            conflicting_time = "2026-10-04T13:10:01Z"
            evidence = json.loads(members["upstream-refresh-evidence.json"])
            diff = json.loads(members["catalog-diff.json"])
            packet = json.loads(members["upstream-refresh-work-packet.json"])
            diff["generated_at"] = conflicting_time
            diff_raw = _pretty(diff)
            evidence["observed_at"] = conflicting_time
            evidence["diff"]["sha256"] = _sha(diff_raw)
            packet["observed_at"] = conflicting_time
            members["catalog-diff.json"] = diff_raw
            members["upstream-refresh-evidence.json"] = _pretty(evidence)
            members["upstream-refresh-work-packet.json"] = _pretty(packet)
            mutated_archive = io.BytesIO()
            with zipfile.ZipFile(mutated_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as target:
                for member, raw in members.items():
                    if member == "candidate.registry.json":
                        with (ROOT / "data/data-go-kr.registry.json").open("rb") as source, target.open(member, "w", force_zip64=True) as destination:
                            shutil.copyfileobj(source, destination, length=1024 * 1024)
                    else:
                        target.writestr(member, raw)
            self._put(input_root, archive_item, "conflicting-same-run-source.zip", mutated_archive.getvalue())
            metadata_item = roles["pipeline_artifact_metadata"]
            metadata = json.loads((input_root / metadata_item["path"]).read_bytes())
            metadata["artifacts"][0]["size_in_bytes"] = len(mutated_archive.getvalue())
            metadata["artifacts"][0]["digest"] = f"sha256:{_sha(mutated_archive.getvalue())}"
            self._put(input_root, metadata_item, "conflicting-same-run-artifacts.json", _pretty(metadata))
            archive_item["observed_at"] = conflicting_time
            jobs_item = roles["pipeline_jobs"]
            jobs = json.loads((input_root / jobs_item["path"]).read_bytes())
            observe_step = next(
                step for step in jobs["jobs"][0]["steps"]
                if step.get("name") == "Observe upstream catalogue without publishing"
            )
            observe_step["started_at"] = conflicting_time
            self._put(input_root, jobs_item, "conflicting-same-run-jobs.json", _pretty(jobs))
            (input_root / "conflicting-index.json").write_text(json.dumps(conflict, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "processor latest collector locator differs from the exact validated source artifact"):
                ROLLUP.build_report(root=ROOT, input_root=input_root, input_index_path=input_root / "conflicting-index.json")

    def test_source_manifest_mismatch_cannot_flow_into_b(self) -> None:
        with tempfile.TemporaryDirectory(prefix="completeness-source-manifest-mismatch-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, index, _generation = self._build_chain(input_root)
            mismatch = copy.deepcopy(index)
            roles = self._stage_roles(mismatch, "source")
            run = json.loads((input_root / roles["pipeline_run"]["path"]).read_bytes())
            run["head_sha"] = "4321df1868754045ff3705b5c133c9fad2abde6e"
            run["head_commit"]["id"] = run["head_sha"]
            self._put(input_root, roles["pipeline_run"], "source-wrong-manifest-head.json", _pretty(run))
            for item in roles.values():
                item["producer"]["revision"] = run["head_sha"]
            jobs = json.loads((input_root / roles["pipeline_jobs"]["path"]).read_bytes())
            jobs["jobs"][0]["head_sha"] = run["head_sha"]
            self._put(input_root, roles["pipeline_jobs"], "source-wrong-manifest-jobs.json", _pretty(jobs))
            metadata = json.loads((input_root / roles["pipeline_artifact_metadata"]["path"]).read_bytes())
            metadata["artifacts"][0]["workflow_run"]["head_sha"] = run["head_sha"]
            self._put(input_root, roles["pipeline_artifact_metadata"], "source-wrong-manifest-artifacts.json", _pretty(metadata))
            (input_root / "source-wrong-manifest-index.json").write_text(json.dumps(mismatch, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "collector baseline does not match the source revision's manifest and LFS pointer"):
                ROLLUP.build_report(
                    root=ROOT, input_root=input_root,
                    input_index_path=input_root / "source-wrong-manifest-index.json",
                )


if __name__ == "__main__":
    unittest.main()
