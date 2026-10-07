from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts/generate-operation-observation-plan-fixtures.py"
COMPILER = ROOT / "scripts/generate-operation-observation-plan.py"
SCHEMA = ROOT / "schemas/datapan.operation-observation-plan.v1.schema.json"
FIXTURES = ROOT / "fixtures/operation-observation-plan"


def fixture_generator():
    spec = importlib.util.spec_from_file_location("operation_plan_fixtures", GENERATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load synthetic operation-plan fixture generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compiler_module():
    spec = importlib.util.spec_from_file_location("operation_plan_compiler", COMPILER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load operation-plan compiler")
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
        compiler = compiler_module()
        for operation_id, expected in generated.items():
            path = FIXTURES / f"{operation_id}.json"
            actual = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(actual, expected)
            self.validator.validate(actual)
            compiler.validate_record(actual, self.schema, ROOT)
            self.assertEqual(actual["request_plan"]["status"], "complete")
            self.assertEqual(actual["runtime_binding"]["status"], "bound")
            self.assertEqual(actual["admission"]["status"], "admitted")

    def test_transport_auth_and_input_dimensions_are_explicit(self):
        by_id = {item["operation_identity"]["operation_id"]: item for item in self.generated}
        rest = by_id["synthetic-rest-list"]["request_plan"]["request_contract"]
        soap = by_id["synthetic-soap-read"]["request_plan"]["request_contract"]
        self.assertEqual(rest["transport"]["http_method"], "GET")
        self.assertEqual(rest["operation_effect"]["classification"], "read_only")
        self.assertEqual(rest["authentication"]["placement"], "query")
        self.assertEqual(rest["parameters"][0]["location"], "query")
        self.assertEqual(rest["parameters"][0]["cardinality"], "required_single")
        self.assertEqual(soap["transport"]["http_method"], "POST")
        self.assertEqual(soap["transport"]["soap_action"], "urn:synthetic:Read")
        self.assertEqual(soap["transport"]["soap_version"], "1.1")
        self.assertEqual(soap["transport"]["envelope_namespace"], "http://schemas.xmlsoap.org/soap/envelope/")
        self.assertEqual(soap["transport"]["operation_qname"], {"namespace": "urn:synthetic:items", "local_name": "Read"})
        self.assertEqual(soap["transport"]["body_encoding"], "document_literal")
        self.assertEqual(soap["authentication"]["placement"], "soap_header")
        self.assertEqual(soap["authentication"]["header_qname"], {"namespace": "urn:synthetic:auth", "local_name": "ServiceKey"})
        self.assertEqual(soap["parameters"][0]["location"], "body")
        self.assertEqual(soap["parameters"][0]["cardinality"], "required_single")
        self.assertEqual(soap["parameters"][0]["value_strategy"]["selected_value"], "synthetic-item-1")
        self.assertTrue(by_id["synthetic-rest-list"]["source_binding"]["test_only"])
        self.assertTrue(by_id["synthetic-soap-read"]["source_binding"]["test_only"])
        self.assertEqual(by_id["synthetic-rest-list"]["source_binding"]["adapter_id"], "synthetic-test")

    def test_schema_rejects_admission_without_request_or_runtime_binding(self):
        record = copy.deepcopy(self.generated[0])
        record["runtime_binding"] = {
            "status": "unbound",
            "missing_fields": ["credential_entitlement_reference"],
            "evidence_refs": [],
        }
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(record)

    def test_schema_rejects_incomplete_soap_transport_and_unbounded_values(self):
        soap = copy.deepcopy(self.generated[1])
        del soap["request_plan"]["request_contract"]["transport"]["envelope_namespace"]
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(soap)

        rest = copy.deepcopy(self.generated[0])
        del rest["request_plan"]["request_contract"]["parameters"][0]["value_strategy"]["minimum"]
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(rest)

    def test_reviewed_literal_accepts_integer_selected_value(self):
        record = copy.deepcopy(self.generated[1])
        literal = record["request_plan"]["request_contract"]["parameters"][0]["value_strategy"]
        literal["selected_value"] = 7
        self.validator.validate(record)

    def test_schema_allows_explicit_unauthenticated_runtime_without_credential_ref(self):
        record = copy.deepcopy(self.generated[0])
        authentication = record["request_plan"]["request_contract"]["authentication"]
        authentication.update({"requirement": "none", "mechanism": "none", "placement": "none", "credential_reference_required": False})
        authentication.pop("parameter_name")
        authentication.pop("cardinality")
        record["request_plan"]["request_contract"]["parameters"] = [
            item for item in record["request_plan"]["request_contract"]["parameters"]
            if item["value_strategy"]["kind"] != "credential_reference"
        ]
        record["runtime_binding"].pop("credential_reference")
        record["runtime_binding"].pop("credential_scope_key")
        self.validator.validate(record)

    def test_validator_rejects_registry_default_get_as_method_authority(self):
        compiler = compiler_module()
        record = copy.deepcopy(self.generated[0])
        record["source_binding"].update({
            "source_id": "data_go_kr",
            "provider": "data.go.kr",
            "adapter_id": "data-go-kr",
            "inventory_status": "source_complete",
            "inventory_unknown": False,
            "test_only": False,
        })
        contract = record["request_plan"]["request_contract"]
        contract["transport"]["authority"] = "operation_document"
        manifest_path = ROOT / "reports/data-go-kr/operation-manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest_ref = {
            "artifact_path": manifest_path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "json_pointer": "#/operations/0",
            "evidence_kind": "operation_manifest",
        }
        contract["transport"]["evidence_refs"] = [
            {**manifest_ref, "evidence_kind": "operation_document"},
            manifest_ref,
        ]
        contract["operation_effect"]["authority"] = "operation_document"
        contract["operation_effect"]["evidence_refs"] = [{**manifest_ref, "evidence_kind": "operation_document"}]
        with self.assertRaisesRegex(compiler.PlanError, "registry_default_get"):
            compiler.validate_record(record, self.schema, ROOT)

    def test_validator_requires_credential_parameter_to_match_authentication(self):
        compiler = compiler_module()
        record = copy.deepcopy(self.generated[0])
        contract = record["request_plan"]["request_contract"]
        contract["authentication"]["parameter_name"] = "differentKey"
        with self.assertRaisesRegex(compiler.PlanError, "parameter name differs"):
            compiler.validate_record(record, self.schema, ROOT)

        record = copy.deepcopy(self.generated[1])
        contract = record["request_plan"]["request_contract"]
        contract["parameters"][-1]["qualified_name"]["local_name"] = "OtherHeader"
        with self.assertRaisesRegex(compiler.PlanError, "parameter QName differs"):
            compiler.validate_record(record, self.schema, ROOT)

        record = copy.deepcopy(self.generated[0])
        record["runtime_binding"]["credential_scope_key"] = "synthetic:wrong-group"
        with self.assertRaisesRegex(compiler.PlanError, "differs from its quota policy"):
            compiler.validate_record(record, self.schema, ROOT)

    def test_shared_quota_limits_must_agree_and_digest_has_pinned_vector(self):
        generator = compiler_module()
        credential = next(
            item
            for item in self.generated[0]["runtime_binding"]["quota_policies"]
            if item["scope_kind"] == "credential"
        )
        self.assertEqual(
            credential["scope_sha256"],
            "9edfd346f33fb2c2b2308b096260bb16a9c5dc8910c216095c7f23fb3cc78ddf",
        )
        modified = copy.deepcopy(self.generated)
        soap_credential = next(
            item
            for item in modified[1]["runtime_binding"]["quota_policies"]
            if item["scope_kind"] == "credential"
        )
        soap_credential["requests_per_window"] += 1
        with self.assertRaises(generator.PlanError):
            generator.validate_quota_consistency(modified)

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
