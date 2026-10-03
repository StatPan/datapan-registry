from __future__ import annotations

import json
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts/refresh-canonical-snapshot-evidence.py"
SPEC = importlib.util.spec_from_file_location("refresh_canonical_snapshot_evidence", MODULE_PATH)
assert SPEC and SPEC.loader
refresh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = refresh
SPEC.loader.exec_module(refresh)


PINNED_CLI = "e3b2fa44cb0f68d7e58e003fbd71ecde505878de"


class SourceRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name) / "registry"
        self.cli = pathlib.Path(self.temporary.name) / "datapan-cli"
        self.root.mkdir()
        self.cli.mkdir()
        (self.cli / ".git").write_text("gitdir: /temporary/fake", encoding="utf-8")
        (self.root / "policy").mkdir()
        (self.root / "schemas").mkdir()
        (self.root / "policy/external-checkout-refs.json").write_text(json.dumps({"repository": "StatPan/datapan-cli", "ref": PINNED_CLI}), encoding="utf-8")
        (self.root / "data").mkdir()
        (self.root / "reports").mkdir()
        (self.root / "data/data-go-kr.registry.json").write_text("[]\n", encoding="utf-8")
        (self.root / "reports/latest-verification.json").write_text("{}\n", encoding="utf-8")
        (self.root / "reports/health-probe-catalog.json").write_text("{}\n", encoding="utf-8")
        (self.root / "schemas/datapan.diagnostic-current-source-applicability.v1.schema.json").write_text(json.dumps({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["schema_version", "status", "current_inputs", "authority"],
            "properties": {
                "schema_version": {"const": "datapan.diagnostic-current-source-applicability.v1"},
                "status": {"enum": ["historical_scope_unchanged", "revalidation_required"]},
                "current_inputs": {
                    "type": "object",
                    "required": ["registry", "health_catalog"],
                    "properties": {
                        "registry": {"$ref": "#/$defs/input"},
                        "health_catalog": {"$ref": "#/$defs/input"},
                    },
                },
                "authority": {
                    "type": "object",
                    "required": [
                        "current_source_compatibility_approved", "current_runtime_evidence_accepted",
                        "current_consumer_approval", "release_readiness_granted", "publication_allowed",
                        "human_acceptance_granted",
                    ],
                    "properties": {
                        key: {"const": False} for key in (
                            "current_source_compatibility_approved", "current_runtime_evidence_accepted",
                            "current_consumer_approval", "release_readiness_granted", "publication_allowed",
                            "human_acceptance_granted",
                        )
                    },
                },
            },
            "$defs": {"input": {
                "type": "object", "required": ["path", "bytes", "sha256"],
                "properties": {"path": {"type": "string"}, "bytes": {"type": "integer"}, "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
            }},
        }), encoding="utf-8")
        (self.root / ".datapan/previous").mkdir(parents=True, exist_ok=True)
        (self.root / ".datapan/previous/data-go-kr.registry.json").write_text("[]\n", encoding="utf-8")
        self.bad_diagnostic_authority = False
        self.bad_diagnostic_registry_binding = False

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_real_source_command_inventory_rejects_a_missing_script(self) -> None:
        command = refresh.SourceCommand(("python3", "scripts/validate-diagnostic-publication.py"), self.root)
        with self.assertRaisesRegex(refresh.SourceRefreshError, "missing or unsafe"):
            refresh.validate_root_source_scripts((command,), self.root)
        script = self.root / "scripts/validate-diagnostic-publication.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("pass\n", encoding="utf-8")
        refresh.validate_root_source_scripts((command,), self.root)

    def test_runtime_plan_generator_uses_confined_repository_relative_inputs(self) -> None:
        commands = refresh.build_commands(
            self.root,
            self.cli,
            self.root / "data/data-go-kr.registry.json",
            self.root / "reports/latest-verification.json",
        )
        runtime_plan = next(
            command for command in commands
            if len(command.argv) > 1 and command.argv[1] == "scripts/generate-institution-runtime-plan.py"
        )
        self.assertEqual(
            runtime_plan.argv[runtime_plan.argv.index("--registry") + 1],
            "data/data-go-kr.registry.json",
        )
        self.assertEqual(
            runtime_plan.argv[runtime_plan.argv.index("--latest-verification") + 1],
            "reports/latest-verification.json",
        )
        for label in (
            runtime_plan.argv[runtime_plan.argv.index("--registry") + 1],
            runtime_plan.argv[runtime_plan.argv.index("--latest-verification") + 1],
        ):
            self.assertTrue((self.root / label).is_file())

        outside_registry = pathlib.Path(self.temporary.name) / "outside.registry.json"
        outside_registry.write_text("[]\n", encoding="utf-8")
        with self.assertRaisesRegex(refresh.SourceRefreshError, "outside the repository root"):
            refresh.build_commands(
                self.root,
                self.cli,
                outside_registry,
                self.root / "reports/latest-verification.json",
            )

        symlink_root = pathlib.Path(self.temporary.name) / "symlink-root"
        (symlink_root / "data").mkdir(parents=True)
        (symlink_root / "reports").mkdir()
        (symlink_root / "reports/latest-verification.json").write_text("{}\n", encoding="utf-8")
        (symlink_root / "data/data-go-kr.registry.json").symlink_to(outside_registry)
        with self.assertRaisesRegex(refresh.SourceRefreshError, "outside the repository root"):
            refresh.build_commands(
                symlink_root,
                self.cli,
                symlink_root / "data/data-go-kr.registry.json",
                symlink_root / "reports/latest-verification.json",
            )

    def test_runtime_plan_report_validates_after_relocation(self) -> None:
        scripts = MODULE_PATH.parent
        generator_name = "generate-institution-runtime-plan.py"
        validator_name = "validate-institution-runtime-plan.py"
        (self.root / "scripts").mkdir()
        for script in (generator_name, validator_name):
            shutil.copy2(scripts / script, self.root / "scripts" / script)
        shutil.copy2(
            scripts.parent / "schemas/datapan.institution-runtime-plan.v1.schema.json",
            self.root / "schemas/datapan.institution-runtime-plan.v1.schema.json",
        )
        backlog_path = self.root / "reports/data-go-kr/coverage-backlog.json"
        backlog_path.parent.mkdir(parents=True, exist_ok=True)
        backlog_path.write_text(json.dumps({
            "schema_version": "datapan.coverage-backlog.v1",
            "generated_at": "2026-10-03T00:00:00Z",
            "provider": "data.go.kr",
            "source_id": "data_go_kr",
            "summary": {"institutions": 1},
            "institutions": [{
                "organization": "Example Institution",
                "api_count": 1,
                "covered_api_count": 0,
                "uncovered_api_count": 1,
                "operation_count": 1,
                "runtime_evidence_api_count": 0,
                "runtime_reactivation_api_count": 1,
                "runtime_missing_evidence_count": 1,
                "approval_required_operations": 0,
                "priority_score": 1,
            }],
        }), encoding="utf-8")

        # Reproduce the old producer output: it validates in the source tree,
        # but fails once Verify checks the same report from a nested checkout.
        registry_abs = str((self.root / "data/data-go-kr.registry.json").resolve())
        verification_abs = str((self.root / "reports/latest-verification.json").resolve())
        old_command = (
            sys.executable,
            "scripts/generate-institution-runtime-plan.py",
            "--registry", registry_abs,
            "--latest-verification", verification_abs,
        )
        subprocess.run(old_command, cwd=self.root, check=True, capture_output=True, text=True)
        old_relocated = pathlib.Path(self.temporary.name) / "verify-old" / "nested" / "datapan-registry"
        shutil.copytree(self.root, old_relocated)
        hidden_producer_root = pathlib.Path(self.temporary.name) / "producer-root-unavailable"
        self.root.rename(hidden_producer_root)
        try:
            failed = subprocess.run(
                [sys.executable, "scripts/validate-institution-runtime-plan.py"],
                cwd=old_relocated,
                capture_output=True,
                text=True,
            )
        finally:
            hidden_producer_root.rename(self.root)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("generation_inputs.registry does not exist", failed.stderr)

        # The corrected command records relocatable input paths and produces
        # relocatable batch invocations; no source-tree absolute prefix leaks.
        fixed_command = next(
            command.argv for command in refresh.build_commands(
                self.root,
                self.cli,
                self.root / "data/data-go-kr.registry.json",
                self.root / "reports/latest-verification.json",
            )
            if len(command.argv) > 1 and command.argv[1] == f"scripts/{generator_name}"
        )
        subprocess.run(fixed_command, cwd=self.root, check=True, capture_output=True, text=True)
        report = json.loads((self.root / "reports/data-go-kr/institution-runtime-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(report["generation_inputs"]["registry"], "data/data-go-kr.registry.json")
        self.assertEqual(report["generation_inputs"]["latest_verification"], "reports/latest-verification.json")
        self.assertTrue(all(
            registry_abs not in row["command"] and verification_abs not in row["command"]
            for row in report["batches"]
        ))

        fixed_relocated = pathlib.Path(self.temporary.name) / "verify-fixed" / "nested" / "datapan-registry"
        shutil.copytree(self.root, fixed_relocated)
        self.root.rename(hidden_producer_root)
        try:
            validated = subprocess.run(
                [sys.executable, f"scripts/{validator_name}"],
                cwd=fixed_relocated,
                check=True,
                capture_output=True,
                text=True,
            )
        finally:
            hidden_producer_root.rename(self.root)
        self.assertIn("ok reports/data-go-kr/institution-runtime-plan.json", validated.stdout)

    @staticmethod
    def result(argv: tuple[str, ...], cwd: pathlib.Path, returncode: int = 0, stdout_override: str | None = None) -> subprocess.CompletedProcess[str]:
        if argv == ("git", "rev-parse", "HEAD"):
            stdout = PINNED_CLI
        elif argv == ("git", "rev-parse", "HEAD^{tree}"):
            stdout = "a" * 40
        else:
            stdout = ""
        if stdout_override is not None:
            stdout = stdout_override
        return subprocess.CompletedProcess(list(argv), returncode, stdout, "")

    def fake_run(self, argv: tuple[str, ...], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ("go", "run", "./cmd/datapan"):
            output = pathlib.Path(argv[argv.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"generated_at": "2026-10-01T00:00:00Z", "registry": str(self.root / "data/data-go-kr.registry.json")}), encoding="utf-8")
        elif len(argv) > 1 and argv[1].endswith("generate-health-probe-catalog.py"):
            (self.root / "reports/health-probe-catalog.json").write_text('{"health":"refreshed"}\n', encoding="utf-8")
        elif len(argv) > 1 and argv[1].endswith("generate-diagnostic-current-source-applicability.py") and argv[-1:] == ("--write",):
            registry = self.root / "data/data-go-kr.registry.json"
            health = self.root / "reports/health-probe-catalog.json"
            authority = {
                "current_source_compatibility_approved": False,
                "current_runtime_evidence_accepted": False,
                "current_consumer_approval": False,
                "release_readiness_granted": False,
                "publication_allowed": False,
                "human_acceptance_granted": False,
            }
            if self.bad_diagnostic_authority:
                authority["publication_allowed"] = True
            registry_bytes = registry.read_bytes()
            health_bytes = health.read_bytes()
            registry_identity = {
                "path": "data/data-go-kr.registry.json",
                "bytes": len(registry_bytes),
                "sha256": refresh.hashlib.sha256(registry_bytes).hexdigest(),
            }
            if self.bad_diagnostic_registry_binding:
                registry_identity["sha256"] = "0" * 64
            (self.root / "reports/diagnostic-current-source-applicability.json").write_text(json.dumps({
                "schema_version": "datapan.diagnostic-current-source-applicability.v1",
                "status": "revalidation_required",
                "current_inputs": {
                    "registry": registry_identity,
                    "health_catalog": {
                        "path": "reports/health-probe-catalog.json",
                        "bytes": len(health_bytes),
                        "sha256": refresh.hashlib.sha256(health_bytes).hexdigest(),
                    },
                },
                "authority": authority,
            }), encoding="utf-8")
        return self.result(argv, cwd)

    def test_source_refresh_orders_native_source_generators_before_ledger(self) -> None:
        calls: list[tuple[tuple[str, ...], pathlib.Path]] = []

        def fake_run(argv: tuple[str, ...], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
            calls.append((argv, cwd))
            return self.fake_run(argv, cwd)

        registry_sha, labels, evidence = refresh.run_source_refresh(
            repository_root=self.root,
            datapan_cli=self.cli,
            registry=self.root / "data/data-go-kr.registry.json",
            verification=self.root / "reports/latest-verification.json",
            previous_registry=self.root / ".datapan/previous/data-go-kr.registry.json",
            run=fake_run,
        )
        self.assertEqual(len(registry_sha), 64)
        self.assertEqual(calls[0][0][:5], ("git", "rev-parse", "HEAD")[:5])
        native = [argv for argv, cwd in calls if cwd == self.cli and argv[:3] == ("go", "run", "./cmd/datapan")]
        native_commands = [tuple(argv[3:5]) for argv in native]
        self.assertEqual(native_commands[:2], [("catalog", "audit"), ("catalog", "errors")])
        self.assertIn(("catalog", "diff"), native_commands)
        self.assertTrue(all("--registry" in argv or "--new" in argv for argv in native))
        self.assertTrue(all(str((self.root / "data/data-go-kr.registry.json").resolve()) in argv for argv in native))
        self.assertEqual((self.root / "data/data-go-kr.registry.json").read_text(encoding="utf-8"), "[]\n")
        self.assertEqual(evidence["registry_sha256"], registry_sha)
        self.assertEqual(evidence["verification_sha256"], refresh.hashlib.sha256((self.root / "reports/latest-verification.json").read_bytes()).hexdigest())
        self.assertIn("top_level_generated_at", evidence["output_normalization"])
        self.assertEqual(len(evidence["commands"]), 9)
        applicability = evidence["diagnostic_current_source_applicability"]
        self.assertEqual(applicability["status"], "revalidation_required")
        self.assertEqual(applicability["path"], "reports/diagnostic-current-source-applicability.json")
        self.assertEqual(applicability["schema_path"], "schemas/datapan.diagnostic-current-source-applicability.v1.schema.json")
        self.assertTrue(all(row["raw_output_sha256"] and row["output_sha256"] for row in evidence["commands"]))
        self.assertTrue(all(
            (row.get("input_verification_sha256") == evidence["verification_sha256"])
            == ("--verification" in row["argv"])
            for row in evidence["commands"]
        ))
        self.assertTrue(any(row["raw_output_sha256"] != row["output_sha256"] for row in evidence["commands"]))
        self.assertEqual({tuple(row["argv"][3:5]) for row in evidence["commands"]}, {
            ("catalog", "audit"), ("catalog", "errors"), ("catalog", "dependencies"),
            ("catalog", "adapter-targets"), ("catalog", "providers"), ("catalog", "diff"),
            ("catalog", "route-disposition"), ("catalog", "coverage"), ("catalog", "verify"),
        })
        self.assertFalse(any("release" in row["argv"] and "draft" in row["argv"] for row in evidence["commands"]))
        labels_joined = "\n".join(labels)
        self.assertLess(labels_joined.index("generate-data-go-kr-operation-denominator.py"), labels_joined.index("generate-operation-denominator-rollup.py"))
        self.assertLess(labels_joined.index("generate-data-go-kr-operation-manifest.py"), labels_joined.index("update-data-go-kr-operation-denominator-expectation.py"))
        self.assertLess(labels_joined.index("generate-current-runtime-evidence-projection.py"), labels_joined.index("generate-runtime-freshness-queue.py"))
        self.assertLess(labels_joined.index("generate-runtime-freshness-queue.py"), labels_joined.index("generate-sustainable-coverage.py"))
        health_generated = labels.index(next(label for label in labels if "generate-health-probe-catalog.py" in label))
        health_validated = labels.index(next(label for label in labels if "validate-health-probe-catalog.py" in label))
        health_manifest_sync = next(
            index for index, label in enumerate(labels)
            if index > health_generated and "sync-release-manifest-artifacts.py --write" in label
        )
        self.assertLess(health_generated, health_manifest_sync)
        self.assertLess(health_manifest_sync, health_validated)
        applicability_generated = labels.index(next(label for label in labels if "generate-diagnostic-current-source-applicability.py --write" in label))
        applicability_manifest_sync = next(
            index for index, label in enumerate(labels)
            if index > applicability_generated and "sync-release-manifest-artifacts.py --write" in label
        )
        applicability_checked = labels.index(next(label for label in labels if "generate-diagnostic-current-source-applicability.py --check" in label))
        applicability_validated = labels.index(next(label for label in labels if "validate-diagnostic-current-source-applicability.py" in label))
        historical_mapping_validated = labels.index(next(label for label in labels if "validate-diagnostic-evidence-mapping-draft.py" in label))
        self.assertLess(health_validated, applicability_generated)
        self.assertLess(applicability_generated, applicability_manifest_sync)
        self.assertLess(applicability_manifest_sync, applicability_checked)
        self.assertLess(applicability_checked, applicability_validated)
        self.assertLess(applicability_validated, historical_mapping_validated)
        self.assertFalse(any("refresh-release-ledger-evidence.py" in " ".join(argv) for argv, _cwd in calls))

    def test_pending_applicability_is_recorded_without_granting_authority(self) -> None:
        _registry_sha, _labels, evidence = refresh.run_source_refresh(
            repository_root=self.root,
            datapan_cli=self.cli,
            registry=self.root / "data/data-go-kr.registry.json",
            verification=self.root / "reports/latest-verification.json",
            run=self.fake_run,
        )
        self.assertEqual(evidence["diagnostic_current_source_applicability"]["status"], "revalidation_required")

    def test_current_applicability_cannot_grant_publication_authority(self) -> None:
        self.bad_diagnostic_authority = True
        with self.assertRaisesRegex(refresh.SourceRefreshError, "must not grant approval or publication authority"):
            refresh.run_source_refresh(
                repository_root=self.root,
                datapan_cli=self.cli,
                registry=self.root / "data/data-go-kr.registry.json",
                verification=self.root / "reports/latest-verification.json",
                run=self.fake_run,
            )

    def test_current_applicability_must_bind_exact_candidate_registry(self) -> None:
        self.bad_diagnostic_registry_binding = True
        with self.assertRaisesRegex(refresh.SourceRefreshError, "exact admitted candidate registry"):
            refresh.run_source_refresh(
                repository_root=self.root,
                datapan_cli=self.cli,
                registry=self.root / "data/data-go-kr.registry.json",
                verification=self.root / "reports/latest-verification.json",
                run=self.fake_run,
            )

    def test_source_failure_stops_before_later_projections_and_ledger(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if any("generate-current-runtime-evidence-projection.py" in arg for arg in argv):
                return self.result(argv, cwd, returncode=1)
            return self.fake_run(argv, cwd)

        with self.assertRaisesRegex(refresh.SourceRefreshError, "before ledger refresh"):
            refresh.run_source_refresh(
                repository_root=self.root,
                datapan_cli=self.cli,
                registry=self.root / "data/data-go-kr.registry.json",
                verification=self.root / "reports/latest-verification.json",
                run=fake_run,
            )
        self.assertFalse(any("generate-coverage-backlog.py" in argv for argv in calls))
        self.assertFalse(any("refresh-release-ledger-evidence.py" in argv for argv in calls))

    def test_wrong_cli_revision_blocks_before_any_generator(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv == ("git", "rev-parse", "HEAD"):
                return subprocess.CompletedProcess(list(argv), 0, "0" * 40, "")
            return self.result(argv, cwd)

        with self.assertRaisesRegex(refresh.SourceRefreshError, "reviewed immutable revision"):
            refresh.run_source_refresh(
                repository_root=self.root,
                datapan_cli=self.cli,
                registry=self.root / "data/data-go-kr.registry.json",
                verification=self.root / "reports/latest-verification.json",
                run=fake_run,
            )
        self.assertFalse(any(argv[:2] == ("go", "run") for argv in calls))

    def test_dirty_cli_checkout_blocks_before_any_generator(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv == ("git", "status", "--porcelain", "--untracked-files=all"):
                return self.result(argv, cwd, stdout_override="?? injected.go\n")
            return self.result(argv, cwd)

        with self.assertRaisesRegex(refresh.SourceRefreshError, "dirty"):
            refresh.run_source_refresh(
                repository_root=self.root,
                datapan_cli=self.cli,
                registry=self.root / "data/data-go-kr.registry.json",
                verification=self.root / "reports/latest-verification.json",
                run=fake_run,
            )
        self.assertFalse(any(argv[:2] == ("go", "run") for argv in calls))

    def test_cli_tree_mutation_during_native_generation_blocks(self) -> None:
        native_ran = False

        def changing_run(argv: tuple[str, ...], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
            nonlocal native_ran
            result = self.fake_run(argv, cwd)
            if argv[:3] == ("go", "run", "./cmd/datapan"):
                native_ran = True
            if argv == ("git", "status", "--porcelain", "--untracked-files=all") and native_ran:
                return self.result(argv, cwd, stdout_override=" M go.mod\n")
            return result

        with self.assertRaisesRegex(refresh.SourceRefreshError, "became dirty"):
            refresh.run_source_refresh(
                repository_root=self.root,
                datapan_cli=self.cli,
                registry=self.root / "data/data-go-kr.registry.json",
                verification=self.root / "reports/latest-verification.json",
                run=changing_run,
            )

    def test_verification_input_is_immutable_for_each_native_command(self) -> None:
        def changing_run(argv: tuple[str, ...], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
            result = self.fake_run(argv, cwd)
            if argv[:3] == ("go", "run", "./cmd/datapan"):
                (self.root / "reports/latest-verification.json").write_text('{"changed":true}\n', encoding="utf-8")
            return result

        with self.assertRaisesRegex(refresh.SourceRefreshError, "latest-verification input changed"):
            refresh.run_source_refresh(
                repository_root=self.root,
                datapan_cli=self.cli,
                registry=self.root / "data/data-go-kr.registry.json",
                verification=self.root / "reports/latest-verification.json",
                run=changing_run,
            )

    def test_previous_registry_baseline_is_immutable_for_each_native_command(self) -> None:
        previous = self.root / ".datapan/previous/data-go-kr.registry.json"

        def changing_run(argv: tuple[str, ...], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
            result = self.fake_run(argv, cwd)
            if argv[:3] == ("go", "run", "./cmd/datapan"):
                previous.write_text("[{}]\n", encoding="utf-8")
            return result

        with self.assertRaisesRegex(refresh.SourceRefreshError, "previous registry baseline changed"):
            refresh.run_source_refresh(
                repository_root=self.root,
                datapan_cli=self.cli,
                registry=self.root / "data/data-go-kr.registry.json",
                verification=self.root / "reports/latest-verification.json",
                previous_registry=previous,
                run=changing_run,
            )

    def test_lfs_placeholder_is_rejected_before_cli_runs(self) -> None:
        registry = self.root / "data/data-go-kr.registry.json"
        registry.write_text("version https://git-lfs.github.com/spec/v1\n", encoding="utf-8")
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return self.fake_run(argv, cwd)

        with self.assertRaisesRegex(refresh.SourceRefreshError, "unmaterialized Git LFS pointer"):
            refresh.run_source_refresh(
                repository_root=self.root,
                datapan_cli=self.cli,
                registry=registry,
                verification=self.root / "reports/latest-verification.json",
                run=fake_run,
            )
        self.assertFalse(any(argv[:2] == ("go", "run") for argv in calls))

    def test_ledger_runs_only_after_successful_source_refresh(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return self.fake_run(argv, cwd)

        refresh.run_source_refresh(
            repository_root=self.root,
            datapan_cli=self.cli,
            registry=self.root / "data/data-go-kr.registry.json",
            verification=self.root / "reports/latest-verification.json",
            run=fake_run,
        )
        refresh.run_ledger_refresh(self.root, fake_run)
        self.assertEqual(calls[-1], ("python3", "scripts/refresh-release-ledger-evidence.py", "--write"))

        failed: list[tuple[str, ...]] = []

        def fail_source(argv: tuple[str, ...], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
            failed.append(argv)
            if any("generate-current-runtime-evidence-projection.py" in arg for arg in argv):
                return self.result(argv, cwd, returncode=1)
            return self.fake_run(argv, cwd)

        with self.assertRaises(refresh.SourceRefreshError):
            refresh.run_source_refresh(
                repository_root=self.root,
                datapan_cli=self.cli,
                registry=self.root / "data/data-go-kr.registry.json",
                verification=self.root / "reports/latest-verification.json",
                run=fail_source,
            )
            refresh.run_ledger_refresh(self.root, fail_source)
        self.assertFalse(any("refresh-release-ledger-evidence.py" in argv for argv in failed))


if __name__ == "__main__":
    unittest.main()
