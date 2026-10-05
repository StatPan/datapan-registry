from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "generate-completeness-proof-rollup.py"
SPEC = importlib.util.spec_from_file_location("generate_completeness_proof_rollup", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CompletenessProofRollupTest(unittest.TestCase):
    @staticmethod
    def _hardlink_worktree(source: pathlib.Path, destination: pathlib.Path) -> None:
        skipped = {".git", ".datapan", "__pycache__", ".pytest_cache"}
        for current, dirs, files in os.walk(source):
            current_path = pathlib.Path(current)
            relative = current_path.relative_to(source)
            dirs[:] = [name for name in dirs if name not in skipped]
            target_dir = destination / relative
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in files:
                if name in skipped:
                    continue
                source_file = current_path / name
                target_file = target_dir / name
                if source_file.name == ".git" or source_file.is_dir():
                    continue
                if source_file.is_symlink():
                    target_file.symlink_to(os.readlink(source_file))
                    continue
                try:
                    os.link(source_file, target_file)
                except OSError:
                    shutil.copy2(source_file, target_file)

    @staticmethod
    def _write_json_file(root: pathlib.Path, relative: str, value: object) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            path.unlink()
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _write_rebound_evidence(
        input_root: pathlib.Path, item: dict[str, object], filename: str, raw: bytes,
    ) -> None:
        path = input_root / filename
        path.write_bytes(raw)
        item.update({
            "root": "evidence", "path": filename,
            "bytes": len(raw), "sha256": MODULE.sha256_bytes(raw),
        })

    def _build_synthetic_native_ack_packet(
        self, input_root: pathlib.Path, *, outcome: str = "already_acknowledged",
        publisher_identity: tuple[int, int] | None = None,
        acknowledgement_identity: tuple[int, int] | None = None,
        acknowledgement_event: str = "schedule",
        predecessor_status: str | None = None,
    ) -> tuple[pathlib.Path, str]:
        """Build an indexed native publisher/ACK packet with a nonlegacy ACK identity."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        # Keep the exact publisher and its full immutable read-back packet, but
        # omit the independent A/B/C/Health links so this test covers only the
        # generic native publisher + ACK contract and cannot claim completeness.
        core_stages = {"source", "processor", "promotion", "health"}
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_id
            or (
                item.get("subject", {}).get("stage") not in core_stages
                and item.get("role") != "proof_v1"
            )
        ]
        ack_rows = [
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id
            and (
                item.get("subject", {}).get("stage") == "acknowledgement"
                or item.get("role") in {"acknowledgement_journal_before", "acknowledgement_journal_after"}
            )
        ]
        ack_roles = {item["role"]: item for item in ack_rows}
        current_run_id, current_attempt = acknowledgement_identity or (99972290001, 1)
        current_url = (
            f"https://github.com/StatPan/datapan-registry/actions/runs/{current_run_id}"
            f"/attempts/{current_attempt}"
        )
        current_start = "2026-10-04T13:20:00Z"
        job_start = "2026-10-04T13:20:10Z"
        job_finish = "2026-10-04T13:22:00Z"
        current_revision = "e34062309a48b0e0b6c0f38add32f0cdec088616"

        source_rows = [
            item for item in json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())["inputs"]
            if item.get("scope_id") == operation_id
        ]
        source_by_role = {item["role"]: item for item in source_rows}

        if publisher_identity is not None:
            publisher_run_id, publisher_attempt = publisher_identity
            if publisher_run_id < 1 or publisher_attempt < 1:
                raise AssertionError("synthetic publisher identity must be positive")

            def rebind_publisher_json(role: str, filename: str, update: object) -> None:
                item = next(
                    row for row in index["inputs"]
                    if row.get("scope_id") == operation_id and row["role"] == role
                )
                value = json.loads((ROOT / source_by_role[role]["path"]).read_bytes())
                update(value)
                self._write_rebound_evidence(
                    input_root, item, filename,
                    (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )

            rebind_publisher_json(
                "publication_producer_run", "synthetic-publisher-run.json",
                lambda value: value.update({"id": publisher_run_id, "run_attempt": publisher_attempt}),
            )

            def rebind_jobs(value: dict[str, object]) -> None:
                jobs_value = value.get("jobs")
                if not isinstance(jobs_value, list) or len(jobs_value) != 1 or not isinstance(jobs_value[0], dict):
                    raise AssertionError("retained publisher must have one exact successful job")
                jobs_value[0].update({"run_id": publisher_run_id, "run_attempt": publisher_attempt})

            rebind_publisher_json("publication_producer_jobs", "synthetic-publisher-jobs.json", rebind_jobs)

            def rebind_artifacts(value: dict[str, object]) -> None:
                artifacts = value.get("artifacts")
                if not isinstance(artifacts, list) or not artifacts:
                    raise AssertionError("retained publisher must have an exact artifact list")
                for artifact in artifacts:
                    if not isinstance(artifact, dict) or not isinstance(artifact.get("workflow_run"), dict):
                        raise AssertionError("retained publisher artifact lacks its workflow run identity")
                    artifact["workflow_run"]["id"] = publisher_run_id

            rebind_publisher_json("publication_output_artifact", "synthetic-publisher-artifacts.json", rebind_artifacts)

            def rebind_readback(value: dict[str, object]) -> None:
                value["publisher_run_id"] = publisher_run_id
                value["publisher_attempt"] = publisher_attempt
                checks = value.get("checks")
                if isinstance(checks, list):
                    for check in checks:
                        if not isinstance(check, dict) or not isinstance(check.get("detail"), dict):
                            continue
                        if check.get("check") == "publisher_run_is_exact_completed_success":
                            check["detail"]["id"] = publisher_run_id
                            check["detail"]["run_attempt"] = publisher_attempt

            rebind_publisher_json(
                "publication_anonymous_payload", "synthetic-publisher-readback.json", rebind_readback,
            )
            for item in index["inputs"]:
                if item.get("scope_id") != operation_id or item.get("subject", {}).get("stage") != "publisher":
                    continue
                item["producer"].update({
                    "run_id": str(publisher_run_id), "attempt": publisher_attempt,
                    "workflow_id": 311133646,
                    "workflow_path": ".github/workflows/huggingface-distribution.yml",
                    "event": "workflow_dispatch", "revision": "e34062309a48b0e0b6c0f38add32f0cdec088616",
                })

        run = json.loads((ROOT / source_by_role["acknowledgement_run"]["path"]).read_bytes())
        run.update({
            "id": current_run_id, "event": acknowledgement_event, "run_attempt": current_attempt,
            "status": "completed", "conclusion": "success", "head_sha": current_revision,
            "head_branch": "main", "created_at": current_start,
            "updated_at": job_finish, "run_started_at": current_start,
            "run_completed_at": job_finish,
        })
        jobs = json.loads((ROOT / source_by_role["acknowledgement_jobs"]["path"]).read_bytes())
        if jobs.get("total_count") != 1 or len(jobs.get("jobs", [])) != 1:
            raise AssertionError("retained ACK packet must have exactly one successful reconcile job")
        jobs["jobs"][0].update({
            "run_id": current_run_id, "run_attempt": current_attempt,
            "head_sha": current_revision, "head_branch": "main",
            "status": "completed", "conclusion": "success",
            "started_at": job_start, "completed_at": job_finish,
        })

        before_path = ROOT / source_by_role["acknowledgement_journal_before"]["path"]
        after_path = ROOT / source_by_role["acknowledgement_journal_after"]["path"]
        actual_before = json.loads(before_path.read_bytes())
        actual_after = json.loads(after_path.read_bytes())
        publisher_receipt = json.loads((ROOT / source_by_role["publication_receipt"]["path"]).read_bytes())
        publisher_record = publisher_receipt["publication"]
        source_sha = publisher_receipt["source_binding"]["source_sha"]
        manifest_sha = publisher_receipt["source_binding"]["manifest_sha256"]
        if outcome == "already_acknowledged":
            journal_before = copy.deepcopy(actual_after)
            journal_after = copy.deepcopy(actual_after)
            log_status = "already_acknowledged"
            journal_writes = 0
        elif outcome == "read-back-confirmed":
            if predecessor_status is None:
                journal_before = copy.deepcopy(actual_before)
                journal_after = copy.deepcopy(actual_after)
                catalog_rows = [
                    row for row in journal_after["records"]
                    if row.get("candidate", {}).get("manifest_sha256") == manifest_sha
                    and row.get("pr", {}).get("merge_commit_sha") == source_sha
                ]
                if len(catalog_rows) != 1:
                    raise AssertionError("retained journal must identify the exact publisher source once")
                old_acks = journal_before["records"][journal_after["records"].index(catalog_rows[0])]["acknowledgements"]
                row = catalog_rows[0]
                appended = row["acknowledgements"][len(old_acks):]
                if [item.get("status") for item in appended] != [
                    "publication-pending", "published", "read-back-confirmed",
                ]:
                    raise AssertionError("retained first ACK must have its exact three-event suffix")
                observation_times = [
                    "2026-10-04T13:20:20Z", "2026-10-04T13:21:00Z", "2026-10-04T13:21:40Z",
                ]
                for acknowledgement, observed_at in zip(appended, observation_times, strict=True):
                    acknowledgement.update({
                        "run_id": current_run_id, "run_attempt": current_attempt,
                        "run_url": current_url, "observed_at": observed_at,
                    })
                journal_before["updated_at"] = "2026-10-04T12:00:00Z"
                journal_after["updated_at"] = "2026-10-04T13:21:45Z"
            else:
                if predecessor_status not in {
                    "prepared", "pending-review", "merged", "publication-pending", "published",
                }:
                    raise AssertionError(f"unsupported synthetic ACK predecessor: {predecessor_status}")
                owner_source = MODULE.git_read_only(
                    ROOT, ["show", f"{current_revision}:scripts/canonical_update_pr.py"],
                )
                with tempfile.TemporaryDirectory(prefix="completeness-owner-ack-fixture-") as temporary:
                    owner_path = pathlib.Path(temporary) / "canonical_update_pr.py"
                    owner_path.write_bytes(owner_source)
                    owner = MODULE.import_module("synthetic_ack_owner", owner_path)
                exact_merged_rows = [
                    row for row in actual_before["records"]
                    if row.get("candidate", {}).get("manifest_sha256") == manifest_sha
                    and row.get("pr", {}).get("merge_commit_sha") == source_sha
                ]
                if len(exact_merged_rows) != 1:
                    raise AssertionError("retained journal must identify the exact publisher source once")
                prepared = copy.deepcopy(exact_merged_rows[0])
                pr_number = prepared.get("pr", {}).get("number")
                if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
                    raise AssertionError("retained candidate must have an exact positive PR number")
                prepared["status"] = "prepared"
                prepared["acknowledgements"] = []
                prepared["pr"] = {
                    "number": pr_number,
                    "url": f"https://github.com/StatPan/datapan-registry/pull/{pr_number}",
                    "state": "open", "merge_commit_sha": None,
                }
                for key in ("refresh_from", "refresh_target_main_sha", "superseded_by"):
                    prepared.pop(key, None)
                prior_url = "https://github.com/StatPan/datapan-registry/actions/runs/99972289999/attempts/1"
                prior_observed = "2026-10-04T13:19:20Z"
                pr_readback = {
                    "number": pr_number,
                    "url": f"https://github.com/StatPan/datapan-registry/pull/{pr_number}",
                    "body": prepared["ownership"]["body"],
                    "headRefName": prepared["ownership"]["branch"],
                    "baseRefName": "main",
                    "headRefOid": prepared["candidate"]["head_sha"],
                    "state": "MERGED",
                    "mergeCommit": {"oid": source_sha},
                }
                if predecessor_status == "prepared":
                    before_row = prepared
                elif predecessor_status == "pending-review":
                    before_row = owner.record_pr_readback(
                        copy.deepcopy(prepared), {**pr_readback, "state": "OPEN", "mergeCommit": None},
                        observed_at=prior_observed, run_url=prior_url,
                    )
                else:
                    merged_row = owner.record_pr_readback(
                        copy.deepcopy(prepared), pr_readback,
                        observed_at=prior_observed, run_url=prior_url,
                    )
                    if predecessor_status == "merged":
                        before_row = merged_row
                    else:
                        receipt_path = ROOT / source_by_role["publication_receipt"]["path"]
                        reconciled = owner.reconcile_huggingface_publication(
                            copy.deepcopy(merged_row), receipt_path,
                            observed_at=prior_observed, run_url=prior_url,
                        )
                        prior_suffix_length = 1 if predecessor_status == "publication-pending" else 2
                        prefix_length = len(merged_row["acknowledgements"]) + prior_suffix_length
                        before_row = copy.deepcopy(reconciled)
                        before_row["status"] = predecessor_status
                        before_row["acknowledgements"] = reconciled["acknowledgements"][:prefix_length]

                before_updated = "2026-10-04T13:19:45Z"
                after_row = copy.deepcopy(before_row)
                if predecessor_status in {"prepared", "pending-review"}:
                    after_row = owner.record_pr_readback(
                        after_row, pr_readback,
                        observed_at="2026-10-04T13:20:20Z", run_url=current_url,
                    )
                after_row = owner.reconcile_huggingface_publication(
                    after_row, ROOT / source_by_role["publication_receipt"]["path"],
                    observed_at="2026-10-04T13:20:30Z", run_url=current_url,
                )
                journal_before = {
                    "schema_version": actual_before["schema_version"],
                    "repository": actual_before["repository"],
                    "updated_at": before_updated,
                    "records": [before_row],
                }
                journal_after = {
                    "schema_version": actual_after["schema_version"],
                    "repository": actual_after["repository"],
                    "updated_at": "2026-10-04T13:21:45Z",
                    "records": [after_row],
                }
            log_status = "read-back-confirmed"
            journal_writes = None
        else:
            raise AssertionError(f"unsupported synthetic ACK outcome: {outcome}")

        def pretty(value: object) -> bytes:
            return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()

        source_stage_roles = {
            item["role"]: item for item in source_rows
            if item.get("subject", {}).get("stage") == "promotion"
        }
        base_tree = json.loads((ROOT / source_stage_roles["promotion_state_tree"]["path"]).read_bytes())
        base_commit = json.loads((ROOT / source_stage_roles["promotion_state_commit"]["path"]).read_bytes())
        base_ref = json.loads((ROOT / source_stage_roles["promotion_state_ref"]["path"]).read_bytes())
        base_blob = json.loads((ROOT / source_stage_roles["promotion_journal_blob_api"]["path"]).read_bytes())
        base_journal = MODULE.base64.b64decode(base_blob["content"])
        if base_journal != pretty(actual_after):
            # The stored evidence files retain source formatting. Compare JSON
            # meaning here; the rebuilder uses byte-exact snapshots below.
            if json.loads(base_journal) != actual_after:
                raise AssertionError("promotion state tree must contain the retained exact ACK after journal")

        def state_bundle(journal_raw: bytes, label: str) -> dict[str, bytes]:
            tree_api = copy.deepcopy(base_tree)
            tree_rows = copy.deepcopy(tree_api["tree"])
            journal_rows = [
                row for row in tree_rows
                if row.get("path") == "reports/canonical-update-promotion-receipt.json"
            ]
            if len(journal_rows) != 1:
                raise AssertionError("promotion state tree must contain one journal blob")
            blob_sha = MODULE.git_blob_sha1(journal_raw)
            journal_rows[0].update({"sha": blob_sha, "size": len(journal_raw)})
            journal_rows[0]["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{blob_sha}"
            directories = sorted(
                (row["path"] for row in tree_rows if row.get("type") == "tree"),
                key=lambda value: value.count("/"), reverse=True,
            )
            for directory in directories:
                children = [
                    row for row in tree_rows
                    if pathlib.PurePosixPath(row["path"]).parent.as_posix() == directory
                ]
                children.sort(key=lambda row: (
                    pathlib.PurePosixPath(row["path"]).name
                    + ("/" if row.get("type") == "tree" else "")
                ).encode())
                body = bytearray()
                for child in children:
                    mode = str(child["mode"]).lstrip("0") or "0"
                    body.extend(
                        mode.encode("ascii") + b" "
                        + pathlib.PurePosixPath(child["path"]).name.encode("utf-8")
                        + b"\0" + bytes.fromhex(child["sha"])
                    )
                digest = MODULE.hashlib.sha1(f"tree {len(body)}\0".encode("ascii") + body).hexdigest()
                directory_row = next(row for row in tree_rows if row.get("path") == directory)
                directory_row.update({
                    "sha": digest,
                    "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{digest}",
                })
            tree_sha = self._git_tree_sha_from_recursive_rows(tree_rows)
            tree_api.update({
                "sha": tree_sha,
                "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}",
                "tree": tree_rows,
            })
            author = "Synthetic completeness ACK <completeness-test@example.invalid>"
            commit_content = (
                f"tree {tree_sha}\nparent {base_commit['sha']}\n"
                f"author {author} 1791118800 +0000\ncommitter {author} 1791118800 +0000\n\n"
                f"Synthetic ACK {label} state snapshot\n"
            ).encode()
            commit_sha = MODULE.hashlib.sha1(
                f"commit {len(commit_content)}\0".encode() + commit_content
            ).hexdigest()
            commit_api = copy.deepcopy(base_commit)
            commit_api.update({
                "sha": commit_sha, "message": f"Synthetic ACK {label} state snapshot",
                "tree": {**base_commit["tree"], "sha": tree_sha,
                         "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}"},
                "parents": [{"sha": base_commit["sha"], "url": base_commit["url"],
                             "html_url": base_commit.get("html_url")}],
            })
            ref_api = copy.deepcopy(base_ref)
            ref_api["object"].update({
                "sha": commit_sha, "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/commits/{commit_sha}",
            })
            blob_api = copy.deepcopy(base_blob)
            blob_api.update({
                "sha": blob_sha, "size": len(journal_raw),
                "content": MODULE.base64.b64encode(journal_raw).decode("ascii"),
                "encoding": "base64",
                "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{blob_sha}",
            })
            return {
                "acknowledgement_state_ref": pretty(ref_api),
                "acknowledgement_state_commit": pretty(commit_api),
                "acknowledgement_state_tree": pretty(tree_api),
                "acknowledgement_journal_blob_api": pretty(blob_api),
            }

        after_bundle = state_bundle(pretty(journal_after), "after")
        before_bundle = (
            state_bundle(pretty(journal_before), "before")
            if outcome != "already_acknowledged" else after_bundle
        )
        output = io.BytesIO()
        log_value = {
            "status": log_status, "source_sha": source_sha,
            "manifest_sha256": manifest_sha, "run_url": current_url,
            **({"journal_writes": journal_writes} if journal_writes is not None else {}),
        }
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("reconcile.log", f"{current_start} {json.dumps(log_value, sort_keys=True)}\n")
        log_raw = output.getvalue()

        rebound_rows = {
            "acknowledgement_run": pretty(run),
            "acknowledgement_jobs": pretty(jobs),
            "acknowledgement_log_archive": log_raw,
            "acknowledgement_journal_before": pretty(journal_before),
            "acknowledgement_journal_after": pretty(journal_after),
            **after_bundle,
            **{
                f"{role}_before": raw
                for role, raw in before_bundle.items()
            },
        }
        for role, raw in rebound_rows.items():
            existing = ack_roles.get(role)
            if existing is None:
                artifact_types = {
                    "acknowledgement_state_ref": "pipeline_state_reference",
                    "acknowledgement_state_commit": "pipeline_git_commit",
                    "acknowledgement_state_tree": "pipeline_git_tree",
                    "acknowledgement_journal_blob_api": "pipeline_git_blob",
                    "acknowledgement_state_ref_before": "pipeline_state_reference",
                    "acknowledgement_state_commit_before": "pipeline_git_commit",
                    "acknowledgement_state_tree_before": "pipeline_git_tree",
                    "acknowledgement_journal_blob_api_before": "pipeline_git_blob",
                }
                existing = {
                    "input_id": f"synthetic-ack-{role}", "scope_id": operation_id,
                    "role": role, "artifact_type": artifact_types[role],
                    "root": "evidence", "path": "", "bytes": 0, "sha256": "0" * 64,
                    "namespace": "live_operational", "producer": {}, "observed_at": None,
                    "subject": {"stage": "acknowledgement"},
                }
                index["inputs"].append(existing)
                ack_roles[role] = existing
            existing["namespace"] = "live_operational"
            if role not in {"acknowledgement_journal_before", "acknowledgement_journal_after"}:
                existing["subject"] = {**existing.get("subject", {}), "stage": "acknowledgement"}
            producer = existing.setdefault("producer", {})
            producer.update({
                "repository": "StatPan/datapan-registry", "identity": f"ack-run-{current_run_id}",
                "run_id": str(current_run_id),
                "attempt": current_attempt, "workflow_id": 373708873,
                "workflow_path": ".github/workflows/canonical-update-publication-ack.yml",
                "event": acknowledgement_event, "revision": current_revision,
            })
            self._write_rebound_evidence(input_root, existing, f"synthetic-{role}.bin", raw)

        # The non-stage journal inputs also belong to this exact current ACK
        # read. They carry no new observation or journal-write timestamp.
        for role in ("acknowledgement_journal_before", "acknowledgement_journal_after"):
            item = ack_roles[role]
            item["namespace"] = "live_operational"
            item["producer"].update({
                "repository": "StatPan/datapan-registry", "identity": f"ack-run-{current_run_id}",
                "run_id": str(current_run_id),
                "attempt": current_attempt, "workflow_id": 373708873,
                "workflow_path": ".github/workflows/canonical-update-publication-ack.yml",
                "event": acknowledgement_event, "revision": current_revision,
            })

        index_path = input_root / "input-index.json"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        return index_path, source_sha

    def _write_rebound_health_archive(
        self, index: dict[str, object], input_root: pathlib.Path,
        member_name: str, member_bytes: bytes,
    ) -> pathlib.Path:
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        health_items = [
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id
            and item.get("subject", {}).get("stage") == "health"
        ]
        archive_item = next(item for item in health_items if item["role"] == "pipeline_artifact_archive")
        metadata_item = next(item for item in health_items if item["role"] == "pipeline_artifact_metadata")
        archive_raw = (ROOT / archive_item["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as source_archive:
            members = {entry.filename: source_archive.read(entry.filename) for entry in source_archive.infolist()}
        if member_name not in members:
            raise AssertionError(f"fixture Health archive lacks {member_name}")
        members[member_name] = member_bytes
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target_archive:
            for name, value in members.items():
                target_archive.writestr(name, value)
        rebound_archive = output.getvalue()
        self._write_rebound_evidence(input_root, archive_item, "rebound-health.zip", rebound_archive)

        metadata = json.loads((ROOT / metadata_item["path"]).read_bytes())
        self.assertEqual(metadata.get("total_count"), 1)
        self.assertEqual(len(metadata.get("artifacts", [])), 1)
        metadata["artifacts"][0]["size_in_bytes"] = len(rebound_archive)
        metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(rebound_archive)}"
        metadata_raw = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        self._write_rebound_evidence(input_root, metadata_item, "rebound-health-metadata.json", metadata_raw)

        index_path = input_root / "input-index.json"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        return index_path

    @staticmethod
    def _git_tree_sha_from_recursive_rows(rows: list[dict[str, object]]) -> str:
        """Recompute a Git tree id from a complete recursive GitHub tree response."""
        by_path = {str(row["path"]): row for row in rows}
        if len(by_path) != len(rows):
            raise AssertionError("synthetic Git tree has duplicate paths")

        def tree_sha(directory: str) -> str:
            children = []
            for path, row in by_path.items():
                if pathlib.PurePosixPath(path).parent.as_posix() != (directory or "."):
                    continue
                children.append((pathlib.PurePosixPath(path).name, row))
            children.sort(key=lambda item: (item[0] + ("/" if item[1].get("type") == "tree" else "")).encode())
            body = bytearray()
            for name, row in children:
                mode = str(row["mode"]).lstrip("0") or "0"
                child_sha = tree_sha(f"{directory}/{name}".strip("/")) if row.get("type") == "tree" else str(row["sha"])
                body.extend(mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(child_sha))
            raw = bytes(body)
            return MODULE.hashlib.sha1(f"tree {len(raw)}\0".encode("ascii") + raw).hexdigest()

        return tree_sha("")

    def _build_health_last_good_variant(
        self, *, variant: str, input_root: pathlib.Path,
    ) -> pathlib.Path:
        """Build a fully rebound synthetic Health packet using the real persister replay."""
        root = ROOT
        registry, _policy, scope_by_id = MODULE.load_and_validate_registry(root)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        index = json.loads((root / MODULE.INPUT_INDEX_PATH).read_bytes())
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_scope["scope_id"]
            or (
                item.get("subject", {}).get("stage") == "health"
                or not (
                    item.get("subject", {}).get("stage") in {"source", "processor", "promotion", "publisher", "acknowledgement"}
                    or item["role"].startswith(("publication_", "acknowledgement_"))
                    or item["role"] == "consumer_readback"
                )
            )
        ]
        health_items = [
            item for item in index["inputs"]
            if item.get("scope_id") == operation_scope["scope_id"]
            and item.get("subject", {}).get("stage") == "health"
        ]
        roles = {item["role"]: item for item in health_items}
        run = json.loads((root / roles["pipeline_run"]["path"]).read_bytes())
        workflow_head = run["head_sha"]
        archive_raw = (root / roles["pipeline_artifact_archive"]["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as source_archive:
            members = {entry.filename: source_archive.read(entry.filename) for entry in source_archive.infolist()}
        receipt = json.loads(members["health-receipt.json"])
        pre_state = json.loads(members["health-state/state.json"])
        source = next(row for row in receipt["sources"] if row["source_id"] == "data_go_kr")
        prior_good = copy.deepcopy(pre_state["last_good_by_source"].get("data_go_kr"))
        candidate_good = copy.deepcopy(source["canonical"].get("last_good"))
        if not isinstance(prior_good, dict) or not isinstance(candidate_good, dict):
            raise AssertionError("retained Health packet lacks its producer-shaped last-good identities")

        if variant == "null":
            prior_good = None
            candidate_good = None
        elif variant == "older":
            candidate_good["publication_run_id"] = 37199709257
            candidate_good["publication_run_jobs_completed_at"] = "2026-10-04T12:00:00Z"
        elif variant == "newer":
            candidate_good["publication_run_id"] = 37199709259
            candidate_good["publication_run_jobs_completed_at"] = "2026-10-04T13:22:00Z"
        else:
            raise AssertionError(f"unknown last-good test variant: {variant}")

        if prior_good is None:
            pre_state["last_good_by_source"].pop("data_go_kr", None)
        else:
            pre_state["last_good_by_source"]["data_go_kr"] = copy.deepcopy(prior_good)
        source["canonical"]["last_good"] = copy.deepcopy(candidate_good)
        persister = MODULE.import_health_persister_at_revision(root, workflow_head)
        pre_state = persister.seal(pre_state, "state_sha256")
        receipt = persister.seal(receipt, "receipt_sha256")
        checker_result = json.loads(members["checker-result.json"])
        checker_result["receipt_sha256"] = receipt["receipt_sha256"]
        receipt_raw = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        pre_state_raw = (json.dumps(pre_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        checker_result_raw = (json.dumps(checker_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        members["health-receipt.json"] = receipt_raw
        members["health-state/state.json"] = pre_state_raw
        members["checker-result.json"] = checker_result_raw
        post_state = MODULE.expected_health_post_state(
            root=root, workflow_head=workflow_head, pre_state=pre_state, receipt=receipt,
        )
        post_state_raw = (json.dumps(post_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        receipt_name = persister.receipt_file_name(receipt)

        # Rebuild the output state GitHub API responses from the exact resealed
        # bytes. Tree IDs and blob IDs are real Git object hashes; the synthetic
        # commit binds the recomputed tree and retained parent without claiming
        # an external write or runtime observation.
        tree_item = roles["health_state_tree"]
        tree_api = json.loads((root / tree_item["path"]).read_bytes())
        old_receipt_api = json.loads((root / roles["health_receipt_blob_api"]["path"]).read_bytes())
        old_receipt_rows = [row for row in tree_api["tree"] if row.get("sha") == old_receipt_api.get("sha")]
        if len(old_receipt_rows) != 1:
            raise AssertionError("retained Health tree does not uniquely bind its receipt blob")
        old_receipt_path = old_receipt_rows[0]["path"]
        receipt_blob_sha = MODULE.git_blob_sha1(receipt_raw)
        state_blob_sha = MODULE.git_blob_sha1(post_state_raw)
        receipt_member_path = f"health/upstream-catalogue/receipts/{receipt_name}"
        tree_rows = [row for row in tree_api["tree"] if row.get("path") != old_receipt_path]
        replacement_rows = {
            "health/upstream-catalogue/state.json": (state_blob_sha, len(post_state_raw)),
            receipt_member_path: (receipt_blob_sha, len(receipt_raw)),
        }
        for row in tree_rows:
            if row.get("path") in replacement_rows:
                row["sha"], row["size"] = replacement_rows[row["path"]]
                row["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{row['sha']}"
        if receipt_member_path not in {row.get("path") for row in tree_rows}:
            tree_rows.append({
                "path": receipt_member_path, "mode": "100644", "type": "blob",
                "sha": receipt_blob_sha, "size": len(receipt_raw),
                "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{receipt_blob_sha}",
            })
        tree_api["tree"] = tree_rows
        original_tree = json.loads((root / tree_item["path"]).read_bytes())
        self.assertEqual(
            self._git_tree_sha_from_recursive_rows(original_tree["tree"]), original_tree["sha"],
            "fixture builder must begin from a complete cryptographically consistent API tree",
        )
        tree_sha = self._git_tree_sha_from_recursive_rows(tree_rows)
        tree_api["sha"] = tree_sha
        tree_api["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}"

        def contents_api(role: str, path: str, raw: bytes) -> dict[str, object]:
            value = json.loads((root / roles[role]["path"]).read_bytes())
            value.update({
                "path": path, "sha": MODULE.git_blob_sha1(raw), "size": len(raw),
                "content": MODULE.base64.b64encode(raw).decode("ascii"), "encoding": "base64",
            })
            value["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{value['sha']}"
            return value

        state_api = contents_api("health_state_blob_api", "health/upstream-catalogue/state.json", post_state_raw)
        receipt_api = contents_api("health_receipt_blob_api", f"health/upstream-catalogue/receipts/{receipt_name}", receipt_raw)
        parent_sha = members["health-ref"].decode("ascii").strip().split("\t")[0]
        commit_api = json.loads((root / roles["health_state_commit"]["path"]).read_bytes())
        commit_api["tree"] = {
            **commit_api["tree"], "sha": tree_sha,
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}",
        }
        commit_raw = (
            f"tree {tree_sha}\nparent {parent_sha}\nauthor Completeness test <completeness-test@example.invalid> 1791118800 +0000\n"
            "committer Completeness test <completeness-test@example.invalid> 1791118800 +0000\n\n"
            f"Synthetic resealed Health {variant} last-good fixture\n"
        ).encode()
        commit_sha = MODULE.hashlib.sha1(f"commit {len(commit_raw)}\0".encode() + commit_raw).hexdigest()
        commit_api["sha"] = commit_sha
        commit_api["message"] = f"Synthetic resealed Health {variant} last-good fixture"
        commit_api["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/commits/{commit_sha}"
        ref_api = json.loads((root / roles["health_state_ref"]["path"]).read_bytes())
        ref_api["object"]["sha"] = commit_sha
        ref_api["object"]["url"] = commit_api["url"]
        ref_api["url"] = "https://api.github.com/repos/StatPan/datapan-registry/git/ref/heads/automation/upstream-catalogue-health-state"

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target_archive:
            for member_name, raw in members.items():
                target_archive.writestr(member_name, raw)
        archive_raw = output.getvalue()
        metadata = json.loads((root / roles["pipeline_artifact_metadata"]["path"]).read_bytes())
        metadata["artifacts"][0]["size_in_bytes"] = len(archive_raw)
        metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(archive_raw)}"

        rebound = {
            "pipeline_artifact_archive": archive_raw,
            "pipeline_artifact_metadata": (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            "health_state_ref": (json.dumps(ref_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            "health_state_commit": (json.dumps(commit_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            "health_state_tree": (json.dumps(tree_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            "health_state_blob_api": (json.dumps(state_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            "health_receipt_blob_api": (json.dumps(receipt_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        }
        for role, raw in rebound.items():
            item = roles[role]
            self._write_rebound_evidence(input_root, item, f"health-{variant}-{role}.json", raw)
        index_path = input_root / "input-index.json"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        return index_path

    @staticmethod
    def _commit_test_tree(root: pathlib.Path, changed_paths: list[str], parent: str, message: str) -> str:
        subprocess.run(["git", "add", "--", *changed_paths], cwd=root, check=True)
        tree = subprocess.check_output(["git", "write-tree"], cwd=root, text=True).strip()
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Completeness rollup test",
            "GIT_AUTHOR_EMAIL": "completeness-test@example.invalid",
            "GIT_COMMITTER_NAME": "Completeness rollup test",
            "GIT_COMMITTER_EMAIL": "completeness-test@example.invalid",
            "GIT_AUTHOR_DATE": "2026-10-04T13:00:00Z",
            "GIT_COMMITTER_DATE": "2026-10-04T13:00:00Z",
        }
        commit = subprocess.check_output(
            ["git", "commit-tree", tree, "-p", parent], cwd=root,
            input=message + "\n", text=True, env=environment,
        ).strip()
        subprocess.run(["git", "update-ref", "refs/heads/main", commit], cwd=root, check=True)
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", commit], cwd=root, check=True)
        return commit

    def _rebind_synthetic_processor_history(
        self, *, index: dict[str, object], input_root: pathlib.Path, repository: pathlib.Path,
        variant: str,
    ) -> None:
        """Re-seal a local B checkpoint with either two distinct locators or a duplicate replay."""
        processor_rows = [
            item for item in index["inputs"]  # type: ignore[index]
            if item.get("scope_id") == "data-go-kr.api-operations"
            and item.get("subject", {}).get("stage") == "processor"
        ]
        roles = {item["role"]: item for item in processor_rows}
        generation_api = json.loads((ROOT / roles["processor_generation_api"]["path"]).read_bytes())
        checkpoint = json.loads(MODULE.base64.b64decode(generation_api["content"]))
        archive_item = roles["pipeline_artifact_archive"]
        archive_raw = (ROOT / archive_item["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as source_archive:
            original_uploaded_checkpoint = json.loads(
                source_archive.read("upstream-catalogue-checkpoint-receipt.json")
            )
        locators = copy.deepcopy(checkpoint["input_artifacts"])
        if len(locators) != 1:
            raise AssertionError("retained fixture must begin with its original single-A locator")
        latest = locators[-1]
        if variant == "distinct-second-observation":
            earlier = copy.deepcopy(latest)
            earlier.update({
                "run_id": "99972210001", "artifact_id": "99972210002",
                "name": "upstream-catalog-refresh-99972210001",
                "expires_at": "2026-10-04T13:00:00Z",
                "candidate_sha256": "a" * 64,
                "evidence_sha256": "b" * 64,
                "diff_sha256": "c" * 64,
            })
            checkpoint["input_artifacts"] = [earlier, *locators]
            checkpoint["observation_count"] = 2
        elif variant == "duplicate-replay":
            checkpoint["input_artifacts"] = [*locators, copy.deepcopy(latest)]
            checkpoint["observation_count"] = 2
        else:
            raise AssertionError(f"unknown synthetic B history variant: {variant}")

        unsigned = copy.deepcopy(checkpoint)
        unsigned.pop("checkpoint_sha256", None)
        checkpoint["checkpoint_sha256"] = MODULE.sha256_bytes(
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        checkpoint_raw = json.dumps(
            checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
        uploaded_checkpoint = copy.deepcopy(checkpoint)
        uploaded_checkpoint["output_artifact"]["artifact_id"] = None
        uploaded_checkpoint["output_artifact"]["expires_at"] = original_uploaded_checkpoint[
            "output_artifact"
        ]["expires_at"]
        uploaded_checkpoint["last_heartbeat_at"] = original_uploaded_checkpoint["last_heartbeat_at"]
        unsigned_uploaded = copy.deepcopy(uploaded_checkpoint)
        unsigned_uploaded.pop("checkpoint_sha256", None)
        uploaded_checkpoint["checkpoint_sha256"] = MODULE.sha256_bytes(
            json.dumps(unsigned_uploaded, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        uploaded_checkpoint_raw = json.dumps(
            uploaded_checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
        generation_api.update({
            "content": MODULE.base64.b64encode(checkpoint_raw).decode("ascii"),
            "encoding": "base64", "size": len(checkpoint_raw),
            "sha": MODULE.git_blob_sha1(checkpoint_raw),
        })
        generation_api["url"] = (
            f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{generation_api['sha']}"
        )

        member_name = "upstream-catalogue-checkpoint-receipt.json"
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as source_archive:
            members = {info.filename: source_archive.read(info) for info in source_archive.infolist()}
        if member_name not in members:
            raise AssertionError("retained B artifact lacks its producer checkpoint receipt member")
        members[member_name] = uploaded_checkpoint_raw
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target_archive:
            for name, raw in members.items():
                target_archive.writestr(name, raw)
        rebound_archive = output.getvalue()
        metadata = json.loads((ROOT / roles["pipeline_artifact_metadata"]["path"]).read_bytes())
        metadata["artifacts"][0]["size_in_bytes"] = len(rebound_archive)
        metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(rebound_archive)}"

        state_tree_api = json.loads((ROOT / roles["processor_state_tree"]["path"]).read_bytes())
        rows = copy.deepcopy(state_tree_api["tree"])
        generation_path = generation_api["path"]
        generation_rows = [row for row in rows if row.get("path") == generation_path]
        if len(generation_rows) != 1:
            raise AssertionError("B state tree must contain its exact generation blob once")
        generation_rows[0]["sha"] = MODULE.git_blob_sha1(checkpoint_raw)
        generation_rows[0]["size"] = len(checkpoint_raw)
        generation_rows[0]["url"] = (
            "https://api.github.com/repos/StatPan/datapan-registry/git/blobs/"
            f"{generation_rows[0]['sha']}"
        )
        directories = sorted(
            (row["path"] for row in rows if row.get("type") == "tree"),
            key=lambda value: value.count("/"), reverse=True,
        )
        for directory in directories:
            children = [
                row for row in rows
                if pathlib.PurePosixPath(row["path"]).parent.as_posix() == directory
            ]
            children.sort(key=lambda row: (
                pathlib.PurePosixPath(row["path"]).name
                + ("/" if row.get("type") == "tree" else "")
            ).encode())
            body = bytearray()
            for child in children:
                mode = str(child["mode"]).lstrip("0") or "0"
                body.extend(
                    mode.encode("ascii") + b" "
                    + pathlib.PurePosixPath(child["path"]).name.encode("utf-8")
                    + b"\0" + bytes.fromhex(child["sha"])
                )
            digest = MODULE.hashlib.sha1(f"tree {len(body)}\0".encode("ascii") + body).hexdigest()
            directory_row = next(row for row in rows if row.get("path") == directory)
            directory_row["sha"] = digest
            directory_row["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{digest}"
        tree_sha = self._git_tree_sha_from_recursive_rows(rows)
        state_tree_api.update({
            "sha": tree_sha,
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}",
            "tree": rows,
        })
        root_children = []
        for row in rows:
            if pathlib.PurePosixPath(row["path"]).parent.as_posix() == ".":
                name = pathlib.PurePosixPath(row["path"]).name
                root_children.append((
                    (name + ("/" if row.get("type") == "tree" else "")).encode(), name, row,
                ))
        root_children.sort(key=lambda value: value[0])
        tree_body = bytearray()
        for _sort_name, name, row in root_children:
            mode = str(row["mode"]).lstrip("0") or "0"
            tree_body.extend(
                mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(row["sha"])
            )
        written_tree = subprocess.check_output(
            ["git", "hash-object", "-w", "-t", "tree", "--stdin"], cwd=repository,
            input=bytes(tree_body),
        ).decode("ascii").strip()
        if written_tree != tree_sha:
            raise AssertionError("synthetic B state tree API SHA does not match its Git tree bytes")

        old_commit = json.loads((ROOT / roles["processor_state_commit"]["path"]).read_bytes())
        author = "Completeness fixture <completeness-test@example.invalid>"
        commit_content = (
            f"tree {tree_sha}\nparent {old_commit['sha']}\n"
            f"author {author} 1791110520 +0000\ncommitter {author} 1791110520 +0000\n\n"
            "Synthetic producer-shaped B observation history\n"
        ).encode()
        commit_sha = subprocess.check_output(
            ["git", "hash-object", "-w", "-t", "commit", "--stdin"], cwd=repository,
            input=commit_content,
        ).decode("ascii").strip()
        commit_api = copy.deepcopy(old_commit)
        commit_api.update({
            "sha": commit_sha,
            "tree": {**old_commit["tree"], "sha": tree_sha,
                    "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{tree_sha}"},
            "parents": [{"sha": old_commit["sha"], "url": old_commit["url"], "html_url": old_commit["html_url"]}],
            "message": "Synthetic producer-shaped B observation history",
        })
        ref_api = json.loads((ROOT / roles["processor_state_ref"]["path"]).read_bytes())
        ref_api["object"]["sha"] = commit_sha
        ref_api["object"]["url"] = commit_api["url"]

        for role, filename, raw in (
            ("processor_generation_api", "synthetic-b-generation-api.json", json.dumps(
                generation_api, ensure_ascii=False, indent=2, sort_keys=True,
            ).encode() + b"\n"),
            ("pipeline_artifact_archive", "synthetic-b-observation-history.zip", rebound_archive),
            ("pipeline_artifact_metadata", "synthetic-b-artifact-metadata.json", json.dumps(
                metadata, ensure_ascii=False, indent=2, sort_keys=True,
            ).encode() + b"\n"),
            ("processor_state_tree", "synthetic-b-state-tree.json", json.dumps(
                state_tree_api, ensure_ascii=False, indent=2, sort_keys=True,
            ).encode() + b"\n"),
            ("processor_state_commit", "synthetic-b-state-commit.json", json.dumps(
                commit_api, ensure_ascii=False, indent=2, sort_keys=True,
            ).encode() + b"\n"),
            ("processor_state_ref", "synthetic-b-state-ref.json", json.dumps(
                ref_api, ensure_ascii=False, indent=2, sort_keys=True,
            ).encode() + b"\n"),
        ):
            self._write_rebound_evidence(input_root, roles[role], filename, raw)

    def _build_synthetic_matching_b_c_packet(
        self, input_root: pathlib.Path, *, candidate_generation_override: str | None = None,
        candidate_source_id_override: str | None = None,
        candidate_manifest_override: str | None = None,
        candidate_status: str = "merged",
        include_legacy_publication: bool = True,
        processor_history_variant: str | None = None,
        only_source_processor: bool = False,
    ) -> tuple[pathlib.Path, pathlib.Path, str]:
        """Rebind a local, producer-shaped B→merged-C→Health packet without external writes."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())

        repository = input_root / "repository"
        subprocess.run(
            ["git", "clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(repository)],
            check=True,
        )
        trusted_main = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/main"], cwd=ROOT, text=True,
        ).strip()
        subprocess.run(["git", "checkout", "--quiet", "--detach", trusted_main], cwd=repository, check=True)
        if processor_history_variant is not None:
            self._rebind_synthetic_processor_history(
                index=index, input_root=input_root, repository=repository,
                variant=processor_history_variant,
            )
        needed_paths = {
            MODULE.SCOPE_REGISTRY_PATH, MODULE.POLICY_PATH,
            MODULE.ROLLUP_SCHEMA_PATH,
            "scripts/completeness_publication_evidence.py",
            "schemas/datapan.completeness-proof-scopes.v1.schema.json",
            "schemas/datapan.completeness-proof-identities.v1.schema.json",
            "schemas/datapan.completeness-proof-inputs.v1.schema.json",
            "schemas/datapan.completeness-proof-policy.v1.schema.json",
            *(item["path"] for item in index["inputs"] if item.get("root") == "repository"),
        }
        for relative in sorted(needed_paths):
            source_path = ROOT / relative
            destination_path = repository / relative
            if not source_path.is_file() or destination_path.exists():
                continue
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source_path, destination_path)
            except OSError:
                shutil.copy2(source_path, destination_path)
        c_run_head = "01148419bef5d7f212e92ab96d0ba1c3da6c298c"
        self.assertEqual(
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", c_run_head, trusted_main],
                cwd=repository, check=False,
            ).returncode,
            0,
        )
        tree_sha = subprocess.check_output(
            ["git", "rev-parse", f"{trusted_main}^{{tree}}"], cwd=repository, text=True,
        ).strip()
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Synthetic completeness fixture",
            "GIT_AUTHOR_EMAIL": "completeness-test@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic completeness fixture",
            "GIT_COMMITTER_EMAIL": "completeness-test@example.invalid",
            "GIT_AUTHOR_DATE": "2026-10-04T13:19:00Z",
            "GIT_COMMITTER_DATE": "2026-10-04T13:19:00Z",
        }
        candidate_head = subprocess.check_output(
            ["git", "commit-tree", tree_sha, "-p", c_run_head], cwd=repository,
            input="Synthetic changed-B candidate with the authenticated Registry subject\n",
            text=True, env=environment,
        ).strip()
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", candidate_head],
            cwd=repository, check=True,
        )
        local_registry = repository / "data/data-go-kr.registry.json"
        local_registry.unlink()
        os.link(ROOT / "data/data-go-kr.registry.json", local_registry)
        current_artifact, candidate_manifest_sha = MODULE.source_lfs_binding(repository, candidate_head)

        by_role = {
            item["role"]: item for item in index["inputs"]
            if item.get("scope_id") == operation_id and item.get("subject", {}).get("stage") == "processor"
        }
        generation_item = by_role["processor_generation_api"]
        generation_root = ROOT if generation_item["root"] == "repository" else input_root
        checkpoint_api = json.loads((generation_root / generation_item["path"]).read_bytes())
        checkpoint = json.loads(MODULE.base64.b64decode(checkpoint_api["content"]))
        candidate_output = next(
            row for row in checkpoint["output_digests"]
            if row.get("path") == "composed-candidate.registry.json"
        )
        composition_output = next(
            row for row in checkpoint["output_digests"]
            if row.get("path") == "composition-receipt.json"
        )
        self.assertEqual(candidate_output["sha256"], current_artifact["sha256"])
        self.assertEqual(candidate_output["bytes"], current_artifact["bytes"])
        self.assertNotEqual(
            checkpoint["generation_inputs"]["baseline_sha256"], candidate_output["sha256"],
            "the synthetic C row must bind B's composed output, not pretend B was a no-op",
        )

        def rebind(item: dict[str, object], filename: str, raw: bytes) -> None:
            self._write_rebound_evidence(input_root, item, filename, raw)

        def pretty(value: object) -> bytes:
            return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()

        c_rows = [
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id and item.get("subject", {}).get("stage") == "promotion"
        ]
        c_roles = {item["role"]: item for item in c_rows}
        original_journal_api = json.loads((ROOT / c_roles["promotion_journal_blob_api"]["path"]).read_bytes())
        journal_raw = MODULE.base64.b64decode(original_journal_api["content"])
        journal = json.loads(journal_raw)
        candidate_row = copy.deepcopy(journal["records"][-1])
        candidate = candidate_row["candidate"]
        candidate_generation = candidate_generation_override or checkpoint["generation_id"]
        if candidate_status not in {"merged", "publication-pending", "published", "read-back-confirmed"}:
            raise AssertionError(f"unsupported synthetic C lifecycle status: {candidate_status}")
        candidate.update({
            "base_sha": c_run_head,
            "head_sha": candidate_head,
            "manifest_sha256": candidate_manifest_override or candidate_manifest_sha,
            "registry_path": "data/data-go-kr.registry.json",
            "registry_bytes": candidate_output["bytes"],
            "registry_sha256": candidate_output["sha256"],
            "composition_receipt_sha256": composition_output["sha256"],
            "generation_id": candidate_generation,
        })
        if candidate_source_id_override is not None:
            candidate["source_id"] = candidate_source_id_override
        candidate["payload_readback"] = {
            "status": "verified", "provider": "synthetic-local-git-tree",
            "repository": "StatPan/datapan-registry", "remote": "origin",
            "source_sha": candidate_head, "manifest_sha256": candidate_manifest_sha,
            "path": candidate_output["path"], "bytes": candidate_output["bytes"],
            "sha256": candidate_output["sha256"], "lfs_oid": candidate_output["sha256"],
            "upload_attempted": False, "readback": "synthetic-producer-shaped-tree",
            "observed_at": "2026-10-04T13:19:30Z",
        }
        identity = MODULE.import_module("completeness_journal_contract", ROOT / "scripts/canonical_update_pr.py")
        owner_id = identity.owner_id("StatPan/datapan-registry", "data_go_kr", "aggregate_supported_catalog")
        candidate_row.update({
            "status": "merged", "acknowledgements": [],
            "pr": {
                "number": 999722,
                "url": "https://github.com/StatPan/datapan-registry/pull/999722",
                "state": "merged", "merge_commit_sha": candidate_head,
            },
            "ownership": {
                "owner_id": owner_id, "branch": "automation/canonical-update/synthetic-b-c",
                "expected_head_sha": candidate_head, "issue_number": 722,
                "body": (
                    f"<!-- {owner_id} generation={candidate_generation} -->\n\n"
                    "Synthetic producer-shaped offline B-to-C merge fixture; no external PR, provider, "
                    "publication, or source observation was created.\n"
                ),
            },
        })
        candidate_row["ownership"]["body_sha256"] = MODULE.sha256_bytes(
            candidate_row["ownership"]["body"].encode()
        )
        if candidate_status != "merged":
            # Exercise the production transition reducer against a clearly
            # synthetic local journal. These journal acknowledgements are not
            # independent workflow-run evidence and cannot satisfy publisher,
            # read-back, or #631 claim requirements.
            ack_times = {
                "publication-pending": "2026-10-04T13:19:40Z",
                "published": "2026-10-04T13:19:50Z",
                "read-back-confirmed": "2026-10-04T13:20:00Z",
            }
            transitions = {
                "publication-pending": ["publication-pending"],
                "published": ["publication-pending", "published"],
                "read-back-confirmed": ["publication-pending", "published", "read-back-confirmed"],
            }[candidate_status]
            publication_revision = "a" * 40
            pointer_revision = "b" * 40
            for ordinal, next_status in enumerate(transitions, start=1):
                read_back = next_status == "read-back-confirmed"
                published = next_status in {"published", "read-back-confirmed"}
                run_id = 99972200000 + ordinal
                acknowledgement = {
                    "status": next_status,
                    "observed_at": ack_times[next_status],
                    "source_sha": candidate_head,
                    "manifest_sha256": candidate["manifest_sha256"],
                    "artifact_identity": {
                        "path": candidate["registry_path"],
                        "bytes": candidate["registry_bytes"],
                        "sha256": candidate["registry_sha256"],
                    },
                    "read_back_verified": read_back,
                    "read_back_sha256": candidate["registry_sha256"] if read_back else None,
                    "read_back_bytes": candidate["registry_bytes"] if read_back else None,
                    "publication_revision": publication_revision if published else None,
                    "publication_pointer_revision": pointer_revision if published else None,
                    "run_id": run_id,
                    "run_attempt": 1,
                    "run_url": f"https://github.com/StatPan/datapan-registry/actions/runs/{run_id}/attempts/1",
                    "evidence_reference": f"synthetic local transition #{ordinal}; no external workflow exists",
                }
                candidate_row = identity.record_acknowledgement(candidate_row, acknowledgement)
            self.assertEqual(candidate_row["status"], candidate_status)
        candidate_row.pop("refresh_from", None)
        candidate_row.pop("refresh_target_main_sha", None)
        candidate_row.pop("superseded_by", None)
        candidate_row.pop("ci", None)
        journal["records"].append(candidate_row)
        journal["updated_at"] = "2026-10-04T13:21:00Z"
        journal_raw = pretty(journal)

        original_tree = json.loads((ROOT / c_roles["promotion_state_tree"]["path"]).read_bytes())
        tree_rows = copy.deepcopy(original_tree["tree"])
        journal_rows = [
            row for row in tree_rows
            if row.get("path") == "reports/canonical-update-promotion-receipt.json"
        ]
        self.assertEqual(len(journal_rows), 1)
        journal_blob_sha = MODULE.git_blob_sha1(journal_raw)
        journal_rows[0]["sha"] = journal_blob_sha
        journal_rows[0]["size"] = len(journal_raw)
        journal_rows[0]["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{journal_blob_sha}"
        directories = sorted(
            (row["path"] for row in tree_rows if row.get("type") == "tree"),
            key=lambda value: value.count("/"), reverse=True,
        )
        for directory in directories:
            children = [
                row for row in tree_rows
                if pathlib.PurePosixPath(row["path"]).parent.as_posix() == directory
            ]
            body = bytearray()
            children.sort(key=lambda row: (
                pathlib.PurePosixPath(row["path"]).name
                + ("/" if row.get("type") == "tree" else "")
            ).encode())
            for child in children:
                mode = str(child["mode"]).lstrip("0") or "0"
                body.extend(
                    mode.encode("ascii") + b" "
                    + pathlib.PurePosixPath(child["path"]).name.encode("utf-8")
                    + b"\0" + bytes.fromhex(child["sha"])
                )
            digest = MODULE.hashlib.sha1(f"tree {len(body)}\0".encode("ascii") + body).hexdigest()
            directory_row = next(row for row in tree_rows if row["path"] == directory)
            directory_row["sha"] = digest
            directory_row["url"] = f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{digest}"
        c_tree_sha = self._git_tree_sha_from_recursive_rows(tree_rows)
        c_tree_api = copy.deepcopy(original_tree)
        c_tree_api.update({
            "sha": c_tree_sha,
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{c_tree_sha}",
            "tree": tree_rows,
        })
        root_children = []
        for row in tree_rows:
            if pathlib.PurePosixPath(row["path"]).parent.as_posix() == ".":
                name = pathlib.PurePosixPath(row["path"]).name
                sort_name = name + ("/" if row.get("type") == "tree" else "")
                root_children.append((sort_name.encode(), name, row))
        root_children.sort(key=lambda value: value[0])
        root_tree_body = bytearray()
        for _sort_name, name, row in root_children:
            mode = str(row["mode"]).lstrip("0") or "0"
            root_tree_body.extend(
                mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(row["sha"])
            )
        written_tree_sha = subprocess.check_output(
            ["git", "hash-object", "-w", "-t", "tree", "--stdin"], cwd=repository,
            input=bytes(root_tree_body),
        ).decode("ascii").strip()
        self.assertEqual(written_tree_sha, c_tree_sha)

        old_commit = json.loads((ROOT / c_roles["promotion_state_commit"]["path"]).read_bytes())
        author = "Completeness fixture <completeness-test@example.invalid>"
        state_commit_content = (
            f"tree {c_tree_sha}\nparent {old_commit['sha']}\n"
            f"author {author} 1791110520 +0000\ncommitter {author} 1791110520 +0000\n\n"
            "Synthetic producer-shaped C journal transition\n"
        ).encode()
        state_commit_sha = subprocess.check_output(
            ["git", "hash-object", "-w", "-t", "commit", "--stdin"], cwd=repository,
            input=state_commit_content,
        ).decode("ascii").strip()
        c_commit_api = copy.deepcopy(old_commit)
        c_commit_api.update({
            "sha": state_commit_sha,
            "tree": {**old_commit["tree"], "sha": c_tree_sha,
                     "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/trees/{c_tree_sha}"},
            "parents": [{"sha": old_commit["sha"], "url": old_commit["url"], "html_url": old_commit["html_url"]}],
            "message": "Synthetic producer-shaped C journal transition",
        })
        c_ref_api = json.loads((ROOT / c_roles["promotion_state_ref"]["path"]).read_bytes())
        c_ref_api["object"]["sha"] = state_commit_sha
        c_ref_api["object"]["url"] = c_commit_api["url"]
        journal_api = copy.deepcopy(original_journal_api)
        journal_api.update({
            "path": "reports/canonical-update-promotion-receipt.json",
            "sha": journal_blob_sha, "size": len(journal_raw),
            "content": MODULE.base64.b64encode(journal_raw).decode("ascii"),
            "encoding": "base64",
            "url": f"https://api.github.com/repos/StatPan/datapan-registry/git/blobs/{journal_blob_sha}",
        })
        for role, filename, value in (
            ("promotion_journal_blob_api", "synthetic-promotion-journal-blob.json", journal_api),
            ("promotion_state_ref", "synthetic-promotion-state-ref.json", c_ref_api),
            ("promotion_state_commit", "synthetic-promotion-state-commit.json", c_commit_api),
            ("promotion_state_tree", "synthetic-promotion-state-tree.json", c_tree_api),
        ):
            rebind(c_roles[role], filename, pretty(value))

        log_item = c_roles["pipeline_log_archive"]
        run = json.loads((ROOT / c_roles["pipeline_run"]["path"]).read_bytes())
        run_url = f"https://github.com/StatPan/datapan-registry/actions/runs/{run['id']}/attempts/{run['run_attempt']}"
        log_raw = MODULE.gzip.compress(
            (json.dumps({"status": "promotion-prs-reconciled", "run_url": run_url}, sort_keys=True) + "\n").encode(),
            mtime=0,
        )
        rebind(log_item, "synthetic-promotion-reconcile.log.gz", log_raw)

        health_roles = {
            item["role"]: item for item in index["inputs"]
            if item.get("scope_id") == operation_id and item.get("subject", {}).get("stage") == "health"
        }
        health_archive_item = health_roles["pipeline_artifact_archive"]
        health_archive_raw = (ROOT / health_archive_item["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(health_archive_raw)) as archive:
            health_members = {name: archive.read(name) for name in archive.namelist()}
        health_members["promotion/reports/canonical-update-promotion-receipt.json"] = journal_raw
        health_members["promotion-ref"] = (
            f"{state_commit_sha}\trefs/heads/automation/canonical-update-state\n"
        ).encode("ascii")
        health_output = io.BytesIO()
        with zipfile.ZipFile(health_output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member_name, raw in health_members.items():
                archive.writestr(member_name, raw)
        new_health_archive = health_output.getvalue()
        rebind(health_archive_item, "synthetic-health-chain.zip", new_health_archive)
        health_metadata = json.loads((ROOT / health_roles["pipeline_artifact_metadata"]["path"]).read_bytes())
        health_metadata["artifacts"][0]["size_in_bytes"] = len(new_health_archive)
        health_metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(new_health_archive)}"
        rebind(
            health_roles["pipeline_artifact_metadata"], "synthetic-health-artifacts.json", pretty(health_metadata),
        )

        if not include_legacy_publication:
            index["inputs"] = [
                item for item in index["inputs"]
                if item.get("subject", {}).get("stage") not in {"publisher", "acknowledgement"}
                and not item["role"].startswith(("publication_", "acknowledgement_"))
                and item["role"] != "consumer_readback"
            ]
        if only_source_processor:
            index["inputs"] = [
                item for item in index["inputs"]
                if item.get("scope_id") != operation_id
                or item.get("subject", {}).get("stage") in {None, "source", "processor"}
                and not item["role"].startswith(("publication_", "acknowledgement_"))
                and item["role"] != "consumer_readback"
            ]

        index_path = input_root / "input-index.json"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        return repository, index_path, checkpoint["generation_id"]

    def test_verify_release_fetches_bounded_main_ref_for_retained_provenance(self) -> None:
        workflow = (ROOT / ".github/workflows/verify-release.yml").read_text(encoding="utf-8")
        fetch_step = "git fetch --no-tags --depth=64 origin +refs/heads/main:refs/remotes/origin/main"
        self.assertIn(fetch_step, workflow)
        self.assertLess(
            workflow.index("Fetch bounded trusted main history for retained evidence checks"),
            workflow.index("Validate registry policy artifacts"),
        )

        with tempfile.TemporaryDirectory(prefix="completeness-main-history-fetch-") as name:
            root = pathlib.Path(name)
            remote = root / "origin.git"
            seed = root / "seed"
            checkout = root / "ci-checkout"
            subprocess.run(
                ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(remote)],
                check=True,
            )
            subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(seed)], check=True)
            for repository in (seed,):
                subprocess.run(["git", "config", "user.name", "Completeness rollup test"], cwd=repository, check=True)
                subprocess.run(["git", "config", "user.email", "completeness-test@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=seed, check=True)
            source_sha = ""
            for index in range(16):
                (seed / "history.txt").write_text(f"commit {index}\n", encoding="utf-8")
                subprocess.run(["git", "add", "history.txt"], cwd=seed, check=True)
                subprocess.run(["git", "commit", "--quiet", "-m", f"history {index}"], cwd=seed, check=True)
                if index == 0:
                    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=seed, text=True).strip()
            main_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=seed, text=True).strip()
            subprocess.run(["git", "push", "--quiet", "origin", "main"], cwd=seed, check=True)
            subprocess.run(["git", "checkout", "--quiet", "-b", "fixture-pr"], cwd=seed, check=True)
            (seed / "pull-request.txt").write_text("PR source\n", encoding="utf-8")
            subprocess.run(["git", "add", "pull-request.txt"], cwd=seed, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "PR source"], cwd=seed, check=True)
            subprocess.run(["git", "push", "--quiet", "origin", "HEAD:refs/heads/fixture-pr"], cwd=seed, check=True)

            subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(checkout)], check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=checkout, check=True)
            subprocess.run([
                "git", "fetch", "--no-tags", "--depth=1", "origin",
                "+refs/heads/fixture-pr:refs/remotes/origin/fixture-pr",
            ], cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "checkout", "--quiet", "--detach", "refs/remotes/origin/fixture-pr"], cwd=checkout, check=True)
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run(
                    ["git", "rev-parse", "refs/remotes/origin/main"], cwd=checkout,
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            subprocess.run(fetch_step.split(), cwd=checkout, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            fetched_main = subprocess.check_output(
                ["git", "rev-parse", "refs/remotes/origin/main"], cwd=checkout, text=True,
            ).strip()
            self.assertEqual(fetched_main, main_sha)
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", source_sha, "refs/remotes/origin/main"],
                cwd=checkout, check=True,
            )

    def test_build_report_admits_exact_subject_and_rejects_borrowed_release_member(self) -> None:
        """Exercise proof -> index -> native publisher/C admission without changing live authority."""
        original_index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        base_main = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/main"], cwd=ROOT, text=True,
        ).strip()
        common_dir = pathlib.Path(subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], cwd=ROOT, text=True,
        ).strip()).resolve()

        with tempfile.TemporaryDirectory(prefix="completeness-proof-rollup-e2e-", dir=ROOT.parent) as name:
            temp_root = pathlib.Path(name)
            self._hardlink_worktree(ROOT, temp_root)
            temp_manifest = temp_root / "manifest.json"
            temp_manifest.unlink()
            shutil.copy2(ROOT / "manifest.json", temp_manifest)
            subprocess.run(
                [sys.executable, "scripts/sync-release-manifest-artifacts.py", "--write"],
                cwd=temp_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=temp_root, check=True)
            alternate = temp_root / ".git/objects/info/alternates"
            alternate.parent.mkdir(parents=True, exist_ok=True)
            alternate.write_text(str(common_dir / "objects") + "\n")
            subprocess.run(["git", "config", "user.name", "Completeness rollup test"], cwd=temp_root, check=True)
            subprocess.run(["git", "config", "user.email", "completeness-test@example.invalid"], cwd=temp_root, check=True)
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", base_main], cwd=temp_root, check=True)
            subprocess.run(["git", "read-tree", base_main], cwd=temp_root, check=True)

            scope_registry = json.loads((temp_root / MODULE.SCOPE_REGISTRY_PATH).read_bytes())
            scope = next(row for row in scope_registry["scopes"] if row["resource_kind"] == "api_operation_manifest" and row["source_id"] == "data_go_kr")
            scope["authority_state"] = "available"
            scope["blockers"] = []
            self._write_json_file(temp_root, MODULE.SCOPE_REGISTRY_PATH, scope_registry)
            scope_registry, policy, scopes_by_id = MODULE.load_and_validate_registry(temp_root)
            index_path = temp_root / ".test-input-index.json"
            input_index = copy.deepcopy(original_index)
            input_index["evaluation_epoch"] = "2026-10-04T13:23:00Z"

            checked_index_path = temp_root / MODULE.INPUT_INDEX_PATH
            checked_index_path.parent.mkdir(parents=True, exist_ok=True)
            checked_index_path.unlink(missing_ok=True)
            checked_index_path.write_text(json.dumps(input_index, ensure_ascii=False, indent=2) + "\n")
            index, checked_inputs, resolved = MODULE.validate_input_index(
                root=temp_root, input_root=temp_root, index_path=checked_index_path,
                scope_by_id=scopes_by_id,
            )
            scoped = [item for item in checked_inputs if item["scope_id"] == scope["scope_id"]]
            publisher_roles = MODULE.stage_input_map(scoped, "publisher")
            publisher_run, publisher_job, _started, _completed = MODULE.validate_stage_run(
                stage="publisher", roles=publisher_roles, resolved=resolved,
                evaluation_epoch=index["evaluation_epoch"], root=temp_root,
            )

            def exact_role(role: str) -> bytes:
                item = MODULE.single_role(scoped, role)
                assert item is not None
                return MODULE.receipt_path_bytes(resolved[item["input_id"]], item)

            publication = MODULE.validate_native_publisher_attempt(
                root=temp_root,
                run=publisher_run,
                jobs=MODULE.object_at(resolved[publisher_roles["publication_producer_jobs"]["input_id"]], "publisher jobs"),
                artifact_response=MODULE.object_at(resolved[publisher_roles["publication_output_artifact"]["input_id"]], "publisher artifacts"),
                archive=exact_role("publication_artifact_archive"),
                receipt_raw=exact_role("publication_receipt"),
                binding_raw=exact_role("publication_source_binding"),
                evaluation_epoch=index["evaluation_epoch"],
            )

            source_revision = "6a5138c792f4b7402da0c5ab439646bd752a307f"
            operation_path = "reports/data-go-kr/operation-manifest.json"
            source_bytes = MODULE.git_read_only(temp_root, ["show", f"{source_revision}:{operation_path}"])
            source_input_root = temp_root / ".test-proof-source-inputs"
            source_snapshot_path = source_input_root / operation_path
            source_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            source_snapshot_path.write_bytes(source_bytes)
            source_sha = MODULE.sha256_bytes(source_bytes)
            observed_at = "2026-09-29T23:44:39Z"
            source_manifest = json.loads(source_bytes)
            identities = sorted(item["operation_id"] for item in source_manifest["operations"])
            identity_sha = MODULE.sha256_bytes(json.dumps(identities, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            denominator_path = "reports/completeness-test/authoritative-denominator.json"
            proof_path = "reports/completeness-test/complete-proof.json"
            readback_path = "reports/completeness-test/consumer-readback.json"
            journal_item = MODULE.single_role(scoped, "promotion_journal_blob_api")
            assert journal_item is not None
            denominator = {
                "schema_version": "datapan.completeness-proof-identities.v1",
                "scope_id": scope["scope_id"], "resource_kind": scope["resource_kind"],
                "source_sha256": source_sha, "identity_algorithm": scope["identity_algorithm"],
                "identity_set_sha256": identity_sha, "authority_owner": scope["identity_owner"],
                "observed_at": observed_at, "identities": identities,
                "reconciliation": {
                    "expected": len(identities), "accepted": len(identities),
                    "missing": 0, "extra": 0, "duplicate": 0, "rejected": 0,
                },
            }
            importlib_module = MODULE.import_module(
                "completeness_proof_validator_for_e2e", temp_root / "scripts/validate-completeness-proof.py",
            )
            importlib_module.POLICY = temp_root / MODULE.POLICY_PATH
            importlib_module.POLICY_SCHEMA = temp_root / "schemas/datapan.completeness-proof-policy.v1.schema.json"
            importlib_module.PROOF_SCHEMA = temp_root / "schemas/datapan.completeness-proof.v1.schema.json"
            publication_manifest = json.loads(MODULE.git_read_only(
                temp_root, ["show", f"{publication['source_sha']}:manifest.json"],
            ))
            publisher_roles = MODULE.stage_input_map(scoped, "publisher")
            pointer = MODULE.object_at(
                resolved[MODULE.single_role(scoped, "publication_pointer_immutable")["input_id"]],
                "immutable publisher pointer",
            )
            publisher_receipt = json.loads(exact_role("publication_receipt"))
            source_script = MODULE.git_read_only(
                temp_root, ["show", f"{publication['source_sha']}:scripts/huggingface_registry_distribution.py"],
            )
            publisher_workflow = MODULE.git_read_only(
                temp_root,
                ["show", f"{publication['workflow_head_sha']}:{MODULE.PIPELINE_WORKFLOWS['publisher']['workflow_path']}"],
            )
            native_inventory = MODULE.validate_native_distribution_inventory(
                manifest=publication_manifest,
                manifest_raw=MODULE.git_read_only(temp_root, ["show", f"{publication['source_sha']}:manifest.json"]),
                pointer=pointer,
                receipt=publisher_receipt,
                source_script_raw=source_script,
                workflow_raw=publisher_workflow,
            )
            release_notes_member = next(
                row for row in native_inventory["verified_artifacts"] if row["path"] == "RELEASE_NOTES.md"
            )
            self.assertEqual(native_inventory["source_manifest_artifacts"], 191)
            self.assertEqual(native_inventory["distribution_artifacts"], 196)
            self.assertEqual(
                (release_notes_member["bytes"], release_notes_member["sha256"]),
                next(
                    (row["bytes"], row["sha256"])
                    for row in publication_manifest["artifacts"] if row["path"] == "RELEASE_NOTES.md"
                ),
            )
            publication_member = next(row for row in publication_manifest["artifacts"] if row["path"] == operation_path)
            native_readback_at = publication["native_verification_completed_at"]
            consumer_readback = {
                "publisher_run_id": publisher_run["id"],
                "publisher_attempt": publisher_run["run_attempt"],
                "generated_at": publisher_run.get("updated_at"),
                "checks": [{"detail": {
                    "path": operation_path, "bytes_streamed": publication_member["bytes"],
                    "sha256": publication_member["sha256"], "revision": publication["payload_revision"],
                }}],
            }
            proof = {
                "schema_version": "datapan.completeness-proof.v1",
                "proof_id": "e2e.data-go-kr-operation-manifest",
                "generated_at": index["evaluation_epoch"],
                "policy": {
                    "id": policy["policy_id"], "schema_version": policy["schema_version"],
                    "sha256": MODULE.sha256_file(temp_root / MODULE.POLICY_PATH),
                },
                "scope": {
                    "scope_id": scope["scope_id"], "source_id": scope["source_id"],
                    "provider": scope["provider"], "resource_kind": scope["resource_kind"],
                    "selector": scope["selector"],
                    "authority": {
                        "state": "available", "owner": scope["evidence_owner"],
                        "evidence": {"path": operation_path, "sha256": source_sha},
                    },
                    "identity_algorithm": scope["identity_algorithm"],
                },
                "source_snapshot": {
                    "observed_at": observed_at, "revision": source_revision, "sha256": source_sha,
                },
                "denominator": {
                    "type": "authoritative_operation_identity_set", "value": len(identities),
                    "bound_source_sha256": source_sha, "identity_algorithm": scope["identity_algorithm"],
                },
                "reconciliation": denominator["reconciliation"],
                "freshness": {
                    "as_of": index["evaluation_epoch"], "evidence_observed_at": observed_at,
                    "max_age_seconds": 2592000, "state": "current",
                },
                "import_durability": {
                    "state": "proven",
                    "receipt": {"path": MODULE.input_path(journal_item), "sha256": journal_item["sha256"]},
                },
                "publication": {
                    "state": "published",
                    "artifact": {"path": operation_path, "sha256": publication_member["sha256"]},
                },
                "consumer_read_back": {
                    "state": "proven", "observed_at": native_readback_at,
                    "artifact_sha256": publication_member["sha256"],
                },
                "proof_state": "complete",
                "claims": {"complete": True, "current": True, "updated": True},
                "missing_evidence": [], "dependencies": [],
            }
            self._write_json_file(temp_root, denominator_path, denominator)
            self._write_json_file(temp_root, proof_path, proof)
            self._write_json_file(temp_root, readback_path, consumer_readback)
            first_commit = self._commit_test_tree(
                temp_root,
                [MODULE.SCOPE_REGISTRY_PATH, denominator_path, proof_path, readback_path],
                base_main, "test: admit exact operation subject through native delivery",
            )

            def test_item(
                input_id: str, role: str, artifact_type: str, path: str,
                content: bytes, *, namespace: str = "live_operational",
                root_label: str = "repository",
                observed: str | None = None, producer_revision: str | None = None,
                subject: dict[str, object] | None = None, producer: dict[str, object] | None = None,
            ) -> dict[str, object]:
                return {
                    "input_id": input_id, "scope_id": scope["scope_id"], "role": role,
                    "artifact_type": artifact_type, "root": root_label, "path": path,
                    "bytes": len(content), "sha256": MODULE.sha256_bytes(content),
                    "namespace": namespace,
                    "producer": producer or {
                        "repository": "StatPan/datapan-registry", "identity": f"synthetic test {role}",
                        "revision": producer_revision or first_commit,
                    },
                    "observed_at": observed,
                    **({"subject": subject} if subject is not None else {}),
                }

            denominator_raw = (temp_root / denominator_path).read_bytes()
            proof_raw = (temp_root / proof_path).read_bytes()
            readback_raw = (temp_root / readback_path).read_bytes()
            input_index["inputs"].extend([
                test_item(
                    "e2e-authoritative-source", "authoritative_source_snapshot", "source_snapshot",
                    operation_path, source_bytes, root_label="evidence", observed=observed_at,
                    producer_revision=source_revision,
                    subject={
                        "scope_id": scope["scope_id"], "source_id": scope["source_id"],
                        "resource_kind": scope["resource_kind"], "identity_algorithm": scope["identity_algorithm"],
                        "source_sha256": source_sha, "source_revision": source_revision,
                        "admission_revision": source_revision,
                    },
                ),
                test_item(
                    "e2e-authoritative-denominator", "authoritative_denominator", "authority_denominator",
                    denominator_path, denominator_raw, observed=observed_at,
                    producer_revision=first_commit,
                    subject={
                        "scope_id": scope["scope_id"], "source_id": scope["source_id"],
                        "resource_kind": scope["resource_kind"], "identity_algorithm": scope["identity_algorithm"],
                        "source_sha256": source_sha, "identity_set_sha256": identity_sha,
                        "admission_revision": first_commit,
                    },
                ),
                test_item(
                    "e2e-proof", "proof_v1", "proof_v1", proof_path, proof_raw,
                    producer_revision=first_commit,
                    subject={"admission_revision": first_commit},
                ),
                test_item(
                    "e2e-consumer-readback", "consumer_readback", "consumer_readback",
                    readback_path, readback_raw, observed=consumer_readback["generated_at"],
                    producer={
                        "repository": "StatPan/datapan-registry", "identity": "corroborating native verifier tuple",
                        "revision": publisher_run["head_sha"], "run_id": str(publisher_run["id"]),
                        "attempt": publisher_run["run_attempt"], "workflow_id": publisher_run["workflow_id"],
                        "workflow_path": MODULE.PIPELINE_WORKFLOWS["publisher"]["workflow_path"],
                        "event": publisher_run["event"],
                    },
                    subject={"stage": "publisher"},
                ),
            ])
            index_path.write_text(json.dumps(input_index, ensure_ascii=False, indent=2) + "\n")

            report = MODULE.build_report(
                root=temp_root, input_root=source_input_root, input_index_path=index_path,
            )
            row = next(item for item in report["scopes"] if item["scope_id"] == scope["scope_id"])
            self.assertEqual(row["claims"], {"complete": True, "current": True, "updated": True})
            delivery = next(facet for facet in row["facets"] if facet["facet_id"] == "immutable_publication_read_back")
            self.assertEqual(delivery["state"], "proven")
            self.assertEqual(delivery["details"]["subject"]["artifact_path"], operation_path)

            # Candidate inventory is rebuilt from the immutable base index;
            # its local Registry bytes cannot borrow the retained A/B/C/Health
            # history as proof for a different candidate subject.
            canonical_index_path = temp_root / MODULE.INPUT_INDEX_PATH
            canonical_index_path.parent.mkdir(parents=True, exist_ok=True)
            # Retain the admitted proof in the immutable baseline index so
            # candidate evaluation must validate its historical subject before
            # suppressing applicability for the changed local inventory.
            candidate_baseline_index = copy.deepcopy(input_index)
            canonical_index_path.write_text(json.dumps(candidate_baseline_index, ensure_ascii=False, indent=2) + "\n")
            baseline_main = self._commit_test_tree(
                temp_root,
                [
                    MODULE.INPUT_INDEX_PATH, MODULE.SCOPE_REGISTRY_PATH, MODULE.POLICY_PATH,
                    MODULE.INPUT_SCHEMA_PATH, MODULE.SCOPE_SCHEMA_PATH,
                    "schemas/datapan.completeness-proof-identities.v1.schema.json",
                    MODULE.ROLLUP_SCHEMA_PATH, "schemas/index.json", "manifest.json",
                    "reports/completeness-proof-rollup.json", "reports/completeness-proof-rollup.md",
                ],
                first_commit,
                "test: pin completeness baseline and retained delivery packet",
            )
            candidate_registry = temp_root / "data/data-go-kr.registry.json"
            baseline_registry_bytes = candidate_registry.read_bytes()
            baseline_registry_path = temp_root / ".test-baseline.registry.json"
            baseline_registry_path.write_bytes(baseline_registry_bytes)
            candidate_operation_manifest = temp_root / operation_path
            baseline_operation_manifest_bytes = candidate_operation_manifest.read_bytes()
            candidate_registry_copy = temp_root / "data/.candidate-registry.tmp"
            with candidate_registry.open("rb") as source, candidate_registry_copy.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.write(b"\n")
            candidate_registry.unlink()
            candidate_registry_copy.replace(candidate_registry)
            generator = MODULE.import_module(
                "candidate_operation_manifest_generator_for_e2e",
                temp_root / "scripts/generate-data-go-kr-operation-manifest.py",
            )
            prior_registry_path = generator.REGISTRY
            generator.REGISTRY = candidate_registry
            try:
                candidate_manifest = generator.build(json.loads(candidate_registry.read_bytes()))
                candidate_manifest_bytes = generator.render(candidate_manifest)
            finally:
                generator.REGISTRY = prior_registry_path
            candidate_operation_manifest.unlink()
            candidate_operation_manifest.write_bytes(candidate_manifest_bytes)
            candidate_args = [
                "--repo-root", str(temp_root), "--input-root", str(source_input_root),
                "--candidate-registry", str(candidate_registry),
                "--candidate-operation-manifest", str(candidate_operation_manifest),
                "--candidate-baseline-registry", str(baseline_registry_path),
            ]
            self.assertEqual(MODULE.main([*candidate_args, "--write"]), 0)
            candidate_report = json.loads((temp_root / MODULE.OUTPUT_JSON_PATH).read_bytes())
            candidate_operations = next(row for row in candidate_report["scopes"] if row["scope_id"] == scope["scope_id"])
            self.assertEqual(candidate_operations["claims"], {"complete": False, "current": False, "updated": False})
            self.assertIsNotNone(candidate_operations["proof"])
            self.assertEqual(candidate_operations["proof"]["claims"], {"complete": True, "current": True, "updated": True})
            self.assertEqual(candidate_operations["proof_state"], "blocked")
            self.assertEqual(candidate_report["evaluation_context"]["mode"], "candidate")
            candidate_facets = {facet["facet_id"]: facet for facet in candidate_operations["facets"]}
            self.assertEqual(candidate_facets["candidate_inventory"]["state"], "blocked")
            self.assertTrue(candidate_facets["candidate_inventory"]["details"]["historical_proof_validated"])
            self.assertTrue(candidate_facets["candidate_inventory"]["details"]["historical_claims_suppressed"])
            self.assertEqual(candidate_facets["specification_pipeline"]["state"], "historical")
            self.assertEqual(candidate_facets["immutable_publication_read_back"]["state"], "historical")
            rebuilt_candidate_index = json.loads(canonical_index_path.read_bytes())
            _candidate_context, changed_scopes = MODULE.validate_candidate_input_index(
                root=temp_root, index=rebuilt_candidate_index,
                input_index_path=canonical_index_path,
            )
            catalog_scope = next(
                item for item in scopes_by_id.values()
                if item["source_id"] == "data_go_kr" and item["resource_kind"] == "api_catalog_metadata"
            )
            self.assertIn(catalog_scope["scope_id"], changed_scopes)
            self.assertIn(scope["scope_id"], changed_scopes)

            watched_paths = [canonical_index_path, temp_root / MODULE.OUTPUT_JSON_PATH, temp_root / MODULE.OUTPUT_MARKDOWN_PATH]
            watched_before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in watched_paths]
            self.assertEqual(MODULE.main([*candidate_args, "--check"]), 0)
            self.assertEqual(watched_before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in watched_paths])

            # A candidate delta must not short-circuit validation of an
            # admitted baseline proof. A malformed but byte-bound proof in the
            # pinned baseline is a hard failure, with outputs left untouched.
            valid_candidate_index_bytes = canonical_index_path.read_bytes()
            valid_proof_bytes = (temp_root / proof_path).read_bytes()
            invalid_proof = copy.deepcopy(proof)
            invalid_proof["claims"]["current"] = "true"
            self._write_json_file(temp_root, proof_path, invalid_proof)
            bad_proof_commit = self._commit_test_tree(
                temp_root, [proof_path], baseline_main,
                "test: pin a malformed baseline proof for candidate validation",
            )
            bad_baseline_index = copy.deepcopy(candidate_baseline_index)
            bad_proof_item = next(item for item in bad_baseline_index["inputs"] if item["input_id"] == "e2e-proof")
            bad_proof_item["bytes"] = (temp_root / proof_path).stat().st_size
            bad_proof_item["sha256"] = MODULE.sha256_file(temp_root / proof_path)
            bad_proof_item["producer"]["revision"] = bad_proof_commit
            bad_proof_item["subject"]["admission_revision"] = bad_proof_commit
            self._write_json_file(temp_root, MODULE.INPUT_INDEX_PATH, bad_baseline_index)
            bad_index_commit = self._commit_test_tree(
                temp_root, [MODULE.INPUT_INDEX_PATH], bad_proof_commit,
                "test: bind malformed proof bytes into candidate baseline index",
            )
            self.assertEqual(bad_index_commit, subprocess.check_output(
                ["git", "rev-parse", "refs/remotes/origin/main"], cwd=temp_root, text=True,
            ).strip())
            unchanged_before_bad_candidate = [
                (path.read_bytes(), path.stat().st_mtime_ns) for path in watched_paths
            ]
            self.assertEqual(MODULE.main([*candidate_args, "--write"]), 1)
            self.assertEqual(
                unchanged_before_bad_candidate,
                [(path.read_bytes(), path.stat().st_mtime_ns) for path in watched_paths],
            )
            # Restore the valid baseline so later independent subject-tamper
            # controls continue from the same history.
            (temp_root / proof_path).write_bytes(valid_proof_bytes)
            canonical_index_path.write_bytes(valid_candidate_index_bytes)
            subprocess.run(["git", "update-ref", "refs/heads/main", baseline_main], cwd=temp_root, check=True)
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", baseline_main], cwd=temp_root, check=True)

            candidate_index = json.loads(canonical_index_path.read_bytes())
            tampered_candidate_index = copy.deepcopy(candidate_index)
            retained_run = next(item for item in tampered_candidate_index["inputs"] if item["role"] == "pipeline_run")
            retained_run["producer"]["revision"] = baseline_main
            with self.assertRaisesRegex(ValueError, "changed evidence beyond registered local inventory rows"):
                MODULE.validate_candidate_input_index(
                    root=temp_root, index=tampered_candidate_index,
                    input_index_path=canonical_index_path,
                )

            policy_path = temp_root / MODULE.POLICY_PATH
            policy_raw = policy_path.read_bytes()
            policy_path.unlink()
            policy_path.write_bytes(policy_raw + b"\n")
            with self.assertRaisesRegex(ValueError, "cannot change completeness scope registration or policy"):
                MODULE.input_index_for_candidate(
                    root=temp_root, baseline_main_sha=baseline_main,
                    candidate_registry=candidate_registry,
                    candidate_operation_manifest=candidate_operation_manifest,
                )
            policy_path.unlink()
            policy_path.write_bytes(policy_raw)
            candidate_registry.unlink()
            candidate_registry.write_bytes(baseline_registry_bytes)
            candidate_operation_manifest.unlink()
            candidate_operation_manifest.write_bytes(baseline_operation_manifest_bytes)

            proof["publication"]["artifact"] = {
                "path": "RELEASE_NOTES.md",
                "sha256": next(row["sha256"] for row in publication_manifest["artifacts"] if row["path"] == "RELEASE_NOTES.md"),
            }
            proof["consumer_read_back"]["artifact_sha256"] = proof["publication"]["artifact"]["sha256"]
            release_notes = next(row for row in publication_manifest["artifacts"] if row["path"] == "RELEASE_NOTES.md")
            consumer_readback["checks"] = [{"detail": {
                "path": "RELEASE_NOTES.md", "bytes_streamed": release_notes["bytes"],
                "sha256": release_notes["sha256"], "revision": publication["payload_revision"],
            }}]
            self._write_json_file(temp_root, proof_path, proof)
            self._write_json_file(temp_root, readback_path, consumer_readback)
            second_commit = self._commit_test_tree(
                temp_root, [proof_path, readback_path], first_commit,
                "test: reject a different verified release member",
            )
            for item in input_index["inputs"]:
                if item["input_id"] == "e2e-proof":
                    item["bytes"] = (temp_root / proof_path).stat().st_size
                    item["sha256"] = MODULE.sha256_file(temp_root / proof_path)
                    item["producer"]["revision"] = second_commit
                    item["subject"]["admission_revision"] = second_commit
                elif item["input_id"] == "e2e-consumer-readback":
                    item["bytes"] = (temp_root / readback_path).stat().st_size
                    item["sha256"] = MODULE.sha256_file(temp_root / readback_path)
                    item["observed_at"] = consumer_readback["generated_at"]
            index_path.write_text(json.dumps(input_index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "no registered semantic subject adapter"):
                MODULE.build_report(root=temp_root, input_root=source_input_root, input_index_path=index_path)

    def test_retained_633_import_receipt_matches_admitted_counts(self) -> None:
        admission = json.loads((ROOT / "reports/runtime-freshness-import-admissions/35798122454.json").read_text())
        receipt = json.loads((ROOT / "reports/runtime-freshness-imports/35798122454.json").read_text())

        MODULE.validate_import_receipt_arithmetic(admission=admission, receipt=receipt, source_id="data_go_kr")

    def test_import_receipt_status_count_drift_is_rejected(self) -> None:
        admission = json.loads((ROOT / "reports/runtime-freshness-import-admissions/35798122454.json").read_text())
        receipt = json.loads((ROOT / "reports/runtime-freshness-imports/35798122454.json").read_text())
        corrupted = copy.deepcopy(receipt)
        corrupted["results"][0]["status"] = "verified"

        with self.assertRaisesRegex(ValueError, "status counts differ"):
            MODULE.validate_import_receipt_arithmetic(admission=admission, receipt=corrupted, source_id="data_go_kr")

    def test_historical_633_delivery_does_not_promote_current_or_updated_claims(self) -> None:
        report = MODULE.build_report(
            root=ROOT,
            input_root=ROOT,
            input_index_path=ROOT / MODULE.INPUT_INDEX_PATH,
        )
        operations = next(row for row in report["scopes"] if row["scope_id"] == "data-go-kr.api-operations")
        import_facet = next(facet for facet in operations["facets"] if facet["facet_id"] == "import_durability")

        self.assertEqual(import_facet["state"], "historical")
        self.assertFalse(import_facet["details"]["current_subject_applicable"])
        self.assertIsNone(operations["proof"])
        self.assertEqual(operations["claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(report["summary"]["registered_scopes"], 9)
        self.assertEqual(report["input_index"]["path"], "canonical-semantic-json:reports/completeness-proof-inputs.json")
        facets = {facet["facet_id"]: facet for facet in operations["facets"]}
        self.assertEqual(facets["specification_pipeline"]["state"], "historical")
        self.assertEqual(facets["immutable_publication_read_back"]["state"], "historical")
        self.assertTrue(facets["immutable_publication_read_back"]["details"]["payload_equivalent_to_current_registry"])
        self.assertFalse(facets["immutable_publication_read_back"]["details"]["current_release_subject_applicable"])
        self.assertTrue(facets["source_observation"]["details"]["observation_authenticated"])
        self.assertNotIn("new_post_repair_observation", facets["source_observation"]["details"])
        self.assertEqual(
            facets["source_observation"]["missing_evidence"][0]["code"],
            "post_repair_observation_baseline_unbound",
        )
        self.assertEqual(
            facets["health_observation"]["missing_evidence"][0]["code"],
            "health_receipt_does_not_establish_additional_source_observations",
        )
        markdown = MODULE.render_markdown(report).decode("utf-8")
        self.assertIn("## Evidence facets", markdown)
        self.assertIn("Input index semantic identity: `canonical-semantic-json:", markdown)
        self.assertIn("immutable_publication_read_back", markdown)

    def test_retained_pipeline_chain_binds_exact_attempts_and_current_state(self) -> None:
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index, checked, resolved = MODULE.validate_input_index(
            root=ROOT, input_root=ROOT, index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            scope_by_id=scope_by_id,
        )
        scoped = [item for item in checked if item["scope_id"] == "data-go-kr.api-operations"]
        result = MODULE.validate_pipeline_evidence(
            root=ROOT, operation_inputs=scoped, all_inputs=checked,
            resolved=resolved, evaluation_epoch=index["evaluation_epoch"],
            scope_registry=scope_registry,
        )
        self.assertEqual(result["status"], "verified_historical_chain")
        self.assertEqual(result["source"]["run_id"], "36646768289")
        self.assertEqual(result["processor"]["run_id"], "37204681028")
        self.assertEqual(result["promotion"]["run_id"], "37205271032")
        self.assertEqual(result["health"]["run_id"], "37205341388")
        self.assertEqual(result["health"]["observation_count"], 1)
        self.assertEqual(result["health"]["last_good_source_sha"], "6a5138c792f4b7402da0c5ab439646bd752a307f")
        self.assertTrue(result["promotion"]["processor_generation_matches"])
        self.assertEqual(result["publication"]["status"], "verified")
        self.assertFalse(result["publication"]["details"]["currentness_established"])
        self.assertIn("main_ancestry_checked_tip", result["promotion"])
        self.assertNotIn("main_sha", result["promotion"])

        promotion_roles = MODULE.stage_input_map(scoped, "promotion")
        promotion_run, promotion_job, _started, _completed = MODULE.validate_stage_run(
            stage="promotion", roles=promotion_roles, resolved=resolved,
            evaluation_epoch=index["evaluation_epoch"], root=ROOT,
        )
        wrong_b_size = copy.deepcopy(result["processor"])
        wrong_b_size["candidate_bytes"] += 1
        with self.assertRaisesRegex(ValueError, "exact B candidate, byte count, and outcome"):
            MODULE.validate_promotion_stage(
                root=ROOT, roles=promotion_roles, resolved=resolved,
                run=promotion_run, job=promotion_job, processor=wrong_b_size,
                current_registry=result["current_registry"], evaluation_epoch=index["evaluation_epoch"],
            )

        # Publication/ACK is a separate facet. Removing both must not discard
        # the already complete and authenticated A/B/C/Health chain.
        without_publication = [
            item for item in scoped
            if item.get("subject", {}).get("stage") not in {"publisher", "acknowledgement"}
            and not item["role"].startswith(("publication_", "acknowledgement_"))
        ]
        partial = MODULE.validate_pipeline_evidence(
            root=ROOT, operation_inputs=without_publication, all_inputs=checked,
            resolved=resolved, evaluation_epoch=index["evaluation_epoch"],
            scope_registry=scope_registry,
        )
        self.assertEqual(partial["status"], "verified_historical_chain")
        self.assertEqual(partial["source"]["run_id"], result["source"]["run_id"])
        self.assertEqual(partial["processor"]["generation_id"], result["processor"]["generation_id"])
        self.assertEqual(partial["promotion"]["run_id"], result["promotion"]["run_id"])
        self.assertEqual(partial["health"]["run_id"], result["health"]["run_id"])
        self.assertIsNone(partial["publication"])
        self.assertEqual(partial["publication_missing_stages"], ["publisher", "acknowledgement"])
        historical_import = MODULE.validate_historical_import(
            root=ROOT, inputs=without_publication, resolved=resolved,
            scope=scope_by_id["data-go-kr.api-operations"],
        )
        operation_manifest_item = next(
            item for item in without_publication if item["role"] == "local_operation_manifest"
        )
        facets = MODULE.scope_facets(
            scope=scope_by_id["data-go-kr.api-operations"], scoped_inputs=without_publication,
            resolved=resolved, current_manifest_sha256=operation_manifest_item["sha256"],
            historical_import=historical_import, pipeline_evidence=partial,
            updated_delivery=None,
        )
        by_facet = {item["facet_id"]: item for item in facets}
        self.assertEqual(by_facet["specification_pipeline"]["state"], "historical")
        self.assertEqual(by_facet["source_observation"]["state"], "historical")
        self.assertEqual(by_facet["health_observation"]["state"], "historical")
        self.assertEqual(by_facet["immutable_publication_read_back"]["state"], "missing")
        self.assertEqual(
            by_facet["immutable_publication_read_back"]["missing_evidence"][0]["code"],
            "same_subject_publication_read_back_missing",
        )

        incomplete_ack = [
            item for item in scoped
            if not (item.get("subject", {}).get("stage") == "acknowledgement" and item["role"] == "acknowledgement_jobs")
        ]
        with self.assertRaisesRegex(ValueError, "pipeline stage acknowledgement is incomplete"):
            MODULE.validate_pipeline_evidence(
                root=ROOT, operation_inputs=incomplete_ack, all_inputs=checked,
                resolved=resolved, evaluation_epoch=index["evaluation_epoch"],
                scope_registry=scope_registry,
            )

        # A later native attempt must route through the parameterized #716
        # validator, never through the packet-specific historical adapter.
        original_stage_validator = MODULE.validate_stage_run

        def future_attempt_stage_validator(**kwargs: object):
            result = original_stage_validator(**kwargs)  # type: ignore[arg-type]
            if kwargs["stage"] == "publisher":
                run, job, started, completed = result
                future_run = copy.deepcopy(run)
                future_run["id"] = 99999999999
                return future_run, job, started, completed
            return result

        future_native_attempt = {"run_id": "99999999999", "attempt": 1, "source_sha": "a" * 40}
        future_inputs_without_ack = [
            item for item in scoped
            if item.get("subject", {}).get("stage") != "acknowledgement"
            and not item["role"].startswith("acknowledgement_")
        ]
        original_import_module = MODULE.import_module

        def reject_legacy_adapter(name: str, path: pathlib.Path):
            loaded = original_import_module(name, path)
            if name == "completeness_publication_evidence":
                loaded.validate_historical_publication_facet = mock.Mock(
                    side_effect=AssertionError("future attempt entered the legacy historical adapter")
                )
            return loaded

        with (
            mock.patch.object(MODULE, "validate_stage_run", side_effect=future_attempt_stage_validator),
            mock.patch.object(MODULE, "validate_native_publisher_attempt", return_value=future_native_attempt),
            mock.patch.object(MODULE, "validate_native_publication_readback", return_value={"future": True}),
            mock.patch.object(MODULE, "import_module", side_effect=reject_legacy_adapter),
        ):
            future = MODULE.validate_pipeline_evidence(
                root=ROOT, operation_inputs=future_inputs_without_ack, all_inputs=checked,
                resolved=resolved, evaluation_epoch=index["evaluation_epoch"],
                scope_registry=scope_registry,
            )
        self.assertEqual(future["status"], "verified_historical_chain")
        self.assertIsNone(future["publication"])
        self.assertEqual(future["publisher_attempt"], future_native_attempt)

    def test_source_refresh_result_is_derived_from_policy_for_no_change(self) -> None:
        source_config = {
            "source_id": "data_go_kr", "owner": "release-operator",
            "diff": {"material_change_fields": ["added", "removed", "changed"]},
            "publication": {"required_gates": ["release_manifest_verification", "release_readiness", "consumer_compatibility"]},
        }
        summary = {"added": 0, "removed": 0, "changed": 0, "stable": 120}
        diff = {
            "generated_at": "2026-10-04T01:02:03Z", "old": "data/data-go-kr.registry.json",
            "new": ".datapan/ci/upstream-refresh/candidate.registry.json", "summary": summary,
        }
        snapshot = {
            "path": diff["new"], "bytes": 123, "sha256": "a" * 64, "records": 120,
        }
        evidence = {
            "source_id": "data_go_kr", "owner": "release-operator", "status": "no_change",
            "collection": {"attempted": True, "succeeded": True, "exit_code": 0, "error_class": None},
            "snapshot": snapshot, "diff": {"path": ".datapan/ci/upstream-refresh/catalog-diff.json"},
            "review": {"action": "none"},
            "publication": {
                "automatic": False, "release_allowed": False,
                "required_gates": source_config["publication"]["required_gates"],
            },
        }
        packet = {
            "source_id": "data_go_kr", "owner": "release-operator", "status": "no_change",
            "action": "none", "observed_at": diff["generated_at"], "work_key": "",
            "automatic_publication": False, "snapshot": snapshot["path"], "diff": evidence["diff"]["path"],
        }
        payload = {
            "source_id": "data_go_kr", "status": "no_change", "summary": summary,
            "error_class": None,
        }
        packet["work_key"] = "upstream-refresh:data_go_kr:" + __import__("hashlib").sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        evidence["review"]["work_key"] = packet["work_key"]
        status, _ = MODULE.source_refresh_result_contract(
            source_config=source_config, evidence=evidence, diff=diff, work_packet=packet,
        )
        self.assertEqual(status, "no_change")
        self.assertNotEqual(snapshot["sha256"], "b" * 64)

        evidence["collection"]["exit_code"] = 1
        with self.assertRaisesRegex(ValueError, "producer-owned refresh policy"):
            MODULE.source_refresh_result_contract(
                source_config=source_config, evidence=evidence, diff=diff, work_packet=packet,
            )
        evidence["collection"]["exit_code"] = 0

        material = copy.deepcopy(summary)
        material["added"] = 1
        diff["summary"] = material
        payload["status"] = "material_change"
        payload["summary"] = material
        work_key = "upstream-refresh:data_go_kr:" + __import__("hashlib").sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        packet.update({"status": "material_change", "action": "review_catalog_drift", "work_key": work_key})
        evidence.update({"status": "material_change"})
        evidence["review"].update({"action": "review_catalog_drift", "work_key": work_key})
        self.assertEqual(
            MODULE.source_refresh_result_contract(
                source_config=source_config, evidence=evidence, diff=diff, work_packet=packet,
            )[0],
            "material_change",
        )

    def test_build_report_admits_policy_consistent_no_change_observation_without_peers(self) -> None:
        """Exercise A's real archive/schema/policy adapter through the indexed caller."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_id
            or (
                item.get("subject", {}).get("stage") == "source"
                or not (
                    item["role"].startswith("publication_")
                    or item["role"].startswith("acknowledgement_")
                    or item["role"] == "consumer_readback"
                    or item.get("subject", {}).get("stage") in {"processor", "promotion", "health", "publisher", "acknowledgement"}
                )
            )
        ]
        source_archive_item = next(
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id
            and item.get("subject", {}).get("stage") == "source"
            and item["role"] == "pipeline_artifact_archive"
        )
        source_metadata_item = next(
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id
            and item.get("subject", {}).get("stage") == "source"
            and item["role"] == "pipeline_artifact_metadata"
        )
        with zipfile.ZipFile(ROOT / source_archive_item["path"]) as original:
            members = {name: original.read(name) for name in original.namelist()}
        evidence = json.loads(members["upstream-refresh-evidence.json"])
        diff = json.loads(members["catalog-diff.json"])
        work_packet = json.loads(members["upstream-refresh-work-packet.json"])
        diff["summary"] = {"added": 0, "removed": 0, "changed": 0, "stable": 10}
        diff_raw = (json.dumps(diff, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        evidence["status"] = "no_change"
        evidence["diff"]["summary"] = diff["summary"]
        evidence["diff"]["sha256"] = MODULE.sha256_bytes(diff_raw)
        payload = {
            "source_id": "data_go_kr", "status": "no_change",
            "summary": diff["summary"], "error_class": None,
        }
        work_key = "upstream-refresh:data_go_kr:" + MODULE.hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        evidence["review"].update({"action": "none", "work_key": work_key})
        work_packet.update({"status": "no_change", "action": "none", "work_key": work_key})
        members["catalog-diff.json"] = diff_raw
        members["upstream-refresh-evidence.json"] = (
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        members["upstream-refresh-work-packet.json"] = (
            json.dumps(work_packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()

        archive_output = io.BytesIO()
        with zipfile.ZipFile(archive_output, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for name, raw in members.items():
                output.writestr(name, raw)
        archive_raw = archive_output.getvalue()

        with tempfile.TemporaryDirectory(prefix="completeness-no-change-indexed-") as name:
            input_root = pathlib.Path(name)
            self._write_rebound_evidence(input_root, source_archive_item, "source-no-change.zip", archive_raw)
            metadata = json.loads((ROOT / source_metadata_item["path"]).read_bytes())
            metadata["artifacts"][0]["size_in_bytes"] = len(archive_raw)
            metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(archive_raw)}"
            metadata_raw = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            self._write_rebound_evidence(input_root, source_metadata_item, "source-no-change-metadata.json", metadata_raw)
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")

            report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)
            row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
            pipeline = next(item for item in row["facets"] if item["facet_id"] == "specification_pipeline")
            self.assertEqual(report["evaluation_context"]["mode"], "repository")
            self.assertEqual(row["claims"], {"complete": False, "current": False, "updated": False})
            self.assertEqual(pipeline["state"], "missing")
            self.assertEqual(set(pipeline["details"]["missing_stages"]), {"processor", "promotion", "health"})
            self.assertTrue(any(item["path"].endswith("source-no-change.zip") for item in pipeline["evidence"]))

            # Keep the index/archive/API outer hashes internally consistent, but
            # relabel a policy-material diff as no_change. The owning A adapter
            # must reject it before any report can be written.
            diff["summary"]["added"] = 1
            changed_diff_raw = (json.dumps(diff, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            members["catalog-diff.json"] = changed_diff_raw
            evidence["diff"]["summary"] = diff["summary"]
            evidence["diff"]["sha256"] = MODULE.sha256_bytes(changed_diff_raw)
            members["upstream-refresh-evidence.json"] = (
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode()
            changed_archive_output = io.BytesIO()
            with zipfile.ZipFile(changed_archive_output, "w", compression=zipfile.ZIP_DEFLATED) as output:
                for member_name, raw in members.items():
                    output.writestr(member_name, raw)
            changed_archive = changed_archive_output.getvalue()
            self._write_rebound_evidence(input_root, source_archive_item, "source-no-change.zip", changed_archive)
            metadata["artifacts"][0]["size_in_bytes"] = len(changed_archive)
            metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(changed_archive)}"
            changed_metadata_raw = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            self._write_rebound_evidence(input_root, source_metadata_item, "source-no-change-metadata.json", changed_metadata_raw)
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "producer-owned refresh policy"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_build_report_replays_resealed_health_last_good_choices_without_claim_credit(self) -> None:
        """Exercise null, older, and newer Health publication choices through indexed build_report."""
        registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        expected_run_ids = {"null": None, "older": 37199709258, "newer": 37199709259}
        for variant, expected_run_id in expected_run_ids.items():
            with self.subTest(last_good=variant), tempfile.TemporaryDirectory(prefix=f"completeness-health-{variant}-") as name:
                input_root = pathlib.Path(name)
                index_path = self._build_health_last_good_variant(variant=variant, input_root=input_root)
                report = MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )
                row = next(item for item in report["scopes"] if item["scope_id"] == operation_scope["scope_id"])
                self.assertEqual(row["claims"], {"complete": False, "current": False, "updated": False})
                pipeline = next(item for item in row["facets"] if item["facet_id"] == "specification_pipeline")
                self.assertEqual(pipeline["state"], "missing")
                self.assertEqual(set(pipeline["details"]["missing_stages"]), {"source", "processor", "promotion"})

                # Inspect the same packet with the stage-local production
                # validator after the full caller has accepted the conservative
                # partial result. This asserts the actual persister replay,
                # seals, ref/tree membership, and selected last-good identity.
                _, checked, resolved = MODULE.validate_input_index(
                    root=ROOT, input_root=input_root, index_path=index_path,
                    scope_by_id=scope_by_id,
                )
                scoped = [item for item in checked if item["scope_id"] == operation_scope["scope_id"]]
                health_roles = MODULE.stage_input_map(scoped, "health")
                health_run, health_job, _started, _completed = MODULE.validate_stage_run(
                    stage="health", roles=health_roles, resolved=resolved,
                    evaluation_epoch=report["evaluation_epoch"], root=ROOT,
                )
                local = MODULE.validate_health_stage_local(
                    root=ROOT, roles=health_roles, resolved=resolved, run=health_run,
                    job=health_job, evaluation_epoch=report["evaluation_epoch"],
                )
                selected = local["post_state"]["last_good_by_source"].get("data_go_kr")
                self.assertEqual(selected.get("publication_run_id") if selected else None, expected_run_id)
                self.assertTrue(local["persister"].verify_seal(local["receipt"], "receipt_sha256"))
                self.assertTrue(local["persister"].verify_seal(local["post_state"], "state_sha256"))

    def test_build_report_admits_synthetic_changed_b_to_merged_c_with_exact_lineage(self) -> None:
        """The indexed caller joins a changed B output to a matching producer-shaped C merge."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        with tempfile.TemporaryDirectory(prefix="completeness-synthetic-matching-b-c-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            repository, index_path, expected_generation = self._build_synthetic_matching_b_c_packet(input_root)
            report = MODULE.build_report(
                root=repository, input_root=input_root, input_index_path=index_path,
            )
        row = next(item for item in report["scopes"] if item["scope_id"] == operation_scope["scope_id"])
        pipeline = next(facet for facet in row["facets"] if facet["facet_id"] == "specification_pipeline")
        delivery = next(facet for facet in row["facets"] if facet["facet_id"] == "immutable_publication_read_back")
        self.assertEqual(report["evaluation_context"]["mode"], "repository")
        self.assertEqual(row["claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(pipeline["state"], "historical")
        self.assertEqual(pipeline["details"]["processor_generation_id"], expected_generation)
        self.assertTrue(pipeline["details"]["promotion_candidate_available"])
        self.assertTrue(pipeline["details"]["promotion_processor_generation_matches"])
        self.assertEqual(pipeline["details"]["promotion_candidate_key"][3], expected_generation)
        self.assertTrue(pipeline["details"]["health_processor_observation_matches"])
        self.assertEqual(delivery["state"], "historical")
        self.assertFalse(delivery["details"]["current_release_subject_applicable"])
        self.assertEqual(
            delivery["details"]["subject"]["source_sha"],
            "6a5138c792f4b7402da0c5ab439646bd752a307f",
        )

    def test_build_report_keeps_synthetic_c_lifecycle_separate_from_native_publication(self) -> None:
        """C journal transitions remain visible but cannot stand in for publisher/ACK runs."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        transitions = {
            "publication-pending": ["publication-pending"],
            "published": ["publication-pending", "published"],
            "read-back-confirmed": ["publication-pending", "published", "read-back-confirmed"],
        }
        for final_status, expected_history in transitions.items():
            with self.subTest(final_status=final_status), tempfile.TemporaryDirectory(
                prefix=f"completeness-synthetic-c-{final_status}-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                repository, index_path, expected_generation = self._build_synthetic_matching_b_c_packet(
                    input_root, candidate_status=final_status, include_legacy_publication=False,
                )
                report = MODULE.build_report(
                    root=repository, input_root=input_root, input_index_path=index_path,
                )

            row = next(item for item in report["scopes"] if item["scope_id"] == operation_scope["scope_id"])
            facets = {facet["facet_id"]: facet for facet in row["facets"]}
            pipeline = facets["specification_pipeline"]
            delivery = facets["immutable_publication_read_back"]
            self.assertEqual(row["claims"], {"complete": False, "current": False, "updated": False})
            self.assertEqual(pipeline["state"], "historical")
            self.assertEqual(pipeline["details"]["processor_generation_id"], expected_generation)
            self.assertTrue(pipeline["details"]["promotion_candidate_available"])
            self.assertEqual(pipeline["details"]["promotion_candidate_lifecycle_status"], final_status)
            self.assertEqual(
                pipeline["details"]["promotion_candidate_acknowledgement_statuses"], expected_history,
            )
            self.assertEqual(delivery["state"], "missing")
            self.assertEqual(delivery["details"]["missing_stages"], ["publisher", "acknowledgement"])
            self.assertEqual(
                delivery["missing_evidence"][0]["code"], "same_subject_publication_read_back_missing",
            )

    def test_build_report_does_not_match_c_rows_with_foreign_generation_or_source(self) -> None:
        """A validly sealed candidate with the wrong B generation/source is not borrowed."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        variants = (
            {"candidate_generation_override": "f" * 64},
            {"candidate_source_id_override": "unregistered-source"},
        )
        for override in variants:
            with self.subTest(override=override), tempfile.TemporaryDirectory(
                prefix="completeness-synthetic-c-mismatch-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                repository, index_path, expected_generation = self._build_synthetic_matching_b_c_packet(
                    input_root, include_legacy_publication=False, **override,
                )
                report = MODULE.build_report(
                    root=repository, input_root=input_root, input_index_path=index_path,
                )
            row = next(item for item in report["scopes"] if item["scope_id"] == operation_scope["scope_id"])
            pipeline = next(facet for facet in row["facets"] if facet["facet_id"] == "specification_pipeline")
            self.assertEqual(row["claims"], {"complete": False, "current": False, "updated": False})
            self.assertEqual(pipeline["details"]["processor_generation_id"], expected_generation)
            self.assertFalse(pipeline["details"]["promotion_candidate_available"])
            self.assertFalse(pipeline["details"]["promotion_processor_generation_matches"])

    def test_build_report_rejects_synthetic_c_manifest_mismatch(self) -> None:
        """A merged C row cannot bind B's exact bytes to another release manifest."""
        with tempfile.TemporaryDirectory(prefix="completeness-synthetic-c-manifest-mismatch-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            repository, index_path, _generation = self._build_synthetic_matching_b_c_packet(
                input_root, candidate_manifest_override="f" * 64, include_legacy_publication=False,
            )
            with self.assertRaisesRegex(ValueError, "does not bind its exact release manifest"):
                MODULE.build_report(
                    root=repository, input_root=input_root, input_index_path=index_path,
                )

            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(repository),
                    "--input-root", str(input_root),
                    "--input-index", str(index_path),
                    "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md",
                    "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

    def test_build_report_admits_second_distinct_checkpointed_a_without_new_credit(self) -> None:
        """A sealed B history can retain two distinct A locators without creating live A credit."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        with tempfile.TemporaryDirectory(prefix="completeness-synthetic-second-a-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            repository, index_path, _generation = self._build_synthetic_matching_b_c_packet(
                input_root, include_legacy_publication=False,
                processor_history_variant="distinct-second-observation",
                only_source_processor=True,
            )
            report = MODULE.build_report(
                root=repository, input_root=input_root, input_index_path=index_path,
            )
            index = json.loads(index_path.read_bytes())
            generation_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_scope["scope_id"]
                and item.get("subject", {}).get("stage") == "processor"
                and item["role"] == "processor_generation_api"
            )
            checkpoint_api = json.loads((input_root / generation_item["path"]).read_bytes())
            checkpoint = json.loads(MODULE.base64.b64decode(checkpoint_api["content"]))
        row = next(item for item in report["scopes"] if item["scope_id"] == operation_scope["scope_id"])
        pipeline = next(facet for facet in row["facets"] if facet["facet_id"] == "specification_pipeline")
        self.assertEqual(row["claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(pipeline["state"], "missing")
        self.assertEqual(set(pipeline["details"]["missing_stages"]), {"promotion", "health"})
        self.assertEqual(checkpoint["observation_count"], 2)
        self.assertEqual(len(checkpoint["input_artifacts"]), 2)
        self.assertNotEqual(
            (checkpoint["input_artifacts"][0]["run_id"], checkpoint["input_artifacts"][0]["evidence_sha256"]),
            (checkpoint["input_artifacts"][1]["run_id"], checkpoint["input_artifacts"][1]["evidence_sha256"]),
        )
        self.assertEqual(
            checkpoint["last_observation"]["producer_run_id"], "36646768289",
            "the retained latest-A source identity remains unchanged in this synthetic history test",
        )

    def test_build_report_rejects_duplicate_a_replay_in_synthetic_b_history(self) -> None:
        """The full indexed B adapter rejects replayed run/evidence locators before a missing-stage return."""
        with tempfile.TemporaryDirectory(prefix="completeness-synthetic-duplicate-a-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            repository, index_path, _generation = self._build_synthetic_matching_b_c_packet(
                input_root, include_legacy_publication=False,
                processor_history_variant="duplicate-replay",
                only_source_processor=True,
            )
            with self.assertRaisesRegex(ValueError, "repeats an exact run/evidence locator"):
                MODULE.build_report(
                    root=repository, input_root=input_root, input_index_path=index_path,
                )

    def test_health_stage_preserves_bounded_history_and_nullable_last_good(self) -> None:
        """A validated Health snapshot may carry multiple observations and no publication."""
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index, checked, resolved = MODULE.validate_input_index(
            root=ROOT, input_root=ROOT, index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            scope_by_id=scope_by_id,
        )
        operation_inputs = [item for item in checked if item["scope_id"] == "data-go-kr.api-operations"]
        chain = MODULE.validate_pipeline_evidence(
            root=ROOT, operation_inputs=operation_inputs, all_inputs=checked,
            resolved=resolved, evaluation_epoch=index["evaluation_epoch"], scope_registry=scope_registry,
        )
        self.assertEqual(chain["status"], "verified_historical_chain")

        health_roles = MODULE.stage_input_map(operation_inputs, "health")
        health_run, health_job, _started, _completed = MODULE.validate_stage_run(
            stage="health", roles=health_roles, resolved=resolved,
            evaluation_epoch=index["evaluation_epoch"], root=ROOT,
        )
        validated_local = MODULE.validate_health_stage_local(
            root=ROOT, roles=health_roles, resolved=resolved, run=health_run,
            job=health_job, evaluation_epoch=index["evaluation_epoch"],
        )
        local = dict(validated_local)
        local["receipt"] = copy.deepcopy(validated_local["receipt"])
        local["post_state"] = copy.deepcopy(validated_local["post_state"])
        history = local["post_state"]["observations_by_source"]["data_go_kr"]
        self.assertEqual(len(history), 1)
        prior = copy.deepcopy(history[0])
        prior.update({
            "producer_run_id": "36640000000",
            "observed_at": "2026-09-28T23:44:39Z",
            "refresh_evidence_sha256": "a" * 64,
        })
        history.insert(0, prior)
        local["post_state"]["last_good_by_source"]["data_go_kr"] = None
        local["receipt"]["sources"][0]["canonical"]["last_good"] = None
        local["receipt"]["sources"][0]["canonical"]["already_canonical_candidate"] = None
        no_publication_promotion = copy.deepcopy(chain["promotion"])
        no_publication_promotion["already_canonical_generation_id"] = None
        no_publication_promotion["candidate_available"] = False

        # The local ZIP/state/ref/seal/persister transition above was validated
        # from the real retained packet. This focused adapter probe supplies a
        # producer-shaped, post-validation history extension to exercise the
        # generic bounded-history/null-publication branch without claiming it
        # is a new live observation or a new publication.
        with mock.patch.object(MODULE, "validate_health_stage_local", return_value=local):
            health = MODULE.validate_health_stage(
                root=ROOT, roles=health_roles, resolved=resolved,
                run=health_run, job=health_job,
                processor=chain["processor"], promotion=no_publication_promotion,
                source=chain["source"], current_registry=chain["current_registry"],
                evaluation_epoch=index["evaluation_epoch"],
            )
        self.assertEqual(health["observation_count"], 2)
        self.assertIsNone(health["last_good"])
        self.assertIsNone(health["last_good_source_sha"])
        self.assertTrue(health["source_observation_matches"])
        self.assertTrue(health["processor_observation_matches"])
        self.assertFalse(health["candidate_relation_valid"])

        bad_local = dict(local)
        bad_local["receipt"] = copy.deepcopy(local["receipt"])
        bad_local["receipt"]["sources"][0]["processor"]["checkpoint_sha256"] = "f" * 64
        with mock.patch.object(MODULE, "validate_health_stage_local", return_value=bad_local):
            with self.assertRaisesRegex(ValueError, "checkpoint digest differs"):
                MODULE.validate_health_stage(
                    root=ROOT, roles=health_roles, resolved=resolved,
                    run=health_run, job=health_job,
                    processor=chain["processor"], promotion=no_publication_promotion,
                    source=chain["source"], current_registry=chain["current_registry"],
                    evaluation_epoch=index["evaluation_epoch"],
                )

    def test_processor_input_history_supports_multiple_observations_and_rejects_duplicate_locator(self) -> None:
        candidate_sha = "a" * 64
        first = {
            "run_id": "100", "name": "upstream-catalog-refresh-100", "artifact_id": "200",
            "expires_at": "2026-10-10T00:00:00Z", "candidate_sha256": "f" * 64,
            "evidence_sha256": "b" * 64, "diff_sha256": "c" * 64,
        }
        latest = {
            "run_id": "101", "name": "upstream-catalog-refresh-101", "artifact_id": "201",
            "expires_at": "2026-10-11T00:00:00Z", "candidate_sha256": candidate_sha,
            "evidence_sha256": "d" * 64, "diff_sha256": "e" * 64,
        }
        checkpoint = {
            "generation_inputs": {"candidate_sha256": candidate_sha},
            "input_artifacts": [first, latest], "observation_count": 2,
            "last_observation": {
                "producer_run_id": "101", "refresh_evidence_sha256": "d" * 64,
                "observed_at": "2026-10-05T00:00:00Z",
            },
        }
        source = {
            "run_id": "101", "artifact_id": "201", "candidate_sha256": candidate_sha,
            "refresh_evidence_sha256": "d" * 64, "diff_sha256": "e" * 64,
            "observed_at": "2026-10-05T00:00:00Z",
        }
        selected, locator_count, observation_count = MODULE.validate_processor_input_history(checkpoint, source)
        self.assertEqual(selected["run_id"], "101")
        self.assertEqual(locator_count, 2)
        self.assertEqual(observation_count, 2)

        checkpoint["input_artifacts"].append(copy.deepcopy(latest))
        checkpoint["observation_count"] = 3
        with self.assertRaisesRegex(ValueError, "repeats an exact run/evidence locator"):
            MODULE.validate_processor_input_history(checkpoint, source)

    def test_partial_pipeline_rejects_tampered_present_pointer_without_ack(self) -> None:
        """Missing ACK is conservative, but it cannot hide malformed publisher evidence."""
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id
            or (
                item.get("subject", {}).get("stage") != "acknowledgement"
                and not item["role"].startswith("acknowledgement_")
            )
        ]
        pointer = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "publication_pointer_immutable"
        )

        with tempfile.TemporaryDirectory(prefix="completeness-partial-publisher-") as name:
            input_root = pathlib.Path(name)
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            valid_report = MODULE.build_report(
                root=ROOT, input_root=input_root, input_index_path=index_path,
            )
            operation_row = next(row for row in valid_report["scopes"] if row["scope_id"] == operation_id)
            pipeline_facet = next(facet for facet in operation_row["facets"] if facet["facet_id"] == "specification_pipeline")
            publication_facet = next(
                facet for facet in operation_row["facets"]
                if facet["facet_id"] == "immutable_publication_read_back"
            )
            self.assertEqual(pipeline_facet["state"], "historical")
            self.assertEqual(publication_facet["state"], "historical")
            self.assertEqual(publication_facet["details"]["missing_stages"], ["acknowledgement"])
            self.assertEqual(publication_facet["details"]["publisher_attempt"]["run_id"], "37199628001")
            self.assertTrue(publication_facet["details"]["native_readback"])
            self.assertIn(
                "publication_acknowledgement_missing",
                {item["code"] for item in publication_facet["missing_evidence"]},
            )
            self.assertFalse(publication_facet["details"]["current_release_subject_applicable"])

            tampered = b'{"tampered_pointer": true}\n'
            (input_root / "tampered-pointer.json").write_bytes(tampered)
            pointer.update({
                "root": "evidence", "path": "tampered-pointer.json",
                "bytes": len(tampered), "sha256": MODULE.sha256_bytes(tampered),
            })
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "pointer|distribution|publication"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {
                "rollup.json": json_output,
                "rollup.md": markdown_output,
            }
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT),
                    "--input-root", str(input_root),
                    "--input-index", str(index_path),
                    "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md",
                    "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

    def test_partial_pipeline_rejects_tampered_present_readback_without_ack(self) -> None:
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id
            or (
                item.get("subject", {}).get("stage") != "acknowledgement"
                and not item["role"].startswith("acknowledgement_")
            )
        ]
        readback_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "publication_anonymous_payload"
        )

        with tempfile.TemporaryDirectory(prefix="completeness-partial-readback-") as name:
            input_root = pathlib.Path(name)
            readback = json.loads((ROOT / readback_item["path"]).read_bytes())
            detail = next(
                row["detail"] for row in readback["checks"]
                if row.get("check") == "immutable_registry_stream_matches_expected_sha_and_size"
            )
            detail["sha256"] = "a" * 64
            raw = (json.dumps(readback, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            (input_root / "tampered-readback.json").write_bytes(raw)
            readback_item.update({
                "root": "evidence", "path": "tampered-readback.json",
                "bytes": len(raw), "sha256": MODULE.sha256_bytes(raw),
            })
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "anonymous read-back"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

    def test_missing_health_does_not_hide_malformed_present_source_archive(self) -> None:
        """Validate each present stage's local artifact before returning a missing-chain result."""
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id
            or item.get("subject", {}).get("stage") != "health"
        ]
        source_archive = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id
            and item.get("subject", {}).get("stage") == "source"
            and item["role"] == "pipeline_artifact_archive"
        )
        source_metadata = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id
            and item.get("subject", {}).get("stage") == "source"
            and item["role"] == "pipeline_artifact_metadata"
        )

        with tempfile.TemporaryDirectory(prefix="completeness-partial-source-") as name:
            input_root = pathlib.Path(name)
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            report = MODULE.build_report(
                root=ROOT, input_root=input_root, input_index_path=index_path,
            )
            operation = next(row for row in report["scopes"] if row["scope_id"] == operation_id)
            self.assertEqual(operation["claims"], {"complete": False, "current": False, "updated": False})

            malformed = b"not-a-zip"
            (input_root / "malformed-source.zip").write_bytes(malformed)
            source_archive.update({
                "root": "evidence", "path": "malformed-source.zip",
                "bytes": len(malformed), "sha256": MODULE.sha256_bytes(malformed),
            })
            metadata_path = ROOT / source_metadata["path"]
            metadata = json.loads(metadata_path.read_bytes())
            metadata["artifacts"][0]["size_in_bytes"] = len(malformed)
            metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(malformed)}"
            metadata_raw = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            (input_root / "malformed-source-metadata.json").write_bytes(metadata_raw)
            source_metadata.update({
                "root": "evidence", "path": "malformed-source-metadata.json",
                "bytes": len(metadata_raw), "sha256": MODULE.sha256_bytes(metadata_raw),
            })
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")

            with self.assertRaisesRegex(ValueError, "source collector artifact|valid bounded ZIP"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

    def test_missing_health_does_not_hide_malformed_present_processor_receipt(self) -> None:
        """A valid ZIP and matching artifact hashes cannot bless a malformed checkpoint member."""
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id or item.get("subject", {}).get("stage") != "health"
        ]
        processor_archive = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id
            and item.get("subject", {}).get("stage") == "processor"
            and item["role"] == "pipeline_artifact_archive"
        )
        processor_metadata = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id
            and item.get("subject", {}).get("stage") == "processor"
            and item["role"] == "pipeline_artifact_metadata"
        )

        archive_path = ROOT / processor_archive["path"]
        original_archive = archive_path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(original_archive)) as archive:
            members = {item.filename: archive.read(item.filename) for item in archive.infolist()}
        checkpoint_name = "upstream-catalogue-checkpoint-receipt.json"
        self.assertIn(checkpoint_name, members)
        members[checkpoint_name] = b"not-json\n"
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        malformed_archive = output.getvalue()

        metadata = json.loads((ROOT / processor_metadata["path"]).read_bytes())
        self.assertEqual(metadata["total_count"], 1)
        self.assertEqual(len(metadata["artifacts"]), 1)
        metadata["artifacts"][0]["size_in_bytes"] = len(malformed_archive)
        metadata["artifacts"][0]["digest"] = f"sha256:{MODULE.sha256_bytes(malformed_archive)}"
        metadata_raw = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()

        with tempfile.TemporaryDirectory(prefix="completeness-missing-health-bad-checkpoint-") as name:
            input_root = pathlib.Path(name)
            (input_root / "malformed-processor.zip").write_bytes(malformed_archive)
            (input_root / "malformed-processor-metadata.json").write_bytes(metadata_raw)
            processor_archive.update({
                "root": "evidence", "path": "malformed-processor.zip",
                "bytes": len(malformed_archive), "sha256": MODULE.sha256_bytes(malformed_archive),
            })
            processor_metadata.update({
                "root": "evidence", "path": "malformed-processor-metadata.json",
                "bytes": len(metadata_raw), "sha256": MODULE.sha256_bytes(metadata_raw),
            })
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")

            with self.assertRaisesRegex(ValueError, "processor archive or producer-head input provenance"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

            no_source_index = copy.deepcopy(index)
            no_source_index["inputs"] = [
                item for item in no_source_index["inputs"]
                if item["scope_id"] != operation_id or item.get("subject", {}).get("stage") != "source"
            ]
            no_source_index_path = input_root / "no-source-input-index.json"
            no_source_index_path.write_text(json.dumps(no_source_index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "processor archive or producer-head input provenance"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=no_source_index_path,
                )

            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT),
                    "--input-root", str(input_root),
                    "--input-index", str(index_path),
                    "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md",
                    "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

    def test_missing_processor_does_not_hide_malformed_present_promotion_journal(self) -> None:
        """C's own journal schema and immutable tree binding are checked without B."""
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        scope_registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id or item.get("subject", {}).get("stage") != "processor"
        ]
        journal_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "promotion_journal_blob_api"
        )
        tree_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "promotion_state_tree"
        )
        commit_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "promotion_state_commit"
        )
        ref_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "promotion_state_ref"
        )

        with tempfile.TemporaryDirectory(prefix="completeness-missing-b-bad-journal-") as name:
            input_root = pathlib.Path(name)
            invalid_journal = b"{}\n"
            blob_api = json.loads((ROOT / journal_item["path"]).read_bytes())
            blob_api.update({
                "size": len(invalid_journal),
                "sha": MODULE.git_blob_sha1(invalid_journal),
                "content": MODULE.base64.b64encode(invalid_journal).decode("ascii"),
            })
            tree = json.loads((ROOT / tree_item["path"]).read_bytes())
            tree_rows = [
                row for row in tree["tree"]
                if row.get("path") == "reports/canonical-update-promotion-receipt.json"
            ]
            self.assertEqual(len(tree_rows), 1)
            tree_rows[0]["sha"] = blob_api["sha"]
            tree["sha"] = "d" * 40
            commit = json.loads((ROOT / commit_item["path"]).read_bytes())
            commit["tree"]["sha"] = tree["sha"]
            commit["sha"] = "c" * 40
            ref = json.loads((ROOT / ref_item["path"]).read_bytes())
            ref["object"]["sha"] = commit["sha"]

            self._write_rebound_evidence(
                input_root, journal_item, "bad-promotion-journal-blob.json",
                (json.dumps(blob_api, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            self._write_rebound_evidence(
                input_root, tree_item, "bad-promotion-tree.json",
                (json.dumps(tree, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            self._write_rebound_evidence(
                input_root, commit_item, "bad-promotion-commit.json",
                (json.dumps(commit, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            self._write_rebound_evidence(
                input_root, ref_item, "bad-promotion-ref.json",
                (json.dumps(ref, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "promotion journal schema"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

    def test_missing_processor_does_not_hide_invalid_durable_health_seal(self) -> None:
        """Health's durable state is independently schema-, tree-, and seal-checked."""
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        scope_registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_scope = next(
            scope for scope in scope_registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        operation_id = operation_scope["scope_id"]
        index["inputs"] = [
            item for item in index["inputs"]
            if item["scope_id"] != operation_id or item.get("subject", {}).get("stage") != "processor"
        ]
        state_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "health_state_blob_api"
        )
        tree_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "health_state_tree"
        )
        commit_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "health_state_commit"
        )
        ref_item = next(
            item for item in index["inputs"]
            if item["scope_id"] == operation_id and item["role"] == "health_state_ref"
        )

        with tempfile.TemporaryDirectory(prefix="completeness-missing-b-bad-health-state-") as name:
            input_root = pathlib.Path(name)
            blob_api = json.loads((ROOT / state_item["path"]).read_bytes())
            state_raw = MODULE.base64.b64decode(blob_api["content"])
            state = json.loads(state_raw)
            state["state_sha256"] = "0" * 64
            invalid_state = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
            blob_api.update({
                "size": len(invalid_state),
                "sha": MODULE.git_blob_sha1(invalid_state),
                "content": MODULE.base64.b64encode(invalid_state).decode("ascii"),
            })
            tree = json.loads((ROOT / tree_item["path"]).read_bytes())
            tree_rows = [
                row for row in tree["tree"]
                if row.get("path") == "health/upstream-catalogue/state.json"
            ]
            self.assertEqual(len(tree_rows), 1)
            tree_rows[0]["sha"] = blob_api["sha"]
            tree["sha"] = "d" * 40
            commit = json.loads((ROOT / commit_item["path"]).read_bytes())
            commit["tree"]["sha"] = tree["sha"]
            commit["sha"] = "c" * 40
            ref = json.loads((ROOT / ref_item["path"]).read_bytes())
            ref["object"]["sha"] = commit["sha"]
            for row, filename, value in (
                (state_item, "bad-health-state-blob.json", blob_api),
                (tree_item, "bad-health-tree.json", tree),
                (commit_item, "bad-health-commit.json", commit),
                (ref_item, "bad-health-ref.json", ref),
            ):
                self._write_rebound_evidence(
                    input_root, row, filename,
                    (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "durable Health post-state seal"):
                MODULE.build_report(
                    root=ROOT, input_root=input_root, input_index_path=index_path,
                )

    def test_partial_health_packet_remains_blocked_when_b_and_c_are_absent(self) -> None:
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_id
            or item.get("subject", {}).get("stage") not in {"processor", "promotion"}
        ]
        with tempfile.TemporaryDirectory(prefix="completeness-health-without-bc-") as name:
            input_root = pathlib.Path(name)
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)
        row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
        facet = next(item for item in row["facets"] if item["facet_id"] == "specification_pipeline")
        self.assertEqual(row["claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(facet["state"], "missing")
        self.assertEqual(set(facet["details"]["missing_stages"]), {"processor", "promotion"})

    def test_present_ack_transition_is_validated_but_not_promoted_without_c_and_health(self) -> None:
        """A locally valid ACK stays non-authoritative while independent peers are absent."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_id
            or item.get("subject", {}).get("stage") not in {"promotion", "health"}
        ]
        with tempfile.TemporaryDirectory(prefix="completeness-ack-without-c-health-") as name:
            input_root = pathlib.Path(name)
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)
        operation = next(row for row in report["scopes"] if row["scope_id"] == operation_id)
        pipeline = next(facet for facet in operation["facets"] if facet["facet_id"] == "specification_pipeline")
        delivery = next(facet for facet in operation["facets"] if facet["facet_id"] == "immutable_publication_read_back")
        self.assertEqual(operation["claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(pipeline["state"], "missing")
        self.assertEqual(set(pipeline["details"]["missing_stages"]), {"promotion", "health"})
        self.assertEqual(delivery["details"]["subject"]["publisher_run_id"], "37199628001")
        self.assertEqual(delivery["details"]["subject"]["registry_sha256"], "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0")
        self.assertTrue(delivery["details"]["acknowledgement_present"])
        self.assertFalse(delivery["details"]["current_release_subject_applicable"])
        self.assertTrue(pipeline["details"]["validated_present_stage_evidence"])

    def test_build_report_admits_nonlegacy_native_ack_and_zero_write_replay(self) -> None:
        """The indexed caller admits a new schedule ACK and preserves its old witness on replay."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        expected_source = "6a5138c792f4b7402da0c5ab439646bd752a307f"
        cases = (
            ("read-back-confirmed", None),
            ("already_acknowledged", None),
            ("already_acknowledged", (99972280001, 2)),
        )
        for outcome, publisher_identity in cases:
            with self.subTest(outcome=outcome, publisher_identity=publisher_identity), tempfile.TemporaryDirectory(
                prefix=f"completeness-generic-native-ack-{outcome}-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                index_path, source_sha = self._build_synthetic_native_ack_packet(
                    input_root, outcome=outcome, publisher_identity=publisher_identity,
                )
                report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

            self.assertEqual(source_sha, expected_source)
            row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
            facets = {item["facet_id"]: item for item in row["facets"]}
            pipeline = facets["specification_pipeline"]
            delivery = facets["immutable_publication_read_back"]
            self.assertEqual(row["claims"], {"complete": False, "current": False, "updated": False})
            self.assertEqual(pipeline["state"], "missing")
            self.assertEqual(
                set(pipeline["details"]["missing_stages"]),
                {"source", "processor", "promotion", "health"},
            )
            self.assertEqual(delivery["state"], "historical")
            self.assertEqual(delivery["details"]["delivery_status"], "publisher_readback_acknowledged")
            self.assertEqual(delivery["details"]["verified_artifact_count"], 196)
            self.assertEqual(delivery["details"]["subject"]["source_sha"], expected_source)
            self.assertFalse(delivery["details"]["current_release_subject_applicable"])
            if publisher_identity is not None:
                self.assertEqual(delivery["details"]["subject"]["publisher_run_id"], str(publisher_identity[0]))
                self.assertEqual(delivery["details"]["subject"]["publisher_attempt"], publisher_identity[1])
            if outcome == "read-back-confirmed":
                self.assertEqual(delivery["details"]["acknowledgement_run_id"], "99972290001")
                self.assertNotIn("acknowledgement_replay_run_id", delivery["details"])
            else:
                # The original ACK remains the observation witness. The later
                # schedule execution is separately shown as a zero-write replay.
                self.assertEqual(delivery["details"]["acknowledgement_run_id"], "37199709258")
                self.assertEqual(delivery["details"]["acknowledgement_replay_run_id"], "99972290001")
                self.assertEqual(delivery["details"]["acknowledgement_replay_attempt"], 1)
                self.assertEqual(delivery["details"]["acknowledgement_replay_status"], "already_acknowledged")
                self.assertEqual(delivery["details"]["acknowledgement_replay_journal_writes"], 0)
                self.assertTrue(delivery["details"]["acknowledgement_replay_state_unchanged"])

    def test_build_report_admits_ack_suffixes_from_all_selected_owner_states(self) -> None:
        """Full indexed admission follows the owning runner from each selected predecessor state."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        current_ack_run = 99972290001
        for predecessor in (
            "prepared", "pending-review", "merged", "publication-pending", "published",
        ):
            with self.subTest(predecessor=predecessor), tempfile.TemporaryDirectory(
                prefix=f"completeness-ack-owner-state-{predecessor}-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                index_path, source_sha = self._build_synthetic_native_ack_packet(
                    input_root, outcome="read-back-confirmed", predecessor_status=predecessor,
                )
                report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

            row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
            delivery = next(
                facet for facet in row["facets"] if facet["facet_id"] == "immutable_publication_read_back"
            )
            self.assertEqual(row["claims"], {"complete": False, "current": False, "updated": False})
            self.assertEqual(delivery["details"]["delivery_status"], "publisher_readback_acknowledged")
            self.assertEqual(delivery["details"]["acknowledgement_run_id"], str(current_ack_run))
            self.assertEqual(delivery["details"]["acknowledgement_attempt"], 1)
            self.assertEqual(delivery["details"]["subject"]["source_sha"], source_sha)

        # The sixth selected state is the no-write replay of an already
        # read-back-confirmed row. This intentionally reuses the original run
        # ID with a later attempt, so legacy dispatch must not swallow it.
        with tempfile.TemporaryDirectory(prefix="completeness-ack-owner-state-read-back-confirmed-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, source_sha = self._build_synthetic_native_ack_packet(
                input_root, outcome="already_acknowledged",
                acknowledgement_identity=(37199709258, 3), acknowledgement_event="workflow_run",
            )
            report = MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)
        row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
        delivery = next(
            facet for facet in row["facets"] if facet["facet_id"] == "immutable_publication_read_back"
        )
        self.assertEqual(row["claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(delivery["details"]["subject"]["source_sha"], source_sha)
        self.assertEqual(delivery["details"]["acknowledgement_run_id"], "37199709258")
        self.assertEqual(delivery["details"]["acknowledgement_attempt"], 2)
        self.assertEqual(delivery["details"]["acknowledgement_replay_run_id"], "37199709258")
        self.assertEqual(delivery["details"]["acknowledgement_replay_attempt"], 3)
        self.assertEqual(delivery["details"]["acknowledgement_replay_journal_writes"], 0)

    def test_build_report_rejects_failed_ack_predecessor_as_unselected(self) -> None:
        """A real owning-runner failure row is not an ACK recovery predecessor."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        with tempfile.TemporaryDirectory(prefix="completeness-ack-failed-predecessor-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, _source = self._build_synthetic_native_ack_packet(
                input_root, outcome="read-back-confirmed", predecessor_status="merged",
            )
            index = json.loads(index_path.read_bytes())
            before_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_id and item["role"] == "acknowledgement_journal_before"
            )
            before = json.loads((input_root / before_item["path"]).read_bytes())
            run_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_id and item["role"] == "acknowledgement_run"
            )
            ack_run = json.loads((input_root / run_item["path"]).read_bytes())
            owner_source = MODULE.git_read_only(
                ROOT, ["show", f"{ack_run['head_sha']}:scripts/canonical_update_pr.py"],
            )
            with tempfile.TemporaryDirectory(prefix="completeness-failed-owner-contract-") as temporary:
                owner_path = pathlib.Path(temporary) / "canonical_update_pr.py"
                owner_path.write_bytes(owner_source)
                owner = MODULE.import_module("synthetic_failed_ack_owner", owner_path)
            receipt_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_id and item["role"] == "publication_receipt"
            )
            failed_receipt = json.loads((ROOT / receipt_item["path"]).read_bytes())
            failed_receipt["status"] = "failed"
            failure_path = input_root / "prior-publication-failure.json"
            failure_path.write_text(json.dumps(failed_receipt, ensure_ascii=False, indent=2) + "\n")
            ack_url = "https://github.com/StatPan/datapan-registry/actions/runs/99972289999/attempts/1"
            before["records"][0] = owner.reconcile_huggingface_publication(
                before["records"][0], failure_path,
                observed_at="2026-10-04T13:19:20Z", run_url=ack_url,
            )
            self._write_rebound_evidence(
                input_root, before_item, "failed-before-journal.json",
                (json.dumps(before, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            before_state_roles = {
                "acknowledgement_state_ref_before", "acknowledgement_state_commit_before",
                "acknowledgement_state_tree_before", "acknowledgement_journal_blob_api_before",
            }
            index["inputs"] = [
                item for item in index["inputs"]
                if not (item.get("scope_id") == operation_id and item.get("role") in before_state_roles)
            ]
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "starts from a status not selected"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_build_report_binds_pr_number_across_ack_without_ci_or_state_bundles(self) -> None:
        """Optional CI/state snapshots cannot replace the PR identity in both ACK journals."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        mutations = ("changed_after", "missing_before", "boolean_before", "zero_before", "negative_before")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                prefix=f"completeness-ack-pr-number-{mutation}-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                index_path, _source = self._build_synthetic_native_ack_packet(
                    input_root, outcome="read-back-confirmed", predecessor_status="prepared",
                )
                index = json.loads(index_path.read_bytes())
                index["inputs"] = [
                    item for item in index["inputs"]
                    if not (
                        item.get("scope_id") == operation_id
                        and item.get("role") in {
                            "acknowledgement_state_ref", "acknowledgement_state_commit",
                            "acknowledgement_state_tree", "acknowledgement_journal_blob_api",
                            "acknowledgement_state_ref_before", "acknowledgement_state_commit_before",
                            "acknowledgement_state_tree_before", "acknowledgement_journal_blob_api_before",
                        }
                    )
                ]
                snapshots: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
                for role in ("acknowledgement_journal_before", "acknowledgement_journal_after"):
                    item = next(
                        row for row in index["inputs"]
                        if row.get("scope_id") == operation_id and row["role"] == role
                    )
                    journal = json.loads((input_root / item["path"]).read_bytes())
                    target = journal["records"][0]
                    target.pop("ci", None)
                    snapshots[role] = (item, journal)

                before_item, before = snapshots["acknowledgement_journal_before"]
                after_item, after = snapshots["acknowledgement_journal_after"]
                before_pr = before["records"][0]["pr"]
                after_pr = after["records"][0]["pr"]
                if mutation == "changed_after":
                    after_pr["number"] += 1
                    after_pr["url"] = f"https://github.com/StatPan/datapan-registry/pull/{after_pr['number']}"
                elif mutation == "missing_before":
                    before_pr.pop("number")
                elif mutation == "boolean_before":
                    before_pr["number"] = True
                elif mutation == "zero_before":
                    before_pr["number"] = 0
                else:
                    before_pr["number"] = -1
                self._write_rebound_evidence(
                    input_root, before_item, f"pr-number-before-{mutation}.json",
                    (json.dumps(before, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )
                self._write_rebound_evidence(
                    input_root, after_item, f"pr-number-after-{mutation}.json",
                    (json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )
                index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
                with self.assertRaisesRegex(ValueError, "exact positive PR number"):
                    MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_build_report_rejects_ack_source_payload_manifest_pr_history_and_ci_mutations(self) -> None:
        """Rebound indexed journals still fail each independent native subject or history join."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        cases = ("source", "payload", "manifest", "pr", "history", "ci")
        for mutation in cases:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                prefix=f"completeness-ack-mutation-{mutation}-"
            ) as name:
                input_root = pathlib.Path(name) / "evidence"
                input_root.mkdir()
                index_path, _source = self._build_synthetic_native_ack_packet(
                    input_root, outcome="read-back-confirmed", predecessor_status="merged",
                )
                index = json.loads(index_path.read_bytes())
                state_bundle_roles = {
                    "acknowledgement_state_ref", "acknowledgement_state_commit",
                    "acknowledgement_state_tree", "acknowledgement_journal_blob_api",
                    "acknowledgement_state_ref_before", "acknowledgement_state_commit_before",
                    "acknowledgement_state_tree_before", "acknowledgement_journal_blob_api_before",
                }
                index["inputs"] = [
                    item for item in index["inputs"]
                    if not (item.get("scope_id") == operation_id and item.get("role") in state_bundle_roles)
                ]
                after_item = next(
                    item for item in index["inputs"]
                    if item.get("scope_id") == operation_id
                    and item["role"] == "acknowledgement_journal_after"
                )
                after = json.loads((input_root / after_item["path"]).read_bytes())
                row = after["records"][0]
                last_ack = row["acknowledgements"][-1]
                if mutation == "source":
                    last_ack["source_sha"] = "a" * 40
                elif mutation == "payload":
                    last_ack["read_back_bytes"] += 1
                elif mutation == "manifest":
                    last_ack["manifest_sha256"] = "f" * 64
                elif mutation == "pr":
                    row["pr"]["number"] += 1
                elif mutation == "history":
                    row["acknowledgements"][0]["run_id"] = 99972289998
                else:
                    self.assertIsInstance(row.get("ci"), dict)
                    row["ci"]["head_sha"] = "f" * 40
                self._write_rebound_evidence(
                    input_root, after_item, f"mutated-after-{mutation}.json",
                    (json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )
                index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
                with self.assertRaises(ValueError):
                    MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_build_report_keeps_payload_equivalence_separate_from_manifest_metadata(self) -> None:
        """A metadata-only current manifest change cannot make an older delivery current."""
        base_main = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/main"], cwd=ROOT, text=True,
        ).strip()
        common_dir = pathlib.Path(subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], cwd=ROOT, text=True,
        ).strip()).resolve()
        base_manifest_raw = subprocess.check_output(
            ["git", "show", f"{base_main}:manifest.json"], cwd=ROOT,
        )
        base_manifest = json.loads(base_manifest_raw)
        changed_generated_at = "2026-07-11T10:28:36Z"
        old_generated_at = base_manifest["generated_at"].encode()
        self.assertEqual(base_manifest_raw.count(old_generated_at), 1)
        changed_manifest_raw = base_manifest_raw.replace(old_generated_at, changed_generated_at.encode(), 1)
        changed_manifest = json.loads(changed_manifest_raw)
        self.assertEqual(changed_manifest["artifacts"], base_manifest["artifacts"])
        self.assertEqual(changed_manifest["artifact_count"], base_manifest["artifact_count"])

        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        with tempfile.TemporaryDirectory(prefix="completeness-native-publisher-metadata-manifest-") as name:
            repository = pathlib.Path(name) / "repo"
            repository.mkdir()
            self._hardlink_worktree(ROOT, repository)
            local_manifest = repository / "manifest.json"
            local_manifest.unlink()
            local_manifest.write_bytes(changed_manifest_raw)
            subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=repository, check=True)
            alternates = repository / ".git/objects/info/alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(str(common_dir / "objects") + "\n")
            subprocess.run(["git", "config", "user.name", "Completeness metadata fixture"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "completeness-test@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", base_main], cwd=repository, check=True)
            subprocess.run(["git", "update-ref", "refs/heads/main", base_main], cwd=repository, check=True)
            subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repository, check=True)
            subprocess.run(["git", "read-tree", base_main], cwd=repository, check=True)
            subprocess.run(["git", "add", "manifest.json"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "metadata-only manifest fixture"], cwd=repository, check=True)
            metadata_main = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", metadata_main], cwd=repository, check=True)
            self.assertEqual(
                subprocess.check_output(["git", "show", f"{metadata_main}:manifest.json"], cwd=repository),
                changed_manifest_raw,
            )

            input_root = repository / ".test-native-publication"
            input_root.mkdir()
            index_path, _source = self._build_synthetic_native_ack_packet(
                input_root, outcome="already_acknowledged", publisher_identity=(99972280001, 2),
            )
            report = MODULE.build_report(root=repository, input_root=input_root, input_index_path=index_path)

        row = next(item for item in report["scopes"] if item["scope_id"] == operation_id)
        delivery = next(item for item in row["facets"] if item["facet_id"] == "immutable_publication_read_back")
        details = delivery["details"]
        self.assertEqual(row["claims"], {"complete": False, "current": False, "updated": False})
        self.assertEqual(details["subject"]["publisher_run_id"], "99972280001")
        self.assertEqual(details["subject"]["publisher_attempt"], 2)
        self.assertEqual(details["current_release_manifest_sha256"], MODULE.sha256_bytes(changed_manifest_raw))
        self.assertNotEqual(details["current_release_manifest_sha256"], details["subject"]["manifest_sha256"])
        self.assertTrue(details["payload_equivalent_to_current_registry"])
        self.assertFalse(details["current_release_subject_applicable"])
        self.assertEqual(delivery["state"], "historical")

    def test_build_report_rejects_nonlegacy_ack_replay_mutations_and_preserves_outputs(self) -> None:
        """Rebound bytes cannot turn replay into a write, another subject, or another state ref."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )

        def rewrite_log(
            input_root: pathlib.Path, index_path: pathlib.Path, *, key: str, value: object,
        ) -> None:
            index = json.loads(index_path.read_bytes())
            item = next(
                row for row in index["inputs"]
                if row.get("scope_id") == operation_id and row["role"] == "acknowledgement_log_archive"
            )
            path = input_root / item["path"]
            with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
                members = [(entry.filename, archive.read(entry.filename)) for entry in archive.infolist()]
            rewritten: list[tuple[str, bytes]] = []
            for filename, raw in members:
                if filename != "reconcile.log":
                    rewritten.append((filename, raw))
                    continue
                lines = raw.decode("utf-8").splitlines()
                self.assertEqual(len(lines), 1)
                marker = lines[0].find("{")
                payload = json.loads(lines[0][marker:])
                payload[key] = value
                lines[0] = lines[0][:marker] + json.dumps(payload, sort_keys=True)
                rewritten.append((filename, ("\n".join(lines) + "\n").encode()))
            rebuilt = io.BytesIO()
            with zipfile.ZipFile(rebuilt, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for filename, raw in rewritten:
                    archive.writestr(filename, raw)
            self._write_rebound_evidence(input_root, item, "mutated-ack-log.zip", rebuilt.getvalue())
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")

        with tempfile.TemporaryDirectory(prefix="completeness-bad-native-ack-replay-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, _source = self._build_synthetic_native_ack_packet(input_root)
            rewrite_log(input_root, index_path, key="journal_writes", value=1)
            with self.assertRaisesRegex(ValueError, "exact zero-write journal replay"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT), "--input-root", str(input_root),
                    "--input-index", str(index_path), "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md", "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

        with tempfile.TemporaryDirectory(prefix="completeness-wrong-native-ack-witness-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, _source = self._build_synthetic_native_ack_packet(input_root)
            rewrite_log(input_root, index_path, key="manifest_sha256", value="f" * 64)
            with self.assertRaisesRegex(ValueError, "exact-subject read-back witness"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

        with tempfile.TemporaryDirectory(prefix="completeness-native-ack-state-ref-mismatch-") as name:
            input_root = pathlib.Path(name) / "evidence"
            input_root.mkdir()
            index_path, _source = self._build_synthetic_native_ack_packet(input_root)
            index = json.loads(index_path.read_bytes())
            ref_item = next(
                row for row in index["inputs"]
                if row.get("scope_id") == operation_id
                and row["role"] == "acknowledgement_state_ref_before"
            )
            ref = json.loads((input_root / ref_item["path"]).read_bytes())
            ref["object"]["sha"] = "a" * 40
            self._write_rebound_evidence(
                input_root, ref_item, "mismatched-before-ref.json",
                (json.dumps(ref, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "branch ref, immutable commit, and complete tree"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_partial_ack_rejects_rebound_journal_and_log_before_missing_health_return(self) -> None:
        """Missing Health cannot hide malformed supplied ACK journal or job logs."""
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )

        def partial_index() -> dict[str, object]:
            index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
            index["inputs"] = [
                item for item in index["inputs"]
                if item.get("scope_id") != operation_id
                or item.get("subject", {}).get("stage") != "health"
            ]
            return index

        with tempfile.TemporaryDirectory(prefix="completeness-ack-local-negative-") as name:
            input_root = pathlib.Path(name)
            index = partial_index()
            after_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_id and item["role"] == "acknowledgement_journal_after"
            )
            self._write_rebound_evidence(
                input_root, after_item, "tampered-ack-journal.json", b'{"tampered_ack":true}\n',
            )
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT),
                    "--input-root", str(input_root),
                    "--input-index", str(index_path),
                    "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md",
                    "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

        with tempfile.TemporaryDirectory(prefix="completeness-ack-invalid-owned-body-") as name:
            input_root = pathlib.Path(name)
            index = partial_index()
            for role in ("acknowledgement_journal_before", "acknowledgement_journal_after"):
                item = next(
                    row for row in index["inputs"]
                    if row.get("scope_id") == operation_id and row["role"] == role
                )
                original_path = ROOT / item["path"]
                journal = json.loads(original_path.read_bytes())
                target = next(
                    row for row in journal["records"]
                    if row.get("pr", {}).get("state") == "merged"
                )
                target["ownership"]["body"] += "\nTampered without updating the durable body digest."
                self._write_rebound_evidence(
                    input_root, item, f"invalid-owned-body-{role}.json",
                    (json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )
            index_path = input_root / "input-index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT),
                    "--input-root", str(input_root),
                    "--input-index", str(index_path),
                    "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md",
                    "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")
            with self.assertRaisesRegex(ValueError, "fails its authenticated producer contract"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

            index = partial_index()
            log_item = next(
                item for item in index["inputs"]
                if item.get("scope_id") == operation_id and item["role"] == "acknowledgement_log_archive"
            )
            self._write_rebound_evidence(input_root, log_item, "not-ack-logs.zip", b"not-a-zip\n")
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "acknowledgement logs.*ZIP|not a valid bounded ZIP"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_health_archived_state_refs_join_each_supplied_peer_without_full_chain(self) -> None:
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        cases = (
            (
                "processor", "promotion-ref",
                b"0" * 40 + b"\trefs/heads/automation/canonical-update-state\n",
                "health archived promotion ref differs from the exact supplied C state",
            ),
            (
                "promotion", "processor-ref",
                b"0" * 40 + b"\trefs/heads/automation/upstream-catalogue-state\n",
                "health archived processor ref differs from the exact supplied B state",
            ),
        )
        for missing_stage, member_name, changed_ref, message in cases:
            with self.subTest(missing_stage=missing_stage):
                index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
                index["inputs"] = [
                    item for item in index["inputs"]
                    if item.get("scope_id") != operation_id
                    or item.get("subject", {}).get("stage") != missing_stage
                ]
                with tempfile.TemporaryDirectory(prefix="completeness-health-peer-join-") as name:
                    input_root = pathlib.Path(name)
                    index_path = self._write_rebound_health_archive(
                        index, input_root, member_name, changed_ref,
                    )
                    with self.assertRaisesRegex(ValueError, message):
                        MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_health_archived_promotion_journal_exactly_joins_c_and_preserves_write(self) -> None:
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
        index["inputs"] = [
            item for item in index["inputs"]
            if item.get("scope_id") != operation_id
            or item.get("subject", {}).get("stage") != "processor"
        ]
        health_archive_item = next(
            item for item in index["inputs"]
            if item.get("scope_id") == operation_id
            and item.get("subject", {}).get("stage") == "health"
            and item["role"] == "pipeline_artifact_archive"
        )
        archive_raw = (ROOT / health_archive_item["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as archive:
            journal = json.loads(archive.read("promotion/reports/canonical-update-promotion-receipt.json"))
        original_updated_at = journal["updated_at"]
        journal["updated_at"] = "2026-10-04T13:23:01Z"
        self.assertNotEqual(journal["updated_at"], original_updated_at)
        valid_schema_different_journal = (
            json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()

        with tempfile.TemporaryDirectory(prefix="completeness-health-c-journal-join-") as name:
            input_root = pathlib.Path(name)
            index_path = self._write_rebound_health_archive(
                index, input_root,
                "promotion/reports/canonical-update-promotion-receipt.json",
                valid_schema_different_journal,
            )
            with self.assertRaisesRegex(ValueError, "health archived promotion journal differs from the exact supplied C state"):
                MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

            json_output = input_root / "existing-rollup.json"
            markdown_output = input_root / "existing-rollup.md"
            json_output.write_bytes(b"prior-json-report\n")
            markdown_output.write_bytes(b"prior-markdown-report\n")
            output_paths = {"rollup.json": json_output, "rollup.md": markdown_output}
            with mock.patch.object(
                MODULE, "output_path",
                side_effect=lambda _root, value, _label: output_paths[value],
            ):
                result = MODULE.main([
                    "--repo-root", str(ROOT),
                    "--input-root", str(input_root),
                    "--input-index", str(index_path),
                    "--output-json", "rollup.json",
                    "--output-markdown", "rollup.md",
                    "--write",
                ])
            self.assertEqual(result, 1)
            self.assertEqual(json_output.read_bytes(), b"prior-json-report\n")
            self.assertEqual(markdown_output.read_bytes(), b"prior-markdown-report\n")

    def test_health_local_snapshot_schema_and_ref_fail_before_missing_b_c_return(self) -> None:
        registry, _policy, _scope_by_id = MODULE.load_and_validate_registry(ROOT)
        operation_id = next(
            scope["scope_id"] for scope in registry["scopes"]
            if scope["resource_kind"] == "api_operation_manifest" and scope["source_id"] == "data_go_kr"
        )
        cases = (
            (
                "promotion/reports/canonical-update-promotion-receipt.json", b"{}\n",
                "health archived promotion journal",
            ),
            (
                "processor-ref", b"bad\trefs/heads/automation/upstream-catalogue-state\n",
                "health archived processor-ref",
            ),
            (
                "promotion-ref", b"0" * 40 + b"\trefs/heads/foreign-state\n",
                "health archived promotion-ref",
            ),
        )
        for member_name, bad_bytes, expected in cases:
            with self.subTest(member=member_name):
                index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_bytes())
                index["inputs"] = [
                    item for item in index["inputs"]
                    if item.get("scope_id") != operation_id
                    or item.get("subject", {}).get("stage") not in {"processor", "promotion"}
                ]
                with tempfile.TemporaryDirectory(prefix="completeness-health-local-snapshot-") as name:
                    input_root = pathlib.Path(name)
                    index_path = self._write_rebound_health_archive(index, input_root, member_name, bad_bytes)
                    with self.assertRaisesRegex(ValueError, expected):
                        MODULE.build_report(root=ROOT, input_root=input_root, input_index_path=index_path)

    def test_retained_full_chain_uses_registry_defined_catalog_scope_id(self) -> None:
        scope_registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index, checked, resolved = MODULE.validate_input_index(
            root=ROOT, input_root=ROOT, index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            scope_by_id=scope_by_id,
        )
        changed_registry = copy.deepcopy(scope_registry)
        catalog_scope = next(
            scope for scope in changed_registry["scopes"]
            if scope["source_id"] == "data_go_kr"
            and scope["resource_kind"] == "api_catalog_metadata"
        )
        old_scope_id = catalog_scope["scope_id"]
        catalog_scope["scope_id"] = f"{old_scope_id}-reviewed-alias"
        changed_inputs = copy.deepcopy(checked)
        for item in changed_inputs:
            if item["scope_id"] == old_scope_id:
                item["scope_id"] = catalog_scope["scope_id"]
        operation_scope = next(
            scope for scope in changed_registry["scopes"]
            if scope["source_id"] == "data_go_kr"
            and scope["resource_kind"] == "api_operation_manifest"
        )
        operation_inputs = [
            item for item in changed_inputs if item["scope_id"] == operation_scope["scope_id"]
        ]

        result = MODULE.validate_pipeline_evidence(
            root=ROOT, operation_inputs=operation_inputs, all_inputs=changed_inputs,
            resolved=resolved, evaluation_epoch=index["evaluation_epoch"],
            scope_registry=changed_registry,
        )

        self.assertEqual(result["status"], "verified_historical_chain")
        self.assertEqual(result["current_registry"]["sha256"], "0520d0db0d9ee07b7cbccce0c08439d0b02be901bf10e8491187d96e59d7a0d0")

    def test_native_publisher_validator_binds_actual_run_receipt_and_artifact(self) -> None:
        publication_root = ROOT / "reports/completeness-proof-evidence/issue-659/publication"
        run = json.loads((publication_root / "publisher-run.json").read_bytes())
        jobs = json.loads((publication_root / "publisher-jobs.json").read_bytes())
        artifacts = json.loads((publication_root / "publisher-artifacts.json").read_bytes())
        archive = (publication_root / "publisher-receipts.zip").read_bytes()
        receipt = (publication_root / "publisher-receipt.json").read_bytes()
        binding = (publication_root / "publisher-source-binding.json").read_bytes()

        result = MODULE.validate_native_publisher_attempt(
            root=ROOT, run=run, jobs=jobs, artifact_response=artifacts,
            archive=archive, receipt_raw=receipt, binding_raw=binding,
            evaluation_epoch="2026-10-04T13:23:00Z",
        )
        self.assertEqual(result["run_id"], "37199628001")
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(result["event"], "workflow_dispatch")
        self.assertEqual(result["source_sha"], "6a5138c792f4b7402da0c5ab439646bd752a307f")
        self.assertLessEqual(
            result["native_verification_started_at"],
            result["native_verification_completed_at"],
        )

        malformed_artifacts = copy.deepcopy(artifacts)
        malformed_artifacts["total_count"] = True
        with self.assertRaisesRegex(ValueError, "artifact_list_incomplete"):
            MODULE.validate_native_publisher_attempt(
                root=ROOT, run=run, jobs=jobs, artifact_response=malformed_artifacts,
                archive=archive, receipt_raw=receipt, binding_raw=binding,
                evaluation_epoch="2026-10-04T13:23:00Z",
            )

    def test_native_distribution_inventory_binds_191_manifest_rows_and_five_exact_extras(self) -> None:
        publication_root = ROOT / "reports/completeness-proof-evidence/issue-659/publication"
        manifest_raw = (publication_root / "publisher-source-manifest.json").read_bytes()
        manifest = json.loads(manifest_raw)
        pointer = json.loads((publication_root / "publisher-pointer-immutable.json").read_bytes())
        receipt = json.loads((publication_root / "publisher-receipt.json").read_bytes())
        source_sha = receipt["source_binding"]["source_sha"]
        workflow_sha = receipt["source_binding"]["workflow_sha"]
        source_script = subprocess.check_output(
            ["git", "show", f"{source_sha}:scripts/huggingface_registry_distribution.py"], cwd=ROOT,
        )
        workflow = subprocess.check_output(
            ["git", "show", f"{workflow_sha}:.github/workflows/huggingface-distribution.yml"], cwd=ROOT,
        )

        result = MODULE.validate_native_distribution_inventory(
            manifest=manifest, manifest_raw=manifest_raw, pointer=pointer,
            receipt=receipt, source_script_raw=source_script, workflow_raw=workflow,
        )

        self.assertEqual(result["source_manifest_artifacts"], 191)
        self.assertEqual(result["distribution_artifacts"], 196)
        self.assertEqual(len(result["verified_artifacts"]), 196)
        members = {item["path"]: item for item in result["verified_artifacts"]}
        self.assertIn("RELEASE_NOTES.md", members)
        self.assertIn("reports/latest-release-readiness.json", members)
        self.assertEqual(len(result["workflow_extras"]), 5)

        mutations = []
        truncated = copy.deepcopy(pointer)
        truncated["artifacts"].pop()
        truncated["artifact_count"] -= 1
        mutations.append((truncated, "receipt counts"))
        duplicate = copy.deepcopy(pointer)
        duplicate["artifacts"][-1] = copy.deepcopy(duplicate["artifacts"][0])
        mutations.append((duplicate, "duplicate or non-canonical"))
        changed = copy.deepcopy(pointer)
        changed["artifacts"][0]["sha256"] = "0" * 64
        mutations.append((changed, "changes a source-manifest artifact"))
        substituted = copy.deepcopy(pointer)
        extra_row = next(
            row for row in substituted["artifacts"]
            if row["path"] == "reports/latest-release-readiness.json"
        )
        extra_row["path"] = "reports/unreviewed-extra.json"
        mutations.append((substituted, "missing or unreviewed workflow extras"))
        for candidate, message in mutations:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                MODULE.validate_native_distribution_inventory(
                    manifest=manifest, manifest_raw=manifest_raw, pointer=candidate,
                    receipt=receipt, source_script_raw=source_script, workflow_raw=workflow,
                )
        with self.assertRaisesRegex(ValueError, "no reviewed all-artifact verifier contract"):
            MODULE.validate_native_distribution_inventory(
                manifest=manifest, manifest_raw=manifest_raw, pointer=pointer,
                receipt=receipt, source_script_raw=source_script + b"\n# changed",
                workflow_raw=workflow,
            )

    def test_consumer_readback_binds_a_parameterized_release_artifact(self) -> None:
        publication_root = ROOT / "reports/completeness-proof-evidence/issue-659/publication"
        run = json.loads((publication_root / "publisher-run.json").read_bytes())
        jobs = json.loads((publication_root / "publisher-jobs.json").read_bytes())
        artifact_api = json.loads((publication_root / "publisher-artifacts.json").read_bytes())
        publication = MODULE.validate_native_publisher_attempt(
            root=ROOT, run=run, jobs=jobs, artifact_response=artifact_api,
            archive=(publication_root / "publisher-receipts.zip").read_bytes(),
            receipt_raw=(publication_root / "publisher-receipt.json").read_bytes(),
            binding_raw=(publication_root / "publisher-source-binding.json").read_bytes(),
            evaluation_epoch="2026-10-04T13:23:00Z",
        )
        manifest = json.loads(subprocess.check_output(
            ["git", "show", f"{publication['source_sha']}:manifest.json"],
            cwd=ROOT,
        ))
        artifact = next(
            item for item in manifest["artifacts"]
            if item["path"] not in {"data/data-go-kr.registry.json", "manifest.json"}
        )
        native_observed_at = publication["native_verification_completed_at"]
        reported_at = run["updated_at"]
        proof = {
            "publication": {"artifact": {"path": artifact["path"], "sha256": artifact["sha256"]}},
            "consumer_read_back": {"state": "proven", "observed_at": native_observed_at, "artifact_sha256": artifact["sha256"]},
        }
        readback = {
            "publisher_run_id": run["id"],
            "publisher_attempt": run["run_attempt"],
            "generated_at": reported_at,
            "checks": [{"detail": {
                "path": artifact["path"], "bytes_streamed": artifact["bytes"],
                "sha256": artifact["sha256"], "revision": publication["payload_revision"],
            }}],
        }
        publisher_job = jobs["jobs"][0]
        verified = MODULE.validate_consumer_readback(
            readback=readback, proof=proof, publication=publication,
            publisher_job=publisher_job, evaluation_epoch="2026-10-04T13:23:00Z",
            publisher_run=run, expected_bytes=artifact["bytes"],
        )
        self.assertEqual(verified["path"], artifact["path"])
        self.assertEqual(verified["sha256"], artifact["sha256"])
        self.assertEqual(verified["revision"], publication["payload_revision"])
        self.assertEqual(verified["observed_at"], native_observed_at)

        readback["checks"][0]["detail"]["bytes_streamed"] += 1
        with self.assertRaisesRegex(ValueError, "consumer stream bytes"):
            MODULE.validate_consumer_readback(
                readback=readback, proof=proof, publication=publication,
                publisher_job=publisher_job, evaluation_epoch="2026-10-04T13:23:00Z",
                publisher_run=run, expected_bytes=artifact["bytes"],
            )

    def test_stage_job_attempt_bool_is_not_accepted_as_attempt_one(self) -> None:
        _registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index, checked, resolved = MODULE.validate_input_index(
            root=ROOT, input_root=ROOT, index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            scope_by_id=scope_by_id,
        )
        scoped = [item for item in checked if item["scope_id"] == "data-go-kr.api-operations"]
        roles = MODULE.stage_input_map(scoped, "source")
        MODULE.stage_value(roles, resolved, "pipeline_run")
        original_jobs = MODULE.stage_value(roles, resolved, "pipeline_jobs")
        corrupted = copy.deepcopy(original_jobs)
        corrupted["jobs"][0]["run_attempt"] = True
        with tempfile.TemporaryDirectory() as directory:
            mutated = pathlib.Path(directory) / "jobs.json"
            mutated.write_text(json.dumps(corrupted))
            mutated_role = copy.deepcopy(roles["pipeline_jobs"])
            mutated_role["input_id"] = "mutated-jobs"
            resolved["mutated-jobs"] = mutated
            roles["pipeline_jobs"] = mutated_role
            with self.assertRaisesRegex(ValueError, "exact successful run attempt"):
                MODULE.validate_stage_run(
                    stage="source", roles=roles, resolved=resolved,
                    evaluation_epoch=index["evaluation_epoch"], root=ROOT,
                )

    def test_indexed_pipeline_input_must_bind_the_same_trusted_repository(self) -> None:
        _registry, _policy, scope_by_id = MODULE.load_and_validate_registry(ROOT)
        index, checked, resolved = MODULE.validate_input_index(
            root=ROOT, input_root=ROOT, index_path=ROOT / MODULE.INPUT_INDEX_PATH,
            scope_by_id=scope_by_id,
        )
        scoped = [item for item in checked if item["scope_id"] == "data-go-kr.api-operations"]
        roles = MODULE.stage_input_map(scoped, "source")
        roles["pipeline_artifact_archive"] = copy.deepcopy(roles["pipeline_artifact_archive"])
        roles["pipeline_artifact_archive"]["producer"]["repository"] = "Other/repository"
        with self.assertRaisesRegex(ValueError, "indexed inputs disagree"):
            MODULE.validate_stage_run(
                stage="source", roles=roles, resolved=resolved,
                evaluation_epoch=index["evaluation_epoch"], root=ROOT,
            )

    def test_unmerged_candidate_commit_is_not_trusted_main_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Completeness test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "completeness@example.invalid"], cwd=repo, check=True)
            (repo / "file").write_text("base\n")
            subprocess.run(["git", "add", "file"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "main base"], cwd=repo, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", base], cwd=repo, check=True)
            subprocess.run(["git", "checkout", "-qb", "candidate"], cwd=repo, check=True)
            (repo / "file").write_text("candidate\n")
            subprocess.run(["git", "commit", "-qam", "unmerged candidate"], cwd=repo, check=True)
            candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
            (repo / "file").write_text("main advance\n")
            subprocess.run(["git", "commit", "-qam", "main advance"], cwd=repo, check=True)
            main = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", main], cwd=repo, check=True)
            with self.assertRaisesRegex(ValueError, "not reachable from the selected trusted origin/main history"):
                MODULE.assert_main_ancestor(repo, candidate, "test candidate")

    def test_input_row_order_does_not_change_canonical_rollup(self) -> None:
        index = json.loads((ROOT / MODULE.INPUT_INDEX_PATH).read_text())
        reordered = copy.deepcopy(index)
        reordered["inputs"] = list(reversed(reordered["inputs"]))

        with tempfile.TemporaryDirectory() as directory:
            directory_path = pathlib.Path(directory)
            original_index = directory_path / "original.json"
            alternate_index = directory_path / "reordered.json"
            original_index.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n")
            alternate_index.write_text(json.dumps(reordered, ensure_ascii=False, separators=(",", ":")) + "\n")
            baseline = MODULE.build_report(
                root=ROOT,
                input_root=ROOT,
                input_index_path=original_index,
            )
            reordered_report = MODULE.build_report(
                root=ROOT,
                input_root=ROOT,
                input_index_path=alternate_index,
            )

        self.assertEqual(MODULE.render_json(baseline), MODULE.render_json(reordered_report))
        self.assertEqual(MODULE.render_markdown(baseline), MODULE.render_markdown(reordered_report))

    def test_indexed_claim_cannot_bind_bytes_different_from_committed_source(self) -> None:
        item = {
            "root": "repository",
            "path": "sources/data_go_kr.json",
            "namespace": "live_operational",
            "producer": {"repository": "StatPan/datapan-registry", "revision": "849af2936a743573358820275df985b5809d0f7b"},
            "bytes": 1,
            "sha256": "0" * 64,
        }
        with tempfile.NamedTemporaryFile() as tampered:
            tampered.write(b"{}")
            tampered.flush()
            with self.assertRaisesRegex(ValueError, "receipt bytes changed"):
                MODULE.verify_main_committed_input(
                    root=ROOT,
                    item=item,
                    path=pathlib.Path(tampered.name),
                    label="test evidence",
                )


if __name__ == "__main__":
    unittest.main()
