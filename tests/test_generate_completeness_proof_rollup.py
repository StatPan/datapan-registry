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
        self.assertFalse(facets["source_observation"]["details"]["new_post_repair_observation"])
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
        self.assertEqual(result["publication"]["status"], "verified")
        self.assertFalse(result["publication"]["details"]["currentness_established"])
        self.assertIn("main_ancestry_checked_tip", result["promotion"])
        self.assertNotIn("main_sha", result["promotion"])

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
                root=ROOT, operation_inputs=scoped, all_inputs=checked,
                resolved=resolved, evaluation_epoch=index["evaluation_epoch"],
                scope_registry=scope_registry,
            )
        self.assertEqual(future["status"], "verified_historical_chain")
        self.assertIsNone(future["publication"])
        self.assertEqual(future["publisher_attempt"], future_native_attempt)

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
            self.assertEqual(publication_facet["state"], "missing")
            self.assertEqual(publication_facet["details"]["missing_stages"], ["acknowledgement"])
            self.assertEqual(publication_facet["details"]["publisher_attempt"]["run_id"], "37199628001")

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
