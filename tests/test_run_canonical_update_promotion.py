from __future__ import annotations

import importlib.util
import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/run-canonical-update-promotion.py"
SPEC = importlib.util.spec_from_file_location("run_canonical_update_promotion_test_module", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class CandidateStagingTests(unittest.TestCase):
    def git(self, root: pathlib.Path, *args: str) -> str:
        return subprocess.run(("git", *args), cwd=root, check=True, text=True, capture_output=True).stdout

    def repository(self, root: pathlib.Path) -> None:
        self.git(root, "init", "-q")
        self.git(root, "config", "user.name", "Test")
        self.git(root, "config", "user.email", "test@example.invalid")
        (root / ".gitignore").write_text(".datapan/\n", encoding="utf-8")
        (root / "data").mkdir()
        (root / "data/data-go-kr.registry.json").write_text("old\n", encoding="utf-8")
        (root / "manifest.json").write_text("{}\n", encoding="utf-8")
        (root / "reports").mkdir()
        (root / "reports/catalog-audit.json").write_text("{}\n", encoding="utf-8")
        self.git(root, "add", ".gitignore", "data/data-go-kr.registry.json", "manifest.json", "reports/catalog-audit.json")
        self.git(root, "commit", "-qm", "baseline")

    def candidate_changes(self, root: pathlib.Path) -> None:
        (root / "data/data-go-kr.registry.json").write_text("candidate\n", encoding="utf-8")
        (root / "manifest.json").write_text('{"candidate":true}\n', encoding="utf-8")
        (root / "reports/catalog-audit.json").write_text('{"candidate":true}\n', encoding="utf-8")
        (root / ".datapan").mkdir()
        (root / ".datapan/local-cache.json").write_text("ignored\n", encoding="utf-8")

    def test_stages_only_explicit_generated_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            self.repository(root)
            self.candidate_changes(root)
            paths = RUNNER.stage_candidate_outputs(root, "generation-a")
            self.assertEqual(set(paths), {"data/data-go-kr.registry.json", "manifest.json", "reports/catalog-audit.json"})
            staged = set(self.git(root, "diff", "--cached", "--name-only").splitlines())
            self.assertEqual(staged, set(paths))
            self.assertNotIn(".datapan/local-cache.json", staged)

    def test_unexpected_cache_file_blocks_before_any_staging(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            self.repository(root)
            self.candidate_changes(root)
            cache = root / "scripts/__pycache__/unrelated.cpython-312.pyc"
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"cache")
            with self.assertRaisesRegex(RUNNER.PromotionError, "outside its explicit output allowlist"):
                RUNNER.stage_candidate_outputs(root, "generation-a")
            self.assertEqual(self.git(root, "diff", "--cached", "--name-only"), "")


class IdleProcessorResultTests(unittest.TestCase):
    def result_dir(self, raw: str, result: dict) -> pathlib.Path:
        directory = pathlib.Path(raw)
        (directory / "upstream-catalogue-processing-result.json").write_text(json.dumps(result), encoding="utf-8")
        return directory

    def test_exact_producer_replay_is_a_verified_no_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = {
                "status": "idle", "reason": "exact_producer_delivery_replay",
                "processing_replay": True, "candidate_available": False,
                "source_id": "data_go_kr", "producer_run_id": "36646768289",
                "processor_run_id": "70000000001-2", "processor_artifact_run_id": "70000000001",
            }
            outcome = RUNNER.validate_no_candidate_processor_result(
                self.result_dir(raw, result), repository="StatPan/datapan-registry",
                workflow_run_id="70000000001", workflow_run_attempt="2",
            )
            self.assertEqual(outcome["status"], "no-candidate")
            self.assertEqual(outcome["reason"], "exact_producer_delivery_replay")
            self.assertFalse(outcome["candidate_available"])

    def test_replay_from_another_processor_attempt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = {
                "status": "idle", "reason": "exact_producer_delivery_replay",
                "processing_replay": True, "candidate_available": False,
                "source_id": "data_go_kr", "producer_run_id": "36646768289",
                "processor_run_id": "70000000001-1", "processor_artifact_run_id": "70000000001",
            }
            with self.assertRaisesRegex(RUNNER.PromotionError, "exact processor/source identity"):
                RUNNER.validate_no_candidate_processor_result(
                    self.result_dir(raw, result), repository="StatPan/datapan-registry",
                    workflow_run_id="70000000001", workflow_run_attempt="2",
                )


    def test_idle_artifact_with_unbound_files_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = {"status": "idle", "reason": "no_active_generation"}
            directory = self.result_dir(raw, result)
            (directory / "composed-candidate.registry.json").write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(RUNNER.PromotionError, "only file"):
                RUNNER.validate_no_candidate_processor_result(
                    directory, repository="StatPan/datapan-registry",
                    workflow_run_id="70000000001", workflow_run_attempt="1",
                )


class PublicationReconciliationTests(unittest.TestCase):
    def test_publication_can_resolve_a_merge_before_hourly_pr_journal_reconciliation(self) -> None:
        # The durable candidate still records the prepared PR/head, while the
        # immutable #592 receipt already names the squash merge commit.
        candidate = {
            "status": "pending-review",
            "candidate": {"head_sha": "a" * 40, "manifest_sha256": "c" * 64},
            "pr": {"number": 42, "state": "open", "merge_commit_sha": None},
        }
        merged_pr = {
            "number": 42,
            "state": "MERGED",
            "headRefOid": "a" * 40,
            "mergeCommit": {"oid": "b" * 40},
        }
        observed_calls = []

        def readback(number: int) -> dict:
            observed_calls.append(number)
            return merged_pr

        selected, observed = RUNNER.select_publication_candidate(
            [candidate], source_sha="b" * 40, readback=readback,
        )
        self.assertIsNot(selected, candidate)
        self.assertEqual(selected, candidate)
        self.assertEqual(observed, merged_pr)
        self.assertEqual(observed_calls, [42])

    def test_publication_requires_one_exact_live_merged_pr_readback(self) -> None:
        candidate = {
            "candidate": {"head_sha": "a" * 40, "manifest_sha256": "c" * 64},
            "pr": {"number": 42},
        }
        open_pr = {"number": 42, "state": "OPEN", "mergeCommit": None}
        with self.assertRaisesRegex(RUNNER.PromotionError, "exactly one merged canonical candidate"):
            RUNNER.select_publication_candidate(
                [candidate], source_sha="b" * 40, readback=lambda _number: open_pr,
            )


class ProcessorBundleContractTests(unittest.TestCase):
    def test_ready_bundle_requires_eight_bound_outputs_in_frozen_order(self) -> None:
        expected = [
            "composed-candidate.registry.json",
            "ready-scope.registry.json",
            "semantic-diff.json",
            "regeneration-queue.json",
            "quarantine.json",
            "composition-receipt.json",
            "upstream-catalogue-enrichment-evidence.json",
            "upstream-catalogue-processing-result.json",
        ]
        self.assertEqual(list(RUNNER.REQUIRED_PROCESSOR_FILES), expected)
        checkpoint = {
            "status": "ready",
            "output_digests": [{"path": name, "sha256": "0" * 64, "bytes": 1} for name in expected[:-1]],
            "output_artifact": {"bundle_manifest_sha256": "0" * 64},
        }
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(RUNNER.PromotionError, "exact ordered eight-file contract"):
                RUNNER.validate_processor_bundle(checkpoint, pathlib.Path(raw), {}, None)

        checkpoint["output_digests"] = [
            {"path": name, "sha256": "0" * 64, "bytes": 1} for name in expected
        ]
        checkpoint["output_digests"][0], checkpoint["output_digests"][-1] = (
            checkpoint["output_digests"][-1], checkpoint["output_digests"][0],
        )
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(RUNNER.PromotionError, "exact ordered eight-file contract"):
                RUNNER.validate_processor_bundle(checkpoint, pathlib.Path(raw), {}, None)

    def test_result_only_quarantine_is_verified_as_no_candidate(self) -> None:
        generation = "f" * 64
        processor_run_id = "70000000001-1"
        processor_artifact_run_id = "70000000001"
        result = {
            "status": "quarantined", "reason": "input_expired", "generation_id": generation,
            "source_id": "data_go_kr", "producer_run_id": "36646768289",
            "processor_run_id": processor_run_id, "processor_artifact_run_id": processor_artifact_run_id,
            "processing_replay": False, "candidate_available": False,
        }
        with tempfile.TemporaryDirectory() as raw:
            bundle = pathlib.Path(raw)
            result_path = bundle / "upstream-catalogue-processing-result.json"
            result_path.write_bytes(RUNNER.canonical_json(result))
            digests = [{"path": result_path.name, "sha256": RUNNER.file_sha256(result_path), "bytes": result_path.stat().st_size}]
            locator = {
                "repository": "StatPan/datapan-registry", "run_id": processor_artifact_run_id,
                "name": f"upstream-catalogue-processing-{processor_run_id}", "artifact_id": "123456",
                "expires_at": "2026-10-31T00:00:00Z",
                "bundle_manifest_sha256": hashlib.sha256(RUNNER.canonical_json(digests)).hexdigest(),
            }
            checkpoint = {
                "generation_id": generation, "status": "quarantined", "source_id": "data_go_kr",
                "source_scope": "aggregate_supported_catalog", "output_digests": digests,
                "output_artifact": locator, "last_observation": {"producer_run_id": "36646768289"},
                "outcome": {"reason": "input_expired"},
            }
            uploaded_copy = dict(checkpoint)
            uploaded_copy["output_artifact"] = {**locator, "artifact_id": None}
            uploaded_copy.pop("checkpoint_sha256", None)
            uploaded_copy["checkpoint_sha256"] = hashlib.sha256(RUNNER.canonical_json(uploaded_copy)).hexdigest()
            (bundle / "upstream-catalogue-checkpoint-receipt.json").write_text(json.dumps(uploaded_copy), encoding="utf-8")
            outcome = RUNNER.validate_processor_bundle(checkpoint, bundle, {}, None)
            self.assertEqual(outcome["status"], "quarantined")
            self.assertEqual(outcome["reason"], "input_expired")

if __name__ == "__main__":
    unittest.main()
