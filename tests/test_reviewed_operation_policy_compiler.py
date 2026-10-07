from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
COMPILER_PATH = ROOT / "scripts/generate-operation-observation-plan.py"
PLAN_SCHEMA_PATH = ROOT / "schemas/datapan.operation-observation-plan.v1.schema.json"
POLICY_SCHEMA_PATH = ROOT / "schemas/datapan.operation-observation-policy.v1.schema.json"
ASSERTION_SCHEMA_PATH = ROOT / "schemas/datapan.operation-response-assertion.v2.schema.json"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReviewedOperationPolicyCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = load_module("reviewed_operation_policy_compiler", COMPILER_PATH)
        cls.plan_schema = json.loads(PLAN_SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.policy_schema = json.loads(POLICY_SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.assertion_schema = json.loads(ASSERTION_SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.plan_validator = jsonschema.Draft202012Validator(cls.plan_schema, format_checker=jsonschema.FormatChecker())

    def _write_json(self, root: Path, relative: str, value: object) -> dict:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return self.compiler.artifact_ref(path, root)

    def _fixture(self, root: Path, protocol: str) -> dict:
        is_soap = protocol == "SOAP"
        operation = {
            "operation_id": f"synthetic-{protocol.lower()}-operation",
            "protocol": protocol,
            "provenance": {
                "dataset_id": "dataset-synthetic",
                "operation_name": "read-only list operation",
                "upstream_operation_key": "op-1",
            },
            "transport": {
                "endpoint": "https://soap.example.test/api" if is_soap else "https://api.example.test/v1/list",
                "method_evidence": "operation_documented_method",
            },
        }
        identity = self.compiler.operation_policy_identity("data_go_kr", "data.go.kr", operation)
        source_ref = {"source_id": "official-example", "evidence_kind": "official_operation_document"}

        def fact(value: object, evidence_kind: str = "operation_http_method", **extra: object) -> dict:
            return {"value": value, "status": "documented", "source_refs": [{**source_ref, "evidence_kind": evidence_kind}], **extra}

        transport = {
            "scheme": fact("https", "operation_endpoint"),
            "host": fact("soap.example.test" if is_soap else "api.example.test", "operation_endpoint"),
            "path": fact("/api" if is_soap else "/v1/list", "operation_endpoint"),
            "http_method": fact("POST" if is_soap else "GET", "operation_http_method", authority_scope="operation_specific"),
            "soap_action": fact("urn:example:list", "soap_action") if is_soap else {"value": None, "status": "not_applicable", "source_refs": [source_ref]},
        }
        if is_soap:
            transport.update({
                "soap_version": fact("1.1", "soap_version"),
                "envelope_namespace": fact("http://schemas.xmlsoap.org/soap/envelope/", "soap_envelope"),
                "operation_qname": fact("{urn:example}GetItems", "soap_operation_qname"),
                "body_encoding": fact("document_literal", "soap_body_encoding"),
            })

        parameters = []
        if not is_soap:
            parameters = [
                {
                    "name": "serviceKey",
                    "location": fact("query", "parameter_location"),
                    "requiredness": fact("required", "parameter_requiredness"),
                    "cardinality": {"minimum": 1, "maximum": 1, "status": "documented", "source_refs": [source_ref]},
                    "data_type": fact("string", "parameter_type"),
                    "source_refs": [{**source_ref, "evidence_kind": "parameter_name"}],
                },
                {
                    "name": "pageNo",
                    "location": fact("query", "parameter_location"),
                    "requiredness": fact("optional", "parameter_requiredness"),
                    "cardinality": {"minimum": 0, "maximum": 1, "status": "documented", "source_refs": [source_ref]},
                    "data_type": fact("integer", "parameter_type"),
                    "source_refs": [{**source_ref, "evidence_kind": "parameter_name"}],
                },
            ]

        auth = (
            {
                "status": "documented",
                "requirement": "none",
                "mechanism": "none",
                "placement": "none",
                "parameter_names": [],
                "source_refs": [{**source_ref, "evidence_kind": "authentication_none"}],
            }
            if is_soap
            else {
                "status": "documented",
                "requirement": "required",
                "mechanism": "service_key",
                "placement": "query",
                "parameter_names": ["serviceKey"],
                "source_refs": [{**source_ref, "evidence_kind": "authentication_parameter_description"}],
            }
        )
        response_path = (
            {"kind": "xml_qname_path", "segments": [{"namespace": "urn:example", "local_name": "items"}]}
            if is_soap
            else {"kind": "json_pointer", "value": "#/items"}
        )
        collection_path = None if is_soap else response_path
        container_path = (
            {"kind": "xml_qname_path", "segments": [{"namespace": "urn:example", "local_name": "result"}]}
            if is_soap
            else {"kind": "json_pointer", "value": "#"}
        )
        response_field = {
            "status": "documented",
            "path": response_path,
            "value_type": "array",
            "cardinality": {"status": "documented", "minimum": 1, "maximum": 1, "source_refs": [source_ref]},
            "source_refs": [source_ref],
        }
        root_shape = {
            "status": "documented",
            "kind": "xml_element" if is_soap else "object",
            "source_refs": [source_ref],
        }
        if is_soap:
            root_shape["qname"] = {"namespace": "urn:example", "local_name": "GetItemsResponse"}
        response_branch = {
            "http_status_code": 200,
            "payload": {"kind": "soap_xml" if is_soap else "json", "media_types": ["text/xml" if is_soap else "application/json"], "status": "documented", "source_refs": [source_ref]},
            "schema_shape": {"status": "complete", "source_refs": [source_ref]},
            "schema_source": {"status": "documented", "json_pointer": "#/paths/~1list/get/responses/200/schema", "source_refs": [source_ref]},
            "root_shape": root_shape,
            "documented_fields": [] if is_soap else [response_field],
            "required_fields": [] if is_soap else [response_field],
            "coded_result_field_inventory": {"status": "documented", "candidates": [], "source_refs": [source_ref]},
            "provider_result_codes": {"status": "not_applicable", "source_refs": [source_ref]},
            "result_collection": {
                "status": "documented",
                "path": collection_path,
                "container_path": container_path,
                "item_path": response_path if is_soap else None,
                "container_cardinality": (
                    {"status": "documented", "minimum": 1, "maximum": 1, "source_refs": [source_ref]}
                    if is_soap
                    else {"status": "not_applicable", "minimum": None, "maximum": None, "source_refs": []}
                ),
                "value_type": "array",
                "source_refs": [source_ref],
            },
            "source_refs": [source_ref],
        }
        document = {
            "identity": identity,
            "transport": transport,
            "effect": {
                "classification": "read_only",
                "status": "documented",
                "authority": "operation_document",
                "source_refs": [{**source_ref, "evidence_kind": "operation_effect"}],
            },
            "authentication": auth,
            "parameters": parameters,
            "response_contract": {
                "payload": {"kind": "soap_xml" if is_soap else "json", "media_types": ["text/xml" if is_soap else "application/json"], "status": "documented", "source_refs": [source_ref]},
                "schema_shape": {"status": "complete", "source_refs": [source_ref]},
                "coded_result_field_inventory": {"status": "documented", "candidates": [], "source_refs": [source_ref]},
                "accepted_http_status_codes": {"status": "documented", "values": [200], "source_refs": [source_ref]},
                "documented_fields": [response_field],
                "required_fields": [response_field],
                "provider_result_codes": {"status": "not_applicable", "source_refs": [source_ref]},
                "result_collection": {
                    "status": "documented",
                    "path": collection_path,
                    "container_path": container_path,
                    "item_path": response_path if is_soap else None,
                    "container_cardinality": (
                        {"status": "documented", "minimum": 1, "maximum": 1, "source_refs": [source_ref]}
                        if is_soap
                        else {"status": "not_applicable", "minimum": None, "maximum": None, "source_refs": []}
                    ),
                    "value_type": "array",
                    "source_refs": [source_ref],
                },
                "success_branches": [response_branch],
            },
        }
        document_ref = self._write_json(root, f"reports/operation-document-evidence/{operation['operation_id']}.json", document)
        doc_reference = lambda pointer: {
            "artifact_path": document_ref["path"],
            "sha256": document_ref["sha256"],
            "json_pointer": pointer,
            "evidence_kind": "operation_document",
        }
        review_artifact_ref = self._write_json(
            root,
            "policy/synthetic-response-review.json",
            {"review": {"review_ref": "https://github.com/StatPan/datapan-registry/issues/759#synthetic-test-policy", "reviewed_by": "test-reviewer"}},
        )
        assertion = {
            "schema_version": "datapan.operation-response-assertion.v2",
            "artifact_kind": "operation_response_assertion",
            "source_binding": {key: identity[key] for key in ("source_id", "provider", "protocol")},
            "operation_identity": {key: identity[key] for key in ("operation_id", "dataset_id", "operation_name", "upstream_operation_key")},
            "document_evidence": document_ref,
            "review": {
                "review_ref": "https://github.com/StatPan/datapan-registry/issues/759#synthetic-test-policy",
                "reviewed_by": "test-reviewer",
                "rationale": "Test-only reviewed response contract for a synthetic operation.",
            },
            "assertion": {
                "payload_kind": "soap_xml" if is_soap else "json",
                "branches": [{
                  "branch_id": "success-200",
                  "classification": "success",
                  "empty_result_semantics": "valid",
                  "selector": {
                        "accepted_http_status_codes": [200],
                        "root_kind": "xml_element" if is_soap else "object",
                        **({"root_qname": {"namespace": "urn:example", "local_name": "GetItemsResponse"}} if is_soap else {}),
                        "discriminators": [],
                    },
                    "http_status_source_refs": [doc_reference("#/response_contract/success_branches/0/http_status_code")],
                    "required_fields": [] if is_soap else [{
                        "path": response_path,
                        "value_type": "array",
                        "cardinality": {"minimum": 1, "maximum": 1},
                        "source_refs": [doc_reference("#/response_contract/success_branches/0/documented_fields/0")],
                    }],
                    "provider_result_code_status": "none_by_policy",
                    "provider_result_code_evidence_refs": [
                        doc_reference("#/response_contract/success_branches/0/schema_shape"),
                        doc_reference("#/response_contract/success_branches/0/coded_result_field_inventory"),
                    ],
                    "result_collection": {
                        "path": collection_path,
                        "container_path": container_path,
                        "item_path": response_path if is_soap else None,
                        **({"container_cardinality": {"minimum": 1, "maximum": 1}} if is_soap else {}),
                        "value_type": "array",
                        "semantics": "valid",
                        "source_refs": [doc_reference("#/response_contract/success_branches/0/result_collection")],
                    },
                    "source_refs": [
                        doc_reference("#/response_contract/success_branches/0"),
                        doc_reference("#/response_contract/success_branches/0/payload"),
                        doc_reference("#/response_contract/success_branches/0/schema_shape"),
                        doc_reference("#/response_contract/success_branches/0/root_shape"),
                        *([] if is_soap else [doc_reference("#/response_contract/success_branches/0/documented_fields/0")]),
                        doc_reference("#/response_contract/success_branches/0/result_collection"),
                    ],
                  "review_refs": [{
                      "artifact_path": review_artifact_ref["path"],
                      "sha256": review_artifact_ref["sha256"],
                      "json_pointer": "#/review",
                      "evidence_kind": "reviewed_policy",
                  }],
                }],
            },
        }
        assertion_path = f"reports/operation-response-assertions/{operation['operation_id']}.json"
        assertion_ref = self._write_json(root, assertion_path, assertion)
        strategies = [] if is_soap else [{"name": "pageNo", "strategy": {"kind": "bounded_integer", "minimum": 1, "maximum": 1, "selection": "minimum"}}]
        quota_policies = [
            {"scope_kind": "provider", "scope_key": "data-go-kr", "max_concurrent": 1, "requests_per_window": 10, "window_seconds": 3600, "minimum_interval_seconds": 60},
        ]
        runtime_binding = {
            "quota_policies": quota_policies,
            "observation_period_seconds": 3600,
        }
        if not is_soap:
            runtime_binding["credential_reference"] = "credential://data-go-kr/synthetic-test"
            runtime_binding["credential_scope_key"] = "test-credential-group"
            quota_policies.append({
                "scope_kind": "credential",
                "scope_key": "test-credential-group",
                "max_concurrent": 1,
                "requests_per_window": 5,
                "window_seconds": 3600,
                "minimum_interval_seconds": 60,
            })
        policy = {
            "identity": identity,
            "document_evidence": document_ref,
            "review": {
                "review_ref": "https://github.com/StatPan/datapan-registry/issues/759#synthetic-test-policy",
                "reviewed_by": "test-reviewer",
                "rationale": "Test-only request choices for a synthetic, source-grounded operation.",
            },
            "request": {
                "parameter_strategies": strategies,
                "omit_unmapped_optional_parameters": False,
                "limits": {"request_budget": 1, "timeout_ms": 5000, "max_request_bytes": 8192, "max_response_bytes": 65536},
                "response_assertion_artifact": assertion_ref,
            },
            "runtime_binding": runtime_binding,
        }
        self._write_json(root, "policy/operation-observation-policies.v1.json", {
            "schema_version": "datapan.operation-observation-policy.v1",
            "artifact_kind": "operation_observation_policy_set",
            "policies": [policy],
            "profiles": [],
        })
        self._write_json(root, "schemas/datapan.operation-observation-policy.v1.schema.json", self.policy_schema)
        self._write_json(root, "schemas/datapan.operation-response-assertion.v2.schema.json", self.assertion_schema)
        manifest = {"operations": [operation]}
        manifest_ref = self._write_json(root, "reports/data-go-kr/operation-manifest.json", manifest)
        scope = {
            "source_id": "data_go_kr",
            "provider": "data.go.kr",
            "adapter_id": "data-go-kr",
            "inventory_status": "source_complete",
            "inventory_unknown": False,
            "test_only": False,
            "source_artifacts": [manifest_ref, document_ref, assertion_ref],
        }
        return {"operation": operation, "document": document, "document_ref": document_ref, "scope": scope}

    def _compile(self, root: Path, protocol: str) -> dict:
        fixture = self._fixture(root, protocol)
        operation = fixture["operation"]
        document = fixture["document"]
        identity = {key: operation["provenance"][key] for key in ("dataset_id", "operation_name", "upstream_operation_key")}
        plan = self.compiler.make_incomplete_plan(
            scope=fixture["scope"],
            operation_id=operation["operation_id"],
            protocol=protocol,
            identity=identity,
            evidence={"artifact_path": "reports/data-go-kr/operation-manifest.json", "sha256": "a" * 64, "json_pointer": "#/operations/0", "evidence_kind": "operation_manifest"},
        )
        evidence = {operation["operation_id"]: {"document": document, "artifact_ref": fixture["document_ref"]}}
        policies, _policy_set, policy_ref, _refs = self.compiler.load_reviewed_operation_policies(root, [operation], evidence)
        reviewed = policies[operation["operation_id"]]
        return self.compiler.compile_reviewed_operation_plan(
            plan,
            operation,
            document,
            fixture["document_ref"],
            reviewed["policy"],
            reviewed["artifact_ref"],
            reviewed["index"],
            reviewed["assertion"],
            reviewed["assertion_ref"],
            root,
        )

    def test_synthetic_rest_policy_compiles_to_typed_complete_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._compile(root, "REST")
            self.plan_validator.validate(record)
            self.compiler.validate_record(record, self.plan_schema, root, self.plan_validator)
            self.assertEqual(record["request_plan"]["status"], "complete")
            self.assertEqual(record["request_plan"]["request_contract"]["transport"]["http_method"], "GET")
            self.assertEqual(record["request_plan"]["request_contract"]["parameters"][1]["value_strategy"]["kind"], "bounded_integer")
            self.assertEqual(record["runtime_binding"]["status"], "bound")
            self.assertEqual(record["admission"]["status"], "not_admitted")
            self.assertNotIn("selected_value", record["request_plan"]["request_contract"]["parameters"][1]["value_strategy"])

    def test_synthetic_soap_policy_compiles_qname_and_no_auth_without_fake_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._compile(root, "SOAP")
            self.plan_validator.validate(record)
            self.compiler.validate_record(record, self.plan_schema, root, self.plan_validator)
            contract = record["request_plan"]["request_contract"]
            self.assertEqual(contract["transport"]["operation_qname"], {"namespace": "urn:example", "local_name": "GetItems"})
            self.assertEqual(contract["authentication"]["requirement"], "none")
            self.assertNotIn("credential_reference", record["runtime_binding"])
            self.assertEqual(contract["response_assertion"]["kind"], "soap_fault_free")

    def test_reviewed_policy_can_omit_only_documented_optional_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root, "REST")
            policy_path = root / "policy/operation-observation-policies.v1.json"
            policy_set = json.loads(policy_path.read_text(encoding="utf-8"))
            policy_set["policies"][0]["request"]["parameter_strategies"] = []
            policy_set["policies"][0]["request"]["omit_unmapped_optional_parameters"] = True
            policy_path.write_text(json.dumps(policy_set, ensure_ascii=False), encoding="utf-8")
            operation = fixture["operation"]
            evidence = {operation["operation_id"]: {"document": fixture["document"], "artifact_ref": fixture["document_ref"]}}
            policies, _policy_set, policy_ref, _refs = self.compiler.load_reviewed_operation_policies(root, [operation], evidence)
            reviewed = policies[operation["operation_id"]]
            identity = {key: operation["provenance"][key] for key in ("dataset_id", "operation_name", "upstream_operation_key")}
            plan = self.compiler.make_incomplete_plan(
                scope=fixture["scope"], operation_id=operation["operation_id"], protocol="REST", identity=identity,
                evidence={"artifact_path": "reports/data-go-kr/operation-manifest.json", "sha256": "a" * 64, "json_pointer": "#/operations/0", "evidence_kind": "operation_manifest"},
            )
            record = self.compiler.compile_reviewed_operation_plan(
                plan, operation, fixture["document"], fixture["document_ref"], reviewed["policy"], policy_ref,
                reviewed["index"], reviewed["assertion"], reviewed["assertion_ref"], root,
            )
            self.plan_validator.validate(record)
            self.compiler.validate_record(record, self.plan_schema, root, self.plan_validator)
            self.assertEqual([item["name"] for item in record["request_plan"]["request_contract"]["parameters"]], ["serviceKey"])

    def test_reviewed_omission_rejects_any_required_parameter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root, "REST")
            document = copy.deepcopy(fixture["document"])
            document["parameters"][1]["requiredness"]["value"] = "required"
            document["parameters"][1]["cardinality"].update({"minimum": 1, "maximum": 1})
            policy_path = root / "policy/operation-observation-policies.v1.json"
            policy_set = json.loads(policy_path.read_text(encoding="utf-8"))
            policy_set["policies"][0]["request"]["parameter_strategies"] = []
            policy_set["policies"][0]["request"]["omit_unmapped_optional_parameters"] = True
            policy_path.write_text(json.dumps(policy_set, ensure_ascii=False), encoding="utf-8")
            operation = fixture["operation"]
            evidence = {operation["operation_id"]: {"document": document, "artifact_ref": fixture["document_ref"]}}
            policies, _policy_set, policy_ref, _refs = self.compiler.load_reviewed_operation_policies(root, [operation], evidence)
            reviewed = policies[operation["operation_id"]]
            identity = {key: operation["provenance"][key] for key in ("dataset_id", "operation_name", "upstream_operation_key")}
            plan = self.compiler.make_incomplete_plan(
                scope=fixture["scope"], operation_id=operation["operation_id"], protocol="REST", identity=identity,
                evidence={"artifact_path": "reports/data-go-kr/operation-manifest.json", "sha256": "a" * 64, "json_pointer": "#/operations/0", "evidence_kind": "operation_manifest"},
            )
            with self.assertRaisesRegex(self.compiler.PlanError, "not documented optional"):
                self.compiler.compile_reviewed_operation_plan(
                    plan, operation, document, fixture["document_ref"], reviewed["policy"], policy_ref,
                    reviewed["index"], reviewed["assertion"], reviewed["assertion_ref"], root,
                )

    def test_reusable_profile_compiles_multiple_matching_operations_without_per_operation_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root, "REST")
            operation = fixture["operation"]
            document = fixture["document"]
            policy_path = root / "policy/operation-observation-policies.v1.json"
            policy_set = {
                "schema_version": "datapan.operation-observation-policy.v1",
                "artifact_kind": "operation_observation_policy_set",
                "policies": [],
                "profiles": [{
                    "profile_id": "synthetic-rest-list-v1",
                    "selector": {
                        "source_id": "data_go_kr",
                        "provider": "data.go.kr",
                        "protocol": "REST",
                        "effect": "read_only",
                        "method": "GET",
                        "authentication": {
                            "requirement": "required",
                            "mechanism": "service_key",
                            "placement": "query",
                            "parameter_name": "serviceKey",
                        },
                    },
                    "review": {
                        "review_ref": "https://github.com/StatPan/datapan-registry/issues/759#synthetic-profile",
                        "reviewed_by": "test-reviewer",
                        "rationale": "Test-only shared policy for documented read-only REST list operations.",
                    },
                    "request": {
                        "parameter_strategies": [{"name": "pageNo", "strategy": {"kind": "bounded_integer", "minimum": 1, "maximum": 1, "selection": "minimum"}}],
                        "omit_unmapped_optional_parameters": True,
                        "limits": {"request_budget": 1, "timeout_ms": 5000, "max_request_bytes": 8192, "max_response_bytes": 65536},
                        "response": {
                            "payload_kind": "json",
                            "branches": [{
                                "branch_id": "success-200",
                                "classification": "success",
                                "selector": {"accepted_http_status_codes": [200], "root_kind": "object", "discriminators": []},
                                "empty_result_semantics": "valid",
                                "code_mode": "none",
                                "code_mode_rationale": "The exact complete synthetic response schema has no coded result field.",
                                "required_fields": [{"path": {"kind": "json_pointer", "value": "#/items"}, "value_type": "array", "minimum": 1, "maximum": 1}],
                                "result_collection": {
                                    "path": {"kind": "json_pointer", "value": "#/items"},
                                    "container_path": {"kind": "json_pointer", "value": "#"},
                                    "item_path": None,
                                },
                            }],
                        },
                    },
                    "runtime_binding": {
                        "credential_reference": "credential://data-go-kr/synthetic-test",
                        "credential_scope_key": "test-credential-group",
                        "quota_policies": [
                            {"scope_kind": "provider", "scope_key": "data-go-kr", "max_concurrent": 1, "requests_per_window": 10, "window_seconds": 3600, "minimum_interval_seconds": 60},
                            {"scope_kind": "credential", "scope_key": "test-credential-group", "max_concurrent": 1, "requests_per_window": 5, "window_seconds": 3600, "minimum_interval_seconds": 60},
                        ],
                        "observation_period_seconds": 3600,
                    },
                }],
            }
            policy_path.write_text(json.dumps(policy_set, ensure_ascii=False, indent=2), encoding="utf-8")
            evidence = {operation["operation_id"]: {"document": document, "artifact_ref": fixture["document_ref"]}}
            policies, _policy_set, policy_ref, policy_inputs = self.compiler.load_reviewed_operation_policies(root, [operation], evidence)
            self.assertFalse(policies)
            profile = policy_inputs["profiles"][0]
            self.assertTrue(self.compiler.profile_matches_operation(profile, "data_go_kr", "data.go.kr", operation, document))
            operation_identity = self.compiler.operation_policy_identity("data_go_kr", "data.go.kr", operation)
            assertion = self.compiler.compile_profile_assertion(profile, operation_identity, document, fixture["document_ref"], policy_ref, 0)
            assertion_ref = self._write_json(root, f"reports/operation-response-assertions/{operation['operation_id']}.json", assertion)
            effective_policy = {
                "identity": operation_identity,
                "document_evidence": fixture["document_ref"],
                "review": profile["review"],
                "request": {**profile["request"], "response_assertion_artifact": assertion_ref},
                "runtime_binding": profile["runtime_binding"],
            }
            identity = {key: operation["provenance"][key] for key in ("dataset_id", "operation_name", "upstream_operation_key")}
            plan = self.compiler.make_incomplete_plan(
                scope=fixture["scope"], operation_id=operation["operation_id"], protocol="REST", identity=identity,
                evidence={"artifact_path": "reports/data-go-kr/operation-manifest.json", "sha256": "a" * 64, "json_pointer": "#/operations/0", "evidence_kind": "operation_manifest"},
            )
            plan = self.compiler.compile_reviewed_operation_plan(
                plan, operation, document, fixture["document_ref"], effective_policy, policy_ref, 0,
                assertion, assertion_ref, root, policy_pointer_base="#/profiles/0",
            )
            self.plan_validator.validate(plan)
            self.compiler.validate_record(plan, self.plan_schema, root, self.plan_validator)
            contract = plan["request_plan"]["request_contract"]
            self.assertEqual(plan["request_plan"]["status"], "complete")
            self.assertEqual(contract["response_assertion"]["empty_result_semantics"], "valid")
            self.assertEqual(contract["response_assertion"]["kind"], "json_contract")
            self.assertEqual([item["name"] for item in contract["parameters"]], ["serviceKey", "pageNo"])

    def test_response_union_compiles_array_success_and_object_error_with_member_selectors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root, "REST")
            operation = fixture["operation"]
            document = fixture["document"]
            source_ref = {"source_id": "official-example", "evidence_kind": "official_operation_document"}

            def branch(member: str, value_type: str, *, with_collection: bool) -> dict:
                row = copy.deepcopy(document["response_contract"]["success_branches"][0])
                member_path = {"kind": "json_pointer", "value": f"#/{member}"}
                field = {
                    "status": "documented",
                    "path": member_path,
                    "value_type": value_type,
                    "cardinality": {"status": "documented", "minimum": 1, "maximum": 1, "source_refs": [source_ref]},
                    "source_refs": [source_ref],
                }
                row["documented_fields"] = [field]
                row["required_fields"] = [field]
                row["member_shape_selectors"] = [
                    {"status": "required", "path": member_path, "value_type": value_type, "source_refs": [source_ref]},
                    {"status": "required", "path": {"kind": "json_pointer", "value": "#/meta"}, "value_type": "object", "source_refs": [source_ref]},
                    {"status": "required", "path": {"kind": "json_pointer", "value": "#/kind"}, "value_type": "string", "enum_values": ["success" if member == "service" else "error"], "source_refs": [source_ref]},
                ]
                row["coded_result_field_inventory"] = {"status": "documented", "candidates": [], "source_refs": [source_ref]}
                row["provider_result_codes"] = {"status": "not_applicable", "source_refs": [source_ref]}
                if with_collection:
                    row["result_collection"] = {
                        "status": "documented",
                        "path": member_path,
                        "container_path": {"kind": "json_pointer", "value": "#"},
                        "item_path": None,
                        "container_cardinality": {"status": "not_applicable", "minimum": None, "maximum": None, "source_refs": []},
                        "value_type": "array",
                        "source_refs": [source_ref],
                    }
                else:
                    row.pop("result_collection", None)
                return row

            success = branch("service", "array", with_collection=True)
            provider_error = branch("RESULT", "object", with_collection=False)
            provider_error["member_shape_selectors"].append({
                "status": "not_present",
                "path": {"kind": "json_pointer", "value": "#/service"},
                "source_refs": [source_ref],
            })
            document["response_contract"]["success_branches"] = [success, provider_error]
            document_ref = self._write_json(root, "reports/operation-document-evidence/response-union.json", document)
            profile = {
                "profile_id": "synthetic-object-response-union-v1",
                "selector": {
                    "source_id": "data_go_kr", "provider": "data.go.kr", "protocol": "REST",
                    "effect": "read_only", "method": "GET",
                    "authentication": {"requirement": "required", "mechanism": "service_key", "placement": "query", "parameter_name": "serviceKey"},
                },
                "review": {"review_ref": "https://github.com/StatPan/datapan-registry/issues/759#union-test", "reviewed_by": "test-reviewer", "rationale": "Test-only mutually guarded response union."},
                "request": {
                    "parameter_strategies": [{"name": "pageNo", "strategy": {"kind": "bounded_integer", "minimum": 1, "maximum": 1, "selection": "minimum"}}],
                    "omit_unmapped_optional_parameters": True,
                    "limits": {"request_budget": 1, "timeout_ms": 5000, "max_request_bytes": 8192, "max_response_bytes": 65536},
                    "response": {
                        "payload_kind": "json",
                        "branches": [
                            {"branch_id": "service-array-success", "classification": "success", "selector": {"accepted_http_status_codes": [200], "root_kind": "object", "discriminators": [{"path": {"kind": "json_pointer", "value": "#/meta"}, "predicate": "present"}, {"path": {"kind": "json_pointer", "value": "#/kind"}, "predicate": "equals_any", "value_type": "string", "values": ["success"]}, {"path": {"kind": "json_pointer", "value": "#/service"}, "predicate": "present"}]}, "empty_result_semantics": "valid", "code_mode": "none", "code_mode_rationale": "The complete synthetic branch has no result-code candidate.", "required_fields": [{"path": {"kind": "json_pointer", "value": "#/service"}, "value_type": "array", "minimum": 1, "maximum": 1}], "result_collection": {"path": {"kind": "json_pointer", "value": "#/service"}, "container_path": {"kind": "json_pointer", "value": "#"}, "item_path": None}},
                            {"branch_id": "result-object-error", "classification": "provider_error", "selector": {"accepted_http_status_codes": [200], "root_kind": "object", "discriminators": [{"path": {"kind": "json_pointer", "value": "#/meta"}, "predicate": "present"}, {"path": {"kind": "json_pointer", "value": "#/kind"}, "predicate": "equals_any", "value_type": "string", "values": ["error"]}, {"path": {"kind": "json_pointer", "value": "#/service"}, "predicate": "absent"}, {"path": {"kind": "json_pointer", "value": "#/RESULT"}, "predicate": "present"}]}, "empty_result_semantics": "not_applicable", "code_mode": "none", "code_mode_rationale": "The complete synthetic error branch has no separate result-code field.", "required_fields": [], "result_collection": None},
                        ],
                    },
                },
            }
            policy_path = root / "policy/operation-observation-policies.v1.json"
            policy_path.write_text(json.dumps({"schema_version": "datapan.operation-observation-policy.v1", "artifact_kind": "operation_observation_policy_set", "policies": [], "profiles": [profile]}, ensure_ascii=False), encoding="utf-8")
            policy_validator = jsonschema.Draft202012Validator(self.policy_schema, format_checker=jsonschema.FormatChecker())
            policy_validator.validate(json.loads(policy_path.read_text(encoding="utf-8")))
            self.assertTrue(self.compiler.profile_matches_operation(profile, "data_go_kr", "data.go.kr", operation, document))
            identity = self.compiler.operation_policy_identity("data_go_kr", "data.go.kr", operation)
            assertion = self.compiler.compile_profile_assertion(profile, identity, document, document_ref, self.compiler.artifact_ref(policy_path, root), 0)
            self.assertEqual([row["classification"] for row in assertion["assertion"]["branches"]], ["success", "provider_error"])
            self.assertEqual([row["selector"]["root_kind"] for row in assertion["assertion"]["branches"]], ["object", "object"])
            self.assertEqual(len(assertion["assertion"]["branches"]), 2)
            self.assertTrue(all(row["selector"]["discriminators"] for row in assertion["assertion"]["branches"]))
            jsonschema.Draft202012Validator(self.assertion_schema, format_checker=jsonschema.FormatChecker()).validate(assertion)
            self.compiler._validate_assertion_v2_fact_binding(assertion, document, document_ref)

    def test_integer_and_number_discriminators_overlap(self):
        path = {"kind": "json_pointer", "value": "#/code"}
        integer_type = {"path": path, "predicate": "node_type", "value_type": "integer"}
        number_type = {"path": path, "predicate": "node_type", "value_type": "number"}
        integer_value = {"path": path, "predicate": "equals_any", "value_type": "integer", "values": [1]}
        numeric_value = {"path": path, "predicate": "equals_any", "value_type": "number", "values": [1.0]}
        self.assertFalse(self.compiler._same_path_discriminators_are_disjoint(integer_type, number_type))
        self.assertFalse(self.compiler._same_path_discriminators_are_disjoint(integer_value, numeric_value))

    def test_documented_object_success_without_collection_compiles_under_unknown_inventory_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root, "REST")
            operation = fixture["operation"]
            document = fixture["document"]
            source = document["response_contract"]["success_branches"][0]
            source["documented_fields"] = [{
                "status": "documented",
                "path": {"kind": "json_pointer", "value": "#"},
                "value_type": "object",
                "cardinality": {"status": "documented", "minimum": 1, "maximum": 1, "source_refs": [{"source_id": "official-example", "evidence_kind": "official_operation_document"}]},
                "source_refs": [{"source_id": "official-example", "evidence_kind": "official_operation_document"}],
            }]
            source["required_fields"] = copy.deepcopy(source["documented_fields"])
            source.pop("result_collection", None)
            document["response_contract"].pop("result_collection", None)
            document_ref = self._write_json(root, "reports/operation-document-evidence/object-success.json", document)

            profile = {
                "profile_id": "synthetic-object-success-without-collection-v1",
                "selector": {
                    "source_id": "data_go_kr", "provider": "data.go.kr", "protocol": "REST",
                    "effect": "read_only", "method": "GET",
                    "authentication": {"requirement": "required", "mechanism": "service_key", "placement": "query", "parameter_name": "serviceKey"},
                },
                "review": {"review_ref": "https://github.com/StatPan/datapan-registry/issues/759#object-success-test", "reviewed_by": "test-reviewer", "rationale": "Test-only code-free object response policy."},
                "request": {
                    "parameter_strategies": [{"name": "pageNo", "strategy": {"kind": "bounded_integer", "minimum": 1, "maximum": 1, "selection": "minimum"}}],
                    "omit_unmapped_optional_parameters": True,
                    "limits": {"request_budget": 1, "timeout_ms": 5000, "max_request_bytes": 8192, "max_response_bytes": 65536},
                    "response": {
                        "payload_kind": "json",
                        "branches": [{
                            "branch_id": "object-success",
                            "classification": "success",
                            "selector": {"accepted_http_status_codes": [200], "root_kind": "object", "discriminators": []},
                            "empty_result_semantics": "not_applicable",
                            "code_mode": "none",
                            "code_mode_rationale": "The complete synthetic object shape has no coded result field.",
                            "required_fields": [{"path": {"kind": "json_pointer", "value": "#"}, "value_type": "object", "minimum": 1, "maximum": 1}],
                            "result_collection": None,
                        }],
                    },
                },
            }
            invalid_empty_policy = copy.deepcopy(profile)
            invalid_empty_policy["request"]["response"]["branches"][0]["empty_result_semantics"] = "valid"
            invalid_policy_set = {"schema_version": "datapan.operation-observation-policy.v1", "artifact_kind": "operation_observation_policy_set", "policies": [], "profiles": [invalid_empty_policy]}
            self.assertTrue(list(jsonschema.Draft202012Validator(self.policy_schema).iter_errors(invalid_policy_set)))
            self.assertTrue(self.compiler.profile_matches_operation(profile, "data_go_kr", "data.go.kr", operation, document))
            policy_ref = self._write_json(root, "policy/operation-observation-policies.v1.json", {"schema_version": "datapan.operation-observation-policy.v1", "artifact_kind": "operation_observation_policy_set", "policies": [], "profiles": [profile]})
            identity = self.compiler.operation_policy_identity("data_go_kr", "data.go.kr", operation)
            assertion = self.compiler.compile_profile_assertion(profile, identity, document, document_ref, policy_ref, 0)
            assertion_ref = self._write_json(root, f"reports/operation-response-assertions/{operation['operation_id']}.json", assertion)
            self.compiler._validate_assertion_v2_fact_binding(assertion, document, document_ref)
            self.assertNotIn("result_collection", assertion["assertion"]["branches"][0])
            invalid_assertion = copy.deepcopy(assertion)
            invalid_assertion["assertion"]["branches"][0]["empty_result_semantics"] = "valid"
            self.assertTrue(list(jsonschema.Draft202012Validator(self.assertion_schema).iter_errors(invalid_assertion)))

            policy_entry = {
                "identity": identity,
                "document_evidence": document_ref,
                "review": profile["review"],
                "request": {**profile["request"], "response_assertion_artifact": assertion_ref},
            }
            scope = copy.deepcopy(fixture["scope"])
            scope["inventory_status"] = "partial"
            scope["inventory_unknown"] = True
            plan = self.compiler.make_incomplete_plan(
                scope=scope,
                operation_id=operation["operation_id"],
                protocol="REST",
                identity={key: operation["provenance"][key] for key in ("dataset_id", "operation_name", "upstream_operation_key")},
                evidence={"artifact_path": "reports/data-go-kr/operation-manifest.json", "sha256": "a" * 64, "json_pointer": "#/operations/0", "evidence_kind": "operation_manifest"},
            )
            plan = self.compiler.compile_reviewed_operation_plan(
                plan, operation, document, document_ref, policy_entry, policy_ref, 0,
                assertion, assertion_ref, root, policy_pointer_base="#/profiles/0",
            )
            self.plan_validator.validate(plan)
            legacy_schema = copy.deepcopy(self.plan_schema)
            legacy_schema["$defs"]["request_contract"]["properties"]["response_assertion"]["properties"]["empty_result_semantics"]["enum"] = ["valid", "invalid"]
            legacy_validator = jsonschema.Draft202012Validator(legacy_schema, format_checker=jsonschema.FormatChecker())
            with self.assertRaises(jsonschema.ValidationError):
                legacy_validator.validate(plan)
            self.assertEqual(plan["request_plan"]["status"], "complete")
            self.assertEqual(plan["request_plan"]["request_contract"]["response_assertion"]["empty_result_semantics"], "not_applicable")
            self.assertTrue(plan["source_binding"]["inventory_unknown"])
            self.assertEqual(plan["runtime_binding"]["status"], "unbound")

    def test_policy_identity_and_documented_method_mismatches_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root, "REST")
            operation = fixture["operation"]
            evidence = {operation["operation_id"]: {"document": fixture["document"], "artifact_ref": fixture["document_ref"]}}
            policy_path = root / "policy/operation-observation-policies.v1.json"
            policy_set = json.loads(policy_path.read_text(encoding="utf-8"))
            policy_set["policies"][0]["identity"]["operation_name"] = "different operation"
            policy_path.write_text(json.dumps(policy_set), encoding="utf-8")
            with self.assertRaisesRegex(self.compiler.PlanError, "identity differs"):
                self.compiler.load_reviewed_operation_policies(root, [operation], evidence)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root, "REST")
            fixture["document"]["transport"]["http_method"]["authority_scope"] = "service_level_only"
            operation = fixture["operation"]
            identity = {key: operation["provenance"][key] for key in ("dataset_id", "operation_name", "upstream_operation_key")}
            plan = self.compiler.make_incomplete_plan(scope=fixture["scope"], operation_id=operation["operation_id"], protocol="REST", identity=identity, evidence={"artifact_path": "manifest.json", "sha256": "a" * 64, "json_pointer": "#/operations/0", "evidence_kind": "operation_manifest"})
            evidence = {operation["operation_id"]: {"document": fixture["document"], "artifact_ref": fixture["document_ref"]}}
            policies, _doc, policy_ref, _ = self.compiler.load_reviewed_operation_policies(root, [operation], evidence)
            reviewed = policies[operation["operation_id"]]
            with self.assertRaisesRegex(self.compiler.PlanError, "not operation-specific"):
                self.compiler.compile_reviewed_operation_plan(plan, operation, fixture["document"], fixture["document_ref"], reviewed["policy"], policy_ref, 0, reviewed["assertion"], reviewed["assertion_ref"], root)

    def test_document_endpoint_and_strategy_type_mismatches_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root, "REST")
            fixture["document"]["transport"]["path"]["value"] = "/other"
            operation = fixture["operation"]
            identity = {key: operation["provenance"][key] for key in ("dataset_id", "operation_name", "upstream_operation_key")}
            plan = self.compiler.make_incomplete_plan(scope=fixture["scope"], operation_id=operation["operation_id"], protocol="REST", identity=identity, evidence={"artifact_path": "manifest.json", "sha256": "a" * 64, "json_pointer": "#/operations/0", "evidence_kind": "operation_manifest"})
            evidence = {operation["operation_id"]: {"document": fixture["document"], "artifact_ref": fixture["document_ref"]}}
            policies, _doc, policy_ref, _ = self.compiler.load_reviewed_operation_policies(root, [operation], evidence)
            reviewed = policies[operation["operation_id"]]
            with self.assertRaisesRegex(self.compiler.PlanError, "endpoint differs"):
                self.compiler.compile_reviewed_operation_plan(plan, operation, fixture["document"], fixture["document_ref"], reviewed["policy"], policy_ref, 0, reviewed["assertion"], reviewed["assertion_ref"], root)

        with self.assertRaisesRegex(self.compiler.PlanError, "no documented integer type"):
            self.compiler._require_policy_strategy_matches_type({"kind": "bounded_integer", "minimum": 1, "maximum": 2, "selection": "minimum"}, "string", "pageNo")

    def test_unknown_result_code_semantics_cannot_be_asserted_as_not_applicable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root, "REST")
            fixture["document"]["response_contract"]["success_branches"][0]["schema_shape"]["status"] = "unknown"
            fixture["document"]["response_contract"]["success_branches"][0]["coded_result_field_inventory"]["candidates"] = [{"name": "status"}]
            operation = fixture["operation"]
            evidence = {operation["operation_id"]: {"document": fixture["document"], "artifact_ref": fixture["document_ref"]}}
            with self.assertRaisesRegex(self.compiler.PlanError, "shape is incomplete"):
                self.compiler.load_reviewed_operation_policies(root, [operation], evidence)

    def test_shared_quota_scope_cannot_have_conflicting_limits(self):
        first = {"runtime_binding": {"quota_policies": [{"scope_kind": "credential", "scope_key": "shared", "scope_sha256": self.compiler.quota_scope_digest("credential", "shared"), "max_concurrent": 1, "requests_per_window": 10, "window_seconds": 3600, "minimum_interval_seconds": 60}]}}
        second = copy.deepcopy(first)
        second["runtime_binding"]["quota_policies"][0]["requests_per_window"] = 11
        with self.assertRaisesRegex(self.compiler.PlanError, "conflicting digest or limits"):
            self.compiler.validate_quota_consistency([first, second])

    def test_reviewed_integer_literal_is_valid_without_overlapping_oneof_branches(self):
        value = {
            "schema_version": "datapan.operation-observation-policy.v1",
            "artifact_kind": "operation_observation_policy_set",
            "policies": [{
                "identity": {
                    "source_id": "data_go_kr",
                    "operation_id": "synthetic-op",
                    "provider": "data.go.kr",
                    "protocol": "REST",
                    "dataset_id": "dataset",
                    "operation_name": "operation",
                    "upstream_operation_key": "1",
                },
                "document_evidence": {"path": "reports/doc.json", "sha256": "a" * 64, "bytes": 1},
                "review": {"review_ref": "https://example.test/review", "reviewed_by": "reviewer", "rationale": "Synthetic reviewed integer literal."},
                "request": {
                    "parameter_strategies": [{"name": "year", "strategy": {"kind": "reviewed_literal", "selected_value": 1}}],
                    "omit_unmapped_optional_parameters": False,
                    "limits": {"request_budget": 1, "timeout_ms": 1, "max_request_bytes": 1, "max_response_bytes": 1},
                    "response_assertion_artifact": {"path": "reports/assertion.json", "sha256": "b" * 64, "bytes": 1},
                },
            }],
            "profiles": [],
        }
        validator = jsonschema.Draft202012Validator(self.policy_schema, format_checker=jsonschema.FormatChecker())
        validator.validate(value)

    def test_explicit_success_example_binds_only_the_expected_code_and_unknown_codes_stay_incomplete(self):
        document_ref = {"path": "reports/operation-document-evidence/example.json", "sha256": "a" * 64, "bytes": 1}
        code_path = {"kind": "json_pointer", "value": "#/resultCode"}
        items_path = {"kind": "json_pointer", "value": "#/items"}

        def source_ref(pointer: str) -> dict:
            return {"artifact_path": document_ref["path"], "sha256": document_ref["sha256"], "json_pointer": pointer, "evidence_kind": "operation_document"}

        document = {
            "response_contract": {
                "accepted_http_status_codes": {"status": "documented", "values": [200]},
                "documented_fields": [
                    {"status": "documented", "path": code_path, "value_type": "string", "cardinality": {"status": "documented", "minimum": 1, "maximum": 1}},
                    {"status": "documented", "path": items_path, "value_type": "array", "cardinality": {"status": "documented", "minimum": 1, "maximum": 1}},
                ],
                "provider_result_codes": {
                    "status": "example_only",
                    "evidence_strength": "official_success_example",
                    "path": code_path,
                    "value_type": "string",
                    "success_values": {"status": "example_only", "values": ["00"]},
                    "error_values": {"status": "unknown", "values": []},
                },
                "result_collection": {
                    "status": "documented",
                    "path": items_path,
                    "container_path": {"kind": "json_pointer", "value": "#"},
                    "item_path": None,
                    "container_cardinality": {"status": "not_applicable", "minimum": None, "maximum": None},
                    "value_type": "array",
                },
            }
        }
        assertion = {
            "payload_kind": "json",
            "accepted_http_status_codes": [200],
            "http_status_source_refs": [source_ref("#/response_contract/accepted_http_status_codes")],
            "required_fields": [
                {"path": code_path, "value_type": "string", "cardinality": {"minimum": 1, "maximum": 1}, "source_refs": [source_ref("#/response_contract/documented_fields/0")]},
                {"path": items_path, "value_type": "array", "cardinality": {"minimum": 1, "maximum": 1}, "source_refs": [source_ref("#/response_contract/documented_fields/1")]},
            ],
            "provider_result_code_status": "expected_success_example",
            "provider_result_code_evidence_refs": [source_ref("#/response_contract/provider_result_codes")],
            "provider_result_codes": {
                "path": code_path,
                "value_type": "string",
                "basis": "official_success_example",
                "success_values": ["00"],
                "source_refs": [source_ref("#/response_contract/provider_result_codes")],
            },
            "result_collection": {
                "path": items_path,
                "container_path": {"kind": "json_pointer", "value": "#"},
                "item_path": None,
                "value_type": "array",
                "semantics": "valid",
                "source_refs": [source_ref("#/response_contract/result_collection")],
            },
        }
        self.compiler._validate_assertion_fact_binding({"assertion": assertion}, document, document_ref)
        unknown = copy.deepcopy(document)
        unknown["response_contract"]["provider_result_codes"]["status"] = "unknown"
        with self.assertRaisesRegex(self.compiler.PlanError, "exact documented or official-example"):
            self.compiler._validate_assertion_fact_binding({"assertion": assertion}, unknown, document_ref)

        overlapping_document = copy.deepcopy(document)
        overlapping_codes = overlapping_document["response_contract"]["provider_result_codes"]
        overlapping_codes.update({
            "status": "documented",
            "success_values": {"status": "documented", "values": ["00"]},
            "error_values": {"status": "documented", "values": ["00"]},
        })
        overlapping_assertion = copy.deepcopy(assertion)
        overlapping_assertion["provider_result_code_status"] = "documented"
        overlapping_assertion["provider_result_codes"].update({
            "basis": "documented_code_map",
            "success_values": ["00"],
            "error_values": ["00"],
        })
        with self.assertRaisesRegex(self.compiler.PlanError, "sets overlap"):
            self.compiler._validate_assertion_fact_binding({"assertion": overlapping_assertion}, overlapping_document, document_ref)

        invalid_cardinality = copy.deepcopy(assertion)
        invalid_cardinality["required_fields"][0]["cardinality"] = {"minimum": 2, "maximum": 1}
        with self.assertRaisesRegex(self.compiler.PlanError, "cardinality is inverted"):
            self.compiler._validate_assertion_fact_binding({"assertion": invalid_cardinality}, document, document_ref)


if __name__ == "__main__":
    unittest.main()
