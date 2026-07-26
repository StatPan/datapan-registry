from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_admission_receipts as admission  # noqa: E402


class ReleaseAdmissionReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "registry.json").write_text('{"registry":true}\n', encoding="utf-8")
        artifact = self.root / "data" / "registry.json"
        manifest = {
            "schema_version": "datapan.release-manifest.v1",
            "generated_at": "2026-07-22T00:00:00Z",
            "datapan_version": "0.1.0-test",
            "provider": "data.go.kr",
            "source_registry": "data/registry.json",
            "artifact_count": 2,
            "artifacts": [
                {"path": "data/registry.json", "sha256": admission.file_digest(artifact)},
                {"path": "reports/health-runtime-observation-plan.v1.json", "kind": "verification_plan", "schema": "https://schemas.datapan.dev/datapan.health-runtime-observation-plan.v1.schema.json", "bytes": 0, "sha256": "0" * 64},
            ],
        }
        self.manifest_path = self.root / "manifest.json"
        self.plan_path = self.root / "reports" / "health-runtime-observation-plan.v1.json"
        self.plan_path.parent.mkdir(parents=True)
        projection = copy.deepcopy(manifest)
        projection["artifacts"][1].pop("bytes")
        projection["artifacts"][1].pop("sha256")
        self.plan_binding = hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self.plan_path.write_text(json.dumps({"manifest_binding": {"sha256": self.plan_binding}}), encoding="utf-8")
        manifest["artifacts"][1]["bytes"] = self.plan_path.stat().st_size
        manifest["artifacts"][1]["sha256"] = admission.file_digest(self.plan_path)
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.schema = admission.load_json(ROOT / "schemas/datapan.release-receipt-admission.v1.schema.json")
        self.policy = admission.load_json(ROOT / "policy/release-receipt-admission.json")
        self.policy_path = ROOT / "policy/release-receipt-admission.json"
        _, self.manifest_digest, self.source_digest = admission.validate_manifest(self.manifest_path, check_artifacts=True)
        self.artifact_roots: dict[str, pathlib.Path] = {}
        for kind, contract in self.policy["producer_contracts"].items():
            root = self.artifact_roots.setdefault(contract["repository"], self.root / "producer-artifacts" / contract["repository"].split("/")[1])
            receipt_path = root / "receipts" / f"{kind}.json"
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(json.dumps({"producer_fixture": kind}) + "\n", encoding="utf-8")
        self.aggregate_path = self.artifact_roots["StatPan/datapan-health"] / "runs" / "fixture-run-42.json"
        self.aggregate_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def receipt(self, kind: str, *, shard: int | None = None) -> dict:
        contract = self.policy["producer_contracts"][kind]
        value = {
            "schema_version": admission.SCHEMA_VERSION,
            "generated_at": "2026-07-22T00:00:00Z",
            "receipt_kind": kind,
            "producer": {"repository": contract["repository"], "revision": "a" * 40, "receipt_path": f"receipts/{kind}.json", "receipt_sha256": admission.file_digest(self.artifact_roots[contract["repository"]] / "receipts" / f"{kind}.json")},
            "registry": {"manifest_path": self.manifest_path.as_posix(), "manifest_sha256": self.manifest_digest, "source_path": "data/registry.json", "source_sha256": self.source_digest, "policy_path": self.policy_path.as_posix(), "policy_sha256": admission.file_digest(self.policy_path)},
            "outcome": contract["outcomes"][0],
            "scope": {"provider": contract.get("provider", "data.go.kr"), "subject": contract["subject"]},
            "redaction": {"secret_values_present": False, "secret_hashes_present": False, "request_urls_present": False, "response_bodies_present": False, "forbidden_fields_checked": ["credential_value", "credential_hash", "authorization_header", "service_key", "api_key"]},
        }
        if kind in {admission.RUNTIME_KIND, admission.LIVE_KIND}:
            value["outcome"] = "verified"
            if kind == admission.RUNTIME_KIND:
                value["execution"] = {"run_id": "run-42", "shard_count": 8, "shard_index": shard, "shard_digest": f"{shard:064x}", "batch_size": 100, "max_parallelism": 2, "per_operation_timeout_seconds": 20, "terminal_state": "verified"}
            else:
                value["live_execution"] = {"run_id": "run-42", "shard_count": 8, "batch_size": 100, "max_parallelism": 2, "per_operation_timeout_seconds": 20, "terminal_state": "verified"}
            value["execution_plan"] = {"path": "reports/health-runtime-observation-plan.v1.json", "sha256": admission.file_digest(self.plan_path), "manifest_binding_sha256": self.plan_binding}
            value["producer"]["aggregate_path"] = "runs/fixture-run-42.json"
            value["producer"]["aggregate_sha256"] = "0" * 64
            source_redaction = {"secret_values_removed": True, "secret_hashes_removed": True, "authorization_headers_removed": True, "credential_bearing_urls_removed": True, "raw_provider_text_removed": True, "raw_provider_urls_removed": True, "response_bodies_removed": True, "response_rows_removed": True, "user_identity_removed": True}
            aggregate_shards = [
                {
                    "index": index,
                    "receipt_available": True,
                    "completed": True,
                    "manifest_sha256": value["registry"]["manifest_sha256"],
                    "policy_sha256": value["registry"]["policy_sha256"],
                    "scope": admission.RUNTIME_SCOPE,
                    "terminal_state": "verified",
                    "observed_at": "2026-07-22T00:00:00Z",
                    "redaction": source_redaction,
                }
                for index in range(8)
            ]
            if kind == admission.RUNTIME_KIND:
                aggregate_shards[shard].update({"receipt_path": value["producer"]["receipt_path"], "receipt_sha256": value["producer"]["receipt_sha256"], "shard_digest": value["execution"]["shard_digest"]})
            aggregate = {"schema_version": "datapan.health-bounded-observation-run.v1", "producer": {"repository": "StatPan/datapan-health", "revision": "a" * 40}, "registry": {"manifest_sha256": value["registry"]["manifest_sha256"], "source_sha256": value["registry"]["source_sha256"], "policy_sha256": value["registry"]["policy_sha256"]}, "run": {"run_id": "run-42", "started_at": "2026-07-21T23:59:00Z", "completed_at": "2026-07-22T00:00:00Z", "shard_count": 8, "batch_size": 100, "max_parallel": 2, "timeout_ms": 20000}, "shards": aggregate_shards, "aggregate": {"terminal_state": "verified", "completeness": "complete", "timed_out": False}, "redaction": source_redaction}
            self.aggregate_path.write_text(json.dumps(aggregate), encoding="utf-8")
            value["producer"]["aggregate_sha256"] = admission.file_digest(self.aggregate_path)
            if kind == admission.LIVE_KIND:
                value["producer"]["receipt_path"] = value["producer"]["aggregate_path"]
                value["producer"]["receipt_sha256"] = value["producer"]["aggregate_sha256"]
        value["receipt_digest"] = admission.canonical_digest(value)
        return value

    def validate(self, receipt: dict, *, admission_time: str = "2026-07-22T00:05:00Z") -> None:
        admission.validate_receipt(receipt, schema=self.schema, policy=self.policy, policy_path=self.policy_path, manifest_path=self.manifest_path, manifest_sha256=self.manifest_digest, source_sha256=self.source_digest, artifact_roots=self.artifact_roots, admitted_at=admission.parse_time(admission_time, "test admission time"), label="fixture")

    def test_all_producer_kinds_are_admitted_with_immutable_bindings(self) -> None:
        for kind in self.policy["producer_contracts"]:
            self.validate(self.receipt(kind, shard=0 if kind == admission.RUNTIME_KIND else None))

    def test_checked_in_fixtures_follow_documented_local_admission_command(self) -> None:
        fixture_root = pathlib.Path("tests/fixtures/release-admission")
        result = subprocess.run(
            [
                sys.executable,
                "scripts/validate-release-admission-receipts.py",
                "--manifest",
                str(fixture_root / "manifest.json"),
                "--check-manifest-artifacts",
                "--admission-time",
                "2026-07-22T01:00:00Z",
                "--producer-artifact-root",
                f"StatPan/datapan-data={fixture_root / 'producer-artifacts' / 'datapan-data'}",
                "--producer-artifact-root",
                f"StatPan/datapan-health={fixture_root / 'producer-artifacts' / 'datapan-health'}",
                "--producer-artifact-root",
                f"StatPan/datapan-cli={fixture_root / 'producer-artifacts' / 'datapan-cli'}",
                str(fixture_root / "catalog-observation.json"),
                str(fixture_root / "cli-consumer-smoke.json"),
                str(fixture_root / "runtime-freshness-shard-0.json"),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("receipts=3", result.stdout)

    def test_secret_bearing_receipt_is_rejected(self) -> None:
        receipt = self.receipt("catalog_observation")
        receipt["scope"]["provider"] = "data.go.kr Bearer abcdefghijklmnopqrstuvwxyz"
        receipt["receipt_digest"] = admission.canonical_digest(receipt)
        with self.assertRaisesRegex(ValueError, "secret"):
            self.validate(receipt)

    def test_unsafe_url_raw_body_and_identity_are_rejected_without_echo(self) -> None:
        cases = [
            ("scope.provider", "https://provider.example/?serviceKey=super-secret-token"),
            ("scope.subject", "operator@example.com"),
            ("response_body", "super-secret-response-body"),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                receipt = self.receipt("catalog_observation")
                if field.startswith("scope."):
                    receipt["scope"][field.split(".")[1]] = value
                else:
                    receipt[field] = value
                receipt["receipt_digest"] = admission.canonical_digest(receipt)
                with self.assertRaises(ValueError) as caught:
                    self.validate(receipt)
                self.assertNotIn(value, str(caught.exception))

    def test_canonical_digest_excludes_its_own_field_but_detects_payload_tampering(self) -> None:
        receipt = self.receipt("catalog_observation")
        digest = receipt["receipt_digest"]
        self.assertEqual(digest, admission.canonical_digest(receipt))
        receipt["outcome"] = "material_change"
        with self.assertRaisesRegex(ValueError, "receipt_digest"):
            self.validate(receipt)

    def test_stale_and_future_receipts_fail_at_the_explicit_admission_time(self) -> None:
        for generated_at, reason in (("2026-07-20T00:00:00Z", "stale"), ("2026-07-23T00:00:00Z", "future")):
            with self.subTest(generated_at=generated_at):
                receipt = self.receipt("catalog_observation")
                receipt["generated_at"] = generated_at
                receipt["receipt_digest"] = admission.canonical_digest(receipt)
                with self.assertRaisesRegex(ValueError, reason):
                    self.validate(receipt)

    def test_malformed_timestamp_fails_schema_format_validation(self) -> None:
        receipt = self.receipt("catalog_observation")
        receipt["generated_at"] = "not-a-time"
        receipt["receipt_digest"] = admission.canonical_digest(receipt)
        with self.assertRaisesRegex(ValueError, "schema validation format"):
            self.validate(receipt)

    def test_manifest_paths_reject_traversal_and_symlink_escape(self) -> None:
        outside = self.root.parent / "outside-registry.json"
        outside.write_text('{"outside":true}\n', encoding="utf-8")
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["source_registry"] = "../outside-registry.json"
        manifest["artifacts"] = [{"path": "../outside-registry.json", "sha256": admission.file_digest(outside)}]
        manifest["artifact_count"] = 1
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "escapes"):
            admission.validate_manifest(self.manifest_path, check_artifacts=True)
        manifest["source_registry"] = "data/registry.json"
        manifest["artifacts"] = [{"path": "data/registry.json", "sha256": admission.file_digest(self.root / "data" / "registry.json")}]
        link = self.root / "data" / "escaped-link.json"
        link.symlink_to(outside)
        manifest["source_registry"] = "data/escaped-link.json"
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "escapes"):
            admission.validate_manifest(self.manifest_path, check_artifacts=False)

    def test_producer_receipt_digest_and_path_are_verified_against_artifact_root(self) -> None:
        receipt = self.receipt("catalog_observation")
        receipt["producer"]["receipt_sha256"] = "0" * 64
        receipt["receipt_digest"] = admission.canonical_digest(receipt)
        with self.assertRaisesRegex(ValueError, "producer receipt digest"):
            self.validate(receipt)
        receipt = self.receipt("catalog_observation")
        receipt["producer"]["receipt_path"] = "../outside.json"
        receipt["receipt_digest"] = admission.canonical_digest(receipt)
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.validate(receipt)

    def test_manifest_digest_mismatch_is_rejected(self) -> None:
        receipt = self.receipt("cli_consumer_smoke")
        receipt["registry"]["manifest_sha256"] = "0" * 64
        receipt["receipt_digest"] = admission.canonical_digest(receipt)
        with self.assertRaisesRegex(ValueError, "manifest binding"):
            self.validate(receipt)

    def test_source_path_mismatch_is_rejected(self) -> None:
        receipt = self.receipt("catalog_observation")
        receipt["registry"]["source_path"] = "data/other-registry.json"
        receipt["receipt_digest"] = admission.canonical_digest(receipt)
        with self.assertRaisesRegex(ValueError, "source path"):
            self.validate(receipt)

    def test_runtime_receipts_require_all_eight_unique_shards(self) -> None:
        receipts = [self.receipt(admission.RUNTIME_KIND, shard=index) for index in range(8)]
        self.assertEqual(admission.validate_runtime_completeness(receipts), "run-42")
        with self.assertRaisesRegex(ValueError, "indexes"):
            admission.validate_runtime_completeness(receipts[:-1] + [self.receipt(admission.RUNTIME_KIND, shard=6)])
        receipts = [self.receipt(admission.RUNTIME_KIND, shard=index) for index in range(8)]
        receipts[-1]["producer"]["revision"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "producer repository and revision"):
            admission.validate_runtime_completeness(receipts)
        receipts = [self.receipt(admission.RUNTIME_KIND, shard=index) for index in range(8)]
        receipts[-1]["registry"]["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Registry manifest/source binding"):
            admission.validate_runtime_completeness(receipts)
        receipts = [self.receipt(admission.RUNTIME_KIND, shard=index) for index in range(8)]
        receipts[-1]["scope"]["provider"] = "other-provider"
        with self.assertRaisesRegex(ValueError, "runtime scope"):
            admission.validate_runtime_completeness(receipts)

    def test_partial_or_byte_drifted_health_aggregate_cannot_admit_an_outer_shard(self) -> None:
        receipt = self.receipt(admission.RUNTIME_KIND, shard=0)
        aggregate = json.loads(self.aggregate_path.read_text(encoding="utf-8"))
        aggregate["shards"][3]["receipt_available"] = False
        self.aggregate_path.write_text(json.dumps(aggregate), encoding="utf-8")
        receipt["producer"]["aggregate_sha256"] = admission.file_digest(self.aggregate_path)
        receipt["receipt_digest"] = admission.canonical_digest(receipt)
        with self.assertRaisesRegex(ValueError, "unavailable or incomplete"):
            self.validate(receipt)

        receipt = self.receipt(admission.RUNTIME_KIND, shard=0)
        receipt["producer"]["aggregate_sha256"] = "0" * 64
        receipt["receipt_digest"] = admission.canonical_digest(receipt)
        with self.assertRaisesRegex(ValueError, "aggregate digest"):
            self.validate(receipt)

    def test_health_canary_is_not_a_runtime_freshness_scope(self) -> None:
        receipt = self.receipt(admission.RUNTIME_KIND, shard=0)
        receipt["scope"]["subject"] = "health_probe_canary"
        receipt["receipt_digest"] = admission.canonical_digest(receipt)
        with self.assertRaisesRegex(ValueError, "scope.subject"):
            self.validate(receipt)
        receipt = self.receipt(admission.RUNTIME_KIND, shard=0)
        receipt["scope"]["provider"] = "data.go.kr"
        receipt["receipt_digest"] = admission.canonical_digest(receipt)
        with self.assertRaisesRegex(ValueError, "scope.provider"):
            self.validate(receipt)

    def test_runtime_plan_digest_and_manifest_binding_fail_closed(self) -> None:
        receipt = self.receipt(admission.RUNTIME_KIND, shard=0)
        receipt["execution_plan"]["sha256"] = "0" * 64
        receipt["receipt_digest"] = admission.canonical_digest(receipt)
        with self.assertRaisesRegex(ValueError, "execution plan digest"):
            self.validate(receipt)
        receipt = self.receipt(admission.RUNTIME_KIND, shard=0)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["provider"] = "other"
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "manifest binding"):
            self.validate(receipt)

    def test_live_release_is_a_distinct_600_second_complete_aggregate_gate(self) -> None:
        live = self.receipt(admission.LIVE_KIND)
        self.validate(live, admission_time="2026-07-22T00:09:59Z")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.validate(live, admission_time="2026-07-22T00:10:01Z")
        future = self.receipt(admission.LIVE_KIND)
        future["generated_at"] = "2026-07-22T00:05:01Z"
        future["receipt_digest"] = admission.canonical_digest(future)
        with self.assertRaisesRegex(ValueError, "future"):
            self.validate(future, admission_time="2026-07-22T00:05:00Z")
        self.assertEqual(admission.validate_required_live_release([live]), live)
        with self.assertRaisesRegex(ValueError, "health_live_observation"):
            admission.validate_required_live_release([self.receipt(admission.RUNTIME_KIND, shard=0)])

    def test_live_release_rejects_partial_mismatched_or_unsafe_evidence(self) -> None:
        live = self.receipt(admission.LIVE_KIND)
        aggregate = json.loads(self.aggregate_path.read_text(encoding="utf-8"))
        aggregate["shards"][2]["receipt_available"] = False
        self.aggregate_path.write_text(json.dumps(aggregate), encoding="utf-8")
        live["producer"]["aggregate_sha256"] = admission.file_digest(self.aggregate_path)
        live["producer"]["receipt_sha256"] = live["producer"]["aggregate_sha256"]
        live["receipt_digest"] = admission.canonical_digest(live)
        with self.assertRaisesRegex(ValueError, "unavailable or incomplete"):
            self.validate(live)
        live = self.receipt(admission.LIVE_KIND)
        live["producer"]["receipt_path"] = "receipts/health_live_observation.json"
        live["producer"]["receipt_sha256"] = admission.file_digest(self.artifact_roots["StatPan/datapan-health"] / "receipts" / "health_live_observation.json")
        live["receipt_digest"] = admission.canonical_digest(live)
        with self.assertRaisesRegex(ValueError, "complete aggregate"):
            self.validate(live)
        live = self.receipt(admission.LIVE_KIND)
        live["redaction"]["secret_values_present"] = True
        live["receipt_digest"] = admission.canonical_digest(live)
        with self.assertRaisesRegex(ValueError, "schema validation"):
            self.validate(live)

    def test_fresh_outer_envelope_cannot_rewrap_stale_health_aggregate_or_shards(self) -> None:
        live = self.receipt(admission.LIVE_KIND)
        stale_fixture = ROOT / "tests" / "fixtures" / "release-admission" / "producer-artifacts" / "datapan-health" / "runs" / "stale-live-rewrap.json"
        aggregate = json.loads(stale_fixture.read_text(encoding="utf-8"))
        aggregate["registry"].update({"manifest_sha256": live["registry"]["manifest_sha256"], "source_sha256": live["registry"]["source_sha256"], "policy_sha256": live["registry"]["policy_sha256"]})
        for shard in aggregate["shards"]:
            shard["manifest_sha256"] = live["registry"]["manifest_sha256"]
            shard["policy_sha256"] = live["registry"]["policy_sha256"]
        self.aggregate_path.write_text(json.dumps(aggregate), encoding="utf-8")
        fresh_outer = "2026-07-22T00:04:59Z"
        live["generated_at"] = fresh_outer
        live["live_execution"]["run_id"] = aggregate["run"]["run_id"]
        live["producer"]["aggregate_sha256"] = admission.file_digest(self.aggregate_path)
        live["producer"]["receipt_sha256"] = live["producer"]["aggregate_sha256"]
        live["receipt_digest"] = admission.canonical_digest(live)
        with self.assertRaisesRegex(ValueError, "aggregate completion is stale"):
            self.validate(live, admission_time="2026-07-22T00:05:00Z")

    def test_live_release_rejects_future_and_stale_shard_observation_times(self) -> None:
        live = self.receipt(admission.LIVE_KIND)
        aggregate = json.loads(self.aggregate_path.read_text(encoding="utf-8"))
        aggregate["shards"][0]["observed_at"] = "2026-07-22T00:06:00Z"
        self.aggregate_path.write_text(json.dumps(aggregate), encoding="utf-8")
        live["producer"]["aggregate_sha256"] = admission.file_digest(self.aggregate_path)
        live["producer"]["receipt_sha256"] = live["producer"]["aggregate_sha256"]
        live["receipt_digest"] = admission.canonical_digest(live)
        with self.assertRaisesRegex(ValueError, "shard observation is in the future"):
            self.validate(live)
        live = self.receipt(admission.LIVE_KIND)
        aggregate = json.loads(self.aggregate_path.read_text(encoding="utf-8"))
        aggregate["run"]["started_at"] = "2026-07-21T23:40:00Z"
        aggregate["shards"][0]["observed_at"] = "2026-07-21T23:49:59Z"
        self.aggregate_path.write_text(json.dumps(aggregate), encoding="utf-8")
        live["producer"]["aggregate_sha256"] = admission.file_digest(self.aggregate_path)
        live["producer"]["receipt_sha256"] = live["producer"]["aggregate_sha256"]
        live["receipt_digest"] = admission.canonical_digest(live)
        with self.assertRaisesRegex(ValueError, "shard observation is stale"):
            self.validate(live)

    def test_generic_runtime_shard_does_not_gain_live_timestamp_requirement(self) -> None:
        runtime = self.receipt(admission.RUNTIME_KIND, shard=0)
        aggregate = json.loads(self.aggregate_path.read_text(encoding="utf-8"))
        aggregate["run"].pop("started_at")
        aggregate["run"].pop("completed_at")
        for shard in aggregate["shards"]:
            shard.pop("observed_at")
        self.aggregate_path.write_text(json.dumps(aggregate), encoding="utf-8")
        runtime["producer"]["aggregate_sha256"] = admission.file_digest(self.aggregate_path)
        runtime["receipt_digest"] = admission.canonical_digest(runtime)
        self.validate(runtime)


if __name__ == "__main__":
    unittest.main()
