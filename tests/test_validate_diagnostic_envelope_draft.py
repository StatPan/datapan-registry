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
        approval.pop("observed_at")
        self.assert_schema_rejects(value)
        value = copy.deepcopy(self.fixtures["approval-propagating"])
        approval = next(item for item in value["evidence_refs"] if item["kind"] == "approval_record")
        approval["authority"] = "datapan_cli"
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
