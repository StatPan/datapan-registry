from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts/generate-operation-observation-plan-fixtures.py"
SCHEMA = ROOT / "schemas/datapan.operation-observation-plan.v1.schema.json"
FIXTURES = ROOT / "fixtures/operation-observation-plan"


def fixture_generator():
    spec = importlib.util.spec_from_file_location("operation_plan_fixtures", GENERATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load synthetic operation-plan fixture generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OperationObservationPlanFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.validator = jsonschema.Draft202012Validator(cls.schema, format_checker=jsonschema.FormatChecker())
        cls.generated = fixture_generator().build_records()

    def test_rest_and_soap_fixtures_validate_and_match_generation(self):
        generated = {item["operation_identity"]["operation_id"]: item for item in self.generated}
        self.assertEqual(set(generated), {"synthetic-rest-list", "synthetic-soap-read"})
        for operation_id, expected in generated.items():
            path = FIXTURES / f"{operation_id}.json"
            actual = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(actual, expected)
            self.validator.validate(actual)
            self.assertEqual(actual["request_plan"]["status"], "complete")
            self.assertEqual(actual["runtime_binding"]["status"], "bound")
            self.assertEqual(actual["admission"]["status"], "admitted")

    def test_transport_auth_and_input_dimensions_are_explicit(self):
        by_id = {item["operation_identity"]["operation_id"]: item for item in self.generated}
        rest = by_id["synthetic-rest-list"]["request_plan"]["request_contract"]
        soap = by_id["synthetic-soap-read"]["request_plan"]["request_contract"]
        self.assertEqual(rest["transport"]["method_or_action"], "GET")
        self.assertEqual(rest["authentication"]["placement"], "query")
        self.assertEqual(rest["parameters"][0]["location"], "query")
        self.assertEqual(rest["parameters"][0]["cardinality"], "required_single")
        self.assertEqual(soap["transport"]["method_or_action"], "urn:synthetic:Read")
        self.assertEqual(soap["authentication"]["placement"], "soap_header")
        self.assertEqual(soap["parameters"][0]["location"], "body")
        self.assertEqual(soap["parameters"][0]["cardinality"], "required_single")

    def test_shared_credential_quota_scope_is_identical_across_apis(self):
        by_id = {item["operation_identity"]["operation_id"]: item for item in self.generated}
        rest_scopes = by_id["synthetic-rest-list"]["runtime_binding"]["quota_policies"]
        soap_scopes = by_id["synthetic-soap-read"]["runtime_binding"]["quota_policies"]
        rest_credential = next(item for item in rest_scopes if item["scope_kind"] == "credential")
        soap_credential = next(item for item in soap_scopes if item["scope_kind"] == "credential")
        self.assertEqual(rest_credential["scope_key"], soap_credential["scope_key"])
        self.assertEqual(rest_credential["scope_sha256"], soap_credential["scope_sha256"])
        rest_api = next(item for item in rest_scopes if item["scope_kind"] == "api")
        soap_api = next(item for item in soap_scopes if item["scope_kind"] == "api")
        self.assertNotEqual(rest_api["scope_sha256"], soap_api["scope_sha256"])

    def test_fixture_transport_and_host_are_test_only(self):
        for item in self.generated:
            contract = item["request_plan"]["request_contract"]
            self.assertEqual(item["source_binding"]["source_id"], "synthetic_test")
            self.assertEqual(contract["transport"]["authority"], "synthetic_fixture")
            self.assertTrue(contract["transport"]["host"].endswith(".invalid"))


if __name__ == "__main__":
    unittest.main()
