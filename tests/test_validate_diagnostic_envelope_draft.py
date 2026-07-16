import copy
import importlib.util
import pathlib
import subprocess
import sys
import unittest

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "diagnostic_envelope", ROOT / "scripts/validate-diagnostic-envelope-draft.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class DiagnosticEnvelopeDraftTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = MODULE.load(MODULE.SCHEMA)
        cls.validator = jsonschema.Draft202012Validator(
            cls.schema, format_checker=jsonschema.FormatChecker()
        )
        cls.fixtures = {
            path.stem: MODULE.load(path) for path in sorted(MODULE.FIXTURES.glob("*.json"))
        }

    def assert_schema_rejects(self, value):
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(value)

    def test_checked_in_contract_and_fixtures(self):
        self.assertEqual(MODULE.validate_all(), {"fixtures": 11, "causes": 11})

    def test_cli_entrypoint(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/validate-diagnostic-envelope-draft.py")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixtures=11", result.stdout)

    def test_certainty_is_one_bounded_axis(self):
        value = copy.deepcopy(self.fixtures["provider-outage"])
        value["cause"]["confidence"] = "probable"
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["unknown"])
        value["cause"]["determination"] = "inferred"
        self.assert_schema_rejects(value)

    def test_approval_propagation_requires_authoritative_timed_scope(self):
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        approval = next(item for item in value["evidence_refs"] if item["kind"] == "approval_record")
        approval.pop("timing")
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        approval = next(item for item in value["evidence_refs"] if item["kind"] == "approval_record")
        approval["authority"] = "datapan_cli"
        self.assert_schema_rejects(value)

    def test_scope_is_structurally_bound_to_the_envelope_subject(self):
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        value["subject"].pop("operation_id")
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        approval = next(item for item in value["evidence_refs"] if item["kind"] == "approval_record")
        approval["scope"]["subject_ref"] = "another_subject"
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        approval = next(item for item in value["evidence_refs"] if item["kind"] == "approval_record")
        approval["approval"]["effective_scope"]["subject_ref"] = "another_subject"
        self.assert_schema_rejects(value)

    def test_supporting_evidence_is_current_bounded_and_explicit(self):
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        approval = next(item for item in value["evidence_refs"] if item["kind"] == "approval_record")
        approval["timing"]["validity"] = "immutable"
        approval["timing"].pop("remaining_validity_seconds")
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        approval = next(item for item in value["evidence_refs"] if item["kind"] == "approval_record")
        approval["timing"]["observed_age_seconds"] = 604801
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        approval = next(item for item in value["evidence_refs"] if item["kind"] == "approval_record")
        approval["timing"]["remaining_validity_seconds"] = 0
        self.assert_schema_rejects(value)
        for support in ("cause", "action", "determination"):
            with self.subTest(support=support):
                value = copy.deepcopy(self.fixtures["approval-propagating"])
                approval = next(item for item in value["evidence_refs"] if item["kind"] == "approval_record")
                approval["supports"].remove(support)
                self.assert_schema_rejects(value)

    def test_approval_propagation_requires_current_failure_symptom(self):
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        value["evidence_refs"] = [
            item for item in value["evidence_refs"] if item["kind"] != "provider_response"
        ]
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        response = next(item for item in value["evidence_refs"] if item["kind"] == "provider_response")
        response["response"]["http_status"] = 500
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        response = next(item for item in value["evidence_refs"] if item["kind"] == "provider_response")
        response["timing"]["remaining_validity_seconds"] = 0
        self.assert_schema_rejects(value)

    def test_credential_rejection_does_not_need_approval_state(self):
        value = self.fixtures["credential-invalid"]
        self.assertFalse(any(item["kind"] == "approval_record" for item in value["evidence_refs"]))
        self.assertEqual(value["evidence_refs"][0]["response"]["provider_class"], "credential_rejected")

    def test_invalid_input_requires_failed_typed_request_validation(self):
        value = copy.deepcopy(self.fixtures["invalid-input"])
        value["evidence_refs"][0].pop("request_validation")
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["invalid-input"])
        value["evidence_refs"][0]["request_validation"]["failure_class"] = "credential_configuration"
        self.assert_schema_rejects(value)

    def test_rate_limit_requires_typed_provider_class(self):
        value = copy.deepcopy(self.fixtures["rate-limited"])
        value["evidence_refs"][0]["response"]["provider_class"] = "unclassified"
        self.assert_schema_rejects(value)

    def test_provider_outage_requires_correlated_or_direct_outage_evidence(self):
        value = copy.deepcopy(self.fixtures["provider-outage"])
        value["evidence_refs"] = [value["evidence_refs"][0]]
        self.assert_schema_rejects(value)

    def test_provider_outage_key_advice_requires_credential_independent_evidence(self):
        value = copy.deepcopy(self.fixtures["provider-outage"])
        response = value["evidence_refs"][0]
        response["response"]["provider_class"] = "service_unavailable"
        value["evidence_refs"] = [response]
        self.assert_schema_rejects(value)
        value["actions"]["avoid"] = []
        response["supports"] = ["cause", "determination", "action"]
        self.validator.validate(value)

    def test_provider_outage_determination_and_correlation_are_bounded(self):
        value = copy.deepcopy(self.fixtures["provider-outage"])
        response = value["evidence_refs"][0]
        response["response"]["provider_class"] = "service_unavailable"
        response["supports"] = ["cause", "determination", "action"]
        value["evidence_refs"] = [response]
        value["actions"]["avoid"] = []
        value["cause"]["determination"] = "observed"
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["provider-outage"])
        value["cause"]["determination"] = "observed"
        self.validator.validate(value)
        value = copy.deepcopy(self.fixtures["provider-outage"])
        health = next(item for item in value["evidence_refs"] if item["kind"] == "health_observation")
        health["timing"]["observed_age_seconds"] = 901
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["provider-outage"])
        health = next(item for item in value["evidence_refs"] if item["kind"] == "health_observation")
        health["timing"]["remaining_validity_seconds"] = 901
        self.assert_schema_rejects(value)
        for kind in ("health_observation", "provider_notice", "provider_response"):
            with self.subTest(kind=kind, remaining_validity_seconds=0):
                value = copy.deepcopy(self.fixtures["provider-outage"])
                evidence = next(item for item in value["evidence_refs"] if item["kind"] == kind)
                evidence["timing"]["remaining_validity_seconds"] = 0
                self.assert_schema_rejects(value)

    def test_contract_and_quality_causes_require_failed_typed_assertions(self):
        value = copy.deepcopy(self.fixtures["contract-drift"])
        value["evidence_refs"][0]["contract_assertion"]["result"] = "match"
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["semantic-quality"])
        evidence = next(item for item in value["evidence_refs"] if item["kind"] == "data_quality_assertion")
        evidence["quality_assertion"]["result"] = "passed"
        self.assert_schema_rejects(value)

    def test_evidence_kind_restricts_authority(self):
        value = copy.deepcopy(self.fixtures["rate-limited"])
        value["evidence_refs"][0]["authority"] = "registry"
        self.assert_schema_rejects(value)

    def test_same_http_symptom_has_different_evidence_and_actions(self):
        cases = [self.fixtures[name] for name in ("approval-propagating", "credential-invalid", "provider-outage")]
        self.assertTrue(all(any(item["ref_id"] == "provider-response:http-401" for item in case["evidence_refs"]) for case in cases))
        self.assertEqual({case["cause"]["code"] for case in cases}, {"approval_propagating", "credential_invalid", "provider_outage"})
        self.assertEqual(len({case["actions"]["recommended"][0]["action_id"] for case in cases}), 3)

    def test_stale_data_requires_versioned_time_assertion(self):
        value = copy.deepcopy(self.fixtures["stale-data"])
        evidence = value["evidence_refs"][0]
        evidence["freshness"].pop("reference_time")
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["stale-data"])
        value["evidence_refs"][0]["freshness"]["state"] = "fresh"
        self.assert_schema_rejects(value)

    def test_ready_requires_operation_scoped_passed_validation(self):
        value = copy.deepcopy(self.fixtures["ready"])
        value["evidence_refs"][0]["validation"]["result"] = "unknown"
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["ready"])
        value["evidence_refs"][0]["scope"]["level"] = "source"
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["ready"])
        value["evidence_refs"][0]["validation"].pop("required_level")
        self.assert_schema_rejects(value)

    def test_ready_requires_achieved_level_to_meet_required_level(self):
        value = copy.deepcopy(self.fixtures["ready"])
        value["evidence_refs"][0]["validation"]["required_level"] = "L4"
        value["evidence_refs"][0]["validation"]["achieved_level"] = "L1"
        self.assert_schema_rejects(value)

    def test_cause_actions_are_exact_and_actor_bound(self):
        value = copy.deepcopy(self.fixtures["invalid-input"])
        value["actions"]["recommended"].append(
            {
                "action_id": "continue_to_reuse",
                "actor": "user",
                "rationale_id": "action.continue_to_reuse",
            }
        )
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["rate-limited"])
        value["actions"]["avoid"] = [
            {
                "action_id": "reissue_credential",
                "actor": "user",
                "rationale_id": "avoid.outage_not_fixed_by_key",
            }
        ]
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["invalid-input"])
        value["actions"]["recommended"][0]["actor"] = "datapan_cli"
        self.assert_schema_rejects(value)

    def test_evidence_kind_scope_levels_reject_source_scope(self):
        for fixture_name, fixture in self.fixtures.items():
            for index in range(len(fixture["evidence_refs"])):
                with self.subTest(fixture=fixture_name, evidence=index):
                    value = copy.deepcopy(fixture)
                    value["evidence_refs"][index]["scope"]["level"] = "source"
                    self.assert_schema_rejects(value)

    def test_evidence_payload_is_kind_exclusive(self):
        value = copy.deepcopy(self.fixtures["rate-limited"])
        value["evidence_refs"][0]["notice"] = {
            "state": "service_suspended",
            "notice_version": "notice-v1",
        }
        self.assert_schema_rejects(value)

    def test_provider_identity_is_a_bounded_identifier_not_free_text(self):
        for provider_id in (
            "upstream rejected the supplied credential",
            "serviceKey=example-secret",
            "https://provider.test/status",
        ):
            with self.subTest(provider_id=provider_id):
                value = copy.deepcopy(self.fixtures["unknown"])
                value["subject"]["provider_id"] = provider_id
                self.assert_schema_rejects(value)

    def test_all_identifier_surfaces_reject_urls_and_sensitive_labels(self):
        mutations = (
            ("ref_id", lambda value: value["evidence_refs"][0].__setitem__("ref_id", "https://provider.test/failure")),
            ("operation_id_url", lambda value: value["subject"].__setitem__("operation_id", "https://provider.test/op")),
            ("operation_id_sensitive", lambda value: value["subject"].__setitem__("operation_id", "operation:api-key:value")),
            ("operation_id_bearer_like", lambda value: value["subject"].__setitem__("operation_id", "operation:bearertoken")),
            ("version", lambda value: value["evidence_refs"][0].__setitem__("version", "api-key-value")),
            ("rationale_id", lambda value: value["actions"]["recommended"][0].__setitem__("rationale_id", "action.api-key.value")),
            ("explanation_id", lambda value: value["cause"].__setitem__("explanation_id", "https://provider.test/text")),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                value = copy.deepcopy(self.fixtures["invalid-input"])
                mutate(value)
                self.assert_schema_rejects(value)

    def test_consumers_need_only_the_portable_schema(self):
        contract = MODULE.load(MODULE.CONSUMER_CONTRACT)
        validation = contract["validation_contract"]
        self.assertEqual(validation["validator"], "json-schema-draft-2020-12")
        self.assertEqual(validation["single_required_artifact"], contract["envelope_schema"])
        self.assertFalse(validation["additional_semantic_validator_required"])
        self.assertTrue(validation["ready_level_order_encoded_in_schema"])

    def test_raw_provider_text_url_and_credentials_fail_closed(self):
        for key, content in (
            ("raw_provider_text", "provider said no"),
            ("raw_provider_url", "https://example.test/failure"),
            ("credential", "example-secret"),
        ):
            with self.subTest(key=key):
                value = copy.deepcopy(self.fixtures["unknown"])
                value[key] = content
                self.assert_schema_rejects(value)
                with self.assertRaisesRegex(ValueError, "forbidden"):
                    MODULE.reject_sensitive(value)

    def test_credential_like_text_and_urls_fail_closed(self):
        for text in ("serviceKey=example", "Authorization: Bearer example", "https://provider.test/status"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "forbidden raw or credential-like text"):
                    MODULE.reject_sensitive({"safe_name": text})

    def test_draft_is_not_release_indexed_or_manifest_bound(self):
        MODULE.validate_draft_boundary()


if __name__ == "__main__":
    unittest.main()
