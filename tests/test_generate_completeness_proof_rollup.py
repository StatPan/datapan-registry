from __future__ import annotations

import copy
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


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
                observed: str | None = None, producer_revision: str | None = None,
                subject: dict[str, object] | None = None, producer: dict[str, object] | None = None,
            ) -> dict[str, object]:
                return {
                    "input_id": input_id, "scope_id": scope["scope_id"], "role": role,
                    "artifact_type": artifact_type, "root": "repository", "path": path,
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
                    operation_path, source_bytes, observed=observed_at,
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
                root=temp_root, input_root=temp_root, input_index_path=index_path,
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
            candidate_baseline_index = copy.deepcopy(original_index)
            candidate_baseline_index["evaluation_epoch"] = input_index["evaluation_epoch"]
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
                "--repo-root", str(temp_root), "--input-root", str(temp_root),
                "--candidate-registry", str(candidate_registry),
                "--candidate-operation-manifest", str(candidate_operation_manifest),
            ]
            self.assertEqual(MODULE.main([*candidate_args, "--write"]), 0)
            candidate_report = json.loads((temp_root / MODULE.OUTPUT_JSON_PATH).read_bytes())
            candidate_operations = next(row for row in candidate_report["scopes"] if row["scope_id"] == scope["scope_id"])
            self.assertEqual(candidate_operations["claims"], {"complete": False, "current": False, "updated": False})
            self.assertIsNone(candidate_operations["proof"])
            self.assertEqual(candidate_report["evaluation_context"]["mode"], "candidate")
            candidate_facets = {facet["facet_id"]: facet for facet in candidate_operations["facets"]}
            self.assertEqual(candidate_facets["candidate_inventory"]["state"], "blocked")
            self.assertEqual(candidate_facets["specification_pipeline"]["state"], "historical")
            self.assertEqual(candidate_facets["immutable_publication_read_back"]["state"], "historical")

            watched_paths = [canonical_index_path, temp_root / MODULE.OUTPUT_JSON_PATH, temp_root / MODULE.OUTPUT_MARKDOWN_PATH]
            watched_before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in watched_paths]
            self.assertEqual(MODULE.main([*candidate_args, "--check"]), 0)
            self.assertEqual(watched_before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in watched_paths])

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
                MODULE.build_report(root=temp_root, input_root=temp_root, input_index_path=index_path)

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
