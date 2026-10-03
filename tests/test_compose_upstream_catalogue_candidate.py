from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/compose-upstream-catalogue-candidate.py"
SPEC = importlib.util.spec_from_file_location("catalogue_composer", SCRIPT)
composer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(composer)


def provider_index() -> dict:
    return {
        "adapters": [
            {"name": "link-fixture", "hosts": ["link.example.gov", "safety.example.gov", "www.nfqs.go.kr"], "status": "registered", "capabilities": ["verification"]}
        ]
    }


def rest_api(identifier: str, *, title: str | None = None, request_cnt: str = "1", seq: str = "op-1") -> dict:
    source_url = f"https://www.data.go.kr/tcs/dss/selectApiDataDetailView.do?publicDataPk={identifier}"
    return {
        "id": identifier,
        "title": title or f"API {identifier}",
        "provider": "data.go.kr",
        "priority": "P2",
        "source_keywords": ["first", "second"],
        "operations": [{
            "name": "lookup",
            "endpoint": "https://apis.data.go.kr/example/service",
            "default_params": {"pageNo": "1"},
            "request_params": [{"name": "serviceKey", "label": "Key"}],
            "response_params": [{"name": "items"}],
            "source": {"system": "data.go.kr", "url": source_url, "raw": {"api_type": "REST", "operation_seq": seq}},
        }],
        "source": {"system": "data.go.kr", "url": source_url, "raw": {"api_type": "REST", "request_cnt": request_cnt}},
    }


def link_api(identifier: str, *, guide: str | None | bool = None, endpoint: str = "https://link.example.gov/v1/list", operations: bool = True) -> dict:
    source_url = f"https://www.data.go.kr/tcs/dss/selectApiDataDetailView.do?publicDataPk={identifier}"
    guide_url = None if guide is False else (guide or f"https://www.data.go.kr/guide/{identifier}")
    value = {
        "id": identifier,
        "title": f"LINK {identifier}",
        "provider": "data.go.kr",
        "priority": "P2",
        "operations": [],
        "source": {"system": "data.go.kr", "url": source_url, "raw": {
            "api_type": "LINK", "meta_url": source_url,
        }},
    }
    if guide_url is not None:
        value["source"]["raw"]["guide_url"] = guide_url
    if operations:
        operation_raw = {"api_type": "LINK", "operation_url": endpoint, "meta_url": source_url}
        if guide_url is not None:
            operation_raw["guide_url"] = guide_url
        value["operations"] = [{
            "name": "download",
            "endpoint": endpoint,
            "request_params": [],
            "response_params": [],
            "source": {"system": "data.go.kr", "url": source_url, "raw": operation_raw},
        }]
    return value


def compose(baseline: list[dict], candidate: list[dict], **kwargs) -> dict:
    index = kwargs.pop("provider_index", provider_index())
    return composer.compose_registries(
        baseline, candidate, index,
        baseline_sha256="a" * 64, candidate_sha256="b" * 64,
        provider_index_sha256="c" * 64, **kwargs,
    )


def worker_outcome(candidate_row: dict, status: str) -> dict:
    return {
        "api_key": {"provider": "data.go.kr", "id": candidate_row["id"]},
        "status": status,
        "source_sha256": composer.source_fingerprint(candidate_row),
        "guide_sha256": composer.guide_fingerprint(candidate_row),
    }


def worker_enrichment(outcomes: list[dict], records: list[dict] | None = None) -> dict:
    return {
        "schema_version": "datapan.catalogue-enrichment-evidence.v1",
        "original_candidate_sha256": "b" * 64,
        "provider_index_sha256": "c" * 64,
        "adapter_revision": "c" * 64,
        "extractor_revision": "d" * 64,
        "records": records or [],
        "worker_outcomes": outcomes,
    }


def enrichment_evidence(candidate_row: dict, operations: list[dict], *, candidate_sha: str = "b" * 64) -> dict:
    page_url = composer.canonical_detail_page_url(candidate_row)
    evidence_operations = copy.deepcopy(operations)
    for operation in evidence_operations:
        operation["source"]["url"] = page_url
    observed_guide = next((
        operation.get("source", {}).get("raw", {}).get("guide_url")
        for operation in evidence_operations
        if operation.get("source", {}).get("raw", {}).get("guide_url")
    ), None)
    return {
        "schema_version": "datapan.catalogue-enrichment-evidence.v1",
        "original_candidate_sha256": candidate_sha,
        "provider_index_sha256": "c" * 64,
        "adapter_revision": "c" * 64,
        "extractor_revision": "d" * 64,
        "records": [{
            "api_key": {"provider": "data.go.kr", "id": candidate_row["id"]},
            "source_sha256": composer.source_fingerprint(candidate_row),
            "guide_sha256": composer.guide_fingerprint(candidate_row),
            "observed_guide_url": observed_guide,
            "observed_guide_url_sha256": hashlib.sha256(observed_guide.encode("utf-8")).hexdigest() if observed_guide else None,
            "operations": evidence_operations,
            "operations_sha256": composer.operations_digest(evidence_operations),
            "status": "enriched",
            "source_provenance": {
                "system": "data.go.kr", "page_url": page_url, "effective_url": page_url,
                "page_sha256": hashlib.sha256(("detail-page:" + candidate_row["id"]).encode()).hexdigest(),
                "observed_at": "2026-10-01T00:00:00Z",
            },
        }],
    }


class CatalogueCompositionTests(unittest.TestCase):
    def test_actual_failed_run_worker_quarantines_retain_baselines_and_allow_safe_partial_row(self):
        fixture_path = ROOT / "tests/fixtures/upstream_catalogue/failed-worker-scope-run-37091592758.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        baseline = fixture["baseline"]
        candidate = fixture["candidate"]
        outcomes = fixture["expected_worker_outcomes"]
        self.assertEqual([row["api_key"]["id"] for row in outcomes], ["15013677", "15019347", "15020786", "15020966"])
        legacy = compose(
            baseline, candidate,
            provider_index=provider_index(),
            enrichment_evidence=worker_enrichment([]),
        )
        legacy_by_id = {
            row["api_key"]["id"]: row
            for row in legacy["semantic_diff"]["api_decisions"]
        }
        for identity, expected_tags in fixture["expected_composer_findings"].items():
            self.assertEqual(legacy_by_id[identity]["disposition"], "accept_changed")
            self.assertEqual(legacy_by_id[identity]["tags"], expected_tags)

        safe_before = rest_api("safe-independent")
        safe_before["operations"][0]["endpoint"] = "https://link.example.gov/v1/safe"
        safe_before["operations"][0]["source"]["raw"]["operation_url"] = "https://link.example.gov/v1/safe"
        safe_after = copy.deepcopy(safe_before)
        safe_after["title"] = "Independently safe update"
        result = compose(
            [*baseline, safe_before], [*candidate, safe_after],
            provider_index=provider_index(),
            enrichment_evidence=worker_enrichment(outcomes),
        )
        self.assertEqual(result["composed_registry"], [*baseline, safe_after])
        self.assertEqual(result["ready_scope_registry"], [safe_after])
        self.assertEqual(
            [key["id"] for key in result["semantic_diff"]["quarantined_api_keys"]],
            ["15013677", "15019347", "15020786", "15020966"],
        )
        candidate_by_id = {row["id"]: row for row in candidate}
        baseline_by_id = {row["id"]: row for row in baseline}
        for decision in result["semantic_diff"]["api_decisions"][:4]:
            identity = decision["api_key"]["id"]
            self.assertEqual(decision["disposition"], "quarantine")
            self.assertIn("prior_runtime_evidence_stale", decision["tags"])
            self.assertIn("source_contract_change", decision["tags"])
            self.assertIn("volatile_request_cnt", decision["tags"])
            self.assertEqual(decision["baseline_record_sha256"], composer.digest_json(baseline_by_id[identity]))
            self.assertEqual(decision["candidate_record_sha256"], composer.digest_json(candidate_by_id[identity]))
            self.assertEqual(decision["findings"], ["worker_detail_quarantined"])
        self.assertEqual(result["status"], "ready_scoped")

    def test_retry_and_unqueued_outcomes_are_pending_and_new_rows_stay_excluded(self):
        baseline = link_api("retry-existing")
        candidate_existing = link_api("retry-existing", guide="https://www.data.go.kr/guide/new", operations=False)
        candidate_new = link_api("retry-new", operations=False)
        outcomes = [worker_outcome(candidate_existing, "retry"), worker_outcome(candidate_new, "retry")]
        result = compose(
            [baseline], [candidate_existing, candidate_new],
            enrichment_evidence=worker_enrichment(outcomes),
        )
        self.assertEqual(result["composed_registry"], [baseline])
        self.assertEqual(result["ready_scope_registry"], [])
        self.assertEqual(result["semantic_diff"]["applied_api_keys"], [])
        self.assertEqual(result["semantic_diff"]["retained_pending_api_keys"], [
            {"provider": "data.go.kr", "id": "retry-existing"},
            {"provider": "data.go.kr", "id": "retry-new"},
        ])
        decisions = {row["api_key"]["id"]: row for row in result["semantic_diff"]["api_decisions"]}
        self.assertEqual(decisions["retry-existing"]["disposition"], "retain_worker_pending")
        self.assertEqual(decisions["retry-new"]["disposition"], "retain_worker_pending")
        self.assertEqual(result["status"], "no_safe_change")

    def test_worker_outcome_identity_fingerprint_and_overlap_fail_closed(self):
        candidate = link_api("19000001", operations=False)
        outcome = worker_outcome(candidate, "quarantined")
        malformed = copy.deepcopy(outcome)
        malformed["source_sha256"] = "f" * 64
        with self.assertRaisesRegex(composer.CompositionError, "source binding mismatch"):
            compose([], [candidate], enrichment_evidence=worker_enrichment([malformed]))
        malformed_guide = copy.deepcopy(outcome)
        malformed_guide["guide_sha256"] = "e" * 64
        with self.assertRaisesRegex(composer.CompositionError, "guide binding mismatch"):
            compose([], [candidate], enrichment_evidence=worker_enrichment([malformed_guide]))
        with self.assertRaisesRegex(composer.CompositionError, "duplicate API identity"):
            compose([], [candidate], enrichment_evidence=worker_enrichment([outcome, outcome]))
        success = enrichment_evidence(candidate, link_api("19000001")["operations"])["records"]
        with self.assertRaisesRegex(composer.CompositionError, "overlaps successful enrichment"):
            compose([], [candidate], enrichment_evidence=worker_enrichment([outcome], success))

    def test_real_link_empty_import_retains_baseline_contract_operations(self):
        fixture_path = ROOT / "tests/fixtures/catalogue-composition-real-link-cases.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(fixture["schema_version"], "datapan.real-link-contract-projection.v1")
        self.assertEqual(
            fixture["provenance"]["baseline_artifact"]["payload_sha256"],
            "eeda72ee8590f458de8d75703662578e80edf3e61282f0e5e67547c4f6e5f644",
        )
        self.assertEqual(
            fixture["provenance"]["upstream_candidate_artifact"]["payload_sha256"],
            "90bc22e0ad61dd3672b09b7c52e4898be5afb535f87a8d7fa9e5e5c983e6e3b5",
        )
        self.assertFalse(fixture["provenance"]["projection"]["is_byte_exact_source_record"])

        baseline = [sample["baseline"] for sample in fixture["records"]]
        candidate = [sample["upstream_candidate"] for sample in fixture["records"]]
        for sample, before, after in zip(fixture["records"], baseline, candidate, strict=True):
            self.assertEqual(before["id"], sample["api_key"]["id"])
            self.assertEqual(after["id"], sample["api_key"]["id"])
            self.assertEqual(composer.digest_json(before), sample["projection_sha256"]["baseline"])
            self.assertEqual(composer.digest_json(after), sample["projection_sha256"]["upstream_candidate"])
            self.assertEqual(composer.source_fingerprint(before), composer.source_fingerprint(after))
            self.assertEqual(after["operations"], [])
            self.assertEqual(len(before["operations"]), sample["expected_baseline_operation_count"])
            identity_hashes = sorted(
                hashlib.sha256(composer.operation_identity(before, operation).encode("utf-8")).hexdigest()
                for operation in before["operations"]
            )
            self.assertEqual(identity_hashes, sample["expected_baseline_operation_identity_sha256"])
            self.assertNotEqual(before["source"]["raw"]["request_cnt"], after["source"]["raw"]["request_cnt"])

        result = compose(baseline, candidate)
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["ready_scope_registry"], [])
        self.assertEqual(result["composed_registry"], baseline)

    def test_real_link_request_count_only_drift_is_volatile(self):
        fixture_path = ROOT / "tests/fixtures/catalogue-composition-real-link-cases.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        baseline = [sample["baseline"] for sample in fixture["records"]]
        candidate = copy.deepcopy(baseline)
        for row in candidate:
            raw = row["source"]["raw"]
            raw["request_cnt"] = int(raw["request_cnt"]) + 1

        result = compose(baseline, candidate)
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["ready_scope_registry"], [])
        self.assertEqual(result["composed_registry"], baseline)
        decisions = {entry["api_key"]["id"]: entry for entry in result["semantic_diff"]["api_decisions"]}
        self.assertEqual(set(decisions), {row["id"] for row in baseline})
        for row in baseline:
            self.assertIn("volatile_request_cnt", decisions[row["id"]]["tags"])

    def test_unchanged_link_candidate_preserves_baseline_enrichment(self):
        baseline = link_api("link-1")
        candidate = link_api("link-1", operations=False)
        result = compose([baseline], [candidate])
        self.assertEqual(result["composed_registry"], [baseline])
        self.assertEqual(result["semantic_diff"]["api_decisions"][0]["disposition"], "unchanged")
        self.assertEqual(result["status"], "no_change")

    def test_link_request_count_only_change_does_not_create_weekly_delta(self):
        baseline = link_api("link-count")
        baseline["source"]["raw"]["request_cnt"] = "1"
        candidate = link_api("link-count", operations=False)
        candidate["source"]["raw"]["request_cnt"] = "2"
        result = compose([baseline], [candidate])
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["ready_scope_registry"], [])
        self.assertEqual(result["composed_registry"], [baseline])

    def test_changed_guide_without_current_detail_quarantines_whole_old_row(self):
        baseline = link_api("link-2")
        candidate = link_api("link-2", guide="https://www.data.go.kr/guide/changed", operations=False)
        result = compose([baseline], [candidate])
        self.assertEqual(result["composed_registry"], [baseline])
        self.assertEqual(result["semantic_diff"]["api_decisions"][0]["disposition"], "quarantine")
        self.assertIn("prior_runtime_evidence_stale", result["semantic_diff"]["api_decisions"][0]["tags"])

    def test_changed_guide_admits_only_bound_enrichment(self):
        baseline = link_api("1003")
        candidate = link_api("1003", guide="https://www.data.go.kr/guide/changed", operations=False)
        enriched = link_api("1003", guide="https://www.data.go.kr/guide/changed")["operations"]
        evidence = enrichment_evidence(candidate, enriched)
        result = compose([baseline], [candidate], enrichment_evidence=evidence)
        self.assertEqual(result["status"], "ready_scoped")
        self.assertEqual(result["composed_registry"][0]["operations"], evidence["records"][0]["operations"])
        self.assertIn("prior_runtime_evidence_stale", result["semantic_diff"]["api_decisions"][0]["tags"])
        decision = result["semantic_diff"]["api_decisions"][0]
        self.assertEqual(decision["enrichment_page_sha256"], evidence["records"][0]["source_provenance"]["page_sha256"])
        self.assertEqual(decision["observed_guide_url_sha256"], evidence["records"][0]["observed_guide_url_sha256"])

    def test_changed_guide_rejects_mismatched_enrichment_binding_locally(self):
        baseline = link_api("1004")
        candidate = link_api("1004", guide="https://www.data.go.kr/guide/changed", operations=False)
        evidence = enrichment_evidence(candidate, link_api("1004")["operations"])
        evidence["records"][0]["guide_sha256"] = "e" * 64
        result = compose([baseline], [candidate], enrichment_evidence=evidence)
        self.assertEqual(result["composed_registry"], [baseline])
        self.assertEqual(result["semantic_diff"]["api_decisions"][0]["disposition"], "quarantine")

    def test_enrichment_row_must_still_satisfy_specs_schema(self):
        candidate = link_api("1005", operations=False)
        operations = link_api("1005")["operations"]
        operations[0]["unrecognized"] = True
        evidence = enrichment_evidence(candidate, operations)
        result = compose([], [candidate], enrichment_evidence=evidence)
        self.assertEqual(result["composed_registry"], [])
        self.assertEqual(result["semantic_diff"]["api_decisions"][0]["findings"], ["enrichment_specs_schema_invalid"])

    def test_new_gateway_is_admitted_independently_of_pending_link(self):
        baseline = [rest_api("old")]
        candidate = [rest_api("old"), rest_api("new"), link_api("new-link", operations=False)]
        result = compose(baseline, candidate)
        self.assertEqual(result["status"], "ready_scoped")
        self.assertEqual([row["id"] for row in result["ready_scope_registry"]], ["new"])
        self.assertEqual(result["semantic_diff"]["summary"]["evidence"]["quarantined"], 1)
        self.assertEqual(result["semantic_diff"]["quarantined_api_keys"], [{"provider": "data.go.kr", "id": "new-link"}])

    def test_absent_record_is_retained_pending_and_not_called_quarantine(self):
        baseline = [rest_api("removed")]
        result = compose(baseline, [])
        self.assertEqual(result["composed_registry"], baseline)
        self.assertEqual(result["status"], "no_safe_change")
        self.assertEqual(result["semantic_diff"]["retained_pending_api_keys"], [{"provider": "data.go.kr", "id": "removed"}])
        self.assertEqual(result["semantic_diff"]["quarantined_api_keys"], [])
        self.assertEqual(result["regeneration_queue"]["items"][0]["required_evidence"], ["authoritative_deletion_evidence"])

    def test_request_count_and_set_order_are_non_contract_observations(self):
        baseline = rest_api("stable", request_cnt="1")
        candidate = copy.deepcopy(baseline)
        candidate["source"]["raw"]["request_cnt"] = "2"
        candidate["source_keywords"] = list(reversed(candidate["source_keywords"]))
        result = compose([baseline], [candidate])
        decision = result["semantic_diff"]["api_decisions"][0]
        self.assertEqual(decision["disposition"], "unchanged")
        self.assertIn("volatile_request_cnt", decision["tags"])
        self.assertIn("ordering_only", decision["tags"])
        self.assertNotIn("source_contract_change", decision["tags"])
        self.assertNotIn("prior_runtime_evidence_stale", decision["tags"])
        self.assertEqual(result["ready_scope_registry"], [])
        self.assertEqual(result["composed_registry"], [baseline])

    def test_accepted_link_enrichment_hashes_do_not_replace_original_candidate_hash(self):
        baseline = link_api("1006")
        candidate = link_api("1006", guide="https://www.data.go.kr/guide/current", operations=False)
        evidence_ops = link_api("1006", guide="https://www.data.go.kr/guide/current")["operations"]
        evidence = enrichment_evidence(candidate, evidence_ops)
        result = compose([baseline], [candidate], enrichment_evidence=evidence)
        decision = result["semantic_diff"]["api_decisions"][0]
        self.assertEqual(decision["candidate_record_sha256"], composer.digest_json(candidate))
        enriched = composer.apply_enrichment_record(candidate, evidence["records"][0])
        self.assertEqual(decision["enriched_candidate_record_sha256"], composer.digest_json(enriched))
        self.assertEqual(decision["enrichment_operations_sha256"], composer.operations_digest(evidence["records"][0]["operations"]))

    def test_missing_candidate_guide_accepts_bound_detail_page_evidence(self):
        candidate = link_api("1010", guide=False, operations=False)
        evidence_ops = link_api("1010", guide=False)["operations"]
        evidence = enrichment_evidence(candidate, evidence_ops)
        result = compose([], [candidate], enrichment_evidence=evidence)
        self.assertEqual(result["status"], "ready_scoped")
        self.assertEqual(result["composed_registry"][0]["operations"], evidence["records"][0]["operations"])
        self.assertNotIn("guide_url", result["composed_registry"][0]["source"]["raw"])
        self.assertIsNone(evidence["records"][0]["guide_sha256"])
        self.assertEqual(result["semantic_diff"]["api_decisions"][0]["candidate_record_sha256"], composer.digest_json(candidate))
        replay = compose(result["composed_registry"], [candidate], enrichment_evidence=evidence)
        self.assertEqual(replay["status"], "no_change")
        self.assertEqual(replay["composed_registry"], result["composed_registry"])

    def test_detail_evidence_rejects_redirect_away_from_canonical_page(self):
        candidate = link_api("1011", operations=False)
        evidence = enrichment_evidence(candidate, link_api("1011")["operations"])
        evidence["records"][0]["source_provenance"]["effective_url"] = "https://other.example/data/1011/openapi.do"
        result = compose([], [candidate], enrichment_evidence=evidence)
        self.assertEqual(result["composed_registry"], [])
        self.assertEqual(result["semantic_diff"]["api_decisions"][0]["disposition"], "quarantine")
        self.assertIn("enrichment_detail_source_binding_mismatch", result["semantic_diff"]["api_decisions"][0]["findings"])

    def test_enrichment_evidence_schema_accepts_nullable_original_guide_and_page_receipt(self):
        candidate = link_api("1013", guide=False, operations=False)
        evidence = enrichment_evidence(candidate, link_api("1013", guide=False)["operations"])
        schema = composer.load_json(composer.ENRICHMENT_EVIDENCE_SCHEMA)
        composer.jsonschema.Draft202012Validator(schema, format_checker=composer.jsonschema.FormatChecker()).validate(evidence)
        composer.jsonschema.Draft202012Validator(schema, format_checker=composer.jsonschema.FormatChecker()).validate(
            worker_enrichment([worker_outcome(candidate, "retry")])
        )

    def test_enriched_detail_source_url_survives_later_cycle_and_refresh(self):
        baseline = link_api("1012", guide="https://www.data.go.kr/guide/v1")
        candidate_v1 = link_api("1012", guide="https://www.data.go.kr/guide/v2", operations=False)
        operations_v1 = link_api("1012", guide="https://www.data.go.kr/guide/v2")["operations"]
        evidence_v1 = enrichment_evidence(candidate_v1, operations_v1)
        first = compose([baseline], [candidate_v1], enrichment_evidence=evidence_v1)
        self.assertEqual(first["status"], "ready_scoped")
        self.assertEqual(first["composed_registry"][0]["operations"][0]["source"]["url"], composer.canonical_detail_page_url(candidate_v1))

        second = compose(first["composed_registry"], [candidate_v1])
        self.assertEqual(second["status"], "no_change")
        self.assertEqual(second["composed_registry"], first["composed_registry"])

        candidate_v2 = link_api("1012", guide="https://www.data.go.kr/guide/v3", operations=False)
        operations_v2 = link_api("1012", guide="https://www.data.go.kr/guide/v3")["operations"]
        operations_v2[0]["name"] = "download v2"
        evidence_v2 = enrichment_evidence(candidate_v2, operations_v2)
        third = compose(second["composed_registry"], [candidate_v2], enrichment_evidence=evidence_v2)
        self.assertEqual(third["status"], "ready_scoped")
        self.assertEqual(third["composed_registry"][0]["operations"][0]["name"], "download v2")

    def test_default_parameter_change_is_contract_drift(self):
        baseline = rest_api("defaults")
        candidate = copy.deepcopy(baseline)
        candidate["operations"][0]["default_params"]["pageNo"] = "2"
        result = compose([baseline], [candidate])
        decision = result["semantic_diff"]["api_decisions"][0]
        self.assertEqual(decision["disposition"], "accept_changed")
        self.assertIn("operation_contract_change", decision["tags"])

    def test_rest_operation_removal_without_proof_retains_whole_baseline_row(self):
        baseline = rest_api("ops", seq="op-1")
        second = copy.deepcopy(baseline["operations"][0])
        second["name"] = "second"
        second["source"]["raw"]["operation_seq"] = "op-2"
        baseline["operations"].append(second)
        candidate = copy.deepcopy(baseline)
        candidate["operations"] = [candidate["operations"][0]]
        result = compose([baseline], [candidate])
        self.assertEqual(result["status"], "no_safe_change")
        self.assertEqual(result["composed_registry"], [baseline])
        decision = result["semantic_diff"]["api_decisions"][0]
        self.assertEqual(decision["disposition"], "quarantine")
        self.assertEqual(decision["findings"], ["operation_removed_without_authoritative_evidence"])

    def test_enriched_link_does_not_remove_independently_sourced_safetydata_operation(self):
        baseline = link_api("1007")
        safety_operation = {
            "name": "safety detail",
            "endpoint": "https://safety.example.gov/v1/items",
            "request_params": [],
            "response_params": [],
            "source": {"system": "safetydata.go.kr", "url": "https://safety.example.gov/catalog", "raw": {"source_interface_id": "safe-1"}},
        }
        baseline["operations"].append(safety_operation)
        candidate = link_api("1007", guide="https://www.data.go.kr/guide/current", operations=False)
        enriched = link_api("1007", guide="https://www.data.go.kr/guide/current")["operations"]
        evidence = enrichment_evidence(candidate, enriched)
        result = compose([baseline], [candidate], enrichment_evidence=evidence)
        self.assertEqual(result["status"], "ready_scoped")
        output_ops = result["composed_registry"][0]["operations"]
        self.assertEqual(len(output_ops), 2)
        self.assertEqual(output_ops[1], safety_operation)

    def test_new_rest_with_ambiguous_parameter_identity_is_quarantined(self):
        candidate = rest_api("ambiguous")
        candidate["operations"][0]["request_params"] = [{"name": "page"}, {"name": "page"}]
        result = compose([], [candidate])
        self.assertEqual(result["status"], "no_safe_change")
        self.assertEqual(result["composed_registry"], [])
        self.assertEqual(result["semantic_diff"]["quarantined_api_keys"], [{"provider": "data.go.kr", "id": "ambiguous"}])

    def test_link_endpoint_replacement_requires_operation_deletion_evidence(self):
        baseline = link_api("1008", endpoint="https://link.example.gov/v1/list")
        candidate = link_api("1008", endpoint="https://link.example.gov/v2/list")
        evidence = enrichment_evidence(candidate, candidate["operations"])
        result = compose([baseline], [candidate], enrichment_evidence=evidence)
        self.assertEqual(result["composed_registry"], [baseline])
        self.assertEqual(result["semantic_diff"]["api_decisions"][0]["findings"], ["operation_removed_without_authoritative_evidence"])

    def test_http_to_https_link_route_is_explicit_stale_contract_change(self):
        baseline = link_api("scheme", endpoint="http://link.example.gov/v1/list")
        candidate = link_api("scheme", endpoint="https://link.example.gov/v1/list")
        candidate["source"]["raw"]["guide_url"] = baseline["source"]["raw"]["guide_url"]
        result = compose([baseline], [candidate])
        decision = result["semantic_diff"]["api_decisions"][0]
        self.assertEqual(decision["disposition"], "quarantine")
        self.assertIn("http_to_https_contract_change", decision["tags"])
        self.assertIn("prior_runtime_evidence_stale", decision["tags"])

    def test_duplicate_api_identity_fails_globally(self):
        with self.assertRaisesRegex(composer.CompositionError, "duplicate API identity"):
            compose([rest_api("duplicate"), rest_api("duplicate")], [])

    def test_candidate_digest_mismatch_fails_enrichment_envelope(self):
        candidate = link_api("1009", operations=False)
        evidence = enrichment_evidence(candidate, link_api("1009")["operations"], candidate_sha="f" * 64)
        with self.assertRaisesRegex(composer.CompositionError, "original_candidate_sha256"):
            compose([], [candidate], enrichment_evidence=evidence)

    def test_bundle_is_byte_deterministic_and_schema_valid(self):
        baseline = [rest_api("same")]
        candidate = [rest_api("same"), rest_api("new")]
        result = compose(baseline, candidate)
        digests = {
            name: {"bytes": 1, "sha256": hashlib.sha256(name.encode()).hexdigest()}
            for name in (
                "baseline", "candidate", "full_diff", "refresh_evidence", "provider_index", "source_policy",
                "registry_schema", "provider_index_schema", "diff_schema", "refresh_evidence_schema",
                "enrichment_evidence_schema", "composer", "receipt_schema",
            )
        }
        first = composer.build_bundle(result, input_digests=digests, run_id="123456", run_url="https://github.com/StatPan/datapan-registry/actions/runs/123456")
        second = composer.build_bundle(result, input_digests=digests, run_id="123456", run_url="https://github.com/StatPan/datapan-registry/actions/runs/123456")
        self.assertEqual(first, second)
        self.assertIn("composition-receipt.json", first)
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "bundle"
            self.assertEqual(composer.publish_bundle(output, first), "created")
            self.assertEqual(composer.publish_bundle(output, second), "replayed")

    def test_refresh_receipt_binds_diff_hash_without_requiring_unreported_byte_count(self):
        baseline = [rest_api("old")]
        candidate = [rest_api("old"), rest_api("new")]
        diff = {
            "generated_at": "2026-09-30T00:00:00Z",
            "provider": "data.go.kr",
            "old": "baseline.registry.json",
            "new": "candidate.registry.json",
            "limit": 0,
            "truncated": False,
            "counts": {"old": 1, "new": 2},
            "summary": {"added": 1, "removed": 0, "changed": 0, "stable": 1},
            "added": [{"id": "new", "title": "API new", "provider": "data.go.kr", "operations_count": 1}],
            "removed": [],
            "changed": [],
        }
        baseline_bytes = composer.stable_json_bytes(baseline)
        candidate_bytes = composer.stable_json_bytes(candidate)
        diff_bytes = composer.stable_json_bytes(diff)
        baseline_digest = {"bytes": len(baseline_bytes), "sha256": hashlib.sha256(baseline_bytes).hexdigest()}
        candidate_digest = {"bytes": len(candidate_bytes), "sha256": hashlib.sha256(candidate_bytes).hexdigest()}
        diff_digest = {"bytes": len(diff_bytes), "sha256": hashlib.sha256(diff_bytes).hexdigest()}
        evidence = {
            "schema_version": "datapan.upstream-refresh-evidence.v1",
            "source_id": "data_go_kr",
            "status": "material_change",
            "baseline": {**baseline_digest, "path": "baseline.registry.json"},
            "snapshot": {**candidate_digest, "path": "candidate.registry.json"},
            "diff": {"path": "diff.json", "sha256": diff_digest["sha256"], "summary": diff["summary"]},
            "collection": {"succeeded": True},
            "publication": {"automatic": False},
        }
        composer.verify_refresh_inputs(
            baseline, candidate, diff, evidence, baseline_digest, candidate_digest, diff_digest,
            "123456", "https://github.com/StatPan/datapan-registry/actions/runs/123456",
        )
        evidence["diff"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(composer.CompositionError, "diff input sha256"):
            composer.verify_refresh_inputs(
                baseline, candidate, diff, evidence, baseline_digest, candidate_digest, diff_digest,
                "123456", "https://github.com/StatPan/datapan-registry/actions/runs/123456",
            )


if __name__ == "__main__":
    unittest.main()
