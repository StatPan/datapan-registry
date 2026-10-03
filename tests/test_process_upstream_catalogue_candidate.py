from __future__ import annotations

import copy
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
import jsonschema
from datetime import datetime, timedelta, timezone
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "process-upstream-catalogue-candidate.py"
ACTUAL_COMPOSER = pathlib.Path(os.environ.get(
    "DATAPAN_TEST_COMPOSER", str(ROOT / "scripts/compose-upstream-catalogue-candidate.py"),
))
SPEC = importlib.util.spec_from_file_location("process_upstream_catalogue_candidate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(self, body: bytes = b"", *, content_length: str | None = None, url: str = "https://www.data.go.kr/data/2/openapi.do") -> None:
        self.body = body
        self.headers = {"Content-Length": content_length} if content_length else {}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self.url

    def read(self, size: int = -1) -> bytes:
        return self.body[:size]


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def open(self, _request: urllib.request.Request, timeout: float):
        self.timeout = timeout
        return self.response


class UpstreamCatalogueProcessorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.baseline_path = self.root / "baseline.json"
        self.candidate_path = self.root / "candidate.registry.json"
        self.diff_path = self.root / "diff.json"
        self.evidence_path = self.root / "refresh-evidence.json"
        self.policy_path = self.root / "source-policy.json"
        self.provider_index_path = self.root / "provider-index.json"
        self.state_dir = self.root / "state"
        self.output_dir = self.root / "output"
        self.composer_path = self.root / "fixture-composer.py"
        self.now = "2026-10-01T10:00:00Z"
        self.old_link = {
            "id": "1", "provider": "data.go.kr", "type": "LINK", "title": "Existing detail",
            "operations": [{"name": "old", "endpoint": "https://api.example.gov/old", "source": {"system": "data.go.kr", "url": "https://www.data.go.kr/data/1/openapi.do", "raw": {"operation_url": "https://api.example.gov/old"}}}],
            "source": {"system": "data.go.kr", "url": "https://www.data.go.kr/data/1/openapi.do", "raw": {"type": "LINK", "api_type": "LINK", "title": "Existing detail", "guide_url": "https://www.data.go.kr/guide/1.pdf", "meta_url": "https://www.data.go.kr/data/1/openapi.do", "api_id": "1"}},
        }
        self.new_link = {
            "id": "2", "provider": "data.go.kr", "type": "LINK", "title": "New detail", "operations": [],
            "source": {"system": "data.go.kr", "url": "https://www.data.go.kr/data/2/openapi.do", "raw": {"type": "LINK", "api_type": "LINK", "title": "New detail", "meta_url": "https://www.data.go.kr/data/2/openapi.do", "api_id": "2"}},
        }
        self.baseline_path.write_text(json.dumps([self.old_link]), encoding="utf-8")
        self.candidate_path.write_text(json.dumps([self.old_link, self.new_link]), encoding="utf-8")
        self.diff_path.write_text(json.dumps({"summary": {"added": 1, "removed": 0, "changed": 0}}), encoding="utf-8")
        self.write_observation("2026-10-01T10:00:00Z")
        self.policy_path.write_text(json.dumps({"sources": [{"source_id": "data_go_kr", "canonical_registry": str(self.baseline_path)}]}), encoding="utf-8")
        self.provider_index_path.write_text(json.dumps({"adapters": [{"name": "example", "hosts": ["api.example.gov"]}]}), encoding="utf-8")
        self.composer_path.write_text(self.fake_composer_source(), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_observation(self, observed_at: str, *, succeeded: bool = True) -> None:
        value = {
            "schema_version": "datapan.upstream-refresh-evidence.v1", "source_id": "data_go_kr",
            "observed_at": observed_at, "collection": {"attempted": True, "succeeded": succeeded},
        }
        self.evidence_path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def fake_composer_source() -> str:
        return '''from __future__ import annotations
import argparse, hashlib, json, pathlib, sys
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
p=argparse.ArgumentParser(); p.add_argument("--baseline"); p.add_argument("--candidate"); p.add_argument("--diff"); p.add_argument("--refresh-evidence"); p.add_argument("--provider-index"); p.add_argument("--source-policy"); p.add_argument("--producer-run-id"); p.add_argument("--producer-run-url"); p.add_argument("--output-dir"); p.add_argument("--enrichment-evidence"); a=p.parse_args()
out=pathlib.Path(a.output_dir); out.mkdir(parents=True, exist_ok=True)
enrichment=json.loads(pathlib.Path(a.enrichment_evidence).read_text())
assert set(enrichment) in ({"schema_version", "original_candidate_sha256", "provider_index_sha256", "adapter_revision", "extractor_revision", "records"}, {"schema_version", "original_candidate_sha256", "provider_index_sha256", "adapter_revision", "extractor_revision", "records", "worker_outcomes"})
for row in enrichment["records"]: assert set(row) == {"api_key", "status", "source_sha256", "guide_sha256", "observed_guide_url", "observed_guide_url_sha256", "operations", "operations_sha256", "source_provenance"}
for row in enrichment.get("worker_outcomes", []):
    assert set(row) in ({"api_key", "status", "source_sha256", "guide_sha256"}, {"api_key", "status", "source_sha256", "guide_sha256", "failure_diagnostic"})
    if "failure_diagnostic" in row:
        assert set(row["failure_diagnostic"]) in ({"code"}, {"code", "http_status"})
(out/"composed-candidate.registry.json").write_bytes(pathlib.Path(a.candidate).read_bytes())
(out/"ready-scope.registry.json").write_bytes(pathlib.Path(a.candidate).read_bytes())
(out/"semantic-diff.json").write_text(json.dumps({"summary":{"added":1,"removed":0,"changed":0}}))
(out/"regeneration-queue.json").write_text(json.dumps({"items":[]}))
(out/"quarantine.json").write_text(json.dumps({"items":[]}))
outputs={name:digest(out/name) for name in ("composed-candidate.registry.json","ready-scope.registry.json","semantic-diff.json","regeneration-queue.json","quarantine.json")}
enriched_ids={row["api_key"]["id"] for row in enrichment["records"]}
candidate=json.loads(pathlib.Path(a.candidate).read_text())
baseline=json.loads(pathlib.Path(a.baseline).read_text())
baseline_ids={row["id"] for row in baseline}
unresolved=[{"provider":"data.go.kr","id":row["id"]} for row in candidate if row.get("type") == "LINK" and row["id"] not in enriched_ids and row["id"] not in baseline_ids]
status=__import__("os").environ.get("TEST_COMPOSER_STATUS", "no_safe_change" if unresolved else "ready_scoped")
quarantined=[] if __import__("os").environ.get("TEST_OMIT_WORKER_OUTCOMES") == "1" else unresolved
receipt={"status":status,"inputs":{"baseline_sha256":digest(pathlib.Path(a.baseline)),"candidate_sha256":digest(pathlib.Path(a.candidate))},"outputs":outputs,"scope":{"full_scope_fresh":False,"publication_allowed":False,"global_counts":{"before":1,"after":2},"applied_api_keys":[],"retained_pending_api_keys":[],"quarantined_api_keys":quarantined}}
(out/"composition-receipt.json").write_text(json.dumps(receipt))
'''

    def args(self, *, run_id: str = "101", **overrides):
        values = {
            "--baseline": self.baseline_path,
            "--candidate": self.candidate_path,
            "--diff": self.diff_path,
            "--refresh-evidence": self.evidence_path,
            "--source-policy": self.policy_path,
            "--provider-index": self.provider_index_path,
            "--state-dir": self.state_dir,
            "--output-dir": self.output_dir,
            "--checkpoint-schema": ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json",
            "--fixture-composer": self.composer_path,
            "--allow-fixture-composer": None,
            "--producer-run-id": run_id,
            "--processor-run-id": run_id,
            "--repository": "StatPan/datapan-registry",
            "--execution-mode": "fixture",
            "--producer-run-url": f"https://github.com/StatPan/datapan-registry/actions/runs/{run_id}",
            "--now": self.now,
        }
        for key, value in overrides.items():
            values[key] = value
        argv = []
        for flag, value in values.items():
            argv.append(flag)
            if value is not None:
                argv.append(str(value))
        return MODULE.build_parser().parse_args(argv)

    def successful_fetch(self, url: str, timeout: float) -> str:
        self.assertRegex(url, r"^https://www\.data\.go\.kr/data/[0-9]+/openapi\.do$")
        self.assertLessEqual(timeout, 30)
        return '<a href="https://api.example.gov/detail" onclick="fn_LinkApiRequest()">API</a>'

    def invoke(self, *, run_id: str = "101", fetcher=None, **overrides):
        return MODULE.process(self.args(run_id=run_id, **overrides), fetcher=fetcher or self.successful_fetch, sleeper=lambda _delay: None)

    def checkpoint_path(self, checkpoint: dict) -> pathlib.Path:
        return self.state_dir / "sources/data_go_kr/generations" / f"{checkpoint['generation_id']}.json"

    def test_ready_scoped_candidate_has_exact_composer_evidence_and_durable_artifact_digests(self) -> None:
        code, checkpoint = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(checkpoint["status"], "ready")
        result = MODULE.processing_result(
            checkpoint, producer_run_id="101", processor_run_id="101", processor_artifact_run_id="101",
        )
        self.assertTrue(result["candidate_available"])
        self.assertFalse(result["processing_replay"])
        self.assertEqual(result["source_id"], "data_go_kr")
        self.assertEqual(result["producer_run_id"], "101")
        self.assertEqual(result["processor_run_id"], "101")
        result_path = self.output_dir / "upstream-catalogue-processing-result.json"
        self.assertEqual(json.loads(result_path.read_text()), result)
        result_digest_entry = next(entry for entry in checkpoint["output_digests"] if entry["path"] == result_path.name)
        self.assertEqual(result_digest_entry["sha256"], MODULE.file_sha256(result_path))
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual(set(evidence), {"schema_version", "original_candidate_sha256", "provider_index_sha256", "adapter_revision", "extractor_revision", "records", "worker_outcomes"})
        self.assertEqual([row["status"] for row in evidence["records"]], ["enriched"])
        self.assertEqual(evidence["worker_outcomes"], [])
        for row in evidence["records"]:
            self.assertEqual(set(row), {"api_key", "status", "source_sha256", "guide_sha256", "observed_guide_url", "observed_guide_url_sha256", "operations", "operations_sha256", "source_provenance"})
            expected_url = "https://www.data.go.kr/data/" + row["api_key"]["id"] + "/openapi.do"
            self.assertEqual(set(row["source_provenance"]), {"system", "page_url", "effective_url", "page_sha256", "observed_at"})
            self.assertEqual(row["source_provenance"]["page_url"], expected_url)
            self.assertEqual(row["source_provenance"]["effective_url"], expected_url)
            self.assertRegex(row["source_provenance"]["page_sha256"], r"^[a-f0-9]{64}$")
            self.assertIsNone(row["observed_guide_url"])
            self.assertIsNone(row["observed_guide_url_sha256"])
            self.assertIsNone(row["guide_sha256"])
            self.assertEqual(row["operations_sha256"], MODULE.sha256_bytes(MODULE.canonical_json(row["operations"])))
        self.assertEqual(evidence["records"][0]["operations"][0]["source"]["url"], "https://www.data.go.kr/data/2/openapi.do")
        self.assertEqual(checkpoint["output_artifact"]["name"], "upstream-catalogue-processing-101")
        self.assertIsNotNone(checkpoint["output_artifact"]["bundle_manifest_sha256"])
        self.assertEqual(checkpoint["attempts_consumed"], 1)
        self.assertTrue(self.checkpoint_path(checkpoint).exists())
        schema = json.loads((ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json").read_text())
        self.assertIs(MODULE.verify_checkpoint(checkpoint, schema), checkpoint)

    def test_fresh_observation_reuses_bound_detail_cache_without_refetching(self) -> None:
        calls = []
        fetch = lambda url, timeout: (calls.append(url) or self.successful_fetch(url, timeout))
        code, first = self.invoke(fetcher=fetch)
        self.assertEqual(code, 0)
        first_progress = first["last_progress_at"]
        resume_dir = self.root / "duplicate-resume"
        resume_dir.mkdir()
        resume = resume_dir / "upstream-catalogue-enrichment-evidence.json"
        resume.write_bytes((self.output_dir / resume.name).read_bytes())
        self.write_observation("2026-10-02T10:00:00Z")
        self.now = "2026-10-02T10:00:01Z"
        code, second = self.invoke(
            run_id="102", fetcher=fetch,
            **{"--resume-enrichment-evidence": resume},
        )
        self.assertEqual(code, 0)
        self.assertEqual(second["generation_id"], first["generation_id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["last_observation"]["observed_at"], "2026-10-02T10:00:00Z")
        self.assertNotEqual(second["last_progress_at"], first_progress)
        self.assertNotEqual(second["last_heartbeat_at"], first["last_heartbeat_at"])
        self.assertEqual(second["observed_at"], first["observed_at"])
        self.assertEqual(second["observation_count"], 2)

    def test_exact_delivery_replay_updates_heartbeat_only(self) -> None:
        calls = []
        fetch = lambda url, timeout: (calls.append(url) or self.successful_fetch(url, timeout))
        code, first = self.invoke(fetcher=fetch)
        self.assertEqual(code, 0)
        self.now = "2026-10-02T10:00:00Z"
        code, replay = self.invoke(
            run_id="102", fetcher=fetch,
            **{"--producer-run-id": "101", "--processor-run-id": "102"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(replay["observation_count"], 1)
        self.assertEqual(replay["last_progress_at"], first["last_progress_at"])
        self.assertNotEqual(replay["last_heartbeat_at"], first["last_heartbeat_at"])
    def test_exact_delivery_replay_has_an_explicit_idle_result_without_rebinding_old_artifact(self) -> None:
        code, first = self.invoke()
        self.assertEqual(code, 0)
        prior_locator = copy.deepcopy(first["output_artifact"])
        shutil.rmtree(self.output_dir)
        replay_args = self.args(
            run_id="102",
            **{"--producer-run-id": "101", "--processor-run-id": "102"},
        )
        code, replay = MODULE.process(replay_args, fetcher=lambda *_args: self.fail("exact replay must not fetch"))
        self.assertEqual(code, 0)
        self.assertTrue(replay_args.exact_delivery_replay)
        self.assertEqual(
            MODULE.processing_result(
                replay,
                exact_delivery_replay=True,
                producer_run_id="101",
                processor_run_id="102",
                processor_artifact_run_id="102",
            ),
            {
                "status": "idle",
                "reason": "exact_producer_delivery_replay",
                "processing_replay": True,
                "candidate_available": False,
                "source_id": "data_go_kr",
                "producer_run_id": "101",
                "processor_run_id": "102",
                "processor_artifact_run_id": "102",
            },
        )
        self.assertEqual(replay["output_artifact"], prior_locator)
        self.assertFalse((self.output_dir / "upstream-catalogue-checkpoint-receipt.json").exists())
        self.assertFalse((self.output_dir / "composed-candidate.registry.json").exists())

    def test_successful_observation_replay_requires_real_valid_producer_timestamp(self) -> None:
        code, checkpoint = self.invoke()
        self.assertEqual(code, 0)
        checkpoint_path = self.checkpoint_path(checkpoint)
        durable_before = checkpoint_path.read_bytes()
        base_evidence = json.loads(self.evidence_path.read_text(encoding="utf-8"))
        malformed_cases = [
            ("missing", None, "successful_producer_observation_timestamp_missing"),
            ("empty", "", "successful_producer_observation_timestamp_missing"),
            ("malformed", "not-a-timestamp", "producer_observation_timestamp_invalid"),
            ("future", "2026-10-01T10:06:00Z", "producer_observation_in_future"),
        ]

        for index, (label, observed_at, expected_error) in enumerate(malformed_cases, start=102):
            evidence = copy.deepcopy(base_evidence)
            if label == "missing":
                evidence.pop("observed_at")
            else:
                evidence["observed_at"] = observed_at
            self.evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.subTest(case=label), self.assertRaisesRegex(ValueError, expected_error):
                self.invoke(run_id=str(index))
            self.assertEqual(checkpoint_path.read_bytes(), durable_before)

    def test_policy_change_creates_new_generation_even_when_source_bytes_match(self) -> None:
        _, first = self.invoke()
        policy = json.loads(self.policy_path.read_text())
        policy["semantic_policy_revision"] = "changed"
        self.policy_path.write_text(json.dumps(policy), encoding="utf-8")
        self.now = "2026-10-02T10:00:00Z"
        _, second = self.invoke(run_id="102")
        self.assertNotEqual(first["generation_id"], second["generation_id"])
        self.assertEqual(first["generation_inputs"]["candidate_sha256"], second["generation_inputs"]["candidate_sha256"])

    def test_failed_requests_consume_attempt_budget_before_call_and_keep_retry_state(self) -> None:
        calls = []
        def fail(url: str, timeout: float) -> str:
            calls.append((url, timeout))
            raise TimeoutError("secret URL must not be recorded")
        code, checkpoint = self.invoke(fetcher=fail, **{"--retries-per-detail": 1, "--max-attempts": 2})
        self.assertEqual(code, 2)
        self.assertEqual(checkpoint["status"], "retry")
        self.assertEqual(checkpoint["attempts_consumed"], 2)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("secret", self.checkpoint_path(checkpoint).read_text())
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual(evidence["worker_outcomes"], [{
            "api_key": {"provider": "data.go.kr", "id": "2"},
            "status": "retry",
            "source_sha256": MODULE.source_fingerprint(self.new_link),
            "guide_sha256": MODULE.guide_fingerprint(self.new_link),
            "failure_diagnostic": {"code": "timeout"},
        }])
        self.assertEqual(checkpoint["detail_records"][-1]["failure_diagnostic"], {"code": "timeout"})
        self.assertEqual(checkpoint["outcome"]["detail_failure_counts"], {"timeout": 1})
        self.assertEqual(checkpoint["outcome"]["detail_reason_unavailable_count"], 0)
        self.assertEqual(checkpoint["outcome"]["detail_unattempted_count"], 0)

    def test_malformed_observation_timestamp_keeps_retry_budget_and_status(self) -> None:
        expected_url = "https://www.data.go.kr/data/2/openapi.do"
        body = "<html>malformed timestamp fixture</html>"
        page_bytes = body.encode("utf-8")
        observation = MODULE.DetailPageObservation(
            body=body,
            page_url=expected_url,
            effective_url=expected_url,
            page_sha256=MODULE.sha256_bytes(page_bytes),
            observed_at="not-a-timestamp",
        )
        calls = []

        def malformed_observation(url: str, _timeout: float):
            calls.append(url)
            return observation

        code, checkpoint = self.invoke(
            fetcher=malformed_observation,
            **{"--retries-per-detail": 2, "--max-attempts": 3},
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual(checkpoint["attempts_consumed"], 3)
        self.assertEqual(checkpoint["status"], "retry")
        self.assertEqual(checkpoint["detail_records"][-1]["status"], "retry")
        self.assertEqual(
            checkpoint["detail_records"][-1]["failure_diagnostic"],
            {"code": "observation_mismatch"},
        )

    def test_provider_http_error_persists_only_fixed_status_code(self) -> None:
        def fail(_url: str, _timeout: float) -> str:
            raise urllib.error.HTTPError(
                "https://www.data.go.kr/data/2/openapi.do?token=SECRET", 503,
                "SECRET response text", {}, None,
            )

        code, checkpoint = self.invoke(fetcher=fail, **{"--retries-per-detail": 0, "--max-attempts": 1})
        self.assertEqual(code, 2)
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual(evidence["worker_outcomes"][0]["failure_diagnostic"], {
            "code": "provider_http_error", "http_status": 503,
        })
        self.assertEqual(checkpoint["detail_records"][-1]["failure_diagnostic"], {
            "code": "provider_http_error", "http_status": 503,
        })
        serialized = self.checkpoint_path(checkpoint).read_text() + json.dumps(evidence)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("token=", serialized)

    def test_parser_failure_is_classified_at_parser_boundary_without_exception_text(self) -> None:
        with mock.patch.object(
            MODULE.DETAIL_HELPERS,
            "extract_link_detail_operation_urls",
            side_effect=ValueError("SECRET parser payload"),
        ):
            code, checkpoint = self.invoke(**{"--retries-per-detail": 0, "--max-attempts": 1})
        self.assertEqual(code, 2)
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual(evidence["worker_outcomes"][0]["failure_diagnostic"], {
            "code": "contract_or_parse_error",
        })
        serialized = self.checkpoint_path(checkpoint).read_text() + json.dumps(evidence)
        self.assertNotIn("SECRET", serialized)

    def test_failure_diagnostic_is_carried_then_cleared_by_success(self) -> None:
        calls = []

        def fail(url: str, timeout: float) -> str:
            calls.append(url)
            raise TimeoutError("secret timeout details")

        code, first = self.invoke(
            fetcher=fail, **{"--retries-per-detail": 1, "--max-attempts": 1},
        )
        self.assertEqual(code, 2)
        first_index = json.loads((self.state_dir / "sources/data_go_kr/index.json").read_text())
        identity = MODULE.record_id(self.new_link)
        self.assertEqual(first_index["detail_retry_state"][identity]["failure_diagnostic"], {"code": "timeout"})

        code, second = self.invoke(
            run_id="102", fetcher=lambda url, timeout: (calls.append(url) or self.successful_fetch(url, timeout)),
            **{"--retries-per-detail": 1, "--max-attempts": 1},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(second["detail_records"][-1]["status"], "enriched")
        self.assertNotIn("failure_diagnostic", second["detail_records"][-1])
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual(evidence["worker_outcomes"], [])
        second_index = json.loads((self.state_dir / "sources/data_go_kr/index.json").read_text())
        self.assertNotIn(identity, second_index["detail_retry_state"])
        self.assertNotIn("failure_diagnostic", json.dumps(second))

    def test_failure_diagnostic_survives_same_contract_new_generation_without_refetch(self) -> None:
        calls = []

        def fail(url: str, _timeout: float) -> str:
            calls.append(url)
            raise TimeoutError("first observed failure")

        code, first = self.invoke(
            fetcher=fail, **{"--retries-per-detail": 1, "--max-attempts": 1},
        )
        self.assertEqual(code, 2)
        policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        policy["semantic_policy_revision"] = "independent-policy-generation"
        self.policy_path.write_text(json.dumps(policy), encoding="utf-8")

        def unexpected_fetch(*_args):
            self.fail("exhausted retry must not issue another detail request")

        code, second = self.invoke(
            run_id="102", fetcher=unexpected_fetch,
            **{"--retries-per-detail": 0, "--max-attempts": 1},
        )
        self.assertEqual(code, 2)
        self.assertNotEqual(second["generation_id"], first["generation_id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["attempts_consumed"], 0)
        self.assertEqual(second["detail_records"][-1]["status"], "quarantined")
        self.assertEqual(second["detail_records"][-1]["failure_diagnostic"], {"code": "timeout"})
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual(evidence["worker_outcomes"][0]["failure_diagnostic"], {"code": "timeout"})

    def test_checkpoint_and_retry_index_reject_malformed_diagnostics(self) -> None:
        code, checkpoint = self.invoke(
            fetcher=lambda *_: (_ for _ in ()).throw(TimeoutError("secret")),
            **{"--retries-per-detail": 0, "--max-attempts": 1},
        )
        self.assertEqual(code, 2)
        schema = json.loads((ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json").read_text())
        tampered = copy.deepcopy(checkpoint)
        tampered["detail_records"][0]["failure_diagnostic"]["raw_error"] = "secret"
        MODULE.seal_checkpoint(tampered)
        with self.assertRaises(jsonschema.ValidationError):
            MODULE.verify_checkpoint(tampered, schema)

        index = json.loads((self.state_dir / "sources/data_go_kr/index.json").read_text())
        retry_row = next(iter(index["detail_retry_state"].values()))
        retry_row["failure_diagnostic"] = {"code": "provider_http_error", "http_status": 503, "message": "secret"}
        path = self.root / "tampered-index.json"
        path.write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "corrupt_detail_retry_state"):
            MODULE.source_retry_state(path)

    def test_failure_classifier_uses_fixed_types_not_exception_text_or_class_name(self) -> None:
        class SecretRuntimeFailure(RuntimeError):
            pass

        self.assertEqual(MODULE.classify_detail_exception(TimeoutError("SECRET URL")), {"code": "timeout"})
        self.assertEqual(MODULE.classify_detail_exception(urllib.error.URLError("SECRET URL")), {"code": "transport_error"})
        self.assertEqual(MODULE.classify_detail_exception(urllib.error.URLError(TimeoutError("SECRET URL"))), {"code": "timeout"})
        for status in (429, 503):
            error = urllib.error.HTTPError("https://public.example/path?token=SECRET", status, "SECRET body", {}, None)
            self.assertEqual(MODULE.classify_detail_exception(error), {
                "code": "provider_http_error", "http_status": status,
            })
        self.assertEqual(MODULE.classify_detail_exception(MODULE.DetailResponseBytesCapError("SECRET")), {"code": "response_bytes_cap"})
        self.assertEqual(MODULE.classify_detail_exception(MODULE.UnsafeDetailRedirectError("SECRET")), {"code": "unsafe_redirect"})
        self.assertEqual(MODULE.classify_detail_exception(SecretRuntimeFailure("SECRET")), {"code": "unexpected_error"})

    def test_retry_failure_diagnostic_is_dropped_when_source_fingerprint_changes(self) -> None:
        def fail(_url: str, _timeout: float) -> str:
            raise TimeoutError("old observed cause")

        code, original = self.invoke(fetcher=fail, **{"--retries-per-detail": 2, "--max-attempts": 1})
        self.assertEqual(code, 2)
        identity = MODULE.record_id(self.new_link)
        index_path = self.state_dir / "sources/data_go_kr/index.json"
        initial_index = json.loads(index_path.read_text())
        self.assertEqual(initial_index["detail_retry_state"][identity]["failure_diagnostic"], {"code": "timeout"})

        candidate = json.loads(self.candidate_path.read_text())
        candidate[-1]["source"]["raw"]["title"] = "Changed source contract"
        self.candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        self.write_observation("2026-10-02T10:00:00Z")
        self.now = "2026-10-02T10:00:01Z"
        code, changed = self.invoke(
            run_id="102", fetcher=lambda *_: self.fail("claim only must not fetch"),
            **{"--claim-only": None, "--retries-per-detail": 2, "--max-attempts": 1},
        )
        self.assertEqual(code, 0)
        self.assertNotEqual(changed["generation_id"], original["generation_id"])
        changed_index = json.loads(index_path.read_text())
        self.assertNotIn("failure_diagnostic", changed_index["detail_retry_state"][identity])

    def test_retry_failure_diagnostic_is_dropped_at_new_observation_epoch(self) -> None:
        def fail(_url: str, _timeout: float) -> str:
            raise TimeoutError("old observation failure")

        code, first = self.invoke(fetcher=fail, **{"--retries-per-detail": 2, "--max-attempts": 1})
        self.assertEqual(code, 2)
        identity = MODULE.record_id(self.new_link)
        index_path = self.state_dir / "sources/data_go_kr/index.json"
        initial_index = json.loads(index_path.read_text())
        self.assertEqual(initial_index["detail_retry_state"][identity]["failure_diagnostic"], {"code": "timeout"})

        self.write_observation("2026-10-22T10:00:00Z")
        self.now = "2026-10-22T10:00:01Z"
        code, next_claim = self.invoke(
            run_id="102", fetcher=lambda *_: self.fail("new epoch claim must not fetch"),
            **{"--claim-only": None, "--retries-per-detail": 2, "--max-attempts": 1},
        )
        self.assertEqual(code, 0)
        self.assertEqual(next_claim["generation_id"], first["generation_id"])
        self.assertIn(identity, next_claim["detail_retry_reset_ids"])
        updated_index = json.loads(index_path.read_text())
        self.assertNotIn("failure_diagnostic", updated_index["detail_retry_state"][identity])

    def test_unqueued_worker_rows_are_explicitly_retained_as_retry_outcomes(self) -> None:
        third = copy.deepcopy(self.new_link)
        third["id"] = "3"
        third["title"] = "Third detail"
        third["source"]["url"] = "https://www.data.go.kr/data/3/openapi.do"
        third["source"]["raw"]["meta_url"] = "https://www.data.go.kr/data/3/openapi.do"
        third["source"]["raw"]["api_id"] = "3"
        self.candidate_path.write_text(json.dumps([self.old_link, self.new_link, third]), encoding="utf-8")
        code, checkpoint = self.invoke(
            fetcher=lambda *_: (_ for _ in ()).throw(TimeoutError("fixture")),
            **{"--retries-per-detail": 0, "--max-attempts": 1, "--max-queue": 1},
        )
        self.assertEqual(code, 2)
        self.assertEqual(checkpoint["status"], "retry")
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual([row["api_key"]["id"] for row in evidence["worker_outcomes"]], ["2", "3"])
        self.assertEqual([row["status"] for row in evidence["worker_outcomes"]], ["retry", "retry"])
        self.assertEqual(
            [row["source_sha256"] for row in evidence["worker_outcomes"]],
            [MODULE.source_fingerprint(self.new_link), MODULE.source_fingerprint(third)],
        )

    def test_full_queue_diagnostics_count_observed_failures_and_unattempted_rows(self) -> None:
        rows = []
        for identity in range(1000, 1908):
            row = copy.deepcopy(self.new_link)
            row["id"] = str(identity)
            row["title"] = f"Queued detail {identity}"
            row["source"]["url"] = f"https://www.data.go.kr/data/{identity}/openapi.do"
            row["source"]["raw"]["api_id"] = str(identity)
            row["source"]["raw"]["meta_url"] = row["source"]["url"]
            rows.append(row)
        self.baseline_path.write_text("[]", encoding="utf-8")
        self.candidate_path.write_text(json.dumps(rows), encoding="utf-8")
        calls = []

        def fail(url: str, _timeout: float) -> str:
            calls.append(url)
            raise TimeoutError("SECRET detail response")

        code, checkpoint = self.invoke(
            fetcher=fail,
            **{"--retries-per-detail": 0, "--max-attempts": 24, "--max-queue": 48},
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 24)
        self.assertEqual(checkpoint["attempts_consumed"], 24)
        self.assertEqual(checkpoint["request_reservation"]["attempts_made"], 24)
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual(evidence["records"], [])
        self.assertEqual(len(evidence["worker_outcomes"]), 908)
        self.assertEqual(
            sum("failure_diagnostic" in row for row in evidence["worker_outcomes"]), 24,
        )
        self.assertEqual(checkpoint["outcome"]["detail_failure_counts"], {"timeout": 24})
        self.assertEqual(checkpoint["outcome"]["detail_reason_unavailable_count"], 884)
        self.assertEqual(checkpoint["outcome"]["detail_unattempted_count"], 884)
        result = json.loads((self.output_dir / "upstream-catalogue-processing-result.json").read_text())
        self.assertEqual(result["detail_failure_counts"], {"timeout": 24})
        self.assertEqual(result["detail_reason_unavailable_count"], 884)
        self.assertEqual(result["detail_unattempted_count"], 884)
        self.assertNotIn("SECRET", json.dumps(evidence) + json.dumps(checkpoint) + json.dumps(result))

    def test_unavailable_diagnostic_aggregate_can_exceed_retry_state_capacity(self) -> None:
        rows = []
        for identity in range(20_000, 24_100):
            row = copy.deepcopy(self.new_link)
            row["id"] = str(identity)
            row["title"] = f"Large queued detail {identity}"
            row["source"]["url"] = f"https://www.data.go.kr/data/{identity}/openapi.do"
            row["source"]["raw"]["api_id"] = str(identity)
            row["source"]["raw"]["meta_url"] = row["source"]["url"]
            rows.append(row)
        self.baseline_path.write_text("[]", encoding="utf-8")
        self.candidate_path.write_text(json.dumps(rows), encoding="utf-8")
        calls = []

        def fail(url: str, _timeout: float) -> str:
            calls.append(url)
            raise TimeoutError("fixture")

        code, checkpoint = self.invoke(
            fetcher=fail,
            **{"--retries-per-detail": 0, "--max-attempts": 1, "--max-queue": 1},
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(checkpoint["attempts_consumed"], 1)
        self.assertEqual(checkpoint["outcome"]["detail_failure_counts"], {"timeout": 1})
        self.assertEqual(checkpoint["outcome"]["detail_reason_unavailable_count"], 4099)
        self.assertEqual(checkpoint["outcome"]["detail_unattempted_count"], 4099)
        schema = json.loads((ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json").read_text())
        self.assertIs(MODULE.verify_checkpoint(checkpoint, schema), checkpoint)

    def test_new_generator_preserves_all_frozen_retry_attempts_across_recovery(self) -> None:
        from tests.test_recover_upstream_catalogue_failed_generation import RECOVERY, frozen_inputs

        values = frozen_inputs()
        durable_state, old_checkpoint, run, artifact, payload, current_run = values
        plan = RECOVERY.recover_failed_generation(
            durable_state, old_checkpoint, run, artifact, payload,
            RECOVERY.GENERATION_CHECKPOINT_SHA256, current_run,
            datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc),
        )
        terminal = MODULE.seal_checkpoint(plan.checkpoint)
        old_retry_state = copy.deepcopy(durable_state["index"]["detail_retry_state"])
        self.assertEqual(len(old_retry_state), 24)
        self.assertEqual({row["attempts"] for row in old_retry_state.values()}, {1})

        source_dir = self.state_dir / "sources/data_go_kr"
        generation_dir = source_dir / "generations"
        generation_dir.mkdir(parents=True)
        old_index = copy.deepcopy(durable_state["index"])
        old_index["generations"][0].update(
            status="quarantined", updated_at=terminal["last_heartbeat_at"],
        )
        (source_dir / "index.json").write_text(json.dumps(old_index), encoding="utf-8")
        (generation_dir / f"{terminal['generation_id']}.json").write_text(
            json.dumps(terminal), encoding="utf-8",
        )

        candidate_rows = []
        for identity in sorted(old_retry_state):
            url = f"https://www.data.go.kr/data/{identity}/openapi.do"
            candidate_rows.append({
                "id": identity,
                "provider": "data.go.kr",
                "type": "LINK",
                "title": f"Frozen retry {identity}",
                "operations": [],
                "source": {
                    "system": "data.go.kr",
                    "url": url,
                    "raw": {"type": "LINK", "api_type": "LINK", "api_id": identity, "meta_url": url},
                },
            })
        self.baseline_path.write_text("[]", encoding="utf-8")
        self.candidate_path.write_text(json.dumps(candidate_rows), encoding="utf-8")
        self.write_observation("2026-10-03T03:50:00Z")
        self.now = "2026-10-03T04:00:00Z"
        self.provider_index_path.write_text(json.dumps({"adapters": []}), encoding="utf-8")

        def frozen_source_fingerprint(row: dict) -> str:
            return old_retry_state[MODULE.record_id(row)]["source_sha256"]

        def frozen_guide_fingerprint(row: dict) -> str | None:
            return old_retry_state[MODULE.record_id(row)]["guide_sha256"]

        calls = []

        def fail_again(url: str, _timeout: float) -> str:
            calls.append(url)
            raise TimeoutError("frozen retry fixture")

        with (
            mock.patch.object(MODULE, "source_fingerprint", side_effect=frozen_source_fingerprint),
            mock.patch.object(MODULE, "guide_fingerprint", side_effect=frozen_guide_fingerprint),
        ):
            code, checkpoint = self.invoke(
                run_id="36646768289",
                fetcher=fail_again,
                **{
                    "--processor-run-id": "38000000000-1",
                    "--processor-artifact-run-id": "38000000000",
                    "--max-attempts": 24,
                    "--max-queue": 48,
                    "--retries-per-detail": 2,
                },
            )

        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 24)
        self.assertNotEqual(checkpoint["generation_id"], RECOVERY.GENERATION_ID)
        self.assertEqual(checkpoint["generation_inputs"]["generator_revision"], MODULE.generator_revision())
        self.assertNotEqual(
            checkpoint["generation_inputs"]["generator_revision"],
            old_checkpoint["generation_inputs"]["generator_revision"],
        )
        self.assertEqual(checkpoint["attempts_consumed"], 24)
        self.assertEqual(checkpoint["detail_queue_cursor"], 0)
        self.assertEqual(checkpoint["attempts_by_id"], {identity: 2 for identity in old_retry_state})
        updated_index = json.loads((source_dir / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(set(updated_index["detail_retry_state"]), set(old_retry_state))
        for identity, previous in old_retry_state.items():
            current = updated_index["detail_retry_state"][identity]
            self.assertEqual(current["attempts"], previous["attempts"] + 1, identity)
            self.assertEqual(current["source_sha256"], previous["source_sha256"], identity)
            self.assertEqual(current["guide_sha256"], previous["guide_sha256"], identity)

    def test_worker_scope_mismatch_seals_result_only_quarantine_marker(self) -> None:
        def fail(_url: str, _timeout: float) -> str:
            raise TimeoutError("fixture transient error")
        with mock.patch.dict(os.environ, {"TEST_OMIT_WORKER_OUTCOMES": "1"}):
            code, checkpoint = self.invoke(fetcher=fail, **{"--retries-per-detail": 0, "--max-attempts": 1})
        self.assertEqual(code, 3)
        self.assertEqual(checkpoint["status"], "quarantined")
        self.assertEqual(checkpoint["outcome"]["reason"], "composer_scope_omits_worker_outcomes")
        self.assertEqual([row["path"] for row in checkpoint["output_digests"]], ["upstream-catalogue-processing-result.json"])
        self.assertEqual(checkpoint["output_artifact"]["bundle_manifest_sha256"], MODULE.sha256_bytes(MODULE.canonical_json(checkpoint["output_digests"])))
        result = json.loads((self.output_dir / "upstream-catalogue-processing-result.json").read_text())
        self.assertFalse(result["candidate_available"])
        for name in ("composed-candidate.registry.json", "ready-scope.registry.json", "semantic-diff.json", "regeneration-queue.json", "quarantine.json", "composition-receipt.json", "upstream-catalogue-enrichment-evidence.json"):
            self.assertFalse((self.output_dir / name).exists())
        schema = json.loads((ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json").read_text())
        self.assertIs(MODULE.verify_checkpoint(checkpoint, schema), checkpoint)

    def test_claim_reserves_attempts_durably_before_any_detail_request(self) -> None:
        args = self.args(**{"--claim-only": None, "--max-attempts": 2, "--retries-per-detail": 2})
        code, claim = MODULE.process(args, fetcher=lambda *_: self.fail("claim made a network request"))
        self.assertEqual(code, 0)
        self.assertEqual(claim["outcome"]["reason"], "request_budget_reserved")
        self.assertEqual(claim["request_reservation"]["reserved_attempts"], 2)
        self.assertEqual(claim["attempts_consumed"], 2)
        self.assertEqual(claim["attempts_by_id"], {"2": 2})
        self.assertEqual(claim["request_reservation"]["owner_run_id"], "101")

        args.claim_only = False
        args.require_durable_reservation = True
        code, completed = MODULE.process(args, fetcher=self.successful_fetch, sleeper=lambda _delay: None)
        self.assertEqual(code, 0)
        self.assertEqual(completed["attempts_consumed"], 1)
        self.assertEqual(completed["output_artifact"]["run_id"], "101")

    def test_distinct_processor_run_cannot_consume_another_runs_reservation(self) -> None:
        claim_args = self.args(**{"--claim-only": None, "--max-attempts": 1})
        _, claim = MODULE.process(claim_args)
        process_args = self.args(run_id="101", **{
            "--producer-run-id": "101", "--processor-run-id": "processor-2",
            "--require-durable-reservation": None,
        })
        code, conflict = MODULE.process(process_args)
        self.assertEqual(code, 2)
        self.assertEqual(conflict["outcome"]["reason"], "lease_conflict")
        self.assertEqual(claim["request_reservation"]["owner_run_id"], "101")

    def write_real_composer_inputs(self, baseline_rows, candidate_rows, observed_at: str) -> None:
        baseline_bytes = json.dumps(baseline_rows, ensure_ascii=False, separators=(",", ":")).encode()
        candidate_bytes = json.dumps(candidate_rows, ensure_ascii=False, separators=(",", ":")).encode()
        self.baseline_path.write_bytes(baseline_bytes)
        self.candidate_path.write_bytes(candidate_bytes)
        added = [row for row in candidate_rows if row["id"] not in {item["id"] for item in baseline_rows}]
        removed = [row for row in baseline_rows if row["id"] not in {item["id"] for item in candidate_rows}]
        summary = {"added": len(added), "removed": len(removed), "changed": 0, "stable": len(candidate_rows) - len(added)}
        diff = {
            "generated_at": observed_at, "provider": "data.go.kr",
            "old": self.baseline_path.name, "new": self.candidate_path.name,
            "limit": 0, "truncated": False,
            "counts": {"old": len(baseline_rows), "new": len(candidate_rows)},
            "summary": summary,
            "added": [{"id": row["id"], "title": row["title"], "provider": row["provider"], "operations_count": len(row["operations"])} for row in added],
            "removed": [{"id": row["id"], "title": row["title"], "provider": row["provider"], "operations_count": len(row["operations"])} for row in removed],
            "changed": [],
        }
        diff_bytes = json.dumps(diff, ensure_ascii=False, separators=(",", ":")).encode()
        self.diff_path.write_bytes(diff_bytes)
        self.evidence_path.write_text(json.dumps({
            "schema_version": "datapan.upstream-refresh-evidence.v1", "observed_at": observed_at,
            "source_id": "data_go_kr", "owner": "test", "status": "material_change" if added or removed else "no_change",
            "collection": {"attempted": True, "succeeded": True, "exit_code": 0, "error_class": None},
            "baseline": {"path": self.baseline_path.name, "bytes": len(baseline_bytes), "sha256": MODULE.sha256_bytes(baseline_bytes), "records": len(baseline_rows)},
            "snapshot": {"path": self.candidate_path.name, "bytes": len(candidate_bytes), "sha256": MODULE.sha256_bytes(candidate_bytes), "records": len(candidate_rows)},
            "diff": {"path": self.diff_path.name, "sha256": MODULE.sha256_bytes(diff_bytes), "summary": summary},
            "review": {"action": "review_catalog_drift" if added or removed else "none", "work_key": "upstream-refresh:data_go_kr:0123456789abcdef"},
            "publication": {"automatic": False, "release_allowed": False, "required_gates": ["release_manifest_verification", "release_readiness", "consumer_compatibility"]},
        }), encoding="utf-8")
        self.policy_path.write_text(json.dumps({"sources": [{
            "source_id": "data_go_kr", "canonical_registry": "data/data-go-kr.registry.json",
            "importer": {"capability": "catalogue_import"}, "publication": {"automatic": False},
        }]}), encoding="utf-8")
        self.provider_index_path = ROOT / "data/provider-index.json"

    @staticmethod
    def real_link_row(request_count: int = 1) -> dict:
        return {
            "id": "2", "title": "Guide-less LINK", "provider": "data.go.kr", "priority": "medium", "operations": [],
            "source": {
                "system": "data.go.kr", "url": "https://www.data.go.kr/data/2/openapi.do",
                "raw": {"api_type": "LINK", "type": "LINK", "title": "Guide-less LINK", "meta_url": "https://www.data.go.kr/data/2/openapi.do", "request_cnt": request_count},
            },
        }

    @unittest.skipUnless(ACTUAL_COMPOSER.is_file(), "actual composer integration requires the reviewed #656 CLI")
    def test_real_composer_admits_missing_guide_from_verified_page_then_rebinds_cache(self) -> None:
        first_at = "2026-10-01T10:00:00Z"
        row = self.real_link_row()
        self.write_real_composer_inputs([], [row], first_at)
        args = self.args(run_id="101001", **{"--composer": ACTUAL_COMPOSER})
        args.fixture_composer = None
        args.allow_fixture_composer = False
        page_bytes = b'<a href="https://www.data.go.kr/guide/2.pdf">API Guide</a><a href="https://openapi.airport.co.kr/detail" onclick="fn_LinkApiRequest()">API</a>'
        observation = MODULE.DetailPageObservation(
            body=page_bytes.decode(), page_bytes=page_bytes,
            page_url="https://www.data.go.kr/data/2/openapi.do",
            effective_url="https://www.data.go.kr/data/2/openapi.do",
            page_sha256=MODULE.sha256_bytes(page_bytes), observed_at=first_at,
        )
        code, first = MODULE.process(args, fetcher=lambda _url, _timeout: observation, sleeper=lambda _delay: None)
        self.assertEqual(code, 0, first.get("outcome"))
        self.assertEqual(first["status"], "ready")
        receipt = json.loads((self.output_dir / "composition-receipt.json").read_text())
        self.assertEqual(receipt["status"], "ready_scoped")
        self.assertFalse(receipt["scope"]["full_scope_fresh"])
        composed = json.loads((self.output_dir / "composed-candidate.registry.json").read_text())
        self.assertEqual(len(composed), 1)
        self.assertEqual(composed[0]["source"]["raw"]["guide_url"], "https://www.data.go.kr/guide/2.pdf")
        enrichment_name = "upstream-catalogue-enrichment-evidence.json"
        resume_dir = self.root / "real-composer-resume"
        resume_dir.mkdir()
        resume = resume_dir / "upstream-catalogue-enrichment-evidence.json"
        resume.write_bytes((self.output_dir / enrichment_name).read_bytes())

        second_at = "2026-10-02T10:00:00Z"
        row2 = self.real_link_row(request_count=2)
        canonical_baseline = self.output_dir / "composed-candidate.registry.json"
        self.baseline_path.write_bytes(canonical_baseline.read_bytes())
        self.write_real_composer_inputs(composed, [row2], second_at)
        self.now = second_at
        second_args = self.args(run_id="102002", **{
            "--baseline": self.baseline_path, "--resume-enrichment-evidence": resume,
            "--composer": ACTUAL_COMPOSER,
        })
        second_args.fixture_composer = None
        second_args.allow_fixture_composer = False
        second_calls = []
        code, second = MODULE.process(
            second_args, fetcher=lambda url, timeout: (second_calls.append(url) or observation),
            sleeper=lambda _delay: None,
        )
        self.assertEqual(code, 0, second.get("outcome"))
        self.assertEqual(second["status"], "no-change")
        no_change_result = MODULE.processing_result(
            second, producer_run_id="102002", processor_run_id="102002", processor_artifact_run_id="102002",
        )
        self.assertFalse(no_change_result["candidate_available"])
        self.assertFalse(no_change_result["processing_replay"])
        self.assertEqual(second_calls, [])
        second_receipt = json.loads((self.output_dir / "composition-receipt.json").read_text())
        self.assertEqual(second_receipt["status"], "no_change")
        self.assertEqual(second["observation_count"], 1)
        rebound = json.loads((self.output_dir / enrichment_name).read_text())
        self.assertEqual(rebound["original_candidate_sha256"], MODULE.file_sha256(self.candidate_path))
        self.assertEqual(rebound["records"][0]["source_provenance"]["observed_at"], first_at)

    @unittest.skipUnless(ACTUAL_COMPOSER.is_file(), "bootstrap regression requires the reviewed #656 composer")
    def test_bootstrap_processor_resumes_old_producer_without_producer_code_or_schema(self) -> None:
        observed_at = "2026-10-03T10:00:00Z"
        self.write_real_composer_inputs([], [self.real_link_row()], observed_at)
        bootstrap = self.root / "bootstrap"
        bootstrap.symlink_to(ROOT, target_is_directory=True)
        producer = self.root / "datapan-registry"
        producer.mkdir()
        state_repo = self.root / "state-repo"

        data = producer / "data"
        data.mkdir()
        shutil.copy2(self.baseline_path, data / "data-go-kr.registry.json")
        shutil.copy2(self.provider_index_path, data / "provider-index.json")
        policy_dir = producer / "policy"
        policy_dir.mkdir()
        shutil.copy2(self.policy_path, policy_dir / "source-refresh.json")
        artifact_dir = producer / ".datapan/ci/upstream-refresh"
        artifact_dir.mkdir(parents=True)
        shutil.copy2(self.candidate_path, artifact_dir / "candidate.registry.json")
        shutil.copy2(self.diff_path, artifact_dir / "catalog-diff.json")
        shutil.copy2(self.evidence_path, artifact_dir / "upstream-refresh-evidence.json")

        self.assertFalse((producer / "scripts/process-upstream-catalogue-candidate.py").exists())
        self.assertFalse((producer / "scripts/compose-upstream-catalogue-candidate.py").exists())
        self.assertFalse((producer / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json").exists())

        page_bytes = (
            b'<a href="https://www.data.go.kr/guide/2.pdf">API Guide</a>'
            b'<a href="https://openapi.airport.co.kr/detail" onclick="fn_LinkApiRequest()">API</a>'
        )
        (producer / "sitecustomize.py").write_text(
            "import urllib.request\n"
            "BODY = " + repr(page_bytes) + "\n"
            "class Response:\n"
            "    def __init__(self, request):\n"
            "        self.url = request.full_url\n"
            "        self.headers = {'Content-Length': str(len(BODY))}\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *_args): return False\n"
            "    def geturl(self): return self.url\n"
            "    def read(self, size=-1): return BODY[:size]\n"
            "class Opener:\n"
            "    def open(self, request, timeout): return Response(request)\n"
            "urllib.request.build_opener = lambda *_args, **_kwargs: Opener()\n",
            encoding="utf-8",
        )

        processor = "../bootstrap/scripts/process-upstream-catalogue-candidate.py"
        checkpoint_schema = "../bootstrap/schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
        composer = "../bootstrap/scripts/compose-upstream-catalogue-candidate.py"
        state_dir = "../state-repo/.datapan/upstream-catalogue-state"
        common = [
            "--baseline", "data/data-go-kr.registry.json",
            "--candidate", ".datapan/ci/upstream-refresh/candidate.registry.json",
            "--diff", ".datapan/ci/upstream-refresh/catalog-diff.json",
            "--refresh-evidence", ".datapan/ci/upstream-refresh/upstream-refresh-evidence.json",
            "--source-policy", "policy/source-refresh.json",
            "--provider-index", "data/provider-index.json",
            "--checkpoint-schema", checkpoint_schema,
            "--composer", composer,
            "--state-dir", state_dir,
            "--output-dir", ".datapan/ci/upstream-catalogue-processing",
            "--producer-run-id", "123456789",
            "--producer-run-url", "https://github.com/StatPan/datapan-registry/actions/runs/123456789",
            "--processor-run-id", "777-1",
            "--processor-artifact-run-id", "777",
            "--repository", "StatPan/datapan-registry",
            "--execution-mode", "fixture",
            "--artifact-name", "upstream-catalog-refresh-123456789",
            "--input-artifact-id", "654321",
            "--artifact-expires-at", "2099-03-04T05:06:07Z",
            "--output-artifact-expires-at", "2099-03-04T05:06:07Z",
            "--max-attempts", "24", "--max-queue", "48", "--retries-per-detail", "2", "--timeout", "20",
            "--now", "2026-10-03T10:00:01Z",
        ]
        env = {
            **os.environ,
            "PYTHONPATH": str(producer) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        def run_processor(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["python3", processor, *arguments], cwd=producer, env=env,
                text=True, capture_output=True, check=False,
            )

        producer_inputs = (
            data / "data-go-kr.registry.json",
            data / "provider-index.json",
            policy_dir / "source-refresh.json",
            artifact_dir / "candidate.registry.json",
            artifact_dir / "catalog-diff.json",
            artifact_dir / "upstream-refresh-evidence.json",
        )
        input_digests = {path.name: MODULE.file_sha256(path) for path in producer_inputs}
        claim = run_processor(*common, "--claim-only")
        self.assertEqual(claim.returncode, 0, claim.stderr or claim.stdout)
        claim_result = json.loads(claim.stdout)
        generation_id = claim_result["generation_id"]
        self.assertRegex(generation_id, r"^[a-f0-9]{64}$")
        checkpoint_path = state_repo / ".datapan/upstream-catalogue-state/sources/data_go_kr/generations" / f"{generation_id}.json"
        reserved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(reserved["request_reservation"]["owner_run_id"], "777-1")

        worker = run_processor(*common, "--require-durable-reservation")
        self.assertEqual(worker.returncode, 0, worker.stderr or worker.stdout)
        completed = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(completed["status"], "ready")
        self.assertEqual(
            {path.name: MODULE.file_sha256(path) for path in producer_inputs},
            input_digests,
            "processor must leave the immutable producer baseline, policy, adapter and artifact bytes untouched",
        )

        generation_inputs = completed["generation_inputs"]
        self.assertEqual(generation_inputs["baseline_sha256"], input_digests["data-go-kr.registry.json"])
        self.assertEqual(generation_inputs["policy_sha256"], input_digests["source-refresh.json"])
        self.assertEqual(generation_inputs["adapter_revision"], input_digests["provider-index.json"])
        self.assertEqual(
            generation_inputs["generator_revision"],
            MODULE.file_sha256(bootstrap / "scripts/process-upstream-catalogue-candidate.py"),
        )
        self.assertEqual(
            generation_inputs["extractor_revision"],
            MODULE.file_sha256(bootstrap / "scripts/generate-batch-link-detail-registry-patches.py"),
        )
        composition = json.loads(
            (producer / ".datapan/ci/upstream-catalogue-processing/composition-receipt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(composition["input_digests"]["composer"]["sha256"], MODULE.file_sha256(bootstrap / "scripts/compose-upstream-catalogue-candidate.py"))
        self.assertEqual(composition["input_digests"]["baseline"]["sha256"], input_digests["data-go-kr.registry.json"])
        self.assertEqual(composition["input_digests"]["source_policy"]["sha256"], input_digests["source-refresh.json"])
        self.assertEqual(composition["input_digests"]["provider_index"]["sha256"], input_digests["provider-index.json"])

        bind = run_processor(
            "--source", "data_go_kr", "--state-dir", state_dir,
            "--checkpoint-schema", checkpoint_schema,
            "--processor-run-id", "777-1", "--processor-artifact-run-id", "777",
            "--bind-output-artifact-id", "998877",
            "--bind-output-artifact-expires-at", "2099-03-04T05:06:07Z",
            "--bind-generation-id", generation_id,
        )
        self.assertEqual(bind.returncode, 0, bind.stderr or bind.stdout)
        bound = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(bound["output_artifact"]["artifact_id"], "998877")

    @unittest.skipUnless(ACTUAL_COMPOSER.is_file(), "actual composer integration requires the reviewed #656 CLI")
    def test_real_composer_keeps_failed_link_pending_while_admitting_safe_rest_addition(self) -> None:
        observed_at = "2026-10-01T10:00:00Z"
        link = self.real_link_row()
        rest = {
            "id": "11", "title": "Safe REST addition", "provider": "data.go.kr", "priority": "medium", "operations": [],
            "source": {"system": "data.go.kr", "url": "https://www.data.go.kr/data/11/openapi.do", "raw": {"api_type": "REST", "type": "REST", "title": "Safe REST addition", "meta_url": "https://www.data.go.kr/data/11/openapi.do"}},
        }
        self.write_real_composer_inputs([], [link, rest], observed_at)
        args = self.args(run_id="101001", **{"--composer": ACTUAL_COMPOSER})
        args.fixture_composer = None
        args.allow_fixture_composer = False
        wrong_page_bytes = b'<a href="https://openapi.airport.co.kr/detail" onclick="fn_LinkApiRequest()">API</a>'
        wrong_observation = MODULE.DetailPageObservation(
            body=wrong_page_bytes.decode(), page_bytes=wrong_page_bytes,
            page_url="https://www.data.go.kr/data/2/openapi.do",
            effective_url="https://www.data.go.kr/data/3/openapi.do",
            page_sha256=MODULE.sha256_bytes(wrong_page_bytes), observed_at=observed_at,
        )
        code, checkpoint = MODULE.process(args, fetcher=lambda _url, _timeout: wrong_observation, sleeper=lambda _delay: None)
        self.assertEqual(code, 0, checkpoint.get("outcome"))
        self.assertEqual(checkpoint["status"], "ready")
        self.assertEqual(checkpoint["outcome"]["pending_count"], 1)
        ready_result = MODULE.processing_result(
            checkpoint, producer_run_id="101001", processor_run_id="101001", processor_artifact_run_id="101001",
        )
        self.assertTrue(ready_result["candidate_available"])
        receipt = json.loads((self.output_dir / "composition-receipt.json").read_text())
        self.assertEqual(receipt["status"], "ready_scoped")
        self.assertEqual(receipt["scope"]["quarantined_api_keys"], [{"provider": "data.go.kr", "id": "2"}])
        self.assertEqual(receipt["scope"]["applied_api_keys"], [{"provider": "data.go.kr", "id": "11"}])
        ready = json.loads((self.output_dir / "ready-scope.registry.json").read_text())
        self.assertEqual([row["id"] for row in ready], ["11"])

    @unittest.skipUnless(ACTUAL_COMPOSER.is_file(), "actual composer integration requires the reviewed #656 CLI")
    def test_real_composer_ready_scoped_pending_link_continues_on_same_producer_delivery(self) -> None:
        observed_at = "2026-10-01T10:00:00Z"
        link = self.real_link_row()
        rest = {
            "id": "11", "title": "Safe REST addition", "provider": "data.go.kr", "priority": "medium", "operations": [],
            "source": {"system": "data.go.kr", "url": "https://www.data.go.kr/data/11/openapi.do", "raw": {"api_type": "REST", "type": "REST", "title": "Safe REST addition", "meta_url": "https://www.data.go.kr/data/11/openapi.do"}},
        }
        self.write_real_composer_inputs([], [link, rest], observed_at)
        first_args = self.args(run_id="101101", **{
            "--composer": ACTUAL_COMPOSER,
            "--max-attempts": 1, "--retries-per-detail": 2,
        })
        first_args.fixture_composer = None
        first_args.allow_fixture_composer = False
        calls = []
        def fail_link(url: str, timeout: float):
            calls.append(url)
            raise TimeoutError("temporarily unavailable")
        code, first = MODULE.process(first_args, fetcher=fail_link, sleeper=lambda _delay: None)
        self.assertEqual(code, 0, first.get("outcome"))
        self.assertEqual(first["status"], "ready")
        self.assertEqual(first["outcome"]["detail_retry_count"], 1)
        self.assertEqual(first["outcome"]["pending_count"], 1)
        self.assertEqual(json.loads((self.output_dir / "ready-scope.registry.json").read_text())[0]["id"], "11")

        self.now = "2026-10-01T11:00:00Z"
        second_args = self.args(run_id="101102", **{
            "--producer-run-id": "101101", "--processor-run-id": "101102",
            "--producer-run-url": "https://github.com/StatPan/datapan-registry/actions/runs/101101",
            "--composer": ACTUAL_COMPOSER,
            "--max-attempts": 1, "--retries-per-detail": 2,
        })
        second_args.fixture_composer = None
        second_args.allow_fixture_composer = False
        valid_page = '<a href="https://www.data.go.kr/guide/2.pdf">API Guide</a><a href="https://openapi.airport.co.kr/detail" onclick="fn_LinkApiRequest()">API</a>'
        code, second = MODULE.process(second_args, fetcher=lambda _url, _timeout: valid_page, sleeper=lambda _delay: None)
        self.assertEqual(code, 0, second.get("outcome"))
        self.assertEqual(second["generation_id"], first["generation_id"])
        self.assertEqual(second["status"], "ready")
        self.assertEqual(second["outcome"]["detail_retry_count"], 0)
        self.assertEqual(second["observation_count"], 1)
        self.assertEqual(calls, ["https://www.data.go.kr/data/2/openapi.do"])
        second_receipt = json.loads((self.output_dir / "composition-receipt.json").read_text())
        self.assertEqual(second_receipt["status"], "ready_scoped")
        self.assertEqual(second_receipt["scope"]["quarantined_api_keys"], [])

    def test_output_artifact_retention_is_independent_of_input_expiry(self) -> None:
        output_expiry = "2026-10-31T10:00:00Z"
        code, checkpoint = self.invoke(**{
            "--artifact-expires-at": "2026-10-02T10:00:00Z",
            "--output-artifact-expires-at": output_expiry,
        })
        self.assertEqual(code, 0)
        self.assertEqual(checkpoint["input_artifacts"][0]["expires_at"], "2026-10-02T10:00:00Z")
        self.assertEqual(checkpoint["output_artifact"]["expires_at"], output_expiry)

    def test_output_artifact_must_expire_in_the_future(self) -> None:
        with self.assertRaisesRegex(ValueError, "output_artifact_expired"):
            self.invoke(**{"--output-artifact-expires-at": self.now})

    def test_request_bounds_cannot_be_overridden_above_policy_caps(self) -> None:
        for overrides in (
            {"--max-attempts": MODULE.DEFAULT_MAX_ATTEMPTS + 1},
            {"--max-queue": MODULE.DEFAULT_MAX_QUEUE + 1},
            {"--retries-per-detail": MODULE.DEFAULT_RETRIES_PER_DETAIL + 1},
            {"--timeout": 31},
        ):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, "invalid_request_bounds"):
                MODULE.process(self.args(**overrides))

    def test_artifact_id_binding_requires_the_checkpoint_processor_identity(self) -> None:
        code, checkpoint = self.invoke(run_id="101", **{"--processor-artifact-run-id": "5001"})
        self.assertEqual(code, 0)
        schema = ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json"
        actual_expiry = "2099-03-04T05:06:07Z"
        with self.assertRaisesRegex(ValueError, "output_artifact_owner_mismatch"):
            MODULE.bind_output_artifact_id(
                self.state_dir, "data_go_kr", checkpoint["generation_id"], "artifact-77", schema,
                processor_run_id="stale-run", processor_artifact_run_id="5001",
                artifact_expires_at=actual_expiry,
            )
        MODULE.bind_output_artifact_id(
            self.state_dir, "data_go_kr", checkpoint["generation_id"], "artifact-77", schema,
            processor_run_id="101", processor_artifact_run_id="5001",
            artifact_expires_at=actual_expiry,
        )
        bound = json.loads(self.checkpoint_path(checkpoint).read_text())
        self.assertEqual(bound["output_artifact"]["artifact_id"], "artifact-77")
        self.assertEqual(bound["output_artifact"]["expires_at"], actual_expiry)

    def test_retry_restarts_from_checkpoint_attempt_count_with_same_retry_bounds(self) -> None:
        def fail(_url: str, _timeout: float) -> str:
            raise TimeoutError("offline")
        code, first = self.invoke(fetcher=fail, **{"--retries-per-detail": 2, "--max-attempts": 1})
        self.assertEqual(code, 2)
        self.assertEqual(first["attempts_consumed"], 1)
        self.write_observation("2026-10-02T10:00:00Z")
        self.now = "2026-10-02T10:00:01Z"
        code, resumed = self.invoke(run_id="102", **{"--retries-per-detail": 2, "--max-attempts": 1})
        self.assertEqual(code, 0)
        self.assertEqual(resumed["generation_id"], first["generation_id"])
        self.assertEqual(resumed["attempts_consumed"], 2)

    def test_retry_resumes_digest_bound_enrichment_without_refetching_successful_row(self) -> None:
        third = copy.deepcopy(self.new_link)
        third["id"] = "3"
        third["source"]["url"] = "https://www.data.go.kr/data/3/openapi.do"
        third["source"]["raw"]["api_id"] = "3"
        third["source"]["raw"]["meta_url"] = "https://www.data.go.kr/data/3/openapi.do"
        self.candidate_path.write_text(json.dumps([self.old_link, self.new_link, third]), encoding="utf-8")
        calls = []
        def first_fetch(url: str, timeout: float) -> str:
            calls.append(url)
            if url.endswith("/3/openapi.do"):
                raise TimeoutError("temporarily unavailable")
            return self.successful_fetch(url, timeout)
        code, first = self.invoke(fetcher=first_fetch, **{"--retries-per-detail": 2, "--max-attempts": 2})
        self.assertEqual(code, 2)
        self.assertEqual(first["status"], "retry")
        self.assertEqual(calls, [
            "https://www.data.go.kr/data/2/openapi.do",
            "https://www.data.go.kr/data/3/openapi.do",
        ])
        resume_dir = self.root / "resume"
        resume_dir.mkdir()
        resume = resume_dir / "upstream-catalogue-enrichment-evidence.json"
        resume.write_bytes((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_bytes())
        self.write_observation("2026-10-02T10:00:00Z")
        self.now = "2026-10-02T10:00:01Z"
        second_calls = []
        def second_fetch(url: str, timeout: float) -> str:
            second_calls.append(url)
            return self.successful_fetch(url, timeout)
        code, resumed = self.invoke(
            run_id="102", fetcher=second_fetch,
            **{"--retries-per-detail": 2, "--max-attempts": 2, "--resume-enrichment-evidence": resume},
        )
        self.assertEqual(code, 0)
        self.assertEqual(second_calls, ["https://www.data.go.kr/data/3/openapi.do"])
        self.assertEqual(resumed["attempts_consumed"], 3)
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual([row["api_key"]["id"] for row in evidence["records"]], ["2", "3"])

    def test_fair_cursor_advances_past_exhausted_prefix_across_counter_only_generations(self) -> None:
        rows = []
        for identity in ("2", "3", "4"):
            row = copy.deepcopy(self.new_link)
            row["id"] = identity
            row["source"]["url"] = f"https://www.data.go.kr/data/{identity}/openapi.do"
            row["source"]["raw"]["api_id"] = identity
            row["source"]["raw"]["meta_url"] = row["source"]["url"]
            rows.append(row)
        self.baseline_path.write_text("[]", encoding="utf-8")
        self.candidate_path.write_text(json.dumps(rows), encoding="utf-8")
        first_calls = []
        def first_fetch(url: str, _timeout: float) -> str:
            first_calls.append(url)
            raise TimeoutError("offline")
        code, first = self.invoke(fetcher=first_fetch, **{
            "--max-attempts": 2, "--max-queue": 2, "--retries-per-detail": 0,
        })
        self.assertEqual(code, 2)
        self.assertEqual(len(first_calls), 2)
        self.assertEqual(first["detail_queue_cursor"], 2)
        self.assertEqual(first["attempts_by_id"], {"2": 1, "3": 1})

        for row in rows:
            row["source"]["raw"]["request_cnt"] = 7
        self.candidate_path.write_text(json.dumps(rows), encoding="utf-8")
        self.write_observation("2026-10-02T10:00:00Z")
        self.now = "2026-10-02T10:00:01Z"
        later_calls = []
        def later_fetch(url: str, timeout: float) -> str:
            later_calls.append(url)
            return self.successful_fetch(url, timeout)
        code, second = self.invoke(run_id="102", fetcher=later_fetch, **{
            "--max-attempts": 2, "--max-queue": 2, "--retries-per-detail": 0,
        })
        self.assertEqual(code, 2)
        self.assertEqual(later_calls, ["https://www.data.go.kr/data/4/openapi.do"])
        statuses = {row["id"]: row["status"] for row in second["detail_records"]}
        self.assertEqual(statuses, {"4": "enriched", "2": "quarantined", "3": "quarantined"})
        self.assertEqual(second["attempts_by_id"], {"2": 1, "3": 1})

    def test_large_queue_resumes_after_first_bounded_chunk_without_restarting_prefix(self) -> None:
        rows = []
        for identity in range(200, 245):
            row = copy.deepcopy(self.new_link)
            row["id"] = str(identity)
            row["source"]["url"] = f"https://www.data.go.kr/data/{identity}/openapi.do"
            row["source"]["raw"]["api_id"] = str(identity)
            row["source"]["raw"]["meta_url"] = row["source"]["url"]
            rows.append(row)
        self.baseline_path.write_text("[]", encoding="utf-8")
        self.candidate_path.write_text(json.dumps(rows), encoding="utf-8")
        first_calls = []
        code, first = self.invoke(
            fetcher=lambda url, timeout: (first_calls.append(url) or self.successful_fetch(url, timeout)),
            **{"--max-attempts": 24, "--max-queue": 48, "--retries-per-detail": 0},
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(first_calls), 24)
        self.assertEqual(first["attempts_this_invocation"] if "attempts_this_invocation" in first else first["outcome"]["attempts_this_invocation"], 24)
        self.assertEqual(first["detail_queue_cursor"], 24)

        resume_dir = self.root / "large-queue-resume"
        resume_dir.mkdir()
        resume = resume_dir / "upstream-catalogue-enrichment-evidence.json"
        resume.write_bytes((self.output_dir / resume.name).read_bytes())
        for row in rows:
            row["source"]["raw"]["request_cnt"] = 8
        self.candidate_path.write_text(json.dumps(rows), encoding="utf-8")
        self.write_observation("2026-10-02T10:00:00Z")
        self.now = "2026-10-02T10:00:01Z"
        second_calls = []
        code, second = self.invoke(
            run_id="102", fetcher=lambda url, timeout: (second_calls.append(url) or self.successful_fetch(url, timeout)),
            **{
                "--max-attempts": 24, "--max-queue": 48, "--retries-per-detail": 0,
                "--resume-enrichment-evidence": resume,
            },
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(second_calls), 21)
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual({row["api_key"]["id"] for row in evidence["records"]}, {str(value) for value in range(200, 245)})
        self.assertEqual(second["attempts_this_invocation"] if "attempts_this_invocation" in second else second["outcome"]["attempts_this_invocation"], 21)

    def test_stale_failed_detail_epoch_reopens_only_on_new_source_observation(self) -> None:
        self.baseline_path.write_text("[]", encoding="utf-8")
        self.candidate_path.write_text(json.dumps([self.new_link]), encoding="utf-8")
        calls = []
        def fail(url: str, _timeout: float) -> str:
            calls.append(url)
            raise TimeoutError("offline")
        code, first = self.invoke(fetcher=fail, **{"--max-attempts": 1, "--retries-per-detail": 0})
        self.assertEqual(code, 2)
        self.assertEqual(calls, ["https://www.data.go.kr/data/2/openapi.do"])
        self.assertEqual(first["attempts_by_id"], {"2": 1})

        self.write_observation("2026-10-08T10:00:00Z")
        self.now = "2026-10-08T10:00:01Z"
        calls.clear()
        code, second = self.invoke(run_id="102", fetcher=self.successful_fetch, **{
            "--max-attempts": 1, "--retries-per-detail": 0,
        })
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertEqual(second["attempts_by_id"], {"2": 1})

        self.write_observation("2026-10-23T10:00:00Z")
        self.now = "2026-10-23T10:00:01Z"
        calls.clear()
        code, third = self.invoke(run_id="103", fetcher=lambda url, timeout: (calls.append(url) or self.successful_fetch(url, timeout)), **{
            "--max-attempts": 1, "--retries-per-detail": 0,
        })
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["https://www.data.go.kr/data/2/openapi.do"])
        self.assertEqual(third["attempts_by_id"], {})
        self.assertEqual(third["attempts_consumed"], 2)

    def test_successful_detail_can_refresh_in_four_ttl_epochs_without_retry_cap_lockout(self) -> None:
        self.baseline_path.write_text("[]", encoding="utf-8")
        self.candidate_path.write_text(json.dumps([self.new_link]), encoding="utf-8")
        first_day = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
        total_calls = []
        current_resume = None
        checkpoints = []
        for sequence in range(4):
            observed = first_day + timedelta(days=22 * sequence)
            observed_text = observed.isoformat().replace("+00:00", "Z")
            run_id = str(101 + sequence)
            self.write_observation(observed_text)
            self.now = (observed + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            options = {"--max-attempts": 1, "--retries-per-detail": 2}
            if current_resume is not None:
                options["--resume-enrichment-evidence"] = current_resume
            code, checkpoint = self.invoke(
                run_id=run_id,
                fetcher=lambda url, timeout: (total_calls.append(url) or self.successful_fetch(url, timeout)),
                **options,
            )
            self.assertEqual(code, 0, checkpoint.get("outcome"))
            checkpoints.append(checkpoint)
            resume_dir = self.root / f"ttl-resume-{sequence}"
            resume_dir.mkdir()
            current_resume = resume_dir / "upstream-catalogue-enrichment-evidence.json"
            current_resume.write_bytes((self.output_dir / current_resume.name).read_bytes())
        self.assertEqual(len(total_calls), 4)
        self.assertEqual(checkpoints[-1]["attempts_consumed"], 4)
        self.assertEqual(checkpoints[-1]["attempts_by_id"], {})

    def test_lease_conflict_does_not_mutate_owner_and_expired_owner_is_fenced(self) -> None:
        checkpoint = {"lease": None, "fencing_token": 0}
        now = MODULE.parse_timestamp("2026-10-01T10:00:00Z")
        self.assertTrue(MODULE.acquire_lease(checkpoint, "run-a", now)[0])
        owner_snapshot = copy.deepcopy(checkpoint)
        self.assertEqual(MODULE.acquire_lease(checkpoint, "run-b", now), (False, "lease_conflict"))
        self.assertEqual(checkpoint, owner_snapshot)
        old_token = checkpoint["fencing_token"]
        with self.assertRaisesRegex(ValueError, "stale_lease"):
            MODULE.ensure_fence(checkpoint, "run-b", old_token, now)
        reclaimed, _ = MODULE.acquire_lease(checkpoint, "run-b", now + MODULE.dt.timedelta(seconds=MODULE.LEASE_SECONDS + 1))
        self.assertTrue(reclaimed)
        self.assertGreater(checkpoint["fencing_token"], old_token)

    def test_corrupt_checkpoint_is_quarantined_without_rewriting_the_bad_record(self) -> None:
        _, first = self.invoke()
        path = self.checkpoint_path(first)
        broken = json.loads(path.read_text())
        broken["checkpoint_sha256"] = "0" * 64
        path.write_text(json.dumps(broken), encoding="utf-8")
        code, outcome = self.invoke(run_id="102")
        self.assertEqual(code, 3)
        self.assertEqual(outcome["status"], "quarantined")
        self.assertEqual(json.loads(path.read_text())["checkpoint_sha256"], "0" * 64)
        quarantine = self.state_dir / "quarantine" / f"{first['generation_id']}.json"
        self.assertTrue(quarantine.is_file())

    def test_expired_artifact_is_explicitly_quarantined(self) -> None:
        expired = "2026-09-30T00:00:00Z"
        code, checkpoint = self.invoke(**{"--artifact-expires-at": expired})
        self.assertEqual(code, 3)
        self.assertEqual(checkpoint["status"], "quarantined")
        self.assertEqual(checkpoint["outcome"]["reason"], "input_expired")

    def test_existing_generation_can_be_closed_without_restoring_expired_input(self) -> None:
        def fail(_url: str, _timeout: float) -> str:
            raise TimeoutError("detail temporarily unavailable")

        code, original = self.invoke(
            fetcher=fail,
            **{"--artifact-expires-at": "2026-10-01T10:00:10Z", "--max-attempts": 1},
        )
        self.assertEqual(code, 2)
        self.assertEqual(original["status"], "retry")
        checkpoint_path = self.checkpoint_path(original)
        prior_observation = copy.deepcopy(original["last_observation"])
        prior_fencing_token = original["fencing_token"]
        durable_before = checkpoint_path.read_bytes()

        args = self.args(
            run_id="102",
            **{
                "--producer-run-id": "101",
                "--target-generation-id": original["generation_id"],
                "--input-error": "input_expired",
            },
        )
        args.candidate = args.diff = args.refresh_evidence = args.resume_enrichment_evidence = None
        with self.assertRaisesRegex(ValueError, "not_expired"):
            MODULE.process(args, fetcher=lambda *_args: self.fail("expired-input marker must not fetch"))
        self.assertEqual(checkpoint_path.read_bytes(), durable_before)

        self.now = "2026-10-01T10:00:11Z"
        args = self.args(
            run_id="102",
            **{
                "--producer-run-id": "101",
                "--target-generation-id": original["generation_id"],
                "--input-error": "input_expired",
            },
        )
        args.candidate = args.diff = args.refresh_evidence = args.resume_enrichment_evidence = None
        code, closed = MODULE.process(args, fetcher=lambda *_args: self.fail("expired-input marker must not fetch"))

        schema = json.loads((ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json").read_text())
        self.assertEqual(code, 3)
        self.assertEqual(closed["generation_id"], original["generation_id"])
        self.assertEqual(closed["status"], "quarantined")
        self.assertEqual(closed["outcome"]["reason"], "input_expired")
        self.assertEqual(closed["last_observation"], prior_observation)
        self.assertEqual(closed["fencing_token"], prior_fencing_token + 1)
        self.assertIsNone(closed["lease"])
        self.assertIsNone(closed["request_reservation"])
        result = json.loads((self.output_dir / "upstream-catalogue-processing-result.json").read_text())
        self.assertFalse(result["candidate_available"])
        self.assertFalse(result["processing_replay"])
        self.assertEqual(result["source_id"], "data_go_kr")
        self.assertEqual(result["producer_run_id"], "101")
        self.assertEqual(result["processor_run_id"], "102")
        self.assertIs(MODULE.verify_checkpoint(closed, schema), closed)
        index = json.loads((self.state_dir / "sources/data_go_kr/index.json").read_text())
        indexed = next(row for row in index["generations"] if row["generation_id"] == original["generation_id"])
        self.assertEqual(indexed["status"], "quarantined")

    def test_new_collector_missing_artifact_is_distinct_from_existing_generation_marker(self) -> None:
        args = self.args(run_id="103", **{"--input-error": "artifact_missing"})
        args.candidate = args.diff = args.refresh_evidence = None
        code, checkpoint = MODULE.process(args, fetcher=lambda *_args: self.fail("missing collector artifact must not fetch"))

        self.assertEqual(code, 3)
        self.assertEqual(checkpoint["status"], "quarantined")
        self.assertEqual(checkpoint["outcome"]["reason"], "artifact_missing")
        self.assertIsNone(checkpoint["generation_inputs"]["candidate_sha256"])
        result = self.output_dir / "upstream-catalogue-processing-result.json"
        self.assertTrue(result.is_file())
        result_value = json.loads(result.read_text())
        self.assertEqual(result_value["generation_id"], checkpoint["generation_id"])
        self.assertFalse(result_value["candidate_available"])
        self.assertFalse(result_value["processing_replay"])
        self.assertEqual(result_value["source_id"], "data_go_kr")
        self.assertEqual(result_value["producer_run_id"], "103")
        self.assertEqual(result_value["processor_run_id"], "103")
        self.assertEqual(len(checkpoint["output_digests"]), 1)
        entry = checkpoint["output_digests"][0]
        self.assertEqual(entry["path"], result.name)
        self.assertEqual(entry["bytes"], result.stat().st_size)
        self.assertEqual(entry["sha256"], MODULE.file_sha256(result))
        self.assertEqual(
            checkpoint["output_artifact"]["bundle_manifest_sha256"],
            MODULE.sha256_bytes(MODULE.canonical_json(checkpoint["output_digests"])),
        )
        self.assertTrue((self.output_dir / "upstream-catalogue-checkpoint-receipt.json").is_file())

    def test_duplicate_ids_and_changed_guides_are_not_silently_skipped(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate id"):
            MODULE.unique_rows([self.new_link, self.new_link], "candidate")
        changed = copy.deepcopy(self.old_link)
        changed["source"]["raw"]["guide_url"] = "https://www.data.go.kr/guide/changed.pdf"
        queued, retained, _ = MODULE.detail_queue([self.old_link], [changed])
        self.assertEqual(queued[0]["reason"], "changed_link_source")
        self.assertEqual(retained, [])

    def test_missing_original_guide_can_be_enriched_with_exact_page_observation(self) -> None:
        code, checkpoint = self.invoke()
        self.assertEqual(code, 0)
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        record = next(row for row in evidence["records"] if row["api_key"]["id"] == "2")
        self.assertIsNone(record["guide_sha256"])
        self.assertEqual(record["source_provenance"]["page_url"], "https://www.data.go.kr/data/2/openapi.do")
        self.assertEqual(record["source_provenance"]["effective_url"], record["source_provenance"]["page_url"])
        self.assertEqual(checkpoint["detail_records"][-1]["status"], "enriched")

    def test_observed_guide_url_is_separately_bound_from_original_guide(self) -> None:
        page = '<a href="https://www.data.go.kr/guide/2.pdf">사용자 가이드</a><a href="https://api.example.gov/detail" onclick="fn_LinkApiRequest()">API</a>'
        self.new_link["source"]["raw"].pop("guide_url", None)
        self.candidate_path.write_text(json.dumps([self.old_link, self.new_link]), encoding="utf-8")
        self.baseline_path.write_text(json.dumps([self.old_link]), encoding="utf-8")
        code, _ = self.invoke(fetcher=lambda _url, _timeout: page)
        self.assertEqual(code, 0)
        evidence = json.loads((self.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        record = next(row for row in evidence["records"] if row["api_key"]["id"] == "2")
        expected = "https://www.data.go.kr/guide/2.pdf"
        self.assertIsNone(record["guide_sha256"])
        self.assertEqual(record["observed_guide_url"], expected)
        self.assertEqual(record["observed_guide_url_sha256"], MODULE.sha256_bytes(expected.encode("utf-8")))

    def test_detail_page_with_wrong_effective_url_and_digest_is_quarantined(self) -> None:
        wrong = MODULE.DetailPageObservation(
            body="<a href='https://api.example.gov/detail' onclick='fn_LinkApiRequest()'>API</a>",
            page_url="https://www.data.go.kr/data/2/openapi.do",
            effective_url="https://www.data.go.kr/data/3/openapi.do",
            page_sha256="0" * 64,
            observed_at="2026-10-01T10:00:00Z",
        )
        args = self.args(**{"--execution-mode": "fixture"})
        code, checkpoint = MODULE.process(args, fetcher=lambda _url, _timeout: wrong, sleeper=lambda _delay: None)
        self.assertEqual(code, 2)
        self.assertEqual(checkpoint["detail_records"][-1]["status"], "quarantined")
        self.assertEqual(checkpoint["outcome"]["composer_status"], "no_safe_change")

    def test_live_mode_rejects_fixture_composer_and_injected_test_clock(self) -> None:
        args = self.args(**{"--execution-mode": "live"})
        with self.assertRaisesRegex(ValueError, "fixture_composer_forbidden_in_live_mode"):
            MODULE.process(args)
        args.fixture_composer = None
        with self.assertRaisesRegex(ValueError, "injected_test_clock_forbidden_in_live_mode"):
            MODULE.process(args)

    def test_live_mode_requires_distinct_processor_and_artifact_run_identity(self) -> None:
        args = self.args(**{
            "--execution-mode": "live", "--processor-run-id": "101",
            "--processor-artifact-run-id": "5001",
        })
        args.now = None
        args.fixture_composer = None
        args.allow_fixture_composer = False
        with self.assertRaisesRegex(ValueError, "processor_run_id_invalid"):
            MODULE.process(args)

    def test_redirect_secret_url_and_response_byte_cap_are_rejected(self) -> None:
        handler = MODULE.SameHostRedirectHandler()
        request = urllib.request.Request("https://www.data.go.kr/data/2/openapi.do")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://attacker.example/collect?token=secret")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://www.data.go.kr/data/3/openapi.do")
        self.assertFalse(MODULE.safe_operation({"endpoint": "https://api.example.gov/x?serviceKey=secret"}))
        too_large = FakeResponse(content_length=str(MODULE.MAX_DETAIL_BYTES + 1))
        with mock.patch.object(MODULE.urllib.request, "build_opener", return_value=FakeOpener(too_large)):
            with self.assertRaisesRegex(ValueError, "too_large"):
                MODULE.fetch_public_detail("https://www.data.go.kr/data/2/openapi.do")

    def test_partial_composer_outputs_never_become_ready(self) -> None:
        self.composer_path.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        code, checkpoint = self.invoke()
        self.assertEqual(code, 3)
        self.assertEqual(checkpoint["status"], "quarantined")
        self.assertIn("composer_output_missing", checkpoint["outcome"]["reason"])


if __name__ == "__main__":
    unittest.main()
